/**
 * content-regression.test.js
 *
 * 새 사이드패널 구조(content.js)의 핵심 불변식을 검증한다.
 *  (1) 위조된 main-to-isolated 파일 메시지(bridgeToken 불일치)는 SW 로 새어나가지
 *      않는다 → START_SCAN 미발생.
 *  (2) content 가 소유한 파일 인풋 change 는 페이지 전파를 막고(stopImmediatePropagation),
 *      "즉시" kind:'file' START_SCAN 을 시작한다 — 문서는 첨부되는 순간 검토·
 *      재주입까지 끝낸다(2026-08-01 재정정: 프롬프트 전송 시점까지 보류하다가
 *      재주입하던 예전 방식은 그 보류 구간 동안 SPA가 DOM을 다시 그려 재주입
 *      시 크래시로 이어져 폐기됨).
 *  (3) 그렇게 검토·재주입된 문서가 있는 상태에서 사용자가 프롬프트를 제출(Enter)
 *      하면, "이 문서가 실제 프롬프트의 의도를 무시/변조하려는가"를 판단하는
 *      kind:'combined' START_SCAN 이 (문서를 다시 DOM에 손대지 않고) 텍스트+캐시된
 *      원본 바이트로 발생한다.
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
class HTMLTextAreaElementStub extends EventTargetStub {
  constructor(value = '') {
    super();
    this.tagName = 'TEXTAREA';
    this.value = value;
  }
  focus() {}
}
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

// chatgpt.com 의 PROMPT_CONFIGS.editorSel('#prompt-textarea')로 조회되는 stub 에디터.
// 테스트 (3)에서 .value 를 채워 프롬프트 제출을 시뮬레이션한다.
const promptEditorStub = new HTMLTextAreaElementStub('');

const documentStub = {
  documentElement: { appendChild() {} },
  activeElement: null,
  addEventListener: (t, l) => addListener(documentListeners, t, l),
  createElement: () => ({ style: {}, appendChild() {}, remove() {}, attachShadow: () => ({}), addEventListener() {} }),
  querySelector: (sel) => (sel === '#prompt-textarea' ? promptEditorStub : null),
  querySelectorAll: () => [],
  execCommand: () => true,
};

let scanDecisionListener = null;
let nextFileDecision = null; // 다음 kind:'file' START_SCAN에 즉시 회신할 결정(테스트 제어용)

const chromeStub = {
  tabs: { sendMessage: () => Promise.resolve(), create() {} },
  storage: {
    local: { get(defaults, cb) { cb({ ...defaults, protectionEnabled: true }); }, set(_v, cb) { cb?.(); } },
    onChanged: { addListener() {} },
  },
  runtime: {
    lastError: null,
    getURL: (p) => `chrome-extension://test-id/${p}`,
    onMessage: {
      addListener: (l) => { scanDecisionListener = l; },
      removeListener() {},
    },
    sendMessage(message, cb) {
      runtimeMessages.push(message);
      if (message.type === 'GET_TAB_ID') { cb?.({ tabId: 1 }); return; }
      // 문서 첨부는 이제 즉시 검토되므로, 다음 change/drop/paste가 만드는
      // kind:'file' 세션엔 곧바로 결정을 회신해 재주입까지 이어지게 한다.
      if (message.type === 'START_SCAN' && message.kind === 'file' && nextFileDecision) {
        const decision = nextFileDecision;
        nextFileDecision = null;
        queueMicrotask(() => scanDecisionListener?.({ type: 'PANEL_DECISION', sessionId: message.sessionId, decision }));
      }
      // kind:'combined'/'prompt' 는 응답 없이 세션 대기 (테스트에선 decision 미도착)
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

  // (2) content 소유 파일 change → 전파는 차단하고(stopImmediatePropagation),
  // 즉시 kind:'file' START_SCAN 을 시작한다(보류 없음).
  nextFileDecision = { action: 'passthrough' };
  const file = new FileStub(['pdf bytes'], 'report.pdf', { type: 'application/pdf' });
  const input = new HTMLInputElementStub(file);
  let stopped = false;
  const beforeStageCount = runtimeMessages.length;
  dispatchDocumentEvent('change', {
    target: input,
    composedPath: () => [input, documentStub],
    preventDefault() {},
    stopImmediatePropagation() { stopped = true; },
  });
  await flush();

  if (!stopped) throw new Error('content-owned file change did not stop page propagation');
  const fileScan = runtimeMessages.slice(beforeStageCount).find(m => m.type === 'START_SCAN');
  if (!fileScan) throw new Error('file attach did not start an immediate scan');
  if (fileScan.kind !== 'file') throw new Error(`expected kind:'file', got kind:'${fileScan.kind}'`);

  // (3) 위에서 즉시 검토·재주입된 문서가 있는 상태에서 프롬프트를 제출(Enter)하면,
  // 문서를 다시 건드리지 않으면서도 문서+프롬프트를 함께 판단하는 kind:'combined'
  // START_SCAN 이 발생해야 한다.
  promptEditorStub.value = '이 문서를 요약해줘';
  documentStub.activeElement = promptEditorStub;
  const beforeSubmitCount = runtimeMessages.length;
  dispatchDocumentEvent('keydown', {
    key: 'Enter', shiftKey: false, ctrlKey: false, altKey: false, metaKey: false,
    preventDefault() {},
    stopImmediatePropagation() {},
  });
  await flush();

  const combinedScan = runtimeMessages.slice(beforeSubmitCount).find(m => m.type === 'START_SCAN');
  if (!combinedScan) throw new Error('prompt submit with a pending attachment did not start a scan');
  if (combinedScan.kind !== 'combined') {
    throw new Error(`expected kind:'combined', got kind:'${combinedScan.kind}'`);
  }
  if (!combinedScan.payload?.base64Data || combinedScan.payload.text !== '이 문서를 요약해줘') {
    throw new Error('combined scan payload missing staged file data or prompt text');
  }

  console.log('content regression ok');
  process.exit(0);
})();
