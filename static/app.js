const $ = (selector) => document.querySelector(selector);
const dropzone = $('#dropzone');
const fileInput = $('#fileInput');
const analyzeButton = $('#analyzeButton');
const preview = $('#preview');
const results = $('#results');
let selectedFile = null;

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
    number.textContent = numeric ? `${item.percent}%` : '—';
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
  renderForensics(data);
  $('#reportDetails').open = false;
  results.hidden = false;
  results.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

async function analyze() {
  if (!selectedFile) return;
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
    if (!response.ok) throw new Error(data.detail || 'Не удалось проверить файл');
    renderResult(data);
  } catch (error) {
    alert(error.message || 'Ошибка проверки');
  } finally {
    analyzeButton.disabled = false;
    setAnalyzeButtonText('Проверить ещё раз');
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

function clipboardImage(event) {
  const clipboard = event.clipboardData;
  if (!clipboard) return null;
  const item = Array.from(clipboard.items || []).find((entry) => entry.kind === 'file' && entry.type.startsWith('image/'));
  const file = item?.getAsFile() || Array.from(clipboard.files || []).find((entry) => entry.type.startsWith('image/'));
  if (!file) return null;
  const extension = (file.type.split('/')[1] || 'png').replace('jpeg', 'jpg');
  return file.name ? file : new File([file], `clipboard-${Date.now()}.${extension}`, { type: file.type || 'image/png' });
}

document.addEventListener('paste', (event) => {
  const file = clipboardImage(event);
  if (!file) return;
  event.preventDefault();
  if (seriesMode) {
    addSeriesFiles([file]);
    $('#seriesDrop').focus();
    return;
  }
  chooseFile(file);
  dropzone.focus();
});

fetch('/api/providers').then((response) => response.json()).then(renderProviderStatus).catch(() => {
  $('#providerStatus').innerHTML = '<span class="loading-copy">Не удалось получить конфигурацию API</span>';
});

// ---------------------------------------------------------------- Серия ---

const MAX_SERIES = 6;
const seriesDrop = $('#seriesDrop');
const seriesInput = $('#seriesInput');
const seriesButton = $('#seriesButton');
const seriesResults = $('#seriesResults');
let seriesMode = false;
let seriesFiles = [];

function setMode(series) {
  seriesMode = series;
  $('#singleTab').classList.toggle('active', !series);
  $('#seriesTab').classList.toggle('active', series);
  $('#singleTab').setAttribute('aria-selected', String(!series));
  $('#seriesTab').setAttribute('aria-selected', String(series));
  $('#singlePanel').hidden = series;
  $('#seriesPanel').hidden = !series;
  $('#startHint').textContent = series
    ? 'Загрузите несколько снимков одной сцены или одного человека — проверим, согласуются ли они между собой.'
    : 'Загрузите файл или вставьте его из буфера.';
  results.hidden = true;
  seriesResults.hidden = true;
}

function seriesUrl(file) {
  if (!file.previewUrl) file.previewUrl = URL.createObjectURL(file);
  return file.previewUrl;
}

function addSeriesFiles(files) {
  const rejected = [];
  Array.from(files).forEach((file) => {
    if (!file.type.startsWith('image/')) rejected.push(`${file.name}: не изображение`);
    else if (file.size > 20 * 1024 * 1024) rejected.push(`${file.name}: больше 20 МБ`);
    else if (seriesFiles.length >= MAX_SERIES) rejected.push(`${file.name}: максимум ${MAX_SERIES} снимков`);
    else seriesFiles.push(file);
  });
  if (rejected.length) alert(rejected.join('\n'));
  renderSeriesThumbs();
}

function removeSeriesFile(index) {
  const [file] = seriesFiles.splice(index, 1);
  if (file?.previewUrl) URL.revokeObjectURL(file.previewUrl);
  renderSeriesThumbs();
}

function renderSeriesThumbs() {
  const root = $('#seriesThumbs');
  root.innerHTML = '';
  seriesFiles.forEach((file, index) => {
    const thumb = document.createElement('div');
    thumb.className = 'series-thumb';
    const image = document.createElement('img');
    image.src = seriesUrl(file);
    image.alt = file.name;
    const number = document.createElement('span');
    number.className = 'num';
    number.textContent = index + 1;
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'remove-button';
    remove.setAttribute('aria-label', `Убрать снимок ${index + 1}`);
    remove.textContent = '×';
    remove.addEventListener('click', (event) => { event.stopPropagation(); removeSeriesFile(index); });
    thumb.title = `${file.name} · ${formatBytes(file.size)}`;
    thumb.append(image, number, remove);
    root.append(thumb);
  });
  if (seriesFiles.length && seriesFiles.length < MAX_SERIES) {
    const add = document.createElement('button');
    add.type = 'button';
    add.className = 'series-add';
    add.textContent = '+ ещё снимок';
    root.append(add);
  }
  root.hidden = !seriesFiles.length;
  $('#seriesEmpty').hidden = Boolean(seriesFiles.length);
  seriesButton.disabled = seriesFiles.length < 2;
  setButtonText(seriesButton, seriesFiles.length === 1 ? 'Нужен ещё один снимок' : 'Проверить серию');
}

function setButtonText(button, text) {
  const label = button.querySelector('span');
  if (label) label.textContent = text;
  else button.textContent = text;
}

const VERDICT_TEXT = { consistent: 'Согласовано', inconsistent: 'Не согласуется', insufficient: 'Недостаточно данных' };
const PAIR_TEXT = {
  consistent_3d: 'согласовано', inconsistent: 'не сходится', planar: 'плоская сцена',
  near_duplicate: 'почти дубликат', no_overlap: 'нет общих деталей', weak: 'слабое совпадение',
};

function textElement(tag, text, className) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  element.textContent = text;
  return element;
}

function renderSeries(data) {
  const card = $('#seriesCard');
  card.className = `series-card ${data.verdict}`;
  $('#seriesVerdict').textContent = VERDICT_TEXT[data.verdict] || data.verdict;
  $('#seriesHeadline').textContent = data.headline || '';
  const modeText = data.mode === 'person' ? 'один человек' : 'одна сцена';
  const noun = data.count % 10 === 1 && data.count % 100 !== 11 ? 'снимок' : [2, 3, 4].includes(data.count % 10) && ![12, 13, 14].includes(data.count % 100) ? 'снимка' : 'снимков';
  $('#seriesMeta').textContent = `${data.count} ${noun} · режим: ${modeText}${data.latency_ms ? ` · геометрия ${data.latency_ms} мс` : ''}`;

  const actions = $('#seriesActions');
  actions.innerHTML = '';
  (data.recommendations || []).forEach((text) => actions.append(textElement('li', text)));

  const reasons = $('#seriesReasons');
  reasons.innerHTML = '';
  (data.reasons || []).forEach((item) => {
    const row = document.createElement('div');
    row.className = `reason ${item.level} ${item.polarity > 0 ? 'pos' : item.polarity < 0 ? 'neg' : 'neutral'}`;
    const body = document.createElement('div');
    body.append(textElement('b', item.title), textElement('p', item.detail));
    row.append(textElement('span', '', 'reason-dot'), body);
    reasons.append(row);
  });
  if (!reasons.children.length) reasons.append(textElement('p', 'Явных сигналов нет.', 'empty-copy'));

  const images = $('#seriesImages');
  images.innerHTML = '';
  (data.images || []).forEach((item) => {
    const figure = document.createElement('figure');
    figure.className = 'series-image';
    const image = document.createElement('img');
    image.src = seriesFiles[item.index] ? seriesUrl(seriesFiles[item.index]) : '';
    image.alt = item.filename;
    const caption = document.createElement('figcaption');
    const percent = Number.isFinite(item.percent) ? `${item.percent}% AI` : '—';
    caption.append(textElement('span', `${item.index + 1}. ${item.filename}`), textElement('b', percent, item.percent >= 65 ? 'high' : ''));
    caption.title = item.headline || '';
    figure.append(image, caption);
    images.append(figure);
  });

  const faces = data.faces;
  $('#seriesFacesBlock').hidden = !faces;
  if (faces) {
    const faceRoot = $('#seriesFaces');
    faceRoot.innerHTML = '';
    faces.faces.forEach((face, index) => {
      const figure = document.createElement('figure');
      if (face) {
        const image = document.createElement('img');
        image.src = face.crop;
        image.alt = `Лицо на снимке ${index + 1}`;
        figure.append(image, textElement('figcaption', `Снимок ${index + 1} · ${face.size_px}px`));
      } else {
        figure.append(textElement('figcaption', `Снимок ${index + 1}: лицо не найдено`));
      }
      faceRoot.append(figure);
    });
    const pairRoot = $('#seriesFacePairs');
    pairRoot.innerHTML = '';
    faces.pairs.forEach((pair) => {
      const row = document.createElement('div');
      row.className = 'face-pair';
      row.append(
        textElement('b', `Снимки ${pair.a + 1} и ${pair.b + 1}: ${pair.same_person ? 'тот же человек' : 'не совпадает'}`),
        textElement('span', `сходство ${pair.cosine.toFixed(2)} · идентичность пикселей ${pair.face_ssim.toFixed(2)}${pair.pose_diff_deg !== null ? ` · поворот головы ${pair.pose_diff_deg}°` : ''}`),
      );
      pairRoot.append(row);
    });
  }

  const pairs = $('#seriesPairs');
  pairs.innerHTML = '';
  (data.pairs || []).forEach((pair) => {
    const box = document.createElement('article');
    box.className = 'series-pair';
    const top = document.createElement('div');
    top.className = 'series-pair-top';
    top.append(textElement('b', `Снимки ${pair.a + 1} и ${pair.b + 1}`), textElement('span', PAIR_TEXT[pair.status] || pair.status, `chip ${pair.status}`));
    box.append(top, textElement('p', pair.detail || ''));
    if (pair.visual) {
      const image = document.createElement('img');
      image.src = pair.visual;
      image.alt = `Совпадения между снимками ${pair.a + 1} и ${pair.b + 1}`;
      image.loading = 'lazy';
      box.append(image);
    }
    const stats = document.createElement('dl');
    [
      ['Совпадений', pair.matches],
      ['Согласованных', pair.inliers],
      ['Покрытие', Number.isFinite(pair.coverage) ? `${Math.round(pair.coverage * 100)}%` : null],
      ['Перед камерой', pair.cheirality ? `${Math.round(pair.cheirality.ratio * 100)}%` : null],
      ['Без пары', Number.isFinite(pair.suspicious_share) ? `${Math.round(pair.suspicious_share * 100)}%` : null],
    ].forEach(([name, value]) => {
      if (value === null || value === undefined) return;
      const group = document.createElement('div');
      group.append(textElement('dt', `${name}:`), textElement('dd', String(value)));
      stats.append(group);
    });
    box.append(stats);
    pairs.append(box);
  });

  const exif = data.exif_series || {};
  const facts = [
    ['EXIF', `${exif.with_exif ?? 0} из ${data.count}`],
    ['Устройства', (exif.devices || []).join(', ') || '—'],
    ['Разброс времени', exif.time_span_s !== null && exif.time_span_s !== undefined ? `${Math.round(exif.time_span_s)} с` : '—'],
    ['Разброс GPS', exif.gps_max_distance_m !== null && exif.gps_max_distance_m !== undefined ? `${exif.gps_max_distance_m} м` : '—'],
  ];
  const exifRoot = $('#seriesExif');
  exifRoot.innerHTML = '';
  facts.forEach(([key, value]) => {
    const fact = document.createElement('div');
    fact.className = 'fact';
    fact.append(textElement('span', key), textElement('b', value));
    exifRoot.append(fact);
  });

  seriesResults.hidden = false;
  seriesResults.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

async function analyzeSeries() {
  if (seriesFiles.length < 2) return;
  seriesButton.disabled = true;
  setButtonText(seriesButton, 'Анализируем серию…');
  seriesDrop.classList.add('scanning');
  try {
    const form = new FormData();
    seriesFiles.forEach((file) => form.append('files', file, file.name));
    const mode = document.querySelector('input[name="seriesMode"]:checked').value;
    const remote = $('#seriesRemote').checked;
    const response = await fetch(`/api/analyze-set?mode=${mode}&remote=${remote}`, { method: 'POST', body: form });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Не удалось проверить серию');
    renderSeries(data);
  } catch (error) {
    alert(error.message || 'Ошибка проверки серии');
  } finally {
    seriesButton.disabled = seriesFiles.length < 2;
    setButtonText(seriesButton, 'Проверить ещё раз');
    seriesDrop.classList.remove('scanning');
  }
}

$('#singleTab').addEventListener('click', () => setMode(false));
$('#seriesTab').addEventListener('click', () => setMode(true));
$('#seriesPick').addEventListener('click', (event) => { event.stopPropagation(); seriesInput.click(); });
seriesDrop.addEventListener('click', (event) => {
  if (!event.target.closest('button') || event.target.closest('.series-add')) seriesInput.click();
});
seriesDrop.addEventListener('keydown', (event) => { if (event.key === 'Enter' || event.key === ' ') seriesInput.click(); });
seriesInput.addEventListener('change', () => { addSeriesFiles(seriesInput.files); seriesInput.value = ''; });
['dragenter', 'dragover'].forEach((name) => seriesDrop.addEventListener(name, (event) => { event.preventDefault(); seriesDrop.classList.add('dragging'); }));
['dragleave', 'drop'].forEach((name) => seriesDrop.addEventListener(name, (event) => { event.preventDefault(); seriesDrop.classList.remove('dragging'); }));
seriesDrop.addEventListener('drop', (event) => addSeriesFiles(event.dataTransfer.files));
seriesButton.addEventListener('click', analyzeSeries);
$('#seriesNew').addEventListener('click', () => {
  seriesFiles.forEach((file) => file.previewUrl && URL.revokeObjectURL(file.previewUrl));
  seriesFiles = [];
  renderSeriesThumbs();
  seriesResults.hidden = true;
  window.scrollTo({ top: 0, behavior: 'smooth' });
});
