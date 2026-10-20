"""The HomeShield side of the ESP32 nodes: MQTT topics, adoption, commands.

Topics, all under homeshield/<node>/ where <node> is the node's name
(node-kitchen), or esp32-<chip> for a board not adopted yet:

    status          node -> retained "online"; "offline" is its last will
    info            node -> retained JSON: fw, ip, rssi, up(time), mac
    config          HomeShield -> retained JSON: what is wired to which pin
    config/ack      node -> the config version it has applied
    pin/<n>/set     HomeShield -> a value for an output: 1/0, 0-100, test/stop
    pin/<n>/state   node -> retained: the value now (inputs: 1 = active)
    event           node -> SOS or BTN (its SOS or silence button)
    cmd             HomeShield -> blink, restart, forget, update <url>
    adopt           HomeShield -> {"id": "node-kitchen"} for a new board

plus homeshield/alarm, retained: the line every buzzer acts on, exactly as
the USB buzzer receives it (ALARM FIRE 1729 / STOP 1730, see alarm.py).

A command counts as done when the node reports the new state; if it hasn't
within CONFIRM_S, the activity log says the node did not reply.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .kinds import (BOARDS, DEFAULT_BOARD, KINDS, PinError, describe, firmware_entry,
                    normalise_value, pin_choices, pin_type)
from .store import IotStore

log = logging.getLogger(__name__)

PREFIX = "homeshield"
ALARM_TOPIC = f"{PREFIX}/alarm"
CONFIRM_S = 4.0
DISCOVERED_STALE_S = 300.0       # a found board that hasn't spoken for this long is dropped
INFO_PERSIST_S = 60.0
_TOPIC_RE = re.compile(rf"^{PREFIX}/([A-Za-z0-9_-]{{1,40}})/(.+)$")

FIRMWARE_DIR = Path(__file__).resolve().parents[2] / "firmware" / "HomeShieldNode"
_FW_RE = re.compile(r'FIRMWARE\s*=\s*"([0-9.]+)"')


def firmware_version(sketch: Path = FIRMWARE_DIR / "HomeShieldNode.ino") -> Optional[str]:
    try:
        m = _FW_RE.search(sketch.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    return m.group(1) if m else None


def build_board(folder_name: str) -> str:
    """Which board a build folder is for: Arduino names it after the board,
    e.g. esp32.esp32.esp32 or esp32.esp32.nologo_esp32c3_super_mini."""
    return "esp32c3" if "c3" in folder_name.lower() else "esp32"


def firmware_binary(folder: Path = FIRMWARE_DIR, board: str = DEFAULT_BOARD) -> Optional[Path]:
    """The newest compiled HomeShieldNode.ino.bin for this board: Arduino IDE's
    Sketch > Export Compiled Binary puts it in build/<board>/ inside the sketch
    folder. A binary for one chip doesn't run on the other."""
    bins = [p for p in folder.glob("build/*/HomeShieldNode.ino.bin") if build_board(p.parent.name) == board]
    if board == DEFAULT_BOARD:
        bins += list(folder.glob("HomeShieldNode*.bin"))
    return max(bins, key=lambda p: p.stat().st_mtime) if bins else None


def _version_tuple(v: Optional[str]) -> tuple:
    try:
        return tuple(int(x) for x in str(v).split("."))
    except ValueError:
        return ()


class IotHub:
    def __init__(self, store: IotStore, *, alarm=None, clock: Callable[[], float] = time.time):
        self.store = store
        self.alarm = alarm
        self._clock = clock
        self._lock = threading.Lock()
        self._link = None                          # publish(topic, payload, retain)
        self._connected = False
        self._live: dict[str, dict[str, Any]] = {}       # node -> online, since, info, config_v
        self._found: dict[str, dict[str, Any]] = {}      # esp32-xxxx -> info, seen
        self._pending: dict[tuple[str, int], dict[str, Any]] = {}
        self._alarm_line: Optional[str] = None
        self._listeners: list[Callable[[str], None]] = []
        self._stop = threading.Event()
        self.broker = None                         # set by create_app: broker.Broker
        self.sensors = None                        # sensors.SensorEngine attaches itself
        self.automations = None                    # automations.AutomationEngine attaches itself
        if alarm is not None:
            alarm.add_listener(self._publish_alarm)
            alarm.add_link_source(self._buzzer_links)

    # ---- wiring ---------------------------------------------------------------

    def attach(self, link) -> None:
        """link: an object with publish(topic, payload, retain=False)."""
        self._link = link

    def add_listener(self, fn: Callable[[str], None]) -> None:
        """fn(what) after any change: "devices", "nodes" or "activity"."""
        self._listeners.append(fn)

    def _notify(self, what: str) -> None:
        for fn in list(self._listeners):
            try:
                fn(what)
            except Exception as e:
                log.warning("iot listener failed: %s", e)

    def _publish(self, topic: str, payload: str, retain: bool = False) -> bool:
        if self._link is None or not self._connected:
            return False
        try:
            self._link.publish(topic, payload, retain=retain)
            return True
        except Exception as e:
            log.warning("MQTT publish to %s failed: %s", topic, e)
            return False

    def start_ticker(self) -> None:
        threading.Thread(target=self._tick_loop, name="hs-iot-tick", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _tick_loop(self) -> None:
        while not self._stop.wait(0.5):
            try:
                self.tick()
            except Exception as e:
                log.warning("iot tick: %s", e)

    def tick(self, now: Optional[float] = None) -> None:
        """Commands nobody confirmed, and the alarm's time limit."""
        now = self._clock() if now is None else now
        with self._lock:
            late = [k for k, p in self._pending.items() if now - p["at"] >= CONFIRM_S]
            gone = [self._pending.pop(k) for k in late]
        for p in gone:
            self.store.log(f"{p['name']} → {p['label']}: {p['node']} did not reply",
                           by=p["by"], device_id=p["device_id"], node_id=p["node"])
        if gone:
            self._notify("devices")
            self._notify("activity")
        if self.alarm is not None:
            self._publish_alarm()
        if self.sensors is not None:
            self.sensors.tick(now)
        if self.automations is not None:
            self.automations.tick(now)

    # ---- connection -------------------------------------------------------------

    def on_connected(self) -> None:
        """The MQTT link is up: resend every config (retained) and the alarm."""
        self._connected = True
        for node in self.store.nodes():
            self.publish_config(node["node_id"])
        self._alarm_line = None
        self._publish_alarm()
        self._notify("nodes")

    def on_disconnected(self) -> None:
        self._connected = False
        self._notify("nodes")

    @property
    def connected(self) -> bool:
        return self._connected

    # ---- incoming -----------------------------------------------------------------

    def handle(self, topic: str, payload: str) -> None:
        m = _TOPIC_RE.match(topic)
        if not m:
            return
        node, rest = m.group(1), m.group(2)
        adopted = self.store.node(node) is not None
        if rest == "status":
            self._on_status(node, payload.strip(), adopted)
        elif rest == "info":
            self._on_info(node, payload, adopted)
        elif rest == "config/ack" and adopted:
            with self._lock:
                self._live.setdefault(node, {})["config_v"] = payload.strip()
            self._notify("nodes")
        elif rest.startswith("pin/") and rest.endswith("/state") and adopted:
            try:
                pin = int(rest.split("/")[1])
            except ValueError:
                return
            self._on_state(node, pin, payload.strip())
        elif rest == "event" and adopted:
            self._on_event(node, payload.strip())

    def _on_status(self, node: str, status: str, adopted: bool) -> None:
        now = self._clock()
        if not adopted:
            with self._lock:
                if status == "online":
                    self._found.setdefault(node, {"info": {}})["seen"] = now
                else:
                    self._found.pop(node, None)
            self._notify("nodes")
            return
        online = status == "online"
        with self._lock:
            live = self._live.setdefault(node, {})
            was = live.get("online")
            live["online"] = online
            if was != online:
                live["since"] = now
        if was is not None and was != online:
            self.store.log(f"{node} came back online" if online else f"{node} went offline",
                           node_id=node, by="")
            self._notify("activity")
        if online:
            self.store.seen_node(node)
        self._notify("nodes")
        self._notify("devices")

    def _on_info(self, node: str, payload: str, adopted: bool) -> None:
        try:
            info = json.loads(payload) if payload.strip() else {}
        except ValueError:
            return
        now = self._clock()
        with self._lock:
            if not adopted:
                if info:
                    self._found.setdefault(node, {})
                    self._found[node].update(info=info, seen=now)
                else:
                    self._found.pop(node, None)
                persist = False
            else:
                live = self._live.setdefault(node, {})
                live["info"] = info
                live["info_at"] = now
                persist = now - live.get("persisted", 0) >= INFO_PERSIST_S
                if persist:
                    live["persisted"] = now
        if adopted and persist:
            self.store.seen_node(node, info)
        self._notify("nodes")

    def _on_state(self, node: str, pin: int, value: str) -> None:
        dev = self.store.device_at(node, pin)
        if dev is None:
            return
        changed = dev["state"] != value
        self.store.set_state(dev["device_id"], value)
        with self._lock:
            pending = self._pending.get((node, pin))
            confirmed = pending is not None and pending["value"] == value
            if confirmed:
                self._pending.pop((node, pin))
        if self.sensors is not None:
            self.sensors.on_state(dev, value)
        if changed and pending is None and dev["kind"] in KINDS and \
                KINDS[dev["kind"]]["type"] in ("out", "pwm") and dev["state"] is not None:
            # Not asked for by anyone here: a timer on the board, or a restart.
            auto = dev["options"].get("auto_off")
            by = f"Auto-off after {auto} s" if auto and value == "0" else node
            if dev["kind"] == "lock" and auto and value == "0":
                by = f"Auto-lock after {auto} s"
            self.store.log(f"{dev['name']} → {describe(dev['kind'], value)}",
                           by=by, device_id=dev["device_id"], node_id=node)
            self._notify("activity")
        if changed and dev["state"] is not None and self.automations is not None and \
                KINDS.get(dev["kind"], {}).get("type") in ("out", "pwm"):
            self.automations.on_device_state(dev, value)
        self._notify("devices")

    def _on_event(self, node: str, event: str) -> None:
        if event.startswith("UPDAT"):             # UPDATING, UPDATE_FAILED <why>
            text = (f"{node} is updating its firmware" if event == "UPDATING"
                    else f"{node} could not update: {event.partition(' ')[2] or 'unknown error'}")
            self.store.log(text, node_id=node)
            self._notify("activity")
            return
        if self.alarm is None:
            return
        if event == "BTN":
            self.alarm.silence(f"the button on {node}")
        elif event == "SOS":
            sos = next((d for d in self.store.devices(node) if d["kind"] == "sos"), None)
            room = (sos or {}).get("room") or (self.store.node(node) or {}).get("room") or node
            self.alarm.sos_pressed(room)

    # ---- outgoing --------------------------------------------------------------------

    def config_for(self, node_id: str) -> dict[str, Any]:
        node = self.store.node(node_id) or {}
        pins = [firmware_entry(d["pin"], d["kind"], d["options"])
                for d in self.store.devices(node_id)]
        return {"v": int(self._clock()), "room": node.get("room", ""), "pins": pins}

    def publish_config(self, node_id: str) -> None:
        self._publish(f"{PREFIX}/{node_id}/config", json.dumps(self.config_for(node_id)),
                      retain=True)

    def _publish_alarm(self) -> None:
        if self.alarm is None:
            return
        line = self.alarm.command()
        if line != self._alarm_line and self._publish(ALARM_TOPIC, line, retain=True):
            self._alarm_line = line

    def node_online(self, node_id: str) -> bool:
        with self._lock:
            return bool(self._live.get(node_id, {}).get("online"))

    def node_board(self, node_id: str) -> str:
        with self._lock:
            board = self._live.get(node_id, {}).get("info", {}).get("board")
        return board or self.store.node_board(node_id)

    def node_uptime(self, node_id: str) -> Optional[float]:
        """Seconds since the board started, from its last info message."""
        with self._lock:
            live = self._live.get(node_id, {})
            up, at = live.get("info", {}).get("up"), live.get("info_at")
            online = live.get("online")
        if not online or up is None or at is None:
            return None
        try:
            return float(up) + (self._clock() - at)
        except (TypeError, ValueError):
            return None

    def set_device(self, device_id: int, value: Any, by: str) -> dict[str, Any]:
        dev = self.store.device(device_id)
        if dev is None:
            raise PinError("That device no longer exists.")
        payload = normalise_value(dev["kind"], value)
        if not self._connected:
            raise PinError("HomeShield isn't connected to its MQTT broker.")
        if not self.node_online(dev["node_id"]):
            raise PinError(f"{dev['node_id']} is offline, so {dev['name']} can't be switched.")
        label = describe(dev["kind"], payload)
        self._publish(f"{PREFIX}/{dev['node_id']}/pin/{dev['pin']}/set", payload)
        if KINDS[dev["kind"]]["type"] != "buzzer":
            with self._lock:
                self._pending[(dev["node_id"], dev["pin"])] = {
                    "value": payload, "at": self._clock(), "by": by, "label": label,
                    "name": dev["name"], "node": dev["node_id"], "device_id": device_id}
        self.store.log(f"{dev['name']} → {label}", by=by, device_id=device_id,
                       node_id=dev["node_id"])
        self._notify("activity")
        self._notify("devices")
        return {"device_id": device_id, "sent": payload, "label": label}

    def node_command(self, node_id: str, cmd: str) -> None:
        if self.store.node(node_id) is None:
            raise PinError("That node isn't adopted.")
        if not self.node_online(node_id):
            raise PinError(f"{node_id} is offline.")
        self._publish(f"{PREFIX}/{node_id}/cmd", cmd)

    def adopt(self, chip_id: str, node_id: str, room: str, by: str) -> dict[str, Any]:
        with self._lock:
            found = chip_id in self._found
            info = dict(self._found.get(chip_id, {}).get("info") or {})
        if not found:
            raise PinError("That board is no longer on the network. Power it on and wait "
                           "for it to appear under Found on your network.")
        node = self.store.add_node(node_id, room, chip_id)
        if info:
            self.store.seen_node(node["node_id"], info)     # which board it is, for its pin rules
        with self._lock:
            self._found.pop(chip_id, None)
        self.publish_config(node["node_id"])
        self._publish(f"{PREFIX}/{chip_id}/adopt", json.dumps({"id": node["node_id"]}))
        self.store.log(f"Adopted {chip_id} as {node['node_id']} ({node['room']})", by=by,
                       node_id=node["node_id"])
        self._notify("nodes")
        self._notify("activity")
        return node

    def forget_node(self, node_id: str, by: str) -> None:
        if self.store.node(node_id) is None:
            raise PinError("That node isn't adopted.")
        self._publish(f"{PREFIX}/{node_id}/cmd", "forget")
        for leftover in ("config", "status", "info"):
            self._publish(f"{PREFIX}/{node_id}/{leftover}", "", retain=True)
        for dev in self.store.devices(node_id):
            self._publish(f"{PREFIX}/{node_id}/pin/{dev['pin']}/state", "", retain=True)
            if self.sensors is not None:
                self.sensors.forget(dev["device_id"])
        self.store.remove_node(node_id)
        with self._lock:
            self._live.pop(node_id, None)
        self.store.log(f"Removed {node_id}", by=by)
        self._notify("nodes")
        self._notify("devices")
        self._notify("activity")

    def map_pin(self, node_id: str, pin: Any, kind: str, name: Any, room: Any,
                options: Optional[dict[str, Any]], by: str) -> dict[str, Any]:
        dev = self.store.add_device(node_id=node_id, pin=pin, kind=kind, name=name,
                                    room=room, options=options)
        self.publish_config(node_id)
        if self.sensors is not None:
            self.sensors.on_device_added(dev)
        self.store.log(f"Added {dev['name']} on {node_id} GPIO {dev['pin']}", by=by,
                       device_id=dev["device_id"], node_id=node_id)
        self._notify("devices")
        self._notify("nodes")
        self._notify("activity")
        return dev

    def update_device(self, device_id: int, data: dict[str, Any]) -> dict[str, Any]:
        dev = self.store.update_device(device_id, name=data.get("name"), room=data.get("room"),
                                       options=data.get("options"))
        if data.get("options") is not None:
            self.publish_config(dev["node_id"])
        self._notify("devices")
        return dev

    def unmap_device(self, device_id: int, by: str) -> None:
        dev = self.store.remove_device(device_id)
        if dev is None:
            raise PinError("That device no longer exists.")
        if self.sensors is not None:
            self.sensors.forget(device_id)
        self._publish(f"{PREFIX}/{dev['node_id']}/pin/{dev['pin']}/state", "", retain=True)
        self.publish_config(dev["node_id"])
        self.store.log(f"Removed {dev['name']} from {dev['node_id']} GPIO {dev['pin']}", by=by,
                       node_id=dev["node_id"])
        self._notify("devices")
        self._notify("nodes")
        self._notify("activity")

    def run_scene(self, scene_id: int, by: str) -> dict[str, Any]:
        scene = self.store.scene(scene_id)
        if scene is None:
            raise PinError("That scene no longer exists.")
        done, skipped = 0, []
        for action in scene["actions"]:
            try:
                self.set_device(action["device_id"], action["value"], f"Scene “{scene['name']}”")
                done += 1
            except PinError as e:
                skipped.append(str(e))
        self.store.scene_ran(scene_id, by)
        self.store.log(f"Scene “{scene['name']}” ran", by=by)
        self._notify("activity")
        self._notify("devices")
        return {"done": done, "skipped": skipped}

    # ---- alarm links ------------------------------------------------------------

    def _buzzer_links(self) -> list[dict[str, Any]]:
        out = []
        for dev in self.store.devices():
            if dev["kind"] == "buzzer" and self.node_online(dev["node_id"]):
                with self._lock:
                    info = self._live.get(dev["node_id"], {}).get("info", {})
                out.append({"via": "mqtt", "id": dev["node_id"], "ip": info.get("ip"),
                            "firmware": info.get("fw")})
        return out

    # ---- for the dashboard ----------------------------------------------------------

    def nodes_view(self) -> dict[str, Any]:
        now = self._clock()
        latest = firmware_version()
        binaries = {b: firmware_binary(board=b) for b in BOARDS}
        devices = self.store.devices()
        with self._lock:
            live = {k: dict(v) for k, v in self._live.items()}
            for k in [k for k, v in self._found.items() if now - v.get("seen", 0) > DISCOVERED_STALE_S]:
                self._found.pop(k)
            found = [{"chip_id": k, "seen": v.get("seen"), **{
                "ip": v.get("info", {}).get("ip"), "fw": v.get("info", {}).get("fw"),
                "mac": v.get("info", {}).get("mac"),
                "board": v.get("info", {}).get("board") or DEFAULT_BOARD,
                "board_label": BOARDS.get(v.get("info", {}).get("board"), BOARDS[DEFAULT_BOARD])["label"]}}
                for k, v in self._found.items()]
        nodes = []
        for n in self.store.nodes():
            lv = live.get(n["node_id"], {})
            info = lv.get("info") or n.get("info") or {}
            fw = info.get("fw")
            board = info.get("board") or DEFAULT_BOARD
            mine = [d for d in devices if d["node_id"] == n["node_id"]]
            nodes.append({
                "node_id": n["node_id"], "room": n["room"], "chip_id": n["chip_id"],
                "online": bool(lv.get("online")), "since": lv.get("since"),
                "last_seen": n.get("last_seen"), "info": info,
                "config_v": lv.get("config_v"),
                "update": bool(latest and fw and _version_tuple(fw) < _version_tuple(latest)),
                "devices": len(mine), "board": board,
                "board_label": BOARDS.get(board, BOARDS[DEFAULT_BOARD])["label"],
                "binary": binaries.get(board) is not None,
            })
        online = sum(n["online"] for n in nodes)
        return {"connected": self._connected, "nodes": nodes, "found": found,
                "online": online, "total": len(nodes),
                "firmware": {"latest": latest, "binary": any(binaries.values()),
                             "binaries": {b: p is not None for b, p in binaries.items()}},
                "broker": self.broker.status() if self.broker is not None else None}

    def node_view(self, node_id: str) -> dict[str, Any]:
        node = next((n for n in self.nodes_view()["nodes"] if n["node_id"] == node_id), None)
        if node is None:
            raise PinError("That node isn't adopted.")
        devices = [self._device_view(d) for d in self.store.devices(node_id)]
        return {**node, "pins": devices, "config": self.config_for(node_id)}

    def _device_view(self, d: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            pending = self._pending.get((d["node_id"], d["pin"]))
        kind = KINDS.get(d["kind"], {})
        out = {**d, "type": pin_type(d["kind"], d["options"]) if kind else None,
               "kind_label": kind.get("label", d["kind"]),
               "online": self.node_online(d["node_id"]),
               "pending": pending["value"] if pending else None}
        if self.sensors is not None:
            out["reading"] = self.sensors.reading(d)
        return out

    def devices_view(self) -> dict[str, Any]:
        devices = [self._device_view(d) for d in self.store.devices()]
        return {"devices": devices, "scenes": self.store.scenes(),
                "online": sum(d["online"] for d in devices), "total": len(devices),
                "connected": self._connected}

    def pin_options(self, node_id: str, kind: str, options: dict[str, Any]) -> list[dict[str, Any]]:
        taken = {d["pin"] for d in self.store.devices(node_id)}
        return pin_choices(kind, options, taken, self.node_board(node_id))
