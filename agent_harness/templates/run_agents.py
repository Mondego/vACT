#!/usr/bin/env python3
"""Parent-level driver — fan out one fresh agent session per task directory.

Run this from the out-root produced by ``agent_harness.scaffold``. For each task
subdir (any immediate child containing ``AGENTS.md``) it launches a headless
coding-agent session with that subdir as the working directory, feeding the dir's
``AGENTS.md`` as the prompt. One isolated session per task, stdin = /dev/null,
in its own session / process group.

Everything a session produces is recorded under ``<task>/harness/``:

    agent_run.log       the agent's stdout. Structured JSON events are enforced:
                          claude -> --output-format stream-json --verbose
                          codex  -> exec --json
    agent_stderr.log    the agent's stderr (kept apart so the JSON stays parseable)
    codex_rollout.jsonl Codex only: the full session rollout copied from
                        $CODEX_HOME/sessions
    run_meta.json       attempt number, command (prompt elided), CLI version, model,
                        host, start/end, seconds, returncode, timed_out, interrupted,
                        submission_exists, hashes of AGENTS.md and the tools (start/end)
    tool_calls.jsonl    written by the tools themselves (harness/cli.py): one line
                        per verify/harness/submit call
    harness_stderr.log  everything the tools printed (OpenJML, harness banner)
    attempt_<n>/        the complete state of earlier sessions of this task
    run.lock            per-task lock (flock), held while a session of the task lives

Retry policy
------------
Every session starts from a fresh task state: whatever an earlier session left
(edited Solution.java, submission.json, .attempts, last_valid_Solution.java, logs,
harness outputs, any file the agent created) is moved into
``harness/attempt_<n>/`` and the original Solution.java is restored.

``--only-missing`` runs a task only if it never ran (no run_meta.json) or its last
session failed for an infrastructure reason (non-zero exit, timeout, or interrupted
by a signal). A session that ended normally is never re-run, with or without a
submission.

Locking: a task whose ``harness/run.lock`` is held (flock) by a live process on this
host is skipped, so two runs never work on the same task. The lock descriptor is
inherited by the agent session, so it stays held while any process of the session
lives.

Stopping: SIGINT / SIGTERM / SIGHUP kill every running session (its process group
and every process carrying its session marker, including OpenJML groups started
with setsid), record them as interrupted, and exit.

Summarize any out-root with ``python usage_report.py``; check the agents' actions
with ``python audit.py``.

This script is self-contained (stdlib only); it ships inside the out-root.

Examples
--------
    python run_agents.py --agent claude --threads 4 --only-missing
    python run_agents.py --agent codex  --threads 4 --only-missing
    python run_agents.py --cmd 'claude -p {agents_md} --dangerously-skip-permissions --model claude-opus-4-8'
"""

from __future__ import annotations

import argparse
import datetime
import fcntl
import glob
import hashlib
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.abspath(__file__))

# Built-in agent command templates. Tokens are replaced per task:
#   {agents_md}       -> full text of the dir's AGENTS.md (single argv token)
#   {agents_md_path}  -> absolute path to AGENTS.md
#   {task_dir}        -> absolute path to the task directory
#   {task_id}         -> directory name
BUILTIN_AGENTS = {
    # No MCP servers and a minimal tool set keep Claude Code's startup light.
    # AUTH: `claude /login` or ANTHROPIC_API_KEY. Do not add --bare unless using an
    # API key (it skips the keychain).
    "claude": (
        "claude -p {agents_md} --dangerously-skip-permissions "
        "--model claude-sonnet-4-6 --strict-mcp-config --mcp-config \"\" "
        "--tools Read,Edit,Write,Bash --disable-slash-commands --no-session-persistence "
        "--output-format stream-json --verbose"
    ),
    # Codex without its bubblewrap sandbox (bwrap often cannot run in containers);
    # only on an already-isolated host. Model comes from ~/.codex/config.toml unless
    # -m is given via --cmd.
    "codex": (
        "codex exec --json --skip-git-repo-check "
        "--dangerously-bypass-approvals-and-sandbox {agents_md}"
    ),
}

# What scaffold puts in a task dir; everything else is session state (archived before a new session).
SCAFFOLD_TOP = {"AGENTS.md", "Solution.java", "verify.sh", "run_specharness.sh", "submit.sh", "harness"}
SCAFFOLD_HARNESS = {"cli.py", "harness_tool.py", "verifier_tool.py", "_env.sh", "requirements.txt",
                    "task.json", "config.json", "__pycache__", "run.lock", "previous_runs"}
TOOL_FILES = ["harness/cli.py", "harness/harness_tool.py", "harness/verifier_tool.py", "harness/_env.sh",
              "harness/task.json", "harness/config.json", "verify.sh", "run_specharness.sh", "submit.sh",
              "AGENTS.md"]
LOCK_NAME = "run.lock"
MARKER_VAR = "RQ4_AGENT_SESSION"        # env marker inherited by every process of a session


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="milliseconds")


def _sha256_file(path: str) -> str | None:
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def _read_int(path: str) -> int | None:
    try:
        with open(path) as fh:
            return int(fh.read().strip() or 0)
    except (OSError, ValueError):
        return None


def _load_json(path: str):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _agent_kind(argv: list[str]) -> str:
    return os.path.basename(argv[0]) if argv else ""


def enforce_structured_output(argv: list[str]) -> tuple[list[str], list[str]]:
    """Make sure the agent emits machine-readable events. Returns (argv, flags_added)."""
    kind, added = _agent_kind(argv), []
    argv = list(argv)
    if kind == "claude":
        if "--output-format" in argv:
            i = argv.index("--output-format")
            if i + 1 < len(argv) and argv[i + 1] != "stream-json":
                argv[i + 1] = "stream-json"
                added.append("--output-format stream-json (replaced)")
        else:
            argv += ["--output-format", "stream-json"]
            added.append("--output-format stream-json")
        if "--verbose" not in argv:           # required by stream-json in -p mode
            argv.append("--verbose")
            added.append("--verbose")
    elif kind == "codex" and "exec" in argv and "--json" not in argv:
        argv.insert(argv.index("exec") + 1, "--json")
        added.append("--json")
    return argv, added


def _model_of(argv: list[str]) -> str | None:
    for flag in ("--model", "-m"):
        if flag in argv:
            i = argv.index(flag)
            if i + 1 < len(argv):
                return argv[i + 1]
    if _agent_kind(argv) == "codex":
        cfg = os.path.join(os.environ.get("CODEX_HOME", os.path.expanduser("~/.codex")), "config.toml")
        try:
            for line in open(cfg):
                if line.strip().startswith("model") and "=" in line and not line.strip().startswith("model_"):
                    return line.split("=", 1)[1].strip().strip('"').strip("'") + " (from config.toml)"
        except OSError:
            pass
    return None


_VERSION_CACHE: dict[str, str | None] = {}


def _cli_version(binary: str) -> str | None:
    if binary not in _VERSION_CACHE:
        try:
            out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=60,
                                 stdin=subprocess.DEVNULL)
            _VERSION_CACHE[binary] = (out.stdout or out.stderr).strip() or None
        except Exception:  # noqa: BLE001
            _VERSION_CACHE[binary] = None
    return _VERSION_CACHE[binary]


def codex_thread_id(log_path: str) -> str | None:
    try:
        for line in open(log_path, errors="ignore"):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            for key in ("thread_id", "session_id", "conversation_id"):
                if isinstance(ev.get(key), str):
                    return ev[key]
            msg = ev.get("msg") if isinstance(ev.get("msg"), dict) else {}
            if isinstance(msg.get("session_id"), str):
                return msg["session_id"]
    except OSError:
        pass
    return None


def copy_codex_rollout(task_dir: str, thread_id: str | None, started: float) -> str | None:
    """Copy the Codex session rollout for this session into harness/codex_rollout.jsonl."""
    home = os.environ.get("CODEX_HOME", os.path.expanduser("~/.codex"))
    cands = []
    if thread_id:
        cands = glob.glob(os.path.join(home, "sessions", "**", f"rollout-*{thread_id}*.jsonl"), recursive=True)
    if not cands:  # fall back to rollouts written during this session whose cwd matches
        for p in glob.glob(os.path.join(home, "sessions", "**", "rollout-*.jsonl"), recursive=True):
            try:
                if os.path.getmtime(p) >= started - 5:
                    with open(p, errors="ignore") as fh:
                        head = fh.read(4000)
                    if task_dir in head:
                        cands.append(p)
            except OSError:
                pass
    if not cands:
        return None
    src = max(cands, key=os.path.getmtime)
    shutil.copy2(src, os.path.join(task_dir, "harness", "codex_rollout.jsonl"))
    return src


# ---------------------------------------------------------------------------
# retry policy, fresh state, lock
# ---------------------------------------------------------------------------

def last_outcome(task_dir: str) -> str:
    """'never_ran' | 'infra_failed' | 'ended' for the task's last session (harness/run_meta.json)."""
    meta = _load_json(os.path.join(task_dir, "harness", "run_meta.json"))
    if not isinstance(meta, dict):
        return "never_ran"
    if meta.get("returncode") != 0 or meta.get("timed_out") or meta.get("interrupted"):
        return "infra_failed"
    return "ended"


def needs_run(task_dir: str, only_missing: bool) -> bool:
    return not only_missing or last_outcome(task_dir) != "ended"


def _attempt_dirs(harness: str) -> list[int]:
    out = []
    for name in os.listdir(harness):
        if name.startswith("attempt_") and name[8:].isdigit():
            out.append(int(name[8:]))
    return sorted(out)


def fresh_state(task_dir: str) -> tuple[int, list[str]]:
    """Move everything an earlier session left into harness/attempt_<k>/ and restore the original
    Solution.java. Returns (attempt number of the session about to start, archived names)."""
    harness = os.path.join(task_dir, "harness")
    task = _load_json(os.path.join(harness, "task.json")) or {}
    original = task.get("code", "")
    done = _attempt_dirs(harness)
    last_done = done[-1] if done else 0
    prev_meta = _load_json(os.path.join(harness, "run_meta.json"))
    k = prev_meta.get("attempt") if isinstance(prev_meta, dict) else None
    if not isinstance(k, int) or k <= last_done:        # no / old run_meta: the next free number
        k = last_done + 1
    dest = os.path.join(harness, f"attempt_{k}")        # the previous session's state goes here
    moved: list[str] = []

    def _move(src: str, rel: str) -> None:
        os.makedirs(os.path.dirname(os.path.join(dest, rel)), exist_ok=True)
        shutil.move(src, os.path.join(dest, rel))
        moved.append(rel)

    for name in sorted(os.listdir(harness)):
        if name in SCAFFOLD_HARNESS or name.startswith("attempt_"):
            continue
        _move(os.path.join(harness, name), os.path.join("harness", name))
    for name in sorted(os.listdir(task_dir)):
        if name in SCAFFOLD_TOP:
            continue
        _move(os.path.join(task_dir, name), name)          # submission.json, files the agent created
    sol = os.path.join(task_dir, "Solution.java")
    cur = open(sol).read() if os.path.exists(sol) else None
    if cur is not None and cur != original:
        os.makedirs(dest, exist_ok=True)
        shutil.copy2(sol, os.path.join(dest, "Solution.java"))
        moved.append("Solution.java")
    if cur != original:
        with open(sol, "w") as fh:
            fh.write(original)
    return (k + 1 if moved else last_done + 1), moved


class TaskLock:
    """harness/run.lock: an flock held by this runner and inherited by the agent session."""

    def __init__(self, task_dir: str):
        self.path = os.path.join(task_dir, "harness", LOCK_NAME)
        self.fd: int | None = None

    def acquire(self) -> bool:
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "since": _now()}).encode())
        self.fd = fd
        return True

    def holder(self) -> str:
        try:
            with open(self.path) as fh:
                return fh.read().strip()
        except OSError:
            return ""

    def release(self) -> None:
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None


# ---------------------------------------------------------------------------
# process control
# ---------------------------------------------------------------------------

STOP = threading.Event()
_RUNNING: dict[int, str] = {}           # session pid -> marker
_RUNNING_LOCK = threading.Lock()


def _marked_pids(marker: str) -> list[int]:
    """Every process whose environment carries this session's marker (setsid'd OpenJML groups, PTY
    sessions and background jobs of the agent included)."""
    needle = f"{MARKER_VAR}={marker}".encode()
    pids = []
    for d in os.listdir("/proc"):
        if not d.isdigit() or int(d) == os.getpid():
            continue
        try:
            with open(f"/proc/{d}/environ", "rb") as fh:
                if needle in fh.read().split(b"\0"):
                    pids.append(int(d))
        except OSError:
            continue
    return pids


def kill_session(pid: int, marker: str, grace: float = 10.0) -> int:
    """SIGTERM the session's process group (the tools log a killed call and kill their OpenJML groups),
    wait up to `grace` s, then SIGKILL the group and every process carrying the marker. Returns the
    number of marked processes that had to be SIGKILLed."""
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    for p in _marked_pids(marker):
        try:
            os.kill(p, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.time() + grace
    while time.time() < deadline and _marked_pids(marker):
        time.sleep(0.2)
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    left = _marked_pids(marker)
    for p in left:
        try:
            os.kill(p, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    return len(left)


def _stop_handler(signum, frame):
    if STOP.is_set():                     # second signal: kill hard and leave at once
        with _RUNNING_LOCK:
            items = list(_RUNNING.items())
        for pid, marker in items:
            kill_session(pid, marker, grace=0)
        os._exit(128 + signum)
    STOP.set()
    print(f"signal {signum}: stopping all sessions", file=sys.stderr, flush=True)
    with _RUNNING_LOCK:
        items = list(_RUNNING.items())
    for pid, _marker in items:            # the workers finish the kill and write run_meta (interrupted)
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass


def install_stop_handlers() -> None:
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _stop_handler)


# ---------------------------------------------------------------------------
# one session
# ---------------------------------------------------------------------------

def find_task_dirs() -> list[str]:
    return [os.path.join(ROOT, n) for n in sorted(os.listdir(ROOT))
            if os.path.isdir(os.path.join(ROOT, n)) and os.path.exists(os.path.join(ROOT, n, "AGENTS.md"))]


def build_command(template: str, task_dir: str) -> tuple[list[str], list[str]]:
    """Return (argv to execute, argv for the log with the prompt text elided)."""
    agents_md_path = os.path.join(task_dir, "AGENTS.md")
    with open(agents_md_path) as fh:
        agents_md = fh.read()
    repl = {"{agents_md_path}": agents_md_path, "{task_dir}": task_dir,
            "{task_id}": os.path.basename(task_dir)}
    argv, shown = [], []
    for tok in shlex.split(template):
        for k, v in repl.items():
            tok = tok.replace(k, v)
        argv.append(tok.replace("{agents_md}", agents_md))
        shown.append(tok.replace("{agents_md}", "<AGENTS.md>"))
    return argv, shown


def _tool_hashes(task_dir: str) -> dict:
    return {n: _sha256_file(os.path.join(task_dir, n)) for n in TOOL_FILES
            if os.path.exists(os.path.join(task_dir, n))}


def run_one(template: str, task_dir: str, timeout: int | None, only_missing: bool) -> tuple[str, int, str]:
    task_id = os.path.basename(task_dir)
    harness = os.path.join(task_dir, "harness")
    if STOP.is_set():
        return task_id, 1, "stopped"
    lock = TaskLock(task_dir)
    if not lock.acquire():
        return task_id, 1, f"locked ({lock.holder() or 'held'})"
    try:
        if not needs_run(task_dir, only_missing):          # re-check under the lock
            return task_id, 0, "done"
        attempt, archived = fresh_state(task_dir)

        argv, shown = build_command(template, task_dir)
        argv, added = enforce_structured_output(argv)
        shown, _ = enforce_structured_output(shown)
        marker = f"{task_id}:{uuid.uuid4().hex}"
        env = dict(os.environ)
        env[MARKER_VAR] = marker
        env.pop("AGENT_MAX_ATTEMPTS", None)                # no longer read; never passed on

        cfg = _load_json(os.path.join(harness, "config.json")) or {}
        meta = {
            "task_id": task_id,
            "attempt": attempt,
            "archived_previous": archived,
            "agent": _agent_kind(argv),
            "command": shown,
            "structured_output_flags_added": added,
            "cli_version": _cli_version(argv[0]) if argv else None,
            "model": _model_of(argv),
            "host": socket.gethostname(),
            "runner_pid": os.getpid(),
            "python": sys.version.split()[0],
            "env": {k: os.environ.get(k) for k in ("OPENJML", "OPENJML_JVM", "SPEC_HARNESS_JVM_SLOTS",
                                                   "CODEX_HOME", "VACT_PYTHON", "PATH")},
            "openjml": shutil.which("openjml"),
            "session_marker": marker,
            "agents_md_sha256": _sha256_file(os.path.join(task_dir, "AGENTS.md")),
            "tool_sha256": _tool_hashes(task_dir),
            "max_attempts": cfg.get("max_attempts"),
            "no_harness": cfg.get("no_harness"),
            "timeout_s": timeout,
            "ts_start": _now(),
        }

        log_path = os.path.join(harness, "agent_run.log")
        err_path = os.path.join(harness, "agent_stderr.log")
        started = time.time()
        rc, timed_out, interrupted, killed_left = None, False, False, 0
        try:
            with open(log_path, "w") as out, open(err_path, "w") as err:
                proc = subprocess.Popen(argv, cwd=task_dir, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                        start_new_session=True, env=env, pass_fds=(lock.fd,))
                with _RUNNING_LOCK:
                    _RUNNING[proc.pid] = marker
                try:
                    while True:
                        try:
                            rc = proc.wait(timeout=1.0)
                            break
                        except subprocess.TimeoutExpired:
                            if STOP.is_set():
                                interrupted = True
                                break
                            if timeout and time.time() - started > timeout:
                                timed_out = True
                                break
                    if STOP.is_set():                 # the stop handler may have ended the session itself
                        interrupted = True
                    if rc is None:
                        killed_left = kill_session(proc.pid, marker)
                        try:
                            proc.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            pass
                        rc = proc.returncode if proc.returncode is not None else -1
                finally:
                    # leftovers of a session that ended by itself (e.g. a backgrounded tool call)
                    killed_left += kill_session(proc.pid, marker, grace=2.0) if _marked_pids(marker) else 0
                    with _RUNNING_LOCK:
                        _RUNNING.pop(proc.pid, None)
        except FileNotFoundError as exc:
            rc = -2
            meta["error"] = f"command not found: {exc}"

        seconds = round(time.time() - started, 3)
        meta.update({
            "ts_end": _now(),
            "seconds": seconds,
            "wall_s": seconds,
            "returncode": rc,
            "timed_out": timed_out,
            "interrupted": interrupted,
            "leftover_processes_killed": killed_left,
            "attempts_used": _read_int(os.path.join(harness, ".attempts")),
            "submission_exists": os.path.exists(os.path.join(task_dir, "submission.json")),
            "last_valid_sha256": _sha256_file(os.path.join(harness, "last_valid_Solution.java")),
            "solution_sha256_end": _sha256_file(os.path.join(task_dir, "Solution.java")),
            "tool_sha256_end": _tool_hashes(task_dir),
        })
        meta["tools_modified"] = sorted(k for k, v in meta["tool_sha256"].items()
                                        if meta["tool_sha256_end"].get(k) != v)
        if meta["agent"] == "codex":
            tid = codex_thread_id(log_path)
            meta["codex_thread_id"] = tid
            meta["codex_rollout_source"] = copy_codex_rollout(task_dir, tid, started)
        tmp = os.path.join(harness, "run_meta.json.tmp")
        with open(tmp, "w") as fh:
            json.dump(meta, fh, indent=2)
        os.replace(tmp, os.path.join(harness, "run_meta.json"))
        status = ("interrupted" if interrupted else "timeout" if timed_out else
                  "ok" if rc == 0 else f"rc={rc}")
        return task_id, (0 if rc == 0 and not timed_out and not interrupted else (rc if rc else -1)), status
    finally:
        lock.release()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Drive an agent over scaffolded task dirs.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--agent", choices=sorted(BUILTIN_AGENTS), help="built-in agent template")
    g.add_argument("--cmd", help="custom command template (tokens: {agents_md} {agents_md_path} "
                                 "{task_dir} {task_id})")
    p.add_argument("--task-ids", help="file with task ids to run (one per line)")
    p.add_argument("--threads", type=int, default=1, help="parallel sessions (default: 1)")
    p.add_argument("--timeout", type=int, default=None, help="per-task timeout seconds")
    p.add_argument("--only-missing", action="store_true",
                   help="run only tasks that never ran (no harness/run_meta.json) or whose last session "
                        "failed for an infrastructure reason (non-zero exit, timeout, interrupted); a "
                        "session that ended normally is never re-run, with or without a submission")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    template = args.cmd or BUILTIN_AGENTS[args.agent]

    task_dirs = find_task_dirs()
    if args.task_ids:
        with open(args.task_ids) as fh:
            wanted = {ln.strip() for ln in fh if ln.strip()}
        task_dirs = [d for d in task_dirs if os.path.basename(d) in wanted]
    task_dirs = [d for d in task_dirs if needs_run(d, args.only_missing)]
    if not task_dirs:
        print("No task dirs to run.", file=sys.stderr)
        return 0 if args.only_missing else 1

    install_stop_handlers()
    print(f"Running '{template}' over {len(task_dirs)} task(s), threads={args.threads}", flush=True)
    results = []
    with ThreadPoolExecutor(max_workers=args.threads) as pool:
        futs = {pool.submit(run_one, template, d, args.timeout, args.only_missing): d for d in task_dirs}
        for fut in as_completed(futs):
            task_id, rc, status = fut.result()
            print(f"  [{status:>11}] {task_id}", flush=True)
            results.append((task_id, rc))
    ok = sum(1 for _, rc in results if rc == 0)
    print(f"Done: {ok}/{len(results)} sessions exited 0. Next: python audit.py; python usage_report.py")
    return 130 if STOP.is_set() else 0


if __name__ == "__main__":
    raise SystemExit(main())
