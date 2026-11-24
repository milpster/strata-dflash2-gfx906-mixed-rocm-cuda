#!/usr/bin/env bash
# engine_pid.sh - print the real serving engine pid, never a shell wrapper.
#
# Why: `pgrep -f 'port0218-bin/rpcw-hip/strata --serve'` matches the caller's own
# bash -c command line, so a wrapper pid gets recorded as the engine (it happened
# twice: records named /usr/bin/bash as engine_exe). This requires argv[0] to BE
# the engine binary path.
set -euo pipefail
PAT=${1:-rpcw-hip/strata}
for p in $(ps -eo pid=,args= | awk -v pat="$PAT" '$2 ~ pat && $3=="--serve" {print $1}'); do
  exe=$(readlink "/proc/$p/exe" 2>/dev/null || true)
  case ${exe##*/} in
    strata|strata-*) echo "$p"; exit 0 ;;
  esac
done
exit 1
