"""CPU-only tests for tools/mixed_gpu/run_acceptance.py (the mixed-GPU hardware-window driver).

Everything is fake and SELF-CONTAINED: a fixture host tree under tmp_path with a minimal
canonical config (vision + MCP servers configured), patched driver path constants, fake procfs
(argv / cwd / exe-inode / starttime / children, including zombies), fake `ss` listeners, fake
pidfd (open/signal/death recorded, ESRCH as PidfdGone, never a real signal), fake health, a
fake acceptance.py subprocess and a fake clock.  No real host paths (no /mnt/user, no venv,
no launcher, no ROCm binaries, no production config) are read, and no real network, service,
GPU, engine, production process or signal is used - `no_network_plugin` proves the suite is
socket-free.  The single permitted real-process test spawns a CPU-only `time.sleep` child and
drives the real pidfd/reap path: no network, no GPU, no service.

The pinned contract these tests enforce:

  plan      : the default performs NO side effect - no popen, no listeners, no pidfd, no signal,
              no health, no acceptance.py call; artifacts go only into a FRESH run directory;
              the AF_UNIX check uses the LONGEST stage label (fault-decode).
  identity  : --execute needs the expected wrapper PID + starttime and the canonical --config;
              a non-listener PID, a stale starttime, a non-canonical argv/cwd, a child matching
              nothing configured, no engine child, a busy :8019, an existing run dir, an AF_UNIX
              path >= 108, an existing socket, an occupied :8009 at restore, the GLOBAL flock, a
              missing identity/path are refusals BEFORE any signal.
  children  : direct children are classified against the config: engine exe, vision exe, MCP by
              exact configured command token (exec'd script or python-interpreter form; never
              arbitrary python).  ALL identities are recorded; wrapper TERM owns teardown; known
              children are identity-bound TERM'd after the wrapper; their confirmed exit is
              required before candidate start; survivors block the run and the restore.
  signals   : only pidfd-bound, re-verified identities are signalled; a changed exe inode,
              starttime or argv refuses and sends nothing; a proven-exited process (ESRCH,
              POLLIN, empty cmdline / unreadable /proc/exe of a zombie) is recorded as skipped
              and never signalled; our own children are reaped after pidfd-proven exit.  The
              driver source uses no os.kill/killpg/pkill/pgrep/unlink/start_new_session
              (AST-checked, docstrings excluded).
  lifecycle : the chain registers for cleanup BEFORE its first process starts; cleanup and the
              restore always run (never a return in finally); cleanup/restore exceptions are
              recorded; a surviving process blocks further stages; a capture timeout is recorded
              as rc 124 with partial output.
  restore   : restoration_required flips BEFORE the production signal; the canonical launcher
              runs exactly once; candidate cleanup happens BEFORE restore; survivors are
              RESTORE_BLOCKED (never a competing service); an unrelated :8009 occupant is a
              CONFLICT (never killed); restored verification allows configured vision/MCP
              children, requires >=1 canonical engine child with the expected engine args, and
              rejects children matching nothing configured.
  arms      : OFF/ON differ ONLY by helper keys (socket/slots 3400/timeout 100); OFF strips
              every inherited expert_helper_* key; every stage has a UNIQUE engine log and all
              six are kept; mcp_servers={} and the vision removal are identical in both arms;
              the helper argv is exact; the wrapper env is the cleaned launcher env; the
              build-id is a COMPOSITE over driver/capture/candidate binaries/config/production.
  capture   : acceptance.py runs as a separate subprocess per call with a FRESH --out and an
              explicit request/log/chain/build-id; rc/stdout/stderr are recorded; a non-zero rc
              is a recorded capture failure (never a PASS) and forces a non-zero exit.
  readiness : bounded internal polling with early child-exit detection; the helper socket is
              never connected - readiness comes from log markers only.
"""
from __future__ import annotations

import ast
import dataclasses
import hashlib
import importlib.util
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("driver_under_test",
                                              ROOT / "tools" / "mixed_gpu" / "run_acceptance.py")
assert SPEC and SPEC.loader
drv = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = drv
SPEC.loader.exec_module(drv)

ASPEC = importlib.util.spec_from_file_location("capture_under_test",
                                               ROOT / "tools" / "mixed_gpu" / "acceptance.py")
assert ASPEC and ASPEC.loader
acc = importlib.util.module_from_spec(ASPEC)          # the frozen r3-2 capture utility
sys.modules[ASPEC.name] = acc
ASPEC.loader.exec_module(acc)

PROD_PID, PROD_START, ENGINE_PID, VISION_PID = 4001, 111, 4002, 4003
MCP_PIDS = (4004, 4005, 4006)


def fixture_config(host: Path) -> dict:
    """A MINIMAL canonical config for the tests - never the real production file."""
    return {
        "exe": str(host / "prod" / "strata"),
        "cwd": str(host / "prod"),
        "port": 8009,
        "log": str(host / "prod" / "engine.log"),
        "args": ["--model", str(host / "prod" / "m.gguf"), "--ctx", "3400"],
        "layer_split": "16,32",
        "env": {"STRATA_ENGINE_NICE": "0"},
        "vision": {"exe": str(host / "prod" / "vision"), "args": ["--encoder", "cpu"]},
        "mcp_servers": {
            "pandoc": {"command": str(host / "bin" / "mcp-pandoc"), "args": ["--stdio"]},
            # canonical docling nuance: the CONFIGURED command is a wrapper that execs another
            # file, so the configured token is NOT in the child's argv
            "docling": {"command": str(host / "bin" / "mcp-docling"), "args": ["--stdio"],
                        "exec_target": str(host / "bin" / "docling-mcp-server")},
            "markitdown": {"command": str(host / "bin" / "mcp-markitdown"), "args": ["--stdio"]},
        },
        "expert_helper_socket": "/stale/from/canonical/helper.sock",
        "expert_helper_layers": "ffn",
        "expert_helper_slots": 3400,
        "expert_helper_reserve_mib": 512,
        "expert_helper_timeout": 100,
    }


@dataclasses.dataclass
class Proc:
    pid: int
    argv: list[str]
    cwd: str
    exe: str
    dev: int
    ino: int
    starttime: int
    children: list[int]
    alive: bool = True
    rc: int | None = None
    exit_after_signal: bool = True
    ignore_signals: bool = False
    zombie: bool = False


class World:
    """The fake host: procfs, listeners, pidfd, health, acceptance.py subprocess, clock."""

    def __init__(self, run_dir: Path, cfg: dict) -> None:
        self.run_dir, self.cfg = run_dir, cfg
        self.canon = os.path.realpath(cfg["exe"])
        self.host = Path(cfg["mcp_servers"]["pandoc"]["command"]).parent.parent
        self.procs: dict[int, Proc] = {}
        self.listener_rows: dict[int, list[dict]] = {drv.PROD_PORT: [{
            "addr": f"127.0.0.1:{drv.PROD_PORT}", "pid": PROD_PID,
            "line": f"LISTEN 127.0.0.1:{drv.PROD_PORT} pid={PROD_PID}"}]}
        self.next_pid, self.next_fd = 5000, 90
        self.fds: dict[int, int] = {}
        self.signals: list[tuple[int, int]] = []
        self.popen_calls: list[dict] = []
        self.health_calls: list[dict] = []
        self.captures: list[list[str]] = []
        self.now, self.health_loaded = 1000.0, True
        self.watchdog_on_signal: tuple[int, int] | None = None
        self.wrap_argv = [str(drv.PY), "serve/server.py", "--engine", "strata", "--config",
                          str(drv.PROD_CONFIG), "--port", str(drv.PROD_PORT)]
        self.add(Proc(PROD_PID, list(self.wrap_argv), str(drv.MERGED_CWD), self.canon, 7, 1000,
                      PROD_START, [ENGINE_PID, VISION_PID, *MCP_PIDS]))
        self.add(Proc(ENGINE_PID, drv.expected_engine_argv({**cfg, "exe": self.canon}),
                      cfg["cwd"], self.canon, 7, 1001, 120, []))
        self.add(Proc(VISION_PID, [cfg["vision"]["exe"]] + cfg["vision"]["args"], cfg["cwd"],
                      os.path.realpath(cfg["vision"]["exe"]), 7, 1002, 121, []))
        for i, name in enumerate(("pandoc", "docling", "markitdown")):
            spec = cfg["mcp_servers"][name]
            argv = [spec["command"], "--stdio"] if i < 2 else ["python3", spec["command"],
                                                                "--stdio"]
            self.add(Proc(MCP_PIDS[i], argv, cfg["cwd"], os.path.realpath(argv[0]), 7,
                          1003 + i, 122 + i, []))

    def docling_exec_form(self):
        """Switch the docling child to the canonical nuance: the configured wrapper execs
        TARGET --transport stdio, so the configured token is gone from argv."""
        target = self.cfg["mcp_servers"]["docling"]["exec_target"]
        kid = self.procs[MCP_PIDS[1]]
        kid.argv, kid.exe = [target, "--transport", "stdio"], os.path.realpath(target)

    def docling_interpreter_form(self):
        """The REAL inventory shape (parent r3 inventory): argv[0] is the uv interpreter,
        argv[1] is the exec'd script, tokens follow.  Attestation covers interpreter + script +
        tokens; nothing is resolved from the configured wrapper command."""
        interp = str(self.host / "bin" / "uvdocling-python")
        script = self.cfg["mcp_servers"]["docling"]["exec_target"]
        kid = self.procs[MCP_PIDS[1]]
        kid.argv = [interp, script, "--transport", "stdio"]
        kid.exe = os.path.realpath(interp)

    def add(self, p: Proc) -> Proc:
        self.procs[p.pid] = p
        return p

    # ------------------------------------------------------------- procfs seams
    def cmdline(self, pid: int) -> list[str]:
        return [] if self.procs[pid].zombie else self.procs[pid].argv

    def starttime(self, pid: int) -> int:
        if self.procs[pid].zombie:
            raise OSError("No such process: /proc/pid/stat")
        return self.procs[pid].starttime

    def exe(self, pid: int) -> tuple[str, int, int]:
        p = self.procs[pid]
        if p.zombie:
            raise OSError("No such process: /proc/pid/exe")
        return p.exe, p.dev, p.ino

    def cwd(self, pid: int) -> str:
        return self.procs[pid].cwd

    def children(self, pid: int) -> list[int]:
        return list(self.procs[pid].children)

    def alive(self, pid: int) -> bool:
        return pid in self.procs and self.procs[pid].alive

    def listeners(self, port: int) -> list[dict]:
        return list(self.listener_rows.get(port, []))

    # ------------------------------------------------------------- pidfd seams
    def pidfd_open(self, pid: int) -> int:
        if pid not in self.procs:                                       # ESRCH
            raise drv.PidfdGone(f"pidfd_open({pid}): ESRCH, process is gone")
        fd = self.next_fd
        self.next_fd, self.fds[fd] = fd + 1, pid
        return fd

    def pidfd_signal(self, fd: int, sig: int) -> None:
        pid = self.fds[fd]
        self.signals.append((pid, int(sig)))
        p = self.procs[pid]
        if p.ignore_signals:
            return
        p.alive, p.rc = False, -int(sig)
        for kid in p.children:                        # teardown of children that go with it
            k = self.procs.get(kid)
            if k is not None and k.alive and k.exit_after_signal and not k.ignore_signals:
                k.alive, k.rc = False, -int(sig)
        if pid == PROD_PID:
            self.listener_rows.pop(drv.PROD_PORT, None)

    def pidfd_death(self, fd: int, timeout: float) -> bool:
        p = self.procs[self.fds[fd]]
        return p.zombie or not p.alive

    def pidfd_close(self, fd: int) -> None:
        self.fds.pop(fd, None)

    # ------------------------------------------------------------- popen / health / capture
    def popen(self, argv: list, cwd: str, env: dict, out: str, err: str) -> Proc:
        argv = [str(a) for a in argv]
        requested = list(argv)                                    # what the driver asked for
        pid = self.next_pid
        self.next_pid += 1
        if argv == [str(drv.LAUNCHER)]:                           # the launcher execs the wrapper
            argv, cwd = list(self.wrap_argv), str(drv.MERGED_CWD)
        kids: list[int] = []
        if "--port" in argv:
            cfg = json.loads(Path(argv[argv.index("--config") + 1]).read_text(encoding="utf-8"))
            kid = self.next_pid
            self.next_pid += 1
            self.add(Proc(kid, drv.expected_engine_argv(cfg), cfg["cwd"],
                          os.path.realpath(cfg["exe"]), 7, 2000 + kid, 150, []))
            kids = [kid]
            if argv[-1] == str(drv.PROD_PORT):                    # the canonical service also
                for aux in (cfg["vision"], *[cfg["mcp_servers"][n] for n in            # spawns these
                                             cfg["mcp_servers"]]):
                    a = self.next_pid
                    self.next_pid += 1
                    self.add(Proc(a, [aux["exe"] if "exe" in aux else aux["command"],
                                      *(aux.get("args") or [])], cfg["cwd"],
                                  os.path.realpath(aux.get("exe") or aux["command"]), 7,
                                  2100 + a, 151, []))
                    kids.append(a)
            log = Path(cfg["log"])
            if log.is_relative_to(self.run_dir):          # never write a real host path in tests
                log.parent.mkdir(parents=True, exist_ok=True)
                log.write_text("", encoding="utf-8")
            Path(out).write_text(f"{drv.WRAPPER_READY}\n", encoding="utf-8")
            if argv[-1] == str(drv.PROD_PORT):
                self.listener_rows[drv.PROD_PORT] = [{"addr": "", "pid": pid, "line": "LISTEN 8009"}]
        if argv[0] == str(drv.CAND_HELPER):
            Path(out).write_text(f"{drv.HELPER_READY} {argv[argv.index('--socket') + 1]}\n",
                                 encoding="utf-8")
        self.popen_calls.append({"argv": requested, "exec_argv": argv, "cwd": cwd,
                                 "env": dict(env), "pid": pid})
        return self.add(Proc(pid, argv, cwd, os.path.realpath(argv[0]), 7, 3000 + pid, 200, kids))

    def poll(self, p: Proc):
        return p.rc

    def reap(self, p: Proc):
        if p.rc is not None:
            p.alive = False                       # the fake kernel removes the zombie on reap
        return p.rc

    def health(self, host: str, port: int, timeout: float) -> dict:
        self.health_calls.append({"host": host, "port": port})
        return {"status": "ok", "loaded": self.health_loaded, "model": "swift-1.5-iq3_xxs"}

    def capture(self, argv: list, cwd: str, timeout: float) -> tuple[int, str, str]:
        argv = [str(a) for a in argv]
        self.captures.append(argv)
        Path(argv[argv.index("--out") + 1]).mkdir(parents=True, exist_ok=True)
        return 0, "captured", ""

    def sha(self, path: Path) -> str:
        return hashlib.sha256(str(path).encode()).hexdigest()      # never hash 40 MB binaries

    def sleep(self, s: float) -> None:
        self.now += s

    def hooks(self) -> drv.Hooks:
        # every seam is a lambda so a test may swap a World method AFTER the hooks are built
        return drv.Hooks(popen=lambda *a: self.popen(*a), poll=lambda p: self.poll(p),
                         reap=lambda p: self.reap(p), pidfd_open=lambda pid: self.pidfd_open(pid),
                         pidfd_signal=lambda fd, sig: self.pidfd_signal(fd, sig),
                         pidfd_death=lambda fd, t: self.pidfd_death(fd, t),
                         pidfd_close=lambda fd: self.pidfd_close(fd),
                         cmdline=lambda pid: self.cmdline(pid),
                         starttime=lambda pid: self.starttime(pid), exe=lambda pid: self.exe(pid),
                         cwd=lambda pid: self.cwd(pid), children=lambda pid: self.children(pid),
                         alive=lambda pid: self.alive(pid), listeners=lambda port: self.listeners(port),
                         health=lambda *a: self.health(*a), capture=lambda *a: self.capture(*a),
                         sha256=lambda p: self.sha(p), wall=lambda: self.now,
                         mono=lambda: self.now, sleep=lambda s: self.sleep(s))

    def events(self) -> list[dict]:
        path = self.run_dir / "manifest.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] \
            if path.exists() else []

    def kinds(self) -> list[str]:
        return [e["event"] for e in self.events()]

    def summary(self) -> dict:
        return json.loads((self.run_dir / "summary.json").read_text(encoding="utf-8"))


_SHORT_BASE = ROOT / "build-mixed-gpu" / "t"
_HOST_SEQ = iter(range(1, 100000))


def _short_root() -> Path:
    _SHORT_BASE.mkdir(parents=True, exist_ok=True)
    return _SHORT_BASE


@pytest.fixture
def host(tmp_path_factory, monkeypatch):
    """A portable fixture host: NO real /mnt/user path, venv, launcher, config or binary.
    It lives in a SHORT project directory so the driver's AF_UNIX socket check is about the
    driver's rule, not about pytest's long per-test temp names."""
    hs = _short_root() / f"h{next(_HOST_SEQ)}"
    hs.mkdir(parents=True)
    (hs / "bin").mkdir(parents=True)
    (hs / "prod").mkdir(parents=True)
    (hs / "merged" / "serve").mkdir(parents=True)
    (hs / "build" / "engine-hip").mkdir(parents=True)
    (hs / "build" / "engine-cuda").mkdir(parents=True)
    (hs / "build" / "r3-driver").mkdir(parents=True)
    (hs / "tools").mkdir(parents=True)
    (hs / ".venv").mkdir()
    for f in (hs / "bin" / "mcp-pandoc", hs / "bin" / "mcp-docling", hs / "bin" /
              "mcp-markitdown", hs / "bin" / "docling-mcp-server", hs / "prod" / "strata",
              hs / "prod" / "vision",
              hs / "build" / "engine-hip" / "strata", hs / "build" / "engine-cuda" /
              "strata-expert-helper", hs / ".venv" / "py"):
        f.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        f.chmod(0o755)
    (hs / "merged" / "serve" / "server.py").write_text("# fixture\n", encoding="utf-8")
    (hs / "tools" / "capture.py").write_text("# fixture capture\n", encoding="utf-8")
    cfg = fixture_config(hs)
    (hs / "config.json").write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    launcher = hs / "launch-prod.sh"
    launcher.write_text("#!/bin/sh\nexec \"$PY\" serve/server.py --engine strata --config \"$CFG\" "
                        "--port 8009\n", encoding="utf-8")
    for name, value in {"PY": hs / ".venv" / "py", "LAUNCHER": launcher,
                        "PROD_CONFIG": hs / "config.json", "MERGED_CWD": hs / "merged",
                        "CAND_ENGINE": hs / "build" / "engine-hip" / "strata",
                        "CAND_HELPER": hs / "build" / "engine-cuda" / "strata-expert-helper",
                        "UTILITY": hs / "tools" / "capture.py",
                        "LOCK_PATH": hs / "build" / "r3-driver" / "driver.lock"}.items():
        monkeypatch.setattr(drv, name, value)
    return hs


@pytest.fixture
def world(host):
    cfg = json.loads((host / "config.json").read_text(encoding="utf-8"))
    return World(_short_root() / f"r{next(_HOST_SEQ)}", cfg)


@pytest.fixture(autouse=True)
def _clean_short_root():
    import shutil
    yield
    shutil.rmtree(_SHORT_BASE, ignore_errors=True)


def run(world, extra=()):
    argv = ["--execute", "--expected-wrapper-pid", str(PROD_PID), "--expected-wrapper-starttime",
            str(PROD_START), "--config", str(drv.PROD_CONFIG), "--out-dir",
            str(world.run_dir)] + list(extra)
    return drv.main(argv, world.hooks())


# ------------------------------------------------------------------ default plan

def test_default_plan_performs_no_side_effects(world, host):
    rc = drv.main(["--out-dir", str(world.run_dir), "--config", str(drv.PROD_CONFIG)],
                  world.hooks())
    assert rc == drv.EXIT_DONE
    assert world.popen_calls == [] and world.signals == [] and world.captures == []
    assert world.health_calls == []
    names = sorted(p.name for p in world.run_dir.iterdir())
    assert names == sorted(["config-off.json", "config-on.json", "manifest.jsonl",
                            "build-sha.json", "diffs.json", "plan.json",
                            "request-fault-decode.json", "request-fault-prefill.json",
                            "request-sample.json"])
    plan = json.loads((world.run_dir / "plan.json").read_text(encoding="utf-8"))
    assert plan["claims"] == {"acceptance_pass": False, "phase_proven": False,
                              "speed_claim": False, "measured_by_this_tool": False}
    assert [s["stage"] for s in plan["stages"]] == ["off-1", "on-1", "on-2", "off-2",
                                                    "fault-prefill", "fault-decode"]
    assert plan["faults"] == ["prefill", "decode"]
    assert plan["wrapper_cwd"] == str(drv.MERGED_CWD)
    assert plan["lock_path"] == str(drv.LOCK_PATH)
    logs = [s["engine_log"] for s in plan["stages"]]
    assert len(set(logs)) == 6 and all("engine-" in p for p in logs)


def test_plan_never_reuses_a_run_directory(world):
    argv = ["--out-dir", str(world.run_dir), "--config", str(drv.PROD_CONFIG)]
    assert drv.main(argv, world.hooks()) == drv.EXIT_DONE
    assert drv.main(argv, world.hooks()) == drv.EXIT_REFUSED


def test_execute_without_expected_identity_refuses_before_anything(world):
    assert drv.main(["--execute", "--out-dir", str(world.run_dir), "--config",
                     str(drv.PROD_CONFIG)], world.hooks()) == drv.EXIT_REFUSED
    assert world.signals == [] and world.popen_calls == []


def test_execute_refuses_a_non_canonical_config_path(world):
    rc = drv.main(["--execute", "--expected-wrapper-pid", str(PROD_PID),
                   "--expected-wrapper-starttime", str(PROD_START), "--config",
                   str(world.run_dir / "other.json"), "--out-dir", str(world.run_dir.parent /
                   "run2")], world.hooks())
    assert rc == drv.EXIT_REFUSED
    assert world.signals == [] and world.popen_calls == []


# ------------------------------------------------------------------ identity refusals

def test_execute_refuses_a_wrapper_that_is_not_the_listener(world):
    world.listener_rows[drv.PROD_PORT] = [{"addr": "", "pid": 9999, "line": "LISTEN pid=9999"}]
    assert run(world) == drv.EXIT_REFUSED
    assert world.signals == [] and world.popen_calls == []


def test_execute_refuses_a_stale_starttime(world):
    world.procs[PROD_PID].starttime = 999
    assert run(world) == drv.EXIT_REFUSED and world.signals == []


def test_execute_refuses_a_non_canonical_argv(world):
    world.procs[PROD_PID].argv = [str(drv.PY), "other/serve.py", "--port", "8009"]
    assert run(world) == drv.EXIT_REFUSED and world.signals == []


def test_execute_refuses_a_wrong_cwd(world):
    world.procs[PROD_PID].cwd = "/tmp"
    assert run(world) == drv.EXIT_REFUSED and world.signals == []


def test_execute_refuses_a_child_that_matches_nothing_configured(world):
    world.add(Proc(4009, ["python3", "/tmp/evil.py"], "/tmp", "/usr/bin/python3", 7, 1100, 130,
                   []))
    world.procs[PROD_PID].children.append(4009)
    assert run(world) == drv.EXIT_REFUSED
    assert world.signals == []                                    # refused BEFORE any signal


def test_execute_refuses_arbitrary_python_as_an_mcp_child(world):
    world.procs[MCP_PIDS[0]].argv = ["/usr/bin/python3", "/tmp/not-configured.py", "--stdio"]
    world.procs[MCP_PIDS[0]].exe = "/usr/bin/python3"
    assert run(world) == drv.EXIT_REFUSED and world.signals == []


def test_wrapper_exec_child_without_attestation_is_refused_before_any_signal(world):
    """Canonical docling nuance: configured command is a wrapper that execs another file, so
    the configured token is NOT in argv.  Without an attestation that child matches nothing."""
    world.docling_exec_form()
    doc = world.procs[MCP_PIDS[1]]
    assert doc.argv[0].endswith("docling-mcp-server")
    assert str(world.cfg["mcp_servers"]["docling"]["command"]) not in doc.argv
    assert run(world) == drv.EXIT_REFUSED
    assert world.signals == [] and world.popen_calls == []


def test_attested_exec_child_is_accepted_and_recorded_with_the_script_digest(world):
    world.docling_exec_form()
    target = world.cfg["mcp_servers"]["docling"]["exec_target"]
    spec = f"docling={target}:--transport,stdio"
    assert run(world, extra=["--expect-aux", spec]) == drv.EXIT_DONE
    att = [e for e in world.events() if e["event"] == "aux_attestations"]
    assert att and att[0]["attestations"]["docling"]["exec_target"] == os.path.realpath(target)
    assert att[0]["attestations"]["docling"]["command_sha256"]            # backed by the script
    pre = [e for e in world.events() if e["event"] == "production_pre_stop"][0]
    assert {c["kind"] for c in pre["children"]} == {"production-engine-child",
                                                    "production-vision-child",
                                                    "production-mcp-child"}
    assert len(pre["children"]) == 5
    assert world.summary()["restore"]["ok"] is True


def test_attestation_mismatch_is_a_refusal(world):
    world.docling_exec_form()
    wrong = f"docling={world.cfg['mcp_servers']['docling']['exec_target']}:--transport,wrong"
    assert run(world, extra=["--expect-aux", wrong]) == drv.EXIT_REFUSED
    assert world.signals == []
    far = f"docling=/nonexistent/path:--transport,stdio"
    assert run(world, extra=["--expect-aux", far]) == drv.EXIT_REFUSED
    assert world.signals == []


def test_execute_refuses_a_wrapper_with_no_engine_child(world):
    world.procs[PROD_PID].children = [VISION_PID, *MCP_PIDS]
    assert run(world) == drv.EXIT_REFUSED and world.signals == []


def test_execute_refuses_a_busy_candidate_port_before_any_signal(world):
    world.listener_rows[drv.CAND_PORT] = [{"addr": "", "pid": 7777, "line": "LISTEN 8019"}]
    assert run(world) == drv.EXIT_REFUSED
    assert world.signals == [] and world.popen_calls == []


def test_socket_path_length_is_checked_before_use(tmp_path):
    deep = tmp_path / ("d" * 60) / ("e" * 60)
    deep.mkdir(parents=True)
    with pytest.raises(drv.Refusal, match="AF_UNIX"):
        drv.socket_path(deep / "run", "fault-decode")


def test_default_run_dir_keeps_the_longest_socket_inside_af_unix():
    run_dir = drv.default_run_dir()
    assert len(os.fsencode(run_dir / "helper-fault-decode.sock")) < drv.AFUNIX_MAX
    assert run_dir.parent in (drv.ROOT / "build-mixed-gpu/r3-driver",
                              Path("/tmp/agent/mixed-gpu-r3"))


def test_execute_refuses_an_existing_socket_path():
    run_dir = _short_root() / "sock-exists"
    run_dir.mkdir(parents=True)
    (run_dir / "helper-on-1.sock").write_text("", encoding="utf-8")
    with pytest.raises(drv.Refusal, match="already exists"):
        drv.socket_path(run_dir, "on-1")


def test_execute_refuses_the_global_lock_from_a_different_out_dir(world, tmp_path):
    import fcntl
    lock = drv.LOCK_PATH.open("a+")                          # LOCK_PATH is the fixture's
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        argv = ["--execute", "--expected-wrapper-pid", str(PROD_PID),
                "--expected-wrapper-starttime", str(PROD_START), "--config",
                str(drv.PROD_CONFIG), "--out-dir", str(tmp_path / "elsewhere")]
        assert drv.main(argv, world.hooks()) == drv.EXIT_REFUSED
    finally:
        lock.close()
    assert world.signals == [] and world.popen_calls == []


def test_execute_refuses_a_missing_required_path(world, monkeypatch):
    monkeypatch.setattr(drv, "CAND_HELPER", drv.CAND_HELPER.parent / "absent-helper")
    assert run(world) == drv.EXIT_REFUSED
    assert world.signals == [] and world.popen_calls == []


def test_tests_use_only_the_fixture_tree(host, world):
    """Portability proof: every host constant the driver touches lives under the fixture."""
    for p in (drv.PY, drv.LAUNCHER, drv.PROD_CONFIG, drv.MERGED_CWD, drv.CAND_ENGINE,
              drv.CAND_HELPER, drv.UTILITY, drv.LOCK_PATH):
        assert str(p).startswith(str(host)), p
    assert drv.PROD_CONFIG.parent == host
    assert not any(str(p).startswith("/mnt/user/Strata/prod") for p in
                   (drv.PY, drv.LAUNCHER, drv.PROD_CONFIG, drv.MERGED_CWD))


# ------------------------------------------------------------------ signals

def signalled(world, ident, sig=signal.SIGTERM):
    world.run_dir.mkdir(parents=True, exist_ok=True)
    m = drv.Manifest(world.run_dir / "manifest.jsonl", world.hooks())
    drv.signal_owned(ident, sig, world.hooks(), m)


def test_signal_refused_when_the_exe_inode_changes_after_recording(world):
    ident = drv.record("production-wrapper", PROD_PID, world.hooks())
    world.procs[PROD_PID].ino += 1
    with pytest.raises(drv.OwnedSignal, match="inode changed"):
        signalled(world, ident)
    assert world.signals == []


def test_signal_refused_when_the_starttime_changes_after_recording(world):
    ident = drv.record("production-wrapper", PROD_PID, world.hooks())
    world.procs[PROD_PID].starttime += 1
    with pytest.raises(drv.OwnedSignal, match="starttime changed"):
        signalled(world, ident)
    assert world.signals == []


def test_signal_refused_when_the_argv_changes_after_recording(world):
    ident = drv.record("production-wrapper", PROD_PID, world.hooks())
    world.procs[PROD_PID].argv = list(world.wrap_argv) + ["--extra"]
    with pytest.raises(drv.OwnedSignal, match="argv changed"):
        signalled(world, ident)
    assert world.signals == []


def test_signal_never_targets_a_process_proven_exited(world):
    ident = drv.record("production-wrapper", PROD_PID, world.hooks())
    world.procs[PROD_PID].alive = False
    signalled(world, ident)
    assert world.signals == []                                    # recorded, never signalled
    assert any(e["event"] == "signal_skipped_exited" for e in world.events())


def test_zombie_with_empty_cmdline_is_skipped_not_signalled(world):
    world.add(Proc(4210, [str(drv.CAND_HELPER)], str(drv.ROOT), str(drv.CAND_HELPER), 7, 1, 1,
                   []))
    ident = drv.record("candidate-helper", 4210, world.hooks())
    world.procs[4210].zombie, world.procs[4210].rc = True, -9      # exited, not yet reaped
    signalled(world, ident)
    assert world.signals == []
    assert any(e["event"] == "signal_skipped_exited" and e["pid"] == 4210 for e in world.events())


def test_real_pidfd_open_reports_esrch_and_never_falls_back():
    with pytest.raises(drv.PidfdGone, match="pidfd_open"):
        drv.real_pidfd_open(2147483000)          # an absent PID: no fallback, no signal
    assert issubclass(drv.PidfdGone, drv.OwnedSignal)


@pytest.mark.skipif(not hasattr(drv._libc(), "pidfd_open"), reason="pidfd needs glibc >= 2.35")
def test_real_cpu_child_pidfd_lifecycle_and_reap():
    """The ONE permitted real-process test: CPU-only sleep child, real pidfd, real reap.
    No network, no service, no GPU, no engine binary."""
    h = drv.Hooks()
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        ident = drv.record("candidate-wrapper", proc.pid, h)
        assert h.poll(proc) is None and drv.exited_proven(ident, h) is False
        m = drv.Manifest(Path(os.environ.get("PYTEST_TMPDIR", "/tmp")) / "lifecycle.jsonl", h)
        drv.signal_owned(ident, signal.SIGTERM, h, m)
        assert drv.wait_gone(ident, h, 15.0) is True              # pidfd proves the exit
        for _ in range(60):                                      # zombie keeps /proc alive
            if h.alive(ident.pid) and not h.cmdline(ident.pid):
                break
            h.sleep(0.05)
        assert h.cmdline(ident.pid) == []                        # exited, not yet reaped
        h.reap(proc)
        assert proc.returncode is not None
        assert drv.exited_proven(ident, h) is True
        with pytest.raises(drv.PidfdGone):
            h.pidfd_open(proc.pid)                               # reaped: ESRCH, no signal
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=15)


def test_driver_source_contains_no_broad_process_kill_or_unlink():
    tree = ast.parse((ROOT / "tools/mixed_gpu/run_acceptance.py").read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            docstrings.add(id(body[0].value))
    tokens: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute):
            tokens.add(n.attr)
        elif isinstance(n, ast.Name):
            tokens.add(n.id)
        elif isinstance(n, ast.keyword):
            tokens.add(n.arg)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings:
            tokens.add(n.value)
    banned = {"kill", "killpg", "pkill", "pgrep", "start_new_session", "unlink", "setsid",
              "SIGKILL", "SIGINT", "SIGSTOP"}
    assert not (tokens & banned), sorted(tokens & banned)
    assert "pidfd_send_signal" in tokens and "pidfd_open" in tokens


# ------------------------------------------------------------------ arms / env / build ids

def test_arms_differ_only_by_helper_keys_with_unique_engine_logs(world):
    assert run(world) == drv.EXIT_DONE
    events = [e for e in world.events() if e["event"] == "chain_started"]
    cfgs = {e["stage"]: json.loads(Path(e["config"]).read_text(encoding="utf-8"))
            for e in events}
    assert set(cfgs) == {"off-1", "on-1", "on-2", "off-2", "fault-prefill", "fault-decode"}
    off, on = cfgs["off-1"], cfgs["on-1"]
    assert set(off) ^ set(on) == {"expert_helper_socket", "expert_helper_slots",
                                  "expert_helper_timeout"}
    assert off["log"] != on["log"]
    assert on["expert_helper_slots"] == 3400 and on["expert_helper_timeout"] == 100
    for stage, cfg in cfgs.items():                             # unique engine log per stage
        assert cfg["log"] == str(world.run_dir / f"engine-{stage}.log")
    assert len({cfg["log"] for cfg in cfgs.values()}) == 6
    assert all(Path(cfg["log"]).is_file() for cfg in cfgs.values())   # all six kept
    arms = {e["stage"]: e["arm"] for e in events}
    for stage, cfg in cfgs.items():
        helper = {k for k in cfg if str(k).startswith("expert_helper_")}
        want = set() if arms[stage] == "off" else {"expert_helper_socket",
                                                   "expert_helper_slots",
                                                   "expert_helper_timeout"}
        assert helper == want, (stage, helper)
    assert off["mcp_servers"] == {} and on["mcp_servers"] == {}
    assert "--vision" not in off["args"] and "--vision" not in on["args"]
    assert "vision" not in off and "vision" not in on
    assert off["env"] == on["env"]
    assert off["env"]["STRATA_ENGINE_NICE"] == "0"
    assert all(k in on["env"] for k in drv.TELEMETRY)
    assert off["args"] == on["args"] and off["port"] == on["port"] == drv.CAND_PORT
    assert off["cwd"] == on["cwd"] == world.cfg["cwd"]
    assert off["exe"] == on["exe"] == str(drv.CAND_ENGINE)
    diff = json.loads((world.run_dir / "diffs.json").read_text(encoding="utf-8"))
    assert set(diff["on_vs_off"]) <= {"log", "expert_helper_socket", "expert_helper_slots",
                                      "expert_helper_timeout"}
    assert set(diff["off_vs_canonical"]) <= {"exe", "port", "log", "mcp_servers", "env", "args",
                                             "vision"} | {k for k in world.cfg
                                                         if str(k).startswith("expert_helper_")}


def test_off_arm_strips_every_inherited_helper_key(world):
    assert run(world) == drv.EXIT_DONE
    for name in ("config-off.json", "config-on.json"):
        cfg = json.loads((world.run_dir / name).read_text(encoding="utf-8"))
        inherited = {"expert_helper_layers", "expert_helper_reserve_mib"}
        assert not (inherited & set(cfg))


def test_helper_argv_slots_timeouts_and_env_are_exact(world):
    assert run(world) == drv.EXIT_DONE
    helpers = [c for c in world.popen_calls if c["argv"][0] == str(drv.CAND_HELPER)]
    assert len(helpers) == 4                                      # on-1, on-2, 2 fault chains
    argv = helpers[0]["argv"]
    for flag, value in [("--pack", drv.HELPER_PACK),
                        ("--experts", drv.HELPER_EXPERTS), ("--device", "0"),
                        ("--slots", "3400"), ("--reserve-mib", "512"),
                        ("--timeout", "7200000"), ("--begin-timeout", "1800000")]:
        assert argv[argv.index(flag) + 1] == value
    assert argv.count("--experts") == 1 and argv.count("--slots") == 1
    assert argv[argv.index("--experts") + 1].endswith(".gguf")
    assert helpers[0]["env"]["CUDA_VISIBLE_DEVICES"] == "0"
    assert "LD_PRELOAD" not in helpers[0]["env"]
    sockets = [c["argv"][c["argv"].index("--socket") + 1] for c in helpers]
    assert len(set(sockets)) == 4
    assert all(len(os.fsencode(s)) < drv.AFUNIX_MAX for s in sockets)
    assert all(Path(s).parent == world.run_dir for s in sockets)


def test_wrapper_command_and_env_match_the_launcher_context(world):
    for name in ("STRATA_DF_PROBE", "STRATA_DUMP_X", "LD_PRELOAD", "STRATA_WINDOW_X",
                 "STRATA_ARENA_MMAP", "STRATA_DFLASH_SAMPLED"):
        os.environ[name] = "probe"
    try:
        assert run(world) == drv.EXIT_DONE
    finally:
        for name in ("STRATA_DF_PROBE", "STRATA_DUMP_X", "LD_PRELOAD", "STRATA_WINDOW_X",
                     "STRATA_ARENA_MMAP", "STRATA_DFLASH_SAMPLED"):
            os.environ.pop(name, None)
    wrappers = [c for c in world.popen_calls if c["argv"][0] == str(drv.PY)]
    assert wrappers
    for c in wrappers:
        assert "LD_PRELOAD" not in c["env"] and "STRATA_ARENA_MMAP" not in c["env"]
        assert "STRATA_DFLASH_SAMPLED" not in c["env"]
        assert not any(k.startswith(drv.DROP_ENV_PREFIX) for k in c["env"])
        assert c["env"]["LD_LIBRARY_PATH"] == ":".join(drv.LIB_DIRS)
        assert c["cwd"] == str(drv.MERGED_CWD)
    cand = [c for c in wrappers if str(drv.CAND_PORT) in c["argv"]]
    assert cand and cand[0]["argv"][:2] == [str(drv.PY), str(drv.ROOT / "serve/server.py")]
    assert cand[0]["argv"][-2:] == ["--port", str(drv.CAND_PORT)]
    assert all(str(drv.PROD_PORT) not in c["argv"] for c in cand)   # candidates never touch 8009


def test_build_id_is_a_composite_over_the_measured_binaries(world):
    assert run(world) == drv.EXIT_DONE
    sha = json.loads((world.run_dir / "build-sha.json").read_text(encoding="utf-8"))
    assert set(sha["sha256"]) == {"driver_py", "capture_py", "engine_hip", "helper_cuda",
                                  "canonical_config", "production_engine"}
    assert sha["parts"]["engine_hip"] == str(drv.CAND_ENGINE)
    assert sha["parts"]["production_engine"] == world.cfg["exe"]
    assert len(sha["build_id"]) == 24
    ids = {c[c.index("--build-id") + 1] for c in world.captures}
    assert ids == {sha["build_id"]}
    assert sha["build_id"] == world.summary()["restore"]["build_sha256"]


# ------------------------------------------------------------------ capture

def test_capture_runs_acceptance_py_per_call_with_fresh_out_dirs(world):
    assert run(world) == drv.EXIT_DONE
    chains = [c[c.index("--chain") + 1] for c in world.captures]
    assert chains[:4] == ["off-1", "on-1", "on-2", "off-2"]
    assert [c[c.index("--samples") + 1] for c in world.captures[:4]] == ["3"] * 4
    assert [c[c.index("--warmups") + 1] for c in world.captures[:4]] == ["2"] * 4
    assert all(c[0] == sys.executable and c[1] == str(drv.UTILITY) for c in world.captures)
    outs = [c[c.index("--out") + 1] for c in world.captures]
    assert len(set(outs)) == len(outs) and all(Path(o).is_dir() for o in outs)
    for c in world.captures:
        assert {"--request", "--log", "--chain", "--build-id"} <= set(c)
    faults = [c for c in world.captures if c[2] == "fault"]
    assert {c[c.index("--trigger") + 1] for c in faults} == {"log-marker", "first-delta"}
    prefill = next(c for c in faults if c[c.index("--trigger") + 1] == "log-marker")
    assert prefill[prefill.index("--marker") + 1] == drv.PREFILL_MARKER
    helper_pids = {str(c["pid"]) for c in world.popen_calls
                   if c["argv"][0] == str(drv.CAND_HELPER)}
    assert {c[c.index("--pid") + 1] for c in faults} <= helper_pids
    assert {c[c.index("--exe") + 1] for c in faults} == {str(drv.CAND_HELPER)}
    post = [c for c in world.captures if c[c.index("--chain") + 1].startswith("postdeath")]
    assert len(post) == 2 and all(c[c.index("--samples") + 1] == "1" for c in post)
    logs = [v for c in world.captures for v in c if v.startswith("engine=")]
    assert len(set(logs)) == 6                                    # per-stage engine log per call


def test_pinned_requests_share_identical_bytes_across_arms(world):
    assert run(world) == drv.EXIT_DONE
    sample = json.loads((world.run_dir / "request-sample.json").read_text(encoding="utf-8"))
    assert sample["temperature"] == 0 and sample["seed"] == 271828
    assert sample["top_p"] == 1 and sample["top_k"] == 1
    assert sample["reasoning_effort"] == "none" and sample["max_tokens"] == 256
    assert "tools" not in sample and "stream" not in sample
    assert set(sample) == {"model", "messages", "max_tokens", "temperature", "seed", "top_p",
                           "top_k", "reasoning_effort"}
    assert sample["messages"][0]["content"] == drv.SAMPLE_PROMPT
    pre = json.loads((world.run_dir / "request-fault-prefill.json").read_text(encoding="utf-8"))
    dec = json.loads((world.run_dir / "request-fault-decode.json").read_text(encoding="utf-8"))
    assert pre["max_tokens"] == 120 and dec["max_tokens"] == 768
    assert pre["stream"] and dec["stream"]
    assert pre["stream_options"] == {"include_usage": True}
    assert "tools" not in pre and "tools" not in dec
    assert pre["seed"] == dec["seed"] == 271828
    urls = {c[c.index("--url") + 1] for c in world.captures}
    assert urls == {drv.URL}                                      # loopback candidate only


def test_capture_failure_is_recorded_and_forces_a_nonzero_exit(world):
    def capture(argv, cwd, timeout):
        argv = [str(a) for a in argv]
        Path(argv[argv.index("--out") + 1]).mkdir(parents=True, exist_ok=True)
        return (3, "bad body", "log") if "sample-off-1" in " ".join(argv) else (0, "", "")
    world.capture = capture
    assert run(world) == drv.EXIT_FAIL
    s = world.summary()
    assert any("acceptance.py exit 3" in f for f in s["capture_failures"])
    assert s["claims"] == {"acceptance_pass": False, "phase_proven": False, "speed_claim": False}
    assert (world.run_dir / "sample-off-1.rc").read_text(encoding="utf-8") == "3"
    assert (world.run_dir / "sample-off-1.stdout").read_text(encoding="utf-8") == "bad body"
    assert s["restore"]["ok"] is True                             # restore is still verified


def test_capture_timeout_is_recorded_as_rc_124_with_partial_output(world):
    def capture(argv, cwd, timeout):
        argv = [str(a) for a in argv]
        Path(argv[argv.index("--out") + 1]).mkdir(parents=True, exist_ok=True)
        if "sample-off-1" in " ".join(argv):
            return 124, "partial-out", "capture timeout after 3600.0s"
        return 0, "captured", ""
    world.capture = capture
    assert run(world) == drv.EXIT_FAIL
    assert any("acceptance.py exit 124" in f for f in world.summary()["capture_failures"])
    assert "partial-out" in (world.run_dir / "sample-off-1.stdout").read_text(encoding="utf-8")


def test_real_capture_handler_records_a_real_timeout_with_partial_output():
    """CPU-only real subprocess: the handler must record rc 124 and the partial stdout."""
    argv = [sys.executable, "-c",
            "import sys, time; sys.stdout.write('partial'); sys.stdout.flush(); time.sleep(20)"]
    rc, out, err = drv.real_capture(argv, str(ROOT), 1.0)
    assert rc == 124 and "partial" in out and "capture timeout" in err


# ------------------------------------------------------------------ lifecycle order

def test_production_stops_before_any_candidate_starts(world):
    assert run(world) == drv.EXIT_DONE
    kinds = world.kinds()
    assert (PROD_PID, int(signal.SIGTERM)) in world.signals
    assert kinds.index("production_stopped") < min(i for i, k in enumerate(kinds)
                                                   if k == "chain_started")
    assert kinds.index("production_stopped") < kinds.index("restore_verified")
    assert world.summary()["restore"]["status"] == "restored and verified"
    assert world.summary()["restoration_required"] is True


def test_all_configured_production_children_are_recorded_and_confirmed_gone(world):
    assert run(world) == drv.EXIT_DONE
    pre = [e for e in world.events() if e["event"] == "production_pre_stop"][0]
    kinds = {c["kind"] for c in pre["children"]}
    assert kinds == {"production-engine-child", "production-vision-child",
                     "production-mcp-child"}
    assert len(pre["children"]) == 5
    assert "production_child_survived" not in world.kinds()
    assert world.summary()["production_stopped"] is True


def test_surviving_aux_child_gets_identity_bound_term(world):
    world.procs[VISION_PID].exit_after_signal = False              # not torn down with wrapper
    assert run(world) == drv.EXIT_DONE
    term = [e for e in world.events() if e["event"] == "terminated" and e["pid"] == VISION_PID]
    assert term and term[0]["gone"] is True                       # died on the direct TERM
    assert (VISION_PID, int(signal.SIGTERM)) in world.signals
    assert world.summary()["exit"] == drv.EXIT_DONE


def test_child_that_survives_every_signal_blocks_the_run_and_the_restore(world):
    world.procs[VISION_PID].ignore_signals = True
    world.procs[VISION_PID].exit_after_signal = False
    world.procs[VISION_PID].alive = True

    def pidfd_signal(fd, sig):
        pid = world.fds[fd]
        world.signals.append((pid, int(sig)))
        if pid == VISION_PID:
            return                                                # it never dies
        World.pidfd_signal(world, fd, sig)
    world.pidfd_signal = pidfd_signal
    assert run(world) == drv.EXIT_FAIL
    s = world.summary()
    assert s["blocked"] is True
    assert any("survived their wrapper" in f for f in s["capture_failures"])
    assert s["restore"]["status"].startswith("RESTORE_BLOCKED")
    assert [c for c in world.popen_calls if c["argv"] == [str(drv.LAUNCHER)]] == []


def test_watchdog_replacement_child_is_recorded_and_terminated(world):
    """A watchdog-replaced engine child present at discovery is recorded and confirmed gone;
    a child that does not go with the wrapper is terminated by its own identity."""
    replacement = 4100
    world.add(Proc(replacement, drv.expected_engine_argv({**world.cfg, "exe": world.canon}),
                   world.cfg["cwd"], world.canon, 7, 1500, 160, [],
                   exit_after_signal=False))                                # survives the wrapper
    world.procs[PROD_PID].children.append(replacement)
    assert run(world) == drv.EXIT_DONE
    assert (replacement, int(signal.SIGTERM)) in world.signals     # identity-bound TERM
    term = [e for e in world.events() if e["event"] == "terminated" and e["pid"] == replacement]
    assert term and term[0]["gone"] is True
    assert world.summary()["production_stopped"] is True


def test_restoration_required_flips_before_the_production_signal(world):
    assert run(world) == drv.EXIT_DONE
    events = world.events()
    signal_rows = [i for i, e in enumerate(events) if e["event"] == "signal"
                   and e["pid"] == PROD_PID]
    summary = world.summary()
    assert summary["restoration_required"] is True and signal_rows
    assert "production_pre_stop" in world.kinds()
    assert world.kinds().index("production_pre_stop") < signal_rows[0]


def test_every_stage_uses_a_fresh_chain_and_stops_it(world):
    assert run(world) == drv.EXIT_DONE
    events = world.events()
    starts = [e for e in events if e["event"] == "chain_started"]
    stops = [e for e in events if e["event"] == "chain_stopped"]
    assert [e["stage"] for e in starts] == [e["stage"] for e in stops]
    assert [e["stage"] for e in starts] == ["off-1", "on-1", "on-2", "off-2", "fault-prefill",
                                            "fault-decode"]
    assert [e["arm"] for e in starts] == ["off", "on", "on", "off", "on", "on"]
    assert all(e["port_8019_free"] for e in stops)
    assert all(e["wrapper_gone"] and e["engine_gone"] and e["helper_gone"] for e in stops)


def test_candidate_engine_children_are_recorded_and_match_the_config_intent(world):
    assert run(world) == drv.EXIT_DONE
    events = [e for e in world.events() if e["event"] == "chain_children"]
    assert len(events) == 6
    for e in events:
        assert e["children"]
        assert all(c["exe"] == os.path.realpath(drv.CAND_ENGINE) for c in e["children"])
        assert all("--serve" in c["argv"] for c in e["children"])
    on = next(e for e in events if e["stage"] == "on-1")
    child = on["children"][0]
    assert "--expert-helper-socket" in child["argv"]
    assert child["argv"][child["argv"].index("--expert-helper-slots") + 1] == "3400"
    assert all(k["exe"] != os.path.realpath(drv.CAND_ENGINE)
               for k in world.summary()["restore"]["children"])


def test_a_candidate_engine_child_with_the_wrong_exe_fails_every_stage(world):
    def popen(argv, cwd, env, out, err):
        p = World.popen(world, argv, cwd, env, out, err)
        if "--port" in argv and argv[-1] == str(drv.CAND_PORT):   # the candidate chains only
            for kid in p.children:
                world.procs[kid].exe = world.canon                # the production engine
                world.procs[kid].argv[0] = world.canon
        return p
    world.popen = popen
    assert run(world) == drv.EXIT_FAIL
    assert world.kinds().count("chain_child_unexpected") == 6
    assert world.kinds().count("chain_started") == 0
    assert world.kinds().count("restore_verified") == 1
    assert world.summary()["restore"]["ok"] is True


def test_engine_child_with_the_wrong_args_fails_the_stage(world):
    def popen(argv, cwd, env, out, err):
        p = World.popen(world, argv, cwd, env, out, err)
        for kid in p.children:
            world.procs[kid].argv = [world.procs[kid].argv[0], "--serve"]
        return p
    world.popen = popen
    assert run(world) == drv.EXIT_FAIL
    assert world.kinds().count("chain_child_args_unexpected") == 6


def test_candidate_cleanup_happens_before_the_restore(world):
    assert run(world) == drv.EXIT_DONE
    kinds = world.kinds()
    restore_at = min(i for i, k in enumerate(kinds) if k.startswith("restore"))
    assert all(i < restore_at for i, k in enumerate(kinds) if k == "chain_stopped")
    launchers = [c for c in world.popen_calls if c["argv"] == [str(drv.LAUNCHER)]]
    assert len(launchers) == 1                                    # the canonical launcher, once


def test_restore_verifies_canonical_wrapper_argv_cwd_and_children(world):
    assert run(world) == drv.EXIT_DONE
    r = world.summary()["restore"]
    assert r["ok"] is True and not r["problems"]
    assert r["wrapper"]["cwd"] == str(drv.MERGED_CWD)
    assert r["wrapper"]["argv"][:2] == [str(drv.PY), "serve/server.py"]
    assert str(drv.PROD_CONFIG) in r["wrapper"]["argv"]
    kinds = {k["kind"] for k in r["children"]}
    assert kinds == {"engine", "vision", "mcp:pandoc", "mcp:docling", "mcp:markitdown"}
    engine = [k for k in r["children"] if k["kind"] == "engine"]
    assert engine and engine[0]["argv"][1] == "--serve"
    assert all(arg in engine[0]["argv"] for arg in world.cfg["args"])
    assert "--expert-helper-socket" in engine[0]["argv"]         # expected engine args
    assert not any(k["exe"] == str(drv.CAND_ENGINE) for k in r["children"])


def test_restore_rejects_an_unclassified_restored_child(world):
    def popen(argv, cwd, env, out, err):
        p = World.popen(world, argv, cwd, env, out, err)
        if argv == [str(drv.LAUNCHER)]:
            stranger = world.next_pid
            world.next_pid += 1
            world.add(Proc(stranger, ["python3", "/tmp/mystery.py"], cwd, "/usr/bin/python3",
                           7, 9000, 300, []))
            p.children.append(stranger)
        return p
    world.popen = popen
    assert run(world) == drv.EXIT_FAIL
    r = world.summary()["restore"]
    assert r["ok"] is False and any("matches nothing configured" in p for p in r["problems"])


def test_restore_rejects_a_restored_engine_child_with_the_wrong_args(world):
    def popen(argv, cwd, env, out, err):
        p = World.popen(world, argv, cwd, env, out, err)
        if argv == [str(drv.LAUNCHER)]:
            for kid in p.children:
                if world.procs[kid].exe == world.canon:
                    world.procs[kid].argv = [world.canon, "--serve", "--model", "other.gguf"]
        return p
    world.popen = popen
    assert run(world) == drv.EXIT_FAIL
    r = world.summary()["restore"]
    assert r["ok"] is False and any("does not match the expected engine args" in p
                                    for p in r["problems"])


def test_restore_blocked_when_a_candidate_survives_cleanup(world):
    def popen(argv, cwd, env, out, err):
        p = World.popen(world, argv, cwd, env, out, err)
        if argv[0] == str(drv.CAND_HELPER):
            p.exit_after_signal = False                           # helper stays alive past TERM
            p.ignore_signals = True
        return p
    world.popen = popen
    assert run(world) == drv.EXIT_FAIL
    r = world.summary()["restore"]
    assert r["ok"] is False and r["status"].startswith("RESTORE_BLOCKED") and "survivors" in r
    assert [c for c in world.popen_calls if c["argv"] == [str(drv.LAUNCHER)]] == []


def test_restore_records_a_conflict_for_an_occupied_8009_and_never_kills_it(world):
    def pidfd_signal(fd, sig):
        pid = world.fds[fd]
        world.signals.append((pid, int(sig)))
        p = world.procs[pid]
        p.alive, p.rc = False, -int(sig)
        for kid in p.children:
            world.procs[kid].alive, world.procs[kid].rc = False, -int(sig)
        if pid != PROD_PID:
            return                                                # the production wrapper survives
        world.listener_rows[drv.PROD_PORT] = [{"addr": "", "pid": 9999, "line": "LISTEN 8009"}]
    world.pidfd_signal = pidfd_signal
    assert run(world) == drv.EXIT_FAIL
    r = world.summary()["restore"]
    assert r["ok"] is False and "CONFLICT" in r["status"]
    assert [c for c in world.popen_calls if c["argv"] == [str(drv.LAUNCHER)]] == []
    assert not any(pid == 9999 for pid, _ in world.signals)


def test_no_restore_when_no_production_stop_was_attempted(world):
    world.listener_rows[drv.PROD_PORT] = [{"addr": "", "pid": 9999, "line": "LISTEN"}]
    assert run(world) == drv.EXIT_REFUSED
    assert world.kinds().count("restore_skipped") == 1
    assert [c for c in world.popen_calls if c["argv"] == [str(drv.LAUNCHER)]] == []
    assert world.signals == []


def test_failure_during_a_capture_still_restores_the_canonical_service_once(world):
    def capture(argv, cwd, timeout):
        argv = [str(a) for a in argv]
        Path(argv[argv.index("--out") + 1]).mkdir(parents=True, exist_ok=True)
        if "sample-on-1" in " ".join(argv):
            raise OSError("capture tool died")
        return 0, "", ""
    world.capture = capture
    assert run(world) == drv.EXIT_FAIL
    assert world.kinds().count("restore_verified") == 1
    assert world.summary()["restore"]["ok"] is True


def test_helper_that_never_reports_ready_ends_the_stage_and_restores(world):
    def popen(argv, cwd, env, out, err):
        p = World.popen(world, argv, cwd, env, out, err)
        if argv[0] == str(drv.CAND_HELPER):
            Path(out).write_text("", encoding="utf-8")            # no listening line
            p.rc, p.alive = 1, False                              # helper dies immediately
        return p
    world.popen = popen
    assert run(world) == drv.EXIT_FAIL
    assert "ready_failed" in world.kinds()
    assert world.summary()["restore"]["ok"] is True


def test_wrapper_that_never_reports_ready_ends_the_stage_and_restores(world):
    def popen(argv, cwd, env, out, err):
        p = World.popen(world, argv, cwd, env, out, err)
        if "--port" in argv and argv[-1] == str(drv.CAND_PORT):
            Path(out).write_text("starting\n", encoding="utf-8")  # no ready marker
            p.rc, p.alive = -11, False
            for kid in p.children:
                world.procs[kid].alive, world.procs[kid].rc = False, -9
        return p
    world.popen = popen
    assert run(world) == drv.EXIT_FAIL
    assert "ready_failed" in world.kinds()
    assert world.kinds().count("restore_verified") == 1


def test_keyboard_interrupt_mid_run_still_restores(world):
    def capture(argv, cwd, timeout):
        argv = [str(a) for a in argv]
        Path(argv[argv.index("--out") + 1]).mkdir(parents=True, exist_ok=True)
        if "off-2" in " ".join(argv):
            raise KeyboardInterrupt("SIGTERM")
        return 0, "", ""
    world.capture = capture
    assert run(world) == drv.EXIT_FAIL
    assert world.kinds().count("restore_verified") == 1
    assert world.summary()["restore"]["ok"] is True
    assert any("interrupted at off-2" in f for f in world.summary()["capture_failures"])


def test_unexpected_exception_is_recorded_not_suppressed(world):
    def capture(argv, cwd, timeout):
        argv = [str(a) for a in argv]
        Path(argv[argv.index("--out") + 1]).mkdir(parents=True, exist_ok=True)
        if "sample-off-1" in " ".join(argv):
            raise ValueError("a bug outside the declared exceptions")
        return 0, "", ""
    world.capture = capture
    assert run(world) == drv.EXIT_FAIL
    assert world.kinds().count("restore_verified") == 1           # restore ran despite the bug
    assert any("unexpected" in f for f in world.summary()["capture_failures"])


def test_chain_is_registered_before_the_first_process_starts(world):
    """Cleanup knows the chain even when its FIRST popen raises (helper start fails)."""
    def popen(argv, cwd, env, out, err):
        if argv[0] == str(drv.CAND_HELPER):
            raise OSError("exec failed at start")
        return World.popen(world, argv, cwd, env, out, err)
    world.popen = popen
    assert run(world) == drv.EXIT_FAIL
    assert "stage_failed" in world.kinds()                        # recorded, stage aborted
    assert world.kinds().count("restore_verified") == 1
    assert all(s["helper_gone"] for s in world.events() if s["event"] == "chain_stopped")


def test_cleanup_exception_never_suppresses_the_restore_attempt(world, monkeypatch):
    def boom(self, why):
        raise RuntimeError("cleanup raised")
    monkeypatch.setattr(drv.Driver, "cleanup_active", boom)
    assert run(world) == drv.EXIT_FAIL
    assert "cleanup_exception" in world.kinds()
    assert world.kinds().count("restore_verified") == 1            # restore attempted
    assert any("cleanup raised" in f for f in world.summary()["capture_failures"])


def test_unverified_health_after_the_restore_is_a_failure(world):
    world.health_loaded = False
    assert run(world) == drv.EXIT_FAIL
    r = world.summary()["restore"]
    assert r["ok"] is False and "loaded=true" in r["status"]
    assert world.health_calls and all(c["port"] == drv.PROD_PORT for c in world.health_calls)


def test_helper_socket_is_never_connected_for_readiness(world):
    assert run(world) == drv.EXIT_DONE
    ready = [e for e in world.events() if e["event"] == "ready"]
    assert all("marker" in e and "socket" not in json.dumps(e) for e in ready)
    assert any("listening" in e["marker"] for e in ready)
    helper_logs = [p for p in world.run_dir.glob("helper-*.log")]
    assert len(helper_logs) == 4                                  # all helper logs retained
    assert len(list(world.run_dir.glob("engine-*.log"))) == 6


def test_summary_and_manifest_record_every_transition(world):
    assert run(world) == drv.EXIT_DONE
    s = world.summary()
    assert s["exit"] == drv.EXIT_DONE and s["restoration_required"] is True
    assert s["production_stopped"] is True and s["capture_failures"] == []
    assert Path(s["manifest"]).is_file() and Path(s["run_dir"]).is_dir()
    events = world.events()
    assert {"plan", "verified", "production_pre_stop", "signal", "chain_started", "ready",
            "capture", "chain_stopped", "restore_health", "restore_verified",
            "summary"} <= {e["event"] for e in events}
    sigs = [e for e in events if e["event"] == "signal"]
    assert sigs and all(e["sig"] == int(signal.SIGTERM) for e in sigs)
    assert all(int(e["sig"]) != int(signal.SIGKILL) for e in sigs)
    assert all("at" in e for e in events)


# ------------------------------------------------------------------ defect 1: OFF passes no helper log

def test_off_stage_passes_only_existing_logs_to_the_frozen_utility(world):
    assert run(world) == drv.EXIT_DONE
    by_chain: dict[str, list[str]] = {}
    for c in world.captures:
        chain = c[c.index("--chain") + 1]
        specs = [c[i + 1] for i, v in enumerate(c) if v == "--log"]
        by_chain.setdefault(chain, []).extend(specs)
    off = ["off-1", "off-2"]
    on = ["on-1", "on-2", "fault-prefill", "fault-decode", "postdeath-prefill", "postdeath-decode"]
    for chain in off:
        labels = {s.split("=")[0] for s in by_chain[chain]}
        assert labels == {"wrapper", "engine"}, (chain, labels)      # no helper= for OFF
    for chain in on:
        labels = {s.split("=")[0] for s in by_chain[chain]}
        assert labels == {"wrapper", "engine", "helper"}, (chain, labels)
    for chain, specs in by_chain.items():
        logs = acc.parse_logs(specs)                                  # frozen validator: passes
        starts, problems = acc.snapshot_logs(logs, acc.FaultHooks())  # frozen preflight snapshot
        assert not problems, (chain, problems)                        # every log exists+readable
        assert set(starts) == set(logs)


def test_no_fake_empty_helper_log_is_written_for_off_arms(world):
    assert run(world) == drv.EXIT_DONE
    assert sorted(p.name for p in world.run_dir.glob("helper-*.log")) == [
        "helper-fault-decode.log", "helper-fault-prefill.log", "helper-on-1.log",
        "helper-on-2.log"]


# ------------------------------------------------------------------ defect 2: never signal unknown children

def test_unknown_child_under_candidate_wrapper_exits_with_wrapper_never_signalled(world):
    def popen(argv, cwd, env, out, err):
        p = World.popen(world, argv, cwd, env, out, err)
        if "--port" in argv and argv[-1] == str(drv.CAND_PORT):       # candidate chains only
            for kid in p.children:
                world.procs[kid].exe = world.canon                    # NOT our candidate engine
                world.procs[kid].argv[0] = world.canon
        return p
    world.popen = popen
    assert run(world) == drv.EXIT_FAIL
    assert world.kinds().count("chain_child_unexpected") == 6
    kid_pids = {5001 + k for k in range(0, 20)}                       # fake grandchild pid range
    signalled_kids = {pid for pid, _ in world.signals if pid in kid_pids and pid not in
                      {c["pid"] for c in world.popen_calls}}
    assert not signalled_kids, sorted(signalled_kids)                 # unknown: ZERO signals
    stops = [e for e in world.events() if e["event"] == "chain_stopped"]
    assert stops and all(e["wrapper_gone"] and e["engine_gone"] for e in stops)
    assert world.summary()["restore"]["ok"] is True                   # exited with the wrapper


def test_surviving_unknown_child_blocks_restore_and_is_never_signalled(world):
    def popen(argv, cwd, env, out, err):
        p = World.popen(world, argv, cwd, env, out, err)
        if "--port" in argv and argv[-1] == str(drv.CAND_PORT):
            for kid in p.children:
                world.procs[kid].exe = world.canon
                world.procs[kid].argv[0] = world.canon
                world.procs[kid].exit_after_signal = False            # does not die with wrapper
                world.procs[kid].ignore_signals = True                # and it never dies
        return p
    world.popen = popen
    assert run(world) == drv.EXIT_FAIL
    # every child pid the fake made under a CANDIDATE wrapper (never a popen pid, never production)
    popen_pids = {c["pid"] for c in world.popen_calls}
    grandkids = {pid for pid in world.procs
                 if pid not in popen_pids and pid not in {PROD_PID, ENGINE_PID, VISION_PID,
                                                          *MCP_PIDS}}
    assert grandkids                                               # the mutated children exist
    signalled = {pid for pid, _ in world.signals}
    assert not (signalled & grandkids), sorted(signalled & grandkids)   # unknown: ZERO signals
    s = world.summary()
    assert s["blocked"] is True
    assert s["restore"]["status"].startswith("RESTORE_BLOCKED")
    assert [c for c in world.popen_calls if c["argv"] == [str(drv.LAUNCHER)]] == []


def test_expected_exe_with_wrong_args_is_known_and_terminated_without_orphan(world):
    def popen(argv, cwd, env, out, err):
        p = World.popen(world, argv, cwd, env, out, err)
        if "--port" in argv and argv[-1] == str(drv.CAND_PORT):
            for kid in p.children:
                world.procs[kid].argv = [world.procs[kid].argv[0], "--serve"]   # known exe, bad args
        return p
    world.popen = popen
    assert run(world) == drv.EXIT_FAIL
    assert world.kinds().count("chain_child_args_unexpected") == 6
    stops = [e for e in world.events() if e["event"] == "chain_stopped"]
    assert stops and all(e["engine_gone"] for e in stops)             # terminated: no orphan
    assert world.summary()["restore"]["ok"] is True


# ------------------------------------------------------------------ defect 3: record failure after a live Popen

def _arm_first_read(world, kind_target: str, exc_factory):
    """Make the FIRST identity read of the first chain child of `kind_target` fail, after the
    Popen already succeeded (the live-process case, not the popen-raise case)."""
    real_cmdline, armed = world.cmdline, {"armed": True}

    def cmdline(pid: int) -> list[str]:
        if armed["armed"] and pid >= 5000:                            # first owned child pid
            armed["armed"] = False
            raise exc_factory()
        return real_cmdline(pid)
    world.cmdline = cmdline


def test_record_failure_after_helper_popen_owns_and_terminates_the_helper(world):
    def popen(argv, cwd, env, out, err):
        p = World.popen(world, argv, cwd, env, out, err)
        if argv[0] == str(drv.CAND_HELPER):
            _arm_first_read(world, "helper", lambda: OSError(2, "No such file or directory"))
        return p
    world.popen = popen
    assert run(world) == drv.EXIT_DONE                                # transient: recovered
    assert "owned_identity_degraded" in world.kinds()
    helper_pids = {c["pid"] for c in world.popen_calls if c["argv"][0] == str(drv.CAND_HELPER)}
    assert world.signals and any(pid in helper_pids for pid, _ in world.signals)
    assert world.summary()["restore"]["ok"] is True                   # no untracked survivor


def test_record_interrupt_after_wrapper_popen_owns_and_terminates_the_wrapper(world):
    def popen(argv, cwd, env, out, err):
        p = World.popen(world, argv, cwd, env, out, err)
        if "--port" in argv and argv[-1] == str(drv.CAND_PORT):
            _arm_first_read(world, "wrapper", lambda: KeyboardInterrupt("interrupted record"))
        return p
    world.popen = popen
    assert run(world) == drv.EXIT_DONE
    degraded = [e for e in world.events() if e["event"] == "owned_identity_degraded"]
    assert degraded and degraded[0]["kind"] == "candidate-wrapper"
    wrapper_pids = {c["pid"] for c in world.popen_calls
                    if "--port" in c["argv"] and c["argv"][-1] == str(drv.CAND_PORT)}
    assert any(pid in wrapper_pids for pid, _ in world.signals)
    assert world.summary()["restore"]["ok"] is True


def test_record_failure_and_surviving_helper_blocks_restore(world):
    def popen(argv, cwd, env, out, err):
        p = World.popen(world, argv, cwd, env, out, err)
        if argv[0] == str(drv.CAND_HELPER):
            _arm_first_read(world, "helper", lambda: OSError(2, "No such file or directory"))
            p.exit_after_signal, p.ignore_signals = False, True       # it will not die
        return p
    world.popen = popen
    assert run(world) == drv.EXIT_FAIL
    s = world.summary()
    assert s["restore"]["status"].startswith("RESTORE_BLOCKED")
    assert any(k["kind"] == "candidate-helper" for k in s["restore"]["survivors"])


def test_exited_proven_handles_the_alive_to_cmdline_race():
    """Between alive() and cmdline() the PID can vanish: ENOENT is exit proof, EACCES is not."""
    w = World(_short_root() / "race", fixture_config(_short_root()))
    w.add(Proc(7777, ["x"], "/", "x", 7, 1, 1, []))
    h = w.hooks()
    w.cmdline = lambda pid: (_ for _ in ()).throw(FileNotFoundError("no such pid"))
    ident = drv.Ident("candidate-helper", 7777, "x", 7, 1, 1, ("x",), "/", False)
    assert drv.exited_proven(ident, h) is True
    w.cmdline = lambda pid: (_ for _ in ()).throw(PermissionError(13, "denied"))
    assert drv.exited_proven(ident, h) is False                       # not exit proof


def test_recheck_treats_eacces_as_refusal_not_exit(world):
    ident = drv.record("production-wrapper", PROD_PID, world.hooks())

    def exe(pid: int):
        raise PermissionError(13, "denied")
    world.exe = exe
    problems = drv.recheck(ident, world.hooks())
    assert problems and not any(str(p).startswith("exited") for p in problems)
    with pytest.raises(drv.OwnedSignal, match="unreadable"):
        signalled(world, ident)
    assert world.signals == []


# ------------------------------------------------------------------ defect 4: ordered engine argv intent

def _mutate_candidate_child_argv(world, fn):
    def popen(argv, cwd, env, out, err):
        p = World.popen(world, argv, cwd, env, out, err)
        if "--port" in argv and argv[-1] == str(drv.CAND_PORT):
            for kid in p.children:
                world.procs[kid].argv = fn(list(world.procs[kid].argv))
        return p
    world.popen = popen


def test_wrong_on_arm_socket_value_fails_the_on_stages(world):
    def swap(argv):
        i = argv.index("--expert-helper-socket")
        argv[i + 1] = "/tmp/a-different-socket.sock"
        return argv
    _mutate_candidate_child_argv(world, swap)
    assert run(world) == drv.EXIT_FAIL
    assert world.kinds().count("chain_child_args_unexpected") == 4      # the four ON chains
    assert world.kinds().count("chain_child_unexpected") == 0


def test_missing_helper_flag_fails_the_on_stages(world):
    def drop(argv):
        i = argv.index("--expert-helper-slots")
        del argv[i:i + 2]
        return argv
    _mutate_candidate_child_argv(world, drop)
    assert run(world) == drv.EXIT_FAIL
    assert world.kinds().count("chain_child_args_unexpected") == 4


def test_helper_flag_value_from_another_flag_fails(world):
    """--expert-helper-slots 512 (the reserve value sitting on the wrong flag) must fail:
    the match is ordered flag/value pairs, not 'token present somewhere'."""
    def swap(argv):
        i = argv.index("--expert-helper-slots")
        argv[i + 1] = "512"
        return argv
    _mutate_candidate_child_argv(world, swap)
    assert run(world) == drv.EXIT_FAIL
    assert world.kinds().count("chain_child_args_unexpected") == 4


def test_helper_flag_inside_the_off_arm_sequence_fails(world):
    """An expected-sequence violation (a helper flag inserted IN the sequence the OFF config
    cannot produce) must fail; extras appended AFTER the sequence are the server's business."""
    def insert(argv):
        argv.insert(2, "--expert-helper-socket")
        argv.insert(3, "/tmp/x.sock")
        return argv
    _mutate_candidate_child_argv(world, insert)
    assert run(world) == drv.EXIT_FAIL
    assert world.kinds().count("chain_child_args_unexpected") == 6      # OFF + ON chains


def test_documented_extra_server_args_after_the_sequence_are_permitted(world):
    def extra(argv):
        return argv + ["--parallel-extra", "3"]
    _mutate_candidate_child_argv(world, extra)
    assert run(world) == drv.EXIT_DONE
    events = [e for e in world.events() if e["event"] == "chain_children"]
    assert events and all(e["engine_extra_args"] for e in events)


def test_engine_sequence_includes_the_layer_split_from_the_config(world):
    assert run(world) == drv.EXIT_DONE
    want = drv.expected_engine_argv({**world.cfg, "exe": str(drv.CAND_ENGINE)})
    assert want[:2] == [str(drv.CAND_ENGINE), "--serve"]
    assert want[2:4] == world.cfg["args"][:2]
    i = want.index("--layer-split")
    assert want[i + 1] == world.cfg["layer_split"]
    cfgs = [json.loads(Path(c["exec_argv"][c["exec_argv"].index("--config") + 1])
                       .read_text(encoding="utf-8"))
            for c in world.popen_calls if "--port" in c["argv"]
            and c["argv"][-1] == str(drv.CAND_PORT)]
    cfg = next(c for c in cfgs if "expert_helper_socket" in c)          # an ON-stage config
    assert drv.expected_engine_argv(cfg)[-6:] == [
        "--expert-helper-socket", cfg["expert_helper_socket"], "--expert-helper-slots", "3400",
        "--expert-helper-timeout-ms", "100"]


def test_restore_matches_the_real_recorded_production_argv(world):
    assert run(world) == drv.EXIT_DONE
    r = world.summary()["restore"]
    engine = [k for k in r["children"] if k["kind"] == "engine"][0]
    assert drv.engine_intent_matches(world.cfg, engine["argv"])[0] is True
    assert "--layer-split" in engine["argv"]
    assert engine["argv"][engine["argv"].index("--layer-split") + 1] == world.cfg["layer_split"]


# ------------------------------------------------------------------ defect 5: interrupt aborts the run

def test_keyboard_interrupt_aborts_further_stages_and_restores(world):
    def capture(argv, cwd, timeout):
        argv = [str(a) for a in argv]
        Path(argv[argv.index("--out") + 1]).mkdir(parents=True, exist_ok=True)
        raise KeyboardInterrupt("SIGTERM during off-1")
    world.capture = capture
    assert run(world) == drv.EXIT_FAIL
    assert "run_interrupted" in world.kinds()
    assert "run_aborted" in world.kinds()
    assert world.kinds().count("chain_started") == 1                   # NO further stages
    assert world.kinds().count("restore_verified") == 1
    assert world.summary()["exit"] == drv.EXIT_FAIL


def test_system_exit_aborts_further_stages_and_restores(world):
    def capture(argv, cwd, timeout):
        argv = [str(a) for a in argv]
        Path(argv[argv.index("--out") + 1]).mkdir(parents=True, exist_ok=True)
        raise SystemExit(3)
    world.capture = capture
    assert run(world) == drv.EXIT_FAIL
    assert world.kinds().count("chain_started") == 1
    assert world.kinds().count("restore_verified") == 1


def test_refusal_and_failure_never_report_zero(world):
    world.listener_rows[drv.CAND_PORT] = [{"addr": "", "pid": 7777, "line": "LISTEN 8019"}]
    assert run(world) == drv.EXIT_REFUSED                              # 2, never 0
    assert world.summary()["exit"] == drv.EXIT_REFUSED


# ------------------------------------------------------------------ defect 6: the REAL docling shape

def test_real_inventory_docling_interpreter_form_needs_the_attestation(world):
    world.docling_interpreter_form()
    assert run(world) == drv.EXIT_REFUSED                              # argv0 python, argv1 script
    assert world.signals == []


def test_real_inventory_docling_interpreter_form_is_attested_accepted(world):
    world.docling_interpreter_form()
    interp = os.path.realpath(str(world.host / "bin" / "uvdocling-python"))
    script = world.cfg["mcp_servers"]["docling"]["exec_target"]
    spec = f"docling={interp}:{script},--transport,stdio"
    assert run(world, extra=["--expect-aux", spec]) == drv.EXIT_DONE
    att = [e for e in world.events() if e["event"] == "aux_attestations"][0]
    assert att["attestations"]["docling"]["exec_target"] == interp
    assert att["attestations"]["docling"]["tokens"] == [script, "--transport", "stdio"]
    assert world.summary()["restore"]["ok"] is True


def test_docling_interpreter_form_wrong_attestation_refuses(world):
    world.docling_interpreter_form()
    interp = os.path.realpath(str(world.host / "bin" / "uvdocling-python"))
    spec = f"docling={interp}:/some/other/script,--transport,stdio"
    assert run(world, extra=["--expect-aux", spec]) == drv.EXIT_REFUSED
    assert world.signals == []


def test_expect_aux_format_is_validated_in_plan_mode_too(world):
    """A malformed attestation never passes silently, even in the mode that ignores its
    content: the format is refused in every mode, before any effect."""
    rc = drv.main(["--out-dir", str(world.run_dir), "--config", str(drv.PROD_CONFIG),
                   "--expect-aux", "badformat"], world.hooks())
    assert rc == drv.EXIT_REFUSED
    assert not world.run_dir.exists()                                   # refused before writing
    assert world.popen_calls == [] and world.signals == []
