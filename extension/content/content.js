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

  // 전체 보호(프롬프트 전송 가로채기 포함). 확장 팝업의 헤더 토글이 이 값을 쓴다.
  let protectionEnabled = true;
  // 파일(문서) 레이어. 팝업 토글은 두 값을 함께 끄고 켠다 — "끄면 검사뿐 아니라
  // 인터셉트 자체가 없어야 한다"가 요구사항이라 둘을 갈라둘 이유가 없다.
  // (예전엔 팝업이 이 값만 썼고 protectionEnabled 는 writer 가 없어 항상 true 였다.
  //  그래서 토글을 꺼도 프롬프트는 계속 가로채였다 — 실사용자 리포트로 확인.)
  let fileInterceptEnabled = true;

  const bridgeToken = (
    globalThis.crypto?.randomUUID?.()
    || `${Date.now()}-${Math.random().toString(36).slice(2)}`
  );

  function sendBridgeTokenToMain() {
    window.postMessage({
      __campfire_config: true,
      direction: 'isolated-to-main',
      type: 'SECUREDOC_BRIDGE_TOKEN',
      token: bridgeToken,
    }, '*');
  }

  function sendProtectionStateToMain(enabled, fileEnabled) {
    window.postMessage({
      __campfire_config: true,
      direction: 'isolated-to-main',
      type: 'UPS_PROTECTION_STATE',
      enabled: Boolean(enabled),
      fileInterceptEnabled: Boolean(fileEnabled),
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
      __campfire_config: true,
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

  // ── MAIN world 가 알려주는 "지금 파일을 올리는 중" 신호 ────────────────────
  //
  // content.js 는 isolated world 라 페이지의 XHR/fetch 를 직접 볼 수 없다. 반면
  // interceptor.js 는 MAIN world 에서 이미 그 둘을 후킹하고 있어서, 파일처럼 생긴
  // 바디를 가진 요청의 진행 중 개수를 여기로 브로드캐스트해준다(그쪽 파일 맨 아래
  // "첨부 업로드 관측기" 참고). waitForAttachmentReady 가 이 값을 쓴다.
  let uploadInflight = 0;
  // 시작 이벤트 누적 카운터. "대기를 시작한 뒤 업로드가 하나라도 있었나"를 보려면
  // 진행 중 개수만으론 부족하다 — 업로드가 폴링 간격보다 빨리 끝나면 inflight 는
  // 계속 0 으로만 보이기 때문이다.
  let uploadStartCount = 0;
  window.addEventListener('message', (event) => {
    if (event.source !== window) return;
    const data = event.data;
    if (!data?.__campfire_config || data.direction !== 'main-to-isolated') return;
    if (data.type !== 'UPS_UPLOAD_ACTIVITY') return;
    uploadInflight = Math.max(0, Number(data.inflight) || 0);
    if (data.phase === 'start') uploadStartCount += 1;
  });

  chrome.storage?.local?.get?.(
    { protectionEnabled: true, fileInterceptEnabled: true },
    ({ protectionEnabled: enabled, fileInterceptEnabled: fileEnabled }) => {
      protectionEnabled = Boolean(enabled);
      fileInterceptEnabled = Boolean(fileEnabled);
      sendBridgeTokenToMain();
      sendProtectionStateToMain(protectionEnabled, fileInterceptEnabled);
    },
  );

  chrome.storage?.onChanged?.addListener?.((changes, areaName) => {
    if (areaName !== 'local') return;
    if (!changes.protectionEnabled && !changes.fileInterceptEnabled) return;
    if (changes.protectionEnabled) protectionEnabled = Boolean(changes.protectionEnabled.newValue);
    if (changes.fileInterceptEnabled) fileInterceptEnabled = Boolean(changes.fileInterceptEnabled.newValue);
    sendProtectionStateToMain(protectionEnabled, fileInterceptEnabled);
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
    if (!protectionEnabled || !fileInterceptEnabled || !file) return false;
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

  /** 주입 "시점"에 살아 있는 파일 input 을 고른다.
   *
   *  왜 필요한가: 파일을 가로챈 순간부터 사용자가 검토 패널에서 승인할 때까지 수 초~
   *  수십 초가 흐른다(그 사이 프롬프트를 다 입력한다). 그동안 SPA 가 컴포저를 다시
   *  그리면, 가로챌 때 붙들어 둔 input 은 DOM 에서 떨어져 나간 고아 노드가 된다.
   *  거기에 파일을 넣고 change 를 쏘면 — 예외도 안 나고 input.files 에는 파일이
   *  들어가는데 — 그 이벤트는 document 까지 버블링하지 않으므로 사이트는 아무것도
   *  못 받는다. 파일만 조용히 사라지고 프롬프트는 그대로 전송된다.
   *  (실측: 살아있는 input 은 사이트가 change 1회 수신, detached 는 0회. 고아 노드에
   *   파일 1개가 들어간 채 실제 컴포저는 0개였다.)
   *
   *  주의: findFileInput 처럼 target.closest('form') 을 먼저 보면, target 이 이미
   *  detached 인 경우 그 "detached 한 form" 안의 낡은 input 을 그대로 돌려준다.
   *  그래서 매 후보마다 isConnected 를 확인한다. */
  function liveFileInput(preferred) {
    if (preferred?.isConnected) return preferred;
    const byForm = preferred?.closest?.('form')?.querySelector?.('input[type="file"]');
    if (byForm?.isConnected) return byForm;
    // querySelector 는 문서에 붙어 있는 노드만 돌려주므로 이건 항상 살아 있다.
    return document.querySelector('input[type="file"]');
  }

  /** 마스킹본을 파일 input 으로 흘려보내고, 사이트가 실제로 받았는지까지 확인한다.
   *  못 넣었으면 조용히 넘어가지 않고 알린다 — 예전엔 실패해도 아무 신호가 없어서
   *  "검사는 되는데 파일만 안 간다" 로만 보였다. 성공 여부를 돌려주므로 호출부가
   *  폴백(합성 drop/paste)으로 넘어갈지 정할 수 있다. */
  /** 파일 input 이 아예 없을 때의 폴백 — 컴포저에 합성 drop 을 재생한다.
   *
   *  Gemini 는 첨부 메뉴를 닫으면 input[type=file] 을 DOM 에서 통째로 없앤다
   *  (실사용자 콘솔: "파일 재주입 실패: 살아 있는 input[type=file] 을 찾지 못했습니다"
   *   — content.js:498 = 파일 선택 경로의 주입 콜백). 그래서 승인 시점엔 넣을 곳이
   *  없어 파일이 조용히 버려지고 프롬프트만 전송됐다.
   *
   *  이런 사이트도 컴포저에 파일을 끌어다 놓는 건 지원하므로 그 경로로 넣는다.
   *  일부 사이트는 dragenter/dragover 로 드롭 상태가 만들어져야 drop 을 처리해서
   *  세 이벤트를 순서대로 보낸다.
   *
   *  이 합성 drop 은 우리 drop 캡처 리스너에도 걸리지만, 마스킹본은
   *  base64ToFile() 이 contentOwnedFiles 에 넣어둔 파일이라 그 리스너가 걸러낸다
   *  (재귀하지 않는다). */
  function injectFileByDrop(finalFile, preferredTarget) {
    let target = preferredTarget?.isConnected ? preferredTarget : null;
    if (!target) {
      const editor = findEditor(getPromptConfig());
      if (editor?.isConnected) target = editor;
    }
    if (!target) target = document.body;
    if (!target) return false;
    try {
      const dt = new DataTransfer();
      dt.items.add(finalFile);
      const init = { bubbles: true, cancelable: true, composed: true, dataTransfer: dt };
      target.dispatchEvent(new DragEvent('dragenter', init));
      target.dispatchEvent(new DragEvent('dragover', init));
      target.dispatchEvent(new DragEvent('drop', init));
      console.log('[SecureDoc] 파일 재주입: input 이 없어 합성 drop 으로 넣었습니다');
      return true;
    } catch (e) {
      console.error('[SecureDoc] 파일 재주입 폴백(drop) 실패:', e);
      return false;
    }
  }

  /** 사이트가 DOM 에서 떼어낸 파일 input 을 원래 자리에 되돌려 놓는다.
   *
   *  Gemini 는 첨부 메뉴를 닫으면 input[type=file] 을 DOM 에서 없앤다. 그런데 그
   *  노드 자체는 우리가 붙들고 있고, DOM 리스너는 노드를 떼어내도 그대로 살아 있다.
   *  원래 부모에 다시 붙이면 그 노드에 직접 걸린 리스너도, 조상에 위임된 리스너도
   *  다시 유효해진다 — 사이트가 만든 바로 그 노드라 사이트 입장에선 자기 input 이다.
   *
   *  합성 drop 보다 이걸 먼저 시도한다. drop 재생은 사이트의 드래그 상태 머신에
   *  기대는데, 그 상태가 없으면 사이트 핸들러가 "this.drop is not a function" 으로
   *  터진다(실사용자 Gemini 콘솔에서 재현 — 이 파일 아래 drop 재주입 주석에도
   *  같은 크래시가 ChatGPT 사례로 기록돼 있다). 리스너 안에서 난 예외는 우리
   *  try/catch 로 잡을 수도 없어서, 사이트의 드롭 처리만 조용히 중단된다. */
  function reviveFileInput(orphan, parentHint) {
    if (!orphan || orphan.isConnected) return orphan || null;
    const host = parentHint?.isConnected
      ? parentHint
      : (findEditor(getPromptConfig())?.closest?.('form') || document.body);
    if (!host) return null;
    try {
      host.appendChild(orphan);
      if (!orphan.isConnected) return null;
      console.log('[SecureDoc] 파일 재주입: 사이트가 떼어낸 input 을 되돌려 놓았습니다');
      return orphan;
    } catch (e) {
      console.warn('[SecureDoc] 파일 input 되돌리기 실패:', e);
      return null;
    }
  }

  function injectFileIntoInput(preferred, finalFile, parentHint) {
    // input 이 사라진 사이트(Gemini 등)는 여기서 끝내면 파일이 통째로 없어진다.
    // 되돌려 놓기 → 그래도 안 되면 합성 drop 순으로 시도한다.
    const input = liveFileInput(preferred) || reviveFileInput(preferred, parentHint);
    if (!input) {
      return injectFileByDrop(finalFile, null);
    }
    // 성공 판정은 "쏘기 전에 DOM 에 붙어 있었나" 로 한다. 쏜 "뒤" 의 isConnected 를
    // 보면 안 된다 — 사이트가 change 를 처리하면서 그 input 을 곧바로 떼어내는 게
    // 정상 동작이기 때문이다(실사용자 Gemini 로그: files:1 인데 connected:false).
    // 그걸 실패로 오판하면 이미 잘 들어간 파일에 합성 drop 을 한 번 더 쏘게 되고,
    // 그 drop 이 사이트 핸들러를 터뜨리거나 첨부가 중복될 수 있다.
    const wasConnected = input.isConnected;
    setFileOnInput(input, finalFile);
    if (!wasConnected || input.files?.length !== 1) {
      console.warn('[SecureDoc] input 주입이 반영되지 않았습니다 — 합성 drop 으로 재시도합니다', {
        connectedBefore: wasConnected, files: input.files?.length,
      });
      return injectFileByDrop(finalFile, null);
    }
    return true;
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
      overlayIframe.title = 'Campfire 문서 검토';
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

    // 여기서 붙든 input 을 그대로 쓰지 않는다 — 승인까지 시간이 흐르는 동안 SPA 가
    // 컴포저를 다시 그리면 이 노드는 고아가 되고, 거기에 넣은 파일은 사이트에 전달되지
    // 않는다(liveFileInput 주석 참고). drop/paste 경로는 이미 주입 시점에 다시 찾고
    // 있었는데 이 📎 경로만 예전 방식으로 남아 있었다.
    // 지금 부모를 기억해 둔다 — 사이트가 나중에 이 input 을 DOM 에서 떼어내면
    // 여기로 되돌려 붙여야 사이트의 위임 리스너까지 살아난다(reviveFileInput).
    const originalParent = input.parentElement;
    await stageFileAttachment(file, (finalFile) => injectFileIntoInput(input, finalFile, originalParent));
  }, true);

  // ── 드래그앤드롭 — 즉시 스캔하지 않고 보류 ───────────────────────────────────
  document.addEventListener('dragover', (event) => {
    // 보호가 꺼져 있으면 preventDefault 도 하지 않는다 — 이걸 빼먹으면 토글을 꺼도
    // 페이지가 계속 우리 드롭 타깃으로 동작해서 "인터셉트가 안 꺼진다" 로 보인다
    // (실사용자 리포트). 검사만 건너뛰는 게 아니라 개입 자체를 하지 않아야 한다.
    if (!protectionEnabled || !fileInterceptEnabled) return;
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
      if (injectFileIntoInput(findFileInput(target), finalFile)) return;

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
      if (injectFileIntoInput(findFileInput(target), finalFile)) return;

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

  // ── 선택자가 깨졌을 때의 폴백 ─────────────────────────────────────────────
  //
  // 위 PROMPT_CONFIGS 는 사이트 DOM 에 그대로 의존해서, 그쪽이 개편되면 조용히
  // 깨진다. 그리고 깨졌을 때의 증상이 "아무 일도 안 일어남" 이라 원인 파악이
  // 어렵다(실사용자 리포트: copilot·perplexity 는 사이드바가 아예 안 뜨고,
  // gemini 는 뜨는데 전송이 안 됨).
  //
  // 트리거 경로가 셋인데 전부 선택자에 걸려 있는 게 문제였다:
  //   click    → sendBtnSel 이 안 맞으면 안 뜬다
  //   keydown  → editorSel 이 안 맞으면 안 뜬다
  //   submit   → SPA 는 네이티브 form submit 을 거의 안 쓴다
  // 그래서 editorSel 이 깨지면 사이드바 자체가 안 뜨고(copilot·perplexity),
  // sendBtnSel 만 깨지면 검토는 되는데 재전송이 실패한다(gemini — 15초 폴링 후
  // 합성 Enter 로 폴백하지만 Quill 은 그걸 무시한다).
  //
  // 아래 폴백은 "사이트별 선택자가 실제로 안 맞을 때만" 동작한다 — 맞는 사이트의
  // 기존 동작은 건드리지 않는다.

  function isEditableEl(el) {
    if (!el || el === document.body) return false;
    if (el.tagName === 'TEXTAREA') return true;
    return el.isContentEditable === true;
  }

  /** 지금 실제로 글을 쓰고 있는 입력창. 선택자가 맞으면 그걸 쓰고, 아니면 포커스된
   *  편집 가능한 요소로 폴백한다. */
  function findEditor(cfg) {
    let bySel = null;
    try { bySel = cfg?.editorSel ? document.querySelector(cfg.editorSel) : null; } catch (_) { bySel = null; }
    if (bySel) return bySel;
    const active = document.activeElement;
    if (!isEditableEl(active)) return null;
    warnStaleSelector('editorSel', cfg?.editorSel);
    return active;
  }

  // 사이트를 안 가리는 전송 버튼 후보. aria-label 은 언어별로 다르므로 부분 일치를
  // 쓰고, 사이트가 자기 선택자로 이미 잡히는 경우엔 아예 쓰이지 않는다.
  const GENERIC_SEND_SELS = [
    'button[type="submit"]',
    'button[aria-label*="send" i]',
    'button[aria-label*="submit" i]',
    'button[aria-label*="보내기"]',
    'button[aria-label*="제출"]',
    'button[data-testid*="send" i]',
    'button[data-testid*="submit" i]',
  ];

  /** 전송 버튼 후보 선택자 목록. 사이트별 선택자가 문서에서 하나도 안 잡힐 때만
   *  일반 후보를 덧붙인다(= 선택자가 깨진 상태). */
  function sendButtonSelectors(cfg) {
    const configured = (cfg?.sendBtnSel || '').split(',').map(s => s.trim()).filter(Boolean);
    const anyPresent = configured.some(sel => {
      try { return !!document.querySelector(sel); } catch (_) { return false; }
    });
    if (anyPresent) return configured;
    // 여기서 경고하지 않는다. 이 함수는 폴링 중에도 매번 불리는데, 컴포저가 아직
    // 안 그려진 순간에도 "선택자가 낡았다" 고 잘못 울렸다(실사용자 Gemini 로그:
    // 경고가 뜬 뒤 정작 재전송은 설정된 선택자로 성공했다). 실제로 어떤 선택자가
    // 먹었는지는 재전송 성공 로그에 sel= 로 찍히므로 정보가 아쉬울 것도 없다.
    return [...configured, ...GENERIC_SEND_SELS];
  }

  // 선택자가 깨진 걸 조용히 넘기지 않는다. 폴백이 있어도 사이트가 개편됐다는
  // 사실 자체는 남겨야 다음에 원인을 찾을 수 있다(종류당 한 번만).
  const _warnedSelectors = new Set();
  function warnStaleSelector(kind, sel) {
    if (_warnedSelectors.has(kind)) return;
    _warnedSelectors.add(kind);
    console.warn(
      `[SecureDoc] ${location.hostname} 의 ${kind}("${sel}")이 현재 페이지에서 하나도 안 잡힙니다 — `
      + '사이트 개편으로 선택자가 낡았을 수 있어 일반 후보로 폴백합니다.',
    );
  }

  function getEditorText(cfg) {
    const editor = findEditor(cfg);
    if (!editor) return '';
    if (editor.tagName === 'TEXTAREA') return editor.value.trim();
    return (editor.innerText || editor.textContent || '').trim();
  }

  function setEditorText(cfg, text) {
    const editor = findEditor(cfg);
    if (!editor) return false;
    editor.focus();
    if (editor.tagName === 'TEXTAREA') {
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set;
      if (setter) setter.call(editor, text); else editor.value = text;
      editor.dispatchEvent(new Event('input', { bubbles: true }));
      editor.dispatchEvent(new Event('change', { bubbles: true }));
      return true;
    }
    // contenteditable 은 에디터 프레임워크마다 먹는 방법이 달라 여러 수단을 쓴다.
    // 중요한 건 "첫 번째로 성공한 것에서 멈추는" 것이다.
    //
    // 예전엔 세 수단(execCommand / 합성 InputEvent / 합성 paste)을 조건 없이 전부
    // 실행했다. Lexical(perplexity)처럼 셋을 다 받아들이는 에디터에서는 같은 글이
    // 그만큼 여러 번 삽입되고, 게다가 Lexical 은 execCommand('selectAll'+'delete')를
    // 무시해서 원래 있던 글까지 남는다 — 합쳐서 프롬프트가 4벌로 들어갔다
    // (실사용자 리포트: "프롬프트가 4번 반복돼서 넘어간다").
    const target = text.trim();
    const current = () => (editor.innerText || editor.textContent || '').trim();
    const done = () => current() === target;

    // 기존 내용을 지운다. execCommand('selectAll') 만으로는 Lexical 같은 에디터에서
    // 안 먹는 경우가 있어, 실제 DOM 선택 영역을 직접 잡아준다 — insertText/paste 는
    // "선택 영역을 대체" 하므로 선택만 제대로 잡혀 있으면 지우기와 넣기가 한 번에 된다.
    const clear = () => {
      try {
        const sel = window.getSelection?.();
        if (sel) {
          const range = document.createRange();
          range.selectNodeContents(editor);
          sel.removeAllRanges();
          sel.addRange(range);
        }
      } catch (_) { /* 아래 execCommand 로도 시도한다 */ }
      try {
        document.execCommand('selectAll', false, null);
        document.execCommand('delete', false, null);
      } catch (_) { /* 아래 수단이 알아서 덮어쓴다 */ }
    };

    // 1) execCommand — 가장 "진짜 입력"에 가까워 대부분의 에디터가 자기 이벤트를 낸다.
    clear();
    try { document.execCommand('insertText', false, text); } catch (_) { /* 다음 수단 */ }
    if (done()) return true;

    // 2) 합성 paste — Lexical 등 execCommand 를 무시하는 에디터용.
    clear();
    try {
      const dt = new DataTransfer();
      dt.setData('text/plain', text);
      editor.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true }));
    } catch (_) { /* 다음 수단 */ }
    if (done()) return true;

    // 3) 최후 — DOM 을 직접 갈아끼우고 input 을 알린다. 프레임워크가 자기 상태와
    //    어긋난 것으로 보고 되돌릴 수 있어 마지막에만 쓴다.
    clear();
    editor.textContent = text;
    // 알림용 이벤트에는 data/inputType 을 싣지 않는다. insertText + data 를 실으면
    // 프레임워크가 "또 넣으라는 뜻" 으로 읽어 방금 세팅한 내용 뒤에 한 벌 더 붙는다
    // (실측: 이 한 줄 때문에 마지막 수단에서도 2벌이 됐다).
    editor.dispatchEvent(new Event('input', { bubbles: true }));
    if (done()) return true;

    console.warn('[SecureDoc] 입력창에 마스킹본을 넣지 못했습니다 — 현재 내용이 의도와 다를 수 있습니다');
    return false;
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
  // 사이트를 안 가리는 "무언가 진행 중" 표시. 첨부 칩의 스피너/진행률 바가 보통
  // 이 중 하나로 그려진다. 문서 전체에서 세되 "대기 시작 시점의 개수"를 기준선으로
  // 잡고 그보다 늘어난 것만 신호로 본다 — 답변 스트리밍 인디케이터처럼 원래부터
  // 떠 있는 것에 걸려 영원히 기다리는 일을 막기 위해서다.
  const BUSY_INDICATOR_SELS = [
    '[role="progressbar"]',
    'progress',
    '[aria-busy="true"]',
  ];

  function countBusyIndicators() {
    let n = 0;
    for (const sel of BUSY_INDICATOR_SELS) {
      try { n += document.querySelectorAll(sel)?.length ?? 0; } catch (_) { /* 무시 */ }
    }
    return n;
  }

  /** 첨부가 사이트에 실제로 올라갈 때까지 기다린다.
   *
   *  왜 필요한가: 예전엔 파일을 넣고 고정 900ms 만 기다린 뒤 전송했다. 사이트는
   *  파일을 자기 서버로 올리는 중인데 전송 버튼은 "텍스트가 있으니" 열려 있어서,
   *  업로드가 끝나기 전에 눌려 프롬프트만 먼저 나갔다(실사용자 Gemini 리포트:
   *  "첨부는 됐는데 요약하기가 먼저 들어갔어").
   *
   *  (2026-08-05 재정정) 그 다음 시도는 "업로드 중엔 전송 버튼이 잠긴다"를 신호로
   *  썼다. ChatGPT 는 그렇게 동작하지만 Gemini 는 아니다 — 업로드 내내 전송 버튼이
   *  활성이라 신호가 안 잡히고 900ms 폴백으로 떨어졌다(실사용자 Gemini 콘솔:
   *    [SecureDoc] 파일 재주입: 사이트가 떼어낸 input 을 되돌려 놓았습니다
   *    [SecureDoc] 첨부 대기: 업로드 신호 없음 — 900ms 후 전송합니다
   *    [SecureDoc] 재전송: 버튼 클릭 성공 (207ms, sel=button[aria-label="메시지 보내기"])
   *  주입은 성공했는데 207ms 만에 전송됐다 = 업로드 도중 전송).
   *
   *  그래서 신호를 하나에 걸지 않고, 사이트에 덜 의존하는 순서로 셋을 함께 본다:
   *    1) network — MAIN world(interceptor.js)가 세는 "파일 바디를 가진 요청"의
   *                 진행 중 개수. 어떤 사이트의 DOM 에도 의존하지 않는다.
   *    2) dom     — 진행률/스피너 요소가 대기 시작 시점보다 늘어났는지.
   *    3) button  — 전송 버튼 잠김(예전 신호). ChatGPT 처럼 실제로 잠그는 사이트용.
   *  셋 중 하나라도 "시작"을 보면 그것들이 전부 풀릴 때까지 기다린다. 사이트별
   *  선택자나 클래스명은 하나도 쓰지 않는다 — 그런 하드코딩은 사이트 개편 때마다
   *  조용히 깨져왔다(이 파일의 "선택자가 깨졌을 때의 폴백" 섹션 참고).
   *
   *  아무 신호도 못 보면 그대로 전송한다. 예전의 900ms 추가 대기는 근거 없는
   *  값이었다 — 어디서 온 숫자인지 설명할 수 없고, 실제로 Gemini 를 못 구했다.
   *  지금은 그 시간을 "가만히 자는" 데 쓰지 않고 probeMs 동안 세 신호를 실제로
   *  관측하는 데 쓴다. 신호가 잡히면 그 즉시 빠져나오므로 정상 경로는 오히려 빨라지고,
   *  끝까지 아무것도 안 잡히면 그건 "모르겠다"가 아니라 "이 페이지는 업로드를
   *  시작하지 않았다"는 관측 결과다. */
  async function waitForAttachmentReady(cfg, probeMs = 1500, readyWaitMs = 60000) {
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const sendBtn = () => {
      for (const sel of sendButtonSelectors(cfg)) {
        try {
          const b = document.querySelector(sel);
          if (b) return b;
        } catch (_) { /* 잘못된 선택자는 건너뛴다 */ }
      }
      return null;
    };
    const locked = () => {
      const b = sendBtn();
      return !b || b.disabled || b.getAttribute('aria-disabled') === 'true';
    };

    // 기준선은 반드시 주입 "직후"인 지금 잡는다 — 이 뒤에 늘어난 것만 우리 첨부
    // 때문이라고 볼 수 있다.
    const startCountBase = uploadStartCount;
    const busyBase = countBusyIndicators();

    const netBusy = () => uploadInflight > 0;
    const netFinished = () => uploadStartCount > startCountBase && uploadInflight === 0;
    const domBusy = () => countBusyIndicators() > busyBase;

    const fired = new Set();
    const probe = () => {
      if (netBusy()) fired.add('network');
      if (domBusy()) fired.add('dom');
      if (locked()) fired.add('button');
      return fired.size > 0;
    };
    const stillBusy = () => (
      (fired.has('network') && netBusy())
      || (fired.has('dom') && domBusy())
      || (fired.has('button') && locked())
    );

    const startedAt = Date.now();
    while (Date.now() - startedAt < probeMs) {
      if (probe()) break;
      // 업로드가 폴링 간격보다 빨리 끝나버린 경우 — 시작을 못 봤어도 이미 끝났다.
      if (netFinished()) {
        console.log(`[SecureDoc] 첨부 대기: 업로드가 즉시 끝났습니다 (network, ${Date.now() - startedAt}ms)`);
        return true;
      }
      await sleep(80);
    }

    if (!fired.size) {
      console.log(
        `[SecureDoc] 첨부 대기: 업로드 신호 없음 — ${probeMs}ms 동안 network/dom/button 셋 다 무반응이라 그대로 전송합니다`,
      );
      return false;
    }

    const signal = [...fired].join('+');
    console.log(`[SecureDoc] 첨부 대기: 업로드 진행 감지 (${signal}) — 끝날 때까지 기다립니다`);
    while (Date.now() - startedAt < readyWaitMs) {
      if (!stillBusy()) {
        // 업로드 응답을 받은 뒤 사이트가 첨부를 컴포저 상태에 반영하는 데 한 틱이
        // 더 걸린다. 그 사이에 눌리면 다시 첨부 없이 나갈 수 있어 짧게 양보한다.
        // (이 값은 안전 여유일 뿐 신호가 아니다 — 실기 계측으로 정한 값은 아니다.)
        await sleep(250);
        console.log(
          `[SecureDoc] 첨부 대기: 업로드 완료로 보고 전송합니다 (${signal}, ${Date.now() - startedAt}ms)`,
        );
        return true;
      }
      probe(); // 늦게 켜지는 신호도 이후 대기에 반영한다
      await sleep(120);
    }
    console.warn(
      `[SecureDoc] 첨부 대기: ${readyWaitMs}ms 안에 업로드가 끝나지 않았습니다 (${signal}) — 그대로 전송을 시도합니다`,
    );
    return false;
  }

  async function resubmitPrompt(cfg, waitMs = 15000) {
    const startedAt = Date.now();
    await new Promise(r => setTimeout(r, 200)); // setEditorText 후 React 상태 반영 대기
    const deadline = Date.now() + waitMs;
    while (Date.now() < deadline) {
      // 매 회 다시 고른다 — 사이트별 선택자가 깨졌으면 일반 후보로 폴백해야 하고,
      // 그 판정은 DOM 이 다시 그려지면 바뀔 수 있다.
      for (const sel of sendButtonSelectors(cfg)) {
        let btn = null;
        try { btn = document.querySelector(sel); } catch (_) { continue; }
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
    const editor = findEditor(cfg);
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
        // 주입 성공 여부를 반드시 본다 — 예전엔 반환값을 버려서, 파일이 하나도 안
        // 들어갔는데도 그대로 프롬프트만 전송했다("문서가 안 간다"의 마지막 조각).
        // 텍스트를 "먼저" 넣는다. 그래야 전송 버튼이 텍스트 기준으로 일단 열리고,
        // 그 다음 파일을 넣었을 때 사이트가 업로드하느라 버튼을 잠그는 것을
        // 신호로 쓸 수 있다(waitForAttachmentReady). 순서가 반대면 대기 내내
        // 입력창이 비어 버튼이 계속 잠겨 있어 업로드 중인지 구분할 수 없다.
        setEditorText(latestCfg, finalText);

        let injected = true;
        if (decision.file?.action === 'upload' && decision.file.maskedBase64) {
          const maskedFile = base64ToFile(decision.file.maskedBase64, decision.file.mimeType, decision.file.fileName);
          await announceContentApprovedFile(maskedFile);
          injected = staged.inject(maskedFile) !== false;
        } else if (decision.file?.action === 'passthrough') {
          await announceContentApprovedFile(staged.file);
          injected = staged.inject(staged.file) !== false;
        }
        if (!injected) {
          console.error(
            '[SecureDoc] 문서를 페이지에 다시 넣지 못했습니다 — 프롬프트만 전송됩니다. '
            + '문서를 다시 첨부해 주세요.',
          );
        }
        // decision.file?.action === 'cancel'(파일 재생성 실패)이면 파일 없이 프롬프트만 전송.
        clearPendingAttachment();
        // 첨부가 사이트에 실제로 올라갈 때까지 기다린 뒤 전송한다.
        if (injected) await waitForAttachmentReady(latestCfg);
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
    // 사이트별 선택자가 깨졌으면 일반 후보까지 본다(sendButtonSelectors 주석 참고).
    const isSendButton = sendButtonSelectors(cfg).some(sel => {
      try { return !!event.target.closest?.(sel); } catch (_) { return false; }
    });
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
    // 선택자가 깨졌으면 포커스된 편집 요소로 폴백한다 — 이게 없으면 사이드바가
    // 아예 안 뜬다(copilot·perplexity 증상).
    const editor = findEditor(cfg);
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
