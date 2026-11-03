"""File-only derivation of a private, production-parity measurement pair."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
from types import SimpleNamespace

import helper_ab

WORK = Path(__file__).resolve().parents[2]
ROOT = Path("/mnt/user/Strata")
PROD_CONFIG = ROOT / "prod-config-swift15-dflash-w4-q4_0-mixed.json"
PROD_LAUNCHER = ROOT / "launch-prod-swift15-dflash-w4-q4_0-mixed.sh"
EVIDENCE = WORK / "build-mixed-gpu/cap1-validation"

# L6 config-knob sweep set: engine argv only, applied after arm derivation like the
# draft env override. VALUE knobs replace the one token after --NAME; FLAG knobs turn
# a bare flag on or off. Launcher-owned fields (sockets, slots, fill) stay untouchable.
KNOB_VALUE_ARGS = ("conversation-cache-mib", "conversation-cache-park-gain",
                   "conversation-cache-min-tokens", "pcie-frac", "kv-resident",
                   "vram-reserve-later-mib")
KNOB_FLAG_ARGS = ("expert-cache-cpu-order", "prompt-cache-spread", "dflash-remote-only")


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError(f"Expected exactly one launcher field: {old}")
    return text.replace(old, new)


def replace_regex_once(text, pattern, new):
    if len(re.findall(pattern, text)) != 1:
        raise ValueError(f"Expected exactly one launcher field: {pattern}")
    return re.sub(pattern, lambda _match: new, text, count=1)


def apply_tuning(config, tuning):
    """One named four-lever experiment on a PRIVATE config; no generic overrides."""
    if not isinstance(tuning, dict) or len(tuning) != 1:
        raise ValueError("Tuning requires exactly one approved lever")
    name, value = next(iter(tuning.items()))
    args = config["args"]
    if name == "stage_trim" and value is True:
        config.setdefault("env", {})["STRATA_STAGE_TRIM"] = "1"
    elif name == "dflash" and isinstance(value, (list, tuple)) and len(value) == 3:
        spec, block, floor = value
        if (type(spec) is not int or type(block) is not int or
                type(floor) not in (int, float) or not 2 <= spec <= 8 or
                not 1 <= block <= min(spec - 1, 7) or not 0 < floor <= 1):
            raise ValueError("DFlash tuning needs spec 2..8, block <= spec-1 and floor (0,1]")
        for flag, replacement in zip(("--spec", "--dflash-block", "--spec-min-p"), value):
            if args.count(flag) != 1:
                raise ValueError(f"Tuning requires exactly one {flag}")
            args[args.index(flag) + 1] = str(replacement)
    elif name == "prefill_pipe" and type(value) is int and value in (1, 3072, 6144):
        if "--prefill-pipe" in args:
            raise ValueError("Production already carries --prefill-pipe")
        args += ["--prefill-pipe", str(value)]
    elif name == "layer_split" and type(value) is int and value in (30, 34):
        if config.get("layer_split") != "32" or "--layer-split" in args:
            raise ValueError("Split tuning requires the explicit production split 32")
        config["layer_split"] = str(value)
    else:
        raise ValueError("Unsupported four-lever tuning")


def prepare(family, *, slots=4000, fill_mib=6400, port=18080, evidence=EVIDENCE,
            prod_config=PROD_CONFIG, prod_launcher=PROD_LAUNCHER,
            engine=None, helper=None, rpc_profile=False, draft_helper=None, knob=None,
            prefill_timing=False, helper_streams=False, draft_tap_bf16=False,
            split_overlap=False, helper_off=False, iq_mt_min=False, adapt_off=False,
            prompt_f32=False, first_logits=None, tuning=None):
    if tuning is not None and (knob is not None or engine is not None or helper is not None or
                               draft_helper is not None or rpc_profile or prefill_timing or
                               helper_streams or draft_tap_bf16 or split_overlap or helper_off):
        raise ValueError("Four-lever tuning cannot compose with other experiments")
    if prefill_timing is not None and type(prefill_timing) is not bool:
        raise ValueError("Prefill timing override must be True, False, or None")
    if helper_streams is not None and type(helper_streams) is not bool:
        raise ValueError("Helper-stream override must be True or False")
    if type(split_overlap) is not bool:
        raise ValueError("Split-overlap override must be True or False")
    if type(helper_off) is not bool:
        raise ValueError("Helper-off override must be True or False")
    if type(iq_mt_min) is not bool:
        raise ValueError("IQ-MT-MIN baseline override must be True or False")
    if type(adapt_off) is not bool:
        raise ValueError("Adaptive-off baseline override must be True or False")
    if type(prompt_f32) is not bool:
        raise ValueError("Prompt-F32 baseline override must be True or False")
    if first_logits is not None and not isinstance(first_logits, (str, Path)):
        raise ValueError("First-logits baseline needs a dump-path prefix or None")
    if helper_off and (draft_helper or draft_tap_bf16 or helper is not None):
        raise ValueError("Helper-off cannot combine with the remote draft lane or a helper binary")
    if helper_off and (rpc_profile or helper_streams):
        raise ValueError("Helper-off arms start no helper, so helper-side env injection has no seam")
    if type(draft_tap_bf16) is not bool:
        raise ValueError("Draft tap bf16 override must be True or False")
    if draft_tap_bf16 and not draft_helper:
        raise ValueError("The bf16 tap wire needs the remote draft lane (draft_helper=True)")
    if draft_helper is not None and type(draft_helper) is not bool:
        raise ValueError("Draft override must be True, False, or None")
    if knob is not None:
        if (not isinstance(knob, tuple) or len(knob) != 2 or
                knob[0] not in KNOB_VALUE_ARGS + KNOB_FLAG_ARGS or not str(knob[1]).strip()):
            raise ValueError("Knob must be (name, value) from the approved L6 sweep set")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,20}", family) or not 1 <= slots <= 4800:
        raise ValueError("Invalid window family or slots outside the approved sweep")
    if not 3072 <= fill_mib <= 7000:
        raise ValueError("Fill budget outside the approved probe range")
    if engine is not None:
        engine = Path(engine)
        if not engine.is_file() or not os.access(engine, os.X_OK):
            raise ValueError("Candidate engine binary is missing or not executable")
    if helper is not None:
        helper = Path(helper)
        if not helper.is_file() or not os.access(helper, os.X_OK):
            raise ValueError("Candidate helper binary is missing or not executable")
    if not 1024 <= port <= 65535 or port == 8009:
        raise ValueError("A private non-production port is required")
    prefix = Path(evidence) / family
    config_path = Path(f"{prefix}-config.json")
    launcher_path = Path(f"{prefix}-launch.sh")
    if config_path.exists() or launcher_path.exists():
        raise ValueError("Refusing to overwrite an earlier window; choose a fresh family")
    socket = Path(f"{prefix}.sock")
    socket2 = Path(f"{prefix}2.sock")
    if max(len(str(socket).encode()), len(str(socket2).encode())) >= 108:
        raise ValueError("Private socket path is too long")
    state_path = Path(f"{prefix}-supervisor.json")
    engine_log = Path(f"{prefix}-engine.log")
    prod = json.loads(Path(prod_config).read_text())
    if engine is not None and engine.resolve() == Path(prod["exe"]).resolve():
        raise ValueError("Candidate engine must differ from the live production binary")
    subs = dict(exe=str(engine) if engine is not None else prod["exe"], log=str(engine_log),
                port=port, cwd=str(WORK),
                model_name=prod["model_name"], model_name_off=prod["model_name"],
                socket=str(socket), socket2=str(socket2))
    config, off = helper_ab.derive_arms(prod, subs)
    failures = helper_ab.check_arm_pair(prod, config, off, subs)
    if failures:
        raise ValueError(f"Production parity failed: {failures}")
    if helper_off:
        # The user-queued helper-off arm: reuse helper_ab's VALIDATED OFF derivation
        # verbatim (both socket keys popped - "the shipped byte-identical default path:
        # the engine never creates the helper client"). check_arm_pair has already
        # accepted it; the --expert-helper-slots argv pair stays production's (inert
        # with no socket key to inject the connection).
        config = off
    args = config["args"]
    if not helper_off:
        if args.count("--expert-helper-slots") != 1:
            raise ValueError("Expected one explicit engine slot cap")
        args[args.index("--expert-helper-slots") + 1] = str(slots)
    if iq_mt_min:
        # Issue #152 identity probe: STRATA_IQ_MT_MIN=1 rides the config's env (the
        # server applies it to the engine, serve/server.py; DETAILS.md documents the
        # knob: with it every CPU IQ expert group uses the multi-token kernel, so an
        # expert's rows stop depending on how many window tokens share it). A BASELINE
        # on every arm - controls and candidates alike - so the window measures the
        # lever alone under drafting-independent CPU rounding. It composes with tuning
        # by design and is engine-side only (the CUDA helper has no CPU IQ kernels),
        # and an unset value keeps the shipped default byte-identical.
        config.setdefault("env", {})["STRATA_IQ_MT_MIN"] = "1"
    if adapt_off:
        # Adaptive-placement identity probe: --adapt-swaps 0 on EVERY arm freezes both
        # adaptive tiers at the deterministic initial fill (--peer-adapt-swaps defaults
        # to --adapt-swaps). The swap cadence is keyed to verify rounds with a
        # spec-dependent warm interval (generate.cpp's STRATA_SPEC_ADAPT_WARM/EVERY),
        # so draft timing changes the residency trajectory - which backend computes an
        # expert - and with it the rounding. The engine's own contract: with
        # --adapt-swaps 0 "the answer must match the restored one token for token".
        # A BASELINE like iq_mt_min: controls and candidates alike, composes with the
        # tuning by design; refused when production already carries the flag so a
        # baseline cannot silently duplicate or edit it.
        args = config["args"]
        if "--adapt-swaps" in args:
            raise ValueError("Adaptive-off baseline requires production argv without --adapt-swaps")
        args += ["--adapt-swaps", "0"]
    if prompt_f32:
        # Prompt-FP16 identity probe (Gate A, prefill-diagnostic-review.md §3): the private
        # config inherits production's env, which carries STRATA_HIP_PROMPT_F16=1 (engine
        # log "the prompt's 16-bit GEMMs run FP16 in and out ... not bit-identical to the
        # default"). Setting it to 0 on EVERY arm - controls and candidates alike - selects
        # the non-FP16 default prompt GEMMs (include/strata/prefill/gemm.hpp:121-123,
        # prompt_f16() overrides), i.e. it disables the prompt-FP16 override ONLY. It is not
        # a whole-engine FP32 claim, is engine-side only (a HIP-only key the CUDA helper never
        # reads), composes with the tuning like iq_mt_min, and an unset value leaves the
        # inherited production env byte-identical.
        config.setdefault("env", {})["STRATA_HIP_PROMPT_F16"] = "0"
    if first_logits is not None:
        # First-logits instrumentation baseline (parent-approved option A): every arm's
        # private config carries STRATA_DUMP_FIRST_LOGITS=<prefix>, so each engine writes
        # the first verify window's chosen logit row (raw f32, ver.vocab() floats, one
        # <prefix>.<n> file per request - generate.cpp's serve diagnostics, merged ?
        # T-1 : 0) for offline KL comparison of the prompt paths. A BASELINE on every arm
        # alike, so it composes like iq_mt_min; the dump's device sync perturbs
        # scheduling, so instrumented timings are never gain evidence.
        config.setdefault("env", {})["STRATA_DUMP_FIRST_LOGITS"] = str(first_logits)
    if knob is not None:
        # L6: engine argv only. Exactly one occurrence is enforced so a silent change
        # in the production argv cannot make the knob land somewhere unexpected.
        name, value = knob
        occurrences = args.count(f"--{name}")
        if name in KNOB_VALUE_ARGS:
            if occurrences != 1:
                raise ValueError(f"Value knob --{name} must appear exactly once in production argv")
            if str(value) in ("on", "off"):
                raise ValueError(f"Value knob --{name} needs NAME=<new-value>")
            args[args.index(f"--{name}") + 1] = str(value)
        elif value == "on":
            if occurrences:
                raise ValueError(f"Flag knob --{name} is already set in production argv")
            args.append(f"--{name}")
        elif value == "off":
            if occurrences != 1:
                raise ValueError(f"Flag knob --{name} must appear exactly once to be removable")
            del args[args.index(f"--{name}")]
        else:
            raise ValueError(f"Flag knob --{name} takes only on/off")
    if tuning is not None:
        apply_tuning(config, tuning)
    text = Path(prod_launcher).read_text()
    text = replace_once(text, 'CONFIG="$ROOT/prod-config-swift15-dflash-w4-q4_0-mixed.json"',
                        f"CONFIG={shlex.quote(str(config_path))}")
    text = replace_once(text, 'SOCKET="$ROOT/run/prod-mixed.sock"', f"SOCKET={shlex.quote(str(socket))}")
    text = replace_once(text, 'SOCKET2="$ROOT/run/prod-mixed2.sock"', f"SOCKET2={shlex.quote(str(socket2))}")
    text = replace_once(text, '--state "$ROOT/run/prod-mixed-supervisor.json"',
                        f"--state {shlex.quote(str(state_path))}")
    if helper_off:
        # Strip the helper start and its readiness gates (the launcher would otherwise
        # refuse on sockets nothing creates) and record helper_pid 0, the supervisor's
        # helper-less convention (the production launcher never passes 0, so the
        # canonical path is untouched; pair_supervisor then attaches on the server's
        # own "ready: http://" line instead of the helper's fill line).
        start_marker = 'env -i PATH="$PATH" HOME="$HOME"'
        end_marker = 'Second channel socket missing: $SOCKET2" >&2; exit 1; }\n'
        if text.count(start_marker) != 1 or text.count(end_marker) != 1:
            raise ValueError("Helper block markers not unique for the helper-off strip")
        start, end = text.index(start_marker), text.index(end_marker) + len(end_marker)
        if start >= end:
            raise ValueError("Helper block markers out of order")
        text = (text[:start]
                + "# helper-off arm: no RPC expert helper is started; the engine runs the\n"
                + "# shipped helper-less default path (helper_ab's OFF derivation)\n"
                + text[end:])
        text = replace_once(text, 'record "$helper_pid" "$server_pid"',
                            'record 0 "$server_pid"')
    else:
        text = replace_regex_once(text, r"--device 0 --slots \d+", f"--device 0 --slots {slots}")
        text = replace_regex_once(text, r"--fill-mib \d+ --socket2", f"--fill-mib {fill_mib} --socket2")
    if draft_helper is not None:
        # Explicit OFF cannot inherit an ambient remote-draft flag. None leaves
        # the existing derivation unchanged, including the production env.
        env = config.setdefault("env", {})
        env["STRATA_DRAFT_HELPER"] = "1" if draft_helper else "0"
        # l1w5 lesson: the draft lane's default deadline is max(--expert-helper-timeout-ms,
        # 2000 ms) - too tight for a kDraftCtx ingest that arrives on the channel the prefill
        # window tier is hammering (2048 rows/~105 MiB + the drafter forward missed 2 s).
        # A draft timeout closes the SHARED channel (req_id desync safety), which kills the
        # helper and the request (l1w5: 503). The remote_experts.cpp override is the knob;
        # 30 s covers contention spikes, only draft arms carry it.
        if draft_helper:
            env["STRATA_DRAFT_TIMEOUT_MS"] = "30000"
        if draft_tap_bf16 and draft_helper:
            # L8: engine-side env only - the sender's gate reads it once; the helper follows
            # the frame kind (kDraftCtxBf16/kDraftReqBf16), so no helper-side injection.
            env["STRATA_DRAFT_TAP_BF16"] = "1"
        if draft_helper:
            if args.count("--dflash") != 1 or args.count("--max-context") != 1:
                raise ValueError("Remote draft needs one artifact and context capacity")
            artifact = Path(args[args.index("--dflash") + 1])
            capacity = int(args[args.index("--max-context") + 1])
            if not artifact.is_file() or capacity < 64:
                raise ValueError("Remote draft artifact or context capacity unavailable")
            text = replace_once(text, f"--device 0 --slots {slots}",
                                f"--dflash {shlex.quote(str(artifact))} "
                                f"--dflash-window {capacity} --device 0 --slots {slots}")
    text = replace_once(text, "$ROOT/logs/prod-swift15-dflash-w4-q4_0-mixed-", f"$ROOT/logs/window-{family}-")
    if helper is not None:
        text = replace_once(text, 'HELPER="$WORK/build-mixed-gpu/rpcw-cuda/strata-expert-helper"',
                            f"HELPER={shlex.quote(str(helper))}")
    if engine is not None:
        # The executable preflight must check the binary the config will actually run.
        text = replace_once(text, '"$WORK/build-mixed-gpu/rpcw-hip/strata"', shlex.quote(str(engine)))
    exports = []
    if rpc_profile:
        exports.append("STRATA_RPC_PROFILE=1")
    if prefill_timing:
        # L7 (l1w3): STRATA_PREFILL_TIMING=1 turns on the PfTimer's per-chunk phase
        # spans in the engine log (prefill.cpp) - engine-side only, so unlike the RPC
        # profile it needs no env -i injection for the helper.
        exports.append("STRATA_PREFILL_TIMING=1")
    if split_overlap:
        # Lane 2 (the VII-alternation lever): STRATA_SPLIT_OVERLAP=1 overlaps the layer
        # split's per-window hand-off (generate.cpp reads it at serve start; the only
        # exclusion is beside --pipeline-windows, which the RPC helper scope forbids
        # anyway, generate.cpp's helper_scope_bad).  Engine-side only, so like the
        # prefill spans it needs no env -i for the helper; the OFF arms export nothing
        # and stay byte-identical to production, and the engine's startup line prints
        # "(overlapped: STRATA_SPLIT_OVERLAP=1)" so the ON arm self-documents.
        exports.append("STRATA_SPLIT_OVERLAP=1")
    if exports:
        # The export reaches the engine through the server (its env starts from
        # dict(os.environ), serve/server.py:3525); the helper needs its own injection
        # because the launcher scrubs it with env -i.
        text = replace_once(text, 'mkdir -p "$ROOT/run" "$ROOT/logs"',
                            f"export {' '.join(exports)}\nmkdir -p \"$ROOT/run\" \"$ROOT/logs\"")
    if rpc_profile:
        text = replace_once(text, 'env -i PATH="$PATH"', 'env -i STRATA_RPC_PROFILE=1 PATH="$PATH"')
    if helper_streams:
        # L2: HELPER-side only (the engine never reads STRATA_HELPER_STREAMS) - the same
        # env -i seam rpc_profile uses. The helper binary's own default is OFF, so an
        # unset env is exactly the pre-L2 single-mutex, single-stream behaviour; only an
        # ON arm's launcher carries the injection.
        text = replace_once(text, 'env -i PATH="$PATH"', 'env -i STRATA_HELPER_STREAMS=1 PATH="$PATH"')
    text = "# Generated private measurement launcher; production comments below are historical.\n" + text.replace("8009", str(port))
    Path(evidence).mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    launcher_path.write_text(text)
    subprocess.run(["bash", "-n", str(launcher_path)], check=True)
    manifest = dict(family=family, slots=slots, fill_mib=fill_mib, port=port,
                    parity_failures=failures, slot_override="engine argv and helper argv only",
                    engine=str(engine) if engine is not None else prod["exe"],
                    helper=str(helper) if helper is not None else "production",
                    rpc_profile=rpc_profile, draft_helper=draft_helper, prefill_timing=prefill_timing,
                    helper_streams=helper_streams, draft_tap_bf16=draft_tap_bf16,
                     split_overlap=split_overlap, helper_off=helper_off,
                     tuning=tuning, prompt_f32=prompt_f32,
                    knob=None if knob is None else dict(name=knob[0], value=str(knob[1])),
                    config=str(config_path), launcher=str(launcher_path),
                    socket=str(socket), socket2=str(socket2), state=str(state_path))
    Path(f"{prefix}-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return SimpleNamespace(**manifest, base_url=f"http://127.0.0.1:{port}")


# The preserved pre-helper stock product (what production ran before the mixed backend):
# the pinned dfsample-1 engine, the integration clone's own serve/server.py, and the stock
# config - no helper sockets, no --expert-helper-slots, no STRATA_PREFILL_HELPER.  A
# --stock-controls arm measures THAT product; how1/how2 measured the mixed engine without
# the helper instead.
STOCK_CLONE = Path("/mnt/user/dflash2/Strata-integ")
STOCK_EXE = STOCK_CLONE / "build-dfsample-1/strata"
STOCK_SERVER = STOCK_CLONE / "serve/server.py"
STOCK_CONFIG = ROOT / "prod-config-swift15-dflash-w4-q4_0.json"
STOCK_LAUNCHER = ROOT / "launch-prod-swift15-dflash-w4-q4_0.sh"


def stock_artifacts():
    """The four preserved pre-helper artifacts a stock-controls window must hash-pin."""
    return (STOCK_EXE, STOCK_SERVER, STOCK_CONFIG, STOCK_LAUNCHER)


def prepare_stock(family, *, port=18080, evidence=EVIDENCE, first_logits=None):
    """Derive a private, supervised launch of the PRE-HELPER stock product.

    Same private-artifact discipline as prepare(): a fresh family gets its own config
    (only "log"/"port" redirected, plus the optional first-logits baseline env), launcher
    and supervisor state; the preserved stock files are only ever READ.  The launcher is
    derived from the REAL stock launcher by strict once-only substitutions, so drift in
    the preserved script fails loudly instead of silently changing what runs.  Two
    mechanical differences are REQUIRED for ownership: the server starts in the
    launcher's own process group (no `setsid nohup` - pair_supervisor must own the tree)
    and the helper-less supervisor conventions are added (a --recover branch, `record 0`
    so attach keys on the server's own ready line, as in the helper-off derivation)."""
    if first_logits is not None and not isinstance(first_logits, (str, Path)):
        raise ValueError("First-logits baseline needs a dump-path prefix or None")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,20}", family):
        raise ValueError("Invalid window family")
    if not 1024 <= port <= 65535 or port == 8009:
        raise ValueError("A private non-production port is required")
    for path in stock_artifacts():
        if not path.is_file():
            raise ValueError(f"Preserved stock artifact missing: {path}")
    if not os.access(STOCK_EXE, os.X_OK):
        raise ValueError("Preserved stock engine is not executable")
    prefix = Path(evidence) / family
    config_path = Path(f"{prefix}-config.json")
    launcher_path = Path(f"{prefix}-launch.sh")
    if config_path.exists() or launcher_path.exists():
        raise ValueError("Refusing to overwrite an earlier window; choose a fresh family")
    state_path = Path(f"{prefix}-supervisor.json")
    engine_log = Path(f"{prefix}-engine.log")
    prod = json.loads(STOCK_CONFIG.read_text())
    args = prod["args"]
    value = lambda flag: args[args.index(flag) + 1]
    if (prod["exe"] != str(STOCK_EXE) or "--expert-helper-slots" in args
            or "expert_helper_socket" in prod or "expert_helper_socket2" in prod
            or "STRATA_PREFILL_HELPER" in prod.get("env", {})):
        raise ValueError("Stock config no longer describes the pre-helper product")
    if (value("--max-context") != "262144" or value("--gpu") != "0,1"
            or value("--prefill") != "8192" or prod.get("layer_split") != "32"):
        raise ValueError("Stock config drifted from the matched measurement settings")
    config = json.loads(STOCK_CONFIG.read_text())
    config["log"] = str(engine_log)
    config["port"] = port
    if first_logits is not None:
        config.setdefault("env", {})["STRATA_DUMP_FIRST_LOGITS"] = str(first_logits)
    text = STOCK_LAUNCHER.read_text()
    text = replace_once(text, 'CONFIG="$ROOT/prod-config-swift15-dflash-w4-q4_0.json"',
                        f"CONFIG={shlex.quote(str(config_path))}")
    text = replace_once(text, 'LOGDIR="$ROOT/logs"\n', 'LOGDIR="$ROOT/logs"\n\n'
                        + "if [[ ${1:-} == --recover ]]; then\n"
                        + f'  exec "$PY" {shlex.quote(str(WORK / "tools/mixed_gpu/pair_supervisor.py"))} run \\\n'
                        + f'    --launcher "$0" --state {shlex.quote(str(state_path))} \\\n'
                        + f'    --lock {shlex.quote(str(Path(f"{prefix}-supervisor.lock")))}\n'
                        + "fi\n")
    text = replace_regex_once(text, r"(?s)exec env -u CUDA_VISIBLE_DEVICES.*?< /dev/null\n",
        "env -u CUDA_VISIBLE_DEVICES -u STRATA_ARENA_MMAP -u GPU_PINNED_MIN_XFER_SIZE \\\n"
        "  -u HSA_USERPTR_FOR_PAGED_MEM \\\n"
        "  LD_LIBRARY_PATH=/home/user/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib \\\n"
        '  "$PY" serve/server.py \\\n'
        '  --engine strata --config "$CONFIG" --port 8009 \\\n'
        '  >> "$LOG" 2>&1 < /dev/null &\n'
        "server_pid=$!\n"
        'HLOG="${LOG%.log}-helper.log"\n'
        ': > "$HLOG"\n'
        "if [[ -n ${STRATA_PAIR_RECORD:-} ]]; then\n"
        f'  "$PY" {shlex.quote(str(WORK / "tools/mixed_gpu/pair_supervisor.py"))} \\\n'
        '    record 0 "$server_pid" "$LOG" "$HLOG"\n'
        "fi\n"
        'wait "$server_pid"\n')
    text = replace_once(text, "prod-swift15-dflash-w4-q4_0-$(date", f"window-{family}-$(date")
    text = "# Generated private stock-arm launcher; production comments below are historical.\n" + text.replace("8009", str(port))
    Path(evidence).mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    launcher_path.write_text(text)
    subprocess.run(["bash", "-n", str(launcher_path)], check=True)
    manifest = dict(family=family, port=port, kind="stock", engine=str(STOCK_EXE),
                    server=str(STOCK_SERVER), stock_config=str(STOCK_CONFIG),
                    stock_launcher=str(STOCK_LAUNCHER), config=str(config_path),
                    launcher=str(launcher_path), state=str(state_path),
                    socket=None, socket2=None, tuning=None,
                    first_logits=str(first_logits) if first_logits is not None else None)
    Path(f"{prefix}-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return SimpleNamespace(**manifest, base_url=f"http://127.0.0.1:{port}")
