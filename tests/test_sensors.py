"""Sensors on ESP32 nodes: the MQ-2's ppm, levels with hold and hysteresis,
the warm-up, automatic responses, alerts and acknowledging them, Home/Away,
history, sensor incidents, and the API."""

import json
from types import SimpleNamespace

import pytest

from homeshield.alarm import AlarmController
from homeshield.db import init_db, read_conn, write_conn
from homeshield.incidents import assign
from homeshield.iot.hub import IotHub
from homeshield.iot.kinds import PinError
from homeshield.iot.automations import AutomationEngine
from homeshield.iot.sensors import (CLEAN_AIR_RATIO, SensorEngine, gas_ppm, lpg_ppm,
                                    mv_from_state)
from homeshield.iot.store import IotStore

from test_iot import Clock, FakeLink, Settings, _login, adopted
from test_iot import app  # noqa: F401  (fixture)

NODE = "node-kitchen"


class FakeNotifier:
    def __init__(self, ready=True):
        self.sent, self._ready = [], ready

    def ready(self):
        return self._ready

    def phones(self):
        return ["+60123456789", "+60198765432"] if self._ready else []

    def send_text(self, body):
        if self._ready:
            self.sent.append(body)
        return self._ready


def mv_for(ppm, v0=0.4, divider=0.5):
    """The pin millivolts at which an MQ-2 with clean-air output v0 reads `ppm`."""
    lo, hi = v0, 4.99
    for _ in range(60):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if lpg_ppm(mid, v0) < ppm else (lo, mid)
    return round(lo * divider * 1000)


CLEAN = mv_for(0)            # 200 mV at the pin: 0.4 V from the module


@pytest.fixture
def rig(tmp_path):
    db = str(tmp_path / "hs.db")
    init_db(db)
    clock = Clock()
    settings = Settings(buzzer_enabled=True, buzzer_max_seconds=600,
                        buzzer_kinds="fire,sos,fall,intruder,zone")
    alarm = AlarmController(settings, clock=clock)
    h = IotHub(IotStore(db), alarm=alarm, clock=clock)
    link = FakeLink()
    h.attach(link)
    h.on_connected()
    events, resolved = [], []
    notifier = FakeNotifier()
    eng = SensorEngine(h, settings=settings, alarm=alarm,
                       publish_event=lambda *a: events.append(a), notifier=notifier,
                       resolve_incident=lambda iid, by: resolved.append((iid, by)), clock=clock)
    later = []
    AutomationEngine(h, eng, settings=settings, alarm=alarm, notifier=notifier, clock=clock,
                     later=lambda secs, fn: later.append((secs, fn)))
    adopted(h, NODE, "Kitchen")
    fan = h.map_pin(NODE, 26, "fan", "Exhaust fan", "", {}, "admin")
    valve = h.map_pin(NODE, 27, "valve", "Gas valve", "", {}, "admin")
    gas = h.map_pin(NODE, 34, "gas", "Gas sensor", "", {}, "admin")
    return SimpleNamespace(h=h, link=link, clock=clock, alarm=alarm, eng=eng, events=events,
                           resolved=resolved, notifier=notifier, fan=fan, valve=valve, gas=gas,
                           settings=settings)


def send(r, pin, payload, at=None):
    if at is not None:
        r.clock.t = at
    r.h.handle(f"homeshield/{NODE}/pin/{pin}/state", payload)


def gas(r, ppm=None, mv=None, at=None):
    send(r, 34, json.dumps({"mv": mv if mv is not None else mv_for(ppm)}), at)


def level(r, device):
    return r.eng.reading(r.h.store.device(device["device_id"]))["level"]


# ---- the MQ-2 ----------------------------------------------------------------------

def test_mq2_readings_become_lpg_ppm():
    assert mv_from_state('{"mv": 812}') == 812.0
    assert mv_from_state("4095") == pytest.approx(3300.0)       # firmware 1.0-1.1
    assert mv_from_state("") is None and mv_from_state('{"x": 1}') is None
    # In clean air Rs/Ro is 9.83: a few ppm. More gas, lower Rs, higher voltage.
    assert lpg_ppm(0.4, 0.4) == pytest.approx(574.25 * CLEAN_AIR_RATIO ** -2.222)
    readings = [lpg_ppm(v, 0.4) for v in (0.4, 1.0, 2.0, 3.0, 4.0)]
    assert readings == sorted(readings) and readings[-1] <= 10000
    assert 1500 < lpg_ppm(3.0, 0.4) < 2500


def test_an_mq5_reads_methane_with_its_own_curve_and_levels(rig):
    assert gas_ppm(0.4, 0.4, "mq5") == pytest.approx(177.65 * 6.5 ** -2.56)
    assert gas_ppm(2.5, 0.4, "mq5") != pytest.approx(gas_ppm(2.5, 0.4, "mq2"))
    with pytest.raises(PinError, match="mq2 or mq5"):
        rig.h.map_pin(NODE, 35, "gas", "Methane", "", {"model": "mq9"}, "admin")
    mq5 = rig.h.map_pin(NODE, 35, "gas", "Methane sensor", "", {"model": "mq5"}, "admin")
    view = rig.eng.sensor_view(rig.h.store.device(mq5["device_id"]))
    assert view["short"] == "MQ-5" and view["gas"] == "Methane"
    assert view["settings"]["warn"] == 1000 and view["settings"]["danger"] == 5000
    send(rig, 35, json.dumps({"mv": 200}))
    send(rig, 35, json.dumps({"mv": mv_for(3000)}), at=rig.clock.t + 2)
    reading = rig.eng.reading(rig.h.store.device(mq5["device_id"]))
    expected = gas_ppm(mv_for(3000) / 500, 0.4, "mq5")
    assert reading["value"] == pytest.approx(expected, rel=0.01)


def test_until_calibrated_the_cleanest_reading_is_clean_air(rig):
    gas(rig, mv=CLEAN + 40, at=rig.clock.t)
    gas(rig, mv=CLEAN, at=rig.clock.t + 2)
    reading = rig.eng.reading(rig.h.store.device(rig.gas["device_id"]))
    assert reading["baseline"] == "auto" and reading["value"] < 10
    gas(rig, mv=mv_for(500), at=rig.clock.t + 2)
    assert 450 < rig.eng.reading(rig.h.store.device(rig.gas["device_id"]))["value"] < 550


def test_calibrating_in_clean_air_sets_the_baseline(rig):
    with pytest.raises(PinError, match="Not enough readings"):
        rig.eng.calibrate(rig.gas["device_id"], "admin")
    for i in range(4):
        gas(rig, mv=300, at=rig.clock.t + 2)
    view = rig.eng.calibrate(rig.gas["device_id"], "admin")
    assert view["reading"]["baseline"] == "calibrated"
    assert view["settings"]["calibrated_by"] == "admin"
    gas(rig, mv=300, at=rig.clock.t + 2)
    assert rig.eng.reading(rig.h.store.device(rig.gas["device_id"]))["value"] < 10


# ---- levels and responses -----------------------------------------------------------

def test_gas_must_stay_high_for_the_hold_time_then_its_rules_run(rig):
    t = rig.clock.t
    gas(rig, mv=CLEAN, at=t)
    gas(rig, 2000, at=t + 1)                      # above danger (1000 ppm)
    gas(rig, 2000, at=t + 5)
    assert level(rig, rig.gas) == "normal"        # not 10 s yet
    rig.h.tick(t + 12)
    assert level(rig, rig.gas) == "danger"
    # Warning: notify + exhaust fan on. Danger: buzzer, incident, valve closed.
    assert rig.link.last(f"homeshield/{NODE}/pin/26/set") == "1"
    assert rig.link.last(f"homeshield/{NODE}/pin/27/set") == "0"
    assert rig.alarm.command().startswith("ALARM FIRE")
    assert [e[0] for e in rig.events] == ["gas_danger"]
    event_type, room, details, meta = rig.events[0]
    assert room == "Kitchen" and meta["device_id"] == rig.gas["device_id"]
    assert meta["source"] == f"{NODE} · MQ-2" and meta["reading"].endswith("ppm")
    assert len(rig.notifier.sent) == 1 and "Gas warning in Kitchen" in rig.notifier.sent[0]
    assert "Exhaust fan on" in rig.notifier.sent[0]
    [alert] = rig.eng.alerts()
    assert alert["level"] == "danger" and alert["title"] == "Gas danger · Kitchen"
    assert "alarm buzzer sounding" in alert["text"]
    activity = [a["text"] for a in rig.h.store.activity(20)]
    assert any("Gas sensor: gas danger" in t for t in activity)
    assert any(a["by"].startswith("Automation") for a in rig.h.store.activity(20))


def test_a_level_is_left_only_after_a_clear_drop(rig):
    t = rig.clock.t
    gas(rig, mv=CLEAN, at=t)
    gas(rig, 1200, at=t + 1)
    rig.h.tick(t + 12)
    assert level(rig, rig.gas) == "danger"
    gas(rig, 950, at=t + 13)                      # below 1000 but above 90 % of it
    rig.h.tick(t + 30)
    assert level(rig, rig.gas) == "danger"
    gas(rig, 400, at=t + 31)                      # warning territory
    rig.h.tick(t + 42)
    assert level(rig, rig.gas) == "warning"
    assert rig.alarm.command().startswith("ALARM")   # still the same episode
    gas(rig, mv=CLEAN, at=t + 43)
    rig.h.tick(t + 54)
    assert level(rig, rig.gas) == "normal"
    assert rig.alarm.command().startswith("STOP")
    assert rig.eng.alerts() == []
    # A new episode runs the rules again.
    gas(rig, 2000, at=t + 60)
    rig.h.tick(t + 71)
    assert [e[0] for e in rig.events] == ["gas_danger", "gas_danger"]


def test_acknowledging_stops_the_buzzer_and_closes_the_incident(rig):
    t = rig.clock.t
    gas(rig, mv=CLEAN, at=t)
    gas(rig, 2000, at=t + 1)
    rig.h.tick(t + 12)
    rig.eng.on_incident("opened", {"incident_id": 7, "device_id": rig.gas["device_id"]})
    rig.eng.acknowledge(rig.gas["device_id"], "mum")
    assert rig.eng.alerts() == []
    assert rig.alarm.command().startswith("STOP")
    assert rig.resolved == [(7, "mum")]
    # Worse than before brings it back: here a fresh episode after clearing.
    gas(rig, mv=CLEAN, at=t + 20)
    rig.h.tick(t + 31)
    gas(rig, 2000, at=t + 32)
    rig.h.tick(t + 43)
    assert len(rig.eng.alerts()) == 1


def test_closing_the_incident_elsewhere_acknowledges_the_alert(rig):
    t = rig.clock.t
    gas(rig, mv=CLEAN, at=t)
    gas(rig, 2000, at=t + 1)
    rig.h.tick(t + 12)
    rig.eng.on_incident("opened", {"incident_id": 9, "device_id": rig.gas["device_id"]})
    rig.eng.on_incident("resolved", {"incident_id": 9, "device_id": rig.gas["device_id"],
                                     "resolved_by": "dad"})
    assert rig.eng.alerts() == [] and rig.alarm.command().startswith("STOP")


def test_a_cold_mq2_is_ignored_while_it_warms_up(rig):
    t = rig.clock.t
    rig.h.handle(f"homeshield/{NODE}/info", json.dumps({"fw": "1.2", "up": 10}))
    gas(rig, mv=CLEAN, at=t)
    gas(rig, 3000, at=t + 1)
    rig.h.tick(t + 30)
    assert level(rig, rig.gas) == "normal"
    assert rig.eng.reading(rig.h.store.device(rig.gas["device_id"]))["warming"]
    with pytest.raises(PinError, match="warming up"):
        rig.eng.calibrate(rig.gas["device_id"], "admin")
    gas(rig, 3000, at=t + 85)                     # 95 s since it started
    rig.h.tick(t + 96)
    assert level(rig, rig.gas) == "danger"


def test_motion_raises_the_alarm_only_while_away(rig):
    motion = rig.h.map_pin(NODE, 32, "motion", "Porch motion", "Entrance", {}, "admin")
    send(rig, 32, "1")
    assert level(rig, motion) == "active"
    assert rig.events == [] and rig.eng.alerts() == []
    send(rig, 32, "0", at=rig.clock.t + 1)
    rig.h.tick(rig.clock.t + 10)
    assert level(rig, motion) == "normal"

    assert rig.eng.set_mode("away", "nadia")["by"] == "nadia"
    send(rig, 32, "1", at=rig.clock.t + 1)
    assert [e[0] for e in rig.events] == ["motion_detected"]
    assert "while everyone is away" in rig.events[0][2]
    assert rig.alarm.command().startswith("ALARM INTRUDER")
    assert rig.eng.alerts()[0]["title"] == "Motion detected · Entrance"
    with pytest.raises(PinError):
        rig.eng.set_mode("holiday", "nadia")


def test_flame_must_last_its_hold_and_clear_for_a_while(rig):
    flame = rig.h.map_pin(NODE, 33, "flame", "Flame sensor", "", {}, "admin")
    t = rig.clock.t
    send(rig, 33, "1", at=t)
    rig.h.tick(t + 1)
    assert level(rig, flame) == "normal"
    rig.h.tick(t + 2.5)
    assert level(rig, flame) == "active"
    assert [e[0] for e in rig.events] == ["flame_detected"]
    send(rig, 33, "0", at=t + 3)
    rig.h.tick(t + 5)
    assert level(rig, flame) == "active"           # a flicker doesn't end it
    rig.h.tick(t + 9)
    assert level(rig, flame) == "normal"


def test_the_checklist_edits_the_sensors_rules(rig):
    view = rig.eng.sensor_view(rig.h.store.device(rig.gas["device_id"]))
    warning = {i["key"]: i for i in view["response"]["warning"]["items"]}
    fan_key = f"device:{rig.fan['device_id']}:1"
    assert warning[fan_key]["checked"] and warning["notify"]["checked"]
    assert warning[fan_key]["label"] == "Turn on Exhaust fan"
    assert "2 numbers" in warning["notify"]["label"]
    danger = {i["key"]: i for i in view["response"]["danger"]["items"]}
    assert danger["buzzer"]["checked"] and danger["incident"]["checked"]
    assert danger[f"device:{rig.valve['device_id']}:0"]["label"] == "Close Gas valve"

    rig.eng.set_response(rig.gas["device_id"], "warning", ["notify"], "admin")
    view = rig.eng.sensor_view(rig.h.store.device(rig.gas["device_id"]))
    checked = [i["key"] for i in view["response"]["warning"]["items"] if i["checked"]]
    assert checked == ["notify"]
    with pytest.raises(PinError):
        rig.eng.set_response(rig.gas["device_id"], "warning", ["device:999:1"], "admin")
    with pytest.raises(PinError):
        rig.eng.set_response(rig.gas["device_id"], "warning",
                             [f"device:{rig.gas['device_id']}:1"], "admin")
    # Removing the fan takes it out of the rule.
    rig.eng.set_response(rig.gas["device_id"], "warning", [fan_key], "admin")
    rig.h.unmap_device(rig.fan["device_id"], "admin")
    rule = next(r for r in rig.h.store.rules() if r["trigger"].get("level") == "warning")
    assert rule["actions"] == []


def test_levels_are_validated(rig):
    did = rig.gas["device_id"]
    with pytest.raises(PinError, match="below the danger"):
        rig.eng.update_settings(did, {"warn": 1200}, "admin")
    with pytest.raises(PinError, match="between"):
        rig.eng.update_settings(did, {"danger": 50000}, "admin")
    with pytest.raises(PinError, match="wired"):
        rig.eng.update_settings(did, {"divider": 0.3}, "admin")
    view = rig.eng.update_settings(did, {"warn": 200, "danger": 800, "hold": 5, "divider": 0.667,
                                         "every": 5}, "admin")
    assert view["settings"]["warn"] == 200 and view["settings"]["hold"] == 5
    assert view["settings"]["divider"] == 0.667 and view["settings"]["every"] == 5
    cfg = json.loads(rig.link.last(f"homeshield/{NODE}/config"))
    assert next(p for p in cfg["pins"] if p["pin"] == 34)["every"] == 5


def test_whatsapp_not_set_up_still_shows_the_alert(rig):
    rig.eng._notifier = rig.h.automations._notifier = FakeNotifier(ready=False)
    t = rig.clock.t
    gas(rig, mv=CLEAN, at=t)
    gas(rig, 500, at=t + 1)
    rig.h.tick(t + 12)
    [alert] = rig.eng.alerts()
    assert alert["level"] == "warning" and "WhatsApp isn't set up" in alert["text"]


# ---- history -------------------------------------------------------------------------

def test_readings_are_kept_per_minute(rig):
    t = (rig.clock.t // 60) * 60
    gas(rig, mv=CLEAN, at=t + 1)
    gas(rig, mv=CLEAN, at=t + 30)
    gas(rig, 500, at=t + 61)
    rig.h.tick(t + 125)
    hist = rig.eng.history(rig.gas["device_id"], "1h")
    assert [p["ts"] for p in hist["points"]] == [t, t + 60]
    assert hist["points"][1]["max"] == pytest.approx(500, rel=0.05)
    assert hist["warn"] == 300 and hist["danger"] == 1000
    gas(rig, 600, at=t + 130)                     # the minute still being collected
    assert len(rig.eng.history(rig.gas["device_id"], "1h")["points"]) == 3
    with pytest.raises(PinError):
        rig.eng.history(rig.fan["device_id"], "1h")


def test_digital_sensors_count_how_often_they_fire(rig):
    motion = rig.h.map_pin(NODE, 32, "motion", "Porch motion", "Entrance", {}, "admin")
    t = (rig.clock.t // 3600) * 3600
    for i, at in enumerate((t + 10, t + 70, t + 400)):
        send(rig, 32, "1", at=at)
        send(rig, 32, "0", at=at + 5)
    rig.h.tick(t + 500)
    hist = rig.eng.history(motion["device_id"], "24h")
    assert hist["series"] == "on" and hist["bucket"] == 3600
    assert sum(p["n"] for p in hist["points"]) == 3
    view = rig.eng.sensor_view(rig.h.store.device(motion["device_id"]))
    assert view["today"] == 3


def test_the_auto_baseline_is_learned_from_clean_minutes(rig):
    t = (rig.clock.t // 60) * 60
    for i in range(3):
        gas(rig, mv=260, at=t + 1 + i * 2)
    rig.h.tick(t + 61)
    saved = rig.h.store.sensor_settings(rig.gas["device_id"])
    assert saved["auto_v0"] == pytest.approx(0.52)


def test_a_leak_is_never_learned_as_clean_air(rig):
    t = (rig.clock.t // 60) * 60
    gas(rig, mv=CLEAN, at=t + 1)                  # a leak starting within the minute
    gas(rig, 2000, at=t + 20)
    gas(rig, 2000, at=t + 40)
    rig.h.tick(t + 61)
    for i in range(3):                            # a whole minute at danger
        gas(rig, 2000, at=t + 62 + i * 10)
    rig.h.tick(t + 125)
    assert "auto_v0" not in (rig.h.store.sensor_settings(rig.gas["device_id"]) or {})
    assert level(rig, rig.gas) == "danger"


# ---- incidents and the buzzer ---------------------------------------------------------

def test_sensor_incidents_keep_rooms_apart_and_carry_the_reading(tmp_path):
    db = str(tmp_path / "i.db")
    init_db(db)

    def ev(event_type, name, device_id=None, reading=None, ts=1000.0):
        return SimpleNamespace(event_type=event_type, ts=ts, camera_id=None, camera_name=name,
                               confidence=1.0, person_category="unknown", snapshot_path=None,
                               event_id=None, meta={"device_id": device_id, "reading": reading,
                                                    "source": "node-kitchen · MQ-2"})

    with write_conn(db) as conn:
        a = assign(conn, ev("sos_pressed", "bedroom"))[1]
        b = assign(conn, ev("sos_pressed", "kitchen", ts=1001))[1]
        g1 = assign(conn, ev("gas_warning", "Kitchen", 5, "400 ppm", 1002))[1]
        change, g2 = assign(conn, ev("gas_danger", "Kitchen", 5, "1,400 ppm", 1010))
    assert a["incident_id"] != b["incident_id"]
    assert g2["incident_id"] == g1["incident_id"] and change == "escalated"
    assert g2["reading"] == "1,400 ppm" and g2["source"] == "node-kitchen · MQ-2"
    with read_conn(db) as conn:
        assert conn.execute("SELECT device_id FROM incidents WHERE incident_id = ?",
                            (g1["incident_id"],)).fetchone()[0] == 5


def test_sensor_incidents_leave_the_buzzer_to_the_rule(tmp_path):
    clock = Clock()
    alarm = AlarmController(Settings(buzzer_enabled=True, buzzer_max_seconds=60,
                                     buzzer_kinds="fire,gas"), clock=clock)
    alarm.on_incident("opened", {"incident_id": 1, "kind": "gas", "headline_type": "gas_danger"})
    assert alarm.command().startswith("STOP")
    assert alarm.ring(("sensor", 5), "gas")
    assert alarm.command().startswith("ALARM FIRE")
    alarm.reload()                                # saving Settings keeps a sensor's ring
    assert alarm.command().startswith("ALARM FIRE")
    assert alarm.unring(("sensor", 5)) and alarm.command().startswith("STOP")


# ---- the API --------------------------------------------------------------------------

def test_everyone_sees_sensors_and_only_admins_change_them(app):  # noqa: F811
    h = app.extensions["homeshield_iot"]
    admin = _login(app, "admin", "admin")
    adopted(h)
    gas_dev = admin.post("/api/devices", json={"node_id": NODE, "pin": 34, "kind": "gas",
                                               "name": "Gas sensor"}).get_json()
    did = gas_dev["device_id"]
    h.handle(f"homeshield/{NODE}/pin/34/state", json.dumps({"mv": 200}))
    admin.post("/api/users", json={"username": "nadia", "password": "nadia1", "role": "guest",
                                   "must_change_password": False})
    guest = _login(app, "nadia", "nadia1")
    data = guest.get("/api/sensors").get_json()
    [s] = data["sensors"]
    assert s["kind"] == "gas" and s["reading"]["unit"] == "ppm" and data["mode"]["mode"] == "home"
    assert guest.get(f"/api/sensors/{did}/history?range=24h").get_json()["bucket"] == 900
    assert guest.get(f"/api/sensors/{did}/history?range=1y").status_code == 400
    assert guest.patch(f"/api/sensors/{did}", json={"warn": 100}).status_code == 403
    assert guest.post(f"/api/sensors/{did}/acknowledge").status_code == 200
    assert guest.post("/api/mode", json={"mode": "away"}).get_json()["mode"] == "away"
    assert guest.get("/api/status").get_json()["mode"]["mode"] == "away"
    r = admin.patch(f"/api/sensors/{did}", json={"warn": 5000})
    assert r.status_code == 400 and "below the danger" in r.get_json()["error"]
    r = admin.put(f"/api/sensors/{did}/response", json={"level": "danger", "steps": ["buzzer"]})
    assert r.status_code == 200
    devices = guest.get("/api/devices").get_json()["devices"]
    assert devices[0]["reading"]["text"].endswith("ppm")
