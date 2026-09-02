#!/bin/sh
# Remove the weekly job. Leaves check.py, snapshots and logs untouched.
set -eu
DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.hadarfisher.bkc-watcher"

case "$(uname -s)" in
Darwin)
    PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || \
        launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "removed: $PLIST"
    ;;
Linux)
    crontab -l 2>/dev/null | grep -v "bkc-watcher/check.py" | crontab - || true
    echo "removed the bkc-watcher crontab line"
    ;;
esac
echo "The job is gone. Your snapshots and logs in $DIR are untouched."
