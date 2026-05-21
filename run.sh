#!/bin/bash
# F3800 Monitor launcher — uses the project's .venv Python automatically
# Default: 5-minute polling. Override with --interval N (minutes).
# Usage:  ./run.sh               (default 5-min polling)
#         ./run.sh --interval 10 (10-min polling)
#         ./run.sh -v            (verbose/debug logging)
#         ./run.sh --headless    (no live display, for background)

cd "$(dirname "$0")"

if [ ! -f .venv/bin/python ]; then
    echo "❌ Virtual environment not found at .venv/"
    echo "   Run this first:  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    exit 1
fi

# Default to 5-minute polling unless user specifies otherwise
if [[ "$*" != *"--interval"* ]]; then
    .venv/bin/python f3800_monitor.py --interval 5 "$@"
else
    .venv/bin/python f3800_monitor.py "$@"
fi
