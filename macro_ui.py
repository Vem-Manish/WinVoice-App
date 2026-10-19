# macro_ui.py
import tkinter as tk
import threading
import time


class MacroRecorderUI:
    def __init__(self, on_stop_callback=None, on_pause_callback=None, on_resume_callback=None):
        self.window = None
        self.recording = False
        self.paused = False
        self.start_time = None
        self.pause_time = None
        self.total_paused = 0
        self._keep_updating = False
        self._dot_visible = True
        self.should_close = False

        self.on_stop = on_stop_callback
        self.on_pause = on_pause_callback
        self.on_resume = on_resume_callback

        self.timer_label = None
        self.status_label = None
        self.pause_btn = None
        self.rec_dot = None

    def create_window(self):
        self.window = tk.Tk()
        self.window.title("WinVoice — Recording")
        self.window.overrideredirect(True)          # borderless
        self.window.attributes("-topmost", True)
        self.window.configure(bg="#0f1117")

        W, H = 320, 200

        # ── Position: bottom-right corner ──
        sw = self.window.winfo_screenwidth()
        sh = self.window.winfo_screenheight()
        x = sw - W - 24
        y = sh - H - 60          # above taskbar
        self.window.geometry(f"{W}x{H}+{x}+{y}")

        # ── Drag support ──
        self._drag_x = 0
        self._drag_y = 0
        self.window.bind("<ButtonPress-1>",   self._drag_start)
        self.window.bind("<B1-Motion>",        self._drag_move)

        # ── Outer border frame ──
        outer = tk.Frame(self.window, bg="#ef4444", padx=2, pady=2)
        outer.pack(fill="both", expand=True)

        inner = tk.Frame(outer, bg="#0f1117")
        inner.pack(fill="both", expand=True)

        # ── Header row ──
        hdr = tk.Frame(inner, bg="#161b27", height=44)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)

        self.rec_dot = tk.Label(hdr, text="●", fg="#ef4444", bg="#161b27",
                                font=("Segoe UI", 14, "bold"))
        self.rec_dot.pack(side="left", padx=(14, 6), pady=0)

        tk.Label(hdr, text="RECORDING MACRO", fg="#f2aa5c", bg="#161b27",
                 font=("Segoe UI", 9, "bold")).pack(side="left", pady=0)

        close_btn = tk.Button(hdr, text="✕", fg="#6b7280", bg="#161b27",
                              relief="flat", font=("Segoe UI", 10, "bold"),
                              cursor="hand2", bd=0, padx=8,
                              activebackground="#ef4444", activeforeground="white",
                              command=self.stop)
        close_btn.pack(side="right", padx=4)

        # ── Timer ──
        self.timer_label = tk.Label(inner, text="00:00", fg="#ffffff",
                                    bg="#0f1117",
                                    font=("Segoe UI", 42, "bold"))
        self.timer_label.pack(pady=(10, 2))

        # ── Status ──
        self.status_label = tk.Label(inner, text="Recording…", fg="#10b981",
                                     bg="#0f1117", font=("Segoe UI", 8, "italic"))
        self.status_label.pack()

        # ── Buttons ──
        btn_row = tk.Frame(inner, bg="#0f1117")
        btn_row.pack(fill="x", padx=14, pady=(8, 12))

        self.pause_btn = tk.Button(
            btn_row, text="⏸  Pause",
            command=self.toggle_pause,
            bg="#1e293b", fg="#f2aa5c",
            relief="flat", font=("Segoe UI", 9, "bold"),
            cursor="hand2", bd=0, pady=7,
            activebackground="#334155", activeforeground="#f2aa5c"
        )
        self.pause_btn.pack(side="left", fill="x", expand=True, padx=(0, 5))

        stop_btn = tk.Button(
            btn_row, text="⏹  Stop",
            command=self.stop,
            bg="#ef4444", fg="white",
            relief="flat", font=("Segoe UI", 9, "bold"),
            cursor="hand2", bd=0, pady=7,
            activebackground="#dc2626", activeforeground="white"
        )
        stop_btn.pack(side="left", fill="x", expand=True)

        self.window.protocol("WM_DELETE_WINDOW", self.stop)

        # ── Start timer NOW (before returning) ──
        self.recording = True
        self.start_time = time.time()
        self._keep_updating = True
        self._tick()          # use Tk's after() — no thread needed
        self._blink_dot()

        return self.window

    # ── Drag ────────────────────────────────────────────────────────────
    def _drag_start(self, e):
        self._drag_x = e.x; self._drag_y = e.y

    def _drag_move(self, e):
        if not self.window: return
        x = self.window.winfo_x() + e.x - self._drag_x
        y = self.window.winfo_y() + e.y - self._drag_y
        self.window.geometry(f"+{x}+{y}")

    # ── Timer via after() — runs on main thread, no race conditions ──────
    def _tick(self):
        if getattr(self, 'should_close', False):
            self._close()
            return
        if not self._keep_updating or not self.window:
            return
        if not self.paused:
            elapsed = time.time() - self.start_time - self.total_paused
            mins  = int(elapsed // 60)
            secs  = int(elapsed % 60)
            try:
                self.timer_label.config(text=f"{mins:02d}:{secs:02d}")
            except Exception:
                pass
        self.window.after(500, self._tick)

    # ── Blinking dot ────────────────────────────────────────────────────
    def _blink_dot(self):
        if not self._keep_updating or not self.window:
            return
        if not self.paused:
            self._dot_visible = not self._dot_visible
            try:
                self.rec_dot.config(fg="#ef4444" if self._dot_visible else "#0f1117")
            except Exception:
                pass
        self.window.after(600, self._blink_dot)

    # ── Pause / Resume ──────────────────────────────────────────────────
    def toggle_pause(self):
        if self.paused: self.resume()
        else: self.pause()

    def pause(self):
        if not self.recording or self.paused: return
        self.paused = True
        self.pause_time = time.time()
        try:
            self.pause_btn.config(text="▶  Resume", bg="#10b981",
                                  activebackground="#059669", fg="white")
            self.status_label.config(text="Paused", fg="#f59e0b")
            self.rec_dot.config(fg="#f59e0b")
        except Exception:
            pass
        if self.on_pause: self.on_pause()

    def resume(self):
        if not self.recording or not self.paused: return
        self.paused = False
        self.total_paused += time.time() - self.pause_time
        try:
            self.pause_btn.config(text="⏸  Pause", bg="#1e293b",
                                  activebackground="#334155", fg="#f2aa5c")
            self.status_label.config(text="Recording…", fg="#10b981")
        except Exception:
            pass
        if self.on_resume: self.on_resume()

    # ── Stop ────────────────────────────────────────────────────────────
    def stop(self):
        self.recording = False
        self._keep_updating = False
        if self.on_stop:
            threading.Thread(target=self.on_stop, daemon=True).start()
        if self.window:
            try:
                self.window.after(0, self._close)
            except Exception:
                pass

    def _close(self):
        if self.window:
            try: self.window.destroy()
            except Exception: pass
            self.window = None

    def close_from_outside(self):
        self.recording = False
        self._keep_updating = False
        self.should_close = True

    def show(self):
        self.create_window()


def show_macro_recorder(on_stop=None, on_pause=None, on_resume=None):
    ui = MacroRecorderUI(on_stop, on_pause, on_resume)
    ui.show()
    if ui.window:
        ui.window.mainloop()
    return ui