#!/usr/bin/env bash
# RQ2: GEPA prompt optimization (GPT-4o as generating and reflecting model).
# Run from the repository root:
#   experiments/scripts/run_optimizer.sh [seed] [output-dir]
#   seed: zero | cot | ltm (the zero-shot, few-shot CoT and few-shot least-to-most seed prompts); default zero
set -euo pipefail
SEED=${1:-zero}
OUT=${2:-results/rq2/$SEED}
python -m optimizer.prompt_optimizer \
  --formalbench_path benchmarks/formalbench/fb.json \
  --specgenbench_path benchmarks/specgenbench/sgb.json \
  --optimizers gepa \
  --best_seed "$SEED" \
  --model openai/gpt-4o \
  --reflection_model openai/gpt-4o \
  --log_dir "$OUT/optimizer_logs" \
  --output_dir "$OUT/optimizer_results" \
  --openjml_output_dir "$OUT/openjml_output"
