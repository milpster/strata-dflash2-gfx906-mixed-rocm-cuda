#!/usr/bin/env bash
# Capture the r7 supervisor's next helper spawn: cmdline + env, then leave it alone.
set -u
for i in $(seq 1 40); do
  P=$(pgrep -f "strata-expert-helper --pack" | head -1)
  if [ -n "${P:-}" ]; then
    echo "== helper pid $P"
    tr '\0' ' ' < "/proc/$P/cmdline"; echo
    echo "-- env:"
    tr '\0' '\n' < "/proc/$P/environ" | grep -E "^(PATH|HOME|TERM|STRATA|LD_)" | head -12
    exit 0
  fi
  sleep 2
done
echo "no helper spawn seen in 80 s"
