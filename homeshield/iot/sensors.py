"""Sensors on ESP32 nodes: readings turned into levels, history and alerts,
and the automatic responses that go with them.

Readings arrive as pin states (IotHub._on_state):

    gas      {"mv": 812}: millivolts at the pin (firmware 1.2+), or 0-4095 raw before
    climate  {"t": 27.5, "h": 66}
    flame, water, motion   1 = active, 0 = clear

Gas: an MQ sensor's resistance Rs falls as gas rises. HomeShield works out Rs/Ro
from the module's output voltage, with Ro measured in clean air (Calibrate),
and reads ppm off the datasheet curve: LPG for an MQ-2, methane for an MQ-5. Until someone calibrates, the
cleanest minute seen so far stands in for clean air (the "auto baseline").
MQ-2 sensors are rough: the numbers are estimates, good for warnings.

Levels: an analog sensor is normal, warning or danger. It must stay at or
above a level for `hold` seconds to enter it, and below 90 % of it for as
long to leave. A digital one is active once it has been for `hold` seconds.
Reaching a level runs that level's rules (the rules table). The Sensors page
edits each sensor's own rules as a checklist; the Automations page will
edit rules in general.

An episode lasts from leaving normal until back to normal. Its alert (the
dashboard banner) stays up until someone acknowledges it, which also
stops the buzzer and closes the incident it opened.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from typing import Any, Callable, Optional

from .kinds import KINDS, PinError, describe

log = logging.getLogger(__name__)

SENSOR_KINDS = ("gas", "climate", "flame", "water", "motion")
DIGITAL = ("flame", "water", "motion")

# Datasheet curves: Rs/Ro in clean air, and ppm = a * (Rs/Ro) ** b for the gas shown.
CURVES = {
    "mq2": {"clean": 9.83, "a": 574.25, "b": -2.222, "gas": "LPG", "short": "MQ-2"},
    "mq5": {"clean": 6.5, "a": 177.65, "b": -2.56, "gas": "Methane", "short": "MQ-5"},
}
CLEAN_AIR_RATIO = CURVES["mq2"]["clean"]
# Methane's lower explosive limit is 5 % (50,000 ppm): warn at 1,000, alarm at 10 % of it.
MODEL_DEFAULTS = {"mq2": {"warn": 300.0, "danger": 1000.0}, "mq5": {"warn": 1000.0, "danger": 5000.0}}
SUPPLY_V = 5.0                    # the module's heater and divider run from 5 V
MAX_PPM = 10000.0
LEGACY_FULL_MV = 3300.0           # firmware before 1.2 sent analogRead() 0-4095
DIVIDERS = {1.0: "None (AO straight to the pin)", 0.5: "10 kΩ + 10 kΩ",
            0.667: "10 kΩ + 20 kΩ"}

HYSTERESIS = 0.9
WARMUP_S = 90.0                   # an MQ-2 reads high until its heater is warm
CLEAR_HOLD_S = 5.0                # a digital sensor must be clear this long to end an alert
RECENT_S = 1800.0
CALIBRATE_WINDOW_S = 30.0
HISTORY_DAYS = 30

SERIES = {"gas": ("ppm", "ppm"), "climate": ("t", "°C")}
LEVELS = {"gas": ("warning", "danger"), "climate": ("warning", "danger"),
          "flame": ("active",), "water": ("active",), "motion": ("active",)}
RANK = {"normal": 0, "warning": 1, "active": 1, "danger": 2}

DEFAULTS: dict[str, dict[str, Any]] = {
    "gas": {"warn": 300.0, "danger": 1000.0, "hold": 10, "divider": 0.5},
    "climate": {"warn": None, "danger": None, "hold": 60},
    "flame": {"hold": 2},
    "water": {"hold": 10},
    "motion": {"hold": 0},
}
LIMITS = {"gas": (1.0, MAX_PPM), "climate": (-40.0, 125.0)}

SHORT = {"gas": "MQ-2", "climate": "DHT", "flame": "flame sensor", "water": "leak probe",
         "motion": "PIR"}
TITLES = {("gas", "warning"): "Gas warning", ("gas", "danger"): "Gas danger",
          ("climate", "warning"): "Temperature warning",
          ("climate", "danger"): "Temperature danger",
          ("flame", "active"): "Flame detected", ("water", "active"): "Water leak",
          ("motion", "active"): "Motion detected"}
EVENT_TYPES = {("gas", "warning"): "gas_warning", ("gas", "danger"): "gas_danger",
               ("climate", "warning"): "heat_warning", ("climate", "danger"): "heat_danger",
               ("flame", "active"): "flame_detected", ("water", "active"): "water_leak",
               ("motion", "active"): "motion_detected"}
ALARM_KINDS = {"gas": "gas", "climate": "heat", "flame": "fire", "water": "water",
               "motion": "intruder"}

# What a sensor suggests doing to the outputs in its room, per output kind.
SUGGEST: dict[str, dict[str, str]] = {
    "gas": {"fan": "1", "fan_pwm": "100", "valve": "0", "light": "0", "socket": "0", "relay": "0",
            "dimmer": "0"},
    "flame": {"valve": "0", "socket": "0", "relay": "0", "fan": "0", "fan_pwm": "0", "light": "1"},
    "water": {"valve": "0", "socket": "0", "relay": "0"},
    "motion": {"light": "1", "dimmer": "100", "lock": "0"},
    "climate": {"fan": "1", "fan_pwm": "100"},
}
# The checklist a new sensor starts with (device steps: output kinds in its room).
DEFAULT_RESPONSE: dict[str, dict[str, list[str]]] = {
    "gas": {"warning": ["notify", "fan"], "danger": ["buzzer", "incident", "valve"]},
    "flame": {"active": ["buzzer", "incident", "valve"]},
    "water": {"active": ["notify"]},
    "motion": {"active": ["buzzer", "incident"]},
    "climate": {"warning": ["notify"], "danger": ["notify", "buzzer"]},
}
STEP_TYPES = ("device", "notify", "buzzer", "incident")


def mv_from_state(raw: str) -> Optional[float]:
    """{"mv": 812} from firmware 1.2+, a plain 0-4095 count from older ones."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw.startswith("{"):
        try:
            mv = json.loads(raw).get("mv")
            return float(mv) if mv is not None else None
        except (ValueError, AttributeError, TypeError):
            return None
    try:
        return float(raw) * LEGACY_FULL_MV / 4095.0
    except ValueError:
        return None


def gas_ppm(volts: float, v0: float, model: str = "mq2") -> float:
    """ppm for the module's output `volts`, given `v0` in clean air."""
    curve = CURVES.get(model, CURVES["mq2"])
    v = min(max(volts, 0.01), SUPPLY_V - 0.01)
    c = min(max(v0, 0.01), SUPPLY_V - 0.01)
    ratio = curve["clean"] * ((SUPPLY_V - v) / v) / ((SUPPLY_V - c) / c)
    return max(0.0, min(MAX_PPM, curve["a"] * ratio ** curve["b"]))


def lpg_ppm(volts: float, v0: float) -> float:
    """An MQ-2's LPG ppm."""
    return gas_ppm(volts, v0, "mq2")


def gas_model(dev: dict[str, Any]) -> str:
    return (dev.get("options") or {}).get("model", "mq2") if dev["kind"] == "gas" else ""


def short_name(dev: dict[str, Any]) -> str:
    """MQ-2, MQ-5, PIR...: what the sensor is, briefly."""
    return CURVES[gas_model(dev)]["short"] if dev["kind"] == "gas" else SHORT[dev["kind"]]


def _fmt(value: Optional[float], unit: str) -> str:
    if value is None:
        return "—"
    if unit == "ppm":
        return f"{round(value):,} ppm"
    if unit == "°C":
        return f"{value:.1f} °C"
    return f"{value:g} {unit}".strip()


def _clock_text(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


class SensorEngine:
    def __init__(self, hub, *, settings, alarm=None,
                 publish_event: Optional[Callable[..., None]] = None,
                 notifier=None, resolve_incident: Optional[Callable[[int, str], None]] = None,
                 clock: Callable[[], float] = time.time):
        self.hub = hub
        self.store = hub.store
        self.settings = settings
        self.alarm = alarm
        self._publish_event = publish_event
        self._notifier = notifier
        self._resolve_incident = resolve_incident
        self._clock = clock
        self._lock = threading.RLock()
        self._w: dict[int, dict[str, Any]] = {}            # device id -> live state
        self._recent: dict[int, deque] = {}                # device id -> (ts, value, mv)
        # (id, series) -> [minute, sum, n, min, max, warm, mv sum, normal, mv min, mv max]
        self._acc: dict[tuple[int, str], list] = {}
        self._flush: list[tuple] = []
        self._cfg: dict[int, dict[str, Any]] = {}
        self._pruned_at = 0.0
        self.automations = None                     # automations.AutomationEngine attaches itself
        hub.sensors = self

    # ---- settings ----------------------------------------------------------------

    def _saved(self, device_id: int) -> Optional[dict[str, Any]]:
        with self._lock:
            if device_id not in self._cfg:
                self._cfg[device_id] = self.store.sensor_settings(device_id)
            cfg = self._cfg[device_id]
            return dict(cfg) if cfg is not None else None

    def config(self, dev: dict[str, Any]) -> dict[str, Any]:
        base = {**DEFAULTS.get(dev["kind"], {}), **MODEL_DEFAULTS.get(gas_model(dev), {})}
        return {**base, **(self._saved(dev["device_id"]) or {})}

    def _save(self, device_id: int, changes: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            data = {**(self._saved(device_id) or {}), **changes}
            self.store.save_sensor_settings(device_id, data)
            self._cfg[device_id] = data
        return data

    def update_settings(self, device_id: int, data: dict[str, Any], by: str) -> dict[str, Any]:
        dev = self._sensor(device_id)
        kind, changes = dev["kind"], {}
        cfg = self.config(dev)
        if kind in SERIES:
            lo, hi = LIMITS[kind]
            for key in ("warn", "danger"):
                if key in data:
                    v = data[key]
                    if v in (None, ""):
                        changes[key] = None
                        continue
                    try:
                        v = float(v)
                    except (TypeError, ValueError):
                        raise PinError("Levels must be numbers.") from None
                    if not lo <= v <= hi:
                        raise PinError(f"Levels must be between {lo:g} and {hi:g} {SERIES[kind][1]}.")
                    changes[key] = v
            warn = changes.get("warn", cfg.get("warn"))
            danger = changes.get("danger", cfg.get("danger"))
            if warn is not None and danger is not None and warn >= danger:
                raise PinError("The warning level must be below the danger level.")
        if "hold" in data:
            try:
                hold = int(data["hold"])
            except (TypeError, ValueError):
                raise PinError("'Must stay above' is a number of seconds.") from None
            if not 0 <= hold <= 600:
                raise PinError("'Must stay above' must be between 0 and 600 seconds.")
            changes["hold"] = hold
        if "divider" in data and kind == "gas":
            try:
                divider = float(data["divider"])
            except (TypeError, ValueError):
                divider = -1.0
            match = next((d for d in DIVIDERS if abs(d - divider) < 0.01), None)
            if match is None:
                raise PinError("Pick how the MQ-2's output is wired to the pin.")
            changes["divider"] = match
        if "every" in data and KINDS[kind]["options"].get("every") is not None:
            self.hub.update_device(device_id, {"options": {"every": data["every"]}})
        if changes:
            self._save(device_id, changes)
            self.store.log(f"{dev['name']}: settings changed", by=by, device_id=device_id,
                           node_id=dev["node_id"])
            self._changed()
        return self.sensor_view(self.store.device(device_id))

    def calibrate(self, device_id: int, by: str) -> dict[str, Any]:
        dev = self._sensor(device_id)
        if dev["kind"] != "gas":
            raise PinError("Only the gas sensor is calibrated.")
        if not self.hub.node_online(dev["node_id"]):
            raise PinError(f"{dev['node_id']} is offline, so there is nothing to calibrate with.")
        up = self.hub.node_uptime(dev["node_id"])
        if up is not None and up < WARMUP_S:
            raise PinError(f"The sensor is still warming up. Try again in {int(WARMUP_S - up) + 1} s.")
        now = self._clock()
        with self._lock:
            mvs = [mv for ts, _, mv in self._recent.get(device_id, ())
                   if mv is not None and now - ts <= CALIBRATE_WINDOW_S]
        if len(mvs) < 3:
            raise PinError("Not enough readings in the last 30 seconds yet. Wait a moment and try again.")
        divider = self.config(dev)["divider"]
        v0 = sum(mvs) / len(mvs) / 1000.0 / divider
        self._save(device_id, {"cal": {"v0": round(v0, 4), "at": now, "by": by}})
        self.store.log(f"{dev['name']} calibrated in clean air", by=by, device_id=device_id,
                       node_id=dev["node_id"])
        self._changed()
        return self.sensor_view(self.store.device(device_id))

    def _sensor(self, device_id: int) -> dict[str, Any]:
        dev = self.store.device(device_id)
        if dev is None or dev["kind"] not in SENSOR_KINDS:
            raise PinError("That sensor no longer exists.")
        return dev

    # ---- Home / Away -------------------------------------------------------------

    def mode(self) -> dict[str, Any]:
        return {"mode": self.settings.get("home_mode", "home") or "home",
                "by": self.settings.get("home_mode_by", "") or None,
                "at": self.settings.get("home_mode_at") or None}

    def set_mode(self, mode: str, by: str) -> dict[str, Any]:
        if mode not in ("home", "away"):
            raise PinError("Choose Home or Away.")
        if mode == self.mode()["mode"]:
            return self.mode()
        self.settings.update({"home_mode": mode, "home_mode_by": by,
                              "home_mode_at": self._clock()})
        self.store.log("Away: motion sensors now raise the alarm" if mode == "away"
                       else "Home: motion is only logged", by=by)
        self.hub._notify("activity")
        self._changed()
        if self.automations is not None:
            self.automations.on_mode(mode, by)
        return self.mode()

    # ---- readings ----------------------------------------------------------------

    def on_state(self, dev: dict[str, Any], raw: str) -> None:
        """A sensor reported (from the MQTT thread)."""
        kind = dev["kind"]
        if kind not in SENSOR_KINDS:
            return
        now = self._clock()
        cfg = self.config(dev)
        did = dev["device_id"]
        warming = kind == "gas" and self._warming(dev["node_id"])
        mv = None
        values: dict[str, float] = {}
        if kind == "gas":
            mv = mv_from_state(raw)
            if mv is None:
                return
            v0, _ = self._baseline(did, cfg, extra_mv=None if warming else mv)
            if v0 is not None:
                values["ppm"] = gas_ppm(mv / 1000.0 / cfg["divider"], v0, gas_model(dev))
        elif kind == "climate":
            try:
                d = json.loads(raw)
                values = {"t": float(d["t"]), "h": float(d["h"])}
            except (ValueError, KeyError, TypeError):
                return
        else:
            if raw not in ("0", "1"):
                return
            values = {"on": float(raw)}
        primary = values.get(SERIES[kind][0] if kind in SERIES else "on")
        work: list[Callable[[], None]] = []
        with self._lock:
            w = self._w.setdefault(did, {"level": "normal", "cand": None, "cand_since": None,
                                         "episode": None, "last": None})
            was = w.get("value")
            w.update(value=primary, at=now, mv=mv, warming=warming, h=values.get("h"))
            rec = self._recent.setdefault(did, deque())
            rec.append((now, primary, mv))
            while rec and now - rec[0][0] > RECENT_S:
                rec.popleft()
            for series, v in values.items():
                if series == "on":
                    if v == 1 and was != 1:
                        self._add(did, "on", now, 1.0, True)
                        w["last_active"] = now
                else:
                    self._add(did, series, now, v, not warming, mv=mv if series == "ppm" else None,
                              normal=w["level"] == "normal")
            if primary is not None and not warming:
                work = self._evaluate(dev, w, primary, cfg, now)
        for fn in work:
            fn()
        if work:
            self._changed()

    def _warming(self, node_id: str) -> bool:
        up = self.hub.node_uptime(node_id)
        return up is not None and up < WARMUP_S

    def _baseline(self, did: int, cfg: dict[str, Any], extra_mv: Optional[float] = None
                  ) -> tuple[Optional[float], Optional[str]]:
        """Clean-air output voltage, and where it came from."""
        cal = cfg.get("cal") or {}
        if cal.get("v0"):
            return float(cal["v0"]), "calibrated"
        if cfg.get("auto_v0"):
            v = float(cfg["auto_v0"])
            if extra_mv is not None:
                v = min(v, extra_mv / 1000.0 / cfg["divider"])
            return v, "auto"
        with self._lock:
            mvs = [mv for _, _, mv in self._recent.get(did, ()) if mv is not None]
        if extra_mv is not None:
            mvs.append(extra_mv)
        if not mvs:
            return None, None
        return min(mvs) / 1000.0 / cfg["divider"], "auto"

    def _add(self, did: int, series: str, now: float, value: float, warm: bool,
             mv: Optional[float] = None, normal: bool = True) -> None:
        minute = int(now // 60 * 60)
        key = (did, series)
        acc = self._acc.get(key)
        if acc is not None and acc[0] != minute:
            self._flush.append((did, series, acc))
            acc = None
        if acc is None:
            acc = self._acc[key] = [minute, 0.0, 0, value, value, warm, 0.0, normal,
                                    mv if mv is not None else 0.0, mv if mv is not None else 0.0]
        acc[1] += value
        acc[2] += 1
        acc[3] = min(acc[3], value)
        acc[4] = max(acc[4], value)
        acc[5] = acc[5] and warm
        acc[6] += mv or 0.0
        acc[7] = acc[7] and normal
        if mv is not None:
            acc[8], acc[9] = min(acc[8], mv), max(acc[9], mv)

    # ---- levels ------------------------------------------------------------------

    def _target(self, kind: str, value: float, current: str, cfg: dict[str, Any]) -> str:
        if kind in DIGITAL:
            return "active" if value >= 1 else "normal"

        def at_or_above(threshold: Optional[float], level: str) -> bool:
            if threshold is None:
                return False
            # Leaving a level needs a clear drop below it, so a reading
            # wobbling on the line doesn't keep raising and clearing alerts.
            return value >= (threshold * HYSTERESIS if RANK[current] >= RANK[level] else threshold)

        if at_or_above(cfg.get("danger"), "danger"):
            return "danger"
        if at_or_above(cfg.get("warn"), "warning"):
            return "warning"
        return "normal"

    def _evaluate(self, dev: dict[str, Any], w: dict[str, Any], value: float,
                  cfg: dict[str, Any], now: float) -> list[Callable[[], None]]:
        """Decide whether the level changes; returns the work to do outside the lock."""
        current = w["level"]
        target = self._target(dev["kind"], value, current, cfg)
        if target == current:
            w["cand"] = w["cand_since"] = None
            return []
        if w["cand"] != target:
            w["cand"], w["cand_since"] = target, now
        hold = float(cfg.get("hold") or 0)
        if dev["kind"] in DIGITAL and RANK[target] < RANK[current]:
            hold = max(hold, CLEAR_HOLD_S)
        if now - w["cand_since"] < hold:
            return []
        w["cand"] = w["cand_since"] = None
        return self._set_level(dev, w, target, value, cfg, now)

    def _set_level(self, dev: dict[str, Any], w: dict[str, Any], new: str, value: float,
                   cfg: dict[str, Any], now: float) -> list[Callable[[], None]]:
        old, w["level"] = w["level"], new
        kind, did = dev["kind"], dev["device_id"]
        work: list[Callable[[], None]] = []
        reading = self._reading_text(dev, value)
        if RANK[new] > RANK[old]:
            if w["episode"] is None:
                w["episode"] = {"started": now, "level": new, "peak": value, "fired": [],
                                "done": [], "alerting": False, "acked": None, "incident_id": None}
            ep = w["episode"]
            ep["level"] = new
            ep["acked"] = None                 # worse than before: a new alert
            todo = [lv for lv in LEVELS[kind] if RANK[lv] <= RANK[new] and lv not in ep["fired"]]
            ep["fired"].extend(todo)
            text = (f"{dev['name']}: {TITLES[(kind, new)].lower()} ({reading})" if kind in SERIES
                    else f"{dev['name']}: {TITLES[(kind, new)].lower()}")
            if kind != "motion":
                work.append(lambda: self.store.log(text, device_id=did, node_id=dev["node_id"]))
            for lv in todo:
                work.append(lambda lv=lv: self._run_rules(dev, lv, value, cfg))
        elif new == "normal":
            ep, w["episode"] = w["episode"], None
            if ep is not None:
                w["last"] = {"level": ep["level"], "started": ep["started"], "ended": now}
                if kind != "motion" or ep["alerting"]:
                    work.append(lambda: self.store.log(f"{dev['name']}: back to normal",
                                                       device_id=did, node_id=dev["node_id"]))
            if self.alarm is not None:
                work.append(lambda: self.alarm.unring(("sensor", did)))
        else:
            if w["episode"] is not None:
                w["episode"]["level"] = new
        work.append(lambda: self.hub._notify("activity"))
        return work

    def _run_rules(self, dev: dict[str, Any], level: str, value: float,
                   cfg: dict[str, Any]) -> None:
        """Hand the level to the automations engine, which runs every rule it
        triggers (this sensor's checklist rule and any automation using it)."""
        if self.automations is None:
            return
        did = dev["device_id"]

        def done(texts: list[str]) -> None:
            now = self._clock()
            with self._lock:
                ep = (self._w.get(did) or {}).get("episode")
                if ep is not None:
                    ep["alerting"] = True
                    ep["done"].extend((now, t) for t in texts)
            self._changed()

        self.automations.fire({
            "type": "sensor", "device_id": did, "level": level, "value": value,
            "room": dev["room"], "title": TITLES[(dev["kind"], level)],
            "text": (f"{dev['name']} reached {level} ({self._reading_text(dev, value)})"
                     if dev["kind"] in SERIES else f"{dev['name']}: {TITLES[(dev['kind'], level)].lower()}"),
            "alarm_kind": ALARM_KINDS[dev["kind"]], "ring_key": ("sensor", did),
            "open_incident": lambda: self._open_incident(dev, level, value),
            "message": lambda steps: self._message(dev, level, value, cfg, steps),
            "done": done,
        })

    def _whatsapp_ready(self) -> bool:
        return self._notifier is not None and self._notifier.ready()

    def _message(self, dev, level, value, cfg, done) -> str:
        title = TITLES[(dev["kind"], level)]
        when = _clock_text(self._clock())
        text = (f"HomeShield: {title} in {dev['room'] or dev['node_id']} at {when}. "
                f"{self._sentence(dev, level, value, cfg)}")
        steps = [d for d in done if not d.startswith(("WhatsApp", "shown"))]
        if steps:
            text += " Done automatically: " + ", ".join(steps) + "."
        return text

    def _sentence(self, dev, level, value, cfg) -> str:
        kind = dev["kind"]
        if kind in SERIES:
            key = "danger" if level == "danger" else "warn"
            return (f"{dev['name']} reads {self._reading_text(dev, value)}, above your "
                    f"{'danger' if level == 'danger' else 'warning'} level of "
                    f"{_fmt(cfg.get(key), SERIES[kind][1])}.")
        if kind == "flame":
            return f"{dev['name']} sees a flame."
        if kind == "water":
            return f"{dev['name']} is wet."
        away = " while everyone is away" if self.mode()["mode"] == "away" else ""
        return f"{dev['name']} sensed motion{away}."

    def _open_incident(self, dev, level, value) -> None:
        if self._publish_event is None:
            return
        self._publish_event(
            EVENT_TYPES[(dev["kind"], level)], dev["room"] or dev["node_id"],
            self._sentence(dev, level, value, self.config(dev)),
            {"device_id": dev["device_id"], "source": f"{dev['node_id']} · {short_name(dev)}",
             "reading": {"flame": "Flame seen", "water": "Wet", "motion": "Motion"}.get(
                 dev["kind"], self._reading_text(dev, value))})

    # ---- alerts ------------------------------------------------------------------

    def acknowledge(self, device_id: int, by: str) -> Optional[int]:
        """Returns the incident it closed, if there was one (for an undo)."""
        dev = self._sensor(device_id)
        with self._lock:
            ep = (self._w.get(device_id) or {}).get("episode")
            if ep is None or ep["acked"]:
                return None
            rang = self.alarm.ring_kind(("sensor", device_id)) if self.alarm is not None else None
            ep["acked"] = {"by": by, "at": self._clock(), "rang": rang}
            iid = ep["incident_id"]
        if self.alarm is not None:
            self.alarm.unring(("sensor", device_id))
        if iid and self._resolve_incident is not None:
            self._resolve_incident(iid, by)
        self.store.log(f"{TITLES.get((dev['kind'], ep['level']), 'Alert')} in {dev['room']} acknowledged",
                       by=by, device_id=device_id, node_id=dev["node_id"])
        self.hub._notify("activity")
        self._changed()
        return iid

    def sound(self, device_id: int, by: str) -> None:
        dev = self._sensor(device_id)
        if self.alarm is None or not self.alarm.enabled():
            raise PinError("The alarm buzzer is switched off in Settings › Alarm buzzer.")
        self.alarm.ring(("sensor", device_id), ALARM_KINDS[dev["kind"]])
        self.store.log(f"Alarm sounded for {dev['name']}", by=by, device_id=device_id,
                       node_id=dev["node_id"])
        self.hub._notify("activity")
        self._changed()

    def on_incident(self, change: str, inc: dict[str, Any]) -> None:
        """EventBus listener (and the resolve route): link sensor incidents to
        their episode, and treat closing the incident as acknowledging it."""
        did = inc.get("device_id")
        if did is None:
            return
        with self._lock:
            ep = (self._w.get(did) or {}).get("episode")
            if ep is None:
                return
            if change in ("opened", "updated", "escalated"):
                ep["incident_id"] = inc.get("incident_id")
                return
            if change == "reopened":
                # Someone undid closing it: the alert is back, and so is the buzzer if it was sounding.
                if ep["incident_id"] != inc.get("incident_id") or not ep["acked"]:
                    return
                rang = ep["acked"].get("rang")
                ep["acked"] = None
            else:
                rang = None
        if change == "reopened":
            if rang and self.alarm is not None:
                self.alarm.ring(("sensor", did), rang)
            self._changed()
            return
        with self._lock:
            if change != "resolved" or ep["incident_id"] != inc.get("incident_id") or ep["acked"]:
                return
            rang = self.alarm.ring_kind(("sensor", did)) if self.alarm is not None else None
            ep["acked"] = {"by": inc.get("resolved_by") or "someone", "at": self._clock(), "rang": rang}
        if self.alarm is not None:
            self.alarm.unring(("sensor", did))
        self._changed()

    def alerts(self) -> list[dict[str, Any]]:
        """Episodes that raised an alert nobody has acknowledged, worst first."""
        out = []
        with self._lock:
            items = [(did, dict(w), dict(w["episode"])) for did, w in self._w.items()
                     if w.get("episode") and w["episode"]["alerting"] and not w["episode"]["acked"]]
        for did, w, ep in items:
            dev = self.store.device(did)
            if dev is None:
                continue
            cfg = self.config(dev)
            kind, level = dev["kind"], ep["level"]
            value = w.get("value")
            done = [d for _, d in ep["done"]]
            first = ep["done"][0][0] if ep["done"] else ep["started"]
            text = self._sentence(dev, level, value, cfg) if value is not None else ""
            if done:
                text += f" At {_clock_text(first)}: " + ", ".join(done) + "."
            quick = []
            if kind in ("gas", "flame", "water"):
                for d in self.store.devices():
                    if d["kind"] == "valve" and d["room"] == dev["room"] and d["state"] == "1":
                        quick.append({"device_id": d["device_id"], "value": "0",
                                      "label": f"Close {d['name'].lower()}"})
            out.append({
                "device_id": did, "kind": kind, "level": level, "room": dev["room"],
                "title": f"{TITLES[(kind, level)]} · {dev['room'] or dev['node_id']}",
                "text": text, "started": ep["started"], "incident_id": ep["incident_id"],
                "quick": quick,
                "can_sound": bool(self.alarm is not None and self.alarm.enabled()
                                  and not self.alarm.ringing(("sensor", did))),
            })
        out.sort(key=lambda a: (-RANK[a["level"]], a["started"]))
        return out

    # ---- ticking -----------------------------------------------------------------

    def tick(self, now: Optional[float] = None) -> None:
        """Holds that run out without a new reading, and the minute history."""
        now = self._clock() if now is None else now
        work: list[Callable[[], None]] = []
        with self._lock:
            pending = [(did, w) for did, w in self._w.items()
                       if w.get("cand") is not None and w.get("value") is not None]
        for did, w in pending:
            dev = self.store.device(did)
            if dev is None:
                continue
            cfg = self.config(dev)
            with self._lock:
                if w.get("cand") is not None and not w.get("warming"):
                    work += self._evaluate(dev, w, w["value"], cfg, now)
        for fn in work:
            fn()
        if work:
            self._changed()
        self._flush_history(now)

    def _flush_history(self, now: float) -> None:
        minute = int(now // 60 * 60)
        with self._lock:
            for key in [k for k, acc in self._acc.items() if acc[0] < minute]:
                self._flush.append((*key, self._acc.pop(key)))
            batch, self._flush = self._flush, []
        if not batch:
            return
        rows = []
        for did, series, acc in batch:
            m, total, n, lo, hi, warm, mv_total, normal, mv_lo, mv_hi = acc
            rows.append((did, series, m, total / n if series != "on" else 1.0, lo, hi, n))
            # Clean air is a calm minute at the normal level, after the warm-up:
            # never a minute in which the reading climbed (a leak starting).
            if series == "ppm" and warm and normal and n >= 3 and mv_hi <= mv_lo * 1.15:
                self._learn_baseline(did, mv_total / n)
        try:
            self.store.add_history(rows)
        except Exception as e:
            log.warning("sensor history not saved: %s", e)
        if now - self._pruned_at > 3600:
            self._pruned_at = now
            days = int(self.settings.get("sensor_history_days", HISTORY_DAYS) or HISTORY_DAYS)
            self.store.prune_history(now - days * 86400)

    def _learn_baseline(self, did: int, mv: float) -> None:
        dev = self.store.device(did)
        if dev is None:
            return
        cfg = self.config(dev)
        if (cfg.get("cal") or {}).get("v0"):
            return
        v = mv / 1000.0 / cfg["divider"]
        if not cfg.get("auto_v0") or v < float(cfg["auto_v0"]) * 0.99:
            self._save(did, {"auto_v0": round(v, 4)})

    # ---- devices coming and going ------------------------------------------------

    def start(self) -> None:
        """Sensors mapped before this version get their default rules now,
        not on the first visit to the Sensors page."""
        for dev in self.store.devices():
            if dev["kind"] in SENSOR_KINDS:
                self.ensure_rules(dev)

    def on_device_added(self, dev: dict[str, Any]) -> None:
        if dev["kind"] in SENSOR_KINDS:
            self.ensure_rules(dev)

    def forget(self, device_id: int) -> None:
        with self._lock:
            self._w.pop(device_id, None)
            self._recent.pop(device_id, None)
            self._cfg.pop(device_id, None)
            for key in [k for k in self._acc if k[0] == device_id]:
                self._acc.pop(key)
        if self.alarm is not None:
            self.alarm.unring(("sensor", device_id))

    def ensure_rules(self, dev: dict[str, Any]) -> None:
        """A sensor's first look: give it the default checklist, once."""
        saved = self._saved(dev["device_id"])
        if saved is not None and saved.get("seeded"):
            return
        devices = self.store.devices()
        for level, keys in DEFAULT_RESPONSE.get(dev["kind"], {}).items():
            steps = []
            for key in keys:
                if key in ("notify", "buzzer", "incident"):
                    steps.append({"type": key})
                else:
                    value = SUGGEST.get(dev["kind"], {}).get(key)
                    steps += [{"type": "device", "device_id": d["device_id"], "value": value}
                              for d in devices if d["kind"] == key and d["room"] == dev["room"]
                              and value is not None]
            self._save_card_rule(dev, level, steps)
        self._save(dev["device_id"], {"seeded": True})

    @staticmethod
    def _extended(rule: dict[str, Any]) -> bool:
        """More than this page's checklist can show: extra triggers,
        conditions, or steps like waits, groups and scenes."""
        return (len(rule["triggers"]) > 1 or bool(rule["conditions"])
                or any(a.get("type") not in ("device", "notify", "buzzer", "incident")
                       for a in rule["actions"]))

    def _card_rule(self, dev: dict[str, Any], level: str) -> Optional[dict[str, Any]]:
        return next((r for r in self.store.rules() if r["origin"] == "sensor"
                     and r["trigger"].get("device_id") == dev["device_id"]
                     and r["trigger"].get("level") == level), None)

    def _save_card_rule(self, dev: dict[str, Any], level: str, steps: list[dict[str, Any]]) -> None:
        rule = self._card_rule(dev, level)
        trigger = {"type": "sensor", "device_id": dev["device_id"], "level": level,
                   "mode": "away" if dev["kind"] == "motion" else "any"}
        if rule is not None:
            trigger["mode"] = rule["trigger"].get("mode", trigger["mode"])
        if rule is not None:
            # Keep what the Automations page may have changed: name, conditions, options.
            self.store.save_rule(rule["rule_id"], name=rule["name"], triggers=[trigger] + rule["triggers"][1:],
                                 actions=steps, conditions=rule["conditions"], options=rule["options"],
                                 origin="sensor", enabled=rule["enabled"])
            return
        name = f"{dev['name']} · {level}" + (" while away" if trigger["mode"] == "away" else "")
        self.store.save_rule(None, name=name, triggers=[trigger], actions=steps, origin="sensor")

    def set_response(self, device_id: int, level: str, keys: Any, by: str) -> dict[str, Any]:
        dev = self._sensor(device_id)
        if level not in LEVELS[dev["kind"]]:
            raise PinError("Unknown level.")
        if not isinstance(keys, list):
            raise PinError("Send the list of ticked steps.")
        rule = self._card_rule(dev, level)
        if rule is not None and self._extended(rule):
            raise PinError("This rule was changed on the Automations page. Edit it there.")
        steps: list[dict[str, Any]] = []
        for key in keys:
            key = str(key)
            if key in ("notify", "buzzer", "incident"):
                if {"type": key} not in steps:
                    steps.append({"type": key})
                continue
            parts = key.split(":")
            if len(parts) != 3 or parts[0] != "device":
                raise PinError("Unknown step.")
            target = self.store.device(int(parts[1])) if parts[1].isdigit() else None
            if target is None:
                raise PinError("A device in this response no longer exists.")
            if KINDS.get(target["kind"], {}).get("type") not in ("out", "pwm"):
                raise PinError(f"{target['name']} can't be switched automatically.")
            steps.append({"type": "device", "device_id": target["device_id"], "value": parts[2]})
        self._save_card_rule(dev, level, steps)
        self._save(device_id, {"seeded": True})
        self.store.log(f"{dev['name']}: automatic response at {level} changed", by=by,
                       device_id=device_id, node_id=dev["node_id"])
        self._changed()
        return self.sensor_view(self.store.device(device_id))

    # ---- for the dashboard -------------------------------------------------------

    def _reading_text(self, dev: dict[str, Any], value: Optional[float]) -> str:
        kind = dev["kind"]
        if value is None:
            return "—"
        if kind in SERIES:
            return _fmt(value, SERIES[kind][1])
        active = value >= 1
        return {"flame": ("Flame!", "No flame"), "water": ("Wet", "Dry"),
                "motion": ("Motion now", "Clear")}[kind][0 if active else 1]

    def reading(self, dev: dict[str, Any]) -> Optional[dict[str, Any]]:
        kind = dev["kind"]
        if kind not in SENSOR_KINDS:
            return None
        with self._lock:
            w = dict(self._w.get(dev["device_id"]) or {})
        value = w.get("value")
        if value is None and dev.get("state") is not None:
            value = self._from_state(dev)                # after a restart: the stored state
        out: dict[str, Any] = {
            "value": round(value, 1) if isinstance(value, float) and kind in SERIES else value,
            "text": self._reading_text(dev, value), "level": w.get("level", "normal"),
            "at": w.get("at") or dev.get("state_at"), "warming": bool(w.get("warming")),
        }
        if kind in SERIES:
            out["unit"] = SERIES[kind][1]
        if kind == "gas":
            cfg = self.config(dev)
            out["baseline"] = self._baseline(dev["device_id"], cfg)[1]
            out["mv"] = w.get("mv")
        if kind == "climate":
            h = w.get("h")
            if h is None and dev.get("state"):
                try:
                    h = float(json.loads(dev["state"])["h"])
                except (ValueError, KeyError, TypeError):
                    h = None
            out["h"] = h
            if h is not None and value is not None:
                out["text"] = f"{value:.1f} °C · {round(h)} %"
        if kind in DIGITAL:
            out["last_active"] = w.get("last_active") or (
                dev.get("state_at") if dev.get("state") == "1" else None) or                 self.store.last_active(dev["device_id"])
        return out

    def _from_state(self, dev: dict[str, Any]) -> Optional[float]:
        kind, state = dev["kind"], dev["state"]
        try:
            if kind == "climate":
                return float(json.loads(state)["t"])
            if kind in DIGITAL:
                return float(state) if state in ("0", "1") else None
            if kind == "gas":
                mv = mv_from_state(state)
                cfg = self.config(dev)
                v0, _ = self._baseline(dev["device_id"], cfg)
                return (gas_ppm(mv / 1000.0 / cfg["divider"], v0, gas_model(dev))
                        if mv is not None and v0 else None)
        except (ValueError, KeyError, TypeError):
            return None
        return None

    def history(self, device_id: int, rng: str) -> dict[str, Any]:
        """Readings averaged per bucket; for a digital sensor, how many times
        it became active per bucket."""
        dev = self._sensor(device_id)
        if dev["kind"] in SERIES:
            span, bucket = {"1h": (3600, 60), "24h": (86400, 900),
                            "7d": (7 * 86400, 3600)}.get(rng, (3600, 60))
            series, unit = SERIES[dev["kind"]]
        else:
            span, bucket = {"1h": (3600, 300), "24h": (86400, 3600),
                            "7d": (7 * 86400, 6 * 3600)}.get(rng, (3600, 300))
            series, unit = "on", ""
        now = self._clock()
        points = self.store.history(device_id, series, now - span, bucket)
        with self._lock:
            acc = self._acc.get((device_id, series))
            if acc is not None and acc[2]:
                b = acc[0] // bucket * bucket
                live = {"ts": b, "avg": acc[1] / acc[2], "min": acc[3], "max": acc[4], "n": acc[2]}
                if points and points[-1]["ts"] == b:
                    p = points[-1]
                    n = p["n"] + live["n"]
                    points[-1] = {"ts": b, "avg": (p["avg"] * p["n"] + live["avg"] * live["n"]) / n,
                                  "min": min(p["min"], live["min"]), "max": max(p["max"], live["max"]),
                                  "n": n}
                else:
                    points.append(live)
        cfg = self.config(dev)
        return {"device_id": device_id, "series": series, "unit": unit, "range": rng,
                "bucket": bucket, "from": now - span, "to": now,
                "points": [{"ts": p["ts"], "avg": round(p["avg"], 2), "min": round(p["min"], 2),
                            "max": round(p["max"], 2), "n": p["n"]}
                           for p in points if p["avg"] is not None],
                "warn": cfg.get("warn"), "danger": cfg.get("danger")}

    def _stats(self, dev: dict[str, Any]) -> Optional[dict[str, Any]]:
        if dev["kind"] not in SERIES:
            return None
        pts = self.history(dev["device_id"], "1h")["points"]
        if not pts:
            return None
        return {"min": min(p["min"] for p in pts), "max": max(p["max"] for p in pts),
                "avg": sum(p["avg"] for p in pts) / len(pts)}

    def _trend(self, dev: dict[str, Any]) -> Optional[dict[str, Any]]:
        if dev["kind"] not in SERIES:
            return None
        now = self._clock()
        with self._lock:
            rec = [(ts, v) for ts, v, _ in self._recent.get(dev["device_id"], ()) if v is not None]
        window = [(ts, v) for ts, v in rec if now - ts <= 600]
        if len(window) < 2:
            return None
        lo_ts, lo = min(window, key=lambda p: p[1])
        hi_ts, hi = max(window, key=lambda p: p[1])
        last_ts, last = window[-1]
        # The move that ends at the latest reading: up from the low, or down from the high.
        if last - lo >= hi - last:
            delta, since = last - lo, lo_ts
        else:
            delta, since = last - hi, hi_ts
        return {"delta": round(delta, 1), "minutes": max(1, round((last_ts - since) / 60))}

    def _today(self, dev: dict[str, Any]) -> Optional[int]:
        if dev["kind"] not in DIGITAL:
            return None
        lt = time.localtime(self._clock())
        midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        n = sum(p["n"] for p in self.store.history(dev["device_id"], "on", midnight, 86400))
        with self._lock:
            acc = self._acc.get((dev["device_id"], "on"))
            if acc is not None:
                n += acc[2]
        return n

    def _step_label(self, step: dict[str, Any], target: Optional[dict[str, Any]] = None) -> str:
        t = step["type"]
        if t == "notify":
            if self._notifier is None or not self._notifier.ready():
                return "Notify on the dashboard (WhatsApp isn't set up)"
            n = len(self._notifier.phones())
            return f"Notify on the dashboard and by WhatsApp ({n} number{'s' if n != 1 else ''})"
        if t == "buzzer":
            return "Sound the alarm buzzer"
        if t == "incident":
            return "Open an incident"
        if target is None:
            return "A removed device"
        kind, value, name = target["kind"], str(step.get("value")), target["name"]
        if kind == "valve":
            return f"{'Open' if value == '1' else 'Close'} {name}"
        if kind == "lock":
            return f"{'Unlock' if value == '1' else 'Lock'} {name}"
        if kind == "dimmer":
            return f"Turn on {name}" if value == "100" else (
                f"Switch off {name}" if value == "0" else f"Set {name} to {value} %")
        if kind == "fan_pwm":
            return f"Switch off {name}" if value == "0" else (
                f"Turn on {name} ({describe(kind, value).lower()})")
        return f"{'Turn on' if value == '1' else 'Switch off'} {name}"

    def _response(self, dev: dict[str, Any], devices: list[dict[str, Any]]) -> dict[str, Any]:
        by_id = {d["device_id"]: d for d in devices}
        out = {}
        for level in LEVELS[dev["kind"]]:
            rule = self._card_rule(dev, level)
            have = rule["actions"] if rule else []
            items: list[dict[str, Any]] = []
            seen: set[str] = set()

            def add(step: dict[str, Any], checked: bool) -> None:
                key = (f"device:{step['device_id']}:{step['value']}" if step["type"] == "device"
                       else step["type"])
                if key in seen:
                    return
                seen.add(key)
                items.append({"key": key, "label": self._step_label(step, by_id.get(step.get("device_id"))),
                              "checked": checked})

            suggest = SUGGEST.get(dev["kind"], {})
            nearby = sorted((d for d in devices if d["room"] == dev["room"] and d["kind"] in suggest),
                            key=lambda d: (list(suggest).index(d["kind"]), d["name"]))
            for d in nearby:
                if suggest[d["kind"]] in ("1", "100"):
                    step = {"type": "device", "device_id": d["device_id"], "value": suggest[d["kind"]]}
                    add(step, step in have)
            add({"type": "notify"}, {"type": "notify"} in have)
            add({"type": "buzzer"}, {"type": "buzzer"} in have)
            for d in nearby:
                if suggest[d["kind"]] not in ("1", "100"):
                    step = {"type": "device", "device_id": d["device_id"], "value": suggest[d["kind"]]}
                    add(step, step in have)
            for step in have:                       # anything else the rule does
                if step.get("type") == "device":
                    add(step, True)
            add({"type": "incident"}, {"type": "incident"} in have)
            out[level] = {"items": items, "mode": (rule or {}).get("trigger", {}).get(
                "mode", "away" if dev["kind"] == "motion" else "any"),
                "fired_at": (rule or {}).get("last_fired_at"),
                "rule_id": (rule or {}).get("rule_id"),
                "enabled": (rule or {}).get("enabled", True),
                "extended": bool(rule) and self._extended(rule)}
        return out

    def sensor_view(self, dev: dict[str, Any], devices: Optional[list[dict[str, Any]]] = None
                    ) -> dict[str, Any]:
        devices = self.store.devices() if devices is None else devices
        self.ensure_rules(dev)
        kind = dev["kind"]
        cfg = self.config(dev)
        with self._lock:
            w = dict(self._w.get(dev["device_id"]) or {})
            ep = dict(w["episode"]) if w.get("episode") else None
        settings: dict[str, Any] = {"hold": cfg.get("hold"),
                                    "every": dev["options"].get("every")}
        if kind in SERIES:
            settings.update(warn=cfg.get("warn"), danger=cfg.get("danger"))
        if kind == "gas":
            cal = cfg.get("cal") or {}
            settings.update(divider=cfg["divider"], calibrated_at=cal.get("at"),
                            calibrated_by=cal.get("by"),
                            dividers=[{"value": k, "label": v} for k, v in DIVIDERS.items()])
        related = []
        for d in devices:
            if d["room"] == dev["room"] and d["kind"] in SUGGEST.get(kind, {}):
                related.append({"device_id": d["device_id"], "name": d["name"], "kind": d["kind"],
                                "state": d["state"], "state_at": d["state_at"],
                                "text": describe(d["kind"], d["state"]) if d["state"] is not None else None,
                                "online": self.hub.node_online(d["node_id"])})
        return {
            "device_id": dev["device_id"], "name": dev["name"], "room": dev["room"],
            "node_id": dev["node_id"], "pin": dev["pin"], "kind": kind,
            "kind_label": KINDS[kind]["label"], "short": short_name(dev),
            "gas": CURVES[gas_model(dev)]["gas"] if kind == "gas" else None,
            "online": self.hub.node_online(dev["node_id"]),
            "reading": self.reading(dev), "settings": settings,
            "series": SERIES.get(kind, (None,))[0], "levels": list(LEVELS[kind]),
            "episode": {"level": ep["level"], "started": ep["started"],
                        "acked": ep["acked"], "alerting": ep["alerting"]} if ep else None,
            "last_episode": w.get("last"),
            "stats": self._stats(dev), "trend": self._trend(dev), "today": self._today(dev),
            "response": self._response(dev, devices), "related": related,
        }

    def sensors_view(self) -> dict[str, Any]:
        devices = self.store.devices()
        sensors = [self.sensor_view(d, devices) for d in devices if d["kind"] in SENSOR_KINDS]
        order = {k: i for i, k in enumerate(SENSOR_KINDS)}
        sensors.sort(key=lambda s: (order[s["kind"]], s["room"], s["name"]))
        return {"sensors": sensors, "alerts": self.alerts(), "mode": self.mode(),
                "whatsapp": self._whatsapp_ready(),
                "phones": len(self._notifier.phones()) if self._notifier is not None else 0}

    def _changed(self, what: str = "sensors") -> None:
        self.hub._notify(what)
