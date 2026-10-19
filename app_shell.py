import os
import sys
import threading
import time
import requests
import keyboard
import webbrowser
import shutil
import subprocess

from PySide6.QtCore import QUrl, QObject, Signal, QThread
from PySide6.QtGui import QIcon, QAction
from PySide6.QtWidgets import QApplication, QMainWindow, QSystemTrayIcon, QMenu
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEnginePage
import uvicorn
import voice_auth

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HOTKEY = "ctrl+space"
APP_HOST = "127.0.0.1"
APP_PORT = 8000
APP_URL = f"http://{APP_HOST}:{APP_PORT}/homepage"
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")


class ExternalAwarePage(QWebEnginePage):
    """
    Keep app navigation inside webview.
    Allow external browser only for OAuth providers.
    """

    def acceptNavigationRequest(self, url, nav_type, is_main_frame):
        u = url.toString().lower()

        # Allow local app routes in webview
        if u.startswith(f"http://{APP_HOST}:{APP_PORT}") or u.startswith(f"http://localhost:{APP_PORT}"):
            return True

        # OAuth provider pages -> open externally
        oauth_domains = (
            "accounts.google.com",
            "oauth2.googleapis.com",
            "accounts.spotify.com",
            "api.spotify.com",
        )
        if any(d in u for d in oauth_domains):
            webbrowser.open(url.toString())
            return False

        # Everything else external
        webbrowser.open(url.toString())
        return False


class WakeWordThread(QThread):
    """
    Wraps voice_auth.WakeWordListener.
    Emits wake_word_detected (Qt signal) when 'Hey WinVoice' is heard.
    """
    wake_word_detected = Signal()

    def __init__(self):
        super().__init__()
        self._listener = voice_auth.WakeWordListener(on_wake=self._on_wake)

    def _on_wake(self):
        """Called from listener thread — emit Qt signal thread-safely."""
        self.wake_word_detected.emit()

    def run(self):
        # BUG-13: use _running flag instead of infinite while True loop
        self._running = True
        self._listener.start()
        while self._running:
            time.sleep(0.5)

    def stop_listener(self):
        # BUG-13: signal run() to exit cleanly
        self._running = False
        self._listener.stop()


class HotkeyBridge(QObject):
    summon_signal = Signal()
    macro_stop_signal = Signal()

    def __init__(self):
        super().__init__()
        self._running = True
        self._ctrl_space_was_down = False
        self._ctrl_q_was_down = False
        self._ctrl_pressed = False

        # Use a polling thread — more reliable than add_hotkey when Qt WebEngine has focus
        self._thread = threading.Thread(target=self._poll, daemon=True)
        self._thread.start()

    def _poll(self):
        """Continuously poll for Ctrl+Space and Ctrl+Q."""
        while self._running:
            try:
                ctrl = keyboard.is_pressed("ctrl")
                space = keyboard.is_pressed("space")
                q = keyboard.is_pressed("q")

                # ── CTRL+SPACE for voice popup ──
                ctrl_space_down = ctrl and space
                if ctrl_space_down and not self._ctrl_space_was_down:
                    self.summon_signal.emit()
                self._ctrl_space_was_down = ctrl_space_down

                # ── CTRL+Q for macro stop ──
                ctrl_q_down = ctrl and q
                if ctrl_q_down and not self._ctrl_q_was_down:
                    print("[Hotkey] Ctrl+Q pressed — stopping macro")
                    self.macro_stop_signal.emit()
                self._ctrl_q_was_down = ctrl_q_down

            except Exception as e:
                print(f"[Hotkey] Polling error: {e}")

            time.sleep(0.05)  # 50ms polling — snappy enough

    def stop(self):
        self._running = False


class BrowserWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("WinVoice")
        self.popup_size = (760, 560)
        self.is_popup = True

        # Auto mode thresholds (Option A)
        self._auto_mode_enabled = True
        self._full_threshold_w = 1100
        self._full_threshold_h = 760
        # BUG-17: guard flag to prevent recursive resizeEvent -> set_*_mode -> resizeEvent loop
        self._in_mode_change = False

        self.view = QWebEngineView(self)
        self.page = ExternalAwarePage(self.view)
        self.view.setPage(self.page)

        # Grant audio capture permission for voice biometrics automatically
        self.page.featurePermissionRequested.connect(self.on_feature_permission_requested)

        self.setCentralWidget(self.view)

        self.resize(*self.popup_size)
        self._center()
        self.view.setUrl(QUrl(APP_URL))

    def resizeEvent(self, event):
        # BUG-17: bail early if a mode change is already in progress (prevents recursion)
        if self._in_mode_change:
            super().resizeEvent(event)
            return
        super().resizeEvent(event)
        if not self._auto_mode_enabled:
            return
        try:
            w, h = self.width(), self.height()

            # If big enough and currently popup -> full
            if (w >= self._full_threshold_w or h >= self._full_threshold_h) and self.is_popup:
                self._in_mode_change = True
                try:
                    self.set_full_mode()
                finally:
                    self._in_mode_change = False
                return

            # If not big enough and in full (but not OS-maximized) -> popup
            if (w < self._full_threshold_w and h < self._full_threshold_h) and (not self.is_popup) and (
                    not self.isMaximized()):
                self._in_mode_change = True
                try:
                    self.set_popup_mode()
                finally:
                    self._in_mode_change = False
        except Exception:
            pass

    def _center(self):
        screen = QApplication.primaryScreen().availableGeometry()
        x = (screen.width() - self.width()) // 2
        y = (screen.height() - self.height()) // 2
        self.move(max(0, x), max(0, y))

    def on_feature_permission_requested(self, url, feature):
        if feature == QWebEnginePage.MediaAudioCapture:
            self.page.setFeaturePermission(url, feature, QWebEnginePage.PermissionGrantedByUser)

    def set_popup_mode(self):
        self.is_popup = True
        self.showNormal()
        self.resize(*self.popup_size)
        self._center()
        self.show()
        self.raise_()
        self.activateWindow()
        self._send_mode("popup")

    def set_full_mode(self):
        """Go full mode: maximize the window and switch UI to full dashboard."""
        self.is_popup = False
        self.showMaximized()
        self.show()
        self.raise_()
        self.activateWindow()
        self._send_mode("full")
        self._inject_expand_helper()

    def hide_window(self):
        self.hide()

    def toggle_popup_full(self):
        if self.is_popup:
            self.set_full_mode()
        else:
            self.set_popup_mode()

    def toggle_visibility_hotkey(self):
        from PySide6.QtCore import QTimer
        # Always trigger voice listen on hotkey
        if not self.isVisible() or self.isMinimized():
            self.set_popup_mode()
            
        self.raise_()
        self.activateWindow()
        QTimer.singleShot(100, self.trigger_voice_listen)

    def trigger_macro_stop(self):
        """Called by hotkey bridge when Ctrl+Q is pressed."""
        try:
            requests.post(f"http://{APP_HOST}:{APP_PORT}/macros/stop-recording", timeout=2)
        except Exception as e:
            print(f"[Macro Stop] Error: {e}")

    def trigger_voice_listen(self):
        js = """
        if (typeof window.startVoice === 'function') {
            window.startVoice();
        }
        """
        self.view.page().runJavaScript(js)

    def _send_mode(self, mode: str):
        js = f"""
        if (window.setWinVoiceMode) {{
            window.setWinVoiceMode("{mode}");
        }}
        """
        self.view.page().runJavaScript(js)

    def _inject_expand_helper(self):
        js = """
        window.appShellExpand = function() {
            // kept for compatibility
        };
        """
        self.view.page().runJavaScript(js)


class AppShell:
    def __init__(self):
        self.qt_app = QApplication(sys.argv)
        self.qt_app.setQuitOnLastWindowClosed(False)

        icon_path = os.path.join(BASE_DIR, "assets", "logo.png")
        self.icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()

        self.window = BrowserWindow()
        if not self.icon.isNull():
            self.window.setWindowIcon(self.icon)
            self.qt_app.setWindowIcon(self.icon)

        self.tray = QSystemTrayIcon(self.icon, self.window)
        self.tray.setToolTip("WinVoice")

        menu = QMenu()

        a1 = QAction("Show Popup", self.window)
        a1.triggered.connect(self.window.set_popup_mode)
        menu.addAction(a1)

        a2 = QAction("Show Full Dashboard", self.window)
        a2.triggered.connect(self.window.set_full_mode)
        menu.addAction(a2)

        a3 = QAction("Toggle Popup/Full", self.window)
        a3.triggered.connect(self.window.toggle_popup_full)
        menu.addAction(a3)

        menu.addSeparator()

        a4 = QAction("Reload UI", self.window)
        a4.triggered.connect(lambda: self.window.view.reload())
        menu.addAction(a4)

        menu.addSeparator()

        a5 = QAction("Quit", self.window)
        a5.triggered.connect(self.quit_all)
        menu.addAction(a5)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.on_tray_click)
        self.tray.show()

        self.hotkey = HotkeyBridge()
        self.hotkey.summon_signal.connect(self.window.toggle_visibility_hotkey)
        self.hotkey.macro_stop_signal.connect(self.window.trigger_macro_stop)

        self.wakeword_thread = WakeWordThread()
        self.wakeword_thread.wake_word_detected.connect(self.on_wake_word)
        self.wakeword_thread.start()

    def on_wake_word(self):
        """Simulate Ctrl+Space when wake word is detected."""
        self.window.toggle_visibility_hotkey()

    def on_tray_click(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            self.window.set_popup_mode()

    def quit_all(self):
        try:
            self.hotkey.stop()
        except Exception:
            pass
        # BUG-14: stop the wake word thread before quitting to avoid zombie thread
        try:
            self.wakeword_thread.stop_listener()
        except Exception:
            pass
        self.tray.hide()
        self.qt_app.quit()

    def run(self):
        # Start hidden in tray
        self.window.hide()
        sys.exit(self.qt_app.exec())


def _is_ollama_up(base_url=OLLAMA_BASE_URL):
    try:
        r = requests.get(f"{base_url}/api/tags", timeout=1.5)
        return r.status_code == 200
    except Exception:
        return False


def ensure_ollama_running():
    if _is_ollama_up():
        print("[app_shell] Ollama already running.")
        return True

    ollama_bin = shutil.which("ollama")
    if not ollama_bin:
        print("[app_shell] Ollama binary not found in PATH.")
        return False

    try:
        if sys.platform == "win32":
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            subprocess.Popen(
                [ollama_bin, "serve"],
                creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        else:
            subprocess.Popen(
                [ollama_bin, "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
    except Exception as e:
        print(f"[app_shell] Failed to start ollama serve: {e}")
        return False

    start = time.time()
    while time.time() - start < 12:
        if _is_ollama_up():
            print("[app_shell] Ollama is running.")
            return True
        time.sleep(0.4)

    print("[app_shell] Ollama did not start in time.")
    return False


def start_fastapi_server():
    config = uvicorn.Config("main:app", host=APP_HOST, port=APP_PORT, reload=False, log_level="info")
    server = uvicorn.Server(config)
    server.run()


def wait_for_server(url=f"http://{APP_HOST}:{APP_PORT}/healthz", timeout=25):
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = requests.get(url, timeout=1.5)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.35)
    return False


if __name__ == "__main__":
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (
        "--disable-gpu-compositing "
        "--enable-begin-frame-scheduling "
        "--num-raster-threads=4 "
        "--disable-software-rasterizer "
        "--enable-accelerated-2d-canvas "
        "--disable-partial-raster "
        "--enable-zero-copy "
        "--max-tiles-for-interest-area=512 "
        "--renderer-process-limit=2"
    )
    ensure_ollama_running()

    t = threading.Thread(target=start_fastapi_server, daemon=True)
    t.start()

    if not wait_for_server():
        print("WARNING: Backend did not respond in time.")

    shell = AppShell()

    # Register the BrowserWindow with main.py so /shell/expand and /shell/popup can call window methods
    try:
        import main as _main_module

        _main_module._shell_window = shell.window
    except Exception as e:
        print(f"[app_shell] Could not register window with main: {e}")

    shell.run()