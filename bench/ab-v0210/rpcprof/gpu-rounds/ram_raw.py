#!/usr/bin/env python3
"""Raw-token lifecycle adapter for the RAM-acceptance window: stdlib + siblings.

Wired, but its live edges (spawn, pgrep, /proc, `serve.server`) sit behind the single `Seams`
injection point so the real CLI callgraph — `preflight` / `run` / `run --worker` — is exercised
end-to-end against fakes and NEVER launches a real process, imports the real serving engine, or
issues anything to production. The default seams are the real implementations; the offline test
seams substitute them. Nothing here is executed by importing this module or by `prepare`.

Why raw: the settled decision is exact raw output token IDs + exact required state hashes. The
real path embeds the actual `serve.server.StrataEngine` (`popen("the Strata engine",
[exe, "--serve", *args], ...)`, `.generate(ids, max_new, sampling, cancel)` yields int token IDs
and `None` heartbeats then `DONE` -> `.last`), driven through `engine_args(cfg, exe,
announce=False)` and `child_env(cfg)` as the source authority (not a hand mirror).

Ownership (single, chosen): an UNSCOPED Python outer owner (`run`) launches and owns the expert
helper (`SubprocessHelper`, canonical `env -i` clean env, launched via the injected `popen`),
waits it ready, then launches the engine in a SEPARATE `--worker` process wrapped in the
canonical `systemd-run --user --scope -p MemoryMin=48G` scope and reads back its result JSON.
The worker owns exactly one `StrataEngine` and closes it in its own `finally` (server.close
STOP->QUIT->TERM->KILL; the engine is the worker's own child, tied by PR_SET_PDEATHSIG in
`serve/lifeline.py`); the outer owner terminates/kills the helper only AFTER the worker exits,
so the engine is always closed first and a crash never leaks the pair. Helper is OUTSIDE the
engine scope by construction (the scope wraps only the worker), never by attaching to a running
engine. The thin `ram_raw_launch.sh` only normalizes the canonical environment and execs `run`;
it does not launch, wait for, kill or scope anything itself.
"""
from __future__ import annotations

# pyright: reportMissingImports=false
import json
import math
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

_HERE = Path(__file__).resolve().parent
for _sibling in (str(_HERE), str(_HERE.parent)):
    if _sibling not in sys.path:
        sys.path.insert(0, _sibling)

# PP/TG/reused/read come from champions_log.parse_window, reused (not duplicated) via RA.
import ram_acceptance as RA  # noqa: E402  reuse config/provenance/token/state/guard (unchanged)

# --- invariants / status ------------------------------------------------------

ADAPTER_INACTIVE = True                       # importing/preparing launches nothing
GPU_STATUS = "UNTESTEDGPU"                    # any produced output manifest carries this
REQUIRED_IMPORT_ENV = ("STRATA_ENGINE_READY_S",)  # read at serve.server import time
# The engine log's STATE_HASH fields, from the actual emission format (conversation_cache_parity).
REQUIRED_STATE_KEYS = ("L", "gdn", "ple", "tail", "dead", "pooled", "pooled_full", "kv", "ple_prev")
# Tokens that mean the resolved tuple drifted from the canonical mixed pair: recorded + abort.
FALLBACK_MARKERS = ("--mtp", "--draft-fallback", "--split-skip-if-fits", "--vram-elastic", "--batch")
# The canonical scope the engine worker runs in (launch-v0238.sh: engine inside, helper outside).
ENGINE_SCOPE = ("systemd-run", "--user", "--scope", "-p", "MemoryMin=48G")
# The read-only production identity pattern stop-all.sh sweeps (stop-all.sh:24,37).
PROD_IDENTITY_PATTERN = "launch-prod-|launch-trial-|strata --serve|strata-expert-helper|serve/server[.]py --engine strata"
# serve dir inserted on sys.path before lazy-importing `server` (so its ROOT then finds strata_control).
DEFAULT_SERVE_DIR = "/mnt/srcds/Strata/v0215/serve"
# The preflight/worker MUST see all of these probe names; an empty/missing probe set BLOCKS.
REQUIRED_PROBES = ("live_serving_processes", "supervisor_alive", "isolated_sockets_free")
# Engine-scope env normalization from launch-v0238.sh:117-122 (cfg.env still applied last by child_env).
ENGINE_UNSET = ("CUDA_VISIBLE_DEVICES", "STRATA_ARENA_MMAP", "GPU_PINNED_MIN_XFER_SIZE",
                "HSA_USERPTR_FOR_PAGED_MEM", "LD_PRELOAD")
ENGINE_SET_DEFAULT = {"HSA_OVERRIDE_GFX_VERSION": "9.0.6", "GPU_MAX_ALLOC_PERCENT": "100",
                      "STRATA_WATCHDOG_S": "120", "STRATA_MEMORY_LIMIT_MIB": "51200",
                      "STRATA_ENGINE_READY_S": "2400"}


class AdapterBlocked(Exception):
    """A preflight refused to proceed; raised instead of touching any real process."""


@dataclass(frozen=True)
class StopProof:
    """The canonical stop evidence: stop-all.sh exit 0 AND the ALL STOPPED verdict.

    Reused contract from champions_restore.sh:99-105. The parent's stop command records the
    exit code in `<stop_proof>.code` (the sidecar) and the verdict in `<stop_proof>`; a missing
    sidecar or a nonzero/ALL-STOPPED-absent pair REFUSES (see `_load_stop_proof`).
    """
    exit_code: int
    verdict_text: str

    @property
    def is_clean(self) -> bool:
        return self.exit_code == 0 and "ALL STOPPED" in self.verdict_text


# =============================================================================
# A. CONFIG + PROVENANCE (dry-run; never launches or imports the real wrapper)
# =============================================================================

def _gpu_list(cfg: dict) -> list:
    return cfg.get("gpu") or []


def _layer_split_value(cfg: dict) -> str:
    return str(cfg.get("layer_split", "auto"))


def _layer_split_of(cfg: dict) -> bool:
    return len(_gpu_list(cfg)) > 1 and "--peer-device" not in cfg.get("args", [])


def build_engine_argv(cfg: dict, exe: str) -> list[str]:
    """DRY-RUN reconstruction of the canonical mixed-pair engine argv (pre-`--serve`).

    Mirrors the source-backed additions engine_args makes for the layer_split-30 tuple. On the
    execute path it is the frozen THIS-ARM expectation reconciled EXACTLY against the real
    `engine_args(cfg, exe, announce=False)` output BEFORE the StrataEngine constructor runs.
    """
    args = list(cfg["args"])
    if "--serve" in args:
        raise RA.ConfigRejected("--serve is added by StrataEngine; it must not be in config args")
    if _layer_split_of(cfg) and "--layer-split" not in args:
        args += ["--layer-split", _layer_split_value(cfg)]
    sock, sock2 = cfg.get("expert_helper_socket"), cfg.get("expert_helper_socket2")
    if isinstance(sock, str) and sock.strip() and "--expert-helper-socket" not in args:
        args += ["--expert-helper-socket", sock.strip()]
    if (isinstance(sock, str) and sock.strip() and isinstance(sock2, str) and sock2.strip()
            and sock2.strip() != sock.strip() and "--expert-helper-socket2" not in args):
        args += ["--expert-helper-socket2", sock2.strip()]
    return args


def plan_engine_env_precedence(cfg: dict, base_env: dict) -> dict:
    """config.env LAST over an inherited base, for the precedence contract only.

    Mirrors child_env's precedence for dry-run/tests; the real spawn env is `child_env(cfg)`.
    """
    env = dict(base_env)
    for key, value in (cfg.get("env") or {}).items():
        env[str(key)] = str(value)
    return env


def import_time_env_ok(env: dict) -> tuple[bool, list[str]]:
    """The flags server.py reads at import time must already be present before lazy import."""
    missing = [name for name in REQUIRED_IMPORT_ENV if not env.get(name)]
    return (not missing), missing


def clean_helper_env(base_env: dict) -> dict:
    """Helper env EXACTLY as launch-v0238.sh:89-92 (`env -i`, NOT a merge of the caller's env).

    A minimal base plus any explicitly opted-in helper profile knobs; the inherited
    STRATA_DF_*/DFLASH_*/DUMP_*/WINDOW_*/PREFILL_HELPER knobs are never carried through.
    """
    env = {"STRATA_HELPER_REPLY_TIMEOUT_MS": "600000",
           "PATH": base_env.get("PATH", ""), "HOME": base_env.get("HOME", ""),
           "TERM": base_env.get("TERM") or "dumb"}
    for key in ("STRATA_RPC_PROFILE", "STRATA_VERIFY_PROFILE", "STRATA_HELPER_REPLY_TIMEOUT_MS"):
        if base_env.get(key):
            env[key] = base_env[key]
    return env


def engine_scope_env(base_env: dict) -> dict:
    """Worker env: unset the 5 knobs and set the canonical engine vars (launch-v0238.sh:117-122).

    cfg.env is still applied LAST by child_env in the worker, so cfg.env (e.g. STRATA_WATCHDOG_S
    900) overrides these launch defaults; that override is a frozen recorded input, not changed.
    """
    env = dict(base_env)
    for key in ENGINE_UNSET:
        env.pop(key, None)
    for key, value in ENGINE_SET_DEFAULT.items():
        env[key] = value
    env.setdefault("LD_LIBRARY_PATH",
                   f"{base_env.get('STRATA_ROCM_ROOT', '/home/srcds/rocm-gfx906-xnack')}/lib:/opt/rocm-6.1.0/lib")
    return env


def build_helper_command(*, helper_bin: str, pack: str, experts: str, socket: str, socket2: str,
                         hlog: str) -> tuple[list[str], dict]:
    """Canonical expert-helper argv + the canonical CLEAN env spec (launch-v0238.sh:89-98)."""
    if not str(helper_bin).strip():
        raise RA.ConfigRejected("helper binary identity required")
    if socket == socket2:
        raise RA.ConfigRejected("helper must bind two distinct sockets")
    argv = [str(helper_bin),
            "--pack", pack, "--experts", experts,
            "--device", "0", "--slots", "4600", "--reserve-mib", "156",
            "--timeout", "63115200000", "--begin-timeout", "63115200000",
            "--socket", socket, "--fill-mib", "7100", "--socket2", socket2]
    return argv, clean_helper_env({})  # env is re-derived at launch from the real base via clean_helper_env


def prepare_plan(*, base_cfg: dict, arm: str, out_dir: Path, exe: str, socket1: str, socket2: str,
                 cache_dir: str, engine_log: str, helper_bin: str, pack: str, experts: str,
                 supplied_files: dict, port: int | None = None, memory_limit_mib: int | None = None,
                 env: dict | None = None, serve_dir: str = DEFAULT_SERVE_DIR) -> dict:
    """Build the dry-run plan: matched test config, expected argv, helper command, provenance.

    Performs NO launch and NO real-wrapper import. Returns status PREPARED with the GPU claim
    explicitly UNTESTED. A future cap comes from a frozen manifest only (none here). `serve_dir`
    is the search path the worker inserts before lazy-importing `server`.
    """
    if RA.min_free_floor_mib(base_cfg) != RA.FLOOR_MIB:
        raise RA.ConfigRejected("the base config's RAM floor is not the immutable 2560 MiB")
    test_cfg, deltas = RA.build_test_config(
        base_cfg, arm=arm, out_dir=out_dir, exe=exe, socket1=socket1, socket2=socket2,
        cache_dir=cache_dir, engine_log=engine_log, port=port, memory_limit_mib=memory_limit_mib, env=env)
    expected_argv = build_engine_argv(test_cfg, exe)
    helper_argv, helper_env = build_helper_command(
        helper_bin=helper_bin, pack=pack, experts=experts, socket=test_cfg["expert_helper_socket"],
        socket2=test_cfg["expert_helper_socket2"], hlog=str(Path(out_dir) / f"{arm}-helper.log"))
    return {
        "offline_preparation": RA.OFFLINE_PREPARATION,
        "status": "PREPARED",
        "gpu": GPU_STATUS,
        "adapter_inactive_by_default": ADAPTER_INACTIVE,
        "arm": arm,
        "exe": exe,
        "out_dir": str(out_dir),
        "serve_dir": serve_dir,
        "test_config": test_cfg,
        "config_deltas": deltas,
        "expected_engine_argv": expected_argv,           # pre --serve; --serve added by engine
        "expected_helper_argv": helper_argv,
        "expected_helper_env": helper_env,
        "provenance": RA.provenance_manifest(supplied_files),
        "outstanding_gates": list(RA.OUTSTANDING_GATES),
    }


# =============================================================================
# D. PHASE-CORRECT SAFETY (read-only; never stops anything)
# =============================================================================

def run_pgrep(pattern: str, run) -> tuple[str, list[str]]:
    """A single read-only pgrep sweep -> (status, pids). status: present(rc0)/absent(rc1)/error.

    rc0 -> present with parsed pids; rc1 -> absent; any other rc (or an error) -> 'error', which
    every absence consumer treats as PRESENT (BLOCK), never as absence.
    """
    try:
        out = run(["pgrep", "-f", pattern], capture_output=True, text=True, timeout=5)
    except Exception as exc:  # noqa: BLE001 - unreadable is never treated as absence
        return "error", [f"pgrep-error:{type(exc).__name__}"]
    rc = getattr(out, "returncode", None)
    if rc == 0:
        return "present", [p for p in out.stdout.split() if p.strip()]
    if rc == 1:
        return "absent", []
    return "error", [f"pgrep-rc{rc}"]


def phase_P_probes(*, sockets: list[str], supervisor_state: Path, pgrep, run) -> dict:
    """Phase-P (BEFORE the helper is launched): no serving procs, supervisor down, sockets FREE."""
    def live_serving_processes():
        status, pids = pgrep(PROD_IDENTITY_PATTERN, run)
        return pids if status != "absent" else []      # present or error -> truthy -> blocks

    def supervisor_alive() -> bool:
        if not supervisor_state.exists():
            return False
        try:
            pid = int(json.loads(supervisor_state.read_text(encoding="utf-8")).get("supervisor_pid") or 0)
        except (OSError, ValueError):
            return True                                 # unreadable state -> assume present (safe)
        if not pid:
            return False
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def isolated_sockets_free() -> list[str]:
        return [s for s in sockets if Path(s).exists() or Path(str(s) + ".lock").exists()]

    return {"live_serving_processes": live_serving_processes,
            "supervisor_alive": supervisor_alive,
            "isolated_sockets_free": isolated_sockets_free}


def production_absent(probes: dict) -> tuple[bool, list[str]]:
    """True only when every REQUIRED probe is supplied AND reports absence (a missing/empty
    required probe, or a probe error, is treated as PRESENT; it never stops production)."""
    reasons: list[str] = []
    for name in REQUIRED_PROBES:
        probe = probes.get(name)
        if probe is None or not callable(probe):
            reasons.append(str(name) + ": required probe missing; assume present")
            continue
        try:
            present = probe()
        except Exception as exc:  # noqa: BLE001 - a probe that cannot read is treated as present (safe)
            reasons.append(f"{name}: probe error {type(exc).__name__}; assume present")
            continue
        if present:
            reasons.append(str(name) + ": production identity present")
    return (not reasons), reasons


def preflight_gate(stop_proof: StopProof | None, probes: dict) -> tuple[bool, list[str]]:
    """`preflight`: canonical stop proof + phase-P absence only (NO confirm, NO import-env: it
    launches nothing and imports nothing, so it needs neither)."""
    reasons: list[str] = []
    if stop_proof is None or not stop_proof.is_clean:
        reasons.append(f"no clean canonical stop proof (exit 0 AND ALL STOPPED): {stop_proof!r}")
    absent, why = production_absent(probes)
    if not absent:
        reasons.extend(why)
    return (not reasons), reasons


def run_gate(*, confirm: bool, env: dict, stop_proof: StopProof | None, probes: dict) -> None:
    """`run` gate: explicit confirm + clean stop proof + import-time env + phase-P absence.

    Every failure raises BEFORE any spawn/import.
    """
    if not confirm:
        raise AdapterBlocked("run needs an explicit production-down confirmation; refusing")
    if stop_proof is None or not stop_proof.is_clean:
        raise AdapterBlocked(f"no clean canonical stop proof (exit 0 AND ALL STOPPED): {stop_proof!r}")
    ok, missing = import_time_env_ok(env)
    if not ok:
        raise AdapterBlocked(f"import-time env not established before lazy import: {missing}")
    absent, reasons = production_absent(probes)
    if not absent:
        raise AdapterBlocked(f"production not absent; refusing to import/start: {reasons}")


def worker_gate(*, confirm: bool, env: dict, stop_proof: StopProof | None,
                pgrep_status: str, serving_pids: list[str], owned_pid: int | None,
                self_pids: set[int], owned_identity_ok: bool,
                sockets_ok: bool) -> tuple[bool, list[str]]:
    """`run --worker` gate (AFTER the outer owner launched the owned helper, before the engine):
    confirm + stop proof + import env + BOTH isolated sockets are present + the owned-helper
    identity is positive + no serving process remains EXCEPT the verified owned helper and our
    own self/ancestor chain (an unowned matching helper/engine REFUSES)."""
    reasons: list[str] = []
    if not confirm:
        reasons.append("worker needs an explicit production-down confirmation")
    if stop_proof is None or not stop_proof.is_clean:
        reasons.append(f"no clean canonical stop proof: {stop_proof!r}")
    ok, missing = import_time_env_ok(env)
    if not ok:
        reasons.append(f"import-time env not established: {missing}")
    if pgrep_status == "error":
        reasons.append(f"pgrep unreadable ({serving_pids}); cannot prove absence")
    elif pgrep_status == "present":
        unowned = [p for p in serving_pids if p != str(owned_pid) and p not in {str(x) for x in self_pids}]
        if unowned:
            reasons.append(f"unowned serving identity present {unowned}; refusing")
    if not owned_identity_ok:
        reasons.append("owned-helper identity not positively proven (pid/exe/listening)")
    if not sockets_ok:
        reasons.append("both isolated sockets are not present as sockets under out_dir")
    return (not reasons), reasons


def _load_stop_proof(path: str | None) -> StopProof | None:
    """Require the recorded exit-code sidecar `<path>.code` AND an ALL STOPPED verdict.

    A missing proof file, a missing sidecar, a non-integer/nonzero exit code, or absent ALL
    STOPPED returns None (the caller refuses); it NEVER defaults the exit code to 0.
    """
    if not path:
        return None
    text_file, code_file = Path(path), Path(str(path) + ".code")   # append `.code`, never replace the suffix
    if not text_file.exists() or not code_file.exists():
        return None
    try:
        code = int(code_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return StopProof(exit_code=code, verdict_text=text_file.read_text(encoding="utf-8", errors="replace"))


# =============================================================================
# B/C. RAW ENGINE WORKER (the scoped side; injected seams only, never real exec in tests)
# =============================================================================

@dataclass
class PairFixture:
    """Frozen request inputs + separately frozen expected raw output (never derived from it)."""
    ids: list[int]
    max_new: int
    sampling: dict
    required_state_keys: tuple = REQUIRED_STATE_KEYS
    expected_output_ids: list[int] | None = None
    expected_state: dict | None = None


def load_fixture(path: str | Path) -> PairFixture:
    """Read a frozen request fixture; forwarding ids/max_new/sampling unchanged (no greedy)."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return PairFixture(ids=[int(t) for t in raw["ids"]], max_new=int(raw["max_new"]),
                       sampling=dict(raw.get("sampling") or {}),
                       expected_output_ids=([int(t) for t in raw["expected_output_ids"]]
                                            if raw.get("expected_output_ids") is not None else None),
                       expected_state=raw.get("expected_state"))


def parse_state_hashes(text: str, required_keys: tuple) -> dict:
    """Map the last `STATE_HASH L=` line's REQUIRED keys; absent line or any missing/empty key
    yields {} (never an absent-key pass)."""
    import re
    last = None
    for line in text.splitlines():
        if "STATE_HASH L=" in line:
            fields = dict(re.findall(r"(\w+)=([0-9a-fA-F,-]+)", line))
            last = fields
    if last is None:
        return {}
    if any(not last.get(key) for key in required_keys):
        return {}
    return {key: last[key] for key in required_keys}


def log_window(path: str | Path, start: int, limit: int = 1 << 20) -> bytes:
    """Bounded read of `limit` bytes from `path` starting at `start` (a per-request engine
    stderr window). Mirrors serve.server's bounded vision_log_tail read shape; never unbounded."""
    try:
        with open(path, "rb") as fh:
            fh.seek(start)
            return fh.read(max(0, int(limit)))
    except OSError:
        return b""


def exact_argv_problems(exe: str, expected_args: list[str], observed_argv: list[str]) -> list[str]:
    """EXACT this-arm argv check. Unlike RA._arg_differences (whose cache-dir wildcard is for
    CROSS-ARM comparison), a this-arm observed argv must match the isolated cache-dir value too,
    or a production cache mis-wire could hide here."""
    expected_full = [exe, "--serve", *expected_args]
    if expected_full == list(observed_argv):
        return []
    for index in range(min(len(expected_full), len(observed_argv))):
        if expected_full[index] != observed_argv[index]:
            return [f"observed argv differs at position {index}: {expected_full[index]!r} vs "
                    f"{observed_argv[index]!r} (this-arm argv must match exactly, cache-dir included)"]
    return [f"observed argv length differs: {len(expected_full)} vs {len(observed_argv)}"]


def _argv_drift(args: list[str]) -> list[str]:
    problems: list[str] = []
    if "--serve" in args:
        problems.append("engine argv must not already carry --serve")
    present = [marker for marker in FALLBACK_MARKERS if marker in args]
    if present:
        problems.append(f"resolved argv carries fallback/auto-refit flags {present}; abort, not matched tuple")
    return problems


def _exe_problems(expected_exe: str, observed_exe: str) -> list[str]:
    if not observed_exe:
        return []                                        # a reader with no exe fact is not a mismatch claim
    if os.path.realpath(expected_exe) == os.path.realpath(observed_exe):
        return []
    return [f"observed engine exe {observed_exe!r} is not the pinned exe {expected_exe!r}"]


def _finite_problems(last: dict) -> list[str]:
    problems: list[str] = []
    for key in ("prompt_ms", "decode_ms"):
        value = last.get(key)
        if isinstance(value, float) and not math.isfinite(value):
            problems.append("nonfinite " + str(key))
    return problems


@dataclass
class WorkerResult:
    status: str
    generated_ids: list[int]
    engine_argv: list[str] = field(default_factory=list)
    engine_last: dict = field(default_factory=dict)
    engine_info: dict = field(default_factory=dict)
    engine_pid: int | None = None
    engine_exe: str = ""
    state_hashes: dict = field(default_factory=dict)
    engine_log_bytes: bytes = b""
    problems: list[str] = field(default_factory=list)
    gpu: str = GPU_STATUS
    events: list[str] = field(default_factory=list)
    configured_watchdogs: dict = field(default_factory=dict)
    harness_fired: bool = False
    engine_exit: str = ""

    @property
    def prompt_passes(self) -> list:
        return RA.prompt_passes(self.engine_log_bytes)   # PP/TG via RA/champions_log.parse_window


def arm_timer_watchdog(seconds: float, on_fire):
    """Default request watchdog: a threading.Timer that fires `on_fire` (sets cancel) without
    sleeping the consumer thread. Returns a `disarm` callable. Tests inject their own so a
    stalled no-yield case fires deterministically with no sleeps."""
    timer = threading.Timer(seconds, on_fire)
    timer.daemon = True
    timer.start()
    return timer.cancel


def run_worker(*, exe: str, args: list[str], env: dict, log_path: str, fixture: PairFixture,
               engine_factory, events: list | None = None, watchdog=arm_timer_watchdog,
               request_deadline_s: float = 1200.0, log_limit: int = 1 << 20,
               configured_watchdogs: dict | None = None) -> WorkerResult:
    """Drive the scoped engine worker over an injected handle factory; close in finally.

    `engine_factory(exe, args, env, log_path)` returns a handle exposing `.argv` (the OBSERVED
    spawned argv from /proc), `.exe` (the observed exe), `.generate(ids, max_new, sampling,
    cancel)` (int IDs + `None` heartbeats), `.last`/`.info`, `.log_window()`, `.exit_code()` and
    `.close()`. The observed argv/exe are checked EXACTLY against the frozen this-arm expectation
    (cache-dir included). The engine is closed in the `finally` on EVERY path (argv/exe drift
    never even generates). A stalled no-output generate is bounded by the request watchdog
    setting `cancel` (generate observes it) plus the engine's own silence watchdog — recorded for
    attribution; neither limit is allowed to produce PASS when it fires.
    """
    events = events if events is not None else []
    result = WorkerResult(status="PREPARED", generated_ids=[], events=events,
                          configured_watchdogs=dict(configured_watchdogs or {}),
                          engine_exe="")

    drift = _argv_drift(args)
    if drift:
        result.status, result.problems = "FAIL", drift
        return result

    engine = None
    log_start = os.path.getsize(log_path) if log_path and os.path.exists(log_path) else 0
    cancel = threading.Event()
    fired = threading.Event()
    result.configured_watchdogs.setdefault("request_deadline_s", request_deadline_s)

    def _fire():
        fired.set()
        cancel.set()

    disarm = None
    try:
        events.append("engine_new")
        engine = engine_factory(exe, args, env, log_path)             # exactly one; real path is gated
        events.append("argv_check")
        observed = list(getattr(engine, "argv", []) or [])
        argv_problems = exact_argv_problems(exe, args, observed) + _exe_problems(exe, str(getattr(engine, "exe", "") or ""))
        if argv_problems:
            result.status, result.problems = "FAIL", argv_problems
            result.engine_argv = observed
            result.engine_exe = str(getattr(engine, "exe", "") or "")
            return result
        result.engine_argv = observed
        result.engine_exe = str(getattr(engine, "exe", "") or "")
        result.engine_pid = getattr(engine, "pid", None)
        events.append("generate")
        disarm = watchdog(request_deadline_s, _fire)
        generated: list[int] = []
        for token in engine.generate(fixture.ids, fixture.max_new, fixture.sampling, cancel):
            if isinstance(token, int) and not isinstance(token, bool):
                generated.append(token)
        result.generated_ids = generated
        result.engine_last = dict(getattr(engine, "last", {}) or {})
        result.engine_info = dict(getattr(engine, "info", {}) or {})
        result.engine_log_bytes = _read_window(engine, log_path, log_start, log_limit)
        result.state_hashes = parse_state_hashes(
            result.engine_log_bytes.decode("utf-8", "replace"), tuple(fixture.required_state_keys))
        exit_code = engine.exit_code() if callable(getattr(engine, "exit_code", None)) else None
        result.engine_exit = "" if exit_code is None else str(exit_code)
        result.harness_fired = fired.is_set()
        if fired.is_set():
            result.problems.append("request deadline: watchdog cancelled a stalled/no-output generate")
        result.problems = _evaluate_worker(result, fixture)
        result.status = "PASS" if not result.problems else "FAIL"
    except Exception as exc:  # noqa: BLE001 - any generation failure aborts the isolated arm
        result.status = "FAIL"
        result.problems.append(f"{type(exc).__name__}: {exc}")
    finally:
        if disarm is not None:
            try:
                disarm()
            except Exception:  # noqa: BLE001 - disarm must not mask the real outcome
                pass
        if engine is not None:
            events.append("engine_close")
            try:
                engine.close()                                        # bounded STOP/QUIT/TERM/KILL (server.close)
            except Exception as exc:  # noqa: BLE001 - cleanup still records
                result.problems.append(f"engine.close: {type(exc).__name__}")
    return result


def _read_window(engine, log_path: str, start: int, limit: int) -> bytes:
    """Prefer the real engine's log handle (it knows its own start offset); else the file."""
    reader = getattr(engine, "log_window", None)
    if callable(reader):
        raw = reader()
        return raw if isinstance(raw, bytes) else str(raw).encode("utf-8", "replace")
    return log_window(log_path, start, limit)


def _evaluate_worker(result: WorkerResult, fixture: PairFixture) -> list[str]:
    problems: list[str] = []
    last = result.engine_last
    if not last or "finish" not in last or "generated" not in last:
        problems.append("incomplete/DONEless/EOF: engine reported no finish/generated")
    else:
        problems += _finite_problems(last)
    if not result.state_hashes:
        problems.append("missing/empty required state hashes")
    if fixture.expected_state is not None and result.state_hashes != fixture.expected_state:
        problems.append("state hash mismatch vs frozen expectation")
    if fixture.expected_output_ids is not None and result.generated_ids != fixture.expected_output_ids:
        problems.append("generated raw token IDs mismatch vs frozen expectation")
    return list(dict.fromkeys(problems + result.problems))


def worker_result_paths(out_dir: str | Path, arm: str) -> tuple[Path, Path]:
    out = Path(out_dir)
    return out / f"{arm}.worker.result.json", out / f"{arm}.worker.window.bin"


def _default_read_json(out_dir: str | Path, arm: str) -> Path:
    return worker_result_paths(out_dir, arm)[0]


def write_worker_result(result: WorkerResult, out_dir: str | Path, arm: str) -> Path:
    """Persist the worker outcome + the byte window to a separate file (never an env/secret dump)."""
    json_path, window_path = worker_result_paths(out_dir, arm)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    window_path.write_bytes(result.engine_log_bytes)
    json_path.write_text(json.dumps({
        "status": result.status, "generated_ids": result.generated_ids, "engine_argv": result.engine_argv,
        "engine_exe": result.engine_exe, "engine_pid": result.engine_pid, "engine_last": result.engine_last,
        "engine_info": result.engine_info, "state_hashes": result.state_hashes, "problems": result.problems,
        "gpu": result.gpu, "events": result.events, "configured_watchdogs": result.configured_watchdogs,
        "harness_fired": result.harness_fired, "engine_exit": result.engine_exit,
        "window_file": str(window_path), "window_bytes": len(result.engine_log_bytes),
    }, indent=2), encoding="utf-8")
    return json_path


def read_worker_result(json_path: str | Path) -> WorkerResult:
    """Read a worker's persisted result; a missing/malformed file raises (the outer FAILs, never PASS)."""
    raw = json.loads(Path(json_path).read_text(encoding="utf-8"))
    window_path = Path(raw.get("window_file") or "")
    window = window_path.read_bytes() if window_path.exists() else b""
    return WorkerResult(status=raw["status"], generated_ids=list(raw.get("generated_ids") or []),
                        engine_argv=list(raw.get("engine_argv") or []), engine_last=dict(raw.get("engine_last") or {}),
                        engine_info=dict(raw.get("engine_info") or {}), engine_pid=raw.get("engine_pid"),
                        engine_exe=str(raw.get("engine_exe") or ""), state_hashes=dict(raw.get("state_hashes") or {}),
                        engine_log_bytes=window, problems=list(raw.get("problems") or []), gpu=raw.get("gpu", GPU_STATUS),
                        events=list(raw.get("events") or []),
                        configured_watchdogs=dict(raw.get("configured_watchdogs") or {}),
                        harness_fired=bool(raw.get("harness_fired")), engine_exit=str(raw.get("engine_exit") or ""))


# =============================================================================
# B. OUTER OWNER: unscoped helper + scoped engine worker (single path; seams inject effects)
# =============================================================================

def scoped_worker_argv(worker_argv: list[str]) -> list[str]:
    """Wrap the engine-worker invocation in the canonical engine scope (helper stays unscoped)."""
    return [*ENGINE_SCOPE, *worker_argv]


def build_worker_command(*, manifest_path: str | Path, fixture_path: str | Path, stop_proof: str | None,
                         owned_helper_pid: int | None, confirm: bool, python: str | None = None,
                         owned_helper_start: int | None = None) -> list[str]:
    """The SCOPED engine worker's own argv (before the scope wrapper is added)."""
    worker = [python or sys.executable, str(_HERE / "ram_raw.py"), "run", "--worker",
              "--manifest", str(manifest_path), "--fixture", str(fixture_path),
              "--stop-proof", str(stop_proof)]
    if owned_helper_pid is not None:
        worker += ["--owned-helper-pid", str(owned_helper_pid)]
    if owned_helper_start is not None:
        worker += ["--owned-helper-start", str(owned_helper_start)]
    if confirm:
        worker.append("--confirm-production-down")
    return worker


@dataclass
class OwnedPairOutcome:
    status: str
    worker: WorkerResult | None
    events: list[str]
    problems: list[str] = field(default_factory=list)
    gpu: str = GPU_STATUS


def run_owned(*, helper, worker_launcher, events: list | None = None,
              ready_deadline_s: float = 600.0, helper_wait_s: float = 120.0,
              clock=time.monotonic, sleep=time.sleep) -> OwnedPairOutcome:
    """Unscoped outer owner: launch+ready helper -> scoped worker -> engine-close-before-terminate.

    The gate is applied by the CALLER (the CLI `run`) BEFORE this runs, so this function does not
    re-gate; it owns the owned resources only. `helper` exposes launch()/wait_ready(deadline,
    clock, sleep)/alive()/terminate()/wait(timeout); `worker_launcher()` runs the scoped worker
    (which closes its engine in its own finally) and returns its WorkerResult. The helper is
    terminated/waited only after the worker returns (normal or crashed), so the engine is always
    closed first and a failure never leaks the pair. No real process/systemd here; that is the
    injected seams' job.
    """
    events = events if events is not None else []
    events.append("helper_launch")
    try:
        helper.launch()
        events.append("helper_wait_ready")
        if not helper.wait_ready(ready_deadline_s, clock=clock, sleep=sleep):
            events.append("helper_ready_failed")
            _own_cleanup(helper, events, helper_wait_s)
            return OwnedPairOutcome("FAIL", None, events, ["helper never ready (listening/both sockets/alive)"])
        if not helper.alive():
            events.append("helper_exited")
            _own_cleanup(helper, events, helper_wait_s)
            return OwnedPairOutcome("FAIL", None, events, ["helper exited before worker"])
        events.append("worker_launch")
        try:
            worker = worker_launcher()                                  # scoped; engine closed in its finally
        except Exception as exc:  # noqa: BLE001 - a worker crash must still tear down the owned helper
            _own_cleanup(helper, events, helper_wait_s)
            return OwnedPairOutcome("FAIL", None, events, [f"worker_launcher: {type(exc).__name__}: {exc}"])
        events.append("worker_done")
        _own_cleanup(helper, events, helper_wait_s)
        status = worker.status if worker is not None else "FAIL"
        return OwnedPairOutcome(status, worker, events, list(worker.problems) if worker else ["no worker result"])
    except Exception as exc:  # noqa: BLE001 - helper owned from launch; never leak it
        _own_cleanup(helper, events, helper_wait_s)
        return OwnedPairOutcome("FAIL", None, events, [f"owner: {type(exc).__name__}: {exc}"])


def _own_cleanup(helper, events: list, timeout: float) -> None:
    """Terminate then WAIT the owned helper (bounded), recording order; never touches unowned procs."""
    try:
        events.append("helper_terminate")
        helper.terminate()
    except Exception:  # noqa: BLE001 - still attempt the wait
        pass
    events.append("helper_wait")
    try:
        helper.wait(timeout=timeout)
    except TypeError:                                                    # seam without a timeout kwarg
        helper.wait()
    except Exception:  # noqa: BLE001 - bounded wait failed; caller decides
        pass


# =============================================================================
# E. REAL SEAMS (the production default effects; tests substitute each through `Seams`)
# =============================================================================

@dataclass(frozen=True)
class EngineDeps:
    """The real factory's external effects: lazy server import + the /proc identity reader."""
    server_import: Callable[[], Any]
    process_reader: Callable[[int], Any]


def real_server_import(serve_dir: str):
    """Production: put `serve_dir` on sys.path (server's own ROOT insert then finds strata_control),
    then import the real `server` module. NEVER called in tests (a fake import seam substitutes)."""
    if serve_dir not in sys.path:
        sys.path.insert(0, serve_dir)
    import server  # noqa: PLC0415, F401 - the real serving module, lazy and gated
    return server


def real_process_reader(pid: int) -> dict:
    """Production: read the spawned process identity from /proc (argv, exe). Injected fake in tests."""
    argv, exe = [], ""
    try:
        argv = [a for a in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\x00") if a]
        argv = [a.decode("utf-8", "replace") for a in argv]
    except OSError:
        pass
    try:
        exe = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        pass
    return {"argv": argv, "exe": exe}


class RealEngineHandle:
    """Wraps a real `serve.server.StrataEngine`; the OBSERVED argv/exe come from /proc, not from
    the resolver's own list (so a constructor/mis-wire is caught, not asserted tautologically)."""
    def __init__(self, engine, log_path: str, process_reader, window_limit: int = 1 << 20):
        self._engine, self._log_path = engine, log_path
        self._window_limit = window_limit
        self._start = os.path.getsize(log_path) if log_path and os.path.exists(log_path) else 0
        pid = getattr(engine.proc, "pid", None) if getattr(engine, "proc", None) else None
        self._pid = pid
        obs = process_reader(pid) if pid is not None else {"argv": [], "exe": ""}
        self.argv = list(obs.get("argv") or [])          # OBSERVED [exe, --serve, ...args] from /proc
        self.exe = str(obs.get("exe") or "")

    @property
    def last(self):
        return self._engine.last

    @property
    def info(self):
        return self._engine.info

    @property
    def pid(self):
        return self._pid

    def exit_code(self):
        return self._engine.proc.poll() if getattr(self._engine, "proc", None) else None

    def generate(self, ids, max_new, sampling, cancel):
        return self._engine.generate(ids, max_new, sampling, cancel)

    def log_window(self):
        return log_window(self._log_path, self._start, self._window_limit)

    def close(self):
        self._engine.close()


def default_engine_factory(cfg: dict, deps: EngineDeps):
    """The REAL engine factory: lazy-imports serve.server via deps, resolves engine_args/child_env,
    and checks drift against the frozen this-arm expectation BEFORE constructing StrataEngine (whose
    constructor runs the destructive end_orphans). A resolved-tuple drift aborts with ctor calls==0.
    """
    def build(exe: str, expected_args: list[str], env: dict, log_path: str) -> RealEngineHandle:
        server = deps.server_import()                                    # lazy, gated; fake in tests
        resolved = list(server.engine_args(cfg, exe, announce=False))    # SOURCE authority (not a mirror)
        drift = _argv_drift(resolved) + exact_argv_problems(exe, expected_args, [exe, "--serve", *resolved])
        if drift:                                                        # BEFORE the end_orphans constructor
            raise AdapterBlocked(f"resolved argv drift vs frozen this-arm tuple, refusing to construct: {drift}")
        child = server.child_env(cfg)                                    # cfg.env applied LAST by child_env
        cwd = cfg.get("cwd")
        engine = server.StrataEngine(exe, resolved, cwd=cwd, log=log_path, env=child)  # ctor adds --serve + end_orphans
        return RealEngineHandle(engine, log_path, deps.process_reader)   # observed argv/exe read from /proc
    return build


class SubprocessHelper:
    """The real expert-helper handle launched UNSCOPED, via the injected popen + clean env.

    Ready = the owned helper's `listening` line in its OWN new log window AND both isolated
    sockets present AND the owned process alive, polled until an explicit deadline; never a
    fall-through on 'process alive alone'.
    """
    def __init__(self, *, argv, env, hlog, sockets, popen, socket_probe, clock=time.monotonic, sleep=time.sleep):
        self._argv, self._env, self._hlog = argv, env, hlog
        self._sockets, self._popen, self._socket_probe = sockets, popen, socket_probe
        self._clock, self._sleep = clock, sleep
        self._proc, self._log_start, self._listening = None, 0, "expert-helper: listening on"

    @property
    def pid(self):
        return self._proc.pid if self._proc else None

    @property
    def start_time(self):  # real path only; tests use a fake helper that overrides this
        return _proc_start_ticks(self._proc.pid) if self._proc else None

    def launch(self):
        self._log_start = os.path.getsize(self._hlog) if os.path.exists(self._hlog) else 0
        log = open(self._hlog, "a", encoding="utf-8", errors="replace")
        self._proc = self._popen(self._argv, env=self._env, stderr=log, stdin="devnull")

    def _listening_seen(self) -> bool:
        try:
            with open(self._hlog, "rb") as fh:
                fh.seek(self._log_start)
                return self._listening in fh.read().decode("utf-8", "replace")
        except OSError:
            return False

    def wait_ready(self, deadline_s: float, *, clock, sleep) -> bool:
        deadline = clock() + deadline_s
        while clock() < deadline:
            if not self.alive():
                return False
            if self._listening_seen() and all(self._socket_probe(s) for s in self._sockets):
                return True
            sleep(0.5)
        return self.alive() and self._listening_seen() and all(self._socket_probe(s) for s in self._sockets)

    def alive(self):
        return self._proc is not None and self._proc.poll() is None

    def terminate(self):
        if self._proc is not None:
            self._proc.terminate()

    def wait(self, timeout: float = 120.0):
        if self._proc is None:
            return None
        try:
            return self._proc.wait(timeout=timeout)
        except Exception:  # noqa: BLE001 - escalate to a bounded kill
            self._proc.kill()
            return self._proc.wait(timeout=timeout)


def launch_scoped_worker(*, manifest: str, fixture_path: str, stop_proof: str, owned_pid: int | None,
                         confirm: bool, out_dir: str, arm: str, popen, read_json, clock=time.monotonic,
                         sleep=time.sleep, wait_s: float = 7200.0, owned_start: int | None = None) -> WorkerResult:
    """Launch the SCOPED engine worker subprocess, wait bounded, then read its persisted result JSON.

    A missing/malformed result or a nonzero worker exit FAILS (raises); it never reports PASS. The
    worker closes its engine in its own finally before exiting, so reading its result is safe.
    """
    argv = scoped_worker_argv(build_worker_command(
        manifest_path=manifest, fixture_path=fixture_path, stop_proof=stop_proof,
        owned_helper_pid=owned_pid, confirm=confirm, owned_helper_start=owned_start))
    proc = popen(argv, env=engine_scope_env(dict(os.environ)))
    deadline = clock() + wait_s
    while proc.poll() is None and clock() < deadline:
        sleep(0.5)
    rc = proc.poll()
    json_path = read_json(out_dir, arm)
    if not Path(json_path).exists():
        raise RuntimeError(f"worker produced no result JSON (exit {rc})")
    result = read_worker_result(json_path)
    if rc not in (0,) or result.status != "PASS":
        result.status = "FAIL"
    return result


# =============================================================================
# F. SEAMS + CLI (the single dependency-injection point the tests traverse)
# =============================================================================

def _default_popen(argv, *, env=None, stderr=None, stdin=None):  # pragma: no cover - real path
    import subprocess
    devnull = subprocess.DEVNULL if stdin == "devnull" else None
    return subprocess.Popen(list(argv), env=env, stderr=stderr, stdin=devnull)


def _default_run(cmd, **kw):  # pragma: no cover - real path
    import subprocess
    return subprocess.run(cmd, **kw)


def _default_socket_probe(path: str) -> bool:
    import stat
    try:
        return stat.S_ISSOCK(os.stat(path).st_mode)
    except OSError:
        return False


def _default_self_pids() -> set[int]:  # pragma: no cover - real path: this pid + ancestor chain
    pids, pid = set(), os.getpid()
    while pid and pid > 1:
        pids.add(pid)
        try:
            pid = int(Path(f"/proc/{pid}/stat").read_text().split(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    return pids


def _proc_start_ticks(pid: int) -> int:
    """The kernel start-time tick from /proc/<pid>/stat field 22 (PID-reuse guard), -1 if unreadable."""
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return -1


def _default_proc_identity(pid: int) -> dict:  # pragma: no cover - real path
    """Observed identity for the owned-helper proof: exe symlink + start-time tick (PID-reuse guard)."""
    exe = ""
    try:
        exe = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        pass
    return {"pid": pid, "exe": exe, "start": _proc_start_ticks(pid)}


def owned_helper_proof(*, owned_pid: int | None, owned_start: int | None, proc_identity,
                       helper_bin: str, out_dir: str, socket_probe, sockets: list[str]) -> list[str]:
    """EXACT positive proof the live owned helper is the one the outer owner launched (no match
    on basename/pid alone). Returns [] when proven; otherwise the reasons it is NOT proven:
    pid/start/exe must all line up with the launched helper and both isolated sockets must be
    live sockets under out_dir. An unowned or mismatched helper does not satisfy this."""
    if owned_pid is None or owned_start is None:
        return ["worker needs the outer owner's --owned-helper-pid AND --owned-helper-start"]
    ident = proc_identity(owned_pid) or {}
    exe, start = str(ident.get("exe") or ""), ident.get("start", None)
    reasons: list[str] = []
    if not exe or os.path.realpath(exe) != os.path.realpath(str(helper_bin)):
        reasons.append(f"owned pid {owned_pid} exe {exe!r} is not exactly the frozen helper {helper_bin!r}")
    if start is None or int(start) != int(owned_start):
        reasons.append(f"owned pid {owned_pid} start {start!r} does not match launched start {owned_start!r}")
    root = Path(out_dir).resolve()
    for sock in sockets:
        if not socket_probe(sock):
            reasons.append(f"isolated socket {sock} is not a live socket")
        elif not Path(sock).resolve().is_relative_to(root):
            reasons.append(f"isolated socket {sock} is not under out_dir {out_dir}")
    return reasons


@dataclass(frozen=True)
class Seams:
    """Every external effect of the live paths, defaulted to the real implementations and faked in
    tests, so `main` runs the EXACT callgraph while nothing real is spawned/imported/read."""
    popen: Callable[..., Any] = staticmethod(_default_popen)
    run: Callable[..., Any] = staticmethod(_default_run)
    pgrep: Callable[..., Any] = run_pgrep
    socket_probe: Callable[..., Any] = staticmethod(_default_socket_probe)
    process_reader: Callable[..., Any] = staticmethod(real_process_reader)
    proc_identity: Callable[..., Any] = staticmethod(_default_proc_identity)
    self_pids: Callable[..., Any] = staticmethod(_default_self_pids)
    server_import: Callable[[str], Any] = real_server_import                      # takes serve_dir; fake in tests
    engine_factory: Callable[..., Any] = default_engine_factory
    helper_factory: Callable[[dict], Any] | None = None                  # plan -> helper; fake in tests
    launch_worker: Callable[..., Any] = launch_scoped_worker
    read_json: Callable[..., Any] = staticmethod(_default_read_json)
    supervisor_state: Any = Path("/mnt/srcds/Strata/run/prod-swift15-f16kv-mixed-supervisor.json")


def _make_helper_factory(seams: Seams):
    def make(plan: dict) -> SubprocessHelper:
        out_dir = Path(plan["out_dir"])
        argv = list(plan["expected_helper_argv"])
        env = clean_helper_env(dict(os.environ))       # canonical `env -i` base, NOT a full-env merge
        hlog = str(out_dir / f"{plan['arm']}-helper.log")
        sockets = [plan["test_config"]["expert_helper_socket"], plan["test_config"]["expert_helper_socket2"]]
        return SubprocessHelper(argv=argv, env=env, hlog=hlog, sockets=sockets,
                                popen=seams.popen, socket_probe=seams.socket_probe)
    return make


def _build_from_manifest(manifest_path: str | Path, *, serve_dir_default: str = DEFAULT_SERVE_DIR) -> dict:
    """Read a frozen test manifest (paths/identity only) and build the dry-run plan (pure)."""
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    base_cfg = json.loads(Path(manifest["base_config"]).read_text(encoding="utf-8"))
    return prepare_plan(
        base_cfg=base_cfg, arm=manifest["arm"], out_dir=Path(manifest["out_dir"]),
        exe=manifest["exe"], socket1=manifest["socket1"], socket2=manifest["socket2"],
        cache_dir=manifest["cache_dir"], engine_log=manifest["engine_log"],
        helper_bin=manifest["helper_bin"], pack=manifest["pack"], experts=manifest["experts"],
        supplied_files={role: Path(p) for role, p in (manifest.get("supplied_files") or {}).items()},
        port=manifest.get("port"), memory_limit_mib=manifest.get("memory_limit_mib"),
        env=manifest.get("env"), serve_dir=manifest.get("serve_dir", serve_dir_default))


def main(argv=None, *, seams: Seams | None = None) -> int:
    import argparse
    seams = seams or Seams()
    parser = argparse.ArgumentParser(description="RAM raw-token lifecycle adapter (prepare is inactive; run is gated).")
    sub = parser.add_subparsers(dest="cmd")
    p = sub.add_parser("prepare", help="dry-run plan from a frozen manifest (no launch, no server import)")
    p.add_argument("--manifest", required=True)
    p.add_argument("--write", action="store_true", help="also write <out_dir>/ram_raw_plan.json")
    hc = sub.add_parser("helper-command", help="print the canonical helper argv/env (single settings authority)")
    hc.add_argument("--manifest", required=True)
    sub.add_parser("check-env", help="report whether import-time env is established")
    pf = sub.add_parser("preflight", help="read-only stop-proof + phase-P absence gate (launches nothing)")
    pf.add_argument("--manifest", required=True)
    pf.add_argument("--stop-proof", required=True, help="stop-all output; needs a <path>.code exit-code sidecar")
    run = sub.add_parser("run", help="outer owner: gated unscoped helper + scoped engine worker")
    run.add_argument("--manifest", required=True)
    run.add_argument("--fixture", required=True)
    run.add_argument("--stop-proof", required=True)
    run.add_argument("--confirm-production-down", action="store_true")
    run.add_argument("--worker", action="store_true", help="scoped engine-worker mode (invoked by the outer owner)")
    run.add_argument("--owned-helper-pid", type=int, default=None,
                     help="the outer owner's owned helper pid (worker only; the only allowed live identity)")
    run.add_argument("--owned-helper-start", type=int, default=None,
                     help="the outer owner's owned helper start tick (worker only; PID-reuse guard)")
    args = parser.parse_args(argv)
    command = getattr(args, "cmd", None)

    if command in ("prepare", "helper-command"):
        plan = _build_from_manifest(getattr(args, "manifest"))
        if command == "helper-command":
            print(json.dumps({"argv": plan["expected_helper_argv"], "env": plan["expected_helper_env"]}, indent=2))
            return 0
        if getattr(args, "write", False):
            Path(plan["out_dir"]).mkdir(parents=True, exist_ok=True)
            (Path(plan["out_dir"]) / "ram_raw_plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
        print(json.dumps({k: plan[k] for k in
                          ("status", "gpu", "adapter_inactive_by_default", "expected_engine_argv",
                           "expected_helper_argv")}, indent=2))
        return 0

    if command == "check-env":
        ok, missing = import_time_env_ok(dict(os.environ))
        print(f"import-time env ok={ok} missing={missing}")
        return 0 if ok else 3

    if command == "preflight":
        plan = _build_from_manifest(getattr(args, "manifest"))
        tc = plan["test_config"]
        probes = phase_P_probes(sockets=[tc["expert_helper_socket"], tc["expert_helper_socket2"]],
                                supervisor_state=seams.supervisor_state,
                                pgrep=seams.pgrep, run=seams.run)
        ok, reasons = preflight_gate(_load_stop_proof(getattr(args, "stop_proof")), probes)
        if not ok:
            print(json.dumps({"preflight": "REFUSE", "reasons": reasons}), file=sys.stderr)
            return 2
        print("preflight: stop proof clean and production absent; a run may proceed")
        return 0

    if command == "run" and getattr(args, "worker", False):
        return _cli_worker(args, plan_seams=seams)

    if command == "run":
        plan = _build_from_manifest(getattr(args, "manifest"))
        tc = plan["test_config"]
        probes = phase_P_probes(sockets=[tc["expert_helper_socket"], tc["expert_helper_socket2"]],
                                supervisor_state=seams.supervisor_state,
                                pgrep=seams.pgrep, run=seams.run)
        confirm = bool(getattr(args, "confirm_production_down", False))
        stop_proof = _load_stop_proof(getattr(args, "stop_proof"))
        run_gate(confirm=confirm, env=dict(os.environ), stop_proof=stop_proof, probes=probes)   # raises before spawn
        helper = (seams.helper_factory or _make_helper_factory(seams))(plan)
        outcome = run_owned(
            helper=helper,
            worker_launcher=lambda: launch_scoped_worker(
                manifest=getattr(args, "manifest"), fixture_path=getattr(args, "fixture"),
                stop_proof=getattr(args, "stop_proof"), owned_pid=helper.pid, confirm=confirm,
                out_dir=plan["out_dir"], arm=plan["arm"], popen=seams.popen, read_json=seams.read_json,
                owned_start=helper.start_time),
            ready_deadline_s=float(plan.get("ready_deadline_s", 600.0)))
        payload = {"status": outcome.status, "events": outcome.events, "problems": outcome.problems,
                   "worker": (None if outcome.worker is None else
                              {"status": outcome.worker.status, "generated_ids": outcome.worker.generated_ids,
                               "engine_argv": outcome.worker.engine_argv, "engine_exe": outcome.worker.engine_exe,
                               "engine_pid": outcome.worker.engine_pid, "state_hashes": outcome.worker.state_hashes,
                               "engine_last": outcome.worker.engine_last, "problems": outcome.worker.problems})}
        print(json.dumps(payload, indent=2))
        return 0 if outcome.status == "PASS" else 4

    print("ram_raw: importing/preparing launches nothing; run is the gated execution path.")
    return 0


def _cli_worker(args, *, plan_seams: Seams) -> int:
    """`run --worker`: gate (phase-correct), require owned-helper proof, then run the real worker."""
    plan = _build_from_manifest(getattr(args, "manifest"))
    tc = plan["test_config"]
    out_dir, arm = plan["out_dir"], plan["arm"]
    owned_pid = getattr(args, "owned_helper_pid", None)
    owned_start = getattr(args, "owned_helper_start", None)
    status, pids = plan_seams.pgrep(PROD_IDENTITY_PATTERN, plan_seams.run)
    self_pids = set(plan_seams.self_pids())
    sockets = [tc["expert_helper_socket"], tc["expert_helper_socket2"]]
    proof = owned_helper_proof(owned_pid=owned_pid, owned_start=owned_start,
                               proc_identity=plan_seams.proc_identity,
                               helper_bin=str(plan["expected_helper_argv"][0]), out_dir=out_dir,
                               socket_probe=plan_seams.socket_probe, sockets=sockets)
    owned_identity_ok = not proof
    sockets_ok = all(plan_seams.socket_probe(s) for s in sockets)
    ok, reasons = worker_gate(confirm=bool(getattr(args, "confirm_production_down", False)),
                              env=dict(os.environ), stop_proof=_load_stop_proof(getattr(args, "stop_proof")),
                              pgrep_status=status, serving_pids=pids, owned_pid=owned_pid,
                              self_pids=self_pids, owned_identity_ok=owned_identity_ok, sockets_ok=sockets_ok)
    if proof:
        reasons = reasons + proof
    if not ok:
        print(json.dumps({"worker": "REFUSE", "reasons": reasons}), file=sys.stderr)
        return 2
    fixture = load_fixture(getattr(args, "fixture"))
    deps = EngineDeps(server_import=lambda: plan_seams.server_import(plan["serve_dir"]),
                      process_reader=plan_seams.process_reader)
    factory = plan_seams.engine_factory(tc, deps)
    result = run_worker(exe=plan["exe"], args=plan["expected_engine_argv"], env={}, log_path=tc["log"],
                        fixture=fixture, engine_factory=factory)
    try:
        write_worker_result(result, out_dir, arm)
    except OSError as exc:  # an output-write failure is a FAIL, after the engine is already closed
        print(json.dumps({"worker": "FAIL", "reasons": [f"result write: {type(exc).__name__}"]}), file=sys.stderr)
        return 4
    print(json.dumps({"status": result.status, "generated_ids": result.generated_ids,
                      "engine_pid": result.engine_pid, "engine_exe": result.engine_exe,
                      "state_hashes": result.state_hashes, "problems": result.problems}, indent=2))
    return 0 if result.status == "PASS" else 4


if __name__ == "__main__":
    raise SystemExit(main())
