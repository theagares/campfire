/**
 * interceptor.js  ─  world: "MAIN"
 *
 * 목표: 원본 파일이 ChatGPT 서버에 "업로드 한다"는 정보조차 가지 않도록,
 *       Azure PUT(Layer 2)에서 완전히 차단하고 마스킹본으로 처음부터 재등록.
 *
 * 흐름:
 *   원본 PDF/DOCX 업로드 시도
 *     → Layer 2 (XHR Azure PUT) 가로채기
 *     → 모달 표시 (처리 완료 대기)
 *     → 사용자 승인 시: Azure PUT 취소 + 마스킹본을 파일 인풋에 주입
 *       → ChatGPT가 마스킹본을 새로 등록 + 업로드
 *       → Layer 2에서 "승인된 파일" 확인 후 통과
 *     → 사용자 취소 시: Azure PUT 취소 (ChatGPT 업로드 실패 처리)
 */

(function () {
  'use strict';
  if (window.__securedocLoaded) return;
  window.__securedocLoaded = true;

  // ─── 원본 선저장 (훅 전, 재귀 방지) ──────────────────────────────────────
  const _origBlobArrayBuffer       = Blob.prototype.arrayBuffer;
  const _origFRReadAsAB            = FileReader.prototype.readAsArrayBuffer;
  const _origFRReadAsDataURL       = FileReader.prototype.readAsDataURL;
  const _origFRReadAsBinStr        = FileReader.prototype.readAsBinaryString;
  const _origFetch                 = window.fetch.bind(window);
  const _origXHROpen               = XMLHttpRequest.prototype.open;
  const _origXHRSend               = XMLHttpRequest.prototype.send;
  const _origShowOpenFilePicker    = window.showOpenFilePicker?.bind(window);
  const _origShowSaveFilePicker    = window.showSaveFilePicker?.bind(window);
  const CONTENT_OWNS_LAYER1_UPLOADS = true;
  const CONTENT_OWNS_PROMPTS = true;
  const DEBUG = false;

  function debugLog(...args) {
    if (DEBUG) console.debug(...args);
  }

  let _protectionEnabled = true;
  let _bridgeToken = '';

  window.addEventListener('message', (event) => {
    if (event.source !== window) return;
    if (!event.data?.__upsecurity_config || event.data.direction !== 'isolated-to-main') return;
    if (event.data.type === 'SECUREDOC_BRIDGE_TOKEN') {
      _bridgeToken = String(event.data.token || '');
      return;
    }
    if (event.data.type === 'UPS_CONTENT_APPROVED_FILE') {
      _rememberContentApproved(event.data.meta);
      return;
    }
    if (event.data.type !== 'UPS_PROTECTION_STATE') return;
    _protectionEnabled = Boolean(event.data.enabled);
    debugLog(`[SecureDoc] 보호 상태: ${_protectionEnabled ? 'ON' : 'OFF'}`);
  });

  function isProtectionEnabled() {
    return _protectionEnabled;
  }

  // ─── content.js(isolated world)가 이미 검토를 마치고 주입한 파일 ─────────────
  //
  // content.js 가 만든 마스킹본 File 은 isolated world 소속이라 아래 _approvedFiles
  // WeakSet 으로는 원리적으로 인식할 수 없다 — 두 world 는 DOM 은 공유하지만 JS 렘이
  // 별개라 같은 파일이라도 서로 다른 JS 객체로 보이기 때문. 그 결과 사이트가 그
  // 마스킹본을 업로드할 때 Layer 2/3 가 "처음 보는 원본"으로 오인해 검토 패널을 한 번
  // 더 띄웠다(실사용자 재현: 전송 직후 이미 마스킹된 내용으로 패널 재등장). 객체
  // 동일성을 쓸 수 없으니 name+size+type 메타로 기억해뒀다가 통과시킨다.
  //
  // 한 번 쓰고 버리지 않는 이유: 사이트가 등록(fetch POST /files)과 실제 업로드
  // (XHR PUT)로 같은 파일을 두 번 이상 만지기 때문 — TTL 로만 만료시킨다.
  const _CONTENT_APPROVED_TTL_MS = 10 * 60 * 1000;
  const _CONTENT_APPROVED_MAX = 16;
  // [{ name, size, type, expiresAt }] — 파일명에 어떤 문자가 들어와도 안전하도록
  // 문자열 키 하나로 합치지 않고 필드를 그대로 들고 비교한다.
  const _contentApproved = [];

  function _pruneContentApproved() {
    const now = Date.now();
    for (let i = _contentApproved.length - 1; i >= 0; i--) {
      if (_contentApproved[i].expiresAt <= now) _contentApproved.splice(i, 1);
    }
  }

  function _rememberContentApproved(meta) {
    if (!meta?.name) return;
    _pruneContentApproved();
    while (_contentApproved.length >= _CONTENT_APPROVED_MAX) _contentApproved.shift();
    _contentApproved.push({
      name: String(meta.name),
      size: Number(meta.size),
      type: meta.type || 'application/octet-stream',
      expiresAt: Date.now() + _CONTENT_APPROVED_TTL_MS,
    });
    debugLog('[SecureDoc] content 검토 완료 파일 등록:', meta.name);
  }

  /** 이름만으로 대조 — 사이트가 등록 요청 JSON 에 size/mime 를 자기 방식대로 채워
   *  보내는 경우가 있어, 등록 단계에서는 이름 일치만으로 판단한다. */
  function _isContentApprovedName(name) {
    if (!name) return false;
    _pruneContentApproved();
    return _contentApproved.some(e => e.name === name);
  }

  /** Blob/File 대조 — 사이트가 File 을 이름 없는 Blob 으로 다시 감싸 업로드하는
   *  경우가 있어(예: new Blob([file])), 이름이 없으면 size+type 만으로 본다. */
  function _isContentApprovedBlob(blob) {
    if (!blob) return false;
    _pruneContentApproved();
    const type = blob.type || 'application/octet-stream';
    if (blob.name) {
      return _contentApproved.some(e => e.name === blob.name && e.size === blob.size && e.type === type);
    }
    return _contentApproved.some(e => e.size === blob.size && e.type === type);
  }

  debugLog('[SecureDoc] ✅ Interceptor 로드됨 (MAIN world)');

  // ─── Shadow DOM 관찰 (closed 포함) ────────────────────────────────────────
  // attachShadow를 훅해 shadow root 내 file input을 감지
  const _origAttachShadow = Element.prototype.attachShadow;
  Element.prototype.attachShadow = function (init) {
    const shadow = _origAttachShadow.call(this, init);
    _observeShadowRoot(shadow);
    return shadow;
  };

  function _observeShadowRoot(root) {
    new MutationObserver((muts) => {
      for (const m of muts)
        for (const node of m.addedNodes)
          if (node.nodeType === 1) _hookInputsIn(node);
    }).observe(root, { childList: true, subtree: true });
    _hookInputsIn(root);
  }

  function _hookInputsIn(node) {
    const inputs = [];
    if (node.matches?.('input[type="file"]')) inputs.push(node);
    node.querySelectorAll?.('input[type="file"]').forEach(n => inputs.push(n));
    for (const inp of inputs) _hookSingleInput(inp);
  }

  function _hookSingleInput(inp) {
    if (CONTENT_OWNS_LAYER1_UPLOADS) return;
    if (inp._sdHooked) return;
    inp._sdHooked = true;
    inp.addEventListener('change', async function (event) {
      _lastFileInput = inp;
      if (inp._sdDone) { delete inp._sdDone; return; }
      const file = inp.files?.[0];
      if (!isSupportedFile(file) || _inProcess.has(file) || _approvedFiles.has(file)) return;
      event.stopImmediatePropagation();
      const id = nextRequestId('sd');
      _pending.set(id, { type: 'input', input: inp, originalFile: file });
      _inProcess.add(file);
      try {
        const b64 = await fileToBase64(file);
        window.postMessage({ __securedoc: true, direction: 'main-to-isolated', bridgeToken: _bridgeToken, type: 'SECUREDOC_FILE_SELECTED',
          payload: { inputId: id, base64Data: b64, mimeType: file.type || 'application/octet-stream',
                     fileName: file.name, fileSize: file.size } }, '*');
      } catch (e) { _pending.delete(id); _inProcess.delete(file); }
    }, true);
  }

  // ══════════════════════════════════════════════════════════════════════════
  // file input 완전 선점 훅
  //
  // 핵심 원리: detached element에서는 이벤트 리스너가 "등록 순서"대로 실행됨.
  // ChatGPT가 change 리스너를 type='file' 설정 전에 등록할 수 있으므로,
  // document.createElement('input') 시점에 바로 우리 리스너를 첫 번째로 부착.
  // ══════════════════════════════════════════════════════════════════════════

  // (A) addEventListener 훅 — change 리스너 등록 직전에 우리 리스너를 선점
  //
  // ChatGPT가 change 리스너를 type='file' 전에 등록하거나
  // createElement 이전에 리스너를 붙이는 경우에도 대응.
  // ChatGPT가 addEventListener('change', ...) 호출하는 순간,
  // 우리 리스너가 아직 없으면 먼저 삽입한다.
  const _origAddEL = EventTarget.prototype.addEventListener;

  function _makeSDFirstListener(el) {
    return async function _sdFirst(event) {
      if (CONTENT_OWNS_LAYER1_UPLOADS) return;
      if (el.type !== 'file') return;
      if (el._sdDone) { delete el._sdDone; return; }
      const file = el.files?.[0];
      if (!file || !isSupportedFile(file) || _inProcess.has(file) || _approvedFiles.has(file)) return;

      debugLog('[SecureDoc] 📁 [1-AEL] change 선점 성공:', file.name);
      event.stopImmediatePropagation();
      _lastFileInput = el;

      const id = nextRequestId('sd');
      _pending.set(id, { type: 'input', input: el, originalFile: file });
      _inProcess.add(file);
      try {
        const b64 = await fileToBase64(file);
        window.postMessage({
          __securedoc: true, direction: 'main-to-isolated', bridgeToken: _bridgeToken, type: 'SECUREDOC_FILE_SELECTED',
          payload: { inputId: id, base64Data: b64,
                     mimeType: file.type || 'application/octet-stream',
                     fileName: file.name, fileSize: file.size },
        }, '*');
      } catch (e) { _pending.delete(id); _inProcess.delete(file); }
    };
  }

  EventTarget.prototype.addEventListener = function (type, listener, options) {
    // input의 change 리스너 등록 감지 → 우리 리스너를 먼저 삽입
    if (type === 'change' && this instanceof HTMLInputElement && !this._sdAELHooked) {
      this._sdAELHooked = true;
      _origAddEL.call(this, 'change', _makeSDFirstListener(this), true);
    }
    return _origAddEL.call(this, type, listener, options);
  };

  // (A-2) createElement 훅 — 보조 (addEventListener 이전에 input이 생성되는 경우)
  const _origCreateElement = Document.prototype.createElement;
  Document.prototype.createElement = function (tagName, ...args) {
    const el = _origCreateElement.call(this, tagName, ...args);
    if (String(tagName).toLowerCase() === 'input' && !el._sdAELHooked) {
      el._sdAELHooked = true;
      _origAddEL.call(el, 'change', _makeSDFirstListener(el), true);
    }
    return el;
  };

  // (B) type 세터 훅 — createElement 이후 type 설정 시 _lastFileInput 갱신 + 보조 훅
  function _onFileInputCreated(inp) {
    _lastFileInput = inp;
    _hookSingleInput(inp); // shadow DOM 내 input에도 대응
  }

  const _typeDesc = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'type');
  if (_typeDesc?.set) {
    Object.defineProperty(HTMLInputElement.prototype, 'type', {
      set: function (val) {
        _typeDesc.set.call(this, val);
        if (String(val).toLowerCase() === 'file') _onFileInputCreated(this);
      },
      get: _typeDesc.get,
      configurable: true,
      enumerable: _typeDesc.enumerable,
    });
  }

  // (C) setAttribute 훅 — innerHTML 등 비정규 경로 대응
  const _origSetAttr = Element.prototype.setAttribute;
  Element.prototype.setAttribute = function (name, value) {
    _origSetAttr.call(this, name, value);
    if (this instanceof HTMLInputElement &&
        name.toLowerCase() === 'type' &&
        String(value).toLowerCase() === 'file') {
      _onFileInputCreated(this);
    }
  };

  // (D) .click() / dispatchEvent 보조 추적
  const _origHTMLInputClick = HTMLInputElement.prototype.click;
  HTMLInputElement.prototype.click = function () {
    if (this.type === 'file') { _lastFileInput = this; }
    return _origHTMLInputClick.call(this);
  };

  const _origDispatchEvent = EventTarget.prototype.dispatchEvent;
  EventTarget.prototype.dispatchEvent = function (event) {
    if (this instanceof HTMLInputElement && this.type === 'file' && event.type === 'click') {
      _lastFileInput = this;
    }
    return _origDispatchEvent.call(this, event);
  };

  // ─── 설정 ─────────────────────────────────────────────────────────────────
  const SUPPORTED_TYPES = new Set([
    'application/pdf',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  ]);
  const SUPPORTED_EXTS = /\.(pdf|docx)$/i;

  function isSupportedFile(b) {
    if (!isProtectionEnabled()) return false;
    if (!b) return false;
    return SUPPORTED_TYPES.has(b.type) || SUPPORTED_EXTS.test(b.name || '');
  }

  // ─── 사이트별 업로드 패턴 설정 ───────────────────────────────────────────
  //
  // dropMode:
  //   'redispatch'        - 합성 DragEvent 재디스패치 (기본, ChatGPT 등)
  //   'passthrough-xhr'   - drop 통과 후 XHR Layer 2에서 인터셉트 (Gemini 등)
  //   'passthrough-fetch' - drop 통과 후 fetch Layer 3에서 인터셉트 (Claude.ai 등)
  //
  // uploadLayer:
  //   'xhr-put'       - XHR PUT + Blob body (ChatGPT → Azure Blob)
  //   'xhr-formdata'  - XHR POST + FormData (Gemini 등)
  //   'fetch-formdata'- fetch POST + FormData (Claude.ai 등)
  //
  // multiUpload: 같은 파일로 업로드 요청이 여러 번 올 수 있음 (캐시 재사용)
  //
  // 새 사이트 추가 시 이 테이블에만 한 줄 추가하면 됨.
  const SITE_CONFIGS = {
    'chatgpt.com':        { dropMode: 'redispatch',        uploadLayer: 'xhr-put',        multiUpload: false },
    'claude.ai':          { dropMode: 'passthrough-fetch', uploadLayer: 'fetch-formdata', multiUpload: true  },
    'gemini.google.com':  { dropMode: 'passthrough-xhr',   uploadLayer: 'xhr-formdata',   multiUpload: false },
    'copilot.microsoft.com': { dropMode: 'passthrough-fetch', uploadLayer: 'fetch-formdata', multiUpload: false }, // POST /c/api/attachments (docs: FormData+Blob, images: raw binary → Layer4)
    'grok.com':           { dropMode: 'passthrough-fetch', uploadLayer: 'fetch-json',     multiUpload: false }, // POST /rest/app-chat/upload-file JSON+base64 → Layer4 arrayBuffer 인터셉트
    'perplexity.ai':      { dropMode: 'passthrough-fetch', uploadLayer: 'fetch-formdata', multiUpload: false },
  };

  // 현재 사이트 설정 (없으면 기본값)
  function getSiteConfig() {
    const host = location.hostname;
    for (const [domain, cfg] of Object.entries(SITE_CONFIGS)) {
      if (host === domain || host.endsWith('.' + domain)) return cfg;
    }
    // 미등록 사이트: 런타임 감지 결과 사용 (2단계에서 채워짐)
    return _detectedConfig;
  }

  // 런타임 자동감지 결과 (미등록 사이트용)
  let _detectedConfig = { dropMode: 'redispatch', uploadLayer: null, multiUpload: false };

  // 미등록 사이트에서 첫 업로드 패턴 감지 시 _detectedConfig를 업데이트
  function _autoDetectUploadLayer(layer) {
    if (_detectedConfig.uploadLayer) return; // 이미 감지됨
    const host = location.hostname;
    // SITE_CONFIGS에 등록된 사이트는 자동감지 불필요
    for (const domain of Object.keys(SITE_CONFIGS)) {
      if (host === domain || host.endsWith('.' + domain)) return;
    }
    _detectedConfig = { ..._detectedConfig, uploadLayer: layer };
    debugLog(`[SecureDoc] 🔍 자동감지: ${host} → uploadLayer=${layer}`);
  }

  // 처리 중 파일 (재귀/중복 방지)
  const _inProcess = new WeakSet();
  // 승인된 마스킹본 파일 (Layer 2에서 통과시킬 파일)
  const _approvedFiles = new WeakSet();
  // 최근 file input 추적 (마스킹본 재주입용)
  let _lastFileInput = null;
  // 마지막으로 승인된 마스킹본 파일 (Layer 3a 등록 요청 수정용)
  let _lastApprovedFile = null;
  // 원본 파일 ChatGPT 등록 file_id (upload_complete 차단으로 원본 카드 제거 유도)
  let _originalFileId = null;
  // Layer 3 위임 사이트(Gemini 등) 드롭 파일의 마스킹 완료 Promise (XHR 인터셉트)
  let _pendingLayer3Promise = null;
  // fetch 레이어 위임 사이트(Claude.ai 등) 드롭 파일의 마스킹 완료 Promise (fetch 인터셉트)
  let _pendingFetchDropPromise = null;
  // drop 원본 파일 메타 (Claude.ai가 new File()로 복사하므로 identity 대신 name+size+type 비교)
  let _pendingFetchDropMeta = null; // { name, size, type }
  // 드롭/업로드 결과로 받은 마스킹 파일 캐시 (같은 파일로 요청이 여러 번 오는 사이트 대비)
  // TTL 30초: 그 이후엔 새 파일로 간주하고 재분석
  let _lastDropResult = null; // { meta: {name, size, type}, maskedFile: File, ts: number }
  const DROP_RESULT_TTL = 30_000;

  function _getCachedDrop(file) {
    if (!_lastDropResult) return null;
    if (Date.now() - _lastDropResult.ts > DROP_RESULT_TTL) { _lastDropResult = null; return null; }
    const m = _lastDropResult.meta;
    if (file.name === m.name && file.size === m.size &&
        (file.type || 'application/octet-stream') === (m.type || 'application/octet-stream')) {
      return _lastDropResult.maskedFile;
    }
    return null;
  }

  function _setCachedDrop(file, maskedFile) {
    _lastDropResult = { meta: { name: file.name, size: file.size, type: file.type }, maskedFile, ts: Date.now() };
  }

  // Layer 1 passthrough-fetch 드롭 결과를 Layer 4에서 활용하기 위한 헬퍼
  // (Grok: fetch JSON+base64, Copilot: fetch raw binary 등 FormData 미사용 사이트)
  function _matchesPendingDrop(file) {
    if (!_pendingFetchDropPromise || !_pendingFetchDropMeta) return false;
    const m = _pendingFetchDropMeta;
    return file.name === m.name && file.size === m.size &&
      (file.type || 'application/octet-stream') === (m.type || 'application/octet-stream');
  }
  async function _awaitPendingDrop() {
    const prom = _pendingFetchDropPromise;
    _pendingFetchDropPromise = null;
    _pendingFetchDropMeta   = null;
    return await prom;
  }

  // ─── fileToBase64 (원본 arrayBuffer 사용, 재귀 없음) ─────────────────────
  async function fileToBase64(file) {
    const ab = await _origBlobArrayBuffer.call(file);
    const arr = new Uint8Array(ab);
    let bin = '';
    for (let i = 0; i < arr.length; i += 8192)
      bin += String.fromCharCode(...arr.subarray(i, i + 8192));
    return btoa(bin);
  }

  function base64ToFile(b64, mime, name) {
    const bin = atob(b64);
    const arr = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
    return new File([arr], name, { type: mime });
  }

  // ─── file input에 파일 주입 ──────────────────────────────────────────────
  function setFileOnInput(input, file) {
    try {
      const dt = new DataTransfer();
      dt.items.add(file);
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'files')?.set;
      if (setter) setter.call(input, dt.files);
      else input.files = dt.files;
      input._sdDone = true;
      input.dispatchEvent(new Event('change', { bubbles: true }));
      input.dispatchEvent(new Event('input',  { bubbles: true }));
      debugLog('[SecureDoc] ♻️ 마스킹본을 인풋에 주입:', file.name);
    } catch (err) {
      console.error('[SecureDoc] 파일 교체 실패:', err);
    }
  }

  // ─── requestProcessing: content.js(isolated)에 처리 요청 ─────────────────
  let _cnt = 0;
  const _pending = new Map();

  function nextRequestId(prefix) {
    const bytes = new Uint32Array(2);
    crypto.getRandomValues(bytes);
    return `${prefix}_${Date.now().toString(36)}_${bytes[0].toString(36)}${bytes[1].toString(36)}_${++_cnt}`;
  }

  function requestProcessing(file) {
    if (!isProtectionEnabled()) return Promise.resolve(null);
    if (_inProcess.has(file)) return Promise.resolve(null);
    _inProcess.add(file);

    return new Promise(async (resolve) => {
      const id = nextRequestId('sd');
      _pending.set(id, { resolve, type: 'req' });
      const timeoutId = setTimeout(() => {
        if (!_pending.has(id)) return;
        _pending.delete(id);
        _inProcess.delete(file);
        resolve({ action: 'cancel' });
      }, 10 * 60 * 1000);
      _pending.get(id).timeoutId = timeoutId;
      try {
        const b64 = await fileToBase64(file);
        debugLog(`[SecureDoc] 📤 처리 요청: ${file.name} (${(file.size / 1024).toFixed(1)} KB)`);
        window.postMessage({
          __securedoc: true,
          direction: 'main-to-isolated',
          bridgeToken: _bridgeToken,
          type: 'SECUREDOC_FILE_SELECTED',
          payload: { inputId: id, base64Data: b64, mimeType: file.type || 'application/octet-stream', fileName: file.name || 'document', fileSize: file.size },
        }, '*');
      } catch (e) {
        clearTimeout(timeoutId);
        console.error('[SecureDoc] 처리 요청 오류:', e);
        _pending.delete(id);
        _inProcess.delete(file);
        resolve(null);
      }
    }).finally(() => _inProcess.delete(file));
  }

  // ─── content.js 응답 수신 ────────────────────────────────────────────────
  window.addEventListener('message', (e) => {
    if (e.source !== window) return;
    if (!e.data?.__securedoc || e.data.direction !== 'isolated-to-main' || e.data.type !== 'SECUREDOC_RESULT') return;
    const { inputId, action, maskedBase64, mimeType, fileName } = e.data.payload;
    const entry = _pending.get(inputId);
    if (!entry) return;
    if (entry.timeoutId) clearTimeout(entry.timeoutId);

    // 마스킹본 파일 생성 (pending.delete 전에 설정 → Layer 3a 경쟁 조건 방지)
    let maskedFile = null;
    if (action === 'upload' && maskedBase64) {
      maskedFile = base64ToFile(maskedBase64, mimeType, fileName);
      _approvedFiles.add(maskedFile);
      _inProcess.add(maskedFile);
      _lastApprovedFile = maskedFile;
    }

    _pending.delete(inputId); // Layer 3a polling이 여기서 깨어남

    if (entry.type === 'input') {
      // Layer 1 경로 (change 이벤트)
      const { input, originalFile } = entry;
      debugLog(`[SecureDoc] 🔔 [RESULT] type=input action=${action} maskedFile=${maskedFile?.name ?? 'null'}`);
      if (maskedFile) {
        debugLog('[SecureDoc] 💉 setFileOnInput 시작:', maskedFile.name, maskedFile.size, 'B');
        setFileOnInput(input, maskedFile);
        debugLog('[SecureDoc] 💉 setFileOnInput 완료, input.files[0]:', input.files?.[0]?.name);
      } else if (action === 'passthrough') {
        setFileOnInput(input, originalFile);
      } else {
        input.value = '';
      }

    } else if (entry.type === 'drop' || entry.type === 'paste') {
      // Layer 1-DROP / 1-PASTE 경로
      const { originalFile, dropTarget, pasteTarget, clientX, clientY } = entry;
      const target = dropTarget ?? pasteTarget ?? document.activeElement ?? document.body;
      const fileToDispatch = maskedFile ?? (action === 'passthrough' ? originalFile : null);

      if (fileToDispatch) {
        const newDT = new DataTransfer();
        newDT.items.add(fileToDispatch);

        if (entry.type === 'drop') {
          try {
            // isTrusted 검사 사이트에서는 synthetic drop이 무시됨 — 감지 후 fallback
            let dispatched = false;
            const dropEl = target;
            const onDropSuccess = () => { dispatched = true; };
            dropEl.addEventListener('drop', onDropSuccess, { once: true, capture: true });
            dropEl.dispatchEvent(new DragEvent('drop', {
              bubbles: true, cancelable: true, composed: true,
              dataTransfer: newDT, clientX, clientY,
            }));
            dropEl.removeEventListener('drop', onDropSuccess, true);
            // 재디스패치 후 fetch/XHR 업로드가 일어나지 않으면 사이트가 무시한 것
            // → 미등록 사이트면 dropMode를 passthrough-fetch로 자동 전환
            setTimeout(() => {
              const host = location.hostname;
              const isKnown = Object.keys(SITE_CONFIGS).some(d => host === d || host.endsWith('.' + d));
              if (!isKnown && _detectedConfig.dropMode === 'redispatch') {
                console.warn('[SecureDoc] drop 재디스패치 효과 미확인 → passthrough-fetch 전환');
                _detectedConfig = { ..._detectedConfig, dropMode: 'passthrough-fetch' };
              }
            }, 2000);
            debugLog('[SecureDoc] ♻️ [DROP] 마스킹본 드롭 재디스패치:', fileToDispatch.name);
          } catch (err) {
            console.error('[SecureDoc] drop 재디스패치 실패:', err);
          }
        } else {
          // paste: ClipboardEvent with clipboardData
          try {
            target.dispatchEvent(new ClipboardEvent('paste', {
              bubbles: true, cancelable: true, composed: true,
              clipboardData: newDT,
            }));
            debugLog('[SecureDoc] ♻️ [PASTE] 마스킹본 붙여넣기 재디스패치:', fileToDispatch.name);
          } catch (_) {
            // ClipboardEvent with files가 막히는 경우 → Layer 2/3에서 처리됨
            console.warn('[SecureDoc] [PASTE] ClipboardEvent 재디스패치 제한 → Layer 2/3 폴백');
          }
        }
      }
      // 취소/다운로드: 재디스패치 없음
      _inProcess.delete(originalFile);

    } else {
      // Layer 2/3 경로 (XHR / fetch) — layer3 드롭 포함
      debugLog(`[SecureDoc] 🔔 [RESULT] type=${entry.type} action=${action} maskedFile=${maskedFile?.name ?? 'null'}`);
      if (entry.originalFile) _inProcess.delete(entry.originalFile);
      entry.resolve(
        maskedFile
          ? { action: 'upload', file: maskedFile }
          : { action: action || 'cancel' }
      );
    }
  });

  // ════════════════════════════════════════════════════════════════════════════
  // 레이어 0: showOpenFilePicker (File System Access API) ← ChatGPT 핵심 경로
  //   ChatGPT는 <input type="file"> 대신 이 API를 사용해 파일 선택.
  //   여기서 가로채면 파일 등록 API 호출 전에 모달 표시 가능.
  // ════════════════════════════════════════════════════════════════════════════
  if (_origShowOpenFilePicker) {
    window.showOpenFilePicker = async function (...args) {
      const handles = await _origShowOpenFilePicker(...args);
      const result = [];
      for (const handle of handles) {
        const file = await handle.getFile();
        if (!isSupportedFile(file)) { result.push(handle); continue; }

        debugLog('[SecureDoc] 📁 [0] showOpenFilePicker 감지:', file.name);
        const res = await requestProcessing(file);

        if (res?.action === 'upload' && res.file) {
          _approvedFiles.add(res.file);
          // 마스킹본 파일을 반환하는 가짜 FileHandle 프록시
          result.push(new Proxy(handle, {
            get (target, prop) {
              if (prop === 'getFile') return async () => res.file;
              const v = target[prop];
              return typeof v === 'function' ? v.bind(target) : v;
            },
          }));
        } else if (res?.action === 'cancel' || res?.action === 'download') {
          // 취소 → picker를 취소한 것처럼 AbortError 던지기
          throw new DOMException('Upload cancelled by upsecurity', 'AbortError');
        } else {
          result.push(handle); // 처리 오류 시 원본 통과
        }
      }
      return result;
    };
  }

  // ════════════════════════════════════════════════════════════════════════════
  // 레이어 1: file input change 이벤트 (capture)
  //   composedPath()로 Shadow DOM 내부 input도 탐지
  // ════════════════════════════════════════════════════════════════════════════
  document.addEventListener('change', async (event) => {
    if (CONTENT_OWNS_LAYER1_UPLOADS) return;
    // 진단 로그: document 레벨까지 이벤트가 도달하는지 확인
    const path = event.composedPath?.() ?? [];
    debugLog('[SecureDoc] 📡 document change:', event.target?.tagName, event.target?.type, 'path:', path.length, 'composed:', path.map(e => e.tagName || e.constructor?.name).join('>'));
    const input = path.find(el => el instanceof HTMLInputElement && el.type === 'file')
                  ?? (event.target instanceof HTMLInputElement && event.target.type === 'file' ? event.target : null);
    if (!input) return;
    _lastFileInput = input; // 항상 추적

    if (input._sdDone) { delete input._sdDone; return; }
    const file = input.files?.[0];
    if (!isSupportedFile(file) || _inProcess.has(file) || _approvedFiles.has(file)) return;

    debugLog('[SecureDoc] 📁 [1] change 이벤트:', file.name);
    event.stopImmediatePropagation();

    const id = nextRequestId('sd');
    _pending.set(id, { type: 'input', input, originalFile: file });
    _inProcess.add(file);
    try {
      const b64 = await fileToBase64(file);
      window.postMessage({ __securedoc: true, direction: 'main-to-isolated', bridgeToken: _bridgeToken, type: 'SECUREDOC_FILE_SELECTED', payload: { inputId: id, base64Data: b64, mimeType: file.type || 'application/octet-stream', fileName: file.name, fileSize: file.size } }, '*');
    } catch (e) { _pending.delete(id); _inProcess.delete(file); }
  }, true);

  // ════════════════════════════════════════════════════════════════════════════
  // 레이어 1-DROP: 드래그앤드롭 파일 가로채기 (모든 사이트 대응)
  //   document 레벨 capture → 사이트 핸들러보다 먼저 파일 획득 → 전파 차단
  //   처리 완료 후 마스킹본을 담은 새 DragEvent를 원본 target에 재디스패치
  // ════════════════════════════════════════════════════════════════════════════
  document.addEventListener('dragover', (e) => {
    if (CONTENT_OWNS_LAYER1_UPLOADS) return;
    // Layer 3 위임 사이트(Gemini 등): dragover도 건드리지 않음
    // → 페이지 전체를 드롭 가능 영역으로 만들면 Gemini 의도한 드롭존 외의
    //   버그 있는 핸들러(this.drop is not a function)가 실행됨
    if (getSiteConfig().dropMode === 'passthrough-xhr') return;

    if (Array.from(e.dataTransfer?.items ?? []).some(i => i.kind === 'file')) {
      e.preventDefault();
    }
  }, true);

  document.addEventListener('drop', async (event) => {
    if (CONTENT_OWNS_LAYER1_UPLOADS) return;
    const files = Array.from(event.dataTransfer?.files ?? []);
    const file = files.find(f => isSupportedFile(f) && !_inProcess.has(f) && !_approvedFiles.has(f));
    if (!file) return; // 지원 안 되는 파일 or 승인된 마스킹본 → 통과

    const cfg = getSiteConfig();

    // DragEvent 재디스패치가 깨지는 사이트 → 이벤트 통과, XHR Layer 2에서 인터셉트
    if (cfg.dropMode === 'passthrough-xhr') {
      _inProcess.add(file);
      _pendingLayer3Promise = fileToBase64(file).then(b64 => new Promise(resolve => {
        const id = nextRequestId('sd');
        _pending.set(id, { type: 'layer3', resolve, originalFile: file });
        window.postMessage({ __securedoc: true, direction: 'main-to-isolated', bridgeToken: _bridgeToken, type: 'SECUREDOC_FILE_SELECTED',
          payload: { inputId: id, base64Data: b64,
                     mimeType: file.type || 'application/octet-stream',
                     fileName: file.name, fileSize: file.size } }, '*');
      })).catch(() => { _pendingLayer3Promise = null; _inProcess.delete(file); });
      debugLog('[SecureDoc] 📁 [1-DROP→L3] 모달 표시, XHR 대기:', file.name);
      return;
    }

    // isTrusted=false DragEvent를 거부하는 사이트 → drop 통과, fetch 레이어에서 인터셉트
    if (cfg.dropMode === 'passthrough-fetch') {
      _inProcess.add(file);
      _pendingFetchDropMeta = { name: file.name, size: file.size, type: file.type };
      _pendingFetchDropPromise = fileToBase64(file).then(b64 => new Promise(resolve => {
        const id = nextRequestId('sd');
        _pending.set(id, { type: 'layer3', resolve, originalFile: file });
        window.postMessage({ __securedoc: true, direction: 'main-to-isolated', bridgeToken: _bridgeToken, type: 'SECUREDOC_FILE_SELECTED',
          payload: { inputId: id, base64Data: b64,
                     mimeType: file.type || 'application/octet-stream',
                     fileName: file.name, fileSize: file.size } }, '*');
      })).catch(() => { _pendingFetchDropPromise = null; _pendingFetchDropMeta = null; _inProcess.delete(file); });
      debugLog('[SecureDoc] 📁 [1-DROP→FetchL3] 모달 표시, fetch 대기:', file.name);
      return;
    }

    event.preventDefault();
    event.stopImmediatePropagation();
    debugLog('[SecureDoc] 📁 [1-DROP] 드래그앤드롭 가로챔:', file.name);

    // 사이트 자체의 "여기에 드롭하세요" 오버레이는 보통 dragenter에서 뜨고 drop에서
    // 닫힌다 — 방금 실제 drop을 stopImmediatePropagation으로 죽여버려 사이트가 그
    // 이벤트를 못 보므로, 검사(스캔+검토)가 끝날 때까지 오버레이가 화면에 남는다
    // (실측: ChatGPT 등에서 재현됨).
    //
    // 처음엔 dragleave/dragend 합성 이벤트를 대신 흘려보내는 방식으로 고쳤었는데
    // (이전 커밋), 실측 결과 오버레이가 여전히 안 사라졌고 — 오히려 사이트가 dragenter
    // 시점에 시작한 내부 "드래그 진행 중" 상태를 이 시점에 미리 리셋시켜버려서,
    // 검사 완료 후 마스킹본을 재주입하는 새 DragEvent('drop', ...)(아래, §변경1)을
    // 사이트가 무시하는 부작용까지 생겼다(추정 원인: dragenter 카운트만큼 dragleave가
    // 안 맞으면 오버레이가 안 꺼지는 카운터 기반 구현이거나, drop 이 아닌 dragleave로는
    // 애초에 오버레이를 안 닫는 구현). "파일이 없는 진짜 drop 이벤트"를 그대로
    // 흘려보내는 쪽이 더 안전하다 — drop 은 대부분의 구현에서 dragenter 카운터와
    // 무관하게 무조건 "드래그 종료"로 처리되고, 아래 이 함수 자신의 early-return
    // (`if (!file) return`) 덕분에 이 빈 drop 은 우리 로직을 다시 타지 않고 그대로
    // 사이트 핸들러까지 전파된다(같은 async 함수가 재귀 호출되지만 file 없음 분기라
    // 즉시 반환 — 무한루프 없음).
    const resetTarget = event.target || document;
    const emptyDataTransfer = new DataTransfer();
    resetTarget.dispatchEvent(
      new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer: emptyDataTransfer })
    );

    _inProcess.add(file);
    const id = nextRequestId('sd');
    _pending.set(id, {
      type: 'drop',
      originalFile: file,
      dropTarget: event.target,
      clientX: event.clientX,
      clientY: event.clientY,
    });
    try {
      const b64 = await fileToBase64(file);
      window.postMessage({
        __securedoc: true, direction: 'main-to-isolated', bridgeToken: _bridgeToken, type: 'SECUREDOC_FILE_SELECTED',
        payload: { inputId: id, base64Data: b64,
                   mimeType: file.type || 'application/octet-stream',
                   fileName: file.name, fileSize: file.size },
      }, '*');
    } catch (e) { _pending.delete(id); _inProcess.delete(file); }
  }, true);

  // ════════════════════════════════════════════════════════════════════════════
  // 레이어 1-PASTE: 붙여넣기 파일 가로채기 (Ctrl+V / Cmd+V)
  //   paste 이벤트 캡처 → clipboardData.files에서 파일 추출 → 전파 차단
  //   처리 완료 후 마스킹본으로 ClipboardEvent 재디스패치
  //   ClipboardEvent 제한 시 → Layer 2/3 (XHR/fetch 훅)에서 폴백 처리
  // ════════════════════════════════════════════════════════════════════════════
  document.addEventListener('paste', async (event) => {
    if (CONTENT_OWNS_LAYER1_UPLOADS) return;
    const files = Array.from(event.clipboardData?.files ?? []);
    const file = files.find(f => isSupportedFile(f) && !_inProcess.has(f) && !_approvedFiles.has(f));
    if (!file) return;

    event.preventDefault();
    event.stopImmediatePropagation();
    debugLog('[SecureDoc] 📁 [1-PASTE] 붙여넣기 가로챔:', file.name);

    _inProcess.add(file);
    const id = nextRequestId('sd');
    _pending.set(id, {
      type: 'paste',
      originalFile: file,
      pasteTarget: event.target,
    });
    try {
      const b64 = await fileToBase64(file);
      window.postMessage({
        __securedoc: true, direction: 'main-to-isolated', bridgeToken: _bridgeToken, type: 'SECUREDOC_FILE_SELECTED',
        payload: { inputId: id, base64Data: b64,
                   mimeType: file.type || 'application/octet-stream',
                   fileName: file.name, fileSize: file.size },
      }, '*');
    } catch (e) { _pending.delete(id); _inProcess.delete(file); }
  }, true);

  // MutationObserver: 동적으로 추가되는 file input에 직접 리스너 부착
  new MutationObserver((mutations) => {
    if (CONTENT_OWNS_LAYER1_UPLOADS) return;
    for (const m of mutations) {
      for (const node of m.addedNodes) {
        if (node.nodeType !== 1) continue;
        const inputs = [];
        if (node.matches?.('input[type="file"]')) inputs.push(node);
        node.querySelectorAll?.('input[type="file"]').forEach(n => inputs.push(n));
        inputs.forEach(inp => {
          if (inp._sdHooked) return;
          inp._sdHooked = true;
          inp.addEventListener('change', async function (event) {
            const p = event.composedPath?.() ?? [];
            const realInp = p.find(el => el instanceof HTMLInputElement && el.type === 'file') ?? inp;
            _lastFileInput = realInp;
            if (inp._sdDone) { delete inp._sdDone; return; }
            const file = inp.files?.[0];
            if (!isSupportedFile(file) || _inProcess.has(file)) return;
            debugLog('[SecureDoc] 📁 [1b] MutationObserver input:', file.name);
            event.stopImmediatePropagation();
            const id = nextRequestId('sd');
            _pending.set(id, { type: 'input', input: inp, originalFile: file });
            _inProcess.add(file);
            try {
              const b64 = await fileToBase64(file);
              window.postMessage({ __securedoc: true, direction: 'main-to-isolated', bridgeToken: _bridgeToken, type: 'SECUREDOC_FILE_SELECTED', payload: { inputId: id, base64Data: b64, mimeType: file.type || 'application/octet-stream', fileName: file.name, fileSize: file.size } }, '*');
            } catch (e) { _pending.delete(id); _inProcess.delete(file); }
          }, true);
        });
      }
    }
  }).observe(document.documentElement, { childList: true, subtree: true });

  // ════════════════════════════════════════════════════════════════════════════
  // 레이어 2: XHR + Blob body ← ChatGPT Azure PUT 경로
  //
  // 핵심 전략:
  //   원본 파일 → Azure PUT 차단 → 모달 표시
  //   사용자 승인 → _approvedFiles에 마스킹본 등록 → 파일 인풋에 주입
  //                → ChatGPT가 마스킹본으로 새로 등록 + Azure PUT 재시도
  //   사용자 취소 → Azure PUT 취소 (ChatGPT 업로드 실패)
  //   사용자 다운로드 → 다운로드만, Azure PUT 취소
  // ════════════════════════════════════════════════════════════════════════════
  const _xhrUrls = new WeakMap();

  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    _xhrUrls.set(this, String(url));
    return _origXHROpen.call(this, method, url, ...rest);
  };


  XMLHttpRequest.prototype.send = function (body) {
    if (!isProtectionEnabled()) return _origXHRSend.call(this, body);

    // ── [DEBUG] XHR body 타입 로깅 ───────────────────────────────────────────
    if (body) {
      const url = _xhrUrls.get(this) || '';
      if (!url.includes('google-analytics') && !url.includes('doubleclick') && !url.includes('/log?')) {
        const desc = body instanceof File ? `File(${body.name},${body.size}B,${body.type})`
                   : body instanceof Blob ? `Blob(${body.size}B,${body.type})`
                   : body instanceof FormData ? `FormData([${[...body.keys()].join(',')}])`
                   : body instanceof ArrayBuffer ? `ArrayBuffer(${body.byteLength}B)`
                   : body?.constructor?.name ?? typeof body;
        debugLog(`[SecureDoc] 🔍 XHR.send: ${url.slice(0,70)} | body: ${desc}`);

      }
    }
    // ── 승인된 마스킹본: 통과 ────────────────────────────────────────────────
    if (body instanceof Blob && _approvedFiles.has(body)) {
      debugLog('[SecureDoc] ✅ 승인된 마스킹본 업로드 통과');
      _lastApprovedFile = null;
      return _origXHRSend.call(this, body);
    }

    // ── content.js 가 이미 검토를 마치고 주입한 파일: 통과 ──────────────────────
    // (위 _approvedFiles 는 MAIN world 소속 File 만 알아본다 — 파일 목록 주석 참고)
    if (body instanceof Blob && _isContentApprovedBlob(body)) {
      debugLog('[SecureDoc] ✅ content 검토 완료 파일 업로드 통과 (XHR)');
      return _origXHRSend.call(this, body);
    }

    // ── 자동감지: XHR Blob PUT → xhr-put, XHR FormData → xhr-formdata ────────
    if (body instanceof Blob && isSupportedFile(body)) _autoDetectUploadLayer('xhr-put');
    if (body instanceof FormData) {
      const fd = body;
      for (const val of fd.values()) {
        if (val instanceof File && isSupportedFile(val)) { _autoDetectUploadLayer('xhr-formdata'); break; }
      }
    }

    // ── Layer 3 위임 사이트(Gemini 등) Blob 업로드 인터셉트 ─────────────────
    // MIME type 없는 anonymous Blob으로 업로드하므로 isSupportedFile 우회 필요
    if (body instanceof Blob && !_approvedFiles.has(body)
        && getSiteConfig().dropMode === 'passthrough-xhr') {
      // 캐시된 결과 재사용 (multiUpload 사이트에서 같은 Blob이 재전송되는 경우)
      const blobAsFile = body instanceof File ? body : null;
      const cached = blobAsFile && _getCachedDrop(blobAsFile);
      if (cached) {
        debugLog('[SecureDoc] ✅ [2-L3] 캐시 재사용 XHR 전송');
        _approvedFiles.add(cached);
        return _origXHRSend.call(this, cached);
      }
      if (!_pendingLayer3Promise) return _origXHRSend.call(this, body);
      const self = this;
      const prom = _pendingLayer3Promise;
      _pendingLayer3Promise = null;
      const origUrl = _xhrUrls.get(this) || '';
      debugLog(`[SecureDoc] 📁 [2-L3] XHR Blob 업로드 차단: ${origUrl.slice(0, 60)}`);
      prom.then(result => {
        if (result?.action === 'upload' && result.file) {
          _approvedFiles.add(result.file);
          if (blobAsFile) _setCachedDrop(blobAsFile, result.file);
          _origXHRSend.call(self, result.file);
          debugLog('[SecureDoc] ✅ [2-L3] 마스킹본 XHR 전송');
        } else if (result?.action === 'cancel' || result?.action === 'download') {
          debugLog('[SecureDoc] 🚫 [2-L3] XHR 차단');
        } else {
          _origXHRSend.call(self, body);
        }
      }).catch(() => _origXHRSend.call(self, body));
      return;
    }

    // ── 원본 파일 (Blob body): 차단 후 처리 ─────────────────────────────────
    if (body instanceof Blob && isSupportedFile(body) && !_inProcess.has(body)) {
      const file = body instanceof File
        ? body
        : new File([body], 'upload.' + (body.type === 'application/pdf' ? 'pdf' : 'docx'), { type: body.type });
      const self = this;
      const url = _xhrUrls.get(this) || '';
      const capturedInput = _lastFileInput;
      debugLog(`[SecureDoc] 📁 [2] XHR Blob 차단: ${file.name} (input=${capturedInput ? '추적됨' : 'null'}) -> ${url.slice(0, 60)}...`);
      requestProcessing(file).then((result) => {
        if (result?.action === 'upload' && result.file) {
          _approvedFiles.add(result.file);
          _inProcess.add(result.file);
          if (capturedInput) {
            debugLog('[SecureDoc] ♻️ 마스킹본 -> 파일 인풋 재주입');
            setFileOnInput(capturedInput, result.file);
            try { self.abort(); } catch (_) {}
          } else {
            debugLog('[SecureDoc] ⚠️ 파일 인풋 미추적 -> 직접 XHR 전송');
            _origXHRSend.call(self, result.file);
          }
        } else if (result?.action === 'cancel') {
          debugLog('[SecureDoc] 🚫 업로드 취소 (원본 XHR 차단)');
        } else if (result?.action === 'download') {
          debugLog('[SecureDoc] 💾 다운로드 선택 (원본 XHR 차단)');
        } else {
          console.warn('[SecureDoc] ⚠️ 처리 오류 -> 원본 통과');
          _origXHRSend.call(self, body);
        }
      }).catch(() => {
        console.error('[SecureDoc] requestProcessing 오류 -> 원본 통과');
        _origXHRSend.call(self, body);
      });
      return;
    }

    // ── FormData ──────────────────────────────────────────────────────────────
    if (body instanceof FormData) {
      const file = findFileInFD(body);
      if (file && !_inProcess.has(file) && !_approvedFiles.has(file) && !_isContentApprovedBlob(file)) {
        const self = this;
        debugLog(`[SecureDoc] 📁 [2] XHR FormData: ${file.name}`);
        requestProcessing(file).then((result) => {
          if (result?.action === 'upload' && result.file) {
            _approvedFiles.add(result.file);
            _origXHRSend.call(self, replaceFD(body, file.name, result.file));
          } else if (result?.action === 'cancel' || result?.action === 'download') {
            // 차단
          } else {
            _origXHRSend.call(self, body);
          }
        }).catch(() => _origXHRSend.call(self, body));
        return;
      }
    }

    _origXHRSend.call(this, body);
  };

  // ── FormData 헬퍼 ─────────────────────────────────────────────────────────
  function findFileInFD(fd) {
    for (const [, val] of fd.entries()) {
      if (val instanceof File && isSupportedFile(val)) return val;
    }
    return null;
  }
  function replaceFD(fd, origName, newFile) {
    const out = new FormData();
    for (const [key, val] of fd.entries()) {
      if (val instanceof File && val.name === origName) out.append(key, newFile, newFile.name);
      else out.append(key, val);
    }
    return out;
  }

  // ════════════════════════════════════════════════════════════════════════════
  // 레이어 3: fetch
  //   3a: ChatGPT 파일 등록 API (/backend-api/files POST)
  //   3b: FormData / Blob body 가로채기
  // ════════════════════════════════════════════════════════════════════════════
  // CSP 위반 광고 도메인 → 요청 자체를 차단해서 에러 미출력
  const _SILENT_BLOCK_RE = /doubleclick\.net|googlesyndication\.com|googleadservices\.com|\.g\.doubleclick\.net|stats\.g\.doubleclick/;
  // 일반 추적 URL → 원본 fetch 위임 (최소 스택)
  const _PASSTHROUGH_RE  = /google-analytics\.com|googletagmanager\.com|\/log\?|\/log\b|\/collect\?/;

  window.fetch = async function (input, init = {}) {
    if (!isProtectionEnabled()) return _origFetch(input, init);

    const url    = String(input?.url ?? input);

    // CSP 위반 확실한 광고 URL → 빈 204로 silently 처리 (에러 미발생)
    if (_SILENT_BLOCK_RE.test(url)) return new Response('', { status: 204, statusText: 'No Content' });
    // 기타 추적 URL → 원본 fetch 위임
    if (_PASSTHROUGH_RE.test(url)) return _origFetch(input, init);

    const method = (init?.method ?? 'GET').toUpperCase();
    const body   = init?.body;
    const _isFilesApi = url.includes('/backend-api/files') || url.includes('/backend-anon/files');


    // ── upload_complete 차단 (원본 파일 카드 제거 유도) ──────────────────────
    if (method === 'POST' && _isFilesApi && url.includes('/upload_complete') && _originalFileId) {
      const m = url.match(/\/files\/([^/]+)\/upload_complete/);
      if (m?.[1] === _originalFileId) {
        debugLog('[SecureDoc] 🚫 원본 upload_complete 차단 -> 원본 카드 제거 유도:', _originalFileId);
        _originalFileId = null;
        return new Response(JSON.stringify({ detail: 'Upload validation failed' }),
          { status: 400, headers: { 'Content-Type': 'application/json' } });
      }
    }

    // ── 3a: ChatGPT 파일 등록 API — 마스킹본 메타데이터로 교체 ────────────
    if (method === 'POST' && _isFilesApi && !url.includes('/upload_complete')) {
      if (_pending.size > 0) {
        debugLog('[SecureDoc] ⏳ 파일 등록 API 대기 — 마스킹 완료까지 홀딩...');
        await new Promise(r => {
          const t = setInterval(() => { if (_pending.size === 0) { clearInterval(t); r(); } }, 150);
          setTimeout(() => { clearInterval(t); r(); }, 90000);
        });
        const mf = _lastApprovedFile;
        if (mf) {
          debugLog(`[SecureDoc] ✏️ 파일 등록 -> 마스킹본 메타데이터 (${mf.name}, ${mf.size}B)`);
          try {
            const origBody = init?.body;
            const bodyStr = typeof origBody === 'string' ? origBody
                          : origBody instanceof ArrayBuffer ? new TextDecoder().decode(origBody)
                          : origBody ? await new Response(origBody).text() : null;
            if (bodyStr) {
              const obj = JSON.parse(bodyStr);
              if ('name'         in obj) obj.name         = mf.name;
              if ('file_name'    in obj) obj.file_name    = mf.name;
              if ('size'         in obj) obj.size         = mf.size;
              if ('file_size'    in obj) obj.file_size    = mf.size;
              if ('mime_type'    in obj) obj.mime_type    = mf.type;
              if ('content_type' in obj) obj.content_type = mf.type;
              return _origFetch(input, { ...init, body: JSON.stringify(obj) });
            }
          } catch (err) { console.warn('[SecureDoc] 등록 body 수정 실패:', err.message); }
        }
        console.warn('[SecureDoc] ⚠️ 파일 등록 — 원본 body 통과 (마스킹본 없음)');
      } else if (!_lastApprovedFile) {
        // content.js 가 이미 검토를 마치고 주입한 파일이면 이건 "원본"이 아니라
        // 승인본이다 — 여기서 file_id 를 원본으로 기록해두면 뒤이은 upload_complete
        // 가 위 분기에서 400 으로 차단되어 첨부 자체가 실패한다.
        let approvedByContent = false;
        if (typeof init?.body === 'string') {
          try {
            const o = JSON.parse(init.body);
            approvedByContent = _isContentApprovedName(o.file_name ?? o.name);
          } catch (_) { /* JSON 이 아니면 원래대로 원본 취급 */ }
        }
        // 원본 파일 등록 요청 -> 통과시키되 file_id 기록
        const resp = await _origFetch(input, init);
        if (resp.ok && !approvedByContent) {
          resp.clone().json().then(data => {
            if (data.file_id) {
              _originalFileId = data.file_id;
              debugLog('[SecureDoc] 📝 원본 file_id 기록:', _originalFileId);
            }
          }).catch(() => {});
        }
        return resp;
      }
      // else: 마스킹본 등록 -> 그냥 통과
    }

    if (body instanceof Blob && _approvedFiles.has(body)) {
      debugLog(`[SecureDoc] ✅ [3] fetch Blob 승인 통과: ${body.name || 'blob'}`);
      return _origFetch(input, init);
    }
    if (body instanceof FormData) {
      const file = findFileInFD(body);
      if (file && isSupportedFile(file)) _autoDetectUploadLayer('fetch-formdata');
      if (file && _isContentApprovedBlob(file)) {
        debugLog(`[SecureDoc] ✅ content 검토 완료 파일 업로드 통과 (fetch FormData): ${file.name}`);
        return _origFetch(input, init);
      }
      if (file) {
        const approved = _approvedFiles.has(file);
        const inProc   = _inProcess.has(file);
        // drop: 파일을 new File()로 복사하는 사이트 → name+size+type 메타 매칭
        const metaMatchesPending = _pendingFetchDropPromise && _pendingFetchDropMeta &&
          file.name === _pendingFetchDropMeta.name && file.size === _pendingFetchDropMeta.size &&
          (file.type || 'application/octet-stream') === (_pendingFetchDropMeta.type || 'application/octet-stream');
        const cachedMasked = !metaMatchesPending && _getCachedDrop(file);
        debugLog(`[SecureDoc] 🔍 [3] fetch FormData: ${file.name} | approved=${approved} inProcess=${inProc} pendingDrop=${metaMatchesPending} cached=${!!cachedMasked} url=${url.slice(0,60)}`);
        if (!approved) {
          // drop 경로: 메타 매칭으로 대기
          if (metaMatchesPending) {
            debugLog(`[SecureDoc] 📁 [3] fetch FormData 드롭 대기: ${file.name}`);
            const prom = _pendingFetchDropPromise;
            _pendingFetchDropPromise = null;
            _pendingFetchDropMeta = null;
            const result = await prom;
            if (result?.action === 'upload' && result.file) {
              _setCachedDrop(file, result.file);
              return _origFetch(input, { ...init, body: replaceFD(body, file.name, result.file) });
            }
            _lastDropResult = null;
            if (result?.action === 'cancel' || result?.action === 'download') {
              return new Response('{}', { status: 200 });
            }
            return _origFetch(input, init);
          }
          // 캐시된 결과 재사용 (같은 파일로 두 번 이상 요청하는 사이트)
          if (cachedMasked) {
            debugLog(`[SecureDoc] 📁 [3] fetch FormData 캐시 재사용: ${file.name}`);
            return _origFetch(input, { ...init, body: replaceFD(body, file.name, cachedMasked) });
          }
          // 일반 경로: 파일 인풋 업로드
          if (!inProc) {
            debugLog(`[SecureDoc] 📁 [3] fetch FormData 차단: ${file.name}`);
            const result = await requestProcessing(file);
            if (result?.action === 'upload' && result.file) {
              _approvedFiles.add(result.file);
              return _origFetch(input, { ...init, body: replaceFD(body, file.name, result.file) });
            }
            if (result?.action === 'cancel' || result?.action === 'download') {
              return new Response('{}', { status: 200 });
            }
          } else {
            debugLog(`[SecureDoc] ⚠️ [3] fetch FormData inProcess 상태로 통과: ${file.name}`);
          }
        }
      }
    }
    if (body instanceof Blob && isSupportedFile(body) && !_inProcess.has(body) && !_isContentApprovedBlob(body)) {
      const file = body instanceof File ? body : new File([body], 'upload', { type: body.type });
      debugLog(`[SecureDoc] 📁 [3] fetch Blob: ${file.name}`);
      const result = await requestProcessing(file);
      if (result?.action === 'upload' && result.file) {
        _approvedFiles.add(result.file);
        return _origFetch(input, { ...init, body: result.file });
      }
      if (result?.action === 'cancel' || result?.action === 'download') {
        return new Response('{}', { status: 200 });
      }
    }
    return _origFetch(input, init);
  };

  // ════════════════════════════════════════════════════════════════════════════
  // Layer 4: File.arrayBuffer / FileReader (backup interceptors)
  // ════════════════════════════════════════════════════════════════════════════
  Blob.prototype.arrayBuffer = async function () {
    if (this instanceof File && isSupportedFile(this) && !_approvedFiles.has(this)) {
      // passthrough-fetch 드롭 대기 (Grok JSON+base64, Copilot raw binary 등)
      if (_matchesPendingDrop(this)) {
        debugLog(`[SecureDoc] [4] arrayBuffer() 드롭 대기: ${this.name}`);
        _inProcess.delete(this);
        const result = await _awaitPendingDrop();
        if (result?.action === 'upload' && result.file) {
          _approvedFiles.add(result.file);
          return _origBlobArrayBuffer.call(result.file);
        }
        return new ArrayBuffer(0);
      }
      if (!_inProcess.has(this)) {
        debugLog(`[SecureDoc] [4] arrayBuffer(): ${this.name}`);
        const result = await requestProcessing(this);
        if (result?.action === 'upload' && result.file) {
          _approvedFiles.add(result.file);
          return _origBlobArrayBuffer.call(result.file);
        }
        if (result?.action === 'cancel') return new ArrayBuffer(0);
      }
    }
    return _origBlobArrayBuffer.call(this);
  };

  const _origBlobText   = Blob.prototype.text;
  const _origBlobStream = Blob.prototype.stream;
  Blob.prototype.text = function () { return _origBlobText.call(this); };
  Blob.prototype.stream = function () { return _origBlobStream.call(this); };

  function makeHook(orig) {
    return function (blob, ...a) {
      if (blob instanceof File && isSupportedFile(blob) && !_approvedFiles.has(blob)) {
        const self = this;
        // passthrough-fetch 드롭 대기 (Grok readAsDataURL 등)
        if (_matchesPendingDrop(blob)) {
          debugLog(`[SecureDoc] [4] FileReader 드롭 대기: ${blob.name}`);
          _inProcess.delete(blob);
          _awaitPendingDrop().then((result) => {
            if (result?.action === 'upload' && result.file) {
              _approvedFiles.add(result.file);
              orig.call(self, result.file, ...a);
            } else if (result?.action === 'cancel') {
              orig.call(self, new File([], blob.name, { type: blob.type }), ...a);
            } else { orig.call(self, blob, ...a); }
          });
          return;
        }
        if (!_inProcess.has(blob)) {
          debugLog(`[SecureDoc] [4] FileReader: ${blob.name}`);
          requestProcessing(blob).then((result) => {
            if (result?.action === 'upload' && result.file) {
              _approvedFiles.add(result.file);
              orig.call(self, result.file, ...a);
            } else if (result?.action === 'cancel') {
              orig.call(self, new File([], blob.name, { type: blob.type }), ...a);
            } else { orig.call(self, blob, ...a); }
          });
          return;
        }
      }
      return orig.call(this, blob, ...a);
    };
  }

  FileReader.prototype.readAsArrayBuffer  = makeHook(_origFRReadAsAB);
  FileReader.prototype.readAsDataURL      = makeHook(_origFRReadAsDataURL);
  FileReader.prototype.readAsBinaryString = makeHook(_origFRReadAsBinStr);

  // ════════════════════════════════════════════════════════════════════════════
  // PROMPT INTERCEPTION — 프롬프트 전송 인터셉트
  // ════════════════════════════════════════════════════════════════════════════

  const PROMPT_CONFIGS = {
    'chatgpt.com': {
      editorSel:   '#prompt-textarea',
      sendBtnSel:  '[data-testid="send-button"]',
      editorType:  'prosemirror',
    },
    'claude.ai': {
      editorSel:   '[data-testid="chat-input"]',
      sendBtnSel:  'button[aria-label="메시지 보내기"], button[aria-label="Send message"]',
      editorType:  'prosemirror',
    },
    'gemini.google.com': {
      editorSel:   '.ql-editor[role="textbox"]',
      sendBtnSel:  'button[aria-label="메시지 보내기"], button[aria-label="Send message"]',
      editorType:  'quill',
    },
    'grok.com': {
      editorSel:   '[aria-label="Ask Grok anything"]',
      sendBtnSel:  '[data-testid="chat-submit"]',
      editorType:  'prosemirror',
    },
    'perplexity.ai': {
      editorSel:   'textarea[placeholder], [contenteditable="true"][aria-label]',
      sendBtnSel:  'button[aria-label="Submit"]',
      editorType:  'lexical',
    },
    'copilot.microsoft.com': {
      editorSel:   'textarea, [contenteditable="true"]',
      sendBtnSel:  'button[aria-label="제출"], button[aria-label="Submit"]',
      editorType:  'unknown',
    },
  };

  function _getPromptConfig() {
    const host = location.hostname;
    for (const [domain, cfg] of Object.entries(PROMPT_CONFIGS)) {
      if (host === domain || host.endsWith('.' + domain)) return cfg;
    }
    return null;
  }

  // ── 상태 플래그 ─────────────────────────────────────────────────────────────
  const _promptPending = new Map();  // promptId → { resolve }
  let _promptInProcess = false;      // 분석 중 중복 차단
  let _promptApproved  = false;      // 재전송 시 인터셉트 스킵

  // ── 에디터 텍스트 추출 ────────────────────────────────────────────────────────

  function _getEditorText(cfg) {
    if (!cfg.editorSel) return '';
    const editor = document.querySelector(cfg.editorSel);
    if (!editor) return '';
    if (editor.tagName === 'TEXTAREA') return editor.value.trim();
    return (editor.innerText || editor.textContent || '').trim();
  }

  // ── 에디터 텍스트 주입 ────────────────────────────────────────────────────────

  function _setEditorText(cfg, text) {
    if (!cfg.editorSel) return false;
    const editor = document.querySelector(cfg.editorSel);
    if (!editor) return false;

    editor.focus();

    if (editor.tagName === 'TEXTAREA') {
      // React controlled input: native setter 사용
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
      setter.call(editor, text);
      editor.dispatchEvent(new Event('input',  { bubbles: true }));
      editor.dispatchEvent(new Event('change', { bubbles: true }));
      return true;
    }

    // contenteditable (ProseMirror, Quill, Lexical)
    // execCommand: ProseMirror / Quill에서 정상 동작
    try {
      document.execCommand('selectAll', false, null);
      document.execCommand('delete',     false, null);
      document.execCommand('insertText', false, text);
    } catch (e) {
      // 폴백: textContent 직접 설정 후 이벤트 발생
      editor.textContent = text;
    }

    // React / 프레임워크 상태 동기화
    editor.dispatchEvent(new InputEvent('input', {
      bubbles: true, data: text, inputType: 'insertText',
    }));

    // Lexical 폴백: clipboard paste
    if (cfg.editorType === 'lexical') {
      try {
        const dt = new DataTransfer();
        dt.setData('text/plain', text);
        editor.dispatchEvent(new ClipboardEvent('paste', {
          clipboardData: dt, bubbles: true, cancelable: true,
        }));
      } catch (e) { /* ignore */ }
    }

    return true;
  }

  // ── 재전송 ──────────────────────────────────────────────────────────────────

  async function _reSubmitPrompt(cfg) {
    // React 상태 업데이트 대기
    await new Promise(r => setTimeout(r, 200));

    if (cfg.sendBtnSel) {
      // 첫 번째로 매칭되는 활성화된 버튼 클릭
      for (const sel of cfg.sendBtnSel.split(',').map(s => s.trim())) {
        const btn = document.querySelector(sel);
        if (btn && !btn.disabled && btn.getAttribute('aria-disabled') !== 'true') {
          btn.click();
          return;
        }
      }
    }

    // 버튼이 없으면 Enter 키 발송
    const editor = cfg.editorSel && document.querySelector(cfg.editorSel);
    if (editor) {
      editor.focus();
      editor.dispatchEvent(new KeyboardEvent('keydown', {
        key: 'Enter', code: 'Enter', keyCode: 13, which: 13,
        bubbles: true, cancelable: true,
      }));
    }
  }

  // ── content.js에 처리 요청 ──────────────────────────────────────────────────

  function _requestPromptProcessing(text) {
    const id = nextRequestId('sp');
    return new Promise((resolve) => {
      _promptPending.set(id, { resolve });
      const timeoutId = setTimeout(() => {
        if (!_promptPending.has(id)) return;
        _promptPending.delete(id);
        resolve({ action: 'cancel' });
      }, 10 * 60 * 1000);
      _promptPending.get(id).timeoutId = timeoutId;
      window.postMessage({
        __securedoc: true,
        direction: 'main-to-isolated',
        bridgeToken: _bridgeToken,
        type: 'SECUREDOC_PROMPT_SELECTED',
        payload: { promptId: id, text },
      }, '*');
    });
  }

  // ── content.js 결과 수신 ────────────────────────────────────────────────────

  window.addEventListener('message', (e) => {
    if (e.source !== window) return;
    if (!e.data?.__securedoc || e.data.direction !== 'isolated-to-main' || e.data.type !== 'SECUREDOC_PROMPT_RESULT') return;
    const { promptId, action, maskedText } = e.data.payload;
    const entry = _promptPending.get(promptId);
    if (!entry) return;
    if (entry.timeoutId) clearTimeout(entry.timeoutId);
    _promptPending.delete(promptId);
    entry.resolve({ action, maskedText });
  });

  // ── 인터셉트 공통 처리 ──────────────────────────────────────────────────────

  async function _interceptPromptSubmit(event, cfg) {
    if (!isProtectionEnabled()) return;

    const text = _getEditorText(cfg);
    if (!text || text.length < 2) return; // 빈 텍스트 스킵

    event.preventDefault();
    event.stopImmediatePropagation();

    _promptInProcess = true;

    let result = null;
    try {
      result = await _requestPromptProcessing(text);
    } catch (e) {
      console.error('[SecureDoc] 프롬프트 처리 오류:', e);
    }

    _promptInProcess = false;

    if (!result || result.action === 'cancel') {
      debugLog('[SecureDoc] 프롬프트 전송 취소');
      return;
    }

    const finalText = (result.action === 'masked' && result.maskedText)
      ? result.maskedText
      : text; // passthrough: 원본 그대로

    _promptApproved = true;

    const cfg2 = _getPromptConfig(); // DOM 변경 가능성 → 재취득
    if (cfg2) {
      _setEditorText(cfg2, finalText);
      await _reSubmitPrompt(cfg2);
    }

    // 재전송이 처리된 후 플래그 해제
    setTimeout(() => { _promptApproved = false; }, 3000);
  }

  // ── 이벤트 훅: 전송 버튼 클릭 (capture) ─────────────────────────────────────

  document.addEventListener('click', async (event) => {
    if (CONTENT_OWNS_PROMPTS) return;
    if (_promptApproved || _promptInProcess) return;
    const cfg = _getPromptConfig();
    if (!cfg?.sendBtnSel) return;

    // 클릭된 요소가 전송 버튼인지 확인
    let isBtn = false;
    for (const sel of cfg.sendBtnSel.split(',').map(s => s.trim())) {
      if (event.target.closest(sel)) { isBtn = true; break; }
    }
    if (!isBtn) return;

    // 비활성화 버튼 스킵
    const btn = event.target.closest('button, [role="button"]');
    if (btn?.disabled || btn?.getAttribute('aria-disabled') === 'true') return;

    await _interceptPromptSubmit(event, cfg);
  }, true);

  // ── 이벤트 훅: Enter 키 (capture) ────────────────────────────────────────────

  document.addEventListener('keydown', async (event) => {
    if (CONTENT_OWNS_PROMPTS) return;
    if (event.key !== 'Enter') return;
    if (event.shiftKey || event.ctrlKey || event.altKey || event.metaKey) return;
    if (_promptApproved || _promptInProcess) return;

    const cfg = _getPromptConfig();
    if (!cfg?.editorSel) return;

    const editor = document.querySelector(cfg.editorSel);
    if (!editor) return;

    // 에디터 또는 그 내부에 포커스가 있을 때만
    const active = document.activeElement;
    if (!editor.contains(active) && active !== editor) return;

    await _interceptPromptSubmit(event, cfg);
  }, true);

  // ── 이벤트 훅: form submit (capture) — ChatGPT 등 form 기반 폴백 ────────────

  document.addEventListener('submit', async (event) => {
    if (CONTENT_OWNS_PROMPTS) return;
    if (_promptApproved || _promptInProcess) return;
    const cfg = _getPromptConfig();
    if (!cfg) return;
    await _interceptPromptSubmit(event, cfg);
  }, true);

})();

