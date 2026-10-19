# whatsapp_agent.py
# Sends WhatsApp messages by automating WhatsApp Web.
#
# Flow:
#   1. Bring WhatsApp Web browser tab to front (PowerShell on Windows)
#   2. Click the search bar (one saved coordinate from calibration)
#   3. Paste contact name → wait for results → press Enter to open chat
#   4. Wait 0.8s (WA auto-focuses message box) → paste message → Enter
#
# One-time setup: say "calibrate whatsapp".
# Hover over the WhatsApp search bar during the 10-second countdown.
# Coordinates saved to .ww_calibration.json — never needs redoing.
#
# Requirements: pip install pyautogui pyperclip

import time
import webbrowser
import threading
import multiprocessing
import json
import os
import subprocess
import sys
import traceback
import re

from ai_backend import _call_ollama_chat

# ── Paths ─────────────────────────────────────────────────────────────
_BASE             = os.path.dirname(os.path.abspath(__file__))
WW_SESSION_FILE   = os.path.join(_BASE, ".ww_session")
WW_CALIBRATE_FILE = os.path.join(_BASE, ".ww_calibration.json")

# ── AI prompt ─────────────────────────────────────────────────────────
WHATSAPP_SYSTEM_PROMPT = """You are an intelligent WhatsApp assistant. Parse ANY WhatsApp message request and output ONLY valid JSON.

ACTIONS:
send_message(contact_name, message)

OUTPUT FORMAT:
{"steps": [{"action":"action_name","data":{"field":"value"}}]}

INTELLIGENCE RULES:
- "send whatsapp to X saying Y" → send_message
- "whatsapp X that Y" → send_message
- "message X on whatsapp: Y" → send_message
- PIPED DATA: If the prompt contains "[CONTEXT DATA FROM PREVIOUS STEP]", do NOT try to write it out yourself. You MUST output EXACTLY the literal string "[PIPED_DATA]" inside the `message` field!
- contact_name: name exactly as it appears in WhatsApp contacts
- message: full text to send

EXAMPLES:
User: send a whatsapp to manish saying I will be late
{"steps": [{"action":"send_message","data":{"contact_name":"Manish","message":"I will be late"}}]}

User: whatsapp mom that dinner is ready
{"steps": [{"action":"send_message","data":{"contact_name":"Mom","message":"Dinner is ready"}}]}

User: send manish a whatsapp saying hey what is up
{"steps": [{"action":"send_message","data":{"contact_name":"Manish","message":"Hey what is up"}}]}

User: [CONTEXT DATA FROM PREVIOUS STEP]
(Some very long text here)
Task: send it to manish
{"steps": [{"action":"send_message","data":{"contact_name":"Manish","message":"Here is the document data:\n[PIPED_DATA]"}}]}"""


# ══════════════════════════════════════════════════════════════════════
# CALIBRATION — WinVoice-themed popup
# ══════════════════════════════════════════════════════════════════════

def _calibration_process(result_queue: multiprocessing.Queue):
    try:
        import pyautogui
        # PySide6 handles Windows high-DPI correctly (unlike tkinter)
        from PySide6.QtWidgets import (QApplication, QWidget, QVBoxLayout,
                                       QHBoxLayout, QLabel, QPushButton)
        from PySide6.QtCore import Qt, QTimer
        from PySide6.QtGui import QFont

        app = QApplication(sys.argv)

        captured = [None]

        # ── Main window ──────────────────────────────────────────────────
        win = QWidget()
        win.setWindowTitle("WinVoice — WhatsApp Calibration")
        win.setFixedSize(700, 460)
        win.setWindowFlags(win.windowFlags() | Qt.WindowStaysOnTopHint)
        win.setStyleSheet("""
            QWidget#main {
                background: #0d0d12;
                border: 2px solid #e3963e;
            }
        """)
        win.setObjectName("main")

        layout = QVBoxLayout(win)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── Header bar ───────────────────────────────────────────────────
        header = QWidget()
        header.setFixedHeight(52)
        header.setStyleSheet("background: #13131a;")
        hdr_layout = QHBoxLayout(header)
        hdr_layout.setContentsMargins(20, 0, 20, 0)

        title = QLabel("WhatsApp Configuration")
        title.setFont(QFont("Segoe UI", 12, QFont.Bold))
        title.setStyleSheet("color: #e3963e;")
        hdr_layout.addWidget(title)
        
        layout.addWidget(header)

        # ── Body ─────────────────────────────────────────────────────────
        body = QWidget()
        body.setStyleSheet("background: #0d0d12;")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(40, 30, 40, 30)
        body_layout.setAlignment(Qt.AlignCenter)

        instruction = QLabel("Place your cursor on the WhatsApp\nsearch bar when the timer hits 0.")
        instruction.setFont(QFont("Segoe UI", 16, QFont.Bold))
        instruction.setStyleSheet("color: #ffffff; background: transparent;")
        instruction.setAlignment(Qt.AlignCenter)
        body_layout.addWidget(instruction)

        body_layout.addSpacing(20)

        # Countdown label (hidden initially)
        cnt_label = QLabel("")
        cnt_label.setFont(QFont("Segoe UI", 96, QFont.Bold))
        cnt_label.setStyleSheet("color: #e3963e; background: transparent;")
        cnt_label.setAlignment(Qt.AlignCenter)
        cnt_label.hide()
        body_layout.addWidget(cnt_label)

        # Status label (hidden initially)
        status_label = QLabel("")
        status_label.setFont(QFont("Segoe UI", 12))
        status_label.setStyleSheet("color: #10b981; background: transparent; font-style: italic;")
        status_label.setAlignment(Qt.AlignCenter)
        status_label.hide()
        body_layout.addWidget(status_label)

        # Start button
        start_btn = QPushButton("Start")
        start_btn.setFixedSize(200, 55)
        start_btn.setFont(QFont("Segoe UI", 14, QFont.Bold))
        start_btn.setCursor(Qt.PointingHandCursor)
        start_btn.setStyleSheet("""
            QPushButton {
                background: #e3963e;
                color: #ffffff;
                border: none;
                border-radius: 8px;
            }
            QPushButton:hover {
                background: #f2aa5c;
            }
            QPushButton:pressed {
                background: #c97f2e;
            }
        """)
        body_layout.addWidget(start_btn, alignment=Qt.AlignCenter)

        layout.addWidget(body)

        # ── Countdown logic ──────────────────────────────────────────────
        timer = QTimer()
        timer.setInterval(1000)
        countdown = [10]

        def on_tick():
            countdown[0] -= 1
            n = countdown[0]
            if n <= 0:
                timer.stop()
                captured[0] = pyautogui.position()
                cnt_label.setText("✓")
                cnt_label.setStyleSheet("color: #10b981; background: transparent;")
                status_label.setText("Done! Position saved.")
                status_label.show()
                result_queue.put({"ok": True,
                                  "x": int(captured[0][0]),
                                  "y": int(captured[0][1])})
                QTimer.singleShot(1200, app.quit)
                return
            cnt_label.setText(str(n))
            if n <= 5:
                cnt_label.setStyleSheet("color: #10b981; background: transparent;")

        timer.timeout.connect(on_tick)

        def on_start():
            start_btn.hide()
            cnt_label.setText("10")
            cnt_label.show()
            status_label.show()
            timer.start()

        start_btn.clicked.connect(on_start)

        # ── Centre on screen and show ────────────────────────────────────
        screen = app.primaryScreen().geometry()
        win.move((screen.width() - win.width()) // 2,
                 (screen.height() - win.height()) // 2)
        win.show()
        app.exec()

        if captured[0] is None:
            result_queue.put({"ok": False, "error": "Closed before calibration completed."})

    except Exception as e:
        result_queue.put({"ok": False, "error": str(e), "tb": traceback.format_exc()})


def _load_cal() -> dict | None:
    if os.path.exists(WW_CALIBRATE_FILE):
        try:
            with open(WW_CALIBRATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return None


def _save_cal(x: int, y: int):
    with open(WW_CALIBRATE_FILE, "w") as f:
        json.dump({"search_x": x, "search_y": y}, f, indent=2)


def calibrate_whatsapp() -> str:
    """
    Spawn a child process that owns Tkinter entirely.
    Waits for the result via a multiprocessing Queue.
    Safe to call from any thread (including asyncio workers).
    """
    try:
        import pyautogui  # just to check it's installed
    except ImportError:
        return "❌ pyautogui not installed. Run: pip install pyautogui"

    result_queue: multiprocessing.Queue = multiprocessing.Queue()

    proc = multiprocessing.Process(
        target=_calibration_process,
        args=(result_queue,),
        daemon=True
    )
    proc.start()
    proc.join(timeout=90)

    if proc.is_alive():
        proc.terminate()
        return "⏱️ Calibration timed out. Please try again."

    if result_queue.empty():
        return "❌ Calibration process ended without a result."

    res = result_queue.get()
    if not res.get("ok"):
        err = res.get("error", "Unknown error")
        print(f"[Calibration] Error: {err}")
        if res.get("tb"):
            print(res["tb"])
        return f"❌ Calibration failed: {err}"

    x, y = res["x"], res["y"]
    _save_cal(x, y)
    print(f"[Calibration] Search bar saved at ({x}, {y})")

    return (
        f"✅ WhatsApp calibrated!\n"
        f"   Search bar locked at ({x}, {y}).\n"
        f"   You're all set — say 'send a whatsapp to [name] saying [message]'!"
    )


# ══════════════════════════════════════════════════════════════════════
# TAB FOCUS
# ══════════════════════════════════════════════════════════════════════

def _focus_whatsapp_tab() -> bool:
    if sys.platform != "win32":
        webbrowser.open("https://web.whatsapp.com")
        return True

    ps = r"""
$shell = New-Object -ComObject Shell.Application
$found = $false
foreach ($win in $shell.Windows()) {
    try {
        if ($win.LocationURL -like '*web.whatsapp.com*') {
            $hwnd = [IntPtr]$win.HWND
            Add-Type -TypeDefinition @"
using System;using System.Runtime.InteropServices;
public class U32{
  [DllImport("user32.dll")]public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")]public static extern bool ShowWindow(IntPtr h,int n);
}
"@ -ErrorAction SilentlyContinue
            [U32]::ShowWindow($hwnd, 9)
            [U32]::SetForegroundWindow($hwnd)
            $found = $true
            break
        }
    } catch {}
}
if (-not $found) { Start-Process 'https://web.whatsapp.com' }
Write-Output $found
"""
    try:
        r = subprocess.run(
            ["powershell", "-Command", ps],
            capture_output=True, text=True, timeout=8,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        if "False" in r.stdout or not r.stdout.strip():
            time.sleep(8.0)
        return True
    except Exception as e:
        print(f"[WhatsApp] Tab focus failed: {e}")
        webbrowser.open("https://web.whatsapp.com")
        time.sleep(8.0)
        return True


# ══════════════════════════════════════════════════════════════════════
# CORE SEND
# ══════════════════════════════════════════════════════════════════════

def _copy_file_to_clipboard(file_path: str):
    """Copies a file to the Windows clipboard as a shell file drop list (CF_HDROP)."""
    import struct
    import win32clipboard
    abs_path = os.path.abspath(file_path)
    encoded = abs_path.encode('utf-16le') + b'\x00\x00\x00\x00'
    dropfiles = struct.pack('IIIII', 20, 0, 0, 0, 1) + encoded
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_HDROP, dropfiles)
    finally:
        win32clipboard.CloseClipboard()


def _send_via_browser(contact_name: str, message: str, file_path: str = None) -> str:
    try:
        import pyautogui
        import pyperclip
    except ImportError:
        return "❌ pyautogui or pyperclip not installed. Run: pip install pyautogui pyperclip"

    pyautogui.PAUSE    = 0.05
    pyautogui.FAILSAFE = True

    cal = _load_cal()
    if not cal:
        return "⚠️ Not calibrated. Say 'calibrate whatsapp' first."

    sx, sy = cal["search_x"], cal["search_y"]

    # Try to block user input so they don't interrupt the automation
    input_blocked = False
    try:
        import ctypes
        input_blocked = ctypes.windll.user32.BlockInput(True)
    except Exception:
        pass

    try:
        # ── 1. Bring WhatsApp Web to front ────────────────────────────────
        print("   📱 Focusing WhatsApp Web tab...")
        _focus_whatsapp_tab()

        # Read delay preference
        delay = 2.0
        pref_file = os.path.join(_BASE, "preferences.json")
        if os.path.exists(pref_file):
            try:
                with open(pref_file, "r") as f:
                    delay = float(json.load(f).get("whatsapp_delay", 2.0))
            except Exception:
                pass

        print(f"   ⏳ Waiting {delay}s for WhatsApp to load...")
        time.sleep(delay)

        # ── 2. Click the search bar ───────────────────────────────────────
        print(f"   🔍 Clicking search bar at ({sx}, {sy})...")
        pyautogui.click(sx, sy)
        time.sleep(0.4)

        # Clear any leftover text
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.1)
        pyautogui.press("backspace")
        time.sleep(0.1)

        # ── 3. Paste contact name, wait for results ───────────────────────
        print(f"   👤 Searching for '{contact_name}'...")
        pyperclip.copy(contact_name)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(2.0)

        # ── 4. Press Enter — opens first/top search result ────────────────
        pyautogui.press("enter")
        time.sleep(1.0)

        # ── 5. Paste file (if present) or message and send ────────────────
        if file_path and os.path.isfile(file_path):
            fn = os.path.basename(file_path)
            print(f"   📎 Attaching and pasting file: '{fn}'...")
            _copy_file_to_clipboard(file_path)
            time.sleep(0.4)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(1.2)  # Wait for WhatsApp Web to process file paste
            pyautogui.press("enter")
            print(f"   🚀 Sent file: '{fn}'")
            time.sleep(1.0)  # Wait for file send to finish and chat input box to refocus

            # If user provided a follow-up message / text to say
            clean_msg = (message or "").strip()
            if clean_msg and clean_msg.lower() not in (f"here is {fn}".lower(), "here is the document data:"):
                print(f"   ✍️  Sending follow-up message: '{clean_msg[:50]}'")
                pyperclip.copy(clean_msg)
                time.sleep(0.3)
                pyautogui.hotkey("ctrl", "v")
                time.sleep(0.4)
                pyautogui.press("enter")
                time.sleep(0.4)
                return f"✅ File '{fn}' and message sent to {contact_name} on WhatsApp"

            return f"✅ File '{fn}' sent to {contact_name} on WhatsApp"
        else:
            short = message[:50] + ("..." if len(message) > 50 else "")
            print(f"   ✍️  Sending: '{short}'")
            pyperclip.copy(message)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.4)
            pyautogui.press("enter")
            time.sleep(0.3)
            return f"✅ WhatsApp message sent to {contact_name}"
    finally:
        if input_blocked:
            try:
                ctypes.windll.user32.BlockInput(False)
            except Exception:
                pass


def _send_thread(contact_name: str, message: str, out: list, file_path: str = None):
    try:
        out.append(_send_via_browser(contact_name, message, file_path=file_path))
    except Exception as e:
        traceback.print_exc()
        out.append(f"❌ WhatsApp automation error: {e}")


# ══════════════════════════════════════════════════════════════════════
# SETUP (called from homepage Connect button)
# ══════════════════════════════════════════════════════════════════════

def setup_whatsapp() -> bool:
    webbrowser.open("https://web.whatsapp.com")
    print("\n=== WhatsApp Web Setup ===")
    print("Scan the QR code in your browser with your phone.")
    with open(WW_SESSION_FILE, "w") as f:
        f.write("connected")
    print("✅ Session marker saved. Running calibration now...")
    print(calibrate_whatsapp())
    return True


def _is_setup() -> bool:
    return os.path.exists(WW_SESSION_FILE)


# ══════════════════════════════════════════════════════════════════════
# AGENT
# ══════════════════════════════════════════════════════════════════════

def _fix_wrapper(plan: dict) -> dict:
    if not plan:
        return plan
    action = plan.get("action")
    data   = plan.get("data")
    if action and not data:
        top = {k: v for k, v in plan.items() if k != "action"}
        if top:
            return {"action": action, "data": top}
    return plan


class WhatsAppAgent:
    def __init__(self, contacts_session: dict | None = None):
        self.contacts_session = contacts_session

    def run(self, query: str, status_callback=None, piped_context: str = None) -> str:
        q = query.strip().lower()
        if status_callback: status_callback("Analyzing WhatsApp Command...")

        if "calibrate whatsapp" in q or "setup whatsapp" in q:
            if status_callback: status_callback("Calibrating WhatsApp...")
            return calibrate_whatsapp()

        piped_context = ""
        if "[CONTEXT DATA FROM PREVIOUS STEP" in query:
            parts = query.split("Task:")
            if len(parts) > 1:
                piped_context = parts[0].split("]\n")[-1].strip()

        # Check for explicit [ATTACHMENT_FILE]: path from previous step or FilesAgent
        raw_query = query or ""
        attached_file = None
        attached_name = None
        attached_size = None
        m_file = re.search(r'\[ATTACHMENT_FILE\]:\s*([^\r\n]+)', raw_query)
        if m_file:
            fp = m_file.group(1).strip()
            if os.path.isfile(fp):
                attached_file = fp
                attached_name = os.path.basename(fp)
                try:
                    from files_agent import format_size
                    attached_size = format_size(os.path.getsize(fp))
                except Exception:
                    attached_size = ""

        raw = _call_ollama_chat(system=WHATSAPP_SYSTEM_PROMPT, user=query + "\nJSON:")
        if not raw:
            return "❌ AI returned empty response."

        plan   = _fix_wrapper(raw)
        
        steps = plan.get("steps", [plan]) if "steps" in plan else [plan]
        results = []
        for step in steps:
            action = step.get("action", "")
            data   = step.get("data", {})
            if not action:
                continue

            if attached_file:
                data["file_path"] = attached_file
                data["file_name"] = attached_name
                data["file_size"] = attached_size
                
                # Check for explicit saying / message in the query
                clean_q = query.split("Task:")[-1] if "Task:" in query else query
                m_say = re.search(r'\b(?:saying|with\s+message|that)\s+(.+)$', clean_q, re.I)
                if m_say:
                    data["message"] = m_say.group(1).strip()
                else:
                    data["message"] = ""
            elif piped_context and "message" in data:
                if "[PIPED_DATA]" in data["message"]:
                    data["message"] = data["message"].replace("[PIPED_DATA]", piped_context)
                else:
                    data["message"] += f"\n\n{piped_context}"

            if "message" in data and isinstance(data["message"], str):
                data["message"] = re.sub(r'\[ATTACHMENT_FILE\]:[^\r\n]*', '', data["message"])
                data["message"] = data["message"].replace("[ATTACHMENT_FILE]", "")
                data["message"] = data["message"].replace("[PIPED_DATA]", "").strip()

            if action == "send_message":
                if status_callback: status_callback(f"Drafting message to {data.get('contact_name', '')}")
                res = self._send(data)
                if isinstance(res, dict): return res
                results.append(res)
            else:
                results.append(f"❌ Unknown WhatsApp action: {action}")
                
        return "\n".join(results) if results else "❌ Could not understand the WhatsApp command."

    def _send(self, data: dict) -> str | dict:
        contact_name = data.get("contact_name", "").strip()
        message      = data.get("message", "").strip()

        if not contact_name:
            return "❌ Contact name is required."
        if not message and not data.get("file_path"):
            return "❌ Message or file is required."
        if not _is_setup():
            return (
                "⚠️ WhatsApp Web not set up.\n"
                "   Go to Connections → Connect WhatsApp and scan the QR code."
            )
        if not _load_cal():
            return (
                "⚠️ WhatsApp not calibrated.\n"
                "   Say 'calibrate whatsapp' and hover over the search bar when prompted."
            )

        # Instead of automating immediately, yield pending confirmation
        ret = {
            "pending_confirmation": True,
            "type": "whatsapp_send",
            "contact_name": contact_name,
            "message": message
        }
        if data.get("file_path"):
            ret["file_path"] = data["file_path"]
            ret["file_name"] = data.get("file_name", os.path.basename(data["file_path"]))
            ret["file_size"] = data.get("file_size", "")
        return ret

    def execute_pending(self, pending: dict):
        if pending.get("type") == "whatsapp_send":
            contact_name = pending.get("contact_name")
            message = pending.get("message")
            file_path = pending.get("file_path")
            
            print(f"[WhatsApp] → '{contact_name}': message='{message[:60] if message else ''}' file='{file_path}'")
            out = []
            t = threading.Thread(target=_send_thread, args=(contact_name, message, out, file_path), daemon=True)
            t.start()
            t.join(timeout=35)
            return out[0] if out else "⏱️ WhatsApp send timed out."
        return "❌ Unknown pending type."