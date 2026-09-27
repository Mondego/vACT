#!/usr/bin/env python3
"""
Fig. 3 (RQ1): Verification Rate (VR) heatmaps, one per benchmark.

  VR(cell) = #records with status == "verified" AND final_error == ""  /  benchmark size
  benchmark size is FIXED: SpecGenBench = 120, FormalBench = 662 (whatever a file contains; a few RQ1
  files lack some tasks, which then count as not verified). A "verified" record with a non-empty
  final_error is an OpenJML internal crash / prover noise, not a verification.

  rows    = the 11 prompt-based configurations (approach x prompt type); Daikon and Houdini are not in
            Fig. 3 (their VR rows are still written to the CSV, in_fig3 = False, for Table II)
  columns = the 6 language models

Two steps (figures are drawn only from the CSV):
  python figures/fig3_vr.py csv  [--rq1-dir ../artifacts/rq1] [--out ../results/rq1]   -> rq1_vr.csv
  python figures/fig3_vr.py plot [--out ../results/rq1]                                  -> fig3_vr_{sgb,fb}.{pdf,png}
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))                   # the project dir that holds artifacts/
DENOM = {"sgb": 120, "fb": 662}
BENCH_LABEL = {"sgb": "SpecGenBench", "fb": "FormalBench"}

# row order and labels (approach, prompt) -> label
ROWS = [("specgen", "zero_shot", "SG-ZS"), ("specgen", "two_shot", "SG-2S"), ("specgen", "four_shot", "SG-4S"),
        ("autospec", "zero_shot", "AS-ZS"), ("autospec", "two_shot", "AS-2S"), ("autospec", "four_shot", "AS-4S"),
        ("formalbench", "zero_shot", "FB-ZS"), ("formalbench", "two_shot", "FB-2S"),
        ("formalbench", "zs_cot", "FB-ZS-CoT"), ("formalbench", "fs_cot", "FB-FS-CoT"),
        ("formalbench", "fs_ltm", "FB-FS-LtM")]
# column order: proprietary, then open-weight
MODELS = [("claude-sonnet-4-6", "Claude Sonnet 4.6"), ("gpt-4o-2024-08-06", "GPT-4o"),
          ("gemini-2.0-flash", "Gemini 2.0 Flash"), ("Qwen_Qwen2.5-Coder-32B-Instruct", "Qwen2.5-Coder-32B"),
          ("deepseek-ai_deepseek-coder-33b-instruct", "DeepSeek-Coder-33B"),
          ("codellama_CodeLlama-34b-Instruct-hf", "CodeLlama-34B")]


def parse(stem):
    p = stem.split("__") + ["", "", ""]
    return p[0], p[1].replace("_run", ""), p[2], p[3].replace("two-shot", "two_shot")


def cmd_csv(a):
    rows = []
    row_label = {(ap, pr): lab for ap, pr, lab in ROWS}
    model_label = dict(MODELS)
    for f in sorted(glob.glob(os.path.join(a.rq1_dir, "*.jsonl"))):
        approach, bench, model, prompt = parse(os.path.basename(f)[:-6])
        n = verified = with_err = 0
        for line in open(f):
            r = json.loads(line)
            n += 1
            if r.get("status") == "verified":
                if (r.get("final_error") or "") == "":
                    verified += 1
                else:
                    with_err += 1
        in_fig3 = (approach, prompt) in row_label and model in model_label
        rows.append({"bench": bench, "approach": approach, "prompt": prompt, "model": model,
                     "row_label": row_label.get((approach, prompt), approach),
                     "model_label": model_label.get(model, model), "in_fig3": in_fig3,
                     "verified": verified, "denominator": DENOM[bench], "vr": round(verified / DENOM[bench], 4),
                     "records_in_file": n, "verified_with_final_error": with_err,
                     "file": os.path.basename(f)})
    os.makedirs(a.out, exist_ok=True)
    path = os.path.join(a.out, "rq1_vr.csv")
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    fig = [r for r in rows if r["in_fig3"]]
    print(f"{len(rows)} rows ({len(fig)} in Fig. 3) -> {path}")
    for b in ("sgb", "fb"):
        for ap in ("specgen", "autospec", "formalbench"):
            best = max((r for r in fig if r["bench"] == b and r["approach"] == ap), key=lambda r: r["vr"])
            print(f"  best {BENCH_LABEL[b]:12s} {ap:11s} {best['verified']}/{best['denominator']} "
                  f"({100 * best['vr']:.1f}%)  {best['row_label']} x {best['model_label']}")
    return 0


def cmd_plot(a):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    rows = list(csv.DictReader(open(os.path.join(a.out, "rq1_vr.csv"))))
    for b in ("sgb", "fb"):
        grid = np.full((len(ROWS), len(MODELS)), np.nan)
        for r in rows:
            if r["bench"] != b or r["in_fig3"] != "True":
                continue
            i = next(k for k, (ap, pr, _) in enumerate(ROWS) if (ap, pr) == (r["approach"], r["prompt"]))
            j = next(k for k, (m, _) in enumerate(MODELS) if m == r["model"])
            grid[i, j] = 100 * float(r["vr"])
        fig, ax = plt.subplots(figsize=(6.4, 5.0))
        im = ax.imshow(grid, cmap="Blues", vmin=0, vmax=100, aspect="auto")      # one hue, light -> dark
        for i in range(grid.shape[0]):
            for j in range(grid.shape[1]):
                if not np.isnan(grid[i, j]):
                    ax.text(j, i, f"{grid[i, j]:.1f}", ha="center", va="center", fontsize=8,
                            color="white" if grid[i, j] >= 55 else "#1f2328")
        ax.set_xticks(range(len(MODELS)), [lab for _, lab in MODELS], rotation=30, ha="right", fontsize=8)
        ax.set_yticks(range(len(ROWS)), [lab for _, _, lab in ROWS], fontsize=8)
        for y in (2.5, 5.5):                                                    # separate the three approaches
            ax.axhline(y, color="white", linewidth=2)
        ax.set_title(f"{BENCH_LABEL[b]}: verification rate (%), n = {DENOM[b]}", fontsize=9)
        ax.tick_params(length=0)
        for s in ax.spines.values():
            s.set_visible(False)
        cb = fig.colorbar(im, ax=ax, fraction=0.04, pad=0.02)
        cb.ax.tick_params(labelsize=7)
        cb.outline.set_visible(False)
        fig.tight_layout()
        for ext in ("pdf", "png"):
            fig.savefig(os.path.join(a.out, f"fig3_vr_{b}.{ext}"), dpi=200)
        plt.close(fig)
        print(f"wrote {a.out}/fig3_vr_{b}.pdf/.png")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("csv")
    c.add_argument("--rq1-dir", default=os.path.join(ROOT, "artifacts", "rq1"))
    c.add_argument("--out", default=os.path.join(ROOT, "results", "rq1"))
    c.set_defaults(func=cmd_csv)
    p = sub.add_parser("plot")
    p.add_argument("--out", default=os.path.join(ROOT, "results", "rq1"))
    p.set_defaults(func=cmd_plot)
    a = ap.parse_args()
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
