# tasks_agent.py
import json
import re
import dateutil.parser
from datetime import datetime, timedelta
from ai_backend import _call_ollama_chat
from tools import create_task, get_tasks, complete_task

TASKS_SYSTEM_PROMPT = """You are an intelligent Google Tasks assistant. Parse ANY task/reminder request and output ONLY valid JSON.

ACTIONS:
create_task(title, due_date_text?)
get_tasks()
complete_task(task_title_keyword)

OUTPUT FORMAT (Always use a steps array):
{"steps": [{"action":"action_name","data":{"field":"value"}}]}

INTELLIGENCE RULES:
- If processing PIPED DATA (e.g. email notes), extract ALL action items and return a create_task step for EACH one.
- "remind me to X on/by [day]" → create_task (NO specific time = task not event)
- "add task / todo / don't forget / remember to" → create_task
- "what are my tasks / show tasks / my todo / pending" → get_tasks
- "mark X done / complete X / finished X / tick off X / done with X" → complete_task
- due_date_text: use natural words like "tomorrow", "friday", "next monday", "today", "this weekend"
- Extract a CLEAN title — remove "remind me to", "add task", "don't forget to" etc
- task_title_keyword: use the KEY WORD from the task title, 1-3 words is enough

TITLE INTELLIGENCE:
- "remind me to buy milk tomorrow" → title: "Buy milk"
- "don't forget to call the bank on friday" → title: "Call the bank"
- "add a task to review the code" → title: "Review the code"
- "remind me to go to the gym on saturday" → title: "Go to the gym"
- "todo finish the report by thursday" → title: "Finish the report"

COMPLETION INTELLIGENCE:
- "mark buy milk as done" → task_title_keyword: "buy milk"
- "I finished the report" → task_title_keyword: "report"
- "done with gym" → task_title_keyword: "gym"
- "complete the call bank task" → task_title_keyword: "call"

EXAMPLES:
User: remind me to buy milk tomorrow
{"steps":[{"action":"create_task","data":{"title":"Buy milk","due_date_text":"tomorrow"}}]}

User: remind me to call the bank on friday and also pay bills
{"steps":[{"action":"create_task","data":{"title":"Call the bank","due_date_text":"friday"}}, {"action":"create_task","data":{"title":"Pay bills"}}]}

User: [CONTEXT DATA...] ... notes: 1. call John 2. email Sarah ... [TASK] add these as tasks
{"steps":[{"action":"create_task","data":{"title":"Call John"}}, {"action":"create_task","data":{"title":"Email Sarah"}}]}

User: add a task to pay bills today
{"steps":[{"action":"create_task","data":{"title":"Pay bills","due_date_text":"today"}}]}

User: remind me to buy groceries on saturday
{"steps":[{"action":"create_task","data":{"title":"Buy groceries","due_date_text":"saturday"}}]}

User: don't forget to call sara on sunday
{"steps":[{"action":"create_task","data":{"title":"Call Sara","due_date_text":"sunday"}}]}

User: todo review code today
{"steps":[{"action":"create_task","data":{"title":"Review code","due_date_text":"today"}}]}

User: remind me to water the plants tomorrow
{"steps":[{"action":"create_task","data":{"title":"Water the plants","due_date_text":"tomorrow"}}]}

User: add task prepare for interview on wednesday
{"steps":[{"action":"create_task","data":{"title":"Prepare for interview","due_date_text":"wednesday"}}]}

User: remind me to renew my license next month
{"steps":[{"action":"create_task","data":{"title":"Renew license","due_date_text":"next month"}}]}

User: what are my tasks
{"steps":[{"action":"get_tasks","data":{}}]}

User: list my todos
{"steps":[{"action":"get_tasks","data":{}}]}

User: what is on my to do list
{"steps":[{"action":"get_tasks","data":{}}]}

User: mark buy milk as done
{"steps":[{"action":"complete_task","data":{"task_title_keyword":"buy milk"}}]}

User: complete the call the bank task
{"steps":[{"action":"complete_task","data":{"task_title_keyword":"call"}}]}

User: I finished the report
{"steps":[{"action":"complete_task","data":{"task_title_keyword":"report"}}]}

User: done with pay bills
{"steps":[{"action":"complete_task","data":{"task_title_keyword":"pay bills"}}]}

User: tick off buy groceries
{"steps":[{"action":"complete_task","data":{"task_title_keyword":"groceries"}}]}

User: mark submit report as complete
{"steps":[{"action":"complete_task","data":{"task_title_keyword":"submit report"}}]}"""


WEEKDAYS = {
    "monday":0,"tuesday":1,"wednesday":2,"thursday":3,
    "friday":4,"saturday":5,"sunday":6,
}


def _fuzzy_date(s: str):
    if not s:
        return None
    s = s.lower().strip()
    now = datetime.utcnow()
    try:
        if "tomorrow" in s:
            dt = now + timedelta(days=1)
        elif "today" in s:
            dt = now
        elif "next week" in s:
            dt = now + timedelta(weeks=1)
        elif "next month" in s:
            dt = now + timedelta(days=30)
        elif "this weekend" in s:
            if now.weekday() == 6:  # Already Sunday
                days = 0
            else:
                days = 5 - now.weekday()
                if days < 0:
                    days += 7
            dt = now + timedelta(days=days)
        else:
            for d, n in WEEKDAYS.items():
                if d in s:
                    ahead = n - now.weekday()
                    if ahead <= 0:
                        ahead += 7
                    return (now + timedelta(days=ahead)).strftime('%Y-%m-%dT00:00:00.000Z')
            dt = dateutil.parser.parse(s, fuzzy=True)
        return dt.strftime('%Y-%m-%dT00:00:00.000Z')
    except Exception:
        return None


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


class TasksAgent:
    def __init__(self, session: dict):
        self.session = session

    def run(self, query: str, status_callback=None, piped_context: str = None) -> str:
        # Piped data can be large. Let the LLM extract tasks directly from it.
        if "[CONTEXT DATA" in query and status_callback:
            status_callback("Extracting tasks from context data...")
            
        raw = _call_ollama_chat(system=TASKS_SYSTEM_PROMPT, user=query + "\nJSON:")
        if not raw:
            return "❌ AI returned empty response."
        plan = _fix_wrapper(raw)
        
        # Support both single action and steps array
        steps = plan.get("steps", [plan]) if "steps" in plan else [plan]
        
        # ── Bulk-list fallback ────────────────────────────────────────────────
        # If the LLM collapsed a bullet/numbered list into a single create_task,
        # detect and explode it into multiple tasks automatically.
        if len(steps) == 1 and steps[0].get("action") == "create_task":
            single_title = steps[0].get("data", {}).get("title", "")
            # Detect if title contains list-like content (newlines, bullets, numbers)
            if "\n" in single_title or re.search(r"^\d+\.", single_title.strip()):
                lines = single_title.split("\n")
                parsed = []
                for line in lines:
                    # Strip bullet/number prefixes: "1.", "•", "-", "*"
                    cleaned = re.sub(r"^\s*[\d]+[\.\)]\s*|^\s*[-•*]\s*", "", line).strip()
                    if cleaned and len(cleaned) > 2:
                        parsed.append({"action": "create_task", "data": {"title": cleaned}})
                if len(parsed) > 1:
                    steps = parsed  # Replace single step with exploded list
        # ─────────────────────────────────────────────────────────────────────

        results = []
        for step in steps:
            action = step.get("action", "")
            data = step.get("data", {})
            if not action:
                continue
            if action == "create_task":
                if status_callback: status_callback(f"Creating task: {data.get('title', '')[:40]}")
                results.append(self._create(data))
            elif action == "get_tasks":
                results.append(self._get())
            elif action == "complete_task":
                res = self._complete(data)
                # If it's a pending confirmation dict, just return it immediately (can't batch these easily)
                if isinstance(res, dict): return res
                results.append(res)
            else:
                results.append(f"❌ Unknown tasks action: {action}")
                
        return "\n".join(results) if results else "❌ Could not understand the task command."


    def _create(self, data: dict) -> str:
        title = data.get("title", "").strip()
        if not title:
            return "❌ Task title is required."
        raw_date = data.get("due_date_text")
        iso = _fuzzy_date(raw_date) if raw_date else None
        try:
            create_task(self.session, title=title, due=iso)
            msg = f"✅ Task created — \"{title}\""
            if raw_date:
                msg += f" (Due: {raw_date.title()})"
            return msg
        except Exception as e:
            return f"❌ Failed to create task: {e}"

    def _get(self) -> str:
        try:
            tasks = get_tasks(self.session)
            if not tasks:
                return "📋 No pending tasks. You're all caught up!"
            lines = [f"  {i+1}. {t.get('title', '(Untitled)')}" for i, t in enumerate(tasks)]
            return f"📋 Your tasks ({len(tasks)}):\n" + "\n".join(lines)
        except Exception as e:
            return f"❌ Could not fetch tasks: {e}"

    def _complete(self, data: dict) -> dict | str:
        keyword = data.get("task_title_keyword", "").lower().strip()
        if not keyword:
            return "❌ Please specify which task to complete."
        try:
            tasks = get_tasks(self.session)
            tasks = tasks or []
            target = next((t for t in tasks if keyword in t.get("title", "").lower()), None)
            if not target:
                return f"❌ No task found matching \"{keyword}\". Check your tasks list."
            return {
                "pending_confirmation": True,
                "type": "complete_task",
                "subject": f"Complete task '{target['title']}'",
                "task_id": target["id"],
                "tts": True
            }
        except Exception as e:
            return f"❌ Failed to complete task: {e}"
            
    def execute_pending(self, pending: dict) -> str:
        try:
            if pending.get("type") == "complete_task":
                task_id = pending.get("task_id")
                complete_task(self.session, task_id=task_id)
                return "✅ Task marked as completed."
            return "❌ Unknown pending action."
        except Exception as e:
            return f"❌ Execution failed: {e}"