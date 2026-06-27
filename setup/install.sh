#!/bin/bash
# Install launchd plists and wrapper scripts for the F3800 monitor.
# Run from the project root:  bash setup/install.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
USER="$(/usr/bin/whoami)"
HOME_DIR="$(eval echo ~$USER)"

echo "Installing F3800 monitor setup files..."
echo "  Project: $PROJECT_DIR"
echo "  User:    $USER"
echo ""

# --- Wrapper scripts ---
mkdir -p "$HOME_DIR/.local/bin"

for script in f3800-monitor f3800-daily-summary; do
    SRC="$SCRIPT_DIR/$script"
    DST="$HOME_DIR/.local/bin/$script"
    if [ ! -f "$SRC" ]; then
        echo "  [SKIP] $script not found in setup/"
        continue
    fi
    # Update the project path inside the wrapper to match this machine
    sed "s|__PROJECT_DIR__|$PROJECT_DIR|g" "$SRC" > "$DST"
    chmod +x "$DST"
    echo "  [OK]   $DST"
done

# --- launchd plists ---
PLIST_DIR="$HOME_DIR/Library/LaunchAgents"
mkdir -p "$PLIST_DIR"

for plist in com.anker-f3800.monitor com.anker-f3800.daily-summary com.anker-f3800.caffeinate; do
    SRC="$SCRIPT_DIR/$plist.plist"
    DST="$PLIST_DIR/$plist.plist"
    if [ ! -f "$SRC" ]; then
        echo "  [SKIP] $plist.plist not found in setup/"
        continue
    fi
    # Update the username in the plist to match this machine
    sed "s|__USER__|$USER|g" "$SRC" > "$DST"
    echo "  [OK]   $DST"
done

# --- Log directory ---
LOG_DIR="$HOME_DIR/Library/Logs/anker-f3800"
mkdir -p "$LOG_DIR"
echo "  [OK]   $LOG_DIR (log directory)"

# --- Reload launchd jobs ---
echo ""
echo "Reloading launchd jobs..."
for plist in com.anker-f3800.caffeinate com.anker-f3800.monitor com.anker-f3800.daily-summary; do
    launchctl bootout "gui/$(id -u)/$plist" 2>/dev/null || true
    launchctl load "$PLIST_DIR/$plist.plist" 2>&1 && echo "  [OK]   $plist loaded" || echo "  [FAIL] $plist"
done

echo ""
echo "Done. Verify with:"
echo "  launchctl list | grep anker"
echo "  tail -f ~/Library/Logs/anker-f3800/monitor.err"
