"""The Live page's weather (weather.py): read from Open-Meteo, kept for 15 minutes,
the last good answer kept when the internet drops, and sunrise / sunset with no
internet at all. A fake Open-Meteo stands in; nothing here goes online."""
import pytest

from homeshield.db import write_conn
from homeshield.server import create_app
from homeshield.weather import AIR_URL, FORECAST_URL, PLACES_URL, REFRESH_S, RETRY_S, Weather

NOW = 1_800_000_000          # a weekday afternoon in Kuala Lumpur
KL = {"city": "Kuala Lumpur", "lat": 3.139, "lon": 101.6869}


class Clock:
    def __init__(self, t=float(NOW)):
        self.t = t

    def __call__(self):
        return self.t


class FakeOpenMeteo:
    def __init__(self):
        self.calls, self.down = [], False

    def __call__(self, url, params, timeout=8.0):
        self.calls.append((url, dict(params)))
        if self.down:
            raise OSError("no route to host")
        if url == FORECAST_URL:
            start = NOW - NOW % 3600 - 10 * 3600
            hours = [start + i * 3600 for i in range(48)]
            return {"current": {"time": NOW, "temperature_2m": 29.4, "weather_code": 2, "is_day": 1, "visibility": 24140},
                    "hourly": {"time": hours, "temperature_2m": [25 + i % 7 for i in range(48)],
                               "weather_code": [3] * 48, "is_day": [1] * 48},
                    "daily": {"time": [start, start + 86400], "temperature_2m_max": [32.1, 31.0],
                              "temperature_2m_min": [24.2, 24.0]}}
        if url == AIR_URL:
            return {"current": {"time": NOW, "us_aqi": 45, "pm2_5": 11.2}}
        if url == PLACES_URL:
            return {"results": [{"name": "Ipoh", "latitude": 4.5975, "longitude": 101.0901,
                                 "admin1": "Perak", "country": "Malaysia"},
                                {"name": "Nowhere"}]}
        raise AssertionError(url)

    def forecasts(self):
        return [p for u, p in self.calls if u == FORECAST_URL]


def _weather(location=KL):
    fake, clock = FakeOpenMeteo(), Clock()
    w = Weather(lambda: location if not callable(location) else location(), fetch=fake, clock=clock)
    return w, fake, clock


def test_the_weather_is_read_and_kept_for_fifteen_minutes():
    w, fake, clock = _weather()
    out = w.get(wait=5)
    assert out["location"] == "Kuala Lumpur" and out["error"] is None
    assert out["now"] == {"temp": 29.4, "code": 2, "is_day": True, "high": 32.1, "low": 24.2}
    assert len(out["hours"]) == 6 and all(h["ts"] > NOW for h in out["hours"])
    assert out["visibility_m"] == 24140 and out["aqi"] == 45 and out["pm2_5"] == 11.2
    assert fake.forecasts()[0]["latitude"] == 3.139          # only the coordinates go out
    clock.t += REFRESH_S - 1
    w.get(wait=5)
    assert len(fake.forecasts()) == 1                        # still fresh: not asked again
    clock.t += 2
    w.get(wait=5)
    assert len(fake.forecasts()) == 2


def test_offline_keeps_the_last_answer_and_tries_again_later():
    w, fake, clock = _weather()
    w.get(wait=5)
    fake.down = True
    clock.t += REFRESH_S + 1
    out = w.get(wait=5)
    assert out["now"]["temp"] == 29.4 and out["error"] and not out["stale"]
    asked = len(fake.calls)
    clock.t += RETRY_S - 1
    w.get(wait=5)
    assert len(fake.calls) == asked                          # not hammering a dead connection
    clock.t += REFRESH_S * 3
    assert w.get(wait=5)["stale"]


def test_sunrise_and_sunset_need_no_internet():
    w, fake, _ = _weather()
    fake.down = True
    out = w.get(wait=5)
    assert "now" not in out and out["error"]
    sun = out["sun"]
    assert sun["sunrise"] < sun["sunset"] and sun["next"] in ("sunrise", "sunset") and sun["next_ts"] >= NOW


def test_a_new_home_location_is_asked_for_at_once():
    where = dict(KL)
    w, fake, _ = _weather(lambda: where)
    w.get(wait=5)
    where.update(city="Ipoh", lat=4.5975, lon=101.0901)
    out = w.get(wait=5)
    assert out["location"] == "Ipoh" and fake.forecasts()[-1]["latitude"] == 4.5975


def test_places_for_the_home_location():
    w, fake, _ = _weather()
    assert w.places("I") == [] and not fake.calls
    assert w.places("Ipoh") == [{"name": "Ipoh", "area": "Perak, Malaysia", "lat": 4.5975, "lon": 101.0901}]


# ---- the routes ---------------------------------------------------------------------

@pytest.fixture
def app(tmp_path):
    path = str(tmp_path / "hs.db")
    app = create_app(db_path=path, snapshot_dir=str(tmp_path / "s"), person_photos_dir=str(tmp_path / "p"),
                     intruder_photos_dir=str(tmp_path / "i"), auto_start=False)
    with write_conn(path) as conn:
        conn.execute("UPDATE users SET must_change = 0")
    app.extensions["homeshield_weather"].fetch = FakeOpenMeteo()
    return app


def _login(app, user, pw):
    c = app.test_client()
    assert c.post("/api/login", json={"username": user, "password": pw}).status_code == 200
    return c


def test_everyone_sees_the_weather_and_admins_choose_where(app):
    admin = _login(app, "admin", "admin")
    admin.post("/api/users", json={"username": "nadia", "password": "nadia1", "role": "guest",
                                   "must_change_password": False})
    guest = _login(app, "nadia", "nadia1")
    assert app.test_client().get("/api/weather").status_code == 401
    assert guest.get("/api/weather").get_json()["location"] == "Kuala Lumpur"
    assert guest.get("/api/weather/places?q=Ipoh").status_code == 403
    assert admin.get("/api/weather/places?q=Ipoh").get_json()[0]["area"] == "Perak, Malaysia"
    r = admin.put("/api/automations/location", json={"city": "Ipoh", "lat": 4.5975, "lon": 101.0901})
    assert r.status_code == 200 and r.get_json()["city"] == "Ipoh"
    assert guest.get("/api/weather").get_json()["location"] == "Ipoh"
    assert admin.put("/api/automations/location", json={"city": "Mars", "lat": 400, "lon": 0}).status_code == 400
    assert guest.put("/api/automations/location", json={"city": "Ipoh", "lat": 4.6, "lon": 101.1}).status_code == 403


def test_the_place_search_says_when_it_is_offline(app):
    app.extensions["homeshield_weather"].fetch.down = True
    admin = _login(app, "admin", "admin")
    r = admin.get("/api/weather/places?q=Ipoh")
    assert r.status_code == 502 and "internet" in r.get_json()["error"]
