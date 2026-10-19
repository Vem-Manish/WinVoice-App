# ── UTF-8 stdout/stderr (prevents charmap crash on Windows with emoji) ──
import sys as _sys
for _s in (_sys.stdout, _sys.stderr):
    if _s and hasattr(_s, 'reconfigure'):
        try: _s.reconfigure(encoding='utf-8', errors='replace')
        except Exception: pass
del _sys, _s

from fastapi import FastAPI, Request, Depends, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
import memory_store

import database as db
import subprocess
import os
import json
import time
import threading
import base64
import urllib.parse
import requests
from dotenv import load_dotenv
from pydantic import BaseModel
import asyncio
import traceback

load_dotenv()

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = "http://127.0.0.1:8000/auth/callback"
GOOGLE_SCOPES = " ".join([
    "openid", "email", "profile",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
])

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")
SPOTIFY_REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8000/auth/spotify/callback")
SPOTIFY_SCOPES = " ".join([
    "user-modify-playback-state",
    "user-read-playback-state",
    "user-read-currently-playing",
    "playlist-read-private",
    "user-library-read",
    "streaming",
])

# ── Server Configuration ──────────────────────────────────────────────
APP_HOST = "127.0.0.1"
APP_PORT = 8000

# ── Files ──────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SESSION_FILE = os.path.join(BASE_DIR, "session.json")
from tools import session_lock as _session_lock
SPOTIFY_CACHE = os.path.join(BASE_DIR, ".spotify_token")
CURRENT_USER_FILE = os.path.join(BASE_DIR, "current_user.json")
PREFERENCES_FILE = os.path.join(BASE_DIR, "preferences.json")
# Root of the drive that BASE_DIR lives on — used for disk usage in the dashboard
_disk_path = os.path.splitdrive(BASE_DIR)[0] + os.sep  # e.g. "C:\\"

import datetime as dt

# ── GLOBAL CACHE STORAGE ──────────────────────────────────────────────
# This prevents the 30-second lag by keeping "slow" data in memory
_db_cache = {
    "slow_data": {
        "emails": [],
        "events": [],
        "weather": {}
    },
    "last_update": 0
}

_command_lock = None  # initialised in lifespan after asyncio is available
_wake_event = None    # initialised in lifespan after asyncio is available (BUG-7)
_rich_update_lock: "asyncio.Lock | None" = None  # initialised in lifespan (PERF-3)
_reset_saved_task = None  # tracks the last _reset_saved task (MED-15)
_macro_recording = {"name": None, "steps": []}

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app_instance):
    global _command_lock, _wake_event, _rich_update_lock
    _command_lock = asyncio.Lock()       # safe to create here — event loop is running
    _wake_event = asyncio.Event()        # BUG-7: moved here so event loop exists
    _rich_update_lock = asyncio.Lock()   # PERF-3: prevents dashboard cache race
    loop = asyncio.get_event_loop()
    try:
        from state_store import init_state_db
        from background_jobs import start_background_jobs
        init_state_db()
        start_background_jobs()
        print("[State] Local history/macros/scheduler ready")
    except Exception as e:
        print(f"[State] Startup skipped: {e}")

    # 1. Warm up Ollama in background
    def _init_ollama():
        try:
            from ai_backend import initialize_ai
            initialize_ai()
        except Exception as e:
            print(f"[Ollama] Init error: {e}")

    loop.run_in_executor(None, _init_ollama)

    # 1.5 Warm up Vector Database (RAG Memory)
    # Delayed by 4s so chromadb's internal numpy import doesn't race with
    # Whisper's numpy import (both starting simultaneously caused NDArray crash)
    def _init_memory_delayed():
        import time as _t
        _t.sleep(4)
        memory_store.init_memory()

    loop.run_in_executor(None, _init_memory_delayed)

    # 2. Microphone calibration + Whisper preload in background
    try:
        import voice
        loop.run_in_executor(None, voice.calibrate)
        try:
            loop.run_in_executor(None, voice.preload_whisper)
        except Exception:
            pass  # Whisper optional
        print("[Voice] Calibration + Whisper preload started")
    except Exception as e:
        print(f"[Voice] Calibration skipped: {e}")

    # 3. Token auto-refresh background job

    # 4. Ollama keep-alive — ping every 20 min so model stays in RAM
    # Without this, Ollama unloads after 10min of inactivity and next command
    # pays the full cold-start penalty again (~3-4s)
    def _ollama_keepalive():
        import time as _time
        while True:
            _time.sleep(4 * 60)  # every 4 minutes
            try:
                from ai_backend import OLLAMA_BASE_URL, OLLAMA_MODEL, ROUTER_MODEL
                for mdl in set([OLLAMA_MODEL, ROUTER_MODEL]):
                    requests.post(
                        f"{OLLAMA_BASE_URL}/api/chat",
                        json={
                            "model": mdl,
                            "messages": [{"role": "user", "content": "ping"}],
                            "stream": False,
                            "keep_alive": "30m",
                            "options": {"num_predict": 1, "temperature": 0}
                        },
                        timeout=10
                    )
            except Exception:
                pass

    threading.Thread(target=_ollama_keepalive, daemon=True, name="OllamaKeepAlive").start()

    # 5. Start wake-word background listener (mic always-on in background)
    # When "Hey WinVoice" is detected it POSTs to /wake/trigger which fires
    # the SSE stream that Electron's main.js listens to, showing the popup.
    # NOTE: We wait 5 seconds before starting so that numpy is fully initialised
    # by Whisper / memory_store before voice_auth also tries to import it.
    # Starting them simultaneously causes a "NDArray circular import" crash.
    def _start_wake_word():
        try:
            time.sleep(5)   # let numpy finish loading across all other modules first
            import voice_auth
            listener = voice_auth.WakeWordListener(on_wake=lambda: None)
            listener.start()
            print("[WakeWord] Background listener started — say 'Hey WinVoice'")
        except Exception as e:
            print(f"[WakeWord] Could not start listener: {e}")

    threading.Thread(target=_start_wake_word, daemon=True, name="WakeWordStarter").start()

    yield


app = FastAPI(lifespan=lifespan)

# Try to initialize DB, but gracefully handle connection errors
try:
    db.init_db()
    print("[DB] Database initialized successfully")
except Exception as e:
    print(f"[DB] Database initialization deferred (may retry on first request): {e}")

# Mount static files only if directory exists
_static_dir = os.path.join(BASE_DIR, "static")
if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")

_assets_dir = os.path.join(BASE_DIR, "assets")
if os.path.isdir(_assets_dir):
    app.mount("/assets", StaticFiles(directory=_assets_dir), name="assets")

templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


def get_db():
    database = db.SessionLocal()
    try:
        yield database
    finally:
        database.close()


# ═══════════════════════════════════════════════════════════════════════
# PREFERENCES HELPERS
# ═══════════════════════════════════════════════════════════════════════

def _read_preferences():
    if os.path.exists(PREFERENCES_FILE):
        try:
            with open(PREFERENCES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
    return {"ask_before_send_email": True, "ask_before_deleting_emails": True, "context_length": 5, "whatsapp_delay": 2.0, "tts_enabled": True}


def _write_preferences(data: dict):
    with open(PREFERENCES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# ═══════════════════════════════════════════════════════════════════════
# AGENT HELPERS (extracted from gui.py — no Qt/PySide6 imports needed)
# ═══════════════════════════════════════════════════════════════════════

AGENT_MAP = {
    "GMAIL_TASK": "gmail",
    "CALENDAR_TASK": "calendar",
    "TASKS_TASK": "tasks",
    "DOCS_TASK": "docs",
    "SHEETS_TASK": "sheets",
    "CONTACTS_TASK": "contacts",
    "WHATSAPP_TASK": "whatsapp",
    "SPOTIFY_TASK": "spotify",
    "WEATHER_TASK": "weather",
    "TIMER_TASK": "timer",
    "SYSTEM_TASK": "system",
    "MAPS_TASK": "maps",
    "GENERAL_TASK": "general",
    "MEMORY_TASK": "memory",
    "FILES_TASK": "files",
    "FILE_TASK": "files",
    "FIND_FILE": "files",
}

GOOGLE_REQUIRED = {
    "GMAIL_TASK", "CALENDAR_TASK", "TASKS_TASK", "DOCS_TASK",
    "SHEETS_TASK", "CONTACTS_TASK", "WHATSAPP_TASK",
}

_stateless_agents: dict = {}
_google_agents: dict = {}
_google_session_cache = None
_last_session_mtime = None


def _load_session() -> dict | None:
    if os.path.exists(SESSION_FILE):
        try:
            with _session_lock, open(SESSION_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None


def _get_agents() -> dict:
    global _stateless_agents, _google_agents, _last_session_mtime, _google_session_cache

    # Initialise stateless agents once
    if not _stateless_agents:
        try:
            from weather_agent import WeatherAgent
            from timer_agent import TimerAgent
            from system_agent import SystemAgent
            from maps_agent import MapsAgent
            from general_agent import GeneralAgent
            from memory_agent import MemoryAgent
            from files_agent import FilesAgent
            _stateless_agents = {
                "weather": WeatherAgent(),
                "timer": TimerAgent(),
                "system": SystemAgent(),
                "maps": MapsAgent(),
                "general": GeneralAgent(),
                "memory": MemoryAgent(),
                "files": FilesAgent(),
            }
        except Exception as e:
            print(f"[Agents] Stateless init error: {e}")

    # Spotify (re-init each call — token may change)
    try:
        from spotify_agent import SpotifyAgent
        _stateless_agents["spotify"] = SpotifyAgent()
    except Exception:
        pass

    # Google agents — reload only when session.json changes
    if os.path.exists(SESSION_FILE):
        try:
            mtime = os.path.getmtime(SESSION_FILE)
            if _last_session_mtime != mtime:
                _last_session_mtime = mtime
                with _session_lock, open(SESSION_FILE, "r", encoding="utf-8") as f:
                    _google_session_cache = json.load(f)
                
                if _google_session_cache:
                    from gmail_agent import GmailAgent
                    from calendar_agent import CalendarAgent
                    from tasks_agent import TasksAgent
                    from docs_agent import DocsAgent
                    from sheets_agent import SheetsAgent
                    from contacts_agent import ContactsAgent
                    from whatsapp_agent import WhatsAppAgent
                    _google_agents = {
                        "gmail": GmailAgent(_google_session_cache),
                        "calendar": CalendarAgent(_google_session_cache),
                        "tasks": TasksAgent(_google_session_cache),
                        "docs": DocsAgent(_google_session_cache),
                        "sheets": SheetsAgent(_google_session_cache),
                        "contacts": ContactsAgent(_google_session_cache),
                        "whatsapp": WhatsAppAgent(contacts_session=_google_session_cache),
                    }
        except Exception as e:
            print(f"[Agents] Google agents init error: {e}")

    return {**_stateless_agents, **_google_agents}


def _sanitize(steps: list) -> list:
    """Remove 'shutdown' steps if a Stop-Process step exists (safety guard)."""
    has_stop = any(
        "Stop-Process" in str(s.get("command", ""))
        for s in steps if s.get("action") == "POWERSHELL"
    )
    has_shut = any(
        "shutdown" in str(s.get("command", ""))
        for s in steps if s.get("action") == "POWERSHELL"
    )
    if has_stop and has_shut:
        return [s for s in steps if "shutdown" not in str(s.get("command", ""))]
    return steps


# ═══════════════════════════════════════════════════════════════════════
# CORE ROUTES
# ═══════════════════════════════════════════════════════════════════════

@app.get("/healthz")
async def healthz():
    return {"status": "ok", "ok": True}


@app.get("/startup/summary")
async def startup_summary():
    try:
        from weather_agent import WeatherAgent
        text = await asyncio.to_thread(WeatherAgent().run, "weather today")
        return {"status": "success", "message": "Daily summary", "summary": text}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ── Shell window reference (set by app_shell.py) ───────────────────────
_shell_window = None  # BrowserWindow instance injected by app_shell


@app.post("/shell/expand")
async def shell_expand():
    """
    Called by the in-page expand button to tell the Qt window to go fullscreen.
    app_shell.py sets _shell_window so we can call set_full_mode() here.
    """
    import main as _self
    if _self._shell_window is not None:
        try:
            from PySide6.QtCore import QMetaObject, Qt
            QMetaObject.invokeMethod(_self._shell_window, "set_full_mode", Qt.QueuedConnection)
        except Exception as e:
            print(f"[shell/expand] Qt invoke error: {e}")
    return {"ok": True}


@app.post("/shell/popup")
async def shell_popup():
    """
    Called by UI mode toggle to return Qt window to popup mode.
    """
    import main as _self
    if _self._shell_window is not None:
        try:
            from PySide6.QtCore import QMetaObject, Qt
            QMetaObject.invokeMethod(_self._shell_window, "set_popup_mode", Qt.QueuedConnection)
        except Exception as e:
            print(f"[shell/popup] Qt invoke error: {e}")
    return {"ok": True}


# ── ELECTRON BRIDGE (Wake Word) ──────────────────────────────────────────
# _wake_event is now initialised inside lifespan() to avoid BUG-7
# (asyncio.Event() created before the event loop exists at module import time)


@app.post("/wake/trigger")  # called by voice_auth when wake fires
async def wake_trigger():
    _wake_event.set()
    return {"ok": True}


@app.get("/wake/stream")  # Electron listens on this SSE stream
async def wake_stream():
    async def generate():
        while True:
            await _wake_event.wait()
            _wake_event.clear()
            yield b"data: wake\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.get("/", response_class=HTMLResponse)
async def root():
    return RedirectResponse("/homepage")


@app.get("/homepage", response_class=HTMLResponse)
async def homepage(request: Request):
    return templates.TemplateResponse("homepage.html", {"request": request})


# ═══════════════════════════════════════════════════════════════════════
# AUTH — USER
# ═══════════════════════════════════════════════════════════════════════

@app.post("/signup")
async def signup(
        name: str = Form(...),
        email: str = Form(...),
        password: str = Form(...),
        session: Session = Depends(get_db),
):
    if session.query(db.User).filter(db.User.email == email).first():
        return {"status": "error", "message": "Email already registered"}

    session.add(db.User(
        username=name,
        email=email,
        hashed_password=db.get_password_hash(password),
    ))
    session.commit()
    return {"status": "success", "message": "Account created"}


@app.post("/login")
async def login(
        email: str = Form(...),
        password: str = Form(...),
        session: Session = Depends(get_db),
):
    user = session.query(db.User).filter(db.User.email == email).first()
    if not user or not db.verify_password(password, user.hashed_password):
        return {"status": "error", "message": "Invalid credentials"}

    with open(CURRENT_USER_FILE, "w", encoding="utf-8") as f:
        json.dump({"email": user.email, "username": user.username}, f)

    # Return both status and redirect for compatibility with old/new frontends
    return {"status": "success", "redirect": "/homepage"}


@app.post("/logout")
async def logout():
    try:
        if os.path.exists(CURRENT_USER_FILE):
            os.remove(CURRENT_USER_FILE)
    except Exception:
        pass
    return {"status": "success"}


@app.get("/me")
async def me(session: Session = Depends(get_db)):
    if not os.path.exists(CURRENT_USER_FILE):
        return {"status": "error", "message": "Not logged in"}
    try:
        with open(CURRENT_USER_FILE, "r", encoding="utf-8") as f:
            cur = json.load(f)
        email = cur.get("email", "")
        if not email:
            return {"status": "error", "message": "Not logged in"}
        user = session.query(db.User).filter(db.User.email == email).first()
        if not user:
            # Auto-provision user in local DB if session/database was recreated
            username = cur.get("username", "User")
            user = db.User(username=username, email=email, hashed_password="")
            session.add(user)
            session.commit()
            session.refresh(user)
        return {
            "status": "success",
            "email": user.email,
            "username": user.username,
            "user": {"username": user.username, "email": user.email}
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/profile/update")
async def profile_update(
        username: str = Form(...),
        email: str = Form(...),
        session: Session = Depends(get_db),
):
    if not os.path.exists(CURRENT_USER_FILE):
        return {"status": "error", "message": "Not logged in"}

    with open(CURRENT_USER_FILE, "r", encoding="utf-8") as f:
        cur = json.load(f)

    current_email = cur.get("email")
    user = session.query(db.User).filter(db.User.email == current_email).first()
    if not user:
        return {"status": "error", "message": "User not found"}

    if email != current_email:
        exists = session.query(db.User).filter(db.User.email == email).first()
        if exists:
            return {"status": "error", "message": "Email already in use"}

    user.username = username
    user.email = email
    session.commit()

    with open(CURRENT_USER_FILE, "w", encoding="utf-8") as f:
        json.dump({"email": email, "username": username}, f)

    return {"status": "success", "message": "Profile updated"}


@app.post("/profile/change-password")
async def profile_change_password(
        current_password: str = Form(...),
        new_password: str = Form(...),
        session: Session = Depends(get_db),
):
    # SEC-3: require current password before allowing a change
    if not os.path.exists(CURRENT_USER_FILE):
        return {"status": "error", "message": "Not logged in"}

    with open(CURRENT_USER_FILE, "r", encoding="utf-8") as f:
        cur = json.load(f)

    email = cur.get("email")
    user = session.query(db.User).filter(db.User.email == email).first()
    if not user:
        return {"status": "error", "message": "User not found"}

    from fastapi import HTTPException
    if not db.verify_password(current_password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    user.hashed_password = db.get_password_hash(new_password)
    session.commit()
    return {"status": "success", "message": "Password updated"}


# ═══════════════════════════════════════════════════════════════════════
# PREFERENCES API
# ═══════════════════════════════════════════════════════════════════════

@app.get("/preferences")
async def get_preferences():
    prefs = _read_preferences()
    resp = {"status": "success", "data": prefs}
    resp.update(prefs)
    return resp


@app.post("/preferences")
async def set_preferences(request: Request):
    payload = await request.json()
    prefs = _read_preferences()
    if "ask_before_send_email" in payload:
        prefs["ask_before_send_email"] = bool(payload["ask_before_send_email"])
    if "ask_before_deleting_emails" in payload:
        prefs["ask_before_deleting_emails"] = bool(payload["ask_before_deleting_emails"])
    if "context_length" in payload:
        try:
            prefs["context_length"] = int(payload["context_length"])
        except Exception:
            prefs["context_length"] = 5
        if prefs["context_length"] not in (2, 3, 5, 8, 10):
            prefs["context_length"] = 5
    if "whatsapp_delay" in payload:
        try:
            val = float(payload["whatsapp_delay"])
            prefs["whatsapp_delay"] = max(2.0, min(10.0, val))
        except Exception:
            pass
    if "tts_enabled" in payload:
        prefs["tts_enabled"] = bool(payload["tts_enabled"])
    # Persist any extra preference keys (theme, voice, rate, etc.)
    for k, v in payload.items():
        if k not in prefs:
            prefs[k] = v

    _write_preferences(prefs)
    resp = {"status": "success", "data": prefs}
    resp.update(prefs)
    return resp


class ConfirmSendPayload(BaseModel):
    to_addr: str | None = None
    to_name: str | None = None
    subject: str | None = None
    body_html: str | None = None
    type: str | None = None
    thread_id: str | None = None
    message_id_header: str | None = None
    message_id: str | None = None
    attachments: list[str] = []
    scheduled_send_at: float | None = None
    contact_name: str | None = None
    message: str | None = None
    file_path: str | None = None
    file_name: str | None = None
    pending: dict | None = None


@app.post("/agent/confirm-send")
async def agent_confirm_send(payload: ConfirmSendPayload):
    try:
        if payload.type == "whatsapp_send":
            from whatsapp_agent import WhatsAppAgent
            agent = WhatsAppAgent()
            res = await asyncio.to_thread(agent.execute_pending, {
                "type": "whatsapp_send",
                "contact_name": payload.contact_name,
                "message": payload.message,
                "file_path": payload.file_path,
                "file_name": payload.file_name,
            })
            if "❌" in res:
                return {"status": "error", "message": res}
            return {"status": "success", "message": res}

        session = _load_session()
        if not session:
            return {"status": "error", "message": "Google not connected."}

        if payload.type == "delete_event":
            from calendar_agent import CalendarAgent
            agent = CalendarAgent(session)
            res = await asyncio.to_thread(agent.execute_pending, payload.pending)
            if "❌" in res:
                return {"status": "error", "message": res}
            return {"status": "success", "message": res}
            
        if payload.type == "complete_task":
            from tasks_agent import TasksAgent
            agent = TasksAgent(session)
            res = await asyncio.to_thread(agent.execute_pending, payload.pending)
            if "❌" in res:
                return {"status": "error", "message": res}
            return {"status": "success", "message": res}

        if payload.type in ("delete_row", "clear_sheet_range"):
            from sheets_agent import SheetsAgent
            agent = SheetsAgent(session)
            res = await asyncio.to_thread(agent.execute_pending, payload.pending)
            if "❌" in res:
                return {"status": "error", "message": res}
            return {"status": "success", "message": res}

        if payload.type in ("archive_email", "delete_email"):
            from tools import modify_labels, trash_email
            try:
                if payload.type == "archive_email":
                    modify_labels(session, payload.message_id, remove_labels=["INBOX"])
                    return {"status": "success", "message": f"✅ Email archived: {payload.subject}"}
                else:
                    trash_email(session, payload.message_id)
                    return {"status": "success", "message": f"✅ Email deleted: {payload.subject}"}
            except Exception as e:
                return {"status": "error", "message": f"❌ Failed to {payload.type.split('_')[0]}: {e}"}

        from tools import send_email
        from email.utils import parseaddr

        user_email = session.get("user", {}).get("email")
        if not user_email:
            return {"status": "error", "message": "User email not found in session."}

        _, parsed_to = parseaddr(payload.to_addr or "")
        if "@" not in parsed_to:
            return {"status": "error", "message": "Invalid recipient email."}

        if payload.type == "schedule_send" and payload.scheduled_send_at:
            from state_store import add_scheduled_email
            send_id = add_scheduled_email(
                payload.to_name, parsed_to, payload.subject, payload.body_html,
                payload.attachments or [], float(payload.scheduled_send_at),
            )
            return {"status": "success", "message": f"Email scheduled. Queue #{send_id}"}

        if payload.type == "reply" and payload.thread_id:
            from tools import reply_email
            reply_email(
                session, user_email, parsed_to, payload.subject, payload.body_html,
                payload.thread_id, payload.message_id_header,
            )
            # MED-17: update command status to success after confirmed reply send
            try:
                from state_store import update_command
                command_id = getattr(payload, "command_id", None)
                if command_id:
                    update_command(command_id, {}, "success")
            except Exception:
                pass
            return {"status": "success", "message": f"Reply sent to {payload.to_name}"}

        send_email(session, user_email, parsed_to, payload.subject, payload.body_html, attachments=payload.attachments)
        # MED-17: update command status to success after confirmed email send
        try:
            from state_store import update_command
            command_id = getattr(payload, "command_id", None)
            if command_id:
                update_command(command_id, {}, "success")
        except Exception:
            pass
        return {"status": "success", "message": f"Email sent to {payload.to_name}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ═══════════════════════════════════════════════════════════════════════
# OAUTH: GOOGLE
# ═══════════════════════════════════════════════════════════════════════

@app.get("/auth/google")
async def auth_google():
    if not GOOGLE_CLIENT_ID:
        return JSONResponse({"status": "error", "message": "GOOGLE_CLIENT_ID missing"})
    params = urllib.parse.urlencode({
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": GOOGLE_SCOPES,
        "access_type": "offline",
        "prompt": "consent",
    })
    return RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?{params}")


@app.get("/auth/google/url")
async def auth_google_url():
    """
    Returns the Google OAuth URL as JSON instead of redirecting.
    Used by the Electron frontend to open the URL in the system browser (Chrome)
    so the user sees their saved Google accounts (account picker).
    """
    if not GOOGLE_CLIENT_ID:
        return JSONResponse({"status": "error", "message": "GOOGLE_CLIENT_ID missing"})
    params = urllib.parse.urlencode({
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": GOOGLE_SCOPES,
        "access_type": "offline",
        "prompt": "select_account consent",
    })
    url = f"https://accounts.google.com/o/oauth2/v2/auth?{params}"
    return JSONResponse({"status": "success", "url": url})


@app.get("/auth/callback")
async def auth_google_callback(code: str = None, error: str = None):
    if error or not code:
        return RedirectResponse("/homepage?google_error=true")

    r = await asyncio.to_thread(requests.post, 
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": GOOGLE_REDIRECT_URI,
        },
        timeout=15,
    )
    tokens = r.json()
    if "error" in tokens:
        return RedirectResponse("/homepage?google_error=true")

    r_user = await asyncio.to_thread(requests.get, 
        "https://www.googleapis.com/oauth2/v1/userinfo",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
        timeout=10,
    )
    user_info = r_user.json()

    tokens["expires_at"] = time.time() + int(tokens.get("expires_in", 3600))

    with _session_lock, open(SESSION_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "user": user_info,
            "tokens": tokens,
        }, f, indent=2)

    # Reset cached google agents so they reload with new session
    global _google_agents, _last_session_mtime
    _google_agents = {}
    _last_session_mtime = None
    _db_cache["last_update"] = 0

    html_content = """
    <html>
    <head><title>Connected</title></head>
    <body style="background:#0a0a0e; color:#d97706; display:flex; justify-content:center; align-items:center; height:100vh; font-family:sans-serif; margin:0;">
        <h2>Connected to Google</h2>
        <script>
            setTimeout(() => { window.close(); }, 1500);
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)


@app.get("/auth/status/google")
async def auth_status_google():
    if not os.path.exists(SESSION_FILE):
        return {"connected": False, "email": ""}
    try:
        with _session_lock, open(SESSION_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)

        email = d.get("user", {}).get("email", "")
        expires_at = d.get("tokens", {}).get("expires_at", 0)

        if not expires_at or time.time() > expires_at - 300:  # refresh if missing or within 5 min
            refresh = d.get("tokens", {}).get("refresh_token", "")
            if refresh:
                nr = await asyncio.to_thread(requests.post, 
                    "https://oauth2.googleapis.com/token",
                    data={
                        "client_id": d.get("client_id", GOOGLE_CLIENT_ID),
                        "client_secret": d.get("client_secret", GOOGLE_CLIENT_SECRET),
                        "refresh_token": refresh,
                        "grant_type": "refresh_token",
                    },
                    timeout=10,
                )
                if nr.status_code == 200:
                    nd = nr.json()
                    d["tokens"]["access_token"] = nd["access_token"]
                    d["tokens"]["expires_at"] = time.time() + int(nd.get("expires_in", 3600))
                    with _session_lock, open(SESSION_FILE, "w", encoding="utf-8") as wf:
                        json.dump(d, wf, indent=2)

        return {"connected": True, "email": email}
    except Exception:
        return {"connected": False, "email": ""}


@app.post("/auth/disconnect/google")
async def auth_disconnect_google():
    try:
        with _session_lock:
            if os.path.exists(SESSION_FILE):
                os.remove(SESSION_FILE)
        global _google_agents, _last_session_mtime
        _google_agents = {}
        _last_session_mtime = None
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ═══════════════════════════════════════════════════════════════════════
# OAUTH: SPOTIFY
# ═══════════════════════════════════════════════════════════════════════

@app.get("/auth/spotify")
async def auth_spotify():
    if not SPOTIFY_CLIENT_ID:
        return JSONResponse({"status": "error", "message": "SPOTIFY_CLIENT_ID missing"})
    params = urllib.parse.urlencode({
        "client_id": SPOTIFY_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": SPOTIFY_REDIRECT_URI,
        "scope": SPOTIFY_SCOPES,
    })
    return RedirectResponse(f"https://accounts.spotify.com/authorize?{params}")


@app.get("/auth/spotify/url")
async def auth_spotify_url():
    """Returns the Spotify OAuth URL as JSON for Electron to open in system browser."""
    if not SPOTIFY_CLIENT_ID:
        return JSONResponse({"status": "error", "message": "SPOTIFY_CLIENT_ID missing"})
    params = urllib.parse.urlencode({
        "client_id": SPOTIFY_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": SPOTIFY_REDIRECT_URI,
        "scope": SPOTIFY_SCOPES,
    })
    url = f"https://accounts.spotify.com/authorize?{params}"
    return JSONResponse({"status": "success", "url": url})


@app.get("/auth/spotify/callback")
async def auth_spotify_callback(code: str = None, error: str = None):
    if error or not code:
        return RedirectResponse("/homepage?spotify_error=true")

    creds = base64.b64encode(f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()).decode()
    r = await asyncio.to_thread(requests.post, 
        "https://accounts.spotify.com/api/token",
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": SPOTIFY_REDIRECT_URI,
        },
        timeout=15,
    )
    tokens = r.json()
    if "error" in tokens or "access_token" not in tokens:
        return RedirectResponse("/homepage?spotify_error=true")

    cache_data = {
        "access_token": tokens["access_token"],
        "token_type": tokens.get("token_type", "Bearer"),
        "expires_in": tokens.get("expires_in", 3600),
        "refresh_token": tokens.get("refresh_token", ""),
        "scope": tokens.get("scope", SPOTIFY_SCOPES),
        "expires_at": int(time.time()) + int(tokens.get("expires_in", 3600)),
    }
    with open(SPOTIFY_CACHE, "w", encoding="utf-8") as f:
        json.dump(cache_data, f)
    
    _db_cache["last_update"] = 0
    return RedirectResponse("/homepage?spotify_connected=true")


@app.get("/auth/status/spotify")
async def auth_status_spotify():
    if not os.path.exists(SPOTIFY_CACHE):
        return {"connected": False, "display_name": ""}
    try:
        with open(SPOTIFY_CACHE, "r", encoding="utf-8") as f:
            d = json.load(f)

        access_token = d.get("access_token", "")
        if time.time() > d.get("expires_at", 0) - 60:
            refresh = d.get("refresh_token", "")
            if refresh:
                creds = base64.b64encode(f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()).decode()
                nr = await asyncio.to_thread(requests.post, 
                    "https://accounts.spotify.com/api/token",
                    headers={
                        "Authorization": f"Basic {creds}",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    data={"grant_type": "refresh_token", "refresh_token": refresh},
                    timeout=10,
                )
                if nr.status_code == 200:
                    nd = nr.json()
                    d["access_token"] = nd["access_token"]
                    d["expires_at"] = int(time.time()) + int(nd.get("expires_in", 3600))
                    if "refresh_token" in nd:
                        d["refresh_token"] = nd["refresh_token"]
                    with open(SPOTIFY_CACHE, "w", encoding="utf-8") as wf:
                        json.dump(d, wf)
                    access_token = d["access_token"]
                else:
                    os.remove(SPOTIFY_CACHE)
                    return {"connected": False, "display_name": ""}

        me = await asyncio.to_thread(requests.get, 
            "https://api.spotify.com/v1/me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=8,
        )
        if me.status_code == 200:
            return {"connected": True, "display_name": me.json().get("display_name", "")}
        return {"connected": False, "display_name": ""}
    except Exception:
        return {"connected": False, "display_name": ""}


@app.post("/auth/disconnect/spotify")
async def auth_disconnect_spotify():
    try:
        if os.path.exists(SPOTIFY_CACHE):
            os.remove(SPOTIFY_CACHE)
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ═══════════════════════════════════════════════════════════════════════
# WHATSAPP
# ═══════════════════════════════════════════════════════════════════════

@app.get("/auth/status/whatsapp")
async def auth_status_whatsapp():
    """
    WhatsApp is considered connected (calibrated) when:
      - .ww_session file exists (user has logged in to WhatsApp Web), AND
      - .ww_calibration.json exists (search bar position has been saved)
    """
    session_file = os.path.join(BASE_DIR, ".ww_session")
    calibration_file = os.path.join(BASE_DIR, ".ww_calibration.json")
    connected = os.path.exists(session_file) and os.path.exists(calibration_file)
    return {"connected": connected}


@app.post("/auth/whatsapp/connect")
async def auth_connect_whatsapp():
    """
    Trigger the WhatsApp calibration wizard directly (no subprocess script).
    Runs calibrate_whatsapp() in a background thread so the HTTP response
    returns immediately while the GUI wizard opens.
    """
    import threading

    def _run_calibration():
        try:
            from whatsapp_agent import calibrate_whatsapp, WW_SESSION_FILE
            # Mark session as started so status flips to connected after calibration
            with open(WW_SESSION_FILE, "w") as f:
                f.write("connected")
            result = calibrate_whatsapp()
            print(f"[WhatsApp Calibration] {result}")
        except Exception as e:
            print(f"[WhatsApp Calibration] Error: {e}")

    threading.Thread(target=_run_calibration, daemon=True).start()
    return {"status": "success", "message": "WhatsApp calibration wizard started"}


@app.post("/auth/disconnect/whatsapp")
async def auth_disconnect_whatsapp():
    """Delete both marker files so status shows disconnected."""
    try:
        for fname in (".ww_session", ".ww_calibration.json"):
            fpath = os.path.join(BASE_DIR, fname)
            if os.path.exists(fpath):
                os.remove(fpath)
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ═══════════════════════════════════════════════════════════════════════
# SHORTCUTS
# ═══════════════════════════════════════════════════════════════════════

@app.get("/get-shortcuts")
async def get_shortcuts(email: str, session: Session = Depends(get_db)):
    user = session.query(db.User).filter(db.User.email == email).first()
    if not user:
        return {"status": "error", "message": "User not found"}
    return {
        "status": "success",
        "shortcuts": [{"id": s.id, "trigger": s.trigger, "action": s.action}
                      for s in user.shortcuts],
    }


@app.post("/save-shortcut")
async def save_shortcut(
        trigger: str = Form(...),
        action: str = Form(...),
        email: str = Form(...),
        session: Session = Depends(get_db),
):
    user = session.query(db.User).filter(db.User.email == email).first()
    if not user:
        return {"status": "error", "message": "User not found"}
    try:
        session.add(db.Shortcut(trigger=trigger, action=action, owner_email=email))
        session.commit()
        return {"status": "success", "message": "Shortcut saved"}
    except Exception as e:
        session.rollback()
        return {"status": "error", "message": str(e)}


@app.post("/delete-shortcut")
async def delete_shortcut(
        shortcut_id: int | None = Form(None),
        trigger: str | None = Form(None),
        email: str | None = Form(None),
        session: Session = Depends(get_db),
):
    try:
        query = session.query(db.Shortcut)
        if shortcut_id is not None:
            query = query.filter(db.Shortcut.id == shortcut_id)
        elif trigger:
            query = query.filter(db.Shortcut.trigger == trigger)
            if email:
                query = query.filter(db.Shortcut.owner_email == email)
        else:
            return {"status": "error", "message": "shortcut_id or trigger required"}

        row = query.first()
        if row:
            session.delete(row)
            session.commit()
        return {"status": "success"}
    except Exception as e:
        session.rollback()
        return {"status": "error", "message": str(e)}


# ═══════════════════════════════════════════════════════════════════════
# AGENT EXECUTE — Smart AI Voice Integration & RAG
# ═══════════════════════════════════════════════════════════════════════

async def generate_smart_voice_response(agent_name: str, action: str, query: str, raw_result: str) -> str:
    """
    Converts raw agent output into a short, natural spoken response.
    Fast-path covers ~95% of cases instantly; Ollama only for weather/general.
    """
    import asyncio
    import re as _re
    import random as _random

    agent_name = (agent_name or "").lower()
    action     = (action or "").lower()
    raw        = (raw_result or "").strip()
    raw_lower  = raw.lower()
    q_lower    = (query or "").lower()

    # ── ERRORS ────────────────────────────────────────────────────────────
    # MED-01: only trigger on genuine failure signals; ignore "error" in positive context
    if raw_lower.startswith("❌") or ("❌" in raw and "✅" not in raw) or ("failed" in raw_lower and "✅" not in raw):
        return "Hmm, something went wrong. Check the screen for details."

    # ── SYSTEM / APP CONTROL ──────────────────────────────────────────────
    if action in ("open_app", "open_url") or "open_app" in action:
        words = [w for w in q_lower.split() if w not in
                 ("open","launch","start","the","a","an","please","my","up")]
        hint = words[0].capitalize() if words else ""
        return _random.choice([
            f"Opening {hint} for you." if hint else "Opening that for you.",
            f"Sure, launching {hint}." if hint else "On it.",
            f"Got it." if not hint else f"Opening {hint}.",
        ])

    if action == "find_file" or "find_file" in action:
        return _random.choice([
            "Searching your computer for that file.",
            "I'm looking for that file now.",
            "Scanning your folders to find it."
        ])

    if action in ("native", "powershell", "press", "shutdown", "sleep", "restart"):
        combined = (q_lower + " " + action).lower()
        if "volume" in combined:
            m = _re.search(r"(\d+)", combined)
            return f"Volume set to {m.group(1)} percent." if m else "Volume adjusted."
        if "brightness" in combined:
            m = _re.search(r"(\d+)", combined)
            return f"Brightness set to {m.group(1)} percent." if m else "Brightness adjusted."
        if "lock" in combined:      return "Locking your PC."
        if "sleep" in combined:     return "Putting your computer to sleep."
        if "shutdown" in combined:  return "Shutting down. Goodbye!"
        if "restart" in combined:   return "Restarting your PC."
        if "screenshot" in combined:return "Screenshot taken."
        return "Done."

    # ── TIMER ─────────────────────────────────────────────────────────────
    if "timer" in agent_name:
        m = _re.search(r"(\d+)\s*(minute|second|hour)", raw_lower)
        if m: return f"Timer set for {m.group(1)} {m.group(2)}s. I'll let you know when it's up."
        if "alarm" in raw_lower: return "Alarm set."
        return "Timer started. I've got you."

    # ── SPOTIFY ───────────────────────────────────────────────────────────
    if "spotify" in agent_name:
        if "paused" in raw_lower:   return "Paused."
        if "resumed" in raw_lower:  return "Resuming."
        if "shuffle on" in raw_lower:  return "Shuffle on."
        if "shuffle off" in raw_lower: return "Shuffle off."
        m_track = _re.search(r'"([^"]+)"', raw)
        if "now playing" in raw_lower or "playing" in raw_lower:
            return f"Playing {m_track.group(1)}." if m_track else "Playing your music."
        if "skipped" in raw_lower or "next" in raw_lower:
            return f"Skipped. Now playing {m_track.group(1)}." if m_track else "Skipped."
        m_vol = _re.search(r"(\d+)%", raw)
        if "volume" in raw_lower:
            return f"Spotify volume at {m_vol.group(1)} percent." if m_vol else "Volume set."
        return "Done."

    # ── GMAIL ─────────────────────────────────────────────────────────────
    if "gmail" in agent_name:
        if "draft" in raw_lower:
            return "Draft saved. You can review it in Gmail."
        if "sent" in raw_lower:
            m = _re.search(r"to\s+([A-Za-z]+)", raw, _re.IGNORECASE)
            return f"Email sent to {m.group(1)}." if m else "Email sent."
        if "reply" in raw_lower:
            return "Reply sent."
        count = len(_re.findall(r"📧", raw))
        if count: return f"You've got {count} email{'s' if count != 1 else ''} in your inbox."
        return "Here are your emails."

    # ── CALENDAR ──────────────────────────────────────────────────────────
    if "calendar" in agent_name:
        if "✅" in raw or "created" in raw_lower:
            m = _re.search(r'"([^"]+)"', raw)
            title = m.group(1) if m else "event"
            return f"Done, added {title} to your calendar."
        if "deleted" in raw_lower or "cancelled" in raw_lower:
            return "Event cancelled."
        if "no upcoming" in raw_lower or "no events" in raw_lower:
            return "Your calendar's clear."
        count = len(_re.findall(r"📅", raw))
        if count: return f"You've got {count} event{'s' if count != 1 else ''} coming up."
        return "Here's your schedule."

    # ── TASKS ─────────────────────────────────────────────────────────────
    if "task" in agent_name:
        if "✅" in raw or "created" in raw_lower:
            m = _re.search(r'"([^"]+)"', raw)
            task = m.group(1) if m else "that"
            return f"Got it, added {task} to your to-do list."
        if "completed" in raw_lower: return "Marked as done."
        if "all caught up" in raw_lower: return "You're all caught up. Nothing pending."
        return "Here are your tasks."

    # ── CONTACTS ──────────────────────────────────────────────────────────
    if "contact" in agent_name:
        if "saved" in raw_lower or "✅" in raw:
            m = _re.search(r"saved[^\w]*([A-Za-z ]+)", raw, _re.IGNORECASE)
            name = m.group(1).strip() if m else ""
            return f"{name} added to contacts." if name else "Contact saved."
        if "deleted" in raw_lower: return "Contact removed."
        return "Here are your contacts."

    # ── WHATSAPP ──────────────────────────────────────────────────────────
    if "whatsapp" in agent_name:
        if "sent" in raw_lower or "✅" in raw:
            m = _re.search(r"to\s+([A-Za-z]+)", raw, _re.IGNORECASE)
            return f"WhatsApp sent to {m.group(1)}." if m else "WhatsApp message sent."
        return "Done."

    # ── FILES ─────────────────────────────────────────────────────────────
    if "files" in agent_name:
        if "opened" in raw_lower:
            m = _re.search(r'\*\*([^\*]+)\*\*', raw)
            fn = m.group(1) if m else "the file"
            return f"Found and opened {fn}."
        if "located" in raw_lower or "found" in raw_lower:
            m = _re.search(r'\*\*([^\*]+)\*\*', raw)
            fn = m.group(1) if m else "the file"
            return f"I found {fn}."
        return "File search complete."

    # ── DOCS / SHEETS ─────────────────────────────────────────────────────
    if "doc" in agent_name or "sheet" in agent_name:
        if "✅" in raw or "created" in raw_lower:
            m = _re.search(r'"([^"]+)"', raw)
            title = m.group(1) if m else "it"
            return f"Created {title} for you."
        if "written" in raw_lower: return "Done, content added."
        if "opened" in raw_lower:  return "Opening it in your browser."
        if "📊" in raw: return raw.replace("📊", "").strip()
        return "Done."

    # ── MAPS ──────────────────────────────────────────────────────────────
    if "map" in agent_name:
        m = _re.search(r"Navigating to[: ]+([^\n]+)", raw, _re.IGNORECASE)
        if m: return f"Opening directions to {m.group(1).strip()} in Maps."
        return "Opening Google Maps."

    # ── SYSTEM INFO ───────────────────────────────────────────────────────
    if "system" in agent_name:
        if "battery" in q_lower:
            m = _re.search(r"Battery[: ]+(\d+)%", raw, _re.IGNORECASE)
            if m:
                pct = int(m.group(1))
                charging = "charging" in raw_lower
                adj = "low" if pct < 20 else ("looking good" if pct > 60 else "okay")
                return f"Battery's at {pct} percent{', and charging' if charging else ''}. {adj.capitalize()}."
        if "cpu" in q_lower:
            m = _re.search(r"CPU[: ]+(\d+)%", raw, _re.IGNORECASE)
            if m: return f"CPU is at {m.group(1)} percent right now."
        if "ram" in q_lower or "memory" in q_lower:
            m = _re.search(r"RAM[: ]+(\d+)%", raw, _re.IGNORECASE)
            if m: return f"RAM is at {m.group(1)} percent."
        return "Here's your system info."

    # ── MEMORY AGENT ──────────────────────────────────────────────────────
    if "memory" in agent_name:
        if "✅" in raw: return "Got it, I'll remember that."
        return "Okay, noted."

    # ── GENERAL AGENT (Already conversational) ────────────────────────────
    if "general" in agent_name:
        return raw  # The general agent already outputs perfectly formatted conversational text

    # ── WEATHER / DATA SUMMARY — Ollama for rich natural summary ──────────
    if "weather" in agent_name or "search" in agent_name:
        prompt = (
            "You are WinVoice, an advanced AI assistant inspired by Jarvis. "
            "The user asked a question, and the system retrieved some raw data. "
            "Provide a highly concise, professional, and slightly British spoken response based on the data. "
            "Do NOT use AI filler phrases. Just give the answer in one or two short sentences. "
            "No markdown, no emojis. "
            f"User asked: {query!r}. "
            f"Raw Data: {raw[:350]!r}"
        )
        try:
            from ai_backend import call_ollama_raw_stream
            return call_ollama_raw_stream(prompt)
        except Exception as e:
            print(f"[TTS] Ollama stream error: {e}")
        first = raw.split("\n")[0].strip()
        return first[:120] if first else "Here is the information."

    # ── CATCH-ALL ─────────────────────────────────────────────────────────
    if "✅" in raw: return "Done."
    # Instead of reading JSON off the screen, just give a generic acknowledgement
    if "{" in raw or "[" in raw:
        return "I've pulled that up for you on the screen."
    first_line = raw.split("\n")[0].strip()[:100]
    return first_line if first_line else "Task complete."


@app.post("/agent/execute")
async def agent_execute(request: Request, command: str = Form(...), session: Session = Depends(get_db)):
    wants_stream = "text/event-stream" in (request.headers.get("accept") or "")

    async def events():
        async for ev in _agent_execute_events(command, session):
            yield ev

    if wants_stream:
        return StreamingResponse(events(), media_type="text/event-stream")

    final = {"status": "success", "message": "Command executed", "logs": []}
    async for ev in _agent_execute_events(command, session, raw=True):
        if ev.get("event") == "result":
            final["logs"].append(ev.get("message", ""))
        elif ev.get("event") == "error":
            return {"status": "error", "message": ev.get("message", "Error"), "logs": final["logs"]}
        elif ev.get("event") == "confirmation":
            return {"status": "needs_confirmation", "message": "Email requires confirmation",
                    "pending": ev.get("pending"), "logs": final["logs"]}
    return final


def _sse(payload: dict) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


async def _emit(payload: dict, raw: bool):
    return payload if raw else _sse(payload)


def _macro_name(text: str, prefixes: list[str]) -> str:
    q = text.strip().lower()
    for p in prefixes:
        if q.startswith(p):
            return q[len(p):].strip().strip('"\'')
    return ""


def _undo_steps(last: dict | None) -> list[dict]:
    if not last:
        return []
    steps = (last.get("plan") or {}).get("steps", [])
    if any(s.get("action") in ("OPEN_APP", "OPEN_URL") for s in steps):
        return [{"action": "PRESS", "keys": ["alt", "f4"]}]
    return []


async def _agent_execute_events(command: str, session: Session, raw: bool = False):
    global _macro_recording
    try:
        from router import route_intent
        from executor import execute_step
        from state_store import add_command, update_command, get_recent_commands, get_last_command, save_macro, \
            get_macro, list_macros

        original_query = command.strip().rstrip(".?!")
        if not original_query:
            yield await _emit({"event": "error", "message": "Empty command"}, raw)
            return

        yield await _emit({"event": "queued", "message": "Queued"}, raw)

        # Acquire the lock with a 15s timeout — prevents hanging if a previous command crashed
        # CRASH-1: use lock_acquired flag so the finally block never releases a lock we don't own
        lock_acquired = False
        try:
            await asyncio.wait_for(_command_lock.acquire(), timeout=15.0)
            lock_acquired = True
        except asyncio.TimeoutError:
            yield await _emit({"event": "error",
                "message": "WinVoice is busy. Please wait a moment and try again."}, raw)
            return

        try:
            yield await _emit({"event": "progress", "message": "Starting"}, raw)
            lower = original_query.lower()

            if lower in ("repeat that", "repeat last command", "do that again"):
                last = get_last_command(exclude=original_query)
                if not last:
                    yield await _emit({"event": "error", "message": "No previous command to repeat."}, raw)
                    return
                original_query = last["command"]
                lower = original_query.lower()
                yield await _emit({"event": "progress", "message": f"Repeating: {original_query}"}, raw)

            if lower in ("undo that", "undo last command", "undo the last command"):
                last = get_last_command(exclude=command)
                steps = _undo_steps(last)
                if not steps:
                    yield await _emit(
                        {"event": "result", "message": "I do not have a safe undo action for the last command."}, raw)
                    return
                plan = {"steps": steps}
                command_id = add_command(command, plan, "running")
                for step in steps:
                    await asyncio.to_thread(execute_step, step)
                    yield await _emit({"event": "result", "message": "Undo step completed."}, raw)
                update_command(command_id, plan, "success")
                return

            name = _macro_name(lower, ["record a macro called ", "record macro called ", "start recording macro "])
            if name:
                _macro_recording = {"name": name, "steps": []}
                yield await _emit(
                    {"event": "result", "message": f"Recording macro '{name}'. Say 'stop recording macro' when done."},
                    raw)
                return

            if lower in ("stop recording macro", "stop macro recording", "save macro"):
                if not _macro_recording.get("name"):
                    yield await _emit({"event": "error", "message": "No macro is currently recording."}, raw)
                    return
                save_macro(_macro_recording["name"], _macro_recording["steps"])
                count = len(_macro_recording["steps"])
                macro_name = _macro_recording["name"]
                _macro_recording = {"name": None, "steps": []}
                yield await _emit({"event": "result", "message": f"Saved macro '{macro_name}' with {count} step(s)."},
                                  raw)
                return

            if lower in ("list macros", "show macros", "show my macros"):
                macros = list_macros()
                msg = "No macros saved yet." if not macros else "Saved macros:\n" + "\n".join(
                    f"- {m['name']}" for m in macros)
                yield await _emit({"event": "result", "message": msg}, raw)
                return

            macro_to_run = _macro_name(lower, ["run the macro ", "run macro ", "execute macro ", "run the ", "run "])
            if macro_to_run.endswith(" macro"):
                macro_to_run = macro_to_run[:-6].strip()
            
            # If the user explicitly asked to run a macro but we couldn't find it
            is_explicit_macro = lower.startswith("run macro") or lower.startswith("run the macro") or lower.startswith("execute macro")
            macro = get_macro(macro_to_run) if macro_to_run else None
            
            if macro:
                plan = {"steps": macro["steps"]}
                command_id = add_command(original_query, plan, "running")
                steps = _sanitize(plan["steps"])
                yield await _emit(
                    {"event": "progress", "message": f"Running macro '{macro['name']}' ({len(steps)} steps)"}, raw)
            elif is_explicit_macro:
                yield await _emit({"event": "error", "message": f"I couldn't find a saved macro named '{macro_to_run}'."}, raw)
                return
            else:
                query = original_query
                if os.path.exists(CURRENT_USER_FILE):
                    try:
                        with open(CURRENT_USER_FILE, "r", encoding="utf-8") as f:
                            cur = json.load(f)
                        user_email = cur.get("email")
                        if user_email:
                            match = session.query(db.Shortcut).filter(db.Shortcut.owner_email == user_email,
                                                                      db.Shortcut.trigger.ilike(query)).first()
                            if match:
                                query = match.action
                                yield await _emit({"event": "progress", "message": f"Shortcut expanded: {query}"}, raw)
                    except Exception as e:
                        print(f"[Shortcut Check Error] {e}")

                prefs = _read_preferences()
                context_len = int(prefs.get("context_length", 5))
                context = [x["command"] for x in reversed(get_recent_commands(context_len))]

                yield await _emit({"event": "progress", "message": "Routing command"}, raw)

                # Run memory recall and routing IN PARALLEL — saves ~500ms
                # Memory recall is fire-and-forget; routing starts immediately
                # Memory is only used by the general agent, NOT injected into router context
                # (injecting memories into router was causing garbled responses)
                memory_task = asyncio.create_task(
                    asyncio.to_thread(memory_store.recall, query.lower())
                )
                
                plan = await asyncio.to_thread(route_intent, query.lower(), context)
                # Now get memories (likely already done while routing ran)
                try:
                    memories = await asyncio.wait_for(memory_task, timeout=1.0)
                except Exception:
                    memories = []
                if not plan:
                    yield await _emit({"event": "error", "message": "I didn't understand that command."}, raw)
                    return
                if "steps" not in plan:
                    # Robust recovery for small local LLMs or single-action router outputs
                    if "action" in plan and isinstance(plan["action"], str):
                        plan = {"steps": [dict(plan)]}
                    elif "action" in plan and isinstance(plan["action"], list):
                        recovered_steps = []
                        actions_list = plan["action"]
                        contexts_list = plan.get("context_from_step", [])
                        
                        for i, act in enumerate(actions_list):
                            # Handle case where model outputs 'type' instead of 'action'
                            if "type" in act and "action" not in act:
                                act["action"] = act.pop("type")
                                
                            # Re-attach context if model put it in a parallel array
                            if isinstance(contexts_list, list):
                                for ctx in contexts_list:
                                    if isinstance(ctx, dict) and ctx.get("step") == (i + 1):
                                        # Handle either it putting it as 'context_from_step' inside the dict, or we just use 'step'
                                        # Or if it's a completely messy schema, try to map it safely
                                        pass
                                    elif isinstance(ctx, dict) and ctx.get("task_type") == act.get("action"):
                                         act["context_from_step"] = ctx.get("context_from_step", ctx.get("step", []))

                            recovered_steps.append(act)
                        plan["steps"] = recovered_steps
                    else:
                        yield await _emit({"event": "error", "message": "I didn't understand that command."}, raw)
                        return

                steps = _sanitize(plan["steps"])
                plan = {"steps": steps}
                command_id = add_command(original_query, plan, "running")

                # --- AUTO-MEMORY TRIGGER ---
                try:
                    from memory_agent import trigger_auto_memory
                    trigger_auto_memory(original_query)
                except Exception as e:
                    print(f"Auto-memory trigger failed: {e}")

                if _macro_recording.get("name"):
                    _macro_recording["steps"].extend(steps)
                    yield await _emit({"event": "progress",
                                       "message": f"Added {len(steps)} step(s) to macro '{_macro_recording['name']}'"},
                                      raw)

            agents = _get_agents()
            pending_confirmation_payload = None  # Bug 5 fix: collect confirmations without halting
            step_results = {}  # Cross-agent pipeline: store results keyed by step index
            prefs = _read_preferences()
            tts_enabled = prefs.get("tts_enabled", True)
            
            # Find all steps whose output is consumed by a later step
            consumed_steps = set()
            for s in steps:
                ctx = s.get("context_from_step")
                if ctx is None:
                    continue
                if isinstance(ctx, int):
                    consumed_steps.add(ctx)
                elif isinstance(ctx, list):
                    for c in ctx:
                        if isinstance(c, int):
                            consumed_steps.add(c)


            for i, step in enumerate(steps, start=1):
                action = step.get("action", "")
                yield await _emit({"event": "step", "index": i, "total": len(steps), "action": action,
                                   "message": f"[{i}/{len(steps)}] {action}"}, raw)

                # Fetch query representing user input for TTS context
                q = step.get("query", original_query)

                if action in AGENT_MAP:
                    agent_key = AGENT_MAP[action]
                    if action in GOOGLE_REQUIRED and not _load_session():
                        update_command(command_id, plan, "error")
                        yield await _emit({"event": "error",
                                           "message": "Google not connected. Go to Connections and connect Google."},
                                          raw)
                        return
                    agent = agents.get(agent_key)
                    if not agent:
                        yield await _emit({"event": "result", "message": f"{agent_key} agent unavailable"}, raw)
                        continue

                    # Inject memories into agents that frequently require personal context to resolve names/preferences
                    # We inject just the raw facts to avoid confusing the local LLMs
                    if agent_key in ["general", "gmail", "whatsapp", "calendar", "tasks", "docs"] and memories:
                        mem_facts = "; ".join(memories[:3])  # max 3 memories, most relevant first
                        q = f"[User Context (Use this to resolve names/preferences if needed): {mem_facts}]\n{q}"

                    # ── Cross-agent pipeline: inject context from a previous step ──
                    ctx_step = step.get("context_from_step")
                    if ctx_step is not None:
                        ctx_list = ctx_step if isinstance(ctx_step, list) else [ctx_step]
                        ctx_data_combined = []
                        valid_sources = []
                        for c in ctx_list:
                            if isinstance(c, int) and c in step_results:
                                data = step_results[c]
                                if data and len(str(data)) > 5:
                                    ctx_data_combined.append(f"--- DATA FROM STEP {c} ---\n{data}")
                                    valid_sources.append(str(c))
                        
                        if ctx_data_combined:
                            q = (
                                f"[CONTEXT DATA FROM PREVIOUS STEPS — use this data to complete the task below]\n"
                                f"{chr(10).join(ctx_data_combined)}\n\n"
                                f"[TASK]\n{q}"
                            )
                            sources_str = ", ".join(valid_sources)
                            yield await _emit(
                                {"event": "progress", "message": f"Piping data from step(s) {sources_str} → step {i}"}, raw)

                    loop = asyncio.get_running_loop()
                    progress_queue = asyncio.Queue()

                    def status_callback(msg):
                        loop.call_soon_threadsafe(progress_queue.put_nowait, {"event": "progress", "message": msg})

                    # Run agent in background thread, while polling the progress queue
                    agent_task = asyncio.create_task(asyncio.to_thread(agent.run, q, status_callback))
                    
                    while not agent_task.done():
                        try:
                            ev = await asyncio.wait_for(progress_queue.get(), timeout=0.1)
                            yield await _emit(ev, raw)
                        except asyncio.TimeoutError:
                            continue

                    res = agent_task.result()
                    
                    # Yield any remaining progress events
                    while not progress_queue.empty():
                        ev = progress_queue.get_nowait()
                        yield await _emit(ev, raw)

                    if isinstance(res, dict) and res.get("pending_confirmation"):
                        ptype = res.get("type")
                        res["tts"] = tts_enabled  # Pass TTS state to frontend
                        if ptype in ("send", "schedule_send") and prefs.get("ask_before_send_email", True):
                            pending_confirmation_payload = res
                            update_command(command_id, plan, "needs_confirmation")
                            if tts_enabled:
                                import tts
                                tts.speak("Email drafted. Please review it on the screen before sending.")
                            yield await _emit({"event": "result", "message": "📧 Email queued — awaiting your confirmation."}, raw)
                            step_results[i] = "Email pending confirmation"
                            continue
                        elif ptype == "whatsapp_send":
                            pending_confirmation_payload = res
                            update_command(command_id, plan, "needs_confirmation")
                            if tts_enabled:
                                import tts
                                tts.speak("WhatsApp drafted. Please confirm on screen.")
                            yield await _emit({"event": "result", "message": "💬 WhatsApp queued — awaiting your confirmation."}, raw)
                            step_results[i] = "WhatsApp pending confirmation"
                            continue
                        elif ptype in ("archive_email", "delete_email") and prefs.get("ask_before_deleting_emails", True):
                            pending_confirmation_payload = res
                            update_command(command_id, plan, "needs_confirmation")
                            if tts_enabled:
                                import tts
                                act_str = "archiving" if ptype == "archive_email" else "deleting"
                                tts.speak(f"Please review the email on screen before {act_str}.")
                            yield await _emit({"event": "result", "message": f"⏳ Pending {ptype.replace('_email', '')} confirmation."}, raw)
                            step_results[i] = f"{ptype} pending confirmation"
                            continue
                        elif ptype in ("delete_event", "complete_task", "delete_row", "clear_sheet_range"):
                            pending_confirmation_payload = res
                            update_command(command_id, plan, "needs_confirmation")
                            if tts_enabled:
                                import tts
                                act_str = ptype.replace("_", " ")
                                tts.speak(f"Please review the action on screen before proceeding.")
                            yield await _emit({"event": "result", "message": f"⏳ Pending confirmation for {ptype.replace('_', ' ')}."}, raw)
                            step_results[i] = f"{ptype} pending confirmation"
                            continue
                            
                        # If confirmation is disabled, execute immediately
                        if ptype in ("send", "schedule_send"):
                            agent_obj = agents.get("gmail")
                            res = await asyncio.to_thread(agent_obj.execute_pending, res) if agent_obj else "Gmail agent unavailable"
                        elif ptype in ("archive_email", "delete_email"):
                            from tools import modify_labels, trash_email
                            session = _load_session()
                            try:
                                if ptype == "archive_email":
                                    modify_labels(session, res["message_id"], remove_labels=["INBOX"])
                                    res = f"✅ Email archived: {res['subject']}"
                                else:
                                    trash_email(session, res["message_id"])
                                    res = f"✅ Email deleted: {res['subject']}"
                            except Exception as _e:
                                res = f"❌ Failed: {_e}"

                    # Store result for potential use by later steps
                    step_results[i] = str(res) if res else ""

                    # If this step was a file search for a downstream sending task and failed, abort pipeline cleanly
                    if action == "FILES_TASK" and i in consumed_steps and ("❌" in str(res) or "Could not find" in str(res)):
                        update_command(command_id, plan, "error")
                        yield await _emit({"event": "error", "message": str(res)}, raw)
                        return

                    # Skip UI output and TTS if this step's output is strictly meant for a subsequent step
                    if i in consumed_steps:
                        continue

                    # 1. INSTANT UI UPDATE: Send raw data to the screen immediately
                    yield await _emit({"event": "result", "message": str(res)}, raw)

                    # 2. SMART TTS VOICE: Generate and speak the humanized response
                    if tts_enabled:
                        voice_out = await generate_smart_voice_response(agent_key, step.get('action_name', ''), q, str(res))
                        import tts
                        if hasattr(voice_out, '__iter__') and not isinstance(voice_out, str):
                            tts.speak_stream(voice_out)
                        else:
                            tts.speak(voice_out)

                else:
                    if step.get("action") == "INPUT_REPLAY":
                        # Real input macro replay
                        try:
                            from macro_recorder import replay
                            events = step.get("events", [])
                            result = await asyncio.to_thread(replay, events)
                            step_results[i] = str(result)
                            yield await _emit({"event": "result", "message": result}, raw)
                        except Exception as rpe:
                            step_results[i] = f"Replay error: {rpe}"
                            yield await _emit({"event": "result", "message": f"Replay error: {rpe}"}, raw)
                    else:
                        await asyncio.to_thread(execute_step, step)
                        step_results[i] = "Done"
                        yield await _emit({"event": "result", "message": "Done"}, raw)

                        if tts_enabled:
                            voice_out = await generate_smart_voice_response("system", step.get("action", ""), q, "Done")
                            import tts
                            if hasattr(voice_out, '__iter__') and not isinstance(voice_out, str):
                                tts.speak_stream(voice_out)
                            else:
                                tts.speak(voice_out)

            # Bug 5 fix: after all steps are done, emit the confirmation if one is pending
            if pending_confirmation_payload:
                yield await _emit({"event": "confirmation", "message": "Email requires confirmation",
                                   "pending": pending_confirmation_payload}, raw)
            else:
                update_command(command_id, plan, "success")
                yield await _emit({"event": "done", "message": "Command executed"}, raw)

        except Exception as _inner_e:
            traceback.print_exc()
            yield await _emit({"event": "error", "message": str(_inner_e)}, raw)
        finally:
            # CRASH-1: only release if WE acquired the lock (lock_acquired flag)
            if lock_acquired:
                _command_lock.release()

    except Exception as e:
        traceback.print_exc()
        prefs = _read_preferences()
        if prefs.get("tts_enabled", True):
            import tts
            tts.speak("Sorry, an unexpected error occurred during execution.")
        yield await _emit({"event": "error", "message": str(e)}, raw)


@app.get("/macros")
async def macros_list():
    from state_store import list_macros
    return {"status": "success", "macros": list_macros()}


@app.get("/command-history")
async def command_history():
    from state_store import get_recent_commands
    return {"status": "success", "commands": get_recent_commands(20)}


@app.get("/timers")
async def get_active_timers():
    """Returns all active timers with remaining seconds for the dashboard widget."""
    try:
        from timer_agent import _timers, _timer_lock
        import time as _time
        from datetime import datetime as _dt
        result = []
        with _timer_lock:
            for entry in list(_timers):
                try:
                    elapsed = (_dt.now() - entry["started_at"]).total_seconds()
                    remaining = max(0, entry["duration"] - int(elapsed))
                    result.append({
                        "id": entry.get("id", 0),
                        "label": entry.get("label", "Timer"),
                        "type": entry.get("type", "timer"),
                        "duration": entry.get("duration", 0),
                        "duration_text": entry.get("duration_text", ""),
                        "remaining": remaining,
                    })
                except Exception:
                    pass
        return {"status": "success", "timers": result}
    except Exception as e:
        return {"status": "success", "timers": []}


# ═══════════════════════════════════════════════════════════════════════
# AUDIO LEVEL — for live waveform animation in frontend
# ═══════════════════════════════════════════════════════════════════════

@app.get("/audio/level")
async def audio_level():
    """Returns current microphone input level 0.0–1.0 for waveform animation."""
    try:
        import voice
        return {"level": voice.get_audio_level(), "listening": voice.is_listening()}
    except Exception:
        return {"level": 0.0, "listening": False}


# ═══════════════════════════════════════════════════════════════════════
# SCHEDULED EMAILS — list and delete
# ═══════════════════════════════════════════════════════════════════════

@app.get("/scheduled-emails")
async def get_scheduled_emails():
    try:
        from state_store import list_scheduled_emails
        emails = list_scheduled_emails(50)
        # Format send_at as human-readable
        import datetime as _dt
        for em in emails:
            try:
                em["send_at_str"] = _dt.datetime.fromtimestamp(em["send_at"]).strftime("%b %d, %I:%M %p")
            except Exception:
                em["send_at_str"] = "Unknown"
        return {"status": "success", "emails": emails}
    except Exception as e:
        return {"status": "error", "message": str(e), "emails": []}


@app.post("/scheduled-emails/delete")
async def delete_scheduled_email(request: Request):
    try:
        body = await request.json()
        email_id = int(body.get("id", 0))
        from state_store import _connect, _LOCK
        with _LOCK, _connect() as conn:
            conn.execute("DELETE FROM scheduled_emails WHERE id=? AND status='pending'", (email_id,))
            conn.commit()
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ═══════════════════════════════════════════════════════════════════════
# MACRO RECORDING — real input capture via pynput
# ═══════════════════════════════════════════════════════════════════════

_input_macro_recording_name: str | None = None
_macro_ui_instance = None  # holds the MacroRecorderUI so stop can close it
_macro_stop_lock = threading.Lock()  # prevents double-stop races
_macro_already_saved: bool = False  # prevents sequential double-save (Ctrl+Q then Stop button)


@app.post("/macros/start-recording")
async def macro_start_recording(request: Request):
    global _input_macro_recording_name, _macro_ui_instance
    try:
        body = await request.json()
        name = (body.get("name") or "").strip()
        if not name:
            return {"status": "error", "message": "Macro name is required"}

        from macro_recorder import start_recording, register_stop_hotkey_callback
        msg = start_recording()
        _input_macro_recording_name = name

        # Ctrl+Q inside pynput → call stop endpoint (same as clicking Stop)
        def _ctrl_q_stop():
            try:
                import requests as _req
                _req.post(f"http://{APP_HOST}:{APP_PORT}/macros/stop-recording", timeout=5)
            except Exception:
                pass

        register_stop_hotkey_callback(_ctrl_q_stop)

        # Launch UI — on_stop just calls the stop endpoint (idempotent)
        def _launch_ui():
            global _macro_ui_instance
            try:
                from macro_ui import MacroRecorderUI
                def _on_ui_stop():
                    try:
                        import requests as _req
                        _req.post(f"http://{APP_HOST}:{APP_PORT}/macros/stop-recording", timeout=5)
                    except Exception:
                        pass

                ui = MacroRecorderUI(on_stop_callback=_on_ui_stop)
                _macro_ui_instance = ui
                ui.show()
                if ui.window:
                    ui.window.mainloop()
            except Exception as e:
                print(f"[Macro UI] Failed: {e}")
            finally:
                _macro_ui_instance = None

        threading.Thread(target=_launch_ui, daemon=True).start()
        return {"status": "success", "message": msg}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/macros/stop-recording")
async def macro_stop_recording():
    global _input_macro_recording_name, _macro_ui_instance, _macro_already_saved

    # Close UI first regardless (idempotent)
    ui = _macro_ui_instance
    if ui:
        try:
            ui.close_from_outside()
        except Exception:
            pass
        _macro_ui_instance = None

    # If already saved by a previous call (e.g. Ctrl+Q saved, then Stop button fires)
    if _macro_already_saved:
        _macro_already_saved = False
        return {"status": "success", "message": "Already saved."}

    # Prevent truly simultaneous calls
    if not _macro_stop_lock.acquire(blocking=False):
        return {"status": "success", "message": "Already stopping."}

    try:
        from macro_recorder import stop_recording, get_summary, is_recording
        from state_store import save_macro

        events, msg = stop_recording()
        name = _input_macro_recording_name or "untitled"
        _input_macro_recording_name = None

        if not events:
            return {"status": "error", "message": "No events recorded."}

        steps = [{"action": "INPUT_REPLAY", "events": events}]
        save_macro(name, steps)
        _macro_already_saved = True  # mark saved — next call will skip
        summary = get_summary(events)
        return {"status": "success", "message": f"✅ Macro '{name}' saved — {summary}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        try:
            _macro_stop_lock.release()
        except Exception:
            pass

        # MED-15: cancel any previous reset task before creating a new one (task leak fix)
        async def _reset_saved():
            import asyncio
            await asyncio.sleep(3)
            global _macro_already_saved
            _macro_already_saved = False

        global _reset_saved_task
        if _reset_saved_task and not _reset_saved_task.done():
            _reset_saved_task.cancel()
        _reset_saved_task = asyncio.create_task(_reset_saved())


@app.get("/macros/recording-status")
async def macro_recording_status():
    try:
        from macro_recorder import is_recording
        return {"recording": is_recording(), "name": _input_macro_recording_name}
    except Exception:
        return {"recording": False, "name": None}


@app.delete("/macros/{name}")
async def macro_delete(name: str):
    try:
        from state_store import delete_macro
        deleted = delete_macro(name)
        return {"status": "success" if deleted else "error",
                "message": "Deleted" if deleted else "Macro not found"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ═══════════════════════════════════════════════════════════════════════
# SPOTIFY WIDGET CONTROL
# ═══════════════════════════════════════════════════════════════════════

@app.post("/spotify/control")
async def spotify_control(action: str = Form(...)):
    try:
        from spotify_agent import SpotifyAgent, _get_spotify
        sp, err = _get_spotify()  # Get the authenticated client directly
        if err: return {"status": "error", "message": err}

        agent = SpotifyAgent()
        # Direct API calls - NO OLLAMA HERE
        if action == "prev":
            out = agent._prev(sp)
        elif action == "next":
            out = agent._next(sp)
        elif action == "toggle":
            out = agent._toggle(sp)
        else:
            return {"status": "error", "message": "Unknown action"}

        return {"status": "success", "message": out}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ═══════════════════════════════════════════════════════════════════════
# SPEECH — voice input/output
# ═══════════════════════════════════════════════════════════════════════

@app.post("/speech-to-text")
async def speech_to_text():
    """
    Runs voice.listen() in a thread executor so the FastAPI event loop
    stays free — this is the fix for /speech/stop being queued and firing
    immediately after /speech-to-text starts.
    """
    import voice
    try:
        loop = asyncio.get_event_loop()
        text = await loop.run_in_executor(None, voice.listen)
        if text and text.strip():
            return {"status": "success", "text": text.strip()}
        return {"status": "error", "message": "No speech detected. Please try again."}
    except Exception as e:
        return {"status": "error", "message": f"Speech error: {e}"}


@app.post("/speech/stop")
async def speech_stop():
    try:
        import voice
        voice.force_stop_listening()
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/speech/status")
async def speech_status():
    """Let the frontend poll whether a listen session is active."""
    try:
        import voice
        return {"listening": voice.is_listening()}
    except Exception:
        return {"listening": False}


@app.get("/dashboard/snapshot")
async def dashboard_snapshot():
    data = {
        "weather": "Unavailable",
        "spotify": "Not connected",
        "system": "Unavailable",
    }

    # System
    try:
        import psutil
        parts = []
        bat = psutil.sensors_battery()
        if bat:
            status = "🔌 Charging" if bat.power_plugged else "🔋 On Battery"
            parts.append(f"🔋 Battery: {round(bat.percent)}% — {status}")
        cpu_pct = psutil.cpu_percent(interval=0.5)
        parts.append(f"⚙️ CPU: {cpu_pct}%")
        vm = psutil.virtual_memory()
        parts.append(f"💾 RAM: {vm.percent}% ({vm.used / 1024 ** 3:.1f}/{vm.total / 1024 ** 3:.1f} GB)")
        data["system"] = "\n".join(parts)
    except Exception as e:
        data["system"] = f"System unavailable: {e}"

    # Weather
    try:
        import requests as req
        geo = req.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": "Hyderabad", "count": 1, "language": "en", "format": "json"},
            timeout=5,
        ).json().get("results", [])
        if geo:
            lat, lon = geo[0]["latitude"], geo[0]["longitude"]
            w = req.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat, "longitude": lon,
                    "current": "temperature_2m,apparent_temperature,weather_code,relative_humidity_2m,wind_speed_10m",
                    "timezone": "auto",
                },
                timeout=5,
            ).json().get("current", {})
            temp = round(w.get("temperature_2m", 0))
            feels = round(w.get("apparent_temperature", 0))
            humidity = w.get("relative_humidity_2m", 0)
            wind = round(w.get("wind_speed_10m", 0))
            data["weather"] = f"🌡️ {temp}°C (feels {feels}°C)\n💧 Humidity: {humidity}%\n💨 Wind: {wind} km/h"
    except Exception as e:
        data["weather"] = f"Weather unavailable: {e}"

    # Spotify
    try:
        from spotify_agent import _get_spotify
        sp, err = _get_spotify()
        if err:
            data["spotify"] = err
        else:
            curr = sp.current_playback()
            if curr and curr.get("item"):
                item = curr["item"]
                name = item["name"]
                artists = ", ".join(a["name"] for a in item["artists"])
                state = "▶️" if curr.get("is_playing") else "⏸️"
                data["spotify"] = f"{state} {name}\nby {artists}"
            else:
                data["spotify"] = "🎵 Nothing playing"
    except Exception as e:
        data["spotify"] = f"Spotify unavailable: {e}"

    return {"status": "success", "data": data}


@app.get("/dashboard/rich")
async def dashboard_rich():
    import psutil
    import os
    import json

    current_time = time.time()

    # 1. ALWAYS REFRESH "FAST" DATA (Local System Stats)
    # This part runs every 3 seconds and is near-instant
    try:
        bat = psutil.sensors_battery()
        cpu = psutil.cpu_percent(interval=None)  # interval=None is faster than 0.4
        vm = psutil.virtual_memory()
        disk = psutil.disk_usage(_disk_path)

        system_data = {
            "cpu": cpu,
            "ram_pct": vm.percent,
            "ram_used_gb": round(vm.used / 1024 ** 3, 1),
            "ram_total_gb": round(vm.total / 1024 ** 3, 1),
            "disk_pct": round(disk.percent),
            "disk_used_gb": round(disk.used / 1024 ** 3, 1),
            "disk_total_gb": round(disk.total / 1024 ** 3, 1),
            "battery_pct": round(bat.percent) if bat else None,
            "charging": bat.power_plugged if bat else None
        }
    except Exception as e:
        system_data = {"error": str(e)}

    # 2. ONLY REFRESH "SLOW" DATA EVERY 60 SECONDS (Google/Weather/GPU)
    # If we refreshed this every 3 seconds, the app would stay laggy
    # PERF-3: guard the slow-data update block with an async lock to prevent race conditions
    if current_time - _db_cache["last_update"] > 60:
        async with _rich_update_lock:
            if current_time - _db_cache["last_update"] > 60:
                slow_update = {
                    "emails": list(_db_cache["slow_data"].get("emails", [])),
                    "events": list(_db_cache["slow_data"].get("events", [])),
                    "weather": _db_cache["slow_data"].get("weather", {})
                }

                # --- Full Weather Logic (wttr.in) ---
                try:
                    import requests as req
                    w = req.get("https://wttr.in/Hyderabad?format=j1", timeout=5).json()
                    cur = w.get("current_condition", [{}])[0]
                    daily = w.get("weather", [{}])[0]

                    def wttr_icon(desc):
                        d = desc.lower()
                        if "sun" in d or "clear" in d: return "☀️"
                        if "cloud" in d or "overcast" in d: return "☁️"
                        if "fog" in d or "mist" in d or "haze" in d: return "🌫️"
                        if "rain" in d or "drizzle" in d: return "🌧️"
                        if "snow" in d or "ice" in d: return "❄️"
                        if "thunder" in d or "storm" in d: return "⛈️"
                        if "shower" in d: return "🌦️"
                        return "🌡️"

                    desc_val = cur.get("weatherDesc", [{"value": ""}])[0]["value"]
                    slow_update["weather"] = {
                        "temp": int(cur.get("temp_C", 0)),
                        "feels": int(cur.get("FeelsLikeC", 0)),
                        "humidity": int(cur.get("humidity", 0)),
                        "wind": int(cur.get("windspeedKmph", 0)),
                        "icon": wttr_icon(desc_val),
                        "city": "Hyderabad",
                        "high": int(daily.get("maxtempC", 0)),
                        "low": int(daily.get("mintempC", 0))
                    }
                except Exception as e:
                    print(f"[Dashboard] error: {e}")

                # --- Full Gmail & Calendar Logic ---
                if os.path.exists(SESSION_FILE):
                    try:
                        with open(SESSION_FILE, "r", encoding="utf-8") as f:
                            google_session = json.load(f)
                        from tools import refresh_token_if_needed, _auth_headers, GMAIL_MESSAGES_URL, CALENDAR_EVENTS_URL
                        refresh_token_if_needed(google_session)
                        headers = _auth_headers(google_session)

                        # Emails
                        em_res = req.get(GMAIL_MESSAGES_URL, headers=headers, params={"maxResults": 5, "labelIds": ["INBOX"]},
                                         timeout=5)
                        if em_res.status_code == 200:
                            new_emails = []
                            for msg in em_res.json().get("messages", []):
                                d = req.get(f"{GMAIL_MESSAGES_URL}/{msg['id']}", headers=headers,
                                            params={"format": "metadata", "metadataHeaders": ["Subject", "From", "Date"]},
                                            timeout=4).json()
                                hdrs = d.get("payload", {}).get("headers", [])
                                sender = next((h["value"] for h in hdrs if h["name"] == "From"), "Unknown")
                                date_r = next((h["value"] for h in hdrs if h["name"] == "Date"), "")
                                if "<" in sender: sender = sender.split("<")[0].strip().strip('"')
                                try:
                                    from email.utils import parsedate_to_datetime
                                    date_str = parsedate_to_datetime(date_r).strftime("%b %d, %I:%M %p")
                                except Exception as e:
                                    print(f"[Dashboard] error: {e}")
                                    date_str = date_r[:16]
                                thread_id = d.get("threadId") or msg.get("threadId") or msg["id"]
                                user_email = google_session.get("user", {}).get("email", "") if isinstance(google_session, dict) else ""
                                new_emails.append({
                                    "id": msg["id"],
                                    "thread_id": thread_id,
                                    "user_email": user_email,
                                    "from": sender,
                                    "subject": next((h["value"] for h in hdrs if h["name"] == "Subject"), "No Subject")[:60],
                                    "date": date_str, "unread": "UNREAD" in d.get("labelIds", [])
                                })
                            if new_emails:
                                slow_update["emails"] = new_emails

                        # Calendar
                        import pytz
                        tz = pytz.timezone("Asia/Kolkata")
                        now = dt.datetime.now(tz)
                        cal_res = req.get(CALENDAR_EVENTS_URL, headers=headers, params={"maxResults": 8,
                                                                                        "timeMin": now.replace(hour=0, minute=0,
                                                                                                               second=0).isoformat(),
                                                                                        "timeMax": now.replace(hour=23,
                                                                                                               minute=59,
                                                                                                               second=59).isoformat(),
                                                                                        "singleEvents": "true",
                                                                                        "orderBy": "startTime"}, timeout=5)
                        if cal_res.status_code == 200:
                            for ev in cal_res.json().get("items", []):
                                start = ev.get("start", {})
                                dt_raw = start.get("dateTime", start.get("date", ""))
                                time_str = "All day"
                                if "T" in dt_raw:
                                    time_str = dt.datetime.fromisoformat(dt_raw).strftime("%I:%M %p").lstrip("0")
                                slow_update["events"].append(
                                    {"id": ev.get("id", ""), "title": ev.get("summary", "Untitled")[:50], "time": time_str})
                    except Exception as e:
                        print(f"[Dashboard] error: {e}")

                # Update cache
                _db_cache["slow_data"] = slow_update
                _db_cache["last_update"] = current_time

    # 3. SPOTIFY REFRESH (Medium speed - refreshes every call)
    spotify_data = ""
    try:
        from spotify_agent import _get_spotify
        sp, err = _get_spotify()
        if not err:
            curr = sp.current_playback()
            if curr and curr.get("item"):
                item = curr["item"]
                track_id = item.get("id")
                images = item.get("album", {}).get("images", [])
                img_url = images[0]["url"] if images else None
                
                color = "#f97316"
                if track_id and img_url:
                    if "spotify_colors" not in _db_cache:
                        _db_cache["spotify_colors"] = {}
                    
                    if track_id in _db_cache["spotify_colors"]:
                        color = _db_cache["spotify_colors"][track_id]
                    else:
                        try:
                            import requests, io, colorsys
                            from PIL import Image
                            r = await asyncio.to_thread(requests.get, img_url, timeout=2)
                            if r.status_code == 200:
                                img = Image.open(io.BytesIO(r.content)).convert("RGB")
                                img.thumbnail((50, 50))
                                colors = img.getcolors(50 * 50)
                                if colors:
                                    colors = sorted(colors, key=lambda x: x[0], reverse=True)
                                    best_color = colors[0][1]
                                    best_score = -1
                                    for count, (r_c, g_c, b_c) in colors[:15]:
                                        h, s, v = colorsys.rgb_to_hsv(r_c/255.0, g_c/255.0, b_c/255.0)
                                        score = s * v * (count ** 0.5)
                                        if score > best_score:
                                            best_score = score
                                            best_color = (r_c, g_c, b_c)
                                    r_c, g_c, b_c = best_color
                                    if r_c < 40 and g_c < 40 and b_c < 40:
                                        r_c, g_c, b_c = min(r_c+50, 255), min(g_c+50, 255), min(b_c+50, 255)
                                    color = f"#{r_c:02x}{g_c:02x}{b_c:02x}"
                                _db_cache["spotify_colors"][track_id] = color
                        except Exception as e:
                            pass

                spotify_data = {
                    "track": item["name"],
                    "artist": ", ".join(a["name"] for a in item["artists"]),
                    "playing": curr.get("is_playing", False),
                    "progress": curr.get("progress_ms", 0),
                    "duration": item.get("duration_ms", 0),
                    "color": color
                }
    except Exception as e:
        print(f"[Dashboard] error: {e}")

    # Combine everything
    return {
        "status": "success",
        "data": {
            "system": system_data,
            "spotify": spotify_data,
            "emails": _db_cache["slow_data"]["emails"],
            "events": _db_cache["slow_data"]["events"],
            "weather": _db_cache["slow_data"]["weather"],
            "google_connected": os.path.exists(SESSION_FILE),
            "google_email": _load_session().get("user", {}).get("email", "") if _load_session() else ""
        }
    }


# ═══════════════════════════════════════════════════════════════════════
# VOICE AUTHENTICATION (SPEAKER VERIFICATION)
# ═══════════════════════════════════════════════════════════════════════

@app.get("/voice-auth/status")
async def voice_auth_status():
    import voice_auth
    return {
        "model_ready": voice_auth.is_model_ready(),
        "enrolled": voice_auth.is_enrolled(),
    }


@app.post("/voice/enroll/start")
async def voice_enroll_start():
    import voice_auth
    voice_auth.delete_enrollment()
    return {"status": "success"}


@app.post("/voice/enroll/record")
async def voice_enroll_record():
    import voice_auth
    import asyncio
    try:
        # Pushed to thread so FastAPI doesn't block for 3 seconds
        await asyncio.to_thread(voice_auth.record_sample, 3.0)
        return {"status": "success", "samples_collected": len(voice_auth.temp_enroll_wavs)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/voice/enroll/commit")
async def voice_enroll_commit():
    import voice_auth
    import asyncio
    # Commit can also take a moment to compute embeddings
    success = await asyncio.to_thread(voice_auth.commit_enrollment)
    if success:
        return {"status": "success", "message": "Voice profile saved!"}
    return {"status": "error", "message": "Failed to save profile. Did you record samples?"}


@app.post("/voice/enroll/delete")
async def voice_enroll_delete():
    import voice_auth
    voice_auth.delete_enrollment()
    return {"status": "success", "message": "Voice profile deleted."}


@app.post("/voice/enroll/cancel")
async def voice_enroll_cancel():
    import voice_auth
    voice_auth.temp_enroll_wavs.clear()
    voice_auth.is_enrolling = False
    return {"status": "success"}


# ═══════════════════════════════════════════════════════════════════════
# LONG-TERM MEMORY (RAG) ROUTES
# ═══════════════════════════════════════════════════════════════════════

@app.get("/memory/all")
async def memory_all():
    import memory_store
    import asyncio
    try:
        mems = await asyncio.to_thread(memory_store.get_all_memories)
        return {"status": "success", "memories": mems}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.delete("/memory/{mem_id}")
async def memory_delete(mem_id: str):
    import memory_store
    import asyncio
    try:
        success = await asyncio.to_thread(memory_store.delete_memory, mem_id)
        if success:
            return {"status": "success"}
        return {"status": "error", "message": "Failed to delete"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

_notifications = []

@app.post("/notify")
async def add_notification(req: Request):
    data = await req.json()
    _notifications.append(data)
    return {"status": "success"}

@app.get("/notifications")
async def get_notifications():
    global _notifications
    if not _notifications:
        return {"notifications": []}
    n = list(_notifications)
    _notifications.clear()
    return {"notifications": n}

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=APP_HOST, port=APP_PORT)