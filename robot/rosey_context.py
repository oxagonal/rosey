#!/usr/bin/env python3
"""Live facts for Rosey's conversation: where she is (city, from the Jetson's IP
address) and the weather there (Open-Meteo, free, no account).

    ctx = LiveContext(); ctx.start()       # refreshes in a background thread
    ctx.note()  -> "Location: Boston, Massachusetts, US (approximate, from the internet connection).
                    Weather now: 41°F (feels like 36°F), light rain, wind 12 mph ..."

Replies never wait on the network: note() returns whatever was last fetched.

Settings (agent.env):
    ROSEY_LOCATION      "auto" (default: from IP) or a fixed place, e.g. "Boston, MA"
    ROSEY_WEATHER       "0" to turn weather off
    ROSEY_UNITS         "us" (default, °F / mph) or "metric"

CLI test:  python3 rosey_context.py
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
import urllib.request

LOCATION_EVERY_S = 6 * 3600
WEATHER_EVERY_S = 15 * 60
RETRY_S = 120
UA = {"User-Agent": "rosey-robot/1.0"}

WMO = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "light freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "light freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "light showers", 81: "showers",
    82: "heavy showers", 85: "light snow showers", 86: "snow showers", 95: "thunderstorm",
    96: "thunderstorm with hail", 99: "thunderstorm with heavy hail",
}


def _get(url: str, timeout: float = 8.0) -> dict:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def locate_by_ip() -> dict:
    """Approximate city from the public IP address. Tries two services."""
    try:
        d = _get("https://ipinfo.io/json")
        lat, lon = (float(v) for v in d["loc"].split(","))
        return {"city": d.get("city"), "region": d.get("region"), "country": d.get("country"),
                "lat": lat, "lon": lon, "source": "ip"}
    except Exception:
        d = _get("https://ipapi.co/json/")
        return {"city": d.get("city"), "region": d.get("region"), "country": d.get("country_code"),
                "lat": float(d["latitude"]), "lon": float(d["longitude"]), "source": "ip"}


def locate_by_name(name: str) -> dict:
    city = name.split(",")[0].strip()
    q = urllib.parse.urlencode({"name": city, "count": 10, "language": "en", "format": "json"})
    results = _get("https://geocoding-api.open-meteo.com/v1/search?" + q).get("results") or []
    if not results:
        raise ValueError("place not found: %s" % name)
    rest = name.lower().split(",")[1:]
    hint = rest[0].strip() if rest else ""
    best = next((r for r in results if hint and (hint in (r.get("admin1") or "").lower()
                                                   or hint == (r.get("country_code") or "").lower()
                                                   or _state_abbrev(r.get("admin1")) == hint)), results[0])
    return {"city": best.get("name"), "region": best.get("admin1"), "country": best.get("country_code"),
            "lat": best["latitude"], "lon": best["longitude"], "source": "settings"}


_STATES = {"massachusetts": "ma", "new york": "ny", "california": "ca", "texas": "tx", "florida": "fl",
           "new hampshire": "nh", "rhode island": "ri", "connecticut": "ct", "maine": "me", "vermont": "vt",
           "new jersey": "nj", "pennsylvania": "pa", "illinois": "il", "washington": "wa"}


def _state_abbrev(region):
    return _STATES.get((region or "").lower())


def weather(lat: float, lon: float, units: str = "us") -> dict:
    q = {
        "latitude": "%.3f" % lat, "longitude": "%.3f" % lon, "timezone": "auto", "forecast_days": 2,
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,precipitation",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset",
    }
    if units == "us":
        q.update(temperature_unit="fahrenheit", wind_speed_unit="mph", precipitation_unit="inch")
    return _get("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(q))


def _clock(iso: str) -> str:
    t = time.strptime(iso, "%Y-%m-%dT%H:%M")
    return time.strftime("%-I:%M %p", t)


def describe(w: dict, units: str = "us") -> str:
    deg = "°F" if units == "us" else "°C"
    spd = "mph" if units == "us" else "km/h"
    c, d = w["current"], w["daily"]
    parts = ["Weather now: %.0f%s (feels like %.0f%s), %s, wind %.0f %s, humidity %d%%." % (
        c["temperature_2m"], deg, c["apparent_temperature"], deg, WMO.get(c["weather_code"], "unknown conditions"),
        c["wind_speed_10m"], spd, c["relative_humidity_2m"])]
    for i, label in ((0, "Today"), (1, "Tomorrow")):
        if i < len(d.get("time", [])):
            rain = d["precipitation_probability_max"][i]
            parts.append("%s: %s, high %.0f%s, low %.0f%s%s." % (
                label, WMO.get(d["weather_code"][i], "mixed"), d["temperature_2m_max"][i], deg,
                d["temperature_2m_min"][i], deg, ", %d%% chance of rain or snow" % rain if rain is not None else ""))
    try:
        parts.append("Sunrise %s, sunset %s." % (_clock(d["sunrise"][0]), _clock(d["sunset"][0])))
    except Exception:
        pass
    return " ".join(parts)


class LiveContext:
    def __init__(self):
        self.setting = os.environ.get("ROSEY_LOCATION", "auto").strip() or "auto"
        self.units = os.environ.get("ROSEY_UNITS", "us").strip().lower()
        self.weather_on = os.environ.get("ROSEY_WEATHER", "1") != "0"
        self.place = None
        self.place_at = 0.0
        self.weather_text = None
        self.weather_at = 0.0
        self._lock = threading.Lock()

    def refresh(self) -> None:
        now = time.time()
        if self.place is None or now - self.place_at > LOCATION_EVERY_S:
            try:
                place = locate_by_ip() if self.setting.lower() == "auto" else locate_by_name(self.setting)
                with self._lock:
                    self.place, self.place_at = place, now
            except Exception as exc:
                print("rosey_context: location lookup failed: %s" % exc, flush=True)
        if self.weather_on and self.place and (self.weather_text is None or now - self.weather_at > WEATHER_EVERY_S):
            try:
                text = describe(weather(self.place["lat"], self.place["lon"], self.units), self.units)
                with self._lock:
                    self.weather_text, self.weather_at = text, now
            except Exception as exc:
                print("rosey_context: weather lookup failed: %s" % exc, flush=True)

    def start(self) -> "LiveContext":
        def loop():
            while True:
                self.refresh()
                ok = self.place is not None and (self.weather_text is not None or not self.weather_on)
                time.sleep(60 if ok else RETRY_S)
        threading.Thread(target=loop, daemon=True, name="rosey-context").start()
        return self

    def note(self) -> str:
        with self._lock:
            place, wtext, wat = self.place, self.weather_text, self.weather_at
        lines = []
        if place:
            name = ", ".join(p for p in (place.get("city"), place.get("region"), place.get("country")) if p)
            how = "approximate, from the internet connection" if place.get("source") == "ip" else "set by the caregiver"
            lines.append("Location: %s (%s)." % (name, how))
        if wtext:
            age = int((time.time() - wat) / 60)
            lines.append(wtext + (" (updated %d min ago)" % age if age >= 5 else ""))
        elif self.weather_on:
            lines.append("Weather: not available right now.")
        return "\n".join(lines)


if __name__ == "__main__":
    ctx = LiveContext()
    ctx.refresh()
    print(ctx.note())
