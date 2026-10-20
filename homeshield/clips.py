"""Incident clips: a short video of each incident, from a few seconds before
it started until a few seconds after its last detection.

The capture thread hands every annotated frame to feed(). A ring buffer per
camera keeps the last few seconds, downscaled and stored as JPEG at about 10
frames a second, so a clip can begin *before* the detection that opened the
incident. When an incident opens, a job takes that buffer and keeps
collecting frames until POST seconds after the camera's latest detection
(capped at MAX_S in all). A writer thread turns the frames into an H.264
MP4, at a steady frame rate: when the pipeline ran slower than that, frames
are repeated, so a clip always plays in real time.

H.264 comes from Windows' own encoder (Media Foundation), so nothing extra is
installed; elsewhere OpenCV's FFmpeg build is tried, which needs OpenH264.
If neither works, clips stay off and Settings says why. MPEG-4 Part 2
('mp4v'), which OpenCV can always write, is not used: browsers can't play it.

Clips live in their own folder, named in incidents.clip_path, and are
deleted after the number of days set in Settings.
"""

from __future__ import annotations

import logging
import os
import queue
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import cv2
import numpy as np

from .db import read_conn, write_conn

log = logging.getLogger(__name__)

CLIP_FPS = 10
MAX_W = 960                  # wide enough to see a face, small enough to keep
JPEG_QUALITY = 75
MAX_S = 90                   # longest clip, however long the incident goes on
GRACE_S = 5.0                # a camera gone silent: finish its clip this long after
MAX_FILL_S = 3.0             # longest stall bridged by repeating a frame
ORPHAN_AGE_S = 3600.0        # a file no incident names (e.g. after a crash)

# (OpenCV backend, fourcc, name shown in Settings), tried in this order.
ENCODERS = ((cv2.CAP_MSMF, "H264", "H.264 via Windows Media Foundation"),
            (cv2.CAP_FFMPEG, "avc1", "H.264 via FFmpeg and OpenH264"))
NO_ENCODER = ("No H.264 encoder found. On Windows N editions, install the Media "
              "Feature Pack (Settings > Apps > Optional features).")


def _even(n: float) -> int:
    return max(2, int(n) // 2 * 2)


def target_size(width: int, height: int, max_w: int = MAX_W) -> tuple[int, int]:
    """The clip size for a camera: at most max_w wide, even sides (H.264)."""
    if width > max_w:
        height, width = height * max_w / width, max_w
    return _even(width), _even(round(height))


def find_encoder(probe_dir: Optional[Path] = None) -> tuple[Optional[tuple[int, str]], str]:
    """Write a tiny test video with each encoder; the first that works wins.
    The test file goes to the system's temp folder, not among the clips."""
    probe_dir = Path(probe_dir or tempfile.gettempdir())
    frame = np.zeros((64, 64, 3), np.uint8)
    for api, fourcc, name in ENCODERS:
        path = probe_dir / f"homeshield-probe-{os.getpid()}-{threading.get_ident()}-{fourcc}.mp4"
        ok = False
        try:
            writer = cv2.VideoWriter(str(path), api, cv2.VideoWriter_fourcc(*fourcc),
                                     CLIP_FPS, (64, 64))
            ok = writer.isOpened()
            if ok:
                for _ in range(3):
                    writer.write(frame)
            writer.release()
            ok = ok and path.exists() and path.stat().st_size > 0
        except Exception as e:
            log.debug("clip encoder %s failed: %s", name, e)
        finally:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        if ok:
            return (api, fourcc), name
    return None, NO_ENCODER


_found: Optional[tuple[Optional[tuple[int, str]], str]] = None
_found_lock = threading.Lock()


def cached_encoder() -> tuple[Optional[tuple[int, str]], str]:
    """find_encoder(), once per run: the answer can't change while it runs."""
    global _found
    with _found_lock:
        if _found is None:
            _found = find_encoder()
            if _found[0] is None:
                log.warning("Incident clips are off: %s", _found[1])
            else:
                log.info("Incident clips: %s", _found[1])
        return _found


@dataclass(eq=False)
class _Job:
    camera_id: int
    name: str
    start_ts: float
    end_ts: float
    deadline: float                      # wall clock: finish even with no frames
    incidents: set = field(default_factory=set)
    writer: Any = None
    size: Optional[tuple[int, int]] = None
    prev: Optional[np.ndarray] = None
    next_tick: float = 0.0
    frames: int = 0
    failed: bool = False
    cancelled: bool = False

    @property
    def max_end(self) -> float:
        return self.start_ts + MAX_S


class ClipManager:
    def __init__(self, db_path: str, clips_dir, settings, *,
                 on_ready: Optional[Callable[[list[int], str], None]] = None,
                 writer_factory: Optional[Callable[[Path, float, tuple[int, int]], Any]] = None,
                 clock: Callable[[], float] = time.time):
        self.db_path = db_path
        self.dir = Path(clips_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.settings = settings
        self._on_ready = on_ready
        self._writer_factory = writer_factory
        self._clock = clock
        self._lock = threading.Lock()
        self._buffers: dict[int, deque] = {}       # camera -> (ts, jpeg) for the last PRE s
        self._last_kept: dict[int, float] = {}
        self._jobs: dict[int, _Job] = {}           # camera -> the clip being recorded
        self._q: queue.Queue = queue.Queue()
        self._dropped = 0
        self._encoder: Optional[tuple[int, str]] = None
        self.encoder_name: Optional[str] = "test writer" if writer_factory else None
        self.error: Optional[str] = None
        threading.Thread(target=self._writer_loop, name="hs-clips", daemon=True).start()
        threading.Thread(target=self._housekeeping, name="hs-clips-house", daemon=True).start()

    # ---- settings -----------------------------------------------------------

    def _int(self, key: str, default: int, lo: int, hi: int) -> int:
        try:
            return min(hi, max(lo, int(self.settings.get(key, default))))
        except (TypeError, ValueError):
            return default

    def enabled(self) -> bool:
        return bool(self.settings.get("clips_enabled", True))

    def pre_seconds(self) -> int:
        return self._int("clip_pre_seconds", 5, 0, 10)

    def post_seconds(self) -> int:
        return self._int("clip_post_seconds", 15, 5, 60)

    def keep_days(self) -> int:
        return self._int("clip_keep_days", 30, 1, 365)

    # ---- encoder ------------------------------------------------------------

    def available(self) -> bool:
        """Whether this computer can write clips that browsers play."""
        if self._writer_factory is not None:
            return True
        if self._encoder is None and self.error is None:
            encoder, text = cached_encoder()
            if encoder is None:
                self.error = text
            else:
                self._encoder, self.encoder_name = encoder, text
        return self._encoder is not None

    def _open_writer(self, path: Path, size: tuple[int, int]):
        if self._writer_factory is not None:
            return self._writer_factory(path, CLIP_FPS, size)
        api, fourcc = self._encoder
        writer = cv2.VideoWriter(str(path), api, cv2.VideoWriter_fourcc(*fourcc),
                                 CLIP_FPS, size)
        return writer if writer.isOpened() else None

    # ---- frames in (capture threads) ------------------------------------------

    def feed(self, camera_id: int, frame: np.ndarray, ts: float) -> None:
        """Called with every annotated frame. Cheap when there's nothing to do."""
        if not self.enabled():
            if self._buffers:
                with self._lock:
                    self._buffers.clear()
            return
        pre = self.pre_seconds()
        with self._lock:
            if ts - self._last_kept.get(camera_id, -1e18) < 1.0 / CLIP_FPS - 1e-3:
                return
            if pre == 0 and camera_id not in self._jobs:
                return                          # no lead-in wanted and nothing recording
            self._last_kept[camera_id] = ts
        jpeg = self._compress(frame)
        if jpeg is None:
            return
        finished = None
        with self._lock:
            buf = self._buffers.setdefault(camera_id, deque())
            buf.append((ts, jpeg))
            while buf and ts - buf[0][0] > pre:
                buf.popleft()
            job = self._jobs.get(camera_id)
            if job is not None:
                self._q.put(("frame", job, ts, jpeg))
                if ts >= job.end_ts:
                    finished = self._jobs.pop(camera_id)
        if finished is not None:
            self._q.put(("finish", finished))

    @staticmethod
    def _compress(frame: np.ndarray) -> Optional[bytes]:
        h, w = frame.shape[:2]
        tw, th = target_size(w, h)
        small = frame if (tw, th) == (w, h) else cv2.resize(
            frame, (tw, th), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", small, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
        return buf.tobytes() if ok else None

    # ---- incidents (event publisher thread) ------------------------------------

    def on_incident(self, change: str, inc: dict[str, Any]) -> None:
        """EventBus listener. A new incident starts a clip; later detections
        on the same camera keep it going."""
        if change not in ("opened", "updated", "escalated") or not self.enabled():
            return
        cam, iid = inc.get("camera_id"), inc.get("incident_id")
        if cam is None or iid is None:
            return
        at = float(inc.get("last_ts") or self._clock())
        post = self.post_seconds()
        with self._lock:
            job = self._jobs.get(cam)
            if job is None:
                # Repeat detections only extend a clip that is running, and an
                # incident that has its clip keeps the first one: that shows
                # how it started (the fall, not the lying still after it).
                if change == "updated" or inc.get("clip_path"):
                    return
                if not self.available():
                    return
                buf = list(self._buffers.get(cam, ()))
                start = buf[0][0] if buf else at
                job = _Job(camera_id=cam, name=f"{int(at * 1000)}_{cam}_{iid}.mp4",
                           start_ts=start, end_ts=min(start + MAX_S, at + post),
                           deadline=self._clock() + post + GRACE_S)
                self._jobs[cam] = job
                for ts, jpeg in buf:
                    self._q.put(("frame", job, ts, jpeg))
                log.info("Recording a clip of incident %s on camera %s", iid, cam)
            else:
                job.end_ts = min(job.max_end, max(job.end_ts, at + post))
                job.deadline = self._clock() + max(0.0, job.end_ts - at) + GRACE_S
            job.incidents.add(int(iid))

    def recording(self, incident_id: int) -> bool:
        with self._lock:
            return any(int(incident_id) in j.incidents for j in self._jobs.values())

    def check_deadlines(self, now: Optional[float] = None) -> None:
        """Finish clips whose camera stopped sending frames."""
        now = self._clock() if now is None else now
        with self._lock:
            late = [cam for cam, j in self._jobs.items() if now >= j.deadline]
            jobs = [self._jobs.pop(cam) for cam in late]
        for job in jobs:
            self._q.put(("finish", job))

    # ---- writer thread -------------------------------------------------------

    def _writer_loop(self) -> None:
        while True:
            item = self._q.get()
            job = item[1]
            try:
                if item[0] == "frame":
                    self._write(job, item[2], item[3])
                else:
                    self._finish(job)
            except Exception as e:
                job.failed = True
                log.warning("clip %s: %s", job.name, e)
            finally:
                self._q.task_done()

    def _write(self, job: _Job, ts: float, jpeg: bytes) -> None:
        if job.failed or job.cancelled:
            return
        img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return
        if job.writer is None:
            job.size = (img.shape[1], img.shape[0])
            job.writer = self._open_writer(self.dir / job.name, job.size)
            if job.writer is None:
                job.failed = True
                self.error = "The video encoder refused to start."
                return
            job.next_tick = ts
        elif (img.shape[1], img.shape[0]) != job.size:
            img = cv2.resize(img, job.size, interpolation=cv2.INTER_AREA)
        if job.prev is not None:
            # Repeat the previous frame until this one is due, so the clip
            # plays in real time; after a long stall, jump instead.
            if ts - job.next_tick > MAX_FILL_S:
                job.next_tick = ts - MAX_FILL_S
            while job.next_tick < ts - 1e-6:
                job.writer.write(job.prev)
                job.frames += 1
                job.next_tick += 1.0 / CLIP_FPS
        job.prev = img

    def _finish(self, job: _Job) -> None:
        path = self.dir / job.name
        if job.writer is not None:
            if job.prev is not None and not (job.failed or job.cancelled):
                job.writer.write(job.prev)
                job.frames += 1
            job.writer.release()
        ok = (not job.failed and not job.cancelled and job.frames >= 2
              and path.exists() and path.stat().st_size > 0)
        saved = 0
        if ok:
            marks = ",".join("?" * len(job.incidents))
            with write_conn(self.db_path) as conn:
                saved = conn.execute(
                    f"UPDATE incidents SET clip_path = ? WHERE clip_path IS NULL "
                    f"AND incident_id IN ({marks})", (job.name, *job.incidents)).rowcount
        if not saved:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            if not job.cancelled:
                log.warning("clip %s was not saved (%d frames)", job.name, job.frames)
            return
        self.error = None
        log.info("Saved clip %s (%.0f s)", job.name, job.frames / CLIP_FPS)
        if self._on_ready is not None:
            try:
                self._on_ready(sorted(job.incidents), job.name)
            except Exception as e:
                log.warning("clip listener failed: %s", e)

    def flush(self, timeout: float = 10.0) -> bool:
        """Wait until the writer has caught up (tests, shutdown)."""
        end = time.monotonic() + timeout
        while self._q.unfinished_tasks and time.monotonic() < end:
            time.sleep(0.02)
        return not self._q.unfinished_tasks

    # ---- housekeeping --------------------------------------------------------

    def _housekeeping(self) -> None:
        last_cleanup = 0.0
        while True:
            time.sleep(1.0)
            try:
                self.check_deadlines()
                if time.monotonic() - last_cleanup > 3600:
                    last_cleanup = time.monotonic()
                    self.cleanup()
            except Exception as e:
                log.warning("clip housekeeping: %s", e)

    def _files(self) -> list[Path]:
        return [f for f in self.dir.glob("*.mp4") if f.is_file()]

    def _busy_names(self) -> set[str]:
        with self._lock:
            return {j.name for j in self._jobs.values()}

    def cleanup(self, now: Optional[float] = None) -> int:
        """Delete clips past the keep-days limit and files no incident names."""
        now = self._clock() if now is None else now
        cutoff = now - self.keep_days() * 86400
        with read_conn(self.db_path) as conn:
            named = {r["clip_path"] for r in conn.execute(
                "SELECT clip_path FROM incidents WHERE clip_path IS NOT NULL")}
        busy = self._busy_names()
        removed = []
        for f in self._files():
            if f.name in busy:
                continue
            try:
                mtime = f.stat().st_mtime
                if mtime < cutoff or (f.name not in named and mtime < now - ORPHAN_AGE_S):
                    f.unlink()
                    removed.append(f.name)
            except OSError:
                continue
        gone = [n for n in removed if n in named]
        if gone:
            marks = ",".join("?" * len(gone))
            with write_conn(self.db_path) as conn:
                conn.execute(f"UPDATE incidents SET clip_path = NULL "
                             f"WHERE clip_path IN ({marks})", gone)
        if removed:
            log.info("Deleted %d old incident clip(s)", len(removed))
        return len(removed)

    def clear_all(self) -> None:
        """The incident history was deleted: stop recording and delete clips."""
        with self._lock:
            jobs = list(self._jobs.values())
            self._jobs.clear()
            self._buffers.clear()
        for job in jobs:
            job.cancelled = True
            self._q.put(("finish", job))
        self.flush(5.0)
        for f in self._files():
            try:
                f.unlink()
            except OSError:       # being watched right now: cleanup() takes it later
                pass

    def forget_camera(self, camera_id: int) -> None:
        with self._lock:
            self._buffers.pop(camera_id, None)
            self._last_kept.pop(camera_id, None)
            job = self._jobs.pop(camera_id, None)
        if job is not None:
            self._q.put(("finish", job))

    def status(self) -> dict[str, Any]:
        sizes = []
        for f in self._files():
            try:
                sizes.append(f.stat().st_size)
            except OSError:
                pass
        with self._lock:
            recording = len(self._jobs)
        available = self.available()
        return {"enabled": self.enabled(), "available": available,
                "encoder": self.encoder_name, "error": None if available else self.error,
                "recording": recording, "count": len(sizes), "bytes": sum(sizes),
                "pre_seconds": self.pre_seconds(), "post_seconds": self.post_seconds(),
                "keep_days": self.keep_days()}
