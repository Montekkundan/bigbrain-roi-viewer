#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd "$(dirname "$0")" && pwd)"
cd "$project_dir"
if [[ ! -f logs/demo.pid ]]; then
  echo 'No demo PID is recorded.'
  exit 0
fi
demo_pid="$(cat logs/demo.pid)"
if [[ ! "$demo_pid" =~ ^[0-9]+$ ]]; then
  echo 'Invalid PID file; refusing to stop an unrelated process.'
  exit 1
fi
demo_command="$(ps -p "$demo_pid" -o command= || true)"
demo_cwd="$(/usr/sbin/lsof -a -p "$demo_pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' || true)"
if [[ "$demo_command" == *'-m openmind serve'* && "$demo_cwd" == "$project_dir" ]]; then
  kill -TERM "$demo_pid"
  rm logs/demo.pid
  echo 'Demo stopped.'
else
  echo 'The recorded demo is no longer running; no process was stopped.'
fi
