#!/usr/bin/env bash
# watch-panel.sh - live engine + panel activity for the operator (no agent involved).
#
# usage: watch-panel.sh [ELOG]
#   no argument: newest engine log in /mnt/srcds/Strata/logs (engine-*.log / eng-*.log)
#
# Streams the engine log (NUL-stripped) with tok/s DONE lines, swap-in storms and
# errors colorized, merged with any running panel's leg progress (panel.sh tees to
# out/*/panel-progress.log). Run it in your own terminal whenever a window is on;
# Ctrl-C stops the view, nothing else.
set -uo pipefail
LOGS_DIR=/mnt/srcds/Strata/logs
RPC=/mnt/srcds/Strata/bench/ab-v0210/rpcprof

LOG=${1:-$(ls -t "$LOGS_DIR"/engine-*.log "$LOGS_DIR"/eng-*.log 2>/dev/null | head -1)}
[ -n "${LOG:-}" ] || { echo "watch-panel: no engine log in $LOGS_DIR"; exit 1; }

PANEL_LOG=$(ls -t "$RPC"/out/*/panel-progress.log 2>/dev/null | head -1)
echo "== watching engine: $LOG"
[ -n "$PANEL_LOG" ] && echo "== panel progress: $PANEL_LOG"
echo "== Ctrl-C stops the view only. $(date '+%H:%M:%S')"

# merge engine log + panel progress by arrival, both tailed from their last 10 lines
{
  tail -n 10 -F "$LOG" 2>/dev/null | sed -u 's/\x00//g' | awk '
    /tok\/s/                          { print "\033[1;32m" $0 "\033[0m"; fflush(); next }
    /adapt swap-in/                   { print "\033[1;33m" $0 "\033[0m"; fflush(); next }
    /[Ee]rror|FATAL|refused|watchdog/ { print "\033[1;31m" $0 "\033[0m"; fflush(); next }
    { print; fflush() }'
  [ -n "$PANEL_LOG" ] && tail -n 10 -F "$PANEL_LOG" 2>/dev/null | awk '
    /^=== /      { print "\033[1;36m" $0 "\033[0m"; fflush(); next }
    /panel done/ { print "\033[1;35m" $0 "\033[0m"; fflush(); next }
    { print; fflush() }'
} | awk '{ print strftime("%H:%M:%S ") $0; fflush() }'
