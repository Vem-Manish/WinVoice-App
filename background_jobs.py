import os
import threading
import time

from state_store import due_scheduled_emails, mark_scheduled_email, init_state_db


_started = False


def _load_google_session():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session.json")
    if not os.path.exists(path):
        return None
    import json
    from tools import session_lock
    with session_lock, open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _scheduled_email_loop(interval: int = 30):
    while True:
        try:
            session = _load_google_session()
            if session:
                from tools import send_email, refresh_token_if_needed, save_session
                try:
                    refresh_token_if_needed(session)
                    save_session(session)
                except Exception as e:
                    print(f"[Scheduler] Token refresh failed: {e}")
                from_addr = session.get("user", {}).get("email", "")
                for item in due_scheduled_emails():
                    try:
                        send_email(
                            session, from_addr, item["to_addr"], item.get("subject") or "",
                            item.get("body_html") or "", attachments=item.get("attachments", []),
                        )
                        mark_scheduled_email(item["id"], "sent")
                    except Exception as e:
                        mark_scheduled_email(item["id"], "error", str(e))
        except Exception as e:
            print(f"[Scheduler] scheduled email loop error: {e}")
        time.sleep(interval)


def _token_validation_loop(interval: int = 300):  # Every 5 minutes
    """Proactively validate and refresh Google tokens."""
    while True:
        try:
            session = _load_google_session()
            if session:
                from tools import refresh_token_if_needed, save_session
                try:
                    refresh_token_if_needed(session)
                    save_session(session)
                    # If we got here the token is valid (or was silently refreshed)
                except Exception as e:
                    print(f"[Token] Validation/refresh failed: {e}")
        except Exception as e:
            print(f"[Token Validator] error: {e}")
        time.sleep(interval)


def start_background_jobs():
    global _started, _email_thread, _token_thread
    if not _started:
        init_state_db()
        _started = True

    if '_email_thread' not in globals() or not _email_thread.is_alive():
        _email_thread = threading.Thread(target=_scheduled_email_loop, daemon=True)
        _email_thread.start()
        
    if '_token_thread' not in globals() or not _token_thread.is_alive():
        _token_thread = threading.Thread(target=_token_validation_loop, daemon=True)
        _token_thread.start()
