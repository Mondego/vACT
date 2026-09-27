#!/usr/bin/env python3
"""
Collect RQ3 / RQ4 results from one or more machines into CSVs (figures are generated from these CSVs only).

  python runners/collect.py rq3 --in m1/rq3_out [m2/rq3_out ...] --out results/rq3
  python runners/collect.py rq4 --in m2/rq4_out m3/rq4_out --out results/rq4

rq3 writes
  rq3_per_spec.csv       one row per RQ1 spec (all machines merged)
  rq3_fig4_cells.csv     Fig. 4: per benchmark x model x approach (SG/AS/FB) x metric x tau:
                         n_verified, n_evaluable, n_meeting, fraction = n_meeting / n_evaluable
  rq3_by_config.csv      per RQ1 file (approach, bench, model, prompt) x metric: mean and fraction >= tau
                         (Fig. 5 / Table IV select from it)
  rq3_collect_report.json  completeness (expected vs present units), statuses, bundle hashes, conflicts
rq4 writes
  rq4_per_task.csv       one row per (arm, agent, bench, task)
  rq4_summary.csv        per arm x agent x bench: VR, Post-Harness@tau (PostCorr >= tau and PostComp >= tau;
                         denominator = all tasks, unverified = 0, tasks with all-unknown post checks excluded
                         and counted), mean metrics, fraction >= tau per metric
  rq4_collect_report.json

Scoring conventions (identical to the paper's definitions):
  * integrity failure -> every metric = 0 and evaluable (a failed synthesis, not an unscorable one)
  * a metric with no decided check (e.g. PreComp with an empty T-, all-unknown post checks) is not
    evaluable: it is left out of that metric's denominator and counted in n_not_evaluable
"""
from __future__ import annotations

import argparse
import collections
import csv
import glob
import json
import os
import shutil

import common as C

METRICS = ("post_correctness", "post_completeness", "pre_correctness", "pre_completeness")
SHORT = {"post_correctness": "PostCorr", "post_completeness": "PostComp", "pre_correctness": "PreCorr",
         "pre_completeness": "PreComp"}
APPROACH = {"specgen": "SG", "autospec": "AS", "formalbench": "FB"}
THRESHOLDS = (0.25, 0.5, 0.75, 1.0)


def metric_values(rec: dict) -> dict:
    """metric -> score (float) or None (not evaluable)."""
    h = rec.get("harness") or {}
    if h.get("integrity_failed") or h.get("no_postcondition"):
        return {m: 0.0 for m in METRICS}
    out = {}
    for m in METRICS:
        x = h.get(m) or {}
        ev = x.get("evaluable", (x.get("total") or 0) > 0)
        out[m] = float(x["score"]) if ev and x.get("score") is not None else None
    return out


def write_csv(path, rows):
    if not rows:
        return
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def merge(ins, sub, key_fn, out_dir):
    """Copy result JSONs from every input dir into out_dir/<sub>; report duplicate keys."""
    seen, conflicts = {}, []
    for d in ins:
        for p in glob.glob(os.path.join(d, sub)):
            rec = json.load(open(p))
            k = key_fn(rec)
            if k in seen:
                conflicts.append({"key": k, "a": seen[k], "b": p})
                continue
            seen[k] = p
            dst = os.path.join(out_dir, "merged", os.path.relpath(p, d))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(p, dst)
    return seen, conflicts


# ------------------------------------------------------------------ RQ3

def cmd_rq3(a) -> int:
    import rq3_run
    os.makedirs(a.out, exist_ok=True)
    seen, conflicts = merge(a.inputs, os.path.join("results", "*", "*.json"), lambda r: r["unit"], a.out)
    expected = {u["unit"]: u for u in rq3_run.units(a.rq1_dir, ["sgb", "fb"], None, 0)}
    recs = {k: json.load(open(p)) for k, p in seen.items()}
    rows = []
    for unit in sorted(recs):
        r = recs[unit]
        v = metric_values(r) if r.get("status") == "scored" else {m: None for m in METRICS}
        h = r.get("harness") or {}
        row = {k: r.get(k, "") for k in ("unit", "file", "approach", "bench", "model", "prompt", "task_id", "status")}
        row.update(integrity_failed=h.get("integrity_failed", False),
                   no_postcondition=h.get("no_postcondition", False), excluded=h.get("excluded", ""),
                   exclude_reason=h.get("exclude_reason", ""), post_all_unknown=h.get("post_all_unknown", ""))
        for m in METRICS:
            x = h.get(m) or {}
            row[SHORT[m]] = "" if v[m] is None else v[m]
            row[f"{SHORT[m]}_decided"] = x.get("total", "")
            row[f"{SHORT[m]}_unknown"] = x.get("unknowns", "")
            row[f"{SHORT[m]}_error"] = x.get("errors", "")
        row.update(seconds=r.get("seconds", ""), host=(r.get("provenance") or {}).get("host", ""),
                   bundle=(r.get("provenance") or {}).get("bundle", ""))
        rows.append(row)
    write_csv(os.path.join(a.out, "rq3_per_spec.csv"), rows)

    # Fig. 4 cells: per bench x model x approach(SG/AS/FB) x metric x tau
    cells = []
    groups = collections.defaultdict(list)
    for r in rows:
        if r["approach"] in APPROACH and r["status"] == "scored":
            groups[(r["bench"], r["model"], APPROACH[r["approach"]])].append(r)
    for (bench, model, ap), rs in sorted(groups.items()):
        for m in METRICS:
            vals = [float(r[SHORT[m]]) for r in rs if r[SHORT[m]] != ""]
            for t in THRESHOLDS:
                meet = sum(1 for x in vals if x >= t)
                cells.append({"bench": bench, "model": model, "approach": ap, "metric": SHORT[m], "tau": t,
                              "n_verified": len(rs), "n_evaluable": len(vals), "n_not_evaluable": len(rs) - len(vals),
                              "n_meeting": meet, "fraction": round(meet / len(vals), 4) if vals else ""})
    write_csv(os.path.join(a.out, "rq3_fig4_cells.csv"), cells)

    # per RQ1 configuration (all approaches incl. Houdini / Daikon): Fig. 5 / Table IV select from this
    by = collections.defaultdict(list)
    for r in rows:
        if r["status"] == "scored":
            by[(r["approach"], r["bench"], r["model"], r["prompt"], r["file"])].append(r)
    conf = []
    for (ap, bench, model, prompt, f), rs in sorted(by.items()):
        row = {"approach": ap, "bench": bench, "model": model, "prompt": prompt, "file": f, "n_scored": len(rs)}
        for m in METRICS:
            vals = [float(r[SHORT[m]]) for r in rs if r[SHORT[m]] != ""]
            row[f"{SHORT[m]}_n_evaluable"] = len(vals)
            row[f"{SHORT[m]}_mean"] = round(sum(vals) / len(vals), 4) if vals else ""
            for t in THRESHOLDS:
                row[f"{SHORT[m]}_ge{t:g}"] = round(sum(1 for x in vals if x >= t) / len(vals), 4) if vals else ""
        conf.append(row)
    write_csv(os.path.join(a.out, "rq3_by_config.csv"), conf)

    report = {"expected_units": len(expected), "present": len(recs),
              "missing": sorted(set(expected) - set(recs))[:200], "n_missing": len(set(expected) - set(recs)),
              "unexpected": sorted(set(recs) - set(expected))[:50],
              "status": dict(collections.Counter(r.get("status") for r in recs.values())),
              "bundles": dict(collections.Counter((r.get("provenance") or {}).get("bundle") for r in recs.values())),
              "hosts": dict(collections.Counter((r.get("provenance") or {}).get("host") for r in recs.values())),
              "duplicate_units_across_inputs": conflicts[:50], "n_conflicts": len(conflicts)}
    C.atomic_json(os.path.join(a.out, "rq3_collect_report.json"), report)
    print(json.dumps({k: v for k, v in report.items() if k not in ("missing", "unexpected", "duplicate_units_across_inputs")}, indent=1))
    if len(report["bundles"]) > 1:
        print("WARNING: results come from different bundles")
    return 0


# ------------------------------------------------------------------ RQ4

def cmd_rq4(a) -> int:
    os.makedirs(a.out, exist_ok=True)
    seen, conflicts = merge(a.inputs, os.path.join("*", "_scores", "*", "*.json"),
                            lambda r: f"{r['arm']}/{r['agent']}_{r['bench']}/{r['task_id']}", a.out)
    recs = {k: json.load(open(p)) for k, p in seen.items()}
    rows = []
    for k in sorted(recs):
        r = recs[k]
        v = metric_values(r) if r.get("status") == "scored" else {m: None for m in METRICS}
        h = r.get("harness") or {}
        row = {x: r.get(x, "") for x in ("arm", "agent", "bench", "task_id", "status", "verified")}
        row.update(integrity_failed=h.get("integrity_failed", False),
                   no_postcondition=h.get("no_postcondition", False), post_all_unknown=h.get("post_all_unknown", ""))
        for m in METRICS:
            row[SHORT[m]] = "" if v[m] is None else v[m]
        row.update(host=(r.get("provenance") or {}).get("host", ""), bundle=(r.get("provenance") or {}).get("bundle", ""))
        rows.append(row)
    write_csv(os.path.join(a.out, "rq4_per_task.csv"), rows)
    summ = []
    groups = collections.defaultdict(list)
    for r in rows:
        groups[(r["arm"], r["agent"], r["bench"])].append(r)
    for (arm, agent, bench), rs in sorted(groups.items()):
        s = {"arm": arm, "agent": agent, "bench": bench, "n_tasks": len(rs),
             "verified": sum(1 for r in rs if r["verified"] is True), }
        s["VR"] = round(s["verified"] / len(rs), 4)
        unk = [r for r in rs if r["post_all_unknown"] is True]
        denom = [r for r in rs if r not in unk]
        s["post_all_unknown"] = len(unk)
        for t in THRESHOLDS:
            ok = sum(1 for r in denom if r["PostCorr"] != "" and r["PostComp"] != ""
                     and float(r["PostCorr"]) >= t and float(r["PostComp"]) >= t)
            s[f"PostHarness@{t:g}"] = round(ok / len(denom), 4) if denom else ""
        for m in METRICS:
            vals = [float(r[SHORT[m]]) for r in rs if r[SHORT[m]] != ""]
            s[f"{SHORT[m]}_mean"] = round(sum(vals) / len(vals), 4) if vals else ""
            s[f"{SHORT[m]}_ge0.75"] = round(sum(1 for x in vals if x >= 0.75) / len(vals), 4) if vals else ""
        summ.append(s)
    write_csv(os.path.join(a.out, "rq4_summary.csv"), summ)
    report = {"present": len(recs), "status": dict(collections.Counter(r.get("status") for r in recs.values())),
              "bundles": dict(collections.Counter((r.get("provenance") or {}).get("bundle") for r in recs.values())),
              "hosts": dict(collections.Counter((r.get("provenance") or {}).get("host") for r in recs.values())),
              "n_conflicts": len(conflicts)}
    C.atomic_json(os.path.join(a.out, "rq4_collect_report.json"), report)
    print(json.dumps(report, indent=1))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p3 = sub.add_parser("rq3")
    p3.add_argument("--in", dest="inputs", nargs="+", required=True)
    p3.add_argument("--out", required=True)
    p3.add_argument("--rq1-dir", default=os.path.join(C.REPO, "inputs", "rq1"))
    p3.set_defaults(func=cmd_rq3)
    p4 = sub.add_parser("rq4")
    p4.add_argument("--in", dest="inputs", nargs="+", required=True)
    p4.add_argument("--out", required=True)
    p4.set_defaults(func=cmd_rq4)
    a = ap.parse_args()
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
