#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd "$(dirname "$0")" && pwd)"
cd "$project_dir"
mkdir -p logs tmp .cache/matplotlib
export PYTHONPATH="$project_dir/src${PYTHONPATH:+:$PYTHONPATH}"
export TMPDIR="$project_dir/tmp"
export XDG_CACHE_HOME="$project_dir/.cache"
export MPLCONFIGDIR="$project_dir/.cache/matplotlib"
export PIP_CACHE_DIR="$project_dir/.cache/pip"
if command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:8081 -sTCP:LISTEN >/dev/null 2>&1; then
  echo 'Port 8081 is already in use. The demo may already be running at http://127.0.0.1:8081.'
  if [[ "${1:-}" != --no-browser ]] && command -v open >/dev/null 2>&1; then
    open http://127.0.0.1:8081
  fi
  exit 0
fi
if [[ ! -f data/bigbrain-cube/catalog.json ]]; then
  .venv/bin/python -u -m openmind prepare --config configs/mac-demo.json --output data/bigbrain-cube
fi
if [[ ! -f data/bigbrain-1um-cube/catalog.json ]]; then
  .venv/bin/python -u -m openmind prepare --config configs/mac-1um-demo.json --output data/bigbrain-1um-cube
fi
printf '%s\n' "$$" > logs/demo.pid
echo 'Open the Mind: http://127.0.0.1:8081'
echo 'Keep this terminal open. Press Control+C to stop.'
if [[ "${1:-}" == --no-browser ]]; then
  exec .venv/bin/python -u -m openmind serve --catalog data/bigbrain-1um-cube/catalog.json --compare-catalog data/bigbrain-cube/catalog.json --port 8081
fi
exec .venv/bin/python -u -m openmind serve --catalog data/bigbrain-1um-cube/catalog.json --compare-catalog data/bigbrain-cube/catalog.json --port 8081 --open-browser
