/**
 * protection-off.test.js
 *
 * 확장 팝업의 보호 토글을 끄면 "검사만 건너뛰는" 게 아니라 **인터셉트 자체가 없어야
 * 한다**는 계약을 지킨다.
 *
 * 왜 생겼나: 데스크탑 앱의 ON/OFF 토글이 제거되면서 protectionEnabled 를 쓰는 곳이
 * 사라져 그 값이 항상 true 로 남았고, 팝업 토글은 fileInterceptEnabled 만 껐다.
 * 그래서 토글을 꺼도 프롬프트 전송은 계속 가로채였다(실사용자 리포트). 추가로
 * dragover 핸들러는 플래그를 아예 안 보고 preventDefault 를 걸어서, 꺼도 페이지가
 * 계속 우리 드롭 타깃으로 동작했다.
 *
 * 실행: node tests/protection-off.test.js   (exit 0 = 통과)
 */

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const documentListeners = new Map();
const runtimeMessages = [];

class ElStub {
  addEventListener() {}
  dispatchEvent() { return true; }
  closest() { return null; }
  contains() { return true; }
  focus() {}
  querySelector() { return null; }
}

class EditorStub extends ElStub {
  constructor(value) {
    super();
    this.tagName = 'TEXTAREA';
    this.value = value;
    const props = new Map();
    this.style = {
      getPropertyValue: (k) => props.get(k)?.value ?? '',
      getPropertyPriority: (k) => props.get(k)?.priority ?? '',
      setProperty: (k, v, p = '') => props.set(k, { value: v, priority: p }),
      removeProperty: (k) => props.delete(k),
    };
  }
}

class FileStub {
  constructor(name, type) { this.name = name; this.type = type; this.size = 10; }
  async arrayBuffer() { return new ArrayBuffer(10); }
}

const editor = new EditorStub('김도윤 010-1234-5678 확인 부탁드립니다');

const documentStub = {
  addEventListener(type, l) {
    if (!documentListeners.has(type)) documentListeners.set(type, []);
    documentListeners.get(type).push(l);
  },
  removeEventListener() {},
  querySelector: () => editor,
  querySelectorAll: () => [],
  createElement: () => new ElStub(),
  body: new ElStub(),
  documentElement: new ElStub(),
  activeElement: editor,
  execCommand: () => true,
};

const windowStub = {
  addEventListener() {},
  removeEventListener() {},
  postMessage() {},
  location: { hostname: 'chatgpt.com', href: 'https://chatgpt.com/' },
  getSelection: () => null,
};

/** 보호가 꺼진 상태로 content.js 를 로드한다. */
function loadContentWithProtection({ protectionEnabled, fileInterceptEnabled }) {
  documentListeners.clear();
  runtimeMessages.length = 0;

  const chromeStub = {
    tabs: { sendMessage: () => Promise.resolve(), create() {} },
    storage: {
      local: {
        get(defaults, cb) { cb({ ...defaults, protectionEnabled, fileInterceptEnabled }); },
        set(_v, cb) { cb?.(); },
      },
      onChanged: { addListener() {} },
    },
    runtime: {
      lastError: null,
      getURL: (p) => `chrome-extension://test-id/${p}`,
      onMessage: { addListener() {}, removeListener() {} },
      sendMessage(message, cb) {
        runtimeMessages.push(message);
        if (message.type === 'GET_TAB_ID') { cb?.({ tabId: 1 }); return; }
        cb?.({ ok: true });
      },
    },
  };

  const sandbox = {
    window: windowStub,
    document: documentStub,
    chrome: chromeStub,
    location: windowStub.location,
    console: { log() {}, debug() {}, warn() {}, error() {} },
    setTimeout,
    clearTimeout,
    crypto: { randomUUID: () => 'uuid-test' },
    TextEncoder,
    TextDecoder,
    File: FileStub,
    DataTransfer: class { constructor() { this.files = []; this.items = { add: (f) => this.files.push(f) }; } },
    HTMLInputElement: ElStub,
    HTMLTextAreaElement: EditorStub,
    Event: class { constructor(type) { this.type = type; } },
    requestAnimationFrame: (cb) => setTimeout(cb, 0),
    InputEvent: class {},
    KeyboardEvent: class {},
    DragEvent: class {},
    ClipboardEvent: class {},
    atob: (v) => Buffer.from(v, 'base64').toString('binary'),
    btoa: (v) => Buffer.from(v, 'binary').toString('base64'),
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(
    fs.readFileSync(path.join(__dirname, '..', 'content', 'content.js'), 'utf8'),
    sandbox,
    { filename: 'content.js' },
  );
  return sandbox;
}

function fire(type, event) {
  for (const l of documentListeners.get(type) || []) l(event);
}
const flush = () => new Promise((r) => setTimeout(r, 60));

function makeDragEvent(file) {
  let prevented = false;
  return {
    ev: {
      type: 'dragover',
      dataTransfer: { items: [{ kind: 'file' }], files: [file] },
      preventDefault() { prevented = true; },
      stopImmediatePropagation() {},
      target: editor,
    },
    get prevented() { return prevented; },
  };
}

function makeEnterEvent() {
  let prevented = false;
  return {
    ev: {
      type: 'keydown', key: 'Enter',
      shiftKey: false, ctrlKey: false, altKey: false, metaKey: false,
      preventDefault() { prevented = true; },
      stopImmediatePropagation() {},
      target: editor,
    },
    get prevented() { return prevented; },
  };
}

(async () => {
  // ── OFF: 아무것도 가로채면 안 된다 ──────────────────────────────────────────
  loadContentWithProtection({ protectionEnabled: false, fileInterceptEnabled: false });
  await flush();

  const dragOff = makeDragEvent(new FileStub('a.docx',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document'));
  fire('dragover', dragOff.ev);
  assert.equal(dragOff.prevented, false,
    'OFF 인데 dragover 를 preventDefault 했다 — 페이지가 계속 우리 드롭 타깃이 된다');

  const enterOff = makeEnterEvent();
  fire('keydown', enterOff.ev);
  await flush();
  assert.equal(enterOff.prevented, false,
    'OFF 인데 프롬프트 전송을 preventDefault 했다 — 인터셉트가 안 꺼졌다');
  assert.equal(
    runtimeMessages.filter((m) => m.type === 'START_SCAN').length, 0,
    'OFF 인데 검사를 시작했다',
  );

  // ── ON: 평소대로 가로채야 한다(위 단언이 "항상 통과"가 아님을 보장) ─────────
  loadContentWithProtection({ protectionEnabled: true, fileInterceptEnabled: true });
  await flush();

  const dragOn = makeDragEvent(new FileStub('a.docx',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document'));
  fire('dragover', dragOn.ev);
  assert.equal(dragOn.prevented, true, 'ON 인데 dragover 를 가로채지 않았다');

  const enterOn = makeEnterEvent();
  fire('keydown', enterOn.ev);
  await flush();
  assert.equal(enterOn.prevented, true, 'ON 인데 프롬프트 전송을 가로채지 않았다');

  console.log('protection-off ok');
  // content.js 가 걸어둔 타이머 때문에 이벤트 루프가 안 비므로 명시적으로 끝낸다.
  process.exit(0);
})().catch((e) => { console.error(e.message); process.exit(1); });
