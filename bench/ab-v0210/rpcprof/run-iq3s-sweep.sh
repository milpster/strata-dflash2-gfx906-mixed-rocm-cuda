#!/usr/bin/env bash
# run-iq3s-sweep.sh — iq3_s tuple pick: binary A/B at the files' helper class.
# Fixed: split-36 (Nov-10 winner), 256/256, relief, helper 7400/5000/0 (as the
# promoted files encode). Arms: A v0216 vs B v0220 (exe rewrite only). Helper
# class is a documented wash on this pack family; binary is the live question.
# iq3_s boots ~13 min (large pack fill) — 2 arms + prod return ~= 65 min.
# Legs each: warm, 8k x3, 80k, longtg+tg1024, killer144k. Verdict: informational.
set -uo pipefail
cd /mnt/srcds/Strata
RPC=/mnt/srcds/Strata/bench/ab-v0210/rpcprof
PY=/mnt/srcds/Strata/.venv/bin/python
OUT=$RPC/out/iq3s-sweep-20261111
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
    > logs/prod-post-iq3s-sweep-20261111.log 2>&1 < /dev/null &
  for _ in $(seq 1 60); do
    curl -s --max-time 3 http://127.0.0.1:8009/health \
      | jq -e '.status=="ok" and .loaded==true' >/dev/null 2>&1 && return 0
    sleep 10
  done
  return 1
}

arm() {  # TAG TREE
  local tag=$1 tree=$2
  teardown run/trial-spill40-supervisor.json
  jq --arg exe "$tree/rpcw-hip/strata" '.exe = $exe' "$RPC/trial-iq3s-s36.json" > "$OUT/conf-$tag.json"
  echo "iq3s: booting $tag (${tree##*Strata/}) $(date +%H:%M:%S) — iq3_s boots take ~13 min"
  TRIAL_CONFIG="$OUT/conf-$tag.json" STRATA_BIN="$tree" \
  STRATA_FILL_MIB=7400 STRATA_HELPER_SLOTS=5000 STRATA_HELPER_RESERVE=300 STRATA_PORT=8019 \
    setsid nohup "$RPC/launch-trial-spill40.sh" > "logs/iq3s-$tag-20261111.log" 2>&1 < /dev/null &
  for _ in $(seq 1 120); do   # 20 min for the big fill
    curl -s --max-time 3 -o /dev/null http://127.0.0.1:8019/v1/chat/completions && break
    sleep 10
  done
  if ! curl -s --max-time 5 http://127.0.0.1:8019/health | jq -e '.status=="ok" and .loaded==true' >/dev/null 2>&1; then
    echo "iq3s: $tag BOOT FAILED"
    teardown run/trial-spill40-supervisor.json
    return 1
  fi
  echo "iq3s: $tag up $(date +%H:%M:%S) — legs"
  export AB9_URL=http://127.0.0.1:8019/v1/chat/completions
  local ELOG HLOG
  ELOG=$(jq -r '.engine_stderr_log' run/trial-spill40-supervisor.json)
  HLOG=$(jq -r '.helper_log' run/trial-spill40-supervisor.json)
  "$PY" "$RPC/ab9_driver.py" --arm "$tag" --leg warm --kind warm --ctx 512 --out "$OUT" \
    --elog "$ELOG" --hlog "$HLOG" --url "$AB9_URL" || echo "iq3s: $tag warm FAILED"
  for seq in 1 2 3; do
    "$PY" "$RPC/ab9_driver.py" --arm "$tag" --leg "8000-$seq" --kind prefill --ctx 8000 --max-tokens 8 \
      --out "$OUT" --elog "$ELOG" --hlog "$HLOG" --url "$AB9_URL" || echo "iq3s: $tag 8k-$seq FAILED"
  done
  "$PY" "$RPC/ab9_driver.py" --arm "$tag" --leg 80000-1 --kind prefill --ctx 80000 --max-tokens 8 \
    --out "$OUT" --elog "$ELOG" --hlog "$HLOG" --url "$AB9_URL" || echo "iq3s: $tag 80k FAILED"
  "$RPC/run_longtg.sh" "$tag" "$OUT" || echo "iq3s: $tag longtg FAILED"
  ELOG=$(jq -r '.engine_stderr_log' run/trial-spill40-supervisor.json)
  HLOG=$(jq -r '.helper_log' run/trial-spill40-supervisor.json)
  "$PY" "$RPC/ab9_driver.py" --arm "$tag" --leg killer144k-tg1024 --kind tg --ctx 786801 --max-tokens 1024 \
    --out "$OUT" --elog "$ELOG" --hlog "$HLOG" --url "$AB9_URL" || echo "iq3s: $tag killer144k FAILED/TIMED-OUT"
  local boot
  boot=$(grep -an 'engine started' "$ELOG" | tail -1 | cut -d: -f1)
  echo "iq3s: $tag judge lines:"
  sed -n "${boot},\$p" "$ELOG" | grep -a 'strata serve: prompt' | tail -8 | cut -c1-145 || true
  echo "iq3s: $tag #2065: $(sed -n "${boot},\$p" "$ELOG" | grep -acE 'WARNING|#2065' || true)"
  echo "iq3s: $tag helper boot slots:"
  sed -n "${boot},\$p" "$ELOG" | grep -a 'expert-helper:' | head -1 | cut -c1-120 || true
  teardown run/trial-spill40-supervisor.json
}

echo "iq3s sweep: start $(date +%H:%M:%S) — tearing down prod (iq3xxs v0220)"
teardown run/prod-swift15-f16kv-mixed-supervisor.json

arm A16 /mnt/srcds/Strata/v0216/build-mixed-gpu
arm B20 /mnt/srcds/Strata/v0220/build-mixed-gpu

echo "iq3s sweep: ranking (longtg wall_s):"
for t in A16 B20; do
  echo "  $t: $(jq -r '.wall_s' "$OUT/$t-longtg.json" 2>/dev/null || echo missing)"
done

echo "iq3s sweep: teardown; booting prod (iq3xxs v0220) $(date +%H:%M:%S)"
teardown run/trial-spill40-supervisor.json
prod_up && echo "iq3s sweep: DONE $(date +%H:%M:%S) — prod back on iq3xxs v0220" \
             || echo 'iq3s sweep: PROD BOOT FAILED — manual action'
