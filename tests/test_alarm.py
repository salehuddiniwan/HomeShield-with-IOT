"""The ESP32 alarm buzzer: when it sounds and stops, the USB link to the board,
and the board's Wi-Fi endpoints."""

import queue
import time

import pytest

from homeshield.alarm import AlarmController, SerialLink
from homeshield.db import write_conn
from homeshield.events import EventBus
from homeshield.server import create_app

from test_incidents import emit


class Settings(dict):
    def get(self, k, default=None):
        return super().get(k, default)

    def update(self, partial):
        super().update(partial)
        return dict(self)


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def _alarm(**settings):
    s = Settings(buzzer_enabled=True, buzzer_kinds="fire,sos,fall,intruder,zone",
                 buzzer_max_seconds=60, **settings)
    clock = Clock()
    return AlarmController(s, clock=clock), s, clock


def _inc(iid, kind, **kw):
    return {"incident_id": iid, "kind": kind, **kw}


def _pattern(line):
    return line.split()[1] if line.startswith("ALARM") else None


def _seq(line):
    return int(line.split()[-1])


# ---- when it sounds -------------------------------------------------------

def test_an_incident_sounds_until_someone_handles_it():
    alarm, _, _ = _alarm()
    quiet = alarm.command()
    assert quiet.startswith("STOP ")
    alarm.on_incident("opened", _inc(1, "fire"))
    ringing = alarm.command()
    assert _pattern(ringing) == "FIRE" and _seq(ringing) > _seq(quiet)
    alarm.on_incident("updated", _inc(1, "fire"))         # repeat detection
    assert alarm.command() == ringing                     # same line: no restart
    alarm.on_incident("resolved", _inc(1, "fire", resolved_by="nadia"))
    stopped = alarm.command()
    assert stopped.startswith("STOP ") and _seq(stopped) > _seq(ringing)
    assert alarm.status()["silenced_by"] == "nadia"


def test_the_most_urgent_kind_is_heard():
    alarm, _, _ = _alarm()
    alarm.on_incident("opened", _inc(1, "zone"))
    alarm.on_incident("opened", _inc(2, "fire"))
    assert _pattern(alarm.command()) == "FIRE"
    alarm.on_incident("resolved", _inc(2, "fire"))
    assert _pattern(alarm.command()) == "ZONE"            # the zone incident still needs someone


def test_a_silenced_incident_stays_quiet_until_it_gets_worse():
    alarm, _, _ = _alarm()
    alarm.on_incident("opened", _inc(1, "fall"))
    assert alarm.silence("admin") is True
    assert alarm.command().startswith("STOP")
    alarm.on_incident("updated", _inc(1, "fall"))
    assert alarm.command().startswith("STOP")
    alarm.on_incident("escalated", _inc(1, "fall"))       # now lying motionless
    assert _pattern(alarm.command()) == "FALL"


def test_it_stops_at_the_time_limit_and_zero_means_never():
    alarm, settings, clock = _alarm()
    alarm.on_incident("opened", _inc(1, "intruder"))
    clock.t += 59
    assert _pattern(alarm.command()) == "INTRUDER"
    clock.t += 2
    assert alarm.command().startswith("STOP")
    assert alarm.status()["silenced_by"] == "the time limit"

    settings["buzzer_max_seconds"] = 0
    alarm.on_incident("opened", _inc(2, "intruder"))
    clock.t += 3600
    assert _pattern(alarm.command()) == "INTRUDER"


def test_only_the_chosen_kinds_sound_and_turning_it_off_stops_it():
    alarm, settings, _ = _alarm(buzzer_port="auto")
    settings["buzzer_kinds"] = "fire"
    alarm.on_incident("opened", _inc(1, "zone"))
    assert alarm.command().startswith("STOP")
    alarm.on_incident("opened", _inc(2, "fire"))
    assert _pattern(alarm.command()) == "FIRE"
    settings["buzzer_enabled"] = False
    alarm.reload()
    assert alarm.command().startswith("STOP")
    alarm.on_incident("opened", _inc(3, "fire"))
    assert alarm.command().startswith("STOP")


def test_a_test_plays_once_and_never_over_a_real_alarm():
    alarm, _, clock = _alarm()
    alarm.test()
    line = alarm.command()
    assert _pattern(line) == "TEST" and alarm.status()["testing"]
    clock.t += 3.1
    after = alarm.command()
    assert after.startswith("STOP") and _seq(after) > _seq(line)
    alarm.on_incident("opened", _inc(1, "fire"))
    with pytest.raises(RuntimeError):
        alarm.test()


def test_the_seq_survives_a_restart():
    first, _, _ = _alarm()
    seq_before = _seq(first.command())
    later = AlarmController(Settings(buzzer_device_key="k"), clock=Clock(1_800_000_100.0))
    assert _seq(later.command()) > seq_before


# ---- USB ------------------------------------------------------------------

class FakeBoard:
    """The firmware's end of the USB link, as the PC sees it."""

    def __init__(self, answers=True, hello=b"HOMESHIELD-BUZZER 1.0 wifi=off\n"):
        self.answers = answers
        self.hello = hello
        self.lines: list[str] = []
        self.inbox: queue.Queue = queue.Queue()
        self.fail = None
        self.closed = False

    def write(self, data):
        if self.fail:
            raise self.fail
        for line in data.decode().splitlines():
            self.lines.append(line)
            if line == "HELLO" and self.answers:
                self.inbox.put(self.hello)
        return len(data)

    def readline(self):
        if self.fail:
            raise self.fail
        try:
            return self.inbox.get(timeout=0.02)
        except queue.Empty:
            return b""

    def close(self):
        self.closed = True

    def press_button(self):
        self.inbox.put(b"BTN\n")


def _eventually(check, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.02)
    return False


PORTS = [{"device": "COM6", "description": "USB Serial Device", "bridge": None},
         {"device": "COM9", "description": "Silicon Labs CP210x", "bridge": "CP2102"}]


def _link(alarm, settings, boards, ports=PORTS):
    opened = []

    def opener(device):
        opened.append(device)
        board = boards.pop(0) if boards else None
        if isinstance(board, Exception):
            raise board
        if board is None:
            raise OSError(f"could not open port '{device}': FileNotFoundError")
        return board

    link = SerialLink(alarm, settings, opener=opener, list_ports=lambda: list(ports))
    link.RETRY_S, link.HANDSHAKE_S = 0.1, 0.3
    return link, opened


def test_usb_finds_the_board_by_its_chip_plays_alarms_and_hears_the_button():
    alarm, settings, _ = _alarm(buzzer_port="auto")
    board = FakeBoard()
    link, opened = _link(alarm, settings, [board])
    link.start()
    try:
        assert _eventually(lambda: link.status()["connected"])
        assert opened == ["COM9"]                      # the unknown COM6 is left alone
        assert link.status()["port"] == "COM9" and link.status()["firmware"] == "1.0"
        assert alarm.status()["connected"]

        alarm.on_incident("opened", _inc(1, "fire"))
        assert _eventually(lambda: any(l.startswith("ALARM FIRE") for l in board.lines))
        board.press_button()
        assert _eventually(lambda: not alarm.status()["sounding"])
        assert alarm.status()["silenced_by"] == "the button on the buzzer"
        assert _eventually(lambda: board.lines[-1].startswith("STOP"))
    finally:
        link.stop()


def test_usb_reconnects_after_the_cable_is_pulled():
    alarm, settings, _ = _alarm(buzzer_port="auto")
    first, second = FakeBoard(), FakeBoard()
    link, opened = _link(alarm, settings, [first, second])
    link.start()
    try:
        assert _eventually(lambda: link.status()["connected"])
        first.fail = OSError("ClearCommError failed")
        assert _eventually(lambda: first.closed)
        assert _eventually(lambda: len(opened) == 2 and link.status()["connected"])
    finally:
        link.stop()


def test_usb_explains_a_busy_port_and_a_board_without_the_sketch():
    alarm, settings, _ = _alarm(buzzer_port="COM9")
    busy = OSError("could not open port 'COM9': PermissionError(13, 'Access is denied.', None, 5)")
    link, _ = _link(alarm, settings, [busy])
    assert link._connect("COM9") is False
    assert "in use by another program" in link.error

    link, _ = _link(alarm, settings, [FakeBoard(answers=False)])
    assert link._connect("COM9") is False
    assert "Upload firmware/HomeShieldBuzzer" in link.error

    link, _ = _link(alarm, settings, [], ports=[PORTS[0]])
    assert link._connect("auto") is False
    assert "No ESP32 found" in link.error


def test_usb_lets_go_of_the_port_when_the_buzzer_is_turned_off():
    alarm, settings, _ = _alarm(buzzer_port="auto")
    board = FakeBoard()
    link, _ = _link(alarm, settings, [board])
    link.start()
    try:
        assert _eventually(lambda: link.status()["connected"])
        settings["buzzer_enabled"] = False          # e.g. to upload new firmware
        assert _eventually(lambda: board.closed and not link.status()["connected"])
    finally:
        link.stop()


# ---- through the dashboard and Wi-Fi ---------------------------------------

@pytest.fixture
def app(tmp_path):
    path = str(tmp_path / "hs.db")
    app = create_app(db_path=path, snapshot_dir=str(tmp_path / "s"),
                     person_photos_dir=str(tmp_path / "p"),
                     intruder_photos_dir=str(tmp_path / "i"), auto_start=False)
    with write_conn(path) as conn:
        conn.execute("UPDATE users SET must_change = 0")
    app.config["DB"] = path
    return app


def _login(app, user, pw):
    c = app.test_client()
    assert c.post("/api/login", json={"username": user, "password": pw}).status_code == 200
    return c


def _guest(app, admin):
    admin.post("/api/users", json={"username": "nadia", "password": "nadia1",
                                   "role": "guest", "must_change_password": False})
    return _login(app, "nadia", "nadia1")


def _key(admin):
    return admin.get("/api/alarm/device_key").get_json()["buzzer_device_key"]


LAN = {"REMOTE_ADDR": "192.168.1.50"}


def test_a_wifi_board_needs_the_key_and_only_works_on_the_home_network(app):
    admin = _login(app, "admin", "admin")
    key = _key(admin)
    assert len(key) == 24
    board = app.test_client()
    r = board.get("/api/device/poll?id=hall", environ_base=LAN)
    assert r.status_code == 401 and r.get_data(as_text=True) == "ERR key\n"
    assert board.get("/api/device/poll", headers={"X-Device-Key": "wrong"},
                     environ_base=LAN).status_code == 401
    r = board.get("/api/device/poll?id=hall&fw=1.0", headers={"X-Device-Key": key},
                  environ_base=LAN)
    assert r.status_code == 200 and r.mimetype == "text/plain"
    assert r.get_data(as_text=True).startswith("STOP ")
    # From the internet, through the tunnel, the endpoint doesn't exist.
    assert board.get("/api/device/poll", headers={"X-Device-Key": key,
                                                  "Cf-Connecting-Ip": "203.0.113.7"}
                     ).status_code == 404
    links = admin.get("/api/alarm/status").get_json()["links"]
    assert links == [{"via": "wifi", "id": "hall", "ip": "192.168.1.50",
                      "firmware": "1.0", "age_s": links[0]["age_s"]}]


def test_closing_the_incident_in_the_dashboard_stops_the_buzzer(app):
    alarm = app.extensions["homeshield_alarm"]
    ev = emit(EventBus(db_path=app.config["DB"]), "fire_detected", time.time())
    alarm.on_incident(ev.incident["change"], ev.incident)
    admin = _login(app, "admin", "admin")
    key = _key(admin)
    poll = lambda: app.test_client().get("/api/device/poll", headers={"X-Device-Key": key},
                                         environ_base=LAN).get_data(as_text=True)
    assert poll().startswith("ALARM FIRE ")
    st = admin.get("/api/status").get_json()["alarm"]
    assert st["sounding"] and st["kind"] == "fire" and st["connected"]
    admin.post(f"/api/incidents/{ev.incident_id}/resolve", json={"outcome": "acknowledged"})
    assert poll().startswith("STOP ")


def test_anyone_signed_in_can_silence_but_only_admins_test_and_set_up(app):
    alarm = app.extensions["homeshield_alarm"]
    admin = _login(app, "admin", "admin")
    guest = _guest(app, admin)
    alarm.on_incident("opened", _inc(7, "intruder"))
    st = guest.get("/api/status").get_json()["alarm"]
    assert st["sounding"] and "links" not in st          # no ports or addresses for guests
    r = guest.post("/api/alarm/silence")
    assert r.status_code == 200 and not r.get_json()["sounding"]
    assert alarm.status()["silenced_by"] == "nadia"
    for method, url in (("POST", "/api/alarm/test"), ("GET", "/api/alarm/ports"),
                        ("POST", "/api/alarm/device_key")):
        assert guest.open(url, method=method).status_code == 403


def test_the_board_button_over_wifi_silences_it(app):
    alarm = app.extensions["homeshield_alarm"]
    admin = _login(app, "admin", "admin")
    key = _key(admin)
    alarm.on_incident("opened", _inc(3, "zone"))
    r = app.test_client().post("/api/device/button?id=kitchen", headers={"X-Device-Key": key},
                               environ_base=LAN)
    assert r.get_data(as_text=True).startswith("STOP ")
    assert alarm.status()["silenced_by"] == "the button on kitchen"


def test_testing_needs_a_connected_board(app):
    admin = _login(app, "admin", "admin")
    r = admin.post("/api/alarm/test")
    assert r.status_code == 400 and "No buzzer is connected" in r.get_json()["error"]
    key = _key(admin)
    poll = lambda: app.test_client().get("/api/device/poll", headers={"X-Device-Key": key},
                                         environ_base=LAN).get_data(as_text=True)
    poll()                                               # the board shows up
    assert admin.post("/api/alarm/test").status_code == 200
    assert poll().startswith("ALARM TEST ")


def test_buzzer_settings_are_checked_and_the_key_changes_only_on_purpose(app):
    admin = _login(app, "admin", "admin")
    key = _key(admin)
    for bad in ({"buzzer_port": "COM5; del *"}, {"buzzer_max_seconds": 99999},
                {"buzzer_max_seconds": "soon"}):
        assert admin.post("/api/settings", json=bad).status_code == 400, bad
    saved = admin.post("/api/settings", json={
        "buzzer_port": "com7", "buzzer_kinds": "fire,bogus,fire,zone",
        "buzzer_max_seconds": "0", "buzzer_enabled": "false",
        "buzzer_device_key": "chosen-by-a-stale-form"}).get_json()
    assert (saved["buzzer_port"], saved["buzzer_kinds"], saved["buzzer_max_seconds"],
            saved["buzzer_enabled"]) == ("COM7", "fire,zone", 0, False)
    assert "buzzer_device_key" not in saved                  # never sent with the settings
    assert _key(admin) == key

    new = admin.post("/api/alarm/device_key").get_json()["buzzer_device_key"]
    assert new != key
    board = app.test_client()
    assert board.get("/api/device/poll", headers={"X-Device-Key": key},
                     environ_base=LAN).status_code == 401
    assert board.get("/api/device/poll", headers={"X-Device-Key": new},
                     environ_base=LAN).status_code == 200


# ---- SOS button -------------------------------------------------------------

def test_sos_outranks_everything_but_fire_and_every_press_sounds_again():
    alarm, _, _ = _alarm()
    alarm.on_incident("opened", _inc(1, "fall"))
    alarm.on_incident("opened", _inc(2, "sos"))
    assert _pattern(alarm.command()) == "SOS"
    alarm.on_incident("opened", _inc(3, "fire"))
    assert _pattern(alarm.command()) == "FIRE"
    alarm.silence("admin")
    alarm.on_incident("updated", _inc(3, "fire"))          # a repeat detection: quiet
    assert alarm.command().startswith("STOP")
    alarm.on_incident("updated", _inc(2, "sos"))           # pressed again: a person asking
    assert _pattern(alarm.command()) == "SOS"


def test_one_press_counts_once_per_board():
    alarm, _, clock = _alarm()
    heard = []
    alarm.add_sos_listener(heard.append)
    assert alarm.sos_pressed("bedroom") is True
    clock.t += 1
    assert alarm.sos_pressed("bedroom") is False            # the same press over USB and Wi-Fi
    assert alarm.sos_pressed("kitchen") is True
    clock.t += 2
    assert alarm.sos_pressed("bedroom") is True
    assert heard == ["bedroom", "kitchen", "bedroom"]


def test_usb_board_reports_sos_under_its_own_name():
    alarm, settings, _ = _alarm(buzzer_port="auto")
    heard = []
    alarm.add_sos_listener(heard.append)
    board = FakeBoard(hello=b"HOMESHIELD-BUZZER 1.1 wifi=off id=grandma-room\n")
    link, _ = _link(alarm, settings, [board])
    link.start()
    try:
        assert _eventually(lambda: link.status()["connected"])
        assert link.status()["id"] == "grandma-room" and link.status()["firmware"] == "1.1"
        board.inbox.put(b"SOS\n")
        assert _eventually(lambda: heard == ["grandma-room"])
    finally:
        link.stop()


def test_a_wifi_sos_opens_a_critical_incident_named_after_the_board(app):
    admin = _login(app, "admin", "admin")
    key = _key(admin)
    board = app.test_client()
    assert board.post("/api/device/sos?id=bedroom", environ_base=LAN).status_code == 401
    assert board.post("/api/device/sos?id=bedroom", headers={"X-Device-Key": key,
                      "Cf-Connecting-Ip": "203.0.113.7"}).status_code == 404
    r = board.post("/api/device/sos?id=bedroom&fw=1.1", headers={"X-Device-Key": key},
                   environ_base=LAN)
    assert r.status_code == 200
    assert _eventually(lambda: admin.get("/api/incidents/board").get_json()["open_total"] == 1)
    inc = admin.get("/api/incidents/board").get_json()["open"][0]
    assert (inc["kind"], inc["headline_type"], inc["camera_name"], inc["severity"],
            inc["camera_id"]) == ("sos", "sos_pressed", "bedroom", "critical", None)
    poll = board.get("/api/device/poll?id=bedroom", headers={"X-Device-Key": key},
                     environ_base=LAN).get_data(as_text=True)
    assert poll.startswith("ALARM SOS ")
    assert admin.get("/api/status").get_json()["alarm"]["kind"] == "sos"


def test_the_sos_whatsapp_message_asks_for_help_without_a_confidence():
    from homeshield.notify import compose
    text = compose("opened", {"kind": "sos", "headline_type": "sos_pressed",
                              "camera_name": "bedroom", "last_ts": 1_800_000_000.0,
                              "peak_confidence": 1.0})
    assert "SOS button pressed at bedroom" in text and "confidence" not in text
