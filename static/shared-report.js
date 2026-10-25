const find = (selector) => document.querySelector(selector);
const token = window.location.pathname.match(/^\/r\/([A-Za-z0-9_-]+)\/?$/)?.[1];
let loadingReport = null;
let copyInProgress = false;

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== null && text !== undefined) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}

function valueText(value) {
  if (value === null || value === undefined || value === '') return '—';
  if (Array.isArray(value)) return value.map(valueText).join(', ');
  return typeof value === 'object' ? JSON.stringify(value) : String(value);
}

function formattedDate(value) {
  if (!value) return '';
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date.toLocaleString('ru-RU', { dateStyle: 'long', timeStyle: 'short' }) : '';
}

function showFailure(title, detail, retry = true) {
  find('#reportLoading').hidden = true;
  find('#savedReport').hidden = true;
  find('#reportError').hidden = false;
  find('#reportErrorTitle').textContent = title;
  find('#reportErrorDetail').textContent = detail;
  find('#retryReport').hidden = !retry;
}

function renderSavedReport(data) {
  const title = typeof data.title === 'string' && data.title ? data.title : 'Результат проверки';
  document.title = `${title} — AntiSlop`;
  find('#savedTitle').textContent = title;
  const dates = [];
  const created = formattedDate(data.created_at);
  const expires = formattedDate(data.expires_at);
  if (created) dates.push(`Сохранён ${created}`);
  if (expires) dates.push(`Доступен до ${expires}`);
  find('#savedDate').textContent = dates.join(' · ');
  const tones = ['high', 'mid', 'low', 'unknown', 'consistent', 'inconsistent', 'insufficient'];
  find('#savedOverview').className = `saved-overview ${tones.includes(data.tone) ? data.tone : 'unknown'}`;
  find('#savedLabel').textContent = data.label || 'Результат проверки';
  find('#savedHeadline').textContent = data.headline || '';
  find('#savedHeadline').hidden = !data.headline || data.headline === data.label;
  find('#savedSummary').textContent = data.summary || '';
  find('#savedSummary').hidden = !data.summary;
  find('#savedScore').textContent = Number.isFinite(data.score) ? `${data.score}%` : '';
  find('#savedScore').hidden = !Number.isFinite(data.score);
  find('#savedPdf').href = `/api/reports/${encodeURIComponent(token)}/pdf`;

  const images = Array.isArray(data.images) ? data.images : [];
  const imageRoot = find('#savedImages');
  imageRoot.replaceChildren();
  images.forEach((item, index) => {
    if (!item || typeof item !== 'object') return;
    const figure = element('figure', null, 'saved-image');
    if (typeof item.preview === 'string' && /^data:image\/jpeg;base64,[A-Za-z0-9+/=\r\n]+$/.test(item.preview)) {
      const image = element('img');
      image.src = item.preview;
      image.alt = item.label || `Снимок ${index + 1}`;
      image.loading = 'lazy';
      figure.append(image);
    }
    const caption = element('figcaption');
    const label = element('span', item.label || `Снимок ${index + 1}`, 'saved-image-caption');
    if (Number.isFinite(item.width) && Number.isFinite(item.height)) label.append(element('span', `${item.width} × ${item.height}`, 'saved-image-meta'));
    caption.append(label);
    if (Number.isFinite(item.score)) caption.append(element('strong', `${item.score}% ИИ`, 'saved-image-score'));
    figure.append(caption);
    imageRoot.append(figure);
  });
  find('#savedImagesSection').hidden = !imageRoot.childElementCount;

  const sectionRoot = find('#savedSections');
  sectionRoot.replaceChildren();
  (Array.isArray(data.sections) ? data.sections : []).forEach((section) => {
    if (!section || !Array.isArray(section.items) || !section.items.length) return;
    const block = element('section', null, 'saved-section');
    block.append(element('h2', section.title || 'Подробности проверки'));
    const facts = element('dl', null, 'saved-facts');
    section.items.forEach((item) => {
      if (!item || typeof item !== 'object') return;
      const row = element('div', null, 'saved-item');
      const content = element('dd', valueText(item.value));
      if (item.detail !== null && item.detail !== undefined && item.detail !== '') content.append(element('span', valueText(item.detail), 'saved-item-detail'));
      row.append(element('dt', item.label || ''), content);
      facts.append(row);
    });
    block.append(facts);
    sectionRoot.append(block);
  });
  find('#reportLoading').hidden = true;
  find('#reportError').hidden = true;
  find('#savedReport').hidden = false;
}

async function loadReport() {
  if (!token) {
    showFailure('Некорректная ссылка', 'Проверьте адрес сохранённого отчёта.', false);
    return;
  }
  if (loadingReport) loadingReport.abort();
  const controller = new AbortController();
  loadingReport = controller;
  find('#reportLoading').hidden = false;
  find('#reportError').hidden = true;
  find('#savedReport').hidden = true;
  const timeout = window.setTimeout(() => controller.abort(), 20000);
  try {
    const response = await fetch(`/api/reports/${encodeURIComponent(token)}`, { signal: controller.signal, cache: 'no-store' });
    if (loadingReport !== controller) return;
    if (response.status === 404 || response.status === 410) {
      showFailure('Отчёт недоступен', 'Срок хранения отчёта истёк или ссылка неверна. Попросите отправителя проверить ссылку или создать новый отчёт.', false);
      return;
    }
    if (!response.ok) throw new Error('Сервер не смог загрузить отчёт. Повторите попытку.');
    const data = await response.json();
    if (!data || data.version !== 1 || typeof data !== 'object' || Array.isArray(data)) throw new Error('Не удалось прочитать сохранённый отчёт.');
    if (loadingReport === controller) renderSavedReport(data);
  } catch (error) {
    if (loadingReport !== controller) return;
    showFailure('Не удалось открыть отчёт', error.name === 'AbortError' ? 'Сервер слишком долго отвечает. Повторите загрузку.' : error instanceof TypeError ? 'Проверьте соединение с сервером и повторите загрузку.' : error.message);
  } finally {
    window.clearTimeout(timeout);
    if (loadingReport === controller) loadingReport = null;
  }
}

async function copyCurrentUrl() {
  if (copyInProgress) return;
  copyInProgress = true;
  find('#copyReportLink').disabled = true;
  const url = `${window.location.origin}${window.location.pathname}`;
  let copied = false;
  let timeout;
  try {
    if (navigator.clipboard?.writeText) {
      await Promise.race([
        navigator.clipboard.writeText(url),
        new Promise((_, reject) => { timeout = window.setTimeout(() => reject(new Error('clipboard timeout')), 3000); }),
      ]);
      copied = true;
    }
  } catch (_) { /* The URL remains available for manual copying. */ }
  finally { window.clearTimeout(timeout); }
  const status = find('#savedLinkStatus');
  status.replaceChildren(element('span', copied ? 'Ссылка скопирована.' : 'Скопируйте ссылку из поля ниже.', 'share-feedback'));
  const link = element('a', url, 'share-url');
  link.href = url;
  status.append(link);
  status.hidden = false;
  if (!copied) {
    const field = element('input', null, 'share-url-field');
    field.type = 'url';
    field.readOnly = true;
    field.value = url;
    field.setAttribute('aria-label', 'Ссылка на этот отчёт');
    status.append(field);
    field.focus({ preventScroll: true });
    field.select();
  }
  copyInProgress = false;
  find('#copyReportLink').disabled = false;
}

find('#retryReport').addEventListener('click', loadReport);
find('#copyReportLink').addEventListener('click', copyCurrentUrl);
loadReport();
