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

// 단일 인스턴스 (중복 실행 방지)
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
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
    title: 'UpSecurity',
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

app.on('second-instance', () => showDashboard());

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
