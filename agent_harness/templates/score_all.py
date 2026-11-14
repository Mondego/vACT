#!/usr/bin/env python3
"""Post-hoc spec-harness scoring for every task dir in this out-root.

Intended for the **no-harness** ablation arm: after the agents finish, run this
once to score each dir's agent-written ``Solution.java`` with the spec-harness (the
tool the agent never had access to) and store the four metrics. Works for the
with-harness arm too — it simply re-scores the final code.

For each task dir it invokes the vendored ``harness/cli.py harness --no-budget``
(so scoring never counts against the agent's attempt budget), then writes:

  * ``harness_scores.json`` / ``harness_scores.csv``  — the results table
  * updates each dir's ``submission.json`` (scores + passed) when present, so
    ``collect.py`` reflects the post-hoc scores too.

Honors ``$VACT_PYTHON`` / ``$OPENJML`` (or ``--python`` / ``--openjml``).
Self-contained (stdlib only); ships inside the out-root.

Examples
--------
    VACT_PYTHON=/path/.venv/bin/python OPENJML=/path/openjml python score_all.py
    python score_all.py --threads 8 --openjml /path/openjml
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.abspath(__file__))
METRICS = ["post_correctness", "post_completeness", "pre_correctness", "pre_completeness"]
THRESHOLD = 0.75


def find_task_dirs() -> list[str]:
    dirs = []
    for name in sorted(os.listdir(ROOT)):
        d = os.path.join(ROOT, name)
        if os.path.isdir(d) and os.path.exists(os.path.join(d, "harness", "cli.py")):
            dirs.append(d)
    return dirs


def score_dir(task_dir: str, python: str, openjml: str | None, timeout: int) -> dict:
    task_id = os.path.basename(task_dir)
    cli = os.path.join(task_dir, "harness", "cli.py")
    row: dict = {"task_id": task_id, "verified": False,
                 **{m: 0.0 for m in METRICS}, "passed": False, "error": None}
    try:
        # 1) Verify gate — the spec-harness only applies to verifier-accepted specs.
        vcmd = [python, cli, "verify", "--no-budget"]
        if openjml:
            vcmd += ["--openjml", openjml]
        vproc = subprocess.run(
            vcmd, cwd=task_dir, capture_output=True, text=True, timeout=timeout
        )
        verified = bool(json.loads(vproc.stdout).get("verified"))
        row["verified"] = verified
        if not verified:
            # Unverified spec: counts as a failure (0 scores); skip the harness.
            return row

        # 2) Spec-harness scoring (only when verified).
        cmd = [python, cli, "harness", "--no-budget"]
        if openjml:
            cmd += ["--openjml", openjml]
        proc = subprocess.run(
            cmd, cwd=task_dir, capture_output=True, text=True, timeout=timeout
        )
        data = json.loads(proc.stdout)
        metrics = data.get(task_id) or next(
            (v for k, v in data.items() if k != "_budget"), None
        )
        if not isinstance(metrics, dict):
            row["error"] = data.get("error", "no scores")
            return row
        for m in METRICS:
            row[m] = metrics.get(m)
        row["passed"] = (
            (metrics.get("post_correctness") or 0.0) >= THRESHOLD
            and (metrics.get("post_completeness") or 0.0) >= THRESHOLD
        )
        _update_submission(task_dir, task_id, metrics, row["passed"])
    except subprocess.TimeoutExpired:
        row["error"] = "timeout"
    except (subprocess.SubprocessError, json.JSONDecodeError, ValueError) as exc:
        row["error"] = str(exc)
    return row


def _update_submission(task_dir: str, task_id: str, metrics: dict, passed: bool) -> None:
    sub = os.path.join(task_dir, "submission.json")
    if not os.path.exists(sub):
        return
    try:
        with open(sub) as fh:
            data = json.load(fh)
        data["scores"] = {m: metrics.get(m) for m in METRICS}
        data["passed"] = passed
        with open(sub, "w") as fh:
            json.dump(data, fh, indent=2)
    except (OSError, json.JSONDecodeError):
        pass


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Score every task dir's Solution.java.")
    p.add_argument("--threads", type=int, default=4, help="parallel scorers (default: 4)")
    p.add_argument("--timeout", type=int, default=1800, help="per-task timeout seconds")
    p.add_argument("--python", help="interpreter to run harness/cli.py (default: $VACT_PYTHON or 'python')")
    p.add_argument("--openjml", help="OpenJML binary (default: $OPENJML)")
    p.add_argument("--out", default=os.path.join(ROOT, "harness_scores"),
                   help="output path prefix (default: ./harness_scores)")
    args = p.parse_args(argv)

    python = args.python or os.environ.get("VACT_PYTHON", "python")
    openjml = args.openjml or os.environ.get("OPENJML")

    task_dirs = find_task_dirs()
    if not task_dirs:
        print("No task dirs found.", file=sys.stderr)
        return 1

    print(f"Scoring {len(task_dirs)} task(s) with spec-harness, threads={args.threads}")
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.threads) as pool:
        futs = {
            pool.submit(score_dir, d, python, openjml, args.timeout): d
            for d in task_dirs
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
    with open(args.out + ".json", "w") as fh:
        json.dump(rows, fh, indent=2)
    with open(args.out + ".csv", "w", newline="") as fh:
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
    print(f"  -> {args.out}.json / {args.out}.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
