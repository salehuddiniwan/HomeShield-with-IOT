"""Polygon zones per camera.

* danger zones    - alert when a child enters
* safe zones      - suppress lying / inactivity inside (e.g. a bed)
* fireplace zones - ignore fire and smoke that stays inside (a fireplace, a stove)

Polygons are stored as JSON [[x, y], ...] in the UI's 640x480 reference
frame and scaled per-camera at runtime.
"""

from __future__ import annotations

import json
import threading
from typing import Any

from .db import read_conn, write_conn


def point_in_polygon(px: float, py: float, polygon: list[list[float]]) -> bool:
    """Ray-casting point-in-polygon test."""
    n = len(polygon)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i][0], polygon[i][1]
        xj, yj = polygon[j][0], polygon[j][1]
        if ((yi > py) != (yj > py)) and (
            px < (xj - xi) * (py - yi) / ((yj - yi) or 1e-9) + xi
        ):
            inside = not inside
        j = i
    return inside


ZONE_TYPES = ("danger", "safe", "fireplace")

# A flame or smoke box counts as "in the fireplace" when at least this share of it
# lies inside the zone. A fire spreading out of the fireplace has a box reaching well
# past the zone, drops below this, and alerts as usual.
FIREPLACE_SHARE = 0.6


def box_share_inside(bbox, polygon: list[list[float]], steps: int = 8) -> float:
    """Roughly how much of the box (x1, y1, x2, y2) lies inside the polygon (0 to 1),
    from a steps x steps grid of points across the box."""
    x1, y1, x2, y2 = (float(v) for v in bbox[:4])
    if x2 <= x1 or y2 <= y1 or len(polygon) < 3:
        return 0.0
    inside = 0
    for i in range(steps):
        for j in range(steps):
            px = x1 + (i + 0.5) * (x2 - x1) / steps
            py = y1 + (j + 0.5) * (y2 - y1) / steps
            inside += point_in_polygon(px, py, polygon)
    return inside / (steps * steps)


def mark_fireplace_fires(fires: list[dict[str, Any]], zones: list[dict[str, Any]],
                         share: float = FIREPLACE_SHARE) -> None:
    """Set d["ignored"] on each fire / smoke detection that is mostly inside a
    fireplace zone (zones carry "polygon_scaled" in frame pixels)."""
    for d in fires:
        d["ignored"] = any(box_share_inside(d["bbox"], z["polygon_scaled"]) >= share for z in zones)


def scale_polygon(poly: list[list[float]], src_w: int, src_h: int,
                  dst_w: int, dst_h: int) -> list[list[float]]:
    sx = dst_w / max(1, src_w)
    sy = dst_h / max(1, src_h)
    return [[p[0] * sx, p[1] * sy] for p in poly]


class ZoneStore:
    """In-memory cache of zones-per-camera, backed by SQLite."""

    REF_W = 640
    REF_H = 480

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._by_camera: dict[int, list[dict[str, Any]]] = {}
        self.reload()

    def reload(self) -> None:
        with read_conn(self.db_path) as conn:
            rows = conn.execute("SELECT * FROM zones").fetchall()
        cache: dict[int, list[dict[str, Any]]] = {}
        for r in rows:
            try:
                poly = json.loads(r["polygon_json"])
            except Exception:
                continue
            cache.setdefault(int(r["camera_id"]), []).append({
                "zone_id": r["zone_id"],
                "zone_name": r["zone_name"],
                "camera_id": int(r["camera_id"]),
                "polygon": poly,
                "zone_type": r["zone_type"] or "danger",
            })
        with self._lock:
            self._by_camera = cache

    # ---- CRUD -----------------------------------------------------------

    def list_all(self) -> list[dict[str, Any]]:
        with self._lock:
            return [z for zs in self._by_camera.values() for z in zs]

    def for_camera(self, camera_id: int) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._by_camera.get(int(camera_id), []))

    def add(self, *, zone_name: str, camera_id: int,
            polygon: list[list[float]], zone_type: str = "danger") -> int:
        zone_type = zone_type if zone_type in ZONE_TYPES else "danger"
        with write_conn(self.db_path) as conn:
            cur = conn.execute(
                """INSERT INTO zones (zone_name, camera_id, polygon_json, zone_type)
                   VALUES (?, ?, ?, ?)""",
                (zone_name, int(camera_id), json.dumps(polygon), zone_type),
            )
            zid = cur.lastrowid
        self.reload()
        return zid  # type: ignore[return-value]

    def delete(self, zone_id: int) -> None:
        with write_conn(self.db_path) as conn:
            conn.execute("DELETE FROM zones WHERE zone_id = ?", (int(zone_id),))
        self.reload()

    # ---- runtime helpers -----------------------------------------------

    def _scaled_zones(self, camera_id: int, frame_w: int, frame_h: int,
                      zone_type: str) -> list[dict[str, Any]]:
        out = []
        for z in self.for_camera(camera_id):
            if z["zone_type"] != zone_type:
                continue
            out.append({
                **z,
                "polygon_scaled": scale_polygon(
                    z["polygon"], self.REF_W, self.REF_H, frame_w, frame_h
                ),
            })
        return out

    def is_in_safe_zone(self, camera_id: int, frame_w: int, frame_h: int,
                        x: float, y: float) -> bool:
        for z in self._scaled_zones(camera_id, frame_w, frame_h, "safe"):
            if point_in_polygon(x, y, z["polygon_scaled"]):
                return True
        return False

    def danger_zones_for(self, camera_id: int, frame_w: int, frame_h: int
                         ) -> list[dict[str, Any]]:
        return self._scaled_zones(camera_id, frame_w, frame_h, "danger")

    def fireplace_zones_for(self, camera_id: int, frame_w: int, frame_h: int
                            ) -> list[dict[str, Any]]:
        return self._scaled_zones(camera_id, frame_w, frame_h, "fireplace")
