// Unit tests for automation/static/core.mjs — run with: node --test tests/
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  mapToDesktop, clamp, createStep, labelFor, validateFlow, emptyFlow,
  keysFromText, keysToText, moveStep, promptSteps, flowAsJson, extractJson,
  normaliseImportedFlow, formatElapsed, translate, stepDef, STEP_DEFS,
} from '../automation/static/core.mjs';

const VP = { width: 1366, height: 768 };

test('mapToDesktop converts a scaled canvas to desktop pixels', () => {
  // noVNC shows the 1366x768 desktop inside a 683x384 box at (10,20).
  const rect = { left: 10, top: 20, width: 683, height: 384 };
  const centre = mapToDesktop(10 + 683 / 2, 20 + 384 / 2, rect, VP);
  assert.deepEqual(centre, { x: 683, y: 384 });

  const origin = mapToDesktop(10, 20, rect, VP);
  assert.deepEqual(origin, { x: 0, y: 0 });

  const corner = mapToDesktop(10 + 683, 20 + 384, rect, VP);
  // The extreme corner maps one past the last pixel; clamping keeps it usable.
  assert.deepEqual(corner, { x: 1365, y: 767 });
});

test('mapToDesktop clamps to the desktop instead of returning nonsense', () => {
  const rect = { left: 0, top: 0, width: 100, height: 100 };
  assert.deepEqual(mapToDesktop(-50, -50, rect, VP), { x: 0, y: 0 });
  const far = mapToDesktop(5000, 5000, rect, VP);
  assert.deepEqual(far, { x: 1365, y: 767 });
});

test('mapToDesktop refuses a zero sized rect', () => {
  assert.equal(mapToDesktop(5, 5, { left: 0, top: 0, width: 0, height: 0 }, VP), null);
  assert.equal(mapToDesktop(5, 5, null, VP), null);
});

test('clamp', () => {
  assert.equal(clamp(5, 0, 10), 5);
  assert.equal(clamp(-1, 0, 10), 0);
  assert.equal(clamp(11, 0, 10), 10);
});

test('every step definition has defaults and a label', () => {
  for (const def of STEP_DEFS) {
    const step = createStep(def.type);
    assert.equal(step.type, def.type);
    assert.equal(step.enabled, true);
    assert.ok(step.id.startsWith('s-'));
    assert.ok(labelFor(step).length > 0, `no label for ${def.type}`);
    assert.ok(stepDef(def.type) === def);
  }
});

test('labels are readable', () => {
  assert.equal(labelFor(createStep('click', { x: 10, y: 20 })), 'click 10,20');
  assert.equal(labelFor(createStep('click', { x: 1, y: 2, button: 'right' })), 'click 1,2 right');
  assert.equal(labelFor(createStep('key', { keys: ['ctrl', 'l'] })), 'key ctrl+l');
  assert.equal(labelFor(createStep('wait', { ms: 1500 })), 'wait 1500ms');
  assert.equal(labelFor(createStep('scroll', { amount: -3 })), 'scroll -3');
  assert.equal(labelFor(createStep('goto_url', { url: 'https://example.com' })),
               'open https://example.com');
  const long = labelFor(createStep('paste', { text: 'x'.repeat(60) }));
  assert.ok(long.length < 40, `label should be truncated: ${long}`);
});

test('validateFlow accepts a good flow and rejects a bad one', () => {
  const flow = emptyFlow('ok');
  flow.steps = [createStep('click', { x: 10, y: 10 }), createStep('key', { keys: ['Return'] })];
  assert.deepEqual(validateFlow(flow, VP), []);

  const bad = emptyFlow('bad');
  bad.steps = [
    createStep('click', { x: 5000, y: 10 }),
    { id: 'x', type: 'teleport' },
    createStep('key', { keys: [] }),
    createStep('goto_url', { url: 'example.com' }),
    createStep('wait', { ms: 0 }),
  ];
  const errors = validateFlow(bad, VP);
  assert.ok(errors.some((e) => e.includes('outside the 1366x768 desktop')));
  assert.ok(errors.some((e) => e.includes('unknown step type')));
  assert.ok(errors.some((e) => e.includes('keys must not be empty')));
  assert.ok(errors.some((e) => e.includes('scheme')));
  assert.ok(errors.some((e) => e.includes('ms must be positive')));
});

test('keysFromText and keysToText round trip', () => {
  assert.deepEqual(keysFromText('ctrl+l, Return'), ['ctrl+l', 'Return']);
  assert.deepEqual(keysFromText('  ctrl+a   ctrl+c '), ['ctrl+a', 'ctrl+c']);
  assert.deepEqual(keysFromText(''), []);
  assert.equal(keysToText(['ctrl', 'l']), 'ctrl, l');
});

test('moveStep reorders and ignores out of range targets', () => {
  const steps = [{ id: 'a' }, { id: 'b' }, { id: 'c' }];
  assert.deepEqual(moveStep(steps, 0, 2).map((s) => s.id), ['b', 'c', 'a']);
  assert.deepEqual(moveStep(steps, 2, 0).map((s) => s.id), ['c', 'a', 'b']);
  assert.deepEqual(moveStep(steps, 0, 99).map((s) => s.id), ['a', 'b', 'c']);
  assert.deepEqual(steps.map((s) => s.id), ['a', 'b', 'c'], 'the input must not mutate');
});

test('promptSteps drops schema defaults', () => {
  const flow = emptyFlow();
  flow.steps = [
    createStep('click', { x: 1, y: 2 }),
    createStep('click', { x: 3, y: 4, enabled: false }),
    createStep('paste', { text: 'hi', requiresConfirmation: true }),
  ];
  const compact = promptSteps(flow);
  assert.equal('enabled' in compact[0], false);
  assert.equal(compact[1].enabled, false);
  assert.equal(compact[2].requiresConfirmation, true);
  assert.equal(compact[0].label, 'click 1,2', 'labels stay: they help the AI');
});

test('flowAsJson is valid JSON that the server accepts', () => {
  const flow = emptyFlow('demo');
  flow.steps = [createStep('click', { x: 5, y: 6 })];
  const parsed = JSON.parse(flowAsJson(flow));
  assert.equal(parsed.schema, 1);
  assert.equal(parsed.steps.length, 1);
  assert.equal(parsed.viewport.width, 1366);
});

test('extractJson finds JSON inside prose and fences', () => {
  assert.deepEqual(extractJson('{"a":1}'), { a: 1 });
  assert.deepEqual(extractJson('Sure!\n```json\n{"steps":[]}\n```\nDone.'), { steps: [] });
  assert.deepEqual(extractJson('Here you go: {"steps":[{"type":"wait","ms":10}]} enjoy'),
                   { steps: [{ type: 'wait', ms: 10 }] });
  assert.equal(extractJson('no json here'), null);
  assert.equal(extractJson(''), null);
});

test('extractJson survives braces inside strings', () => {
  const parsed = extractJson('{"text":"a } b","n":1}');
  assert.deepEqual(parsed, { text: 'a } b', n: 1 });
});

test('normaliseImportedFlow turns an AI reply into a runnable flow', () => {
  const { flow, errors } = normaliseImportedFlow({
    name: 'search',
    steps: [
      { type: 'goto_url', url: 'https://example.com' },
      { type: 'click', x: 100, y: 200 },
      { type: 'key', keys: 'Return' },
      { type: 'fly' },
    ],
  }, VP);
  assert.equal(flow.name, 'search');
  assert.equal(flow.steps.length, 3, 'the unknown step is dropped');
  assert.deepEqual(flow.steps[2].keys, ['Return'], 'a string keys value is promoted');
  assert.ok(flow.steps.every((s) => s.id && s.enabled === true));
  assert.ok(errors.some((e) => e.includes('unknown type')));
});

test('normaliseImportedFlow reports an empty result clearly', () => {
  const { flow, errors } = normaliseImportedFlow({ steps: [{ type: 'nope' }] }, VP);
  assert.equal(flow.steps.length, 0);
  assert.ok(errors.some((e) => e.includes('no usable steps')));
});

test('normaliseImportedFlow keeps a sane viewport', () => {
  const { flow } = normaliseImportedFlow({ steps: [], viewport: { width: 1920, height: 1080 } });
  assert.deepEqual(flow.viewport, { width: 1920, height: 1080 });
  const fallback = normaliseImportedFlow({ steps: [] }, VP);
  assert.deepEqual(fallback.flow.viewport, VP);
});

test('formatElapsed', () => {
  assert.equal(formatElapsed(900), '0s');
  assert.equal(formatElapsed(5000), '5s');
  assert.equal(formatElapsed(125000), '2m 5s');
  assert.equal(formatElapsed(undefined), '--');
});

test('translate falls back to English then to the key', () => {
  assert.equal(translate('fa', 'run'), 'اجرا');
  assert.equal(translate('en', 'run'), 'Run');
  assert.equal(translate('de', 'run'), 'Run');
  assert.equal(translate('fa', 'noSuchKey'), 'noSuchKey');
});
