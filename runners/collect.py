#!/usr/bin/env python3
"""
Collect RQ3 / RQ4 results from one or more machines into CSVs (figures are generated from these CSVs only).

  python runners/collect.py rq3 --in m1/rq3_out [m2/rq3_out ...] --out results/rq3
  python runners/collect.py rq4 --in m2/rq4_out m3/rq4_out --out results/rq4
  --pairs N  re-scores every result from its first N evaluation pairs (e.g. machines scored with 10 and with
             the T8-chosen N: collect both with --pairs N)

rq3 writes
  rq3_per_spec.csv       one row per RQ1 spec (all machines merged)
  rq3_fig4_cells.csv     Fig. 4: per benchmark x model x approach (SG/AS/FB) x metric x tau:
                         fraction = n_meeting / denominator, with a FIXED denominator (see below), plus the
                         counts behind it (verified, evaluable, not evaluable, unknown)
  rq3_by_config.csv      per RQ1 file (approach, bench, model, prompt) x metric: fraction >= tau over the fixed
                         denominator, and the mean over evaluable specs (Fig. 5 / Table IV select from it)
  rq3_collect_report.json  completeness (expected vs present units), statuses, bundle hashes, conflicts
rq4 writes
  rq4_per_task.csv       one row per (arm, agent, bench, task)
  rq4_summary.csv        per arm x agent x bench: VR, Post-Harness@tau (PostCorr >= tau and PostComp >= tau) and
                         fraction >= tau per metric, all over the FIXED denominator; all-unknown post checks
                         are counted (not meeting); mean metrics over evaluable tasks
  rq4_collect_report.json

Scoring conventions (identical to the paper's definitions):
  * integrity failure / no postcondition (D13) -> every metric = 0 (a failed synthesis, not an unscorable one)
  * FIXED denominators (D14, like the verification rate of Fig. 3): whatever a run produced, a fraction >= tau
    is n_meeting / (benchmark tasks x prompt configurations of the cell). SpecGenBench = 120 tasks,
    FormalBench = 662 (RQ3) and FB-120 = 120 (RQ4), from the evaluation suite files. Prompt configurations
    are the RQ1 design: SpecGen 3, AutoSpec 3, FormalBench 5, Houdini / Daikon 1 (the RQ1 files per
    bench x approach x model); RQ4 has 1. Everything that does not reach tau counts as not meeting: not
    verified in RQ1 / by the agent, no postcondition, integrity failure, undecided (unknown) checks.
    PreComp: the denominator is the tasks whose evaluation suite has invalid inputs (T-), x configurations;
    for the other tasks no input makes the reference fail, so PreComp is undefined by design (a fixed set
    per benchmark: SGB 65, FB 523, FB-120 96). The means are over evaluable specs and are descriptive only.
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
SUITE_FILES = {"rq3": {"sgb": "specgenbench/sgb_v2_eval.json", "fb": "formalbench/fb_v2_eval.json"},
               "rq4": {"sgb": "specgenbench/sgb_v2_eval.json", "fb": "formalbench/fb_120_v2_eval.json"}}


def fixed_sizes(rq: str) -> dict:
    """bench -> (tasks, tasks with a non-empty evaluation T-), from the suite files the scoring used."""
    out = {}
    for b, rel in SUITE_FILES[rq].items():
        ts = json.load(open(os.path.join(C.REPO, "benchmarks", rel)))
        out[b] = (len(ts), sum(1 for t in ts if t.get("invalid_inputs")))
    return out


def meets(r: dict, m: str, t: float) -> bool:
    x = r.get(SHORT[m], "")
    return x != "" and float(x) >= t


KEY = {"post_correctness": "pass", "post_completeness": "killed", "pre_correctness": "pass",
       "pre_completeness": "rejected"}
PAIRS = 0          # --pairs N: re-score every result from its first N evaluation pairs (0 = as scored)


def metric_values(rec: dict) -> dict:
    """metric -> score (float) or None (not evaluable)."""
    h = rec.get("harness") or {}
    if h.get("integrity_failed") or h.get("no_postcondition"):
        return {m: 0.0 for m in METRICS}
    if PAIRS:
        from spec_harness.score_subsets import at_n
        return {m: v["score"] for m, v in at_n(h, PAIRS).items()}
    out = {}
    for m in METRICS:
        x = h.get(m) or {}
        ev = x.get("evaluable", (x.get("total") or 0) > 0)
        out[m] = float(x["score"]) if ev and x.get("score") is not None else None
    return out


def post_all_unknown(rec: dict):
    """True if every post check is undecided (at --pairs N: among the first N pairs)."""
    h = rec.get("harness") or {}
    if not PAIRS or h.get("integrity_failed") or h.get("no_postcondition"):
        return h.get("post_all_unknown", "")
    from spec_harness.score_subsets import at_n
    pc = at_n(h, PAIRS)["post_correctness"]
    return pc["decided"] == 0 and pc["unknown"] > 0


def check_pairs(recs: dict) -> None:
    """--pairs N needs results scored with at least N pairs (pairs 0 = all of the suite's, >= 10)."""
    if not PAIRS:
        return
    few = [k for k, r in recs.items() if r.get("status") == "scored" and 0 < (r.get("pairs") or 0) < PAIRS]
    if few:
        raise SystemExit(f"--pairs {PAIRS}: {len(few)} results were scored with fewer pairs, e.g. {few[:3]}")


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
    check_pairs(recs)
    rows = []
    for unit in sorted(recs):
        r = recs[unit]
        v = metric_values(r) if r.get("status") == "scored" else {m: None for m in METRICS}
        h = r.get("harness") or {}
        row = {k: r.get(k, "") for k in ("unit", "file", "approach", "bench", "model", "prompt", "task_id", "status")}
        row.update(integrity_failed=h.get("integrity_failed", False),
                   no_postcondition=h.get("no_postcondition", False), excluded=h.get("excluded", ""),
                   exclude_reason=h.get("exclude_reason", ""), post_all_unknown=post_all_unknown(r))
        for m in METRICS:
            x = h.get(m) or {}
            row[SHORT[m]] = "" if v[m] is None else v[m]
            if PAIRS and x.get("details") is not None:     # counts of the first N pairs' checks
                from spec_harness.score_subsets import rank
                det = [d for d in x["details"] if m == "pre_completeness"
                       or (rank(d.get("label")) is not None and rank(d.get("label")) < PAIRS)]
                row[f"{SHORT[m]}_decided"] = sum(1 for d in det if d.get(KEY[m]) is not None)
                row[f"{SHORT[m]}_unknown"] = sum(1 for d in det if d.get(KEY[m]) is None and d.get("verdict") == "unknown")
                row[f"{SHORT[m]}_error"] = sum(1 for d in det if d.get(KEY[m]) is None and d.get("verdict") == "error")
            else:
                row[f"{SHORT[m]}_decided"] = x.get("total", "")
                row[f"{SHORT[m]}_unknown"] = x.get("unknowns", "")
                row[f"{SHORT[m]}_error"] = x.get("errors", "")
        row.update(seconds=r.get("seconds", ""), host=(r.get("provenance") or {}).get("host", ""),
                   bundle=(r.get("provenance") or {}).get("bundle", ""))
        rows.append(row)
    write_csv(os.path.join(a.out, "rq3_per_spec.csv"), rows)

    # fixed denominators (D14): tasks (or tasks with T-) x RQ1 prompt configurations of the cell
    sizes = fixed_sizes("rq3")
    configs = collections.defaultdict(set)                 # (bench, approach, model) -> RQ1 files
    for f in glob.glob(os.path.join(a.rq1_dir, "*.jsonl")):
        meta = rq3_run.parse_stem(os.path.basename(f)[:-6])
        configs[(meta["bench"], meta["approach"], meta["model"])].add(os.path.basename(f)[:-6])

    def denom(bench, m, n_configs):
        return (sizes[bench][1] if m == "pre_completeness" else sizes[bench][0]) * n_configs

    # Fig. 4 cells: per bench x model x approach(SG/AS/FB) x metric x tau
    cells = []
    groups = collections.defaultdict(list)
    for r in rows:
        if r["approach"] in APPROACH:
            groups[(r["bench"], r["model"], r["approach"])].append(r)
    for (bench, approach, model) in sorted(configs):
        if approach not in APPROACH:
            continue
        rs = [r for r in groups.get((bench, model, approach), []) if r["status"] == "scored"]
        n_cfg = len(configs[(bench, approach, model)])
        for m in METRICS:
            vals = [r for r in rs if r[SHORT[m]] != ""]
            for t in THRESHOLDS:
                meet = sum(1 for r in rs if meets(r, m, t))
                d = denom(bench, m, n_cfg)
                cells.append({"bench": bench, "model": model, "approach": APPROACH[approach], "metric": SHORT[m],
                              "tau": t, "configs": n_cfg, "denominator": d, "n_meeting": meet,
                              "fraction": round(meet / d, 4),
                              "n_verified_scored": len(rs), "n_evaluable": len(vals),
                              "n_not_evaluable": len(rs) - len(vals),
                              "n_no_postcondition": sum(1 for r in rs if r["no_postcondition"] is True),
                              "n_integrity_failed": sum(1 for r in rs if r["integrity_failed"] is True),
                              "n_post_all_unknown": sum(1 for r in rs if r["post_all_unknown"] is True)})
    write_csv(os.path.join(a.out, "rq3_fig4_cells.csv"), cells)

    # per RQ1 configuration (all approaches incl. Houdini / Daikon): Fig. 5 / Table IV select from this
    by = collections.defaultdict(list)
    for r in rows:
        if r["status"] == "scored":
            by[r["file"]].append(r)
    conf = []
    for (bench, ap, model), files in sorted(configs.items()):
        for f in sorted(files):
            rs = by.get(f, [])
            meta = rq3_run.parse_stem(f)
            row = {"approach": ap, "bench": bench, "model": model, "prompt": meta["prompt"], "file": f,
                   "n_scored": len(rs), "tasks": sizes[bench][0], "tasks_with_tminus": sizes[bench][1]}
            for m in METRICS:
                vals = [float(r[SHORT[m]]) for r in rs if r[SHORT[m]] != ""]
                row[f"{SHORT[m]}_n_evaluable"] = len(vals)
                row[f"{SHORT[m]}_mean_evaluable"] = round(sum(vals) / len(vals), 4) if vals else ""
                for t in THRESHOLDS:
                    row[f"{SHORT[m]}_ge{t:g}"] = round(sum(1 for r in rs if meets(r, m, t)) / denom(bench, m, 1), 4)
            for t in THRESHOLDS:
                row[f"PostHarness@{t:g}"] = round(sum(1 for r in rs if meets(r, "post_correctness", t)
                                                      and meets(r, "post_completeness", t)) / sizes[bench][0], 4)
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
    check_pairs(recs)
    rows = []
    for k in sorted(recs):
        r = recs[k]
        v = metric_values(r) if r.get("status") == "scored" else {m: None for m in METRICS}
        h = r.get("harness") or {}
        row = {x: r.get(x, "") for x in ("arm", "agent", "bench", "task_id", "status", "verified")}
        row.update(integrity_failed=h.get("integrity_failed", False),
                   no_postcondition=h.get("no_postcondition", False), post_all_unknown=post_all_unknown(r))
        for m in METRICS:
            row[SHORT[m]] = "" if v[m] is None else v[m]
        row.update(host=(r.get("provenance") or {}).get("host", ""), bundle=(r.get("provenance") or {}).get("bundle", ""))
        rows.append(row)
    write_csv(os.path.join(a.out, "rq4_per_task.csv"), rows)
    sizes = fixed_sizes("rq4")
    summ = []
    groups = collections.defaultdict(list)
    for r in rows:
        groups[(r["arm"], r["agent"], r["bench"])].append(r)
    for (arm, agent, bench), rs in sorted(groups.items()):
        n, n_pre = sizes[bench]
        ok_rs = [r for r in rs if r["verified"] is True]          # unverified: no metric meets any tau
        s = {"arm": arm, "agent": agent, "bench": bench, "tasks": n, "tasks_with_tminus": n_pre,
             "task_records": len(rs), "verified": len(ok_rs)}
        s["VR"] = round(len(ok_rs) / n, 4)
        s["post_all_unknown"] = sum(1 for r in rs if r["post_all_unknown"] is True)
        s["no_postcondition"] = sum(1 for r in rs if r["no_postcondition"] is True)
        s["integrity_failed"] = sum(1 for r in rs if r["integrity_failed"] is True)
        for t in THRESHOLDS:
            s[f"PostHarness@{t:g}"] = round(sum(1 for r in ok_rs if meets(r, "post_correctness", t)
                                                and meets(r, "post_completeness", t)) / n, 4)
        for m in METRICS:
            vals = [float(r[SHORT[m]]) for r in ok_rs if r[SHORT[m]] != ""]
            s[f"{SHORT[m]}_mean_evaluable"] = round(sum(vals) / len(vals), 4) if vals else ""
            for t in THRESHOLDS:
                s[f"{SHORT[m]}_ge{t:g}"] = round(sum(1 for r in ok_rs if meets(r, m, t))
                                                 / (n_pre if m == "pre_completeness" else n), 4)
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
    p3.add_argument("--pairs", type=int, default=0,
                    help="re-score from the first N evaluation pairs (results must have >= N; 0 = as scored)")
    p3.set_defaults(func=cmd_rq3)
    p4 = sub.add_parser("rq4")
    p4.add_argument("--in", dest="inputs", nargs="+", required=True)
    p4.add_argument("--out", required=True)
    p4.add_argument("--pairs", type=int, default=0,
                    help="re-score from the first N evaluation pairs (results must have >= N; 0 = as scored)")
    p4.set_defaults(func=cmd_rq4)
    a = ap.parse_args()
    global PAIRS
    PAIRS = a.pairs
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
