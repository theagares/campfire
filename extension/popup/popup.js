/**
 * popup.js  ─  설정 전용 popup (PLAN §변경2)
 *
 * 표시 항목:
 *   - 헤더: 파일 인터셉트 on/off (content.js/interceptor.js가 chrome.storage.local의
 *     fileInterceptEnabled를 그대로 구독 — 이 팝업은 그 값을 읽고 쓰기만 한다)
 *   - 서버 설정(읽기 전용): 연결 대상(로컬/원격), 로컬 포트(자동 탐지, 편집 불가), 다시 탐지
 *   - 앱 설정: 엔진 연결 상태, 대시보드 열기(campfire:// 커스텀 프로토콜로 데스크탑 앱 자체를 연다)
 * 원격 URL 편집란·범위 카드·최근 활동 카드는 두지 않는다. 텍스트 프롬프트 검사까지
 * 포함한 전체 보호(protectionEnabled)를 끄는 토글도 의도적으로 두지 않는다 — 이 팝업이
 * 다루는 건 파일 인터셉트 하나뿐이다.
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

function renderFileIntercept(enabled) {
  const toggle = $('file-intercept-toggle');
  toggle.checked = enabled;
  $('file-intercept-label').textContent = enabled ? 'ON' : 'OFF';
}

$('file-intercept-toggle').addEventListener('change', (e) => {
  const enabled = e.target.checked;
  renderFileIntercept(enabled);
  chrome.storage?.local?.set?.({ fileInterceptEnabled: enabled });
});

chrome.storage?.local?.get?.({ fileInterceptEnabled: true }, ({ fileInterceptEnabled }) => {
  renderFileIntercept(Boolean(fileInterceptEnabled));
});

(async () => { render(await requestInfo('GET_CONNECTION_INFO')); })();
