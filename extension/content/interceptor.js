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
  // content.js(isolated world)의 fileInterceptEnabled를 그대로 미러링한다 — 확장 팝업의
  // 보호 토글이 꺼지면 이 MAIN world 레이어(XHR/fetch 자동 감지)도 같이 꺼져야 한다.
  // 팝업은 두 값을 함께 끄지만, 여기서는 둘 다 확인한다(어느 한쪽만 꺼진 예전 설정이
  // 남아 있어도 안전하게 꺼진 쪽을 따르도록).
  let _fileInterceptEnabled = true;
  let _bridgeToken = '';

  window.addEventListener('message', (event) => {
    if (event.source !== window) return;
    if (!event.data?.__campfire_config || event.data.direction !== 'isolated-to-main') return;
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
    _fileInterceptEnabled = Boolean(event.data.fileInterceptEnabled);
    debugLog(`[SecureDoc] 보호 상태: ${_protectionEnabled ? 'ON' : 'OFF'}, 파일 인터셉트: ${_fileInterceptEnabled ? 'ON' : 'OFF'}`);
  });

  function isProtectionEnabled() {
    return _protectionEnabled;
  }

  function isFileInterceptEnabled() {
    return _fileInterceptEnabled;
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
    if (!isProtectionEnabled() || !isFileInterceptEnabled()) return false;
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
          throw new DOMException('Upload cancelled by campfire', 'AbortError');
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
    // 보호가 꺼져 있으면 preventDefault 조차 하지 않는다 — content.js 의 같은 훅과
    // 같은 이유다(끄면 개입 자체가 없어야 한다).
    if (!isProtectionEnabled() || !isFileInterceptEnabled()) return;
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

  // ══════════════════════════════════════════════════════════════════════════
  // 첨부 업로드 관측기 + 요청 추적(진단)
  //
  // 【역할 1】 관측기 — "지금 이 페이지가 파일을 올리고 있는가"를 isolated world 로
  // 브로드캐스트한다. content.js 가 마스킹본을 컴포저에 넣은 뒤, 사이트가 그 파일을
  // 자기 서버로 다 올릴 때까지 기다렸다가 전송 버튼을 누르기 위한 신호다.
  //
  // 【역할 2】 요청 추적 — 페이지가 어디로 무엇을 보내는지 링버퍼에 조용히 모아둔다.
  // 평소엔 아무것도 출력하지 않고, 첨부 대기가 "신호 없음"으로 끝났을 때(= 버그가
  // 재현된 순간)만 한 번 덤프한다.
  //
  // ── 왜 추적까지 넣었나 ────────────────────────────────────────────────────
  // 같은 증상(Gemini 에서 첨부보다 프롬프트가 먼저 나감)에 대해 신호를 세 번 바꿔
  // 달았고 세 번 다 빗나갔다: 전송 버튼 잠김 → 합성 drop → 네트워크 관측.
  // 마지막 핫패치 후 실사용자 콘솔은 이렇게 나왔다:
  //   [SecureDoc] 첨부 대기: 업로드 신호 없음 — 1500ms 동안 network/dom/button 셋 다 무반응…
  // 즉 관측기가 Gemini 의 업로드를 하나도 못 봤다. 그런데 첨부 자체는 성공한다
  // (사용자 확인: "첨부는 됐는데 요약하기가 먼저 들어갔어"). 파일은 분명히 올라가는데
  // 우리 눈에만 안 보인다는 뜻이다.
  //
  // 여기서 네 번째 신호를 또 찍어 넣는 건 같은 실수의 반복이다. 그래서 이번엔
  // "무엇이 실제로 일어나는가"를 사용자가 한 번에 복사해 보고할 수 있는 데이터로
  // 바꾼다. 아래 세 가설을 덤프 두 장(첨부 대기 창 / 전송 후)으로 가른다:
  //   H1 업로드는 주입 직후 일어나는데 우리가 못 보는 경로다(워커·다른 realm·다른 전송)
  //      → 대기 창의 perf 항목에는 업로드가 보이는데 fetch/xhr 항목에는 없다.
  //   H2 업로드가 "전송 시점"에 일어난다(= 전송 전엔 기다릴 업로드가 없다)
  //      → 대기 창은 비어 있고 전송 후 창에만 업로드가 찍힌다.
  //   H3 업로드가 주입보다 한참 늦게 시작한다
  //      → 우리 fetch/xhr 항목에 찍히되 1500ms 창 밖이다.
  // H2 로 판명되면 "업로드를 기다린다"는 접근 자체가 틀린 것이고, 붙잡아야 할 지점은
  // 컴포저가 첨부를 자기 상태에 반영했는지(= content.js 의 컴포저 DOM 변화 추적)로
  // 옮겨간다. 그 판단 근거도 이 덤프와 함께 나온다.
  //
  // ── 관측 범위와 사각지대 (중요) ───────────────────────────────────────────
  // 볼 수 있는 것:
  //   - 이 realm(document 컨텍스트)의 window.fetch / XMLHttpRequest
  //   - navigator.sendBeacon
  //   - WebSocket.send (4KB 이상 프레임만 — 채팅 스트림으로 콘솔이 터지지 않게)
  //   - Worker / ServiceWorker 로 "건네지는" 바이너리(postMessage 인자)
  //   - PerformanceObserver('resource') — 우리 훅을 우회한 요청도 잡힌다. 사이트가
  //     우리보다 먼저 pristine fetch 를 붙들어 뒀거나 <img>/EventSource 등으로 보내도
  //     document 의 리소스 타임라인에는 남기 때문이다. 요청 바디 크기는 알 수 없고
  //     URL·시각·응답 전송량만 나온다. (실측: sendBeacon 도 perf 에 beacon 으로 잡힌다.)
  //     ★ 읽는 법: fetch/xhr 줄 없이 perf 줄만 있는 요청 = 우리 훅을 우회한 경로.
  //   - 하위 프레임(all_frames)의 위 항목들 — 각 프레임의 interceptor 가 top 으로
  //     릴레이한다. 단 릴레이는 "진단 출력" 에만 쓰고, 전송을 막는 신호로는 절대 쓰지
  //     않는다(임의의 프레임이 우리 전송을 지연시킬 수 있으면 안 되므로).
  // 원리적으로 못 보는 것:
  //   - Dedicated Worker / Service Worker "안에서" 도는 fetch·XHR.
  //     ★ 헤드리스 Chrome 으로 실제로 재봤다: 워커가 Blob 40KB 를 fetch 로 올리는 동안
  //       fetch/xhr 훅은 물론 **PerformanceObserver 에도 그 요청이 한 줄도 안 남았다**
  //       (워커 스크립트 파일 자체를 받아오는 `/worker.js` 만 perf 에 찍혔다).
  //       워커는 별도 전역 스코프라 콘텐츠 스크립트가 그 안에 코드를 주입할 수 없고,
  //       워커가 만든 요청은 워커 자신의 리소스 타임라인에 기록되기 때문이다.
  //       → 사이트가 업로드를 워커로 옮겨 두었다면 우리가 볼 수 있는 유일한 흔적은
  //         "파일이 워커로 건너갔다"(Worker.postMessage)뿐이다. 그래서 그걸 잡는다.
  //         단 워커가 파일을 postMessage 가 아닌 다른 경로(blob: URL, IndexedDB,
  //         OPFS 등)로 가져가면 그 흔적조차 남지 않는다.
  //   - Service Worker 가 스스로 만들어 보내는 요청(페이지 fetch 를 가로채 다시
  //     보내는 경우가 아니라, SW 가 독자적으로 시작하는 요청).
  //   - 확장 프로그램 로드 전에 페이지가 이미 캡처해 둔 원본 함수로 보내는 요청
  //     (document_start 로 먼저 실행되지만 이론적 사각지대로 남겨둔다). 다만 이 경우도
  //     PerformanceObserver 에는 잡힌다.
  //
  // ── 데이터가 나오면 어디로 가는가 (H2 결론) ──────────────────────────────
  // 전송 후 창에만 업로드가 찍히면(H2), "전송 전에 업로드를 기다린다"는 접근은 성립할
  // 수 없다 — 기다릴 업로드가 그 시점에 존재하지 않기 때문이다. 그때의 올바른 게이트는
  // 네트워크가 아니라 **사이트가 첨부를 자기 컴포저 상태에 반영했는가**이고, 그건
  // content.js 의 컴포저 DOM 변화 추적으로 이미 재고 있다: 주입 후 컴포저 하위에
  // 노드가 추가되고(첨부 칩) 그 변화가 일정 시간 잠잠해지면 반영이 끝난 것으로 본다.
  // 선택자를 안 쓰는 규칙이라 사이트 개편에도 안 깨진다. 다만 이건 데이터를 보고
  // 결정할 일이라 지금 구현하지 않았다 — 이번 라운드의 산출물은 그 판단 근거다.
  //
  // ── 헤드리스 Chrome 실측 (임시 하니스 + 로컬 서버, 리포엔 커밋 안 함) ────────
  //   Google 스타일 resumable 업로드(start 빈 바디 → chunk 바이트)
  //        → goog:protocol=resumable / goog:command=start / 응답 goog:status=active,
  //          이어서 Blob(40000B) 청크와 응답 goog:status=final 까지 전부 한 줄씩 남음.
  //          업로드 티켓은 "바이트를 실은 청크" 에서만 열린다(start 는 37B 문자열이라 제외).
  //   XHR PUT + 청크 헤더    → goog:command / content-type 기록, 응답 goog:status=final
  //   워커 내부 fetch        → fetch/xhr/perf 어디에도 안 남음 (위 사각지대 항목 참고)
  //   Worker.postMessage(Blob) → 잡힘
  //   sendBeacon(Blob 8KB)   → 잡힘 (+ perf 에도 beacon 으로 중복 관측)
  //   작은 JSON fetch        → 추적엔 남고 업로드 티켓은 안 열림(오탐 없음)
  //   파일명 유출            → 바디 JSON 에 "secret-report.pdf" 를 넣고 보냈지만
  //                            로그에는 str(37B) 로만 남았다(내용·파일명 미기록 확인)
  //
  // ── 개인정보 ──────────────────────────────────────────────────────────────
  // 바디 "내용"은 어떤 경로로도 출력하지 않는다. 종류와 바이트 수만 찍는다.
  // 파일명도 확장자만 남기고 가린다(*.pdf). URL 은 호스트+경로까지만 쓰고 쿼리는
  // 파라미터 "이름"만 남긴다(값 제거). 헤더는 허용목록에 있는 것만, 그중 서명된 업로드
  // URL 처럼 민감할 수 있는 것은 값 대신 <있음> 으로만 표시한다.
  //
  // ── 기존 동작과의 관계 ────────────────────────────────────────────────────
  // 이 아래 훅들은 전부 기존 훅 "위에" 한 겹 더 감싼 것이다. 기록만 하고 요청은 그대로
  // 위임하므로 인터셉트 동작(차단·치환·보류)은 하나도 바뀌지 않는다. 사이트가
  // window.fetch / XHR.send 로 부르는 요청은 이 겉껍질을 반드시 지난다(안쪽 훅이
  // 마스킹본으로 바꿔 보내든 보류했다 보내든). 반대로 이 파일이 내부적으로
  // _origFetch/_origXHRSend 를 직접 부르는 경로는 세지 않는다 — 그건 MAIN world 가
  // 스스로 주도하는 흐름이라 content.js 가 기다리고 있지 않다.
  //
  // 위조 우려: 페이지 스크립트도 UPS_UPLOAD_ACTIVITY 를 흉내낼 수 있다. 그래도 유출로
  // 이어지지 않는다 — 이 시점의 파일은 이미 사용자 검토를 마친 마스킹본이고, 이 신호가
  // 할 수 있는 일은 우리 전송을 "더 기다리게" 하는 것뿐이다(상한이 있어 결국 전송된다).
  // 그래서 bridgeToken 을 요구하지 않는다.
  // ══════════════════════════════════════════════════════════════════════════

  // 요청이 끝났다는 신호를 영영 못 받는 경우(사이트가 취소했거나 우리 인터셉트가
  // 그 요청을 막은 경우)를 대비한 상한. content.js 쪽 대기 상한과 같은 자리수여야
  // "영원히 업로드 중" 으로 굳지 않는다.
  const _UPLOAD_TICKET_TTL_MS = 60_000;
  // 문자열 바디를 업로드로 볼 최소 길이. Grok 처럼 파일을 base64 로 JSON 에 실어
  // 보내는 사이트를 잡기 위한 것 — 일반 채팅 요청 JSON 이 이만큼 커지는 일은 없다.
  const _UPLOAD_STRING_BODY_MIN = 64 * 1024;
  const _openUploadTickets = new Set();
  let _uploadTicketSeq = 0;

  // ── 진단용 링버퍼 ─────────────────────────────────────────────────────────
  const _TRACE_MAX = 240;        // 보관 상한(오래된 것부터 버린다)
  const _TRACE_PRINT_MAX = 80;   // 한 번 덤프에 찍는 최대 줄 수
  const _WS_TRACE_MIN = 4096;    // 이보다 작은 WebSocket 프레임은 기록하지 않는다
  const _traceBuf = [];
  let _traceLive = false;        // __campfireTrace.live(true) 로 켜는 실시간 로깅

  const _isSubFrame = (() => { try { return window.top !== window; } catch (_) { return true; } })();

  // 값까지 찍어도 안전한 헤더(업로드 프로토콜 식별용). 문서 내용은 들어가지 않는다.
  const _TRACE_HEADERS = new Set([
    'content-type', 'content-length', 'content-range',
    'x-goog-upload-protocol', 'x-goog-upload-command', 'x-goog-upload-offset',
    'x-goog-upload-header-content-length', 'x-goog-upload-header-content-type',
    'x-goog-upload-status', 'x-goog-upload-url',
    'upload-offset', 'upload-length', 'tus-resumable',
  ]);
  // 있다는 사실만 남기고 값은 가리는 헤더(서명된 업로드 URL 등).
  const _TRACE_HEADERS_PRESENCE_ONLY = new Set(['x-goog-upload-url']);
  // 응답에서 읽을 헤더(업로드 프로토콜의 진행 상태). 위 허용목록에서 파생시켜 한 곳에서만
  // 관리한다 — 새 헤더를 추가할 때 두 군데를 고치다 하나를 빠뜨리는 일이 없게.
  const _TRACE_RESPONSE_HEADERS = [..._TRACE_HEADERS].filter(
    k => k.startsWith('x-goog-upload-') || k === 'upload-offset' || k === 'content-range',
  );

  /** XHR 응답에서 "실제로 읽을 수 있는" 헤더 이름 집합.
   *
   *  왜 필요한가: 교차 출처 응답이 Access-Control-Expose-Headers 로 노출하지 않은 헤더를
   *  getResponseHeader() 로 읽으려 하면 Chrome 이
   *      Refused to get unsafe header "x-goog-upload-status"
   *  를 콘솔에 찍는다. 이건 던져지는 예외가 아니라 브라우저가 직접 내는 경고라
   *  try/catch 로 막을 수 없고(값은 그냥 null 로 온다), 업로드와 무관한 XHR 에서도 뜬다.
   *  진단의 목적이 "사용자가 콘솔을 통째로 복사해 보내는 것"인데 정작 우리가 그 콘솔을
   *  오염시키게 된다(실사용자 리포트).
   *
   *  getAllResponseHeaders() 는 노출된 헤더만 돌려주고 그 자체로는 경고를 내지 않는다.
   *  그래서 여기 담긴 이름만 골라 읽으면 경고가 애초에 발생하지 않는다.
   *  (fetch 쪽은 다르다 — Response.headers 에는 노출된 헤더만 들어 있어서 없는 헤더를
   *   get() 해도 그냥 null 이고 경고가 없다. 헤드리스 Chrome 으로 확인했다.) */
  function _exposedResponseHeaderNames(xhr) {
    const names = new Set();
    try {
      for (const line of String(xhr.getAllResponseHeaders() || '').split(/\r?\n/)) {
        const i = line.indexOf(':');
        if (i > 0) names.add(line.slice(0, i).trim().toLowerCase());
      }
    } catch (_) { /* ignore */ }
    return names;
  }

  /** XHR·fetch 가 같은 규칙으로 응답 헤더를 읽도록 한 곳에 모은다.
   *  has(k) 가 true 인 헤더에 대해서만 read(k) 를 부른다 — 이 계약이 위 경고를 막는다. */
  function _collectResponseTraceHeaders(has, read) {
    const bits = [];
    for (const k of _TRACE_RESPONSE_HEADERS) {
      if (!has(k)) continue;
      let v = '';
      try { v = read(k) || ''; } catch (_) { v = ''; }
      if (!v) continue;
      const shown = _TRACE_HEADERS_PRESENCE_ONLY.has(k) ? '<있음>' : String(v).slice(0, 48);
      bits.push(`${k.replace(/^x-goog-upload-/, 'goog:')}=${shown}`);
    }
    return bits;
  }

  function _isCrossOrigin(u) {
    try { return new URL(String(u), location.href).origin !== location.origin; } catch (_) { return false; }
  }

  /** 호스트+경로까지만. 쿼리는 파라미터 "이름"만 남기고 값은 버린다. */
  function _shortUrl(u) {
    try {
      const url = new URL(String(u), location.href);
      const keys = [...url.searchParams.keys()].slice(0, 8);
      const qs = keys.length ? ` ?${keys.join(',')}` : '';
      let p = url.pathname;
      if (p.length > 90) p = p.slice(0, 87) + '...';
      return `${url.host}${p}${qs}`;
    } catch (_) {
      return String(u).slice(0, 90);
    }
  }

  /** 파일명은 확장자만 남긴다 — 문서 제목이 콘솔에 남지 않게. */
  function _redactName(name) {
    const m = String(name || '').match(/\.([A-Za-z0-9]{1,8})$/);
    return m ? `*.${m[1]}` : '*';
  }

  /** 바디의 "종류와 크기"만 만든다. 내용은 어떤 경우에도 읽지 않는다. */
  function _describeBody(body) {
    if (body === null || body === undefined || body === '') return '-';
    try {
      if (typeof body === 'string') return `str(${body.length}B)`;
      if (body instanceof FormData) {
        const parts = [];
        let i = 0;
        for (const [k, v] of body.entries()) {   // entries() 는 비파괴적이다
          if (i++ >= 8) { parts.push('...'); break; }
          if (v instanceof File) parts.push(`${k}=File(${_redactName(v.name)},${v.size}B,${v.type || '?'})`);
          else if (v instanceof Blob) parts.push(`${k}=Blob(${v.size}B,${v.type || '?'})`);
          else parts.push(`${k}=str(${String(v).length}B)`);
        }
        return `FormData{${parts.join(' ')}}`;
      }
      if (body instanceof File) return `File(${_redactName(body.name)},${body.size}B,${body.type || '?'})`;
      if (body instanceof Blob) return `Blob(${body.size}B,${body.type || '?'})`;
      if (body instanceof ArrayBuffer) return `ArrayBuffer(${body.byteLength}B)`;
      if (ArrayBuffer.isView?.(body)) return `TypedArray(${body.byteLength}B)`;
      if (typeof URLSearchParams !== 'undefined' && body instanceof URLSearchParams) {
        return `URLSearchParams{${[...body.keys()].slice(0, 8).join(',')}}`;
      }
      if (typeof ReadableStream !== 'undefined' && body instanceof ReadableStream) return 'ReadableStream(크기미상)';
      return `${body?.constructor?.name || typeof body}(?)`;
    } catch (_) {
      return '?';
    }
  }

  /** postMessage 인자에 바이너리가 실렸는지 얕게 본다(워커로 파일이 넘어갔는지 확인). */
  function _binaryKindOf(msg, depth = 0) {
    try {
      if (!msg) return null;
      if (msg instanceof Blob || msg instanceof ArrayBuffer || ArrayBuffer.isView(msg)) return _describeBody(msg);
      if (depth >= 2) return null;
      if (Array.isArray(msg)) {
        for (const v of msg.slice(0, 12)) { const k = _binaryKindOf(v, depth + 1); if (k) return k; }
        return null;
      }
      if (typeof msg === 'object') {
        for (const v of Object.values(msg).slice(0, 24)) { const k = _binaryKindOf(v, depth + 1); if (k) return k; }
      }
    } catch (_) { /* ignore */ }
    return null;
  }

  function _pickHeaders(h) {
    const out = {};
    try {
      if (!h) return out;
      const put = (k, v) => {
        const key = String(k).toLowerCase();
        if (!_TRACE_HEADERS.has(key)) return;
        out[key] = _TRACE_HEADERS_PRESENCE_ONLY.has(key) ? '<있음>' : String(v).slice(0, 48);
      };
      if (typeof Headers !== 'undefined' && h instanceof Headers) h.forEach((v, k) => put(k, v));
      else if (Array.isArray(h)) for (const pair of h) put(pair?.[0], pair?.[1]);
      else for (const [k, v] of Object.entries(h)) put(k, v);
    } catch (_) { /* ignore */ }
    return out;
  }

  function _fmtHeaders(h) {
    const e = Object.entries(h || {});
    return e.length ? e.map(([k, v]) => `${k.replace(/^x-goog-upload-/, 'goog:')}=${v}`).join(' ') : '';
  }

  function _traceLine(r, t0) {
    const dt = ((r.t - t0) / 1000).toFixed(2);
    const via = String(r.via || '?').padEnd(7);
    const method = String(r.method || '').padEnd(5);
    return `[SecureDoc][진단] +${dt}s ${via} ${method} ${r.path}  body=${r.body || '-'}${r.extra ? '  ' + r.extra : ''}`;
  }

  function _traceAdd(rec) {
    try {
      rec.t = Date.now();
      _traceBuf.push(rec);
      while (_traceBuf.length > _TRACE_MAX) _traceBuf.shift();
      if (_traceLive) console.log(_traceLine(rec, rec.t));
      // 하위 프레임이면 top 으로 올려보내 한자리에서 볼 수 있게 한다(진단 전용).
      if (_isSubFrame) {
        window.top.postMessage({
          __campfire_config: true, direction: 'frame-to-top', type: 'UPS_TRACE_RELAY',
          rec: { ...rec, frame: location.host },
        }, '*');
      }
    } catch (_) { /* 추적은 절대 본류를 깨뜨리면 안 된다 */ }
  }

  /** 추적 대상에서 뺄 잡음(광고·애널리틱스·로그 비컨). */
  function _traceSkip(url) {
    try { return _SILENT_BLOCK_RE.test(url) || _PASSTHROUGH_RE.test(url); } catch (_) { return false; }
  }

  function _printTrace(label, windowMs) {
    const span = Number(windowMs) || 10000;
    const since = Date.now() - span;
    const rows = _traceBuf.filter(r => r.t >= since);
    console.log(`[SecureDoc][진단] ===== 요청 추적 · ${label} · 최근 ${(span / 1000).toFixed(1)}초 · ${rows.length}건 =====`);
    if (!rows.length) {
      console.log('[SecureDoc][진단] (이 구간에 페이지가 보낸 요청이 하나도 관측되지 않았습니다)');
    } else {
      const shown = rows.slice(-_TRACE_PRINT_MAX);
      if (shown.length < rows.length) {
        console.log(`[SecureDoc][진단] (앞부분 ${rows.length - shown.length}건 생략)`);
      }
      const t0 = shown[0].t;
      for (const r of shown) console.log(_traceLine(r, t0));
    }
    console.log('[SecureDoc][진단] ===== 여기까지 통째로 복사해 주세요 (바디 내용은 기록하지 않습니다) =====');
  }

  function _broadcastUploadActivity(phase) {
    try {
      window.postMessage({
        __campfire_config: true,
        direction: 'main-to-isolated',
        type: 'UPS_UPLOAD_ACTIVITY',
        phase,
        inflight: _openUploadTickets.size,
      }, '*');
    } catch (_) { /* ignore */ }
  }

  function _beginUploadTicket() {
    const id = ++_uploadTicketSeq;
    _openUploadTickets.add(id);
    _broadcastUploadActivity('start');
    setTimeout(() => _endUploadTicket(id), _UPLOAD_TICKET_TTL_MS);
    return id;
  }

  function _endUploadTicket(id) {
    if (!_openUploadTickets.delete(id)) return; // 이미 닫힘(중복 호출 방지)
    _broadcastUploadActivity('end');
  }

  function _bodyLooksLikeUpload(body) {
    if (!body) return false;
    try {
      if (body instanceof Blob) return body.size > 0;
      if (body instanceof FormData) {
        for (const v of body.values()) if (v instanceof Blob) return true;
        return false;
      }
      if (body instanceof ArrayBuffer) return body.byteLength >= _UPLOAD_STRING_BODY_MIN;
      if (ArrayBuffer.isView?.(body)) return body.byteLength >= _UPLOAD_STRING_BODY_MIN;
      if (typeof body === 'string') return body.length >= _UPLOAD_STRING_BODY_MIN;
    } catch (_) { /* 이상한 바디는 업로드로 보지 않는다 */ }
    return false;
  }

  /** fetch(new Request(url, {body})) 처럼 init.body 가 비어 있는 형태까지 본다.
   *  Request 의 body 는 스트림이라 크기를 직접 못 재므로 content-length 로 판단하고,
   *  그것마저 없으면 업로드로 치지 않는다(모든 POST 를 업로드로 오인하지 않도록). */
  function _fetchLooksLikeUpload(input, init) {
    const body = init?.body ?? null;
    if (body != null) return _bodyLooksLikeUpload(body);
    try {
      if (typeof Request !== 'undefined' && input instanceof Request && input.body) {
        return Number(input.headers?.get?.('content-length') || 0) >= _UPLOAD_STRING_BODY_MIN;
      }
    } catch (_) { /* ignore */ }
    return false;
  }

  // ── XHR ───────────────────────────────────────────────────────────────────
  const _xhrTrace = new WeakMap();

  const _observedXHROpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    try { _xhrTrace.set(this, { method: String(method || 'GET').toUpperCase(), url: String(url), headers: {} }); } catch (_) { /* ignore */ }
    return _observedXHROpen.call(this, method, url, ...rest);
  };

  const _observedXHRSetHeader = XMLHttpRequest.prototype.setRequestHeader;
  XMLHttpRequest.prototype.setRequestHeader = function (name, value) {
    try {
      const st = _xhrTrace.get(this);
      const k = String(name).toLowerCase();
      if (st && _TRACE_HEADERS.has(k)) {
        st.headers[k] = _TRACE_HEADERS_PRESENCE_ONLY.has(k) ? '<있음>' : String(value).slice(0, 48);
      }
    } catch (_) { /* ignore */ }
    return _observedXHRSetHeader.call(this, name, value);
  };

  const _observedXHRSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function (body) {
    const st = _xhrTrace.get(this) || {};
    const url = st.url || '';
    let rec = null;
    if (!_traceSkip(url)) {
      rec = { via: 'xhr', method: st.method || 'POST', path: _shortUrl(url), body: _describeBody(body), extra: _fmtHeaders(st.headers) };
      _traceAdd(rec);
    }
    const id = _bodyLooksLikeUpload(body) ? _beginUploadTicket() : 0;
    // 이 요청이 "업로드처럼 보이는가" — 응답 헤더를 못 읽었을 때 그 사실을 남길지
    // 판단하는 데만 쓴다(모든 교차 출처 응답에 주석을 달면 그게 곧 잡음이므로).
    const uploadish = !!id || Object.keys(st.headers || {}).some(
      k => k.startsWith('x-goog-upload-') || k === 'upload-offset' || k === 'content-range',
    );
    const crossOrigin = _isCrossOrigin(url);
    // loadend 는 성공/실패/abort 어느 쪽으로 끝나도 발생한다. 우리 훅이 요청을
    // 보류했다가 나중에 보내는 경우에도, 실제로 끝나는 그 시점에 닫힌다.
    try {
      this.addEventListener('loadend', () => {
        if (id) _endUploadTicket(id);
        if (!rec) return;
        try {
          const bits = [`→${this.status}`, `${Date.now() - rec.t}ms`];
          // 노출된 헤더만 골라 읽는다 — 안 그러면 Chrome 이
          // 'Refused to get unsafe header' 를 콘솔에 찍어 진단 출력을 오염시킨다
          // (_exposedResponseHeaderNames 주석 참고).
          const exposed = _exposedResponseHeaderNames(this);
          const found = _collectResponseTraceHeaders((k) => exposed.has(k), (k) => this.getResponseHeader(k));
          bits.push(...found);
          // "노출이 안 돼 못 읽었다" 와 "값이 없다" 는 진단상 의미가 다르다.
          if (!found.length && uploadish && crossOrigin) bits.push('업로드헤더=읽을수없음(CORS 미노출)');
          _traceAdd({ via: 'xhr↩', method: rec.method, path: rec.path, body: '-', extra: bits.join(' ') });
        } catch (_) { /* ignore */ }
      }, { once: true });
    } catch (_) {
      if (id) _endUploadTicket(id);
    }
    try {
      return _observedXHRSend.call(this, body);
    } catch (e) {
      if (id) _endUploadTicket(id);
      throw e;
    }
  };

  // ── fetch ─────────────────────────────────────────────────────────────────
  const _observedFetch = window.fetch;
  window.fetch = function (input, init) {
    let rec = null;
    try {
      const isReq = typeof Request !== 'undefined' && input instanceof Request;
      const url = isReq ? input.url : String(input?.url ?? input);
      if (!_traceSkip(url)) {
        const body = init?.body ?? null;
        let bodyDesc = _describeBody(body);
        if (body == null && isReq) bodyDesc = input.body ? 'Request(스트림, 크기미상)' : '-';
        rec = {
          via: 'fetch',
          method: String(init?.method ?? (isReq ? input.method : 'GET')).toUpperCase(),
          path: _shortUrl(url),
          body: bodyDesc,
          extra: _fmtHeaders(_pickHeaders(init?.headers ?? (isReq ? input.headers : null))),
        };
        _traceAdd(rec);
      }
    } catch (_) { /* ignore */ }

    const id = _fetchLooksLikeUpload(input, init) ? _beginUploadTicket() : 0;
    const uploadish = !!id || /goog:|upload/i.test(rec?.extra || '');
    let out;
    try {
      out = _observedFetch.call(this, input, init);
    } catch (e) {
      if (id) _endUploadTicket(id);
      throw e;
    }
    return Promise.resolve(out).then(
      (res) => {
        if (id) _endUploadTicket(id);
        if (rec) {
          try {
            const bits = [`→${res?.status}`, `${Date.now() - rec.t}ms`];
            // fetch 는 XHR 과 달리 노출 안 된 헤더를 get() 해도 경고가 없다(헤드리스
            // Chrome 확인). 그래도 has()→get() 순서와 허용목록은 XHR 과 같은 함수를
            // 써서 규칙이 한 곳에만 있게 한다.
            const found = _collectResponseTraceHeaders(
              (k) => { try { return !!res?.headers?.has?.(k); } catch (_) { return false; } },
              (k) => res.headers.get(k),
            );
            bits.push(...found);
            if (!found.length && uploadish && res?.type === 'cors') bits.push('업로드헤더=읽을수없음(CORS 미노출)');
            _traceAdd({ via: 'fetch↩', method: rec.method, path: rec.path, body: '-', extra: bits.join(' ') });
          } catch (_) { /* ignore */ }
        }
        return res;
      },
      (err) => {
        if (id) _endUploadTicket(id);
        if (rec) _traceAdd({ via: 'fetch↩', method: rec.method, path: rec.path, body: '-', extra: `→실패 ${Date.now() - rec.t}ms` });
        throw err;
      },
    );
  };

  // ── sendBeacon ────────────────────────────────────────────────────────────
  // 기록만 한다. 비컨은 완료 신호가 없어서(항상 fire-and-forget) 대기 게이트로는
  // 쓸 수 없다 — "언제 끝났는지"를 알 방법이 원리적으로 없다.
  try {
    if (typeof navigator !== 'undefined' && typeof navigator.sendBeacon === 'function') {
      const _origBeacon = navigator.sendBeacon.bind(navigator);
      navigator.sendBeacon = function (url, data) {
        if (!_traceSkip(String(url))) {
          _traceAdd({ via: 'beacon', method: 'POST', path: _shortUrl(url), body: _describeBody(data), extra: '완료 신호 없음' });
        }
        return _origBeacon(url, data);
      };
    }
  } catch (_) { /* ignore */ }

  // ── WebSocket (큰 프레임만) ───────────────────────────────────────────────
  try {
    if (typeof WebSocket !== 'undefined' && WebSocket.prototype?.send) {
      const _origWSSend = WebSocket.prototype.send;
      WebSocket.prototype.send = function (data) {
        try {
          const size = typeof data === 'string' ? data.length : (data?.byteLength ?? data?.size ?? 0);
          if (size >= _WS_TRACE_MIN) {
            _traceAdd({ via: 'ws', method: 'SEND', path: _shortUrl(this.url || '(ws)'), body: _describeBody(data), extra: '' });
          }
        } catch (_) { /* ignore */ }
        return _origWSSend.call(this, data);
      };
    }
  } catch (_) { /* ignore */ }

  // ── Worker / ServiceWorker 로 넘어가는 바이너리 ───────────────────────────
  // 워커 "안에서" 도는 fetch·XHR 은 못 본다(별도 전역 스코프라 주입 불가, 워커의
  // performance 타임라인도 document 에서 조회 불가). 파일이 워커로 넘어갔다는 사실만
  // 잡아서, 업로드가 그쪽으로 옮겨갔는지 판단할 근거를 남긴다.
  try {
    for (const [ctor, tag] of [[typeof Worker !== 'undefined' ? Worker : null, 'worker'],
                               [typeof ServiceWorker !== 'undefined' ? ServiceWorker : null, 'sw']]) {
      if (!ctor?.prototype?.postMessage) continue;
      const orig = ctor.prototype.postMessage;
      ctor.prototype.postMessage = function (msg, ...rest) {
        try {
          const kind = _binaryKindOf(msg);
          if (kind) {
            _traceAdd({ via: tag, method: 'POST', path: `(${tag}.postMessage)`, body: kind, extra: '워커 내부 업로드는 관측 불가' });
          }
        } catch (_) { /* ignore */ }
        return orig.call(this, msg, ...rest);
      };
    }
  } catch (_) { /* ignore */ }

  // ── PerformanceObserver — 우리 훅을 우회한 요청까지 본다 ──────────────────
  // 요청 바디는 알 수 없지만 "그 시각에 그 URL 로 요청이 나갔다"는 사실은 남는다.
  // fetch/xhr 줄 없이 perf 줄만 있는 요청 = 우리가 못 보는 경로로 나간 것이다.
  try {
    if (typeof PerformanceObserver === 'function') {
      const po = new PerformanceObserver((list) => {
        for (const e of list.getEntries()) {
          try {
            const it = String(e.initiatorType || '');
            if (it === 'img' || it === 'css' || it === 'script' || it === 'link' || it === 'font') continue;
            const name = String(e.name || '');
            if (_traceSkip(name)) continue;
            _traceAdd({
              via: 'perf', method: it || 'res', path: _shortUrl(name), body: '-',
              extra: `dur=${Math.round(e.duration)}ms ↓${e.transferSize || 0}B`,
            });
          } catch (_) { /* ignore */ }
        }
      });
      po.observe({ type: 'resource', buffered: false });
    }
  } catch (_) { /* ignore */ }

  // ── isolated world 의 덤프 요청 / 하위 프레임 릴레이 수신 ─────────────────
  window.addEventListener('message', (e) => {
    const d = e.data;
    if (!d?.__campfire_config) return;
    if (e.source !== window) {
      // 하위 프레임에서 올라온 진단 릴레이. 출력용 버퍼에만 넣는다 — 전송을 막는
      // 신호(UPS_UPLOAD_ACTIVITY)로는 절대 승격시키지 않는다. 임의의 프레임(광고 등)이
      // 우리 전송을 지연시킬 수 있으면 안 되기 때문이다.
      if (d.direction === 'frame-to-top' && d.type === 'UPS_TRACE_RELAY' && d.rec) {
        try {
          _traceBuf.push({
            via: `f:${String(d.rec.via || '?').slice(0, 5)}`,
            method: String(d.rec.method || '').slice(0, 8),
            path: String(d.rec.path || '').slice(0, 120),
            body: String(d.rec.body || '-').slice(0, 80),
            extra: String(d.rec.extra || '').slice(0, 80),
            t: Number(d.rec.t) || Date.now(),
          });
          while (_traceBuf.length > _TRACE_MAX) _traceBuf.shift();
        } catch (_) { /* ignore */ }
      }
      return;
    }
    if (d.direction !== 'isolated-to-main') return;
    if (d.type === 'UPS_TRACE_PRINT') _printTrace(String(d.label || '진단'), Number(d.windowMs) || 10000);
  });

  // ── 수동 조작구 (MAIN world 이므로 페이지 콘솔에서 바로 부를 수 있다) ─────
  //   __campfireTrace()          최근 60초 덤프
  //   __campfireTrace(15000)     최근 15초 덤프
  //   __campfireTrace.live(true) 요청이 나갈 때마다 실시간 출력
  //   __campfireTrace.clear()    버퍼 비우기
  try {
    const api = (windowMs) => {
      _printTrace('수동 요청', Number(windowMs) || 60000);
      return `${_traceBuf.length}건 보관 중`;
    };
    api.live = (on = true) => {
      _traceLive = !!on;
      console.log(`[SecureDoc][진단] 실시간 요청 로깅 ${_traceLive ? 'ON' : 'OFF'}`);
      return _traceLive;
    };
    api.clear = () => { _traceBuf.length = 0; return '비웠습니다'; };
    Object.defineProperty(window, '__campfireTrace', { value: api, configurable: true, enumerable: false });
  } catch (_) { /* ignore */ }

})();

