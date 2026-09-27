#!/usr/bin/env python3
"""Scaffold self-contained per-task working directories from a benchmark.

For each selected benchmark task this creates a directory (named by ``task_id``)
containing the reference ``Solution.java``, ``Test.java``, exactly five IO pairs
under ``tests/``, a vendored ``harness/`` (the four-tool CLI + its dependencies),
the four ``*.sh`` tool wrappers, and an ``AGENTS.md`` instruction file. It then
drops parent-root helpers (``run_agents.py``, ``collect.py``, ``README.md``) so
the whole out-root can be handed to an external coding agent.

The package is self-contained: it copies everything from ``templates/`` and never
imports ``veriact``, so it can live in its own repository.

Both benchmarks are already the final 120-task sets and are loaded the same way —
no sampling here.

Examples
--------
    # SpecGenBench (120 tasks)
    python -m agent_harness.scaffold \
        --benchmark benchmarks/specgenbench/sgb.json --out-root out/sgb

    # FormalBench (the pre-sampled 120 tasks)
    python -m agent_harness.scaffold \
        --benchmark benchmarks/formalbench/fb_120.json --out-root out/fb
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import shutil
import stat
import subprocess
import sys

TEMPLATES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

# Files vendored into each task dir's harness/ (verbatim copies).
HARNESS_FILES = [
    "cli.py",
    "harness_tool.py",
    "verifier_tool.py",
    "_env.sh",
    "requirements.txt",
]
# Wrapper scripts written into each task dir (verbatim copies, made executable).
# run_specharness.sh is only included when scaffolding *with* the harness tool.
WRAPPER_FILES_BASE = ["verify.sh", "submit.sh"]
HARNESS_WRAPPER = "run_specharness.sh"
# Parent-root helpers dropped once into the out-root.
ROOT_FILES = ["run_agents.py", "collect.py", "score_all.py", "aggregate.py", "usage_report.py"]

MAX_PAIRS = 10                      # T5 feedback list size


# ---------------------------------------------------------------------------
# benchmark loading / selection
# ---------------------------------------------------------------------------

def load_benchmark(path: str) -> list[dict]:
    with open(path) as fh:
        raw = fh.read().strip()
    if not raw:
        return []
    if path.endswith(".jsonl") or (raw[0] != "[" and "\n" in raw):
        records = []
        for line in raw.splitlines():
            line = line.strip()
            if line:
                records.append(json.loads(line))
        return records
    return json.loads(raw)


def select_tasks(tasks: list[dict], args: argparse.Namespace) -> list[dict]:
    """Scaffold all tasks in the benchmark, optionally filtered.

    Both sgb.json and fb_120.json are already the final 120-task sets, so no
    sampling happens here — they are loaded the same way.
    """
    if args.task_id:
        sel = [t for t in tasks if t.get("task_id") == args.task_id]
        if not sel:
            sys.exit(f"task_id '{args.task_id}' not found in benchmark")
        return sel

    if args.task_ids:
        with open(args.task_ids) as fh:
            wanted = {ln.strip() for ln in fh if ln.strip()}
        return [t for t in tasks if t.get("task_id") in wanted]

    if args.limit:
        return tasks[: args.limit]

    return tasks


# ---------------------------------------------------------------------------
# per-task scaffolding
# ---------------------------------------------------------------------------

def pick_pairs(task: dict, max_pairs: int = MAX_PAIRS) -> list[dict]:
    """Exactly *max_pairs* IO pairs: test_inputs first, then generated_test_cases."""
    pairs = list(task.get("test_inputs", []))[:max_pairs]
    if len(pairs) < max_pairs:
        need = max_pairs - len(pairs)
        pairs.extend(list(task.get("generated_test_cases", []))[:need])
    return pairs


def _safe_name(task_id: str) -> str:
    return task_id.replace("/", "_").replace("\\", "_")


def _write(path: str, content: str, executable: bool = False) -> None:
    with open(path, "w") as fh:
        fh.write(content)
    if executable:
        mode = os.stat(path).st_mode
        os.chmod(path, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _render(template_name: str, mapping: dict[str, str]) -> str:
    with open(os.path.join(TEMPLATES, template_name)) as fh:
        text = fh.read()
    for k, v in mapping.items():
        text = text.replace("{{" + k + "}}", v)
    return text


def scaffold_task(
    task: dict,
    out_root: str,
    threshold: float,
    with_harness: bool = True,
    max_attempts: int = 10,
) -> str:
    task_id = task["task_id"]
    task_dir = os.path.join(out_root, _safe_name(task_id))
    harness_dir = os.path.join(task_dir, "harness")
    tests_dir = os.path.join(task_dir, "tests")
    os.makedirs(harness_dir, exist_ok=True)
    os.makedirs(tests_dir, exist_ok=True)

    # Source files the agent works on / reads.
    _write(os.path.join(task_dir, "Solution.java"), task.get("code", ""))
    _write(os.path.join(task_dir, "Test.java"), task.get("test_code", ""))

    # The feedback pairs (MAX_PAIRS; with a *_v2_feedback.json benchmark these are the T5 feedback list).
    pairs = pick_pairs(task)
    for i, pair in enumerate(pairs):
        _write(
            os.path.join(tests_dir, f"case_{i}.json"),
            json.dumps({"input": pair["input"], "output": pair["output"]}, indent=2),
        )

    # Authoritative task record for the harness CLI: the chosen pairs as
    # test_inputs, generated_test_cases emptied (so scoring == tests/).
    task_record = dict(task)
    task_record["test_inputs"] = pairs
    task_record["generated_test_cases"] = []
    _write(os.path.join(harness_dir, "task.json"), json.dumps(task_record, indent=2))

    # Per-task tool config (attempt budget). $AGENT_MAX_ATTEMPTS overrides at runtime.
    _write(
        os.path.join(harness_dir, "config.json"),
        json.dumps({"max_attempts": max_attempts}, indent=2),
    )

    # Vendored harness files (cli.py is always present so the harness CLI can be
    # run offline for scoring, even in the no-harness ablation arm).
    for name in HARNESS_FILES:
        shutil.copy2(os.path.join(TEMPLATES, name), os.path.join(harness_dir, name))

    # Executable wrappers — run_specharness.sh only in the with-harness arm.
    wrappers = list(WRAPPER_FILES_BASE)
    if with_harness:
        wrappers.insert(1, HARNESS_WRAPPER)
    for name in wrappers:
        dst = os.path.join(task_dir, name)
        shutil.copy2(os.path.join(TEMPLATES, name), dst)
        mode = os.stat(dst).st_mode
        os.chmod(dst, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    # Instruction file — harness-aware vs harness-free variant.
    template = "AGENTS.md.tmpl" if with_harness else "AGENTS_no_harness.md.tmpl"
    agents_md = _render(
        template,
        {
            "TASK_ID": task_id,
            "THRESHOLD": f"{threshold:g}",
            "MAX_ATTEMPTS": str(max_attempts),
        },
    )
    _write(os.path.join(task_dir, "AGENTS.md"), agents_md)
    return task_dir


def write_root_helpers(
    out_root: str, benchmark: str, n_tasks: int, with_harness: bool = True
) -> None:
    for name in ROOT_FILES:
        dst = os.path.join(out_root, name)
        shutil.copy2(os.path.join(TEMPLATES, name), dst)
        if name.endswith(".py"):
            mode = os.stat(dst).st_mode
            os.chmod(dst, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    readme = _render(
        "root_README.md.tmpl",
        {
            "BENCHMARK": os.path.basename(benchmark),
            "N_TASKS": str(n_tasks),
            "MODE": "with-harness" if with_harness else "no-harness (ablation)",
        },
    )
    _write(os.path.join(out_root, "README.md"), readme)


def ensure_shared_venv(out_root: str) -> bool:
    """Create ONE shared venv at <out-root>/.venv with the harness deps, so each
    task session loads it instead of bootstrapping its own. Idempotent."""
    venv = os.path.join(out_root, ".venv")
    py = os.path.join(venv, "bin", "python")
    if os.path.exists(py):
        print(f"  shared venv already exists: {venv}")
        return True
    req = os.path.join(TEMPLATES, "requirements.txt")
    try:
        if shutil.which("uv"):
            subprocess.run(["uv", "venv", venv], check=True)
            subprocess.run(
                ["uv", "pip", "install", "--python", py, "-r", req], check=True
            )
        else:
            subprocess.run([sys.executable, "-m", "venv", venv], check=True)
            subprocess.run([py, "-m", "pip", "install", "-q", "-r", req], check=True)
        print(f"  shared venv ready: {venv}")
        return True
    except (subprocess.CalledProcessError, OSError) as exc:
        print(
            f"WARNING: could not create shared venv ({exc}); task scripts will "
            f"bootstrap it on first run, or set VERIACT_PYTHON.",
            file=sys.stderr,
        )
        return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Scaffold per-task agent working dirs.")
    p.add_argument("--benchmark", required=True, help="benchmark .json/.jsonl path")
    p.add_argument("--out-root", required=True, help="output root directory")
    p.add_argument("--task-id", help="scaffold a single task by id")
    p.add_argument("--task-ids", help="file with task ids (one per line)")
    p.add_argument("--limit", type=int, help="scaffold only the first N tasks")
    p.add_argument("--threshold", type=float, default=0.75, help="pass threshold (default: 0.75)")
    p.add_argument(
        "--no-harness",
        action="store_true",
        help="ablation: omit the run_spec_harness tool from the dirs and use a "
        "harness-free AGENTS.md (agent works with verify + submit only)",
    )
    p.add_argument(
        "--max-attempts",
        type=int,
        default=10,
        help="attempt budget per task (verify+harness calls; 0 = unlimited; "
        "default: 10 — matches VeriAct --max-steps 11, i.e. 10 tool calls + submit)",
    )
    p.add_argument(
        "--no-venv",
        action="store_true",
        help="don't create the shared <out-root>/.venv (use $VERIACT_PYTHON, or "
        "let task scripts bootstrap it on first run)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    tasks = load_benchmark(args.benchmark)
    selected = select_tasks(tasks, args)
    os.makedirs(args.out_root, exist_ok=True)

    with_harness = not args.no_harness
    for task in selected:
        scaffold_task(
            task, args.out_root, args.threshold, with_harness, args.max_attempts
        )
    write_root_helpers(args.out_root, args.benchmark, len(selected), with_harness)

    counts = collections.Counter(t.get("category", "") for t in selected)
    mode = "with-harness" if with_harness else "no-harness (ablation)"
    print(f"Scaffolded {len(selected)} task(s) [{mode}] -> {args.out_root}")
    for cat, n in counts.most_common():
        print(f"  {cat or '(none)'}: {n}")

    if not args.no_venv:
        ensure_shared_venv(args.out_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
