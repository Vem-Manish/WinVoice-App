# calendar_agent.py
import re
import json
import pytz
import dateutil.parser
from datetime import datetime, timedelta
from ai_backend import _call_ollama_chat
from tools import (
    create_event, get_events, update_event, delete_event,
    find_event_id_by_name, find_all_event_instances_by_name, get_event_items,
    sheets_find_contact
)

CALENDAR_SYSTEM_PROMPT = """You are an intelligent Google Calendar assistant. Parse ANY calendar request and output ONLY valid JSON.

ACTIONS:
create_event(title, start_time, end_time, date?, recurrence_rule?, attendees?)
get_events(max_results?)
delete_event(event_id, delete_all?, delete_scope?)
update_event(event_id, update_body)

OUTPUT FORMAT:
{"steps": [{"action":"action_name","data":{"field":"value"}}]}

INTELLIGENCE RULES:
- "add/schedule/create event X at TIME" → create_event
- "remind me to X at TIME" → create_event (reminder = event when time is given)
- "every/weekly/daily/recurring" → create_event with recurrence_rule
- "what events/schedule/calendar" → get_events
- "delete/cancel/remove event" → delete_event
- Always infer end_time: if not stated, add 1 hour to start_time
- date values: "today", "tomorrow", "this monday", "this friday", "this saturday", etc.
- RECURRENCE: RRULE:FREQ=DAILY or RRULE:FREQ=WEEKLY;BYDAY=MO,WE etc
- delete_all defaults true. Set false ONLY if "only this / just this / this week / this saturday"
- delete_scope: "week" if "this week", "month" if "this month"

ATTENDEE INTELLIGENCE:
- "with X" / "and X" / "invite X" → attendees: ["X"] (extract NAMES only, not emails)
- "meeting with manish and sara" → attendees: ["manish", "sara"]
- "call with john" → attendees: ["john"]
- "schedule meeting with manish, sara, and priya" → attendees: ["manish","sara","priya"]
- "team meeting" with no specific names → no attendees field
- Only extract PERSON NAMES — never put emails in attendees
- attendees is a LIST of name strings

TIME INTELLIGENCE:
- "at 6pm" → start_time: "6:00 PM", end_time: "7:00 PM"
- "at 9am" → start_time: "9:00 AM", end_time: "10:00 AM"
- "at 11pm" → start_time: "11:00 PM", end_time: "11:59 PM"
- "from 2pm to 4pm" → start_time: "2:00 PM", end_time: "4:00 PM"
- "for 30 minutes at 3pm" → start_time: "3:00 PM", end_time: "3:30 PM"
- "for 2 hours at 10am" → start_time: "10:00 AM", end_time: "12:00 PM"

RECURRENCE INTELLIGENCE:
- "every monday" → RRULE:FREQ=WEEKLY;BYDAY=MO
- "every friday" → RRULE:FREQ=WEEKLY;BYDAY=FR
- "every monday and wednesday" → RRULE:FREQ=WEEKLY;BYDAY=MO,WE
- "every day / daily" → RRULE:FREQ=DAILY
- "every weekday" → RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR
- "every weekend" → RRULE:FREQ=WEEKLY;BYDAY=SA,SU
- "every tuesday and thursday" → RRULE:FREQ=WEEKLY;BYDAY=TU,TH
- "every monday wednesday friday" → RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR

EXAMPLES:
User: schedule a meeting at 3pm today
{"steps": [{"action":"create_event","data":{"title":"Meeting","start_time":"3:00 PM","end_time":"4:00 PM","date":"today"}}]}

User: schedule a meeting with manish at 5pm today
{"steps": [{"action":"create_event","data":{"title":"Meeting","start_time":"5:00 PM","end_time":"6:00 PM","date":"today","attendees":["manish"]}}]}

User: create an event named project review at 3pm tomorrow with manish and sara
{"steps": [{"action":"create_event","data":{"title":"Project Review","start_time":"3:00 PM","end_time":"4:00 PM","date":"tomorrow","attendees":["manish","sara"]}}]}

User: schedule a call with john from 2pm to 4pm today
{"steps": [{"action":"create_event","data":{"title":"Call with John","start_time":"2:00 PM","end_time":"4:00 PM","date":"today","attendees":["john"]}}]}

User: add team lunch on friday at 1pm with manish sara and priya
{"steps": [{"action":"create_event","data":{"title":"Team Lunch","start_time":"1:00 PM","end_time":"2:00 PM","date":"this friday","attendees":["manish","sara","priya"]}}]}

User: book a standup with the team at 9am
{"steps": [{"action":"create_event","data":{"title":"Standup","start_time":"9:00 AM","end_time":"9:15 AM","date":"today"}}]}

User: add an event called gym at 7am tomorrow
{"steps": [{"action":"create_event","data":{"title":"Gym","start_time":"7:00 AM","end_time":"8:00 AM","date":"tomorrow"}}]}

User: schedule dentist appointment on monday at 10am
{"steps": [{"action":"create_event","data":{"title":"Dentist Appointment","start_time":"10:00 AM","end_time":"11:00 AM","date":"this monday"}}]}

User: add a meeting every friday at 5pm
{"steps": [{"action":"create_event","data":{"title":"Meeting","start_time":"5:00 PM","end_time":"6:00 PM","date":"this friday","recurrence_rule":"RRULE:FREQ=WEEKLY;BYDAY=FR"}}]}

User: add weekly sync with john every monday at 10am
{"steps": [{"action":"create_event","data":{"title":"Weekly Sync","start_time":"10:00 AM","end_time":"11:00 AM","date":"this monday","recurrence_rule":"RRULE:FREQ=WEEKLY;BYDAY=MO","attendees":["john"]}}]}

User: add gym every monday and wednesday at 7am
{"steps": [{"action":"create_event","data":{"title":"Gym","start_time":"7:00 AM","end_time":"8:00 AM","date":"this monday","recurrence_rule":"RRULE:FREQ=WEEKLY;BYDAY=MO,WE"}}]}

User: what events do I have today
{"steps": [{"action":"get_events","data":{"max_results":10}}]}

User: show my calendar for this week
{"steps": [{"action":"get_events","data":{"max_results":20}}]}

User: am I free today
{"steps": [{"action":"get_events","data":{"max_results":10}}]}

User: delete the standup event
{"steps": [{"action":"delete_event","data":{"event_id":"standup","delete_all":true}}]}

User: cancel all gym events
{"steps": [{"action":"delete_event","data":{"event_id":"gym","delete_all":true}}]}

User: delete the meeting event this thursday only
{"steps": [{"action":"delete_event","data":{"event_id":"meeting","delete_all":false}}]}

User: skip this week gym
{"steps": [{"action":"delete_event","data":{"event_id":"gym","delete_all":false,"delete_scope":"week"}}]}

User: delete standup for this month
{"steps": [{"action":"delete_event","data":{"event_id":"standup","delete_all":false,"delete_scope":"month"}}]}"""


WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

# Words the AI might accidentally put in the attendees list
_NOISE_WORDS = {
    "the", "and", "or", "a", "an", "team", "everyone", "all", "us",
    "with", "at", "on", "for", "to", "my", "our",
}


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


def _next_weekday(today, target):
    d = target - today.weekday()
    if d < 0:
        d += 7
    return today + timedelta(days=d)


def _build_event(data: dict, tz_name="Asia/Kolkata") -> dict:
    tz_name = data.get("timezone") or tz_name
    tz = pytz.timezone(tz_name)
    now = datetime.now(tz)
    raw = (data.get("date") or "").lower()

    if "tomorrow" in raw:
        ev_date = now.date() + timedelta(days=1)
    elif "today" in raw or raw == "":
        ev_date = now.date()
    elif "next" in raw:
        ev_date = now.date()
        for d, n in WEEKDAYS.items():
            if d in raw:
                ev_date = _next_weekday(now.date(), n) + timedelta(weeks=1)
                break
    else:
        ev_date = now.date()
        for d, n in WEEKDAYS.items():
            if d in raw:
                ev_date = _next_weekday(now.date(), n)
                break
        else:
            try:
                ev_date = dateutil.parser.parse(raw).date()
            except Exception:
                ev_date = now.date()

    def clean_t(t):
        if not t:
            return None
        return t.lower().replace("today", "").replace("tomorrow", "").replace("next", "").strip()

    st = clean_t(data.get("start_time"))
    et = clean_t(data.get("end_time"))
    if not st:
        raise ValueError("start_time is required")

    st_parsed = dateutil.parser.parse(st)
    et_parsed = dateutil.parser.parse(et) if et else st_parsed + timedelta(hours=1)

    s_dt = tz.localize(datetime.combine(ev_date, st_parsed.time()))
    e_dt = tz.localize(datetime.combine(ev_date, et_parsed.time()))
    if e_dt <= s_dt:
        e_dt += timedelta(days=1)

    payload = {
        "summary": data.get("title", "Event"),
        "start": {"dateTime": s_dt.isoformat(), "timeZone": tz_name},
        "end":   {"dateTime": e_dt.isoformat(), "timeZone": tz_name},
    }
    rrule = data.get("recurrence_rule")
    if rrule:
        if not rrule.startswith("RRULE:"):
            rrule = "RRULE:" + rrule
        payload["recurrence"] = [rrule]
    return payload


def _extract_timezone(query: str) -> str | None:
    q = (query or "").lower()
    zones = {
        "new york": "America/New_York",
        "nyc": "America/New_York",
        "london": "Europe/London",
        "uk": "Europe/London",
        "los angeles": "America/Los_Angeles",
        "california": "America/Los_Angeles",
        "dubai": "Asia/Dubai",
        "singapore": "Asia/Singapore",
        "sydney": "Australia/Sydney",
    }
    if "his time" in q or "her time" in q or "their time" in q:
        for key, zone in zones.items():
            if key in q:
                return zone
    return None


def _event_bounds(ev: dict):
    s = ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date")
    e = ev.get("end", {}).get("dateTime") or ev.get("end", {}).get("date")
    if not s or not e:
        return None, None
    return dateutil.parser.isoparse(s), dateutil.parser.isoparse(e)


def _resolve_attendees(session: dict, names: list[str]) -> tuple[list[dict], list[str]]:
    """
    Look up each name in the Contacts sheet.
    Returns:
      resolved  — list of {"email": "...", "display": "Name (email)"}
      not_found — list of names whose email wasn't in contacts
    """
    resolved = []
    not_found = []

    for name in names:
        name = name.strip()
        if not name or name.lower() in _NOISE_WORDS:
            continue

        try:
            matches = sheets_find_contact(session, name)
        except Exception:
            matches = []

        email_found = None
        display_name = name.title()

        if matches:
            # sheets_find_contact returns (name, email) tuples
            for m in matches:
                contact_name  = m[0] if len(m) > 0 else ""
                contact_email = m[1] if len(m) > 1 else ""
                if contact_email and "@" in contact_email:
                    email_found  = contact_email
                    display_name = contact_name or name.title()
                    break

        if email_found:
            resolved.append({
                "email":   email_found,
                "display": f"{display_name} ({email_found})",
            })
        else:
            not_found.append(name.title())

    return resolved, not_found


def _format_response(data: dict, resolved_attendees: list[dict] = None) -> str:
    title    = data.get("title", "Event")
    date_str = (data.get("date") or "today").replace("this ", "").title()
    start    = data.get("start_time", "")
    end      = data.get("end_time", "")
    rrule    = data.get("recurrence_rule", "")
    days_map = {
        "MO": "Monday", "TU": "Tuesday", "WE": "Wednesday", "TH": "Thursday",
        "FR": "Friday",  "SA": "Saturday", "SU": "Sunday",
    }

    if rrule:
        if "BYDAY=" in rrule:
            raw_days = rrule.split("BYDAY=")[1].split(";")[0].split(",")
            days_str = " & ".join(days_map.get(d, d) for d in raw_days)
            base = f"✅ Recurring event created — \"{title}\" every {days_str} at {start}"
        elif "FREQ=DAILY" in rrule:
            base = f"✅ Daily event created — \"{title}\" at {start} every day"
        else:
            base = f"✅ Recurring event created — \"{title}\" at {start}"
    else:
        time_str = f"{start}" + (f" – {end}" if end else "")
        base = f"✅ Event created — \"{title}\" on {date_str} at {time_str}"

    if resolved_attendees:
        names = ", ".join(a["display"] for a in resolved_attendees)
        base += f"\n👥 Invited: {names}"

    return base


class CalendarAgent:
    def __init__(self, session: dict):
        self.session = session

    def run(self, query: str, status_callback=None, piped_context: str = None) -> str:
        if "free slot" in query.lower() or "free time" in query.lower():
            return self._suggest_free_slots(query)

        raw = _call_ollama_chat(system=CALENDAR_SYSTEM_PROMPT, user=query + "\nJSON:")
        if not raw:
            return "❌ AI returned empty response."
        plan = _fix_wrapper(raw)
        
        # Support both single action and steps array
        steps = plan.get("steps", [plan]) if "steps" in plan else [plan]
        
        results = []
        for step in steps:
            action = step.get("action", "")
            data   = step.get("data", {})
            if not action:
                continue

            if action == "create_event":
                data["timezone"] = _extract_timezone(query) or data.get("timezone")
                res = self._create(data, original_query=query)
                if isinstance(res, dict): return res
                results.append(res)
            elif action == "get_events":
                res = self._get(data, original_query=query)
                if isinstance(res, dict): return res
                results.append(res)
            elif action == "delete_event":
                res = self._delete(data)
                if isinstance(res, dict): return res
                results.append(res)
            elif action == "update_event":
                res = self._update(data)
                if isinstance(res, dict): return res
                results.append(res)
            else:
                results.append(f"❌ Unknown calendar action: {action}")

        return "\n".join(results) if results else "❌ Could not understand the calendar command."

    # ── CREATE ────────────────────────────────────────────────────────────
    def _create(self, data: dict, original_query: str = "") -> str:
        clean = {
            "title":          data.get("title") or data.get("summary", "Event"),
            "start_time":     data.get("start_time") or data.get("time"),
            "end_time":       data.get("end_time"),
            "date":           data.get("date", ""),
            "recurrence_rule": data.get("recurrence_rule"),
            "timezone":       data.get("timezone"),
        }

        # ── Resolve attendees ──────────────────────────────────────────
        raw_names = data.get("attendees") or []
        if isinstance(raw_names, str):
            # AI occasionally returns a comma-separated string instead of a list
            raw_names = [n.strip() for n in re.split(r"[,;]", raw_names) if n.strip()]

        resolved_attendees: list[dict] = []
        not_found_names:    list[str]  = []

        if raw_names:
            resolved_attendees, not_found_names = _resolve_attendees(
                self.session, raw_names
            )

        # ── Build event body ───────────────────────────────────────────
        try:
            body = _build_event(clean)
        except ValueError as e:
            return f"❌ Could not create event: {e}"
        except Exception as e:
            return f"❌ Event error: {e}"

        # ── Conflict check ─────────────────────────────────────────────
        conflicts = self._conflicts(body)
        if conflicts:
            names = ", ".join(c.get("summary", "Busy") for c in conflicts[:3])
            return (
                f"⚠️ Conflict detected at that time: {names}. "
                f"I did not create the event."
            )

        # ── Attach attendees — Google Calendar will email each one ─────
        if resolved_attendees:
            body["attendees"] = [{"email": a["email"]} for a in resolved_attendees]

        # ── Create in Google Calendar ──────────────────────────────────
        try:
            # Pass sendUpdates="all" so Google emails the invite to each attendee
            create_event(self.session, body, send_updates="all" if resolved_attendees else "none")
        except TypeError:
            # Older tools.py signature without send_updates — fall back silently
            try:
                create_event(self.session, body)
            except Exception as e:
                return f"❌ Failed to save event: {e}"
        # ── Build response ─────────────────────────────────────────────
        response = _format_response(clean, resolved_attendees)

        if not_found_names:
            missing = ", ".join(not_found_names)
            response += (
                f"\n⚠️ No email found for: {missing}. "
                f"Add them to contacts to include them in future invites."
            )

        return response

    # ── HELPERS ───────────────────────────────────────────────────────────
    def _conflicts(self, body: dict) -> list[dict]:
        start = body["start"]["dateTime"]
        end   = body["end"]["dateTime"]
        items = get_event_items(self.session, time_min=start, time_max=end, max_results=20)
        out   = []
        new_s = dateutil.parser.isoparse(start)
        new_e = dateutil.parser.isoparse(end)
        for ev in items:
            s, e = _event_bounds(ev)
            if s and e and s < new_e and e > new_s:
                out.append(ev)
        return out

    def _schedule_local_reminder(self, body: dict):
        pass

    def _suggest_free_slots(self, query: str) -> str:
        tz   = pytz.timezone("Asia/Kolkata")
        now  = datetime.now(tz)
        duration = timedelta(hours=1)
        m = re.search(r'(\d+)\s*(hour|hr|minute|min)', query.lower())
        if m:
            n = int(m.group(1))
            duration = (
                timedelta(hours=n)
                if m.group(2).startswith(("hour", "hr"))
                else timedelta(minutes=n)
            )
        week_end = now + timedelta(days=7)
        items = get_event_items(
            self.session, now.isoformat(), week_end.isoformat(), max_results=100
        )
        busy = []
        for ev in items:
            s, e = _event_bounds(ev)
            if s and e:
                busy.append((s.astimezone(tz), e.astimezone(tz)))
        busy.sort()
        suggestions = []
        cursor = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        while cursor < week_end and len(suggestions) < 3:
            if cursor.hour < 9:
                cursor = cursor.replace(hour=9)
            if cursor.hour >= 18:
                cursor = (cursor + timedelta(days=1)).replace(hour=9)
                continue
            slot_end = cursor + duration
            if all(not (s < slot_end and e > cursor) for s, e in busy):
                suggestions.append(cursor)
                cursor = slot_end
            else:
                cursor += timedelta(minutes=30)
        if not suggestions:
            return "I could not find a free slot this week."
        lines = ["Here are the next 3 free slots:"]
        for s in suggestions:
            lines.append(f"  • {s.strftime('%A, %b %d at %I:%M %p')}")
        return "\n".join(lines)

    # ── GET / DELETE / UPDATE ─────────────────────────────────────────────
    def _get(self, data: dict, original_query: str = "") -> str:
        try:
            import pytz
            from datetime import datetime as _dt, timedelta as _td
            tz = pytz.timezone("Asia/Kolkata")
            now = _dt.now(tz)
            q = (original_query or data.get("query", "")).lower()

            # Determine the right time window based on what the user asked
            time_min = None
            time_max = None

            if "today" in q or "am i free" in q:
                # Today only: midnight → 23:59
                time_min = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
                time_max = now.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()
            elif "tomorrow" in q:
                tomorrow = now + _td(days=1)
                time_min = tomorrow.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
                time_max = tomorrow.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()
            elif "this week" in q or "week" in q:
                time_min = now.isoformat()
                # End of the current week (Sunday 23:59)
                days_until_sunday = 6 - now.weekday()
                week_end = now + _td(days=days_until_sunday)
                time_max = week_end.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()
            elif "next week" in q:
                days_until_monday = 7 - now.weekday()
                next_mon = now + _td(days=days_until_monday)
                next_sun = next_mon + _td(days=6)
                time_min = next_mon.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
                time_max = next_sun.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()
            elif "this month" in q or "month" in q:
                time_min = now.isoformat()
                # End of current month
                import calendar as _cal
                last_day = _cal.monthrange(now.year, now.month)[1]
                time_max = now.replace(day=last_day, hour=23, minute=59, second=59, microsecond=0).isoformat()
            # else: no time_max → show next N upcoming events (existing behaviour for open-ended queries)

            return get_events(
                self.session,
                max_results=data.get("max_results", 10),
                time_min=time_min,
                time_max=time_max,
            )
        except Exception as e:
            return f"❌ Could not fetch events: {e}"

    def _delete(self, data: dict) -> dict | str:
        raw_id = data.get("event_id", "")
        name   = raw_id.replace("today", "").replace("titled", "").replace("recurring", "").strip()
        delete_all = data.get("delete_all", True)
        scope      = data.get("delete_scope")
        try:
            if scope in ("week", "month"):
                days = 7 if scope == "week" else 31
                ids  = find_all_event_instances_by_name(self.session, name, days=days)
                if not ids:
                    return f"❌ No \"{name}\" events found in the next {scope}."
                return {
                    "pending_confirmation": True,
                    "type": "delete_event",
                    "subject": f"Delete {len(ids)} occurrence(s) of '{name}' for this {scope}",
                    "ids": ids,
                    "delete_all_recurring": False,
                    "tts": True
                }
            eid, is_rec = find_event_id_by_name(
                self.session, name, single_instance=not delete_all
            )
            if not eid:
                return f"❌ No event found matching \"{name}\""
            
            return {
                "pending_confirmation": True,
                "type": "delete_event",
                "subject": f"Delete event '{name}'",
                "ids": [eid],
                "delete_all_recurring": bool(is_rec and delete_all),
                "tts": True
            }
        except Exception as e:
            return f"❌ Delete failed: {e}"
            
    def execute_pending(self, pending: dict) -> str:
        try:
            if pending.get("type") == "delete_event":
                ids = pending.get("ids", [])
                delete_all_recurring = pending.get("delete_all_recurring", False)
                for eid in ids:
                    delete_event(self.session, eid, delete_all_recurring=delete_all_recurring)
                return "✅ Event(s) deleted successfully."
            return "❌ Unknown pending action."
        except Exception as e:
            return f"❌ Execution failed: {e}"


    def _update(self, data: dict) -> str:
        try:
            raw_id = data.get("event_id", "")
            name   = raw_id.replace("today", "").replace("titled", "").replace("recurring", "").strip()
            eid, _ = find_event_id_by_name(self.session, name, single_instance=False)
            if not eid:
                return f"❌ No event found matching \"{name}\""
            update_event(self.session, eid, data.get("update_body", {}))
            return "✅ Event updated."
        except Exception as e:
            return f"❌ Update failed: {e}"