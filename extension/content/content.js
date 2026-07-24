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
  async function reviewFileViaPanel(file) {
    if (!isSupportedFile(file) || contentProcessingFiles.has(file) || contentOwnedFiles.has(file)) return null;
    contentProcessingFiles.add(file);
    openSidePanel(); // 제스처 시점에 먼저 연다
    try {
      const base64Data = await fileToBase64(file);
      return await startPanelSession('file', {
        base64Data,
        mimeType: file.type || 'application/octet-stream',
        fileName: file.name,
        fileSize: file.size,
      });
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

  // ── 파일 인풋 change ────────────────────────────────────────────────────────
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

    const decision = await reviewFileViaPanel(file);
    const finalFile = await buildCurrentFileFromDecision(decision, file);
    if (finalFile) setFileOnInput(input, finalFile);
    else input.value = '';
  }, true);

  // ── 드래그앤드롭 ─────────────────────────────────────────────────────────────
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
    const decision = await reviewFileViaPanel(file);
    const finalFile = await buildCurrentFileFromDecision(decision, file);
    if (!finalFile) return;
    const dt = new DataTransfer();
    dt.items.add(finalFile);
    target.dispatchEvent(new DragEvent('drop', {
      bubbles: true, cancelable: true, composed: true,
      dataTransfer: dt, clientX: event.clientX, clientY: event.clientY,
    }));
  }, true);

  // ── 붙여넣기 ─────────────────────────────────────────────────────────────────
  document.addEventListener('paste', async (event) => {
    const files = Array.from(event.clipboardData?.files ?? []);
    const file = files.find(f => isSupportedFile(f) && !contentOwnedFiles.has(f) && !contentProcessingFiles.has(f));
    if (!file) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    const target = event.target;
    const decision = await reviewFileViaPanel(file);
    const finalFile = await buildCurrentFileFromDecision(decision, file);
    if (!finalFile) return;
    const dt = new DataTransfer();
    dt.items.add(finalFile);
    target.dispatchEvent(new ClipboardEvent('paste', {
      bubbles: true, cancelable: true, composed: true, clipboardData: dt,
    }));
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

    openSidePanel(); // 제스처 시점에 먼저 연다

    let decision = null;
    try {
      decision = await startPanelSession('prompt', { text });
    } catch (_) {
      decision = { action: 'cancel' };
    } finally {
      promptInProcess = false;
    }

    if (!decision || decision.action === 'cancel') return;
    const finalText = decision.action === 'masked' && decision.maskedText ? decision.maskedText : text;
    const latestCfg = getPromptConfig();
    if (!latestCfg) return;

    promptApproved = true;
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
