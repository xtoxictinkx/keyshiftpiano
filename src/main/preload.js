const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('keyShiftPiano', {
  selectFile: (payload) => ipcRenderer.invoke('select-input-file', payload),
  transposeFile: (payload) => ipcRenderer.invoke('transpose-file', payload),
  getSettings: () => ipcRenderer.invoke('get-settings'),
  saveSettings: (payload) => ipcRenderer.invoke('save-settings', payload),
  chooseToolPath: (payload) => ipcRenderer.invoke('choose-tool-path', payload)
});
