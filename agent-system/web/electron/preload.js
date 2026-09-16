const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("electronAPI", {
  platform: process.platform,
  isDesktop: true,
  sendNotification: (title, body) => {
    ipcRenderer.send("desktop-notification", { title, body });
  },
});
