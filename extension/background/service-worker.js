/**
 * background/service-worker.js  ─  securedoc-gateway 백그라운드 (MV3, module)
 *
 * 역할 (PLAN §3 · §변경1 · §변경2):
 *   1) 서버 선택: 48200~48209 병렬 /health 스캔 → 시그니처 일치 포트 채택(캐싱),
 *      실패 시 원격 폴백. (PLAN §3/§11)
 *   2) 검사 오케스트레이션: content.js 의 START_SCAN 요청을 받아 엔진 REST(/jobs,
 *      /jobs/prompt, /jobs/{id}/events)로 검사하고, 진행/결과를 사이드패널에 push.
 *   3) HITL 결정 라우팅: 사이드패널의 PANEL_DECISION 을 원본 탭의 content.js 로 중계
 *      (제스처 불필요 흐름이라 SW 경유해도 무방 — PLAN §변경1).
 *   4) 설정 popup 지원: GET_CONNECTION_INFO 로 현재 연결 대상/포트/엔진 상태 제공.
 *   5) 마스킹 조정 파일 재생성: WRAP_MASKED_TEXT.
 *
 * ※ 사이드패널을 "여는" 호출은 SW 가 한다 (2026-07-23 정정). chrome.sidePanel
 *   네임스페이스는 content script(Isolated world) 컨텍스트엔 애초에 존재하지 않음을
 *   실측 확인했다 — 그래서 content.js 는 OPEN_SIDE_PANEL 메시지를 SW 로 "1회만" 보내고,
 *   SW 는 그 메시지 핸들러 안에서 곧바로(추가 await 없이) chrome.sidePanel.open() 을
 *   호출한다. 메시지 왕복이 1회뿐이면 사용자 제스처가 보존된다는 걸 실측으로 확인했다
 *   (PLAN §변경1 참고).
 */

import { wrapMaskedFile } from '../utils/docwrapper.js';
import {
  REMOTE_URL, LOCAL_HOST, BASE_PORT, PORT_SCAN_COUNT,
  HEALTH_TIMEOUT_MS, SERVICE_SIGNATURE, CACHE_KEY,
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
    if (data && data.service === SERVICE_SIGNATURE) {
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
  if (!res.ok) throw new Error(`엔진 업로드 실패 (${res.status})`);
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
  if (!res.ok) throw new Error(`엔진 업로드 실패 (${res.status})`);
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
let activeSessionId = null;   // 사이드패널이 PANEL_READY 로 물어볼 최신 세션

// ════════════════════════════════════════════════════════════════════════════
// 사이드패널 탭 스코핑
//
// manifest의 side_panel.default_path를 두면 "모든 탭에 기본으로 열려 있는 전역
// 패널 인스턴스"가 되어(크롬 공식 side panel 가이드 및 커뮤니티에서 확인된 동작 —
// https://pmds.info/blog/chrome-extension-side-panel-per-tab), 탭별 enabled:false
// 로 막으려는 시도와 충돌한다. 그래서 manifest에서 default_path를 아예 제거하고,
// 서비스 워커 시작 시 전역으로 setOptions({enabled:false})를 걸어 "기본은 어떤
// 탭에도 패널이 없음"을 만든 다음, 검사를 요청한 탭에만 그때그때 enabled:true +
// path를 부여한다.
//
// 다만 이렇게 해도 완전한 "다른 탭으로 넘기면 즉시 닫힘"은 현재 크롬
// sidePanel API로는 보장되지 않는다 — enabled:false는 "그 탭에서 다시 열 수
// 없게" 만들 뿐, 이미 창에 도킹되어 열려 있는 패널을 강제로 닫는 sidePanel.close()
// 같은 API 자체가 아직 없다(W3C webextensions 이슈로 계속 요청 중:
// https://github.com/w3c/webextensions/issues/521, 크로미움 개발자 그룹 논의:
// https://groups.google.com/a/chromium.org/g/chromium-extensions/c/YAfMKV-GN4I).
// 현재 API로 달성 가능한 최선은 "다른 탭에서는 내용이 새지 않고 빈 대기 화면만
// 보이는 것"까지이고, 실제로 그렇게 동작한다. 패널이 물리적으로 닫히는 것은
// 크롬이 close()류 API를 추가하기 전까지는 확장 코드만으로 강제할 수 없다.
//
// "지금 패널이 켜져 있어야 하는 탭" 추적은 일반 JS 변수가 아니라 chrome.storage.session
// 에 저장한다 — MV3 서비스 워커는 ~30초 유휴 후 꺼졌다가 다음 이벤트에 다시 깨어나는데,
// 그때 모듈 전체가 재실행되면서 일반 변수는 null로 초기화돼버린다(검토 화면을 30초
// 넘게 보고 있다가 탭을 넘기면 흔히 발생). storage.session은 SW 재시작에도 값이
// 유지되므로 이 문제를 피한다.
// ════════════════════════════════════════════════════════════════════════════
const SIDEPANEL_PATH = 'sidepanel/sidepanel.html';
const PANEL_TAB_KEY = 'sidepanelActiveTabId';

async function getCurrentPanelTabId() {
  const obj = await chrome.storage.session.get(PANEL_TAB_KEY);
  return obj[PANEL_TAB_KEY] ?? null;
}
async function setCurrentPanelTabId(tabId) {
  if (tabId == null) await chrome.storage.session.remove(PANEL_TAB_KEY);
  else await chrome.storage.session.set({ [PANEL_TAB_KEY]: tabId });
}

function disablePanelForTab(tabId) {
  if (tabId == null) return Promise.resolve();
  return chrome.sidePanel?.setOptions?.({ tabId, enabled: false }).catch(() => {}) ?? Promise.resolve();
}

// 요청한 탭만 남기고 "그 순간 열려 있는 다른 모든 탭"을 즉시 비활성화한다.
// (이전엔 tabs.onActivated 로 "전환되는 순간"에 반응해서 껐는데, 크롬이 그 전환
// 시점에 이미 예전 상태로 패널을 표시하기로 결정해버리는 타이밍 경합 여지가 있다.
// 패널을 켜는 바로 그 순간 다른 탭들을 미리 다 꺼두면 그런 경합 자체가 없어진다.
// open() 의 제스처 보존과 무관하므로 비동기로 처리해도 된다 — OPEN_SIDE_PANEL
// 핸들러에서 await 없이 fire-and-forget으로 호출한다.)
async function switchPanelToTab(newTabId) {
  try {
    const tabs = await chrome.tabs.query({});
    await Promise.all(tabs.filter((t) => t.id !== newTabId).map((t) => disablePanelForTab(t.id)));
  } catch (_) {}
  await setCurrentPanelTabId(newTabId);
}

async function clearPanelTabIfMatches(tabId) {
  const current = await getCurrentPanelTabId();
  if (current === tabId) await setCurrentPanelTabId(null);
}

// 새로 활성화되는 탭이 "요청한 탭"이 아니면 그 자리에서 비활성화 — 탭을 넘기면
// 사이드패널이 닫힌다(크롬이 enabled:false 탭에선 패널을 자동으로 숨김).
chrome.tabs.onActivated?.addListener(async ({ tabId }) => {
  const current = await getCurrentPanelTabId();
  if (tabId !== current) disablePanelForTab(tabId);
});

// 탭이 아니라 브라우저 "창"을 전환하는 경우도 대비 — 다른 창의 활성 탭이 요청한
// 탭이 아니면 그 탭도 꺼준다.
chrome.windows.onFocusChanged?.addListener(async (windowId) => {
  if (windowId === chrome.windows.WINDOW_ID_NONE) return;
  try {
    const [tab] = await chrome.tabs.query({ active: true, windowId });
    if (!tab) return;
    const current = await getCurrentPanelTabId();
    if (tab.id !== current) disablePanelForTab(tab.id);
  } catch (_) {}
});

// 확장 설치/브라우저 시작 시점: 전역 기본값 자체를 비활성화(더 이상 manifest
// default_path가 없으니 이게 유일한 "기본은 꺼짐" 설정이다) + 이미 열려 있던
// 개별 탭들도 혹시 이전에 켜진 상태가 남아있을 수 있으니 전부 다시 비활성화.
async function disableAllExistingTabsPanel() {
  try {
    await chrome.sidePanel.setOptions({ enabled: false });
    const tabs = await chrome.tabs.query({});
    tabs.forEach((t) => disablePanelForTab(t.id));
    await setCurrentPanelTabId(null);
  } catch (_) {}
}

function pushToPanel(message) {
  // chrome.runtime.sendMessage는 특정 탭이 아니라 열려 있는 모든 확장 페이지(모든
  // 탭의 사이드패널 인스턴스 포함)에 전역 broadcast된다 — 그래서 반드시 message.tabId
  // 를 실어 보내고, 받는 쪽(sidepanel.js)이 자기 탭 것이 아니면 무시하게 해야 한다.
  // 아직 아무 패널도 안 열렸으면 조용히 무시된다.
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
// 메시지 핸들러
// ════════════════════════════════════════════════════════════════════════════

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  const type = message?.type;

  // content.js 가 자신의 tabId 를 (제스처 이전에) 캐싱하기 위해 요청
  if (type === 'GET_TAB_ID') {
    sendResponse({ tabId: sender?.tab?.id ?? null });
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
  if (type === 'PANEL_READY') {
    const requesterTabId = sender?.tab?.id ?? null;
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

  // 사이드패널의 HITL 결정 → 원본 탭 content.js 로 중계 (제스처 불필요)
  if (type === 'PANEL_DECISION') {
    const { sessionId, decision } = message;
    const session = sessions.get(sessionId);
    if (session?.tabId != null) {
      chrome.tabs.sendMessage(session.tabId, {
        type: 'PANEL_DECISION', sessionId, kind: session.kind, decision,
      }).catch(() => {});
      // 결정이 끝났으니 이 탭의 패널도 꺼둔다 — 나중에 이 탭으로 돌아와도
      // 끝난 검사 결과가 다시 뜨지 않게.
      disablePanelForTab(session.tabId);
      clearPanelTabIfMatches(session.tabId);
    }
    sessions.delete(sessionId);
    if (activeSessionId === sessionId) activeSessionId = null;
    sendResponse({ ok: true });
    return false;
  }

  // content.js: 제스처 시점에 사이드패널을 연다 (PLAN §변경1, 2026-07-23 정정 — 정식 1차 경로).
  // 메시지 왕복 1회 안에서 추가 await 없이 곧바로 호출해야 제스처가 보존된다(실측 확인).
  if (type === 'OPEN_SIDE_PANEL') {
    const tabId = sender?.tab?.id ?? message.tabId;
    if (tabId != null && chrome.sidePanel?.open) {
      // setOptions/switchPanelToTab 은 await 하지 않는다 — 바로 아래 open() 이 이
      // 메시지 핸들러의 동기 호출 스택 안에서 곧바로 실행돼야 제스처가 유지된다
      // (실측 확인, 위 주석 참고). 이전 탭 비활성화·추적 갱신은 open() 성패와
      // 무관하므로 fire-and-forget 으로 흘려보내도 안전하다.
      chrome.sidePanel.setOptions({ tabId, path: SIDEPANEL_PATH, enabled: true }).catch(() => {});
      switchPanelToTab(tabId);
      chrome.sidePanel.open({ tabId }).catch(() => {});
    }
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

// 설치/기동 시 서버 1회 탐지 + 사이드패널 동작 방식 설정
chrome.runtime.onInstalled?.addListener(() => {
  discoverServer().catch(() => {});
  disableAllExistingTabsPanel();
});
chrome.runtime.onStartup?.addListener(() => {
  discoverServer().catch(() => {});
  disableAllExistingTabsPanel();
});

// 툴바 아이콘 클릭은 action.default_popup(설정 전용)로 처리되므로, 사이드패널이
// 아이콘 클릭으로 열리지 않게 명시적으로 비활성화한다(PLAN §변경1/§변경2 분리).
chrome.sidePanel?.setPanelBehavior?.({ openPanelOnActionClick: false }).catch(() => {});
