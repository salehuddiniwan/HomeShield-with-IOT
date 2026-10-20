"""Who is using the dashboard right now, and when each account was last seen.

Every signed-in request touches the tracker, keyed by a random id kept in
the browser's session cookie, so one account open on a phone and a laptop
counts as two devices. An open dashboard asks for /api/status every three
seconds and says whether its tab is in front; a background tab is slowed by
the browser to about once a minute, hence ONLINE_S.

What is kept in memory is lost on restart, so each account's last-seen time
is also written to users.last_seen_at, at most once a minute.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable, Optional

from .db import write_conn

ONLINE_S = 90.0               # heard from this recently: online
PERSIST_S = 60.0              # write last_seen_at at most this often per account
MAX_SESSIONS = 300

_PHONE_RE = re.compile(r"Mobi|Android|iPhone|iPad|iPod", re.IGNORECASE)


def device_kind(user_agent: str) -> str:
    return "phone" if _PHONE_RE.search(user_agent or "") else "computer"


class Presence:
    def __init__(self, db_path: str, *, clock: Callable[[], float] = time.time):
        self.db_path = db_path
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions: dict[str, dict[str, Any]] = {}     # session id -> last request
        self._persisted: dict[int, float] = {}

    def touch(self, user_id: int, session_id: str, *, user_agent: str = "",
              remote: bool = False, ip: str = "", visible: Optional[bool] = None) -> None:
        """A signed-in request. visible: whether the dashboard tab is in front,
        when the request says so (the status poll); None keeps the last answer."""
        now = self._clock()
        uid = int(user_id)
        with self._lock:
            prev = self._sessions.get(session_id)
            self._sessions[session_id] = {
                "user_id": uid, "at": now, "device": device_kind(user_agent),
                "remote": bool(remote),
                "visible": visible if visible is not None else (prev or {}).get("visible", True),
            }
            if len(self._sessions) > MAX_SESSIONS:       # forget the longest-quiet one
                oldest = min(self._sessions, key=lambda k: self._sessions[k]["at"])
                self._sessions.pop(oldest, None)
            persist = now - self._persisted.get(uid, -1e18) >= PERSIST_S
            if persist:
                self._persisted[uid] = now
        if persist:
            with write_conn(self.db_path) as conn:
                conn.execute("UPDATE users SET last_seen_at = ?, last_seen_ip = ? "
                             "WHERE user_id = ?", (now, (ip or "")[:64] or None, uid))

    def forget(self, session_id: Optional[str]) -> None:
        """Signed out: that browser is no longer here."""
        if session_id:
            with self._lock:
                self._sessions.pop(session_id, None)

    def snapshot(self, users: list[dict[str, Any]], me: Optional[int] = None
                 ) -> list[dict[str, Any]]:
        """Each account with whether it is online now, on which devices, and
        when it was last seen; online first, then the most recently seen."""
        now = self._clock()
        with self._lock:
            sessions = [dict(s) for s in self._sessions.values()]
        out = []
        for u in users:
            uid = u["user_id"]
            mine = [s for s in sessions if s["user_id"] == uid]
            live = [s for s in mine if now - s["at"] < ONLINE_S]
            seen = max([s["at"] for s in mine] + [u.get("last_seen_at") or 0.0]) or None
            out.append({
                "user_id": uid, "username": u["username"], "role": u["role"],
                "you": uid == me,
                "online": bool(live),
                "watching": any(s["visible"] for s in live),
                "devices": sorted({s["device"] for s in live}),
                "device_counts": {d: sum(s["device"] == d for s in live)
                                  for d in sorted({s["device"] for s in live})},
                "sessions": len(live),
                "remote": any(s["remote"] for s in live),
                "last_seen_at": seen,
            })
        out.sort(key=lambda p: (not p["online"], not p["you"], -(p["last_seen_at"] or 0.0)))
        return out
