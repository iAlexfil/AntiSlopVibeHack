const $ = (selector) => document.querySelector(selector);
const dropzone = $('#dropzone');
const fileInput = $('#fileInput');
const analyzeButton = $('#analyzeButton');
const preview = $('#preview');
const results = $('#results');
let selectedFile = null;

const formatBytes = (bytes) => {
  if (bytes < 1024) return `${bytes} Б`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} КБ`;
  return `${(bytes / 1024 / 1024).toFixed(1)} МБ`;
};

function reset() {
  if (preview.src) URL.revokeObjectURL(preview.src);
  selectedFile = null;
  fileInput.value = '';
  preview.removeAttribute('src');
  $('#resultPreview').removeAttribute('src');
  $('#previewState').hidden = true;
  $('#emptyState').hidden = false;
  analyzeButton.disabled = true;
  analyzeButton.querySelector('span').textContent = 'Проверить изображение';
  dropzone.classList.remove('scanning');
  results.hidden = true;
}

function chooseFile(file) {
  if (!file || !file.type.startsWith('image/')) {
    alert('Выберите изображение PNG, JPG, WEBP, GIF, TIFF или BMP.');
    return;
  }
  if (file.size > 20 * 1024 * 1024) {
    alert('Максимальный размер файла — 20 МБ.');
    return;
  }
  if (preview.src) URL.revokeObjectURL(preview.src);
  selectedFile = file;
  preview.src = URL.createObjectURL(file);
  $('#fileBadge').textContent = `${file.name} · ${formatBytes(file.size)}`;
  $('#previewState').hidden = false;
  $('#emptyState').hidden = true;
  analyzeButton.disabled = false;
  results.hidden = true;
}

function renderProviderStatus(providers) {
  const root = $('#providerStatus');
  root.innerHTML = '';
  providers.forEach((provider) => {
    const chip = document.createElement('div');
    chip.className = `provider-chip ${provider.configured ? 'configured' : provider.unavailable ? 'unavailable' : 'missing'}`;
    const name = document.createElement('b');
    const state = document.createElement('small');
    name.textContent = provider.name;
    state.textContent = provider.configured ? 'ON' : provider.unavailable ? 'NO API' : 'NO KEY';
    chip.title = provider.unavailable || provider.required_env.join(' + ');
    chip.append(name, state);
    root.append(chip);
  });
}

function renderOverall(data) {
  const probability = data.generation_probability || {};
  const value = probability.percent;
  const numeric = Number.isFinite(value);
  const tone = !numeric ? 'unknown' : value >= 65 ? 'high' : value <= 35 ? 'low' : 'mid';
  const card = $('#scoreCard');
  card.className = `score-card ${tone}`;
  $('#resultPreview').src = preview.src;
  $('#probabilityValue').textContent = numeric ? `${value}%` : '—';
  $('#probabilityFill').style.width = numeric ? `${value}%` : '0%';
  $('#resultLabel').textContent = !numeric ? 'Недостаточно данных' : value >= 65 ? 'Высокая вероятность' : value <= 35 ? 'Низкая вероятность' : 'Неоднозначный результат';
  $('#resultHeadline').textContent = data.headline || 'Анализ завершён';
  $('#resultSummary').textContent = numeric
    ? `Итоговая оценка: ${value}%. Она собрана из доступных модельных ответов и подтверждённых признаков происхождения.`
    : 'Вероятностные модели не ответили. Отсутствие метаданных само по себе ничего не доказывает.';
  $('#scoreBasis').textContent = probability.detail || 'Источники оценки отсутствуют.';
}

function renderBreakdown(data) {
  const root = $('#breakdownGrid');
  const icons = { model: 'ML', semantic: 'AI', external: 'API', provenance: 'C2', pixels: 'PX' };
  root.innerHTML = '';
  (data.probability_breakdown || []).forEach((item) => {
    const numeric = Number.isFinite(item.percent);
    const tone = !numeric ? 'missing' : item.percent >= 65 ? 'high' : item.percent >= 36 ? 'mid' : 'low';
    const card = document.createElement('article');
    card.className = `breakdown-card ${tone}`;
    card.innerHTML = '<div class="breakdown-top"><span class="breakdown-icon"></span><span class="breakdown-state"></span></div><strong class="breakdown-number"></strong><h3></h3><p></p><div class="breakdown-bar"><span></span></div>';
    card.querySelector('.breakdown-icon').textContent = icons[item.id] || '•';
    card.querySelector('.breakdown-state').textContent = !numeric ? 'нет данных' : item.percent >= 65 ? 'сильный сигнал' : item.percent >= 36 ? 'средний сигнал' : 'слабый сигнал';
    const number = card.querySelector('.breakdown-number');
    number.textContent = numeric ? `${item.percent}%` : '—';
    if (!numeric) number.classList.add('missing');
    card.querySelector('h3').textContent = item.title;
    card.querySelector('p').textContent = item.detail;
    card.querySelector('.breakdown-bar span').style.width = numeric ? `${item.percent}%` : '0%';
    root.append(card);
  });
}

function renderFileReport(data) {
  const findings = $('#findings');
  findings.innerHTML = '';
  if (!data.findings.length) {
    findings.innerHTML = '<p class="empty-copy">AI-специфичные метаданные, C2PA-указание и видимый бейдж не найдены. Модельные результаты показаны отдельно ниже.</p>';
  } else {
    data.findings.forEach((item) => {
      const row = document.createElement('div');
      row.className = `finding ${item.level}`;
      row.innerHTML = '<span class="finding-dot"></span><div><b></b><p></p></div>';
      row.querySelector('b').textContent = item.title;
      row.querySelector('p').textContent = item.detail;
      findings.append(row);
    });
  }

  const facts = [['Формат', data.format], ['Разрешение', `${data.width} × ${data.height}`], ['Размер файла', formatBytes(data.size_bytes)], ['Цветовой режим', data.mode]];
  const factsRoot = $('#fileFacts');
  factsRoot.innerHTML = '';
  facts.forEach(([key, value]) => {
    const fact = document.createElement('div');
    fact.className = 'fact';
    fact.innerHTML = '<span></span><b></b>';
    fact.querySelector('span').textContent = key;
    fact.querySelector('b').textContent = value;
    factsRoot.append(fact);
  });

  const metadata = $('#metadata');
  metadata.innerHTML = '';
  if (!data.metadata.length) metadata.textContent = 'Метаданные отсутствуют или были очищены.';
  data.metadata.forEach((item) => {
    const row = document.createElement('div');
    row.textContent = `${item.key}: ${item.value}`;
    metadata.append(row);
  });
}

function renderApiResults(data) {
  const root = $('#apiResults');
  const items = data.api_results || [];
  root.innerHTML = '';
  $('#apiConsensus').textContent = data.api_summary?.checked ? `${data.api_summary.ai_votes}/${data.api_summary.checked} ответов за AI` : 'Только локальные эвристики';
  if (!items.length) {
    root.innerHTML = '<div class="api-result"><h4>Внешние API отключены</h4><p>Для этого запуска файл остался локально.</p></div>';
    return;
  }
  items.forEach((item) => {
    const card = document.createElement('article');
    const verdict = item.verdict || item.status;
    card.className = `api-result ${item.status} ${verdict}`;
    const label = item.status === 'ok' ? item.verdict === 'ai' ? 'AI' : item.verdict === 'unknown' ? 'UNKNOWN' : 'HUMAN' : item.status === 'error' ? 'ERROR' : item.status === 'unavailable' ? 'NO API' : 'NO KEY';
    card.innerHTML = '<div class="api-result-top"><h4></h4><span class="status"></span></div><div class="api-score"></div><p></p>';
    card.querySelector('h4').textContent = item.name;
    card.querySelector('.status').textContent = label;
    card.querySelector('.api-score').textContent = item.status === 'ok' ? `${Math.round(item.score * 100)}%` : '—';
    card.querySelector('p').textContent = item.detail || '';
    if (item.generator) {
      const generator = document.createElement('span');
      generator.className = 'api-generator';
      generator.textContent = item.generator;
      card.append(generator);
    }
    root.append(card);
  });
}

function appendDetail(root, label, value) {
  const row = document.createElement('div');
  row.className = 'detail-row';
  const name = document.createElement('b');
  name.textContent = `${label}: `;
  row.append(name, document.createTextNode(value));
  root.append(row);
}

function renderForensics(data) {
  const report = data.forensics;
  if (!report) return;
  const budget = report.resource_budget;
  const ml = data.local_model;
  const mlBudget = ml?.status === 'ok' ? `ML ${ml.peak_rss_mb} MB / лимит ${ml.memory_limit_mb} MB` : 'ML недоступна';
  $('#resourceBudget').textContent = `Изображение ≤ ${budget.max_analysis_edge}px · FFT ≤ ${budget.max_fft_edge}px · ${mlBudget}`;

  const signals = $('#forensicSignals');
  signals.innerHTML = '';
  report.signals.forEach((signal) => {
    const card = document.createElement('article');
    card.className = `forensic-signal ${signal.status}`;
    card.innerHTML = '<div class="forensic-signal-top"><b></b><span class="lamp"></span></div><div class="forensic-value"></div><small></small><p></p>';
    card.querySelector('b').textContent = signal.name;
    card.querySelector('.forensic-value').textContent = signal.value;
    card.querySelector('small').textContent = signal.unit;
    card.querySelector('p').textContent = signal.detail;
    signals.append(card);
  });

  const maps = $('#forensicMaps');
  maps.innerHTML = '';
  report.maps.forEach((map) => {
    const card = document.createElement('figure');
    card.className = 'forensic-map';
    const image = document.createElement('img');
    image.src = map.image;
    image.alt = `Карта: ${map.name}`;
    image.loading = 'lazy';
    const label = document.createElement('span');
    label.textContent = map.name;
    card.append(image, label);
    maps.append(card);
  });

  const c2pa = $('#c2paResult');
  c2pa.innerHTML = '';
  const badge = document.createElement('span');
  badge.className = `c2pa-badge ${report.c2pa.status}`;
  badge.textContent = report.c2pa.status;
  c2pa.append(badge);
  appendDetail(c2pa, 'Результат', report.c2pa.detail);
  if (report.c2pa.generator) appendDetail(c2pa, 'Claim generator', report.c2pa.generator);

  const exif = $('#exifChecks');
  exif.innerHTML = '';
  report.exif_checks.forEach((check) => appendDetail(exif, check.name, check.detail));
  const hashes = $('#hashes');
  hashes.innerHTML = '';
  Object.entries(report.hashes).forEach(([name, value]) => appendDetail(hashes, name, value));
}

function renderResult(data) {
  renderOverall(data);
  renderBreakdown(data);
  renderFileReport(data);
  renderApiResults(data);
  renderForensics(data);
  $('#reportDetails').open = false;
  results.hidden = false;
  results.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

async function analyze() {
  if (!selectedFile) return;
  analyzeButton.disabled = true;
  analyzeButton.querySelector('span').textContent = 'Анализируем…';
  dropzone.classList.add('scanning');
  try {
    const remote = $('#useRemote').checked;
    const response = await fetch(`/api/analyze?filename=${encodeURIComponent(selectedFile.name)}&remote=${remote}`, {
      method: 'POST',
      headers: { 'Content-Type': selectedFile.type || 'application/octet-stream' },
      body: selectedFile,
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Не удалось проверить файл');
    renderResult(data);
  } catch (error) {
    alert(error.message || 'Ошибка проверки');
  } finally {
    analyzeButton.disabled = false;
    analyzeButton.querySelector('span').textContent = 'Проверить ещё раз';
    dropzone.classList.remove('scanning');
  }
}

$('#pickButton').addEventListener('click', (event) => { event.stopPropagation(); fileInput.click(); });
dropzone.addEventListener('click', (event) => { if (!event.target.closest('button')) fileInput.click(); });
dropzone.addEventListener('keydown', (event) => { if (event.key === 'Enter' || event.key === ' ') fileInput.click(); });
fileInput.addEventListener('change', () => chooseFile(fileInput.files[0]));
['dragenter', 'dragover'].forEach((name) => dropzone.addEventListener(name, (event) => { event.preventDefault(); dropzone.classList.add('dragging'); }));
['dragleave', 'drop'].forEach((name) => dropzone.addEventListener(name, (event) => { event.preventDefault(); dropzone.classList.remove('dragging'); }));
dropzone.addEventListener('drop', (event) => chooseFile(event.dataTransfer.files[0]));
$('#removeButton').addEventListener('click', (event) => { event.stopPropagation(); reset(); });
$('#newCheck').addEventListener('click', () => { reset(); window.scrollTo({ top: 0, behavior: 'smooth' }); });
analyzeButton.addEventListener('click', analyze);

document.addEventListener('paste', (event) => {
  const clipboard = event.clipboardData;
  if (!clipboard) return;
  const item = Array.from(clipboard.items || []).find((entry) => entry.kind === 'file' && entry.type.startsWith('image/'));
  const file = item?.getAsFile() || Array.from(clipboard.files || []).find((entry) => entry.type.startsWith('image/'));
  if (!file) return;
  event.preventDefault();
  const extension = (file.type.split('/')[1] || 'png').replace('jpeg', 'jpg');
  const namedFile = file.name ? file : new File([file], `clipboard-${Date.now()}.${extension}`, { type: file.type || 'image/png' });
  chooseFile(namedFile);
  dropzone.focus();
});

fetch('/api/providers').then((response) => response.json()).then(renderProviderStatus).catch(() => {
  $('#providerStatus').innerHTML = '<span class="loading-copy">Не удалось получить конфигурацию API</span>';
});
