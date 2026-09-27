#!/usr/bin/env python3
"""
Prefix re-scoring (T8 sensitivity): recompute the Spec-Harness metrics for the first N evaluation pairs,
for several N, from ONE harness run (evaluate on the v2 eval50 suite, then run this).

The harness saves one verdict per check with its pair label case_<k>; in a v2 suite k is the pair's rank
in the T5 evaluation list, so "the first N pairs" = labels case_0 .. case_<N-1>:
  PostCorr / PostComp / PreCorr   checks of pairs with rank < N
  PreComp                         always the full validated T- (independent of N)
Undecided checks (unknown / error) are excluded from each metric's denominator (as in the harness).
A metric with no decided check is not evaluable for that task.

Post-Harness@tau (as in rescore_rq4.py): PostCorr >= tau and PostComp >= tau. Denominator: all tasks
of the run except those whose post checks at this N are all unknown (D3 all-unknown rule; reported).

Usage: python spec_harness/score_subsets.py <per-task result dir> [--n 3 5 10 20 50] [--out table.json]
"""
import argparse
import glob
import json
import os
import statistics as st

METRICS = {"post_correctness": "pass", "post_completeness": "killed", "pre_correctness": "pass",
           "pre_completeness": "rejected"}
THRESHOLDS = [0.25, 0.5, 0.75, 1.0]


def rank(label):
    if label and label.startswith("case_"):
        try:
            return int(label.split("_", 1)[1])
        except ValueError:
            return None
    return None


def at_n(res, n):
    out = {}
    for m, key in METRICS.items():
        det = res.get(m, {}).get("details", [])
        if m != "pre_completeness":
            det = [d for d in det if (rank(d.get("label")) is not None and rank(d.get("label")) < n)]
        decided = [d for d in det if d.get(key) is not None]
        unknown = sum(1 for d in det if d.get(key) is None and d.get("verdict") == "unknown")
        out[m] = {"score": (sum(1 for d in decided if d[key]) / len(decided)) if decided else None,
                  "decided": len(decided), "unknown": unknown}
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("result_dir")
    ap.add_argument("--n", type=int, nargs="+", default=[3, 5, 10, 20, 50])
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    results = []
    for f in sorted(glob.glob(os.path.join(a.result_dir, "*.json"))):
        r = json.load(open(f))
        if isinstance(r, dict) and "task_id" in r and "post_correctness" in r:
            results.append(r)
    table = {"tasks": len(results), "per_n": {}}
    for n in a.n:
        rows = []
        for r in results:
            if r.get("excluded") and not r.get("integrity_failed"):
                continue
            s = at_n(r, n) if not r.get("integrity_failed") else {m: {"score": 0.0, "decided": 1, "unknown": 0}
                                                                   for m in METRICS}
            rows.append((r["task_id"], s))
        entry = {"tasks_scored": len(rows)}
        for m in METRICS:
            vals = [s[m]["score"] for _, s in rows if s[m]["score"] is not None]
            entry[m] = {"mean": round(st.mean(vals), 4) if vals else None, "evaluable_tasks": len(vals)}
        post_unknown = [tid for tid, s in rows if s["post_correctness"]["decided"] == 0
                        and s["post_correctness"]["unknown"] > 0]
        denom = [s for tid, s in rows if tid not in post_unknown]
        for t in THRESHOLDS:
            ok = sum(1 for s in denom if (s["post_correctness"]["score"] or 0) >= t
                     and (s["post_completeness"]["score"] or 0) >= t)
            entry[f"post_harness@{t:g}"] = round(ok / len(denom), 4) if denom else None
        entry["post_all_unknown_tasks"] = len(post_unknown)
        table["per_n"][str(n)] = entry
    print(json.dumps(table, indent=1))
    if a.out:
        json.dump(table, open(a.out, "w"), indent=1)


if __name__ == "__main__":
    main()
