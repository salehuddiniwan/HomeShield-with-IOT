"""CameraPipeline behaviour with stand-in models (no GPU or weights needed)."""

import threading
import time
from types import SimpleNamespace

import numpy as np

import homeshield.pipeline as pipeline_mod
from homeshield.pipeline import CameraPipeline, FaceWorker


# ---- a pose model per camera ----------------------------------------------

class _PoseModel:
    """Stands in for an Ultralytics model, which keeps its tracker on itself."""

    def __init__(self):
        self.cameras_seen = []

    def track(self, frame, **kw):
        self.cameras_seen.append(int(frame[0, 0, 0]))
        return [SimpleNamespace(boxes=None, keypoints=None)]


def _models(face=None):
    built = []

    def new_pose_model():
        built.append(_PoseModel())
        return built[-1]

    return SimpleNamespace(new_pose_model=new_pose_model, pose_gen=1, built=built,
                           fire_model=None, face_engine=face, device="cpu", half=False,
                           pose_weights_path="", infer_lock=threading.Lock())


def test_each_camera_runs_its_own_pose_model_and_sees_only_its_frames():
    models = _models()
    settings = {"fire_enabled": False}
    cams = {cid: CameraPipeline(camera_id=cid, camera_name=str(cid),
                                models=models, settings=settings) for cid in (1, 2, 3)}

    def frame_round():
        for cid, pipe in cams.items():
            pipe.process(np.full((8, 8, 3), cid, np.uint8), ts=time.time())

    for _ in range(4):
        frame_round()
    assert len(models.built) == 3                       # one copy each, built once
    for cid, pipe in cams.items():
        assert pipe._pose.cameras_seen == [cid] * 4
    # A new pose choice in Settings: every camera builds a new copy on its next frame.
    models.pose_gen = 2
    frame_round()
    assert len(models.built) == 6
    assert all(pipe._pose.cameras_seen == [cid] for cid, pipe in cams.items())


# ---- face worker: quality gate + intruder confirmation --------------------

class _Engine:
    available = True

    def __init__(self, faces):
        self.faces = faces

    def detect(self, frame):
        return [dict(f) for f in self.faces]


class _People:
    def __init__(self): self.seen = []
    def gallery(self): return [(1, np.eye(512, dtype=np.float32)[0])]
    def name_of(self, pid): return "Alice"
    def category_of(self, pid): return "elderly"
    def mark_seen(self, pid, camera_name): self.seen.append((pid, camera_name))


class _Bus:
    def __init__(self): self.events = []
    def publish(self, ev, frame=None): self.events.append(ev)


def _face(axis, **kw):
    f = {"x": 10.0, "y": 10.0, "w": 80.0, "h": 90.0, "det_score": 0.9, "age": 30,
         "embedding": np.eye(512, dtype=np.float32)[axis]}
    f.update(kw)
    return f


def _run_worker(faces, cycles, people=None, **settings):
    bus = _Bus()
    w = FaceWorker(camera_id=1, camera_name="t", models=_models(face=_Engine(faces)),
                   settings={"intruder_confirm_frames": 2, **settings},
                   person_store=people or _People(), intruder_store=None, bus=bus)
    w.start()
    states = []
    try:
        for i in range(cycles):
            w.submit(np.zeros((8, 8, 3), np.uint8), ts=100.0 + i)
            deadline = time.time() + 2.0
            while w._inbox.qsize() and time.time() < deadline:
                time.sleep(0.005)
            time.sleep(0.05)
            states.append(w.intruder_active())
    finally:
        w.request_stop()
    return states, bus.events, w.latest_faces()


def test_unknown_face_needs_two_cycles_before_intruder_alert():
    states, events, _ = _run_worker([_face(axis=5)], cycles=3)
    assert states == [False, True, True]
    assert [e.event_type for e in events] == ["intruder_detected"]


def test_known_face_is_named_and_never_an_intruder():
    people = _People()
    states, events, faces = _run_worker([_face(axis=0), _face(axis=5)], cycles=3,
                                        people=people, intruder_confirm_frames=9)
    assert not any(states) and not events
    assert faces[0]["match_name"] == "Alice"
    # each match tells the card where they were seen; the unknown face doesn't
    assert people.seen == [(1, "t")] * 3


def test_small_or_unsure_faces_are_not_intruders():
    states, events, faces = _run_worker(
        [_face(axis=5, w=20.0, h=24.0), _face(axis=6, det_score=0.3)], cycles=3)
    assert not any(states) and not events
    assert [f["quality_ok"] for f in faces] == [False, False]
