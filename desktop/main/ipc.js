'use strict';
/**
 * main/ipc.js
 * ipcMain 핸들러 등록 + 실시간 push 브로드캐스트.
 */

const { ipcMain, BrowserWindow, shell, app } = require('electron');
const systemMetrics = require('./system-metrics');
const engineStats = require('./engine-stats');
const { getConnections } = require('./connections');
const models = require('./models');
const mcpClients = require('./mcp-clients');
const paths = require('./paths');

/** 모든 창(대시보드+트레이)에 이벤트 push */
function broadcast(channel, payload) {
  for (const win of BrowserWindow.getAllWindows()) {
    if (!win.isDestroyed()) {
      win.webContents.send(channel, payload);
    }
  }
}

/** /health 응답에서 모델 상태 pill(§8) 파생 */
function deriveModelStatus(engineStatus) {
  const running = engineStatus.state === 'running';
  const detectors = (engineStatus.health && engineStatus.health.detectors) || {};
  return {
    pii: {
      name: detectors.pii || 'rule_based',
      ready: running,
      label: running ? '작동 중' : '중지됨',
    },
    injection: {
      name: detectors.injection || 'rule_based',
      ready: running,
      label: running ? '작동 중' : '중지됨',
    },
  };
}

/** 통계 조회 (엔진 store 실측 + /health 파생 모델 상태 결합) */
function buildStats(engineManager) {
  const engineStatus = engineManager.getStatus();
  const stats = engineStats.readStats(app);
  return {
    ...stats,
    models: deriveModelStatus(engineStatus),
    injectionPolicy:
      (engineStatus.health && engineStatus.health.injectionPolicy) || null,
    engineState: engineStatus.state,
  };
}

function register(ctx) {
  const { engineManager, config, onShowDashboard, onQuit } = ctx;

  ipcMain.handle('app:info', () => ({
    version: app.getVersion(),
    platform: process.platform,
    isPackaged: app.isPackaged,
    dev: process.argv.includes('--dev') || !app.isPackaged,
  }));

  ipcMain.handle('engine:status', () => engineManager.getStatus());

  ipcMain.handle('engine:restart', async () => {
    await engineManager.restart();
    return engineManager.getStatus();
  });

  ipcMain.handle('engine:setSecurity', async (_e, enabled) => {
    await engineManager.setSecurityEnabled(!!enabled);
    return engineManager.getStatus();
  });

  ipcMain.handle('stats:get', () => buildStats(engineManager));

  ipcMain.handle('metrics:get', () => systemMetrics.sample());

  ipcMain.handle('settings:get', () => config.get());

  ipcMain.handle('settings:set', async (_e, patch) => {
    const prev = config.get();
    const next = config.set(patch || {});
    // 인젝션 정책 / detector 선택 변경 → 엔진 재시작으로 env 반영 (엔진엔 REST 쓰기 없음)
    const policyChanged = patch && patch.injectionPolicy && patch.injectionPolicy !== prev.injectionPolicy;
    const detectorChanged =
      patch &&
      ((patch.piiDetector && patch.piiDetector !== prev.piiDetector) ||
        (patch.injectionDetector && patch.injectionDetector !== prev.injectionDetector));
    if (policyChanged || detectorChanged) {
      if (config.get('securityEnabled')) {
        engineManager.restart().catch((err) => console.error('[ipc] restart 실패:', err.message));
      }
    }
    return next;
  });

  // ── 모델 가중치 상태 조회 / 다운로드(엔진 REST 프록시, PLAN 모델 배포 B안) ──────
  ipcMain.handle('models:status', () => models.getStatus(engineManager));

  ipcMain.handle('models:fetch', () =>
    models.fetchModels(engineManager, (ev) => broadcast('models:fetchProgress', ev))
  );

  // ── 확장 프로그램 폴더(설치 파일에 함께 번들됨 — chrome://extensions 에서 그대로 로드) ──
  ipcMain.handle('extension:openFolder', async () => {
    const dir = paths.resolveExtensionDir(app);
    const err = await shell.openPath(dir);
    if (err) throw new Error(`폴더를 열 수 없습니다: ${err}`);
    return dir;
  });

  // ── MCP 클라이언트 원클릭 연결(연결 화면) ──────────────────────────────────
  ipcMain.handle('mcp:detectClients', async () => {
    const base = engineManager.getStatus().baseUrl;
    const mcpUrl = base ? `${base}/mcp` : null;
    return { mcpUrl, clients: mcpUrl ? await mcpClients.detectClients(app, mcpUrl) : [] };
  });

  ipcMain.handle('mcp:connect', async (_e, clientId) => {
    const base = engineManager.getStatus().baseUrl;
    if (!base) throw new Error('엔진이 실행 중이 아닙니다');
    await mcpClients.connect(app, clientId, `${base}/mcp`);
    return true;
  });

  ipcMain.handle('mcp:disconnect', async (_e, clientId) => {
    await mcpClients.disconnect(app, clientId);
    return true;
  });

  ipcMain.handle('settings:setPipelineLayout', (_e, layout) => {
    config.setPipelineLayout(layout);
    return true;
  });

  ipcMain.handle('connections:get', async () => getConnections(engineManager.getStatus()));

  ipcMain.handle('window:showDashboard', () => {
    if (typeof onShowDashboard === 'function') onShowDashboard();
    return true;
  });

  ipcMain.handle('app:quit', () => {
    if (typeof onQuit === 'function') onQuit();
    return true;
  });

  ipcMain.handle('external:open', (_e, url) => {
    // http/https 만 허용 (안전)
    if (typeof url === 'string' && /^https?:\/\//i.test(url)) {
      shell.openExternal(url);
      return true;
    }
    return false;
  });

  // 엔진 상태 변화 → 즉시 브로드캐스트
  engineManager.on('status', (status) => broadcast('engine:status', status));
}

module.exports = { register, broadcast, buildStats };
