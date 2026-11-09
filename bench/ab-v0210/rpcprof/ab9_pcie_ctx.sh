#!/usr/bin/env bash
# ab9 — Stage 1 (PCIe/socket transfer vs prefill gain, arms A/B/C at 8k+80k) + Stage 2
# (memory-lever arms m1..m4) in ONE window. SPEC: SPEC-ab9.md + SPEC-stage2.md.
# BUILD + STATIC VALIDATION ONLY (2026-11-08): this lane never ran it. Prod was live the
# whole prep; this script touches prod ONLY through the window protocol (stop -> manual
# pair -> restore supervised) when a human runs it on an explicit go.
#
# Protocol (copied from ab7_rpc_profile.sh, hardened per SPEC-ab9 §Window protocol):
#   host guard -> gate1 prod healthy -> mem-baseline start -> stop-prod -> VRAM drain ->
#   port-8009 free -> per arm: [manual pair boot (no supervisor), exported profile env] ->
#   warmup -> gate2 -> legs (8k x3, 80k x3, TG1024 sanity; per-leg dmon+vram sidecars,
#   driver-sliced log offsets) -> per-arm pair teardown -> Stage-1 gates 3/4 from the
#   collector -> Stage-2 arms m1..m4 (2x 8k PP + 1x TG512, mem-baseline before/after) ->
#   mem-baseline end -> stop-prod -> supervised prod restore -> health verify.
# EVERY exit path restores prod (trap EXIT) ONCE THE WINDOW IS OPEN: restore_prod() is gated on
# window_open (set only after stop-prod.sh), so a --dry-run or a gate5/gate1 abort never stops
# the live pair. Before the window opens this script is read-only.
#
# Arms (one variable each; prod configs are NEVER edited — per-arm runtime configs are
# generated from the prod config / validated arm-m*.json into out/ab9/run/):
#   A STRATA_PREFILL_HELPER=0            B =1,PIPE=0            C =1,PIPE=1 (prod parity)
#   D = B env on the helper-ipc-diet binaries (arm-d.json exe/_ab9_helper_exe override;
#       run with AB9_ARMS="A D" for the diet validation window)
#   m1 expert-helper-slots 4600->4400    m2 --kv int8           m3 helper fill 7200+reserve 128
#   m4 layer-split 36
#
# Gates (SPEC-ab9; fail fast, documented exit codes):
#   0 ok | 2 gate5 host guard (MemAvail<12GiB or swap used>=12GiB at start)
#   3 gate1 pair unhealthy before window | 4 port 8009 unreleasable
#   5 pair boot/readiness failure | 6 gate2 helper rpcprofile serve lines absent after warmup
#   7 gate3 participation mismatch (collector) | 8 gate4 sha byte-parity mismatch (collector)
#   9 driver/leg failure | 10 prod RESTORE failed (needs human) | 11 collector failure
# Gate 6 (sub-512 MiB VRAM free on a VII during a leg) is a FLAG in the output, not an abort.
#
# Exit-code budget (measured bases, SPEC-ab9 §Window): pair boot 75-180 s, 8k ~20-25 s,
# 80k ~190-200 s, engine restart/arm 2.5-3 min => Stage 1 ~50-60 min, Stage 2 ~25-30 min,
# restore ~5-10 min => ~80-95 min exclusive window.
#
# Usage: ab9_pcie_ctx.sh [--dry-run]
set -u
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)"
export STRATA_PROD_TREE="$ROOT"
D="$ROOT/bench/ab-v0210/rpcprof"
OUT="$D/out/ab9"
RUN="$OUT/run"
LOGS="$D/logs"
TS="$(date +%Y%m%d-%H%M%S)"
source "$D/ab9-prod.env"
PY="$AB9_PY"

log(){ echo "=== ab9 $(date +%T) $* ==="; }
restore_done=0
window_open=0     # 1 only after stop-prod.sh has run: before that the window never opened and
                  # the restore path must touch NOTHING (a dry-run / gate5 / gate1 abort that
                  # stopped and relaunched prod was a live-prod kill, not a restore)
restore_prod(){   # every exit path lands here (trap EXIT); idempotent
  [[ $restore_done == 1 ]] && return 0
  if (( ! window_open )); then
    restore_done=1
    log "window never opened (dry-run / gate abort): prod left exactly as found, no stop, no relaunch"
    return 0
  fi
  restore_done=1
  log "RESTORE: stop + supervised relaunch of prod"
  bash "$ROOT/stop-prod.sh" || true
  nohup bash "$AB9_LAUNCHER" --recover > "$ROOT/logs/ab9-restore-$TS.log" 2>&1 &
  local rp=$!
  echo "supervisor launcher pid $rp (log logs/ab9-restore-$TS.log)"
  # HONEST health: a bare 200 can come from an orphaned arm serve wrapper with a dead
  # engine behind it (runs 1-4: launcher wrote REFUSING, port was held by a leftover
  # wrapper, the poll was fooled and prod sat down silently). Require the model actually
  # loaded AND a live engine process, and fail fast if the launcher refused.
  for i in $(seq 1 40); do
    if grep -q REFUSING "$ROOT/logs/ab9-restore-$TS.log" 2>/dev/null; then
      echo "RESTORE FAILED - launcher REFUSED (port busy?):"; tail -5 "$ROOT/logs/ab9-restore-$TS.log"
      return 10
    fi
    if curl -sf -m 2 "http://127.0.0.1:$AB9_PORT/health" 2>/dev/null | grep -q '"loaded": *true' \
       && pgrep -f "$AB9_ENGINE --serve" >/dev/null; then
      echo "restore verified after ~$((i*15))s: health 200, loaded:true, engine pid $(pgrep -f "$AB9_ENGINE --serve" | head -1)"
      restore_done=2; return 0
    fi
    echo "$(date +%T) restore: not ready yet ($((i*15))s)"
    sleep 15
  done
  echo "RESTORE FAILED - no loaded engine in 10 min; tail:"; tail -15 "$ROOT/logs/ab9-restore-$TS.log"
  return 10
}
on_exit(){ rc=$?; trap - EXIT; restore_prod; rc2=$?; [[ $rc2 != 0 ]] && rc=$rc2; exit $rc; }
trap on_exit EXIT
trap 'exit 130' INT TERM

# ---------- dry-run: print the whole plan, touch NOTHING, exit 0 ----------
if [[ ${1:-} == --dry-run ]]; then
  echo "ab9 dry-run plan (nothing executed, nothing written)"
  echo "prod: launcher=$AB9_LAUNCHER config=$AB9_CONFIG port=$AB9_PORT work=$AB9_WORK"
  echo "arms A/B/C: runtime cfg = live prod config + env override only (ab9_mkconfig.py -> $RUN/<arm>.json)"
  echo "arms m1..m4: runtime cfg = validated arm-<arm>.json (validate-arms.py gate) + harness log/profile env"
  echo
  echo "GATES (fail fast, before the drive; codes in header):"
  echo "  gate5 host guard   MemAvail >=12 GiB and swap used <12 GiB at window start -> exit 2"
  echo "  gate1 pair healthy /health 200 + engine/helper pids alive + both sockets present -> exit 3"
  echo "  gate2 profile lines 'rpcprofile: serve tier=p' in the helper log after warmup -> exit 6"
  echo "  gate3 participation identity: equal DBG helper-row counts per ctx across B and C (collector) -> exit 7"
  echo "  gate4 byte parity: 8k temp0 thinking-off FIRST leg sha256 equal across A/B/C (collector) -> exit 8"
  echo "  gate6 sub-512 MiB free VRAM on a VII during a leg: FLAG in the output, not an abort"
  echo
  echo "mem-baseline: mem-baseline.sh ab9-start-\$TS before the window and ab9-end-\$TS after the last"
  echo "  arm, plus before/after every Stage-2 arm (the gate evidence) -> $LOGS/ab9-mem*.log + $D/out/"
  echo
  echo "WINDOW: gate5 -> gate1 -> mem-baseline start -> stop-prod -> VII drain <2 GiB -> port $AB9_PORT"
  echo "  released (kill its owner only) -> per arm: manual pair boot (NO supervisor) with"
  echo "  STRATA_RPC_PROFILE=1 STRATA_DBG_PROMPT_HELPER=1 + the arm's STRATA_PREFILL_HELPER* exported ->"
  echo "  warmup(512 B, mt16) -> gate2 -> legs -> collector gates -> teardown + VRAM drain -> next arm"
  echo
  for arm in A B C; do
    echo "STAGE1 arm $arm: env STRATA_PREFILL_HELPER$([[ $arm == A ]] && echo "=0" || echo "=1")$([[ $arm != A ]] && echo " STRATA_PREFILL_HELPER_PIPE=$([[ $arm == B ]] && echo 0 || echo 1)")"
    echo "  legs: warm -> fresh-prefill 8k x3 (unique seeded text, mt8; leg 8000-1 = gate4 parity) ->"
    echo "        fresh-prefill 80k x3 (mt8) -> TG1024 cached rerun x2 (ctx2048, sanity only)"
    echo "  per-leg sampling: nvidia-smi dmon -s $AB9_DMON_FLAGS -d 1 + rocm-smi vram CSV/5 s sidecars;"
    echo "        engine+helper log byte slices recorded by ab9_driver.py (collector reads only the slice)"
    echo "  est: boot 75-180s + warm 25s + 3x22s + 3x195s + tg 50s + teardown/drain 150s = ~13-16 min"
  done
  echo "STAGE1 gates 3+4+6: ab9_collect.py --gates $OUT (exit 7/8 abort, gate6 printed as confounds)"
  for arm in m1 m2 m3 m4; do
    echo "STAGE2 arm $arm: mem-baseline before -> boot (cfg from arm-$arm.json; validate-arms.py PASSed)"
    echo "  -> legs 8k x2 (mt8) + TG512 x1 with the same per-leg sidecars -> mem-baseline after"
    echo "  -> teardown/drain; drift gate PP(arm)/PP(C) in .9..1.1 from the collector; est ~7-8 min/arm"
  done
  echo
  echo "STAGE2 gates: mem-baseline before/after per arm + PP/TG drift .9..1.1 + sha byte-parity"
  echo "  + zero sub-512 MiB VRAM warnings; >=30 min real-traffic soak is an OUTSIDE-window gate"
  echo
  echo "RESTORE (every exit path, trap EXIT, idempotent): stop-prod.sh -> $AB9_LAUNCHER --recover"
  echo "  (pair supervisor, nohup, log $ROOT/logs/ab9-restore-\$TS.log) -> /health poll 40x15 s;"
  echo "  no health in 10 min = exit 10 for human attention. Prod is NOT swapped; it is restored."
  echo
  echo "estimated exclusive window: stage1 ~50-60 min + stage2 ~28-32 min + restore ~5-10 min"
  echo "  => ~80-95 min (bases: pair boot 75-180 s, 8k ~20-25 s, 80k ~190-200 s, restart/arm 2.5-3 min)"
  echo "outputs: $OUT/<arm>-<leg>.json, -dmon.txt, -vram.csv, results.tsv, results.md"
  exit 0
fi

mkdir -p "$OUT" "$RUN" "$LOGS"

# ---------- gate 5: host guard (window start) ----------
avail_kb=$(awk '/^MemAvailable:/{print $2}' /proc/meminfo)
swap_used_kb=$(( $(awk '/^SwapTotal:/{print $2}' /proc/meminfo) - $(awk '/^SwapFree:/{print $2}' /proc/meminfo) ))
log "gate5 host guard: MemAvailable $((avail_kb/1048576)) GiB (need >=12), swap used $((swap_used_kb/1048576)) GiB (need <12)"
(( avail_kb >= 12*1048576 )) || { echo "GATE5 FAIL host guard"; exit 2; }
(( swap_used_kb < 12*1048576 )) || { echo "GATE5 FAIL swap debt"; exit 2; }

# ---------- gate 1: prod pair healthy BEFORE we take it down ----------
# Accepted states: (a) live+healthy pair, or (b) an already-down CLEAN state (no pids,
# no sockets, port free) when the user took prod down deliberately before the window.
PRE_DOWN=0
if curl -sf -m 3 -o /dev/null "http://127.0.0.1:$AB9_PORT/health"; then
  epids=$(pgrep -f "$AB9_ENGINE --serve" || true); hpids=$(pgrep -f "$AB9_HELPER" || true)
  [[ -n $epids && -n $hpids ]] || { echo "GATE1 FAIL engine/helper pids absent"; exit 3; }
  [[ -S $AB9_SOCKET && -S $AB9_SOCKET2 ]] || { echo "GATE1 FAIL sockets absent"; exit 3; }
  log "gate1 PASS: engine $epids helper $hpids sockets present health200"
else
  epids=$(pgrep -f "$AB9_ENGINE --serve" || true); hpids=$(pgrep -f "$AB9_HELPER" || true)
  port_held=$(ss -ltn 2>/dev/null | grep -c ":$AB9_PORT ")
  if [[ -z $epids && -z $hpids && ! -e $AB9_SOCKET && ! -e $AB9_SOCKET2 && $port_held -eq 0 ]]; then
    PRE_DOWN=1
    log "gate1 PASS (prod already down clean: no pids, no sockets, port $AB9_PORT free)"
  else
    echo "GATE1 FAIL: health down but not a clean state (pids='$epids/$hpids' port_held=$port_held)"; exit 3
  fi
fi

bash "$D/mem-baseline.sh" "ab9-start-$TS" > "$LOGS/ab9-memstart-$TS.log" 2>&1 || echo "mem-baseline start failed (non-fatal)"

# ---------- take prod down (window begins) ----------
if (( PRE_DOWN )); then
  log "prod already down (user-ordered): skipping stop-prod; window opens now"
else
  log "stopping prod (pair supervisor first)"
  bash "$ROOT/stop-prod.sh"
fi
window_open=1     # from here every exit path must restore prod supervised
for i in $(seq 1 60); do
  vram_b=$(rocm-smi --showmeminfo vram --csv 2>/dev/null | awk -F, '/^card[01]/{u+=$3} END{print u+0}')
  (( vram_b < 2000000000 )) && break
  echo "$(date +%T) drain: VII used $vram_b bytes"; sleep 5
done
for i in $(seq 1 24); do
  if ss -ltn 2>/dev/null | grep -q ":$AB9_PORT "; then
    for pid in $(ss -ltnp 2>/dev/null | grep ":$AB9_PORT " | grep -oP 'pid=\K[0-9]+' | sort -u); do
      echo "$(date +%T) port $AB9_PORT held by pid $pid - killing"; kill -9 "$pid" 2>/dev/null || true
    done
    sleep 2
  else break; fi
done
if ss -ltn 2>/dev/null | grep -q ":$AB9_PORT "; then echo "PORT $AB9_PORT STILL OCCUPIED"; exit 4; fi
rm -f "$AB9_SOCKET" "$AB9_SOCKET2"   # stale socket files of the stopped pair
log "port $AB9_PORT free; window open"

# ---------- per-arm boot / drive / teardown ----------
PAIR_PIDS=""
kill_pair(){   # our own manual pair only
  # PAIR_PIDS first, then pattern fallback: a missed engine pid must not orphan the pair
  # on the port (first window run: pgrep raced the engine fork, teardown drained 5 min
  # against a live engine holding 34 GB until the final restore reaped it).
  for pid in $PAIR_PIDS; do kill -INT "$pid" 2>/dev/null || true; done
  for pid in $(pgrep -f "$AB9_ENGINE --serve" || true) $(pgrep -f "$AB9_HELPER --pack" || true); do
    kill -INT "$pid" 2>/dev/null || true
  done
  for i in $(seq 1 20); do
    [[ -z $(pgrep -f "$AB9_ENGINE --serve" 2>/dev/null) ]] && [[ -z $(pgrep -f "$AB9_HELPER" 2>/dev/null) ]] && break
    sleep 3
  done
  for pid in $PAIR_PIDS; do kill -TERM "$pid" 2>/dev/null || true; done
  for pid in $(pgrep -f "$AB9_ENGINE --serve" || true) $(pgrep -f "$AB9_HELPER --pack" || true); do
    kill -TERM "$pid" 2>/dev/null || true
  done
  PAIR_PIDS=""
  # the serve wrapper can outlive its engine and keep :$AB9_PORT bound (run 3: arm B's serve
  # died to "port already in use" while arm A's server.py was still shutting down) — the
  # port must be free before the next arm boots.
  for i in $(seq 1 30); do
    ss -ltn 2>/dev/null | grep -q ":$AB9_PORT " || break
    for pid in $(ss -ltnp 2>/dev/null | grep ":$AB9_PORT " | grep -oP 'pid=\K[0-9]+' | sort -u); do
      echo "$(date +%T) kill_pair: port $AB9_PORT still held by pid $pid - TERM then KILL"
      kill -TERM "$pid" 2>/dev/null || true; sleep 3
      kill -9 "$pid" 2>/dev/null || true
    done
    sleep 2
  done
  if ss -ltn 2>/dev/null | grep -q ":$AB9_PORT "; then echo "kill_pair: port $AB9_PORT never released"; return 4; fi
  for i in $(seq 1 60); do
    used=$(rocm-smi --showmeminfo vram --csv 2>/dev/null | awk -F, '/^card[01]/{s+=$3} END{print s+0}')
    (( used < 200000000 )) && break
    echo "$(date +%T) waiting VRAM drain: $used bytes"; sleep 5
  done
}
boot_pair(){   # $1=arm name; exports STRATA_RPC_PROFILE/DBG; writes runtime cfg + helper args
  arm=$1
  ELOG="$LOGS/ab9-$TS-$arm-engine.log"; HLOG="$LOGS/ab9-$TS-$arm-helper.log"
  $PY "$D/ab9_mkconfig.py" --arm "$arm" --out "$RUN/$arm.json" --elog "$ELOG" \
     --helper-args "$RUN/$arm.helper" || { echo "mkconfig failed arm $arm"; exit 5; }
  read -r hslots hfill hreserve < "$RUN/$arm.helper"
  hbin=$(head -1 "$RUN/$arm.helper.bin" 2>/dev/null); hbin=${hbin:-$AB9_HELPER}
  export STRATA_RPC_PROFILE=1 STRATA_DBG_PROMPT_HELPER=1
  log "arm $arm: helper up slots=$hslots fill=$hfill reserve=$hreserve bin=$hbin"
  env -i PATH="$PATH" HOME="$HOME" TERM="${TERM:-dumb}" LD_LIBRARY_PATH="$AB9_ROCM_LIBS" \
      STRATA_RPC_PROFILE=1 "$hbin" \
    --pack /mnt/srcds/ai/iq3xxs-15 --experts ../Swift-1.5-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS.gguf \
    --device 0 --slots "$hslots" --reserve-mib "$hreserve" --timeout "$AB9_HELPER_TIMEOUT" \
    --begin-timeout "$AB9_HELPER_TIMEOUT" --socket "$AB9_SOCKET" --fill-mib "$hfill" \
    --socket2 "$AB9_SOCKET2" >>"$HLOG" 2>&1 </dev/null &
  PAIR_PIDS="$!"; hpid=$!
  for i in $(seq 1 300); do
    kill -0 "$hpid" 2>/dev/null || { echo "arm $arm helper exited; tail:"; tail -5 "$HLOG"; return 5; }
    grep -q 'expert-helper: listening on' "$HLOG" && break
    (( i % 30 == 0 )) && echo "$(date +%T) arm $arm helper booting ($i s)"
    sleep 1
  done
  grep -q 'expert-helper: listening on' "$HLOG" || { echo "arm $arm helper readiness timed out"; return 5; }
  [[ -S "$AB9_SOCKET" && -S "$AB9_SOCKET2" ]] || { echo "arm $arm sockets missing"; return 5; }
  log "arm $arm: serve+engine up on :$AB9_PORT"
  ( cd "$AB9_WORK" && env -u CUDA_VISIBLE_DEVICES -u STRATA_ARENA_MMAP -u GPU_PINNED_MIN_XFER_SIZE \
      -u HSA_USERPTR_FOR_PAGED_MEM -u LD_PRELOAD LD_LIBRARY_PATH="$AB9_ROCM_LIBS" \
      "$PY" "$AB9_SERVE" --engine strata --config "$RUN/$arm.json" --port "$AB9_PORT" \
      >>"$ROOT/logs/ab9-$TS-$arm-serve.log" 2>&1 </dev/null & echo $! ) > "$RUN/$arm.servepid"
  PAIR_PIDS="$PAIR_PIDS $(cat "$RUN/$arm.servepid")"
  ready=0
  for i in $(seq 1 1800); do
    kill -0 "$(cat "$RUN/$arm.servepid")" 2>/dev/null || { echo "arm $arm serve exited; tail:"; tail -10 "$ROOT/logs/ab9-$TS-$arm-serve.log"; return 5; }
    # /health answers 200 at the serve wrapper BEFORE the engine is loaded; legs must not
    # fire mid-load (first window run, arm B: warm went out instantly, 56 s wall, elog absent)
    if curl -sf -m 2 "http://127.0.0.1:$AB9_PORT/health" 2>/dev/null | grep -q '"loaded": *true'; then ready=1; break; fi
    (( i % 30 == 0 )) && echo "$(date +%T) arm $arm model loading ($i s)"
    sleep 1
  done
  (( ready )) || { echo "arm $arm readiness timed out (health loaded:true)"; return 5; }
  # serve is a wrapper: the engine is its child and holds the port + sockets. kill_pair must own
  # it too, or a teardown leaves an orphan on :$AB9_PORT and the next arm cannot boot.
  engine_pid=""
  for i in $(seq 1 120); do
    engine_pid=$(pgrep -f "$AB9_ENGINE --serve" | head -1)
    [[ -n $engine_pid && -s "$ELOG" ]] && break
    sleep 1
  done
  [[ -n $engine_pid && -s "$ELOG" ]] || { echo "arm $arm engine pid or engine log missing after 120 s"; return 5; }
  PAIR_PIDS="$PAIR_PIDS $engine_pid"
  log "arm $arm: serve pid $(cat "$RUN/$arm.servepid") engine pid $engine_pid elog $(stat -c %s "$ELOG")B"
  return 0
}
arm_legs(){   # $1=arm; stage1 legs with sidecars
  arm=$1
  warm_slice="$OUT/$arm-warm.sliceoff"
  hb=$(stat -c %s "$HLOG"); eb=$(stat -c %s "$ELOG")
  $PY "$D/ab9_driver.py" --arm "$arm" --leg warm --kind warm --ctx 512 --out "$OUT" \
      --elog "$ELOG" --hlog "$HLOG" || { echo "leg $arm/warm driver failed"; return 9; }
  he=$(stat -c %s "$HLOG"); ee=$(stat -c %s "$ELOG")
  echo "$hb $he $eb $ee" > "$warm_slice"
  for ctx in 8000 80000; do
    for seq in 1 2 3; do
      sidecar "$arm" "${ctx}-${seq}"
      $PY "$D/ab9_driver.py" --arm "$arm" --leg "${ctx}-${seq}" --kind prefill --ctx "$ctx" --max-tokens 8 \
          --out "$OUT" --elog "$ELOG" --hlog "$HLOG" || { stop_sidecar; echo "leg $arm/${ctx}-${seq} driver failed"; return 9; }
      stop_sidecar
      # gate 2 runs after the FIRST 8k leg: prompt-tier claims need a real prefill window —
      # the 512-B warm never triggers tier=p (that aborted arm B on the first window run).
      if [[ $ctx == 8000 && $seq == 1 ]]; then
        he=$(stat -c %s "$HLOG")
        if [[ $arm != A ]]; then
          n2=$(dd if="$HLOG" bs=1 skip="$hb" count=$((he-hb)) 2>/dev/null | grep -c "rpcprofile: serve tier=p")
          (( n2 > 0 )) || { echo "GATE6 FAIL: no helper serve tier=p lines after the first 8k leg (arm $arm)"; return 6; }
          log "arm $arm gate2 PASS: $n2 serve tier=p lines (warm + first 8k)"
        else
          n=$(dd if="$HLOG" bs=1 skip="$hb" count=$((he-hb)) 2>/dev/null | grep -c 'rpcprofile: serve tier=')
          (( n > 0 )) || { echo "GATE6 FAIL: no helper profile lines at all (arm $arm)"; return 6; }
          log "arm $arm gate2 PASS (helper off arm): $n serve lines (warm + first 8k)"
        fi
      fi
    done
  done
  sidecar "$arm" "tg1024"
  $PY "$D/ab9_driver.py" --arm "$arm" --leg tg1024 --kind tg --ctx 2048 --max-tokens 1024 \
      --out "$OUT" --elog "$ELOG" --hlog "$HLOG" || { stop_sidecar; echo "leg tg failed"; return 9; }
  stop_sidecar
}
sidecar(){   # per-leg sampling: nvidia dmon + rocm vram csv (read-only queries, no GPU work)
  a=$1; l=$2
  nvidia-smi dmon -s "$AB9_DMON_FLAGS" -d 1 > "$OUT/$a-$l-dmon.txt" 2>&1 & DMON_PID=$!
  ( while :; do rocm-smi --showmeminfo vram --csv 2>/dev/null >> "$OUT/$a-$l-vram.csv"; sleep 5; done ) & VRAM_PID=$!
}
stop_sidecar(){ kill "$DMON_PID" "$VRAM_PID" 2>/dev/null || true; wait "$DMON_PID" "$VRAM_PID" 2>/dev/null || true; }

stage2_legs(){   # 2x 8k PP + 1x TG512 drift legs
  arm=$1
  for seq in 1 2; do
    sidecar "$arm" "8000-${seq}"
    $PY "$D/ab9_driver.py" --arm "$arm" --leg "8000-${seq}" --kind prefill --ctx 8000 --max-tokens 8 \
        --out "$OUT" --elog "$ELOG" --hlog "$HLOG" || { stop_sidecar; return 9; }
    stop_sidecar
  done
  sidecar "$arm" tg512
  $PY "$D/ab9_driver.py" --arm "$arm" --leg tg512 --kind tg --ctx 2048 --max-tokens 512 \
      --out "$OUT" --elog "$ELOG" --hlog "$HLOG" || { stop_sidecar; return 9; }
  stop_sidecar
}

# ---------- Stage 1: A, B, C (or the AB9_ARMS subset; "A D" = helper-ipc-diet validation) ----------
ARMS=${AB9_ARMS:-"A B C"}
for arm in $ARMS; do
  log "STAGE1 arm $arm"
  boot_pair "$arm" || { rc=$?; kill_pair; echo "arm $arm boot failed"; exit $rc; }
  arm_legs "$arm" || { rc=$?; kill_pair; echo "arm $arm legs failed"; exit $rc; }
  kill_pair
done
# gates 3+4+6 (participation identity, sha parity, sub-512 MiB flags) from the collector
log "STAGE1 gates 3/4/6 via ab9_collect.py --gates"
$PY "$D/ab9_collect.py" --dir "$OUT" --gates || { rc=$?; echo "collector gates rc=$rc"; exit $rc; }

# ---------- Stage 2: m1..m4 (full protocol only; a focused AB9_ARMS run skips it) ----------
if [[ "$ARMS" == "A B C" ]]; then
for arm in m1 m2 m3 m4; do
  log "STAGE2 arm $arm (mem-baseline before)"
  bash "$D/mem-baseline.sh" "ab9-$arm-before-$TS" > "$LOGS/ab9-mem-$arm-before-$TS.log" 2>&1 || true
  boot_pair "$arm" || { rc=$?; kill_pair; echo "arm $arm boot failed"; exit $rc; }
  stage2_legs "$arm" || { rc=$?; kill_pair; echo "arm $arm legs failed"; exit $rc; }
  kill_pair
  bash "$D/mem-baseline.sh" "ab9-$arm-after-$TS" > "$LOGS/ab9-mem-$arm-after-$TS.log" 2>&1 || true
done
else
  log "stage2 skipped (focused AB9_ARMS='$ARMS')"
fi

log "window legs done; collecting derived table"
$PY "$D/ab9_collect.py" --dir "$OUT" --tsv "$OUT/results.tsv" --md "$OUT/results.md" \
  || { echo "collector failed"; exit 11; }
bash "$D/mem-baseline.sh" "ab9-end-$TS" > "$LOGS/ab9-memend-$TS.log" 2>&1 || true
log "ab9 window complete; restoring prod"
exit 0
