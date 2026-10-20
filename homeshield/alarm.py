"""Alarm buzzer on an ESP32: a sound in the house when an incident starts.

The ESP32 runs the sketch in firmware/HomeShieldBuzzer and drives a buzzer.
HomeShield only says which alarm to play; the board knows the patterns and
keeps time itself, so a busy PC never stretches a beep.

Two ways to reach the board, usable together:

- USB: plugged into this computer. The DevKit's CP2102 (or CH340) chip shows
  up as a COM port; HomeShield finds it by its USB ids and a handshake, or
  uses the port chosen in Settings, and talks at 115200 baud.
- Wi-Fi: the board polls GET /api/device/poll about once a second with the
  device key from the ESP32 nodes page, so it can sit anywhere on the
  home network.

Both carry the same one-line messages:

    PC -> board:   ALARM <FIRE|SOS|FALL|INTRUDER|ZONE|TEST> <seq>   STOP <seq>   HELLO
    board -> PC:   HOMESHIELD-BUZZER <version> [wifi=on|off] [id=<name>]   BTN   SOS

<seq> grows every time the wanted sound changes. The board's button silences
it at once and reports BTN; the seq lets the board ignore the same alarm
being repeated before HomeShield has heard about the button.

SOS: a second button on the board, for someone who needs help. The board
starts the SOS sound itself at once (it works with the PC off), and reports
SOS; HomeShield turns that into a critical "help requested" incident named
after the board, which sends WhatsApp alerts and sounds every buzzer.

When it sounds: an incident opens or escalates, for the kinds ticked in
Settings. It stops when each of those incidents is acknowledged or marked a
false alarm, when someone presses Silence (dashboard or board), or at the
time limit. Repeat detections inside a silenced incident stay quiet.
"""

from __future__ import annotations

import hmac
import logging
import re
import secrets
import threading
import time
from typing import Any, Callable, Hashable, Optional

from .incidents import SENSOR_EVENTS

log = logging.getLogger(__name__)

# The boards know these five patterns. Sensor kinds borrow one: gas and heat
# sound like fire (the smoke-alarm rhythm), a water leak like a danger zone.
PATTERNS = {"fire": "FIRE", "sos": "SOS", "fall": "FALL", "intruder": "INTRUDER",
            "zone": "ZONE", "gas": "FIRE", "heat": "FIRE", "water": "ZONE"}
PRIORITY = ("fire", "gas", "sos", "heat", "fall", "intruder", "zone", "water")
CAMERA_KINDS = ("fire", "sos", "fall", "intruder", "zone")   # ticked in Settings
DEFAULT_KINDS = ",".join(CAMERA_KINDS)
TEST_SECONDS = 3.0
WIFI_STALE_S = 10.0          # a Wi-Fi board that hasn't polled for this long is offline
SOS_REPEAT_S = 2.0           # one board's presses closer than this count once
BAUD = 115200
HELLO = "HOMESHIELD-BUZZER"      # how the board introduces itself
HELLO_REQUEST = "HELLO"          # asks it to

# USB-to-serial chips found on ESP32 boards, by (vendor id, product id).
# Only these are probed automatically; other serial devices are left alone.
USB_BRIDGES = {
    (0x10C4, 0xEA60): "CP2102",      # Silicon Labs CP210x (CP2102, CP2104)
    (0x10C4, 0xEA70): "CP2105",
    (0x1A86, 0x7523): "CH340",
    (0x1A86, 0x55D4): "CH9102",
    (0x0403, 0x6001): "FT232",
}

_ID_RE = re.compile(r"[^A-Za-z0-9_.-]")


def parse_kinds(raw: Any) -> list[str]:
    parts = raw if isinstance(raw, (list, tuple)) else str(raw or "").split(",")
    out: list[str] = []
    for p in parts:
        k = str(p).strip().lower()
        if k in PATTERNS and k not in out:
            out.append(k)
    return out


def new_device_key() -> str:
    return secrets.token_hex(12)


def _clean(value: Any, max_len: int, default: str) -> str:
    return _ID_RE.sub("", str(value or ""))[:max_len] or default


class AlarmController:
    """What the buzzer should be doing, whichever way the board is reached."""

    def __init__(self, settings, *, clock: Callable[[], float] = time.time):
        self.settings = settings
        self._clock = clock
        self._lock = threading.Lock()
        self._ringing: dict[Hashable, str] = {}      # incident id, or ("sensor", id) -> kind
        self._since: Optional[float] = None
        self._test_until = 0.0
        # Starts from the clock, so a board that silenced seq N before a
        # restart can't mistake the first new alarm for that old one.
        self._seq = int(clock())
        self._silenced: dict[str, Any] = {"by": None, "at": None}
        self._wifi: dict[str, dict[str, Any]] = {}
        self._listeners: list[Callable[[], None]] = []
        self._sos_listeners: list[Callable[[str], None]] = []
        self._sos_at: dict[str, float] = {}
        self._link_sources: list[Callable[[], list[dict[str, Any]]]] = []
        self.usb: Optional[SerialLink] = None
        if not str(settings.get("buzzer_device_key", "") or ""):
            settings.update({"buzzer_device_key": new_device_key()})

    # ---- settings ---------------------------------------------------------

    def enabled(self) -> bool:
        return bool(self.settings.get("buzzer_enabled", True))

    def kinds(self) -> list[str]:
        return parse_kinds(self.settings.get("buzzer_kinds", DEFAULT_KINDS))

    def max_seconds(self) -> int:
        try:
            return max(0, int(self.settings.get("buzzer_max_seconds", 60)))
        except (TypeError, ValueError):
            return 60

    def reload(self) -> None:
        """Settings were saved: drop alarms for kinds that are now off."""
        on = self.enabled()
        keep = set(self.kinds()) if on else set()
        with self._lock:
            # A sensor's own rule rang it ("sensor", id); only switching the
            # buzzer off stops that.
            drop = [i for i, k in self._ringing.items()
                    if not on or (k not in keep and not isinstance(i, tuple))]
            for iid in drop:
                del self._ringing[iid]
            stop_test = not on and bool(self._test_until)
            if stop_test:
                self._test_until = 0.0
            changed = bool(drop) or stop_test
            if changed:
                if not self._ringing:
                    self._since = None
                self._seq += 1
        if changed:
            self.notify()

    # ---- listeners ----------------------------------------------------------

    def add_listener(self, fn: Callable[[], None]) -> None:
        """fn() runs after every change (alarm, silence, a board connecting)."""
        self._listeners.append(fn)

    def add_link_source(self, fn: Callable[[], list[dict[str, Any]]]) -> None:
        """fn() lists more connected buzzers, e.g. on MQTT nodes (iot/hub.py)."""
        self._link_sources.append(fn)

    def notify(self) -> None:
        for fn in list(self._listeners):
            try:
                fn()
            except Exception as e:
                log.warning("alarm listener failed: %s", e)

    # ---- state --------------------------------------------------------------

    def _tick_locked(self, now: float) -> bool:
        """End a finished test and apply the time limit. True if changed."""
        changed = False
        if self._test_until and now >= self._test_until:
            self._test_until = 0.0
            changed = True
        limit = self.max_seconds()
        if self._ringing and limit and self._since is not None and now - self._since >= limit:
            self._ringing.clear()
            self._since = None
            self._silenced = {"by": "the time limit", "at": now}
            log.info("Alarm buzzer stopped after %d s; the incident stays open", limit)
            changed = True
        if changed:
            self._seq += 1
        return changed

    def _kind_locked(self) -> Optional[str]:
        kinds = set(self._ringing.values())
        return next((k for k in PRIORITY if k in kinds), None)

    def _pattern_locked(self) -> Optional[str]:
        if not self.enabled():
            return None
        if self._test_until:
            return "TEST"
        kind = self._kind_locked()
        return PATTERNS[kind] if kind else None

    def command(self) -> str:
        """The line every board should be acting on right now."""
        with self._lock:
            changed = self._tick_locked(self._clock())
            pattern = self._pattern_locked()
            line = f"ALARM {pattern} {self._seq}" if pattern else f"STOP {self._seq}"
        if changed:
            self.notify()
        return line

    def on_incident(self, change: str, inc: dict[str, Any]) -> None:
        """EventBus listener, also called when someone closes an incident."""
        iid, kind = inc.get("incident_id"), inc.get("kind")
        if iid is None or kind not in PATTERNS:
            return
        if inc.get("headline_type") in SENSOR_EVENTS and change != "resolved":
            return                      # the sensor's rule decides (ring / unring)
        now = self._clock()
        with self._lock:
            if change == "resolved":
                if self._ringing.pop(iid, None) is None:
                    return
                if not self._ringing:
                    self._since = None
                    self._silenced = {"by": inc.get("resolved_by") or "someone", "at": now}
            elif change in ("opened", "escalated") or (change == "updated" and kind == "sos"):
                # Repeat detections stay quiet, but every SOS press is a person
                # asking again: it sounds even after someone silenced it.
                if not self.enabled() or kind not in self.kinds():
                    return
                if change != "escalated" and iid in self._ringing:
                    return
                # An escalation is a new reason to sound, so the time limit
                # starts again from it.
                if not self._ringing or change == "escalated":
                    self._since = now
                self._ringing[iid] = kind
                self._test_until = 0.0
            else:
                return                      # repeat detections stay quiet
            self._seq += 1
        self.notify()

    def ring(self, key: Hashable, kind: str) -> bool:
        """Sound for something other than an incident's own rules: a sensor
        rule ("sensor", device_id), or someone pressing Sound alarm."""
        if kind not in PATTERNS:
            return False
        with self._lock:
            if not self.enabled() or self._ringing.get(key) == kind:
                return False
            if not self._ringing:
                self._since = self._clock()
            self._ringing[key] = kind
            self._test_until = 0.0
            self._seq += 1
        self.notify()
        return True

    def unring(self, key: Hashable) -> bool:
        """The sensor is back to normal or its alert was acknowledged."""
        with self._lock:
            if self._ringing.pop(key, None) is None:
                return False
            if not self._ringing:
                self._since = None
            self._seq += 1
        self.notify()
        return True

    def ringing(self, key: Hashable) -> bool:
        with self._lock:
            return key in self._ringing

    def silence(self, by: str) -> bool:
        """Stop the sound; the incidents themselves stay open."""
        now = self._clock()
        with self._lock:
            if not self._ringing and not self._test_until:
                return False
            self._ringing.clear()
            self._since = None
            self._test_until = 0.0
            self._silenced = {"by": by, "at": now}
            self._seq += 1
        log.info("Alarm buzzer silenced by %s", by)
        self.notify()
        return True

    def clear(self) -> None:
        """The incident history was deleted: nothing is left to sound for."""
        with self._lock:
            if not self._ringing:
                return
            self._ringing.clear()
            self._since = None
            self._seq += 1
        self.notify()

    def test(self) -> None:
        with self._lock:
            if self._ringing:
                raise RuntimeError("The buzzer is already sounding for an incident.")
            self._test_until = self._clock() + TEST_SECONDS
            self._seq += 1
        self.notify()

    # ---- SOS button ---------------------------------------------------------

    def add_sos_listener(self, fn: Callable[[str], None]) -> None:
        """fn(board name) runs when someone presses a board's SOS button."""
        self._sos_listeners.append(fn)

    def sos_pressed(self, device_id: Any) -> bool:
        """A board reported its SOS button. False for a repeat within
        SOS_REPEAT_S (the same press seen over USB and Wi-Fi, or a bounce)."""
        device = _clean(device_id, 32, "buzzer")
        now = self._clock()
        with self._lock:
            if now - self._sos_at.get(device, -1e18) < SOS_REPEAT_S:
                return False
            self._sos_at[device] = now
        log.warning("SOS button pressed on %s", device)
        for fn in list(self._sos_listeners):
            try:
                fn(device)
            except Exception as e:
                log.warning("SOS listener failed: %s", e)
        return True

    # ---- Wi-Fi boards -------------------------------------------------------

    def check_key(self, key: str) -> bool:
        saved = str(self.settings.get("buzzer_device_key", "") or "")
        key = str(key or "").strip()
        return bool(saved and key) and hmac.compare_digest(saved.encode(), key.encode())

    def new_key(self) -> str:
        key = new_device_key()
        self.settings.update({"buzzer_device_key": key})
        return key

    def wifi_seen(self, device_id: Any, ip: str, firmware: Any) -> None:
        device_id = _clean(device_id, 32, "buzzer")
        now = self._clock()
        with self._lock:
            prev = self._wifi.get(device_id)
            fresh = prev is None or now - prev["last_seen"] > WIFI_STALE_S
            self._wifi[device_id] = {"id": device_id, "ip": str(ip or "")[:64],
                                     "firmware": _clean(firmware, 16, "?"),
                                     "last_seen": now}
            if len(self._wifi) > 8:                  # forget the longest-silent one
                oldest = min(self._wifi.values(), key=lambda d: d["last_seen"])
                self._wifi.pop(oldest["id"], None)
        if fresh:
            log.info("Alarm buzzer %s connected over Wi-Fi from %s", device_id, ip)
            self.notify()

    # ---- status -------------------------------------------------------------

    def status(self, full: bool = False) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            changed = self._tick_locked(now)
            pattern = self._pattern_locked()
            kind = self._kind_locked() if pattern not in (None, "TEST") else None
            since = self._since
            silenced = dict(self._silenced)
            wifi = [dict(d, age_s=round(now - d["last_seen"], 1))
                    for d in self._wifi.values() if now - d["last_seen"] < WIFI_STALE_S]
        if changed:
            self.notify()
        usb = self.usb.status() if self.usb is not None else {
            "available": False, "connected": False, "port": None,
            "firmware": None, "error": None}
        links: list[dict[str, Any]] = []
        if usb["connected"]:
            links.append({"via": "usb", "port": usb["port"], "firmware": usb["firmware"],
                          "id": usb.get("id")})
        links += [{"via": "wifi", "id": d["id"], "ip": d["ip"],
                   "firmware": d["firmware"], "age_s": d["age_s"]} for d in wifi]
        for source in list(self._link_sources):
            try:
                links += source()
            except Exception as e:
                log.debug("alarm link source failed: %s", e)
        out: dict[str, Any] = {
            "enabled": self.enabled(),
            "connected": bool(links),
            "sounding": kind is not None,
            "testing": pattern == "TEST",
            "kind": kind,
            "since": since,
            "silenced_by": silenced["by"],
            "silenced_at": silenced["at"],
        }
        if full:
            out.update(links=links, usb=usb, kinds=self.kinds(),
                       max_seconds=self.max_seconds(),
                       port_setting=str(self.settings.get("buzzer_port", "auto") or "auto"))
        return out


# ---- USB ------------------------------------------------------------------

def serial_available() -> bool:
    try:
        import serial  # noqa: F401
    except ImportError:
        return False
    return True


def list_serial_ports() -> list[dict[str, Any]]:
    """Serial ports on this computer; ESP32-style USB chips first."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    out = []
    for p in list_ports.comports():
        bridge = USB_BRIDGES.get((p.vid, p.pid)) if p.vid is not None else None
        out.append({"device": p.device, "description": p.description or p.device,
                    "bridge": bridge})
    out.sort(key=lambda p: (p["bridge"] is None, p["device"]))
    return out


def open_serial(device: str):
    import serial
    conn = serial.Serial()
    conn.port = device
    conn.baudrate = BAUD
    conn.timeout = 0.1
    conn.write_timeout = 1.0
    # On a DevKit, DTR and RTS drive the EN and BOOT pins through the
    # auto-reset circuit. Holding both low keeps the board running when the
    # port opens instead of resetting it (or leaving it in the bootloader).
    conn.dtr = False
    conn.rts = False
    conn.open()
    return conn


def _open_error(device: str, e: Exception) -> str:
    text = str(e)
    if "PermissionError" in text or "Access is denied" in text or "busy" in text.lower():
        return (f"{device} is in use by another program. Close the Arduino "
                f"Serial Monitor or uploader, and HomeShield will connect.")
    if "FileNotFoundError" in text or "could not find" in text.lower():
        return f"{device} isn't there. Check the USB cable, or pick Find automatically."
    return f"Couldn't open {device} ({text})"


class SerialLink:
    """Finds the ESP32 on USB and keeps telling it what to play."""

    RETRY_S = 5.0
    BAD_PORT_S = 30.0       # a port that opened but didn't answer as our board
    RESEND_S = 2.0          # the current state, resent, is also the heartbeat
    HANDSHAKE_S = 4.0       # covers the board rebooting as the port opens

    def __init__(self, alarm: AlarmController, settings, *,
                 opener: Optional[Callable[[str], Any]] = None,
                 list_ports: Optional[Callable[[], list[dict[str, Any]]]] = None,
                 clock: Callable[[], float] = time.monotonic):
        self.alarm = alarm
        self.settings = settings
        self._opener = opener or open_serial
        self._list_ports = list_ports or list_serial_ports
        self._available = (lambda: True) if opener else serial_available
        self._clock = clock
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._conn = None
        self._bad: dict[str, float] = {}
        self.port: Optional[str] = None
        self.firmware: Optional[str] = None
        self.device_id: Optional[str] = None      # from the board's HELLO (id=...)
        self.error: Optional[str] = None
        alarm.usb = self

    # ---- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="hs-buzzer-usb",
                                            daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._close(None)

    def wake(self) -> None:
        """Retry at once, e.g. after the port setting changed."""
        self._wake.set()

    def status(self) -> dict[str, Any]:
        return {"available": self._available(), "connected": self._conn is not None,
                "port": self.port if self._conn is not None else None,
                "firmware": self.firmware, "id": self.device_id, "error": self.error}

    # ---- loop ---------------------------------------------------------------

    def _wanted_port(self) -> str:
        port = str(self.settings.get("buzzer_port", "auto") or "auto").strip()
        return "auto" if port.lower() == "auto" else port

    def _run(self) -> None:
        next_try = 0.0
        last_line, last_sent = None, 0.0
        while not self._stop.is_set():
            wanted = self._wanted_port()
            if not self.alarm.enabled():
                if self._conn is not None:
                    self._close(None)
                self.error = None
                self._wait(1.0)
                next_try = 0.0
                continue
            if (self._conn is not None and wanted != "auto"
                    and wanted.upper() != (self.port or "").upper()):
                self._close(None)
                next_try = 0.0
            if self._conn is None:
                if self._wake.is_set():
                    self._wake.clear()
                    next_try = 0.0
                if self._clock() < next_try:
                    self._wait(min(1.0, next_try - self._clock()))
                    continue
                if not self._connect(wanted):
                    next_try = self._clock() + self.RETRY_S
                    continue
                last_line, last_sent = None, 0.0
            try:
                line = self.alarm.command()
                now = self._clock()
                if line != last_line or now - last_sent >= self.RESEND_S:
                    self._conn.write((line + "\n").encode("ascii"))
                    last_line, last_sent = line, now
                raw = self._conn.readline()          # waits up to 0.1 s
                if raw:
                    self._handle(raw)
            except Exception as e:                   # unplugged, driver gone
                self._close(f"The connection on {self.port} was lost ({e}).")
                next_try = self._clock() + 1.0

    def _wait(self, seconds: float) -> None:
        if self._wake.wait(max(0.0, seconds)):
            self._wake.clear()

    def _handle(self, raw: bytes) -> None:
        text = raw.decode("ascii", "replace").strip()
        if text == "BTN":
            self.alarm.silence("the button on the buzzer")
        elif text == "SOS":
            self.alarm.sos_pressed(self.device_id or "buzzer")
        elif text.startswith(HELLO):
            self._hello(text)
        elif text:
            log.debug("buzzer: %s", text)

    def _hello(self, text: str) -> str:
        """'HOMESHIELD-BUZZER 1.1 wifi=off id=bedroom': remember the version
        and the board's name; returns the version."""
        words = text[len(HELLO):].split() or ["?"]
        self.firmware = words[0]
        for w in words[1:]:
            if w.startswith("id="):
                self.device_id = _clean(w[3:], 32, "") or None
        return self.firmware

    def _connect(self, wanted: str) -> bool:
        if not self._available():
            self.error = "pyserial is not installed. Run: pip install pyserial"
            return False
        if wanted == "auto":
            now = self._clock()
            ports = [p["device"] for p in self._list_ports() if p.get("bridge")]
            if not ports:
                self.error = ("No ESP32 found on USB. Plug it in with a data cable "
                              "(some USB cables only carry power).")
                return False
            candidates = [d for d in ports if self._bad.get(d, 0.0) <= now]
            if not candidates:
                return False                          # keep the last error
        else:
            candidates = [wanted]
        errors = []
        for device in candidates:
            try:
                conn = self._opener(device)
            except Exception as e:
                errors.append(_open_error(device, e))
                continue
            firmware = self._handshake(conn)
            if firmware is not None:
                self._conn, self.port, self.firmware, self.error = conn, device, firmware, None
                log.info("Alarm buzzer connected over USB on %s (firmware %s)",
                         device, firmware)
                self.alarm.notify()
                return True
            try:
                conn.close()
            except Exception:
                pass
            self._bad[device] = self._clock() + self.BAD_PORT_S
            errors.append(f"{device} opened, but no HomeShield buzzer answered. Upload "
                          f"firmware/HomeShieldBuzzer to the ESP32 (see README).")
        self.error = errors[0] if errors else None
        return False

    def _handshake(self, conn) -> Optional[str]:
        deadline = self._clock() + self.HANDSHAKE_S
        next_hello = 0.0
        while self._clock() < deadline and not self._stop.is_set():
            if self._clock() >= next_hello:
                conn.write(f"{HELLO_REQUEST}\n".encode("ascii"))
                next_hello = self._clock() + 0.5
            raw = conn.readline()
            if raw:
                text = raw.decode("ascii", "replace").strip()
                if text.startswith(HELLO):
                    return self._hello(text)
        return None

    def _close(self, reason: Optional[str]) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            conn.close()
        except Exception:
            pass
        self.firmware = None
        self.error = reason
        if reason:
            log.warning("Alarm buzzer: %s", reason)
        self.alarm.notify()
