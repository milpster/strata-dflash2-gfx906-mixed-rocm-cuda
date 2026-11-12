#!/usr/bin/env bash
# run-iq3s-sweep-v0222.sh — cap/hot knob sweep on the ROUND-3 binary (v0222,
# arena hot/cold split). Two arms, full panel + memsnap + abort greps each.
# Flags are env-parameterized so round-3's final flag surface (from the
# fixer report) slots in without restructuring:
#   ARM1_FLAGS / ARM2_FLAGS  — extra engine args beyond plain relief
#   ARM1_TAG / ARM2_TAG      — out/ tags (default phase1-rerun / buyback)
# Default arms: phase-1 tuple re-run (cap 20000 hot 15000 — measures round-3
# delta cleanly vs out/iq3s-diet-20261111) + buy-back (cap 24000 hot 18000).
# Window ~50-70 min; prod (iq3xxs diet) auto-returns at end.
set -uo pipefail
cd /mnt/srcds/Strata
RPC=/mnt/srcds/Strata/bench/ab-v0210/rpcprof
PY=/mnt/srcds/Strata/.venv/bin/python
OUT=$RPC/out/iq3s-sweep-v0222-$(date +%Y%m%d)
mkdir -p "$OUT"

BIN_DIR=${V0222_BIN:-/mnt/srcds/Strata/v0222/build-mixed-gpu}
EXE=${V0222_EXE:-$BIN_DIR/rpcw-hip/strata}
ARM1_TAG=${ARM1_TAG:-phase1-rerun}
ARM2_TAG=${ARM2_TAG:-buyback}
# bash arrays via env don't serialize; pass as strings, split on commas.
ARM1_FLAGS=${ARM1_FLAGS:-"--expert-helper-ram-relief-packs,--expert-helper-relief-hot-mib,15000,--expert-helper-relief-arena-cap-mib,20000,--prompt-window-retry"}
ARM2_FLAGS=${ARM2_FLAGS:-"--expert-helper-ram-relief-packs,--expert-helper-relief-hot-mib,18000,--expert-helper-relief-arena-cap-mib,24000,--prompt-window-retry"}

memsnap() {
  echo "sweep-mem $1: $(free -m | awk 'NR==2{printf "avail=%s MiB free=%s", $7, $4}') swap=$(free -m | awk 'NR==3{print $3}') | engine RSS: $(ps -eo rss,args --sort=-rss | grep -a 'strata --serve' | grep -av grep | head -1 | awk '{printf "%.1f GiB", $1/1048576}')"
}
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
    > logs/prod-post-sweep-v0222-$(date +%Y%m%d-%H%M).log 2>&1 < /dev/null &
  for _ in $(seq 1 90); do
    curl -s --max-time 3 http://127.0.0.1:8009/health \
      | jq -e '.status=="ok" and .loaded==true' >/dev/null 2>&1 && return 0
    sleep 10
  done
  return 1
}

arm() {  # TAG FLAGS_CSV
  local tag=$1
  IFS=',' read -r -a flags <<< "$2"
  teardown run/trial-spill40-supervisor.json
  jq --arg exe "$EXE" --argjson extra "$(printf '%s\n' "${flags[@]}" | jq -R . | jq -s .)" \
     '.exe = $exe | .args += $extra' "$RPC/trial-iq3s-s36.json" \
     | jq '.layer_split = "34"' > "$OUT/conf-$tag.json"
  echo "sweep: booting $tag ($(date +%H:%M:%S)) flags: $2"
  TRIAL_CONFIG="$OUT/conf-$tag.json" STRATA_BIN="$BIN_DIR" \
  STRATA_FILL_MIB=7400 STRATA_HELPER_SLOTS=5000 STRATA_HELPER_RESERVE=300 STRATA_PORT=8019 \
    setsid nohup "$RPC/launch-trial-spill40.sh" > "logs/sweep-v0222-$tag-$(date +%Y%m%d).log" 2>&1 < /dev/null &
  for _ in $(seq 1 150); do
    curl -s --max-time 3 -o /dev/null http://127.0.0.1:8019/v1/chat/completions && break
    sleep 10
  done
  if ! curl -s --max-time 5 http://127.0.0.1:8019/health | jq -e '.status=="ok" and .loaded==true' >/dev/null 2>&1; then
    echo "sweep: $tag BOOT FAILED"
    return 1
  fi
  export AB9_URL=http://127.0.0.1:8019/v1/chat/completions
  ELOG=$(jq -r '.engine_stderr_log' run/trial-spill40-supervisor.json)
  HLOG=$(jq -r '.helper_log' run/trial-spill40-supervisor.json)
  boot=$(grep -an 'engine started' "$ELOG" | tail -1 | cut -d: -f1)
  echo "sweep: $tag up $(date +%H:%M:%S); boot lines:"
  sed -n "${boot},\$p" "$ELOG" | tr -d '\0' | grep -aE 'arena cap:|hot/cold|ram-relief: [0-9]+ pairs total|stage drop split' | head -6 | cut -c1-138
  memsnap "$tag-post-boot"
  "$PY" "$RPC/ab9_driver.py" --arm "$tag" --leg warm --kind warm --ctx 512 --out "$OUT" \
    --elog "$ELOG" --hlog "$HLOG" --url "$AB9_URL" || echo "sweep: $tag warm FAILED"
  for seq in 1 2 3; do
    "$PY" "$RPC/ab9_driver.py" --arm "$tag" --leg "8000-$seq" --kind prefill --ctx 8000 --max-tokens 8 \
      --out "$OUT" --elog "$ELOG" --hlog "$HLOG" --url "$AB9_URL" || echo "sweep: $tag 8k-$seq FAILED"
  done
  "$PY" "$RPC/ab9_driver.py" --arm "$tag" --leg 80000-1 --kind prefill --ctx 80000 --max-tokens 8 \
    --out "$OUT" --elog "$ELOG" --hlog "$HLOG" --url "$AB9_URL" || echo "sweep: $tag 80k FAILED"
  memsnap "$tag-post-80k"
  "$RPC/run_longtg.sh" "$tag" "$OUT" || echo "sweep: $tag longtg FAILED"
  ELOG=$(jq -r '.engine_stderr_log' run/trial-spill40-supervisor.json)
  HLOG=$(jq -r '.helper_log' run/trial-spill40-supervisor.json)
  "$PY" "$RPC/ab9_driver.py" --arm "$tag" --leg killer144k-tg1024 --kind tg --ctx 786801 --max-tokens 1024 \
    --out "$OUT" --elog "$ELOG" --hlog "$HLOG" --url "$AB9_URL" || echo "sweep: $tag killer FAILED/TIMED-OUT"
  memsnap "$tag-post-killer"
  boot=$(grep -an 'engine started' "$ELOG" | tail -1 | cut -d: -f1)
  span=$(sed -n "${boot},\$p" "$ELOG" | tr -d '\0')
  echo "sweep: $tag ABORT CHECK: zero-gen = $(echo "$span" | grep -ac '0 generated in 0 ms' || true)"
  echo "sweep: $tag decode rates: $(echo "$span" | grep -a 'strata serve: prompt' | grep -aoE '[0-9]+ generated in [0-9]+ ms \([0-9.]+ tok/s\)' | grep -oE '\([0-9.]+ ' | tr -d '( ' | sort -n | tr '\n' ' ')"
  teardown run/trial-spill40-supervisor.json
}

echo "sweep-v0222: start $(date +%H:%M:%S) — tearing down prod"
teardown run/prod-swift15-f16kv-mixed-supervisor.json
arm "$ARM1_TAG" "$ARM1_FLAGS"
arm "$ARM2_TAG" "$ARM2_FLAGS"
echo "sweep-v0222: ranking (longtg / killer / tg1024):"
for t in "$ARM1_TAG" "$ARM2_TAG"; do
  echo "  $t: $(jq -r '.wall_s' "$OUT/$t-longtg.json" 2>/dev/null || echo -) / $(jq -r '.wall_s' "$OUT/$t-killer144k-tg1024.json" 2>/dev/null || echo -) / $(jq -r '.wall_s' "$OUT/$t-tg1024.json" 2>/dev/null || echo -)"
done
echo "sweep-v0222: booting prod $(date +%H:%M:%S)"
prod_up && echo "sweep-v0222: DONE $(date +%H:%M:%S) — prod back" || echo "sweep-v0222: PROD BOOT FAILED — manual action"
