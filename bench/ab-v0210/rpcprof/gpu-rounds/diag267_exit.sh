#!/usr/bin/env bash
# diag267_exit.sh — boot the diag267 engine shape and capture WHY it dies:
# exit status (128+N = signal N), boot log, and timing. No driving, no gdb.
# Usage: diag267_exit.sh <out_dir>
set -u
OUT="$1"; mkdir -p "$OUT"
CFG=/mnt/srcds/Strata/bench/ab-v0210/quality/diag267.json
python3 - "$CFG" > "$OUT/boot.cmd" <<'PY'
import json, shlex, sys
c = json.load(open(sys.argv[1]))
print(shlex.join([c["exe"]] + c.get("args", [])))
PY
eval "$(python3 - "$CFG" <<'PY'
import json, shlex, sys
c = json.load(open(sys.argv[1]))
for k, v in c.get("env", {}).items():
    print(f"export {k}={shlex.quote(str(v))}")
PY
)"
T0=$(date +%s)
bash $(cat "$OUT/boot.cmd") > "$OUT/engine.stderr.txt" 2>&1
RC=$?
T1=$(date +%s)
{
  echo "exit=$RC elapsed=$((T1-T0))s date=$(date)"
  tail -c 400 "$OUT/engine.stderr.txt" | tr -d '\0'
} | tee "$OUT/exit_report.txt"
# 139 = SIGSEGV, 135 = SIGBUS, 137 = SIGKILL, 134 = SIGABRT, 0 = clean
