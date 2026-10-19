# timer_agent.py
# No external dependencies needed - uses threading + tkinter
import json
import threading
import time
import re
from datetime import datetime, timedelta
from ai_backend import _call_ollama_chat

TIMER_SYSTEM_PROMPT = """You are an intelligent Timer and Alarm assistant. Parse ANY timer/alarm request and output ONLY valid JSON.

ACTIONS:
set_timer(duration_seconds, label?)   - countdown timer
set_alarm(time_str, label?)           - alarm at specific time
list_timers()                         - show active timers/alarms
cancel_timer(label_or_index)          - cancel a timer
cancel_all()                          - cancel all timers

OUTPUT FORMAT:
{"action":"action_name","data":{"field":"value"}}

DURATION INTELLIGENCE:
- "5 seconds" → duration_seconds: 5
- "10 seconds" → duration_seconds: 10
- "15 seconds" → duration_seconds: 15
- "20 seconds" → duration_seconds: 20
- "30 seconds" → duration_seconds: 30
- "45 seconds" → duration_seconds: 45
- "1 minute" → duration_seconds: 60
- "5 minutes" → duration_seconds: 300
- "10 minutes" → duration_seconds: 600
- "1 hour" → duration_seconds: 3600
- "1 hour 30 minutes" → duration_seconds: 5400
- "90 minutes" → duration_seconds: 5400
- "2 hours 15 minutes" → duration_seconds: 8100

CRITICAL: Seconds means seconds — NEVER convert seconds to minutes. 10 seconds = 10, NOT 60.

TIME INTELLIGENCE:
- "at 7am" → time_str: "07:00"
- "at 3:30pm" → time_str: "15:30"
- "at 6pm" → time_str: "18:00"
- "at 9:15am" → time_str: "09:15"

EXAMPLES:
User: set a timer for 5 seconds
{"steps": [{"action":"set_timer","data":{"duration_seconds":5,"label":"Timer"}}]}

User: set a timer for 10 seconds
{"steps": [{"action":"set_timer","data":{"duration_seconds":10,"label":"Timer"}}]}

User: set a timer for 10 seconds for my tea
{"steps": [{"action":"set_timer","data":{"duration_seconds":10,"label":"Tea Timer"}}]}

User: set a timer for 15 seconds
{"steps": [{"action":"set_timer","data":{"duration_seconds":15,"label":"Timer"}}]}

User: set a timer for 30 seconds
{"steps": [{"action":"set_timer","data":{"duration_seconds":30,"label":"Timer"}}]}

User: timer 30 seconds
{"steps": [{"action":"set_timer","data":{"duration_seconds":30,"label":"Timer"}}]}

User: set a timer for 5 minutes
{"action":"set_timer","data":{"duration_seconds":300,"label":"Timer"}}

User: set a timer for 10 minutes for my tea
{"action":"set_timer","data":{"duration_seconds":600,"label":"Tea Timer"}}

User: set timer for 1 hour 30 minutes
{"steps": [{"action":"set_timer","data":{"duration_seconds":5400,"label":"Timer"}}]}

User: set an alarm for 7am
{"action":"set_alarm","data":{"time_str":"07:00","label":"Morning Alarm"}}

User: alarm at 3:30pm
{"action":"set_alarm","data":{"time_str":"15:30","label":"Afternoon Alarm"}}

User: wake me up at 6am
{"action":"set_alarm","data":{"time_str":"06:00","label":"Wake Up"}}

User: remind me at 5pm
{"action":"set_alarm","data":{"time_str":"17:00","label":"Reminder"}}

User: set alarm at 9pm for gym
{"action":"set_alarm","data":{"time_str":"21:00","label":"Gym"}}

User: show my timers
{"steps": [{"action":"list_timers","data":{}}]}

User: what timers do I have
{"steps": [{"action":"list_timers","data":{}}]}

User: cancel the tea timer
{"action":"cancel_timer","data":{"label_or_index":"Tea Timer"}}

User: cancel all timers
{"steps": [{"action":"cancel_all","data":{}}]}"""


# ── Active timer registry ─────────────────────────────────────────────
_timers: list[dict] = []
_timer_lock = threading.Lock()
_timer_counter = 0

# Optional callback hooks (for future UI/web integration without breaking existing behavior)
_on_timer_set = None
_on_timer_done = None
_on_timer_cancel = None


def register_timer_callbacks(on_set=None, on_done=None, on_cancel=None):
    """
    Optional hooks:
      on_set(entry_dict)
      on_done(entry_dict)
      on_cancel(entry_dict)
    """
    global _on_timer_set, _on_timer_done, _on_timer_cancel
    _on_timer_set = on_set
    _on_timer_done = on_done
    _on_timer_cancel = on_cancel


def _safe_callback(cb, payload):
    if cb:
        try:
            cb(payload)
        except Exception:
            pass


def _fix_wrapper(plan: dict) -> dict:
    if not plan:
        return plan
    action = plan.get("action")
    data = plan.get("data")
    if action and not data:
        top = {k: v for k, v in plan.items() if k != "action"}
        if top:
            return {"action": action, "data": top}
    return plan


def _fire_notification(label: str, message: str):
    """Send a notification to the Electron frontend via FastAPI."""
    import urllib.request
    import json
    try:
        data = json.dumps({"title": label, "message": message}).encode('utf-8')
        req = urllib.request.Request("http://127.0.0.1:8000/notify", data=data, headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=2)
    except Exception as e:
        print(f"⏰ {label}: {message} (notification failed: {e})")


def _play_sound():
    """Play a beep sound on Windows."""
    try:
        import winsound
        for _ in range(3):
            winsound.Beep(1000, 300)
            time.sleep(0.1)
    except Exception:
        pass


class TimerAgent:
    def __init__(self):
        pass

    def run(self, query: str, status_callback=None, piped_context: str = None) -> str:
        q = query.strip()

        # ── Deterministic pre-parse for pure-second queries ──────────
        # Catches "10 seconds", "set a timer for 10 seconds for my tea", etc.
        # Runs BEFORE the AI to guarantee correctness for small values.
        _sec_match = re.search(
            r'(\d+)\s+second(?:s)?',
            q, re.IGNORECASE
        )
        _min_match = re.search(
            r'(\d+)\s+minute(?:s)?',
            q, re.IGNORECASE
        )
        _hr_match  = re.search(
            r'(\d+)\s+hour(?:s)?',
            q, re.IGNORECASE
        )

        # Only use deterministic parse when the query ONLY has seconds (no minutes/hours)
        if _sec_match and not _min_match and not _hr_match:
            secs = int(_sec_match.group(1))
            # Extract an optional label
            label_match = re.search(
                r'for\s+(?:my\s+)?(.+?)\s*(?:timer|alarm)?\s*$',
                q, re.IGNORECASE
            )
            # Filter out duration words from the label
            raw_label = (label_match.group(1).strip() if label_match else "").lower()
            skip_words = {'second', 'seconds', 'minute', 'minutes', 'hour', 'hours', 'timer', 'alarm', 'a', 'the'}
            if raw_label and raw_label not in skip_words:
                label = raw_label.title() + " Timer"
            else:
                label = "Timer"
            return self._set_timer({"duration_seconds": secs, "label": label})

        raw = _call_ollama_chat(system=TIMER_SYSTEM_PROMPT, user=q + "\nJSON:")
        if not raw:
            return "❌ AI returned empty response."
        plan = _fix_wrapper(raw)
        
        steps = plan.get("steps", [plan]) if "steps" in plan else [plan]
        results = []
        for step in steps:
            action = step.get("action", "")
            data = step.get("data", {})
            if not action:
                continue

            if action == "set_timer":
                results.append(self._set_timer(data))
            elif action == "set_alarm":
                results.append(self._set_alarm(data))
            elif action == "list_timers":
                results.append(self._list())
            elif action == "cancel_timer":
                results.append(self._cancel(data.get("label_or_index", "")))
            elif action == "cancel_all":
                results.append(self._cancel_all())
            else:
                results.append(f"❌ Unknown timer action: {action}")
                
        return "\n\n".join(results) if results else "❌ Could not understand the timer command."

    def _set_timer(self, data: dict) -> str:
        secs = int(data.get("duration_seconds", 0))
        if secs <= 0:
            return "❌ Invalid duration."
        label = data.get("label") or "Timer"

        # Format duration
        if secs >= 3600:
            h = secs // 3600
            m = (secs % 3600) // 60
            dur_str = f"{h}h {m}m" if m else f"{h}h"
        elif secs >= 60:
            m = secs // 60
            s = secs % 60
            dur_str = f"{m}m {s}s" if s else f"{m}m"
        else:
            dur_str = f"{secs}s"

        cancel_event = threading.Event()
        global _timer_counter
        with _timer_lock:
            _timer_counter += 1
            entry = {
                "id": _timer_counter,
                "label": label,
                "type": "timer",
                "duration": secs,
                "started_at": datetime.now(),
                "cancel": cancel_event,
                "duration_text": dur_str,
            }
            _timers.append(entry)

        _safe_callback(_on_timer_set, {
            "id": entry["id"],
            "label": label,
            "type": "timer",
            "duration": secs,
            "duration_text": dur_str
        })

        def _run():
            cancel_event.wait(timeout=secs)
            if cancel_event.is_set():
                return

            if not cancel_event.is_set():
                _play_sound()
                _fire_notification(label, f"{label} is complete!\nDuration: {dur_str}")
                _safe_callback(_on_timer_done, {
                    "id": entry["id"],
                    "label": label,
                    "type": "timer",
                    "duration": secs,
                    "duration_text": dur_str
                })
                with _timer_lock:
                    if entry in _timers:
                        _timers.remove(entry)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return f"⏱️ Timer set — \"{label}\" for {dur_str}"

    def _set_alarm(self, data: dict) -> str:
        time_str = data.get("time_str", "").strip()
        label = data.get("label") or "Alarm"
        if not time_str:
            return "❌ Alarm time is required."

        # Parse the alarm time
        now = datetime.now()
        try:
            alarm_dt = datetime.strptime(f"{now.strftime('%Y-%m-%d')} {time_str}", "%Y-%m-%d %H:%M")
            if alarm_dt <= now:
                alarm_dt += timedelta(days=1)  # Next day if time passed
        except Exception:
            return f"❌ Could not parse time: {time_str}"

        secs_until = int((alarm_dt - now).total_seconds())
        alarm_label = alarm_dt.strftime("%I:%M %p")

        cancel_event = threading.Event()
        entry = {
            "id": len(_timers) + 1,
            "label": label,
            "type": "alarm",
            "alarm_at": alarm_dt,
            "cancel": cancel_event,
        }

        with _timer_lock:
            _timers.append(entry)

        _safe_callback(_on_timer_set, {
            "id": entry["id"],
            "label": label,
            "type": "alarm",
            "alarm_at": alarm_dt.isoformat(),
            "alarm_label": alarm_label
        })

        def _run():
            if not cancel_event.wait(timeout=secs_until):
                _play_sound()
                _fire_notification(label, f"{label}!\nTime: {alarm_label}")
                _safe_callback(_on_timer_done, {
                    "id": entry["id"],
                    "label": label,
                    "type": "alarm",
                    "alarm_at": alarm_dt.isoformat(),
                    "alarm_label": alarm_label
                })
                with _timer_lock:
                    if entry in _timers:
                        _timers.remove(entry)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return f"⏰ Alarm set — \"{label}\" at {alarm_label}"

    def _list(self) -> str:
        with _timer_lock:
            if not _timers:
                return "⏱️ No active timers or alarms."
            lines = ["⏱️ Active timers:"]
            for i, e in enumerate(_timers):
                if e["type"] == "timer":
                    elapsed = int((datetime.now() - e["started_at"]).total_seconds())
                    remaining = max(0, e["duration"] - elapsed)
                    m, s = remaining // 60, remaining % 60
                    lines.append(f"  {i+1}. ⏱️ {e['label']} — {m}m {s}s remaining")
                else:
                    at = e["alarm_at"].strftime("%I:%M %p")
                    lines.append(f"  {i+1}. ⏰ {e['label']} — rings at {at}")
            return "\n".join(lines)

    def _cancel(self, label_or_idx) -> str:
        with _timer_lock:
            # Try by number
            try:
                idx = int(str(label_or_idx)) - 1
                if 0 <= idx < len(_timers):
                    entry = _timers[idx]
                    entry["cancel"].set()
                    _timers.pop(idx)
                    _safe_callback(_on_timer_cancel, {
                        "id": entry.get("id"),
                        "label": entry.get("label"),
                        "type": entry.get("type")
                    })
                    return f"✅ Cancelled — \"{entry['label']}\""
            except (ValueError, TypeError):
                pass

            # Try by label — cancel ALL matching entries
            label_lower = str(label_or_idx).lower()
            matched = [e for e in _timers if label_lower in e["label"].lower()]
            for entry in matched:
                entry["cancel"].set()
                _timers.remove(entry)
                _safe_callback(_on_timer_cancel, {
                    "id": entry.get("id"),
                    "label": entry.get("label"),
                    "type": entry.get("type")
                })
            if matched:
                cancelled_names = ", ".join(f'"{e["label"]}"' for e in matched)
                return f"✅ Cancelled {len(matched)} timer(s): {cancelled_names}"

        return f"❌ No timer found matching \"{label_or_idx}\""

    def _cancel_all(self) -> str:
        with _timer_lock:
            count = len(_timers)
            snapshot = list(_timers)
            for e in _timers:
                e["cancel"].set()
            _timers.clear()

        for e in snapshot:
            _safe_callback(_on_timer_cancel, {
                "id": e.get("id"),
                "label": e.get("label"),
                "type": e.get("type")
            })

        return f"✅ Cancelled all {count} timer(s)."