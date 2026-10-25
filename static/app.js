const $ = (selector) => document.querySelector(selector);
const dropzone = $('#dropzone');
const fileInput = $('#fileInput');
const analyzeButton = $('#analyzeButton');
const preview = $('#preview');
const results = $('#results');
const remoteToggle = $('#useRemote');
const imagePickButtons = Array.from(document.querySelectorAll('[data-pick-image], #pickButton'));
const imageSearchButtons = Array.from(document.querySelectorAll('[data-image-search]'));
const imageSearchRoutes = { google: { name: 'Google' }, yandex: { name: 'Яндекс' } };
const asArray = (value) => Array.isArray(value) ? value : [];
const initialButtonText = analyzeButton.querySelector('span')?.textContent.trim() || 'Проверить изображение';
const supportedTypes = new Set(['image/png', 'image/x-png', 'image/jpeg', 'image/pjpeg', 'image/webp', 'image/gif', 'image/tiff', 'image/x-tiff', 'image/bmp', 'image/x-bmp', 'image/x-ms-bmp']);
const supportedExtension = /\.(png|jpe?g|webp|gif|tiff?|bmp)$/i;
let selectedFile = null;
let previewUrl = null;
let isAnalyzing = false;
let previewAvailable = false;
let configuredProviders = null;
let lastRemoteEnabled = remoteToggle.checked;
let stageTimer = null;
let dragDepth = 0;
let pendingImageSearch = null;
let uploadedSearchImage = null;
const resultReports = { single: null, series: null };
let pendingShare = null;
let pendingPdf = null;

function setText(selector, value) {
  const element = $(selector);
  if (element) element.textContent = value;
}

function shareStatus(kind) {
  return $(kind === 'series' ? '#seriesShareStatus' : '#shareStatus');
}

function refreshShareButtons() {
  document.querySelectorAll('[data-share-result]').forEach((button) => {
    const kind = button.dataset.shareResult;
    button.disabled = !resultReports[kind]?.reportId || Boolean(pendingShare) || Boolean(pendingPdf);
    button.setAttribute('aria-busy', String(pendingShare?.kind === kind));
    const label = button.querySelector('span');
    if (label) label.textContent = resultReports[kind]?.publicUrl ? 'Копировать ссылку' : 'Поделиться';
  });
  document.querySelectorAll('[data-download-pdf]').forEach((link) => {
    const kind = link.dataset.downloadPdf;
    const path = resultReports[kind]?.pdfPath;
    const disabled = !path || Boolean(pendingPdf) || Boolean(pendingShare);
    if (path) link.href = path;
    else link.removeAttribute('href');
    link.classList.toggle('is-disabled', disabled);
    link.setAttribute('aria-disabled', String(disabled));
    link.setAttribute('aria-busy', String(pendingPdf?.kind === kind));
    link.tabIndex = disabled ? -1 : 0;
  });
}

function reportApiPath(value, expectedPath) {
  if (typeof value !== 'string') return null;
  try {
    const url = new URL(value, window.location.origin);
    return url.origin === window.location.origin && url.pathname === expectedPath && !url.search && !url.hash ? url.pathname : null;
  } catch (_) { return null; }
}

function setResultReport(kind, data) {
  if (pendingShare?.kind === kind) {
    pendingShare.controller.abort();
    pendingShare = null;
  }
  if (pendingPdf?.kind === kind) {
    pendingPdf.controller.abort();
    pendingPdf = null;
  }
  const reportId = typeof data?.report_id === 'string' && /^[A-Za-z0-9_-]{1,200}$/.test(data.report_id) ? data.report_id : null;
  resultReports[kind] = data ? {
    reportId,
    pdfPath: reportId ? reportApiPath(data.pdf_path, `/api/report-drafts/${encodeURIComponent(reportId)}/pdf`) : null,
    publicUrl: null,
    expiresAt: null,
  } : null;
  const status = shareStatus(kind);
  if (status) {
    status.replaceChildren();
    status.hidden = true;
    status.classList.remove('is-error');
    if (data && !reportId) {
      status.textContent = typeof data.report_error === 'string' ? data.report_error : 'Повторите анализ, чтобы создать ссылку на отчёт и скачать PDF.';
      status.hidden = false;
    }
  }
  refreshShareButtons();
}

function setReportStatus(kind, message, isError = false) {
  const status = shareStatus(kind);
  status.textContent = message;
  status.hidden = !message;
  status.classList.toggle('is-error', isError);
}

function showPublishedReport(kind, report, feedback, manualCopy = false) {
  if (resultReports[kind] !== report || !report.publicUrl) return;
  const status = shareStatus(kind);
  status.replaceChildren();
  status.classList.remove('is-error');
  status.hidden = false;
  const message = document.createElement('span');
  message.className = 'share-feedback';
  message.textContent = feedback;
  const link = document.createElement('a');
  link.className = 'share-url';
  link.href = report.publicUrl;
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  link.textContent = report.publicUrl;
  status.append(message, link);
  if (manualCopy) {
    const field = document.createElement('input');
    field.className = 'share-url-field';
    field.type = 'url';
    field.readOnly = true;
    field.value = report.publicUrl;
    field.setAttribute('aria-label', 'Ссылка на сохранённый отчёт');
    status.append(field);
    field.focus({ preventScroll: true });
    field.select();
  }
  if (['localhost', '127.0.0.1', '[::1]'].includes(new URL(report.publicUrl).hostname)) {
    const note = document.createElement('span');
    note.className = 'share-help';
    note.textContent = 'Это локальный адрес: ссылка открывается только на устройстве, где запущен AntiSlop.';
    status.append(note);
  }
}

async function copyReportUrl(url) {
  if (!navigator.clipboard?.writeText) return false;
  let timeout;
  try {
    await Promise.race([
      navigator.clipboard.writeText(url),
      new Promise((_, reject) => { timeout = window.setTimeout(() => reject(new Error('clipboard timeout')), 3000); }),
    ]);
    return true;
  } catch (_) { return false; }
  finally { window.clearTimeout(timeout); }
}

async function shareResult(event) {
  if (!event.isTrusted || pendingShare) return;
  const kind = event.currentTarget.dataset.shareResult;
  const report = resultReports[kind];
  if (!report?.reportId) return;
  const request = { kind, report, controller: new AbortController() };
  pendingShare = request;
  refreshShareButtons();
  const timeout = window.setTimeout(() => request.controller.abort(), 25000);
  try {
    if (!report.publicUrl) {
      setReportStatus(kind, 'Создаём ссылку на сохранённый отчёт…');
      const response = await fetch('/api/reports', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ report_id: report.reportId }),
        signal: request.controller.signal,
      });
      let data = null;
      try { data = await response.json(); } catch (_) { /* A failed response may not contain JSON. */ }
      if (resultReports[kind] !== report) return;
      if (!response.ok) throw new Error(typeof data?.detail === 'string' ? data.detail : 'Не удалось сохранить отчёт. Повторите попытку.');
      if (typeof data?.path !== 'string' || !/^\/r\/[A-Za-z0-9_-]+$/.test(data.path)) throw new Error('Сервер не вернул ссылку на отчёт. Повторите анализ.');
      const url = new URL(data.url || data.path, window.location.origin);
      if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password || url.pathname !== data.path || url.search || url.hash) throw new Error('Сервер вернул некорректную ссылку на отчёт.');
      report.publicUrl = url.href;
      report.expiresAt = data.expires_at || null;
      const publicToken = data.path.slice('/r/'.length);
      const publicPdfPath = reportApiPath(data.pdf_path, `/api/reports/${encodeURIComponent(publicToken)}/pdf`);
      if (publicPdfPath) report.pdfPath = publicPdfPath;
    }
    window.clearTimeout(timeout);
    if (resultReports[kind] !== report) return;
    showPublishedReport(kind, report, 'Ссылка на отчёт создана.');
    const copied = await copyReportUrl(report.publicUrl);
    showPublishedReport(kind, report, copied ? 'Ссылка скопирована. Вставьте её в сообщение.' : 'Ссылка готова. Скопируйте её из поля ниже.', !copied);
  } catch (error) {
    if (resultReports[kind] !== report) return;
    setReportStatus(kind, error.name === 'AbortError' ? 'Сервер слишком долго создаёт ссылку. Повторите попытку.' : error.message || 'Не удалось создать ссылку на отчёт.', true);
  } finally {
    window.clearTimeout(timeout);
    if (pendingShare === request) pendingShare = null;
    refreshShareButtons();
  }
}

async function downloadReportPdf(event) {
  event.preventDefault();
  if (!event.isTrusted || pendingPdf) return;
  const kind = event.currentTarget.dataset.downloadPdf;
  const report = resultReports[kind];
  if (!report?.pdfPath) return;
  const request = { kind, report, controller: new AbortController() };
  pendingPdf = request;
  refreshShareButtons();
  if (report.publicUrl) showPublishedReport(kind, report, 'Готовим PDF…');
  else setReportStatus(kind, 'Готовим PDF…');
  const timeout = window.setTimeout(() => request.controller.abort(), 30000);
  try {
    const response = await fetch(report.pdfPath, { signal: request.controller.signal });
    if (!response.ok) {
      let data = null;
      try { data = await response.json(); } catch (_) { /* Keep a readable error for non-JSON responses. */ }
      throw new Error(typeof data?.detail === 'string' ? data.detail : 'Не удалось скачать PDF. Повторите анализ и попробуйте ещё раз.');
    }
    if (!(response.headers.get('content-type') || '').includes('application/pdf')) throw new Error('Сервер не вернул PDF. Повторите попытку.');
    const blob = await response.blob();
    if (resultReports[kind] !== report) return;
    if (!blob.size) throw new Error('Сервер вернул пустой PDF. Повторите попытку.');
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = kind === 'series' ? 'AntiSlop-series.pdf' : 'AntiSlop-report.pdf';
    link.hidden = true;
    document.body.append(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 60000);
    if (report.publicUrl) showPublishedReport(kind, report, 'PDF подготовлен для скачивания.');
    else setReportStatus(kind, 'PDF подготовлен для скачивания.');
  } catch (error) {
    if (resultReports[kind] !== report) return;
    const message = error.name === 'AbortError' ? 'Подготовка PDF заняла слишком много времени. Повторите попытку.' : error.message || 'Не удалось скачать PDF.';
    if (report.publicUrl) showPublishedReport(kind, report, message);
    else setReportStatus(kind, message, true);
  } finally {
    window.clearTimeout(timeout);
    if (pendingPdf === request) pendingPdf = null;
    refreshShareButtons();
  }
}

function updateManualSearchVisibility() {
  const hasFile = Boolean(selectedFile);
  const toolbar = $('#manualSearch');
  if (toolbar) toolbar.hidden = !hasFile;
  imageSearchButtons.forEach((button) => {
    const disabled = !hasFile || !Object.prototype.hasOwnProperty.call(imageSearchRoutes, button.dataset.imageSearch) || Boolean(pendingImageSearch);
    button.disabled = disabled;
    button.setAttribute('aria-disabled', String(disabled));
  });
}

function setImageSearchStatus(message, isError = false) {
  const status = $('#imageSearchStatus');
  if (!status) return;
  status.textContent = message;
  status.hidden = !message;
  status.classList.toggle('is-error', isError);
}

function searchSelectedImage(event) {
  event.preventDefault();
  if (!event.isTrusted) return;
  const file = selectedFile;
  if (!file) return;
  const engine = event.currentTarget.dataset.imageSearch;
  if (!Object.prototype.hasOwnProperty.call(imageSearchRoutes, engine)) return;
  searchInternetImage(file, event.currentTarget, engine);
}

function closePendingSearchPopup(request) {
  if (!request?.popup || request.navigated || request.popup.closed) return;
  try {
    if (request.popup.location.href === 'about:blank') request.popup.close();
  } catch (_) {
    // A tab the user has navigated elsewhere belongs to the user.
  }
}

function cancelImageSearch() {
  uploadedSearchImage = null;
  const request = pendingImageSearch;
  if (!request) return;
  pendingImageSearch = null;
  request.controller.abort();
  window.clearTimeout(request.timeout);
  window.clearTimeout(request.uploadTimeout);
  closePendingSearchPopup(request);
  request.button.classList.remove('is-searching');
  request.button.setAttribute('aria-busy', 'false');
}

function ensureImageSearchActive(request) {
  if (pendingImageSearch !== request || selectedFile !== request.file || request.controller.signal.aborted) throw new DOMException('Поиск отменён', 'AbortError');
  if (request.popup.closed) {
    request.controller.abort();
    throw new Error('Вкладка поиска закрыта. Нажмите кнопку поиска ещё раз.');
  }
  let reservedTab = false;
  try {
    reservedTab = request.popup.location.href === 'about:blank';
  } catch (_) {
    // A cross-origin tab has already been navigated by the user.
  }
  if (!reservedTab) {
    request.userNavigated = true;
    request.controller.abort();
    throw new Error('Вкладка поиска уже используется. Нажмите кнопку поиска, чтобы открыть новую.');
  }
}

async function prepareYandexImage(file, signal) {
  let source = preview;
  let temporaryImage = null;
  let temporaryUrl = null;
  let canvas = null;
  try {
    if (selectedFile !== file || !previewAvailable || !preview.complete || !preview.naturalWidth) {
      temporaryImage = new Image();
      temporaryUrl = URL.createObjectURL(file);
      source = temporaryImage;
      await new Promise((resolve, reject) => {
        const cleanup = () => {
          temporaryImage.onload = null;
          temporaryImage.onerror = null;
          signal.removeEventListener('abort', onAbort);
        };
        const onAbort = () => {
          cleanup();
          reject(new DOMException('Поиск отменён', 'AbortError'));
        };
        temporaryImage.onload = () => { cleanup(); resolve(); };
        temporaryImage.onerror = () => {
          cleanup();
          reject(new Error('Браузер не смог прочитать изображение для поиска. Выберите JPG, PNG или WebP.'));
        };
        signal.addEventListener('abort', onAbort, { once: true });
        if (signal.aborted) onAbort();
        else temporaryImage.src = temporaryUrl;
      });
    }
    if (signal.aborted) throw new DOMException('Поиск отменён', 'AbortError');
    const width = source.naturalWidth;
    const height = source.naturalHeight;
    if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) throw new Error('Не удалось определить размеры изображения. Выберите другой файл.');
    if (width * height > 40000000) throw new Error('Для поиска поддерживаются изображения до 40 мегапикселей. Выберите уменьшенную версию.');
    const scale = Math.min(1, 1280 / Math.max(width, height));
    canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.floor(width * scale));
    canvas.height = Math.max(1, Math.floor(height * scale));
    const context = canvas.getContext('2d', { alpha: false });
    if (!context) throw new Error('Браузер не смог подготовить изображение для поиска. Попробуйте другой браузер.');
    context.fillStyle = '#fff';
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.drawImage(source, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise((resolve, reject) => {
      const onAbort = () => reject(new DOMException('Поиск отменён', 'AbortError'));
      signal.addEventListener('abort', onAbort, { once: true });
      if (signal.aborted) {
        signal.removeEventListener('abort', onAbort);
        onAbort();
        return;
      }
      try {
        canvas.toBlob((result) => {
          signal.removeEventListener('abort', onAbort);
          if (result) resolve(result);
          else reject(new Error('Не удалось подготовить изображение. Повторите поиск.'));
        }, 'image/jpeg', 0.86);
      } catch (error) {
        signal.removeEventListener('abort', onAbort);
        reject(error);
      }
    });
    if (signal.aborted) throw new DOMException('Поиск отменён', 'AbortError');
    if (!blob.size || blob.size > 2 * 1024 * 1024 || blob.type !== 'image/jpeg') throw new Error('Не удалось подготовить компактное изображение для поиска. Выберите другой файл.');
    return blob;
  } finally {
    if (canvas) { canvas.width = 1; canvas.height = 1; }
    if (temporaryImage) temporaryImage.removeAttribute('src');
    if (temporaryUrl) URL.revokeObjectURL(temporaryUrl);
  }
}

function validateUploadedImageUrl(value) {
  if (typeof value !== 'string' || !value || value.length > 4096) throw new Error('Яндекс вернул некорректный адрес изображения. Повторите поиск.');
  let url;
  try { url = new URL(value); }
  catch (_) { throw new Error('Яндекс вернул некорректный адрес изображения. Повторите поиск.'); }
  if (url.protocol !== 'https:' || url.hostname !== 'avatars.mds.yandex.net' || url.username || url.password || url.port || !url.pathname.startsWith('/get-images-cbir/')) throw new Error('Яндекс вернул некорректный адрес изображения. Повторите поиск.');
  return url.href;
}

function uploadedImageDescriptor(data) {
  if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error('Яндекс вернул некорректный ответ. Повторите поиск.');
  if (data.type === 'captcha') {
    const value = data.captcha?.['captcha-page'];
    const url = typeof value === 'string' ? new URL(value) : null;
    const allowed = url && ['ya.ru', 'yandex.ru', 'yandex.com'].some((host) => url.hostname === host || url.hostname.endsWith(`.${host}`));
    if (!allowed || url.protocol !== 'https:' || url.username || url.password || url.port) throw new Error('Яндекс запросил подтверждение. Попробуйте повторить поиск позже.');
    return { captchaUrl: url.href };
  }
  if (!Number.isSafeInteger(data.image_shard) || data.image_shard < 0 || typeof data.image_id !== 'string' || !data.image_id.trim() || data.image_id.length > 512 || typeof data.url !== 'string' || data.url.length > 4096) throw new Error('Яндекс вернул некорректный ответ. Повторите поиск.');
  const previewImageUrl = validateUploadedImageUrl(data.url);
  const originalPath = data.sizes?.orig?.path;
  const imageUrl = validateUploadedImageUrl(typeof originalPath === 'string' && originalPath ? originalPath : previewImageUrl.replace(/\/preview$/, '/orig'));
  if (!new URL(imageUrl).pathname.endsWith('/orig')) throw new Error('Яндекс не вернул адрес изображения для поиска. Повторите попытку.');
  return { imageUrl, cbirId: `${data.image_shard}/${data.image_id}` };
}

function internetSearchDestination(descriptor, engine) {
  if (descriptor.captchaUrl) return descriptor.captchaUrl;
  if (engine === 'google') {
    const url = new URL('https://lens.google.com/v3/upload');
    url.search = new URLSearchParams({ url: descriptor.imageUrl, ep: 'gsbubu', st: String(Date.now()), authuser: '0', hl: 'ru', vpw: String(window.innerWidth), vph: String(window.innerHeight) }).toString();
    return url.href;
  }
  const url = new URL('https://ya.ru/images/search');
  url.search = new URLSearchParams({ rpt: 'imageview', url: descriptor.imageUrl, cbir_id: descriptor.cbirId, tabInt: '1' }).toString();
  return url.href;
}

async function searchInternetImage(file, button, engine) {
  if (pendingImageSearch) return;
  let request = null;
  try {
    const popup = window.open('about:blank', '_blank');
    if (!popup) throw new Error('Браузер заблокировал новую вкладку. Разрешите всплывающие окна для AntiSlop и повторите поиск.');
    try { popup.opener = null; }
    catch (_) {
      popup.close();
      throw new Error('Не удалось безопасно открыть вкладку поиска. Обновите браузер и повторите попытку.');
    }
    request = { file, button, popup, controller: new AbortController(), timeout: null, uploadTimeout: null, timedOut: false, navigated: false, userNavigated: false };
    pendingImageSearch = request;
    request.timeout = window.setTimeout(() => { request.timedOut = true; request.controller.abort(); }, 60000);
    button.classList.add('is-searching');
    button.setAttribute('aria-busy', 'true');
    updateManualSearchVisibility();
    const loadingDocument = popup.document;
    loadingDocument.documentElement.lang = 'ru';
    loadingDocument.title = `Поиск — ${imageSearchRoutes[engine].name}`;
    const heading = loadingDocument.createElement('h1');
    heading.textContent = 'Открываем поиск по изображению';
    const explanation = loadingDocument.createElement('p');
    explanation.textContent = 'Подготавливаем выбранную картинку для поиска…';
    loadingDocument.body.replaceChildren(heading, explanation);
    loadingDocument.documentElement.style.background = '#080c10';
    loadingDocument.body.style.color = '#f0f4f5';
    loadingDocument.body.style.fontFamily = 'system-ui, sans-serif';
    loadingDocument.body.style.lineHeight = '1.6';
    loadingDocument.body.style.margin = '48px';
    let descriptor = uploadedSearchImage?.file === file && uploadedSearchImage.expiresAt > Date.now() ? uploadedSearchImage.descriptor : null;
    if (!descriptor) {
      setImageSearchStatus('Готовим изображение для поиска…');
      const blob = await prepareYandexImage(file, request.controller.signal);
      ensureImageSearchActive(request);
      setImageSearchStatus('Отправляем изображение в Яндекс…');
      request.uploadTimeout = window.setTimeout(() => { request.timedOut = true; request.controller.abort(); }, 25000);
      const response = await fetch('https://ya.ru/images-apphost/image-download?cbird=111&images_avatars_size=preview&images_avatars_namespace=images-cbir', {
        method: 'POST',
        headers: { 'Content-Type': 'image/jpeg' },
        body: blob,
        credentials: 'omit',
        mode: 'cors',
        redirect: 'error',
        referrerPolicy: 'no-referrer',
        signal: request.controller.signal,
      });
      ensureImageSearchActive(request);
      if (!response.ok) throw new Error(response.status === 429 ? 'Яндекс ограничил число запросов. Повторите поиск позже.' : 'Яндекс не смог принять изображение. Повторите поиск.');
      const data = await response.json();
      ensureImageSearchActive(request);
      descriptor = uploadedImageDescriptor(data);
      if (!descriptor.captchaUrl) uploadedSearchImage = { file, descriptor, expiresAt: Date.now() + 5 * 60 * 1000 };
    }
    ensureImageSearchActive(request);
    const destination = internetSearchDestination(descriptor, engine);
    setImageSearchStatus(descriptor.captchaUrl ? 'Яндекс запросил проверку. Пройдите её в новой вкладке и повторите поиск.' : '');
    request.popup.location.replace(destination);
    request.navigated = true;
  } catch (error) {
    if (request && (pendingImageSearch !== request || selectedFile !== file)) return;
    const popupWasClosed = Boolean(request?.popup.closed);
    if (request) closePendingSearchPopup(request);
    const message = popupWasClosed
      ? 'Вкладка поиска закрыта. Нажмите кнопку поиска ещё раз.'
      : request?.timedOut
        ? 'Яндекс слишком долго не отвечает. Повторите поиск.'
        : request?.userNavigated
          ? 'Вкладка поиска уже используется. Нажмите кнопку поиска, чтобы открыть новую.'
          : error instanceof TypeError || error instanceof SyntaxError
            ? 'Не удалось получить ответ Яндекса. Проверьте соединение и повторите поиск.'
            : error instanceof DOMException
              ? 'Браузер не смог подготовить или отправить изображение. Повторите поиск.'
              : error.message || 'Не удалось открыть поиск по изображению. Повторите попытку.';
    setImageSearchStatus(message, true);
  } finally {
    if (request) {
      window.clearTimeout(request.timeout);
      window.clearTimeout(request.uploadTimeout);
      if (pendingImageSearch === request) {
        pendingImageSearch = null;
        button.classList.remove('is-searching');
        button.setAttribute('aria-busy', 'false');
        updateManualSearchVisibility();
      }
    }
  }
}

function setAnalyzeButtonText(value) {
  const label = analyzeButton.querySelector('span');
  if (label) label.textContent = value;
  else analyzeButton.textContent = value;
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes)) return '—';
  if (bytes < 1024) return `${bytes} Б`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} КБ`;
  return `${(bytes / 1024 / 1024).toFixed(1)} МБ`;
}

function plural(count, one, few, many) {
  const lastTwo = count % 100;
  if (lastTwo >= 11 && lastTwo <= 14) return many;
  return count % 10 === 1 ? one : count % 10 >= 2 && count % 10 <= 4 ? few : many;
}

function motionBehavior() {
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth';
}

function setState(state) {
  document.body.dataset.state = state;
  const labels = { idle: 'Ожидает изображение', ready: 'Готово к проверке', analyzing: 'Идёт проверка', complete: 'Проверка завершена' };
  const badge = $('#analysisState');
  if (badge) {
    const dot = badge.querySelector('.status-dot');
    badge.replaceChildren();
    if (dot) badge.append(dot, document.createTextNode(` ${labels[state]}`));
    else badge.textContent = labels[state];
  }
  const activeIndex = state === 'idle' ? 0 : state === 'complete' ? 2 : 1;
  ['#stepUpload', '#stepAnalyze', '#stepResult'].forEach((selector, index) => {
    const step = $(selector);
    if (!step) return;
    step.classList.toggle('is-active', index === activeIndex);
    step.classList.toggle('is-complete', index < activeIndex);
    if (index === activeIndex) step.setAttribute('aria-current', 'step');
    else step.removeAttribute('aria-current');
  });
}

function showError(message) {
  const error = $('#uploadError');
  if (error) {
    error.textContent = message;
    error.hidden = false;
    error.tabIndex = -1;
    window.requestAnimationFrame(() => {
      if (error.hidden) return;
      error.focus({ preventScroll: true });
      error.scrollIntoView({ block: 'center', behavior: motionBehavior() });
    });
  }
  fileInput.setAttribute('aria-invalid', 'true');
}

function clearError() {
  const error = $('#uploadError');
  if (error) {
    error.hidden = true;
    error.textContent = '';
  }
  fileInput.removeAttribute('aria-invalid');
}

function setBusy(busy) {
  isAnalyzing = busy;
  [fileInput, ...imagePickButtons, $('#removeButton'), $('#newCheck'), remoteToggle].forEach((element) => {
    if (element) element.disabled = busy;
  });
  imagePickButtons.forEach((button) => button.setAttribute('aria-disabled', String(busy)));
  analyzeButton.disabled = busy || !selectedFile;
  analyzeButton.classList.toggle('is-busy', busy);
  analyzeButton.setAttribute('aria-busy', String(busy));
  dropzone.setAttribute('aria-disabled', String(busy));
  dropzone.setAttribute('aria-busy', String(busy));
  dropzone.tabIndex = busy ? -1 : 0;
  dropzone.classList.toggle('scanning', busy);
  dropzone.classList.remove('dragging');
  results.setAttribute('aria-busy', String(busy));
  const status = $('#analyzeStatus');
  if (status) status.hidden = !busy;
  dragDepth = 0;
}

function clearReport() {
  setResultReport('single', null);
  results.hidden = true;
  $('#visualAnalysis').hidden = true;
  $('#reportDetails').open = false;
  ['#breakdownGrid', '#findings', '#fileFacts', '#metadata', '#apiResults', '#forensicSignals', '#forensicMaps', '#c2paResult', '#exifChecks', '#hashes'].forEach((selector) => {
    $(selector)?.replaceChildren();
  });
  ['#resourceBudget', '#apiConsensus', '#scoreBasis', '#resultSources', '#resultHeadline', '#resultSummary', '#resultLabel'].forEach((selector) => setText(selector, ''));
  setText('#probabilityValue', '—');
  $('#probabilityFill').style.width = '0%';
  if ($('#probabilityMarker')) $('#probabilityMarker').hidden = true;
  $('#scoreCard').classList.remove('high', 'mid', 'low', 'preview-unavailable');
  $('#scoreCard').classList.add('unknown');
  $('#resultPreview').removeAttribute('src');
}

function releasePreview() {
  const oldUrl = previewUrl;
  previewUrl = null;
  preview.onload = null;
  preview.onerror = null;
  preview.removeAttribute('src');
  $('#resultPreview').removeAttribute('src');
  if (oldUrl) URL.revokeObjectURL(oldUrl);
}

function reset() {
  if (isAnalyzing) return;
  cancelImageSearch();
  releasePreview();
  selectedFile = null;
  setImageSearchStatus('');
  updateManualSearchVisibility();
  previewAvailable = false;
  fileInput.value = '';
  $('#previewState').hidden = true;
  $('#previewState').classList.remove('preview-unavailable');
  const fallback = $('#previewState').querySelector('.preview-fallback');
  if (fallback) fallback.hidden = true;
  $('#emptyState').hidden = false;
  clearReport();
  clearError();
  setBusy(false);
  setAnalyzeButtonText(initialButtonText);
  setText('#uploadHint', 'PNG, JPG, WebP, GIF, TIFF или BMP · до 20 МБ');
  setText('#fileName', '');
  setText('#fileDimensions', '');
  setState('idle');
}

function isSupportedFile(file) {
  if (!file) return false;
  const type = (file.type || '').toLowerCase();
  return supportedTypes.has(type) || ((!type || type === 'application/octet-stream') && supportedExtension.test(file.name || ''));
}

function chooseFile(file) {
  if (isAnalyzing || !file) return;
  fileInput.value = '';
  if (!isSupportedFile(file)) {
    showError('Этот формат не поддерживается. Выберите PNG, JPG, WebP, GIF, TIFF или BMP.');
    return;
  }
  if (!file.size) {
    showError('Файл пустой. Выберите другое изображение.');
    return;
  }
  if (file.size > 20 * 1024 * 1024) {
    showError('Файл слишком большой. Максимальный размер — 20 МБ.');
    return;
  }
  cancelImageSearch();
  releasePreview();
  clearReport();
  clearError();
  selectedFile = file;
  setImageSearchStatus('');
  updateManualSearchVisibility();
  previewAvailable = false;
  const objectUrl = URL.createObjectURL(file);
  previewUrl = objectUrl;
  const previewState = $('#previewState');
  previewState.hidden = false;
  previewState.classList.remove('preview-unavailable');
  const fallback = previewState.querySelector('.preview-fallback');
  if (fallback) fallback.hidden = true;
  preview.hidden = false;
  $('#emptyState').hidden = true;
  setText('#fileBadge', `${file.name} · ${formatBytes(file.size)}`);
  setText('#fileName', file.name);
  setText('#fileDimensions', formatBytes(file.size));
  setText('#uploadHint', 'Изображение готово к проверке.');
  preview.alt = `Предпросмотр: ${file.name}`;
  preview.onload = () => {
    if (previewUrl !== objectUrl) return;
    previewAvailable = true;
    setText('#fileDimensions', `${preview.naturalWidth} × ${preview.naturalHeight} · ${formatBytes(file.size)}`);
  };
  preview.onerror = () => {
    if (previewUrl !== objectUrl) return;
    previewAvailable = false;
    preview.hidden = true;
    previewState.classList.add('preview-unavailable');
    setText('#uploadHint', 'Браузер не показывает превью этого файла. Его можно проверить.');
    let fallback = previewState.querySelector('.preview-fallback');
    if (!fallback) {
      fallback = document.createElement('p');
      fallback.className = 'preview-fallback';
      fallback.textContent = 'Файл готов к проверке. Предпросмотр недоступен.';
      previewState.append(fallback);
    }
    fallback.hidden = false;
  };
  preview.src = objectUrl;
  setBusy(false);
  setAnalyzeButtonText(initialButtonText);
  setState('ready');
  window.requestAnimationFrame(() => {
    if (selectedFile !== file || isAnalyzing) return;
    const checker = $('#checker') || dropzone;
    const smallScreen = window.matchMedia('(max-width: 760px)').matches;
    checker.scrollIntoView({ block: smallScreen ? 'end' : 'start', behavior: motionBehavior() });
  });
}

function updateRemoteHint() {
  const hint = !remoteToggle.checked
    ? 'Только локальная проверка. Изображение не отправляется внешним сервисам.'
    : configuredProviders === null
      ? 'Будут использованы только подключённые на сервере сервисы. Изображение будет передано им для проверки.'
      : configuredProviders === 0
        ? 'Внешние сервисы не подключены. Доступна локальная проверка.'
        : `Подключено ${configuredProviders} ${plural(configuredProviders, 'сервис', 'сервиса', 'сервисов')}. Изображение будет отправлено им для проверки.`;
  setText('#remoteHint', hint);
}

function renderProviderStatus(providers) {
  configuredProviders = asArray(providers).filter((provider) => provider.configured && !provider.unavailable).length;
  const root = $('#providerStatus');
  if (root) root.textContent = configuredProviders ? `Подключено сервисов: ${configuredProviders}` : 'Внешние сервисы не подключены';
  updateRemoteHint();
}

function renderOverall(data) {
  const probability = data.generation_probability || {};
  const value = probability.percent;
  const numeric = Number.isFinite(value);
  const tone = !numeric ? 'unknown' : value >= 65 ? 'high' : value <= 35 ? 'low' : 'mid';
  const card = $('#scoreCard');
  card.classList.remove('unknown', 'high', 'low', 'mid');
  card.classList.add(tone);
  card.classList.toggle('preview-unavailable', !previewAvailable);
  const resultImage = $('#resultPreview');
  resultImage.hidden = !previewAvailable;
  if (previewAvailable && previewUrl) resultImage.src = previewUrl;
  else resultImage.removeAttribute('src');
  $('#probabilityValue').textContent = numeric ? `${value}%` : '—';
  $('#probabilityFill').style.width = numeric ? `${Math.max(0, Math.min(100, value))}%` : '0%';
  const marker = $('#probabilityMarker');
  if (marker) {
    marker.hidden = !numeric;
    marker.style.left = numeric ? `${Math.max(0, Math.min(100, value))}%` : '0%';
  }
  setText('#resultEyebrow', 'Результат анализа');
  $('#resultLabel').textContent = !numeric ? 'Недостаточно данных' : value >= 65 ? 'Высокая вероятность ИИ' : value <= 35 ? 'Низкая вероятность ИИ' : 'Неоднозначный результат';
  const disagreement = probability.label === 'Низкая согласованность';
  $('#resultHeadline').textContent = !numeric
    ? 'Не удалось получить оценку'
    : disagreement
      ? 'Источники расходятся во мнениях'
      : value >= 65
        ? 'Есть признаки генерации ИИ'
        : value <= 35
          ? 'Мало признаков генерации ИИ'
          : 'Однозначного ответа пока нет';
  $('#resultSummary').textContent = !numeric
    ? 'Модели не ответили. Попробуйте проверить изображение ещё раз.'
    : disagreement
      ? 'Источники расходятся — посмотрите разбивку ниже.'
      : value >= 65
        ? 'Найдены признаки генерации или обработки изображения нейросетью.'
        : value <= 35
          ? 'Выраженных признаков генерации не обнаружено.'
          : 'Часть сигналов указывает на ИИ, часть — нет.';
  const items = asArray(data.api_results);
  const modelAnswers = items.filter((item) => item.status === 'ok' && !item.provenance && item.verdict !== 'unknown' && Number.isFinite(item.score)).length;
  const provenanceChecks = items.filter((item) => item.status === 'ok' && item.provenance).length;
  const sources = [modelAnswers ? `${plural(modelAnswers, 'Ответила', 'Ответили', 'Ответили')} ${modelAnswers} ${plural(modelAnswers, 'модель', 'модели', 'моделей')}` : 'Нет ответивших моделей'];
  if (provenanceChecks) sources.push(provenanceChecks === 1 ? 'Проверка происхождения выполнена' : `Проверок происхождения выполнено: ${provenanceChecks}`);
  if (Number.isFinite(probability.sources) && probability.sources > modelAnswers) sources.push('учтён сигнал происхождения');
  if (numeric && probability.label) sources.push(probability.label === 'Один модельный сигнал' && modelAnswers === 0 ? 'Один сигнал происхождения' : probability.label);
  setText('#resultSources', sources.join(' · '));
  $('#scoreBasis').textContent = probability.detail || 'Источники оценки отсутствуют.';
}

function missingMetricState(item, data) {
  const providerResults = asArray(data.api_results);
  if (item.status === 'error') return { label: 'Ошибка', note: item.detail || 'Источник не смог завершить проверку.' };
  if (item.id === 'model') {
    const model = providerResults.find((entry) => entry.id === 'local_frontier') || data.local_model;
    return { label: model?.status === 'error' ? 'Нет ответа' : 'Недоступна', note: model?.status === 'error' ? 'Локальная модель не смогла завершить проверку.' : 'Локальная модель недоступна для этой проверки.' };
  }
  if (item.id === 'semantic' || item.id === 'external') {
    if (!lastRemoteEnabled) return { label: item.id === 'external' ? 'Отключены' : 'Отключён', note: 'Внешний анализ отключён для этой проверки.' };
    const providers = providerResults.filter((entry) => item.id === 'semantic' ? entry.id === 'openai_vision' : !entry.provenance && entry.id !== 'synthid' && entry.id !== 'local_frontier' && entry.id !== 'openai_vision');
    if (providers.some((entry) => entry.status === 'error')) return { label: 'Нет ответа', note: 'Сервис не смог завершить проверку. Подробности — в отчёте.' };
    if (providers.some((entry) => entry.status === 'ok')) return { label: 'Нет оценки', note: 'Сервис ответил, но не смог определить вероятность.' };
    return { label: item.id === 'external' ? 'Не подключены' : 'Не подключён', note: item.id === 'external' ? 'Специализированные детекторы не подключены на сервере.' : 'Сервис смыслового анализа не подключён на сервере.' };
  }
  return { label: 'Нет данных', note: item.detail || 'Этот сигнал не удалось рассчитать.' };
}

function renderBreakdown(data) {
  const root = $('#breakdownGrid');
  const titles = { model: 'Визуальная модель', semantic: 'Смысловой анализ', external: 'Внешние детекторы', provenance: 'Происхождение', pixels: 'Пиксельный анализ' };
  const notes = { model: 'Поиск визуальных признаков генерации.', semantic: 'Проверка анатомии, деталей и логики сцены.', external: 'Оценки подключённых детекторов.', provenance: 'Цифровые подписи, метаданные и водяные знаки.', pixels: 'Шум, сжатие и повторяющиеся структуры.' };
  root.replaceChildren();
  asArray(data.probability_breakdown).forEach((item, index) => {
    const numeric = Number.isFinite(item.percent);
    const tone = !numeric ? 'missing' : item.percent >= 65 ? 'high' : item.percent >= 36 ? 'mid' : 'low';
    const missing = numeric ? null : missingMetricState(item, data);
    const card = document.createElement('div');
    card.className = `metric ${tone}`;
    card.innerHTML = '<span class="metric-index" aria-hidden="true"></span><div class="metric-main"><span class="metric-title"></span><strong></strong></div><p class="metric-note"></p><div class="metric-bar" aria-hidden="true"><span></span></div>';
    card.querySelector('.metric-index').textContent = String(index + 1).padStart(2, '0');
    card.querySelector('.metric-title').textContent = titles[item.id] || item.title || 'Источник сигнала';
    const number = card.querySelector('strong');
    number.textContent = numeric ? `${item.percent}%` : missing.label;
    number.classList.toggle('missing', !numeric);
    const note = numeric
      ? item.id === 'provenance' && item.percent === 0 ? 'Метки генерации не найдены.' : notes[item.id] || item.detail || ''
      : missing.note;
    card.querySelector('.metric-note').textContent = note;
    card.querySelector('.metric-bar span').style.width = numeric ? `${Math.max(0, Math.min(100, item.percent))}%` : '0%';
    root.append(card);
  });
}

function renderFileReport(data) {
  const findings = $('#findings');
  findings.replaceChildren();
  const findingItems = asArray(data.findings);
  if (!findingItems.length) {
    findings.innerHTML = '<p class="empty-copy">Сигнатуры генераторов в метаданных не найдены.</p>';
  }
  findingItems.forEach((item) => {
    const row = document.createElement('div');
    row.className = `finding ${item.level || ''}`;
    row.innerHTML = '<span class="finding-dot"></span><div><b></b><p></p></div>';
    row.querySelector('b').textContent = item.title || '';
    row.querySelector('p').textContent = item.detail || '';
    findings.append(row);
  });
  const dimensions = Number.isFinite(data.width) && Number.isFinite(data.height) ? `${data.width} × ${data.height}` : '—';
  const facts = [['Формат', data.format || '—'], ['Разрешение', dimensions], ['Размер файла', formatBytes(data.size_bytes)], ['Цветовой режим', data.mode || '—']];
  const factsRoot = $('#fileFacts');
  factsRoot.replaceChildren();
  facts.forEach(([key, value]) => {
    const fact = document.createElement('div');
    fact.className = 'fact';
    fact.innerHTML = '<span></span><b></b>';
    fact.querySelector('span').textContent = key;
    fact.querySelector('b').textContent = value;
    factsRoot.append(fact);
  });
  const metadata = $('#metadata');
  metadata.replaceChildren();
  const metadataItems = asArray(data.metadata);
  if (!metadataItems.length) metadata.textContent = 'Метаданные отсутствуют или были очищены.';
  metadataItems.forEach((item) => {
    const row = document.createElement('div');
    row.textContent = `${item.key}: ${item.value}`;
    metadata.append(row);
  });
}

function renderApiResults(data) {
  const root = $('#apiResults');
  const items = asArray(data.api_results);
  root.replaceChildren();
  const checked = Number.isFinite(data.api_summary?.checked) ? data.api_summary.checked : items.filter((item) => item.status === 'ok').length;
  $('#apiConsensus').textContent = checked ? `${checked} ${plural(checked, 'источник ответил', 'источника ответили', 'источников ответили')}` : 'Ответов источников нет';
  if (!items.length) {
    const empty = document.createElement('p');
    empty.className = 'empty-copy';
    empty.textContent = lastRemoteEnabled ? 'Модельные результаты недоступны.' : 'Внешний анализ отключён для этой проверки.';
    root.append(empty);
  }
  items.forEach((item) => {
    const card = document.createElement('article');
    card.className = `api-result ${item.status || 'unknown'} ${item.verdict || ''}`;
    if (item.provenance) card.classList.add('provenance');
    const states = { error: 'Ошибка проверки', unavailable: 'Недоступен', not_configured: 'Не подключён', disabled: 'Отключён', unsupported: 'Формат не поддержан' };
    const label = item.status === 'ok'
      ? item.provenance ? item.verdict === 'ai' ? 'Найдено' : 'Не найдено' : item.verdict === 'ai' ? 'Вероятно ИИ' : item.verdict === 'human' ? 'Ниже порога' : 'Неопределённо'
      : states[item.status] || 'Нет ответа';
    card.innerHTML = '<div class="api-result-top"><h4></h4><span class="status"></span></div><div class="api-score"></div><p></p>';
    card.querySelector('h4').textContent = item.name || 'Источник сигнала';
    card.querySelector('.status').textContent = label;
    card.querySelector('.api-score').textContent = item.provenance && item.status === 'ok'
      ? item.verdict === 'ai' ? 'Признак найден' : 'Не обнаружено'
      : item.status === 'ok' && Number.isFinite(item.score) ? `${Math.round(item.score * 100)}%` : '—';
    card.querySelector('p').textContent = item.status === 'not_configured' ? 'Этот сервис не подключён на сервере.' : item.detail || '';
    asArray(item.checks).forEach((check) => {
      const checkRoot = document.createElement('div');
      checkRoot.className = 'provenance-check';
      const type = check.type === 'synthid' ? 'SynthID' : check.type === 'c2pa' ? 'C2PA' : check.type || 'Происхождение';
      const outcome = check.outcome === 'detected' ? 'Признак найден' : check.outcome === 'not_detected' ? 'Не обнаружено' : 'Нет результата';
      appendDetail(checkRoot, type, outcome);
      if (check.validation_state) {
        const validationStates = { trusted: 'Доверие подтверждено', valid: 'Подпись действительна', invalid: 'Некорректная подпись', not_present: 'Манифест отсутствует' };
        appendDetail(checkRoot, 'Проверка подписи', validationStates[check.validation_state] || check.validation_state);
      }
      if (check.issuer) appendDetail(checkRoot, 'Издатель', check.issuer);
      if (check.model) appendDetail(checkRoot, 'Модель', check.model);
      if (check.generated_at) appendDetail(checkRoot, 'Дата генерации', check.generated_at);
      card.append(checkRoot);
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
  row.append(name, document.createTextNode(value == null ? '—' : String(value)));
  root.append(row);
}

function renderVision(data) {
  const result = asArray(data.api_results).find((item) => item.id === 'openai_vision' && item.status === 'ok' && item.analysis);
  const root = $('#visualAnalysis');
  root.hidden = !result;
  if (!result) return;
  const analysis = result.analysis;
  $('#visionExplanation').textContent = analysis.explanation || '';
  $('#visionJson').textContent = JSON.stringify(analysis, null, 2);
  const image = $('#visionPreview');
  $('#visionImageFrame').hidden = !(previewAvailable && previewUrl);
  image.onload = () => {
    $('#visionImageFrame').style.width = `${Math.min(640, 480 * image.naturalWidth / image.naturalHeight)}px`;
  };
  if (previewAvailable && previewUrl) image.src = previewUrl;
  const regions = $('#visionRegions');
  const signals = $('#visionSignals');
  regions.replaceChildren();
  signals.replaceChildren();
  const select = (index) => {
    root.querySelectorAll('[data-signal]').forEach((element) => {
      const active = element.dataset.signal === String(index);
      element.classList.toggle('active', active);
      if (element.tagName === 'BUTTON') element.setAttribute('aria-pressed', String(active));
    });
  };
  const strength = { weak: 'слабый', moderate: 'средний', strong: 'сильный' };
  asArray(analysis.signals).forEach((signal, index) => {
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
      const { x1, y1, x2, y2 } = signal.region;
      Object.assign(box.style, { left: `${x1 * 100}%`, top: `${y1 * 100}%`, width: `${(x2 - x1) * 100}%`, height: `${(y2 - y1) * 100}%` });
      box.addEventListener('click', () => select(index));
      regions.append(box);
    }
  });
  if (!asArray(analysis.signals).length) signals.textContent = 'Явных визуальных признаков не найдено. Это не подтверждает подлинность.';
  const limitations = $('#visionLimitations');
  limitations.replaceChildren();
  asArray(analysis.limitations).forEach((text) => {
    const item = document.createElement('li');
    item.textContent = text;
    limitations.append(item);
  });
}

function renderForensics(data) {
  ['#forensicSignals', '#forensicMaps', '#c2paResult', '#exifChecks', '#hashes'].forEach((selector) => $(selector).replaceChildren());
  setText('#resourceBudget', '');
  const report = data.forensics;
  if (!report) {
    setText('#forensicSignals', 'Пиксельный анализ недоступен для этой проверки.');
    setText('#c2paResult', 'Данные о происхождении недоступны.');
    return;
  }
  const budget = report.resource_budget || {};
  const ml = data.local_model;
  const budgetParts = [];
  if (Number.isFinite(budget.max_analysis_edge)) budgetParts.push(`Изображение ≤ ${budget.max_analysis_edge}px`);
  if (Number.isFinite(budget.max_fft_edge)) budgetParts.push(`FFT ≤ ${budget.max_fft_edge}px`);
  if (ml?.status === 'ok' && Number.isFinite(ml.peak_rss_mb)) budgetParts.push(`Модель: ${ml.peak_rss_mb} МБ памяти`);
  $('#resourceBudget').textContent = budgetParts.join(' · ');
  const signals = $('#forensicSignals');
  asArray(report.signals).forEach((signal) => {
    const card = document.createElement('article');
    card.className = `forensic-signal ${signal.status || ''}`;
    card.innerHTML = '<div class="forensic-signal-top"><b></b><span class="lamp"></span></div><div class="forensic-value"></div><small></small><p></p>';
    card.querySelector('b').textContent = signal.name || '';
    card.querySelector('.forensic-value').textContent = signal.value ?? '—';
    card.querySelector('small').textContent = signal.unit || '';
    card.querySelector('p').textContent = signal.detail || '';
    signals.append(card);
  });
  const maps = $('#forensicMaps');
  asArray(report.maps).filter((map) => typeof map.image === 'string' && map.image.startsWith('data:image/')).forEach((map) => {
    const card = document.createElement('figure');
    card.className = 'forensic-map';
    const image = document.createElement('img');
    image.src = map.image;
    image.alt = `Диагностическая карта: ${map.name || ''}`;
    image.loading = 'lazy';
    const label = document.createElement('figcaption');
    label.textContent = map.name || '';
    card.append(image, label);
    maps.append(card);
  });
  const c2pa = $('#c2paResult');
  const credentials = report.c2pa || {};
  const c2paStates = { valid: 'Подпись подтверждена', invalid: 'Некорректная подпись', untrusted: 'Доверие не подтверждено', present: 'Манифест найден', none: 'Манифест не найден', unavailable: 'Проверка недоступна', error: 'Ошибка проверки' };
  const badge = document.createElement('span');
  badge.className = `c2pa-badge ${credentials.status || 'unavailable'}`;
  badge.textContent = c2paStates[credentials.status] || 'Нет данных';
  c2pa.append(badge);
  if (credentials.detail) appendDetail(c2pa, 'Результат', credentials.detail);
  if (credentials.generator) appendDetail(c2pa, 'Приложение-источник', credentials.generator);
  const exif = $('#exifChecks');
  asArray(report.exif_checks).forEach((check) => appendDetail(exif, check.name, check.detail));
  if (!exif.childElementCount) exif.textContent = 'Проверяемые EXIF-данные отсутствуют.';
  const hashes = $('#hashes');
  Object.entries(report.hashes || {}).forEach(([name, value]) => appendDetail(hashes, name, value));
}

function renderResult(data) {
  renderOverall(data);
  renderBreakdown(data);
  renderFileReport(data);
  renderApiResults(data);
  renderVision(data);
  renderForensics(data);
  setResultReport('single', data);
  $('#reportDetails').open = false;
  results.hidden = false;
  setState('complete');
  results.focus({ preventScroll: true });
  results.scrollIntoView({ behavior: motionBehavior(), block: 'start' });
}

function startStageMessages(remote) {
  const stages = remote && configuredProviders !== 0
    ? ['Изучаем изображение и его метаданные…', 'Проверяем локальные и подключённые источники…', 'Ожидаем результаты анализа…']
    : ['Изучаем изображение и его метаданные…', 'Проверяем локальную модель и пиксельные сигналы…', 'Ожидаем результаты локального анализа…'];
  let stage = 0;
  setText('#analysisStage', stages[stage]);
  stageTimer = window.setInterval(() => {
    stage = Math.min(stage + 1, stages.length - 1);
    setText('#analysisStage', stages[stage]);
  }, 4500);
}

async function analyze() {
  if (!selectedFile || isAnalyzing) return;
  const file = selectedFile;
  lastRemoteEnabled = remoteToggle.checked;
  clearError();
  clearReport();
  setBusy(true);
  setState('analyzing');
  setAnalyzeButtonText('Проверяем изображение…');
  setText('#uploadHint', 'Проверка выполняется. Это может занять до минуты.');
  startStageMessages(lastRemoteEnabled);
  let completed = false;
  const controller = new AbortController();
  const requestTimeout = window.setTimeout(() => controller.abort(), 120000);
  try {
    const response = await fetch(`/api/analyze?filename=${encodeURIComponent(file.name)}&remote=${lastRemoteEnabled}`, {
      method: 'POST',
      headers: { 'Content-Type': file.type || 'application/octet-stream' },
      body: file,
      signal: controller.signal,
    });
    const data = await response.json().catch(() => null);
    if (!response.ok) {
      const detail = typeof data?.detail === 'string' ? data.detail : response.status === 413 ? 'Максимальный размер файла — 20 МБ.' : 'Сервер не смог проверить изображение. Попробуйте ещё раз.';
      throw new Error(detail);
    }
    if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error('Получен некорректный ответ сервера. Повторите проверку.');
    renderResult(data);
    completed = true;
    setText('#uploadHint', 'Проверка завершена. Результат — ниже.');
  } catch (error) {
    const message = controller.signal.aborted
      ? 'Сервер слишком долго не отвечает. Файл сохранён — повторите проверку.'
      : error instanceof TypeError ? 'Не удалось связаться с сервером. Проверьте соединение и попробуйте ещё раз.' : error.message || 'Не удалось проверить изображение. Попробуйте ещё раз.';
    showError(message);
    setState('ready');
    setText('#uploadHint', 'Файл сохранён. Можно повторить проверку.');
  } finally {
    window.clearTimeout(requestTimeout);
    window.clearInterval(stageTimer);
    stageTimer = null;
    setBusy(false);
    setAnalyzeButtonText(completed ? 'Проверить ещё раз' : 'Повторить проверку');
  }
}

imagePickButtons.forEach((button) => button.addEventListener('click', (event) => {
  event.preventDefault();
  event.stopPropagation();
  if (seriesMode && button.id !== 'pickButton') setMode(false);
  if (!isAnalyzing) fileInput.click();
}));
dropzone.addEventListener('click', (event) => {
  if (!isAnalyzing && !event.target.closest('button, input, a, label')) fileInput.click();
});
dropzone.addEventListener('keydown', (event) => {
  if (event.target !== dropzone || (event.key !== 'Enter' && event.key !== ' ')) return;
  event.preventDefault();
  if (!isAnalyzing) fileInput.click();
});
fileInput.addEventListener('change', () => chooseFile(fileInput.files[0]));
dropzone.addEventListener('dragenter', (event) => {
  event.preventDefault();
  if (isAnalyzing) return;
  dragDepth += 1;
  dropzone.classList.add('dragging');
});
dropzone.addEventListener('dragover', (event) => {
  event.preventDefault();
  if (event.dataTransfer) event.dataTransfer.dropEffect = isAnalyzing ? 'none' : 'copy';
});
dropzone.addEventListener('dragleave', (event) => {
  event.preventDefault();
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) dropzone.classList.remove('dragging');
});
dropzone.addEventListener('drop', (event) => {
  event.preventDefault();
  dragDepth = 0;
  dropzone.classList.remove('dragging');
  if (!isAnalyzing) chooseFile(event.dataTransfer?.files[0]);
});
$('#removeButton').addEventListener('click', (event) => {
  event.stopPropagation();
  if (isAnalyzing) return;
  reset();
  dropzone.focus();
});
$('#newCheck').addEventListener('click', () => {
  if (isAnalyzing) return;
  reset();
  dropzone.focus({ preventScroll: true });
  $('#checker').scrollIntoView({ block: 'start', behavior: motionBehavior() });
});
analyzeButton.addEventListener('click', analyze);
remoteToggle.addEventListener('change', updateRemoteHint);
imageSearchButtons.forEach((button) => button.addEventListener('click', searchSelectedImage));

document.addEventListener('paste', (event) => {
  if (isAnalyzing || event.target.closest?.('input:not([type="file"]):not([type="checkbox"]):not([type="radio"]):not([type="button"]), textarea, [contenteditable="true"]')) return;
  const clipboard = event.clipboardData;
  if (!clipboard) return;
  const item = Array.from(clipboard.items || []).find((entry) => entry.kind === 'file' && entry.type.startsWith('image/'));
  const file = item?.getAsFile() || Array.from(clipboard.files || []).find((entry) => isSupportedFile(entry));
  if (!file) return;
  event.preventDefault();
  const extension = (file.type.split('/')[1] || 'png').replace('jpeg', 'jpg');
  const namedFile = file.name ? file : new File([file], `clipboard-${Date.now()}.${extension}`, { type: file.type || 'image/png' });
  if (seriesMode) {
    if (!seriesBusy) addSeriesFiles([namedFile]);
    $('#seriesDrop').focus();
    return;
  }
  chooseFile(namedFile);
  dropzone.focus();
});

setState('idle');
setImageSearchStatus('');
updateManualSearchVisibility();
updateRemoteHint();
fetch('/api/providers')
  .then((response) => {
    if (!response.ok) throw new Error('Конфигурация недоступна');
    return response.json();
  })
  .then(renderProviderStatus)
  .catch(() => {
    setText('#providerStatus', 'Не удалось получить конфигурацию внешних сервисов');
    updateRemoteHint();
  });

// ---------------------------------------------------------------- Серия ---

const MAX_SERIES = 6;
const seriesDrop = $('#seriesDrop');
const seriesInput = $('#seriesInput');
const seriesButton = $('#seriesButton');
const seriesResults = $('#seriesResults');
let seriesMode = false;
let seriesBusy = false;
let seriesFiles = [];
let seriesRevision = 0;
const seriesKindInputs = Array.from(document.querySelectorAll('input[name="seriesMode"]'));

function invalidateSeriesReport() {
  setResultReport('series', null);
  seriesRevision += 1;
  seriesResults.hidden = true;
  if (!seriesBusy) {
    setButtonLabel(seriesButton, seriesFiles.length === 1 ? 'Нужен ещё один снимок' : 'Проверить серию');
    if (seriesMode) setState(seriesFiles.length >= 2 ? 'ready' : 'idle');
  }
}

function setSeriesBusy(busy) {
  seriesBusy = busy;
  const controls = [seriesInput, $('#seriesPick'), $('#seriesRemote'), $('#singleTab'), $('#seriesTab'), $('#seriesNew'), ...seriesKindInputs, ...imagePickButtons, ...Array.from($('#seriesThumbs').querySelectorAll('button'))];
  controls.forEach((control) => { control.disabled = busy; });
  seriesButton.disabled = busy || seriesFiles.length < 2;
  seriesButton.classList.toggle('is-busy', busy);
  seriesButton.setAttribute('aria-busy', String(busy));
  seriesDrop.setAttribute('aria-disabled', String(busy));
  seriesDrop.setAttribute('aria-busy', String(busy));
  seriesDrop.tabIndex = busy ? -1 : 0;
  seriesDrop.classList.toggle('scanning', busy);
  $('#seriesStatus').hidden = !busy;
  imagePickButtons.forEach((button) => button.setAttribute('aria-disabled', String(busy)));
}

function setMode(series) {
  if (isAnalyzing || seriesBusy || seriesMode === series) return;
  seriesMode = series;
  $('#singleTab').classList.toggle('active', !series);
  $('#seriesTab').classList.toggle('active', series);
  $('#singleTab').setAttribute('aria-selected', String(!series));
  $('#seriesTab').setAttribute('aria-selected', String(series));
  $('#singlePanel').hidden = series;
  $('#seriesPanel').hidden = !series;
  results.hidden = true;
  seriesResults.hidden = true;
  if (series) $('#manualSearch').hidden = true;
  else updateManualSearchVisibility();
  setState(series ? (seriesFiles.length >= 2 ? 'ready' : 'idle') : selectedFile ? 'ready' : 'idle');
}

function seriesUrl(file) {
  if (!file.previewUrl) file.previewUrl = URL.createObjectURL(file);
  return file.previewUrl;
}

function setSeriesError(message) {
  const error = $('#seriesError');
  error.textContent = message || '';
  error.hidden = !message;
}

function addSeriesFiles(files) {
  if (seriesBusy) return;
  const previousCount = seriesFiles.length;
  const rejected = [];
  Array.from(files || []).forEach((file) => {
    if (!isSupportedFile(file)) rejected.push(`${file.name}: неподдерживаемый формат`);
    else if (!file.size) rejected.push(`${file.name}: файл пустой`);
    else if (file.size > 20 * 1024 * 1024) rejected.push(`${file.name}: больше 20 МБ`);
    else if (seriesFiles.length >= MAX_SERIES) rejected.push(`${file.name}: не больше ${MAX_SERIES} снимков`);
    else seriesFiles.push(file);
  });
  if (seriesFiles.length !== previousCount) invalidateSeriesReport();
  setSeriesError(rejected.join('\n'));
  renderSeriesThumbs();
}

function removeSeriesFile(index) {
  if (seriesBusy) return;
  const [file] = seriesFiles.splice(index, 1);
  if (file?.previewUrl) URL.revokeObjectURL(file.previewUrl);
  if (file) invalidateSeriesReport();
  setSeriesError('');
  renderSeriesThumbs();
}

function renderSeriesThumbs() {
  const root = $('#seriesThumbs');
  root.replaceChildren();
  seriesFiles.forEach((file, index) => {
    const thumb = document.createElement('div');
    thumb.className = 'series-thumb';
    thumb.title = `${file.name} · ${formatBytes(file.size)}`;
    const image = document.createElement('img');
    image.src = seriesUrl(file);
    image.alt = file.name;
    const number = textElement('span', String(index + 1), 'num');
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'remove-button';
    remove.setAttribute('aria-label', `Убрать снимок ${index + 1}`);
    remove.innerHTML = '<svg class="icon" aria-hidden="true"><use href="#icon-close"/></svg>';
    remove.addEventListener('click', (event) => { event.stopPropagation(); removeSeriesFile(index); });
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
  seriesButton.disabled = seriesBusy || seriesFiles.length < 2;
  setButtonLabel(seriesButton, seriesFiles.length === 1 ? 'Нужен ещё один снимок' : 'Проверить серию');
  if (seriesMode) setState(seriesFiles.length >= 2 ? 'ready' : 'idle');
}

function setButtonLabel(button, text) {
  const label = button.querySelector('span');
  if (label) label.textContent = text;
  else button.textContent = text;
}

function textElement(tag, text, className) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  element.textContent = text;
  return element;
}

const VERDICT_TEXT = { consistent: 'Согласовано', inconsistent: 'Не согласуется', insufficient: 'Недостаточно данных' };
const PAIR_TEXT = {
  consistent_3d: 'согласовано', inconsistent: 'не сходится', planar: 'плоская сцена',
  near_duplicate: 'почти дубликат', no_overlap: 'нет общих деталей', weak: 'слабое совпадение',
};

function renderSeries(data) {
  const consistency = data.consistency || {};
  const verdict = consistency.verdict || data.verdict;
  $('#seriesCard').className = `series-card ${verdict}`;
  setText('#seriesVerdict', consistency.label || VERDICT_TEXT[verdict] || verdict || '—');
  setText('#seriesHeadline', consistency.headline || data.headline || '');
  setText('#seriesDetail', consistency.detail || '');
  $('#seriesDetail').hidden = !consistency.detail;
  const count = Number(data.count) || 0;
  const meta = [`${count} ${plural(count, 'снимок', 'снимка', 'снимков')}`, `режим: ${data.mode === 'person' ? 'один человек' : 'одна сцена'}`];
  if (Number.isFinite(consistency.checked_images) && Number.isFinite(consistency.total_images)) {
    meta.push(`сопоставлено снимков: ${consistency.checked_images} из ${consistency.total_images}`);
  }
  if (Number.isFinite(data.latency_ms)) meta.push(`сравнение ${(data.latency_ms / 1000).toFixed(1)} с`);
  setText('#seriesMeta', meta.join(' · '));

  const ai = data.ai_assessment;
  const aiCard = $('#seriesAiAssessment');
  aiCard.hidden = !ai;
  aiCard.className = `series-ai-assessment ${ai?.status || ''}`;
  setText('#seriesAiHeadline', ai?.headline || '');
  setText('#seriesAiDetail', ai?.detail || '');
  const aiCoverage = Number.isFinite(ai?.checked_images) && Number.isFinite(ai?.total_images)
    ? `Оценки получены для ${ai.checked_images} из ${ai.total_images} снимков.` : '';
  setText('#seriesAiCoverage', aiCoverage);
  $('#seriesAiCoverage').hidden = !aiCoverage;

  const actions = $('#seriesActions');
  actions.replaceChildren(...asArray(data.recommendations).map((text) => textElement('li', text)));

  const reasons = $('#seriesReasons');
  reasons.replaceChildren();
  asArray(data.reasons).forEach((item) => {
    const row = document.createElement('div');
    row.className = `reason ${item.level} ${item.polarity > 0 ? 'pos' : item.polarity < 0 ? 'neg' : 'neutral'}`;
    const body = document.createElement('div');
    body.append(textElement('b', item.title || ''), textElement('p', item.detail || ''));
    row.append(textElement('span', '', 'reason-dot'), body);
    reasons.append(row);
  });
  if (!reasons.childElementCount) reasons.append(textElement('p', 'Явных сигналов нет.', 'series-meta'));

  const images = $('#seriesImages');
  images.replaceChildren();
  asArray(data.images).forEach((item) => {
    const figure = document.createElement('figure');
    figure.className = 'series-image';
    const image = document.createElement('img');
    if (seriesFiles[item.index]) image.src = seriesUrl(seriesFiles[item.index]);
    image.alt = item.filename || '';
    const caption = document.createElement('figcaption');
    caption.title = item.headline || '';
    const numeric = Number.isFinite(item.percent);
    caption.append(textElement('span', `${item.index + 1}. ${item.filename}`), textElement('b', numeric ? `${item.percent}% ИИ` : '—', numeric && item.percent >= 65 ? 'high' : ''));
    figure.append(image, caption);
    images.append(figure);
  });

  const faces = data.faces;
  $('#seriesFacesBlock').hidden = !faces;
  if (faces) {
    const faceRoot = $('#seriesFaces');
    faceRoot.replaceChildren();
    asArray(faces.faces).forEach((face, index) => {
      const figure = document.createElement('figure');
      if (face && typeof face.crop === 'string' && face.crop.startsWith('data:image/')) {
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
    pairRoot.replaceChildren();
    asArray(faces.pairs).forEach((pair) => {
      const row = document.createElement('div');
      row.className = 'face-pair';
      const pose = pair.pose_diff_deg !== null && pair.pose_diff_deg !== undefined ? ` · поворот головы ${pair.pose_diff_deg}°` : '';
      const identity = pair.low_resolution ? 'сравнение ненадёжно: мелкое лицо'
        : pair.same_person ? 'лица похожи'
          : Number.isFinite(pair.cosine) && pair.cosine < 0.25 ? 'лица различаются' : 'сходство неоднозначно';
      row.append(
        textElement('b', `Снимки ${pair.a + 1} и ${pair.b + 1}: ${identity}`),
        textElement('span', `сходство ${Number(pair.cosine).toFixed(2)} · идентичность пикселей ${Number(pair.face_ssim).toFixed(2)}${pose}`),
      );
      pairRoot.append(row);
    });
  }

  const pairs = $('#seriesPairs');
  pairs.replaceChildren();
  asArray(data.pairs).forEach((pair) => {
    const box = document.createElement('article');
    box.className = 'series-pair';
    const top = document.createElement('div');
    top.className = 'series-pair-top';
    top.append(textElement('b', `Снимки ${pair.a + 1} и ${pair.b + 1}`), textElement('span', PAIR_TEXT[pair.status] || pair.status, `chip ${pair.status}`));
    box.append(top, textElement('p', pair.detail || ''));
    if (typeof pair.visual === 'string' && pair.visual.startsWith('data:image/')) {
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
  const known = (value) => value !== null && value !== undefined;
  const facts = [
    ['EXIF', `${exif.with_exif ?? 0} из ${count}`],
    ['Устройства', asArray(exif.devices).join(', ') || '—'],
    ['Разброс времени', known(exif.time_span_s) ? `${Math.round(exif.time_span_s)} с` : '—'],
    ['Разброс GPS', known(exif.gps_max_distance_m) ? `${exif.gps_max_distance_m} м` : '—'],
  ];
  $('#seriesExif').replaceChildren(...facts.map(([key, value]) => {
    const fact = document.createElement('div');
    fact.className = 'fact';
    fact.append(textElement('span', key), textElement('b', value));
    return fact;
  }));

  setResultReport('series', data);
  seriesResults.hidden = false;
  setState('complete');
  seriesResults.focus({ preventScroll: true });
  seriesResults.scrollIntoView({ behavior: motionBehavior(), block: 'start' });
}

async function analyzeSeries() {
  if (isAnalyzing || seriesBusy || seriesFiles.length < 2) return;
  setResultReport('series', null);
  const revision = seriesRevision;
  const files = [...seriesFiles];
  const mode = document.querySelector('input[name="seriesMode"]:checked')?.value || 'scene';
  const remote = $('#seriesRemote').checked;
  setSeriesBusy(true);
  setSeriesError('');
  setButtonLabel(seriesButton, 'Анализируем серию…');
  seriesResults.hidden = true;
  setState('analyzing');
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 180000);
  try {
    const form = new FormData();
    files.forEach((file) => form.append('files', file, file.name));
    const response = await fetch(`/api/analyze-set?mode=${mode}&remote=${remote}`, { method: 'POST', body: form, signal: controller.signal });
    let data = null;
    try { data = await response.json(); } catch { data = null; }
    if (revision !== seriesRevision) return;
    if (!response.ok || !data) throw new Error(data?.detail || 'Не удалось проверить серию. Попробуйте ещё раз.');
    renderSeries(data);
  } catch (error) {
    if (revision !== seriesRevision) return;
    setSeriesError(error.name === 'AbortError' ? 'Проверка серии заняла слишком много времени. Попробуйте меньше снимков.' : error.message || 'Ошибка проверки серии');
    setState(seriesFiles.length >= 2 ? 'ready' : 'idle');
  } finally {
    window.clearTimeout(timeout);
    setSeriesBusy(false);
    setButtonLabel(seriesButton, revision === seriesRevision ? 'Проверить ещё раз' : 'Проверить серию');
    if (revision !== seriesRevision) setState(seriesFiles.length >= 2 ? 'ready' : 'idle');
  }
}

$('#singleTab').addEventListener('click', () => setMode(false));
$('#seriesTab').addEventListener('click', () => setMode(true));
[...seriesKindInputs, $('#seriesRemote')].forEach((control) => control.addEventListener('change', () => {
  invalidateSeriesReport();
  setSeriesError('');
}));
$('#seriesPick').addEventListener('click', (event) => { event.stopPropagation(); if (!seriesBusy) seriesInput.click(); });
seriesDrop.addEventListener('click', (event) => {
  if (seriesBusy) return;
  if (!event.target.closest('button') || event.target.closest('.series-add')) seriesInput.click();
});
seriesDrop.addEventListener('keydown', (event) => {
  if (event.target !== seriesDrop || (event.key !== 'Enter' && event.key !== ' ')) return;
  event.preventDefault();
  if (!seriesBusy) seriesInput.click();
});
seriesInput.addEventListener('change', () => { addSeriesFiles(seriesInput.files); seriesInput.value = ''; });
let seriesDragDepth = 0;
seriesDrop.addEventListener('dragenter', (event) => { event.preventDefault(); if (seriesBusy) return; seriesDragDepth += 1; seriesDrop.classList.add('dragging'); });
seriesDrop.addEventListener('dragover', (event) => { event.preventDefault(); if (event.dataTransfer) event.dataTransfer.dropEffect = seriesBusy ? 'none' : 'copy'; });
seriesDrop.addEventListener('dragleave', (event) => { event.preventDefault(); seriesDragDepth = Math.max(0, seriesDragDepth - 1); if (!seriesDragDepth) seriesDrop.classList.remove('dragging'); });
seriesDrop.addEventListener('drop', (event) => {
  event.preventDefault();
  seriesDragDepth = 0;
  seriesDrop.classList.remove('dragging');
  if (!seriesBusy) addSeriesFiles(event.dataTransfer?.files);
});
seriesButton.addEventListener('click', analyzeSeries);
$('#seriesNew').addEventListener('click', () => {
  if (seriesBusy) return;
  seriesFiles.forEach((file) => file.previewUrl && URL.revokeObjectURL(file.previewUrl));
  seriesFiles = [];
  invalidateSeriesReport();
  setSeriesError('');
  renderSeriesThumbs();
  seriesResults.hidden = true;
  seriesDrop.focus({ preventScroll: true });
  $('#checker').scrollIntoView({ block: 'start', behavior: motionBehavior() });
});

document.querySelectorAll('[data-share-result]').forEach((button) => button.addEventListener('click', shareResult));
document.querySelectorAll('[data-download-pdf]').forEach((link) => link.addEventListener('click', downloadReportPdf));
refreshShareButtons();
