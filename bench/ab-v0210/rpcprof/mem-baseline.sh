#!/usr/bin/env bash
# Stage-2 memory baseline: READ-ONLY snapshot of the live prod stack.
# Touches nothing: no signals, no restarts, no GPU control, no writes outside OUT.
# Usage: bench/ab-v0210/rpcprof/mem-baseline.sh [label]
set -u
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)"
LABEL="${1:-baseline}"
OUT_DIR="$ROOT/bench/ab-v0210/rpcprof/out"
mkdir -p "$OUT_DIR"
OUT="$OUT_DIR/mem-${LABEL}-$(date +%Y%m%d-%H%M%S).txt"
log(){ printf '%s\n' "$*" >> "$OUT"; }
sec(){ log ""; log "== $* =="; }

log "mem-baseline label=$LABEL ts=$(date '+%F %T')"
log "host=$(uname -r) cwd=$ROOT"
git -C "$ROOT" rev-parse --abbrev-ref HEAD >> "$OUT" 2>&1
git -C "$ROOT" log --oneline -1 >> "$OUT" 2>&1

sec "meminfo"
free -m >> "$OUT" 2>&1
grep -E "^(MemTotal|MemFree|MemAvailable|Buffers|Cached|SwapTotal|SwapFree|Dirty|AnonPages|Mapped|Shmem|Hugepages_total|Hugepages_free|Committed_AS)" /proc/meminfo >> "$OUT" 2>&1
swapon --show >> "$OUT" 2>&1

sec "ksm"
for f in run pages_to_scan pages_sharing pages_shared general_profit max_page_sharing; do
  log "ksm.$f=$(cat /sys/kernel/mm/ksm/$f 2>/dev/null || echo n/a)"
done
log "ksmd_cpu=$(ps -eo pid,pcpu,comm 2>/dev/null | awk '/ksmd/{print $2}')"

sec "processes (RSS/ANON/FILE/SWAP, GiB)"
for pat in "rpcw-hip/strata --serve" "strata-expert-helper" "strata-vision" "serve/server.py --engine strata"; do
  for pid in $(pgrep -f "$pat" 2>/dev/null); do
    [ -r "/proc/$pid/status" ] || continue
    r=$(awk '/^VmRSS:/{printf "%.2f",$2/1048576}' "/proc/$pid/status")
    s=$(awk '/^VmSwap:/{printf "%.2f",$2/1048576}' "/proc/$pid/status")
    a=$(awk '/^RssAnon:/{printf "%.2f",$2/1048576}' "/proc/$pid/status")
    f=$(awk '/^RssFile:/{printf "%.2f",$2/1048576}' "/proc/$pid/status")
    e=$(awk '/^VmEligible:/{printf "%.2f",$2/1048576}' "/proc/$pid/status")
    m=$(awk '/^VmPin:/{printf "%.2f",$2/1048576}' "/proc/$pid/status")
    k=$(awk '/^ksm_rss/{print}' "/proc/$pid/ksm_stat" 2>/dev/null)
    log "pid=$pid rss=${r}G anon=${a}G file=${f}G swap=${s}G pin=${m}G ksm=${k:-n/a} cmd=$(tr '\0' ' ' < "/proc/$pid/cmdline" | cut -c1-60)"
    if [ -r "/proc/$pid/smaps_rollup" ]; then
      awk '/^Pss:|^Pss_Anon:|^Pss_File:|^Pss_Dirty:/{printf "%s=%.2fG ", $1, $2/1048576}' "/proc/$pid/smaps_rollup" >> "$OUT"
      log ""
    fi
  done
done

sec "engine memory lines (newest engine log)"
for L in $(ls -t "$ROOT"/logs/engine-swift15-f16kv*.log 2>/dev/null | head -1); do
  log "log=$L"
  grep -h -iE "ram-relief|fill report|registered what stayed|reserve only|arena read|slots|GiB VRAM|VRAM free|below 512 MiB|pages given back|MAP_HUGETLB|transparent h" "$L" 2>/dev/null | tail -20 | cut -c1-200 >> "$OUT"
done

sec "gpu vram"
rocm-smi --showmeminfo vram 2>/dev/null | head -20 >> "$OUT" 2>&1
nvidia-smi --query-gpu=index,memory.used,memory.total,utilization.gpu --format=csv 2>/dev/null >> "$OUT" 2>&1
nvidia-smi --query-gpu=memory.free --format=csv,noheader 2>/dev/null | sed 's/^/3080 free MiB: /' >> "$OUT" 2>&1

sec "fault + paging rate (vmstat 1 5)"
vmstat 1 5 >> "$OUT" 2>&1
grep -E "^(pgmajfault|pswpin|pswpout)" /proc/vmstat >> "$OUT" 2>&1

sec "file cache pressure"
grep -E "^(Cached|SwapCached|Dirty|Writeback)" /proc/meminfo >> "$OUT" 2>&1
cat /proc/sys/vm/watermark_scale_ratio /proc/sys/vm/min_free_kbytes 2>/dev/null | sed 's/^/vm./' >> "$OUT" 2>&1

sec "prod liveness proof (read-only)"
log "engine_pids=$(pgrep -f 'rpcw-hip/strata --serve' | tr '\n' ' ')"
log "health=$(curl -sf -m 3 http://127.0.0.1:8009/health 2>/dev/null | cut -c1-120)"

echo "wrote $OUT"
cat "$OUT"
