#!/usr/bin/env bash
# bench/gpu-fault-repro/run_window.sh
#
# One fault-repro window, with the record the README's "Run" and "What to
# capture" sections ask for:
#
#   pre-run gates -> positive control -> long --mode cycle leg -> kernel slice
#   -> summary.txt
#
# Code and docs only. The only processes this script starts are the short device
# probe gate and the repro itself; it never starts, stops or probes the engine,
# and it never takes :8009.
#
# Environment trap: this session's shell exports CUDA_VISIBLE_DEVICES= (set but
# empty, which rocclr reads as "no devices") and HSA_OVERRIDE_GFX_VERSION=9.0.0,
# which makes every agent report gfx900 against a gfx906-only fatbin, so the
# repro dies with "hipGetDeviceCount: no ROCm-capable device is detected
# code=100". Every repro invocation therefore builds its own environment with
# env -u CUDA_VISIBLE_DEVICES -u GPU_DEVICE_ORDINAL HSA_OVERRIDE_GFX_VERSION=9.0.6
# and the ROCm 6.1.0 LD_LIBRARY_PATH, and never inherits the caller's ROCm vars.
#
# Usage, defaults and exit codes: --help; the dry listing: --plan.

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPRO_BIN=$SCRIPT_DIR/build/hip-fault-repro
RUN_ROOT=$SCRIPT_DIR/run

# Options (README defaults).
DURATION_SEC=1800
CYCLE_SEC=30
HOST_WRITE_SEC=30
DEV=1
PCI=0000:09:00.0
SKIP_CONTROL=0
PLAN=0

# Window constants.
CONTROL_DURATION_SEC=120  # README: the control is expected to fault immediately
PROBE_DURATION_SEC=1      # the gate's probe: start line + arch, then a clean DONE
DONE_WAIT_SEC=60          # bounded wait for the DONE line after a forwarded stop
MIN_MEM_AVAIL_KIB=$((15 * 1024 * 1024))
KFD_DEV=/dev/kfd
# The kernel's own list of processes holding a KFD context (needs CONFIG_KFD_SYSFS).
# Overridable for a kernel that mounts it elsewhere, or to test this script.
KFD_PROC=${KFD_PROC:-/sys/class/kfd/kfd/proc}
PORT=8009
STRATA_PAT='rpcw-hip/strata --serve'
KERNEL_PAT='XDMA|permission fault|amdgpu|KFD|svm'

# The child environment, built explicitly (never inherited). ROCm 6.1.0 userspace
# plus this box's gfx906 xnack build; never /opt/rocm 7.x, which has no gfx906
# kernels and the wrong sonames.
ROCM_DIR=${ROCM_DIR:-/opt/rocm-6.1.0}
XNACK_LIB=${XNACK_LIB:-/home/srcds/rocm-gfx906-xnack/lib}
REPRO_LD_LIBRARY_PATH=$XNACK_LIB:$ROCM_DIR/lib
CHILD_HSA_OVERRIDE=9.0.6
CHILD_ENV=(env -u CUDA_VISIBLE_DEVICES -u GPU_DEVICE_ORDINAL
  HSA_OVERRIDE_GFX_VERSION="$CHILD_HSA_OVERRIDE"
  LD_LIBRARY_PATH="$REPRO_LD_LIBRARY_PATH")
CHILD_ENV_TEXT="env -u CUDA_VISIBLE_DEVICES -u GPU_DEVICE_ORDINAL HSA_OVERRIDE_GFX_VERSION=$CHILD_HSA_OVERRIDE LD_LIBRARY_PATH=$REPRO_LD_LIBRARY_PATH"

# Gate commands. The --plan listing prints these very strings and the gates
# execute these very strings, so the dry listing cannot drift from the run.
CMD_MEMINFO="grep '^MemAvailable:' /proc/meminfo"
CMD_FUSER="fuser $KFD_DEV"
CMD_SMI_PIDS="rocm-smi --showpids"
CMD_PORT="ss -H -ltnp 'sport = :$PORT'"
CMD_STRATA_SERVE="pgrep -f -- '$STRATA_PAT'"
CMD_LOADAVG="cut -d' ' -f1 /proc/loadavg"
CMD_CORES="lscpu -p=Core,Socket"

# Window state, filled in as the run advances; the summary reads only these.
RUN_DIR=
GATES=
RUN_START_EPOCH=$(date +%s)
RUN_START_ISO=$(date -Is)
RUN_END_EPOCH=
GATE_FAILED=0
GATE_COUNT=0
GATE_PASS_COUNT=0
GATE_FAIL_LIST=
PROBE_RUN=0
PROBE_LOG=
PROBE_CMD=
PROBE_RC=
PROBE_LINE=
CONTROL_RUN=0
CONTROL_LOG=
CONTROL_CMD=
CONTROL_RC=
CONTROL_LINE=
CONTROL_VERDICT=
CONTROL_PASS=0
CONTROL_REFUSE_CODE=3
LONG_RUN=0
LONG_LOG=
LONG_CMD=
LONG_RC=
STOP_SIG=
STOP_AT=
HARD_KILLED=0
ACTIVE_PID=
STREAM_PID=
FINAL_EXIT=-1
SUMMARY_WRITTEN=0

usage() {
  cat <<'EOF'
usage: run_window.sh [options]

Runs one gpu fault-repro window: pre-run gates (including a 1 s device probe),
the positive control, then the long --mode cycle leg in the foreground (its log
streamed live), and writes an auditable record to run/<YYYYmmdd-HHMMSS>/
(gates.txt, probe.log, control.log, repro.log, kernel-before.txt,
kernel-after.txt, summary.txt).

  --duration-sec N     long leg wall time in seconds                 (default 1800)
  --cycle-sec N        cycle period in seconds, 0 = cycle every scan  (default 30)
  --host-write-sec N   host coherence probe period, 0 = off           (default 30)
  --dev N              device that reads the buffer                   (default 1)
  --pci DDDD:BB:DD.F   required PCI address (card 1 on this box)      (default 0000:09:00.0)
  --skip-control       skip the positive control. A clean long leg is then
                       uninterpretable: nothing proves the fault detector fires.
  --plan               print the gates and commands and exit; nothing is run
  --help               this text

The repro never inherits this shell's ROCm environment: every invocation is
wrapped as
  env -u CUDA_VISIBLE_DEVICES -u GPU_DEVICE_ORDINAL HSA_OVERRIDE_GFX_VERSION=9.0.6 LD_LIBRARY_PATH=...

exit codes
  0    long leg clean, or stopped and clean (summary.txt says stopped)
  2    usage error (unknown argument, bad value), missing/unusable binary,
       or a failed pre-run gate (including the device probe); the run is refused
  3    refused: the positive control did not certify the detector.
       Also used when the long leg itself exits 3 (HIP fault).
  4    long leg: checksum fault
  5    long leg: churn defect
  6    positive control failed: no error at all; detector path unproven
  7    positive control inconclusive: rejected without a memory fault
  128+n  long leg ended by signal n, or hard-killed after DONE_WAIT_SEC
EOF
}

die_usage() {
  printf 'run_window.sh: %s\n' "$1" >&2
  printf 'run_window.sh: see --help\n' >&2
  exit 2
}

require_int() { # require_int FLAG VALUE
  [[ ${2:-} =~ ^[0-9]+$ ]] || die_usage "$1 takes a non-negative integer (got '${2:-}')"
}

parse_args() {
  while (( $# > 0 )); do
    case $1 in
      --duration-sec)
        (( $# >= 2 )) || die_usage "--duration-sec needs a value"
        DURATION_SEC=$2
        shift 2
        ;;
      --cycle-sec)
        (( $# >= 2 )) || die_usage "--cycle-sec needs a value"
        CYCLE_SEC=$2
        shift 2
        ;;
      --host-write-sec)
        (( $# >= 2 )) || die_usage "--host-write-sec needs a value"
        HOST_WRITE_SEC=$2
        shift 2
        ;;
      --dev)
        (( $# >= 2 )) || die_usage "--dev needs a value"
        DEV=$2
        shift 2
        ;;
      --pci)
        (( $# >= 2 )) || die_usage "--pci needs a value"
        PCI=$2
        shift 2
        ;;
      --skip-control)
        SKIP_CONTROL=1
        shift
        ;;
      --plan)
        PLAN=1
        shift
        ;;
      --help | -h)
        usage
        exit 0
        ;;
      *)
        die_usage "unknown argument '$1'"
        ;;
    esac
  done
  require_int --duration-sec "$DURATION_SEC"
  require_int --cycle-sec "$CYCLE_SEC"
  require_int --host-write-sec "$HOST_WRITE_SEC"
  require_int --dev "$DEV"
  if [[ -n $PCI ]] && [[ ! $PCI =~ ^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]$ ]]; then
    die_usage "--pci takes DDDD:BB:DD.F (got '$PCI')"
  fi
}

check_binary() {
  if [[ ! -f $REPRO_BIN ]]; then
    printf 'run_window.sh: missing %s\n' "$REPRO_BIN" >&2
    printf 'run_window.sh: build it first (README "Build"):\n' >&2
    printf '  cd %s\n' "$SCRIPT_DIR" >&2
    # shellcheck disable=SC2016  # the $PATH here is text for the reader, not an expansion
    printf '  PATH=%s/bin:$PATH cmake -S . -B build -G Ninja -DCMAKE_HIP_COMPILER=%s/llvm/bin/clang++ -DCMAKE_PREFIX_PATH=%s\n' \
      "$ROCM_DIR" "$ROCM_DIR" "$ROCM_DIR" >&2
    # shellcheck disable=SC2016
    printf '  PATH=%s/bin:$PATH cmake --build build -j\n' "$ROCM_DIR" >&2
    exit 2
  fi
  if [[ ! -x $REPRO_BIN ]]; then
    printf 'run_window.sh: %s is not executable\n' "$REPRO_BIN" >&2
    exit 2
  fi
}

caller_val() { # caller_val NAME -> value, or <unset> / <set but empty>
  local name=$1 v
  if ! v=$(printenv "$name"); then
    printf '<unset>'
    return 0
  fi
  if [[ -z $v ]]; then
    printf '<set but empty>'
  else
    printf '%s' "$v"
  fi
}

# ------------------------------------------------------------- run bookkeeping

make_run_dir() {
  mkdir -p -- "$RUN_ROOT"
  local base stamp n
  stamp=$(date +%Y%m%d-%H%M%S)
  RUN_DIR=$RUN_ROOT/$stamp
  n=1
  base=$RUN_DIR
  while [[ -e $RUN_DIR ]]; do
    RUN_DIR=$base-$n
    n=$((n + 1))
  done
  mkdir -- "$RUN_DIR"
  GATES=$RUN_DIR/gates.txt
  : >"$GATES"
  printf '[window] run dir: %s\n' "$RUN_DIR"
}

on_exit() {
  local rc=$?
  stream_stop
  if (( FINAL_EXIT < 0 )); then
    FINAL_EXIT=$rc
  fi
  if (( SUMMARY_WRITTEN == 0 )) && [[ -n $RUN_DIR ]] && [[ -d $RUN_DIR ]]; then
    RUN_END_EPOCH=$(date +%s)
    write_summary >/dev/null 2>&1 || true
    SUMMARY_WRITTEN=1
  fi
}

finish() { # finish EXITCODE
  FINAL_EXIT=$1
  RUN_END_EPOCH=$(date +%s)
  if (( SUMMARY_WRITTEN == 0 )) && [[ -n $RUN_DIR ]] && [[ -d $RUN_DIR ]]; then
    write_summary
    SUMMARY_WRITTEN=1
  fi
  printf '\n[window] record: %s\n' "$RUN_DIR"
  printf '[window] exit %s\n' "$FINAL_EXIT"
  exit "$FINAL_EXIT"
}

# Live progress: the repro writes only to its --log file, so stream that file to
# the caller's terminal while it runs (README: "tail -f ... live progress").
stream_start() { # stream_start LOG
  STREAM_PID=
  tail -n +1 -F -- "$1" 2>/dev/null &
  STREAM_PID=$!
}

stream_stop() {
  if [[ -n ${STREAM_PID:-} ]] && kill -0 "$STREAM_PID" 2>/dev/null; then
    kill "$STREAM_PID" 2>/dev/null || true
    wait "$STREAM_PID" 2>/dev/null || true
  fi
  STREAM_PID=
}

handle_stop() { # handle_stop SIGNAME
  if [[ -n $STOP_SIG ]]; then
    return 0
  fi
  STOP_SIG=$1
  STOP_AT=$(date -Is)
  printf '\n[window] %s at %s: forwarding to pid %s\n' "$1" "$STOP_AT" "${ACTIVE_PID:-none}"
  if [[ -n ${ACTIVE_PID:-} ]] && kill -0 "$ACTIVE_PID" 2>/dev/null; then
    kill -s "$1" "$ACTIVE_PID" 2>/dev/null || true
  fi
}

run_child() { # run_child cmd... -> the child's exit status
  local rc=0 deadline=0
  "$@" &
  ACTIVE_PID=$!
  while kill -0 "$ACTIVE_PID" 2>/dev/null; do
    if [[ -n $STOP_SIG ]] && (( deadline == 0 )); then
      deadline=$((SECONDS + DONE_WAIT_SEC))
      printf '[window] waiting up to %ss for the DONE line\n' "$DONE_WAIT_SEC"
    fi
    if (( deadline > 0 )) && (( SECONDS >= deadline )); then
      HARD_KILLED=1
      printf '[window] pid %s still alive %ss after %s: sending SIGKILL\n' \
        "$ACTIVE_PID" "$DONE_WAIT_SEC" "$STOP_SIG"
      kill -KILL "$ACTIVE_PID" 2>/dev/null || true
      deadline=0
    fi
    sleep 1 || true
  done
  set +e
  wait "$ACTIVE_PID"
  rc=$?
  set -e
  ACTIVE_PID=
  return "$rc"
}

build_cmd() { # build_cmd probe|control|long -> CMD_ARGV (uses $RUN_DIR; --plan passes a placeholder)
  CMD_ARGV=("${CHILD_ENV[@]}" "$REPRO_BIN" --dev "$DEV")
  if [[ -n $PCI ]]; then
    CMD_ARGV+=(--pci "$PCI")
  fi
  case $1 in
    probe)
      CMD_ARGV+=(--mode file --duration-sec "$PROBE_DURATION_SEC")
      CMD_ARGV+=(--dummy-dir "$RUN_DIR" --log "$RUN_DIR/probe.log")
      ;;
    control)
      CMD_ARGV+=(--mode cycle --positive-control --duration-sec "$CONTROL_DURATION_SEC")
      CMD_ARGV+=(--dummy-dir "$RUN_DIR" --log "$RUN_DIR/control.log")
      ;;
    long)
      CMD_ARGV+=(--mode cycle --duration-sec "$DURATION_SEC" --cycle-sec "$CYCLE_SEC")
      CMD_ARGV+=(--host-write-sec "$HOST_WRITE_SEC")
      CMD_ARGV+=(--dummy-dir "$RUN_DIR" --log "$RUN_DIR/repro.log")
      ;;
    *)
      die_usage "build_cmd: unknown kind '$1'"
      ;;
  esac
}

# ----------------------------------------------------------------- pre-run gates

report_gate() { # report_gate NAME OK DETAIL
  local name=$1 ok=$2 detail=$3
  GATE_COUNT=$((GATE_COUNT + 1))
  if [[ $ok == 1 ]]; then
    GATE_PASS_COUNT=$((GATE_PASS_COUNT + 1))
    printf '  [PASS] %-16s %s\n' "$name" "$detail"
    printf 'verdict: PASS - %s\n' "$detail" >>"$GATES"
  else
    GATE_FAILED=1
    GATE_FAIL_LIST="${GATE_FAIL_LIST:+$GATE_FAIL_LIST, }$name"
    printf '  [FAIL] %-16s %s\n' "$name" "$detail"
    printf 'verdict: FAIL - %s\n' "$detail" >>"$GATES"
  fi
}

gate_begin() { # gate_begin N NAME
  printf '\n=== gate %s: %s ===\n' "$1" "$2" >>"$GATES"
}

run_gate_cmd() { # run_gate_cmd "cmd" -> OUT, RC, and a block in gates.txt
  local rc=0
  OUT=$(bash -c "$1" 2>&1) || rc=$?
  RC=$rc
  {
    printf 'cmd: %s\n' "$1"
    printf 'rc: %s\n' "$rc"
    printf 'output:\n'
    if [[ -n $OUT ]]; then
      printf '%s\n' "$OUT" | sed 's/^/  /'
    else
      printf '  (empty)\n'
    fi
  } >>"$GATES"
}

record_caller_env() {
  {
    printf '\n=== caller environment (recorded, never inherited by the repro) ===\n'
    printf 'CUDA_VISIBLE_DEVICES=%s\n' "$(caller_val CUDA_VISIBLE_DEVICES)"
    printf 'GPU_DEVICE_ORDINAL=%s\n' "$(caller_val GPU_DEVICE_ORDINAL)"
    printf 'HSA_OVERRIDE_GFX_VERSION=%s\n' "$(caller_val HSA_OVERRIDE_GFX_VERSION)"
    printf 'LD_LIBRARY_PATH=%s\n' "$(caller_val LD_LIBRARY_PATH)"
    printf 'child env (every repro invocation): %s\n' "$CHILD_ENV_TEXT"
  } >>"$GATES"
  printf '  [note] caller CUDA_VISIBLE_DEVICES=%s HSA_OVERRIDE_GFX_VERSION=%s (recorded in gates.txt)\n' \
    "$(caller_val CUDA_VISIBLE_DEVICES)" "$(caller_val HSA_OVERRIDE_GFX_VERSION)"
}

gate_mem_available() {
  gate_begin 1 'MemAvailable >= 15 GiB (the pre-boot gate)'
  run_gate_cmd "$CMD_MEMINFO"
  local avail
  avail=$(printf '%s\n' "$OUT" | awk '{print $2}')
  if [[ ! $avail =~ ^[0-9]+$ ]]; then
    report_gate mem-available 0 "cannot read MemAvailable (output: ${OUT:-none})"
    return 0
  fi
  if (( avail >= MIN_MEM_AVAIL_KIB )); then
    report_gate mem-available 1 "MemAvailable=${avail} kB >= ${MIN_MEM_AVAIL_KIB} kB"
  else
    report_gate mem-available 0 "MemAvailable=${avail} kB < ${MIN_MEM_AVAIL_KIB} kB (needs 15 GiB)"
  fi
}

gate_kfd_exclusive() {
  gate_begin 2 "no other process on $KFD_DEV (fuser, rocm-smi --showpids)"
  run_gate_cmd "$CMD_FUSER"
  local fuser_out=$OUT fuser_rc=$RC
  run_gate_cmd "$CMD_SMI_PIDS"
  local smi_out=$OUT smi_rc=$RC
  # fuser prints "/dev/kfd: 1234m 5678" - each PID carries an access-mode suffix
  # (m mapped, c/f/e/r), so strip everything but the digits of each PID.
  local pids bad syspids d
  pids=$(printf '%s\n' "$fuser_out" | sed 's/^[^:]*://' | grep -oE '[0-9]+' | sort -u | tr '\n' ' ' || true)
  bad=$(printf '%s\n' "$smi_out" | grep -E 'GPU\[[0-9]+\][[:space:]]*:[[:space:]]*[0-9]+' || true)
  # the kernel's own list of processes holding a KFD context (authoritative)
  syspids=
  if [[ -d $KFD_PROC ]]; then
    for d in "$KFD_PROC"/*; do
      [[ -d $d ]] || continue
      syspids="$syspids${d##*/} "
    done
  fi
  printf 'fuser pids: %s\nkernel KFD proc pids (%s): %s\n' \
    "${pids:-none}" "$KFD_PROC" "${syspids:-none}" >>"$GATES"
  if [[ -n $pids || -n $bad || -n $syspids ]]; then
    report_gate kfd-exclusive 0 "KFD holders present: fuser='${pids:-none}' kernel='${syspids:-none}' rocm-smi='${bad:-none}'"
  elif ! command -v rocm-smi >/dev/null 2>&1; then
    report_gate kfd-exclusive 0 'rocm-smi not installed: exclusivity cannot be proven'
  elif (( smi_rc != 0 )) && ! printf '%s' "$smi_out" | grep -q 'No KFD PIDs'; then
    report_gate kfd-exclusive 0 "rocm-smi --showpids failed (rc=$smi_rc, fuser rc=$fuser_rc): ${smi_out:-no output}"
  else
    report_gate kfd-exclusive 1 "fuser empty (rc=$fuser_rc); rocm-smi reports no KFD PIDs (rc=$smi_rc)"
  fi
}

gate_port_free() {
  gate_begin 3 "no listener on :$PORT"
  run_gate_cmd "$CMD_PORT"
  if [[ -z $OUT ]]; then
    report_gate "port-$PORT" 1 "no listener on :$PORT"
  else
    report_gate "port-$PORT" 0 "listener present: $(printf '%s\n' "$OUT" | head -1)"
  fi
}

gate_no_strata() {
  gate_begin 4 "nothing named '$STRATA_PAT' is serving"
  run_gate_cmd "$CMD_STRATA_SERVE"
  local pids
  pids=$(printf '%s\n' "$OUT" | grep -E '^[0-9]+$' | grep -vx -e "$$" -e "$PPID" || true)
  if [[ -z $pids ]]; then
    report_gate strata-serving 1 'no matching process'
  else
    report_gate strata-serving 0 "serving processes: $(printf '%s' "$pids" | tr '\n' ' ')"
  fi
}

gate_load_below_cores() {
  gate_begin 5 'loadavg (1 min) below the physical core count'
  run_gate_cmd "$CMD_LOADAVG"
  local load=$OUT
  run_gate_cmd "$CMD_CORES"
  local phys
  phys=$(printf '%s\n' "$OUT" | grep -v '^#' | sort -u | wc -l)
  phys=${phys// /}
  if [[ ! $load =~ ^[0-9]+(\.[0-9]+)?$ ]] || (( phys == 0 )); then
    report_gate loadavg 0 "cannot read loadavg ('$load') or core count ($phys)"
    return 0
  fi
  if awk -v l="$load" -v p="$phys" 'BEGIN { exit !(l < p) }'; then
    report_gate loadavg 1 "loadavg1=$load < $phys physical cores"
  else
    report_gate loadavg 0 "loadavg1=$load >= $phys physical cores"
  fi
}

# The device probe: the only device contact before the control. It proves that a
# device enumerates at all (the CUDA_VISIBLE_DEVICES / gfx900 trap fails here) and
# that the start line names arch=gfx906 on the requested PCI address.
gate_probe_device() {
  gate_begin 6 "short probe (${PROBE_DURATION_SEC}s, --mode file): a device enumerates as arch=gfx906"
  PROBE_RUN=1
  PROBE_LOG=$RUN_DIR/probe.log
  build_cmd probe
  PROBE_CMD=${CMD_ARGV[*]}
  local rc=0 errs
  printf 'cmd: %s\n' "$PROBE_CMD" >>"$GATES"
  "${CMD_ARGV[@]}" >/dev/null 2>&1 || rc=$?
  PROBE_RC=$rc
  PROBE_LINE=$(lines_log "$PROBE_LOG" ' start: ' | tail -1)
  errs=$(lines_log "$PROBE_LOG" 'SETUP ERROR|no ROCm-capable device|refusing device|not present' | tail -3)
  {
    printf 'rc: %s\n' "$rc"
    printf 'start line: %s\n' "${PROBE_LINE:-MISSING}"
    if [[ -n $errs ]]; then
      printf 'errors:\n%s\n' "$errs"
    fi
  } >>"$GATES"
  if (( rc == 0 )) && [[ -n $PROBE_LINE ]] && printf '%s' "$PROBE_LINE" | grep -q 'arch=gfx906'; then
    report_gate probe-device 1 'reached the start line with arch=gfx906 (see probe.log)'
  else
    report_gate probe-device 0 "probe rc=$rc; start line: ${PROBE_LINE:-MISSING}; errors: ${errs:-none}"
  fi
}

run_gates() {
  printf '\n[window] pre-run gates (recorded in %s)\n' "$GATES"
  {
    printf 'window start: %s (@%s)\n' "$RUN_START_ISO" "$RUN_START_EPOCH"
    printf 'uname: %s\n' "$(uname -sr)"
    printf 'host: %s\n' "$(uname -n)"
  } >>"$GATES"
  record_caller_env
  gate_mem_available
  gate_kfd_exclusive
  gate_port_free
  gate_no_strata
  gate_load_below_cores
  if (( GATE_FAILED == 0 )); then
    gate_probe_device
  else
    printf '\n=== gate 6: skipped, an earlier gate failed; no device contact ===\n' >>"$GATES"
    printf '  [skip] probe-device    no device contact: an earlier gate already failed\n'
  fi
  printf 'gates: %s/%s PASS%s\n' "$GATE_PASS_COUNT" "$GATE_COUNT" \
    "${GATE_FAIL_LIST:+, FAILED: $GATE_FAIL_LIST}" >>"$GATES"
  printf '[window] gates: %s/%s PASS%s\n' "$GATE_PASS_COUNT" "$GATE_COUNT" \
    "${GATE_FAIL_LIST:+ - FAILED: $GATE_FAIL_LIST}"
}

# ------------------------------------------------------------------- captures

capture_kernel_slice() { # capture_kernel_slice FILE
  {
    printf '# journalctl -k --since @%s --no-pager | grep -Ei "%s"\n' \
      "$RUN_START_EPOCH" "$KERNEL_PAT"
    journalctl -k --since "@$RUN_START_EPOCH" --no-pager 2>&1 | grep -Ei "$KERNEL_PAT" || true
  } >"$1"
}

capture_env() {
  {
    printf 'window start: %s (@%s)\n' "$RUN_START_ISO" "$RUN_START_EPOCH"
    printf 'binary: %s sha256=%s\n' "$REPRO_BIN" "$(sha256sum -- "$REPRO_BIN" | awk '{print $1}')"
    printf 'child env: %s\n' "$CHILD_ENV_TEXT"
    printf 'caller CUDA_VISIBLE_DEVICES=%s GPU_DEVICE_ORDINAL=%s HSA_OVERRIDE_GFX_VERSION=%s\n' \
      "$(caller_val CUDA_VISIBLE_DEVICES)" "$(caller_val GPU_DEVICE_ORDINAL)" \
      "$(caller_val HSA_OVERRIDE_GFX_VERSION)"
    printf 'rocm version: %s\n' "$(cat "$ROCM_DIR/.info/version" 2>/dev/null || printf 'unknown')"
    printf 'kernel: %s\n' "$(uname -sr)"
    printf 'uptime: %s\n' "$(cut -d' ' -f1 /proc/uptime)"
    printf 'loadavg: %s\n' "$(cat /proc/loadavg)"
    printf 'physical cores: %s\n' "$(lscpu -p=Core,Socket | grep -v '^#' | sort -u | wc -l)"
    printf '\nfree -m:\n'
    free -m 2>/dev/null || true
  } >"$RUN_DIR/env.txt"
  grep -E '^(MemTotal|MemFree|MemAvailable|Cached|SwapCached|SwapTotal|SwapFree):' \
    /proc/meminfo >"$RUN_DIR/meminfo-start.txt" || true
}

capture_rocm_smi() { # capture_rocm_smi FILE
  if command -v rocm-smi >/dev/null 2>&1; then
    rocm-smi --showmeminfo vram --showtemp >"$1" 2>&1 || true
  else
    printf 'rocm-smi not installed\n' >"$1"
  fi
}

# ------------------------------------------------------------------ the two legs

lines_log() { # lines_log FILE ERE -> matching lines, never fails
  local file=$1 pat=$2
  [[ -f $file ]] || return 0
  grep -E -e "$pat" -- "$file" || true
}

classify_control() { # classify_control RC
  local rc=$1
  CONTROL_LINE=$(lines_log "$CONTROL_LOG" 'positive control' | tail -1)
  CONTROL_PASS=0
  CONTROL_REFUSE_CODE=3
  if (( rc == 3 )); then
    CONTROL_PASS=1
    if printf '%s' "$CONTROL_LINE" | grep -qF 'memory fault detected as expected'; then
      CONTROL_VERDICT='PASS: exit 3, memory fault detected as expected'
    else
      CONTROL_VERDICT='PASS: exit 3 (WARNING: no "memory fault detected as expected" line)'
    fi
  elif (( rc > 128 )); then
    if [[ -f $CONTROL_LOG ]] && grep -qF 'FATAL signal' "$CONTROL_LOG"; then
      CONTROL_PASS=1
      CONTROL_VERDICT="PASS: died by signal $((rc - 128)) (exit $rc) with a FATAL signal block"
    else
      CONTROL_VERDICT="FAIL: killed by signal $((rc - 128)) (exit $rc) without a FATAL signal block"
    fi
  elif (( rc == 7 )); then
    CONTROL_REFUSE_CODE=7
    CONTROL_VERDICT='INCONCLUSIVE: exit 7, rejected without a memory fault; detector path not proven'
  elif (( rc == 6 )); then
    CONTROL_REFUSE_CODE=6
    CONTROL_VERDICT='FAILED: exit 6, no fault at all; detector path unproven'
  else
    CONTROL_VERDICT="FAIL: exit $rc does not certify the detector"
  fi
}

run_control() {
  local rc=0
  CONTROL_RUN=1
  CONTROL_LOG=$RUN_DIR/control.log
  build_cmd control
  CONTROL_CMD=${CMD_ARGV[*]}
  printf '\n[window] positive control (%ss): %s\n' "$CONTROL_DURATION_SEC" "$CONTROL_CMD"
  stream_start "$CONTROL_LOG"
  run_child "${CMD_ARGV[@]}" || rc=$?
  stream_stop
  CONTROL_RC=$rc
  classify_control "$rc"
  printf '[window] control: exit %s - %s\n' "$rc" "$CONTROL_VERDICT"
  printf 'control cmd: %s\ncontrol exit: %s\nverdict: %s\n' "$CONTROL_CMD" "$rc" \
    "$CONTROL_VERDICT" >>"$GATES"
}

run_long_leg() {
  local rc=0
  LONG_RUN=1
  LONG_LOG=$RUN_DIR/repro.log
  build_cmd long
  LONG_CMD=${CMD_ARGV[*]}
  printf '\n[window] long leg, foreground, %ss: %s\n' "$DURATION_SEC" "$LONG_CMD"
  printf '[window] SIGTERM/SIGINT are forwarded, then up to %ss for the DONE line; a stop is recorded as stopped, never clean.\n' \
    "$DONE_WAIT_SEC"
  stream_start "$LONG_LOG"
  run_child "${CMD_ARGV[@]}" || rc=$?
  stream_stop
  LONG_RC=$rc
  printf '[window] long leg exit %s%s\n' "$rc" "${STOP_SIG:+ (stopped by $STOP_SIG)}"
}

# -------------------------------------------------------------------- summary

count_log() { # count_log FILE ERE -> a number on stdout
  local file=$1 pat=$2 n
  [[ -f $file ]] || {
    printf '0'
    return 0
  }
  n=$(grep -cE -e "$pat" -- "$file" || true)
  printf '%s' "${n:-0}"
}

count_kernel() { # count_kernel FILE ERE -> a number; the '#' header line is skipped
  local file=$1 pat=$2 n
  [[ -f $file ]] || {
    printf '0'
    return 0
  }
  n=$(grep -v '^#' -- "$file" | grep -cE -e "$pat" || true)
  printf '%s' "${n:-0}"
}

long_verdict() {
  local v
  if (( LONG_RUN == 0 )); then
    printf 'not run'
    return 0
  fi
  case $LONG_RC in
    0) v='clean (exit 0)' ;;
    2) v='setup error (exit 2)' ;;
    3) v='HIP fault (exit 3)' ;;
    4) v='checksum fault (exit 4)' ;;
    5) v='churn defect (exit 5)' ;;
    *)
      if (( LONG_RC > 128 )); then
        v="ended by signal $((LONG_RC - 128)) (exit $LONG_RC)"
      else
        v="exit $LONG_RC"
      fi
      ;;
  esac
  if [[ -n $STOP_SIG ]]; then
    if [[ $v == clean* ]]; then
      v="stopped by $STOP_SIG (exit $LONG_RC) - stopped, not clean"
    else
      v="stopped by $STOP_SIG after: $v"
    fi
  fi
  if (( HARD_KILLED == 1 )); then
    v="$v; hard-killed after ${DONE_WAIT_SEC}s without a DONE line"
  fi
  printf '%s' "$v"
}

summarize_cached() { # sets CACHED_TOTAL, CACHED_LAST, CACHED_EVICTED
  local log=$1 cached b a w
  CACHED_TOTAL=0
  CACHED_EVICTED=0
  CACHED_LAST='-'
  cached=$(lines_log "$log" 'Cached before=' |
    sed -E 's/.*Cached before=(-?[0-9]+) after-fadvise=(-?[0-9]+) after-rewrite=(-?[0-9]+) kB.*/\1 \2 \3/')
  while read -r b a w; do
    [[ -n $b ]] || continue
    CACHED_TOTAL=$((CACHED_TOTAL + 1))
    if (( a < b )); then
      CACHED_EVICTED=$((CACHED_EVICTED + 1))
    fi
    CACHED_LAST="before=$b after-fadvise=$a after-rewrite=$w kB"
  done <<<"$cached"
}

write_summary() {
  local out=$RUN_DIR/summary.txt log=$RUN_DIR/repro.log
  local start_line runtime_lines last_hb done_line fault_block fault_count last_cycle probe_faults
  CACHED_TOTAL=0
  CACHED_EVICTED=0
  CACHED_LAST='-'
  summarize_cached "$log"

  start_line=$(lines_log "$log" ' start: ' | tail -1)
  runtime_lines=$(lines_log "$log" ' runtime: ')
  last_hb=$(lines_log "$log" ' HB scans=' | tail -1)
  done_line=$(lines_log "$log" ' DONE ' | tail -1)
  last_cycle=$(lines_log "$log" ' cycle [0-9]+: ' | tail -1)
  fault_block=$(lines_log "$log" \
    'HIP FAULT|CHECKSUM FAULT|FATAL signal|\[fault class\]|SETUP ERROR|CHURN DEFECT|churn [0-9]+: failed')
  fault_count=$(printf '%s\n' "$fault_block" | grep -c . || true)

  {
    printf 'gpu fault repro window: %s\n' "$RUN_START_ISO"
    printf 'written            : %s\n' "$(date -Is)"
    printf 'run dir            : %s\n' "$RUN_DIR"
    printf 'binary             : %s sha256=%s\n' "$REPRO_BIN" \
      "$(sha256sum -- "$REPRO_BIN" | awk '{print $1}')"
    printf 'child env          : %s\n' "$CHILD_ENV_TEXT"
    printf 'caller env         : CUDA_VISIBLE_DEVICES=%s GPU_DEVICE_ORDINAL=%s HSA_OVERRIDE_GFX_VERSION=%s (not inherited)\n' \
      "$(caller_val CUDA_VISIBLE_DEVICES)" "$(caller_val GPU_DEVICE_ORDINAL)" \
      "$(caller_val HSA_OVERRIDE_GFX_VERSION)"
    printf 'options            : duration=%ss cycle=%ss host-write=%ss dev=%s pci=%s skip-control=%s\n' \
      "$DURATION_SEC" "$CYCLE_SEC" "$HOST_WRITE_SEC" "$DEV" "${PCI:-none}" \
      "$([[ $SKIP_CONTROL == 1 ]] && printf yes || printf no)"
    printf 'wall time          : %ss (start @%s, end @%s)\n' \
      "$SECONDS" "$RUN_START_EPOCH" "${RUN_END_EPOCH:-?}"
    if (( GATE_FAILED == 1 )); then
      printf 'pre-run gates      : REFUSED - failed: %s (%s/%s passed; see gates.txt)\n' \
        "$GATE_FAIL_LIST" "$GATE_PASS_COUNT" "$GATE_COUNT"
    elif (( GATE_COUNT > 0 )); then
      printf 'pre-run gates      : %s/%s PASS (readings in gates.txt)\n' \
        "$GATE_PASS_COUNT" "$GATE_COUNT"
    else
      printf 'pre-run gates      : not run\n'
    fi
    if (( PROBE_RUN == 1 )); then
      printf 'device probe       : rc=%s %s\n' "${PROBE_RC:-?}" "${PROBE_LINE:-MISSING}"
      probe_faults=$(lines_log "$PROBE_LOG" 'HIP FAULT|CHECKSUM FAULT|FATAL signal|SETUP ERROR')
      if [[ -n $probe_faults ]]; then
        printf 'probe faults       :\n'
        printf '%s\n' "$probe_faults" | sed 's/^/  /'
      fi
    else
      printf 'device probe       : not run (no device contact)\n'
    fi
    printf '\n-- positives --\n'
    if (( SKIP_CONTROL == 1 )); then
      printf 'control            : SKIPPED (--skip-control)\n'
      printf '!!! WARNING: the positive control was skipped. Nothing in this window proves\n'
      printf '!!! that the detector fires, so a clean long leg below is UNINTERPRETABLE\n'
      printf '!!! ("no HIP fault" cannot be told apart from a detector that never triggers).\n'
    elif (( CONTROL_RUN == 0 )); then
      printf 'control            : NOT RUN\n'
    else
      printf 'control            : exit=%s verdict=%s\n' "$CONTROL_RC" "$CONTROL_VERDICT"
      printf 'control line       : %s\n' "${CONTROL_LINE:-missing}"
      printf 'control log        : %s\n' "$CONTROL_LOG"
    fi
    printf 'long leg command   : %s\n' "${LONG_CMD:-not run}"
    printf 'long leg exit code : %s\n' "${LONG_RC:-not run}"
    if [[ -n $STOP_SIG ]]; then
      printf 'stop               : %s at %s (forwarded; DONE %s)\n' "$STOP_SIG" "$STOP_AT" \
        "$([[ -n $done_line ]] && printf seen || printf missing)"
    else
      printf 'stop               : none\n'
    fi
    printf '\n-- from repro.log --\n'
    printf 'start line         : %s\n' "${start_line:-MISSING}"
    printf 'runtime lines      :\n'
    if [[ -n $runtime_lines ]]; then
      printf '%s\n' "$runtime_lines" | sed 's/^/  /'
    else
      printf '  MISSING\n'
    fi
    printf 'last HB            : %s\n' "${last_hb:-MISSING}"
    printf 'cycles             : %s cycle lines; last: %s\n' \
      "$(count_log "$log" ' cycle [0-9]+: ')" "${last_cycle:-none}"
    printf 'cached samples     : %s lines with Cached before=; eviction (after-fadvise < before) in %s of %s; last %s\n' \
      "$CACHED_TOTAL" "$CACHED_EVICTED" "$CACHED_TOTAL" "$CACHED_LAST"
    printf 'faults             : HIP FAULT=%s CHECKSUM FAULT=%s FATAL signal=%s [fault class]=%s SETUP ERROR=%s CHURN DEFECT=%s churn failed=%s\n' \
      "$(count_log "$log" 'HIP FAULT')" "$(count_log "$log" 'CHECKSUM FAULT')" \
      "$(count_log "$log" 'FATAL signal')" "$(count_log "$log" '\[fault class\]')" \
      "$(count_log "$log" 'SETUP ERROR')" "$(count_log "$log" 'CHURN DEFECT')" \
      "$(count_log "$log" 'churn [0-9]+: failed')"
    printf 'fault lines        :\n'
    if (( fault_count > 0 )); then
      printf '%s\n' "$fault_block" | head -20 | sed 's/^/  /'
      if (( fault_count > 20 )); then
        printf '  ... %s more\n' "$((fault_count - 20))"
      fi
    else
      printf '  none\n'
    fi
    printf 'DONE line          : %s\n' "${done_line:-MISSING}"
    printf 'verdict            : %s\n' "$(long_verdict)"
    printf 'exit code          : %s (this script)\n' "$FINAL_EXIT"
    printf '\n-- kernel slice (journalctl -k --since @%s) --\n' "$RUN_START_EPOCH"
    printf 'kernel-before      : %s matched lines (kernel-before.txt)\n' \
      "$(count_kernel "$RUN_DIR/kernel-before.txt" "$KERNEL_PAT")"
    if [[ -f $RUN_DIR/kernel-after.txt ]]; then
      printf 'kernel-after       : %s matched lines (kernel-after.txt)\n' \
        "$(count_kernel "$RUN_DIR/kernel-after.txt" "$KERNEL_PAT")"
      printf 'kernel-after XDMA  : %s (no-retry: %s; permission fault: %s)\n' \
        "$(count_kernel "$RUN_DIR/kernel-after.txt" 'XDMA')" \
        "$(count_kernel "$RUN_DIR/kernel-after.txt" 'no-retry')" \
        "$(count_kernel "$RUN_DIR/kernel-after.txt" 'permission fault')"
      if [[ -n $PCI ]]; then
        printf 'kernel-after on amdgpu %s: no-retry=%s XDMA=%s\n' "$PCI" \
          "$(count_kernel "$RUN_DIR/kernel-after.txt" "amdgpu $PCI:.*no-retry")" \
          "$(count_kernel "$RUN_DIR/kernel-after.txt" "amdgpu $PCI:.*XDMA")"
      fi
    else
      printf 'kernel-after       : not captured (the window did not reach the long leg)\n'
    fi
  } >"$out"
  printf '\n[window] summary: %s\n' "$out"
}

# ----------------------------------------------------------------------- plan

print_plan() {
  RUN_DIR='<run dir>'
  printf '[plan] nothing is executed and no device is touched; this is the dry listing of one window.\n\n'
  printf 'run dir            : %s/<YYYYmmdd-HHMMSS>/\n' "$RUN_ROOT"
  printf '                     gates.txt probe.log control.log repro.log kernel-before.txt kernel-after.txt\n'
  printf '                     summary.txt env.txt meminfo-start.txt rocm-smi-start.txt rocm-smi-end.txt\n'
  printf 'scratch in run dir : scan.bin 64 MiB + park.bin 8 MiB (the repro --dummy-dir)\n'
  printf 'binary             : %s\n' "$REPRO_BIN"
  printf 'child env          : %s\n' "$CHILD_ENV_TEXT"
  printf 'options            : duration=%ss cycle=%ss host-write=%ss dev=%s pci=%s skip-control=%s\n\n' \
    "$DURATION_SEC" "$CYCLE_SEC" "$HOST_WRITE_SEC" "$DEV" "${PCI:-none}" \
    "$([[ $SKIP_CONTROL == 1 ]] && printf yes || printf no)"
  printf 'pre-run gates (each recorded in gates.txt; any failure refuses with exit 2):\n'
  printf '  0 record the caller environment (CUDA_VISIBLE_DEVICES, GPU_DEVICE_ORDINAL,\n'
  printf '    HSA_OVERRIDE_GFX_VERSION, LD_LIBRARY_PATH) - never inherited by the repro\n'
  printf '  1 MemAvailable >= %s kB (15 GiB)\n     %s\n' "$MIN_MEM_AVAIL_KIB" "$CMD_MEMINFO"
  printf '  2 no other process on %s\n     %s  (must print nothing)\n     %s  (no "GPU[n] : <pid>" lines)\n     ls /sys/class/kfd/kfd/proc/  (must be empty: the kernel KFD context list)\n' \
    "$KFD_DEV" "$CMD_FUSER" "$CMD_SMI_PIDS"
  printf '  3 no listener on :%s\n     %s  (must be empty)\n' "$PORT" "$CMD_PORT"
  printf '  4 nothing named "%s" is serving\n     %s  (must be empty)\n' "$STRATA_PAT" "$CMD_STRATA_SERVE"
  printf '  5 loadavg 1 min below the physical core count\n     %s ; %s\n' "$CMD_LOADAVG" "$CMD_CORES"
  build_cmd probe
  printf '  6 device probe: %s\n' "${CMD_ARGV[*]}"
  printf '    pass = exit 0, a start: line, and arch=gfx906 in it; the start line goes to summary.txt\n\n'
  printf 'capture at start   : journalctl -k --since @<start-epoch> | grep -Ei "%s" > kernel-before.txt\n' \
    "$KERNEL_PAT"
  printf '                     rocm-smi --showmeminfo vram --showtemp > rocm-smi-start.txt\n\n'
  if (( SKIP_CONTROL == 1 )); then
    printf 'positive control   : SKIPPED (--skip-control): summary.txt then warns loudly that a\n'
    printf '                     clean long leg is uninterpretable\n\n'
  else
    build_cmd control
    printf 'positive control   : %s\n' "${CMD_ARGV[*]}"
    printf '                     pass = exit 3, or 128+signal with a FATAL signal block in control.log;\n'
    printf '                     exit 6 -> script exits 6, exit 7 -> 7, anything else -> refused, exit 3\n\n'
  fi
  build_cmd long
  printf 'long leg           : %s\n' "${CMD_ARGV[*]}"
  printf '                     runs in the foreground, repro.log streamed live (tail -F)\n\n'
  printf 'capture at end     : kernel-after.txt (same journalctl query), rocm-smi-end.txt, summary.txt\n'
  printf 'stop               : SIGTERM/SIGINT forwarded, then up to %ss for the DONE line\n' "$DONE_WAIT_SEC"
  printf '                     (SIGKILL after that); a stop is recorded as stopped, never clean\n'
  printf 'exit codes         : 0 clean/stopped-clean, 2 usage|setup|gate refusal, 3 refused (control)\n'
  printf '                     or HIP fault, 4 checksum fault, 5 churn defect, 6 control no-error,\n'
  printf '                     7 control inconclusive, 128+n signalled\n'
}

# ----------------------------------------------------------------------- main

main() {
  trap on_exit EXIT
  trap 'handle_stop TERM' TERM
  trap 'handle_stop INT' INT
  # Job control: a non-interactive shell starts background children with SIGINT
  # ignored, so a forwarded SIGINT would be dropped. With monitor mode on, the
  # child gets its own process group and the forwarded signal is delivered.
  set -m

  parse_args "$@"
  check_binary

  if (( PLAN == 1 )); then
    print_plan
    return 0
  fi

  SECONDS=0
  RUN_START_EPOCH=$(date +%s)
  RUN_START_ISO=$(date -Is)
  make_run_dir

  run_gates
  capture_env
  capture_rocm_smi "$RUN_DIR/rocm-smi-start.txt"
  capture_kernel_slice "$RUN_DIR/kernel-before.txt"

  if (( GATE_FAILED == 1 )); then
    printf '\n[window] REFUSING: %s pre-run gate(s) failed: %s\n' \
      "$((GATE_COUNT - GATE_PASS_COUNT))" "$GATE_FAIL_LIST"
    printf 'verdict: refused before start (gates)\n' >>"$GATES"
    finish 2
  fi

  if (( SKIP_CONTROL == 1 )); then
    printf '\n[window] !!! positive control SKIPPED: a clean long leg is uninterpretable !!!\n'
  else
    run_control
    if [[ -n $STOP_SIG ]]; then
      printf '[window] interrupted by %s during the control; refusing the long leg\n' "$STOP_SIG"
      finish 3
    fi
    if (( CONTROL_PASS == 0 )); then
      printf '\n[window] REFUSING the long leg: the control did not certify the detector (%s)\n' \
        "$CONTROL_VERDICT"
      finish "$CONTROL_REFUSE_CODE"
    fi
  fi

  run_long_leg

  capture_kernel_slice "$RUN_DIR/kernel-after.txt"
  capture_rocm_smi "$RUN_DIR/rocm-smi-end.txt"
  finish "${LONG_RC:-2}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
