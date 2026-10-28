"""Automations: triggers of every kind, conditions, ordered steps with waits,
groups and scenes, cooldowns and the loop guard, confirming and retrying,
test runs, starters, sun times, and the API."""

import datetime as dt
import json
import time
from types import SimpleNamespace

import pytest

from homeshield.alarm import AlarmController
from homeshield.db import init_db
from homeshield.iot.automations import AutomationEngine
from homeshield.iot.hub import IotHub
from homeshield.iot.kinds import PinError
from homeshield.iot.sensors import SensorEngine
from homeshield.iot.store import IotStore
from homeshield.iot.sun import CITIES, sun_times

from test_iot import Clock, FakeLink, Settings, _login, adopted
from test_iot import app  # noqa: F401  (fixture)

NODE = "node-kitchen"


def local(h, m=0, day=(2026, 10, 14)):
    """A Unix time at h:m local time (14 Oct 2026 is a Wednesday)."""
    return time.mktime((*day, h, m, 0, 0, 0, -1))


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def ready(self):
        return True

    def phones(self):
        return ["+60123456789"]

    def send_text(self, body):
        self.sent.append(body)
        return True


@pytest.fixture
def rig(tmp_path):
    db = str(tmp_path / "hs.db")
    init_db(db)
    clock = Clock(local(12))
    settings = Settings(buzzer_enabled=True, buzzer_max_seconds=600,
                        buzzer_kinds="fire,sos,fall,intruder,zone")
    alarm = AlarmController(settings, clock=clock)
    h = IotHub(IotStore(db), alarm=alarm, clock=clock)
    link = FakeLink()
    h.attach(link)
    h.on_connected()
    sensors = SensorEngine(h, settings=settings, alarm=alarm, clock=clock)
    cams = {"1": {"name": "Living Room cam", "location": "Living room", "people": 0, "active": True}}
    later, broadcasts, monitor = [], [], {"on": True}
    notifier = FakeNotifier()
    eng = AutomationEngine(h, sensors, settings=settings, alarm=alarm, notifier=notifier,
                           broadcast=lambda name, data: broadcasts.append((name, data)),
                           cameras=lambda: cams, monitoring=lambda: monitor["on"],
                           set_monitoring=lambda on: monitor.update(on=on), clock=clock,
                           later=lambda secs, fn: later.append((secs, fn)))
    adopted(h, NODE, "Kitchen")
    dev = {}
    for pin, kind, name, room in ((26, "light", "Kitchen light", "Kitchen"),
                                  (27, "light", "Lounge light", "Living room"),
                                  (18, "dimmer", "Floor lamp", "Living room"),
                                  (19, "socket", "TV socket", "Living room"),
                                  (23, "lock", "Front door", "Entrance")):
        dev[name] = h.map_pin(NODE, pin, kind, name, room, {}, "admin")
    return SimpleNamespace(h=h, link=link, clock=clock, alarm=alarm, sensors=sensors, eng=eng,
                           cams=cams, later=later, broadcasts=broadcasts, monitor=monitor,
                           notifier=notifier, dev=dev, settings=settings)


def sent(r, device):
    return r.link.last(f"homeshield/{NODE}/pin/{device['pin']}/set")


def reply(r, device, value):
    r.h.handle(f"homeshield/{NODE}/pin/{device['pin']}/state", value)


def make(r, name, triggers, steps, conditions=(), **options):
    return r.eng.save(None, {"name": name, "triggers": triggers, "steps": steps,
                             "conditions": list(conditions), "options": options}, "admin")


def run_later(r):
    while r.later:
        _, fn = r.later.pop(0)
        fn()


# ---- the sun -----------------------------------------------------------------------

def test_sun_times_for_kuala_lumpur():
    lat, lon = CITIES["Kuala Lumpur"]
    for month in range(1, 13):
        rise, set_ = sun_times(dt.date(2026, month, 15), lat, lon)
        length = (set_ - rise) / 3600
        assert 11.8 < length < 12.4                    # near the equator: about 12 h
        utc = dt.datetime.fromtimestamp(set_, dt.timezone.utc)
        assert 10 * 60 + 50 <= utc.hour * 60 + utc.minute <= 11 * 60 + 40   # 18:50-19:40 in Malaysia
        myt = dt.timezone(dt.timedelta(hours=8))
        assert dt.datetime.fromtimestamp(rise, myt).date() == dt.date(2026, month, 15)   # that day's, not the next
    assert sun_times(dt.date(2026, 6, 21), 80.0, 0.0) == (None, None)    # midnight sun


# ---- triggers ------------------------------------------------------------------------

def test_a_camera_fall_turns_on_the_lights_where_it_happened(rig):
    make(rig, "Fall → lights", [{"type": "camera", "event": "fall"}],
         [{"type": "group", "kind": "light", "room": "@trigger", "value": "on"}, {"type": "notify"}])
    rig.eng.on_incident("opened", {"incident_id": 4, "kind": "fall", "camera_id": 1,
                                   "camera_name": "Living Room cam", "headline_type": "fall_detected"})
    assert sent(rig, rig.dev["Lounge light"]) == "1" and sent(rig, rig.dev["Floor lamp"]) == "100"
    assert sent(rig, rig.dev["Kitchen light"]) is None          # another room
    assert "Fall detected on Living Room cam" in rig.notifier.sent[0]
    assert rig.broadcasts[0][0] == "automation"
    [run] = rig.h.store.runs(rig.h.store.rules()[-1]["rule_id"])
    assert run["cause"] == "Fall detected on Living Room cam" and run["ok"]
    assert run["summary"] == "2 actions done"                   # the lights, and the notify
    # Sensor incidents and repeat detections don't count as camera triggers.
    rig.eng.on_incident("updated", {"incident_id": 4, "kind": "fall", "camera_id": 1})
    rig.eng.on_incident("opened", {"incident_id": 5, "kind": "gas", "camera_id": None,
                                   "headline_type": "gas_danger"})
    assert len(rig.notifier.sent) == 1


def test_an_incident_lists_what_automations_did_about_it(rig):
    make(rig, "Fall → lights", [{"type": "camera", "event": "fall"}],
         [{"type": "group", "kind": "light", "room": "@trigger", "value": "on"}])
    rig.eng.on_incident("opened", {"incident_id": 4, "kind": "fall", "camera_id": 1,
                                   "camera_name": "Living Room cam", "headline_type": "fall_detected"})
    store = rig.h.store
    [run] = store.incident_runs(incident_id=4)
    assert run["rule"] == "Fall → lights" and run["ok"] and len(run["steps"]) == 1
    # A sensor incident: its sensor's runs from just before it, not older ones or other sensors'.
    rid, t = run["rule_id"], rig.clock.t
    store.add_run(rid, ts=t - 900, cause="old", summary="", ok=True, steps=["earlier"], device_id=7)
    store.add_run(rid, ts=t - 200, cause="warning", summary="", ok=True, steps=["Kitchen fan on"], device_id=7)
    store.add_run(rid, ts=t - 100, cause="other", summary="", ok=True, steps=["elsewhere"], device_id=8)
    got = store.incident_runs(incident_id=99, device_id=7, start=t - 300, end=t)
    assert [r["steps"] for r in got] == [["Kitchen fan on"]]


def test_a_person_on_camera_fires_once_until_the_camera_is_empty(rig):
    make(rig, "Visitor", [{"type": "person", "camera_id": 1}],
         [{"type": "device", "device_id": rig.dev["Lounge light"]["device_id"], "value": "on"}])
    t = rig.clock.t
    rig.cams["1"]["people"] = 1
    rig.eng.tick(t)
    assert sent(rig, rig.dev["Lounge light"]) is None          # not 2 s yet
    rig.eng.tick(t + 2.5)
    assert sent(rig, rig.dev["Lounge light"]) == "1"
    n = len(rig.link.sent)
    rig.eng.tick(t + 10)
    rig.cams["1"]["people"] = 0
    rig.eng.tick(t + 12)
    rig.cams["1"]["people"] = 2
    rig.eng.tick(t + 20)
    rig.eng.tick(t + 23)
    assert len(rig.link.sent) == n                              # empty for only 8 s
    rig.cams["1"]["people"] = 0
    rig.eng.tick(t + 30)
    rig.eng.tick(t + 61)
    rig.cams["1"]["people"] = 1
    rig.eng.tick(t + 62)
    rig.eng.tick(t + 65)
    assert len(rig.link.sent) == n + 1


def test_schedules_run_at_their_minute_on_their_days(rig):
    make(rig, "Evening", [{"type": "schedule", "time": {"at": "19:15"}, "days": [2]}],   # Wednesday
         [{"type": "device", "device_id": rig.dev["Kitchen light"]["device_id"], "value": "on"}])
    rig.eng.tick(local(19, 14))
    assert sent(rig, rig.dev["Kitchen light"]) is None
    rig.eng.tick(local(19, 15))
    assert sent(rig, rig.dev["Kitchen light"]) == "1"
    n = len(rig.link.sent)
    rig.eng.tick(local(19, 15) + 30)                            # same minute: once
    rig.eng.tick(local(19, 15, day=(2026, 10, 15)))             # Thursday: not on its days
    assert len(rig.link.sent) == n


def test_sunset_schedules_follow_the_sun(rig):
    make(rig, "Porch", [{"type": "schedule", "time": {"sun": "sunset", "offset": 30}, "days": []}],
         [{"type": "device", "device_id": rig.dev["Kitchen light"]["device_id"], "value": "on"}])
    loc = CITIES["Kuala Lumpur"]
    _, set_ = sun_times(dt.date(2026, 10, 14), *loc)
    due = time.localtime(set_ + 1800)
    rig.eng.tick(local(due.tm_hour, due.tm_min))
    assert sent(rig, rig.dev["Kitchen light"]) == "1"
    view = rig.eng.rule_view(rig.h.store.rules()[-1])
    assert view["trigger_views"][0]["title"] == "30 min after sunset"
    assert "today" in view["trigger_views"][0]["sub"]


def test_home_away_and_monitoring_start_rules(rig):
    make(rig, "Arm & leave", [{"type": "homeshield", "event": "mode_away"},
                              {"type": "homeshield", "event": "monitoring_started"}],
         [{"type": "group", "kind": "light", "value": "off"},
          {"type": "group", "kind": "lock", "value": "off"}])
    rig.sensors.set_mode("away", "nadia")
    assert sent(rig, rig.dev["Kitchen light"]) == "0" and sent(rig, rig.dev["Front door"]) == "0"
    rig.link.sent.clear()
    rig.eng.on_monitoring(True, "admin")
    assert sent(rig, rig.dev["Lounge light"]) == "0"


def test_a_device_change_can_trigger_a_rule(rig):
    door = rig.dev["Front door"]
    make(rig, "Door unlocked", [{"type": "device", "device_id": door["device_id"], "value": "on"}],
         [{"type": "notify", "text": "The front door was unlocked"}])
    reply(rig, door, "0")
    reply(rig, door, "1")                                       # unlocked
    assert rig.notifier.sent == ["HomeShield: The front door was unlocked"]
    reply(rig, door, "1")                                       # no change: nothing
    assert len(rig.notifier.sent) == 1


# ---- conditions ------------------------------------------------------------------------

def test_conditions_must_all_hold(rig):
    lamp = rig.dev["Floor lamp"]
    make(rig, "Night visitor", [{"type": "person", "camera_id": None}],
         [{"type": "device", "device_id": lamp["device_id"], "value": "40"}],
         conditions=[{"type": "time", "from": {"at": "22:00"}, "to": {"at": "06:00"}},
                     {"type": "mode", "mode": "away"}])
    ev = {"type": "person", "camera_id": 1, "room": "Living room", "text": "Person seen"}
    rig.clock.t = local(23, 0)
    rig.eng.fire(ev)
    assert sent(rig, lamp) is None                              # at Home
    rig.sensors.set_mode("away", "x")
    rig.clock.t = local(12, 0)
    rig.eng.fire(ev)
    assert sent(rig, lamp) is None                              # daytime
    rig.clock.t = local(2, 30)                                  # after midnight still counts
    rig.eng.fire(ev)
    assert sent(rig, lamp) == "40"


def test_device_and_monitoring_conditions(rig):
    door = rig.dev["Front door"]
    make(rig, "Remind", [{"type": "homeshield", "event": "mode_away"}], [{"type": "notify"}],
         conditions=[{"type": "device", "device_id": door["device_id"], "value": "on"},
                     {"type": "monitoring", "on": True}])
    reply(rig, door, "0")
    rig.sensors.set_mode("away", "x")
    assert rig.notifier.sent == []                              # door locked
    rig.sensors.set_mode("home", "x")
    reply(rig, door, "1")
    rig.sensors.set_mode("away", "x")
    assert len(rig.notifier.sent) == 1


# ---- steps -----------------------------------------------------------------------------

def test_waits_split_the_steps(rig):
    light = rig.dev["Kitchen light"]
    rule = make(rig, "Porch for 5 min", [{"type": "homeshield", "event": "mode_home"}],
                [{"type": "device", "device_id": light["device_id"], "value": "on"},
                 {"type": "wait", "seconds": 300},
                 {"type": "device", "device_id": light["device_id"], "value": "off"}])
    rig.sensors.set_mode("away", "x")
    rig.sensors.set_mode("home", "x")
    assert sent(rig, light) == "1"
    [run] = rig.h.store.runs(rule["rule_id"])
    assert run["summary"] == "waiting 5 min…"
    waits = [s for s, _ in rig.later if s == 300]
    assert waits
    reply(rig, light, "1")
    secs, fn = rig.later.pop(0)
    fn()
    assert sent(rig, light) == "0"
    assert rig.h.store.runs(rule["rule_id"])[0]["summary"] == "2 actions done"


def test_scenes_mode_and_monitoring_steps(rig):
    scene = rig.h.store.save_scene(None, name="Night", description="", icon="moon",
                                   actions=[{"device_id": rig.dev["Floor lamp"]["device_id"], "value": "20"}])
    make(rig, "Bedtime", [{"type": "schedule", "time": {"at": "23:00"}, "days": []}],
         [{"type": "scene", "scene_id": scene["scene_id"]}, {"type": "mode", "mode": "away"},
          {"type": "monitoring", "on": False}])
    rig.eng.tick(local(23, 0))
    assert sent(rig, rig.dev["Floor lamp"]) == "20"
    assert rig.sensors.mode()["mode"] == "away" and rig.monitor["on"] is False


def test_an_unconfirmed_switch_is_retried_once_then_reported(rig):
    light = rig.dev["Kitchen light"]
    rule = make(rig, "On", [{"type": "homeshield", "event": "mode_away"}],
                [{"type": "device", "device_id": light["device_id"], "value": "on"}])
    rig.sensors.set_mode("away", "x")
    assert len([1 for t, p, _ in rig.link.sent if t.endswith("/26/set")]) == 1
    run_later(rig)                                              # no reply: resent, then reported
    assert len([1 for t, p, _ in rig.link.sent if t.endswith("/26/set")]) == 2
    [run] = rig.h.store.runs(rule["rule_id"])
    assert not run["ok"] and run["summary"] == f"{NODE} did not reply (Kitchen light)"


def test_a_confirmed_switch_is_left_alone(rig):
    light = rig.dev["Kitchen light"]
    rule = make(rig, "On", [{"type": "homeshield", "event": "mode_away"}],
                [{"type": "device", "device_id": light["device_id"], "value": "on"}])
    rig.sensors.set_mode("away", "x")
    reply(rig, light, "1")
    run_later(rig)
    assert len([1 for t, p, _ in rig.link.sent if t.endswith("/26/set")]) == 1
    assert rig.h.store.runs(rule["rule_id"])[0]["ok"]


def test_cooldown_and_the_loop_guard(rig):
    make(rig, "Calm", [{"type": "homeshield", "event": "mode_away"}], [{"type": "notify"}],
         cooldown_min=5)
    rig.sensors.set_mode("away", "x")
    rig.sensors.set_mode("home", "x")
    rig.sensors.set_mode("away", "x")
    assert len(rig.notifier.sent) == 1
    # Two rules switching each other back and forth stop after 10 runs a minute.
    a, b = rig.dev["Kitchen light"], rig.dev["Lounge light"]
    make(rig, "A", [{"type": "device", "device_id": a["device_id"], "value": "any"}],
         [{"type": "device", "device_id": b["device_id"], "value": "on"}])
    make(rig, "B", [{"type": "device", "device_id": b["device_id"], "value": "any"}],
         [{"type": "device", "device_id": a["device_id"], "value": "on"}])
    for i in range(30):
        reply(rig, a, str(i % 2))
        reply(rig, b, str(i % 2))
    runs = [r for r in rig.h.store.rules() if r["name"] in ("A", "B")]
    assert all(r["fire_count"] <= 10 for r in runs)
    assert any("times in a minute" in a["text"] for a in rig.h.store.activity(200))


def test_paused_rules_and_run_when_stopped(rig):
    rule = make(rig, "Only while watching", [{"type": "homeshield", "event": "mode_away"}],
                [{"type": "notify"}], run_when_stopped=False)
    rig.monitor["on"] = False
    rig.sensors.set_mode("away", "x")
    assert rig.notifier.sent == []
    view = rig.eng.set_enabled(rule["rule_id"], False, "dad")
    assert view["changed_by"] == "dad" and not view["enabled"]
    rig.monitor["on"] = True
    rig.sensors.set_mode("home", "x")
    rig.sensors.set_mode("away", "x")
    assert rig.notifier.sent == []


def test_a_test_run_does_the_steps_but_no_incident(rig):
    rule = make(rig, "T", [{"type": "homeshield", "event": "mode_away"}],
                [{"type": "device", "device_id": rig.dev["TV socket"]["device_id"], "value": "off"},
                 {"type": "buzzer"}, {"type": "notify"}])
    rig.eng.test(rule["rule_id"], "admin")
    assert sent(rig, rig.dev["TV socket"]) == "0"
    assert rig.alarm.command().startswith("ALARM TEST")
    assert rig.notifier.sent[0].startswith("HomeShield test run.")
    [run] = rig.h.store.runs(rule["rule_id"])
    assert run["cause"] == "Test run by admin"
    assert rig.h.store.rule(rule["rule_id"])["fire_count"] == 0


# ---- editing ---------------------------------------------------------------------------

def test_rules_are_validated(rig):
    light = rig.dev["Kitchen light"]
    step = {"type": "device", "device_id": light["device_id"], "value": "on"}
    for data, why in (
        ({"name": "x", "triggers": [], "steps": [step]}, "Add a trigger"),
        ({"name": "x", "triggers": [{"type": "homeshield", "event": "mode_away"}], "steps": []}, "at least one action"),
        ({"name": "x", "triggers": [{"type": "homeshield", "event": "mode_away"}],
          "steps": [{"type": "incident"}]}, "sensor triggers"),
        ({"name": "x", "triggers": [{"type": "schedule", "time": {"at": "25:00"}}], "steps": [step]}, "19:15"),
        ({"name": "x", "triggers": [{"type": "homeshield", "event": "mode_away"}],
          "steps": [{"type": "wait", "seconds": 2}]}, "between 5 seconds"),
        ({"name": "x", "triggers": [{"type": "device", "device_id": 999}], "steps": [step]}, "Pick a device"),
        ({"name": "", "triggers": [{"type": "homeshield", "event": "mode_away"}], "steps": [step]}, "Enter a name"),
    ):
        with pytest.raises(PinError, match=why):
            rig.eng.save(None, data, "admin")


def test_sensor_checklists_hand_over_to_the_automations_page(rig):
    gas = rig.h.map_pin(NODE, 34, "gas", "Gas sensor", "", {}, "admin")
    rule = next(r for r in rig.h.store.rules()
                if r["origin"] == "sensor" and r["trigger"]["level"] == "warning")
    data = rig.eng.rule_view(rule)
    rig.eng.save(rule["rule_id"], {"name": "Gas leak response", "triggers": data["triggers"],
                                   "steps": data["steps"] + [{"type": "wait", "seconds": 60}, {"type": "notify"}],
                                   "conditions": [], "options": {}}, "admin")
    view = rig.sensors.sensor_view(rig.h.store.device(gas["device_id"]))
    assert view["response"]["warning"]["extended"]
    with pytest.raises(PinError, match="Automations page"):
        rig.sensors.set_response(gas["device_id"], "warning", ["notify"], "admin")
    assert rig.h.store.rule(rule["rule_id"])["name"] == "Gas leak response"


def test_starters_are_added_once_and_switched_off(rig):
    rig.eng.start()
    names = [r["name"] for r in rig.h.store.rules() if r["origin"] == "starter"]
    assert names == ["Fall → lights on", "Fire detected", "Night visitor",
                     "Unknown face while away", "Arm & leave", "Evening lights"]
    assert not any(r["enabled"] for r in rig.h.store.rules() if r["origin"] == "starter")
    rig.eng.start()
    assert len([r for r in rig.h.store.rules() if r["origin"] == "starter"]) == 6
    views = {v["name"]: v for v in rig.eng.automations_view()["rules"]}
    assert views["Night visitor"]["summary"] == "Person seen → turn on every light, wait 5 min +1"
    assert views["Arm & leave"]["trigger_views"][0]["title"] == "Switched to Away"


# ---- the API ---------------------------------------------------------------------------

def test_admins_manage_automations(app):  # noqa: F811
    h = app.extensions["homeshield_iot"]
    admin = _login(app, "admin", "admin")
    adopted(h)
    light = admin.post("/api/devices", json={"node_id": NODE, "pin": 26, "kind": "light",
                                             "name": "Kitchen light"}).get_json()
    data = admin.get("/api/automations").get_json()
    assert data["total"] == 6 and data["active"] == 0             # the starters
    assert data["sun"]["city"] == "Kuala Lumpur" and data["sun"]["sunset"]
    r = admin.post("/api/automations", json={
        "name": "Away lights off", "triggers": [{"type": "homeshield", "event": "mode_away"}],
        "steps": [{"type": "device", "device_id": light["device_id"], "value": "off"}]})
    assert r.status_code == 200
    rid = r.get_json()["rule_id"]
    assert admin.post(f"/api/automations/{rid}/test").status_code == 200
    assert admin.post(f"/api/automations/{rid}/enabled", json={"enabled": False}).get_json()["enabled"] is False
    bad = admin.put(f"/api/automations/{rid}", json={"name": "x", "triggers": [], "steps": []})
    assert bad.status_code == 400
    assert admin.put("/api/automations/location", json={"city": "Kuching"}).get_json()["city"] == "Kuching"
    assert admin.put("/api/automations/location", json={"city": "Atlantis"}).status_code == 400
    assert admin.delete(f"/api/automations/{rid}").status_code == 200
    assert admin.get("/api/automations").get_json()["total"] == 6
