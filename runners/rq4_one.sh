#!/usr/bin/env bash
# One RQ4 run, end to end: scaffold (Claude/Codex only) -> agent sessions -> agents again (re-runs ONLY sessions
# that failed: crash, API error, timeout) -> final scoring on the first 8 evaluation pairs -> status.
# Resumable: re-running the same command skips finished sessions and finished scores.
#
#   runners/rq4_one.sh OUT ARM AGENT BENCH [SESSIONS] [JVM_SLOTS]
#     ARM       with_harness | no_harness
#     AGENT     claude | codex | veriact
#     BENCH     sgb | fb
#     SESSIONS  agent sessions in parallel (1-8, default 4)
#     JVM_SLOTS OpenJML JVMs for this run (default 60); each session's tools get JVM_SLOTS / SESSIONS
set -euo pipefail
if [ $# -lt 4 ]; then sed -n '2,13p' "$0"; exit 2; fi
OUT=$1; ARM=$2; AGENT=$3; BENCH=$4; SESSIONS=${5:-4}; SLOTS=${6:-60}
cd "$(dirname "$0")/.."
SEL=(--out "$OUT" --arm "$ARM" --agents "$AGENT" --bench "$BENCH")
echo "== $(date '+%F %T') RQ4 run $ARM/$AGENT/$BENCH: sessions $SESSIONS, JVM slots $SLOTS, out $OUT"
python runners/rq4_run.py scaffold "${SEL[@]}"
python runners/rq4_run.py agents "${SEL[@]}" --jvm-slots "$SLOTS" --sessions "$SESSIONS"
echo "== $(date '+%F %T') retry pass (only failed sessions)"
python runners/rq4_run.py agents "${SEL[@]}" --jvm-slots "$SLOTS" --sessions "$SESSIONS"
echo "== $(date '+%F %T') final scoring (first 8 evaluation pairs)"
python runners/rq4_run.py score "${SEL[@]}" --jvm-slots "$SLOTS" --pairs 8
python runners/rq4_run.py status "${SEL[@]}"
echo "== $(date '+%F %T') RQ4 run $ARM/$AGENT/$BENCH done"
