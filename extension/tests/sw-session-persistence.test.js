/**
 * sw-session-persistence.test.js
 *
 * MV3 서비스워커가 재시작돼도 검토 세션이 살아남아야 한다는 계약을 지킨다.
 *
 * 왜 생겼나 (2026-09-06 실사용 재현):
 *   사용자가 검토 패널에서 [전송]을 눌렀는데 "이 검토는 만료되었습니다" 가 떴다.
 *   세션은 멀쩡히 진행 중이었다 — 사라진 건 SW 쪽 기억이다.
 *
 *   MV3 서비스워커는 할 일이 없으면 30초쯤 뒤 종료된다. 그런데 "사용자가 검토
 *   화면을 보고 있는 시간" 이 정확히 그 할 일 없는 구간이라, 결정을 누르기 전에
 *   SW 가 죽는 일이 흔하다. sessions 는 메모리 Map 이었으므로 그때 통째로
 *   사라졌고, 뒤늦게 도착한 PANEL_DECISION 은 "모르는 세션" 이 됐다.
 *
 *   같은 원인으로 sessionSeq 도 0 으로 되돌아갔다. 그러면 다음 세션이 seq=1 을
 *   받는데 패널은 이미 더 큰 seq 를 들고 있어 새 결과를 "지나간 세션" 으로
 *   무시한다 — 패널에 옛 화면이 남고, 그 옛 sessionId 로 결정을 보내 다시
 *   만료가 뜬다. 두 증상이 한 원인에서 나온다.
 *
 * 실행: node tests/sw-session-persistence.test.js   (exit 0 = 통과)
 */

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const SW_PATH = path.join(__dirname, '..', 'background', 'service-worker.js');
const RAW = fs.readFileSync(SW_PATH, 'utf8');

// import 문만 걷어내고(그 심볼들은 아래에서 스텁으로 넣는다) 평범한 스크립트로 돌린다.
const SRC = RAW
  .replace(/^import\s+\{[\s\S]*?\}\s+from\s+'[^']+';\s*$/gm, '')
  .replace(/^import\s+[^\n]*from\s+'[^']+';\s*$/gm, '');

const noop = () => {};

/** 서비스워커를 한 번 "기동" 한다. storage 는 호출자가 넘긴 객체를 그대로 쓰므로,
 *  같은 객체로 두 번 기동하면 재시작을 재현할 수 있다. */
function bootWorker(storageBacking) {
  const listeners = [];
  const tabsSent = [];

  const sandbox = {
    console: { log: noop, warn: noop, error: noop, debug: noop },
    setTimeout, clearTimeout, setInterval, clearInterval,
    Promise, Date, Math, JSON, Set, Map, Object, Array, String, Number, Boolean,
    Error, RegExp, Symbol, URL, URLSearchParams, TextEncoder, TextDecoder,
    fetch: () => Promise.reject(new Error('네트워크는 이 테스트의 관심사가 아니다')),
    atob: (b) => Buffer.from(b, 'base64').toString('binary'),
    btoa: (b) => Buffer.from(b, 'binary').toString('base64'),
    crypto: { randomUUID: () => 'uuid-' + Math.random().toString(16).slice(2) },

    // import 로 들어오던 심볼들
    wrapMaskedFile: noop,
    REMOTE_URL: 'https://example.invalid',
    LOCAL_HOST: '127.0.0.1',
    BASE_PORT: 48200,
    PORT_SCAN_COUNT: 1,
    HEALTH_TIMEOUT_MS: 100,
    isOurEngine: () => false,
    CACHE_KEY: 'k',

    chrome: {
      runtime: {
        lastError: null,
        onMessage: { addListener: (fn) => listeners.push(fn) },
        onInstalled: { addListener: noop },
        onStartup: { addListener: noop },
        sendMessage: () => Promise.resolve(),
        getURL: (p) => 'chrome-extension://x/' + p,
      },
      tabs: {
        sendMessage: (tabId, msg) => { tabsSent.push({ tabId, msg }); return Promise.resolve(); },
        query: () => Promise.resolve([]),
      },
      action: { setBadgeText: noop, setBadgeBackgroundColor: noop },
      sidePanel: { setOptions: () => Promise.resolve(), open: () => Promise.resolve(), close: () => Promise.resolve() },
      storage: {
        session: {
          get: (key) => Promise.resolve(
            key in storageBacking ? { [key]: storageBacking[key] } : {},
          ),
          set: (obj) => { Object.assign(storageBacking, obj); return Promise.resolve(); },
        },
        local: { get: () => Promise.resolve({}), set: () => Promise.resolve() },
      },
    },
  };
  sandbox.self = sandbox;
  sandbox.globalThis = sandbox;

  vm.runInContext(SRC, vm.createContext(sandbox), { filename: 'service-worker.js' });
  assert.ok(listeners.length, 'SW 가 onMessage 리스너를 달지 않았다 — 하네스가 스크립트를 건너뛴 것');

  /** 메시지 하나를 보내고 sendResponse 로 돌아온 값을 받는다. */
  const send = (message, sender = {}) => new Promise((resolve) => {
    let settled = false;
    const respond = (res) => { if (!settled) { settled = true; resolve(res); } };
    for (const fn of listeners) fn(message, sender, respond);
    // 응답이 영영 안 오면 테스트가 멎지 않게 끊는다.
    setTimeout(() => respond(undefined), 1500);
  });

  return { send, tabsSent };
}

const SESSION_KEY = 'campfireSessionState';

/** 앞선 기동에서 검사가 끝난 채 사용자의 결정을 기다리던 상태. */
const pendingState = () => ({
  [SESSION_KEY]: {
    sessions: {
      S1: {
        tabId: 7, kind: 'prompt', status: 'ready', progress: [],
        result: { maskedText: '가려진 텍스트' }, error: null,
        meta: { textPreview: '...' }, seq: 3,
      },
    },
    activeSessionId: 'S1',
    sessionSeq: 3,
  },
});

(async () => {
  // ── (1) 재시작 뒤에도 그 세션의 결정이 원래 탭으로 전달돼야 한다 ────────────
  {
    const storage = pendingState();
    const w = bootWorker(storage); // 재시작 = 메모리는 비었고 storage 만 남은 상태

    const res = await w.send(
      { type: 'PANEL_DECISION', sessionId: 'S1', tabId: 7, decision: { action: 'masked', maskedText: '가려진 텍스트' } },
      {}, // 네이티브 사이드패널이라 sender.tab 이 없다
    );

    assert.ok(
      res && res.ok === true,
      'SW 재시작 뒤 멀쩡한 세션의 결정이 만료로 처리됐다 — 사용자는 [전송]을 눌렀는데 '
      + '"이 검토는 만료되었습니다" 만 보게 된다',
    );
    const routed = w.tabsSent.find(t => t.msg?.type === 'PANEL_DECISION');
    assert.ok(routed, '결정이 원래 탭의 content.js 로 중계되지 않았다 — 아무것도 전송되지 않는다');
    assert.strictEqual(routed.tabId, 7, '결정이 엉뚱한 탭으로 갔다');
    assert.strictEqual(routed.msg.decision.action, 'masked');
  }

  // ── (2) 정말 모르는 세션은 여전히 만료로 처리해야 한다 ──────────────────────
  //
  // (1) 을 통과시키려고 전부 ok:true 로 만들어 버리면, 낡은 패널의 결정이 엉뚱한
  // 세션에 적용되는 더 나쁜 실패로 바뀐다.
  {
    const storage = pendingState();
    const w = bootWorker(storage);

    const res = await w.send(
      { type: 'PANEL_DECISION', sessionId: '없는세션', tabId: 7, decision: { action: 'cancel' } },
      {},
    );
    assert.ok(res && res.ok === false, '모르는 세션인데 접수된 것처럼 응답했다');
    assert.strictEqual(res.reason, 'stale-session');
  }

  // ── (3) seq 는 재시작을 넘어 단조 증가해야 한다 ─────────────────────────────
  //
  // 0 으로 되돌아가면 패널이 새 결과를 "지나간 세션" 으로 무시한다(#134 의 필터).
  {
    const storage = pendingState();
    const w = bootWorker(storage);

    // 아무 메시지나 하나 보내면 hydrate 가 돌고, 그 뒤 상태가 다시 저장된다.
    await w.send({ type: 'PANEL_READY', tabId: 7 }, {});
    await new Promise(r => setTimeout(r, 50));

    assert.ok(
      (storage[SESSION_KEY].sessionSeq || 0) >= 3,
      'seq 가 재시작 뒤 되돌아갔다 — 다음 검토 결과가 패널에서 통째로 무시된다',
    );
  }

  // ── (4) 재시작 뒤에도 패널이 스냅샷을 되찾을 수 있어야 한다 ─────────────────
  {
    const storage = pendingState();
    const w = bootWorker(storage);

    const res = await w.send({ type: 'PANEL_READY', tabId: 7 }, {});
    assert.ok(res?.ok, 'PANEL_READY 에 응답하지 않았다');
    assert.strictEqual(
      res.sessionId, 'S1',
      'SW 재시작 뒤 패널이 진행 중이던 검토를 되찾지 못한다 — 빈 패널이 뜬다',
    );
  }

  console.log('sw-session-persistence ok');
})().catch((e) => { console.error(e); process.exit(1); });
