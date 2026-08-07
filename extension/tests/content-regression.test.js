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
  // isConnected: 진짜 input 은 DOM 에 붙어 있다. 재주입 코드가 "이 노드가 아직
  // 살아 있나"를 이걸로 판단하므로 기본값이 true 여야 실제와 같다. 테스트 (9)에서만
  // false 로 내려 SPA 재렌더로 떨어져 나간 상황을 만든다.
  // id: 어느 input 에 주입됐는지 구분하려고 순서 로그에 같이 남긴다.
  constructor(file = null, id = 'input') {
    super();
    this.type = 'file';
    this.files = file ? [file] : [];
    this.value = '';
    this.isConnected = true;
    this.id = id;
  }
  // setFileOnInput 이 마스킹본을 넣고 input/change 를 쏘는 시점을 순서 로그에 남긴다.
  dispatchEvent() { actionLog.push({ kind: 'inject', id: this.id }); return true; }
  // 되돌려 붙인 노드를 다시 떼어내는 경로((22))가 실제로 떼어냈는지 보려면 필요하다.
  // host.appended 는 "붙인 적이 있다"는 **기록**이므로 여기서 건드리지 않는다 — (16)이
  // 그걸로 "되돌리기 전략을 시도했는지" 를 판정한다. 떼어냈는지는 isConnected 로 본다.
  remove() {
    this.isConnected = false;
    this.parentElement = null;
  }
}
class HTMLTextAreaElementStub extends EventTargetStub {
  constructor(value = '') {
    super();
    this.tagName = 'TEXTAREA';
    // 증거 판정(watchAttachmentEvidence)은 관찰 루트가 진짜 엘리먼트인지 nodeType 으로
    // 확인한다. 실제 입력창은 당연히 1이므로 stub 도 맞춰준다.
    this.nodeType = 1;
    this.value = value;
    // 진짜 입력창은 DOM 에 붙어 있다. 재주입 폴백이 "이 요소가 살아 있나"를
    // isConnected 로 보므로 기본값이 true 여야 실제와 같다(테스트 12).
    this.isConnected = true;
    this.dispatched = [];
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
  dispatchEvent(event) { this.dispatched.push(event); return true; }
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
  constructor() {
    this.files = [];
    this.items = { add: f => this.files.push(f) };
    this._data = new Map();
  }
  setData(type, value) { this._data.set(type, String(value)); }
  getData(type) { return this._data.get(type) ?? ''; }
}

// ── 선택 영역 모델 ────────────────────────────────────────────────────────────
// 브라우저에서 insertText/paste 는 "선택 영역을 대체" 한다. 선택이 잡혀 있지 않으면
// 캐럿 자리에 덧붙는다 — 쌓임은 항상 여기서 난다. 이 구분을 모델링하지 않으면
// (20) 이 잡아내는 회귀를 테스트로 재현할 수 없다.
let selectedNode = null;
const selectionStub = {
  removeAllRanges() { selectedNode = null; },
  addRange(range) { selectedNode = range?.node ?? null; },
};

/** 프레임워크형 contenteditable(Lexical/Quill 계열) 흉내 — 테스트 18·20.
 *  · 자기 모델만 신뢰한다: textContent 직접 대입은 무시한다
 *  · 문단을 블록으로 렌더한다 → innerText 에 개행이 하나 더 낀다(Chrome 실측 동작)
 *  · acceptDelete=false 면 지우기를 무시한다(= clear() 가 안 먹는 에디터)
 *  · 삽입은 선택 영역을 대체한다. 선택이 없으면 덧붙는다(브라우저 실제 동작).
 *    그리고 textContent 대입은 값 자체는 무시하더라도 **선택을 부순다** — 자식 노드가
 *    통째로 갈리기 때문이며, 이게 perplexity 에서 원문과 마스킹본이 공존한 기전이다. */
class ContentEditableStub {
  constructor(initial, {
    acceptDelete = false, acceptSelectionReplace = true, partialDelete = false,
  } = {}) {
    this.partialDelete = partialDelete;
    this.tagName = 'DIV';
    this.nodeType = 1;
    this.isContentEditable = true;
    this.isConnected = true;
    this.acceptDelete = acceptDelete;
    // false 면 선택 영역 대체조차 안 먹는다 = 우리가 어떤 방법으로도 못 바꾸는 에디터.
    this.acceptSelectionReplace = acceptSelectionReplace;
    this.lines = initial ? String(initial).split('\n') : [];
    this.inserts = 0;
    this.deletesBeforeFirstInsert = 0;
    const props = new Map();
    this.style = {
      getPropertyValue: (k) => props.get(k)?.value ?? '',
      getPropertyPriority: (k) => props.get(k)?.priority ?? '',
      setProperty: (k, v, p = '') => props.set(k, { value: v, priority: p }),
      removeProperty: (k) => props.delete(k),
    };
  }
  get innerText() { return this.lines.join('\n\n'); }   // 블록 사이 개행 2개
  get textContent() { return this.lines.join('\n\n'); }
  set textContent(_v) {
    // 값은 무시하지만(프레임워크가 자기 모델을 지킨다) 선택은 실제로 부서진다.
    selectedNode = null;
  }
  focus() { documentStub.activeElement = this; }
  acceptInsert(v) {
    if (!v) return;
    if (selectedNode === this && this.acceptSelectionReplace) this.lines = []; // 선택 영역 대체
    for (const line of String(v).split('\n')) this.lines.push(line);
    selectedNode = null;                        // 삽입하면 선택은 접힌다
    this.inserts += 1;
  }
  acceptDeleteAll() {
    if (this.inserts === 0) this.deletesBeforeFirstInsert += 1; // (21) 이 보는 값
    if (!this.acceptDelete) return;
    if (this.partialDelete) {
      // 지우기가 "부분적으로만" 먹는 에디터 — 실사용자 증상의 기전이다. 앞부분만
      // 지워지고 꼬리가 조각으로 남는다. 조각이 남는다는 사실 자체가 핵심이라
      // 얼마나 남는지는 중요하지 않다.
      const tail = this.lines.join('\n').slice(-6);
      this.lines = tail ? [tail] : [];
      selectedNode = null; // 부분 삭제도 선택을 무너뜨린다
      return;
    }
    this.lines = [];
    selectedNode = null;
  }
  dispatchEvent(ev) {
    if (ev?.type === 'paste') this.acceptInsert(ev.clipboardData?.getData?.('text/plain') ?? '');
    return true;
  }
  closest() { return null; }
  contains() { return false; }
  getAttribute() { return null; }
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

// content.js 가 콘솔에 찍은 줄 (테스트 14-a: 진단 로그가 필요한 때만 나오는지).
// 실제 콘솔로도 그대로 흘려보내 기존처럼 눈으로 볼 수 있게 둔다.
const consoleLines = [];
function captureConsole(fn) {
  return (...args) => {
    consoleLines.push(args.map(a => String(a)).join(' '));
    fn(...args);
  };
}
const consoleStub = {
  log: captureConsole(console.log.bind(console)),
  warn: captureConsole(console.warn.bind(console)),
  error: captureConsole(console.error.bind(console)),
  info: captureConsole(console.info.bind(console)),
  debug: () => {},
};
const diagCount = () => consoleLines.filter(l => l.includes('[SecureDoc][진단]')).length;

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
  getSelection: () => selectionStub,
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

// querySelectorAll 용 — waitForAttachmentReady 의 "진행 중 표시" 탐지(테스트 15)에서
// 진행률 바가 떴다가 사라지는 것을 흉내내려면 이게 제어 가능해야 한다.
const domBySelectorAll = new Map();

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
  querySelectorAll: (sel) => domBySelectorAll.get(sel) ?? [],
  // 실제 execCommand 는 "지금 포커스된 편집 요소"에 작용한다. 테스트 18의 프레임워크
  // 에디터가 그 동작을 받아볼 수 있어야 삽입/지우기 시도를 셀 수 있다. 다른 테스트의
  // 입력창 stub 에는 이 메서드들이 없으므로 예전처럼 아무 일도 일어나지 않는다.
  execCommand: (cmd, _showUI, value) => {
    const ed = documentStub.activeElement;
    if (cmd === 'insertText') ed?.acceptInsert?.(value);
    if (cmd === 'delete') ed?.acceptDeleteAll?.();
    if (cmd === 'selectAll') selectedNode = ed ?? null;
    return true;
  },
  // Range 는 "무엇을 선택했는가" 만 알면 되므로 노드만 들고 있는다.
  createRange: () => ({ node: null, selectNodeContents(n) { this.node = n; } }),
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
  console: consoleStub,
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
  // init 을 보관해야 setEditorText 의 붙여넣기/삭제 시도를 에디터 stub 이 받아볼 수 있다.
  InputEvent: class { constructor(type, init = {}) { this.type = type; Object.assign(this, init); } },
  KeyboardEvent: class { constructor(type, init = {}) { this.type = type; Object.assign(this, init); } },
  DragEvent: DragEventStub,
  ClipboardEvent: class { constructor(type, init = {}) { this.type = type; Object.assign(this, init); } },
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
  // 첨부 업로드 대기(waitForAttachmentReady)는 전송 버튼이 잠기는 걸 신호로 쓴다.
  // 여기선 그 신호가 없는 사이트를 모사한다 — 짧은 고정 대기로 물러난 뒤 전송한다.
  sendButtonStub.disabled = false;
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
  // 테스트 4가 세운 promptApproved(재전송 후 3초) 해제 대기.
  // 첨부가 보류된 제출은 이제 waitForAttachmentReady 를 거쳐 전송하므로 테스트 4가
  // 그만큼 늦게 끝난다 — 실측으로 이 값이 필요했다(6초로는 아직 promptApproved 가
  // 살아 있어 Enter 가 통째로 무시됐다).
  await new Promise(r => setTimeout(r, 12000));
  // (첨부 대기 waitForAttachmentReady 가 붙어 테스트 4 가 그만큼 늦게 끝난다)
  // 2026-08-05: 신호 없음 경로가 "2.5초 관측 + 900ms 고정 대기"에서 "1.5초 관측"으로
  // 짧아져 테스트 4 가 약 1.9초 빨리 끝난다. 이 값은 하한이라 그대로 둬도 안전하고,
  // 여유를 남겨 두면 되돌리기 실험(수정 전 코드로 되돌려 돌려보기)도 그대로 돌아간다.

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
  // 첨부가 보류돼 있으면 전송 전에 waitForAttachmentReady 가 "버튼이 다시 열릴 때까지"
  // 기다린 뒤에야 resubmitPrompt 로 넘어간다 — 그 왕복까지 덮는 창이어야 한다.
  await new Promise(r => setTimeout(r, 2000));
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
  await new Promise(r => setTimeout(r, 7000));
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

  // (10) 마스킹본은 "주입 시점에 살아 있는" 파일 input 으로 들어가야 한다.
  //
  // 배경(실사용자 리포트): "인젝션 검사는 되는데 전송을 눌러도 파일이 안 간다".
  // 첨부를 가로챈 순간부터 검토 패널에서 승인할 때까지 수 초~수십 초가 흐르는데
  // (그 사이 프롬프트를 다 입력한다), SPA 가 컴포저를 다시 그리면 가로챌 때 붙들어
  // 둔 input 은 DOM 에서 떨어져 나간 고아 노드가 된다. 거기에 파일을 넣고 change 를
  // 쏘면 예외도 안 나고 input.files 에도 들어가지만, 그 이벤트는 document 까지
  // 버블링하지 않아 사이트는 아무것도 못 받는다 — 파일만 조용히 사라지고 프롬프트는
  // 그대로 전송된다. drop/paste 경로는 이미 주입 시점에 다시 찾고 있었는데 파일
  // 선택(📎) 경로만 예전 방식으로 남아 있었다.
  // 앞 테스트는 일부러 결정을 회신하지 않아 검사를 진행 중으로 남겨뒀다
  // (promptInProcess=true). 취소해서 이 테스트가 깨끗한 상태에서 시작하게 한다.
  const pendingScan = runtimeMessages.filter(m => m.type === 'START_SCAN').slice(-1)[0];
  decisionListener?.({
    type: 'PANEL_DECISION', sessionId: pendingScan.sessionId, decision: { action: 'cancel' },
  });
  await flush();

  const file9 = new FileStub(['pdf bytes'], 'stale.pdf', { type: 'application/pdf' });
  const staleInput = new HTMLInputElementStub(file9, 'stale');
  dispatchDocumentEvent('change', {
    target: staleInput,
    composedPath: () => [staleInput, documentStub],
    preventDefault() {},
    stopImmediatePropagation() {},
  });
  await flush();

  // 여기서 사이트가 컴포저를 다시 그렸다 — 우리가 들고 있던 input 은 죽고 새 것이 생겼다.
  staleInput.isConnected = false;
  const liveInput = new HTMLInputElementStub(null, 'live');
  domBySelector.set('input[type="file"]', liveInput);

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
      fileName: 'stale.pdf',
    },
  };
  dispatchDocumentEvent('keydown', {
    key: 'Enter', shiftKey: false, ctrlKey: false, altKey: false, metaKey: false,
    preventDefault() {},
    stopImmediatePropagation() {},
  });
  await flush();

  const injected = actionLog.filter(e => e.kind === 'inject');
  if (injected.some(e => e.id === 'stale')) {
    throw new Error('재렌더로 떨어져 나간 낡은 input 에 주입했다 — 사이트는 파일을 못 받는다');
  }
  if (!injected.some(e => e.id === 'live')) {
    throw new Error('살아 있는 input 에 마스킹본이 주입되지 않았다 — 파일이 전송되지 않는다');
  }
  if (liveInput.files?.length !== 1) {
    throw new Error(`살아 있는 input 에 파일이 담기지 않았다: ${liveInput.files?.length}`);
  }

  // (11) 사이트별 선택자가 깨져도 검토 흐름이 시작돼야 한다.
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
  await new Promise(r => setTimeout(r, 7000)); // promptApproved(3초) 해제 대기

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

  // (12) 파일 input 이 아예 없는 사이트에서도 문서가 페이지로 들어가야 한다.
  //
  // 배경(실사용자 콘솔, gemini.google.com):
  //   [SecureDoc] 파일 재주입 실패: 살아 있는 input[type=file] 을 찾지 못했습니다
  //     injectFileIntoInput @ content.js:177
  //     (익명) @ content.js:498        ← 파일 선택(📎) 경로의 주입 콜백
  // Gemini 는 첨부 메뉴를 닫으면 input[type=file] 을 DOM 에서 통째로 없앤다. 그래서
  // 승인 시점엔 넣을 곳이 없는데 📎 경로에는 폴백이 없어 파일이 조용히 버려지고
  // 프롬프트만 전송됐다. drop/paste 경로에만 있던 합성 drop 폴백을 여기에도 태운다.
  const pendingScan12 = runtimeMessages.filter(m => m.type === 'START_SCAN').slice(-1)[0];
  decisionListener?.({
    type: 'PANEL_DECISION', sessionId: pendingScan12.sessionId, decision: { action: 'cancel' },
  });
  await flush();
  await new Promise(r => setTimeout(r, 7000)); // promptApproved(3초) 해제 대기

  // 선택자를 (11) 이 지웠으므로 되돌려 놓는다 — 이 테스트는 정상 사이트 전제다.
  domBySelector.set('#prompt-textarea', promptEditorStub);
  const send12 = new SendButtonStub();
  send12.disabled = false;
  domBySelector.set('[data-testid="send-button"]', send12);

  // 첨부는 정상적으로 가로챈다(이때는 input 이 살아 있다).
  const file12 = new FileStub(['pdf bytes'], 'gemini.pdf', { type: 'application/pdf' });
  const input12 = new HTMLInputElementStub(file12, 'gone');
  dispatchDocumentEvent('change', {
    target: input12,
    composedPath: () => [input12, documentStub],
    preventDefault() {},
    stopImmediatePropagation() {},
  });
  await flush();

  // Gemini 재현: 승인 전에 input 이 DOM 에서 사라진다(문서 어디에도 없다).
  input12.isConnected = false;
  domBySelector.delete('input[type="file"]');
  actionLog.length = 0;
  promptEditorStub.dispatched.length = 0;   // 합성 drop 이 여기로 와야 한다
  documentStub.activeElement = promptEditorStub;
  promptEditorStub.value = '이 문서를 요약해줘';
  nextDecision = {
    action: 'send',
    maskedText: '이 문서를 요약해줘',
    file: {
      action: 'upload',
      maskedBase64: btoa('masked pdf bytes'),
      mimeType: 'application/pdf',
      fileName: 'gemini.pdf',
    },
  };
  dispatchDocumentEvent('keydown', {
    key: 'Enter', shiftKey: false, ctrlKey: false, altKey: false, metaKey: false,
    preventDefault() {}, stopImmediatePropagation() {},
  });
  await flush();

  // 되돌릴 부모가 없는 경우(원래 부모까지 사라짐)라 최후 수단인 합성 drop 으로 간다.
  const dropped = promptEditorStub.dispatched.filter(e => e.type === 'drop' && e.dataTransfer?.files?.length);
  if (!dropped.length) {
    throw new Error('input 이 사라진 사이트에서 문서가 페이지로 전혀 들어가지 않았다 — 프롬프트만 전송된다');
  }
  if (dropped[0].dataTransfer.files[0]?.name !== 'gemini.pdf') {
    throw new Error(`합성 drop 에 실린 파일이 다르다: ${dropped[0].dataTransfer.files[0]?.name}`);
  }

  // (13) 원래 부모가 살아 있으면 합성 drop 이 아니라 "input 되돌리기" 를 쓴다.
  //
  // 합성 drop 은 사이트의 드래그 상태 머신에 기대는데, 그 상태가 없으면 사이트
  // 핸들러가 "this.drop is not a function" 으로 터진다(실사용자 Gemini 콘솔).
  // 리스너 안에서 난 예외라 우리 try/catch 로도 못 잡고, 사이트의 드롭 처리만
  // 조용히 중단된다. 그래서 노드를 원래 자리에 되돌려 놓는 쪽을 먼저 시도해야 한다.
  const pendingScan13 = runtimeMessages.filter(m => m.type === 'START_SCAN').slice(-1)[0];
  decisionListener?.({
    type: 'PANEL_DECISION', sessionId: pendingScan13.sessionId, decision: { action: 'cancel' },
  });
  await flush();
  await new Promise(r => setTimeout(r, 7000));

  // 사이트의 컴포저(부모)는 살아 있고, 그 안의 input 만 떼어진 상황.
  const parent13 = new DropTargetStub();
  parent13.appended = [];
  parent13.appendChild = function (node) { this.appended.push(node); node.isConnected = true; };
  const input13 = new HTMLInputElementStub(
    new FileStub(['pdf bytes'], 'revive.pdf', { type: 'application/pdf' }), 'orphan',
  );
  input13.parentElement = parent13;

  dispatchDocumentEvent('change', {
    target: input13,
    composedPath: () => [input13, documentStub],
    preventDefault() {}, stopImmediatePropagation() {},
  });
  await flush();

  input13.isConnected = false;              // 사이트가 떼어냈다
  domBySelector.delete('input[type="file"]');
  actionLog.length = 0;
  promptEditorStub.dispatched.length = 0;
  documentStub.activeElement = promptEditorStub;
  promptEditorStub.value = '이 문서를 요약해줘';
  nextDecision = {
    action: 'send',
    maskedText: '이 문서를 요약해줘',
    file: {
      action: 'upload', maskedBase64: btoa('masked'),
      mimeType: 'application/pdf', fileName: 'revive.pdf',
    },
  };
  dispatchDocumentEvent('keydown', {
    key: 'Enter', shiftKey: false, ctrlKey: false, altKey: false, metaKey: false,
    preventDefault() {}, stopImmediatePropagation() {},
  });
  await flush();

  if (!parent13.appended.includes(input13)) {
    throw new Error('원래 부모가 살아 있는데 input 을 되돌려 놓지 않았다');
  }
  if (!actionLog.some(e => e.kind === 'inject' && e.id === 'orphan')) {
    throw new Error('되돌린 input 에 마스킹본이 주입되지 않았다');
  }
  const drops13 = promptEditorStub.dispatched.filter(e => e.type === 'drop');
  if (drops13.length) {
    throw new Error('되돌리기로 충분한데 합성 drop 까지 쐈다 — 사이트 핸들러를 터뜨릴 수 있다');
  }

  // (14) 전송 버튼이 업로드 내내 활성인 사이트(Gemini)에서도, 첨부 업로드가 끝날
  //      때까지 기다렸다가 전송해야 한다.
  //
  // 배경(실사용자 gemini.google.com 콘솔):
  //   [SecureDoc] 파일 재주입: 사이트가 떼어낸 input 을 되돌려 놓았습니다
  //   [SecureDoc] 첨부 대기: 업로드 신호 없음 — 900ms 후 전송합니다
  //   [SecureDoc] 재전송: 버튼 클릭 성공 (207ms, sel=button[aria-label="메시지 보내기"])
  // 파일 주입은 성공했는데 207ms 만에 전송됐다. Gemini 는 업로드 중에도 전송 버튼을
  // 잠그지 않아서 "버튼 잠김 → 열림" 신호가 아예 안 잡히고 900ms 폴백으로 떨어진
  // 것이다. 그 900ms 안에 업로드가 끝날 리 없으니 프롬프트만 먼저 나가고 첨부가 빠진다.
  //
  // 이제는 MAIN world(interceptor.js)가 XHR/fetch 로 관측한 "파일 업로드 진행 중"을
  // UPS_UPLOAD_ACTIVITY 로 알려주고, content.js 가 그게 끝날 때까지 기다린다.
  // 여기서는 그 사이트를 모사한다: 전송 버튼은 처음부터 끝까지 활성이고, 업로드는
  // 5초 걸린다. 그 5초 동안 단 한 번도 눌리면 안 된다.
  //
  // 앞 테스트(13)의 재전송 + promptApproved(3초) 해제 대기.
  // 지금 코드에서 (13)이 끝나는 데 걸리는 시간은 첨부 대기 1.5초 + 재전송 폴링 0.2초
  // + promptApproved 3초 = 약 4.7초다. 그런데 이 값을 4.7초에 맞춰 깎으면, 수정을
  // 되돌렸을 때(첨부 대기가 2.5초 관측 + 900ms 고정 = 3.4초로 길어진다) (13)이 6.6초에
  // 끝나면서 이 테스트의 Enter 가 promptApproved 에 통째로 먹혀 "검사가 시작되지
  // 않았다"로 엉뚱하게 실패한다 — 되돌리기 실험이 무의미해진다. 두 경우를 모두 덮도록
  // 9초로 잡는다.
  await new Promise(r => setTimeout(r, 9000));

  // (14-a) 신호를 하나도 못 본 경로에서는 진단 기록이 남아야 한다.
  //
  // Gemini 가 딱 그 경로인데, 지금까지는 "신호 없음" 한 줄만 찍고 끝나서 왜 못 봤는지
  // 알 방법이 없었다. 그 결과 같은 사이트에서 신호를 세 번이나 헛짚었다. 앞선 테스트
  // (4)(12)(13)이 모두 이 경로였으므로 여기까지 왔으면 진단 줄이 있어야 한다.
  if (diagCount() === 0) {
    throw new Error('업로드 신호를 하나도 못 봤는데 진단 기록이 남지 않았다 — 원인 파악용 데이터가 없다');
  }

  // (14-b) 반대로 신호가 정상적으로 잡히는 경로에서는 진단이 조용해야 한다.
  //        최종 사용자에게 상시 노이즈가 되면 안 되므로 아래 (14) 구간 동안 진단 줄이
  //        하나도 늘지 않는 것을 확인한다.
  const diagBefore = diagCount();

  const send14 = new SendButtonStub();
  send14.disabled = false;                    // Gemini: 업로드 중에도 계속 활성
  domBySelector.set('[data-testid="send-button"]', send14);

  const file14 = new FileStub(['pdf bytes'], 'gemini-upload.pdf', { type: 'application/pdf' });
  const input14 = new HTMLInputElementStub(file14, 'live14');
  dispatchDocumentEvent('change', {
    target: input14,
    composedPath: () => [input14, documentStub],
    preventDefault() {}, stopImmediatePropagation() {},
  });
  await flush();

  promptEditorStub.value = '이 문서를 요약해줘';
  documentStub.activeElement = promptEditorStub;
  nextDecision = {
    action: 'send',
    maskedText: '이 문서를 요약해줘',
    file: {
      action: 'upload', maskedBase64: btoa('masked pdf bytes'),
      mimeType: 'application/pdf', fileName: 'gemini-upload.pdf',
    },
  };
  dispatchDocumentEvent('keydown', {
    key: 'Enter', shiftKey: false, ctrlKey: false, altKey: false, metaKey: false,
    preventDefault() {}, stopImmediatePropagation() {},
  });
  await flush();

  // 사이트가 마스킹본을 자기 서버로 올리기 시작했다(MAIN world 관측기의 브로드캐스트).
  await dispatchWindowMessage({
    __campfire_config: true, direction: 'main-to-isolated',
    type: 'UPS_UPLOAD_ACTIVITY', phase: 'start', inflight: 1,
  });

  // 업로드가 5초 걸린다. 예전 코드는 2.5초 관측 + 900ms 고정 대기 후 약 3.6초에
  // 눌러버렸다 — 그 회귀를 여기서 잡는다.
  await new Promise(r => setTimeout(r, 5000));
  if (send14.clicks !== 0) {
    throw new Error(`첨부 업로드가 아직 끝나지 않았는데 전송했다 (clicks=${send14.clicks}) — 프롬프트만 먼저 나가고 첨부가 빠진다`);
  }

  await dispatchWindowMessage({
    __campfire_config: true, direction: 'main-to-isolated',
    type: 'UPS_UPLOAD_ACTIVITY', phase: 'end', inflight: 0,
  });
  for (let i = 0; i < 40 && send14.clicks < 1; i += 1) {
    await new Promise(r => setTimeout(r, 100));
  }
  if (send14.clicks !== 1) {
    throw new Error(`업로드가 끝났는데도 전송되지 않았다 (clicks=${send14.clicks})`);
  }
  if (diagCount() !== diagBefore) {
    throw new Error(
      `업로드 신호가 정상적으로 잡힌 경로에서 진단 로그가 나왔다 (${diagCount() - diagBefore}줄) — 평소엔 조용해야 한다`,
    );
  }

  // (15) 네트워크 신호를 못 받는 경우엔 진행률/스피너 표시를 신호로 쓴다.
  //
  // interceptor.js 가 못 보는 경로(워커 업로드 등)로 올리는 사이트를 대비한 2차 신호.
  // 중요한 건 "대기 시작 시점보다 늘어난 것"만 신호로 본다는 점이다 — 답변 스트리밍
  // 인디케이터처럼 원래부터 떠 있는 progressbar 에 걸리면 매번 60초를 기다리게 된다.
  // 그래서 여기서는 기준선으로 하나를 미리 띄워두고, 그 위에 업로드용 하나를 더
  // 얹었다가 내린다.
  // (14)의 재전송 직후부터 promptApproved 3초가 흐른다 — 여유를 두고 5초 기다린다.
  await new Promise(r => setTimeout(r, 5000));

  const send15 = new SendButtonStub();
  send15.disabled = false;
  domBySelector.set('[data-testid="send-button"]', send15);
  domBySelectorAll.set('[role="progressbar"]', [{ id: 'always-there' }]); // 기준선

  const file15 = new FileStub(['pdf bytes'], 'spinner.pdf', { type: 'application/pdf' });
  const input15 = new HTMLInputElementStub(file15, 'live15');
  dispatchDocumentEvent('change', {
    target: input15,
    composedPath: () => [input15, documentStub],
    preventDefault() {}, stopImmediatePropagation() {},
  });
  await flush();

  promptEditorStub.value = '이 문서를 요약해줘';
  documentStub.activeElement = promptEditorStub;
  nextDecision = {
    action: 'send',
    maskedText: '이 문서를 요약해줘',
    file: {
      action: 'upload', maskedBase64: btoa('masked pdf bytes'),
      mimeType: 'application/pdf', fileName: 'spinner.pdf',
    },
  };
  dispatchDocumentEvent('keydown', {
    key: 'Enter', shiftKey: false, ctrlKey: false, altKey: false, metaKey: false,
    preventDefault() {}, stopImmediatePropagation() {},
  });
  await flush();

  // 주입 직후(= 기준선을 잡은 뒤) 첨부 칩의 진행률 표시가 하나 더 뜬다.
  domBySelectorAll.set('[role="progressbar"]', [{ id: 'always-there' }, { id: 'upload' }]);
  await new Promise(r => setTimeout(r, 4500));
  if (send15.clicks !== 0) {
    throw new Error(`진행률 표시가 떠 있는데 전송했다 (clicks=${send15.clicks}) — 업로드 도중 전송이다`);
  }

  // 업로드 완료 → 진행률 표시만 사라지고 기준선은 그대로 남는다.
  domBySelectorAll.set('[role="progressbar"]', [{ id: 'always-there' }]);
  for (let i = 0; i < 40 && send15.clicks < 1; i += 1) {
    await new Promise(r => setTimeout(r, 100));
  }
  if (send15.clicks !== 1) {
    throw new Error(`진행률 표시가 사라졌는데도 전송되지 않았다 (clicks=${send15.clicks}) — 기준선 progressbar 에 걸려 계속 기다린다`);
  }

  // (16) 주입은 "넣었다"가 아니라 "사이트가 받았다"로 판정해야 한다.
  //
  // 배경(실사용자 gemini.google.com 진단, 2026-08-06):
  //   [SecureDoc] 파일 재주입: 사이트가 떼어낸 input 을 되돌려 놓았습니다
  //   [진단] ===== 컴포저 DOM 변화 · 첨부 대기 창 · 0건 =====
  //   [진단] 요청 추적 · 첨부 대기 창 · 3건 — 최대 바디 str(167B)
  //   [진단] 요청 추적 · 전송 후 8초 · 15건 — 파일 업로드 0건
  // revive 는 노드를 되붙이고 files 를 채우고 change 를 쏘는 데까지 "성공" 했지만,
  // Gemini 는 그 파일을 받은 적이 없었다. Angular 가 컴포넌트를 파괴하면서 리스너까지
  // 걷어갔기 때문이다 — 노드를 되붙여도 파괴된 바인딩은 돌아오지 않는다.
  //
  // 진짜 문제는 그 다음이다: revive 가 성공을 반환해 버려서 합성 drop 폴백까지
  // 내려가지 못했다. 사용자가 "첨부는 됐다"고 했던 예전 빌드에는 그 drop 이 살아
  // 있었으니, revive 도입이 실제로 되던 경로를 가로챈 회귀였을 수 있다.
  //
  // 여기서는 관찰이 가능한 환경(MutationObserver 존재)을 만들어, revive 가 기계적으로
  // 성공해도 컴포저에 아무 변화가 없으면 합성 drop 까지 내려가는지 확인한다.
  await new Promise(r => setTimeout(r, 5000)); // (15)의 promptApproved(3초) 해제 대기

  // 사이트가 첨부를 받으면 컴포저에 무언가를 그린다 — 그 반응을 흉내내는 최소 stub.
  class MutationObserverStub {
    constructor(cb) { this.cb = cb; MutationObserverStub.instances.push(this); }
    observe() {}
    disconnect() { this.disconnected = true; }
    static emitAdded(node) {
      for (const o of MutationObserverStub.instances) {
        if (o.disconnected) continue;
        o.cb([{ target: {}, addedNodes: [node], removedNodes: [] }]);
      }
    }
  }
  MutationObserverStub.instances = [];
  sandbox.MutationObserver = MutationObserverStub;

  const send16 = new SendButtonStub();
  send16.disabled = false;
  domBySelector.set('[data-testid="send-button"]', send16);
  domBySelector.set('#prompt-textarea', promptEditorStub);
  domBySelector.delete('input[type="file"]');      // 살아 있는 input 은 없다
  domBySelectorAll.delete('[role="progressbar"]');

  // 컴포저(부모)는 살아 있고 그 안의 input 만 떼어진 상황 = (13)과 같은 조건.
  const parent16 = new DropTargetStub();
  parent16.appended = [];
  parent16.appendChild = function (node) { this.appended.push(node); node.isConnected = true; };
  const input16 = new HTMLInputElementStub(
    new FileStub(['pdf bytes'], 'evidence.pdf', { type: 'application/pdf' }), 'orphan16',
  );
  input16.parentElement = parent16;

  // 사이트는 "합성 drop 을 받았을 때만" 첨부 칩을 그린다 — 되돌린 input 의 change 는
  // 죽은 바인딩이라 무시한다(= Gemini 에서 실제로 벌어진 일).
  const editorDispatch = promptEditorStub.dispatchEvent.bind(promptEditorStub);
  promptEditorStub.dispatchEvent = (event) => {
    const r = editorDispatch(event);
    if (event?.type === 'drop' && event?.dataTransfer?.files?.length) {
      // 첨부 칩에는 그 파일의 이름이 실린다 — 증거 판정은 이제 그걸 본다(아래 (23)).
      setTimeout(
        () => MutationObserverStub.emitAdded({ nodeType: 1, tagName: 'DIV', textContent: 'evidence.pdf' }),
        100,
      );
    }
    return r;
  };

  dispatchDocumentEvent('change', {
    target: input16,
    composedPath: () => [input16, documentStub],
    preventDefault() {}, stopImmediatePropagation() {},
  });
  await flush();

  input16.isConnected = false;                     // 사이트가 떼어냈다
  const linesBefore16 = consoleLines.length;
  promptEditorStub.dispatched.length = 0;
  documentStub.activeElement = promptEditorStub;
  promptEditorStub.value = '이 문서를 요약해줘';
  nextDecision = {
    action: 'send',
    maskedText: '이 문서를 요약해줘',
    file: {
      action: 'upload', maskedBase64: btoa('masked'),
      mimeType: 'application/pdf', fileName: 'evidence.pdf',
    },
  };
  dispatchDocumentEvent('keydown', {
    key: 'Enter', shiftKey: false, ctrlKey: false, altKey: false, metaKey: false,
    preventDefault() {}, stopImmediatePropagation() {},
  });
  // 전략 체인: 살아있는input(즉시 실패) → 되돌리기(700ms 증거 대기) → 합성drop(≈160ms)
  await new Promise(r => setTimeout(r, 4000));

  if (!parent16.appended.includes(input16)) {
    throw new Error('되돌리기 전략을 아예 시도하지 않았다 — 체인 순서가 깨졌다');
  }
  const drops16 = promptEditorStub.dispatched.filter(e => e.type === 'drop' && e.dataTransfer?.files?.length);
  if (!drops16.length) {
    throw new Error(
      '되돌리기가 기계적으로만 성공했는데 합성 drop 까지 내려가지 않았다 — '
      + '사이트가 파일을 받은 적 없어도 성공으로 단정하는 그 회귀 그대로다',
    );
  }
  if (drops16[0].dataTransfer.files[0]?.name !== 'evidence.pdf') {
    throw new Error(`합성 drop 에 실린 파일이 다르다: ${drops16[0].dataTransfer.files[0]?.name}`);
  }
  const chainLines = consoleLines.slice(linesBefore16);
  if (!chainLines.some(l => l.includes('먹힌 방법: 합성drop'))) {
    throw new Error(`어느 전략이 먹혔는지 알려주는 로그가 없다: ${chainLines.filter(l => l.includes('첨부 주입')).join(' | ')}`);
  }
  if (!chainLines.some(l => l.includes('input되돌리기=증거없음'))) {
    throw new Error('되돌리기가 증거 없이 성공으로 기록됐다 — 판정이 우리 쪽 상태만 보고 있다');
  }
  // 이 테스트에서만 쓰는 "사이트가 drop 에 반응한다" 흉내를 되돌린다.
  // 안 되돌리면 다음 테스트의 합성 drop 도 증거를 얻어버려 전제가 깨진다.
  promptEditorStub.dispatchEvent = editorDispatch;

  // (17) 문서를 끝내 못 붙였으면 **전송하지 않는다**.
  //
  // 예전에는 주입에 실패해도 프롬프트를 그대로 보내고 콘솔에만 남겼다
  // ("문서를 페이지에 다시 넣지 못했습니다 — 프롬프트만 전송됩니다"). 사용자는 문서가
  // 갔다고 믿은 채 대화를 이어간다. 보안 제품에서 가장 나쁜 실패 모드다.
  // 실사용자 Gemini 에서 실제로 이 경로가 나왔다(네 전략 모두 증거 없음).
  await new Promise(r => setTimeout(r, 5000)); // (16)의 promptApproved 해제 대기

  const send17 = new SendButtonStub();
  send17.disabled = false;
  domBySelector.set('[data-testid="send-button"]', send17);
  domBySelector.delete('input[type="file"]');

  // 어떤 전략도 증거를 못 얻는 상황: 살아있는 input 없음, 되돌릴 부모 없음,
  // 합성 drop 을 쏴도 사이트가 아무 반응 없음(MutationObserverStub 이 아무것도 안 쏨).
  const input17 = new HTMLInputElementStub(
    new FileStub(['pdf bytes'], 'blocked.pdf', { type: 'application/pdf' }), 'gone17',
  );
  dispatchDocumentEvent('change', {
    target: input17,
    composedPath: () => [input17, documentStub],
    preventDefault() {}, stopImmediatePropagation() {},
  });
  await flush();

  input17.isConnected = false;
  appendedToRoot.length = 0;
  const lines17 = consoleLines.length;
  documentStub.activeElement = promptEditorStub;
  promptEditorStub.value = '이 문서를 요약해줘';
  nextDecision = {
    action: 'send',
    maskedText: '이 문서를 요약해줘',
    file: {
      action: 'upload', maskedBase64: btoa('masked'),
      mimeType: 'application/pdf', fileName: 'blocked.pdf',
    },
  };
  dispatchDocumentEvent('keydown', {
    key: 'Enter', shiftKey: false, ctrlKey: false, altKey: false, metaKey: false,
    preventDefault() {}, stopImmediatePropagation() {},
  });
  await new Promise(r => setTimeout(r, 3500)); // 전략 체인(증거 대기 포함) 통과 시간

  if (send17.clicks !== 0) {
    throw new Error(`문서를 못 붙였는데 프롬프트를 전송했다 (clicks=${send17.clicks}) — 사용자는 문서가 갔다고 믿는다`);
  }
  const badge17 = appendedToRoot.find(el => el?.id === '__ups_pending_badge');
  if (!badge17) {
    throw new Error('전송을 멈췄는데 화면에 아무 안내도 띄우지 않았다 — 콘솔은 아무도 안 본다');
  }
  const badgeText = (badge17.children || []).map(c => String(c?.textContent || '')).join(' ');
  if (!/다시 첨부/.test(badgeText)) {
    throw new Error(`뱃지 문구가 "다시 첨부" 안내를 담고 있지 않다: ${badgeText}`);
  }
  if (!consoleLines.slice(lines17).some(l => l.includes('전송을 중단했습니다'))) {
    throw new Error('전송 중단 사실이 로그에 남지 않았다');
  }

  delete sandbox.MutationObserver; // 증거 판정 뒷정리 — 아래 (18)은 텍스트 경로만 본다

  // (18) 프레임워크형 입력창에서 마스킹 텍스트가 여러 벌 쌓이면 안 된다.
  //
  // 배경(실사용자 perplexity): "프롬프트가 4번" → 한 번 고쳐 3번 → 여전히 쌓임.
  // setEditorText 의 contenteditable 경로에는 삽입 전략이 정확히 3개 있는데, 각 전략은
  // done() 이 true 여야 멈춘다. 그 판정이 깨지면 3개가 전부 실행되어 그대로 쌓인다.
  // 헤드리스 Chrome 실측으로 확인한 두 원인:
  //   ① 프레임워크가 문단을 블록으로 렌더하면 Chrome innerText 가 블록 사이에 개행을
  //      "두 개" 넣는다 → 글자는 같은데 === 가 false → 성공을 실패로 오판
  //   ② 지우기(execCommand delete)가 무시되어 원문이 그대로 남는다
  await new Promise(r => setTimeout(r, 5000)); // (17)에서 promptApproved 는 즉시 내려가지만 여유

  // (18-a) 지우기가 먹는 에디터: 개행 때문에 판정만 깨지던 경우 → 정확히 1벌, 전송됨.
  const ed18a = new ContentEditableStub('원래 프롬프트\n둘째 줄', { acceptDelete: true });
  domBySelector.set('#prompt-textarea', ed18a);
  const send18a = new SendButtonStub();
  send18a.disabled = false;
  domBySelector.set('[data-testid="send-button"]', send18a);
  documentStub.activeElement = ed18a;
  nextDecision = { action: 'masked', maskedText: '마스킹된 프롬프트\n둘째 줄' };
  dispatchDocumentEvent('keydown', {
    key: 'Enter', shiftKey: false, ctrlKey: false, altKey: false, metaKey: false,
    preventDefault() {}, stopImmediatePropagation() {},
  });
  await new Promise(r => setTimeout(r, 1200));

  if (ed18a.inserts !== 1) {
    throw new Error(`삽입이 ${ed18a.inserts}회 일어났다 — 판정이 깨져 전략이 연달아 덧씌운 그 회귀다`);
  }
  const bodyA = ed18a.lines.join(' ');
  if ((bodyA.split('마스킹된 프롬프트').length - 1) !== 1) {
    throw new Error(`마스킹 텍스트가 ${bodyA.split('마스킹된 프롬프트').length - 1}벌 들어갔다: ${bodyA}`);
  }
  if (bodyA.includes('원래 프롬프트')) {
    throw new Error(`원문이 지워지지 않고 남았다: ${bodyA}`);
  }
  if (send18a.clicks !== 1) {
    throw new Error(`정상적으로 넣었는데 전송되지 않았다 (clicks=${send18a.clicks})`);
  }

  // (18-b) 우리가 어떤 방법으로도 못 바꾸는 에디터(지우기도 선택 영역 대체도 안 먹는다):
  //        삽입은 1회로 막고, 원문이 남았으므로 전송 금지 — fail-closed 가 살아 있는지.
  await new Promise(r => setTimeout(r, 4000)); // promptApproved(3초) 해제 대기

  const ed18b = new ContentEditableStub('주민번호 900101-1234567 알려줘', {
    acceptDelete: false, acceptSelectionReplace: false,
  });
  domBySelector.set('#prompt-textarea', ed18b);
  const send18b = new SendButtonStub();
  send18b.disabled = false;
  domBySelector.set('[data-testid="send-button"]', send18b);
  documentStub.activeElement = ed18b;
  nextDecision = { action: 'masked', maskedText: '주민번호 [RRN_1] 알려줘' };
  dispatchDocumentEvent('keydown', {
    key: 'Enter', shiftKey: false, ctrlKey: false, altKey: false, metaKey: false,
    preventDefault() {}, stopImmediatePropagation() {},
  });
  await new Promise(r => setTimeout(r, 1200));

  if (ed18b.inserts > 1) {
    throw new Error(`지우지 못한 입력창에 ${ed18b.inserts}회 삽입했다 — 쌓임을 막지 못했다`);
  }
  if (send18b.clicks !== 0) {
    throw new Error('마스킹본을 못 넣었는데 전송했다 — 입력창에 남은 원문이 그대로 나간다');
  }

  // (19) 마스킹이 프롬프트를 하나도 바꾸지 않았으면 입력창에 손대지 않는다.
  //
  // 배경(실사용자 perplexity, v0.2.14): "입력하는 곳에는 2번 반복되게 써있는데 실제로
  // 가진 않았어". 로그는 "삽입 1회, 현재 2벌 감지" 였다 — 한 번 넣었는데 target 이 2벌
  // 이라는 건 넣기 전에 이미 한 벌 있었다는 뜻이다. PII 가 첨부 문서 쪽에만 있으면
  // maskedText 는 원문과 같은데, 예전 코드는 그 확인 없이 지우고-넣기를 시도했고
  // 지우기가 안 먹는 에디터에서는 그게 곧 2벌이 됐다. 그리고 2벌을 감지해 전송을
  // 막으니 사용자 입장에선 "아예 안 감" 이 된다.
  //
  // 지우기가 안 먹는(acceptDelete:false) 에디터를 쓰는 게 핵심이다 — 지우기가 먹으면
  // 넣어도 1벌이라 이 회귀가 드러나지 않는다.
  await new Promise(r => setTimeout(r, 4000)); // promptApproved(3초) 해제 대기

  const SAME = '이 문서 요약해줘';
  const ed19 = new ContentEditableStub(SAME, { acceptDelete: false });
  domBySelector.set('#prompt-textarea', ed19);
  const send19 = new SendButtonStub();
  send19.disabled = false;
  domBySelector.set('[data-testid="send-button"]', send19);
  documentStub.activeElement = ed19;
  nextDecision = { action: 'masked', maskedText: SAME }; // 마스킹이 바꾼 게 없다
  dispatchDocumentEvent('keydown', {
    key: 'Enter', shiftKey: false, ctrlKey: false, altKey: false, metaKey: false,
    preventDefault() {}, stopImmediatePropagation() {},
  });
  await new Promise(r => setTimeout(r, 1200));

  if (ed19.inserts !== 0) {
    throw new Error(`이미 목표 상태인 입력창에 ${ed19.inserts}회 삽입했다 — 그게 2벌이 되는 경로다`);
  }
  const body19 = ed19.lines.join(' ');
  if ((body19.split(SAME).length - 1) !== 1) {
    throw new Error(`프롬프트가 ${body19.split(SAME).length - 1}벌 있다: ${body19}`);
  }
  if (send19.clicks !== 1) {
    throw new Error(`넣을 게 없어 성공인데 전송되지 않았다 (clicks=${send19.clicks})`);
  }

  // (20) 지우기가 안 먹는 에디터라도 "전체를 선택한 채로" 넣으면 대체된다.
  //
  // 배경(실사용자 perplexity, v0.2.15):
  //   "execCommand: 입력창을 비우지 못한 채 넣었고 결과도 불일치 → 중단.
  //    삽입 1회, 현재 1벌 감지"
  // 마스킹본은 들어갔는데 원문이 그대로 남아 둘이 공존했다. 원인은 우리 clear() 의
  // 마지막 단계 — editor.textContent='' 이 자식 노드를 통째로 갈아치우면서 방금 잡아둔
  // **선택 영역을 부쉈다**. 선택이 없으면 insertText 는 "대체"가 아니라 캐럿 자리에
  // "덧붙이기"가 된다.
  //
  // 이 테스트가 성립하려면 스텁이 선택 영역을 모델링해야 한다(위 ContentEditableStub).
  // 지우기는 안 먹지만(acceptDelete:false) 선택 영역 대체는 먹는 — 실제 Lexical 이
  // 그렇다 — 에디터를 쓴다.
  await new Promise(r => setTimeout(r, 4000)); // promptApproved(3초) 해제 대기

  const ed20 = new ContentEditableStub('내 번호 010-1234-5678 로 연락해줘', { acceptDelete: false });
  domBySelector.set('#prompt-textarea', ed20);
  const send20 = new SendButtonStub();
  send20.disabled = false;
  domBySelector.set('[data-testid="send-button"]', send20);
  documentStub.activeElement = ed20;
  selectedNode = null;
  nextDecision = { action: 'masked', maskedText: '내 번호 [PHONE_1] 로 연락해줘' };
  dispatchDocumentEvent('keydown', {
    key: 'Enter', shiftKey: false, ctrlKey: false, altKey: false, metaKey: false,
    preventDefault() {}, stopImmediatePropagation() {},
  });
  await new Promise(r => setTimeout(r, 1200));

  const body20 = ed20.lines.join(' ');
  if (body20.includes('010-1234-5678')) {
    throw new Error(`원문이 지워지지 않고 마스킹본과 공존한다 — 그게 perplexity 회귀다: ${body20}`);
  }
  if ((body20.split('[PHONE_1]').length - 1) !== 1) {
    throw new Error(`마스킹본이 ${body20.split('[PHONE_1]').length - 1}벌 있다: ${body20}`);
  }
  if (send20.clicks !== 1) {
    throw new Error(`정상적으로 넣었는데 전송되지 않았다 (clicks=${send20.clicks})`);
  }

  // (21) 넣기 전에 지우지 않는다 — 부분 삭제가 조각을 남길 기회를 아예 주지 않는다.
  //
  // 배경(실사용자, v0.2.16): 이름·전화번호를 넣고 보냈더니 입력창에
  //   "마스킹][이름 마스킹] [전화번호 마스킹]"
  // 이 들어갔다. 앞에 붙은 "마스킹][" 는 마스킹 토큰([이름 마스킹] 형식)의 조각이다 —
  // 온전한 삽입이 아니라 부분 치환이 일어났다는 뜻이다.
  //
  // ※ 이 테스트는 그 증상 자체를 재현하지 못한다. 실제 기전(어느 사이트에서, 삭제가
  //   왜 부분적으로만 먹었는지)은 확인되지 않았다. 대신 그 기전이 **작동할 기회 자체가
  //   없어졌는지**를 본다: 삽입(insertText/paste)은 원래 선택 영역을 대체하므로 지우기는
  //   필요 없고, 지우기를 먼저 하는 건 이득 없이 "부분 삭제로 조각이 남을" 위험만 만든다.
  //   그래서 첫 삽입 전에는 지우기를 단 한 번도 부르지 않아야 한다.
  //   (지우기는 A 가 실패한 뒤 B 단계에서만 쓰이고, 거기서는 "비었음을 확인한 뒤에만"
  //    넣으므로 조각이 남을 수 없다.)
  await new Promise(r => setTimeout(r, 4000)); // promptApproved(3초) 해제 대기

  const ORIG21 = '홍길동이고 번호는 010-1234-5678 이야';
  const ed21 = new ContentEditableStub(ORIG21, { acceptDelete: true, partialDelete: true });
  domBySelector.set('#prompt-textarea', ed21);
  const send21 = new SendButtonStub();
  send21.disabled = false;
  domBySelector.set('[data-testid="send-button"]', send21);
  documentStub.activeElement = ed21;
  selectedNode = null;
  nextDecision = { action: 'masked', maskedText: '[이름 마스킹] 이고 번호는 [전화번호 마스킹] 이야' };
  dispatchDocumentEvent('keydown', {
    key: 'Enter', shiftKey: false, ctrlKey: false, altKey: false, metaKey: false,
    preventDefault() {}, stopImmediatePropagation() {},
  });
  await new Promise(r => setTimeout(r, 1200));

  if (ed21.deletesBeforeFirstInsert !== 0) {
    throw new Error(
      `첫 삽입 전에 지우기를 ${ed21.deletesBeforeFirstInsert}회 불렀다 — `
      + '부분 삭제가 조각을 남길 기회를 그대로 열어둔 것이다',
    );
  }
  const body21 = ed21.lines.join(' ');
  // 성공했다면 마스킹본 1벌만, 실패했다면 원문 그대로. 그 사이의 "조각 + 본문" 은 없어야 한다.
  const clean21 = body21 === '[이름 마스킹] 이고 번호는 [전화번호 마스킹] 이야' || body21 === ORIG21;
  if (!clean21) {
    throw new Error(`입력창에 조각이 남았다 — 그게 "마스킹][" 회귀다: ${body21}`);
  }
  if (body21 === ORIG21 && send21.clicks !== 0) {
    throw new Error('마스킹본을 못 넣었는데 전송했다');
  }

  // (22) 되돌려 붙일 자리가 없으면 body 가 아니라 컴포저 안에 붙이고,
  //      증거를 못 얻었으면 그 노드를 **다시 떼어낸다**.
  //
  // 배경(실사용자 Gemini, 0.2.18):
  //   [SecureDoc] 파일 재주입: 사이트가 떼어낸 input 을 되돌려 놓았습니다
  //               (붙인 곳: body ← 컴포저 바깥이라 사이트가 못 들을 수 있습니다)
  //   → input되돌리기=증거없음
  // 사이트 리스너는 대개 컴포저에 위임돼 있어서 body 에 붙이면 change 를 아무도 안
  // 듣는다. 코드가 그 사실을 로그로 경고하면서도 정작 body 에 붙이고 있었다.
  //
  // 그리고 실패한 되돌리기를 DOM 에 남겨두면 안 된다. 뒤따르는 "사이트 첨부 UI" 전략은
  // **새로 생긴 input** 으로 성공을 판정하는데, 우리가 붙여둔 노드가 기준 스냅샷에
  // 들어가면 사이트가 같은 노드를 재사용하는 구조일 때 영영 못 알아본다.
  await new Promise(r => setTimeout(r, 9000)); // 앞 테스트의 promptApproved 해제 대기

  sandbox.MutationObserver = MutationObserverStub; // 관찰 가능 = 증거 판정이 실제로 돈다
  MutationObserverStub.instances.length = 0;       // 아무 변화도 안 쏜다 → 증거 없음

  // 컴포저 루트로 쓰일 입력창. 하니스에는 부모 사슬이 없어 findComposerRoot 가 이걸
  // 그대로 돌려준다.
  const composer22 = new HTMLTextAreaElementStub('이 문서 요약해줘');
  composer22.appended = [];
  composer22.appendChild = function (node) {
    this.appended.push(node); node.isConnected = true; node.parentElement = this;
  };
  domBySelector.set('#prompt-textarea', composer22);
  const send22 = new SendButtonStub();
  send22.disabled = false;
  domBySelector.set('[data-testid="send-button"]', send22);
  domBySelector.delete('input[type="file"]');   // 살아 있는 input 은 없다
  documentStub.activeElement = composer22;

  const input22 = new HTMLInputElementStub(
    new FileStub(['pdf bytes'], 'revive22.pdf', { type: 'application/pdf' }), 'orphan22',
  );
  input22.parentElement = null;                 // 원래 부모까지 사라졌다 = body 로 떨어지던 조건

  dispatchDocumentEvent('change', {
    target: input22,
    composedPath: () => [input22, documentStub],
    preventDefault() {}, stopImmediatePropagation() {},
  });
  await flush();
  input22.isConnected = false;                  // 사이트가 떼어냈다

  nextDecision = {
    action: 'send',
    maskedText: '이 문서 요약해줘',
    file: {
      action: 'upload', maskedBase64: btoa('masked'),
      mimeType: 'application/pdf', fileName: 'revive22.pdf',
    },
  };
  dispatchDocumentEvent('keydown', {
    key: 'Enter', shiftKey: false, ctrlKey: false, altKey: false, metaKey: false,
    preventDefault() {}, stopImmediatePropagation() {},
  });
  await new Promise(r => setTimeout(r, 6000)); // 증거 대기(700ms) × 전략 + 첨부 UI 예산

  if (!composer22.appended.includes(input22) && input22.parentElement !== composer22) {
    throw new Error('되돌린 input 을 컴포저에 붙이지 않았다 — body 로 떨어지면 사이트가 못 듣는다');
  }
  if (input22.isConnected) {
    throw new Error('증거를 못 얻었는데 되돌린 input 을 DOM 에 남겨뒀다 — 다음 전략의 판정을 망친다');
  }

  // (23) 첨부와 무관한 재렌더를 "사이트가 받았다" 로 오판하지 않는다.
  //
  // 배경(실사용자 Gemini, 0.2.19):
  //   [SecureDoc] 첨부 주입 성공 — 먹힌 방법: input되돌리기
  //               (증거: 컴포저에 div.model-picker-container 추가됨)
  // model-picker-container 는 모델 선택 드롭다운이지 첨부 칩이 아니다. 되돌린 input 을
  // 컴포저 안에 붙이자 Angular 가 그 안을 다시 그렸고, 예전 기준("컴포저 하위에 요소가
  // 추가됨")이 그 재렌더를 증거로 셌다. 같은 로그의 진단은 정반대를 말한다 — 대기 창
  // DOM 변화 0건, 요청 15건 모두 batchexecute(최대 1654B), 전송 후 49건에도 업로드 없음.
  // 파일은 가지 않았다.
  //
  // 이 오탐이 특히 나쁜 이유: 성공을 선언해 뒤 전략으로 내려가지 못하게 막고, fail-closed
  // 까지 우회해 **문서 없이 프롬프트만 전송**시킨다. 우리가 막으려던 바로 그 실패다.
  await new Promise(r => setTimeout(r, 9000)); // 앞 테스트의 promptApproved 해제 대기

  sandbox.MutationObserver = MutationObserverStub;
  MutationObserverStub.instances.length = 0;

  const composer23 = new HTMLTextAreaElementStub('이 문서 요약해줘');
  composer23.appended = [];
  composer23.appendChild = function (node) {
    this.appended.push(node); node.isConnected = true; node.parentElement = this;
  };
  domBySelector.set('#prompt-textarea', composer23);
  const send23 = new SendButtonStub();
  send23.disabled = false;
  domBySelector.set('[data-testid="send-button"]', send23);
  domBySelector.delete('input[type="file"]');
  documentStub.activeElement = composer23;

  const input23 = new HTMLInputElementStub(
    new FileStub(['pdf bytes'], 'secret-report.pdf', { type: 'application/pdf' }), 'orphan23',
  );
  input23.parentElement = null;

  // 사이트는 파일을 받지 않았지만, 우리가 노드를 붙인 탓에 무관한 컨테이너를 다시 그린다.
  const origDispatch23 = HTMLInputElementStub.prototype.dispatchEvent;
  HTMLInputElementStub.prototype.dispatchEvent = function (ev) {
    const r = origDispatch23.call(this, ev);
    setTimeout(
      () => MutationObserverStub.emitAdded({
        nodeType: 1, tagName: 'DIV', textContent: '모델 선택 2.5 Pro 2.5 Flash',
      }),
      50,
    );
    return r;
  };

  dispatchDocumentEvent('change', {
    target: input23,
    composedPath: () => [input23, documentStub],
    preventDefault() {}, stopImmediatePropagation() {},
  });
  await flush();
  input23.isConnected = false;

  nextDecision = {
    action: 'send',
    maskedText: '이 문서 요약해줘',
    file: {
      action: 'upload', maskedBase64: btoa('masked'),
      mimeType: 'application/pdf', fileName: 'secret-report.pdf',
    },
  };
  dispatchDocumentEvent('keydown', {
    key: 'Enter', shiftKey: false, ctrlKey: false, altKey: false, metaKey: false,
    preventDefault() {}, stopImmediatePropagation() {},
  });
  await new Promise(r => setTimeout(r, 8000));
  HTMLInputElementStub.prototype.dispatchEvent = origDispatch23;

  if (send23.clicks !== 0) {
    throw new Error(
      '첨부와 무관한 재렌더를 증거로 삼아 문서 없이 전송했다 — fail-closed 가 우회됐다',
    );
  }
  const claimedSuccess23 = consoleLines.some(
    (l) => l.includes('첨부 주입 성공') && l.includes('모델 선택'),
  );
  if (claimedSuccess23) {
    throw new Error('무관한 컨테이너 추가를 첨부 증거로 판정했다');
  }

  console.log('content regression ok');
  process.exit(0);
})();
