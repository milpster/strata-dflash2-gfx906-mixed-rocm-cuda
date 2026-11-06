#!/usr/bin/env bash
# Execute the real launcher, but intercept supervision and forbid GPU startup.
set -euo pipefail
exec() {
  builtin printf '%s\n' "$@" > "$PAIR_EXEC_CAPTURE"
  exit 0
}
ss() { builtin printf 'test occupied port\n'; }
env() { builtin printf 'GPU startup forbidden in launcher guard test\n' >&2; exit 97; }
export -f exec ss env
export STRATA_KSM=0
builtin exec bash "$@"
