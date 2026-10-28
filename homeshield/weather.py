"""Outdoor weather for the Live page, from Open-Meteo (free, no account or key).

Only the home's coordinates leave the house: no camera data, names or events. An
answer is kept for 15 minutes, and the last good one is kept when the internet
drops, so the dashboard always has something to show. Sunrise and sunset are
worked out here (iot/sun.py), so that card works with no internet at all.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

from .iot.sun import sun_times

log = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
AIR_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
PLACES_URL = "https://geocoding-api.open-meteo.com/v1/search"
REFRESH_S = 15 * 60          # how long an answer is good for
RETRY_S = 2 * 60             # how soon to try again after a failure
HOURS = 6                    # hourly forecast shown on the card


def get_json(url: str, params: dict[str, Any], timeout: float = 8.0) -> Any:
    req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params),
                                 headers={"User-Agent": "HomeShield"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _num(v: Any) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


class Weather:
    """`location` returns {"city", "lat", "lon"}: the home location automations use."""

    def __init__(self, location: Callable[[], dict[str, Any]], *,
                 fetch: Callable[..., Any] = get_json, clock: Callable[[], float] = time.time):
        self._location = location
        self.fetch = fetch
        self._clock = clock
        self._lock = threading.Lock()
        self._data: Optional[dict[str, Any]] = None     # the last good answer
        self._data_key: Optional[tuple] = None          # ... and where it was for
        self._tried_at = 0.0
        self._tried_key: Optional[tuple] = None
        self._error: Optional[str] = None
        self._busy = False

    # ---- what the page asks for -------------------------------------------------------

    def get(self, wait: float = 0.0) -> dict[str, Any]:
        """The weather now. Starts a refresh in the background when the answer is old
        (or for another place); `wait` lets a caller block for it (tests)."""
        loc = self._location()
        key = (round(float(loc["lat"]), 4), round(float(loc["lon"]), 4))
        now = self._clock()
        with self._lock:
            fresh = (self._data is not None and self._data_key == key
                     and now - self._data["fetched_at"] < REFRESH_S)
            due = (not fresh and not self._busy
                   and (self._tried_key != key or now - self._tried_at >= RETRY_S))
            if due:
                self._busy = True
                self._tried_at, self._tried_key = now, key
        if due:
            t = threading.Thread(target=self._refresh, args=(loc, key), name="hs-weather", daemon=True)
            t.start()
            if wait:
                t.join(wait)
        with self._lock:
            data = self._data if self._data_key == key else None
            out: dict[str, Any] = {"location": loc["city"], "sun": self._sun(loc),
                                   "loading": self._busy and data is None,
                                   "error": self._error if self._tried_key == key else None}
            if data is not None:
                out.update(data)
                out["stale"] = now - data["fetched_at"] > REFRESH_S * 3
            return out

    def places(self, query: str) -> list[dict[str, Any]]:
        """Towns matching `query`, for picking the home location in Settings."""
        query = (query or "").strip()
        if len(query) < 2:
            return []
        res = self.fetch(PLACES_URL, {"name": query[:60], "count": 6, "language": "en", "format": "json"})
        out = []
        for p in (res or {}).get("results") or []:
            lat, lon = _num(p.get("latitude")), _num(p.get("longitude"))
            if lat is None or lon is None:
                continue
            out.append({"name": str(p.get("name") or ""),
                        "area": ", ".join(x for x in (p.get("admin1"), p.get("country")) if x),
                        "lat": round(lat, 4), "lon": round(lon, 4)})
        return out

    # ---- fetching ----------------------------------------------------------------------

    def _refresh(self, loc: dict[str, Any], key: tuple) -> None:
        try:
            data = self._fetch(loc)
            with self._lock:
                self._data, self._data_key, self._error = data, key, None
        except Exception as e:                                 # offline, slow, or a bad answer
            log.info("weather unavailable: %s", e)
            with self._lock:
                self._error = "HomeShield couldn't reach the weather service."
        finally:
            with self._lock:
                self._busy = False

    def _fetch(self, loc: dict[str, Any]) -> dict[str, Any]:
        where = {"latitude": loc["lat"], "longitude": loc["lon"], "timezone": "auto", "timeformat": "unixtime"}
        f = self.fetch(FORECAST_URL, {
            **where, "current": "temperature_2m,weather_code,is_day,visibility",
            "hourly": "temperature_2m,weather_code,is_day",
            "daily": "temperature_2m_max,temperature_2m_min", "forecast_days": 2})
        cur, hourly, daily = f["current"], f.get("hourly") or {}, f.get("daily") or {}
        now_ts = int(cur["time"])
        hours = []
        for i, ts in enumerate(hourly.get("time") or []):
            if ts > now_ts and len(hours) < HOURS:
                hours.append({"ts": int(ts), "temp": _num(hourly["temperature_2m"][i]),
                              "code": int(hourly["weather_code"][i] or 0),
                              "is_day": bool(hourly.get("is_day", [1] * (i + 1))[i])})
        out = {"now": {"temp": _num(cur.get("temperature_2m")), "code": int(cur.get("weather_code") or 0),
                       "is_day": bool(cur.get("is_day", 1)),
                       "high": _num((daily.get("temperature_2m_max") or [None])[0]),
                       "low": _num((daily.get("temperature_2m_min") or [None])[0])},
               "hours": hours, "visibility_m": _num(cur.get("visibility")),
               "aqi": None, "pm2_5": None, "fetched_at": self._clock()}
        try:                                                   # air quality is a separate service
            a = self.fetch(AIR_URL, {**where, "current": "us_aqi,pm2_5"})["current"]
            out["aqi"], out["pm2_5"] = _num(a.get("us_aqi")), _num(a.get("pm2_5"))
        except Exception as e:
            log.info("air quality unavailable: %s", e)
        return out

    def _sun(self, loc: dict[str, Any]) -> dict[str, Any]:
        """Today's sunrise and sunset here, and which comes next."""
        now = self._clock()
        today = dt.date.fromtimestamp(now)
        rise, set_ = sun_times(today, float(loc["lat"]), float(loc["lon"]))
        if rise is None or set_ is None:
            return {"sunrise": rise, "sunset": set_, "next": None, "next_ts": None}
        if now < rise:
            nxt = ("sunrise", rise)
        elif now < set_:
            nxt = ("sunset", set_)
        else:
            nxt = ("sunrise", sun_times(today + dt.timedelta(days=1), float(loc["lat"]), float(loc["lon"]))[0])
        return {"sunrise": rise, "sunset": set_, "next": nxt[0], "next_ts": nxt[1]}
