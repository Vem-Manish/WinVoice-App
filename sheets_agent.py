# sheets_agent.py
import json
from ai_backend import _call_ollama_chat, call_ollama_raw
from tools import (
    sheets_create_spreadsheet, sheets_read_range, sheets_write_range,
    sheets_append_row, sheets_clear_range, sheets_get_sheet_names,
    refresh_token_if_needed, _auth_headers, SHEETS_URL
)
import requests as _req

SHEETS_SYSTEM_PROMPT = """You are an intelligent Google Sheets assistant. Parse ANY spreadsheet request and output ONLY valid JSON.

ACTIONS:
create_spreadsheet(title)
read_sheet(sheet_name, range?)
query_sheet(sheet_name, question)        ← USE THIS for any question about sheet data
write_to_sheet(sheet_name, range, values)
append_row_to_sheet(sheet_name, row_data)
clear_sheet_range(sheet_name, range)
write_headers(sheet_name, headers)
sort_sheet(sheet_name, col_index, ascending?)
find_in_sheet(sheet_name, search_term)
sum_column(sheet_name, col_letter, start_row?, end_row?)
add_formula(sheet_name, cell, formula)
duplicate_sheet(sheet_name, new_title)
rename_sheet(sheet_name, new_title)
count_rows(sheet_name)
delete_row(sheet_name, row_number)

OUTPUT FORMAT:
{"steps": [{"action":"action_name","data":{"field":"value"}}]}

CRITICAL: Use query_sheet for ANY question that requires understanding sheet data:
- "when is X birthday" → query_sheet(birthdays, "when is X birthday")
- "what is X's salary" → query_sheet(employees, "what is X's salary")
- "how much did I spend on Y" → query_sheet(expenses, "how much did I spend on Y")
- "who has the highest score" → query_sheet(scores, "who has the highest score")
- "what is the price of X" → query_sheet(inventory, "what is price of X")
- "show me all entries where X" → query_sheet(sheet, "show entries where X")
- "tell me about X from Y sheet" → query_sheet(Y, "tell me about X")
- Any natural language question about data in a sheet → query_sheet
- PIPED DATA: If the prompt contains "[CONTEXT DATA FROM PREVIOUS STEP]", read the unstructured text and parse it into structured data using `append_row_to_sheet` or `write_to_sheet`!

Use read_sheet ONLY when user explicitly says "read/show/display all data in sheet".

MULTI-STEP DETECTION:
- "create X with headings A and B" → [create_spreadsheet(X), write_headers(X,[A,B])]
- "create X and add row Y" → [create_spreadsheet(X), append_row_to_sheet(X, Y)]

EXAMPLES:
User: from the birthdays sheet tell me when is shravya's birthday
{"steps": [{"action":"query_sheet","data":{"sheet_name":"birthdays","question":"when is shravya's birthday"}}]}

User: from the employees sheet what is john's salary
{"steps": [{"action":"query_sheet","data":{"sheet_name":"employees","question":"what is john's salary"}}]}

User: from the expenses sheet how much did I spend on food
{"steps": [{"action":"query_sheet","data":{"sheet_name":"expenses","question":"how much did I spend on food"}}]}

User: who has the highest score in the leaderboard sheet
{"steps": [{"action":"query_sheet","data":{"sheet_name":"leaderboard","question":"who has the highest score"}}]}

User: create a new sheet named birthdays with the headings name and birthday
{"steps": [{"action":"create_spreadsheet","data":{"title":"birthdays"}},{"action":"write_headers","data":{"sheet_name":"birthdays","headers":["name","birthday"]}}]}

User: add shravya and 7th feb to birthdays sheet
{"steps": [{"action":"append_row_to_sheet","data":{"sheet_name":"birthdays","row_data":["shravya","7th feb"]}}]}

User: sort the expenses sheet by amount
{"steps": [{"action":"sort_sheet","data":{"sheet_name":"expenses","col_index":2,"ascending":true}}]}

User: sum column C in the expenses sheet
{"steps": [{"action":"sum_column","data":{"sheet_name":"expenses","col_letter":"C"}}]}

User: delete row 3 in expenses sheet
{"steps": [{"action":"delete_row","data":{"sheet_name":"expenses","row_number":3}}]}

User: how many rows in contacts sheet
{"steps": [{"action":"count_rows","data":{"sheet_name":"contacts"}}]}

User: read the expenses sheet
{"steps": [{"action":"read_sheet","data":{"sheet_name":"expenses","range":"Sheet1"}}]}

User: [CONTEXT DATA FROM PREVIOUS STEP]
John spent $50 on food.
Task: add this to the expenses sheet
{"steps": [{"action":"append_row_to_sheet","data":{"sheet_name":"expenses","row_data":["John","Food","$50"]}}]}
"""


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


def _get_first_sheet_name(session, spreadsheet_id: str) -> str:
    """Returns the name of the first sheet tab."""
    try:
        r = _req.get(
            f"{SHEETS_URL}/{spreadsheet_id}",
            headers=_auth_headers(session),
            timeout=10
        )
        r.raise_for_status()
        sheets = r.json().get("sheets", [])
        if sheets:
            return sheets[0]["properties"]["title"]
    except Exception:
        pass
    return "Sheet1"  # fallback


def _col_letter(n: int) -> str:
    """Convert 0-based column index to Sheets letter (0→A, 25→Z, 26→AA, etc.)"""
    result = ""
    n += 1  # 1-based
    while n:
        n, r = divmod(n - 1, 26)
        result = chr(65 + r) + result
    return result


def _find_sheet_id(session, spreadsheet_id: str, tab_name: str = None) -> int:
    """Return the numeric sheetId for a tab. If tab_name is given, match by title."""
    try:
        refresh_token_if_needed(session)
        r = _req.get(
            f"{SHEETS_URL}/{spreadsheet_id}",
            headers=_auth_headers(session),
            params={"fields": "sheets.properties"}
        )
        r.raise_for_status()
        sheets = r.json().get("sheets", [])
        if not sheets:
            return 0
        if tab_name:
            for s in sheets:
                if s["properties"]["title"] == tab_name:
                    return s["properties"]["sheetId"]
        return sheets[0]["properties"]["sheetId"]
    except Exception:
        return 0


def _find_spreadsheet_id(session, name: str) -> str | None:
    from tools import DRIVE_URL
    try:
        refresh_token_if_needed(session)
        q = (f"name = '{name}' and mimeType = 'application/vnd.google-apps.spreadsheet'"
             f" and trashed = false")
        r = _req.get(DRIVE_URL, headers=_auth_headers(session),
                     params={"q": q, "fields": "files(id,name)"})
        r.raise_for_status()
        files = r.json().get("files", [])
        if files:
            return files[0]["id"]
        # Fuzzy fallback
        q2 = (f"name contains '{name}' and mimeType = 'application/vnd.google-apps.spreadsheet'"
              f" and trashed = false")
        r2 = _req.get(DRIVE_URL, headers=_auth_headers(session),
                      params={"q": q2, "fields": "files(id,name)"})
        r2.raise_for_status()
        files2 = r2.json().get("files", [])
        return files2[0]["id"] if files2 else None
    except Exception:
        return None


def _fetch_all_rows(session, spreadsheet_id: str) -> list[list[str]]:
    """Fetch all rows from the first tab of a spreadsheet."""
    try:
        refresh_token_if_needed(session)
        tab_name = _get_first_sheet_name(session, spreadsheet_id)
        r = _req.get(
            f"{SHEETS_URL}/{spreadsheet_id}/values/{tab_name}",
            headers=_auth_headers(session)
        )
        r.raise_for_status()
        return r.json().get("values", [])
    except Exception:
        return []


def _rows_to_text(rows: list[list[str]]) -> tuple[str, list[str]]:
    """
    Convert raw rows to a structured text table and extract headers.
    Returns (table_text, headers).
    """
    if not rows:
        return "", []
    headers = [str(h).strip() for h in rows[0]]
    lines = ["Headers: " + " | ".join(headers)]
    for i, row in enumerate(rows[1:], start=2):
        # Pad row to header length
        padded = list(row) + [""] * (len(headers) - len(row))
        row_parts = [f"{headers[j] if j < len(headers) else f'col{j}'}={str(padded[j]).strip()}"
                     for j in range(len(headers))]
        lines.append(f"Row {i}: " + ", ".join(row_parts))
    return "\n".join(lines), headers


class SheetsAgent:
    def __init__(self, session: dict):
        self.session = session

    def run(self, query: str, status_callback=None, piped_context: str = None) -> str:
        if status_callback: status_callback("Analyzing Spreadsheet Command...")
        raw = _call_ollama_chat(system=SHEETS_SYSTEM_PROMPT, user=query + "\nJSON:")
        if not raw:
            return "❌ AI returned empty response."

        if isinstance(raw, dict) and "steps" in raw:
            return self._run_steps(raw["steps"], status_callback)

        plan = _fix_wrapper(raw)
        action = plan.get("action", "")
        data = plan.get("data", {})
        if not action:
            return "❌ Could not understand the Sheets command."
        return self._dispatch(action, data, status_callback)

    def _run_steps(self, steps: list, status_callback=None) -> str:
        results = []
        last_created_title = None
        for step in steps:
            step = _fix_wrapper(step) if isinstance(step, dict) else {}
            action = step.get("action", "")
            data = dict(step.get("data", {}))
            if action != "create_spreadsheet" and last_created_title:
                sn = data.get("sheet_name", "").lower().strip()
                if not sn or sn in ("it", "that", "the sheet", "same sheet", ""):
                    data["sheet_name"] = last_created_title
            result = self._dispatch(action, data, status_callback)
            results.append(result)
            if action == "create_spreadsheet":
                last_created_title = data.get("title", "").strip()
        return "\n".join(results)

    def _dispatch(self, action: str, data: dict, status_callback=None) -> str:
        if action == "create_spreadsheet":
            if status_callback: status_callback(f"Creating spreadsheet: {data.get('title', '')}")
            return self._create(data)
        elif action == "read_sheet":
            if status_callback: status_callback(f"Reading spreadsheet: {data.get('sheet_name', '')}")
            return self._read(data)
        elif action == "query_sheet":
            if status_callback: status_callback(f"Querying spreadsheet: {data.get('sheet_name', '')}")
            return self._query(data)
        elif action == "write_to_sheet":
            if status_callback: status_callback(f"Writing data to spreadsheet: {data.get('sheet_name', '')}")
            return self._write(data)
        elif action == "append_row_to_sheet":
            if status_callback: status_callback(f"Appending row to spreadsheet: {data.get('sheet_name', '')}")
            return self._append_row(data)
        elif action == "clear_sheet_range":
            if status_callback: status_callback("Clearing sheet data...")
            return self._clear(data)
        elif action == "write_headers":
            if status_callback: status_callback("Writing headers...")
            headers = data.get("headers", [])
            sheet_name = data.get("sheet_name", "")
            if not headers or not sheet_name:
                return "❌ Sheet name and headers are required."
            col_end = _col_letter(len(headers) - 1)
            return self._write({
                "sheet_name": sheet_name,
                "range": f"Sheet1!A1:{col_end}1",
                "values": [headers]
            })
        elif action == "sort_sheet":
            if status_callback: status_callback("Sorting spreadsheet...")
            return self._sort(data)
        elif action == "find_in_sheet":
            if status_callback: status_callback("Searching spreadsheet...")
            return self._find(data)
        elif action == "sum_column":
            if status_callback: status_callback("Calculating sum...")
            return self._sum_column(data)
        elif action == "add_formula":
            if status_callback: status_callback("Adding formula...")
            return self._add_formula(data)
        elif action == "duplicate_sheet":
            if status_callback: status_callback("Duplicating spreadsheet...")
            return self._duplicate(data)
        elif action == "rename_sheet":
            if status_callback: status_callback("Renaming spreadsheet...")
            return self._rename(data)
        elif action == "count_rows":
            if status_callback: status_callback("Counting rows...")
            return self._count_rows(data)
        elif action == "delete_row":
            return self._delete_row(data)
        else:
            return f"❌ Unknown Sheets action: {action}"

    # ── SMART QUERY — the key new feature ────────────────────────────

    def _query(self, data: dict) -> str:
        """
        Read the entire sheet, understand its structure from headers,
        and use AI to answer any natural-language question about the data.
        """
        name = data.get("sheet_name", "").strip()
        question = data.get("question", "").strip()
        if not name:
            return "❌ Sheet name is required."
        if not question:
            return "❌ Question is required."

        sid = _find_spreadsheet_id(self.session, name)
        if not sid:
            return f"❌ Spreadsheet \"{name}\" not found."

        rows = _fetch_all_rows(self.session, sid)
        if not rows:
            return f"📊 \"{name}\" is empty — no data to query."

        table_text, headers = _rows_to_text(rows)
        total_rows = len(rows) - 1  # exclude header

        prompt = f"""You are a smart spreadsheet assistant. A user has a Google Sheet called "{name}" and is asking a question about its data.

SHEET STRUCTURE:
- Sheet name: {name}
- Columns: {', '.join(headers)}
- Total data rows: {total_rows}

FULL SHEET DATA:
{table_text}

USER QUESTION: {question}

INSTRUCTIONS:
- Answer the question directly and clearly using ONLY the data shown above
- Be conversational and helpful, like a smart assistant
- If asking for a specific value (e.g. "when is X birthday"), give a direct answer like "Shravya's birthday is 7th February"
- If asking for aggregates (sum, count, highest, lowest), calculate and state the result
- If the person/item is not found, say so clearly
- If asking to filter/list entries matching criteria, list them neatly
- Never say "based on the data" or "according to the sheet" — just answer naturally
- Keep the answer concise

ANSWER:"""

        answer = call_ollama_raw(prompt).strip()
        if not answer:
            return f"❌ Could not answer from \"{name}\" data."
        return f"📊 {answer}"

    # ── BASIC CRUD ────────────────────────────────────────────────────

    def _create(self, data: dict) -> str:
        title = data.get("title", "").strip()
        if not title:
            return "❌ Spreadsheet title is required."
        try:
            return sheets_create_spreadsheet(self.session, title)
        except Exception as e:
            return f"❌ Failed to create spreadsheet: {e}"

    def _read(self, data: dict) -> str:
        name = data.get("sheet_name", "").strip()
        if not name:
            return "❌ Sheet name is required."
        try:
            return sheets_read_range(self.session, name, data.get("range", "Sheet1"))
        except Exception as e:
            return f"❌ Failed to read sheet: {e}"

    def _write(self, data: dict) -> str:
        name = data.get("sheet_name", "").strip()
        range_ = data.get("range", "Sheet1!A1")
        values = data.get("values", [[]])
        if not name:
            return "❌ Sheet name is required."
        if not isinstance(values, list):
            values = [[str(values)]]
        if values and not isinstance(values[0], list):
            values = [values]
        try:
            sheets_write_range(self.session, name, range_, values)
            flat = " | ".join(str(v) for row in values for v in row)
            return f"✅ Written to \"{name}\" at {range_} — {flat}"
        except Exception as e:
            return f"❌ Failed to write to sheet: {e}"

    def _append_row(self, data: dict) -> str:
        name = data.get("sheet_name", "").strip()
        row = data.get("row_data", [])
        if not name:
            return "❌ Sheet name is required."
        if isinstance(row, str):
            row = [r.strip() for r in row.split(",")]
        if not row:
            return "❌ Row data is required."
        try:
            sheets_append_row(self.session, name, row)
            row_str = " | ".join(str(r) for r in row)
            return f"✅ Row added to \"{name}\" — {row_str}"
        except Exception as e:
            return f"❌ Failed to append row: {e}"

    def _clear(self, data: dict) -> dict | str:
        name = data.get("sheet_name", "").strip()
        range_ = data.get("range", "Sheet1")
        if not name:
            return "❌ Sheet name is required."
        try:
            return {
                "pending_confirmation": True,
                "type": "clear_sheet_range",
                "subject": f"Clear range '{range_}' in sheet '{name}'",
                "sheet_name": name,
                "range": range_,
                "tts": True
            }
        except Exception as e:
            return f"❌ Failed to clear sheet: {e}"

    # ── ADVANCED FEATURES ─────────────────────────────────────────────

    def _sort(self, data: dict) -> str:
        name = data.get("sheet_name", "").strip()
        col_index_raw = data.get("col_index", 0)
        try:
            col_index = int(col_index_raw)
        except (ValueError, TypeError):
            # Try letter conversion (A=0, B=1, etc.)
            if isinstance(col_index_raw, str) and col_index_raw.isalpha():
                col_index = ord(col_index_raw.upper()[0]) - ord('A')
            else:
                col_index = 0
        ascending = data.get("ascending", True)
        if isinstance(ascending, str):
            ascending = ascending.lower() not in ("false", "no", "desc", "descending")
        if not name:
            return "❌ Sheet name is required."
        try:
            sid = _find_spreadsheet_id(self.session, name)
            if not sid:
                return f"❌ Spreadsheet \"{name}\" not found."
            sheet_gid = _find_sheet_id(self.session, sid)
            refresh_token_if_needed(self.session)
            body = {"requests": [{"sortRange": {
                "range": {"sheetId": sheet_gid, "startRowIndex": 1},
                "sortSpecs": [{"dimensionIndex": col_index,
                               "sortOrder": "ASCENDING" if ascending else "DESCENDING"}]
            }}]}
            r = _req.post(f"{SHEETS_URL}/{sid}:batchUpdate",
                          headers=_auth_headers(self.session), json=body)
            r.raise_for_status()
            direction = "A→Z" if ascending else "Z→A"
            col_letter = chr(ord('A') + col_index)
            return f"✅ \"{name}\" sorted by column {col_letter} ({direction})"
        except Exception as e:
            return f"❌ Failed to sort sheet: {e}"

    def _find(self, data: dict) -> str:
        name = data.get("sheet_name", "").strip()
        term = data.get("search_term", "").strip().lower()
        if not name or not term:
            return "❌ Sheet name and search term are required."
        try:
            sid = _find_spreadsheet_id(self.session, name)
            if not sid:
                return f"❌ Spreadsheet \"{name}\" not found."
            rows = _fetch_all_rows(self.session, sid)
            if not rows:
                return f"📊 \"{name}\" is empty."
            headers = rows[0]
            matches = []
            for i, row in enumerate(rows[1:], start=2):
                if any(term in str(cell).lower() for cell in row):
                    row_str = " | ".join(str(c) for c in row)
                    matches.append(f"  Row {i}: {row_str}")
            if not matches:
                return f"🔍 No results for \"{term}\" in \"{name}\"."
            header_str = " | ".join(headers)
            result = f"🔍 {len(matches)} match(es) for \"{term}\" in \"{name}\":\n"
            result += f"  Headers: {header_str}\n"
            result += "\n".join(matches[:20])
            if len(matches) > 20:
                result += f"\n  … and {len(matches)-20} more"
            return result
        except Exception as e:
            return f"❌ Failed to search sheet: {e}"

    def _sum_column(self, data: dict) -> str:
        name = data.get("sheet_name", "").strip()
        col_letter = data.get("col_letter", "A").upper().strip()
        if not name:
            return "❌ Sheet name is required."
        try:
            sid = _find_spreadsheet_id(self.session, name)
            if not sid:
                return f"❌ Spreadsheet \"{name}\" not found."
            refresh_token_if_needed(self.session)
            r = _req.get(
                f"{SHEETS_URL}/{sid}/values/Sheet1!{col_letter}:{col_letter}",
                headers=_auth_headers(self.session)
            )
            r.raise_for_status()
            cells = r.json().get("values", [])
            total = 0.0
            count = 0
            for cell in cells[1:]:
                if cell:
                    try:
                        total += float(str(cell[0]).replace(",", "").replace("₹", "").replace("$", "").strip())
                        count += 1
                    except (ValueError, IndexError):
                        pass
            return f"➕ Sum of column {col_letter} in \"{name}\": {total:,.2f} ({count} values)"
        except Exception as e:
            return f"❌ Failed to sum column: {e}"

    def _add_formula(self, data: dict) -> str:
        name = data.get("sheet_name", "").strip()
        cell = data.get("cell", "").strip()
        formula = data.get("formula", "").strip()
        if not name or not cell or not formula:
            return "❌ Sheet name, cell, and formula are all required."
        if not cell.startswith("Sheet1!"):
            cell = f"Sheet1!{cell}"
        try:
            sheets_write_range(self.session, name, cell, [[formula]])
            return f"✅ Formula written to {cell} in \"{name}\" — {formula}"
        except Exception as e:
            return f"❌ Failed to add formula: {e}"

    def _duplicate(self, data: dict) -> str:
        name = data.get("sheet_name", "").strip()
        new_title = data.get("new_title", name + " Copy").strip()
        if not name:
            return "❌ Sheet name is required."
        try:
            sid = _find_spreadsheet_id(self.session, name)
            if not sid:
                return f"❌ Spreadsheet \"{name}\" not found."
            from tools import DRIVE_URL
            refresh_token_if_needed(self.session)
            r = _req.post(f"{DRIVE_URL}/{sid}/copy",
                          headers=_auth_headers(self.session), json={"name": new_title})
            r.raise_for_status()
            new_id = r.json().get("id", "")
            url = f"https://docs.google.com/spreadsheets/d/{new_id}"
            return f"✅ Duplicated \"{name}\" → \"{new_title}\"\n🔗 {url}"
        except Exception as e:
            return f"❌ Failed to duplicate spreadsheet: {e}"

    def _rename(self, data: dict) -> str:
        name = data.get("sheet_name", "").strip()
        new_title = data.get("new_title", "").strip()
        if not name or not new_title:
            return "❌ Both current name and new name are required."
        try:
            sid = _find_spreadsheet_id(self.session, name)
            if not sid:
                return f"❌ Spreadsheet \"{name}\" not found."
            refresh_token_if_needed(self.session)
            body = {"requests": [{"updateSpreadsheetProperties": {
                "properties": {"title": new_title}, "fields": "title"
            }}]}
            r = _req.post(f"{SHEETS_URL}/{sid}:batchUpdate",
                          headers=_auth_headers(self.session), json=body)
            r.raise_for_status()
            return f"✅ Renamed \"{name}\" → \"{new_title}\""
        except Exception as e:
            return f"❌ Failed to rename sheet: {e}"

    def _count_rows(self, data: dict) -> str:
        name = data.get("sheet_name", "").strip()
        if not name:
            return "❌ Sheet name is required."
        try:
            sid = _find_spreadsheet_id(self.session, name)
            if not sid:
                return f"❌ Spreadsheet \"{name}\" not found."
            rows = _fetch_all_rows(self.session, sid)
            data_rows = max(0, len(rows) - 1)
            return f"📊 \"{name}\" has {data_rows} data row(s) (+ 1 header row)"
        except Exception as e:
            return f"❌ Failed to count rows: {e}"

    def _delete_row(self, data: dict) -> dict | str:
        name = data.get("sheet_name", "").strip()
        row_number = int(data.get("row_number", 0))
        if not name:
            return "❌ Sheet name is required."
        if row_number < 1:
            return "❌ Row number must be 1 or greater."
        if row_number == 1:
            return "⚠️ Cannot delete the header row. Use clear instead."
        try:
            sid = _find_spreadsheet_id(self.session, name)
            if not sid:
                return f"❌ Spreadsheet \"{name}\" not found."
            sheet_gid = _find_sheet_id(self.session, sid)
            return {
                "pending_confirmation": True,
                "type": "delete_row",
                "subject": f"Delete row {row_number} in sheet '{name}'",
                "sheet_name": name,
                "row_number": row_number,
                "sid": sid,
                "sheet_gid": sheet_gid,
                "tts": True
            }
        except Exception as e:
            return f"❌ Failed to delete row: {e}"
            
    def execute_pending(self, pending: dict) -> str:
        try:
            ptype = pending.get("type")
            if ptype == "clear_sheet_range":
                return sheets_clear_range(self.session, pending.get("sheet_name"), pending.get("range"))
            elif ptype == "delete_row":
                sid = pending.get("sid")
                sheet_gid = pending.get("sheet_gid")
                row_number = pending.get("row_number")
                name = pending.get("sheet_name")
                
                refresh_token_if_needed(self.session)
                body = {"requests": [{"deleteDimension": {
                    "range": {
                        "sheetId": sheet_gid, "dimension": "ROWS",
                        "startIndex": row_number - 1, "endIndex": row_number
                    }
                }}]}
                r = _req.post(f"{SHEETS_URL}/{sid}:batchUpdate",
                              headers=_auth_headers(self.session), json=body)
                r.raise_for_status()
                return f"✅ Deleted row {row_number} from \"{name}\""
            return "❌ Unknown pending action."
        except Exception as e:
            return f"❌ Execution failed: {e}"