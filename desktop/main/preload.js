'use strict';
/**
 * main/preload.js — 메인 대시보드 창용 contextBridge.
 * 렌더러는 Node 접근 없이 이 화이트리스트 API 만 사용한다(contextIsolation 유지).
 */

const { contextBridge, ipcRenderer } = require('electron');

const api = {
  // 조회
  getAppInfo: () => ipcRenderer.invoke('app:info'),
  getEngineStatus: () => ipcRenderer.invoke('engine:status'),
  getStats: () => ipcRenderer.invoke('stats:get'),
  getMetrics: () => ipcRenderer.invoke('metrics:get'),
  getSettings: () => ipcRenderer.invoke('settings:get'),
  getConnections: () => ipcRenderer.invoke('connections:get'),

  // 변경/제어
  setSettings: (patch) => ipcRenderer.invoke('settings:set', patch),
  savePipelineLayout: (layout) => ipcRenderer.invoke('settings:setPipelineLayout', layout),
  restartEngine: () => ipcRenderer.invoke('engine:restart'),
  setSecurityEnabled: (enabled) => ipcRenderer.invoke('engine:setSecurity', enabled),
  openExternal: (url) => ipcRenderer.invoke('external:open', url),

  // 실시간 push 구독
  onEngineStatus: (cb) => subscribe('engine:status', cb),
  onMetrics: (cb) => subscribe('metrics:tick', cb),
  onStats: (cb) => subscribe('stats:tick', cb),
  onNavigate: (cb) => subscribe('nav:goto', cb),
};

function subscribe(channel, cb) {
  const listener = (_evt, payload) => cb(payload);
  ipcRenderer.on(channel, listener);
  return () => ipcRenderer.removeListener(channel, listener);
}

contextBridge.exposeInMainWorld('upsec', api);
