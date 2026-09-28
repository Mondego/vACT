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
import collections
import csv
import glob
import hashlib
import importlib.util
import json
import os
import shlex
import shutil
import signal
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
MAX_ATTEMPTS = 10                 # verify + harness tool calls per task, every agent (VeriAct --max-tool-calls)
METRICS = ("post_correctness", "post_completeness", "pre_correctness", "pre_completeness")

# models of the paper's RQ4 runs, passed explicitly so every machine runs the same configuration
# Pinned configuration, identical on every machine (no user settings / memory / skills / project docs):
#   claude: --setting-sources project,local (the user's ~/.claude settings and CLAUDE.md are not loaded; the task
#           dirs have none), --effort medium; auto-memory, telemetry and non-essential traffic off and the Bash
#           tool timeout = the tool timeout via the environment (agent_env)
#   codex:  a clean CODEX_HOME holding only auth.json (no config, skills, memories), --ignore-user-config,
#           project_doc_max_bytes=0 (the task's AGENTS.md is the prompt, not also a project doc), the pilot's
#           personality, reasoning effort medium
CLAUDE_CMD = ("claude -p {{agents_md}} --dangerously-skip-permissions --model {model} --effort {effort} "
              "--setting-sources project,local --strict-mcp-config --mcp-config \"\" --tools Read,Edit,Write,Bash "
              "--disable-slash-commands --no-session-persistence --output-format stream-json --verbose")
CODEX_CMD = ("codex exec --json --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox --ignore-user-config "
             "-m {model} -c model_reasoning_effort={effort} -c project_doc_max_bytes=0 -c personality=pragmatic "
             "{{agents_md}}")


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


def session_slots(args) -> int:
    """OpenJML JVM slots of one agent session's tools: the machine's slots split over the concurrent sessions.
    A harness call (5 feedback pairs + T-) is ~40 decisions; with >= 10 slots it takes a few minutes."""
    return max(2, args.jvm_slots // max(1, args.parallel_runs * args.agent_threads))


def agent_env(args, agent: str):
    env = {k: v for k, v in os.environ.items() if k not in ("AGENT_MAX_ATTEMPTS", "PYTHONPATH")}
    if agent == "veriact":
        # the bundle's own packages first: an older installed `veriact` must never shadow it; the CLI agents
        # get no PYTHONPATH (the repo, with the evaluation suites, is not theirs to import)
        env["PYTHONPATH"] = C.REPO
    env["SPEC_HARNESS_JVM_SLOTS"] = str(session_slots(args))
    env["OPENJML"] = args.openjml
    oj_dir = os.path.dirname(shutil.which(args.openjml) or args.openjml)
    if oj_dir:
        env["PATH"] = oj_dir + os.pathsep + env.get("PATH", "")
    if agent == "claude":
        env.update(CLAUDE_CODE_DISABLE_AUTO_MEMORY="1", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
                   DISABLE_TELEMETRY="1", DISABLE_ERROR_REPORTING="1",
                   BASH_DEFAULT_TIMEOUT_MS=str(args.tool_timeout * 1000),
                   BASH_MAX_TIMEOUT_MS=str(args.tool_timeout * 1000))
    if agent == "codex":
        env["CODEX_HOME"] = codex_home(args)
    return env


def codex_home(args) -> str:
    """A clean CODEX_HOME with only the login (auth.json), so no user config, skills or memories load."""
    home = os.path.join(args.out, ".codex_home")
    os.makedirs(home, exist_ok=True)
    src = os.path.join(os.environ.get("CODEX_HOME", os.path.expanduser("~/.codex")), "auth.json")
    dst = os.path.join(home, "auth.json")
    if not os.path.exists(dst):
        if not os.path.exists(src):
            raise SystemExit(f"codex login not found ({src}): run `codex login --api-key` first")
        shutil.copy2(src, dst)
        os.chmod(dst, 0o600)
    return home


# ------------------------------------------------------------------ scaffold

def cmd_scaffold(args) -> int:
    for arm, agent, bench in runs(args):
        if agent == "veriact":
            continue                                   # VeriAct needs no scaffold
        root = run_dir(args.out, arm, agent, bench)
        wanted = [t["task_id"] for t in json.load(open(FEEDBACK[bench]))
                  if not args.task_ids or t["task_id"] in args.task_ids]
        missing = [t for t in wanted if args.force or not os.path.exists(os.path.join(root, t, "harness", "task.json"))]
        if not missing:
            C.log(f"scaffold {arm}/{agent}_{bench}: all {len(wanted)} tasks present, skipped")
            continue
        cmd = [sys.executable, os.path.join(C.REPO, "agent_harness", "scaffold.py"), "--benchmark", FEEDBACK[bench],
               "--out-root", root, "--max-attempts", str(MAX_ATTEMPTS)] + (["--no-harness"] if arm == "no_harness" else [])
        os.makedirs(root, exist_ok=True)
        ids_path = os.path.join(root, ".scaffold_task_ids")
        open(ids_path, "w").write("\n".join(missing) + "\n")
        cmd += ["--task-ids", ids_path]                  # exactly the tasks not scaffolded yet
        C.log(f"scaffold {arm}/{agent}_{bench}: {' '.join(cmd)}")
        subprocess.run(cmd, check=True)
    return 0


# ------------------------------------------------------------------ agents

def run_cli_agent(arm, agent, bench, args):
    root = run_dir(args.out, arm, agent, bench)
    tmpl = (CLAUDE_CMD.format(model=args.claude_model, effort=args.claude_effort) if agent == "claude"
            else CODEX_CMD.format(model=args.codex_model, effort=args.codex_effort))
    cmd = [sys.executable, os.path.join(root, "run_agents.py"), "--cmd", tmpl, "--threads", str(args.agent_threads),
           "--only-missing"] + (["--timeout", str(args.task_timeout)] if args.task_timeout else [])
    f = ids_file(args, root)
    if f:
        cmd += ["--task-ids", f]
    C.log(f"agents {arm}/{agent}_{bench}: {' '.join(shlex.quote(c) for c in cmd)}")
    with open(os.path.join(root, "rq4_agents.log"), "a") as fh:
        rc = subprocess.run(cmd, cwd=root, env=agent_env(args, agent), stdout=fh, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL).returncode
    return f"{arm}/{agent}_{bench}", rc


def run_veriact(arm, bench, args):
    base = run_dir(args.out, arm, "veriact", bench)
    tasks = [t for t in json.load(open(FEEDBACK[bench])) if not args.task_ids or t["task_id"] in args.task_ids]
    env = agent_env(args, "veriact")

    def one(t):
        tdir = os.path.join(base, "runs", t["task_id"])
        done = os.path.join(tdir, "done.json")
        if os.path.exists(done):
            if json.load(open(done)).get("returncode") == 0:
                return 0                            # finished normally: never re-run
            # an infrastructure failure (crash, API error, timeout): retry from a fresh state, keeping the
            # failed attempt for the record
            n = len(glob.glob(os.path.join(tdir, "attempt_*")))
            keep = os.path.join(tdir, f"attempt_{n + 1}")
            os.makedirs(keep)
            for x in os.listdir(tdir):
                if not x.startswith("attempt_"):
                    shutil.move(os.path.join(tdir, x), os.path.join(keep, x))
        elif os.path.isdir(tdir) and glob.glob(os.path.join(tdir, "veriact__*")):
            # an interrupted session (no done.json): same, a fresh start
            n = len(glob.glob(os.path.join(tdir, "attempt_*")))
            keep = os.path.join(tdir, f"attempt_{n + 1}")
            os.makedirs(keep)
            for x in os.listdir(tdir):
                if not x.startswith("attempt_"):
                    shutil.move(os.path.join(tdir, x), os.path.join(keep, x))
        os.makedirs(tdir, exist_ok=True)
        bf = os.path.join(tdir, "task_benchmark.json")
        json.dump([t], open(bf, "w"))
        cmd = [sys.executable, "-m", "veriact.run.run_single", "--benchmark", bf,
               "--model", args.veriact_model, "--output-dir", tdir, "--openjml-path", args.openjml,
               "--max-steps", "20", "--max-tool-calls", str(MAX_ATTEMPTS),
               "--reasoning-effort", args.veriact_effort, "--tool-timeout", str(args.tool_timeout),
               "--harness-max-pairs", "5"] + \
            (["--no-harness"] if arm == "no_harness" else [])
        t0 = time.time()
        with open(os.path.join(tdir, "veriact_run.log"), "a") as fh:
            # own session, so a session timeout can stop the whole tree: SIGTERM first (run_single then kills
            # its tool call and that call's OpenJML processes), SIGKILL after 30 s
            p = subprocess.Popen(cmd, cwd=C.REPO, env=env, stdout=fh, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, start_new_session=True)
            try:
                rc = p.wait(timeout=args.task_timeout or None)
            except subprocess.TimeoutExpired:
                rc = "timeout"
                for sig, wait in ((signal.SIGTERM, 30), (signal.SIGKILL, 10)):
                    try:
                        os.killpg(p.pid, sig)
                    except ProcessLookupError:
                        break
                    try:
                        p.wait(timeout=wait)
                        break
                    except subprocess.TimeoutExpired:
                        continue
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
        origin = veriact_origin(agent_env(args, "veriact"))
        if not origin.startswith(C.REPO):
            raise SystemExit(f"veriact resolves to {origin}, not this bundle ({C.REPO}); fix the environment")
        C.log(f"veriact module: {origin}")
    jobs = list(runs(args))
    C.atomic_json(os.path.join(args.out, f"agents_meta_{int(time.time())}.json"),
                  {"started": C.now(), "runs": jobs, "provenance": C.bundle_info(),
                   "models": {"claude": f"{args.claude_model} ({args.claude_effort})",
                              "codex": f"{args.codex_model} ({args.codex_effort})",
                              "veriact": f"{args.veriact_model} ({args.veriact_effort})"},
                   "agent_threads": args.agent_threads, "parallel_runs": args.parallel_runs,
                   "session_jvm_slots": session_slots(args), "task_timeout": args.task_timeout,
                   "tool_timeout": args.tool_timeout, "max_tool_calls": MAX_ATTEMPTS,
                   "feedback_pairs": 5, "commands": {"claude": CLAUDE_CMD, "codex": CODEX_CMD}})
    if session_slots(args) < 10:
        C.log(f"WARNING: only {session_slots(args)} OpenJML slots per agent session (--jvm-slots {args.jvm_slots} / "
              f"{args.parallel_runs * args.agent_threads} sessions); harness calls will be slow. Use fewer "
              f"--parallel-runs/--agent-threads or more --jvm-slots (>= 10 per session).")
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

def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def session_state(out, arm, agent, bench, tid) -> str:
    """finished | failed | not_run: whether the agent session of a task has ended normally (only finished
    sessions are scored; failed ones are retried by the `agents` phase)."""
    base = run_dir(out, arm, agent, bench)
    if agent == "veriact":
        done = os.path.join(base, "runs", tid, "done.json")
        if not os.path.exists(done):
            return "not_run"
        return "finished" if json.load(open(done)).get("returncode") == 0 else "failed"
    meta = os.path.join(base, tid, "harness", "run_meta.json")
    if not os.path.exists(meta):
        return "not_run"
    j = json.load(open(meta))
    return ("failed" if j.get("returncode") not in (0,) or j.get("timed_out") or j.get("interrupted")
            else "finished")


def final_code(out, arm, agent, bench, tid, original: str):
    """(code, source): the task's final specification = the code of the agent's last ACCEPTED tool call,
    for every agent; the original program if no call was ever accepted (then: no postcondition, 0).
      veriact      the newest workspace's Solution.java (VeriAct writes it only for accepted calls)
      claude/codex harness/last_valid_Solution.java, accepted only if its sha256 equals the code_sha256 of
                   the last accepted call in harness/tool_calls.jsonl (written by the tool, not the agent)"""
    base = run_dir(out, arm, agent, bench)
    if agent == "veriact":
        cands = sorted(glob.glob(os.path.join(base, "runs", tid, "veriact__*", "workspaces", tid, "Solution.java")))
        if cands:
            return open(cands[-1]).read(), "veriact_workspace"
        return original, "original"
    tdir = os.path.join(base, tid)
    last = os.path.join(tdir, "harness", "last_valid_Solution.java")
    log = os.path.join(tdir, "harness", "tool_calls.jsonl")
    accepted = []
    if os.path.exists(log):
        for line in open(log):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("accepted") and e.get("code_sha256") and e.get("tool") in ("verify", "harness"):
                accepted.append(e["code_sha256"])
    if accepted and os.path.exists(last):
        code = open(last).read()
        if _sha(code) == accepted[-1]:
            return code, "last_accepted"
        return original, "tampered_last_valid"
    return original, "original"


def load_verifier():
    spec = importlib.util.spec_from_file_location("verifier_tool", os.path.join(C.REPO, "veriact", "tools", "verifier_tool.py"))
    vt = importlib.util.module_from_spec(spec)
    sys.modules["verifier_tool"] = vt
    spec.loader.exec_module(vt)
    return vt


def cmd_score(args) -> int:
    import spec_harness.eval_llm_response as ev
    from spec_harness.eval_spec import extract_jml_spec
    from spec_harness.harness import JavaMethodParser, check_integrity
    ev.set_jvm_slots(args.jvm_slots)
    vt = load_verifier()
    ev.install_stop_handlers(vt.kill_live_groups)   # Ctrl-C / kill / closed screen also stop OpenJML
    info = C.bundle_info()
    meta_p = os.path.join(args.out, "score_meta.json")
    if os.path.exists(meta_p) and json.load(open(meta_p)).get("pairs") != args.pairs:
        C.log(f"ERROR: {args.out} was scored with --pairs {json.load(open(meta_p)).get('pairs')}, not {args.pairs}")
        return 2
    C.atomic_json(meta_p, {"pairs": args.pairs, "provenance": info, "at": C.now()})
    evals = {b: {t["task_id"]: t for t in json.load(open(EVAL[b]))} for b in args.bench}
    todo, waiting = [], collections.Counter()
    for arm, agent, bench in runs(args):
        for tid in sorted(evals[bench]):
            if args.task_ids and tid not in args.task_ids:
                continue
            state = session_state(args.out, arm, agent, bench, tid)
            if state != "finished":
                waiting[f"{arm}/{agent}_{bench}:{state}"] += 1   # never scored before its session ended
                continue
            code, source = final_code(args.out, arm, agent, bench, tid, evals[bench][tid]["code"])
            res = os.path.join(args.out, arm, "_scores", f"{agent}_{bench}", f"{tid}.json")
            if os.path.exists(res) and not args.force:
                old = json.load(open(res))
                if old.get("final_sha256") == _sha(code) and not (args.retry_errors and old.get("status") == "error"):
                    continue
            todo.append((arm, agent, bench, tid, code, source, res))
    C.log(f"RQ4 score: {len(todo)} tasks to score | sessions not finished: {dict(waiting) or 'none'} | "
          f"JVM slots {args.jvm_slots} | pairs {args.pairs or 'all'} | {info}")
    prog = C.Progress(len(todo), os.path.join(args.out, "score_progress.log"))

    def work(item):
        arm, agent, bench, tid, code, source, res = item
        lock = res + ".lock"
        if not C.try_lock(lock, args.stale_hours):
            return tid, "locked"
        task = evals[bench][tid]
        rec = {"arm": arm, "agent": agent, "bench": bench, "task_id": tid, "final_source": source,
               "final_sha256": _sha(code), "final_code": code, "provenance": info, "pairs": args.pairs,
               "started": C.now()}
        work_dir = os.path.join(args.out, "work", arm, f"{agent}_{bench}", tid)
        try:
            # integrity (F10, identical to the harness): a final file that changes the program, or cannot be
            # parsed, is a failed synthesis: not verified, all metrics 0
            try:
                orig = JavaMethodParser().parse(task["code"], "solve")
                integ = check_integrity(task["code"], code, orig, JavaMethodParser().parse(code, "solve"))
                broken = integ["class_renamed"] or integ["signature_changed"] or integ["body_modified"]
                reason = integ.get("reason", "")
            except Exception as e:
                broken, reason = True, f"unparseable: {e}"
            sp = extract_jml_spec(code, "solve")
            no_post = sp.postcondition is None
            rec.update(integrity_failed=broken, integrity_reason=reason, no_postcondition=no_post)
            if broken:
                rec.update(status="integrity_failed", verified=False)
            else:
                # D13: no `ensures` on solve (incl. no JML at all) says nothing about the result: not verified
                # and all metrics 0; OpenJML is still run and recorded as openjml_verified (RQ1's definition)
                t0 = time.time()
                ev.JVM_SLOTS.acquire(1)
                try:
                    vr = vt.verify_with_openjml(code, classname=task.get("class_name") or "Solution",
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
                    h = ev.evaluate_problem(task=ev.Task.from_dict(task), llm_code=code, openjml_path=args.openjml,
                                            output_dir=os.path.join(work_dir, "stubs"), verbose=False,
                                            max_pairs=args.pairs, exclude_collection_tasks=False)
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
        futs = {pool.submit(work, it): it for it in todo}
        for f in as_completed(futs):
            try:
                tid, st = f.result()
            except Exception as e:
                tid, st = futs[f][3], f"FAILED {type(e).__name__}: {e}"
            prog.tick(f"{tid}: {st}")
    if waiting:
        C.log(f"not scored because their agent session has not finished normally: {dict(waiting)}; "
              f"run `agents` again (it retries failed sessions), then `score` again")
    return 0


# ------------------------------------------------------------------ status / csv

def cmd_status(args) -> int:
    for arm, agent, bench in runs(args):
        n = len(json.load(open(FEEDBACK[bench]))) if os.path.exists(FEEDBACK[bench]) else "?"
        tids = [t["task_id"] for t in json.load(open(FEEDBACK[bench]))]
        st = collections.Counter(session_state(args.out, arm, agent, bench, t) for t in tids)
        scored = len(glob.glob(os.path.join(args.out, arm, "_scores", f"{agent}_{bench}", "*.json")))
        print(f"{arm:13s} {agent:8s} {bench:4s} tasks {n} | sessions {dict(st)} | scored {scored}")
    return 0


def cmd_csv(args) -> int:
    rows = []
    for p in sorted(glob.glob(os.path.join(args.out, "*", "_scores", "*", "*.json"))):
        r = json.load(open(p))
        h = r.get("harness") or {}
        row = {k: r.get(k, "") for k in ("arm", "agent", "bench", "task_id", "status", "verified", "openjml_verified",
                                          "verify_timed_out", "final_source", "verify_seconds", "harness_seconds")}
        row.update(integrity_failed=bool(r.get("integrity_failed") or h.get("integrity_failed")),
                   no_postcondition=bool(r.get("no_postcondition") or h.get("no_postcondition")),
                   excluded=h.get("excluded", ""), post_all_unknown=h.get("post_all_unknown", ""),
                   not_evaluable=";".join(h.get("not_evaluable", [])))
        failed = r.get("status") in ("unverified", "no_postcondition", "integrity_failed", "no_solution")
        for m in METRICS:
            x = h.get(m) or {}
            for k in ("score", "passed", "total", "unknowns", "errors", "evaluable"):
                row[f"{m}_{k}"] = x.get(k, "")
            if failed:
                row[f"{m}_score"] = 0.0                   # a failed synthesis scores 0 (never blank)
            elif x and x.get("evaluable") is False and not (h.get("integrity_failed") or h.get("no_postcondition")):
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
            p.add_argument("--parallel-runs", type=int, default=3, help="runs (agent x bench) in parallel")
            p.add_argument("--agent-threads", type=int, default=2, help="tasks in parallel per run")
            p.add_argument("--task-timeout", type=int, default=10800,
                           help="seconds per agent session, identical for every agent (default 3 h)")
            p.add_argument("--tool-timeout", type=int, default=1800,
                           help="seconds one tool call may take, identical for every agent (default 30 min)")
            p.add_argument("--claude-model", default="claude-sonnet-4-6")
            p.add_argument("--codex-model", default="gpt-5.4")
            p.add_argument("--codex-effort", default="medium")
            p.add_argument("--claude-effort", default="medium")
            p.add_argument("--veriact-model", default="gpt-5.4")
            p.add_argument("--veriact-effort", default="medium")
        if name == "score":
            p.add_argument("--workers", type=int, default=0, help="tasks in flight (default: jvm-slots / 2)")
            p.add_argument("--pairs", type=int, default=0,
                           help="score the first N evaluation pairs (0 = all 10; T8 chooses N)")
            p.add_argument("--stale-hours", type=float, default=12.0)
            p.add_argument("--retry-errors", action="store_true", help="re-score tasks whose result is an error")
    a = ap.parse_args()
    a.out = os.path.abspath(a.out)
    if a.cmd == "score" and not a.workers:
        a.workers = max(1, a.jvm_slots // 2)
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
