/**
 * interceptor-bridge.test.js
 *
 * MAIN world(interceptor.js)가 "isolated world에서 왔다"고 주장하는 메시지를 그대로
 * 믿으면 안 된다는 계약을 지킨다.
 *
 * 왜 생겼나: interceptor.js 는 manifest 의 world:"MAIN" 으로 주입돼 **페이지와 같은 JS
 * 컨텍스트**에서 돈다. 그래서 페이지 스크립트도 window.postMessage 로 같은 모양의
 * 메시지를 보낼 수 있는데, 예전에는 두 메시지가 아무 검증 없이 처리됐다:
 *
 *   - UPS_PROTECTION_STATE      → 한 줄로 XHR/fetch/drop/file input 훅을 전부 끌 수 있었다.
 *                                 사용자 팝업 토글은 ON 인 채라 꺼진 걸 알 방법이 없다.
 *   - UPS_CONTENT_APPROVE_BATCH → 이 목록에 든 파일은 업로드 훅이 검사 없이 통과시킨다.
 *                                 등록 대조가 파일명만이라, 사용자가 방금 고른 파일명을
 *                                 아는 페이지(자기 input 이니 안다)가 그 이름만 등록하면
 *                                 마스킹되지 않은 원본이 그대로 올라갔다.
 *
 * 한계도 같이 박아둔다: 토큰은 content.js 가 postMessage(..., '*') 로 보내므로 페이지도
 * 읽을 수 있다. 이 검사는 "토큰을 줍지 않은 공격"까지만 막는 방어선이지 경계선이 아니다.
 * 근본 해결은 보안 판단을 MAIN world 밖에 두는 것이고, 그건 별도 작업이다.
 *
 * 실행: node tests/interceptor-bridge.test.js   (exit 0 = 통과)
 */

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');
const { webcrypto } = require('crypto');

const SRC = fs.readFileSync(path.join(__dirname, '..', 'content', 'interceptor.js'), 'utf8');
const TOKEN = 'real-bridge-token-abc';
const noop = function () {};
const settle = () => new Promise((r) => setTimeout(r, 30));

/** interceptor.js 를 새 샌드박스에 올리고 조작 손잡이를 돌려준다. */
function load({ proxyMarked = false, host = 'chatgpt.com' } = {}) {
  const win = new Map();
  const doc = new Map();
  const origFetchCalls = [];
  const posted = [];
  const store = Object.create(null);
  let hasProxyMark = proxyMarked;
  let skew = 0; // advance(ms) 로 interceptor 가 보는 시계만 앞당긴다
  const RealDate = Date;
  const SkewDate = class extends RealDate { static now() { return RealDate.now() + skew; } };

  Object.assign(store, {
    console, setTimeout, clearTimeout, setInterval, clearInterval, queueMicrotask,
    Promise, Date: SkewDate, Math, JSON, Set, Map, WeakSet, WeakMap, Array, Object, String,
    Number, Boolean, Error, TypeError, RegExp, Symbol, Proxy, Reflect,
    parseInt, parseFloat, isNaN, Uint32Array, Uint8Array, ArrayBuffer, crypto: webcrypto,
    btoa: (s) => Buffer.from(s, 'binary').toString('base64'),
    atob: (s) => Buffer.from(s, 'base64').toString('binary'),
  });

  store.MutationObserver = function () {
    this.observe = noop; this.disconnect = noop; this.takeRecords = () => [];
  };
  class BlobStub { constructor() { this.size = 2048; this.type = 'application/pdf'; } }
  // 실제 ArrayBuffer 를 돌려줘야 한다 — noop 이면 fileToBase64 가 throw 하고,
  // 그 에러 경로가 요청을 그대로 통과시켜 모든 케이스가 '통과' 로 보인다.
  BlobStub.prototype.arrayBuffer = function () { return Promise.resolve(new ArrayBuffer(this.size)); };
  store.Blob = BlobStub;
  store.File = class extends BlobStub {
    constructor(name, type) { super(); this.name = name || 'secret.pdf'; if (type) this.type = type; }
  };
  store.FileReader = function () {};
  store.FileReader.prototype = {
    readAsArrayBuffer: noop, readAsDataURL: noop, readAsBinaryString: noop,
  };
  store.XMLHttpRequest = function () {};
  store.XMLHttpRequest.prototype = { open: noop, send: noop };
  store.Element = { prototype: { attachShadow: noop } };
  store.HTMLInputElement = { prototype: {} };

  const el = () => ({
    addEventListener: noop, removeEventListener: noop, dispatchEvent: () => true,
    querySelectorAll: () => [], querySelector: () => null, matches: () => false,
    closest: () => null, contains: () => false, appendChild: noop, removeChild: noop,
    parentElement: null, style: {}, nodeType: 1,
  });
  store.document = Object.assign(el(), {
    nodeType: 9, documentElement: el(), body: el(), createElement: el,
    addEventListener: (t, f) => { const a = doc.get(t) || []; a.push(f); doc.set(t, a); },
  });
  store.location = { hostname: host, href: `https://${host}/` };
  // 훅이 원본으로 붙들어 두는 fetch. 여기 도착했다 = 인터셉트 없이 통과했다.
  store.fetch = function (...args) {
    origFetchCalls.push(args);
    return Promise.resolve({
      ok: true,
      url: String(args[0]),
      headers: { get: (name) => (
        hasProxyMark && String(name).toLowerCase() === 'x-campfire-proxy' ? '1' : null
      ) },
    });
  };

  // 미정의 전역은 생성자 모양(대문자 시작)만 자동 스텁한다. 소문자까지 스텁하면
  // interceptor.js 첫 줄의 `if (window.__securedocLoaded) return;` 가 truthy 로 잡혀
  // 스크립트 전체가 조용히 건너뛰어진다 — 빈 테스트가 그대로 통과해버린다.
  const sandbox = new Proxy(store, {
    has: () => true,
    get(t, k) {
      if (k in t) return t[k];
      if (typeof k !== 'string' || !/^[A-Z]/.test(k)) return undefined;
      const f = function () {}; f.prototype = {}; t[k] = f; return f;
    },
    set(t, k, v) { t[k] = v; return true; },
  });
  store.window = sandbox;
  store.globalThis = sandbox;
  store.self = sandbox;
  store.top = sandbox;
  store.addEventListener = (t, f) => { const a = win.get(t) || []; a.push(f); win.set(t, a); };
  store.postMessage = (message) => { posted.push(message); };

  const ctx = vm.createContext(sandbox);
  vm.runInContext(SRC, ctx, { filename: 'interceptor.js' });
  // interceptor 의 첫 가드가 `event.source !== window` 다. VM 안에서 본 window 는 바깥
  // sandbox 객체와 참조가 다를 수 있어(contextify), 바깥 것을 source 로 주면 메시지가
  // 통째로 무시된다 — 그러면 모든 케이스가 '아무 일도 안 일어나서' 통과해버린다.
  // VM 이 보는 window 를 그대로 가져와 쓴다.
  const vmWindow = vm.runInContext('window', ctx);
  assert.ok(
    win.get('message')?.length,
    'interceptor 가 message 리스너를 달지 않았다 — 하네스가 스크립트를 통째로 건너뛴 것',
  );

  const send = (data) => win.get('message').forEach((f) => f({ source: vmWindow, data }));
  const config = (extra) => send({ __campfire_config: true, direction: 'isolated-to-main', ...extra });

  /** 파일 업로드를 흉내내고 "인터셉트 없이 통과했는지"를 돌려준다. */
  async function passedThrough(fileName = 'secret.pdf', type) {
    origFetchCalls.length = 0;
    // 인터셉트되면 이 promise 는 사용자 결정 때까지 끝나지 않는다 — await 하지 않는다.
    sandbox.fetch('https://chatgpt.com/upload', {
      method: 'POST', body: new store.File(fileName, type),
    }).catch(() => {});
    await settle();
    return origFetchCalls.length > 0;
  }

  return {
    config,
    passedThrough,
    posted,
    sandbox,
    setProxyMarked: (value) => { hasProxyMark = Boolean(value); },
    advance: (ms) => { skew += ms; },
  };
}

const withToken = () => {
  const t = load();
  t.config({ type: 'SECUREDOC_BRIDGE_TOKEN', token: TOKEN });
  return t;
};

async function main() {
  // (1) 기본값은 보호 ON — 파일이 그냥 나가지 않는다(다른 케이스의 기준선).
  assert.strictEqual(
    await withToken().passedThrough(), false,
    '기본 상태에서 파일이 인터셉트 없이 나갔다 — 이후 케이스의 판별이 무의미해진다',
  );

  // (2) 토큰 없는 보호 해제 요청은 무시된다 (fail-closed).
  {
    const t = withToken();
    t.config({ type: 'UPS_PROTECTION_STATE', enabled: false, fileInterceptEnabled: false });
    assert.strictEqual(
      await t.passedThrough(), false,
      '페이지가 토큰 없이 보낸 UPS_PROTECTION_STATE 로 보호가 꺼졌다',
    );
  }

  // (3) 토큰이 있으면 정상적으로 꺼진다 — 팝업 토글이 계속 동작해야 한다.
  {
    const t = withToken();
    t.config({
      type: 'UPS_PROTECTION_STATE', bridgeToken: TOKEN,
      enabled: false, fileInterceptEnabled: false,
    });
    assert.strictEqual(
      await t.passedThrough(), true,
      '정상 경로(토큰 동봉)의 보호 해제가 먹지 않았다 — 사용자 토글이 죽는다',
    );
  }

  // (4) 토큰은 한 번만 받는다 — 페이지가 나중에 자기 토큰으로 덮어쓸 수 없다.
  {
    const t = withToken();
    t.config({ type: 'SECUREDOC_BRIDGE_TOKEN', token: 'evil-token' }); // 덮어쓰기 시도
    t.config({
      type: 'UPS_PROTECTION_STATE', bridgeToken: 'evil-token',
      enabled: false, fileInterceptEnabled: false,
    });
    assert.strictEqual(
      await t.passedThrough(), false,
      '페이지가 토큰을 덮어쓴 뒤 보호를 껐다 — 토큰 검사 자체가 무의미해진다',
    );
  }

  // (4-2) 그림은 엔진에서 이미지 속 글자 검사를 켰을 때만 가로챈다(content.js 가 imageScan 으로 알린다).
  //       끄는 방향은 보호 해제처럼 토큰이 있어야 한다 — 페이지가 위조하면 그림이 검사 없이 나간다.
  {
    const t = withToken();
    assert.strictEqual(await t.passedThrough('shot.png', 'image/png'), true, '이미지 검사가 꺼져 있는데 그림 업로드를 가로챘다');
    t.config({ type: 'UPS_PROTECTION_STATE', bridgeToken: TOKEN, enabled: true, fileInterceptEnabled: true, imageScan: true });
    assert.strictEqual(await t.passedThrough('shot.png', 'image/png'), false, '이미지 검사가 켜져 있는데 그림 업로드가 검사 없이 나갔다');
    t.config({ type: 'UPS_PROTECTION_STATE', enabled: true, fileInterceptEnabled: true, imageScan: false });
    assert.strictEqual(await t.passedThrough('shot.png', 'image/png'), false, '페이지가 토큰 없이 보낸 요청으로 그림 검사가 꺼졌다');
    t.config({ type: 'UPS_PROTECTION_STATE', bridgeToken: TOKEN, enabled: true, fileInterceptEnabled: true, imageScan: false });
    assert.strictEqual(await t.passedThrough('shot.png', 'image/png'), true, '정상 경로(토큰 동봉)로 그림 검사를 껐는데 계속 가로챈다');
  }

  const APPROVED = { name: 'secret.pdf', size: 2048, type: 'application/pdf' };

  // (5) 토큰 없는 "검토 완료" 등록은 면제되지 않는다.
  {
    const t = withToken();
    t.config({ type: 'UPS_CONTENT_APPROVE_BATCH', batchId: 'b1', files: [APPROVED] });
    assert.strictEqual(
      await t.passedThrough('secret.pdf'), false,
      '페이지가 파일명만 등록해 마스킹 안 된 원본을 통과시켰다',
    );
  }

  // (6) 토큰이 있으면 면제된다 — content.js 의 정상 경로.
  {
    const t = withToken();
    t.config({ type: 'UPS_CONTENT_APPROVE_BATCH', bridgeToken: TOKEN, batchId: 'b1', files: [APPROVED] });
    assert.strictEqual(
      await t.passedThrough('secret.pdf'), true,
      '검토를 마친 파일이 다시 인터셉트됐다 — content.js 가 자기 첨부를 삼키게 된다',
    );
  }

  // (7) 주입 전에 취소·실패하면 승인을 즉시 회수한다.
  //     남겨 두면 다음 첨부가 우연히 같은 메타를 가질 때 검사 없이 통과한다.
  {
    const t = withToken();
    t.config({ type: 'UPS_CONTENT_APPROVE_BATCH', bridgeToken: TOKEN, batchId: 'b2', files: [APPROVED] });
    t.config({ type: 'UPS_CONTENT_ABORT_BATCH', bridgeToken: TOKEN, batchId: 'b2' });
    assert.strictEqual(
      await t.passedThrough('secret.pdf'), false,
      '취소된 배치의 승인이 살아남아 검사 없이 통과했다',
    );
  }

  // (8) 회수 메시지도 증거를 요구한다 — 토큰 없는 abort 로는 남의 승인을 못 지운다.
  //     지울 수 있으면 페이지가 정상 배치를 무력화해 전송을 방해할 수 있다.
  {
    const t = withToken();
    t.config({ type: 'UPS_CONTENT_APPROVE_BATCH', bridgeToken: TOKEN, batchId: 'b3', files: [APPROVED] });
    t.config({ type: 'UPS_CONTENT_ABORT_BATCH', batchId: 'b3' });   // 토큰 없음
    assert.strictEqual(
      await t.passedThrough('secret.pdf'), true,
      '토큰 없는 abort 가 정상 승인을 지웠다',
    );
  }

  // (9) 닫힌 배치에는 새 승인을 붙일 수 없다.
  //     붙일 수 있으면 회수 직전에 끼어든 등록 하나가 수명을 처음부터 다시 시작시킨다.
  {
    const t = withToken();
    t.config({ type: 'UPS_CONTENT_CLOSE_BATCH', bridgeToken: TOKEN, batchId: 'b4' });
    t.config({ type: 'UPS_CONTENT_APPROVE_BATCH', bridgeToken: TOKEN, batchId: 'b4', files: [APPROVED] });
    assert.strictEqual(
      await t.passedThrough('secret.pdf'), false,
      '닫힌 배치에 새 승인이 추가됐다',
    );
  }

  // (10) 닫은 직후에는 아직 통과해야 한다 — 사이트는 같은 파일을 등록 요청 →
  //      실제 PUT → 재시도로 여러 번 쓴다. 첫 일치에서 지우면 그 다음 요청이
  //      검사 패널을 다시 띄운다.
  {
    const t = withToken();
    t.config({ type: 'UPS_CONTENT_APPROVE_BATCH', bridgeToken: TOKEN, batchId: 'b5', files: [APPROVED] });
    t.config({ type: 'UPS_CONTENT_CLOSE_BATCH', bridgeToken: TOKEN, batchId: 'b5' });
    assert.strictEqual(
      await t.passedThrough('secret.pdf'), true,
      '닫자마자 승인이 사라져 다단계 업로드의 다음 요청이 막혔다',
    );
  }

  // (11) macOS 파일명(NFD)으로 승인했어도, 사이트가 NFC 로 바꿔 다루면 같은 파일로 본다.
  //      실측: claude 는 NFD 파일명을 NFC 로 그린다. 이름이 어긋나면 검토를 마친 마스킹본이
  //      "처음 보는 원본" 으로 다시 붙들려 첨부가 멈춘다.
  {
    const nfc = '채용공고_masked.md';
    const t = withToken();
    t.config({
      type: 'UPS_CONTENT_APPROVE_BATCH', bridgeToken: TOKEN, batchId: 'b6',
      files: [{ ...APPROVED, name: nfc.normalize('NFD') }],
    });
    assert.strictEqual(
      await t.passedThrough(nfc), true,
      'NFD 로 승인한 마스킹본이 NFC 이름으로 오자 다시 인터셉트됐다',
    );
  }

  // (12) 엔진이 프록시를 기대해도 실제 HEAD 응답에 표식이 있을 때만 확장이 양보한다.
  {
    // copilot.microsoft.com 은 아직 프록시가 파일 등록까지 맡는 대조군이다.
    const t = load({ proxyMarked: true, host: 'copilot.microsoft.com' });
    t.config({ type: 'SECUREDOC_BRIDGE_TOKEN', token: TOKEN });
    t.config({
      type: 'SECUREDOC_PROXY_EXPECTATION', bridgeToken: TOKEN, enabled: true,
    });
    await settle();
    assert.ok(t.posted.some((m) => m.type === 'SECUREDOC_PROXY_ROUTE' && m.mode === 'proxy'));
    assert.strictEqual(
      await t.passedThrough(), true,
      '실제 프록시 표식을 확인했는데 확장이 계속 이중 인터셉트한다',
    );
  }

  // (13) 설정은 켜졌지만 표식이 없으면 fallback을 보고하고 확장 보호를 유지한다.
  {
    const t = withToken();
    t.config({
      type: 'SECUREDOC_PROXY_EXPECTATION', bridgeToken: TOKEN, enabled: true,
    });
    await settle();
    assert.ok(t.posted.some((m) => m.type === 'SECUREDOC_PROXY_ROUTE' && m.mode === 'fallback'));
    assert.strictEqual(
      await t.passedThrough(), false,
      '프록시 표식 없이 확장이 손을 뗐다 — 보호 공백이 생긴다',
    );
  }

  // (14) 프록시를 끄면 TTL을 기다리지 않고 즉시 확장 보호로 돌아온다.
  {
    const t = load({ proxyMarked: true });
    t.config({ type: 'SECUREDOC_BRIDGE_TOKEN', token: TOKEN });
    t.config({
      type: 'SECUREDOC_PROXY_EXPECTATION', bridgeToken: TOKEN, enabled: true,
    });
    await settle();
    t.config({
      type: 'SECUREDOC_PROXY_EXPECTATION', bridgeToken: TOKEN, enabled: false,
    });
    assert.strictEqual(
      await t.passedThrough(), false,
      '프록시 해제 뒤에도 TTL 동안 확장 보호가 비었다',
    );
  }

  // (15) 다른 호스트 응답의 표식(예: Gemini 업로드 호스트)으로는 이 탭의 보호를 내려놓지 않는다.
  {
    const t = withToken();
    t.config({
      type: 'SECUREDOC_PROXY_EXPECTATION', bridgeToken: TOKEN, enabled: true,
    });
    await settle();
    t.setProxyMarked(true);
    await t.sandbox.fetch('https://push.clients6.google.com/upload/status');
    await settle();
    assert.strictEqual(
      await t.passedThrough(), false,
      '다른 호스트 응답의 표식만 보고 확장이 손을 뗐다',
    );
  }

  // (14-1) 표식이 오래되면(그 사이 앱이 꺼졌거나 프록시를 껐을 수 있다) 업로드를 넘기지 않고 확장이 맡는다 — 넘기면
  //        직결로 검사 없이 나간다. 다시 확인해 표식이 새로 잡히면(첨부 버튼 클릭이 확인을 띄운다) 다시 넘긴다.
  //        확장이 맡은 마스킹본은 프록시가 다시 검사하지 않아(#301) 두 번 검사되지 않는다.
  {
    const t = load({ proxyMarked: true, host: 'claude.ai' });
    t.config({ type: 'SECUREDOC_BRIDGE_TOKEN', token: TOKEN });
    t.config({ type: 'SECUREDOC_PROXY_EXPECTATION', bridgeToken: TOKEN, enabled: true });
    await settle();
    assert.strictEqual(await t.passedThrough(), true, '대조군: 방금 표식을 봤는데 업로드를 붙들었다');
    t.advance(20000);
    assert.strictEqual(await t.passedThrough(), false, '표식이 20초 전 것인데 업로드를 프록시에 넘겼다 — 직결이면 검사 없이 나간다');
    t.config({ type: 'SECUREDOC_PROXY_EXPECTATION', bridgeToken: TOKEN, enabled: true });
    await settle();
    assert.strictEqual(await t.passedThrough(), true, '다시 확인해 표식이 새로 잡혔는데 업로드를 계속 붙들었다');
  }

  // (15-1) 프록시가 아직 다 처리하지 못하는 사이트는 프록시 방식이어도 확장이 업로드를 전송 시점까지 맡는다
  //        (ChatGPT 그림·Perplexity 그림 미확인).
  for (const host of ['chatgpt.com', 'chat.openai.com', 'www.perplexity.ai']) {
    const t = load({ proxyMarked: true, host });
    t.config({ type: 'SECUREDOC_BRIDGE_TOKEN', token: TOKEN });
    t.config({
      type: 'SECUREDOC_PROXY_EXPECTATION', bridgeToken: TOKEN, enabled: true,
    });
    await settle();
    assert.ok(
      t.posted.some((m) => m.type === 'SECUREDOC_PROXY_ROUTE' && m.mode === 'proxy'),
      `${host}: 프록시 표식을 보지 못했다 — 이 사례가 아무것도 확인하지 않는다`,
    );
    assert.strictEqual(
      await t.passedThrough(), false,
      `${host}: 전송 전 파일 보류를 확장이 맡지 않았다`,
    );
  }

  // (15-2) 프록시만으로 문서·그림·텍스트 파일이 다 되는 사이트(Claude·Grok·Gemini)는 프록시 방식에서 확장이 손을 뗀다.
  for (const host of ['claude.ai', 'grok.com', 'gemini.google.com']) {
    const t = load({ proxyMarked: true, host });
    t.config({ type: 'SECUREDOC_BRIDGE_TOKEN', token: TOKEN });
    t.config({ type: 'SECUREDOC_PROXY_EXPECTATION', bridgeToken: TOKEN, enabled: true });
    await settle();
    assert.strictEqual(await t.passedThrough(), true, `${host}: 프록시 방식인데 확장이 업로드를 또 붙들었다`);
  }

  // (16) copilot.com 처럼 일반 입력을 무시하는 Lexical 에는 편집기 상태로 마스킹본을 넣는다.
  //      노드 모양(글 노드 속성·sentinel)은 지금 상태에서 빌리고, 토큰 없는 요청은 받지 않는다.
  {
    const t = withToken();
    let applied = null;
    const state = { root: { type: 'root', version: 1, children: [{ type: 'paragraph', version: 1, textFormat: 0, children: [
      { type: 'text', version: 1, detail: 0, format: 0, mode: 'normal', style: '', text: '주민번호 900101-1234567​‌' },
      { type: 'sentinel', version: 1, text: '​‌' },
    ] }] } };
    const lexical = {
      getEditorState: () => ({ toJSON: () => JSON.parse(JSON.stringify(state)) }),
      parseEditorState: (s) => ({ parsed: JSON.parse(s) }),
      setEditorState: (s) => { applied = s.parsed; },
    };
    t.sandbox.document.querySelector = (sel) => (sel === '#m365-chat-editor-target-element' ? { __lexicalEditor: lexical } : null);
    const ask = (extra) => t.config({ type: 'SECUREDOC_SET_EDITOR_STATE', selector: '#m365-chat-editor-target-element',
      text: '주민번호 [주민등록번호 마스킹]\n둘째 줄', nonce: 'n1', ...extra });

    ask({ bridgeToken: 'attacker' });
    assert.strictEqual(applied, null, '토큰 없는 요청으로 입력창을 바꿨다');
    assert.ok(!t.posted.some((m) => m.type === 'SECUREDOC_SET_EDITOR_STATE_RESULT'));

    ask({ bridgeToken: TOKEN });
    const kids = applied?.root?.children?.[0]?.children || [];
    assert.deepStrictEqual(kids.map((n) => [n.type, n.text]), [
      ['text', '주민번호 [주민등록번호 마스킹]'], ['linebreak', undefined], ['text', '둘째 줄'], ['sentinel', '​‌'],
    ], '편집기 상태에 마스킹본이 그대로 안 들어갔다');
    assert.strictEqual(kids[0].mode, 'normal', '글 노드 속성을 지금 상태에서 빌리지 않았다');
    const res = t.posted.find((m) => m.type === 'SECUREDOC_SET_EDITOR_STATE_RESULT');
    assert.ok(res && res.ok === true && res.nonce === 'n1' && res.bridgeToken === TOKEN, '결과를 content.js 에 알리지 않았다');

    // Copilot 은 여러 줄을 문단 여럿으로 친다(Shift+Enter, 문단마다 sentinel) — 한 문단 linebreak 로 넣으면 보낼 때 줄이 붙었다.
    const line = (text) => ({ type: 'paragraph', version: 1, textFormat: 0, children: [
      { type: 'text', version: 1, detail: 0, format: 0, mode: 'normal', style: '', text }, { type: 'sentinel', version: 1, text: '​‌' },
    ] });
    state.root.children = [line('주민번호 900101-1234567'), line('둘째 줄')];
    ask({ bridgeToken: TOKEN, text: '주민번호 [주민등록번호 마스킹]\n\n둘째 줄', nonce: 'n2' });
    assert.deepStrictEqual((applied?.root?.children || []).map((p) => p.children.map((n) => [n.type, n.text])), [
      [['text', '주민번호 [주민등록번호 마스킹]'], ['sentinel', '​‌']], [['text', '둘째 줄'], ['sentinel', '​‌']],
    ], '여러 문단으로 친 글을 사이트와 같은 모양(줄마다 문단)으로 넣지 않았다');
  }

  console.log('interceptor-bridge ok');
  process.exit(0); // interceptor 가 건 타이머 때문에 이벤트 루프가 안 비운다
}

main().catch((err) => { console.error(err); process.exit(1); });
