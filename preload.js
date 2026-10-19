// preload.js — WinVoice IPC bridge
'use strict';
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  // ── Shell mode controls ─────────────────────────────────────────────
  setPopupMode: () => ipcRenderer.send('shell:popup'),
  setFullMode:  () => ipcRenderer.send('shell:full'),
  hideWindow:   () => ipcRenderer.send('shell:hide'),
  minimizeWindow: () => ipcRenderer.send('win:minimize'),

  // ── Open a URL in the system default browser (Chrome, Edge, etc.) ──
  openExternal: (url) => ipcRenderer.send('open:external', url),

  // ── Inbound events from main process ───────────────────────────────
  // BUG-11: removeAllListeners before .on() prevents duplicate handler accumulation
  onWakeWord: (cb) => {
    ipcRenderer.removeAllListeners('wake:trigger');
    ipcRenderer.on('wake:trigger', () => cb());
  },
  onHotkey: (cb) => {
    ipcRenderer.removeAllListeners('hotkey:trigger');
    ipcRenderer.on('hotkey:trigger', () => cb());
  },
  onModeChange: (cb) => {
    ipcRenderer.removeAllListeners('mode:change');
    ipcRenderer.on('mode:change', (_e, mode) => cb(mode));
  },
});