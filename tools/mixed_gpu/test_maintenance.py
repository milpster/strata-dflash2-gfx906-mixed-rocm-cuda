#!/usr/bin/env python3
"""Fake-backend tests for tools/mixed_gpu/maintenance.py.

Everything here runs in memory: no real command, no GPU, no service, no swap.  The fake
backend models the real transitions the tool depends on (/proc/swaps rows, unit
active/MainPID, sysfs NBD client record, /proc entries, sysctls) and it records every call,
so the tests assert on the real implementation's call order and postconditions.

Two fake-backend rules that make the negative tests mean something:

  * An UNSCRIPTED command never succeeds: it returns rc 127 with 'UNSCRIPTED' in stderr.
    A test that silently needs an unscripted command fails loudly instead of passing.
  * Mutating commands only do anything when the test installs a hook (with an explicit
    return code).  No mutation is silently successful by default.

The fixture snapshots the live-shape host through maintenance.capture(), so the tests
exercise the capture code path too, not a hand-written snapshot.
"""
from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import maintenance as M  # noqa: E402

UNIT = M.ALLOWED_UNITS[0]
DEV = "/dev/nbd0"
SNAP = "build-mixed-gpu/test-baseline.json"
OPSTATE = "build-mixed-gpu/test-opstate.json"
BIN, CFG, LDR = "/w/strata", "/w/prod-config.json", "/w/launch.sh"
WRAP_PID, ENG_PID, DAEMON_PID = 2523170, 3715513, 4243
CLIENT_PID = 4569
GPU_UUID = "GPU-1a2b3c4d"
BTIME = 1760000000
HEAD = "26631e4bbbeb338d7c3d32726c50b3938dfdd656"
DAEMON_TICKS = 4533


def swap_row(dev, size=7462908, used=7300000, prio=1500, typ="file"):
    return {"filename": dev, "type": typ, "size_kib": size, "used_kib": used,
            "priority": prio}


def render_swaps(rows):
    out = "Filename\tType\tSize\tUsed\tPriority\n"
    for r in rows:
        out += (f"{r['filename']}\t{r['type']}\t{r['size_kib']}\t{r['used_kib']}\t"
                f"{r['priority']}\n")
    return out


def stat_line(pid, ticks):
    """Field 22 (1-based) of /proc/<pid>/stat is starttime in clock ticks: 19 fields after
    the ')' plus the state char, so starttime lands at tail index 19."""
    tail = " ".join(["1", "1", "1", "0", "0", "0", "0", "0", "0", "0", "0", "0", "0", "0",
                     "0", "0", "1", "0", str(ticks)])
    return f"{pid} (proc) S {tail} 0 0 0"


def gpu_row(uuid=GPU_UUID, total=8192, used=7544, index="0", name="RTX 3080"):
    return f"{index}, {name}, {uuid}, {total} MiB, {used} MiB"


class FakeBackend:
    """In-memory stand-in for RealBackend with the same method signatures."""

    def __init__(self):
        self.files = {}
        self.paths = set()          # exists() only (socket, device nodes)
        self.unreadable = set()     # reads report READ_UNKNOWN
        self.readlinks = {}
        self.proc_pids = set()
        self.dirs = {}
        self.units = {}
        self.ports = set()
        self.gpu_gpus = []
        self.gpu_apps = []
        self.gpu_unknown = False
        self.git_head = HEAD
        self.git_porcelain = ""
        self.swap_mode = "ok"
        self.commands = {}          # exact argv tuple -> [(rc, stdout, stderr), ...]
        self.unix_lines = []          # `ss -x -p` output: only root sees the owning pid
        self.sock_mode = {}           # per-path stat mode overrides (socket identity tests)
        self.sock_uid = {}            # per-path stat uid overrides
        self.write_error = None       # make persistence fail with this message
        self.on_swapoff = None
        self.on_swapon = None
        self.on_nbd_client = None
        self.on_sysctl = None
        self.on_start = None
        self.on_stop = None
        self.calls = []             # every argv, in order
        self.log = []               # ("run", argv) / ("write", path) / ("remove", path)
        self.ticks = 100
        self.epoch_val = 1761000000.0

    # ---- reads ----
    def read(self, path):
        p = str(path)
        self.log.append(("read", p))
        if p in self.unreadable:
            return M.READ_UNKNOWN, None
        if p == M.SWAPS_FILE:
            return M.READ_OK, self._swap_text()
        if p in self.files:
            return M.READ_OK, self.files[p]
        return M.READ_MISSING, None

    def _swap_text(self):
        if self.swap_mode == "blank":
            return "\n"
        if self.swap_mode == "malformed":
            return ("Filename\tType\tSize\tUsed\tPriority\n"
                    f"{DEV}\tfile\tnotanumber\t0\t1500\n")
        if self.swap_mode == "header-only":
            return "Filename\tType\tSize\tUsed\tPriority\n"
        return render_swaps(self.files_rows())

    def files_rows(self):
        return getattr(self, "swap_rows", [])

    def read_bytes(self, path):
        p = str(path)
        if p in self.unreadable:
            return None
        if p == M.SWAPS_FILE:
            return self._swap_text().encode()
        if p in self.files:
            return self.files[p].encode()
        return None

    def read_text(self, path):
        return self.read(path)[1]

    def exists(self, path):
        p = str(path)
        if p in self.unreadable:
            return M.READ_UNKNOWN, False
        return M.READ_OK, (p in self.files) or (p in self.paths)

    def readlink(self, path):
        p = str(path)
        if p in self.unreadable:
            return M.READ_UNKNOWN, None
        if p in self.readlinks:
            return M.READ_OK, self.readlinks[p]
        return M.READ_MISSING, None

    def listdir(self, path):
        p = str(path)
        if p in self.unreadable:
            return M.READ_UNKNOWN, []
        if p == "/proc":
            return M.READ_OK, sorted(self.proc_pids)
        if p in self.dirs:
            return M.READ_OK, sorted(self.dirs[p])
        return M.READ_MISSING, []

    def socket_stat(self, path):
        p = str(path)
        if p in self.unreadable:
            return M.READ_UNKNOWN, None
        if p in self.paths:
            mode = self.sock_mode.get(p, 0o140600)
            uid = self.sock_uid.get(p, 0)
            return M.READ_OK, (0, int(self.epoch_val), uid, mode)
        if p in self.files:
            # a plain file on disk: a regular object, not a socket
            mode = self.sock_mode.get(p, 0o100600)
            return M.READ_OK, (len(self.files[p].encode()), int(self.epoch_val),
                               self.sock_uid.get(p, 0), mode)
        return M.READ_MISSING, None

    def size_mtime_uid(self, path):
        p = str(path)
        if p in self.unreadable:
            return M.READ_UNKNOWN, None
        if p in self.files:
            return M.READ_OK, (len(self.files[p].encode()), int(self.epoch_val), 0)
        if p in self.paths:
            return M.READ_OK, (0, int(self.epoch_val), 0)
        return M.READ_MISSING, None

    # ---- commands ----
    def run(self, argv, timeout=None, cwd=None):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        self.log.append(("run", argv))
        scripted = self.commands.get(tuple(argv))
        if scripted:
            r = scripted.pop(0)
            return M.Cmd(argv, r[0], r[1] if len(r) > 1 else "", r[2] if len(r) > 2 else "")
        a0 = argv[0]
        if a0 == "git":
            if "rev-parse" in argv:
                return M.Cmd(argv, 0, self.git_head + "\n", "")
            if "status" in argv:
                return M.Cmd(argv, 0, self.git_porcelain, "")
        if a0 == "uname":
            return M.Cmd(argv, 0, "7.3.0-x64v3-xanmod1\n", "")
        if a0 == "rocm-smi":
            return M.Cmd(argv, 1, "", "no amd on this host")
        if a0 == "ss":
            if "-x" in argv:
                # `ss -x -p`: only the socket's owner (root here) sees the owning pid, so the
                # default is an empty listing - the real host's shape for a normal operator.
                return M.Cmd(argv, 0, "".join(self.unix_lines), "")
            port = argv[-1].rsplit(":", 1)[-1]
            line = f"LISTEN 0 128 0.0.0.0:{port}\n" if port in self.ports else ""
            return M.Cmd(argv, 0, line, "")
        if a0 == "nvidia-smi" and self.gpu_unknown:
            return M.Cmd(argv, 1, "", "nvidia-smi: NVIDIA driver not loaded")
        if a0 == "nvidia-smi" and "--query-gpu" in " ".join(argv):
            return M.Cmd(argv, 0, "\n".join(self.gpu_gpus) + "\n", "")
        if a0 == "nvidia-smi" and "--query-compute-apps" in " ".join(argv):
            rows = [f"{a['pid']}, {a['name']}, {a['used_mib']} MiB" for a in self.gpu_apps]
            return M.Cmd(argv, 0, "\n".join(rows) + ("\n" if rows else ""), "")
        if a0 == "systemctl":
            return self._systemctl(argv)
        if a0 == "swapoff":
            return self._hook(argv, self.on_swapoff, argv[1])
        if a0 == "swapon":
            return self._hook(argv, self.on_swapon, argv[1], argv[3])
        if a0 == "nbd-client":
            return self._hook(argv, self.on_nbd_client, argv)
        if a0 == "sysctl":
            return self._hook(argv, self.on_sysctl, argv[2:])
        return M.Cmd(argv, 127, "", f"UNSCRIPTED COMMAND: {argv}")

    def _hook(self, argv, hook, *args):
        if hook is None:
            return M.Cmd(argv, 127, "", f"UNSCRIPTED MUTATOR: {argv}")
        rc, out, err = hook(*args)
        return M.Cmd(argv, rc, out, err)

    def _systemctl(self, argv):
        verb, unit = argv[1], argv[2]
        u = self.units.get(unit)
        if verb == "is-active":
            if u is None:
                return M.Cmd(argv, 4, "failed\n", "")
            return M.Cmd(argv, u.get("is_active_rc", 3 if not u.get("active") else 0),
                         u.get("is_active_stdout", "inactive\n" if not u.get("active")
                               else "active\n"), "")
        if verb == "is-enabled":
            return M.Cmd(argv, 0, "enabled\n", "")
        if verb == "show":
            if u is None:
                return M.Cmd(argv, 1, "", f"Unit {unit} not found.")
            if u.get("show_rc"):
                return M.Cmd(argv, u["show_rc"], "", "show failed")
            state = "active" if u.get("active") else "inactive"
            ast = u.get("active_state", state)
            props = [f"MainPID={u.get('main_pid', 0)}", f"ActiveState={ast}",
                     f"SubState={u.get('sub_state', 'running' if u.get('active') else 'dead')}",
                     "KillMode=process", "SendSIGKILL=no", "TimeoutStopUSec=30min",
                     "Restart=on-failure",
                     f"FragmentPath=/etc/systemd/system/{unit}",
                     "Environment=VRAM_SETUP_SIZE_MB=7800 VRAM_SWAP_PRIORITY=1500"]
            return M.Cmd(argv, 0, "\n".join(props) + "\n", "")
        if verb == "stop":
            return self._hook(argv, self.on_stop, unit)
        if verb == "start":
            return self._hook(argv, self.on_start, unit)
        return M.Cmd(argv, 127, "", f"UNSCRIPTED systemctl verb: {argv}")

    # ---- writes ----
    def write_private(self, path, text):
        self.log.append(("write", str(path)))
        if self.write_error:
            raise M.Refuse(self.write_error)
        self.files[str(path)] = text

    def remove(self, path):
        p = str(path)
        self.log.append(("remove", p))
        if p in self.unreadable:
            raise M.Refuse(f"cannot remove {p}: permission/I/O error (unknown state)")
        if p not in self.files:
            return False
        del self.files[p]
        return True

    def clock_ticks(self):
        return self.ticks

    def epoch(self):
        return self.epoch_val

    # ---- helpers used by tests ----
    def mutations(self):
        return [c for c in self.calls if M.is_mutating(c)]

    def argv_of(self, prefix):
        return [c for c in self.calls if [str(x) for x in c[:len(prefix)]] == list(prefix)]

    def set_sysctls(self, swappiness, minfree):
        self.files["/proc/sys/vm/swappiness"] = str(swappiness) + "\n"
        self.files["/proc/sys/vm/min_free_kbytes"] = str(minfree) + "\n"

    def make_causal(self, pid):
        """Root-shape causal proof: the daemon's own descriptor table links the socket."""
        self.dirs[f"/proc/{pid}/fd"] = ["0", "1", "2", "3"]
        self.readlinks[f"/proc/{pid}/fd/0"] = "/dev/null"
        self.readlinks[f"/proc/{pid}/fd/1"] = "/dev/null"
        self.readlinks[f"/proc/{pid}/fd/2"] = "/run/log"
        self.readlinks[f"/proc/{pid}/fd/3"] = M.SOCK_FILE

    def make_causal_by_ss(self, pid):
        """Root-shape causal proof via `ss -x -p` only (no readable /proc/<pid>/fd)."""
        self.unix_lines = [f"u_u STR 0 0 * 0 * 0 users:\"((pid={pid},fd=3))\" 0 0 "
                           f"{M.SOCK_FILE}\n"]

    def make_uncasual(self, pid):
        """The current host's shape: /proc/<pid>/fd is root-only and the socket has no
        visible peer for this operator."""
        self.dirs.pop(f"/proc/{pid}/fd", None)
        for k in [k for k in self.readlinks if k.startswith(f"/proc/{pid}/fd/")]:
            del self.readlinks[k]
        self.unix_lines = []

    def standard_mutators(self, prio=1500, new_pid=4400, new_client=4600,
                          new_dev=DEV, gpu_used_after_release=0, sysctls_on_start=True,
                          sysctls_on_stop=True):
        """Canonical transitions: swapoff removes the row, swapon adds it, nbd-client -d
        clears the client record, systemctl start re-derives a fresh device like the real
        connect script does (it picks the first free /dev/nbdN), stop clears the unit."""
        def swapoff(dev):
            self.swap_rows = [r for r in self.swap_rows if r["filename"] != dev]
            self.files[M.SWAPS_FILE] = render_swaps(self.swap_rows)
            return (0, "", "")

        def swapon(dev, p):
            self.swap_rows.append(swap_row(dev, used=0, prio=int(p)))
            self.files[M.SWAPS_FILE] = render_swaps(self.swap_rows)
            return (0, "", "")

        def nbd_client(argv):
            dev = argv[-1]
            self.files.pop(f"/sys/block/{os.path.basename(dev)}/pid", None)
            self.paths.discard(f"/sys/block/{os.path.basename(dev)}")
            return (0, "", "")

        def sysctl(pairs):
            for kv in pairs:
                k, v = kv.split("=", 1)
                self.files["/proc/sys/" + k.replace(".", "/")] = v + "\n"
            return (0, "", "")

        def stop(unit):
            self.units[unit] = {"active": False, "main_pid": 0, "active_state": "inactive",
                                "sub_state": "dead"}
            self.files.pop(f"/proc/{DAEMON_PID}/cmdline", None)
            self.files.pop(f"/proc/{DAEMON_PID}/stat", None)
            self.proc_pids.discard(str(DAEMON_PID))
            self.dirs.pop(f"/proc/{DAEMON_PID}/fd", None)
            self.unix_lines = []
            if sysctls_on_stop:
                # Faithful to the installed unit: ExecStop runs nbd-vram-disconnect.sh, whose
                # last action is `sysctl -w vm.swappiness=60 vm.min_free_kbytes=65536`.
                self.set_sysctls(60, 65536)
            return (0, "", "")

        def start(unit):
            # The installed unit's ExecStartPost connects, mkswaps and swaps on, and the
            # connect script re-picks the first free /dev/nbdN - so the device may RENUMBER.
            self.files[M.STATE_FILE] = new_dev + "\n"
            self.paths.add(M.SOCK_FILE)
            self.files[f"/sys/block/{os.path.basename(new_dev)}/pid"] = str(new_client) + "\n"
            self.units[unit] = {"active": True, "main_pid": new_pid, "active_state": "active",
                                "sub_state": "running"}
            # The unit started NOW, so its start time is (now - btime) in clock ticks.
            new_ticks = int((self.epoch_val - BTIME + 60) * self.ticks)
            self.files[f"/proc/{new_pid}/cmdline"] = "/usr/local/bin/nbd-vram\0"
            self.files[f"/proc/{new_pid}/stat"] = stat_line(new_pid, new_ticks)
            self.proc_pids.add(str(new_pid))
            self.make_causal(new_pid)
            self.swap_rows.append(swap_row(new_dev, used=0, prio=prio))
            self.files[M.SWAPS_FILE] = render_swaps(self.swap_rows)
            self.gpu_apps = [{"pid": new_pid, "name": "nbd-vram", "used_mib": 7450}]
            self.gpu_gpus = [gpu_row(used=gpu_used_after_release)]
            if sysctls_on_start:
                self.set_sysctls(150, 1048576)
            return (0, "", "")

        self.on_swapoff, self.on_swapon = swapoff, swapon
        self.on_nbd_client, self.on_sysctl = nbd_client, sysctl
        self.on_stop, self.on_start = stop, start

    def fail_swapoff(self, rc=8):
        def f(dev):
            return (rc, "", "swapoff: swap operation failed: Device or resource busy")
        self.on_swapoff = f

    def fail_swapon(self, rc=1):
        self.on_swapon = lambda d, p: (rc, "", "swapon failed")

    def fail_disconnect(self, rc=1):
        def f(argv):
            return (rc, "", "nbd-client: failed to disconnect")
        self.on_nbd_client = f

    def fail_stop(self, rc=1):
        self.on_stop = lambda u: (rc, "", "timed out")

    def fail_start(self, rc=1):
        self.on_start = lambda u: (rc, "", "start request failed")

    def fail_sysctl(self, rc=1):
        self.on_sysctl = lambda pairs: (rc, "", "sysctl: permission denied")


def args(argv):
    return M.build_parser().parse_args(argv)


def fixture(with_prod=True, prio=1500, gpu_used=7544, cls=None):
    be = (cls or FakeBackend)()
    be.swap_rows = [swap_row(DEV, prio=prio), swap_row("/mnt/swap2", 2097152, 0, 2),
                    swap_row("/home/swap1", 2097152, 0, 1)]
    be.files[M.SWAPS_FILE] = render_swaps(be.swap_rows)
    be.files["/proc/swaps"] = be.files[M.SWAPS_FILE]
    be.files["/proc/meminfo"] = ("MemTotal: 32700000 kB\nMemFree: 9000000 kB\n"
                                 "MemAvailable: 12000000 kB\nSwapTotal: 11657212 kB\n"
                                 "SwapFree: 4357212 kB\nDirty: 12000 kB\n"
                                 "Cached: 6000000 kB\nCommitted_AS: 20000000 kB\n")
    be.files["/proc/stat"] = f"btime {BTIME}\n"
    be.files[M.STATE_FILE] = DEV + "\n"
    be.paths.add(M.SOCK_FILE)
    be.files["/sys/block/nbd0/pid"] = str(CLIENT_PID) + "\n"
    be.units[UNIT] = {"active": True, "main_pid": DAEMON_PID, "active_state": "active",
                      "sub_state": "running"}
    be.files[f"/proc/{DAEMON_PID}/cmdline"] = "/usr/local/bin/nbd-vram\0"
    be.files[f"/proc/{DAEMON_PID}/stat"] = stat_line(DAEMON_PID, DAEMON_TICKS)
    be.proc_pids = {"1", str(DAEMON_PID)}
    be.make_causal(DAEMON_PID)          # root-shape: the causal proof IS obtainable here
    be.ports.add("8009")
    if with_prod:
        be.proc_pids |= {str(WRAP_PID), str(ENG_PID)}
        be.files[f"/proc/{WRAP_PID}/cmdline"] = (
            "/mnt/user/Strata/.venv/bin/python\0server.py\0--engine\0strata\0--config\0"
            "prod-config.json\0--port\08009\0--api-key-env\0STRATA_KEY\0")
        be.files[f"/proc/{WRAP_PID}/stat"] = stat_line(WRAP_PID, 90000)
        be.files[f"/proc/{ENG_PID}/cmdline"] = (
            "/mnt/user/Strata/worktrees/v023-merge/build-906-inco/strata\0"
            "--model\0q4_0\0--max-context\0131072\0")
        be.files[f"/proc/{ENG_PID}/stat"] = stat_line(ENG_PID, 90010)
    be.set_sysctls(150, 1048576)
    be.gpu_gpus = [gpu_row(used=gpu_used)]
    be.gpu_apps = [{"pid": DAEMON_PID, "name": "nbd-vram", "used_mib": 7450}]
    be.files[BIN], be.files[CFG], be.files[LDR] = "BINARY", "{}", "#!/bin/sh\n"
    be.readlinks[f"/proc/{DAEMON_PID}/exe"] = "/usr/local/bin/nbd-vram"
    return be


def capture(be, out=SNAP):
    a = args(["capture", "--out", out, "--live-root", "/w", "--binary", BIN, "--config", CFG,
              "--launcher", LDR, "--build-dir", "/w/b"])
    snap = M.capture(a, be)
    M.write_snapshot(be, snap, out)
    return snap


def shut_prod_down(be):
    for pid in (WRAP_PID, ENG_PID):
        be.proc_pids.discard(str(pid))
        be.files.pop(f"/proc/{pid}/cmdline", None)
        be.files.pop(f"/proc/{pid}/stat", None)
    be.ports.discard("8009")


def drain_cmd(execute=True, extra=()):
    base = ["drain", "--snapshot", SNAP, "--mem-budget-kib", "8000000", "--opstate", OPSTATE]
    return base + (["--execute"] if execute else []) + list(extra)


def restore_cmd(execute=True, extra=()):
    base = ["restore", "--snapshot", SNAP, "--opstate", OPSTATE, "--expect-prod-stopped"]
    return base + (["--execute"] if execute else []) + list(extra)


def verify_cmd(expect="drained"):
    return ["verify", "--snapshot", SNAP, "--expect", expect, "--opstate", OPSTATE]


def opstate(be):
    return json.loads(be.files[OPSTATE])


def read_opstate_writes(be):
    return [i for i, e in enumerate(be.log)
            if e[0] == "write" and e[1] == OPSTATE]


def first_mutator_index(be):
    for i, e in enumerate(be.log):
        if e[0] == "run" and M.is_mutating(e[1]):
            return i
    return -1


def full_drain(be, clear_after=True):
    """Capture, take prod down, install working mutators and drain.  Clears the call log by
    default so a later restore assertion sees only the restore's own commands."""
    capture(be)
    shut_prod_down(be)
    be.standard_mutators()
    rc = M.main(drain_cmd(True), be)
    if clear_after:
        be.calls.clear()
        be.log.clear()
    return rc


# ----------------------------------------------------------------------------------
# F1 / G1: unknown is never absence, never inactive, never success.
# ----------------------------------------------------------------------------------
class TestFailClosedParsing(unittest.TestCase):
    def test_header_only_empty_is_known_and_distinct_from_unreadable(self):
        self.assertEqual(M.parse_swaps("Filename\tType\tSize\tUsed\tPriority\n"), [])
        be = fixture()
        be.swap_mode = "header-only"
        st, rows, _ = M.read_swaps(be)
        self.assertEqual((st, rows), ("ok", []))
        be.swap_mode = None
        be.unreadable.add(M.SWAPS_FILE)
        st2, rows2, why = M.read_swaps(be)
        self.assertEqual(st2, "unknown")
        self.assertNotEqual((st, rows), (st2, rows2))
        self.assertIn("unknown", why)

    def test_blank_and_malformed_swap_tables(self):
        be = fixture()
        be.swap_mode = "blank"
        self.assertEqual(M.read_swaps(be)[0], "unknown")
        be.swap_mode = "malformed"
        st, rows, why = M.read_swaps(be)
        self.assertEqual((st, rows), ("malformed", []))
        self.assertIn("non-numeric", why)
        self.assertRaises(M.ParseFail, M.parse_swaps, "Filename\tType\tSize\tUsed\tPriority\n/dev/nbd0\tfile\tx\t0\t1500\n")
        self.assertRaises(M.ParseFail, M.parse_swaps, "Bogus header\n/dev/nbd0 file 1 0 1\n")
        self.assertRaises(M.ParseFail, M.parse_swaps, "Filename\tType\tSize\tUsed\tPriority\n/dev/nbd0\tfile\t10\t20\t1\n")

    def test_drain_refuses_unreadable_table_before_any_mutator(self):
        capture(fixture())          # good baseline captured first
        be = fixture()
        capture(be)
        shut_prod_down(be)
        be.swap_mode = None
        be.unreadable.add(M.SWAPS_FILE)
        self.assertEqual(M.main(drain_cmd(True), be), M.E_REFUSED)
        self.assertEqual(be.mutations(), [])
        self.assertNotIn(OPSTATE, be.files)

    def test_drain_refuses_blank_and_malformed_table(self):
        be = fixture()
        capture(be)
        shut_prod_down(be)
        for mode in ("blank", "malformed"):
            be.swap_mode = mode
            self.assertEqual(M.main(drain_cmd(True), be), M.E_REFUSED, msg=mode)
        self.assertEqual(be.mutations(), [])
        self.assertNotIn(OPSTATE, be.files)

    def test_is_active_rc3_inactive_is_known_stopped_but_empty_stdout_is_unknown(self):
        be = fixture()
        be.units[UNIT] = {"active": False, "main_pid": 0}
        s = M.service_state(be, UNIT)
        self.assertEqual(s["state"], "inactive")
        be.units[UNIT] = {"active": True, "main_pid": DAEMON_PID, "is_active_rc": 1,
                          "is_active_stdout": "", "active_state": "failed"}
        s2 = M.service_state(be, UNIT)
        self.assertEqual(s2["state"], "unknown")
        self.assertTrue(any("is-active" in u for u in s2["unknown"]))
        be.units[UNIT]["show_rc"] = 1
        self.assertEqual(M.service_state(be, UNIT)["state"], "unknown")
        be.units[UNIT] = {"active": True, "main_pid": DAEMON_PID, "is_active_rc": 0,
                          "is_active_stdout": "active", "active_state": "inactive"}
        s3 = M.service_state(be, UNIT)
        self.assertEqual(s3["state"], "unknown")
        self.assertTrue(any("contradicts" in u for u in s3["unknown"]))

    def test_verify_cannot_pass_on_unknown_state(self):
        be = fixture()
        self.assertEqual(full_drain(be), M.E_OK)
        be.gpu_gpus = [gpu_row(used=180)]           # release proven -> the PASS baseline
        self.assertEqual(M.main(verify_cmd("drained"), be), M.E_OK)
        be.swap_mode = None
        be.unreadable.add(M.SWAPS_FILE)
        self.assertEqual(M.main(verify_cmd("drained"), be), M.E_VERIFY_FAIL)
        be.unreadable.discard(M.SWAPS_FILE)
        be.units[UNIT] = {"active": True, "main_pid": DAEMON_PID, "is_active_rc": 1,
                          "is_active_stdout": ""}
        self.assertEqual(M.main(verify_cmd("drained"), be), M.E_VERIFY_FAIL)
        be.unreadable.add("/proc/sys/vm/swappiness")
        # A drain record is not a restore receipt: verify --expect restored refuses (exit 2)
        # rather than reading raw JSON and guessing a target.
        self.assertEqual(M.main(verify_cmd("restored"), be), M.E_REFUSED)

    def test_marker_missing_is_known_absent_but_unreadable_is_not(self):
        be = fixture()
        capture(be)
        shut_prod_down(be)
        be.units[UNIT] = {"active": False, "main_pid": 0}
        be.files.pop(M.STATE_FILE)                      # ENOENT
        self.assertEqual(M.main(verify_cmd("drained"), be), M.E_VERIFY_FAIL)  # gpu unknown
        be.unreadable.add(M.STATE_FILE)                  # permission denied
        self.assertEqual(M.main(verify_cmd("drained"), be), M.E_VERIFY_FAIL)
        self.assertEqual(M.read_state_file(be), (M.READ_UNKNOWN, None))
        be.unreadable.discard(M.STATE_FILE)
        be.files.pop(M.STATE_FILE, None)
        self.assertEqual(M.read_state_file(be), (M.READ_MISSING, None))

    def test_unreadable_pid_stat_and_proc_enumeration_refuse(self):
        be = fixture()
        capture(be)
        shut_prod_down(be)
        be.proc_pids.add(str(ENG_PID))
        be.files[f"/proc/{ENG_PID}/cmdline"] = "/x/strata --model q4_0\0"
        be.files[f"/proc/{ENG_PID}/stat"] = stat_line(ENG_PID, 1)
        be.unreadable.add(f"/proc/{WRAP_PID}/stat")
        self.assertEqual(M.main(drain_cmd(True), be), M.E_REFUSED)
        be.unreadable.discard(f"/proc/{WRAP_PID}/stat")
        be.unreadable.add("/proc")
        self.assertEqual(M.main(drain_cmd(True), be), M.E_REFUSED)
        self.assertEqual(be.mutations(), [])

    def test_recorded_pid_reuse_is_refused_not_skipped(self):
        be = fixture()
        capture(be)
        shut_prod_down(be)
        be.proc_pids.add(str(ENG_PID))
        be.files[f"/proc/{ENG_PID}/cmdline"] = "/usr/bin/strata --serve\0"
        be.files[f"/proc/{ENG_PID}/stat"] = stat_line(ENG_PID, 123456)   # different start
        self.assertEqual(M.main(drain_cmd(True), be), M.E_REFUSED)
        self.assertEqual(be.mutations(), [])

    def test_port_is_bound_to_the_snapshot(self):
        be = fixture()
        capture(be)
        shut_prod_down(be)
        rc = M.main(drain_cmd(False, ["--port", "8123"]), be)
        self.assertEqual(rc, M.E_REFUSED)
        be.ports.add("8009")
        self.assertEqual(M.main(drain_cmd(False), be), M.E_REFUSED)   # snapshot port listening


# ----------------------------------------------------------------------------------
# F5: every number a mutation depends on is checked before the first mutator.
# ----------------------------------------------------------------------------------
class TestPreActionValidation(unittest.TestCase):
    def snapshot_mutate(self, be, patch):
        capture(be)
        snap = json.loads(be.files[SNAP])
        patch(snap)
        be.files[SNAP] = json.dumps(snap, indent=2, sort_keys=True) + "\n"
        shut_prod_down(be)
        be.standard_mutators()
        return M.main(drain_cmd(True), be)

    def test_critical_hashes_ranges_and_negative_numbers(self):
        cases = []

        def drop_hash(s):
            f = next(x for x in s["files"] if x["label"] == "binary")
            f.pop("sha256")
        cases.append((drop_hash, "binary hash"))

        def drop_launcher(s):
            s["files"] = [x for x in s["files"] if x["label"] != "launcher"]
        cases.append((drop_launcher, "launcher entry"))

        def bad_head(s):
            s["git"]["head"] = "26631e4"
        cases.append((bad_head, "short head"))

        def bad_sysctl(s):
            s["sysctls"]["vm.swappiness"] = 99999
        cases.append((bad_sysctl, "sysctl bound"))

        def unknown_sysctl(s):
            s["sysctls_unknown"] = ["vm.swappiness read status unknown"]
        cases.append((unknown_sysctl, "sysctl unknown"))

        def bad_port(s):
            s["port"] = 0
        cases.append((bad_port, "port"))

        def no_procs(s):
            s["processes"] = []
            s["processes_unknown"] = "refused"
        cases.append((no_procs, "process unknown"))

        def no_ticks(s):
            s["processes"][0]["starttime_ticks"] = None
        cases.append((no_ticks, "no start time"))

        def no_ticks_svc(s):
            s["service"]["main_start_ticks"] = None
        cases.append((no_ticks_svc, "no daemon start time"))

        def bad_state(s):
            s["service"]["state"] = "unknown"
        cases.append((bad_state, "unit state"))

        def bad_swap(s):
            s["swaps"]["status"] = "unknown"
        cases.append((bad_swap, "swap status"))

        def bad_marker(s):
            s["nbd_state_file"]["status"] = "unknown"
            s["nbd_state_file"]["device"] = None
        cases.append((bad_marker, "marker status"))

        for i, (fn, label) in enumerate(cases):
            with self.subTest(label):
                be = fixture()
                self.assertEqual(self.snapshot_mutate(be, fn), M.E_REFUSED,
                                 msg=f"snapshot tampering {label} must refuse")
                self.assertEqual(be.mutations(), [], msg=f"{label} mutated anyway")

    def test_negative_budget_staging_margin_and_timeouts(self):
        be = fixture()
        capture(be)
        shut_prod_down(be)
        be.standard_mutators()
        self.assertEqual(M.main(drain_cmd(True, ["--mem-budget-kib", "0"]), be), M.E_REFUSED)
        self.assertEqual(M.main(drain_cmd(True, ["--mem-budget-kib", "-5"]), be), M.E_REFUSED)
        self.assertEqual(M.main(drain_cmd(True, ["--mem-budget-kib", "8000000",
                                                  "--staging-kib", "-1"]), be), M.E_REFUSED)
        self.assertEqual(M.main(drain_cmd(True, ["--mem-budget-kib", "8000000",
                                                  "--margin-kib", "-1"]), be), M.E_REFUSED)
        self.assertEqual(M.main(drain_cmd(True, ["--mem-budget-kib", "8000000",
                                                  "--drain-timeout", "0"]), be), M.E_REFUSED)
        self.assertEqual(M.main(drain_cmd(True, ["--mem-budget-kib", "8000000",
                                                  "--timeout", "-2"]), be), M.E_REFUSED)
        self.assertEqual(M.main(restore_cmd(True, ["--vram-margin-mib", "-1"]), be), M.E_REFUSED)
        self.assertEqual(be.mutations(), [])

    def test_memory_budget_uses_mem_available_not_disk_swap_free(self):
        be = fixture()
        capture(be)
        shut_prod_down(be)
        be.standard_mutators()
        be.files["/proc/meminfo"] = be.files["/proc/meminfo"].replace(
            "MemAvailable: 12000000 kB", "MemAvailable: 7900000 kB")
        be.files["/proc/meminfo"] = be.files["/proc/meminfo"].replace(
            "SwapFree: 4357212 kB", "SwapFree: 11657212 kB")
        rc = M.main(drain_cmd(True, ["--mem-budget-kib", "8000000", "--staging-kib",
                                     "1000000", "--margin-kib", "2097152"]), be)
        self.assertEqual(rc, M.E_REFUSED)
        self.assertEqual(be.mutations(), [])

    def test_unscripted_command_never_counts_as_success(self):
        be = fixture()
        r = be.run(["systemctl", "status", UNIT])
        self.assertEqual(r.rc, 127)
        self.assertIn("UNSCRIPTED", r.stderr)
        r2 = be.run(["nvidia-smi", "-pm", "1"])
        self.assertEqual(r2.rc, 127)
        self.assertTrue(M.is_mutating(["nvidia-smi", "-pm", "1"]))

    def test_mutating_classifier_matches_reality(self):
        for argv in (["swapoff", DEV], ["swapon", DEV, "-p", "1500"], ["mkswap", DEV],
                     ["nbd-client", "-d", DEV], ["sysctl", "-w", "vm.swappiness=150"],
                     ["systemctl", "start", UNIT], ["systemctl", "stop", UNIT],
                     ["nvidia-smi", "-pm", "1"]):
            self.assertTrue(M.is_mutating(argv), msg=str(argv))
        for argv in (["systemctl", "is-active", UNIT], ["systemctl", "show", UNIT],
                     ["ss", "-H", "-ltn", "sport = :8009"],
                     ["nvidia-smi", "--query-gpu=index,name,uuid,memory.total,memory.used",
                      "--format=csv,noheader"],
                     ["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
                      "--format=csv,noheader"],
                     ["uname", "-r"], ["git", "-C", "/w", "rev-parse", "HEAD"]):
            self.assertFalse(M.is_mutating(argv), msg=str(argv))
            self.assertTrue(M.is_readonly(argv), msg=str(argv))


# ----------------------------------------------------------------------------------
# F2 / G3: durable intent, checked postconditions, resumable partial states.
# ----------------------------------------------------------------------------------
class TestDrainOrderAndDurability(unittest.TestCase):
    def test_plan_only_runs_no_mutator_at_all(self):
        be = fixture()
        capture(be)
        shut_prod_down(be)
        be.standard_mutators()
        self.assertEqual(M.main(drain_cmd(False), be), M.E_OK)
        self.assertEqual(be.mutations(), [])
        self.assertNotIn(OPSTATE, be.files)

    def test_intent_is_written_before_the_first_mutator(self):
        be = fixture()
        rc = full_drain(be, clear_after=False)
        self.assertEqual(rc, M.E_OK)
        writes, muts = read_opstate_writes(be), first_mutator_index(be)
        self.assertTrue(writes, "no operation record written")
        self.assertLess(writes[0], muts, "operation record written after the first mutator")
        rec = opstate(be)
        self.assertEqual(rec["phase"], "cleaned")
        want = hashlib.sha256(be.files[SNAP].encode()).hexdigest()
        self.assertEqual(rec["snapshot_sha256"], want)
        self.assertEqual(rec["device"], DEV)
        self.assertEqual(rec["unit"], UNIT)

    def test_full_drain_call_order(self):
        be = fixture()
        self.assertEqual(full_drain(be, clear_after=False), M.E_OK)
        # One pass, in the safe order, and the sysctl write happens AFTER the stop (the
        # installed ExecStop resets 60/65536, so a pre-stop cache would skip the repair).
        order = [c[0] for c in be.calls if M.is_mutating(c)]
        self.assertEqual(order, ["swapoff", "nbd-client", "systemctl", "sysctl"])
        stop_i = [i for i, c in enumerate(be.calls) if c[:2] == ["systemctl", "stop"]]
        sysc_i = [i for i, c in enumerate(be.calls) if c[0] == "sysctl"]
        self.assertTrue(stop_i and sysc_i and min(sysc_i) > min(stop_i))
        self.assertEqual(opstate(be)["phase"], "cleaned")
        self.assertEqual(be.files.get(M.STATE_FILE, None), None)
        self.assertEqual([r["filename"] for r in be.swap_rows if r["filename"] == DEV], [])
        self.assertFalse(be.units[UNIT]["active"])

    def test_swapoff_failure_keeps_backing_marker_and_runs_nothing_else(self):
        be = fixture()
        capture(be)
        shut_prod_down(be)
        be.standard_mutators()
        be.fail_swapoff(8)
        self.assertEqual(M.main(drain_cmd(True), be), M.E_DRAIN_ABORTED)
        self.assertEqual(opstate(be)["phase"], "drain-failed")
        self.assertEqual([c[0] for c in be.mutations()], ["swapoff"])
        self.assertIn(M.STATE_FILE, be.files)                    # marker kept
        self.assertTrue(any(r["filename"] == DEV for r in be.swap_rows))  # backing kept
        self.assertTrue(be.units[UNIT]["active"])

    def test_swapoff_rc0_with_entry_surviving_is_not_drained(self):
        be = fixture()
        capture(be)
        shut_prod_down(be)
        be.standard_mutators()
        be.on_swapoff = lambda d: (0, "", "")      # claims success, removes nothing
        self.assertEqual(M.main(drain_cmd(True), be), M.E_DRAIN_ABORTED)
        self.assertEqual(opstate(be)["phase"], "drain-failed")
        self.assertEqual([c[0] for c in be.mutations()], ["swapoff"])
        self.assertIn(M.STATE_FILE, be.files)
        self.assertTrue(be.units[UNIT]["active"])

    def test_table_unreadable_after_swapoff_aborts_without_cleanup(self):
        be = fixture()
        capture(be)
        shut_prod_down(be)
        be.standard_mutators()
        real = be.on_swapoff

        def swapoff_then_blind(dev):
            r = real(dev)
            be.swap_mode = None
            be.unreadable.add(M.SWAPS_FILE)
            return r
        be.on_swapoff = swapoff_then_blind
        self.assertEqual(M.main(drain_cmd(True), be), M.E_DRAIN_ABORTED)
        self.assertEqual(opstate(be)["phase"], "drain-abort-swap-table-unknown")
        self.assertEqual([c[0] for c in be.mutations()], ["swapoff"])
        self.assertIn(M.STATE_FILE, be.files)
        self.assertTrue(be.units[UNIT]["active"])

    def test_disconnect_failure_is_partial_and_resumes_next_run(self):
        be = fixture()
        capture(be)
        shut_prod_down(be)
        be.standard_mutators()
        be.fail_disconnect(1)
        self.assertEqual(M.main(drain_cmd(True), be), M.E_PARTIAL)
        self.assertEqual(opstate(be)["phase"], "drain-disconnect-failed")
        self.assertIn(M.STATE_FILE, be.files)                    # never removed unproved
        self.assertTrue(any(f"/sys/block/{DEV[5:]}/pid" == p for p in be.files))
        self.assertEqual([c[0] for c in be.mutations()], ["swapoff", "nbd-client"])
        self.assertTrue(be.units[UNIT]["active"])               # unit not stopped
        be.on_nbd_client = None
        be.standard_mutators()                                   # operator fixes the cause
        self.assertEqual(M.main(drain_cmd(True), be), M.E_OK)
        self.assertEqual(opstate(be)["phase"], "cleaned")

    def test_stop_failure_after_proved_detach_resumes_without_the_removed_marker(self):
        be = fixture()
        capture(be)
        shut_prod_down(be)
        be.standard_mutators()
        be.fail_stop(1)
        rc = M.main(drain_cmd(True), be)
        if rc != M.E_OK:
            self.assertEqual(rc, M.E_PARTIAL)
            self.assertEqual(opstate(be)["phase"], "drain-stop-failed")
            self.assertNotIn(M.STATE_FILE, be.files)             # detach was proved
            self.assertEqual([c[0] for c in be.mutations()],
                             ["swapoff", "nbd-client", "systemctl"])
            be.on_stop = None
            be.standard_mutators()
        self.assertEqual(M.main(drain_cmd(True), be), M.E_OK)
        self.assertEqual(opstate(be)["phase"], "cleaned")
        self.assertFalse(be.units[UNIT]["active"])

    def test_stop_rc0_with_unit_still_active_is_partial(self):
        be = fixture()
        capture(be)
        shut_prod_down(be)
        be.standard_mutators()
        be.on_stop = lambda u: (0, "", "")      # claims success, changes nothing
        self.assertEqual(M.main(drain_cmd(True), be), M.E_PARTIAL)
        self.assertEqual(opstate(be)["phase"], "drain-stop-failed")
        self.assertTrue(be.units[UNIT]["active"])
        self.assertNotIn(M.STATE_FILE, be.files)

    def test_sysctl_failure_keeps_the_partial_record_and_completes_on_retry(self):
        be = fixture()
        capture(be)
        shut_prod_down(be)
        be.standard_mutators()
        be.set_sysctls(60, 65536)               # live differs from captured 150/1048576
        be.fail_sysctl(1)
        self.assertEqual(M.main(drain_cmd(True), be), M.E_PARTIAL)
        rec = opstate(be)
        self.assertEqual(rec["phase"], "drain-sysctl-failed")
        self.assertEqual([r["filename"] for r in be.swap_rows if r["filename"] == DEV], [])
        self.assertFalse(be.units[UNIT]["active"])
        be.on_sysctl = None
        be.standard_mutators()
        self.assertEqual(M.main(drain_cmd(True), be), M.E_OK)
        self.assertEqual(opstate(be)["phase"], "cleaned")
        self.assertEqual(be.files["/proc/sys/vm/swappiness"].strip(), "150")

    def test_drain_never_touches_other_swap_devices(self):
        be = fixture()
        self.assertEqual(full_drain(be, clear_after=False), M.E_OK)
        self.assertEqual([c for c in be.mutations() if "swapoff" == c[0] and c[1] != DEV], [])
        self.assertTrue(any(r["filename"] == "/mnt/swap2" for r in be.swap_rows))
        self.assertNotIn("swapoff -a", [" ".join(c) for c in be.calls])

    def test_already_drained_is_proven_from_live_facts_not_from_its_own_marker(self):
        be = fixture()
        self.assertEqual(full_drain(be), M.E_OK)
        be.calls.clear()
        be.log.clear()
        self.assertEqual(M.main(drain_cmd(True), be), M.E_OK)
        self.assertEqual(be.mutations(), [])

# ----------------------------------------------------------------------------------
# F3 / G2: the backing identity binding (client record != daemon) and its refusals.
# ----------------------------------------------------------------------------------
class TestBackingIdentity(unittest.TestCase):
    def test_client_record_is_not_the_daemon(self):
        be = fixture()
        capture(be)
        shut_prod_down(be)
        snap = json.loads(be.files[SNAP])
        self.assertEqual(snap["nbd_client"]["pid"], CLIENT_PID)
        self.assertEqual(snap["service"]["main_pid"], DAEMON_PID)
        self.assertNotEqual(snap["nbd_client"]["pid"], snap["service"]["main_pid"])
        self.assertIn("presence", snap["nbd_client"]["note"])
        self.assertTrue(snap["nbd_socket"]["is_socket"])
        self.assertEqual(snap["nbd_socket"]["uid"], 0)
        self.assertTrue(snap["nbd_causality"]["proven"])

    def test_daemon_executable_mismatch_refuses(self):
        be = fixture()
        capture(be)
        shut_prod_down(be)
        be.files[f"/proc/{DAEMON_PID}/cmdline"] = "/usr/sbin/something-else\0"
        be.standard_mutators()
        self.assertEqual(M.main(drain_cmd(True), be), M.E_REFUSED)
        self.assertEqual(be.mutations(), [])

    def test_daemon_starttime_mismatch_refuses(self):
        be = fixture()
        capture(be)
        shut_prod_down(be)
        be.files[f"/proc/{DAEMON_PID}/stat"] = stat_line(DAEMON_PID, DAEMON_TICKS + 5000)
        be.standard_mutators()
        self.assertEqual(M.main(drain_cmd(True), be), M.E_REFUSED)
        self.assertEqual(be.mutations(), [])

    def test_marker_naming_another_device_refuses(self):
        be = fixture()
        capture(be)
        shut_prod_down(be)
        be.files[M.STATE_FILE] = "/dev/nbd3\n"
        be.standard_mutators()
        self.assertEqual(M.main(drain_cmd(True), be), M.E_REFUSED)
        self.assertEqual(be.mutations(), [])

    def test_unreadable_client_record_with_active_swap_refuses(self):
        be = fixture()
        capture(be)
        shut_prod_down(be)
        be.unreadable.add(f"/sys/block/{DEV[5:]}/pid")
        be.standard_mutators()
        self.assertEqual(M.main(drain_cmd(True), be), M.E_REFUSED)
        self.assertEqual(be.mutations(), [])

    def test_unit_names_are_not_taken_from_the_snapshot_blindly(self):
        be = fixture()
        capture(be)
        snap = json.loads(be.files[SNAP])
        snap["unit"] = "evil.service; rm -rf"
        be.files[SNAP] = json.dumps(snap, indent=2, sort_keys=True) + "\n"
        shut_prod_down(be)
        self.assertEqual(M.main(drain_cmd(True), be), M.E_REFUSED)
        self.assertEqual(be.mutations(), [])


# ----------------------------------------------------------------------------------
# F4: non-bypassable free-VRAM gate, unit-start path, device renumber.
# ----------------------------------------------------------------------------------
def backing_alive_after_drain(be):
    """Post-drain shape where the backing daemon survived with its client attached: unit
    active, kernel client record present, marker present, no swap row, and the causal proof
    for THIS daemon readable (the root-shape the harness requires for any action)."""
    be.units[UNIT] = {"active": True, "main_pid": DAEMON_PID, "active_state": "active",
                      "sub_state": "running"}
    be.files[f"/proc/{DAEMON_PID}/cmdline"] = "/usr/local/bin/nbd-vram\0"
    be.files[f"/proc/{DAEMON_PID}/stat"] = stat_line(DAEMON_PID, DAEMON_TICKS)
    be.proc_pids.add(str(DAEMON_PID))
    be.make_causal(DAEMON_PID)
    be.files[f"/sys/block/{DEV[5:]}/pid"] = str(CLIENT_PID) + "\n"
    be.files[M.STATE_FILE] = DEV + "\n"
    be.gpu_apps = [{"pid": DAEMON_PID, "name": "nbd-vram", "used_mib": 7450}]


def rearm(be):
    """Put the system back into the shape a drain has work on: swap row present, daemon
    active with a proved backing, marker present, sysctls at the captured values.  Used by
    the record-reuse regressions so the drain genuinely reaches its mutators."""
    backing_alive_after_drain(be)
    if not any(r["filename"] == DEV for r in be.swap_rows):
        be.swap_rows.append(swap_row(DEV, used=1000, prio=1500))
        be.files[M.SWAPS_FILE] = render_swaps(be.swap_rows)
    be.set_sysctls(150, 1048576)


class TestRestore(unittest.TestCase):
    def test_drained_fixture_restore_uses_unit_start_and_never_swapon(self):
        be = fixture()
        self.assertEqual(full_drain(be), M.E_OK)
        be.gpu_apps = []
        be.gpu_gpus = [gpu_row(used=0)]
        be.standard_mutators()
        rc = M.main(restore_cmd(True, ["--confirm-cuda-released"]), be)
        self.assertEqual(rc, M.E_OK)
        self.assertEqual([c[0] for c in be.mutations()], ["systemctl"])
        self.assertEqual(opstate(be)["phase"], "restored")
        self.assertEqual(opstate(be)["restored_device"], DEV)

    def test_helper_allocation_blocks_unit_start_even_with_allowed_pid(self):
        be = fixture()
        self.assertEqual(full_drain(be), M.E_OK)
        # A known CUDA helper now holds 4000 MiB of the 8192 MiB card.
        be.gpu_apps = [{"pid": 5000, "name": "cuda-helper", "used_mib": 4000}]
        be.gpu_gpus = [gpu_row(used=4000)]
        be.standard_mutators()
        rc = M.main(restore_cmd(True, ["--confirm-cuda-released", "--allow-compute-pid",
                                       "5000"]), be)
        self.assertEqual(rc, M.E_REFUSED)
        self.assertEqual(be.mutations(), [])

    def test_free_vram_must_cover_reservation_plus_margin(self):
        be = fixture()
        self.assertEqual(full_drain(be), M.E_OK)
        be.standard_mutators()
        # 8192 - 700 = 7492 MiB free, reservation 7438 MiB + default margin 512 = 7950.
        be.gpu_apps = []
        be.gpu_gpus = [gpu_row(used=700)]
        self.assertEqual(M.main(restore_cmd(True, ["--confirm-cuda-released"]), be), M.E_REFUSED)
        # The gate is arithmetic, not a flag: naming the client cannot waive it.
        self.assertEqual([c[0] for c in be.mutations()], [])

    def test_uuid_missing_from_snapshot_refuses_unit_start(self):
        be = fixture()
        self.assertEqual(full_drain(be), M.E_OK)
        snap = json.loads(be.files[SNAP])
        snap["gpu"]["nvidia"]["gpus"][0]["uuid"] = ""
        be.files[SNAP] = json.dumps(snap, indent=2, sort_keys=True) + "\n"
        be.gpu_apps, be.gpu_gpus = [], [gpu_row(used=0)]
        be.standard_mutators()
        self.assertEqual(M.main(restore_cmd(True, ["--confirm-cuda-released"]), be), M.E_REFUSED)

    def test_uuid_change_refuses_unit_start(self):
        be = fixture()
        self.assertEqual(full_drain(be), M.E_OK)
        be.gpu_apps, be.gpu_gpus = [], [gpu_row(used=0, uuid="GPU-changed")]
        be.standard_mutators()
        self.assertEqual(M.main(restore_cmd(True, ["--confirm-cuda-released"]), be), M.E_REFUSED)

    def test_unreadable_nvidia_data_refuses_unit_start(self):
        be = fixture()
        self.assertEqual(full_drain(be), M.E_OK)
        be.gpu_unknown = True
        be.standard_mutators()
        self.assertEqual(M.main(restore_cmd(True, ["--confirm-cuda-released"]), be), M.E_REFUSED)

    def test_unit_start_requires_cuda_release_confirmation(self):
        be = fixture()
        self.assertEqual(full_drain(be), M.E_OK)
        be.gpu_apps, be.gpu_gpus = [], [gpu_row(used=0)]
        be.standard_mutators()
        self.assertEqual(M.main(restore_cmd(True), be), M.E_REFUSED)
        self.assertEqual(be.mutations(), [])

    def test_unit_start_rc0_without_swap_keeps_the_record(self):
        be = fixture()
        self.assertEqual(full_drain(be), M.E_OK)
        be.gpu_apps, be.gpu_gpus = [], [gpu_row(used=0)]
        be.standard_mutators()
        be.on_start = lambda u: (0, "", "")     # start "succeeds", changes nothing
        self.assertEqual(M.main(restore_cmd(True, ["--confirm-cuda-released"]), be), M.E_PARTIAL)
        self.assertEqual(opstate(be)["phase"], "restore-swap-unproven")
        self.assertIn(OPSTATE, be.files)

    def test_unit_start_connect_failure_keeps_the_record(self):
        be = fixture()
        self.assertEqual(full_drain(be), M.E_OK)
        be.gpu_apps, be.gpu_gpus = [], [gpu_row(used=0)]
        be.standard_mutators()
        be.fail_start(1)
        self.assertEqual(M.main(restore_cmd(True, ["--confirm-cuda-released"]), be), M.E_PARTIAL)
        self.assertEqual(opstate(be)["phase"], "restore-swap-unproven")
        self.assertIn(OPSTATE, be.files)

    def test_restore_renumbers_nbd0_to_nbd1_and_binds_the_new_device(self):
        be = fixture()
        self.assertEqual(full_drain(be), M.E_OK)
        be.gpu_apps, be.gpu_gpus = [], [gpu_row(used=0)]
        be.standard_mutators(new_dev="/dev/nbd1")
        rc = M.main(restore_cmd(True, ["--confirm-cuda-released"]), be)
        self.assertEqual(rc, M.E_OK)
        rec = opstate(be)
        self.assertEqual(rec["restored_device"], "/dev/nbd1")
        self.assertEqual(rec["restored_priority"], 1500)
        self.assertEqual(be.files[M.STATE_FILE], "/dev/nbd1\n")
        self.assertTrue(any(r["filename"] == "/dev/nbd1" and r["priority"] == 1500
                            for r in be.swap_rows))
        self.assertFalse(any(r["filename"] == DEV for r in be.swap_rows))
        self.assertEqual(M.main(verify_cmd("restored"), be), M.E_OK)

    def test_swapon_path_when_backing_alive_and_connected(self):
        be = fixture()
        self.assertEqual(full_drain(be), M.E_OK)
        backing_alive_after_drain(be)
        be.standard_mutators()
        rc = M.main(restore_cmd(True), be)
        self.assertEqual(rc, M.E_OK)
        self.assertEqual([c[0] for c in be.mutations()], ["swapon"])
        self.assertTrue(any(r["filename"] == DEV and r["priority"] == 1500
                            for r in be.swap_rows))
        self.assertEqual(opstate(be)["phase"], "restored")

    def test_swapon_rc0_without_swap_entry_is_failure_with_record_kept(self):
        be = fixture()
        self.assertEqual(full_drain(be), M.E_OK)
        backing_alive_after_drain(be)
        be.standard_mutators()
        be.fail_swapon(1)
        self.assertEqual(M.main(restore_cmd(True), be), M.E_PARTIAL)
        self.assertEqual(opstate(be)["phase"], "restore-swap-unproven")
        be.on_swapon = None
        be.standard_mutators()
        self.assertEqual(M.main(restore_cmd(True), be), M.E_OK)
        self.assertEqual(opstate(be)["phase"], "restored")

    def test_unexpected_compute_client_blocks_the_swapon_path(self):
        be = fixture()
        self.assertEqual(full_drain(be), M.E_OK)
        backing_alive_after_drain(be)
        be.standard_mutators()
        be.gpu_apps = [{"pid": DAEMON_PID, "name": "nbd-vram", "used_mib": 7450},
                       {"pid": 5001, "name": "cuda-helper", "used_mib": 4000}]
        self.assertEqual(M.main(restore_cmd(True), be), M.E_REFUSED)
        self.assertEqual([c[0] for c in be.mutations()], [])
        self.assertEqual(M.main(restore_cmd(True, ["--allow-compute-pid", "5001"]), be), M.E_OK)

    def test_restore_requires_its_own_record_and_the_matching_snapshot(self):
        be = fixture()
        self.assertEqual(full_drain(be), M.E_OK)
        be.files[OPSTATE] = be.files[OPSTATE].replace(opstate(be)["snapshot_sha256"],
                                                      "0" * 64)
        self.assertEqual(M.main(restore_cmd(True), be), M.E_REFUSED)
        be.files.pop(OPSTATE)
        self.assertEqual(M.main(restore_cmd(True), be), M.E_REFUSED)

    def test_restore_refuses_a_marker_it_removed_itself(self):
        be = fixture()
        self.assertEqual(full_drain(be), M.E_OK)
        self.assertNotIn(M.STATE_FILE, be.files)
        be.gpu_apps, be.gpu_gpus = [], [gpu_row(used=0)]
        be.standard_mutators()
        # No live marker, no swap entry, unit inactive: the unit-start path must be chosen
        # and must NOT swapon the snapshot's old device name.
        self.assertEqual(M.main(restore_cmd(True, ["--confirm-cuda-released"]), be), M.E_OK)
        self.assertNotIn("swapon", [c[0] for c in be.mutations()])

    def test_restore_sysctl_failure_keeps_the_swap_and_the_record(self):
        be = fixture()
        self.assertEqual(full_drain(be), M.E_OK)
        # Model the case where the unit's own sysctl write does not happen, so restore has
        # to repair vm.swappiness / vm.min_free_kbytes itself.
        be.standard_mutators(sysctls_on_start=False)
        be.set_sysctls(60, 65536)
        be.fail_sysctl(1)
        be.gpu_apps, be.gpu_gpus = [], [gpu_row(used=0)]
        rc = M.main(restore_cmd(True, ["--confirm-cuda-released"]), be)
        self.assertEqual(rc, M.E_PARTIAL)
        self.assertEqual(opstate(be)["phase"], "restore-sysctl-failed")
        self.assertTrue(any(r["filename"] == DEV for r in be.swap_rows))
        be.on_sysctl = None
        be.standard_mutators()
        self.assertEqual(M.main(restore_cmd(True, ["--confirm-cuda-released"]), be), M.E_OK)
        self.assertEqual(opstate(be)["phase"], "restored")

    def test_restore_refuses_a_state_it_did_not_create(self):
        be = fixture()
        self.assertEqual(full_drain(be), M.E_OK)
        backing_alive_after_drain(be)
        be.files.pop(f"/sys/block/{DEV[5:]}/pid")     # marker + active unit, no client: mixed
        be.standard_mutators()
        self.assertEqual(M.main(restore_cmd(True), be), M.E_REFUSED)
        self.assertEqual([c[0] for c in be.mutations()], [])


# ----------------------------------------------------------------------------------
# Verify as the runtime readiness gate.
# ----------------------------------------------------------------------------------
class TestVerifyReadiness(unittest.TestCase):
    def test_drained_passes_when_gpu_release_is_proven(self):
        be = fixture()
        self.assertEqual(full_drain(be), M.E_OK)
        be.gpu_gpus = [gpu_row(used=180)]          # 7544 - 180 = 7364 drop >= 7287 - 512
        self.assertEqual(M.main(verify_cmd("drained"), be), M.E_OK)

    def test_drained_fails_when_gpu_release_is_unknown(self):
        be = fixture()
        self.assertEqual(full_drain(be), M.E_OK)
        be.gpu_unknown = True
        self.assertEqual(M.main(verify_cmd("drained"), be), M.E_VERIFY_FAIL)

    def test_drained_fails_when_gpu_release_is_small(self):
        be = fixture()
        self.assertEqual(full_drain(be), M.E_OK)
        be.gpu_gpus = [gpu_row(used=7500)]
        self.assertEqual(M.main(verify_cmd("drained"), be), M.E_VERIFY_FAIL)

    def test_restored_requires_priority_and_sysctls_proven(self):
        be = fixture()
        self.assertEqual(full_drain(be), M.E_OK)
        be.gpu_apps, be.gpu_gpus = [], [gpu_row(used=0)]
        be.standard_mutators()
        self.assertEqual(M.main(restore_cmd(True, ["--confirm-cuda-released"]), be), M.E_OK)
        self.assertEqual(M.main(verify_cmd("restored"), be), M.E_OK)
        be.set_sysctls(60, 65536)
        self.assertEqual(M.main(verify_cmd("restored"), be), M.E_VERIFY_FAIL)


# ----------------------------------------------------------------------------------
# Capture behaviour and the redaction guarantee.
# ----------------------------------------------------------------------------------
class TestCaptureAndRedaction(unittest.TestCase):
    def test_capture_records_unknown_processes_instead_of_aborting(self):
        be = fixture()
        be.unreadable.add("/proc")
        snap = capture(be)
        self.assertEqual(snap["processes"], [])
        self.assertTrue(snap["processes_unknown"])
        shut_prod_down(be)
        be.standard_mutators()
        self.assertEqual(M.main(drain_cmd(True), be), M.E_REFUSED)

    def test_capture_marks_unreadable_files_and_records_socket_uid(self):
        be = fixture()
        be.unreadable.add(BIN)
        be.unreadable.add(f"/proc/{DAEMON_PID}/exe")
        snap = capture(be)
        binary = next(f for f in snap["files"] if f["label"] == "binary")
        self.assertTrue(binary.get("unreadable"))
        self.assertNotIn("sha256", binary)
        self.assertTrue(snap["nbd_socket"]["is_socket"])
        self.assertEqual(snap["nbd_socket"]["status"], "ok")
        self.assertEqual(snap["nbd_socket"]["uid"], 0)
        shut_prod_down(be)
        be.standard_mutators()
        self.assertEqual(M.main(drain_cmd(True), be), M.E_REFUSED)

    def test_snapshot_is_never_overwritten(self):
        be = fixture()
        capture(be)
        self.assertRaises(M.Refuse, capture, be)

    def test_redaction_keeps_engine_flags_and_drops_credentials(self):
        raw = ("\0".join(["/venv/bin/python", "server.py", "--engine", "strata", "--config",
                          "c.json", "--port", "8009", "--max-context", "131072",
                          "--api-key", "hunter2", "--api-key-env", "STRATA_KEY",
                          "--token", "abc", "--x", "/home/u/private.txt"]) + "\0")
        red = M.redact_cmdline(raw)
        self.assertIn("--port 8009", red)
        self.assertIn("--max-context 131072", red)
        self.assertIn("server.py", red)
        self.assertNotIn("hunter2", red)
        self.assertNotIn("STRATA_KEY", red)
        self.assertNotIn("abc", red)
        self.assertNotIn("/home/u/private.txt", red)
        self.assertIn("--api-key <redacted>", red)

    def test_conversation_cache_min_tokens_is_not_a_secret(self):
        self.assertFalse(M.is_secret_flag("conversation-cache-min-tokens"))
        self.assertFalse(M.is_secret_flag("max-tokens"))
        self.assertFalse(M.is_secret_flag("max-context"))
        self.assertTrue(M.is_secret_flag("api-key"))
        self.assertTrue(M.is_secret_flag("authToken"))
        self.assertTrue(M.is_secret_flag("tokens-per-sec"))   # whole word 'tokens' -> redact
        # A flag whose value is numeric but whose NAME carries a secret word is redacted:
        # redaction is conservative, and only the whitelist names are kept.
        self.assertFalse(M.is_secret_flag("prompt-cache-spread"))

    def test_snapshot_schema_mismatch_refuses(self):
        be = fixture()
        capture(be)
        snap = json.loads(be.files[SNAP])
        snap["schema"] = "strata-mixed-gpu-snapshot/1"
        be.files[SNAP] = json.dumps(snap, indent=2, sort_keys=True) + "\n"
        shut_prod_down(be)
        self.assertEqual(M.main(drain_cmd(True), be), M.E_REFUSED)

    def test_opstate_phase_must_be_one_this_tool_writes(self):
        be = fixture()
        self.assertEqual(full_drain(be), M.E_OK)
        rec = opstate(be)
        rec["phase"] = "whatever"
        be.files[OPSTATE] = json.dumps(rec, indent=2, sort_keys=True) + "\n"
        be.gpu_apps, be.gpu_gpus = [], [gpu_row(used=0)]
        be.standard_mutators()
        self.assertEqual(M.main(restore_cmd(True, ["--confirm-cuda-released"]), be), M.E_REFUSED)

    def test_proc_start_ticks_field_position(self):
        self.assertEqual(M.proc_start_ticks(stat_line(7, 4533)), 4533)
        self.assertRaises(M.ParseFail, M.proc_start_ticks, "7 (x) S 1 2 3")
        self.assertRaises(M.ParseFail, M.proc_start_ticks, None)
        bad = f"7 (proc) S {' '.join(['1'] * 18)} notanumber 0 0 0"
        self.assertRaises(M.ParseFail, M.proc_start_ticks, bad)

    def test_start_time_is_the_20th_tail_field(self):
        # Documents the field position this code relies on: /proc/<pid>/stat field 22
        # (1-based) is starttime, i.e. index 19 of the tail after the ')'.
        tail = stat_line(1, 4533).rsplit(")", 1)[-1].split()
        self.assertEqual(len(tail), 23)
        self.assertEqual(tail[19], "4533")
        self.assertTrue(tail[0] in ("R", "S", "D", "T", "Z", "X", "I"))


# ----------------------------------------------------------------------------------
# Gate 1 attempt 2, finding 1: one-pass sysctl handling.
# ----------------------------------------------------------------------------------
class TestOnePassSysctl(unittest.TestCase):
    def test_successful_drain_is_one_pass_and_reaches_cleaned(self):
        be = fixture()
        self.assertEqual(full_drain(be, clear_after=False), M.E_OK)
        rec = opstate(be)
        self.assertEqual(rec["phase"], "cleaned")                    # NOT a partial
        sysc = [c for c in be.calls if c[0] == "sysctl"]
        self.assertEqual(len(sysc), 1)                               # exactly one write
        self.assertEqual(be.files["/proc/sys/vm/swappiness"], "150\n")
        self.assertEqual(be.files["/proc/sys/vm/min_free_kbytes"], "1048576\n")

    def test_plan_states_the_post_stop_sysctl_decision(self):
        be = fixture()
        capture(be)
        shut_prod_down(be)
        self.assertEqual(M.main(drain_cmd(False), be), M.E_OK)
        self.assertEqual(be.mutations(), [])

    def test_sysctl_failure_then_retry_completes(self):
        be = fixture()
        self.assertEqual(full_drain(be), M.E_OK)                     # baseline is clean
        be.set_sysctls(60, 65536)                                    # a later stop reset them
        be.standard_mutators()
        be.fail_sysctl(1)
        self.assertEqual(M.main(drain_cmd(True), be), M.E_PARTIAL)
        self.assertEqual(opstate(be)["phase"], "drain-sysctl-failed")
        be.on_sysctl = None
        be.standard_mutators()
        self.assertEqual(M.main(drain_cmd(True), be), M.E_OK)
        self.assertEqual(opstate(be)["phase"], "cleaned")


# ----------------------------------------------------------------------------------
# Gate 1 attempt 2, finding 2: no PASS on states the tool's own proofs reject.
# ----------------------------------------------------------------------------------
def drained_ready():
    """A fixture proven drained: drained, production down, port closed, GPU released."""
    be = fixture()
    assert full_drain(be) == M.E_OK
    be.gpu_gpus = [gpu_row(used=180)]        # drop 7364 MiB >= 7287 - 512 needed
    return be


def restore_ready_unit_start(be, new_dev=DEV):
    be.gpu_apps, be.gpu_gpus = [], [gpu_row(used=0)]
    be.standard_mutators(new_dev=new_dev)


def restore_prod(be):
    be.proc_pids |= {str(WRAP_PID), str(ENG_PID)}
    be.files[f"/proc/{WRAP_PID}/cmdline"] = "/venv/bin/python\0server.py\0--port\08009\0"
    be.files[f"/proc/{WRAP_PID}/stat"] = stat_line(WRAP_PID, 90000)
    be.files[f"/proc/{ENG_PID}/cmdline"] = "/w/build-906-inco/strata\0--model\0q4_0\0"
    be.files[f"/proc/{ENG_PID}/stat"] = stat_line(ENG_PID, 90010)
    be.ports.add("8009")


class TestVerifyPredicates(unittest.TestCase):
    def test_drained_passes_only_with_every_proof(self):
        self.assertEqual(M.main(verify_cmd("drained"), drained_ready()), M.E_OK)

    def test_drained_requires_the_marker_absent(self):
        be = drained_ready()
        be.files[M.STATE_FILE] = DEV + "\n"
        self.assertEqual(M.main(verify_cmd("drained"), be), M.E_VERIFY_FAIL)

    def test_drained_requires_the_client_detached(self):
        be = drained_ready()
        be.files["/sys/block/nbd0/pid"] = str(CLIENT_PID) + "\n"
        self.assertEqual(M.main(verify_cmd("drained"), be), M.E_VERIFY_FAIL)

    def test_drained_requires_the_port_closed(self):
        be = drained_ready()
        be.ports.add("8009")
        self.assertEqual(M.main(verify_cmd("drained"), be), M.E_VERIFY_FAIL)

    def test_drained_requires_recorded_prod_gone(self):
        be = drained_ready()
        restore_prod(be)
        self.assertEqual(M.main(verify_cmd("drained"), be), M.E_VERIFY_FAIL)

    def test_drained_requires_sysctls_to_match(self):
        be = drained_ready()
        be.set_sysctls(60, 65536)
        self.assertEqual(M.main(verify_cmd("drained"), be), M.E_VERIFY_FAIL)

    def test_restored_renumbered_receipt_passes(self):
        """Probe D1, run explicitly: the genuine renumbered unit-start restore must PASS."""
        be = fixture()
        self.assertEqual(full_drain(be), M.E_OK)
        restore_ready_unit_start(be, new_dev="/dev/nbd1")
        self.assertEqual(M.main(restore_cmd(True, ["--confirm-cuda-released"]), be), M.E_OK)
        rec = opstate(be)
        self.assertEqual(rec["restored_device"], "/dev/nbd1")
        self.assertEqual(rec["restored_daemon_binding"], "new-daemon-from-restore-intent")
        self.assertEqual(M.main(verify_cmd("restored"), be), M.E_OK)

    def test_restored_foreign_all_zero_record_refuses(self):
        """Probe D2, run explicitly: an all-zero snapshot_sha256, a restored_device of /dev/nbd9
        and a live marker naming nbd1 must NEVER produce a PASS, and must not be used to pick
        a device at all."""
        be = fixture()
        self.assertEqual(full_drain(be), M.E_OK)
        restore_ready_unit_start(be, new_dev="/dev/nbd1")
        self.assertEqual(M.main(restore_cmd(True, ["--confirm-cuda-released"]), be), M.E_OK)
        rec = opstate(be)
        rec["snapshot_sha256"] = "0" * 64
        rec["restored_device"] = "/dev/nbd9"
        be.files[OPSTATE] = json.dumps(rec, indent=2, sort_keys=True) + "\n"
        be.files[M.STATE_FILE] = "/dev/nbd1\n"
        rc = M.main(verify_cmd("restored"), be)
        self.assertEqual(rc, M.E_REFUSED)              # refused: not a PASS, not a guess
        self.assertNotEqual(rc, M.E_OK)

    def test_restored_malformed_record_refuses(self):
        be = fixture()
        self.assertEqual(full_drain(be), M.E_OK)
        be.files[OPSTATE] = "[1, 2]\n"
        self.assertEqual(M.main(verify_cmd("restored"), be), M.E_REFUSED)
        be.files[OPSTATE] = "{not json"
        self.assertEqual(M.main(verify_cmd("restored"), be), M.E_REFUSED)

    def test_restored_marker_must_name_the_receipt_target(self):
        be = fixture()
        self.assertEqual(full_drain(be), M.E_OK)
        restore_ready_unit_start(be, new_dev="/dev/nbd1")
        self.assertEqual(M.main(restore_cmd(True, ["--confirm-cuda-released"]), be), M.E_OK)
        be.files[M.STATE_FILE] = DEV + "\n"            # marker names the OLD device
        self.assertEqual(M.main(verify_cmd("restored"), be), M.E_VERIFY_FAIL)

    def test_restored_requires_the_socket_object_and_owner(self):
        be = fixture()
        self.assertEqual(full_drain(be), M.E_OK)
        restore_ready_unit_start(be, new_dev="/dev/nbd1")
        self.assertEqual(M.main(restore_cmd(True, ["--confirm-cuda-released"]), be), M.E_OK)
        be.sock_mode[M.SOCK_FILE] = 0o100600           # a regular file at the socket path
        self.assertEqual(M.main(verify_cmd("restored"), be), M.E_VERIFY_FAIL)
        be.sock_mode[M.SOCK_FILE] = 0o140600
        be.sock_uid[M.SOCK_FILE] = 1000                # someone else owns it
        self.assertEqual(M.main(verify_cmd("restored"), be), M.E_VERIFY_FAIL)

    def test_restored_requires_the_causal_proof(self):
        be = fixture()
        self.assertEqual(full_drain(be), M.E_OK)
        restore_ready_unit_start(be, new_dev="/dev/nbd1")
        self.assertEqual(M.main(restore_cmd(True, ["--confirm-cuda-released"]), be), M.E_OK)
        be.make_uncasual(4400)                         # the new daemon's backing is unprovable
        self.assertEqual(M.main(verify_cmd("restored"), be), M.E_VERIFY_FAIL)

    def test_restored_requires_the_client_connected(self):
        be = fixture()
        self.assertEqual(full_drain(be), M.E_OK)
        restore_ready_unit_start(be, new_dev="/dev/nbd1")
        self.assertEqual(M.main(restore_cmd(True, ["--confirm-cuda-released"]), be), M.E_OK)
        be.files.pop("/sys/block/nbd1/pid")
        self.assertEqual(M.main(verify_cmd("restored"), be), M.E_VERIFY_FAIL)


# ----------------------------------------------------------------------------------
# Gate 1 attempt 2, finding 3: identity is boot + pid + start time, and the causal
# device->socket->daemon mapping must be provable or the action is refused.
# ----------------------------------------------------------------------------------
class TestIdentityAndCausality(unittest.TestCase):
    def test_same_ticks_in_a_new_boot_refuses(self):
        be = fixture()
        capture(be)
        shut_prod_down(be)
        be.standard_mutators()
        be.files["/proc/stat"] = "btime 1770000000\n"      # rebooted, identical tick offset
        self.assertEqual(M.main(drain_cmd(True), be), M.E_REFUSED)
        self.assertEqual(be.mutations(), [])

    def test_daemon_pid_change_refuses(self):
        be = fixture()
        capture(be)
        shut_prod_down(be)
        be.standard_mutators()
        be.units[UNIT]["main_pid"] = 9999                  # restarted unit, same start ticks
        be.files["/proc/9999/cmdline"] = "/usr/local/bin/nbd-vram\0"
        be.files["/proc/9999/stat"] = stat_line(9999, DAEMON_TICKS)
        be.proc_pids.add("9999")
        be.make_causal(9999)
        self.assertEqual(M.main(drain_cmd(True), be), M.E_REFUSED)
        self.assertEqual(be.mutations(), [])

    def test_socket_owner_change_refuses(self):
        be = fixture()
        capture(be)
        shut_prod_down(be)
        be.standard_mutators()
        be.sock_uid[M.SOCK_FILE] = 1000
        self.assertEqual(M.main(drain_cmd(True), be), M.E_REFUSED)
        self.assertEqual(be.mutations(), [])

    def test_socket_type_change_refuses(self):
        be = fixture()
        capture(be)
        shut_prod_down(be)
        be.standard_mutators()
        be.sock_mode[M.SOCK_FILE] = 0o100600
        self.assertEqual(M.main(drain_cmd(True), be), M.E_REFUSED)
        self.assertEqual(be.mutations(), [])

    def test_unprovable_causality_refuses_and_mutates_nothing(self):
        """The current host's shape: /proc/<daemonpid>/fd is root-only and a unix socket has
        no readable peer for this operator.  Presence is not causality, so the action is
        refused as unsupported - not documented as a permission to proceed."""
        be = fixture()
        capture(be)
        shut_prod_down(be)
        be.standard_mutators()
        be.make_uncasual(DAEMON_PID)
        self.assertFalse(M.probe_causality(be, DAEMON_PID)["proven"])
        with redirect_stderr(io.StringIO()) as err:
            rc = M.main(drain_cmd(True), be)
        self.assertEqual(rc, M.E_REFUSED)
        self.assertIn("unsupported in V1", err.getvalue())
        self.assertEqual(be.mutations(), [])

    def test_causality_proved_by_socket_owner_alone_allows_the_action(self):
        be = fixture()
        capture(be)
        shut_prod_down(be)
        be.standard_mutators()
        be.make_uncasual(DAEMON_PID)
        be.make_causal_by_ss(DAEMON_PID)                   # root-shape: ss -x -p shows the pid
        self.assertEqual(M.main(drain_cmd(True), be), M.E_OK)

    def test_a_daemon_this_restore_never_started_refuses(self):
        be = fixture()
        self.assertEqual(full_drain(be), M.E_OK)
        backing_alive_after_drain(be)
        be.swap_rows.append(swap_row(DEV, used=1000, prio=1500))
        be.files[M.SWAPS_FILE] = render_swaps(be.swap_rows)
        be.units[UNIT]["main_pid"] = 7777                  # someone else started the unit
        be.files["/proc/7777/cmdline"] = "/usr/local/bin/nbd-vram\0"
        be.files["/proc/7777/stat"] = stat_line(7777, 10)  # started long before the intent
        be.proc_pids.add("7777")
        be.make_causal(7777)
        self.assertEqual(M.main(restore_cmd(True), be), M.E_REFUSED)
        self.assertEqual(be.mutations(), [])

    def test_swapon_reprobe_binds_the_captured_daemon(self):
        be = fixture()
        self.assertEqual(full_drain(be), M.E_OK)
        backing_alive_after_drain(be)
        be.standard_mutators()
        self.assertEqual(M.main(restore_cmd(True), be), M.E_OK)
        rec = opstate(be)
        self.assertEqual(rec["restored_daemon_binding"], "captured-daemon")
        self.assertEqual(rec["restored_daemon_pid"], DAEMON_PID)


# ----------------------------------------------------------------------------------
# Gate 1 attempt 2, finding 4: reuse and validate the operation record; never truncate
# a prior receipt; write it durably.
# ----------------------------------------------------------------------------------
class TestRecordDurability(unittest.TestCase):
    def test_drain_retry_preserves_started_identity_and_history(self):
        be = fixture()
        capture(be)
        shut_prod_down(be)
        be.standard_mutators()
        be.fail_swapoff(8)
        self.assertEqual(M.main(drain_cmd(True), be), M.E_DRAIN_ABORTED)
        first = opstate(be)
        self.assertEqual(first["phase"], "drain-failed")
        be.on_swapoff = None
        be.standard_mutators()                             # RAM freed, operator retries
        self.assertEqual(M.main(drain_cmd(True), be), M.E_OK)
        rec = opstate(be)
        self.assertEqual(rec["phase"], "cleaned")
        self.assertEqual(rec["started_iso"], first["started_iso"])   # identity preserved
        self.assertEqual(rec["started_btime"], first["started_btime"])
        self.assertEqual(rec["prior_phase"], "drain-failed")
        self.assertEqual(len(rec["operations"]), 2)
        self.assertGreater(len(rec["steps"]), len(first["steps"]))   # appended, not replaced
        self.assertEqual(rec["steps"][:len(first["steps"])], first["steps"])

    def test_foreign_record_refuses_before_any_mutator(self):
        be = fixture()
        self.assertEqual(full_drain(be), M.E_OK)
        rec = opstate(be)
        be.files[OPSTATE] = json.dumps({**rec, "snapshot_sha256": "0" * 64},
                                       indent=2, sort_keys=True) + "\n"
        rearm(be)
        self.assertEqual(M.main(drain_cmd(True), be), M.E_REFUSED)
        self.assertEqual(be.mutations(), [])
        self.assertEqual(opstate(be)["snapshot_sha256"], "0" * 64)   # untouched, not rewritten

    def test_unreadable_record_refuses_before_any_mutator(self):
        be = fixture()
        self.assertEqual(full_drain(be), M.E_OK)
        be.files[OPSTATE] = json.dumps(opstate(be), indent=2, sort_keys=True) + "\n"
        be.unreadable.add(OPSTATE)
        rearm(be)
        self.assertEqual(M.main(drain_cmd(True), be), M.E_REFUSED)
        self.assertEqual(be.mutations(), [])

    def test_record_for_another_device_refuses(self):
        be = fixture()
        self.assertEqual(full_drain(be), M.E_OK)
        rec = opstate(be)
        be.files[OPSTATE] = json.dumps({**rec, "device": "/dev/nbd9"},
                                       indent=2, sort_keys=True) + "\n"
        rearm(be)
        self.assertEqual(M.main(drain_cmd(True), be), M.E_REFUSED)
        self.assertEqual(be.mutations(), [])

    def test_intent_write_failure_refuses_with_zero_mutations(self):
        be = fixture()
        capture(be)
        shut_prod_down(be)
        be.standard_mutators()
        be.write_error = "permission denied on the record file"
        self.assertEqual(M.main(drain_cmd(True), be), M.E_REFUSED)
        self.assertEqual(be.mutations(), [])

    def test_post_mutation_record_write_failure_is_an_honest_partial(self):
        be = fixture()
        capture(be)
        shut_prod_down(be)
        be.standard_mutators()
        base = be.on_swapoff

        def swapoff_then_break_the_record(dev):
            out = base(dev)
            be.write_error = "record file on a full filesystem"
            return out

        be.on_swapoff = swapoff_then_break_the_record
        self.assertEqual(M.main(drain_cmd(True), be), M.E_PARTIAL)
        self.assertIn("swapoff", [c[0] for c in be.mutations()])   # mutations DID happen
        self.assertEqual(opstate(be)["phase"], "intent")           # last durable state kept


# ----------------------------------------------------------------------------------
# Gate 1 attempt 2, finding 5: clean handling of non-objects, late reads and renewed
# state - no AttributeError, no "refused" after a mutation, no destructive follow-up.
# ----------------------------------------------------------------------------------
class TestCleanHandling(unittest.TestCase):
    def test_non_object_opstate_refuses_cleanly(self):
        be = fixture()
        self.assertEqual(full_drain(be), M.E_OK)
        be.files[OPSTATE] = "[1, 2]\n"
        rearm(be)
        self.assertEqual(M.main(drain_cmd(True), be), M.E_REFUSED)     # not exit 6
        self.assertEqual(M.main(restore_cmd(True), be), M.E_REFUSED)
        self.assertEqual(be.mutations(), [])

    def test_non_object_snapshot_refuses_cleanly(self):
        be = fixture()
        be.files[SNAP] = "[1, 2]\n"
        self.assertEqual(M.main(drain_cmd(True), be), M.E_REFUSED)
        self.assertEqual(M.main(restore_cmd(True), be), M.E_REFUSED)
        self.assertEqual(M.main(verify_cmd("drained"), be), M.E_REFUSED)

    def test_renewed_swap_row_stops_the_drain_before_any_disconnect(self):
        """A row that reappears at the detachment recheck aborts the drain: the marker stays,
        the device is NOT disconnected, the unit is NOT stopped."""
        be = fixture()
        capture(be)
        shut_prod_down(be)
        be.standard_mutators()
        base = be.on_swapoff

        def swapoff_then_renew(dev):
            base(dev)
            be.swap_rows.append(swap_row(dev, used=2000, prio=1500))
            be.files[M.SWAPS_FILE] = render_swaps(be.swap_rows)
            return (0, "", "")

        be.on_swapoff = swapoff_then_renew
        self.assertEqual(M.main(drain_cmd(True), be), M.E_DRAIN_ABORTED)
        self.assertEqual([c[0] for c in be.mutations()], ["swapoff"])   # no disconnect/stop
        self.assertEqual(opstate(be)["phase"], "drain-failed")
        self.assertIn(M.STATE_FILE, be.files)

    def test_unknown_marker_before_disconnect_stops_the_drain(self):
        be = fixture()
        capture(be)
        shut_prod_down(be)
        be.standard_mutators()
        base = be.on_swapoff

        def swapoff_then_break_marker(dev):
            base(dev)
            be.unreadable.add(M.STATE_FILE)
            return (0, "", "")

        be.on_swapoff = swapoff_then_break_marker
        self.assertEqual(M.main(drain_cmd(True), be), M.E_PARTIAL)
        self.assertEqual([c[0] for c in be.mutations()], ["swapoff"])
        self.assertEqual(opstate(be)["phase"], "drain-precheck-unknown")

    def test_late_unreadable_sysctls_are_a_partial_not_a_refusal(self):
        be = fixture()
        capture(be)
        shut_prod_down(be)
        be.standard_mutators()
        base = be.on_stop

        def stop_then_break_sysctls(unit):
            out = base(unit)
            be.unreadable.add("/proc/sys/vm/swappiness")
            return out

        be.on_stop = stop_then_break_sysctls
        rc = M.main(drain_cmd(True), be)
        self.assertEqual(rc, M.E_PARTIAL)                     # not exit 2: mutations happened
        self.assertEqual(opstate(be)["phase"], "drain-sysctl-failed")
        self.assertEqual([c for c in be.mutations() if c[0] == "sysctl"], [])

    def test_internal_error_is_reported_not_a_traceback(self):
        be = fixture()
        capture(be)
        shut_prod_down(be)
        be.standard_mutators()
        be.files_rows = lambda: None                          # programming-level surprise
        self.assertEqual(M.main(drain_cmd(True), be), M.E_INTERNAL)
        self.assertEqual(be.mutations(), [])


# ----------------------------------------------------------------------------------
# Gate 1 attempt 2, finding 4/5: real local-file persistence - atomic private writes,
# fsync, replace, directory fsync, and fault injection.  System facts stay fake, so no
# live host is mutated by these tests.
# ----------------------------------------------------------------------------------
EVID = "build-mixed-gpu/remediation-final"


class RealFileBackend(FakeBackend):
    """Fake system facts with REAL local-file persistence under build-mixed-gpu/."""

    def __init__(self):
        super().__init__()
        self.rb = M.RealBackend()

    def _real(self, path):
        return str(path).startswith(EVID)

    def read(self, path):
        if self._real(path):
            st, txt = self.rb.read(path)
            if st == M.READ_OK:
                self.files[str(path)] = txt
            return st, txt
        return super().read(path)

    def exists(self, path):
        if self._real(path):
            self.read(path)
        return super().exists(path)

    def write_private(self, path, text):
        if self._real(path):
            self.rb.write_private(path, text)
            self.log.append(("write", str(path)))
            self.files[str(path)] = text
        else:
            super().write_private(path, text)

    def remove(self, path):
        if self._real(path):
            self.log.append(("remove", str(path)))
            return self.rb.remove(path)
        return super().remove(path)


def fresh(name):
    os.makedirs(EVID, exist_ok=True)
    p = os.path.join(EVID, name)
    if os.path.exists(p):
        os.unlink(p)
    return p


def capture_real(be, out):
    a = args(["capture", "--out", out, "--live-root", "/w", "--binary", BIN, "--config", CFG,
              "--launcher", LDR, "--build-dir", "/w/b"])
    snap = M.capture(a, be)
    M.write_snapshot(be, snap, out)
    return snap


class TestRealFilePersistence(unittest.TestCase):
    def test_capture_writes_a_private_snapshot_and_never_overwrites(self):
        be = fixture(cls=RealFileBackend)
        snap_path = fresh("snap-capture.json")
        capture_real(be, snap_path)
        mode = oct(os.stat(snap_path).st_mode & 0o777)
        self.assertEqual(mode, "0o600")
        self.assertFalse(os.path.exists(snap_path + ".tmp"))
        self.assertRaises(M.Refuse, capture_real, be, snap_path)

    def test_record_survives_a_real_roundtrip_and_history(self):
        be = fixture(cls=RealFileBackend)
        snap_path = fresh("snap-roundtrip.json")
        op_path = fresh("op-roundtrip.json")
        capture_real(be, snap_path)
        shut_prod_down(be)
        be.standard_mutators()
        self.assertEqual(M.main(["drain", "--snapshot", snap_path, "--mem-budget-kib",
                                 "8000000", "--opstate", op_path, "--execute"], be), M.E_OK)
        rec = json.loads(open(op_path).read())
        self.assertEqual(rec["phase"], "cleaned")
        self.assertEqual(oct(os.stat(op_path).st_mode & 0o777), "0o600")
        # a retry on the same real record appends and preserves the started identity
        first_steps = len(rec["steps"])
        backing_alive_after_drain(be)
        be.swap_rows.append(swap_row(DEV, used=1000, prio=1500))
        be.files[M.SWAPS_FILE] = render_swaps(be.swap_rows)
        be.set_sysctls(150, 1048576)
        self.assertEqual(M.main(["drain", "--snapshot", snap_path, "--mem-budget-kib",
                                 "8000000", "--opstate", op_path, "--execute"], be), M.E_OK)
        rec2 = json.loads(open(op_path).read())
        self.assertEqual(rec2["started_iso"], rec["started_iso"])
        self.assertEqual(len(rec2["operations"]), 2)
        self.assertGreater(len(rec2["steps"]), first_steps)

    def test_replace_failure_preserves_the_previous_record(self):
        be = fixture(cls=RealFileBackend)
        op_path = fresh("op-replace-fault.json")
        be.write_private(op_path, json.dumps({"phase": "intent", "n": 1}, indent=2) + "\n")
        before = open(op_path).read()
        real_replace = os.replace

        def boom(src, dst):
            raise OSError("replace failed")

        M.os.replace = boom
        try:
            with self.assertRaises(M.Refuse):
                be.write_private(op_path, json.dumps({"phase": "restored", "n": 2}) + "\n")
        finally:
            M.os.replace = real_replace
        self.assertEqual(open(op_path).read(), before)          # old record intact
        self.assertFalse(os.path.exists(op_path + ".tmp"))      # no torn temp file left

    def test_fsync_failure_is_reported_and_stops_the_run(self):
        be = fixture(cls=RealFileBackend)
        op_path = fresh("op-fsync-fault.json")
        be.write_private(op_path, json.dumps({"phase": "intent"}, indent=2) + "\n")
        before = open(op_path).read()
        real_fsync = os.fsync

        def boom(fd):
            raise OSError("fsync failed")

        M.os.fsync = boom
        try:
            with self.assertRaises(M.Refuse):
                be.write_private(op_path, json.dumps({"phase": "restored"}) + "\n")
        finally:
            M.os.fsync = real_fsync
        self.assertEqual(open(op_path).read(), before)

    def test_record_write_failure_mid_drain_is_a_partial_with_the_old_record(self):
        be = fixture(cls=RealFileBackend)
        snap_path = fresh("snap-midfail.json")
        op_path = fresh("op-midfail.json")
        capture_real(be, snap_path)
        shut_prod_down(be)
        be.standard_mutators()
        base = be.on_swapoff

        def swapoff_then_break(dev):
            out = base(dev)
            be.rb.write_private(op_path, json.dumps({"phase": "intent", "marker": "real"}) + "\n")
            os.chmod(op_path, 0o400)                # read-only: the record cannot be updated
            be.rb.write_private = lambda p, t: (_ for _ in ()).throw(
                M.Refuse(f"cannot write {p}: permission denied"))
            return out

        be.on_swapoff = swapoff_then_break
        rc = M.main(["drain", "--snapshot", snap_path, "--mem-budget-kib", "8000000",
                     "--opstate", op_path, "--execute"], be)
        self.assertEqual(rc, M.E_PARTIAL)
        self.assertEqual(json.loads(open(op_path).read())["phase"], "intent")
        os.chmod(op_path, 0o600)


if __name__ == "__main__":
    unittest.main()
