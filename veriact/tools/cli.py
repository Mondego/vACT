#!/usr/bin/env python3
"""veriact tool CLI — the three agent tools as subcommands.

The CLI-ReAct agent ([cli_agent.py]) drives these as subprocesses and observes
their JSON stdout:

    verify   run OpenJML ESC on the given code; output includes the error
             analysis (failure modes + repair hints) — analyze is merged in
    harness  score the spec on the four spec-harness metrics
    submit   record the final submission (task_complete)

No attempt budget here: the agent's loop is bounded by --max-steps instead.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Import the scorer/verifier whether run as `-m veriact.tools.cli` or by file path.
try:
    from veriact.tools.harness_tool import Task, evaluate_problem
    from veriact.tools.verifier_tool import VerificationResult, verify_with_openjml
except ImportError:  # invoked directly by path (the agent does this)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from harness_tool import Task, evaluate_problem  # type: ignore
    from verifier_tool import VerificationResult, verify_with_openjml  # type: ignore

# Repair hints — same mapping VeriAct's analyze tool uses.
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


def _resolve_openjml(openjml: str) -> None:
    """verify_with_openjml/evaluate_problem invoke the literal binary 'openjml';
    if a path is given, prepend its dir to PATH so that resolves to it."""
    if openjml and openjml != "openjml" and os.sep in openjml:
        d = os.path.dirname(os.path.abspath(openjml))
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")


def _default_openjml(arg: str | None) -> str:
    return arg or os.environ.get("OPENJML", "openjml")


def _read(path: str) -> str:
    with open(path) as fh:
        return fh.read()


def _emit(obj) -> None:
    global _LAST_EMIT
    _LAST_EMIT = obj
    print(json.dumps(obj, indent=2))


# Cap the OpenJML log returned to the agent (chars). Override with $MAX_RAW_OUTPUT.
MAX_RAW_OUTPUT = int(os.environ.get("MAX_RAW_OUTPUT", "1200"))


def _tail(s: str, n: int) -> str:
    s = s or ""
    if n <= 0 or len(s) <= n:
        return s
    return "...[truncated]...\n" + s[-n:]


def _analyze(classified_errors: list[dict]) -> dict:
    failure_modes, repair_hints, seen = [], [], set()
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
    return {"failure_modes": failure_modes, "repair_hints": repair_hints, "summary": summary}


def cmd_verify(args: argparse.Namespace) -> int:
    _resolve_openjml(_default_openjml(args.openjml))
    code = _read(args.code)
    result: VerificationResult = verify_with_openjml(
        code, classname="Solution", output_dir=args.output_dir or "veriact_tmp"
    )
    analysis = _analyze(result.classified_errors)
    _emit(
        {
            "verified": result.success,
            "return_code": result.return_code,
            # Analyzed view only (not the full classified-error list) to keep the
            # agent's context small: failure modes + targeted repair hints + summary.
            "failure_modes": analysis["failure_modes"],
            "repair_hints": analysis["repair_hints"],
            "summary": analysis["summary"],
            # Truncated tail of the OpenJML log for context (override $MAX_RAW_OUTPUT).
            "raw_output": _tail(result.error_log, MAX_RAW_OUTPUT),
        }
    )
    return 0


def cmd_harness(args: argparse.Namespace) -> int:
    openjml = _default_openjml(args.openjml)
    _resolve_openjml(openjml)
    code = _read(args.code)
    with open(args.task_json) as fh:
        task = Task.from_dict(json.load(fh))
    scores = evaluate_problem(
        task,
        llm_code=code,
        openjml_path=openjml,
        output_dir=args.output_dir or "veriact_tmp",
        max_pairs=args.max_pairs,
        run_id=args.run_id or "",
    )
    if not scores:
        _emit({"error": "No test pairs could be parsed.", "task_id": task.task_id})
        return 1
    _emit(
        {
            task.task_id: {
                "post_correctness": scores.get("post_correctness", 0.0),
                "post_completeness": scores.get("post_completeness", 0.0),
                "pre_correctness": scores.get("pre_correctness", 0.0),
                "pre_completeness": scores.get("pre_completeness", 0.0),
                **({"note": scores["note"]} if scores.get("note") else {}),
            }
        }
    )
    return 0


def cmd_submit(args: argparse.Namespace) -> int:
    import datetime

    code = _read(args.code) if args.code and os.path.exists(args.code) else ""
    submission = {
        "summary": args.summary,
        "final_code": code,
        "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(submission, fh, indent=2)
    _emit({"submitted": True, "summary": args.summary, "path": args.out})
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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="veriact.tools.cli")
    sub = p.add_subparsers(dest="command", required=True)

    pv = sub.add_parser("verify", help="OpenJML ESC + error analysis")
    pv.add_argument("--code", required=True)
    pv.add_argument("--openjml")
    pv.add_argument("--output-dir", dest="output_dir")
    pv.set_defaults(func=cmd_verify)

    ph = sub.add_parser("harness", help="spec-harness 4-metric scoring")
    ph.add_argument("--code", required=True)
    ph.add_argument("--task-json", dest="task_json", required=True)
    ph.add_argument("--openjml")
    ph.add_argument("--output-dir", dest="output_dir")
    ph.add_argument("--max-pairs", type=int, default=10, dest="max_pairs")
    ph.add_argument("--run-id", dest="run_id")
    ph.set_defaults(func=cmd_harness)

    ps = sub.add_parser("submit", help="record the final submission")
    ps.add_argument("summary")
    ps.add_argument("--code")
    ps.add_argument("--out")
    ps.set_defaults(func=cmd_submit)
    return p


def main(argv: list[str] | None = None) -> int:
    import datetime
    import time
    args = build_parser().parse_args(argv)
    # Log next to the workspace: <workspace>/harness/tool_calls.jsonl
    anchor = getattr(args, "code", None) or getattr(args, "out", None) or os.getcwd()
    workspace = os.path.dirname(os.path.abspath(anchor))
    start = time.time()
    rec = {
        "ts_start": datetime.datetime.now().isoformat(timespec="milliseconds"),
        "command": args.command,
        "run_id": getattr(args, "run_id", None),
        **(_code_snapshot(args.code) if getattr(args, "code", None) else {}),
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
        })
        _log_tool_call(os.path.join(workspace, "harness", "tool_calls.jsonl"), rec)


if __name__ == "__main__":
    raise SystemExit(main())
