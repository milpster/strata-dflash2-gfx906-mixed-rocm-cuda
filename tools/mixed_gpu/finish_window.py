#!/usr/bin/env python3
"""Parent-owned numerical acceptance window: capture real inputs, measure, restore.

Two bounded phases over ONE exclusive evidence family, never the legacy runners:

  capture - stop supervised canonical production (identity-checked), then for each
            of the 8 pinned fixtures boot ONE private helper-less engine built with
            STRATA_P2_DIAGNOSTICS=ON, send the fixture text once, and collect the
            first-chunk multi-layer dumps <fixture>.<layer>.bin for layers 32-47
            written by STRATA_P2_DUMP_PREFIX (exclusive creation, never overwrite).
            Checkpointed; a failed fixture is retained and stops the window.
  measure - with a complete capture, stop production, start the CUDA helper on the
            3080 over a private socket, run the gfx906 HIP reference harness on a
            Radeon VII against the SAME dumps, audit the strict-v2 report, restore.
  longctx - NO outage: the live canonical production IS the mixed system under
            test.  Identity-check the supervisor, send ONE deterministic
            ~80k-token prompt with thinking off and a >=5k-token generation
            request, and accept only on the server's ACTUAL usage.prompt_tokens
            and usage.completion_tokens (never the prompt-size estimate).

Both phases ALWAYS restore the canonical --recover launcher in ``finally`` and
verify models/fill/thinking-off content before reporting success.  No phase edits
source, changes gates or adopts anything; the original relative-error rule stays
with the parent, applied AFTER the run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE / "p2_measure"))

import check_fixture_pins                                              # noqa: E402
import pair_supervisor as pair                                         # noqa: E402
import recovery_window as runtime                                      # noqa: E402
import slots_window                                                    # noqa: E402

WORK = _HERE.parents[1]
ROOT = Path("/mnt/user/Strata")
EV_ROOT = WORK / "build-mixed-gpu/p2-finish"
PACK = Path("/mnt/user/ai/iq3xxs-15")
EXPERTS_GGUF = "../Swift-1.5-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS.gguf"
DIAG_ENGINE = WORK / "build-mixed-gpu/rpcw-hip-nmse2/strata"
CUDA_HELPER = WORK / "build-mixed-gpu/rpcw-cuda-nmse2/strata-expert-helper"
HIP_REFERENCE = WORK / "build-mixed-gpu/rpcw-hip-nmse2/p2_measure_hip_reference"
AUDIT = _HERE / "p2_measure/p2_report_audit.py"
LAYERS = list(range(32, 48))
ROCM_LIBS = "/home/user/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib"


# ---------------------------------------------------------------- pure helpers

def dump_size_ok(path: Path) -> tuple[bool, str, int]:
    """Validate one STRATA_P2_DUMP file: header {T,N,K} int64 all > 0 and the
    byte count exactly 24 + T*N*4 + T*K*4.  Never invents a pass."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return False, f"unreadable: {exc}", 0
    if len(raw) < 24:
        return False, "short header", len(raw)
    t = int.from_bytes(raw[0:8], "little", signed=True)
    n = int.from_bytes(raw[8:16], "little", signed=True)
    k = int.from_bytes(raw[16:24], "little", signed=True)
    if t <= 0 or n <= 0 or k <= 0:
        return False, f"bad dims {t},{n},{k}", len(raw)
    want = 24 + t * n * 4 + t * k * 4
    if len(raw) != want:
        return False, f"size {len(raw)} != {want} for {{{t},{n},{k}}}", len(raw)
    return True, f"{{{t},{n},{k}}}", len(raw)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_checkpoint(path: Path) -> dict[tuple[str, int], str]:
    done: dict[tuple[str, int], str] = {}
    if not path.exists():
        return done
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) == 5 and parts[4] == "ok":
            done[(parts[0], int(parts[1]))] = parts[3]
    return done


def append_checkpoint(path: Path, fixture: str, layer: int, nbytes: int, sha: str, status: str) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write("%s\t%d\t%d\t%s\t%s\n" % (fixture, layer, nbytes, sha, status))


def capture_complete(capdir: Path, fixtures: list[str]) -> tuple[bool, list[str]]:
    """Every (fixture, layer) file validates; returns the missing/invalid ids."""
    bad = []
    for fid in fixtures:
        for layer in LAYERS:
            path = capdir / f"{fid}.{layer}.bin"
            ok, _why, _n = dump_size_ok(path) if path.exists() else (False, "absent", 0)
            if not ok:
                bad.append(f"{fid}.{layer}")
    return (not bad), bad


def derive_capture_config(prod: dict, *, engine: Path, prefix: Path, log: Path,
                          port: int, model_name: str) -> dict:
    """The helper-less OFF capture pair from the production config: parity args
    verbatim, NO expert-helper socket keys (so server.py injects none), the
    diagnostics env knob added, private port/log/model name."""
    cfg = json.loads(json.dumps(prod))
    cfg["exe"] = str(engine)
    cfg["log"] = str(log)
    cfg["port"] = port
    cfg["model_name"] = model_name
    for key in ("expert_helper_socket", "expert_helper_socket2"):
        cfg.pop(key, None)
    cfg.pop("mcp_servers", None)
    cfg.pop("vision", None)
    env = dict(cfg.get("env", {}))
    env["STRATA_P2_DUMP_PREFIX"] = str(prefix)
    cfg["env"] = env
    if cfg.get("lib_dirs") != ["/home/user/rocm-gfx906-xnack/lib", "/opt/rocm-6.1.0/lib"]:
        raise ValueError("capture lib_dirs must keep custom rocBLAS before ROCm 6.1")
    if not 1024 <= port <= 65535 or port == 8009:
        raise ValueError("capture port must be private (not 8009)")
    if cfg["args"].count("--expert-helper-slots") != 1:
        raise ValueError("production argv lost its explicit helper slot cap")
    return cfg


def capture_request(model: str, text: str, max_tokens: int) -> bytes:
    """Deterministic capture request: temp 0, thinking explicitly OFF (a
    reasoning-only reply still prefills, but the artifact must be plain)."""
    return json.dumps({"model": model, "messages": [{"role": "user", "content": text}],
                       "max_tokens": int(max_tokens), "temperature": 0,
                       "reasoning_budget_tokens": 0,
                       "chat_template_kwargs": {"enable_thinking": False}}).encode()


def helper_argv(helper: Path, socket: Path, *, device: int = 0, slots: int = 3400,
                reserve_mib: int = 4000) -> list[str]:
    return [str(helper), "--pack", str(PACK), "--experts", EXPERTS_GGUF,
            "--socket", str(socket), "--device", str(device),
            "--slots", str(slots), "--reserve-mib", str(reserve_mib),
            "--begin-timeout", "600000", "--timeout", "600000"]


def reference_env(base: dict | None = None) -> dict:
    """Engine-parity GPU env for the gfx906 reference process.  The session can
    carry CUDA_VISIBLE_DEVICES='' and a wrong HSA_OVERRIDE_GFX_VERSION (probe
    2026-11-02 fin1-measure-env-probe.log: inherited env -> hipGetDeviceCount
    rc=100, n_dev=0, the harness's REFUSAL (device); engine-parity mask ->
    rc=0, n_dev=2).  Both are therefore set explicitly, never inherited."""
    env = dict(os.environ if base is None else base)
    env.update(LD_LIBRARY_PATH=ROCM_LIBS, CUDA_VISIBLE_DEVICES="0,1",
               HSA_OVERRIDE_GFX_VERSION="9.0.6", HIP_FORCE_P2P="1",
               HSA_ENABLE_SDMA="1")
    return env


def reference_argv(out_json: Path, socket: Path, capdir: Path, *, device: int = 0,
                   max_device_mib: int = 3000, slots: int = 3400,
                   reserve_mib: int = 4000, layers: str = "32-47") -> list[str]:
    """The harness hello must carry an EXPLICIT slot budget: the helper refuses
    any hello with slots <= 0 (sidecar/helper_main.cpp:1046 -> kErrGeometry, the
    fin1 att2 refusal), so the harness default --slots 0 is unusable; 3400/4000
    mirror helper_argv and the proven M5 harness invocation.  No --arm-groups:
    the arm walk (claimed pairs removed) refused fin1 att3 ("this run keeps no
    rows") because the 16-layer ranked fill covers early layers completely; the
    proven M5 measurement never passed it, and the strict-v2 report's core
    reference-vs-helper rows come from the main walk."""
    return [str(HIP_REFERENCE), "--pack", str(PACK), "--socket", str(socket),
            "--activations", str(capdir), "--out", str(out_json),
            "--layers", layers, "--device", str(device),
            "--max-device-mib", str(max_device_mib),
            "--slots", str(slots), "--reserve-mib", str(reserve_mib)]


def window_paths(ev: Path, report_name: str = "measure.json") -> dict[str, Path]:
    """Per-window evidence names: each 4-layer window gets its own report, logs,
    audit and result file so sequential windows never overwrite each other (the
    reference log is opened 'wb').  The default names match the att4 evidence."""
    m = re.fullmatch(r"measure(?:-L(3[2-9]|4[0-7])-(3[2-9]|4[0-7]))?\.json", report_name)
    if not m:
        raise ValueError(f"report name must be measure.json or measure-L<A>-<B>.json "
                         f"with A,B in 32..47: {report_name}")
    if m.group(1) is not None and int(m.group(1)) > int(m.group(2)):
        raise ValueError(f"report window wants A<=B: {report_name}")
    stem = report_name[:-len(".json")]
    sfx = "" if stem == "measure" else "-" + stem[len("measure-"):]
    return {"report": ev / report_name,
            "reference_log": ev / f"reference{sfx}.log",
            "helper_log": ev / f"helper{sfx}.log",
            "audit_log": ev / f"audit{sfx}.log",
            "result": ev / f"measure-result{sfx}.json",
            "socket": ev / "measure.sock"}


def parse_layers(text: str) -> tuple[int, int]:
    """Validate a LO-HI window inside the capture's 32..47 span."""
    m = re.fullmatch(r"(3[2-9]|4[0-7])-(3[2-9]|4[0-7])", text or "")
    if not m:
        raise ValueError(f"--layers wants A-B with A,B in 32..47, got {text!r}")
    lo, hi = int(m.group(1)), int(m.group(2))
    if lo > hi:
        raise ValueError(f"--layers wants A<=B, got {text!r}")
    return lo, hi


PROD_MODEL = "swift-1.5-iq3_xxs"          # the canonical production model name
LONGCTX_MIN_PROMPT = 80000                 # acceptance floors live here, once
LONGCTX_MIN_COMPLETION = 5000


def longctx_prompt(target_tokens: int, chars_per_token: float = 4.0) -> tuple[str, dict]:
    """Deterministic prompt whose ACTUAL token count the server reports back; the
    estimate here only sizes the text (with headroom) and is never the verdict."""
    head = ("You are a maintenance-log analysis assistant. The document below is a long "
            "plant cooling-loop maintenance log. Read all of it, then do the task at the "
            "very end.\n\n### LOG START\n\n")
    tail = ("\n### LOG END\n\nTASK: Using the whole log above, write a detailed chronological "
            "digest with one bullet per 500-section block (quote each block's first section "
            "number), then a table of recurring part failures with dates, then a paragraph on "
            "flow-rate trends, then a proposed maintenance schedule for the next year. Be "
            "exhaustive and specific; do not summarize briefly and do not stop early.")
    sentences = [
        "The maintenance log records every cooling loop service with dates, part numbers and measured flow rates.",
        "Each pump service lists the replaced seal kit, the measured shaft runout and the motor current draw.",
        "Filter cartridges were swapped on a rotating schedule and the pressure drop across each bank was logged.",
        "Nightly telemetry captured loop temperatures, reservoir levels and the glycol concentration readings.",
        "Technicians noted every alarm with its code, the responding shift and the corrective action taken.",
        "Quarterly borescope inspections photographed the heat-exchanger plates and logged the fouling estimate.",
        "The spares inventory tracked every issued part against its storage rack and its supplier lot number.",
        "Seasonal load tests ran both loops at full capacity and recorded the bypass valve positions throughout.",
    ]
    want = int(target_tokens * chars_per_token * 1.07) + 2048
    parts, total, index = [head], len(head), 0
    while total < want:
        line = (f"[sec {index:06d}] {sentences[index % len(sentences)]} Loop {index % 9}; "
                f"flow 3.{index % 10 + 1} l/min; filter P{index % 7}; "
                f"serviced day {index % 28 + 1}.\n")
        parts.append(line)
        total += len(line)
        index += 1
    text = "".join(parts) + tail
    meta = dict(target_tokens=target_tokens, chars_per_token=chars_per_token,
                headroom=1.07, chars=len(text), est_tokens=round(len(text) / chars_per_token),
                sections=index,
                sha256=hashlib.sha256(text.encode()).hexdigest())
    return text, meta


def longctx_request(prompt: str, max_tokens: int, model: str = PROD_MODEL) -> bytes:
    """One thinking-OFF deterministic completion; same request shape as the
    capture/restore smokes (temp 0, budget 0)."""
    return json.dumps({"model": model,
                       "messages": [{"role": "user", "content": prompt}],
                       "max_tokens": int(max_tokens), "temperature": 0,
                       "reasoning_budget_tokens": 0,
                       "chat_template_kwargs": {"enable_thinking": False}}).encode()


def longctx_verdict(http_code: int, usage: dict | None, content,
                    *, min_prompt: int = LONGCTX_MIN_PROMPT,
                    min_completion: int = LONGCTX_MIN_COMPLETION) -> dict:
    """Acceptance on the server-reported ACTUAL usage only; no estimate ever
    substitutes for prompt_tokens/completion_tokens."""
    usage = usage or {}
    prompt_tokens, completion_tokens = usage.get("prompt_tokens"), usage.get("completion_tokens")
    checks = dict(http_ok=http_code == 200,
                  prompt_tokens_ok=type(prompt_tokens) is int and prompt_tokens >= min_prompt,
                  completion_tokens_ok=type(completion_tokens) is int and completion_tokens >= min_completion,
                  content_ok=bool(content))
    return dict(**checks, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                thresholds=dict(min_prompt_tokens=min_prompt, min_completion_tokens=min_completion),
                passed=all(checks.values()))


def expect_production(pid: int) -> dict:
    """The stop_production identity check WITHOUT stopping anything: argv, state
    file and liveness must all name this exact attached supervisor."""
    argv = runtime.command(pid)
    required = [str(runtime.WORK / "tools/mixed_gpu/pair_supervisor.py"),
                str(runtime.LAUNCHER), str(runtime.STATE)]
    if not all(value in argv for value in required):
        raise RuntimeError("Production supervisor identity mismatch; refusing longctx")
    state = json.loads(runtime.STATE.read_text())
    if state["supervisor_pid"] != pid:
        raise RuntimeError("Production state belongs to another supervisor")
    if state.get("state") != "attached":
        raise RuntimeError("Production state is not attached")
    if not pair.alive(pid, pair.process_token(pid)[1]):
        raise RuntimeError("Expected supervisor is not alive")
    return state


# ------------------------------------------------------------------ lifecycle

def exclusive_dir(path: Path) -> Path:
    if path.exists() or path.is_symlink():
        raise RuntimeError(f"Refusing to touch an existing evidence path: {path}")
    path.mkdir(parents=True)
    return path


def wait_http_ok(url: str, deadline: float, timeout: int = 10) -> bool:
    """Readiness is the /health JSON's loaded:true, not a bare 200 (a 200 with
    loaded:false would send the capture request into a loading engine)."""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                if response.status == 200:
                    try:
                        if json.loads(response.read().decode()).get("loaded"):
                            return True
                    except ValueError:
                        pass
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(2)
    return False


def stop_tree(server: subprocess.Popen, known: dict[int, str], timeout: float = 90) -> None:
    """Bounded teardown of OUR OWN session: TERM the group, wait every remembered
    start-token dead, escalate to KILL, then insist none survive."""
    try:
        os.killpg(server.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        known = pair.descendants(server.pid, known)
        if not any(pair.alive(pid, start) for pid, start in known.items()):
            server.wait(timeout=5)
            return
        time.sleep(1)
    known = pair.descendants(server.pid, known)
    for pid, start in known.items():
        if pair.alive(pid, start):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    server.wait(timeout=15)
    deadline = time.monotonic() + 15
    while any(pair.alive(pid, start) for pid, start in pair.descendants(server.pid, known).items()):
        if time.monotonic() >= deadline:
            raise RuntimeError("capture pair did not exit; refusing the next fixture")
        time.sleep(0.5)


def restore_canonical(result: dict, family: str) -> None:
    """The unconditional exit path: canonical --recover, attached helper fill,
    then a thinking-OFF content smoke (HTTP 200 alone is not a content pass)."""
    console = EV_ROOT / family / "restore-console.log"
    console.parent.mkdir(parents=True, exist_ok=True)
    restored = runtime.launch(True, console)
    state = runtime.wait_for(lambda: runtime.attached(supervisor_pid=restored.pid))
    runtime.check_fill(state)
    state["engine_pid"] = runtime.engine_of(state)
    body = json.dumps({"model": "swift-1.5-iq3_xxs",
                       "messages": [{"role": "user", "content": "Reply with just the word ready."}],
                       "max_tokens": 96, "temperature": 0, "reasoning_budget_tokens": 0,
                       "chat_template_kwargs": {"enable_thinking": False}}).encode()
    request = urllib.request.Request("http://127.0.0.1:8009/v1/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=180) as response:
        raw = response.read()
        code = response.status
    (EV_ROOT / family / "restore-smoke.json").write_bytes(raw)
    reply = json.loads(raw)
    if code != 200 or not reply["choices"][0]["message"].get("content"):
        raise RuntimeError("Restored production did not generate final content")
    result["restored"] = dict(state, completion_http=code, smoke="content-ok")


def fixture_texts() -> dict[str, str]:
    src = (_HERE / "p2_measure/p2_measure.cpp").read_text(encoding="utf-8")
    out = {}
    for fid, sym in check_fixture_pins.FIXTURES:
        m = re.search(r"\bconst char\* const %s\s*=" % sym, src)
        if not m:
            raise RuntimeError(f"fixture constant {sym} missing from p2_measure.cpp")
        lits, _ = check_fixture_pins.c_string_literals(src, src.index('"', m.end()))
        out[fid] = "".join(lits)
    return out


# --------------------------------------------------------------------- phases

def phase_capture(args, result: dict) -> None:
    ev = exclusive_dir(EV_ROOT / args.family)
    capdir = ev / "cap"
    capdir.mkdir()
    texts = fixture_texts()
    fixtures = list(texts) if args.fixtures == "all" else args.fixtures.split(",")
    unknown = [f for f in fixtures if f not in texts]
    if unknown:
        raise RuntimeError(f"unknown fixtures: {unknown}")
    for binary in (args.engine, ):
        if not Path(binary).is_file() or not os.access(binary, os.X_OK):
            raise RuntimeError(f"diagnostics engine missing or not executable: {binary}")
    if Path(args.engine).resolve() == Path(json.loads(runtime.CONFIG.read_text())["exe"]).resolve():
        raise RuntimeError("capture engine must differ from the production binary")
    result["capture"] = dict(family=args.family, engine=str(args.engine),
                             engine_sha256=sha256_file(Path(args.engine)), fixtures=fixtures)

    slots_window.stop_production(args.expected_supervisor, result)
    try:
        for fid in fixtures:
            prefix = capdir / fid
            log = ev / f"engine-{fid}.log"
            cfg_path = ev / f"config-{fid}.json"
            cfg = derive_capture_config(json.loads(runtime.CONFIG.read_text()),
                                        engine=Path(args.engine), prefix=prefix, log=log,
                                        port=args.port, model_name=f"{args.family}-capture")
            cfg_path.write_text(json.dumps(cfg, indent=1) + "\n", encoding="utf-8")
            body = capture_request(cfg["model_name"], texts[fid], args.max_tokens)
            (ev / f"request-{fid}.json").write_bytes(body)
            with log.open("wb") as elog:
                server = subprocess.Popen(["/mnt/user/Strata/.venv/bin/python",
                                           "serve/server.py", "--engine", "strata",
                                           "--config", str(cfg_path), "--port", str(args.port)],
                                          cwd=str(WORK), stdout=elog, stderr=subprocess.STDOUT,
                                          start_new_session=True)
            known = {server.pid: pair.process_token(server.pid)[1]}
            try:
                if not wait_http_ok(f"http://127.0.0.1:{args.port}/health",
                                    time.monotonic() + args.load_timeout):
                    raise RuntimeError(f"{fid}: engine not ready (rc={server.poll()})")
                request = urllib.request.Request(
                    f"http://127.0.0.1:{args.port}/v1/chat/completions", data=body,
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(request, timeout=args.req_timeout) as response:
                    raw = response.read()
                    code = response.status
                (ev / f"reply-{fid}.json").write_bytes(raw)
                if code != 200:
                    raise RuntimeError(f"{fid}: capture request HTTP {code}")
                written = [ln for ln in log.read_text(errors="replace").splitlines()
                           if "p2 input capture:" in ln and "written" in ln]
                (ev / f"capture-lines-{fid}.txt").write_text("\n".join(written) + "\n")
                for layer in LAYERS:
                    path = Path(f"{prefix}.{layer}.bin")
                    ok, why, nbytes = dump_size_ok(path) if path.exists() else (False, "absent", 0)
                    sha = sha256_file(path) if ok else "-"
                    append_checkpoint(ev / "checkpoint.tsv", fid, layer, nbytes, sha,
                                      "ok" if ok else why)
                    if not ok:
                        raise RuntimeError(f"{fid}: layer {layer} dump {why}")
            finally:
                stop_tree(server, known)
            print(f"[capture] {fid}: 16/16 layers validated", flush=True)
        complete, bad = capture_complete(capdir, fixtures)
        result["capture"]["complete"] = complete
        result["capture"]["missing"] = bad
        if not complete:
            raise RuntimeError(f"capture incomplete after pass: {bad[:8]}")
    finally:
        restore_canonical(result, args.family)


def phase_measure(args, result: dict) -> None:
    ev = EV_ROOT / args.family
    capdir = ev / "cap"
    texts = fixture_texts()
    complete, bad = capture_complete(capdir, list(texts))
    if not complete:
        raise RuntimeError(f"capture family {args.family} incomplete: {bad[:8]}")
    for binary in (CUDA_HELPER, HIP_REFERENCE):
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise RuntimeError(f"measure binary missing: {binary}")
    paths = window_paths(ev, getattr(args, "report_name", "measure.json"))
    out_json = paths["report"]
    if out_json.exists():
        raise RuntimeError(f"refusing to overwrite an existing report: {out_json}")
    socket_path = paths["socket"]
    if socket_path.exists() or socket_path.is_symlink():
        raise RuntimeError(f"refusing an existing measure socket path: {socket_path}")
    layers = getattr(args, "layers", "32-47")
    parse_layers(layers)                                    # validated again in main()
    result["measure"] = dict(family=args.family, layers=layers, socket=str(socket_path),
                             helper_sha256=sha256_file(CUDA_HELPER),
                             reference_sha256=sha256_file(HIP_REFERENCE),
                             helper_device=0, reference_device=0,
                             reference_backend="gfx906-HIP")

    slots_window.stop_production(args.expected_supervisor, result)
    helper = None
    helper_log = paths["helper_log"].open("ab")
    try:
        env_clean = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                     "HOME": os.environ.get("HOME", "/home/user"), "TERM": "dumb"}
        helper = subprocess.Popen(helper_argv(CUDA_HELPER, socket_path), env=env_clean,
                                  stdout=helper_log, stderr=subprocess.STDOUT,
                                  start_new_session=True)
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            if socket_path.exists() and "listening" in paths["helper_log"].read_text(errors="replace"):
                break
            if helper.poll() is not None:
                raise RuntimeError(f"helper died rc={helper.returncode}")
            time.sleep(2)
        else:
            raise RuntimeError("helper socket/listening deadline expired")

        env_hip = reference_env()
        result["measure"]["reference_env"] = {k: env_hip[k] for k in
                                             ("LD_LIBRARY_PATH", "CUDA_VISIBLE_DEVICES",
                                              "HSA_OVERRIDE_GFX_VERSION", "HIP_FORCE_P2P",
                                              "HSA_ENABLE_SDMA")}
        with paths["reference_log"].open("wb") as rlog:
            ref = subprocess.run(reference_argv(out_json, socket_path, capdir, layers=layers),
                                 env=env_hip, stdout=rlog,
                                 stderr=subprocess.STDOUT, timeout=args.window_timeout)
        result["measure"]["reference_rc"] = ref.returncode
        if ref.returncode != 0 or not out_json.exists():
            raise RuntimeError(f"reference harness rc={ref.returncode}")
        result["measure"]["report_sha256"] = sha256_file(out_json)
        audit = subprocess.run([sys.executable, str(AUDIT), str(out_json)],
                               capture_output=True, text=True, timeout=120)
        (paths["audit_log"]).write_bytes(audit.stdout.encode() + audit.stderr.encode())
        result["measure"]["audit_rc"] = audit.returncode
        print(audit.stdout, flush=True)
    finally:
        helper_log.close()
        if helper is not None and helper.poll() is None:
            try:
                os.killpg(helper.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                helper.wait(timeout=45)
            except subprocess.TimeoutExpired:
                os.killpg(helper.pid, signal.SIGKILL)
                helper.wait(timeout=15)
        token = pair.socket_token(socket_path) if socket_path.exists() else None
        if token is not None:
            pair.release_sockets({socket_path: token})
        restore_canonical(result, args.family)


def phase_longctx(args, result) -> None:
    """Real-world speed acceptance against the LIVE canonical production (the
    mixed system under test).  NO outage, NO restore: nothing is stopped.  The
    supervisor identity is verified before AND after; the verdict uses only the
    server-reported actual usage tokens."""
    ev = exclusive_dir(EV_ROOT / args.family)
    state = expect_production(args.expected_supervisor)
    with urllib.request.urlopen("http://127.0.0.1:8009/v1/models", timeout=30) as response:
        if response.status != 200:
            raise RuntimeError(f"models endpoint HTTP {response.status}")
    prompt, meta = longctx_prompt(args.ctx_tokens)
    body = longctx_request(prompt, args.out_tokens)
    (ev / "prompt.txt").write_text(prompt, encoding="utf-8")
    (ev / "request-meta.json").write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8")
    (ev / "request.json").write_bytes(body)
    result["longctx"] = dict(family=args.family, model=PROD_MODEL,
                             expected_supervisor=args.expected_supervisor,
                             supervisor=dict(supervisor_pid=state["supervisor_pid"],
                                             helper_pid=state.get("helper_pid"),
                                             server_pid=state.get("server_pid")),
                             prompt=meta, max_tokens=args.out_tokens)
    try:
        request = urllib.request.Request("http://127.0.0.1:8009/v1/chat/completions",
                                         data=body,
                                         headers={"Content-Type": "application/json"})
        started = time.monotonic()
        with urllib.request.urlopen(request, timeout=args.window_timeout) as response:
            raw = response.read()
            code = response.status
        wall_s = time.monotonic() - started
        (ev / "reply.json").write_bytes(raw)
        reply = json.loads(raw)
        content = (reply.get("choices") or [{}])[0].get("message", {}).get("content")
        verdict = longctx_verdict(code, reply.get("usage"), content)
        usage = reply.get("usage") or {}
        completion_tokens = verdict["completion_tokens"] or 0
        prompt_tokens = verdict["prompt_tokens"] or 0
        result["longctx"].update(
            http=code, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            total_tokens=usage.get("total_tokens"), wall_s=round(wall_s, 3),
            prompt_plus_output_tok_per_s=round((prompt_tokens + completion_tokens) / wall_s, 3)
            if prompt_tokens and completion_tokens else None,
            output_tok_per_s_wall=round(completion_tokens / wall_s, 3) if completion_tokens else None,
            verdict=verdict)
        if not verdict["passed"]:
            failed = [k for k, ok in verdict.items() if k in
                      ("http_ok", "prompt_tokens_ok", "completion_tokens_ok", "content_ok")
                      and not ok]
            raise RuntimeError(f"longctx acceptance failed: {failed}; verdict={verdict}")
        after = expect_production(args.expected_supervisor)      # still the same pair
        result["longctx"]["post_supervisor"] = dict(
            supervisor_pid=after["supervisor_pid"], state=after.get("state"))
        print(f"[longctx] prompt={prompt_tokens} completion={completion_tokens} "
              f"wall={wall_s:.1f}s", flush=True)
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode(errors="replace")[:4000]
        (ev / "reply-error.json").write_text(json.dumps(
            dict(http=exc.code, body=body_text), indent=1) + "\n", encoding="utf-8")
        raise RuntimeError(f"longctx request HTTP {exc.code}: {body_text[:200]}") from exc


PHASES = {"capture": phase_capture, "measure": phase_measure, "longctx": phase_longctx}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("phase", choices=("capture", "measure", "longctx", "capacity"))
    ap.add_argument("--family", required=True)
    ap.add_argument("--expected-supervisor", type=int, required=True,
                    help="live canonical supervisor pid (identity-checked)")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--engine", default=str(DIAG_ENGINE),
                    help="STRATA_P2_DIAGNOSTICS=ON engine for capture")
    ap.add_argument("--port", type=int, default=18085)
    ap.add_argument("--fixtures", default="all")
    ap.add_argument("--max-tokens", type=int, default=16)
    ap.add_argument("--load-timeout", type=int, default=900)
    ap.add_argument("--req-timeout", type=int, default=600)
    ap.add_argument("--window-timeout", type=int, default=7200)
    ap.add_argument("--layers", default="32-47",
                    help="layer window for the measure phase, e.g. 36-39")
    ap.add_argument("--report-name", default="measure.json",
                    help="measure.json or measure-L<A>-<B>.json (its own logs/result)")
    ap.add_argument("--ctx-tokens", type=int, default=80000,
                    help="longctx: target context size (actual usage is the verdict)")
    ap.add_argument("--out-tokens", type=int, default=6000,
                    help="longctx: requested generation cap; acceptance needs >=5000 actual")
    args = ap.parse_args()
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,20}", args.family):
        raise SystemExit("invalid family name")
    try:
        parse_layers(args.layers)
        window_paths(EV_ROOT / args.family, args.report_name)
    except ValueError as exc:
        raise SystemExit(f"invalid window: {exc}")
    if args.phase == "capacity":
        raise SystemExit("phase 'capacity' was dropped by the user (2026-11-02 18:10); "
                         "speed-side work only")
    if args.phase == "longctx":
        if args.ctx_tokens < LONGCTX_MIN_PROMPT:
            raise SystemExit(f"--ctx-tokens wants >= {LONGCTX_MIN_PROMPT} (acceptance floor)")
        if args.out_tokens <= LONGCTX_MIN_COMPLETION:
            raise SystemExit(f"--out-tokens wants > {LONGCTX_MIN_COMPLETION} so the "
                             f"actual >= {LONGCTX_MIN_COMPLETION} floor is requestable")
    plan = {"capture": "stop production; 8 fixture boots; 32-47 dumps; restore",
            "measure": "stop production; CUDA helper on 3080; gfx906 reference on VII 0; audit; restore",
            "longctx": "NO outage: identity-check the live supervisor; one thinking-off "
                       f"~{args.ctx_tokens}-token prompt with a {args.out_tokens}-token "
                       "generation request against canonical production; accept only on "
                       "actual usage tokens; verify the supervisor after"}
    if not args.execute:
        print(f"Plan [{args.phase}/{args.family}]: {plan[args.phase]} (--execute to run)")
        return 0
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    result = {"started": time.time(), "phase": args.phase, "family": args.family,
              "success": False}
    out = (EV_ROOT / args.family / "capture-result.json" if args.phase == "capture"
           else EV_ROOT / args.family / "longctx-result.json" if args.phase == "longctx"
           else window_paths(EV_ROOT / args.family, args.report_name)["result"])
    try:
        PHASES[args.phase](args, result)
        result["success"] = True
    except BaseException as exc:                       # retained, never hidden
        result["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        EV_ROOT / args.family / f"{args.phase}-result.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".new")
        tmp.write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8")
        tmp.replace(out)
        print(f"[{args.phase}] result -> {out} success={result.get('success')}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
