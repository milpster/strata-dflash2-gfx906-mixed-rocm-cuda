#!/usr/bin/env bash
# bench/gpu-fault-repro/trace_window.sh
#
# A bounded capture of the kernel's own view of one GPU-fault window.
#
# WHAT IT CAPTURES
#   The tracefs tracepoints in the amdgpu, drm and kfd groups (every event those
#   groups expose that the kernel lets us enable), into one trace buffer, for a
#   fixed number of seconds, then copied out to a directory the operator can
#   read with less afterwards:
#
#     bench/gpu-fault-repro/run/trace-<YYYYmmdd-HHMMSS>/
#       events.txt            which of the three groups exist here and which do
#                             not, with the reason. The amdgpu group may be
#                             absent or empty on this kernel; that is recorded,
#                             not assumed away.
#       notes.txt             start and end time, kernel release, cmdline, trace
#                             root used, duration, number of events enabled
#       enabled-events.txt    the exact enable files this run set to 1, so they
#                             can be set back to 0, and so the record says what
#                             was actually being watched
#       trace.txt             the trace buffer, copied at the end
#
# WHY ROOT
#   Tracefs is root-owned, and writing events/<group>/<event>/enable and
#   tracing_on needs root (or CAP_SYS_ADMIN). Without root nothing can be
#   enabled and the buffer cannot even be cleared, so the script refuses rather
#   than run and capture nothing.
#
# THE THREE-COMMAND WORKFLOW
#   Terminal 1, opened before the engine starts:
#     sudo bench/gpu-fault-repro/trace_window.sh start 900
#   Terminal 2, boot the engine as usual while terminal 1 is capturing:
#     ./launch-prod-...                       # whatever the operator's launcher is
#   Afterwards, from either terminal:
#     sudo bench/gpu-fault-repro/trace_window.sh status
#     sudo bench/gpu-fault-repro/trace_window.sh stop
#     less <the trace-<stamp>/trace.txt it prints>
#
#   start returns by itself after SECONDS (default 900) through the same exit
#   path as stop, so tracing is never left on. status and stop are safe at any
#   time: status only reads, and stop with nothing running just makes sure
#   tracing_on is 0.
#
# THE LIMIT
#   This records tracepoints, not page tables. It can show which jobs, fences,
#   VMIDs and queues the driver and the KFD were running when the kernel
#   refused a mapping, but it cannot dump the GPU's page tables or the content
#   of a command stream, and it cannot say why a page went missing. It is the
#   kernel-side timeline that a post-mortem dmesg alone cannot give.
#
#   If this kernel exposes no amdgpu tracepoints, the script says so in
#   events.txt and in the terminal and stops, instead of starting a capture
#   that would look like it had covered amdgpu when it had not.
#   TRACE_ALLOW_NO_AMDGPU=1 overrides that, for a kernel where only drm/kfd
#   events are wanted; the capture still records the absence.
#
#   The three groups' events are turned off again at the end, whatever their
#   state was before the run. This script owns those enable files while it
#   runs, and start refuses if a capture from this script is already alive.
#
# Nothing here starts, stops or probes the engine, touches :8009 or any GPU
# device, and nothing reads or writes bench/ab-v0210/rpcprof/out/.

set -euo pipefail
umask 022

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
RUN_ROOT=$SCRIPT_DIR/run
# run/ is gitignored (bench/gpu-fault-repro/.gitignore), so captures and this
# pidfile never reach a commit.
STATE_FILE=$RUN_ROOT/trace-capture.pid
# Written at start and kept after the run, so status can still point at the last
# capture once the pidfile is gone.
LAST_FILE=$RUN_ROOT/trace-last.txt

TMP_TRACE=/sys/kernel/tracing
DBG_TRACE=/sys/kernel/debug/tracing
GROUPS=(amdgpu drm kfd)
DEFAULT_DURATION_SEC=900
MAX_DURATION_SEC=86400
BUFFER_KB=65536
# stop waits this long for the capturing shell to finish its own cleanup (the
# copy of a large buffer takes a while) before it says so and takes over.
STOP_WAIT_SEC=60
# The three kernel lines this window exists to explain, counted only for the
# heartbeat. Read-only; nothing acts on them.
FAULT_PAT='no-retry page fault|Faulty UTCL2 client ID|PERMISSION_FAULTS'

TRACE_ROOT=
TRACE_TRIED=()
STAMP=
OUT_DIR=
AMGPU_EVENTS=0
AVAILABLE_EVENTS=0
CAP_PID=
CAP_OUT=
CAP_ROOT=
CAP_STARTED=
CAP_DURATION=
capture_finished=0

usage() {
  cat <<'EOF'
usage: sudo trace_window.sh <command>

  start [SECONDS]   clear the trace buffer, enable the amdgpu/drm/kfd events and
                    capture for SECONDS (default 900, max 86400), then stop,
                    copy the trace out and print where it is. Returns by itself;
                    Ctrl-C ends the window early through the same exit path.
  stop              end a running capture started by this script, or just make
                    sure tracing is off when none is running.
  status            report the trace root, tracing state, enabled event count
                    and whether a capture is running. Changes nothing.
  -h, --help        this text.

Root is required for every command. Output goes to
bench/gpu-fault-repro/run/trace-<YYYYmmdd-HHMMSS>/, readable by any user once
the capture ends. events.txt there records which of the amdgpu, drm and kfd
groups this kernel has; if amdgpu is missing the script stops rather than
capture nothing (TRACE_ALLOW_NO_AMDGPU=1 overrides that).
EOF
}

say() { printf '%s\n' "$*"; }
die() { printf 'trace_window.sh: %s\n' "$*" >&2; exit 1; }

require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    cat >&2 <<'EOF'
trace_window.sh: must run as root (uid 0).

Tracefs is root-owned, and enabling events or clearing the trace buffer needs
root (or CAP_SYS_ADMIN). Started without it, the capture would silently watch
nothing while looking like it worked.

Re-run it as:  sudo bench/gpu-fault-repro/trace_window.sh <start|stop|status>
EOF
    exit 1
  fi
}

# Both tracefs mount points are tried in order; the first one that is a real,
# mounted tracefs wins. An empty events/ listing means tracefs is not mounted
# there (a bare directory of that name exists on many kernels), so that path is
# not used.
detect_trace_root() {
  local c listed
  TRACE_TRIED=()
  for c in "$TMP_TRACE" "$DBG_TRACE"; do
    if [ ! -d "$c" ]; then
      TRACE_TRIED+=("$c does not exist")
      continue
    fi
    if [ ! -w "$c/tracing_on" ]; then
      TRACE_TRIED+=("$c exists but tracing_on is not writable")
      continue
    fi
    listed=$(find "$c/events" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null || true)
    if [ -z "$listed" ]; then
      TRACE_TRIED+=("$c exists but its events/ listing is empty (tracefs not mounted there)")
      continue
    fi
    TRACE_ROOT=$c
    return 0
  done
  return 1
}

no_trace_root_message() {
  local t
  printf 'trace_window.sh: no writable tracefs found; refusing. Paths tried:\n' >&2
  for t in "${TRACE_TRIED[@]}"; do
    printf '  %s\n' "$t" >&2
  done
  cat >&2 <<'EOF'

Mount one of them and retry:
  mount -t tracefs nodev /sys/kernel/tracing
  mount -t debugfs none /sys/kernel/debug
Then check:  ls /sys/kernel/tracing/events | head
EOF
}

# Names of the event directories in one group. The group directory also holds
# an "enable" file, which is not an event.
group_events() {
  local dir=$1 e
  for e in "$dir"/*/; do
    [ -d "$e" ] || continue
    basename -- "$e"
  done
}

count_group_events() {
  group_events "$1" | wc -l | tr -d ' '
}

# Writes the honest inventory of the three groups to $1/events.txt and sets
# AMGPU_EVENTS and AVAILABLE_EVENTS. No stdout: the caller prints the summary,
# and everything worth keeping goes into the file.
write_events_inventory() {
  local out=$1 g dir n total=0
  AMGPU_EVENTS=0
  AVAILABLE_EVENTS=0

  {
    printf 'Tracepoints available on this kernel, read at start.\n'
    printf 'Trace root: %s\n' "$TRACE_ROOT"
    printf 'Kernel:     %s\n' "$(uname -r)"
    printf 'Read at:    %s\n\n' "$STAMP"
  } >"$out/events.txt"

  for g in "${GROUPS[@]}"; do
    dir=$TRACE_ROOT/events/$g
    n=0
    if [ ! -d "$dir" ]; then
      printf '%s: NOT PRESENT on this kernel - no such group (%s does not exist)\n\n' \
        "$g" "$dir" >>"$out/events.txt"
    elif ! ls -1 -- "$dir" >/dev/null 2>&1; then
      printf '%s: NOT READABLE - %s exists but cannot be listed (permission, or a kernel that hides the group); nothing under it can be enabled\n\n' \
        "$g" "$dir" >>"$out/events.txt"
    else
      n=$(count_group_events "$dir")
      if [ "$n" -eq 0 ]; then
        printf '%s: PRESENT BUT EMPTY - %s holds no events\n\n' "$g" "$dir" \
          >>"$out/events.txt"
      else
        {
          printf '%s: PRESENT - %s events\n' "$g" "$n"
          group_events "$dir" | sed 's/^/  /'
          printf '\n'
        } >>"$out/events.txt"
      fi
    fi
    if [ "$g" = amdgpu ]; then
      AMGPU_EVENTS=$n
    fi
    total=$((total + n))
  done

  printf 'Total events available across the three groups: %s\n' "$total" \
    >>"$out/events.txt"
  AVAILABLE_EVENTS=$total
}

# Enables every group event whose enable file is writable. Writes the ones it
# set into $1/enabled-events.txt, one path per line, and prints the count of
# events enabled. Each refusal is reported: a half-enabled group is visible
# rather than guessed at.
enable_group_events() {
  local out=$1 g e n=0
  : >"$out/enabled-events.txt"
  for g in "${GROUPS[@]}"; do
    for e in "$TRACE_ROOT/events/$g"/*/enable; do
      [ -e "$e" ] || continue
      if [ ! -w "$e" ]; then
        printf 'trace_window.sh: could not enable %s (not writable)\n' "$e" >&2
        continue
      fi
      if printf '1' >"$e" 2>/dev/null; then
        printf '%s\n' "$e" >>"$out/enabled-events.txt"
        n=$((n + 1))
      else
        printf 'trace_window.sh: could not enable %s (write failed)\n' "$e" >&2
      fi
    done
  done
  printf '%s' "$n"
}

disable_enabled_events() {
  local out=$1 e
  [ -f "$out/enabled-events.txt" ] || return 0
  while IFS= read -r e; do
    [ -n "$e" ] || continue
    printf '0' >"$e" 2>/dev/null || true
  done <"$out/enabled-events.txt"
}

# The current tracing_on value, or "?" when it cannot be read.
tracing_state() {
  local v
  v=$(cat "$1/tracing_on" 2>/dev/null || true)
  case "$v" in
    ''|*[!0-9]*) printf '?' ;;
    *) printf '%s' "$v" ;;
  esac
}

# How many amdgpu/drm/kfd events are on right now. Reads each enable file.
count_enabled_in_groups() {
  local g e v n=0
  for g in "${GROUPS[@]}"; do
    for e in "$TRACE_ROOT/events/$g"/*/enable; do
      [ -e "$e" ] || continue
      v=$(cat "$e" 2>/dev/null || true)
      if [ "$v" = 1 ]; then
        n=$((n + 1))
      fi
    done
  done
  printf '%s' "$n"
}

# Kernel fault lines seen so far, for the heartbeat only.
fault_lines() {
  local n
  n=$(dmesg 2>/dev/null | grep -cE "$FAULT_PAT" || true)
  case "$n" in
    ''|*[!0-9]*) n=0 ;;
  esac
  printf '%s' "$n"
}

# The one end path for start, stop and a signal: tracing off, buffer copied
# out, events off, output readable by the operator. Safe to call twice, and
# safe to call when nothing was ever configured.
finish_capture() {
  local root=$1 out=$2 lines
  set +e
  trap - INT TERM HUP EXIT
  if [ "$capture_finished" -eq 1 ]; then
    return 0
  fi
  capture_finished=1

  if [ -z "$root" ] || [ -z "$out" ] || [ ! -d "$out" ]; then
    return 0
  fi

  printf '0' >"$root/tracing_on" 2>/dev/null || true

  if [ -f "$root/trace" ]; then
    if ! cp -- "$root/trace" "$out/trace.txt" 2>/dev/null; then
      printf 'trace_window.sh: copying %s/trace failed\n' "$root" >&2
    fi
  fi

  disable_enabled_events "$out"

  {
    printf 'end=%s\n' "$(date -Is)"
    printf 'tracing_on_at_end=%s\n' "$(tracing_state "$root")"
    printf 'events_enabled_at_end=%s\n' "$(count_enabled_in_groups)"
  } >>"$out/notes.txt" 2>/dev/null

  rm -f "$STATE_FILE" 2>/dev/null

  chmod -R a+rX "$out" 2>/dev/null
  if [ -f "$out/trace.txt" ]; then
    lines=$(wc -l <"$out/trace.txt" 2>/dev/null || true)
    printf 'trace_window.sh: capture finished\n'
    printf '  trace:  %s/trace.txt (%s lines)\n' "$out" "$lines"
  else
    printf 'trace_window.sh: capture finished, but no trace.txt was written under %s\n' \
      "$out" >&2
  fi
  printf '  record: %s/events.txt and %s/notes.txt\n' "$out" "$out"
  printf '  read:   less %s/trace.txt\n' "$out"
}

on_exit() {
  finish_capture "$TRACE_ROOT" "$OUT_DIR"
}

on_stop_signal() {
  printf '\ntrace_window.sh: %s received; ending the capture early\n' "$1" >&2
  exit 130
}

# Reads the pidfile into CAP_PID/CAP_OUT/CAP_ROOT/CAP_STARTED/CAP_DURATION; all
# empty when there is no pidfile.
load_state() {
  CAP_PID=
  CAP_OUT=
  CAP_ROOT=
  CAP_STARTED=
  CAP_DURATION=
  [ -f "$STATE_FILE" ] || return 0
  CAP_PID=$(sed -n '1p' "$STATE_FILE" 2>/dev/null | tr -dc '0-9')
  CAP_OUT=$(sed -n 's/^out=//p' "$STATE_FILE" 2>/dev/null | head -n 1)
  CAP_ROOT=$(sed -n 's/^trace_root=//p' "$STATE_FILE" 2>/dev/null | head -n 1)
  CAP_STARTED=$(sed -n 's/^started=//p' "$STATE_FILE" 2>/dev/null | head -n 1)
  CAP_DURATION=$(sed -n 's/^duration=//p' "$STATE_FILE" 2>/dev/null | head -n 1)
}

# True only if the pid is alive AND its command line is this script, so a
# recycled pid is never signalled.
is_our_capture() {
  local pid=$1
  [ -n "$pid" ] || return 1
  [ -r "/proc/$pid/cmdline" ] || return 1
  tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null | grep -q 'trace_window\.sh' || return 1
  return 0
}

validate_duration() {
  local s=$1
  case "$s" in
    ''|*[!0-9]*) die "SECONDS must be a whole number of seconds, got '$s'" ;;
  esac
  s=$((10#$s))
  if [ "$s" -lt 1 ] || [ "$s" -gt "$MAX_DURATION_SEC" ]; then
    die "SECONDS must be between 1 and $MAX_DURATION_SEC, got $s"
  fi
  printf '%s' "$s"
}

cmd_start() {
  local seconds=$1 dur enabled prior_on buffer_note elapsed step base now delta

  dur=$(validate_duration "$seconds")

  load_state
  if is_our_capture "$CAP_PID"; then
    die "a capture from this script is already running (pid $CAP_PID, out $CAP_OUT); run 'stop' first"
  fi
  if [ -f "$STATE_FILE" ]; then
    say "trace_window.sh: removing stale pidfile $STATE_FILE (no such capture is alive)"
    rm -f "$STATE_FILE"
  fi

  if ! detect_trace_root; then
    no_trace_root_message
    exit 1
  fi
  say "trace_window.sh: trace root $TRACE_ROOT"

  STAMP=$(date +%Y%m%d-%H%M%S)
  OUT_DIR=$RUN_ROOT/trace-$STAMP
  mkdir -p "$RUN_ROOT" "$OUT_DIR"
  printf '%s' "$OUT_DIR" >"$LAST_FILE"

  # Armed before anything below can fail, so the exit path always knows which
  # tracefs to switch off and where to put the trace.
  trap on_exit EXIT
  trap 'on_stop_signal INT' INT
  trap 'on_stop_signal TERM' TERM
  trap 'on_stop_signal HUP' HUP

  # Inventory first: it decides whether there is anything worth capturing.
  write_events_inventory "$OUT_DIR"
  say "trace_window.sh: events available - amdgpu ${AMGPU_EVENTS}, three groups ${AVAILABLE_EVENTS} (see $OUT_DIR/events.txt)"

  if [ "$AMGPU_EVENTS" -eq 0 ] && [ "${TRACE_ALLOW_NO_AMDGPU:-0}" != 1 ]; then
    printf '\nNOT STARTED: this kernel exposes no amdgpu tracepoints, and amdgpu job/VM activity is the point of the window. Nothing was enabled and tracing was not turned on.\n' \
      >>"$OUT_DIR/events.txt"
    chmod -R a+rX "$OUT_DIR" 2>/dev/null
    trap - EXIT INT TERM HUP
    die "no amdgpu tracepoints on this kernel ($(uname -r)); not starting. Record: $OUT_DIR/events.txt. TRACE_ALLOW_NO_AMDGPU=1 captures drm/kfd anyway"
  fi
  if [ "$AMGPU_EVENTS" -eq 0 ]; then
    say "trace_window.sh: WARNING no amdgpu tracepoints here; TRACE_ALLOW_NO_AMDGPU=1 is set, capturing the other groups"
  fi

  if [ "$AVAILABLE_EVENTS" -eq 0 ]; then
    printf '\nNOT STARTED: not one event in the amdgpu, drm or kfd groups could be listed, so a capture would record nothing.\n' \
      >>"$OUT_DIR/events.txt"
    chmod -R a+rX "$OUT_DIR" 2>/dev/null
    trap - EXIT INT TERM HUP
    die "no amdgpu/drm/kfd events to enable on this kernel; not starting. Record: $OUT_DIR/events.txt"
  fi

  # Buffer first (it cannot be resized while tracing is on), then clear, then
  # enable, then turn tracing on.
  prior_on=$(tracing_state "$TRACE_ROOT")
  if printf '%s' "$BUFFER_KB" >"$TRACE_ROOT/buffer_size_kb" 2>/dev/null; then
    buffer_note="$BUFFER_KB KiB per CPU"
  else
    buffer_note="could not set $BUFFER_KB KiB; kernel keeps $(cat "$TRACE_ROOT/buffer_size_kb" 2>/dev/null || printf 'unknown') KiB per CPU"
  fi
  say "trace_window.sh: buffer $buffer_note"

  printf '0' >"$TRACE_ROOT/tracing_on"
  : >"$TRACE_ROOT/trace"

  enabled=$(enable_group_events "$OUT_DIR")
  if [ "$enabled" -eq 0 ]; then
    chmod -R a+rX "$OUT_DIR" 2>/dev/null
    trap - EXIT INT TERM HUP
    die "no event could be enabled (every enable file was unwritable); tracing was not turned on. Record: $OUT_DIR/events.txt"
  fi

  {
    printf 'start=%s\n' "$(date -Is)"
    printf 'host=%s\n' "$(hostname 2>/dev/null || printf '?')"
    printf 'kernel_release=%s\n' "$(uname -r)"
    printf 'cmdline=%s\n' "$(cat /proc/cmdline 2>/dev/null || printf '?')"
    printf 'trace_root=%s\n' "$TRACE_ROOT"
    printf 'current_tracer=%s\n' "$(cat "$TRACE_ROOT/current_tracer" 2>/dev/null || printf '?')"
    printf 'duration_sec=%s\n' "$dur"
    printf 'events_enabled=%s\n' "$enabled"
    printf 'events_available_total=%s\n' "$AVAILABLE_EVENTS"
    printf 'amdgpu_events_available=%s\n' "$AMGPU_EVENTS"
    printf 'buffer=%s\n' "$buffer_note"
    printf 'tracing_on_before_start=%s\n' "$prior_on"
    printf 'fault_lines_at_start=%s\n' "$(fault_lines)"
    printf 'purpose=record the kernel tracepoint activity of one engine window, so a\n'
    printf '  later page fault (no-retry / UTCL2 XDMA / PERMISSION_FAULTS) can be put\n'
    printf '  on a timeline. Tracepoints only: no page tables, no command streams.\n'
  } >"$OUT_DIR/notes.txt"

  {
    printf '%s\n' "$$"
    printf 'started=%s\n' "$(date -Is)"
    printf 'trace_root=%s\n' "$TRACE_ROOT"
    printf 'out=%s\n' "$OUT_DIR"
    printf 'duration=%s\n' "$dur"
  } >"$STATE_FILE"

  if [ "$prior_on" = 1 ]; then
    say "trace_window.sh: note - tracing was already ON before start; the buffer was cleared for this window"
  fi
  printf '1' >"$TRACE_ROOT/tracing_on"
  if [ "$(tracing_state "$TRACE_ROOT")" != 1 ]; then
    die "tracing_on did not read back as 1 after being set; capture not running (trace root $TRACE_ROOT)"
  fi

  say "trace_window.sh: capturing $enabled events for ${dur}s, pid $$"
  say "  output:     $OUT_DIR"
  say "  end early:  sudo $0 stop    (or Ctrl-C here)"

  # The bounded window, with a heartbeat so the operator can see it is alive and
  # whether the kernel has logged a fault line yet.
  elapsed=0
  base=$(fault_lines)
  while [ "$elapsed" -lt "$dur" ]; do
    step=30
    if [ "$((dur - elapsed))" -lt "$step" ]; then
      step=$((dur - elapsed))
    fi
    sleep "$step"
    elapsed=$((elapsed + step))
    now=$(fault_lines)
    delta=$((now - base))
    printf 'trace_window.sh: %ss/%ss captured, tracing_on=%s, new kernel fault lines=%s\n' \
      "$elapsed" "$dur" "$(tracing_state "$TRACE_ROOT")" "$delta"
  done

  say "trace_window.sh: window elapsed; stopping through the normal exit path"
  exit 0
}

cmd_stop() {
  local i st out root lines

  load_state

  if is_our_capture "$CAP_PID"; then
    say "trace_window.sh: asking capture pid $CAP_PID to stop (TERM; it copies the trace itself)"
    kill -TERM "$CAP_PID" 2>/dev/null || true
    i=0
    while [ "$i" -lt "$STOP_WAIT_SEC" ]; do
      sleep 1
      i=$((i + 1))
      if ! is_our_capture "$CAP_PID"; then
        break
      fi
    done
    if is_our_capture "$CAP_PID"; then
      say "trace_window.sh: pid $CAP_PID still alive after ${STOP_WAIT_SEC}s; killing it and copying the trace here"
      kill -KILL "$CAP_PID" 2>/dev/null || true
      sleep 2
      finish_capture "$CAP_ROOT" "$CAP_OUT"
    fi
  elif [ -n "$CAP_PID" ] && [ -r "/proc/$CAP_PID/cmdline" ]; then
    say "trace_window.sh: pid $CAP_PID is alive but is not this script; leaving it alone"
  elif [ -f "$STATE_FILE" ]; then
    say "trace_window.sh: pidfile $STATE_FILE is stale (capture not running); removing it"
    rm -f "$STATE_FILE"
  else
    say "trace_window.sh: no capture from this script is running"
  fi

  # Whatever happened above, this command ends with tracing off, so an orphaned
  # buffer cannot keep filling.
  if ! detect_trace_root; then
    no_trace_root_message
    exit 1
  fi
  st=$(tracing_state "$TRACE_ROOT")
  if [ "$st" != 0 ]; then
    printf '0' >"$TRACE_ROOT/tracing_on" 2>/dev/null \
      || say "trace_window.sh: WARNING could not write tracing_on=0"
  fi

  root=$CAP_ROOT
  out=$CAP_OUT
  if [ -z "$root" ]; then
    root=$TRACE_ROOT
  fi
  if [ -n "$out" ] && [ -d "$out" ]; then
    if [ ! -f "$out/trace.txt" ]; then
      # The capturing shell could not do this itself (it was killed), so the
      # copy, the event shutdown and the chmod happen here.
      finish_capture "$root" "$out"
    else
      disable_enabled_events "$out"
      chmod -R a+rX "$out" 2>/dev/null
      lines=$(wc -l <"$out/trace.txt" 2>/dev/null || true)
      say "trace_window.sh: trace already written: $out/trace.txt ($lines lines)"
    fi
  fi

  rm -f "$STATE_FILE" 2>/dev/null
  say "trace_window.sh: tracing_on=$(tracing_state "$TRACE_ROOT"), events enabled in amdgpu/drm/kfd=$(count_enabled_in_groups)"
}

cmd_status() {
  local st n t last

  load_state

  if detect_trace_root; then
    say "trace root:     $TRACE_ROOT"
    st=$(tracing_state "$TRACE_ROOT")
    say "tracing_on:     $st"
    n=$(count_enabled_in_groups)
    say "events enabled: $n (amdgpu/drm/kfd)"
    say "buffer:         $(cat "$TRACE_ROOT/buffer_size_kb" 2>/dev/null || printf '?') KiB per CPU"
    if [ "$st" = 1 ] && ! is_our_capture "$CAP_PID"; then
      say "warning:        tracing is ON but no capture from this script is running; 'stop' clears it"
    fi
  else
    say "trace root:     NOT FOUND"
    for t in "${TRACE_TRIED[@]}"; do
      say "                tried $t"
    done
  fi

  if is_our_capture "$CAP_PID"; then
    say "capture:        RUNNING, pid $CAP_PID"
    say "  started:      $CAP_STARTED"
    say "  duration:     ${CAP_DURATION}s"
    say "  output:       $CAP_OUT"
  elif [ -n "$CAP_PID" ] && [ -r "/proc/$CAP_PID/cmdline" ]; then
    say "capture:        pid $CAP_PID is alive but is not this script; stale pidfile $STATE_FILE"
  elif [ -f "$STATE_FILE" ]; then
    say "capture:        NOT RUNNING (stale pidfile $STATE_FILE from $CAP_STARTED)"
  else
    say "capture:        NOT RUNNING"
  fi

  last=$CAP_OUT
  if [ -z "$last" ]; then
    last=$(cat "$LAST_FILE" 2>/dev/null || true)
  fi
  if [ -n "$last" ] && [ -d "$last" ]; then
    if [ -f "$last/trace.txt" ]; then
      say "last trace:     $last/trace.txt ($(wc -l <"$last/trace.txt" 2>/dev/null || true) lines)"
    else
      say "last output:    $last (no trace.txt yet)"
    fi
  fi
}

main() {
  local cmd="${1:-}"

  case "$cmd" in
    -h|--help|help)
      usage
      exit 0
      ;;
    start|stop|status) ;;
    '')
      usage >&2
      exit 2
      ;;
    *)
      printf 'trace_window.sh: unknown command %s\n\n' "$cmd" >&2
      usage >&2
      exit 2
      ;;
  esac

  require_root
  shift
  case "$cmd" in
    start)
      if [ "$#" -gt 0 ]; then
        cmd_start "$1"
      else
        cmd_start "$DEFAULT_DURATION_SEC"
      fi
      ;;
    stop) cmd_stop ;;
    status) cmd_status ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
