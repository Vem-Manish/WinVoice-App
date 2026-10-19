# macro_recorder.py
import time
import threading

# ── State ─────────────────────────────────────────────────────────────
_recording: bool = False
_events: list[dict] = []
_start_time: float = 0.0
_keyboard_listener = None
_mouse_listener = None
_lock = threading.Lock()  # BUG-28: mutex for start/stop_recording

# Called by main.py's /macros/stop-recording endpoint — no circular calls
_stop_hotkey_callback = None


def register_stop_hotkey_callback(cb):
    global _stop_hotkey_callback
    _stop_hotkey_callback = cb


def is_recording() -> bool:
    return _recording


def start_recording() -> str:
    global _recording, _events, _start_time, _keyboard_listener, _mouse_listener

    with _lock:
        if _recording:
            return "⚠️ Already recording."

        try:
            from pynput import keyboard as kb, mouse as ms
        except ImportError:
            return "❌ pynput not installed. Run: pip install pynput"

        _recording = True
        _events = []
        _start_time = time.time()

    def _t():
        return time.time() - _start_time

    # ── Track modifier keys ────────────────────────────────────────────
    _ctrl_held = [False]

    def on_key_press(key):
        if not _recording:
            return False   # stop listener

        # Track Ctrl state
        if key in (kb.Key.ctrl_l, kb.Key.ctrl_r):
            _ctrl_held[0] = True
            return  # don't record bare Ctrl

        # Detect Ctrl+Q → stop recording
        is_q = False
        try:
            is_q = (key.char is not None and key.char.lower() == 'q')
        except AttributeError:
            pass

        if _ctrl_held[0] and is_q:
            print("[Macro] Ctrl+Q — stopping recording")
            _stop_recording_internal()
            return False   # stop pynput listener

        # Record the event
        try:
            char = key.char
            if char is not None:
                _events.append({"type": "key_press", "key": char, "t": _t()})
            else:
                _events.append({"type": "key_press", "key": f"special:{key.name}", "t": _t()})
        except AttributeError:
            try:
                _events.append({"type": "key_press", "key": f"special:{key.name}", "t": _t()})
            except Exception:
                pass

    def on_key_release(key):
        if not _recording:
            return False

        if key in (kb.Key.ctrl_l, kb.Key.ctrl_r):
            _ctrl_held[0] = False
            return

        try:
            char = key.char
            if char is not None:
                _events.append({"type": "key_release", "key": char, "t": _t()})
            else:
                _events.append({"type": "key_release", "key": f"special:{key.name}", "t": _t()})
        except AttributeError:
            try:
                _events.append({"type": "key_release", "key": f"special:{key.name}", "t": _t()})
            except Exception:
                pass

    def on_click(x, y, button, pressed):
        if not _recording:
            return False
        _events.append({
            "type": "mouse_click",
            "x": x, "y": y,
            "button": button.name,
            "pressed": pressed,
            "t": _t()
        })

    def on_scroll(x, y, dx, dy):
        if not _recording:
            return False
        _events.append({"type": "mouse_scroll", "x": x, "y": y, "dx": dx, "dy": dy, "t": _t()})

    def on_move(x, y):
        if not _recording:
            return False
        # Throttle mouse moves
        if _events and _events[-1].get("type") == "mouse_move":
            if _t() - _events[-1]["t"] < 0.05:
                return
        _events.append({"type": "mouse_move", "x": x, "y": y, "t": _t()})

    _keyboard_listener = kb.Listener(on_press=on_key_press, on_release=on_key_release, daemon=True)
    _mouse_listener    = ms.Listener(on_click=on_click, on_scroll=on_scroll, on_move=on_move, daemon=True)
    _keyboard_listener.start()
    _mouse_listener.start()

    return "🔴 Recording started — perform your actions now."


def _stop_recording_internal():
    """Stop listeners and optionally fire the registered callback (e.g. Ctrl+Q path)."""
    global _recording
    if not _recording:
        return
    _recording = False

    try:
        if _keyboard_listener:
            _keyboard_listener.stop()
    except Exception:
        pass
    try:
        if _mouse_listener:
            _mouse_listener.stop()
    except Exception:
        pass

    # Fire registered callback (posts to /macros/stop-recording)
    if _stop_hotkey_callback:
        threading.Thread(target=_stop_hotkey_callback, daemon=True).start()


def stop_recording() -> tuple[list[dict], str]:
    """
    Called by /macros/stop-recording endpoint.
    Stops listeners and returns the recorded events.
    """
    global _recording, _keyboard_listener, _mouse_listener

    with _lock:
        if not _recording:
            # May have already been stopped by Ctrl+Q — return whatever we have
            events = list(_events)
            _events.clear()  # BUG-29: clear stale data
            return events, f"⏹️ Recorded {len(events)} events."

        _recording = False

    try:
        if _keyboard_listener:
            _keyboard_listener.stop()
    except Exception:
        pass
    try:
        if _mouse_listener:
            _mouse_listener.stop()
    except Exception:
        pass

    with _lock:
        events = list(_events)
        _events.clear()  # BUG-29: prevent stale data on second call

    # Strip leading mouse-move noise
    while events and events[0].get("type") == "mouse_move":
        events.pop(0)

    return events, f"⏹️ Recorded {len(events)} events."


def replay(events: list[dict], speed: float = 1.0) -> str:
    if not events:
        return "❌ No events to replay."

    try:
        from pynput.keyboard import Controller as KbCtrl, Key
        from pynput.mouse import Controller as MsCtrl, Button
    except ImportError:
        return "❌ pynput not installed."

    kb = KbCtrl()
    ms = MsCtrl()

    def _get_key(k: str):
        if k.startswith("special:"):
            name = k[8:]
            return getattr(Key, name, None)
        return k

    def _get_button(b: str):
        return getattr(Button, b, Button.left)

    try:
        prev_t = 0.0
        for ev in events:
            t = ev.get("t", 0)
            delay = (t - prev_t) / speed
            if delay > 0:
                time.sleep(max(0, delay))
            prev_t = t

            kind = ev.get("type")
            if kind == "key_press":
                key = _get_key(ev["key"])
                if key:
                    kb.press(key)
            elif kind == "key_release":
                key = _get_key(ev["key"])
                if key:
                    kb.release(key)
            elif kind == "mouse_move":
                ms.position = (ev["x"], ev["y"])
            elif kind == "mouse_click":
                ms.position = (ev["x"], ev["y"])
                btn = _get_button(ev["button"])
                if ev["pressed"]:
                    ms.press(btn)
                else:
                    ms.release(btn)
            elif kind == "mouse_scroll":
                ms.position = (ev["x"], ev["y"])
                ms.scroll(ev["dx"], ev["dy"])

        return "✅ Macro replayed successfully."
    except Exception as e:
        return f"❌ Replay error: {e}"


def get_summary(events: list[dict]) -> str:
    if not events:
        return "empty"
    duration  = events[-1].get("t", 0)
    clicks    = sum(1 for e in events if e.get("type") == "mouse_click" and e.get("pressed"))
    keystrokes = sum(1 for e in events if e.get("type") == "key_press")
    scrolls   = sum(1 for e in events if e.get("type") == "mouse_scroll")
    return f"{len(events)} events · {duration:.1f}s · {keystrokes} keys · {clicks} clicks · {scrolls} scrolls"