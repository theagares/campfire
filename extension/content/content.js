/**
 * content.js  ─  isolated world
 *
 * 역할 (PLAN §변경1 — 사이드패널 HITL):
 *   1. 사용자 제스처(전송 버튼 클릭 / Enter / 파일 선택·드롭·붙여넣기)를 캡처 단계에서
 *      가로챈다.
 *   2. 그 자리에서 **직접 chrome.sidePanel.open({tabId})** 을 호출해 사이드패널을 연다.
 *      (SW 경유 금지 — 제스처 소실 방지. content 컨텍스트에 sidePanel API 가 없을 때만
 *       SW 폴백.)  "패널 열기(제스처 필요)"와 "검사 수행(제스처 무관)"을 분리한다.
 *   3. 패널을 연 "이후에" SW(START_SCAN)를 거쳐 엔진 REST 로 PII/인젝션 검사를 수행한다.
 *   4. 사이드패널의 승인 결과(PANEL_DECISION)를 SW→content 로 받아, 파일 인풋 재주입
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
  let myTabId = null;

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

  // 제스처 시점에 동기적으로 쓰기 위해 tabId 를 미리(로드 시) 캐싱한다.
  try {
    chrome.runtime.sendMessage({ type: 'GET_TAB_ID' }, (res) => {
      if (chrome.runtime.lastError) return;
      if (res && typeof res.tabId === 'number') myTabId = res.tabId;
    });
  } catch (_) { /* context invalidated */ }

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
  // 사이드패널 열기 — SW 단일 홉 위임 (PLAN §변경1 핵심, 2026-07-23 실측 후 정정)
  //
  // (정정 경위) 원래는 "content 스크립트가 chrome.sidePanel.open()을 직접 호출"하는
  // 걸 1순위로 두고 SW 경유를 "제스처 소실 위험 있는 폴백"으로 취급했었다. 그런데
  // 실제 브라우저(Chrome 149)로 크로미움 익스텐션을 로드해 검증한 결과, chrome.sidePanel
  // 네임스페이스 자체가 content script(Isolated world) 컨텍스트엔 애초에 존재하지 않는다
  // (typeof chrome.sidePanel === 'undefined' 실측 확인 — 문서의 "content script에서
  // 여는 사용자 인터랙션" 문구는 "제스처가 content에서 시작돼도 된다"는 뜻이지 "API
  // 호출 자체를 content에서 한다"는 뜻이 아니었다). 즉 "직접 호출" 경로는 애초에
  // 실행 불가능한 죽은 코드였고, 반대로 "폴백"이라 불렀던 SW 단일 홉 경유가
  // Chromium 팀이 공식적으로 보장하는 유일하게 동작하는 경로임을 실측으로 확인했다
  // (메시지 왕복 1회, SW가 그 메시지 안에서 동기적으로 처리하면 제스처가 보존됨).
  // 그래서 SW 위임을 정식 1차 경로로 승격한다.
  // ══════════════════════════════════════════════════════════════════════════
  function openSidePanel() {
    try {
      const p = chrome.runtime.sendMessage({ type: 'OPEN_SIDE_PANEL', tabId: myTabId });
      if (p?.catch) p.catch(() => {});
    } catch (_) { /* context invalidated */ }
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
