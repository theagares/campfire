/**
 * background/service-worker.js  ─  campfire 백그라운드 (MV3, module)
 *
 * 역할 (PLAN §3 · §변경1 · §변경2):
 *   1) 서버 선택: 48200~48209 병렬 /health 스캔 → 시그니처 일치 포트 채택(캐싱),
 *      실패 시 원격 폴백. (PLAN §3/§11)
 *   2) 검사 오케스트레이션: content.js 의 START_SCAN 요청을 받아 엔진 REST(/jobs,
 *      /jobs/prompt, /jobs/{id}/events)로 검사하고, 진행/결과를 검토 패널에 push.
 *   3) HITL 결정 라우팅: 검토 패널의 PANEL_DECISION 을 원본 탭의 content.js 로 중계
 *      (제스처 불필요 흐름이라 SW 경유해도 무방 — PLAN §변경1).
 *   4) 설정 popup 지원: GET_CONNECTION_INFO 로 현재 연결 대상/포트/엔진 상태 제공.
 *   5) 마스킹 조정 파일 재생성: WRAP_MASKED_TEXT.
 *   6) 검토 패널 열기/닫기: content.js 의 OPEN_PANEL/CLOSE_PANEL 을 받아 브라우저
 *      네이티브 사이드패널을 해당 탭에만 띄운다(2026-08-01 재정정 — content.js 의
 *      "검토 패널 열기" 섹션 주석에 iframe 오버레이에서 되돌아온 경위가 있다).
 */

import { wrapMaskedFile } from '../utils/docwrapper.js';
import {
  REMOTE_URL, LOCAL_HOST, BASE_PORT, PORT_SCAN_COUNT,
  HEALTH_TIMEOUT_MS, isOurEngine, CACHE_KEY,
} from './config.js';

const BADGE_OK = '#1fa36d';
const BADGE_WARN = '#d88a16';
const BADGE_ERROR = '#d84a4a';

function setActionBadge(text, color) {
  chrome.action?.setBadgeText({ text });
  if (color) chrome.action?.setBadgeBackgroundColor({ color });
}

// ════════════════════════════════════════════════════════════════════════════
// §3 서버 선택 — 48200~48209 병렬 스캔 + 시그니처 + 캐싱
// ════════════════════════════════════════════════════════════════════════════

/** 단일 포트 /health 시그니처 확인 (500ms 타임아웃). 일치 시 {port} 반환, 아니면 null. */
async function probePort(port) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), HEALTH_TIMEOUT_MS);
  try {
    const res = await fetch(`http://${LOCAL_HOST}:${port}/health`, {
      method: 'GET', cache: 'no-store', signal: ctrl.signal,
    });
    if (!res.ok) return null;
    const data = await res.json().catch(() => null);
    if (data && isOurEngine(data.service)) {
      return { port, health: data };
    }
    return null;
  } catch (_) {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

/** 10개 포트 병렬 스캔 → 시그니처 일치하는 가장 낮은 포트 채택. 없으면 원격 폴백. */
async function discoverServer() {
  const ports = Array.from({ length: PORT_SCAN_COUNT }, (_, i) => BASE_PORT + i);
  const results = await Promise.all(ports.map(probePort));
  const hits = results.filter(Boolean).sort((a, b) => a.port - b.port);

  let server;
  if (hits.length > 0) {
    const { port, health } = hits[0];
    server = { target: 'local', baseUrl: `http://${LOCAL_HOST}:${port}`, port, health };
  } else {
    server = { target: 'remote', baseUrl: REMOTE_URL, port: null, health: null };
  }
  await chrome.storage.session.set({ [CACHE_KEY]: server });
  return server;
}

/** 캐시된 서버를 반환하고, 없으면 스캔한다. */
async function getServer(forceRescan = false) {
  if (!forceRescan) {
    const cached = (await chrome.storage.session.get(CACHE_KEY))[CACHE_KEY];
    if (cached && cached.baseUrl) return cached;
  }
  return discoverServer();
}

/**
 * 엔진 REST 호출. 캐시된 로컬 포트가 죽었으면(요청 실패) 즉시 재스캔 후 1회 재시도.
 * (PLAN §3: "요청 실패 시에만 재스캔", 주기 폴링 없음)
 */
async function fetchServer(path, options = {}, _retried = false) {
  const server = await getServer();
  const headers = new Headers(options.headers || {});
  // 원격 인증: v1 계획엔 사양 없음. 토큰을 소스에 하드코딩하지 않는다(config.js 주석 참고).
  // 향후 필요 시 여기서 런타임 설정/빌드 주입 값으로 Authorization 헤더를 채운다.
  try {
    const res = await fetch(`${server.baseUrl}${path}`, { ...options, headers });
    return { res, server };
  } catch (err) {
    // 로컬 캐시가 죽었을 가능성 → 재스캔 후 1회 재시도
    if (!_retried) {
      await chrome.storage.session.remove(CACHE_KEY);
      return fetchServer(path, options, true);
    }
    setActionBadge('!', BADGE_ERROR);
    throw new Error(
      `엔진에 연결할 수 없습니다 (${server.baseUrl}). 로컬 앱 실행 여부 또는 네트워크를 확인하세요. 원인: ${err?.message || 'network error'}`,
    );
  }
}

async function checkEngineHealth() {
  try {
    const { res, server } = await fetchServer('/health', { method: 'GET', cache: 'no-store' });
    if (!res.ok) return { ok: false, target: server.target, baseUrl: server.baseUrl, port: server.port };
    const data = await res.json().catch(() => null);
    setActionBadge('', BADGE_OK);
    return {
      ok: true,
      target: server.target,
      baseUrl: server.baseUrl,
      port: server.port ?? data?.port ?? null,
      health: data,
    };
  } catch (err) {
    return { ok: false, error: err.message };
  }
}

// ════════════════════════════════════════════════════════════════════════════
// 진행 이벤트 폴링 (엔진 REST /jobs/{id}/events, EC2 계약 호환)
// ════════════════════════════════════════════════════════════════════════════

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

/** FastAPI HTTPException 응답 본문({"detail": "..."})에서 실제 원인 문구를 뽑는다.
 * 엔진이 500을 던져도 지금까지는 상태코드만 보여줬는데(예: "엔진 업로드 실패 (500)"),
 * 실제로 뭐가 문제인지 알 수 없어 디버깅이 안 됐다 — jobs.py 가 이제 detail 에
 * 예외 메시지를 실어주므로(app/adapters/http_api/jobs.py의 _fail 참고) 그걸 그대로
 * 보여준다. JSON 파싱이 안 되면(엔진이 아예 안 뜬 경우 등) 상태코드만 보여준다. */
async function errorDetail(res) {
  try {
    const data = await res.clone().json();
    if (data && typeof data.detail === 'string') return data.detail;
  } catch (_) { /* JSON 이 아니면 무시 */ }
  return `HTTP ${res.status}`;
}

async function pollJobEvents(jobId, onProgress) {
  let after = 0;
  while (true) {
    const { res } = await fetchServer(`/jobs/${jobId}/events?after=${after}`, {
      method: 'GET', cache: 'no-store',
    });
    if (!res.ok) throw new Error(`진행 상태 조회 실패 (${res.status})`);
    const payload = await res.json();
    const events = payload.events ?? [];
    for (const event of events) {
      after = Math.max(after, event.seq ?? after);
      if (event.type === 'done') return event.result;
      if (event.type === 'error') throw new Error(event.message ?? '엔진 처리 오류');
      onProgress?.(event);
    }
    if (payload.done) {
      // done 이벤트를 못 봤는데 done 플래그면(방어) 마지막 폴링
      const { res: r2 } = await fetchServer(`/jobs/${jobId}/events?after=${after}`, { method: 'GET', cache: 'no-store' });
      const p2 = await r2.json();
      for (const ev of (p2.events ?? [])) {
        if (ev.type === 'done') return ev.result;
        if (ev.type === 'error') throw new Error(ev.message ?? '엔진 처리 오류');
      }
      return null;
    }
    await sleep(800);
  }
}

// ── 파일/프롬프트 검사 ─────────────────────────────────────────────────────────

async function scanFile({ base64Data, mimeType, fileName }, onProgress) {
  const binary = atob(base64Data);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  const blob = new Blob([bytes], { type: mimeType });

  const form = new FormData();
  form.append('file', blob, fileName);
  form.append('mimeType', mimeType);
  form.append('fileName', fileName);

  const { res } = await fetchServer('/jobs', { method: 'POST', body: form });
  if (!res.ok) throw new Error(`엔진 업로드 실패: ${await errorDetail(res)}`);
  const data = await res.json();
  if (data.done && data.result) {
    // 인라인 결과가 있어도 진행 단계 재생을 위해 이벤트를 한 번 훑어준다.
    try { await pollJobEvents(data.jobId, onProgress); } catch (_) {}
    return data.result;
  }
  return pollJobEvents(data.jobId, onProgress);
}

async function scanPrompt({ text }, onProgress) {
  const form = new FormData();
  form.append('text', text);
  const { res } = await fetchServer('/jobs/prompt', { method: 'POST', body: form });
  if (!res.ok) throw new Error(`엔진 업로드 실패: ${await errorDetail(res)}`);
  const data = await res.json();
  if (data.done && data.result) {
    try { await pollJobEvents(data.jobId, onProgress); } catch (_) {}
    return data.result;
  }
  return pollJobEvents(data.jobId, onProgress);
}

// 문서 첨부를 곧바로 스캔하지 않고 보류했다가, 사용자가 프롬프트를 보낼 때 함께
// 넘기는 경로(인젝션 탐지가 실제 user_prompt 를 근거로 판단할 수 있게). 엔진의
// POST /jobs 에 파일 + userPrompt 를 한 번에 보낸다 — 문서/프롬프트 양쪽 결과가
// 한 응답(result.piiItems/injectionItems = 문서, result.userPromptPiiItems = 프롬프트)
// 에 함께 담겨 온다(engine/app/core/pipeline/orchestrator.py 참고).
async function scanCombined({ text, base64Data, mimeType, fileName }, onProgress) {
  const binary = atob(base64Data);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  const blob = new Blob([bytes], { type: mimeType });

  const form = new FormData();
  form.append('file', blob, fileName);
  form.append('mimeType', mimeType);
  form.append('fileName', fileName);
  form.append('userPrompt', text);

  const { res } = await fetchServer('/jobs', { method: 'POST', body: form });
  if (!res.ok) throw new Error(`엔진 업로드 실패: ${await errorDetail(res)}`);
  const data = await res.json();
  if (data.done && data.result) {
    try { await pollJobEvents(data.jobId, onProgress); } catch (_) {}
    return data.result;
  }
  return pollJobEvents(data.jobId, onProgress);
}

// ════════════════════════════════════════════════════════════════════════════
// 세션 상태 — 사이드패널이 열리는 타이밍과 무관하게 스냅샷을 pull 할 수 있게 저장
// ════════════════════════════════════════════════════════════════════════════

// sessionId -> { tabId, kind, status, progress:[], result, error, meta }
const sessions = new Map();
let activeSessionId = null;   // PANEL_READY 가 sender.tab 없이 물어볼 때의 폴백

function pushToPanel(message) {
  // chrome.runtime.sendMessage는 특정 탭이 아니라 열려 있는 모든 확장 페이지(iframe으로
  // 주입된 검토 패널 포함)에 전역 broadcast된다 — 그래서 반드시 message.tabId를 실어
  // 보내고, 받는 쪽(sidepanel.js)이 자기 탭 것이 아니면 무시하게 해야 한다(각 탭의
  // 검토 패널은 그 탭의 DOM 안에만 존재하므로 실제로 다른 탭에 새어나가진 않지만,
  // 한 탭 안에 여러 세션이 겹칠 수 있는 경우를 위한 방어).
  const p = chrome.runtime.sendMessage(message);
  if (p?.catch) p.catch(() => {});
}

function recordSecurityBadge(result) {
  const pii = result?.stats?.piiCount ?? 0;
  const inj = result?.stats?.injectionCount ?? 0;
  const total = pii + inj;
  if (total > 0) setActionBadge(total > 99 ? '99+' : String(total), BADGE_WARN);
  else setActionBadge('', BADGE_OK);
}

async function runScan(sessionId, kind, payload, tabId) {
  const session = {
    tabId, kind, status: 'scanning', progress: [], result: null, error: null,
    meta: kind === 'file'
      ? { fileName: payload.fileName, fileSize: payload.fileSize, mimeType: payload.mimeType }
      : kind === 'combined'
        ? {
            fileName: payload.fileName, fileSize: payload.fileSize, mimeType: payload.mimeType,
            textPreview: (payload.text || '').slice(0, 120),
          }
        : { textPreview: (payload.text || '').slice(0, 120) },
  };
  sessions.set(sessionId, session);
  activeSessionId = sessionId;

  const onProgress = (event) => {
    session.progress.push(event);
    pushToPanel({ type: 'PANEL_PROGRESS', sessionId, tabId, event });
  };

  try {
    const result = kind === 'file'
      ? await scanFile(payload, onProgress)
      : kind === 'combined'
        ? await scanCombined(payload, onProgress)
        : await scanPrompt(payload, onProgress);
    session.status = 'ready';
    session.result = result;
    recordSecurityBadge(result);
    pushToPanel({ type: 'PANEL_RESULT', sessionId, tabId, kind, result, meta: session.meta });
  } catch (err) {
    session.status = 'error';
    session.error = err.message;
    setActionBadge('!', BADGE_ERROR);
    pushToPanel({ type: 'PANEL_ERROR', sessionId, tabId, error: err.message, meta: session.meta });
  }
}

// ════════════════════════════════════════════════════════════════════════════
// 검토 패널(브라우저 네이티브 사이드패널) 열기/닫기
//
// manifest 에 side_panel.default_path 는 일부러 두지 않는다 — 그걸 선언하는 순간
// "모든 탭에 기본으로 존재하는 전역 패널"이 생겨 탭별 스코핑과 정면으로 충돌한다.
// 대신 열어야 하는 그 순간에 그 탭에만 path 를 심고 enabled 를 켠다. path 가 없는
// 탭에는 패널이 애초에 존재하지 않으므로 다른 탭으로 새어나갈 여지가 없다.
//
// path 에 tabId 를 붙이는 이유: 네이티브 사이드패널에서 보낸 메시지에는 sender.tab
// 이 없다(확장 페이지라 탭에 종속되지 않은 컨텍스트로 취급된다). 패널이 자기가 어느
// 탭의 것인지 알 방법이 URL 밖에 없어서, SW 가 직접 만든 값을 그렇게 실어 보낸다
// (sidepanel.js 의 myTabId 주석 참고).
// ════════════════════════════════════════════════════════════════════════════

function panelPathFor(tabId) {
  return `sidepanel/sidepanel.html?tabId=${tabId}`;
}

/** 패널이 URL 에서 읽어 보낸 tabId 를 정수로만 받아들인다.
 *  빈 문자열이 Number('') === 0 으로 통과해 엉뚱한 탭 0 을 만드는 걸 막으려고
 *  숫자/숫자문자열만 명시적으로 통과시킨다. */
function asTabId(value) {
  if (typeof value === 'number') return Number.isInteger(value) ? value : null;
  if (typeof value === 'string' && /^\d+$/.test(value)) return Number(value);
  return null;
}

/** 그 탭에서 패널이 다시 뜨지 않게 끈다. 이미 열려 있는 패널을 강제로 닫지는
 *  못하지만(그건 close() 담당), 검토가 끝난 뒤 그 탭으로 돌아왔을 때 빈 패널이
 *  되살아나는 걸 막는다. */
function disablePanelForTab(tabId) {
  if (tabId == null) return;
  try { chrome.sidePanel?.setOptions({ tabId, enabled: false })?.catch?.(() => {}); } catch (_) { /* ignore */ }
}

/** 열려 있는 패널까지 닫는다. close() 는 Chrome 141+ 이므로 없을 수 있고, 그때는
 *  비활성화만 남는다 — 정상 흐름에선 패널이 스스로 window.close() 로 닫히므로
 *  이 경로는 타임아웃 등 예외 상황용이다. */
function closePanelForTab(tabId) {
  if (tabId == null) return;
  try { chrome.sidePanel?.close?.({ tabId })?.catch?.(() => {}); } catch (_) { /* ignore */ }
  disablePanelForTab(tabId);
}

// ════════════════════════════════════════════════════════════════════════════
// 메시지 핸들러
// ════════════════════════════════════════════════════════════════════════════

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  const type = message?.type;

  // content.js 가 자신의 tabId 를 (제스처 이전에) 캐싱하기 위해 요청.
  // 네이티브 사이드패널에서 물어보면 sender.tab 이 없으므로, 패널이 자기 URL 에서
  // 읽어 실어 보낸 tabId 를 그대로 되돌려준다(SW 가 만든 값이라 신뢰 가능).
  if (type === 'GET_TAB_ID') {
    sendResponse({ tabId: sender?.tab?.id ?? asTabId(message.tabId) });
    return false;
  }

  // content.js: 사용자 제스처 시점에 검토 패널을 열어달라는 요청.
  //
  // open() 은 사용자 제스처에 대한 응답으로만 허용되고, 그 제스처는 content 의
  // sendMessage 를 타고 이 리스너까지 전파된다. 다만 앞에 await 를 하나라도 끼우면
  // 그 시점에 소실되므로(Chromium issue 355266358), setOptions 도 결과를 기다리지
  // 않고 던지기만 한다 — 같은 채널로 순서대로 처리되므로 open() 은 방금 심은 path 를
  // 본다. 그래도 거부되면 content 가 예전 iframe 오버레이로 폴백할 수 있게
  // ok:false 를 돌려준다.
  if (type === 'OPEN_PANEL') {
    const tabId = sender?.tab?.id ?? null;
    if (tabId == null) { sendResponse({ ok: false, reason: 'no-tab' }); return false; }
    try {
      chrome.sidePanel.setOptions({ tabId, path: panelPathFor(tabId), enabled: true })?.catch?.(() => {});
      chrome.sidePanel.open({ tabId })
        .then(() => sendResponse({ ok: true }))
        .catch((err) => sendResponse({ ok: false, reason: 'rejected', error: String(err?.message || err) }));
      return true; // 응답이 비동기 — 채널을 열어둔다
    } catch (err) {
      // sidePanel API 자체가 없는 구버전 Chrome 등
      sendResponse({ ok: false, reason: 'unavailable', error: String(err?.message || err) });
      return false;
    }
  }

  if (type === 'CLOSE_PANEL') {
    closePanelForTab(sender?.tab?.id ?? asTabId(message.tabId));
    sendResponse({ ok: true });
    return false;
  }

  // content.js: 사이드패널을 연 뒤 검사 시작 요청
  if (type === 'START_SCAN') {
    const { sessionId, kind, payload } = message;
    const tabId = sender?.tab?.id ?? message.tabId ?? null;
    runScan(sessionId, kind, payload, tabId);
    sendResponse({ ok: true });
    return false;
  }

  // 사이드패널 로드 완료 → 최신 세션 스냅샷 요청
  // 반드시 "이 요청을 보낸 탭"의 세션만 찾아 돌려준다 — activeSessionId(전역 최신
  // 세션)로 폴백하면, 탭 B에서 새로 뜬 패널이 탭 A의 검사 결과를 자기 것으로
  // 잘못 받아버리는 문제가 있었다(탭 스코핑이 안 먹히는 것처럼 보인 실제 원인).
  //
  // 네이티브 사이드패널에는 sender.tab 이 없으므로 그 스코핑 기준을 잃지 않으려면
  // 패널이 자기 URL(?tabId=)에서 읽어 보낸 값을 써야 한다 — 그 URL 은 SW 가
  // setOptions 할 때 직접 만든 것이라 패널이 임의의 탭을 사칭할 수 없다.
  if (type === 'PANEL_READY') {
    const requesterTabId = sender?.tab?.id ?? asTabId(message.tabId);
    let sid = null, session = null;
    if (requesterTabId != null) {
      for (const [id, s] of sessions) {
        if (s.tabId === requesterTabId) { sid = id; session = s; }
      }
    }
    if (!session && requesterTabId == null && message.sessionId) {
      // sender.tab이 없는(=탭에 종속되지 않은) 특수 컨텍스트에서의 요청만 예외적으로
      // 명시적 sessionId를 신뢰한다.
      session = sessions.get(message.sessionId) || null;
      sid = session ? message.sessionId : null;
    }
    sendResponse({ ok: true, sessionId: sid, session, tabId: requesterTabId });
    return false;
  }

  // 검토 패널의 HITL 결정 → 원본 탭 content.js 로 중계 (제스처 불필요)
  if (type === 'PANEL_DECISION') {
    const { sessionId, decision } = message;
    const session = sessions.get(sessionId);
    if (session?.tabId != null) {
      chrome.tabs.sendMessage(session.tabId, {
        type: 'PANEL_DECISION', sessionId, kind: session.kind, decision,
      }).catch(() => {});
    }
    // 검토가 끝났으니 그 탭의 패널을 꺼둔다 — 패널은 스스로 window.close() 로
    // 닫지만, 옵션을 켠 채로 두면 나중에 그 탭으로 돌아왔을 때 빈 패널이 되살아난다.
    disablePanelForTab(session?.tabId);
    sessions.delete(sessionId);
    if (activeSessionId === sessionId) activeSessionId = null;
    sendResponse({ ok: true });
    return false;
  }

  // 설정 popup: 현재 연결 대상/포트/엔진 상태
  if (type === 'GET_CONNECTION_INFO') {
    checkEngineHealth().then((info) => sendResponse(info));
    return true;
  }

  // 설정 popup: 강제 재스캔
  if (type === 'RESCAN_SERVER') {
    discoverServer().then(() => checkEngineHealth()).then((info) => sendResponse(info));
    return true;
  }

  // 사이드패널: 마스킹 토글 반영 파일 재생성
  if (type === 'WRAP_MASKED_TEXT') {
    const { text, mimeType, fileName } = message.payload;
    try {
      const wrapped = wrapMaskedFile(text, mimeType, fileName, 0);
      let bin = '';
      for (let i = 0; i < wrapped.bytes.length; i += 8192) {
        bin += String.fromCharCode(...wrapped.bytes.subarray(i, i + 8192));
      }
      sendResponse({ success: true, base64: btoa(bin), mime: wrapped.mimeType, name: wrapped.fileName });
    } catch (e) {
      sendResponse({
        success: true,
        base64: btoa(unescape(encodeURIComponent(text))),
        mime: 'text/plain',
        name: (fileName || 'document').replace(/(\.[^.]+)$/, '_masked.md'),
      });
    }
    return false;
  }

  return false;
});

// 설치/기동 시 서버 1회 탐지
chrome.runtime.onInstalled?.addListener(() => {
  discoverServer().catch(() => {});
});
chrome.runtime.onStartup?.addListener(() => {
  discoverServer().catch(() => {});
});

// 툴바 아이콘 클릭은 action.default_popup(설정 전용)로 처리된다(PLAN §변경1/§변경2 분리).
