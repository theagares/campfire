/**
 * popup.js  ─  설정 전용 popup (PLAN §변경2)
 *
 * 표시 항목:
 *   - 헤더: 보호 on/off — 프롬프트·문서 인터셉트를 통째로 켜고 끈다.
 *     content.js/interceptor.js가 chrome.storage.local의 protectionEnabled /
 *     fileInterceptEnabled 를 구독하고, 이 팝업은 그 값을 읽고 쓰기만 한다.
 *   - 서버 설정(읽기 전용): 연결 대상(로컬/원격), 로컬 포트(자동 탐지, 편집 불가), 다시 탐지
 *   - 앱 설정: 엔진 연결 상태, 대시보드 열기(upsecurity:// 커스텀 프로토콜로 데스크탑 앱 자체를 연다)
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
  const dashBtn = $('open-dashboard');

  const isLocal = info?.target === 'local';
  const ok = !!info?.ok;

  targetEl.textContent = info?.target === 'local' ? '로컬 (앱 엔진)'
    : info?.target === 'remote' ? '원격 (AWS 폴백)'
    : '알 수 없음';
  portEl.textContent = isLocal && info?.port ? String(info.port) : '해당 없음';

  pill.classList.remove('ok', 'off');
  if (ok) { pill.classList.add('ok'); engineText.textContent = '연결됨'; }
  else { pill.classList.add('off'); engineText.textContent = '미연결'; }

  if (ok && info?.health) {
    const h = info.health;
    detail.textContent = `버전 ${h.version || '?'} · 정책 ${h.injectionPolicy || '?'} · detector ${(h.detectors ? Object.values(h.detectors).join('/') : '?')}`;
  } else if (info?.baseUrl) {
    detail.textContent = info.baseUrl;
  } else {
    detail.textContent = '';
  }

  // 로컬 앱이 연결됐을 때만 대시보드 열기 가능
  dashBtn.disabled = !(isLocal && ok);
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
  // 엔진 REST(baseUrl)가 아니라 데스크탑 앱 자체를 연다 — 앱이 upsecurity:// 를
  // 기본 프로토콜로 등록해두고(main.js), OS가 이 스킴을 열면 이미 떠 있는 앱 인스턴스의
  // second-instance 이벤트가 대시보드 창을 포그라운드로 띄운다(main.js showDashboard).
  if (lastInfo?.target === 'local' && lastInfo?.ok) {
    chrome.tabs.create({ url: 'upsecurity://dashboard' });
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

(async () => { render(await requestInfo('GET_CONNECTION_INFO')); })();
