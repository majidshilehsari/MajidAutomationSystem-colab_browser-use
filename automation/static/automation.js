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
  stepDef, truncate, structuredCloneSafe, compact,
} from './core.mjs';

const LS = {
  token: 'mas.token', lang: 'mas.lang', flow: 'mas.flow', panel: 'mas.panelOpen',
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

  const tabs = ['flow', 'record', 'pages', 'ai', 'log'].map((name) => el('button', {
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
    el('div', { class: 'mas-body' }, [
      el('section', { id: 'mas-tab-flow', class: 'mas-tabpane' }),
      el('section', { id: 'mas-tab-record', class: 'mas-tabpane', hidden: true }),
      el('section', { id: 'mas-tab-pages', class: 'mas-tabpane', hidden: true }),
      el('section', { id: 'mas-tab-ai', class: 'mas-tabpane', hidden: true }),
      el('section', { id: 'mas-tab-log', class: 'mas-tabpane', hidden: true }),
    ]),
    el('div', { class: 'mas-foot' }, [
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
          onClick: () => control('stop'),
        }),
        el('span', { id: 'mas-progress', class: 'mas-progress', text: '0/0' }),
      ]),
      el('div', { id: 'mas-statusline', class: 'mas-statusline' }),
    ]),
  ]);

  const confirm = el('div', { id: 'mas-confirm', class: 'mas-confirm', hidden: true }, [
    el('div', { class: 'mas-confirm-box' }, [
      el('h3', { text: t('confirmTitle') }),
      el('p', { id: 'mas-confirm-label' }),
      el('div', { class: 'mas-row' }, [
        el('button', {
          class: 'mas-btn mas-primary', type: 'button', text: t('approve'),
          onClick: () => control('confirm', { approve: true }),
        }),
        el('button', {
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
  if (name === 'log') renderLog();
  if (name === 'ai') renderAi();
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
        type: 'checkbox', checked: settings.stopOnError,
        onChange: (e) => { settings.stopOnError = e.target.checked; persistFlow(); },
      }),
      el('span', { text: t('stopOnError') }),
    ]),
  ]);

  const ioRow = el('div', { class: 'mas-row mas-wrap' }, [
    button(t('save'), saveFlow, { class: 'mas-btn mas-primary' }),
    button(t('saveAs'), () => {
      const name = window.prompt(t('saveAs'), state.flow.name);
      if (name) saveFlow(name);
    }),
    button(t('load'), loadFlowDialog),
    button('JSON ⇩', exportJson),
  ]);

  const errorBox = errors.length
    ? el('div', { class: 'mas-errors' }, [
      el('b', { text: t('invalidFlow') }),
      el('ul', {}, errors.map((e) => el('li', { text: e }))),
    ])
    : null;

  replace(pane, nameRow, addRow, errorBox, list, settingsBox, ioRow);
}

function stepRow(step, index) {
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
          onChange: (event) => { step[field.key] = event.target.value; },
        }, (field.options || []).map((option) => el('option', {
          value: option, selected: String(step[field.key] || '') === option,
        }, option))),
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

  fields.push(el('label', { class: 'mas-field mas-wide' }, [
    el('span', { text: 'label' }),
    el('input', {
      class: 'mas-input', value: step.label || '',
      onInput: (event) => { step.label = event.target.value; },
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
        step.label = step.label || labelFor(step);
        persistFlow(); state.openStepId = null; renderFlow();
      }, { class: 'mas-btn mas-primary' }),
      button('↺ ' + labelFor(step), () => { step.label = labelFor(step); renderFlow(); }),
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

async function control(action, extra = {}) {
  try {
    const data = await api('/control', { method: 'POST', body: { action, ...extra } });
    state.status = data.run || state.status;
    renderStatus();
  } catch (error) {
    toast(error.message, 'error');
  }
}

async function refreshStatus() {
  try {
    const [status, info] = await Promise.all([api('/status'), api('/info')]);
    state.status = status;
    state.info = info;
    setConnected(true);
  } catch (error) {
    // A 401 means the token is missing or wrong; anything else means the server
    // is not answering. Both are shown, but only the first is fixable here.
    setConnected(false, error.status === 401);
    return;
  }
  renderStatus();
  if (currentTab() === 'log') renderLog();
  // Redraw the step list only when the highlighted step moved, otherwise an
  // editor the human opened would close under their hands every poll.
  if (currentTab() === 'flow' && isRunning() && state.status.index !== state.lastRenderedIndex) {
    state.lastRenderedIndex = state.status.index;
    renderFlow();
  }
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
    if (status.error) parts.push(truncate(status.error, 90));
    replace(line, el('span', { class: 'mas-status-' + (status.status || 'idle'), text: parts.join(' · ') }));
  }

  const confirm = document.getElementById('mas-confirm');
  if (confirm) {
    const waiting = status.awaitingConfirmation;
    confirm.hidden = !waiting;
    if (waiting) {
      document.getElementById('mas-confirm-label').textContent =
        `#${waiting.index + 1} ${waiting.label || waiting.type}`;
    }
  }
  const run = document.getElementById('mas-run');
  if (run) run.disabled = isRunning();
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

function appendToKeyBuffer(char) {
  const buffer = state.keyBuffer;
  if (buffer.timer) clearTimeout(buffer.timer);
  if (!buffer.step) {
    buffer.step = createStep('type', { text: '' });
    state.flow.steps.push(buffer.step);
  }
  buffer.step.text += char;
  buffer.step.label = labelFor(buffer.step);
  buffer.timer = setTimeout(flushKeyBuffer, 1500);
  persistFlow();
  renderFlow();
}

function flushKeyBuffer() {
  if (state.keyBuffer.timer) clearTimeout(state.keyBuffer.timer);
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

  replace(container,
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
 * AI tab
 * ------------------------------------------------------------------ */

function renderAi() {
  const pane = document.getElementById('mas-tab-ai');
  const importBox = el('textarea', { class: 'mas-input mas-area', rows: '8', placeholder: t('importHint') });

  replace(pane,
    el('label', { class: 'mas-field mas-wide' }, [
      el('span', { text: t('token') }),
      el('input', {
        class: 'mas-input', type: 'password', value: state.token, placeholder: t('tokenHint'),
        onInput: (event) => {
          state.token = event.target.value.trim();
          localStorage.setItem(LS.token, state.token);
          refreshStatus();
        },
      }),
    ]),
    el('div', { class: 'mas-row mas-wrap' }, [
      button('📄 ' + t('copyGuide'), () => copyFrom('/guide')),
      button('🧩 ' + t('copyPrompt'), buildPrompt, { class: 'mas-btn mas-primary' }),
      button('⤓ JSON', () => copyText(JSON.stringify({ name: state.flow.name, steps: promptSteps(state.flow) }, null, 2))),
    ]),
    el('div', { class: 'mas-row' }, [
      button(t('importBtn'), async () => {
        const parsed = extractJson(importBox.value);
        if (!parsed) { toast('JSON?', 'error'); return; }
        const { flow, errors } = normaliseImportedFlow(parsed, state.flow.viewport);
        if (!flow || !flow.steps.length) {
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
    el('p', { class: 'mas-hint', text: `${state.flow.steps.length} steps · ${state.flow.name}` }),
  );
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
  const body = { flow: state.flow };
  if (state.activePage) body.pageId = state.activePage.id;
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

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast('copied', 'ok');
  } catch (_) {
    const area = el('textarea', { class: 'mas-input mas-area', rows: '10', value: text });
    const pane = document.getElementById('mas-tab-ai');
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
    el('span', { class: 'mas-log-t', text: new Date(entry.t * 1000).toLocaleTimeString() }),
    el('span', { text: entry.message }),
  ]))));
  pane.scrollTop = pane.scrollHeight;
}

/* ------------------------------------------------------------------ *
 * Boot
 * ------------------------------------------------------------------ */

function start() {
  buildPanel();
  setPanelOpen(localStorage.getItem(LS.panel) === '1');
  selectTab('flow');
  renderStatus();
  refreshStatus();
  setInterval(refreshStatus, 900);
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
