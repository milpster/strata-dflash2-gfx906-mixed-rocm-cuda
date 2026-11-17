#!/usr/bin/env bash
# warm-band.sh — re-warm the RAM-relief expert band (file-backed, page-cache-served)
# after a reboot, by grinding broad-routing long generations until decode recovers.
# Stops at 2 consecutive requests >= FLOOR tok/s (client-measured), MAX_REQ, or MAX_S.
# Run with NO agents live (operator rule: no prod requests while agents run).
set -u
PORT=${PORT:-8009}
MODEL=${MODEL:-ud-iq3_xxs}
FLOOR=${FLOOR:-20}
MAX_REQ=${MAX_REQ:-36}
MAX_S=${MAX_S:-3300}
LOG=${LOG:-/mnt/srcds/Strata/logs/warm-band-$(date +%Y%m%d-%H%M%S).log}
exec > >(tee -a "$LOG") 2>&1

PROMPTS=(
  "Continue this encyclopedia passage in the same style for several paragraphs: In the early years of the railway boom, the standardization of track gauge"
  "Write a long technical explanation of how instruction pipelining and branch prediction interact in modern CPUs, with concrete examples."
  "Tell a long connected story about a lighthouse keeper who discovers a strange radio signal, in at least twenty paragraphs."
  "Explain in depth, with worked examples, how gradient descent and backpropagation train a neural network."
  "Write several paragraphs of Python code with commentary implementing a small key-value store with LRU eviction."
  "Discuss the economic history of the spice trade across the Indian Ocean in essay form, over many paragraphs."
  "Describe in long detail the process of photosynthesis at the molecular level, then the ecology of coral reefs."
  "Write a lengthy review comparing different approaches to garbage collection in programming language runtimes."
)
echo "warm-band: model=$MODEL floor=${FLOOR} tok/s max_req=$MAX_REQ max_s=${MAX_S}s log=$LOG"
T0=$(date +%s)
streak=0
for i in $(seq 1 "$MAX_REQ"); do
  elapsed=$(( $(date +%s) - T0 ))
  if [ "$elapsed" -ge "$MAX_S" ]; then echo "warm-band: time cap ${elapsed}s reached"; break; fi
  p="${PROMPTS[$(( (i - 1) % ${#PROMPTS[@]} ))]} (part $i)"
  t=$(date +%s)
  curl -s -m 400 "http://127.0.0.1:$PORT/v1/chat/completions" \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"$p\"}],\"max_tokens\":700}" \
    -o /dev/null
  dt=$(( $(date +%s) - t )); [ "$dt" -gt 0 ] || dt=1
  tps=$(awk -v n=700 -v d="$dt" 'BEGIN { printf "%.1f", n / d }')
  ok=$(awk -v n=700 -v d="$dt" -v f="$FLOOR" 'BEGIN { print (n / d >= f) ? 1 : 0 }')
  echo "warm-band: req $i/$MAX_REQ ${dt}s ≈ ${tps} tok/s (elapsed ${elapsed}s)"
  if [ "$ok" = "1" ]; then
    streak=$((streak + 1))
    if [ "$streak" -ge 2 ]; then echo "warm-band: recovered — 2 consecutive >= ${FLOOR} tok/s"; break; fi
  else
    streak=0
  fi
done
echo "warm-band: done"
