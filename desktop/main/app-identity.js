'use strict';

const { build } = require('../package.json');

const APP_USER_MODEL_ID = build.appId;

/**
 * Windows 알림 센터는 실행 중인 프로세스의 AppUserModelID가 설치 바로가기의
 * AppUserModelID(electron-builder appId)와 같아야 Electron 알림을 표시한다.
 * 앱이 준비되기 전에 설정해야 첫 프록시 차단 알림도 올바른 발신자로 등록된다.
 */
function configureAppIdentity(app, platform = process.platform) {
  if (platform !== 'win32') return false;
  app.setAppUserModelId(APP_USER_MODEL_ID);
  return true;
}

module.exports = { APP_USER_MODEL_ID, configureAppIdentity };
