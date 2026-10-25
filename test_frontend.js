const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const nodes = new Map();
function node(id) {
  if (!nodes.has(id)) {
    const classes = new Set();
    nodes.set(id, {
      get innerHTML() { return this._html || ''; },
      set innerHTML(value) { this._html = value; this.children = []; this.textContent = ''; },
      children: [], dataset: new Proxy({}, { set(target, key, value) { target[key] = String(value); return true; } }), attributes: {},
      append(...children) { this.children.push(...children); },
      setAttribute(name, value) { this.attributes[name] = value; },
      querySelectorAll() {
        const root = id === 'visualAnalysis' ? node('visionSignals') : this;
        const descendants = (parent) => (parent.children || []).flatMap((child) => [child, ...descendants(child)]);
        return descendants(root).filter((value) => value.dataset?.signal !== undefined);
      },
      src: '', hidden: true, disabled: false, value: '', style: {}, listeners: {},
      classList: {
        add: (name) => classes.add(name), remove: (name) => classes.delete(name),
        toggle(name, active) { if (active) classes.add(name); else classes.delete(name); },
        contains: (name) => classes.has(name),
      },
      addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); },
      emit(name, event = {}) { (this.listeners[name] || []).forEach((callback) => callback(event)); },
      querySelector: (selector) => node(selector === 'span' ? `${id}-label` : `${id}:${selector}`),
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
    createElement: (tag) => { const result = node(`created-${nodes.size}`); result.tagName = tag.toUpperCase(); return result; },
    createTextNode: (text) => ({ textContent: text }),
    addEventListener: (name, callback) => node('document').addEventListener(name, callback),
  },
  URL: { createObjectURL: (file) => `blob:${file.name}`, revokeObjectURL() {} },
  window: { scrollTo() {} },
  alert: (message) => alerts.push(message),
  fetch: (url, options) => url === '/api/providers'
    ? Promise.resolve({ ok: true, json: async () => [] })
    : new Promise((resolve, reject) => requests.push({ url, options, resolve, reject })),
});
vm.runInContext(fs.readFileSync(path.join(__dirname, 'static/app.js'), 'utf8'), context);
assert.equal(requests.length, 0, 'The upload screen must not request a separate Vision login status');

const html = fs.readFileSync(path.join(__dirname, 'static/index.html'), 'utf8');
const htmlIds = [...html.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]);
assert.equal(new Set(htmlIds).size, htmlIds.length, 'Every results target must have a unique ID');
const imageStart = html.indexOf('id="resultImage"');
const previewStart = html.indexOf('id="visionPreview"');
assert.ok(imageStart >= 0 && previewStart > imageStart, 'The result has a dedicated image block');
assert.equal((html.match(/<img\b[^>]*\bid="visionPreview"/g) || []).length, 1, 'The result image must not be duplicated');
for (const id of ['scoreCard', 'modelAnalysis', 'provenanceAnalysis', 'pixelAnalysis', 'visualAnalysis']) {
  assert.ok(previewStart < html.indexOf(`id="${id}"`), `The image appears before ${id}`);
}
const reportStart = html.indexOf('<details id="reportDetails"');
assert.ok(reportStart > 0);
assert.doesNotMatch(html, /method-contribution|section-intro|id="(?:visionBasis|apiConsensus|provenanceSummary)"/, 'Results must not restore repeated explanations or contribution labels');
assert.match(html, /id="modelsTitle">Модели и детекторы<\/h2><\/div>\s*<div id="apiResults"/, 'The model section starts with results, without an introductory paragraph');
assert.match(html, /id="provenanceTitle">Метки и происхождение<\/h2><\/div>\s*<div id="provenanceChecks"/, 'The provenance section starts with checks, without an introductory paragraph');
assert.doesNotMatch(html.slice(reportStart, html.indexOf('>', reportStart)), /\bopen\b/, 'The technical report starts closed');
for (const id of ['modelAnalysis', 'apiResults', 'inactiveDetectors', 'provenanceAnalysis', 'provenanceChecks', 'findings', 'exifChecks', 'pixelAnalysis', 'forensicSignals', 'visualAnalysis', 'visionResponseDetails', 'visionResponseText']) {
  const position = html.indexOf(`id="${id}"`);
  assert.ok(position >= 0 && position < reportStart, `${id} must be visible independently of the technical report`);
}

const file = (name) => ({ name, type: 'image/png', size: 100 });
const a = file('a.png');
const b = file('b.png');
const rendered = [];
const actualRenderResult = context.renderResult;
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
  context.renderBreakdown({probability_breakdown: [{id: 'model', title: 'Локальная ML-модель', percent: null, status: 'error', detail: 'Ошибка воркера'}]});
  assert.equal(node('breakdownGrid').children.at(-1).querySelector('strong').textContent, 'Ошибка');
  context.renderBreakdown({probability_breakdown: [{id: 'model', title: 'Локальная ML-модель', percent: 65, status: 'ok'}]});
  assert.equal(node('breakdownGrid').children.at(-1).querySelector('strong').textContent, '65%');
  context.chooseFile(a);
  const analysis = {schema_version: '1.0', probability: 90, assessment: 'likely_ai', explanation: '<script>not HTML</script>',
    signals: [{observation: '<img onerror=alert(1)>', strength: 'strong', explanation: 'Видимая деталь', alternative_explanation: 'Монтаж',
      region: {x1: 0.1, y1: 0.2, x2: 0.5, y2: 0.8}}], limitations: ['Мелкие детали']};
  context.renderVision({api_results: [{id: 'openai_vision', status: 'ok', analysis}]});
  assert.equal(node('visualAnalysis').hidden, false);
  assert.equal(node('visionExplanation').textContent, analysis.explanation);
  assert.deepEqual(JSON.parse(node('visionJson').textContent), analysis);
  assert.equal(node('visionPreview').src, 'blob:a.png');
  const box = node('visionRegions').children[0];
  assert.equal(box.style.left, '10%');
  assert.equal(box.style.width, '40%');
  assert.equal(box.attributes['aria-pressed'], 'true', 'The first observation is selected by default');
  box.emit('click');
  assert.equal(box.attributes['aria-pressed'], 'true');
  context.renderVision({api_results: [{id: 'openai_vision', status: 'ok', analysis: {...analysis, signals: [analysis.signals[0], {...analysis.signals[0], observation: 'Другой участок'}]}}]});
  const boxes = node('visionRegions').children;
  const cards = node('visionSignals').children;
  assert.equal(boxes[0].attributes['aria-pressed'], 'true');
  assert.equal(boxes[1].attributes['aria-pressed'], 'false');
  boxes[1].emit('click');
  assert.equal(boxes[0].attributes['aria-pressed'], 'false');
  assert.equal(boxes[1].attributes['aria-pressed'], 'true');
  assert.equal(cards[1].children[0].attributes['aria-pressed'], 'true', 'Selecting a region activates its observation outside the image block');
  cards[0].children[0].emit('click');
  assert.equal(boxes[0].attributes['aria-pressed'], 'true', 'Selecting an observation activates its region outside the semantic section');
  assert.equal(boxes[1].attributes['aria-pressed'], 'false');
  assert.equal(cards[0].classList.contains('active'), true);
  assert.equal(cards[1].classList.contains('active'), false);
  const visionError = '<img src=x onerror=alert(1)> Истекло время анализа';
  context.renderVision({api_results: [{id: 'openai_vision', status: 'error', detail: visionError}]});
  assert.equal(node('visionResponseDetails').hidden, false, 'The semantic section must keep the actual LLM failure visible');
  assert.equal(node('visionResponseText').textContent, visionError, 'LLM errors stay literal text');
  assert.equal(node('visionResponseText').innerHTML, '', 'LLM errors must not be inserted as HTML');
  context.renderVision({api_results: [{id: 'openai_vision', status: 'ok', analysis}]});
  assert.equal(node('visionResponseDetails').hidden, true);
  assert.equal(node('visionResponseText').textContent, '', 'A successful analysis clears the previous LLM error');
  context.renderVision({api_results: [{id: 'openai_vision', status: 'error', detail: visionError}]});
  context.renderVision({api_results: []});
  assert.equal(node('visualAnalysis').hidden, false, 'Keep the image visible when visual analysis is disabled');
  assert.equal(node('visionHeading').textContent, 'Выключен');
  assert.equal(node('visionJsonSection').hidden, true);
  assert.equal(node('visionResponseDetails').hidden, true);
  assert.equal(node('visionResponseText').textContent, '', 'Disabling remote analysis clears the previous LLM error');
  context.renderOverall({ generation_probability: { percent: null }, headline: 'Явные водяные знаки не найдены' });
  assert.equal(node('resultHeadline').textContent, 'Недостаточно данных');
  assert.equal(node('probabilityFill').style.width, '0%');
  context.renderOverall({ generation_probability: { percent: 99 }, headline: 'Есть признаки AI-происхождения' });
  assert.equal(node('probabilityValue').textContent, '99%');
  assert.equal(node('resultHeadline').textContent, 'Есть признаки генерации ИИ');

  for (const source of ['SynthID', 'C2PA', 'C2PA (OpenAI)']) {
    context.renderOverall({generation_probability: {percent: 99, label: 'Низкая согласованность', sources: 1, decisive_source: source}});
    assert.equal(node('resultHeadline').textContent, `Обнаружена метка ${source}`, 'Confirmed provenance takes priority over conflicting models');
    assert.equal(node('probabilityValue').textContent, '99%');
    assert.equal(node('scoreCard').className, 'score-card high');
    assert.match(node('resultSources').textContent, /1 источник/);
  }
  context.renderOverall({generation_probability: {percent: 52, label: 'Низкая согласованность', sources: 2}, headline: 'Есть признаки AI-происхождения'});
  assert.equal(node('resultHeadline').textContent, 'Оценки расходятся', 'The next result must clear the previous provenance override');
  assert.equal(node('scoreCard').className, 'score-card conflict');
  assert.equal(node('probabilityValue').textContent, '52%');
  assert.match(node('resultSources').textContent, /2 источника/);
  const localOnly = {findings: [], api_results: [{id: 'local_frontier', status: 'ok', score: .75}], probability_breakdown: [
    {id: 'model', percent: 75, status: 'ok'}, {id: 'semantic', percent: null},
    {id: 'provenance', percent: 0}, {id: 'pixels', percent: 65}],
    forensics: {signals: [{status: 'review'}, {status: 'normal'}]}};
  context.renderBreakdown(localOnly);
  assert.equal(node('breakdownGrid').children.length, 2, 'Only model probabilities belong in the score card');
  assert.equal(node('breakdownGrid').children[1].querySelector('strong').textContent, 'Выключен');
  context.renderForensics(localOnly);
  assert.equal(node('pixelSummary').textContent, 'Отклонения: 1 из 2');
  assert.doesNotMatch(node('pixelSummary').textContent, /%/);
  context.renderVision({api_results: [{id: 'openai_vision', status: 'ok', analysis: {...analysis, signals: [], probability: null, assessment: 'insufficient_detail'}}]});
  assert.equal(node('visionHeading').textContent, 'Недостаточно деталей');
  context.renderBreakdown({...localOnly, api_results: [...localOnly.api_results,
    {id: 'openai_vision', status: 'ok', verdict: 'unknown', score: null}]});
  assert.equal(node('breakdownGrid').children[1].querySelector('strong').textContent, 'Нет оценки');
  context.renderBreakdown({...localOnly, api_results: [...localOnly.api_results,
    {id: 'openai_vision', status: 'error'}]});
  assert.equal(node('breakdownGrid').children[1].querySelector('strong').textContent, 'Ошибка');

  context.renderBreakdown({...localOnly, api_results: [...localOnly.api_results,
    {id: 'synthid', status: 'error'}], probability_breakdown: [...localOnly.probability_breakdown,
    {id: 'external', percent: null, source_count: 0}]});
  assert.equal(node('breakdownGrid').children.length, 2, 'A provenance error must not appear as an external detector');

  context.renderApiResults({api_results: [
    {...localOnly.api_results[0], name: 'Frontier ViT', detail: 'Локальная оценка', latency_ms: 1200},
    {id: 'hive', name: 'Hive', status: 'ok', score: .8, detail: 'Ответ Hive', generator: 'Flux'},
    {id: 'sightengine', name: 'Sightengine', status: 'ok', score: .2},
    {id: 'aiornot', name: 'AI or Not', status: 'error', detail: '<img onerror=alert(1)>'},
    {id: 'wasitai', name: 'WasItAI', status: 'not_configured', detail: 'Не подключён'},
    {id: 'illuminarty', name: 'Illuminarty', status: 'unavailable', detail: 'Нет доступного API'},
    {id: 'openai_vision', name: 'Vision', status: 'ok', score: .9},
    {id: 'synthid', name: 'SynthID', status: 'error', detail: 'Нет ответа'},
    {id: 'future_provenance', name: 'Другая метка', provenance: true, status: 'ok'},
  ]});
  const modelCards = node('apiResults').children;
  assert.deepEqual(modelCards.map((card) => card.querySelector('h3').textContent), ['Локальная модель', 'Hive', 'Sightengine', 'AI or Not']);
  assert.deepEqual(modelCards.map((card) => card.querySelector('.api-score').textContent), ['75%', '80%', '20%', 'Ошибка']);
  assert.equal(modelCards[0].querySelector('p').hidden, true);
  assert.equal(modelCards[0].querySelector('p').textContent, '', 'Successful model cards do not repeat generic explanations');
  assert.equal(modelCards[3].querySelector('p').hidden, false, 'A failure cause is visible directly on the detector card');
  assert.equal(modelCards[3].querySelector('p').textContent, '<img onerror=alert(1)>', 'API errors stay literal text');
  assert.equal(modelCards[3].querySelector('p').innerHTML, '', 'API errors must not be inserted as HTML');
  assert.deepEqual(node('inactiveDetectors').children.map((row) => row.querySelector('summary').textContent), ['WasItAI: Не подключён', 'Illuminarty: Недоступен']);
  context.renderApiResults(localOnly);
  assert.equal(node('apiResults').children.length, 1);
  assert.equal(node('inactiveDetectors').children.length, 0, 'Inactive results from the previous image must be cleared');
  assert.equal(node('inactiveDetectors').textContent, 'Внешние детекторы выключены.');
  context.renderApiResults({api_results: [{id: 'hive', name: 'Hive', status: 'ok', score: null}]});
  assert.equal(node('apiResults').children[0].querySelector('.api-score').textContent, 'Нет оценки');
  const nearCertainLocal = {...localOnly, api_results: [{...localOnly.api_results[0], score: .9999}], probability_breakdown: [{id: 'model', percent: 99, status: 'ok'}]};
  context.renderBreakdown(nearCertainLocal);
  context.renderApiResults(nearCertainLocal);
  assert.equal(node('apiResults').children[0].querySelector('.api-score').textContent, '99%');
  assert.equal(node('apiResults').children[0].querySelector('.api-score').textContent, node('breakdownGrid').children[0].querySelector('strong').textContent, 'The detailed local score agrees with the score card near the display cap');
  context.renderApiResults({api_results: [{id: 'local_frontier', status: 'error', detail: 'Воркер превысил время ожидания'}]});
  assert.equal(node('apiResults').children[0].querySelector('.api-score').textContent, 'Ошибка');
  assert.equal(node('apiResults').children[0].querySelector('p').textContent, 'Воркер превысил время ожидания');
  assert.equal(node('apiResults').children[0].querySelector('p').hidden, false, 'Local worker errors are visible without opening another report');

  const provenanceLabels = () => node('provenanceChecks').children.slice(0, 3).map((card) => card.querySelector('strong').textContent);
  const detailText = (root) => root.children.filter((child) => child.className === 'detail-row').map((row) => row.children.map((child) => child.textContent || '').join('')).join(' ');
  for (const [status, label] of [['untrusted', 'Источник подписи не подтверждён'], ['invalid', 'Подпись не прошла проверку'], ['valid', 'Подпись проверена']]) {
    context.renderProvenance({forensics: {c2pa: {status, found: true, generative: true, signature_valid: status === 'valid', detail: 'Подпись подтверждена', generator: 'Приложение'}}});
    const local = node('provenanceChecks').children[0];
    assert.equal(provenanceLabels()[0], label);
    assert.match(detailText(local), /ИИ в манифесте: Указан/);
    assert.match(detailText(local), /Приложение: Приложение/);
    if (status !== 'valid') assert.doesNotMatch(detailText(local), /Подпись подтверждена/, 'An untrusted generative manifest must not claim a verified signature');
  }
  context.renderProvenance({api_results: [{id: 'synthid', status: 'ok', checks: [{type: 'synthid', outcome: 'not_detected'}]}]});
  assert.deepEqual(provenanceLabels(), ['Нет результата', 'Метка не найдена', 'Нет результата'], 'An omitted C2PA check is not a negative result');
  context.renderProvenance({api_results: [{id: 'synthid', status: 'ok', checks: [{type: 'c2pa', outcome: 'not_detected', validation_state: 'invalid'}]}]});
  assert.equal(provenanceLabels()[2], 'Подпись не прошла проверку');
  for (const [status, label] of [['error', 'Ошибка сервиса'], ['not_configured', 'Не подключено'], ['unsupported', 'Формат не поддерживается'], ['unavailable', 'Недоступно']]) {
    context.renderProvenance({api_results: [{id: 'synthid', status, detail: 'Причина проверки'}]});
    assert.deepEqual(provenanceLabels().slice(1), [label, label]);
    if (['error', 'unsupported'].includes(status)) assert.match(detailText(node('provenanceChecks')), /Причина проверки/);
    else assert.equal(detailText(node('provenanceChecks')), '', 'Unavailable services do not repeat a generic setup explanation');
  }
  context.renderProvenance({api_results: [{id: 'synthid', status: 'ok', checks: [{type: 'synthid', outcome: 'detected', model: 'Image', generated_at: '2026-10-25'}]}]});
  assert.equal(provenanceLabels()[1], 'Метка найдена');
  assert.match(detailText(node('provenanceChecks').children[1]), /Модель генерации: Image/);
  assert.match(detailText(node('provenanceChecks').children[1]), /Дата генерации: 2026-10-25/);
  context.renderProvenance({});
  assert.deepEqual(provenanceLabels(), ['Нет результата', 'Выключено', 'Выключено']);
  assert.equal(node('provenanceChecks').children.length, 3, 'Previous provenance metadata and errors must not remain');

  const forensicIds = ['ela', 'noise', 'frequency', 'repetition'];
  const forensicTitles = ['Повторное JPEG-сжатие', 'Различия шума', 'Частотный спектр', 'Повторяющиеся участки'];
  context.renderForensics({forensics: {
    signals: forensicIds.map((id, index) => ({id, name: id, value: index, unit: 'test unit', status: ['not_applicable', 'review', 'normal', 'review'][index], detail: `Подробности ${id}`})),
    maps: [...forensicIds].reverse().map((id) => ({id, image: `data:image/png;${id}`})),
    exif_checks: [{name: 'Software', status: 'present', detail: 'Редактор изображения'}], hashes: {sha256: 'abc'},
  }});
  const forensicCards = node('forensicSignals').children;
  assert.deepEqual(forensicCards.map((card) => card.querySelector('h3').textContent), forensicTitles);
  assert.deepEqual(forensicCards.map((card) => card.querySelector('strong').textContent), ['Не применимо', 'Есть отклонения', 'Без отклонений', 'Есть отклонения']);
  assert.equal(node('pixelSummary').textContent, 'Отклонения: 2 из 3');
  forensicCards.forEach((card, index) => {
    const details = card.children[0];
    assert.equal(details.tagName, 'DETAILS', 'Each method has its own map and measurements disclosure');
    assert.notEqual(details.open, true, 'Measurements start collapsed');
    assert.match(detailText(details), new RegExp(`${index} test unit`));
    assert.match(detailText(details), new RegExp(`Подробности ${forensicIds[index]}`), 'The raw method result remains available next to its map');
    const figure = details.children.find((child) => child.tagName === 'FIGURE');
    assert.equal(figure.children[0].src, `data:image/png;${forensicIds[index]}`, 'Maps match signal IDs regardless of map array order');
    assert.equal(figure.children[0].alt, `Карта: ${forensicTitles[index]}`);
    assert.equal(figure.children[1].tagName, 'FIGCAPTION');
    assert.ok(figure.children[1].textContent.length > 0, 'Each map keeps a short caption');
    if (forensicIds[index] === 'frequency') assert.equal(figure.children[1].textContent, 'Спектр частот, не координаты объектов.');
  });
  assert.match(detailText(node('exifChecks')), /Редактор: Редактор изображения/);
  context.renderForensics({});
  assert.equal(node('forensicSignals').children.length, 0, 'Missing forensics clears previous cards and maps');
  assert.match(node('forensicSignals').innerHTML, /не вернули результатов/);
  assert.equal(node('exifChecks').children.length, 0);
  assert.equal(node('exifChecks').textContent, 'Нет данных.');
  assert.equal(node('hashes').children.length, 0);
  assert.equal(node('c2paResult').children[0].textContent, 'Нет результата');
  assert.equal(node('resourceBudget').textContent, 'Данные о ресурсах отсутствуют');

  actualRenderResult({filename: 'a.png', format: 'PNG', width: 640, height: 400, size_bytes: 100,
    generation_probability: {percent: 90, sources: 1},
    api_results: [{id: 'openai_vision', name: 'Vision', status: 'ok', analysis, score: .9}],
    probability_breakdown: [{id: 'semantic', percent: 90}]});
  assert.equal(node('uploadSection').hidden, true);
  assert.equal(node('results').hidden, false);
  assert.equal(node('reportDetails').open, false);
  assert.equal(node('visionJsonSection').hidden, false);
  context.reset();
  assert.equal(node('uploadSection').hidden, false);
  assert.equal(node('results').hidden, true);

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
