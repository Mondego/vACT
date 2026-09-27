#!/usr/bin/env python3
"""
Generate the vendored, self-contained ``harness_tool.py`` from ``spec_harness/``.

The agents' tools (agent_harness/templates/cli.py, veriact/tools/cli.py,
veriact/run/score_no_harness.py) import ``harness_tool`` as a single file so a task
directory stays self-contained. It used to be a hand-merged copy of
spec_harness/{harness,eval_spec,eval_llm_response}.py, which let the copies drift
apart. This script rebuilds it from those sources, so there is one implementation.

Run from the repository root after changing anything in spec_harness/:
    python agent_harness/build_harness_tool.py          # writes both copies
    python agent_harness/build_harness_tool.py --check  # exit 1 if a copy is stale

The generated module keeps the vendored API:
    evaluate_problem(task, llm_code, openjml_path="openjml", output_dir=...,
                     verbose=False, max_pairs=10, run_id="")
      -> {} if no test pairs parse, else
         {"task_id", "post_correctness", "post_completeness",
          "pre_correctness", "pre_completeness", "integrity_failed", "note"}
    with float scores, and nothing printed to stdout (the CLI's stdout is JSON).
"""
from __future__ import annotations

import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "spec_harness")
TARGETS = {
    os.path.join(ROOT, "agent_harness", "templates", "harness_tool.py"): "",
    os.path.join(ROOT, "veriact", "tools", "harness_tool.py"):
        '# Cap OpenJML\'s JVM heap (its default max heap is ~1/4 of RAM). Overridable via env.\n'
        'os.environ.setdefault("OPENJML_JVM", "-Xmx8g")\n',
}

HEADER = '''"""
harness_tool.py  --  GENERATED FILE, DO NOT EDIT.

Built by agent_harness/build_harness_tool.py from spec_harness/harness.py,
spec_harness/eval_spec.py and spec_harness/eval_llm_response.py. Edit those files
and re-run the builder.

Self-contained module exposing ``evaluate_problem`` (float scores, no stdout output)
and ``evaluate_problem_detailed`` (per-check details), plus ``Task`` / ``TestCase``.
"""
from __future__ import annotations
'''

ADAPTER = '''

# ============================================================
# Vendored API adapter (generated)
# ============================================================

import contextlib as _contextlib
import sys as _sys


def evaluate_problem(task, llm_code, openjml_path="openjml", output_dir="veriact_outputs",
                     verbose=False, max_pairs=10, run_id=""):
    """Scores for the agents' tools: a float per metric, or None when the metric is not evaluable for
    this task (no decided check), explained in `note`. Nothing is written to stdout.
    Collection-typed tasks are scored (D3), exactly as in final scoring."""
    out_dir = os.path.join(output_dir, run_id) if run_id else output_dir
    with _contextlib.redirect_stdout(_sys.stderr):
        res = evaluate_problem_detailed(task, llm_code, openjml_path=openjml_path, output_dir=out_dir,
                                        verbose=verbose, max_pairs=max_pairs, exclude_collection_tasks=False)
    if not res:
        return {}
    out = {"task_id": res.get("task_id", task.task_id)}
    metrics = ("post_correctness", "post_completeness", "pre_correctness", "pre_completeness")
    notes = []
    if res.get("integrity_failed"):
        for k in metrics:
            out[k] = 0.0
        notes.append("The method body or signature differs from the original program; "
                     "restore the original code and change only the JML annotations.")
    elif res.get("no_postcondition"):
        for k in metrics:
            out[k] = 0.0
        notes.append("No JML postcondition: the method has no `ensures` clause, so the specification says "
                     "nothing about the result. Add `ensures` clauses that describe \\result.")
    elif res.get("excluded"):
        for k in metrics:
            out[k] = None
        notes.append("Task could not be scored: " + str(res.get("exclude_reason", "")))
    else:
        for k in metrics:
            r = res.get(k, {}) or {}
            evaluable = r.get("evaluable", (r.get("total") or 0) > 0)
            out[k] = float(r.get("score", 0.0)) if evaluable else None
            if evaluable:
                continue
            if k == "pre_completeness" and not res.get("invalid_inputs_used"):
                notes.append("pre_completeness: not applicable (no input makes the method fail)")
            elif r.get("unknowns"):
                notes.append(f"{k}: not evaluable (the verifier could not decide these checks)")
            elif r.get("errors"):
                notes.append(f"{k}: not evaluable (the checks could not be run; see the verifier output)")
            else:
                notes.append(f"{k}: not evaluable (no checks)")
    out["integrity_failed"] = bool(res.get("integrity_failed"))
    out["note"] = "; ".join(notes)
    return out
'''


def _drop_block_import(text: str, module: str) -> str:
    """Remove `from <module> import (...)` (possibly multi-line) statements."""
    return re.sub(r"^from " + re.escape(module) + r" import \([^)]*\)\n", "", text, flags=re.M)


def _body(path: str, cut_at: str | None) -> str:
    text = open(path).read()
    if cut_at:
        i = text.find(cut_at)
        if i >= 0:
            text = text[:i]
    text = re.sub(r"^from __future__ import annotations\n", "", text, flags=re.M)
    text = _drop_block_import(text, "spec_harness.harness")
    text = _drop_block_import(text, "spec_harness.eval_spec")
    text = re.sub(r"^import spec_harness\.harness as harness_cfg.*\n", "", text, flags=re.M)
    text = text.replace("harness_cfg.", "")
    return text


def build() -> str:
    parts = [HEADER]
    parts.append("\n# ==== from spec_harness/harness.py ====\n")
    parts.append(_body(os.path.join(SRC, "harness.py"), 'if __name__ == "__main__":'))
    parts.append("\n# ==== from spec_harness/eval_spec.py ====\n")
    parts.append(_body(os.path.join(SRC, "eval_spec.py"), None))
    ev = _body(os.path.join(SRC, "eval_llm_response.py"), "\ndef main(")
    ev = ev.replace("def evaluate_problem(", "def evaluate_problem_detailed(")
    ev = ev.replace("result = evaluate_problem(", "result = evaluate_problem_detailed(")
    parts.append("\n# ==== from spec_harness/eval_llm_response.py ====\n")
    parts.append(ev)
    parts.append(ADAPTER)
    return "".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="only check that the copies are up to date")
    a = ap.parse_args()
    code = build()
    stale = []
    for path, extra in TARGETS.items():
        content = code
        if extra:
            content = content.replace("\nimport os\n", "\nimport os\n" + extra, 1)
        if a.check:
            if not os.path.exists(path) or open(path).read() != content:
                stale.append(path)
        else:
            with open(path, "w") as fh:
                fh.write(content)
            print(f"wrote {os.path.relpath(path, ROOT)}")
    if a.check:
        for p in stale:
            print(f"STALE: {os.path.relpath(p, ROOT)}")
        return 1 if stale else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
