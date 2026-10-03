const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("rundiffDesktop", {
  saveExport: (filename, content) => ipcRenderer.invoke("rundiff:save-export", { filename, content }),
});
