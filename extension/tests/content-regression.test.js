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
 *  (7) 검토 패널은 SW 에 OPEN_PANEL 을 보내 브라우저 네이티브 사이드패널로 열고,
 *      페이지 DOM 은 건드리지 않는다(iframe 주입도, 본문 밀어내기도 없음).
 *  (8) 그 OPEN_PANEL 이 거부되면(제스처 전파 실패 등) iframe 오버레이로 폴백한다.
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
const dispatchedWindowEvents = [];
let decisionListener = null;
let nextDecision = null; // 설정해두면 다음 START_SCAN 에 이 결정을 즉시 회신한다
// SW 가 sidePanel.open() 에 실패했다고 답하는 상황(제스처 전파 실패 등)을 만든다.
let failNextOpenPanel = false;

const windowStub = {
  addEventListener: (t, l) => addListener(windowListeners, t, l),
  removeEventListener: (t, l) => {
    const arr = windowListeners.get(t);
    if (arr) windowListeners.set(t, arr.filter(x => x !== l));
  },
  // window 로 나가는 합성 이벤트를 기록한다. 특히 resize — 예전엔 사이트 레이아웃
  // 재계산을 유도하려고 가짜 resize 를 쐈는데, 그게 우리 resize 리스너를 다시 깨워
  // 무한 재귀로 탭이 멎었다(0.1.8 실배포 결함). 네이티브 사이드패널은 브라우저가
  // 뷰포트를 진짜로 줄여주므로 그런 유도가 아예 필요 없다 — 아래 (7)에서 우리가
  // resize 를 단 한 번도 쏘지 않는지 확인해 그 시절 코드가 되살아나는 걸 막는다.
  // 등록된 리스너를 실제로 호출하되, 폭주가 테스트 자체를 멎게 하지 않도록 상한에서
  // 전파만 멈춘다.
  dispatchEvent(ev) {
    dispatchedWindowEvents.push(ev?.type);
    if (dispatchedWindowEvents.length > 50) return true;
    for (const l of windowListeners.get(ev?.type) || []) l(ev);
    return true;
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

// 인라인 스타일 최소 구현 — 페이지(html/body)를 건드리지 않는지 보는 데 쓴다(테스트 7).
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

// 폴백 오버레이의 호스트/iframe 엘리먼트 — 주입·제거 경로를 흉내낸다(테스트 8).
const createdElements = [];
function makeElementStub(tagName = '') {
  const el = {
    tagName: String(tagName).toUpperCase(),
    children: [],
    removed: false,
    style: makeStyleStub(),
    contentWindow: {},
    appendChild(child) { this.children.push(child); },
    remove() { this.removed = true; },
    attachShadow: () => ({}),
    addEventListener() {},
    getBoundingClientRect: () => ({ width: 560, height: 900 }),
  };
  // cssText 를 문자열로 통째로 넣는 코드가 있어 받아만 둔다
  Object.defineProperty(el.style, 'cssText', { value: '', writable: true });
  createdElements.push(el);
  return el;
}

// <html> 에 실제로 붙은 노드. 첨부 보류 뱃지도 여기로 오므로, 패널 오버레이는
// id 로 골라낸다(네이티브 경로에선 오버레이가 하나도 없어야 한다).
const appendedToRoot = [];
const injectedOverlays = () => appendedToRoot.filter(el => el?.id === '__ups_overlay_host');

const documentStub = {
  documentElement: {
    appendChild(node) { appendedToRoot.push(node); },
    style: makeStyleStub(),
  },
  body: { dispatchEvent: () => true, style: makeStyleStub() },
  activeElement: null,
  addEventListener: (t, l) => addListener(documentListeners, t, l),
  createElement: (tag) => makeElementStub(tag),
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
      // SW 의 OPEN_PANEL 핸들러 흉내 — ok:false 면 content 가 iframe 으로 폴백해야 한다.
      if (message.type === 'OPEN_PANEL') {
        if (failNextOpenPanel) { failNextOpenPanel = false; cb?.({ ok: false, reason: 'rejected' }); }
        else cb?.({ ok: true });
        return;
      }
      if (message.type === 'CLOSE_PANEL') { cb?.({ ok: true }); return; }
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
  Event: class { constructor(type) { this.type = type; } },
  requestAnimationFrame: (cb) => setTimeout(cb, 0),
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

  // (7) 검토 패널은 SW 에 OPEN_PANEL 을 보내 브라우저 네이티브 사이드패널로 열어야
  // 하고, 페이지 DOM 은 전혀 건드리지 않아야 한다.
  //
  // 예전엔 패널을 position:fixed iframe 으로 페이지에 주입하고, 그게 사이트를 덮는 걸
  // CSS(html/body 의 margin·max-width·transform + 가짜 resize)로 비켜가려 했다. 세 번
  // 시도해 전부 실패했다 — ChatGPT 앱 셸이 100vw 기준이라 body 를 좁혀도 폭이 안 줄고
  // 잘리기만 했고(실사용자 스크린샷: 본문 글자가 중간에서 잘림), 재계산을 유도하려던
  // 가짜 resize 는 무한 재귀로 탭을 멎게 했다(0.1.8 실배포 결함). 뷰포트 자체를 줄일 수
  // 있는 건 브라우저뿐이라 네이티브 패널로 돌아왔다. 그 시절 코드가 되살아나지 않도록
  // "페이지를 안 건드린다"를 불변식으로 못 박는다.
  if (!runtimeMessages.some(m => m.type === 'OPEN_PANEL')) {
    throw new Error('검토 패널 열기 요청(OPEN_PANEL)이 SW 로 나가지 않았다 — 패널이 아예 안 뜬다');
  }
  if (injectedOverlays().length !== 0) {
    throw new Error('네이티브 패널이 열렸는데 페이지에 오버레이를 주입했다 — 사이트를 이중으로 덮는다');
  }

  const htmlStyle = documentStub.documentElement.style;
  const bodyStyle = documentStub.body.style;
  for (const [label, style] of [['html', htmlStyle], ['body', bodyStyle]]) {
    if (style._props.size !== 0) {
      throw new Error(`${label} 인라인 스타일을 건드렸다 (${[...style._props.keys()].join(', ')}) — 밀어내기는 원리적으로 안 먹혔고 사이트만 망가뜨린다`);
    }
  }
  if (dispatchedWindowEvents.includes('resize')) {
    throw new Error('가짜 resize 를 쐈다 — 무한 재귀로 탭이 멎었던 코드가 되살아났다');
  }

  // (8) 그 OPEN_PANEL 이 거부되면 iframe 오버레이로 폴백해야 한다.
  //
  // sidePanel.open() 은 사용자 제스처에 대한 응답으로만 허용되는데, 그 제스처가
  // content → SW 메시징을 타고 전파되는 건 Chromium 구현에 달려 있다(issue 355266358).
  // 전파가 깨지는 경로(예: 제스처가 아닌 postMessage 로 시작되는 흐름)에서 패널이 아예
  // 안 뜨면 검토 없이 원본이 나가버리므로, 사이트를 덮더라도 오버레이로라도 띄운다.
  await new Promise(r => setTimeout(r, 3200)); // 테스트 6이 세운 promptApproved 해제 대기

  failNextOpenPanel = true;
  appendedToRoot.length = 0;
  promptEditorStub.value = '폴백 확인용 프롬프트';
  documentStub.activeElement = promptEditorStub;
  dispatchDocumentEvent('keydown', {
    key: 'Enter', shiftKey: false, ctrlKey: false, altKey: false, metaKey: false,
    preventDefault() {},
    stopImmediatePropagation() {},
  });
  await flush();

  const overlayHost = injectedOverlays()[0];
  if (!overlayHost) {
    throw new Error('OPEN_PANEL 이 거부됐는데 iframe 오버레이 폴백이 뜨지 않았다 — 검토 없이 원본이 나간다');
  }
  const overlayFrame = overlayHost.children.find(c => c.tagName === 'IFRAME');
  if (!overlayFrame || !String(overlayFrame.src).endsWith('sidepanel/sidepanel.html')) {
    throw new Error('폴백 오버레이가 검토 패널을 로드하지 않는다');
  }

  // 폴백 오버레이는 자기 iframe 의 contentWindow 에서 온 UPS_CLOSE_OVERLAY 로만 닫힌다
  // (네이티브 패널은 자기 window.close() 로 닫으므로 이 경로를 타지 않는다).
  for (const l of windowListeners.get('message') || []) {
    l({ source: overlayFrame.contentWindow, data: { type: 'UPS_CLOSE_OVERLAY' } });
  }
  await flush();
  if (!overlayHost.removed) {
    throw new Error('UPS_CLOSE_OVERLAY 를 받고도 폴백 오버레이가 페이지에 남아있다');
  }

  // (9) 검사가 진행 중일 때 들어온 전송 시도는 사이트로 새어나가면 안 된다.
  //
  // 예전엔 promptInProcess 면 리스너가 preventDefault 없이 그냥 return 해서, 그 이벤트가
  // 사이트로 흘러가 검사가 끝나기도 전에 원본 프롬프트가 전송됐다(실사용자 macOS 재현:
  // "보안 분석 중" 에서 안 넘어가는데 메시지만 먼저 들어감). 마스킹 전 원본이 나가는
  // 것이므로 이 제품에서 가장 치명적인 실패다.
  // 앞 테스트들이 남긴 상태를 둘 다 정리해야 이 테스트가 자기 상태에서 시작한다:
  //   promptApproved  — 승인 직후 3초간 남아 있다(우리 재전송을 통과시키려고).
  //   promptInProcess — 결정이 안 온 검사가 아직 진행 중일 수 있다.
  // 3초 타이머는 앞 테스트의 재전송이 끝난 뒤에야 시작되므로, 그 지연까지 넉넉히 덮는다
  // (3.2초로는 아슬아슬하게 걸려 간헐적으로 실패했다).
  await new Promise(r => setTimeout(r, 4500));
  const stuckScan = runtimeMessages.filter(m => m.type === 'START_SCAN').slice(-1)[0];
  decisionListener?.({
    type: 'PANEL_DECISION', sessionId: stuckScan.sessionId, decision: { action: 'cancel' },
  });
  await flush();

  promptEditorStub.value = '주민번호 900101-1234567 포함된 프롬프트';
  documentStub.activeElement = promptEditorStub;
  nextDecision = null;          // 결정을 회신하지 않아 검사가 계속 진행 중인 상태를 만든다
  const beforeScan = runtimeMessages.length;
  dispatchDocumentEvent('keydown', {
    key: 'Enter', shiftKey: false, ctrlKey: false, altKey: false, metaKey: false,
    preventDefault() {}, stopImmediatePropagation() {},
  });
  await flush();
  if (!runtimeMessages.slice(beforeScan).some(m => m.type === 'START_SCAN')) {
    throw new Error('첫 전송에서 검사가 시작되지 않았다 — 이 테스트의 전제가 깨졌다');
  }
  const afterFirstScan = runtimeMessages.length;

  // 검사가 아직 안 끝난 상태에서 사용자가 한 번 더 Enter 를 누른다.
  let leaked = true;  // 막히면 preventDefault 가 불려 false 로 바뀐다
  let propagated = true;
  dispatchDocumentEvent('keydown', {
    key: 'Enter', shiftKey: false, ctrlKey: false, altKey: false, metaKey: false,
    preventDefault() { leaked = false; },
    stopImmediatePropagation() { propagated = false; },
  });
  await flush();

  if (leaked || propagated) {
    throw new Error('검사 중 눌린 전송이 사이트로 새어나갔다 — 마스킹 전 원본이 전송된다');
  }
  if (runtimeMessages.slice(afterFirstScan).some(m => m.type === 'START_SCAN')) {
    throw new Error('검사가 이미 진행 중인데 또 다른 검사를 시작했다');
  }

  // (9) 사이트별 선택자가 깨져도 검토 흐름이 시작돼야 한다.
  //
  // 배경(실사용자 리포트): copilot.microsoft.com / perplexity.ai 는 사이드바가 아예
  // 안 뜨고, gemini.google.com 은 뜨는데 전송이 안 된다. 트리거 경로가 셋인데 전부
  // PROMPT_CONFIGS 의 선택자에 걸려 있어서, 사이트가 개편되면 editorSel 이 깨진 곳은
  // 사이드바 자체가 안 뜨고(click/keydown 둘 다 막힘) sendBtnSel 만 깨진 곳은 검토는
  // 되는데 재전송이 실패한다. 게다가 아무 로그도 없어 원인 파악이 안 됐다.
  //
  // 여기서는 두 선택자를 전부 문서에서 없애(=개편으로 낡은 상태) 그래도
  //   - Enter 로 검사가 시작되고(포커스된 편집 요소로 폴백)
  //   - 재전송이 일반 전송 버튼 후보로 폴백해 실제로 클릭되는지
  // 를 확인한다.
  const pendingScan9 = runtimeMessages.filter(m => m.type === 'START_SCAN').slice(-1)[0];
  decisionListener?.({
    type: 'PANEL_DECISION', sessionId: pendingScan9.sessionId, decision: { action: 'cancel' },
  });
  await flush();
  await new Promise(r => setTimeout(r, 4500)); // promptApproved(3초) 해제 대기

  // 사이트 개편 재현: 설정된 선택자가 문서에서 하나도 안 잡히게 만든다.
  domBySelector.delete('#prompt-textarea');
  domBySelector.delete('[data-testid="send-button"]');
  // 대신 일반 후보로 잡히는 전송 버튼만 남는다.
  const genericSendBtn = new SendButtonStub();
  genericSendBtn.disabled = false;
  domBySelector.set('button[type="submit"]', genericSendBtn);
  // 사용자가 실제로 글을 쓰고 있는 입력창(포커스됨) — 선택자로는 못 찾는다.
  promptEditorStub.value = '선택자가 깨진 사이트에서 보내는 프롬프트';
  documentStub.activeElement = promptEditorStub;

  const before9 = runtimeMessages.length;
  nextDecision = { action: 'masked', maskedText: '마스킹된 프롬프트' };
  dispatchDocumentEvent('keydown', {
    key: 'Enter', shiftKey: false, ctrlKey: false, altKey: false, metaKey: false,
    preventDefault() {}, stopImmediatePropagation() {},
  });
  await flush();

  const scan9 = runtimeMessages.slice(before9).find(m => m.type === 'START_SCAN');
  if (!scan9) {
    throw new Error('선택자가 깨지자 검사가 아예 시작되지 않았다 — 사이드바가 안 뜨는 증상 그대로다');
  }
  // resubmitPrompt 는 200ms 대기 후 폴링하므로 실제로 클릭될 때까지 기다린다.
  for (let i = 0; i < 30 && genericSendBtn.clicks < 1; i += 1) {
    await new Promise(r => setTimeout(r, 100));
  }
  if (genericSendBtn.clicks < 1) {
    throw new Error('일반 전송 버튼 후보로 폴백하지 못했다 — 검토는 되는데 전송이 안 되는 증상 그대로다');
  }

  console.log('content regression ok');
  process.exit(0);
})();
