#!/usr/bin/env bash
# run-iq3s-splits.sh — iq3_s WHOLE PANEL x splits {34,36,38} on v0220 + helper
# 7400/5000/300 (aligned class). Re-answers the split question on the modern
# binary: Nov-10's split sweep was all v0216 (34 stall-contaminated; 36 won; 38
# +2.4% PP / -10% TG). iq3_s boots ~13 min; panel legs ~13 min; 3 arms + prod
# return ~= 85 min. Panel: warm, 8k x3, 80k, longtg+tg1024, killer144k.
set -uo pipefail
cd /mnt/srcds/Strata
RPC=/mnt/srcds/Strata/bench/ab-v0210/rpcprof
PY=/mnt/srcds/Strata/.venv/bin/python
OUT=$RPC/out/iq3s-splits-20261111
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
    > logs/prod-post-iq3s-splits-20261111.log 2>&1 < /dev/null &
  for _ in $(seq 1 60); do
    curl -s --max-time 3 http://127.0.0.1:8009/health \
      | jq -e '.status=="ok" and .loaded==true' >/dev/null 2>&1 && return 0
    sleep 10
  done
  return 1
}

arm() {  # TAG SPLIT
  local tag=$1 split=$2
  teardown run/trial-spill40-supervisor.json
  jq --arg split "$split" '.layer_split = $split | .exe = "/mnt/srcds/Strata/v0220/build-mixed-gpu/rpcw-hip/strata"' \
    "$RPC/trial-iq3s-s36.json" > "$OUT/conf-$tag.json"
  echo "iq3s-splits: booting $tag (split $split, v0220, 7400/5000/300) $(date +%H:%M:%S) — ~13 min fill"
  TRIAL_CONFIG="$OUT/conf-$tag.json" STRATA_BIN=/mnt/srcds/Strata/v0220/build-mixed-gpu \
  STRATA_FILL_MIB=7400 STRATA_HELPER_SLOTS=5000 STRATA_HELPER_RESERVE=300 STRATA_PORT=8019 \
    setsid nohup "$RPC/launch-trial-spill40.sh" > "logs/iq3s-splits-$tag-20261111.log" 2>&1 < /dev/null &
  for _ in $(seq 1 120); do
    curl -s --max-time 3 -o /dev/null http://127.0.0.1:8019/v1/chat/completions && break
    sleep 10
  done
  if ! curl -s --max-time 5 http://127.0.0.1:8019/health | jq -e '.status=="ok" and .loaded==true' >/dev/null 2>&1; then
    echo "iq3s-splits: $tag BOOT FAILED"
    teardown run/trial-spill40-supervisor.json
    return 1
  fi
  export AB9_URL=http://127.0.0.1:8019/v1/chat/completions
  local ELOG HLOG
  ELOG=$(jq -r '.engine_stderr_log' run/trial-spill40-supervisor.json)
  HLOG=$(jq -r '.helper_log' run/trial-spill40-supervisor.json)
  local boot=$(grep -an 'engine started' "$ELOG" | tail -1 | cut -d: -f1)
  echo "iq3s-splits: $tag up $(date +%H:%M:%S) — $(sed -n "${boot},\$p" "$ELOG" | grep -a 'layer split' | head -1 | cut -c1-100) | $(sed -n "${boot},\$p" "$ELOG" | grep -a 'expert-helper:' | head -1 | grep -oE 'mirrored [0-9]+/[0-9]+' || echo 'slots?')"
  "$PY" "$RPC/ab9_driver.py" --arm "$tag" --leg warm --kind warm --ctx 512 --out "$OUT" \
    --elog "$ELOG" --hlog "$HLOG" --url "$AB9_URL" || echo "iq3s-splits: $tag warm FAILED"
  for seq in 1 2 3; do
    "$PY" "$RPC/ab9_driver.py" --arm "$tag" --leg "8000-$seq" --kind prefill --ctx 8000 --max-tokens 8 \
      --out "$OUT" --elog "$ELOG" --hlog "$HLOG" --url "$AB9_URL" || echo "iq3s-splits: $tag 8k-$seq FAILED"
  done
  "$PY" "$RPC/ab9_driver.py" --arm "$tag" --leg 80000-1 --kind prefill --ctx 80000 --max-tokens 8 \
    --out "$OUT" --elog "$ELOG" --hlog "$HLOG" --url "$AB9_URL" || echo "iq3s-splits: $tag 80k FAILED"
  "$RPC/run_longtg.sh" "$tag" "$OUT" || echo "iq3s-splits: $tag longtg FAILED"
  ELOG=$(jq -r '.engine_stderr_log' run/trial-spill40-supervisor.json)
  HLOG=$(jq -r '.helper_log' run/trial-spill40-supervisor.json)
  "$PY" "$RPC/ab9_driver.py" --arm "$tag" --leg killer144k-tg1024 --kind tg --ctx 786801 --max-tokens 1024 \
    --out "$OUT" --elog "$ELOG" --hlog "$HLOG" --url "$AB9_URL" || echo "iq3s-splits: $tag killer144k FAILED/TIMED-OUT"
  boot=$(grep -an 'engine started' "$ELOG" | tail -1 | cut -d: -f1)
  echo "iq3s-splits: $tag judge lines:"
  sed -n "${boot},\$p" "$ELOG" | grep -a 'strata serve: prompt' | tail -8 | cut -c1-145 || true
  echo "iq3s-splits: $tag #2065: $(sed -n "${boot},\$p" "$ELOG" | grep -acE 'WARNING|#2065' || true)"
  teardown run/trial-spill40-supervisor.json
}

echo "iq3s-splits: start $(date +%H:%M:%S) — tearing down prod (iq3xxs v0220)"
teardown run/prod-swift15-f16kv-mixed-supervisor.json

arm s36 36   # champion reference first (matches Nov-10 winner)
arm s34 34   # first clean reading (Nov-10 was stall-contaminated)
arm s38 38   # the PP-favoring alternative

echo "iq3s-splits: ranking (longtg wall_s):"
for t in s36 s34 s38; do
  echo "  $t: $(jq -r '.wall_s' "$OUT/$t-longtg.json" 2>/dev/null || echo missing)"
done
echo "iq3s-splits: ranking (killer144k wall_s):"
for t in s36 s34 s38; do
  echo "  $t: $(jq -r '.wall_s' "$OUT/$t-killer144k-tg1024.json" 2>/dev/null || echo missing)"
done

echo "iq3s-splits: teardown; booting prod (iq3xxs v0220) $(date +%H:%M:%S)"
teardown run/trial-spill40-supervisor.json
prod_up && echo "iq3s-splits: DONE $(date +%H:%M:%S) — prod back on iq3xxs v0220" \
             || echo 'iq3s-splits: PROD BOOT FAILED — manual action'
