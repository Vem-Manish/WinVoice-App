"""
tts.py — Text-to-speech for WinVoice
Uses edge-tts (Microsoft Azure Neural) with pygame for playback.
Fixed: charmap codec crash on Windows by stripping all non-ASCII before printing.
"""
import re
import asyncio
import threading
import os
import sys
import tempfile
import unicodedata


# ── Ensure stdout/stderr never crash on emoji/special chars ──────────────
# This is what caused "'charmap' codec can't encode characters" on Windows
if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, 'reconfigure'):
    try:
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass


# ── Lazy pygame init ─────────────────────────────────────────────────────
_pygame_ready    = False
_pygame_lock     = threading.Lock()
_tts_play_lock   = threading.Lock()

def _ensure_pygame():
    global _pygame_ready
    if _pygame_ready:
        return True
    with _pygame_lock:
        if _pygame_ready:
            return True
        try:
            import pygame
            pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
            _pygame_ready = True
            return True
        except Exception as e:
            _safe_print(f"[TTS] pygame init failed: {e}")
            return False


def _safe_print(msg: str):
    """Print without crashing on non-ASCII characters on Windows."""
    try:
        print(msg)
    except (UnicodeEncodeError, UnicodeDecodeError):
        try:
            print(msg.encode('ascii', errors='replace').decode('ascii'))
        except Exception:
            pass


def clean_text(text: str) -> str:
    """
    Strips markdown, URLs, emojis and control characters so the
    text sounds natural and never causes a codec error.
    """
    if not text:
        return ""

    # Remove markdown formatting
    text = text.replace('**', '').replace('*', '').replace('__', '').replace('_', ' ')
    text = re.sub(r'#{1,6}\s*', '', text)

    # Remove URLs
    text = re.sub(r'https?://\S+', 'a link', text)

    # Remove emoji and non-BMP characters using unicode categories
    cleaned = []
    for ch in text:
        cat = unicodedata.category(ch)
        # Keep: letters, numbers, punctuation, spaces, marks
        # Drop: So (other symbol = emoji range), Cs (surrogates)
        if cat.startswith(('L', 'N', 'P', 'Z', 'M')):
            cleaned.append(ch)
        elif ch in (' ', '\n', '\t', '.', ',', '!', '?', ':', ';', '-', "'", '"'):
            cleaned.append(ch)
        # Everything else (emoji, symbols, etc.) → space
        else:
            cleaned.append(' ')
    text = ''.join(cleaned)

    # Collapse multiple spaces/newlines
    text = re.sub(r'\s+', ' ', text).strip()
    return text


async def _generate_and_play(text: str, voice: str = "en-GB-RyanNeural"):
    text = clean_text(text)
    if not text:
        return

    # Truncate to first 2 sentences so TTS stays snappy
    sentences = re.split(r'(?<=[.!?])\s+', text)
    if len(sentences) > 2:
        text = ' '.join(sentences[:2])

    if len(text) < 3:
        return

    import uuid
    temp_file = os.path.join(tempfile.gettempdir(), f"winvoice_tts_{uuid.uuid4().hex}.mp3")

    try:
        import edge_tts
        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(temp_file)
    except ImportError:
        _safe_print("[TTS] edge-tts not installed — run: pip install edge-tts")
        return
    except Exception as e:
        _safe_print(f"[TTS] edge-tts error: {e}")
        return

    if not _ensure_pygame():
        try:
            if os.path.exists(temp_file):
                os.remove(temp_file)
        except Exception:
            pass
        return

    try:
        import pygame
        with _tts_play_lock:
            pygame.mixer.music.load(temp_file)
            pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            await asyncio.sleep(0.05)
        with _tts_play_lock:
            pygame.mixer.music.unload()
    except Exception as e:
        _safe_print(f"[TTS] Playback error: {e}")
    finally:
        try:
            if os.path.exists(temp_file):
                os.remove(temp_file)
        except Exception:
            pass


def speak(text: str):
    """
    Non-blocking: fire-and-forget TTS in a daemon thread.
    Safe to call from any thread, including asyncio workers.
    """
    if not text or not text.strip():
        return

    def _run():
        try:
            asyncio.run(_generate_and_play(text))
        except Exception as e:
            _safe_print(f"[TTS] speak() thread error: {e}")

    t = threading.Thread(target=_run, daemon=True, name="TTS_Thread")
    t.start()


def speak_stream(text_generator):
    """
    Consumes a text generator token-by-token.
    When a sentence boundary is reached, it generates and plays the TTS chunk instantly.
    """
    def _run():
        buffer = ""
        for chunk in text_generator:
            buffer += chunk
            while True:
                match = re.search(r'([.?!]+(?:\s+|\n)|\n)', buffer)
                if match:
                    split_idx = match.end()
                    sentence = buffer[:split_idx].strip()
                    buffer = buffer[split_idx:]
                    if sentence:
                        try:
                            asyncio.run(_generate_and_play(sentence))
                        except Exception as e:
                            _safe_print(f"[TTS Stream] error: {e}")
                else:
                    break
        
        if buffer.strip():
            try:
                asyncio.run(_generate_and_play(buffer.strip()))
            except Exception as e:
                _safe_print(f"[TTS Stream] Final flush error: {e}")

    t = threading.Thread(target=_run, daemon=True, name="TTS_Stream_Thread")
    t.start()