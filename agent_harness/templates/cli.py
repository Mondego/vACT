#!/usr/bin/env python3
"""agent_harness CLI — VeriAct's four tools as a single command-line dispatcher.

This file is *vendored* into each generated task directory as ``harness/cli.py``.
It imports its sibling modules ``harness_tool`` and ``verifier_tool`` (also
vendored) so the task directory is fully self-contained: the only external
requirements are the ``javalang`` pip package and the ``openjml`` binary.

Subcommands
-----------
    verify     run OpenJML ESC on Solution.java; the result also includes the
               error analysis (failure modes + repair hints)
    harness    score the spec on the four spec-harness metrics
    submit     record the final submission (the comparison artifact)

Paths are resolved relative to this file: ``harness/`` is this file's directory
and the task directory is its parent (which holds ``Solution.java``).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Cap OpenJML's JVM heap (its default max heap is ~1/4 of RAM). Overridable.
os.environ.setdefault("OPENJML_JVM", "-Xmx8g")

# This file lives at <task_dir>/harness/cli.py
HARNESS_DIR = os.path.dirname(os.path.abspath(__file__))
TASK_DIR = os.path.dirname(HARNESS_DIR)

# Make the vendored sibling modules importable regardless of cwd.
if HARNESS_DIR not in sys.path:
    sys.path.insert(0, HARNESS_DIR)

from harness_tool import Task, evaluate_problem  # noqa: E402
from verifier_tool import (  # noqa: E402
    VerificationResult,
    verify_with_openjml,
)

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
TASK_JSON = os.path.join(HARNESS_DIR, "task.json")
CONFIG_JSON = os.path.join(HARNESS_DIR, "config.json")
LAST_VERIFY = os.path.join(HARNESS_DIR, "last_verify.json")
LAST_SCORES = os.path.join(HARNESS_DIR, "last_scores.json")
RUN_COUNTER = os.path.join(HARNESS_DIR, ".run_counter")
ATTEMPTS_FILE = os.path.join(HARNESS_DIR, ".attempts")
SUBMISSION = os.path.join(TASK_DIR, "submission.json")

DEFAULT_MAX_ATTEMPTS = 15


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _resolve_openjml(openjml: str) -> None:
    """The vendored tools invoke the literal binary name ``openjml``.

    If a concrete path is supplied (via --openjml or $OPENJML), prepend its
    directory to PATH so the literal ``openjml`` resolves to it.
    """
    if openjml and openjml != "openjml" and os.sep in openjml:
        d = os.path.dirname(os.path.abspath(openjml))
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")


def _default_openjml(arg: str | None) -> str:
    return arg or os.environ.get("OPENJML", "openjml")


def _read_code(code_path: str | None) -> str:
    path = code_path or os.path.join(TASK_DIR, "Solution.java")
    with open(path) as fh:
        return fh.read()


def _load_task() -> Task:
    with open(TASK_JSON) as fh:
        return Task.from_dict(json.load(fh))


def _emit(obj) -> None:
    global _LAST_EMIT
    _LAST_EMIT = obj
    print(json.dumps(obj, indent=2))


# ---- attempt budget (shared across verify + harness) ----------------------

def _max_attempts() -> int:
    """Per-task attempt cap: $AGENT_MAX_ATTEMPTS, else config.json, else default.

    A value <= 0 means unlimited.
    """
    env = os.environ.get("AGENT_MAX_ATTEMPTS")
    if env is not None:
        try:
            return int(env)
        except ValueError:
            pass
    if os.path.exists(CONFIG_JSON):
        try:
            with open(CONFIG_JSON) as fh:
                return int(json.load(fh).get("max_attempts", DEFAULT_MAX_ATTEMPTS))
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    return DEFAULT_MAX_ATTEMPTS


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
    with open(ATTEMPTS_FILE, "w") as fh:
        fh.write(str(used + 1))
    return None


def _budget_info() -> dict:
    mx = _max_attempts()
    return {"attempts": _read_attempts(), "max_attempts": mx if mx > 0 else None}


# Cap the OpenJML log returned to the agent (chars). Override with $MAX_RAW_OUTPUT.
MAX_RAW_OUTPUT = int(os.environ.get("MAX_RAW_OUTPUT", "1200"))


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


# ---------------------------------------------------------------------------
# subcommands
# ---------------------------------------------------------------------------

def cmd_verify(args: argparse.Namespace) -> int:
    if not getattr(args, "no_budget", False):
        exhausted = _check_budget()
        if exhausted is not None:
            _emit(exhausted)
            return 2
    _resolve_openjml(_default_openjml(args.openjml))
    code = _read_code(args.code)
    try:
        task = _load_task()
        classname = task.class_name or "Solution"
    except (OSError, KeyError, json.JSONDecodeError):
        classname = "Solution"

    result: VerificationResult = verify_with_openjml(
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
        # Truncated tail of the OpenJML log for context (override $MAX_RAW_OUTPUT).
        "raw_output": _tail(result.error_log, MAX_RAW_OUTPUT),
        **_budget_info(),
    }
    with open(LAST_VERIFY, "w") as fh:
        json.dump(out, fh, indent=2)
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
    if not args.no_budget:
        exhausted = _check_budget()
        if exhausted is not None:
            _emit(exhausted)
            return 2
    openjml = _default_openjml(args.openjml)
    _resolve_openjml(openjml)
    code = _read_code(args.code)
    try:
        task = _load_task()
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        _emit({"error": f"cannot load task.json: {exc}"})
        return 1

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
        _emit({"error": "No test pairs could be parsed.", "task_id": task.task_id})
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
        json.dump(out, fh, indent=2)
    if not args.no_budget:
        _emit({**out, "_budget": _budget_info()})
    else:
        _emit(out)
    return 0


def cmd_submit(args: argparse.Namespace) -> int:
    import datetime

    try:
        task = _load_task()
        task_id = task.task_id
    except (OSError, KeyError, json.JSONDecodeError):
        task_id = os.path.basename(TASK_DIR)

    final_code = ""
    sol_path = os.path.join(TASK_DIR, "Solution.java")
    if os.path.exists(sol_path):
        with open(sol_path) as fh:
            final_code = fh.read()

    scores = None
    passed = False
    if os.path.exists(LAST_SCORES):
        with open(LAST_SCORES) as fh:
            last = json.load(fh)
        metrics = last.get(task_id) or next(iter(last.values()), {})
        if isinstance(metrics, dict):
            scores = metrics
            passed = (
                (metrics.get("post_correctness") or 0.0) >= HARNESS_PASS_THRESHOLD      # None = not evaluable
                and (metrics.get("post_completeness") or 0.0) >= HARNESS_PASS_THRESHOLD
            )

    submission = {
        "task_id": task_id,
        "summary": args.summary,
        "final_code": final_code,
        "scores": scores,
        "passed": passed,
        "threshold": HARNESS_PASS_THRESHOLD,
        "attempts": _read_attempts(),
        "max_attempts": _max_attempts() or None,
        "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    with open(SUBMISSION, "w") as fh:
        json.dump(submission, fh, indent=2)
    _emit(
        {
            "submitted": True,
            "task_id": task_id,
            "passed": passed,
            "scores": scores,
            "path": SUBMISSION,
        }
    )
    return 0



# ---------------------------------------------------------------------------
# tool-call log: one JSON line per invocation, independent of the agent
# ---------------------------------------------------------------------------

_LAST_EMIT = None


def _log_tool_call(log_path, record):
    """Append one record to the tool-call log; logging must never break the tool."""
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except Exception:
        pass


def _code_snapshot(path):
    import hashlib
    try:
        with open(path) as fh:
            code = fh.read()
        return {"code_path": path, "code_sha256": hashlib.sha256(code.encode()).hexdigest(), "code": code}
    except Exception:
        return {"code_path": path}


TOOL_CALLS = os.path.join(HARNESS_DIR, "tool_calls.jsonl")

# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="harness/cli.py",
        description="VeriAct tools: verify | analyze | harness | submit.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    pv = sub.add_parser(
        "verify",
        help="run OpenJML ESC (output includes failure modes + repair hints)",
    )
    pv.add_argument("--code", help="path to Java source (default: ../Solution.java)")
    pv.add_argument("--openjml", help="OpenJML binary path (default: $OPENJML or 'openjml')")
    pv.add_argument("--no-budget", action="store_true",
                    help="don't count this run against the attempt budget "
                         "(for offline experimenter verification)")
    pv.set_defaults(func=cmd_verify)

    ph = sub.add_parser("harness", help="score the spec on the 4 spec-harness metrics")
    ph.add_argument("--code", help="path to Java source (default: ../Solution.java)")
    ph.add_argument("--openjml", help="OpenJML binary path (default: $OPENJML or 'openjml')")
    ph.add_argument("--max-pairs", type=int, default=10, dest="max_pairs",
                    help="max test pairs to score (default: 5)")
    ph.add_argument("--no-budget", action="store_true",
                    help="don't count this run against the attempt budget "
                         "(for offline experimenter scoring)")
    ph.set_defaults(func=cmd_harness)

    ps = sub.add_parser("submit", help="record the final submission")
    ps.add_argument("summary", help="brief summary of the final spec and scores")
    ps.set_defaults(func=cmd_submit)

    return p


def main(argv: list[str] | None = None) -> int:
    import datetime
    import time
    args = build_parser().parse_args(argv)
    start = time.time()
    rec = {
        "ts_start": datetime.datetime.now().isoformat(timespec="milliseconds"),
        "command": args.command,
        "offline": bool(getattr(args, "no_budget", False)),
        **_code_snapshot(getattr(args, "code", None) or os.path.join(TASK_DIR, "Solution.java")),
    }
    rc, err = None, None
    try:
        rc = args.func(args)
        return rc
    except BaseException as exc:  # noqa: BLE001  (logged, then re-raised)
        err = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        rec.update({
            "ts_end": datetime.datetime.now().isoformat(timespec="milliseconds"),
            "duration_s": round(time.time() - start, 3),
            "exit_code": rc,
            "error": err,
            "output": _LAST_EMIT,
            **_budget_info(),
        })
        _log_tool_call(TOOL_CALLS, rec)


if __name__ == "__main__":
    raise SystemExit(main())
