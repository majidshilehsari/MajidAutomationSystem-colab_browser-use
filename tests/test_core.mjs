// Unit tests for automation/static/core.mjs — run with: node --test tests/
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  mapToDesktop, clamp, createStep, labelFor, validateFlow, emptyFlow,
  keysFromText, keysToText, moveStep, promptSteps, flowAsJson, extractJson,
  normaliseImportedFlow, formatElapsed, translate, stepDef, STEP_DEFS, compact,
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

/* ------------------------------------------------------------------ *
 * Round 26: one step language. The editor knows exactly one type -
 * python - and everything that arrives in the old JSON vocabulary is
 * translated by the server (POST /convert) before the panel sees it.
 * ------------------------------------------------------------------ */

test('the editor knows exactly one step type: python', () => {
  assert.deepEqual(STEP_DEFS.map((def) => def.type), ['python']);
  assert.ok(stepDef('python'));
  for (const legacy of ['agent_open', 'agent_ask', 'captcha_solve', 'click',
                        'goto_url', 'pause_for_human_verification']) {
    assert.equal(stepDef(legacy), null, `${legacy} must not be editable anymore`);
  }
});

test('labels are readable', () => {
  assert.equal(labelFor(createStep('python', { code: 'check(click(10, 20))' })),
               'check(click(10, 20))');
  // The first real line of code is the label; comments do not count.
  const multiline = labelFor(createStep('python', {
    code: '# a note to myself\ncheck(goto(\'https://example.com\'))\nwait(1)',
  }));
  assert.equal(multiline, "check(goto('https://example.com'))");
  assert.equal(labelFor(createStep('python')), 'پایتون',
               'empty code still reads as something');
  const long = labelFor(createStep('python', { code: `check(${'x'.repeat(80)})` }));
  assert.ok(long.length <= 45, `label should be truncated: ${long}`);
});

test('compact drops the values that must not reach appendChild', () => {
  const node = { tag: 'div' };
  assert.deepEqual(compact([node, null, undefined, false, true]), [node]);
  assert.deepEqual(compact([node, [null, node]]), [node, node], 'nested arrays flatten');
  assert.deepEqual(compact(node), [node], 'a bare node is wrapped');
  assert.deepEqual(compact([]), []);
  assert.deepEqual(compact([0, '', 'text']), [0, '', 'text'], 'falsy but real values survive');
  // This is the exact shape that crashed the sidebar: a conditional child.
  const errors = [];
  assert.deepEqual(compact([node, errors.length ? node : null]), [node]);
});

test('validateFlow accepts a good flow and rejects a bad one', () => {
  const flow = emptyFlow('ok');
  flow.steps = [createStep('python', { code: 'check(click(10, 10))' }),
                createStep('python', { code: "check(press_key(['Return']))" })];
  assert.deepEqual(validateFlow(flow, VP), []);

  const bad = emptyFlow('bad');
  bad.steps = [
    createStep('python', { code: '   ' }),
    { id: 'x', type: 'teleport' },
    // old vocabulary: the server converts it before it ever reaches here,
    // so locally it must be refused honestly instead of half-rendered
    { id: 'y', type: 'click', x: 1, y: 2 },
  ];
  const errors = validateFlow(bad, VP);
  assert.ok(errors.some((e) => e.includes('code must not be empty')));
  assert.equal(errors.filter((e) => e.includes('unknown step type')).length, 2);
});

test('validateFlow still guards the flow-level typing mode', () => {
  const good = emptyFlow('safe');
  good.settings.typingMode = 'low';
  good.steps = [createStep('python', { code: "check(type_text('hello', mode='fast'))" })];
  assert.deepEqual(validateFlow(good, VP), []);

  const bad = emptyFlow('bad mode');
  bad.settings.typingMode = 'random';
  bad.steps = [createStep('python', { code: 'wait(1)' })];
  assert.ok(validateFlow(bad, VP).some((error) => error.includes('settings: typingMode')));
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
    createStep('python', { code: 'wait(1)' }),
    createStep('python', { code: 'wait(2)', enabled: false }),
    createStep('python', { code: 'check(click(1, 2))', requiresConfirmation: true }),
  ];
  const compacted = promptSteps(flow);
  assert.equal('enabled' in compacted[0], false);
  assert.equal(compacted[1].enabled, false);
  assert.equal(compacted[2].requiresConfirmation, true);
  assert.equal(compacted[0].label, 'wait(1)', 'labels stay: they help the AI');
});

test('flowAsJson is valid JSON that the server accepts', () => {
  const flow = emptyFlow('demo');
  flow.steps = [createStep('python', { code: 'check(click(5, 6))' })];
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

test('normaliseImportedFlow turns a converted AI reply into a runnable flow', () => {
  const { flow, errors } = normaliseImportedFlow({
    name: 'search',
    steps: [
      { type: 'python', code: "check(goto('https://example.com'))" },
      { type: 'python', code: 'check(click(100, 200))' },
      { type: 'python', code: "check(press_key(['Return']))" },
      { type: 'fly' },
    ],
  }, VP);
  assert.equal(flow.name, 'search');
  assert.equal(flow.steps.length, 3, 'the unknown step is dropped');
  assert.ok(flow.steps.every((s) => s.id && s.enabled === true && s.label));
  assert.ok(errors.some((e) => e.includes('unknown type')));
});

test('normalising drops the old vocabulary honestly', () => {
  // The panel converts through the server first; if an old flow ever skips
  // that, the normaliser must say so instead of silently accepting it.
  const { flow, errors } = normaliseImportedFlow({
    name: 'اتصال به ایجنت',
    steps: [
      { type: 'agent_open', provider: 'lmarena' },
      { type: 'python', code: 'wait(1.5)' },
    ],
  });
  assert.equal(flow.steps.length, 1);
  assert.equal(flow.steps[0].type, 'python');
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

test('the round-26 wording is in both languages', () => {
  // «مرحله» replaced «گام», and the scope vocabulary exists on both sides.
  assert.equal(translate('fa', 'steps'), 'مرحله');
  assert.equal(translate('fa', 'stepNo'), 'مرحلهٔ');
  assert.equal(translate('en', 'stepNo'), 'Step');
  assert.equal(translate('fa', 'scopeBrain'), 'اختصاصی مغز');
  assert.equal(translate('en', 'scopeBrain'), 'Brain only');
  assert.ok(translate('fa', 'chatFlowApplied').includes('مستقیم به مراحل'));
});
