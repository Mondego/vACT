#!/usr/bin/env bash
# Build the portable bundle for the RQ3 / RQ4 machines (run on the reference machine AFTER the test-set
# build and after vACT-phase0 has been reviewed and committed, fse_todo A7).
#   runners/pack.sh [output-dir]      -> <output-dir>/spec_harness_bundle_<commit>_<date>.tar.gz
# Contents: the vACT-phase0 snapshot (code + v2 suites), inputs/rq1 (slim RQ1 files), requirements.txt,
# BUNDLE_MANIFEST.sha256 (every result records its hash). OpenJML is NOT included: every machine has
# `openjml` (21-0.21) on PATH.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="$(cd "$REPO/.." && pwd)"
OUT="${1:-$ROOT/bundles}"
COMMIT="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo nogit)"
DIRTY="$(git -C "$REPO" status --porcelain 2>/dev/null | wc -l)"
if [ "$DIRTY" != "0" ]; then echo "WARNING: vACT-phase0 has $DIRTY uncommitted changes (A7: review and commit first)"; fi
NAME="spec_harness_bundle_${COMMIT}_$(date +%Y%m%d)"
STAGE="$(mktemp -d)/$NAME"
mkdir -p "$STAGE" "$OUT"
rsync -a --exclude .git --exclude __pycache__ --exclude '*.pyc' --exclude 'rq3_out' --exclude 'rq4_out' "$REPO/" "$STAGE/"
for f in sgb_v2_eval sgb_v2_feedback; do test -s "$STAGE/benchmarks/specgenbench/$f.json" || { echo "missing suite $f"; exit 1; }; done
for f in fb_v2_eval fb_120_v2_eval fb_120_v2_feedback; do test -s "$STAGE/benchmarks/formalbench/$f.json" || { echo "missing suite $f"; exit 1; }; done
test -s "$STAGE/runners/reference/smoke_verdicts.json" || { echo "missing runners/reference/smoke_verdicts.json (run preflight --record-reference)"; exit 1; }
python3 "$REPO/runners/slim_rq1.py" "$ROOT/artifacts/rq1" "$STAGE/inputs/rq1"
# runtime dependencies = the package's own (pyproject.toml), so RQ4's VeriAct runs too
python3 - "$REPO/pyproject.toml" > "$STAGE/requirements.txt" <<'PY'
import re, sys
deps = re.search(r"dependencies\s*=\s*\[(.*?)\]", open(sys.argv[1]).read(), re.S).group(1)
print("\n".join(re.findall(r'"([^"]+)"', deps)))
PY
(cd "$STAGE" && find . -type f ! -name BUNDLE_MANIFEST.sha256 -print0 | sort -z | xargs -0 sha256sum > BUNDLE_MANIFEST.sha256)
echo "commit=$COMMIT uncommitted_changes=$DIRTY built=$(date -Is) host=$(hostname)" > "$STAGE/BUNDLE_INFO"
tar -C "$(dirname "$STAGE")" -czf "$OUT/$NAME.tar.gz" "$NAME"
echo "bundle: $OUT/$NAME.tar.gz ($(du -h "$OUT/$NAME.tar.gz" | cut -f1)), manifest hash $(sha256sum "$STAGE/BUNDLE_MANIFEST.sha256" | cut -c1-16)"
