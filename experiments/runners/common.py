"""Shared helpers for the standalone RQ3/RQ4 runners: locks, atomic writes, sharding, provenance, progress."""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import platform
import socket
import subprocess
import sys
import threading
import time

RUNNERS = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(RUNNERS))      # repository root (runners live in experiments/runners)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

OPENJML_VERSION = "21-0.21"                          # the version every RQ1/RQ3/RQ4 run uses
# OpenJML's JVM options, identical in every phase and on every machine (the wrapper script reads it)
os.environ.setdefault("OPENJML_JVM", "-Xmx8g")
EXPERIMENT_TIMEOUT = 300                              # seconds, every verification in every experiment


def now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def log(msg: str, fh=None) -> None:
    line = f"[{time.strftime('%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    if fh is not None:
        fh.write(line + "\n")
        fh.flush()


def atomic_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, indent=1)
    os.replace(tmp, path)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _proc_start(pid: int) -> str:
    """Start time of a process (clock ticks since boot, /proc/<pid>/stat field 22); "" if unknown. With the
    pid it identifies a process even after its pid has been reused."""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return ""


def try_lock(path: str, stale_hours: float = 12.0) -> bool:
    """Claim a work unit. The lock file is created atomically with its content (host:pid:process start:time,
    written to a temporary file and hard-linked into place). A lock is stale, and taken over, when its owner
    is on this host and that process no longer exists (pid dead, or reused by another process), or when it
    is older than stale_hours (owner on another host). Returns False if someone else holds it."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    me = f"{socket.gethostname()}:{os.getpid()}:{_proc_start(os.getpid())}:{time.time():.0f}"
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    for _ in range(3):
        with open(tmp, "w") as fh:
            fh.write(me)
        try:
            os.link(tmp, path)
            return True
        except FileExistsError:
            pass
        finally:
            try:
                os.remove(tmp)
            except FileNotFoundError:
                pass
        try:
            host, pid, start, ts = open(path).read().split(":")
            pid = int(pid)
            if host == socket.gethostname():
                stale = not _pid_alive(pid) or (start and _proc_start(pid) != start)
            else:
                stale = time.time() - float(ts) > stale_hours * 3600
        except (ValueError, OSError):
            stale = True                      # unreadable / old-format lock
        if not stale:
            return False
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    return False


def unlock(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def shard_of(unit_id: str, n: int) -> int:
    return int(hashlib.sha256(unit_id.encode()).hexdigest(), 16) % n


def parse_shard(s: str | None) -> tuple[int, int]:
    if not s:
        return 0, 1
    i, n = s.split("/")
    i, n = int(i), int(n)
    if not 0 <= i < n:
        raise SystemExit(f"--shard {s}: need 0 <= i < n")
    return i, n


def bundle_info() -> dict:
    """Provenance of the code this run uses (the bundle manifest hash if packaged, else a hash of the
    harness sources), plus host and tool versions."""
    manifest = os.path.join(REPO, "BUNDLE_MANIFEST.sha256")
    if os.path.exists(manifest):
        bundle = hashlib.sha256(open(manifest, "rb").read()).hexdigest()[:16]
    else:
        h = hashlib.sha256()
        for rel in ("spec_harness/harness.py", "spec_harness/eval_spec.py", "spec_harness/eval_llm_response.py"):
            h.update(open(os.path.join(REPO, rel), "rb").read())
        bundle = "src-" + h.hexdigest()[:16]
    return {"bundle": bundle, "host": socket.gethostname(), "python": platform.python_version(),
            "openjml": openjml_version(), "cpus": os.cpu_count()}


def openjml_version(openjml: str = "openjml") -> str:
    try:
        r = subprocess.run([openjml, "--version"], capture_output=True, text=True, timeout=120)
        return (r.stdout + r.stderr).strip().splitlines()[0].split()[-1]
    except Exception as e:                         # reported by preflight
        return f"unavailable ({type(e).__name__})"


class Progress:
    """Throughput and ETA for long runs, written to <out>/progress.log."""

    def __init__(self, total: int, path: str):
        self.total, self.done, self.t0 = total, 0, time.time()
        self.lock = threading.Lock()
        self.fh = open(path, "a")

    def tick(self, msg: str) -> None:
        with self.lock:
            self.done += 1
            el = time.time() - self.t0
            rate = self.done / el if el else 0
            eta = (self.total - self.done) / rate / 3600 if rate else float("nan")
            log(f"{self.done}/{self.total} {msg} | {rate * 3600:.0f}/h, ETA {eta:.1f} h", self.fh)
