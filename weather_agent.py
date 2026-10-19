# weather_agent.py
# Uses Open-Meteo API — completely free, no API key, no signup, no rate limits
# Geocoding:  https://geocoding-api.open-meteo.com  (city name → lat/lon)
# Weather:    https://api.open-meteo.com            (lat/lon → weather data)
# Requires:   pip install requests  (already installed)

import json
from datetime import datetime, timedelta
import requests
from ai_backend import _call_ollama_chat

WEATHER_SYSTEM_PROMPT = """You are an intelligent Weather assistant. Parse ANY weather request and output ONLY valid JSON.

ACTIONS:
current(city)           - current weather conditions
forecast(city, days?)   - multi-day forecast (1-7 days)
hourly(city)            - hour-by-hour forecast for today
will_rain(city, date?)  - rain check for today or tomorrow
temperature(city)       - quick temperature summary
outfit(city)            - clothing suggestion based on weather

OUTPUT FORMAT:
{"action":"action_name","data":{"field":"value"}}

INTELLIGENCE RULES:
- "weather in X / how is the weather in X" → current
- "weather today" (no city) → current with city: "Hyderabad"
- "forecast / next X days" → forecast with days
- "will it rain / umbrella / chance of rain" → will_rain
- "temperature / how hot / how cold" → temperature
- "hourly / hour by hour / what time will it rain" → hourly
- city: extract exactly as said. Default to "Hyderabad" if no city mentioned
- days: default 3, max 7

EXAMPLES:
User: what is the weather today
{"steps": [{"action":"current","data":{"city":"Hyderabad"}}]}

User: weather in mumbai
{"steps": [{"action":"current","data":{"city":"Mumbai"}}]}

User: what is the weather in london
{"steps": [{"action":"current","data":{"city":"London"}}]}

User: weather in new york
{"steps": [{"action":"current","data":{"city":"New York"}}]}

User: forecast for bangalore for next 3 days
{"steps": [{"action":"forecast","data":{"city":"Bangalore","days":3}}]}

User: 5 day forecast for delhi
{"steps": [{"action":"forecast","data":{"city":"Delhi","days":5}}]}

User: will it rain tomorrow in hyderabad
{"steps": [{"action":"will_rain","data":{"city":"Hyderabad","date":"tomorrow"}}]}

User: should I carry an umbrella today
{"steps": [{"action":"will_rain","data":{"city":"Hyderabad","date":"today"}}]}

User: will it rain in mumbai today
{"steps": [{"action":"will_rain","data":{"city":"Mumbai","date":"today"}}]}

User: what is the temperature in delhi
{"action":"temperature","data":{"city":"Delhi"}}

User: how hot is it in chennai
{"action":"temperature","data":{"city":"Chennai"}}

User: hourly forecast for hyderabad
{"action":"hourly","data":{"city":"Hyderabad"}}

User: what time will it rain today in bangalore
{"action":"hourly","data":{"city":"Bangalore"}}

User: how is the weather in paris
{"action":"current","data":{"city":"Paris"}}"""

WMO_CODES = {
    0:  ("Clear sky", "☀️"),
    1:  ("Mainly clear", "🌤️"),
    2:  ("Partly cloudy", "⛅"),
    3:  ("Overcast", "☁️"),
    45: ("Foggy", "🌫️"),
    48: ("Icy fog", "🌫️"),
    51: ("Light drizzle", "🌦️"),
    53: ("Moderate drizzle", "🌦️"),
    55: ("Dense drizzle", "🌧️"),
    61: ("Slight rain", "🌧️"),
    63: ("Moderate rain", "🌧️"),
    65: ("Heavy rain", "🌧️"),
    71: ("Slight snow", "❄️"),
    73: ("Moderate snow", "❄️"),
    75: ("Heavy snow", "❄️"),
    77: ("Snow grains", "🌨️"),
    80: ("Slight showers", "🌦️"),
    81: ("Moderate showers", "🌧️"),
    82: ("Violent showers", "⛈️"),
    85: ("Slight snow showers", "🌨️"),
    86: ("Heavy snow showers", "🌨️"),
    95: ("Thunderstorm", "⛈️"),
    96: ("Thunderstorm w/ hail", "⛈️"),
    99: ("Thunderstorm w/ hail", "⛈️"),
}

RAIN_CODES = {51,53,55,61,63,65,80,81,82,95,96,99}

def _wmo(code: int) -> tuple[str, str]:
    return WMO_CODES.get(code, ("Unknown", "🌡️"))


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



def _geocode(city: str) -> tuple[float, float, str]:
    """Return (latitude, longitude, resolved_name) for a city name."""
    r = requests.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": city, "count": 1, "language": "en", "format": "json"},
        timeout=10,
        headers={"User-Agent": "WinVoice/1.0", "Connection": "close"}
    )
    r.raise_for_status()
    results = r.json().get("results", [])
    if not results:
        raise ValueError(f'City "{city}" not found. Try a different spelling.')
    loc = results[0]
    name = loc.get("name", city)
    country = loc.get("country", "")
    display = f"{name}, {country}" if country else name
    return float(loc["latitude"]), float(loc["longitude"]), display



def _get_current(lat: float, lon: float) -> dict:
    r = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude":  lat,
            "longitude": lon,
            "current": ",".join([
                "temperature_2m", "apparent_temperature",
                "relative_humidity_2m", "weather_code",
                "wind_speed_10m", "precipitation",
                "surface_pressure", "cloud_cover",
            ]),
            "timezone": "auto",
        },
        timeout=10,
        headers={"User-Agent": "WinVoice/1.0", "Connection": "close"}
    )
    r.raise_for_status()
    return r.json()["current"]


def _get_daily(lat: float, lon: float, days: int = 7) -> dict:
    r = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude":  lat,
            "longitude": lon,
            "daily": ",".join([
                "weather_code", "temperature_2m_max", "temperature_2m_min",
                "precipitation_sum", "precipitation_probability_max",
                "wind_speed_10m_max",
            ]),
            "timezone":     "auto",
            "forecast_days": min(days, 7),
        },
        timeout=10,
        headers={"User-Agent": "WinVoice/1.0", "Connection": "close"}
    )
    r.raise_for_status()
    return r.json()["daily"]


def _get_hourly(lat: float, lon: float) -> dict:
    r = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude":  lat,
            "longitude": lon,
            "hourly": ",".join([
                "temperature_2m", "weather_code",
                "precipitation_probability", "precipitation",
            ]),
            "timezone":     "auto",
            "forecast_days": 1,
        },
        timeout=10,
        headers={"User-Agent": "WinVoice/1.0", "Connection": "close"}
    )
    r.raise_for_status()
    return r.json()["hourly"]


def _get_air_quality(lat: float, lon: float) -> dict:
    r = requests.get(
        "https://air-quality-api.open-meteo.com/v1/air-quality",
        params={
            "latitude": lat,
            "longitude": lon,
            "current": "us_aqi,pm2_5,pm10",
            "timezone": "auto",
        },
        timeout=10,
        headers={"User-Agent": "WinVoice/1.0", "Connection": "close"}
    )
    r.raise_for_status()
    return r.json().get("current", {})



class WeatherAgent:
    def __init__(self, default_city: str = "Hyderabad"):
        self.default_city = default_city

    def run(self, query: str, status_callback=None, piped_context: str = None) -> str:
        q = (query or "").strip()
        ql = q.lower()

        action = None
        data = {}

        # 1) Try AI parse first
        raw = _call_ollama_chat(system=WEATHER_SYSTEM_PROMPT, user=q + "\nJSON:")
        if raw:
            try:
                plan = raw if isinstance(raw, dict) else _fix_wrapper(json.loads(str(raw)))
                if not isinstance(plan, dict):
                    plan = {}
                action = (plan.get("action") or "").strip().lower()
                data = plan.get("data", {}) or {}
            except Exception:
                action = None
                data = {}

        # 2) Deterministic fallback if AI failed
        if not action:
            if "wear" in ql or "outfit" in ql:
                action = "outfit"
            elif "forecast" in ql or "next" in ql:
                action = "forecast"
            elif "hourly" in ql:
                action = "hourly"
            elif "rain" in ql or "umbrella" in ql:
                action = "will_rain"
            elif "temperature" in ql or "hot" in ql or "cold" in ql:
                action = "temperature"
            else:
                action = "current"

        city = (data.get("city") or self.default_city).strip()
        try:
            days = int(data.get("days", 3))
        except (ValueError, TypeError):
            days = 3
        date_hint = (data.get("date") or "today").strip()

        try:
            lat, lon, display_name = _geocode(city)
        except ValueError as e:
            return f"❌ {e}"
        except Exception as e:
            return f"❌ Geocoding failed: {e}"

        try:
            if action == "current":
                return self._current(lat, lon, display_name)
            elif action == "forecast":
                return self._forecast(lat, lon, display_name, days)
            elif action == "will_rain":
                return self._will_rain(lat, lon, display_name, date_hint)
            elif action == "temperature":
                return self._temperature(lat, lon, display_name)
            elif action == "hourly":
                return self._hourly(lat, lon, display_name)
            elif action == "outfit":
                return self._outfit(lat, lon, display_name)
            else:
                return self._current(lat, lon, display_name)
        except Exception as e:
            return f"❌ Weather error: {e}"

    def _current(self, lat, lon, name) -> str:
        c = _get_current(lat, lon)
        temp     = round(c["temperature_2m"])
        feels    = round(c["apparent_temperature"])
        humidity = c["relative_humidity_2m"]
        wind     = round(c["wind_speed_10m"])
        code     = c["weather_code"]
        desc, icon = _wmo(code)
        try:
            aq = _get_air_quality(lat, lon)
            aqi = aq.get("us_aqi")
            aq_line = f"\n   AQI: {aqi}" if aqi is not None else ""
        except Exception:
            aq_line = ""

        if temp >= 38:
            hint = "\n   Extremely hot - stay hydrated!"
        elif temp >= 32:
            hint = "\n   Hot day - keep water handy."
        elif temp <= 10:
            hint = "\n   Cold - dress warmly."
        elif code in RAIN_CODES:
            hint = "\n   Rain detected - carry an umbrella."
        else:
            hint = ""
        if code in {95, 96, 99}:
            hint += "\n   Severe weather alert: thunderstorm risk."
        if temp >= 40:
            hint += "\n   Severe heat alert: avoid long outdoor exposure."

        return (
            f"{icon} Weather in {name}\n"
            f"   Temperature: {temp}C (feels like {feels}C)\n"
            f"   Conditions: {desc}\n"
            f"   Humidity: {humidity}%\n"
            f"   Wind: {wind} km/h"
            f"{aq_line}"
            f"{hint}"
        )

    def _temperature(self, lat, lon, name) -> str:
        c    = _get_current(lat, lon)
        temp = round(c["temperature_2m"])
        feels = round(c["apparent_temperature"])
        code = c["weather_code"]
        _, icon = _wmo(code)

        # Get today's high/low from daily
        daily = _get_daily(lat, lon, days=1)
        high = round(daily["temperature_2m_max"][0])
        low  = round(daily["temperature_2m_min"][0])

        return (
            f"{icon} {name}: {temp}°C\n"
            f"   Feels like {feels}°C  |  High {high}°C / Low {low}°C"
        )

    def _outfit(self, lat, lon, name) -> str:
        c = _get_current(lat, lon)
        daily = _get_daily(lat, lon, days=1)
        temp = round(c["temperature_2m"])
        feels = round(c["apparent_temperature"])
        code = c["weather_code"]
        rain_pct = daily["precipitation_probability_max"][0]
        desc, icon = _wmo(code)
        parts = []
        if feels >= 34:
            parts.append("light cotton or linen")
        elif feels <= 18:
            parts.append("a light jacket or hoodie")
        else:
            parts.append("comfortable casual wear")
        if code in RAIN_CODES or rain_pct >= 40:
            parts.append("water-resistant shoes")
            parts.append("an umbrella or rain jacket")
        if temp >= 38:
            parts.append("sunscreen and a cap")
        return (
            f"{icon} Outfit suggestion for {name}: wear {', '.join(parts)}.\n"
            f"   Weather: {temp}C, feels like {feels}C, {desc}, rain chance {rain_pct}%."
        )


    def _forecast(self, lat, lon, name, days: int = 3) -> str:
        days  = max(1, min(7, days))
        daily = _get_daily(lat, lon, days=days)

        lines = [f"📅 {days}-day forecast — {name}:"]
        for i in range(days):
            date_str = daily["time"][i]
            dt       = datetime.strptime(date_str, "%Y-%m-%d")
            day_name = dt.strftime("%A")
            high     = round(daily["temperature_2m_max"][i])
            low      = round(daily["temperature_2m_min"][i])
            code     = daily["weather_code"][i]
            rain_mm  = daily["precipitation_sum"][i]
            rain_pct = daily["precipitation_probability_max"][i]
            desc, icon = _wmo(code)

            rain_str = f" | 🌧️ {rain_pct}% ({rain_mm}mm)" if rain_pct > 20 else ""
            lines.append(
                f"   {icon} {day_name} ({date_str}):  "
                f"{low}°C – {high}°C  |  {desc}{rain_str}"
            )
        return "\n".join(lines)


    def _will_rain(self, lat, lon, name, date_str: str = "today") -> str:
        is_tomorrow = "tomorrow" in (date_str or "").lower()
        target = datetime.today()
        if is_tomorrow:
            target += timedelta(days=1)
        target_str = target.strftime("%Y-%m-%d")
        day_label  = "tomorrow" if is_tomorrow else "today"

        daily = _get_daily(lat, lon, days=2)
        idx = None
        for i, d in enumerate(daily["time"]):
            if d == target_str:
                idx = i
                break

        if idx is None:
            return f"❌ Could not find forecast for {day_label}."

        code     = daily["weather_code"][idx]
        rain_pct = daily["precipitation_probability_max"][idx]
        rain_mm  = daily["precipitation_sum"][idx]
        desc, icon = _wmo(code)

        if code in RAIN_CODES or rain_pct >= 40:
            return (
                f"🌧️ Yes, expect rain in {name} {day_label}.\n"
                f"   {icon} {desc}  |  {rain_pct}% chance  |  ~{rain_mm}mm\n"
                f"   🌂 Carry an umbrella!"
            )
        elif rain_pct >= 20:
            return (
                f"🌦️ Possible rain in {name} {day_label} ({rain_pct}% chance).\n"
                f"   {icon} {desc}\n"
                f"   🌂 Maybe take an umbrella just in case."
            )
        else:
            return (
                f"☀️ No significant rain expected in {name} {day_label}.\n"
                f"   {icon} {desc}  |  {rain_pct}% chance of rain\n"
                f"   ✅ You're good — no umbrella needed!"
            )


    def _hourly(self, lat, lon, name) -> str:
        hourly   = _get_hourly(lat, lon)
        now_hour = datetime.now().hour

        lines = [f"🕐 Hourly forecast — {name} today:"]
        shown = 0
        for i, time_str in enumerate(hourly["time"]):
            hour = int(time_str.split("T")[1][:2])
            if hour < now_hour:
                continue
            if shown >= 8:
                break

            temp      = round(hourly["temperature_2m"][i])
            code      = hourly["weather_code"][i]
            rain_pct  = hourly["precipitation_probability"][i]
            rain_mm   = hourly["precipitation"][i]
            desc, icon = _wmo(code)

            # Format time
            suffix  = "AM" if hour < 12 else "PM"
            hour12  = hour if 1 <= hour <= 12 else (hour - 12 if hour > 12 else 12)
            t_label = f"{hour12:02d}:00 {suffix}"

            rain_str = f" | 🌧️ {rain_pct}% ({rain_mm}mm)" if rain_pct >= 20 else ""
            lines.append(f"   {t_label}  {icon} {temp}°C  {desc}{rain_str}")
            shown += 1

        if shown == 0:
            lines.append("   No more hourly data for today.")
        return "\n".join(lines)