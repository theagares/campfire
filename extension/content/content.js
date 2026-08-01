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

  /** 사이트가 파일 선택(📎) 버튼용으로 이미 갖고 있는 숨은 input[type=file]을 찾는다
   *  — drop/paste로 들어온 파일도 이 input을 통해 "새로 파일을 선택한 것"처럼
   *  흘려보내기 위함(아래 "드래그앤드롭/붙여넣기 재주입" 섹션 참고). target이 속했던
   *  form을 먼저 보고(재주입 시점엔 target이 detached일 수 있어 best-effort), 없으면
   *  문서 전체에서 찾는다. */
  function findFileInput(target) {
    return target?.closest?.('form')?.querySelector?.('input[type="file"]')
      ?? document.querySelector('input[type="file"]')
      ?? null;
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
  // 문서 첨부 보류(pending) — 인젝션 탐지 재설계
  //
  // 문서를 첨부한 즉시 스캔하지 않고, 사용자가 프롬프트를 "보낼 때"까지 보류했다가
  // 프롬프트 텍스트와 함께 한 번에 넘긴다. 인젝션 탐지 모델이 "이 문서가 사용자의
  // 실제 지시를 무시/변조하려는가"를 판단하려면 진짜 user_prompt 가 필요한데,
  // 첨부 시점엔 그 프롬프트가 아직 존재하지 않기 때문이다(engine 쪽
  // orchestrator.run_pipeline(user_prompt=...) / POST /jobs 의 userPrompt 필드 참고).
  //
  // MVP 범위: 보류 중인 첨부는 최대 1개만 추적한다(두 번째를 첨부하면 첫 번째를
  // 교체) — 여러 파일을 동시에 보류·결합하는 건 다음 단계.
  // ══════════════════════════════════════════════════════════════════════════
  let pendingAttachment = null; // { file, base64Data, mimeType, fileName, fileSize, inject(finalFile) }
  let badgeRoot = null;

  function showPendingBadge(fileName) {
    hidePendingBadge();
    try {
      badgeRoot = document.createElement('div');
      badgeRoot.id = '__ups_pending_badge';
      badgeRoot.style.cssText = [
        'all: initial', 'position: fixed', 'right: 16px', 'bottom: 16px',
        'z-index: 2147483646', 'background: #1f2430', 'color: #fff',
        'font: 12px/1.4 -apple-system, BlinkMacSystemFont, sans-serif', 'padding: 9px 12px',
        'border-radius: 10px', 'box-shadow: 0 4px 16px rgba(0,0,0,.25)',
        'display: flex', 'align-items: center', 'gap: 8px', 'max-width: 320px',
      ].join(' !important; ') + ' !important;';

      const label = document.createElement('span');
      label.textContent = `📎 ${fileName} 대기 중 — 프롬프트 전송 시 함께 검사됩니다`;
      label.style.cssText = 'flex: 1 !important; overflow: hidden !important; text-overflow: ellipsis !important; white-space: nowrap !important;';

      const closeBtn = document.createElement('button');
      closeBtn.type = 'button';
      closeBtn.textContent = '✕';
      closeBtn.title = '첨부 취소';
      closeBtn.style.cssText = 'all: unset !important; cursor: pointer !important; opacity: .75 !important; padding: 0 2px !important; flex-shrink: 0 !important;';
      closeBtn.addEventListener('click', () => { clearPendingAttachment(); });

      badgeRoot.appendChild(label);
      badgeRoot.appendChild(closeBtn);
      (document.documentElement || document.body).appendChild(badgeRoot);
    } catch (_) { /* context invalidated */ }
  }

  function hidePendingBadge() {
    try { badgeRoot?.remove(); } catch (_) { /* ignore */ }
    badgeRoot = null;
  }

  function clearPendingAttachment() {
    pendingAttachment = null;
    hidePendingBadge();
  }

  /** 파일을 즉시 스캔하지 않고 보류 상태로 저장한다. inject(finalFile)은 나중에
   *  마스킹된(또는 원본) 파일을 원래 있어야 할 자리(입력창/드롭 타깃)에 넣는 방법을
   *  호출부가 정의해 넘긴다(input.files 세터 vs 합성 drop/paste 이벤트 등, 첨부
   *  경로마다 다르므로). */
  async function stageFileAttachment(file, inject) {
    if (!isSupportedFile(file) || contentProcessingFiles.has(file) || contentOwnedFiles.has(file)) return;
    const base64Data = await fileToBase64(file);
    pendingAttachment = {
      file,
      base64Data,
      mimeType: file.type || 'application/octet-stream',
      fileName: file.name,
      fileSize: file.size,
      inject,
    };
    showPendingBadge(file.name);
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

  // ── 파일 인풋 change — 즉시 스캔하지 않고 보류(위 "문서 첨부 보류" 참고) ──────
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

    await stageFileAttachment(file, (finalFile) => setFileOnInput(input, finalFile));
  }, true);

  // ── 드래그앤드롭 — 즉시 스캔하지 않고 보류 ───────────────────────────────────
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

    await stageFileAttachment(file, (finalFile) => {
      // (2026-08-01 재정정) 원래는 드롭 지점(target)에 합성 drop 이벤트를 재생해
      // 재주입했다. 하지만 target은 검토 패널에서 승인될 때까지(수 초~수십 초, 길게는
      // 프롬프트를 다 입력할 때까지) 지난 뒤에야 이 콜백에 도달하는데, 그 사이 SPA가
      // 컴포저 주변을 다시 그려버리면 드롭 재생이 의존하는 사이트의 내부 드래그
      // 상태 머신 자체가 사라져 있을 수 있다(실측: ChatGPT에서 "this.drop is not a
      // function" 크래시 + 드롭 오버레이 고착 재현). 드래그 제스처를 흉내내는 대신,
      // 아예 원래 drop 이벤트를 취소해버리고 사이트가 이미 갖고 있는 파일 선택
      // input[type=file]에 "새로 파일을 선택한 것"처럼 흘려보낸다 — 이 경로는
      // change 이벤트 하나로 끝나며 드래그 상태와 전혀 무관하다.
      const input = findFileInput(target);
      if (input) { setFileOnInput(input, finalFile); return; }

      // 폴백: 이 사이트에 파일 선택 input이 따로 없는 경우에만 기존 방식(합성 drop
      // 재생)을 시도한다. isConnected로 detached 여부를 확인해 document.body로
      // 폴백하고, 사이트 쪽 핸들러 예외가 우리 흐름을 끊지 않도록 try/catch로 감싼다.
      const dt = new DataTransfer();
      dt.items.add(finalFile);
      const dispatchTarget = target.isConnected ? target : document.body;
      try {
        dispatchTarget.dispatchEvent(new DragEvent('drop', {
          bubbles: true, cancelable: true, composed: true,
          dataTransfer: dt, clientX, clientY,
        }));
      } catch (e) {
        console.error('[SecureDoc] 마스킹본 재주입 drop 디스패치 실패:', e);
      }
    });
  }, true);

  // ── 붙여넣기 — 즉시 스캔하지 않고 보류 ────────────────────────────────────────
  document.addEventListener('paste', async (event) => {
    const files = Array.from(event.clipboardData?.files ?? []);
    const file = files.find(f => isSupportedFile(f) && !contentOwnedFiles.has(f) && !contentProcessingFiles.has(f));
    if (!file) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    const target = event.target;

    await stageFileAttachment(file, (finalFile) => {
      // drop 재주입과 동일한 이유(위 주석 참고)로 파일 선택 input을 우선 사용한다.
      const input = findFileInput(target);
      if (input) { setFileOnInput(input, finalFile); return; }

      // 폴백: 파일 선택 input이 없는 경우에만 기존 합성 paste 재생을 시도한다.
      const dt = new DataTransfer();
      dt.items.add(finalFile);
      const dispatchTarget = target.isConnected ? target : document.body;
      try {
        dispatchTarget.dispatchEvent(new ClipboardEvent('paste', {
          bubbles: true, cancelable: true, composed: true, clipboardData: dt,
        }));
      } catch (e) {
        console.error('[SecureDoc] 마스킹본 재주입 paste 디스패치 실패:', e);
      }
    });
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

  /** 마스킹된 텍스트가 채팅 입력창에 채워졌다가 전송되기까지의 짧은 순간, 입력창을
   *  시각적으로 숨긴다. 사용자는 이미 검토 패널에서 마스킹 결과를 확인/승인했으므로
   *  실제 채팅 입력창에 마스킹 텍스트가 한 번 더 노출될 필요가 없다 — opacity만
   *  0으로 감출 뿐 값 자체는 그대로 세팅되어 사이트는 정상적으로 읽어 전송한다. */
  function hideEditorDuringSubmit(cfg) {
    const editor = cfg?.editorSel && document.querySelector(cfg.editorSel);
    if (!editor) return () => {};
    const prevOpacity = editor.style.getPropertyValue('opacity');
    const prevPriority = editor.style.getPropertyPriority('opacity');
    editor.style.setProperty('opacity', '0', 'important');
    return () => {
      if (prevOpacity) editor.style.setProperty('opacity', prevOpacity, prevPriority);
      else editor.style.removeProperty('opacity');
    };
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

    const staged = pendingAttachment; // 보류 중인 첨부가 있으면 결합 검사(combined)
    hidePendingBadge();
    openSidePanel(); // 제스처 시점에 먼저 연다

    let decision = null;
    try {
      decision = staged
        ? await startPanelSession('combined', {
            text,
            base64Data: staged.base64Data,
            mimeType: staged.mimeType,
            fileName: staged.fileName,
            fileSize: staged.fileSize,
          })
        : await startPanelSession('prompt', { text });
    } catch (_) {
      decision = { action: 'cancel' };
    } finally {
      promptInProcess = false;
    }

    if (!decision || decision.action === 'cancel') {
      // 취소하면 보류 중이던 첨부도 함께 정리한다(재시도하려면 다시 첨부해야 함).
      if (staged) clearPendingAttachment();
      return;
    }

    const latestCfg = getPromptConfig();
    if (!latestCfg) return;
    promptApproved = true;

    const restoreEditor = hideEditorDuringSubmit(latestCfg);
    try {
      if (staged) {
        // combined 응답 형태: {action:'send', maskedText, file:{action:'upload'|'passthrough'|'cancel', ...}}
        const finalText = decision.maskedText || text;
        if (decision.file?.action === 'upload' && decision.file.maskedBase64) {
          staged.inject(base64ToFile(decision.file.maskedBase64, decision.file.mimeType, decision.file.fileName));
        } else if (decision.file?.action === 'passthrough') {
          staged.inject(staged.file);
        }
        // decision.file?.action === 'cancel'(파일 재생성 실패)이면 파일 없이 프롬프트만 전송.
        clearPendingAttachment();
        setEditorText(latestCfg, finalText);
        // 파일 재주입(입력창 change/합성 drop 등)을 사이트가 처리할 시간을 준 뒤 전송한다.
        await new Promise((r) => setTimeout(r, 900));
      } else {
        const finalText = decision.action === 'masked' && decision.maskedText ? decision.maskedText : text;
        setEditorText(latestCfg, finalText);
      }

      await resubmitPrompt(latestCfg);
    } finally {
      restoreEditor();
    }
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
