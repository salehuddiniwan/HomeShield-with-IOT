"""Security requirements checked across the whole API (S-SDLC verification):
every route needs a session (SR-05), admin routes refuse guests (SR-06),
the last admin can't be removed (SR-07), passwords are stored only as
hashes (SR-04) and SQL metacharacters are treated as data (SR-08)."""

import re
import sqlite3

import pytest

from homeshield.auth import UserStore
from homeshield.db import write_conn
from homeshield.server import create_app

PUBLIC = {"/", "/api/login", "/api/logout", "/api/password_reset_request",
          "/api/setup_state"}
STREAMS = {"/events_stream", "/video_feed/<int:cid>"}   # never read to the end here
ADMIN_ONLY = {
    ("POST", "/api/cameras"), ("DELETE", "/api/cameras/<int:cid>"),
    ("GET", "/api/detect_face/<int:cid>"), ("POST", "/api/events/clear"),
    ("GET", "/api/intruders"), ("DELETE", "/api/intruders/<int:iid>"),
    ("POST", "/api/intruders/<int:iid>/dismiss"), ("POST", "/api/intruders/<int:iid>/register"),
    ("GET", "/api/models"), ("GET", "/api/notify/status"), ("POST", "/api/notify/test"),
    ("GET", "/api/persons"), ("POST", "/api/persons"), ("DELETE", "/api/persons/<int:pid>"),
    ("GET", "/api/settings"), ("POST", "/api/settings"),
    ("POST", "/api/system/start"), ("POST", "/api/system/stop"),
    ("GET", "/api/users"), ("POST", "/api/users"), ("DELETE", "/api/users/<int:uid>"),
    ("POST", "/api/users/<int:uid>/password"), ("POST", "/api/users/<int:uid>/role"),
    ("GET", "/api/zones"), ("POST", "/api/zones"), ("DELETE", "/api/zones/<int:zid>"),
    ("GET", "/intruder_photos/<path:fname>"), ("GET", "/person_photos/<path:fname>"),
    ("POST", "/api/alarm/test"), ("GET", "/api/alarm/ports"), ("POST", "/api/alarm/device_key"),
    ("GET", "/api/alarm/device_key"),
    ("GET", "/api/clips/status"),
    ("POST", "/api/devices"), ("PATCH", "/api/devices/<int:device_id>"),
    ("DELETE", "/api/devices/<int:device_id>"), ("POST", "/api/scenes"),
    ("PUT", "/api/scenes/<int:scene_id>"), ("DELETE", "/api/scenes/<int:scene_id>"),
    ("GET", "/api/nodes"), ("GET", "/api/nodes/<node_id>"), ("PATCH", "/api/nodes/<node_id>"),
    ("DELETE", "/api/nodes/<node_id>"), ("POST", "/api/nodes/adopt"),
    ("GET", "/api/nodes/<node_id>/pins"), ("POST", "/api/nodes/<node_id>/cmd"),
    ("PATCH", "/api/sensors/<int:device_id>"), ("POST", "/api/sensors/<int:device_id>/calibrate"),
    ("PUT", "/api/sensors/<int:device_id>/response"),
    ("GET", "/api/automations"), ("POST", "/api/automations"),
    ("PUT", "/api/automations/<int:rule_id>"), ("DELETE", "/api/automations/<int:rule_id>"),
    ("POST", "/api/automations/<int:rule_id>/enabled"), ("POST", "/api/automations/<int:rule_id>/test"),
    ("PUT", "/api/automations/location"), ("POST", "/api/automations/describe"),
}


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("HOMESHIELD_SECRET", raising=False)
    db = str(tmp_path / "hs.db")
    app = create_app(db_path=db, snapshot_dir=str(tmp_path / "s"),
                     person_photos_dir=str(tmp_path / "p"),
                     intruder_photos_dir=str(tmp_path / "i"), auto_start=False)
    with write_conn(db) as conn:
        conn.execute("UPDATE users SET must_change = 0")
    app.config["DB"] = db
    return app


def _routes(app):
    for rule in app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        url = re.sub(r"<int:\w+>", "999", rule.rule)
        url = re.sub(r"<(?:path:)?\w+>", "nothing.jpg", url)
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            yield method, rule.rule, url


def _call(client, method, url):
    return client.open(url, method=method, json={} if method in ("POST", "PUT") else None)


def _signed_in(app, username, password):
    c = app.test_client()
    assert c.post("/api/login", json={"username": username, "password": password}).status_code == 200
    return c


def test_every_route_needs_a_session_except_the_public_ones(app):
    anon = app.test_client()
    seen = 0
    for method, rule, url in _routes(app):
        if rule in PUBLIC:
            continue
        r = _call(anon, method, url)
        assert r.status_code == 401, f"{method} {rule} answered {r.status_code} without a session"
        seen += 1
    assert seen >= 40


def test_guests_are_refused_every_admin_route_and_only_those(app):
    admin = _signed_in(app, "admin", "admin")
    admin.post("/api/users", json={"username": "nadia", "password": "nadia1",
                                   "role": "guest", "must_change_password": False})
    guest = _signed_in(app, "nadia", "nadia1")
    refused = set()
    for method, rule, url in _routes(app):
        if rule in PUBLIC or rule in STREAMS or rule == "/api/change_password":
            continue
        if _call(guest, method, url).status_code == 403:
            refused.add((method, rule))
    assert refused == ADMIN_ONLY


def test_the_last_admin_cannot_be_removed_or_demoted(app):
    admin = _signed_in(app, "admin", "admin")
    me = admin.get("/api/me").get_json()
    assert admin.delete(f"/api/users/{me['user_id']}").status_code == 400
    assert admin.post(f"/api/users/{me['user_id']}/role", json={"role": "guest"}).status_code == 400
    store = UserStore(app.config["DB"])
    with pytest.raises(ValueError):
        store.delete_user(me["user_id"])
    assert store.get(me["user_id"])["role"] == "admin"


def test_passwords_are_stored_only_as_salted_hashes(app):
    admin = _signed_in(app, "admin", "admin")
    admin.post("/api/users", json={"username": "nadia", "password": "correct-horse-7",
                                   "role": "guest", "must_change_password": False})
    with sqlite3.connect(app.config["DB"]) as conn:
        stored = conn.execute("SELECT password_hash FROM users WHERE username='nadia'").fetchone()[0]
    assert stored.startswith(("scrypt:", "pbkdf2:")) and "correct-horse-7" not in stored
    with open(app.config["DB"], "rb") as f:
        assert b"correct-horse-7" not in f.read()


def test_sql_metacharacters_are_treated_as_data(app):
    anon = app.test_client()
    r = anon.post("/api/login", json={"username": "admin' OR '1'='1' --", "password": "x' OR '1'='1"})
    assert r.status_code == 401
    admin = _signed_in(app, "admin", "admin")
    admin.post("/api/cameras", json={"name": "Lab", "url": "0", "location": ""})
    cam = admin.get("/api/cameras").get_json()[0]["camera_id"]
    nasty = "Dapur'); DROP TABLE zones;--"
    assert admin.post("/api/zones", json={"zone_name": nasty, "camera_id": cam,
                                          "polygon": [[0, 0], [100, 0], [100, 100]],
                                          "zone_type": "danger"}).status_code == 200
    names = [z["zone_name"] for z in admin.get("/api/zones").get_json()]
    assert nasty in names


def test_the_default_admin_must_change_the_password_before_anything_else(tmp_path, monkeypatch):
    monkeypatch.delenv("HOMESHIELD_SECRET", raising=False)
    fresh = create_app(db_path=str(tmp_path / "fresh.db"), snapshot_dir=str(tmp_path / "s"),
                       person_photos_dir=str(tmp_path / "p"),
                       intruder_photos_dir=str(tmp_path / "i"), auto_start=False)
    c = fresh.test_client()
    assert c.post("/api/login", json={"username": "admin", "password": "admin"}).status_code == 200
    blocked = c.get("/api/status")
    assert blocked.status_code in (401, 403)
    assert blocked.get_json()["error"] == "password_change_required"
    assert c.post("/api/change_password", json={"new_password": "a-better-one"}).status_code == 200
    assert c.get("/api/status").status_code == 200
