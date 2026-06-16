#!/usr/bin/env bash
# Music World launcher. Checks apt dependencies, then starts the web app.
set -euo pipefail
cd "$(dirname "$0")"

missing=()
python3 -c "import flask"    2>/dev/null || missing+=("python3-flask")
python3 -c "import requests" 2>/dev/null || missing+=("python3-requests")

if [ ${#missing[@]} -gt 0 ]; then
  echo "Missing apt packages: ${missing[*]}"
  echo "Install them with:"
  echo "    sudo apt install ${missing[*]}"
  exit 1
fi

HOST="${MUSIC_WORLD_HOST:-127.0.0.1}"
PORT="${MUSIC_WORLD_PORT:-5000}"
echo "Starting Music World on http://${HOST}:${PORT}"
exec python3 app.py
