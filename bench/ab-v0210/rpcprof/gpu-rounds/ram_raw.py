#!/usr/bin/env python3
"""Offline raw-token lifecycle adapter for the RAM-acceptance window: stdlib + siblings.

Import-safe and DEFAULT INACTIVE: importing this module, or running `prepare`/`--help`,
starts nothing, opens nothing and imports NO serving engine. The real `serve.server`
(whose import reads STRATA_ENGINE_READY_S and installs TLS/sys.path) and the destructive
`end_leftover_engines`/`procs.end_orphans` path are reached ONLY on an explicit,
production-absence-gated `execute` — never in dry-run, tests, or refusal paths.

Why raw: the settled decision is exact raw output token IDs + exact required state
hashes. HTTP text/log counts are NOT equivalent, so the future path embeds the actual
`StrataEngine` (which adds `--serve` itself and speaks `GEN <ids>` -> `T <id>`/`DONE`).

Ownership model (single): this adapter owns the canonical helper command/env and the
engine argv reconstruction. `ram_raw_launch.sh` is a thin delegator to `execute` and does
NOT re-derive settings, so there is one authority, not two competing launchers.
"""
from __future__ import annotations

# pyright: reportMissingImports=false
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _sibling in (str(_HERE), str(_HERE.parent)):
    if _sibling not in __import__("sys").path:
        __import__("sys").path.insert(0, _sibling)

# PP/TG/reused/read come from champions_log.parse_window, reused (not duplicated) via RA.
import ram_acceptance as RA  # noqa: E402  reuse config/provenance/token/state/guard (unchanged)

# --- invariants / status ------------------------------------------------------

ADAPTER_INACTIVE = True                       # default path performs no launch
GPU_STATUS = "UNTESTEDGPU"                    # any produced output manifest carries this
REQUIRED_IMPORT_ENV = ("STRATA_ENGINE_READY_S",)  # read at server.py import time
# The engine log's STATE_HASH fields, from the actual emission format (conversation_cache_parity).
REQUIRED_STATE_KEYS = ("L", "gdn", "ple", "tail", "dead", "pooled", "pooled_full", "kv", "ple_prev")
# Tokens that mean the resolved tuple drifted from the canonical mixed pair: recorded + abort.
FALLBACK_MARKERS = ("--mtp", "--draft-fallback", "--split-skip-if-fits", "--vram-elastic", "--batch")


class AdapterBlocked(Exception):
    """A preflight refused to proceed; raised instead of touching any real process."""


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
    """Deterministic reconstruction of the canonical mixed-pair engine argv (pre-`--serve`).

    Mirrors the source-backed additions engine_args makes for the layer_split-30 tuple
    (the layer split + the two expert-helper sockets) without the hardware detection the
    real resolver does. On the execute path the observed `engine_args` output is
    reconciled against this; any extra/fallback flag is recorded and aborts, not accepted.
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


def verify_observed_argv(exe: str, expected_args: list[str], observed_argv: list[str]) -> list[str]:
    """Reconcile the engine's actual popen argv against the expected resolved tuple.

    StrataEngine launches `[exe, "--serve", *engine_args]`; this compares that exact
    sequence (cache-dir VALUE slots excepted, reused from RA) so a resolved-tuple drift,
    an added fallback flag or a doubled --serve is recorded, never silently accepted.
    """
    expected_full = [exe, "--serve", *expected_args]
    return RA._arg_differences(expected_full, list(observed_argv))


def plan_engine_env_precedence(cfg: dict, base_env: dict) -> dict:
    """config.env LAST over an inherited base, for the precedence contract only.

    Mirrors child_env's precedence so dry-run/tests can prove cfg.env wins over launcher
    env; the execute path uses the real child_env (single authority for the actual spawn).
    """
    env = dict(base_env)
    for key, value in (cfg.get("env") or {}).items():
        env[str(key)] = str(value)
    return env


def import_time_env_ok(env: dict) -> tuple[bool, list[str]]:
    """The flags server.py reads at import time must already be present before lazy import."""
    missing = [name for name in REQUIRED_IMPORT_ENV if not env.get(name)]
    return (not missing), missing


# Helper command/env reconstructed from launch-v0238.sh's canonical helper block, with
# ONLY isolated paths parameterized (device/slots/fill/reserve/timeouts stay canonical).
def build_helper_command(*, helper_bin: str, pack: str, experts: str, socket: str, socket2: str,
                         hlog: str) -> tuple[list[str], list[str]]:
    if not str(helper_bin).strip():
        raise RA.ConfigRejected("helper binary identity required")
    if socket == socket2:
        raise RA.ConfigRejected("helper must bind two distinct sockets")
    argv = [str(helper_bin),
            "--pack", pack, "--experts", experts,
            "--device", "0", "--slots", "4600", "--reserve-mib", "156",
            "--timeout", "63115200000", "--begin-timeout", "63115200000",
            "--socket", socket, "--fill-mib", "7100", "--socket2", socket2]
    env = ["STRATA_HELPER_REPLY_TIMEOUT_MS=600000"]  # clean helper env; opt-ins added by the launcher
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
    """True only when every production-identity probe reports absence.

    `probes` maps names to zero-arg callables returning a truthy 'present' indicator
    (live production engine/helper identities, sockets, supervisor). The adapter MUST NOT
    stop production itself; a present production pair simply blocks. Injected in tests.
    """
    reasons = []
    for name, probe in probes.items():
        try:
            present = probe()
        except Exception as exc:  # noqa: BLE001 - a probe that cannot read is treated as present (safe)
            reasons.append(f"{name}: probe error {type(exc).__name__}; assume present")
            continue
        if present:
            reasons.append(f"{name}: production identity present")
    return (not reasons), reasons


def assert_execute_allowed(*, confirm: bool, env: dict, probes: dict) -> None:
    """The only gate into the lazy real-import path: explicit opt-in + import env + prod absent."""
    if not confirm:
        raise AdapterBlocked("execute needs an explicit production-down confirmation; refusing")
    ok, missing = import_time_env_ok(env)
    if not ok:
        raise AdapterBlocked(f"import-time env not established before lazy import: {missing}")
    absent, reasons = production_absent(probes)
    if not absent:
        raise AdapterBlocked(f"production not absent; refusing to import/start: {reasons}")


# =============================================================================
# B/C. RAW PAIR LIFECYCLE (parameterized seams; tests inject fakes, never real exec)
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


@dataclass
class PairResult:
    status: str
    generated_ids: list[int]
    engine_last: dict = field(default_factory=dict)
    engine_info: dict = field(default_factory=dict)
    state_hashes: dict = field(default_factory=dict)
    engine_log_bytes: bytes = b""
    problems: list[str] = field(default_factory=list)
    gpu: str = GPU_STATUS
    events: list[str] = field(default_factory=list)

    @property
    def prompt_passes(self) -> list:
        # PP/TG/reused/read over this request's engine byte window, reused via RA.
        return RA.prompt_passes(self.engine_log_bytes)


def parse_state_hashes(text: str, required_keys: tuple) -> dict:
    """Map the last `STATE_HASH L=` line's REQUIRED keys, using the actual field format.

    Absent line or any missing/empty required key yields {} (never an absent-key pass).
    """
    last = None
    for line in text.splitlines():
        if "STATE_HASH L=" in line:
            fields = dict(__import__("re").findall(r"(\w+)=([0-9a-fA-F,-]+)", line))
            last = fields
    if last is None:
        return {}
    if any(not last.get(key) for key in required_keys):
        return {}
    return {key: last[key] for key in required_keys}


def run_pair(*, exe: str, args: list[str], env: dict, log: str | None, fixture: PairFixture,
             engine_factory, helper, events: list | None = None,
             clock=time.monotonic, ready_deadline_s: float = 1.0, request_deadline_s: float = 1.0) -> PairResult:
    """Run exactly one owned engine/helper raw pair through injected seams; label UNTESTEDGPU.

    Order: helper launch -> helper ready -> exactly one engine (via engine_factory) ->
    generate raw IDs -> capture -> engine.close() -> helper.terminate() -> helper.wait().
    Any argv drift, factory error, deadline, EOF-without-DONE, nonfinite, missing state or
    ID mismatch aborts and closes the isolated arm before any caller could retry. It never
    imports server.py or starts a real process: that is the injected factory's job only on
    the gated execute path.
    """
    events = events if events is not None else []
    result = PairResult(status="PREPARED", generated_ids=[], events=events)

    # Refuse before touching any pair if the resolved argv drifted from the canonical tuple.
    drift = _argv_drift(args)
    if drift:
        result.status, result.problems = "FAIL", drift
        return result

    events.append("helper_launch")
    helper.launch()
    ready_at = clock() + ready_deadline_s
    while not helper.ready():
        if clock() >= ready_at:
            events.append("helper_ready_timeout")
            _cleanup(result, engine=None, helper=helper)
            result.status = "FAIL"
            result.problems.append("helper readiness deadline")
            return result
    if not helper.alive():
        events.append("helper_exited")
        _cleanup(result, engine=None, helper=helper)
        result.status, result.problems = "FAIL", ["helper exited before engine start"]
        return result
    events.append("helper_ready")

    engine = None
    try:
        events.append("engine_new")
        engine = engine_factory(exe, args, env, log)          # exactly one; real path is gated
        events.append("generate")
        cancel = threading.Event()
        started = clock()
        generated: list[int] = []
        for token in engine.generate(fixture.ids, fixture.max_new, fixture.sampling, cancel):
            if isinstance(token, int) and not isinstance(token, bool):
                generated.append(token)
            if clock() - started >= request_deadline_s:
                events.append("request_deadline")
                raise TimeoutError("request deadline exceeded")
        result.generated_ids = generated
        result.engine_last = dict(getattr(engine, "last", {}) or {})
        result.engine_info = dict(getattr(engine, "info", {}) or {})
        result.engine_log_bytes = _engine_log_bytes(engine)
        result.state_hashes = parse_state_hashes(
            result.engine_log_bytes.decode("utf-8", "replace"), tuple(fixture.required_state_keys))
        result.problems = _evaluate_result(result, fixture)
        result.status = "PASS" if not result.problems else "FAIL"
    except Exception as exc:  # noqa: BLE001 - any generation failure aborts the isolated arm
        result.status = "FAIL"
        result.problems.append(f"{type(exc).__name__}: {exc}")
    finally:
        _cleanup(result, engine=engine, helper=helper)
    return result


def _engine_log_bytes(engine) -> bytes:
    """The per-request engine byte window: the seam exposes bytes or text; never empty-pass."""
    reader = getattr(engine, "state_log_bytes", None)
    if callable(reader):
        raw = reader()
        return raw if isinstance(raw, bytes) else str(raw).encode("utf-8", "replace")
    text = getattr(engine, "state_log_text", None)
    return str(text()).encode("utf-8", "replace") if callable(text) else b""


def _cleanup(result: PairResult, *, engine, helper) -> None:
    if engine is not None:
        result.events.append("engine_close")
        try:
            engine.close()
        except Exception as exc:  # noqa: BLE001 - cleanup still tries the helper
            result.problems.append(f"engine.close: {type(exc).__name__}")
    result.events.append("helper_terminate")
    helper.terminate()
    result.events.append("helper_wait")
    helper.wait()


def _argv_drift(args: list[str]) -> list[str]:
    problems = []
    if "--serve" in args:
        problems.append("engine argv must not already carry --serve")
    present = [marker for marker in FALLBACK_MARKERS if marker in args]
    if present:
        problems.append(f"resolved argv carries fallback/auto-refit flags {present}; abort, not matched tuple")
    return problems


def _evaluate_result(result: PairResult, fixture: PairFixture) -> list[str]:
    problems: list[str] = []
    last = result.engine_last
    if not last or "finish" not in last or "generated" not in last:
        problems.append("incomplete/DONEless/EOF: engine reported no finish/generated")
    else:
        for key in ("prompt_ms", "decode_ms"):
            value = last.get(key)
            if isinstance(value, float) and (value != value or value == float("inf")):
                problems.append("nonfinite " + str(key))
    if not result.state_hashes:
        problems.append("missing/empty required state hashes")
    if fixture.expected_state is not None and result.state_hashes != fixture.expected_state:
        problems.append("state hash mismatch vs frozen expectation")
    if fixture.expected_output_ids is not None and result.generated_ids != fixture.expected_output_ids:
        problems.append("generated raw token IDs mismatch vs frozen expectation")
    return problems


# =============================================================================
# CLI (prepare/helper-command are pure; execute is guarded and never the default)
# =============================================================================

def _build_from_manifest(manifest_path: str | Path) -> dict:
    """Read a frozen test manifest (paths/identity only) and build the dry-run plan.

    Pure: reads JSON + hashes supplied files; never launches a process or imports the
    serving engine. The base config is opened read-only; isolated paths come from here.
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


def main(argv=None) -> int:
    import argparse
    import os
    parser = argparse.ArgumentParser(description="Offline RAM raw-token lifecycle adapter (dry-run by default).")
    sub = parser.add_subparsers(dest="cmd")
    p = sub.add_parser("prepare", help="dry-run plan from a frozen manifest (no launch, no server import)")
    p.add_argument("--manifest", required=True)
    p.add_argument("--write", action="store_true", help="also write <out_dir>/ram_raw_plan.json")
    hc = sub.add_parser("helper-command", help="print the canonical helper argv/env (single settings authority)")
    hc.add_argument("--manifest", required=True)
    sub.add_parser("check-env", help="report whether import-time env is established")
    ex = sub.add_parser("execute", help="gated real path: needs explicit prod-down confirmation")
    ex.add_argument("--confirm-production-down", action="store_true")
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
    if command == "execute":
        raise AdapterBlocked("execute refuses here: it imports serve.server and is reserved for the "
                             "parent's production-down window; this entrypoint never runs a real pair.")
    print("ram_raw: dry-run/inactive only; real pair execution is gated and untested for GPU.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
