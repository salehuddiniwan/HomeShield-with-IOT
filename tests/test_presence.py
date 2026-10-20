"""Who is using the dashboard: online now, on which devices, last seen, and
who may see it."""

import pytest

from homeshield.db import init_db, read_conn, write_conn
from homeshield.presence import ONLINE_S, PERSIST_S, Presence, device_kind
from homeshield.server import create_app

PHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) Mobile/15E148 Safari/604.1"
LAPTOP = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/150.0 Safari/537.36"


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def tracker(tmp_path):
    db = str(tmp_path / "hs.db")
    init_db(db)
    with write_conn(db) as conn:
        conn.execute("INSERT INTO users (user_id, username, password_hash, role) "
                     "VALUES (1, 'admin', 'x', 'admin'), (2, 'nadia', 'x', 'guest'), "
                     "(3, 'atuk', 'x', 'guest')")
    clock = Clock()
    return Presence(db, clock=clock), clock, db


USERS = [{"user_id": 1, "username": "admin", "role": "admin"},
         {"user_id": 2, "username": "nadia", "role": "guest"},
         {"user_id": 3, "username": "atuk", "role": "guest", "last_seen_at": 1_700_000_000.0}]


def _by_name(snap):
    return {p["username"]: p for p in snap}


def test_phones_and_computers_are_told_apart():
    assert device_kind(PHONE) == "phone"
    assert device_kind(LAPTOP) == "computer"
    assert device_kind("") == "computer"


def test_one_account_on_two_devices_is_online_until_it_goes_quiet(tracker):
    p, clock, _ = tracker
    p.touch(2, "s-phone", user_agent=PHONE)
    clock.t += 5
    p.touch(2, "s-laptop", user_agent=LAPTOP, remote=True)
    now = _by_name(p.snapshot(USERS, me=1))
    assert now["nadia"]["online"] and now["nadia"]["devices"] == ["computer", "phone"]
    assert now["nadia"]["sessions"] == 2 and now["nadia"]["remote"]
    assert now["nadia"]["device_counts"] == {"computer": 1, "phone": 1}
    assert not now["admin"]["online"] and now["admin"]["you"]
    last = clock.t
    clock.t += ONLINE_S + 1
    later = _by_name(p.snapshot(USERS))
    assert not later["nadia"]["online"] and later["nadia"]["last_seen_at"] == last
    assert later["atuk"]["last_seen_at"] == 1_700_000_000.0        # from the database


def test_online_accounts_come_first_then_the_most_recently_seen(tracker):
    p, clock, _ = tracker
    p.touch(1, "a", user_agent=LAPTOP)
    clock.t += ONLINE_S + 5
    p.touch(3, "c", user_agent=PHONE)
    order = [x["username"] for x in p.snapshot(USERS)]
    assert order == ["atuk", "admin", "nadia"]


def test_a_background_tab_is_online_but_not_watching(tracker):
    p, _, _ = tracker
    p.touch(2, "s", visible=False)
    p.touch(2, "s")                          # an ordinary request keeps the last answer
    assert not _by_name(p.snapshot(USERS))["nadia"]["watching"]
    p.touch(2, "s", visible=True)
    assert _by_name(p.snapshot(USERS))["nadia"]["watching"]


def test_last_seen_is_saved_at_most_once_a_minute_and_signing_out_forgets(tracker):
    p, clock, db = tracker

    def saved():
        with read_conn(db) as conn:
            return conn.execute("SELECT last_seen_at FROM users WHERE user_id = 2").fetchone()[0]
    p.touch(2, "s", ip="192.168.1.9")
    first = saved()
    assert first == clock.t
    clock.t += 30
    p.touch(2, "s")
    assert saved() == first
    clock.t += PERSIST_S
    p.touch(2, "s")
    assert saved() == clock.t
    p.forget("s")
    assert not _by_name(p.snapshot(USERS))["nadia"]["online"]


# ---- through the dashboard ---------------------------------------------------

@pytest.fixture
def app(tmp_path):
    db = str(tmp_path / "api.db")
    app = create_app(db_path=db, snapshot_dir=str(tmp_path / "s"),
                     person_photos_dir=str(tmp_path / "p"),
                     intruder_photos_dir=str(tmp_path / "i"), auto_start=False)
    with write_conn(db) as conn:
        conn.execute("UPDATE users SET must_change = 0")
    return app


def _login(app, user, pw, ua=LAPTOP, headers=None):
    c = app.test_client()
    r = c.post("/api/login", json={"username": user, "password": pw},
               headers={"User-Agent": ua, **(headers or {})})
    assert r.status_code == 200
    return c


def test_admins_see_who_is_online_and_guests_do_not(app):
    admin = _login(app, "admin", "admin")
    admin.post("/api/users", json={"username": "nadia", "password": "nadia1",
                                   "role": "guest", "must_change_password": False})
    guest = _login(app, "nadia", "nadia1", ua=PHONE)
    guest.get("/api/status?seen=hidden", headers={"User-Agent": PHONE})
    st = guest.get("/api/status", headers={"User-Agent": PHONE}).get_json()
    assert "presence" not in st

    who = _by_name(admin.get("/api/status?seen=visible").get_json()["presence"])
    assert who["admin"]["you"] and who["admin"]["online"] and who["admin"]["watching"]
    assert who["nadia"]["online"] and who["nadia"]["devices"] == ["phone"]
    assert not who["nadia"]["watching"]                    # her tab is in the background
    users = {u["username"]: u for u in admin.get("/api/users").get_json()["users"]}
    assert users["nadia"]["online"] and users["nadia"]["last_seen_at"]

    guest.post("/api/logout")
    who = _by_name(admin.get("/api/status").get_json()["presence"])
    assert not who["nadia"]["online"] and who["nadia"]["last_seen_at"]


def test_a_visit_through_the_tunnel_is_marked_remote(app):
    admin = _login(app, "admin", "admin")
    away = _login(app, "admin", "admin", headers={"Cf-Connecting-Ip": "203.0.113.7",
                                                  "User-Agent": PHONE})
    away.get("/api/status", headers={"Cf-Connecting-Ip": "203.0.113.7", "User-Agent": PHONE})
    me = _by_name(admin.get("/api/status").get_json()["presence"])["admin"]
    assert me["sessions"] == 2 and me["remote"] and me["devices"] == ["computer", "phone"]
