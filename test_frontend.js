const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const nodes = new Map();
function node(id) {
  if (!nodes.has(id)) {
    const classes = new Set();
    nodes.set(id, {
      src: '', hidden: true, disabled: false, value: '', style: {}, listeners: {},
      classList: {
        add: (name) => classes.add(name), remove: (name) => classes.delete(name),
        contains: (name) => classes.has(name),
      },
      addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); },
      emit(name, event = {}) { (this.listeners[name] || []).forEach((callback) => callback(event)); },
      querySelector: (selector) => selector === 'span' ? node(`${id}-label`) : null,
      removeAttribute(name) { this[name] = ''; },
      click() { this.clicks = (this.clicks || 0) + 1; },
      focus() {}, scrollIntoView() {},
    });
  }
  return nodes.get(id);
}

const requests = [];
const alerts = [];
const context = vm.createContext({
  document: {
    querySelector: (selector) => node(selector.slice(1)),
    addEventListener: (name, callback) => node('document').addEventListener(name, callback),
  },
  URL: { createObjectURL: (file) => `blob:${file.name}`, revokeObjectURL() {} },
  window: { scrollTo() {} },
  alert: (message) => alerts.push(message),
  fetch: (url, options) => url === '/api/providers'
    ? Promise.resolve({ json: async () => [] })
    : new Promise((resolve, reject) => requests.push({ url, options, resolve, reject })),
});
vm.runInContext(fs.readFileSync(path.join(__dirname, 'static/app.js'), 'utf8'), context);

const file = (name) => ({ name, type: 'image/png', size: 100 });
const a = file('a.png');
const b = file('b.png');
const rendered = [];
context.renderResult = (data) => rendered.push(data);
function start() {
  const done = context.analyze();
  return { ...requests.at(-1), done };
}
async function respond(request, data) {
  request.resolve({ ok: true, json: async () => data });
  await request.done;
}

async function main() {
  context.renderOverall({ generation_probability: { percent: null }, headline: 'Явные водяные знаки не найдены' });
  assert.equal(node('resultHeadline').textContent, 'Недостаточно данных');
  assert.equal(node('probabilityFill').style.width, '0%');
  context.renderOverall({ generation_probability: { percent: 99 }, headline: 'Есть признаки AI-происхождения' });
  assert.equal(node('probabilityValue').textContent, '99%');
  assert.equal(node('resultHeadline').textContent, 'Есть признаки AI-происхождения');

  node('pickButton').emit('click', { stopPropagation() {} });
  assert.equal(node('fileInput').clicks, 1);
  node('fileInput').files = [a];
  node('fileInput').emit('change');
  assert.equal(node('preview').src, 'blob:a.png');
  node('dropzone').emit('drop', { preventDefault() {}, dataTransfer: { files: [b] } });
  assert.equal(node('preview').src, 'blob:b.png');
  node('document').emit('paste', {
    preventDefault() {}, clipboardData: { items: [{ kind: 'file', type: a.type, getAsFile: () => a }] },
  });
  assert.equal(node('preview').src, 'blob:a.png');
  node('document').emit('paste', { preventDefault() {}, clipboardData: { items: [], files: [b] } });
  assert.equal(node('preview').src, 'blob:b.png');

  context.chooseFile(a);
  node('useRemote').checked = false;
  const old = start();
  assert.match(old.url, /remote=false$/);
  assert.equal(old.options.body, a);
  context.chooseFile(b);
  assert.equal(node('analyzeButton').disabled, false);
  assert.equal(node('analyzeButton-label').textContent, 'Проверить');
  assert.equal(node('dropzone').classList.contains('scanning'), false);
  node('useRemote').checked = true;
  const current = start();
  assert.match(current.url, /remote=true$/);
  await respond(old, { filename: 'a.png' });
  assert.equal(rendered.length, 0, 'An old response must not render against the new image');
  assert.equal(node('analyzeButton').disabled, true, 'An old finally must not enable an active button');
  assert.equal(node('dropzone').classList.contains('scanning'), true);
  await respond(current, { filename: 'b.png' });
  assert.equal(rendered.length, 1);
  assert.equal(rendered[0].filename, 'b.png');
  assert.equal(node('analyzeButton').disabled, false);
  assert.equal(node('dropzone').classList.contains('scanning'), false);

  context.chooseFile(a);
  const failing = start();
  context.chooseFile(b);
  const active = start();
  failing.reject(new Error('outdated failure'));
  await failing.done;
  assert.equal(alerts.length, 0, 'Errors from an old image must not interrupt the current check');
  assert.equal(node('analyzeButton').disabled, true);
  assert.equal(node('dropzone').classList.contains('scanning'), true);
  await respond(active, { filename: 'b.png' });

  const beforeReset = start();
  context.reset();
  const count = rendered.length;
  await respond(beforeReset, { filename: 'b.png' });
  assert.equal(rendered.length, count, 'A removed image must not reappear when its request finishes');
  assert.equal(node('preview').src, '');
  assert.equal(node('results').hidden, true);
  assert.equal(node('analyzeButton').disabled, true);
  assert.equal(node('analyzeButton-label').textContent, 'Проверить');
  assert.equal(node('dropzone').classList.contains('scanning'), false);

  context.chooseFile(a);
  const failure = start();
  failure.reject(new Error('current failure'));
  await failure.done;
  assert.equal(alerts[0], 'current failure');
  assert.equal(node('analyzeButton').disabled, false);
  assert.equal(node('dropzone').classList.contains('scanning'), false);
  console.log('frontend state and upload tests passed');
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
