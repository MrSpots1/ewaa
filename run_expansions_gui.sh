#!/usr/bin/env bash
# Opens the expansion progress window and restarts it if it crashes.
# Progress is saved to the checkpoint, so each restart resumes where it left off.
# Usage: ./run_expansions_gui.sh [--input FILE] [--outdir DIR]
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
PY="$HOME/.local/share/ewaa-gui-venv/bin/python"
MAX_RESTARTS=5
restarts=0

while true; do
  "$PY" "$HERE/expansion_gui.py" "$@"
  code=$?

  case $code in
    0) exit 0 ;;
    3) exit 0 ;;
  esac

  restarts=$((restarts + 1))
  if [ "$restarts" -gt "$MAX_RESTARTS" ]; then
    notify-send "EWAA" "Window crashed $restarts times. Giving up."
    exit 1
  fi
  notify-send "EWAA" "Window crashed (exit $code). Restarting ($restarts/$MAX_RESTARTS)."
  sleep 3
done
