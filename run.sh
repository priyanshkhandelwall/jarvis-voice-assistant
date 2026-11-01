#!/usr/bin/env bash
# Start JARVIS on macOS. Run setup.sh once first, then: bash run.sh
cd "$(dirname "$0")" || exit 1
if [ ! -x "venv/bin/python" ]; then
    echo "JARVIS isn't set up yet. Run: bash setup.sh   then: bash run.sh"
    exit 1
fi
exec venv/bin/python main.py
