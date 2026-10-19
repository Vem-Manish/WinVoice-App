# voice.py — Speech-to-text for WinVoice
# Priority: Google STT (online) → Whisper (offline fallback) → empty string

import os
import re
import threading
import unicodedata

# ── State ─────────────────────────────────────────────────────────────
_stop_event      = threading.Event()
_listening_flag  = threading.Event()
_current_audio_level: float = 0.0
_level_lock      = threading.Lock()

# Whisper model cache
_whisper_model      = None
_whisper_model_lock = threading.Lock()
_WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL", "base")


def _get_whisper():
    global _whisper_model
    if _whisper_model is not None:
        return _whisper_model, None
    try:
        with _whisper_model_lock:
            # Re-check inside lock to avoid TOCTOU race
            if _whisper_model is None:
                import whisper
                print(f"[Voice] Loading Whisper '{_WHISPER_MODEL_SIZE}'...")
                _whisper_model = whisper.load_model(_WHISPER_MODEL_SIZE)
                print("[Voice] Whisper loaded.")
        return _whisper_model, None
    except ImportError:
        return None, "whisper_not_installed"
    except Exception as e:
        return None, str(e)


def _whisper_available() -> bool:
    try:
        import whisper
        return True
    except ImportError:
        return False


def get_audio_level() -> float:
    with _level_lock:
        return _current_audio_level


def is_listening() -> bool:
    return _listening_flag.is_set()


def force_stop_listening():
    _stop_event.set()
    _listening_flag.clear()


_recognizer = None
_recognizer_lock = threading.Lock()


def _get_recognizer():
    global _recognizer
    if _recognizer is not None:
        return _recognizer
    with _recognizer_lock:
        if _recognizer is None:
            import speech_recognition as sr
            r = sr.Recognizer()
            r.energy_threshold = 300
            r.dynamic_energy_threshold = True
            r.pause_threshold = 0.8
            r.phrase_threshold = 0.3
            r.non_speaking_duration = 0.5
            _recognizer = r
    return _recognizer


def calibrate():
    try:
        import speech_recognition as sr
        r = _get_recognizer()
        with sr.Microphone() as source:
            r.adjust_for_ambient_noise(source, duration=1.0)
        print(f"[Voice] Calibration done. Energy threshold: {r.energy_threshold}")
    except Exception as e:
        print(f"[Voice] Calibration skipped: {e}")


# ── Hallucination / garbage detection ────────────────────────────────
# Whisper 'base' hallucinates CJK, Arabic, etc. when it mishears English audio.
# We catch this and filter out garbage output.
_BAD_PATTERNS = [
    r'[\u4e00-\u9fff]',   # CJK Unified Ideographs (Chinese/Japanese/Korean)
    r'[\u3040-\u30ff]',   # Hiragana / Katakana
    r'[\uac00-\ud7af]',   # Korean Hangul
    r'[\u0600-\u06ff]',   # Arabic
    r'[\u0400-\u04ff]',   # Cyrillic
    r'[\u0900-\u097f]',   # Devanagari (Hindi script — user speaks English)
]
_BAD_COMPILED = [re.compile(p) for p in _BAD_PATTERNS]

# Whisper hallucinates these exact phrases on silence / noise:
_HALLUCINATION_PHRASES = {
    "thank you", "thanks for watching", "thanks for listening",
    "please subscribe", "you", ".", "..", "...", "the",
    "i", "bye", "bye.",
    "subtitles by", "subtitles", "transcribed by",
    "[music]", "[applause]", "[laughter]", "(music)",
}


def _is_garbage(text: str) -> bool:
    """Returns True if the transcription looks like a hallucination."""
    t = text.strip().lower().rstrip(".,!?")
    # Also strip surrounding brackets so "[music]" matches "music" in the set
    t_stripped = t.strip("[]")

    # Too short to be a real command
    if len(t) < 2:
        return True

    # Exact hallucination phrase — check both raw and bracket-stripped forms
    if t in _HALLUCINATION_PHRASES or t_stripped in _HALLUCINATION_PHRASES:
        return True

    # Contains non-English script characters
    for pat in _BAD_COMPILED:
        if pat.search(text):
            return True

    # More than 15% non-ASCII printable chars → likely hallucination
    total = len(text)
    non_ascii = sum(1 for c in text if ord(c) > 127)
    if total > 0 and non_ascii / total > 0.15:
        return True

    return False


def _is_silent(audio_np) -> bool:
    """Returns True if the audio is too quiet to contain speech."""
    try:
        import numpy as np
        rms = float(np.sqrt(np.mean(audio_np ** 2)))
        return rms < 0.005   # below ~0.5% of full scale = silence/noise
    except Exception:
        return False


def clean_speech_text(text: str) -> str:
    """
    Cleans up common speech recognition artifacts:
    - Deduplication of echoed/repeated phrases from STT
    - Leading command verb commas ('Text, Manish' -> 'Text Manish')
    - 'Next ... on WhatsApp' -> 'Text ... on WhatsApp'
    - Phonetic misrecognition of contact 'Manish' heard as 'money'
    - Trailing commas before prepositions
    """
    if not text:
        return ""
    t = text.strip()
    # 0. Deduplicate repeated consecutive phrases or clauses (e.g. Google STT echoing: "find the file X find the file X and ...")
    t = re.sub(r'\b(.{5,60}?)\s+\1\b', r'\1', t, flags=re.IGNORECASE)
    t = re.sub(r'\b([a-zA-Z]{3,})\s+\1\b', r'\1', t, flags=re.IGNORECASE)

    # 1. Fix 'Next ... on WhatsApp' -> 'Text ... on WhatsApp'
    t = re.sub(r'^Next\b(?=.*(?:on whatsapp|whatsapp|an email|email to))', 'Text', t, flags=re.IGNORECASE)
    # 2. Clean up awkward command commas
    t = re.sub(r'^(Text|Send|Find|Email|Search|Call|Open|Show|Add|Set)\s*,\s*', r'\1 ', t, flags=re.IGNORECASE)
    # 3. Clean up contact misrecognition: 'money' -> 'Manish'
    t = re.sub(r'\b(invite|to|text|message|whatsapp)\s+money\b', r'\1 Manish', t, flags=re.IGNORECASE)
    t = re.sub(r'\bmoney\s+(on whatsapp|saying)\b', r'Manish \1', t, flags=re.IGNORECASE)
    # 4. Remove comma before 'on whatsapp' or 'saying'
    t = re.sub(r'\s*,\s*(on whatsapp|saying)\b', r' \1', t, flags=re.IGNORECASE)
    # 5. Clean up any remaining leading command comma
    t = re.sub(r'^(Text|Send|Find|Email|Search|Call|Open|Show|Add|Set)\s*,\s*', r'\1 ', t, flags=re.IGNORECASE)
    return t.strip()


def listen() -> str:
    """
    Record from mic and transcribe.
    Returns cleaned English text, or empty string on failure/silence/hallucination.
    Priority: Google STT (online) → Whisper (offline fallback) → empty string.
    """
    global _current_audio_level
    _stop_event.clear()
    _listening_flag.set()

    try:
        import speech_recognition as sr
        r = _get_recognizer()

        with sr.Microphone() as source:
            # We do not call adjust_for_ambient_noise here to avoid clipping
            # the opening syllable (e.g. 'Text' becoming 'Next'). Calibration is
            # handled at startup by calibrate().
            audio = None
            try:
                audio = r.listen(source, timeout=10, phrase_time_limit=20)
            except sr.WaitTimeoutError:
                return ""

            if _stop_event.is_set() or audio is None:
                return ""

            # Explicit Whisper override if configured in env
            if os.getenv("VOICE_STT_ENGINE", "google").lower() == "whisper" and _whisper_available():
                text = _transcribe_whisper(audio)
                if text and not _is_garbage(text):
                    cleaned = clean_speech_text(text)
                    print(f"[Voice/Whisper (override)] '{cleaned}'")
                    return cleaned

            # ── 1. Google Online Speech Recognition (Primary) ───────────
            # Try Google Speech Recognition online first.
            # en-IN handles Indian names/phonetics accurately (e.g. "Manish"),
            # and en-US serves as fallback if en-IN raises UnknownValueError.
            for lang in ("en-IN", "en-US"):
                try:
                    text = r.recognize_google(audio, language=lang)
                    if text and not _is_garbage(text):
                        cleaned = clean_speech_text(text)
                        print(f"[Voice/Google {lang}] '{cleaned}'")
                        return cleaned
                except sr.UnknownValueError:
                    continue
                except sr.RequestError as e:
                    print(f"[Voice] Google STT network error ({lang}): {e}")
                    break  # Network is down; drop through to offline Whisper fallback

            # ── 2. Whisper (Offline Fallback) ───────────────────────────
            if _whisper_available():
                print("[Voice] Google STT unavailable, attempting local Whisper fallback...")
                text = _transcribe_whisper(audio)
                if text and not _is_garbage(text):
                    cleaned = clean_speech_text(text)
                    print(f"[Voice/Whisper fallback] '{cleaned}'")
                    return cleaned
                if text:
                    print(f"[Voice/Whisper] Garbage detected ('{text[:40]}')")
                else:
                    print("[Voice/Whisper] Empty result")

            return ""

    except ImportError as e:
        print(f"[Voice] Missing dependency: {e}")
        return ""
    except Exception as e:
        print(f"[Voice] listen() error: {e}")
        return ""
    finally:
        _listening_flag.clear()
        with _level_lock:
            _current_audio_level = 0.0


def _transcribe_whisper(audio) -> str:
    try:
        import numpy as np
        model, err = _get_whisper()
        if err:
            return ""

        raw      = audio.get_raw_data(convert_rate=16000, convert_width=2)
        audio_np = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

        # Reject silence before even calling the model — saves ~1-2 seconds
        if _is_silent(audio_np):
            print("[Voice/Whisper] Audio too quiet, skipping transcription")
            return ""

        result = model.transcribe(
            audio_np,
            language="en",          # LOCK to English — prevents CJK hallucinations
            fp16=False,
            task="transcribe",
            # Strong English prompt forces the model to stay in English
            initial_prompt=(
                "The following is a voice command in English for a Windows computer assistant. "
                "Commands include opening apps, sending emails, checking weather, playing music, "
                "and other computer tasks. Transcribe exactly what is said in English."
            ),
            # Suppress hallucination tokens
            condition_on_previous_text=False,  # don't carry over previous hallucinations
            no_speech_threshold=0.6,           # higher = more aggressive silence rejection
            logprob_threshold=-1.0,            # reject low-confidence segments
            compression_ratio_threshold=2.4,   # reject repetitive/hallucinated output
            temperature=0.0,                   # deterministic = less hallucination
        )

        text = (result.get("text") or "").strip()

        # Extra safety: strip any non-ASCII characters that slipped through
        text = _strip_non_english(text)

        return text

    except Exception as e:
        print(f"[Voice/Whisper] Error: {e}")
        return ""


def _strip_non_english(text: str) -> str:
    """
    Remove non-ASCII characters that don't belong in English speech.
    Keeps standard punctuation and letters.
    """
    cleaned = []
    for ch in text:
        if ord(ch) < 128:                          # plain ASCII — keep
            cleaned.append(ch)
        elif unicodedata.category(ch).startswith('L'):  # any letter — keep (accents etc.)
            cleaned.append(ch)
        else:
            cleaned.append(' ')                    # replace with space
    result = re.sub(r'\s+', ' ', ''.join(cleaned)).strip()
    return result


def preload_whisper():
    if os.getenv("VOICE_STT_ENGINE", "google").lower() == "whisper" and _whisper_available():
        threading.Thread(target=_get_whisper, daemon=True).start()