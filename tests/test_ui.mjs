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
function fakeApi(requests, posts = []) {
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
      return json({ runId: null, status: 'idle', entries: [], history: [] });
    }
    return json({});
  };
}

/** Mount the sidebar in a fresh jsdom page and hand back the handles. */
async function mount({ readyStateComplete = true } = {}) {
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
    fetch: fakeApi(requests, posts),
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
    assert.equal(tabs.length, 5);

    tabs[3].dispatchEvent(new page.window.Event('click'));  // AI tab
    assert.equal(page.display('#mas-tab-ai'), 'block');
    assert.equal(page.display('#mas-tab-flow'), 'none');
    assert.ok(page.doc.querySelector('#mas-tab-ai textarea'), 'the import box is missing');
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

test('the AI tab sends the user request and an absolute origin', async () => {
  const page = await mount();
  try {
    const input = page.doc.getElementById('mas-token-input');
    input.value = VALID_TOKEN;
    page.byText('button', 'ثبت').dispatchEvent(new page.window.Event('click'));
    await page.wait(60);

    page.doc.getElementById('mas-toggle').dispatchEvent(new page.window.Event('click'));
    const aiTab = Array.from(page.doc.querySelectorAll('.mas-tab'))[3];
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

test('record layer stays hidden until recording starts', async () => {
  const page = await mount();
  try {
    assert.equal(page.display('#mas-record-layer'), 'none');
  } finally {
    await page.cleanup();
  }
});
