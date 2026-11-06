"""CPU-only tests for tools/mixed_gpu/run_dflash_numeric.py (the t0 single-arm numeric adapter).

Everything is fake and self-contained: a fixture host tree with a minimal canonical config,
fake procfs, fake :8009/:8019 listeners, fake pidfd (open/signal/death recorded, never a real
signal), fake health, a fake clock - and for the capture seam the REAL accepted record emitter
`tools/mixed_gpu/acceptance.py` driven through its own fake-I/O hooks (fake transport, fake
connect, real log files under the run dir).  The adapter therefore classifies records produced
by the accepted utility's own code, not handmade dictionaries.  No real production path,
binary, service, network, GPU, signal or subprocess is used: an autouse fixture makes
socket.socket raise, proving the suite is socket-free.
Set STRATA_T0_SHA_EVIDENCE_DIR=<dir> to copy the mock runs' generated output-shas.tsv and their
sha-bearing manifest rows into <dir> for review: a NO-OP in every normal run, CPU file copies only.

The pinned contract these tests enforce (gate t0-numeric-gate.md §1, findings 1-8):

  1 binding  : plan AND execute rebind the imported drv.CAND_ENGINE to the typed role path
               BEFORE prepare; plan.json's binding, the ids, the driver's composite build-id
               and BOTH derived configs all name the same frozen role (checked in the plan
               artifacts, not in a binding row alone).
  2 timing   : the role hash is checked before ANY downtime (gate) and AGAIN inside the
               lifecycle AFTER the production stop and BEFORE the candidate start; a
               post-stop change refuses the start, runs cleanup + canonical restore, exits
               REFUSED with zero candidate processes.
  3 capture  : clean-200 needs record.json + response.bin + log-engine.slice.bin, EMPTY
               problems, transport/JSON/usage/log checks passed and utility rc 0 (driven by
               the REAL emitter); any other class stops the arm at the first error.
  4 parity   : num-red-parity --execute is REFUSED before any ownership/downtime action;
               the dry plan is allowed and says cap_monitored_not_enforced: true; the
               observer is read-only with a bounded join and never signals.
  5 type     : the stage tool runs only with fixtures AND original-BF16 (type 30) weights;
               verdicts are UNRUN_TYPE / UNRUN_FIXTURES / TOOL_REPORT_RC0 / TOOL_REPORT_RC1 /
               TOOL_CRASH (report line = real Report.print contract, not rc alone).
  6 survival : the follow-up checks the exact recorded child identity BEFORE and AFTER;
               attributable = alive_before AND alive_after AND same_identity; a replacement
               during the request makes the response NOT attributable, and the accepted
               cleanup rescan records the new child.
  7 counters : the PRIMARY acceptance observable is the captured response's
               timings.draft_n / timings.draft_n_accepted (serve/server.py request_timings);
               the DONE-line parser is secondary/parser-only (serve reads the engine stdout
               pipe, so a real log slice has no DONE line); absent fields are "unknown".
  8 tests    : self-contained; host-file integrations skip when absent; module constants
               (SAMPLE_SHA256, CAND_ENGINE, ...) are restored after every test.
  9 shas     : sha256(response.bin) is computed at capture time and recorded in the per-sample
               manifest row (output_sha256, plus the content-level output_content_sha256) AND as
               one line in <out-dir>/output-shas.tsv (arm, role, kind, index, label, position,
               request sha, response sha, content sha).  Same-arm repeat stability is the claim;
               OFF-vs-ON SHAs are recorded as EVIDENCE, not required equal; the identity key is
               (fresh engine, identical request sequence, position).  Rows exist for clean-200
               captures ONLY - a missing, refused, non-clean or incomplete record writes NO row
               and NO sha field (never a placeholder).

Requests: max 2 warmups + 3 samples per normal arm, ONE request per parity arm; the run stops
at the first non-clean request; no retry-until-pass.  A handled HTTP 200 refusal is
availability ONLY (claims stay False).  Lifecycle safety is the accepted driver's, reused
unchanged: global flock, child-only classification, pidfd-only signals, pre-registration,
restoration_required before the production signal, canonical restore via the fixture launcher.
"""
from __future__ import annotations

import ast
import dataclasses
import hashlib
import importlib.util
import json
import os
import shutil
import socket
import sys
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
# the adapter under test; STRATA_T0_ADAPTER_PATH lets the evidence runs point the SAME suite at
# an evidence-only snapshot copy (mutation checks never touch the real file)
ADAPTER_PATH = Path(os.environ.get("STRATA_T0_ADAPTER_PATH",
                                   ROOT / "tools" / "mixed_gpu" / "run_dflash_numeric.py"))
SPEC = importlib.util.spec_from_file_location("numeric_adapter_under_test", ADAPTER_PATH)
assert SPEC and SPEC.loader
ad = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ad
SPEC.loader.exec_module(ad)
drv = ad.drv                                  # the byte-frozen accepted driver the adapter imports

# the REAL accepted capture utility: its own record emitter, driven with fake I/O.
# Use importlib's canonical name so sys.modules aliasing cannot hand us an empty shell.
acc = importlib.import_module("tools.mixed_gpu.acceptance")
assert hasattr(acc, "FaultHooks") and hasattr(acc, "main")

PROD_PID, PROD_START = 4001, 111
PROD_ENGINE_PID = 4002
_SHORT_BASE = ROOT / "build-mixed-gpu" / "t0test"
_SEQ = iter(range(1, 100000))

_ORIG = {k: getattr(ad, k) for k in ("SAMPLE_SHA256", "DRIVER_SHA256", "CAPTURE_SHA256",
                                     "CONFIG_SHA256", "ROLES", "CAP_POLL_S", "PARITY_BYTE_CAP",
                                     "PARITY_TOOL", "GGUF_READER", "TOOL_VERSION",
                                     "OUTPUT_SHA_FILE", "OUTPUT_SHA_HEADER",
                                     "OUTPUT_SHA_COLUMNS")}
_ORIG_CAND = drv.CAND_ENGINE
_CANON_GGUF = Path(os.environ.get("STRATA_DFLASH2_DIR", "/mnt/user/dflash2") + "/artifacts/inco-dflash2-q4_0.gguf")
_CANON_CONFIG = Path(os.environ.get("STRATA_PROD_TREE", "/mnt/user/Strata") + "/prod-config-swift15-dflash-w4-q4_0.json")
NOTICE_LINE = ("strata generate: at position 41 the drafter returned candidate tokens outside "
               "the vocabulary; drafting is off for the rest of this request\n")
REPORT_STDOUT = ("stage                        agg cos  row0 cos  row0 max|d|  row0 rel\n"
                 "attn_gate                      1.000000     1.000000    0.000e+00  0.000e+00 P\n"
                 "FIRST DIVERGENCE (any row): none\n"
                 "FIRST ROW-0 DIVERGENCE:     none\n")


def _fsha(path) -> str:
    return hashlib.sha256(str(path).encode()).hexdigest()      # the fake seam: never hash 40 MB


@dataclasses.dataclass
class Proc:
    pid: int
    argv: list
    cwd: str
    exe: str
    dev: int
    ino: int
    starttime: int
    children: list
    alive: bool = True
    rc: int | None = None
    ignore_signals: bool = False


def fixture_config(hs: Path) -> dict:
    return {
        "exe": str(hs / "prod" / "strata"), "cwd": str(hs / "prod"), "port": 8009,
        "log": str(hs / "prod" / "engine.log"),
        "args": ["--dflash", str(hs / "models" / "d.gguf"), "--spec", "5", "--spec-min-p", "0.5",
                 "--dflash-block", "5", "--suffix-draft", "0"],
        "env": {"CUDA_VISIBLE_DEVICES": "0,1", "STRATA_DFLASH_SAMPLED": "1",
                "STRATA_WATCHDOG_S": "300", "STRATA_ENGINE_NICE": "0"},
        "mcp_servers": {},
    }


class World:
    """Fake host: procfs, listeners, pidfd, health, REAL acceptance emitter, parity tool, clock."""

    def __init__(self, run_dir: Path, cfg: dict) -> None:
        self.run_dir, self.cfg = run_dir, cfg
        self.canon = os.path.realpath(cfg["exe"])
        self.procs: dict[int, Proc] = {}
        self.listeners_map: dict[int, list] = {drv.PROD_PORT: [{"addr": "127.0.0.1:8009",
                                                                "pid": PROD_PID, "line": "L"}]}
        self.next_pid, self.next_fd = 5000, 90
        self.fds: dict[int, int] = {}
        self.signals: list = []
        self.popen_calls: list = []
        self.captures: list = []
        self.now = 1000.0
        self.script: list[dict] = []            # per-request outcomes, consumed in order
        self.parity_rc, self.parity_out = 0, REPORT_STDOUT
        self.wrap_argv = [str(drv.PY), "serve/server.py", "--engine", "strata", "--config",
                          str(drv.PROD_CONFIG), "--port", str(drv.PROD_PORT)]
        self.add(Proc(PROD_PID, list(self.wrap_argv), str(drv.MERGED_CWD), self.canon, 7, 1000,
                      PROD_START, [PROD_ENGINE_PID]))
        self.add(Proc(PROD_ENGINE_PID, drv.expected_engine_argv({**cfg, "exe": self.canon}),
                      cfg["cwd"], self.canon, 7, 1001, 120, []))

    def add(self, p: Proc) -> Proc:
        self.procs[p.pid] = p
        return p

    # ------------------------------------------------------------------ procfs / pidfd
    def cmdline(self, pid): return self.procs[pid].argv
    def starttime(self, pid): return self.procs[pid].starttime
    def exe(self, pid):
        p = self.procs[pid]
        return p.exe, p.dev, p.ino
    def cwd(self, pid): return self.procs[pid].cwd
    def children(self, pid): return list(self.procs[pid].children)
    def alive(self, pid): return pid in self.procs and self.procs[pid].alive
    def listeners(self, port): return list(self.listeners_map.get(port, []))

    def pidfd_open(self, pid):
        if pid not in self.procs:
            raise drv.PidfdGone(f"pidfd_open({pid}): ESRCH")
        fd = self.next_fd
        self.next_fd, self.fds[fd] = fd + 1, pid
        return fd

    def pidfd_signal(self, fd, sig):
        pid = self.fds[fd]
        self.signals.append((pid, int(sig)))
        p = self.procs[pid]
        if p.ignore_signals:
            return
        p.alive, p.rc = False, -int(sig)
        for kid in p.children:
            k = self.procs.get(kid)
            if k is not None and k.alive:
                k.alive, k.rc = False, -int(sig)
        if pid == PROD_PID:
            self.listeners_map.pop(drv.PROD_PORT, None)

    def pidfd_death(self, fd, timeout):
        p = self.procs[self.fds[fd]]
        return (not p.alive) and not p.ignore_signals
    def pidfd_close(self, fd): self.fds.pop(fd, None)

    # ------------------------------------------------------------------ popen
    def popen(self, argv, cwd, env, out, err):
        argv = [str(a) for a in argv]
        pid = self.next_pid
        self.next_pid += 1
        if argv == [str(drv.LAUNCHER)]:
            argv, cwd = list(self.wrap_argv), str(drv.MERGED_CWD)
        kids = []
        if "--port" in argv:
            cfg = json.loads(Path(argv[argv.index("--config") + 1]).read_text(encoding="utf-8"))
            kid = self.next_pid
            self.next_pid += 1
            self.add(Proc(kid, drv.expected_engine_argv(cfg), cfg["cwd"],
                          os.path.realpath(cfg["exe"]), 7, 2000 + kid, 150, []))
            Path(cfg["log"]).write_text("# engine log (fixture engine opened it)\n",
                                        encoding="utf-8")
            kids = [kid]
            Path(out).write_text(f"{drv.WRAPPER_READY}\n", encoding="utf-8")
            if argv[-1] == str(drv.PROD_PORT):
                self.listeners_map[drv.PROD_PORT] = [{"addr": "", "pid": pid, "line": "L"}]
        if argv[0] == str(drv.CAND_HELPER):
            Path(out).write_text(f"{drv.HELPER_READY} {argv[argv.index('--socket') + 1]}\n",
                                 encoding="utf-8")
        self.popen_calls.append({"argv": argv, "pid": pid})
        return self.add(Proc(pid, argv, cwd, os.path.realpath(argv[0]), 7, 3000 + pid, 200, kids))

    def candidate_wrapper_pid(self):
        for pid, p in self.procs.items():
            av = [str(a) for a in p.argv]
            if str(drv.CAND_PORT) in av and str(drv.PROD_PORT) not in av:
                return pid
        return None

    def restart_engine(self):
        """Simulate the watchdog's lazy restart: the recorded child GONE, a NEW identity under
        the SAME candidate wrapper.  Returns the new pid."""
        w = self.candidate_wrapper_pid()
        old = None
        if w is not None:
            for kid in self.procs[w].children:
                p = self.procs.get(kid)
                if p is not None and p.exe == os.path.realpath(str(drv.CAND_ENGINE)):
                    old = kid
        if old is not None:
            self.procs[old].alive, self.procs[old].rc = False, 0
        pid = self.next_pid
        self.next_pid += 1
        cfg = self.cfg
        self.add(Proc(pid, drv.expected_engine_argv({**cfg, "exe": str(drv.CAND_ENGINE)}),
                      cfg["cwd"], os.path.realpath(str(drv.CAND_ENGINE)), 7, 4000 + pid,
                      150 + pid, []))                       # new starttime => identity change
        if w is not None:
            self.procs[w].children = [k for k in self.procs[w].children if k != old] + [pid]
        return pid

    # ------------------------------------------------------------------ capture: REAL emitter
    @staticmethod
    def _ok_body(spec: dict) -> bytes:
        body = {"id": "c1", "object": "chat.completion", "model": "swift-1.5-iq3_xxs",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok text"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 40, "total_tokens": 160}}
        if spec.get("timings"):
            body["timings"] = spec["timings"]
        return json.dumps(body).encode()

    @staticmethod
    def _log_specs(argv: list[str]) -> dict[str, str]:
        logs = {}
        for i, a in enumerate(argv):
            if a == "--log":
                label, _, path = argv[i + 1].partition("=")
                logs[label] = path
        return logs

    def capture(self, argv, cwd, timeout):
        argv = [str(a) for a in argv]
        self.captures.append(argv)
        if argv[1] == str(ad.PARITY_TOOL):                    # the parity tool subprocess
            return self.parity_rc, self.parity_out, ""
        acc_argv = argv[2:]                                   # drop python + utility paths
        label = Path(acc_argv[acc_argv.index("--out") + 1]).name.replace("evidence-", "")
        spec = self.script.pop(0) if self.script else {"status": 200}
        eng = self._log_specs(acc_argv).get("engine")
        if spec.get("delete_log_before_capture") and eng:
            Path(eng).unlink()                                # REFUSED at --log validation

        def transport(host, port, path, timeout_, body, connect):
            if spec.get("transport_error"):
                raise ConnectionRefusedError(spec["transport_error"])
            if spec.get("raise") == "interrupt":
                raise KeyboardInterrupt("SIGTERM")
            if spec.get("delete_engine_log") and eng:
                Path(eng).unlink()                            # vanishes mid-request
            if eng and (spec.get("notice") or spec.get("append_engine")):
                with Path(eng).open("a", encoding="utf-8") as fh:
                    if spec.get("notice"):
                        fh.write(NOTICE_LINE)
                    if spec.get("append_engine"):
                        fh.write(spec["append_engine"] + "\n")
            info = {"status": spec.get("status", 200), "reason": spec.get("reason"),
                    "headers": []}
            chunk = spec.get("body")
            chunk = self._ok_body(spec) if chunk is None else \
                (chunk.encode() if isinstance(chunk, str) else chunk)
            return info, iter([(self.now, self.now, chunk)])

        hooks = acc.FaultHooks(transport=transport,
                               connect=lambda *a: (_ for _ in ()).
                               throw(AssertionError("no connect in tests")),
                               wall=lambda: self.now, mono=lambda: self.now,
                               sleep=lambda s: None)
        rc = acc.main(acc_argv, hooks)                        # the REAL record emitter
        return rc, f"captured {label}\n", ""

    def health(self, host, port, timeout):
        return {"status": "ok", "loaded": True, "model": "swift-1.5-iq3_xxs"}

    def sha(self, path): return _fsha(path)
    def poll(self, p): return p.rc
    def reap(self, p):
        if p.rc is not None:
            p.alive = False
        return p.rc
    def sleep(self, s): self.now += s

    def hooks(self):
        return drv.Hooks(popen=lambda *a: self.popen(*a), poll=lambda p: self.poll(p),
                         reap=lambda p: self.reap(p), pidfd_open=lambda pid: self.pidfd_open(pid),
                         pidfd_signal=lambda fd, sig: self.pidfd_signal(fd, sig),
                         pidfd_death=lambda fd, t: self.pidfd_death(fd, t),
                         pidfd_close=lambda fd: self.pidfd_close(fd),
                         cmdline=lambda pid: self.cmdline(pid),
                         starttime=lambda pid: self.starttime(pid), exe=lambda pid: self.exe(pid),
                         cwd=lambda pid: self.cwd(pid), children=lambda pid: self.children(pid),
                         alive=lambda pid: self.alive(pid),
                         listeners=lambda port: self.listeners(port),
                         health=lambda *a: self.health(*a), capture=lambda *a: self.capture(*a),
                         sha256=lambda p: self.sha(p), wall=lambda: self.now,
                         mono=lambda: self.now, sleep=lambda s: self.sleep(s))

    def events(self):
        p = self.run_dir / "manifest.jsonl"
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() \
            else []

    def kinds(self): return [e["event"] for e in self.events()]

    def summary(self): return json.loads((self.run_dir / "summary.json").read_text())

    def arm_outcome(self): return self.summary()["arm_outcome"]


@pytest.fixture(autouse=True)
def _no_sockets(monkeypatch):
    """The suite must be socket-free: any real socket use raises immediately."""
    class _Blocked(socket.socket):
        def __init__(self, *a, **k):
            raise AssertionError("test suite must not use sockets")
    monkeypatch.setattr(socket, "socket", _Blocked)


@pytest.fixture(autouse=True)
def _restore_module_state():
    """Findings §8: every test ends with the adapter/driver module constants as found."""
    snap = {k: getattr(ad, k) for k in _ORIG}
    cand = drv.CAND_ENGINE
    yield
    for k, v in snap.items():
        setattr(ad, k, v)
    drv.CAND_ENGINE = cand


@pytest.fixture(autouse=True)
def _clean():
    yield
    shutil.rmtree(_SHORT_BASE, ignore_errors=True)


@pytest.fixture
def host(tmp_path_factory, monkeypatch):
    hs = _SHORT_BASE / f"h{next(_SEQ)}"
    (hs / "bin").mkdir(parents=True)
    (hs / "prod").mkdir(parents=True)
    (hs / "merged" / "serve").mkdir(parents=True)
    (hs / "models").mkdir(parents=True)
    (hs / "build" / "engine-hip").mkdir(parents=True)
    (hs / "build" / "engine-cuda").mkdir(parents=True)
    (hs / "build" / "r3-driver").mkdir(parents=True)
    (hs / "tools").mkdir(parents=True)
    (hs / ".venv").mkdir()
    for f in (hs / "bin" / "mcp", hs / "prod" / "strata", hs / "models" / "d.gguf",
              hs / "build" / "engine-hip" / "strata-red",
              hs / "build" / "engine-hip" / "strata-cand",
              hs / "build" / "engine-cuda" / "strata-expert-helper", hs / ".venv" / "py"):
        f.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        f.chmod(0o755)
    (hs / "merged" / "serve" / "server.py").write_text("# fixture\n", encoding="utf-8")
    (hs / "tools" / "parity.py").write_text("# fixture parity tool\n", encoding="utf-8")
    (hs / "tools" / "capture.py").write_text("# fixture capture\n", encoding="utf-8")
    cfg = fixture_config(hs)
    (hs / "config.json").write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    launcher = hs / "launch-prod.sh"
    launcher.write_text("#!/bin/sh\nexec serve\n", encoding="utf-8")
    for name, value in {"PY": hs / ".venv" / "py", "LAUNCHER": launcher,
                        "PROD_CONFIG": hs / "config.json", "MERGED_CWD": hs / "merged",
                        "CAND_ENGINE": hs / "build" / "engine-hip" / "strata-red",
                        "CAND_HELPER": hs / "build" / "engine-cuda" / "strata-expert-helper",
                        "UTILITY": hs / "tools" / "capture.py",
                        "LOCK_PATH": hs / "build" / "r3-driver" / "driver.lock"}.items():
        monkeypatch.setattr(drv, name, value)
    monkeypatch.setattr(ad, "PARITY_TOOL", hs / "tools" / "parity.py")
    # the fake sha256 seam hashes the PATH STRING: pin the adapter constants to those digests
    monkeypatch.setattr(ad, "DRIVER_SHA256", _fsha(ROOT / "tools" / "mixed_gpu" /
                                                    "run_acceptance.py"))
    monkeypatch.setattr(ad, "CAPTURE_SHA256", _fsha(hs / "tools" / "capture.py"))
    monkeypatch.setattr(ad, "CONFIG_SHA256", _fsha(hs / "config.json"))
    monkeypatch.setattr(ad, "ROLES", {
        "red": {"path": str(hs / "build" / "engine-hip" / "strata-red"),
                "sha256": _fsha(hs / "build" / "engine-hip" / "strata-red")},
        "cand": {"path": str(hs / "build" / "engine-hip" / "strata-cand"),
                 "sha256": _fsha(hs / "build" / "engine-hip" / "strata-cand")}})
    return hs


def plan(world, arm, extra=()):
    return ad.main(["--arm", arm, "--config", str(drv.PROD_CONFIG), "--out-dir",
                    str(world.run_dir)] + list(extra), world.hooks())


def run(world, arm, extra=()):
    return ad.main(["--arm", arm, "--execute", "--expected-wrapper-pid", str(PROD_PID),
                    "--expected-wrapper-starttime", str(PROD_START),
                    "--config", str(drv.PROD_CONFIG), "--out-dir", str(world.run_dir)] +
                   list(extra), world.hooks())


@pytest.fixture
def world(host):
    cfg = json.loads((host / "config.json").read_text(encoding="utf-8"))
    w = World(_SHORT_BASE / f"r{next(_SEQ)}", cfg)
    return w


def with_sample_pin(w):
    ad.SAMPLE_SHA256 = _fsha(w.run_dir / "request-sample.json")
    return w


def alt(world, suffix):
    return World(Path(str(world.run_dir) + suffix), world.cfg)


def sha_lines(w) -> list[str]:
    """Every line of the run's output-shas.tsv, in order."""
    return (w.run_dir / ad.OUTPUT_SHA_FILE).read_text(encoding="utf-8").splitlines()


def sha_header(w) -> list[str]:
    return [ln for ln in sha_lines(w) if ln.startswith("#")]


def sha_rows(w) -> list[dict]:
    """The TSV data rows keyed by the adapter's documented columns ('' = empty field)."""
    return [dict(zip(ad.OUTPUT_SHA_COLUMNS, ln.split("\t")))
            for ln in sha_lines(w) if not ln.startswith("#")]


def save_mock_evidence(w, name: str) -> None:
    """Opt-in CPU evidence copy (NO-OP when the env var is unset, so normal runs are unchanged):
    STRATA_T0_SHA_EVIDENCE_DIR copies the generated output-shas.tsv and the sha-bearing manifest
    rows of THIS mock run so the file shape is reviewable without any hardware."""
    dest = os.environ.get("STRATA_T0_SHA_EVIDENCE_DIR")
    if not dest:
        return
    out = Path(dest)
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy(w.run_dir / ad.OUTPUT_SHA_FILE, out / f"{name}-output-shas.tsv")
    rows = [e for e in w.events() if e["event"] in ("output_sha_file", "arm_request")]
    (out / f"{name}-manifest-rows.jsonl").write_text(
        "".join(json.dumps(r, sort_keys=True, default=str) + "\n" for r in rows), encoding="utf-8")


# ------------------------------------------------------------------ §8 module-state discipline
# (file order matters: the mutation test runs first, the baseline check right after)

def test_module_state_is_restored_between_tests():
    ad.SAMPLE_SHA256 = "deadbeef"
    ad.PARITY_BYTE_CAP = -1
    ad.TOOL_VERSION = "mutated"
    ad.OUTPUT_SHA_FILE = "mutated-shas.tsv"
    ad.OUTPUT_SHA_HEADER = ("# mutated",)
    drv.CAND_ENGINE = Path("/mutated/cand")
    assert ad.SAMPLE_SHA256 == "deadbeef"


def test_module_state_was_restored_before_this_test():
    assert ad.SAMPLE_SHA256 == _ORIG["SAMPLE_SHA256"] == ad.__dict__["SAMPLE_SHA256"]
    assert ad.PARITY_BYTE_CAP == 2 * 1024 ** 3
    assert ad.TOOL_VERSION == "t0-numeric-2"
    assert ad.OUTPUT_SHA_FILE == _ORIG["OUTPUT_SHA_FILE"] == "output-shas.tsv"
    assert ad.OUTPUT_SHA_HEADER == _ORIG["OUTPUT_SHA_HEADER"]
    assert ad.OUTPUT_SHA_COLUMNS == _ORIG["OUTPUT_SHA_COLUMNS"]
    assert drv.CAND_ENGINE == _ORIG_CAND


# ------------------------------------------------------------------ finite arms & plan

def test_arm_table_is_the_finite_named_set():
    assert set(ad.ARMS) == {"num-red-graphon", "num-red-graphoff", "num-red-map",
                            "num-red-parity", "num-cand-graphon", "num-cand-graphoff",
                            "decode-baseline"}
    assert ad.ARMS["decode-baseline"].helper == "on"
    assert ad.ARMS["num-cand-graphon"].followup and not ad.ARMS["num-cand-graphoff"].followup
    assert ad.ARMS["num-red-parity"].parity and (ad.ARMS["num-red-parity"].samples == 1)


def test_plan_performs_no_side_effects(world, host):
    with_sample_pin(world)
    assert plan(world, "num-red-graphoff") == drv.EXIT_DONE
    assert world.popen_calls == [] and world.captures == [] and world.signals == []
    view = json.loads((world.run_dir / "plan.json").read_text())
    assert view["claims"] == {"numeric_pass": False, "root_cause_found": False,
                              "availability_only": True, "speed_claim": False,
                              "all3gpu_phase_proven": False, "measured_by_this_tool": False}
    assert view["binding"]["role"] == "red"
    assert view["binding"]["cand_engine_bound_to"] == view["binding"]["role_path"]
    assert view["lock_path"] == str(drv.LOCK_PATH)          # ONE global lock, not per-out-dir
    assert view["cap_monitored_not_enforced"] is True


def test_plan_labels_and_env_pins(world):
    with_sample_pin(world)
    plan(world, "num-red-graphon")
    v = json.loads((world.run_dir / "plan.json").read_text())
    labels = [r["label"] for r in v["requests"]]
    assert labels == ["num-red-graphon-warm1", "num-red-graphon-warm2",
                      "num-red-graphon-sample1", "num-red-graphon-sample2",
                      "num-red-graphon-sample3"]              # red arms get NO follow-up
    assert v["engine_env"]["STRATA_DFLASH_SAMPLED"] == "1"          # canonical keys kept
    assert v["engine_env"]["CUDA_VISIBLE_DEVICES"] == "0,1"        # no topology change
    assert v["graph_controls_effective"]["STRATA_DFLASH_GRAPH"] == "<absent>"
    w_cand = alt(world, "-cand")
    ad.SAMPLE_SHA256 = _fsha(w_cand.run_dir / "request-sample.json")
    assert plan(w_cand, "num-cand-graphon") == drv.EXIT_DONE
    v2 = json.loads((w_cand.run_dir / "plan.json").read_text())
    assert v2["requests"][-1]["label"] == "num-cand-graphon-followup"
    assert v2["binding"]["role"] == "cand"
    assert v2["binding"]["cand_engine_bound_to"] == ad.ROLES["cand"]["path"]
    w_map = alt(world, "-map")
    ad.SAMPLE_SHA256 = _fsha(w_map.run_dir / "request-sample.json")
    assert plan(w_map, "num-red-map") == drv.EXIT_DONE
    v3 = json.loads((w_map.run_dir / "plan.json").read_text())
    assert v3["engine_env"]["STRATA_DF_DBG"] == "1" and v3["engine_env"]["STRATA_DFLASH_GRAPH"] == "0"
    assert v3["graph_controls_effective"]["STRATA_DFLASH_GRAPH"] == "0"


# ------------------------------------------------------------------ §1.1 binding consistency

def test_plan_binds_role_before_prepare_and_every_artifact_agrees(world, host):
    """The plan's binding row, the ids, the driver's composite build-id and BOTH derived
    configs must name the SAME frozen role - checked in the written artifacts (§1.1)."""
    with_sample_pin(world)
    role = str(host / "build" / "engine-hip" / "strata-red")
    assert plan(world, "num-red-graphon") == drv.EXIT_DONE
    v = json.loads((world.run_dir / "plan.json").read_text())
    b = v["binding"]
    assert b["role_path"] == role and b["role_sha256_used"] == _fsha(role)
    assert all(v["identity_consistency"].values())
    ids = v["ids"]                                          # the adapter ids (plan.json)
    composite = json.loads((world.run_dir / "build-sha.json").read_text())
    off = json.loads((world.run_dir / "config-off.json").read_text())
    on = json.loads((world.run_dir / "config-on.json").read_text())
    assert ids["parts"]["role_binary"] == role
    assert ids["sha256"]["role_binary"] == b["role_sha256_used"]
    assert composite["parts"]["engine_hip"] == role           # the driver composite followed it
    assert composite["sha256"]["engine_hip"] == b["role_sha256_used"]
    assert off["exe"] == role and on["exe"] == role           # derived configs followed it
    assert str(drv.CAND_ENGINE) == role                       # bound in-process before prepare
    assert b["cand_engine_bound_from"] == str(host / "build" / "engine-hip" / "strata-red")


def test_plan_cand_arm_binds_the_cand_role_everywhere(world, host):
    w = alt(world, "-cand")
    ad.SAMPLE_SHA256 = _fsha(w.run_dir / "request-sample.json")
    role = str(host / "build" / "engine-hip" / "strata-cand")
    assert plan(w, "num-cand-graphon") == drv.EXIT_DONE
    v = json.loads((w.run_dir / "plan.json").read_text())
    composite = json.loads((w.run_dir / "build-sha.json").read_text())
    off = json.loads((w.run_dir / "config-off.json").read_text())
    assert v["binding"]["role_path"] == role
    assert composite["parts"]["engine_hip"] == role
    assert off["exe"] == role
    assert v["binding"]["cand_engine_bound_to"] == role


# ------------------------------------------------------------------ parity plan labels (§1.4/§1.5)

def test_parity_plan_requires_typed_cycle_and_labels_the_cap_as_observed(world):
    with_sample_pin(world)
    assert plan(world, "num-red-parity") == drv.EXIT_REFUSED        # no cycle -> refusal
    assert plan(world, "num-red-graphon", ["--parity-cycle", "3"]) == drv.EXIT_REFUSED
    assert plan(world, "num-red-parity", ["--parity-cycle", "-1"]) == drv.EXIT_REFUSED
    assert plan(world, "num-red-parity", ["--parity-cycle", "7"]) == drv.EXIT_DONE
    v = json.loads((world.run_dir / "plan.json").read_text())
    assert v["engine_env"]["STRATA_DF_PARITY_CYCLE"] == "7"
    assert v["engine_env"]["STRATA_DF_PARITY"] == f"{world.run_dir}/parity/num-red-parity"
    assert v["parity_byte_cap_observed"] == ad.PARITY_BYTE_CAP
    assert v["cap_monitored_not_enforced"] is True
    assert v["arm_spec"]["parity_execute_refused_at_gate"] is True


def test_parity_execute_is_refused_before_any_ownership_or_downtime(world):
    with_sample_pin(world)
    assert run(world, "num-red-parity", ["--parity-cycle", "3"]) == drv.EXIT_REFUSED
    assert world.popen_calls == [] and world.signals == [] and world.captures == []
    assert not Path(drv.LOCK_PATH).exists()                        # no lock, no ownership
    assert not (world.run_dir / "parity").exists()                 # no downtime, no dirs touched


def test_parity_plan_records_the_weight_state_and_gguf_override(world, monkeypatch):
    with_sample_pin(world)
    # the fixture d.gguf is not a real GGUF: the typed gate says UNRUN_TYPE, plan stays allowed
    v_ok = plan(world, "num-red-parity", ["--parity-cycle", "7"])
    v = json.loads((world.run_dir / "plan.json").read_text())
    assert v_ok == drv.EXIT_DONE
    assert v["binding"]["parity_weight_state"] == "UNRUN_TYPE"
    assert v["binding"]["parity_gguf_used"] == world.cfg["args"][
        world.cfg["args"].index("--dflash") + 1]
    w2 = alt(world, "-bf16")
    ad.SAMPLE_SHA256 = _fsha(w2.run_dir / "request-sample.json")
    monkeypatch.setattr(ad, "bf16_compatible", lambda p: (True, "all tensors original BF16 type 30"))
    assert plan(w2, "num-red-parity", ["--parity-cycle", "7"]) == drv.EXIT_DONE
    v2 = json.loads((w2.run_dir / "plan.json").read_text())
    assert v2["binding"]["parity_weight_state"] == "OK"


def test_parity_gguf_override_needs_pair_and_hash(world, monkeypatch):
    with_sample_pin(world)
    monkeypatch.setattr(ad, "bf16_compatible", lambda p: (True, "all tensors original BF16 type 30"))
    g = str(world.cfg["args"][world.cfg["args"].index("--dflash") + 1])
    base = ["--parity-cycle", "7"]
    assert plan(world, "num-red-parity", base + ["--parity-gguf", g]) == drv.EXIT_REFUSED
    assert plan(world, "num-red-parity", base + ["--parity-gguf", g,
                                                  "--parity-gguf-sha256", "abc"]) == drv.EXIT_REFUSED
    assert plan(world, "num-red-parity", base + ["--parity-gguf", g,
                                                  "--parity-gguf-sha256", "f" * 64]) == \
        drv.EXIT_REFUSED                                          # digest mismatch
    assert plan(world, "num-red-parity", base + ["--parity-gguf", g,
                                                  "--parity-gguf-sha256", _fsha(g)]) == \
        drv.EXIT_DONE
    v = json.loads((world.run_dir / "plan.json").read_text())
    assert v["binding"]["parity_gguf_used"] == g
    assert v["binding"]["parity_gguf_sha256"] == _fsha(g)


# ------------------------------------------------------------------ §1.5 typed GGUF predicate

def test_bf16_predicate_is_typed_on_tensor_type_ids(world, monkeypatch):
    monkeypatch.setattr(ad, "gguf_tensor_type_ids", lambda p: [30, 30, 30])
    ok, why = ad.bf16_compatible(Path("x.gguf"))
    assert ok and "type 30" in why
    monkeypatch.setattr(ad, "gguf_tensor_type_ids", lambda p: [30, 29, 24])
    ok, why = ad.bf16_compatible(Path("x.gguf"))
    assert not ok and "29" in why                                  # quantized drafter
    monkeypatch.setattr(ad, "gguf_tensor_type_ids", lambda p: [])
    ok, why = ad.bf16_compatible(Path("x.gguf"))
    assert not ok and "no tensors" in why

    def boom(p):
        raise ValueError("bad magic")
    monkeypatch.setattr(ad, "gguf_tensor_type_ids", boom)
    ok, why = ad.bf16_compatible(Path("x.gguf"))
    assert not ok and "unreadable GGUF header" in why


@pytest.mark.skipif(not _CANON_GGUF.exists(), reason="host integration: canonical drafter absent")
def test_canonical_q4_drafter_is_not_bf16_compatible_real_header():
    """Host integration (read-only header parse via tools/gguf_reader.py): the Q4 drafter
    CANNOT run the BF16 stage oracle -> UNRUN_TYPE is the honest label."""
    ok, why = ad.bf16_compatible(_CANON_GGUF)
    assert ok is False and "30" in why


# ------------------------------------------------------------------ §1.5 parity verdict contract

def _parity_fixture_d(tmp_path, capture_out):
    calls: list = []

    def cap(argv, cwd, timeout):
        calls.append(argv)
        return capture_out
    m = drv.Manifest(tmp_path / "manifest.jsonl", drv.Hooks(wall=lambda: 1000.0))
    d = types.SimpleNamespace(m=m, h=drv.Hooks(capture=cap))
    args = types.SimpleNamespace(arm="num-red-parity", parity_cycle=3, out_dir=tmp_path)
    return d, args, calls


def _parity_binding(bf16: bool):
    return {"parity_gguf_used": "/parent/supplied/bf16.gguf",
            "parity_gguf_bf16_compatible": bf16,
            "parity_weight_state": "OK" if bf16 else "UNRUN_TYPE",
            "parity_weight_reason": "all tensors original BF16 type 30" if bf16
                                    else "quantized drafter weights (tensor type ids [29] != 30)"}


def test_parity_tool_runs_only_with_fixtures_and_bf16(tmp_path):
    d, args, calls = _parity_fixture_d(tmp_path, (0, REPORT_STDOUT, ""))
    pdir = tmp_path / "parity"
    pdir.mkdir()
    (pdir / "meta.bin").write_bytes(b"x" * 64)
    out = ad.run_parity_analysis(d, ad.ARMS["num-red-parity"], args, _parity_binding(True), pdir)
    assert out["verdict"] == "TOOL_REPORT_RC0" and out["rc"] == 0
    assert out["report_line_present"] is True and out["fixtures_present"] is True
    assert len(calls) == 1 and calls[0][calls[0].index("--dir") + 1] == str(pdir)
    assert calls[0][calls[0].index("--dflash") + 1] == "/parent/supplied/bf16.gguf"
    assert out["cap_monitored_not_enforced"] is True


def test_parity_report_rc1_is_a_report_not_a_pass(tmp_path):
    d, args, calls = _parity_fixture_d(tmp_path, (1, REPORT_STDOUT, "stage parity"))
    pdir = tmp_path / "parity"
    pdir.mkdir()
    (pdir / "meta.bin").write_bytes(b"x" * 64)
    out = ad.run_parity_analysis(d, ad.ARMS["num-red-parity"], args, _parity_binding(True), pdir)
    assert out["verdict"] == "TOOL_REPORT_RC1" and out["report_line_present"] is True


def test_parity_crash_without_report_line_is_not_a_report(tmp_path):
    d, args, calls = _parity_fixture_d(tmp_path, (1, "", "Traceback (most recent call last):\n..."))
    pdir = tmp_path / "parity"
    pdir.mkdir()
    (pdir / "meta.bin").write_bytes(b"x" * 64)
    out = ad.run_parity_analysis(d, ad.ARMS["num-red-parity"], args, _parity_binding(True), pdir)
    assert out["verdict"] == "TOOL_CRASH" and out["report_line_present"] is False
    assert "NOT a PASS" in out["note"]
    junk = tmp_path / "junk"                                # a fake 0 on junk meta is NOT accepted
    junk.mkdir()
    d0, a0, c0 = _parity_fixture_d(junk, (0, "", ""))
    jd = junk / "p"
    jd.mkdir()
    (jd / "meta.bin").write_bytes(b"x")
    out0 = ad.run_parity_analysis(d0, ad.ARMS["num-red-parity"], a0, _parity_binding(True), jd)
    assert out0["verdict"] == "TOOL_CRASH"                  # rc 0 with no report line is not RC0


def test_parity_quantized_weight_is_unrun_type_never_invoked(tmp_path):
    d, args, calls = _parity_fixture_d(tmp_path, (0, REPORT_STDOUT, ""))
    pdir = tmp_path / "parity"
    pdir.mkdir()
    (pdir / "meta.bin").write_bytes(b"x" * 64)
    out = ad.run_parity_analysis(d, ad.ARMS["num-red-parity"], args, _parity_binding(False), pdir)
    assert out["verdict"] == "UNRUN_TYPE" and calls == []   # exact command, NOT invoked
    assert out["command"][1] == str(ad.PARITY_TOOL)
    assert "no download" in out["note"]


def test_parity_missing_fixtures_is_unrun_fixtures_not_invented(tmp_path):
    d, args, calls = _parity_fixture_d(tmp_path, (0, REPORT_STDOUT, ""))
    pdir = tmp_path / "parity"
    pdir.mkdir()                                            # no meta.bin
    out = ad.run_parity_analysis(d, ad.ARMS["num-red-parity"], args, _parity_binding(True), pdir)
    assert out["verdict"] == "UNRUN_FIXTURES" and calls == []
    assert out["command"][1] == str(ad.PARITY_TOOL)         # exact command recorded


def test_cap_observer_is_read_only_with_bounded_join(tmp_path):
    """§1.4: the observer OBSERVES (bytes/polls/breach), never signals, never claims a cap stop."""
    monkey_dir = tmp_path / "dump"
    monkey_dir.mkdir()
    (monkey_dir / "meta.bin").write_bytes(b"x" * 100)        # starts under the cap
    ad.CAP_POLL_S = 0.05
    stop, state = ad.start_cap_observer(monkey_dir, 1024)
    time.sleep(0.12)                                         # a couple of periodic polls
    (monkey_dir / "meta.bin").write_bytes(b"x" * 2048)       # the dump grows past the cap
    for _ in range(60):
        if state["breach_observed"] is not None:
            break
        time.sleep(0.05)
    assert state["breach_observed"] is not None
    assert state["breach_observed"]["bytes"] > 1024
    assert "no signal was sent" in state["breach_observed"]["note"]
    assert state["cap_monitored_not_enforced"] is True
    stop.set()
    stop.wait(timeout=ad.CAP_POLL_S + 1.0)                  # bounded join, thread is a daemon
    assert state["polls"] >= 2
    assert (monkey_dir / "meta.bin").stat().st_size == 2048  # observer never writes


# ------------------------------------------------------------------ finite arms CLI / hash gates

def test_unknown_arm_is_refused_by_the_cli(world):
    with_sample_pin(world)
    with pytest.raises(SystemExit):                      # argparse: closed choice set
        ad.main(["--arm", "num-red-combobox", "--out-dir", str(world.run_dir)], world.hooks())


def test_role_hash_mismatch_refuses_before_anything(world, host):
    with_sample_pin(world)
    ad.ROLES["red"]["sha256"] = "0" * 64                 # pinned expectation drifts
    assert plan(world, "num-red-graphon") == drv.EXIT_REFUSED
    assert world.popen_calls == [] and world.signals == [] and world.captures == []


def test_role_override_requires_full_hash(world):
    with_sample_pin(world)
    eng = str(Path(ad.ROLES["red"]["path"]))
    assert plan(world, "num-red-graphon", ["--red-engine", eng]) == drv.EXIT_REFUSED
    assert plan(world, "num-red-graphon", ["--red-engine", eng, "--red-sha256", "abc"]) == \
        drv.EXIT_REFUSED
    assert plan(world, "num-red-graphon", ["--red-sha256", "f" * 64]) == drv.EXIT_REFUSED
    good = _fsha(eng)
    assert plan(world, "num-red-graphon", ["--red-engine", eng, "--red-sha256", good]) == \
        drv.EXIT_DONE
    v = json.loads((world.run_dir / "plan.json").read_text())
    assert v["binding"]["role_override"] and v["binding"]["role_sha256_used"] == good


def test_frozen_import_and_config_hashes_gate_plan(world, host, monkeypatch):
    with_sample_pin(world)
    monkeypatch.setattr(ad, "CAPTURE_SHA256", "1" * 64)   # frozen capture drift
    assert plan(world, "decode-baseline") == drv.EXIT_REFUSED
    monkeypatch.setattr(ad, "CAPTURE_SHA256", _fsha(drv.UTILITY))
    monkeypatch.setattr(ad, "CONFIG_SHA256", "2" * 64)    # canonical config drift
    assert plan(world, "decode-baseline") == drv.EXIT_REFUSED
    assert world.popen_calls == [] and world.signals == []


def test_pins_equal_the_real_frozen_repo_files():
    """Repo files only (self-contained): the accepted driver and capture digests."""
    d = hashlib.sha256((ROOT / "tools" / "mixed_gpu" / "run_acceptance.py").read_bytes()).hexdigest()
    c = hashlib.sha256((ROOT / "tools" / "mixed_gpu" / "acceptance.py").read_bytes()).hexdigest()
    assert ad.DRIVER_SHA256 == d == "dea0e7d73f15b082cb7b5f3c98ddad5bd8e12fac89e064d9ca030109835238f2"
    assert ad.CAPTURE_SHA256 == c == "06e9fbd237055196ea89345a94594664591600c8357ff6dd9ccbc1aa0f0a4af7"
    assert ad.SAMPLE_SHA256 == "93d4b9ca1524c26e0d03309d044a3c6898289e0a52fa9d19aa8daabae07dfe8d"


@pytest.mark.skipif(not _CANON_CONFIG.exists(), reason="host integration: canonical config absent")
def test_canonical_config_pin_matches_host_file():
    """Host integration (read-only): the pinned canonical config digest on this host."""
    cfg = hashlib.sha256(_CANON_CONFIG.read_bytes()).hexdigest()
    assert ad.CONFIG_SHA256 == cfg == "4dbe460a484c53b435ecd8eeb909fb2247cff85c7ccdfb5573a332259a347afa"


def test_adapter_source_uses_no_forbidden_lifecycle_calls():
    src = ADAPTER_PATH.read_text()
    tree = ast.parse(src)
    body = src
    docstrings = [n for n in ast.walk(tree)
                  if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    for node in docstrings:                                   # remove whole docstrings first
        doc = getattr(node, "docstring", None)
        if isinstance(doc, str) and doc:
            body = body.replace(doc, "", 1)
    strings = [n.value for n in ast.walk(tree)
               if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    for s in sorted(set(strings), key=len, reverse=True):     # longest constants first
        body = body.replace(s, "")
    body = "\n".join(ln.split("#")[0] for ln in body.splitlines())   # strip comments too
    banned = {"kill", "killpg", "pgrep", "pkill", "start_new_session", "setsid", "unlink",
              "system"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in banned:
            raise AssertionError(f"forbidden lifecycle call surface: {node.attr}")
        if isinstance(node, ast.ImportFrom) and node.module == "socket":
            raise AssertionError("adapter must not import socket")
        if isinstance(node, ast.Import) and any(a.name == "socket" for a in node.names):
            raise AssertionError("adapter must not import socket")
    assert "os.kill" not in body and "start_new_session" not in body and "pgrep" not in body


def test_notice_stderr_precondition_is_recorded(world):
    """§7 finding: the handled-200 class depends on the notice in the ENGINE log; the plan
    states the precondition explicitly so a silent window cannot pretend it held."""
    with_sample_pin(world)
    assert plan(world, "num-cand-graphon") == drv.EXIT_DONE
    v = json.loads((world.run_dir / "plan.json").read_text())
    pre = v["binding"]["notice_stderr_precondition"]
    assert ad.REFUSAL_NOTICE_MARK in pre and "parent must confirm" in pre


# ------------------------------------------------------------------ execute: refusals, no signal

def test_execute_requires_fresh_identities(world):
    with_sample_pin(world)
    assert ad.main(["--arm", "num-red-graphon", "--execute", "--config", str(drv.PROD_CONFIG),
                    "--out-dir", str(world.run_dir)], world.hooks()) == drv.EXIT_REFUSED
    assert world.signals == [] and world.popen_calls == []


def test_execute_refuses_busy_candidate_port_before_signal(world):
    with_sample_pin(world)
    world.listeners_map[drv.CAND_PORT] = [{"addr": "", "pid": 999, "line": "busy"}]
    assert run(world, "num-red-graphon") == drv.EXIT_REFUSED
    assert world.signals == []


# ------------------------------------------------------------------ §1.2 start-boundary recheck

def test_role_hash_change_after_stop_refuses_start_and_restores(world, host):
    """§1.2 TOCTOU: mutate the role digest DURING the production stop; the candidate must
    never start; cleanup + canonical restore run; the exit is REFUSED, not a silent FAIL."""
    with_sample_pin(world)
    role = str(host / "build" / "engine-hip" / "strata-red")
    world.script = [{"status": 200}] * 5
    hooks = world.hooks()
    real_sha = hooks.sha256

    def sha_hook(path):
        if str(path) == role and any(pid == PROD_PID for pid, _ in world.signals):
            return "e" * 64                                  # simulated post-stop tampering
        return real_sha(path)
    hooks.sha256 = sha_hook
    rc = ad.main(["--arm", "num-red-graphon", "--execute", "--expected-wrapper-pid", str(PROD_PID),
                  "--expected-wrapper-starttime", str(PROD_START), "--config",
                  str(drv.PROD_CONFIG), "--out-dir", str(world.run_dir)], hooks)
    assert rc == drv.EXIT_REFUSED
    s = world.summary()
    assert s["exit"] == drv.EXIT_REFUSED
    assert s["production_stopped"] is True and s["restoration_required"] is True
    assert s["restore"]["ok"] is True and not s["blocked"]
    assert s["capture_failures"]
    assert any(e["event"] == "start_boundary_refusal" for e in world.events())
    assert not any(str(drv.CAND_PORT) in c["argv"] for c in world.popen_calls)  # never started
    assert not any("followup" in c[c.index("--out") + 1] for c in world.captures)


# ------------------------------------------------------------------ clean run + capture contract
# (§1.3: every class below is classified from records written by the REAL acceptance.py emitter)

def test_clean_arm_runs_all_requests_and_restores(world):
    with_sample_pin(world)
    world.script = [{"status": 200}] * 5
    assert run(world, "num-red-graphoff") == drv.EXIT_DONE
    s = world.summary()
    assert s["restore"]["ok"] and not s["blocked"] and s["capture_failures"] == []
    out = s["arm_outcome"]
    rows = out["requests"]
    assert [r["class"] for r in rows] == ["clean-200"] * 5
    assert all(r["utility_rc"] == 0 for r in rows)
    assert all(all(r["artifacts"].values()) for r in rows)    # all real artifacts present
    assert out["stopped_at"] is None and out["followup"]["ran"] is False
    assert s["claims"]["numeric_pass"] is False and s["claims"]["availability_only"] is True
    assert len([c for c in world.captures if c[1] == str(drv.UTILITY)]) == 5
    labels = [Path(c[c.index("--out") + 1]).name for c in world.captures]
    assert labels == [f"evidence-num-red-graphoff-{k}{i}" for k, n in (("warm", 2), ("sample", 3))
                      for i in range(1, n + 1)]


def test_stop_at_first_numeric_fatal_400_no_retry(world):
    with_sample_pin(world)
    body = json.dumps({"error": {"message": "dflash: non-finite candidate scores"}})
    world.script = [{"status": 200}, {"status": 200},
                    {"status": 400, "body": body}, {"status": 200}, {"status": 200}]
    assert run(world, "num-red-graphon") == drv.EXIT_FAIL       # recorded evidence failure
    out = world.arm_outcome()
    assert out["requests"][-1]["class"] == "numeric-refusal-fatal-400"
    assert out["requests"][-1]["utility_rc"] == acc.EXIT_EVIDENCE_BAD
    assert out["stopped_at"]["class"] == "numeric-refusal-fatal-400"
    assert len(out["requests"]) == 3                            # the rest never ran: no retry
    s = world.summary()
    assert s["restore"]["ok"] and s["capture_failures"]         # the 400 capture is recorded
    assert s["claims"]["root_cause_found"] is False
    rec_dir = world.run_dir / "evidence-num-red-graphon-sample1" / "sample-001"
    assert (rec_dir / "record.json").exists() and (rec_dir / "response.bin").exists()


def test_incomplete_200_missing_log_slice_stops_arm(world):
    """§1.3: a 200 whose engine log vanished mid-request has real problems + a missing slice:
    NOT clean; the arm stops at the first request; raw evidence is kept; rc is propagated."""
    with_sample_pin(world)
    world.script = [{"status": 200, "delete_engine_log": True}] + [{"status": 200}] * 4
    assert run(world, "num-red-graphoff") == drv.EXIT_FAIL
    out = world.arm_outcome()
    row = out["requests"][0]
    assert row["class"] == "incomplete-200" and row["utility_rc"] == acc.EXIT_EVIDENCE_BAD
    slice_p = world.run_dir / "evidence-num-red-graphoff-warm1" / "sample-001" / \
        "log-engine.slice.bin"
    assert not slice_p.exists() or slice_p.stat().st_size == 0   # the slice never arrived
    assert any("log_missing_after_preflight" in p for p in row["problems"])
    assert out["stopped_at"]["label"] == row["label"] and len(out["requests"]) == 1
    assert s_dir_exists(world.run_dir / "evidence-num-red-graphoff-warm1" / "sample-001" /
                        "record.json")
    s = world.summary()
    assert s["restore"]["ok"] and s["capture_failures"]


def test_transport_error_stops_arm_as_error_not_refusal(world):
    with_sample_pin(world)
    world.script = [{"status": 200, "transport_error": "127.0.0.1:8019 refused"}]
    assert run(world, "num-red-graphoff") == drv.EXIT_FAIL
    row = world.arm_outcome()["requests"][0]
    assert row["class"] == "transport-error" and row["status"] is None
    assert any("transport failed" in p for p in row["problems"])
    assert world.arm_outcome()["stopped_at"]["class"] == "transport-error"


def test_missing_record_json_is_capture_missing(world):
    with_sample_pin(world)
    # engine log missing at --log validation: the utility REFUSES (rc 2), no record.json
    world.script = [{"status": 200, "delete_log_before_capture": True}]
    assert run(world, "num-red-graphoff") == drv.EXIT_FAIL
    row = world.arm_outcome()["requests"][0]
    assert row["class"] == "capture-missing" and row["utility_rc"] == acc.EXIT_REFUSED
    assert not (world.run_dir / "evidence-num-red-graphoff-warm1" / "sample-001" /
                "record.json").exists()


def test_non_json_body_is_incomplete_200_not_clean(world):
    with_sample_pin(world)
    world.script = [{"status": 200, "body": "this is not json"}] + [{"status": 200}] * 4
    assert run(world, "num-red-graphoff") == drv.EXIT_FAIL
    row = world.arm_outcome()["requests"][0]
    assert row["class"] == "incomplete-200"
    assert any("not JSON" in p for p in row["problems"])
    assert len(world.arm_outcome()["requests"]) == 1


def test_missing_usage_is_incomplete_200_not_clean(world):
    with_sample_pin(world)
    body = json.dumps({"choices": [{"index": 0,
                                    "message": {"role": "assistant", "content": "ok"},
                                    "finish_reason": "stop"}]})           # no usage object
    world.script = [{"status": 200, "body": body}] + [{"status": 200}] * 4
    assert run(world, "num-red-graphoff") == drv.EXIT_FAIL
    row = world.arm_outcome()["requests"][0]
    assert row["class"] == "incomplete-200"
    assert any("no usage object" in p for p in row["problems"])


def s_dir_exists(p: Path) -> bool:
    assert p.exists(), f"evidence not preserved: {p}"
    return True


# ------------------------------------------------------------------ refusal classes + follow-up

def test_handled_200_refusal_is_availability_and_drives_one_followup(world):
    with_sample_pin(world)
    done = "DONE 256 12 4000.0 2100.0 stop 37 45 1 0 0 0 0 4.1 2 90 chunk=0 experts_streamed=0"
    world.script = [{"status": 200}, {"status": 200}, {"status": 200, "notice": True},
                    {"status": 200, "timings": {"prompt_n": 120, "completion_n": 40,
                                                "draft_n": 45, "draft_n_accepted": 37},
                     "append_engine": done}]
    assert run(world, "num-cand-graphon") == drv.EXIT_DONE
    out = world.arm_outcome()
    assert out["requests"][2]["class"] == "numeric-refusal-handled-200"
    assert out["stopped_at"]["class"] == "numeric-refusal-handled-200"
    fu = out["followup"]
    assert fu["ran"] is True and fu["attributable"] is True and fu["same_identity"] is True
    assert fu["alive_before"] and fu["alive_after"]
    assert fu["handled_200_refusal"] is True
    prim = fu["acceptance_timings"]                               # PRIMARY: response timings
    assert prim["present"] is True and prim["draft_n"] == 45 \
        and prim["draft_n_accepted"] == 37
    sec = fu["acceptance_done_parser_only"]                       # SECONDARY: parser-only
    assert sec["draft_accepted"] == "37" and sec["draft_offered"] == "45"
    assert "parser-only" in sec["note"]
    s = world.summary()
    assert s["claims"]["numeric_pass"] is False                   # 200 = availability ONLY
    fus = [c for c in world.captures if "followup" in c[c.index("--out") + 1]]
    assert len(fus) == 1                                          # exactly ONE follow-up


def test_timings_absent_means_unknown_never_a_drafting_claim(world):
    with_sample_pin(world)
    world.script = [{"status": 200}, {"status": 200}, {"status": 200, "notice": True},
                    {"status": 200}]                              # body WITHOUT timings
    assert run(world, "num-cand-graphon") == drv.EXIT_DONE
    fu = world.arm_outcome()["followup"]
    assert fu["ran"] is True and fu["attributable"] is True
    prim = fu["acceptance_timings"]
    assert prim["present"] is False and prim["draft_n"] == "unknown" \
        and prim["draft_n_accepted"] == "unknown"
    assert world.summary()["claims"]["numeric_pass"] is False


def test_followup_requires_the_recorded_child_a_lazy_restart_is_not_survival(world):
    with_sample_pin(world)
    world.script = [{"status": 200}, {"status": 200}, {"status": 200, "notice": True}]

    def capture_hook(argv, cwd, timeout):
        out = world.capture(argv, cwd, timeout)
        if "sample1" in argv[argv.index("--out") + 1]:
            world.restart_engine()                             # watchdog replaced the child
        return out
    hooks = world.hooks()
    hooks.capture = capture_hook
    assert ad.main(["--arm", "num-cand-graphon", "--execute", "--expected-wrapper-pid",
                    str(PROD_PID), "--expected-wrapper-starttime", str(PROD_START),
                    "--config", str(drv.PROD_CONFIG), "--out-dir", str(world.run_dir)],
                   hooks) == drv.EXIT_DONE
    out = world.arm_outcome()
    assert out["requests"][-1]["class"] == "numeric-refusal-handled-200"
    assert out["followup"]["ran"] is False
    assert out["followup"]["attributable"] is False
    assert "survival" in out["followup"]["skipped_reason"]
    assert not [c for c in world.captures if "followup" in c[c.index("--out") + 1]]


def test_followup_mid_request_replacement_is_not_attributable(world):
    """§1.6: identity is checked BEFORE and AFTER; a replacement DURING the request makes the
    response NOT attributable; the accepted cleanup rescan records the new child."""
    with_sample_pin(world)
    world.script = [{"status": 200}, {"status": 200}, {"status": 200, "notice": True},
                    {"status": 200}]

    def capture_hook(argv, cwd, timeout):
        if "followup" in argv[argv.index("--out") + 1]:
            world.restart_engine()                             # replaced DURING the request
            return world.capture(argv, cwd, timeout)
        return world.capture(argv, cwd, timeout)
    hooks = world.hooks()
    hooks.capture = capture_hook
    assert ad.main(["--arm", "num-cand-graphon", "--execute", "--expected-wrapper-pid",
                    str(PROD_PID), "--expected-wrapper-starttime", str(PROD_START),
                    "--config", str(drv.PROD_CONFIG), "--out-dir", str(world.run_dir)],
                   hooks) == drv.EXIT_DONE
    out = world.arm_outcome()
    fu = out["followup"]
    assert fu["ran"] is True                                   # the request WAS made
    assert fu["alive_before"] and not fu["alive_after"]        # the recorded child died
    assert fu["same_identity"] is False and fu["attributable"] is False
    assert "NOT attributable" in fu["attributability"]
    new_pids = {e["child"]["pid"] for e in world.events() if e["event"] == "chain_child_new"}
    assert new_pids                                            # cleanup rescan OWNS the new child
    stopped = [e for e in world.events() if e["event"] == "chain_stopped"][-1]
    assert stopped["engine_gone"] is True                      # the new child was confirmed gone


def test_followup_skipped_without_a_handled_refusal(world):
    with_sample_pin(world)
    world.script = [{"status": 200}] * 5
    run(world, "num-cand-graphon")
    out = world.arm_outcome()
    assert out["followup"]["ran"] is False and out["followup"]["handled_200_refusal"] is False
    assert not [c for c in world.captures if "followup" in c[c.index("--out") + 1]]


def test_done_line_parser_is_secondary_and_says_unknown_without_lines():
    done = "DONE 256 12 4000.0 2100.0 stop 37 45 1 0 0 0 0 4.1 2 90 chunk=0 experts_streamed=0"
    row = ad.parse_done_acceptance(done.encode())
    assert row["draft_accepted"] == "37" and row["draft_offered"] == "45"  # fields 7 and 8 verbatim
    assert "parser-only" in row["note"]
    empty = ad.parse_done_acceptance(b"engine: loaded\n")
    assert empty["draft_accepted"] == "unknown" and empty["draft_offered"] == "unknown"


def test_utility_rc_and_all_artifacts_are_part_of_clean_200(tmp_path):
    """§1.3 adapter-side contract on a complete real-shape record dir: clean-200 needs rc 0 AND
    record.json + response.bin + log-engine.slice.bin; rc!=0 is capture-failed, a missing
    artifact is incomplete-200 - never clean."""
    d = types.SimpleNamespace(run_dir=tmp_path, m=None)
    rec = tmp_path / "evidence-x-sample-1" / "sample-001"
    rec.mkdir(parents=True)
    (rec / "record.json").write_text(json.dumps({"http": {"status": 200, "reason": None},
                                                 "problems": []}), encoding="utf-8")
    (rec / "response.bin").write_bytes(b'{"choices": []}')
    (rec / "log-engine.slice.bin").write_bytes(b"")
    assert ad.read_sample_record(d, "x-sample-1", 0)["class"] == "clean-200"
    assert ad.read_sample_record(d, "x-sample-1", 3)["class"] == "capture-failed"
    (rec / "response.bin").unlink()
    assert ad.read_sample_record(d, "x-sample-1", 0)["class"] == "incomplete-200"


# ------------------------------------------------------------------ output-SHA auto-record
# sha256(response.bin) is computed AT CAPTURE time and recorded in the per-sample manifest row AND
# as one line in <out-dir>/output-shas.tsv.  Rows exist for clean-200 captures ONLY (never a
# placeholder); stop-at-first-failure is unchanged.  Rows are driven by the REAL accepted emitter.

def test_clean_arm_rows_equal_sha256_of_the_captured_response_bins(world):
    with_sample_pin(world)
    world.script = [{"status": 200}] * 5
    assert run(world, "num-red-graphoff") == drv.EXIT_DONE
    rows, mrows = sha_rows(world), world.arm_outcome()["requests"]
    assert len(rows) == len(mrows) == 5
    for n, (row, mrow) in enumerate(zip(rows, mrows), start=1):
        rec_dir = world.run_dir / f"evidence-{row['label']}" / "sample-001"
        raw = (rec_dir / "response.bin").read_bytes()
        assert row["response_sha256"] == hashlib.sha256(raw).hexdigest() == mrow["output_sha256"]
        assert row["content_sha256"] == hashlib.sha256(b"ok text").hexdigest()
        assert row["content_sha256"] == mrow["output_content_sha256"]
        assert row["arm"] == "num-red-graphoff" and row["role"] == "red"
        assert row["label"] == mrow["label"] and row["position"] == str(n)
        assert row["request_sha256"] == ad.SAMPLE_SHA256        # the pinned identical request
    assert [(r["kind"], r["index"]) for r in rows] == [
        ("warmup", "1"), ("warmup", "2"), ("sample", "1"), ("sample", "2"), ("sample", "3")]
    ev = [e for e in world.events() if e["event"] == "arm_request"]
    assert [e["position"] for e in ev] == [1, 2, 3, 4, 5]
    assert [e["kind"] for e in ev] == ["warmup", "warmup", "sample", "sample", "sample"]
    assert all("output_sha256" in e and "output_content_sha256" in e for e in ev)
    save_mock_evidence(world, "clean-arm-num-red-graphoff")


def test_output_sha_header_is_written_exactly_once(world):
    with_sample_pin(world)
    world.script = [{"status": 200}] * 5
    run(world, "num-red-graphoff")
    lines = sha_lines(world)
    header = sha_header(world)
    assert header == list(ad.OUTPUT_SHA_HEADER)                 # once, verbatim
    assert lines[:len(header)] == header                        # only at the top
    assert "same-arm repeat stability" in "\n".join(header)
    assert "NOT required equal" in "\n".join(header)            # OFF-vs-ON = evidence, not a claim
    assert "identity key" in "\n".join(header)
    assert "never a placeholder" in "\n".join(header)
    assert all(len(ln.split("\t")) == len(ad.OUTPUT_SHA_COLUMNS)
               for ln in lines if not ln.startswith("#"))


def test_missing_record_writes_no_output_sha_row(world):
    """capture-missing (no record.json at all): NO row, NO sha field in the manifest row."""
    with_sample_pin(world)
    world.script = [{"status": 200, "delete_log_before_capture": True}]
    assert run(world, "num-red-graphoff") == drv.EXIT_FAIL
    row = world.arm_outcome()["requests"][0]
    assert row["class"] == "capture-missing"
    assert "output_sha256" not in row and "output_content_sha256" not in row
    assert sha_rows(world) == []
    assert sha_header(world) == list(ad.OUTPUT_SHA_HEADER)       # header only, no placeholder
    assert not (world.run_dir / "evidence-num-red-graphoff-warm1" / "sample-001" /
                "record.json").exists()


def test_incomplete_200_writes_no_output_sha_row_even_with_response_bytes(world):
    """§1.3 + placeholder rule: the raw response.bin IS kept as evidence, but a non-clean record
    never produces a sha row and never a sha field in its manifest row."""
    with_sample_pin(world)
    world.script = [{"status": 200, "delete_engine_log": True}] + [{"status": 200}] * 4
    assert run(world, "num-red-graphoff") == drv.EXIT_FAIL
    out = world.arm_outcome()
    row = out["requests"][0]
    assert row["class"] == "incomplete-200" and "output_sha256" not in row
    assert (world.run_dir / "evidence-num-red-graphoff-warm1" / "sample-001" /
            "response.bin").exists()                             # raw bytes kept
    assert sha_rows(world) == []
    assert out["stopped_at"]["label"] == row["label"] and len(out["requests"]) == 1
    save_mock_evidence(world, "incomplete-200-num-red-graphoff")


def test_fatal_400_writes_no_row_and_stops_the_arm(world):
    """The refused request's own bytes are kept raw, produce NO sha row; the rows of the earlier
    clean requests stand; the arm stops there (no retry, no later rows)."""
    with_sample_pin(world)
    body = json.dumps({"error": {"message": "dflash: non-finite candidate scores"}})
    world.script = [{"status": 200}, {"status": 200}, {"status": 400, "body": body},
                    {"status": 200}, {"status": 200}]
    assert run(world, "num-red-graphon") == drv.EXIT_FAIL
    out = world.arm_outcome()
    assert out["requests"][-1]["class"] == "numeric-refusal-fatal-400"
    assert "output_sha256" not in out["requests"][-1]
    rows = sha_rows(world)
    assert [r["label"] for r in rows] == ["num-red-graphon-warm1", "num-red-graphon-warm2"]
    assert [r["position"] for r in rows] == ["1", "2"]
    assert (world.run_dir / "evidence-num-red-graphon-sample1" / "sample-001" /
            "response.bin").exists()                             # raw refusal bytes kept


def test_handled_200_refusal_writes_no_row_and_the_followup_row_is_distinguished(world):
    """handled-200 is non-clean: NO row.  The follow-up IS a clean capture: its row carries
    kind=followup and keeps its position in the sequence (the refused request still occupies one)."""
    with_sample_pin(world)
    world.script = [{"status": 200}, {"status": 200}, {"status": 200, "notice": True},
                    {"status": 200, "timings": {"prompt_n": 120, "completion_n": 40,
                                                "draft_n": 45, "draft_n_accepted": 37}}]
    assert run(world, "num-cand-graphon") == drv.EXIT_DONE
    out = world.arm_outcome()
    assert out["requests"][2]["class"] == "numeric-refusal-handled-200"
    assert "output_sha256" not in out["requests"][2]
    rows = sha_rows(world)
    assert [(r["label"], r["kind"], r["index"], r["position"]) for r in rows] == [
        ("num-cand-graphon-warm1", "warmup", "1", "1"),
        ("num-cand-graphon-warm2", "warmup", "2", "2"),
        ("num-cand-graphon-followup", "followup", "1", "4")]
    assert all(r["role"] == "cand" for r in rows)
    fu = out["followup"]
    assert fu["ran"] is True and fu["kind"] == "followup" and fu["position"] == 4
    rec_dir = world.run_dir / "evidence-num-cand-graphon-followup" / "sample-001"
    assert fu["output_sha256"] == hashlib.sha256((rec_dir / "response.bin").read_bytes()).hexdigest()
    assert rows[-1]["response_sha256"] == fu["output_sha256"]
    save_mock_evidence(world, "handled-200-plus-followup-num-cand-graphon")


def test_content_sha_field_is_empty_for_a_tool_calls_only_clean_completion(world):
    """A clean capture whose body carries tool_calls and NO text: the raw row stands, the content
    column is EMPTY (documented) - never a fake hash."""
    with_sample_pin(world)
    body = json.dumps({"id": "c1", "object": "chat.completion",
                       "choices": [{"index": 0, "message": {"role": "assistant", "content": None,
                                                            "tool_calls": [{"id": "1",
                                                                            "type": "function",
                                                                            "function": {"name": "x",
                                                                                         "arguments": "{}"}}]},
                                    "finish_reason": "tool_calls"}],
                       "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}})
    world.script = [{"status": 200, "body": body}] * 5
    assert run(world, "num-red-graphoff") == drv.EXIT_DONE
    rows, mrows = sha_rows(world), world.arm_outcome()["requests"]
    assert len(rows) == 5
    assert all(r["content_sha256"] == "" for r in rows)
    assert all("output_content_sha256" not in m for m in mrows)
    for row, mrow in zip(rows, mrows):
        raw = (world.run_dir / f"evidence-{row['label']}" / "sample-001" /
               "response.bin").read_bytes()
        assert row["response_sha256"] == hashlib.sha256(raw).hexdigest() == mrow["output_sha256"]


def test_content_sha256_of_hashes_the_generated_content_only():
    """The table tool's discipline (tools/mixed_gpu/output_sha_table.py): content, never the raw
    JSON.  No readable text -> None (never a placeholder)."""
    msg = json.dumps({"choices": [{"index": 0, "message": {"role": "assistant",
                                                           "content": "hello"}}]}).encode()
    assert ad.content_sha256_of(msg) == hashlib.sha256(b"hello").hexdigest()
    text = json.dumps({"choices": [{"index": 0, "text": "streamed text"}]}).encode()
    assert ad.content_sha256_of(text) == hashlib.sha256(b"streamed text").hexdigest()
    assert ad.content_sha256_of(b"not json") is None
    assert ad.content_sha256_of(json.dumps({"choices": []}).encode()) is None
    assert ad.content_sha256_of(json.dumps({"choices": [{"message": {"role": "assistant",
                                                                      "content": ""}}]}).encode()) is None
    assert ad.content_sha256_of(json.dumps({"choices": [{"message": {}}]}).encode()) is None


def test_plan_documents_the_output_sha_contract_and_writes_no_rows(world):
    with_sample_pin(world)
    assert plan(world, "num-red-graphon") == drv.EXIT_DONE
    v = json.loads((world.run_dir / "plan.json").read_text())
    ev = v["output_sha_evidence"]
    assert ev["file"] == str(world.run_dir / ad.OUTPUT_SHA_FILE)
    assert ev["columns"] == list(ad.OUTPUT_SHA_COLUMNS)
    assert "clean-200" in ev["rows_only_for"] and "never a placeholder" in ev["rows_only_for"]
    assert ev["identity_key"] == "(fresh engine, identical request sequence, position)"
    assert "not required equal" in ev["claim_scope"]
    assert not (world.run_dir / ad.OUTPUT_SHA_FILE).exists()      # plan: no captures, no rows
    assert world.captures == [] and world.popen_calls == []


def test_interrupt_keeps_rows_for_the_completed_requests_only(world):
    with_sample_pin(world)
    world.script = [{"status": 200}, {"status": 200, "raise": "interrupt"}]
    assert run(world, "num-cand-graphoff") == drv.EXIT_FAIL
    rows = sha_rows(world)
    assert len(rows) == 1 and rows[0]["kind"] == "warmup" and rows[0]["position"] == "1"
    assert "run_interrupted" in world.kinds()
    ev = [e for e in world.events() if e["event"] == "arm_request"]     # the manifest keeps the
    assert len(ev) == 1 and "output_sha256" in ev[0]                    # one completed capture


def test_output_sha_event_documents_the_semantics_at_the_arm_start(world):
    with_sample_pin(world)
    world.script = [{"status": 200}] * 5
    run(world, "num-red-graphoff")
    ev = [e for e in world.events() if e["event"] == "output_sha_file"]
    assert len(ev) == 1
    assert ev[0]["rows_only_for"] == "clean-200"
    assert ev[0]["identity_key"] == "(fresh engine, identical request sequence, position)"
    assert ev[0]["placeholder_policy"] == "never"
    assert ev[0]["columns"] == list(ad.OUTPUT_SHA_COLUMNS)
    assert ev[0]["header_lines"] == len(ad.OUTPUT_SHA_HEADER)


# ------------------------------------------------------------------ interrupt / cleanup

def test_interrupt_cleans_up_and_restores(world):
    with_sample_pin(world)
    world.script = [{"status": 200}, {"status": 200, "raise": "interrupt"}]
    assert run(world, "num-cand-graphoff") == drv.EXIT_FAIL
    s = world.summary()
    kinds = world.kinds()
    assert "run_interrupted" in kinds and "restore_verified" in kinds
    assert s["restore"]["ok"] is True
    assert s["restoration_required"] is True
    assert [p for p, _ in world.signals if p == PROD_PID] == [PROD_PID]     # exactly one TERM


def test_restore_blocked_survivor_gives_failure_not_silent_success(world):
    with_sample_pin(world)
    world.script = [{"status": 200}] * 5

    def popen_hook(argv, cwd, env, out, err):
        p = world.popen(argv, cwd, env, out, err)
        if str(drv.CAND_PORT) in [str(a) for a in argv]:
            p.ignore_signals = True            # the candidate wrapper survives its TERM: survivor
        return p
    hooks = world.hooks()
    hooks.popen = popen_hook

    def signal_hook(fd, sig):
        pid = world.fds[fd]
        world.signals.append((pid, int(sig)))
        p = world.procs[pid]
        if "--port" in [str(a) for a in p.argv] and str(drv.CAND_PORT) in [str(a) for a in p.argv]:
            return                                              # the candidate wrapper survives
        p.alive, p.rc = False, -int(sig)
        for kid in p.children:
            k = world.procs.get(kid)
            if k is not None and k.alive:
                k.alive, k.rc = False, -int(sig)
        if pid == PROD_PID:
            world.listeners_map.pop(drv.PROD_PORT, None)
    hooks.pidfd_signal = signal_hook
    rc = ad.main(["--arm", "decode-baseline", "--execute", "--expected-wrapper-pid", str(PROD_PID),
                  "--expected-wrapper-starttime", str(PROD_START), "--config",
                  str(drv.PROD_CONFIG), "--out-dir", str(world.run_dir)], hooks)
    assert rc == drv.EXIT_FAIL
    s = world.summary()
    assert s["restore"]["ok"] is False and "RESTORE" in s["restore"]["status"].upper()


# ------------------------------------------------------------------ frozen imports untouched

def test_adapter_does_not_mutate_the_accepted_lifecycle_api():
    """The adapter binds only CAND_ENGINE: the driver's lifecycle symbols keep their identities."""
    import inspect
    for fn in ("start_chain", "stop_chain", "utility", "cleanup_active", "stop_production",
               "restore_production", "log_args"):
        src = inspect.getsource(getattr(drv.Driver, fn))
        assert "numeric" not in src.lower()
    assert drv.TOOL_VERSION == "r3-driver-2"
    text = Path(drv.__file__).read_text(encoding="utf-8")
    assert "def run_arm" not in text and "PARITY_BYTE_CAP" not in text
    assert "def run_parity_analysis" not in text
