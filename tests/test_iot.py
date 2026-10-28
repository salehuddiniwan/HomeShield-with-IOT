"""ESP32 nodes over MQTT: pin rules, adoption, configs, commands and their
confirmation, scenes, the alarm on the boards, and the dashboard API."""

import json

import pytest

from homeshield.alarm import AlarmController
from homeshield.db import init_db, write_conn
from homeshield.iot.hub import CONFIRM_S, IotHub
from homeshield.iot.kinds import PinError, check_pin, firmware_entry, pin_choices
from homeshield.iot.store import IotStore
from homeshield.server import create_app

T0 = 1_800_000_000.0


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


class Settings(dict):
    def get(self, k, default=None):
        return super().get(k, default)

    def update(self, partial):
        super().update(partial)
        return dict(self)


class FakeLink:
    """The broker, as the hub sees it: everything it publishes."""

    def __init__(self):
        self.sent = []

    def publish(self, topic, payload, retain=False):
        self.sent.append((topic, payload, retain))

    def last(self, topic):
        return next((p for t, p, _ in reversed(self.sent) if t == topic), None)


@pytest.fixture
def hub(tmp_path):
    db = str(tmp_path / "hs.db")
    init_db(db)
    clock = Clock()
    alarm = AlarmController(Settings(buzzer_enabled=True, buzzer_max_seconds=60,
                                     buzzer_kinds="fire,sos,fall,intruder,zone"), clock=clock)
    h = IotHub(IotStore(db), alarm=alarm, clock=clock)
    link = FakeLink()
    h.attach(link)
    h.on_connected()
    return h, link, clock, alarm


def board(h, chip="esp32-3C61A8", ip="192.168.1.47"):
    h.handle(f"homeshield/{chip}/status", "online")
    h.handle(f"homeshield/{chip}/info", json.dumps({"fw": "1.0", "ip": ip, "rssi": -55}))


def adopted(h, name="node-kitchen", room="Kitchen"):
    board(h)
    h.adopt("esp32-3C61A8", name, room, "admin")
    h.handle(f"homeshield/{name}/status", "online")
    return name


# ---- pins --------------------------------------------------------------------

def test_pins_are_checked_against_what_the_esp32_can_do():
    check_pin(34, "gas", {})                         # analog, Wi-Fi safe
    check_pin(26, "light", {"inv": True})
    for pin, kind, opts, why in ((25, "gas", {}, "analog pin"),
                                 (34, "light", {}, "can only read"),
                                 (2, "light", {}, "blue LED"),
                                 (12, "relay", {}, "start-up"),
                                 (35, "sos", {"pull": True}, "pull-up")):
        with pytest.raises(PinError, match=why):
            check_pin(pin, kind, opts)
    choices = {c["pin"]: c for c in pin_choices("light", {}, taken={26})}
    assert not choices[26]["ok"] and "in use" in choices[26]["why"]
    assert choices[27]["ok"] and not choices[34]["ok"]


def test_the_board_gets_short_config_entries():
    assert firmware_entry(27, "lock", {"inv": True, "auto_off": 60}) == \
        {"pin": 27, "t": "out", "inv": 1, "auto": 60}
    assert firmware_entry(25, "buzzer", {"inv": True, "passive": True, "hz": 2400}) == \
        {"pin": 25, "t": "buzzer", "inv": 1, "passive": 1, "hz": 2400}
    assert firmware_entry(33, "sos", {"inv": True, "pull": True, "hold": 300, "role": "sos"}) == \
        {"pin": 33, "t": "in", "inv": 1, "pull": 1, "role": "sos", "hold": 300}
    assert firmware_entry(4, "climate", {"model": "dht11", "every": 10})["t"] == "dht11"


# ---- adoption --------------------------------------------------------------------

def test_a_new_board_is_found_adopted_and_sent_its_config(hub):
    h, link, _, _ = hub
    board(h)
    view = h.nodes_view()
    assert [f["chip_id"] for f in view["found"]] == ["esp32-3C61A8"]
    assert view["found"][0]["ip"] == "192.168.1.47"
    with pytest.raises(PinError, match="lowercase"):
        h.adopt("esp32-3C61A8", "Kitchen Node!", "Kitchen", "admin")
    with pytest.raises(PinError, match="no longer on the network"):
        h.adopt("esp32-FFFFFF", "node-x", "Hall", "admin")
    h.adopt("esp32-3C61A8", "node-kitchen", "Kitchen", "admin")
    assert json.loads(link.last("homeshield/esp32-3C61A8/adopt")) == {"id": "node-kitchen"}
    assert json.loads(link.last("homeshield/node-kitchen/config"))["pins"] == []
    assert h.nodes_view()["found"] == []
    with pytest.raises(PinError, match="already a node"):
        board(h)
        h.adopt("esp32-3C61A8", "node-kitchen", "Kitchen", "admin")


def test_mapping_a_pin_sends_the_new_config_and_validates(hub):
    h, link, _, _ = hub
    node = adopted(h)
    h.map_pin(node, 26, "light", "Kitchen light", "", {}, "admin")
    h.map_pin(node, 34, "gas", "Gas detector", "", {"every": 3}, "admin")
    pins = json.loads(link.last(f"homeshield/{node}/config"))["pins"]
    assert {"pin": 26, "t": "out", "inv": 1} in pins and {"pin": 34, "t": "adc", "inv": 0, "every": 3} in pins
    with pytest.raises(PinError, match="already in use"):
        h.map_pin(node, 26, "fan", "Fan", "", {}, "admin")
    with pytest.raises(PinError, match="analog"):
        h.map_pin(node, 27, "gas", "Second gas", "", {}, "admin")
    dev = h.store.device_at(node, 26)
    assert dev["room"] == "Kitchen"                   # the node's room by default


# ---- commands ---------------------------------------------------------------------

def test_a_command_is_sent_confirmed_by_the_board_and_logged(hub):
    h, link, clock, _ = hub
    node = adopted(h)
    dev = h.map_pin(node, 26, "light", "Kitchen light", "", {}, "admin")
    h.set_device(dev["device_id"], "on", "nadia")
    assert link.last(f"homeshield/{node}/pin/26/set") == "1"
    assert h.devices_view()["devices"][0]["pending"] == "1"
    h.handle(f"homeshield/{node}/pin/26/state", "1")
    view = h.devices_view()["devices"][0]
    assert view["state"] == "1" and view["pending"] is None
    assert h.store.activity()[0]["text"] == "Kitchen light → On"
    assert h.store.activity()[0]["by"] == "nadia"

    h.set_device(dev["device_id"], "off", "nadia")
    clock.t += CONFIRM_S + 0.1
    h.tick()
    assert h.store.activity()[0]["text"].endswith(f"{node} did not reply")


def test_offline_boards_and_sensors_refuse_commands(hub):
    h, _, _, _ = hub
    node = adopted(h)
    gas = h.map_pin(node, 34, "gas", "Gas", "", {}, "admin")
    lamp = h.map_pin(node, 25, "dimmer", "Lamp", "", {}, "admin")
    with pytest.raises(PinError, match="sensor"):
        h.set_device(gas["device_id"], "on", "admin")
    with pytest.raises(PinError, match="0 to 100"):
        h.set_device(lamp["device_id"], 150, "admin")
    h.handle(f"homeshield/{node}/status", "offline")
    with pytest.raises(PinError, match="offline"):
        h.set_device(lamp["device_id"], 40, "admin")
    assert h.store.activity()[0]["text"] == f"{node} went offline"


def test_a_speed_fan_takes_speeds_and_a_dimmer_remembers_its_level(hub):
    h, link, _, _ = hub
    node = adopted(h)
    fan = h.map_pin(node, 25, "fan_pwm", "Ceiling fan", "", {}, "admin")
    lamp = h.map_pin(node, 26, "dimmer", "Lamp", "", {}, "admin")
    assert json.loads(link.last(f"homeshield/{node}/config"))["pins"][0] == {"pin": 25, "t": "pwm", "inv": 0}
    h.set_device(fan["device_id"], 70, "admin")
    assert link.last(f"homeshield/{node}/pin/25/set") == "70"
    h.handle(f"homeshield/{node}/pin/25/state", "70")
    assert h.store.activity()[0]["text"] == "Ceiling fan → Medium"
    with pytest.raises(PinError, match="a speed from 0 to 100"):
        h.set_device(fan["device_id"], 120, "admin")

    def level(dev):
        return next(d for d in h.devices_view()["devices"] if d["device_id"] == dev["device_id"])["level"]
    assert level(lamp) == "100"                       # never on yet: its switch turns it on full
    h.handle(f"homeshield/{node}/pin/26/state", "40")
    h.handle(f"homeshield/{node}/pin/26/state", "0")
    assert level(lamp) == "40"                        # off: its switch brings back 40 %
    assert level(fan) == "70"


def test_a_lock_that_relocks_itself_is_logged_as_auto_lock(hub):
    h, _, _, _ = hub
    node = adopted(h, "node-door", "Front Door")
    lock = h.map_pin(node, 27, "lock", "Door lock", "", {}, "admin")
    h.set_device(lock["device_id"], "unlock", "saleh")
    h.handle(f"homeshield/{node}/pin/27/state", "1")
    h.handle(f"homeshield/{node}/pin/27/state", "0")      # 60 s later, by the board
    top = h.store.activity()[0]
    assert (top["text"], top["by"]) == ("Door lock → Locked", "Auto-lock after 60 s")


def test_a_scene_sets_each_device_and_skips_what_it_cant(hub):
    h, link, _, _ = hub
    node = adopted(h)
    light = h.map_pin(node, 26, "light", "Light", "", {}, "admin")
    lamp = h.map_pin(node, 25, "dimmer", "Lamp", "", {}, "admin")
    scene = h.store.save_scene(None, name="Night", description="", icon="moon",
                               actions=[{"device_id": light["device_id"], "value": "off"},
                                        {"device_id": lamp["device_id"], "value": 20}])
    out = h.run_scene(scene["scene_id"], "admin")
    assert out == {"done": 2, "skipped": []}
    assert link.last(f"homeshield/{node}/pin/25/set") == "20"
    assert h.store.scene(scene["scene_id"])["last_run_by"] == "admin"
    with pytest.raises(PinError, match="at least one"):
        h.store.save_scene(None, name="Empty", description="", icon="", actions=[])


# ---- the alarm on the boards ----------------------------------------------------------

def test_buzzers_on_nodes_play_the_alarm_and_their_buttons_reach_it(hub):
    h, link, _, alarm = hub
    node = adopted(h, "node-living", "Living Room")
    h.map_pin(node, 25, "buzzer", "Alarm buzzer", "", {}, "admin")
    h.map_pin(node, 33, "sos", "SOS button", "Bedroom", {}, "admin")
    assert alarm.status()["connected"]
    assert alarm.status(full=True)["links"][-1]["via"] == "mqtt"

    alarm.on_incident("opened", {"incident_id": 1, "kind": "fire"})
    assert link.last("homeshield/alarm").startswith("ALARM FIRE ")
    h.handle(f"homeshield/{node}/event", "BTN")
    assert link.last("homeshield/alarm").startswith("STOP ")
    assert alarm.status()["silenced_by"] == f"the button on {node}"

    heard = []
    alarm.add_sos_listener(heard.append)
    h.handle(f"homeshield/{node}/event", "SOS")
    assert heard == ["Bedroom"]                            # named after the button's room


def test_forgetting_a_node_clears_its_retained_messages(hub):
    h, link, _, _ = hub
    node = adopted(h)
    h.map_pin(node, 26, "light", "Light", "", {}, "admin")
    h.forget_node(node, "admin")
    assert link.last(f"homeshield/{node}/cmd") == "forget"
    for topic in ("config", "status", "info", "pin/26/state"):
        assert link.last(f"homeshield/{node}/{topic}") == ""
    assert h.store.nodes() == [] and h.store.devices() == []


# ---- the dashboard ------------------------------------------------------------------------

@pytest.fixture
def app(tmp_path):
    db = str(tmp_path / "api.db")
    app = create_app(db_path=db, snapshot_dir=str(tmp_path / "s"),
                     person_photos_dir=str(tmp_path / "p"),
                     intruder_photos_dir=str(tmp_path / "i"), auto_start=False)
    with write_conn(db) as conn:
        conn.execute("UPDATE users SET must_change = 0")
    h = app.extensions["homeshield_iot"]
    link = FakeLink()
    h.attach(link)
    h.on_connected()
    return app


def _login(app, user, pw):
    c = app.test_client()
    assert c.post("/api/login", json={"username": user, "password": pw}).status_code == 200
    return c


def test_admins_set_up_nodes_and_guests_use_the_devices(app):
    h = app.extensions["homeshield_iot"]
    admin = _login(app, "admin", "admin")
    board(h)
    nodes = admin.get("/api/nodes").get_json()
    assert nodes["found"][0]["chip_id"] == "esp32-3C61A8" and nodes["kinds"]
    r = admin.post("/api/nodes/adopt", json={"chip_id": "esp32-3C61A8", "node_id": "node-kitchen",
                                             "room": "Kitchen"})
    assert r.status_code == 200
    h.handle("homeshield/node-kitchen/status", "online")
    bad = admin.post("/api/devices", json={"node_id": "node-kitchen", "pin": 25, "kind": "gas",
                                           "name": "Gas"})
    assert bad.status_code == 400 and "analog" in bad.get_json()["error"]
    light = admin.post("/api/devices", json={"node_id": "node-kitchen", "pin": 26, "kind": "light",
                                             "name": "Kitchen light"}).get_json()
    pins = admin.get("/api/nodes/node-kitchen/pins?kind=gas").get_json()
    assert {p["pin"] for p in pins if p["ok"]} == {32, 33, 34, 35, 36, 39}

    admin.post("/api/users", json={"username": "nadia", "password": "nadia1", "role": "guest",
                                   "must_change_password": False})
    guest = _login(app, "nadia", "nadia1")
    r = guest.post(f"/api/devices/{light['device_id']}/set", json={"value": "on"})
    assert r.status_code == 200 and r.get_json()["label"] == "On"
    devices = guest.get("/api/devices").get_json()
    assert devices["total"] == 1 and devices["online"] == 1
    assert guest.get("/api/devices/activity").get_json()[0]["by"] == "nadia"
    st = guest.get("/api/status").get_json()["iot"]
    assert st == {"connected": True, "devices_online": 1, "devices_total": 1,
                  "sensors_total": 0, "sensors_offline": []}
    assert "nodes_total" in admin.get("/api/status").get_json()["iot"]


def test_guests_unlock_doors_and_open_valves_only_when_an_admin_allows_it(app):
    h = app.extensions["homeshield_iot"]
    admin = _login(app, "admin", "admin")
    node = adopted(h)
    door = h.map_pin(node, 23, "lock", "Front door", "Entrance", {}, "admin")["device_id"]
    valve = h.map_pin(node, 26, "valve", "Gas valve", "Kitchen", {}, "admin")["device_id"]
    visitor = admin.post("/api/scenes", json={"name": "Visitor", "actions": [
        {"device_id": door, "value": "1"}]}).get_json()["scene_id"]
    admin.post("/api/users", json={"username": "nadia", "password": "nadia1", "role": "guest",
                                   "must_change_password": False})
    guest = _login(app, "nadia", "nadia1")

    assert guest.get("/api/devices").get_json()["can_unlock"] is False
    for did, value in ((door, "unlock"), (door, "1"), (valve, "open")):
        r = guest.post(f"/api/devices/{did}/set", json={"value": value})
        assert r.status_code == 403 and "Settings › Users" in r.get_json()["error"]
    r = guest.post(f"/api/scenes/{visitor}/run", json={})
    assert r.status_code == 403 and "Front door" in r.get_json()["error"]
    # Locking and closing are always allowed, and admins can always unlock.
    assert guest.post(f"/api/devices/{door}/set", json={"value": "lock"}).status_code == 200
    assert guest.post(f"/api/devices/{valve}/set", json={"value": "0"}).status_code == 200
    assert admin.post(f"/api/devices/{door}/set", json={"value": "unlock"}).status_code == 200

    assert admin.post("/api/settings", json={"guests_unlock": "true"}).status_code == 200
    assert guest.get("/api/devices").get_json()["can_unlock"] is True
    assert guest.post(f"/api/devices/{door}/set", json={"value": "unlock"}).status_code == 200
    assert guest.post(f"/api/scenes/{visitor}/run", json={}).status_code == 200


def test_boards_download_firmware_only_with_the_key(app, tmp_path, monkeypatch):
    import homeshield.iot.routes as routes
    binary = tmp_path / "HomeShieldNode.ino.bin"
    binary.write_bytes(b"\xe9" + b"\0" * 100)
    monkeypatch.setattr(routes, "firmware_binary", lambda board="esp32", **kw: binary)
    key = app.extensions["homeshield_alarm"].settings.get("buzzer_device_key")
    c = app.test_client()
    assert c.get("/api/nodes/firmware.bin").status_code == 401
    assert c.get("/api/nodes/firmware.bin", auth=("node", "wrong")).status_code == 401
    with c.get("/api/nodes/firmware.bin", auth=("node", key)) as r:
        assert r.status_code == 200 and r.data[:1] == b"\xe9"
    assert c.get("/api/nodes/firmware.bin", auth=("node", key),
                 headers={"Cf-Connecting-Ip": "203.0.113.7"}).status_code == 404


# ---- ESP32-C3 SuperMini ----------------------------------------------------------------------

def test_supermini_pins_follow_the_c3():
    check_pin(4, "light", {"inv": False}, "esp32c3")
    check_pin(3, "gas", {}, "esp32c3")                    # ADC1
    for pin, kind, why in ((23, "light", "isn't available on an ESP32-C3 SuperMini"),
                           (8, "light", "blue LED"), (9, "sos", "BOOT button"),
                           (2, "light", "how the board starts"),
                           (5, "gas", "GPIO 0, 1, 3 or 4")):
        with pytest.raises(PinError, match=why):
            check_pin(pin, kind, {}, "esp32c3")
    choices = {c["pin"]: c for c in pin_choices("light", {}, set(), "esp32c3")}
    assert set(choices) == {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 20, 21}
    assert not choices[2]["ok"] and choices[21]["label"] == "GPIO 21 (TX)"
    # The DevKit list now shows its reserved pins too, greyed out with the reason.
    devkit = {c["pin"]: c for c in pin_choices("light", {}, set())}
    assert not devkit[2]["ok"] and "blue LED" in devkit[2]["why"]


def test_a_supermini_is_adopted_with_its_own_pin_rules(hub):
    h, link, clock, alarm = hub
    h.handle("homeshield/esp32-C3A1B2/status", "online")
    h.handle("homeshield/esp32-C3A1B2/info", json.dumps({"fw": "1.3", "ip": "192.168.1.60",
                                                         "board": "esp32c3"}))
    found = h.nodes_view()["found"][0]
    assert found["board"] == "esp32c3" and found["board_label"] == "ESP32-C3 SuperMini"
    h.adopt("esp32-C3A1B2", "node-sos2", "Bedroom 2", "admin")
    assert h.node_board("node-sos2") == "esp32c3"
    with pytest.raises(PinError, match="isn't available on an ESP32-C3"):
        h.map_pin("node-sos2", 23, "light", "Lamp", "", {}, "admin")
    h.map_pin("node-sos2", 4, "sos", "SOS button", "", {}, "admin")
    pins = {p["pin"]: p for p in h.pin_options("node-sos2", "light", {})}
    assert not pins[4]["ok"] and pins[0]["ok"] and 23 not in pins
    node = next(n for n in h.nodes_view()["nodes"] if n["node_id"] == "node-sos2")
    assert node["board_label"] == "ESP32-C3 SuperMini"


def test_each_board_gets_its_own_firmware_file(tmp_path):
    from homeshield.iot.hub import build_board, firmware_binary
    devkit = tmp_path / "build" / "esp32.esp32.esp32"
    c3 = tmp_path / "build" / "esp32.esp32.nologo_esp32c3_super_mini"
    devkit.mkdir(parents=True)
    assert firmware_binary(tmp_path, "esp32c3") is None
    (devkit / "HomeShieldNode.ino.bin").write_bytes(b"\xe9")
    assert firmware_binary(tmp_path, "esp32c3") is None
    c3.mkdir(parents=True)
    (c3 / "HomeShieldNode.ino.bin").write_bytes(b"\xe9")
    assert firmware_binary(tmp_path, "esp32c3").parent == c3
    assert firmware_binary(tmp_path, "esp32").parent == devkit
    assert build_board("esp32.esp32.esp32c3") == "esp32c3" and build_board("esp32.esp32.esp32") == "esp32"


def test_a_supermini_downloads_the_c3_binary(app, tmp_path, monkeypatch):  # noqa: F811
    import homeshield.iot.routes as routes
    c3_bin = tmp_path / "c3.bin"
    c3_bin.write_bytes(b"\xe9C3")
    monkeypatch.setattr(routes, "firmware_binary",
                        lambda board="esp32", **kw: c3_bin if board == "esp32c3" else None)
    h = app.extensions["homeshield_iot"]
    admin = _login(app, "admin", "admin")
    adopted(h)                                            # a DevKit: no binary for it
    r = admin.post("/api/nodes/node-kitchen/cmd", json={"cmd": "update"})
    assert r.status_code == 400 and "ESP32 DevKit" in r.get_json()["error"]
    h.handle("homeshield/esp32-C3A1B2/status", "online")
    h.handle("homeshield/esp32-C3A1B2/info", json.dumps({"fw": "1.3", "board": "esp32c3"}))
    h.adopt("esp32-C3A1B2", "node-mini", "Porch", "admin")
    h.handle("homeshield/node-mini/status", "online")
    assert admin.post("/api/nodes/node-mini/cmd", json={"cmd": "update"}).status_code == 200
    link = h._link
    assert link.last("homeshield/node-mini/cmd").endswith("/api/nodes/firmware.bin?board=esp32c3")
    key = app.extensions["homeshield_alarm"].settings.get("buzzer_device_key")
    with app.test_client().get("/api/nodes/firmware.bin?board=esp32c3", auth=("node", key)) as r:
        assert r.status_code == 200 and r.data == b"\xe9C3"
