"""Remote access through a Cloudflare Tunnel: who counts as a tunnel visitor,
what they are shown, and when the tunnel refuses to open."""

import pytest

from homeshield.db import write_conn
from homeshield.server import create_app
from homeshield.tunnel import QUICK_URL_RE, Tunnel

TUNNEL = {"Cf-Connecting-Ip": "203.0.113.7", "X-Forwarded-Proto": "https"}


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


def _login(client, password="admin", **kw):
    return client.post("/api/login", json={"username": "admin", "password": password}, **kw)


def _session_cookie(resp):
    return next(h for h in resp.headers.getlist("Set-Cookie") if h.startswith("session="))


def test_tunnel_visitors_get_a_secure_cookie_and_lan_browsers_do_not(app):
    remote = _session_cookie(_login(app.test_client(), headers=TUNNEL))
    assert "Secure" in remote
    lan = _session_cookie(_login(app.test_client(), environ_base={"REMOTE_ADDR": "192.168.1.20"}))
    assert "Secure" not in lan


def test_the_visitor_address_comes_from_cloudflare_only_on_loopback(app):
    c = app.test_client()
    for _ in range(4):
        _login(c, password="nope", headers=TUNNEL)
    # A different internet visitor is not locked out by the first one's mistakes.
    other = {**TUNNEL, "Cf-Connecting-Ip": "198.51.100.9"}
    r = _login(c, password="nope", headers=other)
    assert r.get_json()["attempts_left"] == 4
    # A LAN device can't pose as a tunnel visitor by sending the header.
    app.test_client().post("/api/login", json={"username": "admin", "password": "admin"},
                           headers={"Cf-Connecting-Ip": "10.9.9.9"},
                           environ_base={"REMOTE_ADDR": "192.168.1.30"})
    with write_conn(app.config["DB"]) as conn:
        ip = conn.execute("SELECT last_login_ip FROM users WHERE username='admin'").fetchone()[0]
    assert ip == "192.168.1.30"


def _admin(app):
    c = app.test_client()
    _login(c)
    return c


def test_from_the_internet_the_sign_in_page_reveals_nothing(app):
    assert app.test_client().get("/api/setup_state", headers=TUNNEL).get_json() == {
        "default_admin": False, "monitoring": None}
    lan = app.test_client().get("/api/setup_state").get_json()
    assert lan["monitoring"] is False and "default_admin" in lan


def test_quick_tunnel_visitors_are_told_to_poll(app):
    c = app.test_client()
    _login(c, headers=TUNNEL)
    assert c.get("/api/me", headers=TUNNEL).get_json()["realtime"] == "sse"   # no tunnel running
    app.extensions["homeshield_tunnel"] = Tunnel(5000, binary="")
    assert c.get("/api/me", headers=TUNNEL).get_json()["realtime"] == "poll"
    assert c.get("/api/me").get_json()["realtime"] == "sse"                    # same user on the LAN
    app.extensions["homeshield_tunnel"] = Tunnel(5000, token="t0k3n", binary="")
    assert c.get("/api/me", headers=TUNNEL).get_json()["realtime"] == "sse"   # named tunnels carry SSE


def test_admins_see_the_tunnel_state_and_guests_do_not(app):
    admin = _admin(app)
    assert admin.get("/api/status").get_json()["remote"]["mode"] is None
    app.extensions["homeshield_tunnel"] = Tunnel.blocked("default_admin")
    assert admin.get("/api/status").get_json()["remote"]["error"] == "default_admin"
    admin.post("/api/users", json={"username": "nadia", "password": "nadia1",
                                   "role": "guest", "must_change_password": False})
    guest = app.test_client()
    guest.post("/api/login", json={"username": "nadia", "password": "nadia1"})
    assert "remote" not in guest.get("/api/status").get_json()


def test_tunnel_needs_cloudflared_and_reads_the_quick_address():
    t = Tunnel(5000, binary="")
    assert t.start() is False and t.error == "not_installed" and not t.running
    line = "2026-10-10T08:00:00Z INF |  https://bright-river-cat-lamp.trycloudflare.com  |"
    assert QUICK_URL_RE.search(line).group(0) == "https://bright-river-cat-lamp.trycloudflare.com"
    assert Tunnel(5000, token="abc", binary="").mode == "named"


def test_the_tunnel_stays_shut_while_the_default_password_works(tmp_path, monkeypatch, capsys):
    import run_homeshield
    monkeypatch.delenv("HOMESHIELD_SECRET", raising=False)
    app = create_app(db_path=str(tmp_path / "fresh.db"), snapshot_dir=str(tmp_path / "s"),
                     person_photos_dir=str(tmp_path / "p"),
                     intruder_photos_dir=str(tmp_path / "i"), auto_start=False)

    class Args:
        port, tunnel_token, public_url = 5000, "", ""
    started = []
    monkeypatch.setattr(Tunnel, "start", lambda self, wait=20.0: started.append(1) or False)
    run_homeshield.start_tunnel(app, Args)
    assert not started
    assert app.extensions["homeshield_tunnel"].error == "default_admin"
    assert "NOT opened" in capsys.readouterr().out


def test_a_viewer_closing_a_stream_is_not_logged_as_a_problem():
    from homeshield.tunnel import is_real_error
    closed = ('2026-10-10T09:09:27Z ERR Request failed error="stream 89 canceled by remote with '
              'error code 0" connIndex=0 dest=https://x.trycloudflare.com/video_feed/3 type=http')
    assert not is_real_error(closed)
    assert not is_real_error("2026-10-10T09:09:27Z ERR  error=\"context canceled\" connIndex=0")
    assert is_real_error("2026-10-10T09:09:27Z ERR Unable to reach the origin service. "
                         "dial tcp 127.0.0.1:5000: connectex: No connection could be made")
    assert not is_real_error("2026-10-10T09:09:20Z INF Registered tunnel connection connIndex=0")
