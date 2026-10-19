# gmail_agent.py
import json
import re
import os
from pathlib import Path
from datetime import datetime, timedelta
from ai_backend import _call_ollama_chat, call_ollama_raw
from tools import (
    send_email, create_draft, get_emails, get_labels, modify_labels,
    get_email_thread_id, reply_email,
    sheets_find_contact, search_web
)

GMAIL_SYSTEM_PROMPT = """You are an intelligent Gmail assistant. Parse ANY email-related request and output ONLY valid JSON.

ACTIONS:
send_email(to_name, subject, body_html)
create_draft(to_name, subject, body_html)
reply_email(to_name, body_html)
get_emails(query?, max_results?)
search_emails(query, max_results?)
archive_email(query)
delete_email(query)

OUTPUT FORMAT:
{"action":"action_name","data":{"field":"value"}}

INTELLIGENCE RULES:
- "send / mail / email to X saying Y" → send_email. Generate a PROPER subject from the content.
- "draft / just draft / don't send / save draft" → create_draft. NEVER send_email.
- "reply to X / respond to X" → reply_email. NEVER send_email.
- "check inbox / read emails / show emails / any new emails" → get_emails
- "search for emails about X / find emails from X" → get_emails with query
- If body needs CURRENT info (news, prices, scores, weather, results, match, movie, stocks, war, politics, world events) → body_html = "SEARCH_WEB: <specific search query>"
- Generate intelligent subjects — don't just copy the message. "saying I will be late" → subject "Running Late"
- For casual messages, keep body professional but friendly
- For news/info emails, use SEARCH_WEB to get current information
- PIPED DATA: If the prompt contains "[CONTEXT DATA FROM PREVIOUS STEP]", do NOT try to write it out yourself. You MUST output EXACTLY the literal string "[PIPED_DATA]" inside the `body_html` field!
SUBJECT INTELLIGENCE:
- "saying hello / hi / hey" → subject: "Hello"
- "saying I will be late" → subject: "Running Late"
- "saying I will be 10-15 minutes late for the meeting" → subject: "Running Late for Meeting"
- "saying the project is done" → subject: "Project Complete"
- "saying happy birthday" → subject: "Happy Birthday"
- "about the ukraine war" → subject: "Ukraine War Update" + SEARCH_WEB
- "about ipl results" → subject: "IPL Match Results" + SEARCH_WEB
- "about bitcoin price" → subject: "Bitcoin Price Update" + SEARCH_WEB
- "about current status of us iran war" → subject: "US-Iran Conflict Update" + SEARCH_WEB

QUERY INTELLIGENCE for get_emails, archive_email, and delete_email:
- "from X" → query: "from:X"
- "about X" or "with X" → query: "X"
- "unread" → query: "is:unread"
- "latest email" or "last email" (singular) → query: "", max_results: 1
- "latest emails" or "my emails" or "recent emails" (plural) → query: "", max_results: 5
- "latest email with X" or "latest email about X" → query: "X", max_results: 1
- "last 5" → max_results: 5
- no filter → max_results: 5

EXAMPLES:
User: what are my latest emails
{"steps": [{"action":"get_emails","data":{"query":"","max_results":5}}]}

User: read my latest email with the meeting notes
{"steps": [{"action":"get_emails","data":{"query":"meeting notes","max_results":1}}]}

User: archive my latest email
{"steps": [{"action":"archive_email","data":{"query":""}}]}

User: delete the latest email from manish
{"steps": [{"action":"delete_email","data":{"query":"from:manish"}}]}

User: send an email to manish saying hello
{"steps": [{"action":"send_email","data":{"to_name":"Manish","subject":"Hello","body_html":"<p>Hello Manish!</p>"}}]}

User: send an email to manish saying hi how are you
{"steps": [{"action":"send_email","data":{"to_name":"Manish","subject":"Hi","body_html":"<p>Hi Manish, how are you?</p>"}}]}

User: send an email to manish saying I will be late today
{"steps": [{"action":"send_email","data":{"to_name":"Manish","subject":"Running Late","body_html":"<p>Hi Manish, I will be late today. Sorry for the inconvenience.</p>"}}]}

User: send an email to manish about the current status of us iran war
{"steps": [{"action":"send_email","data":{"to_name":"Manish","subject":"US-Iran Conflict Update","body_html":"SEARCH_WEB: current status of US Iran war 2026"}}]}

User: email sara about the latest ipl match results
{"steps": [{"action":"send_email","data":{"to_name":"Sara","subject":"IPL Match Results","body_html":"SEARCH_WEB: IPL match results today 2026"}}]}

User: send manish the latest bitcoin price info
{"steps": [{"action":"send_email","data":{"to_name":"Manish","subject":"Bitcoin Price Update","body_html":"SEARCH_WEB: bitcoin price today 2026"}}]}

User: draft an email to john saying I reviewed the document
{"steps": [{"action":"create_draft","data":{"to_name":"John","subject":"Document Review","body_html":"<p>Hi John, I have reviewed the document and everything looks good.</p>"}}]}

User: reply to manish saying I will be there
{"steps": [{"action":"reply_email","data":{"to_name":"Manish","body_html":"<p>I will be there!</p>"}}]}

User: check my inbox
{"steps": [{"action":"get_emails","data":{"max_results":10}}]}

User: check my last 5 emails
{"steps": [{"action":"get_emails","data":{"max_results":5}}]}

User: show me unread emails
{"steps": [{"action":"get_emails","data":{"query":"is:unread","max_results":10}}]}

User: find emails from manish
{"steps": [{"action":"get_emails","data":{"query":"from:manish","max_results":10}}]}

User: [CONTEXT DATA FROM PREVIOUS STEP]
(Some very long text here)
Task: email this to manish
{"steps": [{"action":"send_email","data":{"to_name":"Manish","subject":"Document Data","body_html":"<p>Here is the document data you requested:</p><p>[PIPED_DATA]</p>"}}]}"""


def _parse_json(text: str) -> dict:
    text = text.strip()
    if "```" in text:
        parts = text.split("```")
        for part in parts:
            p = part.strip()
            if p.startswith("json"):
                p = p[4:].strip()
            if p.startswith("{"):
                text = p
                break
    s, e = text.find("{"), text.rfind("}")
    if s != -1 and e != -1:
        text = text[s:e + 1]
    try:
        return json.loads(text)
    except Exception:
        return {}


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


class GmailAgent:
    def __init__(self, session: dict):
        self.session = session
        self.user_email = session["user"]["email"]

    def run(self, query: str, status_callback=None, piped_context: str = None):
        if status_callback: status_callback("Analyzing Email Command...")
        piped_context = ""
        if "[CONTEXT DATA FROM PREVIOUS STEP" in query:
            parts = query.split("Task:")
            if len(parts) > 1:
                piped_context = parts[0].split("]\n")[-1].strip()

        raw = _call_ollama_chat(system=GMAIL_SYSTEM_PROMPT, user=query + "\nJSON:")
        if not raw:
            return "❌ AI returned empty response."
        parsed = _parse_json(raw) if isinstance(raw, str) else raw
        plan = _fix_wrapper(parsed)
        
        # Support both single action and steps array
        steps = plan.get("steps", [plan]) if "steps" in plan else [plan]
        
        results = []
        for step in steps:
            action = step.get("action", "")
            data = step.get("data", {})
            
            has_attached_file = bool(re.search(r'\[ATTACHMENT_FILE\]:', query or ""))
            if piped_context and "body_html" in data and not has_attached_file:
                clean_context = re.sub(r'\[ATTACHMENT_FILE\]:[^\r\n]*', '', piped_context)
                clean_context = re.sub(r'\[FILE_NAME\]:[^\r\n]*', '', clean_context)
                clean_context = re.sub(r'\[FILE_SIZE\]:[^\r\n]*', '', clean_context)
                clean_context = clean_context.strip()
                if clean_context:
                    formatted_context = clean_context.replace("\n", "<br>")
                    if "[PIPED_DATA]" in data["body_html"]:
                        data["body_html"] = data["body_html"].replace("[PIPED_DATA]", formatted_context)
                    else:
                        data["body_html"] += f"<br><br>{formatted_context}"
                else:
                    data["body_html"] = data["body_html"].replace("[PIPED_DATA]", "")
            elif has_attached_file and "body_html" in data:
                data["body_html"] = data["body_html"].replace("[PIPED_DATA]", "")

            if "body_html" in data and isinstance(data["body_html"], str):
                data["body_html"] = re.sub(r'\[ATTACHMENT_FILE\]:[^\r\n]*', '', data["body_html"])
                data["body_html"] = data["body_html"].replace("[ATTACHMENT_FILE]", "")
                data["body_html"] = data["body_html"].replace("[PIPED_DATA]", "").strip()

            local_opts = self._extract_local_options(query)
            for k, v in local_opts.items():
                data.setdefault(k, v)
                
            if not action:
                results.append("❌ Could not understand the email command.")
                continue
                
            if action == "send_email":
                if status_callback: status_callback(f"Drafting email to {data.get('to_name', data.get('to', ''))}")
                res = self._send(data, confirm=True, status_callback=status_callback)
                if isinstance(res, dict): return res
                results.append(res)
            elif action == "create_draft":
                if status_callback: status_callback("Creating draft...")
                res = self._draft(data)
                if isinstance(res, dict): return res
                results.append(res)
            elif action == "reply_email":
                if status_callback: status_callback("Preparing reply...")
                res = self._reply(data, confirm=True)
                if isinstance(res, dict): return res
                results.append(res)
            elif action in ("get_emails", "search_emails"):
                if status_callback: status_callback("Searching inbox...")
                results.append(self._get(data, query))
            elif action in ("archive_email", "delete_email"):
                if status_callback: status_callback("Processing email action...")
                results.append(self._archive_or_delete(action, data))
            else:
                results.append(f"❌ Unknown Gmail action: {action}")
                
        return "\n".join(results) if results else "❌ No actions taken."

    def _resolve(self, name: str):
        matches = sheets_find_contact(self.session, name)
        if not matches:
            return None, None
        return matches[0][0], matches[0][1]

    def _documents_dir(self) -> Path:
        configured = os.getenv("WINVOICE_DOCUMENTS_DIR", "").strip()
        if configured:
            return Path(configured).expanduser()
        return Path.home() / "Documents"

    def _find_attachment(self, file_name: str) -> str | None:
        root = self._documents_dir()
        if root.exists():
            direct = root / file_name
            if direct.is_file():
                return str(direct)
            matches = list(root.rglob(file_name))
            if matches:
                return str(matches[0])
        # Smart fallback: use FilesAgent to search recent files and user folders
        try:
            from files_agent import FilesAgent
            res = FilesAgent().search_file(file_name)
            if res and res.get("file_path"):
                return res["file_path"]
        except Exception:
            pass
        return None

    def _extract_local_options(self, query: str) -> dict:
        opts = {}
        # Strip injected user context / task header before checking local options
        clean_query = query or ""
        if "[TASK]" in clean_query:
            clean_query = clean_query.split("[TASK]")[-1]
        if "[User Context" in clean_query:
            clean_query = re.sub(r'\[User Context[^\]]*\]', '', clean_query)
        if "[CONTEXT DATA" in clean_query:
            clean_query = re.sub(r'\[CONTEXT DATA[^\]]*\]', '', clean_query)

        files = re.findall(r'["\']([^"\']+\.[A-Za-z0-9]{2,8})["\']', clean_query)
        files += re.findall(r'\b([\w .-]+\.(?:pdf|docx?|xlsx?|pptx?|csv|txt|png|jpe?g|zip))\b', clean_query, re.I)
        attachments = []
        for f in dict.fromkeys(x.strip() for x in files if x.strip()):
            found = self._find_attachment(f)
            if found:
                attachments.append(found)

        # Check for explicit [ATTACHMENT_FILE]: path piped from previous step (FilesAgent)
        raw_query = query or ""
        piped_attachments = re.findall(r'\[ATTACHMENT_FILE\]:\s*([^\r\n]+)', raw_query)
        for p in piped_attachments:
            p_clean = p.strip()
            if os.path.isfile(p_clean) and p_clean not in attachments:
                attachments.append(p_clean)

        if attachments:
            opts["attachments"] = attachments

        scheduled = self._parse_schedule(clean_query)
        if scheduled:
            opts["scheduled_send_at"] = scheduled
        return opts

    def _parse_schedule(self, query: str) -> float | None:
        q = (query or "").lower().strip()
        # If the user says "saying ...", "with body ...", "message ...", "that ...",
        # any time mentioned inside the message is message content, NOT a send schedule!
        # Example: "send an email to manish saying we have a meeting today at 5pm" -> time is in message body!
        instruction_part = re.split(r'\b(saying|with body|message|body|that)\b', q, maxsplit=1)[0].strip()

        # The user must explicitly request scheduling/delayed delivery in the instruction
        # e.g., "schedule an email...", "send an email at 5pm to...", "schedule to send...", "send later at 5pm"
        has_schedule_intent = (
            bool(re.search(r'\b(schedule|delay|later)\b', instruction_part)) or
            bool(re.search(r'\b(send|deliver)\b.*\b(at|by)\s+\d{1,2}(:\d{2})?\s*(am|pm)\b', instruction_part))
        )
        if not has_schedule_intent:
            return None

        m = re.search(r'\b(?:at|by|for)?\s*(\d{1,2})(?:[:\s]+(\d{2}))?\s*(am|pm)\b', instruction_part)
        if not m:
            return None

        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        mer = m.group(3)
        if mer == "pm" and hour != 12:
            hour += 12
        if mer == "am" and hour == 12:
            hour = 0
        now = datetime.now()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if "tomorrow" in instruction_part or target <= now:
            target += timedelta(days=1)
        return target.timestamp()

    def _handle_search(self, body: str, to_name: str, subject: str, status_callback=None) -> str | None:
        """
        Handles SEARCH_WEB: prefix. Now routes through general_agent
        for up-to-date, accurate web information.
        """
        if not body or not body.startswith("SEARCH_WEB:"):
            return body

        topic = body.replace("SEARCH_WEB:", "").strip()
        print(f"   🌍 Getting current info via GeneralAgent: '{topic}'...")
        if status_callback: status_callback("Fetching current web info...")

        # Use general_agent for current web info
        try:
            from general_agent import get_current_info
            info = get_current_info(topic, purpose=f"for an email to {to_name} with subject '{subject}'")
        except Exception:
            # Fallback to direct search
            info = search_web(topic)
            if not info or "No search results" in info or "Search failed" in info:
                return None

        if not info:
            return None

        print("   ✍️ Writing email body from current info...")
        if status_callback: status_callback("Writing email body...")
        prompt = (
            f"You are a professional secretary. Write a SHORT professional email in HTML.\n\n"
            f"RECIPIENT: {to_name}\nSUBJECT: {subject}\n\n"
            f"CURRENT INFORMATION TO INCLUDE:\n{info}\n\n"
            f"RULES:\n"
            f"- Return ONLY HTML starting with <p>\n"
            f"- Use only <p> tags\n"
            f"- 3-4 paragraphs max\n"
            f"- Professional tone\n"
            f"- Include the specific current information provided above\n"
            f"- Do NOT use placeholder text or say 'information not available'\n"
            f"- Start with a brief greeting to {to_name}"
        )
        body = call_ollama_raw(prompt).strip()
        if body.startswith("```"):
            lines = body.splitlines()[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            body = "\n".join(lines).strip()
        return body

    def _send(self, data: dict, confirm: bool = False, status_callback=None):
        to_name, to_addr = self._resolve(data.get("to_name", ""))
        if not to_addr:
            return f"❌ Contact not found: '{data.get('to_name')}'. Add them to contacts first."
        subject = data.get("subject", "")
        body = data.get("body_html", "")
        body = self._handle_search(body, to_name, subject, status_callback)
        if body is None:
            return f"❌ Could not get current information. Email not sent."
        if not subject:
            subject = "Message from WinVoice"
        if not body:
            return "❌ Email body is empty."
        if confirm:
            attachments = data.get("attachments", [])
            attachments_info = []
            for a in attachments:
                if os.path.isfile(a):
                    try:
                        from files_agent import format_size
                        attachments_info.append({"name": os.path.basename(a), "size": format_size(os.path.getsize(a)), "path": a})
                    except Exception:
                        attachments_info.append({"name": os.path.basename(a), "path": a})
            return {
                "pending_confirmation": True,
                "type": "schedule_send" if data.get("scheduled_send_at") else "send",
                "to": to_name,
                "to_addr": to_addr,
                "subject": subject,
                "body_html": body,
                "body_preview": re.sub(r'<[^>]+>', '', body)[:400],
                "attachments": attachments,
                "attachments_info": attachments_info,
                "scheduled_send_at": data.get("scheduled_send_at"),
            }
        if data.get("scheduled_send_at"):
            from state_store import add_scheduled_email
            send_id = add_scheduled_email(
                to_name, to_addr, subject, body,
                data.get("attachments", []),
                float(data["scheduled_send_at"]),
            )
            when = datetime.fromtimestamp(float(data["scheduled_send_at"])).strftime("%b %d at %I:%M %p")
            return f"✅ Email scheduled for {when} — Queue #{send_id}"
        send_email(self.session, self.user_email, to_addr, subject, body, attachments=data.get("attachments", []))
        return f"✅ Email sent to {to_name} — Subject: \"{subject}\""

    def _draft(self, data: dict):
        to_name, to_addr = self._resolve(data.get("to_name", ""))
        if not to_addr:
            return f"❌ Contact not found: '{data.get('to_name')}'. Add them to contacts first."
        subject = data.get("subject", "")
        body = data.get("body_html", "")
        body = self._handle_search(body, to_name, subject)
        if body is None:
            return f"❌ Could not get current information. Draft not created."
        if not subject:
            subject = "Draft"
        if not body:
            return "❌ Draft body is empty."
        create_draft(self.session, self.user_email, to_addr, subject, body, attachments=data.get("attachments", []))
        return f"✅ Draft saved — To: {to_name}, Subject: \"{subject}\""

    def _reply(self, data: dict, confirm: bool = False):
        to_name, to_addr = self._resolve(data.get("to_name", ""))
        if not to_addr:
            return f"❌ Contact not found: '{data.get('to_name')}'. Add them to contacts first."
        body = data.get("body_html", "<p>Got it, thanks!</p>")
        result = get_email_thread_id(self.session, to_name)
        thread_id, message_id_header, original_subject = result
        if not thread_id:
            return f"❌ No recent email from {to_name} found to reply to."
        if confirm:
            clean = re.sub(r'<[^>]+>', '', body)[:400]
            return {
                "pending_confirmation": True,
                "type": "reply",
                "to": to_name,
                "to_addr": to_addr,
                "subject": original_subject,
                "body_html": body,
                "body_preview": clean,
                "thread_id": thread_id,
                "message_id_header": message_id_header,
            }
        reply_email(self.session, self.user_email, to_addr, original_subject, body, thread_id, message_id_header)
        return f"✅ Reply sent to {to_name}"

    def execute_pending(self, pending: dict):
        t = pending.get("type")
        if t == "send":
            send_email(
                self.session, self.user_email, pending["to_addr"],
                pending["subject"], pending["body_html"],
                attachments=pending.get("attachments", []),
            )
            return f"✅ Email sent to {pending['to']} — Subject: \"{pending['subject']}\""
        elif t == "schedule_send":
            from state_store import add_scheduled_email
            send_id = add_scheduled_email(
                pending.get("to", ""), pending["to_addr"], pending["subject"],
                pending["body_html"], pending.get("attachments", []),
                float(pending["scheduled_send_at"]),
            )
            when = datetime.fromtimestamp(float(pending["scheduled_send_at"])).strftime("%b %d at %I:%M %p")
            return f"✅ Email scheduled for {when} — Queue #{send_id}"
        elif t == "reply":
            reply_email(
                self.session, self.user_email, pending["to_addr"],
                pending["subject"], pending["body_html"],
                pending["thread_id"], pending["message_id_header"]
            )
            return f"✅ Reply sent to {pending['to']}"
        return "❌ Unknown pending type."
    def _get(self, data: dict, original_query: str = ""):
        try:
            result = get_emails(self.session, query=data.get("query"), max_results=data.get("max_results", 10))
            # AI-summarize each email's snippet in a single batch call
            try:
                import re as _re
                from ai_backend import call_ollama_raw
                blocks = result.split("\n\n")
                email_blocks = [b for b in blocks if "📧" in b]
                if not email_blocks:
                    return result
                # Extract raw snippets from each email block
                snippets = []
                for block in email_blocks:
                    m = _re.search(r"_(.+?)_", block, _re.DOTALL)
                    snippets.append(m.group(1).strip() if m else "")
                # Single AI call: summarize all snippets in one shot
                numbered_input = "\n".join(
                    f"{i+1}. {s[:200]}" for i, s in enumerate(snippets) if s.strip()
                )
                if not numbered_input.strip():
                    return result
                raw_summaries = call_ollama_raw(
                    f"Summarize each of the following email previews in ONE short sentence "
                    f"(max 12 words each). Output ONLY numbered lines, format exactly: "
                    f"1. Summary here\n2. Summary here\n\n{numbered_input}"
                )
                # Parse numbered summaries
                summary_map = {}
                for line in (raw_summaries or "").strip().splitlines():
                    m = _re.match(r"^(\d+)\.\s+(.+)", line.strip())
                    if m:
                        summary_map[int(m.group(1)) - 1] = m.group(2).strip()
                # Inject summaries back into blocks, preserving the card format
                email_idx = 0
                final_blocks = []
                for block in blocks:
                    if "📧" in block:
                        if email_idx in summary_map and summary_map[email_idx]:
                            block = _re.sub(
                                r"_(.+?)_",
                                f"_{summary_map[email_idx]}_",
                                block,
                                flags=_re.DOTALL,
                            )
                        email_idx += 1  # always increment, moved outside if
                    final_blocks.append(block)
                return "\n\n".join(final_blocks)
            except Exception:
                return result  # fallback to raw snippets if AI fails
        except Exception as e:
            return f"❌ Failed to fetch emails: {e}"

    def _archive_or_delete(self, action: str, data: dict):
        try:
            import requests
            from tools import GMAIL_MESSAGES_URL, _auth_headers, modify_labels, trash_email
            
            # 1. Fetch the target email
            query = data.get("query", "")
            params = {"maxResults": 1}
            if query:
                params["q"] = query
            r = requests.get(GMAIL_MESSAGES_URL, headers=_auth_headers(self.session), params=params)
            r.raise_for_status()
            res = r.json()
            messages = res.get("messages", [])
            if not messages:
                return f"❌ No emails found to {action.replace('_email', '')}."
            
            msg_id = messages[0]["id"]
            
            # Fetch the actual email details to show the user
            r2 = requests.get(f"{GMAIL_MESSAGES_URL}/{msg_id}", headers=_auth_headers(self.session), params={"format": "metadata", "metadataHeaders": ["From", "Subject"]})
            r2.raise_for_status()
            msg_data = r2.json()
            
            snippet = msg_data.get("snippet", "No preview available")
            subject = "(No Subject)"
            sender = "Unknown Sender"
            
            for h in msg_data.get("payload", {}).get("headers", []):
                if h["name"].lower() == "subject": subject = h["value"]
                if h["name"].lower() == "from": sender = h["value"]

            # Return a pending_confirmation payload
            # The backend will check prefs.get("ask_before_deleting_emails", True)
            return {
                "pending_confirmation": True,
                "type": action,
                "message_id": msg_id,
                "subject": subject,
                "from": sender,
                "snippet": snippet
            }
        except Exception as e:
            return f"❌ Failed to process {action}: {e}"