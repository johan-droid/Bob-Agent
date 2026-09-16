const { app, BrowserWindow, Menu, shell, Notification } = require("electron");
const path = require("path");
const { spawn } = require("child_process");
const http = require("http");

let mainWindow = null;
let backendProcess = null;
let webProcess = null;

const BACKEND_PORT = 8000;
const WEB_PORT = 3000;

function checkUrlReady(url, timeoutMs = 15000) {
  const start = Date.now();
  return new Promise((resolve) => {
    const check = () => {
      const req = http.get(url, (res) => {
        if (res.statusCode >= 200 && res.statusCode < 500) {
          resolve(true);
        } else if (Date.now() - start < timeoutMs) {
          setTimeout(check, 400);
        } else {
          resolve(false);
        }
      });
      req.on("error", () => {
        if (Date.now() - start < timeoutMs) {
          setTimeout(check, 400);
        } else {
          resolve(false);
        }
      });
      req.end();
    };
    check();
  });
}

async function ensureBackendRunning() {
  const ready = await checkUrlReady(`http://127.0.0.1:${BACKEND_PORT}/api/v1/health`, 1500);
  if (ready) {
    console.log("[Desktop] Backend is already running on port", BACKEND_PORT);
    return;
  }

  console.log("[Desktop] Starting backend service via uv...");
  const backendDir = path.resolve(__dirname, "../../backend");
  backendProcess = spawn("uv", ["run", "uvicorn", "agent_system.api.main:app", "--port", String(BACKEND_PORT), "--reload"], {
    cwd: backendDir,
    stdio: "inherit",
    env: process.env,
  });

  backendProcess.on("error", (err) => {
    console.error("[Desktop] Failed to spawn backend:", err);
  });
}

async function ensureWebRunning() {
  const ready = await checkUrlReady(`http://127.0.0.1:${WEB_PORT}`, 1500);
  if (ready) {
    console.log("[Desktop] Web server is already running on port", WEB_PORT);
    return;
  }

  console.log("[Desktop] Starting Next.js server...");
  const webDir = path.resolve(__dirname, "..");
  webProcess = spawn("npm", ["run", "dev"], {
    cwd: webDir,
    stdio: "inherit",
    env: process.env,
  });

  webProcess.on("error", (err) => {
    console.error("[Desktop] Failed to spawn web server:", err);
  });
}

function createMainWindow() {
  mainWindow = new BrowserWindow({
    width: 1320,
    height: 880,
    minWidth: 960,
    minHeight: 640,
    title: "Bob Agent",
    backgroundColor: "#0d0e11",
    autoHideMenuBar: false,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  // Load chat by default
  const targetUrl = `http://localhost:${WEB_PORT}/chat`;
  mainWindow.loadURL(targetUrl);

  // Open external links in default OS browser
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith("http://") || url.startsWith("https://")) {
      shell.openExternal(url);
      return { action: "deny" };
    }
    return { action: "allow" };
  });

  buildMenu();

  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

function buildMenu() {
  const template = [
    {
      label: "File",
      submenu: [
        {
          label: "New Chat",
          accelerator: "CmdOrCtrl+N",
          click: () => {
            mainWindow?.loadURL(`http://localhost:${WEB_PORT}/chat`);
          },
        },
        {
          label: "Settings",
          accelerator: "CmdOrCtrl+,",
          click: () => {
            mainWindow?.loadURL(`http://localhost:${WEB_PORT}/settings`);
          },
        },
        { type: "separator" },
        { role: "quit" },
      ],
    },
    {
      label: "Navigation",
      submenu: [
        {
          label: "💬 Chat",
          click: () => mainWindow?.loadURL(`http://localhost:${WEB_PORT}/chat`),
        },
        {
          label: "📊 Kanban Board",
          click: () => mainWindow?.loadURL(`http://localhost:${WEB_PORT}/kanban`),
        },
        {
          label: "🖥️ Workspace Files",
          click: () => mainWindow?.loadURL(`http://localhost:${WEB_PORT}/workspace`),
        },
        {
          label: "📚 Obsidian Memory Vault",
          click: () => mainWindow?.loadURL(`http://localhost:${WEB_PORT}/vault`),
        },
        {
          label: "✅ Approvals",
          click: () => mainWindow?.loadURL(`http://localhost:${WEB_PORT}/approvals`),
        },
        {
          label: "💰 Cost Analytics",
          click: () => mainWindow?.loadURL(`http://localhost:${WEB_PORT}/cost`),
        },
        {
          label: "📖 Recipes",
          click: () => mainWindow?.loadURL(`http://localhost:${WEB_PORT}/recipes`),
        },
        {
          label: "🔧 Settings",
          click: () => mainWindow?.loadURL(`http://localhost:${WEB_PORT}/settings`),
        },
      ],
    },
    {
      label: "View",
      submenu: [
        { role: "reload" },
        { role: "forceReload" },
        { role: "toggleDevTools" },
        { type: "separator" },
        { role: "resetZoom" },
        { role: "zoomIn" },
        { role: "zoomOut" },
        { type: "separator" },
        { role: "togglefullscreen" },
      ],
    },
    {
      label: "Help",
      submenu: [
        {
          label: "Documentation",
          click: () => shell.openExternal("https://github.com/"),
        },
      ],
    },
  ];

  const menu = Menu.buildFromTemplate(template);
  Menu.setApplicationMenu(menu);
}

app.whenReady().then(async () => {
  await ensureBackendRunning();
  await ensureWebRunning();

  // Wait for web interface to be ready before creating window
  await checkUrlReady(`http://localhost:${WEB_PORT}`, 15000);

  createMainWindow();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createMainWindow();
    }
  });
});

function cleanup() {
  if (backendProcess) {
    try {
      backendProcess.kill();
    } catch (_) {}
  }
  if (webProcess) {
    try {
      webProcess.kill();
    } catch (_) {}
  }
}

app.on("window-all-closed", () => {
  cleanup();
  if (process.platform !== "darwin") {
    app.quit();
  }
});

app.on("before-quit", () => {
  cleanup();
});
