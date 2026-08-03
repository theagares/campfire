'use strict';
/**
 * main/main.js — Electron 엔트리 (PLAN §8, Build Unit U4).
 *
 * 흐름:
 *   1) ConfigStore 로드 → EngineManager 생성 → IPC 등록
 *   2) 대시보드 BrowserWindow + 트레이 생성
 *   3) Python 엔진 사이드카 spawn (securityEnabled 면) → /health 폴링으로 포트 탐지
 *   4) 시스템 리소스/통계 주기 브로드캐스트
 *   5) 자동 업데이트 확인(패키징 시)
 *   6) 종료 시 사이드카까지 정리
 */

const path = require('path');
const { app, BrowserWindow, Menu } = require('electron');

const { ConfigStore } = require('./config-store');
const { EngineManager } = require('./engine-manager');
const { TrayController } = require('./tray');
const ipc = require('./ipc');
const systemMetrics = require('./system-metrics');
const { initAutoUpdater } = require('./updater');
const models = require('./models');

// 단일 인스턴스 (중복 실행 방지)
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
}

// campfire:// 커스텀 프로토콜 — 확장 프로그램 팝업의 "대시보드 열기"가 엔진 REST(HTML)
// 대신 이 앱 자체를 열 수 있게 등록한다. OS가 이 스킴을 이 exe로 라우팅하면(이미 떠있는
// 인스턴스가 있으므로) second-instance 이벤트가 뜨고, 그 핸들러가 대시보드 창을
// 포그라운드로 띄운다(아래 showDashboard). dev(언패키징) 모드는 electron 런처 자체가
// 아니라 스크립트 경로까지 execPath 인자로 같이 등록해야 한다(Electron 공식 패턴).
if (process.defaultApp) {
  if (process.argv.length >= 2) {
    app.setAsDefaultProtocolClient('campfire', process.execPath, [path.resolve(process.argv[1])]);
  }
} else {
  app.setAsDefaultProtocolClient('campfire');
}

let mainWindow = null;
let tray = null;
let engineManager = null;
let config = null;
let metricsTimer = null;
let statsTimer = null;
let isQuitting = false;

function createMainWindow() {
  // Windows: 타이틀바(~16-20px)/작업표시줄(~32-48px)가 서로 다른 해상도를 요청하는데
  // 단일 48px PNG 하나만 주면 OS가 다운스케일하면서 뭉개진다 — 20px+48px 를 모두 담은
  // 멀티 해상도 .ico 를 써서 각 컨텍스트가 자기 크기를 그대로 골라 쓰게 한다.
  const iconFile = process.platform === 'win32' ? 'icon.ico' : 'icon.png';
  mainWindow = new BrowserWindow({
    width: 1160,
    height: 760,
    minWidth: 940,
    minHeight: 620,
    backgroundColor: '#0f1115',
    show: false,
    title: 'Campfire',
    icon: path.join(__dirname, '..', 'assets', iconFile),
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  mainWindow.loadFile(path.join(__dirname, '..', 'renderer', 'index.html'));
  mainWindow.once('ready-to-show', () => mainWindow.show());

  // 창 닫기 = 트레이로 최소화 (앱은 계속 상주). 실제 종료는 트레이 QUIT.
  mainWindow.on('close', (e) => {
    if (!isQuitting) {
      e.preventDefault();
      mainWindow.hide();
    }
  });
  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

function showDashboard(view) {
  if (!mainWindow) createMainWindow();
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
  if (view) mainWindow.webContents.send('nav:goto', view);
}

function quitApp() {
  isQuitting = true;
  cleanup().finally(() => app.quit());
}

async function cleanup() {
  if (metricsTimer) clearInterval(metricsTimer);
  if (statsTimer) clearInterval(statsTimer);
  if (tray) tray.destroy();
  if (engineManager) await engineManager.dispose();
}

function startBroadcastLoops() {
  // CPU 사용률은 델타 계산이라 주기 샘플이 필요 → 2초마다 push
  metricsTimer = setInterval(() => {
    ipc.broadcast('metrics:tick', systemMetrics.sample());
  }, 2000);

  // 통계는 store 읽기라 다소 무거우므로 5초마다 push
  statsTimer = setInterval(() => {
    ipc.broadcast('stats:tick', ipc.buildStats(engineManager));
  }, 5000);
}

/** engineManager 가 'running' 상태가 될 때까지 대기(타임아웃 시 reject). */
function waitForEngineRunning(timeoutMs = 30000) {
  return new Promise((resolve, reject) => {
    if (engineManager.getStatus().state === 'running') return resolve();
    const timer = setTimeout(() => {
      engineManager.off('status', onStatus);
      reject(new Error('엔진 기동 대기 시간 초과'));
    }, timeoutMs);
    function onStatus(s) {
      if (s.state === 'running') {
        clearTimeout(timer);
        engineManager.off('status', onStatus);
        resolve();
      }
    }
    engineManager.on('status', onStatus);
  });
}

/**
 * 실 모델(encoder/llm_mcp, PII+인젝션) 가중치를 자동으로 준비한다.
 *
 * 룰베이스 폴백을 완전히 없앤 뒤에는 detector 가 encoder/llm_mcp 로 고정이라(엔진
 * config.py 기본값) 예전처럼 "rule_based 로 먼저 띄운 뒤 advanced 로 전환+재시작"
 * 하는 2단계 춤이 필요 없다. 가중치가 아직 없는 동안은 엔진의 model_status 게이트가
 * 검사 없이 통과시키고(§PLAN 9.2), 다운로드가 끝나면 그다음 실제 검사 요청에서
 * detector 가 알아서 실 모델을 스폰한다 — 엔진 재시작도 필요 없다. 이 함수는 그
 * 다운로드만 트리거하고 진행률을 브로드캐스트한다.
 */
async function ensureModelsAutoDownload() {
  if (!config.get('securityEnabled')) return; // 보안 보호가 꺼져 있으면 엔진 자체가 안 뜬다

  try {
    await waitForEngineRunning();
    const status = await models.getStatus(engineManager);
    const modelsReady = !!(status.pii?.ready && status.injection?.ready);
    if (modelsReady) return; // 이미 준비된 상태 — 재다운로드 불필요

    ipc.broadcast('models:fetchProgress', {
      type: 'progress',
      label: '필수 보안 모델(약 600MB)을 준비하는 중...',
    });
    await models.fetchModels(engineManager, (ev) => ipc.broadcast('models:fetchProgress', ev));
  } catch (err) {
    console.error('[main] 모델 자동 다운로드 실패(다음 실행에서 재시도):', err.message);
    ipc.broadcast('models:fetchProgress', {
      type: 'error',
      message: `필수 모델 자동 설치 실패: ${err.message} — 인터넷 연결을 확인하면 다음 실행 시 다시 시도합니다.`,
    });
  }
}

// 두 번째 실행 시도(트레이 아이콘 재클릭, campfire:// 딥링크 등)는 새 창을 띄우는 대신
// 이미 떠 있는 대시보드를 포그라운드로 가져온다.
app.on('second-instance', () => showDashboard('dashboard'));

// macOS는 커스텀 프로토콜을 second-instance가 아니라 open-url로 받는다(이미 실행 중이면).
// 콜드 스타트(앱이 아직 준비 전) 시엔 무시한다 — 어차피 whenReady()의 기본 흐름이 대시보드를
// 띄우고, showDashboard()가 기대하는 BrowserWindow API는 app.isReady() 전엔 쓸 수 없다.
app.on('open-url', (event) => {
  event.preventDefault();
  if (!app.isReady()) return;
  showDashboard('dashboard');
});

app.whenReady().then(async () => {
  // File/Edit/View/Window/Help 기본 메뉴바 제거 (Figma 디자인엔 없음, 대시보드는 자체 nav 사용)
  Menu.setApplicationMenu(null);

  config = new ConfigStore(app.getPath('userData'));
  engineManager = new EngineManager(app, config);

  ipc.register({
    engineManager,
    config,
    onShowDashboard: () => showDashboard('dashboard'),
    onQuit: quitApp,
  });

  createMainWindow();

  tray = new TrayController(app, {
    onShowDashboard: () => showDashboard('dashboard'),
    onQuit: quitApp,
  });
  tray.create();

  // CI 전용: macOS 러너에서 트레이 팝오버를 스크린샷으로 검증하기 위한 훅(§SECUREDOC_* 환경변수
  // 관례). 일반 실행에선 설정하지 않으므로 아무 영향 없음.
  if (process.env.SECUREDOC_E2E_OPEN_TRAY) {
    setTimeout(() => tray.togglePopover(), 1500);
  }

  startBroadcastLoops();

  // 엔진 사이드카 기동 (securityEnabled=false 면 disabled 로 남음)
  engineManager.start().catch((err) => console.error('[main] 엔진 start 실패:', err.message));

  // 설치 직후 자동으로 실 ML 모델(advanced)을 준비 — 엔진 기동과 별도로 백그라운드 진행.
  ensureModelsAutoDownload().catch((err) => console.error('[main] 자동 모델 설치 오류:', err.message));

  initAutoUpdater(app);

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createMainWindow();
    else showDashboard();
  });
});

// 트레이 상주 앱: 모든 창을 닫아도 종료하지 않음 (QUIT 만 종료)
app.on('window-all-closed', () => {
  // no-op (트레이 유지)
});

app.on('before-quit', () => {
  isQuitting = true;
  // 엔진 SSE 구독을 먼저 끊는다 — 안 끊으면 재연결 타이머가 종료를 붙잡는다.
  ipc.disposeActivity();
});

app.on('will-quit', (e) => {
  if (engineManager && engineManager.child) {
    e.preventDefault();
    cleanup().finally(() => {
      engineManager = null;
      app.quit();
    });
  }
});
