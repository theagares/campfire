/**
 * content-regression.test.js
 *
 * 새 사이드패널 구조(content.js)의 핵심 불변식을 검증한다.
 *  (1) 위조된 main-to-isolated 파일 메시지(bridgeToken 불일치)는 SW 로 새어나가지
 *      않는다 → START_SCAN 미발생.
 *  (2) content 가 소유한 파일 인풋 change 는 페이지 전파를 막지만(stopImmediatePropagation),
 *      "즉시" 스캔하지는 않는다 — 인젝션 탐지 재설계 이후 문서는 프롬프트 전송
 *      시점까지 보류(pending)된다(START_SCAN 미발생, engine 쪽 orchestrator.
 *      run_pipeline(user_prompt=...) 참고).
 *  (3) 보류된 문서가 있는 상태에서 사용자가 프롬프트를 제출(Enter)하면, 문서+
 *      프롬프트를 함께 넘기는 kind:'combined' START_SCAN 이 발생한다.
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
  // setFileOnInput 이 마스킹본을 넣고 input/change 를 쏘는 시점을 순서 로그에 남긴다.
  dispatchEvent() { actionLog.push({ kind: 'inject' }); return true; }
}
class HTMLTextAreaElementStub extends EventTargetStub {
  constructor(value = '') {
    super();
    this.tagName = 'TEXTAREA';
    this.value = value;
    // hideEditorDuringSubmit 이 전송 직전 입력창을 잠깐 감출 때 쓰는 최소 CSSOM stub.
    const props = new Map();
    this.style = {
      getPropertyValue: (k) => props.get(k)?.value ?? '',
      getPropertyPriority: (k) => props.get(k)?.priority ?? '',
      setProperty: (k, value, priority = '') => props.set(k, { value, priority }),
      removeProperty: (k) => props.delete(k),
    };
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
// 합성 이벤트의 type/dataTransfer 를 검사할 수 있게 init 을 그대로 보관한다.
class DragEventStub {
  constructor(type, init = {}) { this.type = type; Object.assign(this, init); }
}
// 드롭 지점 엘리먼트 — 우리가 어떤 합성 이벤트를 쐈는지 기록한다.
class DropTargetStub extends EventTargetStub {
  constructor() { super(); this.isConnected = true; this.dispatched = []; }
  dispatchEvent(event) { this.dispatched.push(event); return true; }
}
// 전송 버튼 — 비활성 상태를 흉내내고 클릭 횟수를 센다.
class SendButtonStub extends EventTargetStub {
  constructor() { super(); this.disabled = true; this.clicks = 0; }
  getAttribute() { return null; }
  click() { this.clicks++; }
}

// content.js 가 한 일의 "순서"를 검증하기 위한 로그 (테스트 4).
const actionLog = [];
let decisionListener = null;
let nextDecision = null; // 설정해두면 다음 START_SCAN 에 이 결정을 즉시 회신한다

const windowStub = {
  addEventListener: (t, l) => addListener(windowListeners, t, l),
  removeEventListener: (t, l) => {
    const arr = windowListeners.get(t);
    if (arr) windowListeners.set(t, arr.filter(x => x !== l));
  },
  postMessage(data) {
    if (data?.type === 'UPS_CONTENT_APPROVED_FILE') {
      actionLog.push({ kind: 'approve-msg', meta: data.meta });
    }
  },
};

// chatgpt.com 의 PROMPT_CONFIGS.editorSel('#prompt-textarea')로 조회되는 stub 에디터.
// 테스트 (3)에서 .value 를 채워 프롬프트 제출을 시뮬레이션한다.
const promptEditorStub = new HTMLTextAreaElementStub('');

// chatgpt.com 의 PROMPT_CONFIGS.sendBtnSel 로 조회되는 stub 전송 버튼(테스트 6).
const sendButtonStub = new SendButtonStub();

const domBySelector = new Map([
  ['#prompt-textarea', promptEditorStub],
  ['[data-testid="send-button"]', sendButtonStub],
]);

// 인라인 스타일 최소 구현 — 페이지 밀어내기(테스트 7) 검증용.
function makeStyleStub() {
  const props = new Map();
  return {
    _props: props,
    getPropertyValue: (k) => props.get(k)?.value ?? '',
    getPropertyPriority: (k) => props.get(k)?.priority ?? '',
    setProperty: (k, value, priority = '') => props.set(k, { value, priority }),
    removeProperty: (k) => props.delete(k),
  };
}

// 패널 iframe/호스트 엘리먼트 — 폭 실측과 close 메시지 경로를 흉내낸다.
const createdElements = [];
function makeElementStub() {
  const el = {
    style: makeStyleStub(),
    contentWindow: {},
    appendChild() {},
    remove() {},
    attachShadow: () => ({}),
    addEventListener() {},
    // 패널 호스트는 max-width:92vw 라 실제 폭을 실측해서 민다
    getBoundingClientRect: () => ({ width: 560, height: 900 }),
  };
  // cssText 를 문자열로 통째로 넣는 코드가 있어 받아만 둔다
  Object.defineProperty(el.style, 'cssText', { value: '', writable: true });
  createdElements.push(el);
  return el;
}

const documentStub = {
  documentElement: { appendChild() {}, style: makeStyleStub() },
  body: { dispatchEvent: () => true },
  activeElement: null,
  addEventListener: (t, l) => addListener(documentListeners, t, l),
  createElement: () => makeElementStub(),
  querySelector: (sel) => domBySelector.get(sel) ?? null,
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
    onMessage: {
      addListener: (l) => { decisionListener = l; },
      removeListener() {},
    },
    sendMessage(message, cb) {
      runtimeMessages.push(message);
      if (message.type === 'GET_TAB_ID') { cb?.({ tabId: 1 }); return; }
      // 테스트 4에서만 결정을 회신한다(그 전까지는 세션 대기 = decision 미도착).
      if (message.type === 'START_SCAN' && nextDecision) {
        const decision = nextDecision;
        nextDecision = null;
        queueMicrotask(() => decisionListener?.({
          type: 'PANEL_DECISION', sessionId: message.sessionId, decision,
        }));
      }
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
  DragEvent: DragEventStub,
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

  // (2) content 소유 파일 change → 전파는 차단하지만, 즉시 스캔하지는 않는다(보류).
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
  if (runtimeMessages.slice(beforeStageCount).some(m => m.type === 'START_SCAN')) {
    throw new Error('file attach triggered an immediate scan — should be staged until prompt submit');
  }

  // (3) 보류된 문서가 있는 상태에서 프롬프트를 제출(Enter)하면, 문서+프롬프트를
  // 함께 넘기는 kind:'combined' START_SCAN 이 발생해야 한다.
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

  // (4) 결합 검토가 승인되면, 마스킹본을 페이지에 주입하기 "전에" MAIN world 로
  // UPS_CONTENT_APPROVED_FILE 을 먼저 알려야 한다.
  //
  // 안 그러면 interceptor.js(MAIN world)의 Layer 2/3 업로드 훅이 그 마스킹본을
  // "처음 보는 원본"으로 오인해 검토 패널을 한 번 더 띄운다 — content.js 가 만든
  // File 은 isolated world 소속이라 MAIN world 의 _approvedFiles WeakSet 으로는
  // 인식할 수 없기 때문(실사용자 재현: 전송 직후 이미 마스킹된 내용으로 패널 재등장).
  // 주입(dispatchEvent)은 동기인데 postMessage 는 태스크 큐를 거치므로 순서가 뒤집힐
  // 수 있어, content.js 는 알림 후 한 매크로태스크 양보한 뒤 주입해야 한다.
  // 테스트 (3)은 결정을 회신하지 않고 끝났으므로 그 세션이 아직 대기 중이다
  // (promptInProcess=true). 취소로 정리하고, 새 문서를 다시 보류시켜 놓는다.
  decisionListener?.({
    type: 'PANEL_DECISION', sessionId: combinedScan.sessionId, decision: { action: 'cancel' },
  });
  await flush();

  const file2 = new FileStub(['pdf bytes'], 'report.pdf', { type: 'application/pdf' });
  const input2 = new HTMLInputElementStub(file2);
  dispatchDocumentEvent('change', {
    target: input2,
    composedPath: () => [input2, documentStub],
    preventDefault() {},
    stopImmediatePropagation() {},
  });
  await flush();

  actionLog.length = 0;
  promptEditorStub.value = '이 문서를 요약해줘';
  documentStub.activeElement = promptEditorStub;
  nextDecision = {
    action: 'send',
    maskedText: '이 문서를 요약해줘',
    file: {
      action: 'upload',
      maskedBase64: btoa('masked pdf bytes'),
      mimeType: 'application/pdf',
      fileName: 'report.pdf',
    },
  };
  dispatchDocumentEvent('keydown', {
    key: 'Enter', shiftKey: false, ctrlKey: false, altKey: false, metaKey: false,
    preventDefault() {},
    stopImmediatePropagation() {},
  });
  await flush();

  const approveIdx = actionLog.findIndex(e => e.kind === 'approve-msg');
  const injectIdx = actionLog.findIndex(e => e.kind === 'inject');
  if (approveIdx < 0) {
    throw new Error('마스킹본 주입 전 UPS_CONTENT_APPROVED_FILE 알림이 없었다');
  }
  if (injectIdx < 0) throw new Error('승인된 마스킹본이 페이지에 주입되지 않았다');
  if (approveIdx > injectIdx) {
    throw new Error('UPS_CONTENT_APPROVED_FILE 알림이 주입보다 늦게 나갔다 (순서 역전)');
  }
  if (actionLog[approveIdx].meta?.name !== 'report.pdf') {
    throw new Error(`알림 메타의 파일명이 다르다: ${actionLog[approveIdx].meta?.name}`);
  }

  // (5) 드롭한 문서를 가로챌 때, 사이트의 드래그 상태를 즉시 정리해줘야 한다.
  //
  // 우리는 원본이 새어나가지 않게 진짜 drop 을 stopImmediatePropagation() 으로 삼킨다.
  // 그런데 사이트의 드롭 오버레이("무엇이든 추가하세요")를 내리는 코드가 바로 그
  // drop/dragleave 리스너 안에 있어서, 그대로 두면 오버레이가 화면에 영영 남는다
  // (실사용자 재현). 파일이 하나도 없는 합성 dragleave/drop/dragend 를 흘려보내
  // 첨부는 일으키지 않으면서 드래그 상태만 정리한다.
  const dropTarget = new DropTargetStub();
  const dropFile = new FileStub(['pdf bytes'], 'dropped.pdf', { type: 'application/pdf' });
  dispatchDocumentEvent('drop', {
    target: dropTarget,
    dataTransfer: { files: [dropFile] },
    clientX: 10, clientY: 20,
    preventDefault() {},
    stopImmediatePropagation() {},
  });

  const clearedTypes = dropTarget.dispatched.map(e => e.type);
  for (const t of ['dragleave', 'drop', 'dragend']) {
    if (!clearedTypes.includes(t)) {
      throw new Error(`드롭 가로챈 뒤 사이트 드래그 상태 정리용 '${t}' 가 발생하지 않았다 (오버레이 고착)`);
    }
  }
  const syntheticDrop = dropTarget.dispatched.find(e => e.type === 'drop');
  if ((syntheticDrop.dataTransfer?.files ?? []).length !== 0) {
    throw new Error('드래그 상태 정리용 합성 drop 에 파일이 실려 있다 — 원본이 사이트로 새어나간다');
  }
  await flush();

  // (6) 첨부 업로드가 끝나 전송 버튼이 활성화될 때까지 기다렸다가 눌러야 한다.
  //
  // 사이트는 첨부 파일을 다 업로드할 때까지 전송 버튼을 비활성으로 둔다. 예전엔
  // 고정 시간만 기다리고 한 번만 눌러봐서, 그 시점에 아직 업로드 중이면 클릭이 먹지
  // 않고 그대로 끝났다(실사용자 재현: 검토는 되는데 전송이 안 됨).
  await new Promise(r => setTimeout(r, 3100)); // 테스트 4가 세운 promptApproved 해제 대기

  sendButtonStub.disabled = true;
  sendButtonStub.clicks = 0;
  promptEditorStub.value = '주민번호 없이 요약해줘';
  documentStub.activeElement = promptEditorStub;
  nextDecision = { action: 'masked', maskedText: '주민번호 없이 요약해줘' };
  dispatchDocumentEvent('keydown', {
    key: 'Enter', shiftKey: false, ctrlKey: false, altKey: false, metaKey: false,
    preventDefault() {},
    stopImmediatePropagation() {},
  });

  await new Promise(r => setTimeout(r, 600)); // 버튼이 비활성인 동안
  if (sendButtonStub.clicks !== 0) {
    throw new Error('전송 버튼이 비활성인데도 클릭했다 (업로드 완료 전 전송 시도)');
  }

  sendButtonStub.disabled = false;              // 업로드 완료 → 활성화
  await new Promise(r => setTimeout(r, 800));   // 폴링 주기(200ms) 안에 눌려야 한다
  if (sendButtonStub.clicks !== 1) {
    throw new Error(`전송 버튼 활성화 후에도 눌리지 않았다 (clicks=${sendButtonStub.clicks})`);
  }

  // (7) 검토 패널이 열려 있는 동안에는 사이트 본문을 그 폭만큼 밀어내야 하고,
  // 닫으면 원래대로 되돌려야 한다.
  //
  // 패널은 position:fixed 오버레이라 밀어내기가 없으면 사이트 오른쪽을 그냥 덮는다
  // (실사용자 리포트). 반대로 닫은 뒤 margin 이 남으면 사이트가 계속 찌그러진 채로
  // 남으므로, 원복까지가 한 쌍이다.
  const htmlStyle = documentStub.documentElement.style;

  // 앞선 테스트에서 이미 패널이 열렸다 → 밀어내기가 적용돼 있어야 한다.
  if (htmlStyle.getPropertyValue('margin-right') !== '560px') {
    throw new Error(`패널이 열렸는데 본문 밀어내기가 없다 (margin-right="${htmlStyle.getPropertyValue('margin-right')}")`);
  }
  if (htmlStyle.getPropertyPriority('margin-right') !== 'important') {
    throw new Error('밀어내기 margin-right 에 !important 가 없다 — 사이트 CSS 에 밀릴 수 있다');
  }

  // 패널 닫기: 실제 iframe 의 contentWindow 에서 온 메시지만 content.js 가 받아들인다
  // (나머지 후보는 source 불일치로 무시되므로 전부 쏴도 안전하다).
  for (const el of createdElements) {
    for (const l of windowListeners.get('message') || []) {
      l({ source: el.contentWindow, data: { type: 'UPS_CLOSE_OVERLAY' } });
    }
  }
  await flush();

  if (htmlStyle.getPropertyValue('margin-right') !== '') {
    throw new Error('패널을 닫았는데 본문 밀어내기가 남아있다 — 사이트가 계속 찌그러진다');
  }
  if (htmlStyle.getPropertyValue('overflow-x') !== '') {
    throw new Error('패널을 닫았는데 overflow-x 가 남아있다');
  }

  console.log('content regression ok');
  process.exit(0);
})();
