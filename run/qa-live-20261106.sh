#!/usr/bin/env bash
# Post-QA live verification: health gate -> smoke -> pair self-heal (SIGKILL helper) -> final report.
set -uo pipefail
cd /mnt/srcds/Strata
C=000
for i in $(seq 1 90); do
  C=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:8009/v1/models 2>/dev/null)
  [ "$C" = 200 ] && break
  sleep 6
done
echo "health: $C after ~$((i*6))s"
[ "$C" = 200 ] || exit 1
echo "gen before: $(grep -o '"generation": *[0-9]*' run/prod-swift15-f16kv-mixed-supervisor.json | head -1)"
echo "-- smoke --"
curl -s --max-time 90 http://127.0.0.1:8009/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"swift-1.5-iq3_xxs-f16kv","max_tokens":24,"messages":[{"role":"user","content":"Say OK"}]}' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("smoke finish:", d["choices"][0]["finish_reason"], "| usage:", d["usage"]["total_tokens"])' 2>/dev/null || echo "SMOKE FAILED"
echo "-- helper state --"
H=$(ls -t logs/prod-swift15-f16kv-*helper.log | head -1)
grep -E "listening on|second-channel" "$H" | tail -2
echo "-- self-heal: SIGKILL the helper --"
GEN_BEFORE=$(grep -o '"generation": *[0-9]*' run/prod-swift15-f16kv-mixed-supervisor.json | grep -o '[0-9]*' | head -1)
HPID=$(pgrep -f "strata-expert-helper-patient" | head -1)
echo "killing helper pid $HPID (gen $GEN_BEFORE)"
kill -9 "$HPID"
# wait for supervisor to cycle: new generation + health back
for i in $(seq 1 80); do
  C=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:8009/v1/models 2>/dev/null)
  GEN_NOW=$(grep -o '"generation": *[0-9]*' run/prod-swift15-f16kv-mixed-supervisor.json | grep -o '[0-9]*' | head -1)
  [ "$C" = 200 ] && [ "$GEN_NOW" -gt "$GEN_BEFORE" ] && break
  sleep 6
done
echo "post-kill health: $C, gen: $GEN_NOW (was $GEN_BEFORE)"
echo "-- post-kill smoke --"
curl -s --max-time 120 http://127.0.0.1:8009/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"swift-1.5-iq3_xxs-f16kv","max_tokens":24,"messages":[{"role":"user","content":"Say OK again"}]}' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("post-kill smoke finish:", d["choices"][0]["finish_reason"])' 2>/dev/null || echo "POST-KILL SMOKE FAILED"
H2=$(ls -t logs/prod-swift15-f16kv-*helper.log | head -1)
grep -E "second-channel" "$H2" | tail -1
echo "== live verification done =="
