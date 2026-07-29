'use strict';
/**
 * main/ipc.js
 * ipcMain 핸들러 등록 + 실시간 push 브로드캐스트.
 */

const { ipcMain, BrowserWindow, shell, app } = require('electron');
const systemMetrics = require('./system-metrics');
const engineStats = require('./engine-stats');
const { getConnections } = require('./connections');

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
  ipcMain.handle('models:status', async () => {
    const base = engineManager.getStatus().baseUrl;
    if (!base) return { pii: { ready: false }, injection: { ready: false } };
    try {
      const res = await fetch(`${base}/models/status`);
      return await res.json();
    } catch {
      return { pii: { ready: false }, injection: { ready: false } };
    }
  });

  ipcMain.handle('models:fetch', async () => {
    const base = engineManager.getStatus().baseUrl;
    if (!base) throw new Error('엔진이 실행 중이 아닙니다');

    const startRes = await fetch(`${base}/models/fetch`, { method: 'POST' });
    if (!startRes.ok) throw new Error(`모델 다운로드 시작 실패 (${startRes.status})`);
    const { jobId } = await startRes.json();

    let after = 0;
    for (;;) {
      const evRes = await fetch(`${base}/jobs/${jobId}/events?after=${after}`);
      if (!evRes.ok) throw new Error(`진행 상태 조회 실패 (${evRes.status})`);
      const payload = await evRes.json();
      for (const ev of payload.events || []) {
        after = Math.max(after, ev.seq || after);
        broadcast('models:fetchProgress', ev);
        if (ev.type === 'done') return ev.result;
        if (ev.type === 'error') throw new Error(ev.message || '모델 다운로드 실패');
      }
      if (payload.done) return null;
      await new Promise((r) => setTimeout(r, 500));
    }
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
