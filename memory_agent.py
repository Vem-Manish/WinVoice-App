import re
import threading
from memory_store import remember, recall
from ai_backend import call_ollama_raw


class MemoryAgent:
    def run(self, query: str, status_callback=None, piped_context: str = None) -> str:
        """Handles both memory recall ('What is my wifi password?') and storage ('Remember that...')"""
        q_lower = query.lower().strip("?.!")

        # 1. Detection: Is this an inquiry / recall question?
        is_recall = bool(re.search(
            r'^(what|who|where|when|which|how|do you remember|can you remember|recall|tell me)\b',
            q_lower
        )) and not bool(re.search(r'\b(remember that|make a note that|note that|don\'?t forget that)\b', q_lower))

        if is_recall:
            if status_callback:
                status_callback("Searching memory...")
            memories = recall(query, k=3)
            if not memories:
                return "I don't have any record of that in my memory yet."

            prompt = f"""You are WinVoice memory assistant. Answer the user's question accurately using ONLY the stored memory facts below.
If the facts don't answer the question, say you don't know. Keep your response concise (1-2 sentences).

Stored Memory Facts:
{chr(10).join(f"- {m}" for m in memories)}

User Question: "{query}"

Answer:"""
            ans = call_ollama_raw(prompt).strip()
            return ans if ans else f"According to my memory: {memories[0]}"

        # 2. Otherwise, this is a storage command ("Remember that ...")
        if status_callback:
            status_callback("Committing to memory...")
        prompt = f"""Extract the core fact or preference the user wants you to remember from this text.
Make it a concise third-person statement (e.g., "The user's favorite color is blue" or "The user's passport is in the top drawer").
Output ONLY the extracted fact, nothing else.

User text: "{query}" """

        fact = call_ollama_raw(prompt).strip()
        if not fact or len(fact) < 3:
            fact = query  # Fallback
        elif fact.lower() in {"ok", "yes", "no", "none", "sure", "got it", "noted", "understood"}:
            fact = query

        success = remember(fact)
        if success:
            return f"✅ Got it. I've committed this to memory: \"{fact}\""
        return "❌ Failed to save to long-term memory."


# --- IMPLICIT MEMORY EXTRACTOR ---

def _background_auto_memory(user_text: str):
    """Runs invisibly in the background to catch natural long-term facts only."""

    # Quick pre-filter: skip very long text (piped data / email dumps), or obvious commands
    if len(user_text) > 300:
        return

    _SKIP_KEYWORDS = [
        "create", "open", "send", "email", "write", "read", "play", "pause",
        "stop", "set", "timer", "alarm", "delete", "remove", "add", "schedule",
        "check", "search", "find", "show", "list", "get", "make", "calendar",
        "whatsapp", "spotify", "document", "sheet", "spreadsheet", "task",
        "weather", "volume", "brightness", "lock", "draft", "reply", "archive",
        "testing", "test", "summarize", "translate", "format", "extract",
        "what is", "who is", "how to", "tell me", "explain"
    ]
    lower = user_text.lower()
    if any(kw in lower for kw in _SKIP_KEYWORDS):
        return  # Almost certainly a command, not a personal fact

    prompt = f"""You are a critical-facts extraction AI. Your job is to find ONLY life-changing, permanent personal facts about the user.

STRICT RULES — Output 'NONE' unless ALL conditions are met:
1. The fact must be PERMANENT (not temporary, not a current action).
2. The fact must be PERSONAL (about the user themselves, their family, health, identity, strong preferences).
3. The fact must be CRITICAL (something worth remembering for months/years).

ALWAYS output NONE for:
- Any command, question, task, or instruction (e.g., "Play music", "What's the weather?")
- Testing, experimentation, or casual conversation
- Temporary states (e.g., "I'm tired", "I'm busy today")
- Vague or generic statements

VALID examples to save:
- "My name is Vemma" → "The user's name is Vemma"
- "I hate mushrooms" → "The user hates mushrooms"  
- "My wife is named Priya" → "The user's wife is named Priya"
- "I'm a software engineer at Google" → "The user is a software engineer at Google"
- "I'm allergic to peanuts" → "The user is allergic to peanuts"
- "My birthday is March 15" → "The user's birthday is March 15"

INVALID examples — output NONE:
- "Create a document called project launch" → NONE
- "What is the weather?" → NONE
- "I am testing winvoice" → NONE
- "I deleted the swimming event" → NONE
- "Send an email to Manish" → NONE
- "I am tired right now" → NONE

User message: "{user_text}"

Output ONLY the fact as a concise third-person statement, or the word NONE:"""

    try:
        result = call_ollama_raw(prompt).strip()
        # Strict validation of output
        if (result
                and result.upper() != "NONE"
                and len(result) > 10
                and len(result) < 200
                and "```" not in result
                and "error" not in result.lower()
                and not result.lower().startswith("none")
                and not result.lower().startswith("i ")  # Avoid AI speaking in first-person
        ):
            remember(result)
            print(f"\n[Auto-Memory] 🧠 Learned a new fact: {result}")
    except Exception:
        pass  # Fail silently


def trigger_auto_memory(user_text: str):
    """Fires off the memory analyzer in a thread and immediately returns."""
    # Only analyze if it looks like natural language (not a short command)
    words = user_text.split()
    if len(words) >= 4:  # Minimum 4 words to have any chance of being a personal fact
        threading.Thread(
            target=_background_auto_memory,
            args=(user_text,),
            daemon=True,
            name="AutoMemoryThread"
        ).start()