"""PersonStore (registered faces) + IntruderStore (unrecognised faces).

Both keep a small in-memory cache of (id, embedding) tuples so the
detection loop can match without hitting SQLite per frame.
"""

from __future__ import annotations

import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import cv2
import numpy as np

from .db import read_conn, write_conn


# ---- helpers --------------------------------------------------------------

def emb_to_blob(emb: np.ndarray) -> bytes:
    return np.asarray(emb, dtype=np.float32).tobytes()


def blob_to_emb(blob: Optional[bytes]) -> Optional[np.ndarray]:
    return np.frombuffer(blob, dtype=np.float32) if blob else None


def save_portrait(frame_bgr, bbox, out_path: Path, width: int = 400) -> bool:
    """Head and shoulders around a face: a 4:5 portrait at most ``width`` wide.

    The box is 3.2 face-heights tall with the face a third of the way down. A
    box bigger than the picture shrinks to fit, and one past an edge slides back
    in, so a face near the edge sits off-centre rather than beside a blank strip.
    """
    x, y, w, h = (float(v) for v in bbox)
    H, W = frame_bgr.shape[:2]
    if w <= 0 or h <= 0:
        return False
    bh = 3.2 * h
    bw = bh * 0.8
    k = min(1.0, W / bw, H / bh)
    bw, bh = bw * k, bh * k
    x1 = min(max(x + w / 2 - bw / 2, 0.0), W - bw)
    y1 = min(max(y + h / 2 - 0.34 * bh, 0.0), H - bh)
    crop = frame_bgr[round(y1):round(y1 + bh), round(x1):round(x1 + bw)]
    if crop.size == 0:
        return False
    h_c, w_c = crop.shape[:2]
    scale = min(1.0, width / w_c)
    if scale < 1.0:
        crop = cv2.resize(crop, (max(1, round(w_c * scale)), max(1, round(h_c * scale))),
                          interpolation=cv2.INTER_AREA)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    return bool(cv2.imwrite(str(out_path), crop, [cv2.IMWRITE_JPEG_QUALITY, 90]))


def _maybe_save_thumb(frame_bgr, face_bbox, photo_path: Path) -> bool:
    if frame_bgr is None or face_bbox is None:
        return False
    return save_portrait(frame_bgr, face_bbox, photo_path)


def _copy_photo(src: Optional[str], dest: Path) -> bool:
    """A photo another record already has (an intruder's, when they're registered)."""
    if not src or not Path(src).is_file():
        return False
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        return True
    except OSError:
        return False


def _utc_text_ts(text: Optional[str]) -> Optional[float]:
    """SQLite's datetime('now') text, which is UTC, as epoch seconds."""
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def _unlink_quietly(p: Optional[str]) -> None:
    if p:
        try:
            Path(p).unlink(missing_ok=True)
        except Exception:
            pass


# ---- PersonStore ----------------------------------------------------------

class PersonStore:
    """Registered known faces, and when a camera last recognised each one."""

    _CATEGORIES = ("adult", "child", "elderly")
    SEEN_SAVE_S = 60.0           # a sighting is written to the database at most this often

    def __init__(self, db_path: str, photos_dir: Path):
        self.db_path = db_path
        self.photos_dir = Path(photos_dir)
        self.photos_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._gallery: list[tuple[int, np.ndarray]] = []
        self._meta: dict[int, dict[str, Any]] = {}
        # person_id -> (at, camera name). Kept apart from _meta so a reload
        # doesn't roll back a sighting the database hasn't been given yet.
        self._seen: dict[int, tuple[float, Optional[str]]] = {}
        self._seen_saved: dict[int, float] = {}
        self.reload()

    def reload(self) -> None:
        with read_conn(self.db_path) as conn:
            rows = conn.execute(
                "SELECT person_id, name, category, photo_path, "
                "embedding_blob, created_at, last_seen_at, last_seen_camera "
                "FROM persons ORDER BY person_id ASC"
            ).fetchall()
        gallery, meta, stored = [], {}, {}
        for r in rows:
            pid = int(r["person_id"])
            emb = blob_to_emb(r["embedding_blob"])
            if emb is not None:
                gallery.append((pid, emb))
            meta[pid] = {
                "person_id": pid,
                "name": r["name"],
                "category": r["category"] or "adult",
                "photo_path": r["photo_path"],
                "created_at": r["created_at"],
            }
            if r["last_seen_at"] is not None:
                stored[pid] = (float(r["last_seen_at"]), r["last_seen_camera"])
        with self._lock:
            self._gallery = gallery
            self._meta = meta
            for pid, seen in stored.items():
                if pid not in self._seen or seen[0] > self._seen[pid][0]:
                    self._seen[pid] = seen
            for pid in [p for p in self._seen if p not in meta]:
                del self._seen[pid]
                self._seen_saved.pop(pid, None)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            persons = list(self._meta.values())
            seen = dict(self._seen)
        return [{
            "person_id": p["person_id"],
            "name": p["name"],
            "category": p["category"],
            "photo_url": (f"/person_photos/{Path(p['photo_path']).name}"
                          if p["photo_path"] else None),
            "created_at": p["created_at"],
            "last_seen_at": seen.get(p["person_id"], (None, None))[0],
            "last_seen_camera": seen.get(p["person_id"], (None, None))[1],
        } for p in persons]

    def mark_seen(self, person_id: int, camera_name: Optional[str],
                  at: Optional[float] = None) -> None:
        """A camera recognised this face. Called for every match, so it only
        writes to the database once a minute per person; the list always has
        the latest."""
        pid, at = int(person_id), time.time() if at is None else float(at)
        with self._lock:
            if pid not in self._meta:
                return
            self._seen[pid] = (at, camera_name)
            if at - self._seen_saved.get(pid, float("-inf")) < self.SEEN_SAVE_S:
                return
            self._seen_saved[pid] = at
        with write_conn(self.db_path) as conn:
            conn.execute("UPDATE persons SET last_seen_at = ?, last_seen_camera = ? "
                         "WHERE person_id = ?", (at, camera_name, pid))

    def gallery(self) -> list[tuple[int, np.ndarray]]:
        with self._lock:
            return list(self._gallery)

    def name_of(self, person_id: int) -> Optional[str]:
        with self._lock:
            m = self._meta.get(int(person_id))
        return m["name"] if m else None

    def category_of(self, person_id: int) -> Optional[str]:
        with self._lock:
            m = self._meta.get(int(person_id))
        return m["category"] if m else None

    def add(self, *, name: str, category: str, embedding: np.ndarray,
            frame_bgr=None, face_bbox=None, photo_from: Optional[str] = None,
            seen_at: Optional[float] = None,
            seen_camera: Optional[str] = None) -> dict[str, Any]:
        """``photo_from`` and ``seen_*``: someone registered from an intruder
        record keeps its photo, and was last seen when and where it was."""
        cat = category if category in self._CATEGORIES else "adult"
        photo_rel = f"person_{int(time.time() * 1000)}.jpg"
        photo_path = self.photos_dir / photo_rel
        ok = (_maybe_save_thumb(frame_bgr, face_bbox, photo_path)
              or _copy_photo(photo_from, photo_path))
        with write_conn(self.db_path) as conn:
            cur = conn.execute(
                """INSERT INTO persons (name, category, photo_path, embedding_blob,
                                        last_seen_at, last_seen_camera)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (name.strip(), cat,
                 str(photo_path) if ok else None,
                 emb_to_blob(embedding),
                 seen_at, seen_camera if seen_at is not None else None),
            )
            pid = cur.lastrowid
        self.reload()
        return {
            "person_id": pid,
            "name": name.strip(),
            "category": cat,
            "photo_url": f"/person_photos/{photo_rel}" if ok else None,
        }

    def delete(self, person_id: int) -> None:
        with write_conn(self.db_path) as conn:
            row = conn.execute(
                "SELECT photo_path FROM persons WHERE person_id = ?",
                (int(person_id),),
            ).fetchone()
            conn.execute(
                "DELETE FROM persons WHERE person_id = ?", (int(person_id),)
            )
        _unlink_quietly(row["photo_path"] if row else None)
        self.reload()


# ---- IntruderStore --------------------------------------------------------

class IntruderStore:
    """Faces detected on camera that didn't match anyone registered."""

    def __init__(self, db_path: str, photos_dir: Path):
        self.db_path = db_path
        self.photos_dir = Path(photos_dir)
        self.photos_dir.mkdir(parents=True, exist_ok=True)

    def add(self, *, embedding: np.ndarray, camera_id: Optional[int],
            camera_name: Optional[str], frame_bgr=None,
            face_bbox=None) -> Optional[int]:
        photo_rel = f"intruder_{int(time.time() * 1000)}.jpg"
        photo_path = self.photos_dir / photo_rel
        ok = _maybe_save_thumb(frame_bgr, face_bbox, photo_path)
        with write_conn(self.db_path) as conn:
            cur = conn.execute(
                """INSERT INTO intruders
                   (photo_path, embedding_blob, camera_id, camera_name)
                   VALUES (?, ?, ?, ?)""",
                (str(photo_path) if ok else None,
                 emb_to_blob(embedding),
                 int(camera_id) if camera_id is not None else None,
                 camera_name),
            )
            return cur.lastrowid

    def list(self, include_dismissed: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM intruders"
        if not include_dismissed:
            sql += " WHERE dismissed = 0"
        sql += " ORDER BY detected_at DESC LIMIT 200"
        with read_conn(self.db_path) as conn:
            rows = conn.execute(sql).fetchall()
        return [{
            "intruder_id": r["intruder_id"],
            "photo_url": (f"/intruder_photos/{Path(r['photo_path']).name}"
                          if r["photo_path"] else None),
            "camera_id": r["camera_id"],
            "camera_name": r["camera_name"] or "Unknown",
            "detected_at": r["detected_at"],
            "dismissed": bool(r["dismissed"]),
        } for r in rows]

    def dismiss(self, intruder_id: int) -> None:
        with write_conn(self.db_path) as conn:
            conn.execute(
                "UPDATE intruders SET dismissed = 1 WHERE intruder_id = ?",
                (int(intruder_id),),
            )

    def delete(self, intruder_id: int) -> None:
        with write_conn(self.db_path) as conn:
            row = conn.execute(
                "SELECT photo_path FROM intruders WHERE intruder_id = ?",
                (int(intruder_id),),
            ).fetchone()
            conn.execute(
                "DELETE FROM intruders WHERE intruder_id = ?", (int(intruder_id),)
            )
        _unlink_quietly(row["photo_path"] if row else None)

    def get(self, intruder_id: int) -> Optional[dict[str, Any]]:
        with read_conn(self.db_path) as conn:
            r = conn.execute(
                "SELECT * FROM intruders WHERE intruder_id = ?",
                (int(intruder_id),),
            ).fetchone()
        if not r:
            return None
        return {
            "intruder_id": r["intruder_id"],
            "photo_path": r["photo_path"],
            "embedding": blob_to_emb(r["embedding_blob"]),
            "camera_id": r["camera_id"],
            "camera_name": r["camera_name"],
            "detected_at": r["detected_at"],
            "detected_ts": _utc_text_ts(r["detected_at"]),
            "dismissed": bool(r["dismissed"]),
        }
