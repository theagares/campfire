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

  const d = sandbox.buildMultiDecision();
  assert.strictEqual(d.files.find(f => f.id === 'f1').action, 'exclude', '파일별 선택이 안 돌아왔다');
  assert.strictEqual(
    (d.files.find(f => f.id === 'f0').unmaskedKeys || []).join(','), 'f0:0,f0:2',
    '해제한 항목이 안 돌아왔다',
  );
}

// ── 8) 다중 검사가 끝나면 상단 상태도 완료로 바뀐다 ─────────────────────────
//     예전에는 파일/프롬프트 탭과 전송 버튼은 완료 상태인데 상단만 영원히
//     `검사 중…`으로 남아 사용자에게 서로 모순된 상태를 보여줬다.
{
  panelListener({
    type: 'PANEL_SCAN_INIT', sessionId: 's4', tabId: 101, seq: 4,
    docs: [{ id: 'f0', fileName: 'status.pdf', status: 'scanning', counts: null }],
  });
  assert.strictEqual(els.get('counts').textContent, '검사 중…', '검사 중 상태가 표시되지 않는다');

  panelListener({
    type: 'PANEL_SCAN_ITEM', sessionId: 's4', tabId: 101, seq: 4,
    doc: { id: 'f0', fileName: 'status.pdf', status: 'done', counts: { pii: 2, injection: 1 } },
  });
  assert.strictEqual(els.get('counts').textContent, '검사 중…', '프롬프트가 남았는데 완료로 표시됐다');

  panelListener({
    type: 'PANEL_SCAN_PROMPT', sessionId: 's4', tabId: 101, seq: 4,
    prompt: { status: 'done', counts: { pii: 1, injection: 0 } },
  });
  assert.strictEqual(
    els.get('counts').textContent,
    '검사 완료 · PII 3건 | INJECTION 1건 탐지',
    '모든 검사가 끝난 뒤 상단 완료 요약이 갱신되지 않았다',
  );
}

console.log('panel-multi-gate.test.js: 8개 블록 통과');
