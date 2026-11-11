#!/usr/bin/env bash
# run-iq3xxs-occ.sh — max GPU occupancy sweep for iq3xxs (v0220, split-34, 256/256
# unless swept). Fixed protocol per arm (operator 18:4x): 250k infill (ctx 1365950,
# max 8) THEN tg1024 lane x2 (ctx 2048, max 1024) on the same boot — decode measured
# under the loaded tier. 3080 = 8192 MiB total: 7400/300 uses 7656 (93.4%). Ladder
# pushes: helper reserve 300->0 (~+190 slots), fill 7400->7600, engine reserves
# 256/256 -> 128/128 -> 64/64. Boot guards skip dead arms; slot counts + judge +
# #2065 recorded per arm. killer-free; verdict informational.
set -uo pipefail
cd /mnt/srcds/Strata
RPC=/mnt/srcds/Strata/bench/ab-v0210/rpcprof
PY=/mnt/srcds/Strata/.venv/bin/python
OUT=$RPC/out/iq3xxs-occ-20261111
mkdir -p "$OUT"

teardown() {
  local sup
  sup=$(jq -r '.supervisor_pid' "$1" 2>/dev/null) || return 0
  [ "$sup" = "null" ] && return 0
  kill -TERM "$sup" 2>/dev/null || true
  timeout 90 tail --pid="$sup" -f /dev/null || true
  rm -f run/trial-spill40.sock run/trial-spill40-2.sock \
        run/prod-swift15-f16kv-mixed.sock run/prod-swift15-f16kv-mixed2.sock
}
prod_up() {
  setsid nohup ./launch-prod-swift15-f16kv-dflash-w4-q4_0-mixed.sh \
    > logs/prod-post-occ-20261111.log 2>&1 < /dev/null &
  for _ in $(seq 1 60); do
    curl -s --max-time 3 http://127.0.0.1:8009/health \
      | jq -e '.status=="ok" and .loaded==true' >/dev/null 2>&1 && return 0
    sleep 10
  done
  return 1
}
mkconf() {  # TAG FILL RESERVE ENG_RESERVE ENG_RESERVE_LATER -> $OUT/conf-$TAG.json
  jq --arg exe /mnt/srcds/Strata/v0220/build-mixed-gpu/rpcw-hip/strata \
     '(.args | index("--vram-reserve-mib")) as $i | .args[$i+1] = $ARGS.named["res"] |
      (.args | index("--vram-reserve-later-mib")) as $j | .args[$j+1] = $ARGS.named["res2"] |
      .exe = $exe' \
     --arg res "$3" --arg res2 "$4" "$RPC/trial-s34-h7400.json" > "$OUT/conf-$1.json"
}

arm() {  # TAG FILL RESERVE ENG_RES ENG_RES2
  local tag=$1 fill=$2 hres=$3 eng1=$4 eng2=$5
  teardown run/trial-spill40-supervisor.json
  mkconf "$tag" "$fill" "$hres" "$eng1" "$eng2"
  echo "occ: booting $tag (fill $fill / helper-res $hres / engine $eng1/$eng2) $(date +%H:%M:%S)"
  TRIAL_CONFIG="$OUT/conf-$tag.json" STRATA_BIN=/mnt/srcds/Strata/v0220/build-mixed-gpu \
  STRATA_FILL_MIB=$fill STRATA_HELPER_SLOTS=5000 STRATA_HELPER_RESERVE=$hres STRATA_PORT=8019 \
    setsid nohup "$RPC/launch-trial-spill40.sh" > "logs/occ-$tag-20261111.log" 2>&1 < /dev/null &
  for _ in $(seq 1 60); do
    curl -s --max-time 3 -o /dev/null http://127.0.0.1:8019/v1/chat/completions && break
    sleep 10
  done
  if ! curl -s --max-time 5 http://127.0.0.1:8019/health | jq -e '.status=="ok" and .loaded==true' >/dev/null 2>&1; then
    echo "occ: $tag BOOT FAILED (occupancy wall?)"
    teardown run/trial-spill40-supervisor.json
    return 1
  fi
  export AB9_URL=http://127.0.0.1:8019/v1/chat/completions
  local ELOG HLOG
  ELOG=$(jq -r '.engine_stderr_log' run/trial-spill40-supervisor.json)
  HLOG=$(jq -r '.helper_log' run/trial-spill40-supervisor.json)
  local boot=$(grep -an 'engine started' "$ELOG" | tail -1 | cut -d: -f1)
  echo "occ: $tag up $(date +%H:%M:%S) — slots: $(sed -n "${boot},\$p" "$ELOG" | grep -a 'expert-helper:' | head -1 | grep -oE 'mirrored [0-9]+/[0-9]+' || echo '?') | VII free: $(sed -n "${boot},\$p" "$ELOG" | grep -a 'of VRAM free with everything' | head -1 | grep -oE '^[^:]*: [0-9]+ MiB' || echo '?')"
  # leg 1: 250k infill (the tier loader)
  "$PY" "$RPC/ab9_driver.py" --arm "$tag" --leg infill250k --kind prefill --ctx 1365950 --max-tokens 8 \
    --out "$OUT" --elog "$ELOG" --hlog "$HLOG" --url "$AB9_URL" || echo "occ: $tag infill250k FAILED/TIMED-OUT"
  # leg 2: tg1024 lane under the loaded tier (fresh + rerun)
  for seq in 1 2; do
    "$PY" "$RPC/ab9_driver.py" --arm "$tag" --leg "tg1024-$seq" --kind tg --ctx 2048 --max-tokens 1024 \
      --out "$OUT" --elog "$ELOG" --hlog "$HLOG" --url "$AB9_URL" || echo "occ: $tag tg1024-$seq FAILED"
  done
  boot=$(grep -an 'engine started' "$ELOG" | tail -1 | cut -d: -f1)
  echo "occ: $tag judge (infill + tg1024 under load):"
  sed -n "${boot},\$p" "$ELOG" | grep -a 'strata serve: prompt' | tail -4 | cut -c1-150 || true
  echo "occ: $tag #2065 total: $(sed -n "${boot},\$p" "$ELOG" | grep -acE 'WARNING|#2065' || true)"
  echo "occ: $tag helper socket traffic (last tg): $(sed -n "${boot},\$p" "$ELOG" | grep -a 'helper=reqs' | tail -1 | grep -oE 'helper=reqs=[0-9]+ rows=[0-9]+' || true)"
  teardown run/trial-spill40-supervisor.json
}

echo "occ sweep: start $(date +%H:%M:%S) — tearing down prod (iq3xxs v0220)"
teardown run/prod-swift15-f16kv-mixed-supervisor.json

arm A-ref-7400-300-256   7400 300 256 256
arm B-7400-0-256         7400 0   256 256
arm C-7600-0-256         7600 0   256 256
arm D-7600-0-128         7600 0   128 128
arm E-7600-0-64          7600 0   64  64

echo "occ sweep: ranking (tg1024-1 wall_s):"
for f in "$OUT"/*-tg1024-1.json; do
  echo "  $(basename "$f" -tg1024-1.json): $(jq -r '.wall_s' "$f" 2>/dev/null || echo missing)"
done

echo "occ sweep: teardown; booting prod $(date +%H:%M:%S)"
teardown run/trial-spill40-supervisor.json
prod_up && echo "occ sweep: DONE $(date +%H:%M:%S) — prod back on v0220" \
             || echo 'occ sweep: PROD BOOT FAILED — manual action'
