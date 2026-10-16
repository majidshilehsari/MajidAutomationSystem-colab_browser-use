/* Automation sidebar for the Colab browser.
 *
 * Loaded into noVNC's vnc.html by automation/server.py. It never touches
 * noVNC's own objects: it draws an overlay panel on the right, and every action
 * goes to the server API, which drives xdotool on the virtual display. That is
 * what lets a flow keep running after this tab is closed.
 */

import {
  API_PREFIX, createStep, labelFor, validateFlow, emptyFlow,
  moveStep, extractJson,
  normaliseImportedFlow, promptSteps, translate, formatElapsed, formatTime,
  stepDef, truncate, structuredCloneSafe, compact, formatDateTime,
} from './core.mjs';

/** Tab order, and the tab the panel opens on. */
// Round 26: the record tab is gone - steps are written by the agent or
// pasted in and translated; nothing is captured from raw clicks anymore.
const TAB_ORDER = ['brain', 'chat', 'library', 'flow', 'pages',
  'db', 'ops', 'settings', 'agent', 'log'];
const DEFAULT_TAB = 'brain';

/** The tab the panel opens on: the remembered one, else the first. */
function initialTab() {
  const saved = localStorage.getItem(LS.tab);
  return TAB_ORDER.includes(saved) ? saved : DEFAULT_TAB;
}

const LS = {
  token: 'mas.token', lang: 'mas.lang', flow: 'mas.flow', panel: 'mas.panelOpen',
  clip: 'mas.clipSync',
  request: 'mas.userRequest', reply: 'mas.aiReply',
  provider: 'mas.chatProvider', context: 'mas.chatContext', tab: 'mas.tab',
  theme: 'mas.theme', appliedChat: 'mas.appliedChat',
};

/* One helper owns every interface preference: localStorage for the instant
 * effect (and as the pre-login fallback), the database so the very same look
 * and layout waits for the operator on any other machine or browser after
 * the password is typed. */
let applyingServerPrefs = false;

function saveUiPref(name, value) {
  if (applyingServerPrefs || !state.token) return;
  api('/agent/settings', { method: 'POST', body: { ['ui.' + name]: String(value) } })
    .catch(() => { /* no agent here — the local choice still stands */ });
}


const state = {
  lang: localStorage.getItem(LS.lang) || 'fa',
  token: localStorage.getItem(LS.token) || '',
  theme: localStorage.getItem(LS.theme) || 'dark',
  flow: loadFlow(),
  info: null,
  status: { status: 'idle', entries: [] },
  pages: [],
  activePage: null,
  openStepId: null,
  logOffset: 0,
  lastRenderedIndex: -1,
  opsSub: 'running',
  brainSub: 'interact',
  brainDraft: '',
  brainMsgCount: 0,
  brainSugSeen: false,
  clipSyncOn: false,
  clipSyncTimer: null,
  clipLastLocal: null,
  clipLastRemote: null,
  lastResultsSig: '',
  // Timing for the step that is running right now, measured in the browser so a
  // clock difference with the Colab VM cannot make the counter go backwards.
  runSeenIndex: -1,
  runSeenAt: 0,
  runSeenRun: null,
  runSeenRunAt: 0,
  tokenRejected: false,
  userRequest: localStorage.getItem(LS.request) || '',
  aiReply: localStorage.getItem(LS.reply) || '',
  aiNotes: '',
  // Chat tab: the draft survives a re-render, the provider and the context
  // switch survive a tab change, and the answer arrives by polling.
  chatDraft: '',
  chatProvider: localStorage.getItem(LS.provider) || '',
  chatContext: localStorage.getItem(LS.context) === '1',
  chatProviders: [],
  // Library tab: operations, the step catalogue that documents them, and the
  // row currently being edited (a draft, so a cancel loses nothing saved).
  operations: [],
  stepTypes: {},
  editingOperation: null,
  deleteArmed: '',
  // Agent tab: a revealed key is kept in memory only, never in localStorage.
  revealedKey: '',
  apiIndex: null,
};

function loadFlow() {
  try {
    const raw = localStorage.getItem(LS.flow);
    if (raw) return JSON.parse(raw);
  } catch (_) { /* fall through to a fresh flow */ }
  return emptyFlow();
}

function persistFlow() {
  try { localStorage.setItem(LS.flow, JSON.stringify(state.flow)); } catch (_) { /* quota */ }
}

function t(key) { return translate(state.lang, key); }

/** True when running in the dedicated panel page instead of the noVNC overlay.
 *  The panel drives the same API, so everything works except capturing clicks,
 *  which needs the browser view to click on. */
function isStandalone() {
  return !!(document.body && document.body.classList.contains('mas-standalone'));
}

/* ------------------------------------------------------------------ *
 * API transport
 * ------------------------------------------------------------------ */

async function api(path, { method = 'GET', body } = {}) {
  const headers = { 'X-Automation-Token': state.token };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const response = await fetch(API_PREFIX + path, {
    method, headers, body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (_) { data = { raw: text }; }
  if (!response.ok) {
    const message = (data && data.error) || `HTTP ${response.status}`;
    const err = new Error(message);
    err.status = response.status;
    err.data = data;
    throw err;
  }
  return data;
}

/** <img src> cannot send our token header, so images are fetched as blobs. */
async function imageBlobUrl(path) {
  const response = await fetch(API_PREFIX + path, { headers: { 'X-Automation-Token': state.token } });
  if (!response.ok) return null;
  return URL.createObjectURL(await response.blob());
}

/* Manual paste dialog: reading the OS clipboard programmatically depends on
 * a browser permission that silently resets whenever the tunnel URL (origin)
 * changes, while a native Ctrl+V into a textarea needs no permission at all.
 * This dialog is therefore the guaranteed import path for flow JSON from the
 * operator's own machine. Resolves with the pasted text, or '' on cancel. */
function openPasteDialog() {
  return new Promise((resolve) => {
    const area = el('textarea', {
      id: 'mas-paste-area', class: 'mas-input mas-mono mas-paste-area', rows: 10,
      placeholder: t('flowPastePh'),
    });
    const overlay = el('div', { id: 'mas-paste', class: 'mas-confirm' }, [
      el('div', { class: 'mas-confirm-box' }, [
        el('h3', { text: '📋 ' + t('flowPasteTitle') }),
        el('p', { text: t('flowPasteHint') }),
        area,
        el('div', { class: 'mas-row' }, [
          el('button', {
            class: 'mas-btn mas-primary', type: 'button', text: t('flowPasteImport'),
            onClick: () => { const value = area.value; overlay.remove(); resolve(value); },
          }),
          el('button', {
            class: 'mas-btn', type: 'button', text: t('close'),
            onClick: () => { overlay.remove(); resolve(''); },
          }),
        ]),
      ]),
    ]);
    const host = document.getElementById('mas-root') || document.body;
    host.appendChild(overlay);
    area.focus();
  });
}

async function loadFlowFromClipboard(source) {
  // Two explicit sources and no silent fallback: one button that quietly
  // swapped "my Windows clipboard" for "the automation desktop clipboard"
  // reported confusing errors from whichever side happened to be empty.
  try {
    let text = '';
    if (source === 'browser') {
      if (navigator.clipboard && navigator.clipboard.readText) {
        try { text = await navigator.clipboard.readText(); } catch (err) { text = ''; }
      }
      if (!text) {
        // Permission denied, no API, or an empty clipboard: instead of a
        // dead-end English error, open the manual paste dialog - Ctrl+V
        // always works, on every origin, with no permission at all.
        toast(t('flowClipDenied'), 'warn');
        text = await openPasteDialog();
        if (!text) return;
      }
    } else {
      // a truly empty desktop clipboard arrives as the server's own message
      text = ((await api('/clipboard')).text) || '';
      if (!text) throw new Error(t('flowClipDesktopEmpty'));
    }
    let parsed;
    try { parsed = JSON.parse(text); } catch (err) { throw new Error(t('flowClipBad')); }
    const data = parsed && parsed.flow ? parsed.flow : parsed;
    // Round 26: the server converts whatever arrived in the old step
    // language first, then the shared importer validates the python flow
    const result = normaliseImportedFlow(await convertFlowServer(data));
    if (!result || !result.flow) throw new Error(t('flowClipBad'));
    state.flow = result.flow;
    persistFlow();
    renderFlow();
    toast(`${t('load')}: ${state.flow.name || t('flowClipLoaded')}`, 'ok');
    if (result.errors && result.errors.length) toast(result.errors.join(' | '), 'info');
  } catch (error) {
    toast(error.message || t('flowClipBad'), 'error');
  }
}

/* ------------------------------------------------------------------ *
 * DOM helpers
 * ------------------------------------------------------------------ */

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'html') node.innerHTML = value;
    else if (key === 'value') node.value = value;        // textarea ignores the attribute
    else if (key === 'checked') node.checked = Boolean(value);
    else if (key.startsWith('on') && typeof value === 'function') {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === 'dataset') Object.assign(node.dataset, value);
    else node.setAttribute(key, value === true ? '' : String(value));
  }
  for (const child of compact(children)) {
    node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
  }
  return node;
}

function button(label, onClick, extra = {}) {
  return el('button', { class: 'mas-btn', onClick, type: 'button', ...extra }, label);
}

function replace(node, ...children) {
  node.textContent = '';
  for (const child of compact(children)) {
    node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
  }
  return node;
}

function refreshToastLine(doc = document) {
  // The scrolling region must stop above a sticky line, so the body carries
  // a class while one is on screen. Timers pass their own document: after a
  // jsdom teardown the global one is already gone.
  if (!doc || !doc.body) return;
  const sticky = doc.querySelector('#mas-toast .mas-toast-sticky');
  doc.body.classList.toggle('mas-toastline', !!sticky);
}

function toast(message, kind = 'info', options = {}) {
  const box = document.getElementById('mas-toast');
  const item = el('div', {
    class: `mas-toast mas-toast-${kind}` + (options.sticky ? ' mas-toast-sticky' : ''),
    title: message,
  }, [
    el('span', { class: 'mas-toast-text', text: message }),
  ]);
  item.appendChild(el('button', {
    class: 'mas-toast-x', type: 'button', text: '✕', title: t('close'),
    onClick: () => { item.remove(); refreshToastLine(item.ownerDocument); },
  }));
  box.appendChild(item);
  refreshToastLine();
  // Sticky lines (the report summary) stay until the operator closes them.
  if (!options.sticky) {
    setTimeout(() => { item.remove(); refreshToastLine(item.ownerDocument); }, 4200);
  }
}

function startClipSync() {
  if (state.clipSyncTimer) return;
  state.clipSyncTimer = setInterval(async () => {
    if (!state.clipSyncOn) return;
    try {
      const remote = ((await api('/clipboard')).text) || '';
      if (remote !== state.clipLastRemote) {
        state.clipLastRemote = remote;
        if (remote && remote !== state.clipLastLocal
            && navigator.clipboard && navigator.clipboard.writeText) {
          try {
            await navigator.clipboard.writeText(remote);
            state.clipLastLocal = remote;
          } catch (err) { /* permission or focus: skip this tick */ }
        }
      }
      if (navigator.clipboard && navigator.clipboard.readText) {
        const local = await navigator.clipboard.readText();
        if (local && local !== state.clipLastLocal) {
          state.clipLastLocal = local;
          if (local !== state.clipLastRemote) {
            await api('/clipboard', { method: 'POST', body: { text: local } });
            state.clipLastRemote = local;
          }
        }
      }
    } catch (error) { /* an offline tick is not an error worth surfacing */ }
  }, 1500);
}

/* ------------------------------------------------------------------ *
 * Panel skeleton
 * ------------------------------------------------------------------ */

function buildPanel() {
  const root = el('div', { id: 'mas-root' });

  const toggle = el('button', {
    id: 'mas-toggle', class: 'mas-toggle', type: 'button',
    title: t('title'), onClick: () => setPanelOpen(true),
  }, '◀');

  // Chat first because talking to the model is now the entry point of the
  // panel, and the operations library right before the stages tab because an
  // operation is what you open *into* the stages.
  const tabs = TAB_ORDER.map((name) => el('button', {
    class: 'mas-tab', dataset: { tab: name }, type: 'button',
    text: t('tab' + name[0].toUpperCase() + name.slice(1)),
    onClick: () => selectTab(name),
  }));

  const panel = el('div', { id: 'mas-panel', class: 'mas-panel', hidden: true }, [
    el('div', { class: 'mas-head' }, [
      el('b', { text: t('title') }),
      el('span', { id: 'mas-conn', class: 'mas-dot', title: 'status' }),
      el('button', {
        class: 'mas-icon', type: 'button', text: '×', title: t('close'),
        onClick: () => setPanelOpen(false),
      }),
    ]),
    el('div', { class: 'mas-tabs' }, tabs),
    el('div', { id: 'mas-tokenbar', class: 'mas-tokenbar', hidden: true }, [
      el('div', { id: 'mas-tokenbar-text', class: 'mas-tokenbar-text' }),
      el('div', { class: 'mas-row' }, [
        el('input', {
          id: 'mas-token-input', class: 'mas-input', type: 'password',
          placeholder: 'AUTOMATION_TOKEN',
          onKeydown: (event) => { if (event.key === 'Enter') setToken(event.target.value); },
        }),
        el('button', {
          class: 'mas-btn mas-primary', type: 'button', text: t('tokenSubmit'),
          onClick: () => setToken(document.getElementById('mas-token-input').value),
        }),
      ]),
    ]),
    el('div', { class: 'mas-body' }, [
      el('section', { id: 'mas-tab-chat', class: 'mas-tabpane' }),
      el('section', { id: 'mas-tab-library', class: 'mas-tabpane', hidden: true }),
      el('section', { id: 'mas-tab-flow', class: 'mas-tabpane', hidden: true }),
      el('section', { id: 'mas-tab-pages', class: 'mas-tabpane', hidden: true }),
      el('section', { id: 'mas-tab-db', class: 'mas-tabpane', hidden: true }),
      el('section', { id: 'mas-tab-ops', class: 'mas-tabpane', hidden: true }),
      el('section', { id: 'mas-tab-brain', class: 'mas-tabpane', hidden: true }),
      el('section', { id: 'mas-tab-settings', class: 'mas-tabpane', hidden: true }),
      el('section', { id: 'mas-tab-agent', class: 'mas-tabpane', hidden: true }),
      el('section', { id: 'mas-tab-log', class: 'mas-tabpane', hidden: true }),
    ]),
    el('div', { class: 'mas-foot' }, [
      el('div', { id: 'mas-runpanel', class: 'mas-runpanel', hidden: true }),
      el('div', { class: 'mas-runbar' }, [
        el('button', {
          id: 'mas-run', class: 'mas-btn mas-primary', type: 'button', text: '▶ ' + t('run'),
          onClick: runFlow,
        }),
        el('button', {
          id: 'mas-pause', class: 'mas-btn', type: 'button', text: '⏸',
          onClick: () => control(state.status.status === 'paused' ? 'resume' : 'pause'),
        }),
        el('button', {
          id: 'mas-stop', class: 'mas-btn', type: 'button', text: '⏹',
          title: t('stop'), onClick: stopRun,
        }),
        el('span', { id: 'mas-progress', class: 'mas-progress', text: '0/0' }),
      ]),
      el('div', { id: 'mas-statusline', class: 'mas-statusline' }),
    ]),
  ]);

  const confirm = el('div', { id: 'mas-confirm', class: 'mas-confirm', hidden: true }, [
    el('div', { class: 'mas-confirm-box' }, [
      el('h3', { id: 'mas-confirm-title', text: t('confirmTitle') }),
      el('p', { id: 'mas-confirm-label' }),
      el('p', { id: 'mas-confirm-message' }),
      el('a', {
        id: 'mas-confirm-shot', hidden: true, target: '_blank', rel: 'noopener',
        class: 'mas-confirm-shot', text: t('viewChallengeShot'),
      }),
      el('div', { id: 'mas-confirm-form', hidden: true }),
      el('div', { class: 'mas-row' }, [
        el('button', {
          id: 'mas-confirm-approve',
          class: 'mas-btn mas-primary', type: 'button', text: t('approve'),
          onClick: () => confirmAction(true),
        }),
        el('button', {
          id: 'mas-confirm-refuse',
          class: 'mas-btn', type: 'button', text: t('refuse'),
          onClick: () => confirmAction(false),
        }),
      ]),
    ]),
  ]);

  root.append(toggle, panel, confirm, el('div', { id: 'mas-toast', class: 'mas-toast-box' }));
  document.body.appendChild(root);

  return root;
}

/* The desktop view's only chrome: one button on the right that opens the big
 * dedicated panel, plus a toast host (clipboard-sync messages still need a
 * place to land). Styled outside #mas-root because no root panel exists here. */
function buildDesktopLauncher() {
  const old = document.getElementById('mas-desktop-launcher');
  if (old) old.remove();
  document.body.appendChild(el('div', {
    id: 'mas-desktop-launcher', class: 'mas-desktop-launcher',
  }, [
    el('a', {
      class: 'mas-launcher-btn', href: 'automation/panel.html',
      target: '_blank', rel: 'noopener', title: t('openPanel'),
      text: '⧉ ' + t('openPanel'),
    }),
    // Round 27, item 6: the third panel is one click away from the desktop.
    el('a', {
      class: 'mas-launcher-btn', href: 'automation/human.html',
      target: '_blank', rel: 'noopener', title: t('humanPanelTitle'),
      text: '🙋 ' + t('humanPanelTitle'),
    }),
  ]));
  if (!document.getElementById('mas-toast')) {
    document.body.appendChild(
      el('div', { id: 'mas-toast', class: 'mas-toast-box' }));
  }
}

/* Round 27, item 6: build the dedicated human-action panel - the third
 * panel beside the desktop view and the big automation panel. Its whole job
 * is the moments a human is needed: the handoff card (challenge, manual
 * verification, confirmation, form, secret) rendered by the very same code
 * as the desktop view, plus the brain's open suggestion, plus the way back
 * to the other two panels. */
function buildHumanPanel() {
  const root = el('div', { id: 'mas-human-root' }, [
    el('header', { class: 'mas-human-head' }, [
      el('h1', { text: '🙋 ' + t('humanPanelTitle') }),
      el('p', { class: 'mas-hint', text: t('humanPanelHint') }),
      el('div', { class: 'mas-row mas-wrap' }, [
        el('a', {
          class: 'mas-launcher-btn', href: 'panel.html',
          target: '_blank', rel: 'noopener', title: t('openPanel'),
          text: '⧉ ' + t('openPanel'),
        }),
        el('a', {
          class: 'mas-launcher-btn',
          href: '../vnc.html?autoconnect=true&resize=scale&path=websockify',
          target: '_blank', rel: 'noopener', title: t('openDesktop'),
          text: '🌐 ' + t('openDesktop'),
        }),
      ]),
    ]),
    el('div', { id: 'mas-tokenbar', class: 'mas-tokenbar', hidden: true }, [
      el('div', { id: 'mas-tokenbar-text', class: 'mas-tokenbar-text' }),
      el('div', { class: 'mas-row' }, [
        el('input', {
          id: 'mas-token-input', class: 'mas-input', type: 'password',
          placeholder: 'AUTOMATION_TOKEN',
          onKeydown: (event) => { if (event.key === 'Enter') setToken(event.target.value); },
        }),
        el('button', {
          class: 'mas-btn mas-primary', type: 'button', text: t('tokenSubmit'),
          onClick: () => setToken(document.getElementById('mas-token-input').value),
        }),
      ]),
    ]),
    el('div', { id: 'mas-human-suggestion', class: 'mas-box mas-card', hidden: true }),
    el('div', { id: 'mas-human-empty', class: 'mas-human-empty' }, [
      el('p', { text: '✅ ' + t('humanPanelIdle') }),
    ]),
  ]);
  document.body.appendChild(root);
  if (!document.getElementById('mas-toast')) {
    document.body.appendChild(el('div', { id: 'mas-toast', class: 'mas-toast-box' }));
  }
}

/* The brain's open suggestion is a human choice too, so the dedicated panel
 * carries the same run/reject buttons as the brain tab. */
async function refreshHumanSuggestion() {
  const host = document.getElementById('mas-human-suggestion');
  if (!host) return;
  let data = null;
  try {
    data = await api('/agent/brain');
  } catch (error) {
    host.hidden = true;   // no agent or no token yet: the card simply stays away
    return;
  }
  const suggestion = data.suggestion;
  if (!suggestion || suggestion.expired) {
    host.hidden = true;
    const idleNote = document.getElementById('mas-human-empty');
    const card = document.getElementById('mas-desktop-handoff');
    if (idleNote) idleNote.hidden = Boolean(card && !card.hidden);
    return;
  }
  host.hidden = false;
  const idleNote = document.getElementById('mas-human-empty');
  if (idleNote) idleNote.hidden = true;
  replace(host,
    el('div', { class: 'mas-card-head' },
      [el('b', { text: '💡 ' + t('brainSuggestion') })]),
    el('p', { text: suggestion.name }),
    el('p', { class: 'mas-hint', text: t('humanPanelSuggestionHint') }),
    el('div', { class: 'mas-row mas-wrap' }, [
      button('▶ ' + t('brainRunIt'), () => brainDecide(suggestion.id, 'run'),
        { class: 'mas-btn mas-primary' }),
      button('✕ ' + t('brainReject'), () => brainDecide(suggestion.id, 'reject'),
        { class: 'mas-btn mas-danger' }),
    ]));
}

function setPanelOpen(open) {
  document.getElementById('mas-panel').hidden = !open;
  document.getElementById('mas-toggle').hidden = open;
  localStorage.setItem(LS.panel, open ? '1' : '0');
  saveUiPref('panel', open ? '1' : '0');
  if (open) {
    selectTab(currentTab());
    refreshStatus();
  }
}

function currentTab() {
  const active = document.querySelector('.mas-tab.is-active');
  return (active && active.dataset.tab) || initialTab();
}

function selectTab(name) {
  document.querySelectorAll('.mas-tab').forEach((node) => {
    node.classList.toggle('is-active', node.dataset.tab === name);
  });
  document.querySelectorAll('.mas-tabpane').forEach((node) => {
    node.hidden = node.id !== `mas-tab-${name}`;
  });
  if (name !== 'chat') stopChatPolling();
  if (name !== 'brain') stopBrainPolling();
  try { localStorage.setItem(LS.tab, name); } catch (_) { /* quota */ }
  saveUiPref('tab', name);
  if (name === 'chat') renderChat();
  if (name === 'library') renderOperations();
  if (name === 'pages') renderPages();
  if (name === 'log') renderLog();
  if (name === 'db') renderDatabase();
  if (name === 'ops') renderOps();
  if (name === 'brain') renderBrain();
  if (name === 'settings') renderSettings();
  if (name === 'agent') renderAgent();
  if (name === 'flow') renderFlow();
}

/** Hide the two tabs that need the coworker agent, when there is none.
 *
 *  A 404 means this deployment was built without an agent (`--no-agent`), which
 *  is a configuration, not a fault. A 401 only means no token yet, so the tabs
 *  stay: an operator who is about to type one must not watch them vanish.
 */
async function refreshAgentTabs() {
  // Round 27, item 6: the dedicated human panel has no tab row and no panes
  // to rebuild; its theme comes from localStorage at boot. Everything this
  // function would touch lives on the panel pages only.
  if (document.body.classList.contains('mas-human')) return true;
  let missing = false;
  try {
    const data = await api('/agent');
    // The interface preferences live in the database, so they follow the
    // operator to any browser; localStorage is only the pre-login fallback.
    const prefs = (data && data.settings) || {};
    const str = (value) => (typeof value === 'string' ? value : '');
    applyingServerPrefs = true;
    try {
      if (prefs['ui.theme'] === 'dark' || prefs['ui.theme'] === 'light') {
        applyTheme(prefs['ui.theme'], { save: false });
      }
      let langChanged = false;
      if ((prefs['ui.lang'] === 'fa' || prefs['ui.lang'] === 'en')
          && prefs['ui.lang'] !== state.lang) {
        state.lang = prefs['ui.lang'];
        try { localStorage.setItem(LS.lang, state.lang); } catch (_) { /* quota */ }
        langChanged = true;
      }
      if (TAB_ORDER.includes(str(prefs['ui.tab']))) {
        try { localStorage.setItem(LS.tab, prefs['ui.tab']); } catch (_) { /* quota */ }
      }
      if (str(prefs['ui.agentSub'])) state.agentSub = prefs['ui.agentSub'];
      if (str(prefs['ui.tgSub'])) state.tgSub = prefs['ui.tgSub'];
      if (str(prefs['ui.dbSub'])) state.dbSub = prefs['ui.dbSub'];
      if (prefs['ui.reportsKind'] !== undefined && prefs['ui.reportsKind'] !== null) {
        state.reportsKind = String(prefs['ui.reportsKind']);
      }
      if (str(prefs['ui.brainSub'])) state.brainSub = prefs['ui.brainSub'];
      let panelPref = null;
      if (prefs['ui.panel'] === '1' || prefs['ui.panel'] === '0') {
        panelPref = prefs['ui.panel'];
        try { localStorage.setItem(LS.panel, panelPref); } catch (_) { /* quota */ }
      }
      if (str(prefs['ui.chatProvider'])) {
        state.chatProvider = prefs['ui.chatProvider'];
        try { localStorage.setItem(LS.provider, state.chatProvider); } catch (_) { /* quota */ }
      }
      if (prefs['ui.chatContext'] === '1' || prefs['ui.chatContext'] === '0') {
        state.chatContext = prefs['ui.chatContext'] === '1';
        try { localStorage.setItem(LS.context, prefs['ui.chatContext']); } catch (_) { /* quota */ }
      }
      const standalone = document.body.classList.contains('mas-standalone');
      if (langChanged) {
        // rebuilds everything in the stored language and opens the stored tab
        rerenderAll();
      } else if (TAB_ORDER.includes(str(prefs['ui.tab']))
                 && prefs['ui.tab'] !== currentTab()) {
        selectTab(prefs['ui.tab']);
      }
      // the open/closed panel only follows an actually stored preference —
      // guessing here would slam shut a panel the operator just opened
      if (panelPref !== null && !standalone) setPanelOpen(panelPref === '1');
    } finally {
      applyingServerPrefs = false;
    }
  } catch (error) {
    missing = error.status === 404;
  }
  for (const name of ['chat', 'library', 'db', 'ops', 'brain']) {
    const node = document.querySelector(`.mas-tab[data-tab="${name}"]`);
    if (node) node.hidden = missing;
  }
  if (missing && ['chat', 'library', 'db', 'ops', 'brain'].includes(currentTab())) {
    selectTab('flow');
  }
  return !missing;
}

/* The standalone panel page ships an empty <header class="mas-page-head">;
 * this moves the title, the tab row, the status dot and - at the far left in
 * RTL - the desktop-view button into it. It runs after every full rebuild
 * (language switch) too, so the head always shows the current labels and the
 * fresh tab row replaces the moved-away old one. */
function wireStandaloneHead() {
  const toggle = document.getElementById('mas-toggle');
  if (toggle) toggle.hidden = true;
  const pageHead = document.querySelector('.mas-page-head');
  if (!pageHead) return;
  pageHead.textContent = '';
  const tabsNode = document.querySelector('#mas-root .mas-tabs');
  const conn = document.getElementById('mas-conn');
  pageHead.appendChild(el('span', {
    class: 'mas-brand-logo', title: t('title'),
    html: '<svg viewBox="0 0 28 28" width="26" height="26" aria-hidden="true">'
      + '<defs><linearGradient id="maslogo" x1="0" y1="0" x2="1" y2="1">'
      + '<stop offset="0" stop-color="#14b8a6"/><stop offset="1" stop-color="#0f766e"/>'
      + '</linearGradient></defs>'
      + '<rect x="1.5" y="1.5" width="25" height="25" rx="8" fill="url(#maslogo)"/>'
      + '<path d="M15.6 5.2 8.8 15.4h4.3l-1.1 7.4 7.2-10.6h-4.5z" fill="#fff"/></svg>',
  }));
  pageHead.appendChild(el('h1', { class: 'mas-page-title', text: t('title') }));
  if (tabsNode) pageHead.appendChild(tabsNode);
  if (conn) pageHead.appendChild(conn);
  pageHead.appendChild(el('a', {
    class: 'mas-btn mas-desktop-link',
    href: '../vnc.html?autoconnect=true&resize=scale&path=websockify',
    target: '_blank', rel: 'noopener', text: '🌐 ' + t('openDesktop'),
  }));
  pageHead.appendChild(el('a', {
    class: 'mas-btn mas-desktop-link',
    href: 'human.html',
    target: '_blank', rel: 'noopener', text: '🙋 ' + t('humanPanelTitle'),
  }));
}

function rerenderAll() {
  const standalone = isStandalone();
  document.getElementById('mas-root').remove();
  buildPanel();
  if (standalone) {
    // the rebuilt panel has to hand its tab row and status dot back to the
    // page head, and the dedicated page is never collapsed
    wireStandaloneHead();
    setPanelOpen(true);
  } else {
    setPanelOpen(localStorage.getItem(LS.panel) === '1');
  }
  selectTab(initialTab());
}

/* ------------------------------------------------------------------ *
 * Flow tab
 * ------------------------------------------------------------------ */

/* ------------------------------------------------------------------ *
 * Run progress: which step passed, which one is running, for how long
 * ------------------------------------------------------------------ */

const STATE_ICONS = { ok: '✓', error: '✕', skipped: '⊘', ignored: '⚠' };

/** Outcomes of the pass on screen, keyed by step index. */
function stepOutcomes() {
  const status = state.status || {};
  const pass = status.passIndex || 1;
  const map = new Map();
  (status.results || []).forEach((row) => {
    if ((row.passIndex || 1) === pass) map.set(row.index, row);
  });
  return map;
}

function finishedCount() {
  const status = state.status || {};
  const pass = status.passIndex || 1;
  return (status.results || []).filter((row) => (row.passIndex || 1) === pass).length;
}

/** Seconds the running step has been going, measured here rather than against
 *  the server clock, so a skew cannot make the counter jump or go negative. */
function currentStepSeconds() {
  if (!isRunning()) return 0;
  if (state.status.index !== state.runSeenIndex) {
    state.runSeenIndex = state.status.index;
    state.runSeenAt = Date.now();
  }
  return (Date.now() - state.runSeenAt) / 1000;
}

function formatSeconds(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return '--';
  if (seconds < 10) return `${seconds.toFixed(1)}s`;
  if (seconds < 60) return `${Math.round(seconds)}s`;
  return `${Math.floor(seconds / 60)}m ${Math.round(seconds) % 60}s`;
}

function stepBadge(index, outcomes) {
  if (isRunning() && state.status.index === index) {
    return el('span', { class: 'mas-state is-running', title: t('stateRunning') }, [
      el('span', { text: '▶' }),
      el('span', {
        class: 'mas-timer', id: 'mas-timer', text: formatSeconds(currentStepSeconds()),
      }),
    ]);
  }
  const row = outcomes.get(index);
  if (!row) {
    return el('span', { class: 'mas-state is-pending', text: '·', title: t('statePending') });
  }
  const time = typeof row.durationMs === 'number' ? `${row.durationMs}ms` : '';
  return el('span', {
    class: 'mas-state is-' + row.status,
    text: [STATE_ICONS[row.status] || '·', time].filter(Boolean).join(' '),
    title: row.error || t('state' + row.status[0].toUpperCase() + row.status.slice(1)),
  });
}

/** Total run time, anchored to the browser clock the first time we see a run. */
function runSeconds() {
  const status = state.status || {};
  if (!status.runId) return 0;
  if (status.runId !== state.runSeenRun) {
    state.runSeenRun = status.runId;
    state.runSeenRunAt = Date.now() - (status.elapsedMs || 0);
  }
  if (!isRunning()) return (status.elapsedMs || 0) / 1000;
  return (Date.now() - state.runSeenRunAt) / 1000;
}

/** Refresh only the ticking numbers, so no editor closes under the user. */
function tickTimers() {
  const step = document.getElementById('mas-timer');
  if (step) step.textContent = formatSeconds(currentStepSeconds());
  const total = document.getElementById('mas-run-clock');
  if (total) total.textContent = formatSeconds(runSeconds());
  const now = document.getElementById('mas-run-now');
  if (now) now.textContent = formatSeconds(currentStepSeconds());
}

// Round 26: whatever arrives written in the old JSON step language goes
// through the server converter first, so the panel only ever edits python.
async function convertFlowServer(data) {
  try {
    const converted = await api('/convert', { method: 'POST', body: data });
    if (converted && converted.flow) return converted.flow;
  } catch (_) { /* fall through: the importer reports honestly */ }
  return data;
}

/* Put a flow into the stages editor and show it, ready for the step-by-step
 * check the operator asked for. Returns false when nothing usable arrived. */
function applyFlowToStages(saved) {
  const { flow, errors } = normaliseImportedFlow(saved || {}, state.flow.viewport);
  if (!flow || !(flow.steps || []).length) {
    if (errors && errors.length) toast(errors.join(' | '), 'error');
    return false;
  }
  state.flow = flow;
  persistFlow();
  renderFlow();
  selectTab('flow');
  if (errors && errors.length) toast(errors[0], 'warn');
  return true;
}

function renderFlow() {
  const pane = document.getElementById('mas-tab-flow');
  const errors = validateFlow(state.flow);

  const nameRow = el('div', { class: 'mas-row' }, [
    el('label', { class: 'mas-label', text: t('flowName') }),
    el('input', {
      class: 'mas-input', value: state.flow.name,
      onInput: (event) => { state.flow.name = event.target.value; persistFlow(); },
    }),
  ]);

  // Round 26: no add-step button in the stages. The agent writes the steps
  // (chat output lands here directly) or a pasted flow is translated by the
  // server; the operator reviews, edits and reorders what arrived.

  const list = el('ol', { class: 'mas-steps' });
  if (!state.flow.steps.length) {
    list.appendChild(el('li', { class: 'mas-empty', text: t('stepsEmpty') }));
  }
  state.flow.steps.forEach((step, index) => list.appendChild(stepRow(step, index)));

  const settings = state.flow.settings;
  const settingsRow = el('div', { class: 'mas-row' }, [
    button('⚙ ' + t('flowSettingsGear'), openFlowSettingsModal),
  ]);

  // One line of actions that floats right above the footer while - and only
  // while - the flow tab is the open one (it lives inside the pane, so hiding
  // the section hides the bar with it; nothing to unmount on tab changes).
  const ioRow = el('div', { class: 'mas-flowbar' }, [
    button(t('save'), () => saveFlow(), { class: 'mas-btn mas-primary' }),
    button(t('load'), loadFlowDialog),
    button('🪟 ' + t('flowLoadClipBrowser'), () => loadFlowFromClipboard('browser')),
    button('🖥 ' + t('flowLoadClipDesktop'), () => loadFlowFromClipboard('desktop')),
    button('📋 ' + t('copyReport'), copyReport, { class: 'mas-btn' }),
    button('🗑 ' + t('clearAll'), clearAllSteps, { class: 'mas-btn mas-danger' }),
  ]);

  const errorBox = errors.length
    ? el('div', { class: 'mas-errors' }, [
      el('b', { text: t('invalidFlow') }),
      el('ul', {}, errors.map((e) => el('li', { text: e }))),
    ])
    : null;

  // The action line comes FIRST in the DOM: it is sticky at top:0, so it must
  // start right under the header. When it sat last, sticky anchored it to its
  // natural place - the bottom of the pane - which is exactly where the user
  // kept finding it (round 14).
  replace(pane, ioRow, nameRow, errorBox, list, settingsRow);
}

/* Round 25: the flow settings live in a gear modal, so the steps own the
 * whole width of the pane. Everything saves on change, modal or not. */
function openFlowSettingsModal() {
  const settings = state.flow.settings;
  // `let` + wrapper on purpose: the close button below is built inside the
  // modalCard() arguments, before the function modalCard returns exists.
  let close = () => {};
  close = modalCard([
    el('div', { class: 'mas-card-head' },
      [el('b', { text: '⚙ ' + t('flowSettingsGear') })]),
    el('label', { class: 'mas-field' }, [
      el('span', { text: t('defaultDelay') }),
      el('input', {
        class: 'mas-input', type: 'number', min: '0', value: settings.defaultDelayAfterMs,
        onInput: (e) => { settings.defaultDelayAfterMs = Number(e.target.value) || 0; persistFlow(); },
      }),
    ]),
    el('label', { class: 'mas-field' }, [
      el('span', { text: t('typingMode') }),
      el('select', {
        id: 'mas-flow-typing-mode', class: 'mas-input',
        onChange: (e) => { settings.typingMode = e.target.value; persistFlow(); },
      }, ['low', 'normal', 'fast'].map((mode) => el('option', {
        value: mode, selected: (settings.typingMode || 'normal') === mode,
      }, t('typing' + mode[0].toUpperCase() + mode.slice(1))))),
    ]),
    el('p', { class: 'mas-hint', text: t('typingModeHelp') }),
    el('label', { class: 'mas-field' }, [
      el('span', { text: t('repeat') }),
      el('input', {
        class: 'mas-input', type: 'number', min: '1', value: settings.repeat,
        onInput: (e) => { settings.repeat = Math.max(1, Number(e.target.value) || 1); persistFlow(); },
      }),
    ]),
    el('label', { class: 'mas-check' }, [
      el('input', {
        type: 'checkbox', checked: settings.screenshotAfterEachStep,
        onChange: (e) => { settings.screenshotAfterEachStep = e.target.checked; persistFlow(); },
      }),
      el('span', { text: t('screenshotEach') }),
    ]),
    el('label', { class: 'mas-check' }, [
      el('input', {
        type: 'checkbox', checked: settings.screenshotBeforeEachStep,
        onChange: (e) => { settings.screenshotBeforeEachStep = e.target.checked; persistFlow(); },
      }),
      el('span', { text: t('screenshotBefore') }),
    ]),
    el('label', { class: 'mas-check' }, [
      el('input', {
        type: 'checkbox', checked: settings.screenshotOnError !== false,
        onChange: (e) => { settings.screenshotOnError = e.target.checked; persistFlow(); },
      }),
      el('span', { text: t('screenshotOnError') }),
    ]),
    el('label', { class: 'mas-check' }, [
      el('input', {
        type: 'checkbox', checked: settings.stopOnError,
        onChange: (e) => { settings.stopOnError = e.target.checked; persistFlow(); },
      }),
      el('span', { text: t('stopOnError') }),
    ]),
    el('label', { class: 'mas-field' }, [
      el('span', { text: t('flowErrorOp') }),
      el('input', {
        class: 'mas-input', value: settings.errorOperation || '',
        placeholder: 'op-builtin-observe',
        onInput: (e) => { settings.errorOperation = e.target.value; persistFlow(); },
      }),
    ]),
    el('p', { class: 'mas-hint', text: t('brainPolicy') }),
    el('label', { class: 'mas-check' }, [
      el('input', {
        type: 'checkbox', checked: Boolean(settings.retryAfterProtocol),
        onChange: (e) => { settings.retryAfterProtocol = e.target.checked; persistFlow(); },
      }),
      el('span', { text: t('flowRetryAfterProtocol') }),
    ]),
    el('div', { class: 'mas-row' }, [button(t('close'), () => close())]),
  ]);
}

/* ------------------------------------------------------------------ *
 * Saved flows: one place that keeps the runs which worked
 * ------------------------------------------------------------------ */

async function renderLibrary() {
  const box = document.getElementById('mas-flows-lib');
  if (!box) return;
  replace(box, el('p', { class: 'mas-lib-empty', text: '…' }));
  let flows = [];
  try {
    const data = await api('/flows');
    flows = data.flows || [];
  } catch (error) {
    replace(box, el('p', { class: 'mas-lib-empty', text: error.message }));
    return;
  }
  if (!flows.length) {
    replace(box, el('p', { class: 'mas-lib-empty', text: t('libraryEmpty') }));
    return;
  }
  replace(box, el('div', { class: 'mas-library' }, flows.map((item) => el('div', {
    class: 'mas-lib-row', dataset: { name: item.name },
  }, [
    el('div', { class: 'mas-lib-main' }, compact([
      el('b', { text: item.name }),
      el('span', {
        class: 'mas-lib-meta',
        text: `${item.steps} ${t('stepsWord')} · ${formatTime(item.updatedAt)}`,
      }),
      item.description ? el('span', { class: 'mas-lib-desc', text: item.description }) : null,
    ])),
    el('div', { class: 'mas-lib-actions' }, [
      button('✎', () => openFlowModal(item), { title: t('flowEditTitle') }),
      button('⤓', () => openSavedFlow(item.name), { title: t('load') }),
      button('▶', () => runSavedFlow(item.name), { class: 'mas-btn mas-primary', title: t('run') }),
      button('🗑', () => deleteSavedFlow(item.name), { class: 'mas-btn mas-danger', title: t('delete') }),
    ]),
  ]))));
}

async function openSavedFlow(name) {
  try {
    const detail = await api(`/flows/${encodeURIComponent(name)}`);
    if (!detail.flow) throw new Error('empty flow');
    state.flow = detail.flow;
    state.openStepId = null;
    persistFlow();
    renderFlow();
    toast(`${t('load')}: ${name}`, 'ok');
  } catch (error) {
    toast(error.message, 'error');
  }
}

function modalCard(children) {
  // One centred dialog layer; clicking the dim backdrop closes it.
  let layer = null;
  const close = () => { if (layer) layer.remove(); layer = null; };
  layer = el('div', {
    class: 'mas-modal',
    onClick: (event) => { if (event.target === layer) close(); },
  }, [el('div', { class: 'mas-modal-card mas-card' }, children)]);
  document.body.appendChild(layer);
  return close;
}

/* ------------------------------------------------------------------ *
 * Supabase onboarding: ask for the connection when the panel opens
 * ------------------------------------------------------------------ */

let supaOnboardTried = false;

function supaReloadSoon() {
  setTimeout(() => {
    // wrapped: in a jsdom teardown the global window is already gone
    try { window.location.reload(); } catch (error) { /* no-op */ }
  }, 6000);
}

function openSupaModal(onDone) {
  const conn = el('textarea', { class: 'mas-input', rows: 3, dir: 'ltr',
                                placeholder: t('supaConnPh') });
  const close = modalCard([
    el('div', { class: 'mas-card-head' },
       [el('b', { text: '☁️ ' + t('supaOnboardTitle') })]),
    el('p', { class: 'mas-hint', text: t('supaOnboardHint') }),
    el('label', { class: 'mas-field' },
      [el('span', { text: t('supaConnLabel') }), conn]),
    el('div', { class: 'mas-row mas-wrap' }, [
      button('🔗 ' + t('supaConnectBtn'), async () => {
        const value = conn.value.trim();
        if (!value) { toast(t('supaConnEmpty'), 'warn'); return; }
        try {
          const res = await api('/supabase/connect', { method: 'POST',
                                                       body: { connection: value } });
          close();
          if (res.restarting) {
            toast(t('supaRestoring'), 'warn', { sticky: true });
            supaReloadSoon();
          } else {
            toast(res.restored ? t('supaConnectedRestore') : t('supaConnected'), 'ok');
          }
          if (onDone) onDone(res);
        } catch (error) { toast(error.message, 'error'); }
      }),
      button(t('supaLater'), () => close()),
    ]),
  ]);
  return close;
}

async function maybeSupaOnboard() {
  // Once per page load, only for an operator who is already in, and only when
  // nothing is configured AND nothing is stored locally (a fresh Colab
  // session): exactly the moment a cloud snapshot can be poured in without
  // risking anybody's data. A machine that has data is never nagged.
  if (supaOnboardTried || !state.token) return;
  supaOnboardTried = true;
  let st;
  try { st = await api('/supabase/status'); } catch (error) { return; }
  if (st.configured || !st.dataEmpty) return;
  openSupaModal();
}

async function openFlowModal(item) {
  let detail;
  try {
    detail = await api(`/flows/${encodeURIComponent(item.name)}`);
  } catch (error) { toast(error.message, 'error'); return; }
  const flow = detail.flow || {};
  const nameInput = el('input', { class: 'mas-input', value: flow.name || item.name });
  const descInput = el('textarea', {
    class: 'mas-input', rows: 3, value: flow.description || '',
  });
  const close = modalCard([
    el('div', { class: 'mas-card-head' }, [el('b', { text: '✎ ' + t('flowEditTitle') })]),
    el('p', { class: 'mas-hint',
      text: `${(flow.steps || []).length} ${t('stepsWord')} · ${formatTime(item.updatedAt)}` }),
    el('label', { class: 'mas-field' }, [el('span', { text: t('flowName') }), nameInput]),
    el('label', { class: 'mas-field' }, [el('span', { text: t('flowDesc') }), descInput]),
    el('div', { class: 'mas-row mas-wrap' }, [
      button('💾 ' + t('save'), async () => {
        const newName = nameInput.value.trim();
        if (!newName) { toast(t('invalidFlow'), 'warn'); return; }
        try {
          const next = Object.assign({}, flow, {
            name: newName, description: descInput.value,
          });
          await api(`/flows/${encodeURIComponent(newName)}`,
            { method: 'PUT', body: { flow: next } });
          if (newName !== item.name) {
            await api(`/flows/${encodeURIComponent(item.name)}`, { method: 'DELETE' });
          }
          close();
          toast(t('opSaved'), 'ok');
          renderLibrary();
        } catch (error) { toast(error.message, 'error'); }
      }, { class: 'mas-btn mas-primary' }),
      button('▶ ' + t('run'), () => { close(); runSavedFlow(item.name); }),
      button('🗑 ' + t('delete'), async () => {
        if (!window.confirm(t('deleteSavedConfirm').replace('{n}', item.name))) return;
        try {
          await api(`/flows/${encodeURIComponent(item.name)}`, { method: 'DELETE' });
          close();
          toast(t('flowDeleted'), 'ok');
          renderLibrary();
        } catch (error) { toast(error.message, 'error'); }
      }, { class: 'mas-btn mas-danger' }),
      button(t('close'), () => close()),
    ]),
  ]);
}

async function runSavedFlow(name) {
  try {
    const detail = await api(`/flows/${encodeURIComponent(name)}`);
    if (!detail.flow) throw new Error('empty flow');
    state.flow = detail.flow;
    persistFlow();
    renderFlow();
    await runFlow();
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function deleteSavedFlow(name) {
  if (!window.confirm(t('deleteSavedConfirm').replace('{n}', name))) return;
  try {
    await api(`/flows/${encodeURIComponent(name)}`, { method: 'DELETE' });
    toast(`${t('delete')}: ${name}`, 'ok');
    renderLibrary();
  } catch (error) {
    toast(error.message, 'error');
  }
}

/** The report with absolute screenshot links, so the model can open them. */
async function copyReport() {
  try {
    const response = await fetch(API_PREFIX + '/report?base='
      + encodeURIComponent(window.location.origin),
    { headers: { 'X-Automation-Token': state.token } });
    const text = await response.text();
    copyText(text);
    const line = text.split('\n').map((s) => s.trim()).filter(Boolean).join(' · ');
    toast((line || t('copied')).slice(0, 400), 'info', { sticky: true });
  } catch (error) {
    toast(error.message, 'error');
  }
}

/** Delete every step, after asking. One at a time gets old by step ten. */
function clearAllSteps() {
  if (!state.flow.steps.length) {
    toast(t('nothingToClear'), 'warn');
    return;
  }
  const message = t('clearAllConfirm').replace('{n}', String(state.flow.steps.length));
  if (!window.confirm(message)) return;
  state.flow.steps = [];
  state.openStepId = null;
  persistFlow();
  renderFlow();
  toast(t('clearedAll'), 'ok');
}

function stepRow(step, index, outcomes = stepOutcomes()) {
  const row = el('li', {
    class: 'mas-step' + (state.status.index === index && isRunning() ? ' is-current' : ''),
    draggable: 'true', dataset: { id: step.id },
  });
  row.addEventListener('dragstart', (event) => {
    event.dataTransfer.setData('text/plain', String(index));
    event.dataTransfer.effectAllowed = 'move';
  });
  row.addEventListener('dragover', (event) => { event.preventDefault(); row.classList.add('is-over'); });
  row.addEventListener('dragleave', () => row.classList.remove('is-over'));
  row.addEventListener('drop', (event) => {
    event.preventDefault();
    row.classList.remove('is-over');
    const from = Number(event.dataTransfer.getData('text/plain'));
    state.flow.steps = moveStep(state.flow.steps, from, index);
    persistFlow();
    renderFlow();
  });

  row.append(
    el('span', { class: 'mas-grip', text: '⠿' }),
    // Round 26: the step number reads before the enable checkbox, so the
    // stages read top-to-bottom as «مرحلهٔ ۱، مرحلهٔ ۲، …».
    el('span', { class: 'mas-step-no', text: `${t('stepNo')} ${index + 1}` }),
    el('input', {
      type: 'checkbox', checked: step.enabled !== false, title: 'enabled',
      onChange: (event) => { step.enabled = event.target.checked; persistFlow(); },
    }),
    stepBadge(index, outcomes),
    el('button', {
      class: 'mas-step-label', type: 'button', text: step.label || labelFor(step),
      title: step.note || '',
      onClick: () => {
        state.openStepId = state.openStepId === step.id ? null : step.id;
        renderFlow();
      },
    }),
    el('span', { class: 'mas-spacer' }),
    el('button', {
      class: 'mas-icon', type: 'button', text: '✎', title: t('opEdit'),
      onClick: () => {
        state.openStepId = step.id;
        renderFlow();
        const node = document.querySelector(`.mas-step[data-id="${step.id}"]`);
        if (node && node.scrollIntoView) node.scrollIntoView({ block: 'nearest' });
      },
    }),
    el('button', {
      class: 'mas-icon', type: 'button', text: '📄', title: t('stepNoteTitle'),
      onClick: () => openStepNoteModal(step),
    }),
    el('button', {
      class: 'mas-icon', type: 'button', text: '⧉', title: t('duplicate'),
      onClick: () => {
        const copy = { ...structuredCloneSafe(step), id: `s-${Math.random().toString(16).slice(2, 10)}` };
        state.flow.steps.splice(index + 1, 0, copy);
        persistFlow(); renderFlow();
      },
    }),
    el('button', {
      class: 'mas-icon', type: 'button', text: '✕', title: t('delete'),
      onClick: () => { state.flow.steps.splice(index, 1); persistFlow(); renderFlow(); },
    }),
  );

  if (state.openStepId === step.id) row.appendChild(stepEditor(step));
  return row;
}

function openStepNoteModal(step) {
  const area = el('textarea', {
    class: 'mas-input mas-area', rows: '4', value: step.note || '',
    placeholder: t('stepNoteHint'),
    onInput: (event) => { step.note = event.target.value; persistFlow(); },
  });
  const writeBtn = button(t('stepDocWrite'), async () => {
    writeBtn.disabled = true;
    writeBtn.textContent = t('stepDocWorking');
    try {
      const data = await api('/agent/step-doc', { method: 'POST', body: { step } });
      step.note = String((data && data.text) || '').trim();
      area.value = step.note;
      persistFlow();
      toast(t('stepDocSaved'), 'ok');
    } catch (error) { toast(error.message, 'error'); }
    writeBtn.disabled = false;
    writeBtn.textContent = t('stepDocWrite');
  }, { class: 'mas-btn' });
  modalCard([
    el('div', { class: 'mas-card-head' }, [el('b', {
      text: '📄 ' + t('stepNoteTitle') + ' — ' + (step.label || labelFor(step)),
    })]),
    area,
    el('div', { class: 'mas-row mas-wrap' }, [writeBtn]),
  ]);
}

function stepEditor(step) {
  const def = stepDef(step.type);
  const fields = [];

  for (const field of (def ? def.fields : [])) {
    if (field.kind === 'area') {
      fields.push(el('label', { class: 'mas-field mas-wide' }, [
        el('span', { text: field.key }),
        el('textarea', {
          class: 'mas-input', rows: String(field.rows || 3),
          value: step[field.key] || '',
          onInput: (event) => { step[field.key] = event.target.value; },
        }),
      ]));
      continue;
    }
    if (field.kind === 'select') {
      fields.push(el('label', { class: 'mas-field' }, [
        el('span', { text: field.key }),
        el('select', {
          class: 'mas-input',
          onChange: (event) => {
            if (field.key === 'typingMode' && event.target.value === 'inherit') delete step[field.key];
            else step[field.key] = event.target.value;
          },
        }, (field.options || []).map((option) => {
          const selected = field.key === 'typingMode'
            ? String(step[field.key] || 'inherit') === option
            : String(step[field.key] || '') === option;
          const optionLabel = field.key === 'typingMode'
            ? t(option === 'inherit' ? 'typingInherit'
              : 'typing' + option[0].toUpperCase() + option.slice(1))
            : option;
          return el('option', { value: option, selected }, optionLabel);
        })),
      ]));
      continue;
    }
    if (field.kind === 'bool') {
      fields.push(el('label', { class: 'mas-check' }, [
        el('input', {
          type: 'checkbox', checked: Boolean(step[field.key]),
          onChange: (event) => { step[field.key] = event.target.checked; },
        }),
        el('span', { text: field.key }),
      ]));
      continue;
    }
    fields.push(el('label', { class: 'mas-field' }, [
      el('span', { text: field.key }),
      el('input', {
        class: 'mas-input', type: field.kind === 'int' ? 'number' : 'text',
        value: step[field.key] === undefined ? '' : step[field.key],
        onInput: (event) => {
          step[field.key] = field.kind === 'int' ? Number(event.target.value) : event.target.value;
        },
      }),
    ]));
  }

  // A label is derived from the step's fields. It used to be kept as soon as one
  // existed, so a step created empty stayed: paste "" for ever, even after the
  // text was typed. Only a label the user typed by hand survives from now on.
  let customLabel = false;
  fields.push(el('label', { class: 'mas-field mas-wide' }, [
    el('span', { text: 'label' }),
    el('input', {
      class: 'mas-input', value: step.label || '',
      onInput: (event) => { step.label = event.target.value; customLabel = true; },
    }),
  ]));
  fields.push(el('label', { class: 'mas-field' }, [
    el('span', { text: 'delayAfterMs' }),
    el('input', {
      class: 'mas-input', type: 'number', min: '0',
      value: step.delayAfterMs === undefined ? '' : step.delayAfterMs,
      onInput: (event) => {
        step.delayAfterMs = event.target.value === '' ? undefined : Number(event.target.value);
      },
    }),
  ]));
  fields.push(el('label', { class: 'mas-check' }, [
    el('input', {
      type: 'checkbox', checked: Boolean(step.requiresConfirmation),
      onChange: (event) => { step.requiresConfirmation = event.target.checked; },
    }),
    el('span', { text: 'requiresConfirmation' }),
  ]));
  fields.push(el('label', { class: 'mas-check' }, [
    el('input', {
      type: 'checkbox', checked: Boolean(step.continueOnError),
      onChange: (event) => { step.continueOnError = event.target.checked; },
    }),
    el('span', { text: 'continueOnError' }),
  ]));

  return el('div', { class: 'mas-editor' }, [
    el('div', { class: 'mas-grid' }, fields),
    el('div', { class: 'mas-row' }, [
      button('OK', () => {
        if (!customLabel) step.label = labelFor(step);
        persistFlow(); state.openStepId = null; renderFlow();
      }, { class: 'mas-btn mas-primary' }),
      button('↺ ' + labelFor(step), () => {
        step.label = labelFor(step); customLabel = false; renderFlow();
      }),
    ]),
  ]);
}

function textField(label, value, onInput, placeholder) {
  return el('label', { class: 'mas-field mas-wide' }, [
    el('span', { text: label }),
    el('input', {
      class: 'mas-input', value: value || '', placeholder: placeholder || '',
      onInput: (event) => onInput(event.target.value),
    }),
  ]);
}

/* ------------------------------------------------------------------ *
 * Run / status
 * ------------------------------------------------------------------ */

function isRunning() {
  return ['running', 'paused', 'waiting'].includes(state.status.status);
}

async function runFlow() {
  const errors = validateFlow(state.flow);
  if (errors.length) {
    toast(`${t('invalidFlow')}: ${errors[0]}`, 'error');
    renderFlow();
    return;
  }
  try {
    await api('/run', { method: 'POST', body: { flow: state.flow } });
    toast(t('statusRunning'), 'ok');
    refreshStatus();
  } catch (error) {
    toast(error.message, 'error');
  }
}

/** Stop with immediate feedback: the button reacts before the server answers. */
async function stopRun() {
  const stop = document.getElementById('mas-stop');
  if (stop) {
    stop.disabled = true;
    stop.textContent = '⏹ …';
  }
  try {
    await control('stop');
    await refreshStatus();
  } finally {
    if (stop) {
      stop.disabled = false;
      stop.textContent = '⏹';
    }
  }
}

async function control(action, extra = {}) {
  try {
    const data = await api('/control', { method: 'POST', body: { action, ...extra } });
    state.status = data.run || state.status;
    renderStatus();
  } catch (error) {
    toast(error.message, 'error');
  }
}

/** One place owns the token so the AI tab and the banner cannot disagree. */
function setToken(value) {
  state.token = String(value || '').trim();
  localStorage.setItem(LS.token, state.token);
  state.tokenRejected = false;
  const aiField = document.getElementById('mas-ai-token');
  if (aiField) aiField.value = state.token;
  if (state.token && document.getElementById('mas-panel')) {
    // Panes rendered while logged out are empty shells - every fetch was a
    // 401. Re-render the open tab so provider lists, statuses and settings
    // come alive the moment the token lands.
    selectTab(currentTab());
  }
  refreshStatus();
}

async function refreshStatus() {
  // /info needs no token, so it is always safe to ask. Everything else does,
  // and knocking on those before the human has typed a token only produces a
  // stream of 401s, so ask for the token instead.
  let info;
  try {
    info = await api('/info');
  } catch (error) {
    // The server itself is not answering; asking for a token would be misleading.
    setConnected(false, false);
    renderTokenBar(false);
    return;
  }
  state.info = info;

  if (info.authRequired && !state.token) {
    setConnected(false, true);
    renderTokenBar(true);
    renderStatus();
    return;
  }

  try {
    state.status = await api('/status');
  } catch (error) {
    // A 401 here means the token is wrong. Say so where the human can fix it
    // rather than throwing a toast from a background poll.
    if (error.status === 401) {
      state.tokenRejected = true;
      setConnected(false, true);
      renderTokenBar(true);
    } else {
      setConnected(false, false);
    }
    renderStatus();
    return;
  }
  state.tokenRejected = false;
  setConnected(true);
  renderTokenBar(false);
  renderStatus();
  maybeSupaOnboard();
  if (currentTab() === 'log') renderLog();
  // Redraw the step list only when the highlighted step moved, otherwise an
  // editor the human opened would close under their hands every poll.
  if (currentTab() === 'flow' && isRunning() && state.status.index !== state.lastRenderedIndex) {
    state.lastRenderedIndex = state.status.index;
    renderFlow();
  }
  // Once a run has finished the highlight stops moving, but the badges still
  // have to catch up, otherwise every step keeps looking "not run yet".
  const sig = `${state.status.runId || ''}:${(state.status.results || []).length}`;
  if (currentTab() === 'flow' && sig !== state.lastResultsSig) {
    state.lastResultsSig = sig;
    // Never close an editor the human has open; the badges catch up later.
    if (!state.openStepId) renderFlow();
  }
}

function renderTokenBar(show, detail = '') {
  const bar = document.getElementById('mas-tokenbar');
  const text = document.getElementById('mas-tokenbar-text');
  const input = document.getElementById('mas-token-input');
  if (!bar || !text) return;
  bar.hidden = !show;
  if (!show) return;
  text.textContent = state.tokenRejected ? t('tokenBarWrong') : t('tokenBarTitle');
  if (detail && !state.tokenRejected) text.textContent = detail;
  bar.classList.toggle('is-error', Boolean(state.tokenRejected));
  if (input && document.activeElement !== input) input.value = state.token;
}

function setConnected(ok, needsToken = false) {
  // Only on a change, not on every poll: this fires an extra /agent request.
  if (ok && lastConnected !== true) refreshAgentTabs();
  lastConnected = ok;
  const dot = document.getElementById('mas-conn');
  if (!dot) return;
  dot.className = 'mas-dot ' + (ok ? 'is-ok' : 'is-bad');
  dot.title = ok ? t('connected') : (needsToken ? t('needToken') : 'offline');
}

/* Mount a form/secret card's fields into the given host. Shared by the panel
 * overlay and the desktop-view handoff card (round 27, item 2): same formId
 * means the fields already on screen ARE the draft, so no host ever rebuilds
 * them on a poll tick and resets what the human typed. */
function mountConfirmForm(formHost, waiting, kind) {
  if (!formHost || formHost.dataset.formId === waiting.formId) return;
  const schema = waiting.formSchema || {};
  draftRestore(waiting.formId);
  const bits = [el('p', { class: 'mas-hint', text: schema.message || '' })];
  if (kind === 'secret') {
    const policy = schema.policy || {};
    bits.push(el('p', { class: 'mas-form-warn', text: t('secretWarn') }));
    bits.push(el('p', {
      class: 'mas-form-policy',
      text: `${t('secretScope')}: ${(policy.allowedHosts || []).join('، ')} · `
        + `${(policy.allowedPaths || []).join('، ')} · `
        + `${(policy.methods || []).join('/')} · TTL ${policy.ttlSeconds || 900}s · `
        + `max ${policy.maxUses || 20}`,
    }));
  }
  (schema.fields || []).forEach((field) => bits.push(formFieldNode(field, schema)));
  formHost.dataset.formId = waiting.formId;
  replace(formHost, ...bits);
}

/* Round 27, item 2: the "needs human action" box must be on the desktop view
 * AND the big panel at the same time. Both read the same server state and
 * press the same endpoints (/control confirm, /forms submit), so acting on
 * either one advances the flow. The card sits bottom-right and covers only
 * its own box: during a challenge the human keeps working on the live page. */
function renderDesktopHandoff() {
  if (document.getElementById('mas-panel')) return;   // panel mode owns #mas-confirm
  const status = state.status || {};
  const waiting = status.awaitingConfirmation;
  let card = document.getElementById('mas-desktop-handoff');
  if (!waiting) {
    if (card) card.hidden = true;
    // On the human panel the idle note returns - unless a brain suggestion
    // is still waiting there (that one manages the note itself).
    const idleNote = document.getElementById('mas-human-empty');
    const sug = document.getElementById('mas-human-suggestion');
    if (idleNote) idleNote.hidden = Boolean(sug && !sug.hidden);
    return;
  }
  const kind = waiting.kind;
  const humanHandoff = kind === 'challenge' || kind === 'manual_verification';
  const formKind = kind === 'form' || kind === 'secret';
  const schema = waiting.formSchema || {};
  const sig = [kind, waiting.formId || '', waiting.index || 0, waiting.label || '',
    waiting.message || '', waiting.publicShot || ''].join('|');
  if (!card) {
    card = el('div', { id: 'mas-desktop-handoff', class: 'mas-desktop-handoff' });
    // On the dedicated human panel the card sits inside the page flow; on
    // the desktop view it floats over noVNC.
    const parent = document.getElementById('mas-human-root') || document.body;
    parent.appendChild(card);
  }
  card.hidden = false;
  const idleNote = document.getElementById('mas-human-empty');
  if (idleNote) idleNote.hidden = true;
  const mountedForm = card.querySelector('#mas-desktop-form');
  if (card.dataset.sig === sig && (!formKind || mountedForm)) {
    if (formKind) mountConfirmForm(mountedForm, waiting, kind);
    return;
  }
  replace(card,
    el('h3', { text: '✋ ' + (kind === 'challenge' ? t('challengeTitle')
      : kind === 'manual_verification' ? t('manualHandoffTitle') : t('confirmTitle')) }),
    el('p', {
      class: 'mas-hint',
      text: `#${(waiting.index || 0) + 1} ${waiting.label || waiting.type || ''}`
        + (waiting.pageOrigin ? ` · ${waiting.pageOrigin}` : ''),
    }),
    el('p', { text: kind === 'challenge' ? t('challengeBody') : (waiting.message || '') }),
    waiting.publicShot ? el('a', {
      href: `${API_PREFIX}/public/shot/${encodeURIComponent(waiting.publicShot)}`,
      target: '_blank', rel: 'noopener', class: 'mas-confirm-shot',
      text: t('viewChallengeShot'),
    }) : null,
    formKind ? el('div', { id: 'mas-desktop-form', class: 'mas-confirm-form' }) : null,
    el('div', { class: 'mas-row' }, [
      el('button', {
        id: 'mas-desktop-approve', class: 'mas-btn mas-primary', type: 'button',
        text: formKind ? (schema.submitLabel || t('approve'))
          : humanHandoff ? t('humanContinue') : t('approve'),
        onClick: () => confirmAction(true),
      }),
      el('button', {
        id: 'mas-desktop-refuse', class: 'mas-btn', type: 'button',
        text: formKind ? (schema.cancelLabel || t('refuse'))
          : humanHandoff ? t('humanStop') : t('refuse'),
        onClick: () => confirmAction(false),
      }),
    ]),
  );
  card.dataset.sig = sig;
  if (formKind) mountConfirmForm(card.querySelector('#mas-desktop-form'), waiting, kind);
}

function renderStatus() {
  const status = state.status;
  const line = document.getElementById('mas-statusline');
  const progress = document.getElementById('mas-progress');
  const known = ['idle', 'running', 'paused', 'waiting', 'done', 'error', 'stopped'];
  const raw = known.includes(status.status) ? status.status : 'idle';
  const label = t('status' + raw[0].toUpperCase() + raw.slice(1));
  if (progress) {
    progress.textContent = `${(status.index || 0) + (isRunning() ? 1 : 0)}/${status.stepCount || 0}`;
  }
  if (line) {
    const parts = [label, formatElapsed(status.elapsedMs)];
    if (state.info && state.info.authRequired && !state.token) parts.push(t('needTokenShort'));
    if (status.error) parts.push(truncate(status.error, 90));
    replace(line, el('span', { class: 'mas-status-' + (status.status || 'idle'), text: parts.join(' · ') }));
  }

  const confirm = document.getElementById('mas-confirm');
  if (confirm) {
    const waiting = status.awaitingConfirmation;
    const kind = waiting && waiting.kind;
    const humanHandoff = kind === 'challenge' || kind === 'manual_verification';
    const formKind = kind === 'form' || kind === 'secret';
    confirm.hidden = !waiting;
    confirm.classList.toggle('mas-confirm-handoff', Boolean(humanHandoff));
    confirm.classList.toggle('mas-confirm-form', Boolean(formKind));
    if (waiting) {
      const title = document.getElementById('mas-confirm-title');
      const labelText = document.getElementById('mas-confirm-label');
      const message = document.getElementById('mas-confirm-message');
      const approve = document.getElementById('mas-confirm-approve');
      const refuse = document.getElementById('mas-confirm-refuse');
      const shot = document.getElementById('mas-confirm-shot');
      title.textContent = kind === 'challenge' ? t('challengeTitle')
        : kind === 'manual_verification' ? t('manualHandoffTitle') : t('confirmTitle');
      labelText.textContent = `#${(waiting.index || 0) + 1} ${waiting.label || waiting.type}`
        + (waiting.pageOrigin ? ` · ${waiting.pageOrigin}` : '');
      message.textContent = kind === 'challenge' ? t('challengeBody') : (waiting.message || '');
      approve.textContent = humanHandoff ? t('humanContinue') : t('approve');
      refuse.textContent = humanHandoff ? t('humanStop') : t('refuse');
      shot.hidden = !waiting.publicShot;
      shot.href = waiting.publicShot
        ? `${API_PREFIX}/public/shot/${encodeURIComponent(waiting.publicShot)}` : '';
      const formHost = document.getElementById('mas-confirm-form');
      formHost.hidden = !formKind;
      if (formKind) {
        const schema = waiting.formSchema || {};
        approve.textContent = schema.submitLabel || t('approve');
        refuse.textContent = schema.cancelLabel || t('refuse');
        mountConfirmForm(formHost, waiting, kind);
      } else if (formHost.dataset.formId) {
        formHost.dataset.formId = '';
        formDraft = { id: null, values: {}, touched: {} };
      }
    }
  }
  const run = document.getElementById('mas-run');
  if (run) run.disabled = isRunning();
  renderDesktopHandoff();
  renderRunPanel(raw, label);
}

// Round 14, problem 1: the panel polls ~1/s and used to rebuild the form card
// on every tick, which reset whatever the human had typed. The draft below is
// the card's local state: defaults apply once (first mount of a formId), every
// edit marks the field touched/dirty, and a same-formId poll never remounts.
// Non-secret drafts survive a page refresh through sessionStorage; a secret
// value NEVER touches any browser storage.
let formDraft = { id: null, values: {}, touched: {} };

function draftRestore(formId) {
  formDraft = { id: formId, values: {}, touched: {} };
  try {
    const raw = window.sessionStorage.getItem('mas.formdraft');
    const saved = raw ? JSON.parse(raw) : null;
    if (saved && saved.id === formId && saved.values && typeof saved.values === 'object') {
      formDraft.values = saved.values;
      formDraft.touched = saved.touched && typeof saved.touched === 'object'
        ? saved.touched : {};
    }
  } catch (error) { /* storage is best-effort, the form still works */ }
}

function draftPersist(schema) {
  try {
    const values = {};
    const touched = {};
    (schema.fields || []).forEach((field) => {
      if ((field.type || 'text') === 'secret') return;  // never stored
      if (formDraft.values[field.id] !== undefined) values[field.id] = formDraft.values[field.id];
      if (formDraft.touched[field.id]) touched[field.id] = true;
    });
    window.sessionStorage.setItem('mas.formdraft',
      JSON.stringify({ id: formDraft.id, values, touched }));
  } catch (error) { /* best-effort */ }
}

function draftClear() {
  formDraft = { id: null, values: {}, touched: {} };
  try {
    window.sessionStorage.removeItem('mas.formdraft');
  } catch (error) { /* best-effort */ }
}

function draftNote(schema, fieldId, value) {
  formDraft.values[fieldId] = value;
  formDraft.touched[fieldId] = true;
  draftPersist(schema);
}

function formFieldNode(field, schema) {
  const id = 'mas-ff-' + field.id;
  const type = field.type || 'text';
  const saved = formDraft.values[field.id];
  let input = null;
  if (type === 'checkbox') {
    input = el('input', {
      id, type: 'checkbox',
      checked: saved !== undefined ? !!saved : !!field.default,
      onChange: (event) => draftNote(schema, field.id, event.target.checked),
    });
  } else if (type === 'radio' || type === 'select') {
    const wanted = saved !== undefined ? saved : field.default;
    input = el('div', { class: 'mas-form-opts' }, (field.options || []).map((opt) => el('label', {
      class: 'mas-check',
    }, [
      el('input', {
        type: 'radio', name: id, value: opt.value,
        checked: opt.value === wanted,
        onChange: () => draftNote(schema, field.id, opt.value),
      }),
      el('span', { text: opt.label || opt.value }),
    ])));
  } else if (type === 'textarea') {
    input = el('textarea', {
      id, class: 'mas-input', rows: 3,
      value: saved !== undefined ? saved : (field.default || ''),
      placeholder: field.placeholder || '',
      onInput: (event) => draftNote(schema, field.id, event.target.value),
    });
  } else {
    const htmlType = type === 'secret' ? 'password'
      : (['number', 'date', 'time', 'url', 'email'].includes(type) ? type : 'text');
    input = el('input', {
      id, class: 'mas-input', type: htmlType,
      // a secret is never restored from anywhere: it starts empty every mount
      value: type === 'secret' ? '' : (saved !== undefined ? saved : (field.default || '')),
      placeholder: field.placeholder || '', maxLength: field.maxLength || undefined,
      onInput: (event) => draftNote(schema, field.id, event.target.value),
    });
  }
  const children = [el('span', {
    text: (field.label || field.id) + (field.required ? ' *' : ''),
  }), input];
  if (type === 'secret') {
    // Peek only while the mouse is held, and a clear button: the value never
    // sits on screen longer than the human chooses.
    children.push(el('div', { class: 'mas-row' }, [
      button('👁 ' + t('holdShow'), () => {}, {
        class: 'mas-btn mas-mini',
        onMouseDown: () => { input.type = 'text'; },
        onMouseUp: () => { input.type = 'password'; },
        onMouseLeave: () => { input.type = 'password'; },
      }),
      button('🧹 ' + t('clearValue'), () => { input.value = ''; input.focus(); },
        { class: 'mas-btn mas-mini' }),
    ]));
  }
  return el('label', { class: 'mas-form-field' }, children);
}

function collectFormValues(schema) {
  const values = {};
  for (const field of (schema.fields || [])) {
    const id = 'mas-ff-' + field.id;
    const type = field.type || 'text';
    if (type === 'checkbox') {
      const node = document.getElementById(id);
      values[field.id] = node ? !!node.checked : !!formDraft.values[field.id];
    } else if (type === 'radio' || type === 'select') {
      const picked = document.querySelector(`input[name="${id}"]:checked`);
      values[field.id] = picked ? picked.value
        : (formDraft.values[field.id] !== undefined ? formDraft.values[field.id] : '');
    } else {
      const node = document.getElementById(id);
      values[field.id] = node ? node.value
        : (formDraft.values[field.id] !== undefined ? formDraft.values[field.id] : '');
    }
    // a required checkbox that is not ticked counts as empty, same as the
    // server-side validation in engine._validate_form_values
    const empty = values[field.id] === '' || values[field.id] == null
      || values[field.id] === false;
    if (field.required && empty) {
      throw new Error(`${t('formRequired')}: ${field.label || field.id}`);
    }
  }
  return values;
}

async function submitPendingForm(kind) {
  const waiting = state.status.awaitingConfirmation || {};
  const schema = waiting.formSchema || {};
  let values;
  try {
    values = collectFormValues(schema);
  } catch (error) {
    toast(error.message, 'warn');
    return;
  }
  const base = kind === 'secret' ? '/secrets/' : '/forms/';
  const path = base + encodeURIComponent(waiting.formId) + '/submit';
  try {
    await api(path, { method: 'POST', body: { values } });
    draftClear();
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function cancelPendingForm() {
  const waiting = state.status.awaitingConfirmation || {};
  const path = '/forms/' + encodeURIComponent(waiting.formId) + '/cancel';
  try {
    await api(path, { method: 'POST', body: {} });
    draftClear();
  } catch (error) {
    toast(error.message, 'error');
  }
}

function confirmAction(approve) {
  const waiting = state.status.awaitingConfirmation || {};
  if (waiting.kind === 'form' || waiting.kind === 'secret') {
    if (approve) submitPendingForm(waiting.kind);
    else cancelPendingForm();
    return;
  }
  control('confirm', { approve });
}

/** The always-visible run board: progress, clock, current step, report. */
function renderRunPanel(raw, label) {
  const panel = document.getElementById('mas-runpanel');
  if (!panel) return;
  const status = state.status;
  if (!status.runId && !isRunning()) {
    panel.hidden = true;
    return;
  }
  const total = status.stepCount || 0;
  const done = finishedCount();
  const failed = (status.results || []).filter((r) => r.status === 'error').length;
  const pct = total ? Math.min(100, Math.round((done / total) * 100)) : 0;
  const current = status.currentStep
    ? (status.currentStep.label || status.currentStep.type) : '';

  panel.hidden = false;
  replace(panel,
    el('div', { class: 'mas-runpanel-head' }, [
      el('span', { class: 'mas-pill mas-pill-' + raw, text: label }),
      el('span', { class: 'mas-clock', id: 'mas-run-clock', text: formatSeconds(runSeconds()) }),
      el('span', {
        class: 'mas-count',
        text: `${done}/${total}${failed ? ` · ${failed} ✕` : ''}`,
      }),
      el('span', { class: 'mas-spacer' }),
      button('📋 ' + t('copyReport'), copyReport),
    ]),
    el('div', { class: 'mas-bar' }, [
      el('div', {
        class: 'mas-bar-fill' + (raw === 'error' ? ' is-error' : ''),
        style: `width:${pct}%`,
      }),
    ]),
    compact([
      isRunning() && current ? el('div', { class: 'mas-runpanel-now' }, [
        el('span', { text: t('nowRunning') }),
        el('b', { text: current }),
        el('span', { class: 'mas-now-timer', id: 'mas-run-now', text: formatSeconds(currentStepSeconds()) }),
      ]) : null,
      status.error ? el('div', { class: 'mas-runpanel-error', text: status.error }) : null,
    ]),
  );
  tickTimers();
}

/* ------------------------------------------------------------------ *
 * Save / load / export
 * ------------------------------------------------------------------ */

async function saveFlow(name = state.flow.name) {
  try {
    await api(`/flows/${encodeURIComponent(name)}`, { method: 'PUT', body: { flow: state.flow } });
    state.flow.name = name;
    persistFlow();
    toast(`${t('save')}: ${name}`, 'ok');
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function loadFlowDialog() {
  try {
    const data = await api('/flows');
    const flows = data.flows || [];
    if (!flows.length) { toast(t('pagesEmpty'), 'info'); return; }
    const names = flows.map((f) => `${f.name} (${f.steps})`).join('\n');
    const choice = window.prompt(`${t('load')}\n\n${names}`, flows[0].name);
    if (!choice) return;
    const found = flows.find((f) => f.name === choice || `${f.name} (${f.steps})` === choice);
    if (!found) { toast('?', 'error'); return; }
    const detail = await api(`/flows/${encodeURIComponent(found.name)}`);
    state.flow = detail.flow;
    persistFlow();
    renderFlow();
    toast(`${t('load')}: ${found.name}`, 'ok');
  } catch (error) {
    toast(error.message, 'error');
  }
}

/* ------------------------------------------------------------------ *
 * Stages helper
 * ------------------------------------------------------------------ */

function addStep(step) {
  state.flow.steps.push(step);
  persistFlow();
  renderFlow();
}

/* ------------------------------------------------------------------ *
 * Pages / detection
 * ------------------------------------------------------------------ */

async function detectNow() {
  const pane = document.getElementById('mas-tab-pages');
  replace(pane, el('p', { class: 'mas-hint', text: t('detecting') }));
  try {
    const data = await api('/detect', { method: 'POST', body: { includeDom: true } });
    if (!data.ok) toast(data.snapshot.error || 'detect failed', 'error');
    else {
      state.activePage = data.snapshot;
      toast(`${t('detect')}: ${data.snapshot.pageKey}`, 'ok');
    }
  } catch (error) {
    toast(error.message, 'error');
  }
  renderPages();
}

async function renderPages() {
  const pane = document.getElementById('mas-tab-pages');
  let pages = [];
  try {
    pages = (await api('/pages')).pages || [];
  } catch (error) {
    replace(pane, el('p', { class: 'mas-hint', text: error.message }));
    return;
  }
  state.pages = pages;

  const head = el('div', { class: 'mas-row' }, [
    button('🔍 ' + t('detect'), detectNow, { class: 'mas-btn mas-primary' }),
    button('📋 ' + t('copyDetected'), copyDetected),
    el('span', { class: 'mas-hint', text: `${pages.length}` }),
  ]);

  const list = el('div', { class: 'mas-pages' });
  if (!pages.length) list.appendChild(el('p', { class: 'mas-empty', text: t('pagesEmpty') }));
  for (const page of pages) {
    const thumb = el('div', { class: 'mas-thumb' });
    if (page.screenshot) {
      imageBlobUrl('/artifact?path=' + encodeURIComponent(page.screenshot)).then((url) => {
        if (url) thumb.appendChild(el('img', { src: url, alt: page.pageKey }));
      }).catch(() => { /* a thumbnail is never worth an error */ });
    }
    list.appendChild(el('div', { class: 'mas-page' }, [
      thumb,
      el('div', { class: 'mas-page-info' }, [
        page.challengeDetected ? el('span', {
          class: 'mas-challenge-badge', text: '⚠ ' + t('possibleChallenge'),
        }) : null,
        el('button', {
          class: 'mas-step-label', type: 'button',
          text: truncate(page.title || page.pageKey, 60),
          onClick: () => openPage(page.id),
        }),
        el('div', { class: 'mas-hint' }, [
          truncate(page.url || page.pageKey, 60),
          ` · ${formatTime(page.capturedAt)}`,
          ` · ${page.elements} ${t('elements')}`,
          page.cdp ? '' : ' · no CDP',
        ]),
      ]),
    ]));
  }

  // The detected page goes *above* the list: after pressing detect you want
  // the screenshot right there, not scrolled away under seven other pages.
  const detail = el('div', { id: 'mas-page-detail' });
  if (state.activePage) renderPageDetail(detail, state.activePage);
  replace(pane, head, detail, list);
}

/** One primitive on the real desktop, no flow needed. */
async function manualControl(body, okMessage) {
  try {
    const data = await api('/control', { method: 'POST', body });
    toast(okMessage || 'ok', 'ok');
    return data;
  } catch (error) {
    toast(error.message, 'error');
    return null;
  }
}

/** The screenshot of a detected page: image first, absolute link beside it. */
function pageShotBlock(page) {
  const box = el('div', { class: 'mas-shotfirst' });
  const name = page.screenshot || page.publicShot || '';
  const publicUrl = page.publicShot
    ? `${window.location.origin}${API_PREFIX}/public/shot/`
      + encodeURIComponent(page.publicShot)
    : '';
  if (name) {
    const img = el('img', { class: 'mas-shotfirst-img', alt: page.pageKey || 'screenshot' });
    imageBlobUrl('/artifact?path=' + encodeURIComponent(name)).then((url) => {
      if (url) img.src = url;
    }).catch(() => { /* the link below still works */ });
    box.appendChild(img);
  } else {
    box.appendChild(el('p', { class: 'mas-hint', text: t('noShotForPage') }));
  }
  const meta = el('div', { class: 'mas-shotfirst-meta' }, [
    el('b', { text: truncate(page.title || page.pageKey || '', 60) }),
    page.url ? el('div', { class: 'mas-hint', text: truncate(page.url, 90) }) : null,
    el('div', { class: 'mas-hint', text: `${formatTime(page.capturedAt)} · ${(page.elements || []).length} ${t('elements')}` }),
    publicUrl ? el('a', { class: 'mas-shotfirst-link', href: publicUrl, target: '_blank', rel: 'noopener', text: publicUrl }) : null,
    el('div', { class: 'mas-row mas-wrap' }, [
      publicUrl ? button('📋 ' + t('copyShotLink'), () => copyText(publicUrl)) : null,
      name ? button('🖱 ' + t('refreshShot'), () => manualControl({ action: 'screenshot' }, t('shotTaken'))) : null,
    ]),
  ]);
  box.appendChild(meta);
  return box;
}

function renderPageDetail(container, page) {
  const rows = (page.elements || []).slice(0, 60).map((item) => el('tr', {}, [
    el('td', {}, el('code', { text: truncate(item.selector, 40) })),
    el('td', { text: truncate(item.text || item.href || '', 28) }),
    el('td', { text: `${item.desktop.x},${item.desktop.y}` }),
    el('td', {}, el('div', { class: 'mas-row' }, [
      // A real click: it moves the actual pointer on the desktop, right now.
      el('button', {
        class: 'mas-btn mas-mini mas-primary', type: 'button', text: t('clickNow'),
        title: t('clickNowHint'),
        onClick: () => manualControl(
          { action: 'click', x: item.desktop.x, y: item.desktop.y },
          `${t('clickNow')}: ${item.desktop.x},${item.desktop.y}`),
      }),
      el('button', {
        class: 'mas-btn mas-mini', type: 'button', text: t('useAsClick'),
        onClick: () => {
          addStep(createStep('python', {
            code: `check(click(${item.desktop.x}, ${item.desktop.y}))`,
            label: `کلیک در (${item.desktop.x}، ${item.desktop.y})`,
          }));
          toast(labelFor(state.flow.steps[state.flow.steps.length - 1]), 'ok');
        },
      }),
    ])),
  ]));

  const challenge = page.challenge || { detected: false, signals: [] };
  replace(container,
    pageShotBlock(page),
    challenge.detected ? el('div', { class: 'mas-challenge-warning' }, [
      el('b', { text: '⚠ ' + t('possibleChallenge') }),
      el('div', { class: 'mas-hint', text: `${t('challengeSignals')}: ${(challenge.signals || []).join(', ')}` }),
      el('div', { class: 'mas-hint', text: t('challengeBody') }),
    ]) : el('p', { class: 'mas-hint', text: t('noChallengeDetected') }),
    el('h4', { text: `${t('elements')} (${(page.elements || []).length})` }),
    el('p', { class: 'mas-hint', text: t('elementsClickHint') }),
    el('table', { class: 'mas-table' }, [
      el('thead', {}, el('tr', {}, [
        el('th', { text: t('elementSelector') }),
        el('th', { text: t('elementText') }),
        el('th', { text: t('elementPos') }),
        el('th', {}),
      ])),
      el('tbody', {}, rows),
    ]),
    el('details', {}, [
      el('summary', { text: `${t('text')} (${(page.text || '').length})` }),
      el('pre', { class: 'mas-pre', text: (page.text || '').slice(0, 4000) }),
    ]),
  );
}

/** One text block an AI can read: page info, the public screenshot link, the
 * visible text, and the strongest elements. Copied to the clipboard. */
async function copyDetected() {
  const page = state.activePage;
  if (!page) { toast(t('pagesEmpty'), 'info'); return; }
  const lines = [];
  lines.push(`${t('elementText')}: ${page.title || ''}`);
  if (page.url) lines.push(`URL: ${page.url}`);
  if (page.pageKey) lines.push(`pageKey: ${page.pageKey}`);
  if (page.challenge && page.challenge.detected) {
    lines.push(`⚠ ${t('possibleChallenge')}: ${(page.challenge.signals || []).join(', ')}`);
    lines.push(t('challengeCopySafety'));
  } else {
    lines.push(t('noChallengeDetected'));
  }
  if (page.publicShot) {
    lines.push(`${t('shotLink')}: ${location.origin}${API_PREFIX}/public/shot/${page.publicShot}`);
  }
  if (page.text) lines.push(`\n${t('text')}:\n${page.text.slice(0, 3000)}`);
  const elements = (page.elements || []).slice(0, 40)
    .map((e) => `- ${e.tag} "${(e.text || e.href || '').slice(0, 40)}" desktop:${e.desktop.x},${e.desktop.y}`);
  if (elements.length) lines.push(`\n${t('elements')}:\n${elements.join('\n')}`);
  await copyText(lines.join('\n'));
}

async function openPage(id) {
  try {
    const data = await api(`/pages/${encodeURIComponent(id)}`);
    state.activePage = data.snapshot;
    renderPages();
  } catch (error) {
    toast(error.message, 'error');
  }
}

/* ------------------------------------------------------------------ *
 * Screenshots tab: every image the system ever took, newest first
 * ------------------------------------------------------------------ */

const ROLE_FA = { detect: 'شناسایی', run: 'اجرا', manual: 'دستی',
  challenge: 'بررسی CAPTCHA', handoff: 'تحویل به انسان',
  before: 'قبل مرحله', after: 'بعد مرحله', error: 'لحظه خطا' };

function shotLink(name) {
  return `${API_PREFIX}/public/shot/${encodeURIComponent(name)}`;
}

async function renderShots(host) {
  const pane = host || document.getElementById('mas-tab-shots');
  if (!pane) return;
  replace(pane, el('p', { class: 'mas-empty', text: '…' }));
  let rows = [];
  try {
    const data = await api('/shots');
    rows = data.shots || [];
  } catch (error) {
    replace(pane, el('p', { class: 'mas-empty', text: error.message }));
    return;
  }
  if (!rows.length) {
    replace(pane, el('p', { class: 'mas-empty', text: t('shotsEmpty') }));
    return;
  }
  replace(pane,
    el('div', { class: 'mas-row mas-wrap' }, [
      button('⟳ ' + t('refresh'), renderShots),
      el('span', { class: 'mas-hint', text: `${rows.length} ${t('shotsCount')}` }),
    ]),
    el('div', { class: 'mas-shots' }, rows.map((row) => el('div', { class: 'mas-shot' }, [
      el('a', { class: 'mas-shot-thumb', href: row.url || shotLink(row.name), target: '_blank',
        rel: 'noopener', title: t('openShot') }, [
        el('img', { src: row.url || shotLink(row.name), alt: row.name || '', loading: 'lazy' }),
      ]),
      el('div', { class: 'mas-shot-meta' }, compact([
        el('b', { text: formatDateTime(row.createdAt) }),
        el('span', {
          text: [ROLE_FA[row.role] || row.role || '',
            row.image ? `${row.image.width}×${row.image.height}` : ''].filter(Boolean).join(' · '),
        }),
        row.stepLabel ? el('span', { class: 'mas-shot-step', text: `#${row.stepIndex} ${row.stepLabel}` }) : null,
        row.title ? el('span', { class: 'mas-shot-step', text: truncate(row.title, 60) }) : null,
      ])),
      el('div', { class: 'mas-shot-actions' }, [
        el('a', { class: 'mas-btn', href: row.url || shotLink(row.name), target: '_blank',
          rel: 'noopener', text: '🔗' , title: t('shotLink') }),
        button('🗑', async () => {
          if (!window.confirm(t('deleteShotConfirm'))) return;
          try {
            await api(`/shots/${encodeURIComponent(row.id)}`, { method: 'DELETE' });
            renderShots();
          } catch (error) { toast(error.message, 'error'); }
        }, { class: 'mas-btn mas-danger', title: t('delete') }),
      ]),
    ]))));
}

/* ------------------------------------------------------------------ *
 * Extracted texts tab: what the system read off a page and kept
 * ------------------------------------------------------------------ */

async function renderTexts(host) {
  const pane = host || document.getElementById('mas-tab-texts');
  if (!pane) return;
  replace(pane, el('p', { class: 'mas-empty', text: '…' }));
  let rows = [];
  try {
    const data = await api('/texts');
    rows = data.texts || [];
  } catch (error) {
    replace(pane, el('p', { class: 'mas-empty', text: error.message }));
    return;
  }
  const noteBox = el('textarea', {
    class: 'mas-input mas-area', rows: '3', placeholder: t('noteHint'),
  });
  replace(pane,
    el('div', { class: 'mas-row mas-wrap' }, [
      button('⟳ ' + t('refresh'), renderTexts),
      button('💾 ' + t('saveNote'), async () => {
        const text = noteBox.value.trim();
        if (!text) { toast(t('noteEmpty'), 'warn'); return; }
        try {
          await api('/texts', { method: 'POST', body: { text, source: 'manual' } });
          renderTexts();
          toast(t('saved'), 'ok');
        } catch (error) { toast(error.message, 'error'); }
      }, { class: 'mas-btn mas-primary' }),
      el('span', { class: 'mas-hint', text: `${rows.length} ${t('textsCount')}` }),
    ]),
    noteBox,
    rows.length
      ? el('div', { class: 'mas-texts' }, rows.map((row) => el('details', { class: 'mas-text' }, [
        el('summary', {}, compact([
          el('b', { text: formatDateTime(row.createdAt) }),
          el('span', {
            class: 'mas-text-src',
            text: ` · ${row.source || ''}${row.stepLabel ? ` · ${row.stepLabel}` : ''} · ${row.chars || (row.text || '').length}`,
          }),
        ])),
        el('pre', { text: row.text || '' }),
        el('div', { class: 'mas-row' }, [
          button('📋', () => copyText(row.text || ''), { title: t('copy') }),
          button('🗑', async () => {
            if (!window.confirm(t('deleteTextConfirm'))) return;
            try {
              await api(`/texts/${encodeURIComponent(row.id)}`, { method: 'DELETE' });
              renderTexts();
            } catch (error) { toast(error.message, 'error'); }
          }, { class: 'mas-btn mas-danger', title: t('delete') }),
        ]),
      ])))
      : el('p', { class: 'mas-empty', text: t('textsEmpty') }),
  );
}

/* ------------------------------------------------------------------ *
 * AI tab
 * ------------------------------------------------------------------ */

function renderAgentAssistant(host) {
  // Its home is the chat tab's toolbox now; the old agent sub-pane id stays as
  // a fallback so an older panel.html keeps working.
  const pane = host || document.getElementById('mas-prompt-host')
    || document.getElementById('mas-chat-assistant')
    || document.getElementById('mas-agent-sub');
  if (!pane) return;
  // The model's whole answer. Kept so the next prompt can hand it back, which
  // is what turns one shot into a conversation.
  const importBox = el('textarea', {
    id: 'mas-ai-reply', class: 'mas-input mas-area', rows: '10',
    value: state.aiReply, placeholder: t('importHint'),
    onInput: (event) => {
      state.aiReply = event.target.value;
      try { localStorage.setItem(LS.reply, state.aiReply); } catch (_) { /* quota */ }
    },
  });
  // What the human wants done on this page. Kept in state so switching tabs
  // does not lose it, and sent to /prompt as part of the prompt.
  const requestBox = el('textarea', {
    id: 'mas-ai-request', class: 'mas-input mas-area', rows: '4',
    value: state.userRequest, placeholder: t('aiRequestHint'),
    onInput: (event) => {
      state.userRequest = event.target.value;
      try { localStorage.setItem(LS.request, state.userRequest); } catch (_) { /* quota */ }
    },
  });

  replace(pane,
    el('label', { class: 'mas-field mas-wide' }, [
      el('span', { text: t('token') }),
      el('input', {
        id: 'mas-ai-token', class: 'mas-input', type: 'password',
        value: state.token, placeholder: t('tokenHint'),
        onInput: (event) => setToken(event.target.value),
      }),
    ]),
    el('label', { class: 'mas-field mas-wide' }, [
      el('span', { text: t('aiRequest') }),
      requestBox,
    ]),
    el('div', { class: 'mas-row mas-wrap' }, [
      button('📄 ' + t('copyGuide'), () => copyFrom('/guide')),
      button('🧩 ' + t('copyPrompt'), buildPrompt, { class: 'mas-btn mas-primary' }),
      button('🤖 ' + t('agentChatDirect'), askAgentDirectly, { class: 'mas-btn mas-primary' }),
      button('⤓ JSON', () => copyText(JSON.stringify({ name: state.flow.name, steps: promptSteps(state.flow) }, null, 2))),
    ]),
    el('div', { class: 'mas-row' }, [
      button(t('importBtn'), async () => {
        const raw = importBox.value;
        // Show what the model said even when its code turns out to be unusable:
        // its summary and questions are the point of the exchange.
        state.aiNotes = aiNotesFrom(raw);
        const parsed = extractJson(raw);
        if (!parsed) { renderAgentAssistant(); toast('JSON?', 'error'); return; }
        const { flow, errors } = normaliseImportedFlow(
          await convertFlowServer(parsed), state.flow.viewport);
        if (!flow || !flow.steps.length) {
          renderAgentAssistant();
          toast(errors.join(' | '), 'error');
          return;
        }
        state.flow = flow;
        persistFlow();
        renderFlow();
        selectTab('flow');
        toast(errors.length ? errors[0] : `${flow.steps.length} steps`, errors.length ? 'warn' : 'ok');
      }, { class: 'mas-btn mas-primary' }),
    ]),
    importBox,
    state.agentFlowProposal
      ? el('div', { class: 'mas-box' }, [
        el('b', { text: `🧩 ${(state.agentFlowProposal.steps || []).length} steps` }),
        el('div', { class: 'mas-row mas-wrap' }, [
          button(t('importBtn'), () => {
            state.flow = state.agentFlowProposal;
            state.agentFlowProposal = null;
            persistFlow();
            renderFlow();
            selectTab('flow');
          }, { class: 'mas-btn mas-primary' }),
          button('✕', () => { state.agentFlowProposal = null; renderAgentAssistant(); }),
        ]),
      ])
      : null,
    state.aiNotes
      ? el('div', { class: 'mas-ai-notes' }, [
        el('b', { text: '💬 ' + t('aiNotes') }),
        el('pre', { text: state.aiNotes }),
      ])
      : null,
    el('p', { class: 'mas-hint', text: `${state.flow.steps.length} steps · ${state.flow.name}` }),
  );
}

/** The model's prose with the JSON block taken out: its summary and questions. */
function aiNotesFrom(raw) {
  return String(raw || '')
    .replace(/```json[\s\S]*?```/gi, '')
    .replace(/```[\s\S]*?```/g, '')
    .trim();
}

async function copyFrom(path) {
  try {
    const response = await fetch(API_PREFIX + path, { headers: { 'X-Automation-Token': state.token } });
    copyText(await response.text());
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function buildPrompt() {
  // The copied prompt must be self-contained, so it carries the user's request,
  // the detected page, and an absolute link to its screenshot. Fall back to the
  // most recent detection when no page is selected.
  const page = state.activePage || (state.pages || [])[0] || null;
  const body = { flow: state.flow, publicBase: window.location.origin };
  if (page) body.pageId = page.id;
  if (state.userRequest && state.userRequest.trim()) body.request = state.userRequest;
  // Hand the model its own previous answer back, so it can iterate on it.
  if (state.aiReply && state.aiReply.trim()) body.previousReply = state.aiReply;
  if (!page) toast(t('aiRequestNone'), 'warn');
  try {
    const response = await fetch(API_PREFIX + '/prompt', {
      method: 'POST',
      headers: { 'X-Automation-Token': state.token, 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    copyText(await response.text());
  } catch (error) {
    toast(error.message, 'error');
  }
}

/* ------------------------------------------------------------------ *
 * Coworker agent tab
 *
 * One tab, seven sub-panes, because "the AI section" had grown into seven
 * unrelated jobs: talking to a model, scheduling, captcha strategy, Telegram,
 * the script gate, the data, and the keys. Each sub-pane owns its own API calls
 * so a failure in one cannot blank the others.
 * ------------------------------------------------------------------ */

// «پرامپت» comes first on purpose: building a prompt for a model is its own
// workspace, separate from the ordinary chat, and it is where the panel opens.
const AGENT_SUBS = ['prompt', 'pages', 'suggestions', 'agentkey', 'telegram',
  'browser', 'cursor', 'jobs', 'captcha', 'scripts', 'data', 'keys'];

const AGENT_RENDERERS = {
  prompt: renderAgentPrompt,
  pages: renderAgentPages,
  suggestions: renderAgentSuggestions,
  agentkey: renderAgentKey,
  cursor: renderAgentCursor,
  jobs: renderAgentJobs,
  captcha: renderAgentCaptcha,
  telegram: renderAgentTelegram,
  browser: renderAgentBrowser,
  scripts: renderAgentScripts,
  data: renderAgentData,
  keys: renderAgentKeys,
};

function agentSubLabel(name) {
  return t('agentSub' + name.charAt(0).toUpperCase() + name.slice(1));
}

function agentField(labelText, node) {
  return el('label', { class: 'mas-field mas-wide' }, [
    el('span', { text: labelText }), node,
  ]);
}

function agentSelect(options, value, onChange) {
  return el('select', { class: 'mas-input', onChange }, options.map((option) => {
    const item = typeof option === 'string' ? { value: option, label: option } : option;
    return el('option', { value: item.value, selected: String(item.value) === String(value) },
      item.label);
  }));
}

function agentTime(value) {
  if (!value) return '-';
  try { return formatDateTime(value); } catch (_) { return String(value); }
}

function agentCounts(overview) {
  const counts = overview.counts || {};
  const pending = counts.pendingScripts || 0;
  return [
    `${counts.jobs || 0} ${t('agentSubJobs')}`,
    `${counts.scripts || 0} ${t('agentSubScripts')}`,
    pending ? `⚠ ${pending} ${t('agentScriptStatus')}: pending` : '',
    (overview.llm || {}).provider || '',
  ].filter(Boolean).join(' · ');
}

function agentTable(columns, rows) {
  return el('div', { class: 'mas-tablewrap' }, [
    el('table', { class: 'mas-table' }, [
      el('thead', {}, el('tr', {}, columns.map((name) => el('th', { text: name })))),
      el('tbody', {}, rows.map((cells) => el('tr', {}, cells.map((cell) => el('td', {},
        typeof cell === 'string' || typeof cell === 'number' ? [String(cell)] : [cell]))))),
    ]),
  ]);
}

async function agentError(host, error) {
  replace(host, el('p', { class: 'mas-empty', text: error.message || String(error) }));
}

async function renderAgent() {
  const pane = document.getElementById('mas-tab-agent');
  if (!pane) return;
  const active = state.agentSub || AGENT_SUBS[0];
  // The status bar is filled in separately, on purpose: it must keep working
  // even when this deployment has no agent attached at all (--no-agent, or an
  // unreachable API), while a sub-pane is allowed to show that error itself.
  replace(pane,
    // Sub-tabs speak the exact same visual language as the top tabs.
    el('div', { class: 'mas-tabs mas-subtabs' }, AGENT_SUBS.map((name) => el('button', {
      class: 'mas-tab' + (name === active ? ' is-active' : ''),
      dataset: { sub: name }, type: 'button', text: agentSubLabel(name),
      onClick: () => selectAgentSub(name),
    }))),
    el('div', { id: 'mas-agent-bar', class: 'mas-row mas-wrap' },
      [el('span', { class: 'mas-hint', text: '\u2026' })]),
    el('div', { id: 'mas-agent-sub', class: 'mas-agent-sub' }),
  );
  renderAgentSub(active);
  refreshAgentBar();
}

async function refreshAgentBar() {
  const bar = document.getElementById('mas-agent-bar');
  if (!bar) return;
  let overview;
  try {
    overview = await api('/agent');
  } catch (error) {
    state.agent = null;
    // A 404 means no agent was built here, which is a configuration, not a fault.
    replace(bar, el('span', {
      class: 'mas-hint',
      text: error.status === 404 ? t('agentNotAttached') : (error.message || ''),
    }));
    return;
  }
  state.agent = overview;
  replace(bar,
    button((overview.killSwitch ? '\u25b6 ' : '\u23f8 ') + t('agentKillSwitch'), async () => {
      try {
        await api('/agent/kill-switch', { method: 'POST', body: { on: !overview.killSwitch } });
        toast(t('agentSaved'), 'ok');
        refreshAgentBar();
      } catch (error) { toast(error.message, 'error'); }
    }, { class: 'mas-btn' + (overview.killSwitch ? ' mas-danger' : '') }),
    el('span', { class: 'mas-hint', text: agentCounts(overview) }),
    overview.killSwitch
      ? el('span', { class: 'mas-warn', text: t('agentKillSwitchHint') })
      : null);
}

function selectAgentSub(name) {
  state.agentSub = name;
  saveUiPref('agentSub', name);
  renderAgent();
}

/* The prompt workspace: copy-prompt / import-JSON, as its own sub-tab. */
function renderAgentPrompt(host) {
  // One card wrapper keeps the sub-pane contract (a .mas-box inside
  // #mas-agent-sub) and gives re-renders a stable home id.
  const wrap = el('div', { id: 'mas-prompt-host', class: 'mas-box mas-card' });
  replace(host, wrap);
  renderAgentAssistant(wrap);
}

function renderAgentSub(name) {
  const host = document.getElementById('mas-agent-sub');
  if (!host) return;
  (AGENT_RENDERERS[name] || AGENT_RENDERERS[AGENT_SUBS[0]])(host);
}

/* -- jobs ------------------------------------------------------------ */

async function renderAgentJobs(host) {
  replace(host, el('p', { class: 'mas-empty', text: '…' }));
  let jobs; let flows = []; let scripts = [];
  try {
    jobs = (await api('/agent/jobs')).jobs || [];
    flows = ((await api('/flows')).flows || []).map((item) => item.name);
    scripts = ((await api('/agent/scripts')).scripts || []);
  } catch (error) { return agentError(host, error); }

  const draft = state.agentJobDraft || {};
  const kind = draft.kind || 'cron';
  const action = (draft.payload && draft.payload.action) || 'flow';
  const kindBox = agentSelect(['at', 'every', 'cron'], kind,
    (event) => { state.agentJobDraft = Object.assign({}, draft, { kind: event.target.value }); renderAgentSub('jobs'); });
  const actionBox = agentSelect([
    { value: 'flow', label: 'flow' }, { value: 'script', label: 'script' },
    { value: 'prompt', label: 'prompt' }], action,
    (event) => {
      state.agentJobDraft = Object.assign({}, draft, { payload: { action: event.target.value, text: (draft.payload || {}).text || '' } });
      renderAgentSub('jobs');
    });
  const targetOptions = action === 'script'
    ? scripts.map((item) => ({ value: item.id, label: `${item.name} (${item.status})` }))
    : flows.map((name) => ({ value: name, label: name }));
  const targetBox = action === 'prompt'
    ? el('textarea', {
      class: 'mas-input mas-area', rows: '3', value: (draft.payload || {}).text || draft.target || '',
      placeholder: t('agentChatPlaceholder'),
      onInput: (event) => { state.agentJobDraft = Object.assign({}, draft, { target: event.target.value, payload: Object.assign({}, draft.payload || {}, { action: 'prompt', text: event.target.value }) }); },
    })
    : agentSelect(targetOptions, draft.target || '', (event) => { state.agentJobDraft = Object.assign({}, draft, { target: event.target.value }); });

  const form = el('div', { class: 'mas-box' }, [
    el('b', { text: (draft.id ? '✎ ' : '＋ ') + t('agentSubJobs') }),
    agentField(t('agentJobName'), el('input', {
      class: 'mas-input', value: draft.name || '',
      onInput: (event) => { state.agentJobDraft = Object.assign({}, draft, { name: event.target.value }); },
    })),
    el('div', { class: 'mas-row mas-wrap' }, [
      agentField(t('agentJobKind'), kindBox),
      agentField(t('agentJobSchedule'), el('input', {
        class: 'mas-input', value: draft.schedule || '', placeholder: '30 7 * * 6',
        onInput: (event) => { state.agentJobDraft = Object.assign({}, draft, { schedule: event.target.value }); },
      })),
    ]),
    el('div', { class: 'mas-row mas-wrap' }, [
      agentField(t('agentJobAction'), actionBox),
      agentField(t('agentJobTarget'), targetBox),
    ]),
    el('p', { class: 'mas-hint', text: t('agentJobHint') }),
    el('div', { class: 'mas-row mas-wrap' }, [
      button('💾 ' + t('agentSave'), async () => {
        const payload = Object.assign({}, draft, { enabled: draft.enabled !== false });
        try {
          if (payload.id) await api(`/agent/jobs/${encodeURIComponent(payload.id)}`, { method: 'PUT', body: payload });
          else await api('/agent/jobs', { method: 'POST', body: payload });
          state.agentJobDraft = null;
          toast(t('agentSaved'), 'ok');
          renderAgent();
        } catch (error) { toast(error.message, 'error'); }
      }, { class: 'mas-btn mas-primary' }),
      draft.id ? button('✕', () => { state.agentJobDraft = null; renderAgentSub('jobs'); }) : null,
    ]),
  ]);

  const rows = jobs.length ? jobs.map((job) => [
    job.name || job.id,
    `${job.kind}: ${job.schedule}`,
    (job.payload || {}).action || 'flow',
    agentTime(job.next_run_at),
    job.enabled ? '✓' : '⏸',
    job.last_status ? `${job.last_status} ${agentTime(job.last_run_at)}` : '-',
    el('div', { class: 'mas-row' }, [
      button(t('agentJobNow'), async () => {
        try {
          const result = await api('/agent/job-run', { method: 'POST', body: { id: job.id } });
          if (result.status === 'queued') toast(t('agentJobQueued'), 'ok');
          else toast(result.status, result.status === 'done' ? 'ok' : 'warn');
          renderAgentSub('jobs');
        } catch (error) { toast(error.message, 'error'); }
      }),
      button(job.enabled ? '⏸' : '▶', async () => {
        try { await api(`/agent/jobs/${encodeURIComponent(job.id)}`, { method: 'PUT', body: { enabled: !job.enabled } }); renderAgentSub('jobs'); } catch (error) { toast(error.message, 'error'); }
      }),
      button('✎', () => { state.agentJobDraft = JSON.parse(JSON.stringify(job)); renderAgentSub('jobs'); }),
      button('🗑', async () => {
        if (!window.confirm(t('agentConfirmDelete'))) return;
        try { await api(`/agent/jobs/${encodeURIComponent(job.id)}`, { method: 'DELETE' }); renderAgentSub('jobs'); } catch (error) { toast(error.message, 'error'); }
      }),
    ]),
  ]) : [[t('agentEmpty'), '', '', '', '', '', '']];

  replace(host, form,
    jobs.length ? agentTable([t('agentJobName'), t('agentJobSchedule'), t('agentJobAction'),
      t('agentJobNext'), '', t('agentJobLast'), ''], rows) : null,
    el('p', { class: 'mas-hint', text: jobs.length ? '' : t('agentEmpty') }));
}

/* -- scripts --------------------------------------------------------- */

async function renderAgentScripts(host) {
  replace(host, el('p', { class: 'mas-empty', text: '…' }));
  let data;
  try { data = await api('/agent/scripts'); } catch (error) { return agentError(host, error); }
  const scripts = data.scripts || [];
  const config = data.config || {};
  const draft = state.agentScriptDraft || { language: 'python3', code: '' };

  const form = el('div', { class: 'mas-box' }, [
    el('b', { text: (draft.id ? '✎ ' : '＋ ') + t('agentSubScripts') }),
    el('div', { class: 'mas-row mas-wrap' }, [
      agentField(t('agentJobName'), el('input', {
        class: 'mas-input', value: draft.name || '',
        onInput: (event) => { state.agentScriptDraft = Object.assign({}, draft, { name: event.target.value }); },
      })),
      agentField(t('agentScriptLang'), agentSelect(config.languages || ['bash', 'python3'],
        draft.language, (event) => { state.agentScriptDraft = Object.assign({}, draft, { language: event.target.value }); renderAgentSub('scripts'); })),
    ]),
    agentField(t('agentScriptCode'), el('textarea', {
      class: 'mas-input mas-area mas-code', rows: '8', value: draft.code || '', spellcheck: 'false',
      onInput: (event) => { state.agentScriptDraft = Object.assign({}, draft, { code: event.target.value }); },
    })),
    el('p', { class: 'mas-warn', text: t('agentScriptGate') }),
    el('div', { class: 'mas-row mas-wrap' }, [
      button('💾 ' + t('agentSave'), async () => {
        try {
          if (draft.id) await api(`/agent/scripts/${encodeURIComponent(draft.id)}`, { method: 'PUT', body: draft });
          else await api('/agent/scripts', { method: 'POST', body: draft });
          state.agentScriptDraft = null;
          toast(t('agentSaved'), 'ok');
          renderAgentSub('scripts');
        } catch (error) { toast(error.message, 'error'); }
      }, { class: 'mas-btn mas-primary' }),
      draft.id ? button('✕', () => { state.agentScriptDraft = null; renderAgentSub('scripts'); }) : null,
      config.running ? button('⏹ ' + t('agentKill'), async () => {
        try { await api('/agent/script-kill', { method: 'POST', body: {} }); renderAgentSub('scripts'); } catch (error) { toast(error.message, 'error'); }
      }, { class: 'mas-btn mas-danger' }) : null,
    ]),
    el('p', { class: 'mas-hint', text: `env: ${(config.strippedEnv || []).join(', ')} ${config.passToken ? '' : '(stripped)'}` }),
  ]);

  const rows = scripts.length ? scripts.map((script) => [
    script.name || script.id,
    script.language,
    script.status === 'approved' ? '✅ approved' : (script.status === 'rejected' ? '⛔ rejected' : '⏳ pending'),
    `${script.run_count || 0}× ${script.last_exit === null || script.last_exit === undefined ? '' : 'exit=' + script.last_exit}`,
    el('div', { class: 'mas-row mas-wrap' }, [
      script.status === 'approved' ? null : button('✓ ' + t('agentApprove'), async () => {
        try { await api('/agent/script-decision', { method: 'POST', body: { id: script.id, approve: true } }); renderAgentSub('scripts'); } catch (error) { toast(error.message, 'error'); }
      }, { class: 'mas-btn mas-primary' }),
      script.status === 'rejected' ? null : button('✕ ' + t('agentReject'), async () => {
        try { await api('/agent/script-decision', { method: 'POST', body: { id: script.id, approve: false } }); renderAgentSub('scripts'); } catch (error) { toast(error.message, 'error'); }
      }),
      button('▶ ' + t('agentRun'), async () => {
        try {
          const result = await api('/agent/script-run', { method: 'POST', body: { id: script.id } });
          state.agentScriptOutput = result;
          renderAgentSub('scripts');
        } catch (error) { toast(error.message, error.status === 409 ? 'warn' : 'error'); }
      }),
      button('✎', () => { state.agentScriptDraft = JSON.parse(JSON.stringify(script)); renderAgentSub('scripts'); }),
      button('🗑', async () => {
        if (!window.confirm(t('agentConfirmDelete'))) return;
        try { await api(`/agent/scripts/${encodeURIComponent(script.id)}`, { method: 'DELETE' }); renderAgentSub('scripts'); } catch (error) { toast(error.message, 'error'); }
      }),
    ]),
  ]) : null;

  replace(host, form,
    rows ? agentTable([t('agentJobName'), t('agentScriptLang'), t('agentScriptStatus'), t('agentRun'), ''], rows) : el('p', { class: 'mas-hint', text: t('agentEmpty') }),
    state.agentScriptOutput ? el('div', { class: 'mas-box' }, [
      el('b', { text: `${t('agentScriptOutput')} · exit ${state.agentScriptOutput.exitCode} · ${state.agentScriptOutput.elapsed}s` }),
      el('pre', { class: 'mas-pre', text: state.agentScriptOutput.output || '(empty)' }),
    ]) : null);
}

/* -- captcha --------------------------------------------------------- */

async function renderAgentCaptcha(host) {
  replace(host, el('p', { class: 'mas-empty', text: '…' }));
  let info; let history = [];
  try {
    info = await api('/agent/captcha/config');
    history = (await api('/agent/captcha/history')).history || [];
  } catch (error) { return agentError(host, error); }
  const config = info.config || {};
  const extension = info.extension || {};
  const draft = Object.assign({}, config);
  const strategies = config.strategies || [];

  const strategyBoxes = ['vision', 'human', 'extension'].map((name) => el('label', { class: 'mas-check' }, [
    el('input', {
      type: 'checkbox', checked: strategies.indexOf(name) >= 0,
      onChange: (event) => {
        const next = strategies.slice();
        if (event.target.checked) next.push(name);
        else next.splice(next.indexOf(name), 1);
        draft.strategies = next;
      },
    }),
    el('span', { text: name }),
  ]));

  const form = el('div', { class: 'mas-box' }, [
    el('b', { text: t('agentSubCaptcha') }),
    el('div', { class: 'mas-row mas-wrap' }, strategyBoxes),
    el('div', { class: 'mas-row mas-wrap' }, [
      agentField(t('agentCaptchaMaxAttempts'), el('input', {
        class: 'mas-input', type: 'number', min: '1', max: '10', value: String(config.maxAttempts || 3),
        onInput: (event) => { draft.maxAttempts = Number(event.target.value); },
      })),
      agentField(t('agentCaptchaMinConfidence'), el('input', {
        class: 'mas-input', type: 'number', step: '0.05', min: '0', max: '1', value: String(config.minConfidence || 0.6),
        onInput: (event) => { draft.minConfidence = Number(event.target.value); },
      })),
      agentField(t('agentCaptchaExtension'), agentSelect(['none', 'buster', 'nopecha'],
        config.extension || 'none', (event) => { draft.extension = event.target.value; })),
    ]),
    el('label', { class: 'mas-check' }, [
      el('input', { type: 'checkbox', checked: Boolean(config.autoClick), onChange: (event) => { draft.autoClick = event.target.checked; } }),
      el('span', { text: t('agentCaptchaAutoClick') }),
    ]),
    el('p', { class: 'mas-hint', text: t('agentCaptchaHint') }),
    extension.name && extension.name !== 'none' ? el('p', { class: 'mas-warn', text: `${extension.name}: ${extension.covers || ''} — ${extension.warning || ''}` }) : null,
    el('div', { class: 'mas-row mas-wrap' }, [
      button('💾 ' + t('agentSave'), async () => {
        try { await api('/agent/captcha/config', { method: 'POST', body: draft }); toast(t('agentSaved'), 'ok'); renderAgentSub('captcha'); } catch (error) { toast(error.message, 'error'); }
      }, { class: 'mas-btn mas-primary' }),
      button('🔍 ' + t('agentCaptchaSolve'), async () => {
        try {
          state.agentCaptchaResult = await api('/agent/captcha/solve', { method: 'POST', body: {} });
          renderAgentSub('captcha');
        } catch (error) { toast(error.message, 'error'); }
      }),
    ]),
  ]);

  const result = state.agentCaptchaResult;
  const proposal = result && result.result && (result.result.actions || []).length
    ? el('div', { class: 'mas-box' }, [
      el('b', { text: `${t('agentCaptchaProposal')} · ${result.strategy}${result.needsApproval ? ' · ⏳' : ''}` }),
      el('p', { class: 'mas-hint', text: [result.result.kind, `confidence ${result.result.confidence}`, result.result.summary].filter(Boolean).join(' · ') }),
      agentTable(['type', 'x', 'y', 'note'], (result.result.actions || []).map((action) => [
        action.type, action.x === undefined ? '-' : action.x, action.y === undefined ? '-' : action.y, action.note || action.text || (action.keys || []).join('+'),
      ])),
      (result.result.rejected || []).length ? el('p', { class: 'mas-warn', text: `refused: ${result.result.rejected.map((item) => item.reason).join('; ')}` }) : null,
      result.needsApproval ? button('▶ ' + t('agentRun'), async () => {
        try {
          await api('/agent/captcha/execute', { method: 'POST', body: { actions: result.result.actions } });
          toast('ok', 'ok');
          state.agentCaptchaResult = null;
          renderAgentSub('captcha');
        } catch (error) { toast(error.message, 'error'); }
      }, { class: 'mas-btn mas-primary' }) : null,
    ]) : null;

  const historyRows = history.length ? history.slice(0, 20).map((item) => [
    agentTime(item.at), item.event, item.kind || item.label || '', item.confidence === undefined ? '' : String(item.confidence),
    truncate(item.summary || item.error || '', 60),
  ]) : null;

  replace(host, form, proposal,
    historyRows ? el('div', { class: 'mas-box' }, [
      el('b', { text: t('agentCaptchaHistory') }),
      agentTable(['time', 'event', 'kind', 'conf', ''], historyRows),
    ]) : null);
}

/* -- telegram -------------------------------------------------------- */

/* Time in Tehran, the way the sample menus show it: Persian digits and the
 * Asia/Tehran zone, whatever zone the server or the browser happens to be in. */
function tehranWhen(epoch) {
  if (!epoch) return '';
  try {
    return new Intl.DateTimeFormat('fa-IR', {
      timeZone: 'Asia/Tehran', dateStyle: 'medium', timeStyle: 'short',
    }).format(new Date(Number(epoch) * 1000));
  } catch (_) {
    return formatDateTime ? formatDateTime(epoch) : String(epoch);
  }
}

function tgStatusText(status) {
  return t('tgSt' + String(status || '').charAt(0).toUpperCase()
    + String(status || '').slice(1)) || String(status);
}

function tgPurposeText(purpose) {
  const key = 'tgPurpose' + String(purpose || 'manual').charAt(0).toUpperCase()
    + String(purpose || 'manual').slice(1);
  return t(key);
}

async function renderAgentTelegram(host) {
  replace(host, el('p', { class: 'mas-empty', text: '\u2026' }));
  let status; let mailbox = { messages: [], counts: {} };
  try {
    status = await api('/agent/telegram/status');
    mailbox = await api('/agent/telegram/log?limit=60');
  } catch (error) { return agentError(host, error); }

  const targets = status.targets || [];
  const channels = status.channels || {};
  const routing = status.routing || {};
  const purposes = status.purposes || ['handoff', 'captcha', 'jobs', 'manual'];
  const counts = mailbox.counts || {};
  const draft = { mode: status.mode, routing: Object.assign({}, routing) };
  const test = { target: '', channel: '', text: '' };
  const send = { target: '', channel: '', text: '', purpose: 'manual' };
  const wantsAccount = draft.mode === 'account' || draft.mode === 'both';

  const channelLabel = (name) => t('tgChannel' + name.charAt(0).toUpperCase() + name.slice(1));
  const tgTypeLabel = (type) => {
    const name = String(type || 'private');
    return t('tgType' + name.charAt(0).toUpperCase() + name.slice(1));
  };
  const routeOptions = [
    { value: '', label: t('tgRouteFollow') },
    { value: 'bot', label: channelLabel('bot') },
    { value: 'account', label: channelLabel('account') },
    { value: 'both', label: t('tgModeBoth') },
  ];

  /* -- the status card: one glance answers "is it working?" ------------- */
  const botInfo = channels.bot || {};
  const accountInfo = channels.account || {};
  const modeBadge = { off: '⛔', bot: '🤖', account: '👤', both: '🤖' }[draft.mode] || '❔';
  const statusCard = el('div', { class: 'mas-box mas-card mas-tg-status' }, [
    el('div', { class: 'mas-card-head' }, [
      el('b', { text: '📡 ' + t('tgStatusTitle') }),
      el('span', { class: 'mas-chip mas-chip-ok', text: modeBadge + ' ' + t('tgModeLine') + ': ' + t('tgMode' + draft.mode.charAt(0).toUpperCase() + draft.mode.slice(1)) }),
      el('span', { class: 'mas-chip', text: '🤖 ' + (botInfo.configured ? t('tgReady') : t('tgNotReady')) }),
      el('span', { class: 'mas-chip', text: '👤 ' + (accountInfo.sessionExists ? t('tgReady') : t('tgNotReady')) }),
    ]),
    el('div', { class: 'mas-row mas-wrap mas-chips' }, [
      el('span', { class: 'mas-chip', text: t('tgCountAll') + ' ' + (counts.all || 0) }),
      el('span', { class: 'mas-chip', text: '⏳ ' + t('tgCountQueued') + ' ' + (counts.queued || 0) }),
      el('span', { class: 'mas-chip mas-chip-ok', text: '📤 ' + t('tgCountSent') + ' ' + (counts.sent || 0) }),
      el('span', { class: 'mas-chip mas-chip-bad', text: '❌ ' + t('tgCountFailed') + ' ' + (counts.failed || 0) }),
      el('span', { class: 'mas-chip', text: '🚫 ' + t('tgCountCancelled') + ' ' + (counts.cancelled || 0) }),
    ]),
    el('div', { class: 'mas-row mas-wrap' }, [
      agentField(t('agentTgMode'), agentSelect([
        { value: 'off', label: t('tgModeOff') },
        { value: 'bot', label: t('tgModeBot') },
        { value: 'account', label: t('tgModeAccount') },
        { value: 'both', label: t('tgModeBoth') },
      ], draft.mode, async (event) => {
        const next = event.target.value;
        draft.mode = next;
        // A control this prominent has to save itself. The old code only
        // touched the local draft, the save button lives on another sub-tab
        // and every sub-tab switch re-renders from the server — so the choice
        // evaporated and telegram.mode could not be changed from the UI at
        // all. Changing this select now persists it on the spot.
        try {
          await api('/agent/settings', { method: 'POST',
                                         body: { 'telegram.mode': next } });
          if ((next === 'account' || next === 'both')
              && !(channels.account || {}).sessionExists) {
            toast(t('tgModeSavedNoAccount'), 'warn', { sticky: true });
          } else {
            toast(t('agentSaved'), 'ok');
          }
          renderAgentSub('telegram');
        } catch (error) { toast(error.message, 'error'); }
      })),
      el('span', { class: 'mas-hint', text: t('tgModeInstantHint') }),
      el('span', { class: 'mas-hint', text: t('tgLastUpdate') + ' ' + tehranWhen(Math.floor(Date.now() / 1000)) }),
    ]),
    el('details', { class: 'mas-help' }, [
      el('summary', { text: '📖 ' + t('tgHelpTitle') }),
      el('p', { text: t('tgHelpMode') }),
      el('p', { text: t('tgHelpChannels') }),
      el('p', { text: t('tgHelpRouting') }),
    ]),
    status.error ? el('p', { class: 'mas-warn', text: status.error }) : null,
  ]);

  /* -- settings: channels, routing, credentials ---------------------- */
  const channelRows = ['bot', 'account'].map((name) => {
    const info = channels[name] || {};
    const ready = name === 'bot' ? info.configured : info.sessionExists;
    return [
      channelLabel(name),
      ready ? '\u2713 ' + t('tgReady') : '\u2715 ' + t('tgNotReady'),
      info.needs || '',
      info.sessionPath ? String(info.sessionPath) : '',
    ];
  });
  const saveSettings = async (extra) => {
    const body = Object.assign({ 'telegram.mode': draft.mode }, extra || {});
    for (const purpose of purposes) {
      body['telegram.channel.' + purpose] = draft.routing[purpose] || '';
    }
    try {
      await api('/agent/settings', { method: 'POST', body });
      toast(t('agentSaved'), 'ok');
      renderAgentSub('telegram');
    } catch (error) { toast(error.message, 'error'); }
  };
  const settingsCard = el('div', { class: 'mas-box mas-card' }, [
    el('div', { class: 'mas-card-head' }, [el('b', { text: '⚙️ ' + t('tgSettingsTitle') })]),
    el('p', { class: 'mas-hint', text: t('tgChannelsHint') }),
    agentTable([t('tgChannel'), t('tgState'), t('tgNeeds'), t('tgSession')], channelRows),
    el('b', { class: 'mas-subhead', text: '🔀 ' + t('tgRouting') }),
    el('p', { class: 'mas-hint', text: t('tgRoutingHint') }),
    purposes.map((purpose) => el('div', { class: 'mas-row mas-wrap mas-tg-route' }, [
      el('span', {
        class: 'mas-tg-purpose',
        text: t('tgPurpose' + purpose.charAt(0).toUpperCase() + purpose.slice(1)),
      }),
      agentSelect(routeOptions, draft.routing[purpose] || '', (event) => {
        draft.routing[purpose] = event.target.value;
      }),
    ])),
    el('b', { class: 'mas-subhead', text: '🤖 ' + channelLabel('bot') }),
    el('details', { class: 'mas-help' }, [
      el('summary', { text: '📖 ' + t('tgHelpBotTitle') }),
      el('p', { text: t('tgHelpBot') }),
    ]),
    agentField(t('agentTgBotToken'), el('input', {
      class: 'mas-input', type: 'password',
      placeholder: status.botTokenSet ? t('tgStored') : '',
      onInput: (event) => { draft.botToken = event.target.value; },
    })),
    el('b', { class: 'mas-subhead', text: '👤 ' + channelLabel('account') }),
    wantsAccount ? el('div', { class: 'mas-wrap' }, [
      el('details', { class: 'mas-help' }, [
        el('summary', { text: '📖 ' + t('tgHelpAccountTitle') }),
        el('p', { text: t('tgHelpAccount') }),
      ]),
      el('p', { class: 'mas-warn', text: t('agentTgAccountWarn') }),
      el('div', { class: 'mas-row mas-wrap' }, [
        agentField('api_id', el('input', {
          class: 'mas-input',
          placeholder: status.apiCredentialsSet ? t('tgStored') : '',
          onInput: (event) => { draft.apiId = event.target.value; },
        })),
        agentField('api_hash', el('input', {
          class: 'mas-input', type: 'password',
          placeholder: status.apiCredentialsSet ? t('tgStored') : '',
          onInput: (event) => { draft.apiHash = event.target.value; },
        })),
        agentField(t('agentTgPhone'), el('input', {
          class: 'mas-input', value: status.phone || '', placeholder: '+98...',
          onInput: (event) => { draft.phone = event.target.value; },
        })),
        agentField(t('agentTgCode'), el('input', {
          class: 'mas-input',
          onInput: (event) => { draft.code = event.target.value; },
        })),
      ]),
      el('div', { class: 'mas-row mas-wrap' }, [
        button(t('agentTgLogin'), async () => {
          try {
            await api('/agent/telegram/login', { method: 'POST', body: { phone: draft.phone } });
            toast(t('tgCodeSent'), 'ok');
          } catch (error) { toast(error.message, 'error'); }
        }),
        button(t('agentTgLoginFinish'), async () => {
          try {
            await api('/agent/telegram/login-finish', { method: 'POST', body: { code: draft.code } });
            toast(t('agentSaved'), 'ok');
            renderAgentSub('telegram');
          } catch (error) { toast(error.message, 'error'); }
        }, { class: 'mas-btn mas-primary' }),
      ]),
    ]) : el('p', { class: 'mas-hint', text: t('tgAccountOff') }),
    el('div', { class: 'mas-row mas-wrap' }, [
      button('💾 ' + t('agentSave'), () => saveSettings(Object.assign({},
        draft.botToken ? { 'telegram.botToken': draft.botToken } : {},
        draft.apiId ? { 'telegram.apiId': draft.apiId } : {},
        draft.apiHash ? { 'telegram.apiHash': draft.apiHash } : {})),
      { class: 'mas-btn mas-primary' }),
    ]),
  ]);

  /* -- targets: fully editable; the draft survives re-renders (round 16) - */
  // The working copy lives in `state`, not in a local const: adding or
  // deleting a row re-renders this pane, and a local draft evaporated on the
  // spot — that is exactly why "save targets" used to persist nothing and the
  // test dropdown never saw a fresh target.
  if (!Array.isArray(state.agentTgDraft)) {
    state.agentTgDraft = targets.map((target) => Object.assign({}, target));
  }
  const draftTargets = state.agentTgDraft;
  if (!state.agentTgAdd) state.agentTgAdd = { id: '', title: '', type: 'private' };
  const add = state.agentTgAdd;
  const found = state.agentTgFound || [];
  const normalizeId = (value) => Number(value) || String(value).trim();
  const tgTypes = [
    { value: 'private', label: '👤 ' + t('tgTypePrivate') },
    { value: 'group', label: '👥 ' + t('tgTypeGroup') },
    { value: 'supergroup', label: '🏢 ' + t('tgTypeSupergroup') },
    { value: 'channel', label: '📣 ' + t('tgTypeChannel') },
  ];
  const pushTarget = (target) => {
    const id = String(target.id || '').trim();
    if (!id) { toast(t('tgNeedId'), 'warn'); return false; }
    if (draftTargets.some((row) => String(row.id).trim() === id)) {
      toast(t('tgAlreadyAdded'), 'warn');
      return false;
    }
    draftTargets.push({ id: normalizeId(id), title: String(target.title || ''),
                        type: String(target.type || 'private') });
    renderAgentSub('telegram');
    return true;
  };
  const saveTargets = async () => {
    const rows = draftTargets.slice();
    // an add-form the operator filled but never pressed ➕ on is included too
    if (String(add.id || '').trim()) rows.push(Object.assign({}, add));
    const clean = rows
      .filter((target) => String(target.id || '').trim() !== '')
      .map((target) => ({
        id: normalizeId(target.id),
        title: String(target.title || ''),
        type: String(target.type || 'private'),
      }));
    try {
      await api('/agent/settings', { method: 'POST', body: { 'telegram.targets': clean } });
      state.agentTgDraft = null;   // next render reads the server again
      state.agentTgAdd = null;
      toast(t('tgTargetsSaved'), 'ok');
      renderAgentSub('telegram');
    } catch (error) { toast(error.message, 'error'); }
  };
  // Everything the test/send dropdowns may offer: the working draft (saved
  // rows plus not-yet-saved ones) and whatever the add form holds right now.
  const pickTargets = draftTargets
    .filter((row) => String(row.id || '').trim() !== '')
    .map((row) => Object.assign({}, row));
  if (String(add.id || '').trim()
      && !pickTargets.some((row) => String(row.id).trim() === String(add.id).trim())) {
    pickTargets.push({ id: add.id.trim(), title: add.title || '',
                       type: add.type || 'private' });
  }
  const rowKey = (row) => [String(row.id).trim(), String(row.title || ''),
                           String(row.type || 'private')].join('|');
  const dirty = draftTargets.map(rowKey).join(',') !== targets.map(rowKey).join(',')
    || String(add.id || '').trim() !== '';
  const targetsCard = el('div', { class: 'mas-box mas-card mas-tg-targets' }, [
    el('div', { class: 'mas-card-head' }, [
      el('b', { text: `🎯 ${t('agentTgTargets')} (${draftTargets.length})` }),
      dirty ? el('span', { class: 'mas-chip mas-chip-warn',
                           text: '✏️ ' + t('tgUnsaved') }) : null,
    ]),
    el('details', { class: 'mas-help' }, [
      el('summary', { text: '📖 ' + t('tgHelpTargetsTitle') }),
      el('p', { text: t('tgHelpTargets') }),
    ]),
    el('p', { class: 'mas-hint', text: t('tgTargetsHint') }),
    draftTargets.length
      ? el('div', { class: 'mas-target-rows' }, draftTargets.map((target, index) => el('div', {
        class: 'mas-row mas-wrap mas-target-row' }, [
        agentField(t('tgColId'), el('input', {
          class: 'mas-input', dir: 'ltr', value: String(target.id || ''),
          onInput: (event) => { draftTargets[index].id = event.target.value; },
        })),
        agentField(t('tgColTitle'), el('input', {
          class: 'mas-input', value: String(target.title || ''),
          onInput: (event) => { draftTargets[index].title = event.target.value; },
        })),
        agentField(t('tgColType'), agentSelect(
          tgTypes, String(target.type || 'private'),
          (event) => { draftTargets[index].type = event.target.value; })),
        button('🗑', () => {
          draftTargets.splice(index, 1);
          renderAgentSub('telegram');
        }, { class: 'mas-btn mas-danger' }),
      ])))
      : el('p', { class: 'mas-empty', text: t('tgTargetsEmptyHint') }),
    el('b', { class: 'mas-subhead', text: '➕ ' + t('tgTargetAdd') }),
    el('div', { class: 'mas-row mas-wrap mas-target-row mas-target-add' }, [
      agentField(t('tgColId'), el('input', {
        class: 'mas-input', dir: 'ltr', value: String(add.id || ''),
        placeholder: '-100… یا @name',
        onInput: (event) => { add.id = event.target.value; },
      })),
      agentField(t('tgColTitle'), el('input', {
        class: 'mas-input', value: String(add.title || ''),
        placeholder: t('tgColTitle'),
        onInput: (event) => { add.title = event.target.value; },
      })),
      agentField(t('tgColType'), agentSelect(
        tgTypes, String(add.type || 'private'),
        (event) => { add.type = event.target.value; })),
      button('➕ ' + t('tgAddButton'), () => {
        if (pushTarget(add)) state.agentTgAdd = { id: '', title: '', type: 'private' };
      }, { class: 'mas-btn mas-primary' }),
    ]),
    el('div', { class: 'mas-row mas-wrap' }, [
      button('🔎 ' + t('agentTgDiscover'), async () => {
        try {
          state.agentTgFound = (await api('/agent/telegram/targets')).targets || [];
          if (!state.agentTgFound.length) toast(t('tgFoundNone'), 'warn');
          renderAgentSub('telegram');
        } catch (error) { toast(error.message, 'error'); }
      }),
      button(`💾 ${t('tgSaveTargets')} (${draftTargets.length})`, saveTargets,
             { class: 'mas-btn mas-primary' }),
      dirty ? button('↩️ ' + t('tgResetDraft'), () => {
        state.agentTgDraft = null;
        state.agentTgAdd = null;
        renderAgentSub('telegram');
      }, { class: 'mas-btn mas-danger' }) : null,
    ]),
    found.length ? el('div', { class: 'mas-box mas-tg-found' }, [
      el('b', { class: 'mas-subhead',
                text: `🔎 ${t('tgFoundTitle')} (${found.length})` }),
      el('div', { class: 'mas-target-rows' }, found.map((target) => el('div', {
        class: 'mas-row mas-wrap mas-found-row' }, [
        el('span', { class: 'mas-chip', dir: 'ltr', text: String(target.id) }),
        el('b', { text: target.title || String(target.id) }),
        el('span', { class: 'mas-hint', text: tgTypeLabel(target.type) }),
        button('\uff0b ' + t('tgAddButton'), () => pushTarget(target),
               { class: 'mas-btn mas-primary' }),
      ]))),
    ]) : null,
  ]);

  /* -- the test the user asked for: one click, a real message ----------- */
  const targetSelect = el('select', {
    class: 'mas-input',
    onChange: (event) => { test.target = event.target.value; },
  }, pickTargets.length
    ? pickTargets.map((target) => el('option',
      { value: String(target.id) },
      `${target.title || target.id} (${tgTypeLabel(target.type)})`))
    : [el('option', { value: '' }, t('tgTestNoTargets'))]);
  if (pickTargets.length) test.target = String(pickTargets[0].id);
  const diagnoseHost = el('div', { id: 'mas-tg-diagnose', class: 'mas-diagnose' });
  const testCard = el('div', { class: 'mas-box mas-card' }, [
    el('div', { class: 'mas-card-head' }, [el('b', { text: '🩺 ' + t('tgTest') })]),
    el('p', { class: 'mas-hint', text: t('tgHelpTest') }),
    el('div', { class: 'mas-row mas-wrap' }, [
      agentField(t('tgTestTarget'), targetSelect),
      agentField(t('tgTestChannel'), agentSelect([
        { value: '', label: t('tgChannelAuto') },
        { value: 'bot', label: channelLabel('bot') },
        { value: 'account', label: channelLabel('account') },
      ], '', (event) => { test.channel = event.target.value; })),
    ]),
    agentField(t('tgTestText'), el('input', {
      class: 'mas-input', value: '', placeholder: t('tgTestTextHint'),
      onInput: (event) => { test.text = event.target.value; },
    })),
    el('div', { class: 'mas-row mas-wrap' }, [
      button('\u2709 ' + t('tgTestSend'), async () => {
        if (!test.target) { toast(t('tgTestNeedTarget'), 'warn'); return; }
        try {
          const result = await api('/agent/telegram/test', {
            method: 'POST',
            body: {
              target: Number(test.target) || test.target,
              channel: test.channel || '',
              text: test.text || '',
            },
          });
          toast(`${t('tgTestSent')} \u00b7 ${t('tgCountSent')} ${result.sent || 0}`
            + ` \u00b7 ${t('tgCountFailed')} ${result.failed || 0}`,
          result.failed ? 'warn' : 'ok');
          renderAgentSub('telegram');
        } catch (error) { toast(error.message, 'error'); }
      }, { class: 'mas-btn mas-primary' }),
      button('🩺 ' + t('tgDiagnoseBtn'), async () => {
        replace(diagnoseHost, el('p', { class: 'mas-hint', text: t('tgDiagnoseRun') }));
        try {
          const report = await api('/agent/telegram/diagnose', {
            method: 'POST',
            body: { target: test.target ? (Number(test.target) || test.target) : '', channel: test.channel || '' },
          });
          replace(diagnoseHost,
            el('b', { text: report.ok ? '✅ ' + t('tgDiagnoseOk') : '⚠️ ' + t('tgDiagnoseBad') }),
            el('ul', { class: 'mas-diagnose-list' }, (report.steps || []).map((step) => el('li', {
              class: 'mas-diagnose-step' + (step.ok ? ' is-ok' : ' is-bad'),
            }, [
              el('span', { class: 'mas-diagnose-mark', text: step.ok ? '✅' : (step.skipped ? '⏭' : '❌') }),
              el('b', { text: step.fa || step.step }),
              el('span', { class: 'mas-hint', text: step.detail || '' }),
            ]))));
        } catch (error) {
          replace(diagnoseHost, el('p', { class: 'mas-warn', text: error.message }));
        }
      }),
    ]),
    diagnoseHost,
  ]);

  /* -- the mailbox: every attempt, Tehran time, like the sample --------- */
  const messages = mailbox.messages || [];
  const mailCard = el('div', { class: 'mas-box mas-card' }, [
    el('div', { class: 'mas-card-head' }, [
      el('b', { text: '📋 ' + t('tgListTitle') }),
      button('🔄', async () => { renderAgentSub('telegram'); }, { class: 'mas-btn' }),
    ]),
    el('p', { class: 'mas-hint', text: t('tgTehranHint') }),
    messages.length ? agentTable(
      [t('tgColStatus'), t('tgColWhen'), t('tgColTarget'), t('tgColText'), t('tgColSource')],
      messages.map((row) => [
        tgStatusText(row.status),
        tehranWhen(row.created_at),
        String(row.target || ''),
        String(row.body || '').slice(0, 80),
        `${tgPurposeText(row.purpose)}${row.channel ? ' \u00b7 ' + channelLabel(row.channel) : ''}`,
      ])) : el('p', { class: 'mas-hint', text: t('tgListEmpty') }),
  ]);

  /* -- programs: scheduled messages ------------------------------------- */
  let programs = [];
  try {
    programs = ((await api('/agent/jobs')).jobs || [])
      .filter((job) => (job.payload || {}).action === 'telegram');
  } catch (_) { programs = []; }
  const program = { title: '', text: '', repeat: 'daily', time: '09:00', until: '', max: '' };
  const programSummary = el('span', { class: 'mas-hint', text: t('tgProgHint') });
  const refreshSummary = () => {
    if (!String(program.text || '').trim()) {
      replace(programSummary, el('span', { class: 'mas-hint', text: t('tgProgHint') }));
      return;
    }
    replace(programSummary, el('span', {
      class: 'mas-hint',
      text: `${t('tgProgRepeat')}: ${t('tgProg' + program.repeat.charAt(0).toUpperCase() + program.repeat.slice(1))} \u00b7 ${program.time}`
        + (program.until ? ` \u00b7 ${t('tgProgUntil')}: ${program.until}` : '')
        + (program.max ? ` \u00b7 ${t('tgProgMax')}: ${program.max}` : ''),
    }));
  };
  const programCard = el('div', { class: 'mas-box mas-card' }, [
    el('div', { class: 'mas-card-head' }, [
      el('b', { text: '🔁 ' + t('tgProgramsTitle') }),
      el('span', { class: 'mas-chip', text: `${programs.length} ${t('tgProgramsCount')} \u00b7 ${programs.filter((job) => job.enabled).length} ${t('tgProgActive')}` }),
    ]),
    programs.length ? el('ul', { class: 'mas-prog-list' }, programs.map((job) => el('li', { class: 'mas-row mas-wrap' }, [
      el('b', { text: job.name || job.id }),
      el('span', { class: 'mas-hint', text: String((job.payload || {}).text || '').slice(0, 60) }),
      el('span', { class: 'mas-chip', text: job.enabled ? '🟢 ' + t('tgProgActive') : '⚪ ' + t('tgProgPaused') }),
      button('▶', async () => {
        try {
          await api('/agent/job-run', { method: 'POST', body: { id: job.id } });
          toast(t('agentSaved'), 'ok');
        } catch (error) { toast(error.message, 'error'); }
      }),
      button(job.enabled ? '⏸' : '▶️', async () => {
        try {
          await api(`/agent/jobs/${job.id}`, { method: 'PUT', body: { enabled: !job.enabled } });
          renderAgentSub('telegram');
        } catch (error) { toast(error.message, 'error'); }
      }),
      button('🗑', async () => {
        try {
          await api(`/agent/jobs/${job.id}`, { method: 'DELETE' });
          renderAgentSub('telegram');
        } catch (error) { toast(error.message, 'error'); }
      }),
    ]))) : el('p', { class: 'mas-hint', text: t('tgProgEmpty') }),
    agentField('🏷 ' + t('tgProgTitle'), el('input', {
      class: 'mas-input', placeholder: t('tgProgTitleHint'),
      onInput: (event) => { program.title = event.target.value; },
    })),
    agentField('✍️ ' + t('tgTestText'), el('textarea', {
      class: 'mas-input mas-textarea', rows: 3, placeholder: t('tgProgTextHint'),
      onInput: (event) => { program.text = event.target.value; refreshSummary(); },
    })),
    el('div', { class: 'mas-row mas-wrap' }, [
      agentField('🔁 ' + t('tgProgRepeat'), agentSelect([
        { value: 'daily', label: t('tgProgDaily') },
        { value: 'hourly', label: t('tgProgHourly') },
        { value: 'weekly', label: t('tgProgWeekly') },
      ], program.repeat, (event) => { program.repeat = event.target.value; refreshSummary(); })),
      agentField('🕒 ' + t('tgProgTime'), el('input', {
        class: 'mas-input', type: 'time', value: program.time,
        onInput: (event) => { program.time = event.target.value; refreshSummary(); },
      })),
      agentField('⏹ ' + t('tgProgUntil'), el('input', {
        class: 'mas-input', type: 'date', value: program.until,
        onInput: (event) => { program.until = event.target.value; refreshSummary(); },
      })),
      agentField('🔢 ' + t('tgProgMax'), el('input', {
        class: 'mas-input', type: 'number', min: '0', placeholder: t('tgProgMaxHint'),
        onInput: (event) => { program.max = event.target.value; refreshSummary(); },
      })),
    ]),
    programSummary,
    el('div', { class: 'mas-row mas-wrap' }, [
      button('🔁 ' + t('tgProgMake'), async () => {
        if (!String(program.text || '').trim()) { toast(t('tgSendNeedText'), 'warn'); return; }
        const [hour, minute] = String(program.time || '09:00').split(':');
        const cron = {
          daily: `${Number(minute) || 0} ${Number(hour) || 0} * * *`,
          hourly: `${Number(minute) || 0} * * * *`,
          weekly: `${Number(minute) || 0} ${Number(hour) || 0} * * 6`,
        }[program.repeat] || '0 9 * * *';
        const payload = { action: 'telegram', text: program.text };
        if (program.until) payload.until = Math.floor(new Date(program.until + 'T23:59:59').getTime() / 1000);
        if (program.max) payload.maxRuns = Number(program.max) || 0;
        try {
          await api('/agent/jobs', {
            method: 'POST',
            body: {
              name: program.title || t('tgProgDefaultName'),
              kind: 'cron', schedule: cron, payload,
            },
          });
          toast(t('agentSaved'), 'ok');
          renderAgentSub('telegram');
        } catch (error) { toast(error.message, 'error'); }
      }, { class: 'mas-btn mas-primary' }),
    ]),
  ]);

  /* -- free-form send ---------------------------------------------------- */
  const sendCard = el('div', { class: 'mas-box mas-card' }, [
    el('div', { class: 'mas-card-head' }, [el('b', { text: '📤 ' + t('tgSendTitle') })]),
    el('p', { class: 'mas-hint', text: t('tgSendHint') }),
    agentField(t('tgTestText'), el('input', {
      class: 'mas-input', value: '', placeholder: t('tgSendTextHint'),
      onInput: (event) => { send.text = event.target.value; },
    })),
    el('div', { class: 'mas-row mas-wrap' }, [
      agentField(t('tgSendTarget'), agentSelect([
        { value: '', label: t('tgSendEveryone') },
      ].concat(pickTargets.map((target) => ({
        value: String(target.id),
        label: `${target.title || target.id} (${tgTypeLabel(target.type)})`,
      }))), '', (event) => { send.target = event.target.value; })),
      agentField(t('tgTestChannel'), agentSelect([
        { value: '', label: t('tgChannelAuto') },
        { value: 'bot', label: channelLabel('bot') },
        { value: 'account', label: channelLabel('account') },
      ], '', (event) => { send.channel = event.target.value; })),
      agentField(t('tgSendPurpose'), agentSelect(purposes.map((purpose) => ({
        value: purpose,
        label: t('tgPurpose' + purpose.charAt(0).toUpperCase() + purpose.slice(1)),
      })), 'manual', (event) => { send.purpose = event.target.value; })),
    ]),
    button('📤 ' + t('tgSendButton'), async () => {
      if (!String(send.text || '').trim()) { toast(t('tgSendNeedText'), 'warn'); return; }
      const body = { text: send.text, purpose: send.purpose || 'manual' };
      if (send.target) body.target = Number(send.target) || send.target;
      if (send.channel) body.channel = send.channel;
      try {
        const result = await api('/agent/telegram/send', { method: 'POST', body });
        toast(`${t('tgTestSent')} \u00b7 ${t('tgCountSent')} ${result.sent || 0}`
          + ` \u00b7 ${t('tgCountFailed')} ${result.failed || 0}`,
        result.failed ? 'warn' : 'ok');
        renderAgentSub('telegram');
      } catch (error) { toast(error.message, 'error'); }
    }, { class: 'mas-btn mas-primary' }),
  ]);

  /* -- nested sub-tabs, styled exactly like the top tab bar ------------- */
  const TG_SUBS = ['status', 'settings', 'targets', 'mailbox', 'programs', 'write'];
  const activeSub = TG_SUBS.includes(state.tgSub) ? state.tgSub : 'status';
  const subBar = el('div', { class: 'mas-tabs mas-subtabs' }, TG_SUBS.map((name) => {
    const badge = name === 'mailbox' && counts.all
      ? el('span', { class: 'mas-tab-count', text: String(counts.all) }) : null;
    return el('button', {
      class: 'mas-tab' + (name === activeSub ? ' is-active' : ''),
      dataset: { tgsub: name }, type: 'button',
      onClick: () => { state.tgSub = name; saveUiPref('tgSub', name); renderAgentSub('telegram'); },
    }, [t('tgSub' + name.charAt(0).toUpperCase() + name.slice(1)), badge]);
  }));
  const cards = {
    status: testCard, settings: settingsCard, targets: targetsCard,
    mailbox: mailCard, programs: programCard, write: sendCard,
  };
  const subHost = el('div', { class: 'mas-subhost' }, cards[activeSub] || testCard);

  replace(host, statusCard, subBar, subHost);
}

/* -- data ------------------------------------------------------------ */

async function renderAgentData(host) {
  replace(host, el('p', { class: 'mas-empty', text: '…' }));
  let notes = []; let audit = [];
  try {
    notes = (await api('/agent/notes')).notes || [];
    audit = (await api('/agent/audit?limit=60')).audit || [];
  } catch (error) { return agentError(host, error); }
  const sqlBox = el('textarea', {
    class: 'mas-input mas-area mas-code', rows: '3', spellcheck: 'false',
    value: state.agentSql || 'SELECT id, name, kind, schedule, enabled, last_status FROM jobs',
    onInput: (event) => { state.agentSql = event.target.value; },
  });

  const dbWinSql = el('textarea', {
    id: 'mas-dbwin-sql', class: 'mas-input mas-mono', rows: 3,
    placeholder: t('dbWinPh'),
  });
  replace(host,
    el('div', { class: 'mas-box mas-card' }, [
      el('div', { class: 'mas-card-head' }, [el('b', { text: '🗄 ' + t('dbWinTitle') })]),
      el('p', { class: 'mas-hint', text: t('dbWinHint') }),
      el('button', { class: 'mas-btn', type: 'button', text: '🗺 ' + t('dbWinSchema'), onClick: async () => {
        try {
          const map = await api('/agent/db/schema');
          replace(host.querySelector('.mas-dbschema-out'),
            agentTable([t('dbWinTables'), t('dbWinRows'), t('dbWinCols'), t('dbWinSensitive')],
              (map.tables || []).map((row) => [
                row.name, String(row.rows), String((row.columns || []).length),
                (row.sensitiveColumns || []).join(', ') || '—',
              ])));
        } catch (error) { toast(error.message, 'error'); }
      } }),
      el('div', { class: 'mas-dbschema-out' }),
      dbWinSql,
      el('button', { class: 'mas-btn mas-primary', type: 'button', text: '▶ ' + t('dbWinRun'), onClick: async () => {
        try {
          const result = await api('/agent/db/query', { method: 'POST', body: { sql: dbWinSql.value } });
          const fmt = (value) => (value && typeof value === 'object' && value.masked
            ? `🔒 ${t('dbWinMaskedNote')}`
            : String(value === null || value === undefined ? '' : value).slice(0, 140));
          replace(host.querySelector('.mas-dbquery-out'),
            result.rows.length
              ? agentTable(result.columns, result.rows.map((row) => result.columns.map((column) => fmt(row[column]))))
              : el('p', { class: 'mas-hint', text: t('agentEmpty') }));
          if (result.truncated) toast(t('dbWinTruncated'), 'warn');
        } catch (error) { toast(error.message, 'error'); }
      } }),
      el('div', { class: 'mas-dbquery-out' }),
    ]),
    el('div', { class: 'mas-box' }, [
      el('b', { text: t('agentQuery') }),
      sqlBox,
      el('p', { class: 'mas-hint', text: t('agentQueryHint') }),
      button('▶ ' + t('agentQueryRun'), async () => {
        try {
          const result = await api('/agent/query', { method: 'POST', body: { sql: sqlBox.value } });
          replace(host.querySelector('.mas-query-result') || host,
            result.rows.length ? agentTable(result.columns, result.rows.map((row) => result.columns.map((column) => String(row[column] === null ? '' : row[column]).slice(0, 120))))
              : el('p', { class: 'mas-hint', text: t('agentEmpty') }));
        } catch (error) { toast(error.message, 'error'); }
      }, { class: 'mas-btn mas-primary' }),
      el('div', { class: 'mas-query-result' }, state.agentQueryResult || null),
    ]),
    el('div', { class: 'mas-box' }, [
      el('b', { text: `${t('agentNotes')} (${notes.length})` }),
      notes.length ? agentTable(['time', 'kind', 'body', ''], notes.slice(0, 30).map((note) => [
        agentTime(note.created_at), note.kind, truncate(note.body, 90),
        button('🗑', async () => {
          try { await api(`/agent/notes/${encodeURIComponent(note.id)}`, { method: 'DELETE' }); renderAgentSub('data'); } catch (error) { toast(error.message, 'error'); }
        }),
      ])) : el('p', { class: 'mas-hint', text: t('agentEmpty') }),
    ]),
    el('div', { class: 'mas-box' }, [
      el('b', { text: t('agentAudit') }),
      audit.length ? agentTable(['time', 'actor', 'action', 'detail'], audit.map((row) => [
        agentTime(row.at), row.actor, row.action, truncate(row.detail, 70),
      ])) : el('p', { class: 'mas-hint', text: t('agentEmpty') }),
    ]));
}

/* -- keys ------------------------------------------------------------ */

async function renderAgentKeys(host) {
  const overview = state.agent || (await api('/agent').catch(() => null));
  if (!overview) return agentError(host, { message: t('agentNotAttached') });
  const settings = overview.settings || {};
  const llm = overview.llm || {};
  const draft = {};
  const secret = (key, label, placeholder) => agentField(label, el('input', {
    class: 'mas-input', type: 'password',
    placeholder: (settings[key] && settings[key].set) ? '•••••• (stored)' : (placeholder || ''),
    onInput: (event) => { draft[key] = event.target.value; },
  }));

  replace(host,
    el('div', { class: 'mas-box' }, [
      el('b', { text: t('agentSubKeys') }),
      el('div', { class: 'mas-row mas-wrap' }, [
        agentField(t('agentProvider'), agentSelect(['ai-browser', 'http-api'], llm.provider,
          (event) => { draft['ai.provider'] = event.target.value; })),
        agentField(t('agentChatProvider'), agentSelect(['deepseek', 'generic'], llm.chatProvider,
          (event) => { draft['ai.chatProvider'] = event.target.value; })),
      ]),
      el('p', { class: 'mas-hint', text: t('agentAiHint') }),
      el('div', { class: 'mas-row mas-wrap' }, [
        agentField(t('agentBaseUrl'), el('input', { class: 'mas-input', value: settings['ai.baseUrl'] || '', placeholder: 'https://api.deepseek.com/v1', onInput: (event) => { draft['ai.baseUrl'] = event.target.value; } })),
        agentField(t('agentModel'), el('input', { class: 'mas-input', value: settings['ai.model'] || '', placeholder: 'deepseek-chat', onInput: (event) => { draft['ai.model'] = event.target.value; } })),
      ]),
      secret('ai.apiKey', t('agentApiKey')),
      el('div', { class: 'mas-row mas-wrap' }, [
        button('💾 ' + t('agentSave'), async () => {
          try { await api('/agent/settings', { method: 'POST', body: draft }); toast(t('agentSaved'), 'ok'); renderAgent(); } catch (error) { toast(error.message, 'error'); }
        }, { class: 'mas-btn mas-primary' }),
        button('🩺 ' + t('agentAiStatus'), async () => {
          try { const status = await api('/agent/ai/status'); toast(JSON.stringify(status).slice(0, 160), status.available ? 'ok' : 'warn'); } catch (error) { toast(error.message, 'error'); }
        }),
        button('🌐 ' + t('agentAiOpen'), async () => {
          try { await api('/agent/ai/open', { method: 'POST', body: {} }); toast('ok', 'ok'); } catch (error) { toast(error.message, 'error'); }
        }),
        button('📸 ' + t('agentAiShot'), async () => {
          const url = await imageBlobUrl('/agent/ai/shot');
          if (!url) { toast('no image', 'error'); return; }
          const viewer = host.querySelector('.mas-ai-shot');
          replace(viewer || host.appendChild(el('div', { class: 'mas-ai-shot' })),
            el('img', { src: url, alt: 'agent display', class: 'mas-shot-full' }));
        }),
      ]),
      llm.browser ? el('p', { class: 'mas-hint', text: `agent browser: ${llm.browser.available ? 'reachable' : 'not running'} (port ${llm.browser.port}, display ${llm.browser.display})` }) : null,
    ]),
    el('div', { class: 'mas-box' }, [
      el('b', { text: t('agentSubData') }),
      el('p', { class: 'mas-hint', text: `db: ${JSON.stringify(overview.counts || {})}` }),
    ]));
}

/** Send the same context packet to the model from inside the app.
 *  The old workflow was: copy the prompt, paste it into a chat website, paste
 *  the answer back. This does all three without leaving the sidebar, and keeps
 *  the copy/paste buttons next to it for when the agent has no model attached. */
async function askAgentDirectly() {
  const page = state.activePage || (state.pages || [])[0] || null;
  const body = { flow: state.flow, publicBase: window.location.origin };
  if (page) body.pageId = page.id;
  if (state.userRequest && state.userRequest.trim()) body.request = state.userRequest;
  if (state.aiReply && state.aiReply.trim()) body.previousReply = state.aiReply;
  if (!body.request) {
    toast(t('aiRequest'), 'warn');
    return;
  }
  toast('…', 'info');
  try {
    const reply = await api('/agent/chat', { method: 'POST', body });
    state.aiReply = reply.text || '';
    try { localStorage.setItem(LS.reply, state.aiReply); } catch (_) { /* quota */ }
    state.aiNotes = aiNotesFrom(state.aiReply);
    let applied = false;
    if (reply.flow && reply.flowValid && (reply.flow.steps || []).length) {
      state.agentFlowProposal = reply.flow;
      // Round 26: the agent's output goes straight into the stages so the
      // steps can be checked one by one, without an extra button.
      applied = applyFlowToStages(await convertFlowServer(reply.flow));
    }
    renderAgentAssistant();
    if (applied) {
      toast(t('chatFlowApplied'), 'ok');
    } else if (state.agentFlowProposal) {
      toast(`${reply.flow.steps.length} steps`, 'ok');
    } else if (reply.flowErrors && reply.flowErrors.length) {
      toast(reply.flowErrors[0], 'warn');
    } else {
      toast(reply.provider || 'ok', 'ok');
    }
  } catch (error) {
    toast(error.message, error.status === 404 ? 'warn' : 'error');
  }
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast('copied', 'ok');
  } catch (_) {
    const area = el('textarea', { class: 'mas-input mas-area', rows: '10', value: text });
    const pane = document.getElementById('mas-agent-sub')
      || document.getElementById('mas-panel');
    pane.appendChild(area);
    area.select();
    toast('Ctrl+C', 'warn');
  }
}

/* ------------------------------------------------------------------ *
 * Log tab
 * ------------------------------------------------------------------ */

/* ------------------------------------------------------------------ *
 * Chat tab
 *
 * A normal chat page: you write, the model answers. The answer is not coming
 * from a purchased API key - the agent drives its own browser (chat.deepseek.com
 * on display :2 by default), which means a turn can take a minute and the pane
 * has to poll instead of waiting on one request. The provider dropdown lists
 * whatever chat profiles exist, so another model is a setting, not a rewrite.
 * ------------------------------------------------------------------ */

const CHAT_POLL_MS = 2000;
let chatTimer = null;
// The brain tab's status poller. Declared up here next to chatTimer because
// selectTab() runs during boot, long before the brain section below executes.
let brainTimer = null;
/** Last known connection state, so a poll does not re-check the tabs. */
let lastConnected = null;

function stopChatPolling() {
  if (chatTimer) { clearInterval(chatTimer); chatTimer = null; }
}

function startChatPolling() {
  stopChatPolling();
  chatTimer = setInterval(() => {
    const pane = document.getElementById('mas-tab-chat');
    // A hidden tab should not keep a request loop alive.
    if (!pane || pane.hidden) { stopChatPolling(); return; }
    refreshChatList();
  }, CHAT_POLL_MS);
}

function chatBubble(message) {
  const mine = message.role === 'user';
  const meta = (message.meta || {});
  const bits = [];
  if (message.provider) bits.push(message.provider);
  bits.push(formatTime(message.created_at));
  if (meta.elapsed) bits.push(formatElapsed(meta.elapsed));

  const body = el('div', { class: 'mas-bubble-body' });
  if (message.status === 'thinking') {
    body.appendChild(el('span', { class: 'mas-thinking', text: t('chatThinking') }));
  } else if (message.status === 'failed') {
    body.appendChild(el('span', { class: 'mas-error', text: message.error || t('chatFailed') }));
  } else {
    body.appendChild(el('pre', { class: 'mas-bubble-text', text: message.body || '' }));
  }

  const actions = [];
  if (!mine && message.body) {
    actions.push(button('📋 ' + t('copy'), () => copyText(message.body)));
    // Round 27, item 3: one click archives this reply as a letter FROM the
    // agent, kept in the database's letters sub-tab.
    actions.push(button('📨 ' + t('lettersSaveReply'), async () => {
      try {
        await api('/agent/letters', {
          method: 'POST',
          body: { direction: 'to_user', subject: '', body: message.body },
        });
        toast(t('lettersSavedReply'), 'ok');
      } catch (error) { toast(error.message, 'error'); }
    }));
  }
  if (!mine && meta.flow && (meta.flow.steps || []).length) {
    actions.push(button(`⤓ ${t('chatApplyFlow')} (${(meta.flow.steps || []).length})`,
      () => applyChatFlow(message), { class: 'mas-btn mas-primary' }));
  }
  return el('div', { class: `mas-bubble ${mine ? 'is-mine' : 'is-ai'}` }, [
    body,
    el('div', { class: 'mas-bubble-meta', text: bits.join(' · ') }),
    actions.length ? el('div', { class: 'mas-row mas-wrap' }, actions) : null,
  ]);
}

async function refreshChatList() {
  const list = document.getElementById('mas-chat-list');
  if (!list) return;
  let data;
  try {
    data = await api('/agent/chat/messages');
  } catch (error) {
    replace(list, el('p', { class: 'mas-hint', text: error.message }));
    return;
  }
  state.chatProviders = data.providers || [];
  const select = document.getElementById('mas-chat-provider');
  if (select && state.chatProviders.length) {
    if (!state.chatProviders.includes(state.chatProvider)) {
      state.chatProvider = state.chatProviders[0];
    }
    replace(select, state.chatProviders.map((name) => el('option',
      { value: name, selected: name === state.chatProvider }, name)));
  }
  const messages = data.messages || [];
  if (!messages.length) {
    replace(list, el('p', { class: 'mas-empty', text: t('chatNoHistory') }));
  } else {
    replace(list, messages.map(chatBubble));
    // Round 26: the newest agent answer that carries a flow lands in the
    // stages by itself - once per message - so the operator verifies the
    // steps one by one without hunting for a button.
    const carriers = messages.filter((message) => message.role !== 'user'
      && (((message.meta || {}).flow || {}).steps || []).length);
    const latest = carriers.length ? carriers[carriers.length - 1] : null;
    let appliedId = '';
    try { appliedId = localStorage.getItem(LS.appliedChat) || ''; } catch (_) { /* private */ }
    if (latest && latest.id && String(latest.id) !== appliedId) {
      try { localStorage.setItem(LS.appliedChat, String(latest.id)); } catch (_) { /* quota */ }
      applyChatFlow(latest, true);
    }
  }
  if ((data.pending || []).length) startChatPolling();
  else stopChatPolling();
  list.scrollTop = list.scrollHeight;
}

async function sendChat() {
  const box = document.getElementById('mas-chat-input');
  const text = String((box && box.value) || state.chatDraft || '').trim();
  if (!text) { toast(t('chatEmpty'), 'warn'); return; }
  state.chatDraft = '';
  if (box) box.value = '';
  try {
    await api('/agent/chat/send', {
      method: 'POST',
      body: { text, provider: state.chatProvider || '', withContext: !!state.chatContext },
    });
    toast(t('chatSent'), 'ok');
    startChatPolling();
  } catch (error) {
    // Give the message back: losing what someone typed is the worst failure
    // a chat pane can have.
    state.chatDraft = text;
    if (box) box.value = text;
    toast(error.message, 'error');
  }
  refreshChatList();
}

async function clearChat() {
  try {
    const data = await api('/agent/chat/clear', { method: 'POST', body: {} });
    toast(`${t('chatCleared')}: ${data.cleared || 0}`, 'ok');
  } catch (error) { toast(error.message, 'error'); }
  refreshChatList();
}

async function applyChatFlow(message, auto = false) {
  try {
    const data = await api('/agent/chat/apply-flow', { method: 'POST', body: { id: message.id } });
    const saved = await convertFlowServer(data.flow || {});
    if (!applyFlowToStages(saved)) {
      toast(t('chatNoFlow'), 'error');
    } else if (auto) {
      toast(t('chatFlowApplied'), 'ok');
    } else {
      toast(`${state.flow.steps.length} ${t('steps')}`, 'ok');
    }
  } catch (error) { toast(error.message, 'error'); }
}

function renderChat() {
  const pane = document.getElementById('mas-tab-chat');
  if (!pane) return;
  const provider = el('select', {
    id: 'mas-chat-provider', class: 'mas-input mas-chat-provider',
    onChange: (event) => {
      state.chatProvider = event.target.value;
      try { localStorage.setItem(LS.provider, state.chatProvider); } catch (_) { /* quota */ }
      saveUiPref('chatProvider', state.chatProvider);
    },
  });
  const input = el('textarea', {
    id: 'mas-chat-input', class: 'mas-input mas-area mas-chat-input', rows: '3',
    value: state.chatDraft, placeholder: t('chatPlaceholder'),
    onInput: (event) => { state.chatDraft = event.target.value; },
    onKeydown: (event) => {
      if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        sendChat();
      }
    },
  });

  replace(pane,
    el('p', { class: 'mas-hint', text: t('chatHint') }),
    el('div', { class: 'mas-row mas-wrap' }, [
      el('label', { class: 'mas-field' }, [
        el('span', { text: t('chatProvider') }), provider,
      ]),
      el('label', { class: 'mas-check', title: t('chatContextHint') }, [
        el('input', {
          type: 'checkbox', checked: state.chatContext,
          onChange: (event) => {
            state.chatContext = event.target.checked;
            try { localStorage.setItem(LS.context, state.chatContext ? '1' : '0'); } catch (_) { /* quota */ }
            saveUiPref('chatContext', state.chatContext ? '1' : '0');
          },
        }),
        el('span', { text: t('chatContext') }),
      ]),
      button('🧹 ' + t('chatClear'), clearChat),
      button('↻ ' + t('refresh'), refreshChatList),
    ]),
    el('div', { id: 'mas-chat-list', class: 'mas-chat-list' },
      [el('p', { class: 'mas-empty', text: '…' })]),
    el('div', { class: 'mas-chat-compose' }, [
      input,
      button('➤ ' + t('chatSend'), sendChat,
        { class: 'mas-btn mas-primary', id: 'mas-chat-send' }),
    ]),
  );
  // The prompt workspace itself lives in the agent's first sub-tab now; the
  // chat tab only points at it, so there is exactly one place to build a
  // prompt and one place to talk.
  refreshChatList();
  startChatPolling();
}

/* ------------------------------------------------------------------ *
 * Library tab: named operations
 *
 * An operation is a reusable bundle of steps with a name - "connect to the
 * lmarena agent", "solve the captcha", "ask DeepSeek something". Opening one
 * puts its steps into the stages tab, where the full editor and the run button
 * already live; running one hands it straight to the engine.
 * ------------------------------------------------------------------ */

function operationSummary(op) {
  return (op.steps || []).map((step, index) =>
    `${index + 1}. ${step.label || labelFor(step)}`).join('\n');
}

async function openOperationInStages(op) {
  const { flow, errors } = normaliseImportedFlow(
    await convertFlowServer({ name: op.name, steps: op.steps || [] }),
    state.flow.viewport);
  if (!flow || !(flow.steps || []).length) {
    toast((errors || []).join(' | ') || t('opEmpty'), 'error');
    return;
  }
  state.flow = flow;
  persistFlow();
  renderFlow();
  selectTab('flow');
  toast(`${t('opOpened')}: ${flow.steps.length} ${t('steps')}`,
    errors && errors.length ? 'warn' : 'ok');
}

async function runOperation(op) {
  try {
    const data = await api('/agent/operation-run', { method: 'POST', body: { id: op.id } });
    toast(`${t('opStarted')}: ${(data.flow && data.flow.steps || op.steps || []).length}`, 'ok');
    refreshStatus();
    renderOperations();
  } catch (error) { toast(error.message, 'error'); }
}

async function resetOperation(op) {
  try {
    await api('/agent/operation-reset', { method: 'POST', body: { id: op.id } });
    toast(t('opResetDone'), 'ok');
    renderOperations();
  } catch (error) { toast(error.message, 'error'); }
}

async function deleteOperation(op) {
  try {
    await api(`/agent/operations/${encodeURIComponent(op.id)}`, { method: 'DELETE' });
    state.deleteArmed = '';
    toast(`${t('opDeleted')}: ${op.name}`, 'ok');
    renderOperations();
  } catch (error) { toast(error.message, 'error'); }
}

function operationCard(op) {
  const steps = op.steps || [];
  const tags = (op.tags || []).filter(Boolean);
  const last = op.last_run_at
    ? `${t('opLastRun')}: ${agentTime(op.last_run_at)} · ${op.last_status || '-'} · ${op.run_count || 0}×`
    : t('opNeverRun');
  return el('div', { class: 'mas-op' + (op.builtin ? ' is-builtin' : '') }, [
    el('div', { class: 'mas-op-head' }, [
      el('b', { text: op.name }),
      op.builtin ? el('span', { class: 'mas-badge', text: t('opBuiltin') }) : null,
      el('span', {
        class: 'mas-badge' + (op.scope === 'brain' ? ' mas-badge-brain' : ''),
        text: op.scope === 'brain' ? '🧠 ' + t('scopeBrain') : '🌐 ' + t('scopeGeneral'),
        title: t('opScopeHint'),
      }),
      el('span', { class: 'mas-hint', text: `${steps.length} ${t('steps')}` }),
    ]),
    op.description ? el('p', { class: 'mas-op-desc', text: op.description }) : null,
    tags.length ? el('div', { class: 'mas-hint', text: tags.join(' · ') }) : null,
    el('div', { class: 'mas-hint', text: last }),
    el('details', { class: 'mas-op-steps' }, [
      el('summary', { text: t('opSteps') }),
      el('pre', { class: 'mas-pre', text: operationSummary(op) || '-' }),
    ]),
    el('div', { class: 'mas-row mas-wrap' }, [
      button('▶ ' + t('opRun'), () => runOperation(op), { class: 'mas-btn mas-primary' }),
      button(op.permission === 'auto' ? '🔓 ' + t('opPermAuto') : '🔒 ' + t('opPermAsk'),
        () => setOperationPermission(op, op.permission === 'auto' ? 'ask' : 'auto'),
        { class: 'mas-btn', title: t('opPermHint') }),
      button(op.scope === 'brain' ? '🧠 ' + t('scopeBrain') : '🌐 ' + t('scopeGeneral'),
        () => setOperationScope(op, op.scope === 'brain' ? 'general' : 'brain'),
        { class: 'mas-btn', title: t('opScopeHint') }),
      button('📂 ' + t('opOpen'), () => openOperationInStages(op)),
      button('✎ ' + t('opEdit'), async () => {
        let editSteps = steps;
        if (steps.some((step) => step.type !== 'python')) {
          // Round 26: an operation from before the one language is
          // translated by the server before the editor shows it.
          const converted = await convertFlowServer({ name: op.name, steps });
          editSteps = (converted && converted.steps) || steps;
        }
        state.editingOperation = {
          id: op.id, name: op.name, description: op.description || '',
          tags: tags.join('، '), builtin: !!op.builtin,
          scope: op.scope === 'brain' ? 'brain' : 'general',
          steps: editSteps.map((step) => ({
            type: 'python',
            code: String(step.code || ''),
            label: String(step.label || ''),
          })),
        };
        renderOperations();
      }),
      op.builtin ? button('↺ ' + t('opReset'), () => resetOperation(op)) : null,
      op.builtin ? null : (state.deleteArmed === op.id
        ? button('⚠ ' + t('opDeleteSure'), () => deleteOperation(op),
          { class: 'mas-btn mas-danger' })
        : button('🗑 ' + t('opDelete'), () => {
          state.deleteArmed = op.id;
          renderOperations();
        })),
    ]),
  ]);
}

function operationEditor(draft) {
  // Round 26: every step of an operation is a python program with a Persian
  // description; no type picker and no JSON fields remain.
  const stepRows = (draft.steps || []).map((row, index) => el('div', { class: 'mas-op-steprow mas-op-steprow-code' }, [
    el('span', { class: 'mas-step-no', text: `${t('stepNo')} ${index + 1}` }),
    el('input', {
      class: 'mas-input', value: row.label || '', placeholder: t('opStepLabel'),
      onInput: (event) => { row.label = event.target.value; },
    }),
    el('textarea', {
      class: 'mas-input mas-area', rows: '4', value: row.code || '',
      spellcheck: 'false', placeholder: 'check(click(512, 300))',
      onInput: (event) => { row.code = event.target.value; },
    }),
    button('🗑', () => { draft.steps.splice(index, 1); renderOperations(); }),
  ]));

  return el('div', { class: 'mas-box mas-op-editor' }, [
    el('b', { text: draft.id ? t('opEdit') : t('opNew') }),
    agentField(t('opName'), el('input', {
      class: 'mas-input', value: draft.name || '', placeholder: t('opNameHint'),
      onInput: (event) => { draft.name = event.target.value; },
    })),
    agentField(t('opDescription'), el('input', {
      class: 'mas-input', value: draft.description || '',
      onInput: (event) => { draft.description = event.target.value; },
    })),
    agentField(t('opTags'), el('input', {
      class: 'mas-input', value: draft.tags || '', placeholder: t('opTagsHint'),
      onInput: (event) => { draft.tags = event.target.value; },
    })),
    agentField(t('opScope'), el('select', {
      class: 'mas-input',
      onChange: (event) => { draft.scope = event.target.value; },
    }, [
      el('option', {
        value: 'general', selected: (draft.scope || 'general') !== 'brain',
      }, '🌐 ' + t('scopeGeneral')),
      el('option', {
        value: 'brain', selected: draft.scope === 'brain',
      }, '🧠 ' + t('scopeBrain')),
    ])),
    el('p', { class: 'mas-hint', text: t('opScopeHint') }),
    el('div', { class: 'mas-row mas-wrap' }, [
      el('b', { text: `${t('opSteps')} (${stepRows.length})` }),
      button('＋ ' + t('opAddStep'), () => {
        draft.steps = draft.steps || [];
        draft.steps.push({ type: 'python', code: '', label: '' });
        renderOperations();
      }),
    ]),
    el('div', {}, stepRows),
    el('p', { class: 'mas-hint', text: t('opFieldsHint') }),
    el('div', { class: 'mas-row mas-wrap' }, [
      button('💾 ' + t('opSave'), () => saveOperation(draft), { class: 'mas-btn mas-primary' }),
      button(t('opCancel'), () => { state.editingOperation = null; renderOperations(); }),
    ]),
  ]);
}

async function saveOperation(draft) {
  const name = String(draft.name || '').trim();
  if (!name) { toast(t('opNeedName'), 'warn'); return; }
  const steps = [];
  for (const row of (draft.steps || [])) {
    const code = String(row.code || '').trim();
    if (!code) { toast(t('opEmptyCode'), 'error'); return; }
    const step = { type: 'python', code };
    const label = String(row.label || '').trim();
    if (label) step.label = label;
    steps.push(step);
  }
  if (!steps.length) { toast(t('opEmpty'), 'warn'); return; }
  const body = {
    name,
    description: String(draft.description || ''),
    tags: String(draft.tags || '').split(/[،,]/).map((item) => item.trim()).filter(Boolean),
    steps,
    scope: draft.scope === 'brain' ? 'brain' : 'general',
  };
  try {
    if (draft.id) {
      await api(`/agent/operations/${encodeURIComponent(draft.id)}`,
        { method: 'PUT', body });
    } else {
      await api('/agent/operations', { method: 'POST', body });
    }
    state.editingOperation = null;
    toast(t('opSaved'), 'ok');
    renderOperations();
  } catch (error) { toast(error.message, 'error'); }
}

async function setOperationScope(op, scope) {
  // The same honest PUT the permission toggle uses; the server converts any
  // old steps it still carries, so a scope flip also finishes the migration.
  try {
    await api(`/agent/operations/${encodeURIComponent(op.id)}`, {
      method: 'PUT',
      body: {
        name: op.name, description: op.description || '', tags: op.tags || [],
        steps: op.steps || [],
        permission: op.permission === 'auto' ? 'auto' : 'ask',
        scope,
      },
    });
    toast(t('saved'), 'ok');
    renderOperations();
  } catch (error) { toast(error.message, 'error'); }
}

async function renderOperations() {
  const pane = document.getElementById('mas-tab-library');
  if (!pane) return;
  replace(pane, el('p', { class: 'mas-empty', text: '…' }));
  let data;
  try {
    data = await api('/agent/operations');
  } catch (error) {
    replace(pane, el('p', { class: 'mas-hint', text: error.message }),
      el('p', { class: 'mas-hint', text: t('opNeedsAgent') }));
    return;
  }
  state.operations = data.operations || [];
  state.stepTypes = data.stepTypes || {};

  const head = el('div', { class: 'mas-row mas-wrap' }, [
    button('＋ ' + t('opNew'), () => {
      state.editingOperation = {
        id: '', name: '', description: '', tags: '', steps: [], scope: 'general',
      };
      renderOperations();
    }, { class: 'mas-btn mas-primary' }),
    button('↻ ' + t('refresh'), renderOperations),
    el('span', { class: 'mas-hint', text: `${state.operations.length} ${t('opCount')}` }),
  ]);
  const cards = state.operations.map(operationCard);
  replace(pane,
    el('p', { class: 'mas-hint', text: t('opHint') }),
    head,
    state.editingOperation ? operationEditor(state.editingOperation) : null,
    el('div', { class: 'mas-ops' }, cards.length ? cards
      : [el('p', { class: 'mas-empty', text: t('opNone') })]),
    el('div', { class: 'mas-box mas-card' }, [
      el('div', { class: 'mas-card-head' }, [el('b', { text: '💾 ' + t('flowsLibTitle') })]),
      el('p', { class: 'mas-hint', text: t('flowsLibHint') }),
      el('div', { id: 'mas-flows-lib' }),
    ]),
  );
  renderLibrary();
}

/* ------------------------------------------------------------------ *
 * Operations management: what runs now, what waits in the queue and
 * what already finished - one list over engine runs and agent jobs.
 * ------------------------------------------------------------------ */

async function renderOps() {
  const pane = document.getElementById('mas-tab-ops');
  if (!pane) return;
  const subs = ['running', 'queue', 'done'];
  const active = subs.includes(state.opsSub) ? state.opsSub : 'running';
  const host = el('div', { class: 'mas-subhost' });
  replace(pane,
    el('div', { class: 'mas-tabs mas-subtabs' }, subs.map((name) => el('button', {
      class: 'mas-tab' + (name === active ? ' is-active' : ''),
      dataset: { opsub: name }, type: 'button',
      text: t('opsSub' + name.charAt(0).toUpperCase() + name.slice(1)),
      onClick: () => { state.opsSub = name; renderOps(); },
    }))),
    host);
  if (active === 'queue') return renderOpsQueue(host);
  if (active === 'done') return renderOpsDone(host);
  return renderOpsRunning(host);
}

async function renderOpsRunning(host) {
  replace(host, el('p', { class: 'mas-empty', text: '…' }));
  let status;
  let agent = null;
  try {
    status = await api('/status');
  } catch (error) {
    return replace(host, el('p', { class: 'mas-hint', text: error.message }));
  }
  try { agent = await api('/agent'); } catch (error) { agent = null; }
  const results = status.results || [];
  const last = results.length ? results[results.length - 1] : null;
  const runningJob = agent && agent.scheduler ? agent.scheduler.runningJob : null;
  const cards = [el('div', { class: 'mas-box mas-card' }, [
    el('div', { class: 'mas-card-head' }, [el('b', { text: '⚙️ ' + t('opsSubRunning') })]),
    agentTable(
      [t('opsColRun'), t('opsColStatus'), t('opsColStep'), t('opsColMotion'), t('opsColTime')],
      [[
        status.runId || '-',
        status.status || '-',
        results.length ? `#${last.index + 1}/${status.stepCount || results.length}` : '-',
        last ? `${last.label || last.type || '-'} ${last.params || ''}`.trim() : '-',
        status.elapsedMs ? `${Math.round(status.elapsedMs / 1000)}s` : '-',
      ]]),
    runningJob
      ? el('p', { class: 'mas-hint', text: `${t('opsJobRunning')}: ${runningJob}` })
      : el('p', { class: 'mas-hint', text: t('opsNoJob') }),
  ])];
  const entries = (status.entries || []).slice(-8).reverse();
  if (entries.length) {
    cards.push(el('div', { class: 'mas-box mas-card' }, [
      el('div', { class: 'mas-card-head' }, [el('b', { text: '📜 ' + t('opsLastEvents') })]),
      el('ul', { class: 'mas-log' }, entries.map((entry) => el('li', {
        class: `mas-log-${entry.level || 'info'}`,
        text: `${formatTime(entry.t)} · ${entry.message}`,
      }))),
    ]));
  }
  replace(host, ...cards);
}

async function renderOpsQueue(host) {
  replace(host, el('p', { class: 'mas-empty', text: '…' }));
  let jobs = [];
  let flows = [];
  try {
    jobs = (await api('/agent/jobs')).jobs || [];
  } catch (error) {
    return replace(host, el('p', { class: 'mas-hint', text: error.message }));
  }
  try { flows = (await api('/flows')).flows || []; } catch (error) { flows = []; }
  const targetSel = el('select', { class: 'mas-input' },
    flows.map((item) => el('option', { value: item.name, text: item.name })));
  const kindSel = el('select', { class: 'mas-input' }, [
    el('option', { value: 'at', text: 'at' }),
    el('option', { value: 'every', text: 'every' }),
    el('option', { value: 'cron', text: 'cron' }),
  ]);
  const scheduleInput = el('input', {
    class: 'mas-input', placeholder: '2026-10-12 07:00 · 90 · 5m · 30 7 * * 6',
  });
  const form = el('div', { class: 'mas-box mas-card' }, [
    el('div', { class: 'mas-card-head' }, [el('b', { text: '⏰ ' + t('opsScheduleTitle') })]),
    el('p', { class: 'mas-hint', text: t('opsScheduleHint') }),
    el('div', { class: 'mas-row mas-wrap' }, [targetSel, kindSel, scheduleInput,
      button('⏰ ' + t('opsSchedule'), async () => {
        const target = targetSel.value;
        if (!target || !scheduleInput.value.trim()) { toast(t('invalidFlow'), 'warn'); return; }
        try {
          await api('/agent/jobs', {
            method: 'POST',
            body: {
              name: target, kind: kindSel.value, schedule: scheduleInput.value.trim(),
              target, payload: { action: 'flow' },
            },
          });
          toast(t('opSaved'), 'ok');
          renderOps();
        } catch (error) { toast(error.message, 'error'); }
      }, { class: 'mas-btn mas-primary' }),
    ]),
  ]);
  const table = agentTable(
    [t('opsColName'), t('opsColSchedule'), t('opsColNext'), t('opsColLast'), ''],
    jobs.map((job) => [
      job.name || job.id,
      `${job.kind}: ${job.schedule}`,
      job.next_run_at ? formatTime(job.next_run_at) : '-',
      job.last_status || '-',
      el('div', { class: 'mas-row' }, [
        button('▶', async () => {
          try {
            await api('/agent/job-run', { method: 'POST', body: { id: job.id } });
            toast(t('opsStarted'), 'ok');
            state.opsSub = 'running';
            renderOps();
          } catch (error) { toast(error.message, 'error'); }
        }, { title: t('opsRunNow') }),
        button(job.enabled ? '⏸' : '▶️', async () => {
          try {
            await api(`/agent/jobs/${encodeURIComponent(job.id)}`,
              { method: 'PUT', body: { enabled: !job.enabled } });
            renderOps();
          } catch (error) { toast(error.message, 'error'); }
        }, { title: t('opsToggle') }),
        button('🗑', async () => {
          try {
            await api(`/agent/jobs/${encodeURIComponent(job.id)}`, { method: 'DELETE' });
            renderOps();
          } catch (error) { toast(error.message, 'error'); }
        }, { class: 'mas-btn mas-danger', title: t('delete') }),
      ]),
    ]));
  replace(host, form,
    jobs.length ? table : el('p', { class: 'mas-empty', text: t('opsNoJobs') }));
}

async function renderOpsDone(host) {
  replace(host, el('p', { class: 'mas-empty', text: '…' }));
  let history = [];
  try {
    history = (await api('/runs')).history || [];
  } catch (error) {
    return replace(host, el('p', { class: 'mas-hint', text: error.message }));
  }
  const rows = history.slice().reverse().map((run) => [
    run.runId || run.id || '-',
    run.status || '-',
    run.startedAt ? formatTime(run.startedAt) : '-',
    run.finishedAt ? formatTime(run.finishedAt) : '-',
    run.stepCount != null ? String(run.stepCount) : '-',
  ]);
  replace(host, rows.length
    ? agentTable([t('opsColRun'), t('opsColStatus'), t('opsColStart'),
      t('opsColEnd'), t('opsColSteps')], rows)
    : el('p', { class: 'mas-empty', text: t('opsNoRuns') }));
}

/* ------------------------------------------------------------------ *
 * Agent tab: the generated key, and the visible pointer
 * ------------------------------------------------------------------ */

function codeLine(text) {
  return el('div', { class: 'mas-row mas-codeline' }, [
    el('code', { class: 'mas-code', text }),
    button('📋', () => copyText(text), { class: 'mas-btn mas-mini', title: t('copy') }),
  ]);
}

async function renderAgentKey(host) {
  replace(host, el('p', { class: 'mas-empty', text: '…' }));
  let info;
  try {
    info = await api('/agent/key');
  } catch (error) { return agentError(host, error); }
  if (!state.apiIndex) {
    try { state.apiIndex = await api('/agent/api-index'); } catch (_) { state.apiIndex = null; }
  }
  const index = state.apiIndex || { endpoints: [], keyWays: [] };
  const keyBox = state.revealedKey
    ? el('div', { class: 'mas-row mas-wrap' }, [
      el('input', { class: 'mas-input mas-keyvalue', value: state.revealedKey, readOnly: true }),
      button('📋 ' + t('keyCopy'), () => copyText(state.revealedKey),
        { class: 'mas-btn mas-primary' }),
      button(t('keyHide'), () => { state.revealedKey = ''; renderAgentSub('agentkey'); }),
    ])
    : el('div', { class: 'mas-row mas-wrap' }, [
      el('code', { class: 'mas-code', text: info.prefix || t('keyNotSet') }),
      button('👁 ' + t('keyShow'), async () => {
        try {
          state.revealedKey = (await api('/agent/key/reveal', { method: 'POST', body: {} })).key || '';
          renderAgentSub('agentkey');
          toast(t('keyRevealAudited'), 'warn');
        } catch (error) { toast(error.message, 'error'); }
      }, { class: 'mas-btn mas-primary' }),
      button('🔄 ' + t('keyRotate'), async () => {
        try {
          const data = await api('/agent/key/rotate', { method: 'POST', body: {} });
          state.revealedKey = data.key || '';
          toast(t('keyRotated'), 'ok');
          renderAgentSub('agentkey');
        } catch (error) { toast(error.message, 'error'); }
      }),
    ]);

  const enabled = !!info.enabled;
  const radios = [true, false].map((value) => el('label', { class: 'mas-check' }, [
    el('input', {
      type: 'radio', name: 'mas-key-enabled', checked: value === enabled,
      onChange: async () => {
        try {
          await api('/agent/key/enable', { method: 'POST', body: { enabled: value } });
          toast(value ? t('keyEnabled') : t('keyDisabled'), value ? 'ok' : 'warn');
          renderAgentSub('agentkey');
        } catch (error) { toast(error.message, 'error'); }
      },
    }),
    el('span', { text: value ? t('keyEnabledLabel') : t('keyDisabledLabel') }),
  ]));

  const endpoints = (index.endpoints || []).map((item) => el('tr', {}, [
    el('td', {}, el('code', { text: item.method })),
    el('td', {}, el('div', { class: 'mas-row' }, [
      el('code', { class: 'mas-code', text: item.path }),
      button('📋', () => copyText(item.path), { class: 'mas-btn mas-mini', title: t('copy') }),
    ])),
    el('td', { class: 'mas-hint', text: state.lang === 'fa' ? item.fa : item.en }),
  ]));

  replace(host,
    el('div', { class: 'mas-box' }, [
      el('b', { text: t('keyTitle') }),
      el('p', { class: 'mas-hint', text: t('keyHint') }),
      keyBox,
      el('div', { class: 'mas-row mas-wrap' }, [
        el('span', { class: 'mas-hint', text: `${t('keyCreatedAt')}: ${agentTime(info.createdAt)}` }),
        el('span', { class: 'mas-hint', text: `${t('keyLastUsed')}: ${agentTime(info.lastUsedAt)}` }),
        el('span', { class: 'mas-hint', text: `${t('keyLength')}: ${info.length || 0}` }),
        el('span', { class: 'mas-hint', text: `${t('keyLastUse')}: ${
          info.lastUse ? `${info.lastUse.endpoint || ''} — ${tehranWhen(info.lastUse.at)}` : '—'}` }),
      ]),
      el('div', { class: 'mas-row mas-wrap' }, radios),
      el('div', { class: 'mas-row mas-wrap' }, [
        button('🔌 ' + t('keyTestConn'), async () => {
          // The connector check the agent letter asked for: prove the key
          // itself opens GET /agent, over a header, without ?k= anywhere.
          try {
            if (!state.revealedKey) {
              state.revealedKey = (await api('/agent/key/reveal', { method: 'POST', body: {} })).key || '';
            }
            const response = await fetch(API_PREFIX + '/agent', {
              headers: { 'x-agent-key': state.revealedKey },
            });
            toast(response.ok ? t('keyTestOk') : `${t('keyTestFail')} (HTTP ${response.status})`,
              response.ok ? 'ok' : 'error');
            renderAgentSub('agentkey');
          } catch (error) { toast(error.message, 'error'); }
        }),
        button('⏸ ' + t('keyCut'), async () => {
          try {
            await api('/agent/key/enable', { method: 'POST', body: { enabled: false } });
            toast(t('keyCutDone'), 'warn');
            renderAgentSub('agentkey');
          } catch (error) { toast(error.message, 'error'); }
        }, { class: 'mas-btn mas-danger' }),
        el('span', { class: 'mas-hint', text: t('keyCutHint') }),
      ]),
    ]),
    el('div', { class: 'mas-box' }, [
      el('b', { text: t('keyWays') }),
      el('p', { class: 'mas-hint', text: t('keyWaysHint') }),
      (index.keyWays || []).map(codeLine),
      el('p', { class: 'mas-warn', text: t('keyCutNote') }),
    ]),
    el('div', { class: 'mas-box' }, [
      el('div', { class: 'mas-row mas-wrap' }, [
        el('b', { text: `${t('keyEndpoints')} (${endpoints.length})` }),
        button('📋 ' + t('keyCopyAll'), () => copyText(apiIndexText(index))),
      ]),
      el('p', { class: 'mas-hint', text: t('keyEndpointsHint') }),
      endpoints.length ? el('table', { class: 'mas-table' }, [
        el('thead', {}, el('tr', {}, [
          el('th', { text: t('keyMethod') }), el('th', { text: t('keyPath') }),
          el('th', { text: t('keyWhat') }),
        ])),
        el('tbody', {}, endpoints),
      ]) : el('p', { class: 'mas-empty', text: t('keyNoEndpoints') }),
    ]),
  );
}

/** The whole endpoint list as one block, ready to paste into another AI. */
function apiIndexText(index) {
  const lines = [t('keyEndpoints'), ''];
  for (const item of (index.endpoints || [])) {
    lines.push(`${item.method} ${item.path}`);
    lines.push(`  ${state.lang === 'fa' ? item.fa : item.en}`);
  }
  lines.push('', t('keyWays'));
  for (const way of (index.keyWays || [])) lines.push(`  ${way}`);
  lines.push('', t('keyCutNote'));
  return lines.join('\n');
}

async function renderAgentCursor(host) {
  replace(host, el('p', { class: 'mas-empty', text: '…' }));
  let cursor;
  try {
    cursor = (await api('/agent/cursor')).cursor || {};
  } catch (error) { return agentError(host, error); }
  const draft = Object.assign({ enabled: true, size: 44, color: '#ffd400',
    outline: '#1b1b1b', ripple: true }, cursor);
  const size = el('input', {
    class: 'mas-input', type: 'number', min: '16', max: '96', value: String(draft.size),
    onInput: (event) => { draft.size = Number(event.target.value); },
  });
  replace(host,
    el('div', { class: 'mas-box' }, [
      el('b', { text: t('cursorTitle') }),
      el('p', { class: 'mas-hint', text: t('cursorHint') }),
      el('div', { class: 'mas-row mas-wrap' }, [
        el('label', { class: 'mas-check' }, [
          el('input', {
            type: 'checkbox', checked: !!draft.enabled,
            onChange: (event) => { draft.enabled = event.target.checked; },
          }),
          el('span', { text: t('cursorEnabled') }),
        ]),
        el('label', { class: 'mas-check' }, [
          el('input', {
            type: 'checkbox', checked: !!draft.ripple,
            onChange: (event) => { draft.ripple = event.target.checked; },
          }),
          el('span', { text: t('cursorRipple') }),
        ]),
        el('label', { class: 'mas-field' }, [el('span', { text: t('cursorSize') }), size]),
        el('label', { class: 'mas-field' }, [
          el('span', { text: t('cursorColor') }),
          el('input', {
            class: 'mas-input mas-color', type: 'color', value: draft.color,
            onInput: (event) => { draft.color = event.target.value; },
          }),
        ]),
        el('label', { class: 'mas-field' }, [
          el('span', { text: t('cursorOutline') }),
          el('input', {
            class: 'mas-input mas-color', type: 'color', value: draft.outline,
            onInput: (event) => { draft.outline = event.target.value; },
          }),
        ]),
      ]),
      el('div', { class: 'mas-cursor-preview' }, [
        el('span', {
          class: 'mas-cursor-dot',
          style: `width:${draft.size / 2}px;height:${draft.size / 2}px;`
            + `background:${draft.color};border:2px solid ${draft.outline}`,
        }),
        el('span', { class: 'mas-hint', text: t('cursorPreview') }),
      ]),
      el('div', { class: 'mas-row mas-wrap' }, [
        button('💾 ' + t('cursorSave'), async () => {
          try {
            const data = await api('/agent/cursor', { method: 'POST', body: draft });
            toast(data.cursor && data.cursor.applied === false
              ? t('cursorSavedNoBrowser') : t('cursorSaved'), 'ok');
            renderAgentSub('cursor');
          } catch (error) { toast(error.message, 'error'); }
        }, { class: 'mas-btn mas-primary' }),
        button('🖱 ' + t('cursorMove'), () => manualControl(
          { action: 'move', x: 480, y: 320 }, t('cursorMoved'))),
      ]),
      el('p', { class: 'mas-hint', text: t('cursorMoveHint') }),
    ]),
  );
}

function renderLog() {
  const pane = document.getElementById('mas-tab-log');
  const entries = (state.status.entries || []).slice(-120);
  if (!entries.length) {
    replace(pane, el('p', { class: 'mas-empty', text: t('logEmpty') }));
    return;
  }
  replace(pane, el('div', { class: 'mas-log' }, entries.map((entry) => el('div', {
    class: `mas-log-line mas-log-${entry.level}`,
  }, [
    el('span', { class: 'mas-log-t', text: formatDateTime(entry.t) }),
    el('span', { text: entry.message }),
  ]))));
  pane.scrollTop = pane.scrollHeight;
}

/* ------------------------------------------------------------------ *
 * Boot
 * ------------------------------------------------------------------ */

function start() {
  if (document.body.classList.contains('mas-human')) {
    // Round 27, item 6: the third panel - a dedicated page whose whole job
    // is the moments the automation needs a human. It polls the one shared
    // run-state, so a handoff acted on here advances the flow exactly like
    // acting on the desktop view or the big panel.
    state.clipSyncOn = false;
    applyTheme(state.theme, { save: false });
    buildHumanPanel();
    refreshStatus();
    setInterval(refreshStatus, 900);
    refreshHumanSuggestion();
    setInterval(refreshHumanSuggestion, 2000);
    return;
  }
  const standalone = isStandalone();
  state.clipSyncOn = localStorage.getItem(LS.clip) === '1';
  applyTheme(state.theme, { save: false });
  if (!standalone) {
    // The desktop view is a clean dark stage now. The operator's verdict:
    // the slide-out panel inside vnc.html duplicated the big dedicated
    // panel and had to go. What remains is one floating button on the
    // right that leads to that panel, a host so clipboard-sync toasts can
    // still land, and the silent clipboard sync itself - copies made on
    // the operator's own machine keep reaching the desktop clipboard.
    document.body.classList.add('mas-desktop-dark');
    buildDesktopLauncher();
    startClipSync();
    // Round 27, item 2: the desktop view polls the same server state as the
    // big panel, so the "needs human action" card can appear here at the
    // same time. Both press the same endpoints; either one advances the flow.
    refreshStatus();
    setInterval(refreshStatus, 900);
    return;
  }
  buildPanel();
  startClipSync();
  wireStandaloneHead();
  setPanelOpen(true);
  selectTab(initialTab());
  refreshAgentTabs();
  renderStatus();
  refreshStatus();
  setInterval(refreshStatus, 900);
  // Counters tick on their own so the numbers move smoothly between polls.
  setInterval(tickTimers, 1000);
}

/* The sidebar is a guest in noVNC's page. If it throws, noVNC shows its own
 * error dialog and the human cannot even reach the password prompt, so nothing
 * here is allowed to escape. */
function boot() {
  try {
    start();
  } catch (error) {
    if (window.console && window.console.error) {
      window.console.error('[automation] sidebar failed to start:', error);
    }
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot);
} else {
  boot();
}

/* ------------------------------------------------------------------ *
 * Brain tab: the awake supervisor - the first tab, with two sub-tabs.
 * "Talk to the brain" is an interactive page: one big start/stop
 * button, the open suggestion with its two decisions, a conversation
 * the operator types into and the brain answers by its rulebook, and
 * the brain's personal memory. "Brain settings" keeps the policies,
 * the operations as the brain sees them and the brain's own log.
 * ------------------------------------------------------------------ */

async function renderBrain() {
  const pane = document.getElementById('mas-tab-brain');
  if (!pane) return;
  const subs = ['interact', 'settings'];
  const active = subs.includes(state.brainSub) ? state.brainSub : 'interact';
  const host = el('div', { class: 'mas-subhost' });
  replace(pane,
    el('div', { class: 'mas-tabs mas-subtabs' }, subs.map((name) => el('button', {
      class: 'mas-tab' + (name === active ? ' is-active' : ''),
      dataset: { brainsub: name }, type: 'button',
      text: t('brainSub' + name.charAt(0).toUpperCase() + name.slice(1)),
      onClick: () => {
        state.brainSub = name;
        saveUiPref('brainSub', name);
        renderBrain();
      },
    }))),
    host);
  replace(host, el('p', { class: 'mas-empty', text: '…' }));
  let data;
  try {
    data = await api('/agent/brain');
  } catch (error) {
    stopBrainPolling();
    replace(host, el('p', { class: 'mas-hint', text: error.message }),
      el('p', { class: 'mas-hint', text: t('opNeedsAgent') }));
    return;
  }
  state.brainSugSeen = Boolean(data.suggestion);
  state.brainMsgCount = (data.messages || []).length;
  if (active === 'settings') renderBrainSettings(host, data);
  else renderBrainInteract(host, data);
  startBrainPolling();
}

function renderBrainInteract(host, data) {
  const messages = data.messages || [];
  const suggestion = data.suggestion;
  const big = el('button', {
    class: 'mas-btn mas-brain-big ' + (data.enabled ? 'mas-danger' : 'mas-primary'),
    type: 'button', id: 'mas-brain-big',
    text: data.enabled ? t('brainStopBig') : t('brainStartBig'),
    onClick: () => brainToggle(!data.enabled),
  });
  replace(host,
    el('div', { class: 'mas-box mas-card' }, [
      el('div', { class: 'mas-card-head' }, [el('b', { text: '🧠 ' + t('brainTitle') })]),
      el('p', {
        class: 'mas-hint', id: 'mas-brain-state',
        text: (data.enabled ? t('brainIsOn') : t('brainIsOff'))
          + ' · ' + (data.alive ? t('brainAlive') : t('brainStopped'))
          + ' · ' + (data.busy ? t('brainBusy') : t('brainIdle')),
      }),
      el('div', { class: 'mas-row' }, [big]),
    ]),
    el('div', { class: 'mas-box mas-card' }, [
      el('div', { class: 'mas-card-head' },
        [el('b', { text: '💡 ' + t('brainSuggestion') })]),
      suggestion ? el('div', {}, compact([
        el('p', { text: suggestion.name }),
        el('div', { class: 'mas-row mas-wrap' }, [
          button('▶ ' + t('brainRunIt'), () => brainDecide(suggestion.id, 'run'),
            { class: 'mas-btn mas-primary' }),
          button('✕ ' + t('brainReject'), () => brainDecide(suggestion.id, 'reject'),
            { class: 'mas-btn mas-danger' }),
        ]),
      ])) : el('p', { class: 'mas-hint', text: t('brainNoSuggestion') }),
    ]),
    el('div', { class: 'mas-box mas-card' }, [
      el('div', { class: 'mas-card-head' },
        [el('b', { text: '💬 ' + t('brainSubInteract') })]),
      el('p', { class: 'mas-hint', text: t('brainInteractHint') }),
      el('div', { id: 'mas-brain-msgs', class: 'mas-chat-list' },
        messages.length ? messages.map(brainBubble)
          : [el('p', { class: 'mas-empty', text: t('brainNoMessages') })]),
      el('div', { class: 'mas-chat-compose' }, [
        el('textarea', {
          id: 'mas-brain-input', class: 'mas-input mas-area', rows: '2',
          value: state.brainDraft, placeholder: t('brainChatPlaceholder'),
          onInput: (event) => { state.brainDraft = event.target.value; },
          onKeydown: (event) => {
            if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
              event.preventDefault();
              sendBrainMessage();
            }
          },
        }),
        button('➤ ' + t('brainSend'), sendBrainMessage,
          { class: 'mas-btn mas-primary', id: 'mas-brain-send' }),
      ]),
    ]),
    el('div', { class: 'mas-box mas-card' }, [
      el('div', { class: 'mas-card-head' },
        [el('b', { text: '🗃 ' + t('brainMemoryTitle') })]),
      (data.memory || []).length
        ? el('div', { class: 'mas-log' }, data.memory.map((row) => el('div', {
          class: 'mas-log-line',
        }, compact([
          el('span', { class: 'mas-log-t', text: formatTime(row.at) }),
          el('span', { text: row.text }),
        ]))))
        : el('p', { class: 'mas-hint', text: t('brainMemoryEmpty') }),
    ]),
    // Round 27, item 4: the brain's continuous step report - every decision,
    // run, suggestion and observation with its exact timestamp. The coworker
    // agent reads the same stream through GET /agent/reports?kind=brain.
    el('div', { class: 'mas-box mas-card' }, [
      el('div', { class: 'mas-card-head' }, [
        el('b', { text: '📜 ' + t('brainStepsTitle') }),
        button('🔄 ' + t('brainStepsAll'), loadBrainSteps,
          { class: 'mas-btn mas-mini', id: 'mas-brain-steps-all' }),
      ]),
      el('p', { class: 'mas-hint', text: t('brainStepsHint') }),
      (data.steps || []).length
        ? el('div', { id: 'mas-brain-steps', class: 'mas-log' },
          data.steps.map((row) => el('div', {
            class: 'mas-log-line' + (row.ok ? '' : ' mas-log-error'),
          }, compact([
            el('span', { class: 'mas-log-t', text: formatDateTime(row.at) }),
            el('span', { class: 'mas-chip', text: row.phase }),
            el('span', { text: row.message }),
          ]))))
        : el('p', { id: 'mas-brain-steps', class: 'mas-hint',
            text: t('brainStepsEmpty') }),
    ]));
  const list = document.getElementById('mas-brain-msgs');
  if (list) list.scrollTop = list.scrollHeight;
}

function brainBubble(row) {
  const mine = row.direction === 'in';
  return el('div', {
    class: 'mas-bubble ' + (mine ? 'is-mine' : 'is-ai'),
    dataset: { dir: row.direction },
  }, compact([
    el('div', { class: 'mas-hint', text: formatDateTime(row.at) }),
    el('pre', { class: 'mas-bubble-text', text: row.text || '' }),
  ]));
}

/* Round 27, item 4: the full step-report stream straight from the database -
 * the same route the coworker agent reads (GET /agent/reports?kind=brain). */
async function loadBrainSteps() {
  const host = document.getElementById('mas-brain-steps');
  if (!host) return;
  try {
    const data = await api('/agent/reports?kind=brain&limit=200');
    const rows = data.reports || [];
    replace(host, rows.length ? rows.map((row) => el('div', {
      class: 'mas-log-line' + (row.ok ? '' : ' mas-log-error'),
    }, compact([
      el('span', { class: 'mas-log-t', text: formatDateTime(row.at) }),
      el('span', { class: 'mas-chip', text: row.phase }),
      el('span', { text: row.message }),
    ]))) : [el('p', { class: 'mas-hint', text: t('brainStepsEmpty') })]);
  } catch (error) { toast(error.message, 'error'); }
}

async function sendBrainMessage() {
  const text = String(state.brainDraft || '').trim();
  if (!text) return;
  state.brainDraft = '';
  try {
    await api('/agent/brain/message', { method: 'POST', body: { text } });
    renderBrain();
  } catch (error) {
    state.brainDraft = text;
    toast(error.message, 'error');
  }
}

function renderBrainSettings(host, data) {
  const policy = data.policy || {};
  const statusCard = el('div', { class: 'mas-box mas-card' }, [
    el('div', { class: 'mas-card-head' }, [el('b', { text: '🧠 ' + t('brainTitle') })]),
    el('p', { class: 'mas-hint', text: t('brainHint') }),
    el('div', { class: 'mas-row mas-wrap' }, compact([
      el('label', { class: 'mas-check' }, [
        el('input', {
          type: 'checkbox', id: 'mas-brain-enabled', checked: Boolean(data.enabled),
          onChange: (event) => brainToggle(event.target.checked),
        }),
        el('span', { text: t('brainEnabled') }),
      ]),
      button('↻ ' + t('refresh'), renderBrain),
    ])),
  ]);
  const opRow = (op, badge) => el('div', { class: 'mas-op' }, compact([
    el('b', { text: op.name }),
    el('span', { class: 'mas-badge', text: badge }),
    op.last_run_at
      ? el('span', { class: 'mas-hint', text: agentTime(op.last_run_at) }) : null,
  ]));
  const opsCard = el('div', { class: 'mas-box mas-card' }, [
    el('div', { class: 'mas-card-head' }, [el('b', { text: '🗂 ' + t('brainOps') })]),
    el('div', { class: 'mas-ops' }, compact([
      (data.autoOps || []).length || (data.askOps || []).length ? null
        : el('p', { class: 'mas-empty', text: t('opNone') }),
      (data.autoOps || []).map((op) => opRow(op, t('brainAuto'))),
      (data.askOps || []).map((op) => opRow(op, t('brainAsk'))),
    ])),
  ]);
  const policyCard = el('div', { class: 'mas-box mas-card' }, [
    el('div', { class: 'mas-card-head' },
      [el('b', { text: '🎛 ' + t('brainPolicy') })]),
    el('div', { class: 'mas-row mas-wrap' }, [
      brainNumber('brain.runGapSec', policy.runGapSec, t('brainRunGap')),
      brainNumber('brain.opCooldownMin', policy.opCooldownMin, t('brainCooldown')),
      brainNumber('brain.suggestTtlMin', policy.suggestTtlMin, t('brainTtl')),
      brainNumber('brain.reportGapSec', policy.reportGapSec, t('brainReportGap')),
      brainNumber('log.shotKeepDays', policy.shotKeepDays, t('brainShotKeep')),
      brainNumber('log.errShotKeepDays', policy.errShotKeepDays, t('brainErrKeep')),
    ]),
    el('label', { class: 'mas-check' }, [
      el('input', {
        type: 'checkbox', id: 'mas-brain-forceshots',
        checked: policy.forceStepShots !== false,
        onChange: (event) => savePolicy('log.forceStepShots', event.target.checked),
      }),
      el('span', { text: t('brainForceShots') }),
    ]),
    el('label', { class: 'mas-check' }, [
      el('input', {
        type: 'checkbox', id: 'mas-brain-notify', checked: policy.notify !== false,
        onChange: (event) => savePolicy('brain.notify', event.target.checked),
      }),
      el('span', { text: t('brainNotify') }),
    ]),
  ]);
  const logCard = el('div', { class: 'mas-box mas-card' }, [
    el('div', { class: 'mas-card-head' }, [el('b', { text: '📜 ' + t('brainLog') })]),
    el('div', { class: 'mas-log' }, (data.log || []).map((entry) => el('div', {
      class: 'mas-log-line',
    }, compact([
      el('span', { class: 'mas-log-t', text: formatDateTime(entry.at) }),
      el('span', { text: entry.title }),
    ])))),
  ]);
  replace(host, statusCard, opsCard, policyCard, logCard);
}

function brainNumber(key, value, label) {
  return el('label', { class: 'mas-field' }, compact([
    el('span', { class: 'mas-hint', text: label }),
    el('input', {
      class: 'mas-input', type: 'number', min: '0',
      value: value === null || value === undefined ? '' : String(value),
      onChange: (event) => savePolicy(key, parseInt(event.target.value, 10) || 0),
    }),
  ]));
}

async function savePolicy(key, value) {
  try {
    await api('/agent/settings', { method: 'POST', body: { [key]: value } });
    toast(t('saved'), 'ok');
  } catch (error) { toast(error.message, 'error'); }
}

async function brainToggle(enabled) {
  try {
    await api('/agent/brain/toggle', { method: 'POST', body: { enabled } });
    toast(enabled ? t('brainOn') : t('brainOff'), 'ok');
    renderBrain();
  } catch (error) { toast(error.message, 'error'); }
}

async function brainDecide(suggestionId, action) {
  try {
    await api('/agent/brain/suggestion',
      { method: 'POST', body: { id: suggestionId, action } });
    toast(t(action === 'run' ? 'brainStarted' : 'brainRejected'), 'ok');
    renderBrain();
  } catch (error) { toast(error.message, 'error'); }
}

function startBrainPolling() {
  stopBrainPolling();
  brainTimer = setInterval(async () => {
    const pane = document.getElementById('mas-tab-brain');
    if (!pane || pane.hidden) { stopBrainPolling(); return; }
    try {
      const data = await api('/agent/brain');
      const line = document.getElementById('mas-brain-state');
      if (line) {
        line.textContent = (data.enabled ? t('brainIsOn') : t('brainIsOff'))
          + ' · ' + (data.alive ? t('brainAlive') : t('brainStopped'))
          + ' · ' + (data.busy ? t('brainBusy') : t('brainIdle'));
      }
      // Redraw only when the conversation grows or a suggestion comes and
      // goes: a full redraw on every tick would be wasteful, and the draft
      // lives in state so even a redraw cannot eat what is being typed.
      if (Boolean(data.suggestion) !== Boolean(state.brainSugSeen)
          || (data.messages || []).length !== state.brainMsgCount) {
        renderBrain();
      }
    } catch (_) { /* the next tick tries again */ }
  }, 5000);
}

function stopBrainPolling() {
  if (brainTimer !== null) { clearInterval(brainTimer); brainTimer = null; }
}

async function setOperationPermission(op, permission) {
  try {
    await api(`/agent/operations/${encodeURIComponent(op.id)}`, {
      method: 'PUT',
      body: {
        name: op.name, description: op.description || '', tags: op.tags || [],
        steps: op.steps || [], permission,
        scope: op.scope === 'brain' ? 'brain' : 'general',
      },
    });
    toast(t('saved'), 'ok');
    renderOperations();
  } catch (error) { toast(error.message, 'error'); }
}

/* ------------------------------------------------------------------ *
 * Database tab: continuous backups
 * ------------------------------------------------------------------ */

async function renderDatabase() {
  const pane = document.getElementById('mas-tab-db');
  if (!pane) return;
  // The operator's grouping: backups, letters, screenshots and extracted
  // texts all live under the database tab as sub-tabs.
  // Round 27, item 3: 'letters' is the dedicated home of the two-way mail
  // between the operator and the coworker agent.
  const subs = ['backups', 'letters', 'reports', 'shots', 'texts'];
  const active = subs.includes(state.dbSub) ? state.dbSub : 'backups';
  const host = el('div', { class: 'mas-subhost' });
  replace(pane,
    el('div', { class: 'mas-tabs mas-subtabs' }, subs.map((name) => el('button', {
      class: 'mas-tab' + (name === active ? ' is-active' : ''),
      dataset: { dbsub: name }, type: 'button',
      text: t('dbSub' + name.charAt(0).toUpperCase() + name.slice(1)),
      onClick: () => { state.dbSub = name; saveUiPref('dbSub', name); renderDatabase(); },
    }))),
    host);
  if (active === 'shots') return renderShots(host);
  if (active === 'texts') return renderTexts(host);
  if (active === 'letters') return renderLetters(host);
  if (active === 'reports') return renderReports(host);
  return renderBackups(host);
}

/* Round 27, item 5: «گزارشات ایجنت همکار» - every message the operator sends
 * the agent produces a precise, timestamped report of what the agent did,
 * and the brain's and the scheduler's streams are one click away. The
 * operator reads everything here; nothing about the database is hidden. */
async function renderReports(host) {
  const pane = host;
  replace(pane, el('p', { class: 'mas-empty', text: '…' }));
  // '' is a real value here: it means "every stream" (the all-filter).
  const kind = state.reportsKind === undefined || state.reportsKind === null
    ? 'agent' : String(state.reportsKind);
  let data;
  try {
    data = await api(`/agent/reports?kind=${encodeURIComponent(kind)}&limit=200`);
  } catch (error) {
    replace(pane, el('p', { class: 'mas-warn', text: error.message || String(error) }));
    return;
  }
  const reports = data.reports || [];
  const kinds = [['agent', 'reportsKindAgent'], ['brain', 'reportsKindBrain'],
    ['scheduler', 'reportsKindScheduler'], ['', 'reportsKindAll']];
  const filterRow = el('div', { class: 'mas-row mas-wrap' }, kinds.map((pair) =>
    button(t(pair[1]), () => {
      state.reportsKind = pair[0];
      saveUiPref('reportsKind', pair[0]);
      renderDatabase();
    }, { class: 'mas-btn' + (pair[0] === kind ? ' mas-primary' : '') })));
  const listCard = el('div', { class: 'mas-box mas-card' }, [
    el('div', { class: 'mas-card-head' }, [
      el('b', { text: '📜 ' + t('reportsTitle') }),
      button('🔄', () => renderDatabase(), { class: 'mas-btn' }),
    ]),
    el('p', { class: 'mas-hint', text: t('reportsHint') }),
    filterRow,
    reports.length ? el('div', { class: 'mas-reports' }, reports.map((row) => el('div', {
      class: 'mas-report' + (row.ok ? '' : ' is-bad'),
    }, [
      el('div', { class: 'mas-report-head' }, compact([
        el('span', { class: 'mas-report-t', text: formatDateTime(row.at) }),
        el('span', {
          class: 'mas-badge ' + (row.kind === 'agent' ? 'mas-badge-agent'
            : row.kind === 'brain' ? 'mas-badge-brain' : 'mas-badge-user'),
          text: row.kind === 'agent' ? '🤖 ' + t('reportsKindAgent')
            : row.kind === 'brain' ? '🧠 ' + t('reportsKindBrain')
              : '⏰ ' + t('reportsKindScheduler'),
        }),
        row.phase ? el('span', { class: 'mas-chip', text: row.phase }) : null,
        row.ok ? null
          : el('span', { class: 'mas-chip mas-chip-bad', text: t('reportsBad') }),
      ])),
      el('div', { class: 'mas-report-msg', text: row.message || '' }),
    ]))) : el('p', { class: 'mas-hint', text: t('reportsEmpty') }),
  ]);
  replace(pane, listCard);
}

/* Round 27, item 3: نامه‌ها - the letter box between the operator and the
 * coworker agent. Operator letters explain new abilities and changes so the
 * agent is never confused; undelivered ones are handed to the agent at the
 * start of its next conversation turn (chat tab and assistant alike) and
 * then marked delivered. Agent replies can be archived here with one click
 * from the chat bubble, and the agent itself may post through its key. */
async function renderLetters(host) {
  const pane = host;
  replace(pane, el('p', { class: 'mas-empty', text: '…' }));
  let data;
  try {
    data = await api('/agent/letters');
  } catch (error) {
    replace(pane, el('p', { class: 'mas-warn', text: error.message || String(error) }));
    return;
  }
  const letters = data.letters || [];
  const draft = { direction: 'to_agent', subject: '', body: '' };

  const compose = el('div', { class: 'mas-box mas-card' }, [
    el('div', { class: 'mas-card-head' }, [
      el('b', { text: '✉️ ' + t('lettersComposeTitle') }),
      el('span', {
        class: 'mas-chip', id: 'mas-letters-unread',
        text: `${data.unread || 0} ${t('lettersUnreadChip')}`,
      }),
    ]),
    el('p', { class: 'mas-hint', text: t('lettersHint') }),
    el('div', { class: 'mas-row mas-wrap' }, [
      el('label', { class: 'mas-field' }, [
        el('span', { text: t('lettersDirection') }),
        el('select', {
          id: 'mas-letters-direction', class: 'mas-input',
          onChange: (event) => { draft.direction = event.target.value; },
        }, [
          el('option', { value: 'to_agent', selected: true }, t('lettersDirToAgent')),
          el('option', { value: 'to_user' }, t('lettersDirToUser')),
        ]),
      ]),
      el('label', { class: 'mas-field mas-grow' }, [
        el('span', { text: t('lettersSubject') }),
        el('input', {
          id: 'mas-letters-subject', class: 'mas-input', type: 'text',
          placeholder: t('lettersSubjectPh'),
          onInput: (event) => { draft.subject = event.target.value; },
        }),
      ]),
    ]),
    el('textarea', {
      id: 'mas-letters-body', class: 'mas-input mas-area', rows: '5',
      placeholder: t('lettersBodyPh'),
      onInput: (event) => { draft.body = event.target.value; },
    }),
    el('div', { class: 'mas-row' }, [
      button('📨 ' + t('lettersSend'), async () => {
        if (!draft.body.trim()) { toast(t('lettersNeedBody'), 'warn'); return; }
        try {
          await api('/agent/letters', {
            method: 'POST',
            body: { direction: draft.direction, subject: draft.subject,
                    body: draft.body },
          });
          toast(t('lettersSent'), 'ok');
          renderDatabase();
        } catch (error) { toast(error.message, 'error'); }
      }, { class: 'mas-btn mas-primary' }),
    ]),
  ]);

  const listCard = el('div', { class: 'mas-box mas-card' }, [
    el('div', { class: 'mas-card-head' }, [
      el('b', { text: '🗃 ' + t('lettersListTitle') }),
      button('🔄', () => renderDatabase(), { class: 'mas-btn' }),
    ]),
    letters.length ? el('div', { class: 'mas-letters' }, letters.map((item) => el('div', {
      class: 'mas-letter' + (item.direction === 'to_agent' && item.status === 'new'
        ? ' is-new' : ''),
    }, [
      el('div', { class: 'mas-letter-head' }, [
        el('span', {
          class: 'mas-badge ' + (item.direction === 'to_agent'
            ? 'mas-badge-user' : 'mas-badge-agent'),
          text: item.direction === 'to_agent'
            ? '🧍 ' + t('lettersDirToAgent') : '🤖 ' + t('lettersDirToUser'),
        }),
        item.subject ? el('b', { text: item.subject }) : null,
        el('span', { class: 'mas-letter-when', text: tehranWhen(item.at) }),
        item.direction === 'to_agent' ? el('span', {
          class: 'mas-chip',
          text: item.status === 'new' ? t('lettersStatusNew') : t('lettersStatusRead'),
        }) : null,
        button('🗑', async () => {
          if (!window.confirm(t('lettersDeleteConfirm'))) return;
          try {
            await api(`/agent/letters/${encodeURIComponent(item.id)}`,
              { method: 'DELETE' });
            renderDatabase();
          } catch (error) { toast(error.message, 'error'); }
        }, { class: 'mas-btn mas-mini mas-danger', title: t('lettersDelete') }),
      ]),
      el('pre', { class: 'mas-letter-body', text: item.body || '' }),
    ]))) : el('p', { class: 'mas-hint', text: t('lettersEmpty') }),
  ]);

  replace(pane, compose, listCard);
}

async function renderBackups(host) {
  const pane = host;
  replace(pane, el('p', { class: 'mas-empty', text: '…' }));
  let data;
  try {
    data = await api('/agent/backups');
  } catch (error) {
    replace(pane, el('p', { class: 'mas-warn', text: error.message || String(error) }));
    return;
  }
  const stats = data.stats || {};
  const config = stats.config || {};
  const backups = data.backups || [];
  const draft = {
    enabled: !!config.enabled, kind: config.scheduleKind || 'cron',
    schedule: config.schedule || '', keep: config.keep || 7,
    target: config.telegramTarget || '', channel: config.telegramChannel || '',
    secrets: !!config.includeSecrets, notify: !!config.notifyAfterBackup,
  };
  const sendDraft = { name: '', target: draft.target, channel: draft.channel };

  const tables = stats.tables || {};
  const statsCard = el('div', { class: 'mas-box mas-card' }, [
    el('div', { class: 'mas-card-head' }, [
      el('b', { text: '🗄 ' + t('dbStatsTitle') }),
      el('span', { class: 'mas-chip', text: stats.dbSizeText || '-' }),
      el('span', { class: 'mas-chip', text: `${stats.flows || 0} ${t('dbFlows')}` }),
      el('span', { class: 'mas-chip', text: `${stats.backups || 0} ${t('dbBackupCount')} · ${stats.backupSizeText || '-'}` }),
    ]),
    el('p', { class: 'mas-hint', text: t('dbHint') }),
    el('div', { class: 'mas-row mas-wrap mas-chips' }, Object.keys(tables).map((name) => el('span', {
      class: 'mas-chip', text: `${name}: ${tables[name]}`,
    }))),
    el('p', { class: 'mas-hint', text: stats.lastBackup
      ? `${t('dbLastBackup')}: ${stats.lastBackup.name} · ${tehranWhen(stats.lastBackup.createdAt)}`
      : t('dbNone') }),
  ]);

  const downloadBackup = async (name) => {
    try {
      const url = await imageBlobUrl(`/agent/backup-download?name=${encodeURIComponent(name)}`);
      if (!url) { toast(t('dbMissing'), 'error'); return; }
      const link = document.createElement('a');
      link.href = url; link.download = name;
      document.body.appendChild(link); link.click(); link.remove();
      toast(t('dbDownloadOk'), 'ok');
    } catch (error) { toast(error.message, 'error'); }
  };

  const listCard = el('div', { class: 'mas-box mas-card' }, [
    el('div', { class: 'mas-card-head' }, [
      el('b', { text: '📦 ' + t('dbListTitle') }),
      button('🔄', () => renderDatabase(), { class: 'mas-btn' }),
    ]),
    backups.length ? agentTable(
      [t('dbColName'), t('dbColSize'), t('dbColWhen'), t('dbColActions')],
      backups.map((row) => [
        row.name, row.sizeText, tehranWhen(row.created_at || row.createdAt),
        el('span', { class: 'mas-row' }, [
          button('♻️ ' + t('dbRestore'), async () => {
            // The operator's rule: wherever a backup button lives, a restore
            // button lives beside it. Restoring replaces the live database,
            // so the server restarts itself right after (same pid).
            if (!window.confirm(t('dbConfirmRestore'))) return;
            try {
              await api('/agent/backup-restore', { method: 'POST', body: { name: row.name } });
              toast(t('dbRestoring'), 'warn', { sticky: true });
              supaReloadSoon();
            } catch (error) { toast(error.message, 'error'); }
          }),
          button('📥 ' + t('dbDownload'), () => downloadBackup(row.name)),
          button('📤 ' + t('dbSendTg'), async () => {
            sendDraft.name = row.name;
            try {
              const result = await api('/agent/backup-send', {
                method: 'POST',
                body: { name: row.name, target: sendDraft.target, channel: sendDraft.channel },
              });
              toast(`${t('tgTestSent')} · ${t('tgCountSent')} ${result.sent || 0}`,
                result.sent ? 'ok' : 'error');
            } catch (error) { toast(error.message, 'error'); }
          }),
          button('🗑 ' + t('dbDelete'), async () => {
            if (!window.confirm(t('dbDeleteConfirm'))) return;
            try {
              await api('/agent/backup-delete', { method: 'POST', body: { name: row.name } });
              renderDatabase();
            } catch (error) { toast(error.message, 'error'); }
          }, { class: 'mas-btn mas-danger' }),
        ]),
      ])) : el('p', { class: 'mas-hint', text: t('dbListEmpty') }),
    el('div', { class: 'mas-row mas-wrap' }, [
      button('📦 ' + t('dbTake'), async () => {
        try {
          const info = await api('/agent/backups', { method: 'POST', body: {} });
          toast(`${t('dbTakenOk')} · ${info.name} · ${info.sizeText}`, 'ok');
          renderDatabase();
        } catch (error) { toast(error.message, 'error'); }
      }, { class: 'mas-btn mas-primary' }),
      button('📦 ' + t('dbTakeSecrets'), async () => {
        if (!window.confirm(t('dbSecretsWarn'))) return;
        try {
          const info = await api('/agent/backups', {
            method: 'POST', body: { includeSecrets: true },
          });
          toast(`${t('dbTakenOk')} · ${info.name}`, 'ok');
          renderDatabase();
        } catch (error) { toast(error.message, 'error'); }
      }, { class: 'mas-btn mas-danger' }),
    ]),
  ]);

  /* -- cloud backups (round 17): the Supabase snapshot lives HERE, next to
        the local backups, with the full version list and per-version
        restore. Configuration = agent settings (supabase.*, key is a
        SECRET); a fresh machine's boot restore reads the SUPABASE_* env. */
  const supaConn = el('input', { class: 'mas-input', dir: 'ltr', placeholder: t('supaConnPh') });
  const supaBucket = el('input', { class: 'mas-input', dir: 'ltr', placeholder: 'mas-snapshots' });
  const supaEvery = el('input', { class: 'mas-input', type: 'number', min: '1', max: '1440', placeholder: '10' });
  const supaStatus = el('p', { class: 'mas-hint', text: '…' });
  const supaHistoryHost = el('div', {});
  const supaRestore = async (name) => {
    if (!window.confirm(name ? t('supaConfirmVersion') : t('supaConfirmRestore'))) return;
    try {
      await api('/supabase/restore', { method: 'POST', body: name ? { name } : {} });
      toast(t('supaRestoring'), 'warn', { sticky: true });
      supaReloadSoon();
    } catch (error) { toast(error.message, 'error'); }
  };
  const loadSupa = async () => {
    let st;
    try {
      st = await api('/supabase/status');
    } catch (error) { supaStatus.textContent = error.message; return; }
    if (st.bucket) supaBucket.value = st.bucket;
    supaEvery.value = st.everyMin || 10;
    const bits = [];
    if (st.url) bits.push(`🔗 ${st.url}${st.key ? ` · ${st.key}` : ''}`);
    bits.push(`${t('supaState')}: ${st.enabled ? '🟢' : '⚪'}${st.source ? ` (${st.source})` : ''}`);
    if (st.lastSyncAt) bits.push(`${t('supaLast')}: ${tehranWhen(st.lastSyncAt)}`);
    if (st.lastError) bits.push(`❌ ${st.lastError}`);
    else if (st.lastResult && st.lastResult.uploaded) {
      bits.push(`✅ ${(st.lastResult.uploaded / 1024).toFixed(0)} KB`);
    }
    if (st.restoredAt) bits.push(`${t('supaRestored')}: ${tehranWhen(st.restoredAt)}`);
    supaStatus.textContent = bits.join(' · ');
    if (!st.configured) {
      replace(supaHistoryHost, el('p', { class: 'mas-hint', text: t('supaConnHint') }));
      return;
    }
    replace(supaHistoryHost, el('p', { class: 'mas-hint', text: '…' }));
    let rows = [];
    try {
      rows = ((await api('/supabase/history')).history) || [];
    } catch (error) {
      replace(supaHistoryHost, el('p', { class: 'mas-warn', text: error.message }));
      return;
    }
    replace(supaHistoryHost,
      el('b', { class: 'mas-subhead', text: `🗂 ${t('supaHistoryTitle')} (${rows.length})` }),
      el('p', { class: 'mas-hint', text: t('supaHistoryHint') }),
      rows.length ? agentTable(
        [t('dbColWhen'), t('dbColSize'), t('dbColActions')],
        rows.map((row) => [
          (row.createdAt
            ? tehranWhen(Math.floor(Date.parse(row.createdAt) / 1000)) : '')
            || row.name,
          `${((row.size || 0) / 1024).toFixed(0)} KB`,
          el('span', { class: 'mas-row' }, [
            button('♻️ ' + t('supaRestoreVersion'), () => supaRestore(row.name)),
          ]),
        ])) : el('p', { class: 'mas-hint', text: t('supaHistoryEmpty') }));
  };
  const supaCard = el('div', { class: 'mas-box mas-card' }, [
    el('div', { class: 'mas-card-head' }, [el('b', { text: '☁️ ' + t('supaTitle') })]),
    el('p', { class: 'mas-hint', text: t('supaHint') }),
    el('p', { class: 'mas-hint', text: t('supaConnHint') }),
    el('div', { class: 'mas-row mas-wrap' }, [
      supaConn,
      el('button', { class: 'mas-btn mas-primary', type: 'button', text: '🔗 ' + t('supaConnectBtn'), onClick: async () => {
        const value = supaConn.value.trim();
        if (!value) { toast(t('supaConnEmpty'), 'warn'); return; }
        try {
          const res = await api('/supabase/connect', { method: 'POST', body: {
            connection: value,
            bucket: supaBucket.value.trim() || 'mas-snapshots',
            everyMin: parseInt(supaEvery.value, 10) || 10,
          } });
          supaConn.value = '';
          if (res.restarting) {
            toast(t('supaRestoring'), 'warn', { sticky: true });
            supaReloadSoon();
          } else {
            toast(res.restored ? t('supaConnectedRestore') : t('supaConnected'), 'ok');
            loadSupa();
          }
        } catch (error) { toast(error.message, 'error'); }
      } }),
    ]),
    el('div', { class: 'mas-row mas-wrap' }, [supaBucket, supaEvery]),
    supaStatus,
    el('div', { class: 'mas-row mas-wrap' }, [
      el('button', { class: 'mas-btn mas-primary', type: 'button', text: '📤 ' + t('supaBackup'), onClick: async () => {
        try {
          const res = await api('/supabase/sync', { method: 'POST', body: { force: true } });
          toast(`${t('supaBackupDone')} (${((res.result || {}).uploaded / 1024 || 0).toFixed(0)} KB)`, 'ok');
          loadSupa();
        } catch (error) { toast(error.message, 'error'); }
      } }),
      el('button', { class: 'mas-btn', type: 'button', text: '📥 ' + t('supaRestore'), onClick: () => supaRestore('') }),
      el('button', { class: 'mas-btn', type: 'button', text: '💾 ' + t('supaSave'), onClick: async () => {
        const body = {
          'supabase.bucket': supaBucket.value.trim() || 'mas-snapshots',
          'supabase.enabled': true,
          'supabase.everyMin': parseInt(supaEvery.value, 10) || 10,
        };
        try {
          await api('/agent/settings', { method: 'POST', body });
          toast(t('supaSaved'), 'ok');
          loadSupa();
        } catch (error) { toast(error.message, 'error'); }
      } }),
    ]),
    supaHistoryHost,
  ]);
  loadSupa();

  const autoCard = el('div', { class: 'mas-box mas-card' }, [
    el('div', { class: 'mas-card-head' }, [el('b', { text: '⏰ ' + t('dbAutoTitle') })]),
    el('p', { class: 'mas-hint', text: t('dbAutoHint') }),
    el('label', { class: 'mas-check' }, [
      el('input', {
        type: 'checkbox', checked: draft.enabled,
        onChange: (event) => { draft.enabled = event.target.checked; },
      }),
      el('span', { text: t('dbJobOn') }),
    ]),
    el('div', { class: 'mas-row mas-wrap' }, [
      agentField(t('dbScheduleKind'), agentSelect([
        { value: 'cron', label: t('dbKindCron') },
        { value: 'every', label: t('dbKindEvery') },
        { value: 'at', label: t('dbKindAt') },
      ], draft.kind, (event) => { draft.kind = event.target.value; })),
      agentField(t('dbSchedule'), el('input', {
        class: 'mas-input', value: draft.schedule, placeholder: '0 3 * * *',
        onInput: (event) => { draft.schedule = event.target.value; },
      })),
      agentField(t('dbKeep'), el('input', {
        class: 'mas-input', type: 'number', min: '1', max: '200', value: String(draft.keep),
        onInput: (event) => { draft.keep = Number(event.target.value) || 7; },
      })),
    ]),
    el('p', { class: 'mas-hint', text: draft.kind === 'cron' ? t('dbCronHint')
      : (draft.kind === 'every' ? t('dbEveryHint') : t('dbAtHint')) }),
    agentField('🎯 ' + t('dbTarget'), el('input', {
      class: 'mas-input', value: draft.target, placeholder: '@mychannel یا -100…',
      onInput: (event) => { draft.target = event.target.value; },
    })),
    el('p', { class: 'mas-hint', text: t('dbTargetHint') }),
    el('div', { class: 'mas-row mas-wrap' }, [
      agentField(t('tgTestChannel'), agentSelect([
        { value: '', label: t('tgChannelAuto') },
        { value: 'bot', label: t('tgChannelBot') },
        { value: 'account', label: t('tgChannelAccount') },
      ], draft.channel, (event) => { draft.channel = event.target.value; })),
      el('label', { class: 'mas-check' }, [
        el('input', {
          type: 'checkbox', checked: draft.notify,
          onChange: (event) => { draft.notify = event.target.checked; },
        }),
        el('span', { text: t('dbNotify') }),
      ]),
    ]),
    el('div', { class: 'mas-row mas-wrap' }, [
      button('💾 ' + t('dbSave'), async () => {
        try {
          await api('/agent/backup-config', {
            method: 'POST',
            body: {
              enabled: draft.enabled, scheduleKind: draft.kind, schedule: draft.schedule,
              keep: draft.keep, telegramTarget: draft.target,
              telegramChannel: draft.channel, notify: draft.notify,
            },
          });
          toast(t('agentSaved'), 'ok');
          renderDatabase();
        } catch (error) { toast(error.message, 'error'); }
      }, { class: 'mas-btn mas-primary' }),
    ]),
    config.lastJobStatus ? el('p', {
      class: config.lastJobStatus === 'done' ? 'mas-hint' : 'mas-warn',
      text: `${t('dbLastJob')}: ${config.lastJobStatus}${config.lastJobError ? ' · ' + config.lastJobError : ''}`,
    }) : null,
  ]);

  const sendCard = el('div', { class: 'mas-box mas-card' }, [
    el('div', { class: 'mas-card-head' }, [el('b', { text: '📤 ' + t('dbSendTitle') })]),
    el('p', { class: 'mas-hint', text: t('dbSendHint') }),
    el('div', { class: 'mas-row mas-wrap' }, [
      agentField(t('dbColName'), agentSelect([
        { value: '', label: t('dbNewest') },
      ].concat(backups.map((row) => ({ value: row.name, label: row.name }))),
      '', (event) => { sendDraft.name = event.target.value; })),
      agentField('🎯 ' + t('dbTarget'), el('input', {
        class: 'mas-input', value: sendDraft.target,
        onInput: (event) => { sendDraft.target = event.target.value; },
      })),
    ]),
    button('📤 ' + t('dbSendNow'), async () => {
      try {
        const result = await api('/agent/backup-send', {
          method: 'POST',
          body: { name: sendDraft.name, target: sendDraft.target, channel: sendDraft.channel },
        });
        toast(`${t('tgTestSent')} · ${t('tgCountSent')} ${result.sent || 0}`,
          result.sent ? 'ok' : 'error');
      } catch (error) { toast(error.message, 'error'); }
    }, { class: 'mas-btn mas-primary' }),
  ]);

  replace(pane, statsCard, listCard, supaCard, autoCard, sendCard);
}

/* ------------------------------------------------------------------ *
 * Agent sub-pane: everything Chrome
 * ------------------------------------------------------------------ */

async function renderAgentBrowser(host) {
  replace(host, el('p', { class: 'mas-empty', text: '…' }));
  let data;
  try {
    data = await api('/agent/browser');
  } catch (error) { return agentError(host, error); }
  const tabs = (data.tabs && data.tabs.tabs) || [];
  const version = data.version || {};
  const history = data.history || {};
  const visits = history.visits || [];
  const closed = data.closedTabs || [];
  const filter = { q: '', days: 0 };

  const headCard = el('div', { class: 'mas-box mas-card' }, [
    el('div', { class: 'mas-card-head' }, [
      el('b', { text: '🌐 ' + t('brTitle') }),
      el('span', { class: 'mas-chip', text: version.ok
        ? String(version.Browser || version['Browser'] || '') : t('brOffline') }),
      el('span', { class: 'mas-chip', text: `${tabs.length} ${t('brTabsOpen')}` }),
      button('🔄', () => renderAgentBrowser(host), { class: 'mas-btn' }),
    ]),
    el('p', { class: 'mas-hint', text: t('brHint') }),
    el('p', { class: 'mas-hint', text: `${t('brProfile')}: ${(data.profile || {}).fa || ''} · ${(data.profile || {}).dir || ''}` }),
  ]);

  const actOnTab = async (action, id, url) => {
    try {
      await api('/agent/browser/tab', { method: 'POST', body: { action, id, url } });
      renderAgentBrowser(host);
    } catch (error) { toast(error.message, 'error'); }
  };
  const newUrl = { value: 'https://chat.deepseek.com/' };
  const tabsCard = el('div', { class: 'mas-box mas-card' }, [
    el('div', { class: 'mas-card-head' }, [el('b', { text: '🗂 ' + t('brTabsTitle') })]),
    tabs.length ? agentTable([t('brColTab'), t('brColUrl'), t('dbColActions')],
      tabs.map((row) => [
        row.title || row.id, row.url,
        el('span', { class: 'mas-row' }, [
          button('👁 ' + t('brActivate'), () => actOnTab('activate', row.id, '')),
          button('✖ ' + t('brClose'), () => actOnTab('close', row.id, ''),
            { class: 'mas-btn mas-danger' }),
        ]),
      ])) : el('p', { class: 'mas-hint', text: t('brTabsNone') }),
    el('div', { class: 'mas-row mas-wrap' }, [
      agentField(t('brNewUrl'), el('input', {
        class: 'mas-input', value: newUrl.value,
        onInput: (event) => { newUrl.value = event.target.value; },
      })),
      button('🌐 ' + t('brOpen'), () => actOnTab('new', '', newUrl.value),
        { class: 'mas-btn mas-primary' }),
    ]),
  ]);

  const reloadHistory = async () => {
    let fresh = history;
    try {
      fresh = await api(`/agent/browser/history?q=${encodeURIComponent(filter.q)}&days=${filter.days}`);
    } catch (error) { toast(error.message, 'error'); return; }
    const rows = (fresh.visits || []).map((row) => [
      row.timeText || tehranWhen(row.at), row.title || row.url,
      String(row.visitCount || 0), row.transition || '',
    ]);
    replace(histBody, rows.length
      ? agentTable([t('brColWhen'), t('brColPage'), t('brColVisits'), t('brColHow')], rows)
      : el('p', { class: 'mas-hint', text: t('brHistNone') }));
  };
  const histBody = el('div', { class: 'mas-history-body' });
  const histCard = el('div', { class: 'mas-box mas-card' }, [
    el('div', { class: 'mas-card-head' }, [
      el('b', { text: '🕘 ' + t('brHistory') }),
      el('span', { class: 'mas-chip', text: `${(history.totals || {}).visits || 0} ${t('brVisitsCount')}` }),
    ]),
    el('div', { class: 'mas-row mas-wrap' }, [
      agentField(t('brFilter'), el('input', {
        class: 'mas-input', value: '', placeholder: t('brFilterHint'),
        onInput: (event) => { filter.q = event.target.value; },
        onChange: () => reloadHistory(),
      })),
      agentField(t('brDays'), agentSelect([
        { value: '0', label: t('brDaysAll') },
        { value: '1', label: t('brDays1') },
        { value: '7', label: t('brDays7') },
        { value: '30', label: t('brDays30') },
      ], '0', (event) => { filter.days = Number(event.target.value) || 0; reloadHistory(); })),
      button('🔄 ' + t('brRefreshTabs'), async () => {
        try {
          await api('/agent/browser/tabs');
          renderAgentBrowser(host);
        } catch (error) { toast(error.message, 'error'); }
      }),
    ]),
    histBody,
    (history.searches || []).length ? el('div', { class: 'mas-row mas-wrap mas-chips' },
      (history.searches || []).slice(0, 12).map((row) => el('span', {
        class: 'mas-chip', text: `🔎 ${row.term}`,
      }))) : null,
    (history.downloads || []).length ? el('div', {}, [
      el('b', { class: 'mas-subhead', text: '📥 ' + t('brDownloads') }),
      agentTable([t('brColWhen'), t('brColFile'), t('dbColSize'), t('tgColStatus')],
        (history.downloads || []).slice(0, 20).map((row) => [
          row.timeText || tehranWhen(row.at), row.path || row.url,
          String(row.bytes || 0), row.state || '',
        ])),
    ]) : null,
  ]);
  reloadHistory();

  const closedCard = el('div', { class: 'mas-box mas-card' }, [
    el('div', { class: 'mas-card-head' }, [el('b', { text: ' ' + t('brClosed') })]),
    el('p', { class: 'mas-hint', text: t('brClosedHint') }),
    closed.length ? agentTable([t('brColWhen'), t('brColPage')],
      closed.slice(0, 30).map((row) => [row.timeText || tehranWhen(row.at),
        row.title || row.url]))
      : el('p', { class: 'mas-hint', text: t('brClosedNone') }),
  ]);

  replace(host, headCard, tabsCard, histCard, closedCard);
}

/* ------------------------------------------------------------------ *
 * Theme: light/dark, chosen in the header, remembered in the database
 * ------------------------------------------------------------------ */

function applyTheme(mode, { save = false } = {}) {
  state.theme = mode === 'dark' ? 'dark' : 'light';
  document.documentElement.dataset.theme = state.theme;
  try { localStorage.setItem(LS.theme, state.theme); } catch (_) { /* private mode */ }
  const label = state.theme === 'dark' ? '☀️ ' + t('themeLight') : '🌙 ' + t('themeDark');
  for (const id of ['mas-theme-btn', 'mas-theme-toggle']) {
    const node = document.getElementById(id);
    if (node) node.textContent = label;
  }
  if (save) saveUiPref('theme', state.theme);
}

function cycleTheme() {
  applyTheme(state.theme === 'dark' ? 'light' : 'dark', { save: true });
  const settings = document.getElementById('mas-tab-settings');
  if (settings && !settings.hidden) renderSettings();
}

/* ------------------------------------------------------------------ *
 * Settings tab: theme, the clipboard bridge, the devlog, system info
 * ------------------------------------------------------------------ */

async function renderSettings() {
  const pane = document.getElementById('mas-tab-settings');
  if (!pane) return;
  const dbTab = document.querySelector('.mas-tab[data-tab="db"]');
  const hasAgent = !(dbTab && dbTab.hidden);

  const themeCard = el('div', { class: 'mas-box mas-card' }, [
    el('div', { class: 'mas-card-head' }, [el('b', { text: '🎨 ' + t('settingsThemeTitle') })]),
    el('p', { class: 'mas-hint', text: t('settingsThemeHint') }),
    el('div', { class: 'mas-row mas-wrap' }, ['light', 'dark'].map((mode) => el('button', {
      class: 'mas-btn' + (state.theme === mode ? ' mas-primary' : ''), type: 'button',
      text: mode === 'dark' ? '🌙 ' + t('settingsThemeDark') : '☀️ ' + t('settingsThemeLight'),
      onClick: () => { applyTheme(mode, { save: true }); renderSettings(); },
    }))),
  ]);

  const clipSyncCard = el('div', { class: 'mas-box mas-card' }, [
    el('div', { class: 'mas-card-head' }, [el('b', { text: '🔄 ' + t('clipSyncTitle') })]),
    el('p', { class: 'mas-hint', text: t('clipSyncHint') }),
    el('div', { class: 'mas-row mas-wrap' }, [
      button(state.clipSyncOn ? '🟢 ' + t('clipSyncOn') : '⚪ ' + t('clipSyncOff'),
        async () => {
          state.clipSyncOn = !state.clipSyncOn;
          localStorage.setItem(LS.clip, state.clipSyncOn ? '1' : '0');
          if (state.clipSyncOn && navigator.clipboard && navigator.clipboard.readText) {
            // The click is the user gesture Chrome wants before readText.
            try { state.clipLastLocal = await navigator.clipboard.readText(); } catch (err) { /* denied */ }
          }
          startClipSync();
          rerenderAll();
          selectTab('settings');
        }, { class: 'mas-btn' + (state.clipSyncOn ? ' mas-primary' : '') }),
    ]),
  ]);

  const langCard = el('div', { class: 'mas-box mas-card' }, [
    el('div', { class: 'mas-card-head' }, [el('b', { text: '🌐 ' + t('settingsLangTitle') })]),
    el('p', { class: 'mas-hint', text: t('settingsLangHint') }),
    el('div', { class: 'mas-row mas-wrap' }, [['fa', 'FA'], ['en', 'EN']].map(([code, label]) => el('button', {
      class: 'mas-btn' + (state.lang === code ? ' mas-primary' : ''), type: 'button',
      text: label,
      onClick: () => {
        state.lang = code;
        localStorage.setItem(LS.lang, code);
        saveUiPref('lang', code);
        rerenderAll();
        selectTab('settings');
      },
    }))),
  ]);

  const clipText = el('textarea', {
    id: 'mas-clip-text', class: 'mas-input mas-mono', rows: 5,
    placeholder: t('settingsClipPh'),
  });
  const clipCard = el('div', { class: 'mas-box mas-card' }, [
    el('div', { class: 'mas-card-head' }, [el('b', { text: '📋 ' + t('settingsClipTitle') })]),
    el('p', { class: 'mas-hint', text: t('settingsClipHint') }),
    clipText,
    el('div', { class: 'mas-row mas-wrap' }, [
      el('button', { class: 'mas-btn mas-primary', type: 'button', text: '📤 ' + t('settingsClipWrite'), onClick: async () => {
        try { await api('/clipboard', { method: 'POST', body: { text: clipText.value } }); toast(t('settingsClipDone'), 'ok'); }
        catch (error) { toast(error.message, 'error'); }
      } }),
      el('button', { class: 'mas-btn', type: 'button', text: '📥 ' + t('settingsClipRead'), onClick: async () => {
        try { clipText.value = (await api('/clipboard')).text || ''; }
        catch (error) { toast(error.message, 'error'); }
      } }),
      el('button', { class: 'mas-btn', type: 'button', text: '🖥 ' + t('settingsClipLocalWrite'), onClick: async () => {
        try { await navigator.clipboard.writeText(clipText.value); toast(t('settingsClipDone'), 'ok'); }
        catch (error) { toast(t('settingsClipFail'), 'error'); }
      } }),
      el('button', { class: 'mas-btn', type: 'button', text: '⬅ ' + t('settingsClipLocalRead'), onClick: async () => {
        try { clipText.value = await navigator.clipboard.readText(); }
        catch (error) { toast(t('settingsClipFail'), 'error'); }
      } }),
    ]),
  ]);

  const devlogList = el('div', { id: 'mas-devlog-list' }, [el('p', { class: 'mas-hint', text: '…' })]);
  const devTitle = el('input', { class: 'mas-input', placeholder: t('settingsDevlogTitlePh') });
  const devBody = el('textarea', { class: 'mas-input', rows: 4, placeholder: t('settingsDevlogBodyPh') });
  const loadDevlog = async () => {
    try {
      const entries = (await api('/agent/devlog')).entries || [];
      replace(devlogList, entries.length ? entries.map((entry) => el('details', { class: 'mas-devlog-item' }, [
        el('summary', {}, [
          el('b', { text: entry.title }), ' ',
          el('span', { class: 'mas-hint', text: tehranWhen(entry.at) + (entry.actor ? ' · ' + entry.actor : '') }),
        ]),
        el('pre', { class: 'mas-pre mas-mono', text: entry.body || '' }),
      ])) : [el('p', { class: 'mas-hint', text: t('settingsDevlogEmpty') })]);
    } catch (error) { replace(devlogList, el('p', { class: 'mas-hint', text: error.message })); }
  };
  const devlogCard = hasAgent ? el('div', { class: 'mas-box mas-card' }, [
    el('div', { class: 'mas-card-head' }, [el('b', { text: '📔 ' + t('settingsDevlogTitle') })]),
    el('p', { class: 'mas-hint', text: t('settingsDevlogHint') }),
    devlogList,
    el('div', { class: 'mas-row mas-wrap' }, [
      devTitle,
      el('button', { class: 'mas-btn mas-primary', type: 'button', text: '＋ ' + t('settingsDevlogAdd'), onClick: async () => {
        try {
          await api('/agent/devlog', { method: 'POST', body: { title: devTitle.value, body: devBody.value } });
          devTitle.value = ''; devBody.value = '';
          toast(t('settingsDevlogSaved'), 'ok');
          loadDevlog();
        } catch (error) { toast(error.message, 'error'); }
      } }),
    ]),
    devBody,
  ]) : null;

  const infoBody = el('div', {}, [el('p', { class: 'mas-hint', text: '…' })]);
  (async () => {
    try {
      const info = await api('/info');
      replace(infoBody, el('div', { class: 'mas-row mas-wrap mas-chips' }, [
        el('span', { class: 'mas-chip', text: `${t('settingsInfoVersion')}: ${info.version || '?'}` }),
        el('span', { class: 'mas-chip', text: `${t('settingsInfoViewport')}: ${(info.viewport || {}).width || '?'}×${(info.viewport || {}).height || '?'}` }),
        el('span', { class: 'mas-chip', text: `${t('settingsInfoServerTime')}: ${tehranWhen(info.serverTime)}` }),
      ]));
    } catch (error) { replace(infoBody, el('p', { class: 'mas-hint', text: error.message })); }
  })();
  const infoCard = el('div', { class: 'mas-box mas-card' }, [
    el('div', { class: 'mas-card-head' }, [el('b', { text: 'ℹ️ ' + t('settingsInfoTitle') })]),
    infoBody,
    el('p', { class: 'mas-hint', text: t('settingsInfoDesktopHint') }),
    el('div', { class: 'mas-row mas-wrap' }, [
      el('button', { class: 'mas-btn', type: 'button', text: '✉️ ' + t('settingsGoTelegram'), onClick: () => { selectTab('agent'); selectAgentSub('telegram'); state.tgSub = 'settings'; saveUiPref('tgSub', 'settings'); } }),
      el('button', { class: 'mas-btn', type: 'button', text: '🗄 ' + t('settingsGoBackup'), onClick: () => selectTab('db') }),
      el('button', { class: 'mas-btn', type: 'button', text: '🔑 ' + t('settingsGoKey'), onClick: () => { selectTab('agent'); selectAgentSub('agentkey'); } }),
    ]),
  ]);

  replace(pane, themeCard, langCard, clipSyncCard, clipCard, devlogCard, infoCard);
  if (hasAgent) loadDevlog();
}

/* ------------------------------------------------------------------ *
 * Dedicated pages: any HTML the agent builds, kept in the database
 * ------------------------------------------------------------------ */

const pageDraft = { id: null };

async function renderAgentPages(host) {
  replace(host, el('p', { class: 'mas-empty', text: '…' }));
  let pages = [];
  try {
    pages = (await api('/agent/pages')).pages || [];
  } catch (error) { return agentError(host, error); }

  const titleInput = el('input', { class: 'mas-input', placeholder: t('pagesTitlePh') });
  const htmlInput = el('textarea', { class: 'mas-input mas-mono', rows: 14, placeholder: t('pagesHtmlPh') });
  const preview = el('iframe', {
    class: 'mas-page-preview', title: 'preview',
    sandbox: 'allow-scripts allow-forms allow-modals allow-popups',
  });
  const showPreview = () => { preview.srcdoc = htmlInput.value || ''; };

  const editPage = async (id) => {
    try {
      const page = await api('/agent/pages?id=' + encodeURIComponent(id));
      pageDraft.id = page.id;
      titleInput.value = page.title || '';
      htmlInput.value = page.html || '';
      showPreview();
      toast(t('pagesLoaded'), 'ok');
    } catch (error) { toast(error.message, 'error'); }
  };
  const save = async () => {
    try {
      const body = { title: titleInput.value, html: htmlInput.value };
      if (pageDraft.id) body.id = pageDraft.id;
      const result = await api('/agent/pages', { method: 'POST', body });
      pageDraft.id = result.page.id;
      showPreview();
      toast(t('pagesSaved'), 'ok');
      renderAgentSub('pages');
    } catch (error) { toast(error.message, 'error'); }
  };
  const grabFromReply = () => {
    const reply = document.getElementById('mas-ai-reply');
    const text = reply ? reply.value : '';
    const fenced = /```html\s*\n([\s\S]*?)```/i.exec(text);
    const whole = /<!doctype html>[\s\S]*<\/html>/i.exec(text);
    const html = fenced ? fenced[1] : (whole ? whole[0] : '');
    if (!html) { toast(t('pagesNoHtml'), 'error'); return; }
    htmlInput.value = html;
    if (!titleInput.value) titleInput.value = t('pagesUntitled');
    showPreview();
    toast(t('pagesGrabbed'), 'ok');
  };
  const remove = async (id) => {
    if (!window.confirm(t('pagesDeleteConfirm'))) return;
    try {
      await api('/agent/page-delete', { method: 'POST', body: { id } });
      toast(t('pagesDeleted'), 'ok');
      renderAgentSub('pages');
    } catch (error) { toast(error.message, 'error'); }
  };

  const editorCard = el('div', { class: 'mas-box mas-card' }, [
    el('div', { class: 'mas-card-head' }, [el('b', { text: '📝 ' + t('pagesEditor') })]),
    el('p', { class: 'mas-hint', text: t('pagesHint') }),
    titleInput,
    htmlInput,
    el('div', { class: 'mas-row mas-wrap' }, [
      el('button', { class: 'mas-btn mas-primary', type: 'button', text: '💾 ' + t('pagesSave'), onClick: save }),
      el('button', { class: 'mas-btn', type: 'button', text: '🔄 ' + t('pagesPreview'), onClick: showPreview }),
      el('button', { class: 'mas-btn', type: 'button', text: '📥 ' + t('pagesGrab'), onClick: grabFromReply }),
      el('button', { class: 'mas-btn', type: 'button', text: '🧹 ' + t('pagesNew'), onClick: () => {
        pageDraft.id = null; titleInput.value = ''; htmlInput.value = ''; preview.srcdoc = '';
      } }),
    ]),
  ]);
  const previewCard = el('div', { class: 'mas-box mas-card' }, [
    el('div', { class: 'mas-card-head' }, [el('b', { text: '🖼 ' + t('pagesPreviewTitle') })]),
    el('p', { class: 'mas-hint', text: t('pagesSandboxHint') }),
    preview,
  ]);
  const listCard = el('div', { class: 'mas-box mas-card' }, [
    el('div', { class: 'mas-card-head' }, [el('b', { text: `📚 ${t('pagesListTitle')} (${pages.length})` })]),
    pages.length ? agentTable(
      [t('pagesColTitle'), t('dbColSize'), t('pagesColUpdated'), t('pagesColAction')],
      pages.map((page) => [
        page.title || t('pagesUntitled'),
        String(page.size || 0),
        tehranWhen(page.updated_at),
        el('span', { class: 'mas-row' }, [
          el('button', { class: 'mas-btn mas-mini', type: 'button', text: t('pagesOpen'), onClick: () => editPage(page.id) }),
          el('button', { class: 'mas-btn mas-mini mas-danger', type: 'button', text: t('pagesDelete'), onClick: () => remove(page.id) }),
        ]),
      ])) : el('p', { class: 'mas-hint', text: t('pagesEmpty') }),
  ]);

  replace(host, editorCard, previewCard, listCard);
}

/* ------------------------------------------------------------------ *
 * Agent suggestions: structured proposals behind human gates
 * ------------------------------------------------------------------ */

const SUGG_KINDS = ['operation', 'flow', 'settings', 'ui', 'db', 'bug', 'security', 'feature'];
const SUGG_RISKS = ['low', 'medium', 'high'];
const SUGG_STATUSES = ['draft', 'pending', 'approved', 'rejected', 'applied', 'failed'];
const SUGG_CHIP = { applied: 'mas-chip-ok', approved: 'mas-chip-ok', rejected: 'mas-chip-bad',
  failed: 'mas-chip-bad', pending: 'mas-chip-warn', draft: '', archived: '' };
const suggState = { status: '', kind: '', risk: '', q: '', open: null, form: false, editing: false };
const suggLabel = (prefix, value) => t(prefix + value.charAt(0).toUpperCase() + value.slice(1));

async function renderAgentSuggestions(host) {
  replace(host, el('p', { class: 'mas-empty', text: '…' }));

  if (suggState.open) {
    let one;
    try {
      one = (await api('/agent/suggestions?id=' + encodeURIComponent(suggState.open))).suggestion;
    } catch (error) {
      suggState.open = null;
      return agentError(host, error);
    }
    const pretty = (raw) => {
      try { return JSON.stringify(JSON.parse(raw), null, 2); } catch (_) { return raw || ''; }
    };
    const editFields = {
      problem: el('textarea', { class: 'mas-input', rows: 3 }),
      proposal: el('textarea', { class: 'mas-input', rows: 3 }),
      reason: el('textarea', { class: 'mas-input', rows: 2 }),
      before_state: el('textarea', { class: 'mas-input mas-mono', rows: 4 }),
      after_state: el('textarea', { class: 'mas-input mas-mono', rows: 4 }),
    };
    const fill = () => {
      for (const [name, node] of Object.entries(editFields)) node.value = one[name] || '';
    };
    const editor = el('div', { id: 'mas-sugg-edit', hidden: !suggState.editing }, Object.entries(editFields).map(
      ([name, node]) => agentField(suggLabel('sugg', name.replace(/_(\w)/g, (m, c) => c.toUpperCase())), node)));
    fill();
    const done = (message) => { toast(message, 'ok'); renderAgentSub('suggestions'); };
    const fail = (error) => toast(error.message, 'error');
    const detailCard = el('div', { class: 'mas-box mas-card' }, [
      el('div', { class: 'mas-card-head' }, [
        el('b', { text: `💡 #${one.id} — ${one.title || ''}` }),
        el('span', { class: 'mas-chip ' + (SUGG_CHIP[one.status] || ''), text: suggLabel('suggStatus', one.status) }),
        el('span', { class: 'mas-chip', text: suggLabel('suggRisk', one.risk || 'low') }),
        el('span', { class: 'mas-chip', text: suggLabel('suggKind', one.kind || 'feature') }),
      ]),
      el('p', { class: 'mas-hint', text: `${t('suggSection')}: ${one.section || '—'} · ${tehranWhen(one.updated_at)}` }),
      el('b', { class: 'mas-subhead', text: t('suggProblem') }),
      el('p', { text: one.problem || '—' }),
      el('b', { class: 'mas-subhead', text: t('suggProposal') }),
      el('p', { text: one.proposal || '—' }),
      el('b', { class: 'mas-subhead', text: t('suggReason') }),
      el('p', { text: one.reason || '—' }),
      one.evidence ? el('div', {}, [el('b', { class: 'mas-subhead', text: t('suggEvidence') }),
        el('pre', { class: 'mas-pre mas-mono', text: one.evidence })]) : null,
      el('div', { class: 'mas-row mas-wrap mas-diff' }, [
        el('div', { class: 'mas-diff-col' }, [el('b', { class: 'mas-subhead', text: t('suggBefore') }),
          el('pre', { class: 'mas-pre mas-mono', text: pretty(one.before_state) })]),
        el('div', { class: 'mas-diff-col' }, [el('b', { class: 'mas-subhead', text: t('suggAfter') }),
          el('pre', { class: 'mas-pre mas-mono', text: pretty(one.after_state) })]),
      ]),
      one.apply_result ? el('p', { class: 'mas-hint', text: `${t('suggApplyResult')}: ${one.apply_result}` }) : null,
      one.rollback_note ? el('p', { class: 'mas-hint', text: `${t('suggRollbackNote')}: ${one.rollback_note}` }) : null,
      editor,
      el('div', { class: 'mas-row mas-wrap' }, [
        el('button', { class: 'mas-btn', type: 'button', text: '→ ' + t('suggBack'), onClick: () => { suggState.open = null; suggState.editing = false; rerender(); } }),
        el('button', { class: 'mas-btn mas-primary', type: 'button', text: '✔ ' + t('suggApprove'), onClick: () => api('/agent/suggestion-decision', { method: 'POST', body: { id: one.id, decision: 'approve' } }).then(() => done(t('suggDecided'))).catch(fail) }),
        el('button', { class: 'mas-btn mas-danger', type: 'button', text: '✖ ' + t('suggReject'), onClick: () => api('/agent/suggestion-decision', { method: 'POST', body: { id: one.id, decision: 'reject' } }).then(() => done(t('suggDecided'))).catch(fail) }),
        el('button', { class: 'mas-btn', type: 'button', text: '↩ ' + t('suggRequestChanges'), onClick: () => api('/agent/suggestion-decision', { method: 'POST', body: { id: one.id, decision: 'request_changes' } }).then(() => done(t('suggDecided'))).catch(fail) }),
        el('button', { class: 'mas-btn mas-primary', type: 'button', text: '⚙ ' + t('suggApply'), onClick: () => {
          if (!window.confirm(t('suggApplyConfirm'))) return;
          api('/agent/suggestion-apply', { method: 'POST', body: { id: one.id } })
            .then(() => done(t('suggApplied'))).catch(fail);
        } }),
        el('button', { class: 'mas-btn', type: 'button', text: '⏪ ' + t('suggRollback'), onClick: () => api('/agent/suggestion-rollback', { method: 'POST', body: { id: one.id } }).then(() => done(t('suggRolledBack'))).catch(fail) }),
        el('button', { class: 'mas-btn mas-danger', type: 'button', text: '🗑 ' + t('suggArchive'), onClick: () => {
          if (!window.confirm(t('suggArchiveConfirm'))) return;
          api('/agent/suggestion-delete', { method: 'POST', body: { id: one.id } })
            .then(() => done(t('suggArchived'))).catch(fail);
        } }),
        el('button', { class: 'mas-btn', type: 'button', text: '✏ ' + t('suggEdit'), onClick: () => {
          suggState.editing = !suggState.editing;
          editor.hidden = !suggState.editing;
        } }),
        el('button', { class: 'mas-btn mas-primary', type: 'button', text: '💾 ' + t('suggSaveEdit'), onClick: async () => {
          const body = { id: one.id };
          for (const [name, node] of Object.entries(editFields)) body[name] = node.value;
          try { await api('/agent/suggestion-update', { method: 'POST', body }); toast(t('suggSavedEdit'), 'ok'); rerender(); }
          catch (error) { toast(error.message, 'error'); }
        } }),
      ]),
      el('p', { class: 'mas-hint', text: t('suggOperatorOnly') }),
    ]);
    replace(host, detailCard);
    return;
  }

  const qs = [];
  if (suggState.status) qs.push('status=' + encodeURIComponent(suggState.status));
  if (suggState.kind) qs.push('kind=' + encodeURIComponent(suggState.kind));
  if (suggState.risk) qs.push('risk=' + encodeURIComponent(suggState.risk));
  if (suggState.q) qs.push('q=' + encodeURIComponent(suggState.q));
  let data;
  try {
    data = await api('/agent/suggestions' + (qs.length ? '?' + qs.join('&') : ''));
  } catch (error) { return agentError(host, error); }
  const rows = data.suggestions || [];
  const counts = data.counts || {};

  const chips = el('div', { class: 'mas-row mas-wrap mas-chips' },
    SUGG_STATUSES.map((status) => el('span', {
      class: 'mas-chip ' + (SUGG_CHIP[status] || ''),
      text: `${counts[status] || 0} ${suggLabel('suggStatus', status)}`,
    })));

  const qInput = el('input', { class: 'mas-input', placeholder: t('suggSearchPh'), value: suggState.q,
    onKeydown: (event) => { if (event.key === 'Enter') { suggState.q = qInput.value; renderAgentSub('suggestions'); } } });
  const filters = el('div', { class: 'mas-row mas-wrap' }, [
    agentSelect([{ value: '', label: t('suggAllStatuses') }]
      .concat(SUGG_STATUSES.map((s) => ({ value: s, label: suggLabel('suggStatus', s) }))),
    suggState.status, (event) => { suggState.status = event.target.value; renderAgentSub('suggestions'); }),
    agentSelect([{ value: '', label: t('suggAllKinds') }]
      .concat(SUGG_KINDS.map((k) => ({ value: k, label: suggLabel('suggKind', k) }))),
    suggState.kind, (event) => { suggState.kind = event.target.value; renderAgentSub('suggestions'); }),
    agentSelect([{ value: '', label: t('suggAllRisks') }]
      .concat(SUGG_RISKS.map((r) => ({ value: r, label: suggLabel('suggRisk', r) }))),
    suggState.risk, (event) => { suggState.risk = event.target.value; renderAgentSub('suggestions'); }),
    qInput,
    el('button', { class: 'mas-btn', type: 'button', text: '🔎', onClick: () => { suggState.q = qInput.value; renderAgentSub('suggestions'); } }),
    el('button', { class: 'mas-btn mas-primary', type: 'button', text: '＋ ' + t('suggNew'), onClick: () => { suggState.form = !suggState.form; renderAgentSub('suggestions'); } }),
  ]);

  const formFields = {
    title: el('input', { class: 'mas-input', placeholder: t('suggTitlePh') }),
    section: el('input', { class: 'mas-input', placeholder: t('suggSectionPh') }),
    kind: agentSelect(SUGG_KINDS.map((k) => ({ value: k, label: suggLabel('suggKind', k) })), 'feature'),
    risk: agentSelect(SUGG_RISKS.map((r) => ({ value: r, label: suggLabel('suggRisk', r) })), 'low'),
    problem: el('textarea', { class: 'mas-input', rows: 3, placeholder: t('suggProblem') }),
    proposal: el('textarea', { class: 'mas-input', rows: 3, placeholder: t('suggProposal') }),
    evidence: el('textarea', { class: 'mas-input mas-mono', rows: 2, placeholder: t('suggEvidence') }),
    before_state: el('textarea', { class: 'mas-input mas-mono', rows: 3, placeholder: t('suggBefore') }),
    after_state: el('textarea', { class: 'mas-input mas-mono', rows: 3, placeholder: t('suggAfter') }),
    reason: el('textarea', { class: 'mas-input', rows: 2, placeholder: t('suggReason') }),
    impact: el('textarea', { class: 'mas-input', rows: 2, placeholder: t('suggImpact') }),
  };
  const formCard = suggState.form ? el('div', { class: 'mas-box mas-card' }, [
    el('div', { class: 'mas-card-head' }, [el('b', { text: '🧾 ' + t('suggNew') })]),
    el('p', { class: 'mas-hint', text: t('suggFormHint') }),
    ...Object.entries(formFields).map(([name, node]) => agentField(
      name === 'title' ? t('suggColTitle') : name === 'section' ? t('suggSection')
        : name === 'kind' ? t('suggKind') : name === 'risk' ? t('suggRisk')
          : suggLabel('sugg', name.replace(/_(\w)/g, (m, c) => c.toUpperCase())), node)),
    el('button', { class: 'mas-btn mas-primary', type: 'button', text: '💾 ' + t('suggCreate'), onClick: async () => {
      const body = { needs_human: 1 };
      for (const [name, node] of Object.entries(formFields)) body[name] = node.value;
      try {
        const result = await api('/agent/suggestions', { method: 'POST', body });
        suggState.form = false;
        suggState.open = result.suggestion.id;
        toast(t('suggCreated'), 'ok');
        renderAgentSub('suggestions');
      } catch (error) { toast(error.message, 'error'); }
    } }),
  ]) : null;

  const listCard = el('div', { class: 'mas-box mas-card' }, [
    el('div', { class: 'mas-card-head' }, [el('b', { text: `🗂 ${t('suggListTitle')} (${rows.length})` })]),
    rows.length ? agentTable(
      ['#', t('suggColTitle'), t('suggColKind'), t('suggColRisk'), t('suggColStatus'), t('suggColUpdated'), ''],
      rows.map((row) => [
        String(row.id),
        row.title || '',
        suggLabel('suggKind', row.kind || 'feature'),
        suggLabel('suggRisk', row.risk || 'low'),
        el('span', { class: 'mas-chip ' + (SUGG_CHIP[row.status] || ''), text: suggLabel('suggStatus', row.status) }),
        tehranWhen(row.updated_at),
        el('button', { class: 'mas-btn mas-mini', type: 'button', text: t('suggDetail'), onClick: () => { suggState.open = row.id; renderAgentSub('suggestions'); } }),
      ])) : el('p', { class: 'mas-hint', text: t('suggEmpty') }),
  ]);

  replace(host,
    el('div', { class: 'mas-box mas-card' }, [
      el('div', { class: 'mas-card-head' }, [el('b', { text: '💡 ' + t('agentSubSuggestions') })]),
      el('p', { class: 'mas-hint', text: t('suggHint') }),
      chips,
      filters,
    ]),
    formCard,
    listCard);
}
