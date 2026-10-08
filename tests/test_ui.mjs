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
function fakeApi(requests, posts = [], statusPayload = null) {
  return async (url, options = {}) => {
    const path = String(url);
    requests.push(path);
    if ((options.method || 'GET').toUpperCase() === 'POST') {
      posts.push({ url: path, body: options.body });
    }
    const token = (options.headers || {})['X-Automation-Token'];
    const json = (body) => ({ ok: true, status: 200, text: async () => JSON.stringify(body) });
    if (path.endsWith('/info')) {
      return json({ authRequired: true, viewport: { width: 1366, height: 768 },
                    engineBusy: false, detector: 'x11+cdp', cdpAvailable: false });
    }
    if (token !== VALID_TOKEN) {
      return { ok: false, status: 401,
               text: async () => '{"error":"missing or wrong X-Automation-Token header"}' };
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
async function mount({ readyStateComplete = true, standalone = false, flow = null,
                      status: statusPayload = null } = {}) {
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
  // know it is the whole page rather than an overlay inside noVNC.
  if (standalone) window.document.body.classList.add('mas-standalone');

  const timers = [];
  const realSetInterval = globalThis.setInterval;
  const realClearInterval = globalThis.clearInterval;
  const globals = {
    window, document: window.document, navigator: window.navigator,
    localStorage: window.localStorage, HTMLElement: window.HTMLElement,
    Element: window.Element, Node: window.Node, Event: window.Event,
    CustomEvent: window.CustomEvent, getComputedStyle: window.getComputedStyle.bind(window),
    requestAnimationFrame: window.requestAnimationFrame
      ? window.requestAnimationFrame.bind(window) : (fn) => setTimeout(fn, 0),
    fetch: fakeApi(requests, posts, statusPayload),
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
    const pane = page.doc.getElementById('mas-tab-flow');
    assert.ok(pane, 'flow tab missing');
    assert.ok(pane.querySelector('.mas-steps'), 'step list was not rendered');
    assert.ok(pane.querySelector('.mas-empty'), 'the empty-state hint is missing');
    assert.ok(page.byText('button', 'افزودن گام'), 'the add-step button is missing');
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

test('the panel starts collapsed and opens from the toggle', async () => {
  const page = await mount();
  try {
    assert.equal(page.display('#mas-panel'), 'none', 'the panel must start collapsed');
    assert.notEqual(page.display('#mas-toggle'), 'none', 'the toggle arrow must be visible');

    page.doc.getElementById('mas-toggle').dispatchEvent(new page.window.Event('click'));
    assert.equal(page.display('#mas-panel'), 'flex', 'the toggle did not open the panel');
    assert.equal(page.display('#mas-toggle'), 'none', 'the toggle should hide once open');

    page.doc.querySelector('.mas-head .mas-icon').dispatchEvent(new page.window.Event('click'));
    assert.equal(page.display('#mas-panel'), 'none', 'the close button did not collapse it');
  } finally {
    await page.cleanup();
  }
});

test('tabs swap panes and only one is visible', async () => {
  const page = await mount();
  try {
    page.doc.getElementById('mas-toggle').dispatchEvent(new page.window.Event('click'));
    const tabs = Array.from(page.doc.querySelectorAll('.mas-tab'));
    assert.deepEqual(tabs.map((n) => n.dataset.tab),
      ['flow', 'record', 'pages', 'shots', 'texts', 'ai', 'log']);

    tabs[5].dispatchEvent(new page.window.Event('click'));  // AI tab
    assert.equal(page.display('#mas-tab-ai'), 'block');
    assert.equal(page.display('#mas-tab-flow'), 'none');
    assert.ok(page.doc.querySelector('#mas-tab-ai textarea'), 'the import box is missing');

    tabs[3].dispatchEvent(new page.window.Event('click'));  // screenshots
    assert.equal(page.display('#mas-tab-shots'), 'block');
    assert.equal(page.display('#mas-tab-ai'), 'none');

    tabs[4].dispatchEvent(new page.window.Event('click'));  // extracted texts
    assert.equal(page.display('#mas-tab-texts'), 'block');
    assert.equal(page.display('#mas-tab-shots'), 'none');
  } finally {
    await page.cleanup();
  }
});

test('adding a step through the UI creates a row', async () => {
  const page = await mount();
  try {
    page.doc.getElementById('mas-toggle').dispatchEvent(new page.window.Event('click'));
    const pane = page.doc.getElementById('mas-tab-flow');
    const select = pane.querySelector('select');
    select.value = 'click';
    page.byText('button', 'افزودن گام').dispatchEvent(new page.window.Event('click'));

    const rows = pane.querySelectorAll('.mas-step');
    assert.equal(rows.length, 1, 'no step row appeared');
    assert.ok(rows[0].textContent.includes('click 0,0'), `unexpected row: ${rows[0].textContent}`);
    assert.equal(pane.querySelector('.mas-empty'), null, 'the empty hint should be gone');
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

test('the panel declares a dark colour scheme so dropdowns do not turn white', async () => {
  const page = await mount();
  try {
    const root = page.doc.getElementById('mas-root');
    assert.equal(page.window.getComputedStyle(root).colorScheme, 'dark',
      'without color-scheme:dark Chrome paints the opened <select> popup white');
  } finally {
    await page.cleanup();
  }
});

test('step list shows no numbering and options are styled dark', async () => {
  const page = await mount();
  try {
    const list = page.doc.querySelector('.mas-steps');
    assert.equal(page.window.getComputedStyle(list).listStyleType, 'none');
    assert.ok(page.window.getComputedStyle(page.doc.querySelector('select.mas-input'))
      .backgroundColor.startsWith('rgb(18, 21, 26)'), 'the select must not be white');
  } finally {
    await page.cleanup();
  }
});

async function enableRecording(page) {
  page.doc.getElementById('mas-toggle').dispatchEvent(new page.window.Event('click'));
  const recordTab = Array.from(page.doc.querySelectorAll('.mas-tab'))[1];
  recordTab.dispatchEvent(new page.window.Event('click'));
  const toggle = Array.from(page.doc.querySelectorAll('#mas-tab-record button'))[0];
  toggle.dispatchEvent(new page.window.Event('click'));
  await page.wait(10);
  assert.equal(page.display('#mas-record-layer'), 'block', 'record layer did not open');
}

test('recording Persian keystrokes becomes a paste step, not type', async () => {
  const page = await mount();
  try {
    await enableRecording(page);
    const layer = page.doc.getElementById('mas-record-layer');
    const key = (k) => layer.dispatchEvent(
      new page.window.KeyboardEvent('keydown', { key: k, bubbles: true }));
    key('س'); key('ل'); key('ا'); key('م');
    key('Enter');  // non-printable: flushes the buffer into one step
    await page.wait(10);

    page.doc.querySelectorAll('.mas-tab')[0].dispatchEvent(new page.window.Event('click'));
    const types = Array.from(page.doc.querySelectorAll('.mas-step .mas-type'))
      .map((n) => n.textContent);
    assert.deepEqual(types, ['paste', 'key'],
      'Persian must use the clipboard path: ' + types.join(','));
    const label = page.doc.querySelector('.mas-step .mas-step-label').textContent;
    assert.ok(label.includes('سلام'), 'the recorded text was lost: ' + label);
  } finally {
    await page.cleanup();
  }
});

test('recording ASCII keystrokes still becomes a type step', async () => {
  const page = await mount();
  try {
    await enableRecording(page);
    const layer = page.doc.getElementById('mas-record-layer');
    const key = (k) => layer.dispatchEvent(
      new page.window.KeyboardEvent('keydown', { key: k, bubbles: true }));
    key('h'); key('i');
    key('Enter');
    await page.wait(10);

    page.doc.querySelectorAll('.mas-tab')[0].dispatchEvent(new page.window.Event('click'));
    const types = Array.from(page.doc.querySelectorAll('.mas-step .mas-type'))
      .map((n) => n.textContent);
    assert.deepEqual(types, ['type', 'key']);
  } finally {
    await page.cleanup();
  }
});

test('editing a step refreshes its label instead of keeping paste ""', async () => {
  const page = await mount();
  try {
    page.doc.getElementById('mas-toggle').dispatchEvent(new page.window.Event('click'));
    const pane = page.doc.getElementById('mas-tab-flow');
    const select = pane.querySelector('select');
    select.value = 'paste';
    page.byText('button', 'افزودن گام').dispatchEvent(new page.window.Event('click'));

    const row = pane.querySelector('.mas-step');
    assert.ok(row.textContent.includes('paste ""'), 'expected an empty label first');

    // Open the editor and type the text the user actually wants pasted.
    row.querySelector('.mas-step-label').dispatchEvent(new page.window.Event('click'));
    const field = (name) => Array.from(pane.querySelectorAll('.mas-editor label'))
      .find((l) => l.querySelector('span') && l.querySelector('span').textContent === name)
      .querySelector('input, textarea, select');
    const text = field('text');
    assert.ok(text, 'the text field is missing');
    text.value = 'سلام دنیا';
    text.dispatchEvent(new page.window.Event('input'));
    page.byText('button', 'OK').dispatchEvent(new page.window.Event('click'));

    const label = pane.querySelector('.mas-step .mas-step-label').textContent;
    assert.ok(label.includes('سلام دنیا'), 'the label stayed stale: ' + label);
  } finally {
    await page.cleanup();
  }
});

test('a hand written label survives editing the step', async () => {
  const page = await mount();
  try {
    page.doc.getElementById('mas-toggle').dispatchEvent(new page.window.Event('click'));
    const pane = page.doc.getElementById('mas-tab-flow');
    pane.querySelector('select').value = 'paste';
    page.byText('button', 'افزودن گام').dispatchEvent(new page.window.Event('click'));
    pane.querySelector('.mas-step .mas-step-label').dispatchEvent(new page.window.Event('click'));

    const field = (name) => Array.from(pane.querySelectorAll('.mas-editor label'))
      .find((l) => l.querySelector('span') && l.querySelector('span').textContent === name)
      .querySelector('input, textarea, select');
    const label = field('label');
    label.value = 'نوشتن در گوگل';
    label.dispatchEvent(new page.window.Event('input'));
    const text = field('text');
    text.value = 'hello';
    text.dispatchEvent(new page.window.Event('input'));
    page.byText('button', 'OK').dispatchEvent(new page.window.Event('click'));

    assert.equal(pane.querySelector('.mas-step .mas-step-label').textContent, 'نوشتن در گوگل');
  } finally {
    await page.cleanup();
  }
});

test('the AI tab sends the user request and an absolute origin', async () => {
  const page = await mount();
  try {
    const input = page.doc.getElementById('mas-token-input');
    input.value = VALID_TOKEN;
    page.byText('button', 'ثبت').dispatchEvent(new page.window.Event('click'));
    await page.wait(60);

    page.doc.getElementById('mas-toggle').dispatchEvent(new page.window.Event('click'));
    const aiTab = Array.from(page.doc.querySelectorAll('.mas-tab'))[5];
    aiTab.dispatchEvent(new page.window.Event('click'));
    await page.wait(10);

    const box = page.doc.getElementById('mas-ai-request');
    assert.ok(box, 'the request box is missing');
    box.value = 'روی اولین نتیجه کلیک کن';
    box.dispatchEvent(new page.window.Event('input'));

    // Switching tabs and back must not lose what the human typed.
    page.doc.querySelectorAll('.mas-tab')[0].dispatchEvent(new page.window.Event('click'));
    aiTab.dispatchEvent(new page.window.Event('click'));
    await page.wait(10);
    assert.equal(page.doc.getElementById('mas-ai-request').value, 'روی اولین نتیجه کلیک کن');

    const build = Array.from(page.doc.querySelectorAll('#mas-tab-ai button'))
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

test('the standalone panel opens full page and explains recording', async () => {
  const page = await mount({ standalone: true });
  try {
    // No collapse tab, and the panel is on screen without anyone clicking.
    assert.equal(page.display('#mas-toggle'), 'none', 'the collapse tab should be gone');
    assert.notEqual(page.display('#mas-panel'), 'none', 'the panel should be open');

    // A way back to the live browser view.
    const link = page.doc.querySelector('.mas-vnclink');
    assert.ok(link, 'no link to the browser view');
    assert.equal(link.getAttribute('href'), '../vnc.html');

    // Recording is the one thing that cannot work away from the browser view.
    page.doc.querySelectorAll('.mas-tab')[1].dispatchEvent(new page.window.Event('click'));
    const pane = page.doc.getElementById('mas-tab-record');
    assert.equal(pane.querySelectorAll('button').length, 1,
      'only Clear should remain: ' + pane.textContent);
    assert.ok(pane.querySelector('a[href="../vnc.html"]'), 'no link to record from');
    assert.ok(pane.textContent.length > 40, 'the explanation is missing');
  } finally {
    await page.cleanup();
  }
});

test('inside noVNC the sidebar offers the dedicated panel', async () => {
  const page = await mount();
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
    page.doc.getElementById('mas-toggle').dispatchEvent(new page.window.Event('click'));
    const aiTab = Array.from(page.doc.querySelectorAll('.mas-tab'))[5];
    aiTab.dispatchEvent(new page.window.Event('click'));
    await page.wait(10);

    const reply = page.doc.getElementById('mas-ai-reply');
    assert.ok(reply, 'no AI reply box');
    reply.value = '### ۱) چه فهمیدی\nباید جستجو کنم.\n```json\n'
      + '{"name":"t","steps":[{"type":"click","x":5,"y":5}]}\n```\n'
      + '### ۳) سؤال و نکته\nکدام نتیجه را کلیک کنم؟';
    reply.dispatchEvent(new page.window.Event('input'));

    const build = Array.from(page.doc.querySelectorAll('#mas-tab-ai button'))
      .find((b) => b.textContent.includes('🧩'));
    build.dispatchEvent(new page.window.Event('click'));
    await page.wait(60);

    const sent = page.posts.filter((p) => p.url.endsWith('/prompt')).pop();
    assert.ok(sent, '/prompt was never called');
    assert.ok(JSON.parse(sent.body).previousReply.includes('چه فهمیدی'),
      'the previous answer was not sent back');

    // Importing shows the model's prose, not just its code.
    const importBtn = Array.from(page.doc.querySelectorAll('#mas-tab-ai button'))
      .find((b) => b.textContent.includes('وارد کردن'));
    importBtn.dispatchEvent(new page.window.Event('click'));
    await page.wait(40);
    aiTab.dispatchEvent(new page.window.Event('click'));
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
    const input = page.doc.getElementById('mas-token-input');
    input.value = VALID_TOKEN;
    page.byText('button', 'ثبت').dispatchEvent(new page.window.Event('click'));
    await page.wait(80);
    const box = page.doc.getElementById('mas-library');
    assert.ok(await page.until(() => box.querySelectorAll('.mas-lib-row').length === 2),
      'the library never listed the flows: ' + box.textContent);

    const rows = box.querySelectorAll('.mas-lib-row');
    assert.equal(rows[0].dataset.name, 'Google Search');
    assert.ok(rows[0].textContent.includes('10'), 'step count is missing');

    const actions = rows[0].querySelectorAll('.mas-lib-actions button');
    assert.equal(actions.length, 3, 'expected load / run / delete');

    page.window.confirm = () => true;
    actions[2].dispatchEvent(new page.window.Event('click'));
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
    page.doc.getElementById('mas-toggle').dispatchEvent(new page.window.Event('click'));
    const shotsTab = Array.from(page.doc.querySelectorAll('.mas-tab'))[3];
    shotsTab.dispatchEvent(new page.window.Event('click'));

    const pane = page.doc.getElementById('mas-tab-shots');
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
    page.doc.getElementById('mas-toggle').dispatchEvent(new page.window.Event('click'));
    const textsTab = Array.from(page.doc.querySelectorAll('.mas-tab'))[4];
    textsTab.dispatchEvent(new page.window.Event('click'));

    const pane = page.doc.getElementById('mas-tab-texts');
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

test('record layer stays hidden until recording starts', async () => {
  const page = await mount();
  try {
    assert.equal(page.display('#mas-record-layer'), 'none');
  } finally {
    await page.cleanup();
  }
});
