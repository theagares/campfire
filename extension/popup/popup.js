/**
 * popup.js  ─  설정 전용 popup (PLAN §변경2)
 *
 * 표시 항목:
 *   - 헤더: 보호 on/off — 프롬프트·문서 인터셉트를 통째로 켜고 끈다.
 *     content.js/interceptor.js가 chrome.storage.local의 protectionEnabled /
 *     fileInterceptEnabled 를 구독하고, 이 팝업은 그 값을 읽고 쓰기만 한다.
 *   - 서버 설정(읽기 전용): 연결 대상(로컬/원격), 로컬 포트(자동 탐지, 편집 불가), 다시 탐지
 *   - 앱 설정: 엔진 연결 상태, 대시보드 열기(campfire:// 커스텀 프로토콜로 데스크탑 앱 자체를 연다)
 * 원격 URL 편집란·범위 카드·최근 활동 카드는 두지 않는다.
 */

const $ = (id) => document.getElementById(id);

let lastInfo = null;

function render(info) {
  lastInfo = info;
  const targetEl = $('target-value');
  const portEl = $('port-value');
  const pill = $('engine-pill');
  const engineText = $('engine-text');
  const detail = $('engine-detail');
  const routePill = $('route-pill');
  const routeText = $('route-text');
  const routeDetail = $('route-detail');
  const dashBtn = $('open-dashboard');

  const isLocal = info?.target === 'local';
  const ok = !!info?.ok;

  // 로컬 엔진이 없으면 검사 없이 전송하지 않는다(원격 폴백 제거 — service-worker.js
  // discoverServer 주석 참고). 그래서 표시는 '로컬' 아니면 '연결 안 됨' 둘뿐이다.
  targetEl.textContent = isLocal ? '로컬 (앱 엔진)' : '연결 안 됨';
  portEl.textContent = isLocal && info?.port ? String(info.port) : '해당 없음';

  pill.classList.remove('ok', 'off', 'warn');
  if (ok) { pill.classList.add('ok'); engineText.textContent = '연결됨'; }
  // 앱은 떠 있는데 규칙을 아직 못 받았다(맥 키체인 승인 대기) — 꺼진 것과 구분해 보여 준다.
  else if (info?.waiting) { pill.classList.add('warn'); engineText.textContent = '키체인 승인 대기'; }
  else { pill.classList.add('off'); engineText.textContent = '미연결'; }

  if (ok && info?.health) {
    const h = info.health;
    detail.textContent = `버전 ${h.version || '?'} · 정책 ${h.injectionPolicy || '?'} · detector ${(h.detectors ? Object.values(h.detectors).join('/') : '?')}`;
    // 라이트 판 엔진에는 프록시가 없다 — 보호 경로(프록시/확장) 줄을 보이지 않는다.
    const lite = h.edition === 'lite';
    routePill.closest('.row').style.display = lite ? 'none' : '';
    routeDetail.style.display = lite ? 'none' : '';
    const route = h.extensionRoute || { mode: 'unknown', activeTabs: 0 };
    const routeLabels = {
      proxy: '프록시 확인됨',
      extension: '확장 보호 중',
      fallback: '확장으로 복구',
      off: '보호 꺼짐',
      mixed: '탭별 경로 다름',
      unknown: 'AI 탭 확인 대기',
    };
    routePill.classList.remove('ok', 'off', 'warn');
    routePill.classList.add(
      route.mode === 'proxy' || route.mode === 'extension' ? 'ok'
        : route.mode === 'fallback' || route.mode === 'mixed' || route.mode === 'off' ? 'warn' : 'off',
    );
    routeText.textContent = routeLabels[route.mode] || routeLabels.unknown;
    if (route.mode === 'fallback') {
      routeDetail.textContent = '프록시 표식이 없어 확장 보호를 유지합니다.';
    } else if (route.mode === 'mixed') {
      routeDetail.textContent = '열린 AI 탭마다 경로가 달라 새로고침이 필요합니다.';
    } else if (route.mode === 'off') {
      routeDetail.textContent = '확장 보호가 꺼져 있고 프록시 경로도 확인되지 않았습니다.';
    } else if (route.mode === 'unknown') {
      routeDetail.textContent = 'AI 사이트를 열면 실제 경로를 자동 확인합니다.';
    } else {
      routeDetail.textContent = `열린 AI 탭 ${route.activeTabs || 1}개에서 확인됨`;
    }
  } else if (info?.baseUrl) {
    detail.textContent = info.baseUrl;
    routePill.className = 'status-pill off';
    routeText.textContent = '확인 불가';
    routeDetail.textContent = '';
  } else {
    detail.textContent = info?.waiting
      ? "macOS 키체인 승인 창('Campfire Safe Storage')에서 '항상 허용'을 누르면 연결됩니다. 그동안 검사 없이 전송하지 않습니다."
      : '';
    routePill.className = 'status-pill off';
    routeText.textContent = '확인 불가';
    routeDetail.textContent = '';
  }

  // 로컬 앱이 떠 있을 때만 대시보드 열기 가능(키체인 대기 중에도 앱은 떠 있다 — 승인 창이
  // 다른 창 뒤에 가려졌을 때 앱을 앞으로 꺼내는 데 쓴다)
  dashBtn.disabled = !(isLocal && (ok || info?.waiting));
}

function requestInfo(type) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage({ type }, (res) => {
      if (chrome.runtime.lastError) { resolve({ ok: false }); return; }
      resolve(res || { ok: false });
    });
  });
}

$('open-dashboard').addEventListener('click', () => {
  // 엔진 REST(baseUrl)가 아니라 데스크탑 앱 자체를 연다 — 앱이 campfire:// 를
  // 기본 프로토콜로 등록해두고(main.js), OS가 이 스킴을 열면 이미 떠 있는 앱 인스턴스의
  // second-instance 이벤트가 대시보드 창을 포그라운드로 띄운다(main.js showDashboard).
  if (lastInfo?.target === 'local' && lastInfo?.ok) {
    chrome.tabs.create({ url: 'campfire://dashboard' });
  }
});

$('rescan').addEventListener('click', async () => {
  $('target-value').textContent = '다시 탐지 중…';
  render(await requestInfo('RESCAN_SERVER'));
});

function renderProtection(enabled) {
  const toggle = $('protection-toggle');
  toggle.checked = enabled;
  $('protection-label').textContent = enabled ? 'ON' : 'OFF';
}

// 이 토글은 "보호 전체" 스위치다 — 끄면 검사뿐 아니라 인터셉트 자체가 멈춰야 한다.
// 그래서 두 키를 함께 쓴다:
//   protectionEnabled     — 프롬프트 전송 가로채기까지 포함한 전체 보호
//   fileInterceptEnabled  — 파일(문서) 레이어
// 예전에는 이 토글이 fileInterceptEnabled 만 썼고 protectionEnabled 는 아무도 쓰지
// 않아 항상 true 였다(데스크탑 앱의 ON/OFF 토글이 제거되면서 writer 가 사라졌다).
// 그 결과 토글을 꺼도 프롬프트는 계속 가로채였다 — 실사용자 리포트.
function setProtection(enabled) {
  chrome.storage?.local?.set?.({ protectionEnabled: enabled, fileInterceptEnabled: enabled });
}

$('protection-toggle').addEventListener('change', (e) => {
  const enabled = e.target.checked;
  renderProtection(enabled);
  setProtection(enabled);
});

chrome.storage?.local?.get?.(
  { protectionEnabled: true, fileInterceptEnabled: true },
  ({ protectionEnabled, fileInterceptEnabled }) => {
    // 둘 중 하나라도 꺼져 있으면 꺼진 것으로 본다(예전 버전에서 fileInterceptEnabled
    // 만 꺼둔 채 업데이트된 사용자도 화면과 실제 동작이 어긋나지 않게).
    renderProtection(Boolean(protectionEnabled) && Boolean(fileInterceptEnabled));
  },
);

async function refresh() { render(await requestInfo('GET_CONNECTION_INFO')); }
refresh();
setInterval(refresh, 2000);
