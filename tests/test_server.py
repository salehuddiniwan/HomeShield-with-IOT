"""API validation: bad input is answered with a 400 and a readable message."""

import io

import cv2
import numpy as np
import pytest

from homeshield.db import write_conn
from homeshield.server import create_app


@pytest.fixture
def app(tmp_path):
    db = str(tmp_path / "hs.db")
    app = create_app(db_path=db,
                     snapshot_dir=str(tmp_path / "snapshots"),
                     person_photos_dir=str(tmp_path / "persons"),
                     intruder_photos_dir=str(tmp_path / "intruders"),
                     auto_start=False)
    with write_conn(db) as conn:      # skip the forced first-login password change
        conn.execute("UPDATE users SET must_change = 0")
    return app


@pytest.fixture
def admin(app):
    c = app.test_client()
    r = c.post("/api/login", json={"username": "admin", "password": "admin"})
    assert r.status_code == 200
    return c


def _add_camera(client, **body):
    return client.post("/api/cameras", json=body)


def test_add_camera_requires_a_source(admin):
    for url in ("", "   ", '""'):
        r = _add_camera(admin, name="Door", url=url)
        assert r.status_code == 400
        assert "source" in r.get_json()["error"]
    assert admin.get("/api/cameras").get_json() == []


def test_add_camera_strips_quotes_and_limits_lengths(admin):
    r = _add_camera(admin, name="  Door  ", url='"0"', location="Hall")
    assert r.status_code == 200
    cam = admin.get("/api/cameras").get_json()[0]
    assert (cam["name"], cam["url"], cam["location"]) == ("Door", "0", "Hall")

    assert _add_camera(admin, name="x" * 61, url="0").status_code == 400
    assert _add_camera(admin, name="Door", url="rtsp://" + "a" * 500).status_code == 400
    assert _add_camera(admin, name="Door", url="0", location="y" * 61).status_code == 400


def test_zone_polygon_is_validated_and_clamped(admin):
    _add_camera(admin, name="Door", url="0")
    cid = admin.get("/api/cameras").get_json()[0]["camera_id"]

    def post(polygon, camera_id=cid, name="Stove"):
        return admin.post("/api/zones", json={"zone_name": name, "camera_id": camera_id,
                                              "polygon": polygon, "zone_type": "danger"})

    assert post([[0, 0], [10, 10]]).status_code == 400                 # too few points
    assert post([[0, 0], [10, "a"], [5, 5]]).status_code == 400          # not a number
    assert post("0,0 10,10 5,5").status_code == 400                      # not a list
    assert post([[0, 0], [1, 1], [2, 2]] * 30).status_code == 400        # too many points
    assert post([[0, 0], [10, 10], [5, 5]], camera_id=cid + 99).status_code == 400
    assert post([[0, 0], [10, 10], [5, 5]], name="z" * 61).status_code == 400

    r = post([[-20, 5], [900, 10], [320, 700]])
    assert r.status_code == 200
    zone = admin.get("/api/zones").get_json()[0]
    assert zone["polygon"] == [[0.0, 5.0], [640.0, 10.0], [320.0, 480.0]]


def test_person_category_and_name_are_checked_before_capture(admin):
    r = admin.post("/api/persons", json={"name": "Aisyah", "category": "pet", "camera_id": 1})
    assert r.status_code == 400 and "Category" in r.get_json()["error"]
    r = admin.post("/api/persons", json={"name": "n" * 61, "category": "adult", "camera_id": 1})
    assert r.status_code == 400 and "too long" in r.get_json()["error"]


class _FakeFaces:
    """Finds one face in the middle of any picture brighter than black."""
    available = True

    def best_face(self, frame):
        h, w = frame.shape[:2]
        if not frame.any():
            return None
        return {"x": w * 0.3, "y": h * 0.2, "w": w * 0.4, "h": h * 0.5, "age": 30,
                "det_score": 0.9, "embedding": np.ones(512, np.float32) / np.sqrt(512)}


def _photo(color=(180, 160, 140), size=(480, 640)):
    ok, buf = cv2.imencode(".jpg", np.full((*size, 3), color, np.uint8))
    assert ok
    return io.BytesIO(buf.tobytes())


def test_a_person_can_be_registered_from_a_photo(app, admin):
    app.extensions["homeshield_manager"].models.face_engine = _FakeFaces()

    r = admin.post("/api/detect_face/photo", data={"photo": (_photo(), "me.jpg")},
                   content_type="multipart/form-data")
    body = r.get_json()
    assert r.status_code == 200 and body["face"]["quality_ok"] is True
    assert (body["width"], body["height"]) == (640, 480)

    r = admin.post("/api/persons", data={"name": "Aisyah", "category": "child",
                                         "photo": (_photo(), "me.jpg")},
                   content_type="multipart/form-data")
    assert r.status_code == 200, r.get_json()
    person = admin.get("/api/persons").get_json()["persons"][0]
    assert (person["name"], person["category"]) == ("Aisyah", "child")
    assert person["photo_url"] and person["last_seen_at"] is None
    photo = admin.get(person["photo_url"])      # head and shoulders, 4:5
    h, w = cv2.imdecode(np.frombuffer(photo.data, np.uint8), cv2.IMREAD_COLOR).shape[:2]
    assert abs(w / h - 0.8) < 0.01


def test_a_registered_intruder_keeps_its_photo_and_where_it_was_seen(app, admin):
    intruders = app.extensions["homeshield_manager"].intruder_store
    iid = intruders.add(embedding=np.ones(512, np.float32), camera_id=1, camera_name="Porch",
                        frame_bgr=np.full((480, 640, 3), 120, np.uint8),
                        face_bbox=(300, 150, 60, 70))
    r = admin.post(f"/api/intruders/{iid}/register", json={"name": "Aiman", "category": "child"})
    assert r.status_code == 200, r.get_json()
    person = admin.get("/api/persons").get_json()["persons"][0]
    assert person["photo_url"] and admin.get(person["photo_url"]).status_code == 200
    assert person["last_seen_camera"] == "Porch" and person["last_seen_at"]
    assert admin.get("/api/intruders").get_json() == []


def test_a_photo_without_a_face_or_not_a_picture_is_refused(app, admin):
    app.extensions["homeshield_manager"].models.face_engine = _FakeFaces()

    def register(file, name="Aisyah"):
        return admin.post("/api/persons", data={"name": name, "category": "adult", "photo": file},
                          content_type="multipart/form-data")

    r = register((_photo(color=(0, 0, 0)), "dark.jpg"))
    assert r.status_code == 400 and "No face in this photo" in r.get_json()["error"]
    r = register((io.BytesIO(b"not a picture"), "notes.jpg"))
    assert r.status_code == 400 and "JPG or PNG" in r.get_json()["error"]
    r = admin.post("/api/persons", data={"name": "Aisyah", "category": "adult"},
                   content_type="multipart/form-data")
    assert r.status_code == 400 and "Choose a photo" in r.get_json()["error"]
    assert register((_photo(), "me.jpg"), name="").status_code == 400
    assert admin.get("/api/persons").get_json()["persons"] == []


def test_a_large_photo_is_shrunk_before_detection(app, admin):
    seen = []

    class Engine(_FakeFaces):
        def best_face(self, frame):
            seen.append(frame.shape[:2])
            return super().best_face(frame)

    app.extensions["homeshield_manager"].models.face_engine = Engine()
    r = admin.post("/api/detect_face/photo", data={"photo": (_photo(size=(3000, 4000)), "big.jpg")},
                   content_type="multipart/form-data")
    assert r.status_code == 200
    assert seen == [(1440, 1920)]


def test_guest_may_fetch_still_frames(app, admin):
    r = admin.post("/api/users", json={"username": "viewer", "password": "viewer1",
                                       "role": "guest", "must_change_password": False})
    assert r.status_code == 200
    guest = app.test_client()
    assert guest.post("/api/login", json={"username": "viewer",
                                          "password": "viewer1"}).status_code == 200
    # No such camera: 404, not 401/403.
    assert guest.get("/frame_snap/1").status_code == 404
    assert guest.post("/api/cameras", json={"url": "0"}).status_code == 403


def test_guests_never_see_camera_sources(app, admin):
    _add_camera(admin, name="Gate", url="rtsp://admin:secret@192.168.1.9:554/stream1")
    admin.post("/api/users", json={"username": "viewer", "password": "viewer1",
                                   "role": "guest", "must_change_password": False})
    guest = app.test_client()
    guest.post("/api/login", json={"username": "viewer", "password": "viewer1"})
    cams = guest.get("/api/cameras").get_json()
    assert cams and all("url" not in c for c in cams)
    status = guest.get("/api/status").get_json()
    assert all("url" not in c for c in status["cameras"].values())
    assert "secret" in admin.get("/api/cameras").get_data(as_text=True)   # admins still manage them


def test_describe_source_drops_credentials():
    from homeshield.cameras import describe_source
    assert describe_source("rtsp://admin:secret@192.168.1.9:554/stream1?x=1") == "rtsp://192.168.1.9:554"
    assert describe_source("0") == "webcam 0"
    assert describe_source("http://cam.local/video") == "http://cam.local"


def test_setup_state_reports_the_default_login_only_while_it_works(tmp_path):
    db = str(tmp_path / "fresh.db")
    app = create_app(db_path=db, snapshot_dir=str(tmp_path / "s"),
                     person_photos_dir=str(tmp_path / "p"),
                     intruder_photos_dir=str(tmp_path / "i"), auto_start=False)
    c = app.test_client()
    assert c.get("/api/setup_state").get_json()["default_admin"] is True
    c.post("/api/login", json={"username": "admin", "password": "admin"})
    assert c.post("/api/change_password", json={"new_password": "better-one"}).status_code == 200
    assert c.get("/api/setup_state").get_json()["default_admin"] is False
