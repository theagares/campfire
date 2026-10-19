'use strict';
/**
 * main/updater.js — 자동 업데이트 (PLAN §8, electron-updater).
 *
 * package.json build.publish 가 GitHub Releases(theagares/campfire)를 가리키므로
 * 그 저장소에 릴리스가 올라가 있으면 그대로 동작한다. 업로드용 인증 토큰(GH_TOKEN)은
 * CI 환경변수로만 주입하고 소스에 넣지 않는다. dev 모드에선 no-op.
 *
 * 받은 업데이트는 앱을 "종료" 할 때 설치된다. 그런데 이 앱은 창을 닫아도 트레이에 남아 종료되지
 * 않아서, 받아 둔 채 며칠이고 설치되지 않았다(실측: v0.2.40 을 받아 두고 v0.2.33 로 계속 실행,
 * 화면엔 아무 안내도 없었다). 그래서 받으면 대시보드·트레이에 "다시 시작해 업데이트" 를 띄우고,
 * 켜 둔 동안에도 주기적으로 확인한다.
 * 설치는 소리 없이(/S) 돌리지 않는다 — 설치가 4~7분 걸리는 동안 아무것도 안 보였다(실측: v0.2.40→0.2.42).
 * 설치기의 진행 창을 띄우고, 업데이트일 때 건너뛸 설치기 화면은 build/installer.nsh 가 정한다.
 * macOS 는 자동 설치가 안 된다(dmg 만 있고 ad-hoc 서명 — electron-updater mac 은 zip + 정식 서명 필요).
 * 새 버전이 있으면 받지 않고 내려받을 페이지를 연다.
 */

const CHECK_INTERVAL_MS = 4 * 60 * 60 * 1000; // 켜 둔 동안 다시 확인하는 간격
// 받을 버전의 릴리스 페이지. releases/latest 는 정식 판만 가리켜, 라이트(Pre-release) 사용자가 정식 판을 받게 된다.
const RELEASES_URL = 'https://github.com/theagares/campfire/releases/tag/v';

/**
 * 업데이트 상태를 들고 화면에 알린다. electron-updater 는 주입받는다(테스트에서 가짜로 바꾼다).
 * 상태: { ready: 받아서 설치만 남음, manual: 직접 받아야 함(mac), version, url }
 */
function createUpdater({ autoUpdater, platform = process.platform, broadcast = () => {}, every = setInterval }) {
  const manual = platform === 'darwin';
  let state = { ready: false, manual: false, version: null, url: null };
  const set = (patch) => {
    state = { ...state, ...patch };
    broadcast('updater:state', state);
  };

  autoUpdater.autoDownload = !manual;
  // electron-updater 의 "종료할 때 설치" 는 소리 없이만 돈다 — 대신 종료 마지막에 installOnQuit() 을 부른다
  autoUpdater.autoInstallOnAppQuit = false;
  autoUpdater.on('error', (e) => console.error('[updater] error:', e && e.message));
  autoUpdater.on('update-not-available', () => console.log('[updater] 최신 버전'));
  autoUpdater.on('update-available', (i) => {
    console.log('[updater] 업데이트 있음:', i.version);
    if (manual) set({ manual: true, version: i.version, url: RELEASES_URL + i.version });
  });
  autoUpdater.on('update-downloaded', (i) => {
    console.log('[updater] 다운로드 완료:', i.version, '— 다시 시작하면 적용');
    set({ ready: true, version: i.version });
  });

  function check() {
    if (state.ready) return; // 이미 받아 둠 — 설치만 남았다
    try {
      Promise.resolve(autoUpdater.checkForUpdates()).catch((err) => console.log('[updater] 확인 실패:', err && err.message));
    } catch (err) {
      console.log('[updater] 확인 실패:', err && err.message);
    }
  }

  let installing = false;
  const run = (fn) => {
    if (!state.ready || installing) return false;
    installing = true;
    fn();
    return true;
  };

  return {
    start() {
      check();
      const timer = every(check, CHECK_INTERVAL_MS);
      if (timer && timer.unref) timer.unref(); // 이 타이머 때문에 종료가 늦어지지 않게
    },
    getState: () => state,
    /**
     * 받아 둔 업데이트를 설치하고 앱을 다시 연다(설치기 진행 창이 보인다). 받은 게 없으면 false.
     * 소리 없이가 아니면 electron-updater 는 둘째 인자 대신 autoRunAppAfterInstall(기본 true)로 --force-run 을 준다.
     */
    install: () => run(() => autoUpdater.quitAndInstall(false, true)),
    /** 앱을 끄는 마지막에 받아 둔 업데이트를 설치한다(진행 창만 보이고 앱은 다시 열지 않는다). */
    installOnQuit: () => run(() => autoUpdater.install(false, false)),
  };
}

function initAutoUpdater(app, { broadcast } = {}) {
  if (!app.isPackaged) {
    console.log('[updater] dev 모드 — 자동 업데이트 비활성');
    return null;
  }
  let autoUpdater;
  try {
    // eslint-disable-next-line global-require
    ({ autoUpdater } = require('electron-updater'));
  } catch (err) {
    console.log('[updater] electron-updater 로드 실패 — 건너뜀:', err.message);
    return null;
  }
  const updater = createUpdater({ autoUpdater, broadcast });
  updater.start();
  return updater;
}

module.exports = { initAutoUpdater, createUpdater, CHECK_INTERVAL_MS, RELEASES_URL };
