#!/usr/bin/env bash
# Guard-only test for the f16kv mixed launcher's KSM default + port guard (SPEC-stage2 lever 4).
# CPU ONLY. It never boots a pair and never signals prod: the launcher is invoked with
# --single-run (falls through to the guards, never --recover) with a FAKE ss() function that
# reports port 8009 occupied, so it must refuse at the port guard BEFORE 'mkdir -p run logs'
# (line ~61), before the helper start (~81) and before the server start (~108).
# Proves: (1) refusal + exit 1 + zero side effects, (2) KSM bootstrap is opt-in: absent with the
# default, present with STRATA_KSM=1. Receipts -> logs/ab9-prep-<ts>-*.log.
set -u
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)"
LAUNCHER="$ROOT/launch-prod-swift15-f16kv-dflash-w4-q4_0-mixed.sh"
LOGS="$ROOT/logs"
TS="$(date +%Y%m%d-%H%M%S)"
fail=0
ok(){ echo "ok   $*"; }
bad(){ echo "FAIL $*"; fail=1; }

# pre-state (read-only): counts of launcher-shaped artifacts + prod liveness
MARK=/tmp/opencode/ab9-ksm-mark-$TS   # 'newer than this' marker for the run/ side-effect census
: > "$MARK"
pre_launch_logs=$(find "$LOGS" -maxdepth 1 -name 'prod-swift15-f16kv-dflash-w4-q4_0-mixed-*.log' | wc -l)
pre_helper_logs=$(find "$LOGS" -maxdepth 1 -name 'prod-swift15-f16kv-dflash-w4-q4_0-mixed-*-helper.log' | wc -l)
pre_run_files=$(find "$ROOT/run" -maxdepth 1 -newer "$MARK" 2>/dev/null | wc -l)
pre_pair_pids=$(pgrep -fc 'rpcw-(hip|cuda)/' || true)
pre_health=$(curl -sf -m 3 -o /dev/null -w '%{http_code}' "http://127.0.0.1:8009/health")

# the fake: 'command -v ss' must find it, and its output must be non-empty => occupied
ss(){ echo "tcp LISTEN 0 128 127.0.0.1:8009 0.0.0.0:* users:((\"strata\",pid=916551,fd=42))"; }
export -f ss

run_case(){   # $1 = receipt label, $2 = STRATA_KSM value ('' = leave unset)
  local label=$1 ksm=$2 rc out receipt
  receipt="$LOGS/ab9-prep-$TS-$label.log"
  if [[ -n $ksm ]]; then
    out=$( cd "$ROOT" && env -u STRATA_KSM_BOOTSTRAPPED STRATA_KSM="$ksm" bash "$LAUNCHER" --single-run 2>&1 )
  else
    out=$( cd "$ROOT" && env -u STRATA_KSM -u STRATA_KSM_BOOTSTRAPPED bash "$LAUNCHER" --single-run 2>&1 )
  fi
  rc=$?
  printf '%s\nexit_code=%s\n' "$out" "$rc" > "$receipt"
  echo "case $label: exit=$rc receipt=$receipt"
  grep -q 'REFUSING: port 8009 occupied' <<<"$out" && ok "$label refused at the port guard" \
    || bad "$label did NOT refuse at the port guard"
  (( rc == 1 )) && ok "$label exit 1" || bad "$label exit $rc != 1"
  grep -qE 'Starting mixed backend|expert-helper|Helper readiness|server.py' <<<"$out" \
    && bad "$label reached the helper/server start" || ok "$label never reached helper/server start"
  echo "$receipt"
}

run_case ksm-guard-default ""
grep -q 'KSM enabled' "$LOGS/ab9-prep-$TS-ksm-guard-default.log" \
  && bad "default path ran the KSM bootstrap" || ok "default is OFF: no KSM bootstrap"
run_case ksm-guard-optin 1
grep -q 'KSM enabled for model launcher and children' "$LOGS/ab9-prep-$TS-ksm-guard-optin.log" \
  && ok "STRATA_KSM=1 opts in (bootstrap ran, then the guard refused)" \
  || bad "STRATA_KSM=1 did not run the KSM bootstrap"

# side-effect-free proof: no launcher log, no helper log, no new run/ entries, prod untouched
post_launch_logs=$(find "$LOGS" -maxdepth 1 -name 'prod-swift15-f16kv-dflash-w4-q4_0-mixed-*.log' | wc -l)
post_helper_logs=$(find "$LOGS" -maxdepth 1 -name 'prod-swift15-f16kv-dflash-w4-q4_0-mixed-*-helper.log' | wc -l)
post_run_files=$(find "$ROOT/run" -maxdepth 1 -newer "$MARK" 2>/dev/null | wc -l)
post_pair_pids=$(pgrep -fc 'rpcw-(hip|cuda)/' || true)
post_health=$(curl -sf -m 3 -o /dev/null -w '%{http_code}' "http://127.0.0.1:8009/health")
(( post_launch_logs == pre_launch_logs )) && ok "no launcher log created ($post_launch_logs)" \
  || bad "launcher log created: $pre_launch_logs -> $post_launch_logs"
(( post_helper_logs == pre_helper_logs )) && ok "no helper log created ($post_helper_logs)" \
  || bad "helper log created: $pre_helper_logs -> $post_helper_logs"
(( post_run_files == pre_run_files )) && ok "no new run/ entries ($post_run_files)" \
  || bad "run/ entries changed: $pre_run_files -> $post_run_files"
[[ $pre_pair_pids == "$post_pair_pids" ]] && ok "prod pair pid count unchanged ($post_pair_pids)" \
  || bad "prod pair pid count changed: $pre_pair_pids -> $post_pair_pids"
[[ $pre_health == 200 && $post_health == 200 ]] && ok "prod /health 200 before and after" \
  || bad "prod /health not 200 both times: $pre_health/$post_health"

echo "test-ksm-guard: $([[ $fail == 0 ]] && echo PASS || echo FAIL) receipts in $LOGS/ab9-prep-$TS-*.log"
exit $fail
