/**
 * content.js  ─  isolated world
 *
 * 역할 (PLAN §변경1 — 검토 패널 HITL, 2026-08-01 재정정):
 *   1. 사용자 제스처(전송 버튼 클릭 / Enter / 파일 선택·드롭·붙여넣기)를 캡처 단계에서
 *      가로챈다.
 *   2. 그 제스처가 살아 있는 동안 SW 에 OPEN_PANEL 을 보내 **브라우저 네이티브
 *      사이드패널**을 연다(아래 "검토 패널 열기" 섹션 참고 — iframe 주입으로
 *      갔다가 다시 네이티브로 돌아온 경위가 그 섹션에 정리돼 있다).
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

  /** MAIN world(interceptor.js)에 "이 파일은 content.js가 사용자 검토까지 이미
   *  마쳤다"고 알린다.
   *
   *  왜 필요한가: interceptor.js의 Layer 2/3(XHR/fetch) 업로드 훅은 여전히 살아
   *  있고, 그쪽은 "이미 승인된 파일"을 _approvedFiles WeakSet 으로 판별한다. 그런데
   *  content.js가 만든 마스킹본 File 은 isolated world 소속이라 MAIN world 의 그
   *  WeakSet 에는 원리적으로 들어갈 수 없다 — 두 world 는 DOM 은 공유하지만 JS 렘이
   *  별개라, 같은 파일이라도 각 world 에서 서로 다른 JS 객체로 보인다. 그래서 우리가
   *  마스킹본을 페이지에 주입하면 사이트가 그걸 업로드할 때 Layer 2 가 "처음 보는
   *  원본"으로 오인해 검토 패널을 한 번 더 띄웠다(실사용자 재현: 전송 직후 이미
   *  마스킹된 내용으로 패널이 재등장). 객체 동일성을 쓸 수 없으니 name+size+type
   *  메타로 알린다.
   *
   *  주입(dispatchEvent)은 동기 실행인데 postMessage 는 태스크 큐를 거치므로, 알림이
   *  MAIN world 에 먼저 도달하도록 한 매크로태스크 양보한 뒤 주입해야 한다(그래서
   *  async 이며, 호출부는 반드시 await 한 뒤 주입해야 한다). */
  async function announceContentApprovedFile(file) {
    if (!file) return;
    window.postMessage({
      __upsecurity_config: true,
      direction: 'isolated-to-main',
      type: 'UPS_CONTENT_APPROVED_FILE',
      meta: {
        name: file.name,
        size: file.size,
        type: file.type || 'application/octet-stream',
      },
    }, '*');
    await new Promise((r) => setTimeout(r, 0));
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

  /** 사이트의 드래그 상태(= "무엇이든 추가하세요" 같은 드롭 오버레이)를 즉시 정리한다.
   *
   *  우리는 원본 파일이 사이트로 새어나가지 않도록 진짜 drop 이벤트를 캡처 단계에서
   *  stopImmediatePropagation() 으로 완전히 삼켜버린다. 그런데 사이트의 오버레이를
   *  내리는 로직은 바로 그 drop(또는 dragleave) 리스너 안에 들어 있다 — dragenter 로
   *  올라간 카운터를 내려줄 이벤트가 영영 오지 않으니 오버레이가 화면에 그대로 남는다
   *  (실사용자 재현: 첨부 후 "무엇이든 추가하세요"가 안 사라짐).
   *
   *  그래서 "파일이 하나도 없는" 합성 dragleave/drop/dragend 를 흘려보낸다. 파일이
   *  없으므로 사이트는 아무것도 첨부하지 않지만, 드래그 상태를 정리하는 코드는 정상적으로
   *  돈다. 우리 drop 캡처 리스너도 파일이 없으면 그냥 통과시키므로 재귀하지 않는다.
   *
   *  진짜 drop 과 같은 tick 에 보내므로 target 이 아직 살아 있다 — 승인 시점까지
   *  미뤘다가 보내면 그 사이 SPA 가 DOM 을 다시 그려 "this.drop is not a function"
   *  크래시로 이어졌던 과거 방식과 다르다. */
  function clearSiteDragState(target, clientX, clientY) {
    const el = target?.isConnected ? target : document.body;
    for (const type of ['dragleave', 'drop', 'dragend']) {
      try {
        el.dispatchEvent(new DragEvent(type, {
          bubbles: true, cancelable: true, composed: true,
          dataTransfer: new DataTransfer(), clientX, clientY,
        }));
      } catch (_) { /* 사이트 핸들러 예외가 우리 흐름을 끊지 않게 */ }
    }
  }

  // ══════════════════════════════════════════════════════════════════════════
  // 검토 패널 열기 — 브라우저 네이티브 사이드패널 (2026-08-01 재정정)
  //
  // (정정 경위) 한동안 패널을 이 탭의 페이지 DOM에 position:fixed iframe 으로
  // 직접 주입했었다. 그러면 패널이 사이트 오른쪽을 그냥 덮어버리는데, 이걸 CSS 로
  // 비켜가려는 시도를 세 번 하고 전부 실패했다:
  //   - <html> 에 margin-right → ChatGPT 에 전혀 안 먹음(앱 셸이 뷰포트 고정이라
  //     조상의 margin 과 무관).
  //   - body 에 transform:translateZ(0) + max-width 로 fixed 컨테이닝 블록을
  //     바꿔치기 → body 는 실제로 좁아졌는데(실측 491px) 앱 셸이 100vw 기준이라
  //     폭이 안 줄고 overflow-x:hidden 에 "잘리기만" 했다(실사용자 스크린샷에서
  //     본문 글자가 중간에 잘림).
  //   - 가짜 resize 로 재계산 유도 → 우리 resize 리스너를 우리가 다시 깨워 무한
  //     재귀, 탭이 멎는 회귀를 냈다.
  // 100vw 는 정의상 뷰포트 기준이라, 콘텐츠 스크립트가 페이지 CSS 로 건드릴 수
  // 있는 범위 안에는 답이 없다. **뷰포트 자체를 줄일 수 있는 건 브라우저뿐**이고,
  // 그게 네이티브 사이드패널이다.
  //
  // 과거에 네이티브를 포기했던 두 사유는 지금 해소됐다:
  //   1) "이미 도킹되어 열린 패널을 닫는 API 가 없다" → chrome.sidePanel.close()
  //      가 Chrome 141 에 추가됐다(W3C webextensions #521 의 결론).
  //   2) "manifest 의 side_panel.default_path 가 모든 탭에 뜨는 전역 패널을
  //      만든다" → default_path 를 아예 선언하지 않고, 열어야 하는 그 순간에
  //      setOptions({ tabId, path, enabled:true }) 로 해당 탭에만 경로를 심는다.
  //      경로가 없는 탭에는 패널이 존재조차 하지 않으므로 탭 스코핑이 성립한다.
  //
  // 남은 제약: sidePanel.open() 은 사용자 제스처에 대한 응답으로만 호출할 수 있고
  // 콘텐츠 스크립트에서 직접 부를 수 없다. 그래서 제스처 핸들러 안에서 곧바로
  // SW 로 OPEN_PANEL 을 보내고, SW 가 그 onMessage 안에서 동기적으로 open() 을
  // 부른다(제스처는 sendMessage 를 타고 전파되지만 await 를 하나라도 끼우면
  // 소실된다 — service-worker.js 의 OPEN_PANEL 핸들러 주석 참고).
  //
  // 그 전파가 깨지는 경로(예: 제스처가 아닌 postMessage 로 시작된 흐름)에 대비해
  // open() 이 거부되면 예전 iframe 오버레이로 폴백한다 — 사이트를 덮긴 하지만
  // 검토 없이 원본이 나가는 것보다는 낫다. 폴백 때문에 manifest 의
  // web_accessible_resources 에 sidepanel/* 노출은 그대로 남겨둔다.
  // ══════════════════════════════════════════════════════════════════════════
  let overlayRoot = null;
  let overlayIframe = null;

  /** 폴백 전용 — 네이티브 패널을 열지 못했을 때만 쓰는 페이지 내 iframe 오버레이.
   *
   *  네이티브 패널과 달리 뷰포트를 줄이지 못해 사이트 오른쪽을 덮는다. 그걸 CSS 로
   *  보정하려던 코드(밀어내기)는 전부 걷어냈다 — 위 섹션 주석대로 원리적으로 안 되고,
   *  대신 사이트 레이아웃만 두 번 망가뜨렸기 때문. */
  function openOverlayPanel() {
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

  function closeOverlayPanel() {
    try { overlayRoot?.remove(); } catch (_) { /* ignore */ }
    overlayRoot = null;
    overlayIframe = null;
  }

  /** 반드시 사용자 제스처 핸들러 안에서, await 없이 동기적으로 호출해야 한다.
   *  여기서 sendMessage 를 거는 시점의 제스처가 SW 의 open() 까지 전파된다. */
  function openSidePanel() {
    if (overlayIframe) return; // 폴백 오버레이가 떠 있으면 그걸 계속 쓴다
    try {
      chrome.runtime.sendMessage({ type: 'OPEN_PANEL' }, (res) => {
        // lastError 를 읽지 않으면 "Unchecked runtime.lastError" 로 콘솔이 시끄럽다.
        if (chrome.runtime.lastError || !res?.ok) openOverlayPanel();
      });
    } catch (_) {
      openOverlayPanel(); // context invalidated — 오버레이도 실패하면 조용히 포기
    }
  }

  /** 정상 흐름에서 패널은 자기가 스스로 닫는다(sidepanel.js 의 window.close()).
   *  여기는 content 쪽 사정으로 닫아야 할 때만 쓴다. */
  function closeSidePanel() {
    if (overlayIframe) { closeOverlayPanel(); return; }
    try {
      chrome.runtime.sendMessage({ type: 'CLOSE_PANEL' }, () => { void chrome.runtime.lastError; });
    } catch (_) { /* ignore */ }
  }

  // 폴백 오버레이(iframe) 자신이 결정 완료 후 닫아달라고 보내는 postMessage 수신.
  // 네이티브 패널은 자기 window.close() 로 닫히므로 이 경로를 타지 않는다.
  window.addEventListener('message', (event) => {
    if (!overlayIframe || event.source !== overlayIframe.contentWindow) return;
    if (event.data?.type === 'UPS_CLOSE_OVERLAY') closeOverlayPanel();
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
        // 우리가 기다리길 포기하면 패널도 닫아준다 — 정상 흐름에선 패널이 결정과
        // 함께 스스로 닫히지만, 이 경로에선 아무도 닫아주지 않아 빈 패널이 남는다.
        closeSidePanel();
        resolve({ action: 'cancel', reason: 'timeout' });
      }, 10 * 60 * 1000);
      pendingSessions.set(sessionId, { resolve, timeout });
      try {
        chrome.runtime.sendMessage({ type: 'START_SCAN', sessionId, kind, payload: scanPayload });
      } catch (e) {
        clearTimeout(timeout);
        pendingSessions.delete(sessionId);
        closeSidePanel();
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

    // 원본 drop 을 삼켜버린 대가로 사이트의 드롭 오버레이가 남는다 — 파일 없는
    // 합성 이벤트로 드래그 상태만 즉시 정리해준다(clearSiteDragState 주석 참고).
    clearSiteDragState(target, clientX, clientY);

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

  /** 전송 버튼이 활성화될 때까지 기다렸다가 누른다.
   *
   *  첨부가 있는 경우 사이트는 그 파일을 다 업로드할 때까지 전송 버튼을 비활성으로
   *  둔다(ChatGPT 확인). 예전엔 재주입 후 고정 900ms 만 기다리고 한 번만 눌러봤는데,
   *  그 시점엔 업로드가 아직 진행 중이라 버튼이 비활성 → 클릭이 먹지 않고 그대로
   *  끝나버렸다(실사용자 재현: 검토는 되는데 전송이 안 됨). 그래서 한 번만 시도하지
   *  않고 버튼이 활성화될 때까지 폴링한다. */
  async function resubmitPrompt(cfg, waitMs = 15000) {
    const startedAt = Date.now();
    await new Promise(r => setTimeout(r, 200)); // setEditorText 후 React 상태 반영 대기
    const sels = (cfg.sendBtnSel || '').split(',').map(s => s.trim()).filter(Boolean);
    const deadline = Date.now() + waitMs;
    while (Date.now() < deadline) {
      for (const sel of sels) {
        const btn = document.querySelector(sel);
        if (btn && !btn.disabled && btn.getAttribute('aria-disabled') !== 'true') {
          btn.click();
          console.log(`[SecureDoc] 재전송: 버튼 클릭 성공 (${Date.now() - startedAt}ms, sel=${sel})`);
          return true;
        }
      }
      await new Promise(r => setTimeout(r, 200));
    }
    // 끝내 활성화되지 않으면 Enter 로 한 번 시도해본다.
    console.warn(`[SecureDoc] 재전송: ${waitMs}ms 동안 전송 버튼이 활성화되지 않음(sel="${cfg.sendBtnSel}") — Enter 로 폴백 시도`);
    const editor = cfg.editorSel && document.querySelector(cfg.editorSel);
    if (editor) {
      editor.focus();
      editor.dispatchEvent(new KeyboardEvent('keydown', {
        key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true,
      }));
    } else {
      console.error(`[SecureDoc] 재전송 실패: 입력창(sel="${cfg.editorSel}")도 못 찾음 — 마스킹본이 입력창에 남아있지만 아무것도 전송되지 않았습니다`);
    }
    return false;
  }

  async function interceptPromptSubmit(event, cfg) {
    // 여기까지 온 시점엔 리스너가 promptInProcess 를 이미 걸러냈지만(blockedWhileScanning),
    // MAIN world 등 다른 경로에서 직접 불릴 수 있어 한 번 더 막는다 — 검사 중 원본이
    // 나가는 것만은 어떤 경로로도 일어나면 안 된다.
    if (!protectionEnabled || promptApproved) return;
    if (blockedWhileScanning(event)) return;
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
    if (!latestCfg) {
      console.error('[SecureDoc] 재전송 실패: 승인은 됐지만 이 사이트 설정을 못 찾음 — 아무것도 전송되지 않았습니다');
      if (staged) clearPendingAttachment();
      return;
    }
    promptApproved = true;

    const restoreEditor = hideEditorDuringSubmit(latestCfg);
    try {
      if (staged) {
        // combined 응답 형태: {action:'send', maskedText, file:{action:'upload'|'passthrough'|'cancel', ...}}
        const finalText = decision.maskedText || text;
        // 주입 전에 MAIN world 에 먼저 알려야 한다 — 안 그러면 사이트가 이 파일을
        // 업로드할 때 interceptor 의 Layer 2/3 가 "처음 보는 원본"으로 오인해 검토
        // 패널을 한 번 더 띄운다(announceContentApprovedFile 주석 참고).
        if (decision.file?.action === 'upload' && decision.file.maskedBase64) {
          const maskedFile = base64ToFile(decision.file.maskedBase64, decision.file.mimeType, decision.file.fileName);
          await announceContentApprovedFile(maskedFile);
          staged.inject(maskedFile);
        } else if (decision.file?.action === 'passthrough') {
          await announceContentApprovedFile(staged.file);
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

  /** 검사가 진행 중일 때 들어온 전송 시도를 삼킨다.
   *
   *  예전엔 promptInProcess 면 리스너가 그냥 return 했는데, preventDefault 를 하지
   *  않으니 그 이벤트가 사이트로 그대로 흘러가 **검사가 끝나기도 전에 원본 프롬프트가
   *  전송**됐다(실사용자 macOS 재현: "보안 분석 중" 에서 안 넘어가는데 메시지만 먼저
   *  들어감). 검사 중에는 아무것도 나가면 안 되므로 이벤트를 확실히 막는다.
   *
   *  promptApproved 는 반대로 그냥 통과시켜야 한다 — 그건 검토를 마치고 우리가 직접
   *  다시 보내는 중이라는 표시라, 막으면 우리 재전송까지 막힌다. */
  function blockedWhileScanning(event) {
    if (!promptInProcess) return false;
    event.preventDefault();
    event.stopImmediatePropagation();
    return true;
  }

  document.addEventListener('click', async (event) => {
    const cfg = getPromptConfig();
    if (!cfg?.sendBtnSel || promptApproved) return;
    const isSendButton = cfg.sendBtnSel.split(',').map(s => s.trim()).some(sel => event.target.closest?.(sel));
    if (!isSendButton) return;
    const btn = event.target.closest?.('button, [role="button"]');
    if (btn?.disabled || btn?.getAttribute?.('aria-disabled') === 'true') return;
    if (blockedWhileScanning(event)) return;
    await interceptPromptSubmit(event, cfg);
  }, true);

  document.addEventListener('keydown', async (event) => {
    if (event.key !== 'Enter' || event.shiftKey || event.ctrlKey || event.altKey || event.metaKey) return;
    const cfg = getPromptConfig();
    if (!cfg?.editorSel || promptApproved) return;
    const editor = document.querySelector(cfg.editorSel);
    if (!editor) return;
    const active = document.activeElement;
    if (!editor.contains?.(active) && active !== editor) return;
    if (blockedWhileScanning(event)) return;
    await interceptPromptSubmit(event, cfg);
  }, true);

  document.addEventListener('submit', async (event) => {
    const cfg = getPromptConfig();
    if (!cfg || promptApproved) return;
    if (blockedWhileScanning(event)) return;
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
