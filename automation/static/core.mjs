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

// Round 26: ONE step language. The panel edits python steps only; whatever
// arrives written in the old JSON vocabulary is translated by the server
// (PUT /flows, PUT /agent/operations and POST /convert all run the same
// converter the engine uses), so the old field schemas are gone from here.
export const STEP_DEFS = [
  { type: 'python', group: 'code', fields: [
    { key: 'code', kind: AREA, rows: 10 },
    { key: 'timeout', kind: INT, optional: true }] },
];

export const STEP_DEFAULTS = {
  python: { code: '' },
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
  if (step && step.type === 'python') {
    const line = String(step.code || '').split('\n')
      .map((l) => l.trim()).filter((l) => l && !l.startsWith('#'))[0];
    return line ? truncate(line, 44) : 'پایتون';
  }
  return String((step && step.type) || '');
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
  (flow.steps || []).forEach((step, index) => {
    const where = `steps[${index}] (${(step && step.type) || '?'})`;
    if (!stepDef(step && step.type)) { errors.push(`${where}: unknown step type`); return; }
    if (!String(step.code || '').trim()) {
      errors.push(`${where}: code must not be empty`);
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
    steps: 'مرحله',
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
    chatFlowApplied: 'جریان ایجنت مستقیم به مراحل آمد — مرحله‌ها را یک‌به‌یک بررسی کن.',
    chatNoFlow: 'این پیام جریان قابل استفاده‌ای ندارد',
    chatTools: 'ابزارهای دستیار (کپی پرامپت / وارد کردن JSON)',
    chatToolsHint: 'همان گردش کار «دستیار»: پرامپت کامل را کپی کن، یا بگذار ایجنت مستقیم جواب بدهد و JSON پیشنهادی‌اش را وارد مراحل کن.',
    opHint: 'عملیات = چند مرحله با یک نام؛ یک‌بار می‌سازی و بعد اجرا یا «باز» می‌کنی. «باز کردن در مراحل» مرحله‌ها را داخل تب مراحل می‌ریزد تا ویرایش و اجراشان کنی. دامنهٔ «عمومی» یعنی ایجنت همکار و مغز هر دو استفاده می‌کنند؛ «اختصاصی مغز» فقط ابزار خود مغز است.',
    opNew: 'عملیات تازه',
    opCount: 'عملیات',
    opNone: 'عملیاتی نیست؛ یکی بساز.',
    opNeedsAgent: 'کتابخانه به ایجنت همکار نیاز دارد (در این استقرار ساخته نشده).',
    opName: 'نام عملیات',
    opNameHint: 'مثلاً: باز کردن lmarena',
    opDescription: 'توضیح',
    opTags: 'برچسب‌ها',
    opTagsHint: 'با «،» جدا کن',
    opSteps: 'مرحله‌ها',
    opAddStep: 'افزودن مرحله',
    opFieldsHint: 'هر مرحله یک برنامهٔ پایتون است؛ مثال: check(click(512, 300)). پوشش‌های میز (click، goto، type_text، ask_agent، run_operation و…) در راهنمای مرحله‌ها توضیح آمده‌اند.',
    opScope: 'دامنهٔ عملیات', scopeGeneral: 'عمومی', scopeBrain: 'اختصاصی مغز',
    opScopeHint: '«عمومی» یعنی ایجنت همکار و مغز هر دو می‌توانند استفاده کنند؛ «اختصاصی مغز» فقط ابزار خود مغز است و داخل جریان‌های معمولی اجرا نمی‌شود.',
    opStepLabel: 'توضیح مرحله (فارسی)', opEmptyCode: 'کد یک مرحله خالی است',
    stepNo: 'مرحلهٔ',
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
    opEmpty: 'این عملیات مرحله‌ای ندارد',
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
    clickNowHint: 'همین حالا نشانگر را روی این مختصات می‌برد و کلیک می‌کند، بدون ساخت مرحله. اگر اجرایی مالک ماوس باشد، رد می‌شود (۴۰۹).',
    elementsClickHint: '«کلیک واقعی» همان لحظه روی دسکتاپ کلیک می‌کند؛ «کلیک» یک مرحلهٔ پایتون به مراحل اضافه می‌کند.',
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
    tgTestNoTargets: 'مقصدی نیست — در زیربخش «مقصدها» اضافه کن',
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
    flowName: 'نام عملیات', stepsEmpty: 'هنوز مرحله‌ای ندارید.',
    run: 'اجرا', pause: 'توقف موقت', resume: 'ادامه', stop: 'قطع', stepOver: 'یک مرحله',
    save: 'ذخیره', load: 'بارگذاری', delete: 'حذف', duplicate: 'تکرار',
    settings: 'تنظیمات', defaultDelay: 'مکث پیش‌فرض (میلی‌ثانیه)', repeat: 'تعداد دفعات',
    screenshotEach: 'عکس بعد از هر مرحله', stopOnError: 'توقف در خطا',
    clear: 'پاک کردن',
    detect: 'شناسایی صفحه', detecting: 'در حال شناسایی…',
    copyDetected: 'کپی موارد شناسایی', shotLink: 'لینک عمومی عکس',
    aiRequest: 'درخواست شما از هوش مصنوعی',
    aiRequestHint: 'مثلاً: روی اولین نتیجه کلیک کن، عنوانش را در فیلد جستجو بنویس و Enter بزن. این متن داخل پرامپت می‌رود.',
    aiRequestNone: 'هنوز صفحه‌ای شناسایی نشده؛ اول «شناسایی صفحه» را بزن.',
    openVnc: 'نمای مرورگر', openPanel: 'پنل بزرگ',
    copyReport: 'کپی گزارش اجرا', nowRunning: 'در حال اجرا:',
    library: 'کتابخانه‌ی جریان‌ها', libraryEmpty: 'هنوز جریانی ذخیره نشده',
    stepsWord: 'مرحله', deleteSavedConfirm: 'جریان «{n}» از کتابخانه پاک شود؟',
    screenshotBefore: 'عکس قبل از هر مرحله',
    screenshotOnError: 'عکس هنگام خطا',
    aiNotes: 'گفته‌های هوش مصنوعی (خلاصه و سؤال‌ها)',
    clearAll: 'پاک کردن همه', clearedAll: 'همه‌ی مراحل پاک شد',
    nothingToClear: 'مرحله‌ای برای پاک کردن نیست',
    clearAllConfirm: 'همه‌ی {n} مرحله پاک شود؟ این کار برگشت‌پذیر نیست.',
    stateRunning: 'در حال اجرا', statePending: 'هنوز اجرا نشده',
    stateOk: 'موفق', stateError: 'ناموفق', stateSkipped: 'رد شده',
    stateIgnored: 'خطا نادیده گرفته شد',
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
    tabDb: 'پایگاه داده',
    agentSubBrowser: 'مرورگر',
    tgStatusTitle: 'وضعیت تلگرام', tgModeLine: 'مسیر ایجنت',
    tgCountAll: 'همه', tgCountQueued: 'در صف', tgCountSent: 'رفته',
    tgCountFailed: 'ناموفق', tgCountCancelled: 'لغوشده',
    tgLastUpdate: 'آخرین به‌روزرسانی:', tgStored: 'ذخیره شده',
    tgSettingsTitle: 'تنظیمات ربات و حساب', tgListTitle: 'لیست پیام‌ها',
    tgProgramsTitle: 'برنامه‌ها', tgProgramsCount: 'برنامه',
    tgColStatus: 'وضعیت', tgColWhen: 'زمان (ساعت تهران)', tgColTarget: 'مقصد',
    tgColText: 'متن', tgColSource: 'منبع', tgColId: 'شناسه', tgColTitle: 'عنوان',
    tgColType: 'نوع', tgListEmpty: 'هنوز پیامی فرستاده نشده؛ از جعبهٔ تست یک پیام بفرست تا اینجا ببینی‌اش.',
    tgTehranHint: 'زمان‌ها به ساعت تهران نمایش داده می‌شوند.',
    tgStQueued: '⏳ در صف', tgStSent: '📤 رفت', tgStFailed: '❌ ناموفق',
    tgStPartial: '⚠️ رفت (با خطا)', tgStCancelled: '🚫 لغوشده',
    tgDiagnoseBtn: 'تست کامل مرحله‌به‌مرحله', tgDiagnoseRun: 'در حال بررسی مرحله‌به‌مرحلهٔ مسیر ارسال…',
    tgDiagnoseOk: 'همهٔ مرحله‌ها سالم است؛ پیام واقعاً فرستاده شد.',
    tgDiagnoseBad: 'یک‌جا ایراد دارد؛ مرحلهٔ قرمز را بخوان.',
    tgHelpTitle: 'این بخش چه کار می‌کند؟ (اول این را بخوان)',
    tgHelpMode: 'حالت یعنی پیام از کجا برود: «ربات» ساده‌ترین راه است (فقط یک توکن)، «حساب» با حساب خودِ تو به هر چتی می‌نویسد، «هر دو» یعنی هر کدام وصل بود همان بفرستد. خاموش یعنی هیچ پیامی بیرون نرود.',
    tgHelpChannels: 'ربات فقط به چت‌هایی پیام می‌دهد که اول به ربات «شروع» زده‌اند یا ربات در آن‌ها عضو است. حساب کاربری (جلسهٔ Telethon) به هر چت، گروه یا کانالی که خودت عضویش می‌تواند بنویسد.',
    tgHelpRouting: 'مسیردهی یعنی هر جور پیام (تحویل به انسان، کپچا، نتیجهٔ کارها، دستی) از کدام مسیر برود. خالی بگذاری، از حالت کلی پیروی می‌کند.',
    tgHelpBotTitle: 'توکن ربات را از کجا بیاورم؟ (۱ دقیقه)',
    tgHelpBot: 'در تلگرام به @BotFather پیام بده و /newbot بزن؛ یک نام و یک نام‌کاربری بده؛ توکن را می‌دهد. توکن را اینجا بچسبان و «ذخیره» بزن. بعد در چتِ ربات دکمهٔ «شروع» بزن تا اجازهٔ نوشتن داشته باشد. برای کانال: ربات را مدیرِ کانال کن.',
    tgHelpAccountTitle: 'ورود با حساب خودم یعنی چه؟',
    tgHelpAccount: 'از my.telegram.org یک api_id و api_hash بگیر (رایگان). شماره‌ات را اینجا بنویس و «درخواست کد» بزن؛ کد به تلگرامت می‌آید؛ کد را وارد کن و «تمام ورود» بزن. جلسه روی volume ذخیره می‌شود و دیگر لازم نیست تکرار کنی.',
    tgHelpTargetsTitle: 'مقصد یعنی چه و چطور پیدایش کنم؟',
    tgHelpTargets: 'مقصد همان چت/گروه/کانالی است که پیام‌ها آنجا می‌روند. با «پیدا کردن چت‌ها» فهرست چت‌هایی که اخیراً با ربات حرف زده‌اند می‌آید؛ با دکمهٔ + اضافه‌اش کن. آی‌دی عددی کانال‌ها معمولاً با -100 شروع می‌شود.',
    tgHelpTest: 'اینجا تست واقعی انجام می‌شود: یک پیام واقعاً به مقصد انتخابی فرستاده می‌شود. دکمهٔ «تست کامل» هم مسیر را مرحله‌به‌مرحله بررسی می‌کند (حالت، اتصال ربات، نشست حساب، مقصد، ارسال واقعی) و دقیقاً می‌گوید کجا ایراد دارد.',
    tgProgEmpty: 'هنوز برنامه‌ای نساخته‌ای؛ پایین همین جعبه بساز.',
    tgProgTitle: 'عنوان', tgProgTitleHint: 'مثلاً یادآوری هفتگی',
    tgProgTextHint: 'متنی که هر بار فرستاده شود…',
    tgProgRepeat: 'تکرار', tgProgDaily: 'هر روز', tgProgHourly: 'هر ساعت', tgProgWeekly: 'هر هفته (جمعه)',
    tgProgTime: 'ساعت', tgProgUntil: 'تا تاریخ', tgProgMax: 'سقف دفعات',
    tgProgMaxHint: 'بی‌سقف', tgProgMake: 'ساختن برنامه',
    tgProgHint: 'متن را بنویس تا خلاصهٔ برنامه اینجا بیاید.',
    tgProgActive: 'فعال', tgProgPaused: 'متوقف', tgProgDefaultName: 'پیام زمان‌بندی‌شده',
    dbStatsTitle: 'دیتابیس ایجنت', dbHint: 'همهٔ حافظهٔ ایجنت (چت، عملیات، تنظیمات، حسابرسی) در یک فایل SQLite روی volume است؛ پشتیبان‌ها هم کنار همان فایل می‌مانند و با redeploy پاک نمی‌شوند.',
    dbFlows: 'جریان', dbBackupCount: 'پشتیبان', dbLastBackup: 'آخرین پشتیبان',
    dbNone: 'هنوز پشتیبانی گرفته نشده.', dbListTitle: 'پشتیبان‌ها',
    dbListEmpty: 'فایلی نیست؛ با دکمهٔ «گرفتن پشتیبان» یکی بساز.',
    dbColName: 'نام', dbColSize: 'اندازه', dbColWhen: 'زمان', dbColActions: 'کارها',
    dbTake: 'گرفتن پشتیبان', dbTakeSecrets: 'پشتیبان + secrets',
    dbTakenOk: 'پشتیبان ساخته شد', dbDownload: 'دانلود', dbDownloadOk: 'دانلود شروع شد',
    dbDelete: 'حذف', dbDeleteConfirm: 'این پشتیبان برای همیشه حذف شود؟',
    dbSendTg: 'فرستادن به تلگرام', dbMissing: 'فایل پشتیبان پیدا نشد',
    dbSecretsWarn: 'این پشتیبان شامل توکن‌ها و کلیدها هم می‌شود. فقط اگر مقصد کاملاً خصوصی است تأیید کن.',
    dbAutoTitle: 'پشتیبان‌گیری خودکار', dbAutoHint: 'یک کار زمان‌بندی‌شده می‌سازد که سر ساعتِ مقرر پشتیبان می‌گیرد، قدیمی‌ها را طبق «تعداد نگه‌داشته» پاک می‌کند و اختیاری به تلگرام می‌فرستد.',
    dbJobOn: 'روشن باشد', dbScheduleKind: 'نوع زمان‌بندی', dbSchedule: 'برنامهٔ زمانی',
    dbKindCron: 'cron (پنج خانه)', dbKindEvery: 'هر N ثانیه', dbKindAt: 'یک زمان مشخص',
    dbCronHint: 'نمونه: «0 3 * * *» یعنی هر شب ساعت ۳. خانه‌ها: دقیقه ساعت روز ماه هفته.',
    dbEveryHint: 'عدد ثانیه بنویس؛ مثلاً 86400 یعنی هر ۲۴ ساعت.',
    dbAtHint: 'زمان یک‌بار مصرف به ثانیهٔ unix؛ معمولاً همان cron بهتر است.',
    dbKeep: 'تعداد نگه‌داشته', dbTarget: 'مقصد تلگرام',
    dbTargetHint: 'آی‌دی چت/کانال یا @نام‌کاربری؛ اگر خالی بماند فرستادن خودکار انجام نمی‌شود.',
    dbNotify: 'بعد از هر پشتیبان خودکار، یک پیام اطلاع بده',
    dbSave: 'ذخیرهٔ تنظیمات پشتیبان‌گیری', dbLastJob: 'آخرین اجرای خودکار',
    dbSendTitle: 'فرستادن پشتیبان به تلگرام', dbSendHint: 'یک پشتیبان را همین حالا به هر چت یا کانالی که می‌خواهی بفرست؛ مقصد را خودت می‌نویسی.',
    dbSendNow: 'فرستادن پشتیبان', dbNewest: 'جدیدترین پشتیبان',
    brTitle: 'مرورگر کروم', brHint: 'ایجنت از پورت دیباگ و فایل History خودِ کروم می‌خواند: تب‌های باز، نسخه، بازدیدها، جست‌وجوها، دانلودها و تب‌هایی که اخیراً بسته شده‌اند.',
    brOffline: 'پورت دیباگ جواب نداد', brProfile: 'پروفایل',
    brTabsTitle: 'تب‌های باز', brTabsOpen: 'تب باز', brTabsNone: 'الان هیچ تبی باز نیست.',
    brColTab: 'تب', brColUrl: 'نشانی', brActivate: 'فعال کردن', brClose: 'بستن',
    brNewUrl: 'نشانی جدید', brOpen: 'باز کردن تب',
    brHistory: 'سابقهٔ کروم', brVisitsCount: 'بازدید', brFilter: 'جست‌وجو در سابقه',
    brFilterHint: 'مثلاً deepseek', brDays: 'بازه', brDaysAll: 'همه', brDays1: '۲۴ ساعت',
    brDays7: '۷ روز', brDays30: '۳۰ روز', brRefreshTabs: 'تازه‌سازی تب‌ها',
    brColWhen: 'زمان', brColPage: 'صفحه', brColVisits: 'بازدیدها', brColHow: 'نحوهٔ ورود',
    brHistNone: 'سابقه‌ای پیدا نشد (شاید کروم هنوز History ننوشته باشد).',
    brDownloads: 'دانلودها', brColFile: 'فایل',
    brClosed: '🔒 تب‌های بسته‌شدهٔ اخیر', brClosedNone: 'موردی پیدا نشد.',
    brClosedHint: 'از تفریقِ «سابقهٔ ۲۴ ساعت اخیر» منهای «تب‌های باز» به دست می‌آید؛ فایل session باینری کروم خوانده نمی‌شود، پس فهرستِ دقیقِ «recently closed» خودِ کروم نیست.',
    agentSubPrompt: 'پرامپت',
    chatToolsOpen: 'رفتن به زیرتب «پرامپت» در ایجنت همکار',
    tgSubStatus: 'وضعیت و تست', tgSubSettings: 'تنظیمات', tgSubTargets: 'مقصدها',
    tgSubMailbox: 'لیست پیام‌ها', tgSubPrograms: 'برنامه‌ها', tgSubWrite: 'نوشتن و فرستادن',
    tgTargetsHint: 'هر مقصد را می‌توانی همین‌جا ویرایش کنی: شناسه، عنوان و نوع. بعد «ذخیرهٔ مقصدها» را بزن تا روی سرور بنشیند.',
    tgTargetAdd: 'افزودن مقصد دستی', tgSaveTargets: 'ذخیرهٔ مقصدها',
    tgAddButton: 'افزودن', tgNeedId: 'شناسهٔ مقصد خالی است؛ اول آن را پر کن',
    tgAlreadyAdded: 'این مقصد قبلاً به فهرست اضافه شده',
    tgResetDraft: 'بازنشانی پیش‌نویس', tgUnsaved: 'تغییرات ذخیره‌نشده',
    tgTargetsSaved: 'مقصدها روی سرور ذخیره شد',
    tgFoundTitle: 'یافت‌شده‌ها',
    tgFoundNone: 'چتی پیدا نشد؛ اول یک پیام به ربات بفرست تا گفتگو دیده شود',
    tgTargetsEmptyHint: 'مقصدی ذخیره نشده — دستی اضافه کن یا «پیدا کردن چت‌ها» را بزن. ربات فقط گفتگوهایی را می‌بیند که قبلاً به آن پیام داده‌اند.',
    tgModeInstantHint: 'همین‌جا انتخاب کن؛ تغییر فوراً روی سرور ذخیره می‌شود.',
    tgModeSavedNoAccount: 'حالت ذخیره شد؛ ولی نشست حساب شخصی آماده نیست — «هر دو» فقط بعد از ورود به حساب کار می‌کند (زیربخش تنظیمات: api_id و api_hash و شماره و کد). تا آن موقع «فقط ربات» انتخاب بهتری است.',
    tgTypePrivate: 'خصوصی', tgTypeGroup: 'گروه',
    tgTypeSupergroup: 'ابَرگروه', tgTypeChannel: 'کانال',
    tabSettings: 'تنظیمات', themeDark: 'تم تاریک', themeLight: 'تم روشن',
    settingsThemeTitle: 'تم روشن/تاریک',
    settingsThemeHint: 'انتخابت هم در این مرورگر و هم در دیتابیس ذخیره می‌شود؛ هر جای دیگری هم باز کنی، همان تم بالا می‌آید.',
    settingsThemeLight: 'روشن', settingsThemeDark: 'تاریک',
    settingsClipTitle: 'پل کلیپ‌بورد',
    settingsClipHint: 'noVNC فقط کلیدها را می‌برد، نه کلیپ‌بورد را. متن را اینجا بگذار، «بفرست به دسکتاپ» را بزن و داخل دسکتاپ Ctrl+V کن — و برعکس. دکمه‌های «محلی» با اجازهٔ مرورگر، کلیپ‌بورد کامپیوتر خودت را می‌خوانند/می‌نویسند.',
    settingsClipPh: 'متنی که باید بین کامپیوتر تو و دسکتاپ جابه‌جا شود…',
    settingsClipWrite: 'بفرست به دسکتاپ', settingsClipRead: 'از دسکتاپ بخوان',
    settingsClipLocalWrite: 'کپی در کلیپ‌بورد محلی', settingsClipLocalRead: 'از کلیپ‌بورد محلی بخوان',
    settingsClipDone: 'انجام شد',
    settingsClipFail: 'مرورگر اجازه نداد (HTTPS و مجوز کلیپ‌بورد لازم است)',
    settingsDevlogTitle: 'لاگ پیشرفت توسعه',
    settingsDevlogHint: 'پیشرفت‌های برنامه اینجا ثبت می‌شود تا ایجنت همکار همیشه در جریان باشد؛ اولین یادداشت، نامهٔ خودِ ایجنت است.',
    settingsDevlogTitlePh: 'عنوان یادداشت', settingsDevlogBodyPh: 'متن یادداشت…',
    settingsDevlogAdd: 'ثبت یادداشت', settingsDevlogEmpty: 'هنوز یادداشتی نیست.',
    settingsDevlogSaved: 'یادداشت ثبت شد',
    settingsInfoTitle: 'سیستم و دسکتاپ', settingsInfoVersion: 'نسخه',
    settingsInfoViewport: 'اندازهٔ صفحه', settingsInfoServerTime: 'زمان سرور (تهران)',
    settingsInfoDesktopHint: 'ساعت دسکتاپ از DESKTOP_TZ می‌آید (پیش‌فرض Asia/Tehran). با PERSIAN_KEYBOARD=off دسکتاپ کاملاً انگلیسی می‌شود. داخل دسکتاپ Alt+Shift زبان را عوض می‌کند.',
    settingsGoTelegram: 'تنظیمات تلگرام', settingsGoBackup: 'پشتیبان‌گیری', settingsGoKey: 'کلید ایجنت',
    agentSubPages: 'صفحه اختصاصی', agentSubSuggestions: 'پیشنهادات ایجنت',
    pagesTitlePh: 'عنوان صفحه', pagesHtmlPh: '<!doctype html> … هر HTML که می‌خواهی',
    pagesEditor: 'سازندهٔ صفحه',
    pagesHint: 'ایجنت همکار (یا خودت) هر HTML می‌سازد؛ اینجا ذخیره می‌شود، در دیتابیس می‌ماند و پیش‌نمایشش پایین است.',
    pagesSave: 'ذخیرهٔ صفحه', pagesPreview: 'پیش‌نمایش', pagesGrab: 'برداشتن HTML از پاسخ ایجنت',
    pagesNew: 'صفحهٔ جدید', pagesPreviewTitle: 'پیش‌نمایش زنده',
    pagesSandboxHint: 'پیش‌نمایش در iframe جدا (sandbox) اجرا می‌شود: اسکریپت دارد ولی به پنل و کوکی‌هایش دسترسی ندارد.',
    pagesListTitle: 'صفحه‌های ذخیره‌شده', pagesColTitle: 'عنوان', pagesColUpdated: 'آخرین تغییر',
    pagesColAction: 'کارها', pagesOpen: 'باز کردن', pagesDelete: 'حذف',
    pagesDeleteConfirm: 'این صفحه حذف شود؟', pagesEmpty: 'هنوز صفحه‌ای ساخته نشده.',
    pagesUntitled: 'بی‌عنوان', pagesSaved: 'صفحه در دیتابیس ذخیره شد',
    pagesLoaded: 'صفحه در ویرایشگر باز شد', pagesGrabbed: 'HTML از پاسخ ایجنت برداشته شد',
    pagesNoHtml: 'در پاسخ ایجنت HTML پیدا نشد (باید ```html یا <html> داشته باشد)',
    pagesDeleted: 'صفحه حذف شد', keyLastUse: 'آخرین استفادهٔ کلید',
    keyTestConn: 'تست اتصال با کلید', keyTestOk: 'اتصال با کلید ایجنت برقرار است',
    keyTestFail: 'اتصال با کلید ایجنت برقرار نشد',
    suggHint: 'ایجنت پیشنهاد می‌دهد، انسان تصمیم می‌گیرد: خواندن و پیش‌نویس آزاد است، ولی تأیید/اعمال/بازگردانی/بایگانی فقط با رمز اپراتور انجام می‌شود و هیچ پیشنهادی خودکار اعمال نمی‌شود.',
    suggAllStatuses: 'همهٔ وضعیت‌ها', suggAllKinds: 'همهٔ انواع', suggAllRisks: 'همهٔ ریسک‌ها',
    suggSearchPh: 'جست‌وجو در عنوان/مشکل/پیشنهاد…', suggNew: 'پیشنهاد جدید',
    suggFormHint: 'قبل و بعد را JSON بگذار تا diff خوانا شود؛ برای نوع «تنظیمات» after_state باید {key, value} باشد تا اعمال و بازگردانی خودکار کار کند.',
    suggCreate: 'ثبت پیشنهاد', suggCreated: 'پیشنهاد ثبت شد', suggListTitle: 'پیشنهادها',
    suggColTitle: 'عنوان', suggColKind: 'نوع', suggColRisk: 'ریسک', suggColStatus: 'وضعیت',
    suggColUpdated: 'آخرین تغییر', suggDetail: 'جزئیات', suggBack: 'بازگشت به فهرست',
    suggProblem: 'مشکل یا نیاز', suggProposal: 'تغییر پیشنهادی', suggReason: 'دلیل پیشنهاد',
    suggEvidence: 'شواهد / وضعیت فعلی', suggImpact: 'اثر احتمالی',
    suggBefore: 'نسخهٔ قبل', suggAfter: 'نسخهٔ بعد',
    suggBeforeState: 'نسخهٔ قبل (before_state)', suggAfterState: 'نسخهٔ بعد (after_state)',
    suggSection: 'بخش هدف', suggSectionPh: 'مثلاً تلگرام، پشتیبان، UI…',
    suggTitlePh: 'عنوان کوتاه پیشنهاد', suggKind: 'نوع پیشنهاد', suggRisk: 'سطح ریسک',
    suggStatus: 'وضعیت', suggApplyResult: 'نتیجهٔ اعمال', suggRollbackNote: 'یادداشت بازگردانی',
    suggApprove: 'تأیید', suggReject: 'رد', suggRequestChanges: 'درخواست اصلاح',
    suggApply: 'اعمال', suggApplyConfirm: 'پیشنهاد اعمال شود؟ فقط انسان می‌تواند این را بزند.',
    suggRollback: 'بازگردانی', suggArchive: 'بایگانی',
    suggArchiveConfirm: 'پیشنهاد بایگانی شود؟ (حذف واقعی نمی‌شود)',
    suggEdit: 'ویرایش', suggSaveEdit: 'ذخیرهٔ ویرایش', suggSavedEdit: 'ویرایش ذخیره شد',
    suggEmpty: 'پیشنهادی نیست — ایجنت یا خودت یکی ثبت کنید.', suggDecided: 'تصمیم ثبت شد',
    suggApplied: 'اعمال شد', suggRolledBack: 'بازگردانده شد', suggArchived: 'بایگانی شد',
    suggOperatorOnly: 'تأیید، اعمال، بازگردانی و بایگانی فقط با رمز اپراتور ممکن است؛ کلید ایجنت ۴۰۳ می‌گیرد و هر استفاده در audit ثبت می‌شود.',
    suggKindOperation: 'ویرایش عملیات', suggKindFlow: 'ویرایش flow', suggKindSettings: 'تغییر تنظیمات',
    suggKindUi: 'اصلاح رابط کاربری', suggKindDb: 'اصلاح دیتابیس', suggKindBug: 'رفع خطا',
    suggKindSecurity: 'بهبود امنیت', suggKindFeature: 'افزودن قابلیت',
    suggStatusDraft: 'پیش‌نویس', suggStatusPending: 'در انتظار بررسی', suggStatusApproved: 'تأییدشده',
    suggStatusRejected: 'ردشده', suggStatusApplied: 'اعمال‌شده', suggStatusFailed: 'ناموفق',
    suggStatusArchived: 'بایگانی',
    suggRiskLow: 'کم', suggRiskMedium: 'متوسط', suggRiskHigh: 'زیاد',
    dbWinTitle: 'پنجرهٔ فقط‌خواندنی دیتابیس',
    dbWinHint: 'کل دیتابیس با یک اتصال read-only باز می‌شود: فقط SELECT/WITH/PRAGMA/EXPLAIN، حداکثر ۵۰۰ ردیف، و هر ستون یا ردیف حساس ماسک می‌شود. برای عیب‌یابی و مقایسهٔ داده‌ها.',
    dbWinSchema: 'نقشهٔ دیتابیس', dbWinTables: 'جدول', dbWinRows: 'رکورد',
    dbWinCols: 'ستون', dbWinSensitive: 'ستون‌های حساس',
    dbWinRun: 'کوئری فقط‌خواندنی', dbWinPh: 'SELECT … — فقط خواندن',
    dbWinMaskedNote: 'ماسک‌شده (محرمانه)', dbWinTruncated: 'بیش از ۵۰۰ ردیف؛ نتیجه بریده شد',
    openDesktop: 'نمای دسکتاپ',
    flowLoadClipBrowser: 'بارگذاری از کلیپ‌بورد مرورگر',
    flowLoadClipDesktop: 'بارگذاری از کلیپ‌بورد دسکتاپ',
    flowClipDenied: 'مرورگر اجازهٔ خواندن کلیپ‌بورد را نداد؛ با Ctrl+V در پنجرهٔ باز‌شده بچسبانید',
    flowPasteTitle: 'چسباندن از کلیپ‌بورد مرورگر',
    flowPasteHint: 'در کادر زیر Ctrl+V بزنید؛ چسباندنِ دستی هیچ اجازه‌ای از مرورگر نمی‌خواهد و همیشه کار می‌کند.',
    flowPastePh: 'JSON جریان را اینجا بچسبانید…',
    flowPasteImport: 'وارد کردن',
    flowClipDesktopEmpty: 'کلیپ‌بورد دسکتاپ خالی است',
    flowSettingsGear: 'تنظیمات جریان', flowErrorOp: 'عملیات پروتکل خطا (نام یا شناسه)',
    flowRetryAfterProtocol: 'بعد از پروتکل موفق، مرحله یک بار دوباره امتحان شود',
    stepNoteTitle: 'توضیح مرحله', stepNoteHint: 'همان چیزی است که وقتی نشانگر روی مرحله می‌رود هم دیده می‌شود.',
    stepDocWrite: '✨ با ایجنت بنویس', stepDocWorking: 'ایجنت دارد می‌نویسد…',
    stepDocSaved: 'توضیح مرحله نوشته و ذخیره شد',
    brainSubInteract: 'تعامل با مغز', brainSubSettings: 'تنظیمات مغز',
    brainStartBig: '▶ شروع مغز', brainStopBig: '■ توقف مغز',
    brainIsOn: 'مغز بیدار است و میز را می‌چرخاند', brainIsOff: 'مغز خاموش است؛ هیچ‌چیز خودکار اجرا نمی‌شود',
    brainChatPlaceholder: 'به مغز بگو… («اجرا: …»، «توقف»، «یادت باشه …»، «حافظه»، «وضعیت»، «کمک»)',
    brainSend: 'بفرست', brainNoMessages: 'هنوز پیامی نیست؛ اولین جمله را تو بگو.',
    brainMemoryTitle: 'حافظهٔ مغز', brainMemoryEmpty: 'حافظه خالی است.',
    brainInteractHint: 'اینجا مستقیم با مغز حرف بزن: دستور بده، سوال بپرس، یادداشت بسپار. پاسخ‌ها قاعده‌محور و صادق‌اند.',
    tabOps: 'مدیریت عملیات',
    tabBrain: 'مغز',
    brainTitle: 'مغزِ بیدار',
    brainHint: 'مغز نمی‌گذارد میز بیکار بماند: بعد از هر اجرا، عملیات‌های همیشه‌مجاز را نوبتی اجرا می‌کند و برای بقیه پیشنهاد می‌سازد تا شما تصمیم بگیرید.',
    brainEnabled: 'مغز روشن باشد',
    brainAlive: 'حلقهٔ مغز زنده است', brainStopped: 'حلقهٔ مغز خاموش است',
    brainBusy: 'میز درگیر اجراست', brainIdle: 'میز آزاد است',
    brainSuggestion: 'پیشنهادِ جاری', brainNoSuggestion: 'پیشنهادی در انتظار تصمیم نیست.',
    brainRunIt: 'اجرا کن', brainReject: 'رد کن',
    brainOps: 'عملیات‌ها از نگاه مغز', brainAuto: 'همیشه مجاز', brainAsk: 'با پرسش',
    brainPolicy: 'سیاست‌های مغز و گزارش‌گیری',
    brainRunGap: 'فاصلهٔ بین اجراها (ثانیه)',
    brainCooldown: 'خنک‌شدن هر عملیات (دقیقه)',
    brainTtl: 'عمر پیشنهاد (دقیقه)',
    brainShotKeep: 'نگهداری اسکرین‌شات‌ها (روز)',
    brainErrKeep: 'نگهداری اسکرین‌شات خطا (روز)',
    brainForceShots: 'اسکرین‌شات قبل/بعد هر مرحله اجباری باشد',
    brainNotify: 'رویدادهای مغز به تلگرام اعلام شود',
    brainLog: 'گزارش‌های مغز',
    brainOn: 'مغز روشن شد', brainOff: 'مغز خاموش شد',
    brainStarted: 'پیشنهاد اجرا شد', brainRejected: 'پیشنهاد رد شد',
    opPermAuto: 'همیشه مجاز', opPermAsk: 'با پرسش',
    opPermHint: 'مغز این عملیات را خودش اجرا کند یا اول از شما بپرسد؟',
    opsSubRunning: 'در حال اجرا', opsSubQueue: 'صف و زمان‌بندی', opsSubDone: 'انجام‌شده',
    opsColRun: 'اجرا', opsColStatus: 'وضعیت', opsColStep: 'مرحله', opsColMotion: 'حرکت',
    opsColTime: 'زمان', opsColName: 'نام', opsColSchedule: 'زمان‌بندی',
    opsColNext: 'اجرای بعدی', opsColLast: 'آخرین وضعیت', opsColStart: 'شروع',
    opsColEnd: 'پایان', opsColSteps: 'مرحله‌ها',
    opsJobRunning: 'job زمان‌بند در حال اجرا', opsNoJob: 'هیچ job زمان‌بندی‌شده‌ای در حال اجرا نیست',
    opsLastEvents: 'آخرین رویدادها', opsScheduleTitle: 'زمان‌بندی عملیات جدید',
    opsScheduleHint: 'یک جریان از کتابخانه برگزین و زمان را بنویس: لحظهٔ دقیق (at)، بازه (every مثل 5m) یا cron پنج خانه‌ای.',
    opsSchedule: 'زمان‌بندی', opsRunNow: 'اجرای فوری', opsToggle: 'فعال یا متوقف',
    opsStarted: 'اجرای عملیات آغاز شد', opsNoJobs: 'چیزی در صف نیست',
    opsNoRuns: 'هنوز اجرای تمام‌شده‌ای ثبت نشده',
    flowsLibTitle: 'کتابخانهٔ جریان‌ها',
    flowsLibHint: 'جریان‌های ذخیره‌شده این‌جا زندگی می‌کنند؛ ویرایش (✎)، اجرا (▶)، بارگذاری (⤓) و حذف (🗑) - ایجنت همکار هم همهٔ این‌ها را مستقیم از API می‌تواند.',
    flowEditTitle: 'ویرایش جریان ذخیره‌شده', flowDesc: 'توضیح', flowDeleted: 'جریان پاک شد',
    copied: 'کپی شد',
    clipSyncTitle: 'پل کلیپبورد زنده',
    clipSyncHint: 'هر ۱٫۵ ثانیه: آنچه در ویندوز کپی می‌کنی به کلیپبورد مرورگر سرور می‌رسد و آنچه آن‌جا کپی شود به این‌جا. بار اول مرورگر یک‌بار اجازه می‌خواهد.',
    clipSyncOn: 'روشن', clipSyncOff: 'خام',
    supaTitle: 'پشتیبان‌گیری ابری Supabase',
    supaHint: 'داده‌ها (دیتابیس ایجنت + کتابخانه فلوها) هر چند دقیقه یک‌بار به باکت خصوصی Supabase می‌روند و اول هر نشستِ تازه خودکار برمی‌گردند. کلید فقط یک‌بار وارد می‌شود و بعد ماسک‌شده نمایش داده می‌شود.',
    supaOnboardTitle: 'اتصال به Supabase',
    supaOnboardHint: 'داده‌های این نشست خالی است. اتصال Supabase را بچسبان تا تنظیمات ذخیره شود و آخرین snapshot ابری خودکار بازیابی گردد (سرور یک بار درجا دوباره راه می‌افتد). یک متن کافی است: کلید service_roleِ JWT به‌تنهایی (نشانی پروژه از دلش درمی‌آید) یا «نشانی کلید» با فاصله.',
    supaConnLabel: 'متن اتصال (نشانی و/یا کلید)',
    supaConnPh: 'eyJhbG… یا https://xxxx.supabase.co sb_secret_…',
    supaConnectBtn: 'اتصال و بازیابی',
    supaLater: 'بعداً',
    supaConnected: 'اتصال Supabase ذخیره شد؛ همگام‌سازی دوره‌ای شروع شد',
    supaConnectedRestore: 'اتصال ذخیره و snapshot ابری بازیابی شد',
    supaConnEmpty: 'اول متن اتصال را بچسبان',
    supaHistoryTitle: 'نسخه‌های ابری',
    supaHistoryHint: '۵ نسخهٔ آخر نگه داشته می‌شود؛ هرکدام را می‌توانی برگردانی (سرور بعدش درجا دوباره راه می‌افتد).',
    supaHistoryEmpty: 'هنوز نسخه‌ای در ابر نیست — «پشتیبان‌گیری فوری» را بزن تا اولین نسخه ساخته شود.',
    supaRestoreVersion: 'بازیابی این نسخه',
    supaConfirmVersion: 'این نسخهٔ قدیمی‌تر جایگزین داده‌های فعلی می‌شود و سرور دوباره راه می‌افتد. ادامه بدهم؟',
    dbRestore: 'بازیابی',
    dbConfirmRestore: 'این پشتیبان جایگزین داده‌های فعلی می‌شود و سرور دوباره راه می‌افتد. ادامه بدهم؟',
    dbRestoring: 'بازیابی انجام شد؛ سرور در حال راه‌اندازی مجدد است… صفحه تا چند ثانیهٔ دیگر تازه می‌شود',
    supaConnHint: 'یک متن: کلید service_role (اگر JWT باشد نشانی پروژه از دلش استخراج می‌شود) یا «https://xxxx.supabase.co sb_secret_…». کلید SECRET ذخیره و همیشه ماسک نمایش داده می‌شود.',
    supaSave: 'ذخیرهٔ تنظیمات', supaSaved: 'تنظیمات Supabase ذخیره شد',
    supaBackup: 'پشتیبان‌گیری فوری', supaBackupDone: 'snapshot آپلود شد',
    supaRestore: 'بازیابی + راه‌اندازی مجدد',
    supaConfirmRestore: 'آخرین snapshot جایگزین داده‌های فعلی می‌شود و سرور دوباره راه می‌افتد. ادامه بدهم؟',
    supaRestoring: 'بازیابی انجام شد؛ سرور در حال راه‌اندازی مجدد است… صفحه تا چند ثانیهٔ دیگر تازه می‌شود',
    supaState: 'وضعیت', supaLast: 'آخرین همگام‌سازی', supaRestored: 'آخرین بازیابی',
    formRequired: 'این فیلد لازم است',
    secretWarn: 'این مقدار فقط به خزانهٔ حافظه‌ای می‌رود؛ وارد چت، پرامپت، کلیپ‌بورد، اسکرین‌شات یا صفحهٔ ثالث نمی‌شود.',
    holdShow: 'نگه‌دار برای نمایش', clearValue: 'پاک کردن',
    secretScope: 'دامنهٔ مجاز',
    flowClipBad: 'کلیپبورد JSON معتبرِ جریان نداشت', flowClipLoaded: 'جریان کلیپبورد',
    dbSubBackups: 'پشتیبان‌گیری', dbSubShots: 'اسکرین‌شات‌ها', dbSubTexts: 'متون استخراج‌شده',
    dbSubLetters: 'نامه‌ها',
    lettersComposeTitle: 'نامهٔ تازه', lettersUnreadChip: 'نامهٔ نرسیده',
    lettersHint: 'توانایی‌ها یا تغییرات تازهٔ سامانه را برای ایجنت همکار بنویس تا گیج نشود؛ نامه‌های نرسیده در شروع اولین گفت‌وگوی بعدی کامل به او داده می‌شوند و بعد «رسیده» می‌شوند. جهت «ایجنت → من» پاسخ‌های مهم ایجنت را همین‌جا نگه می‌دارد.',
    lettersDirection: 'جهت نامه', lettersDirToAgent: 'من → ایجنت', lettersDirToUser: 'ایجنت → من',
    lettersSubject: 'موضوع (اختیاری)', lettersSubjectPh: 'مثلاً: توانایی تازهٔ عملیات پایتون',
    lettersBodyPh: 'متن نامه…', lettersSend: 'ارسال نامه', lettersNeedBody: 'متن نامه خالی است',
    lettersSent: 'نامه ذخیره شد', lettersListTitle: 'صندوق نامه‌ها',
    lettersEmpty: 'هنوز نامه‌ای نیست.', lettersStatusNew: 'نرسیده', lettersStatusRead: 'رسیده',
    lettersDelete: 'حذف نامه', lettersDeleteConfirm: 'این نامه حذف شود؟',
    lettersSaveReply: 'نامه کن', lettersSavedReply: 'پاسخ به‌عنوان نامهٔ ایجنت ذخیره شد',
    brainStepsTitle: 'گزارش قدم‌های مغز',
    brainStepsHint: 'مغز هر چند دقیقه یک‌بار و در هر اتفاق مهم، قدمش را با زمانِ دقیق گزارش می‌کند؛ ایجنت همکار هم می‌تواند بعداً همین گزارش‌ها را بخواند تا بفهمد در غیابش چه گذشته.',
    brainStepsEmpty: 'هنوز قدمی گزارش نشده — مغز را روشن کن.',
    brainStepsAll: 'گزارش کامل',
    brainReportGap: 'فاصلهٔ گزارش مشاهده (ثانیه)',
    settingsLangTitle: 'زبان پنل', settingsLangHint: 'زبان و تم هر دو این‌جا زندگی می‌کنند؛ هدر فقط یک خط می‌ماند.',
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
    chatFlowApplied: 'the agent\'s flow went straight into the stages - check the steps one by one',
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
    opFieldsHint: 'Each step is a small python program, e.g. check(click(512, 300)). The desk helpers (click, goto, type_text, ask_agent, run_operation, ...) are documented in the step guide.',
    opScope: 'Scope', scopeGeneral: 'General', scopeBrain: 'Brain only',
    opScopeHint: 'General operations serve the coworker agent and the brain alike; brain-only operations are the brain\'s own tools and never run inside ordinary flows.',
    opStepLabel: 'Step description', opEmptyCode: 'A step has empty code',
    stepNo: 'Step',
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
    tgTestNoTargets: 'No targets yet — add one in the targets sub-tab',
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
    flowName: 'Operation name', stepsEmpty: 'No steps yet.',
    run: 'Run', pause: 'Pause', resume: 'Resume', stop: 'Stop', stepOver: 'Step',
    save: 'Save', load: 'Load', delete: 'Delete', duplicate: 'Duplicate',
    settings: 'Settings', defaultDelay: 'Default delay (ms)', repeat: 'Repeat',
    screenshotEach: 'Screenshot after each step', stopOnError: 'Stop on error',
    clear: 'Clear',
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
    tabDb: 'Database',
    agentSubBrowser: 'Browser',
    tgStatusTitle: 'Telegram status', tgModeLine: 'agent route',
    tgCountAll: 'all', tgCountQueued: 'queued', tgCountSent: 'sent',
    tgCountFailed: 'failed', tgCountCancelled: 'cancelled',
    tgLastUpdate: 'last update:', tgStored: 'stored',
    tgSettingsTitle: 'bot and account settings', tgListTitle: 'message list',
    tgProgramsTitle: 'programs', tgProgramsCount: 'programs',
    tgColStatus: 'status', tgColWhen: 'time (Tehran)', tgColTarget: 'target',
    tgColText: 'text', tgColSource: 'source', tgColId: 'id', tgColTitle: 'title',
    tgColType: 'type', tgListEmpty: 'nothing sent yet; use the test box and it will show up here.',
    tgTehranHint: 'times are shown in Tehran time.',
    tgStQueued: 'queued', tgStSent: 'sent', tgStFailed: 'failed',
    tgStPartial: 'sent with errors', tgStCancelled: 'cancelled',
    tgDiagnoseBtn: 'full step-by-step test', tgDiagnoseRun: 'walking the delivery path step by step…',
    tgDiagnoseOk: 'every step is healthy; a real message was delivered.',
    tgDiagnoseBad: 'something is wrong; read the red step.',
    tgHelpTitle: 'what does this pane do? (read this first)',
    tgHelpMode: 'The mode decides where messages travel: "bot" is the easy path (one token), "account" writes as you to any chat, "both" uses whichever is usable. Off means nothing leaves.',
    tgHelpChannels: 'A bot can only write to chats that started it or that it was added to. The personal account (a Telethon session) can write anywhere you are a member.',
    tgHelpRouting: 'Routing picks which transport carries each kind of message (human handoff, captcha, job results, manual). Empty follows the global mode.',
    tgHelpBotTitle: 'where do I get a bot token? (1 minute)',
    tgHelpBot: 'Message @BotFather in Telegram, send /newbot, pick a name and username, paste the token here and save. Then press Start in the bot chat so it may write. For channels: make the bot an admin.',
    tgHelpAccountTitle: 'what does logging in as myself mean?',
    tgHelpAccount: 'Get api_id and api_hash from my.telegram.org (free). Enter your phone, request the code, type the code you receive, finish the login. The session is stored on the volume.',
    tgHelpTargetsTitle: 'what is a target and how do I find one?',
    tgHelpTargets: 'A target is the chat/group/channel messages go to. "Discover chats" lists chats that recently talked to the bot; add one with +. Channel ids usually start with -100.',
    tgHelpTest: 'This is a real test: one message is actually delivered to the chosen target. The full test also walks the path step by step (mode, bot login, account session, target, real send) and names the broken step.',
    tgProgEmpty: 'no program yet; build one below.',
    tgProgTitle: 'title', tgProgTitleHint: 'e.g. weekly reminder',
    tgProgTextHint: 'the text sent each time…',
    tgProgRepeat: 'repeat', tgProgDaily: 'daily', tgProgHourly: 'hourly', tgProgWeekly: 'weekly (Friday)',
    tgProgTime: 'time', tgProgUntil: 'until date', tgProgMax: 'run limit',
    tgProgMaxHint: 'unlimited', tgProgMake: 'create program',
    tgProgHint: 'write the text and its summary appears here.',
    tgProgActive: 'active', tgProgPaused: 'paused', tgProgDefaultName: 'scheduled message',
    dbStatsTitle: 'agent database', dbHint: 'The whole agent memory (chat, operations, settings, audit) is one SQLite file on the volume; backups live next to it and survive redeploys.',
    dbFlows: 'flows', dbBackupCount: 'backups', dbLastBackup: 'last backup',
    dbNone: 'no backup taken yet.', dbListTitle: 'backups',
    dbListEmpty: 'nothing here; press "take a backup" to create one.',
    dbColName: 'name', dbColSize: 'size', dbColWhen: 'when', dbColActions: 'actions',
    dbTake: 'take a backup', dbTakeSecrets: 'backup + secrets',
    dbTakenOk: 'backup created', dbDownload: 'download', dbDownloadOk: 'download started',
    dbDelete: 'delete', dbDeleteConfirm: 'delete this backup for good?',
    dbSendTg: 'send to Telegram', dbMissing: 'the backup file is gone',
    dbSecretsWarn: 'this backup will include tokens and keys. Only confirm for a fully private destination.',
    dbAutoTitle: 'automatic backups', dbAutoHint: 'Creates one scheduled job that takes a backup on time, prunes old ones by "keep", and optionally uploads it to Telegram.',
    dbJobOn: 'enabled', dbScheduleKind: 'schedule kind', dbSchedule: 'schedule',
    dbKindCron: 'cron (five fields)', dbKindEvery: 'every N seconds', dbKindAt: 'one timestamp',
    dbCronHint: 'example: "0 3 * * *" means every night at 03:00. fields: minute hour day month weekday.',
    dbEveryHint: 'seconds; 86400 means every 24 hours.',
    dbAtHint: 'a one-shot unix timestamp; cron is usually the better choice.',
    dbKeep: 'keep', dbTarget: 'Telegram target',
    dbTargetHint: 'a chat/channel id or @username; empty means no automatic upload.',
    dbNotify: 'notify me after each automatic backup',
    dbSave: 'save backup settings', dbLastJob: 'last automatic run',
    dbSendTitle: 'send a backup to Telegram', dbSendHint: 'Upload one backup right now to any chat or channel you type; you choose the destination.',
    dbSendNow: 'send backup', dbNewest: 'newest backup',
    brTitle: 'Chrome browser', brHint: 'The agent reads the debugging port and Chrome\'s own History file: open tabs, version, visits, searches, downloads and recently closed tabs.',
    brOffline: 'the debugging port did not answer', brProfile: 'profile',
    brTabsTitle: 'open tabs', brTabsOpen: 'open tabs', brTabsNone: 'no tab is open right now.',
    brColTab: 'tab', brColUrl: 'url', brActivate: 'activate', brClose: 'close',
    brNewUrl: 'new url', brOpen: 'open tab',
    brHistory: 'Chrome history', brVisitsCount: 'visits', brFilter: 'search history',
    brFilterHint: 'e.g. deepseek', brDays: 'range', brDaysAll: 'all', brDays1: '24 hours',
    brDays7: '7 days', brDays30: '30 days', brRefreshTabs: 'refresh tabs',
    brColWhen: 'when', brColPage: 'page', brColVisits: 'visits', brColHow: 'how',
    brHistNone: 'no history found (Chrome may not have written one yet).',
    brDownloads: 'downloads', brColFile: 'file',
    brClosed: 'recently closed tabs', brClosedNone: 'none found.',
    brClosedHint: 'derived from the last 24 hours of history minus the open tabs; Chrome\'s binary session file is not parsed, so this is not its exact "recently closed" list.',
    agentSubPrompt: 'Prompt',
    chatToolsOpen: 'open the Prompt sub-tab in the coworker agent',
    tgSubStatus: 'status & test', tgSubSettings: 'settings', tgSubTargets: 'targets',
    tgSubMailbox: 'message list', tgSubPrograms: 'programs', tgSubWrite: 'write & send',
    tgTargetsHint: 'every target is editable right here: id, title and type. Press "save targets" to persist them on the server.',
    tgTargetAdd: 'add a target by hand', tgSaveTargets: 'save targets',
    tgAddButton: 'add', tgNeedId: 'The target id is empty; fill it in first',
    tgAlreadyAdded: 'This target is already in the list',
    tgResetDraft: 'Reset draft', tgUnsaved: 'Unsaved changes',
    tgTargetsSaved: 'Targets saved on the server',
    tgFoundTitle: 'Discovered chats',
    tgFoundNone: 'No chats found; message the bot once so the chat becomes visible',
    tgTargetsEmptyHint: 'No targets saved yet — add one by hand or press "Find chats". A bot only sees chats that messaged it first.',
    tgModeInstantHint: 'Pick here; the change is saved on the server immediately.',
    tgModeSavedNoAccount: 'Mode saved; but the personal-account session is not ready — "both" only works after logging in (settings sub-tab: api_id, api_hash, phone, code). Until then "bot only" is the better pick.',
    tgTypePrivate: 'private', tgTypeGroup: 'group',
    tgTypeSupergroup: 'supergroup', tgTypeChannel: 'channel',
    tabSettings: 'Settings', themeDark: 'dark theme', themeLight: 'light theme',
    settingsThemeTitle: 'light/dark theme',
    settingsThemeHint: 'your choice is kept in this browser and in the database; open the panel anywhere and the same theme comes up.',
    settingsThemeLight: 'light', settingsThemeDark: 'dark',
    settingsClipTitle: 'clipboard bridge',
    settingsClipHint: 'noVNC carries keys, not clipboards. Put text here, press "send to desktop", then Ctrl+V inside the desktop — and back. The "local" buttons read/write your own computer clipboard with the browser permission.',
    settingsClipPh: 'text to move between your computer and the desktop…',
    settingsClipWrite: 'send to desktop', settingsClipRead: 'read from desktop',
    settingsClipLocalWrite: 'copy to local clipboard', settingsClipLocalRead: 'read local clipboard',
    settingsClipDone: 'done',
    settingsClipFail: 'the browser refused (HTTPS and clipboard permission required)',
    settingsDevlogTitle: 'development journal',
    settingsDevlogHint: 'progress notes live here so the coworker agent always knows the state of play; the first entry is the agent letter itself.',
    settingsDevlogTitlePh: 'note title', settingsDevlogBodyPh: 'note body…',
    settingsDevlogAdd: 'add note', settingsDevlogEmpty: 'no notes yet.',
    settingsDevlogSaved: 'note saved',
    settingsInfoTitle: 'system & desktop', settingsInfoVersion: 'version',
    settingsInfoViewport: 'viewport', settingsInfoServerTime: 'server time (Tehran)',
    settingsInfoDesktopHint: 'the desktop clock follows DESKTOP_TZ (default Asia/Tehran). PERSIAN_KEYBOARD=off makes the desktop English-only. Alt+Shift switches language inside.',
    settingsGoTelegram: 'telegram settings', settingsGoBackup: 'backups', settingsGoKey: 'agent key',
    agentSubPages: 'dedicated page', agentSubSuggestions: 'agent suggestions',
    pagesTitlePh: 'page title', pagesHtmlPh: '<!doctype html> … any HTML you want',
    pagesEditor: 'page builder',
    pagesHint: 'the coworker agent (or you) builds any HTML; it is saved here, persists in the database, and previews below.',
    pagesSave: 'save page', pagesPreview: 'refresh preview', pagesGrab: 'grab HTML from the agent reply',
    pagesNew: 'new page', pagesPreviewTitle: 'live preview',
    pagesSandboxHint: 'the preview runs in a sandboxed iframe: scripts yes, but no access to the panel or its cookies.',
    pagesListTitle: 'saved pages', pagesColTitle: 'title', pagesColUpdated: 'updated',
    pagesColAction: 'actions', pagesOpen: 'open', pagesDelete: 'delete',
    pagesDeleteConfirm: 'delete this page?', pagesEmpty: 'no pages built yet.',
    pagesUntitled: 'untitled', pagesSaved: 'page saved to the database',
    pagesLoaded: 'page opened in the editor', pagesGrabbed: 'HTML grabbed from the agent reply',
    pagesNoHtml: 'no HTML found in the agent reply (needs a ```html block or <html>)',
    pagesDeleted: 'page deleted', keyLastUse: 'key last used at',
    keyTestConn: 'test the key connection', keyTestOk: 'the agent key opens the api',
    keyTestFail: 'the agent key failed to connect',
    suggHint: 'the agent proposes, the human disposes: reading and drafting are free, but approve/apply/rollback/archive only work with the operator password, and nothing is ever applied automatically.',
    suggAllStatuses: 'all statuses', suggAllKinds: 'all kinds', suggAllRisks: 'all risks',
    suggSearchPh: 'search title/problem/proposal…', suggNew: 'new suggestion',
    suggFormHint: 'put JSON in before/after for a readable diff; for kind "settings" the after_state must be {key, value} so apply and rollback can work.',
    suggCreate: 'create suggestion', suggCreated: 'suggestion created', suggListTitle: 'suggestions',
    suggColTitle: 'title', suggColKind: 'kind', suggColRisk: 'risk', suggColStatus: 'status',
    suggColUpdated: 'updated', suggDetail: 'details', suggBack: 'back to the list',
    suggProblem: 'problem or need', suggProposal: 'proposed change', suggReason: 'reason',
    suggEvidence: 'evidence / current state', suggImpact: 'likely impact',
    suggBefore: 'before', suggAfter: 'after',
    suggBeforeState: 'before (before_state)', suggAfterState: 'after (after_state)',
    suggSection: 'target section', suggSectionPh: 'e.g. telegram, backups, UI…',
    suggTitlePh: 'short suggestion title', suggKind: 'kind', suggRisk: 'risk level',
    suggStatus: 'status', suggApplyResult: 'apply result', suggRollbackNote: 'rollback note',
    suggApprove: 'approve', suggReject: 'reject', suggRequestChanges: 'request changes',
    suggApply: 'apply', suggApplyConfirm: 'apply this suggestion? only a human may press this.',
    suggRollback: 'rollback', suggArchive: 'archive',
    suggArchiveConfirm: 'archive this suggestion? (it is never truly deleted)',
    suggEdit: 'edit', suggSaveEdit: 'save edit', suggSavedEdit: 'edit saved',
    suggEmpty: 'no suggestions — let the agent or yourself create one.', suggDecided: 'decision recorded',
    suggApplied: 'applied', suggRolledBack: 'rolled back', suggArchived: 'archived',
    suggOperatorOnly: 'approve, apply, rollback and archive need the operator token; the agent key gets 403 and every use lands in the audit log.',
    suggKindOperation: 'operation edit', suggKindFlow: 'flow edit', suggKindSettings: 'settings change',
    suggKindUi: 'UI fix', suggKindDb: 'database fix', suggKindBug: 'bug fix',
    suggKindSecurity: 'security improvement', suggKindFeature: 'new capability',
    suggStatusDraft: 'draft', suggStatusPending: 'awaiting review', suggStatusApproved: 'approved',
    suggStatusRejected: 'rejected', suggStatusApplied: 'applied', suggStatusFailed: 'failed',
    suggStatusArchived: 'archived',
    suggRiskLow: 'low', suggRiskMedium: 'medium', suggRiskHigh: 'high',
    dbWinTitle: 'read-only database window',
    dbWinHint: 'the whole database opens over a read-only connection: SELECT/WITH/PRAGMA/EXPLAIN only, 500-row cap, and every sensitive column or row is masked. For diagnosis and data comparison.',
    dbWinSchema: 'database map', dbWinTables: 'table', dbWinRows: 'rows',
    dbWinCols: 'columns', dbWinSensitive: 'sensitive columns',
    dbWinRun: 'run read-only query', dbWinPh: 'SELECT … — read only',
    dbWinMaskedNote: 'masked (secret)', dbWinTruncated: 'more than 500 rows; result truncated',
    openDesktop: 'Desktop view',
    flowLoadClipBrowser: 'load from browser clipboard',
    flowLoadClipDesktop: 'load from desktop clipboard',
    flowClipDenied: 'the browser blocked clipboard reading; press Ctrl+V in the dialog instead',
    flowPasteTitle: 'paste from your clipboard',
    flowPasteHint: 'press Ctrl+V in the box below; a manual paste needs no browser permission and always works.',
    flowPastePh: 'paste the flow JSON here…',
    flowPasteImport: 'import',
    flowClipDesktopEmpty: 'the desktop clipboard is empty',
    flowSettingsGear: 'flow settings', flowErrorOp: 'error-protocol operation (name or id)',
    flowRetryAfterProtocol: 'retry the step once after a successful protocol',
    stepNoteTitle: 'the step description', stepNoteHint: 'also shows as a tooltip on the step row.',
    stepDocWrite: '✨ let the agent write it', stepDocWorking: 'the agent is writing…',
    stepDocSaved: 'the step description was written and saved',
    brainSubInteract: 'talk to the brain', brainSubSettings: 'brain settings',
    brainStartBig: '▶ start the brain', brainStopBig: '■ stop the brain',
    brainIsOn: 'the brain is awake and keeps the desk moving', brainIsOff: 'the brain is off; nothing runs by itself',
    brainChatPlaceholder: 'tell the brain… ("run: ...", "stop", "remember ...", "memory", "status", "help")',
    brainSend: 'send', brainNoMessages: 'no messages yet; say the first word.',
    brainMemoryTitle: 'the brain memory', brainMemoryEmpty: 'the memory is empty.',
    brainInteractHint: 'talk to the brain right here: command, ask, or leave a note. Its answers are rule-based and honest.',
    tabOps: 'Operations',
    tabBrain: 'Brain',
    brainTitle: 'The awake brain',
    brainHint: 'The brain never lets the desk idle: after every run it round-robins the always-allowed operations and suggests the rest so you can decide.',
    brainEnabled: 'keep the brain on',
    brainAlive: 'the brain loop is alive', brainStopped: 'the brain loop is stopped',
    brainBusy: 'the desk is busy', brainIdle: 'the desk is free',
    brainSuggestion: 'the open suggestion', brainNoSuggestion: 'nothing is waiting for a decision.',
    brainRunIt: 'run it', brainReject: 'reject',
    brainOps: 'the operations as the brain sees them', brainAuto: 'always allowed', brainAsk: 'ask first',
    brainPolicy: 'brain and logging policies',
    brainRunGap: 'gap between runs (seconds)',
    brainCooldown: 'per-operation cooldown (minutes)',
    brainTtl: 'suggestion lifetime (minutes)',
    brainShotKeep: 'keep screenshots (days)',
    brainErrKeep: 'keep error screenshots (days)',
    brainForceShots: 'force a before/after screenshot on every step',
    brainNotify: 'announce brain events on Telegram',
    brainLog: 'the brain reports',
    brainOn: 'the brain is on', brainOff: 'the brain is off',
    brainStarted: 'the suggestion is running', brainRejected: 'the suggestion was rejected',
    opPermAuto: 'always allowed', opPermAsk: 'ask first',
    opPermHint: 'may the brain run this operation by itself, or must it ask you first?',
    opsSubRunning: 'running now', opsSubQueue: 'queue & schedule', opsSubDone: 'finished',
    opsColRun: 'run', opsColStatus: 'status', opsColStep: 'step', opsColMotion: 'motion',
    opsColTime: 'time', opsColName: 'name', opsColSchedule: 'schedule',
    opsColNext: 'next run', opsColLast: 'last status', opsColStart: 'started',
    opsColEnd: 'finished', opsColSteps: 'steps',
    opsJobRunning: 'scheduled job running', opsNoJob: 'no scheduled job is running',
    opsLastEvents: 'latest events', opsScheduleTitle: 'schedule a new operation',
    opsScheduleHint: 'pick a library flow and write when: a moment (at), an interval (every, e.g. 5m) or a five-field cron.',
    opsSchedule: 'schedule', opsRunNow: 'run now', opsToggle: 'enable or pause',
    opsStarted: 'the operation started', opsNoJobs: 'nothing is queued',
    opsNoRuns: 'no finished run recorded yet',
    flowsLibTitle: 'saved flows',
    flowsLibHint: 'saved flows live here now; edit (✎), run (▶), load (⤓), delete (🗑) - the coworker agent can do all of it straight through the API too.',
    flowEditTitle: 'edit saved flow', flowDesc: 'description', flowDeleted: 'flow deleted',
    copied: 'copied',
    clipSyncTitle: 'live clipboard bridge',
    clipSyncHint: 'every 1.5s: what you copy on Windows reaches the server browser clipboard, and what is copied there reaches you. the browser asks for permission once.',
    clipSyncOn: 'on', clipSyncOff: 'off',
    supaTitle: 'Supabase cloud backup',
    supaHint: 'Data (the agent database + flow library) is snapshotted to a private Supabase bucket every few minutes and restored automatically at the start of each fresh session. The key is entered once and shown masked afterwards.',
    supaOnboardTitle: 'Connect Supabase',
    supaOnboardHint: 'This session has no data yet. Paste your Supabase connection to save the settings and restore the latest cloud snapshot automatically (the server restarts once, in place). One blob is enough: a service_role JWT alone (the project URL is derived from it) or "url key" separated by a space.',
    supaConnLabel: 'Connection text (url and/or key)',
    supaConnPh: 'eyJhbG… or https://xxxx.supabase.co sb_secret_…',
    supaConnectBtn: 'connect + restore',
    supaLater: 'later',
    supaConnected: 'Supabase connection saved; periodic sync started',
    supaConnectedRestore: 'Connection saved and the cloud snapshot was restored',
    supaConnEmpty: 'Paste the connection text first',
    supaHistoryTitle: 'Cloud versions',
    supaHistoryHint: 'The last 5 versions are kept; any of them can be restored (the server restarts in place afterwards).',
    supaHistoryEmpty: 'No cloud version yet — press "back up now" to create the first one.',
    supaRestoreVersion: 'restore this version',
    supaConfirmVersion: 'This older version will replace the current data and the server will restart. Continue?',
    dbRestore: 'restore',
    dbConfirmRestore: 'This backup will replace the current data and the server will restart. Continue?',
    dbRestoring: 'Restored; the server is restarting… this page reloads in a few seconds',
    supaConnHint: 'One blob: the service_role key (a JWT carries the project URL inside it) or "https://xxxx.supabase.co sb_secret_…". The key is stored as a SECRET and always shown masked.',
    supaSave: 'save settings', supaSaved: 'Supabase settings saved',
    supaBackup: 'back up now', supaBackupDone: 'snapshot uploaded',
    supaRestore: 'restore + restart',
    supaConfirmRestore: 'The latest snapshot will replace the current data and the server will restart. Continue?',
    supaRestoring: 'Restored; the server is restarting… this page reloads in a few seconds',
    supaState: 'state', supaLast: 'last sync', supaRestored: 'last restore',
    formRequired: 'this field is required',
    secretWarn: 'this value goes only into the memory vault; never into chat, prompt, clipboard, screenshot or a third-party page.',
    holdShow: 'hold to show', clearValue: 'clear',
    secretScope: 'allowed scope',
    flowClipBad: 'the clipboard did not hold a valid flow JSON', flowClipLoaded: 'clipboard flow',
    dbSubBackups: 'backups', dbSubShots: 'screenshots', dbSubTexts: 'extracted texts',
    dbSubLetters: 'letters',
    lettersComposeTitle: 'new letter', lettersUnreadChip: 'undelivered',
    lettersHint: 'explain new abilities or changes to the coworker agent so it is never confused; undelivered letters are handed to it at the start of its next conversation and then marked delivered. The agent-to-me direction keeps its important replies here.',
    lettersDirection: 'direction', lettersDirToAgent: 'me → agent', lettersDirToUser: 'agent → me',
    lettersSubject: 'subject (optional)', lettersSubjectPh: 'e.g. the new python operations ability',
    lettersBodyPh: 'letter text…', lettersSend: 'send letter', lettersNeedBody: 'the letter body is empty',
    lettersSent: 'letter saved', lettersListTitle: 'letter box',
    lettersEmpty: 'no letters yet.', lettersStatusNew: 'undelivered', lettersStatusRead: 'delivered',
    lettersDelete: 'delete letter', lettersDeleteConfirm: 'delete this letter?',
    lettersSaveReply: 'save as letter', lettersSavedReply: 'the reply was archived as an agent letter',
    brainStepsTitle: 'the brain\'s step report',
    brainStepsHint: 'the brain reports every important step - and one observation line every few minutes - with exact timestamps; the coworker agent can read the same stream later.',
    brainStepsEmpty: 'nothing reported yet - turn the brain on.',
    brainStepsAll: 'full report',
    brainReportGap: 'observation report gap (seconds)',
    settingsLangTitle: 'panel language', settingsLangHint: 'language and theme both live here; the header stays one line.',
  },
};

export function translate(lang, key) {
  const table = STRINGS[lang] || STRINGS.en;
  return table[key] || STRINGS.en[key] || key;
}
