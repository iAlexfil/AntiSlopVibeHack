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
  $('#visionPreview').removeAttribute('src');
  $('#uploadSection').hidden = false;
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
  $('#uploadSection').hidden = false;
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
  const decisive = probability.decisive_source;
  const conflict = !decisive && probability.label === 'Низкая согласованность';
  const tone = !numeric ? 'unknown' : conflict ? 'conflict' : value >= 65 ? 'high' : value <= 35 ? 'low' : 'mid';
  $('#scoreCard').className = `score-card ${tone}`;
  $('#resultFilename').textContent = data.filename || selectedFile?.name || 'Изображение';
  $('#resultFilename').title = $('#resultFilename').textContent;
  $('#resultMeta').textContent = [data.format, data.width && data.height ? `${data.width} × ${data.height}` : '', Number.isFinite(data.size_bytes) ? formatBytes(data.size_bytes) : ''].filter(Boolean).join(' · ');
  $('#probabilityValue').textContent = numeric ? `${value}%` : '—';
  $('#probabilityFill').style.width = numeric ? `${value}%` : '0%';
  $('#resultHeadline').textContent = !numeric ? 'Недостаточно данных' : decisive ? `Обнаружена метка ${decisive}` : conflict ? 'Оценки расходятся' : value >= 65 ? 'Есть признаки генерации ИИ' : value <= 35 ? 'Мало признаков генерации ИИ' : 'Результат неоднозначен';
  const count = probability.sources || 0;
  const ending = count % 10 === 1 && count % 100 !== 11 ? 'источник' : [2, 3, 4].includes(count % 10) && ![12, 13, 14].includes(count % 100) ? 'источника' : 'источников';
  const failures = (data.api_results || []).filter((item) => item.status === 'error').length;
  $('#resultSources').textContent = count ? `${count} ${ending}${failures ? ` · Ошибок: ${failures}` : ''}` : 'Нет оценок';
  $('#scoreBasis').textContent = probability.detail || 'Источники оценки отсутствуют.';
}

function renderBreakdown(data) {
  const root = $('#breakdownGrid');
  root.innerHTML = '';
  const results = data.api_results || [];
  const remote = results.some((item) => item.id !== 'local_frontier');
  const labels = {model: 'Локальная модель', semantic: 'Анализ содержимого', external: 'Внешние детекторы'};
  (data.probability_breakdown || []).filter((item) => labels[item.id]).forEach((item) => {
    const providers = results.filter((result) => item.id === 'model' ? result.id === 'local_frontier' : item.id === 'semantic' ? result.id === 'openai_vision' : !['local_frontier', 'openai_vision', 'synthid'].includes(result.id) && !result.provenance);
    if (item.id === 'external' && !providers.some((result) => !['not_configured', 'unavailable'].includes(result.status))) return;
    const numeric = Number.isFinite(item.percent);
    const status = providers.find((result) => result.status === 'error')?.status || providers[0]?.status || item.status;
    const missing = item.id !== 'model' && !remote ? 'Выключен' : status === 'error' ? 'Ошибка' : status === 'ok' ? 'Нет оценки' : status === 'unavailable' ? 'Недоступен' : status === 'unsupported' ? 'Формат не поддержан' : 'Не подключён';
    const tone = !numeric ? 'missing' : item.percent >= 65 ? 'high' : item.percent >= 36 ? 'mid' : 'low';
    const card = document.createElement('article');
    card.className = `metric ${tone}`;
    card.innerHTML = '<div class="metric-top"><span class="metric-title"></span><strong class="metric-score"></strong></div><div class="metric-bar"><span></span></div>';
    card.querySelector('.metric-title').textContent = labels[item.id];
    const number = card.querySelector('strong');
    number.textContent = numeric ? `${item.percent}%` : missing;
    if (!numeric) number.classList.add('missing');
    card.querySelector('.metric-bar').hidden = !numeric;
    card.querySelector('.metric-bar span').style.width = numeric ? `${item.percent}%` : '0%';
    root.append(card);
  });
  renderProvenance(data);
}

function appendCheck(root, title, result, status = '') {
  const card = document.createElement('article');
  card.className = `method-check ${status}`;
  card.innerHTML = '<h3></h3><strong></strong>';
  card.querySelector('h3').textContent = title;
  card.querySelector('strong').textContent = result;
  root.append(card);
  return card;
}

function renderProvenance(data) {
  const root = $('#provenanceChecks');
  root.innerHTML = '';
  const c2pa = data.forensics?.c2pa;
  const labels = {valid: 'Подпись проверена', none: 'Подпись не найдена', untrusted: 'Источник подписи не подтверждён', invalid: 'Подпись не прошла проверку', present: 'Подпись не подтверждена', error: 'Ошибка проверки', unavailable: 'Проверка недоступна'};
  const local = appendCheck(root, 'C2PA в файле', labels[c2pa?.status] || 'Нет результата', c2pa?.status);
  if (c2pa?.found) appendDetail(local, 'ИИ в манифесте', c2pa.generative ? 'Указан' : 'Не указан');
  if (c2pa?.generator) appendDetail(local, 'Приложение', c2pa.generator);
  const provider = (data.api_results || []).find((item) => item.id === 'synthid');
  const unavailable = !provider ? 'Выключено' : {error: 'Ошибка сервиса', not_configured: 'Не подключено', unavailable: 'Недоступно', unsupported: 'Формат не поддерживается'}[provider.status] || 'Нет результата';
  for (const [type, title] of [['synthid', 'SynthID через OpenAI'], ['c2pa', 'C2PA через OpenAI']]) {
    const check = provider?.status === 'ok' ? provider.checks?.find((item) => item.type === type) : null;
    const detected = check?.outcome === 'detected';
    const label = !check ? unavailable : check.validation_state === 'invalid' ? 'Подпись не прошла проверку' : detected ? 'Метка найдена' : 'Метка не найдена';
    const card = appendCheck(root, title, label, detected ? 'detected' : check?.validation_state === 'invalid' ? 'invalid' : provider?.status || 'off');
    if (detected && check.validation_state) appendDetail(card, 'Подпись', {trusted: 'Доверенная', valid: 'Проверена'}[check.validation_state] || 'Доверие не подтверждено');
    for (const [key, name] of [['issuer', 'Издатель'], ['model', 'Модель генерации'], ['generated_at', 'Дата генерации']]) {
      if (check?.[key]) appendDetail(card, name, check[key]);
    }
  }
  if (provider && ['error', 'unsupported'].includes(provider.status) && provider.detail) appendDetail(root, 'OpenAI', provider.detail);
}

function renderFileReport(data) {
  const findings = $('#findings');
  findings.innerHTML = '';
  const findingItems = data.findings || [];
  if (!findingItems.length) {
    findings.innerHTML = '<p class="empty-copy">Не найдены.</p>';
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
  const items = (data.api_results || []).filter((item) => !['openai_vision', 'synthid'].includes(item.id) && !item.provenance);
  root.innerHTML = '';
  const inactive = $('#inactiveDetectors');
  inactive.innerHTML = '';
  if (!items.length) {
    root.innerHTML = '<p class="empty-copy">Нет результатов проверок.</p>';
  }
  items.forEach((item) => {
    const label = {error: 'Ошибка', unavailable: 'Недоступен', unsupported: 'Формат не поддержан', not_configured: 'Не подключён'}[item.status] || 'Нет оценки';
    if (item.id !== 'local_frontier' && ['not_configured', 'unavailable'].includes(item.status)) {
      const row = document.createElement('details');
      row.innerHTML = '<summary></summary><p></p>';
      row.querySelector('summary').textContent = `${item.name}: ${label}`;
      row.querySelector('p').textContent = item.detail || 'Не участвовал в итоговой оценке.';
      inactive.append(row);
      return;
    }
    const numeric = item.status === 'ok' && item.verdict !== 'unknown' && Number.isFinite(item.score);
    const percent = numeric ? Math.max(0, Math.min(99, Math.round(item.score * 100))) : null;
    const card = document.createElement('article');
    card.className = `api-result ${item.status}`;
    card.innerHTML = '<div class="api-result-top"><h3></h3></div><div class="api-score"></div><p></p>';
    card.querySelector('h3').textContent = item.id === 'local_frontier' ? 'Локальная модель' : item.name;
    card.querySelector('.api-score').textContent = numeric ? `${percent}%` : label;
    card.querySelector('p').textContent = item.status !== 'ok' ? item.detail || '' : '';
    card.querySelector('p').hidden = item.status === 'ok';
    if (item.status === 'ok') {
      const details = document.createElement('details');
      details.className = 'method-details';
      details.innerHTML = '<summary>Данные модели</summary>';
      if (item.id !== 'local_frontier' && item.detail) appendDetail(details, 'Ответ', item.detail);
      if (item.id === 'local_frontier') appendDetail(details, 'Модель', item.name);
      if (item.generator) appendDetail(details, 'Предполагаемый генератор', item.generator);
      if (Number.isFinite(item.latency_ms)) appendDetail(details, 'Время проверки', `${(item.latency_ms / 1000).toFixed(1)} с`);
      card.append(details);
    }
    root.append(card);
  });
  if (!(data.api_results || []).some((item) => item.id !== 'local_frontier')) inactive.textContent = 'Внешние детекторы выключены.';
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
  const provider = (data.api_results || []).find((item) => item.id === 'openai_vision');
  const analysis = provider?.status === 'ok' ? provider.analysis : null;
  const root = $('#visualAnalysis');
  root.hidden = false;
  $('#visionJsonSection').hidden = !analysis;
  $('#visionJsonSection').open = false;
  $('#visionJson').textContent = analysis ? JSON.stringify(analysis, null, 2) : '';
  $('#visionResponseDetails').hidden = !provider?.detail || provider.status === 'ok';
  $('#visionResponseDetails').open = false;
  $('#visionResponseText').textContent = provider?.status !== 'ok' ? provider?.detail || '' : '';
  $('#signalCount').hidden = !analysis?.signals?.length;
  $('#signalCount').textContent = analysis?.signals?.length || 0;
  $('#visionHeading').textContent = analysis ? analysis.assessment === 'insufficient_detail' ? 'Недостаточно деталей' : analysis.signals.length ? 'Наблюдения' : 'Ничего не выделено' : !provider ? 'Выключен' : provider.status === 'error' ? 'Ошибка' : provider.status === 'unsupported' ? 'Формат не поддерживается' : provider.status === 'unavailable' ? 'Недоступен' : 'Не подключён';
  $('#visionExplanation').textContent = analysis?.explanation || '';
  $('#visionExplanation').hidden = !analysis?.explanation;
  const image = $('#visionPreview');
  image.onload = () => { $('#visionImageFrame').style.width = `${Math.min(800, 460 * image.naturalWidth / image.naturalHeight)}px`; };
  image.src = preview.src;
  const regions = $('#visionRegions');
  const signals = $('#visionSignals');
  regions.innerHTML = '';
  signals.innerHTML = '';
  const regionCount = (analysis?.signals || []).filter((signal) => signal.region).length;
  $('#regionHint').textContent = regionCount ? 'Рамки приблизительные' : '';
  $('#regionSelection').textContent = '';
  $('#regionSelection').hidden = !analysis?.signals?.length;
  const select = (index, fromImage = false) => {
    [...root.querySelectorAll('[data-signal]'), ...regions.querySelectorAll('[data-signal]')].forEach((element) => {
      const active = element.dataset.signal === String(index);
      element.classList.toggle('active', active);
      if (element.tagName === 'BUTTON') element.setAttribute('aria-pressed', String(active));
    });
    const signal = analysis.signals[index];
    $('#regionSelection').textContent = `${index + 1}. ${signal.observation}`;
    if (fromImage) signals.children[index]?.scrollIntoView({behavior: 'smooth', block: 'nearest'});
    else if (window.matchMedia?.('(max-width: 760px)').matches) $('#visionImageFrame').scrollIntoView({behavior: 'smooth', block: 'center'});
  };
  (analysis?.signals || []).forEach((signal, index) => {
    const card = document.createElement('article');
    card.className = 'vision-signal';
    card.dataset.signal = index;
    const button = document.createElement('button');
    button.type = 'button';
    button.dataset.signal = index;
    button.setAttribute('aria-pressed', 'false');
    button.setAttribute('aria-controls', 'visionImageFrame');
    const number = document.createElement('span');
    number.className = 'signal-number';
    number.textContent = index + 1;
    const title = document.createElement('span');
    title.textContent = signal.observation;
    button.append(number, title);
    button.addEventListener('click', () => select(index));
    const strength = document.createElement('p');
    strength.className = 'signal-strength';
    strength.textContent = {weak: 'Слабый', moderate: 'Средний', strong: 'Сильный'}[signal.strength];
    const explanation = document.createElement('p');
    explanation.className = 'signal-explanation';
    explanation.textContent = signal.explanation;
    const alternative = document.createElement('details');
    alternative.className = 'signal-alternative';
    const summary = document.createElement('summary');
    summary.textContent = 'Другая причина';
    const text = document.createElement('p');
    text.textContent = signal.alternative_explanation;
    alternative.append(summary, text);
    card.append(button, strength, explanation, alternative);
    signals.append(card);
    if (signal.region) {
      const box = document.createElement('button');
      box.type = 'button';
      box.className = 'vision-region';
      box.dataset.signal = index;
      const marker = document.createElement('span');
      marker.textContent = index + 1;
      box.append(marker);
      box.setAttribute('aria-label', `Область ${index + 1}: ${signal.observation}`);
      box.setAttribute('aria-pressed', 'false');
      const {x1, y1, x2, y2} = signal.region;
      Object.assign(box.style, {left: `${x1 * 100}%`, top: `${y1 * 100}%`, width: `${(x2 - x1) * 100}%`, height: `${(y2 - y1) * 100}%`});
      box.addEventListener('click', () => select(index, true));
      regions.append(box);
    }
  });
  if (analysis?.signals.length) select(0);
  const limitations = $('#visionLimitations');
  limitations.innerHTML = '';
  $('#visionLimitsDetails').hidden = !analysis?.limitations?.length;
  $('#visionLimitsDetails').open = false;
  (analysis?.limitations || []).forEach((text) => {
    const item = document.createElement('li');
    item.textContent = text;
    limitations.append(item);
  });
}

function renderForensics(data) {
  const report = data.forensics || {};
  const budget = report.resource_budget;
  const ml = data.local_model;
  const mlBudget = ml?.status === 'ok' ? `ML ${ml.peak_rss_mb} MB / лимит ${ml.memory_limit_mb} MB` : 'ML недоступна';
  $('#resourceBudget').textContent = budget ? `Изображение ≤ ${budget.max_analysis_edge}px · FFT ≤ ${budget.max_fft_edge}px · ${mlBudget}` : 'Данные о ресурсах отсутствуют';

  const signals = $('#forensicSignals');
  signals.innerHTML = '';
  const applicable = (report.signals || []).filter((signal) => ['normal', 'review'].includes(signal.status));
  $('#pixelSummary').textContent = applicable.length ? `Отклонения: ${applicable.filter((signal) => signal.status === 'review').length} из ${applicable.length}` : 'Нет результата';
  const descriptions = {
    ela: ['Повторное JPEG-сжатие', 'Разница после повторного сжатия.'],
    noise: ['Различия шума', 'Мелкие детали и шумовой остаток.'],
    frequency: ['Частотный спектр', 'Спектр частот, не координаты объектов.'],
    repetition: ['Повторяющиеся участки', 'Блоки с похожей текстурой.'],
  };
  if (!report.signals?.length) signals.innerHTML = '<p class="empty-copy">Пиксельные проверки не вернули результатов.</p>';
  (report.signals || []).forEach((signal) => {
    const [title, mapExplanation] = descriptions[signal.id] || [signal.name, ''];
    const card = document.createElement('article');
    card.className = `forensic-signal ${signal.status}`;
    card.innerHTML = '<h3></h3><strong class="forensic-status"></strong>';
    card.querySelector('h3').textContent = title;
    card.querySelector('strong').textContent = {normal: 'Без отклонений', review: 'Есть отклонения', not_applicable: 'Не применимо', error: 'Ошибка проверки', unavailable: 'Недоступно'}[signal.status] || 'Нет результата';
    const details = document.createElement('details');
    details.className = 'method-details';
    details.innerHTML = '<summary>Карта и измерения</summary>';
    appendDetail(details, 'Измеренное значение', [signal.value, signal.unit].filter((value) => value !== undefined && value !== '').join(' '));
    appendDetail(details, 'О проверке', signal.detail);
    const map = (report.maps || []).find((item) => item.id === signal.id);
    if (map) {
      const figure = document.createElement('figure');
      figure.className = 'forensic-map';
      const image = document.createElement('img');
      image.src = map.image;
      image.alt = `Карта: ${title}`;
      image.loading = 'lazy';
      const caption = document.createElement('figcaption');
      caption.textContent = mapExplanation;
      figure.append(image, caption);
      details.append(figure);
    }
    card.append(details);
    signals.append(card);
  });

  const c2pa = $('#c2paResult');
  c2pa.innerHTML = '';
  const badge = document.createElement('span');
  badge.className = `c2pa-badge ${report.c2pa?.status}`;
  badge.textContent = report.c2pa?.status || 'Нет результата';
  c2pa.append(badge);
  appendDetail(c2pa, 'Результат', report.c2pa?.detail || 'Проверка не вернула результата');
  if (report.c2pa?.generator) appendDetail(c2pa, 'Claim generator', report.c2pa.generator);

  const exif = $('#exifChecks');
  exif.innerHTML = '';
  if (!report.exif_checks?.length) exif.textContent = 'Нет данных.';
  (report.exif_checks || []).forEach((check) => appendDetail(exif, check.name === 'Software' ? 'Редактор' : check.name, check.name === 'Камера' && check.status === 'absent' ? 'Нет данных' : check.detail));
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
  $('#uploadSection').hidden = true;
  results.hidden = false;
  $('#resultHeadline').focus({preventScroll: true});
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
$('#repeatCheck').addEventListener('click', () => {
  results.hidden = true;
  $('#uploadSection').hidden = false;
  dropzone.scrollIntoView({ behavior: 'smooth', block: 'center' });
  analyze();
});
$('#newCheck').addEventListener('click', () => { reset(); dropzone.focus(); window.scrollTo({ top: 0, behavior: 'smooth' }); });
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
