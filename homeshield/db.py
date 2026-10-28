"""SQLite schema + connection helpers. One DB file backs the whole app."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from os import PathLike
from pathlib import Path
from typing import Iterator, Union

PathArg = Union[str, PathLike]

# Serialise writes across threads; reads stay concurrent.
_WRITE_LOCK = threading.Lock()


SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS cameras (
    camera_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    url         TEXT    NOT NULL,
    location    TEXT    NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS events (
    event_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL    NOT NULL,
    created_at      TEXT    NOT NULL,
    event_type      TEXT    NOT NULL,
    camera_id       INTEGER,
    camera_name     TEXT,
    person_category TEXT    NOT NULL DEFAULT 'unknown',
    confidence      REAL    NOT NULL DEFAULT 0,
    details         TEXT,
    snapshot_path   TEXT,
    bbox_json       TEXT,
    meta_json       TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts   ON events (ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_type ON events (event_type);
CREATE INDEX IF NOT EXISTS idx_events_cam  ON events (camera_id);

CREATE TABLE IF NOT EXISTS zones (
    zone_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_name    TEXT    NOT NULL,
    camera_id    INTEGER NOT NULL,
    polygon_json TEXT    NOT NULL,
    zone_type    TEXT    NOT NULL DEFAULT 'danger',
    created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_zones_cam ON zones (camera_id);

CREATE TABLE IF NOT EXISTS persons (
    person_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT    NOT NULL,
    category       TEXT    NOT NULL DEFAULT 'adult',
    photo_path     TEXT,
    embedding_blob BLOB,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    last_seen_at     REAL,       -- when a camera last recognised the face
    last_seen_camera TEXT        -- and which camera
);
CREATE INDEX IF NOT EXISTS idx_persons_name ON persons (name);

CREATE TABLE IF NOT EXISTS intruders (
    intruder_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    photo_path     TEXT,
    embedding_blob BLOB,
    camera_id      INTEGER,
    camera_name    TEXT,
    detected_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    dismissed      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_intruders_at ON intruders (detected_at DESC);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Alerts grouped into episodes someone can act on (see incidents.py).
CREATE TABLE IF NOT EXISTS incidents (
    incident_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT    NOT NULL,
    camera_id       INTEGER,
    camera_name     TEXT,
    headline_type   TEXT    NOT NULL,
    headline_rank   INTEGER NOT NULL DEFAULT 1,
    severity        INTEGER NOT NULL DEFAULT 1,
    started_ts      REAL    NOT NULL,
    last_ts         REAL    NOT NULL,
    event_count     INTEGER NOT NULL DEFAULT 0,
    peak_confidence REAL    NOT NULL DEFAULT 0,
    peak_event_id   INTEGER,
    snapshot_path   TEXT,
    person_category TEXT    NOT NULL DEFAULT 'unknown',
    status          TEXT    NOT NULL DEFAULT 'open',
    escalated       INTEGER NOT NULL DEFAULT 0,
    resolved_by     TEXT,
    resolved_at     REAL,
    note            TEXT,
    clip_path       TEXT              -- incident video (clips.py)
);
CREATE INDEX IF NOT EXISTS idx_incidents_last   ON incidents (last_ts DESC);
CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents (status);
CREATE INDEX IF NOT EXISTS idx_incidents_group  ON incidents (camera_id, kind, last_ts DESC);

CREATE TABLE IF NOT EXISTS users (
    user_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    username       TEXT    NOT NULL UNIQUE,
    password_hash  TEXT    NOT NULL,
    role           TEXT    NOT NULL DEFAULT 'guest',
    must_change    INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_users_username ON users (username);

-- ESP32 boards adopted over MQTT, and what is wired to their pins (iot/).
CREATE TABLE IF NOT EXISTS nodes (
    node_id     TEXT    PRIMARY KEY,         -- MQTT client id and topic base, e.g. node-kitchen
    room        TEXT    NOT NULL DEFAULT '',
    chip_id     TEXT,                        -- the name it had before adoption, e.g. esp32-3C61A8
    adopted_at  REAL    NOT NULL,
    last_seen   REAL,
    info        TEXT                         -- last info message (JSON): fw, ip, rssi, ...
);
CREATE TABLE IF NOT EXISTS devices (
    device_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id     TEXT    NOT NULL,
    pin         INTEGER NOT NULL,
    kind        TEXT    NOT NULL,            -- light, lock, gas, sos, ... (iot/kinds.py)
    name        TEXT    NOT NULL,
    room        TEXT    NOT NULL DEFAULT '',
    options     TEXT    NOT NULL DEFAULT '{}',
    state       TEXT,                        -- the last value the board reported
    state_at    REAL,
    created_at  REAL    NOT NULL,
    UNIQUE (node_id, pin)
);
CREATE TABLE IF NOT EXISTS device_log (
    log_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    device_id   INTEGER,
    node_id     TEXT,
    text        TEXT    NOT NULL,
    by          TEXT
);
CREATE INDEX IF NOT EXISTS idx_device_log_ts ON device_log (ts DESC);
CREATE TABLE IF NOT EXISTS scenes (
    scene_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    description TEXT    NOT NULL DEFAULT '',
    icon        TEXT    NOT NULL DEFAULT 'power',
    actions     TEXT    NOT NULL DEFAULT '[]',   -- [{"device_id": 3, "value": "off"}, ...]
    last_run_at REAL,
    last_run_by TEXT
);

-- Sensors (iot/sensors.py): levels and calibration, readings per minute,
-- and the rules that run when a sensor reaches a level.
CREATE TABLE IF NOT EXISTS sensor_settings (
    device_id   INTEGER PRIMARY KEY,
    data        TEXT    NOT NULL DEFAULT '{}'    -- warn, danger, hold, calibration, ...
);
CREATE TABLE IF NOT EXISTS sensor_history (
    device_id   INTEGER NOT NULL,
    series      TEXT    NOT NULL,                -- ppm, t, h, or on (digital sensors)
    ts          INTEGER NOT NULL,                -- start of the minute
    avg         REAL,
    min         REAL,
    max         REAL,
    n           INTEGER NOT NULL,                -- readings (on: times it became active)
    PRIMARY KEY (device_id, series, ts)
);
CREATE TABLE IF NOT EXISTS rules (
    rule_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    enabled       INTEGER NOT NULL DEFAULT 1,
    origin        TEXT    NOT NULL DEFAULT 'user',   -- sensor: edited on the Sensors page
    trigger_spec  TEXT    NOT NULL,                  -- [{"type": "sensor", "device_id": 5, "level": "danger"}, ...]
    actions       TEXT    NOT NULL DEFAULT '[]',     -- [{"type": "device", "device_id": 7, "value": "0"}, ...]
    created_at    REAL    NOT NULL,
    last_fired_at REAL,
    fire_count    INTEGER NOT NULL DEFAULT 0
);
-- What each automation did (iot/automations.py), for its Recent runs.
CREATE TABLE IF NOT EXISTS rule_runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id     INTEGER NOT NULL,
    ts          REAL    NOT NULL,
    cause       TEXT    NOT NULL DEFAULT '',    -- "Gas sensor reached warning (342 ppm)"
    summary     TEXT    NOT NULL DEFAULT '',    -- "3 actions done" / "node-garage did not reply"
    ok          INTEGER NOT NULL DEFAULT 1,
    steps       TEXT    NOT NULL DEFAULT '[]',
    -- What set it off, so an incident can show what was done about it.
    device_id   INTEGER,                        -- a sensor trigger
    camera_id   INTEGER,                        -- a camera or person trigger
    incident_id INTEGER                         -- a camera incident or SOS press
);
CREATE INDEX IF NOT EXISTS idx_rule_runs ON rule_runs (rule_id, ts DESC);
"""


def init_db(db_path: PathArg) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path, timeout=10.0) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive upgrades for databases created by older versions."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(events)")}
    if "incident_id" not in cols:
        conn.execute("ALTER TABLE events ADD COLUMN incident_id INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_incident "
                 "ON events (incident_id)")
    ucols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    for col, kind in (("last_login_at", "REAL"), ("last_login_ip", "TEXT"),
                      ("reset_requested_at", "REAL"), ("last_seen_at", "REAL"),
                      ("last_seen_ip", "TEXT")):
        if col not in ucols:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} {kind}")
    icols = {r[1] for r in conn.execute("PRAGMA table_info(incidents)")}
    for col, kind in (("clip_path", "TEXT"),
                      # Incidents from ESP32 sensors (iot/sensors.py): which sensor,
                      # "node-kitchen · MQ-2", and what it read, "342 ppm".
                      ("device_id", "INTEGER"), ("source", "TEXT"), ("reading", "TEXT")):
        if col not in icols:
            conn.execute(f"ALTER TABLE incidents ADD COLUMN {col} {kind}")
    rcols = {r[1] for r in conn.execute("PRAGMA table_info(rules)")}
    for col, kind in (("conditions", "TEXT NOT NULL DEFAULT '[]'"),
                      ("options", "TEXT NOT NULL DEFAULT '{}'"),
                      ("changed_by", "TEXT"), ("changed_at", "REAL")):
        if col not in rcols:
            conn.execute(f"ALTER TABLE rules ADD COLUMN {col} {kind}")
    pcols = {r[1] for r in conn.execute("PRAGMA table_info(persons)")}
    for col, kind in (("last_seen_at", "REAL"), ("last_seen_camera", "TEXT")):
        if col not in pcols:
            conn.execute(f"ALTER TABLE persons ADD COLUMN {col} {kind}")
    runcols = {r[1] for r in conn.execute("PRAGMA table_info(rule_runs)")}
    for col in ("device_id", "camera_id", "incident_id"):
        if col not in runcols:
            conn.execute(f"ALTER TABLE rule_runs ADD COLUMN {col} INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rule_runs_ts ON rule_runs (ts)")


def connect(db_path: PathArg) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def write_conn(db_path: PathArg) -> Iterator[sqlite3.Connection]:
    """Serialised writer; commits on success, always closes."""
    with _WRITE_LOCK:
        conn = connect(db_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


@contextmanager
def read_conn(db_path: PathArg) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()
