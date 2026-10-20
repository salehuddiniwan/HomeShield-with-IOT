"""Incident clips: what each clip covers, real-time playback when the pipeline
is slow, sharing and extending, the time and age limits, a real H.264 file,
and how the dashboard serves them."""

import os
import time
from pathlib import Path

import cv2
import numpy as np
import pytest

from homeshield.clips import CLIP_FPS, MAX_S, ClipManager, find_encoder, target_size
from homeshield.db import init_db, read_conn, write_conn
from homeshield.events import EventBus
from homeshield.server import create_app

from test_incidents import emit

T0 = 1_800_000_000.0


class Settings(dict):
    def get(self, k, default=None):
        return super().get(k, default)


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


class FakeWriter:
    made: list = []

    def __init__(self, path, fps, size):
        self.path, self.fps, self.size, self.frames = Path(path), fps, size, 0
        FakeWriter.made.append(self)

    def write(self, img):
        assert (img.shape[1], img.shape[0]) == self.size
        self.frames += 1

    def release(self):
        self.path.write_bytes(b"\0" * (100 + self.frames))


@pytest.fixture
def env(tmp_path):
    db = str(tmp_path / "hs.db")
    init_db(db)
    FakeWriter.made = []
    settings = Settings(clips_enabled=True, clip_pre_seconds=5, clip_post_seconds=15,
                        clip_keep_days=30)
    ready = []
    clock = Clock()
    clips = ClipManager(db, tmp_path / "clips", settings, writer_factory=FakeWriter,
                        clock=clock, on_ready=lambda iids, name: ready.append((iids, name)))
    return clips, settings, db, ready, clock


def _incident(db, iid, cam=1, clip=None):
    with write_conn(db) as conn:
        conn.execute("INSERT INTO incidents (incident_id, kind, camera_id, camera_name, "
                     "headline_type, started_ts, last_ts, clip_path) VALUES "
                     "(?, 'fire', ?, 'Kitchen', 'fire_detected', ?, ?, ?)",
                     (iid, cam, T0, T0, clip))
    return {"incident_id": iid, "camera_id": cam, "kind": "fire", "clip_path": clip}


def _frame(i=0, w=1280, h=720):
    f = np.zeros((h, w, 3), np.uint8)
    cv2.putText(f, str(i), (40, 120), cv2.FONT_HERSHEY_SIMPLEX, 3, (255, 255, 255), 5)
    return f


def _feed(clips, start, end, fps=20, cam=1, size=(1280, 720)):
    frame = _frame(0, *size)
    t = start
    while t < end - 1e-9:
        clips.feed(cam, frame, t)
        t += 1.0 / fps


def _clip_of(db, iid):
    with read_conn(db) as conn:
        return conn.execute("SELECT clip_path FROM incidents WHERE incident_id = ?",
                            (iid,)).fetchone()[0]


def test_clip_size_is_small_enough_and_even():
    assert target_size(1920, 1080) == (960, 540)
    assert target_size(640, 480) == (640, 480)
    assert target_size(1001, 563) == (960, 540)
    assert target_size(639, 359) == (638, 358)


def test_a_clip_starts_before_the_incident_and_ends_after_it(env):
    clips, _, db, ready, _ = env
    _feed(clips, T0, T0 + 8)                        # the camera was already running
    inc = _incident(db, 1)
    clips.on_incident("opened", {**inc, "last_ts": T0 + 8})
    assert clips.recording(1)
    _feed(clips, T0 + 8, T0 + 30)
    assert clips.flush()
    assert not clips.recording(1)
    (w,) = FakeWriter.made
    assert w.size == (960, 540) and w.fps == CLIP_FPS
    assert 195 <= w.frames <= 205                   # 5 s before + 15 s after, at 10 fps
    name = _clip_of(db, 1)
    assert name and (clips.dir / name).exists()
    assert ready == [([1], name)]


def test_a_slow_pipeline_still_plays_in_real_time(env):
    clips, _, db, _, _ = env
    _feed(clips, T0, T0 + 5, fps=4)
    clips.on_incident("opened", {**_incident(db, 1), "last_ts": T0 + 5})
    _feed(clips, T0 + 5, T0 + 25, fps=4)
    assert clips.flush()
    assert 190 <= FakeWriter.made[0].frames <= 210  # 20 s at 10 fps, frames repeated


def test_later_detections_extend_it_and_another_incident_shares_it(env):
    clips, _, db, _, _ = env
    first = _incident(db, 1)
    _feed(clips, T0, T0 + 5)
    clips.on_incident("opened", {**first, "last_ts": T0 + 5})
    _feed(clips, T0 + 5, T0 + 15)
    clips.on_incident("updated", {**first, "last_ts": T0 + 15})
    clips.on_incident("opened", {**_incident(db, 2), "last_ts": T0 + 18})
    _feed(clips, T0 + 15, T0 + 45)
    assert clips.flush()
    (w,) = FakeWriter.made
    assert 325 <= w.frames <= 335                   # T0 .. T0+33 (18 + 15)
    assert _clip_of(db, 1) == _clip_of(db, 2) is not None


def test_a_long_incident_is_capped(env):
    clips, _, db, _, _ = env
    inc = _incident(db, 1)
    clips.on_incident("opened", {**inc, "last_ts": T0})
    for k in range(40):                             # a fire seen every 5 s for 200 s
        _feed(clips, T0 + k * 5, T0 + (k + 1) * 5, fps=10, size=(320, 180))
        clips.on_incident("updated", {**inc, "last_ts": T0 + (k + 1) * 5})
    assert clips.flush()
    assert abs(FakeWriter.made[0].frames - MAX_S * CLIP_FPS) <= 5


def test_repeats_alone_never_start_a_clip_and_an_incident_keeps_its_first(env):
    clips, _, db, _, _ = env
    _feed(clips, T0, T0 + 2)
    clips.on_incident("updated", {**_incident(db, 1), "last_ts": T0 + 2})
    clips.on_incident("escalated", {**_incident(db, 2, clip="old.mp4"), "last_ts": T0 + 2})
    assert not clips.recording(1) and not clips.recording(2)
    clips.on_incident("escalated", {**_incident(db, 3), "last_ts": T0 + 2})
    assert clips.recording(3)


def test_a_camera_that_goes_quiet_still_gets_its_clip(env):
    clips, settings, db, _, clock = env
    _feed(clips, T0, T0 + 5)
    clips.on_incident("opened", {**_incident(db, 1), "last_ts": T0 + 5})
    _feed(clips, T0 + 5, T0 + 8)                    # then the camera drops out
    clips.check_deadlines(now=clock() + 10)
    assert clips.recording(1)                       # not yet
    clips.check_deadlines(now=clock() + settings["clip_post_seconds"] + 6)
    assert clips.flush() and not clips.recording(1)
    assert 75 <= FakeWriter.made[0].frames <= 85 and _clip_of(db, 1)


def test_turned_off_it_records_nothing(env):
    clips, settings, db, _, _ = env
    settings["clips_enabled"] = False
    _feed(clips, T0, T0 + 5)
    clips.on_incident("opened", {**_incident(db, 1), "last_ts": T0 + 5})
    assert not clips.recording(1) and not clips._buffers


def test_old_and_unclaimed_clips_are_deleted(env):
    clips, settings, db, _, clock = env
    for name, age_days in (("old.mp4", 40), ("recent.mp4", 2), ("orphan.mp4", 1)):
        f = clips.dir / name
        f.write_bytes(b"x")
        t = clock() - age_days * 86400
        os.utime(f, (t, t))
    _incident(db, 1, clip="old.mp4")
    _incident(db, 2, clip="recent.mp4")
    assert clips.cleanup() == 2
    assert sorted(p.name for p in clips.dir.glob("*.mp4")) == ["recent.mp4"]
    assert _clip_of(db, 1) is None and _clip_of(db, 2) == "recent.mp4"


def test_the_real_encoder_writes_an_mp4_that_plays_in_real_time(tmp_path):
    encoder, name = find_encoder(tmp_path)
    if encoder is None:
        pytest.skip(name)
    db = str(tmp_path / "hs.db")
    init_db(db)
    clips = ClipManager(db, tmp_path / "clips", Settings(clip_pre_seconds=2, clip_post_seconds=5))
    assert clips.available()
    start = time.time()
    _feed(clips, start, start + 2, fps=15)
    clips.on_incident("opened", {**_incident(db, 1), "last_ts": start + 2})
    _feed(clips, start + 2, start + 8, fps=15)
    assert clips.flush(30)
    path = clips.dir / _clip_of(db, 1)
    cap = cv2.VideoCapture(str(path))
    frames, fps = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)), cap.get(cv2.CAP_PROP_FPS)
    cap.release()
    assert path.read_bytes()[4:8] == b"ftyp"        # an MP4 container
    assert round(fps) == CLIP_FPS and 65 <= frames <= 75   # 7 s


# ---- the dashboard -----------------------------------------------------------

@pytest.fixture
def app(tmp_path):
    db = str(tmp_path / "api.db")
    app = create_app(db_path=db, snapshot_dir=str(tmp_path / "s"),
                     person_photos_dir=str(tmp_path / "p"),
                     intruder_photos_dir=str(tmp_path / "i"), auto_start=False)
    with write_conn(db) as conn:
        conn.execute("UPDATE users SET must_change = 0")
    app.config["DB"] = db
    return app


def _login(app):
    c = app.test_client()
    assert c.post("/api/login", json={"username": "admin", "password": "admin"}).status_code == 200
    return c


def test_clips_are_served_to_signed_in_users_with_seeking(app, tmp_path):
    clips = app.extensions["homeshield_clips"]
    assert clips.dir == (tmp_path / "clips").resolve()   # beside the snapshots
    ev = emit(EventBus(db_path=app.config["DB"]), "fire_detected", time.time())
    (clips.dir / "c1.mp4").write_bytes(b"\0\0\0\x18ftypmp42" + b"\0" * 2000)
    with write_conn(app.config["DB"]) as conn:
        conn.execute("UPDATE incidents SET clip_path = 'c1.mp4' WHERE incident_id = ?",
                     (ev.incident_id,))
    assert app.test_client().get("/clips/c1.mp4").status_code == 401
    admin = _login(app)
    with admin.get("/clips/c1.mp4") as r:
        assert r.status_code == 200 and r.mimetype == "video/mp4"
    with admin.get("/clips/c1.mp4", headers={"Range": "bytes=0-99"}) as r:
        assert r.status_code == 206 and len(r.data) == 100
    with admin.get("/clips/c1.mp4?download=1") as r:
        assert "attachment" in r.headers["Content-Disposition"]
    detail = admin.get(f"/api/incidents/{ev.incident_id}").get_json()
    assert detail["clip_path"] == "c1.mp4" and detail["clip_recording"] is False

    admin.post("/api/events/clear")
    assert not list(clips.dir.glob("*.mp4"))         # clearing history deletes clips too


def test_clip_settings_are_checked(app):
    admin = _login(app)
    for bad in ({"clip_pre_seconds": 11}, {"clip_post_seconds": 4}, {"clip_keep_days": 0},
                {"clip_post_seconds": "soon"}):
        assert admin.post("/api/settings", json=bad).status_code == 400, bad
    saved = admin.post("/api/settings", json={"clips_enabled": "false", "clip_pre_seconds": "3",
                                              "clip_post_seconds": 20, "clip_keep_days": 7}).get_json()
    assert (saved["clips_enabled"], saved["clip_pre_seconds"], saved["clip_post_seconds"],
            saved["clip_keep_days"]) == (False, 3, 20, 7)
    st = admin.get("/api/clips/status").get_json()
    assert st["enabled"] is False and st["count"] == 0 and st["keep_days"] == 7
