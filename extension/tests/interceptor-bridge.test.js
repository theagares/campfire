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
 *   - UPS_CONTENT_APPROVED_FILE → 이 목록에 든 파일은 업로드 훅이 검사 없이 통과시킨다.
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
function load() {
  const win = new Map();
  const doc = new Map();
  const origFetchCalls = [];
  const store = Object.create(null);

  Object.assign(store, {
    console, setTimeout, clearTimeout, setInterval, clearInterval, queueMicrotask,
    Promise, Date, Math, JSON, Set, Map, WeakSet, WeakMap, Array, Object, String,
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
    constructor(name) { super(); this.name = name || 'secret.pdf'; }
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
  store.location = { hostname: 'chatgpt.com', href: 'https://chatgpt.com/' };
  // 훅이 원본으로 붙들어 두는 fetch. 여기 도착했다 = 인터셉트 없이 통과했다.
  store.fetch = function (...args) {
    origFetchCalls.push(args);
    return Promise.resolve({ ok: true });
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
  store.postMessage = noop;

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
  async function passedThrough(fileName = 'secret.pdf') {
    origFetchCalls.length = 0;
    // 인터셉트되면 이 promise 는 사용자 결정 때까지 끝나지 않는다 — await 하지 않는다.
    sandbox.fetch('https://chatgpt.com/upload', {
      method: 'POST', body: new store.File(fileName),
    }).catch(() => {});
    await settle();
    return origFetchCalls.length > 0;
  }

  return { config, passedThrough };
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

  // (5) 토큰 없는 "검토 완료" 등록은 면제되지 않는다.
  {
    const t = withToken();
    t.config({
      type: 'UPS_CONTENT_APPROVED_FILE',
      meta: { name: 'secret.pdf', size: 2048, type: 'application/pdf' },
    });
    assert.strictEqual(
      await t.passedThrough('secret.pdf'), false,
      '페이지가 파일명만 등록해 마스킹 안 된 원본을 통과시켰다',
    );
  }

  // (6) 토큰이 있으면 면제된다 — content.js 의 정상 경로.
  {
    const t = withToken();
    t.config({
      type: 'UPS_CONTENT_APPROVED_FILE', bridgeToken: TOKEN,
      meta: { name: 'secret.pdf', size: 2048, type: 'application/pdf' },
    });
    assert.strictEqual(
      await t.passedThrough('secret.pdf'), true,
      '검토를 마친 파일이 다시 인터셉트됐다 — content.js 가 자기 첨부를 삼키게 된다',
    );
  }

  console.log('interceptor-bridge ok');
  process.exit(0); // interceptor 가 건 타이머 때문에 이벤트 루프가 안 비운다
}

main().catch((err) => { console.error(err); process.exit(1); });
