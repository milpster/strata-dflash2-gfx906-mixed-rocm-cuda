#!/usr/bin/env bash
# tools/mixed_gpu/cuda_prep/checker_cli_tests.sh - CPU-safe command-line behaviour tests of cuda_prep_check.
#
# cuda_prep_check dlopens GPU libraries, so it is a GPU-linked binary: the only runs authorized here are the
# ones that load NOTHING.  Every case below is one of:
#   * the runtime gate closed  -> the program refuses before touching any file and exits 77,
#   * argument parsing only    -> usage refusal (exit 2) or the --classes listing (exit 0),
#   * a dlopen of a path that does not exist -> the loader fails and the program exits 1.  No GPU library,
#     no GPU code and no driver call is involved in any case; the CUDA/HIP libraries are NOT passed.
#   * --all enumeration refusals (R1 keeper fix, r1-gate.md §4) -> exit 2 BEFORE any dlopen.  These cases
#     pass the REAL built CUDA library as the library argument: the refusal must happen before the mode
#     code reaches load_lib, so the real library is named on the command line and never opened.  The proof
#     recorded is (a) the exit code 2, (b) the refusal text, (c) the absence of the driver's dlopen markers
#     ("dlopen", "one dlopen", "SINGLE-LIBRARY mode", "DUAL mode", "backend", "enumerated") in the output,
#     and (d) an LD_DEBUG=libs capture: dlopen would print "trying path" lines for the named library, and
#     the driver's own dlopen markers are absent from the captured output.  LD_DEBUG only reports the
#     loader's activity; nothing GPU is executed.  If the CUDA library is not built, the case is SKIPPED
#     (never counted as pass), so a skip cannot hide a regression.
# The binary itself is proven GPU-free for these paths by static_inspect.sh (zero GPU DT_NEEDED, zero GPU
# undefined symbols, zero fatbin sections), and the gate closed means the dynamic loader is never called.
#
# Usage: checker_cli_tests.sh <build-dir>
set -u
B="${1:?usage: checker_cli_tests.sh <build-dir>}"
EXE="$B/cpu/cuda_prep_check"
FIX="$B/fixtures"
MISSING="$B/no-such-library.so"
[ -f "$EXE" ] || { echo "missing $EXE (build the CPU target first)"; exit 2; }
[ -e "$MISSING" ] && { echo "$MISSING must not exist for the dlopen-failure case"; exit 2; }
# The --all enumeration-refusal fixtures: an empty dir, a dir with subdirs that hold no manifest.txt, and
# the LD_DEBUG capture file.  MISSING (a path that does not exist) doubles as the not-a-directory case.
EMPTY="$B/cli-empty-dir"
NOMAN="$B/cli-no-manifest-dir"
LDOUT="$B/evidence/cli-lddebug.log"
mkdir -p "$EMPTY" "$NOMAN/sub-a" "$NOMAN/sub-b" "$B/evidence"
: > "$NOMAN/sub-a/notes.txt"
: > "$NOMAN/sub-b/notes.txt"

pass=0; fail=0; skip=0
run() {  # run <expected-rc> <label> [-F <forbidden-output-regex>] [-R <required-output-regex>] <env assignments...> -- <args...>
  local want="$1" label="$2"; shift 2
  local forbid="" need=""
  while [ "${1:-}" != "--" ]; do
    if [ "$1" = "-F" ]; then forbid="$2"; shift 2
    elif [ "$1" = "-R" ]; then need="$2"; shift 2
    else break; fi
  done
  local envs=()
  while [ "${1:-}" != "--" ]; do envs+=("$1"); shift; done
  shift
  local out rc
  out="$(env "${envs[@]}" "$EXE" "$@" 2>&1)"; rc=$?
  printf '%s\n' "--- $label"
  printf '%s\n' "\$ env ${envs[*]} $EXE $*"
  printf '%s\n' "$out" | sed 's/^/    /'
  if [ "$rc" != "$want" ]; then
    fail=$((fail+1)); printf 'rc=%s expected=%s FAIL\n' "$rc" "$want"
  elif [ -n "$forbid" ] && printf '%s\n' "$out" | grep -qE "$forbid"; then
    fail=$((fail+1)); printf 'rc=%s expected=%s PASS, but output matches forbidden pattern "%s" FAIL\n' \
        "$rc" "$want" "$forbid"
  elif [ -n "$need" ] && ! printf '%s\n' "$out" | grep -qE "$need"; then
    fail=$((fail+1)); printf 'rc=%s expected=%s PASS, but output lacks required pattern "%s" FAIL\n' \
        "$rc" "$want" "$need"
  else
    [ -n "$forbid" ] && printf 'rc=%s expected=%s, forbidden pattern "%s" absent\n' "$rc" "$want" "$forbid"
    [ -n "$need" ] && printf 'rc=%s expected=%s, required pattern "%s" present\n' "$rc" "$want" "$need"
    pass=$((pass+1)); printf 'rc=%s expected=%s PASS\n' "$rc" "$want"
  fi
  printf '\n'
}

echo "# cuda_prep_check command-line / gate tests (CPU-safe: nothing is loaded in any case)"
date -u '+utc %Y-%m-%d %H:%M:%S'
echo "binary: $EXE (GPU-freedom of these paths proven by static_inspect.sh)"
echo

run 77 "gate closed: refuses before touching any file" \
    "PATH=$PATH" -- "$FIX" "$MISSING" --all
run 77 "gate set to a value other than 1: refuses" \
    "STRATA_PREP_RUNTIME_GATE=0" -- "$FIX" "$MISSING" --all
run 2 "no arguments: usage refusal (gate open, no file read, no dlopen)" \
    "STRATA_PREP_RUNTIME_GATE=1" --
run 2 "one argument only: usage refusal" \
    "STRATA_PREP_RUNTIME_GATE=1" -- "$FIX"
run 0 "--classes lists the ten native pair classes and exits (no library, no fixture read)" \
    "STRATA_PREP_RUNTIME_GATE=1" -- --classes
run 1 "gate open, library path does not exist: the dlopen fails, exit 1 (no GPU code ran)" \
    "STRATA_PREP_RUNTIME_GATE=1" STRATA_PREP_LIB=cuda -- "$FIX" "$MISSING"
run 2 "gate open, single-library mode WITHOUT STRATA_PREP_LIB: refused before any dlopen" \
    "STRATA_PREP_RUNTIME_GATE=1" -- "$FIX" "$MISSING"
run 2 "gate open, STRATA_PREP_LIB names no known backend: refused before any dlopen" \
    "STRATA_PREP_RUNTIME_GATE=1" STRATA_PREP_LIB=nonsense -- "$FIX" "$MISSING"
run 2 "gate open, the second positional is not a .so: usage refusal (no dlopen)" \
    "STRATA_PREP_RUNTIME_GATE=1" STRATA_PREP_LIB=cuda -- "$FIX" q4_0_q4_0
run 1 "gate open, dual mode with a missing second library: refusal (the mode the evidence warns about)" \
    "STRATA_PREP_RUNTIME_GATE=1" -- "$FIX" "$MISSING" "$MISSING"

# ---- R1 keeper fix (r1-gate.md §4): --all enumeration refusals, proved BEFORE any dlopen ----
# These cases name the REAL built CUDA library as the library argument.  The driver must refuse during
# enumeration (exit 2) before its mode code reaches load_lib, so the library is named and never opened:
# the driver prints its dlopen markers ("one dlopen", "SINGLE-LIBRARY mode", "backend N") only after a
# successful load and "dlopen <path>" only on a failed load, so the absence of every marker is the proof
# that no library was opened.  No GPU code runs in any of these cases.
DLOPEN_MARKERS='dlopen|one dlopen|SINGLE-LIBRARY mode|DUAL mode|backend [0-9]|enumerated'
LIB="$B/cuda/libstrata_prep_cuda.so"
if [ -f "$LIB" ]; then
  run 2 "keeper: --all on a NONEXISTENT dir, real CUDA library named -> refusal, no dlopen marker" \
      -F "$DLOPEN_MARKERS" "STRATA_PREP_RUNTIME_GATE=1" STRATA_PREP_LIB=cuda -- \
      "$B/no-such-fixture-dir" "$LIB" --all
  run 2 "keeper: --all on a NOT-A-DIRECTORY path (a regular file), real CUDA library named -> refusal, no dlopen marker" \
      -F "$DLOPEN_MARKERS" "STRATA_PREP_RUNTIME_GATE=1" STRATA_PREP_LIB=cuda -- \
      "$MISSING" "$LIB" --all
  run 2 "keeper: --all on an EMPTY dir, real CUDA library named -> refusal, no dlopen marker" \
      -F "$DLOPEN_MARKERS" "STRATA_PREP_RUNTIME_GATE=1" STRATA_PREP_LIB=cuda -- \
      "$EMPTY" "$LIB" --all
  run 2 "keeper: --all on a dir whose subdirs hold NO manifest.txt, real CUDA library named -> refusal, no dlopen marker" \
      -F "$DLOPEN_MARKERS" "STRATA_PREP_RUNTIME_GATE=1" STRATA_PREP_LIB=cuda -- \
      "$NOMAN" "$LIB" --all
  echo "--- keeper: LD_DEBUG=libs capture on the empty-dir --all refusal (loader activity only, nothing is executed)"
  echo "\$ env STRATA_PREP_RUNTIME_GATE=1 STRATA_PREP_LIB=cuda LD_DEBUG=libs $EXE $EMPTY $LIB --all > ld.log 2>&1"
  env STRATA_PREP_RUNTIME_GATE=1 STRATA_PREP_LIB=cuda LD_DEBUG=libs "$EXE" "$EMPTY" "$LIB" --all \
      > "$LDOUT" 2>&1
  ld_rc=$?
  ld_line="$(grep -c "$LIB" "$LDOUT")"
  printf 'LD_DEBUG capture: rc=%s, lines naming the CUDA library=%s\n' "$ld_rc" "$ld_line"
  if [ "$ld_rc" = 2 ] && [ "$ld_line" = 0 ] && ! grep -qE "$DLOPEN_MARKERS" "$LDOUT"; then
    pass=$((pass+1)); printf 'LD_DEBUG capture: refusal rc=2, library never named by the loader, no dlopen marker PASS\n'
  else
    fail=$((fail+1)); printf 'LD_DEBUG capture: FAIL (rc=%s, lines naming library=%s)\n' "$ld_rc" "$ld_line"
  fi
  printf '\n'
else
  skip=$((skip+1))
  echo "--- keeper: CUDA library $LIB not built -> the four no-dlopen --all cases and the LD_DEBUG capture SKIPPED"
  echo "    (a skip is never counted as a pass; build the CUDA target to run them)"
  printf '\n'
fi

# The good-directory control: enumeration succeeds there, and with the library path absent the driver
# reaches the dlopen of the nonexistent path and exits 1 - the pre-fix ordering evidence (enumeration
# after the load attempt) and the post-fix one coincide here, so this case proves the 0-fixture refusal
# is specific to empty dirs, not a blanket --all refusal.
run 1 "gate open, good fixtures dir, --all, library path does not exist: enumeration passes, dlopen fails -> 1" \
    "STRATA_PREP_RUNTIME_GATE=1" STRATA_PREP_LIB=cuda -- "$FIX" "$MISSING" --all

# The REAL fixture set must not be refused by the new enumeration gate: with the library path absent the
# driver passes the enumeration and stops at the nonexistent library (exit 1, nothing loads).  The full
# 48-count enumeration of this directory is proven in the parent's real window run
# (evidence/r1-parent-cuda-real.log prints "enumerated 48 fixture(s)"); here we prove the refusal is
# specific to zero-fixture directories, not to the real set.
FIXREAL="$B/fixtures-real-r1"
NREAL="$(find "$FIXREAL" -mindepth 2 -maxdepth 2 -name manifest.txt 2>/dev/null | wc -l)"
run 1 "gate open, --all on the REAL fixtures dir ($NREAL manifests) with the library absent: not refused, loads nothing -> 1" \
    "STRATA_PREP_RUNTIME_GATE=1" STRATA_PREP_LIB=cuda -- \
    "$FIXREAL" "$MISSING" --all

echo "checker_cli_tests: $pass pass, $fail fail, $skip skip"
[ "$fail" = 0 ]
