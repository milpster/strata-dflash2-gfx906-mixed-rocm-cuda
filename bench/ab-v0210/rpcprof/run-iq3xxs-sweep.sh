#!/usr/bin/env bash
# run-iq3xxs-sweep.sh — pick the fastest prod tuple for iq3xxs (pack iq3xxs-15).
# Fixed: split-34, reserves 256/256. Swept: binary {v0216,v0219,v0220} x helper class
# {7400/5000/300 prod, 6796/4600/512 trial}. Stage 1: 3 binary arms @ h7400, full leg
# set each (warm, 8k x3, 80k, longtg+tg1024, killer144k). Stage 2: helper A/B — winner
# binary @ h6796. Stage 3: killer250k on the overall winner (longtg wall_s ranking).
set -uo pipefail   # no -e: a failed leg/arm must not skip teardown/prod return
cd /mnt/srcds/Strata
RPC=/mnt/srcds/Strata/bench/ab-v0210/rpcprof
PY=/mnt/srcds/Strata/.venv/bin/python
OUT=$RPC/out/iq3xxs-sweep-20261111
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
    > logs/prod-post-sweep-20261111.log 2>&1 < /dev/null &
  for _ in $(seq 1 60); do
    curl -s --max-time 3 http://127.0.0.1:8009/health \
      | jq -e '.status=="ok" and .loaded==true' >/dev/null 2>&1 && return 0
    sleep 10
  done
  return 1
}

# boot_arm TAG TREE FILL SLOTS RESERVE — config class fixed by the tag suffix in $CONF
boot_arm() {
  local tag=$1 tree=$2 fill=$3 slots=$4 reserve=$5 conf=$6
  teardown run/trial-spill40-supervisor.json
  jq --arg exe "$tree/rpcw-hip/strata" '.exe = $exe' "$conf" > "$OUT/conf-$tag.json"
  echo "sweep: booting $tag (exe ${tree##*Strata/}) $(date +%H:%M:%S)"
  TRIAL_CONFIG="$OUT/conf-$tag.json" STRATA_BIN="$tree" \
  STRATA_FILL_MIB=$fill STRATA_HELPER_SLOTS=$slots STRATA_HELPER_RESERVE=$reserve STRATA_PORT=8019 \
    setsid nohup "$RPC/launch-trial-spill40.sh" > "logs/sweep-$tag-20261111.log" 2>&1 < /dev/null &
  for _ in $(seq 1 60); do
    curl -s --max-time 3 -o /dev/null http://127.0.0.1:8019/v1/chat/completions && break
    sleep 10
  done
  curl -s --max-time 5 http://127.0.0.1:8019/health | jq -e '.status=="ok" and .loaded==true' >/dev/null 2>&1
}

legset() {
  local tag=$1
  export AB9_URL=http://127.0.0.1:8019/v1/chat/completions
  local ELOG HLOG
  ELOG=$(jq -r '.engine_stderr_log' run/trial-spill40-supervisor.json)
  HLOG=$(jq -r '.helper_log' run/trial-spill40-supervisor.json)
  echo "sweep: legs for $tag $(date +%H:%M:%S)"
  "$PY" "$RPC/ab9_driver.py" --arm "$tag" --leg warm --kind warm --ctx 512 --out "$OUT" \
    --elog "$ELOG" --hlog "$HLOG" --url "$AB9_URL" || echo "sweep: $tag warm FAILED"
  for seq in 1 2 3; do
    "$PY" "$RPC/ab9_driver.py" --arm "$tag" --leg "8000-$seq" --kind prefill --ctx 8000 --max-tokens 8 \
      --out "$OUT" --elog "$ELOG" --hlog "$HLOG" --url "$AB9_URL" || echo "sweep: $tag 8k-$seq FAILED"
  done
  "$PY" "$RPC/ab9_driver.py" --arm "$tag" --leg 80000-1 --kind prefill --ctx 80000 --max-tokens 8 \
    --out "$OUT" --elog "$ELOG" --hlog "$HLOG" --url "$AB9_URL" || echo "sweep: $tag 80k FAILED"
  "$RPC/run_longtg.sh" "$tag" "$OUT" || echo "sweep: $tag longtg FAILED"
  ELOG=$(jq -r '.engine_stderr_log' run/trial-spill40-supervisor.json)
  HLOG=$(jq -r '.helper_log' run/trial-spill40-supervisor.json)
  "$PY" "$RPC/ab9_driver.py" --arm "$tag" --leg killer144k-tg1024 --kind tg --ctx 786801 --max-tokens 1024 \
    --out "$OUT" --elog "$ELOG" --hlog "$HLOG" --url "$AB9_URL" || echo "sweep: $tag killer144k FAILED/TIMED-OUT"
  local boot
  boot=$(grep -an 'engine started' "$ELOG" | tail -1 | cut -d: -f1)
  echo "sweep: $tag judge lines:"
  sed -n "${boot},\$p" "$ELOG" | grep -a 'strata serve: prompt' | tail -8 | cut -c1-145 || true
  echo "sweep: $tag #2065 count: $(sed -n "${boot},\$p" "$ELOG" | grep -acE 'WARNING|#2065' || true)"
}

arm() {  # tag tree fill slots reserve conf
  local tag=$1 tree=$2 fill=$3 slots=$4 reserve=$5 conf=$6
  if boot_arm "$tag" "$tree" "$fill" "$slots" "$reserve" "$conf"; then
    legset "$tag"
  else
    echo "sweep: $tag BOOT FAILED — skipping legs"
  fi
  teardown run/trial-spill40-supervisor.json
}

longtg_wall() { jq -r '.wall_s' "$OUT/$1-longtg.json" 2>/dev/null || echo 99999; }

echo "sweep: start $(date +%H:%M:%S) — tearing down prod (v0220)"
teardown run/prod-swift15-f16kv-mixed-supervisor.json

# Stage 1: binary 3-way at prod helper class
arm b16-h7400 /mnt/srcds/Strata/v0216/build-mixed-gpu 7400 5000 300 "$RPC/trial-s34-h7400.json"
arm b19-h7400 /mnt/srcds/Strata/v0219/build-mixed-gpu 7400 5000 300 "$RPC/trial-s34-h7400.json"
arm b20-h7400 /mnt/srcds/Strata/v0220/build-mixed-gpu 7400 5000 300 "$RPC/trial-s34-h7400.json"

# Stage 2: helper A/B — winner binary at trial helper class
win_bin=$(printf '%s\n' b16 b19 b20 | while read -r b; do
  echo "$(longtg_wall "$b-h7400") $b"
done | sort -n | head -1 | awk '{print $2}')
echo "sweep: stage-1 binary winner by longtg wall: $win_bin"
case $win_bin in
  b16) arm b16-h6796 /mnt/srcds/Strata/v0216/build-mixed-gpu 6796 4600 512 "$RPC/trial-s34.json" ;;
  b19) arm b19-h6796 /mnt/srcds/Strata/v0219/build-mixed-gpu 6796 4600 512 "$RPC/trial-s34.json" ;;
  b20) arm b20-h6796 /mnt/srcds/Strata/v0220/build-mixed-gpu 6796 4600 512 "$RPC/trial-s34.json" ;;
esac

# Stage 3: killer250k on the overall winner (longtg wall across all arms)
overall=$(for t in b16-h7400 b19-h7400 b20-h7400 "b$win_bin-h6796"; do
  echo "$(longtg_wall "$t") $t"
done | sort -n | head -1 | awk '{print $2}')
echo "sweep: overall winner tuple: $overall"
echo "$overall" > "$OUT/winner.txt"
case $overall in
  b16-h7400) S3TREE=v0216; S3CONF=$RPC/trial-s34-h7400.json; S3FILL=7400; S3SLOTS=5000; S3RES=300 ;;
  b19-h7400) S3TREE=v0219; S3CONF=$RPC/trial-s34-h7400.json; S3FILL=7400; S3SLOTS=5000; S3RES=300 ;;
  b20-h7400) S3TREE=v0220; S3CONF=$RPC/trial-s34-h7400.json; S3FILL=7400; S3SLOTS=5000; S3RES=300 ;;
  b16-h6796) S3TREE=v0216; S3CONF=$RPC/trial-s34.json;       S3FILL=6796; S3SLOTS=4600; S3RES=512 ;;
  b19-h6796) S3TREE=v0219; S3CONF=$RPC/trial-s34.json;       S3FILL=6796; S3SLOTS=4600; S3RES=512 ;;
  b20-h6796) S3TREE=v0220; S3CONF=$RPC/trial-s34.json;       S3FILL=6796; S3SLOTS=4600; S3RES=512 ;;
esac
if boot_arm s3 "/mnt/srcds/Strata/$S3TREE/build-mixed-gpu" "$S3FILL" "$S3SLOTS" "$S3RES" "$S3CONF"; then
  ELOG=$(jq -r '.engine_stderr_log' run/trial-spill40-supervisor.json)
  HLOG=$(jq -r '.helper_log' run/trial-spill40-supervisor.json)
  "$PY" "$RPC/ab9_driver.py" --arm s3 --leg killer250k-tg1024 --kind tg --ctx 1365950 --max-tokens 1024 \
    --out "$OUT" --elog "$ELOG" --hlog "$HLOG" --url http://127.0.0.1:8019/v1/chat/completions \
    || echo 'sweep: killer250k FAILED/TIMED-OUT'
  boot=$(grep -an 'engine started' "$ELOG" | tail -1 | cut -d: -f1)
  echo "sweep: killer250k judge:"
  sed -n "${boot},\$p" "$ELOG" | grep -a 'strata serve: prompt' | tail -2 | cut -c1-145 || true
else
  echo 'sweep: stage-3 boot FAILED'
fi

echo "sweep: final ranking (longtg wall_s, lower = faster):"
for t in b16-h7400 b19-h7400 b20-h7400 "b$win_bin-h6796"; do
  echo "  $t: $(longtg_wall "$t")"
done

echo "sweep: teardown; booting prod v0220 $(date +%H:%M:%S)"
teardown run/trial-spill40-supervisor.json
prod_up && echo "sweep: DONE $(date +%H:%M:%S) — prod back on v0220 (winner: $overall)" \
             || echo 'sweep: PROD BOOT FAILED — manual action'
