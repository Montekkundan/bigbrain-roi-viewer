#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd "$(dirname "$0")" && pwd)"
cd "$project_dir"
mkdir -p .cache/pip tmp logs
export PIP_CACHE_DIR="$project_dir/.cache/pip"
"${PYTHON:-python3}" -c 'import sys; sys.version_info >= (3, 12) or sys.exit("Python 3.12+ is required. Try PYTHON=python3.13 bash setup.sh")'
"${PYTHON:-python3}" -m venv --copies .venv
.venv/bin/python -m pip install -e .
if command -v dot_clean >/dev/null 2>&1; then
  dot_clean -m "$project_dir/.venv"
fi
.venv/bin/python -m pip check
echo "Ready. Run bash 'Start Demo.command' or .venv/bin/python -m openmind prepare."
