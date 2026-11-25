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
# checked for a zero reused count before the leg counts.
#
# Corpus roots, in order:
#   1. bench/fault-va/out/corpus      soak-owned; missing ctxs are generated here
#   2. .../port-ab-20261123/corpus    frozen W2 window evidence, read-only fallback
# Generation is legitimate, not a new input: build_paired_prompt() is
# deterministic (fixed seed, no timestamps, no per-run text), so a regenerated
# file at a given ctx is byte-identical to the same ctx measured earlier.
#
# Usage: prod_soak.sh [MINUTES] [--dry-run]
#        prod_soak.sh --check-leg RECORD [SHA256]
#   CTXPAIR=<bytes>,<bytes> selects the context pair (default 80000,16000).
#   --dry-run resolves/generates-in-plan the corpora and prints the planned
#           commands; nothing is sent and no corpus is written.
#   --check-leg runs the cold-prefill gate against a leg record outside a soak:
#           0 = cold (reused=0), 1 = warm or prompt-sha mismatch, 2 = unreadable
#           record or no judge line. SHA256 defaults to no digest check.
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
# Frozen W2 window evidence: read-only fallback only.
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

# find_corpus <ctx> - print an existing corpus whose size AND embedded ctx equal
# <ctx>, soak root first, frozen W2 dir second. First match in glob order wins
# (two files may share a byte size).
find_corpus() {
  local ctx=$1 root f size
  for root in "${CORPUS_ROOTS[@]}"; do
    [ -d "$root" ] || continue
    for f in "$root"/*.bin; do
      [ -e "$f" ] || continue
      size=$(stat -c%s "$f")
      [ "$size" -eq "$ctx" ] || continue
      case "$(head -c 200 "$f" | head -1)" in
        *"ctx=$ctx]"*) printf '%s\n' "$f"; return 0 ;;
      esac
    done
  done
  return 1
}

# gen_corpus <ctx> <dest> <dry 0|1> - print "<bytes> <sha256>".
# build_paired_prompt(work, pair, ctx) is deterministic: fixed seed, no
# timestamps, no per-arm text, exactly ctx bytes with the identity tag at the
# HEAD. Regenerating an ctx therefore reproduces the bytes of any earlier
# measurement of that ctx, which is what makes a generated corpus the same input
# rather than a new one. dry=1 computes the digest and writes nothing.
gen_corpus() {
  $PY - "$2" "$1" "$3" <<'PY'
import hashlib, pathlib, sys
sys.path.insert(0, "bench/ab-v0210/rpcprof")
import champions_prepare
dest, ctx, dry = pathlib.Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3] == "1"
blob = champions_prepare.build_paired_prompt("soak", 0, ctx)
assert len(blob) == ctx, (ctx, len(blob))
if not dry:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(blob)
print(len(blob), hashlib.sha256(blob).hexdigest())
PY
}

# resolve_corpus <ctx> - print "<path> <sha256>" for the corpus of this ctx,
# generating one into the soak-owned root when no existing file matches. The
# driver rejects a prompt whose byte length differs from --ctx, so size and head
# tag are both required.
resolve_corpus() {
  local ctx=$1 path out bytes sha
  if path=$(find_corpus "$ctx"); then
    sha=$(sha256sum "$path" | cut -d' ' -f1)
    printf '%s %s\n' "$path" "$sha"
    return 0
  fi
  path="$SOAK_CORPUS_DIR/soak.ctx$ctx.bin"
  if ! out=$(gen_corpus "$ctx" "$path" "$DRY"); then
    say "corpus generator failed for ctx $ctx: no corpus exists and none could be built" >&2
    say "available corpora:" >&2
    list_corpora >&2
    return 1
  fi
  bytes=${out%% *}
  sha=${out##* }
  if [ "$DRY" -eq 1 ]; then
    say "would generate $path ($bytes bytes, sha256 $sha)" >&2
  else
    say "generated head-tagged corpus $path ($bytes bytes, sha256 $sha)" >&2
  fi
  printf '%s %s\n' "$path" "$sha"
}

# read_reused <record> [expected sha] - print the reused count of the leg's FIRST
# request, or "missing" / "sha-mismatch". The judge line is read from that
# request's own [elog_start, elog_end) slice, because --kind tg sends the same
# prompt twice and request 1 is warm by design. A request's own prefill line is
# the first judge line in its slice; more than one means the slice caught a
# neighbouring line, so the first is used and the count is named on stderr.
read_reused() {
  $PY - "$1" "${2:-}" <<'PY'
import json, re, sys
MISSING, MISMATCH = "missing", "sha-mismatch"
path, want = sys.argv[1], sys.argv[2]
try:
    rec = json.load(open(path))
except (OSError, ValueError):
    print(MISSING)
    raise SystemExit(0)
try:
    req = (rec.get("requests") or [{}])[0]
    start = req.get("elog_start", rec.get("elog_start"))
    end = req.get("elog_end", rec.get("elog_end"))
    with open(rec.get("elog", ""), "rb") as f:
        f.seek(start)
        blob = f.read(max(0, end - start)).replace(b"\0", b"").decode("utf-8", "replace")
except (OSError, KeyError, TypeError, ValueError):
    print(MISSING)
    raise SystemExit(0)
if want and rec.get("prompt_sha256") != want:
    print(MISMATCH)
    raise SystemExit(0)
hits = re.findall(r"prompt \d+ tokens = (\d+) reused \+ \d+ read in", blob)
if not hits:
    print(MISSING)
elif len(hits) > 1:
    print(f"warning: {len(hits)} judge lines in the request-0 slice of {path};"
          f" using the first", file=sys.stderr)
    print(hits[0])
else:
    print(hits[0])
PY
}

# cold_check <leg> <record> [expected sha] - a leg whose reused count is not zero
# is NEVER a prefill measurement: the engine served part of the prompt from the
# conversation cache, so its read time and any rate derived from it describe a
# warm request. 0 = cold, 1 = warm or prompt-sha mismatch, 2 = unreadable/missing.
cold_check() {
  local leg=$1 rec=$2 want_sha=${3:-} reused
  reused=$(read_reused "$rec" "$want_sha")
  case "$reused" in
    sha-mismatch)
      say "  $leg INVALID prompt sha256 in the record is not $want_sha"
      return 1 ;;
    missing)
      say "  $leg INVALID warm reused=missing (no judge line readable in $rec)"
      return 2 ;;
    0)
      say "  $leg cold: reused=0${want_sha:+, prompt sha256 $want_sha}"
      return 0 ;;
    ''|*[!0-9]*)
      say "  $leg INVALID warm reused=$reused"
      return 2 ;;
    *)
      say "  $leg INVALID warm reused=$reused"
      return 1 ;;
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

# --check-leg: run the coldness gate on a record from any lane, no engine involved.
if [ -n "$CHECK_LEG" ]; then
  if [ ! -r "$CHECK_LEG" ]; then say "check-leg: cannot read $CHECK_LEG"; exit 2; fi
  if [ -z "$CHECK_SHA" ]; then
    say 'check-leg: no expected sha given, the prompt digest is not verified'
  fi
  cold_check "$(basename "$CHECK_LEG")" "$CHECK_LEG" "$CHECK_SHA"
  exit $?
fi

CTXS=()
for ctx in ${CTXPAIR//,/ }; do
  case "$ctx" in
    ''|*[!0-9]*) say "CTXPAIR entry is not a byte count: $ctx"; exit 2 ;;
  esac
  CTXS+=("$ctx")
done

# Resolve every requested corpus up front; a ctx that cannot be resolved is a
# harness bug and must surface before the first request goes on the wire.
declare -A CORP_PATH=() CORP_SHA=()
for ctx in "${CTXS[@]}"; do
  resolved=$(resolve_corpus "$ctx") || exit 1
  CORP_PATH[$ctx]=${resolved%% *}
  CORP_SHA[$ctx]=${resolved##* }
done

# leg 1 takes the second ctx of the pair, leg 2 the first, and so on (unchanged).
ctx_for_leg() { echo "$CTXPAIR" | cut -d, -f$(( ($1 % 2) + 1 )); }

if [ "$DRY" -eq 1 ]; then
  say "dry-run: ${MIN} min, alternating ctx $CTXPAIR, nothing sent"
  for ctx in "${CTXS[@]}"; do
    say "  ctx $ctx -> ${CORP_PATH[$ctx]} ($(stat -c%s "${CORP_PATH[$ctx]}" 2>/dev/null || echo planned) bytes)"
    say "    sha256 ${CORP_SHA[$ctx]}"
  done
  N=0
  for _ in "${CTXS[@]}"; do
    N=$((N+1))
    CTX=$(ctx_for_leg "$N")
    printf '%s %s --arm prod --leg soak-n%s --kind tg --ctx %s --prompt-file %s --prompt-sha256 %s --max-tokens 1024 --out %s --elog %s --hlog %s --url http://127.0.0.1:8009/v1/chat/completions --engine-pid <prod-pid>\n' \
      "$PY" bench/ab-v0210/rpcprof/ab9_driver.py "$N" "$CTX" "${CORP_PATH[$CTX]}" "${CORP_SHA[$CTX]}" \
      "$OUTR" "$ELOG" "$HLOG"
  done
  say "dry-run: legs 1..$N repeat in that order until the deadline; no request sent"
  exit 0
fi

mkdir -p "$OUTR"

EP=$(./bench/fault-va/engine_pid.sh 2>/dev/null || true)
if [ -z "$EP" ]; then say 'no prod engine pid: is prod serving?'; exit 1; fi
say "prod engine $EP ($(readlink /proc/"$EP"/exe))"
say "soak: ${MIN} min, alternating ctx $CTXPAIR, thinking on (prod shape)"

DEADLINE=$(( $(date +%s) + MIN * 60 ))
N=0
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  N=$((N+1))
  CTX=$(ctx_for_leg "$N")
  LEG="soak-n$N"
  CORPUS=${CORP_PATH[$CTX]}
  CSHA=${CORP_SHA[$CTX]}
  REC="$OUTR/prod-$LEG.json"
  say "request $N (ctx $CTX) corpus $CORPUS sha256 $CSHA"
  # --prompt-file + --prompt-sha256 + a matching --ctx: the head-tagged bytes are
  # the only thing that makes this a cold prefill. The old call passed no prompt
  # file, so build_prompt()'s tail tag left the prefix cacheable and the leg was
  # warm while being logged as a prefill measurement.
  $PY bench/ab-v0210/rpcprof/ab9_driver.py --arm prod --leg "$LEG" --kind tg --ctx "$CTX" \
    --prompt-file "$CORPUS" --prompt-sha256 "$CSHA" \
    --max-tokens 1024 --out "$OUTR" --elog "$ELOG" --hlog "$HLOG" \
    --url http://127.0.0.1:8009/v1/chat/completions --engine-pid "$EP" 2>&1 | tail -1
  cold_check "$LEG" "$REC" "$CSHA" || exit $?
  F=$(journalctl -k --since '-3 min' --no-pager 2>/dev/null | grep -cE 'no-retry page fault|GPU reset begin')
  say "  faults/resets in last 3 min: $F"
  if ! kill -0 "$EP" 2>/dev/null; then say '  ENGINE GONE - stopping'; break; fi
done
say "soak done: $N requests"
tr -d '\000' < "$ELOG" | grep -aE 'prompt [0-9]+ tokens = ' | tail -8 | cut -c1-150
