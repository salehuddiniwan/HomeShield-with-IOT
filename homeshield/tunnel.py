"""Remote access through a Cloudflare Tunnel.

cloudflared opens an outbound connection to Cloudflare and forwards HTTPS
requests for a public address to this computer, so no port on the home
router is opened and Cloudflare provides the certificate. Two modes:

- quick: ``cloudflared tunnel --url http://127.0.0.1:PORT`` gives a random
  https://<words>.trycloudflare.com address with no Cloudflare account. The
  address changes on every start, has no uptime guarantee and does not carry
  Server-Sent Events, so the dashboard polls for alerts instead.
- named: ``cloudflared tunnel run`` with a token for a tunnel created in the
  Cloudflare dashboard. It keeps a fixed hostname and carries SSE.

The tunnel is a child process of HomeShield: it starts after the app is
built and is stopped when HomeShield exits.
"""

from __future__ import annotations

import atexit
import logging
import os
import re
import shutil
import subprocess
import threading
from typing import Any, Optional

log = logging.getLogger(__name__)

QUICK_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
_WINDOWS_PATHS = (r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
                  r"C:\Program Files\cloudflared\cloudflared.exe")


def tie_to_this_process(proc: subprocess.Popen) -> Any:
    """Windows: put cloudflared in a job that is killed when HomeShield's
    process ends, so a crash or End task never leaves a public tunnel
    running on its own. Returns the job handle, which must stay open."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int,
                                            ctypes.c_void_p, wintypes.DWORD)
    k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)

    job = k32.CreateJobObjectW(None, None)
    info = Extended()
    info.BasicLimitInformation.LimitFlags = 0x2000      # KILL_ON_JOB_CLOSE
    if not job or not k32.SetInformationJobObject(job, 9, ctypes.byref(info),
                                                  ctypes.sizeof(info)):
        return None
    handle = k32.OpenProcess(0x0101, False, proc.pid)    # SET_QUOTA | TERMINATE
    ok = bool(handle) and bool(k32.AssignProcessToJobObject(job, handle))
    if handle:
        k32.CloseHandle(handle)
    return job if ok else None


# cloudflared reports a viewer closing a live stream (another camera picked,
# tab hidden, phone locked) as an ERR, because a video feed never ends by
# itself. Those are normal; only other errors are worth a warning.
_VIEWER_CLOSED = ("canceled by remote", "context canceled",
                  "Incoming request ended abruptly")


def is_real_error(line: str) -> bool:
    if not (" ERR " in line or line.startswith("ERR")):
        return False
    return not any(s in line for s in _VIEWER_CLOSED)


def find_cloudflared() -> Optional[str]:
    """CLOUDFLARED_PATH, then PATH, then the folder winget installs into."""
    env = os.environ.get("CLOUDFLARED_PATH", "").strip()
    if env and os.path.isfile(env):
        return env
    found = shutil.which("cloudflared")
    if found:
        return found
    return next((p for p in _WINDOWS_PATHS if os.path.isfile(p)), None)


class Tunnel:
    def __init__(self, port: int, token: str = "", public_url: str = "",
                 binary: Optional[str] = None):
        self.port = int(port)
        self.token = token.strip()
        self.mode = "named" if self.token else "quick"
        # A named tunnel's hostname is set in the Cloudflare dashboard and
        # cloudflared doesn't print it; --public-url lets the admin see it.
        self.url: str = public_url.strip()
        self.binary = binary if binary is not None else find_cloudflared()
        self.error: str = ""
        self.connected = False
        self.proc: Optional[subprocess.Popen] = None
        self._job: Any = None
        self._ready = threading.Event()

    @classmethod
    def blocked(cls, reason: str, port: int = 0, token: str = "") -> "Tunnel":
        t = cls(port, token=token, binary="")
        t.error = reason
        return t

    def start(self, wait: float = 20.0) -> bool:
        if self.error:
            return False
        if not self.binary:
            self.error = "not_installed"
            return False
        cmd = [self.binary, "tunnel", "--no-autoupdate"]
        env = dict(os.environ)
        if self.mode == "quick":
            cmd += ["--url", f"http://127.0.0.1:{self.port}"]
        else:
            # The token goes in the environment, not on the command line,
            # so it doesn't show up in the process list.
            cmd += ["run"]
            env["TUNNEL_TOKEN"] = self.token
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                errors="replace", env=env, creationflags=flags)
        except OSError as e:
            self.error = f"could not start cloudflared: {e}"
            return False
        try:
            self._job = tie_to_this_process(self.proc)
        except Exception as e:                     # never worth losing the tunnel over
            log.debug("could not tie cloudflared to HomeShield: %s", e)
        threading.Thread(target=self._watch, args=(self.proc,), name="hs-tunnel",
                         daemon=True).start()
        atexit.register(self.stop)
        self._ready.wait(wait)
        return self.running

    def _watch(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            if self.mode == "quick" and not self.url:
                m = QUICK_URL_RE.search(line)
                if m:
                    self.url = m.group(0)
            if "Registered tunnel connection" in line:
                self.connected = True
            if self.connected and (self.url or self.mode == "named"):
                self._ready.set()
            if is_real_error(line):
                log.warning("cloudflared: %s", line)
            else:
                log.debug("cloudflared: %s", line)
        code = proc.wait()
        self.connected = False
        if self.proc is proc and not self.error:     # not a stop() we asked for
            self.error = f"cloudflared exited (code {code})"
            log.warning("Remote access stopped: %s", self.error)
        self._ready.set()

    @property
    def running(self) -> bool:
        return (self.proc is not None and self.proc.poll() is None
                and self.connected and (self.mode == "named" or bool(self.url)))

    def stop(self) -> None:
        p, self.proc = self.proc, None
        if p is None or p.poll() is not None:
            return
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()

    def status(self) -> dict[str, Any]:
        return {"mode": self.mode, "url": self.url or None,
                "running": self.running, "error": self.error or None}
