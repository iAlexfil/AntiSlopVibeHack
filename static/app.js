const $ = (selector) => document.querySelector(selector);
const dropzone = $('#dropzone');
const fileInput = $('#fileInput');
const analyzeButton = $('#analyzeButton');
const preview = $('#preview');
const results = $('#results');
let selectedFile = null;
let analysisVersion = 0;

function setAnalyzeButtonText(text) {
  const label = analyzeButton.querySelector('span');
  if (label) label.textContent = text;
  else analyzeButton.textContent = text;
}

const formatBytes = (bytes) => {
  if (bytes < 1024) return `${bytes} Б`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} КБ`;
  return `${(bytes / 1024 / 1024).toFixed(1)} МБ`;
};

function reset() {
  analysisVersion += 1;
  if (preview.src) URL.revokeObjectURL(preview.src);
  selectedFile = null;
  fileInput.value = '';
  preview.removeAttribute('src');
  $('#resultPreview').removeAttribute('src');
  $('#previewState').hidden = true;
  $('#emptyState').hidden = false;
  analyzeButton.disabled = true;
  setAnalyzeButtonText('Проверить');
  dropzone.classList.remove('scanning');
  results.hidden = true;
  $('#visualAnalysis').hidden = true;
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
  analysisVersion += 1;
  if (preview.src) URL.revokeObjectURL(preview.src);
  selectedFile = file;
  preview.src = URL.createObjectURL(file);
  $('#fileBadge').textContent = `${file.name} · ${formatBytes(file.size)}`;
  $('#previewState').hidden = false;
  $('#emptyState').hidden = true;
  analyzeButton.disabled = false;
  setAnalyzeButtonText('Проверить');
  dropzone.classList.remove('scanning');
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
    state.textContent = provider.configured ? 'ON' : provider.unavailable ? 'NO API' : provider.id === 'openai_vision' ? 'НУЖЕН ВХОД' : 'NO KEY';
    chip.title = provider.unavailable || provider.detail || provider.required_env.join(' + ');
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
  $('#resultHeadline').textContent = !numeric ? 'Недостаточно данных' : data.headline || 'Анализ завершён';
  $('#resultSummary').textContent = numeric
    ? `Итоговая оценка: ${value}%. Она собрана из доступных модельных ответов и подтверждённых признаков происхождения.`
    : 'Вероятностные модели не ответили. Отсутствие метаданных само по себе ничего не доказывает.';
  $('#scoreBasis').textContent = probability.detail || 'Источники оценки отсутствуют.';
}

function renderBreakdown(data) {
  const root = $('#breakdownGrid');
  root.innerHTML = '';
  (data.probability_breakdown || []).forEach((item) => {
    const numeric = Number.isFinite(item.percent);
    const tone = !numeric ? 'missing' : item.percent >= 65 ? 'high' : item.percent >= 36 ? 'mid' : 'low';
    const card = document.createElement('div');
    card.className = `metric ${tone}`;
    card.title = item.detail;
    card.innerHTML = '<div class="metric-main"><span></span><strong></strong></div><div class="metric-bar"><span></span></div>';
    card.querySelector('.metric-main span').textContent = item.title;
    const number = card.querySelector('strong');
    number.textContent = numeric ? `${item.percent}%` : item.status === 'error' ? 'Ошибка' : '—';
    if (!numeric) number.classList.add('missing');
    card.querySelector('.metric-bar span').style.width = numeric ? `${item.percent}%` : '0%';
    root.append(card);
  });
}

function renderFileReport(data) {
  const findings = $('#findings');
  findings.innerHTML = '';
  const findingItems = data.findings || [];
  if (!findingItems.length) {
    findings.innerHTML = '<p class="empty-copy">AI-специфичные метаданные, C2PA-указание и видимый бейдж не найдены. Модельные результаты показаны отдельно ниже.</p>';
  } else {
    findingItems.forEach((item) => {
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
  const metadataItems = data.metadata || [];
  if (!metadataItems.length) metadata.textContent = 'Метаданные отсутствуют или были очищены.';
  metadataItems.forEach((item) => {
    const row = document.createElement('div');
    row.textContent = `${item.key}: ${item.value}`;
    metadata.append(row);
  });
}

function renderApiResults(data) {
  const root = $('#apiResults');
  const items = data.api_results || [];
  root.innerHTML = '';
  $('#apiConsensus').textContent = data.api_summary?.checked ? `Ответили источников: ${data.api_summary.checked}; ошибок: ${data.api_summary.failed}` : 'Нет ответов моделей и сервисов';
  if (!items.length) {
    root.innerHTML = '<div class="api-result"><h4>Внешние API отключены</h4><p>Для этого запуска файл остался локально.</p></div>';
    return;
  }
  items.forEach((item) => {
    const card = document.createElement('article');
    const verdict = item.verdict || item.status;
    card.className = `api-result ${item.status} ${verdict}`;
    let label = item.status === 'ok' ? item.verdict === 'ai' ? 'AI' : item.verdict === 'unknown' ? 'UNKNOWN' : 'HUMAN' : item.status === 'error' ? 'ERROR' : item.status === 'unavailable' ? 'НЕДОСТУПЕН' : item.status === 'unsupported' ? 'ФОРМАТ' : 'NO KEY';
    if (item.provenance && item.status === 'ok') label = item.verdict === 'ai' ? 'НАЙДЕНО' : 'НЕ НАЙДЕНО';
    card.innerHTML = '<div class="api-result-top"><h4></h4><span class="status"></span></div><div class="api-score"></div><p></p>';
    card.querySelector('h4').textContent = item.name;
    card.querySelector('.status').textContent = label;
    card.querySelector('.api-score').textContent = item.provenance && item.status === 'ok'
      ? item.verdict === 'ai' ? 'Признак найден' : 'Не обнаружено'
      : item.status === 'ok' && Number.isFinite(item.score) ? `${Math.round(item.score * 100)}%` : '-';
    card.querySelector('p').textContent = item.detail || '';
    (item.checks || []).forEach((check) => {
      const metadata = [check.validation_state, check.issuer, check.model, check.generated_at].filter(Boolean);
      if (metadata.length) appendDetail(card, check.type === 'synthid' ? 'SynthID' : 'C2PA', metadata.join('; '));
    });
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

function renderVision(data) {
  const result = (data.api_results || []).find((item) => item.id === 'openai_vision' && item.status === 'ok' && item.analysis);
  const root = $('#visualAnalysis');
  root.hidden = !result;
  if (!result) return;
  const analysis = result.analysis;
  $('#visionExplanation').textContent = analysis.explanation;
  $('#visionJson').textContent = JSON.stringify(analysis, null, 2);
  const image = $('#visionPreview');
  image.onload = () => {
    $('#visionImageFrame').style.width = `${Math.min(640, 480 * image.naturalWidth / image.naturalHeight)}px`;
  };
  image.src = preview.src;
  const regions = $('#visionRegions');
  const signals = $('#visionSignals');
  regions.innerHTML = '';
  signals.innerHTML = '';
  const select = (index) => {
    root.querySelectorAll('[data-signal]').forEach((element) => {
      const active = element.dataset.signal === String(index);
      element.classList.toggle('active', active);
      if (element.tagName === 'BUTTON') element.setAttribute('aria-pressed', String(active));
    });
  };
  (analysis.signals || []).forEach((signal, index) => {
    const card = document.createElement('article');
    card.className = 'vision-signal';
    card.dataset.signal = index;
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = `${index + 1}. ${signal.observation}`;
    button.dataset.signal = index;
    button.setAttribute('aria-pressed', 'false');
    button.addEventListener('click', () => select(index));
    card.append(button);
    const strength = {weak: 'слабый', moderate: 'средний', strong: 'сильный'};
    appendDetail(card, 'Сигнал', strength[signal.strength] || signal.strength);
    appendDetail(card, 'Объяснение', signal.explanation);
    appendDetail(card, 'Другая возможная причина', signal.alternative_explanation);
    signals.append(card);
    if (signal.region) {
      const box = document.createElement('button');
      box.type = 'button';
      box.className = 'vision-region';
      box.dataset.signal = index;
      box.textContent = index + 1;
      box.setAttribute('aria-label', `Признак ${index + 1}: ${signal.observation}`);
      box.setAttribute('aria-pressed', 'false');
      const {x1, y1, x2, y2} = signal.region;
      Object.assign(box.style, {left: `${x1 * 100}%`, top: `${y1 * 100}%`, width: `${(x2 - x1) * 100}%`, height: `${(y2 - y1) * 100}%`});
      box.addEventListener('click', () => select(index));
      regions.append(box);
    }
  });
  if (!analysis.signals?.length) signals.textContent = 'Явных визуальных признаков не найдено. Это не подтверждает подлинность.';
  const limitations = $('#visionLimitations');
  limitations.innerHTML = '';
  (analysis.limitations || []).forEach((text) => {
    const item = document.createElement('li');
    item.textContent = text;
    limitations.append(item);
  });
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
  (report.signals || []).forEach((signal) => {
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
  (report.maps || []).forEach((map) => {
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
  (report.exif_checks || []).forEach((check) => appendDetail(exif, check.name, check.detail));
  const hashes = $('#hashes');
  hashes.innerHTML = '';
  Object.entries(report.hashes || {}).forEach(([name, value]) => appendDetail(hashes, name, value));
}

function renderResult(data) {
  renderOverall(data);
  renderBreakdown(data);
  renderFileReport(data);
  renderApiResults(data);
  renderVision(data);
  renderForensics(data);
  $('#reportDetails').open = false;
  results.hidden = false;
  results.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

async function analyze() {
  if (!selectedFile) return;
  const requestVersion = ++analysisVersion;
  analyzeButton.disabled = true;
  setAnalyzeButtonText('Анализируем…');
  dropzone.classList.add('scanning');
  try {
    const remote = $('#useRemote').checked;
    const response = await fetch(`/api/analyze?filename=${encodeURIComponent(selectedFile.name)}&remote=${remote}`, {
      method: 'POST',
      headers: { 'Content-Type': selectedFile.type || 'application/octet-stream' },
      body: selectedFile,
    });
    const data = await response.json();
    if (requestVersion !== analysisVersion) return;
    if (!response.ok) throw new Error(data.detail || 'Не удалось проверить файл');
    renderResult(data);
  } catch (error) {
    if (requestVersion === analysisVersion) alert(error.message || 'Ошибка проверки');
  } finally {
    if (requestVersion === analysisVersion) {
      analyzeButton.disabled = false;
      setAnalyzeButtonText('Проверить ещё раз');
      dropzone.classList.remove('scanning');
    }
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
