#!/usr/bin/env python3
"""
Preflight for a machine that will run RQ3 / RQ4. Run it before any long run; it exits non-zero on failure.

Checks
  python      >= 3.10, packages: javalang (harness), yaml (vACT)            [all]
  openjml     on PATH, version 21-0.21 (the RQ1 verifier)                      [all]
  suites      the v2 suite files exist and load                                [all]
  inputs      RQ1 output files present (inputs/rq1/*.jsonl, 136 files)          [--rq3]
  agents      `claude` and `codex` on PATH, codex model/effort, vACT key    [--rq4]
  resources   CPU count, free disk in --out
  tests       tests/test_harness.py (unit tests)
  smoke       the reference smoke specs are scored and every verdict must equal the verdict recorded
              on the reference machine (experiments/runners/reference/smoke_verdicts.json); a mismatch means this
              machine's OpenJML/solver setup differs and results would not be comparable

  python experiments/runners/preflight.py --rq3 --out /data/rq3_out
  python experiments/runners/preflight.py --rq4 --out /data/rq4_out
"""
from __future__ import annotations

import argparse
import glob
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

import common as C

REF = os.path.join(C.RUNNERS, "reference", "smoke_verdicts.json")
SMOKE = os.path.join(C.RUNNERS, "reference", "smoke_cases.json")
SUITE_FILES = ["specgenbench/sgb_v2_eval.json", "specgenbench/sgb_v2_feedback.json",
               "formalbench/fb_v2_eval.json", "formalbench/fb_120_v2_eval.json", "formalbench/fb_120_v2_feedback.json"]
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"[{'OK ' if ok else 'FAIL'}] {name}{': ' + detail if detail else ''}", flush=True)


def smoke_verdicts(out_dir: str) -> dict:
    """Score the reference smoke cases; return {case: {check label: verdict}}."""
    import spec_harness.eval_llm_response as ev
    cases = json.load(open(SMOKE))
    got = {}
    for c in cases:
        res = ev.evaluate_problem(task=ev.Task.from_dict(c["task"]), llm_code=c["code"], openjml_path="openjml",
                                  output_dir=os.path.join(out_dir, c["name"]), verbose=False, max_pairs=0,
                                  exclude_collection_tasks=False)
        v = {}
        for m in ("post_correctness", "post_completeness", "pre_correctness", "pre_completeness"):
            for d in (res.get(m) or {}).get("details", []):
                v[f"{m}|{d.get('label')}|{d.get('mutant', '')}"] = d.get("verdict")
        got[c["name"]] = v
    return got


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rq3", action="store_true")
    ap.add_argument("--rq4", action="store_true")
    ap.add_argument("--out", required=True, help="the output directory the run will use (disk check)")
    ap.add_argument("--rq1-dir", default=os.path.join(C.REPO, "inputs", "rq1"))
    ap.add_argument("--skip-smoke", action="store_true")
    ap.add_argument("--record-reference", action="store_true",
                    help="(reference machine only) write experiments/runners/reference/smoke_verdicts.json")
    a = ap.parse_args()

    check("python >= 3.10", sys.version_info >= (3, 10), sys.version.split()[0])
    for mod in ("javalang",) + (("yaml",) if a.rq4 else ()):
        try:
            importlib.import_module(mod)
            check(f"python package {mod}", True)
        except ImportError as e:
            check(f"python package {mod}", False, str(e))
    oj = shutil.which("openjml")
    ver = C.openjml_version() if oj else "not on PATH"
    check("openjml on PATH", bool(oj), oj or "")
    check(f"openjml version {C.OPENJML_VERSION}", ver == C.OPENJML_VERSION, ver)
    for rel in SUITE_FILES:
        p = os.path.join(C.REPO, "benchmarks", rel)
        try:
            n = len(json.load(open(p)))
            check(f"suite {rel}", n > 0, f"{n} tasks")
        except Exception as e:
            check(f"suite {rel}", False, str(e))
    if a.rq3:
        files = glob.glob(os.path.join(a.rq1_dir, "*.jsonl"))
        check("RQ1 inputs (136 files)", len(files) == 136, f"{len(files)} files in {a.rq1_dir}")
    if a.rq4:
        for exe in ("claude", "codex"):
            check(f"{exe} on PATH", bool(shutil.which(exe)), shutil.which(exe) or "")
        env_files = [os.path.join(C.REPO, ".env"), os.path.join(C.REPO, "config", ".env")]
        in_file = any(os.path.exists(f) and any(l.strip().startswith("OPENAI_API_KEY=") for l in open(f))
                      for f in env_files)
        check("vACT model key (OPENAI_API_KEY in the environment, <repo>/.env or config/.env)",
              bool(os.environ.get("OPENAI_API_KEY")) or in_file)
        env = {**os.environ, "PYTHONPATH": C.REPO + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else "")}
        r = subprocess.run([sys.executable, "-c", "import vact; print(vact.__file__)"], cwd=C.REPO, env=env,
                           capture_output=True, text=True)
        origin = (r.stdout or r.stderr).strip()
        check("vact resolves to this bundle (not an older installed copy)", origin.startswith(C.REPO), origin)
        for mod in ("openai", "dotenv", "dspy", "jinja2", "rich"):
            try:
                importlib.import_module(mod)
                check(f"python package {mod} (vACT)", True)
            except ImportError as e:
                check(f"python package {mod} (vACT)", False, str(e))
    cpus = os.cpu_count() or 0
    check("CPU count", cpus >= 16, str(cpus))
    os.makedirs(a.out, exist_ok=True)
    free = shutil.disk_usage(a.out).free / 2**30
    check("free disk in --out (>= 20 GB)", free >= 20, f"{free:.0f} GB")
    r = subprocess.run([sys.executable, os.path.join(C.REPO, "tests", "test_harness.py")], cwd=C.REPO,
                       capture_output=True, text=True, env={**os.environ, "PYTHONPATH": C.REPO})
    check("unit tests (tests/test_harness.py)", r.returncode == 0, (r.stdout.strip().splitlines() or [""])[-1])
    if not a.skip_smoke and oj:
        tmp = tempfile.mkdtemp(prefix="preflight_smoke_")
        got = smoke_verdicts(tmp)
        if a.record_reference:
            C.atomic_json(REF, {"provenance": C.bundle_info(), "verdicts": got})
            check("reference smoke verdicts recorded", True, REF)
        else:
            ref = json.load(open(REF))["verdicts"]
            diff = [(c, k, ref[c].get(k), got.get(c, {}).get(k)) for c in ref for k in ref[c]
                    if ref[c].get(k) != got.get(c, {}).get(k)]
            n = sum(len(v) for v in ref.values())
            check("smoke verdicts equal the reference machine", not diff,
                  f"{n - len(diff)}/{n} equal" + (f"; first differences: {diff[:3]}" if diff else ""))
        shutil.rmtree(tmp, ignore_errors=True)
    failed = [n for n, ok, _ in results if not ok]
    print("\nPREFLIGHT " + ("PASSED" if not failed else f"FAILED: {failed}"))
    C.atomic_json(os.path.join(a.out, "preflight.json"),
                  {"provenance": C.bundle_info(), "checks": results, "passed": not failed, "at": C.now()})
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
