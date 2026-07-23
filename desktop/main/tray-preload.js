'use strict';
/**
 * main/tray-preload.js — 트레이 팝오버 창용 contextBridge (PLAN §8 트레이 팝업).
 */

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('tray', {
  getEngineStatus: () => ipcRenderer.invoke('engine:status'),
  getStats: () => ipcRenderer.invoke('stats:get'),
  getMetrics: () => ipcRenderer.invoke('metrics:get'),
  getSettings: () => ipcRenderer.invoke('settings:get'),
  setSecurityEnabled: (enabled) => ipcRenderer.invoke('engine:setSecurity', enabled),
  openDashboard: () => ipcRenderer.invoke('window:showDashboard'),
  quitApp: () => ipcRenderer.invoke('app:quit'),

  onEngineStatus: (cb) => sub('engine:status', cb),
  onMetrics: (cb) => sub('metrics:tick', cb),
  onStats: (cb) => sub('stats:tick', cb),
});

function sub(channel, cb) {
  const listener = (_e, p) => cb(p);
  ipcRenderer.on(channel, listener);
  return () => ipcRenderer.removeListener(channel, listener);
}
