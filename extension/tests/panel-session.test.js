/**
 * panel-session.test.js
 *
 * 같은 탭에서 **나중에 시작된 검토 세션이 이긴다**는 계약을 지킨다.
 *
 * 왜 생겼나 (2026-09-06 실사용 재현):
 *   사용자가 검토를 결정 없이 둔 채 다음 작업을 하면, 그 다음 스캔 결과가 화면에
 *   전혀 뜨지 않았다. 패널은 로드될 때 한 번만 세션을 받아오고(pullSnapshot) 그
 *   뒤엔 브로드캐스트로만 갱신되는데, 예전 필터가 "추적 중인 세션이 아니면 무조건
 *   무시" 였기 때문이다 — 옛 세션 id 를 계속 들고 있으니 새 세션의 PROGRESS/RESULT
 *   가 전부 버려졌다.
 *
 *   실측: 파일 스캔이 정상적으로 끝나 엔진에 job 까지 기록됐는데(PII 5건) 패널에는
 *   이전 검토 화면이 그대로 남아 있었다. 사용자에겐 "검사가 안 된다" 로 보이지만
 *   실제로는 검사는 됐고 화면만 막힌 것이라, 증상과 원인이 어긋나 찾기 어렵다.
 *
 * 반대 방향도 함께 지킨다: 지나간 세션의 **뒤늦은** 메시지가 새 결과를 덮어쓰면
 * 안 된다. sessionId 는 UUID 라 순서를 알 수 없어 SW 가 매기는 seq 로 판단한다.
 *
 * 실행: node tests/panel-session.test.js   (exit 0 = 통과)
 */

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const SRC = fs.readFileSync(path.join(__dirname, '..', 'sidepanel', 'sidepanel.js'), 'utf8');
const noop = () => {};

/** sidepanel.js 를 스텁 DOM 위에 올리고 조작 손잡이를 돌려준다. */
function loadPanel(opts = {}) {
  const listeners = [];
  const sent = [];
  // PANEL_DECISION 에 SW 가 뭐라고 답하는지 — 기본은 정상 접수.
  const decisionResponse = opts.decisionResponse || { ok: true };
  let closed = false;

  const makeEl = () => {
    const handlers = {};
    const el = {
      textContent: '', innerHTML: '', hidden: false, value: '', checked: false,
      dataset: {}, style: { setProperty: noop, removeProperty: noop },
      classList: { add: noop, remove: noop, toggle: noop, contains: () => false },
      addEventListener: (ev, fn) => { (handlers[ev] = handlers[ev] || []).push(fn); },
      removeEventListener: noop,
      _fire: (ev) => (handlers[ev] || []).forEach(fn => fn({ preventDefault: noop, stopPropagation: noop })),
      setAttribute: noop, getAttribute: () => null, removeAttribute: noop,
      appendChild: noop, remove: noop, focus: noop, click: noop, scrollTo: noop,
      querySelector: () => makeEl(), querySelectorAll: () => [],
      getBoundingClientRect: () => ({ top: 0, left: 0, width: 0, height: 0 }),
    };
    return el;
  };

  const els = new Map();
  const byId = (id) => {
    if (!els.has(id)) els.set(id, makeEl());
    return els.get(id);
  };

  const sandbox = {
    console, setTimeout, clearTimeout, Promise, Date, Math, JSON, Set, Map,
    Array, Object, String, Number, Boolean, Error, RegExp, Symbol,
    URLSearchParams,
    location: { search: '?tabId=101', href: 'chrome-extension://x/sidepanel/sidepanel.html' },
    document: {
      getElementById: byId,
      querySelector: () => makeEl(),
      querySelectorAll: () => [],
      addEventListener: noop,
      body: makeEl(),
      documentElement: makeEl(),
    },
    chrome: {
      runtime: {
        lastError: null,
        onMessage: { addListener: (fn) => listeners.push(fn) },
        // PANEL_READY 응답은 "세션 없음" 으로 둔다 — 이 테스트의 관심사는 로드 이후의
        // 브로드캐스트 처리이지 스냅샷 복구가 아니다.
        sendMessage: (msg, cb) => {
          sent.push(msg);
          if (!cb) return;
          cb(msg.type === 'PANEL_DECISION' ? decisionResponse : { ok: true, tabId: 101 });
        },
      },
    },
    close: () => { closed = true; },
    parent: { postMessage: noop },
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;

  vm.runInContext(SRC, vm.createContext(sandbox), { filename: 'sidepanel.js' });
  assert.ok(listeners.length, 'sidepanel 이 onMessage 리스너를 달지 않았다 — 하네스가 스크립트를 건너뛴 것');

  const send = (msg) => listeners.forEach((fn) => fn(msg));
  /** 지금 화면에 그려진 세션의 파일명 — 어느 결과가 렌더됐는지 보는 창. */
  const rendered = () => byId('doc-name').textContent;

  /** 버튼을 실제로 눌러 본다 — 결정 경로는 클릭 핸들러를 통해서만 들어간다. */
  const click = (id) => byId(id)._fire('click');
  const text = (id) => byId(id).textContent;

  return { send, rendered, sent, click, text, closed: () => closed };
}

const TAB = 101;
const result = (n) => ({
  originalText: `문서 ${n}`, maskedText: `문서 ${n}`,
  piiItems: [], injectionItems: [], scanStatus: 'ok',
  stats: { piiCount: 0, injectionCount: 0, originalLength: 4 },
});
const meta = (n) => ({ fileName: `file-${n}.docx` });

// ── (1) 방치된 세션이 있어도 새 세션 결과가 그려져야 한다 (이 수정의 핵심) ──
{
  const p = loadPanel();
  // 세션 A 가 결과까지 오고, 사용자가 결정하지 않은 채 방치한다.
  p.send({ type: 'PANEL_PROGRESS', tabId: TAB, sessionId: 'A', seq: 1, event: { type: 'step', step: 1 } });
  p.send({ type: 'PANEL_RESULT', tabId: TAB, sessionId: 'A', seq: 1, kind: 'file', result: result('A'), meta: meta('A') });
  assert.strictEqual(p.rendered(), 'file-A.docx');

  // 같은 탭에서 새 스캔(B)이 시작된다.
  p.send({ type: 'PANEL_PROGRESS', tabId: TAB, sessionId: 'B', seq: 2, event: { type: 'step', step: 1 } });
  p.send({ type: 'PANEL_RESULT', tabId: TAB, sessionId: 'B', seq: 2, kind: 'file', result: result('B'), meta: meta('B') });
  assert.strictEqual(
    p.rendered(), 'file-B.docx',
    '방치된 세션 때문에 새 스캔 결과가 화면에 안 뜬다 — 사용자에겐 "검사가 안 된다" 로 보인다',
  );
}

// ── (2) 지나간 세션의 뒤늦은 메시지가 새 결과를 덮으면 안 된다 ──────────────
{
  const p = loadPanel();
  p.send({ type: 'PANEL_PROGRESS', tabId: TAB, sessionId: 'A', seq: 1, event: { type: 'step', step: 1 } });
  p.send({ type: 'PANEL_RESULT', tabId: TAB, sessionId: 'B', seq: 2, kind: 'file', result: result('B'), meta: meta('B') });
  assert.strictEqual(p.rendered(), 'file-B.docx');

  // A 의 결과가 뒤늦게 도착 — 이미 지나간 세션이므로 무시해야 한다.
  p.send({ type: 'PANEL_RESULT', tabId: TAB, sessionId: 'A', seq: 1, kind: 'file', result: result('A'), meta: meta('A') });
  assert.strictEqual(
    p.rendered(), 'file-B.docx',
    '지나간 세션의 뒤늦은 결과가 새 결과를 덮어썼다',
  );
}

// ── (3) 다른 탭 대상 브로드캐스트는 여전히 무시한다 (기존 방어선) ───────────
{
  const p = loadPanel();
  p.send({ type: 'PANEL_RESULT', tabId: TAB, sessionId: 'A', seq: 1, kind: 'file', result: result('A'), meta: meta('A') });
  p.send({ type: 'PANEL_RESULT', tabId: 999, sessionId: 'X', seq: 9, kind: 'file', result: result('X'), meta: meta('X') });
  assert.strictEqual(
    p.rendered(), 'file-A.docx',
    '다른 탭의 결과가 이 패널에 새어 들어왔다 — 탭 스코핑이 무너졌다',
  );
}

// ── (4) seq 가 없는(옛 SW) 메시지도 깨지지 않는다 ───────────────────────────
{
  const p = loadPanel();
  p.send({ type: 'PANEL_RESULT', tabId: TAB, sessionId: 'A', kind: 'file', result: result('A'), meta: meta('A') });
  assert.strictEqual(p.rendered(), 'file-A.docx');
  // seq 가 없으면 예전처럼 sessionId 로 막는다.
  p.send({ type: 'PANEL_RESULT', tabId: TAB, sessionId: 'B', kind: 'file', result: result('B'), meta: meta('B') });
  assert.strictEqual(
    p.rendered(), 'file-A.docx',
    'seq 없는 경로의 기존 동작(sessionId 일치 요구)이 바뀌었다',
  );
}

// ── (5) 전달되지 못한 결정을 조용히 삼키면 안 된다 ──────────────────────────
//
// 2026-09-06 실사용 재현: [전송]을 눌렀고 패널은 닫혔는데 사이트로는 아무것도
// 나가지 않았다. SW 가 모르는 sessionId 였고(낡은 패널) PANEL_DECISION 은 조용히
// 버려졌는데, 패널이 응답을 보지 않고 그냥 닫혀서 성공과 구분되지 않았다.
// 그동안 원래 기다리던 세션은 10분 타임아웃까지 그 탭의 전송을 전부 삼켰다.
{
  const p = loadPanel({ decisionResponse: { ok: false, reason: 'stale-session' } });
  p.send({ type: 'PANEL_RESULT', tabId: TAB, sessionId: 'A', seq: 1, kind: 'file', result: result('A'), meta: meta('A') });

  p.click('btn-cancel'); // 승인/취소 모두 sendDecision 한 경로를 탄다

  const decision = p.sent.find(m => m.type === 'PANEL_DECISION');
  assert.ok(decision, '결정이 SW 로 전송되지 않았다');
  assert.match(
    p.text('err-title'), /만료/,
    '전달되지 못한 결정을 조용히 삼켰다 — 사용자에겐 눌린 것처럼 보이고 아무것도 전송되지 않는다',
  );
  assert.strictEqual(
    decision.tabId, TAB,
    'tabId 를 같이 보내지 않으면 네이티브 패널의 결정에서 SW 가 탭을 못 찾는다(sender.tab 이 없다)',
  );
}

// ── (6) 정상 접수된 결정은 만료 화면을 띄우지 않는다 ────────────────────────
{
  const p = loadPanel();
  p.send({ type: 'PANEL_RESULT', tabId: TAB, sessionId: 'A', seq: 1, kind: 'file', result: result('A'), meta: meta('A') });
  p.click('btn-cancel');
  assert.ok(
    !/만료/.test(p.text('err-title')),
    '정상 결정인데 만료 화면이 떴다',
  );
}

console.log('panel-session ok');
