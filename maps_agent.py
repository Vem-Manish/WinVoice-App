# maps_agent.py
# No API key needed — uses Google Maps URLs which open in browser
# Optional: Add GOOGLE_MAPS_API_KEY to .env for distance/place search
import json
import os
import webbrowser
import urllib.parse
import requests
from ai_backend import _call_ollama_chat
from dotenv import load_dotenv

load_dotenv()

MAPS_SYSTEM_PROMPT = """You are an intelligent Google Maps assistant. Parse ANY navigation/location request and output ONLY valid JSON.

ACTIONS:
navigate(destination, origin?)         - get directions to a place
search_nearby(query, location?)        - find places nearby (restaurants, hospitals, etc.)
search_place(query)                    - search for a specific place
distance(origin, destination)          - get distance between two places
open_maps(location)                    - open a location on Google Maps
street_view(location)                  - open street view of a location

OUTPUT FORMAT:
{"action":"action_name","data":{"field":"value"}}

INTELLIGENCE RULES:
- "navigate to / directions to / how to get to / take me to" → navigate
- "find / search for / nearest / nearby" + place type → search_nearby
- "how far is / distance to" → distance
- "show on map / open on maps / where is" → open_maps
- "street view of" → street_view
- location/destination: extract exact place name or address
- Default location for nearby searches: use "current location" as location

EXAMPLES:
User: navigate to HICC Hyderabad
{"action":"navigate","data":{"destination":"HICC Hyderabad"}}

User: directions to the nearest hospital
{"action":"search_nearby","data":{"query":"hospital","location":"current location"}}

User: how do I get to Mumbai airport
{"action":"navigate","data":{"destination":"Mumbai Airport"}}

User: find restaurants near me
{"action":"search_nearby","data":{"query":"restaurants","location":"current location"}}

User: find coffee shops near Hyderabad
{"action":"search_nearby","data":{"query":"coffee shops","location":"Hyderabad"}}

User: how far is Bangalore from Hyderabad
{"action":"distance","data":{"origin":"Hyderabad","destination":"Bangalore"}}

User: show me the Eiffel Tower on maps
{"action":"open_maps","data":{"location":"Eiffel Tower Paris"}}

User: where is the nearest pharmacy
{"action":"search_nearby","data":{"query":"pharmacy","location":"current location"}}

User: navigate from my home to the airport
{"action":"navigate","data":{"destination":"airport","origin":"home"}}

User: find petrol stations near me
{"action":"search_nearby","data":{"query":"petrol station","location":"current location"}}

User: street view of Times Square
{"action":"street_view","data":{"location":"Times Square New York"}}

User: distance from Delhi to Agra
{"action":"distance","data":{"origin":"Delhi","destination":"Agra"}}"""


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


def _encode(s: str) -> str:
    return urllib.parse.quote_plus(s)


def _maps_api_key() -> str | None:
    return os.getenv("GOOGLE_MAPS_API_KEY")


class MapsAgent:
    def __init__(self, default_city: str = "Hyderabad"):
        self.default_city = default_city

    def run(self, query: str, status_callback=None, piped_context: str = None) -> str:
        raw = _call_ollama_chat(system=MAPS_SYSTEM_PROMPT, user=query + "\nJSON:")
        if not raw:
            return "❌ AI returned empty response."
        plan = _fix_wrapper(raw)
        
        # Support both single action and steps array
        steps = plan.get("steps", [plan]) if "steps" in plan else [plan]
        
        results = []
        for step in steps:
            action = step.get("action", "")
            data = step.get("data", {})
            if not action:
                continue

            if action == "navigate":
                results.append(self._navigate(data))
            elif action == "search_nearby":
                results.append(self._search_nearby(data))
            elif action == "search_place":
                results.append(self._search_place(data))
            elif action == "distance":
                results.append(self._distance(data))
            elif action == "open_maps":
                results.append(self._open_maps(data))
            elif action == "street_view":
                results.append(self._street_view(data))
            else:
                results.append(f"❌ Unknown maps action: {action}")
                
        return "\n".join(results) if results else "❌ Could not understand the maps command."

    def _navigate(self, data: dict) -> str:
        dest = data.get("destination", "").strip()
        origin = data.get("origin", "").strip()
        if not dest:
            return "❌ Destination is required."

        if origin and origin.lower() not in ("home", "my location", "current location", "here"):
            url = f"https://www.google.com/maps/dir/{_encode(origin)}/{_encode(dest)}"
            msg = f"📍 Directions: {origin} → {dest}"
        else:
            url = f"https://www.google.com/maps/dir//{_encode(dest)}"
            msg = f"📍 Navigating to: {dest}"

        webbrowser.open(url)
        return f"🗺️ {msg}\n   Opening Google Maps..."

    def _search_nearby(self, data: dict) -> str:
        query = data.get("query", "").strip()
        location = data.get("location", "current location").strip()
        if not query:
            return "❌ Search query is required."

        if "current" in location.lower() or location.lower() in ("here", "nearby", "near me"):
            # Search near current location via Maps
            url = f"https://www.google.com/maps/search/{_encode(query)}"
        else:
            url = f"https://www.google.com/maps/search/{_encode(query + ' near ' + location)}"

        webbrowser.open(url)

        # Try Places API if key available
        key = _maps_api_key()
        if key and location.lower() not in ("current location", "here", "near me", "nearby"):
            try:
                r = requests.get(
                    "https://maps.googleapis.com/maps/api/place/textsearch/json",
                    params={"query": f"{query} near {location}", "key": key},
                    timeout=8
                )
                r.raise_for_status()
                resp_json = r.json()
                if resp_json.get("status") != "OK":
                    return f"❌ Places API error: {resp_json.get('status', 'Unknown error')}"
                results = resp_json.get("results", [])[:3]
                if results:
                    lines = [f"🔍 Found {query} near {location}:"]
                    for p in results:
                        name = p.get("name", "")
                        addr = p.get("formatted_address", "")
                        rating = p.get("rating", "N/A")
                        lines.append(f"   • {name} — ⭐{rating}\n     {addr}")
                    lines.append("\n   Opening Google Maps...")
                    return "\n".join(lines)
            except Exception:
                pass

        return f"🔍 Searching for {query} near {location}\n   Opening Google Maps..."

    def _search_place(self, data: dict) -> str:
        query = data.get("query", "").strip()
        if not query:
            return "❌ Search query required."
        url = f"https://www.google.com/maps/search/{_encode(query)}"
        webbrowser.open(url)
        return f"🔍 Searching for \"{query}\" on Google Maps..."

    def _distance(self, data: dict) -> str:
        origin = data.get("origin", "").strip()
        dest = data.get("destination", "").strip()
        if not origin or not dest:
            return "❌ Both origin and destination are required."

        # Open in Maps
        url = f"https://www.google.com/maps/dir/{_encode(origin)}/{_encode(dest)}"
        webbrowser.open(url)

        # Try Distance Matrix API if key available
        key = _maps_api_key()
        if key:
            try:
                r = requests.get(
                    "https://maps.googleapis.com/maps/api/distancematrix/json",
                    params={
                        "origins": origin,
                        "destinations": dest,
                        "key": key,
                        "units": "metric"
                    },
                    timeout=8
                )
                d = r.json()
                row = d.get("rows", [{}])[0]
                elem = row.get("elements", [{}])[0]
                if elem.get("status") == "OK":
                    dist = elem["distance"]["text"]
                    dur = elem["duration"]["text"]
                    return (
                        f"📏 {origin} → {dest}\n"
                        f"   Distance: {dist}\n"
                        f"   Drive time: ~{dur}\n"
                        f"   Opening Google Maps..."
                    )
            except Exception:
                pass

        return f"📏 {origin} → {dest}\n   Opening Google Maps for directions..."

    def _open_maps(self, data: dict) -> str:
        location = data.get("location", "").strip()
        if not location:
            return "❌ Location is required."
        url = f"https://www.google.com/maps/search/{_encode(location)}"
        webbrowser.open(url)
        return f"🗺️ Opened \"{location}\" on Google Maps."

    def _street_view(self, data: dict) -> str:
        location = data.get("location", "").strip()
        if not location:
            return "❌ Location is required."
        url = f"https://www.google.com/maps/@?api=1&map_action=pano&viewpoint={_encode(location)}"
        webbrowser.open(url)
        return f"🏙️ Opened Street View for \"{location}\"."