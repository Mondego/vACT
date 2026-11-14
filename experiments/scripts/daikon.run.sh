#!/usr/bin/env bash
# RQ1 baseline: Daikon (JDK 11; set DAIKONDIR, and DAIKON_JAVA / DAIKON_JAVAC if JDK 11 is not the default).
# Run from the repository root:
#   experiments/scripts/daikon.run.sh [benchmark.json] [output-dir] [run-name]
# Defaults: benchmarks/specgenbench/sgb.json, results/rq1, and the run name <bench>_run (e.g. sgb_run).
set -euo pipefail
BENCH=${1:-benchmarks/specgenbench/sgb.json}
OUT=${2:-results/rq1}
NAME=${3:-"$(basename "$BENCH" .json | cut -d_ -f1)_run"}
python -m baselines.daikon.run \
    --name "$NAME" \
    --input "$BENCH" \
    --output "$OUT" \
    --openjml_timeout 300 \
    --daikon_timeout 600 \
    --threads "${THREADS:-2}" \
    --verbose
