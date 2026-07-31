/**
 * content.js  ─  isolated world
 *
 * 역할 (PLAN §변경1 — 검토 패널 HITL, 2026-07-24 재정정):
 *   1. 사용자 제스처(전송 버튼 클릭 / Enter / 파일 선택·드롭·붙여넣기)를 캡처 단계에서
 *      가로챈다.
 *   2. 그 자리에서 검토 패널을 **직접 이 탭의 페이지 DOM에 iframe으로 주입**한다
 *      (아래 "검토 패널 열기" 섹션 참고 — chrome.sidePanel API 대신 이 방식을 쓰는
 *      이유는 그 섹션의 주석에 정리돼 있다).
 *   3. 패널을 연 "이후에" SW(START_SCAN)를 거쳐 엔진 REST 로 PII/인젝션 검사를 수행한다.
 *   4. 검토 패널의 승인 결과(PANEL_DECISION)를 SW→content 로 받아, 파일 인풋 재주입
 *      또는 interceptor(MAIN world)로의 SECUREDOC_RESULT / SECUREDOC_PROMPT_RESULT
 *      postMessage 로 마스킹본 치환·재전송을 트리거한다.
 *
 * interceptor.js(MAIN world) 의 네트워크 레이어(XHR/fetch/arrayBuffer) 훅은 그대로
 * 두고, 그쪽에서 오는 SECUREDOC_FILE_SELECTED(bridgeToken 검증) 도 같은 패널 흐름으로
 * 처리한다.
 */

(function () {
  'use strict';

  let protectionEnabled = true;

  const bridgeToken = (
    globalThis.crypto?.randomUUID?.()
    || `${Date.now()}-${Math.random().toString(36).slice(2)}`
  );

  function sendBridgeTokenToMain() {
    window.postMessage({
      __upsecurity_config: true,
      direction: 'isolated-to-main',
      type: 'SECUREDOC_BRIDGE_TOKEN',
      token: bridgeToken,
    }, '*');
  }

  function sendProtectionStateToMain(enabled) {
    window.postMessage({
      __upsecurity_config: true,
      direction: 'isolated-to-main',
      type: 'UPS_PROTECTION_STATE',
      enabled: Boolean(enabled),
    }, '*');
  }

  sendBridgeTokenToMain();

  chrome.storage?.local?.get?.({ protectionEnabled: true }, ({ protectionEnabled: enabled }) => {
    protectionEnabled = Boolean(enabled);
    sendBridgeTokenToMain();
    sendProtectionStateToMain(protectionEnabled);
  });

  chrome.storage?.onChanged?.addListener?.((changes, areaName) => {
    if (areaName !== 'local' || !changes.protectionEnabled) return;
    protectionEnabled = Boolean(changes.protectionEnabled.newValue);
    sendProtectionStateToMain(protectionEnabled);
  });

  const SUPPORTED_TYPES = new Set([
    'application/pdf',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  ]);
  const SUPPORTED_EXTS = /\.(pdf|docx)$/i;
  const contentOwnedFiles = new WeakSet();
  const contentProcessingFiles = new WeakSet();
  let promptInProcess = false;
  let promptApproved = false;

  function isSupportedFile(file) {
    if (!protectionEnabled || !file) return false;
    return SUPPORTED_TYPES.has(file.type) || SUPPORTED_EXTS.test(file.name || '');
  }

  async function fileToBase64(file) {
    const arr = new Uint8Array(await file.arrayBuffer());
    let bin = '';
    for (let i = 0; i < arr.length; i += 8192) bin += String.fromCharCode(...arr.subarray(i, i + 8192));
    return btoa(bin);
  }

  function base64ToFile(b64, mime, name) {
    const bin = atob(b64);
    const arr = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
    const file = new File([arr], name, { type: mime });
    contentOwnedFiles.add(file);
    return file;
  }

  function setFileOnInput(input, file) {
    const dt = new DataTransfer();
    dt.items.add(file);
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'files')?.set;
    if (setter) setter.call(input, dt.files);
    else input.files = dt.files;
    input._upsContentDone = true;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
  }

  // ══════════════════════════════════════════════════════════════════════════
  // 검토 패널 열기 — 페이지 DOM에 직접 iframe 오버레이 주입 (2026-07-24 재정정)
  //
  // (정정 경위) chrome.sidePanel API로 구현했었는데, 실사용 중 두 가지가 API 자체의
  // 구조적 한계로 확인됐다:
  //   1) 탭 스코핑이 불완전함 — 그 탭의 활성 상태를 setOptions({enabled:false})로
  //      끄더라도, "이미 창에 도킹되어 열린 패널"을 강제로 닫는 API가 없다
  //      (sidePanel.close() 자체가 없음 — W3C webextensions #521에서 계속 요청
  //      중인 미구현 기능: https://github.com/w3c/webextensions/issues/521).
  //   2) manifest의 side_panel.default_path가 "모든 탭에 기본으로 열린 전역 패널
  //      인스턴스"를 만들어버려 탭별 차단과 근본적으로 충돌한다
  //      (https://pmds.info/blog/chrome-extension-side-panel-per-tab).
  //
  // 대신 검토 패널을 이 탭의 페이지 DOM에 직접 iframe으로 주입하는 방식으로
  // 바꿨다 — 이러면:
  //   - iframe은 물리적으로 "이 탭의 DOM 안"에만 존재하므로 다른 탭엔 애초에
  //     나타날 수 없다(탭 스코핑 문제 자체가 소멸, 별도 enabled/disabled 관리 불요).
  //   - 폭/높이를 우리가 완전히 통제한다(브라우저가 정하는 사이드패널 독 폭에
  //     종속되지 않음).
  //   - DOM에서 제거하면 확실하게 닫힌다(닫기 API 부재 문제가 없음).
  //   - iframe의 src는 chrome-extension:// 오리진이라 그 안에서 로드되는
  //     sidepanel.html은 여전히 chrome.runtime 메시징 등 확장 권한을 그대로 쓴다
  //     (manifest web_accessible_resources에 sidepanel/* 노출 필요).
  //   - DOM 삽입 자체엔 사용자 제스처가 필요 없으므로(사이드패널 API 때와 달리)
  //     SW를 거칠 필요조차 없어졌다 — 제스처 시점에 바로, 동기적으로 주입한다.
  // ══════════════════════════════════════════════════════════════════════════
  let overlayRoot = null;
  let overlayIframe = null;

  function openSidePanel() {
    if (overlayIframe) return; // 이미 열려 있으면 그대로 재사용
    try {
      overlayRoot = document.createElement('div');
      overlayRoot.id = '__ups_overlay_host';
      overlayRoot.style.cssText = [
        'all: initial', 'position: fixed', 'top: 0', 'right: 0',
        'width: 560px', 'max-width: 92vw', 'height: 100vh',
        'z-index: 2147483647', 'box-shadow: -4px 0 24px rgba(0,0,0,.18)',
        'background: #fff',
      ].join(' !important; ') + ' !important;';

      overlayIframe = document.createElement('iframe');
      overlayIframe.src = chrome.runtime.getURL('sidepanel/sidepanel.html');
      overlayIframe.title = 'UpSecurity 문서 검토';
      overlayIframe.style.cssText = 'width: 100% !important; height: 100% !important; border: 0 !important; display: block !important;';

      overlayRoot.appendChild(overlayIframe);
      (document.documentElement || document.body).appendChild(overlayRoot);
    } catch (_) { /* context invalidated */ }
  }

  function closeSidePanel() {
    try { overlayRoot?.remove(); } catch (_) { /* ignore */ }
    overlayRoot = null;
    overlayIframe = null;
  }

  // 검토 패널(iframe) 자신이 결정 완료 후 닫아달라고 보내는 postMessage 수신.
  window.addEventListener('message', (event) => {
    if (event.source !== overlayIframe?.contentWindow) return;
    if (event.data?.type === 'UPS_CLOSE_OVERLAY') closeSidePanel();
  });

  // ══════════════════════════════════════════════════════════════════════════
  // 최근 첨부 문서 컨텍스트 — 결합(문서+프롬프트) 인젝션 판단용 캐시
  //
  // (2026-08-01 재정정) 이전엔 첨부한 문서를 스캔조차 하지 않고 프롬프트를 "보낼
  // 때"까지 완전히 보류했다가, 그 시점에야 (a) 문서를 스캔하고 (b) 승인 결과를
  // 원래 드롭/붙여넣기 지점에 합성 이벤트로 재주입했다. 그런데 이 보류 구간
  // 동안(프롬프트를 다 입력하는 시간 전체) 원래 드롭 대상 엘리먼트가 다른 상태로
  // 남아 있는다고 문제가 되진 않았다 — 실제로 크래시가 난 지점은 "그 오래된
  // target 참조에 합성 drop 이벤트를 재디스패치하는 순간"이었다. ChatGPT 같은
  // SPA는 프롬프트를 입력하는 동안 컴포저 주변 DOM을 다시 그리므로, 실제 드롭이
  // 일어난 시점과 완전히 다른(문서 첨부 당시엔 없었던 내용이 채워진) 상태에서
  // 뒤늦게 그 지점에 합성 드롭을 흘려보내면 사이트 쪽 드래그 상태 핸들러가 깨져
  // "this.drop is not a function" 크래시 + 드롭 오버레이 고착으로 이어졌다
  // (실사용자 재현 확인).
  //
  // 그래서 "문서 자체의 스캔+DOM 재주입"은 첨부되는 즉시 끝낸다(아래 change/drop/
  // paste 핸들러) — 이후 절대 그 DOM을 다시 건드리지 않는다. 다만 "이 문서가
  // 실제 프롬프트의 의도를 무시/변조하려는가"라는 결합 판단은 여전히 진짜
  // user_prompt 텍스트가 있어야 하므로, 첨부 시점에 이미 검토된 문서의 원본
  // 바이트만 여기 캐시해뒀다가 전송 시점에 프롬프트 텍스트와 함께 다시 한 번
  // "결합 스캔"(kind:'combined')을 돌린다 — 단, 그 결과의 file 필드는 절대 DOM
  // 재주입에 쓰지 않고 무시한다(문서는 이미 안전하게 붙어 있다).
  //
  // MVP 범위: 최근 첨부 1건만 추적한다(두 번째를 첨부하면 첫 번째를 교체).
  // ══════════════════════════════════════════════════════════════════════════
  const RECENT_DOC_CONTEXT_TTL_MS = 10 * 60 * 1000;
  let recentDocContext = null; // { base64Data, mimeType, fileName, fileSize, expiresAt }

  function setRecentDocContext(ctx) {
    recentDocContext = ctx ? { ...ctx, expiresAt: Date.now() + RECENT_DOC_CONTEXT_TTL_MS } : null;
  }

  function takeRecentDocContext() {
    const ctx = recentDocContext && recentDocContext.expiresAt > Date.now() ? recentDocContext : null;
    recentDocContext = null; // 결합 스캔에 쓰이든 프롬프트 취소로 버려지든, 한 번 쓰면 소모한다
    return ctx;
  }

  // ══════════════════════════════════════════════════════════════════════════
  // 패널 세션 — START_SCAN 후 PANEL_DECISION 을 기다린다
  // ══════════════════════════════════════════════════════════════════════════
  const pendingSessions = new Map(); // sessionId -> { resolve, timeout }

  function newSessionId() {
    return globalThis.crypto?.randomUUID?.()
      || `sess_${Date.now().toString(36)}_${Math.random().toString(36).slice(2)}`;
  }

  /** 사이드패널로 검사 요청을 보내고 사용자의 결정을 기다린다(10분 타임아웃). */
  function startPanelSession(kind, scanPayload) {
    const sessionId = newSessionId();
    return new Promise((resolve) => {
      const timeout = setTimeout(() => {
        if (!pendingSessions.has(sessionId)) return;
        pendingSessions.delete(sessionId);
        resolve({ action: 'cancel', reason: 'timeout' });
      }, 10 * 60 * 1000);
      pendingSessions.set(sessionId, { resolve, timeout });
      try {
        chrome.runtime.sendMessage({ type: 'START_SCAN', sessionId, kind, payload: scanPayload });
      } catch (e) {
        clearTimeout(timeout);
        pendingSessions.delete(sessionId);
        resolve({ action: 'cancel', reason: 'context' });
      }
    });
  }

  // SW → content : 사이드패널의 HITL 결정 수신
  chrome.runtime?.onMessage?.addListener((message) => {
    if (message?.type !== 'PANEL_DECISION') return;
    const entry = pendingSessions.get(message.sessionId);
    if (!entry) return;
    clearTimeout(entry.timeout);
    pendingSessions.delete(message.sessionId);
    entry.resolve(message.decision || { action: 'cancel' });
  });

  // ══════════════════════════════════════════════════════════════════════════
  // 파일 검토 (사이드패널 경로)
  // ══════════════════════════════════════════════════════════════════════════
  /** 문서 검토 요청 + 결정 대기. decision과 함께, 나중에 결합 스캔에 재사용할 수
   *  있도록 원본 파일의 base64/메타데이터도 함께 돌려준다. */
  async function reviewFileViaPanel(file) {
    if (!isSupportedFile(file) || contentProcessingFiles.has(file) || contentOwnedFiles.has(file)) return null;
    contentProcessingFiles.add(file);
    openSidePanel(); // 제스처 시점에 먼저 연다
    try {
      const mimeType = file.type || 'application/octet-stream';
      const fileName = file.name;
      const fileSize = file.size;
      const base64Data = await fileToBase64(file);
      const decision = await startPanelSession('file', { base64Data, mimeType, fileName, fileSize });
      return { decision, base64Data, mimeType, fileName, fileSize };
    } finally {
      contentProcessingFiles.delete(file);
    }
  }

  async function buildCurrentFileFromDecision(decision, originalFile) {
    if (!decision || decision.action === 'cancel' || decision.action === 'download') return null;
    if (decision.action === 'passthrough') {
      contentOwnedFiles.add(originalFile);
      return originalFile;
    }
    if (decision.action === 'upload' && decision.maskedBase64) {
      return base64ToFile(decision.maskedBase64, decision.mimeType, decision.fileName);
    }
    return null;
  }

  // ── 파일 인풋 change — 첨부 즉시 검토+재주입 ──────────────────────────────────
  document.addEventListener('change', async (event) => {
    const path = event.composedPath?.() ?? [];
    const input = path.find(el => el instanceof HTMLInputElement && el.type === 'file')
      ?? (event.target instanceof HTMLInputElement && event.target.type === 'file' ? event.target : null);
    if (!input) return;
    if (input._upsContentDone) { delete input._upsContentDone; return; }
    const file = input.files?.[0];
    if (!isSupportedFile(file) || contentOwnedFiles.has(file) || contentProcessingFiles.has(file)) return;

    event.preventDefault();
    event.stopImmediatePropagation();
    input.value = ''; // 사이트가 원본 파일을 보지 못하게 즉시 비운다(스캔 전 유출 방지)

    const review = await reviewFileViaPanel(file);
    const finalFile = await buildCurrentFileFromDecision(review?.decision, file);
    if (!finalFile) return;
    setFileOnInput(input, finalFile);
    setRecentDocContext(review); // 전송 시점 결합 스캔용 원본 바이트 캐시(위 설명 참고)
  }, true);

  // ── 드래그앤드롭 — 첨부 즉시 검토+재주입 ──────────────────────────────────────
  document.addEventListener('dragover', (event) => {
    if (Array.from(event.dataTransfer?.items ?? []).some(i => i.kind === 'file')) event.preventDefault();
  }, true);

  document.addEventListener('drop', async (event) => {
    const files = Array.from(event.dataTransfer?.files ?? []);
    const file = files.find(f => isSupportedFile(f) && !contentOwnedFiles.has(f) && !contentProcessingFiles.has(f));
    if (!file) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    const target = event.target;
    const clientX = event.clientX, clientY = event.clientY;

    const review = await reviewFileViaPanel(file);
    const finalFile = await buildCurrentFileFromDecision(review?.decision, file);
    if (!finalFile) return;

    const dt = new DataTransfer();
    dt.items.add(finalFile);
    // target(원래 드롭 지점 엘리먼트)이 검토 패널이 열려 있던 (보통 수 초 이내의)
    // 짧은 시간 동안 SPA 리렌더링으로 detached 됐을 가능성에 대비해 isConnected
    // 폴백 + try/catch는 유지한다 — 다만 이제는 "프롬프트 전송까지" 기다리지
    // 않고 검토가 끝나는 즉시 재주입하므로, 컴포저에 텍스트가 채워지며 DOM이
    // 완전히 바뀔 여지 자체가 없다(과거 크래시의 근본 원인, 위 "최근 첨부 문서
    // 컨텍스트" 섹션 참고).
    const dispatchTarget = target.isConnected ? target : document.body;
    try {
      dispatchTarget.dispatchEvent(new DragEvent('drop', {
        bubbles: true, cancelable: true, composed: true,
        dataTransfer: dt, clientX, clientY,
      }));
    } catch (e) {
      console.error('[SecureDoc] 마스킹본 재주입 drop 디스패치 실패:', e);
    }
    setRecentDocContext(review);
  }, true);

  // ── 붙여넣기 — 첨부 즉시 검토+재주입 ─────────────────────────────────────────
  document.addEventListener('paste', async (event) => {
    const files = Array.from(event.clipboardData?.files ?? []);
    const file = files.find(f => isSupportedFile(f) && !contentOwnedFiles.has(f) && !contentProcessingFiles.has(f));
    if (!file) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    const target = event.target;

    const review = await reviewFileViaPanel(file);
    const finalFile = await buildCurrentFileFromDecision(review?.decision, file);
    if (!finalFile) return;

    const dt = new DataTransfer();
    dt.items.add(finalFile);
    // drop 재주입과 동일한 이유(위 주석 참고)로 detached 폴백 + try/catch.
    const dispatchTarget = target.isConnected ? target : document.body;
    try {
      dispatchTarget.dispatchEvent(new ClipboardEvent('paste', {
        bubbles: true, cancelable: true, composed: true, clipboardData: dt,
      }));
    } catch (e) {
      console.error('[SecureDoc] 마스킹본 재주입 paste 디스패치 실패:', e);
    }
    setRecentDocContext(review);
  }, true);

  // ══════════════════════════════════════════════════════════════════════════
  // 프롬프트 인터셉트 (6개 사이트) — PLAN §변경3
  // ══════════════════════════════════════════════════════════════════════════
  const PROMPT_CONFIGS = {
    'chatgpt.com': {
      editorSel: '#prompt-textarea',
      sendBtnSel: '[data-testid="send-button"]',
      editorType: 'prosemirror',
    },
    'claude.ai': {
      editorSel: '[data-testid="chat-input"]',
      sendBtnSel: 'button[aria-label="메시지 보내기"], button[aria-label="Send message"]',
      editorType: 'prosemirror',
    },
    'gemini.google.com': {
      editorSel: '.ql-editor[role="textbox"]',
      sendBtnSel: 'button[aria-label="메시지 보내기"], button[aria-label="Send message"]',
      editorType: 'quill',
    },
    'grok.com': {
      editorSel: '[aria-label="Ask Grok anything"]',
      sendBtnSel: '[data-testid="chat-submit"]',
      editorType: 'prosemirror',
    },
    'perplexity.ai': {
      editorSel: 'textarea[placeholder], [contenteditable="true"][aria-label]',
      sendBtnSel: 'button[aria-label="Submit"]',
      editorType: 'lexical',
    },
    'copilot.microsoft.com': {
      editorSel: 'textarea, [contenteditable="true"]',
      sendBtnSel: 'button[aria-label="제출"], button[aria-label="Submit"]',
      editorType: 'unknown',
    },
  };

  function getPromptConfig() {
    const host = location.hostname;
    for (const [domain, cfg] of Object.entries(PROMPT_CONFIGS)) {
      if (host === domain || host.endsWith('.' + domain)) return cfg;
    }
    return null;
  }

  function getEditorText(cfg) {
    const editor = cfg?.editorSel && document.querySelector(cfg.editorSel);
    if (!editor) return '';
    if (editor.tagName === 'TEXTAREA') return editor.value.trim();
    return (editor.innerText || editor.textContent || '').trim();
  }

  function setEditorText(cfg, text) {
    const editor = cfg?.editorSel && document.querySelector(cfg.editorSel);
    if (!editor) return false;
    editor.focus();
    if (editor.tagName === 'TEXTAREA') {
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set;
      if (setter) setter.call(editor, text); else editor.value = text;
      editor.dispatchEvent(new Event('input', { bubbles: true }));
      editor.dispatchEvent(new Event('change', { bubbles: true }));
      return true;
    }
    try {
      document.execCommand('selectAll', false, null);
      document.execCommand('delete', false, null);
      document.execCommand('insertText', false, text);
    } catch (_) {
      editor.textContent = text;
    }
    editor.dispatchEvent(new InputEvent('input', { bubbles: true, data: text, inputType: 'insertText' }));
    if (cfg.editorType === 'lexical') {
      try {
        const dt = new DataTransfer();
        dt.setData('text/plain', text);
        editor.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true }));
      } catch (_) { /* ignore */ }
    }
    return true;
  }

  async function resubmitPrompt(cfg) {
    await new Promise(r => setTimeout(r, 200));
    for (const sel of (cfg.sendBtnSel || '').split(',').map(s => s.trim()).filter(Boolean)) {
      const btn = document.querySelector(sel);
      if (btn && !btn.disabled && btn.getAttribute('aria-disabled') !== 'true') { btn.click(); return; }
    }
    const editor = cfg.editorSel && document.querySelector(cfg.editorSel);
    if (editor) {
      editor.focus();
      editor.dispatchEvent(new KeyboardEvent('keydown', {
        key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true,
      }));
    }
  }

  async function interceptPromptSubmit(event, cfg) {
    if (!protectionEnabled || promptApproved || promptInProcess) return;
    const text = getEditorText(cfg);
    if (!text || text.length < 2) return;

    event.preventDefault();
    event.stopImmediatePropagation();
    promptInProcess = true;

    // 최근 첨부된 문서가 있으면 결합 검사(combined)로 "이 문서가 실제 프롬프트의
    // 의도를 무시/변조하려는가"까지 판단한다. 문서 자체는 첨부 시점에 이미 검토·
    // 재주입이 끝난 상태이므로, 여기서는 절대 DOM(파일 첨부)을 다시 건드리지
    // 않는다 — decision.file 은 통째로 무시하고 decision.maskedText(프롬프트
    // 쪽 결과)만 사용한다(위 "최근 첨부 문서 컨텍스트" 섹션 참고).
    const docCtx = takeRecentDocContext();
    openSidePanel(); // 제스처 시점에 먼저 연다

    let decision = null;
    try {
      decision = docCtx
        ? await startPanelSession('combined', {
            text,
            base64Data: docCtx.base64Data,
            mimeType: docCtx.mimeType,
            fileName: docCtx.fileName,
            fileSize: docCtx.fileSize,
          })
        : await startPanelSession('prompt', { text });
    } catch (_) {
      decision = { action: 'cancel' };
    } finally {
      promptInProcess = false;
    }

    if (!decision || decision.action === 'cancel') return;

    const latestCfg = getPromptConfig();
    if (!latestCfg) return;
    promptApproved = true;

    const finalText = docCtx
      ? (decision.maskedText || text) // combined 응답 형태: {action:'send', maskedText, file:{...}} — file은 사용하지 않음
      : (decision.action === 'masked' && decision.maskedText ? decision.maskedText : text);
    setEditorText(latestCfg, finalText);

    await resubmitPrompt(latestCfg);
    setTimeout(() => { promptApproved = false; }, 3000);
  }

  document.addEventListener('click', async (event) => {
    const cfg = getPromptConfig();
    if (!cfg?.sendBtnSel || promptApproved || promptInProcess) return;
    const isSendButton = cfg.sendBtnSel.split(',').map(s => s.trim()).some(sel => event.target.closest?.(sel));
    if (!isSendButton) return;
    const btn = event.target.closest?.('button, [role="button"]');
    if (btn?.disabled || btn?.getAttribute?.('aria-disabled') === 'true') return;
    await interceptPromptSubmit(event, cfg);
  }, true);

  document.addEventListener('keydown', async (event) => {
    if (event.key !== 'Enter' || event.shiftKey || event.ctrlKey || event.altKey || event.metaKey) return;
    const cfg = getPromptConfig();
    if (!cfg?.editorSel || promptApproved || promptInProcess) return;
    const editor = document.querySelector(cfg.editorSel);
    if (!editor) return;
    const active = document.activeElement;
    if (!editor.contains?.(active) && active !== editor) return;
    await interceptPromptSubmit(event, cfg);
  }, true);

  document.addEventListener('submit', async (event) => {
    const cfg = getPromptConfig();
    if (!cfg || promptApproved || promptInProcess) return;
    await interceptPromptSubmit(event, cfg);
  }, true);

  // ══════════════════════════════════════════════════════════════════════════
  // MAIN world(interceptor.js) 네트워크 레이어에서 온 파일 처리 요청
  //   (bridgeToken 검증 후) 동일한 사이드패널 흐름으로 처리 → SECUREDOC_RESULT 회신
  // ══════════════════════════════════════════════════════════════════════════
  window.addEventListener('message', async (event) => {
    if (event.source !== window) return;
    if (!event.data?.__securedoc || event.data.direction !== 'main-to-isolated') return;

    if (event.data.type === 'SECUREDOC_FILE_SELECTED') {
      if (event.data.bridgeToken !== bridgeToken) return; // 위조 메시지 차단
      const { inputId, base64Data, mimeType, fileName, fileSize } = event.data.payload;
      if (!protectionEnabled) { sendResultToMain({ inputId, action: 'passthrough' }); return; }

      openSidePanel();
      const decision = await startPanelSession('file', { base64Data, mimeType, fileName, fileSize });
      if (decision?.action === 'upload' && decision.maskedBase64) {
        sendResultToMain({
          inputId, action: 'upload',
          maskedBase64: decision.maskedBase64, mimeType: decision.mimeType, fileName: decision.fileName,
        });
      } else if (decision?.action === 'passthrough') {
        sendResultToMain({ inputId, action: 'passthrough' });
      } else {
        sendResultToMain({ inputId, action: 'cancel' });
      }
    }

    if (event.data.type === 'SECUREDOC_PROMPT_SELECTED') {
      if (event.data.bridgeToken !== bridgeToken) return;
      const { promptId, text } = event.data.payload;
      if (!protectionEnabled) { sendPromptResultToMain({ promptId, action: 'passthrough' }); return; }

      openSidePanel();
      const decision = await startPanelSession('prompt', { text });
      if (decision?.action === 'masked' && decision.maskedText) {
        sendPromptResultToMain({ promptId, action: 'masked', maskedText: decision.maskedText });
      } else if (decision?.action === 'passthrough') {
        sendPromptResultToMain({ promptId, action: 'passthrough' });
      } else {
        sendPromptResultToMain({ promptId, action: 'cancel' });
      }
    }
  });

  function sendResultToMain(payload) {
    window.postMessage({ __securedoc: true, direction: 'isolated-to-main', type: 'SECUREDOC_RESULT', payload }, '*');
  }
  function sendPromptResultToMain(payload) {
    window.postMessage({ __securedoc: true, direction: 'isolated-to-main', type: 'SECUREDOC_PROMPT_RESULT', payload }, '*');
  }
})();
