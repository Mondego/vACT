#!/usr/bin/env python3
"""agent_harness CLI — the RQ4 agent tools as a single command-line dispatcher.

This file is *vendored* into each generated task directory as ``harness/cli.py``
and is only ever run through the task directory's wrapper scripts
(``verify.sh``, ``run_specharness.sh``, ``submit.sh``). It imports its sibling
modules ``harness_tool`` and ``verifier_tool`` (also vendored), so the task
directory is self-contained: the only external requirements are the
``javalang`` pip package and the ``openjml`` binary.

Subcommands
-----------
    verify     run OpenJML ESC on Solution.java; the result also includes the
               error analysis (failure modes + repair hints)
    harness    score the spec on the four spec-harness metrics (first 5 feedback
               pairs of task.json). Absent when config.json says no_harness.
    submit     record the final submission: the code of the last ACCEPTED
               verify/harness call (or the original program if none was accepted)

Rules shared with VeriAct
-------------------------
* Attempt budget: config.json ``max_attempts`` (verify + harness calls combined;
  not overridable from the environment or the command line).
* Integrity: a verify/harness call on code that changes the program (class,
  signature, method body, any other class member) or puts a JML ``assume`` in
  the method body is rejected (``code_modified``) without running the tool; the
  attempt still counts. The rule is ``harness_tool.agent_code_violation``.
* Every accepted call writes ``harness/last_valid_Solution.java``; the final
  submission is that code.

Tool-call log: ``harness/tool_calls.jsonl``, one JSON line per call (see
``_log_tool_call``). Everything the tools themselves print (OpenJML, the
harness banner, warnings) goes to ``harness/harness_stderr.log``; the agent
sees only the JSON result on stdout.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import signal
import sys
import time

# Cap OpenJML's JVM heap (its default max heap is ~1/4 of RAM).
os.environ.setdefault("OPENJML_JVM", "-Xmx8g")

# This file lives at <task_dir>/harness/cli.py
HARNESS_DIR = os.path.dirname(os.path.abspath(__file__))
TASK_DIR = os.path.dirname(HARNESS_DIR)

# Make the vendored sibling modules importable regardless of cwd.
if HARNESS_DIR not in sys.path:
    sys.path.insert(0, HARNESS_DIR)

# Repair hints — copied from veriact/tools.py so the dir stays self-contained.
REPAIR_HINTS: dict[str, str] = {
    "SyntaxError": "Fix JML syntax (missing semicolons, wrong keywords).",
    "PostconditionFailure": "The @ensures clause is logically incorrect.",
    "ExceptionalPostconditionFailure": "The @signals clause is incorrect.",
    "PreconditionFailure": "The @requires clause is too weak or wrong.",
    "LoopInvariantFailure": "The @maintaining clause doesn't hold.",
    "RankingFunctionFailure": "The @decreases expression is wrong.",
    "AssignableFailure": "The @assignable clause is too permissive or missing.",
    "ArrayIndex": "Missing array bounds check in @requires.",
    "NegativeSize": "Array size may be negative; add check in @requires.",
    "NullDeReference": "Missing null check in @requires.",
    "NullUnbox": "Potential null unboxing; add null check in @requires.",
    "DivideByZero": "Missing division-by-zero guard in @requires.",
    "ArithmeticOperationRange": "Integer overflow not guarded in @requires.",
    "ArithmeticCastRange": "Cast may overflow; add range guard in @requires.",
    "BadCast": "Unsafe cast; add type guard in @requires.",
    "BadArrayAssignment": "Incompatible array assignment; check element types.",
    "CalledMethodPrecondition": "Called method precondition not met; strengthen @requires.",
    "LargeShift": "Shift amount out of range; add bounds check in @requires.",
    "AssertFailure": "An @assert statement fails; check the invariant.",
    "UnknownVerificationFailure": "Unknown verification failure; review the full OpenJML log.",
}

# Threshold for a "passed" submission (mirrors veriact HARNESS_PASS_THRESHOLD).
HARNESS_PASS_THRESHOLD = 0.75

# Sibling artifact paths.
SOLUTION = os.path.join(TASK_DIR, "Solution.java")
TASK_JSON = os.path.join(HARNESS_DIR, "task.json")
CONFIG_JSON = os.path.join(HARNESS_DIR, "config.json")
LAST_VERIFY = os.path.join(HARNESS_DIR, "last_verify.json")
LAST_SCORES = os.path.join(HARNESS_DIR, "last_scores.json")
RUN_COUNTER = os.path.join(HARNESS_DIR, ".run_counter")
ATTEMPTS_FILE = os.path.join(HARNESS_DIR, ".attempts")
SUBMISSION = os.path.join(TASK_DIR, "submission.json")
LAST_VALID = os.path.join(HARNESS_DIR, "last_valid_Solution.java")   # code of the last accepted tool call
TOOL_CALLS = os.path.join(HARNESS_DIR, "tool_calls.jsonl")           # one JSON line per tool call
TOOL_STDERR = os.path.join(HARNESS_DIR, "harness_stderr.log")        # everything the tools print

DEFAULT_MAX_ATTEMPTS = 10
DEFAULT_MAX_PAIRS = 5            # feedback pairs scored by the harness tool (as VeriAct)
DEFAULT_MAX_RAW_OUTPUT = 1200    # chars of the OpenJML log returned to the agent

_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="milliseconds")


def _config() -> dict:
    """harness/config.json — the only source of the tool configuration (no env / CLI overrides)."""
    try:
        with open(CONFIG_JSON) as fh:
            cfg = json.load(fh)
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


def _cfg_int(key: str, default: int) -> int:
    try:
        return int(_config().get(key, default))
    except (TypeError, ValueError):
        return default


def _no_harness() -> bool:
    return bool(_config().get("no_harness", False))


def _read_code() -> str:
    with open(SOLUTION) as fh:
        return fh.read()


def _task_dict() -> dict:
    with open(TASK_JSON) as fh:
        return json.load(fh)


def _original_code() -> str:
    try:
        return _task_dict().get("code", "") or ""
    except (OSError, ValueError):
        return ""


def _write_atomic(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)


# ---- output: the agent sees ONLY the JSON result ----------------------------

_REAL_STDOUT_FD: int | None = None     # dup of the original stdout while the tool output is redirected
_LAST_EMIT = None


def _emit(obj) -> None:
    global _LAST_EMIT
    _LAST_EMIT = obj
    data = (json.dumps(obj, indent=2) + "\n").encode()
    if _REAL_STDOUT_FD is not None:
        os.write(_REAL_STDOUT_FD, data)
    else:
        sys.stdout.write(data.decode())
        sys.stdout.flush()


class _ToolOutputToLog:
    """Redirect fd 1 and fd 2 (Python and every child process: OpenJML, the harness banner, warnings,
    tracebacks) to harness/harness_stderr.log; _emit writes the JSON to the saved real stdout."""

    def __enter__(self):
        global _REAL_STDOUT_FD
        sys.stdout.flush()
        sys.stderr.flush()
        self.saved_out, self.saved_err = os.dup(1), os.dup(2)
        _REAL_STDOUT_FD = self.saved_out
        self.log = open(TOOL_STDERR, "a")
        self.log.write(f"\n===== {_now()} {' '.join(sys.argv[1:2])} =====\n")
        self.log.flush()
        os.dup2(self.log.fileno(), 1)
        os.dup2(self.log.fileno(), 2)
        return self

    def __exit__(self, *exc):
        global _REAL_STDOUT_FD
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:  # noqa: BLE001
            pass
        os.dup2(self.saved_out, 1)
        os.dup2(self.saved_err, 2)
        _REAL_STDOUT_FD = None
        os.close(self.saved_out)
        os.close(self.saved_err)
        self.log.close()
        return False


# ---- tool-call log -----------------------------------------------------------
#
# harness/tool_calls.jsonl — one JSON line per verify / harness / submit call:
#   tool           "verify" | "harness" | "submit"
#   accepted       true iff a verify/harness call passed the budget and the integrity check (the tool
#                  ran, or was started and then killed); its code became harness/last_valid_Solution.java.
#                  Always false for submit (submit never changes last_valid_Solution.java).
#   code_sha256    sha256 (utf-8) of the code the call acted on: Solution.java for verify/harness,
#                  the submitted final code for submit
#   time           ISO timestamp of the call start (== ts_start)
#   status         "ok" | "budget_exhausted" | "code_modified" | "error" | "killed"
#   attempt        attempt number charged to this call (null if none was charged)
#   ... plus ts_end, duration_s, exit_code, error, output (the JSON printed), attempts, max_attempts,
#   code (full text), last_valid_sha256 (sha256 of last_valid_Solution.java after the call),
#   argv, openjml (resolved binary path).

_REC: dict = {}


def _log_tool_call(record: dict) -> None:
    """Append one record to the tool-call log; logging must never break the tool."""
    try:
        with open(TOOL_CALLS, "a") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    except Exception:  # noqa: BLE001
        pass


def _file_sha(path: str) -> str | None:
    try:
        with open(path) as fh:
            return _sha256(fh.read())
    except OSError:
        return None


def _finish_record(**extra) -> None:
    if _REC.get("_written"):
        return
    _REC["_written"] = True
    rec = {k: v for k, v in _REC.items() if not k.startswith("_")}
    rec.update({
        "ts_end": _now(),
        "duration_s": round(time.time() - _REC.get("_t0", time.time()), 3),
        "output": _LAST_EMIT,
        "last_valid_sha256": _file_sha(LAST_VALID),
        **_budget_info(),
        **extra,
    })
    _log_tool_call(rec)


def _on_stop() -> None:
    """Called by harness_tool's stop handler (after the OpenJML groups are killed): log the killed call."""
    if _REC.get("status") in (None, "ok"):
        _REC["status"] = "killed"
    _finish_record(exit_code=None, error="killed by signal")


# ---- attempt budget (shared across verify + harness) ----------------------

def _max_attempts() -> int:
    """Per-task attempt cap from harness/config.json (<= 0 means unlimited)."""
    return _cfg_int("max_attempts", DEFAULT_MAX_ATTEMPTS)


def _read_attempts() -> int:
    if os.path.exists(ATTEMPTS_FILE):
        try:
            with open(ATTEMPTS_FILE) as fh:
                return int(fh.read().strip() or "0")
        except (OSError, ValueError):
            return 0
    return 0


def _check_budget() -> dict | None:
    """If the budget is exhausted return an error dict; otherwise consume one
    attempt and return None."""
    mx = _max_attempts()
    used = _read_attempts()
    if mx > 0 and used >= mx:
        return {
            "error": f"attempt budget exhausted ({used}/{mx})",
            "action": "call ./submit.sh with your best spec",
            "attempts": used,
            "max_attempts": mx,
        }
    _write_atomic(ATTEMPTS_FILE, str(used + 1))
    _REC["attempt"] = used + 1
    return None


def _budget_info() -> dict:
    mx = _max_attempts()
    return {"attempts": _read_attempts(), "max_attempts": mx if mx > 0 else None}


def _code_modified(code: str) -> dict | None:
    """The VeriAct `code_modified` error if the code changes the program or puts a JML `assume` in the
    method body (the tool is then not run; the attempt still counts, as a VeriAct step does). Otherwise
    the call is accepted: the code becomes harness/last_valid_Solution.java and None is returned."""
    from harness_tool import agent_code_violation

    violation = agent_code_violation(_original_code(), code)
    if violation:
        _REC["status"] = "code_modified"
        return {
            "error": "code_modified",
            "detail": violation,
            "instruction": ("Do not change the program. Keep the class name, the method "
                            "signature, and every statement of the method body exactly as in "
                            "the original; only add JML annotations (requires/ensures/"
                            "loop_invariant/assignable/decreases). The tool was not run."),
        }
    # accept: commit last_valid + the log fields together, with the stop signals blocked
    old = signal.pthread_sigmask(signal.SIG_BLOCK, _SIGNALS)
    try:
        _write_atomic(LAST_VALID, code)
        _REC["accepted"] = True
        _REC["status"] = "ok"
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, old)
    return None


def _max_raw_output() -> int:
    return _cfg_int("max_raw_output", DEFAULT_MAX_RAW_OUTPUT)


def _tail(s: str, n: int) -> str:
    s = s or ""
    if n <= 0 or len(s) <= n:
        return s
    return "...[truncated]...\n" + s[-n:]


def _analyze(classified_errors: list[dict]) -> dict:
    """Turn OpenJML classified errors into failure modes + repair hints."""
    failure_modes: list[str] = []
    repair_hints: list[str] = []
    seen: set[str] = set()
    for err in classified_errors:
        ftype = err.get("type", "UnknownVerificationFailure")
        failure_modes.append(ftype)
        hint = REPAIR_HINTS.get(ftype)
        if hint and ftype not in seen:
            repair_hints.append(f"{ftype}: {hint}")
            seen.add(ftype)
    if failure_modes:
        counts: dict[str, int] = {}
        for fm in failure_modes:
            counts[fm] = counts.get(fm, 0) + 1
        summary = ", ".join(f"{t} (x{c})" if c > 1 else t for t, c in counts.items())
    else:
        summary = "No failures detected."
    return {
        "failure_modes": failure_modes,
        "repair_hints": repair_hints,
        "summary": summary,
    }


def _install_stop_handlers() -> None:
    """A killed tool call (e.g. an agent-side command timeout) kills its OpenJML process groups (the
    harness's and verifier_tool's, both started with setsid) and logs the call, then exits."""
    import verifier_tool
    from harness_tool import install_stop_handlers

    install_stop_handlers(verifier_tool.kill_live_groups, _on_stop)


def _openjml_binary() -> str:
    import shutil
    return os.environ.get("OPENJML") or shutil.which("openjml") or "openjml"


def _use_openjml(path: str) -> None:
    """The vendored tools invoke the literal binary name ``openjml``; if $OPENJML names a concrete path,
    put its directory first on PATH so that name resolves to it."""
    if path and path != "openjml" and os.sep in path:
        d = os.path.dirname(os.path.abspath(path))
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")


# ---------------------------------------------------------------------------
# subcommands
# ---------------------------------------------------------------------------

def _begin_tool_call() -> str | None:
    """Budget + integrity for verify/harness. Returns the code to run on, or None (already emitted)."""
    code = _read_code()
    _REC["code_sha256"] = _sha256(code)
    _REC["code"] = code
    if os.path.exists(SUBMISSION):
        # as VeriAct's task_complete, submitting ends the task: no tool runs afterwards (not counted)
        _REC["status"] = "after_submit"
        _emit({"error": "already submitted", "action": "the task is finished; stop here", **_budget_info()})
        return None
    exhausted = _check_budget()
    if exhausted is not None:
        _REC["status"] = "budget_exhausted"
        _emit(exhausted)
        return None
    blocked = _code_modified(code)
    if blocked is not None:
        _emit({**blocked, **_budget_info()})
        return None
    return code


def cmd_verify(args: argparse.Namespace) -> int:
    _install_stop_handlers()
    code = _begin_tool_call()
    if code is None:
        return 2
    from verifier_tool import verify_with_openjml

    openjml = _openjml_binary()
    _REC["openjml"] = openjml
    _use_openjml(openjml)
    try:
        classname = _task_dict().get("class_name") or "Solution"
    except (OSError, ValueError):
        classname = "Solution"

    result = verify_with_openjml(
        code,
        classname=classname,
        output_dir=os.path.join(HARNESS_DIR, "tmp"),
    )
    analysis = _analyze(result.classified_errors)
    out = {
        "verified": result.success,
        "return_code": result.return_code,
        # Analyzed view only (not the full classified-error list) to keep the
        # agent's context small: failure modes + targeted repair hints + summary.
        "failure_modes": analysis["failure_modes"],
        "repair_hints": analysis["repair_hints"],
        "summary": analysis["summary"],
        # Truncated tail of the OpenJML log for context (config.json max_raw_output).
        "raw_output": _tail(result.error_log, _max_raw_output()),
        **_budget_info(),
    }
    with open(LAST_VERIFY, "w") as fh:
        json.dump({**out, "code_sha256": _REC["code_sha256"]}, fh, indent=2)
    _emit(out)
    return 0


def _next_run_id() -> str:
    n = 0
    if os.path.exists(RUN_COUNTER):
        try:
            with open(RUN_COUNTER) as fh:
                n = int(fh.read().strip() or "0")
        except (ValueError, OSError):
            n = 0
    n += 1
    with open(RUN_COUNTER, "w") as fh:
        fh.write(str(n))
    return f"run_{n}"


def cmd_harness(args: argparse.Namespace) -> int:
    _install_stop_handlers()
    from harness_tool import Task, evaluate_problem

    try:
        task = Task.from_dict(_task_dict())
    except (OSError, KeyError, ValueError) as exc:
        _REC["status"] = "error"
        _emit({"error": f"cannot load task.json: {exc}"})
        return 1
    code = _begin_tool_call()
    if code is None:
        return 2

    openjml = _openjml_binary()
    _REC["openjml"] = openjml
    _use_openjml(openjml)
    run_id = _next_run_id()
    scores = evaluate_problem(
        task,
        llm_code=code,
        openjml_path=openjml,
        output_dir=HARNESS_DIR,
        run_id=run_id,
        max_pairs=args.max_pairs,
    )
    if not scores:
        _emit({"error": "No test pairs could be parsed.", "task_id": task.task_id, **_budget_info()})
        return 1

    out = {
        task.task_id: {
            "post_correctness": scores.get("post_correctness", 0.0),
            "post_completeness": scores.get("post_completeness", 0.0),
            "pre_correctness": scores.get("pre_correctness", 0.0),
            "pre_completeness": scores.get("pre_completeness", 0.0),
        }
    }
    if scores.get("note"):
        out[task.task_id]["note"] = scores["note"]
    with open(LAST_SCORES, "w") as fh:
        json.dump({**out, "_code_sha256": _REC["code_sha256"]}, fh, indent=2)
    _emit({**out, "_budget": _budget_info()})
    return 0


def cmd_submit(args: argparse.Namespace) -> int:
    summary = " ".join(args.summary).strip()
    try:
        task_id = _task_dict().get("task_id") or os.path.basename(TASK_DIR)
    except (OSError, ValueError):
        task_id = os.path.basename(TASK_DIR)

    solution_at_submit = ""
    if os.path.exists(SOLUTION):
        with open(SOLUTION) as fh:
            solution_at_submit = fh.read()

    # The final submission is the code of the last ACCEPTED verify/harness call (the same rule for every
    # agent), or the original program if no call was ever accepted; it is written back to Solution.java.
    if os.path.exists(LAST_VALID):
        with open(LAST_VALID) as fh:
            final_code, source = fh.read(), "last_accepted"
    else:
        final_code, source = _original_code(), "original"
    if final_code != solution_at_submit:
        _write_atomic(SOLUTION, final_code)
    final_sha = _sha256(final_code)
    _REC["code_sha256"] = final_sha
    _REC["code"] = final_code
    _REC["status"] = "ok"

    scores, passed, scores_sha = None, False, None
    if os.path.exists(LAST_SCORES):
        try:
            with open(LAST_SCORES) as fh:
                last = json.load(fh)
        except (OSError, ValueError):
            last = {}
        scores_sha = last.pop("_code_sha256", None)
        metrics = last.get(task_id) or next(iter(last.values()), {})
        if isinstance(metrics, dict):
            scores = metrics
            passed = (
                (metrics.get("post_correctness") or 0.0) >= HARNESS_PASS_THRESHOLD      # None = not evaluable
                and (metrics.get("post_completeness") or 0.0) >= HARNESS_PASS_THRESHOLD
            )

    submission = {
        "task_id": task_id,
        "summary": summary,
        "final_code": final_code,
        "final_code_sha256": final_sha,
        "source": source,
        "solution_java_at_submit_sha256": _sha256(solution_at_submit),
        "solution_java_restored": final_code != solution_at_submit,
        "scores": scores,
        "scores_code_sha256": scores_sha,
        "scores_match_final_code": scores_sha == final_sha if scores_sha else None,
        "passed": passed,
        "threshold": HARNESS_PASS_THRESHOLD,
        "attempts": _read_attempts(),
        "max_attempts": _max_attempts() or None,
        "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    _write_atomic(SUBMISSION, json.dumps(submission, indent=2))
    out = {
        "submitted": True,
        "task_id": task_id,
        "source": source,
        "passed": passed,
        "scores": scores,
    }
    if final_code != solution_at_submit:
        out["note"] = ("Solution.java differed from the code of your last accepted tool call; the "
                       + ("last accepted code" if source == "last_accepted" else "original program")
                       + " was submitted and written back to Solution.java.")
    _emit(out)
    return 0


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------

def build_parser(no_harness: bool | None = None) -> argparse.ArgumentParser:
    if no_harness is None:
        no_harness = _no_harness()
    p = argparse.ArgumentParser(
        prog="harness/cli.py",
        description="RQ4 agent tools: verify | " + ("" if no_harness else "harness | ") + "submit.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    pv = sub.add_parser(
        "verify",
        help="run OpenJML ESC on Solution.java (output includes failure modes + repair hints)",
    )
    pv.set_defaults(func=cmd_verify)

    if not no_harness:
        ph = sub.add_parser("harness", help="score the spec on the 4 spec-harness metrics")
        ph.add_argument("--max-pairs", type=int, default=DEFAULT_MAX_PAIRS, dest="max_pairs",
                        help=f"max feedback pairs to score (default: {DEFAULT_MAX_PAIRS})")
        ph.set_defaults(func=cmd_harness)

    ps = sub.add_parser("submit", help="record the final submission")
    # REMAINDER: a summary that starts with '-' (or has several words) is taken as-is
    ps.add_argument("summary", nargs=argparse.REMAINDER, help="brief summary of the final spec and scores")
    ps.set_defaults(func=cmd_submit)

    return p


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    # submit: everything after the subcommand is the summary (even '-x', '--', '--help')
    if argv and argv[0] == "submit":
        rest = argv[1:]
        while rest[:1] == ["--"]:          # submit.sh adds one; an agent may add another
            rest = rest[1:]
        args = argparse.Namespace(command="submit", summary=rest, func=cmd_submit)
    else:
        args = build_parser().parse_args(argv)   # usage errors stay visible to the agent
    _REC.clear()
    _REC.update({
        "tool": args.command,
        "accepted": False,
        "code_sha256": None,
        "time": _now(),
        "ts_start": None,
        "status": None,
        "attempt": None,
        "argv": argv,
        "_t0": time.time(),
    })
    _REC["ts_start"] = _REC["time"]
    rc, err = None, None
    with _ToolOutputToLog():
        try:
            rc = args.func(args)
        except Exception as exc:  # noqa: BLE001  (logged; the agent gets a JSON error)
            import traceback
            traceback.print_exc()
            err = f"{type(exc).__name__}: {exc}"
            _REC["status"] = "error"
            _emit({"error": f"internal tool error: {err}", **_budget_info()})
            rc = 1
        finally:
            _finish_record(exit_code=rc, error=err)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
