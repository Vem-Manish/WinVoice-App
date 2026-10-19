// main.js  — WinVoice Electron shell v2 (fully fixed)
'use strict';

const { app, BrowserWindow, Tray, Menu, globalShortcut, ipcMain, nativeImage, shell } = require('electron');
const { spawn, exec, execSync, execFile } = require('child_process');
const path  = require('path');
const http  = require('http');
const fs    = require('fs');
const os    = require('os');

// ── Transparency flags — MUST come before any GPU/rendering flags ───────────
// These ensure Electron/Chromium properly composites the window with real alpha
// so that border-radius corners are transparent (show desktop) not dark-filled.
app.commandLine.appendSwitch('enable-transparent-visuals');
app.commandLine.appendSwitch('force-color-profile', 'srgb');

// ── Performance flags ─────────────────────────────────────────────────────
app.commandLine.appendSwitch('enable-begin-frame-scheduling');
app.commandLine.appendSwitch('enable-accelerated-2d-canvas');
app.commandLine.appendSwitch('enable-zero-copy');

const APP_HOST = '127.0.0.1';
const APP_PORT = 8000;
const APP_URL  = `http://${APP_HOST}:${APP_PORT}/homepage`;

// Domains that must open in the real system browser (OAuth, external links)
const EXTERNAL_DOMAINS = [
  'accounts.google.com',
  'oauth2.googleapis.com',
  'accounts.spotify.com',
  'api.spotify.com',
  'mail.google.com',
  'docs.google.com',
  'drive.google.com',
  'calendar.google.com',
];

let win, tray, backendProcess, ollamaProcess;
let isPopup  = true;
let winReady = false;
let isQuitting = false;

const POPUP_W = 400,  POPUP_H = 580;
const FULL_W  = 1280, FULL_H  = 820;

// ─────────────────────────────────────────────────────────────────────────────
// 0. Tray icon — auto-generate if missing
// ─────────────────────────────────────────────────────────────────────────────
function ensureIcon() {
  const iconPath = path.join(__dirname, 'static', 'favicon.ico');
  if (fs.existsSync(iconPath)) return iconPath;

  const staticDir = path.join(__dirname, 'static');
  if (!fs.existsSync(staticDir)) fs.mkdirSync(staticDir, { recursive: true });

  const pyScript = String.raw`
import struct,zlib,os,sys
def make(p):
    W=H=32;bg=(13,13,18,255);g=(242,170,92,255)
    px=[]
    for y in range(H):
        row=[]
        for x in range(W):
            nx=x/(W-1);ny=y/(H-1);iw=False
            if 0.07<=nx<=0.32 and 0.12<=ny<=0.90:
                if abs(nx-(0.07+ny*0.26))<0.06:iw=True
            if 0.68<=nx<=0.93 and 0.12<=ny<=0.90:
                if abs(nx-(0.93-ny*0.26))<0.06:iw=True
            if 0.30<=nx<=0.55 and 0.12<=ny<=0.90:
                if abs(nx-(0.68-ny*0.26))<0.055:iw=True
            if 0.45<=nx<=0.70 and 0.12<=ny<=0.90:
                if abs(nx-(0.32+ny*0.26))<0.055:iw=True
            row.append(g if iw else bg)
        px.append(row)
    def ck(t,d):
        c=zlib.crc32(t+d)&0xffffffff
        return struct.pack('>I',len(d))+t+d+struct.pack('>I',c)
    ih=struct.pack('>IIBBBBB',W,H,8,6,0,0,0)
    raw=b''.join(b'\x00'+bytes([c for px2 in r for c in px2]) for r in px)
    png=b'\x89PNG\r\n\x1a\n'+ck(b'IHDR',ih)+ck(b'IDAT',zlib.compress(raw,9))+ck(b'IEND',b'')
    ico=struct.pack('<HHH',0,1,1)+struct.pack('<BBBBHHII',0,0,0,0,1,32,len(png),22)+png
    os.makedirs(os.path.dirname(os.path.abspath(p)),exist_ok=True)
    open(p,'wb').write(ico)
make(sys.argv[1])
`;
  const tmpPy = path.join(os.tmpdir(), 'wv_icon.py');
  fs.writeFileSync(tmpPy, pyScript);

  const pythons = [
    path.join(__dirname, '.venv', 'Scripts', 'python.exe'),
    'python', 'python3',
  ];
  for (const py of pythons) {
    try {
      execSync(`"${py}" "${tmpPy}" "${iconPath}"`, { timeout: 8000, stdio: 'ignore' });
      if (fs.existsSync(iconPath)) { console.log('[Icon] Created:', iconPath); return iconPath; }
    } catch (_) {}
  }
  console.warn('[Icon] Could not auto-generate icon');
  return null;
}

// ─────────────────────────────────────────────────────────────────────────────
// 1. Kill any old process that's using port 8000
// ─────────────────────────────────────────────────────────────────────────────
async function killPortIfBusy(port) {
  if (process.platform !== 'win32') return;
  try {
    // netstat -ano gives us PID of whatever is using the port
    const out = execSync(
      `netstat -ano | findstr ":${port} " | findstr "LISTENING"`,
      { timeout: 4000, stdio: ['pipe', 'pipe', 'ignore'] }
    ).toString().trim();

    const lines = out.split('\n').filter(Boolean);
    for (const line of lines) {
      const parts = line.trim().split(/\s+/);
      const pid = parts[parts.length - 1];
      if (pid && /^\d+$/.test(pid) && parseInt(pid) > 4) {
        console.log(`[Boot] Killing stale process on port ${port} (PID ${pid})`);
        try { execSync(`taskkill /PID ${pid} /F`, { stdio: 'ignore' }); } catch (_) {}
      }
    }
    await new Promise(resolve => setTimeout(resolve, 600));
  } catch (_) {
    // Port was free — nothing to do
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// 2. Ensure Ollama is running (mirrors app_shell.py ensure_ollama_running)
// ─────────────────────────────────────────────────────────────────────────────
function isOllamaUp() {
  try {
    // Read the HTTP status code from curl output — fix CRASH-2 (was ignoring output)
    const out = execSync(
      'curl -s -o NUL -w "%{http_code}" http://127.0.0.1:11434/api/tags',
      { timeout: 2000, stdio: ['ignore', 'pipe', 'ignore'] }
    ).toString().trim();
    return out === '200';
  } catch (_) { return false; }
}

function findOllama() {
  // Check common install paths on Windows
  const candidates = [
    'ollama',  // in PATH
    path.join(process.env.LOCALAPPDATA || '', 'Programs', 'Ollama', 'ollama.exe'),
    path.join('C:\\', 'Program Files', 'Ollama', 'ollama.exe'),
    path.join(process.env.USERPROFILE || '', 'AppData', 'Local', 'Programs', 'Ollama', 'ollama.exe'),
  ];
  for (const c of candidates) {
    try {
      execSync(`"${c}" --version`, { timeout: 2000, stdio: 'ignore' });
      return c;
    } catch (_) {}
  }
  return null;
}

// Fix STARTUP-1: ensureOllama is now async — no more spin-loop sleeps
async function ensureOllama() {
  if (isOllamaUp()) {
    console.log('[Ollama] Already running');
    return true;
  }
  let ollamaExe = findOllama();
  if (!ollamaExe) {
    if (app.isPackaged) {
      console.log('[Ollama] Not found, attempting silent installation...');
      try {
        const setupPath = path.join(process.resourcesPath, 'OllamaSetup.exe');
        if (fs.existsSync(setupPath)) {
          execSync(`"${setupPath}" /S`, { stdio: 'ignore' });
          console.log('[Ollama] Installation complete.');
          ollamaExe = findOllama();
        } else {
          console.warn('[Ollama] Installer not found at', setupPath);
        }
      } catch (err) {
        console.error('[Ollama] Installation failed:', err.message);
      }
    }
    
    if (!ollamaExe) {
      console.warn('[Ollama] Not found in PATH or common locations — skipping auto-start');
      return false;
    }
  }
  console.log('[Ollama] Starting ollama serve…');
  try {
    if (process.platform === 'win32') {
      // Detached so it survives if Electron dies
      ollamaProcess = spawn(ollamaExe, ['serve'], {
        detached: true,
        stdio:    'ignore',
        windowsHide: true,
      });
      ollamaProcess.unref();
    } else {
      ollamaProcess = spawn(ollamaExe, ['serve'], { stdio: 'ignore' });
    }
    // Wait up to 12 seconds for Ollama to become reachable (async — no spin loops)
    const deadline = Date.now() + 12000;
    while (Date.now() < deadline) {
      await new Promise(r => setTimeout(r, 400));
      if (isOllamaUp()) { console.log('[Ollama] Ready'); return true; }
    }
    console.warn('[Ollama] Did not respond within 12s');
  } catch (e) {
    console.warn('[Ollama] Failed to start:', e.message);
  }
  return false;
}

// ─────────────────────────────────────────────────────────────────────────────
// 3. Start Python (FastAPI) backend
// ─────────────────────────────────────────────────────────────────────────────
function startBackend() {
  let pyExe = 'python';
  let args = ['main.py'];
  let cwd = __dirname;
  
  if (app.isPackaged) {
    pyExe = path.join(process.resourcesPath, 'winvoice_server.exe');
    args = [];
    console.log('[Boot] Using packaged backend:', pyExe);
  } else {
    const venvPy = path.join(__dirname, '.venv', 'Scripts', 'python.exe');
    if (fs.existsSync(venvPy)) {
      pyExe = venvPy;
      console.log('[Boot] Using venv Python:', venvPy);
    } else {
      console.log('[Boot] Using global Python');
    }
  }

  backendProcess = spawn(pyExe, args, {
    cwd: cwd,
    env: {
      ...process.env,
      PYTHONIOENCODING: 'utf-8',
      PYTHONUTF8: '1',
    },
  });

  backendProcess.stdout.on('data', d => process.stdout.write(`[FastAPI] ${d}`));
  backendProcess.stderr.on('data', d => process.stderr.write(`[FastAPI ERR] ${d}`));
  backendProcess.on('exit', code => console.log(`[FastAPI] exited with code ${code}`));
}

// ─────────────────────────────────────────────────────────────────────────────
// 4. BrowserWindow — with OAuth interception
// ─────────────────────────────────────────────────────────────────────────────
function createWindow() {
  win = new BrowserWindow({
    icon: ensureIcon(),
    width:  POPUP_W,
    height: POPUP_H,
    show:   false,
    frame:  false,
    // ── Transparency + rounded corners ──────────────────────────────
    // transparent:true + backgroundColor:'#00000000' tells Electron/Chromium
    // to composite the window with a transparent background.
    // The CSS on #app-root then draws the dark background with border-radius,
    // so the OS sees actual transparent pixels at the corners → rounded shell.
    transparent:     true,
    backgroundColor: '#00000000',
    roundedCorners:  true,    // Windows 11 DWM rounded corners (extra safety)
    hasShadow:       true,    // OS shadow under the rounded window
    alwaysOnTop:     true,
    skipTaskbar:     true,
    resizable:       true,
    titleBarStyle:   'hidden',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation:     true,
      backgroundThrottling: false,
      nodeIntegration:      false,
      // Needed so Chromium doesn't paint a white/black background behind the page
      offscreen: false,
    },
  });

  // ── Intercept ALL navigation — send external URLs to system browser ──
  // This is the key fix for Google OAuth: instead of loading inside Electron,
  // accounts.google.com opens in Chrome/Edge/default browser.
  win.webContents.on('will-navigate', (event, url) => {
    if (isExternalUrl(url)) {
      event.preventDefault();
      console.log('[Nav] Opening externally:', url);
      shell.openExternal(url);
    }
  });

  // Same for any new-window requests (target="_blank", window.open, etc.)
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (isExternalUrl(url)) {
      shell.openExternal(url);
      return { action: 'deny' };
    }
    return { action: 'allow' };
  });

  // Retry loading until backend is up — BUG-08: capped at 30 attempts
  function loadWithRetry(attempts = 0) {
    win.loadURL(APP_URL).catch(() => {
      if (attempts >= 30) {
        win.loadURL(`data:text/html,<h2>WinVoice could not connect to the backend. Please restart the app.</h2>`);
        return;
      }
      setTimeout(() => loadWithRetry(attempts + 1), 600);
    });
  }
  loadWithRetry();

  win.webContents.on('did-finish-load', () => {
    winReady = true;
    // Re-assert transparency after page load — Windows DWM sometimes needs this
    try { win.setBackgroundColor('#00000000'); } catch (_) {}
    win.show();
    setTimeout(() => { if (win) win.webContents.send('mode:change', 'popup'); }, 200);
  });

  // Auto-hide when popup loses focus (with small grace period)
  win.on('blur', () => {
    if (isPopup && win && win.isVisible()) {
      setTimeout(() => {
        if (isPopup && win && !win.isFocused()) win.hide();
      }, 180);
    }
  });

  // BUG-10: removed setPopupMode() call on close to prevent window flicker
  win.on('close', (e) => {
    if (!isQuitting) {
      e.preventDefault();
      isPopup = true;
      win.hide();
    }
  });

  win.on('closed', () => { win = null; });
}

function isExternalUrl(url) {
  try {
    const host = new URL(url).hostname.toLowerCase();
    // Keep local app URLs inside Electron
    if (host === APP_HOST || host === 'localhost') return false;
    // Send everything else (OAuth, Google Drive, mail, etc.) to system browser
    return true;
  } catch (_) {
    return false;
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// 5. Wake-word SSE listener
// ─────────────────────────────────────────────────────────────────────────────
function startWakeWordListener() {
  if (!win) { setTimeout(startWakeWordListener, 1000); return; }

  const req = http.get(`http://${APP_HOST}:${APP_PORT}/wake/stream`, (res) => {
    let buf = '';
    res.on('data', chunk => {
      buf += chunk.toString();
      if (buf.includes('wake')) {
        buf = ''; // Clear buffer so it doesn't trigger again on next chunk
        console.log('[WakeWord] Triggered!');
        if (!win) return;
        if (!win.isVisible() || win.isMinimized()) setPopupMode();
        else win.focus();
        setTimeout(() => { if (win) win.webContents.send('wake:trigger'); }, 200);
      }
      // BUG-09: keep only the last 200 chars to prevent unbounded buffer growth
      if (buf.length > 200) buf = buf.slice(-200);
    });
    res.on('end', () => setTimeout(startWakeWordListener, 2000));
  });

  req.on('error', () => setTimeout(startWakeWordListener, 1500));
}

// ─────────────────────────────────────────────────────────────────────────────
// 6. Mode helpers
// ─────────────────────────────────────────────────────────────────────────────
function setPopupMode() {
  if (!win) return;
  isPopup = true;
  win.setAlwaysOnTop(true, 'floating');
  win.setSkipTaskbar(true);
  
  if (win.isMaximized()) {
    win.unmaximize();
  }
  
  // Enforce size and center just in case
  win.setSize(POPUP_W, POPUP_H, false);
  win.center();
  
  try { win.setBackgroundColor('#00000000'); } catch (_) {}
  if (!win.isVisible()) {
    win.show();
  }
  win.focus();
  
  if (winReady) win.webContents.send('mode:change', 'popup');
  updateTrayMenu();
}

function setFullMode() {
  if (!win) return;
  isPopup = false;
  win.setAlwaysOnTop(false);
  win.setSkipTaskbar(false);
  win.setResizable(true);
  
  try { win.setBackgroundColor('#0d0d12'); } catch (_) {}
  
  win.maximize();
  win.show();
  win.focus();
  
  if (winReady) win.webContents.send('mode:change', 'full');
  updateTrayMenu();
}

function toggleMode() { if (isPopup) setFullMode(); else setPopupMode(); }

// ─────────────────────────────────────────────────────────────────────────────
// 7. Tray
// ─────────────────────────────────────────────────────────────────────────────
function updateTrayMenu() {
  if (!tray) return;
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: isPopup ? '✅ Popup Mode'        : 'Show Popup',           click: setPopupMode },
    { label: isPopup ? 'Open Full Dashboard' : '✅ Full Dashboard',     click: setFullMode  },
    { type: 'separator' },
    { label: 'Toggle Popup / Full',                                      click: toggleMode   },
    { type: 'separator' },
    { label: 'Reload UI', click: () => { if (win) win.webContents.reload(); } },
    { type: 'separator' },
    { label: 'Quit WinVoice', click: quitAll },
  ]));
}

function createTray() {
  const iconPath = ensureIcon();
  try {
    tray = iconPath ? new Tray(iconPath) : new Tray(nativeImage.createEmpty());
  } catch (e) {
    console.error('[Tray] Create failed:', e.message);
    try { tray = new Tray(nativeImage.createEmpty()); } catch (_) { return; }
  }
  tray.setToolTip('WinVoice AI Assistant');
  tray.on('click', () => {
    if (!win) return;
    if (win.isVisible() && win.isFocused()) win.hide(); else setPopupMode();
  });
  tray.on('double-click', setFullMode);
  updateTrayMenu();
  console.log('[Tray] System tray ready');
}

// ─────────────────────────────────────────────────────────────────────────────
// 8. IPC
// ─────────────────────────────────────────────────────────────────────────────
// SEC-1: HTML sanitizer to prevent XSS/RCE via notification content
function escapeHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#x27;');
}

// BUG-06: track shown notification IDs to prevent re-displaying
const _shownNotifIds = new Set();

function startNotificationPolling() {
  setInterval(() => {
    http.get('http://127.0.0.1:8000/notifications', (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => {
        try {
          let json = JSON.parse(data);
          if(json.notifications && json.notifications.length > 0) {
            json.notifications.forEach(n => {
              // BUG-06: skip already-shown notifications
              if (_shownNotifIds.has(n.id)) return;
              _shownNotifIds.add(n.id);

              const { screen } = require('electron');
              const { width, height } = screen.getPrimaryDisplay().workAreaSize;
              // SEC-1: nodeIntegration false + contextIsolation true
              const popup = new BrowserWindow({
                icon: ensureIcon(),
                width: 380, height: 130,
                x: width - 380 - 24, y: height - 130 - 24,
                frame: false, transparent: true, alwaysOnTop: true, skipTaskbar: true,
                webPreferences: { nodeIntegration: false, contextIsolation: true }
              });

              // BUG-06: clean up window reference on close
              popup.on('closed', () => { popup.destroy?.(); });

              // SEC-1: escape user-supplied content before injecting into HTML
              const safeTitle   = escapeHtml(n.title);
              const safeMessage = escapeHtml(n.message).replace(/\n/g, '<br>');

              const html = `
              <html><body style="margin:0; padding:16px; font-family:'Segoe UI',sans-serif; background:rgba(15,18,25,0.7); backdrop-filter:blur(20px); border-radius:16px; border:1px solid rgba(255,255,255,0.1); color:white; overflow:hidden; display:flex; flex-direction:column; box-shadow:0 10px 30px rgba(0,0,0,0.5);">
                <div style="width:4px; height:100%; background:#eab308; position:absolute; left:0; top:0;"></div>
                <div style="font-weight:bold; font-size:14px; margin-left:12px; color:#e4e4e7; letter-spacing:1px">⏰ ${safeTitle.toUpperCase()}</div>
                <div style="margin-top:10px; margin-left:12px; font-size:15px; color:#a1a1aa; line-height:1.4">${safeMessage}</div>
                <div style="flex:1"></div>
                <div style="text-align:right;">
                    <button onclick="window.close()" style="background:rgba(255,255,255,0.1); border:none; color:white; padding:6px 16px; border-radius:8px; cursor:pointer; font-weight:bold; font-size:13px; transition:0.2s" onmouseover="this.style.background='rgba(255,255,255,0.2)'" onmouseout="this.style.background='rgba(255,255,255,0.1)'">Dismiss</button>
                </div>
                <script>setTimeout(()=>window.close(), 15000);</script>
              </body></html>
              `;
              popup.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(html)}`);
            });
          }
        } catch(e){}
      });
    }).on('error', ()=>{});
  }, 1000);
}

function registerIPC() {
  ipcMain.on('shell:popup',  () => setPopupMode());
  ipcMain.on('shell:full',   () => setFullMode());
  ipcMain.on('shell:hide',   () => { if (win) win.hide(); });
  ipcMain.on('win:minimize', () => { if (win) win.minimize(); });
  // Open any URL in the system browser (used for OAuth)
  ipcMain.on('open:external', (_event, url) => {
    console.log('[IPC] Opening externally:', url);
    shell.openExternal(url).catch(e => console.warn('[IPC] openExternal error:', e));
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// 9. Shutdown
// ─────────────────────────────────────────────────────────────────────────────
function quitAll() {
  isQuitting = true;
  console.log('[Shutdown] Quitting WinVoice.');
  globalShortcut.unregisterAll();
  try { http.get(`http://${APP_HOST}:${APP_PORT}/shutdown`, () => {}).on('error', () => {}); } catch (_) {}
  if (backendProcess && backendProcess.pid) {
    if (process.platform === 'win32') {
      exec(`taskkill /pid ${backendProcess.pid} /t /f`, () => {});
    } else {
      try { backendProcess.kill(); } catch (_) {}
    }
  }
  if (tray) { try { tray.destroy(); } catch (_) {} }
  app.exit(0);
}

// ─────────────────────────────────────────────────────────────────────────────
// 10. Boot sequence
// ─────────────────────────────────────────────────────────────────────────────
// STARTUP-1: boot sequence is now async so ensureOllama can be awaited
app.whenReady().then(async () => {
  // ── Step 1: Kill any process already on port 8000 ───────────────────
  console.log('[Boot] Checking port 8000…');
  await killPortIfBusy(APP_PORT);

  // ── Step 2: Ensure Ollama is running ────────────────────────────────
  console.log('[Boot] Checking Ollama…');
  await ensureOllama();

  // ── Step 3: Start Python backend ────────────────────────────────────
  startBackend();

  // ── Step 4: Create UI ───────────────────────────────────────────────
  createWindow();
  createTray();
  registerIPC();
  startNotificationPolling();

  // ── Step 5: Register global hotkeys ─────────────────────────────────
  // Try Ctrl+Space first; if taken, fall back to Ctrl+Shift+Space
  let hotkeyRegistered = false;
  const hotkeys = ['CommandOrControl+Space', 'CommandOrControl+Shift+Space', 'Alt+Space'];

  for (const hk of hotkeys) {
    const ok = globalShortcut.register(hk, () => {
      if (!win) return;
      if (win.isVisible() && win.isFocused()) {
        win.hide();
      } else {
        setPopupMode();
        setTimeout(() => { if (win) win.webContents.send('hotkey:trigger'); }, 150);
      }
    });
    if (ok) {
      console.log(`[Hotkey] Registered: ${hk}`);
      hotkeyRegistered = true;
      break;
    }
    console.warn(`[Hotkey] ${hk} taken, trying next…`);
  }

  if (!hotkeyRegistered) {
    console.warn('[Hotkey] All hotkeys taken — use tray to open WinVoice');
  }

  // ── Step 6: Start wake-word SSE listener after backend is up ────────
  setTimeout(startWakeWordListener, 4000);

  // ── Step 7: Auto-start with Windows ────────────────────────────────
  if (app.isPackaged) {
    app.setLoginItemSettings({
      openAtLogin: true,
      args: ['--hidden']
    });
  }
});

app.on('will-quit', () => globalShortcut.unregisterAll());
app.on('window-all-closed', (e) => e.preventDefault());
app.on('activate', () => { if (!win) createWindow(); else setPopupMode(); });