#!/usr/bin/env bash
# RQ1 baseline: Houdini (needs Java 1.6.0_21, see baselines/houdini/README.md).
# Run from the repository root:
#   experiments/scripts/houdini.run.sh [benchmark.json] [output-dir] [run-name]
# Defaults: benchmarks/specgenbench/sgb.json, results/rq1, and the run name <bench>_run (e.g. sgb_run).
set -euo pipefail
BENCH=${1:-benchmarks/specgenbench/sgb.json}
OUT=${2:-results/rq1}
NAME=${3:-"$(basename "$BENCH" .json | cut -d_ -f1)_run"}
python -m baselines.houdini.run \
    --name "$NAME" \
    --input "$BENCH" \
    --output "$OUT" \
    --openjml_timeout 300 \
    --threads "${THREADS:-8}" \
    --verbose
