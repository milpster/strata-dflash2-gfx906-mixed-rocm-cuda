#!/usr/bin/env bash
# run-occ150.sh — the missing cell of the occupancy grid: helper reserve 150
# (fill 7400, engine 256/256, v0220, split-34). VII free expected ~300 — the edge
# of the observed safety zone. Protocol (operator): 250k infill then tg1024 x2.
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
    > logs/prod-post-occ150-20261111.log 2>&1 < /dev/null &
  for _ in $(seq 1 60); do
    curl -s --max-time 3 http://127.0.0.1:8009/health \
      | jq -e '.status=="ok" and .loaded==true' >/dev/null 2>&1 && return 0
    sleep 10
  done
  return 1
}

echo "occ150: start $(date +%H:%M:%S) — tearing down prod"
teardown run/prod-swift15-f16kv-mixed-supervisor.json
teardown run/trial-spill40-supervisor.json

jq '.exe = "/mnt/srcds/Strata/v0220/build-mixed-gpu/rpcw-hip/strata"' \
  "$RPC/trial-s34-h7400.json" > "$OUT/conf-F-7400-150.json"
echo "occ150: booting F-7400-150 (fill 7400 / helper-res 150 / engine 256/256) $(date +%H:%M:%S)"
TRIAL_CONFIG="$OUT/conf-F-7400-150.json" STRATA_BIN=/mnt/srcds/Strata/v0220/build-mixed-gpu \
STRATA_FILL_MIB=7400 STRATA_HELPER_SLOTS=5000 STRATA_HELPER_RESERVE=150 STRATA_PORT=8019 \
  setsid nohup "$RPC/launch-trial-spill40.sh" > logs/occ-F-7400-150-20261111.log 2>&1 < /dev/null &
for _ in $(seq 1 60); do
  curl -s --max-time 3 -o /dev/null http://127.0.0.1:8019/v1/chat/completions && break
  sleep 10
done
if ! curl -s --max-time 5 http://127.0.0.1:8019/health | jq -e '.status=="ok" and .loaded==true' >/dev/null 2>&1; then
  echo 'occ150: BOOT FAILED'
  teardown run/trial-spill40-supervisor.json
  prod_up && echo 'occ150: prod back up' || echo 'occ150: PROD BOOT FAILED'
  exit 1
fi
export AB9_URL=http://127.0.0.1:8019/v1/chat/completions
ELOG=$(jq -r '.engine_stderr_log' run/trial-spill40-supervisor.json)
HLOG=$(jq -r '.helper_log' run/trial-spill40-supervisor.json)
boot=$(grep -an 'engine started' "$ELOG" | tail -1 | cut -d: -f1)
echo "occ150: up $(date +%H:%M:%S) — VII free: $(sed -n "${boot},\$p" "$ELOG" | grep -a 'of VRAM free with everything' | head -1 | grep -oE '[0-9]+ MiB' | head -1) | slots: $(sed -n "${boot},\$p" "$ELOG" | grep -a 'expert-helper:' | head -1 | grep -oE 'mirrored [0-9]+/[0-9]+' || echo '?')"

"$PY" "$RPC/ab9_driver.py" --arm F-7400-150 --leg infill250k --kind prefill --ctx 1365950 --max-tokens 8 \
  --out "$OUT" --elog "$ELOG" --hlog "$HLOG" --url "$AB9_URL" || echo 'occ150: infill250k FAILED/TIMED-OUT'
for seq in 1 2; do
  "$PY" "$RPC/ab9_driver.py" --arm F-7400-150 --leg "tg1024-$seq" --kind tg --ctx 2048 --max-tokens 1024 \
    --out "$OUT" --elog "$ELOG" --hlog "$HLOG" --url "$AB9_URL" || echo "occ150: tg1024-$seq FAILED"
done
boot=$(grep -an 'engine started' "$ELOG" | tail -1 | cut -d: -f1)
echo 'occ150: judge (infill + tg1024 under load):'
sed -n "${boot},\$p" "$ELOG" | grep -a 'strata serve: prompt' | tail -4 | cut -c1-150 || true
echo "occ150: #2065 total: $(sed -n "${boot},\$p" "$ELOG" | grep -acE 'WARNING|#2065' || true)"
echo "occ150: helper socket traffic (last tg): $(sed -n "${boot},\$p" "$ELOG" | grep -a 'helper=reqs' | tail -1 | grep -oE 'helper=reqs=[0-9]+ rows=[0-9]+' || true)"

echo "occ150: teardown; booting prod $(date +%H:%M:%S)"
teardown run/trial-spill40-supervisor.json
prod_up && echo "occ150: DONE $(date +%H:%M:%S) — prod back on v0220" \
             || echo 'occ150: PROD BOOT FAILED — manual action'
