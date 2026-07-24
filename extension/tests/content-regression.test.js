/**
 * content-regression.test.js
 *
 * 새 사이드패널 구조(content.js)의 두 가지 핵심 불변식을 검증한다.
 *  (1) 위조된 main-to-isolated 파일 메시지(bridgeToken 불일치)는 SW 로 새어나가지
 *      않는다 → START_SCAN 미발생.
 *  (2) content 가 소유한 파일 인풋 change 는 페이지 전파를 막고(stopImmediatePropagation)
 *      START_SCAN 을 SW 로 보낸다.
 *
 * 실행: node tests/content-regression.test.js  (exit 0 = 통과)
 */

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const windowListeners = new Map();
const documentListeners = new Map();
const runtimeMessages = [];

function addListener(map, type, listener) {
  if (!map.has(type)) map.set(type, []);
  map.get(type).push(listener);
}

class EventTargetStub {
  addEventListener() {}
  dispatchEvent() { return true; }
  closest() { return null; }
  contains() { return false; }
}
class HTMLInputElementStub extends EventTargetStub {
  constructor(file = null) {
    super();
    this.type = 'file';
    this.files = file ? [file] : [];
    this.value = '';
  }
}
class HTMLTextAreaElementStub extends EventTargetStub {}
class FileStub {
  constructor(parts, name, options = {}) {
    this._parts = parts;
    this.name = name;
    this.type = options.type || '';
    this.size = parts.reduce((s, p) => s + Buffer.byteLength(String(p)), 0);
  }
  async arrayBuffer() {
    return Buffer.concat(this._parts.map(p => Buffer.from(String(p)))).buffer;
  }
}
class DataTransferStub {
  constructor() { this.files = []; this.items = { add: f => this.files.push(f) }; }
}

const windowStub = {
  addEventListener: (t, l) => addListener(windowListeners, t, l),
  postMessage() {},
};

const documentStub = {
  documentElement: { appendChild() {} },
  activeElement: null,
  addEventListener: (t, l) => addListener(documentListeners, t, l),
  createElement: () => ({ style: {}, appendChild() {}, remove() {}, attachShadow: () => ({}), addEventListener() {} }),
  querySelector: () => null,
  querySelectorAll: () => [],
  execCommand: () => true,
};

const chromeStub = {
  tabs: { sendMessage: () => Promise.resolve(), create() {} },
  storage: {
    local: { get(defaults, cb) { cb({ ...defaults, protectionEnabled: true }); }, set(_v, cb) { cb?.(); } },
    onChanged: { addListener() {} },
  },
  runtime: {
    lastError: null,
    getURL: (p) => `chrome-extension://test-id/${p}`,
    onMessage: { addListener() {}, removeListener() {} },
    sendMessage(message, cb) {
      runtimeMessages.push(message);
      if (message.type === 'GET_TAB_ID') { cb?.({ tabId: 1 }); return; }
      // START_SCAN 등은 응답 없이 세션 대기 (테스트에선 decision 미도착)
    },
  },
};

const sandbox = {
  window: windowStub,
  document: documentStub,
  chrome: chromeStub,
  location: { hostname: 'chatgpt.com' },
  console,
  setTimeout,
  clearTimeout,
  crypto: { randomUUID: () => 'uuid-' + Math.random().toString(36).slice(2) },
  TextEncoder,
  TextDecoder,
  File: FileStub,
  DataTransfer: DataTransferStub,
  HTMLInputElement: HTMLInputElementStub,
  HTMLTextAreaElement: HTMLTextAreaElementStub,
  Event: class {},
  InputEvent: class {},
  KeyboardEvent: class {},
  DragEvent: class {},
  ClipboardEvent: class {},
  atob: v => Buffer.from(v, 'base64').toString('binary'),
  btoa: v => Buffer.from(v, 'binary').toString('base64'),
};
sandbox.globalThis = sandbox;

vm.createContext(sandbox);
vm.runInContext(
  fs.readFileSync(path.join(__dirname, '..', 'content', 'content.js'), 'utf8'),
  sandbox,
  { filename: 'content.js' },
);

async function dispatchWindowMessage(data) {
  for (const l of windowListeners.get('message') || []) await l({ source: windowStub, data });
}
function dispatchDocumentEvent(type, event) {
  // 의도적으로 await 하지 않는다: content.js 의 change 핸들러는 패널 결정을
  // 무한 대기하므로, 여기서는 START_SCAN 이 동기+마이크로태스크로 발생하는지만 본다.
  for (const l of documentListeners.get(type) || []) l(event);
}
const flush = () => new Promise(r => setTimeout(r, 60));

(async () => {
  // (1) 위조 메시지: bridgeToken 불일치 → START_SCAN 없어야 함
  await dispatchWindowMessage({
    __securedoc: true,
    direction: 'main-to-isolated',
    type: 'SECUREDOC_FILE_SELECTED',
    bridgeToken: 'attacker-controlled',
    payload: { inputId: 'x', base64Data: btoa('malicious'), mimeType: 'application/pdf', fileName: 'attack.pdf', fileSize: 9 },
  });
  await flush();
  if (runtimeMessages.some(m => m.type === 'START_SCAN')) {
    throw new Error('forged main-to-isolated file message triggered a scan');
  }

  // (2) content 소유 파일 change → 전파 차단 + START_SCAN 발생
  const file = new FileStub(['pdf bytes'], 'report.pdf', { type: 'application/pdf' });
  const input = new HTMLInputElementStub(file);
  let stopped = false;
  dispatchDocumentEvent('change', {
    target: input,
    composedPath: () => [input, documentStub],
    preventDefault() {},
    stopImmediatePropagation() { stopped = true; },
  });
  await flush();

  if (!stopped) throw new Error('content-owned file change did not stop page propagation');
  if (!runtimeMessages.some(m => m.type === 'START_SCAN')) {
    throw new Error('content-owned file change did not start a scan');
  }

  console.log('content regression ok');
  process.exit(0);
})();
