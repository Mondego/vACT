#!/usr/bin/env bash
# Score several baseline output files with Spec-Harness, one after the other.
# Run from the repository root:
#   experiments/scripts/run_spec_harness_loop.sh <list.txt> [responses-dir] [benchmark.json] [output-root]
# <list.txt>: one responses .jsonl file name per line (relative to responses-dir); lines starting with # are skipped.
set -euo pipefail
LIST=${1:?usage: $0 <list.txt> [responses-dir] [benchmark.json] [output-root]}
BASE=${2:-results/rq1}
BENCH=${3:-benchmarks/specgenbench/sgb.json}
OUT_ROOT=${4:-results/spec_harness}
mkdir -p "$OUT_ROOT"
while IFS= read -r resp || [[ -n "$resp" ]]; do
  [[ -z "$resp" || "$resp" =~ ^# ]] && continue
  name="$(basename "$resp" .jsonl)"
  python -m spec_harness.eval_llm_response \
    --benchmark_path "$BENCH" \
    --llm_response_path "$BASE/$resp" \
    --openjml openjml \
    --output "$OUT_ROOT/$name" \
    --threads "${THREADS:-12}" \
    --max-pairs 5 \
    --verbose
  pkill -f cvc4-1.6 || true      # leftover solver processes between runs
  sleep 5
done < "$LIST"
