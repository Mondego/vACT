#!/usr/bin/env bash
# Score one baseline output file (responses.jsonl, the output of a run script above) with Spec-Harness.
# Run from the repository root:
#   experiments/scripts/run_spec_harness.sh <responses.jsonl> [benchmark.json] [output-dir]
# --max-pairs: number of input/output test pairs per task (5 in the study). The study's full RQ3 scoring
# uses experiments/runners/rq3_run.py on the v2 evaluation suites instead (see experiments/README.md).
set -euo pipefail
RESP=${1:?usage: $0 <responses.jsonl> [benchmark.json] [output-dir]}
BENCH=${2:-benchmarks/specgenbench/sgb.json}
OUT=${3:-results/spec_harness/$(basename "$RESP" .jsonl)}
python -m spec_harness.eval_llm_response \
  --benchmark_path "$BENCH" \
  --llm_response_path "$RESP" \
  --openjml openjml \
  --output "$OUT" \
  --threads "${THREADS:-8}" \
  --max-pairs 5 \
  --verbose
