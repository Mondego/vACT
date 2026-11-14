#!/usr/bin/env python3
"""Aggregate a vACT run: pass@{0.25,0.5,0.75,1.0} + mean spec-harness metrics.

Reads per-task scores from (in priority):
  1. <run-dir>/harness_scores.json   (written by run.score_no_harness — used for
     the no-harness arm)
  2. <run-dir>/trajectories.jsonl    (with-harness arm: the last run_spec_harness
     scores recorded in each task's trajectory)

pass@T = fraction of tasks with post_correctness >= T AND post_completeness >= T.
Writes <run-dir>/aggregate.json and prints a summary.

Usage:
    python -m vact.run.aggregate --run-dir out/vact__gpt-4o__<timestamp>
"""

from __future__ import annotations

import argparse
import json
import os
import sys

METRICS = ["post_correctness", "post_completeness", "pre_correctness", "pre_completeness"]
THRESHOLDS = [0.25, 0.5, 0.75, 1.0]


def _scores_from_trajectory(traj: dict) -> dict | None:
    """Pull the last run_spec_harness metrics dict out of a trajectory."""
    steps = (traj.get("agent_dict", {}) or {}).get("trajectories", {}).get("steps", [])
    last = None
    for step in steps:
        for to in step.get("tool_outputs", []) or []:
            if to.get("tool_name") != "run_spec_harness":
                continue
            out = to.get("output")
            if isinstance(out, str):
                try:
                    out = json.loads(out)
                except (json.JSONDecodeError, TypeError):
                    continue
            if isinstance(out, dict):
                for _tid, metrics in out.items():
                    if isinstance(metrics, dict) and "post_correctness" in metrics:
                        last = metrics
    return last


def load_rows(run_dir: str) -> list[dict]:
    # 1) harness_scores.json (no-harness arm, scored offline)
    hs = os.path.join(run_dir, "harness_scores.json")
    if os.path.exists(hs):
        with open(hs) as fh:
            data = json.load(fh)
        return [
            {"task_id": r.get("task_id", "?"), **{m: r.get(m) for m in METRICS}}
            for r in data
            if not r.get("error")
        ]
    # 2) trajectories.jsonl (with-harness arm)
    rows = []
    jl = os.path.join(run_dir, "trajectories.jsonl")
    trajs: list[dict] = []
    if os.path.exists(jl):
        with open(jl) as fh:
            trajs = [json.loads(ln) for ln in fh if ln.strip()]
    else:
        tdir = os.path.join(run_dir, "trajectories")
        if os.path.isdir(tdir):
            for name in sorted(os.listdir(tdir)):
                if name.endswith(".json"):
                    with open(os.path.join(tdir, name)) as fh:
                        trajs.append(json.load(fh))
    for traj in trajs:
        metrics = _scores_from_trajectory(traj)
        if metrics:
            rows.append(
                {"task_id": traj.get("task_id", "?"), **{m: metrics.get(m) for m in METRICS}}
            )
    return rows


def aggregate(rows: list[dict]) -> dict:
    n = len(rows)
    means = {
        m: round(sum((r[m] or 0.0) for r in rows) / n, 4) if n else 0.0
        for m in METRICS
    }
    passes = {}
    for t in THRESHOLDS:
        c = sum(
            1
            for r in rows
            if (r["post_correctness"] or 0.0) >= t and (r["post_completeness"] or 0.0) >= t
        )
        passes[f"{t:g}"] = {"count": c, "rate": round(c / n, 4) if n else 0.0}
    return {"n_tasks": n, "mean_metrics": means, "pass_at": passes}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="pass@{0.25,0.5,0.75,1.0} + mean metrics.")
    p.add_argument("--run-dir", required=True, help="a vact run dir")
    p.add_argument("--out", default=None, help="output json (default: <run-dir>/aggregate.json)")
    args = p.parse_args(argv)

    rows = load_rows(args.run_dir)
    if not rows:
        print(
            "No scores found (need harness_scores.json or trajectories with "
            "run_spec_harness outputs).",
            file=sys.stderr,
        )
        return 1
    agg = aggregate(rows)

    out = args.out or os.path.join(args.run_dir, "aggregate.json")
    with open(out, "w") as fh:
        json.dump(agg, fh, indent=2)

    print(f"tasks scored: {agg['n_tasks']}")
    print("mean metrics:")
    for m, v in agg["mean_metrics"].items():
        print(f"  {m:18s} {v}")
    print("pass@T (post_correctness >= T AND post_completeness >= T):")
    for t, info in agg["pass_at"].items():
        print(f"  @{t:<4} {info['rate']:.3f}  ({info['count']}/{agg['n_tasks']})")
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
