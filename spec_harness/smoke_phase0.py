#!/usr/bin/env python3
"""
OpenJML smoke test for the Phase-0 Spec-Harness fixes, on real RQ4 specs.

Each case exercises one fixed bug. The script scores the case with the fixed
harness (5 pairs, like the submitted evaluation), prints the four metrics and the
ERROR count per metric next to the originally reported in-loop scores, and keeps
every generated stub under <output>/stubs for inspection.

Usage (from the repository root of this worktree):
    python spec_harness/smoke_phase0.py --openjml /path/to/openjml --output smoke_phase0

What to look for:
  * no case should show ERROR counts except where noted;
  * DivisorGame (requires n >= 1, and n = -5 is a valid input): with the D1
    guard semantics (psi => phi) the n = -5 pair passes PostCorr and kills no
    mutant by design; pairs with n >= 1 must kill their mutants, so PostComp
    should be > 0 (it was 0.00 in the submitted evaluation); PreCorr < 1
    flags the narrow precondition;
  * BinarySearch / Biggest / MinInArray: quantified requires and `also` specs
    should score without ERRORs;
  * SumOfSubarrayProd / PrimeNumbers: the spec of solve(), not of the helper,
    should be scored, and stubs should contain the helper method;
  * PreComp on array tasks: null inputs should count as rejected (NullFormal).
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import spec_harness.eval_llm_response as ev  # noqa: E402

PROJECT = os.path.dirname(ROOT)
RQ4 = os.path.join(PROJECT, "artifacts", "rq4")

CASES = [
    ("claude", "sgb", "no",   "SGB_DivisorGame",       "normal_behavior + requires excluding a valid input (D1 guard)"),
    ("claude", "sgb", "no",   "SGB_BinarySearch",      "quantified requires"),
    ("claude", "sgb", "no",   "SGB_Biggest",           "multi-case spec (also)"),
    ("claude", "sgb", "with", "SGB_MinInArray",        "also + array parameter (null input -> NullFormal)"),
    ("claude", "fb",  "with", "FB_SumOfSubarrayProd",  "helper method before solve()"),
    ("codex",  "sgb", "no",   "SGB_PrimeNumbers",      "benchmark itself has a helper before solve()"),
    ("claude", "sgb", "no",   "SGB_Abs",               "baseline; equivalent-mutant fix (0.957 -> 1.0 expected)"),
    ("claude", "sgb", "with", "SGB_SelectionSort",     "void method, in-place array (augmented outputs)"),
]


# D3 probes: collection-typed tasks with hand-written, correct, element-level
# specs. A correct spec must pass PostCorr (1.0) on the true outputs; if it fails,
# OpenJML cannot see the pinned collection contents and D3 should exclude these tasks.
COLLECTION_PROBES = [
    ("FB_AndTuples", "List params + List return",
     "    //@ requires testTup1 != null && testTup2 != null && testTup1.size() == testTup2.size();\n"
     "    //@ ensures \\result.size() == testTup1.size();\n"
     "    //@ ensures (\\forall int i; 0 <= i && i < testTup1.size(); "
     "\\result.get(i).intValue() == (testTup1.get(i).intValue() & testTup2.get(i).intValue()));\n"),
    ("FB_Answer", "int params + List return",
     "    //@ ensures (\\forall int i; 0 <= i && i < \\result.size(); \\result.get(i).intValue() == l * (i + 1));\n"
     "    //@ ensures (\\forall int i; 0 <= i && i < \\result.size(); \\result.get(i).intValue() < r);\n"),
    ("FB_CharFrequency", "String param + HashMap return",
     "    //@ ensures \\result != null && \\result.size() <= str1.length();\n"
     "    //@ ensures (\\forall int i; 0 <= i && i < str1.length(); \\result.containsKey(String.valueOf(str1.charAt(i))));\n"),
    ("FB_AddDict", "HashMap params + HashMap return",
     "    //@ requires d1 != null && d2 != null;\n"
     "    //@ ensures (\\forall String k; d1.containsKey(k) || d2.containsKey(k); \\result.containsKey(k));\n"
     "    //@ ensures \\result.size() <= d1.size() + d2.size();\n"),
]


def task_dir(agent, bench, cond, tid):
    for d in glob.glob(os.path.join(RQ4, f"agent_{cond}_harness", f"{agent}_run", "*")):
        name = os.path.basename(d).replace("veriact__", "")
        if name.startswith(bench + "_"):
            root = os.path.join(d, "workspaces") if os.path.isdir(os.path.join(d, "workspaces")) else d
            return os.path.join(root, tid)
    return None


def original_scores(agent, bench, cond, tid):
    p = os.path.join(RQ4, "_scores", f"{agent}_{bench}_scores_{cond}_harness.csv")
    for r in csv.DictReader(open(p)):
        if r["task_id"] == tid:
            return r
    return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--openjml", default=os.environ.get("OPENJML", "openjml"))
    ap.add_argument("--output", default="smoke_phase0")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--max-pairs", type=int, default=5)
    a = ap.parse_args()
    if os.sep in a.openjml:
        os.environ["PATH"] = os.path.dirname(os.path.abspath(a.openjml)) + os.pathsep + os.environ["PATH"]
    bench = {}
    for p in ["benchmarks/specgenbench/sgb_aug.json", "benchmarks/formalbench/fb_120.json"]:
        for t in json.load(open(os.path.join(ROOT, p))):
            bench[t["task_id"]] = t
    os.makedirs(a.output, exist_ok=True)

    def run(case):
        agent, b, cond, tid, why = case
        d = task_dir(agent, b, cond, tid)
        code = open(os.path.join(d, "Solution.java")).read()
        res = ev.evaluate_problem(ev.Task.from_dict(bench[tid]), code, openjml_path="openjml",
                                  output_dir=os.path.join(a.output, f"{agent}_{b}_{cond}"),
                                  max_pairs=a.max_pairs)
        json.dump(res, open(os.path.join(a.output, f"{agent}_{b}_{cond}_{tid}.json"), "w"), indent=1)
        return case, res

    fb_full = {t["task_id"]: t for t in json.load(open(os.path.join(ROOT, "benchmarks/formalbench/fb.json")))}

    def run_probe(probe):
        tid, why, spec = probe
        t = fb_full[tid]
        code = t["code"].replace("    public static", spec + "    public static", 1)
        res = ev.evaluate_problem(ev.Task.from_dict(t), code, openjml_path="openjml",
                                  output_dir=os.path.join(a.output, "d3_probes"),
                                  max_pairs=a.max_pairs, exclude_collection_tasks=False)
        json.dump(res, open(os.path.join(a.output, f"d3_{tid}.json"), "w"), indent=1)
        return ("probe", "fb", "d3", tid, why), res

    with ThreadPoolExecutor(max_workers=a.threads) as pool:
        futs = [pool.submit(run, c) for c in CASES] + [pool.submit(run_probe, p) for p in COLLECTION_PROBES]
        results = [f.result() for f in futs]

    print(f"\n{'case':44s} {'metric':18s} {'new':>6s} {'n':>4s} {'unk':>4s} {'err':>4s} {'orig':>6s}")
    for (agent, b, cond, tid, why), res in results:
        o = original_scores(agent, b, cond, tid) if agent != "probe" else {}
        print(f"\n{agent}_{b}_{cond}/{tid}  --  {why}")
        if res.get("integrity_failed") or res.get("excluded"):
            print(f"   {'':44s} integrity_failed={res.get('integrity_failed')} excluded={res.get('excluded')} "
                  f"reason={res.get('exclude_reason')}")
        for m in ("post_correctness", "post_completeness", "pre_correctness", "pre_completeness"):
            r = res.get(m, {})
            orig = o.get(m, "")
            orig = f"{float(orig):.3f}" if orig not in ("", None) else "-"
            print(f"   {'':40s} {m:18s} {r.get('score', 0):6.3f} {r.get('total', 0):4d} "
                  f"{r.get('unknowns', 0):4d} {r.get('errors', 0):4d} {orig:>6s}")
    print(f"\nPer-case JSON and stubs are in {os.path.abspath(a.output)}")


if __name__ == "__main__":
    main()
