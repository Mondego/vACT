#!/usr/bin/env bash
# Resolve a Python interpreter that can `import javalang`. Prefers ONE shared venv
# at the out-root (created once at scaffold time) so each task session just loads
# it instead of building its own. Sets and exports $PYTHON.
# Sourced by the task-dir wrapper scripts (verify.sh, run_specharness.sh, ...).
set -euo pipefail

# Cap OpenJML's JVM heap. Without this the JVM defaults its max heap to ~1/4 of
# physical RAM (huge on big machines), and N parallel verifies multiply it.
export OPENJML_JVM="${OPENJML_JVM:--Xmx8g}"

_HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# <out-root>/<task>/harness -> <out-root>
_OUT_ROOT="$(cd "$_HARNESS_DIR/../.." && pwd)"
_SHARED_VENV="$_OUT_ROOT/.venv"

_has_javalang() { "$1" -c 'import javalang' >/dev/null 2>&1; }

if [ -n "${VACT_PYTHON:-}" ]; then
  PYTHON="$VACT_PYTHON"
elif [ -x "$_SHARED_VENV/bin/python" ]; then
  PYTHON="$_SHARED_VENV/bin/python"
elif _has_javalang python; then
  PYTHON="python"
elif _has_javalang python3; then
  PYTHON="python3"
else
  # Build the single shared venv once (scaffold usually did this already).
  # Guard against parallel sessions racing on first run.
  echo "[agent_harness] creating shared venv at $_SHARED_VENV ..." >&2
  if command -v flock >/dev/null 2>&1; then
    exec 9>"$_OUT_ROOT/.venv.lock"
    flock 9
  fi
  if [ ! -x "$_SHARED_VENV/bin/python" ]; then
    if command -v uv >/dev/null 2>&1; then
      uv venv "$_SHARED_VENV" >&2
      uv pip install --python "$_SHARED_VENV/bin/python" \
        -r "$_HARNESS_DIR/requirements.txt" >&2
    else
      python3 -m venv "$_SHARED_VENV" >&2
      "$_SHARED_VENV/bin/python" -m pip install -q --upgrade pip >&2
      "$_SHARED_VENV/bin/python" -m pip install -q \
        -r "$_HARNESS_DIR/requirements.txt" >&2
    fi
  fi
  PYTHON="$_SHARED_VENV/bin/python"
fi

export PYTHON
