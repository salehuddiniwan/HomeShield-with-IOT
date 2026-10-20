"""Automations: When something happens, And only if conditions hold, Then
do these steps in order. They run on the HomeShield server, so they keep
working with every dashboard closed.

Rules live in the rules table (store.py); the Sensors page's checklists
are rules too (origin "sensor"). Any trigger starts a rule; every
condition must hold.

Triggers
    sensor      {"device_id", "level": warning|danger|active, "mode": any|away}
    camera      {"event": fall|fire|intruder|zone|sos|any, "camera_id": id or null for any}
    person      {"camera_id": id or null}             someone appears on a camera
    schedule    {"time": {"at": "19:15"} or {"sun": "sunset", "offset": 30}, "days": [0-6]}
    homeshield  {"event": monitoring_started|monitoring_stopped|mode_away|mode_home}
    device      {"device_id", "value": on|off|any}     a relay or dimmer reported a change
Conditions
    time        {"from": spec, "to": spec}            spec as in schedule; may pass midnight
    days        {"days": [0-6]}                       Monday is 0
    mode        {"mode": home|away}
    device      {"device_id", "value": on|off}
    sensor      {"device_id", "level": normal|warning|danger|active|alert}
    monitoring  {"on": true|false}
Steps, in order
    device {"device_id", "value"}    group {"kind", "room", "value": on|off}    scene {"scene_id"}
    notify {"text"}    buzzer {}    incident {}    wait {"seconds"}    mode {"mode"}    monitoring {"on"}
Options: cooldown_min (wait before running again), run_when_stopped, retry (resend
once to a board that didn't confirm).
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import threading
import time
from collections import deque
from typing import Any, Callable, Optional

from ..incidents import SENSOR_EVENTS
from ..notify import LABELS
from .hub import CONFIRM_S
from .kinds import KINDS, PinError, describe, normalise_value
from .sensors import LEVELS as SENSOR_LEVELS, SENSOR_KINDS, SERIES
from .sun import CITIES, DEFAULT_CITY, sun_times

log = logging.getLogger(__name__)

TRIGGER_TYPES = ("sensor", "camera", "person", "schedule", "homeshield", "device")
CONDITION_TYPES = ("time", "days", "mode", "device", "sensor", "monitoring")
STEP_TYPES = ("device", "group", "scene", "notify", "buzzer", "incident", "wait", "mode", "monitoring")
CAMERA_EVENTS = {"fall": "Fall detected", "fire": "Fire or smoke", "intruder": "Unknown face",
                 "zone": "Child in a danger zone", "sos": "SOS button pressed", "any": "Any camera alert"}
HOMESHIELD_EVENTS = {"monitoring_started": "Monitoring is started",
                     "monitoring_stopped": "Monitoring is stopped",
                     "mode_away": "Switched to Away", "mode_home": "Switched to Home"}
GROUPS = {"light": ("light", "dimmer"), "socket": ("socket",), "fan": ("fan",), "relay": ("relay",),
          "lock": ("lock",), "valve": ("valve",),
          "all": tuple(k for k, v in KINDS.items() if v["type"] in ("out", "pwm"))}
GROUP_NAMES = {"light": "every light", "socket": "every socket", "fan": "every fan",
               "relay": "every relay", "lock": "every lock", "valve": "every valve",
               "all": "everything switchable"}
TRIGGER_ROOM = "@trigger"          # a group step's room: where the trigger happened
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
DEFAULT_OPTIONS = {"cooldown_min": 0, "run_when_stopped": True, "retry": True}
MAX_RUNS_PER_MIN = 10              # a loop between rules stops here
PERSON_HOLD_S = 2.0                # seen this long counts as someone there
PERSON_CLEAR_S = 30.0              # and the camera must be empty this long before the next
MAX_WAIT_S = 6 * 3600
_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _duration(seconds: int) -> str:
    if seconds % 3600 == 0:
        return _plural(seconds // 3600, "hour")
    if seconds % 60 == 0:
        return f"{seconds // 60} min"
    return f"{seconds} s"


class AutomationEngine:
    def __init__(self, hub, sensors, *, settings, alarm=None, notifier=None,
                 broadcast: Optional[Callable[[str, dict], None]] = None,
                 cameras: Optional[Callable[[], dict]] = None,
                 monitoring: Optional[Callable[[], bool]] = None,
                 set_monitoring: Optional[Callable[[bool], None]] = None,
                 clock: Callable[[], float] = time.time,
                 later: Optional[Callable[[float, Callable[[], None]], None]] = None):
        self.hub = hub
        self.store = hub.store
        self.sensors = sensors
        self.settings = settings
        self.alarm = alarm
        self._notifier = notifier
        self._broadcast = broadcast
        self._cameras_fn = cameras
        self._monitoring = monitoring
        self._set_monitoring = set_monitoring
        self._clock = clock
        self._later = later or self._timer
        self._lock = threading.RLock()
        self._last_run: dict[int, float] = {}
        self._recent: dict[int, deque] = {}
        self._loop_logged: dict[int, float] = {}
        self._sched_done: dict[tuple[int, int], str] = {}
        self._minute: Optional[str] = None
        self._people: dict[int, dict[str, Any]] = {}
        self._people_at = 0.0
        hub.automations = self
        if sensors is not None:
            sensors.automations = self

    @staticmethod
    def _timer(seconds: float, fn: Callable[[], None]) -> None:
        t = threading.Timer(seconds, fn)
        t.daemon = True
        t.start()

    # ---- where things happen ------------------------------------------------------------

    def _cameras(self) -> dict[int, dict[str, Any]]:
        if self._cameras_fn is None:
            return {}
        try:
            return {int(k): v for k, v in (self._cameras_fn() or {}).items()}
        except Exception as e:                       # cameras not started yet
            log.debug("cameras unavailable: %s", e)
            return {}

    def _mode(self) -> str:
        return self.sensors.mode()["mode"] if self.sensors is not None else "home"

    # ---- sun ------------------------------------------------------------------------------

    def location(self) -> dict[str, Any]:
        city = self.settings.get("sun_city") or DEFAULT_CITY
        lat, lon = CITIES.get(city, CITIES[DEFAULT_CITY])
        if city not in CITIES:
            try:
                lat, lon = float(self.settings.get("sun_lat")), float(self.settings.get("sun_lon"))
            except (TypeError, ValueError):
                city, (lat, lon) = DEFAULT_CITY, CITIES[DEFAULT_CITY]
        return {"city": city, "lat": lat, "lon": lon}

    def set_location(self, city: Any, by: str) -> dict[str, Any]:
        if city not in CITIES:
            raise PinError("Pick a city from the list.")
        self.settings.update({"sun_city": city})
        self.store.log(f"Sunrise and sunset now for {city}", by=by)
        self._changed()
        return self.sun_info()

    def _sun(self, day: dt.date) -> tuple[Optional[float], Optional[float]]:
        loc = self.location()
        return sun_times(day, loc["lat"], loc["lon"])

    def sun_info(self) -> dict[str, Any]:
        today = dt.date.fromtimestamp(self._clock())
        rise, set_ = self._sun(today)
        fmt = (lambda ts: time.strftime("%H:%M", time.localtime(ts)) if ts else None)
        return {**self.location(), "sunrise": fmt(rise), "sunset": fmt(set_), "cities": list(CITIES)}

    def _spec_minute(self, spec: dict[str, Any], day: dt.date) -> Optional[int]:
        """Minutes after midnight (local) for {"at": "19:15"} or {"sun": "sunset", "offset": 30}."""
        if "at" in spec:
            m = _HHMM.match(str(spec["at"]))
            return int(m.group(1)) * 60 + int(m.group(2)) if m else None
        rise, set_ = self._sun(day)
        ts = rise if spec.get("sun") == "sunrise" else set_
        if ts is None:
            return None
        lt = time.localtime(ts + int(spec.get("offset") or 0) * 60)
        return lt.tm_hour * 60 + lt.tm_min

    # ---- triggers -------------------------------------------------------------------------

    def fire(self, ev: dict[str, Any], only: Optional[int] = None) -> list[int]:
        """Something happened: run every enabled rule it triggers."""
        mode = self._mode()
        ran = []
        for rule in self.store.rules():
            if not rule["enabled"] or (only is not None and rule["rule_id"] != only):
                continue
            if any(self._matches(t, ev, mode) for t in rule["triggers"]) and self.run(rule, ev):
                ran.append(rule["rule_id"])
        return ran

    def _matches(self, t: dict[str, Any], ev: dict[str, Any], mode: str) -> bool:
        kind = t.get("type")
        if kind != ev.get("type"):
            return False
        if kind == "sensor":
            return (t.get("device_id") == ev["device_id"] and t.get("level") == ev["level"]
                    and t.get("mode", "any") in ("any", mode))
        if kind == "camera":
            return (t.get("event", "any") in ("any", ev["event"])
                    and t.get("camera_id") in (None, ev.get("camera_id")))
        if kind == "person":
            return t.get("camera_id") in (None, ev["camera_id"])
        if kind == "homeshield":
            return t.get("event") == ev["event"]
        if kind == "device":
            want = t.get("value", "any")
            on = ev["value"] not in ("0", "")
            return t.get("device_id") == ev["device_id"] and (
                want == "any" or (want == "on") == on)
        if kind == "schedule":
            return ev.get("rule_id") is not None
        return False

    def on_incident(self, change: str, inc: dict[str, Any]) -> None:
        """EventBus listener: a camera detection (or an SOS press) became an incident."""
        if change not in ("opened", "escalated") or inc.get("headline_type") in SENSOR_EVENTS:
            return
        kind, cid = inc.get("kind"), inc.get("camera_id")
        if cid is None and kind != "sos":
            return
        cam = self._cameras().get(cid, {}) if cid is not None else {}
        where = inc.get("camera_name") or cam.get("name") or "a camera"
        self.fire({"type": "camera", "event": kind, "camera_id": cid,
                   "incident_id": inc.get("incident_id"),
                   "room": cam.get("location") or inc.get("camera_name"),
                   "text": f"{LABELS.get(inc.get('headline_type'), 'Alert')} on {where}",
                   "alarm_kind": kind, "ring_key": inc.get("incident_id")})

    def on_device_state(self, dev: dict[str, Any], value: str) -> None:
        self.fire({"type": "device", "device_id": dev["device_id"], "value": value,
                   "room": dev["room"], "text": f"{dev['name']} → {describe(dev['kind'], value)}"})

    def on_mode(self, mode: str, by: str) -> None:
        self.fire({"type": "homeshield", "event": f"mode_{mode}",
                   "text": f"Switched to {mode.capitalize()} by {by}"})

    def on_monitoring(self, on: bool, by: str) -> None:
        self.fire({"type": "homeshield", "event": "monitoring_started" if on else "monitoring_stopped",
                   "text": f"Monitoring {'started' if on else 'stopped'} by {by}"})

    def tick(self, now: Optional[float] = None) -> None:
        """Schedules (once a minute) and people on cameras (once a second)."""
        now = self._clock() if now is None else now
        lt = time.localtime(now)
        key = time.strftime("%Y-%m-%d %H:%M", lt)
        if key != self._minute:
            self._minute = key
            self._run_schedules(now, lt, key)
        if now - self._people_at >= 1.0:
            self._people_at = now
            self._check_people(now)

    def _run_schedules(self, now: float, lt: time.struct_time, key: str) -> None:
        minute = lt.tm_hour * 60 + lt.tm_min
        today = dt.date.fromtimestamp(now)
        for rule in self.store.rules():
            if not rule["enabled"]:
                continue
            for i, t in enumerate(rule["triggers"]):
                if t.get("type") != "schedule":
                    continue
                days = t.get("days") or []
                if days and lt.tm_wday not in days:
                    continue
                if self._spec_minute(t.get("time") or {}, today) != minute:
                    continue
                if self._sched_done.get((rule["rule_id"], i)) == key:
                    continue
                self._sched_done[(rule["rule_id"], i)] = key
                self.fire({"type": "schedule", "rule_id": rule["rule_id"],
                           "text": f"Schedule: {self._describe_trigger(t)['title']}"},
                          only=rule["rule_id"])

    def _check_people(self, now: float) -> None:
        if not any(r["enabled"] and any(t.get("type") == "person" for t in r["triggers"])
                   for r in self.store.rules()):
            return
        for cid, cam in self._cameras().items():
            people = int(cam.get("people") or 0) if cam.get("active") else 0
            st = self._people.setdefault(cid, {"since": None, "fired": False, "empty": now})
            if people > 0:
                st["empty"] = None
                st["since"] = st["since"] or now
                if not st["fired"] and now - st["since"] >= PERSON_HOLD_S:
                    st["fired"] = True
                    name = cam.get("name") or f"camera {cid}"
                    self.fire({"type": "person", "camera_id": cid,
                               "room": cam.get("location") or name,
                               "text": f"Person seen on {name}", "alarm_kind": "intruder"})
            else:
                st["since"] = None
                st["empty"] = st["empty"] or now
                if st["fired"] and now - st["empty"] >= PERSON_CLEAR_S:
                    st["fired"] = False

    # ---- conditions -----------------------------------------------------------------------

    def _holds(self, c: dict[str, Any], now: float) -> bool:
        kind = c.get("type")
        lt = time.localtime(now)
        if kind == "time":
            day = dt.date.fromtimestamp(now)
            a, b = self._spec_minute(c.get("from") or {}, day), self._spec_minute(c.get("to") or {}, day)
            if a is None or b is None:
                return False
            m = lt.tm_hour * 60 + lt.tm_min
            return a <= m < b if a <= b else (m >= a or m < b)
        if kind == "days":
            return lt.tm_wday in (c.get("days") or [])
        if kind == "mode":
            return self._mode() == c.get("mode")
        if kind == "device":
            dev = self.store.device(c.get("device_id"))
            if dev is None or dev["state"] is None:
                return False
            return (dev["state"] not in ("0", "")) == (c.get("value") == "on")
        if kind == "sensor":
            dev = self.store.device(c.get("device_id"))
            if dev is None or self.sensors is None:
                return False
            level = (self.sensors.reading(dev) or {}).get("level", "normal")
            return level != "normal" if c.get("level") == "alert" else level == c.get("level")
        if kind == "monitoring":
            return self._monitoring is not None and bool(self._monitoring()) == bool(c.get("on"))
        return False

    # ---- running ----------------------------------------------------------------------------

    def run(self, rule: dict[str, Any], ev: dict[str, Any], test: bool = False) -> bool:
        rid = rule["rule_id"]
        now = self._clock()
        opts = {**DEFAULT_OPTIONS, **(rule.get("options") or {})}
        if not rule["actions"]:
            return False
        if not test:
            if not all(self._holds(c, now) for c in rule["conditions"]):
                return False
            if not opts["run_when_stopped"] and self._monitoring is not None and not self._monitoring():
                return False
            cooldown = float(opts.get("cooldown_min") or 0) * 60
            with self._lock:
                if cooldown and now - self._last_run.get(rid, -1e18) < cooldown:
                    return False
                recent = self._recent.setdefault(rid, deque())
                while recent and now - recent[0] > 60:
                    recent.popleft()
                if len(recent) >= MAX_RUNS_PER_MIN:
                    if now - self._loop_logged.get(rid, -1e18) > 60:
                        self._loop_logged[rid] = now
                        self.store.log(f"“{rule['name']}” ran {MAX_RUNS_PER_MIN} times in a minute: "
                                       f"skipped until it calms down (two rules starting each other?)",
                                       by="Automation")
                    return False
                recent.append(now)
                self._last_run[rid] = now
        run = {"rule": rule, "ev": ev, "test": test, "opts": opts, "done": [], "problems": [],
               "sent": [], "count": 0, "run_id": None, "reported": 0, "notify": None,
               "by": ("Test run: " if test else "Automation: ") + rule["name"],
               "opens_incident": any(a.get("type") == "incident" for a in rule["actions"])}
        self._continue(run, 0)
        return True

    def _continue(self, run: dict[str, Any], i: int) -> None:
        steps = run["rule"]["actions"]
        while i < len(steps):
            step = steps[i]
            if step.get("type") == "wait":
                seconds = int(step.get("seconds") or 0)
                self._report(run, waiting=seconds)
                nxt = i + 1
                self._later(seconds, lambda: self._continue(run, nxt))
                return
            try:
                self._do(run, step)
            except Exception as e:                      # one bad step mustn't stop the rest
                log.warning("automation step %s failed: %s", step.get("type"), e)
                run["problems"].append(f"{step.get('type')} step failed ({e})")
            i += 1
        self._report(run)

    def _do(self, run: dict[str, Any], step: dict[str, Any]) -> None:
        kind, ev, rule, by = step.get("type"), run["ev"], run["rule"], run["by"]
        done = run["done"]
        if kind == "device":
            target = self.store.device(step.get("device_id"))
            if target is None:
                run["problems"].append("a device in this automation was removed")
                return
            self._switch(run, target, step.get("value"))
        elif kind == "group":
            targets = self._group(step, ev)
            switched = 0
            for d in targets:
                if self._switch(run, d, step.get("value"), quiet=True):
                    switched += 1
            label = self._describe_step(step)["title"]
            offline = len(targets) - switched
            done.append(f"{label.lower()}: {_plural(switched, 'device')}"
                        + (f", {offline} not switched" if offline else ""))
            run["count"] += 1
        elif kind == "scene":
            r = self.hub.run_scene(int(step.get("scene_id")), by)
            scene = self.store.scene(int(step.get("scene_id"))) or {}
            done.append(f"scene “{scene.get('name', '?')}” ran ({_plural(r['done'], 'device')})")
            run["count"] += 1
        elif kind == "notify":
            run["notify"] = step                       # sent at the end, saying what was done
        elif kind == "buzzer" and self.alarm is not None:
            if run["test"]:
                try:
                    self.alarm.test()
                    done.append("buzzer test (three beeps)")
                except RuntimeError:
                    done.append("buzzer already sounding")
            else:
                self.alarm.ring(ev.get("ring_key") or ("rule", rule["rule_id"]),
                                ev.get("alarm_kind") or "intruder")
                done.append("alarm buzzer sounding")
            run["count"] += 1
        elif kind == "incident":
            if run["test"]:
                done.append("incident skipped in a test run")
            elif ev.get("open_incident"):
                ev["open_incident"]()
                done.append("incident opened")
                run["count"] += 1
        elif kind == "mode" and self.sensors is not None:
            self.sensors.set_mode(step.get("mode"), by)
            done.append("switched to Away" if step.get("mode") == "away" else "switched to Home")
            run["count"] += 1
        elif kind == "monitoring" and self._set_monitoring is not None:
            on = bool(step.get("on"))
            self._set_monitoring(on)
            done.append("monitoring started" if on else "monitoring stopped")
            run["count"] += 1
            self.on_monitoring(on, by)

    def _switch(self, run: dict[str, Any], target: dict[str, Any], value: Any,
                quiet: bool = False) -> bool:
        try:
            r = self.hub.set_device(target["device_id"], value, run["by"])
        except PinError as e:
            if not quiet:
                run["done"].append(f"{target['name']} not switched ({e})")
                run["problems"].append(f"{target['name']} not switched: {e}")
            return False
        run["sent"].append((target["device_id"], r["sent"], target["name"], target["node_id"]))
        if not quiet:
            run["done"].append(f"{target['name']} {r['label'].lower()}")
            run["count"] += 1
        return True

    def _group(self, step: dict[str, Any], ev: dict[str, Any]) -> list[dict[str, Any]]:
        kinds = GROUPS.get(step.get("kind"), ())
        room = step.get("room")
        if room == TRIGGER_ROOM:
            room = ev.get("room") or ""
        return [d for d in self.store.devices()
                if d["kind"] in kinds and (not room or d["room"] == room)]

    def _report(self, run: dict[str, Any], waiting: Optional[int] = None) -> None:
        """Record the run so far: the run log, the activity log, and the sensor's alert."""
        self._send_notify(run)
        rule, ev, done = run["rule"], run["ev"], run["done"]
        new = done[run["reported"]:]
        run["reported"] = len(done)
        if waiting is not None:
            summary = f"waiting {_duration(waiting)}…"
        elif run["problems"]:
            summary = run["problems"][0]
        else:
            summary = f"{_plural(run['count'], 'action')} done"
        cause = ev.get("text", "")
        steps = done + ([f"waiting {_duration(waiting)}"] if waiting else [])
        if run["run_id"] is None:
            run["run_id"] = self.store.add_run(
                rule["rule_id"], ts=self._clock(), cause=cause, summary=summary,
                ok=not run["problems"], steps=steps,
                device_id=ev.get("device_id") if ev.get("type") == "sensor" else None,
                camera_id=ev.get("camera_id"), incident_id=ev.get("incident_id"))
            if not run["test"]:
                self.store.rule_fired(rule["rule_id"])
        else:
            self.store.update_run(run["run_id"], summary=summary, ok=not run["problems"], steps=steps)
        if new:
            self.store.log(f"{rule['name']}: " + ", ".join(new),
                           by="Test run" if run["test"] else "Automation")
            if ev.get("done"):
                ev["done"](new)
        if waiting is None and run["sent"]:
            batch, run["sent"] = run["sent"], []
            self._later(CONFIRM_S + 1, lambda: self._confirm(run, batch, 1))
        self.hub._notify("activity")
        self._changed()

    def _send_notify(self, run: dict[str, Any]) -> None:
        step, run["notify"] = run["notify"], None
        if step is None:
            return
        ev, rule, done = run["ev"], run["rule"], run["done"]
        ready = self._notifier is not None and self._notifier.ready()
        if run["opens_incident"] and ev.get("open_incident") and not run["test"]:
            run["count"] += 1
            done.append("WhatsApp sent with the incident" if ready
                        else "shown on the dashboard (WhatsApp isn't set up)")
            return
        if step.get("text"):
            text = f"HomeShield: {step['text']}"
        elif ev.get("message"):
            text = ev["message"](done)
        else:
            when = time.strftime("%H:%M", time.localtime(self._clock()))
            text = f"HomeShield: {ev.get('text') or rule['name']} at {when} ({rule['name']})."
            steps = [d for d in done if not d.startswith(("WhatsApp", "shown"))]
            if steps:
                text += " Done automatically: " + ", ".join(steps) + "."
        if run["test"]:
            text = "HomeShield test run. " + text.removeprefix("HomeShield: ")
        if self._broadcast is not None and ev.get("type") != "sensor":
            self._broadcast("automation", {"rule": rule["name"], "text": text})
        run["count"] += 1
        if self._notifier is not None and self._notifier.send_text(text):
            n = len(self._notifier.phones())
            done.append(f"WhatsApp sent to {_plural(n, 'number')}")
        else:
            done.append("shown on the dashboard (WhatsApp isn't set up)")

    def _confirm(self, run: dict[str, Any], batch: list[tuple], attempt: int) -> None:
        """A few seconds after switching: did each board report the new state?"""
        missing = []
        for did, payload, name, node in batch:
            dev = self.store.device(did)
            if dev is not None and dev["state"] != payload:
                missing.append((did, payload, name, node))
        if not missing:
            return
        if attempt == 1 and run["opts"].get("retry") and not run["test"]:
            again = []
            for did, payload, name, node in missing:
                try:
                    self.hub.set_device(did, payload, run["by"] + " (retry)")
                    again.append((did, payload, name, node))
                except PinError as e:
                    run["problems"].append(f"{name} not switched: {e}")
            if again:
                self._later(CONFIRM_S + 1, lambda: self._confirm(run, again, 2))
                return
        for _, _, name, node in missing:
            run["problems"].append(f"{node} did not reply ({name})")
        summary = run["problems"][0]
        self.store.update_run(run["run_id"], summary=summary, ok=False, steps=run["done"])
        self._changed()

    def test(self, rule_id: int, by: str) -> dict[str, Any]:
        rule = self.store.rule(rule_id)
        if rule is None:
            raise PinError("That automation no longer exists.")
        if not rule["actions"]:
            raise PinError("Add an action first.")
        self.run(rule, {"type": "test", "text": f"Test run by {by}"}, test=True)
        return self.rule_view(self.store.rule(rule_id))

    # ---- editing ------------------------------------------------------------------------------

    def save(self, rule_id: Optional[int], data: dict[str, Any], by: str) -> dict[str, Any]:
        old = self.store.rule(rule_id) if rule_id is not None else None
        if rule_id is not None and old is None:
            raise PinError("That automation no longer exists.")
        triggers = [self._clean_trigger(t) for t in self._list(data.get("triggers"), "triggers")]
        conditions = [self._clean_condition(c) for c in self._list(data.get("conditions"), "conditions")]
        steps = [self._clean_step(s) for s in self._list(data.get("steps"), "steps")]
        if not triggers:
            raise PinError("Add a trigger: when should it run?")
        if not steps:
            raise PinError("Add at least one action.")
        if any(s["type"] == "incident" for s in steps) and not any(t["type"] == "sensor" for t in triggers):
            raise PinError("Open an incident works with sensor triggers. Camera detections "
                           "open their own incident.")
        options = self._clean_options(data.get("options") or {})
        enabled = bool(data.get("enabled", old["enabled"] if old else True))
        rule = self.store.save_rule(rule_id, name=data.get("name"), triggers=triggers,
                                    conditions=conditions, actions=steps, options=options,
                                    origin=old["origin"] if old else "user", enabled=enabled, by=by)
        self.store.log(f"Automation “{rule['name']}” {'saved' if old else 'created'}", by=by)
        self._changed()
        return self.rule_view(rule)

    def set_enabled(self, rule_id: int, enabled: bool, by: str) -> dict[str, Any]:
        rule = self.store.rule(rule_id)
        if rule is None:
            raise PinError("That automation no longer exists.")
        rule = self.store.save_rule(rule_id, name=rule["name"], triggers=rule["triggers"],
                                    conditions=rule["conditions"], actions=rule["actions"],
                                    options=rule["options"], origin=rule["origin"],
                                    enabled=bool(enabled), by=by)
        self.store.log(f"Automation “{rule['name']}” {'switched on' if enabled else 'paused'}", by=by)
        self._changed()
        return self.rule_view(rule)

    def remove(self, rule_id: int, by: str) -> None:
        rule = self.store.rule(rule_id)
        if rule is None:
            raise PinError("That automation no longer exists.")
        self.store.remove_rule(rule_id)
        self.store.log(f"Automation “{rule['name']}” deleted", by=by)
        self._changed()

    @staticmethod
    def _list(value: Any, what: str) -> list:
        if value is None:
            return []
        if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
            raise PinError(f"Send the {what} as a list.")
        return value

    def _device(self, device_id: Any, *, sensor: bool = False) -> dict[str, Any]:
        try:
            dev = self.store.device(int(device_id))
        except (TypeError, ValueError):
            dev = None
        if dev is None:
            raise PinError("Pick a device.")
        is_sensor = dev["kind"] in SENSOR_KINDS
        if sensor != is_sensor or (not sensor and KINDS[dev["kind"]]["type"] not in ("out", "pwm")):
            raise PinError(f"{dev['name']} can't be used there.")
        return dev

    def _camera(self, camera_id: Any) -> Optional[int]:
        if camera_id in (None, "", "any"):
            return None
        try:
            return int(camera_id)
        except (TypeError, ValueError):
            raise PinError("Pick a camera.") from None

    def _clean_spec(self, spec: Any) -> dict[str, Any]:
        if not isinstance(spec, dict):
            raise PinError("Pick a time.")
        if "at" in spec:
            if not _HHMM.match(str(spec["at"])):
                raise PinError("Times are like 19:15.")
            h, m = str(spec["at"]).split(":")
            return {"at": f"{int(h):02d}:{m}"}
        if spec.get("sun") in ("sunrise", "sunset"):
            try:
                offset = int(spec.get("offset") or 0)
            except (TypeError, ValueError):
                raise PinError("The offset is a number of minutes.") from None
            if not -240 <= offset <= 240:
                raise PinError("Keep the offset within 4 hours of sunrise or sunset.")
            return {"sun": spec["sun"], "offset": offset}
        raise PinError("Pick a time.")

    @staticmethod
    def _days(value: Any) -> list[int]:
        try:
            days = sorted({int(d) for d in (value or [])})
        except (TypeError, ValueError):
            raise PinError("Pick days of the week.") from None
        if any(not 0 <= d <= 6 for d in days):
            raise PinError("Pick days of the week.")
        return days

    def _clean_trigger(self, t: dict[str, Any]) -> dict[str, Any]:
        kind = t.get("type")
        if kind == "sensor":
            dev = self._device(t.get("device_id"), sensor=True)
            level = t.get("level")
            if level not in SENSOR_LEVELS[dev["kind"]]:
                raise PinError(f"Pick a level {dev['name']} can reach.")
            mode = t.get("mode", "any")
            if mode not in ("any", "away"):
                raise PinError("Unknown mode.")
            return {"type": kind, "device_id": dev["device_id"], "level": level, "mode": mode}
        if kind == "camera":
            if t.get("event", "any") not in CAMERA_EVENTS:
                raise PinError("Pick what the camera detects.")
            return {"type": kind, "event": t.get("event", "any"), "camera_id": self._camera(t.get("camera_id"))}
        if kind == "person":
            return {"type": kind, "camera_id": self._camera(t.get("camera_id"))}
        if kind == "schedule":
            return {"type": kind, "time": self._clean_spec(t.get("time")), "days": self._days(t.get("days"))}
        if kind == "homeshield":
            if t.get("event") not in HOMESHIELD_EVENTS:
                raise PinError("Pick what happens in HomeShield.")
            return {"type": kind, "event": t["event"]}
        if kind == "device":
            dev = self._device(t.get("device_id"))
            if t.get("value", "any") not in ("on", "off", "any"):
                raise PinError("Pick on, off or any change.")
            return {"type": kind, "device_id": dev["device_id"], "value": t.get("value", "any")}
        raise PinError("Unknown trigger.")

    def _clean_condition(self, c: dict[str, Any]) -> dict[str, Any]:
        kind = c.get("type")
        if kind == "time":
            return {"type": kind, "from": self._clean_spec(c.get("from")), "to": self._clean_spec(c.get("to"))}
        if kind == "days":
            days = self._days(c.get("days"))
            if not days:
                raise PinError("Pick at least one day.")
            return {"type": kind, "days": days}
        if kind == "mode":
            if c.get("mode") not in ("home", "away"):
                raise PinError("Pick Home or Away.")
            return {"type": kind, "mode": c["mode"]}
        if kind == "device":
            dev = self._device(c.get("device_id"))
            if c.get("value") not in ("on", "off"):
                raise PinError("Pick on or off.")
            return {"type": kind, "device_id": dev["device_id"], "value": c["value"]}
        if kind == "sensor":
            dev = self._device(c.get("device_id"), sensor=True)
            if c.get("level") not in ("normal", "alert") + SENSOR_LEVELS[dev["kind"]]:
                raise PinError("Pick a level.")
            return {"type": kind, "device_id": dev["device_id"], "level": c["level"]}
        if kind == "monitoring":
            return {"type": kind, "on": bool(c.get("on"))}
        raise PinError("Unknown condition.")

    def _clean_step(self, s: dict[str, Any]) -> dict[str, Any]:
        kind = s.get("type")
        if kind == "device":
            dev = self._device(s.get("device_id"))
            return {"type": kind, "device_id": dev["device_id"],
                    "value": normalise_value(dev["kind"], s.get("value"))}
        if kind == "group":
            if s.get("kind") not in GROUPS:
                raise PinError("Pick what kind of device.")
            if s.get("value") not in ("on", "off"):
                raise PinError("Pick on or off.")
            room = str(s.get("room") or "").strip()[:40] or None
            return {"type": kind, "kind": s["kind"], "room": room, "value": s["value"]}
        if kind == "scene":
            try:
                scene = self.store.scene(int(s.get("scene_id")))
            except (TypeError, ValueError):
                scene = None
            if scene is None:
                raise PinError("Pick a scene.")
            return {"type": kind, "scene_id": scene["scene_id"]}
        if kind == "notify":
            text = str(s.get("text") or "").strip()
            if len(text) > 200:
                raise PinError("Keep the message under 200 characters.")
            return {"type": kind, "text": text} if text else {"type": kind}
        if kind in ("buzzer", "incident"):
            return {"type": kind}
        if kind == "wait":
            try:
                seconds = int(s.get("seconds"))
            except (TypeError, ValueError):
                raise PinError("Wait for a number of seconds or minutes.") from None
            if not 5 <= seconds <= MAX_WAIT_S:
                raise PinError("Wait between 5 seconds and 6 hours.")
            return {"type": kind, "seconds": seconds}
        if kind == "mode":
            if s.get("mode") not in ("home", "away"):
                raise PinError("Pick Home or Away.")
            return {"type": kind, "mode": s["mode"]}
        if kind == "monitoring":
            return {"type": kind, "on": bool(s.get("on"))}
        raise PinError("Unknown action.")

    @staticmethod
    def _clean_options(o: dict[str, Any]) -> dict[str, Any]:
        try:
            cooldown = int(o.get("cooldown_min", 0) or 0)
        except (TypeError, ValueError):
            raise PinError("'Wait before running again' is a number of minutes.") from None
        if not 0 <= cooldown <= 1440:
            raise PinError("'Wait before running again' must be between 0 and 1440 minutes.")
        return {"cooldown_min": cooldown, "run_when_stopped": bool(o.get("run_when_stopped", True)),
                "retry": bool(o.get("retry", True))}

    # ---- words for the dashboard ----------------------------------------------------------

    def _time_text(self, spec: dict[str, Any]) -> str:
        if "at" in spec:
            return spec["at"]
        off = int(spec.get("offset") or 0)
        sun = spec.get("sun", "sunset")
        if not off:
            return sun
        return f"{abs(off)} min {'after' if off > 0 else 'before'} {sun}"

    def _days_text(self, days: list[int]) -> str:
        if not days or len(days) == 7:
            return "Every day"
        if days == [0, 1, 2, 3, 4]:
            return "Weekdays"
        if days == [5, 6]:
            return "Weekends"
        return ", ".join(DAY_NAMES[d] for d in days)

    def _where(self, dev: Optional[dict[str, Any]]) -> str:
        return f"{dev['room'] or '—'} · {dev['node_id']}" if dev else "Removed"

    def _state_word(self, dev: Optional[dict[str, Any]], on: bool) -> str:
        kind = (dev or {}).get("kind")
        if kind == "lock":
            return "unlocked" if on else "locked"
        if kind == "valve":
            return "open" if on else "closed"
        return "on" if on else "off"

    def _describe_trigger(self, t: dict[str, Any]) -> dict[str, str]:
        kind = t.get("type")
        if kind == "sensor":
            dev = self.store.device(t.get("device_id"))
            if dev is None:
                return {"tag": "SENSOR", "title": "A removed sensor", "sub": ""}
            level = t.get("level")
            if dev["kind"] in SERIES:
                cfg = self.sensors.config(dev) if self.sensors else {}
                limit = cfg.get("danger" if level == "danger" else "warn")
                unit = SERIES[dev["kind"]][1]
                title = (f"{dev['name']} over {round(limit) if unit == 'ppm' else limit} {unit}"
                         if limit is not None else f"{dev['name']} reaches {level}")
            else:
                title = {"flame": f"{dev['name']} sees a flame", "water": f"{dev['name']} gets wet",
                         "motion": f"{dev['name']} senses motion"}[dev["kind"]]
            if t.get("mode") == "away":
                title += " while Away"
            return {"tag": "SENSOR", "title": title, "sub": self._where(dev)}
        if kind == "camera":
            cam = self._cameras().get(t.get("camera_id")) if t.get("camera_id") is not None else None
            sub = (cam or {}).get("name") or ("Any camera" if t.get("camera_id") is None else "A removed camera")
            if t.get("event") == "sos":
                sub = "Any SOS button"
            return {"tag": "CAMERA", "title": CAMERA_EVENTS.get(t.get("event", "any"), "Camera alert"), "sub": sub}
        if kind == "person":
            cam = self._cameras().get(t.get("camera_id")) if t.get("camera_id") is not None else None
            return {"tag": "CAMERA", "title": "Person seen",
                    "sub": (cam or {}).get("name") or ("Any camera" if t.get("camera_id") is None else "A removed camera")}
        if kind == "schedule":
            spec = t.get("time") or {}
            sub = self._days_text(t.get("days") or [])
            if "sun" in spec:
                m = self._spec_minute(spec, dt.date.fromtimestamp(self._clock()))
                if m is not None:
                    sub += f" · today {m // 60:02d}:{m % 60:02d}"
            return {"tag": "SCHEDULE", "title": self._time_text(spec).capitalize(), "sub": sub}
        if kind == "homeshield":
            sub = {"monitoring_started": "Start monitoring button or an automation",
                   "monitoring_stopped": "Stop monitoring button or an automation"}.get(
                t.get("event"), "Top bar switch or an automation")
            return {"tag": "HOMESHIELD", "title": HOMESHIELD_EVENTS.get(t.get("event"), "?"), "sub": sub}
        if kind == "device":
            dev = self.store.device(t.get("device_id"))
            name = dev["name"] if dev else "A removed device"
            v = t.get("value", "any")
            title = f"{name} switched" if v == "any" else f"{name} {self._state_word(dev, v == 'on')}"
            return {"tag": "DEVICE", "title": title, "sub": self._where(dev)}
        return {"tag": "?", "title": "Unknown trigger", "sub": ""}

    def _describe_condition(self, c: dict[str, Any]) -> dict[str, str]:
        kind = c.get("type")
        if kind == "time":
            return {"tag": "TIME", "title": f"Between {self._time_text(c.get('from') or {})} and "
                                            f"{self._time_text(c.get('to') or {})}", "sub": "Any day"}
        if kind == "days":
            return {"tag": "DAYS", "title": self._days_text(c.get("days") or []), "sub": "Days of the week"}
        if kind == "mode":
            return {"tag": "HOMESHIELD", "title": f"HomeShield is {c.get('mode', '').capitalize()}",
                    "sub": "Top bar switch"}
        if kind == "device":
            dev = self.store.device(c.get("device_id"))
            name = dev["name"] if dev else "A removed device"
            return {"tag": "DEVICE", "title": f"{name} is {self._state_word(dev, c.get('value') == 'on')}",
                    "sub": self._where(dev)}
        if kind == "sensor":
            dev = self.store.device(c.get("device_id"))
            name = dev["name"] if dev else "A removed sensor"
            level = c.get("level")
            word = "raising an alert" if level == "alert" else f"at {level}"
            return {"tag": "SENSOR", "title": f"{name} is {word}", "sub": self._where(dev)}
        if kind == "monitoring":
            return {"tag": "HOMESHIELD", "title": f"Monitoring is {'on' if c.get('on') else 'stopped'}",
                    "sub": "Camera monitoring"}
        return {"tag": "?", "title": "Unknown condition", "sub": ""}

    def _describe_step(self, s: dict[str, Any]) -> dict[str, str]:
        kind = s.get("type")
        if kind == "device":
            dev = self.store.device(s.get("device_id"))
            if dev is None:
                return {"tag": "DEVICE", "title": "A removed device", "sub": ""}
            v, name = str(s.get("value")), dev["name"]
            if dev["kind"] == "lock":
                title = f"{'Unlock' if v == '1' else 'Lock'} {name}"
            elif dev["kind"] == "valve":
                title = f"{'Open' if v == '1' else 'Close'} {name}"
            elif dev["kind"] == "dimmer" and v not in ("0", "100"):
                title = f"Set {name} to {v} %"
            else:
                title = f"Turn {'off' if v == '0' else 'on'} {name}"
            return {"tag": "DEVICE", "title": title, "sub": self._where(dev)}
        if kind == "group":
            g = s.get("kind")
            on = s.get("value") == "on"
            verb = ({"lock": "Unlock" if on else "Lock", "valve": "Open" if on else "Close"}.get(g)
                    or ("Turn on" if on else "Turn off"))
            room = s.get("room")
            sub = ("Where it happened" if room == TRIGGER_ROOM else room) or "All rooms"
            return {"tag": "GROUP", "title": f"{verb} {GROUP_NAMES.get(g, '?')}", "sub": sub}
        if kind == "scene":
            scene = self.store.scene(s.get("scene_id")) or {}
            return {"tag": "SCENE", "title": f"Run scene “{scene.get('name', 'removed')}”",
                    "sub": scene.get("description") or ""}
        if kind == "notify":
            return {"tag": "NOTIFY", "title": "Notify",
                    "sub": s.get("text") or "Dashboard and WhatsApp, saying what happened"}
        if kind == "buzzer":
            return {"tag": "BUZZER", "title": "Sound the alarm buzzer", "sub": "Until someone silences it"}
        if kind == "incident":
            return {"tag": "INCIDENT", "title": "Open an incident", "sub": "With the sensor's reading"}
        if kind == "wait":
            return {"tag": "WAIT", "title": f"Wait {_duration(int(s.get('seconds') or 0))}",
                    "sub": "Then the next steps"}
        if kind == "mode":
            return {"tag": "HOMESHIELD", "title": f"Switch to {s.get('mode', '').capitalize()}",
                    "sub": "Away: motion sensors raise the alarm"}
        if kind == "monitoring":
            return {"tag": "HOMESHIELD", "title": "Start monitoring" if s.get("on") else "Stop monitoring",
                    "sub": "Camera detection"}
        return {"tag": "?", "title": "Unknown action", "sub": ""}

    def _short(self, text: str) -> str:
        return text[0].lower() + text[1:] if text else text

    def rule_view(self, rule: dict[str, Any]) -> dict[str, Any]:
        triggers = [self._describe_trigger(t) for t in rule["triggers"]]
        conditions = [self._describe_condition(c) for c in rule["conditions"]]
        steps = [self._describe_step(s) for s in rule["actions"]]
        first = rule["triggers"][0] if rule["triggers"] else {}
        icon = first.get("type", "device")
        if icon == "sensor":
            dev = self.store.device(first.get("device_id"))
            icon = dev["kind"] if dev else "gas"
        summary = (triggers[0]["title"] if triggers else "No trigger") + (
            f" (+{len(triggers) - 1})" if len(triggers) > 1 else "")
        if steps:
            summary += " → " + ", ".join(self._short(s["title"]) for s in steps[:2]) + (
                f" +{len(steps) - 2}" if len(steps) > 2 else "")
        return {"rule_id": rule["rule_id"], "name": rule["name"], "enabled": rule["enabled"],
                "origin": rule["origin"], "triggers": rule["triggers"],
                "conditions": rule["conditions"], "steps": rule["actions"],
                "options": {**DEFAULT_OPTIONS, **(rule.get("options") or {})},
                "trigger_views": triggers, "condition_views": conditions, "step_views": steps,
                "summary": summary, "icon": icon, "last_fired_at": rule.get("last_fired_at"),
                "fire_count": rule.get("fire_count", 0), "changed_by": rule.get("changed_by"),
                "changed_at": rule.get("changed_at"), "runs": self.store.runs(rule["rule_id"], 5)}

    def describe(self, data: dict[str, Any]) -> dict[str, Any]:
        """Words for an automation being edited, before it is saved."""
        parts = {k: [p for p in (data.get(k) or []) if isinstance(p, dict)]
                 for k in ("triggers", "conditions", "steps")}
        return {"trigger_views": [self._describe_trigger(t) for t in parts["triggers"]],
                "condition_views": [self._describe_condition(c) for c in parts["conditions"]],
                "step_views": [self._describe_step(s) for s in parts["steps"]]}

    def catalogue(self) -> dict[str, Any]:
        devices = self.store.devices()
        return {
            "outputs": [{"device_id": d["device_id"], "name": d["name"], "room": d["room"],
                         "kind": d["kind"], "type": KINDS[d["kind"]]["type"]}
                        for d in devices if KINDS.get(d["kind"], {}).get("type") in ("out", "pwm")],
            "sensors": [{"device_id": d["device_id"], "name": d["name"], "room": d["room"],
                         "kind": d["kind"], "levels": list(SENSOR_LEVELS[d["kind"]])}
                        for d in devices if d["kind"] in SENSOR_KINDS],
            "cameras": [{"camera_id": cid, "name": c.get("name"), "location": c.get("location")}
                        for cid, c in sorted(self._cameras().items())],
            "scenes": [{"scene_id": s["scene_id"], "name": s["name"]} for s in self.store.scenes()],
            "rooms": sorted({d["room"] for d in devices if d["room"]}),
            "camera_events": CAMERA_EVENTS, "homeshield_events": HOMESHIELD_EVENTS,
            "groups": GROUP_NAMES,
        }

    def automations_view(self) -> dict[str, Any]:
        rules = [self.rule_view(r) for r in self.store.rules()]
        return {"rules": rules, "total": len(rules), "active": sum(r["enabled"] for r in rules),
                "catalogue": self.catalogue(), "sun": self.sun_info(),
                "whatsapp": self._notifier is not None and self._notifier.ready()}

    def _changed(self) -> None:
        self.hub._notify("automations")

    # ---- starters -------------------------------------------------------------------------

    def start(self) -> None:
        """Add the starter automations once, switched off."""
        if self.settings.get("automations_seeded"):
            return
        devices = self.store.devices()

        def near_door() -> Optional[str]:
            rooms = sorted({d["room"] for d in devices if d["kind"] in ("light", "dimmer")})
            return next((r for r in rooms if re.search(r"porch|entr|front|door|gate|garage", r, re.I)), None)

        door_room = near_door()
        flames = [d for d in devices if d["kind"] == "flame"]
        dimmer = next((d for d in devices if d["kind"] == "dimmer"), None)
        evening = [{"type": "group", "kind": "light", "room": door_room, "value": "on"}]
        if dimmer is not None:
            evening.append({"type": "device", "device_id": dimmer["device_id"], "value": "40"})
        starters = [
            ("Fall → lights on", [{"type": "camera", "event": "fall", "camera_id": None}], [],
             [{"type": "group", "kind": "light", "room": None, "value": "on"},
              {"type": "notify"}], {"cooldown_min": 5}),
            ("Fire detected", [{"type": "camera", "event": "fire", "camera_id": None}] +
             [{"type": "sensor", "device_id": f["device_id"], "level": "active", "mode": "any"}
              for f in flames], [],
             [{"type": "buzzer"}, {"type": "group", "kind": "valve", "room": None, "value": "off"},
              {"type": "notify"}], {"cooldown_min": 5}),
            ("Night visitor", [{"type": "person", "camera_id": None}],
             [{"type": "time", "from": {"at": "22:00"}, "to": {"at": "06:00"}}],
             [{"type": "group", "kind": "light", "room": door_room, "value": "on"},
              {"type": "wait", "seconds": 300},
              {"type": "group", "kind": "light", "room": door_room, "value": "off"}], {"cooldown_min": 10}),
            ("Unknown face while away", [{"type": "camera", "event": "intruder", "camera_id": None}],
             [{"type": "mode", "mode": "away"}], [{"type": "buzzer"}, {"type": "notify"}],
             {"cooldown_min": 5}),
            ("Arm & leave", [{"type": "homeshield", "event": "mode_away"},
                             {"type": "homeshield", "event": "monitoring_started"}], [],
             [{"type": "group", "kind": "light", "room": None, "value": "off"},
              {"type": "group", "kind": "socket", "room": None, "value": "off"},
              {"type": "group", "kind": "lock", "room": None, "value": "off"}], {"cooldown_min": 5}),
            ("Evening lights", [{"type": "schedule", "time": {"sun": "sunset", "offset": 0}, "days": []}],
             [], evening, {}),
        ]
        for name, triggers, conditions, steps, options in starters:
            self.store.save_rule(None, name=name, triggers=triggers, conditions=conditions,
                                 actions=steps, options={**DEFAULT_OPTIONS, **options},
                                 origin="starter", enabled=False)
        self.settings.update({"automations_seeded": True})
