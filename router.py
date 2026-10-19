# router.py
import re
from ai_backend import call_gemini_smart

# ═══════════════════════════════════════════════════════════════════════════
# ⚡ FAST MATCHER — Zero-latency routing for simple, single-step commands.
#    Covers ~70% of everyday usage. Falls through to qwen2.5:7b for anything
#    complex, ambiguous, or multi-step.
# ═══════════════════════════════════════════════════════════════════════════

# Known apps → executable/search name (desktop apps ONLY — no web services here)
_APP_MAP = {
    "chrome":       "chrome",     "firefox":    "firefox",
    "edge":         "edge",       "brave":      "brave",
    "discord":      "discord",    "slack":      "slack",
    "teams":        "teams",      "zoom":       "zoom",
    "notion":       "notion",     "obsidian":   "obsidian",
    "vscode":       "vscode",     "vs code":    "vscode",
    "code":         "vscode",     "pycharm":    "pycharm",
    "notepad":      "notepad",    "calculator": "calculator",
    "word":         "word",       "excel":      "excel",
    "powerpoint":   "powerpoint", "outlook":    "outlook",
    "telegram":     "telegram",   "steam":      "steam",
    "file explorer": "explorer",  "explorer":   "explorer",
    "settings":     "settings",   "task manager": "taskmgr",
    "paint":        "paint",      "vlc":        "vlc",
    "photos":       "photos",     "spotify":    "spotify",
}

# Known websites
_URL_MAP = {
    "youtube":    "https://www.youtube.com",
    "gmail":      "https://mail.google.com",
    "google":     "https://www.google.com",
    "github":     "https://www.github.com",
    "reddit":     "https://www.reddit.com",
    "twitter":    "https://www.twitter.com",
    "x":          "https://www.x.com",
    "linkedin":   "https://www.linkedin.com",
    "instagram":  "https://www.instagram.com",
    "facebook":   "https://www.facebook.com",
    "netflix":    "https://www.netflix.com",
    "notion":     "https://www.notion.so",
    "drive":      "https://drive.google.com",
    "docs":       "https://docs.google.com",
    "sheets":     "https://sheets.google.com",
    "calendar":   "https://calendar.google.com",
    "meet":       "https://meet.google.com",
    "whatsapp":   "https://web.whatsapp.com",
    "amazon":     "https://www.amazon.com",
    "maps":       "https://maps.google.com",
    "chatgpt":    "https://chat.openai.com",
    "perplexity": "https://www.perplexity.ai",
    "spotify":    "https://open.spotify.com",
    "hotstar":    "https://www.hotstar.com",
    "primevideo": "https://www.primevideo.com",
    "prime":      "https://www.primevideo.com",
}

# Services that should ALWAYS open as a website, never as a desktop app
_WEB_FIRST = {
    "youtube", "gmail", "google", "github", "reddit", "twitter", "x",
    "linkedin", "instagram", "facebook", "netflix", "drive", "docs",
    "sheets", "calendar", "meet", "whatsapp", "amazon", "maps",
    "chatgpt", "perplexity", "hotstar", "prime", "primevideo", "notion",
}

def _step(action: str, **kwargs) -> dict:
    return {"action": action, **kwargs}

def _plan(*steps) -> dict:
    return {"steps": list(steps)}

def _fast_match(q: str) -> dict | None:
    """
    Tries to match the query against known simple patterns.
    Returns a full plan dict if matched, None if it should fall through to the LLM.
    """
    raw = q.strip()
    t = raw.lower().strip("?.!")

    # ── COMPOUND FILE PIPELINES (Zero-latency fast match) ─────────────────
    def _clean_extracted_file_target(s: str) -> str:
        s = (s or "").strip()
        s = re.sub(r'^(.*?)\s+(?:find|locate|get)\s+(?:the\s+file\s+|file\s+|the\s+)?\1$', r'\1', s, flags=re.I).strip()
        s = re.sub(r'\b(.{4,40}?)\s+\1\b', r'\1', s, flags=re.I).strip()
        s = re.sub(r'^(?:find|locate|get|search\s+for)\s+(?:the\s+file\s+|file\s+|the\s+|a\s+|my\s+)?', '', s, flags=re.I).strip()
        s = re.sub(r'\s+(the\s+file|file|document|doc)$', '', s, flags=re.I).strip()
        return s

    # 1. "find <file> and email/mail it to <person> [saying <msg>]"
    m_fe = re.search(r'\b(find|locate|get)\s+(?:the\s+file\s+|file\s+|the\s+|my\s+)?(.+?)\s+and\s+(?:email|mail|send\s+email\s+to|send\s+it\s+via\s+email\s+to|send\s+an\s+email\s+to)\s+(?:it\s+to\s+)?([a-zA-Z0-9_\.\@\s]+?)(?:\s+(?:saying|with\s+message|with\s+body|that)\s+(.+))?$', t)
    if m_fe:
        file_target = _clean_extracted_file_target(m_fe.group(2))
        recipient = m_fe.group(3).strip()
        custom_msg = m_fe.group(4).strip() if m_fe.group(4) else None
        q2 = f"email to {recipient} saying {custom_msg}" if custom_msg else f"email to {recipient}"
        return _plan(
            _step("FILES_TASK", query=f"find {file_target}"),
            _step("GMAIL_TASK", query=q2, context_from_step=1)
        )

    # 2. "find <file> and (send it on whatsapp to|whatsapp it to|whatsapp) <person> [saying <msg>]"
    m_fw = re.search(r'\b(find|locate|get)\s+(?:the\s+file\s+|file\s+|the\s+|my\s+)?(.+?)\s+and\s+(?:whatsapp(?:\s+it)?(?:\s+to)?|send(?:\s+it)?\s+(?:on\s+whatsapp\s+to|via\s+whatsapp\s+to|to))\s+([a-zA-Z0-9_\s]+?)(?:\s+on\s+whatsapp|\s+via\s+whatsapp)?(?:\s+(?:saying|with\s+message|that)\s+(.+))?$', t)
    if m_fw:
        file_target = _clean_extracted_file_target(m_fw.group(2))
        recipient = m_fw.group(3).strip()
        custom_msg = m_fw.group(4).strip() if m_fw.group(4) else None
        q2 = f"send it to {recipient} on whatsapp saying {custom_msg}" if custom_msg else f"send it to {recipient} on whatsapp"
        return _plan(
            _step("FILES_TASK", query=f"find {file_target}"),
            _step("WHATSAPP_TASK", query=q2, context_from_step=1)
        )

    # 3. "email <person> the file <file> [saying <msg>]"
    m_ef = re.search(r'\b(email|mail|send\s+an\s+email\s+to)\s+([a-zA-Z0-9_\.\@]+)\s+(?:the\s+file\s+|file\s+)(.+?)(?:\s+(?:saying|with\s+message|with\s+body|that)\s+(.+))?$', t)
    if m_ef and not any(w in t for w in ("inbox", "search", "check")):
        recipient = m_ef.group(2).strip()
        file_target = _clean_extracted_file_target(m_ef.group(3))
        custom_msg = m_ef.group(4).strip() if m_ef.group(4) else None
        q2 = f"email to {recipient} saying {custom_msg}" if custom_msg else f"email to {recipient}"
        return _plan(
            _step("FILES_TASK", query=f"find {file_target}"),
            _step("GMAIL_TASK", query=q2, context_from_step=1)
        )

    # 4a. "send the file <file> to <person> on whatsapp [saying <msg>]"
    m_sfw = re.search(r'\b(send|forward)\s+(?:the\s+file\s+|file\s+)(.+?)\s+(?:to\s+([a-zA-Z0-9_]+)\s+on\s+whatsapp|on\s+whatsapp\s+to\s+([a-zA-Z0-9_]+))(?:\s+(?:saying|with\s+message|that)\s+(.+))?$', t)
    if m_sfw:
        file_target = _clean_extracted_file_target(m_sfw.group(2))
        recipient = (m_sfw.group(3) or m_sfw.group(4)).strip()
        custom_msg = m_sfw.group(5).strip() if m_sfw.group(5) else None
        q2 = f"send it to {recipient} on whatsapp saying {custom_msg}" if custom_msg else f"send it to {recipient} on whatsapp"
        return _plan(
            _step("FILES_TASK", query=f"find {file_target}"),
            _step("WHATSAPP_TASK", query=q2, context_from_step=1)
        )

    # 4b. "whatsapp <person> the file <file> [saying <msg>]" / "send <person> the file <file> on whatsapp"
    m_wf = re.search(r'\b(whatsapp|send)\s+([a-zA-Z0-9_]+)\s+(?:the\s+file\s+|file\s+)(.+?)(?:\s+on\s+whatsapp)?(?:\s+(?:saying|with\s+message|that)\s+(.+))?$', t)
    if m_wf and ("whatsapp" in t or "send" in t) and m_wf.group(2) not in ("the", "a", "my", "this", "that"):
        recipient = m_wf.group(2).strip()
        file_target = _clean_extracted_file_target(m_wf.group(3))
        custom_msg = m_wf.group(4).strip() if m_wf.group(4) else None
        q2 = f"send it to {recipient} on whatsapp saying {custom_msg}" if custom_msg else f"send it to {recipient} on whatsapp"
        return _plan(
            _step("FILES_TASK", query=f"find {file_target}"),
            _step("WHATSAPP_TASK", query=q2, context_from_step=1)
        )

    # 5. Standalone file search / open
    if re.search(r'^(find|search for|locate|where is)\s+(the\s+file\s+|file\s+|my\s+file\s+|the\s+|a\s+|my\s+)(.+)$', t) and \
       not any(w in t for w in ("youtube", "google", "weather", "song", "music", "email", "mail", "event")):
        return _plan(_step("FILES_TASK", query=raw))

    if re.search(r'^(open|launch)\s+(the\s+file\s+|file\s+|my\s+file\s+)(.+)$', t):
        return _plan(_step("FILES_TASK", query=raw))

    # ── Complexity guard — bail out immediately on multi-step prompts ─────────
    # If the sentence has strong piping/chaining language, don't try to fast-match
    _pipeline_signals = [
        r'\b(and then|after that|then also|and also)\b',
        r'\b(and|then)\b\s+(email|send|save|write|add|text|message|create|schedule|draft|put|check|translate|summariz|extract|find)\b',
        r'\b(extract|summariz|bullet points?|translate)\b',
        r'\b(as tasks?|as a task|to my to.?do|to tasks?|to the to.?do)\b',
        r'\b(from the (email|mail|notes|doc|document|sheet|spreadsheet)|in the (doc|sheet|email))\b.*\b(and|then|to|into|create|save|send|email|schedule)\b',
        r'\bwith the (forecast|weather|summary|details|data|info|content|extracted)\b',
        r'\b(and|then)\b.*\b(it|them|that)\b.*\b(to|in|on)\b',
    ]
    if any(re.search(p, t) for p in _pipeline_signals):
        return None

    # Compound independent multi-step splitter (e.g. "open gmail, open youtube, open chatgpt", "open whatsapp open gmail open chatgpt", "open spotify and open youtube", "lock pc, mute")
    compound_pattern = r'\s*(?:[,;]|\b(?:and|then)\b)\s*|(?<=\w)\s+(?=\b(?:open|launch|start|run|close)\s+)'
    parts = [p.strip() for p in re.split(compound_pattern, t) if p.strip()]
    if len(parts) >= 2:
        # Ensure none of the subsequent parts have back-references to previous outputs
        has_reference = any(
            re.search(r'\b(it|them|that|the (summary|result|doc|sheet|email|forecast|data|items?|tasks?))\b', p)
            for p in parts[1:]
        )
        if not has_reference:
            all_steps = []
            all_matched = True
            for part in parts:
                pm = _fast_match(part)
                # If bare target after an 'open' chain (e.g., "open gmail, youtube, chatgpt")
                if not pm and not part.startswith(('open ', 'launch ')):
                    pm = _fast_match('open ' + part)
                if pm and pm.get("steps"):
                    all_steps.extend(pm["steps"])
                else:
                    all_matched = False
                    break
            if all_matched and len(all_steps) >= len(parts):
                return _plan(*all_steps)

    _complex_signals = [
        r'\band then\b', r'\bafter that\b', r'\bthen\b.*\band\b',
        r'\bextract\b', r'\bsummariz', r'\bformat\b.*\bdoc',
        r'\bpipe\b', r'\bcontext\b', r'\bfrom the email\b',
        r'\badd it to\b', r'\bappend\b', r'\bcreate a doc\b',
        r'\bwrap.?up\b', r'\breport\b',
    ]
    if sum(1 for p in _complex_signals if re.search(p, t)) >= 2:
        return None
    # Also bail if there are 3+ distinct action verbs
    _action_verbs = ['open', 'play', 'set', 'send', 'text', 'read', 'check',
                     'create', 'schedule', 'delete', 'lock', 'mute', 'type',
                     'press', 'remind', 'search', 'show', 'get', 'write', 'add', 'save']
    if sum(1 for v in _action_verbs if re.search(rf'\b{v}\b', t)) >= 3:
        return None

    # ── SYSTEM CONTROL ────────────────────────────────────────────────────────
    if re.search(r'\b(turn off|switch off|turn_off)\b.*\b(monitor|screen|display)\b|\b(monitor|screen|display)\s+off\b', t):
        return _plan(_step("NATIVE", command="SCREEN_OFF", query=t))

    if re.search(r'\block\b.*\bpc\b|\block\b.*\bscreen\b|\block\b.*\bcomputer\b|\block (my )?(pc|screen|computer)\b', t):
        return _plan(_step("NATIVE", command="LOCK_PC", query="lock my pc"))

    if m := re.search(r'\b(mute|unmute)\b', t):
        cmd = "MUTE" if m.group(1) == "mute" else "UNMUTE"
        return _plan(_step("NATIVE", command=cmd, query=t))

    # SHUTDOWN
    if (re.search(r'\b(shut ?down|power ?off|power ?down)\b', t) or
        re.search(r'\b(turn|switch)\s+off\s+(the\s+|my\s+)?(pc|computer|laptop|system|machine)\b', t)) and \
       not any(w in t for w in ("monitor", "screen", "display", "wifi", "bluetooth", "light", "lights")):
        return _plan(_step("NATIVE", command="SHUTDOWN", query=t))

    # RESTART
    if re.search(r'\b(restart|reboot)\b', t) and \
       not any(w in t for w in ("router", "wifi", "app", "application", "browser", "chrome")):
        return _plan(_step("NATIVE", command="RESTART", query=t))

    # SLEEP
    if (re.search(r'\b(put|send)\b.*(pc|computer|laptop|system).*sleep\b|\bgo to sleep\b|\bsleep\s+(the\s+|my\s+)?(pc|computer|laptop|system)\b|\b(enter|go into)\s+sleep\s+mode\b|^(please\s+)?(sleep|sleep now)$', t) or
        (re.search(r'\bsleep\b', t) and any(w in t for w in ("pc", "computer", "laptop", "system", "windows", "device")))) and \
       not any(w in t for w in ("song", "music", "playlist", "track", "timer", "alarm")):
        return _plan(_step("NATIVE", command="SLEEP", query=t))

    # Volume
    if m := re.search(r'\b(set|change|turn|put)\b.*\bvolume\b.*\bto\b\s*(\d+)|volume\b.*\bto\b\s*(\d+)|\bvolume\b\s+(\d+)', t):
        level = m.group(2) or m.group(3) or m.group(4)
        return _plan(_step("NATIVE", command="SET_VOLUME", level=int(level), query=t))

    if re.search(r'\b(increase|raise|turn up|crank up)\b.*\bvolume\b|\bvolume\b.*\b(up|higher|louder)\b', t):
        return _plan(_step("NATIVE", command="VOLUME_UP", query=t))

    if re.search(r'\b(decrease|lower|turn down|reduce)\b.*\bvolume\b|\bvolume\b.*\b(down|lower|quieter)\b', t):
        return _plan(_step("NATIVE", command="VOLUME_DOWN", query=t))

    if re.search(r'\bmax(imum)?\b.*\bvolume\b|\bvolume\b.*\b(max|full)\b|\bfull volume\b', t):
        return _plan(_step("NATIVE", command="SET_VOLUME", level=100, query=t))

    # Brightness
    if m := re.search(r'\b(set|change|turn|put)\b.*\bbrightness\b.*\bto\b\s*(\d+)|brightness\b.*\bto\b\s*(\d+)|\bbrightness\b\s+(\d+)', t):
        level = m.group(2) or m.group(3) or m.group(4)
        return _plan(_step("NATIVE", command="BRIGHTNESS", level=int(level), query=t))

    if re.search(r'\b(increase|raise|turn up)\b.*\bbrightness\b|\bbrightness\b.*\b(up|higher)\b', t):
        return _plan(_step("NATIVE", command="BRIGHTNESS_UP", query=t))

    if re.search(r'\b(decrease|lower|turn down|reduce|dim)\b.*\bbrightness\b|\bbrightness\b.*\b(down|lower)\b', t):
        return _plan(_step("NATIVE", command="BRIGHTNESS_DOWN", query=t))

    if re.search(r'\bmax(imum)?\b.*\bbrightness\b|\bbrightness\b.*\b(max|full)\b|\bfull brightness\b', t):
        return _plan(_step("NATIVE", command="BRIGHTNESS", level=100, query=t))

    # ── YOUTUBE ──────────────────────────────────────────────────────────────
    if re.search(r'\byoutube\b', t):
        import urllib.parse
        # 1. "search youtube for <query>" or "search on youtube for <query>"
        if m := re.search(r'\bsearch\s+(?:on\s+)?youtube\s+(?:for\s+)?(.+)', raw, re.I):
            q_clean = m.group(1).strip()
            if q_clean:
                return _plan(_step("OPEN_URL", url=f"https://www.youtube.com/results?search_query={urllib.parse.quote_plus(q_clean)}"))

        # 2. "open/play/search/watch/find/show <query> on/in/from youtube"
        if m := re.search(r'\b(?:open|play|search|watch|find|show|look\s+up|see)\s+(.+?)\s+(?:on|in|from|at)\s+youtube\b', raw, re.I):
            q_clean = m.group(1).strip()
            q_clean = re.sub(r'^for\s+', '', q_clean, flags=re.I).strip()
            if q_clean:
                return _plan(_step("OPEN_URL", url=f"https://www.youtube.com/results?search_query={urllib.parse.quote_plus(q_clean)}"))

        # 3. "youtube <query>" (e.g. "youtube gowtham videos")
        if m := re.search(r'^youtube\s+(?:search\s+)?(.+)', raw, re.I):
            q_clean = m.group(1).strip()
            if q_clean:
                return _plan(_step("OPEN_URL", url=f"https://www.youtube.com/results?search_query={urllib.parse.quote_plus(q_clean)}"))

        # 4. "<query> on/in youtube" (without explicit verb, e.g. "gowtham videos on youtube")
        if m := re.search(r'^(?:the\s+)?(.+?)\s+(?:on|in)\s+youtube\b', raw, re.I):
            q_clean = m.group(1).strip()
            if q_clean and q_clean.lower() not in ("open", "go to", "navigate to", "visit", "launch"):
                return _plan(_step("OPEN_URL", url=f"https://www.youtube.com/results?search_query={urllib.parse.quote_plus(q_clean)}"))

        # 5. Default/plain: "open youtube", "go to youtube", etc.
        # Only match if query does not mention other apps or services
        _other_services = ("gmail", "chatgpt", "whatsapp", "spotify", "slack", "discord", "teams", "zoom", "notion", "email", "mail", "doc", "sheet", "calendar", "weather", "task", "chrome", "vscode")
        if not any(w in t for w in _other_services):
            return _plan(_step("OPEN_URL", url="https://www.youtube.com"))

    # ── OPEN APP / WEBSITE ────────────────────────────────────────────────────
    if m := re.search(r'\b(open|launch|start|run|go to|navigate to|take me to|visit|browse to)\b\s+(.+)', t):
        target = m.group(2).strip().rstrip(".")
        # Web-first: always open as URL (no app lookup)
        if target in _WEB_FIRST and target in _URL_MAP:
            return _plan(_step("OPEN_URL", url=_URL_MAP[target]))
        # Check URL map for anything that looks like a known site
        if target in _URL_MAP:
            return _plan(_step("OPEN_URL", url=_URL_MAP[target]))
        # Fall back to desktop app
        if target in _APP_MAP:
            return _plan(_step("OPEN_APP", app=_APP_MAP[target], query=t))
        # Raw URL typed by user
        if re.search(r'\b\w+\.(com|org|net|io|co|ai|dev|gov|in)\b', target):
            clean = target if target.startswith("http") else f"https://{target}"
            return _plan(_step("OPEN_URL", url=clean))

    # ── OPEN WEBSITE (explicit go-to phrases) ────────────────────────────────
    if m := re.search(r'\b(go to|navigate to|take me to|visit|browse to)\b\s+(.+)', t):
        target = m.group(2).strip().rstrip(".")
        for k, url in _URL_MAP.items():
            if k in target:
                return _plan(_step("OPEN_URL", url=url))
        if re.search(r'\b\w+\.(com|org|net|io|co|ai|dev|gov|in)\b', target):
            clean = target if target.startswith("http") else f"https://{target}"
            return _plan(_step("OPEN_URL", url=clean))

    # ── SPOTIFY ──────────────────────────────────────────────────────────────
    if not re.search(r'\byoutube\b', t) and (
       re.search(r'\bspotify\b', t) or re.search(r'\b(play|pause|skip|next|previous|shuffle|repeat)\b.*\bmusic\b', t) or re.search(r'\b(skip|next|previous|pause|resume)\b.*(song|track|this|it)\b', t)
    ):
        # Simple single-action Spotify commands
        if re.search(r'\bpause\b|\bstop (the )?music\b|\bstop (the )?(song|track)\b', t):
            return _plan(_step("SPOTIFY_TASK", query="pause"))
        if re.search(r'\b(resume|unpause|continue)\b.*\b(music|song|spotify)\b', t):
            return _plan(_step("SPOTIFY_TASK", query="resume"))
        if re.search(r'\b(next|skip)\b.*\b(song|track|this|it)?\b|\bskip\b|\bnext track\b|\bskip (this|the) (song|track)\b', t):
            return _plan(_step("SPOTIFY_TASK", query="next track"))
        if re.search(r'\b(previous|prev|go back|last)\b.*\b(song|track)\b', t):
            return _plan(_step("SPOTIFY_TASK", query="previous track"))
        if re.search(r'\b(shuffle)\b', t):
            return _plan(_step("SPOTIFY_TASK", query="shuffle"))
        # Generic play command — pass full query to Spotify agent
        if re.search(r'\bplay\b', t):
            return _plan(_step("SPOTIFY_TASK", query=raw))

    # Plain "play [song/artist/playlist]" without spotify keyword
    if not re.search(r'\byoutube\b', t) and (m := re.search(r'^play\s+(.+)$', t)):
        return _plan(_step("SPOTIFY_TASK", query=raw))

    # ── TIMER / ALARM ─────────────────────────────────────────────────────────
    if re.search(r'\b(set|start|create|add)\b.*\b(timer|alarm)\b', t) or \
       re.search(r'\btimer\b.*\bfor\b\s*(\d+)\s*(second|minute|hour|sec|min|hr)', t) or \
       re.search(r'\b(stop|cancel|pause)\b.*\b(timer|alarm)\b', t):
        return _plan(_step("TIMER_TASK", query=raw))

    # ── WEATHER ───────────────────────────────────────────────────────────────
    if re.search(r'\bweather\b|\bhow.*(hot|cold|warm|outside)\b|\btemperature\b|\bforecast\b|\bgoing to rain\b|\bwill it rain\b', t):
        return _plan(_step("WEATHER_TASK", query=raw))

    # ── KEYBOARD SHORTCUTS ───────────────────────────────────────────────────
    if m := re.search(r'\bpress\b\s+(.+)', t):
        keys_raw = m.group(1).strip().rstrip(".")
        keys = [k.strip() for k in re.split(r'[+,\s]+', keys_raw) if k.strip()]
        return _plan(_step("PRESS", keys=keys, query=t))

    if re.search(r'\bcopy\b|\bpaste\b|\bundo\b|\bredo\b|\bselect all\b|\bcut\b', t) and not re.search(r'\bemail\b|\bdoc\b|\bfile\b', t):
        return _plan(_step("PRESS", query=raw))

    # ── TYPE TEXT ────────────────────────────────────────────────────────────
    if m := re.search(r'\btype\b\s+["\'](.+)["\']', t):
        return _plan(_step("TYPE", text=m.group(1), query=t))

    if m := re.search(r'^type\s+(.+)$', t):
        text = m.group(1).strip()
        if not re.search(r'\bemail\b|\bdocument\b|\bdoc\b|\bin (my|the)\b', text):
            return _plan(_step("TYPE", text=text, query=t))

    # ── GMAIL (Fast-routable) ────────────────────────────────────────────────
    if re.search(r'\b(check|read|show|get|open|list|what are)\b.*\b(my\s+)?(latest\s+|unread\s+|new\s+|recent\s+)?(emails?|inbox|mails?)\b', t) or \
       re.search(r'\b(any new (emails?|mails?)|search (for )?emails?)\b', t) or \
       re.search(r'\b(draft|compose)\b\s+(an?\s+)?(email|mail)\b', t):
        return _plan(_step("GMAIL_TASK", query=raw))

    # ── CALENDAR (Fast-routable) ─────────────────────────────────────────────
    if re.search(r'\b(what is|what are|show|check|list|what\'?s on)\b.*\b(my\s+)?(next\s+|upcoming\s+)?(schedule|calendar|agenda|meetings?|events?)\b', t) or \
       re.search(r'\bwhat (meetings?|events?)\b.*\b(do i have|today|tomorrow)\b', t) or \
       re.search(r'\b(schedule|book|add)\b.*\b(meeting|lunch|standup|gym|appointment|event|call|session|sync)\b.*\b(at|on|every|tomorrow|today|pm|am|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b', t) or \
       re.search(r'\b(cancel|delete|reschedule)\b.*\b(all\s+)?(meeting|gym|event|appointment|sync|standup)\b', t):
        return _plan(_step("CALENDAR_TASK", query=raw))

    # ── WHATSAPP (Fast-routable simple send / read) ──────────────────────────
    if re.search(r'\b(send\s+(a\s+)?whatsapp|whatsapp)\b', t) and \
       not re.search(r'\b(open|launch|start|download|install|switch to)\s+whatsapp\b', t) and \
       not re.search(r'\b(email|mail|gmail)\b', t):
        return _plan(_step("WHATSAPP_TASK", query=raw))

    # ── SIMPLE TASKS ─────────────────────────────────────────────────────────
    if re.search(r'\b(show|list|what are|check|what\'?s on)\b.*\b(my\s+)?(tasks?|to.?do|to do list)\b', t):
        return _plan(_step("TASKS_TASK", query=raw))
    if re.search(r'\b(add|create|make)\b.*\b(task|to.?do)\b', t):
        return _plan(_step("TASKS_TASK", query=raw))
    if re.search(r'\bremind me to\b', t):
        return _plan(_step("TASKS_TASK", query=raw))
    if re.search(r'\bmark\b.*\b(done|complete|completed|finished)\b', t):
        return _plan(_step("TASKS_TASK", query=raw))
    if re.search(r'^(i\s+)?(finished|completed|done with)\b\s+(the\s+)?([a-zA-Z0-9_\s]+)$', t) or \
       re.search(r'\b(complete|finish|tick off)\b.*\btask\b', t):
        return _plan(_step("TASKS_TASK", query=raw))

    # ── CONTACTS ──────────────────────────────────────────────────────────────
    if re.search(r'\b(what is|find|get|look up)\b.*\b(phone|number|email|contact)\b.*\bof\b', t):
        return _plan(_step("CONTACTS_TASK", query=raw))

    # ── MEMORY ────────────────────────────────────────────────────────────────
    _PRODUCTIVITY_TARGETS = r'\b(emails?|mails?|inbox|meetings?|events?|calendar|schedule|agenda|tasks?|to.?dos?|contacts?|messages?|whatsapp|docs?|sheets?|weather|volume|brightness|pc|laptop)\b'
    
    if re.search(r'\bremember\b|\bkeep in mind\b|\bnote that\b|\bmake a note\b', t):
        # Exclude task creation like "remember to buy milk"
        if not re.search(r'\b(to|that)\b.*\b(buy|call|email|send|pay|check|meet|schedule|clean|submit|finish)\b', t):
            return _plan(_step("MEMORY_TASK", query=raw))

    if re.search(r'\b(do you remember|what did i (say|tell you)|recall)\b', t):
        if not re.search(_PRODUCTIVITY_TARGETS, t):
            return _plan(_step("MEMORY_TASK", query=raw))

    if re.search(r'\b(what|which)\b.*\b(do i (prefer|like|want)|is my (favorite|favourite|preference|preferred|seat|blood|car|spot|wifi|passport|pin|address))\b', t):
        return _plan(_step("MEMORY_TASK", query=raw))

    if re.search(r'\bwhat (is|was|are|were) my\b', t):
        if not re.search(_PRODUCTIVITY_TARGETS, t):
            return _plan(_step("MEMORY_TASK", query=raw))

    # ── GENERAL QUESTIONS (fast-routable) ────────────────────────────────────
    if re.search(r'^(what|who|when|where|why|how|define|explain|tell me about)\b', t):
        # Make sure it's not about an app or service we can do something with
        if not re.search(r'\b(gmail|emails?|mails?|spotify|drive|sheets?|docs?|whatsapp|calendar|meetings?|events?|tasks?|to.?dos?)\b', t):
            return _plan(_step("GENERAL_TASK", query=raw))

    # No match — fall through to LLM
    return None


ROUTER_SYSTEM_PROMPT = """You are the master routing agent for WinVoice.
Analyze the user's command and determine the sequence of steps to fulfill it.
Output ONLY a JSON object containing a "steps" array. Do NOT wrap it in markdown block quotes (e.g. no ```json).

Available agents ("action"):
- "FILES_TASK": Find, locate, or open a local file on the computer by name or extension (provide "query" with file name).
- "GMAIL_TASK": Send, read, or summarize emails.
- "DOCS_TASK": Read, write, type into, or create Google Docs. (e.g., "type in testing inside document", "create a document")
- "SHEETS_TASK": Read, write, or create Google Sheets.
- "CALENDAR_TASK": Manage calendar events.
- "SPOTIFY_TASK": Play music, pause, skip tracks on Spotify.
- "WHATSAPP_TASK": Send WhatsApp messages.
- "WEATHER_TASK": Check the weather forecast.
- "OPEN_APP": Open a Windows application (provide "app" key).
- "OPEN_URL": Open a website (provide "url" key, e.g. "https://www.youtube.com").
- "TYPE": Type text via keyboard (provide "text" key).
- "PRESS": Press a keyboard shortcut (provide "keys" array of strings).
- "NATIVE": System power states and hardware controls (provide "command" key: "SHUTDOWN", "RESTART", "SLEEP", "LOCK_PC", "SCREEN_OFF", "SET_VOLUME", "BRIGHTNESS", and "level" for volume/brightness).
- "TIMER_TASK": Set alarms or timers.
- "TASKS_TASK": Manage Google Tasks.
- "CONTACTS_TASK": Find phone numbers or emails.
- "MEMORY_TASK": Remember or recall user preferences.
- "GENERAL_TASK": Anything else (answering questions, chatting, generic text, OR summarizing/translating/formatting data from a previous step UNLESS specifically asked to write it to a document/sheet). DO NOT use this for opening websites!

== CRITICAL CONTEXT RULE ==
You may be provided with 'RECENT CONTEXT' containing previous commands.
Your SOLE job is to execute the 'CURRENT COMMAND TO EXECUTE'. 
The 'RECENT CONTEXT' is ONLY provided to help you resolve ambiguous pronouns (e.g., "email him" -> "Manish").
DO NOT execute the tasks mentioned in the RECENT CONTEXT.
If the CURRENT COMMAND TO EXECUTE is a completely new instruction unrelated to the RECENT CONTEXT, IGNORE the RECENT CONTEXT entirely!

Format:
{
  "steps": [
    {
      "action": "<AGENT_NAME>",
      "query": "<The exact instruction for this agent>",
      "context_from_step": <OPTIONAL: A single integer OR an array of integers (1-indexed) representing the previous step number(s) this step requires data from>
    }
  ]
}

RULES:

0. STRICT ANTI-HALLUCINATION: 
   - DO NOT invent actions the user did not ask for.
   - If the user says "summarize the data", use GENERAL_TASK to write the summary. Do NOT automatically add a DOCS_TASK to write it down unless the user explicitly asks you to create a document or store the text somewhere.
   - ONLY route to an agent if the user explicitly requested an action that requires that agent.

1. SPLIT steps when different AGENTS are needed for different sub-tasks.
   - WRONG: {"action": "GMAIL_TASK", "query": "read email and add the notes as a task"} (GMAIL_TASK cannot add tasks!)
   - CORRECT: Step 1 (GMAIL_TASK: "read email"), Step 2 (GENERAL_TASK: "extract notes"), Step 3 (TASKS_TASK: "add as task").

2. GROUP into a SINGLE step when the SAME agent handles multiple sub-tasks using the SAME data source.
   - Example: "create a document and write AI news in it" -> Single DOCS_TASK (same agent, same intent)
   - Example: "send email to Sara and draft email to Manish" -> Single GMAIL_TASK (same agent, no data dependency)

3. SPLIT into SEPARATE steps for the SAME agent when each call needs DIFFERENT data from DIFFERENT sources.
   - Example: "create a doc" (step 4) vs "add the sheet data to the doc" (step 6) -> TWO separate DOCS_TASK steps because step 6 depends on step 5's sheet data.
   - Example: any time you would say "first do X, then later after getting Y, do Z with the same agent" -> split them.

4. FAN-OUT: A single step's output CAN and SHOULD feed multiple different downstream agents.
   - Example: Read email (step 1) -> its content flows to TASKS_TASK (step 2) AND to DOCS_TASK (step 3) independently.
   - Both step 2 and step 3 can have context_from_step: 1 pointing at the same source.

5. context_from_step RULES:
   - Add it ONLY when a step genuinely needs the TEXT OUTPUT of a previous step.
   - If a step just needs to know a name/title (e.g., "add to the Wrap Up doc") but doesn't need the actual content, do NOT add context_from_step.
   - context_from_step can be a single int OR an array [1, 2] if a step needs data from multiple previous steps.

6. GENERAL_TASK for processing: Use GENERAL_TASK as a "processing" step to extract, summarize, or reformat data between agents.
   - Example: Gmail reads email -> GENERAL_TASK extracts tasks -> TASKS_TASK creates them.
   - Do NOT skip the GENERAL_TASK middle step; agents cannot extract structured data from raw email dumps themselves.

7. NATIVE agent for system actions: volume, brightness, lock PC. Always include "command" key: "LOCK_PC", "SET_VOLUME", "BRIGHTNESS".

8. IMPORTANT: If the user asks to summarize/format/translate WITHOUT saving to a doc, use GENERAL_TASK, NOT DOCS_TASK.


EXAMPLES:

CURRENT COMMAND TO EXECUTE: open youtube
{
  "steps": [
    {
      "action": "OPEN_URL",
      "url": "https://www.youtube.com"
    }
  ]
}

CURRENT COMMAND TO EXECUTE: what are my latest emails
{
  "steps": [
    {
      "action": "GMAIL_TASK",
      "query": "what are my latest emails"
    }
  ]
}

CURRENT COMMAND TO EXECUTE: what is my next meeting
{
  "steps": [
    {
      "action": "CALENDAR_TASK",
      "query": "what is my next meeting"
    }
  ]
}

CURRENT COMMAND TO EXECUTE: what is my wifi password
{
  "steps": [
    {
      "action": "MEMORY_TASK",
      "query": "what is my wifi password"
    }
  ]
}

CURRENT COMMAND TO EXECUTE: remember that my car parking spot is B2
{
  "steps": [
    {
      "action": "MEMORY_TASK",
      "query": "remember that my car parking spot is B2"
    }
  ]
}

CURRENT COMMAND TO EXECUTE: Read my email, extract the phone number, check the weather for London, and send a WhatsApp to the phone number with the weather forecast.
{
  "steps": [
    {
      "action": "GMAIL_TASK",
      "query": "Read my latest email"
    },
    {
      "action": "GENERAL_TASK",
      "query": "Extract the phone number",
      "context_from_step": 1
    },
    {
      "action": "WEATHER_TASK",
      "query": "Check the weather for London"
    },
    {
      "action": "WHATSAPP_TASK",
      "query": "Send a WhatsApp with the weather forecast",
      "context_from_step": [2, 3]
    }
  ]
}

CURRENT COMMAND TO EXECUTE: open spotify and open gmail
{
  "steps": [
    {
      "action": "OPEN_APP",
      "app": "spotify"
    },
    {
      "action": "OPEN_URL",
      "url": "https://mail.google.com"
    }
  ]
}

CURRENT COMMAND TO EXECUTE: play my playlist on spotify
{
  "steps": [
    {
      "action": "SPOTIFY_TASK",
      "query": "play my playlist on spotify"
    }
  ]
}

CURRENT COMMAND TO EXECUTE: please shutdown the laptop
{
  "steps": [
    {
      "action": "NATIVE",
      "command": "SHUTDOWN"
    }
  ]
}

CURRENT COMMAND TO EXECUTE: put the computer to sleep
{
  "steps": [
    {
      "action": "NATIVE",
      "command": "SLEEP"
    }
  ]
}

CURRENT COMMAND TO EXECUTE: read the Q3 report and email it to manish
{
  "steps": [
    {
      "action": "DOCS_TASK",
      "query": "read the Q3 report"
    },
    {
      "action": "GMAIL_TASK",
      "query": "email it to manish",
      "context_from_step": 1
    }
  ]
}

CURRENT COMMAND TO EXECUTE: get the latest bitcoin price and text it to sarah, then read my unread emails
{
  "steps": [
    {
      "action": "GENERAL_TASK",
      "query": "get the latest bitcoin price"
    },
    {
      "action": "WHATSAPP_TASK",
      "query": "text it to sarah",
      "context_from_step": 1
    },
    {
      "action": "GMAIL_TASK",
      "query": "read my unread emails"
    }
  ]
}


CURRENT COMMAND TO EXECUTE: go through the sales sheet, summarize it and write down the summary in a new document called sales report
{
  "steps": [
    {
      "action": "SHEETS_TASK",
      "query": "go through the sales sheet"
    },
    {
      "action": "GENERAL_TASK",
      "query": "summarize it",
      "context_from_step": 1
    },
    {
      "action": "DOCS_TASK",
      "query": "write down the summary in a new document called sales report",
      "context_from_step": 2
    }
  ]
}

CURRENT COMMAND TO EXECUTE: read my latest email, add any action items from it as tasks, and also save the email content in a new doc called Inbox Summary
{
  "steps": [
    {
      "action": "GMAIL_TASK",
      "query": "read my latest email"
    },
    {
      "action": "GENERAL_TASK",
      "query": "Extract all action items and to-do items as a clean numbered list",
      "context_from_step": 1
    },
    {
      "action": "TASKS_TASK",
      "query": "add each extracted action item as a separate task",
      "context_from_step": 2
    },
    {
      "action": "DOCS_TASK",
      "query": "create a new document called Inbox Summary and write the full email content in it",
      "context_from_step": 1
    }
  ]
}

CURRENT COMMAND TO EXECUTE: check the inventory sheet, create a new doc called Stock Report and write the inventory data in it, then also check the sales sheet and add the sales data to the same Stock Report document
{
  "steps": [
    {
      "action": "SHEETS_TASK",
      "query": "check the inventory sheet"
    },
    {
      "action": "DOCS_TASK",
      "query": "create a new document called Stock Report and write the inventory data in it",
      "context_from_step": 1
    },
    {
      "action": "SHEETS_TASK",
      "query": "check the sales sheet"
    },
    {
      "action": "DOCS_TASK",
      "query": "add the sales data to the Stock Report document",
      "context_from_step": 3
    }
  ]
}

CURRENT COMMAND TO EXECUTE: read my latest email and draft a reply saying I will look into it
{
  "steps": [
    {
      "action": "GMAIL_TASK",
      "query": "read my latest email"
    },
    {
      "action": "GMAIL_TASK",
      "query": "draft a reply saying I will look into it",
      "context_from_step": 1
    }
  ]
}
"""

# ── Valid action names ──────────────────────────────────────────────────────
VALID_ACTIONS = {
    "FILES_TASK", "GMAIL_TASK", "DOCS_TASK", "SHEETS_TASK", "CALENDAR_TASK",
    "SPOTIFY_TASK", "WHATSAPP_TASK", "WEATHER_TASK", "OPEN_APP",
    "OPEN_URL", "TYPE", "PRESS", "NATIVE", "TIMER_TASK", "TASKS_TASK",
    "CONTACTS_TASK", "MEMORY_TASK", "GENERAL_TASK",
}

# ── Aliases: any hallucinated name the LLM might use → the real action ──────
ACTION_ALIASES = {
    "FILE_TASK":          "FILES_TASK",
    "FILE_AGENT":         "FILES_TASK",
    "FILES":              "FILES_TASK",
    "FILE":               "FILES_TASK",
    "FIND_FILE":          "FILES_TASK",
    "SEARCH_FILE":        "FILES_TASK",
    "TASK_MANAGER":       "TASKS_TASK",
    "TASK_TASK":          "TASKS_TASK",
    "GOOGLE_TASKS":       "TASKS_TASK",
    "TASKS":              "TASKS_TASK",
    "CALL_TASK":          "TASKS_TASK",
    "TODO_TASK":          "TASKS_TASK",
    "EMAIL_TASK":         "GMAIL_TASK",
    "EMAIL":              "GMAIL_TASK",
    "MAIL_TASK":          "GMAIL_TASK",
    "MAIL":               "GMAIL_TASK",
    "SEND_EMAIL":         "GMAIL_TASK",
    "TRANSLATE_TASK":     "GENERAL_TASK",
    "TRANSLATE":          "GENERAL_TASK",
    "PC_LOCK":            "NATIVE",
    "LOCK_PC":            "NATIVE",
    "SHUTDOWN":           "NATIVE",
    "SHUTDOWN_PC":        "NATIVE",
    "SHUTDOWN_LAPTOP":    "NATIVE",
    "SHUTDOWN_TASK":      "NATIVE",
    "SLEEP":              "NATIVE",
    "SLEEP_PC":           "NATIVE",
    "SLEEP_LAPTOP":       "NATIVE",
    "SLEEP_TASK":         "NATIVE",
    "RESTART":            "NATIVE",
    "RESTART_PC":         "NATIVE",
    "RESTART_LAPTOP":     "NATIVE",
    "REBOOT":             "NATIVE",
    "POWER_OFF":          "NATIVE",
    "SYSTEM_TASK":        "NATIVE",
    "PC_CONTROL_TASK":    "NATIVE",
    "SYSTEM_CONTROL":     "NATIVE",
    "MONITOR_TASK":       "NATIVE",
    "MONITOR":            "NATIVE",
    "SCREEN_TASK":        "NATIVE",
    "DISPLAY_TASK":       "NATIVE",
    "MUSIC_TASK":         "SPOTIFY_TASK",
    "MUSIC":              "SPOTIFY_TASK",
    "GMAIL":              "GMAIL_TASK",
    "DOCS":               "DOCS_TASK",
    "SHEETS":             "SHEETS_TASK",
    "CALENDAR":           "CALENDAR_TASK",
    "SPOTIFY":            "SPOTIFY_TASK",
    "WHATSAPP":           "WHATSAPP_TASK",
    "WEATHER":            "WEATHER_TASK",
    "TASKS_AGENT":        "TASKS_TASK",
    "DOCS_AGENT":         "DOCS_TASK",
    "GMAIL_AGENT":        "GMAIL_TASK",
    "CALENDAR_AGENT":     "CALENDAR_TASK",
    "SHEETS_AGENT":       "SHEETS_TASK",
    "WHATSAPP_AGENT":     "WHATSAPP_TASK",
    "GENERAL":            "GENERAL_TASK",
    "YOUTUBE_TASK":       "OPEN_URL",
    "YOUTUBE":            "OPEN_URL",
    "SEARCH_YOUTUBE":     "OPEN_URL",
    "INPUT_REPLAY":       "INPUT_REPLAY",  # internal, keep as-is
}

# ── Keywords that signal a step feeds from a previous step ──────────────────
# These are pronouns/references that imply "use the output from before"
_BACKREFERENCE_WORDS = {
    "it", "them", "this", "that", "its", "these", "those",
    "the result", "the output", "the above", "the same",
    "the doc", "the document", "the sheet", "the email",
    "the meeting notes", "the summary", "the data",
    "the report", "the response", "the information",
    "add it", "use it", "send it", "format it", "append it",
    "the marketing spend", "the total", "the amount",
    "birthday", "date", "day", "time", "forecast", "weather",
    "schedule", "update", "notes", "tasks", "items", "action items",
}

def _normalize_action(action: str) -> str:
    """Map any hallucinated action name to a real one."""
    a = action.strip().upper()
    if a in VALID_ACTIONS:
        return a
    if a in ACTION_ALIASES:
        return ACTION_ALIASES[a]
    # Try prefix matching as last resort
    for valid in VALID_ACTIONS:
        if a.startswith(valid) or valid.startswith(a):
            return valid
    return a  # Unknown — return as-is so _sanitize can skip it

def _query_references_previous(query: str) -> bool:
    """Returns True if the query contains a back-reference to a previous step's output."""
    q_lower = query.lower()
    for word in _BACKREFERENCE_WORDS:
        if word in q_lower:
            return True
    return False

def _infer_context_piping(steps: list) -> list:
    """
    If the LLM forgot to add context_from_step, infer it automatically.
    Uses semantic keyword matching to find the most appropriate source step,
    not just the most recent one.
    """
    DATA_PRODUCING = {
        "GMAIL_TASK", "DOCS_TASK", "SHEETS_TASK", "CALENDAR_TASK",
        "WEATHER_TASK", "GENERAL_TASK", "CONTACTS_TASK", "MEMORY_TASK",
    }

    # Keywords in a step's query → which agent type produced the needed data
    SOURCE_KEYWORDS = [
        (["from the email", "from the mail", "from the message", "meeting notes",
          "email content", "from the latest email"], "GMAIL_TASK"),
        (["from the sheet", "from the spreadsheet", "from the budget", "marketing spend",
          "spend", "budget data", "revenue", "sales data", "from the q3", "from the q2",
          "financial data", "the total", "the amount", "inventory data",
          "birthday", "birthdays", "sara's birthday", "that day", "for that day"], "SHEETS_TASK"),
        (["from the document", "from the doc", "the document content",
          "the report content"], "DOCS_TASK"),
        (["weather", "forecast", "temperature", "the weather"], "WEATHER_TASK"),
        (["contact", "phone number", "email address of"], "CONTACTS_TASK"),
        (["summary", "the summary", "extracted tasks", "action items", "extracted items"], "GENERAL_TASK"),
    ]

    # Build list of (step_num_1indexed, action) for all producer steps seen so far
    producers = []  # [(step_num, action)]

    for i, step in enumerate(steps):
        step_num = i + 1
        action = step.get("action", "")
        query = step.get("query", "")
        q_lower = query.lower()

        # Only infer if not already set
        if "context_from_step" not in step and _query_references_previous(query) and producers:
            best_match = None

            # Try to find a semantically appropriate source step
            for keywords, source_action in SOURCE_KEYWORDS:
                if any(kw in q_lower for kw in keywords):
                    # Find the most recent producer step with the matching action type
                    for p_num, p_action in reversed(producers):
                        if p_action == source_action:
                            best_match = p_num
                            break
                    if best_match:
                        break

            # Fall back to the last producer if no keyword match
            if best_match is None:
                best_match = producers[-1][0]

            step["context_from_step"] = best_match

        # Register this step as a producer if it produces data
        if action in DATA_PRODUCING:
            producers.append((step_num, action))

    # Ensure any step where context_from_step points to a DOCS creation step actually points to the preceding data producer
    for i, step in enumerate(steps):
        ctx = step.get("context_from_step")
        if ctx is not None and isinstance(ctx, int) and 1 <= ctx <= len(steps):
            pointed_step = steps[ctx - 1]
            if pointed_step.get("action") == "DOCS_TASK" and any(w in pointed_step.get("query", "").lower() for w in ("create", "new doc", "new document")):
                for prev_idx in range(i - 1, -1, -1):
                    if steps[prev_idx].get("action") in DATA_PRODUCING and steps[prev_idx].get("action") != "DOCS_TASK":
                        step["context_from_step"] = prev_idx + 1
                        break

    return steps


def _fix_native_action(steps: list) -> list:
    """Ensure NATIVE steps for power states, locking PC, volume, brightness, monitor have the correct 'command' key."""
    for step in steps:
        if step.get("action") == "NATIVE":
            q = (step.get("query") or "").lower()
            cmd = (step.get("command") or "").upper()
            if not cmd:
                if any(w in q for w in ("shut down", "shutdown", "power off", "power down", "turn off the pc", "turn off the laptop", "turn off computer")):
                    step["command"] = "SHUTDOWN"
                elif any(w in q for w in ("restart", "reboot")):
                    step["command"] = "RESTART"
                elif any(w in q for w in ("sleep", "suspend")):
                    step["command"] = "SLEEP"
                elif "lock" in q:
                    step["command"] = "LOCK_PC"
                elif "volume" in q:
                    step["command"] = "SET_VOLUME"
                elif "brightness" in q:
                    step["command"] = "BRIGHTNESS"
                elif any(w in q for w in ("monitor", "screen", "display")):
                    step["command"] = "SCREEN_OFF"
    return steps

def _fix_youtube_action(steps: list) -> list:
    """Ensure any step asking for YouTube (even if hallucinated as SPOTIFY_TASK or SEARCH_YOUTUBE) opens YouTube."""
    import urllib.parse
    import re as _re
    for step in steps:
        action = step.get("action", "")
        q = step.get("query", "")
        q_lower = q.lower()

        # If SPOTIFY_TASK was wrongly generated for a query that mentions YouTube
        if action == "SPOTIFY_TASK" and "youtube" in q_lower:
            step["action"] = "OPEN_URL"
            cleaned = _re.sub(r'\b(?:open|play|search|watch|find|show|listen to)\b', '', q, flags=_re.I)
            cleaned = _re.sub(r'\b(?:on|in|from|at)\s+youtube\b', '', cleaned, flags=_re.I).strip()
            cleaned = _re.sub(r'\byoutube\b', '', cleaned, flags=_re.I).strip()
            if cleaned:
                step["url"] = f"https://www.youtube.com/results?search_query={urllib.parse.quote_plus(cleaned)}"
            else:
                step["url"] = "https://www.youtube.com"

        # If SEARCH_YOUTUBE or YOUTUBE_TASK or OPEN_URL missing url was generated
        elif action in ("SEARCH_YOUTUBE", "YOUTUBE_TASK", "YOUTUBE") or (action == "OPEN_URL" and not step.get("url")):
            step["action"] = "OPEN_URL"
            cleaned = _re.sub(r'\b(?:open|play|search|watch|find|show|listen to)\b', '', q, flags=_re.I)
            cleaned = _re.sub(r'\b(?:on|in|from|at)\s+youtube\b', '', cleaned, flags=_re.I).strip()
            cleaned = _re.sub(r'\byoutube\b', '', cleaned, flags=_re.I).strip()
            if cleaned:
                step["url"] = f"https://www.youtube.com/results?search_query={urllib.parse.quote_plus(cleaned)}"
            else:
                step["url"] = "https://www.youtube.com"
    return steps

def route_intent(user_query: str, context: list[str] | None = None) -> dict:
    # ⚡ Try the zero-latency fast matcher first
    fast_result = _fast_match(user_query)
    if fast_result is not None:
        print(f"\n[FAST MATCH] Routed instantly: {fast_result['steps'][0]['action']}")
        return fast_result

    # 🧠 Fall through to qwen2.5:7b for complex / multi-step prompts
    prompt = ""
    if context:
        prompt += (
            "=================================================\n"
            "PAST CONVERSATION HISTORY (FOR CONTEXT ONLY):\n"
            f"{context}\n"
            "=================================================\n"
            "CRITICAL: The history above is ONLY to help you resolve pronouns (like 'him', 'it'). "
            "DO NOT create steps for anything in the history. ONLY output steps for the CURRENT COMMAND.\n\n"
        )
        
    prompt += f"CURRENT COMMAND TO EXECUTE:\n{user_query}\n"
    plan = call_gemini_smart(prompt, system_prompt=ROUTER_SYSTEM_PROMPT)

    if not plan:
        return {}

    if "steps" not in plan:
        if "action" in plan and isinstance(plan["action"], str):
            plan = {"steps": [dict(plan)]}
        elif "action" in plan and isinstance(plan["action"], list):
            recovered = []
            for act in plan["action"]:
                if isinstance(act, str):
                    recovered.append({"action": act})
                elif isinstance(act, dict):
                    recovered.append(act)
            plan = {"steps": recovered}
        else:
            return plan

    steps = plan.get("steps", [])

    # Step 1: Normalize all action names
    for step in steps:
        raw_act = (step.get("action") or step.get("type") or "").strip().upper()
        if raw_act in ("SHUTDOWN", "SHUTDOWN_PC", "SHUTDOWN_LAPTOP", "POWER_OFF") and "command" not in step:
            step["command"] = "SHUTDOWN"
        elif raw_act in ("RESTART", "RESTART_PC", "RESTART_LAPTOP", "REBOOT") and "command" not in step:
            step["command"] = "RESTART"
        elif raw_act in ("SLEEP", "SLEEP_PC", "SLEEP_LAPTOP") and "command" not in step:
            step["command"] = "SLEEP"
        elif raw_act in ("LOCK_PC", "PC_LOCK") and "command" not in step:
            step["command"] = "LOCK_PC"

        if "action" in step:
            step["action"] = _normalize_action(step["action"])
        elif "type" in step:
            step["action"] = _normalize_action(step.pop("type"))

    # Step 1.5: Fix email reply actions
    steps = _fix_email_replies(steps)

    # Step 2: Split any merged steps (e.g., GENERAL_TASK doing TASKS_TASK work)
    steps = _split_merged_steps(steps)

    # Step 3: Infer any missing context_from_step
    steps = _infer_context_piping(steps)

    # Step 4: Fix NATIVE action payloads
    steps = _fix_native_action(steps)

    # Step 4.5: Fix YouTube actions (prevent misrouting to Spotify)
    steps = _fix_youtube_action(steps)

    plan["steps"] = steps
    return plan


def _fix_email_replies(steps: list) -> list:
    """If an email was just read/fetched, drafting or sending a reply is GMAIL_TASK, not WHATSAPP_TASK."""
    import re as _re
    for i in range(1, len(steps)):
        prev_act = steps[i - 1].get("action")
        curr = steps[i]
        c_query = curr.get("query", "").lower()
        if prev_act == "GMAIL_TASK" and _re.search(r'\b(reply|draft a reply|respond|send a reply)\b', c_query):
            if curr.get("action") in ("WHATSAPP_TASK", "GENERAL_TASK"):
                curr["action"] = "GMAIL_TASK"
    return steps


def _split_merged_steps(steps: list) -> list:
    """
    Detects steps where the router wrongly merged two agents into one.
    Most common case: GENERAL_TASK with 'add as tasks / create tasks' intent
    -> split into GENERAL_TASK (extract) + TASKS_TASK (create).
    """
    import re as _re

    TASK_CREATION_PATTERN = _re.compile(
        r'\b(add|create|make|save|put)\b.{0,40}\b(task|tasks|to.?do|todo)\b',
        _re.IGNORECASE
    )

    DATA_PRODUCING = {
        "GMAIL_TASK", "DOCS_TASK", "SHEETS_TASK", "CALENDAR_TASK",
        "WEATHER_TASK", "GENERAL_TASK", "CONTACTS_TASK", "MEMORY_TASK",
    }

    new_steps = []

    for step in steps:
        action = step.get("action", "")
        query = step.get("query", "")
        ctx = step.get("context_from_step")

        if action == "GENERAL_TASK" and TASK_CREATION_PATTERN.search(query):
            # 1-indexed position of the GENERAL_TASK step we're about to add
            new_step_num = len(new_steps) + 1

            # Find the last data-producing step before this one (to pipe email/doc/sheet data)
            last_producer_idx = None
            for j, prev in enumerate(new_steps):
                if prev.get("action") in DATA_PRODUCING:
                    last_producer_idx = j + 1  # 1-indexed

            # Part 1: GENERAL_TASK — extract only (strip the "add as task" suffix)
            extract_query = _re.sub(
                r'\s*(and\s+)?(add|create|make|save|put)\b.{0,60}\b(task|tasks|to.?do|todo)\b.*$',
                '', query, flags=_re.IGNORECASE
            ).strip().rstrip(',; ')
            if not extract_query:
                extract_query = "Extract all action items as a numbered list"

            extract_step = {"action": "GENERAL_TASK", "query": extract_query}
            # Use explicitly set context first, then fall back to last producer
            if ctx is not None:
                extract_step["context_from_step"] = ctx
            elif last_producer_idx is not None:
                extract_step["context_from_step"] = last_producer_idx
            new_steps.append(extract_step)

            # Part 2: TASKS_TASK — create tasks from the extracted list
            tasks_step = {
                "action": "TASKS_TASK",
                "query": "Add each extracted action item as a separate task",
                "context_from_step": new_step_num  # links to GENERAL_TASK just above
            }
            new_steps.append(tasks_step)
        else:
            new_steps.append(step)

    return new_steps