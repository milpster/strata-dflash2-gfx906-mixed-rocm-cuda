#!/usr/bin/env bash
# Logic walk of stop-all.sh's CHANGED supervisor loop, against a mocked run/ listing.
# It never runs stop-all.sh and never touches a real state file: the sandbox has its own
# run/ directory, the destructive parts (kill -TERM, term_pid) are stubbed to print, and the
# "supervisor" it points at is a throwaway python process that just sleeps.
# Usage: mock-stop-all-walk.sh [path-to-stop-all.sh]
set -uo pipefail
STOP=${1:-/mnt/srcds/Strata/stop-all.sh}
D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
mkdir -p "$D/run" "$D/fake"

# a live, harmless stand-in for a pair supervisor: its cmdline contains pair_supervisor.py
cat > "$D/fake/pair_supervisor.py" <<'EOF'
import time; time.sleep(25)
EOF
python3 "$D/fake/pair_supervisor.py" & FAKE=$!
sleep 1

# three lanes' state files: prod, arena54, spill40 - all pointing at the live stand-in,
# plus a stale one whose pid is dead, and the affinity snapshot a real supervisor writes
# next to its state file (must NOT be matched by the glob)
for name in prod-swift15-f16kv-mixed trial-arena54 trial-spill40; do
  printf '{"supervisor_pid": %s}\n' "$FAKE" > "$D/run/$name-supervisor.json"
done
printf '{"supervisor_pid": 999999}\n' > "$D/run/r7-trial-supervisor.json"          # dead pid
printf '{"supervisor_pid": %s}\n' "$$" > "$D/run/prod-ud-iq3xxs-mixed-supervisor.json"  # live, NOT a supervisor
printf '{"snapshot": 1}\n' > "$D/run/prod-swift15-f16kv-mixed-supervisor-affinity-1-1.json"

echo "sandbox run/ listing:"; ls -1 "$D/run"
echo "live stand-in supervisor pid: $FAKE (cmdline: $(tr '\0' ' ' < /proc/$FAKE/cmdline))"
echo "the harness pid $$ is live but is a bash, not a supervisor"
echo

# stub the destructive parts, then run the loop EXTRACTED VERBATIM from the real file
kill() { # -0 stays real (it only probes); -TERM/-KILL are printed, never sent
  case "$1" in
    -0) command kill -0 "${@:2}" ;;
    *)  echo "    [WOULD ${1#-}] ${*:2}" ;;
  esac
}
term_pid() { echo "    [stub term_pid] would wait up to 60s for pid $1"; }

loop=$(awk '/^for state in run\/\*-supervisor\.json; do$/{f=1} f{print} f&&/^done$/{exit}' "$STOP")
[ -n "$loop" ] || { echo "FAIL: could not extract the changed loop from $STOP"; exit 1; }
echo "--- extracted loop (verbatim from $STOP) ---"; printf '%s\n' "$loop"; echo "-----------------------------------------"
( cd "$D" && eval "$loop" )
echo
echo "socket sweep line from the real file:"
awk '/^rm -f run\/prod-swift15/{f=1} f{print} f&&/sock$/{exit}' "$STOP"
wait "$FAKE" 2>/dev/null
echo "stand-in exited; nothing on this box was signalled."
