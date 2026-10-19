# spotify_agent.py
# Requires: pip install spotipy
# Token is saved by main.py via the homepage OAuth flow.
# This agent READS that token — it never opens a browser itself.
import json
import os
import time
from ai_backend import _call_ollama_chat

SPOTIFY_SYSTEM_PROMPT = """You are an intelligent Spotify assistant. Parse ANY music/Spotify request and output ONLY valid JSON.

ACTIONS:
play(query)                    - play a song, artist, album, or playlist
play_artist(artist_name)       - play songs by an artist
play_album(album_name, artist?)- play an album
play_playlist(playlist_name)   - play a playlist
pause()                        - pause playback
resume()                       - resume playback
toggle()                       - toggle play/pause
next_track()                   - skip to next song
previous_track()               - go to previous song
set_volume(level)              - set volume 0-100
get_current()                  - what is currently playing
search(query)                  - search for music
shuffle(state)                 - toggle shuffle on/off
repeat(state)                  - set repeat: off/track/context
queue_track(query)             - add song to queue

OUTPUT FORMAT:
{"action":"action_name","data":{"field":"value"}}

MULTI-STEP OUTPUT:
If user asks multiple Spotify tasks in one sentence, output:
{"actions":[{"action":"...","data":{...}}, {"action":"...","data":{...}}]}

INTELLIGENCE RULES:
- "play X" → play with query=X
- "play songs by X" / "play X music" → play_artist
- "pause / stop music" → pause
- "resume / continue / unpause" → resume
- "toggle / play pause / switch playback" → toggle
- "next / skip / next song" → next_track
- "previous / back / last song" → previous_track
- "volume X" / "set volume to X" → set_volume with level=X (0-100)
- "what's playing / current song" → get_current
- "shuffle on/off" → shuffle with state true/false
- "repeat" → repeat with state "track" or "off"
- "queue X" / "add X to queue" → queue_track
- "play ... with shuffle on/off" → include shuffle_state true/false in the same play action

EXAMPLES:
User: play shape of you
{"steps": [{"action":"play","data":{"query":"Shape of You Ed Sheeran"}}]}

User: play songs by arijit singh
{"steps": [{"action":"play_artist","data":{"artist_name":"Arijit Singh"}}]}

User: play lofi hip hop
{"steps": [{"action":"play","data":{"query":"lofi hip hop"}}]}

User: play bollywood hits playlist
{"action":"play_playlist","data":{"playlist_name":"bollywood hits"}}

User: play the album divide by ed sheeran
{"action":"play_album","data":{"album_name":"Divide","artist":"Ed Sheeran"}}

User: pause the music
{"steps": [{"action":"pause","data":{}}]}

User: resume
{"steps": [{"action":"resume","data":{}}]}

User: toggle playback
{"action":"toggle","data":{}}

User: next song
{"steps": [{"action":"next_track","data":{}}]}

User: previous track
{"steps": [{"action":"previous_track","data":{}}]}

User: set spotify volume to 70
{"action":"set_volume","data":{"level":70}}

User: what song is playing
{"steps": [{"action":"get_current","data":{}}]}

User: turn on shuffle
{"steps": [{"action":"shuffle","data":{"state":true}}]}

User: turn off repeat
{"action":"repeat","data":{"state":"off"}}

User: add blinding lights to queue
{"action":"queue_track","data":{"query":"Blinding Lights The Weeknd"}}

User: search for dua lipa songs
{"action":"search","data":{"query":"dua lipa"}}

User: play my playlist with shuffle off
{"action":"play_playlist","data":{"playlist_name":"my playlist","shuffle_state":false}}

User: play my playlist with shuffle on
{"action":"play_playlist","data":{"playlist_name":"my playlist","shuffle_state":true}}

User: add blinding lights to queue and play my playlist with shuffle off
{"actions":[{"action":"queue_track","data":{"query":"Blinding Lights The Weeknd"}},{"action":"play_playlist","data":{"playlist_name":"my playlist","shuffle_state":false}}]}
"""


# Path where main.py saves the token after the user connects via homepage
SPOTIFY_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".spotify_token")


def _fix_wrapper(plan: dict) -> dict:
    if not plan:
        return plan
    action = plan.get("action")
    data   = plan.get("data")
    if action and not data:
        top = {k: v for k, v in plan.items() if k != "action"}
        if top:
            return {"action": action, "data": top}
    return plan


def _get_spotify():
    """
    Build a spotipy.Spotify instance using the token saved by main.py.
    Never opens a browser. Returns (sp, None) on success or (None, error_string).
    """
    if not os.path.exists(SPOTIFY_CACHE):
        return None, (
            "Spotify not connected.\n"
            "   Go to the WinVoice homepage and click 'Connect Spotify' first."
        )

    try:
        import spotipy
        from spotipy.oauth2 import SpotifyOAuth
        from dotenv import load_dotenv
        load_dotenv()

        client_id     = os.getenv("SPOTIFY_CLIENT_ID", "")
        client_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "")
        redirect_uri  = os.getenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8000/auth/spotify/callback")

        if not client_id or not client_secret:
            return None, (
                "Spotify credentials missing in .env\n"
                "   Add SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET"
            )

        # Read the saved token
        with open(SPOTIFY_CACHE) as f:
            token_data = json.load(f)

        # Check if token is expired and refresh if needed
        expires_at = token_data.get("expires_at", 0)
        if time.time() > expires_at - 60:
            token_data = _refresh_token(token_data, client_id, client_secret)
            if token_data is None:
                return None, (
                    "Spotify token expired and refresh failed.\n"
                    "   Please reconnect Spotify from the homepage."
                )

        # Build spotipy using the token directly (no browser)
        scope = (
            "user-modify-playback-state user-read-playback-state "
            "user-read-currently-playing streaming playlist-read-private "
            "user-library-read"
        )
        auth = SpotifyOAuth(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scope=scope,
            cache_path=SPOTIFY_CACHE,
            open_browser=False,   # ← NEVER open browser
        )
        # Inject the token directly into the cache so spotipy reads it
        auth.cache_handler.save_token_to_cache(token_data)
        sp = spotipy.Spotify(auth_manager=auth)
        return sp, None

    except ImportError:
        return None, "spotipy not installed. Run: pip install spotipy"
    except Exception as e:
        return None, f"Spotify error: {e}"


def _refresh_token(token_data: dict, client_id: str, client_secret: str) -> dict | None:
    """Refresh a Spotify access token using the refresh_token."""
    import base64
    import requests as req
    refresh_token = token_data.get("refresh_token", "")
    if not refresh_token:
        return None
    try:
        creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        r = req.post(
            "https://accounts.spotify.com/api/token",
            headers={
                "Authorization": f"Basic {creds}",
                "Content-Type":  "application/x-www-form-urlencoded",
            },
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            timeout=10,
        )
        if r.status_code != 200:
            return None
        nd = r.json()
        token_data["access_token"] = nd["access_token"]
        token_data["expires_at"]   = int(time.time()) + int(nd.get("expires_in", 3600))
        if "refresh_token" in nd:
            token_data["refresh_token"] = nd["refresh_token"]
        # Save refreshed token
        with open(SPOTIFY_CACHE, "w") as f:
            json.dump(token_data, f)
        return token_data
    except Exception:
        return None


def _active_device(sp):
    """Return the active Spotify device ID, or first available."""
    devices = sp.devices().get("devices", [])
    if not devices:
        return None
    active = next((d for d in devices if d.get("is_active")), None)
    return (active or devices[0])["id"]


class SpotifyAgent:
    def __init__(self):
        pass

    def run(self, query: str, status_callback=None, piped_context: str = None) -> str:
        q = (query or "").strip()
        ql = q.lower()

        plan = None
        steps = []

        # 1) Try AI parse
        raw = _call_ollama_chat(system=SPOTIFY_SYSTEM_PROMPT, user=q + "\nJSON:")
        if raw:
            try:
                if isinstance(raw, dict):
                    plan = raw
                else:
                    # try strict parse
                    plan = json.loads(str(raw))
                plan = _fix_wrapper(plan) if isinstance(plan, dict) else plan
            except Exception:
                plan = None

        # 2) Normalize steps from AI
        if isinstance(plan, dict) and isinstance(plan.get("steps"), list):
            steps = plan["steps"]
        elif isinstance(plan, dict) and plan.get("action"):
            steps = [plan]

        # 3) Deterministic fallback if AI parse failed
        if not steps:
            if "toggle" in ql or "play pause" in ql or "switch playback" in ql:
                steps = [{"action": "toggle", "data": {}}]
            elif "pause" in ql or "stop music" in ql:
                steps = [{"action": "pause", "data": {}}]
            elif "resume" in ql or "continue" in ql or "unpause" in ql:
                steps = [{"action": "resume", "data": {}}]
            elif "next" in ql or "skip" in ql:
                steps = [{"action": "next_track", "data": {}}]
            elif "previous" in ql or "back song" in ql:
                steps = [{"action": "previous_track", "data": {}}]
            elif "what song" in ql or "current" in ql or ("playing" in ql and "play " not in ql):
                steps = [{"action": "get_current", "data": {}}]
            elif "volume" in ql:
                import re
                m = re.search(r'(\d{1,3})', ql)
                level = int(m.group(1)) if m else 50
                level = max(0, min(100, level))
                steps = [{"action": "set_volume", "data": {"level": level}}]
            elif ql.startswith("play ") or " play " in f" {ql} ":
                song = q
                song = song.lower().replace("play", "", 1).strip() if ql.startswith("play ") else q
                steps = [{"action": "play", "data": {"query": song or q}}]
            else:
                steps = [{"action": "get_current", "data": {}}]

        sp, err = _get_spotify()
        if err:
            return f"❌ {err}"

        outputs = []
        for step in steps:
            action = (step.get("action") or "").strip()
            data = step.get("data", {}) or {}
            shuffle_state = data.get("shuffle_state", None)

            try:
                if action == "play":
                    out = self._play(sp, data.get("query", ""), shuffle_state)
                elif action == "play_artist":
                    out = self._play_artist(sp, data.get("artist_name", ""), shuffle_state)
                elif action == "play_album":
                    out = self._play_album(sp, data.get("album_name", ""), data.get("artist", ""), shuffle_state)
                elif action == "play_playlist":
                    out = self._play_playlist(sp, data.get("playlist_name", ""), shuffle_state)
                elif action == "pause":
                    out = self._pause(sp)
                elif action == "resume":
                    out = self._resume(sp)
                elif action == "toggle":
                    out = self._toggle(sp)
                elif action == "next_track":
                    out = self._next(sp)
                elif action == "previous_track":
                    out = self._prev(sp)
                elif action == "set_volume":
                    out = self._volume(sp, int(data.get("level", 50)))
                elif action == "get_current":
                    out = self._current(sp)
                elif action == "shuffle":
                    out = self._shuffle(sp, data.get("state", True))
                elif action == "repeat":
                    out = self._repeat(sp, data.get("state", "off"))
                elif action == "queue_track":
                    out = self._queue(sp, data.get("query", ""))
                elif action == "search":
                    out = self._search(sp, data.get("query", ""))
                else:
                    out = self._current(sp)
                outputs.append(out)
            except Exception as e:
                outputs.append(f"❌ Spotify error on '{action}': {e}")

        return "\n".join(outputs)

    def _device_id(self, sp) -> str:
        d = _active_device(sp)
        if d:
            return d
            
        import os
        import time
        print("   🎵 No active device found. Opening Spotify app...")
        os.system("start spotify:")
        
        # Wait up to 6 seconds for Spotify to open and register with the Web API
        for _ in range(6):
            time.sleep(1)
            d = _active_device(sp)
            if d:
                return d
                
        raise Exception(
            "No active Spotify device found.\n"
            "   Tried to open Spotify automatically, but it took too long to load."
        )

    def _normalize_shuffle(self, shuffle_state):
        if isinstance(shuffle_state, str):
            return shuffle_state.lower() in ("true", "on", "yes", "1")
        return bool(shuffle_state)

    def _play(self, sp, query: str, shuffle_state=None) -> str:
        if not query:
            return "❌ What do you want to play?"
        device_id = self._device_id(sp)

        if shuffle_state is not None:
            sp.shuffle(self._normalize_shuffle(shuffle_state), device_id=device_id)

        results = sp.search(q=query, type="track,playlist,album,artist", limit=5)
        tracks = results.get("tracks", {}).get("items", [])
        if tracks:
            t = tracks[0]
            sp.start_playback(device_id=device_id, uris=[t["uri"]])
            return f"🎵 Now playing — \"{t['name']}\" by {t['artists'][0]['name']}"
        playlists = results.get("playlists", {}).get("items", [])
        if playlists:
            p = playlists[0]
            sp.start_playback(device_id=device_id, context_uri=p["uri"])
            return f"🎵 Now playing playlist — \"{p['name']}\""
        return f"❌ Nothing found for \"{query}\" on Spotify."

    def _play_artist(self, sp, artist: str, shuffle_state=None) -> str:
        if not artist:
            return "❌ Artist name required."
        device_id = self._device_id(sp)

        if shuffle_state is not None:
            sp.shuffle(self._normalize_shuffle(shuffle_state), device_id=device_id)

        results = sp.search(q=artist, type="artist", limit=5)
        artists = results.get("artists", {}).get("items", [])
        if not artists:
            return f"❌ Artist \"{artist}\" not found."
        a = artists[0]
        sp.start_playback(device_id=device_id, context_uri=a["uri"])
        return f"🎵 Playing songs by {a['name']}"

    def _play_album(self, sp, album: str, artist: str = "", shuffle_state=None) -> str:
        device_id = self._device_id(sp)

        if shuffle_state is not None:
            sp.shuffle(self._normalize_shuffle(shuffle_state), device_id=device_id)

        q = f"album:{album}" + (f" artist:{artist}" if artist else "")
        results = sp.search(q=q, type="album", limit=5)
        albums = results.get("albums", {}).get("items", [])
        if not albums:
            return f"❌ Album \"{album}\" not found."
        a = albums[0]
        sp.start_playback(device_id=device_id, context_uri=a["uri"])
        return f"🎵 Now playing album — \"{a['name']}\""

    def _play_playlist(self, sp, name: str, shuffle_state=None) -> str:
        device_id = self._device_id(sp)

        if shuffle_state is not None:
            sp.shuffle(self._normalize_shuffle(shuffle_state), device_id=device_id)

        # Check user's own playlists first
        user_pl = sp.current_user_playlists(limit=50)
        for pl in user_pl.get("items", []):
            if name.lower() in pl.get("name", "").lower():
                sp.start_playback(device_id=device_id, context_uri=pl["uri"])
                return f"🎵 Playing your playlist — \"{pl['name']}\""
        # Search public playlists
        results = sp.search(q=name, type="playlist", limit=5)
        playlists = results.get("playlists", {}).get("items", [])
        if playlists:
            p = playlists[0]
            sp.start_playback(device_id=device_id, context_uri=p["uri"])
            return f"🎵 Playing playlist — \"{p['name']}\""
        return f"❌ Playlist \"{name}\" not found."

    def _pause(self, sp) -> str:
        sp.pause_playback(device_id=self._device_id(sp))
        return "⏸️ Music paused."

    def _resume(self, sp) -> str:
        sp.start_playback(device_id=self._device_id(sp))
        return "▶️ Resumed."

    def _toggle(self, sp) -> str:
        curr = sp.current_playback()
        if curr and curr.get("is_playing"):
            sp.pause_playback(device_id=self._device_id(sp))
            return "⏸️ Music paused."
        sp.start_playback(device_id=self._device_id(sp))
        return "▶️ Resumed."

    def _next(self, sp) -> str:
        sp.next_track(device_id=self._device_id(sp))
        time.sleep(0.6)
        return self._current(sp)

    def _prev(self, sp) -> str:
        sp.previous_track(device_id=self._device_id(sp))
        time.sleep(0.6)
        return self._current(sp)

    def _volume(self, sp, level: int) -> str:
        level = max(0, min(100, level))
        sp.volume(level, device_id=self._device_id(sp))
        return f"🔊 Spotify volume set to {level}%"

    def _current(self, sp) -> str:
        curr = sp.current_playback()
        if not curr or not curr.get("item"):
            return "🎵 Nothing is playing on Spotify right now."
        item    = curr["item"]
        name    = item["name"]
        artists = ", ".join(a["name"] for a in item["artists"])
        album   = item.get("album", {}).get("name", "")
        dur     = item.get("duration_ms", 0)
        prog    = curr.get("progress_ms", 0)
        fmt     = lambda ms: f"{ms//60000}:{(ms%60000)//1000:02d}"
        state   = "▶️" if curr.get("is_playing") else "⏸️"
        return (
            f"{state} \"{name}\" by {artists}\n"
            f"   Album: {album}  |  {fmt(prog)} / {fmt(dur)}"
        )

    def _shuffle(self, sp, state) -> str:
        if isinstance(state, str):
            state = state.lower() in ("true", "on", "yes", "1")
        sp.shuffle(state, device_id=self._device_id(sp))
        return f"🔀 Shuffle {'on' if state else 'off'}."

    def _repeat(self, sp, state: str) -> str:
        mapping = {
            "off": "off", "track": "track", "song": "track",
            "context": "context", "playlist": "context", "on": "context",
        }
        mode = mapping.get(str(state).lower(), "off")
        sp.repeat(mode, device_id=self._device_id(sp))
        return f"🔁 Repeat set to {mode}."

    def _queue(self, sp, query: str) -> str:
        results = sp.search(q=query, type="track", limit=1)
        tracks  = results.get("tracks", {}).get("items", [])
        if not tracks:
            return f"❌ Track \"{query}\" not found."
        t = tracks[0]
        sp.add_to_queue(t["uri"], device_id=self._device_id(sp))
        return f"➕ Queued — \"{t['name']}\" by {t['artists'][0]['name']}"

    def _search(self, sp, query: str) -> str:
        results = sp.search(q=query, type="track", limit=5)
        tracks  = results.get("tracks", {}).get("items", [])
        if not tracks:
            return f"❌ No results for \"{query}\"."
        lines = [f"  {i+1}. \"{t['name']}\" — {t['artists'][0]['name']}" for i, t in enumerate(tracks)]
        return f"🎵 Results for \"{query}\":\n" + "\n".join(lines)