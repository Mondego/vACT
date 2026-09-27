#!/usr/bin/env python3
"""
RQ4 runner: agents with / without the Spec-Harness tool, then final scoring on held-out evaluation pairs.

Runs = 3 agents (claude, codex, veriact) x 2 benchmarks (sgb, fb = the C2 re-sample fb_120_fbdist) x arm
(with_harness | no_harness); one machine usually runs one arm (6 runs).

Suites (built by the test-set pipeline, T7):
  agents see   benchmarks/.../<sgb|fb_120>_v2_feedback.json   10 feedback pairs + feedback T- per task
  scoring uses benchmarks/.../<sgb|fb_120>_v2_eval.json       10 evaluation pairs + evaluation T- (disjoint)

Phases (each resumable; re-running skips finished tasks):
  scaffold   Claude Code / Codex task directories: agent_harness/scaffold.py on the feedback suite
             (--no-harness for the no_harness arm), attempt budget 10
  agents     claude / codex: run_agents.py in each scaffolded root (--only-missing), full logs kept
             (agent_run.log, codex_rollout.jsonl, tool_calls.jsonl, run_meta.json);
             veriact: veriact/run/run_single.py, one task per process (own output dir, resumable)
  score      every final Solution.java: OpenJML verification with the agents' verifier (RQ1 command,
             300 s); if verified, Spec-Harness on the evaluation suite (N = 10, dual decisions, 300 s,
             exclude_collection_tasks=False). Integrity failures count as failures. A file without an
             `ensures` clause on solve (D13) is not verified (verified=False, status no_postcondition) and
             scores 0; OpenJML's own result is kept as openjml_verified.
  status     per run: tasks, submissions, scored
  csv        <out>/rq4_per_task.csv

Layout: <out>/<arm>/<agent>_<bench>/ (agent work), <out>/<arm>/_scores/<agent>_<bench>/<task>.json

  python runners/rq4_run.py scaffold --out rq4_out --arm with_harness
  python runners/rq4_run.py agents   --out rq4_out --arm with_harness --parallel-runs 6 --agent-threads 4
  python runners/rq4_run.py score    --out rq4_out --arm with_harness --jvm-slots 60
  python runners/rq4_run.py csv      --out rq4_out
"""
from __future__ import annotations

import argparse
import csv
import glob
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

import common as C

B = os.path.join(C.REPO, "benchmarks")
FEEDBACK = {"sgb": f"{B}/specgenbench/sgb_v2_feedback.json", "fb": f"{B}/formalbench/fb_120_v2_feedback.json"}
EVAL = {"sgb": f"{B}/specgenbench/sgb_v2_eval.json", "fb": f"{B}/formalbench/fb_120_v2_eval.json"}
ARMS, AGENTS, BENCHES = ("with_harness", "no_harness"), ("claude", "codex", "veriact"), ("sgb", "fb")
MAX_ATTEMPTS = 10                 # harness + verify calls per task (VeriAct --max-steps 11 = 10 calls + submit)
METRICS = ("post_correctness", "post_completeness", "pre_correctness", "pre_completeness")

# models of the paper's RQ4 runs, passed explicitly so every machine runs the same configuration
CLAUDE_CMD = ("claude -p {{agents_md}} --dangerously-skip-permissions --model {model} --strict-mcp-config "
              "--mcp-config \"\" --tools Read,Edit,Write,Bash --disable-slash-commands --no-session-persistence "
              "--output-format stream-json --verbose")
CODEX_CMD = ("codex exec --json --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox "
             "-m {model} -c model_reasoning_effort={effort} {{agents_md}}")


def runs(args):
    for arm in args.arm:
        for agent in args.agents:
            for bench in args.bench:
                yield arm, agent, bench


def ids_file(args, root):
    """--task-ids as a file for scaffold.py / run_agents.py (None = all tasks)."""
    if not args.task_ids:
        return None
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, ".task_ids")
    open(path, "w").write("\n".join(args.task_ids) + "\n")
    return path


def run_dir(out, arm, agent, bench):
    return os.path.join(out, arm, f"{agent}_{bench}")


def agent_env(args):
    env = dict(os.environ)
    # the bundle's own packages first: an older `veriact` installed in the environment must never shadow it
    env["PYTHONPATH"] = C.REPO + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    # each agent process's harness tool may use this many OpenJML JVMs at once
    env["SPEC_HARNESS_JVM_SLOTS"] = str(max(2, args.jvm_slots // max(1, args.parallel_runs * args.agent_threads)))
    return env


# ------------------------------------------------------------------ scaffold

def cmd_scaffold(args) -> int:
    for arm, agent, bench in runs(args):
        if agent == "veriact":
            continue                                   # VeriAct needs no scaffold
        root = run_dir(args.out, arm, agent, bench)
        if os.path.isdir(root) and glob.glob(os.path.join(root, "*", "harness", "task.json")) and not args.force:
            C.log(f"scaffold {arm}/{agent}_{bench}: exists, skipped")
            continue
        cmd = [sys.executable, os.path.join(C.REPO, "agent_harness", "scaffold.py"), "--benchmark", FEEDBACK[bench],
               "--out-root", root, "--max-attempts", str(MAX_ATTEMPTS)] + (["--no-harness"] if arm == "no_harness" else [])
        f = ids_file(args, root)
        if f:
            cmd += ["--task-ids", f]
        C.log(f"scaffold {arm}/{agent}_{bench}: {' '.join(cmd)}")
        subprocess.run(cmd, check=True)
    return 0


# ------------------------------------------------------------------ agents

def run_cli_agent(arm, agent, bench, args):
    root = run_dir(args.out, arm, agent, bench)
    tmpl = (CLAUDE_CMD.format(model=args.claude_model) if agent == "claude"
            else CODEX_CMD.format(model=args.codex_model, effort=args.codex_effort))
    cmd = [sys.executable, os.path.join(root, "run_agents.py"), "--cmd", tmpl, "--threads", str(args.agent_threads),
           "--only-missing"] + (["--timeout", str(args.task_timeout)] if args.task_timeout else [])
    f = ids_file(args, root)
    if f:
        cmd += ["--task-ids", f]
    C.log(f"agents {arm}/{agent}_{bench}: {' '.join(shlex.quote(c) for c in cmd)}")
    with open(os.path.join(root, "rq4_agents.log"), "a") as fh:
        rc = subprocess.run(cmd, cwd=root, env=agent_env(args), stdout=fh, stderr=subprocess.STDOUT).returncode
    return f"{arm}/{agent}_{bench}", rc


def run_veriact(arm, bench, args):
    base = run_dir(args.out, arm, "veriact", bench)
    tasks = [t for t in json.load(open(FEEDBACK[bench])) if not args.task_ids or t["task_id"] in args.task_ids]
    env = agent_env(args)

    def one(t):
        tdir = os.path.join(base, "runs", t["task_id"])
        done = os.path.join(tdir, "done.json")
        if os.path.exists(done):
            return 0
        os.makedirs(tdir, exist_ok=True)
        bf = os.path.join(tdir, "task_benchmark.json")
        json.dump([t], open(bf, "w"))
        cmd = [sys.executable, "-m", "veriact.run.run_single", "--benchmark", bf,
               "--model", args.veriact_model, "--output-dir", tdir, "--openjml-path", args.openjml,
               "--max-steps", str(MAX_ATTEMPTS + 1)] + (["--no-harness"] if arm == "no_harness" else [])
        t0 = time.time()
        with open(os.path.join(tdir, "veriact_run.log"), "a") as fh:
            try:
                rc = subprocess.run(cmd, cwd=C.REPO, env=env, stdout=fh, stderr=subprocess.STDOUT,
                                    timeout=args.task_timeout or None).returncode
            except subprocess.TimeoutExpired:
                rc = "timeout"
        C.atomic_json(done, {"returncode": rc, "seconds": round(time.time() - t0, 1), "finished": C.now(),
                             "veriact_module": veriact_origin(env)})
        return rc

    with ThreadPoolExecutor(args.agent_threads) as pool:
        rcs = list(pool.map(one, tasks))
    return f"{arm}/veriact_{bench}", sum(1 for r in rcs if r not in (0, None))


def session_outcomes(out, arm, agent, bench):
    """Per-task outcome of the agent sessions: ok / failed (non-zero exit or timeout) / no_submission."""
    base = run_dir(out, arm, agent, bench)
    res = {}
    if agent == "veriact":
        for d in glob.glob(os.path.join(base, "runs", "*", "done.json")):
            j = json.load(open(d))
            res[os.path.basename(os.path.dirname(d))] = "ok" if j.get("returncode") == 0 else "failed"
    else:
        for m in glob.glob(os.path.join(base, "*", "harness", "run_meta.json")):
            j = json.load(open(m))
            tid = j.get("task_id") or os.path.basename(os.path.dirname(os.path.dirname(m)))
            res[tid] = ("failed" if j.get("returncode") not in (0,) or j.get("timed_out")
                        else "ok" if j.get("submission_exists") else "no_submission")
    return res


def veriact_origin(env) -> str:
    """The veriact package the agent process actually imports (must be the bundle's)."""
    r = subprocess.run([sys.executable, "-c", "import veriact; print(veriact.__file__)"], cwd=C.REPO, env=env,
                       capture_output=True, text=True)
    return (r.stdout or r.stderr).strip().splitlines()[-1] if (r.stdout or r.stderr) else ""


def cmd_agents(args) -> int:
    if "veriact" in args.agents:
        origin = veriact_origin(agent_env(args))
        if not origin.startswith(C.REPO):
            raise SystemExit(f"veriact resolves to {origin}, not this bundle ({C.REPO}); fix the environment")
        C.log(f"veriact module: {origin}")
    jobs = list(runs(args))
    C.atomic_json(os.path.join(args.out, f"agents_meta_{int(time.time())}.json"),
                  {"started": C.now(), "runs": jobs, "provenance": C.bundle_info(),
                   "models": {"claude": args.claude_model, "codex": f"{args.codex_model} ({args.codex_effort})",
                              "veriact": args.veriact_model}, "agent_threads": args.agent_threads,
                   "parallel_runs": args.parallel_runs, "task_timeout": args.task_timeout})
    with ThreadPoolExecutor(args.parallel_runs) as pool:
        futs = [pool.submit(run_veriact, arm, bench, args) if agent == "veriact"
                else pool.submit(run_cli_agent, arm, agent, bench, args) for arm, agent, bench in jobs]
        for f in as_completed(futs):
            name, rc = f.result()
            C.log(f"agents {name} finished")
    for arm, agent, bench in jobs:
        oc = session_outcomes(args.out, arm, agent, bench)
        cnt = {k: sum(1 for v in oc.values() if v == k) for k in ("ok", "no_submission", "failed")}
        bad = sorted(t for t, v in oc.items() if v == "failed")
        C.log(f"sessions {arm}/{agent}_{bench}: {cnt}" + (f" | FAILED: {bad[:10]} (see <task>/harness/agent_run.log; "
                                                             f"re-run the same command to retry)" if bad else ""))
    return 0


# ------------------------------------------------------------------ score

def final_solutions(out, arm, agent, bench):
    """task_id -> the final Solution.java of that run."""
    base = run_dir(out, arm, agent, bench)
    sols = {}
    if agent == "veriact":
        for p in glob.glob(os.path.join(base, "runs", "*", "**", "Solution.java"), recursive=True):
            tid = os.path.basename(os.path.dirname(p))
            if tid.startswith(("SGB_", "FB_")):
                sols[tid] = p
    else:
        for p in glob.glob(os.path.join(base, "*", "Solution.java")):
            sols[os.path.basename(os.path.dirname(p))] = p
    return sols


def load_verifier():
    spec = importlib.util.spec_from_file_location("verifier_tool", os.path.join(C.REPO, "veriact", "tools", "verifier_tool.py"))
    vt = importlib.util.module_from_spec(spec)
    sys.modules["verifier_tool"] = vt
    spec.loader.exec_module(vt)
    return vt


def cmd_score(args) -> int:
    import spec_harness.eval_llm_response as ev
    ev.set_jvm_slots(args.jvm_slots)
    vt = load_verifier()
    ev.install_stop_handlers(vt.kill_live_groups)   # Ctrl-C / kill / closed screen also stop OpenJML
    info = C.bundle_info()
    evals = {b: {t["task_id"]: t for t in json.load(open(EVAL[b]))} for b in args.bench}
    todo = []
    for arm, agent, bench in runs(args):
        sols = final_solutions(args.out, arm, agent, bench)
        for tid in sorted(evals[bench]):
            if args.task_ids and tid not in args.task_ids:
                continue
            res = os.path.join(args.out, arm, "_scores", f"{agent}_{bench}", f"{tid}.json")
            if os.path.exists(res) and not args.force:
                continue
            todo.append((arm, agent, bench, tid, sols.get(tid), res))
    C.log(f"RQ4 score: {len(todo)} tasks to score | JVM slots {args.jvm_slots} | {info}")
    prog = C.Progress(len(todo), os.path.join(args.out, "score_progress.log"))

    def work(item):
        arm, agent, bench, tid, sol, res = item
        lock = res + ".lock"
        if not C.try_lock(lock, args.stale_hours):
            return tid, "locked"
        rec = {"arm": arm, "agent": agent, "bench": bench, "task_id": tid, "solution": sol, "provenance": info,
               "pairs": args.pairs, "started": C.now()}
        work_dir = os.path.join(args.out, "work", arm, f"{agent}_{bench}", tid)
        try:
            if not sol:
                rec.update(status="no_solution", verified=False)
            else:
                code = open(sol).read()
                from spec_harness.eval_spec import extract_jml_spec
                sp = extract_jml_spec(code, "solve")
                # D13: no `ensures` on solve (incl. no JML at all, e.g. a failed session left the original
                # file) says nothing about the result: not verified and all Spec-Harness metrics 0. OpenJML is
                # still run and recorded as openjml_verified (RQ1's verification-only definition).
                no_post = sp.postcondition is None
                rec["no_postcondition"] = no_post
                t0 = time.time()
                ev.JVM_SLOTS.acquire(1)
                try:
                    vr = vt.verify_with_openjml(code, classname=evals[bench][tid].get("class_name") or "Solution",
                                                timeout=C.EXPERIMENT_TIMEOUT, output_dir=os.path.join(work_dir, "verify"))
                finally:
                    ev.JVM_SLOTS.release(1)
                rec.update(verify_seconds=round(time.time() - t0, 1), openjml_verified=bool(vr.success),
                           verified=bool(vr.success) and not no_post,
                           verify_timed_out="Timeout:" in (vr.error_log or ""))
                if no_post:
                    rec["status"] = "no_postcondition"
                elif not vr.success:
                    rec["status"] = "unverified"
                else:
                    t1 = time.time()
                    h = ev.evaluate_problem(task=ev.Task.from_dict(evals[bench][tid]), llm_code=code,
                                            openjml_path=args.openjml, output_dir=os.path.join(work_dir, "stubs"),
                                            verbose=False, max_pairs=args.pairs, exclude_collection_tasks=False)
                    rec["harness_seconds"] = round(time.time() - t1, 1)
                    rec["status"] = "scored" if h else "no_pairs_parsed"
                    if h:
                        for m in METRICS:
                            if isinstance(h.get(m), dict):
                                h[m] = {**h[m], "details": [{k: v for k, v in d.items() if k != "stub"}
                                                            for d in h[m].get("details", [])]}
                        rec["harness"] = h
        except Exception as e:
            rec.update(status="error", error=f"{type(e).__name__}: {e}", traceback=traceback.format_exc()[-3000:])
        finally:
            rec["finished"] = C.now()
            if not ev.STOPPING:                     # stopped mid-task: nothing saved, redone on resume
                C.atomic_json(res, rec)
            shutil.rmtree(work_dir, ignore_errors=True)
            C.unlock(lock)
        return tid, rec["status"]

    with ThreadPoolExecutor(args.workers) as pool:
        for f in as_completed([pool.submit(work, it) for it in todo]):
            tid, st = f.result()
            prog.tick(f"{tid}: {st}")
    return 0


# ------------------------------------------------------------------ status / csv

def cmd_status(args) -> int:
    for arm, agent, bench in runs(args):
        n = len(json.load(open(FEEDBACK[bench]))) if os.path.exists(FEEDBACK[bench]) else "?"
        subs = len(final_solutions(args.out, arm, agent, bench))
        scored = len(glob.glob(os.path.join(args.out, arm, "_scores", f"{agent}_{bench}", "*.json")))
        oc = session_outcomes(args.out, arm, agent, bench)
        cnt = {k: sum(1 for v in oc.values() if v == k) for k in ("ok", "no_submission", "failed")}
        print(f"{arm:13s} {agent:8s} {bench:4s} tasks {n} | sessions {cnt} | solutions {subs} | scored {scored}")
    return 0


def cmd_csv(args) -> int:
    rows = []
    for p in sorted(glob.glob(os.path.join(args.out, "*", "_scores", "*", "*.json"))):
        r = json.load(open(p))
        h = r.get("harness") or {}
        row = {k: r.get(k, "") for k in ("arm", "agent", "bench", "task_id", "status", "verified", "openjml_verified",
                                          "no_postcondition", "verify_timed_out",
                                          "verify_seconds", "harness_seconds")}
        row.update(integrity_failed=h.get("integrity_failed", False),
                   no_postcondition=h.get("no_postcondition", False), excluded=h.get("excluded", ""),
                   post_all_unknown=h.get("post_all_unknown", ""), not_evaluable=";".join(h.get("not_evaluable", [])))
        for m in METRICS:
            x = h.get(m) or {}
            for k in ("score", "passed", "total", "unknowns", "errors", "evaluable"):
                row[f"{m}_{k}"] = x.get(k, "")
            if x and x.get("evaluable") is False and not (h.get("integrity_failed") or h.get("no_postcondition")):
                row[f"{m}_score"] = ""                    # not evaluable: no score (not 0)
        row.update(host=(r.get("provenance") or {}).get("host", ""), bundle=(r.get("provenance") or {}).get("bundle", ""))
        rows.append(row)
    path = os.path.join(args.out, "rq4_per_task.csv")
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
    for name, fn in (("scaffold", cmd_scaffold), ("agents", cmd_agents), ("score", cmd_score),
                     ("status", cmd_status), ("csv", cmd_csv)):
        p = sub.add_parser(name)
        p.add_argument("--out", required=True)
        p.add_argument("--arm", nargs="+", default=list(ARMS), choices=ARMS)
        p.add_argument("--agents", nargs="+", default=list(AGENTS), choices=AGENTS)
        p.add_argument("--bench", nargs="+", default=list(BENCHES), choices=BENCHES)
        p.add_argument("--openjml", default=os.environ.get("OPENJML", "openjml"))
        p.add_argument("--jvm-slots", type=int, default=os.cpu_count() or 60)
        p.add_argument("--force", action="store_true")
        p.add_argument("--task-ids", nargs="*", default=None, help="restrict to these tasks (testing / partial reruns)")
        p.set_defaults(func=fn)
        if name == "agents":
            p.add_argument("--parallel-runs", type=int, default=6, help="runs (agent x bench) in parallel")
            p.add_argument("--agent-threads", type=int, default=4, help="tasks in parallel per run")
            p.add_argument("--task-timeout", type=int, default=0, help="seconds per agent session (0 = none)")
            p.add_argument("--claude-model", default="claude-sonnet-4-6")
            p.add_argument("--codex-model", default="gpt-5.4")
            p.add_argument("--codex-effort", default="medium")
            p.add_argument("--veriact-model", default="gpt-5.4")
        if name == "score":
            p.add_argument("--workers", type=int, default=0, help="tasks in flight (default: jvm-slots / 2)")
            p.add_argument("--pairs", type=int, default=0,
                           help="score the first N evaluation pairs (0 = all 10; T8 chooses N)")
            p.add_argument("--stale-hours", type=float, default=12.0)
    a = ap.parse_args()
    a.out = os.path.abspath(a.out)
    if a.cmd == "score" and not a.workers:
        a.workers = max(1, a.jvm_slots // 2)
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
