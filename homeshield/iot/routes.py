"""Dashboard API for ESP32 nodes, their devices, and scenes.

Anyone signed in may switch devices, run scenes, acknowledge sensor alerts
and switch Home/Away (family members handle the house); adopting boards,
mapping pins, editing scenes and sensor settings stay admin-only. Unlocking
a door lock or opening a valve (directly or through a scene) also needs an
admin, unless Settings › Users lets guests do it.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from flask import Flask, Response, abort, jsonify, request, send_file

from ..auth import require_admin, require_login
from .hub import IotHub, firmware_binary
from .kinds import BOARDS, DEFAULT_BOARD, KINDS, PinError, normalise_value

NODE_COMMANDS = ("blink", "restart", "update")
UNLOCK_KINDS = ("lock", "valve")          # "1" lets someone in, or gas or water through


def register(app: Flask, hub: IotHub, *, username: Callable[[], str],
             via_tunnel: Callable[[], bool], lan_url: Callable[[], str],
             check_key: Callable[[str], bool],
             may_unlock: Callable[[], bool] = lambda: True) -> None:

    @app.errorhandler(PinError)
    def _pin_error(e: PinError):
        return jsonify({"error": str(e)}), 400

    def body() -> dict[str, Any]:
        return request.get_json(silent=True) or {}

    def unlocks(dev: Optional[dict[str, Any]], value: Any) -> bool:
        """Whether this would unlock a door or open a valve."""
        if dev is None or dev["kind"] not in UNLOCK_KINDS:
            return False
        try:
            return normalise_value(dev["kind"], value) == "1"
        except PinError:
            return False                     # set_device explains the bad value

    def refuse_unlock(names: list[str]) -> Response:
        return jsonify({"error": f"Only an admin can unlock or open {' and '.join(names)}. "
                                 "An admin can let guests do it in Settings › Users."}), 403

    # ---- devices and scenes (everyone signed in) ----------------------------------

    @app.route("/api/devices")
    @require_login
    def api_devices():
        return jsonify({**hub.devices_view(), "can_unlock": may_unlock()})

    @app.route("/api/devices/activity")
    @require_login
    def api_devices_activity():
        try:
            limit = min(200, max(1, int(request.args.get("limit", 30))))
        except ValueError:
            limit = 30
        return jsonify(hub.store.activity(limit))

    @app.route("/api/devices/<int:device_id>/set", methods=["POST"])
    @require_login
    def api_device_set(device_id: int):
        value = body().get("value")
        dev = hub.store.device(device_id)
        if unlocks(dev, value) and not may_unlock():
            return refuse_unlock([dev["name"]])
        return jsonify(hub.set_device(device_id, value, username()))

    @app.route("/api/scenes/<int:scene_id>/run", methods=["POST"])
    @require_login
    def api_scene_run(scene_id: int):
        scene = hub.store.scene(scene_id)
        if scene is not None and not may_unlock():
            names = []
            for a in scene["actions"]:
                d = hub.store.device(a.get("device_id"))
                if unlocks(d, a.get("value")):
                    names.append(d["name"])
            if names:
                return refuse_unlock(names)
        return jsonify(hub.run_scene(scene_id, username()))

    # ---- sensors (iot/sensors.py) -------------------------------------------------------
    # Everyone sees readings and alerts, can acknowledge them, sound the alarm and
    # switch Home/Away; levels, calibration and automatic responses are admin-only.

    def sensors():
        if hub.sensors is None:
            raise PinError("Sensors aren't available.")
        return hub.sensors

    @app.route("/api/sensors")
    @require_login
    def api_sensors():
        return jsonify(sensors().sensors_view())

    @app.route("/api/sensors/<int:device_id>/history")
    @require_login
    def api_sensor_history(device_id: int):
        rng = request.args.get("range") or "1h"
        if rng not in ("1h", "24h", "7d"):
            raise PinError("Choose 1h, 24h or 7d.")
        return jsonify(sensors().history(device_id, rng))

    @app.route("/api/sensors/<int:device_id>/acknowledge", methods=["POST"])
    @require_login
    def api_sensor_ack(device_id: int):
        sensors().acknowledge(device_id, username())
        return jsonify({"ok": True})

    @app.route("/api/sensors/<int:device_id>/sound", methods=["POST"])
    @require_login
    def api_sensor_sound(device_id: int):
        sensors().sound(device_id, username())
        return jsonify({"ok": True})

    @app.route("/api/mode")
    @require_login
    def api_mode():
        return jsonify(sensors().mode())

    @app.route("/api/mode", methods=["POST"])
    @require_login
    def api_mode_set():
        return jsonify(sensors().set_mode(str(body().get("mode") or ""), username()))

    @app.route("/api/sensors/<int:device_id>", methods=["PATCH"])
    @require_admin
    def api_sensor_edit(device_id: int):
        return jsonify(sensors().update_settings(device_id, body(), username()))

    @app.route("/api/sensors/<int:device_id>/calibrate", methods=["POST"])
    @require_admin
    def api_sensor_calibrate(device_id: int):
        return jsonify(sensors().calibrate(device_id, username()))

    @app.route("/api/sensors/<int:device_id>/response", methods=["PUT"])
    @require_admin
    def api_sensor_response(device_id: int):
        d = body()
        return jsonify(sensors().set_response(device_id, str(d.get("level") or ""),
                                              d.get("steps"), username()))

    # ---- automations (admins; iot/automations.py) ----------------------------------------

    def automations():
        if hub.automations is None:
            raise PinError("Automations aren't available.")
        return hub.automations

    @app.route("/api/automations")
    @require_admin
    def api_automations():
        return jsonify(automations().automations_view())

    @app.route("/api/automations", methods=["POST"])
    @require_admin
    def api_automation_add():
        return jsonify(automations().save(None, body(), username()))

    @app.route("/api/automations/<int:rule_id>", methods=["PUT"])
    @require_admin
    def api_automation_edit(rule_id: int):
        return jsonify(automations().save(rule_id, body(), username()))

    @app.route("/api/automations/<int:rule_id>", methods=["DELETE"])
    @require_admin
    def api_automation_remove(rule_id: int):
        automations().remove(rule_id, username())
        return jsonify({"ok": True})

    @app.route("/api/automations/<int:rule_id>/enabled", methods=["POST"])
    @require_admin
    def api_automation_enabled(rule_id: int):
        return jsonify(automations().set_enabled(rule_id, bool(body().get("enabled")), username()))

    @app.route("/api/automations/<int:rule_id>/test", methods=["POST"])
    @require_admin
    def api_automation_test(rule_id: int):
        return jsonify(automations().test(rule_id, username()))

    @app.route("/api/automations/describe", methods=["POST"])
    @require_admin
    def api_automation_describe():
        return jsonify(automations().describe(body()))

    @app.route("/api/automations/location", methods=["PUT"])
    @require_admin
    def api_automation_location():
        return jsonify(automations().set_location(body().get("city"), username()))

    # ---- setting things up (admins) --------------------------------------------------

    @app.route("/api/devices", methods=["POST"])
    @require_admin
    def api_device_add():
        d = body()
        return jsonify(hub.map_pin(str(d.get("node_id") or ""), d.get("pin"),
                                   str(d.get("kind") or ""), d.get("name"), d.get("room"),
                                   d.get("options") if isinstance(d.get("options"), dict) else {},
                                   username()))

    @app.route("/api/devices/<int:device_id>", methods=["PATCH"])
    @require_admin
    def api_device_edit(device_id: int):
        d = body()
        if d.get("options") is not None and not isinstance(d["options"], dict):
            raise PinError("Options must be a list of settings.")
        return jsonify(hub.update_device(device_id, d))

    @app.route("/api/devices/<int:device_id>", methods=["DELETE"])
    @require_admin
    def api_device_remove(device_id: int):
        hub.unmap_device(device_id, username())
        return jsonify({"ok": True})

    @app.route("/api/scenes", methods=["POST"])
    @require_admin
    def api_scene_add():
        d = body()
        return jsonify(hub.store.save_scene(None, name=d.get("name"), description=d.get("description"),
                                            icon=d.get("icon"), actions=d.get("actions")))

    @app.route("/api/scenes/<int:scene_id>", methods=["PUT"])
    @require_admin
    def api_scene_edit(scene_id: int):
        d = body()
        return jsonify(hub.store.save_scene(scene_id, name=d.get("name"), description=d.get("description"),
                                            icon=d.get("icon"), actions=d.get("actions")))

    @app.route("/api/scenes/<int:scene_id>", methods=["DELETE"])
    @require_admin
    def api_scene_remove(scene_id: int):
        hub.store.remove_scene(scene_id)
        return jsonify({"ok": True})

    @app.route("/api/nodes")
    @require_admin
    def api_nodes():
        out = hub.nodes_view()
        out["kinds"] = [{"kind": k, "label": v["label"], "type": v["type"], "options": v["options"]}
                        for k, v in KINDS.items()]
        out["server_url"] = lan_url()
        return jsonify(out)

    @app.route("/api/nodes/<node_id>")
    @require_admin
    def api_node(node_id: str):
        return jsonify(hub.node_view(node_id))

    @app.route("/api/nodes/<node_id>", methods=["PATCH"])
    @require_admin
    def api_node_edit(node_id: str):
        hub.store.update_node(node_id, body().get("room"))
        hub.publish_config(node_id)
        return jsonify(hub.node_view(node_id))

    @app.route("/api/nodes/<node_id>", methods=["DELETE"])
    @require_admin
    def api_node_remove(node_id: str):
        hub.forget_node(node_id, username())
        return jsonify({"ok": True})

    @app.route("/api/nodes/adopt", methods=["POST"])
    @require_admin
    def api_node_adopt():
        d = body()
        return jsonify(hub.adopt(str(d.get("chip_id") or ""), str(d.get("node_id") or ""),
                                 d.get("room"), username()))

    @app.route("/api/nodes/<node_id>/pins")
    @require_admin
    def api_node_pins(node_id: str):
        kind = request.args.get("kind") or ""
        if kind not in KINDS:
            raise PinError("Pick what is wired to the pin.")
        return jsonify(hub.pin_options(node_id, kind, {"pull": request.args.get("pull") == "1"}
                                       if kind != "sos" else {"pull": True}))

    @app.route("/api/nodes/<node_id>/cmd", methods=["POST"])
    @require_admin
    def api_node_cmd(node_id: str):
        cmd = str(body().get("cmd") or "")
        if cmd not in NODE_COMMANDS:
            raise PinError("Unknown command.")
        if cmd == "update":
            board = hub.node_board(node_id)
            if firmware_binary(board=board) is None:
                raise PinError(f"No compiled firmware for an {BOARDS[board]['label']} yet. In the Arduino IDE, "
                               f"open firmware/HomeShieldNode, pick that board, and use Sketch > Export "
                               f"Compiled Binary.")
            cmd = f"update {lan_url()}/api/nodes/firmware.bin?board={board}"
        hub.node_command(node_id, cmd)
        return jsonify({"ok": True})

    # ---- for the boards themselves ----------------------------------------------------
    # A board downloading a firmware update. It signs in with the device key
    # (HTTP basic auth, user "node"), and only from the home network.

    @app.route("/api/nodes/firmware.bin")
    def api_node_firmware():
        if via_tunnel():
            return abort(404)
        auth = request.authorization
        if auth is None or auth.username != "node" or not check_key(auth.password or ""):
            return Response("key required\n", 401, {"WWW-Authenticate": 'Basic realm="HomeShield"'})
        board = request.args.get("board") or DEFAULT_BOARD
        path = firmware_binary(board=board if board in BOARDS else DEFAULT_BOARD)
        if path is None:
            return abort(404)
        return send_file(path, mimetype="application/octet-stream", conditional=True)
