#!/bin/bash
# Double-click to start Scout Finder (macOS)
cd "$(dirname "$0")" || exit 1
clear
echo "⚽  Scout Finder"
echo ""
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "Python 3.10+ is needed. Opening the download page..."
  echo "Install it, then double-click this file again."
  open "https://www.python.org/downloads/macos/"
  read -n 1 -s -r -p "Press any key to close."
  exit 1
fi
if [ ! -x ".venv/bin/python" ]; then
  echo "First start: setting up (about 1 minute)..."
  "$PY" -m venv .venv || { echo "Could not create the environment."; read -n 1 -s; exit 1; }
fi
if [ ! -f ".venv/.installed" ] || [ requirements.txt -nt ".venv/.installed" ]; then
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet -r requirements.txt || { echo "Install failed. Check your internet connection."; read -n 1 -s; exit 1; }
  touch .venv/.installed
fi
.venv/bin/python app.py
