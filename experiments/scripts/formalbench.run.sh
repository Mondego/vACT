#!/usr/bin/env bash
# RQ1 baseline: FormalBench prompts. PROMPT: zero_shot | two_shot | zs_cot | fs_cot | fs_ltm. Set MODEL and PROMPT in the environment (defaults: gpt-4o-2024-08-06, zero_shot).
# Run from the repository root:
#   experiments/scripts/formalbench.run.sh [benchmark.json] [output-dir] [run-name]
# Defaults: benchmarks/specgenbench/sgb.json, results/rq1, and the run name <bench>_run (e.g. sgb_run).
set -euo pipefail
BENCH=${1:-benchmarks/specgenbench/sgb.json}
OUT=${2:-results/rq1}
NAME=${3:-"$(basename "$BENCH" .json | cut -d_ -f1)_run"}
python -m baselines.formalbench.run \
    --name "$NAME" \
    --input "$BENCH" \
    --output "$OUT" \
    --model "${MODEL:-gpt-4o-2024-08-06}" \
    --temperature 0.7 \
    --prompt_type "${PROMPT:-zero_shot}" \
    --max_iterations 3 \
    --openjml_timeout 300 \
    --threads "${THREADS:-8}" \
    --verbose
