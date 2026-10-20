"""HomeShield's own Mosquitto MQTT broker, started and stopped with it.

The Mosquitto installer also sets up a Windows service, but that one only
listens to this computer itself (port 1883) and changing it means editing
files under Program Files. Instead HomeShield runs a second copy as a child
process, like the Cloudflare tunnel, with its own files in mqtt/ beside the
database:

    mosquitto.conf  listener on PORT (1884) on every network interface, no
                    anonymous clients
    passwd          two accounts: "homeshield" (this server, a random
                    password kept in settings) and "node" (every board,
                    whose password is the device key from Settings)
    acl             homeshield may use every topic; a board only
                    homeshield/<its client id>/# and may read homeshield/alarm

A board's client id is its node name (node-kitchen) or esp32-<chip> before
adoption, so one board can't write another board's topics.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional

from ..tunnel import tie_to_this_process

log = logging.getLogger(__name__)

DEFAULT_PORT = 1884
SERVER_USER = "homeshield"
NODE_USER = "node"
_WINDOWS_DIRS = (r"C:\Program Files\mosquitto", r"C:\Program Files (x86)\mosquitto")


def find_mosquitto() -> Optional[Path]:
    """MOSQUITTO_PATH, then PATH, then where the Windows installer puts it."""
    env = os.environ.get("MOSQUITTO_PATH", "").strip()
    if env and Path(env).is_file():
        return Path(env)
    found = shutil.which("mosquitto")
    if found:
        return Path(found)
    return next((Path(d) / "mosquitto.exe" for d in _WINDOWS_DIRS
                 if (Path(d) / "mosquitto.exe").is_file()), None)


def _short(path: Path) -> str:
    """Mosquitto reads a path up to the first space: use the 8.3 form if needed."""
    text = str(path)
    if " " in text and os.name == "nt":
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.kernel32.GetShortPathNameW(text, buf, 1024):
            text = buf.value
    return text.replace("\\", "/")


def hash_password(password: str, iterations: int = 1000) -> str:
    """A Mosquitto password-file hash ($7$: PBKDF2-HMAC-SHA512), made here
    rather than with mosquitto_passwd, which on Windows can't add a second
    user to a file and locks the files it makes against the user running it."""
    salt = secrets.token_bytes(64)
    digest = hashlib.pbkdf2_hmac("sha512", password.encode("utf-8"), salt, iterations, 64)
    return (f"$7${iterations}${base64.b64encode(salt).decode()}"
            f"${base64.b64encode(digest).decode()}")


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


class Broker:
    def __init__(self, data_dir, settings, *, port: int = DEFAULT_PORT,
                 binary: Optional[Path] = None):
        self.dir = Path(data_dir)
        self.settings = settings
        self.port = int(port)
        self.binary = binary if binary is not None else find_mosquitto()
        self.proc: Optional[subprocess.Popen] = None
        self._job: Any = None
        self.error: Optional[str] = None
        self._stopping = False

    # ---- credentials ----------------------------------------------------------

    def server_password(self) -> str:
        pw = str(self.settings.get("mqtt_server_password", "") or "")
        if not pw:
            pw = secrets.token_hex(16)
            self.settings.update({"mqtt_server_password": pw})
        return pw

    def node_password(self) -> str:
        return str(self.settings.get("buzzer_device_key", "") or "")

    def _write_files(self) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        passwd, acl, conf = self.dir / "passwd", self.dir / "acl", self.dir / "mosquitto.conf"
        passwd.write_text("".join(
            f"{user}:{hash_password(pw)}\n" for user, pw in
            ((SERVER_USER, self.server_password()), (NODE_USER, self.node_password()))),
            encoding="utf-8")
        acl.write_text(
            f"user {SERVER_USER}\n"
            f"topic readwrite homeshield/#\n\n"
            f"user {NODE_USER}\n"
            f"topic read homeshield/alarm\n\n"
            f"pattern readwrite homeshield/%c/#\n", encoding="utf-8")
        conf.write_text(
            "# Written by HomeShield on every start; edits are overwritten.\n"
            "per_listener_settings false\n"
            "allow_anonymous false\n"
            f"password_file {_short(passwd)}\n"
            f"acl_file {_short(acl)}\n"
            "persistence false\n"
            "log_dest stdout\n"
            "log_type error\nlog_type warning\nlog_type notice\n"
            f"listener {self.port}\n", encoding="utf-8")
        return conf

    # ---- process --------------------------------------------------------------

    def start(self, wait: float = 5.0) -> bool:
        if self.running:
            return True
        if self.binary is None or not Path(self.binary).is_file():
            self.error = "not_installed"
            return False
        if not self.node_password():
            self.error = "no_key"
            return False
        if port_in_use(self.port):
            self.error = f"port_in_use"
            return False
        try:
            conf = self._write_files()
        except OSError as e:
            self.error = f"could not write the broker's files ({e})"
            return False
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self.proc = subprocess.Popen(
                [str(self.binary), "-c", str(conf)], stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
                encoding="utf-8", errors="replace", creationflags=flags)
        except OSError as e:
            self.error = f"could not start Mosquitto ({e})"
            return False
        try:
            self._job = tie_to_this_process(self.proc)
        except Exception as e:
            log.debug("could not tie mosquitto to HomeShield: %s", e)
        threading.Thread(target=self._watch, args=(self.proc,), name="hs-mosquitto",
                         daemon=True).start()
        end = time.monotonic() + wait
        while time.monotonic() < end:
            if self.proc.poll() is not None:
                break
            if port_in_use(self.port):
                self.error = None
                log.info("MQTT broker listening on port %d", self.port)
                return True
            time.sleep(0.1)
        if self.proc.poll() is None:
            self.error = "Mosquitto started but isn't listening yet."
        return self.running

    def _watch(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            if "Error:" in line:                 # the broker's own failures
                log.warning("mosquitto: %s", line)
                self.error = line.split("Error:", 1)[-1].strip()[:200]
            elif "Warning:" in line:
                log.warning("mosquitto: %s", line)
            else:                                # clients coming and going
                log.debug("mosquitto: %s", line)
        code = proc.wait()
        if self.proc is proc and not self._stopping:
            self.error = self.error or f"Mosquitto stopped (code {code})"
            log.warning("MQTT broker stopped: %s", self.error)

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        self._stopping = True
        p, self.proc = self.proc, None
        if p is not None and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        self._stopping = False

    def restart(self) -> bool:
        """New device key: rewrite the password file and start again."""
        self.stop()
        time.sleep(0.3)
        return self.start()

    def status(self) -> dict[str, Any]:
        return {"running": self.running, "port": self.port, "error": self.error,
                "installed": self.binary is not None and Path(self.binary).is_file()}
