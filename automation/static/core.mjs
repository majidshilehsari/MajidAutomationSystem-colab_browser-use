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
  { type: 'type', group: 'keyboard', fields: [{ key: 'text', kind: TEXT }] },
  { type: 'paste', group: 'keyboard', fields: [{ key: 'text', kind: AREA }] },
  { type: 'key', group: 'keyboard', fields: [{ key: 'keysText', kind: TEXT, virtual: true }] },
  { type: 'wait', group: 'flow', fields: [{ key: 'ms', kind: INT }] },
  { type: 'wait_for_text', group: 'flow', fields: [
    { key: 'text', kind: TEXT }, { key: 'timeoutMs', kind: INT, optional: true },
    { key: 'absent', kind: 'bool', optional: true }] },
  { type: 'goto_url', group: 'flow', fields: [{ key: 'url', kind: TEXT }] },
  { type: 'focus_window', group: 'flow', fields: [{ key: 'title', kind: TEXT }] },
  { type: 'screenshot', group: 'flow', fields: [{ key: 'name', kind: TEXT, optional: true }] },
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
    tabFlow: 'مراحل', tabRecord: 'ضبط', tabPages: 'صفحه‌ها', tabAi: 'هوش مصنوعی', tabLog: 'گزارش',
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
    confirmTitle: 'تأیید انسانی لازم است', approve: 'تأیید می‌کنم', refuse: 'اجرا نشود',
    statusIdle: 'آماده', statusRunning: 'در حال اجرا', statusPaused: 'متوقف موقت',
    statusWaiting: 'منتظر تأیید', statusDone: 'تمام شد', statusError: 'خطا', statusStopped: 'قطع شد',
    invalidFlow: 'این مجموعه ایراد دارد', errors: 'ایرادها', close: 'بستن',
    elementSelector: 'انتخاب‌گر', elementText: 'متن', elementPos: 'مختصات', useAsClick: 'کلیک',
  },
  en: {
    title: 'Automation',
    tabFlow: 'Flow', tabRecord: 'Record', tabPages: 'Pages', tabAi: 'AI', tabLog: 'Log',
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
