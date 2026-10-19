# contacts_agent.py
import json
import re
from ai_backend import _call_ollama_chat
from tools import (
    sheets_add_contact, sheets_get_all_contacts,
    sheets_find_contact, sheets_delete_contact,
    sheets_update_contact_fields,
)

CONTACTS_SYSTEM_PROMPT = """You are an intelligent Contacts assistant. Parse ANY contact request and output ONLY valid JSON.

ACTIONS:
add_contact(name, email)       - add a new contact (email required)
get_contacts()                 - list all contacts
delete_contact(name)           - delete a contact by name

OUTPUT FORMAT:
{"action":"action_name","data":{"field":"value"}}

INTELLIGENCE RULES:
- "add/save/store/new contact X email Y" → add_contact with email
- "what/show/list/my contacts" → get_contacts
- "delete/remove contact X" → delete_contact
- Name: capitalize first letter
- email: must be a valid email address

EXAMPLES:
User: add contact john with email john@gmail.com
{"steps": [{"action":"add_contact","data":{"name":"John","email":"john@gmail.com"}}]}

User: add contact sara with email sara@gmail.com
{"steps": [{"action":"add_contact","data":{"name":"Sara","email":"sara@gmail.com"}}]}

User: save manish as a contact with email manish@gmail.com
{"steps": [{"action":"add_contact","data":{"name":"Manish","email":"manish@gmail.com"}}]}

User: what contacts do I have
{"steps": [{"action":"get_contacts","data":{}}]}

User: show me my contacts
{"steps": [{"action":"get_contacts","data":{}}]}

User: list all contacts
{"steps": [{"action":"get_contacts","data":{}}]}

User: delete contact john
{"steps": [{"action":"delete_contact","data":{"name":"John"}}]}

User: remove sara from contacts
{"action":"delete_contact","data":{"name":"Sara"}}"""


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


class ContactsAgent:
    def __init__(self, session: dict):
        self.session = session

    def run(self, query: str, status_callback=None, piped_context: str = None) -> str:
        raw = _call_ollama_chat(system=CONTACTS_SYSTEM_PROMPT, user=query + "\nJSON:")
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

            if action == "add_contact":
                res = self._add(data)
                if isinstance(res, dict): return res
                results.append(res)
            elif action == "get_contacts":
                results.append(self._get(query))
            elif action == "delete_contact":
                results.append(self._delete(data))
            else:
                results.append(f"❌ Unknown contacts action: {action}")
                
        return "\n\n".join(results) if results else "❌ Could not understand the contacts command."

    def _add(self, data: dict) -> str:
        name  = data.get("name", "").strip()
        email = data.get("email", "").strip()
        if not name:
            return "❌ Contact name is required."
        if not email:
            return "❌ An email address is required."
        if not re.match(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            return f"❌ Invalid email address: {email}"
        try:
            result = sheets_add_contact(self.session, name, email)

            # ── Duplicate name detected — show confirmation dialog ────
            if isinstance(result, dict) and result.get("conflict"):
                return self._handle_conflict(result)

            return result
        except Exception as e:
            return f"❌ Failed to save contact: {e}"

    def _handle_conflict(self, conflict: dict) -> str:
        import tkinter as tk
        from tkinter import messagebox

        existing  = conflict["existing"]
        incoming  = conflict["incoming"]
        row_num   = conflict["row_number"]

        ex_lines = [f"Name:   {existing['name']}"]
        if existing["email"]:
            ex_lines.append(f"Email:  {existing['email']}")
        else:
            ex_lines.append("Email:  (none)")

        changes = []
        if incoming["email"] and incoming["email"] != existing["email"]:
            if existing["email"]:
                changes.append(f"  • Email:  {existing['email']}  →  {incoming['email']}")
            else:
                changes.append(f"  • Email:  (add) {incoming['email']}")

        if not changes:
            return (
                f"⚠️ Contact \"{existing['name']}\" already exists with the same details.\n"
                f"   No changes were made."
            )

        msg = (
            f"A contact named \"{existing['name']}\" already exists:\n\n"
            + "\n".join(ex_lines)
            + "\n\nProposed changes:\n"
            + "\n".join(changes)
            + "\n\nDo you want to update this contact?"
        )

        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        confirmed = messagebox.askyesno(
            "WinVoice — Contact Already Exists",
            msg,
            icon='question'
        )
        root.destroy()

        if not confirmed:
            return f"ℹ️ Cancelled — \"{existing['name']}\" was not changed."

        merged_email = incoming["email"] if incoming["email"] else existing["email"]

        return sheets_update_contact_fields(
            self.session,
            row_number=row_num,
            name=existing["name"],
            email=merged_email,
            phone=existing.get("phone", ""),
        )

    def _get(self, original_query: str = "list contacts") -> str:
        try:
            raw_result = sheets_get_all_contacts(self.session)
            try:
                from general_agent import format_for_display
                formatted = format_for_display(raw_result, original_query, "contacts")
                return formatted
            except Exception:
                return raw_result
        except Exception as e:
            return f"❌ Failed to fetch contacts: {e}"

    def _delete(self, data: dict) -> str:
        name = data.get("name", "").strip()
        if not name:
            return "❌ Contact name is required."
        try:
            return sheets_delete_contact(self.session, name)
        except Exception as e:
            return f"❌ Failed to delete contact: {e}"