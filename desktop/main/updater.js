'use strict';
/**
 * main/updater.js — 자동 업데이트 (PLAN §8, electron-updater).
 *
 * 실제 업데이트 서버 URL/인증은 package.json build.publish 의 placeholder + CI 환경변수로만
 * 주입한다. 소스에 토큰/키 하드코딩 금지. dev 및 publish 미설정 시엔 no-op.
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
