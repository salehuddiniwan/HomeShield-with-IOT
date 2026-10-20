"""SQLite storage for ESP32 nodes, their devices, scenes and the activity log."""

from __future__ import annotations

import json
import re
import time
from typing import Any, Optional

from ..db import read_conn, write_conn
from .kinds import DEFAULT_BOARD, KINDS, PinError, check_pin, clean_options, normalise_value

NODE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}$")
NAME_MAX = 40
LOG_KEEP = 2000


def _row(r) -> dict[str, Any]:
    d = dict(r)
    for key in ("options", "actions", "info", "trigger_spec", "conditions", "steps"):
        if key in d and isinstance(d[key], str):
            try:
                d[key] = json.loads(d[key])
            except ValueError:
                d[key] = [] if key in ("actions", "conditions", "steps") else {}
    if "trigger_spec" in d:
        # Rules made before automations had one trigger, not a list.
        spec = d.pop("trigger_spec")
        d["triggers"] = spec if isinstance(spec, list) else [spec] if spec else []
        d["trigger"] = d["triggers"][0] if d["triggers"] else {}
    if "origin" in d:
        d["enabled"] = bool(d["enabled"])
    return d


def _text(value: Any, label: str, max_len: int = NAME_MAX, required: bool = True) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise PinError(f"Enter a {label}.")
    if len(text) > max_len:
        raise PinError(f"The {label} is too long ({max_len} characters at most).")
    return text


class IotStore:
    def __init__(self, db_path: str):
        self.db_path = db_path

    # ---- nodes ----------------------------------------------------------------

    def nodes(self) -> list[dict[str, Any]]:
        with read_conn(self.db_path) as conn:
            return [_row(r) for r in conn.execute("SELECT * FROM nodes ORDER BY node_id")]

    def node(self, node_id: str) -> Optional[dict[str, Any]]:
        with read_conn(self.db_path) as conn:
            r = conn.execute("SELECT * FROM nodes WHERE node_id = ?", (node_id,)).fetchone()
        return _row(r) if r else None

    def add_node(self, node_id: str, room: str, chip_id: str) -> dict[str, Any]:
        node_id = str(node_id or "").strip().lower()
        if not NODE_ID_RE.match(node_id):
            raise PinError("Name the node with lowercase letters, digits and dashes, "
                           "e.g. node-kitchen (2 to 31 characters).")
        if node_id.startswith("esp32-"):
            raise PinError("Names starting with esp32- are for boards not adopted yet. "
                           "Pick another, e.g. node-kitchen.")
        room = _text(room, "room")
        with write_conn(self.db_path) as conn:
            if conn.execute("SELECT 1 FROM nodes WHERE node_id = ?", (node_id,)).fetchone():
                raise PinError(f"There is already a node called {node_id}.")
            conn.execute("INSERT INTO nodes (node_id, room, chip_id, adopted_at) "
                         "VALUES (?, ?, ?, ?)", (node_id, room, chip_id, time.time()))
        return self.node(node_id)

    def update_node(self, node_id: str, room: str) -> None:
        with write_conn(self.db_path) as conn:
            conn.execute("UPDATE nodes SET room = ? WHERE node_id = ?",
                         (_text(room, "room"), node_id))

    def seen_node(self, node_id: str, info: Optional[dict[str, Any]] = None) -> None:
        with write_conn(self.db_path) as conn:
            if info is None:
                conn.execute("UPDATE nodes SET last_seen = ? WHERE node_id = ?",
                             (time.time(), node_id))
            else:
                conn.execute("UPDATE nodes SET last_seen = ?, info = ? WHERE node_id = ?",
                             (time.time(), json.dumps(info), node_id))

    def node_board(self, node_id: str) -> str:
        """esp32 or esp32c3: what the board said it is (older firmware: esp32)."""
        node = self.node(node_id) or {}
        return (node.get("info") or {}).get("board") or DEFAULT_BOARD

    def remove_node(self, node_id: str) -> None:
        ids = {d["device_id"] for d in self.devices(node_id)}
        with write_conn(self.db_path) as conn:
            conn.execute("DELETE FROM devices WHERE node_id = ?", (node_id,))
            conn.execute("DELETE FROM nodes WHERE node_id = ?", (node_id,))
            for device_id in ids:
                conn.execute("DELETE FROM sensor_settings WHERE device_id = ?", (device_id,))
                conn.execute("DELETE FROM sensor_history WHERE device_id = ?", (device_id,))
        if ids:
            self._drop_from_rules(ids)

    # ---- devices --------------------------------------------------------------

    def devices(self, node_id: Optional[str] = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM devices", ()
        if node_id is not None:
            sql, args = sql + " WHERE node_id = ?", (node_id,)
        with read_conn(self.db_path) as conn:
            return [_row(r) for r in conn.execute(sql + " ORDER BY room, name", args)]

    def device(self, device_id: int) -> Optional[dict[str, Any]]:
        with read_conn(self.db_path) as conn:
            r = conn.execute("SELECT * FROM devices WHERE device_id = ?",
                             (int(device_id),)).fetchone()
        return _row(r) if r else None

    def device_at(self, node_id: str, pin: int) -> Optional[dict[str, Any]]:
        with read_conn(self.db_path) as conn:
            r = conn.execute("SELECT * FROM devices WHERE node_id = ? AND pin = ?",
                             (node_id, int(pin))).fetchone()
        return _row(r) if r else None

    def add_device(self, *, node_id: str, pin: Any, kind: str, name: Any, room: Any,
                   options: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        node = self.node(node_id)
        if node is None:
            raise PinError("That node isn't adopted yet.")
        if kind not in KINDS:
            raise PinError("Pick what is wired to the pin.")
        try:
            pin = int(pin)
        except (TypeError, ValueError):
            raise PinError("Pick a pin.") from None
        opts = clean_options(kind, options or {})
        check_pin(pin, kind, opts, (node.get("info") or {}).get("board") or DEFAULT_BOARD)
        if self.device_at(node_id, pin) is not None:
            raise PinError(f"GPIO {pin} on {node_id} is already in use.")
        if kind == "buzzer" and any(d["kind"] == "buzzer" for d in self.devices(node_id)):
            raise PinError("A node plays one buzzer. Add the second one to another node.")
        name = _text(name, "name")
        room = _text(room, "room", required=False) or node["room"]
        with write_conn(self.db_path) as conn:
            cur = conn.execute(
                "INSERT INTO devices (node_id, pin, kind, name, room, options, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (node_id, pin, kind, name, room, json.dumps(opts), time.time()))
        return self.device(cur.lastrowid)

    def update_device(self, device_id: int, *, name: Any = None, room: Any = None,
                      options: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        dev = self.device(device_id)
        if dev is None:
            raise PinError("That device no longer exists.")
        sets: dict[str, Any] = {}
        if name is not None:
            sets["name"] = _text(name, "name")
        if room is not None:
            sets["room"] = _text(room, "room", required=False)
        if options is not None:
            opts = clean_options(dev["kind"], {**dev["options"], **options})
            check_pin(dev["pin"], dev["kind"], opts, self.node_board(dev["node_id"]))
            sets["options"] = json.dumps(opts)
        if sets:
            cols = ", ".join(f"{k} = ?" for k in sets)
            with write_conn(self.db_path) as conn:
                conn.execute(f"UPDATE devices SET {cols} WHERE device_id = ?",
                             (*sets.values(), int(device_id)))
        return self.device(device_id)

    def remove_device(self, device_id: int) -> Optional[dict[str, Any]]:
        dev = self.device(device_id)
        if dev is not None:
            with write_conn(self.db_path) as conn:
                conn.execute("DELETE FROM devices WHERE device_id = ?", (int(device_id),))
                conn.execute("DELETE FROM sensor_settings WHERE device_id = ?", (int(device_id),))
                conn.execute("DELETE FROM sensor_history WHERE device_id = ?", (int(device_id),))
            self._drop_from_rules({int(device_id)})
        return dev

    def set_state(self, device_id: int, state: str) -> None:
        with write_conn(self.db_path) as conn:
            conn.execute("UPDATE devices SET state = ?, state_at = ? WHERE device_id = ?",
                         (state, time.time(), int(device_id)))

    # ---- activity log ----------------------------------------------------------

    def log(self, text: str, *, by: str = "", device_id: Optional[int] = None,
            node_id: Optional[str] = None) -> dict[str, Any]:
        now = time.time()
        with write_conn(self.db_path) as conn:
            cur = conn.execute("INSERT INTO device_log (ts, device_id, node_id, text, by) "
                               "VALUES (?, ?, ?, ?, ?)", (now, device_id, node_id, text, by))
            if cur.lastrowid % 100 == 0:          # keep the table small
                conn.execute("DELETE FROM device_log WHERE log_id <= ?",
                             (cur.lastrowid - LOG_KEEP,))
        return {"log_id": cur.lastrowid, "ts": now, "device_id": device_id,
                "node_id": node_id, "text": text, "by": by}

    def activity(self, limit: int = 30) -> list[dict[str, Any]]:
        with read_conn(self.db_path) as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM device_log ORDER BY ts DESC, log_id DESC LIMIT ?", (int(limit),))]

    # ---- scenes ---------------------------------------------------------------

    def scenes(self) -> list[dict[str, Any]]:
        with read_conn(self.db_path) as conn:
            return [_row(r) for r in conn.execute("SELECT * FROM scenes ORDER BY scene_id")]

    def scene(self, scene_id: int) -> Optional[dict[str, Any]]:
        with read_conn(self.db_path) as conn:
            r = conn.execute("SELECT * FROM scenes WHERE scene_id = ?",
                             (int(scene_id),)).fetchone()
        return _row(r) if r else None

    def _scene_actions(self, actions: Any) -> list[dict[str, Any]]:
        if not isinstance(actions, list) or not actions:
            raise PinError("A scene needs at least one device to set.")
        out = []
        for a in actions:
            try:
                device_id = int(a["device_id"])
            except (TypeError, ValueError, KeyError):
                raise PinError("Each step of a scene names a device.") from None
            dev = self.device(device_id)
            if dev is None:
                raise PinError("A device in this scene no longer exists.")
            if dev["kind"] not in KINDS or KINDS[dev["kind"]]["type"] not in ("out", "pwm"):
                raise PinError(f"{dev['name']} can't be set by a scene.")
            out.append({"device_id": device_id, "value": normalise_value(dev["kind"], a.get("value"))})
        return out

    def save_scene(self, scene_id: Optional[int], *, name: Any, description: Any,
                   icon: Any, actions: Any) -> dict[str, Any]:
        fields = (_text(name, "scene name"),
                  _text(description, "description", max_len=80, required=False),
                  _text(icon, "icon", max_len=20, required=False) or "power",
                  json.dumps(self._scene_actions(actions)))
        with write_conn(self.db_path) as conn:
            if scene_id is None:
                cur = conn.execute("INSERT INTO scenes (name, description, icon, actions) "
                                   "VALUES (?, ?, ?, ?)", fields)
                scene_id = cur.lastrowid
            elif conn.execute("UPDATE scenes SET name = ?, description = ?, icon = ?, actions = ? "
                              "WHERE scene_id = ?", (*fields, int(scene_id))).rowcount == 0:
                raise PinError("That scene no longer exists.")
        return self.scene(scene_id)

    def remove_scene(self, scene_id: int) -> None:
        with write_conn(self.db_path) as conn:
            conn.execute("DELETE FROM scenes WHERE scene_id = ?", (int(scene_id),))

    def scene_ran(self, scene_id: int, by: str) -> None:
        with write_conn(self.db_path) as conn:
            conn.execute("UPDATE scenes SET last_run_at = ?, last_run_by = ? WHERE scene_id = ?",
                         (time.time(), by, int(scene_id)))

    # ---- sensors --------------------------------------------------------------

    def sensor_settings(self, device_id: int) -> Optional[dict[str, Any]]:
        """What was saved for this sensor, or None if nothing ever was."""
        with read_conn(self.db_path) as conn:
            r = conn.execute("SELECT data FROM sensor_settings WHERE device_id = ?",
                             (int(device_id),)).fetchone()
        if r is None:
            return None
        try:
            return json.loads(r["data"])
        except ValueError:
            return {}

    def save_sensor_settings(self, device_id: int, data: dict[str, Any]) -> None:
        with write_conn(self.db_path) as conn:
            conn.execute("INSERT INTO sensor_settings (device_id, data) VALUES (?, ?) "
                         "ON CONFLICT(device_id) DO UPDATE SET data = excluded.data",
                         (int(device_id), json.dumps(data)))

    def add_history(self, rows: list[tuple]) -> None:
        """rows: (device_id, series, minute, avg, min, max, n). A minute already
        stored (HomeShield restarted within it) is merged, not replaced."""
        if not rows:
            return
        with write_conn(self.db_path) as conn:
            conn.executemany(
                """INSERT INTO sensor_history (device_id, series, ts, avg, min, max, n)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(device_id, series, ts) DO UPDATE SET
                       avg = (COALESCE(avg, 0) * n + COALESCE(excluded.avg, 0) * excluded.n)
                             / MAX(1, n + excluded.n),
                       min = MIN(COALESCE(min, excluded.min), COALESCE(excluded.min, min)),
                       max = MAX(COALESCE(max, excluded.max), COALESCE(excluded.max, max)),
                       n = n + excluded.n""", rows)

    def history(self, device_id: int, series: str, since: float,
                bucket: int) -> list[dict[str, Any]]:
        """Readings since `since`, averaged into buckets of `bucket` seconds."""
        with read_conn(self.db_path) as conn:
            return [dict(r) for r in conn.execute(
                "SELECT (ts / ?) * ? AS ts, SUM(avg * n) / MAX(1, SUM(n)) AS avg, "
                "MIN(min) AS min, MAX(max) AS max, SUM(n) AS n FROM sensor_history "
                "WHERE device_id = ? AND series = ? AND ts >= ? GROUP BY ts / ? ORDER BY 1",
                (bucket, bucket, int(device_id), series, int(since), bucket))]

    def last_active(self, device_id: int) -> Optional[float]:
        """The latest minute a digital sensor became active, from its history."""
        with read_conn(self.db_path) as conn:
            r = conn.execute("SELECT MAX(ts) FROM sensor_history WHERE device_id = ? AND series = 'on'",
                             (int(device_id),)).fetchone()
        return float(r[0]) if r and r[0] is not None else None

    def prune_history(self, before: float) -> None:
        with write_conn(self.db_path) as conn:
            conn.execute("DELETE FROM sensor_history WHERE ts < ?", (int(before),))

    # ---- rules ------------------------------------------------------------------

    def rules(self) -> list[dict[str, Any]]:
        with read_conn(self.db_path) as conn:
            return [_row(r) for r in conn.execute("SELECT * FROM rules ORDER BY rule_id")]

    def rule(self, rule_id: int) -> Optional[dict[str, Any]]:
        with read_conn(self.db_path) as conn:
            r = conn.execute("SELECT * FROM rules WHERE rule_id = ?", (int(rule_id),)).fetchone()
        return _row(r) if r else None

    def save_rule(self, rule_id: Optional[int], *, name: str, triggers: list[dict[str, Any]],
                  actions: list[dict[str, Any]], conditions: Optional[list[dict[str, Any]]] = None,
                  options: Optional[dict[str, Any]] = None, origin: str = "user",
                  enabled: bool = True, by: Optional[str] = None) -> dict[str, Any]:
        fields = (_text(name, "name", max_len=80), 1 if enabled else 0, origin,
                  json.dumps(triggers), json.dumps(actions), json.dumps(conditions or []),
                  json.dumps(options or {}))
        with write_conn(self.db_path) as conn:
            if rule_id is None:
                cur = conn.execute(
                    "INSERT INTO rules (name, enabled, origin, trigger_spec, actions, conditions, "
                    "options, created_at, changed_by, changed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (*fields, time.time(), by, time.time() if by else None))
                rule_id = cur.lastrowid
            else:
                old = conn.execute("SELECT enabled FROM rules WHERE rule_id = ?", (int(rule_id),)).fetchone()
                if old is None:
                    raise PinError("That automation no longer exists.")
                conn.execute("UPDATE rules SET name = ?, enabled = ?, origin = ?, trigger_spec = ?, "
                             "actions = ?, conditions = ?, options = ? WHERE rule_id = ?",
                             (*fields, int(rule_id)))
                if by and bool(old["enabled"]) != enabled:
                    conn.execute("UPDATE rules SET changed_by = ?, changed_at = ? WHERE rule_id = ?",
                                 (by, time.time(), int(rule_id)))
        return self.rule(rule_id)

    def remove_rule(self, rule_id: int) -> None:
        with write_conn(self.db_path) as conn:
            conn.execute("DELETE FROM rules WHERE rule_id = ?", (int(rule_id),))
            conn.execute("DELETE FROM rule_runs WHERE rule_id = ?", (int(rule_id),))

    def add_run(self, rule_id: int, *, ts: float, cause: str, summary: str, ok: bool,
                steps: list[str], device_id: Optional[int] = None,
                camera_id: Optional[int] = None, incident_id: Optional[int] = None) -> int:
        with write_conn(self.db_path) as conn:
            cur = conn.execute("INSERT INTO rule_runs (rule_id, ts, cause, summary, ok, steps, "
                               "device_id, camera_id, incident_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                               (int(rule_id), ts, cause, summary, 1 if ok else 0, json.dumps(steps),
                                device_id, camera_id, incident_id))
            if cur.lastrowid % 50 == 0:
                conn.execute("DELETE FROM rule_runs WHERE run_id <= ?", (cur.lastrowid - 2000,))
        return cur.lastrowid

    def update_run(self, run_id: int, *, summary: str, ok: bool, steps: list[str]) -> None:
        with write_conn(self.db_path) as conn:
            conn.execute("UPDATE rule_runs SET summary = ?, ok = ?, steps = ? WHERE run_id = ?",
                         (summary, 1 if ok else 0, json.dumps(steps), int(run_id)))

    def runs(self, rule_id: int, limit: int = 8) -> list[dict[str, Any]]:
        with read_conn(self.db_path) as conn:
            rows = conn.execute("SELECT * FROM rule_runs WHERE rule_id = ? ORDER BY ts DESC, run_id DESC "
                                "LIMIT ?", (int(rule_id), int(limit))).fetchall()
        out = []
        for r in rows:
            d = _row(r)
            d["ok"] = bool(d["ok"])
            out.append(d)
        return out

    def incident_runs(self, *, incident_id: int, device_id: Optional[int] = None,
                      camera_id: Optional[int] = None, start: float = 0.0,
                      end: Optional[float] = None, limit: int = 12) -> list[dict[str, Any]]:
        """What automations did about one incident, oldest first: the runs it
        set off, and the runs its sensor or camera set off while it lasted."""
        end = time.time() if end is None else end
        where, args = ["r.incident_id = ?"], [int(incident_id)]
        if device_id is not None:
            where.append("(r.device_id = ? AND r.ts BETWEEN ? AND ?)")
            args += [int(device_id), start, end]
        if camera_id is not None:
            where.append("(r.camera_id = ? AND r.ts BETWEEN ? AND ?)")
            args += [int(camera_id), start, end]
        with read_conn(self.db_path) as conn:
            rows = conn.execute(
                "SELECT r.run_id, r.rule_id, r.ts, r.summary, r.ok, r.steps, rules.name AS rule "
                "FROM rule_runs r LEFT JOIN rules ON rules.rule_id = r.rule_id "
                f"WHERE {' OR '.join(where)} ORDER BY r.ts DESC, r.run_id DESC LIMIT ?",
                (*args, int(limit))).fetchall()
        out = []
        for r in reversed(rows):
            d = _row(r)
            d["ok"] = bool(d["ok"])
            out.append(d)
        return out

    def rule_fired(self, rule_id: int) -> None:
        with write_conn(self.db_path) as conn:
            conn.execute("UPDATE rules SET last_fired_at = ?, fire_count = fire_count + 1 "
                         "WHERE rule_id = ?", (time.time(), int(rule_id)))

    def _drop_from_rules(self, device_ids: set[int]) -> None:
        """A device is gone: rules it triggered go too, and steps that set it."""
        for rule in self.rules():
            triggers = [t for t in rule["triggers"] if t.get("device_id") not in device_ids]
            if not triggers and rule["triggers"]:
                self.remove_rule(rule["rule_id"])
                continue
            kept = [a for a in rule["actions"] if a.get("device_id") not in device_ids]
            conditions = [c for c in rule["conditions"] if c.get("device_id") not in device_ids]
            if (len(kept), len(triggers), len(conditions)) != \
                    (len(rule["actions"]), len(rule["triggers"]), len(rule["conditions"])):
                self.save_rule(rule["rule_id"], name=rule["name"], triggers=triggers, actions=kept,
                               conditions=conditions, options=rule["options"],
                               origin=rule["origin"], enabled=rule["enabled"])
