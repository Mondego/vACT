#!/usr/bin/env python3
"""
Build the preflight reference (run once, on the reference machine, after the v2 suites exist):
  experiments/runners/reference/smoke_cases.json     3 real verified RQ1 specs + their v2 evaluation tasks
  experiments/runners/reference/smoke_verdicts.json  every check's verdict for them on this machine

The cases are simple, quantifier-free specs, so their verdicts do not depend on timing. The smoke is scored
twice here and the reference is written only if both runs agree exactly; on another machine, preflight.py
scores the same cases and requires identical verdicts (same OpenJML, solvers and harness).

  python experiments/runners/make_reference.py --rq1-dir ../artifacts/rq1
"""
import argparse
import json
import os
import shutil
import tempfile

import common as C
import preflight

CASES = [("specgen__sgb_run__claude-sonnet-4-6__zero_shot", "SGB_Abs", "sgb"),
         ("specgen__sgb_run__claude-sonnet-4-6__zero_shot", "SGB_IntCube", "sgb"),
         ("specgen__fb_run__claude-sonnet-4-6__zero_shot", "FB_FrontAndRear", "fb")]
SUITE = {"sgb": os.path.join(C.REPO, "benchmarks", "specgenbench", "sgb_v2_eval.json"),
         "fb": os.path.join(C.REPO, "benchmarks", "formalbench", "fb_v2_eval.json")}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rq1-dir", required=True)
    a = ap.parse_args()
    suites = {b: {t["task_id"]: t for t in json.load(open(p))} for b, p in SUITE.items()}
    cases = []
    for stem, tid, bench in CASES:
        rec = next(json.loads(l) for l in open(os.path.join(a.rq1_dir, stem + ".jsonl"))
                   if json.loads(l)["task_id"] == tid)
        assert rec["status"] == "verified" and not rec.get("final_error"), (stem, tid)
        cases.append({"name": f"{stem}/{tid}", "task": suites[bench][tid], "code": rec["final_code"]})
    os.makedirs(os.path.dirname(preflight.SMOKE), exist_ok=True)
    C.atomic_json(preflight.SMOKE, cases)
    runs = []
    for i in range(2):
        tmp = tempfile.mkdtemp(prefix=f"reference_{i}_")
        runs.append(preflight.smoke_verdicts(tmp))
        shutil.rmtree(tmp, ignore_errors=True)
    n = sum(len(v) for v in runs[0].values())
    same = runs[0] == runs[1]
    unknown = sum(1 for v in runs[0].values() for x in v.values() if x not in ("accept", "reject"))
    print(f"{len(cases)} cases, {n} checks, runs identical: {same}, undecided checks: {unknown}")
    for c, v in runs[0].items():
        und = {k: x for k, x in v.items() if x not in ("accept", "reject")}
        diff = {k: (x, runs[1][c].get(k)) for k, x in v.items() if runs[1][c].get(k) != x}
        print(f"  {c}: {len(v)} checks, undecided {len(und)} {list(und.items())[:4]}, differing {list(diff.items())[:4]}")
    if not same or unknown:
        raise SystemExit("reference NOT written: verdicts must be identical across runs and all decided")
    C.atomic_json(preflight.REF, {"provenance": C.bundle_info(), "verdicts": runs[0], "recorded": C.now()})
    print(f"reference -> {preflight.REF}")


if __name__ == "__main__":
    main()
