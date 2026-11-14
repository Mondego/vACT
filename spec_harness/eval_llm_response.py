"""
Batch-evaluate LLM-generated JML annotations from a JSONL response file
against a benchmark JSON, with two levels of threading:

  --threads    : outer parallelism — how many tasks to evaluate concurrently
  --max-pairs  : number of test pairs from benchmark test_inputs to use;
                 each test pair gets its own thread to compute all 4 metrics

Only responses with "status": "verified" are evaluated.

Fixes in this version:
  F3  compile/tool errors are a separate ERROR verdict, excluded from
      denominators and counted in `errors`
  F4  postcondition stubs check psi => phi per specification case (flag --post-stub-mode)
  F5  precondition checks use a trivial callee; only call-site Precondition /
      non-null warnings count as rejections
  F6  collection-typed tasks excluded by default (flag --keep-collection-tasks)
  F7  void-returning tasks excluded
  F8  missing `requires` policy (flag --missing-requires-policy)
  F10 class / signature / body integrity check (token-level); a modified submission
      is kept with all scores 0 (integrity_failed=True), i.e. counted as a failure
  D13 no `ensures` on the method: kept with all scores 0 (no_postcondition=True), not verified

Usage
-----
  python eval_spec_with_model_benchmark_response.py \\
      --benchmark_path benchmarks/benchmark.json \\
      --llm_response_path responses.jsonl \\
      --openjml /path/to/openjml \\
      --output results/ \\
      --threads 4 \\
      --max-pairs 10 \\
      [-v]
"""

from __future__ import annotations

from asyncio.log import logger
import os
import re
import json
import argparse
import atexit
import signal
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

import spec_harness.harness as harness_cfg          # policy flags (F4, F8)
from spec_harness.harness import (
    java_comments,
    MethodSpec,
    TestPair,
    InputCase,
    JavaMethodParser,
    JType,
    HarnessResult,
    VerifyResult,
    StubBuilder,
    OutputMutator,
    parse_openjml_output,      # F3
    classify_pre_verdict,      # F5
    check_integrity,           # F10
)
from spec_harness.eval_spec import (
    detect_output_layout,
    extract_jml_spec,
    detect_input_format,
    parse_input,
    parse_output,
    generate_invalid_inputs,
    decode_tagged,
)


# Data classes

@dataclass
class TestCase:
    input: str
    output: str

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> "TestCase":
        return cls(input=data["input"], output=data["output"])


@dataclass
class Task:
    task_id: str
    code: str
    class_name: str
    test_name: str
    javadoc: str
    category: str
    origin_id: str
    test_code: str = ""
    test_inputs: list[TestCase] = field(default_factory=list)
    generated_test_cases: list[TestCase] = field(default_factory=list)
    # validated invalid inputs (testset T6): None -> the old heuristic generator; [] -> no invalid input
    invalid_inputs: list[dict] | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Task":
        return cls(
            task_id=data["task_id"],
            code=data["code"],
            class_name=data["class_name"],
            test_name=data["test_name"],
            javadoc=data.get("javadoc", ""),
            category=data.get("category", ""),
            origin_id=data.get("origin_id", ""),
            test_code=data.get("test_code", ""),
            test_inputs=[TestCase.from_dict(tc) for tc in data.get("test_inputs", [])],
            generated_test_cases=[
                TestCase.from_dict(tc) for tc in data.get("generated_test_cases", [])
            ],
            invalid_inputs=data.get("invalid_inputs"),
        )


@dataclass
class LLMResponse:
    """One line from the JSONL response file."""

    task_id: str
    status: str
    final_code: str
    class_name: str
    config: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LLMResponse":
        return cls(
            task_id=data["task_id"],
            status=data.get("status", ""),
            final_code=data.get("final_code", ""),
            class_name=data.get("class_name", "Solution"),
            config=data.get("config", {}),
        )



# Loaders



def load_benchmark(benchmark_path: str) -> dict[str, Task]:
    """Load benchmark JSON once and return a task_id -> Task lookup dict."""
    with open(benchmark_path) as f:
        benchmark = json.load(f)
    return {entry["task_id"]: Task.from_dict(entry) for entry in benchmark}


def load_verified_responses(jsonl_path: str) -> list[LLMResponse]:
    """Read JSONL, return only responses with status == 'verified'."""
    responses: list[LLMResponse] = []
    with open(jsonl_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if obj.get("status") == "verified":
                responses.append(LLMResponse.from_dict(obj))
    return responses



# OpenJML runner — writes stubs to a persistent directory

class JvmSlots:
    """Process-wide cap on concurrently running OpenJML JVMs (0 = no cap).

    A dual decision takes 2 slots at once (accept + refute), a single verification 1. Slots are taken
    before the JVMs start and returned when they end, and every timeout is measured from the launch,
    so waiting for a slot never counts against the 300 s. Set with set_jvm_slots(n) or the environment
    variable SPEC_HARNESS_JVM_SLOTS (runners use this to match the machine's cores)."""

    def __init__(self, n: int = 0):
        self.n, self.used = n, 0
        self.cv = threading.Condition()

    def acquire(self, k: int) -> None:
        with self.cv:
            while self.n and self.used + k > self.n:
                self.cv.wait()
            self.used += k

    def release(self, k: int) -> None:
        with self.cv:
            self.used -= k
            self.cv.notify_all()


def _slots(n: int) -> int:
    """0 = no cap; otherwise at least 2 (a dual decision needs 2 slots at once, 1 would deadlock)."""
    return 0 if n <= 0 else max(2, n)


JVM_SLOTS = JvmSlots(_slots(int(os.environ.get("SPEC_HARNESS_JVM_SLOTS", "0") or 0)))


def set_jvm_slots(n: int) -> None:
    with JVM_SLOTS.cv:
        JVM_SLOTS.n = _slots(n)
        JVM_SLOTS.cv.notify_all()


# Every OpenJML call runs in its own session (setsid) so that a timeout kills the whole tree (wrapper
# script, JVM, cvc4). Such a group does not receive the caller's Ctrl-C or kill, so the live groups are
# tracked and killed when this process exits or is stopped; otherwise they outlive a stopped run.
_LIVE_GROUPS: set[int] = set()
_LIVE_LOCK = threading.Lock()
STOPPING = False                      # set by the stop handler: results computed from now on are not saved


def _launch(cmd: list, **kw) -> subprocess.Popen:
    """Start an OpenJML process in its own session and track its group, atomically with respect to the
    stop handler: once stopping, nothing new is launched."""
    with _LIVE_LOCK:
        if STOPPING:
            raise RuntimeError("stopping: no new OpenJML runs")
        p = subprocess.Popen(cmd, preexec_fn=os.setsid, **kw)
        _LIVE_GROUPS.add(p.pid)       # setsid: the group id is the child's pid
        return p


def _kill_group(p: subprocess.Popen) -> None:
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    with _LIVE_LOCK:
        _LIVE_GROUPS.discard(p.pid)


def kill_live_groups() -> None:
    with _LIVE_LOCK:
        groups = list(_LIVE_GROUPS)
    for g in groups:
        try:
            os.killpg(g, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


atexit.register(kill_live_groups)


def install_stop_handlers(*also) -> None:
    """For long runs (call from the main thread): on SIGINT / SIGTERM / SIGHUP kill every live OpenJML
    group (and call each function in `also`), then exit at once. Nothing is written after the signal, so
    no half-scored task is saved; the task is redone on resume (its lock belongs to a dead process)."""
    def stop(signum, frame):
        global STOPPING
        with _LIVE_LOCK:
            STOPPING = True           # no launch after this point (_launch checks it under the lock)
        kill_live_groups()
        for f in also:
            try:
                f()
            except Exception:
                pass
        print(f"stopped by signal {signum}: OpenJML processes killed", file=sys.stderr, flush=True)
        kill_live_groups()            # again: a group registered while the first pass ran
        os._exit(128 + signum)
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, stop)


class OpenJMLRunnerPersistent:
    """Runs OpenJML ESC, writing harness stubs to an actual directory."""

    def __init__(self, openjml_path: str, output_dir: str, timeout: int = 300):
        self.openjml_path = openjml_path
        self.output_dir = output_dir
        self.timeout = timeout
        os.makedirs(output_dir, exist_ok=True)

    def verify(
        self, java_source: str, class_name: str, label: str = ""
    ) -> tuple[VerifyResult, str]:
        # Filename MUST match the public class name inside the stub
        # (OpenJML / javac requires this).  Use a per-label subdirectory
        # so that parallel invocations don't overwrite each other.
        harness_class = f"{class_name}Harness"
        fname = f"{harness_class}.java"
        if label:
            safe_label = re.sub(r"[^\w\-]", "_", label)
            sub_dir = os.path.join(self.output_dir, safe_label)
            os.makedirs(sub_dir, exist_ok=True)
            path = os.path.join(sub_dir, fname)
        else:
            path = os.path.join(self.output_dir, fname)
        with open(path, "w") as f:
            f.write(java_source)
        cmd = [
            self.openjml_path,          # F9: honour --openjml (was hard-coded "openjml")
            "--esc",
            "--esc-max-warnings",
            "1",
            "--prover=cvc4",
            "--nonnull-by-default",
            "--arithmetic-failure=quiet",
            "-nowarn",
            path,
        ]
        JVM_SLOTS.acquire(1)
        try:
            proc = _launch(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except Exception:
            JVM_SLOTS.release(1)
            raise
        try:
            stdout, stderr = proc.communicate(timeout=self.timeout)
            returncode = proc.returncode
            out = stdout + stderr
            return self._parse(returncode, out), out
        except subprocess.TimeoutExpired:
            return VerifyResult.UNKNOWN, "timeout"
        except FileNotFoundError:
            raise RuntimeError(f"OpenJML binary not found at '{self.openjml_path}'.")
        except Exception as e:
            return VerifyResult.UNKNOWN, f"Error: {str(e)}"
        finally:
            _kill_group(proc)
            JVM_SLOTS.release(1)

    @staticmethod
    def _parse(returncode: int, output: str) -> VerifyResult:
        # F3: shared parser; compile/tool errors -> ERROR, not FAIL
        return parse_openjml_output(output, returncode)

    # ---- D8: dual accept / refute decision -----------------------------

    def _cmd(self, path: str) -> list[str]:
        # Exactly the verification command used for RQ1.
        return [self.openjml_path, "--esc", "--esc-max-warnings", "1", "--prover=cvc4",
                "--nonnull-by-default", "--arithmetic-failure=quiet", "-nowarn", path]

    def decide(self, accept_src: str, refute_src: str, class_name: str,
               label: str, wait_both: bool = False) -> tuple[str, dict]:
        """
        Run the accept and refute stubs concurrently. Returns (decision, info):
          "accept"  - the accept stub verified (the spec provably holds)
          "reject"  - the refute stub verified (the spec provably fails)
          "unknown" - neither verified within the timeout
          "error"   - both stubs failed to compile / the tool failed, or (wait_both) both verified
        wait_both (vacuity guard, used when the spec's helper methods carry JML contracts, which could be
        contradictory): do not stop at the first proof; wait for the other side (same timeout) and report
        "error" (vacuous) if it verifies too. Each stub's OpenJML output is kept next to it (openjml.out).
        """
        import time
        base = os.path.join(self.output_dir, re.sub(r"[^\w\-]", "_", label))
        procs = {}
        JVM_SLOTS.acquire(2)                      # both JVMs of this decision; the timer starts after launch
        try:
            for side, src in (("accept", accept_src), ("refute", refute_src)):
                d = os.path.join(base, side)
                os.makedirs(d, exist_ok=True)
                path = os.path.join(d, f"{class_name}Harness.java")
                with open(path, "w") as fh:
                    fh.write(src)
                out = open(os.path.join(d, "openjml.out"), "w")
                try:
                    p = _launch(self._cmd(path), stdout=out, stderr=subprocess.STDOUT, text=True)
                except Exception:
                    out.close()
                    raise
                procs[side] = (p, out, os.path.join(d, "openjml.out"))
        except Exception:                         # launch failed: stop what started, free the slots
            for p, out, _ in procs.values():
                _kill_group(p)
                out.close()
            JVM_SLOTS.release(2)
            raise
        start, verdicts = time.monotonic(), {}
        try:
            while len(verdicts) < 2 and time.monotonic() - start < self.timeout:
                for side, (p, out, opath) in procs.items():
                    if side in verdicts or p.poll() is None:
                        continue
                    _kill_group(p)                # the side has ended: stop any solver it left behind
                    out.close()
                    with open(opath, errors="ignore") as fh:
                        verdicts[side] = self._parse(p.returncode, fh.read())
                    if verdicts[side] == VerifyResult.OK and not wait_both:
                        return ("accept" if side == "accept" else "reject"), \
                               {"verdicts": {k: v.value for k, v in verdicts.items()},
                                "seconds": round(time.monotonic() - start, 2)}
                time.sleep(0.2)
            info = {"verdicts": {k: v.value for k, v in verdicts.items()},
                    "seconds": round(time.monotonic() - start, 2),
                    "timed_out": [s for s in procs if s not in verdicts]}
            ok = [side for side, v in verdicts.items() if v == VerifyResult.OK]
            if len(ok) == 2:
                return "error", {**info, "vacuous": True}      # both provable: contradictory context
            if len(ok) == 1:
                info["wait_both"] = True
                return ("accept" if ok[0] == "accept" else "reject"), info
            if len(verdicts) == 2 and all(v == VerifyResult.ERROR for v in verdicts.values()):
                return "error", info
            return "unknown", info
        finally:
            for side, (p, out, _) in procs.items():
                _kill_group(p)
                try:
                    out.close()
                except Exception:
                    pass
            JVM_SLOTS.release(2)



# Per-pair result — returned by each inner thread
@dataclass
class PairResult:
    """All 4 metric results for a single test pair."""

    pair_idx: int
    # PostCorrectness
    post_correct_detail: dict = field(default_factory=dict)
    # PostCompleteness (multiple mutants)
    post_complete_details: list[dict] = field(default_factory=list)
    # PreCorrectness (valid input for this pair)
    pre_correct_detail: dict = field(default_factory=dict)
    # PreCompleteness (invalid inputs assigned to this pair)
    pre_complete_details: list[dict] = field(default_factory=list)



# Inner thread: compute all 4 metrics for one test pair

def _evaluate_one_pair(
    pair_idx: int,
    pair: TestPair,
    valid_input_case: InputCase,
    invalid_cases: list[InputCase],
    parsed: dict,
    spec: MethodSpec,
    rtype: JType,
    builder: StubBuilder,
    mutator: OutputMutator,
    runner: OpenJMLRunnerPersistent,
    cname: str,
    verbose: bool,
    task_id: str,
    java_source: str = "",
    skip_post: bool = False,
) -> PairResult:
    """Compute all 4 metrics for a single test pair in its own thread."""
    result = PairResult(pair_idx=pair_idx)
    run_pre = _should_run_pre(spec)

    # PostCorrectness
    if spec.postcondition is not None and not skip_post:
        stub = builder.post_correctness_stub(parsed, spec, pair)
        verdict, out = runner.verify(stub, cname, f"post_correct_{pair.label}")
        ok = None if verdict == VerifyResult.ERROR else (verdict == VerifyResult.OK)
        result.post_correct_detail = {
            "label": pair.label,
            "verdict": verdict.value,
            "pass": ok,
            "stub": stub,
        }
        _log(verbose, task_id, "PostCorrectness", pair.label, verdict, bool(ok))

    # PostCompleteness
    if spec.postcondition is not None and not skip_post:
        try:
            mutants = mutator.mutate(rtype, pair.output)
        except Exception as e:                      # F2 guard (mutator should not raise)
            mutants = []
            print(f"WARNING [{task_id}] mutator failed on {pair.label}: {e}", file=sys.stderr)
        for mut_out in mutants:
            stub = builder.post_completeness_stub(parsed, spec, pair, mut_out)
            lbl = f"post_complete_{pair.label}_{str(mut_out)[:20]}"
            verdict, out = runner.verify(stub, cname, lbl)
            killed = None if verdict == VerifyResult.ERROR else (verdict == VerifyResult.FAIL)
            result.post_complete_details.append(
                {
                    "label": pair.label,
                    "mutant": str(mut_out),
                    "verdict": verdict.value,
                    "killed": killed,
                    "stub": stub,
                }
            )
            _log(verbose, task_id, "PostCompleteness", pair.label, verdict,
                 bool(killed), f"mutant={mut_out}")

    # PreCorrectness
    if run_pre:
        stub = builder.pre_correctness_stub(
            parsed, spec, valid_input_case, original_source=java_source
        )
        verdict, out = runner.verify(stub, cname, f"pre_correct_{valid_input_case.label}")
        verdict = classify_pre_verdict(verdict, out)          # F5
        ok = None if verdict == VerifyResult.ERROR else (verdict == VerifyResult.OK)
        result.pre_correct_detail = {
            "label": valid_input_case.label,
            "verdict": verdict.value,
            "pass": ok,
            "stub": stub,
        }
        _log(verbose, task_id, "PreCorrectness", valid_input_case.label, verdict, bool(ok))
    elif harness_cfg.MISSING_REQUIRES_POLICY == "requires_true":
        result.pre_correct_detail = {
            "label": valid_input_case.label, "verdict": "assumed",
            "pass": True, "stub": "",
        }

    # PreCompleteness
    if run_pre:
        for case in invalid_cases:
            stub = builder.pre_completeness_stub(
                parsed, spec, case, original_source=java_source
            )
            verdict, out = runner.verify(stub, cname, f"pre_complete_{case.label}")
            verdict = classify_pre_verdict(verdict, out)      # F5
            rejected = None if verdict == VerifyResult.ERROR else (verdict == VerifyResult.FAIL)
            result.pre_complete_details.append(
                {
                    "label": case.label,
                    "verdict": verdict.value,
                    "rejected": rejected,
                    "stub": stub,
                }
            )
            _log(verbose, task_id, "PreCompleteness", case.label, verdict, bool(rejected))
    elif harness_cfg.MISSING_REQUIRES_POLICY == "requires_true":
        for case in invalid_cases:
            result.pre_complete_details.append(
                {"label": case.label, "verdict": "assumed",
                 "rejected": False, "stub": ""}
            )

    return result


def _evaluate_one_pair_dual(
    pair_idx: int,
    pair: TestPair,
    valid_input_case: InputCase,
    invalid_cases: list[InputCase],
    parsed: dict,
    spec: MethodSpec,
    out_type: JType,
    builder: StubBuilder,
    mutator: OutputMutator,
    runner: OpenJMLRunnerPersistent,
    cname: str,
    verbose: bool,
    task_id: str,
    mutated_arg: tuple | None = None,
) -> PairResult:
    """
    D8: every check is decided by proving the spec accepts (accept stub) or rejects
    (refute stub) the concrete input/output. Detail values: True/False when proved,
    None when unknown or error (then excluded from the denominator and counted).
    """
    result = PairResult(pair_idx=pair_idx)
    guard = harness_cfg.POST_STUB_MODE != "strip"
    vacuity_guard = helpers_have_contracts(spec)

    def failed(e):
        # a check that raised (stub construction / rendering): an ERROR verdict with the exception,
        # excluded from the denominator and counted, never a silently dropped check (or pair)
        print(f"CHECK ERROR [{task_id}] {pair.label}: {type(e).__name__}: {e}", file=sys.stderr)
        return "error", {"exception": f"{type(e).__name__}: {e}"}, ""

    def post_check(output, lbl):
        try:
            stubs = builder.dual_post_stubs(parsed, spec, pair.inputs, output,
                                            mutated_arg=mutated_arg, guard_single_case=guard)
            if stubs is None:
                return None, {}, ""
            decision, info = runner.decide(stubs[0], stubs[1], cname, lbl, wait_both=vacuity_guard)
            return decision, info, stubs[0]
        except Exception as e:
            return failed(e)

    if spec.postcondition is not None:         # always true here: D13 returns earlier without ensures
        # PostCorrectness: the spec must accept the true output
        d, info, stub = post_check(pair.output, f"post_correct_{pair.label}")
        if d is not None:
            result.post_correct_detail = {
                "label": pair.label, "verdict": d, "decision_info": info,
                "pass": True if d == "accept" else False if d == "reject" else None, "stub": stub}
            _log(verbose, task_id, "PostCorrectness", pair.label, VerifyResult.OK if d == "accept"
                 else VerifyResult.FAIL, d == "accept")
        # PostCompleteness: the spec must reject every mutated output
        try:
            mutants = (mutator.mutate_same_length(pair.output, out_type.base) if mutated_arg is not None
                       else mutator.mutate(out_type, pair.output))
        except Exception as e:
            d, info, stub = failed(e)
            result.post_complete_details.append({"label": pair.label, "mutant": "", "verdict": d,
                                                 "decision_info": info, "killed": None, "stub": stub})
            mutants = []
        for mut in mutants:
            d, info, stub = post_check(mut, f"post_complete_{pair.label}_{str(mut)[:20]}")
            if d is None:
                continue
            result.post_complete_details.append({
                "label": pair.label, "mutant": str(mut), "verdict": d, "decision_info": info,
                "killed": True if d == "reject" else False if d == "accept" else None, "stub": stub})

    # Preconditions: prove psi (admitted) or !psi (rejected) on the pinned inputs
    policy = harness_cfg.MISSING_REQUIRES_POLICY
    if not spec.has_requires and policy == "zero":
        return result
    try:
        psi = builder.effective_pre(parsed, spec, implicit_nonnull=(policy == "jml"))
        psi_error = None
    except Exception as e:
        psi, psi_error = None, e

    def pre_check(case, lbl):
        if psi_error is not None:
            return failed(psi_error)
        if psi is None:                      # precondition is `true`: admitted, no proof needed
            return "accept", {"trivial": True}, ""
        try:
            a, r = builder.dual_pre_stubs(parsed, spec, case.inputs, psi)
            d, info = runner.decide(a, r, cname, lbl, wait_both=vacuity_guard)
            return d, info, a
        except Exception as e:
            return failed(e)

    d, info, stub = pre_check(valid_input_case, f"pre_correct_{valid_input_case.label}")
    result.pre_correct_detail = {
        "label": valid_input_case.label, "verdict": d, "decision_info": info,
        "pass": True if d == "accept" else False if d == "reject" else None, "stub": stub}
    for case in invalid_cases:
        d, info, stub = pre_check(case, f"pre_complete_{case.label}")
        result.pre_complete_details.append({
            "label": case.label, "verdict": d, "decision_info": info,
            "rejected": True if d == "reject" else False if d == "accept" else None, "stub": stub})
    return result


_CONTRACT_WORDS = re.compile(r"\b(requires|ensures|pre|post|axiom|invariant|represents|signals|constraint|"
                             r"initially)(_redundantly)?\b")


def helpers_have_contracts(spec: MethodSpec) -> bool:
    """True if the helper members copied into the stubs carry JML contracts of their own. Contradictory
    helper contracts make both the accept and the refute stub provable, so decisions then wait for both
    sides (decide(wait_both=True)) and report such a check as an error."""
    src = spec.helpers or ""
    for s_, e_, kind in java_comments(src):
        body = src[s_ + 2:e_]
        if body.startswith("@") and _CONTRACT_WORDS.search(body):
            return True
    return False


def _should_run_pre(spec: MethodSpec) -> bool:
    """F8: decide whether to run OpenJML for the precondition metrics."""
    if spec.has_requires:
        return True
    return harness_cfg.MISSING_REQUIRES_POLICY == "jml"


_COLLECTION_RE = re.compile(r"\b(List|ArrayList|LinkedList|Set|HashSet|TreeSet|Map|HashMap|TreeMap)\b")


def _has_collection(jtype: JType) -> bool:
    return jtype.is_collection or bool(_COLLECTION_RE.search(jtype.java_decl()))


def _aggregate(details: list[dict], key: str, name: str) -> HarnessResult:
    """F3: ERROR checks (value None) are excluded from the denominator and counted."""
    hr = HarnessResult(name, 0, 0)
    for d in details:
        v = d.get(key)
        if v is None and d.get("verdict") == "unknown":
            hr.unknowns += 1
        elif v is None:
            hr.errors += 1
        else:
            hr.total += 1
            if v:
                hr.passed += 1
    hr.details = details
    return hr


def _empty_result(task_id: str, reason: str, integrity: dict | None = None,
                  extra: dict | None = None) -> dict:
    """Result dict for an excluded task: all four metrics present, score 0, excluded=True."""
    out: dict[str, Any] = {"task_id": task_id, "excluded": True, "exclude_reason": reason}
    for k in ("post_correctness", "post_completeness", "pre_correctness", "pre_completeness"):
        out[k] = {"score": 0.0, "passed": 0, "total": 0, "errors": 0, "evaluable": False, "details": []}
    out["integrity"] = integrity or {}
    if extra:
        out.update(extra)
    return out



# Main evaluation — one thread per test pair

def evaluate_problem(
    task: Task,
    llm_code: str,
    openjml_path: str = "openjml",
    output_dir: str = ".",
    verbose: bool = False,
    max_pairs: int = 0,
    exclude_collection_tasks: bool = True,
) -> dict:
    """
    Evaluate one task. Each test pair gets its own thread to compute
    all 4 metrics in parallel.

    Integrity failure (F10: class name, signature, or method body changed): returned
    with all scores 0 and integrity_failed=True, NOT excluded (counts as a failure).
    No postcondition (D13: no `ensures` clause on the method): all scores 0, no_postcondition=True,
    NOT excluded (counts as a failure); nothing is verified.
    Unparseable LLM code (target method missing, or not valid Java) is an integrity failure too.

    Exclusions (task is returned with excluded=True and all scores 0):
      - F7 : method returns void (no \result to constrain)
      - F6 : collection-typed parameter or return, when exclude_collection_tasks
    """
    solution_src = task.code
    test_src = task.test_code
    io_pairs = [{"input": tc.input, "output": tc.output} for tc in task.test_inputs]
    io_pairs_gen = [
        {"input": tc.input, "output": tc.output} for tc in task.generated_test_cases
    ]

    if max_pairs > 0:
        io_pairs = io_pairs[:max_pairs]
        remaining = max_pairs - len(io_pairs)
        if remaining > 0:
            io_pairs.extend(io_pairs_gen[:remaining])

    # parse method signature from benchmark Solution.java 
    parser = JavaMethodParser()
    bench_parsed = parser.parse(solution_src)
    bench_params = bench_parsed["params"]
    return_type = bench_parsed["return_type"]

    # extract JML spec from LLM code (F9: single call)
    # target = the benchmark's method (normally `solve`), even if the LLM added
    # helper methods before it
    spec = extract_jml_spec(llm_code, bench_parsed["method_name"])

    # ---- parse LLM method signature ----
    try:
        llm_parsed = parser.parse(llm_code, bench_parsed["method_name"])
        llm_params = llm_parsed["params"]
    except (ValueError, Exception) as e:
        # F10: the target method is missing (e.g. renamed) or the submission is not valid Java (e.g. a bare
        # `requires ...;` outside a comment, which OpenJML accepts): the code was not kept, so this is an
        # integrity failure, scored 0 and kept in the denominators (like a changed body)
        print(f"INTEGRITY FAILED [{task.task_id}]: llm code unparseable: {e}", file=sys.stderr)
        res = _empty_result(task.task_id, f"integrity: llm code unparseable: {e}",
                            {"class_renamed": None, "signature_changed": None, "body_modified": None,
                             "unparseable": True, "reason": f"llm code unparseable: {e}"})
        res["excluded"] = False
        res["integrity_failed"] = True
        return res

    # ---- F10: integrity (class, signature, body unchanged) ----
    integrity = check_integrity(solution_src, llm_code, bench_parsed, llm_parsed)
    if integrity["class_renamed"] or integrity["signature_changed"] or integrity["body_modified"]:
        print(f"INTEGRITY FAILED [{task.task_id}]: {integrity['reason']}", file=sys.stderr)
        # A submission that changed the code is a failed synthesis, not an unscorable
        # one: keep it in the denominators with all scores 0 (excluded=False).
        res = _empty_result(task.task_id, "integrity: " + integrity["reason"], integrity)
        res["excluded"] = False
        res["integrity_failed"] = True
        return res

    # ---- void methods: the modified array argument is the output ----
    out_type, mutated_bench = return_type, None
    if return_type.base == "void":
        arrays = [p for p in bench_params if p["jtype"].is_array and p["jtype"].array_dims == 1]
        if not arrays or harness_cfg.DECISION_MODE != "dual":
            return _empty_result(task.task_id, "unsupported_return: void", integrity,
                                 {"unsupported_return": True})
        out_type, mutated_bench = arrays[0]["jtype"], arrays[0]["name"]

    # stubs need the imports of both the benchmark and the submission
    spec.imports = "\n".join(dict.fromkeys(
        [l.strip() for l in (solution_src + "\n" + llm_code).splitlines()
         if re.match(r"^\s*import\s+(static\s+)?[\w.]+(\.\*)?\s*;", l)]))

    # ---- F6: collection-typed parameters / return ----
    has_coll_param = any(_has_collection(p["jtype"]) for p in bench_params)
    has_coll_ret   = _has_collection(return_type)
    if exclude_collection_tasks and (has_coll_param or has_coll_ret):
        why = ("collection parameter" if has_coll_param else "collection return")
        return _empty_result(task.task_id, f"unsupported_type: {why}", integrity,
                             {"has_collection_param": has_coll_param,
                              "has_collection_return": has_coll_ret})

    # ---- D13: no postcondition (no `ensures` on the method) ----
    # A specification without a postcondition says nothing about the result: all four metrics are 0,
    # nothing is verified, and the task stays in the denominators (excluded=False), like an integrity
    # failure. A missing `requires` alone is fine (precondition true).
    if spec.postcondition is None:
        print(f"NOTE [{task.task_id}]: no JML ensures clause: all metrics 0", file=sys.stderr)
        res = _empty_result(task.task_id, "no_postcondition", integrity)
        res["excluded"] = False
        res["no_postcondition"] = True
        for k in ("post_correctness", "post_completeness", "pre_correctness", "pre_completeness"):
            res[k]["evaluable"] = True
        return res

    # detect input format from Test.java
    read_ops = detect_input_format(test_src, bench_params)
    out_layout = detect_output_layout(test_src)          # how Test.java prints the result

    # parse io_pairs into TestPair / InputCase objects
    test_pairs: list[TestPair] = []
    valid_inputs: list[dict] = []

    for idx, pair in enumerate(io_pairs):
        try:
            inputs_bench = parse_input(pair["input"], read_ops)
            output = parse_output(pair["output"], out_type, out_layout)
            if mutated_bench is not None:
                # the in-place output must have the argument's length (an empty array stays empty)
                if not isinstance(output, list) or len(output) != len(inputs_bench.get(mutated_bench) or []):
                    continue
        except Exception as e:
            if verbose:
                print(
                    f"WARNING [{task.task_id}]: skipping case {idx}: {e}",
                    file=sys.stderr,
                )
            continue

        inputs_llm: dict[str, Any] = {}
        for bp, lp in zip(bench_params, llm_params):
            inputs_llm[lp["name"]] = inputs_bench[bp["name"]]

        test_pairs.append(TestPair(inputs_llm, output, f"case_{idx}"))
        valid_inputs.append(inputs_llm)

    if not test_pairs:
        print(f"ERROR [{task.task_id}]: no test pairs could be parsed", file=sys.stderr)
        return {}

    # build InputCases for Pre metrics
    valid_input_cases = [InputCase(tp.inputs, True, tp.label) for tp in test_pairs]
    if task.invalid_inputs is not None:
        # D4: validated T- (the reference throws or does not return within 300 s), all of them,
        # renamed to the submission's parameter names
        invalid_input_cases = []
        for k, inv in enumerate(task.invalid_inputs):
            vals = decode_tagged(inv["inputs"])
            invalid_input_cases.append(InputCase(
                {lp["name"]: vals[bp["name"]] for bp, lp in zip(bench_params, llm_params)}, False, f"inv_{k}"))
    else:
        invalid_input_cases = generate_invalid_inputs(llm_params, valid_inputs)

    # setup
    builder = StubBuilder()
    mutator = OutputMutator(k=5)
    parsed = llm_parsed
    cname = parsed["class_name"]
    rtype = parsed["return_type"]

    safe_task_id = task.task_id.replace("/", "_").replace("\\", "_")
    stubs_dir = os.path.join(output_dir, "stubs", safe_task_id)
    runner = OpenJMLRunnerPersistent(openjml_path, stubs_dir)

    # distribute invalid cases round-robin across test pairs (scheduling only: PreComp always uses all of them)
    invalid_per_pair: list[list[InputCase]] = [[] for _ in test_pairs]
    for i, ic in enumerate(invalid_input_cases):
        invalid_per_pair[i % len(test_pairs)].append(ic)

    # launch one thread per test pair
    n_threads = len(test_pairs)
    pair_results: list[PairResult] = [None] * n_threads  # type: ignore
    pair_errors: list[str] = []          # pairs whose evaluation raised (should not happen: checks catch)

    mutated_arg = None
    if mutated_bench is not None:
        llm_name = dict(zip([p["name"] for p in bench_params],
                            [p["name"] for p in llm_params]))[mutated_bench]
        mutated_arg = (llm_name, out_type)

    if harness_cfg.DECISION_MODE == "dual":
        with ThreadPoolExecutor(max_workers=n_threads) as pool:
            futs = {pool.submit(_evaluate_one_pair_dual, idx, pair, valid_input_cases[idx],
                                invalid_per_pair[idx], parsed, spec, out_type, builder, mutator,
                                runner, cname, verbose, task.task_id, mutated_arg): idx
                    for idx, pair in enumerate(test_pairs)}
            for fut in as_completed(futs):
                idx = futs[fut]
                try:
                    pair_results[idx] = fut.result()
                except Exception as e:
                    pair_errors.append(f"pair {idx}: {type(e).__name__}: {e}")
                    print(f"ERROR [{task.task_id}] pair {idx}: {e}", file=sys.stderr)

    with ThreadPoolExecutor(max_workers=n_threads) as pool:
        future_to_idx = {} if harness_cfg.DECISION_MODE == "dual" else {
            pool.submit(
                _evaluate_one_pair,
                idx,
                pair,
                valid_input_cases[idx],
                invalid_per_pair[idx],
                parsed,
                spec,
                rtype,
                builder,
                mutator,
                runner,
                cname,
                verbose,
                task.task_id,
                llm_code,
                False,
            ): idx
            for idx, pair in enumerate(test_pairs)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                pair_results[idx] = future.result()
            except Exception as e:
                print(f"ERROR [{task.task_id}] pair {idx}: {e}", file=sys.stderr)

    # aggregate PairResults into HarnessResults
    pc_details: list[dict] = []
    pcl_details: list[dict] = []
    prc_details: list[dict] = []
    prl_details: list[dict] = []

    for pr in pair_results:
        if pr is None:
            continue
        if pr.post_correct_detail:
            pc_details.append(pr.post_correct_detail)
        pcl_details.extend(pr.post_complete_details)
        if pr.pre_correct_detail:
            prc_details.append(pr.pre_correct_detail)
        prl_details.extend(pr.pre_complete_details)

    results: dict[str, HarnessResult] = {
        "post_correctness":  _aggregate(pc_details,  "pass",     "PostCorrectness"),
        "post_completeness": _aggregate(pcl_details, "killed",   "PostCompleteness"),
        "pre_correctness":   _aggregate(prc_details, "pass",     "PreCorrectness"),
        "pre_completeness":  _aggregate(prl_details, "rejected", "PreCompleteness"),
    }

    # summary
    sep = "=" * 60
    print(f"\n{sep}")
    print(
        f"  Spec-Harness  |  task: {task.task_id}"
        f"  |  method: {parsed['method_name']}"
    )
    print(sep)
    for r in results.values():
        print(f"  {r}")
    print(sep)

    # build output dict 
    output: dict[str, Any] = {
        "task_id": task.task_id,
        "excluded": False,
        "exclude_reason": "",
        "integrity": integrity,
        "unsupported_return": False,
        "has_collection_param": has_coll_param,
        "has_collection_return": has_coll_ret,
        "pairs_used": len(test_pairs),
        "invalid_inputs_used": len(invalid_input_cases),
        "invalid_inputs_source": "validated" if task.invalid_inputs is not None else "heuristic",
        "mutants_total": len(pcl_details),
        "policy": {
            "post_stub_mode": harness_cfg.POST_STUB_MODE,
            "missing_requires_policy": harness_cfg.MISSING_REQUIRES_POLICY,
            "decision_mode": harness_cfg.DECISION_MODE,
        },
        "mutated_arg": mutated_bench,
    }
    for k, v in results.items():
        output[k] = {
            "score": v.score,
            "passed": v.passed,
            "total": v.total,
            "errors": v.errors,          # F3
            "unknowns": v.unknowns,      # D8
            # D3/D4: no decided check (all unknown/error, or an empty T-) -> the metric is not
            # evaluable for this task: exclude it from Harness@tau denominators, do not count it as 0
            "evaluable": v.total > 0,
            "details": v.details,
        }
    output["pair_errors"] = pair_errors
    output["pairs_unparsed"] = [i for i in range(len(io_pairs)) if f"case_{i}" not in
                                {tp.label for tp in test_pairs}]
    output["check_exceptions"] = sum(1 for det in (pc_details, pcl_details, prc_details, prl_details)
                                     for d in det if (d.get("decision_info") or {}).get("exception"))
    output["not_evaluable"] = [k for k, v in results.items() if v.total == 0]
    output["post_all_unknown"] = (results["post_correctness"].total == 0
                                  and results["post_correctness"].unknowns > 0)
    return output


def _log(
    verbose: bool,
    task_id: str,
    metric: str,
    label: str,
    verdict: VerifyResult,
    desired: bool,
    extra: str = "",
) -> None:
    if not verbose:
        return
    status = "✓" if desired else "✗"
    ex = f"  [{extra}]" if extra else ""
    print(f"  [{task_id}] [{metric:20s}] {label}{ex}" f"  →  {verdict.value}  {status}")



# Outer-level batch processing with threading

def process_one(
    task: Task,
    resp: LLMResponse,
    openjml_path: str,
    output_dir: str,
    verbose: bool,
    max_pairs: int,
    exclude_collection_tasks: bool = True,
) -> dict | None:
    """Evaluate a single (task, response) pair. Returns result dict or None."""
    try:
        result = evaluate_problem(
            task=task,
            llm_code=resp.final_code,
            openjml_path=openjml_path,
            output_dir=output_dir,
            verbose=verbose,
            max_pairs=max_pairs,
            exclude_collection_tasks=exclude_collection_tasks,
        )
        if result:
            safe_id = task.task_id.replace("/", "_").replace("\\", "_")
            out_path = os.path.join(output_dir, f"{safe_id}.json")
            with open(out_path, "w") as f:
                json.dump(result, f, indent=2)
        return result
    except Exception as e:
        print(f"ERROR [{resp.task_id}]: {e}", file=sys.stderr)
        return None


def run_batch(
    benchmark_path: str,
    llm_response_path: str,
    openjml_path: str,
    output_dir: str,
    threads: int = 1,
    verbose: bool = False,
    max_pairs: int = 0,
    exclude_collection_tasks: bool = True,
) -> list[dict]:
    """
    Load benchmark + verified LLM responses, evaluate all.

    Parameters
    ----------
    threads    : outer parallelism — number of tasks evaluated concurrently
    max_pairs  : number of test pairs to use per task (0 = all);
                 also the number of inner threads per task
    """
    result_base = os.path.splitext(os.path.basename(llm_response_path))[0]
    result_file = path = os.path.join(output_dir, f"{result_base}_spec_harness.json")
    output_dir = os.path.join(output_dir, f"{result_base}")
    os.makedirs(output_dir, exist_ok=True)

    task_lookup = load_benchmark(benchmark_path)
    responses = load_verified_responses(llm_response_path)

    print(
        f"Loaded {len(task_lookup)} benchmark tasks, "
        f"{len(responses)} verified responses"
    )

    # Match responses to tasks
    work: list[tuple[Task, LLMResponse]] = []
    for resp in responses:
        task = task_lookup.get(resp.task_id)
        if task is None:
            print(
                f"WARNING: task_id '{resp.task_id}' not in benchmark, " f"skipping",
                file=sys.stderr,
            )
            continue
        work.append((task, resp))

    print(
        f"Evaluating {len(work)} tasks  "
        f"(outer threads={threads}, "
        f"inner threads per task=max_pairs={max_pairs or 'all'})"
    )

    all_results: list[dict] = []

    if threads <= 1:
        for task, resp in work:
            r = process_one(task, resp, openjml_path, output_dir, verbose,
                            max_pairs, exclude_collection_tasks)
            if r:
                all_results.append(r)
    else:
        with ThreadPoolExecutor(max_workers=threads) as pool:
            future_to_tid = {
                pool.submit(
                    process_one,
                    task,
                    resp,
                    openjml_path,
                    output_dir,
                    verbose,
                    max_pairs,
                    exclude_collection_tasks,
                ): resp.task_id
                for task, resp in work
            }
            for future in as_completed(future_to_tid):
                tid = future_to_tid[future]
                try:
                    r = future.result()
                    if r:
                        all_results.append(r)
                except Exception as e:
                    print(f"ERROR [{tid}]: {e}", file=sys.stderr)

    # write summary 
    excluded = [r for r in all_results if r.get("excluded")]
    summary = {
        "total_evaluated": len(all_results),
        "total_scored": len(all_results) - len(excluded),
        "total_excluded": len(excluded),
        "total_integrity_failed": sum(1 for r in all_results if r.get("integrity_failed")),
        "total_no_postcondition": sum(1 for r in all_results if r.get("no_postcondition")),
        "excluded_by_reason": {},
        "total_verified_responses": len(responses),
        "policy": {
            "post_stub_mode": harness_cfg.POST_STUB_MODE,
            "missing_requires_policy": harness_cfg.MISSING_REQUIRES_POLICY,
            "exclude_collection_tasks": exclude_collection_tasks,
        },
        "error_checks": {"post_correctness": 0, "post_completeness": 0,
                         "pre_correctness": 0, "pre_completeness": 0},
        "tasks": [],
    }
    for r in excluded:
        key = r.get("exclude_reason", "").split(":")[0] or "unknown"
        summary["excluded_by_reason"][key] = summary["excluded_by_reason"].get(key, 0) + 1
    for r in all_results:
        for k in summary["error_checks"]:
            summary["error_checks"][k] += r[k].get("errors", 0)
        summary["tasks"].append(
            {
                "task_id": r["task_id"],
                "excluded": r.get("excluded", False),
                "exclude_reason": r.get("exclude_reason", ""),
                "post_correctness": r["post_correctness"]["score"],
                "post_completeness": r["post_completeness"]["score"],
                "pre_correctness": r["pre_correctness"]["score"],
                "pre_completeness": r["pre_completeness"]["score"],
                "errors": {k: r[k].get("errors", 0) for k in summary["error_checks"]},
                "evaluable": {k: r[k].get("evaluable", r[k].get("total", 0) > 0) for k in summary["error_checks"]},
                "invalid_inputs_source": r.get("invalid_inputs_source", ""),
                "pairs_used": r.get("pairs_used", 0),
                "mutants_total": r.get("mutants_total", 0),
            }
        )

    with open(result_file, "w") as f:
        json.dump(summary, f, indent=2)

    print(f"\n{'=' * 60}")
    print(f"  Batch complete: {len(all_results)}/{len(work)} tasks evaluated, "
          f"{len(excluded)} excluded {summary['excluded_by_reason']}")
    print(f"  ERROR checks: {summary['error_checks']}")
    print(f"  Summary -> {result_file}")
    print(f"{'=' * 60}")

    return all_results



# CLI

def main():
    ap = argparse.ArgumentParser(
        description="Batch-evaluate LLM JML annotations from a JSONL "
        "response file against a benchmark JSON."
    )
    ap.add_argument(
        "--benchmark_path", required=True, help="Path to benchmark JSON file"
    )
    ap.add_argument(
        "--llm_response_path",
        required=True,
        help="Path to JSONL file with LLM responses",
    )
    ap.add_argument("--openjml", default="openjml", help="Path to OpenJML binary")
    ap.add_argument(
        "--output",
        default="results",
        help="Directory to store per-task results and summary",
    )
    ap.add_argument(
        "--threads",
        type=int,
        default=8,
        help="Number of tasks to evaluate concurrently " "(default: 8)",
    )
    ap.add_argument(
        "--max-pairs",
        type=int,
        default=5,
        help="Number of test pairs from benchmark test_inputs "
        "to use (0 = all); each pair gets its own thread "
        "for computing all 4 metrics",
    )
    ap.add_argument(
        "--missing-requires-policy",
        choices=["jml", "requires_true", "zero"],
        default="jml",
        help="F8: scoring of precondition metrics when the spec has no "
        "`requires` (jml = run call-site check, absent requires acts as "
        "`requires true` with implicit non-null; requires_true = PreCorr 1.0 / "
        "PreComp 0.0 without OpenJML; zero = legacy 0/0)",
    )
    ap.add_argument(
        "--post-stub-mode",
        choices=["guard", "strip", "raw"],
        default="guard",
        help="F4/D1: guard (default) = check psi => phi per specification case; "
        "strip = drop requires from single-case specs; raw = spec block as written "
        "(the original harness)",
    )
    ap.add_argument(
        "--decision-mode",
        choices=["dual", "single"],
        default="dual",
        help="D8: dual (default) = prove accept or reject per check; single = the "
        "original harness (a failure to prove counts as a kill/rejection)",
    )
    ap.add_argument(
        "--keep-collection-tasks",
        action="store_true",
        help="F6: score tasks with collection-typed params/returns instead of "
        "excluding them (element pinning is best-effort)",
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    harness_cfg.MISSING_REQUIRES_POLICY = args.missing_requires_policy
    harness_cfg.POST_STUB_MODE = args.post_stub_mode
    harness_cfg.DECISION_MODE = args.decision_mode

    run_batch(
        benchmark_path=args.benchmark_path,
        llm_response_path=args.llm_response_path,
        openjml_path=args.openjml,
        output_dir=args.output,
        threads=args.threads,
        verbose=args.verbose,
        max_pairs=args.max_pairs,
        exclude_collection_tasks=not args.keep_collection_tasks,
    )


if __name__ == "__main__":

    main()
