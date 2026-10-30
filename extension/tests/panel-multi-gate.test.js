/**
 * panel-multi-gate.test.js
 *
 * 다중 첨부에서 **막힌 파일이 있으면 전송이 안 열리고, 사용자가 고르면 열린다**.
 *
 * 왜 생겼나: 처음 구현에서 blockingReason() 은 "미지원/실패/부분검사 파일에 결정이
 * 필요하다" 고 막는데, 정작 그 결정을 **고를 UI 가 없었다**. state.decisions 에 쓰는
 * 코드가 한 줄도 없어서, 지원/미지원을 섞어 첨부하면 전송 버튼이 영영 열리지 않고
 * 사용자는 취소밖에 할 수 없었다. fail-closed 이긴 한데 빠져나갈 길이 없는 막다른 길이다.
 *
 * 실행: node tests/panel-multi-gate.test.js   (exit 0 = 통과)
 */

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const noop = () => {};
const read = (...seg) => fs.readFileSync(path.join(__dirname, '..', ...seg), 'utf8');

const SHARED = read('utils', 'mask-segments.js').replace(/^export /gm, '');
const SRC = read('sidepanel', 'sidepanel.js')
  .replace(/^import .*from '[^']+';$/gm, '');

// ── 스텁 DOM ────────────────────────────────────────────────────────────────
const handlersOf = new Map();

function makeEl(id) {
  const handlers = {};
  const el = {
    id,
    textContent: '', innerHTML: '', hidden: false, value: '', checked: false,
    dataset: {}, style: { setProperty: noop, removeProperty: noop },
    classList: { add: noop, remove: noop, toggle: noop, contains: () => false },
    addEventListener: (ev, fn) => { (handlers[ev] = handlers[ev] || []).push(fn); },
    removeEventListener: noop,
    setAttribute: noop, getAttribute: () => null, removeAttribute: noop,
    appendChild: noop, remove: noop, focus: noop, click: noop, scrollTo: noop,
    scrollIntoView: noop,
    querySelector: () => makeEl('q'), querySelectorAll: () => [],
    getBoundingClientRect: () => ({ top: 0, left: 0, width: 0, height: 0 }),
  };
  handlersOf.set(id, handlers);
  return el;
}

const els = new Map();
const byId = (id) => {
  if (!els.has(id)) els.set(id, makeEl(id));
  return els.get(id);
};

let panelListener = null;
const sent = [];

const sandbox = {
  console, setTimeout, clearTimeout, Promise, Date, Math, JSON, Set, Map,
  Array, Object, String, Number, Boolean, Error, RegExp, Symbol, URLSearchParams,
  CSS: { escape: (s) => String(s) },
  location: { search: '?tabId=101', href: 'chrome-extension://x/sidepanel/sidepanel.html' },
  document: {
    getElementById: byId,
    querySelector: () => makeEl('q'),
    querySelectorAll: () => [],
    addEventListener: noop,
    body: makeEl('body'),
    documentElement: makeEl('html'),
  },
  chrome: {
    runtime: {
      onMessage: { addListener: (l) => { panelListener = l; }, removeListener: noop },
      connect: () => ({ onDisconnect: { addListener: noop } }),
      sendMessage: (m, cb) => { sent.push(m); cb?.({ ok: true }); },
      lastError: null,
    },
  },
  window: { close: noop, addEventListener: noop, parent: { postMessage: noop } },
};
sandbox.window = Object.assign(sandbox.window, sandbox);
sandbox.globalThis = sandbox;

const ctx = vm.createContext(sandbox);
vm.runInContext(SHARED, ctx, { filename: 'mask-segments.js' });
vm.runInContext(SRC, ctx, { filename: 'sidepanel.js' });
assert.ok(panelListener, 'sidepanel 이 onMessage 리스너를 달지 않았다');
assert.strictEqual(typeof sandbox.blockingReason, 'function', 'blockingReason 이 없다');

/** 요소에 걸린 리스너를 직접 부른다(스텁에는 실제 이벤트 전파가 없다). */
const fire = (id, ev, event) => (handlersOf.get(id)?.[ev] || []).forEach(fn => fn(event));
const finishPrompt = (sessionId, seq) => panelListener({
  type: 'PANEL_SCAN_PROMPT', sessionId, tabId: 101, seq,
  prompt: { status: 'done', counts: { pii: 0, injection: 0 } },
});

// ── 배치 시작: 지원 1 + 미지원 1 ────────────────────────────────────────────
panelListener({
  type: 'PANEL_SCAN_INIT',
  sessionId: 's1',
  tabId: 101,
  seq: 1,
  docs: [
    { id: 'f0', fileName: 'ok.pdf', status: 'done', counts: { pii: 1, injection: 0 } },
    { id: 'f1', fileName: 'old.hwp', status: 'unsupported', counts: null },
  ],
});
finishPrompt('s1', 1);

// ── 1) 고르기 전에는 막힌다 ─────────────────────────────────────────────────
const blocked = sandbox.blockingReason();
assert.ok(blocked, '미지원 파일이 있는데 전송이 열려 있다');
assert.ok(blocked.includes('old.hwp'), `어떤 파일이 막는지 알려주지 않는다: ${blocked}`);

// ── 2) 위험한 선택은 한 번으로 확정되지 않는다 ──────────────────────────────
// "검사 없이 원본 포함" 은 되돌릴 수 없는 방향이라 두 번 눌러야 한다.
const clickOn = (docId, action, risky) => fire('doc-actions', 'click', {
  target: {
    closest: () => ({
      dataset: { doc: docId, action },
      classList: { contains: (c) => c === 'risky' && risky },
    }),
  },
});

clickOn('f1', 'original', true);
assert.ok(sandbox.blockingReason(), '위험한 선택이 한 번 눌러 확정됐다');

// ── 3) 두 번째에 확정되고 전송이 열린다 ─────────────────────────────────────
clickOn('f1', 'original', true);
assert.strictEqual(sandbox.blockingReason(), null, '사용자가 골랐는데도 전송이 안 열린다');

const decision = sandbox.buildMultiDecision();
const f1 = decision.files.find(f => f.id === 'f1');
assert.strictEqual(f1.action, 'original', '사용자의 선택이 결정에 안 실렸다');
assert.strictEqual(decision.files.length, 2, 'N개가 N개로 남아야 한다');

// ── 4) 위험하지 않은 선택은 한 번으로 확정된다 ──────────────────────────────
panelListener({
  type: 'PANEL_SCAN_INIT', sessionId: 's2', tabId: 101, seq: 2,
  docs: [{ id: 'f0', fileName: 'broken.pdf', status: 'error', error: '파싱 실패' }],
});
finishPrompt('s2', 2);
assert.ok(sandbox.blockingReason(), '실패 파일이 있는데 전송이 열려 있다');
clickOn('f0', 'exclude', false);
assert.strictEqual(sandbox.blockingReason(), null, '제거를 골랐는데 전송이 안 열린다');
assert.strictEqual(sandbox.buildMultiDecision().files[0].action, 'exclude');

// ── 5) 검사가 안 끝난 파일은 어떤 선택으로도 열리지 않는다 ──────────────────
panelListener({
  type: 'PANEL_SCAN_INIT', sessionId: 's3', tabId: 101, seq: 3,
  docs: [{ id: 'f0', fileName: 'slow.pdf', status: 'scanning' }],
});
clickOn('f0', 'exclude', false);
assert.ok(sandbox.blockingReason(), '검사 중인 파일이 있는데 전송이 열렸다');

// ── 6) 패널을 다시 열면 검토 화면이 돌아온다 ────────────────────────────────
//     다중 세션에는 session.result 가 없다(docs/prompt 메타로 들고 있다). 그 분기가
//     없으면 스냅샷 복구가 renderProgress 로 떨어져 **탭 대신 진행 스피너가 영영
//     남는다** — SW 에는 세션이 멀쩡히 살아 있는데 화면만 못 그리는 상태다.
{
  els.get('view-result').hidden = true;
  els.get('view-progress').hidden = false;

  sandbox.renderMulti({
    kind: 'multi',
    status: 'ready',
    docs: [{ id: 'f0', fileName: 'back.pdf', status: 'done', counts: { pii: 2, injection: 0 } }],
    prompt: { status: 'done', counts: { pii: 0, injection: 0 } },
  });

  assert.strictEqual(els.get('view-result').hidden, false, '복구 후 결과 화면이 안 떴다');
  assert.strictEqual(els.get('view-progress').hidden, true, '복구 후에도 진행 스피너가 남았다');
  assert.strictEqual(els.get('tabs').hidden, false, '복구 후 탭이 안 보인다');
  assert.ok(els.get('tabs').innerHTML.includes('back.pdf'), '복구된 탭에 파일이 없다');
  assert.strictEqual(sandbox.blockingReason(), null, '복구 후 전송이 막혀 있다');
}

// ── 7) 복구 때 사용자의 선택까지 되살아난다 ─────────────────────────────────
//     검사 결과만 돌아오고 선택이 초기화되면, 긴 문서에서 수십 개를 하나씩 풀어 둔
//     사람에게는 처음부터 다시 하라는 뜻이다.
{
  sandbox.renderMulti({
    kind: 'multi',
    status: 'ready',
    docs: [
      { id: 'f0', fileName: 'a.pdf', status: 'done', counts: { pii: 3, injection: 0 } },
      { id: 'f1', fileName: 'b.hwp', status: 'unsupported', counts: null },
    ],
    prompt: { status: 'done', counts: { pii: 0, injection: 0 } },
    draft: {
      unmaskedKeys: ['f0:0', 'f0:2'],
      decisions: { f1: 'exclude' },
      activeTab: 'prompt',
    },
  });

  // 막혀 있던 미지원 파일의 선택이 되살아나 전송이 열려 있어야 한다.
  assert.strictEqual(sandbox.blockingReason(), null, '복구된 선택이 반영되지 않아 전송이 막혔다');
  // 어떤 문서가 나가고 빠지는지 이름으로 보인다(예전엔 "1개 전송 · 1개 제외")
  assert.strictEqual(els.get('mask-summary').textContent, '전송: a.pdf · 제외: b.hwp · 1건 마스킹');
  assert.strictEqual(els.get('doc-type').textContent, 'a.pdf, b.hwp + 프롬프트 검토');

  const d = sandbox.buildMultiDecision();
  assert.strictEqual(d.files.find(f => f.id === 'f1').action, 'exclude', '파일별 선택이 안 돌아왔다');
  assert.strictEqual(
    (d.files.find(f => f.id === 'f0').unmaskedKeys || []).join(','), 'f0:0,f0:2',
    '해제한 항목이 안 돌아왔다',
  );
}

// ── 8) 첫 파일 결과부터 점진 공개하되, 전송은 전체 검사가 끝나야 열린다 ─────────
//     INIT 직후에는 별똥별 대기 화면을 유지한다. 파일 하나가 끝나면 그 결과를 바로
//     보여 주고, 나머지는 끝나는 대로 합계와 탭 상태에 추가한다. 검토를 먼저 시작할 수
//     있어도 배치 전송은 원자적이므로 마지막 검사 전에는 버튼이 열리면 안 된다.
{
  const shown = () => ({
    progress: !els.get('view-progress').hidden,
    result: !els.get('view-result').hidden,
    footer: !els.get('footer').hidden,
  });
  const msg = (m) => panelListener({ sessionId: 's8', tabId: 101, seq: 8, ...m });

  msg({
    type: 'PANEL_SCAN_INIT',
    docs: [
      { id: 'f0', fileName: 'a.pdf', status: 'pending' },
      { id: 'f1', fileName: 'b.docx', status: 'pending' },
    ],
  });
  assert.deepStrictEqual(shown(), { progress: true, result: false, footer: false },
    '검사가 막 시작됐는데 대기 화면 대신 탭이 떴다');
  assert.strictEqual(els.get('progress-fill').style.width, '2%');
  assert.strictEqual(els.get('btn-send').disabled, true, '검사 시작 직후 전송 버튼이 열렸다');

  msg({
    type: 'PANEL_SCAN_STATE', kind: 'multi', status: 'scanning',
    docs: [
      { id: 'f0', fileName: 'a.pdf', status: 'scanning' },
      { id: 'f1', fileName: 'b.docx', status: 'pending' },
    ],
    prompt: { status: 'pending', counts: null },
    current: { type: 'status', phase: 'reading', itemId: 'f0', label: '파일 읽는 중…' },
  });
  assert.strictEqual(els.get('progress-title').textContent, '파일 읽는 중…');
  assert.strictEqual(els.get('progress-sub').textContent, 'a.pdf');

  msg({ type: 'PANEL_PROGRESS', itemId: 'f0', event: { type: 'step', step: 2, label: 'PII 탐지 중…' } });
  assert.strictEqual(els.get('progress-title').textContent, 'PII 탐지 중…');
  assert.strictEqual(els.get('progress-sub').textContent, 'a.pdf', '지금 보는 파일이 부제에 없다');
  assert.strictEqual(els.get('progress-fill').style.width, '15%', '현재 항목 단계가 전체 진행률에 반영되지 않았다');

  msg({ type: 'PANEL_SCAN_PROMPT', prompt: { status: 'done', counts: { pii: 1, injection: 0 } } });
  assert.ok(shown().progress, '파일 결과가 하나도 없는데 검토 화면이 먼저 떴다');

  // 두 번째 파일이 먼저 끝나는 경우에도 완료된 파일을 즉시 선택해 보여 준다.
  msg({ type: 'PANEL_SCAN_ITEM', doc: { id: 'f1', fileName: 'b.docx', status: 'done', counts: { pii: 0, injection: 1 } } });
  assert.deepStrictEqual(shown(), { progress: false, result: true, footer: true },
    '첫 파일 결과가 왔는데 검토 화면으로 넘어가지 않았다');
  assert.strictEqual(vm.runInContext('state.activeTab', ctx), 'f1', '먼저 끝난 파일이 자동 선택되지 않았다');
  assert.strictEqual(els.get('counts').textContent, 'PII 1건 | INJECTION 1건 탐지');
  assert.strictEqual(els.get('btn-send').disabled, true, '다른 파일이 검사 중인데 전송 버튼이 열렸다');
  // 여러 문서는 개수 대신 이름으로 — 헤더에는 넣은 문서, 아래 요약에는 문서별 상태
  assert.strictEqual(els.get('doc-type').textContent, 'a.pdf, b.docx · 검사 중');
  assert.strictEqual(els.get('mask-summary').textContent, '검사 완료: b.docx · 검사 중: a.pdf · 2건 마스킹');
  assert.strictEqual(els.get('live-progress').hidden, false, '첫 결과 뒤 남은 검사의 진행 표시가 사라졌다');
  assert.strictEqual(els.get('live-progress-text').textContent, 'a.pdf · PII 탐지 중…');

  msg({ type: 'PANEL_SCAN_ITEM', doc: { id: 'f0', fileName: 'a.pdf', status: 'done', counts: { pii: 2, injection: 0 } } });
  assert.strictEqual(vm.runInContext('state.activeTab', ctx), 'f1', '후속 결과가 사용자가 보던 탭을 빼앗았다');
  assert.strictEqual(els.get('counts').textContent, 'PII 3건 | INJECTION 1건 탐지', '헤더가 검사 중에 머물렀다');
  assert.strictEqual(els.get('btn-send').disabled, false, '전체 검사가 끝났는데 전송 버튼이 열리지 않았다');
  assert.strictEqual(els.get('live-progress').hidden, true, '전체 검사 뒤 진행 표시가 남았다');

  msg({ type: 'PANEL_SCAN_ITEM', doc: { id: 'f1', fileName: 'b.docx', status: 'scanning' } });
  assert.ok(shown().result, '재시도 한 번에 대기 화면으로 되돌아갔다');
  assert.strictEqual(els.get('btn-send').disabled, true, '재검사가 시작됐는데 전송 버튼이 열려 있다');

  // 루프가 끝났다고 알리면(DONE) 남은 게 있어도 탭에서 고르게 한다.
  panelListener({
    type: 'PANEL_SCAN_INIT', sessionId: 's9', tabId: 101, seq: 9,
    docs: [{ id: 'f0', fileName: 'stuck.pdf', status: 'pending' }],
  });
  assert.ok(shown().progress);
  panelListener({
    type: 'PANEL_SCAN_DONE', sessionId: 's9', tabId: 101, seq: 9,
    docs: [{ id: 'f0', fileName: 'stuck.pdf', status: 'pending' }],
    prompt: { status: 'error', counts: null },
  });
  assert.ok(shown().result, 'DONE 이 왔는데 대기 화면에 갇혔다');
  assert.strictEqual(els.get('btn-send').disabled, true, '미완료 파일이 남은 DONE 상태에서 전송 버튼이 열렸다');
}

// ── 9) 그림을 빼고 보내는 문서는 다중 검사 요약에도 단독 검사와 같은 문구로 보인다 ──────────────
//     v0.2.63 Windows 재확인 M1: 메모리가 모자라 그림을 뺐는데(엔진 memoryShort.kind=commit) 파일+프롬프트(늘 다중)
//     패널 요약은 "전송: base_with_image.docx" 뿐이었다 — 문구가 단독 검사 분기에만 있었다.
{
  const summary = (docs) => {
    sandbox.renderMulti({ kind: 'multi', status: 'ready', docs, prompt: { status: 'done', counts: { pii: 0, injection: 0 } } });
    return els.get('mask-summary').textContent;
  };
  const doc = (id, fileName, extra = {}) => ({ id, fileName, status: 'done', counts: { pii: 0, injection: 0 }, ...extra });
  const memory = { imagesRemoved: 1, imagesRemovedReason: 'memory', imagesRemovedMemory: 'commit' };

  assert.strictEqual(summary([doc('f0', 'base_with_image.docx', memory)]),
    '전송: base_with_image.docx · 그림 1개는 빼고 보냄(PC 메모리 부족 — Campfire 앱에서 해결 방법을 볼 수 있습니다)',
    '메모리 부족으로 그림을 뺐는데 요약에 안내가 없다');
  assert.strictEqual(summary([doc('f0', 'off.docx', { imagesRemoved: 2, imagesRemovedReason: 'off' })]),
    '전송: off.docx · 그림 2개는 빼고 보냄(이미지 검사 꺼짐)');
  // 여러 문서면 그림을 뺀 문서마다 이름을 붙여 잇는다
  assert.strictEqual(summary([doc('f0', 'a.docx', memory), doc('f1', 'b.pdf'), doc('f2', 'c.pptx', { imagesRemoved: 3 })]),
    '전송: a.docx, b.pdf, c.pptx · a.docx 그림 1개는 빼고 보냄(PC 메모리 부족 — Campfire 앱에서 해결 방법을 볼 수 있습니다)'
    + ' · c.pptx 그림 3개는 빼고 보냄(이미지 검사 꺼짐)');
  assert.strictEqual(summary([doc('f0', 'plain.docx')]), '전송: plain.docx', '그림을 빼지 않은 문서에 문구가 붙었다');
}

console.log('panel-multi-gate.test.js: 9개 블록 통과');
