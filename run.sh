#!/bin/bash
# F3800 Monitor launcher — uses the project's .venv Python automatically
# Usage:  ./run.sh              (default 10-min polling)
#         ./run.sh --interval 5 (5-min polling)
#         ./run.sh -v           (verbose/debug logging)

cd "$(dirname "$0")"

if [ ! -f .venv/bin/python ]; then
    echo "❌ Virtual environment not found at .venv/"
    echo "   Run this first:  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    exit 1
fi

.venv/bin/python f3800_monitor.py "$@"
