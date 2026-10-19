import json
import os
import sqlite3
import threading
import time
from datetime import datetime


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "winvoice_state.db")
_LOCK = threading.RLock()


def _connect():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_state_db():
    with _LOCK, _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS command_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                command TEXT NOT NULL,
                plan_json TEXT,
                status TEXT DEFAULT 'queued',
                created_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS macros (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                steps_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_emails (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                to_name TEXT,
                to_addr TEXT NOT NULL,
                subject TEXT,
                body_html TEXT,
                attachments_json TEXT DEFAULT '[]',
                send_at REAL NOT NULL,
                status TEXT DEFAULT 'pending',
                error TEXT,
                created_at REAL NOT NULL,
                sent_at REAL
            )
        """)
        conn.commit()


def add_command(command: str, plan: dict | None = None, status: str = "queued") -> int:
    init_state_db()
    with _LOCK, _connect() as conn:
        cur = conn.execute(
            "INSERT INTO command_history(command, plan_json, status, created_at) VALUES(?,?,?,?)",
            (command, json.dumps(plan or {}), status, time.time()),
        )
        conn.commit()
        conn.execute(
            "DELETE FROM command_history WHERE id NOT IN "
            "(SELECT id FROM command_history ORDER BY id DESC LIMIT 20)"
        )
        conn.commit()
        return int(cur.lastrowid)


def update_command(command_id: int, plan: dict | None = None, status: str | None = None):
    init_state_db()
    with _LOCK, _connect() as conn:
        if plan is not None:
            conn.execute("UPDATE command_history SET plan_json=? WHERE id=?",
                         (json.dumps(plan), command_id))
        if status is not None:
            conn.execute("UPDATE command_history SET status=? WHERE id=?", (status, command_id))
        conn.commit()


def get_recent_commands(limit: int = 5, skip_last: int = 0) -> list[dict]:
    init_state_db()
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM command_history ORDER BY id DESC LIMIT ? OFFSET ?",
            (int(limit), int(skip_last)),
        ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        try:
            d["plan"] = json.loads(d.pop("plan_json") or "{}")
        except Exception:
            d["plan"] = {}
        d["created_iso"] = datetime.fromtimestamp(d["created_at"]).isoformat()
        out.append(d)
    return out


def get_last_command(exclude: str | None = None) -> dict | None:
    items = get_recent_commands(20)
    for item in items:
        if exclude and item["command"].strip().lower() == exclude.strip().lower():
            continue
        return item
    return None


def save_macro(name: str, steps: list[dict]) -> int:
    init_state_db()
    now = time.time()
    with _LOCK, _connect() as conn:
        conn.execute(
            """
            INSERT INTO macros(name, steps_json, created_at, updated_at)
            VALUES(?,?,?,?)
            ON CONFLICT(name) DO UPDATE SET steps_json=excluded.steps_json, updated_at=excluded.updated_at
            """,
            (name.strip().lower(), json.dumps(steps), now, now),
        )
        conn.commit()
        row = conn.execute("SELECT id FROM macros WHERE name=?", (name.strip().lower(),)).fetchone()
        return int(row["id"])


def get_macro(name: str) -> dict | None:
    init_state_db()
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT * FROM macros WHERE name=?", (name.strip().lower(),)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["steps"] = json.loads(d.pop("steps_json") or "[]")
    return d


def list_macros() -> list[dict]:
    init_state_db()
    with _LOCK, _connect() as conn:
        rows = conn.execute("SELECT id,name,updated_at FROM macros ORDER BY name").fetchall()
    return [dict(r) for r in rows]


def delete_macro(name: str) -> bool:
    init_state_db()
    with _LOCK, _connect() as conn:
        cur = conn.execute("DELETE FROM macros WHERE name=?", (name.strip().lower(),))
        conn.commit()
        return cur.rowcount > 0


def add_scheduled_email(to_name: str, to_addr: str, subject: str, body_html: str,
                        attachments: list[str], send_at: float) -> int:
    init_state_db()
    with _LOCK, _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO scheduled_emails
            (to_name,to_addr,subject,body_html,attachments_json,send_at,status,created_at)
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (to_name, to_addr, subject, body_html, json.dumps(attachments or []),
             float(send_at), "pending", time.time()),
        )
        conn.commit()
        return int(cur.lastrowid)


def due_scheduled_emails(now: float | None = None) -> list[dict]:
    init_state_db()
    now = time.time() if now is None else now
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM scheduled_emails WHERE status='pending' AND send_at<=? ORDER BY send_at",
            (now,),
        ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["attachments"] = json.loads(d.pop("attachments_json") or "[]")
        out.append(d)
    return out


def mark_scheduled_email(email_id: int, status: str, error: str | None = None):
    init_state_db()
    with _LOCK, _connect() as conn:
        conn.execute(
            "UPDATE scheduled_emails SET status=?, error=?, sent_at=? WHERE id=?",
            (status, error, time.time() if status == "sent" else None, email_id),
        )
        conn.commit()


def list_scheduled_emails(limit: int = 20) -> list[dict]:
    init_state_db()
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM scheduled_emails ORDER BY send_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["attachments"] = json.loads(d.pop("attachments_json") or "[]")
        out.append(d)
    return out

def delete_scheduled_email(email_id: int):
    """Mark scheduled email as deleted instead of actually deleting."""
    mark_scheduled_email(email_id, "deleted")