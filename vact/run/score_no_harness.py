#!/usr/bin/env python3
"""Score a no-harness run's outputs with the spec-harness (offline).

The ``--no-harness`` arm never calls ``run_spec_harness``, so its specs are left
unscored. This utility walks a run directory's per-task workspaces
(``<run-dir>/workspaces/<task_id>/``), runs the spec-harness on each task's final
``Solution.java`` (using the vendored ``harness/task.json`` for inputs/signature),
and writes a consolidated score table — so the no-harness arm can be compared to
the harness arm on identical metrics.

Usage:
    python -m vact.run.score_no_harness \
        --run-dir out/vact__gpt-4o__YYYYmmdd_HHMMSS \
        --openjml openjml --threads 4
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from vact.core.data_types import HARNESS_PASS_THRESHOLD
from vact.tools.harness_tool import Task, evaluate_problem
from vact.tools.verifier_tool import verify_with_openjml

METRICS = ["post_correctness", "post_completeness", "pre_correctness", "pre_completeness"]


def _resolve_openjml(openjml: str) -> None:
    """evaluate_problem invokes the literal binary 'openjml'; if a path is given,
    prepend its dir to PATH so it resolves."""
    if openjml and openjml != "openjml" and os.sep in openjml:
        d = os.path.dirname(os.path.abspath(openjml))
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")


def find_workspaces(run_dir: str) -> list[str]:
    ws_root = os.path.join(run_dir, "workspaces")
    if not os.path.isdir(ws_root):
        return []
    out = []
    for name in sorted(os.listdir(ws_root)):
        d = os.path.join(ws_root, name)
        if os.path.isdir(d) and os.path.exists(os.path.join(d, "Solution.java")):
            out.append(d)
    return out


def score_workspace(ws: str, openjml: str, max_pairs: int, threshold: float) -> dict:
    task_id = os.path.basename(ws)
    row = {"task_id": task_id, "verified": False,
           **{m: None for m in METRICS}, "passed": False, "error": None}
    sol = os.path.join(ws, "Solution.java")
    tj = os.path.join(ws, "harness", "task.json")
    if not os.path.exists(tj):
        row["error"] = "missing harness/task.json"
        return row
    try:
        code = open(sol).read()
        task = Task.from_dict(json.load(open(tj)))
        # Verify gate — the spec-harness only applies to verifier-accepted specs.
        vr = verify_with_openjml(
            code, classname="Solution",
            output_dir=os.path.join(ws, "harness", "verify_tmp"),
        )
        row["verified"] = vr.success
        if not vr.success:
            # Unverified spec: counts as a failure (0 scores); skip the harness.
            for m in METRICS:
                row[m] = 0.0
            return row
        scores = evaluate_problem(
            task,
            llm_code=code,
            openjml_path=openjml,
            output_dir=os.path.join(ws, "harness"),
            max_pairs=max_pairs,
            run_id="score",
        )
    except Exception as e:  # noqa: BLE001
        row["error"] = str(e)
        return row
    if not scores:
        row["error"] = "no test pairs could be parsed"
        return row
    row["task_id"] = scores.get("task_id", task_id)
    for m in METRICS:
        row[m] = scores.get(m, 0.0)
    row["passed"] = (
        (scores.get("post_correctness") or 0.0) >= threshold          # None = not evaluable
        and (scores.get("post_completeness") or 0.0) >= threshold
    )
    return row


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Score a no-harness run with the spec-harness.")
    p.add_argument("--run-dir", required=True, help="run dir containing workspaces/")
    p.add_argument("--openjml", default=os.environ.get("OPENJML", "openjml"))
    p.add_argument("--threads", type=int, default=4, help="parallel tasks (default: 4)")
    p.add_argument("--max-pairs", type=int, default=5, dest="max_pairs")
    p.add_argument("--threshold", type=float, default=HARNESS_PASS_THRESHOLD)
    p.add_argument("--out", default=None, help="output prefix (default: <run-dir>/harness_scores)")
    args = p.parse_args(argv)

    _resolve_openjml(args.openjml)
    workspaces = find_workspaces(args.run_dir)
    if not workspaces:
        print(f"No workspaces with Solution.java under {args.run_dir}/workspaces", file=sys.stderr)
        return 1

    out_prefix = args.out or os.path.join(args.run_dir, "harness_scores")
    print(f"Scoring {len(workspaces)} task(s) with spec-harness, threads={args.threads}")

    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.threads) as pool:
        futs = {
            pool.submit(score_workspace, ws, args.openjml, args.max_pairs, args.threshold): ws
            for ws in workspaces
        }
        for fut in as_completed(futs):
            row = fut.result()
            rows.append(row)
            if row["error"]:
                tag = "err:" + row["error"]
            elif not row["verified"]:
                tag = "unverified"
            else:
                tag = f"pass={row['passed']}"
            print(f"  [{tag}] {row['task_id']}")

    rows.sort(key=lambda r: r["task_id"])
    fields = ["task_id", "verified", *METRICS, "passed", "error"]
    with open(out_prefix + ".json", "w") as fh:
        json.dump(rows, fh, indent=2)
    with open(out_prefix + ".csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    scored = [r for r in rows if r["error"] is None]
    verified = sum(1 for r in scored if r["verified"])
    passed = sum(1 for r in scored if r["passed"])
    avg = {
        m: round(sum(r[m] for r in scored if r[m] is not None) / len(scored), 3)
        if scored else 0.0
        for m in METRICS
    }
    print(f"Scored {len(scored)}/{len(rows)} (verified={verified}, passed={passed}); avg: {avg}")
    print(f"  -> {out_prefix}.json / {out_prefix}.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
