import json
import ast
import requests
import os
from dotenv import load_dotenv

load_dotenv()
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL    = os.getenv("OLLAMA_MODEL",    "qwen2.5:7b")


def initialize_ai(api_key=None):
    """No-op for compatibility. Verifies Ollama is reachable."""
    try:
        r = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=5)
        r.raise_for_status()
        # Pre-warm: load the model into RAM now so first real query is fast
        _warm_up_model()
        print(f"✅ Custom WinVoice AI connected: {ROUTER_MODEL}")
    except Exception as e:
        print(f"⚠️  Ollama not reachable at {OLLAMA_BASE_URL}: {e}")


def _warm_up_model():
    """
    Pre-loads both models into Ollama RAM so first real query has zero cold-start.
    Called once at startup from initialize_ai().
    """
    for model in set([OLLAMA_MODEL, ROUTER_MODEL]):
        try:
            requests.post(
                f"{OLLAMA_BASE_URL}/api/chat",
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                    "keep_alive": "30m",   # keep in RAM for 30 minutes
                    "options": {"num_predict": 1, "temperature": 0}
                },
                timeout=30
            )
            if model == ROUTER_MODEL:
                print(f"[Ollama] Warmed up custom router: {model}")
        except Exception:
            pass


def call_gemini(prompt: str) -> dict:
    """
    Drop-in replacement. Name kept so router.py needs zero changes.
    Uses the CHAT endpoint with keep_alive so the model stays hot.
    """
    return _call_ollama_chat(
        system="You are a JSON-only output machine. Output only valid JSON. No explanation. No markdown.",
        user=prompt
    )


ROUTER_MODEL = os.getenv("OLLAMA_ROUTER_MODEL", "qwen2.5:7b")

def call_gemini_smart(prompt: str, system_prompt: str = None) -> dict:

    sys_content = system_prompt or "You are a JSON-only output machine. Output only valid JSON. No explanation. No markdown. No code fences."
    payload = {
        "model": ROUTER_MODEL,
        "messages": [
            {"role": "system", "content": sys_content},
            {"role": "user",   "content": prompt}
        ],
        "format": "json",
        "stream": False,
        "keep_alive": -1,
        "options": {
            "temperature": 0,
            "num_predict": 512,   # Reduced from 2048: prevents huge VRAM buffer allocation which causes CPU swapping/lag
            "num_ctx": 2048,      # Kept at 2048 as requested for context
        }
    }
    try:
        response = requests.post(
            f"{OLLAMA_BASE_URL}/api/chat",
            json=payload,
            timeout=150  # raised: large context windows take longer on local hardware
        )
        response.raise_for_status()
        text = response.json()["message"]["content"].strip()
        print(f"\n[DEBUG SMART ROUTER OUTPUT]:\n{text}\n")
        return _parse_json(text)
    except requests.exceptions.ConnectionError:
        print("\n[Ollama Error] Cannot connect. Run: ollama serve")
        return {}
    except Exception as e:
        print(f"\n[Ollama Smart Router Error] {e}")
        return {}


def _call_ollama_chat(system: str, user: str) -> dict:
    """
    Uses /api/chat (not /api/generate).
    keep_alive: model stays loaded between requests — eliminates reload delay.
    """
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user}
        ],
        "format": "json",
        "stream": False,
        "keep_alive": -1,
        "options": {
            "temperature": 0,
            "num_predict": 1024,
            "num_ctx": 4096,
        }
    }

    try:
        response = requests.post(
            f"{OLLAMA_BASE_URL}/api/chat",
            json=payload,
            timeout=60
        )
        response.raise_for_status()
        text = response.json()["message"]["content"].strip()
        print(f"\n[DEBUG RAW OUTPUT]:\n{text}\n")
        return _parse_json(text)

    except requests.exceptions.ConnectionError:
        print("\n[Ollama Error] Cannot connect. Run: ollama serve")
        return {}
    except Exception as e:
        print(f"\n[Ollama Error] {e}")
        return {}


def call_ollama_raw(prompt: str) -> str:
    """Returns raw text (used for email body writing)."""
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "keep_alive": -1,
        "options": {
            "temperature": 0.4,
            "num_predict": 2048,
            "num_ctx": 4096
        }
    }
    try:
        r = requests.post(f"{OLLAMA_BASE_URL}/api/chat", json=payload, timeout=60)
        r.raise_for_status()
        return r.json().get("message", {}).get("content", "").strip()
    except Exception as e:
        print(f"\n[Ollama Raw Error] {e}")
        return ""


def call_ollama_raw_stream(prompt: str, system: str = ""):
    """Yields raw text chunks as they are generated by Ollama."""
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "keep_alive": -1,
        "options": {
            "temperature": 0.4,
            "num_predict": 2048,
            "num_ctx": 4096
        }
    }
    try:
        with requests.post(f"{OLLAMA_BASE_URL}/api/chat", json=payload, timeout=60, stream=True) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if line:
                    chunk = json.loads(line)
                    if "message" in chunk and "content" in chunk["message"]:
                        yield chunk["message"]["content"]
    except requests.exceptions.ConnectionError:
        print("\n[Ollama Stream Error] Cannot connect. Run: ollama serve")
    except Exception as e:
        print(f"\n[Ollama Raw Stream Error] {e}")


def _parse_json(text: str) -> dict:
    # Strip markdown fences
    text = text.strip()
    if "```" in text:
        parts = text.split("```")
        for i in range(1, len(parts), 2):
            block = parts[i].strip()
            if block.lower().startswith("json"):
                block = block[4:].strip()
            if block.startswith("{") or block.startswith("["):
                text = block
                break
        else:
            # Fallback if no block looked like JSON
            if len(parts) > 1:
                text = parts[1].strip()
                if text.lower().startswith("json"):
                    text = text[4:].strip()

    # Extract first complete {...} block using bracket stack
    start = text.find("{")
    if start == -1:
        print(f"\n[Parse Error] No JSON object found in:\n{text}")
        return {}

    stack = []
    in_string = False
    escape = False
    end = -1
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == '\\' and in_string:
            escape = True
            continue
        if ch == '"' and not escape:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == '{':
            stack.append('{')
        elif ch == '[':
            stack.append('[')
        elif ch == '}':
            if stack and stack[-1] == '{':
                stack.pop()
            if not stack:
                end = i
                break
        elif ch == ']':
            if stack and stack[-1] == '[':
                stack.pop()
            if not stack:
                end = i
                break

    json_str = text[start:end + 1] if end != -1 else text[start:]

    if end == -1:
        # Auto-fix: close string if needed, then close brackets based on stack
        if in_string:
            json_str += '"'
        while stack:
            top = stack.pop()
            if top == '{':
                json_str += '}'
            elif top == '[':
                json_str += ']'
        print(f"\n[Auto-Fixed] Unmatched braces. Recovered JSON:\n{json_str}")

    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        print(f"\n[Parse Error] Invalid JSON: {e}\nText was:\n{json_str}")
        return {}