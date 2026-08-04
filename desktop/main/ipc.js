'use strict';
/**
 * main/ipc.js
 * ipcMain 핸들러 등록 + 실시간 push 브로드캐스트.
 */

const { ipcMain, BrowserWindow, shell, app } = require('electron');
const systemMetrics = require('./system-metrics');
const engineStats = require('./engine-stats');
const models = require('./models');
const mcpClients = require('./mcp-clients');
const paths = require('./paths');
const { PipelineActivity } = require('./pipeline-activity');

let pipelineActivity = null;
const busyListeners = new Set();

/** 모든 창(대시보드+트레이)에 이벤트 push */
function broadcast(channel, payload) {
  for (const win of BrowserWindow.getAllWindows()) {
    if (!win.isDestroyed()) {
      win.webContents.send(channel, payload);
    }
  }
}

/**
 * /health 응답에서 모델 상태 pill(§8) 파생.
 * "ready"는 엔진이 떠 있는지가 아니라 **실제 가중치가 준비돼 진짜 탐지가 동작하는지**를
 * 뜻한다. 룰베이스 폴백을 없앤 뒤에는 detectors 이름이 항상 pii_encoder/injection_llm_mcp
 * 로 고정이라 이름만으로는 준비 여부를 알 수 없다 — 엔진이 /health 에 실어주는
 * modelsReady(가중치 파일 존재 여부, app.core.model_status)를 그대로 쓴다. 가중치가 없는
 * 동안은 엔진이 죽지 않고 검사만 통과 처리하므로(§PLAN 9.2), pill 은 "준비 중"으로
 * 보여준다.
 */
function deriveModelStatus(engineStatus) {
  const running = engineStatus.state === 'running';
  const detectors = (engineStatus.health && engineStatus.health.detectors) || {};
  const modelsReady = (engineStatus.health && engineStatus.health.modelsReady) || {};
  const piiActive = running && !!modelsReady.pii;
  const injectionActive = running && !!modelsReady.injection;
  return {
    pii: {
      name: detectors.pii || 'pii_encoder',
      ready: piiActive,
      label: !running ? '중지됨' : piiActive ? '작동 중' : '준비 중',
    },
    injection: {
      name: detectors.injection || 'injection_llm_mcp',
      ready: injectionActive,
      label: !running ? '중지됨' : injectionActive ? '작동 중' : '준비 중',
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
    // upstageApiKey 는 사용자가 빈 문자열로 "지우기"도 할 수 있어야 하므로(patch 에 값이
    // undefined 가 아니라 '' 로 명시적으로 실려온 경우도 변경으로 간주) 다른 필드처럼
    // truthy 체크만 하면 "키 지우기"가 재시작을 못 일으켜 이전 env 가 그대로 남는다.
    const apiKeyChanged =
      patch && patch.upstageApiKey !== undefined && patch.upstageApiKey !== prev.upstageApiKey;
    if (policyChanged || detectorChanged || apiKeyChanged) {
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

  // 처리현황(파이프라인 단계) 실시간 구독 → 렌더러로 push.
  // 엔진 SSE 를 메인이 중계한다(렌더러는 엔진 포트를 모르고, contextIsolation 아래
  // 화이트리스트 API 만 쓴다). 구버전 엔진이면 404 로 조용히 물러난다.
  pipelineActivity = new PipelineActivity(engineManager);
  pipelineActivity.on('activity', (payload) => broadcast('pipeline:activity', payload));
  pipelineActivity.on('busy', (busy) => busyListeners.forEach((cb) => cb(busy)));
  pipelineActivity.start();
}

/** "지금 검사 중인가" 구독. 트레이 불꽃이 세기를 바꾸는 데 쓴다.
 *
 *  콜백 등록을 따로 두는 이유: register() 는 트레이보다 먼저 불린다(main.js). 그래서
 *  트레이를 register 인자로 넘길 수가 없어, 만들어진 뒤에 붙이는 형태로 뺐다. */
function onPipelineBusy(cb) {
  busyListeners.add(cb);
  if (pipelineActivity) cb(pipelineActivity.busy); // 현재 상태를 먼저 한 번
  return () => busyListeners.delete(cb);
}

/** 앱 종료 시 SSE 연결 정리 (main.js 에서 호출). */
function disposeActivity() {
  if (pipelineActivity) {
    pipelineActivity.stop();
    pipelineActivity = null;
  }
  busyListeners.clear();
}

module.exports = { register, broadcast, buildStats, disposeActivity, onPipelineBusy };
