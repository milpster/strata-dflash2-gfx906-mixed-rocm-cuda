#!/usr/bin/env bash
# prod_soak.sh - sustained serving load against the live prod engine on :8009.
#
# Why: the promoted tuple has only been measured in short windows. This runs a
# real conversation mix against the engine prod actually serves, so the GPU is
# occupied productively and we get (a) stability evidence at the promoted tuple,
# (b) multi-turn park/restore behaviour, (c) decode rates at two context sizes.
#
# It is NOT a claim sample: one arm, no interleave, and the request shape is
# prod's own serving shape (thinking on, reasoning_budget 2000).
#
# Cold prompts: each leg reads a HEAD-tagged corpus file through --prompt-file.
# The driver's own build_prompt() puts its identity tag at the TAIL, which leaves
# the leading tokens identical between two same-size requests, so the engine
# serves the prefix from the conversation cache and a "fresh prefill" leg is
# silently warm. A head tag diverges at token zero, which is what forces a fresh
# prefill (champions_prepare.build_paired_prompt). Every leg therefore carries
# --prompt-file and the sha256 of exactly those bytes, and its judge line is
# checked for a reused count before the leg is read as a prefill sample.
#
# One corpus per LEG, not per ctx. Alternating two corpora made legs 1 and 3 send
# identical bytes with an identical head tag, and the engine reused the prefix:
# the old soak records n2/n3 read 58423 tokens with 49152 reused after an earlier
# leg at the same ctx. Leg N is built from build_paired_prompt("soak-nN", 0, ctx),
# so every leg's tag is unique and it diverges from every other leg at token zero.
#
# Determinism: build_paired_prompt(work, pair, ctx) is deterministic per
# (work, pair, ctx) - fixed seed, no timestamps, no per-run text. A regenerated
# soak-nN corpus is therefore byte-identical to any other soak-nN corpus of that
# ctx (the file is cached under that name, so leg N of a re-run sends the same
# bytes), and it is deliberately NOT identical to a shape corpus of the same size:
# the work name is in the head tag, so "same size, same seed" never means "same
# bytes" here, and a soak corpus can never be mistaken for, or silently
# substituted for, the bytes an earlier measurement used. Two soak legs at the
# same ctx differ too, by design (soak-n1 vs soak-n3). Generation is input
# preparation, not a new input, and the digest is pinned in every record.
#
# Warm legs are not, by themselves, a harness fault. The soak's evidence is
# stability, multi-turn park/restore and decode rates as well as prefill, so a leg
# that reused a prefix is kept and counted with its memory samples and fault
# counts, reported as "warm, not a prefill sample", and its rate is never read as
# a prefill rate. A warm FIRST leg, or three consecutive warm legs, is a harness
# fault and stops the run.
#
# Corpora, by purpose:
#   1. bench/fault-va/out/corpus      soak-owned: one generated file per leg, cached
#   2. .../port-ab-20261123/corpus    frozen W2 window evidence: READ-ONLY, and read
#      only to answer "where did these bytes come from" for --check-leg. It is never
#      the input of a leg.
#
# Usage: prod_soak.sh [MINUTES] [--dry-run]
#        prod_soak.sh --check-leg RECORD [SHA256]
#   CTXPAIR=<bytes>,<bytes> selects the context pair (default 80000,16000).
#   --dry-run resolves each planned leg's corpus (computing its digest, writing
#           nothing), prints the planned commands; nothing is sent.
#   --check-leg runs the coldness gate on a leg record from any lane, with no engine
#           and no corpus involved. It prints the record's own prompt_sha256, asserts
#           the record's ctx equals its prompt_bytes (and the leg ctx inside a soak),
#           and reports which corpus, if any, holds those bytes.
#   exit codes: 0 cold; 1 warm (check-leg), or the soak aborted on a harness-level
#           warm alarm; 2 unreadable/stale record or no judge line; 3 prompt sha256
#           differs; 4 the record has no prompt_sha256; 5 record ctx is not the
#           expected one.
set -uo pipefail
cd /mnt/srcds/Strata || exit 1
PY=python3
MIN=40
DRY=0
CHECK_LEG=""
CHECK_SHA=""
CTXPAIR=${CTXPAIR:-80000,16000}
OUTR=bench/fault-va/out/records/prod-soak
ELOG=logs/engine-prod-port0218.log
HLOG=logs/prod-iq3xxs-ud-mixed-helper.log
# Generated prompts live here; the frozen W2 tree is never written.
SOAK_CORPUS_DIR=bench/fault-va/out/corpus
# Frozen W2 window evidence: read-only, --check-leg provenance only.
CORPUS_DIR=bench/ab-v0210/rpcprof/out/port-ab-20261123/corpus
CORPUS_ROOTS=("$SOAK_CORPUS_DIR" "$CORPUS_DIR")
say() { echo "[$(date +%H:%M:%S)] $*"; }

# Every corpus file with its byte size and head tag, for the fail-loudly message.
list_corpora() {
  local root f
  for root in "${CORPUS_ROOTS[@]}"; do
    [ -d "$root" ] || continue
    for f in "$root"/*.bin; do
      [ -e "$f" ] || continue
      printf '  %8s bytes  %-52s %s\n' "$(stat -c%s "$f")" "$f" \
        "$(head -c 200 "$f" | head -1)"
    done
  done
}

# find_corpus <ctx> [sha256] - print "<path> work=<work> <soak|shape>-corpus" for the
# corpus files whose size AND embedded ctx equal <ctx>, soak root first, frozen W2 dir
# second; with a sha256, only the file that holds exactly those bytes. The work name is
# the point of this report: "dec144k" is a shape corpus, "soak-n3" is a soak corpus, and
# two corpora of the same size are different inputs, not the same input.
find_corpus() {
  local ctx=$1 want=${2:-} root f tag work kind found=1
  for root in "${CORPUS_ROOTS[@]}"; do
    [ -d "$root" ] || continue
    for f in "$root"/*.bin; do
      [ -e "$f" ] || continue
      [ "$(stat -c%s "$f")" -eq "$ctx" ] || continue
      tag=$(head -c 200 "$f" | head -1)
      case "$tag" in
        *"ctx=$ctx]"*) ;;
        *) continue ;;
      esac
      if [ -n "$want" ] && [ "$(sha256sum "$f" | cut -d' ' -f1)" != "$want" ]; then
        continue
      fi
      work=none
      case "$tag" in
        *work=*) work=${tag#*work=}; work=${work%% *} ;;
      esac
      kind=shape
      case "$work" in
        soak*) kind=soak ;;
      esac
      printf '%s work=%s %s-corpus\n' "$f" "$work" "$kind"
      found=0
      [ -n "$want" ] && return 0
    done
  done
  return "$found"
}

# corpus_is <file> <work> <ctx> - the head tag names this work and ctx, and the byte
# count is exactly ctx. A cached file that fails this belongs to another leg and is
# regenerated, never served.
corpus_is() {
  local f=$1 work=$2 ctx=$3 tag
  [ "$(stat -c%s "$f")" -eq "$ctx" ] || return 1
  tag=$(head -c 200 "$f" | head -1)
  case "$tag" in
    *"work=$work "*) ;;
    *) return 1 ;;
  esac
  case "$tag" in
    *"ctx=$ctx]"*) return 0 ;;
  esac
  return 1
}

# gen_corpus <work> <ctx> <dest> <dry 0|1> - print "<bytes> <sha256>".
# build_paired_prompt(work, pair, ctx) is deterministic per (work, pair, ctx): fixed
# seed, no timestamps, no per-run text, exactly ctx bytes with the identity tag at the
# HEAD. Because the work name is in that tag, regenerating soak-nN reproduces soak-nN
# bytes and NOT the bytes of a shape corpus of the same size, which is what the digest
# in every record asserts. dry=1 computes the digest and writes nothing.
gen_corpus() {
  $PY - "$3" "$2" "$1" "$4" <<'PY'
import hashlib, pathlib, sys
sys.path.insert(0, "bench/ab-v0210/rpcprof")
import champions_prepare
dest, ctx, work, dry = pathlib.Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3], sys.argv[4] == "1"
blob = champions_prepare.build_paired_prompt(work, 0, ctx)
assert len(blob) == ctx, (ctx, len(blob))
if not dry:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(blob)
print(len(blob), hashlib.sha256(blob).hexdigest())
PY
}

# ensure_leg_corpus <leg> <ctx> [dry] - print "<path> <sha256>" for THIS leg's corpus.
# Each leg gets its own work name, so its head tag differs from every other leg's and
# its prompt diverges at token zero. The file is cached under the soak root so a re-run
# of leg N sends the same bytes again; a cached file that does not carry this leg's work
# tag and ctx is regenerated. The frozen W2 corpora are never consulted here: they exist
# for --check-leg provenance only, so no leg inherits a shape corpus's prefix.
ensure_leg_corpus() {
  local leg=$1 ctx=$2 dry=${3:-0} path out bytes sha
  path="$SOAK_CORPUS_DIR/$leg.ctx$ctx.bin"
  if [ -f "$path" ] && corpus_is "$path" "$leg" "$ctx"; then
    printf '%s %s\n' "$path" "$(sha256sum "$path" | cut -d' ' -f1)"
    return 0
  fi
  if [ -f "$path" ]; then
    say "cached $path does not carry work=$leg ctx=$ctx; regenerating" >&2
  fi
  if ! out=$(gen_corpus "$leg" "$ctx" "$path" "$dry"); then
    say "corpus generator failed for leg $leg ctx $ctx: no corpus was written" >&2
    say "available corpora:" >&2
    list_corpora >&2
    return 1
  fi
  bytes=${out%% *}
  sha=${out##* }
  if [ "$dry" -eq 1 ]; then
    say "would generate $path ($bytes bytes, sha256 $sha)" >&2
  else
    say "generated head-tagged corpus $path ($bytes bytes, sha256 $sha)" >&2
  fi
  printf '%s %s\n' "$path" "$sha"
}

# judge_leg <record> [expected sha] [expected ctx] [leg start epoch] - read the leg's
# FIRST request only and set STATUS REUSED DIGEST JUDGE NOTE PBYTES. --kind tg sends
# the same prompt twice; request 1 is fully cached by design, so its judge line
# ("... 143888 reused + 5 read in 217 ms (23.1 tok/s)") is never a prefill number and is
# never printed. Request 0 is read from its own [elog_start, elog_end) slice, because
# the engine log is shared with every other leg and with the second request.
# STATUS: cold (reused=0) | warm (reused>0) | unreadable | stale | no-judge |
#         no-record-digest | sha-mismatch | ctx-mismatch
judge_leg() {
  local rec=$1 want=${2:-} ctx=${3:-} min_ts=${4:-} out
  STATUS='' REUSED='' DIGEST='' JUDGE='' NOTE='' PBYTES=''
  if ! out=$($PY - "$rec" "$want" "$ctx" "$min_ts" <<'PY'
import json, re, sys

REC = re.compile(r"prompt \d+ tokens = (\d+) reused \+ \d+ read in \d+ ms[^\n]*")


def emit(status, reused="", digest="", judge="", note="", pbytes=""):
    # US (0x1f), not tab: tab is IFS whitespace, so a run of tabs collapses and an
    # empty NOTE would be read as the next field's value.
    print("\x1f".join([status, reused, digest, judge, note, pbytes]))
    raise SystemExit(0)


path, want, want_ctx, min_ts = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
try:
    with open(path) as f:
        rec = json.load(f)
    if not isinstance(rec, dict):
        raise ValueError("top level is not a JSON object")
except (OSError, ValueError) as error:
    emit("unreadable", note=f"{path}: {error}")

digest = str(rec.get("prompt_sha256") or "")
raw_bytes = rec.get("prompt_bytes")
pbytes = "" if raw_bytes is None else str(raw_bytes)
ctx = rec.get("ctx")
try:
    ts = float(rec.get("ts") or 0)
except (TypeError, ValueError):
    ts = 0.0

# A record left by an earlier run of the same leg name would otherwise be judged as
# this leg's evidence when the driver fails to write a new one.
if min_ts:
    try:
        started = float(min_ts)
    except ValueError:
        started = 0.0
    if ts and started and ts < started:
        emit("stale", digest=digest, pbytes=pbytes,
             note=f"record ts {ts:.0f} predates this leg ({started:.0f}): a previous run's file")

# Inside a soak the expected ctx is the leg's; for a standalone record the expectation
# is the record's own prompt length, so a record cannot disagree with itself unnoticed.
if want_ctx:
    if str(ctx) != str(want_ctx):
        emit("ctx-mismatch", digest=digest, pbytes=pbytes,
             note=f"record ctx={ctx}, this leg is ctx {want_ctx}")
elif pbytes and str(ctx) != pbytes:
    emit("ctx-mismatch", digest=digest, pbytes=pbytes,
         note=f"record ctx={ctx} differs from its own prompt_bytes {pbytes}")

# A record with no digest is its own failure, never the same message as a wrong digest.
if not digest:
    note = ("the record carries no prompt_sha256"
            + (f"; cannot verify {want}" if want else ""))
    emit("no-record-digest", digest="<none>", pbytes=pbytes, note=note)
if want and digest != want:
    emit("sha-mismatch", digest=digest, pbytes=pbytes,
         note=f"record {digest} != expected {want}")

reqs = rec.get("requests") or [{}]
req = reqs[0] if isinstance(reqs[0], dict) else {}
start = req.get("elog_start", rec.get("elog_start"))
end = req.get("elog_end", rec.get("elog_end"))
elog = rec.get("elog") or ""
try:
    with open(elog, "rb") as f:
        f.seek(int(start))
        blob = f.read(max(0, int(end) - int(start)))
except (OSError, TypeError, ValueError) as error:
    emit("unreadable", digest=digest, pbytes=pbytes,
         note=f"{elog or '<no elog path>'}: {error}")
blob = blob.replace(b"\0", b"").decode("utf-8", "replace")
# finditer, not findall: the judge line is the match's group(0) and its reused count
# is group(1); findall would return only the counts.
matches = list(REC.finditer(blob))
if not matches:
    emit("no-judge", digest=digest, pbytes=pbytes,
         note="no 'prompt N tokens = ... reused + ... read in' line in the request-0 slice")
if len(matches) > 1:
    print(f"warning: {len(matches)} judge lines in the request-0 slice of {path};"
          f" using the first (request 1's line is cached by design and is never a rate)",
          file=sys.stderr)
judge = matches[0].group(0).strip()
reused = matches[0].group(1)
emit("cold" if reused == "0" else "warm", reused=reused, digest=digest, judge=judge,
     pbytes=pbytes)
PY
  ); then
    STATUS=unreadable REUSED='' DIGEST='' JUDGE='' PBYTES=''
    NOTE="the judge helper itself failed; its stderr above says why"
    return 0
  fi
  IFS=$'\x1f' read -r STATUS REUSED DIGEST JUDGE NOTE PBYTES <<< "$out"
}

# judge_exit <status> - the exit code a fatal judge status aborts with, kept apart so a
# bad digest is never read as an unreadable record.
judge_exit() {
  case "$1" in
    unreadable|stale|no-judge) echo 2 ;;
    sha-mismatch) echo 3 ;;
    no-record-digest) echo 4 ;;
    ctx-mismatch) echo 5 ;;
    *) echo 2 ;;
  esac
}

args=("$@")
i=0
while [ "$i" -lt "${#args[@]}" ]; do
  arg=${args[$i]}
  case "$arg" in
    --dry-run) DRY=1 ;;
    --check-leg)
      i=$((i+1))
      CHECK_LEG=${args[$i]:-}
      if [ -z "$CHECK_LEG" ]; then say '--check-leg needs a record path'; exit 2; fi
      next=${args[$((i+1))]:-}
      # An optional 64-hex digest follows the record; anything else starts a new flag.
      if [ "${#next}" -eq 64 ]; then
        case "$next" in
          *[!0-9a-f]*) : ;;
          *) i=$((i+1)); CHECK_SHA=$next ;;
        esac
      fi
      ;;
    ''|*[!0-9]*)
      say "unexpected argument: $arg (usage: prod_soak.sh [MINUTES] [--dry-run] [--check-leg RECORD [SHA256]])"
      exit 2 ;;
    *) MIN=$arg ;;
  esac
  i=$((i+1))
done

# --check-leg: run the coldness gate on a record from any lane, no engine involved and
# no corpus needed for the verdict.
if [ -n "$CHECK_LEG" ]; then
  if [ ! -r "$CHECK_LEG" ]; then say "check-leg: cannot read $CHECK_LEG"; exit 2; fi
  if [ -z "$CHECK_SHA" ]; then
    say 'check-leg: no expected sha given; the record must still carry its own prompt_sha256'
  fi
  judge_leg "$CHECK_LEG" "$CHECK_SHA" ""
  say "check-leg: record prompt sha256 ${DIGEST:-<none>}${CHECK_SHA:+, expected $CHECK_SHA}"
  case "$STATUS" in
    cold)
      say "  cold: reused=0"
      say "  $JUDGE"
      RC=0
      ;;
    warm)
      say "  warm: reused=$REUSED - not a prefill sample"
      say "  $JUDGE"
      RC=1
      ;;
    *)
      say "  INVALID $STATUS: $NOTE"
      exit "$(judge_exit "$STATUS")"
      ;;
  esac
  if [ -n "$PBYTES" ] && matches=$(find_corpus "$PBYTES" "$DIGEST" 2>/dev/null); then
    printf '%s\n' "$matches" | sed 's/^/  bytes are /'
  elif [ -n "$PBYTES" ]; then
    say "  no corpus file in the soak or frozen roots holds these bytes at $PBYTES bytes"
    same=$(find_corpus "$PBYTES" 2>/dev/null || true)
    if [ -n "$same" ]; then
      printf '%s\n' "$same" | sed 's/^/    same-size corpus, different bytes: /'
    fi
  fi
  exit "$RC"
fi

CTXS=()
for ctx in ${CTXPAIR//,/ }; do
  case "$ctx" in
    ''|*[!0-9]*) say "CTXPAIR entry is not a byte count: $ctx"; exit 2 ;;
  esac
  CTXS+=("$ctx")
done

# leg 1 takes the second ctx of the pair, leg 2 the first, and so on.
ctx_for_leg() { echo "$CTXPAIR" | cut -d, -f$(( ($1 % 2) + 1 )); }

if [ "$DRY" -eq 1 ]; then
  say "dry-run: $MIN min, alternating ctx $CTXPAIR, nothing sent and no corpus written"
  N=0
  for _ in "${CTXS[@]}"; do
    N=$((N+1))
    LEG="soak-n$N"
    CTX=$(ctx_for_leg "$N")
    if ! resolved=$(ensure_leg_corpus "$LEG" "$CTX" 1); then
      say "dry-run: leg $N (ctx $CTX) has no usable corpus"; exit 1
    fi
    CORPUS=${resolved%% *}
    CSHA=${resolved##* }
    if [ -f "$CORPUS" ]; then
      say "  leg $LEG ctx $CTX -> $CORPUS ($(stat -c%s "$CORPUS") bytes, cached)"
    else
      say "  leg $LEG ctx $CTX -> $CORPUS ($CTX bytes, to be generated)"
    fi
    say "    sha256 $CSHA"
    printf '%s %s --arm prod --leg %s --kind tg --ctx %s --prompt-file %s --prompt-sha256 %s --max-tokens 1024 --out %s --elog %s --hlog %s --url http://127.0.0.1:8009/v1/chat/completions --engine-pid <prod-pid>\n' \
      "$PY" bench/ab-v0210/rpcprof/ab9_driver.py "$LEG" "$CTX" "$CORPUS" "$CSHA" \
      "$OUTR" "$ELOG" "$HLOG"
  done
  say "dry-run: legs 3,4,... follow the same alternation, each with its own head tag"
  say "dry-run: (work soak-nN) and its own corpus file soak-nN.ctxCTX.bin - no two legs share bytes;"
  say "dry-run: the loop above repeats until the deadline; nothing sent and nothing written"
  exit 0
fi

mkdir -p "$OUTR" "$SOAK_CORPUS_DIR"

EP=$(./bench/fault-va/engine_pid.sh 2>/dev/null || true)
if [ -z "$EP" ]; then say 'no prod engine pid: is prod serving?'; exit 1; fi
say "prod engine $EP ($(readlink /proc/"$EP"/exe))"
say "soak: $MIN min, alternating ctx $CTXPAIR, thinking on (prod shape)"

DEADLINE=$(( $(date +%s) + MIN * 60 ))
N=0
COLD=0
WARM=0
WARM_RUN=0
FAULTS=0
ABORT=""
ABORT_RC=0
WARM_LEGS=()
LEG_LINES=()
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  N=$((N+1))
  CTX=$(ctx_for_leg "$N")
  LEG="soak-n$N"
  if ! resolved=$(ensure_leg_corpus "$LEG" "$CTX"); then
    say "corpus for $LEG (ctx $CTX) could not be generated"; exit 1
  fi
  CORPUS=${resolved%% *}
  CSHA=${resolved##* }
  REC="$OUTR/prod-$LEG.json"
  LEG_START=$(date +%s)
  say "request $N (ctx $CTX) corpus $CORPUS sha256 $CSHA"
  # --prompt-file + --prompt-sha256 + a matching --ctx: the head-tagged bytes are the
  # only thing that makes this a cold prefill, and the per-leg work name is what keeps
  # this leg's tag off every other leg's prefix.
  DRV_OUT=$($PY bench/ab-v0210/rpcprof/ab9_driver.py --arm prod --leg "$LEG" --kind tg --ctx "$CTX" \
    --prompt-file "$CORPUS" --prompt-sha256 "$CSHA" \
    --max-tokens 1024 --out "$OUTR" --elog "$ELOG" --hlog "$HLOG" \
    --url http://127.0.0.1:8009/v1/chat/completions --engine-pid "$EP" 2>&1)
  DRV_RC=$?
  printf '%s\n' "$DRV_OUT" | tail -1
  if [ "$DRV_RC" -ne 0 ]; then
    say "  driver exit $DRV_RC; the record, if any, is judged below"
  fi
  judge_leg "$REC" "$CSHA" "$CTX" "$LEG_START"
  case "$STATUS" in
    cold)
      COLD=$((COLD+1))
      WARM_RUN=0
      say "  $LEG [reused=0 COLD] $JUDGE"
      LEG_LINES+=("$(printf '  %-9s ctx %-7s [reused=%s COLD] %s' "$LEG" "$CTX" "$REUSED" "$JUDGE")")
      ;;
    warm)
      WARM=$((WARM+1))
      WARM_RUN=$((WARM_RUN+1))
      WARM_LEGS+=("$LEG")
      say "  $LEG [reused=$REUSED WARM] $JUDGE"
      say "  $LEG warm, not a prefill sample (its rate is not a prefill rate)"
      LEG_LINES+=("$(printf '  %-9s ctx %-7s [reused=%s WARM, not a prefill sample] %s' "$LEG" "$CTX" "$REUSED" "$JUDGE")")
      if [ "$N" -eq 1 ]; then
        ABORT="the FIRST leg is warm: a fresh per-leg corpus must prefill cold, so this is a harness fault"
        ABORT_RC=1
        break
      fi
      if [ "$WARM_RUN" -ge 3 ]; then
        ABORT="$WARM_RUN consecutive warm legs ($(printf '%s ' "${WARM_LEGS[@]: -$WARM_RUN}")): harness fault"
        ABORT_RC=1
        break
      fi
      ;;
    *)
      say "  $LEG INVALID $STATUS: $NOTE"
      say "    record prompt sha256 ${DIGEST:-<none>}${CSHA:+, expected $CSHA}"
      ABORT="leg $LEG record is $STATUS"
      ABORT_RC=$(judge_exit "$STATUS")
      break
      ;;
  esac
  F=$(journalctl -k --since '-3 min' --no-pager 2>/dev/null | grep -cE 'no-retry page fault|GPU reset begin')
  FAULTS=$((FAULTS+F))
  say "  faults/resets in last 3 min: $F"
  if ! kill -0 "$EP" 2>/dev/null; then say '  ENGINE GONE - stopping'; break; fi
done

say "judge lines (request 0 of each leg; request 1 is cached by design and never shown):"
if [ "${#LEG_LINES[@]}" -gt 0 ]; then
  printf '%s\n' "${LEG_LINES[@]}"
fi
if [ "${#WARM_LEGS[@]}" -gt 0 ]; then
  say "warm legs (not prefill samples): ${WARM_LEGS[*]}"
fi
say "soak done: $N requests, $COLD cold, $WARM warm, $FAULTS page faults/resets"
if [ -n "$ABORT" ]; then say "soak ABORTED: $ABORT"; exit "$ABORT_RC"; fi
exit 0
