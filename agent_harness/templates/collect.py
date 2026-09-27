#!/usr/bin/env python3
"""Aggregate every submission.json in this out-root into a comparison table.

Writes ``comparison.json`` and ``comparison.csv`` summarizing each task's four
spec-harness scores and pass/fail. Run this after ``run_agents.py`` (or after an
agent has solved the dirs). Run it once per agent's out-root, then diff the
tables to compare VeriAct / Claude Code / Codex.

Self-contained (stdlib only); ships inside the out-root.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
METRICS = ["post_correctness", "post_completeness", "pre_correctness", "pre_completeness"]
THRESHOLD = 0.75


def _score_offline(task_dir: str) -> dict | None:
    """Run the vendored harness CLI on a dir's Solution.java (ablation arm).

    Honors $VERIACT_PYTHON (else 'python') and $OPENJML. Returns the metric dict
    or None on failure.
    """
    cli = os.path.join(task_dir, "harness", "cli.py")
    if not os.path.exists(cli):
        return None
    python = os.environ.get("VERIACT_PYTHON", "python")
    cmd = [python, cli, "harness", "--no-budget"]
    if os.environ.get("OPENJML"):
        cmd += ["--openjml", os.environ["OPENJML"]]
    try:
        out = subprocess.run(
            cmd, cwd=task_dir, capture_output=True, text=True, timeout=1800
        ).stdout
        data = json.loads(out)
        metrics = next(iter(data.values()), None)
        return metrics if isinstance(metrics, dict) and "error" not in data else None
    except (subprocess.SubprocessError, json.JSONDecodeError, ValueError):
        return None


def collect(score_missing: bool = False) -> list[dict]:
    rows: list[dict] = []
    for name in sorted(os.listdir(ROOT)):
        task_dir = os.path.join(ROOT, name)
        sub = os.path.join(task_dir, "submission.json")
        if not os.path.exists(sub):
            continue
        with open(sub) as fh:
            data = json.load(fh)
        scores = data.get("scores") or {}
        if not scores and score_missing:
            print(f"  scoring {name} offline ...", file=sys.stderr)
            scored = _score_offline(task_dir)
            if scored:
                scores = scored
                data["passed"] = (
                    (scored.get("post_correctness") or 0.0) >= THRESHOLD
                    and (scored.get("post_completeness") or 0.0) >= THRESHOLD
                )
        row = {
            "task_id": data.get("task_id", name),
            "submitted": True,
            "passed": bool(data.get("passed", False)),
        }
        for m in METRICS:
            row[m] = scores.get(m) if isinstance(scores, dict) else None
        rows.append(row)
    return rows


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Aggregate submissions into a comparison table.")
    p.add_argument("--out", default=os.path.join(ROOT, "comparison"),
                   help="output path prefix (default: ./comparison)")
    p.add_argument("--score-missing", action="store_true",
                   help="score submissions lacking scores via the vendored harness "
                        "CLI (for no-harness ablation roots; honors "
                        "$VERIACT_PYTHON / $OPENJML)")
    args = p.parse_args(argv)

    rows = collect(score_missing=args.score_missing)
    fields = ["task_id", "submitted", "passed", *METRICS]

    with open(args.out + ".json", "w") as fh:
        json.dump(rows, fh, indent=2)
    with open(args.out + ".csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    n = len(rows)
    passed = sum(1 for r in rows if r["passed"])
    avg = {
        m: round(sum(r[m] for r in rows if r[m] is not None) / n, 3) if n else 0.0
        for m in METRICS
    }
    print(f"Collected {n} submission(s); passed={passed}")
    print(f"  avg: {avg}")
    print(f"  -> {args.out}.json / {args.out}.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
