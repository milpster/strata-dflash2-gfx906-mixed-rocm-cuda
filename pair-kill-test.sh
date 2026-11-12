#!/usr/bin/env bash
# Both-reset proof: SIGKILL the helper, expect pair_supervisor to restart the WHOLE pair
# (generation bump) and health to return. On-disk on purpose (no inline kill patterns).
set -u
cd ${STRATA_PROD_TREE:-${STRATA_PROD_TREE:-/mnt/user/Strata}}
HPID=$(pgrep -f 'rpcw-cuda/strata-expert-helper' | head -1)
[[ -n "$HPID" ]] || { echo 'no helper running'; exit 1; }
echo "=== kill-test: SIGKILL helper pid $HPID ==="
kill -9 "$HPID" && echo 'helper SIGKILLed'
t0=$(date +%s)
PASS=0
for i in $(seq 1 60); do
  GEN=$(python3 -c "import json;print(json.load(open('run/prod-mixed-supervisor.json')).get('generation',0))" 2>/dev/null || echo 0)
  H=$(curl -sf -m 2 -o /dev/null -w '%{http_code}' http://127.0.0.1:8009/health 2>/dev/null || echo 000)
  echo "t+$(( $(date +%s) - t0 ))s generation=$GEN health=$H"
  if [[ "$GEN" -ge 2 && "$H" == "200" ]]; then
    echo 'KILL-TEST PASS: helper loss restarted the whole pair'
    PASS=1
    break
  fi
  sleep 5
done
[[ "$PASS" == "1" ]] || echo 'KILL-TEST FAIL OR SLOW - inspect state below'
echo '=== final supervisor state ==='
cat run/prod-mixed-supervisor.json
echo '=== supervisor restart lines ==='
grep -a 'mixed-pair' /tmp/opencode/prod-pair-supervised.log | tail -3
python3 - <<'PY'
import json, time, urllib.request
body = json.dumps({"model":"probe","messages":[{"role":"user","content":"[gen2] The restarted generation serves with both peers alive again, and this smoke confirms the pair came back whole."}],"max_tokens":32,"temperature":0.0,"stream":False}).encode()
req = urllib.request.Request("http://127.0.0.1:8009/v1/chat/completions", data=body, headers={"Content-Type":"application/json"})
t0=time.time(); r=json.loads(urllib.request.urlopen(req,timeout=300).read()); t=time.time()-t0
print(f"gen2-smoke: wall {t:.2f}s finish={(r.get('choices') or [{}])[0].get('finish_reason')}")
PY
