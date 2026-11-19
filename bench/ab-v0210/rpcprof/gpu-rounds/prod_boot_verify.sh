#!/usr/bin/env bash
# Parent-only boot/acceptance after a separately completed stop-all invocation.
# Never stops processes, changes configuration or refreshes old evidence.
set -euo pipefail
R=/mnt/srcds/Strata
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=${1:?usage: prod_boot_verify.sh NEW_OUT EXPECTED_CONFIG_SHA}
CONFIG_SHA=${2:?expected production config SHA256 is required}
ENGINE=$R/v0238p0/rpcw-hip/strata
ENGINE_SHA=b0ec0109ebc50c982512c371d81b2c15a8f72c70edf8a461a4ff10337da52f02
STATE=$R/run/prod-swift15-f16kv-mixed-supervisor.json
ELOG=$R/logs/engine-prod-ud-iq3xxs-v0233.log
VERIFY=$HERE/champions_restore_verify.py
[[ -z $(ss -H -ltn 'sport = :8009') ]] || { echo 'REFUSE: production port still occupied'; exit 1; }
[[ $(sha256sum "$R/prod-config-v0238.json" | cut -d ' ' -f 1) == "$CONFIG_SHA" ]] || { echo 'REFUSE: config pin mismatch'; exit 1; }
[[ $(sha256sum "$ENGINE" | cut -d ' ' -f 1) == "$ENGINE_SHA" ]] || { echo 'REFUSE: engine pin mismatch'; exit 1; }
mkdir -p "$OUT"
[[ ! -e "$OUT/boot.log" ]] || { echo 'REFUSE: boot evidence already exists'; exit 1; }
cp "$R/prod-config-v0238.json" "$OUT/config.boot.json"
sha256sum "$R/prod-config-v0238.json" "$ENGINE" "$R/launch-v0238.sh" | tee "$OUT/boot-pins.txt"
printf 'Booting canonical launcher at %s\n' "$(date --iso-8601=seconds)"
setsid --fork bash "$R/launch-v0238.sh" >"$OUT/boot.log" 2>&1 < /dev/null
deadline=$((SECONDS + 1800))
accepted=0
while ((SECONDS < deadline)); do
  if python3 "$VERIFY" identity --state "$STATE" --engine "$ENGINE" \
      --expected-sha "$ENGINE_SHA" --log "$OUT/boot.log" >"$OUT/identity.txt" 2>&1; then
    accepted=1
    break
  fi
  printf '[%s] Waiting for current generation: ' "$(date +%T)"
  tail -n 1 "$OUT/identity.txt"
  if [[ -f "$ELOG" ]]; then tail -c 8192 "$ELOG" | tr -d '\0' | tail -n 6; fi
  sleep 10
done
[[ $accepted == 1 ]] || { echo 'FAIL: no current generation accepted within 1800 seconds'; exit 1; }
cat "$OUT/identity.txt"
token=$(grep '^token=' "$OUT/identity.txt" | cut -d = -f 2-)
[[ -n "$token" ]] || { echo 'FAIL: identity token absent'; exit 1; }
status=$(curl --silent --show-error --max-time 15 --output "$OUT/models.json" \
  --write-out '%{http_code}' http://127.0.0.1:8009/v1/models)
printf '%s\n' "$status" >"$OUT/models.status"
python3 "$VERIFY" models --status "$status" --json "$OUT/models.json" | tee "$OUT/models.verdict"
echo 'Verifying a real finite generation before resuming agents'
status=$(curl --silent --show-error --max-time 240 --output "$OUT/completion.json" \
  --write-out '%{http_code}' --header 'Content-Type: application/json' \
  --data '{"model":"ud-iq3_xxs","messages":[{"role":"user","content":"Reply with exactly: OK"}],"max_tokens":64,"temperature":0}' \
  http://127.0.0.1:8009/v1/chat/completions)
printf '%s\n' "$status" >"$OUT/completion.status"
python3 "$VERIFY" health --status "$status" --json "$OUT/completion.json" | tee "$OUT/completion.verdict"
python3 "$VERIFY" identity --state "$STATE" --engine "$ENGINE" \
  --expected-sha "$ENGINE_SHA" --log "$OUT/boot.log" --expect "$token" | tee "$OUT/identity.after.txt"
[[ $(sha256sum "$R/prod-config-v0238.json" | cut -d ' ' -f 1) == "$CONFIG_SHA" ]] || { echo 'FAIL: config changed during boot'; exit 1; }
cp "$STATE" "$OUT/supervisor.after.json"
echo 'PASS: pinned production generation served models and a real completion'
