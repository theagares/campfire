/**
 * popup.js  ─  설정 전용 popup (PLAN §변경2)
 *
 * 표시 항목(읽기 전용):
 *   - 서버 설정: 연결 대상(로컬/원격), 로컬 포트(자동 탐지, 편집 불가)
 *   - 앱 설정: 엔진 연결 상태, 대시보드 열기, 앱 미설치 안내
 * 원격 URL 편집란·보호 토글·범위 카드·최근 활동 카드는 두지 않는다.
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
  const installNote = $('install-note');

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
  if (isLocal && ok) {
    dashBtn.disabled = false;
    installNote.hidden = true;
  } else {
    dashBtn.disabled = true;
    installNote.hidden = false;
  }
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
  // 로컬 엔진이 뜬 포트로 앱 대시보드를 연다(앱이 해당 포트에 UI를 서빙하는 전제).
  if (lastInfo?.target === 'local' && lastInfo?.baseUrl) {
    chrome.tabs.create({ url: `${lastInfo.baseUrl}/` });
  }
});

$('rescan').addEventListener('click', async () => {
  $('target-value').textContent = '다시 탐지 중…';
  render(await requestInfo('RESCAN_SERVER'));
});

(async () => { render(await requestInfo('GET_CONNECTION_INFO')); })();
