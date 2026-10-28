/**
 * chatgpt-second-composer.test.js
 *
 * 같은 입력창 선택자에 요소가 여럿 맞을 때 실제로 쓰는 입력창을 골라야 한다.
 *
 * 왜 생겼나(v0.2.56 맥 전체 시험 EX-7): ChatGPT 는 첫 메시지를 보낸 뒤 같은 대화 화면에
 * `form[data-chatgpt-composer] .ProseMirror` 가 둘이 된다(첫 것은 포커스 없음). 확장은 문서 순서상
 * 첫 것을 입력창으로 보고, 실제 입력창에서 누른 Enter 를 "입력창 밖" 으로 판단해 검사 없이 넘겼다 —
 * 두 번째 메시지부터 원문이 그대로 나갔다.
 *
 * (아래 하네스는 protection-off.test.js 와 같다.)
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
// 문서 순서상 먼저 나오는 다른 컴포저 — 포커스도 글도 없고 화면에도 안 보인다.
const staleEditor = new EditorStub('');
staleEditor.getBoundingClientRect = () => ({ width: 0, height: 0 });
editor.getBoundingClientRect = () => ({ width: 600, height: 40 });
staleEditor.contains = (el) => el === staleEditor;
editor.contains = (el) => el === editor;

const documentStub = {
  addEventListener(type, l) {
    if (!documentListeners.has(type)) documentListeners.set(type, []);
    documentListeners.get(type).push(l);
  },
  removeEventListener() {},
  // 맥 실측: #prompt-textarea·#mobile-composer-prompt 는 0개, .ProseMirror 선택자는 2개(첫 것이 포커스 없는 쪽).
  querySelector: (sel) => (sel.includes('ProseMirror') ? staleEditor : null),
  querySelectorAll: (sel) => (sel.includes('ProseMirror') ? [staleEditor, editor] : []),
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
  loadContentWithProtection({ protectionEnabled: true, fileInterceptEnabled: true });
  await flush();

  const enter = makeEnterEvent();
  fire('keydown', enter.ev);
  await flush();
  assert.equal(enter.prevented, true,
    '입력창이 둘일 때 실제 입력창의 Enter 를 가로채지 않았다 — 두 번째 메시지부터 원문이 나간다');
  const scan = runtimeMessages.find((m) => m.type === 'START_SCAN');
  assert.ok(scan, '검사를 시작하지 않았다');
  assert.ok(JSON.stringify(scan).includes('010-1234-5678'), '실제 입력창이 아닌 다른 요소의 글을 검사했다');

  // 전송 버튼(마우스) — 누르면 포커스가 버튼으로 간다. 버튼이 든 form 의 입력창을 골라야 한다.
  // 위 Enter 검사가 패널 결정을 기다리는 중이라 새로 불러온다(아니면 "검사 중" 으로 막히기만 한다).
  loadContentWithProtection({ protectionEnabled: true, fileInterceptEnabled: true });
  await flush();
  const liveForm = { contains: (el) => el === editor || el === sendBtn };
  const staleForm = { contains: (el) => el === staleEditor };
  editor.closest = (sel) => (sel === 'form' ? liveForm : null);
  staleEditor.closest = (sel) => (sel === 'form' ? staleForm : null);
  const sendBtn = {
    tagName: 'BUTTON', disabled: false, getAttribute: () => null,
    closest: (sel) => (/button/.test(sel) ? sendBtn : null),
  };
  documentStub.activeElement = sendBtn;
  let clickPrevented = false;
  fire('click', {
    type: 'click', target: sendBtn,
    preventDefault() { clickPrevented = true; }, stopImmediatePropagation() {},
  });
  await flush();
  assert.equal(clickPrevented, true, '입력창이 둘일 때 전송 버튼을 가로채지 않았다 — 원문이 나간다');
  const clickScan = runtimeMessages.find((m) => m.type === 'START_SCAN');
  assert.ok(clickScan && JSON.stringify(clickScan).includes('010-1234-5678'), '전송 버튼 경로가 실제 입력창 글을 검사하지 않았다');

  console.log('chatgpt-second-composer ok');
  process.exit(0);
})().catch((e) => { console.error(e.message); process.exit(1); });
