#!/usr/bin/env python3
"""
Generate expected outputs for void benchmark methods that modify an array argument
in place, by running the reference implementation (test augmentation).

Some SpecGenBench tasks are void methods (e.g. in-place sorts) whose Test.java
prints nothing ("// void - no output"), so every recorded output is empty and
Spec-Harness has nothing to check the postcondition against. For each such task
this script parses every input with the harness's own input parser, calls the
reference `solve` on it through a generated Java driver, and records the final
content of the modified array as the output.

Only the recorded outputs change; the benchmark programs and inputs are untouched.
The result is written to a NEW file (default: <benchmark>_aug.json) with a
provenance note on every changed task.

Usage (from the repository root):
    python spec_harness/augment_void_outputs.py --benchmark benchmarks/specgenbench/sgb.json
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from spec_harness.harness import JavaMethodParser, JavaLiteralRenderer  # noqa: E402
from spec_harness.eval_spec import detect_input_format, parse_input  # noqa: E402

MARK = "<<<CASE"


def augment_task(task: dict, java_bin: str, javac_bin: str) -> tuple[int, int]:
    parsed = JavaMethodParser().parse(task["code"])
    if parsed["return_type"].base != "void":
        return 0, 0
    arrays = [p for p in parsed["params"] if p["jtype"].is_array and p["jtype"].array_dims == 1]
    pairs = task.get("test_inputs", []) + task.get("generated_test_cases", [])
    if not arrays or any(p["output"].strip() for p in pairs):
        return 0, 0                              # nothing to do (or outputs already recorded)
    target = arrays[0]
    ops = detect_input_format(task.get("test_code", ""), parsed["params"])
    rend = JavaLiteralRenderer()
    cases, calls = [], []
    for i, pair in enumerate(pairs):
        try:
            inputs = parse_input(pair["input"], ops)
        except Exception:
            continue
        decls = []
        for j, p in enumerate(parsed["params"]):
            decls.append(f"{p['jtype'].java_decl()} a{j} = {rend.render(p['jtype'], inputs[p['name']])};")
        tj = [p["name"] for p in parsed["params"]].index(target["name"])
        args = ", ".join(f"a{j}" for j in range(len(parsed["params"])))
        if target["jtype"].base in ("char", "Character"):
            show = f"new String(a{tj})"
        else:
            show = (f"java.util.Arrays.stream(a{tj}).mapToObj(String::valueOf)"
                    f".collect(java.util.stream.Collectors.joining(\" \"))")
        calls.append(f"        try {{ {' '.join(decls)} Solution.{parsed['method_name']}({args}); "
                     f"System.out.println(\"{MARK}{i}>>>\" + {show}); }} catch (Throwable t) {{ "
                     f"System.out.println(\"{MARK}{i}>>>!ERR \" + t); }}")
        cases.append(i)
    work = tempfile.mkdtemp(prefix="augvoid_")
    try:
        with open(os.path.join(work, "Solution.java"), "w") as fh:
            fh.write(task["code"])
        with open(os.path.join(work, "Driver.java"), "w") as fh:
            fh.write("public class Driver {\n    public static void main(String[] args) {\n"
                     + "\n".join(calls) + "\n    }\n}\n")
        subprocess.run([javac_bin, "Solution.java", "Driver.java"], cwd=work, check=True,
                       capture_output=True, text=True)
        out = subprocess.run([java_bin, "-cp", ".", "Driver"], cwd=work, capture_output=True,
                             text=True, timeout=600).stdout
    finally:
        shutil.rmtree(work, ignore_errors=True)
    got = {}
    for line in out.splitlines():
        if line.startswith(MARK):
            k, _, v = line[len(MARK):].partition(">>>")
            if not v.startswith("!ERR"):
                got[int(k)] = v
    filled = 0
    for i, pair in enumerate(pairs):
        if i in got:
            pair["output"] = got[i]
            filled += 1
    task["output_augmentation"] = (
        f"outputs of void method recorded as the final content of argument "
        f"'{target['name']}' by running the reference implementation "
        f"(spec_harness/augment_void_outputs.py); {filled}/{len(pairs)} pairs")
    return filled, len(pairs)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--benchmark", required=True)
    ap.add_argument("--out", default=None)
    ap.add_argument("--java", default="java")
    ap.add_argument("--javac", default="javac")
    a = ap.parse_args()
    tasks = json.load(open(a.benchmark))
    for t in tasks:
        filled, total = augment_task(t, a.java, a.javac)
        if total:
            print(f"{t['task_id']}: filled {filled}/{total} outputs")
    out = a.out or a.benchmark.replace(".json", "_aug.json")
    with open(out, "w") as fh:
        json.dump(tasks, fh, indent=1)
    print(f"-> {out}")


if __name__ == "__main__":
    main()
