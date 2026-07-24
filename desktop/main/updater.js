'use strict';
/**
 * main/updater.js — 자동 업데이트 (PLAN §8, electron-updater).
 *
 * package.json build.publish 가 GitHub Releases(theagares/securedoc-gateway)를 가리키므로
 * 그 저장소에 릴리스가 올라가 있으면 그대로 동작한다. 업로드용 인증 토큰(GH_TOKEN)은
 * CI 환경변수로만 주입하고 소스에 넣지 않는다. dev 모드에선 no-op.
 */

function initAutoUpdater(app) {
  if (!app.isPackaged) {
    console.log('[updater] dev 모드 — 자동 업데이트 비활성');
    return;
  }
  let autoUpdater;
  try {
    // eslint-disable-next-line global-require
    ({ autoUpdater } = require('electron-updater'));
  } catch (err) {
    console.log('[updater] electron-updater 로드 실패 — 건너뜀:', err.message);
    return;
  }

  autoUpdater.autoDownload = true;
  autoUpdater.on('error', (e) => console.error('[updater] error:', e && e.message));
  autoUpdater.on('update-available', (i) => console.log('[updater] 업데이트 있음:', i.version));
  autoUpdater.on('update-not-available', () => console.log('[updater] 최신 버전'));
  autoUpdater.on('update-downloaded', (i) => {
    console.log('[updater] 다운로드 완료:', i.version, '— 다음 재시작 시 적용');
  });

  try {
    autoUpdater.checkForUpdatesAndNotify();
  } catch (err) {
    // publish 설정(placeholder)이 실제 서버가 아니면 여기서 조용히 실패 — 정상.
    console.log('[updater] 업데이트 확인 실패(placeholder 서버 예상):', err.message);
  }
}

module.exports = { initAutoUpdater };
