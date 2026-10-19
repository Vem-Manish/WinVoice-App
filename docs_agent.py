# docs_agent.py
import json
from ai_backend import _call_ollama_chat, call_ollama_raw
from tools import create_doc, append_text_to_doc, read_doc, find_doc_id_by_name, search_web

DOCS_SYSTEM_PROMPT = """You are an intelligent Google Docs assistant. Parse ANY document request and output ONLY valid JSON.

ACTIONS:
create_doc(title)                   → Creates a brand NEW empty document
append_to_doc(doc_name, text)       → Writes text INTO an EXISTING document
open_doc(doc_name)                  → Opens an existing document in the browser (view/open only)
read_doc(doc_name)                  → Reads and returns the content of an existing document
process_text(text, instruction)     → Used ONLY when asked to summarize, format, or process text from a PREVIOUS STEP, WITHOUT saving it to any document.

OUTPUT FORMAT:
{"steps": [{"action":"action_name","data":{"field":"value"}}]}

== CRITICAL DISAMBIGUATION RULES ==

USE create_doc ONLY when:
- User says: create / make / new / start / build + a document
- Keywords: "create", "make a doc", "new document", "a new doc called"

USE process_text ONLY when:
- User says: summarize it, extract info, format it, etc AND does NOT ask to save/write it to a document.

USE append_to_doc ONLY when:
- User says: write / add / put / update / insert + into/in/to an EXISTING doc
- Keywords: "write in", "write inside", "add to", "write in the X doc", "update the X doc", "put this in"
- The document ALREADY EXISTS — do NOT create it

USE open_doc ONLY when:
- User says: open / show me / view / launch + a document
- Keywords: "open the", "open my", "show me the", "view the"

USE read_doc ONLY when:
- User says: read / what's in / show contents of a document
- Keywords: "read the", "what's in", "show contents", "tell me what's in"

== DECISION TREE ==
Does the user want a NEW document?  → create_doc
Does the user want to write INTO an existing document? → append_to_doc
Does the user want to OPEN/VIEW a document in the browser? → open_doc
Does the user want to READ the text content? → read_doc

== MULTI-STEP RULES ==
- "create X and write Y in it" = create_doc(X) + append_to_doc(X, Y)
- "create X and then write about Y" = create_doc(X) + append_to_doc(X, "SEARCH_WEB: Y")
- "create X doc and put the latest Z in it" = create_doc(X) + append_to_doc(X, "SEARCH_WEB: Z")
- "write inside X and Y docs" = append_to_doc(X, text) + append_to_doc(Y, text)
- NEVER produce two create_doc steps for one request unless user explicitly asked to create TWO different docs
- The doc_name in append_to_doc MUST match the title in create_doc exactly
- NEVER create a doc when user says "spreadsheet" or "sheet"
- If text needs CURRENT info (news, prices, scores, results, war, elections) → text = "SEARCH_WEB: <specific query>"

== EXTRACTION RULES ==
- NEVER include the instructional wrapper words in the text! 
- If the user says: "type Hello World inside the document testing" -> text is "Hello World", NOT "type Hello World inside the document testing".
- Strip away all conversational instructions before extracting `text`.
- PIPED DATA: If the prompt contains "[CONTEXT DATA FROM PREVIOUS STEP]", do NOT try to summarize or write it out yourself. You MUST output EXACTLY the literal string "[PIPED_DATA]" inside the `text` field!

EXAMPLES:
User: create a document called meeting notes
{"steps": [{"action":"create_doc","data":{"title":"Meeting Notes"}}]}

User: open the meeting notes doc
{"steps": [{"action":"open_doc","data":{"doc_name":"Meeting Notes"}}]}

User: type testing from winvoice inside the document testing
{"steps": [{"action":"append_to_doc","data":{"doc_name":"testing","text":"testing from winvoice"}}]}

User: write about artificial intelligence in the AI Research doc
{"steps": [{"action":"append_to_doc","data":{"doc_name":"AI Research","text":"SEARCH_WEB: artificial intelligence overview 2026"}}]}

User: write hello world in the test doc
{"steps": [{"action":"append_to_doc","data":{"doc_name":"Test","text":"Hello World"}}]}

User: write in the meeting notes document: project deadline is June 15
{"steps": [{"action":"append_to_doc","data":{"doc_name":"Meeting Notes","text":"Project deadline is June 15"}}]}

User: add the latest cricket scores to the sports doc
{"steps": [{"action":"append_to_doc","data":{"doc_name":"Sports","text":"SEARCH_WEB: latest cricket match scores today 2026"}}]}

User: create a new doc named tamil nadu elections and write about the latest election results
{"steps":[{"action":"create_doc","data":{"title":"Tamil Nadu Elections"}},{"action":"append_to_doc","data":{"doc_name":"Tamil Nadu Elections","text":"SEARCH_WEB: latest Tamil Nadu election results 2026"}}]}

User: make a document called crypto news and add current bitcoin and ethereum prices
{"steps":[{"action":"create_doc","data":{"title":"Crypto News"}},{"action":"append_to_doc","data":{"doc_name":"Crypto News","text":"SEARCH_WEB: bitcoin ethereum price today 2026"}}]}

User: create a meeting notes doc and write: review Q2 plan Q3 team updates
{"steps":[{"action":"create_doc","data":{"title":"Meeting Notes"}},{"action":"append_to_doc","data":{"doc_name":"Meeting Notes","text":"Agenda:\\n1. Review Q2\\n2. Plan Q3\\n3. Team updates"}}]}

User: write hello in the notes doc and also write world in the ideas doc
{"steps":[{"action":"append_to_doc","data":{"doc_name":"Notes","text":"Hello"}},{"action":"append_to_doc","data":{"doc_name":"Ideas","text":"World"}}]}

User: create an AI doc and write about AI, and also create a Python doc and write about python
{"steps":[{"action":"create_doc","data":{"title":"AI Doc"}},{"action":"append_to_doc","data":{"doc_name":"AI Doc","text":"SEARCH_WEB: artificial intelligence overview 2026"}},{"action":"create_doc","data":{"title":"Python Doc"}},{"action":"append_to_doc","data":{"doc_name":"Python Doc","text":"SEARCH_WEB: Python programming language overview"}}]}

User: read the meeting notes document
{"steps": [{"action":"read_doc","data":{"doc_name":"Meeting Notes"}}]}

User: write the current bitcoin price in finance doc
{"steps": [{"action":"append_to_doc","data":{"doc_name":"Finance","text":"SEARCH_WEB: bitcoin price today 2026"}}]}

User: [CONTEXT DATA FROM PREVIOUS STEP]
Q3 Revenue grew by 15%.
Task: summarize it and write down the summary in a new document called sales report
{"steps":[{"action":"create_doc","data":{"title":"Sales Report"}},{"action":"append_to_doc","data":{"doc_name":"Sales Report","text":"[PIPED_DATA]"}}]}

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


def _deduplicate_steps(steps: list) -> list:
    """
    Safety pass: if the AI produced more than one create_doc, collapse them
    into one — use the first title and drop the rest, while keeping all
    append_to_doc steps pointing at that single title.
    """
    creates = [s for s in steps if s.get("action") == "create_doc"]
    if len(creates) <= 1:
        return steps

    # Check if all creates have the same title
    titles = set(c.get("data", {}).get("title", "").lower().strip() for c in creates)
    if len(titles) > 1:
        return steps  # Intentionally creating multiple different docs

    # Keep only the first create and its title
    canonical_title = creates[0].get("data", {}).get("title", "")
    deduped = []
    seen_create = False
    for s in steps:
        if s.get("action") == "create_doc":
            if not seen_create:
                deduped.append(s)
                seen_create = True
            # else: drop duplicate create
        elif s.get("action") == "append_to_doc":
            # Fix doc_name to point at the canonical title
            fixed = dict(s)
            fixed["data"] = dict(s.get("data", {}))
            fixed["data"]["doc_name"] = canonical_title
            deduped.append(fixed)
        else:
            deduped.append(s)
    return deduped


class DocsAgent:
    def __init__(self, session: dict):
        self.session = session

    def run(self, query: str, status_callback=None, piped_context: str = None) -> str:
        q = query.strip()
        if status_callback: status_callback("Analyzing Document Intent...")

        piped_context = ""
        if "[CONTEXT DATA FROM PREVIOUS STEP" in query:
            if "[TASK]" in query:
                parts = query.split("[TASK]", 1)
                # Everything before [TASK] is context (except the marker itself)
                piped_context = parts[0].split("]\n")[-1].strip()
                # Update 'q' so the LLM ONLY sees the clean task instruction (prevents hallucination)
                q = parts[1].strip()
                
                # Strip user context noise from the task instruction if present
                if "[User Context" in q:
                    q = q.split("\n")[-1].strip()

        # ── Deterministic pre-parse: "create X and write/add Y" ──────────
        # Ollama (local model) often ignores multi-step instructions.
        # We catch the most common pattern here before calling the AI.
        import re as _re

        # ── Fast-path: append-only (no create) ───────────────────────────
        # If the query says "add/write/append to [doc]" without "create", skip
        # the create+write path entirely so we don't accidentally create a
        # duplicate document. Let the LLM handle it directly.
        _is_append_only = (
            not _re.search(r'\b(create|make|new doc|new document)\b', q, _re.IGNORECASE)
            and _re.search(r'\b(add|append|write|put|include)\b.+\b(to|in|into)\b.+\b(doc|document)\b', q, _re.IGNORECASE)
        )
        if _is_append_only:
            pass  # Fall through to LLM path below
        else:
            # Pattern: "create [a/new] doc[ument] [called/named] <TITLE> and write/add/put <CONTENT>"
            _create_write = _re.search(
                r'(?:create|make|new)\s+(?:a\s+)?(?:new\s+)?(?:doc(?:ument)?\s+)?'
                r'(?:called|named|titled)?\s*["\']?([^,]+?)["\']?\s+'
                r'(?:and|then)\s+'
                r'(?:write|add|put|include)\s+(?:about\s+)?(.+)',
                q, _re.IGNORECASE
            )
            if _create_write and "sheet" not in q.lower():
                raw_title = _create_write.group(1).strip().rstrip('.,')
                raw_content = _create_write.group(2).strip().rstrip('.,')
                # Clean noise words from title
                for noise in (' document', ' google doc', ' doc', ' file'):
                    if raw_title.lower().endswith(noise):
                        raw_title = raw_title[:-len(noise)].strip()
                # Title-case the title
                title = raw_title.title()
                # Determine if content needs a web search
                _SEARCH_TRIGGERS = (
                    'latest', 'current', 'today', 'news', 'price', 'prices',
                    'result', 'results', 'score', 'scores', 'status', 'update',
                    'war', 'election', 'elections', 'crisis', 'pandemic', 'virus',
                    'market', 'economy', 'weather', 'happening',
                )
                needs_search = any(w in raw_content.lower() for w in _SEARCH_TRIGGERS)
                text = f"SEARCH_WEB: {raw_content}" if needs_search else raw_content
                
                if piped_context:
                    if "[PIPED_DATA]" in text:
                        text = text.replace("[PIPED_DATA]", piped_context)
                    else:
                        text += f"\n\n{piped_context}"

                steps = [
                    {"action": "create_doc", "data": {"title": title}},
                    {"action": "append_to_doc", "data": {"doc_name": title, "text": text}},
                ]
                return self._run_steps(steps, status_callback)

        # ── For append-only + piped data: skip LLM entirely ──────────────
        # We already know the doc name (from query) and data (from piped_context).
        # No need to call the LLM — build the step directly.
        if _is_append_only and piped_context:
            import re as _re2
            # Extract doc name from query like "add X to the Wrap Up document"
            doc_match = _re2.search(
                r'\b(?:to|in|into)\s+(?:the\s+)?["\']?([a-zA-Z0-9 _\-]+?)["\']?\s+(?:doc(?:ument)?|google doc)\b',
                q, _re2.IGNORECASE
            )
            if doc_match:
                doc_name = doc_match.group(1).strip().title()
                steps = [{"action": "append_to_doc", "data": {"doc_name": doc_name, "text": piped_context}}]
                return self._run_steps(steps, status_callback)

        # ── LLM path: include piped_context in prompt so model can use it ──
        if piped_context:
            llm_user_prompt = (
                f"CONTEXT DATA (use this as the content to write/append — do NOT ask for it):\n"
                f"{piped_context}\n\n"
                f"TASK: {q}\nJSON:"
            )
        else:
            llm_user_prompt = q + "\nJSON:"

        raw = _call_ollama_chat(system=DOCS_SYSTEM_PROMPT, user=llm_user_prompt)


        if not raw:
            return "❌ AI returned empty response."

        # ── Multi-step support ─────────────────────────────────────────
        if isinstance(raw, dict) and "steps" in raw:
            steps = _deduplicate_steps(raw["steps"])
            for step in steps:
                if piped_context:
                    if "data" not in step:
                        step["data"] = {}
                    if "text" in step["data"]:
                        if "[PIPED_DATA]" in step["data"]["text"]:
                            step["data"]["text"] = step["data"]["text"].replace("[PIPED_DATA]", piped_context)
                        else:
                            step["data"]["text"] += f"\n\n{piped_context}"
                    elif step.get("action") == "process_text":
                        step["data"]["text"] = piped_context
            return self._run_steps(steps, status_callback)

        plan = _fix_wrapper(raw)
        action = plan.get("action", "")
        data = plan.get("data", {})
        
        if piped_context:
            if "text" in data:
                if "[PIPED_DATA]" in data["text"]:
                    data["text"] = data["text"].replace("[PIPED_DATA]", piped_context)
                else:
                    data["text"] += f"\n\n{piped_context}"
            elif action == "process_text":
                data["text"] = piped_context
                
        if not action:
            return "❌ Could not understand the Docs command."
        return self._dispatch(action, data, status_callback)

    def _run_steps(self, steps: list, status_callback=None) -> str:
        """Execute multiple doc actions sequentially, forwarding the created doc title."""
        results = []
        last_created_title = None

        for step in steps:
            step = _fix_wrapper(step) if isinstance(step, dict) else {}
            action = step.get("action", "")
            data = dict(step.get("data", {}))

            # Auto-fill doc_name from the previously created doc
            if action == "append_to_doc" and last_created_title:
                dn = data.get("doc_name", "").lower().strip()
                if not dn or dn in ("it", "that", "that document", "the document",
                                    "same document", "this document"):
                    data["doc_name"] = last_created_title

            result = self._dispatch(action, data, status_callback)
            results.append(result)

            if action == "create_doc":
                # Update canonical title from what was actually just created
                last_created_title = data.get("title", "").strip()

        return "\n".join(results)

    def _dispatch(self, action: str, data: dict, status_callback=None) -> str:
        if action == "create_doc":
            if status_callback: status_callback(f"Creating document: {data.get('title', '')}")
            return self._create(data)
        elif action == "append_to_doc":
            if status_callback: status_callback(f"Writing to document: {data.get('doc_name', '')}")
            return self._append(data)
        elif action == "open_doc":
            if status_callback: status_callback(f"Opening document: {data.get('doc_name', '')}")
            return self._open(data)
        elif action == "read_doc":
            if status_callback: status_callback(f"Reading document: {data.get('doc_name', '')}")
            return self._read(data, status_callback)
        elif action == "process_text":
            return self._process_text(data, status_callback)
        else:
            return f"❌ Unknown Docs action: {action}"

    def _process_text(self, data: dict, status_callback=None) -> str:
        text = data.get("text", "").strip()
        instruction = data.get("instruction", "Summarize this text").strip()
        if not text:
            return "❌ No text provided to process."
        
        if status_callback: status_callback("Processing text...")
        sys_prompt = "You are a helpful assistant. Process the provided text according to the task and return ONLY the result. Do not wrap in quotes or add preamble."
        user_prompt = f"Source Text:\n{text}\n\nTask: {instruction}"
        
        try:
            from ai_backend import _call_ollama_chat
            ans = _call_ollama_chat(sys_prompt, user_prompt)
            return ans.strip()
        except Exception as e:
            return f"❌ Failed to process text: {e}"

    def _create(self, data: dict) -> str:
        title = data.get("title", "").strip()
        if not title:
            return "❌ Document title is required."
        try:
            res = create_doc(self.session, title=title)
            doc_id = res.get("documentId", "")
            url = f"https://docs.google.com/document/d/{doc_id}"
            return f"✅ Document created — \"{title}\"\n🔗 {url}"
        except Exception as e:
            return f"❌ Failed to create document: {e}"

    def _open(self, data: dict) -> str:
        """Open an existing Google Doc in the browser."""
        doc_name = data.get("doc_name", "").strip()
        if not doc_name:
            return "❌ Document name is required."
        try:
            doc_id = find_doc_id_by_name(self.session, doc_name)
            if not doc_id:
                return f"❌ Document \"{doc_name}\" not found. Check the document name."
            import webbrowser
            url = f"https://docs.google.com/document/d/{doc_id}/edit"
            webbrowser.open(url)
            return f"✅ Opened \"{doc_name}\" in your browser."
        except Exception as e:
            return f"❌ Failed to open document: {e}"

    def _append(self, data: dict) -> str:
        doc_name = data.get("doc_name", "").strip()
        text = data.get("text", "").strip()
        if not doc_name:
            return "❌ Document name is required."
        if not text:
            return "❌ No text provided to write."

        if text.startswith("SEARCH_WEB:"):
            topic = text.replace("SEARCH_WEB:", "").strip()
            print(f"   🌍 Getting current info: '{topic}'...")
            try:
                from general_agent import get_current_info
                text = get_current_info(topic, purpose=f"for a Google Doc named '{doc_name}'")
            except Exception:
                ctx = search_web(topic)
                if "No search results" in ctx or "Search failed" in ctx:
                    return f"❌ Web search failed for '{topic}'. Nothing written to document."
                text = call_ollama_raw(
                    f"Write a clear, informative summary based on these search results:\n\n"
                    f"RESULTS:\n{ctx}\n\n"
                    f"RULES:\n- Plain text only, no HTML\n- 3-5 sentences\n- Factual and concise"
                ).strip()
            if not text:
                return f"❌ Could not get information for '{topic}'."

        try:
            doc_id = find_doc_id_by_name(self.session, doc_name)
            if not doc_id:
                return f"❌ Document \"{doc_name}\" not found. Create it first."
            if not text.endswith("\n"):
                text += "\n"
            append_text_to_doc(self.session, doc_id, text)
            preview = text[:80].strip() + ("..." if len(text) > 80 else "")
            return f"✅ Written to \"{doc_name}\" — \"{preview}\""
        except Exception as e:
            return f"❌ Failed to write to document: {e}"

    def _read(self, data: dict, status_callback=None) -> str:
        doc_name = data.get("doc_name", "").strip()
        if not doc_name:
            return "❌ Document name is required."
        if status_callback: status_callback(f"Scanning document: {doc_name}")
        try:
            doc_id = find_doc_id_by_name(self.session, doc_name)
            if not doc_id:
                return f"❌ Document \"{doc_name}\" not found."
            raw = read_doc(self.session, doc_id)
            content = ""
            for elem in raw.get("body", {}).get("content", []):
                if "paragraph" in elem:
                    for el in elem["paragraph"]["elements"]:
                        content += el.get("textRun", {}).get("content", "")
            content = content.strip()
            if not content:
                return f"📄 \"{doc_name}\" is empty."
            return f"📄 Contents of \"{doc_name}\":\n\n{content}"
        except Exception as e:
            return f"❌ Failed to read document: {e}"