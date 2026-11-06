#!/usr/bin/env python3
"""Hardware-window DRIVER for the mixed HIP/CUDA acceptance runs - RAW EVIDENCE ONLY.
import os

Implements .slim/deepwork/mixed-cuda-finish/r3-runtime-plan.md (read it for the full contract):
fresh OFF/ON/ON/OFF arms on loopback 8019, fresh ON chains for the prefill and decode faults,
one post-death normal request per fault, ALWAYS a canonical restore through the unchanged
production launcher.  It records what happened; it never decides PASS, phase or speed.

Default = PLAN: derived configs, diffs, build ids, pinned requests and argv into a FRESH run
dir, nothing else - no signals, network, /proc inventory, process start or GPU exec.
--execute requires the exact expected wrapper PID + /proc starttime AND the canonical --config
path; :8009's listener must be that PID with the canonical launcher's argv/cwd.  Direct
children are found UNDER that wrapper only (never pgrep) and classified against the canonical
config: engine exe, configured vision exe, MCP by exact configured command token in argv
(shebang interpreter allowed, never arbitrary python) or, for a configured MCP server whose
command execs another file, an explicit --expect-aux NAME=TARGET:TOKEN,TOKEN attestation (exact
target realpath + exact tokens, recorded with the configured script's digest; no shell
resolution, nothing attested by default); an unclassified child, or no engine
child, is a refusal BEFORE any signal.  Every owned process is recorded at ownership (pid,
exe dev+ino, starttime, argv, cwd); a chain registers for cleanup BEFORE its first process
starts and every started process registers immediately.  Signals are pidfd-only with identity
re-verified before pidfd_open and again after; a proven-exited process (ESRCH, POLLIN with no
timeout, empty cmdline) is recorded as skipped and NEVER signalled; our own Popen children are
reaped after pidfd-proven exit.  Never os.kill/killpg/pkill/pgrep, no process groups, no
start_new_session.  restoration_required flips BEFORE the production signal; cleanup and
restore run in a finally that never returns from inside; a surviving process blocks further
stages, blocks the restore, and forces a non-zero exit.  Restore uses ONLY
/mnt/user/Strata/launch-prod-swift15-dflash-w4-q4_0.sh and verifies canonical argv/cwd,
loaded=true health, >=1 canonical engine child with expected engine args, only configured
aux (vision/MCP), no surviving candidate; an unrelated :8009 occupant is a recorded CONFLICT
(never killed).  Arms differ ONLY by helper keys (OFF strips every inherited expert_helper_*;
ON adds per-stage socket, slots 3400, timeout 100), the unique per-stage engine log, port,
mcp_servers={} and the recorded vision removal; telemetry identical, STRATA_ENGINE_NICE=0 kept;
wrapper = absolute worktree serve/server.py, cwd = merged checkout, cleaned launcher env.
Sockets unique per stage, AF_UNIX < 108 (plan checks the longest label), never an existing
path, never unlinked; helper readiness is a log line only, the socket is never probed;
polling bounded (600 s, 5 s step, early child-exit).  Capture runs acceptance.py as a SEPARATE
subprocess per call (fresh --out; composite build-id over driver/capture/candidate binaries/
config/production binary), recording rc/stdout/stderr; a timeout records rc 124 with partial
output.  Failed stages are recorded; the run proceeds to cleanup+restore, no retry.  --execute
holds a global exclusive flock on LOCK_PATH.  Exit: 0 plan/all captured+restored, 2 refused
before acting, 1 recorded capture/cleanup/restore failure.  stdlib only.
"""
from __future__ import annotations

import argparse, ctypes, dataclasses, datetime, errno, hashlib, http.client, json, os, re
import select, signal, subprocess, sys, time
from pathlib import Path
from typing import Any, Callable

TOOL_VERSION = "r3-driver-2"
EXIT_DONE, EXIT_FAIL, EXIT_REFUSED = 0, 1, 2
ROOT = Path(__file__).resolve().parents[2]
UTILITY = ROOT / "tools/mixed_gpu/acceptance.py"
LAUNCHER = Path(os.environ.get("STRATA_PROD_TREE", "/mnt/user/Strata") + "/launch-prod-swift15-dflash-w4-q4_0.sh")
PROD_CONFIG = Path(os.environ.get("STRATA_PROD_TREE", "/mnt/user/Strata") + "/prod-config-swift15-dflash-w4-q4_0.json")
MERGED_CWD = Path(os.environ.get("STRATA_DFLASH2_DIR", "/mnt/user/dflash2") + "/Strata-integ")
PY = Path(os.environ.get("STRATA_PROD_TREE", "/mnt/user/Strata") + "/.venv/bin/python")
CAND_ENGINE = ROOT / "build-mixed-gpu/engine-hip/strata"
CAND_HELPER = ROOT / "build-mixed-gpu/engine-cuda/strata-expert-helper"
LOCK_PATH = ROOT / "build-mixed-gpu/r3-driver/driver.lock"      # ONE lock, not per --out-dir
LIB_DIRS = [os.environ.get("STRATA_ROCM_ROOT", "/home/user/rocm-gfx906-xnack") + "/lib", "/opt/rocm-6.1.0/lib"]
PROD_PORT, CAND_PORT = 8009, 8019
URL = f"http://127.0.0.1:{CAND_PORT}/v1/chat/completions"
TELEMETRY = {"STRATA_DECODE_TIMING": "1", "STRATA_VERIFY_PROFILE": "1", "LOOKAHEAD_STATS": "1"}
DROP_ENV_PREFIX = ("STRATA_DF_", "STRATA_DFLASH_", "STRATA_DUMP_", "STRATA_WINDOW_")
DROP_ENV_EXACT = ("LD_PRELOAD", "CUDA_VISIBLE_DEVICES", "STRATA_ARENA_MMAP",
                  "GPU_PINNED_MIN_XFER_SIZE", "HSA_USERPTR_FOR_PAGED_MEM")
HELPER_PACK = os.environ.get("STRATA_AI_DIR", "/mnt/user/ai") + "/iq3xxs-15"
HELPER_EXPERTS = "../Swift-1.5-Qwen3.8-Flash-Next-GSQ-RCO-IQ3_XXS.gguf"


def helper_argv() -> list[str]:
    """The candidate helper's exact command; read CAND_HELPER at call time so tests can point
    the constants at a fixture tree without changing the flag contract."""
    return [str(CAND_HELPER), "--pack", HELPER_PACK, "--experts", HELPER_EXPERTS, "--device",
            "0", "--slots", "3400", "--reserve-mib", "512", "--timeout", "7200000",
            "--begin-timeout", "1800000"]


HELPER_READY, WRAPPER_READY = "expert-helper: listening on", f"ready: http://127.0.0.1:{CAND_PORT}/v1"
SAMPLE_PROMPT = ("Explain how a moire pattern forms when two grids overlap. Write five short "
                 "paragraphs, with no headings. Do not use tools.")
LH = "Every evening a lighthouse keeper climbs the hundred steps, trims the lamp, and writes the weather in a ledger. "
PREFILL_PROMPT = LH * 90 + "In one sentence, summarize what the keeper does."
DECODE_PROMPT = "List the integers from 1 to 200, each on its own line, with no commentary."
PREFILL_MARKER = "[strata] reading the prompt:"
AFUNIX_MAX, READY_MAX_S, READY_STEP_S, TERM_MAX_S = 108, 600.0, 5.0, 45.0
SAMPLE_TIMEOUT_S, FAULT_TIMEOUT_S, CAPTURE_TIMEOUT_S = 900.0, 900.0, 3600.0
STAGES = [("off-1", "off", "cold"), ("on-1", "on", "cold"), ("on-2", "on", "warm"),
          ("off-2", "off", "warm")]
HELPER_CONFIG_FLAGS = [("expert_helper_socket", "--expert-helper-socket"),
                       ("expert_helper_layers", "--expert-helper-layers"),
                       ("expert_helper_slots", "--expert-helper-slots"),
                       ("expert_helper_reserve_mib", "--expert-helper-reserve-mib"),
                       ("expert_helper_timeout", "--expert-helper-timeout-ms")]


class Refusal(Exception):
    """Refused before acting: nothing was started, stopped, signalled or requested."""


class OwnedSignal(Exception):
    """A signal was refused: the owned identity no longer matches what was recorded."""


class PidfdGone(OwnedSignal):
    """pidfd says the PID is gone (ESRCH): the process is exited, signalling it is pointless."""


class StageFailure(Exception):
    """A stage failed; the run proceeds to cleanup and the canonical restore.  `chain` is the
    partially built chain (helper started, wrapper not ready, ...) that cleanup must tear down."""

    def __init__(self, message: str, chain: "Chain | None" = None) -> None:
        super().__init__(message)
        self.chain = chain


# ------------------------------------------------------------------ real seams

def _libc() -> Any:
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    for n in ("pidfd_open", "pidfd_send_signal"):
        if not hasattr(libc, n):
            raise OwnedSignal(f"libc has no {n}; pidfd is required, no fallback")
    return libc


def real_pidfd_open(pid: int) -> int:
    libc = _libc()
    libc.pidfd_open.argtypes, libc.pidfd_open.restype = [ctypes.c_int, ctypes.c_uint], ctypes.c_int
    if (fd := libc.pidfd_open(pid, 0)) < 0:
        err = ctypes.get_errno()
        if err == errno.ESRCH:
            raise PidfdGone(f"pidfd_open({pid}): ESRCH, process is gone")
        raise OwnedSignal(f"pidfd_open({pid}) failed: {os.strerror(err)}")
    return fd


def real_pidfd_signal(fd: int, sig: int) -> None:
    libc = _libc()
    args = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
    libc.pidfd_send_signal.argtypes, libc.pidfd_send_signal.restype = args, ctypes.c_int
    if libc.pidfd_send_signal(fd, sig, None, 0) != 0:
        raise OwnedSignal(f"pidfd_send_signal({sig}) failed: {os.strerror(ctypes.get_errno())}")


def real_pidfd_death(fd: int, timeout: float) -> bool:
    """POLLIN on a pidfd proves EXIT, not reaping."""
    poller = select.poll()
    poller.register(fd, select.POLLIN)
    return bool(poller.poll(max(0, int(timeout * 1000))))


def real_cmdline(pid: int) -> list[str]:
    raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    return [p.decode("utf-8", "replace") for p in raw.split(b"\0") if p]


def real_starttime(pid: int) -> int:
    stat = Path(f"/proc/{pid}/stat").read_text()
    return int(stat[stat.rindex(")") + 1:].split()[19])       # stat field 22, after pid/comm


def real_exe(pid: int) -> tuple[str, int, int]:
    st = os.stat(f"/proc/{pid}/exe")
    return os.path.realpath(os.readlink(f"/proc/{pid}/exe")), st.st_dev, st.st_ino


def real_cwd(pid: int) -> str:
    return os.path.realpath(os.readlink(f"/proc/{pid}/cwd"))


def real_children(pid: int) -> list[int]:
    kids: list[int] = []
    for task in Path(f"/proc/{pid}/task").iterdir():
        try:
            kids += [int(x) for x in (task / "children").read_text().split()]
        except (OSError, ValueError):
            continue
    return sorted(set(kids))


def real_alive(pid: int) -> bool:
    """True for a ZOMBIE too: /proc stays until the parent reaps.  Exit proof is pidfd/argv."""
    return Path(f"/proc/{pid}").exists()


def real_listeners(port: int) -> list[dict[str, Any]]:
    """The OS view of listening sockets on `port`, with the owning pid from ss -p."""
    try:
        out = subprocess.run(["ss", "-H", "-ltnp", f"sport = :{port}"], capture_output=True,
                             text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise Refusal(f"cannot list port {port}: {exc} (ss is required, like the launcher)")
    return [{"addr": next((f for f in line.split() if f.endswith(f":{port}")), ""),
             "pid": int(m.group(1)) if (m := re.search(r"pid=(\d+)", line)) else None,
             "line": line} for line in out.splitlines()]


def real_popen(argv: list[str], cwd: str, env: dict[str, str], out: str, err: str) -> Any:
    """Owned child.  NO start_new_session: the candidate wrapper is owned by its own pidfd, and
    the production launcher already calls setsid itself."""
    return subprocess.Popen([str(a) for a in argv], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                            stdout=open(out, "ab"), stderr=open(err, "ab"), close_fds=True)


def real_health(host: str, port: int, timeout: float) -> dict[str, Any]:
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", "/health")
        body = conn.getresponse().read()
    finally:
        conn.close()
    return json.loads(body.decode("utf-8"))


def real_capture(argv: list[str], cwd: str, timeout: float) -> tuple[int, str, str]:
    """One acceptance.py call.  A timeout is RECORDED as rc 124 with its partial output."""
    try:
        p = subprocess.run([str(a) for a in argv], cwd=cwd, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as exc:
        def txt(v: Any) -> str:
            return v.decode("utf-8", "replace") if isinstance(v, bytes) else (v or "")
        return 124, txt(exc.stdout), (txt(exc.stderr) + f"\ncapture timeout after {timeout}s")


@dataclasses.dataclass
class Hooks:
    """Every OS touch of --execute goes through one of these seams; tests inject fakes."""
    popen: Callable[..., Any] = real_popen
    poll: Callable[[Any], Any] = lambda p: p.poll()
    reap: Callable[[Any], Any] = lambda p: p.poll()       # after pidfd-proven exit, own children
    pidfd_open: Callable[[int], int] = real_pidfd_open
    pidfd_signal: Callable[[int, int], None] = real_pidfd_signal
    pidfd_death: Callable[[int, float], bool] = real_pidfd_death
    pidfd_close: Callable[[int], None] = os.close
    cmdline: Callable[[int], list[str]] = real_cmdline
    starttime: Callable[[int], int] = real_starttime
    exe: Callable[[int], tuple[str, int, int]] = real_exe
    cwd: Callable[[int], str] = real_cwd
    children: Callable[[int], list[int]] = real_children
    alive: Callable[[int], bool] = real_alive
    listeners: Callable[[int], list[dict[str, Any]]] = real_listeners
    health: Callable[[str, int, float], dict[str, Any]] = real_health
    capture: Callable[[list[str], str, float], tuple[int, str, str]] = real_capture
    sha256: Callable[[Path], str] = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
    wall: Callable[[], float] = time.time
    mono: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


# ------------------------------------------------------------------ identity

@dataclasses.dataclass(frozen=True)
class Ident:
    kind: str
    pid: int
    exe: str
    dev: int
    ino: int
    starttime: int
    argv: tuple[str, ...]
    cwd: str
    approx: bool = False      # recorded from OUR OWN Popen's argv after a /proc read failed;
                              # recheck then compares argv only (dev/ino/starttime unrecorded)

    def brief(self) -> dict[str, Any]:
        return {"kind": self.kind, "pid": self.pid, "exe": self.exe, "starttime": self.starttime,
                "argv": list(self.argv), "cwd": self.cwd, "approx": self.approx}


def record(kind: str, pid: int, h: Hooks) -> Ident:
    exe, dev, ino = h.exe(pid)
    return Ident(kind, pid, exe, dev, ino, h.starttime(pid), tuple(h.cmdline(pid)), h.cwd(pid))


def recheck(i: Ident, h: Hooks) -> list[str]:
    """Fresh check of a recorded process; [] means 'still exactly the one we recorded'.
    ENOENT/ESRCH (and an empty cmdline, e.g. a zombie) prove EXIT; an EACCES-class unreadable
    identity is NOT exit proof - it is a problem that refuses the signal and blocks restore."""
    if not h.alive(i.pid):
        return ["exited: not alive"]
    try:
        argv = tuple(h.cmdline(i.pid))
    except FileNotFoundError:
        return ["exited: not alive"]
    except OSError as exc:
        return [f"identity unreadable: {exc}"]
    if not argv:
        return ["exited: empty cmdline"]
    if i.approx:                       # our own child, recorded from the argv we passed
        return [] if argv == i.argv else ["argv changed"]
    try:
        exe, dev, ino = h.exe(i.pid)
        st = h.starttime(i.pid)
    except FileNotFoundError:
        return ["exited: not alive"]
    except OSError as exc:
        return [f"identity unreadable: {exc}"]
    problems = []
    if (dev, ino) != (i.dev, i.ino):
        problems.append(f"exe inode changed ({i.exe} -> {exe})")
    if st != i.starttime:
        problems.append(f"starttime changed ({i.starttime} -> {st})")
    if argv != i.argv:
        problems.append("argv changed")
    return problems


def exited_proven(i: Ident, h: Hooks) -> bool:
    """The recorded PID is exited: pidfd says ESRCH, POLLIN is ready with no timeout (a zombie
    counts), or its cmdline is empty.  This never claims the process was reaped.  A vanished
    /proc entry (ENOENT) counts; an EACCES-class read does NOT prove exit."""
    if not h.alive(i.pid):
        return True
    try:
        argv = h.cmdline(i.pid)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if not argv:
        return True
    try:
        fd = h.pidfd_open(i.pid)
    except PidfdGone:
        return True
    try:
        return h.pidfd_death(fd, 0.0)
    finally:
        h.pidfd_close(fd)


def signal_owned(i: Ident, sig: int, h: Hooks, m: "Manifest") -> None:
    """Signal ONLY a pidfd-bound, re-verified identity that is proven ALIVE.  An exited PID is
    recorded and never signalled.  Never os.kill / killpg / pkill."""
    problems = recheck(i, h)
    if any(str(p).startswith("exited") for p in problems):
        m.event("signal_skipped_exited", kind=i.kind, pid=i.pid, sig=int(sig),
                reason="; ".join(problems))
        return
    if problems:
        raise OwnedSignal(f"signal refused for {i.kind} pid {i.pid}: {'; '.join(problems)}")
    try:
        fd = h.pidfd_open(i.pid)                                  # pidfd FIRST, then recheck
    except PidfdGone:
        m.event("signal_skipped_exited", kind=i.kind, pid=i.pid, sig=int(sig), reason="ESRCH")
        return
    try:
        if (problems := recheck(i, h)):
            if any(str(p).startswith("exited") for p in problems):
                m.event("signal_skipped_exited", kind=i.kind, pid=i.pid, sig=int(sig),
                        reason="; ".join(problems))
                return
            raise OwnedSignal(f"signal refused for {i.kind} pid {i.pid} after pidfd: "
                              f"{'; '.join(problems)}")
        h.pidfd_signal(fd, sig)
        m.event("signal", kind=i.kind, pid=i.pid, sig=int(sig))
    finally:
        h.pidfd_close(fd)


def wait_gone(i: Ident, h: Hooks, timeout: float) -> bool:
    """True when the recorded PID has exited (zombie counts; reaping is a separate step)."""
    try:
        fd = h.pidfd_open(i.pid)
    except PidfdGone:
        return True
    try:
        return h.pidfd_death(fd, timeout)
    finally:
        h.pidfd_close(fd)


class Manifest:
    """One flushed JSON line per transition: the run's raw record."""

    def __init__(self, path: Path, h: Hooks) -> None:
        self.path, self.h, self.rows = path, h, []

    def event(self, event: str, **fields: Any) -> dict[str, Any]:
        row = {"at": datetime.datetime.fromtimestamp(self.h.wall(),
                                                     datetime.timezone.utc).isoformat(),
               "event": event, **fields}
        self.rows.append(row)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")
            fh.flush()
        return row


# ------------------------------------------------------------------ env / config

def clean_env(h: Hooks) -> dict[str, str]:
    """Wrapper/restore env exactly as the canonical launcher cleans it, plus no LD_PRELOAD."""
    env = dict(os.environ)
    for name in list(env):
        if name.startswith(DROP_ENV_PREFIX) or name in DROP_ENV_EXACT:
            del env[name]
    env["LD_LIBRARY_PATH"] = ":".join(LIB_DIRS)
    return env


def _diff(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    keys = sorted(set(a) | set(b))
    return {k: {"canonical": a.get(k, "<absent>"), "derived": b.get(k, "<absent>")}
            for k in keys if a.get(k) != b.get(k)}


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=1, sort_keys=True, default=str) + "\n",
                    encoding="utf-8")


def helper_config_flags(cfg: dict[str, Any]) -> list[str]:
    """The engine flags the server maps from expert_helper_* config keys (serve/server.py)."""
    argv: list[str] = []
    for key, flag in HELPER_CONFIG_FLAGS:
        if key in cfg:
            argv += [flag, str(cfg[key])]
    return argv


def expected_engine_argv(cfg: dict[str, Any]) -> list[str]:
    """What serve/server.py starts the engine with for THIS config (the documented server
    composition, serve/server.py engine_args/expert_helper_args): [exe, --serve] + the config
    args + the flags the server derives from config keys (layer split, parallel, vram elastic,
    and the expert_helper_* mapping) - in the server's order.  Server-only extra arguments the
    engine receives beyond this sequence are permitted by engine_intent_matches, not demanded."""
    args = [str(a) for a in (cfg.get("args") or [])]
    split = cfg.get("layer_split")
    if split and not isinstance(split, bool) and str(split) and "--layer-split" not in args:
        args += ["--layer-split", str(split)]
        if cfg.get("split_skip_if_fits") and "--split-skip-if-fits" not in args:
            args.append("--split-skip-if-fits")
    par = cfg.get("parallel")
    if isinstance(par, int) and not isinstance(par, bool) and par > 1 \
            and "--batch" not in args and "--slots" not in args:
        args += ["--batch", str(par)]
    if cfg.get("vram_elastic") is True and "--vram-elastic" not in args:
        args += ["--vram-elastic"]
        seg = cfg.get("vram_segment_mib")
        if isinstance(seg, int) and not isinstance(seg, bool) and seg > 0 \
                and "--vram-segment-mib" not in args:
            args += ["--vram-segment-mib", str(seg)]
    args += helper_config_flags(cfg)
    return [str(cfg["exe"]), "--serve", *args]


def engine_intent_matches(cfg: dict[str, Any], argv: tuple[str, ...] | list[str]
                          ) -> tuple[bool, list[str]]:
    """Exact ORDERED sequence match of the expected engine argv (flag/value pairs included: a
    correct token with the wrong value, or the same value sitting on another flag, fails).
    Server-only extra arguments after the sequence are permitted and reported."""
    argv, want = [str(a) for a in argv], expected_engine_argv(cfg)
    if len(argv) < len(want) or argv[:len(want)] != want:
        return False, want
    return True, argv[len(want):]


def config_aux_classes(prod: dict[str, Any]) -> tuple[str, str | None, list[tuple[str, str]]]:
    """What the canonical config legitimately spawns: engine exe, vision exe, MCP commands."""
    engine = os.path.realpath(str(prod["exe"]))
    vision = None
    v = prod.get("vision")
    if isinstance(v, dict) and v.get("exe"):
        vision = os.path.realpath(str(v["exe"]))
    mcp: list[tuple[str, str]] = []
    servers = prod.get("mcp_servers")
    if isinstance(servers, dict):
        for name, spec in servers.items():
            cmd = spec.get("command") if isinstance(spec, dict) else None
            if isinstance(cmd, str) and cmd.strip():
                mcp.append((str(name), cmd))
    return engine, vision, mcp


def parse_expect_aux(specs: list[str]) -> dict[str, tuple[str, tuple[str, ...]]]:
    """Explicit parent attestation for an MCP child whose CONFIGURED command is a wrapper that
    execs another file (the canonical docling case).  Format NAME=TARGET_PATH:TOKEN,TOKEN.
    Deliberately narrow: no shell resolution, no wildcards, only names configured in
    mcp_servers, exact target realpath and exact tokens.  Nothing is attested by default."""
    out: dict[str, tuple[str, tuple[str, ...]]] = {}
    for spec in specs or []:
        name, _, rest = str(spec).partition("=")
        target, _, tokens = rest.partition(":")
        if not name or not target:
            raise Refusal(f"--expect-aux needs NAME=TARGET_PATH:TOKEN,TOKEN, got {spec!r}")
        out[name] = (os.path.realpath(target),
                     tuple(t for t in tokens.split(",") if t))
    return out


def classify_child(kid: Ident, engine: str, vision: str | None,
                   mcp: list[tuple[str, str]],
                   attest: dict[str, tuple[str, tuple[str, ...]]] | None = None) -> str | None:
    """engine/vision/mcp:<name>/None - a configured identity only.  Arbitrary python (no
    configured command token anywhere in argv) matches nothing.  The ONLY allowance beyond the
    configured token is an explicit parent attestation (see parse_expect_aux)."""
    argv = [str(a) for a in kid.argv]
    if kid.exe == engine:
        return "engine"
    if argv and os.path.realpath(argv[0]) == engine:
        return "engine"
    if vision and (kid.exe == vision or (argv and os.path.realpath(argv[0]) == vision)):
        return "vision"
    for name, cmd in mcp:
        if cmd in argv:                                        # exec'd script: token IS argv[0]
            return f"mcp:{name}"
        if argv and Path(argv[0]).name.startswith("python") and cmd in argv[1:]:
            return f"mcp:{name}"                              # shebang interpreter form
        att = (attest or {}).get(name)
        if att is not None and argv and os.path.realpath(argv[0]) == att[0] \
                and all(tok in argv for tok in att[1]):
            return f"mcp:{name}"                              # attested exec target + tokens
    return None


def derive_base(prod: dict[str, Any]) -> dict[str, Any]:
    """Arm base from the canonical config read NOW.  OFF strips EVERY inherited expert_helper_*
    key; every difference from production is recorded in the plan's diff."""
    base = json.loads(json.dumps(prod))
    base["exe"] = str(CAND_ENGINE)
    base["port"] = CAND_PORT
    base["mcp_servers"] = {}                                   # no MCP processes in either arm
    base["env"] = dict(base.get("env") or {})
    base["env"]["STRATA_ENGINE_NICE"] = "0"
    base["env"].update(TELEMETRY)
    args = list(base.get("args") or [])
    if "--vision" in args:                                      # deliberate, recorded: no vision
        args.remove("--vision")
    base["args"] = args
    base.pop("vision", None)
    for key in [k for k in list(base) if str(k).startswith("expert_helper_")]:
        del base[key]
    return base


def arm_config(base: dict[str, Any], arm: str, stage: str, run_dir: Path) -> dict[str, Any]:
    """Per-stage config: unique engine log, ON adds its unique socket + slots + timeout."""
    cfg = json.loads(json.dumps(base))
    cfg["log"] = str(run_dir / f"engine-{stage}.log")
    if arm == "on":
        cfg["expert_helper_socket"] = str(run_dir / f"helper-{stage}.sock")
        cfg["expert_helper_slots"] = 3400
        cfg["expert_helper_timeout"] = 100
    return cfg


def request_payload(content: str, max_tokens: int, stream: bool) -> dict[str, Any]:
    """The pinned workload: identical bytes across arms, never edited by this driver."""
    req = {"model": "swift-1.5-iq3_xxs", "messages": [{"role": "user", "content": content}],
           "max_tokens": max_tokens, "temperature": 0, "seed": 271828, "top_p": 1, "top_k": 1,
           "reasoning_effort": "none"}
    if stream:
        req["stream"] = True
        req["stream_options"] = {"include_usage": True}
    return req


def socket_path(run_dir: Path, label: str) -> Path:
    """Unique per-stage socket in THIS run: never an existing path, never a blind unlink."""
    path = run_dir / f"helper-{label}.sock"
    if len(os.fsencode(path)) >= AFUNIX_MAX:
        raise Refusal(f"AF_UNIX path too long ({len(os.fsencode(path))} >= {AFUNIX_MAX}): {path}")
    if path.exists():
        raise Refusal(f"socket path already exists, refusing to reuse or unlink: {path}")
    return path


# ------------------------------------------------------------------ verification

def canonical_argv(config: str) -> list[str]:
    return [str(PY), str(MERGED_CWD / "serve/server.py"), "--engine", "strata",
            "--config", str(config), "--port", str(PROD_PORT)]


def argv_is_canonical(argv: list[str], config: str) -> bool:
    want = canonical_argv(config)
    if argv[:len(want)] == want:
        return True
    relative = list(want)
    relative[1] = "serve/server.py"            # the launcher cds to the merged checkout first
    return argv[:len(relative)] == relative


def expected_wrapper_identity(h: Hooks, pid: int, starttime: int, config: str) -> Ident:
    """The production wrapper must BE the canonical launcher's command on :8009, right now."""
    owners = sorted({r["pid"] for r in h.listeners(PROD_PORT) if r["pid"]})
    if pid not in owners:
        raise Refusal(f":{PROD_PORT} listener pids {owners} do not include expected wrapper pid "
                      f"{pid}; refusing to signal anything")
    ident = record("production-wrapper", pid, h)
    if ident.starttime != starttime:
        raise Refusal(f"expected starttime {starttime} != live starttime {ident.starttime} for "
                      f"pid {pid}: identity mismatch, refusing")
    if not argv_is_canonical(list(ident.argv), str(config)):
        raise Refusal(f"pid {pid} argv {list(ident.argv)} is not canonical "
                      f"{canonical_argv(str(config))} (nor the launcher's relative form)")
    if ident.cwd != str(MERGED_CWD):
        raise Refusal(f"pid {pid} cwd {ident.cwd} != merged checkout {MERGED_CWD}; refusing")
    return ident


def production_children(ident: Ident, prod: dict[str, Any], h: Hooks,
                        attest: dict[str, tuple[str, tuple[str, ...]]] | None = None
                        ) -> list[Ident]:
    """Direct children discovered UNDER the verified wrapper only - never a broad pgrep -
    classified against the canonical config (engine / vision / configured MCP, plus explicit
    parent attestations).  An unclassified child is a refusal BEFORE any signal; a wrapper with
    no engine child is too."""
    engine, vision, mcp = config_aux_classes(prod)
    kids, unknown = [], []
    for pid in h.children(ident.pid):
        kid = record(f"production-child-{pid}", pid, h)
        cls = classify_child(kid, engine, vision, mcp, attest)
        if cls is None:
            unknown.append(kid)
        else:
            kids.append(Ident(child_kind(cls), kid.pid, kid.exe, kid.dev, kid.ino, kid.starttime,
                              kid.argv, kid.cwd))
    if unknown:
        raise Refusal(f"children under wrapper {ident.pid} match nothing configured: "
                      f"{[k.brief() for k in unknown]}; refusing to stop a chain we do not "
                      f"understand")
    if not [k for k in kids if k.kind == "production-engine-child"]:
        raise Refusal(f"no canonical engine child ({engine}) under wrapper {ident.pid}: "
                      f"{[k.brief() for k in kids]}; refusing")
    return kids


def child_kind(cls: str) -> str:
    base = "mcp" if cls.startswith("mcp:") else cls
    return f"production-{base}-child"


def candidate_port_rows(h: Hooks) -> list[dict[str, Any]]:
    return h.listeners(CAND_PORT)


def ensure_candidate_port_free(h: Hooks) -> None:
    if (rows := candidate_port_rows(h)):
        raise Refusal(f"candidate port {CAND_PORT} occupied {[r['line'] for r in rows]}; "
                      f"refusing to start a chain on a busy port")


def wait_for_marker(proc: Any, log: Path, marker: str, label: str, h: Hooks,
                    m: Manifest) -> bool:
    """Bounded INTERNAL readiness polling on a log line; a child exit ends it early."""
    deadline = h.mono() + READY_MAX_S
    while h.mono() < deadline:
        if (rc := h.poll(proc)) is not None:
            m.event("ready_failed", target=label, child_exit=rc, marker=marker)
            return False
        try:
            text = log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        if marker in text:
            m.event("ready", target=label, marker=marker)
            return True
        h.sleep(READY_STEP_S)
    m.event("ready_timeout", target=label, marker=marker, max_s=READY_MAX_S)
    return False


# ------------------------------------------------------------------ chains

@dataclasses.dataclass(eq=False)
class Chain:
    stage: str
    arm: str
    socket: Path | None = None
    helper: Ident | None = None
    wrapper: Ident | None = None
    proc: Any = None
    helper_proc: Any = None
    engine: list[Ident] = dataclasses.field(default_factory=list)
    logs: dict[str, str] = dataclasses.field(default_factory=dict)
    cfg: dict[str, Any] = dataclasses.field(default_factory=dict)


class Driver:
    def __init__(self, args: argparse.Namespace, h: Hooks, m: Manifest,
                 base: dict[str, Any]) -> None:
        self.a, self.h, self.m, self.base = args, h, m, base
        self.attest = parse_expect_aux(getattr(args, "expect_aux", []) or [])
        self.run_dir = Path(args.out_dir)
        self.owned: list[Ident] = []                 # every process this driver started/knows
        self.active: set[Chain] = set()              # registered BEFORE the first Popen
        self.sockets: list[Path] = []
        self.restoration_required = False
        self.production_stopped = False
        self.blocked = False                         # surviving process: stop stages, block restore
        self.capture_failures: list[str] = []
        self.restore: dict[str, Any] = {"attempted": False, "ok": False, "status": "not-needed"}

    def record_owned(self, kind: str, proc: Any, argv: list[str], cwd: str,
                     m: Manifest) -> Ident:
        """Record OUR OWN started child at once.  If the /proc read fails after a successful
        start (transient race), fall back to the identity we KNOW (pid + the argv we passed +
        the exe path we asked for) marked approx, so the live child is owned and terminable;
        never leave an untracked live candidate."""
        try:
            return record(kind, proc.pid, self.h)
        except BaseException as exc:
            m.event("owned_identity_degraded", kind=kind, pid=proc.pid,
                    error=f"{type(exc).__name__}: {exc}")
            return Ident(kind, proc.pid, os.path.realpath(str(argv[0])), 0, 0, 0,
                         tuple(str(a) for a in argv), os.path.realpath(str(cwd)), approx=True)

    # ---------------------------------------------------------- chain lifecycle
    def start_chain(self, stage: str, arm: str, build_sha: str) -> Chain:
        """Register the chain for cleanup BEFORE its first process starts; register every
        started process the moment it exists.  A chain is never returned unregistered."""
        chain = Chain(stage, arm)
        self.active.add(chain)
        ensure_candidate_port_free(self.h)
        chain.logs = {"wrapper": str(self.run_dir / f"wrapper-{stage}.log"),
                      "engine": str(self.run_dir / f"engine-{stage}.log")}
        if arm == "on":
            chain.logs["helper"] = str(self.run_dir / f"helper-{stage}.log")
        env = clean_env(self.h)
        if arm == "on":
            sock = socket_path(self.run_dir, stage)
            chain.socket, chain.logs["socket"] = sock, str(sock)
            self.sockets.append(sock)
            argv = [*helper_argv(), "--socket", str(sock)]
            chain.helper_proc = self.h.popen(argv, str(ROOT), dict(env, CUDA_VISIBLE_DEVICES="0"),
                                             chain.logs["helper"], chain.logs["helper"])
            chain.helper = self.record_owned("candidate-helper", chain.helper_proc, argv, str(ROOT),
                                             self.m)
            self.owned.append(chain.helper)
            if not wait_for_marker(chain.helper_proc, Path(chain.logs["helper"]), HELPER_READY,
                                   f"helper:{stage}", self.h, self.m):
                raise StageFailure(f"helper never reported ready for {stage}", chain)
        cfg = arm_config(self.base, arm, stage, self.run_dir)
        chain.cfg = cfg
        cfg_path = self.run_dir / f"config-{stage}.json"
        write_json(cfg_path, cfg)
        argv = [str(PY), str(ROOT / "serve/server.py"), "--engine", "strata", "--config",
                str(cfg_path), "--port", str(CAND_PORT)]
        chain.proc = self.h.popen(argv, str(MERGED_CWD), env, chain.logs["wrapper"],
                                  chain.logs["wrapper"])
        chain.wrapper = self.record_owned("candidate-wrapper", chain.proc, argv, str(MERGED_CWD),
                                          self.m)
        self.owned.append(chain.wrapper)
        if not wait_for_marker(chain.proc, Path(chain.logs["wrapper"]), WRAPPER_READY,
                               f"wrapper:{stage}", self.h, self.m):
            raise StageFailure(f"candidate wrapper never reported ready for {stage}", chain)
        self.discover_chain_children(chain)
        self.m.event("chain_started", stage=stage, arm=arm, build_sha256=build_sha,
                     wrapper=chain.wrapper.brief(), config=str(cfg_path), logs=chain.logs,
                     socket=str(chain.socket) if chain.socket else None,
                     helper=chain.helper.brief() if chain.helper else None,
                     children=[k.brief() for k in chain.engine])
        return chain

    def discover_chain_children(self, c: Chain) -> None:
        """ALL current direct children of the owned wrapper (watchdog replacements included),
        recorded as owned identities.  A child that is not the candidate engine exe is recorded
        (so cleanup can see it) and fails the stage: we NEVER signal a child we do not
        understand - it leaves with the wrapper or blocks the restore."""
        assert c.wrapper is not None
        known = {k.pid for k in c.engine}
        engine = os.path.realpath(CAND_ENGINE)
        for pid in self.h.children(c.wrapper.pid):
            if pid in known:
                continue
            kid = record("candidate-engine-child", pid, self.h)
            c.engine.append(kid)
            self.owned.append(kid)
            if kid.exe != engine:
                self.m.event("chain_child_unexpected", stage=c.stage, child=kid.brief(),
                             want=engine)
                raise StageFailure(f"child {pid} under candidate wrapper is not the candidate "
                                   f"engine {engine} at {c.stage}: {kid.brief()}", c)
        if not c.engine:
            raise StageFailure(f"no engine child under the candidate wrapper at {c.stage}", c)
        for kid in c.engine:
            ok, extra = engine_intent_matches(c.cfg, kid.argv)
            if not ok:
                self.m.event("chain_child_args_unexpected", stage=c.stage, child=kid.brief(),
                             expected=expected_engine_argv(c.cfg))
                raise StageFailure(f"engine child {kid.pid} argv {list(kid.argv)} is not the "
                                   f"expected engine sequence for {c.stage}'s config "
                                   f"{expected_engine_argv(c.cfg)}", c)
        self.m.event("chain_children", stage=c.stage,
                     children=[k.brief() for k in c.engine],
                     engine_extra_args=[list(k.argv[len(expected_engine_argv(c.cfg)):])
                                         for k in c.engine if k.exe == engine])

    def _terminate(self, i: Ident, why: str) -> bool:
        """Identity-bound TERM of ONE recorded process.  True when its exit is confirmed."""
        if exited_proven(i, self.h):
            self.m.event("terminate_skipped_exited", kind=i.kind, pid=i.pid, why=why)
            return True
        signal_owned(i, signal.SIGTERM, self.h, self.m)
        gone = wait_gone(i, self.h, TERM_MAX_S)
        self.m.event("terminated", kind=i.kind, pid=i.pid, gone=gone, why=why)
        return gone

    def stop_chain(self, c: Chain, why: str) -> None:
        """TERM the owned wrapper; children with the EXPECTED candidate engine exe get an
        identity-bound TERM; children we do NOT understand are NEVER signalled - they exit with
        the wrapper or their survival is recorded and blocks the restore.  Helper: reap our own
        handle, identity-bound TERM only if proven alive.  The planned helper SIGKILL happens
        ONLY in acceptance.py.  Port freedom is recorded, never used as proof."""
        engine = os.path.realpath(CAND_ENGINE)
        wrapper_gone = True
        self.discover_chain_children_safe(c)        # current children WHILE the wrapper lives
        if c.wrapper is not None:                   # (watchdog replacements recorded too)
            wrapper_gone = self._terminate(c.wrapper, why)
        survivors, unknown_alive = [], []
        for kid in list(c.engine):
            if kid.exe != engine:                   # unknown child: no direct signal, ever
                if not exited_proven(kid, self.h):
                    unknown_alive.append(kid.brief())
                continue
            if not self._terminate(kid, why):
                survivors.append(kid.brief())
        engine_gone = not survivors and not unknown_alive
        helper_gone = True
        if c.helper is not None:
            rc = self.h.poll(c.helper_proc) if c.helper_proc is not None else None
            if rc is not None:
                self.h.reap(c.helper_proc)
                self.m.event("chain_helper_already_exited", stage=c.stage, rc=rc, why=why)
            elif exited_proven(c.helper, self.h):
                if c.helper_proc is not None:
                    self.h.reap(c.helper_proc)
                self.m.event("chain_helper_already_exited", stage=c.stage, why=why)
            else:
                helper_gone = self._terminate(c.helper, why)
                if helper_gone and c.helper_proc is not None:
                    self.h.reap(c.helper_proc)
        rows = candidate_port_rows(self.h)
        self.m.event("chain_stopped", stage=c.stage, arm=c.arm, wrapper_gone=wrapper_gone,
                     engine_gone=engine_gone, engine_survivors=survivors,
                     unknown_children_alive=unknown_alive, helper_gone=helper_gone,
                     port_8019_free=not rows, why=why)
        if rows:
            self.capture_failures.append(f"port {CAND_PORT} busy after {c.stage}: "
                                          f"{[r['line'] for r in rows]}")
        if not wrapper_gone:
            self.capture_failures.append(f"wrapper {c.wrapper.pid} survived TERM at {c.stage}")
            self.blocked = True
        if not engine_gone:
            self.capture_failures.append(f"children at {c.stage} not confirmed gone "
                                          f"(survivors {survivors}, unknown {unknown_alive}; "
                                          f"unknown children are never signalled)")
            self.blocked = True
        if not helper_gone:
            self.capture_failures.append(f"helper {c.helper.pid} survived TERM at {c.stage}")
            self.blocked = True
        self.active.discard(c)

    def discover_chain_children_safe(self, c: Chain) -> None:
        """Record children that appeared under a still-alive wrapper; never signal the unknown."""
        if c.wrapper is None or not self.h.alive(c.wrapper.pid):
            return
        known = {k.pid for k in c.engine}
        engine = os.path.realpath(CAND_ENGINE)
        try:
            pids = self.h.children(c.wrapper.pid)
        except OSError:
            return
        for pid in pids:
            if pid in known:
                continue
            kid = record("candidate-engine-child", pid, self.h)
            c.engine.append(kid)
            self.owned.append(kid)
            self.m.event("chain_child_new", stage=c.stage, child=kid.brief(),
                         engine=kid.exe == engine)

    # ---------------------------------------------------------- capture
    def utility(self, argv: list[str], label: str) -> dict[str, Any]:
        """One acceptance.py call as a SEPARATE subprocess; rc/stdout/stderr recorded."""
        out_dir = self.run_dir / f"evidence-{label}"
        argv = [sys.executable, str(UTILITY), *[str(x) for x in argv], "--out", str(out_dir)]
        if out_dir.exists():
            raise Refusal(f"evidence dir exists, refusing to overwrite: {out_dir}")
        rc, out, err = self.h.capture(argv, str(ROOT), CAPTURE_TIMEOUT_S)
        for suffix, text in (("rc", str(rc)), ("stdout", out), ("stderr", err)):
            (self.run_dir / f"{label}.{suffix}").write_text(text, encoding="utf-8")
        row = self.m.event("capture", label=label, rc=rc, argv=argv, out_dir=str(out_dir),
                           stdout_file=str(self.run_dir / f"{label}.stdout"))
        if rc != 0:
            self.capture_failures.append(f"{label}: acceptance.py exit {rc}")
        return row

    def log_args(self, c: Chain, labels: tuple[str, ...]) -> list[str]:
        return [a for label in labels if c.logs.get(label) for a in ("--log", f"{label}="
                                                                    f"{c.logs[label]}")]

    def run_sample(self, stage: str, arm: str, phase: str, build_sha: str, c: Chain) -> None:
        argv = ["sample", "--url", URL, "--request", str(self.run_dir / "request-sample.json"),
                "--phase", phase, "--chain", stage, "--build-id", build_sha, "--helper-state",
                arm, "--samples", "3", "--warmups", "2", "--timeout", str(SAMPLE_TIMEOUT_S)]
        self.utility(argv + self.log_args(c, ("wrapper", "engine", "helper")), f"sample-{stage}")

    def run_fault(self, kind: str, build_sha: str, c: Chain) -> None:
        """Fresh ON chain, one fault, one post-death normal request.  No retry, no PASS."""
        assert c.helper is not None
        argv = ["fault", "--url", URL, "--request",
                str(self.run_dir / f"request-fault-{kind}.json"), "--pid", str(c.helper.pid),
                "--exe", str(c.helper.exe), "--starttime", str(c.helper.starttime),
                "--trigger", "log-marker" if kind == "prefill" else "first-delta",
                "--chain", f"fault-{kind}", "--build-id", build_sha,
                "--timeout", str(FAULT_TIMEOUT_S)]
        if kind == "prefill":
            argv += ["--marker", PREFILL_MARKER]
        self.utility(argv + self.log_args(c, ("wrapper", "engine", "helper")), f"fault-{kind}")
        argv = ["sample", "--url", URL, "--request", str(self.run_dir / "request-sample.json"),
                "--phase", "warm", "--chain", f"postdeath-{kind}", "--build-id", build_sha,
                "--helper-state", "on", "--samples", "1", "--warmups", "0", "--timeout",
                str(SAMPLE_TIMEOUT_S)]
        self.utility(argv + self.log_args(c, ("wrapper", "engine", "helper")),
                     f"postdeath-{kind}")

    # ---------------------------------------------------------- production stop
    def stop_production(self, ident: Ident, kids: list[Ident]) -> None:
        self.m.event("production_pre_stop", wrapper=ident.brief(),
                     children=[k.brief() for k in kids],
                     candidate_port_free=not candidate_port_rows(self.h))
        ensure_candidate_port_free(self.h)
        self.restoration_required = True                          # BEFORE any signal, always
        self.owned.extend(kids)           # production children are known processes: they must
        if not self._terminate(ident, "production-stop"):         # exit before the candidate runs
            self.m.event("production_stop_unclean", pid=ident.pid, timeout_s=TERM_MAX_S)
            self.blocked = True
            raise StageFailure(f"production wrapper {ident.pid} did not exit after TERM")
        survivors = []
        for kid in kids:                                          # wrapper TERM owns their teardown
            if self._terminate(kid, "production-child"):          # TERM of KNOWN identities only
                continue
            survivors.append(kid.brief())
        if survivors:
            self.m.event("production_child_survived", survivors=survivors)
            self.capture_failures.append(f"production children survived their wrapper: "
                                          f"{survivors}; candidate start is blocked")
            self.blocked = True
            raise StageFailure(f"production children survived their wrapper: {survivors}")
        self.production_stopped = True
        self.m.event("production_stopped", pid=ident.pid)

    # ---------------------------------------------------------- restore
    def restore_production(self, prod: dict[str, Any], build_sha: str) -> None:
        self.restore = {"attempted": True, "ok": False, "status": "unknown"}
        if not self.restoration_required:
            self.restore = {"attempted": False, "ok": True,
                            "status": "no production stop was attempted, nothing to restore"}
            self.m.event("restore_skipped", reason=self.restore["status"])
            return
        candidates = ("candidate-helper", "candidate-wrapper", "candidate-engine-child",
                      "production-wrapper", "production-engine-child", "production-vision-child",
                      "production-mcp-child")
        survivors = [i.brief() for i in self.owned
                     if i.kind in candidates and not exited_proven(i, self.h)]
        if survivors:
            self.restore.update(status="RESTORE_BLOCKED: candidate/production processes are "
                                        "alive; refusing to start a competing service",
                                survivors=survivors)
            self.m.event("restore_blocked", survivors=survivors)
            return
        if (rows := self.h.listeners(PROD_PORT)):
            self.restore.update(status="CONFLICT: :8009 is occupied; refusing to kill anything",
                                occupant=[r["line"] for r in rows])
            self.m.event("restore_conflict", occupant=self.restore["occupant"])
            return
        env = clean_env(self.h)
        proc = self.h.popen([str(LAUNCHER)], str(LAUNCHER.parent), env,
                            str(self.run_dir / "restore.log"), str(self.run_dir / "restore.log"))
        self.owned.append(record("restore-launcher", proc.pid, self.h))
        loaded, last = False, None
        deadline = self.h.mono() + READY_MAX_S
        while self.h.mono() < deadline:
            if self.h.poll(proc) is not None:
                break
            try:
                last = self.h.health("127.0.0.1", PROD_PORT, 10.0)
                if last.get("loaded") is True:
                    loaded = True
                    break
            except (OSError, ValueError) as exc:
                last = {"error": str(exc)}
            self.h.sleep(READY_STEP_S)
        self.m.event("restore_health", loaded=loaded, health=last)
        if not loaded:
            self.restore["status"] = "RESTORE_UNVERIFIED: health never reported loaded=true"
            return
        wrapper = record("restored-wrapper", proc.pid, self.h)
        problems = []
        if not argv_is_canonical(list(wrapper.argv), str(self.a.config)):
            problems.append(f"argv {list(wrapper.argv)} is not canonical "
                            f"{canonical_argv(str(self.a.config))}")
        if wrapper.cwd != str(MERGED_CWD):
            problems.append(f"cwd {wrapper.cwd} != {MERGED_CWD}")
        engine, vision, mcp = config_aux_classes(prod)
        kids, cls = [], []
        for pid in self.h.children(wrapper.pid):
            kid = record("restored-child", pid, self.h)
            kids.append(kid)
            cls.append(classify_child(kid, engine, vision, mcp, self.attest))
        if not kids:
            problems.append("no child under the restored wrapper")
        engines = [k for k, c in zip(kids, cls) if c == "engine"]
        if not engines:
            problems.append(f"no canonical engine child ({engine}) under the restored wrapper")
        problems += [f"engine child {k.pid} argv {list(k.argv)} does not match the expected "
                     f"engine args" for k in engines
                     if not engine_intent_matches(prod, k.argv)[0]]
        problems += [f"child {k.pid} {list(k.argv)} matches nothing configured "
                     f"(engine/vision/MCP of the canonical config)"
                     for k, c in zip(kids, cls) if c is None]
        ghosts = [i.brief() for i in self.owned
                  if i.kind in candidates and not exited_proven(i, self.h)]
        if ghosts:
            problems.append(f"candidate processes alive: {ghosts}")
        self.restore.update(wrapper=wrapper.brief(),
                            children=[dict(k.brief(), kind=c or "unknown")
                                      for k, c in zip(kids, cls)],
                            problems=problems, socket_files=[str(s) for s in self.sockets])
        self.restore.update(ok=not problems, status="restored and verified" if not problems
                            else "RESTORE_UNVERIFIED: " + "; ".join(problems),
                            build_sha256=build_sha)
        self.m.event("restore_verified", **{k: v for k, v in self.restore.items()
                                            if k != "survivors"})

    def cleanup_active(self, why: str) -> None:
        """Tear down EVERY registered chain; records all failures; never raises; a surviving
        process marks the driver blocked."""
        for c in sorted(self.active, key=lambda x: x.stage):
            try:
                self.stop_chain(c, why)
            except BaseException as exc:
                self.capture_failures.append(f"cleanup {c.stage}: {type(exc).__name__}: {exc}")
                self.m.event("cleanup_failed", stage=c.stage,
                             error=f"{type(exc).__name__}: {exc}")
                self.blocked = True

    def arm(self, stage: str, arm: str, phase: str, build_sha: str,
            fault: str | None = None) -> None:
        """One stage: fresh registered chain, its captures, then teardown.  A failure is
        recorded (with the chain torn down) and the run proceeds to cleanup and the canonical
        restore; there is no retry and no automatic restart."""
        chain = None
        abort: BaseException | None = None
        try:
            chain = self.start_chain(stage, arm, build_sha)      # registered inside
            if fault:
                self.run_fault(fault, build_sha, chain)
            else:
                self.run_sample(stage, arm, phase, build_sha, chain)
        except (KeyboardInterrupt, SystemExit) as exc:           # an interrupt ABORTS the run:
            abort = exc                                          # record, tear down, re-raise
            self.m.event("run_interrupted", fault=fault or stage,
                         error=f"{type(exc).__name__}: {exc}")
            self.capture_failures.append(f"interrupted at {fault or stage}: {exc}")
            chain = chain or getattr(exc, "chain", None)
        except (StageFailure, OwnedSignal, Refusal, OSError) as exc:
            self.m.event("fault_failed" if fault else "stage_failed",
                         fault=fault or stage, error=f"{type(exc).__name__}: {exc}")
            self.capture_failures.append(f"{'fault ' + fault if fault else 'stage ' + stage}: "
                                          f"{exc}")
            chain = chain or getattr(exc, "chain", None)
        except BaseException as exc:
            self.m.event("fault_failed" if fault else "stage_failed",
                         fault=fault or stage,
                         error=f"unexpected {type(exc).__name__}: {exc}")
            self.capture_failures.append(f"{'fault ' + fault if fault else 'stage ' + stage}: "
                                          f"unexpected {type(exc).__name__}: {exc}")
        if chain is not None:
            try:
                self.stop_chain(chain, f"end-of-{stage}")
            except BaseException as exc:
                self.capture_failures.append(f"teardown {stage}: {type(exc).__name__}: {exc}")
                self.m.event("cleanup_failed", stage=stage,
                             error=f"{type(exc).__name__}: {exc}")
                self.blocked = True
        if abort is not None:
            raise abort                                          # execute: no further stages


# ------------------------------------------------------------------ plan / run

def build_ids(args: argparse.Namespace, prod: dict[str, Any], h: Hooks) -> dict[str, Any]:
    """Bind measurements to the binaries that produced them: composite digest over driver,
    capture utility, candidate engine/helper, canonical config and production binary."""
    parts = {"driver_py": Path(__file__), "capture_py": UTILITY, "engine_hip": CAND_ENGINE,
             "helper_cuda": CAND_HELPER, "canonical_config": Path(args.config),
             "production_engine": Path(prod["exe"])}
    digests = {name: h.sha256(path) for name, path in parts.items()}
    blob = json.dumps(digests, sort_keys=True).encode("utf-8")
    return {"parts": {k: str(v) for k, v in parts.items()}, "sha256": digests,
            "build_id": hashlib.sha256(blob).hexdigest()[:24]}


def prepare(args: argparse.Namespace, h: Hooks) -> dict[str, Any]:
    """Read the canonical config NOW and derive everything; no writes outside the fresh run dir."""
    if args.out_dir.exists():
        raise Refusal(f"run directory exists, refusing to reuse: {args.out_dir}")
    prod = json.loads(Path(args.config).read_text(encoding="utf-8"))
    for path in (PY, LAUNCHER, MERGED_CWD, UTILITY, CAND_ENGINE, CAND_HELPER,
                 MERGED_CWD / "serve/server.py", ROOT / "serve/server.py"):
        if not path.exists():
            raise Refusal(f"required path missing: {path}")
    args.out_dir.mkdir(parents=True)
    base = derive_base(prod)
    off, on = arm_config(base, "off", "off-1", args.out_dir), arm_config(base, "on", "on-1",
                                                                          args.out_dir)
    socket_path(args.out_dir, "fault-decode")        # AF_UNIX length check, longest stage label
    requests = {"request-sample.json": request_payload(SAMPLE_PROMPT, 256, False),
                "request-fault-prefill.json": request_payload(PREFILL_PROMPT, 120, True),
                "request-fault-decode.json": request_payload(DECODE_PROMPT, 768, True)}
    for name, payload in requests.items():
        write_json(args.out_dir / name, payload)
    write_json(args.out_dir / "config-off.json", off)
    write_json(args.out_dir / "config-on.json", on)
    ids = build_ids(args, prod, h)
    write_json(args.out_dir / "diffs.json", {"off_vs_canonical": _diff(prod, off),
                                             "on_vs_canonical": _diff(prod, on),
                                             "on_vs_off": _diff(off, on)})
    write_json(args.out_dir / "build-sha.json", dict(ids, tool_version=TOOL_VERSION))
    m = Manifest(args.out_dir / "manifest.jsonl", h)
    m.event("plan", version=TOOL_VERSION, canonical_config=str(args.config),
            stages=[s[0] for s in STAGES], faults=["prefill", "decode"], helper_argv=helper_argv(),
            telemetry=TELEMETRY, build_sha256=ids["build_id"], lock_path=str(LOCK_PATH),
            on_socket=on["expert_helper_socket"])
    return {"prod": prod, "base": base, "off": off, "on": on, "ids": ids,
            "build_sha": ids["build_id"], "manifest": m}


def plan(args: argparse.Namespace, h: Hooks) -> int:
    p = prepare(args, h)
    prod, off, on = p["prod"], p["off"], p["on"]
    diffs = {"off_vs_canonical": _diff(prod, off), "on_vs_canonical": _diff(prod, on),
             "on_vs_off": _diff(off, on)}
    view = {"mode": "plan", "run_dir": str(args.out_dir),
            "stages": [{"stage": s, "arm": a, "phase": ph,
                        "engine_log": str(args.out_dir / f"engine-{s}.log")}
                       for s, a, ph in STAGES] +
                      [{"stage": f"fault-{k}", "arm": "on", "phase": "warm",
                        "engine_log": str(args.out_dir / f"engine-fault-{k}.log")}
                       for k in ("prefill", "decode")],
            "faults": ["prefill", "decode"], "diffs": diffs, "helper_argv": helper_argv(),
            "wrapper_argv_template": [str(PY), str(ROOT / "serve/server.py"), "--engine",
                                      "strata", "--config", "<run>/config-<stage>.json",
                                      "--port", str(CAND_PORT)],
            "wrapper_cwd": str(MERGED_CWD), "build_sha256": p["build_sha"],
            "build_ids": p["ids"], "lock_path": str(LOCK_PATH),
            "restore": {"launcher": str(LAUNCHER), "port": PROD_PORT},
            "claims": {"acceptance_pass": False, "phase_proven": False, "speed_claim": False,
                       "measured_by_this_tool": False}}
    write_json(args.out_dir / "plan.json", view)
    print(json.dumps(view, indent=1, sort_keys=True, default=str))
    return EXIT_DONE


def default_run_dir() -> Path:
    """Fresh default run directory.  The helper socket must fit AF_UNIX (< 108 bytes), so a
    worktree path that is already too long is not used - /tmp/agent is short enough.  The
    check uses the LONGEST stage label; a `--out-dir` passed by the caller is checked by the
    same rule in prepare()."""
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
    for base in (ROOT / "build-mixed-gpu/r3-driver", Path("/tmp/agent/mixed-gpu-r3")):
        if len(os.fsencode(base / f"run-{stamp}" / "helper-fault-decode.sock")) < AFUNIX_MAX:
            return base / f"run-{stamp}"
    raise Refusal("no default run directory keeps the helper socket inside the AF_UNIX limit; "
                  "pass --out-dir with a shorter path")


def acquire_lock(h: Hooks) -> Any:
    """One driver at a time, GLOBALLY: an exclusive non-blocking flock on LOCK_PATH (a lock in
    some other --out-dir does not allow a second driver)."""
    import fcntl
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock = LOCK_PATH.open("a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        raise Refusal(f"another driver holds the exclusive run lock {LOCK_PATH}; refusing to "
                      f"overlap")
    return lock


def preflight(d: Driver, args: argparse.Namespace, prod: dict[str, Any]
              ) -> tuple[Ident, list[Ident]]:
    ident = expected_wrapper_identity(d.h, args.expected_wrapper_pid,
                                      args.expected_wrapper_starttime, str(args.config))
    ensure_candidate_port_free(d.h)
    if d.attest:                                               # record the allowance we will use
        servers = prod.get("mcp_servers") or {}
        backed = {}
        for name, (target, tokens) in d.attest.items():
            spec = servers.get(name)
            script = str(spec.get("command")) if isinstance(spec, dict) and spec.get("command") \
                else None
            digest = None
            if script and Path(script).exists():               # the script whose exec we allow
                digest = d.h.sha256(Path(script))
            backed[name] = {"configured_command": script, "command_sha256": digest,
                            "exec_target": target, "tokens": list(tokens)}
        d.m.event("aux_attestations", attestations=backed)
    kids = production_children(ident, prod, d.h, d.attest)
    d.m.event("verified", wrapper=ident.brief(), children=[k.brief() for k in kids])
    return ident, kids


def execute(args: argparse.Namespace, h: Hooks) -> int:
    p = prepare(args, h)
    prod, base, build_sha, m = p["prod"], p["base"], p["build_sha"], p["manifest"]
    d = Driver(args, h, m, base)
    lock = acquire_lock(h)                       # global exclusivity, held for the whole run
    status = EXIT_DONE
    try:
        try:
            ident, kids = preflight(d, args, prod)
        except Refusal as exc:                   # pre-flight refusal: NOTHING was signalled
            m.event("run_refused", error=str(exc))
            status = EXIT_REFUSED
        else:
            try:
                d.stop_production(ident, kids)
                for stage, arm, phase in STAGES:
                    if d.blocked:                # unclean cleanup: stop advancing stages
                        break
                    d.arm(stage, arm, phase, build_sha)
                for kind in ("prefill", "decode"):
                    if d.blocked:
                        break
                    d.arm(f"fault-{kind}", "on", "warm", build_sha, fault=kind)
            except (KeyboardInterrupt, SystemExit, Refusal, OwnedSignal, StageFailure,
                    OSError) as exc:
                m.event("run_aborted", error=f"{type(exc).__name__}: {exc}")
                d.capture_failures.append(f"aborted: {exc}")
                status = EXIT_FAIL
            except BaseException as exc:         # unexpected: recorded, never suppressed
                m.event("run_unexpected", error=f"{type(exc).__name__}: {exc}")
                d.capture_failures.append(f"unexpected: {type(exc).__name__}: {exc}")
                status = EXIT_FAIL
    finally:                                     # cleanup and restore ALWAYS run, never a
        try:                                     # return in here, never a silent success
            d.cleanup_active("run-end")
        except BaseException as exc:
            d.capture_failures.append(f"cleanup raised: {type(exc).__name__}: {exc}")
            m.event("cleanup_exception", error=f"{type(exc).__name__}: {exc}")
        try:
            d.restore_production(prod, build_sha)
        except BaseException as exc:
            d.restore.update(ok=False, status=f"restore raised {type(exc).__name__}: {exc}")
            m.event("restore_failed", error=f"{type(exc).__name__}: {exc}")
        if d.blocked:
            d.capture_failures.append("run blocked by a surviving process")
        if d.capture_failures or not d.restore["ok"]:
            status = EXIT_FAIL if status != EXIT_REFUSED else EXIT_REFUSED
        summary = {"mode": "execute", "exit": status, "run_dir": str(d.run_dir),
                   "restoration_required": d.restoration_required,
                   "production_stopped": d.production_stopped, "blocked": d.blocked,
                   "capture_failures": d.capture_failures, "restore": d.restore,
                   "claims": {"acceptance_pass": False, "phase_proven": False,
                              "speed_claim": False}, "manifest": str(m.path)}
        write_json(d.run_dir / "summary.json", summary)
        m.event("summary", **summary)
        lock.close()
        print(json.dumps(summary, indent=1, sort_keys=True, default=str))
    return status


def main(argv: list[str] | None = None, hooks: Hooks | None = None) -> int:
    ap = argparse.ArgumentParser(prog="run_acceptance.py",
                                 description="mixed-GPU hardware-window driver (raw evidence)")
    ap.add_argument("--execute", action="store_true",
                    help="actually run; the default is a dry plan with no side effects")
    ap.add_argument("--expected-wrapper-pid", type=int, default=0,
                    help="with --execute: the live production wrapper PID (required)")
    ap.add_argument("--expected-wrapper-starttime", type=int, default=0,
                    help="with --execute: /proc starttime of that wrapper (required)")
    ap.add_argument("--config", type=Path, default=None)
    ap.add_argument("--out-dir", type=Path, default=None)
    ap.add_argument("--expect-aux", action="append", default=[],
                    help="with --execute: NAME=TARGET_PATH:TOKEN,TOKEN - an explicit parent "
                         "attestation for an MCP child whose configured command execs TARGET "
                         "(exact realpath + exact tokens, no shell resolution); default none")
    a = ap.parse_args(argv if argv is not None else sys.argv[1:])
    if a.config is None:
        a.config = PROD_CONFIG
    h = hooks or Hooks()
    if a.out_dir is None:
        a.out_dir = default_run_dir()
    if a.execute and (a.expected_wrapper_pid <= 0 or a.expected_wrapper_starttime <= 0):
        print("REFUSED: --execute needs --expected-wrapper-pid and --expected-wrapper-starttime",
              file=sys.stderr)
        return EXIT_REFUSED
    if a.execute and Path(a.config).resolve() != PROD_CONFIG.resolve():
        print(f"REFUSED: --execute requires the canonical config {PROD_CONFIG}, not {a.config}",
              file=sys.stderr)
        return EXIT_REFUSED
    if a.execute:
        def _term(*_: Any) -> None:
            raise KeyboardInterrupt("SIGTERM")
        signal.signal(signal.SIGTERM, _term)
    try:
        parse_expect_aux(a.expect_aux)          # validate the attestation format in EVERY mode
        return plan(a, h) if not a.execute else execute(a, h)
    except Refusal as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
