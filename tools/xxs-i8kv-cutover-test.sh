#!/usr/bin/env bash
# xxs-i8kv-cutover-test.sh - scripted cutover of prod (:8009) to ud-iq3xxs int8-KV + helper persistence check.
#
# Single entry point for the PARENT operator's prod-down window. Safe/idempotent whether prod is up
# or already down: stage 0 stops everything on :8009 first. Never leaves :8009 down: any stage
# failure fully restores the known-good swift-iq3_s-f16kv prod and verifies it before exiting.
#
# STAGE A (xxs boot, engine-only, port untouched): ud helper (with --persist) + the ud engine run
#         DIRECTLY with the exact args from prod-config-ud-iq3xxs-dflash-w4-q4_0-mixed.json
#         (the --native-dense-gguf 4-shard fix). PASS = engine "session is up" + helper "engine
#         connected" + a real "filled N slots" + NO "helper is absent/fill refused/0 requests" lines.
# STAGE B (helper robustness): full ud launcher boot, completion #1, kill ONLY the engine, let
#         server.py restart it on the next request, and require the PERSISTENT helper to be
#         reconnected (no permanent "helper connect failed" / CPU-path fallback) + completion #2.
# STAGE C (cutover): the launcher pair from B is the new prod: verify /v1/models = ud-iq3_xxs.
#
# Final line: "VERDICT: PASS-CUTOVER" (xxs live) | "VERDICT: RESTORED iq3s-f16kv" + evidence lines.
set -euo pipefail
ROOT=/mnt/user/Strata
WORK="$ROOT/v0210"
PY="$ROOT/.venv/bin/python"
UD_CONFIG="$ROOT/prod-config-ud-iq3xxs-dflash-w4-q4_0-mixed.json"
UD_LAUNCH="$ROOT/launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh"
IQ3S_LAUNCH="$ROOT/launch-prod-swift-iq3s-f16kv-dflash-w4-q4_0-mixed.sh"
HELPER="$WORK/build-mixed-gpu/rpcw-cuda/strata-expert-helper"
ENGINE="$WORK/build-mixed-gpu/rpcw-hip/strata"
SOCKET="$ROOT/run/prod-iq3xxs-ud-mixed.sock"
SOCKET2="$ROOT/run/prod-ud-iq3xxs-mixed2.sock"
UD_ENGINE_LOG="$ROOT/logs/engine-iq3xxs-ud-dflash-w4-q4_0-mixed.log"
STAMP="$(date +%Y%m%d-%H%M%S)"
SLOG="$ROOT/logs/xxs-cutover-$STAMP.log"          # this script's own trail (evidence collector)
AENG="$ROOT/logs/xxs-boot-test-$STAMP.log"        # stage A engine (contract-named xxs-boot-test-*)
AHEL="$ROOT/logs/xxs-boot-test-$STAMP-helper.log"
UD_MODEL=ud-iq3_xxs
IQ3S_MODEL=swift-iq3_s-f16kv
ev() { printf '%s\n' "$*" | tee -a "$SLOG"; }
die_restore() { ev "STAGE FAILURE: $*"; restore_iq3s; }

pair_pids() { pgrep -f 'pair_supervisor.py' 2>/dev/null || true; }
server_pids() { pgrep -f 'serve/server.py --engine strata' 2>/dev/null || true; }
engine_pids() { pgrep -f 'rpcw-hip/strata --serve' 2>/dev/null || true; }
helper_pids() { pgrep -f 'rpcw-cuda/strata-expert-helper' 2>/dev/null || true; }
ud_engine_pid() { pgrep -f 'rpcw-hip/strata --serve.*iq3xxs-ud' 2>/dev/null | head -1 || true; }
port_up() { [[ -n $(ss -H -ltn 'sport = :8009' 2>/dev/null) ]]; }

stop_all() {  # idempotent: TERM server.py, engine, helper (launchers first: their traps reap children)
  pkill -TERM -f 'bash .*launch-prod-' 2>/dev/null && ev 'stop: launchers TERMed' || true
  sleep 3
  for pat in 'serve/server.py --engine strata' 'rpcw-hip/strata --serve' 'rpcw-cuda/strata-expert-helper'; do
    pkill -TERM -f "$pat" 2>/dev/null && ev "stop: TERMed $pat" || true
  done
  for _ in $(seq 1 30); do
    [[ -z "$(server_pids)$(engine_pids)$(helper_pids)" ]] && break
    sleep 2
  done
  for pat in 'serve/server.py --engine strata' 'rpcw-hip/strata --serve' 'rpcw-cuda/strata-expert-helper'; do
    pkill -KILL -f "$pat" 2>/dev/null || true
  done
  rm -f "$SOCKET" "$SOCKET2" "$ROOT"/run/prod-swift-iq3s-f16kv-mixed.sock "$ROOT"/run/prod-swift-iq3s-f16kv-mixed2.sock \
        "$ROOT"/run/prod-swift-iq3_s-mixed.sock "$ROOT"/run/prod-swift-iq3_s-mixed2.sock \
        "$ROOT"/run/prod-mixed.sock "$ROOT"/run/prod-mixed2.sock
  ev 'stop: all prod processes stopped, prod sockets unlinked'
}

models_json() { curl -s -m 10 "http://127.0.0.1:8009/v1/models" 2>/dev/null || true; }
wait_models() {  # $1 model id, $2 budget seconds
  local n=0
  while (( n < $2 )); do
    grep -q "\"id\": \"$1\"" <<<"$(models_json)" && return 0
    sleep 5; n=$((n+5))
  done
  return 1
}
completion_json() {  # $1 model id, $2 reasoning effort; the raw chat.completions JSON
  # max_tokens 512 + request-level reasoning_effort: this is a REASONING model (config budget
  # 55k tokens); at 24 tokens everything burned on reasoning_content and message.content stayed
  # null (finish_reason length) - that probe bug failed stage B and faked a restore CRITICAL.
  curl -s -m 300 http://127.0.0.1:8009/v1/chat/completions \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"$1\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly: XXS-OK\"}],\"max_tokens\":512,\"temperature\":0,\"reasoning_effort\":\"$2\"}"
}
completion() {  # $1 model id, $2 effort; prints the reply's message.content (empty = not a pass)
  completion_json "$1" "$2" | "$PY" -c 'import json,sys
try:
    print((json.load(sys.stdin)["choices"][0]["message"].get("content") or "").strip())
except Exception:
    pass' 2>/dev/null
}
gen_probe() {  # $1 model id, $2 effort; exit 0 if the endpoint demonstrably GENERATED text:
  # real content, OR reasoning text that ran the prompt's token budget out (finish_reason
  # length). The restore path must not print CRITICAL on a reasoning model mid-reasoning.
  completion_json "$1" "$2" | "$PY" -c 'import json,sys
try:
    c = json.load(sys.stdin)["choices"][0]
    m = c.get("message") or {}
    ok = bool((m.get("content") or "").strip())
    gen = bool((m.get("reasoning_content") or "").strip())
    sys.exit(0 if ok or (gen and c.get("finish_reason") == "length") else 1)
except Exception:
    sys.exit(1)' 2>/dev/null
}

restore_iq3s() {
  ev 'RESTORE: bringing back the known-good swift-iq3_s-f16kv prod ...'
  stop_all
  RLOG="$ROOT/logs/xxs-restore-iq3s-$STAMP.log"
  setsid nohup "$IQ3S_LAUNCH" >> "$RLOG" 2>&1 &
  if wait_models "$IQ3S_MODEL" 420; then
    ev "restore: /v1/models = $IQ3S_MODEL (see $RLOG)"
    local reply; reply="$(completion "$IQ3S_MODEL" low)"
    ev "restore: completion probe: '${reply:0:60}'"
    if [[ -n "$reply" ]] || gen_probe "$IQ3S_MODEL" low; then
      ev 'VERDICT: RESTORED iq3s-f16kv'
      exit 1
    fi
  fi
  ev "CRITICAL: restore FAILED - :8009 not serving $IQ3S_MODEL; launcher log: $RLOG; INTERVENTION NEEDED"
  exit 1
}

start_ud_launcher() {  # sets the globals LPID UDLOG UDHLOG
  UD_RUNLOG="$ROOT/logs/xxs-cutover-launch-$STAMP.out"
  setsid nohup "$UD_LAUNCH" >> "$UD_RUNLOG" 2>&1 &
  LPID=$!
  for _ in $(seq 1 15); do
    UDLOG=$(grep -oP '(?<=log: )[^\s;]+\.log' "$UD_RUNLOG" | head -1 || true)
    UDHLOG=$(grep -oP '(?<=helper: )[^\s]+\.log' "$UD_RUNLOG" | head -1 || true)
    [[ -n "$UDLOG" && -n "$UDHLOG" ]] && break
    sleep 1
  done
  [[ -n "${UDLOG:-}" && -n "${UDHLOG:-}" ]] || die_restore "launcher printed no 'log:/helper:' line (see $UD_RUNLOG)"
  ev "ud launcher pid $LPID; server log: $UDLOG; helper log: $UDHLOG"
}

start_ud_helper_stage_a() {  # same shape as the launcher's helper, logs to AHEL
  env -i PATH="$PATH" HOME="$HOME" TERM="${TERM:-dumb}" "$HELPER" \
    --pack /mnt/user/ai/iq3xxs-ud \
    --experts ../Qwen3.8-Flash-Next-UD-IQ3_XXS-00001-of-00004.gguf \
    --persist \
    --device 0 --slots 4600 --reserve-mib 300 --timeout 63115200000 \
    --begin-timeout 1800000 --socket "$SOCKET" --fill-mib 6796 --socket2 "$SOCKET2" \
    >> "$AHEL" 2>&1 < /dev/null &
  AHPID=$!
  for _ in $(seq 1 90); do
    kill -0 "$AHPID" 2>/dev/null || die_restore "stage A helper exited early; see $AHEL"
    grep -q 'listening on' "$AHEL" && grep -q 'second channel listening on' "$AHEL" && break
    sleep 1
  done
  grep -q 'listening on' "$AHEL" || die_restore "stage A helper readiness timed out; see $AHEL"
  ev "stage A helper pid $AHPID listening on both ud sockets"
}

# ================================ run ================================
chmod +x "$0" 2>/dev/null || true
[[ -x "$HELPER" && -x "$ENGINE" && -x "$UD_LAUNCH" && -x "$IQ3S_LAUNCH" ]] || { ev "ABORT: missing executable (helper/engine/launcher)"; exit 2; }
"$PY" -m json.tool "$UD_CONFIG" > /dev/null
if [[ -n "$(pair_pids)" ]]; then
  ev 'ABORT: a pair_supervisor.py is running (it would restart what this script stops). Stop it first.'
  exit 2
fi
ev "== xxs-i8kv cutover, run $STAMP (own trail: $SLOG)"
stop_all

# ---------- STAGE A: engine-only boot of the fixed ud config (port 8009 untouched) ----------
rm -f "$SOCKET" "$SOCKET2"
start_ud_helper_stage_a
mapfile -t ENGINE_ARGS < <("$PY" -c '
import json, sys
c = json.load(open(sys.argv[1]))
a = ["--serve"] + list(c["args"])  # --serve prepended exactly as server.py does (checked vs live cmdline)
a += ["--layer-split", str(c["layer_split"]),
      "--expert-helper-socket", c["expert_helper_socket"],
      "--expert-helper-socket2", c["expert_helper_socket2"],
      "--expert-helper-timeout-ms", str(c["expert_helper_timeout"])]
print("\n".join(a))' "$UD_CONFIG")
mapfile -t ENGINE_ENV < <("$PY" -c '
import json, sys
c = json.load(open(sys.argv[1]))
for k, v in c["env"].items(): print(f"{k}={v}")
print("LD_LIBRARY_PATH=" + ":".join(c["lib_dirs"]))' "$UD_CONFIG")
ev "STAGE A: engine direct run (${#ENGINE_ARGS[@]} args, $(grep -c -- --native-dense-gguf <<<"${ENGINE_ARGS[*]}") native-dense shards) -> $AENG"
# stdin /dev/null: the engine exits by itself once its serve loop sees EOF, after session-up.
timeout -k 10 600 env "${ENGINE_ENV[@]}" "$ENGINE" "${ENGINE_ARGS[@]}" < /dev/null >> "$AENG" 2>&1 && ARC=0 || ARC=$?
A_FILL=$(grep -m1 -E 'filled [0-9]+ slots' "$AHEL" || true)
if ! { grep -q 'session is up' "$AENG" && grep -q 'engine connected' "$AHEL" \
       && ! grep -Eq 'helper is absent|fill refused|channel closed after 0 requests' "$AENG" \
       && [[ -n "$A_FILL" ]]; }; then
  ev "STAGE A FAIL (engine rc $ARC); engine log tail:"; tail -12 "$AENG" | tee -a "$SLOG"
  ev "helper log tail:"; tail -6 "$AHEL" | tee -a "$SLOG"
  die_restore "stage A: the xxs engine did not come up ON the expert helper (need session-up + engine connected + a real 'filled N slots' + NO absent/refused/0-requests lines)"
fi
ev "STAGE A PASS: engine rc $ARC, 'session is up' + helper 'engine connected' + ${A_FILL:0:110}"
kill -TERM "$AHPID" 2>/dev/null || true
for _ in $(seq 1 15); do kill -0 "$AHPID" 2>/dev/null || break; sleep 1; done
rm -f "$SOCKET" "$SOCKET2"

# ---------- STAGE B: full ud launcher boot + hiccup simulation (engine killed, helper persists) ----------
port_up && die_restore "port 8009 is still occupied after stage A"
start_ud_launcher
UD_ENGINE_BASE=$(wc -l < "$UD_ENGINE_LOG" 2>/dev/null || echo 0)
if ! wait_models "$UD_MODEL" 420; then
  ev "engine log tail:"; tail -15 "$UD_ENGINE_LOG" | tee -a "$SLOG"
  die_restore "ud launcher boot: /v1/models never served $UD_MODEL"
fi
grep -q 'engine connected' "$UDHLOG" || die_restore "ud boot: helper never saw 'engine connected'"
REPLY1="$(completion "$UD_MODEL" low)"
[[ -n "$REPLY1" ]] || die_restore "ud boot: completion #1 returned no message.content (reasoning burn? engine log tail: $(tail -2 "$UD_ENGINE_LOG" | tr '\n' ' '))"
CONNECT1=$(grep -c 'connected (peer uid' "$UDHLOG" || true)
ev "STAGE B boot: $UD_MODEL serving, completion #1: '${REPLY1:0:60}'; helper engine connections so far: $CONNECT1"

EPID=$(ud_engine_pid)
[[ -n "$EPID" ]] || die_restore "cannot find the ud engine pid for the hiccup simulation"
ev "hiccup: killing ONLY the engine (pid $EPID), server.py restarts it on the next request"
kill -TERM "$EPID"
sleep 5
REPLY2=''
for _ in $(seq 1 28); do            # first post-death request ends with an error; the next starts the engine
  REPLY2="$(completion "$UD_MODEL" low)"
  [[ -n "$REPLY2" ]] && break
  sleep 15
done
CONNECT2=$(grep -c 'connected (peer uid' "$UDHLOG" || true)
NEWSEG=$(tail -n +"$((UD_ENGINE_BASE+1))" "$UD_ENGINE_LOG")
grep -q -- '--persist: engine session .* ended; listening again' "$UDHLOG" \
  || die_restore "hiccup: the helper did NOT re-listen (no --persist line in $UDHLOG)"
grep -q 'helper connect failed' <<<"$NEWSEG" && die_restore "hiccup: restarted engine logged 'helper connect failed' (CPU-path fallback)"
[[ "$CONNECT2" -gt "$CONNECT1" ]] || die_restore "hiccup: the restarted engine never reconnected the helper"
[[ -n "$REPLY2" ]] || { ev "engine log tail:"; tail -15 "$UD_ENGINE_LOG" | tee -a "$SLOG"; die_restore "hiccup: completion #2 after the engine restart returned no content"; }
ev "STAGE B PASS: helper re-listened, engine reconnected ($CONNECT1 -> $CONNECT2 connections), completion #2: '${REPLY2:0:60}'"

# ---------- STAGE C: this pair is the new prod ----------
grep -q "\"id\": \"$UD_MODEL\"" <<<"$(models_json)" || die_restore "cutover: /v1/models lost $UD_MODEL"
ev "helper log re-listen line: $(grep -m1 -- '--persist: engine session' "$UDHLOG" | head -c 140)"
ev "final pids: launcher $LPID, server $(server_pids | head -1), engine $(ud_engine_pid), helper $(helper_pids | head -1)"
ev "kv mode: $(tr '\0' ' ' < "/proc/$(ud_engine_pid)/cmdline" | grep -oP '(?<=--kv )\S+')"
ev "model: $UD_MODEL on 127.0.0.1:8009; server log: $UDLOG; helper log: $UDHLOG"
ev 'VERDICT: PASS-CUTOVER'
exit 0
