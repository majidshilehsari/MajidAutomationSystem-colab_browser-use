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

/** Mount the sidebar in a fresh jsdom page and hand back the handles. */
async function mount({ readyStateComplete = true } = {}) {
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
    fetch: async () => ({ ok: false, status: 401, text: async () => '{"error":"no token"}' }),
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
    dom, window, errors, importError,
    doc: window.document,
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

test('record layer stays hidden until recording starts', async () => {
  const page = await mount();
  try {
    assert.equal(page.display('#mas-record-layer'), 'none');
  } finally {
    await page.cleanup();
  }
});
