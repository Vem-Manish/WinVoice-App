









# general_agent.py
# Handles: general chat, greetings, general knowledge, current events, and
# any query that needs up-to-date web information.
# Used directly for chat and also as a sub-service by other agents
# (gmail, docs, etc.) when they need fresh web content.

import json
import requests as _requests
from datetime import datetime
from ai_backend import call_ollama_raw, _call_ollama_chat
from tools import search_web

GENERAL_SYSTEM_PROMPT = """You are WinVoice, an advanced, highly intelligent AI assistant inspired by Jarvis.
You are concise, exceedingly professional, dryly witty, and maintain a subtle British undertone.

You can help with:
- Greetings and casual conversation
- General knowledge questions
- Current events and news (you will be given live search results)
- Writing summaries, explanations, and analysis

RULES:
- Address the user respectfully but not overly submissively (e.g., "sir" is acceptable sparingly).
- NEVER use generic AI filler phrases (e.g., "As an AI language model", "I can certainly help with that", "Here is the information").
- Keep responses extremely concise. If a one-sentence answer suffices, use one sentence.
- If [User context: ...] is provided, naturally weave that knowledge into your response as if you've always known it. Never explicitly mention "the context provided".
- If web search results are provided, use them as your primary source of truth.
- Always cite the approximate date of information when relevant.
- Format responses in clean plain text, no markdown symbols like ** or ##.
- Maintain a calm, collected, and highly capable demeanor at all times.
- PIPED DATA: If you receive "[CONTEXT DATA FROM PREVIOUS STEP]", you MUST use that text as the source material for your task (e.g., if asked to summarize, summarize that text).
"""

# Query classifier prompt
CLASSIFIER_PROMPT = """Classify this query into one of these categories. Output ONLY the category name, nothing else.

Categories:
- greeting: hi, hello, how are you, good morning, etc.
- smalltalk: casual conversation, jokes, opinions
- general_knowledge: facts, history, science, math, definitions (no need for web)
- current_events: news, current status, latest updates, recent happenings, war status, elections, sports results, stock prices, weather-independent
- web_search_needed: any query that needs fresh/current information to answer accurately

Query: {query}

Category:"""


def _classify_query(query: str) -> str:
    """Classify what kind of response is needed."""
    try:
        result = call_ollama_raw(CLASSIFIER_PROMPT.format(query=query))
        result = result.strip().lower().split()[0] if result.strip() else "web_search_needed"
        valid = {"greeting", "smalltalk", "general_knowledge", "current_events", "web_search_needed"}
        return result if result in valid else "web_search_needed"
    except Exception:
        return "web_search_needed"


def _get_greeting_response(query: str) -> str:
    """Handle greetings with a short, friendly response."""
    hour = datetime.now().hour
    time_of_day = "morning" if hour < 12 else ("afternoon" if hour < 17 else "evening")

    prompt = f"""The user said: "{query}"
Respond naturally and briefly as WinVoice, a helpful voice assistant. 
Mention the time of day ({time_of_day}) if it's a greeting.
Keep it under 2 sentences. Be warm and friendly."""
    return call_ollama_raw(prompt).strip()


def get_web_answer(query: str, context_hint: str = "") -> str:
    """
    Core function: search the web for a query and return a clean,
    well-formatted answer. Used by general_agent AND other agents
    (gmail, docs, etc.) when they need current info.

    context_hint: optional context for the summarizer
                  e.g. "for an email to Manish" or "for a Google Doc"
    """
    import re
    clean_query = re.sub(r'\[User Context[^\]]+\]\n?', '', query).strip()
    print(f"   🌍 Web searching: '{clean_query}'...")

    # Always append the current year so DDGS prioritises recent results
    today = datetime.now().strftime("%B %d, %Y")   # e.g. "June 05, 2026"
    year  = datetime.now().year
    dated_query = f"{clean_query} {year}"

    search_results = search_web(dated_query)

    if not search_results or "No search results" in search_results or "Search failed" in search_results:
        # Fallback: try alternate query
        search_results = search_web(clean_query)

    if not search_results or "No search results" in search_results:
        return f"I searched for '{clean_query}' but couldn't find current results. Please check a news source for the latest information."

    context_str = f" The response will be used {context_hint}." if context_hint else ""

    prompt = f"""You are summarizing web search results for a user query.{context_str}

TODAY'S DATE: {today}
IMPORTANT: Use this date as "today". Do NOT use any other date you may have learned during training.

USER QUERY: {query}

SEARCH RESULTS:
{search_results}

INSTRUCTIONS:
- Answer the query directly and clearly based on the search results
- Include the most recent/relevant information from the results
- If you mention a date, make sure it is from the search results above — never invent a date
- Write in plain text, no markdown symbols like ** or ##
- Keep it concise: 3-5 sentences for simple queries, up to 8 for complex ones
- If the search results don't fully answer the query, say so honestly
- Start directly with the answer, no preamble like "Based on the search results..."

ANSWER:"""

    answer = call_ollama_raw(prompt).strip()
    return answer if answer else "I found some results but couldn't summarize them properly. Please try rephrasing your question."


def format_agent_output(raw_data: str, query: str, output_type: str = "general") -> str:
    """
    Takes raw agent output (contacts list, emails, calendar events, etc.)
    and passes it through a formatting layer to produce human-readable output.

    output_type: "contacts", "emails", "calendar", "tasks", "sheets", "general"
    """
    if not raw_data or not raw_data.strip():
        return raw_data

    # Don't reformat short success/error messages
    if len(raw_data) < 100 and (raw_data.startswith("✅") or raw_data.startswith("❌") or raw_data.startswith("⏱️")):
        return raw_data

    type_instructions = {
        "contacts": "Format this as a clean numbered list of contacts. Each contact on its own line showing name, email (if available), and phone (if available).",
        "emails": "Format these emails as a clean numbered list. For each email show: sender name, subject, and date. Add a brief 1-sentence summary of the snippet if available.",
        "calendar": "Format these calendar events as a clean numbered list. Show time and event title clearly.",
        "tasks": "Format these tasks as a clean numbered checklist. Show task title and due date if available.",
        "sheets": "Present this spreadsheet data in a readable format.",
        "general": "Present this information in a clear, readable way.",
    }

    instruction = type_instructions.get(output_type, type_instructions["general"])

    prompt = f"""The user asked: "{query}"
Here is the raw data from the system:

{raw_data}

Task: {instruction}

Rules:
- Use plain text only, no markdown ** or ## symbols
- Use emojis for visual separation (📧 for emails, 📞 for phones, 📅 for dates, etc.)
- Be concise but complete
- If there's nothing to show, say so clearly
- Start directly with the formatted content

FORMATTED OUTPUT:"""

    formatted = call_ollama_raw(prompt).strip()
    return formatted if formatted else raw_data


class GeneralAgent:
    """
    Handles general queries, chat, and current events.
    Also provides web search services to other agents.
    """

    def run(self, query: str, status_callback=None, piped_context: str = None) -> str:
        query = query.strip()
        if not query:
            return "I didn't catch that. What would you like to know?"

        # ── Piped context bypass ──────────────────────────────────────────────
        # If the query contains injected context data from a previous step,
        # skip the web classifier entirely and process it directly with Ollama.
        # This prevents the agent from web-searching email dumps, spreadsheet
        # data, or document content that was piped in from another agent.
        _CONTEXT_MARKER = "[CONTEXT DATA FROM PREVIOUS STEP"
        if _CONTEXT_MARKER in query:
            if status_callback: status_callback("Processing piped data...")
            print("   🔗 Piped context detected — bypassing web search, processing directly")

            # Split out the actual task instruction (after [TASK]) from the context
            task_part = query
            if "[TASK]" in query:
                parts = query.split("[TASK]", 1)
                context_blob = parts[0].strip()
                task_instruction = parts[1].strip()
            else:
                context_blob = query
                task_instruction = query

            # Strip user context noise from the task instruction if present
            if "[User Context" in task_instruction:
                task_instruction = task_instruction.split("\n")[-1].strip()

            prompt = f"""You are a helpful AI processing assistant. You have been given data from a previous step and a task to perform on it.

INSTRUCTIONS:
- Perform ONLY the task described below on the provided data.
- Do NOT web search. Do NOT make up information not present in the data.
- Be concise and accurate. Output plain text only, no markdown symbols like ** or ##.

DATA:
{context_blob}

TASK: {task_instruction}

OUTPUT:"""
            result = call_ollama_raw(prompt).strip()
            return result if result else f"I processed the data but couldn't complete: {task_instruction}"

        # ── Normal path: classify and route ──────────────────────────────────
        if status_callback: status_callback("Classifying query intent...")
        category = _classify_query(query)
        print(f"   🧠 Query category: {category}")

        if category == "greeting":
            if status_callback: status_callback("Responding...")
            return _get_greeting_response(query)

        if category == "smalltalk":
            if status_callback: status_callback("Thinking...")
            prompt = f'The user said: "{query}". Respond as WinVoice, a helpful AI assistant. Be friendly and concise (2-3 sentences max).'
            return call_ollama_raw(prompt).strip()

        if category == "general_knowledge":
            if status_callback: status_callback("Retrieving knowledge...")
            prompt = f"""Answer this question clearly and concisely as WinVoice: \"{query}\"
Rules: plain text, no markdown, 3-5 sentences max, factual and direct."""
            answer = call_ollama_raw(prompt).strip()
            # Force web search for anything time-sensitive
            _time_sensitive = [
                "current", "latest", "recent", "now", "today", "status",
                "war", "election", "price", "stock", "market", "share",
                "crypto", "bitcoin", "nifty", "sensex", "bse", "nse",
                "news", "update", "2024", "2025", "2026",
                "rupee", "dollar", "forex", "gold", "oil",
            ]
            if any(word in query.lower() for word in _time_sensitive):
                if status_callback: status_callback("Searching the web for latest info...")
                return get_web_answer(query)
            return answer

        # current_events or web_search_needed — always search
        if status_callback: status_callback("Searching the web...")
        return get_web_answer(query)


# ── Convenience functions used by other agents ─────────────────────────

def get_current_info(topic: str, purpose: str = "") -> str:
    """
    Called by gmail_agent, docs_agent etc. when they need
    up-to-date information on a topic.
    Returns a clean text summary suitable for inclusion in emails/docs.
    """
    return get_web_answer(topic, context_hint=purpose)


def format_for_display(raw_data: str, query: str, data_type: str = "general") -> str:
    """
    Called by contacts, email listing, calendar listing etc.
    to format raw data into human-readable output.
    """
    return format_agent_output(raw_data, query, data_type)