"""CPU savings: the live view's own frame rate and size, decoding on the graphics card with
a fall-back, and the frame-rate settings (a 10 fps default; old migrations run once)."""
import threading
import time
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from homeshield.cameras import CaptureWorker, LatestFrame
from homeshield.db import init_db, write_conn
from homeshield.server import create_app
from homeshield.settings import DEFAULTS, SettingsStore


def _width(jpeg: bytes) -> int:
    return cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR).shape[1]


def test_the_live_picture_is_shrunk_to_the_width_cap():
    width = {"v": 960}
    lf = LatestFrame(max_width=lambda: width["v"])
    lf.set(np.zeros((1080, 1920, 3), np.uint8))
    assert _width(lf.jpeg()) == 960
    width["v"] = 0                                    # the camera's full size
    lf.set(np.zeros((1080, 1920, 3), np.uint8))
    assert _width(lf.jpeg()) == 1920
    width["v"] = 960
    lf.set(np.zeros((480, 640, 3), np.uint8))         # never enlarged
    assert _width(lf.jpeg()) == 640


def test_new_installs_analyse_10_frames_a_second(tmp_path):
    db = str(tmp_path / "t.db")
    init_db(db)
    s = SettingsStore(db)
    assert s.get("process_fps") == 10 == DEFAULTS["process_fps"]
    assert (s.get("stream_fps"), s.get("stream_max_width"), s.get("hw_decode")) == (10, 960, True)


def test_the_old_frame_rate_migration_runs_once(tmp_path):
    db = str(tmp_path / "t.db")
    init_db(db)
    SettingsStore(db).update({"process_fps": 15})
    with write_conn(db) as conn:                      # an install from before migrations were recorded
        conn.execute("DELETE FROM settings WHERE key = 'migrations_done'")
    assert SettingsStore(db).get("process_fps") == 0
    SettingsStore(db).update({"process_fps": 15})     # picked again later: it stays
    assert SettingsStore(db).get("process_fps") == 15


def _video(path, frames=12):
    out = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10, (64, 48))
    for i in range(frames):
        out.write(np.full((48, 64, 3), i * 10, np.uint8))
    out.release()
    return path


def _worker(url, **settings):
    pipe = SimpleNamespace(settings=SimpleNamespace(get=lambda k, d=None: settings.get(k, d)))
    return CaptureWorker(camera={"camera_id": 1, "name": "Test", "url": str(url)}, pipeline=pipe,
                         bus=None, latest=LatestFrame())


def test_decoding_on_the_graphics_card_can_be_switched_off(tmp_path):
    w = _worker(_video(tmp_path / "v.mp4"), hw_decode=False)
    cap = w._open()
    try:
        assert cap is not None and not w.status.hw_decode
        assert cap.read()[0]
    finally:
        cap.release()


def test_a_camera_opens_whether_or_not_the_card_can_decode_it(tmp_path):
    w = _worker(_video(tmp_path / "v.mp4"), hw_decode=True)
    cap = w._open()                                   # on the card when it can, else the usual way
    try:
        assert cap is not None and cap.read()[0]
    finally:
        cap.release()
    w._hw_off = True                                  # the card gave no frames before: the CPU from now on
    cap = w._open()
    try:
        assert cap is not None and not w.status.hw_decode
    finally:
        cap.release()


@pytest.fixture
def app(tmp_path):
    path = str(tmp_path / "hs.db")
    app = create_app(db_path=path, snapshot_dir=str(tmp_path / "s"), person_photos_dir=str(tmp_path / "p"),
                     intruder_photos_dir=str(tmp_path / "i"), auto_start=False)
    with write_conn(path) as conn:
        conn.execute("UPDATE users SET must_change = 0")
    return app


def test_each_viewer_gets_at_most_stream_fps_frames(app):
    c = app.test_client()
    assert c.post("/api/login", json={"username": "admin", "password": "admin"}).status_code == 200
    assert c.post("/api/settings", json={"stream_fps": 10}).status_code == 200
    manager = app.extensions["homeshield_manager"]
    lf = LatestFrame()
    manager._frames[99] = lf
    stop = threading.Event()

    def camera():                                     # a 50 fps camera
        while not stop.is_set():
            lf.set(np.zeros((48, 64, 3), np.uint8))
            time.sleep(0.02)
    threading.Thread(target=camera, daemon=True).start()
    try:
        r = c.get("/video_feed/99", buffered=False)
        frames, t0 = 0, time.time()
        for chunk in r.response:
            frames += chunk.count(b"--frame")
            if time.time() - t0 > 1.5:
                break
        rate = frames / (time.time() - t0)
        assert 6 <= rate <= 12                         # ~10 a second, not the camera's 50
    finally:
        stop.set()
        manager._frames.pop(99, None)


def test_the_decoder_follows_what_worked(tmp_path):
    w = _worker(tmp_path / "none.mp4", hw_decode=True)
    w.status.hw_decode = True
    w._choose_decoder(got_frames=True)
    assert not w._hw_off                              # frames on the card: stay on it
    w._choose_decoder(got_frames=False)
    assert w._hw_off                                  # none on the card: the processor next
    w.status.hw_decode = False
    w._choose_decoder(got_frames=True)
    assert w._hw_off                                  # the processor works: keep it
    w._choose_decoder(got_frames=False)
    assert not w._hw_off                              # nothing either way: the camera, not the card


def test_an_offline_network_camera_is_given_up_on_quickly():
    # 192.0.2.10 is a documentation address: nothing answers there.
    for hw in (True, False):
        w = _worker("rtsp://192.0.2.10/stream1", hw_decode=hw)
        t = time.time()
        assert w._open() is None
        assert time.time() - t < 15                   # not OpenCV's 30 s, and not twice over
