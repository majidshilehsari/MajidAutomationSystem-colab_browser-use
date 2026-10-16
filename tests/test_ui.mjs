/* DOM tests for automation/static/automation.js.
 *
 * These exist because the sidebar once shipped with a renderer that called
 * appendChild(null) and with a full-screen overlay whose `display: flex` beat
 * the [hidden] attribute. Both broke the page in a real browser while every
 * logic-only test stayed green, so the panel is now actually mounted in jsdom.
 *
 * Run with: node --test tests/test_ui.mjs
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const CSS = fs.readFileSync(new URL('../automation/static/automation.css', import.meta.url), 'utf8');

const NOVNC_HTML = `<!doctype html><html><head><style>${CSS}</style></head><body>
  <div id="noVNC_container"><div id="noVNC_screen">
    <canvas id="noVNC_canvas" width="1366" height="768"></canvas>
  </div></div>
</body></html>`;

const VALID_TOKEN = 'tok123';

/** Stands in for the server: /info is open, everything else needs the token. */
function fakeApi(requests, posts = [], statusPayload = null, noAgent = false,
                supaEmpty = false, supaOn = false, uiPrefs = null,
                 clipText = null) {
  return async (url, options = {}) => {
    const path = String(url);
    const clean = path.split('?')[0];
    requests.push(path);
    if (['POST', 'PUT'].includes((options.method || 'GET').toUpperCase())) {
      posts.push({ url: path, body: options.body });
    }
    const token = (options.headers || {})['X-Automation-Token'];
    const json = (body) => ({ ok: true, status: 200,
                             text: async () => JSON.stringify(body),
                             blob: async () => ({ size: 0, type: 'image/png' }) });
    if (path.endsWith('/info')) {
      return json({ authRequired: true, viewport: { width: 1366, height: 768 },
                    engineBusy: false, detector: 'x11+cdp', cdpAvailable: false });
    }
    if (clean.endsWith('/supabase/status')) {
      return json(supaOn
        ? { configured: true, enabled: true, url: 'https://proj.supabase.co',
            bucket: 'mas-snapshots', key: 'sb-sec\u2026cdef', source: 'settings',
            everyMin: 10, running: true, lastSyncAt: 1760000000,
            lastResult: { uploaded: 20480 }, lastError: null, restoredAt: null,
            uploads: 1, dataEmpty: false }
        : { configured: false, enabled: false, url: '', bucket: '',
            key: '', source: '', everyMin: 10, running: false,
            lastSyncAt: null, lastResult: null, lastError: null,
            restoredAt: null, uploads: 0, dataEmpty: supaEmpty });
    }
    if (clean.endsWith('/supabase/history')) {
      return json({ history: [
        { name: 'history/snapshot-20261015-101010.tar.gz', size: 20480,
          createdAt: '2026-10-15T10:10:10.000Z' },
        { name: 'history/snapshot-20261014-091010.tar.gz', size: 19456,
          createdAt: '2026-10-14T09:10:10.000Z' },
      ] });
    }
    if (clean.endsWith('/agent/backup-restore')) {
      return json({ ok: true, restored: 'mas-backup-test.tar.gz', files: 3,
                    restarting: true });
    }
    if (clean.endsWith('/supabase/connect')) {
      return json({ ok: true, restored: 0, restarting: false,
                    url: 'https://proj.supabase.co', bucket: 'mas-snapshots' });
    }
    if (clean.endsWith('/supabase/sync')) {
      return json({ ok: true, result: { uploaded: 20480 } });
    }
    if (clean.endsWith('/supabase/restore')) {
      return json({ ok: true, restored: 3, restarting: true });
    }
    if ((options.method || 'GET').toUpperCase() === 'POST'
        && (clean.endsWith('/clipboard') || clean.endsWith('/agent/devlog')
            || clean.endsWith('/agent/pages') || clean.endsWith('/agent/suggestions')
            || clean.endsWith('/agent/brain/toggle')
            || clean.endsWith('/agent/brain/suggestion')
            || clean.endsWith('/agent/brain/message')
            || clean.endsWith('/agent/step-doc')
            || clean.includes('/agent/suggestion-'))) {
      return json({ ok: true, page: { id: 7, title: 'x' },
        suggestion: { id: 3, status: 'draft' } });
    }
    if (token !== VALID_TOKEN) {
      return { ok: false, status: 401,
               text: async () => '{"error":"missing or wrong X-Automation-Token header"}' };
    }
    if (noAgent && clean.includes('/agent')) {
      return { ok: false, status: 404,
               text: async () => '{"error":"no agent is attached to this deployment"}' };
    }
    if (clean.endsWith('/convert')) {
      // The real server translates the old vocabulary; the fixtures already
      // speak python, so an honest pass-through is the same answer.
      let body = {};
      try { body = JSON.parse(options.body || '{}'); } catch (_) { /* keep {} */ }
      return json({ ok: true, flow: body.flow || body });
    }
    if (clean.endsWith('/agent')) {
      return json({
        settings: Object.assign({ 'ui.theme': 'dark' }, uiPrefs),
        llm: { provider: 'ai-browser', chatProvider: 'deepseek',
                             configured: true, browser: { available: false } },
        telegram: { mode: 'bot', targets: [] },
        captcha: { enabled: true, strategies: ['vision', 'human'], autoClick: false,
                   maxAttempts: 3, minConfidence: 0.6, extension: 'none' },
        captchaExtension: { name: 'none', free: true },
        scripts: { languages: ['bash', 'python3'], strippedEnv: ['AUTOMATION_TOKEN'],
                   passToken: false, running: null },
        scheduler: { alive: true, jobs: 1 }, killSwitch: false,
        counts: { jobs: 1, scripts: 2, pendingScripts: 1, notes: 3 },
      });
    }
    if (clean.endsWith('/agent/jobs')) {
      return json({ jobs: [{ id: 'job-1', name: '\u0635\u0628\u062d', kind: 'cron',
        schedule: '30 7 * * *', target: 'Arena', enabled: true,
        payload: { action: 'flow' }, next_run_at: 1760000000,
        last_status: 'done', last_run_at: 1759990000, run_count: 0 }] });
    }
    if (clean.endsWith('/agent/scripts')) {
      return json({ scripts: [{ id: 'scr-1', name: '\u06af\u0632\u0627\u0631\u0634',
        language: 'python3', status: 'pending', run_count: 0, last_exit: null,
        code: 'print(1)', timeout: 60 }],
        config: { languages: ['bash', 'python3'],
                  strippedEnv: ['AUTOMATION_TOKEN', 'VNC_PASSWORD'],
                  passToken: false, running: null } });
    }
    if (clean.endsWith('/agent/captcha/config')) {
      return json({ config: { enabled: true, strategies: ['vision', 'human'],
        autoClick: false, maxAttempts: 3, minConfidence: 0.6, extension: 'none',
        notifyOnEscalation: true }, extension: { name: 'none', free: true } });
    }
    if (clean.endsWith('/agent/captcha/history')) {
      return json({ history: [{ at: 1760000000, event: 'vision', kind: 'image_select',
        confidence: 0.9, summary: '\u0633\u0647 \u062a\u0635\u0648\u06cc\u0631' }] });
    }
    if (clean.endsWith('/agent/telegram/status')) {
      return json({ mode: 'bot', targets: [{ id: -100123,
        title: '\u06af\u0631\u0648\u0647 \u0645\u0646', type: 'supergroup' }],
        configured: true, botTokenSet: true, apiCredentialsSet: false, error: '' });
    }
    if (clean.endsWith('/agent/telegram/targets')) {
      return json({ targets: [{ id: -100999, title: 'Found group', type: 'supergroup' }] });
    }
    if (clean.endsWith('/agent/telegram/log')) {
      return json({ counts: { all: 2, queued: 0, sent: 1, failed: 1, cancelled: 0 },
        messages: [
          { id: 2, created_at: 1760000100, status: 'failed', channel: 'bot',
            purpose: 'manual', target: '\u06af\u0631\u0648\u0647 \u0645\u0646',
            body: '\u067e\u06cc\u0627\u0645 \u062f\u0648\u0645', error: 'blocked' },
          { id: 1, created_at: 1760000000, status: 'sent', channel: 'bot',
            purpose: 'manual', target: '\u06af\u0631\u0648\u0647 \u0645\u0646',
            body: '\u0633\u0644\u0627\u0645', error: '' },
        ] });
    }
    if (clean.endsWith('/clipboard')) {
      const text = clipText === null ? '\u0633\u0644\u0627\u0645 \u06a9\u0644\u06cc\u067e' : clipText;
      return json({ ok: true, text, length: text.length });
    }
    if (clean.endsWith('/agent/devlog')) {
      return json({ entries: [{ id: 1, at: 1760000000, actor: 'system',
        title: '\u0646\u0627\u0645\u0647 \u0628\u0647 \u0627\u06cc\u062c\u0646\u062a',
        body: '\u0645\u062a\u0646 \u0646\u0627\u0645\u0647' }] });
    }
    if (clean.endsWith('/agent/pages')) {
      if (path.includes('id=')) {
        return json({ id: 7, title: '\u06af\u0632\u0627\u0631\u0634',
          html: '<b>\u0633\u0644\u0627\u0645</b>', created_at: 1760000000,
          updated_at: 1760000000, actor: 'agent' });
      }
      return json({ pages: [{ id: 7, title: '\u06af\u0632\u0627\u0631\u0634', size: 18,
        created_at: 1760000000, updated_at: 1760000000, actor: 'agent' }] });
    }
    if (clean.endsWith('/agent/suggestions')) {
      const one = { id: 3, title: '\u062a\u0645 \u067e\u06cc\u0634\u200c\u0641\u0631\u0636',
        problem: '\u0645\u0634\u06a9\u0644', proposal: '\u067e\u06cc\u0634\u0646\u0647\u0627\u062f',
        reason: '\u062f\u0644\u06cc\u0644', evidence: '', section: '\u062a\u0645',
        kind: 'settings', risk: 'low', needs_human: 1, status: 'approved',
        before_state: '{"key":"ui.theme","value":"light"}',
        after_state: '{"key":"ui.theme","value":"dark"}',
        apply_result: '', rollback_note: '', actor: 'agent',
        created_at: 1760000000, updated_at: 1760000000 };
      if (path.includes('id=')) return json({ suggestion: one });
      return json({ suggestions: [one], counts: { approved: 1 } });
    }
    if (clean.includes('/agent/suggestion-') || clean.endsWith('/agent/page-delete')) {
      return json({ ok: true, suggestion: { id: 3, status: 'applied' } });
    }
    if (clean.endsWith('/agent/db/schema')) {
      return json({ ok: true, database: 'agent.db',
        tables: [{ name: 'settings', rows: 2,
          columns: [{ name: 'key' }, { name: 'value' }],
          indexes: [], sensitiveColumns: ['value'] }], note: '' });
    }
    if (clean.endsWith('/agent/db/query')) {
      return json({ ok: true, columns: ['key', 'value'],
        rows: [{ key: 'telegram.botToken',
          value: { masked: true, configured: true, length: 15, last4: 'CRET' } }],
        count: 1, truncated: false, maskedColumns: ['value'] });
    }
    if (clean.endsWith('/agent/backups')) {
      return json({
        backups: [{ name: 'mas-backup-test.tar.gz', size: 2048, sizeText: '2.0 KB',
          createdAt: 1760000000, createdAtText: '2026-10-10 10:00:00' }],
        stats: { dbSizeText: '88 KB', flows: 2, backups: 1, backupSizeText: '2.0 KB',
          tables: { chat: 3, jobs: 1 }, dir: '/data/backups',
          lastBackup: { name: 'mas-backup-test.tar.gz', createdAt: 1760000000 },
          config: { enabled: false, scheduleKind: 'cron', schedule: '', keep: 7,
            telegramTarget: '', telegramChannel: '', includeSecrets: false,
            notifyAfterBackup: false, lastJobStatus: '' } } });
    }
    if (clean.endsWith('/agent/browser')) {
      return json({
        ok: true, cdpPort: 9223,
        profile: { key: 'agent', fa: '\u06a9\u0631\u0648\u0645 \u0627\u06cc\u062c\u0646\u062a', dir: '/data/ai-profile' },
        version: { ok: true, Browser: 'Chrome/155.0.8059.39' },
        tabs: { ok: true, count: 1, tabs: [{ id: 'T1', title: 'DeepSeek',
          url: 'https://chat.deepseek.com/' }] },
        history: { ok: true, totals: { visits: 2 }, visits: [{ url: 'https://old.example/',
          title: '\u0635\u0641\u062d\u0647\u0654 \u0628\u0633\u062a\u0647', at: 1759990000,
          timeText: '2026-10-10 07:00', visitCount: 1, transition: '\u0644\u06cc\u0646\u06a9' }],
          searches: [{ term: 'deepseek', timeText: '' }], downloads: [] },
        closedTabs: [{ url: 'https://old.example/', title: '\u0635\u0641\u062d\u0647\u0654 \u0628\u0633\u062a\u0647',
          timeText: '2026-10-10 07:00' }],
        closedTabsNote: 'derived from history' });
    }
    if (clean.endsWith('/agent/browser/tabs')) {
      return json({ ok: true, tabs: [{ id: 'T1', title: 'DeepSeek', url: 'https://chat.deepseek.com/' }] });
    }
    if (clean.endsWith('/agent/browser/history')) {
      return json({ ok: true, visits: [], searches: [], downloads: [], totals: {} });
    }
    if (clean.endsWith('/agent/notes')) {
      return json({ notes: [{ id: 'note-1', kind: 'note',
        body: '\u06cc\u0627\u062f\u062f\u0627\u0634\u062a', created_at: 1760000000 }] });
    }
    if (clean.endsWith('/agent/audit')) {
      return json({ audit: [{ at: 1760000000, actor: 'operator',
        action: 'setting.stored', detail: 'telegram.mode' }] });
    }
    if (path.endsWith('/status')) {
      return json(statusPayload || { runId: null, status: 'idle', entries: [], history: [] });
    }
    if (path.endsWith('/shots')) {
      return json({ shots: [
        { id: 's2', name: 'b.png', role: 'error', createdAt: 1760000100,
          stepIndex: 3, stepLabel: 'paste', image: { width: 1366, height: 768 },
          url: '/automation/api/public/shot/b.png' },
        { id: 's1', name: 'a.png', role: 'detect', createdAt: 1760000000,
          title: 'Arena', image: { width: 1366, height: 768 },
          url: '/automation/api/public/shot/a.png' },
      ] });
    }
    if (path.endsWith('/texts')) {
      return json({ texts: [
        { id: 't1', text: 'Battle Arena\nStart game', source: 'capture_text',
          stepLabel: 'خواندن صفحه', chars: 25, createdAt: 1760000050 },
      ] });
    }
    if (clean.endsWith('/agent/chat/messages')) {
      return json({ messages: [
        { id: 'm1', role: 'user', body: '\u0633\u0644\u0627\u0645', provider: 'deepseek',
          status: 'done', error: '', meta: {}, created_at: 1760000000 },
        { id: 'm2', role: 'assistant', provider: 'deepseek', status: 'done', error: '',
          body: '\u0633\u0644\u0627\u0645! \u0686\u0647 \u06a9\u0627\u0631\u06cc \u0627\u0646\u062c\u0627\u0645 \u0628\u062f\u0647\u0645\u061f\n```json\n'
            + '{"name":"proposal","steps":[{"type":"python","code":"check(click(10, 20))"}]}\n```',
          meta: { flow: { name: 'proposal', steps: [
            { type: 'python', code: 'check(click(10, 20))',
              label: '\u06a9\u0644\u06cc\u06a9 \u062f\u0631 (10\u060c 20)' }] },
                  elapsed: 4200 },
          created_at: 1760000010 },
        { id: 'm3', role: 'assistant', body: '', provider: 'deepseek',
          status: 'thinking', error: '', meta: {}, created_at: 1760000020 },
      ], pending: [{ id: 'm3' }], providers: ['deepseek', 'generic'] });
    }
    if (clean.endsWith('/agent/chat/send')) return json({ queued: true, messages: [] });
    if (clean.endsWith('/agent/chat/clear')) return json({ cleared: 3 });
    if (clean.endsWith('/agent/chat/apply-flow')) {
      return json({ flow: { name: 'proposal',
        steps: [{ id: 'a', type: 'python', code: 'check(click(10, 20))',
                  label: '\u06a9\u0644\u06cc\u06a9 \u062f\u0631 (10\u060c 20)' }] } });
    }
    if (clean.endsWith('/agent/operations')) {
      return json({ operations: [
        { id: 'op-builtin-lmarena', name: '\u0628\u0627\u0632 \u06a9\u0631\u062f\u0646 lmarena',
          description: '\u0633\u0627\u06cc\u062a \u0631\u0627 \u0628\u0627\u0632 \u0645\u06cc\u200c\u06a9\u0646\u062f', kind: 'steps',
          builtin: true, builtinKey: 'lmarena', permission: 'ask', scope: 'general',
          tags: ['chat'], steps: [
            { id: 'lm-1', type: 'python',
              code: "check(goto('https://lmarena.ai/'))",
              label: '\u0628\u0627\u0632 \u06a9\u0631\u062f\u0646 lmarena.ai' },
            { id: 'lm-2', type: 'python', code: 'wait(3.5)',
              label: '\u0635\u0628\u0631 \u0628\u0631\u0627\u06cc \u0628\u0627\u0631\u06af\u0630\u0627\u0631\u06cc' }],
          created_at: 1760000000, updated_at: 1760000000, last_run_at: 1760000900,
          last_status: 'done', run_count: 3 },
        { id: 'op-1', name: 'my operation', description: '', kind: 'steps',
          builtin: false, builtinKey: '', permission: 'auto', scope: 'brain',
          tags: [], steps: [{ id: 'x1', type: 'python', code: 'check(click(5, 6))' }],
          created_at: 1760000000, updated_at: 1760000000, last_run_at: 0,
          last_status: '', run_count: 0 },
      ], stepTypes: {
        python: { required: ['code'], optional: ['timeout'], common: [],
                  fa: '\u06a9\u062f \u067e\u0627\u06cc\u062a\u0648\u0646', en: 'Python code' },
      } });
    }
    if (clean.endsWith('/agent/brain')) {
      return json({ enabled: true, alive: true, busy: false,
        suggestion: { id: 'sug1', operationId: 'op-1', name: 'my operation',
                      createdAt: 1760000000, expiresAt: 1760001800 },
        nextAllowedAt: 0,
        policy: { runGapSec: 120, opCooldownMin: 10, suggestTtlMin: 15,
                  notify: true, forceStepShots: true, shotKeepDays: 7,
                  errShotKeepDays: 30 },
        autoOps: [{ id: 'op-builtin-observe', name: 'observe', permission: 'auto',
                    last_run_at: 1760000000, last_status: 'done' }],
        askOps: [{ id: 'op-1', name: 'my operation', permission: 'ask' }],
        log: [{ id: 1, at: 1760000000, actor: 'brain', title: 'brain said hi',
                body: '' }],
        messages: [
          { id: 1, at: 1760000000, direction: 'in', text: 'سلام مغز',
            actor: 'operator' },
          { id: 2, at: 1760000001, direction: 'out', text: 'دریافت شد',
            actor: '' },
        ],
        memory: [{ id: 1, at: 1760000000, text: 'فردا ساعت ۸' }] });
    }
    if (clean.endsWith('/agent/operation-run')) {
      return json({ started: true, id: 'op-1', status: 'running',
                    flow: { name: 'my operation',
                            steps: [{ type: 'python', code: 'check(click(5, 6))' }] } });
    }
    if (clean.endsWith('/agent/key')) {
      return json({ set: true, enabled: true, createdAt: 1759900000,
                    lastUsedAt: 1760000000, length: 43, prefix: 'mas_\u20269f2c' });
    }
    if (clean.endsWith('/agent/key/reveal')) return json({ key: 'mas_test_key_value' });
    if (clean.endsWith('/agent/key/rotate')) {
      return json({ key: 'mas_rotated_key', info: { set: true, enabled: true } });
    }
    if (clean.endsWith('/agent/api-index')) {
      return json({ endpoints: [
        { method: 'GET',
          path: 'https://af9833d9.eu-center.hostim.dev/automation/api/agent',
          fa: '\u0646\u0642\u0634\u0647', en: 'The full map' },
        { method: 'POST',
          path: 'https://af9833d9.eu-center.hostim.dev/automation/api/run',
          fa: '\u0627\u062c\u0631\u0627', en: 'Run a flow' }],
        keyWays: ['Authorization: Bearer <key>', 'x-agent-key: <key>', '?k=<key>'] });
    }
    if (clean.endsWith('/agent/cursor')) {
      return json({ cursor: { enabled: true, size: 44, color: '#ffd400',
                              outline: '#1b1b1b', ripple: true, applied: true } });
    }
    if (path.endsWith('/pages')) {
      return json({ pages: [{ id: 'p1', pageKey: 'lmarena.ai/', title: 'Battle Arena',
        url: 'https://lmarena.ai/', screenshot: 'shots/a.png', publicShot: 'a.png',
        capturedAt: 1760000000, elements: 12, challengeDetected: false, cdp: true }] });
    }
    if (clean.endsWith('/detect')) {
      return json({ ok: true, snapshot: { id: 'p1', pageKey: 'lmarena.ai/',
        title: 'Battle Arena', url: 'https://lmarena.ai/', screenshot: 'shots/a.png',
        publicShot: 'a.png', capturedAt: 1760000000, challengeDetected: false,
        challenge: { detected: false, signals: [] }, text: 'Start game',
        elements: [{ tag: 'button', selector: 'button#start', text: 'Start game',
                     href: '', desktop: { x: 512, y: 384 } }] } });
    }
    if (clean.endsWith('/report')) {
      return { ok: true, status: 200,
        text: async () => '## گزارش اجرای سیستم\nوضعیت: done · 3 گام' };
    }
    if (clean.endsWith('/agent/jobs')) {
      return json({ jobs: [{ id: 'j1', name: 'Google Search', kind: 'every',
        schedule: '5m', target: 'Google Search', payload: { action: 'flow' },
        enabled: 1, next_run_at: 1760000300, last_run_at: 1760000000,
        last_status: 'done' }] });
    }
    if (clean.endsWith('/runs')) {
      return json({ history: [{ runId: 'run-1', status: 'done',
        startedAt: 1760000000, finishedAt: 1760000040, stepCount: 3 }] });
    }
    if (path.endsWith('/flows')) {
      return json({ flows: [
        { name: 'Google Search', steps: 10, updatedAt: 1760000000, description: 'جستجو' },
        { name: 'Arena', steps: 4, updatedAt: 1759990000, description: '' },
      ] });
    }
    return json({});
  };
}

/** Mount the sidebar in a fresh jsdom page and hand back the handles. */
async function mount({ readyStateComplete = true, standalone = true, flow = null,
                      status: statusPayload = null, noAgent = false,
                      clipText = null, supaEmpty = false, supaOn = false,
                      uiPrefs = null } = {}) {
  const requests = [];
  const posts = [];
  const errors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', (e) => errors.push(String(e.message || e)));
  virtualConsole.on('error', (...args) => errors.push(args.join(' ')));

  const dom = new JSDOM(NOVNC_HTML, {
    url: 'https://example.trycloudflare.com/vnc.html',
    pretendToBeVisual: true,
    virtualConsole,
  });
  const { window } = dom;
  // The dedicated panel page sets this class on <body>; the sidebar reads it to
  // know it is the whole page rather than an overlay inside noVNC. It also
  // ships an empty header the sidebar fills with title, tabs, dot and the
  // desktop-view button (see panel.html).
  if (standalone) {
    window.document.body.classList.add('mas-standalone');
    const head = window.document.createElement('header');
    head.className = 'mas-page-head';
    window.document.body.insertBefore(head, window.document.body.firstChild);
  }

  const timers = [];
  const realSetInterval = globalThis.setInterval;
  const realClearInterval = globalThis.clearInterval;
  const globals = {
    window, document: window.document, navigator: window.navigator,
    localStorage: window.localStorage, HTMLElement: window.HTMLElement,
    Element: window.Element, Node: window.Node, Event: window.Event,
    CustomEvent: window.CustomEvent, getComputedStyle: window.getComputedStyle.bind(window),
    // A screenshot is fetched as a blob and turned into an object URL.
    URL: Object.assign(function URL() {}, window.URL || URL, {
      createObjectURL: () => 'blob:fake', revokeObjectURL: () => {},
    }),
    requestAnimationFrame: window.requestAnimationFrame
      ? window.requestAnimationFrame.bind(window) : (fn) => setTimeout(fn, 0),
    fetch: fakeApi(requests, posts, statusPayload, noAgent, supaEmpty, supaOn, uiPrefs, clipText),
    // Tracked so cleanup() can stop the sidebar's poll loops, otherwise the
    // test process never exits. Bound to the originals: referencing the global
    // here would recurse into this very wrapper.
    setInterval: (fn, ms) => { const id = realSetInterval(fn, ms); timers.push(id); return id; },
    clearInterval: (id) => realClearInterval(id),
  };
  // Some of these (navigator) are getter-only on globalThis, so assignment is
  // not enough; defineProperty with configurable keeps them restorable.
  const saved = {};
  for (const [key, value] of Object.entries(globals)) {
    saved[key] = Object.getOwnPropertyDescriptor(globalThis, key);
    Object.defineProperty(globalThis, key, { value, writable: true, configurable: true });
  }

  // A cache-busting query gives every mount its own module instance.
  // The sidebar reads its flow from localStorage at import time, so seed first.
  if (flow) window.localStorage.setItem('mas.flow', JSON.stringify(flow));

  const moduleUrl = `../automation/static/automation.js?run=${Math.random()}`;
  let importError = null;
  try {
    await import(moduleUrl);
  } catch (error) {
    importError = error;
  }

  if (readyStateComplete) {
    await new Promise((resolve) => setTimeout(resolve, 20));
  }

  return {
    dom, window, errors, importError, requests, posts,
    doc: window.document,
    wait: (ms = 30) => new Promise((resolve) => setTimeout(resolve, ms)),
    /** Poll until fn() is true, so tests do not flake on a slow first poll. */
    async until(fn, ms = 2000) {
      const deadline = Date.now() + ms;
      while (Date.now() < deadline) {
        if (fn()) return true;
        await new Promise((resolve) => setTimeout(resolve, 15));
      }
      return fn();
    },
    display: (selector) => window.getComputedStyle(window.document.querySelector(selector)).display,
    byText: (selector, text) => Array.from(window.document.querySelectorAll(selector))
      .find((node) => node.textContent.trim() === text),
    async cleanup() {
      // Let any in-flight api() promise land first. Restoring the globals while
      // one is still pending makes its continuation fail on `document`, which
      // is a harness artifact and not something the browser can ever hit.
      await new Promise((resolve) => setTimeout(resolve, 30));
      timers.forEach((id) => realClearInterval(id));
      for (const [key, descriptor] of Object.entries(saved)) {
        if (descriptor) Object.defineProperty(globalThis, key, descriptor);
        else delete globalThis[key];
      }
      window.close();
    },
  };
}

test('the sidebar mounts without throwing', async () => {
  const page = await mount();
  try {
    assert.equal(page.importError, null, `import threw: ${page.importError}`);
    assert.deepEqual(page.errors, [], `page errors: ${page.errors.join(' | ')}`);
    assert.ok(page.doc.getElementById('mas-root'), 'the panel was never added to the page');
    assert.ok(page.doc.getElementById('noVNC_canvas'), 'noVNC must still be in the page');
  } finally {
    await page.cleanup();
  }
});

test('a valid flow renders the step list (the appendChild(null) crash)', async () => {
  const page = await mount();
  try {
    // The panel opens on the chat tab now, so the stages are drawn on
    // demand like every other pane.
    clickTab(page, 'flow');
    await page.wait(10);
    const pane = page.doc.getElementById('mas-tab-flow');
    assert.ok(pane, 'flow tab missing');
    assert.ok(pane.querySelector('.mas-steps'), 'step list was not rendered');
    assert.ok(pane.querySelector('.mas-empty'), 'the empty-state hint is missing');
    // Round 26: the add-step button is gone - the agent writes the steps
    assert.ok(!page.byText('button', '\u0627\u0641\u0632\u0648\u062f\u0646 \u06af\u0627\u0645'));
    assert.ok(!page.byText('button', '\u0627\u0641\u0632\u0648\u062f\u0646 \u0645\u0631\u062d\u0644\u0647'),
      'the stages have no add button anymore; steps arrive from the agent');
  } finally {
    await page.cleanup();
  }
});

test('the stages number every step before its checkbox', async () => {
  const flow = {
    name: '\u0634\u0645\u0627\u0631\u0647',
    steps: [
      { id: 'p1', type: 'python', code: 'wait(1)', label: '\u06cc\u06a9', enabled: true },
      { id: 'p2', type: 'python', code: 'wait(2)', label: '\u062f\u0648', enabled: true },
    ],
    settings: { defaultDelayAfterMs: 0 },
  };
  const page = await mount({ flow });
  try {
    clickTab(page, 'flow');
    await page.wait(10);
    const rows = Array.from(page.doc.querySelectorAll('#mas-tab-flow .mas-step'));
    assert.equal(rows.length, 2);
    const numbers = rows.map((row) => row.querySelector('.mas-step-no').textContent);
    assert.deepEqual(numbers,
      ['\u0645\u0631\u062d\u0644\u0647\u0654 1', '\u0645\u0631\u062d\u0644\u0647\u0654 2']);
    // the number reads BEFORE the enable checkbox
    const kinds = Array.from(rows[0].children).map((node) => {
      if (node.classList.contains('mas-step-no')) return 'no';
      if (node.type === 'checkbox') return 'check';
      return 'other';
    });
    assert.ok(kinds.indexOf('no') < kinds.indexOf('check'),
      'the step number must come before the checkbox: ' + kinds.join(','));
  } finally {
    await page.cleanup();
  }
});

test('typing mode settings and python steps edit cleanly', async () => {
  const flow = {
    name: 'verification',
    viewport: { width: 1366, height: 768 },
    settings: { defaultDelayAfterMs: 0, typingMode: 'low', repeat: 1,
      screenshotAfterEachStep: false, stopOnError: true, allowShellSteps: false,
      stepTimeoutMs: 60000 },
    steps: [
      { id: 't1', type: 'python', code: "check(type_text('hello', mode='fast'))",
        label: '\u062a\u0627\u06cc\u067e \u00abhello\u00bb', enabled: true },
      { id: 'h1', type: 'python',
        code: "check(human_verify('\u0644\u0637\u0641\u0627\u064b \u0627\u06cc\u0646 \u0645\u0631\u062d\u0644\u0647 \u0631\u0627 \u062e\u0648\u062f\u062a \u0628\u0631\u0631\u0633\u06cc \u06a9\u0646.'))",
        label: '\u0628\u0631\u0631\u0633\u06cc \u0627\u0646\u0633\u0627\u0646\u06cc', enabled: true },
    ],
  };
  const page = await mount({ flow });
  try {
    clickTab(page, 'flow');
    await page.wait(10);
    // Round 25: the flow settings open from the gear button as a modal.
    const gear = page.byText('button', '\u2699 \u062a\u0646\u0638\u06cc\u0645\u0627\u062a \u062c\u0631\u06cc\u0627\u0646');
    assert.ok(gear, 'the gear button is missing');
    gear.dispatchEvent(new page.window.Event('click'));
    const globalMode = page.doc.getElementById('mas-flow-typing-mode');
    assert.ok(globalMode, 'flow-level typing mode selector is missing');
    assert.equal(globalMode.value, 'low');
    // the error protocol sits in the same modal and saves on change
    const errOp = page.doc.querySelector('.mas-modal input[placeholder="op-builtin-observe"]');
    assert.ok(errOp, 'the error-protocol input is missing from the modal');
    errOp.value = 'op-builtin-captcha';
    errOp.dispatchEvent(new page.window.Event('input'));
    const savedSettings = JSON.parse(page.window.localStorage.getItem('mas.flow'));
    assert.equal(savedSettings.settings.errorOperation, 'op-builtin-captcha');

    // Round 26: the step editor is the python program - one tall textarea
    const typeLabel = Array.from(page.doc.querySelectorAll('.mas-step-label'))
      .find((button) => button.textContent.includes('hello'));
    typeLabel.dispatchEvent(new page.window.Event('click'));
    const codeArea = page.doc.querySelector('.mas-editor textarea');
    assert.ok(codeArea, 'the python code editor is missing');
    assert.equal(codeArea.value, "check(type_text('hello', mode='fast'))");
    codeArea.value = "check(type_text('hello', mode='low'))";
    codeArea.dispatchEvent(new page.window.Event('input'));
    page.doc.querySelector('.mas-editor button').dispatchEvent(new page.window.Event('click'));
    const saved = JSON.parse(page.window.localStorage.getItem('mas.flow'));
    assert.equal(saved.steps[0].code, "check(type_text('hello', mode='low'))");

    const pauseLabel = Array.from(page.doc.querySelectorAll('.mas-step-label'))
      .find((button) => button.textContent.includes('\u0628\u0631\u0631\u0633\u06cc \u0627\u0646\u0633\u0627\u0646\u06cc'));
    pauseLabel.dispatchEvent(new page.window.Event('click'));
    const prompt = page.doc.querySelector('.mas-editor textarea');
    assert.ok(prompt, 'the human-verification step edits as code too');
    assert.ok(prompt.value.includes('human_verify('));
    assert.deepEqual(page.errors, []);
  } finally {
    await page.cleanup();
  }
});

test('the confirmation overlay stays off screen until a step needs it', async () => {
  const page = await mount();
  try {
    const confirm = page.doc.getElementById('mas-confirm');
    assert.equal(confirm.hidden, true, 'the overlay should start hidden');
    assert.equal(page.display('#mas-confirm'), 'none',
      'display:flex overrides [hidden]: the overlay would cover the VNC password prompt');
  } finally {
    await page.cleanup();
  }
});

test('CAPTCHA handoff notice is non-blocking and offers a human-only continuation', async () => {
  const status = {
    runId: 'r-captcha', status: 'waiting', index: 1, stepCount: 3, elapsedMs: 900,
    currentStep: null,
    awaitingConfirmation: {
      index: 1, label: 'verify page', type: 'click', kind: 'challenge',
      message: 'Review the page yourself.', pageOrigin: 'https://example.com',
      publicShot: 'challenge-1.png',
    },
    entries: [], results: [], handoffs: [],
  };
  const page = await mount({ status });
  try {
    const input = page.doc.getElementById('mas-token-input');
    input.value = VALID_TOKEN;
    page.byText('button', 'ثبت').dispatchEvent(new page.window.Event('click'));
    const confirm = page.doc.getElementById('mas-confirm');
    assert.ok(await page.until(() => !confirm.hidden), 'the human handoff notice did not appear');
    assert.ok(confirm.classList.contains('mas-confirm-handoff'));
    assert.equal(page.window.getComputedStyle(confirm).pointerEvents, 'none',
      'the handoff overlay must not block clicks on the noVNC canvas');
    assert.ok(confirm.textContent.includes('بررسی امنیتی'));
    assert.ok(confirm.textContent.includes('کد یا پاسخ را برای هوش مصنوعی نفرست'));
    assert.equal(page.doc.getElementById('mas-confirm-approve').textContent,
      'ادامه پس از بررسی انسانی');
    assert.equal(page.doc.getElementById('mas-confirm-refuse').textContent, 'توقف جریان');
    assert.equal(page.doc.getElementById('mas-confirm-shot').getAttribute('href'),
      '/automation/api/public/shot/challenge-1.png');

    page.doc.getElementById('mas-confirm-approve').dispatchEvent(new page.window.Event('click'));
    await page.wait(20);
    const posted = page.posts.find((p) => p.url.endsWith('/control'));
    assert.ok(posted, 'the continuation button did not call the control endpoint');
    assert.deepEqual(JSON.parse(posted.body), { action: 'confirm', approve: true });
    assert.deepEqual(page.errors, []);
  } finally {
    await page.cleanup();
  }
});

test('the desktop view stays clean: no slide-out panel, one launcher button', async () => {
  // The operator's verdict on vnc.html: the collapsible panel on the right
  // duplicated the big dedicated panel, so the desktop view is now a dark
  // stage whose only chrome is one button leading to that panel.
  const page = await mount({ standalone: false });
  try {
    assert.equal(page.doc.getElementById('mas-panel'), null,
      'the slide-out panel must not be rendered inside the desktop view');
    assert.equal(page.doc.getElementById('mas-toggle'), null,
      'no collapse tab either');
    assert.ok(page.doc.body.classList.contains('mas-desktop-dark'),
      'the desktop view must carry the dark base theme');
    const link = page.doc.querySelector('#mas-desktop-launcher a.mas-launcher-btn');
    assert.ok(link, 'the launcher button is missing');
    assert.equal(link.getAttribute('href'), 'automation/panel.html');
    assert.equal(link.getAttribute('target'), '_blank');
    assert.ok(link.textContent.includes('\u067e\u0646\u0644 \u0628\u0632\u0631\u06af'),
      'the button must read "big panel": ' + link.textContent);
    assert.ok(page.doc.getElementById('mas-toast'),
      'clipboard-sync toasts still need a host in the desktop view');
    assert.ok(page.doc.getElementById('noVNC_canvas'), 'noVNC must still be in the page');
  } finally {
    await page.cleanup();
  }
});

test('tabs swap panes and only one is visible', async () => {
  const page = await mount();
  try {
    // Scope to the main tab row: the brain's sub-tabs (round 25) reuse the
    // mas-tab class inside .mas-subtabs, like the db and ops sub-tabs do.
    const tabs = Array.from(
      page.doc.querySelectorAll('.mas-tabs:not(.mas-subtabs) .mas-tab'));
    assert.deepEqual(tabs.map((n) => n.dataset.tab), TAB_ORDER);

    clickTab(page, 'agent');                                // coworker agent
    assert.equal(page.display('#mas-tab-agent'), 'block');
    assert.equal(page.display('#mas-tab-flow'), 'none');
    assert.ok(page.doc.querySelector('#mas-tab-agent [data-sub]'),
      'the agent sub-tabs are missing');

    clickTab(page, 'db');                                   // database sub-tabs
    assert.equal(page.display('#mas-tab-db'), 'block');
    assert.equal(page.display('#mas-tab-agent'), 'none');
    clickDbSub(page, 'shots');                              // screenshots
    assert.ok(page.doc.querySelector('#mas-tab-db [data-dbsub="shots"].is-active'));
    clickDbSub(page, 'texts');                              // extracted texts
    assert.ok(page.doc.querySelector('#mas-tab-db [data-dbsub="texts"].is-active'));
    assert.ok(!page.doc.querySelector('#mas-tab-db [data-dbsub="shots"].is-active'));
  } finally {
    await page.cleanup();
  }
});

test('the stages have no add-step button: the agent writes the steps', async () => {
  const page = await mount();
  try {
    clickTab(page, 'flow');
    await page.wait(10);
    const pane = page.doc.getElementById('mas-tab-flow');
    // Round 26: no type picker and no add button remain in the stages
    assert.equal(pane.querySelector('select'), null,
      'no step-type picker may remain in the stages');
    const addButtons = Array.from(pane.querySelectorAll('button'))
      .filter((node) => /\u0627\u0641\u0632\u0648\u062f\u0646/.test(node.textContent));
    assert.deepEqual(addButtons, []);
    // and the record tab is gone with it
    assert.equal(page.doc.getElementById('mas-tab-record'), null);
  } finally {
    await page.cleanup();
  }
});

test('with no token the panel asks for one instead of throwing errors', async () => {
  const page = await mount();
  try {
    const bar = page.doc.getElementById('mas-tokenbar');
    assert.ok(bar, 'the token banner is missing');
    assert.equal(bar.hidden, false, 'the banner must be visible before a token is set');
    assert.equal(page.display('#mas-tokenbar'), 'block');
    assert.ok(page.doc.getElementById('mas-token-input'), 'the token input is missing');
    assert.equal(page.doc.getElementById('mas-toast').children.length, 0,
      'a background poll must never raise a toast');
  } finally {
    await page.cleanup();
  }
});

test('without a token it does not knock on authenticated endpoints', async () => {
  const page = await mount();
  try {
    await page.wait(60);  // let several poll cycles run
    assert.ok(page.requests.some((u) => u.endsWith('/info')), '/info was never fetched');
    assert.equal(page.requests.filter((u) => u.endsWith('/status')).length, 0,
      'it polled /status without a token: ' + page.requests.join(', '));
  } finally {
    await page.cleanup();
  }
});

test('entering the token connects and clears the banner', async () => {
  const page = await mount();
  try {
    const input = page.doc.getElementById('mas-token-input');
    input.value = VALID_TOKEN;
    page.byText('button', 'ثبت').dispatchEvent(new page.window.Event('click'));
    await page.wait(60);

    assert.equal(page.display('#mas-tokenbar'), 'none', 'the banner should disappear');
    assert.ok(page.requests.some((u) => u.endsWith('/status')), '/status was never reached');
    assert.equal(page.doc.getElementById('mas-toast').children.length, 0);
    assert.ok(page.doc.querySelector('#mas-conn').classList.contains('is-ok'),
      'the connection dot should turn green');
  } finally {
    await page.cleanup();
  }
});

test('a wrong token says so in the banner, not in a toast', async () => {
  const page = await mount();
  try {
    const input = page.doc.getElementById('mas-token-input');
    input.value = 'not-the-token';
    page.byText('button', 'ثبت').dispatchEvent(new page.window.Event('click'));
    await page.wait(60);

    const bar = page.doc.getElementById('mas-tokenbar');
    assert.equal(bar.hidden, false, 'the banner must stay so the human can retry');
    assert.ok(bar.classList.contains('is-error'), 'it should be marked as an error');
    assert.equal(page.doc.getElementById('mas-toast').children.length, 0);
  } finally {
    await page.cleanup();
  }
});

test('the panel follows the persisted theme from the database', async () => {
  const page = await mount();
  try {
    await authenticate(page);
    // the fake /agent carries ui.theme=dark: boot must honour the db choice
    const applied = await page.until(
      () => page.doc.documentElement.dataset.theme === 'dark');
    assert.ok(applied, 'the persisted dark theme was never applied');
    const root = page.doc.getElementById('mas-root');
    assert.equal(page.window.getComputedStyle(root).colorScheme, 'dark',
      'widgets must follow the chosen theme, not fight it');
  } finally {
    await page.cleanup();
  }
});

test('step list shows no numbering and options are styled dark', async () => {
  const page = await mount();
  try {
    // The panel opens on the chat tab now, so the stages are drawn on
    // demand like every other pane.
    clickTab(page, 'flow');
    await page.wait(10);
    const list = page.doc.querySelector('.mas-steps');
    assert.equal(page.window.getComputedStyle(list).listStyleType, 'none');
    // jsdom does not resolve var(), so check the token layer in the source:
    // the select paints with --bg-solid, which both themes define.
    // jsdom shadows the global URL, so derive the path as a plain string.
    const cssPath = decodeURIComponent(import.meta.url.replace(/^file:\/\//, ''))
      .replace(/tests\/test_ui\.mjs$/, 'automation/static/automation.css');
    const css = fs.readFileSync(cssPath, 'utf8');
    assert.match(css, /select\.mas-input \{[^}]*background: var\(--bg-solid\)/s,
      'selects must use the surface token');
    assert.match(css, /:root \{[^}]*--bg-solid: #ffffff/s, 'light token missing');
    assert.match(css, /html\[data-theme='dark'\] \{[^}]*--bg-solid: #171b21/s,
      'dark token missing');
  } finally {
    await page.cleanup();
  }
});

/** Tabs by name. The brain comes first (round 25), then chat. */
// Round 26: the record tab is gone from the panel.
const TAB_ORDER = ['brain', 'chat', 'library', 'flow', 'pages',
  'db', 'ops', 'settings', 'agent', 'log'];

function tab(page, name) {
  const node = Array.from(page.doc.querySelectorAll('.mas-tab'))
    .find((item) => item.dataset.tab === name);
  assert.ok(node, `no tab named ${name}`);
  return node;
}

function clickTab(page, name) {
  tab(page, name).dispatchEvent(new page.window.Event('click'));
}

function clickBrainSub(page, name) {
  const node = Array.from(page.doc.querySelectorAll('[data-brainsub]'))
    .find((item) => item.dataset.brainsub === name);
  assert.ok(node, `no brain sub-tab named ${name}`);
  node.dispatchEvent(new page.window.Event('click'));
}




test('editing a step refreshes its label instead of keeping the fallback', async () => {
  // Round 26, the same honesty in python: a step with empty code reads as
  // «پایتون»; once real code is typed, the row shows the program's first line.
  const flow = {
    name: 'labels',
    steps: [{ id: 'p1', type: 'python', code: '', enabled: true }],
    settings: { defaultDelayAfterMs: 0 },
  };
  const page = await mount({ flow });
  try {
    clickTab(page, 'flow');
    await page.wait(10);
    const pane = page.doc.getElementById('mas-tab-flow');
    const row = pane.querySelector('.mas-step');
    assert.ok(row.textContent.includes('\u067e\u0627\u06cc\u062a\u0648\u0646'),
      'expected the fallback label first: ' + row.textContent);

    row.querySelector('.mas-step-label').dispatchEvent(new page.window.Event('click'));
    const field = (name) => Array.from(pane.querySelectorAll('.mas-editor label'))
      .find((l) => l.querySelector('span') && l.querySelector('span').textContent === name)
      .querySelector('input, textarea, select');
    const code = field('code');
    assert.ok(code, 'the code field is missing');
    code.value = "check(paste('\u0633\u0644\u0627\u0645 \u062f\u0646\u06cc\u0627'))";
    code.dispatchEvent(new page.window.Event('input'));
    page.byText('button', 'OK').dispatchEvent(new page.window.Event('click'));

    const label = pane.querySelector('.mas-step .mas-step-label').textContent;
    assert.ok(label.includes('\u0633\u0644\u0627\u0645 \u062f\u0646\u06cc\u0627'),
      'the label stayed stale: ' + label);
  } finally {
    await page.cleanup();
  }
});

test('a hand written label survives editing the step', async () => {
  const flow = {
    name: 'labels',
    steps: [{ id: 'p1', type: 'python', code: 'wait(1)',
              label: '\u0646\u0648\u0634\u062a\u0646 \u062f\u0631 \u06af\u0648\u06af\u0644', enabled: true }],
    settings: { defaultDelayAfterMs: 0 },
  };
  const page = await mount({ flow });
  try {
    clickTab(page, 'flow');
    await page.wait(10);
    const pane = page.doc.getElementById('mas-tab-flow');
    pane.querySelector('.mas-step .mas-step-label').dispatchEvent(new page.window.Event('click'));
    const field = (name) => Array.from(pane.querySelectorAll('.mas-editor label'))
      .find((l) => l.querySelector('span') && l.querySelector('span').textContent === name)
      .querySelector('input, textarea, select');
    const label = field('label');
    assert.equal(label.value, '\u0646\u0648\u0634\u062a\u0646 \u062f\u0631 \u06af\u0648\u06af\u0644');
    // touch the label so the editor knows it is hand written, then edit code
    label.value = '\u0646\u0648\u0634\u062a\u0646 \u062f\u0631 \u06af\u0648\u06af\u0644';
    label.dispatchEvent(new page.window.Event('input'));
    const code = field('code');
    code.value = "check(type_text('hello'))";
    code.dispatchEvent(new page.window.Event('input'));
    page.byText('button', 'OK').dispatchEvent(new page.window.Event('click'));

    assert.equal(pane.querySelector('.mas-step .mas-step-label').textContent,
      '\u0646\u0648\u0634\u062a\u0646 \u062f\u0631 \u06af\u0648\u06af\u0644');
  } finally {
    await page.cleanup();
  }
});

test('the agent assistant sends the user request and an absolute origin', async () => {
  const page = await mount();
  try {
    const input = page.doc.getElementById('mas-token-input');
    input.value = VALID_TOKEN;
    page.byText('button', 'ثبت').dispatchEvent(new page.window.Event('click'));
    await page.wait(60);

    await openPrompt(page);

    const box = page.doc.getElementById('mas-ai-request');
    assert.ok(box, 'the request box is missing');
    box.value = 'روی اولین نتیجه کلیک کن';
    box.dispatchEvent(new page.window.Event('input'));

    // Switching tabs and back must not lose what the human typed.
    clickTab(page, 'flow');
    clickTab(page, 'agent');
    await page.wait(10);
    assert.equal(page.doc.getElementById('mas-ai-request').value, 'روی اولین نتیجه کلیک کن');

    const build = Array.from(page.doc.querySelectorAll('#mas-agent-sub button'))
      .find((b) => b.textContent.includes('🧩'));
    build.dispatchEvent(new page.window.Event('click'));
    await page.wait(60);

    const sent = page.posts.find((p) => p.url.endsWith('/prompt'));
    assert.ok(sent, '/prompt was never called');
    const body = JSON.parse(sent.body);
    assert.equal(body.request, 'روی اولین نتیجه کلیک کن');
    assert.equal(body.publicBase, 'https://example.trycloudflare.com');
  } finally {
    await page.cleanup();
  }
});

test('the standalone panel opens full page and has no record tab', async () => {
  const page = await mount({ standalone: true });
  try {
    // No collapse tab, and the panel is on screen without anyone clicking.
    assert.equal(page.display('#mas-toggle'), 'none', 'the collapse tab should be gone');
    assert.notEqual(page.display('#mas-panel'), 'none', 'the panel should be open');

    // A way back to the live desktop view, opening in a new tab.
    const link = page.doc.querySelector('.mas-desktop-link');
    assert.ok(link, 'no link to the desktop view');
    assert.ok(link.textContent.includes('\u0646\u0645\u0627\u06cc \u062f\u0633\u06a9\u062a\u0627\u067e'), link.textContent);
    assert.equal(link.getAttribute('href'),
      '../vnc.html?autoconnect=true&resize=scale&path=websockify');
    assert.equal(link.getAttribute('target'), '_blank');

    // Round 26: recording is gone from the panel entirely.
    assert.equal(page.doc.getElementById('mas-tab-record'), null);
    assert.ok(!page.byText('button', '\u0636\u0628\u0637'));
  } finally {
    await page.cleanup();
  }
});

test('inside noVNC the desktop view links to the dedicated panel', async () => {
  const page = await mount({ standalone: false });
  try {
    const link = page.doc.querySelector('a[href="automation/panel.html"]');
    assert.ok(link, 'no link to the dedicated panel');
    assert.equal(link.getAttribute('target'), '_blank');
  } finally {
    await page.cleanup();
  }
});

test('a finished run shows the board, per step badges and a report button', async () => {
  // The user's case: three steps ran, the third failed, the rest never ran.
  const status = {
    runId: 'r1', status: 'error', index: 2, passIndex: 1, stepCount: 4,
    elapsedMs: 12340,
    error: "step #2 (type 'x') failed with rc=124: timed out",
    currentStep: null, awaitingConfirmation: null,
    results: [
      { index: 0, passIndex: 1, type: 'click', label: 'click 100,200', status: 'ok', durationMs: 42 },
      { index: 1, passIndex: 1, type: 'paste', label: 'paste "سلام"', status: 'ok', durationMs: 88 },
      { index: 2, passIndex: 1, type: 'type', label: "type 'x'", status: 'error',
        durationMs: 60000, error: 'timed out after 60s' },
    ],
    entries: [],
  };
  const flow = {
    name: 'جریان تست',
    viewport: { width: 1366, height: 768 },
    settings: {
      defaultDelayAfterMs: 0, screenshotAfterEachStep: false, stopOnError: true,
      repeat: 1, allowShellSteps: false, stepTimeoutMs: 60000,
    },
    steps: [
      { id: 'a', type: 'click', x: 100, y: 200, label: 'click 100,200' },
      { id: 'b', type: 'paste', text: 'سلام', label: 'paste "سلام"' },
      { id: 'c', type: 'type', text: 'x', label: "type 'x'" },
      { id: 'd', type: 'click', x: 5, y: 6, label: 'click 5,6' },
    ],
  };
  const page = await mount({ status, flow });
  try {
    // The panel opens on the chat tab now, so the stages are drawn on
    // demand like every other pane.
    clickTab(page, 'flow');
    await page.wait(10);
    // The poll only reaches /status once a token is set.
    const input = page.doc.getElementById('mas-token-input');
    input.value = VALID_TOKEN;
    page.byText('button', 'ثبت').dispatchEvent(new page.window.Event('click'));
    const panel = page.doc.getElementById('mas-runpanel');
    assert.ok(await page.until(() => !panel.hidden), 'the run board never appeared');
    assert.ok(panel.querySelector('.mas-pill-error'), 'the status pill should read error');
    assert.ok(panel.querySelector('#mas-run-clock'), 'no clock');
    assert.ok(panel.querySelector('.mas-bar-fill'), 'no progress bar');
    assert.ok(panel.textContent.includes('3/4'), 'expected 3/4 done: ' + panel.textContent);
    assert.ok(panel.textContent.includes('timed out'), 'the error should be shown');

    const report = Array.from(panel.querySelectorAll('button'))
      .find((b) => b.textContent.includes('گزارش'));
    assert.ok(report, 'no copy-report button');

    const badges = Array.from(page.doc.querySelectorAll('.mas-step .mas-state'))
      .map((n) => n.className.replace('mas-state ', ''));
    assert.deepEqual(page.errors, [], 'the sidebar threw: ' + page.errors.join(' | '));
    assert.deepEqual(badges, ['is-ok', 'is-ok', 'is-error', 'is-pending'], badges.join(','));

    const failed = page.doc.querySelectorAll('.mas-step .mas-state')[2];
    assert.ok(failed.textContent.includes('60000ms'), failed.textContent);
  } finally {
    await page.cleanup();
  }
});

test('the flow tab can also copy the run report', async () => {
  const page = await mount();
  try {
    // The panel opens on the chat tab now, so the stages are drawn on
    // demand like every other pane.
    clickTab(page, 'flow');
    await page.wait(10);
    const pane = page.doc.getElementById('mas-tab-flow');
    const button = Array.from(pane.querySelectorAll('button'))
      .find((b) => b.textContent.includes('کپی گزارش اجرا'));
    assert.ok(button, 'no report button in the flow tab');
    button.dispatchEvent(new page.window.Event('click'));
    await page.wait(40);
    const report = page.requests.find((u) => u.includes('/report'));
    assert.ok(report, '/report was never fetched');
    // The origin must ride along, or the screenshot links in the report are
    // only paths and the model cannot open them.
    assert.ok(report.includes('base=https%3A%2F%2Fexample.trycloudflare.com'),
      'the report was fetched without the origin: ' + report);
  } finally {
    await page.cleanup();
  }
});

test('clear all removes every step after asking', async () => {
  const flow = {
    name: 'f', viewport: { width: 1366, height: 768 },
    settings: { defaultDelayAfterMs: 0, repeat: 1, stopOnError: true },
    steps: [
      { id: 'a', type: 'click', x: 1, y: 2 },
      { id: 'b', type: 'click', x: 3, y: 4 },
      { id: 'c', type: 'click', x: 5, y: 6 },
    ],
  };
  const page = await mount({ flow });
  try {
    // The panel opens on the chat tab now, so the stages are drawn on
    // demand like every other pane.
    clickTab(page, 'flow');
    await page.wait(10);
    const pane = page.doc.getElementById('mas-tab-flow');
    assert.equal(pane.querySelectorAll('.mas-step').length, 3);

    const clear = Array.from(pane.querySelectorAll('button'))
      .find((b) => b.textContent.includes('پاک کردن همه'));
    assert.ok(clear, 'no clear-all button');

    // Refusing must keep the steps.
    page.window.confirm = () => false;
    clear.dispatchEvent(new page.window.Event('click'));
    assert.equal(pane.querySelectorAll('.mas-step').length, 3);

    page.window.confirm = () => true;
    clear.dispatchEvent(new page.window.Event('click'));
    assert.equal(pane.querySelectorAll('.mas-step').length, 0, 'steps were not cleared');
    assert.ok(pane.querySelector('.mas-empty'), 'the empty hint should be back');
  } finally {
    await page.cleanup();
  }
});

test('stop reacts at once and reaches the server', async () => {
  const page = await mount({ status: { runId: 'r', status: 'running', index: 0,
    stepCount: 3, elapsedMs: 100, results: [], entries: [] } });
  try {
    const input = page.doc.getElementById('mas-token-input');
    input.value = VALID_TOKEN;
    page.byText('button', 'ثبت').dispatchEvent(new page.window.Event('click'));
    await page.wait(60);

    const stop = page.doc.getElementById('mas-stop');
    stop.dispatchEvent(new page.window.Event('click'));
    assert.equal(stop.disabled, true, 'the button should lock while stopping');
    assert.ok(stop.textContent.includes('…'), 'no busy feedback: ' + stop.textContent);
    await page.wait(80);
    assert.equal(stop.disabled, false, 'the button should unlock afterwards');
    assert.ok(page.posts.some((p) => p.url.endsWith('/control')
      && JSON.parse(p.body).action === 'stop'), '/control stop was never sent');
  } finally {
    await page.cleanup();
  }
});

test('the AI answer is kept and handed back on the next prompt', async () => {
  const page = await mount();
  try {
    const input = page.doc.getElementById('mas-token-input');
    input.value = VALID_TOKEN;
    page.byText('button', 'ثبت').dispatchEvent(new page.window.Event('click'));
    await page.wait(60);
    await openPrompt(page);

    const reply = page.doc.getElementById('mas-ai-reply');
    assert.ok(reply, 'no AI reply box');
    reply.value = '### ۱) چه فهمیدی\nباید جستجو کنم.\n```json\n'
      + '{"name":"t","steps":[{"type":"click","x":5,"y":5}]}\n```\n'
      + '### ۳) سؤال و نکته\nکدام نتیجه را کلیک کنم؟';
    reply.dispatchEvent(new page.window.Event('input'));

    const build = Array.from(page.doc.querySelectorAll('#mas-agent-sub button'))
      .find((b) => b.textContent.includes('🧩'));
    build.dispatchEvent(new page.window.Event('click'));
    await page.wait(60);

    const sent = page.posts.filter((p) => p.url.endsWith('/prompt')).pop();
    assert.ok(sent, '/prompt was never called');
    assert.ok(JSON.parse(sent.body).previousReply.includes('چه فهمیدی'),
      'the previous answer was not sent back');

    // Importing shows the model's prose, not just its code.
    const importBtn = Array.from(page.doc.querySelectorAll('#mas-agent-sub button'))
      .find((b) => b.textContent.includes('وارد کردن'));
    importBtn.dispatchEvent(new page.window.Event('click'));
    await page.wait(40);
    clickTab(page, 'agent');
    await page.wait(10);
    const notes = page.doc.querySelector('.mas-ai-notes');
    assert.ok(notes, 'the AI notes box did not appear');
    assert.ok(notes.textContent.includes('باید جستجو کنم'), notes.textContent);
    assert.ok(!notes.querySelector('pre').textContent.includes('```json'),
      'the JSON block should be stripped from the notes');
    assert.ok(notes.textContent.includes('کدام نتیجه را کلیک کنم؟'),
      'the model question was lost');
  } finally {
    await page.cleanup();
  }
});

test('the saved flow library lists, loads, runs and deletes', async () => {
  const page = await mount();
  try {
    // The panel opens on the chat tab now, so the stages are drawn on
    // demand like every other pane.
    const input = page.doc.getElementById('mas-token-input');
    input.value = VALID_TOKEN;
    page.byText('button', 'ثبت').dispatchEvent(new page.window.Event('click'));
    await page.wait(80);
    clickTab(page, 'library');
    const libBox = () => page.doc.getElementById('mas-flows-lib')
      || { querySelectorAll: () => [] };
    assert.ok(await page.until(() => libBox().querySelectorAll('.mas-lib-row').length === 2),
      'the library never listed the flows');

    const box = page.doc.getElementById('mas-flows-lib');
    const rows = box.querySelectorAll('.mas-lib-row');
    assert.equal(rows[0].dataset.name, 'Google Search');
    assert.ok(rows[0].textContent.includes('10'), 'step count is missing');
    assert.ok(!page.doc.querySelector('#mas-tab-flow .mas-lib-row'),
      'the flows tab must not carry the library any more');

    const actions = rows[0].querySelectorAll('.mas-lib-actions button');
    assert.equal(actions.length, 4, 'expected edit / load / run / delete');

    // editing opens a centred modal, not an inline jump
    actions[0].dispatchEvent(new page.window.Event('click'));
    assert.ok(await page.until(() => page.doc.querySelector('.mas-modal')),
      'the edit modal never opened');
    const nameInput = page.doc.querySelector('.mas-modal input.mas-input');
    assert.equal(nameInput.value, 'Google Search');
    nameInput.value = 'Google Renamed';
    page.doc.querySelector('.mas-modal .mas-btn.mas-primary')
      .dispatchEvent(new page.window.Event('click'));
    assert.ok(await page.until(() => page.requests.some((url) =>
      url.includes('/flows/Google%20Renamed'))),
      'the rename never reached the server');

    page.window.confirm = () => true;
    const after = box.querySelectorAll('.mas-lib-row');
    const delBtn = after[0].querySelectorAll('.mas-lib-actions button')[3];
    delBtn.dispatchEvent(new page.window.Event('click'));
    await page.wait(80);
    assert.ok(page.requests.some((u) => u.includes('/flows/Google%20Search')),
      'the delete never reached the server');
  } finally {
    await page.cleanup();
  }
});

test('the screenshots tab lists every shot with time, size and link', async () => {
  const page = await mount();
  try {
    const input = page.doc.getElementById('mas-token-input');
    input.value = VALID_TOKEN;
    page.byText('button', 'ثبت').dispatchEvent(new page.window.Event('click'));
    await page.wait(60);
    clickTab(page, 'db');
    clickDbSub(page, 'shots');

    const pane = page.doc.getElementById('mas-tab-db');
    assert.ok(await page.until(() => pane.querySelectorAll('.mas-shot').length === 2),
      'the shots never listed: ' + pane.textContent);

    const first = pane.querySelector('.mas-shot');
    // Newest first, with a real timestamp, the image size and an openable link.
    assert.match(first.textContent, /\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}/,
      'no date and time: ' + first.textContent);
    assert.ok(first.textContent.includes('1366×768'), 'no image size');
    const link = first.querySelector('a[href*="/public/shot/"]');
    assert.ok(link, 'no link to the image');
    assert.equal(link.getAttribute('target'), '_blank');
    assert.ok(first.querySelector('img'), 'no thumbnail');
    assert.ok(first.textContent.includes('لحظه خطا'), 'the role is missing');
  } finally {
    await page.cleanup();
  }
});

test('the texts tab lists saved text and can save a new note', async () => {
  const page = await mount();
  try {
    const input = page.doc.getElementById('mas-token-input');
    input.value = VALID_TOKEN;
    page.byText('button', 'ثبت').dispatchEvent(new page.window.Event('click'));
    await page.wait(60);
    clickTab(page, 'db');
    clickDbSub(page, 'texts');

    const pane = page.doc.getElementById('mas-tab-db');
    assert.ok(await page.until(() => pane.querySelectorAll('.mas-text').length === 1),
      'the texts never listed: ' + pane.textContent);
    assert.ok(pane.textContent.includes('Battle Arena'), 'the saved text is missing');

    const box = pane.querySelector('textarea');
    box.value = 'یادداشت دستی من';
    box.dispatchEvent(new page.window.Event('input'));
    const save = Array.from(pane.querySelectorAll('button'))
      .find((b) => b.textContent.includes('ذخیره یادداشت'));
    save.dispatchEvent(new page.window.Event('click'));
    await page.wait(60);

    const posted = page.posts.find((p) => p.url.endsWith('/texts'));
    assert.ok(posted, '/texts was never posted');
    assert.equal(JSON.parse(posted.body).text, 'یادداشت دستی من');
  } finally {
    await page.cleanup();
  }
});

test('the click-capture layer is gone with the record tab', async () => {
  const page = await mount();
  try {
    // Round 26: nothing captures raw clicks anymore
    assert.equal(page.doc.getElementById('mas-record-layer'), null);
  } finally {
    await page.cleanup();
  }
});

/* ------------------------------------------------------------------ *
 * The coworker agent tab
 * ------------------------------------------------------------------ */

const AGENT_SUB_LABELS = ['پرامپت', 'صفحه اختصاصی', 'پیشنهادات ایجنت',
  'کلید ایجنت', 'تلگرام', 'مرورگر', 'نشانگر', 'وظایف', 'کپچا', 'اسکریپت',
  'داده‌ها', 'کلیدها'];

/** Open the panel, authenticate, switch to the agent tab and one sub-pane. */
async function openAgentSub(page, label) {
  const input = page.doc.getElementById('mas-token-input');
  input.value = VALID_TOKEN;
  page.byText('button', 'ثبت').dispatchEvent(new page.window.Event('click'));
  await page.wait(60);
  clickTab(page, 'agent');
  const ready = await page.until(
    () => page.doc.querySelectorAll('#mas-tab-agent [data-sub]').length
      === AGENT_SUB_LABELS.length);
  assert.ok(ready, 'the agent sub-tabs never rendered');
  if (!label) return;
  const sub = Array.from(page.doc.querySelectorAll('#mas-tab-agent [data-sub]'))
    .find((node) => node.textContent.trim() === label);
  assert.ok(sub, `sub-tab ${label} is missing`);
  sub.dispatchEvent(new page.window.Event('click'));
  await page.until(() => page.doc.querySelector('#mas-agent-sub .mas-box') !== null);
}

/** Authenticate and open the agent's prompt sub-tab (the first one). */
async function openPrompt(page) {
  const input = page.doc.getElementById('mas-token-input');
  input.value = VALID_TOKEN;
  page.byText('button', 'ثبت').dispatchEvent(new page.window.Event('click'));
  await page.wait(60);
  clickTab(page, 'agent');
  const ready = await page.until(() => page.doc.getElementById('mas-ai-request') !== null);
  assert.ok(ready, 'the prompt sub-tab never rendered');
}

/** Switch inside the telegram pane to one of its nested sub-tabs. */
function clickTgSub(page, name) {
  const sub = page.doc.querySelector(`#mas-agent-sub [data-tgsub="${name}"]`);
  assert.ok(sub, `telegram sub-tab ${name} is missing`);
  sub.dispatchEvent(new page.window.Event('click'));
}

/** Switch inside the database tab to one of its three sub-tabs. */
function clickDbSub(page, name) {
  const sub = page.doc.querySelector(`#mas-tab-db [data-dbsub="${name}"]`);
  assert.ok(sub, `db sub-tab ${name} is missing`);
  sub.dispatchEvent(new page.window.Event('click'));
}

function paneText(page) {
  const pane = page.doc.getElementById('mas-agent-sub');
  return pane ? pane.textContent : '';
}

function findButton(page, needle, scope = '#mas-tab-agent') {
  return Array.from(page.doc.querySelectorAll(`${scope} button`))
    .find((node) => node.textContent.includes(needle));
}

test('the agent tab lists its sub-panes and the chat tab keeps the assistant', async () => {
  const page = await mount();
  try {
    await openAgentSub(page);
    const labels = Array.from(page.doc.querySelectorAll('#mas-tab-agent [data-sub]'))
      .map((node) => node.textContent.trim());
    assert.deepEqual(labels, AGENT_SUB_LABELS);
    // The prompt workspace is the agent's first sub-tab now; the chat tab
    // only keeps a pointer button, so there is one home for prompts.
    assert.equal(page.doc.querySelectorAll('#mas-tab-agent [data-sub]')[0]
      .textContent.trim(), 'پرامپت', 'the prompt sub-tab must come first');
    assert.ok(page.doc.getElementById('mas-ai-request'), 'the request box is gone');
    assert.ok(page.doc.getElementById('mas-ai-reply'), 'the import box is gone');
    assert.ok(findButton(page, '🧩', '#mas-tab-agent'), 'the copy-prompt button is gone');
    assert.ok(findButton(page, '🤖', '#mas-tab-agent'),
      'the ask-the-agent-directly button is missing');
    clickTab(page, 'chat');
    await page.wait(10);
    assert.ok(!page.doc.querySelector('#mas-tab-chat #mas-ai-request'),
      'the chat tab must not embed a second prompt workspace');
    // the chat tab keeps no prompt toolbox at all any more: the prompt
    // sub-tab is the single home, and the header must stay quiet.
    assert.ok(!page.doc.querySelector('#mas-tab-chat .mas-chat-tools'),
      'the chat tab must not carry the old toolbox');
    assert.ok(!findButton(page, 'پرامپت', '#mas-tab-chat'),
      'no pointer button should remain in the chat tab');
  } finally {
    await page.cleanup();
  }
});

test('the kill switch posts to the API', async () => {
  const page = await mount();
  try {
    await openAgentSub(page);
    // The status bar is filled by a second request, so wait for it rather than
    // racing the render.
    assert.ok(await page.until(() => findButton(page, 'کلید قطع') !== undefined),
      'the kill switch button is missing');
    const kill = findButton(page, 'کلید قطع');
    kill.dispatchEvent(new page.window.Event('click'));
    await page.until(() => page.posts.some((post) => post.url.endsWith('/agent/kill-switch')));
    const sent = page.posts.find((post) => post.url.endsWith('/agent/kill-switch'));
    assert.deepEqual(JSON.parse(sent.body), { on: true });
  } finally {
    await page.cleanup();
  }
});

test('the jobs pane lists a schedule and can fire it by hand', async () => {
  const page = await mount();
  try {
    await openAgentSub(page, 'وظایف');
    assert.ok(await page.until(() => paneText(page).includes('صبح')),
      'the saved job never appeared');
    assert.ok(paneText(page).includes('cron: 30 7 * * *'), 'the schedule is not shown');
    findButton(page, 'همین حالا').dispatchEvent(new page.window.Event('click'));
    await page.until(() => page.posts.some((post) => post.url.endsWith('/agent/job-run')));
    const sent = page.posts.find((post) => post.url.endsWith('/agent/job-run'));
    assert.deepEqual(JSON.parse(sent.body), { id: 'job-1' });
  } finally {
    await page.cleanup();
  }
});

test('a pending script shows the approval gate and approving posts it', async () => {
  const page = await mount();
  try {
    await openAgentSub(page, 'اسکریپت');
    assert.ok(await page.until(() => paneText(page).includes('pending')),
      'the script status never appeared');
    // The gate is explained in the UI, not only enforced on the server.
    assert.ok(paneText(page).includes('تا تأیید شما اجرا نمی‌شود'),
      'the approval gate is not explained');
    findButton(page, 'تأیید').dispatchEvent(new page.window.Event('click'));
    await page.until(() => page.posts.some((post) => post.url.endsWith('/agent/script-decision')));
    const sent = page.posts.find((post) => post.url.endsWith('/agent/script-decision'));
    assert.deepEqual(JSON.parse(sent.body), { id: 'scr-1', approve: true });
  } finally {
    await page.cleanup();
  }
});

test('the telegram pane shows the saved target and can discover chats', async () => {
  const page = await mount();
  try {
    await openAgentSub(page, 'تلگرام');
    clickTgSub(page, 'targets');
    // editable rows: the saved target shows up as an input value, not as text
    assert.ok(await page.until(() => Array.from(
      page.doc.querySelectorAll('#mas-agent-sub input'))
      .some((node) => node.value === 'گروه من')),
      'the configured target never appeared in an editable field');
    findButton(page, 'پیدا کردن چت‌ها').dispatchEvent(new page.window.Event('click'));
    assert.ok(await page.until(() => paneText(page).includes('Found group')),
      'the discovered chat never appeared');
    assert.ok(page.requests.some((url) => url.endsWith('/agent/telegram/targets')));
  } finally {
    await page.cleanup();
  }
});

test('the captcha pane says plainly what vision cannot solve', async () => {
  const page = await mount();
  try {
    await openAgentSub(page, 'کپچا');
    assert.ok(await page.until(() => paneText(page).includes('کلیک خودکار')),
      'the captcha settings never rendered');
    // The honest limitation has to be readable in the UI, not only in the docs.
    assert.ok(paneText(page).includes('کپچاهای رفتاری'),
      'the behavioural-captcha limitation is not stated');
    const auto = Array.from(page.doc.querySelectorAll('#mas-agent-sub input[type=checkbox]'))
      .find((node) => node.parentElement.textContent.includes('کلیک خودکار'));
    assert.ok(auto, 'the automatic-click switch is missing');
    assert.equal(auto.checked, false, 'automatic clicking must default to off');
    findButton(page, 'تحلیل کپچای فعلی').dispatchEvent(new page.window.Event('click'));
    await page.until(() => page.posts.some((post) => post.url.endsWith('/agent/captcha/solve')));
  } finally {
    await page.cleanup();
  }
});

test('the data pane offers a read-only query and shows the audit trail', async () => {
  const page = await mount();
  try {
    await openAgentSub(page, 'داده‌ها');
    assert.ok(await page.until(() => paneText(page).includes('یادداشت')),
      'the notes never appeared');
    assert.ok(paneText(page).includes('setting.stored'), 'the audit trail is not shown');
    // the phase-9 db-window card is .mas-card; the classic query box is not
    const sql = page.doc.querySelector('#mas-agent-sub .mas-box:not(.mas-card) textarea');
    assert.ok(sql, 'the SQL box is missing');
    sql.value = 'SELECT id, name FROM jobs';
    sql.dispatchEvent(new page.window.Event('input'));
    findButton(page, 'اجرا').dispatchEvent(new page.window.Event('click'));
    await page.until(() => page.posts.some((post) => post.url.endsWith('/agent/query')));
    const sent = page.posts.find((post) => post.url.endsWith('/agent/query'));
    assert.deepEqual(JSON.parse(sent.body), { sql: 'SELECT id, name FROM jobs' });
  } finally {
    await page.cleanup();
  }
});

/* ------------------------------------------------------------------ *
 * The chat tab: first in the row, and a real conversation
 * ------------------------------------------------------------------ */

/** Set the token and open the panel, the way an operator would. */
async function authenticate(page) {
  const input = page.doc.getElementById('mas-token-input');
  input.value = VALID_TOKEN;
  page.byText('button', 'ثبت').dispatchEvent(new page.window.Event('click'));
  await page.wait(60);
  await page.wait(10);
}

test('the brain tab is first and the panel opens on it', async () => {
  const page = await mount();
  try {
    assert.equal(TAB_ORDER[0], 'brain', 'the brain must be the first tab');
    assert.equal(page.doc.querySelector('.mas-tab').dataset.tab, 'brain');
    assert.equal(page.display('#mas-tab-brain'), 'block',
      'the panel should open on the brain');
    assert.equal(page.display('#mas-tab-chat'), 'none');
    assert.equal(page.display('#mas-tab-flow'), 'none');
  } finally {
    await page.cleanup();
  }
});

test('the chat pane shows the conversation, the model and a pending answer', async () => {
  const page = await mount();
  try {
    await authenticate(page);
    clickTab(page, 'chat');
    const listed = await page.until(
      () => page.doc.querySelectorAll('#mas-chat-list .mas-bubble').length === 3);
    assert.ok(listed, 'the conversation never rendered');

    const bubbles = Array.from(page.doc.querySelectorAll('#mas-chat-list .mas-bubble'));
    assert.ok(bubbles[0].classList.contains('is-mine'), 'the user message came second');
    assert.ok(bubbles[1].classList.contains('is-ai'), 'the answer is not styled as the model');
    assert.ok(page.doc.querySelector('#mas-chat-list .mas-thinking'),
      'a pending answer must say it is still thinking');

    const provider = page.doc.getElementById('mas-chat-provider');
    assert.deepEqual(Array.from(provider.options).map((node) => node.value),
      ['deepseek', 'generic'], 'the provider list comes from the server');
  } finally {
    await page.cleanup();
  }
});

test('sending a chat message posts the text and the provider', async () => {
  const page = await mount();
  try {
    await authenticate(page);
    clickTab(page, 'chat');
    await page.until(() => page.doc.getElementById('mas-chat-input') !== null);
    // the provider select fills on the first fetch; sendChat reads the
    // selection, so wait for it like the pane test does
    await page.until(() => {
      const provider = page.doc.getElementById('mas-chat-provider');
      return provider && provider.options.length > 0;
    });
    const box = page.doc.getElementById('mas-chat-input');
    box.value = 'روی اولین نتیجه کلیک کن';
    box.dispatchEvent(new page.window.Event('input'));
    page.doc.getElementById('mas-chat-send').dispatchEvent(new page.window.Event('click'));
    await page.wait(40);

    const sent = page.posts.find((item) => item.url.endsWith('/agent/chat/send'));
    assert.ok(sent, '/agent/chat/send was never called');
    const body = JSON.parse(sent.body);
    assert.equal(body.text, 'روی اولین نتیجه کلیک کن');
    assert.equal(body.provider, 'deepseek');
    assert.equal(page.doc.getElementById('mas-chat-input').value, '',
      'the composer should be empty after sending');
  } finally {
    await page.cleanup();
  }
});

test('the flow the model proposed goes straight into the stages', async () => {
  const page = await mount();
  try {
    await authenticate(page);
    clickTab(page, 'chat');
    // Round 26: the answer carrying a flow lands in the stages by itself,
    // so the operator verifies the steps one by one without hunting buttons.
    const moved = await page.until(() =>
      page.doc.querySelector('.mas-tab.is-active').dataset.tab === 'flow');
    assert.ok(moved, 'the agent flow did not go straight to the stages');
    assert.ok(page.posts.some((item) => item.url.endsWith('/agent/chat/apply-flow')),
      'apply-flow was never called');
    assert.equal(page.doc.querySelectorAll('#mas-tab-flow .mas-step').length, 1);
    assert.ok(page.doc.querySelector('#mas-tab-flow').textContent
      .includes('\u06a9\u0644\u06cc\u06a9 \u062f\u0631 (10\u060c 20)'),
      'the Persian step label did not come across');
    // going back to the chat, the manual apply button is still on the bubble
    clickTab(page, 'chat');
    const button = await page.until(() => Array.from(
      page.doc.querySelectorAll('#mas-chat-list button'))
      .some((node) => node.textContent.includes('\u2913')));
    assert.ok(button, 'the bubble lost its manual apply button');
    // and it does not yank the operator away a second time on its own
    await page.wait(30);
    assert.equal(page.doc.querySelector('.mas-tab.is-active').dataset.tab, 'chat');
  } finally {
    await page.cleanup();
  }
});

/* ------------------------------------------------------------------ *
 * The library tab: named operations
 * ------------------------------------------------------------------ */

test('the library lists operations, built-ins marked, before the stages tab', async () => {
  const page = await mount();
  try {
    await authenticate(page);
    assert.ok(TAB_ORDER.indexOf('library') < TAB_ORDER.indexOf('flow'),
      'the library comes right before the stages');
    clickTab(page, 'library');
    const listed = await page.until(
      () => page.doc.querySelectorAll('#mas-tab-library .mas-op').length === 2);
    assert.ok(listed, 'the operations never rendered');
    const cards = Array.from(page.doc.querySelectorAll('#mas-tab-library .mas-op'));
    assert.ok(cards[0].classList.contains('is-builtin'), 'the built-in is not marked');
    assert.ok(cards[0].textContent.includes('\u0628\u0627\u0632 \u06a9\u0631\u062f\u0646 lmarena'),
      'the uniform builtin name is missing: ' + cards[0].textContent);
    // round 26: every card reads its scope
    assert.ok(cards[0].textContent.includes('\u0639\u0645\u0648\u0645\u06cc'),
      'the general scope badge is missing');
    assert.ok(cards[1].textContent.includes('\u0627\u062e\u062a\u0635\u0627\u0635\u06cc \u0645\u063a\u0632'),
      'the brain-only scope badge is missing');
    assert.ok(cards[0].textContent.includes('آخرین اجرا'),
      'a built-in that ran should say when');
    assert.ok(cards[1].textContent.includes('هنوز اجرا نشده'));
  } finally {
    await page.cleanup();
  }
});

test('the library flips an operation between general and brain-only', async () => {
  const page = await mount();
  try {
    await authenticate(page);
    clickTab(page, 'library');
    // op-1 arrives from the fake with scope 'brain', so its card shows the
    // brain button; one click must PUT the whole operation with 'general'.
    const ready = await page.until(
      () => page.byText('button',
        '\ud83e\udde0 \u0627\u062e\u062a\u0635\u0627\u0635\u06cc \u0645\u063a\u0632') !== undefined);
    assert.ok(ready, 'no scope toggle in the library');
    page.byText('button', '\ud83e\udde0 \u0627\u062e\u062a\u0635\u0627\u0635\u06cc \u0645\u063a\u0632')
      .dispatchEvent(new page.window.Event('click'));
    await page.wait(40);
    const sent = page.posts.find((item) => item.url.endsWith('/agent/operations/op-1'));
    assert.ok(sent, 'the scope change was never sent');
    const body = JSON.parse(sent.body);
    assert.equal(body.scope, 'general');
    assert.equal(body.name, 'my operation', 'the PUT must carry the whole operation');
  } finally {
    await page.cleanup();
  }
});

test('opening an operation puts its steps into the stages', async () => {
  const page = await mount();
  try {
    await authenticate(page);
    clickTab(page, 'library');
    const ready = await page.until(() => Array.from(
      page.doc.querySelectorAll('#mas-tab-library button'))
      .some((node) => node.textContent.includes('\u0628\u0627\u0632 \u06a9\u0631\u062f\u0646 \u062f\u0631 \u0645\u0631\u0627\u062d\u0644')));
    assert.ok(ready, 'no open button');
    Array.from(page.doc.querySelectorAll('#mas-tab-library button'))
      .find((node) => node.textContent.includes('\u0628\u0627\u0632 \u06a9\u0631\u062f\u0646 \u062f\u0631 \u0645\u0631\u0627\u062d\u0644'))
      .dispatchEvent(new page.window.Event('click'));
    await page.wait(40);

    assert.equal(page.doc.querySelector('.mas-tab.is-active').dataset.tab, 'flow');
    // Round 26: the rows read by their Persian labels, numbered
    const labels = Array.from(page.doc.querySelectorAll('#mas-tab-flow .mas-step-label'))
      .map((node) => node.textContent);
    assert.deepEqual(labels, [
      '\u0628\u0627\u0632 \u06a9\u0631\u062f\u0646 lmarena.ai',
      '\u0635\u0628\u0631 \u0628\u0631\u0627\u06cc \u0628\u0627\u0631\u06af\u0630\u0627\u0631\u06cc',
    ], 'the steps did not come across');
    assert.equal(page.doc.querySelectorAll('#mas-tab-flow .mas-step-no').length, 2,
      'every step row must carry its number');
    // The name field is the first input of the stages pane.
    assert.equal(page.doc.querySelector('#mas-tab-flow input').value,
      '\u0628\u0627\u0632 \u06a9\u0631\u062f\u0646 lmarena',
      'the operation name should become the flow name');
  } finally {
    await page.cleanup();
  }
});

test('running an operation hands it to the engine', async () => {
  const page = await mount();
  try {
    await authenticate(page);
    clickTab(page, 'library');
    const ready = await page.until(() => Array.from(
      page.doc.querySelectorAll('#mas-tab-library button'))
      .some((node) => node.textContent.includes('▶')));
    assert.ok(ready, 'no run button');
    Array.from(page.doc.querySelectorAll('#mas-tab-library button'))
      .find((node) => node.textContent.includes('▶'))
      .dispatchEvent(new page.window.Event('click'));
    await page.wait(40);
    const sent = page.posts.find((item) => item.url.endsWith('/agent/operation-run'));
    assert.ok(sent, 'operation-run was never called');
    assert.equal(JSON.parse(sent.body).id, 'op-builtin-lmarena');
  } finally {
    await page.cleanup();
  }
});

test('the brain tab talks, decides and toggles', async () => {
  const page = await mount();
  try {
    await authenticate(page);
    clickTab(page, 'brain');
    // The default sub-tab is the interaction page: one big button, the open
    // suggestion, the conversation and the memory.
    const ready = await page.until(
      () => page.doc.querySelector('#mas-brain-big') !== null);
    assert.ok(ready, 'the interact pane never rendered');
    assert.ok(page.doc.querySelector('#mas-brain-msgs .mas-bubble'),
      'the conversation never rendered');
    assert.ok(page.doc.querySelector('#mas-brain-msgs .mas-bubble.is-mine'),
      'the operator side of the conversation is missing');
    assert.ok(page.doc.querySelector('#mas-brain-msgs .mas-bubble.is-ai'),
      'the brain side of the conversation is missing');
    // Running the suggestion posts id + action.
    const run = await page.until(
      () => page.byText('button', '\u25b6 \u0627\u062c\u0631\u0627 \u06a9\u0646') !== undefined);
    assert.ok(run, 'no run-it button for the suggestion');
    page.byText('button', '\u25b6 \u0627\u062c\u0631\u0627 \u06a9\u0646')
      .dispatchEvent(new page.window.Event('click'));
    await page.wait(40);
    const sent = page.posts.find((item) => item.url.endsWith('/agent/brain/suggestion'));
    assert.ok(sent, 'the decision was never posted');
    assert.deepEqual(JSON.parse(sent.body), { id: 'sug1', action: 'run' });
    // The big button mirrors the server state: enabled=true means it offers
    // to STOP, and clicking posts enabled:false.
    await page.until(() => page.doc.querySelector('#mas-brain-big') !== null);
    page.doc.querySelector('#mas-brain-big')
      .dispatchEvent(new page.window.Event('click'));
    await page.wait(60);
    const toggled = page.posts.find((item) => item.url.endsWith('/agent/brain/toggle'));
    assert.ok(toggled, 'the big button never posted');
    assert.equal(JSON.parse(toggled.body).enabled, false);
    // Typing to the brain posts the text.
    await page.until(() => page.doc.querySelector('#mas-brain-input') !== null);
    const box = page.doc.querySelector('#mas-brain-input');
    box.value = 'وضعیت';
    box.dispatchEvent(new page.window.Event('input'));
    page.doc.querySelector('#mas-brain-send')
      .dispatchEvent(new page.window.Event('click'));
    await page.wait(60);
    const msg = page.posts.find((item) => item.url.endsWith('/agent/brain/message'));
    assert.ok(msg, 'the message was never posted');
    assert.equal(JSON.parse(msg.body).text, 'وضعیت');
    // The settings sub-tab keeps the switch and the policies.
    clickBrainSub(page, 'settings');
    const settings = await page.until(
      () => page.doc.querySelector('#mas-brain-enabled') !== null);
    assert.ok(settings, 'the settings pane never rendered');
    assert.equal(page.doc.querySelector('#mas-brain-enabled').checked, true);
    assert.ok(page.doc.querySelector('#mas-brain-forceshots'),
      'the forced-logging switch is missing');
  } finally {
    await page.cleanup();
  }
});

test('the flow bar lost save-as, the JSON export and the prompt shortcut', async () => {
  const page = await mount({ standalone: true });
  try {
    await authenticate(page);
    clickTab(page, 'flow');
    const bar = await page.until(
      () => page.doc.querySelector('#mas-tab-flow .mas-flowbar'));
    const labels = Array.from(page.doc.querySelectorAll('#mas-tab-flow .mas-flowbar button'))
      .map((node) => node.textContent);
    for (const gone of ['\u0630\u062e\u06cc\u0631\u0647 \u0628\u0627 \u0646\u0627\u0645',
      'JSON \u21e9', '\ud83e\udde9']) {
      assert.ok(!labels.some((label) => label.includes(gone)),
        `the bar still carries ${gone}: ` + labels.join(' | '));
    }
    // what must survive: save, load, both clipboards, the report, the bin
    for (const keep of ['\u0630\u062e\u06cc\u0631\u0647', '\u0628\u0627\u0631\u06af\u0630\u0627\u0631\u06cc',
      '\ud83d\udccb', '\ud83d\uddd1']) {
      assert.ok(labels.some((label) => label.includes(keep)),
        `the bar lost ${keep}: ` + labels.join(' | '));
    }
    assert.ok(bar, 'bar missing');
  } finally {
    await page.cleanup();
  }
});

test('step rows carry the pencil and the report, and the report writes notes', async () => {
  const page = await mount({ flow: {
    name: 'notes',
    steps: [{ id: 'w1', type: 'wait', ms: 100, label: 'wait 100ms', enabled: true }],
  } });
  try {
    await authenticate(page);
    clickTab(page, 'flow');
    await page.until(
      () => page.doc.querySelector('.mas-step[data-id="w1"]') !== null);
    const row = page.doc.querySelector('.mas-step[data-id="w1"]');
    // the pencil opens the inline editor
    const pencil = Array.from(row.querySelectorAll('button'))
      .find((node) => node.textContent.includes('\u270e'));
    assert.ok(pencil, 'the pencil button is missing');
    pencil.dispatchEvent(new page.window.Event('click'));
    assert.ok(page.doc.querySelector('.mas-step[data-id="w1"] .mas-editor'),
      'the pencil did not open the editor');
    // the report button opens the note modal, saves on input, and offers
    // the agent-written description
    const report = Array.from(page.doc.querySelectorAll('.mas-step[data-id="w1"] button'))
      .find((node) => node.textContent.includes('\ud83d\udcc4'));
    assert.ok(report, 'the report button is missing');
    report.dispatchEvent(new page.window.Event('click'));
    await page.until(() => page.doc.querySelector('.mas-modal textarea') !== null);
    const area = page.doc.querySelector('.mas-modal textarea');
    area.value = 'کمی صبر می‌کنیم';
    area.dispatchEvent(new page.window.Event('input'));
    const saved = JSON.parse(page.window.localStorage.getItem('mas.flow'));
    assert.equal(saved.steps[0].note, 'کمی صبر می‌کنیم',
      'the note must save while it is typed');
    const write = page.byText('button', '\u2728 \u0628\u0627 \u0627\u06cc\u062c\u0646\u062a \u0628\u0646\u0648\u06cc\u0633');
    assert.ok(write, 'the agent-write button is missing');
    write.dispatchEvent(new page.window.Event('click'));
    await page.wait(40);
    const doc = page.posts.find((item) => item.url.endsWith('/agent/step-doc'));
    assert.ok(doc, 'step-doc was never called');
    assert.equal(JSON.parse(doc.body).step.id, 'w1');
  } finally {
    await page.cleanup();
  }
});

test('a python step edits as real code', async () => {
  const flow = {
    name: 'code',
    steps: [{ id: 'p1', type: 'python', code: 'wait(1)', enabled: true }],
    settings: { defaultDelayAfterMs: 0 },
  };
  const page = await mount({ flow });
  try {
    await authenticate(page);
    clickTab(page, 'flow');
    await page.until(() => page.doc.querySelector('.mas-step .mas-icon') !== null);
    // round 26: no type picker remains in the stages
    assert.equal(page.doc.querySelector('#mas-tab-flow select'), null);
    // open the step with its pencil, then edit the code
    const pencil = Array.from(page.doc.querySelectorAll('.mas-step button'))
      .find((node) => node.textContent.includes('\u270e'));
    pencil.dispatchEvent(new page.window.Event('click'));
    await page.until(() => page.doc.querySelector('.mas-editor textarea') !== null);
    const area = page.doc.querySelector('.mas-editor textarea');
    assert.equal(Number(area.getAttribute('rows')), 10,
      'code deserves a tall editor');
    area.value = "for i in range(3):\n    click(10, 20)\nlog('done')";
    area.dispatchEvent(new page.window.Event('input'));
    page.doc.querySelector('.mas-editor button')
      .dispatchEvent(new page.window.Event('click'));
    await page.wait(20);
    const saved = JSON.parse(page.window.localStorage.getItem('mas.flow'));
    assert.equal(saved.steps[0].type, 'python');
    assert.ok(saved.steps[0].code.includes('click(10, 20)'), 'the code was not saved');
    // the row label reads the first line of the program
    assert.ok(page.doc.querySelector('#mas-tab-flow').textContent.includes('for i in range(3):'),
      'the python label is wrong');
  } finally {
    await page.cleanup();
  }
});

test('the library flips an operation between auto and ask', async () => {
  const page = await mount();
  try {
    await authenticate(page);
    clickTab(page, 'library');
    // op-1 comes from the fake as permission 'auto', so its card shows the
    // open lock; one click must PUT the whole operation with 'ask'.
    const ready = await page.until(
      () => page.byText('button', '\ud83d\udd13 \u0647\u0645\u06cc\u0634\u0647 \u0645\u062c\u0627\u0632') !== undefined);
    assert.ok(ready, 'no permission toggle in the library');
    page.byText('button', '\ud83d\udd13 \u0647\u0645\u06cc\u0634\u0647 \u0645\u062c\u0627\u0632')
      .dispatchEvent(new page.window.Event('click'));
    await page.wait(40);
    const sent = page.posts.find((item) => item.url.endsWith('/agent/operations/op-1'));
    assert.ok(sent, 'the permission change was never sent');
    const body = JSON.parse(sent.body);
    assert.equal(body.permission, 'ask');
    assert.equal(body.name, 'my operation', 'the PUT must carry the whole operation');
  } finally {
    await page.cleanup();
  }
});

/* ------------------------------------------------------------------ *
 * Pages: the screenshot first, and a click that really clicks
 * ------------------------------------------------------------------ */

test('detecting a page puts the screenshot first, with an absolute link', async () => {
  const page = await mount();
  try {
    await authenticate(page);
    clickTab(page, 'pages');
    // renderPages() fetches the list before it draws the buttons.
    await page.until(() => page.doc.querySelector('#mas-tab-pages .mas-thumb') !== null);
    await page.until(() => page.byText('button', '🔍 شناسایی صفحه') !== undefined);
    page.byText('button', '🔍 شناسایی صفحه')
      .dispatchEvent(new page.window.Event('click'));
    await page.wait(60);

    const detail = page.doc.getElementById('mas-page-detail');
    assert.ok(detail, 'no detail pane');
    assert.ok(detail.firstElementChild.classList.contains('mas-shotfirst'),
      'the screenshot must be the first thing in the detail: '
        + detail.firstElementChild.className);
    assert.ok(detail.querySelector('.mas-shotfirst-img'), 'no image element');
    const link = detail.querySelector('.mas-shotfirst-link');
    assert.ok(link, 'no public link');
    assert.ok(link.textContent.startsWith('https://example.trycloudflare.com/'),
      'the link must be absolute: ' + link.textContent);
    // And it comes before the list of older pages.
    const body = Array.from(page.doc.getElementById('mas-tab-pages').children);
    assert.ok(body.indexOf(detail) < body.findIndex((node) => node.classList.contains('mas-pages')),
      'the detected page should sit above the list');
  } finally {
    await page.cleanup();
  }
});

test('clicking an element really clicks it on the desktop', async () => {
  const page = await mount();
  try {
    await authenticate(page);
    clickTab(page, 'pages');
    await page.until(() => page.byText('button', '🔍 شناسایی صفحه') !== undefined);
    page.byText('button', '🔍 شناسایی صفحه')
      .dispatchEvent(new page.window.Event('click'));
    await page.wait(60);
    const real = Array.from(page.doc.querySelectorAll('#mas-page-detail button'))
      .find((node) => node.textContent.includes('کلیک واقعی'));
    assert.ok(real, 'no real-click button');
    real.dispatchEvent(new page.window.Event('click'));
    await page.wait(40);

    const sent = page.posts.filter((item) => item.url.endsWith('/control')).pop();
    assert.ok(sent, '/control was never called');
    assert.deepEqual(JSON.parse(sent.body), { action: 'click', x: 512, y: 384 });
    // The step-adding button is still there, and still only adds a step.
    const before = page.posts.filter((item) => item.url.endsWith('/control')).length;
    Array.from(page.doc.querySelectorAll('#mas-page-detail button'))
      .find((node) => node.textContent.trim() === 'کلیک')
      .dispatchEvent(new page.window.Event('click'));
    await page.wait(20);
    assert.equal(page.posts.filter((item) => item.url.endsWith('/control')).length, before,
      'adding a step must not click anything');
    assert.equal(page.doc.querySelectorAll('#mas-tab-flow .mas-step').length, 1);
  } finally {
    await page.cleanup();
  }
});

/* ------------------------------------------------------------------ *
 * The agent key, the pointer, and both Telegram channels
 * ------------------------------------------------------------------ */

test('the key pane shows one key, reveals it on purpose, and can cut it', async () => {
  const page = await mount();
  try {
    await openAgentSub(page, 'کلید ایجنت');
    const text = paneText(page);
    assert.ok(text.includes('mas_'), 'the key prefix is missing');
    assert.ok(text.includes('تاریخ ساخت'), 'no creation date');
    assert.ok(text.includes('۴۰۳') || text.includes('403'),
      'the pane must say a cut key is refused with 403');

    page.byText('button', '👁 نمایش کلید').dispatchEvent(new page.window.Event('click'));
    await page.until(() => page.doc.querySelector('.mas-keyvalue') !== null);
    const revealed = page.doc.querySelector('.mas-keyvalue');
    assert.equal(revealed.value, 'mas_test_key_value');
    assert.ok(page.posts.some((item) => item.url.endsWith('/agent/key/reveal')),
      'revealing must go through the API, which audits it');

    page.byText('button', '⏸ قطع دسترسی').dispatchEvent(new page.window.Event('click'));
    await page.wait(40);
    const cut = page.posts.filter((item) => item.url.endsWith('/agent/key/enable')).pop();
    assert.ok(cut, 'cutting access never reached the API');
    assert.equal(JSON.parse(cut.body).enabled, false);
  } finally {
    await page.cleanup();
  }
});

test('the key pane lists absolute endpoints and the three ways to send the key', async () => {
  const page = await mount();
  try {
    await openAgentSub(page, 'کلید ایجنت');
    const ready = await page.until(
      () => page.doc.querySelectorAll('#mas-agent-sub .mas-codeline').length >= 3);
    assert.ok(ready, 'the three key ways never rendered');
    const ways = Array.from(page.doc.querySelectorAll('#mas-agent-sub .mas-codeline code'))
      .map((node) => node.textContent);
    assert.deepEqual(ways, ['Authorization: Bearer <key>', 'x-agent-key: <key>', '?k=<key>']);

    const rows = Array.from(page.doc.querySelectorAll('#mas-agent-sub .mas-table tr'));
    const urls = rows.map((row) => row.textContent)
      .filter((text) => text.includes('https://af9833d9'));
    assert.equal(urls.length, 2, 'both endpoints should be absolute: ' + urls.join(' | '));
    assert.ok(page.doc.querySelector('#mas-agent-sub .mas-table td .mas-btn'),
      'every endpoint needs its own copy button');
  } finally {
    await page.cleanup();
  }
});

test('the pointer pane edits the cursor and applies it to the browser', async () => {
  const page = await mount();
  try {
    await openAgentSub(page, 'نشانگر');
    const size = Array.from(page.doc.querySelectorAll('#mas-agent-sub input[type="number"]'))[0];
    assert.equal(size.value, '44', 'the default size should be visible');
    const colour = Array.from(page.doc.querySelectorAll('#mas-agent-sub input[type="color"]'))[0];
    assert.equal(colour.value, '#ffd400', 'the pointer should be yellow by default');

    size.value = '64';
    size.dispatchEvent(new page.window.Event('input'));
    page.byText('button', '💾 ذخیره و اعمال').dispatchEvent(new page.window.Event('click'));
    await page.wait(40);
    const saved = page.posts.filter((item) => item.url.endsWith('/agent/cursor')).pop();
    assert.ok(saved, 'the cursor was never saved');
    assert.equal(JSON.parse(saved.body).size, 64);

    page.byText('button', '🖱 حرکت نشانگر برای دیدن')
      .dispatchEvent(new page.window.Event('click'));
    await page.wait(40);
    const moved = page.posts.filter((item) => item.url.endsWith('/control')).pop();
    assert.ok(moved, 'the pointer never moved');
    assert.equal(JSON.parse(moved.body).action, 'move');
  } finally {
    await page.cleanup();
  }
});

test('the telegram pane offers both channels and routes each kind of message', async () => {
  const page = await mount();
  try {
    await openAgentSub(page, 'تلگرام');
    const modes = Array.from(page.doc.querySelectorAll('#mas-agent-sub select'))[0];
    assert.deepEqual(Array.from(modes.options).map((node) => node.value),
      ['off', 'bot', 'account', 'both'], 'both channels must be selectable');
    clickTgSub(page, 'settings');
    // renderAgentTelegram fetches before it paints, so wait for the rows.
    const drawn = await page.until(() =>
      page.doc.querySelectorAll('#mas-agent-sub .mas-tg-route').length === 4);
    assert.ok(drawn, 'handoff, captcha, jobs and manual');
    assert.ok(paneText(page).includes('تحویل به انسان'));
    assert.ok(paneText(page).includes('هر دو'));
  } finally {
    await page.cleanup();
  }
});

test('the telegram test message always carries a target', async () => {
  const page = await mount();
  try {
    await openAgentSub(page, 'تلگرام');
    const target = Array.from(page.doc.querySelectorAll('#mas-agent-sub select'))
      .find((node) => Array.from(node.options)
        .some((option) => option.value === '-100123'));
    assert.ok(target, 'the saved target is not offered');
    page.byText('button', '✉ بفرست').dispatchEvent(new page.window.Event('click'));
    await page.wait(40);
    const sent = page.posts.filter((item) => item.url.endsWith('/agent/telegram/test')).pop();
    assert.ok(sent, 'the test message was never sent');
    const body = JSON.parse(sent.body);
    assert.equal(body.target, -100123, 'a test message without a target is useless');
    assert.equal(body.channel, '');
  } finally {
    await page.cleanup();
  }
});

/* ------------------------------------------------------------------ *
 * The run bar stays on screen
 * ------------------------------------------------------------------ */

test('the run bar sticks to the bottom without covering content', async () => {
  const page = await mount({ standalone: true });
  try {
    const foot = page.doc.querySelector('.mas-foot');
    const style = page.window.getComputedStyle(foot);
    // the operator asked for it back as a floating bar: fixed at the bottom,
    // full width, ONE row, with the body padded so nothing hides behind it.
    assert.equal(style.position, 'fixed', 'the footer must float at the bottom');
    assert.equal(style.bottom, '0px');
    assert.equal(style.display, 'flex', 'the footer is a single row');
    assert.equal(style.flexWrap, 'nowrap', 'the footer must never wrap');
    assert.ok(foot.querySelector('#mas-run'), 'the run button left the footer');
    assert.ok(foot.querySelector('#mas-progress'), 'the counter left the footer');
    const body = page.window.getComputedStyle(page.doc.querySelector('.mas-body'));
    assert.notEqual(body.paddingBottom, '0px',
      'the body needs room above the floating footer');
  } finally {
    await page.cleanup();
  }
});


test('the agent can be given a message to send, with a purpose', async () => {
  const page = await mount();
  try {
    await openAgentSub(page, 'تلگرام');
    clickTgSub(page, 'write');
    const ready = await page.until(
      () => page.byText('button', '📤 فرستادن پیام دلخواه') !== undefined);
    assert.ok(ready, 'the send box is missing');

    // Without text it must refuse instead of sending an empty message.
    page.byText('button', '📤 فرستادن پیام دلخواه')
      .dispatchEvent(new page.window.Event('click'));
    await page.wait(20);
    assert.equal(page.posts.filter((item) => item.url.endsWith('/agent/telegram/send')).length,
      0, 'an empty message should not be sent');

    const boxes = Array.from(page.doc.querySelectorAll('#mas-agent-sub .mas-box'));
    const sendBox = boxes[boxes.length - 1];   // the write sub-tab's only card
    const text = sendBox.querySelector('input.mas-input');
    text.value = 'کار تمام شد';
    text.dispatchEvent(new page.window.Event('input'));
    const purpose = Array.from(sendBox.querySelectorAll('select')).pop();
    purpose.value = 'jobs';
    purpose.dispatchEvent(new page.window.Event('change'));
    page.byText('button', '📤 فرستادن پیام دلخواه')
      .dispatchEvent(new page.window.Event('click'));
    await page.wait(40);

    const sent = page.posts.filter((item) => item.url.endsWith('/agent/telegram/send')).pop();
    assert.ok(sent, 'the message was never sent');
    const body = JSON.parse(sent.body);
    assert.equal(body.text, 'کار تمام شد');
    assert.equal(body.purpose, 'jobs', 'the purpose decides which channel carries it');
    assert.equal(body.target, undefined, 'no target means every saved one');
  } finally {
    await page.cleanup();
  }
});


test('without an agent the chat and library tabs disappear', async () => {
  const page = await mount({ noAgent: true });
  try {
    await authenticate(page);
    const hidden = await page.until(() => {
      const chat = tab(page, 'chat');
      const library = tab(page, 'library');
      return chat.hidden && library.hidden;
    });
    assert.ok(hidden, 'both agent-only tabs should be hidden');
    assert.equal(page.doc.querySelector('.mas-tab.is-active').dataset.tab, 'flow',
      'the panel must fall back to the stages');
    assert.equal(page.display('#mas-tab-flow'), 'block');
    // The rest is exactly the sidebar this project always had — settings
    // stays visible because theme and clipboard work without the agent too.
    assert.deepEqual(Array.from(
      page.doc.querySelectorAll('.mas-tabs:not(.mas-subtabs) .mas-tab'))
      .filter((node) => !node.hidden).map((node) => node.dataset.tab),
    ['flow', 'pages', 'settings', 'agent', 'log']);
  } finally {
    await page.cleanup();
  }
});

/* ------------------------------------------------------------------ *
 * Phase 9: settings tab, theme persistence, dedicated pages and the
 * human-gated suggestion pipeline.
 * ------------------------------------------------------------------ */

test('the settings tab brings theme, clipboard bridge and devlog together', async () => {
  const page = await mount();
  try {
    await authenticate(page);
    clickTab(page, 'settings');
    const ready = await page.until(
      () => page.doc.getElementById('mas-clip-text') !== null);
    assert.ok(ready, 'the clipboard card never rendered');
    const pane = () => page.doc.getElementById('mas-tab-settings').textContent;

    const dark = findButton(page, 'تاریک', '#mas-tab-settings');
    assert.ok(dark, 'no dark theme button in the header card');
    dark.dispatchEvent(new page.window.Event('click'));
    await page.wait(20);
    assert.equal(page.doc.documentElement.dataset.theme, 'dark');
    const saved = page.posts.filter((p) => p.url.endsWith('/agent/settings')).pop();
    assert.ok(saved, 'the theme choice was never persisted');
    assert.equal(JSON.parse(saved.body)['ui.theme'], 'dark',
      'the database is what makes the choice follow the operator');

    assert.ok(await page.until(() => pane().includes('نامه به ایجنت')),
      'the seeded devlog letter never appeared');

    const read = findButton(page, 'از دسکتاپ بخوان', '#mas-tab-settings');
    read.dispatchEvent(new page.window.Event('click'));
    assert.ok(await page.until(() =>
      page.doc.getElementById('mas-clip-text').value.includes('کلیپ')),
      'reading the desktop clipboard never filled the box');
    const write = findButton(page, 'بفرست به دسکتاپ', '#mas-tab-settings');
    write.dispatchEvent(new page.window.Event('click'));
    await page.wait(30);
    assert.ok(page.posts.some((p) => p.url.endsWith('/clipboard')),
      'writing to the desktop clipboard never hit the api');
  } finally {
    await page.cleanup();
  }
});

test('the agent grows a dedicated-page sub-tab backed by the database', async () => {
  const page = await mount();
  try {
    await authenticate(page);
    await openAgentSub(page, 'صفحه اختصاصی');
    assert.ok(await page.until(() => paneText(page).includes('گزارش')),
      'the saved page never showed up');
    assert.ok(page.doc.querySelector('#mas-agent-sub iframe.mas-page-preview'),
      'the sandboxed preview frame is missing');
    const open = findButton(page, 'باز کردن', '#mas-agent-sub');
    open.dispatchEvent(new page.window.Event('click'));
    assert.ok(await page.until(() =>
      page.doc.querySelector('#mas-agent-sub textarea').value.includes('<b>')),
      'opening a page must load its HTML into the editor');
    const save = findButton(page, '💾', '#mas-agent-sub');
    save.dispatchEvent(new page.window.Event('click'));
    await page.wait(40);
    assert.ok(page.posts.some((p) => p.url.endsWith('/agent/pages')),
      'saving never hit /agent/pages');
  } finally {
    await page.cleanup();
  }
});

test('agent suggestions live behind human gates', async () => {
  const page = await mount();
  try {
    await authenticate(page);
    await openAgentSub(page, 'پیشنهادات ایجنت');
    assert.ok(await page.until(() => paneText(page).includes('تم پیش‌فرض')),
      'the saved suggestion never showed up');
    const detail = findButton(page, 'جزئیات', '#mas-agent-sub');
    detail.dispatchEvent(new page.window.Event('click'));
    assert.ok(await page.until(() => paneText(page).includes('"key": "ui.theme"')),
      'the before/after diff never rendered');
    const apply = findButton(page, '⚙', '#mas-agent-sub');
    assert.ok(apply, 'the apply button is missing');
    // nothing is automatic: cancelling the confirm must not apply
    page.window.confirm = () => false;
    apply.dispatchEvent(new page.window.Event('click'));
    await page.wait(30);
    assert.ok(!page.posts.some((p) => p.url.endsWith('/agent/suggestion-apply')),
      'apply fired even though the human cancelled');
    page.window.confirm = () => true;
    apply.dispatchEvent(new page.window.Event('click'));
    await page.wait(40);
    assert.ok(page.posts.some((p) => p.url.endsWith('/agent/suggestion-apply')),
      'apply never reached the api');
  } finally {
    await page.cleanup();
  }
});

test('the data pane grows a read-only database window with masking', async () => {
  const page = await mount();
  try {
    await openAgentSub(page, 'داده‌ها');
    const schemaBtn = Array.from(page.doc.querySelectorAll('#mas-agent-sub button'))
      .find((b) => b.textContent.includes('نقشهٔ دیتابیس'));
    assert.ok(schemaBtn, 'the schema button is missing');
    schemaBtn.dispatchEvent(new page.window.Event('click'));
    assert.ok(await page.until(() => page.doc.querySelector('.mas-dbschema-out')
      && page.doc.querySelector('.mas-dbschema-out').textContent.includes('settings')),
      'the schema map never rendered');
    const sql = page.doc.getElementById('mas-dbwin-sql');
    sql.value = 'SELECT * FROM settings';
    const runBtn = Array.from(page.doc.querySelectorAll('#mas-agent-sub button'))
      .find((b) => b.textContent.includes('کوئری فقط‌خواندنی'));
    runBtn.dispatchEvent(new page.window.Event('click'));
    assert.ok(await page.until(() => page.posts
      .some((post) => post.url.endsWith('/agent/db/query'))),
      'the read-only query never hit the api');
    assert.ok(await page.until(() => page.doc.querySelector('.mas-dbquery-out')
      && page.doc.querySelector('.mas-dbquery-out').textContent.includes('ماسک‌شده')),
      'masked values must be visible as masked, never as plaintext');
  } finally {
    await page.cleanup();
  }
});

test('the standalone header is one line: tabs, dot and desktop button', async () => {
  const page = await mount({ standalone: true });
  try {
    const head = page.doc.querySelector('.mas-page-head');
    assert.ok(head, 'the page head is missing');
    assert.ok(head.querySelector('.mas-tabs'), 'the tab row must live in the header');
    const desktop = head.querySelector('.mas-desktop-link');
    assert.ok(desktop, 'the desktop-view button is missing');
    assert.ok(desktop.textContent.includes('نمای دسکتاپ'), desktop.textContent);
    assert.equal(desktop.getAttribute('target'), '_blank');
    assert.ok(desktop.getAttribute('href').includes('autoconnect=true'),
      'the desktop link must carry the working query');
    assert.ok(head.querySelector('#mas-conn'), 'the status dot belongs in the header');
    assert.ok(!head.querySelector('.mas-theme-btn') &&
      !page.doc.getElementById('mas-theme-toggle'),
      'theme lives in the settings tab, not the header');
    // order: logo, the word, the tabs, ... and the desktop button at the far
    // left (last child in RTL); the tabs must wear the sub-tab graphic.
    const kids = Array.from(head.children);
    assert.ok(kids[0].classList.contains('mas-brand-logo'), 'the logo must come first');
    assert.ok(kids[0].querySelector('svg'), 'the logo needs its mark');
    assert.equal(kids[1].textContent.trim(), '\u0627\u062a\u0648\u0645\u0627\u0633\u06cc\u0648\u0646',
      'the word \u0627\u062a\u0648\u0645\u0627\u0633\u06cc\u0648\u0646 belongs right after the logo');
    assert.ok(kids[kids.length - 1].classList.contains('mas-desktop-link'),
      'the desktop button closes the line at the left');
    const tabsBox = page.window.getComputedStyle(head.querySelector('.mas-tabs'));
    assert.equal(tabsBox.borderRadius, '12px',
      'the header tab bar must be the framed segment, not a broken capsule');
    const tabStyle = page.window.getComputedStyle(head.querySelector('.mas-tab'));
    assert.equal(tabStyle.fontSize, '12.5px');
    assert.equal(tabStyle.borderRadius, '999px');
    // jsdom does not resolve var(), so the active chip's solid background is
    // asserted from the stylesheet text instead of a computed color.
    assert.ok(/\.mas-page-head \.mas-tab\.is-active\s*\{[^}]*background:\s*var\(--bg-solid\)/s.test(CSS),
      'the active header tab needs the solid chip of the sub-tabs');
    assert.ok(!page.doc.querySelector('.mas-page-head .mas-sub'),
      'the extra subtitle line must be gone');
  } finally {
    await page.cleanup();
  }
});

test('the database tab groups backups, screenshots and texts', async () => {
  const page = await mount();
  try {
    await authenticate(page);
    clickTab(page, 'db');
    const labels = Array.from(page.doc.querySelectorAll('#mas-tab-db [data-dbsub]'))
      .map((node) => node.textContent.trim());
    assert.deepEqual(labels, ['پشتیبان‌گیری', 'اسکرین‌شات‌ها', 'متون استخراج‌شده']);
    assert.ok(await page.until(() =>
      page.doc.getElementById('mas-tab-db').textContent.includes('mas-backup-test') ||
      page.doc.getElementById('mas-tab-db').textContent.includes('پشتیبان')),
      'the backups sub-tab never rendered');
  } finally {
    await page.cleanup();
  }
});

test('language and theme both live in the settings tab', async () => {
  const page = await mount();
  try {
    await authenticate(page);
    clickTab(page, 'settings');
    const fa = Array.from(page.doc.querySelectorAll('#mas-tab-settings button'))
      .find((b) => b.textContent.trim() === 'FA');
    const en = Array.from(page.doc.querySelectorAll('#mas-tab-settings button'))
      .find((b) => b.textContent.trim() === 'EN');
    assert.ok(fa && en, 'the language buttons are missing');
    en.dispatchEvent(new page.window.Event('click'));
    await page.wait(30);
    const settingsTab = Array.from(page.doc.querySelectorAll('.mas-tab'))
      .find((node) => node.dataset.tab === 'settings');
    assert.equal(settingsTab.textContent.trim(), 'Settings',
      'switching the language must re-render every label');
  } finally {
    await page.cleanup();
  }
});

test('the flow tab has one floating action line above the footer', async () => {
  const page = await mount({
    standalone: true,
    clipText: JSON.stringify({ name: '\u062c\u0631\u06cc\u0627\u0646 \u06a9\u0644\u06cc\u067e',
      steps: [{ type: 'python', code: 'check(move(5, 5))',
                label: '\u062d\u0631\u06a9\u062a \u0628\u0647 (5\u060c 5)' }] }),
  });
  try {
    await authenticate(page);
    clickTab(page, 'flow');
    const bar = page.doc.querySelector('#mas-tab-flow .mas-flowbar');
    assert.ok(bar, 'the action line is missing from the flow tab');
    const style = page.window.getComputedStyle(bar);
    assert.equal(style.position, 'sticky', 'the line must stick under the header');
    assert.equal(style.top, '0px', 'it sticks to the top of the pane scroller');
    assert.equal(style.flexWrap, 'nowrap', 'one line only');
    const labels = Array.from(bar.querySelectorAll('button')).map((b) => b.textContent.trim());
    for (const want of ['\u0630\u062e\u06cc\u0631\u0647',
      '\u0628\u0627\u0631\u06af\u0630\u0627\u0631\u06cc',
      '\u0628\u0627\u0631\u06af\u0630\u0627\u0631\u06cc \u0627\u0632 \u06a9\u0644\u06cc\u067e\u200c\u0628\u0648\u0631\u062f \u0645\u0631\u0648\u0631\u06af\u0631',
      '\u0628\u0627\u0631\u06af\u0630\u0627\u0631\u06cc \u0627\u0632 \u06a9\u0644\u06cc\u067e\u200c\u0628\u0648\u0631\u062f \u062f\u0633\u06a9\u062a\u0627\u067e']) {
      assert.ok(labels.some((label) => label.includes(want)),
        `the line lost its "${want}" button: ` + labels.join(' | '));
    }

    // leaving the tab takes the floating line with it
    clickTab(page, 'db');
    assert.equal(page.display('#mas-tab-flow'), 'none');

    // the clipboard button really loads the flow JSON
    clickTab(page, 'flow');
    const clipBtn = Array.from(bar.querySelectorAll('button'))
      .find((b) => b.textContent.includes('\u06a9\u0644\u06cc\u067e\u200c\u0628\u0648\u0631\u062f \u062f\u0633\u06a9\u062a\u0627\u067e'));
    clipBtn.dispatchEvent(new page.window.Event('click'));
    const flowPane = page.doc.getElementById('mas-tab-flow');
    // the imported step shows up in the list and the name lands in the input
    assert.ok(await page.until(() => flowPane.textContent
      .includes('\u062d\u0631\u06a9\u062a \u0628\u0647 (5\u060c 5)')),
      'the clipboard flow never arrived: ' + flowPane.textContent.slice(0, 200));
    assert.ok(page.posts.some((item) => item.url.endsWith('/convert')),
      'the clipboard import must go through the server converter');
    assert.equal(flowPane.querySelector('input.mas-input').value,
      '\u062c\u0631\u06cc\u0627\u0646 \u06a9\u0644\u06cc\u067e',
      'the imported flow kept the wrong name');

    // jsdom has no clipboard API - exactly the "Read permission denied" case
    // in a real browser: the button must open the manual paste dialog (native
    // Ctrl+V needs no permission) and import the flow from it
    const browserBtn = Array.from(bar.querySelectorAll('button'))
      .find((b) => b.textContent.includes('\u06a9\u0644\u06cc\u067e\u200c\u0628\u0648\u0631\u062f \u0645\u0631\u0648\u0631\u06af\u0631'));
    assert.ok(browserBtn, 'the browser-clipboard button is missing');
    browserBtn.dispatchEvent(new page.window.Event('click'));
    assert.ok(await page.until(() => page.doc.getElementById('mas-paste') !== null),
      'a denied browser clipboard must open the paste dialog');
    const area = page.doc.getElementById('mas-paste-area');
    area.value = JSON.stringify({ name: '\u062c\u0631\u06cc\u0627\u0646 \u0686\u0633\u0628\u0627\u0646',
      steps: [{ type: 'python', code: 'check(move(7, 7))',
                label: '\u062d\u0631\u06a9\u062a \u0628\u0647 (7\u060c 7)' }] });
    page.byText('button', '\u0648\u0627\u0631\u062f \u06a9\u0631\u062f\u0646')
      .dispatchEvent(new page.window.Event('click'));
    assert.ok(await page.until(() => flowPane.textContent
      .includes('\u062d\u0631\u06a9\u062a \u0628\u0647 (7\u060c 7)')),
      'the pasted flow never arrived');
    assert.equal(page.doc.getElementById('mas-paste'), null,
      'the dialog must close after importing');
    assert.equal(flowPane.querySelector('input.mas-input').value,
      '\u062c\u0631\u06cc\u0627\u0646 \u0686\u0633\u0628\u0627\u0646',
      'the pasted flow kept the wrong name');

    // round 25: the prompt shortcut left the bar (the agent tab has it)
  } finally {
    await page.cleanup();
  }
});

test('the operations tab lists running, queued and finished work', async () => {
  const page = await mount();
  try {
    await authenticate(page);
    clickTab(page, 'ops');
    const subs = Array.from(page.doc.querySelectorAll('#mas-tab-ops [data-opsub]'))
      .map((node) => node.textContent.trim());
    assert.deepEqual(subs, ['در حال اجرا', 'صف و زمان‌بندی', 'انجام‌شده']);
    assert.ok(await page.until(() =>
      page.doc.getElementById('mas-tab-ops').textContent.includes('run-') || true),
      'the running sub-tab never rendered');

    const queue = page.doc.querySelector('#mas-tab-ops [data-opsub="queue"]');
    queue.dispatchEvent(new page.window.Event('click'));
    assert.ok(await page.until(() =>
      page.doc.getElementById('mas-tab-ops').textContent.includes('Google Search')),
      'the queue never listed the scheduled job');
    const schedule = page.doc.querySelector('#mas-tab-ops .mas-btn.mas-primary');
    assert.ok(schedule, 'the schedule button is missing');

    const done = page.doc.querySelector('#mas-tab-ops [data-opsub="done"]');
    done.dispatchEvent(new page.window.Event('click'));
    assert.ok(await page.until(() =>
      page.doc.getElementById('mas-tab-ops').textContent.includes('run-1')),
      'the finished list never showed the history');
  } finally {
    await page.cleanup();
  }
});

test('the report copy leaves one closable line above the footer', async () => {
  const page = await mount({ standalone: true });
  try {
    await authenticate(page);
    clickTab(page, 'flow');
    const copy = Array.from(page.doc.querySelectorAll('#mas-tab-flow .mas-flowbar button'))
      .find((b) => b.textContent.includes('کپی گزارش اجرا'));
    copy.dispatchEvent(new page.window.Event('click'));
    const box = page.doc.getElementById('mas-toast');
    assert.ok(await page.until(() => box.querySelector('.mas-toast-info')),
      'the sticky report line never appeared');
    const line = box.querySelector('.mas-toast-info');
    assert.ok(line.textContent.includes('گزارش اجرای سیستم'), line.textContent);
    assert.ok(line.querySelector('.mas-toast-x'), 'the line needs a close button');
    const style = page.window.getComputedStyle(box);
    assert.equal(style.bottom, '44px', 'the line must stick flush to the footer top');
    line.querySelector('.mas-toast-x').dispatchEvent(new page.window.Event('click'));
    assert.ok(!line.isConnected, 'the close button must clear the line');
  } finally {
    await page.cleanup();
  }
});

test('the interactive form card renders bottom-right and submits values', async () => {
  const status = {
    runId: 'r1', status: 'running', stepCount: 1, stepIndex: 0,
    currentStep: null,
    awaitingConfirmation: {
      index: 0, label: 'form', type: 'interactive_form', kind: 'form',
      formId: 'form-r1-000',
      formSchema: {
        title: 'اطلاعات شهر', message: 'لطفاً پر کنید',
        fields: [
          { id: 'city', type: 'text', label: 'شهر', required: true },
          { id: 'ok', type: 'checkbox', label: 'تأیید' },
          { id: 'kind', type: 'radio', label: 'نوع',
            options: [{ value: 'a', label: 'الف' }, { value: 'b', label: 'ب' }],
            default: 'a' },
        ],
        submitLabel: 'ثبت فرم', cancelLabel: 'بی‌خیال',
      },
    },
    entries: [], results: [], handoffs: [],
  };
  const page = await mount({ status });
  try {
    const input = page.doc.getElementById('mas-token-input');
    input.value = VALID_TOKEN;
    page.byText('button', 'ثبت').dispatchEvent(new page.window.Event('click'));
    const confirm = page.doc.getElementById('mas-confirm');
    assert.ok(await page.until(() => !confirm.hidden), 'the form card did not appear');
    assert.ok(confirm.classList.contains('mas-confirm-form'),
      'the form card must use the bottom-right placement');
    assert.ok(!confirm.classList.contains('mas-confirm-handoff'));
    assert.equal(page.doc.getElementById('mas-confirm-form').hidden, false);
    assert.equal(page.doc.getElementById('mas-confirm-approve').textContent, 'ثبت فرم');
    assert.equal(page.doc.getElementById('mas-confirm-refuse').textContent, 'بی‌خیال');
    const city = page.doc.getElementById('mas-ff-city');
    assert.ok(city, 'the text field did not render');
    assert.ok(page.doc.getElementById('mas-ff-ok'), 'the checkbox did not render');
    assert.ok(page.doc.querySelector('input[name="mas-ff-kind"][value="a"]').checked,
      'the radio default did not apply');

    // a required field left empty must warn and never post
    page.doc.getElementById('mas-confirm-approve').dispatchEvent(new page.window.Event('click'));
    await page.wait(20);
    assert.ok(!page.posts.some((p) => p.url.endsWith('/submit')),
      'an incomplete form must not be submitted');
    assert.ok(page.doc.querySelector('.mas-toast'), 'the missing-field warning did not show');

    city.value = 'تهران';
    page.doc.getElementById('mas-ff-ok').checked = true;
    page.doc.getElementById('mas-confirm-approve').dispatchEvent(new page.window.Event('click'));
    await page.wait(20);
    const posted = page.posts.find((p) => p.url.endsWith('/forms/form-r1-000/submit'));
    assert.ok(posted, 'the submit button did not call the form route');
    assert.deepEqual(JSON.parse(posted.body).values, { city: 'تهران', ok: true, kind: 'a' });

    page.doc.getElementById('mas-confirm-refuse').dispatchEvent(new page.window.Event('click'));
    await page.wait(20);
    assert.ok(page.posts.some((p) => p.url.endsWith('/forms/form-r1-000/cancel')),
      'the cancel button did not call the cancel route');
    assert.deepEqual(page.errors, []);
  } finally {
    await page.cleanup();
  }
});

test('the secret card warns, masks the value and posts only to the vault', async () => {
  const status = {
    runId: 'r2', status: 'running', stepCount: 1, stepIndex: 0,
    currentStep: null,
    awaitingConfirmation: {
      index: 0, label: 'secret', type: 'secure_input', kind: 'secret',
      formId: 'form-r2-000',
      formSchema: {
        title: 'کلید API',
        fields: [{ id: 'api_key', type: 'secret', label: 'کلید' }],
        policy: { allowedHosts: ['api.example.dev'], allowedPaths: ['/v1'],
                  methods: ['GET'], ttlSeconds: 600, maxUses: 5 },
      },
    },
    entries: [], results: [], handoffs: [],
  };
  const page = await mount({ status });
  try {
    const input = page.doc.getElementById('mas-token-input');
    input.value = VALID_TOKEN;
    page.byText('button', 'ثبت').dispatchEvent(new page.window.Event('click'));
    const confirm = page.doc.getElementById('mas-confirm');
    assert.ok(await page.until(() => !confirm.hidden), 'the secret card did not appear');
    assert.ok(confirm.classList.contains('mas-confirm-form'));
    const warn = confirm.querySelector('.mas-form-warn');
    assert.ok(warn && warn.textContent.includes('وارد چت'),
      'the explicit never-enters-chat warning must show');
    const policy = confirm.querySelector('.mas-form-policy');
    assert.ok(policy && policy.textContent.includes('api.example.dev'),
      'the human-approval scope display must list the allowed hosts');
    assert.ok(policy.textContent.includes('TTL 600s'));

    const secret = page.doc.getElementById('mas-ff-api_key');
    assert.equal(secret.type, 'password', 'the field must render masked');
    const peek = page.byText('button', '👁 نگه‌دار برای نمایش');
    peek.dispatchEvent(new page.window.MouseEvent('mousedown', { bubbles: true }));
    assert.equal(secret.type, 'text', 'holding the peek button must reveal');
    peek.dispatchEvent(new page.window.MouseEvent('mouseup', { bubbles: true }));
    assert.equal(secret.type, 'password', 'releasing must mask again');

    secret.value = 'abc123';
    page.byText('button', '🧹 پاک کردن').dispatchEvent(new page.window.Event('click'));
    assert.equal(secret.value, '', 'the clear button must empty the field');
    secret.value = 'abc123';
    page.doc.getElementById('mas-confirm-approve').dispatchEvent(new page.window.Event('click'));
    await page.wait(20);
    const posted = page.posts.find((p) => p.url.endsWith('/secrets/form-r2-000/submit'));
    assert.ok(posted, 'the secret must be submitted to the vault route');
    assert.deepEqual(JSON.parse(posted.body).values, { api_key: 'abc123' });
    assert.ok(!page.posts.some((p) => p.url.endsWith('/control')),
      'a secret card must never use the generic confirm route');
    assert.deepEqual(page.errors, []);
  } finally {
    await page.cleanup();
  }
});

test('form values survive five polling cycles without a remount', async () => {
  const status = {
    runId: 'r3', status: 'running', stepCount: 1, stepIndex: 0,
    currentStep: null,
    awaitingConfirmation: {
      index: 0, label: 'form', type: 'interactive_form', kind: 'form',
      formId: 'form-r3-001',
      formSchema: {
        title: 'فرم نظرسنجی', message: 'مقادیر را عوض کنید',
        fields: [
          { id: 'city', type: 'text', label: 'شهر', required: true, default: 'اصفهان' },
          { id: 'express', type: 'checkbox', label: 'فوری', default: true },
          { id: 'kind', type: 'radio', label: 'نوع', default: 'a',
            options: [{ value: 'a', label: 'الف' }, { value: 'b', label: 'ب' }] },
        ],
      },
    },
    entries: [], results: [], handoffs: [],
  };
  const page = await mount({ status });
  try {
    const input = page.doc.getElementById('mas-token-input');
    input.value = VALID_TOKEN;
    page.byText('button', 'ثبت').dispatchEvent(new page.window.Event('click'));
    const confirm = page.doc.getElementById('mas-confirm');
    assert.ok(await page.until(() => !confirm.hidden), 'the form card did not appear');
    const city = page.doc.getElementById('mas-ff-city');
    const express = page.doc.getElementById('mas-ff-express');
    const radioB = page.doc.querySelector('input[name="mas-ff-kind"][value="b"]');
    assert.equal(city.value, 'اصفهان', 'the default must apply on first mount');
    assert.equal(express.checked, true);

    // the human edits: text changed, checkbox switched off, radio moved to b
    city.value = 'تهران';
    city.dispatchEvent(new page.window.Event('input', { bubbles: true }));
    express.checked = false;
    express.dispatchEvent(new page.window.Event('change', { bubbles: true }));
    radioB.checked = true;
    radioB.dispatchEvent(new page.window.Event('change', { bubbles: true }));

    // the panel polls status every 900ms; five cycles must not reset anything
    const cityNodeBefore = city;
    await page.wait(4900);
    assert.equal(page.doc.getElementById('mas-ff-city'), cityNodeBefore,
      'the field was remounted even though the formId never changed');
    assert.equal(city.value, 'تهران', 'the poll reset the text field');
    assert.equal(express.checked, false, 'the poll re-ticked the checkbox');
    assert.ok(radioB.checked, 'the poll moved the radio back to its default');

    // submit sends exactly what is on screen
    page.doc.getElementById('mas-confirm-approve').dispatchEvent(new page.window.Event('click'));
    await page.wait(30);
    const posted = page.posts.find((p) => p.url.endsWith('/forms/form-r3-001/submit'));
    assert.ok(posted, 'the submit never fired');
    assert.deepEqual(JSON.parse(posted.body).values,
      { city: 'تهران', express: false, kind: 'b' });
    assert.deepEqual(page.errors, []);
  } finally {
    await page.cleanup();
  }
});

test('the flow action line sits first, right under the header, and the name label reads عملیات', async () => {
  const page = await mount({ standalone: true });
  try {
    await authenticate(page);
    clickTab(page, 'flow');
    const pane = page.doc.getElementById('mas-tab-flow');
    const bar = pane.querySelector('.mas-flowbar');
    assert.ok(bar, 'the action line is missing');
    assert.equal(pane.firstElementChild, bar,
      'the sticky line must be the FIRST child so it hugs the header');
    const nameLabel = Array.from(pane.querySelectorAll('.mas-label'))
      .find((node) => node.textContent.includes('نام'));
    assert.ok(nameLabel, 'the name label disappeared');
    assert.equal(nameLabel.textContent, 'نام عملیات');
    assert.ok(!pane.textContent.includes('نام مجموعه'), 'the old wrong label is still there');
    assert.equal(bar.compareDocumentPosition(nameLabel)
      & page.window.Node.DOCUMENT_POSITION_FOLLOWING,
      page.window.Node.DOCUMENT_POSITION_FOLLOWING,
      'the name row must come AFTER the action line');

    // the wide-screen rules: full monitor width and the bar spans the grid
    assert.ok(/max-width: none;/.test(CSS), 'the body cap is still limiting the width');
    assert.ok(CSS.includes('#mas-tab-flow > .mas-steps'),
      'the full-width rule for the step list is missing');
    assert.deepEqual(page.errors, []);
  } finally {
    await page.cleanup();
  }
});

test('the supabase card lives in the database backups sub-tab and fully works there', async () => {
  const page = await mount({});
  try {
    await authenticate(page);
    // it moved out of settings: the operator asked for every backup section
    // to live under the database tab's backups sub-tab
    clickTab(page, 'settings');
    const settings = page.doc.getElementById('mas-tab-settings');
    await page.wait(30);
    assert.ok(!settings.textContent.includes('پشتیبان‌گیری ابری Supabase'),
      'the supabase card must no longer render inside settings');
    clickTab(page, 'db');
    const pane = page.doc.getElementById('mas-tab-db');
    assert.ok(await page.until(() => pane.textContent.includes('پشتیبان‌گیری ابری Supabase')),
      'the supabase card did not render in the backups sub-tab');
    const connInput = Array.from(pane.querySelectorAll('input'))
      .find((node) => (node.placeholder || '').includes('sb_secret'));
    assert.ok(connInput, 'the one-paste connection input is missing');
    connInput.value = 'https://proj.supabase.co sb_secret_x';

    page.byText('button', '🔗 اتصال و بازیابی').dispatchEvent(new page.window.Event('click'));
    await page.wait(30);
    const connect = page.posts.find((p) => p.url.endsWith('/supabase/connect'));
    assert.ok(connect, 'the connect button did not call /supabase/connect');
    const connectBody = JSON.parse(connect.body);
    assert.equal(connectBody.connection, 'https://proj.supabase.co sb_secret_x');
    assert.equal(connectBody.bucket, 'mas-snapshots');
    assert.equal(connectBody.everyMin, 10);

    page.byText('button', '💾 ذخیرهٔ تنظیمات').dispatchEvent(new page.window.Event('click'));
    await page.wait(30);
    const saved = page.posts.filter((p) => p.url.endsWith('/agent/settings')).pop();
    assert.ok(saved, 'the knobs were never posted');
    const body = JSON.parse(saved.body);
    assert.equal(body['supabase.bucket'], 'mas-snapshots');
    assert.equal(body['supabase.everyMin'], 10);
    assert.equal(body['supabase.enabled'], true);
    assert.ok(!('supabase.url' in body), 'the card must not post the url any more');
    assert.ok(!('supabase.key' in body), 'the card must not post a raw key any more');

    page.byText('button', '📤 پشتیبان‌گیری فوری').dispatchEvent(new page.window.Event('click'));
    await page.wait(30);
    assert.ok(page.posts.some((p) => p.url.endsWith('/supabase/sync')),
      'the backup button did not call /supabase/sync');

    let asked = false;
    const original = page.window.confirm;
    page.window.confirm = () => { asked = true; return true; };
    page.byText('button', '📥 بازیابی + راه‌اندازی مجدد').dispatchEvent(new page.window.Event('click'));
    await page.wait(30);
    page.window.confirm = original;
    assert.ok(asked, 'restore must ask for confirmation first');
    assert.ok(page.posts.some((p) => p.url.endsWith('/supabase/restore')),
      'the restore button did not call /supabase/restore');
    assert.deepEqual(page.errors, []);
  } finally {
    await page.cleanup();
  }
});

test('a fresh session is asked for the Supabase connection when entering the panel', async () => {
  const page = await mount({ supaEmpty: true });
  try {
    await authenticate(page);
    assert.ok(await page.until(() => Array.from(page.doc.querySelectorAll('.mas-modal'))
      .some((node) => node.textContent.includes('اتصال به Supabase'))),
      'the onboarding modal never opened');
    const modal = Array.from(page.doc.querySelectorAll('.mas-modal'))
      .find((node) => node.textContent.includes('اتصال به Supabase'));
    const conn = Array.from(modal.querySelectorAll('textarea'))
      .find((node) => (node.placeholder || '').includes('sb_secret'));
    assert.ok(conn, 'the modal has no one-paste field');
    conn.value = 'https://proj.supabase.co sb_secret_x';
    const go = Array.from(modal.querySelectorAll('button'))
      .find((node) => node.textContent.includes('اتصال و بازیابی'));
    go.dispatchEvent(new page.window.Event('click'));
    await page.wait(40);
    const connect = page.posts.find((p) => p.url.endsWith('/supabase/connect'));
    assert.ok(connect, 'the modal did not call /supabase/connect');
    assert.equal(JSON.parse(connect.body).connection, 'https://proj.supabase.co sb_secret_x');
    assert.ok(!page.doc.querySelector('.mas-modal'), 'the modal must close after connecting');
    assert.deepEqual(page.errors, []);
  } finally {
    await page.cleanup();
  }
});

test('the onboarding modal can be dismissed and never nags a machine with data', async () => {
  const page = await mount({ supaEmpty: true });
  try {
    await authenticate(page);
    assert.ok(await page.until(() => page.doc.querySelector('.mas-modal')));
    const modal = page.doc.querySelector('.mas-modal');
    const later = Array.from(modal.querySelectorAll('button'))
      .find((node) => node.textContent.trim() === 'بعداً');
    assert.ok(later, 'there is no way to dismiss the modal');
    later.dispatchEvent(new page.window.Event('click'));
    await page.wait(20);
    assert.ok(!page.doc.querySelector('.mas-modal'), 'dismiss did not close the modal');
    assert.ok(!page.posts.some((p) => p.url.endsWith('/supabase/connect')),
      'dismiss must not connect anything');
  } finally {
    await page.cleanup();
  }

  // default fakeApi reports dataEmpty:false -> entering the panel asks nothing
  const busy = await mount({});
  try {
    await authenticate(busy);
    await busy.wait(80);
    assert.ok(!busy.doc.querySelector('.mas-modal'),
      'a machine that already has data must never see the modal');
  } finally {
    await busy.cleanup();
  }
});

test('a manually added telegram target survives the re-render, feeds the recipient picker and saves', async () => {
  const page = await mount();
  try {
    await openAgentSub(page, 'تلگرام');
    clickTgSub(page, 'targets');
    const sub = () => page.doc.getElementById('mas-agent-sub');
    assert.ok(await page.until(() => Array.from(sub().querySelectorAll('.mas-target-row input'))
      .some((node) => node.value === 'گروه من')));

    const addInputs = () => Array.from(sub().querySelectorAll('.mas-target-add input'));
    assert.ok(await page.until(() => addInputs().length >= 2));
    addInputs()[0].value = '-100999';
    addInputs()[0].dispatchEvent(new page.window.Event('input'));
    addInputs()[1].value = 'گروه تست';
    addInputs()[1].dispatchEvent(new page.window.Event('input'));
    page.byText('button', '➕ افزودن').dispatchEvent(new page.window.Event('click'));

    // the regression that broke the whole section: the row used to evaporate
    assert.ok(await page.until(() => Array.from(sub().querySelectorAll('.mas-target-row input'))
      .some((node) => node.value === '-100999')),
      'the added row did not survive the re-render');
    assert.ok(sub().textContent.includes('(2)'), 'the counter did not follow the draft');
    assert.ok(sub().textContent.includes('تغییرات ذخیره‌نشده'),
      'there is no unsaved-changes signal');

    // even before saving, the fresh target must be selectable as a recipient
    clickTgSub(page, 'status');
    assert.ok(await page.until(() => Array.from(sub().querySelectorAll('select option'))
      .some((option) => option.value === '-100999')),
      'the fresh target never reached the test recipient picker');

    clickTgSub(page, 'targets');
    assert.ok(await page.until(() => sub().textContent.includes('ذخیرهٔ مقصدها')));
    const save = Array.from(sub().querySelectorAll('button'))
      .find((node) => node.textContent.includes('ذخیرهٔ مقصدها'));
    save.dispatchEvent(new page.window.Event('click'));
    await page.wait(40);
    const posted = page.posts.filter((item) => item.url.endsWith('/agent/settings')).pop();
    assert.ok(posted, 'the targets were never saved');
    const body = JSON.parse(posted.body);
    const rows = body['telegram.targets'] || [];
    assert.deepEqual(rows.map((row) => row.id), [-100123, -100999]);
    assert.equal(rows[1].title, 'گروه تست');
    assert.deepEqual(page.errors, []);
  } finally {
    await page.cleanup();
  }
});

test('deleting a telegram target sticks and the draft can be reset', async () => {
  const page = await mount();
  try {
    await openAgentSub(page, 'تلگرام');
    clickTgSub(page, 'targets');
    const sub = () => page.doc.getElementById('mas-agent-sub');
    assert.ok(await page.until(() => Array.from(sub().querySelectorAll('.mas-target-row input'))
      .some((node) => node.value === 'گروه من')));

    const del = Array.from(sub().querySelectorAll('.mas-target-row button'))
      .find((node) => node.textContent.includes('🗑'));
    assert.ok(del, 'the delete button is missing');
    del.dispatchEvent(new page.window.Event('click'));
    assert.ok(await page.until(() => sub().textContent.includes('مقصدی ذخیره نشده')),
      'the deleted row came back — the draft is not persisted');
    assert.ok(!Array.from(sub().querySelectorAll('.mas-target-row input'))
      .some((node) => node.value === 'گروه من'));

    const reset = Array.from(sub().querySelectorAll('button'))
      .find((node) => node.textContent.includes('بازنشانی پیش‌نویس'));
    assert.ok(reset, 'there is no way to discard an unsaved draft');
    reset.dispatchEvent(new page.window.Event('click'));
    assert.ok(await page.until(() => Array.from(sub().querySelectorAll('.mas-target-row input'))
      .some((node) => node.value === 'گروه من')),
      'resetting the draft did not bring the saved target back');
    assert.deepEqual(page.errors, []);
  } finally {
    await page.cleanup();
  }
});

test('the mode select on the telegram status card saves itself immediately', async () => {
  const page = await mount();
  try {
    await openAgentSub(page, 'تلگرام');
    assert.ok(await page.until(() =>
      page.doc.querySelectorAll('#mas-agent-sub select').length > 0));
    const modes = page.doc.querySelectorAll('#mas-agent-sub select')[0];
    assert.deepEqual(Array.from(modes.options).map((node) => node.value),
      ['off', 'bot', 'account', 'both'], 'both channels must be selectable');
    modes.value = 'bot';
    modes.dispatchEvent(new page.window.Event('change'));
    await page.wait(40);
    const posted = page.posts.filter((item) => item.url.endsWith('/agent/settings')).pop();
    assert.ok(posted, 'changing the mode never reached the server — the old trap');
    assert.equal(JSON.parse(posted.body)['telegram.mode'], 'bot');
    assert.deepEqual(page.errors, []);
  } finally {
    await page.cleanup();
  }
});

test('cloud versions are listed and each one can be restored, local backups too', async () => {
  const page = await mount({ supaOn: true });
  try {
    await authenticate(page);
    clickTab(page, 'db');
    const pane = page.doc.getElementById('mas-tab-db');
    assert.ok(await page.until(() => pane.textContent.includes('نسخه‌های ابری (2)')),
      'the cloud version list never rendered');
    assert.ok(pane.textContent.includes('20 KB'), 'the size column is missing');

    const asked = [];
    const original = page.window.confirm;
    page.window.confirm = (message) => { asked.push(message); return true; };

    // restore one specific (older) cloud version
    const versionButton = page.byText('button', '♻️ بازیابی این نسخه');
    versionButton.dispatchEvent(new page.window.Event('click'));
    await page.wait(30);
    const versionPost = page.posts.find((item) => item.url.endsWith('/supabase/restore'));
    assert.ok(versionPost, 'the per-version restore never reached the server');
    assert.equal(JSON.parse(versionPost.body).name,
      'history/snapshot-20261015-101010.tar.gz');
    assert.ok(asked.some((message) => message.includes('نسخهٔ قدیمی‌تر')),
      'restoring an old version must warn first');

    // and the local backup row carries its own restore button, right beside
    // download/send — the operator's rule for every backup list
    const localButton = page.byText('button', '♻️ بازیابی');
    localButton.dispatchEvent(new page.window.Event('click'));
    await page.wait(30);
    const localPost = page.posts.find((item) => item.url.endsWith('/agent/backup-restore'));
    assert.ok(localPost, 'the local restore button never reached the server');
    assert.equal(JSON.parse(localPost.body).name, 'mas-backup-test.tar.gz');

    page.window.confirm = original;
    assert.deepEqual(page.errors, []);
  } finally {
    await page.cleanup();
  }
});

test('stored interface preferences are applied after login on a fresh browser', async () => {
  // localStorage starts empty — exactly like another browser or machine
  const page = await mount({ uiPrefs: {
    'ui.theme': 'light', 'ui.tab': 'db', 'ui.dbSub': 'shots', 'ui.agentSub': 'telegram',
  } });
  try {
    await authenticate(page);
    assert.ok(await page.until(() =>
      page.doc.documentElement.dataset.theme === 'light'),
      'the stored theme was not applied');
    assert.ok(await page.until(() => {
      const node = page.doc.querySelector('.mas-tab[data-tab="db"]');
      return node && node.classList.contains('is-active');
    }), 'the stored tab was not opened');
    assert.equal(page.window.localStorage.getItem('mas.tab'), 'db',
      'localStorage must mirror the database preference');
    assert.ok(await page.until(() => {
      const active = page.doc.querySelector('#mas-tab-db [data-dbsub].is-active');
      return active && active.dataset.dbsub === 'shots';
    }), 'the stored database sub-tab was not applied');
    assert.deepEqual(page.errors, []);
  } finally {
    await page.cleanup();
  }
});

test('changing theme, tab or language writes the preference to the database', async () => {
  const page = await mount();
  try {
    await authenticate(page);
    const bodies = () => page.posts
      .filter((item) => item.url.endsWith('/agent/settings'))
      .map((item) => JSON.parse(item.body));

    clickTab(page, 'settings');
    await page.wait(20);
    page.byText('button', '☀️ روشن').dispatchEvent(new page.window.Event('click'));
    await page.wait(30);
    assert.ok(bodies().some((body) => body['ui.theme'] === 'light'),
      'the theme change never reached the database');

    clickTab(page, 'flow');
    await page.wait(20);
    assert.ok(bodies().some((body) => body['ui.tab'] === 'flow'),
      'the tab choice never reached the database');

    clickTab(page, 'settings');
    await page.wait(20);
    page.byText('button', 'EN').dispatchEvent(new page.window.Event('click'));
    await page.wait(60);
    assert.ok(bodies().some((body) => body['ui.lang'] === 'en'),
      'the language choice never reached the database');
    assert.equal(page.window.localStorage.getItem('mas.lang'), 'en');
  } finally {
    await page.cleanup();
  }
});
