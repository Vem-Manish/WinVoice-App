"""
voice_auth.py — Wake-word detection and Speaker Verification for WinVoice
Uses openwakeword ONNX model for "Hey WinVoice" and SpeechBrain for Speaker Verification.

NOTE: numpy, pyaudio and torch are imported lazily (inside functions) to avoid
the 'NDArray circular import' crash that occurs when multiple modules import
numpy simultaneously during FastAPI startup.
"""

import os, time, threading
from collections import deque
from pathlib import Path
import requests

# ── FIX FOR WINDOWS ERROR 1314 ──
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
os.environ["HF_HUB_DISABLE_SYMLINKS"] = "1"

# ── Paths ──────────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent
MODEL_FILE = BASE_DIR / "hey_win_voice.onnx"
PROFILE_FILE = BASE_DIR / ".voice_profile.npy"

# ── Config ─────────────────────────────────────────────────────────────
WAKE_THRESHOLD = float(os.getenv("WAKE_THRESHOLD", "0.05"))
VERIFY_THRESHOLD = float(os.getenv("VERIFY_THRESHOLD", "0.200"))
WAKE_WORD      = "Hey WinVoice"

# ── Audio constants ────────────────────────────────────────────────────
FORMAT   = 8       # pyaudio.paInt16 = 8  (avoids importing pyaudio at module level)
CHANNELS = 1
RATE     = 16000
CHUNK    = 1280   # 80ms — required frame size for openwakeword

# ── Enrollment state ───────────────────────────────────────────────────
is_enrolling: bool     = False
temp_enroll_wavs: list = []
_enroll_buffer: list   = [] # Filled by the listener thread during enrollment

# ── Lazy singletons ────────────────────────────────────────────────────
_oww        = None
_oww_lock   = threading.Lock()
_model_key  = None

_spk_model  = None
_spk_lock   = threading.Lock()
_profile_emb = None

def _get_oww():
    global _oww, _model_key
    if _oww is not None: return _oww, _model_key, None
    with _oww_lock:
        if _oww is not None: return _oww, _model_key, None
        if not MODEL_FILE.exists(): return None, None, f"Model file not found: {MODEL_FILE}"
        try:
            from openwakeword.model import Model
            _oww       = Model(wakeword_models=[str(MODEL_FILE)])
            _model_key = list(_oww.models.keys())[0]
            print(f"[VoiceAuth] Wake model loaded: '{_model_key}'  threshold={WAKE_THRESHOLD}")
            return _oww, _model_key, None
        except Exception as e:
            return None, None, str(e)

def _get_spk_model():
    global _spk_model
    if _spk_model is not None: return _spk_model
    with _spk_lock:
        if _spk_model is not None: return _spk_model
        try:
            from speechbrain.inference.speaker import SpeakerRecognition
            import logging
            logging.getLogger("speechbrain").setLevel(logging.ERROR)

            _spk_model = SpeakerRecognition.from_hparams(
                source="speechbrain/spkrec-ecapa-voxceleb",
                savedir=str(BASE_DIR / "pretrained_models" / "spkrec-ecapa-voxceleb")
            )
            print("[VoiceAuth] Speaker Verification model loaded.")
            return _spk_model
        except Exception as e:
            print(f"[VoiceAuth] Could not load SpeechBrain: {e}")
            return None

def _load_profile():
    """Load speaker embedding from disk. Heavy imports are deferred here."""
    global _profile_emb
    if PROFILE_FILE.exists():
        try:
            import numpy as np
            import torch
            arr = np.load(PROFILE_FILE, allow_pickle=True)
            _profile_emb = torch.tensor(arr.astype(np.float32) if not arr.dtype == object else arr.tolist())
            print("[VoiceAuth] Speaker profile loaded successfully.")
        except Exception as e:
            print(f"[VoiceAuth] Error loading profile, resetting: {e}")
            PROFILE_FILE.unlink(missing_ok=True) # Delete corrupted file
            _profile_emb = None

def _load_profile_deferred():
    """Called from a background thread after startup to avoid circular imports."""
    import time as _t
    _t.sleep(3)  # wait for numpy to fully initialize across all modules
    _load_profile()

threading.Thread(target=_load_profile_deferred, daemon=True, name="VoiceProfileLoader").start()

def _bytes_to_tensor(pcm_bytes: bytes):
    import numpy as np
    import torch
    pcm = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    return torch.tensor(pcm).unsqueeze(0)

# ══════════════════════════════════════════════════════════════════════
# PUBLIC API
# ══════════════════════════════════════════════════════════════════════

def is_model_ready() -> bool:
    return MODEL_FILE.exists()

def is_enrolled() -> bool:
    return PROFILE_FILE.exists() and _profile_emb is not None

def record_sample(duration: float = 3.0) -> bytes:
    """
    Records audio by signaling the existing background PyAudio thread to
    pipe data into _enroll_buffer, completely avoiding concurrency crashes.
    """
    global is_enrolling, _enroll_buffer
    _enroll_buffer.clear()
    is_enrolling = True

    # Block logically while the background thread fills the buffer
    time.sleep(duration)

    is_enrolling = False
    pcm = b"".join(_enroll_buffer)
    if not pcm:
        raise Exception("Failed to capture audio. Is the microphone in use?")

    temp_enroll_wavs.append(pcm)
    return pcm

def commit_enrollment() -> bool:
    import numpy as np
    import torch
    global is_enrolling, _profile_emb
    if not temp_enroll_wavs: return False

    is_enrolling = True
    try:
        spk = _get_spk_model()
        if not spk: return False

        embeddings = []
        for pcm in temp_enroll_wavs:
            t = _bytes_to_tensor(pcm)
            emb = spk.encode_batch(t)
            embeddings.append(emb)

        avg_emb = torch.mean(torch.stack([e.squeeze() for e in embeddings]), dim=0)
        np.save(PROFILE_FILE, avg_emb.detach().cpu().numpy())
        _profile_emb = avg_emb
        temp_enroll_wavs.clear()
        print("[VoiceAuth] Enrollment committed successfully!")
        return True
    except Exception as e:
        print(f"[VoiceAuth] Enrollment failed: {e}")
        return False
    finally:
        is_enrolling = False

def delete_enrollment() -> bool:
    global _profile_emb
    temp_enroll_wavs.clear()
    _profile_emb = None
    if PROFILE_FILE.exists():
        PROFILE_FILE.unlink()
    return True

def verify_speaker(pcm: bytes) -> bool:
    import torch
    if not is_enrolled():
        return True # Allow anyone if no profile is set up

    spk = _get_spk_model()
    if not spk or _profile_emb is None:
        return True

    t = _bytes_to_tensor(pcm)
    emb = spk.encode_batch(t)

    # --- FIX: Force both tensors to CPU so PyTorch doesn't crash ---
    emb_cpu = emb.squeeze(0).cpu()
    prof_cpu = _profile_emb.squeeze(0).cpu()

    score = torch.nn.functional.cosine_similarity(emb_cpu, prof_cpu, dim=-1).item()
    print(f"[VoiceAuth] Verification Score: {score:.3f} (threshold={VERIFY_THRESHOLD})")

    return score >= VERIFY_THRESHOLD

# ══════════════════════════════════════════════════════════════════════
# WAKE WORD LISTENER
# ══════════════════════════════════════════════════════════════════════

class WakeWordListener:
    def __init__(self, on_wake):
        self.on_wake  = on_wake
        self._running = False
        self._thread  = threading.Thread(target=self._run, daemon=True, name="WakeWordListener")

    def start(self):
        self._running = True
        self._thread.start()
        threading.Thread(target=_get_spk_model, daemon=True).start()

    def stop(self):
        self._running = False

    def _run(self):
        import numpy as np
        import pyaudio
        oww, model_key, err = _get_oww()
        if err:
            print(f"[WakeWord] Cannot start: {err}")
            return

        pa     = pyaudio.PyAudio()
        stream = None
        try:
            stream = pa.open(format=FORMAT, channels=CHANNELS, rate=RATE, input=True, frames_per_buffer=CHUNK)
        except Exception as e:
            print(f"[WakeWord] Failed to open stream: {e}")
            pa.terminate()
            return
            
        print(f"[WakeWord] Listening for '{WAKE_WORD}'… (threshold={WAKE_THRESHOLD})")

        # 2.5 second ring buffer (Fast lookup)
        RING_CHUNKS = int((RATE * 2.5) / CHUNK)
        audio_buffer = deque(maxlen=RING_CHUNKS)

        try:
            while self._running:
                raw = stream.read(CHUNK, exception_on_overflow=False)

                # If enrolling, pipe audio to the temp buffer instead of predicting
                if is_enrolling:
                    _enroll_buffer.append(raw)
                    continue

                audio_buffer.append(raw)
                pcm = np.frombuffer(raw, dtype=np.int16)
                preds = oww.predict(pcm)
                score = float(preds.get(model_key, 0.0))

                # --- RESTORED: Debug prints for low-confidence detections ---
                if score >= 0.01:
                    print(f"[WakeWord Debug] Score: {score:.4f}")

                if score >= WAKE_THRESHOLD:
                    print(f"[WakeWord] '{WAKE_WORD}' detected! score={score:.3f}")

                    # --- CRASH-PROOF WRAPPER ---
                    try:
                        if is_enrolled():
                            buffered_raw = b"".join(audio_buffer)
                            print(f"[WakeWord] Verifying speaker...")
                            if not verify_speaker(buffered_raw):
                                print(f"[WakeWord] ❌ Speaker verification failed. Ignoring.")
                                time.sleep(1.0)
                                continue
                            print(f"[WakeWord] ✅ Speaker VERIFIED!")

                        self.on_wake()

                        # --- ELECTRON WAKE WORD BRIDGE TRIGGER ---
                        try:
                            requests.post('http://127.0.0.1:8000/wake/trigger', timeout=1)
                        except Exception:
                            pass

                    except Exception as e:
                        print(f"[WakeWord] ❌ Error during wake process: {e}")

                    # Brief cooldown to prevent double-fires
                    time.sleep(1.5)
                    audio_buffer.clear()

        finally:
            if stream:
                stream.stop_stream()
                stream.close()
            pa.terminate()
            print("[WakeWord] Listener stopped")