#!/usr/bin/env python3
"""
RQ3 runner: re-score every verified RQ1 specification with the v2 test suite and the frozen Spec-Harness.

Work unit = one spec: a record of an RQ1 output file (<approach>__<bench>_run__<model>__<prompt>.jsonl) with
status == "verified" AND final_error == "" (a "verified" record with a non-empty final_error is an OpenJML
internal crash, not a verification). Every such record is scored, duplicates included.

Scoring (identical for every unit):
  suite     benchmarks/<bench dir>/<bench>_v2_eval.json: 10 evaluation pairs + the evaluation T- per task
  harness   spec_harness.eval_llm_response.evaluate_problem, dual accept/refute decisions (D8), OpenJML ESC
            with the RQ1 command, 300 s per decision, exclude_collection_tasks=False (D3)
  JVMs      at most --jvm-slots OpenJML JVMs at once (2 per decision), timers start at launch

Outputs (<out>/):
  results/<file stem>/<task>.json   one per unit: metadata + harness result (stub sources removed)
  stubs/<file stem>/<task>.tar.gz   the unit's stubs and OpenJML outputs (--keep-stubs tar)
  rq3_per_spec.csv                  `csv` subcommand (collect.py builds the merged CSVs)
  progress.log, run_meta.json

Resumable and shardable: units are claimed with lock files, finished units are skipped, and
--shard i/n splits the units deterministically between machines or sessions.

  python runners/rq3_run.py run --out rq3_out --bench sgb fb --jvm-slots 60
  python runners/rq3_run.py run --out rq3_out --shard 0/2     # this machine takes half
  python runners/rq3_run.py status --out rq3_out
  python runners/rq3_run.py csv --out rq3_out
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import shutil
import tarfile
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

import common as C

SUITES = {"sgb": os.path.join(C.REPO, "benchmarks", "specgenbench", "sgb_v2_eval.json"),
          "fb": os.path.join(C.REPO, "benchmarks", "formalbench", "fb_v2_eval.json")}
METRICS = ("post_correctness", "post_completeness", "pre_correctness", "pre_completeness")


def parse_stem(stem: str) -> dict:
    p = stem.split("__") + ["", "", ""]
    bench = p[1].replace("_run", "")
    return {"approach": p[0], "bench": bench, "model": p[2], "prompt": p[3].replace("two-shot", "two_shot")}


def units(rq1_dir: str, benches: list[str], file_glob: str | None, limit: int) -> list[dict]:
    out = []
    for f in sorted(glob.glob(os.path.join(rq1_dir, file_glob or "*.jsonl"))):
        stem = os.path.basename(f)[:-6]
        meta = parse_stem(stem)
        if meta["bench"] not in benches:
            continue
        n = 0
        for line in open(f):
            r = json.loads(line)
            if r.get("status") != "verified" or (r.get("final_error") or "") != "":
                continue
            out.append({"unit": f"{stem}/{r['task_id']}", "file": stem, "task_id": r["task_id"], **meta,
                        "final_code": r.get("final_code", "")})
            n += 1
            if limit and n >= limit:
                break
    return out


def strip_stubs(res: dict) -> dict:
    res = dict(res)
    for m in METRICS:
        if isinstance(res.get(m), dict):
            res[m] = {**res[m], "details": [{k: v for k, v in d.items() if k != "stub"}
                                            for d in res[m].get("details", [])]}
    return res


def score_unit(ev, suite: dict, u: dict, args, info: dict) -> dict:
    rec = {k: u[k] for k in ("unit", "file", "task_id", "approach", "bench", "model", "prompt")}
    rec.update(provenance=info, started=C.now(), suite=os.path.basename(SUITES[u["bench"]]))
    task = suite.get(u["task_id"])
    if task is None:
        rec["status"] = "task_not_in_suite"
        return rec
    work = os.path.join(args.out, "work", u["file"], u["task_id"])
    shutil.rmtree(work, ignore_errors=True)
    t0 = time.time()
    try:
        res = ev.evaluate_problem(task=ev.Task.from_dict(task), llm_code=u["final_code"],
                                  openjml_path=args.openjml, output_dir=work, verbose=False,
                                  max_pairs=0, exclude_collection_tasks=False)
        rec["seconds"] = round(time.time() - t0, 1)
        if not res:
            rec["status"] = "no_pairs_parsed"
        else:
            rec["status"] = "scored"
            rec["harness"] = strip_stubs(res)
    except Exception as e:
        rec.update(status="error", error=f"{type(e).__name__}: {e}", traceback=traceback.format_exc()[-3000:],
                   seconds=round(time.time() - t0, 1))
    if args.keep_stubs == "tar" and os.path.isdir(work):
        tgt = os.path.join(args.out, "stubs", u["file"], f"{u['task_id']}.tar.gz")
        os.makedirs(os.path.dirname(tgt), exist_ok=True)
        with tarfile.open(tgt, "w:gz") as tf:
            tf.add(work, arcname=u["task_id"])
    shutil.rmtree(work, ignore_errors=True)
    rec["finished"] = C.now()
    return rec


def result_path(out: str, u: dict) -> str:
    return os.path.join(out, "results", u["file"], f"{u['task_id']}.json")


def cmd_run(args) -> int:
    import spec_harness.eval_llm_response as ev
    ev.set_jvm_slots(args.jvm_slots)
    args.out = os.path.abspath(args.out)
    os.makedirs(args.out, exist_ok=True)
    i, n = C.parse_shard(args.shard)
    all_units = units(args.rq1_dir, args.bench, args.files, args.limit)
    mine = [u for u in all_units if C.shard_of(u["unit"], n) == i]
    todo = []
    for u in mine:
        p = result_path(args.out, u)
        if os.path.exists(p) and not args.force:
            if not (args.retry_errors and json.load(open(p)).get("status") == "error"):
                continue
        todo.append(u)
    suites = {b: {t["task_id"]: t for t in json.load(open(SUITES[b]))} for b in args.bench}
    info = C.bundle_info()
    C.atomic_json(os.path.join(args.out, "run_meta.json"),
                  {"rq": "RQ3", "args": {k: v for k, v in vars(args).items() if k != "func"}, "provenance": info,
                   "units_total": len(all_units), "units_shard": len(mine), "units_todo": len(todo), "started": C.now(),
                   "suites": {b: SUITES[b] for b in args.bench}})
    C.log(f"RQ3: {len(all_units)} units, shard {i}/{n}: {len(mine)}, to do {len(todo)} | workers {args.workers}, "
          f"JVM slots {args.jvm_slots} | {info}")
    prog = C.Progress(len(todo), os.path.join(args.out, "progress.log"))

    def work(u):
        lock = os.path.join(args.out, "locks", u["file"], f"{u['task_id']}.lock")
        if not C.try_lock(lock, args.stale_hours):
            return u, "locked"
        try:
            rec = score_unit(ev, suites[u["bench"]], u, args, info)
            C.atomic_json(result_path(args.out, u), rec)
            return u, rec["status"] + (f" {rec.get('seconds')}s" if "seconds" in rec else "")
        finally:
            C.unlock(lock)

    with ThreadPoolExecutor(args.workers) as pool:
        futs = [pool.submit(work, u) for u in todo]
        for f in as_completed(futs):
            u, st = f.result()
            prog.tick(f"{u['unit']}: {st}")
    C.log("RQ3 run finished")
    return 0


def cmd_status(args) -> int:
    all_units = units(args.rq1_dir, args.bench, args.files, 0)
    done = err = 0
    for u in all_units:
        p = result_path(args.out, u)
        if os.path.exists(p):
            done += 1
            err += json.load(open(p)).get("status") == "error"
    print(f"units {len(all_units)} | results {done} ({err} errors) | missing {len(all_units) - done}")
    return 0


def rows_from_results(out: str):
    for p in sorted(glob.glob(os.path.join(out, "results", "*", "*.json"))):
        r = json.load(open(p))
        h = r.get("harness") or {}
        row = {k: r.get(k, "") for k in ("unit", "file", "approach", "bench", "model", "prompt", "task_id", "status")}
        row.update(excluded=h.get("excluded", ""), exclude_reason=h.get("exclude_reason", ""),
                   integrity_failed=h.get("integrity_failed", False), post_all_unknown=h.get("post_all_unknown", ""),
                   not_evaluable=";".join(h.get("not_evaluable", [])), pairs_used=h.get("pairs_used", ""),
                   invalid_inputs_used=h.get("invalid_inputs_used", ""),
                   invalid_inputs_source=h.get("invalid_inputs_source", ""))
        for m in METRICS:
            x = h.get(m) or {}
            for k in ("score", "passed", "total", "unknowns", "errors", "evaluable"):
                row[f"{m}_{k}"] = x.get(k, "")
            if x and x.get("evaluable") is False and not h.get("integrity_failed"):
                row[f"{m}_score"] = ""                    # not evaluable: no score (not 0)
        row.update(seconds=r.get("seconds", ""), host=(r.get("provenance") or {}).get("host", ""),
                   bundle=(r.get("provenance") or {}).get("bundle", ""), error=r.get("error", ""))
        yield row


def cmd_csv(args) -> int:
    rows = list(rows_from_results(args.out))
    path = os.path.join(args.out, "rq3_per_spec.csv")
    if rows:
        with open(path, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    print(f"{len(rows)} rows -> {path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("run", cmd_run), ("status", cmd_status), ("csv", cmd_csv)):
        p = sub.add_parser(name)
        p.add_argument("--out", required=True)
        p.add_argument("--rq1-dir", default=os.path.join(C.REPO, "inputs", "rq1"),
                       help="RQ1 output files (*.jsonl); the bundle ships them in inputs/rq1")
        p.add_argument("--bench", nargs="+", default=["sgb", "fb"], choices=["sgb", "fb"])
        p.add_argument("--files", default=None, help="glob restricting the RQ1 files, e.g. 'specgen__*'")
        p.set_defaults(func=fn)
        if name == "run":
            p.add_argument("--shard", default=None, help="i/n: this process takes units with hash %% n == i")
            p.add_argument("--jvm-slots", type=int, default=os.cpu_count() or 60,
                           help="max concurrent OpenJML JVMs (default: CPU count)")
            p.add_argument("--workers", type=int, default=0, help="specs in flight (default: jvm-slots / 2)")
            p.add_argument("--openjml", default=os.environ.get("OPENJML", "openjml"))
            p.add_argument("--limit", type=int, default=0, help="first N units per file (smoke tests)")
            p.add_argument("--keep-stubs", choices=["tar", "none"], default="tar")
            p.add_argument("--force", action="store_true")
            p.add_argument("--retry-errors", action="store_true")
            p.add_argument("--stale-hours", type=float, default=12.0)
            p.add_argument("--suite", action="append", default=[], metavar="BENCH=PATH",
                           help="override a suite file (testing only), e.g. sgb=/tmp/sgb_v2_eval.json")
    a = ap.parse_args()
    for spec in getattr(a, "suite", []) or []:
        b, path = spec.split("=", 1)
        SUITES[b] = os.path.abspath(path)
    if a.cmd == "run" and not a.workers:
        a.workers = max(1, a.jvm_slots // 2)
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
