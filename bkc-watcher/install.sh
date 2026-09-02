#!/bin/sh
# Install the weekly bkc-watcher job. Re-running is safe: it replaces the
# existing schedule rather than stacking a second one.
#
#   ./install.sh              Monday 09:00
#   ./install.sh 7 30         Monday 07:30
#   ./install.sh 9 0 3        Wednesday 09:00   (0=Sun 1=Mon ... 6=Sat)
set -eu

DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.hadarfisher.bkc-watcher"
HOUR="${1:-9}"
MINUTE="${2:-0}"
WEEKDAY="${3:-1}"

PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
    echo "error: python3 not found on PATH." >&2
    echo "       On macOS, install the Command Line Tools: xcode-select --install" >&2
    exit 1
fi
echo "python3:  $PY"
echo "watcher:  $DIR"
echo "schedule: weekday=$WEEKDAY at $(printf '%02d:%02d' "$HOUR" "$MINUTE") local time"
echo

case "$(uname -s)" in
Darwin)
    PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
    mkdir -p "$HOME/Library/LaunchAgents"
    sed -e "s|__PYTHON__|$PY|g" \
        -e "s|__DIR__|$DIR|g" \
        -e "s|__HOUR__|$HOUR|g" \
        -e "s|__MINUTE__|$MINUTE|g" \
        -e "s|__WEEKDAY__|$WEEKDAY|g" \
        "$DIR/$LABEL.plist.template" > "$PLIST"

    # Drop any previous copy first, so we never end up with two schedules.
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || \
        launchctl unload "$PLIST" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || \
        launchctl load "$PLIST"

    echo "installed: $PLIST"
    launchctl list | grep "$LABEL" || echo "(not listed yet - check with: launchctl list | grep $LABEL)"
    ;;
Linux)
    if ! command -v crontab >/dev/null 2>&1; then
        echo "error: no crontab command on this machine, so there is nothing to" >&2
        echo "       install into. Install cron (e.g. 'apt install cron'), or run" >&2
        echo "       check.py from whatever scheduler this box does use:" >&2
        echo "         $PY $DIR/check.py" >&2
        exit 1
    fi
    LINE="$MINUTE $HOUR * * $WEEKDAY cd $DIR && $PY $DIR/check.py >> $DIR/cron.log 2>&1"
    ( crontab -l 2>/dev/null | grep -v "bkc-watcher/check.py" || true; echo "$LINE" ) | crontab -
    echo "installed crontab line:"
    echo "  $LINE"
    ;;
*)
    echo "error: unsupported platform $(uname -s). Schedule check.py yourself." >&2
    exit 1
    ;;
esac

echo
echo "Done. Test the notification now with:  $PY $DIR/check.py --force-notify"
