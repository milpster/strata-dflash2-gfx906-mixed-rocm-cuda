#!/usr/bin/env bash
# start-samplers.sh — start the memory and per-card VRAM samplers detached so they
# survive this shell (the tool's inline `&` does not). Writes pids to samplers.pid.
# Usage: ./start-samplers.sh   (from the evidence dir)
set -euo pipefail
cd "$(dirname "$0")"
RPC=/mnt/srcds/Strata/bench/ab-v0210/rpcprof
: > samplers.pid
setsid "$RPC/trial-managed-sampler.sh" "$PWD/mem-230k.csv" 10 > sampler-230k.log 2>&1 < /dev/null &
echo "mem_sampler_pid=$!" >> samplers.pid
setsid ./vram-sample.sh "$PWD/vram-230k.csv" 10 > vramsampler-230k.log 2>&1 < /dev/null &
echo "vram_sampler_pid=$!" >> samplers.pid
sleep 12
cat samplers.pid
tail -2 mem-230k.csv
tail -2 vram-230k.csv
