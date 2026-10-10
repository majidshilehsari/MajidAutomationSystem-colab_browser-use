/* Automation sidebar for the Colab browser.
 *
 * Loaded into noVNC's vnc.html by automation/server.py. It never touches
 * noVNC's own objects: it draws an overlay panel on the right, and every action
 * goes to the server API, which drives xdotool on the virtual display. That is
 * what lets a flow keep running after this tab is closed.
 */

import {
  API_PREFIX, STEP_DEFS, createStep, labelFor, validateFlow, emptyFlow,
  mapToDesktop, keysFromText, keysToText, moveStep, extractJson,
  normaliseImportedFlow, promptSteps, translate, formatElapsed, formatTime,
  stepDef, truncate, structuredCloneSafe, compact, formatDateTime,
} from './core.mjs';

const LS = {
  token: 'mas.token', lang: 'mas.lang', flow: 'mas.flow', panel: 'mas.panelOpen',
  request: 'mas.userRequest', reply: 'mas.aiReply',
};

const state = {
  lang: localStorage.getItem(LS.lang) || 'fa',
  token: localStorage.getItem(LS.token) || '',
  flow: loadFlow(),
  info: null,
  status: { status: 'idle', entries: [] },
  pages: [],
  activePage: null,
  recording: false,
  captureKeys: true,
  openStepId: null,
  dragFrom: null,
  lastClick: null,
  keyBuffer: { step: null, timer: null },
  logOffset: 0,
  lastRenderedIndex: -1,
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

function toast(message, kind = 'info') {
  const box = document.getElementById('mas-toast');
  const item = el('div', { class: `mas-toast mas-toast-${kind}`, text: message });
  box.appendChild(item);
  setTimeout(() => item.remove(), 4200);
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

  const tabs = ['flow', 'record', 'pages', 'shots', 'texts', 'agent', 'log']
    .map((name) => el('button', {
    class: 'mas-tab', dataset: { tab: name }, type: 'button',
    text: t('tab' + name[0].toUpperCase() + name.slice(1)),
    onClick: () => selectTab(name),
  }));

  const panel = el('div', { id: 'mas-panel', class: 'mas-panel', hidden: true }, [
    el('div', { class: 'mas-head' }, [
      el('b', { text: t('title') }),
      el('span', { id: 'mas-conn', class: 'mas-dot', title: 'status' }),
      el('select', {
        id: 'mas-lang', onChange: (event) => {
          state.lang = event.target.value;
          localStorage.setItem(LS.lang, state.lang);
          rerenderAll();
        },
      }, [el('option', { value: 'fa' }, 'FA'), el('option', { value: 'en' }, 'EN')]),
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
      el('section', { id: 'mas-tab-flow', class: 'mas-tabpane' }),
      el('section', { id: 'mas-tab-record', class: 'mas-tabpane', hidden: true }),
      el('section', { id: 'mas-tab-pages', class: 'mas-tabpane', hidden: true }),
      el('section', { id: 'mas-tab-shots', class: 'mas-tabpane', hidden: true }),
      el('section', { id: 'mas-tab-texts', class: 'mas-tabpane', hidden: true }),
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
      el('div', { class: 'mas-row' }, [
        el('button', {
          id: 'mas-confirm-approve',
          class: 'mas-btn mas-primary', type: 'button', text: t('approve'),
          onClick: () => control('confirm', { approve: true }),
        }),
        el('button', {
          id: 'mas-confirm-refuse',
          class: 'mas-btn', type: 'button', text: t('refuse'),
          onClick: () => control('confirm', { approve: false }),
        }),
      ]),
    ]),
  ]);

  const layer = el('div', { id: 'mas-record-layer', class: 'mas-record-layer', hidden: true });
  layer.tabIndex = 0;
  layer.addEventListener('pointerdown', onRecordPointerDown);
  layer.addEventListener('pointerup', onRecordPointerUp);
  layer.addEventListener('contextmenu', (event) => event.preventDefault());
  layer.addEventListener('keydown', onRecordKeyDown);

  root.append(toggle, panel, confirm, layer, el('div', { id: 'mas-toast', class: 'mas-toast-box' }));
  document.body.appendChild(root);

  document.getElementById('mas-lang').value = state.lang;
  return root;
}

function setPanelOpen(open) {
  document.getElementById('mas-panel').hidden = !open;
  document.getElementById('mas-toggle').hidden = open;
  localStorage.setItem(LS.panel, open ? '1' : '0');
  if (open) {
    selectTab(currentTab());
    refreshStatus();
  }
}

function currentTab() {
  const active = document.querySelector('.mas-tab.is-active');
  return (active && active.dataset.tab) || 'flow';
}

function selectTab(name) {
  document.querySelectorAll('.mas-tab').forEach((node) => {
    node.classList.toggle('is-active', node.dataset.tab === name);
  });
  document.querySelectorAll('.mas-tabpane').forEach((node) => {
    node.hidden = node.id !== `mas-tab-${name}`;
  });
  if (name === 'pages') renderPages();
  if (name === 'shots') renderShots();
  if (name === 'texts') renderTexts();
  if (name === 'log') renderLog();
  if (name === 'agent') renderAgent();
  if (name === 'flow') renderFlow();
  if (name === 'record') renderRecord();
}

function rerenderAll() {
  document.getElementById('mas-root').remove();
  buildPanel();
  setPanelOpen(localStorage.getItem(LS.panel) === '1');
  selectTab('flow');
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

  const addSelect = el('select', { class: 'mas-input' },
    STEP_DEFS.map((def) => el('option', { value: def.type }, def.type)));
  const addRow = el('div', { class: 'mas-row' }, [
    addSelect,
    button(t('addStep'), () => {
      state.flow.steps.push(createStep(addSelect.value));
      persistFlow();
      renderFlow();
    }, { class: 'mas-btn mas-primary' }),
  ]);

  const list = el('ol', { class: 'mas-steps' });
  if (!state.flow.steps.length) {
    list.appendChild(el('li', { class: 'mas-empty', text: t('stepsEmpty') }));
  }
  state.flow.steps.forEach((step, index) => list.appendChild(stepRow(step, index)));

  const settings = state.flow.settings;
  const settingsBox = el('details', { class: 'mas-settings' }, [
    el('summary', { text: t('settings') }),
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
  ]);

  const ioRow = el('div', { class: 'mas-row mas-wrap' }, [
    button(t('save'), () => saveFlow(), { class: 'mas-btn mas-primary' }),
    button(t('saveAs'), () => {
      const name = window.prompt(t('saveAs'), state.flow.name);
      if (name) saveFlow(name);
    }),
    button(t('load'), loadFlowDialog),
    button('JSON ⇩', exportJson),
    button('📋 ' + t('copyReport'), copyReport, { class: 'mas-btn' }),
    button('🗑 ' + t('clearAll'), clearAllSteps, { class: 'mas-btn mas-danger' }),
  ]);

  const errorBox = errors.length
    ? el('div', { class: 'mas-errors' }, [
      el('b', { text: t('invalidFlow') }),
      el('ul', {}, errors.map((e) => el('li', { text: e }))),
    ])
    : null;

  const libraryBox = el('details', { class: 'mas-settings' }, [
    el('summary', { text: '📚 ' + t('library') }),
    el('div', { id: 'mas-library' }),
  ]);

  replace(pane, nameRow, addRow, errorBox, list, settingsBox, libraryBox, ioRow);
  renderLibrary();
}

/* ------------------------------------------------------------------ *
 * Saved flows: one place that keeps the runs which worked
 * ------------------------------------------------------------------ */

async function renderLibrary() {
  const box = document.getElementById('mas-library');
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
function copyReport() {
  return copyFrom('/report?base=' + encodeURIComponent(window.location.origin));
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
  state.keyBuffer = { step: null, timer: null };
  persistFlow();
  renderFlow();
  renderRecord();
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
    el('span', { class: 'mas-type', text: step.type }),
    el('span', { class: 'mas-spacer' }),
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

function stepEditor(step) {
  const def = stepDef(step.type);
  const fields = [];

  for (const field of (def ? def.fields : [])) {
    if (field.virtual) {
      fields.push(textField(field.key === 'keysText' ? 'keys' : field.key,
        keysToText(step.keys), (value) => { step.keys = keysFromText(value); }, 'ctrl+l, Return'));
      continue;
    }
    if (field.kind === 'area') {
      fields.push(el('label', { class: 'mas-field mas-wide' }, [
        el('span', { text: field.key }),
        el('textarea', {
          class: 'mas-input', rows: '3', value: step[field.key] || '',
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
  const dot = document.getElementById('mas-conn');
  if (!dot) return;
  dot.className = 'mas-dot ' + (ok ? 'is-ok' : 'is-bad');
  dot.title = ok ? t('connected') : (needsToken ? t('needToken') : 'offline');
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
    confirm.hidden = !waiting;
    confirm.classList.toggle('mas-confirm-handoff', Boolean(humanHandoff));
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
    }
  }
  const run = document.getElementById('mas-run');
  if (run) run.disabled = isRunning();
  renderRunPanel(raw, label);
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

function exportJson() {
  const blob = new Blob([JSON.stringify(state.flow, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const link = el('a', { href: url, download: `${state.flow.name || 'flow'}.json` });
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

/* ------------------------------------------------------------------ *
 * Record tab
 * ------------------------------------------------------------------ */

function renderRecord() {
  const pane = document.getElementById('mas-tab-record');
  if (isStandalone()) {
    // Capturing clicks needs the browser view to click on, which only the
    // noVNC page has. Everything else in this panel works the same.
    replace(pane,
      el('p', { class: 'mas-hint', text: t('recordStandalone') }),
      el('div', { class: 'mas-row' }, [
        el('a', {
          class: 'mas-btn mas-primary', href: '../vnc.html', target: '_blank',
          rel: 'noopener', text: '🖥 ' + t('openVnc'),
        }),
        button(t('clear'), () => {
          state.flow.steps = []; persistFlow(); renderFlow(); renderRecord();
        }),
      ]),
      el('p', { class: 'mas-hint', text: `${state.flow.steps.length} · ${state.flow.name}` }),
    );
    return;
  }
  replace(pane,
    el('div', { class: 'mas-row' }, [
      el('button', {
        class: 'mas-btn ' + (state.recording ? 'mas-danger' : 'mas-primary'), type: 'button',
        text: (state.recording ? '⏺ ' + t('recordOn') : '○ ' + t('recordOff')),
        onClick: toggleRecording,
      }),
      el('label', { class: 'mas-check' }, [
        el('input', {
          type: 'checkbox', checked: state.captureKeys,
          onChange: (event) => { state.captureKeys = event.target.checked; },
        }),
        el('span', { text: t('keyboardHint') }),
      ]),
      button(t('clear'), () => { state.flow.steps = []; persistFlow(); renderFlow(); renderRecord(); }),
    ]),
    el('p', { class: 'mas-hint', text: t('recordHint') }),
    el('p', { class: 'mas-hint', text: `${state.flow.steps.length} · ${state.flow.name}` }),
  );
}

function toggleRecording() {
  state.recording = !state.recording;
  const layer = document.getElementById('mas-record-layer');
  layer.hidden = !state.recording;
  if (state.recording) {
    positionRecordLayer();
    layer.focus();
    selectTab('record');
  } else {
    flushKeyBuffer();
  }
  renderRecord();
}

/** Keep the capture layer exactly over the noVNC canvas. */
function positionRecordLayer() {
  const canvas = document.getElementById('noVNC_canvas')
    || document.querySelector('#noVNC_screen canvas')
    || document.querySelector('#noVNC_container canvas');
  const layer = document.getElementById('mas-record-layer');
  if (!canvas || !layer) return null;
  const rect = canvas.getBoundingClientRect();
  Object.assign(layer.style, {
    left: `${rect.left}px`, top: `${rect.top}px`,
    width: `${rect.width}px`, height: `${rect.height}px`,
  });
  return rect;
}

function onRecordPointerDown(event) {
  event.preventDefault();
  event.stopPropagation();
  const rect = positionRecordLayer();
  if (!rect) return;
  const point = mapToDesktop(event.clientX, event.clientY, rect, state.flow.viewport);
  if (!point) return;
  state.dragFrom = { ...point, button: event.button === 2 ? 'right' : 'left', at: Date.now() };
  document.getElementById('mas-record-layer').focus();
}

function onRecordPointerUp(event) {
  event.preventDefault();
  event.stopPropagation();
  if (!state.dragFrom) return;
  const rect = positionRecordLayer();
  if (!rect) { state.dragFrom = null; return; }
  const point = mapToDesktop(event.clientX, event.clientY, rect, state.flow.viewport);
  const from = state.dragFrom;
  state.dragFrom = null;
  if (!point) return;

  const distance = Math.hypot(point.x - from.x, point.y - from.y);
  if (distance > 15) {
    addStep(createStep('drag', { x1: from.x, y1: from.y, x2: point.x, y2: point.y, button: from.button }));
    return;
  }

  const previous = state.lastClick;
  const now = Date.now();
  const sameSpot = previous && Math.hypot(previous.x - point.x, previous.y - point.y) < 12
    && now - previous.at < 450;
  if (sameSpot) {
    const last = state.flow.steps[state.flow.steps.length - 1];
    if (last && last.type === 'click') {
      state.flow.steps[state.flow.steps.length - 1] =
        createStep('double_click', { x: point.x, y: point.y, label: '' });
      state.lastClick = null;
      persistFlow();
      renderFlow();
      return;
    }
  }
  state.lastClick = { ...point, at: now };
  addStep(createStep('click', { x: point.x, y: point.y, button: from.button }));
}

function onRecordKeyDown(event) {
  if (!state.recording || !state.captureKeys) return;
  event.preventDefault();
  event.stopPropagation();

  const modifiers = [];
  if (event.ctrlKey) modifiers.push('ctrl');
  if (event.altKey) modifiers.push('alt');
  if (event.shiftKey) modifiers.push('shift');
  if (event.metaKey) modifiers.push('super');

  const printable = event.key.length === 1 && !modifiers.length;
  if (printable) {
    appendToKeyBuffer(event.key);
    return;
  }
  flushKeyBuffer();
  const name = keyName(event.key);
  addStep(createStep('key', { keys: [...modifiers, name] }));
}

function keyName(key) {
  const map = {
    ' ': 'space', Enter: 'Return', Escape: 'Escape', Backspace: 'BackSpace',
    Delete: 'Delete', Tab: 'Tab', ArrowUp: 'Up', ArrowDown: 'Down',
    ArrowLeft: 'Left', ArrowRight: 'Right', Home: 'Home', End: 'End',
    PageUp: 'Prior', PageDown: 'Next',
  };
  if (map[key]) return map[key];
  if (/^F\d{1,2}$/.test(key)) return key;
  return key;
}

function isAscii(text) {
  return /^[\x00-\x7F]*$/.test(text);
}

function appendToKeyBuffer(char) {
  const buffer = state.keyBuffer;
  if (buffer.timer) clearTimeout(buffer.timer);
  if (!buffer.step) {
    buffer.step = createStep(isAscii(char) ? 'type' : 'paste', { text: '' });
    state.flow.steps.push(buffer.step);
  }
  buffer.step.text += char;
  // xdotool type cannot produce non-ASCII glyphs, so Persian and the like must
  // go through the clipboard paste path or they arrive as keyboard garbage.
  if (!isAscii(buffer.step.text)) buffer.step.type = 'paste';
  buffer.step.label = labelFor(buffer.step);
  buffer.timer = setTimeout(flushKeyBuffer, 1500);
  persistFlow();
  renderFlow();
}

function flushKeyBuffer() {
  if (state.keyBuffer.timer) clearTimeout(state.keyBuffer.timer);
  const step = state.keyBuffer.step;
  // A buffer that never received a real character must not survive as a
  // "text must not be empty" error waiting to happen.
  if (step && !(step.text || '').trim()) {
    const index = state.flow.steps.indexOf(step);
    if (index >= 0) {
      state.flow.steps.splice(index, 1);
      persistFlow();
      renderFlow();
    }
  }
  state.keyBuffer = { step: null, timer: null };
}

function addStep(step) {
  flushKeyBuffer();
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
      });
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

  const detail = el('div', { id: 'mas-page-detail' });
  if (state.activePage) renderPageDetail(detail, state.activePage);
  replace(pane, head, list, detail);
}

function renderPageDetail(container, page) {
  const rows = (page.elements || []).slice(0, 60).map((item) => el('tr', {}, [
    el('td', {}, el('code', { text: truncate(item.selector, 40) })),
    el('td', { text: truncate(item.text || item.href || '', 28) }),
    el('td', { text: `${item.desktop.x},${item.desktop.y}` }),
    el('td', {}, el('button', {
      class: 'mas-btn mas-mini', type: 'button', text: t('useAsClick'),
      onClick: () => {
        addStep(createStep('click', { x: item.desktop.x, y: item.desktop.y }));
        toast(labelFor(state.flow.steps[state.flow.steps.length - 1]), 'ok');
      },
    })),
  ]));

  const challenge = page.challenge || { detected: false, signals: [] };
  replace(container,
    challenge.detected ? el('div', { class: 'mas-challenge-warning' }, [
      el('b', { text: '⚠ ' + t('possibleChallenge') }),
      el('div', { class: 'mas-hint', text: `${t('challengeSignals')}: ${(challenge.signals || []).join(', ')}` }),
      el('div', { class: 'mas-hint', text: t('challengeBody') }),
    ]) : el('p', { class: 'mas-hint', text: t('noChallengeDetected') }),
    el('h4', { text: `${t('elements')} (${(page.elements || []).length})` }),
    page.url ? el('div', { class: 'mas-hint', text: page.url }) : null,
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
  before: 'قبل گام', after: 'بعد گام', error: 'لحظه خطا' };

function shotLink(name) {
  return `${API_PREFIX}/public/shot/${encodeURIComponent(name)}`;
}

async function renderShots() {
  const pane = document.getElementById('mas-tab-shots');
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

async function renderTexts() {
  const pane = document.getElementById('mas-tab-texts');
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
  const pane = host || document.getElementById('mas-agent-sub');
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
        const { flow, errors } = normaliseImportedFlow(parsed, state.flow.viewport);
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

const AGENT_SUBS = ['assistant', 'jobs', 'captcha', 'telegram', 'scripts', 'data', 'keys'];

const AGENT_RENDERERS = {
  assistant: renderAgentAssistant,
  jobs: renderAgentJobs,
  captcha: renderAgentCaptcha,
  telegram: renderAgentTelegram,
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
  return el('table', { class: 'mas-table' }, [
    el('thead', {}, el('tr', {}, columns.map((name) => el('th', { text: name })))),
    el('tbody', {}, rows.map((cells) => el('tr', {}, cells.map((cell) => el('td', {},
      typeof cell === 'string' || typeof cell === 'number' ? [String(cell)] : [cell]))))),
  ]);
}

async function agentError(host, error) {
  replace(host, el('p', { class: 'mas-empty', text: error.message || String(error) }));
}

async function renderAgent() {
  const pane = document.getElementById('mas-tab-agent');
  if (!pane) return;
  const active = state.agentSub || 'assistant';
  // The status bar is filled in separately, on purpose: the assistant sub-pane
  // is the copy-prompt/import workflow and must keep working even when this
  // deployment has no agent attached at all (--no-agent, or an unreachable API).
  replace(pane,
    el('div', { class: 'mas-row mas-wrap' }, AGENT_SUBS.map((name) => button(
      agentSubLabel(name), () => selectAgentSub(name),
      { class: 'mas-btn' + (name === active ? ' mas-primary' : ''), dataset: { sub: name } }))),
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
  renderAgent();
}

function renderAgentSub(name) {
  const host = document.getElementById('mas-agent-sub');
  if (!host) return;
  (AGENT_RENDERERS[name] || renderAgentAssistant)(host);
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

async function renderAgentTelegram(host) {
  replace(host, el('p', { class: 'mas-empty', text: '…' }));
  let status; let settings = {};
  try {
    status = await api('/agent/telegram/status');
    settings = (state.agent && state.agent.settings) || {};
  } catch (error) { return agentError(host, error); }
  const targets = status.targets || [];
  const draft = { mode: status.mode };

  // Chats found through getUpdates. Kept apart from the saved targets on
  // purpose: this list is a picker, and rendering the saved targets here showed
  // the operator their own list back and made discovery look broken.
  const found = state.agentTgFound || [];
  const foundRows = found.map((target) => [
    String(target.id), target.title || '', target.type || '',
    button('＋', async () => {
      const next = targets.slice();
      next.push(target);
      try { await api('/agent/settings', { method: 'POST', body: { 'telegram.targets': next } }); toast(t('agentSaved'), 'ok'); renderAgentSub('telegram'); } catch (error) { toast(error.message, 'error'); }
    }, { class: 'mas-btn mas-primary' }),
  ]);

  replace(host,
    el('div', { class: 'mas-box' }, [
      el('b', { text: t('agentSubTelegram') }),
      agentField(t('agentTgMode'), agentSelect(['off', 'bot', 'account'], status.mode,
        (event) => { draft.mode = event.target.value; })),
      agentField(t('agentTgBotToken'), el('input', {
        class: 'mas-input', type: 'password', placeholder: status.botTokenSet ? '•••••• (stored)' : '',
        onInput: (event) => { draft.botToken = event.target.value; },
      })),
      status.mode === 'account' ? el('div', {}, [
        el('p', { class: 'mas-warn', text: t('agentTgAccountWarn') }),
        el('div', { class: 'mas-row mas-wrap' }, [
          agentField('api_id', el('input', { class: 'mas-input', placeholder: status.apiCredentialsSet ? '•••••• (stored)' : '', onInput: (event) => { draft.apiId = event.target.value; } })),
          agentField('api_hash', el('input', { class: 'mas-input', type: 'password', placeholder: status.apiCredentialsSet ? '•••••• (stored)' : '', onInput: (event) => { draft.apiHash = event.target.value; } })),
        ]),
        el('div', { class: 'mas-row mas-wrap' }, [
          agentField(t('agentTgPhone'), el('input', { class: 'mas-input', value: status.phone || '', placeholder: '+98...', onInput: (event) => { draft.phone = event.target.value; } })),
          agentField(t('agentTgCode'), el('input', { class: 'mas-input', onInput: (event) => { draft.code = event.target.value; } })),
        ]),
        el('div', { class: 'mas-row mas-wrap' }, [
          button(t('agentTgLogin'), async () => {
            try { await api('/agent/telegram/login', { method: 'POST', body: { phone: draft.phone } }); toast('code sent', 'ok'); } catch (error) { toast(error.message, 'error'); }
          }),
          button(t('agentTgLoginFinish'), async () => {
            try { await api('/agent/telegram/login-finish', { method: 'POST', body: { code: draft.code } }); toast('ok', 'ok'); renderAgentSub('telegram'); } catch (error) { toast(error.message, 'error'); }
          }, { class: 'mas-btn mas-primary' }),
        ]),
      ]) : null,
      el('div', { class: 'mas-row mas-wrap' }, [
        button('💾 ' + t('agentSave'), async () => {
          const body = { 'telegram.mode': draft.mode };
          if (draft.botToken) body['telegram.botToken'] = draft.botToken;
          if (draft.apiId) body['telegram.apiId'] = draft.apiId;
          if (draft.apiHash) body['telegram.apiHash'] = draft.apiHash;
          try { await api('/agent/settings', { method: 'POST', body }); toast(t('agentSaved'), 'ok'); renderAgent(); } catch (error) { toast(error.message, 'error'); }
        }, { class: 'mas-btn mas-primary' }),
        button('🔎 ' + t('agentTgDiscover'), async () => {
          try {
            state.agentTgFound = (await api('/agent/telegram/targets')).targets || [];
            renderAgentSub('telegram');
          } catch (error) { toast(error.message, 'error'); }
        }),
        button('✉ ' + t('agentTest'), async () => {
          try { const result = await api('/agent/telegram/test', { method: 'POST', body: {} }); toast(`sent: ${result.sent}`, 'ok'); } catch (error) { toast(error.message, 'error'); }
        }),
        button('🩺 probe', async () => {
          try { const probe = await api('/agent/telegram/status?probe=1'); toast(probe.error || JSON.stringify(probe.identity || {}), probe.error ? 'error' : 'ok'); } catch (error) { toast(error.message, 'error'); }
        }),
      ]),
      el('p', { class: 'mas-hint', text: t('agentTgHint') }),
      status.error ? el('p', { class: 'mas-warn', text: status.error }) : null,
    ]),
    el('div', { class: 'mas-box' }, [
      el('b', { text: `${t('agentTgTargets')} (${targets.length})` }),
      targets.length ? agentTable(['id', 'title', 'type', ''], targets.map((target, index) => [
        String(target.id), target.title || '', target.type || '',
        button('🗑', async () => {
          const next = targets.filter((_, position) => position !== index);
          try { await api('/agent/settings', { method: 'POST', body: { 'telegram.targets': next } }); renderAgentSub('telegram'); } catch (error) { toast(error.message, 'error'); }
        }),
      ])) : el('p', { class: 'mas-hint', text: t('agentEmpty') }),
    ]),
    found.length ? el('div', { class: 'mas-box' }, [
      el('b', { text: `${t('agentTgDiscover')} (${found.length})` }),
      agentTable(['id', 'title', 'type', t('agentAdd')], foundRows),
    ]) : null);
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

  replace(host,
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
    if (reply.flow && reply.flowValid && (reply.flow.steps || []).length) {
      state.agentFlowProposal = reply.flow;
    }
    renderAgentAssistant();
    if (state.agentFlowProposal) {
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
  const standalone = isStandalone();
  buildPanel();
  if (standalone) {
    // The panel is the whole page here, so the collapse tab is pointless and a
    // link back to the live browser view is not.
    const toggle = document.getElementById('mas-toggle');
    if (toggle) toggle.hidden = true;
    const head = document.querySelector('#mas-panel .mas-head');
    if (head) {
      head.appendChild(el('a', {
        class: 'mas-btn mas-vnclink', href: '../vnc.html', target: '_blank',
        rel: 'noopener', text: '🖥 ' + t('openVnc'),
      }));
    }
  } else {
    // From inside noVNC, offer the dedicated panel in its own tab.
    const head = document.querySelector('#mas-panel .mas-head');
    if (head) {
      head.appendChild(el('a', {
        class: 'mas-btn mas-vnclink', href: 'automation/panel.html', target: '_blank',
        rel: 'noopener', title: t('openPanel'), text: '⧉ ' + t('openPanel'),
      }));
    }
  }
  setPanelOpen(standalone || localStorage.getItem(LS.panel) === '1');
  selectTab('flow');
  renderStatus();
  refreshStatus();
  setInterval(refreshStatus, 900);
  // Counters tick on their own so the numbers move smoothly between polls.
  setInterval(tickTimers, 1000);
  window.addEventListener('resize', () => { if (state.recording) positionRecordLayer(); });
  window.addEventListener('scroll', () => { if (state.recording) positionRecordLayer(); }, true);
  setInterval(() => { if (state.recording) positionRecordLayer(); }, 1000);
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
