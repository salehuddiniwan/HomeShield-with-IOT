"""Registered people: when a camera last recognised them, and their portrait photo."""

import sqlite3

import cv2
import numpy as np

from homeshield.db import init_db, read_conn
from homeshield.persons import IntruderStore, PersonStore, save_portrait


def _emb(axis=0):
    return np.eye(512, dtype=np.float32)[axis]


def _stored_seen(db, pid):
    with read_conn(db) as conn:
        r = conn.execute("SELECT last_seen_at, last_seen_camera FROM persons "
                         "WHERE person_id = ?", (pid,)).fetchone()
    return r["last_seen_at"], r["last_seen_camera"]


def test_last_seen_is_listed_at_once_but_saved_once_a_minute(tmp_path):
    db = str(tmp_path / "hs.db")
    init_db(db)
    store = PersonStore(db, tmp_path / "persons")
    pid = store.add(name="Siti", category="elderly", embedding=_emb())["person_id"]
    assert store.list()[0]["last_seen_at"] is None

    store.mark_seen(pid, "Living room", at=1000.0)
    assert _stored_seen(db, pid) == (1000.0, "Living room")
    store.mark_seen(pid, "Kitchen", at=1030.0)          # within the minute: listed, not saved
    p = store.list()[0]
    assert (p["last_seen_at"], p["last_seen_camera"]) == (1030.0, "Kitchen")
    assert _stored_seen(db, pid) == (1000.0, "Living room")
    store.reload()                                      # a reload keeps the newer sighting
    assert store.list()[0]["last_seen_camera"] == "Kitchen"

    restarted = PersonStore(db, tmp_path / "persons")
    assert restarted.list()[0]["last_seen_at"] == 1000.0
    store.mark_seen(pid, "Hall", at=1061.0)
    assert _stored_seen(db, pid) == (1061.0, "Hall")

    store.mark_seen(pid + 99, "Hall", at=1200.0)        # nobody by that id: ignored
    store.delete(pid)
    assert store.list() == [] and not store._seen


def test_an_older_database_gets_the_last_seen_columns(tmp_path):
    db = str(tmp_path / "old.db")
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE persons (person_id INTEGER PRIMARY KEY AUTOINCREMENT, "
                     "name TEXT NOT NULL, category TEXT NOT NULL DEFAULT 'adult', "
                     "photo_path TEXT, embedding_blob BLOB, "
                     "created_at TEXT NOT NULL DEFAULT (datetime('now')))")
        conn.execute("INSERT INTO persons (name) VALUES ('Siti')")
    init_db(db)
    store = PersonStore(db, tmp_path / "persons")
    store.mark_seen(1, "Porch", at=50.0)
    assert _stored_seen(db, 1) == (50.0, "Porch")


def test_a_portrait_is_head_and_shoulders_four_by_five(tmp_path):
    frame = np.full((720, 1280, 3), 90, np.uint8)

    def size(bbox):
        path = tmp_path / "p.jpg"
        assert save_portrait(frame, bbox, path)
        h, w = cv2.imread(str(path)).shape[:2]
        return w, h

    assert size((600, 200, 80, 100)) == (256, 320)      # 3.2 face heights tall, 4:5
    assert size((1220, 300, 60, 80)) == (205, 256)      # at the right edge: slides back in
    assert size((300, 100, 500, 560)) == (400, 500)     # a close-up: shrunk to fit, then 400 wide


def test_registering_an_intruder_keeps_its_photo_and_sighting(tmp_path):
    db = str(tmp_path / "hs.db")
    init_db(db)
    people = PersonStore(db, tmp_path / "persons")
    intruders = IntruderStore(db, tmp_path / "intruders")
    iid = intruders.add(embedding=_emb(3), camera_id=1, camera_name="Front door",
                        frame_bgr=np.full((480, 640, 3), 120, np.uint8),
                        face_bbox=(300, 150, 60, 70))
    rec = intruders.get(iid)
    assert rec["photo_path"] and rec["detected_ts"]
    info = people.add(name="Aiman", category="child", embedding=rec["embedding"],
                      photo_from=rec["photo_path"], seen_at=rec["detected_ts"],
                      seen_camera=rec["camera_name"])
    intruders.delete(iid)
    p = people.list()[0]
    assert info["photo_url"] and p["photo_url"] == info["photo_url"]
    assert (tmp_path / "persons" / p["photo_url"].rsplit("/", 1)[1]).is_file()
    assert p["last_seen_camera"] == "Front door"
    assert abs(p["last_seen_at"] - rec["detected_ts"]) < 1
