/* Pure logic for the automation sidebar.
 *
 * Everything here is DOM free so it can be unit tested with `node --test`
 * (see tests/test_core.mjs). automation.js owns the DOM and imports this file.
 */

export const DEFAULT_VIEWPORT = { width: 1366, height: 768 };
export const API_PREFIX = '/automation/api';
export const SCHEMA_VERSION = 1;

/* ------------------------------------------------------------------ *
 * Coordinate mapping
 * ------------------------------------------------------------------ */

/**
 * Map a browser mouse event onto desktop pixels.
 *
 * noVNC opens with resize=scale, so the canvas on screen is usually smaller
 * than the 1366x768 desktop. The ratio is taken from the canvas bounding rect,
 * which stays correct whether noVNC scales with CSS size or with a transform.
 */
export function mapToDesktop(clientX, clientY, rect, viewport = DEFAULT_VIEWPORT) {
  if (!rect || !rect.width || !rect.height) return null;
  const x = Math.round(((clientX - rect.left) / rect.width) * viewport.width);
  const y = Math.round(((clientY - rect.top) / rect.height) * viewport.height);
  return {
    x: clamp(x, 0, viewport.width - 1),
    y: clamp(y, 0, viewport.height - 1),
  };
}

export function clamp(value, low, high) {
  return Math.min(high, Math.max(low, value));
}

/* ------------------------------------------------------------------ *
 * Step definitions used by the editor
 * ------------------------------------------------------------------ */

const INT = 'int';
const TEXT = 'text';
const AREA = 'area';
const SELECT = 'select';

export const STEP_DEFS = [
  { type: 'click', group: 'mouse', fields: [
    { key: 'x', kind: INT }, { key: 'y', kind: INT },
    { key: 'button', kind: SELECT, options: ['left', 'middle', 'right'], optional: true },
    { key: 'clicks', kind: INT, optional: true }] },
  { type: 'double_click', group: 'mouse', fields: [{ key: 'x', kind: INT }, { key: 'y', kind: INT }] },
  { type: 'drag', group: 'mouse', fields: [
    { key: 'x1', kind: INT }, { key: 'y1', kind: INT },
    { key: 'x2', kind: INT }, { key: 'y2', kind: INT }] },
  { type: 'move', group: 'mouse', fields: [{ key: 'x', kind: INT }, { key: 'y', kind: INT }] },
  { type: 'scroll', group: 'mouse', fields: [
    { key: 'amount', kind: INT }, { key: 'x', kind: INT, optional: true },
    { key: 'y', kind: INT, optional: true }] },
  { type: 'type', group: 'keyboard', fields: [
    { key: 'text', kind: TEXT },
    { key: 'typingMode', kind: SELECT, options: ['inherit', 'low', 'normal', 'fast'], optional: true },
  ] },
  { type: 'paste', group: 'keyboard', fields: [{ key: 'text', kind: AREA }] },
  { type: 'key', group: 'keyboard', fields: [{ key: 'keysText', kind: TEXT, virtual: true }] },
  { type: 'wait', group: 'flow', fields: [{ key: 'ms', kind: INT }] },
  { type: 'wait_for_text', group: 'flow', fields: [
    { key: 'text', kind: TEXT }, { key: 'timeoutMs', kind: INT, optional: true },
    { key: 'absent', kind: 'bool', optional: true }] },
  { type: 'goto_url', group: 'flow', fields: [{ key: 'url', kind: TEXT }] },
  { type: 'focus_window', group: 'flow', fields: [{ key: 'title', kind: TEXT }] },
  { type: 'screenshot', group: 'flow', fields: [{ key: 'name', kind: TEXT, optional: true }] },
  // Read what the page says right now and put it in the run report, so the
  // model can see the page instead of guessing.
  { type: 'capture_text', group: 'flow', fields: [{ key: 'limit', kind: INT, optional: true }] },
  { type: 'pause_for_human_verification', group: 'safety', fields: [
    { key: 'prompt', kind: AREA },
  ] },
  // The coworker agent's own steps. They belong in the same editor as everything
  // else: an operation from the library is opened into the stages, and a step
  // the editor does not know is a step the normaliser silently drops.
  { type: 'agent_open', group: 'agent', fields: [
    { key: 'provider', kind: TEXT, optional: true },
    { key: 'url', kind: TEXT, optional: true }] },
  { type: 'agent_ask', group: 'agent', fields: [
    { key: 'prompt', kind: AREA },
    { key: 'provider', kind: TEXT, optional: true },
    { key: 'timeout', kind: INT, optional: true },
    { key: 'freshChat', kind: 'bool', optional: true },
    { key: 'saveAs', kind: TEXT, optional: true }] },
  { type: 'captcha_solve', group: 'agent', fields: [
    { key: 'label', kind: TEXT, optional: true },
    { key: 'autoClick', kind: 'bool', optional: true },
    { key: 'notify', kind: 'bool', optional: true },
    { key: 'shot', kind: TEXT, optional: true }] },
];

export const STEP_DEFAULTS = {
  click: { x: 0, y: 0 },
  double_click: { x: 0, y: 0 },
  drag: { x1: 0, y1: 0, x2: 100, y2: 100 },
  move: { x: 0, y: 0 },
  scroll: { amount: 3 },
  type: { text: '' },
  paste: { text: '' },
  key: { keys: ['Return'] },
  wait: { ms: 1000 },
  wait_for_text: { text: '', timeoutMs: 15000 },
  goto_url: { url: 'https://' },
  focus_window: { title: 'Google Chrome' },
  screenshot: {},
  capture_text: {},
  pause_for_human_verification: {
    prompt: 'وقتی بررسی یا کار انسانی تمام شد و صفحه امن و آماده‌ی ادامه بود، این‌جا ادامه را بزن.',
  },
  // Empty on purpose: the server falls back to the configured chat profile, so
  // a hardcoded provider here would outlive the setting it came from.
  agent_open: {},
  agent_ask: { prompt: '' },
  captcha_solve: {},
};

export function stepDef(type) {
  return STEP_DEFS.find((d) => d.type === type) || null;
}

export function newId(prefix = 's') {
  return `${prefix}-${Math.random().toString(16).slice(2, 10)}`;
}

export function createStep(type, overrides = {}) {
  const base = STEP_DEFAULTS[type] || {};
  const step = { id: newId(), type, enabled: true, ...structuredCloneSafe(base), ...overrides };
  step.label = step.label || labelFor(step);
  return step;
}

export function structuredCloneSafe(value) {
  return JSON.parse(JSON.stringify(value));
}

/** Human readable one-liner shown in the step list. */
export function labelFor(step) {
  switch (step.type) {
    case 'click': return `click ${step.x},${step.y}${step.button && step.button !== 'left' ? ' ' + step.button : ''}`;
    case 'double_click': return `double click ${step.x},${step.y}`;
    case 'drag': return `drag ${step.x1},${step.y1} → ${step.x2},${step.y2}`;
    case 'move': return `move ${step.x},${step.y}`;
    case 'scroll': return `scroll ${step.amount}`;
    case 'type': return `type ${quote(step.text)}`;
    case 'paste': return `paste ${quote(step.text)}`;
    case 'key': return `key ${(step.keys || []).join('+')}`;
    case 'wait': return `wait ${step.ms}ms`;
    case 'wait_for_text': return `wait for ${quote(step.text)}${step.absent ? ' (absent)' : ''}`;
    case 'goto_url': return `open ${truncate(step.url, 40)}`;
    case 'focus_window': return `focus ${truncate(step.title, 30)}`;
    case 'screenshot': return step.name ? `screenshot ${step.name}` : 'screenshot';
    case 'capture_text': return step.limit ? `capture text (max ${step.limit})` : 'capture text';
    case 'pause_for_human_verification': return 'pause for human verification';
    case 'agent_open': return `open agent chat${step.provider ? ' ' + step.provider : ''}`
      + `${step.url ? ' ' + truncate(step.url, 40) : ''}`;
    case 'agent_ask': return `ask agent ${quote(step.prompt)}`;
    case 'captcha_solve': return `solve captcha${step.label ? ' ' + truncate(step.label, 20) : ''}`;
    default: return step.type;
  }
}

export function quote(text) {
  const value = String(text == null ? '' : text);
  return `"${truncate(value, 24)}"`;
}

/**
 * Flatten a children argument and drop the values that must not reach the DOM.
 *
 * `appendChild(null)` throws, and a conditional child written as
 * `cond ? el(...) : null` is the normal way to express "maybe no node here",
 * so filtering has to happen in one place that every renderer goes through.
 */
export function compact(children) {
  const out = [];
  for (const child of [].concat(children)) {
    if (child === null || child === undefined || child === false || child === true) continue;
    if (Array.isArray(child)) out.push(...compact(child));
    else out.push(child);
  }
  return out;
}

export function truncate(value, max) {
  const text = String(value == null ? '' : value);
  return text.length > max ? text.slice(0, max - 1) + '…' : text;
}

/* ------------------------------------------------------------------ *
 * Flow documents
 * ------------------------------------------------------------------ */

export function emptyFlow(name = 'Untitled flow') {
  return {
    schema: SCHEMA_VERSION,
    name,
    description: '',
    viewport: { ...DEFAULT_VIEWPORT },
    settings: {
      defaultDelayAfterMs: 350,
      typingMode: 'normal',
      screenshotAfterEachStep: false,
      stopOnError: true,
      repeat: 1,
      allowShellSteps: false,
      stepTimeoutMs: 60000,
    },
    steps: [],
  };
}

/**
 * Client side sanity check. The server validates authoritatively; this only
 * stops the obviously broken flows before they cost a round trip.
 */
export function validateFlow(flow, viewport = DEFAULT_VIEWPORT) {
  const errors = [];
  if (!flow || typeof flow !== 'object') return ['flow must be an object'];
  if (!Array.isArray(flow.steps)) errors.push('steps must be a list');
  if (flow.settings && flow.settings.typingMode !== undefined
      && !['low', 'normal', 'fast'].includes(flow.settings.typingMode)) {
    errors.push('settings: typingMode must be low, normal, or fast');
  }
  const w = (flow.viewport && flow.viewport.width) || viewport.width;
  const h = (flow.viewport && flow.viewport.height) || viewport.height;
  (flow.steps || []).forEach((step, index) => {
    const where = `steps[${index}] (${step.type})`;
    if (!stepDef(step.type)) { errors.push(`${where}: unknown step type`); return; }
    for (const key of ['x', 'y']) {
      if (step[key] !== undefined && (step[key] < 0 || step[key] >= (key === 'x' ? w : h))) {
        errors.push(`${where}: ${key}=${step[key]} is outside the ${w}x${h} desktop`);
      }
    }
    if (step.type === 'key' && !(Array.isArray(step.keys) && step.keys.length)) {
      errors.push(`${where}: keys must not be empty`);
    }
    if (step.type === 'goto_url' && !/^[a-zA-Z][\w+.-]*:\/\//.test(step.url || '')) {
      errors.push(`${where}: url needs a scheme such as https://`);
    }
    if ((step.type === 'type' || step.type === 'paste') && !step.text) {
      errors.push(`${where}: text must not be empty`);
    }
    if (step.type === 'type' && step.typingMode !== undefined
        && !['low', 'normal', 'fast'].includes(step.typingMode)) {
      errors.push(`${where}: typingMode must be low, normal, or fast`);
    }
    if (step.type === 'pause_for_human_verification') {
      if (!(typeof step.prompt === 'string' && step.prompt.trim())) {
        errors.push(`${where}: prompt must not be empty`);
      } else if (step.prompt.length > 1000) {
        errors.push(`${where}: prompt must be at most 1000 characters`);
      }
    }
    // The server requires a prompt for agent_ask; saying so here means the
    // operator hears it before a run does, in the same words.
    if (step.type === 'agent_ask'
        && !(typeof step.prompt === 'string' && step.prompt.trim())) {
      errors.push(`${where}: prompt must not be empty`);
    }
    if (step.type === 'wait' && !(Number(step.ms) > 0)) {
      errors.push(`${where}: ms must be positive`);
    }
  });
  return errors;
}

/** The keys list is edited as one text box ("ctrl+l, Return"). */
export function keysFromText(text) {
  return String(text || '').split(/[,\s]+/).map((s) => s.trim()).filter(Boolean);
}

export function keysToText(keys) {
  return (keys || []).join(', ');
}

export function moveStep(steps, from, to) {
  const next = steps.slice();
  if (from < 0 || from >= next.length || to < 0 || to >= next.length) return next;
  const [item] = next.splice(from, 1);
  next.splice(to, 0, item);
  return next;
}

/* ------------------------------------------------------------------ *
 * AI prompt helpers
 * ------------------------------------------------------------------ */

/** Compact steps for an AI: defaults implied by the schema are dropped. */
export function promptSteps(flow) {
  const implied = { enabled: true, requiresConfirmation: false, continueOnError: false };
  return (flow.steps || []).map((step) => {
    const out = {};
    for (const [key, value] of Object.entries(step)) {
      if (value === undefined || value === null || value === '') continue;
      if (key in implied && value === implied[key]) continue;
      out[key] = value;
    }
    return out;
  });
}

export function flowAsJson(flow) {
  return JSON.stringify({
    schema: SCHEMA_VERSION,
    name: flow.name,
    viewport: flow.viewport,
    settings: flow.settings,
    steps: promptSteps(flow),
  }, null, 2);
}

/** Pull the first JSON object out of an AI reply, tolerating prose and fences. */
export function extractJson(text) {
  if (!text) return null;
  const fenced = text.match(/```(?:json)?\s*([\s\S]*?)```/i);
  const candidates = [];
  if (fenced) candidates.push(fenced[1]);
  candidates.push(text);
  for (const candidate of candidates) {
    const start = candidate.indexOf('{');
    if (start === -1) continue;
    const slice = candidate.slice(start);
    let depth = 0;
    let inString = false;
    let escaped = false;
    for (let i = 0; i < slice.length; i += 1) {
      const ch = slice[i];
      if (inString) {
        if (escaped) escaped = false;
        else if (ch === '\\') escaped = true;
        else if (ch === '"') inString = false;
        continue;
      }
      if (ch === '"') inString = true;
      else if (ch === '{') depth += 1;
      else if (ch === '}') {
        depth -= 1;
        if (depth === 0) {
          try { return JSON.parse(slice.slice(0, i + 1)); } catch (_) { break; }
        }
      }
    }
  }
  return null;
}

/** Normalise whatever an AI returned into a flow we can run. */
export function normaliseImportedFlow(data, viewport = DEFAULT_VIEWPORT) {
  if (!data || typeof data !== 'object') return { flow: null, errors: ['not a JSON object'] };
  const flow = emptyFlow(typeof data.name === 'string' && data.name ? data.name : 'Imported flow');
  if (data.description) flow.description = String(data.description).slice(0, 300);
  if (data.viewport && Number(data.viewport.width) > 0) {
    flow.viewport = { width: Number(data.viewport.width), height: Number(data.viewport.height) };
  } else {
    flow.viewport = { ...viewport };
  }
  if (data.settings && typeof data.settings === 'object') {
    flow.settings = { ...flow.settings, ...data.settings };
  }
  const raw = Array.isArray(data.steps) ? data.steps : [];
  flow.steps = raw
    .filter((step) => step && stepDef(step.type))
    .map((step) => {
      const next = { id: newId(), enabled: true, ...step };
      if (typeof next.keys === 'string') next.keys = keysFromText(next.keys);
      next.label = next.label || labelFor(next);
      return next;
    });
  const dropped = raw.length - flow.steps.length;
  const errors = validateFlow(flow, flow.viewport);
  if (dropped > 0) errors.push(`${dropped} step(s) used an unknown type and were dropped`);
  if (!flow.steps.length) errors.push('no usable steps were found');
  return { flow, errors };
}

/* ------------------------------------------------------------------ *
 * Small helpers
 * ------------------------------------------------------------------ */

export function formatElapsed(ms) {
  if (!Number.isFinite(ms) || ms < 0) return '--';
  const seconds = Math.floor(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

export function formatDateTime(epochSeconds) {
  if (!epochSeconds) return '--';
  const d = new Date(epochSeconds * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} `
    + `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

export function formatTime(epochSeconds) {
  if (!epochSeconds) return '--';
  const date = new Date(epochSeconds * 1000);
  return date.toLocaleString();
}

/* ------------------------------------------------------------------ *
 * Translations. Persian is the default; English is one click away.
 * ------------------------------------------------------------------ */

export const STRINGS = {
  fa: {
    title: 'اتوماسیون',
    tabFlow: 'مراحل', tabRecord: 'ضبط', tabPages: 'صفحه‌ها',
    tabShots: 'اسکرین‌شات‌ها', tabTexts: 'متون استخراج‌شده',
    tabAi: 'هوش مصنوعی', tabLog: 'گزارش',
    tabAgent: 'ایجنت همکار',
    agentSubAssistant: 'دستیار', agentSubJobs: 'وظایف', agentSubCaptcha: 'کپچا',
    agentSubTelegram: 'تلگرام', agentSubData: 'داده‌ها', agentSubScripts: 'اسکریپت',
    agentSubKeys: 'کلیدها',
    tabChat: 'چت',
    tabLibrary: 'کتابخانه',
    steps: 'گام',
    chatHint: 'پاسخ‌ها از مرورگر خودِ ایجنت می‌آید (به‌طور پیش‌فرض chat.deepseek.com روی نمایش :2)، نه از کلید API پولی. یک پرسش ممکن است یک دقیقه طول بکشد؛ پنل را نبند، جواب خودش می‌آید.',
    chatProvider: 'مدل / پروفایل',
    chatContext: 'زمینهٔ اتوماسیون',
    chatContextHint: 'وضعیت اجرا، نام جریان‌ها، عملیات کتابخانه و متن صفحهٔ فعال به‌صورت فقطخواندنی به پرامپت اضافه می‌شود.',
    chatPlaceholder: 'پیامت را بنویس… (Ctrl+Enter برای ارسال)',
    chatSend: 'ارسال',
    chatSent: 'پیام فرستاده شد؛ منتظر پاسخ',
    chatThinking: '… در حال فکر کردن (مرورگر ایجنت دارد تایپ می‌کند)',
    chatFailed: 'پاسخ ناموفق بود',
    chatEmpty: 'متن پیام خالی است',
    chatNoHistory: 'هنوز پیامی نیست؛ اولین پرسش را بنویس.',
    chatCleared: 'گفت‌وگو پاک شد',
    chatClear: 'پاک کردن گفت‌وگو',
    chatApplyFlow: 'اعمال به‌عنوان جریان',
    chatNoFlow: 'این پیام جریان قابل استفاده‌ای ندارد',
    chatTools: 'ابزارهای دستیار (کپی پرامپت / وارد کردن JSON)',
    chatToolsHint: 'همان گردش کار «دستیار»: پرامپت کامل را کپی کن، یا بگذار ایجنت مستقیم جواب بدهد و JSON پیشنهادی‌اش را وارد مراحل کن.',
    opHint: 'عملیات = چند گام با یک نام؛ یک‌بار می‌سازی و بعد اجرا یا «باز» می‌کنی. «باز کردن در مراحل» گام‌ها را داخل تب مراحل می‌ریزد تا ویرایش و اجراشان کنی.',
    opNew: 'عملیات تازه',
    opCount: 'عملیات',
    opNone: 'عملیاتی نیست؛ یکی بساز.',
    opNeedsAgent: 'کتابخانه به ایجنت همکار نیاز دارد (در این استقرار ساخته نشده).',
    opName: 'نام عملیات',
    opNameHint: 'مثلاً: اتصال به ایجنت lmarena',
    opDescription: 'توضیح',
    opTags: 'برچسب‌ها',
    opTagsHint: 'با «،» جدا کن',
    opSteps: 'گام‌ها',
    opAddStep: 'افزودن گام',
    opFieldsHint: 'مقدارهای هر گام را به‌صورت JSON بنویس، مثلاً {"x": 512, "y": 300}. فیلدهای لازم هر نوع گام داخل placeholder همان کادر آمده است.',
    opSave: 'ذخیرهٔ عملیات',
    opCancel: 'انصراف',
    opEdit: 'ویرایش',
    opDelete: 'حذف',
    opDeleteSure: 'مطمئنی؟ حذف کن',
    opDeleted: 'حذف شد',
    opReset: 'بازنشانی به نسخهٔ کارخانه',
    opResetDone: 'به حالت اول برگشت',
    opRun: 'اجرا',
    opOpen: 'باز کردن در مراحل',
    opOpened: 'در مراحل باز شد',
    opStarted: 'اجرای عملیات شروع شد',
    opBuiltin: 'آماده',
    opLastRun: 'آخرین اجرا',
    opNeverRun: 'هنوز اجرا نشده',
    opNeedName: 'نام عملیات خالی است',
    opBadJson: 'JSON گام درست نیست',
    opEmpty: 'این عملیات گامی ندارد',
    opSaved: 'عملیات ذخیره شد',
    agentSubAgentkey: 'کلید ایجنت',
    agentSubCursor: 'نشانگر',
    keyTitle: 'کلید API ایجنت',
    keyHint: 'یک کلید، با تاریخ ساخت. این کلید همان دسترسی توکن اصلی را می‌دهد، پس فقط به ابزار خودت بده.',
    keyShow: 'نمایش کلید',
    keyHide: 'پنهان کردن',
    keyCopy: 'کپی کلید',
    keyRotate: 'ساخت کلید تازه',
    keyRotated: 'کلید تازه ساخته شد؛ کلید قبلی از کار افتاد',
    keyRevealAudited: 'کلید نمایش داده شد (این اتفاق در گزارش ثبت می‌شود)',
    keyCreatedAt: 'تاریخ ساخت',
    keyLastUsed: 'آخرین استفاده',
    keyLength: 'طول',
    keyEnabledLabel: 'فعال',
    keyDisabledLabel: 'غیرفعال',
    keyEnabled: 'کلید فعال شد',
    keyDisabled: 'کلید غیرفعال شد',
    keyCut: 'قطع دسترسی',
    keyCutDone: 'دسترسی کلید قطع شد',
    keyCutHint: 'همین لحظه قطع می‌شود و در دیتابیس می‌ماند، یعنی بعد از ری‌استارت هم قطع است. توکن اصلی (AUTOMATION_TOKEN) همیشه کار می‌کند.',
    keyNotSet: 'کلیدی ساخته نشده',
    keyWays: 'سه راه فرستادن کلید',
    keyWaysHint: 'هر کدام را که ابزارت قبول می‌کند انتخاب کن؛ هر سه همان یک کلید را می‌رسانند. راه سوم برای ابزارهایی است که هدر سفارشی نمی‌فرستند (مثل نسخهٔ وب ChatGPT).',
    keyCutNote: 'اگر دسترسی قطع باشد، همهٔ این مسیرها با کد ۴۰۳ رد می‌شوند.',
    keyEndpoints: 'مسیرهایی که ایجنت می‌تواند صدا بزند',
    keyEndpointsHint: 'آدرس‌ها کامل‌اند (با دامنهٔ عمومی). «کپی همه» را بزن تا یکجا به یک AI دیگر بدهی.',
    keyCopyAll: 'کپی همه',
    keyMethod: 'متد',
    keyPath: 'آدرس',
    keyWhat: 'کار',
    keyNoEndpoints: 'فهرست مسیرها در دسترس نیست',
    cursorTitle: 'نشانگر ماوس',
    cursorHint: 'نشانگر زرد و بزرگ‌تر از معمول روی دسکتاپ مجازی، تا داخل noVNC گم نشود؛ با هر کلیک هم یک موج باز می‌شود تا معلوم شود کلیک کجا نشست.',
    cursorEnabled: 'نشانگر بزرگ فعال باشد',
    cursorSize: 'اندازه (پیکسل)',
    cursorColor: 'رنگ',
    cursorOutline: 'رنگ لبه',
    cursorRipple: 'موج هنگام کلیک',
    cursorSave: 'ذخیره و اعمال',
    cursorSaved: 'ذخیره شد و روی مرورگر نشست',
    cursorSavedNoBrowser: 'ذخیره شد؛ مرورگر در دسترس نبود، از اجرای بعدی اعمال می‌شود',
    cursorPreview: 'نمایش تقریبی اندازه و رنگ',
    cursorMove: 'حرکت نشانگر برای دیدن',
    cursorMoved: 'نشانگر حرکت کرد',
    cursorMoveHint: 'موج را با یک «کلیک واقعی» از تب صفحه‌ها ببین؛ همان‌جا باز می‌شود.',
    clickNow: 'کلیک واقعی',
    clickNowHint: 'همین حالا نشانگر را روی این مختصات می‌برد و کلیک می‌کند، بدون ساخت گام. اگر اجرایی مالک ماوس باشد، رد می‌شود (۴۰۹).',
    elementsClickHint: '«کلیک واقعی» همان لحظه روی دسکتاپ کلیک می‌کند؛ «کلیک» یک گام به مراحل اضافه می‌کند.',
    copyShotLink: 'کپی لینک تصویر',
    noShotForPage: 'برای این صفحه تصویری ثبت نشده',
    refreshShot: 'اسکرین‌شات تازه',
    shotTaken: 'اسکرین‌شات گرفته شد',
    tgChannels: 'کانال‌ها (ربات و اکانت شخصی)',
    tgChannelsHint: 'هر دو کانال در اختیار ایجنت است. «هر دو» یعنی پیام از هر کدام که سالم باشد می‌رود و خرابی یکی پیام را نمی‌خواباند.',
    tgModeOff: 'خاموش',
    tgModeBot: 'فقط ربات',
    tgModeAccount: 'فقط اکانت شخصی',
    tgModeBoth: 'هر دو',
    tgChannel: 'کانال',
    tgState: 'وضعیت',
    tgNeeds: 'چه چیزی لازم دارد',
    tgSession: 'فایل نشست',
    tgChannelBot: 'ربات',
    tgChannelAccount: 'اکانت شخصی',
    tgChannelAuto: 'خودکار (طبق تنظیم بالا)',
    tgRouteFollow: 'خودکار (پیروی از حالت کلی)',
    tgReady: 'آماده',
    tgNotReady: 'آماده نیست',
    tgRouting: 'مسیریابی پیام‌ها',
    tgRoutingHint: 'برای هر نوع پیام مشخص کن از کدام کانال برود؛ «خودکار» یعنی از همان حالت کلی بالا پیروی کند.',
    tgPurposeHandoff: 'تحویل به انسان',
    tgPurposeCaptcha: 'کپچا',
    tgPurposeJobs: 'وظایف زمان‌بندی‌شده',
    tgPurposeManual: 'پیام دستی / ایجنت',
    tgBotHint: 'توکن را از @BotFather بگیر. مقدار ذخیره‌شده هیچ‌وقت نمایش داده نمی‌شود؛ کادر خالی یعنی تغییر نده.',
    tgAccountHint: 'api_id و api_hash از my.telegram.org؛ بعد یک‌بار «ارسال کد» و «پایان لاگین». فایل نشست روی /data می‌ماند و بعد از ری‌استارت هم اعتبار دارد.',
    tgAccountOff: 'برای فعال شدن این بخش، حالت را روی «فقط اکانت شخصی» یا «هر دو» بگذار و ذخیره کن.',
    tgCodeSent: 'کد ارسال شد؛ آن را در کادر بگذار و «پایان لاگین» را بزن',
    tgTest: 'پیام آزمایشی',
    tgTestHint: 'پیام آزمایشی حتماً یک گیرنده می‌خواهد: اول گیرنده را از فهرست پایین (بخش ۵) انتخاب یا کشف کن.',
    tgTestTarget: 'گیرنده',
    tgTestChannel: 'کانال',
    tgTestText: 'متن',
    tgTestTextHint: 'خالی بگذاری، متن پیش‌فرض می‌رود',
    tgTestSend: 'بفرست',
    tgTestSent: 'فرستاده شد',
    tgTestNeedTarget: 'اول یک گیرنده انتخاب کن',
    tgTestNoTargets: 'گیرنده‌ای ذخیره نشده (اول کشف کن)',
    tgSendTitle: 'پیام دلخواه (دقیقاً کاری که ایجنت می‌کند)',
    tgSendHint: 'اینجا همان مسیر /agent/telegram/send صدا زده می‌شود: متن دلخواه، یک گیرنده یا همه، کانال دلخواه و «منظور» پیام. با تغییر منظور می‌توانی مسیریابی بخش ۲ را آزمایش کنی.',
    tgSendTextHint: 'متن پیام',
    tgSendTarget: 'گیرنده',
    tgSendEveryone: 'همهٔ گیرنده‌های ذخیره‌شده',
    tgSendPurpose: 'منظور پیام',
    tgSendButton: 'فرستادن پیام دلخواه',
    tgSendNeedText: 'متن پیام خالی است',
    agentNotAttached: 'در این استقرار ایجنت همکار ساخته نشده است.',
    agentSave: 'ذخیره', agentRefresh: '⟳ تازه‌سازی', agentDelete: 'پاک کردن',
    agentTest: 'پیام آزمایشی', agentRun: 'اجرا', agentApprove: 'تأیید',
    agentReject: 'رد', agentKill: 'توقف فوری', agentSend: 'بفرست',
    agentAdd: 'افزودن', agentNew: 'جدید', agentConfirmDelete: 'پاک شود؟',
    agentSaved: 'ذخیره شد', agentHint: 'راهنما', agentEmpty: 'موردی نیست',
    agentKillSwitch: 'کلید قطع', agentKillSwitchHint: 'روشن = ایجنت هیچ کاری انجام نمی‌دهد (گفت‌وگو، وظیفه، اسکریپت، کلیک کپچا).',
    agentProvider: 'منبع پاسخ', agentChatProvider: 'سایت چت', agentModel: 'مدل',
    agentBaseUrl: 'نشانی API', agentApiKey: 'کلید API', agentAiStatus: 'وضعیت مرورگر ایجنت',
    agentAiOpen: 'باز کردن سایت چت', agentAiShot: 'تصویر نمایش ایجنت',
    agentAiHint: 'حالت «مرورگر ایجنت» کلید API نمی‌خواهد: ایجنت سایت چت را در یک مرورگر جدا باز می‌کند و پاسخ را از روی صفحه می‌خواند. کندتر و شکننده‌تر است.',
    agentChatAsk: 'از ایجنت بپرس', agentChatPlaceholder: 'چه کاری روی این صفحه انجام شود؟',
    agentChatDirect: 'پرسش مستقیم', agentChatCopy: 'کپی پرامپت',
    agentJobName: 'نام', agentJobKind: 'نوع زمان', agentJobSchedule: 'زمان‌بندی',
    agentJobTarget: 'مقصد', agentJobAction: 'کار', agentJobNext: 'اجرای بعدی',
    agentJobLast: 'آخرین نتیجه', agentJobNow: 'همین حالا',
    agentJobQueued: 'اجرا در پس‌زمینه شروع شد؛ نتیجه در ستون «آخرین نتیجه» ظاهر می‌شود. چند لحظه دیگر این جدول را تازه کنید.',
    agentJobHint: 'at = یک بار در لحظهٔ مشخص · every = بازه (مثل 5m یا 2h) · cron = پنج فیلد (مثل «30 7 * * 6»).',
    agentScriptCode: 'کد', agentScriptLang: 'زبان', agentScriptStatus: 'وضعیت',
    agentScriptOutput: 'خروجی', agentScriptGate: 'اسکریپت تا تأیید شما اجرا نمی‌شود. ویرایش کد، تأیید قبلی را باطل می‌کند.',
    agentTgMode: 'حالت ارسال', agentTgBotToken: 'توکن ربات', agentTgTargets: 'مقصدها',
    agentTgDiscover: 'پیدا کردن چت‌ها', agentTgPhone: 'شمارهٔ تلفن', agentTgCode: 'کد ورود',
    agentTgLogin: 'درخواست کد', agentTgLoginFinish: 'ورود',
    agentTgAccountWarn: 'حالت «اکانت» با Telethon به اکانت شخصی شما وصل می‌شود. فایل نشست آن دسترسی کامل به اکانت می‌دهد و تلگرام ممکن است اکانت اتوماسیون‌شده را محدود کند.',
    agentTgHint: 'برای پیدا کردن مقصد: اول به ربات پیام بدهید، سپس «پیدا کردن چت‌ها» را بزنید.',
    agentCaptchaStrategies: 'راهبردها', agentCaptchaAutoClick: 'کلیک خودکار',
    agentCaptchaMaxAttempts: 'حداکثر تلاش', agentCaptchaMinConfidence: 'حداقل اطمینان',
    agentCaptchaExtension: 'حل‌کنندهٔ آماده', agentCaptchaSolve: 'تحلیل کپچای فعلی',
    agentCaptchaHistory: 'تاریخچه', agentCaptchaProposal: 'پیشنهاد',
    agentCaptchaHint: 'پیش‌فرض: پیشنهاد می‌دهد و کلیک نمی‌کند. «کلیک خودکار» را فقط بعد از دیدن نتیجهٔ درست روشن کنید. کپچاهای رفتاری (تیک reCAPTCHA، hCaptcha) با بینایی حل نمی‌شوند و به انسان می‌روند.',
    agentQuery: 'پرس‌وجوی SQL', agentQueryRun: 'اجرا', agentNotes: 'یادداشت‌ها',
    agentAudit: 'گزارش اقدام‌ها', agentQueryHint: 'فقط SELECT؛ نوشتن از راه همان CRUDها انجام می‌شود.',
    shotsEmpty: 'هنوز اسکرین‌شاتی گرفته نشده', shotsCount: 'اسکرین‌شات',
    openShot: 'باز کردن تصویر', deleteShotConfirm: 'این رکورد اسکرین‌شات پاک شود؟',
    textsEmpty: 'هنوز متنی ذخیره نشده', textsCount: 'متن',
    noteHint: 'متنی را که می‌خواهی نگه داری اینجا بنویس و «ذخیره یادداشت» را بزن',
    noteEmpty: 'متنی ننوشتی', saveNote: 'ذخیره یادداشت', saved: 'ذخیره شد',
    deleteTextConfirm: 'این متن پاک شود؟', refresh: 'تازه‌سازی', copy: 'کپی',
    flowName: 'نام مجموعه', addStep: 'افزودن گام', stepsEmpty: 'هنوز گامی ندارید.',
    run: 'اجرا', pause: 'توقف موقت', resume: 'ادامه', stop: 'قطع', stepOver: 'یک گام',
    save: 'ذخیره', saveAs: 'ذخیره با نام', load: 'بارگذاری', delete: 'حذف', duplicate: 'تکرار',
    settings: 'تنظیمات', defaultDelay: 'مکث پیش‌فرض (میلی‌ثانیه)', repeat: 'تعداد دفعات',
    screenshotEach: 'عکس بعد از هر گام', stopOnError: 'توقف در خطا',
    recordOn: 'ضبط روشن است', recordOff: 'ضبط خاموش',
    recordHint: 'روی صفحه کلیک کنید تا به‌عنوان گام ثبت شود. کلیک‌ها به مرورگر نمی‌رسند.',
    keyboardHint: 'کلیدها هم ضبط شوند', clear: 'پاک کردن',
    detect: 'شناسایی صفحه', detecting: 'در حال شناسایی…',
    copyDetected: 'کپی موارد شناسایی', shotLink: 'لینک عمومی عکس',
    aiRequest: 'درخواست شما از هوش مصنوعی',
    aiRequestHint: 'مثلاً: روی اولین نتیجه کلیک کن، عنوانش را در فیلد جستجو بنویس و Enter بزن. این متن داخل پرامپت می‌رود.',
    aiRequestNone: 'هنوز صفحه‌ای شناسایی نشده؛ اول «شناسایی صفحه» را بزن.',
    openVnc: 'نمای مرورگر', openPanel: 'پنل بزرگ',
    copyReport: 'کپی گزارش اجرا', nowRunning: 'در حال اجرا:',
    library: 'کتابخانه‌ی جریان‌ها', libraryEmpty: 'هنوز جریانی ذخیره نشده',
    stepsWord: 'گام', deleteSavedConfirm: 'جریان «{n}» از کتابخانه پاک شود؟',
    screenshotBefore: 'عکس قبل از هر گام',
    screenshotOnError: 'عکس هنگام خطا',
    aiNotes: 'گفته‌های هوش مصنوعی (خلاصه و سؤال‌ها)',
    clearAll: 'پاک کردن همه', clearedAll: 'همه‌ی مراحل پاک شد',
    nothingToClear: 'مرحله‌ای برای پاک کردن نیست',
    clearAllConfirm: 'همه‌ی {n} مرحله پاک شود؟ این کار برگشت‌پذیر نیست.',
    stateRunning: 'در حال اجرا', statePending: 'هنوز اجرا نشده',
    stateOk: 'موفق', stateError: 'ناموفق', stateSkipped: 'رد شده',
    stateIgnored: 'خطا نادیده گرفته شد',
    recordStandalone: 'ضبطِ کلیک به نمای مرورگر نیاز دارد، چون باید روی خودِ صفحه کلیک کنی. '
      + 'برای ضبط، نمای مرورگر را باز کن؛ بقیه‌ی بخش‌ها (ساخت، ویرایش، اجرا، ذخیره، '
      + 'شناسایی صفحه و هوش مصنوعی) همین‌جا کامل کار می‌کنند و همان مرورگر را کنترل می‌کنند.',
    pagesEmpty: 'هنوز صفحه‌ای شناسایی نشده.',
    elements: 'المان‌ها', text: 'متن صفحه', screenshot: 'عکس',
    copyGuide: 'کپی راهنمای عمومی', copyPrompt: 'کپی پرامپت کامل', importBtn: 'وارد کردن خروجی AI',
    importHint: 'خروجی JSON هوش مصنوعی را اینجا بچسبانید.',
    tokenBarTitle: 'برای فعال شدن پنل، رمز اتصال را وارد کن',
    tokenBarWrong: 'رمز اشتباه است؛ همان AUTOMATION_TOKEN که در Colab چاپ شد را وارد کن',
    tokenSubmit: 'ثبت',
    needTokenShort: 'رمز لازم است',
    token: 'رمز اتصال', tokenHint: 'همان AUTOMATION_TOKEN که در Colab چاپ شد.',
    connected: 'متصل', needToken: 'رمز لازم است',
    logEmpty: 'گزارشی نیست.',
    typingMode: 'سرعت پیش‌فرض تایپ',
    typingModeHelp: 'این گزینه فقط برای پایداری ورودی است؛ تایپ تصادفی یا راه دورزدن CAPTCHA نیست. برای فارسی و متن بلند از paste استفاده کن.',
    typingInherit: 'پیش‌فرض جریان', typingLow: 'کم · ۵۰ ms بین کلیدها',
    typingNormal: 'معمولی · ۱۵ ms بین کلیدها', typingFast: 'سریع · ۰ ms بین کلیدها',
    challengeTitle: 'بررسی امنیتی: اجرای خودکار متوقف شد',
    challengeBody: 'نشانه‌ای از CAPTCHA یا بررسی امنیتی دیده شد. فقط خودت آن را در مرورگر بررسی کن؛ کد یا پاسخ را برای هوش مصنوعی نفرست. بعد از رفع چالش و عادی‌شدن صفحه، ادامه را بزن.',
    manualHandoffTitle: 'اقدام انسانی لازم است',
    humanContinue: 'ادامه پس از بررسی انسانی', humanStop: 'توقف جریان',
    viewChallengeShot: 'دیدن عکس زمان توقف', possibleChallenge: 'احتمال CAPTCHA / بررسی امنیتی',
    challengeSignals: 'نشانه‌ها', noChallengeDetected: 'در این بررسی نشانه‌ی روشنی از CAPTCHA پیدا نشد؛ تشخیص قطعی نیست.',
    challengeCopySafety: 'ایمنی: اگر CAPTCHA یا بررسی امنیتی باشد، توقف کن؛ فقط انسان آن را انجام می‌دهد.',
    confirmTitle: 'تأیید انسانی لازم است', approve: 'تأیید می‌کنم', refuse: 'اجرا نشود',
    statusIdle: 'آماده', statusRunning: 'در حال اجرا', statusPaused: 'متوقف موقت',
    statusWaiting: 'منتظر تأیید', statusDone: 'تمام شد', statusError: 'خطا', statusStopped: 'قطع شد',
    invalidFlow: 'این مجموعه ایراد دارد', errors: 'ایرادها', close: 'بستن',
    elementSelector: 'انتخاب‌گر', elementText: 'متن', elementPos: 'مختصات', useAsClick: 'کلیک',
  },
  en: {
    title: 'Automation',
    tabFlow: 'Flow', tabRecord: 'Record', tabPages: 'Pages',
    tabShots: 'Screenshots', tabTexts: 'Extracted texts',
    tabAi: 'AI', tabLog: 'Log',
    tabAgent: 'Coworker agent',
    agentSubAssistant: 'Assistant', agentSubJobs: 'Jobs', agentSubCaptcha: 'Captcha',
    agentSubTelegram: 'Telegram', agentSubData: 'Data', agentSubScripts: 'Scripts',
    agentSubKeys: 'Keys',
    tabChat: 'Chat',
    tabLibrary: 'Library',
    steps: 'steps',
    chatHint: 'Answers come from the agent\'s own browser (chat.deepseek.com on display :2 by default), not from a paid API key. A turn can take a minute; the answer arrives on its own.',
    chatProvider: 'Model / profile',
    chatContext: 'Automation context',
    chatContextHint: 'Run status, flow names, library operations and the active page text are added read-only to the prompt.',
    chatPlaceholder: 'Write your message… (Ctrl+Enter to send)',
    chatSend: 'Send',
    chatSent: 'Message sent; waiting for the answer',
    chatThinking: '… thinking (the agent browser is typing)',
    chatFailed: 'The answer failed',
    chatEmpty: 'The message is empty',
    chatNoHistory: 'No messages yet; write the first one.',
    chatCleared: 'Chat cleared',
    chatClear: 'Clear the chat',
    chatApplyFlow: 'Apply as a flow',
    chatNoFlow: 'That message holds no usable flow',
    chatTools: 'Assistant tools (copy prompt / import JSON)',
    chatToolsHint: 'The old assistant workflow: copy the full prompt, or let the agent answer directly and import the JSON it proposes.',
    opHint: 'An operation is a named bundle of steps: build it once, then run it or open it. Opening puts the steps into the stages tab for editing and running.',
    opNew: 'New operation',
    opCount: 'operations',
    opNone: 'No operations yet; create one.',
    opNeedsAgent: 'The library needs the coworker agent (not built in this deployment).',
    opName: 'Operation name',
    opNameHint: 'e.g. connect to the lmarena agent',
    opDescription: 'Description',
    opTags: 'Tags',
    opTagsHint: 'separate with commas',
    opSteps: 'Steps',
    opAddStep: 'Add a step',
    opFieldsHint: 'Write each step\'s values as JSON, e.g. {"x": 512, "y": 300}. The fields a step type needs are in that box\'s placeholder.',
    opSave: 'Save the operation',
    opCancel: 'Cancel',
    opEdit: 'Edit',
    opDelete: 'Delete',
    opDeleteSure: 'Sure? delete',
    opDeleted: 'Deleted',
    opReset: 'Reset to the factory version',
    opResetDone: 'Back to the original',
    opRun: 'Run',
    opOpen: 'Open in the stages',
    opOpened: 'Opened in the stages',
    opStarted: 'The operation started',
    opBuiltin: 'built-in',
    opLastRun: 'Last run',
    opNeverRun: 'Never run yet',
    opNeedName: 'The operation needs a name',
    opBadJson: 'That step\'s JSON is not valid',
    opEmpty: 'That operation has no steps',
    opSaved: 'Operation saved',
    agentSubAgentkey: 'Agent key',
    agentSubCursor: 'Pointer',
    keyTitle: 'The agent\'s API key',
    keyHint: 'One key, with its creation date. It unlocks the same routes as the platform token, so only give it to your own tools.',
    keyShow: 'Show the key',
    keyHide: 'Hide',
    keyCopy: 'Copy the key',
    keyRotate: 'Generate a new key',
    keyRotated: 'New key generated; the old one stopped working',
    keyRevealAudited: 'The key was shown (this is audited)',
    keyCreatedAt: 'Created',
    keyLastUsed: 'Last used',
    keyLength: 'Length',
    keyEnabledLabel: 'Enabled',
    keyDisabledLabel: 'Disabled',
    keyEnabled: 'The key is enabled',
    keyDisabled: 'The key is disabled',
    keyCut: 'Cut access',
    keyCutDone: 'Access was cut',
    keyCutHint: 'Cut immediately and persisted in the database, so it stays cut after a restart. The platform token always keeps working.',
    keyNotSet: 'No key yet',
    keyWays: 'Three ways to send the key',
    keyWaysHint: 'Pick whichever your tool accepts; all three carry the same key. The third one is for tools that cannot send custom headers (like ChatGPT on the web).',
    keyCutNote: 'When access is cut, every one of these paths is refused with HTTP 403.',
    keyEndpoints: 'Endpoints the agent may call',
    keyEndpointsHint: 'These are absolute URLs (public domain included). Use copy all to hand them to another AI in one go.',
    keyCopyAll: 'Copy all',
    keyMethod: 'Method',
    keyPath: 'URL',
    keyWhat: 'What it does',
    keyNoEndpoints: 'The endpoint list is unavailable',
    cursorTitle: 'The mouse pointer',
    cursorHint: 'A yellow, larger-than-usual pointer on the virtual desktop so it is not lost inside noVNC; every click also draws a ripple to show where it landed.',
    cursorEnabled: 'Use the big pointer',
    cursorSize: 'Size (px)',
    cursorColor: 'Colour',
    cursorOutline: 'Outline colour',
    cursorRipple: 'Ripple on click',
    cursorSave: 'Save and apply',
    cursorSaved: 'Saved and applied to the browser',
    cursorSavedNoBrowser: 'Saved; the browser was not reachable, so it applies from the next run',
    cursorPreview: 'Approximate preview of size and colour',
    cursorMove: 'Move the pointer to see it',
    cursorMoved: 'The pointer moved',
    cursorMoveHint: 'See the ripple with a real click from the pages tab; it opens right there.',
    clickNow: 'Click it now',
    clickNowHint: 'Moves the real pointer to these coordinates and clicks, without adding a step. Refused while a run owns the mouse (409).',
    elementsClickHint: 'Click it now clicks the desktop right away; Click adds a step to the stages.',
    copyShotLink: 'Copy the image link',
    noShotForPage: 'No screenshot was stored for this page',
    refreshShot: 'Fresh screenshot',
    shotTaken: 'Screenshot taken',
    tgChannels: 'Channels (bot and personal account)',
    tgChannelsHint: 'The agent can use both channels. Both means the message goes through whichever is healthy, so one broken channel cannot lose it.',
    tgModeOff: 'Off',
    tgModeBot: 'Bot only',
    tgModeAccount: 'Personal account only',
    tgModeBoth: 'Both',
    tgChannel: 'Channel',
    tgState: 'State',
    tgNeeds: 'What it needs',
    tgSession: 'Session file',
    tgChannelBot: 'Bot',
    tgChannelAccount: 'Personal account',
    tgChannelAuto: 'Automatic (follows the setting above)',
    tgRouteFollow: 'Automatic (follows the global mode)',
    tgReady: 'ready',
    tgNotReady: 'not ready',
    tgRouting: 'Message routing',
    tgRoutingHint: 'Choose which channel carries each kind of message; automatic follows the global mode above.',
    tgPurposeHandoff: 'Handoff to a human',
    tgPurposeCaptcha: 'CAPTCHA',
    tgPurposeJobs: 'Scheduled jobs',
    tgPurposeManual: 'Manual / agent message',
    tgBotHint: 'Get the token from @BotFather. A stored value is never shown; leaving the box empty means keep it.',
    tgAccountHint: 'api_id and api_hash from my.telegram.org, then send code and finish login once. The session file lives on /data and survives restarts.',
    tgAccountOff: 'Set the mode to personal account only or both, then save, to enable this section.',
    tgCodeSent: 'Code sent; put it in the box and press finish login',
    tgTest: 'Test message',
    tgTestHint: 'A test message needs a target: pick or discover one in section 5 below first.',
    tgTestTarget: 'Target',
    tgTestChannel: 'Channel',
    tgTestText: 'Text',
    tgTestTextHint: 'Leave empty to send the default text',
    tgTestSend: 'Send it',
    tgTestSent: 'Sent',
    tgTestNeedTarget: 'Pick a target first',
    tgTestNoTargets: 'No saved targets (discover one first)',
    tgSendTitle: 'Any message (exactly what the agent can do)',
    tgSendHint: 'This calls the very same /agent/telegram/send the agent uses: any text, one target or all of them, a channel, and the message\'s purpose. Change the purpose to try the routing in section 2.',
    tgSendTextHint: 'The message text',
    tgSendTarget: 'Target',
    tgSendEveryone: 'Every saved target',
    tgSendPurpose: 'Purpose',
    tgSendButton: 'Send the message',
    tgSendNeedText: 'The message text is empty',
    agentNotAttached: 'This deployment was built without the coworker agent.',
    agentSave: 'Save', agentRefresh: '⟳ Refresh', agentDelete: 'Delete',
    agentTest: 'Send a test message', agentRun: 'Run', agentApprove: 'Approve',
    agentReject: 'Reject', agentKill: 'Stop now', agentSend: 'Send',
    agentAdd: 'Add', agentNew: 'New', agentConfirmDelete: 'Delete this?',
    agentSaved: 'Saved', agentHint: 'Note', agentEmpty: 'Nothing here yet',
    agentKillSwitch: 'Kill switch', agentKillSwitchHint: 'On = the agent does nothing at all (chat, jobs, scripts, captcha clicks).',
    agentProvider: 'Answer source', agentChatProvider: 'Chat site', agentModel: 'Model',
    agentBaseUrl: 'API base URL', agentApiKey: 'API key', agentAiStatus: 'Agent browser',
    agentAiOpen: 'Open the chat site', agentAiShot: 'Screenshot the agent display',
    agentAiHint: 'The agent-browser mode needs no API key: the agent opens a chat website in a separate browser and reads the answer off the page. Slower and more brittle.',
    agentChatAsk: 'Ask the agent', agentChatPlaceholder: 'What should be done on this page?',
    agentChatDirect: 'Ask directly', agentChatCopy: 'Copy the prompt',
    agentJobName: 'Name', agentJobKind: 'Schedule kind', agentJobSchedule: 'Schedule',
    agentJobTarget: 'Target', agentJobAction: 'Action', agentJobNext: 'Next run',
    agentJobLast: 'Last result', agentJobNow: 'Now',
    agentJobQueued: 'The run started in the background; its result appears in the "last result" column. Refresh this table in a moment.',
    agentJobHint: 'at = once at a moment · every = an interval (5m, 2h) · cron = five fields ("30 7 * * 6").',
    agentScriptCode: 'Code', agentScriptLang: 'Language', agentScriptStatus: 'Status',
    agentScriptOutput: 'Output', agentScriptGate: 'A script never runs until you approve it. Editing the code cancels the previous approval.',
    agentTgMode: 'Send mode', agentTgBotToken: 'Bot token', agentTgTargets: 'Targets',
    agentTgDiscover: 'Find chats', agentTgPhone: 'Phone number', agentTgCode: 'Login code',
    agentTgLogin: 'Request a code', agentTgLoginFinish: 'Sign in',
    agentTgAccountWarn: 'Account mode signs in as you through Telethon. Its session file grants full access to your account and Telegram may restrict accounts used for automation.',
    agentTgHint: 'To find a target: message the bot first, then press "Find chats".',
    agentCaptchaStrategies: 'Strategies', agentCaptchaAutoClick: 'Click automatically',
    agentCaptchaMaxAttempts: 'Max attempts', agentCaptchaMinConfidence: 'Min confidence',
    agentCaptchaExtension: 'Ready-made solver', agentCaptchaSolve: 'Analyse the current captcha',
    agentCaptchaHistory: 'History', agentCaptchaProposal: 'Proposal',
    agentCaptchaHint: 'By default it proposes and does not click. Turn on automatic clicks only after watching it work. Behavioural challenges (the reCAPTCHA checkbox, hCaptcha) cannot be solved by vision and go to a human.',
    agentQuery: 'SQL query', agentQueryRun: 'Run', agentNotes: 'Notes',
    agentAudit: 'Audit log', agentQueryHint: 'SELECT only; writing goes through the CRUD endpoints.',
    shotsEmpty: 'No screenshots yet', shotsCount: 'screenshots',
    openShot: 'Open the image', deleteShotConfirm: 'Delete this screenshot record?',
    textsEmpty: 'No text saved yet', textsCount: 'texts',
    noteHint: 'Write text you want to keep, then press Save note',
    noteEmpty: 'You did not write anything', saveNote: 'Save note', saved: 'Saved',
    deleteTextConfirm: 'Delete this text?', refresh: 'Refresh', copy: 'Copy',
    flowName: 'Flow name', addStep: 'Add step', stepsEmpty: 'No steps yet.',
    run: 'Run', pause: 'Pause', resume: 'Resume', stop: 'Stop', stepOver: 'Step',
    save: 'Save', saveAs: 'Save as', load: 'Load', delete: 'Delete', duplicate: 'Duplicate',
    settings: 'Settings', defaultDelay: 'Default delay (ms)', repeat: 'Repeat',
    screenshotEach: 'Screenshot after each step', stopOnError: 'Stop on error',
    recordOn: 'Recording', recordOff: 'Record',
    recordHint: 'Click on the page to capture a step. Clicks do not reach the browser.',
    keyboardHint: 'Also capture keys', clear: 'Clear',
    detect: 'Detect page', detecting: 'Detecting…',
    copyDetected: 'Copy detected info', shotLink: 'Public screenshot link',
    aiRequest: 'Your request for the AI',
    aiRequestHint: 'e.g. click the first result, type its title into the search box and press Enter. This text goes into the prompt.',
    aiRequestNone: 'No page detected yet; press Detect first.',
    openVnc: 'Browser view', openPanel: 'Full panel',
    copyReport: 'Copy run report', nowRunning: 'Running:',
    library: 'Saved flows', libraryEmpty: 'Nothing saved yet',
    stepsWord: 'steps', deleteSavedConfirm: 'Delete "{n}" from the library?',
    screenshotBefore: 'Screenshot before each step',
    screenshotOnError: 'Screenshot on error',
    aiNotes: "What the AI said (summary and questions)",
    clearAll: 'Clear all', clearedAll: 'All steps cleared',
    nothingToClear: 'There is nothing to clear',
    clearAllConfirm: 'Delete all {n} steps? This cannot be undone.',
    stateRunning: 'running', statePending: 'not run yet',
    stateOk: 'ok', stateError: 'failed', stateSkipped: 'skipped',
    stateIgnored: 'error ignored',
    recordStandalone: 'Recording clicks needs the browser view, because you have to click '
      + 'on the page itself. Open the browser view to record; everything else here '
      + '(build, edit, run, save, detect, AI) works and drives the same browser.',
    pagesEmpty: 'No page detected yet.',
    elements: 'Elements', text: 'Page text', screenshot: 'Screenshot',
    copyGuide: 'Copy general guide', copyPrompt: 'Copy full prompt', importBtn: 'Import AI output',
    importHint: 'Paste the JSON the AI returned here.',
    tokenBarTitle: 'Enter the access token to enable the panel',
    tokenBarWrong: 'Wrong token; enter the AUTOMATION_TOKEN printed in Colab',
    tokenSubmit: 'Save',
    needTokenShort: 'Token required',
    token: 'Access token', tokenHint: 'The AUTOMATION_TOKEN printed in Colab.',
    connected: 'Connected', needToken: 'Token required',
    logEmpty: 'Nothing logged yet.',
    typingMode: 'Default typing speed',
    typingModeHelp: 'For input reliability only; no randomized typing or CAPTCHA bypass. Use paste for Unicode and long text.',
    typingInherit: 'Flow default', typingLow: 'Low · 50 ms between keys',
    typingNormal: 'Normal · 15 ms between keys', typingFast: 'Fast · 0 ms between keys',
    challengeTitle: 'Security check: automation paused',
    challengeBody: 'CAPTCHA or security-verification cues were detected. Review and handle the page yourself; do not send the code or answer to the AI. Continue only after the challenge is cleared and the page is back to normal.',
    manualHandoffTitle: 'Human action needed',
    humanContinue: 'Continue after human review', humanStop: 'Stop flow',
    viewChallengeShot: 'View screenshot at pause', possibleChallenge: 'Possible CAPTCHA / security check',
    challengeSignals: 'Signals', noChallengeDetected: 'No clear CAPTCHA cue was found in this check; detection is not definitive.',
    challengeCopySafety: 'Safety: pause at CAPTCHA or security checks; only the human handles them.',
    confirmTitle: 'Human confirmation needed', approve: 'Approve', refuse: 'Do not run',
    statusIdle: 'Idle', statusRunning: 'Running', statusPaused: 'Paused',
    statusWaiting: 'Waiting', statusDone: 'Done', statusError: 'Error', statusStopped: 'Stopped',
    invalidFlow: 'This flow has problems', errors: 'Problems', close: 'Close',
    elementSelector: 'Selector', elementText: 'Text', elementPos: 'Position', useAsClick: 'Click',
  },
};

export function translate(lang, key) {
  const table = STRINGS[lang] || STRINGS.en;
  return table[key] || STRINGS.en[key] || key;
}
