# tools.py
import os
import time
import base64
import threading

session_lock = threading.Lock()
import requests
import json
import datetime
import mimetypes
from email.message import EmailMessage
from dotenv import load_dotenv

load_dotenv()

# ── Google API base URLs ──────────────────────────────────────────────
GMAIL_SEND_URL      = "https://www.googleapis.com/gmail/v1/users/me/messages/send"
GMAIL_DRAFT_URL     = "https://www.googleapis.com/gmail/v1/users/me/drafts"
GMAIL_MESSAGES_URL  = "https://www.googleapis.com/gmail/v1/users/me/messages"
GMAIL_LABELS_URL    = "https://www.googleapis.com/gmail/v1/users/me/labels"
GMAIL_MODIFY_URL    = "https://www.googleapis.com/gmail/v1/users/me/messages/{id}/modify"
GMAIL_TRASH_URL     = "https://www.googleapis.com/gmail/v1/users/me/messages/{id}/trash"
CALENDAR_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
TASKS_URL           = "https://tasks.googleapis.com/tasks/v1"
DOCS_URL            = "https://docs.googleapis.com/v1/documents"
DRIVE_URL           = "https://www.googleapis.com/drive/v3/files"
SHEETS_URL          = "https://sheets.googleapis.com/v4/spreadsheets"

# ── Local cache file ──────────────────────────────────────────────────
CONTACTS_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "contacts_sheet_cache.json")
CONTACTS_SHEET_NAME = "Contacts"

# ── Session file (absolute path so it works regardless of cwd) ─────────
_SESSION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session.json")


# ═══════════════════════════════════════════════════════════════════════
# AUTH
# ═══════════════════════════════════════════════════════════════════════

def _auth_headers(session):
    return {"Authorization": f"Bearer {session['tokens']['access_token']}"}


def save_session(session, path=None):
    if path is None:
        path = _SESSION_FILE
    with session_lock, open(path, "w", encoding="utf-8") as f:
        json.dump(session, f, indent=2)


def refresh_token_if_needed(session):
    tokens = session["tokens"]
    if "expires_at" not in tokens:
        tokens["expires_at"] = time.time() + int(tokens.get("expires_in", 3600))
    if tokens["expires_at"] - time.time() > 30:
        return
    resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id":     session["client_id"],
            "client_secret": session["client_secret"],
            "refresh_token": tokens["refresh_token"],
            "grant_type":    "refresh_token",
        },
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Token refresh failed: {resp.text}")
    new = resp.json()
    tokens["access_token"] = new["access_token"]
    tokens["expires_at"]   = time.time() + int(new.get("expires_in", 3600))
    save_session(session)



# ═══════════════════════════════════════════════════════════════════════
# GOOGLE SHEETS — CONTACTS
# Sheet columns: A=Name, B=Email
# ═══════════════════════════════════════════════════════════════════════

def _load_cached_sheet_id() -> str | None:
    try:
        if os.path.exists(CONTACTS_CACHE_FILE):
            with open(CONTACTS_CACHE_FILE, "r") as f:
                return json.load(f).get("sheet_id")
    except Exception:
        pass
    return None


def _save_cached_sheet_id(sheet_id: str):
    with open(CONTACTS_CACHE_FILE, "w") as f:
        json.dump({"sheet_id": sheet_id}, f)


def _find_contacts_sheet(session) -> str | None:
    refresh_token_if_needed(session)
    query = (
        f"name = '{CONTACTS_SHEET_NAME}' "
        f"and mimeType = 'application/vnd.google-apps.spreadsheet' "
        f"and trashed = false"
    )
    r = requests.get(
        DRIVE_URL,
        headers=_auth_headers(session),
        params={"q": query, "fields": "files(id,name)"}
    )
    r.raise_for_status()
    files = r.json().get("files", [])
    return files[0]["id"] if files else None


def _create_contacts_sheet(session) -> str:
    """Create Contacts sheet with Name | Email headers."""
    refresh_token_if_needed(session)
    body = {
        "properties": {"title": CONTACTS_SHEET_NAME},
        "sheets": [{"properties": {"title": CONTACTS_SHEET_NAME, "index": 0}}]
    }
    r = requests.post(SHEETS_URL, headers=_auth_headers(session), json=body)
    r.raise_for_status()
    sheet_id = r.json()["spreadsheetId"]
    # Write headers: Name | Email
    _sheets_write(session, sheet_id, f"{CONTACTS_SHEET_NAME}!A1:B1", [["Name", "Email"]])
    print(f"✅ Created 'Contacts' sheet with Name|Email columns (ID: {sheet_id})")
    return sheet_id


def _get_or_create_contacts_sheet(session) -> str:
    sheet_id = _load_cached_sheet_id()
    if sheet_id:
        return sheet_id
    print("🔍 Looking for 'Contacts' sheet...")
    sheet_id = _find_contacts_sheet(session)
    if not sheet_id:
        print("📄 Creating 'Contacts' sheet...")
        sheet_id = _create_contacts_sheet(session)
    _save_cached_sheet_id(sheet_id)
    return sheet_id






# ── Low-level Sheets helpers ──────────────────────────────────────────

def _sheets_read(session, sheet_id: str, range_: str) -> list:
    refresh_token_if_needed(session)
    r = requests.get(
        f"{SHEETS_URL}/{sheet_id}/values/{range_}",
        headers=_auth_headers(session)
    )
    r.raise_for_status()
    return r.json().get("values", [])


def _sheets_write(session, sheet_id: str, range_: str, values: list):
    refresh_token_if_needed(session)
    r = requests.put(
        f"{SHEETS_URL}/{sheet_id}/values/{range_}",
        headers=_auth_headers(session),
        params={"valueInputOption": "RAW"},
        json={"values": values}
    )
    r.raise_for_status()
    return r.json()


def _sheets_append(session, sheet_id: str, range_: str, values: list):
    refresh_token_if_needed(session)
    r = requests.post(
        f"{SHEETS_URL}/{sheet_id}/values/{range_}:append",
        headers=_auth_headers(session),
        params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
        json={"values": values}
    )
    r.raise_for_status()
    return r.json()


def _sheets_update_row(session, sheet_id: str, row_number: int, values: list):
    """Update a specific row (1-indexed)."""
    refresh_token_if_needed(session)
    range_ = f"{CONTACTS_SHEET_NAME}!A{row_number}:B{row_number}"
    r = requests.put(
        f"{SHEETS_URL}/{sheet_id}/values/{range_}",
        headers=_auth_headers(session),
        params={"valueInputOption": "RAW"},
        json={"values": [values]}
    )
    r.raise_for_status()


# ── Public contact functions ──────────────────────────────────────────

def sheets_add_contact(session, name: str, email: str) -> str:
    """
    Add a contact with Name and Email.

    Duplicate detection (by name, case-insensitive):
      - If a contact with the same name already exists, return a dict:
          {
            "conflict": True,
            "row_number": <int>,
            "existing": {"name","email"},
            "incoming": {"name","email"},
          }
        The caller (ContactsAgent) uses this to show a confirmation dialog.
      - If no name conflict but same email exists, return an error string.
      - Otherwise append the new row and return a success string.
    """
    try:
        sheet_id = _get_or_create_contacts_sheet(session)
        rows = _sheets_read(session, sheet_id, f"{CONTACTS_SHEET_NAME}!A:B")

        # ── Name-based duplicate check ────────────────────────────────
        for i, row in enumerate(rows[1:], start=2):
            if not row or not row[0].strip():
                continue
            existing_name = row[0].strip()
            if existing_name.lower() == name.strip().lower():
                existing_email = row[1].strip() if len(row) >= 2 else ""
                return {
                    "conflict":   True,
                    "row_number": i,
                    "existing":   {"name": existing_name, "email": existing_email},
                    "incoming":   {"name": name.strip(),  "email": email or ""},
                }

        # ── Email-based duplicate check ───────────────────────────────
        if email:
            for row in rows[1:]:
                if len(row) >= 2 and row[1].strip().lower() == email.strip().lower():
                    return f"⚠️ Contact '{row[0]}' with email '{email}' already exists."

        # ── No conflict — append new row ──────────────────────────────
        row_data = [name.strip(), email or ""]
        _sheets_append(session, sheet_id, f"{CONTACTS_SHEET_NAME}!A:B", [row_data])
        parts = [name]
        if email:
            parts.append(email)
        return f"✅ Contact saved — {' | '.join(parts)}"
    except Exception as e:
        return f"❌ Failed to add contact: {e}"


def sheets_update_contact_fields(session, row_number: int, name: str, email: str, phone: str = "") -> str:
    """
    Overwrite a specific row (1-indexed) with the merged contact data.
    Called after the user confirms the 'contact exists' dialog.
    """
    try:
        sheet_id = _get_or_create_contacts_sheet(session)
        _sheets_update_row(session, sheet_id, row_number, [name, email or ""])
        parts = [name]
        if email:
            parts.append(email)
        return f"✅ Contact updated — {' | '.join(parts)}"
    except Exception as e:
        return f"❌ Failed to update contact: {e}"



def sheets_get_all_contacts(session) -> str:
    try:
        sheet_id = _get_or_create_contacts_sheet(session)
        rows = _sheets_read(session, sheet_id, f"{CONTACTS_SHEET_NAME}!A:B")
        if not rows or len(rows) <= 1:
            return "📋 No contacts found."
        lines = []
        for row in rows[1:]:
            if not row or not row[0].strip():
                continue
            name  = row[0]
            email = row[1] if len(row) >= 2 else ""
            parts = [f"• {name}"]
            if email:
                parts.append(email)
            lines.append(" — ".join(parts))
        return "📋 Your contacts:\n" + "\n".join(lines) if lines else "📋 No contacts found."
    except Exception as e:
        return f"❌ Failed to get contacts: {e}"


def sheets_find_contact(session, name: str) -> list:
    """
    Returns list of (name, email) tuples matching the given name.
    """
    try:
        sheet_id = _get_or_create_contacts_sheet(session)
        rows = _sheets_read(session, sheet_id, f"{CONTACTS_SHEET_NAME}!A:B")
        matches = []
        for row in rows[1:]:
            if len(row) >= 1 and name.lower() in row[0].lower():
                r_name  = row[0]
                r_email = row[1] if len(row) >= 2 else ""
                matches.append((r_name, r_email))
        return matches
    except Exception as e:
        print(f"[Sheets] find_contact error: {e}")
        return []


def sheets_delete_contact(session, name: str) -> str:
    try:
        sheet_id = _get_or_create_contacts_sheet(session)
        rows = _sheets_read(session, sheet_id, f"{CONTACTS_SHEET_NAME}!A:B")
        for i, row in enumerate(rows[1:], start=2):  # start=2: 1-indexed + 1 for header
            if len(row) >= 1 and name.lower() in row[0].lower():
                # Clear the row by overwriting with empty values
                refresh_token_if_needed(session)
                range_ = f"{CONTACTS_SHEET_NAME}!A{i}:B{i}"
                requests.post(
                    f"{SHEETS_URL}/{sheet_id}/values/{range_}:clear",
                    headers=_auth_headers(session)
                )
                return f"✅ Contact '{row[0]}' deleted."
        return f"❌ No contact found with name '{name}'"
    except Exception as e:
        return f"❌ Failed to delete contact: {e}"


# ═══════════════════════════════════════════════════════════════════════
# WEB SEARCH
# ═══════════════════════════════════════════════════════════════════════

def search_web(query: str) -> str:
    print(f"   🔍 Searching: {query}")
    try:
        from ddgs import DDGS
        from ai_backend import call_ollama_raw
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=5))
        if results:
            raw_snippets = "\n\n".join([
                f"- {r.get('title','')}: {r.get('body', r.get('snippet',''))}"
                for r in results
            ])
            print("   🤖 Synthesizing search results...")
            summary = call_ollama_raw(
                f"Based on these search results, write a clear factual 5-sentence summary about: {query}\n\n"
                f"SEARCH RESULTS:\n{raw_snippets}\n\n"
                f"OUTPUT RULES:\n- Write flowing sentences, not bullet points\n"
                f"- Be factual and concise\n- No fluff or disclaimers\n- Just the summary directly"
            )
            if summary:
                print("   ✅ Search + synthesis complete")
                return f"Title: {query}\nSnippet: {summary}\n---"
            return f"Title: {query}\nSnippet: {raw_snippets}\n---"
        print("   ⚠️ DDGS returned no results")
    except ImportError:
        print("   ⚠️ ddgs not installed. Run: pip install ddgs")
    except Exception as e:
        print(f"   ⚠️ DDGS failed: {e}")
    try:
        import urllib.parse, urllib.request
        search_params = urllib.parse.urlencode({
            "action": "query", "list": "search",
            "srsearch": query, "format": "json", "srlimit": 1,
        })
        req = urllib.request.Request(
            f"https://en.wikipedia.org/w/api.php?{search_params}",
            headers={"User-Agent": "WinVoice/1.0"}
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            search_data = json.loads(r.read().decode())
        top = search_data.get("query", {}).get("search", [])
        if top:
            title = top[0]["title"]
            summary_req = urllib.request.Request(
                f"https://en.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(title)}",
                headers={"User-Agent": "WinVoice/1.0"}
            )
            with urllib.request.urlopen(summary_req, timeout=8) as r:
                data = json.loads(r.read().decode())
            extract = data.get("extract", "")
            if extract and len(extract) > 50:
                return f"Title: {data['title']}\nSnippet: {extract}\n---"
    except Exception as e:
        print(f"   ⚠️ Wikipedia fallback failed: {e}")
    try:
        from ai_backend import call_ollama_raw
        import datetime as _dt
        today = _dt.datetime.now().strftime("%B %d, %Y")
        result = call_ollama_raw(
            f"Today's date is {today}. Give a factual 5-sentence summary about: {query}. "
            f"Be accurate and concise. Only state facts you are confident about; "
            f"if you don't have reliable current data, say so clearly."
        )
        if result:
            return f"Title: {query}\nSnippet: {result}\n---"
    except Exception as e:
        print(f"   ⚠️ LLM fallback failed: {e}")
    return "No search results found."


# ═══════════════════════════════════════════════════════════════════════
# GMAIL
# ═══════════════════════════════════════════════════════════════════════

def _build_email(from_addr, to_addr, subject, html_body, attachments=None):
    msg = EmailMessage()
    msg["From"]    = from_addr
    msg["To"]      = to_addr
    msg["Subject"] = subject
    msg.set_content(html_body, subtype="html")
    for path in attachments or []:
        if not path or not os.path.isfile(path):
            continue
        ctype, _ = mimetypes.guess_type(path)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        with open(path, "rb") as f:
            msg.add_attachment(
                f.read(),
                maintype=maintype,
                subtype=subtype,
                filename=os.path.basename(path),
            )
    return base64.urlsafe_b64encode(msg.as_bytes()).decode().rstrip("=")


def send_email(session, from_addr, to_addr, subject, html_body, attachments=None):
    refresh_token_if_needed(session)
    raw = _build_email(from_addr, to_addr, subject, html_body, attachments=attachments)
    r = requests.post(GMAIL_SEND_URL, headers=_auth_headers(session), json={"raw": raw})
    r.raise_for_status()
    return r.json()


def create_draft(session, from_addr, to_addr, subject, html_body, attachments=None):
    refresh_token_if_needed(session)
    raw = _build_email(from_addr, to_addr, subject, html_body, attachments=attachments)
    r = requests.post(GMAIL_DRAFT_URL, headers=_auth_headers(session),
                      json={"message": {"raw": raw}})
    r.raise_for_status()
    return r.json()


def get_emails(session, query=None, max_results=5):
    refresh_token_if_needed(session)
    params = {"maxResults": max_results, "labelIds": ["INBOX"]}
    if query:
        params["q"] = query
    r = requests.get(GMAIL_MESSAGES_URL, headers=_auth_headers(session), params=params)
    r.raise_for_status()
    messages = r.json().get("messages", [])
    if not messages:
        return "📭 No emails found in your inbox."
    readable = []
    for msg in messages:
        d = requests.get(
            f"{GMAIL_MESSAGES_URL}/{msg['id']}",
            headers=_auth_headers(session),
            params={"format": "metadata"}
        ).json()
        snippet = d.get("snippet", "")
        hdrs    = d.get("payload", {}).get("headers", [])
        subject, sender = "No Subject", "Unknown"
        for h in hdrs:
            if h["name"] == "Subject": subject = h["value"]
            elif h["name"] == "From":  sender  = h["value"]
        if "<" in sender:
            sender = sender.split("<")[0].strip().replace('"', '')
        readable.append(f"📧 **{sender}**\n   {subject}\n   _{snippet}_")
    return "\n\n".join(readable)


def get_email_thread_id(session, from_name: str):
    refresh_token_if_needed(session)
    params = {"maxResults": 1, "labelIds": ["INBOX"], "q": f"from:{from_name}"}
    r = requests.get(GMAIL_MESSAGES_URL, headers=_auth_headers(session), params=params)
    r.raise_for_status()
    messages = r.json().get("messages", [])
    if not messages:
        return None, None, None
    msg_id = messages[0]["id"]
    d = requests.get(
        f"{GMAIL_MESSAGES_URL}/{msg_id}",
        headers=_auth_headers(session),
        params={"format": "metadata"}
    ).json()
    thread_id = d.get("threadId")
    headers = d.get("payload", {}).get("headers", [])
    message_id_header = next((h["value"] for h in headers if h["name"] == "Message-ID"), None)
    subject = next((h["value"] for h in headers if h["name"] == "Subject"), "Re: ")
    return thread_id, message_id_header, subject


def reply_email(session, from_addr, to_addr, subject, html_body, thread_id, message_id_header):
    refresh_token_if_needed(session)
    msg = EmailMessage()
    msg["From"]    = from_addr
    msg["To"]      = to_addr
    msg["Subject"] = subject if subject.startswith("Re:") else f"Re: {subject}"
    if message_id_header:
        msg["In-Reply-To"] = message_id_header
        msg["References"]  = message_id_header
    msg.set_content(html_body, subtype="html")
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode().rstrip("=")
    r = requests.post(
        GMAIL_SEND_URL,
        headers=_auth_headers(session),
        json={"raw": raw, "threadId": thread_id}
    )
    r.raise_for_status()
    return r.json()


def get_labels(session):
    refresh_token_if_needed(session)
    r = requests.get(GMAIL_LABELS_URL, headers=_auth_headers(session))
    r.raise_for_status()
    return r.json()["labels"]


def modify_labels(session, message_id, add_labels=None, remove_labels=None):
    refresh_token_if_needed(session)
    body = {}
    if add_labels:    body["addLabelIds"]    = add_labels
    if remove_labels: body["removeLabelIds"] = remove_labels
    url = GMAIL_MODIFY_URL.format(id=message_id)
    r = requests.post(url, headers=_auth_headers(session), json=body)
    r.raise_for_status()
    return r.json()

def trash_email(session, message_id):
    refresh_token_if_needed(session)
    url = GMAIL_TRASH_URL.format(id=message_id)
    r = requests.post(url, headers=_auth_headers(session))
    r.raise_for_status()
    return r.json()


# ═══════════════════════════════════════════════════════════════════════
# GOOGLE CALENDAR
# ═══════════════════════════════════════════════════════════════════════

def create_event(session, event_body: dict, send_updates: str = "none"):
    """
    Create a Google Calendar event.
    send_updates="all"  → Google emails a calendar invite to every attendee
    send_updates="none" → silent create, no emails sent (default, existing behaviour)
    """
    refresh_token_if_needed(session)

    # Strip the internal _send_updates key if calendar_agent added it
    body = {k: v for k, v in event_body.items() if k != "_send_updates"}

    params = {"sendUpdates": send_updates} if send_updates != "none" else {}

    r = requests.post(
        CALENDAR_EVENTS_URL,
        headers=_auth_headers(session),
        json=body,
        params=params,
    )
    r.raise_for_status()
    return r.json()


def get_events(session, max_results=10, time_min=None, time_max=None):
    refresh_token_if_needed(session)
    import pytz
    from datetime import datetime
    tz_str = os.getenv("USER_TIMEZONE", "Asia/Kolkata")  # keep IST as default so existing behavior unchanged
    tz = pytz.timezone(tz_str)
    now = datetime.now(tz)

    params = {
        "maxResults":   max_results,
        "timeMin":      time_min or now.isoformat(),
        "singleEvents": "true",
        "orderBy":      "startTime",
    }
    if time_max:
        params["timeMax"] = time_max

    r = requests.get(
        CALENDAR_EVENTS_URL,
        headers=_auth_headers(session),
        params=params,
    )
    r.raise_for_status()
    items = r.json().get("items", [])
    if not items:
        return "📅 No upcoming events."

    lines = ["Your upcoming events:"]
    for ev in items:
        title = ev.get("summary", "Untitled")
        start = ev.get("start", {})
        dt_str = start.get("dateTime") or start.get("date", "")
        try:
            import dateutil.parser
            dt = dateutil.parser.isoparse(dt_str).astimezone(tz)
            formatted = dt.strftime("%b %d, %Y at %I:%M %p")
        except Exception:
            formatted = dt_str
        lines.append(f"📅 {title} — {formatted}")
    return "\n".join(lines)


def get_event_items(session, time_min=None, time_max=None, max_results=50):
    refresh_token_if_needed(session)
    now = datetime.datetime.utcnow()
    params = {
        "maxResults": max_results,
        "timeMin": time_min or now.isoformat() + "Z",
        "singleEvents": "true",
        "orderBy": "startTime",
    }
    if time_max:
        params["timeMax"] = time_max
    r = requests.get(CALENDAR_EVENTS_URL, headers=_auth_headers(session), params=params)
    r.raise_for_status()
    return r.json().get("items", [])


def update_event(session, event_id: str, update_body: dict):
    refresh_token_if_needed(session)
    r = requests.patch(
        f"{CALENDAR_EVENTS_URL}/{event_id}",
        headers=_auth_headers(session),
        json=update_body
    )
    r.raise_for_status()
    return r.json()


def delete_event(session, event_id: str, delete_all_recurring: bool = True):
    refresh_token_if_needed(session)
    url = f"{CALENDAR_EVENTS_URL}/{event_id}"
    if delete_all_recurring:
        r = requests.get(url, headers=_auth_headers(session))
        if r.status_code == 200:
            ev = r.json()
            if ev.get("recurringEventId"):
                url = f"{CALENDAR_EVENTS_URL}/{ev['recurringEventId']}"
    r = requests.delete(url, headers=_auth_headers(session))
    if r.status_code not in (200, 204):
        r.raise_for_status()


def find_event_id_by_name(session, name: str, single_instance: bool = False, days: int = 30):
    refresh_token_if_needed(session)
    now = datetime.datetime.utcnow()
    params = {
        "maxResults": 20,
        "timeMin": now.isoformat() + 'Z',
        "timeMax": (now + datetime.timedelta(days=days)).isoformat() + 'Z',
        "singleEvents": "true",
        "orderBy": "startTime"
    }
    r = requests.get(CALENDAR_EVENTS_URL, headers=_auth_headers(session), params=params)
    r.raise_for_status()
    for ev in r.json().get("items", []):
        if name.lower() in ev.get("summary", "").lower():
            is_recurring = bool(ev.get("recurringEventId"))
            if single_instance:
                return ev["id"], is_recurring
            return ev.get("recurringEventId", ev["id"]), is_recurring
    return None, False


def find_all_event_instances_by_name(session, name: str, days: int = 7):
    refresh_token_if_needed(session)
    now = datetime.datetime.utcnow()
    params = {
        "maxResults": 100,
        "timeMin": now.isoformat() + 'Z',
        "timeMax": (now + datetime.timedelta(days=days)).isoformat() + 'Z',
        "singleEvents": "true",
        "orderBy": "startTime"
    }
    r = requests.get(CALENDAR_EVENTS_URL, headers=_auth_headers(session), params=params)
    r.raise_for_status()
    return [
        event["id"]
        for event in r.json().get("items", [])
        if name.lower() in event.get("summary", "").lower()
    ]


# ═══════════════════════════════════════════════════════════════════════
# GOOGLE TASKS
# ═══════════════════════════════════════════════════════════════════════

def create_task(session, title, notes=None, due=None, list_id="@default"):
    refresh_token_if_needed(session)
    body = {"title": title}
    if notes: body["notes"] = notes
    if due:   body["due"]   = due
    r = requests.post(f"{TASKS_URL}/lists/{list_id}/tasks",
                      headers=_auth_headers(session), json=body)
    r.raise_for_status()
    return r.json()


def get_tasks(session, list_id="@default", show_completed=False):
    refresh_token_if_needed(session)
    r = requests.get(
        f"{TASKS_URL}/lists/{list_id}/tasks",
        headers=_auth_headers(session),
        params={"showCompleted": str(show_completed).lower(), "maxResults": 20}
    )
    r.raise_for_status()
    return r.json().get("items", [])


def complete_task(session, task_id, list_id="@default"):
    refresh_token_if_needed(session)
    r = requests.patch(f"{TASKS_URL}/lists/{list_id}/tasks/{task_id}",
                       headers=_auth_headers(session),
                       json={"status": "completed"})
    r.raise_for_status()
    return r.json()


# ═══════════════════════════════════════════════════════════════════════
# GOOGLE DOCS
# ═══════════════════════════════════════════════════════════════════════

def create_doc(session, title):
    refresh_token_if_needed(session)
    r = requests.post(DOCS_URL, headers=_auth_headers(session), json={"title": title})
    r.raise_for_status()
    return r.json()


def append_text_to_doc(session, doc_id, text):
    refresh_token_if_needed(session)
    body = {"requests": [{
        "insertText": {"text": text, "endOfSegmentLocation": {"segmentId": ""}}
    }]}
    r = requests.post(f"{DOCS_URL}/{doc_id}:batchUpdate",
                      headers=_auth_headers(session), json=body)
    r.raise_for_status()
    return r.json()


def read_doc(session, doc_id):
    refresh_token_if_needed(session)
    r = requests.get(f"{DOCS_URL}/{doc_id}", headers=_auth_headers(session))
    r.raise_for_status()
    return r.json()


def find_doc_id_by_name(session, name):
    """
    Find a Google Doc by name with fuzzy matching.
    Handles cases like "hello world doc" finding a doc named "hello world".

    Search order:
      1. Exact match on cleaned name (noise words like "doc" stripped)
      2. Drive 'contains' search on cleaned name
      3. All-keywords match (every word in the search must appear in the filename)
    """
    refresh_token_if_needed(session)

    # Strip noise words the AI or user might append
    _NOISE = [" document", " google doc", " google document", " doc", " file"]
    clean = name.strip()
    clean_lower = clean.lower()
    for noise in _NOISE:
        if clean_lower.endswith(noise):
            clean = clean[: -len(noise)].strip()
            clean_lower = clean.lower()
            break

    base_filter = "mimeType='application/vnd.google-apps.document' and trashed=false"

    def _search(name_q: str):
        q = f"{name_q} and {base_filter}"
        r = requests.get(
            DRIVE_URL,
            headers=_auth_headers(session),
            params={"q": q, "fields": "files(id,name)", "pageSize": 20}
        )
        r.raise_for_status()
        return r.json().get("files", [])

    # ── 1. Exact match on cleaned name ───────────────────────────────
    files = _search(f"name = '{clean}'")
    if files:
        return files[0]["id"]

    # ── 2. Drive 'contains' on cleaned name ──────────────────────────
    files = _search(f"name contains '{clean}'")
    if files:
        return files[0]["id"]

    # ── 3. Original name (in case cleaning was wrong) ─────────────────
    if clean.lower() != name.strip().lower():
        files = _search(f"name contains '{name.strip()}'")
        if files:
            return files[0]["id"]

    # ── 4. All-keywords match ─────────────────────────────────────────
    keywords = [w for w in clean_lower.split() if len(w) > 2]
    if keywords:
        files = _search(f"name contains '{keywords[0]}'")
        for f in files:
            if all(kw in f["name"].lower() for kw in keywords):
                return f["id"]

    return None


# ═══════════════════════════════════════════════════════════════════════
# GOOGLE SHEETS — GENERAL
# ═══════════════════════════════════════════════════════════════════════

def _find_sheet_by_name(session, name: str):
    refresh_token_if_needed(session)
    query = (
        f"name = '{name}' "
        f"and mimeType = 'application/vnd.google-apps.spreadsheet' "
        f"and trashed = false"
    )
    r = requests.get(
        DRIVE_URL,
        headers=_auth_headers(session),
        params={"q": query, "fields": "files(id,name)"}
    )
    r.raise_for_status()
    files = r.json().get("files", [])
    return files[0]["id"] if files else None


def sheets_create_spreadsheet(session, title: str) -> str:
    refresh_token_if_needed(session)
    body = {
        "properties": {"title": title},
        "sheets": [{"properties": {"title": "Sheet1"}}]
    }
    r = requests.post(SHEETS_URL, headers=_auth_headers(session), json=body)
    r.raise_for_status()
    sheet_id = r.json()["spreadsheetId"]
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}"
    return f"✅ Spreadsheet '{title}' created! URL: {url}"


def sheets_read_range(session, sheet_name: str, range_: str = "Sheet1") -> str:
    refresh_token_if_needed(session)
    sheet_id = _find_sheet_by_name(session, sheet_name)
    if not sheet_id:
        return f"❌ Sheet '{sheet_name}' not found."
    r = requests.get(
        f"{SHEETS_URL}/{sheet_id}/values/{range_}",
        headers=_auth_headers(session)
    )
    r.raise_for_status()
    values = r.json().get("values", [])
    if not values:
        return f"📊 Sheet '{sheet_name}' is empty."
    rows = [" | ".join(str(cell) for cell in row) for row in values]
    return f"📊 '{sheet_name}' ({range_}):\n\n" + "\n".join(rows)


def sheets_write_range(session, sheet_name: str, range_: str, values: list) -> str:
    refresh_token_if_needed(session)
    sheet_id = _find_sheet_by_name(session, sheet_name)
    if not sheet_id:
        return f"❌ Sheet '{sheet_name}' not found."
    r = requests.put(
        f"{SHEETS_URL}/{sheet_id}/values/{range_}",
        headers=_auth_headers(session),
        params={"valueInputOption": "USER_ENTERED"},
        json={"values": values}
    )
    r.raise_for_status()
    return f"✅ Written to '{sheet_name}' at {range_}."


def _get_first_tab_name(session, sheet_id: str) -> str:
    """Return the actual title of the first tab in a spreadsheet, with 'Sheet1' as fallback."""
    try:
        r = requests.get(
            f"{SHEETS_URL}/{sheet_id}",
            headers=_auth_headers(session),
            params={"fields": "sheets.properties.title"},
            timeout=10
        )
        r.raise_for_status()
        sheets = r.json().get("sheets", [])
        if sheets:
            return sheets[0]["properties"]["title"]
    except Exception:
        pass
    return "Sheet1"


def sheets_append_row(session, sheet_name: str, row_data: list) -> str:
    refresh_token_if_needed(session)
    sheet_id = _find_sheet_by_name(session, sheet_name)
    if not sheet_id:
        return f"❌ Sheet '{sheet_name}' not found."
    tab_name = _get_first_tab_name(session, sheet_id)
    r = requests.post(
        f"{SHEETS_URL}/{sheet_id}/values/{tab_name}:append",
        headers=_auth_headers(session),
        params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
        json={"values": [row_data]}
    )
    r.raise_for_status()
    return f"✅ Row added to '{sheet_name}'."


def sheets_clear_range(session, sheet_name: str, range_: str) -> str:
    refresh_token_if_needed(session)
    sheet_id = _find_sheet_by_name(session, sheet_name)
    if not sheet_id:
        return f"❌ Sheet '{sheet_name}' not found."
    r = requests.post(
        f"{SHEETS_URL}/{sheet_id}/values/{range_}:clear",
        headers=_auth_headers(session)
    )
    r.raise_for_status()
    return f"✅ Cleared '{range_}' in '{sheet_name}'."


def sheets_get_sheet_names(session, sheet_name: str) -> str:
    refresh_token_if_needed(session)
    sheet_id = _find_sheet_by_name(session, sheet_name)
    if not sheet_id:
        return f"❌ Sheet '{sheet_name}' not found."
    r = requests.get(
        f"{SHEETS_URL}/{sheet_id}",
        headers=_auth_headers(session),
        params={"fields": "sheets.properties.title"}
    )
    r.raise_for_status()
    tabs = [s["properties"]["title"] for s in r.json().get("sheets", [])]
    return f"📊 Tabs in '{sheet_name}': {', '.join(tabs)}"


# ═══════════════════════════════════════════════════════════════════════
# TOKEN VALIDATION & REFRESH
# ═══════════════════════════════════════════════════════════════════════

def validate_google_token(session) -> dict:
    """
    Validates the Google OAuth token. Returns:
    {
      "valid": bool,
      "expires_at": float (unix timestamp),
      "expires_in_seconds": float,
      "error": str or None,
      "needs_refresh": bool
    }
    """
    if not session or "tokens" not in session:
        return {"valid": False, "error": "No session tokens", "needs_refresh": False}

    tokens = session.get("tokens", {})
    now = time.time()
    expires_at = tokens.get("expires_at", 0)
    expires_in = expires_at - now

    # Token expires in less than 5 minutes — needs refresh
    if expires_in < 300:
        return {
            "valid": False,
            "expires_at": expires_at,
            "expires_in_seconds": expires_in,
            "needs_refresh": True,
            "error": f"Token expires in {expires_in:.0f}s"
        }

    # Try a quick API call to validate token is actually working
    try:
        r = requests.get(
            "https://www.googleapis.com/drive/v3/about?fields=user",
            headers=_auth_headers(session),
            timeout=5
        )
        if r.status_code == 200:
            return {
                "valid": True,
                "expires_at": expires_at,
                "expires_in_seconds": expires_in,
                "needs_refresh": False,
                "error": None
            }
        elif r.status_code == 401:
            return {
                "valid": False,
                "expires_at": expires_at,
                "expires_in_seconds": expires_in,
                "needs_refresh": True,
                "error": "Token rejected by Google (401)"
            }
    except Exception as e:
        return {
            "valid": False,
            "expires_at": expires_at,
            "expires_in_seconds": expires_in,
            "needs_refresh": True,
            "error": f"Validation request failed: {e}"
        }

    return {
        "valid": False,
        "expires_at": expires_at,
        "expires_in_seconds": expires_in,
        "needs_refresh": True,
        "error": "Unknown validation error"
    }


def force_refresh_google_token(session) -> bool:
    """
    Forces a token refresh even if it's not expired.
    Returns True if successful, False otherwise.
    """
    try:
        tokens = session.get("tokens", {})
        if not tokens.get("refresh_token"):
            return False

        resp = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": session.get("client_id", ""),
                "client_secret": session.get("client_secret", ""),
                "refresh_token": tokens["refresh_token"],
                "grant_type": "refresh_token",
            },
            timeout=10
        )

        if resp.status_code != 200:
            print(f"[Token Refresh] Failed: {resp.text}")
            return False

        new = resp.json()
        tokens["access_token"] = new["access_token"]
        tokens["expires_at"] = time.time() + int(new.get("expires_in", 3600))
        if "refresh_token" in new:
            tokens["refresh_token"] = new["refresh_token"]

        print("[Token Refresh] ✅ Token refreshed successfully")
        return True
    except Exception as e:
        print(f"[Token Refresh] ❌ Error: {e}")
        return False