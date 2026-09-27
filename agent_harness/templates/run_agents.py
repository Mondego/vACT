#!/usr/bin/env python3
"""Parent-level driver — fan out one fresh agent session per task directory.

Run this from the out-root produced by ``agent_harness.scaffold``. For each task
subdir (any immediate child containing ``AGENTS.md``) it launches a headless
coding-agent session with that subdir as the working directory, feeding the dir's
``AGENTS.md`` as the prompt. One isolated session per task.

Everything a session produces is recorded under ``<task>/harness/``:

    agent_run.log       the agent's stdout. Structured JSON events are enforced:
                          claude -> --output-format stream-json --verbose
                                    (every message, tool call, tool result, per-turn
                                     usage, and the final result with tokens/cost/time)
                          codex  -> exec --json
                                    (thread/turn/item events with per-turn token usage)
    agent_stderr.log    the agent's stderr (kept apart so the JSON stays parseable)
    codex_rollout.jsonl Codex only: the full session rollout copied from
                        $CODEX_HOME/sessions (complete conversation, reasoning
                        items, cumulative token counts)
    run_meta.json       command (prompt elided), CLI version, model, host,
                        start/end, wall time, exit code, timeout, hashes of
                        AGENTS.md and the vendored tools, attempts used
    tool_calls.jsonl    written by the tools themselves (harness/cli.py): one line
                        per verify/harness/submit call with the code snapshot,
                        output, duration and budget state
    previous_runs/<ts>/ earlier sessions' logs (never overwritten)

Summarize any out-root with ``python usage_report.py``.

This script is self-contained (stdlib only); it ships inside the out-root.

Examples
--------
    python run_agents.py --agent claude --threads 4 --only-missing
    python run_agents.py --agent codex  --threads 4 --only-missing
    python run_agents.py --agent claude --fresh --task-ids redo.txt   # clean re-run
    python run_agents.py --cmd 'claude -p {agents_md} --dangerously-skip-permissions --model claude-opus-4-8'
"""

from __future__ import annotations

import argparse
import datetime
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
import time
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

# Files that belong to one session; archived before a new session starts.
RUN_LOGS = ["agent_run.log", "agent_stderr.log", "run_meta.json", "codex_rollout.jsonl"]
# Task state reset by --fresh (archived, then restored from task.json).
RUN_STATE = [".attempts", ".run_counter", "last_scores.json", "last_verify.json", "tool_calls.jsonl"]


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
            out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=60)
            _VERSION_CACHE[binary] = (out.stdout or out.stderr).strip() or None
        except Exception:  # noqa: BLE001
            _VERSION_CACHE[binary] = None
    return _VERSION_CACHE[binary]


def archive(task_dir: str, names: list[str], stamp: str) -> list[str]:
    """Move existing run files into harness/previous_runs/<stamp>/. Returns moved names."""
    harness = os.path.join(task_dir, "harness")
    dest = os.path.join(harness, "previous_runs", stamp)
    moved = []
    for name in names:
        src = os.path.join(harness, name)
        if os.path.exists(src):
            os.makedirs(dest, exist_ok=True)
            shutil.move(src, os.path.join(dest, name))
            moved.append(name)
    return moved


def reset_task(task_dir: str, stamp: str) -> None:
    """--fresh: archive state, submission and edited Solution.java; restore the original code."""
    archive(task_dir, RUN_STATE, stamp)
    dest = os.path.join(task_dir, "harness", "previous_runs", stamp)
    for name in ("submission.json", "Solution.java"):
        src = os.path.join(task_dir, name)
        if os.path.exists(src):
            os.makedirs(dest, exist_ok=True)
            shutil.copy2(src, os.path.join(dest, name))
            if name == "submission.json":
                os.remove(src)
    with open(os.path.join(task_dir, "harness", "task.json")) as fh:
        code = json.load(fh).get("code", "")
    with open(os.path.join(task_dir, "Solution.java"), "w") as fh:
        fh.write(code)


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


def run_one(template: str, task_dir: str, timeout: int | None, fresh: bool) -> tuple[str, int, str]:
    task_id = os.path.basename(task_dir)
    harness = os.path.join(task_dir, "harness")
    os.makedirs(harness, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S_%f")
    while os.path.exists(os.path.join(harness, "previous_runs", stamp)):
        stamp += "_"
    if fresh:
        reset_task(task_dir, stamp)
    archive(task_dir, RUN_LOGS, stamp)

    argv, shown = build_command(template, task_dir)
    argv, added = enforce_structured_output(argv)
    shown, _ = enforce_structured_output(shown)

    meta = {
        "task_id": task_id,
        "agent": _agent_kind(argv),
        "command": shown,
        "structured_output_flags_added": added,
        "cli_version": _cli_version(argv[0]) if argv else None,
        "model": _model_of(argv),
        "host": socket.gethostname(),
        "python": sys.version.split()[0],
        "env": {k: os.environ.get(k) for k in ("OPENJML", "OPENJML_JVM", "AGENT_MAX_ATTEMPTS", "CODEX_HOME")},
        "agents_md_sha256": _sha256_file(os.path.join(task_dir, "AGENTS.md")),
        "tool_sha256": {n: _sha256_file(os.path.join(harness, n))
                        for n in ("cli.py", "harness_tool.py", "verifier_tool.py")},
        "max_attempts": json.load(open(os.path.join(harness, "config.json"))).get("max_attempts")
        if os.path.exists(os.path.join(harness, "config.json")) else None,
        "fresh": fresh,
        "ts_start": _now(),
    }

    log_path = os.path.join(harness, "agent_run.log")
    err_path = os.path.join(harness, "agent_stderr.log")
    started = time.time()
    rc, timed_out = None, False
    try:
        with open(log_path, "w") as out, open(err_path, "w") as err:
            proc = subprocess.Popen(argv, cwd=task_dir, stdout=out, stderr=err, start_new_session=True)
            try:
                rc = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.killpg(proc.pid, sig)
                    except ProcessLookupError:
                        break
                    try:
                        proc.wait(timeout=10)
                        break
                    except subprocess.TimeoutExpired:
                        continue
                rc = proc.returncode if proc.returncode is not None else -1
    except FileNotFoundError as exc:
        rc = -2
        meta["error"] = f"command not found: {exc}"

    meta.update({
        "ts_end": _now(),
        "wall_s": round(time.time() - started, 3),
        "returncode": rc,
        "timed_out": timed_out,
        "attempts_used": _read_int(os.path.join(harness, ".attempts")),
        "submission_exists": os.path.exists(os.path.join(task_dir, "submission.json")),
        "solution_sha256_end": _sha256_file(os.path.join(task_dir, "Solution.java")),
    })
    if meta["agent"] == "codex":
        tid = codex_thread_id(log_path)
        meta["codex_thread_id"] = tid
        meta["codex_rollout_source"] = copy_codex_rollout(task_dir, tid, started)
    with open(os.path.join(harness, "run_meta.json"), "w") as fh:
        json.dump(meta, fh, indent=2)
    status = "timeout" if timed_out else ("ok" if rc == 0 else f"rc={rc}")
    return task_id, (0 if rc == 0 and not timed_out else (rc if rc else -1)), status


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Drive an agent over scaffolded task dirs.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--agent", choices=sorted(BUILTIN_AGENTS), help="built-in agent template")
    g.add_argument("--cmd", help="custom command template (see token list in --help)")
    p.add_argument("--task-ids", help="file with task ids to run (one per line)")
    p.add_argument("--threads", type=int, default=1, help="parallel sessions (default: 1)")
    p.add_argument("--timeout", type=int, default=None, help="per-task timeout seconds")
    p.add_argument("--only-missing", action="store_true", help="skip dirs that already have submission.json")
    p.add_argument("--fresh", action="store_true",
                   help="archive the previous run (state, submission, edited Solution.java) under "
                        "harness/previous_runs/ and restore the original code before running")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    template = args.cmd or BUILTIN_AGENTS[args.agent]

    task_dirs = find_task_dirs()
    if args.task_ids:
        with open(args.task_ids) as fh:
            wanted = {ln.strip() for ln in fh if ln.strip()}
        task_dirs = [d for d in task_dirs if os.path.basename(d) in wanted]
    if args.only_missing and not args.fresh:
        task_dirs = [d for d in task_dirs if not os.path.exists(os.path.join(d, "submission.json"))]
    if not task_dirs:
        print("No task dirs to run.", file=sys.stderr)
        return 1

    print(f"Running '{template}' over {len(task_dirs)} task(s), threads={args.threads}")
    results = []
    with ThreadPoolExecutor(max_workers=args.threads) as pool:
        futs = {pool.submit(run_one, template, d, args.timeout, args.fresh): d for d in task_dirs}
        for fut in as_completed(futs):
            task_id, rc, status = fut.result()
            print(f"  [{status:>7}] {task_id}")
            results.append((task_id, rc))
    ok = sum(1 for _, rc in results if rc == 0)
    print(f"Done: {ok}/{len(results)} sessions exited 0. Next: python collect.py; python usage_report.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
