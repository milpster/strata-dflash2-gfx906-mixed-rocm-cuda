#!/usr/bin/env python3
"""Offline raw-token lifecycle adapter for the RAM-acceptance window: stdlib + siblings.

Import-safe and DEFAULT INACTIVE: importing this module, or running `prepare`/
`helper-command`/`preflight`/`--help`, starts nothing, opens no socket and imports NO
serving engine. The real `serve.server` (whose import reads STRATA_ENGINE_READY_S and
installs TLS/sys.path) and the destructive `end_leftover_engines`/`procs.end_orphans`
path are reached ONLY on an explicit, production-absence-gated `run`/`run --worker`
execution — never in dry-run, tests, or refusal paths.

Why raw: the settled decision is exact raw output token IDs + exact required state
hashes. HTTP text/log counts are NOT equivalent, so the real path embeds the actual
`serve.server.StrataEngine` (`popen("the Strata engine", [exe, "--serve", *args], ...)`,
`.generate(ids, max_new, sampling, cancel)` yields int token IDs and `None` heartbeats,
then `DONE` -> `.last`), driven through `engine_args(cfg, exe, announce=False)` and
`child_env(cfg)` as the source authority (not a hand mirror).

Ownership model (single, chosen): an UNSCOPED Python outer owner launches and owns the
expert helper (helper SETTINGS come from `build_helper_command` here), waits it ready,
then launches the engine in a SEPARATE `--worker` process wrapped in the canonical
`systemd-run --user --scope -p MemoryMin=48G` scope. The engine is closed in the worker's
`finally`; the outer owner terminates/waits the helper only AFTER the worker exits, so the
engine is always closed before the helper stops and a crash never leaks the pair. Helper is
therefore OUTSIDE the engine scope by construction (launch order + the scope wraps only the
worker), not by a manual operator step and not by attaching to a running HTTP engine.
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

_HERE = Path(__file__).resolve().parent
for _sibling in (str(_HERE), str(_HERE.parent)):
    if _sibling not in sys.path:
        sys.path.insert(0, _sibling)

# PP/TG/reused/read come from champions_log.parse_window, reused (not duplicated) via RA.
import ram_acceptance as RA  # noqa: E402  reuse config/provenance/token/state/guard (unchanged)

# --- invariants / status ------------------------------------------------------

ADAPTER_INACTIVE = True                       # default path performs no launch
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
# The preflight MUST see all of these probe names; an empty/missing probe set BLOCKS.
REQUIRED_PROBES = ("live_serving_processes", "supervisor_alive", "isolated_sockets_free")


class AdapterBlocked(Exception):
    """A preflight refused to proceed; raised instead of touching any real process."""


@dataclass(frozen=True)
class StopProof:
    """The canonical stop evidence: stop-all.sh exit 0 AND the ALL STOPPED verdict.

    Reused contract from champions_restore.sh:99-105. The adapter never RUNS stop-all; the
    parent's production-down window supplies this proof, and the preflight refuses without it.
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

    Mirrors the source-backed additions engine_args makes for the layer_split-30 tuple (the
    layer split + the two expert-helper sockets). The AUTHORITATIVE resolved argv is the real
    `engine_args(cfg, exe, announce=False)` on the execute path; this dry-run form is only a
    pre-check and is reconciled exactly against the real resolver output (see `run_worker`).
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


def build_helper_command(*, helper_bin: str, pack: str, experts: str, socket: str, socket2: str,
                         hlog: str) -> tuple[list[str], list[str]]:
    """Canonical expert-helper argv/env from launch-v0238.sh's helper block; only isolated
    paths/sockets parameterized. Device/slots/reserve/timeouts/fill-mib stay canonical so the
    test helper binds the same tuple the production helper does (but never a prod socket)."""
    if not str(helper_bin).strip():
        raise RA.ConfigRejected("helper binary identity required")
    if socket == socket2:
        raise RA.ConfigRejected("helper must bind two distinct sockets")
    argv = [str(helper_bin),
            "--pack", pack, "--experts", experts,
            "--device", "0", "--slots", "4600", "--reserve-mib", "156",
            "--timeout", "63115200000", "--begin-timeout", "63115200000",
            "--socket", socket, "--fill-mib", "7100", "--socket2", socket2]
    env = ["STRATA_HELPER_REPLY_TIMEOUT_MS=600000"]  # clean helper env; opt-ins stay unset (launcher 70-72)
    return argv, env


def prepare_plan(*, base_cfg: dict, arm: str, out_dir: Path, exe: str, socket1: str, socket2: str,
                 cache_dir: str, engine_log: str, helper_bin: str, pack: str, experts: str,
                 supplied_files: dict, port: int | None = None, memory_limit_mib: int | None = None,
                 env: dict | None = None) -> dict:
    """Build the dry-run plan: matched test config, expected argv, helper command, provenance.

    Performs NO launch and NO real-wrapper import. Returns status PREPARED with the GPU
    claim explicitly UNTESTED. A future cap comes from a frozen manifest only (none here).
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
        "dry_run_only": True,
        "gpu": GPU_STATUS,
        "adapter_inactive_by_default": ADAPTER_INACTIVE,
        "arm": arm,
        "exe": exe,
        "out_dir": str(out_dir),
        "test_config": test_cfg,
        "config_deltas": deltas,
        "expected_engine_argv": expected_argv,           # pre --serve; --serve added by engine
        "expected_helper_argv": helper_argv,
        "expected_helper_env": helper_env,
        "provenance": RA.provenance_manifest(supplied_files),
        "outstanding_gates": list(RA.OUTSTANDING_GATES),
    }


# =============================================================================
# D. SAFETY preflight (production-absence gate; read-only, never stops anything)
# =============================================================================

def production_absent(probes: dict) -> tuple[bool, list[str]]:
    """True only when every REQUIRED probe is supplied AND reports absence.

    A missing/empty required probe name, or a probe error, is treated as PRESENT (safe): the
    adapter refuses to import or start anything rather than assume an unproven absence. It
    NEVER stops production; a present production pair simply blocks.
    """
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


def default_absence_probes(*, sockets: list[str], supervisor_state: Path,
                           run_dir: Path) -> dict:
    """Read-only absence probes wired ONLY on the real execute path (never in tests).

    Reuses the same live identities stop-all.sh keys on: a pgrep sweep for serving processes
    (read-only, no kill), the supervisor state pid liveness, and the isolated sockets being
    free. It does not stop or signal anything; a caller that wants a stop runs stop-all.sh.
    """
    def live_serving_processes() -> int:
        import subprocess
        try:
            out = subprocess.run(["pgrep", "-f", PROD_IDENTITY_PATTERN], capture_output=True, text=True, timeout=5)
            return len([line for line in out.stdout.splitlines() if line.strip()])
        except (OSError, subprocess.SubprocessError):
            return -1                                   # cannot read -> production_absent treats as present

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

    _ = run_dir
    return {"live_serving_processes": live_serving_processes,
            "supervisor_alive": supervisor_alive,
            "isolated_sockets_free": isolated_sockets_free}


def assert_execute_allowed(*, confirm: bool, env: dict, stop_proof: StopProof | None,
                           probes: dict) -> None:
    """The only gate into the lazy real-import path: opt-in + stop proof + import env + absence.

    Order matters: a missing confirmation, a missing/dirty canonical stop proof (exit 0 AND
    ALL STOPPED), an unestablished import-time env, or any non-absent/missing required probe
    raises BEFORE anything is launched or imported.
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


# =============================================================================
# B/C. RAW ENGINE WORKER (the scoped side; tests inject fakes, never real exec)
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
    """Map the last `STATE_HASH L=` line's REQUIRED keys, using the actual field format.

    Absent line or any missing/empty required key yields {} (never an absent-key pass).
    """
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
    """EXACT this-arm argv check. Unlike RA._arg_differences (which lets the cache-dir VALUE
    differ for CROSS-ARM comparison), a this-arm observed argv must match the isolated
    cache-dir value too, or a production cache mis-wire could hide here."""
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
    state_hashes: dict = field(default_factory=dict)
    engine_log_bytes: bytes = b""
    problems: list[str] = field(default_factory=list)
    gpu: str = GPU_STATUS
    events: list[str] = field(default_factory=list)

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
               request_deadline_s: float = 1200.0, log_limit: int = 1 << 20) -> WorkerResult:
    """Drive the scoped engine worker over an injected handle factory; close in finally.

    `engine_factory(exe, args, env, log_path)` returns a handle exposing `.argv` (the actual
    `[exe, "--serve", *args]`), `.generate(ids, max_new, sampling, cancel)` (int IDs + `None`
    heartbeats), `.last`/`.info`, `.log_window()` and `.close()`. The engine is closed in the
    `finally` on EVERY path (argv drift never even constructs it). A stalled no-output generate
    is bounded by the request watchdog setting `cancel` (generate observes it) plus the engine's
    own silence watchdog — not by a post-yield clock check that a stall would never reach.
    """
    events = events if events is not None else []
    result = WorkerResult(status="PREPARED", generated_ids=[], events=events)

    drift = _argv_drift(args)
    if drift:
        result.status, result.problems = "FAIL", drift
        return result

    engine = None
    log_start = os.path.getsize(log_path) if log_path and os.path.exists(log_path) else 0
    cancel = threading.Event()
    fired = threading.Event()

    def _fire():
        fired.set()
        cancel.set()

    disarm = None
    try:
        events.append("engine_new")
        engine = engine_factory(exe, args, env, log_path)             # exactly one; real path is gated
        events.append("argv_check")
        argv_problems = exact_argv_problems(exe, args, list(getattr(engine, "argv", []) or []))
        if argv_problems:
            result.status, result.problems = "FAIL", argv_problems
            return result
        result.engine_argv = list(getattr(engine, "argv", []) or [])
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


# =============================================================================
# B. OUTER OWNER: unscoped helper + scoped engine worker (single path; tests inject seams)
# =============================================================================

def scoped_worker_argv(worker_argv: list[str]) -> list[str]:
    """Wrap the engine-worker invocation in the canonical engine scope (helper stays unscoped)."""
    return [*ENGINE_SCOPE, *worker_argv]


def build_worker_command(*, manifest_path: str | Path, fixture_path: str | Path, confirm: bool) -> list[str]:
    """The SCOPED engine worker's own argv (before the scope wrapper is added)."""
    worker = [sys.executable, str(_HERE / "ram_raw.py"), "run", "--worker",
              "--manifest", str(manifest_path), "--fixture", str(fixture_path)]
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


def run_owned(*, helper, worker_launcher, stop_proof: StopProof, confirm: bool, env: dict,
              probes: dict, events: list | None = None, helper_wait_s: float = 120.0) -> OwnedPairOutcome:
    """Unscoped outer owner: preflight -> launch+ready helper -> scoped worker -> close-before-terminate.

    `helper` is an injected seam (launch/ready/alive/terminate(timeout)/wait(timeout)); in
    production it is a real subprocess of the canonical helper command launched UNSCOPED, and
    `worker_launcher()` runs the scoped worker subprocess (which closes the engine in its own
    finally) and returns its WorkerResult. The helper is terminated/waited only after the worker
    returns (normal or crashed), so the engine is always closed first and a failure never leaks
    the pair. No real process/systemd is started here; that is the injected seams' job.
    """
    events = events if events is not None else []
    assert_execute_allowed(confirm=confirm, env=env, stop_proof=stop_proof, probes=probes)
    events.append("helper_launch")
    try:
        helper.launch()
        events.append("helper_wait_ready")
        if not helper.ready():
            events.append("helper_ready_failed")
            _own_cleanup(helper, events, helper_wait_s)
            return OwnedPairOutcome("FAIL", None, events, ["helper not ready before worker"])
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
# E. REAL SEAMS (constructed ONLY on a confirmed, gated execute; never in tests)
# =============================================================================

class RealEngineHandle:
    """Wraps a real `serve.server.StrataEngine` behind the seam `run_worker` consumes.

    Constructed ONLY by `default_engine_factory` on the gated execute path (which has already
    passed the preflight and established import-time env). Reads the per-request engine stderr
    window from the engine's own log file from the offset captured at construction.
    """
    def __init__(self, engine, log_path: str, argv: list[str], window_limit: int = 1 << 20):
        self._engine, self._log_path, self.argv = engine, log_path, argv
        self._window_limit = window_limit
        self._start = os.path.getsize(log_path) if log_path and os.path.exists(log_path) else 0

    @property
    def last(self):
        return self._engine.last

    @property
    def info(self):
        return self._engine.info

    @property
    def pid(self):
        return self._engine.proc.pid if getattr(self._engine, "proc", None) else None

    def generate(self, ids, max_new, sampling, cancel):
        return self._engine.generate(ids, max_new, sampling, cancel)

    def log_window(self):
        return log_window(self._log_path, self._start, self._window_limit)

    def close(self):
        self._engine.close()


def default_engine_factory(cfg: dict):
    """Return the REAL engine factory: lazy-imports serve.server, uses engine_args + child_env.

    Never called in tests. The caller must have run the preflight and established import-time
    env first; `announce=False` builds args without the drafting announcement side-note.
    """
    def build(exe: str, args_precheck: list[str], env: dict, log_path: str) -> RealEngineHandle:
        import server                                                    # noqa: PLC0415 - lazy, gated
        resolved = server.engine_args(cfg, exe, announce=False)          # SOURCE authority (not a mirror)
        child = server.child_env(cfg)                                    # config.env applied last by child_env
        engine = server.StrataEngine(exe, resolved, log=log_path, env=child)  # constructor adds --serve; end_orphans runs INSIDE, gated here
        return RealEngineHandle(engine, log_path, [exe, "--serve", *resolved])
    return build


class SubprocessHelper:
    """Real expert-helper handle launched UNSCOPED by the outer owner (never in tests)."""
    def __init__(self, argv: list[str], env: list[str], hlog: str, listening_probe):
        self._argv, self._env, self._hlog = argv, env, hlog
        self._listening = listening_probe
        self._proc = None

    def launch(self):
        import subprocess
        env = dict(e.split("=", 1) for e in self._env)
        log = open(self._hlog, "a", encoding="utf-8", errors="replace")     # engine-style stderr append
        self._proc = subprocess.Popen(self._argv, env={**os.environ, **env}, stderr=log)  # unscoped; no systemd-run

    def ready(self):
        return bool(self._listening())

    def alive(self):
        return self._proc is not None and self._proc.poll() is None

    def terminate(self):
        if self._proc is not None:
            self._proc.terminate()

    def wait(self, timeout: float = 120.0):
        if self._proc is not None:
            try:
                return self._proc.wait(timeout=timeout)
            except Exception:  # noqa: BLE001 - bounded wait; caller escalates
                self._proc.kill()
                return self._proc.wait(timeout=timeout)


# =============================================================================
# CLI (prepare/helper-command/preflight are pure; run/run --worker are gated and real)
# =============================================================================

def _build_from_manifest(manifest_path: str | Path) -> dict:
    """Read a frozen test manifest (paths/identity only) and build the dry-run plan.

    Pure: reads JSON + hashes supplied files; never launches a process or imports the serving
    engine. The base config is opened read-only; isolated paths come from here.
    """
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    base_cfg = json.loads(Path(manifest["base_config"]).read_text(encoding="utf-8"))
    return prepare_plan(
        base_cfg=base_cfg, arm=manifest["arm"], out_dir=Path(manifest["out_dir"]),
        exe=manifest["exe"], socket1=manifest["socket1"], socket2=manifest["socket2"],
        cache_dir=manifest["cache_dir"], engine_log=manifest["engine_log"],
        helper_bin=manifest["helper_bin"], pack=manifest["pack"], experts=manifest["experts"],
        supplied_files={role: Path(p) for role, p in (manifest.get("supplied_files") or {}).items()},
        port=manifest.get("port"), memory_limit_mib=manifest.get("memory_limit_mib"),
        env=manifest.get("env"))


def _load_stop_proof(path: str | None) -> StopProof | None:
    if not path or not Path(path).exists():
        return None
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    code = int(Path(path).with_suffix(".code").read_text().strip()) if Path(path).with_suffix(".code").exists() else 0
    return StopProof(exit_code=code, verdict_text=text)


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Offline RAM raw-token lifecycle adapter (dry-run by default).")
    sub = parser.add_subparsers(dest="cmd")
    p = sub.add_parser("prepare", help="dry-run plan from a frozen manifest (no launch, no server import)")
    p.add_argument("--manifest", required=True)
    p.add_argument("--write", action="store_true", help="also write <out_dir>/ram_raw_plan.json")
    hc = sub.add_parser("helper-command", help="print the canonical helper argv/env (single settings authority)")
    hc.add_argument("--manifest", required=True)
    sub.add_parser("check-env", help="report whether import-time env is established")
    pf = sub.add_parser("preflight", help="run the production-absence preflight (read-only; launches nothing)")
    pf.add_argument("--manifest", required=True)
    pf.add_argument("--stop-proof", required=True, help="path to a stop-all output that must read ALL STOPPED")
    run = sub.add_parser("run", help="gated execution path: needs stop proof + prod-down confirmation")
    run.add_argument("--manifest", required=True)
    run.add_argument("--fixture", required=True)
    run.add_argument("--stop-proof", required=True)
    run.add_argument("--confirm-production-down", action="store_true")
    run.add_argument("--worker", action="store_true", help="scoped engine-worker mode (invoked by the outer owner)")
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
                          ("status", "gpu", "dry_run_only", "adapter_inactive_by_default",
                           "expected_engine_argv", "expected_helper_argv")}, indent=2))
        return 0

    if command == "check-env":
        ok, missing = import_time_env_ok(dict(os.environ))
        print(f"import-time env ok={ok} missing={missing}")
        return 0 if ok else 3

    if command in ("preflight", "run"):
        # Every path here refuses BEFORE launching anything or importing serve.server.
        confirm = bool(getattr(args, "confirm_production_down", False))
        stop_proof = _load_stop_proof(getattr(args, "stop_proof", None))
        env = dict(os.environ)
        plan = _build_from_manifest(getattr(args, "manifest"))
        tc = plan["test_config"]
        probes = default_absence_probes(
            sockets=[tc["expert_helper_socket"], tc["expert_helper_socket2"]],
            supervisor_state=Path("/mnt/srcds/Strata/run/prod-swift15-f16kv-mixed-supervisor.json"),
            run_dir=Path("/mnt/srcds/Strata/run"))
        assert_execute_allowed(confirm=confirm, env=env, stop_proof=stop_proof, probes=probes)
        if command == "preflight":
            print("preflight: production absent; a real run would proceed")
            return 0
        if not getattr(args, "worker", False):
            raise AdapterBlocked("run (outer owner) wires the real unscoped helper + scoped worker here; this "
                                 "offline build refuses to launch a real pair — the parent's production-down "
                                 "window owns that.")
        raise AdapterBlocked("run --worker (scoped) wires the real serve.server StrataEngine here; this offline "
                             "build refuses to import/start a real engine.")

    print("ram_raw: dry-run/inactive only; real pair execution is gated and untested for GPU.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
