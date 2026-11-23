#!/usr/bin/env python3
"""faultdiff_run.py - boot, watch, load and stop ONE fault-track leg.

Why this exists: the fault track (plan/measurement-contract-speed-attribution-faults.md,
Phase 4) needs its own boots, separate from the speed windows, on the promoted prod rig with
ONE variable changed. port_ab_boot.sh cannot boot these: it re-derives its config from
port_ab_configs.py and refuses anything that is not the Phase-2 manifest pin. This script
boots the config faultdiff_configs.py froze, and nothing else.

Design, and why (the serving-lane rules):
  * No supervisor. A fault boot must be OBSERVED: pair_supervisor.py heals a dead engine,
    which would hide the fault's first occurrence. The server's own restart-on-next-request
    behavior stays (that is the product), and the engine log keeps the exit.
  * Kill logic lives here, on disk: --stop TERMs recorded pids after checking /proc identity
    (cmdline + start time), never a pattern sweep. Boot and stop are separate invocations.
  * The boot is the prod launcher's own sequence (helper first, wait for 'listening on' and
    both sockets, then serve/server.py), with the leg's config, sockets, log and port.
  * Stage-thread affinity (3,5,7,9,11,13,15) is re-applied after attach with
    tools/mixed_gpu/staging_affinity.py - the same move the pair supervisor makes - because
    an unpinned leg is not the rig the fault hits.
  * Gates: prerequisite 0, no live prod pair, no other /dev/kfd user, free port, no stale
    socket, empty cache dir. A gate that fails refuses the boot; it is never waived silently
    (the two waivers are the operator's, recorded in the boot record).

Static-only validation so far: py_compile and --dry-run. No boot, no GPU, no process control
was exercised from this file while it was written (fixer lane rule).
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
ROOT = pathlib.Path("/mnt/srcds/Strata")
sys.path.insert(0, str(HERE))
import faultdiff_configs as fc

PY = str(ROOT / ".venv" / "bin" / "python")
PREREQ0 = str(HERE / "port_ab_prereq0.py")
STAGE_AFFINITY = str(ROOT / "tools" / "mixed_gpu" / "staging_affinity.py")
STAGE_CPUS = "3,5,7,9,11,13,15"

# The promoted prod launcher's helper command line (launch-prod-ud-iq3xxs-dflash-w4-q4_0-
# mixed.sh:111-116), unchanged: fill-mib 0 = the helper's own free-VRAM budget governs
# (measured budget 7436.4 MiB; the 7400 cap stopped it at 3861 slots), reserve-mib 0 is
# honoured literally. The fill report is authoritative and every boot records it, so legs
# stay comparable (its residual is the co-variate the contract names).
HELPER_ARGV = [
    "--pack", "/mnt/srcds/ai/iq3xxs-ud",
    "--experts", "../Qwen3.8-Flash-Next-UD-IQ3_XXS-00002-of-00004.gguf",
    "--device", "0", "--slots", "4600", "--reserve-mib", "0",
    "--timeout", "63115200000", "--begin-timeout", "63115200000",
    "--fill-mib", "0",
]
HELPER_ENV = {"STRATA_HELPER_REPLY_TIMEOUT_MS": "600000",
              "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
              "HOME": "/home/srcds", "TERM": "dumb"}
SERVER_ENV_KEEP = ("PATH", "HOME", "TERM", "LANG", "LC_ALL", "TZ")
ROCM_LIBS = "/home/srcds/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib"

# The fault classes, in the harness's own words (port_ab_legs.FAULT_CLASS) plus the engine's.
KERNEL_FAULT = (r"amdgpu .*(reset|BACO|hang|timeout|GPU reset)|KFD.*(quiesce|reset)|"
                r"device wedged|ring .* timeout|Memory access fault by GPU|"
                r"permission fault|XDMA")
ENGINE_FAULT = r"Memory access fault by GPU node-|non-finite logits|HIP error|hipError"


def say(text):
    print("faultdiff: " + text, flush=True)


def die(text, code=1):
    print("faultdiff: REFUSING: " + text, file=sys.stderr, flush=True)
    raise SystemExit(code)


def sha256(path):
    return fc.sha256_path(path)


def proc_cmd(pid):
    try:
        return (pathlib.Path("/proc") / str(pid) / "cmdline").read_bytes().replace(b"\0", b" ").decode(
            "utf-8", "replace").strip()
    except OSError:
        return ""


def proc_start(pid):
    """The thread's start time in clock ticks - the identity check against pid reuse."""
    try:
        text = (pathlib.Path("/proc") / str(pid) / "stat").read_text()
        return text.rsplit(") ", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def alive(pid, start=None):
    if not pid or not (pathlib.Path("/proc") / str(pid)).is_dir():
        return False
    if start is not None and proc_start(pid) != start:
        return False
    return True


def state_path(leg, out):
    return pathlib.Path(out) / "run" / (leg + ".state.json")


def read_state(leg, out):
    p = state_path(leg, out)
    if not p.is_file():
        die("no boot record for " + leg + " (" + str(p) + "): was it booted?")
    return json.loads(p.read_text())


def write_state(leg, out, record):
    p = state_path(leg, out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n")


def load(leg, out):
    """The frozen config and paths of one leg, with every pin re-checked."""
    cfg, paths = fc.derive(leg, out)
    problems = fc.verify(leg, out)
    if problems:
        die("the leg's derivation is not its declared one: " + "; ".join(problems))
    on_disk = pathlib.Path(fc.GEN / (leg + ".json"))
    if not on_disk.is_file():
        die(str(on_disk) + " missing: run faultdiff_configs.py")
    manifest = json.loads((fc.GEN / "manifest.json").read_text())
    pin = manifest["legs"][leg]
    got = sha256(on_disk)
    if got != pin["sha256"]:
        die(str(on_disk) + " is " + got[:16] + ", the manifest pins " + pin["sha256"][:16]
            + ": regenerate deliberately, never edit")
    for kind in ("engine", "helper"):
        path = ROOT / fc.PINS[kind]
        want = fc.PINS[kind + "_sha256"]
        if not path.is_file():
            die("missing " + kind + ": " + str(path))
        if sha256(path) != want:
            die(kind + " " + str(path) + " is " + sha256(path)[:16] + ", the rig pins "
                + want[:16])
    if sha256(fc.BASE) != manifest["base_sha256"]:
        die("the base config " + str(fc.BASE) + " changed since the legs were frozen")
    return cfg, paths, manifest


# ------------------------------------------------------------------------------- the gates
def prod_guard():
    """No prod pair may be live during a fault boot (the fault legs are the only kfd user)."""
    live = []
    for st in sorted((ROOT / "run").glob("prod*-supervisor.json")):
        if "affinity" in st.name or "retry" in st.name:
            continue
        try:
            d = json.loads(st.read_text())
        except (OSError, ValueError):
            continue
        if d.get("state") == "stopped":
            continue
        for key in ("supervisor_pid", "launcher_pid", "helper_pid", "server_pid"):
            pid = d.get(key)
            if isinstance(pid, int) and alive(pid):
                live.append((st.name, key, pid))
    if live:
        die("prod looks live: " + ", ".join(n + ":" + k + "=" + str(p) for n, k, p in live)
            + ". Stop prod first (separate invocation).")


def kfd_users(exclude=()):
    """Pids holding /dev/kfd, from fuser (the repro README's exclusivity probe)."""
    try:
        proc = subprocess.run(["fuser", "/dev/kfd"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None, "fuser unavailable"
    pids = [int(x) for x in re.findall(r"\d+", proc.stdout + " " + proc.stderr)]
    pids = [p for p in pids if p not in exclude]
    return pids, ""


def rocm_pids():
    tool = "/opt/rocm-6.1.0/bin/rocm-smi"
    if not pathlib.Path(tool).is_file():
        return None, "rocm-smi (6.1.0) unavailable"
    try:
        proc = subprocess.run([tool, "--showpids"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None, "rocm-smi unavailable"
    return proc.stdout, ""


def meminfo():
    out = {}
    for line in pathlib.Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        if key in ("MemAvailable", "MemTotal", "SwapTotal", "SwapFree", "Cached"):
            out[key] = int(value.split()[0])
    return out


def free_gib(path):
    """Free space on the filesystem the leg's cache dir will be written to (first existing parent)."""
    probe = pathlib.Path(path)
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        return round(shutil.disk_usage(probe).free / (1 << 30), 1), str(probe)
    except OSError:
        return None, str(probe)


def port_taken(port):
    try:
        proc = subprocess.run(["ss", "-H", "-ltn", "sport = :" + str(port)],
                              capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None, "ss unavailable"
    return bool(proc.stdout.strip()), ""


def gate_checks(cfg, paths, out, waive_exclusivity=False):
    """The read-only pre-boot state every fault boot needs, collected into one dict."""
    report = {"checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "failures": []}
    mi = meminfo()
    report["mem"] = {"available_gib": round(mi.get("MemAvailable", 0) / (1 << 20), 2),
                     "total_gib": round(mi.get("MemTotal", 0) / (1 << 20), 1),
                     "swap_used_gib": round((mi.get("SwapTotal", 0) - mi.get("SwapFree", 0)) / (1 << 20), 2),
                     "cached_gib": round(mi.get("Cached", 0) / (1 << 20), 2)}
    me = os.getpid()
    pids, why = kfd_users(exclude=(me,))
    report["kfd"] = {"pids": pids, "probe": why or "fuser /dev/kfd", "waived": False}
    if pids:
        if waive_exclusivity:
            report["kfd"]["waived"] = "operator (--waive-exclusivity)"
        else:
            report["failures"].append("/dev/kfd is held by pid(s) "
                                      + ", ".join(map(str, pids))
                                      + ": the fault leg must be the only kfd user")
    text, why = rocm_pids()
    report["rocm_smi_pids"] = {"output": text if text is not None else why, "available": text is not None}
    taken, why = port_taken(cfg["port"])
    report["port"] = {"port": cfg["port"], "taken": taken, "probe": why or "ss -ltn"}
    if taken:
        report["failures"].append("port " + str(cfg["port"]) + " is occupied")
    elif taken is None:
        report["failures"].append("cannot prove port " + str(cfg["port"]) + " free (" + why + ")")
    for sock in (paths["socket"], paths["socket2"]):
        if pathlib.Path(sock).exists() or pathlib.Path(sock).is_symlink():
            report["failures"].append("stale socket " + sock)
    gib, probe = free_gib(paths["cache_dir"])
    report["disk"] = {"free_gib": gib, "probe": probe}
    if gib is not None and gib < 40:
        report["failures"].append("cache filesystem free " + str(gib) + " GiB < 40 GiB")
    return report


def prereq0(out, waive_nonengine):
    argv = [PY, PREREQ0, "--out", str(out), "--cache-dir", str(out)]
    if waive_nonengine:
        argv.append("--waive-nonengine")
    say("prerequisite 0: " + " ".join(argv))
    proc = subprocess.run(argv)
    if proc.returncode != 0:
        die("prerequisite 0 failed (see above). Only the non-engine RSS sum gate is "
            "waivable, per invocation; memory, per-process, stage-core, serving and disk "
            "gates always refuse.")


def server_env(cfg):
    env = {k: os.environ[k] for k in SERVER_ENV_KEEP if k in os.environ}
    for drop in ("CUDA_VISIBLE_DEVICES", "STRATA_ARENA_MMAP", "GPU_PINNED_MIN_XFER_SIZE",
                 "HSA_USERPTR_FOR_PAGED_MEM", "LD_PRELOAD"):
        env.pop(drop, None)
    env["HSA_OVERRIDE_GFX_VERSION"] = "9.0.6"
    env["GPU_MAX_ALLOC_PERCENT"] = "100"
    env["STRATA_ENGINE_READY_S"] = "2400"     # prod's own value: a 256k start takes minutes
    env["LD_LIBRARY_PATH"] = ROCM_LIBS
    return env


def wait_for(path, pattern, pid, seconds, what):
    """Wait for a log line, failing at once if the process dies."""
    regex = re.compile(pattern)
    deadline = time.time() + seconds
    while time.time() < deadline:
        if pathlib.Path(path).is_file():
            text = pathlib.Path(path).read_text(errors="replace")
            if regex.search(text):
                return True
        if pid and not alive(pid):
            return False
        time.sleep(2)
    return False


def tail(path, lines=40):
    if not pathlib.Path(path).is_file():
        return "(no log)"
    data = pathlib.Path(path).read_bytes().replace(b"\0", b"").decode("utf-8", "replace")
    return "\n".join(data.splitlines()[-lines:])


def find_engine(sock):
    """The engine of THIS leg: its argv names the leg's own helper socket."""
    for entry in pathlib.Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        cmd = proc_cmd(entry.name)
        if sock in cmd and ("rpcw-hip/strata" in cmd or "strata --serve" in cmd):
            return int(entry.name), proc_start(entry.name)
    return None, None


def apply_stage_affinity(engine_pid, snapshot, attempts=10, gap=10):
    """The supervisor's move, standalone: stage threads spawn lazily, so try a few times."""
    last = {}
    for i in range(attempts):
        proc = subprocess.run([PY, STAGE_AFFINITY, "apply", "--pid", str(engine_pid),
                               "--cpus", STAGE_CPUS, "--snapshot", str(snapshot)],
                              capture_output=True, text=True)
        last = {"attempt": i + 1, "rc": proc.returncode,
                "out": (proc.stdout or "").strip()[-400:],
                "err": (proc.stderr or "").strip()[-400:]}
        if proc.returncode == 0:
            return {"state": "applied", **last, "cpus": STAGE_CPUS, "snapshot": str(snapshot)}
        if not alive(engine_pid):
            return {"state": "engine gone", **last}
        time.sleep(gap)
    return {"state": "pending", **last, "cpus": STAGE_CPUS, "snapshot": str(snapshot)}


def cmd_boot(leg, out, waive_nonengine, waive_exclusivity, dry):
    if pathlib.Path(out).resolve() != pathlib.Path(fc.OUT_DEFAULT).resolve():
        die("the legs are frozen for " + str(fc.OUT_DEFAULT) + " (their log, socket and cache "
            "paths are in the configs); a different window needs a deliberate regeneration")
    cfg, paths, _ = load(leg, out)
    out = pathlib.Path(out)
    say("leg " + leg + ": " + fc.LEGS[leg]["purpose"])
    say("config sha " + sha256(paths["frozen"])[:16] + ", engine "
        + fc.PINS["engine_sha256"][:16] + ", helper " + fc.PINS["helper_sha256"][:16])

    st = state_path(leg, out)
    if st.is_file():
        old = json.loads(st.read_text())
        for key in ("server_pid", "helper_pid"):
            pid, start = old.get(key), old.get(key + "_start")
            if alive(pid, start):
                die("leg " + leg + " looks live (" + key + " " + str(pid)
                    + "): run --stop " + leg + " first (separate invocation)")

    prod_guard()
    gate = gate_checks(cfg, paths, out, waive_exclusivity)
    say("gates: MemAvailable " + str(gate["mem"]["available_gib"]) + " GiB, swap used "
        + str(gate["mem"]["swap_used_gib"]) + " GiB, /dev/kfd "
        + ("free" if not gate["kfd"]["pids"] else "held by " + str(gate["kfd"]["pids"]))
        + ", port " + str(cfg["port"]) + " free, disk " + str(gate["disk"]["free_gib"])
        + " GiB on " + gate["disk"]["probe"])
    if gate["failures"]:
        if not dry:
            die("; ".join(gate["failures"]))
        say("this boot WOULD be refused: " + "; ".join(gate["failures"]))

    cache = paths["cache_dir"] if "--conversation-cache-dir" in cfg["args"] else None
    say("cache dir: " + (cache or "<none: parking off, no disk tier>"))
    if dry:
        say("dry run: nothing is started. Boot would run:")
        say("  prereq0 " + " ".join([PREREQ0, "--out", str(out), "--cache-dir", str(out)]))
        say("  helper " + " ".join([str(ROOT / fc.PINS["helper"])] + HELPER_ARGV
                                   + ["--socket", paths["socket"], "--socket2", paths["socket2"]]))
        say("  server " + " ".join([PY, str(ROOT / fc.PINS["server_tree"] / "serve" / "server.py"),
                                    "--engine", "strata", "--config", paths["config"],
                                    "--port", str(cfg["port"])]))
        say("  then wait for the server's 'ready: http://' line, apply stage cpus " + STAGE_CPUS
            + " and record the boot")
        return 1 if gate["failures"] else 0

    prereq0(out, waive_nonengine)
    for d in ("logs", "run", "configs", "records"):
        (out / d).mkdir(parents=True, exist_ok=True)

    # The booted config is a copy of the frozen file, byte-identical to the manifest pin. The
    # server writes its shared-settings sidecar beside the config, so it boots from the window
    # and never from gen/faultdiff/ (a frozen directory with runtime droppings is not frozen).
    cfg_path = pathlib.Path(paths["config"])
    cfg_path.write_bytes(pathlib.Path(paths["frozen"]).read_bytes())
    pin = json.loads((fc.GEN / "manifest.json").read_text())["legs"][leg]["sha256"]
    if sha256(cfg_path) != pin:
        die("the written config " + str(cfg_path) + " does not hash to the manifest pin")
    if cache:
        shutil.rmtree(cache, ignore_errors=True)
        pathlib.Path(cache).mkdir(parents=True, exist_ok=True)
        if any(pathlib.Path(cache).iterdir()):
            die("cache dir not empty after clear: " + cache)

    started_epoch = time.time()
    rec = {
        "leg": leg, "window": fc.WINDOW, "out": str(out),
        "config": paths["config"], "config_sha256": sha256(paths["config"]),
        "engine": str(ROOT / fc.PINS["engine"]), "engine_sha256": fc.PINS["engine_sha256"],
        "helper": str(ROOT / fc.PINS["helper"]), "helper_sha256": fc.PINS["helper_sha256"],
        "server_tree": str(ROOT / fc.PINS["server_tree"]), "port": cfg["port"],
        "sockets": [paths["socket"], paths["socket2"]], "cache_dir": cache,
        "cache_empty_at_boot": (not cache) or not any(pathlib.Path(cache).iterdir()),
        "engine_log": paths["engine_log"], "helper_log": paths["helper_log"],
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "started_epoch": started_epoch,
        "loadavg": pathlib.Path("/proc/loadavg").read_text().split()[:3],
        "mem_at_boot": gate["mem"], "disk_at_boot": gate["disk"], "gates": gate,
        "hsa_xnack": (cfg.get("env") or {}).get("HSA_XNACK", "<unset: runtime default>"),
        "gpu": cfg.get("gpu"), "stage_cpus": STAGE_CPUS,
        "helper_argv": HELPER_ARGV + ["--socket", paths["socket"], "--socket2", paths["socket2"]],
        "helper_env": HELPER_ENV,
    }

    helper_log = open(paths["helper_log"], "ab", buffering=0)
    helper_argv = ([str(ROOT / fc.PINS["helper"])] + HELPER_ARGV
                   + ["--socket", paths["socket"], "--socket2", paths["socket2"]])
    say("starting the helper: " + paths["helper_log"])
    helper = subprocess.Popen(helper_argv, env=HELPER_ENV, cwd="/mnt/srcds/ai/iq3xxs-ud",
                              stdin=subprocess.DEVNULL, stdout=helper_log,
                              stderr=subprocess.STDOUT, start_new_session=True)
    rec["helper_pid"], rec["helper_start"] = helper.pid, proc_start(helper.pid)
    write_state(leg, out, rec)
    if not wait_for(paths["helper_log"], r"expert-helper: listening on", helper.pid, 300,
                    "helper listening"):
        die("helper did not say 'listening on' (see " + paths["helper_log"] + "); run --stop "
            + leg + " to clean up\n" + tail(paths["helper_log"]))
    for _ in range(60):
        if pathlib.Path(paths["socket"]).is_socket() and pathlib.Path(paths["socket2"]).is_socket():
            break
        if not alive(helper.pid):
            die("helper exited while opening its second channel\n" + tail(paths["helper_log"]))
        time.sleep(1)
    else:
        die("second channel socket missing: " + paths["socket2"] + "; run --stop " + leg
            + " to clean up")

    boot_log = open(paths["boot_log"], "ab", buffering=0)
    server_argv = [PY, str(ROOT / fc.PINS["server_tree"] / "serve" / "server.py"), "--engine",
                   "strata", "--config", paths["config"], "--port", str(cfg["port"])]
    say("starting the server: " + paths["boot_log"] + " (port " + str(cfg["port"]) + ")")
    server = subprocess.Popen(server_argv, env=server_env(cfg),
                              cwd=str(ROOT / fc.PINS["server_tree"]),
                              stdin=subprocess.DEVNULL, stdout=boot_log,
                              stderr=subprocess.STDOUT, start_new_session=True)
    rec["server_pid"], rec["server_start"] = server.pid, proc_start(server.pid)
    write_state(leg, out, rec)

    say("waiting for the engine (up to 40 min; 'still starting' lines are normal)")
    if not wait_for(paths["boot_log"], r"^ready: http://", server.pid, 2400, "server ready"):
        print(tail(paths["boot_log"]), file=sys.stderr)
        print(tail(paths["engine_log"]), file=sys.stderr)
        die("the server did not reach its ready line (see " + paths["boot_log"] + " and "
            + paths["engine_log"] + "); run --stop " + leg + " to clean up")

    engine_pid, engine_start = find_engine(paths["socket"])
    rec["engine_pid"], rec["engine_start"] = engine_pid, engine_start
    rec["ready_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    rec["ready_epoch"] = time.time()
    rec["boot_s"] = round(rec["ready_epoch"] - started_epoch, 1)
    if engine_pid:
        say("engine pid " + str(engine_pid) + "; applying stage cpus " + STAGE_CPUS)
        snapshot = state_path(leg, out).with_name(leg + "-affinity.json")
        snapshot.unlink(missing_ok=True)          # staging_affinity apply creates it exclusively
        rec["stage_affinity"] = apply_stage_affinity(engine_pid, snapshot)
    else:
        rec["stage_affinity"] = {"state": "engine not found by socket"}
    write_state(leg, out, rec)

    expected = boot_lines(paths, fc.LEGS[leg])
    print_boot_lines(paths, expected)
    say("READY in " + str(rec["boot_s"]) + " s. Serve: http://127.0.0.1:" + str(cfg["port"])
        + "/ ; load: --load " + leg + "; stop: --stop " + leg + " (separate invocation)")
    return 0


def boot_lines(paths, spec):
    """Which expected lines are present/absent in this leg's logs (never invented)."""
    engine = pathlib.Path(paths["engine_log"]).read_text(errors="replace") if \
        pathlib.Path(paths["engine_log"]).is_file() else ""
    boot = pathlib.Path(paths["boot_log"]).read_text(errors="replace") if \
        pathlib.Path(paths["boot_log"]).is_file() else ""
    text = engine + "\n" + boot
    out = {"present": {}, "absent": {}}
    for pattern in spec["present"]:
        out["present"][pattern] = bool(re.search(pattern, text, re.M))
    for pattern in spec["absent"]:
        out["absent"][pattern] = not re.search(pattern, text, re.M)
    return out


def print_boot_lines(paths, report):
    for pattern, ok in report["present"].items():
        say(("  OK      " if ok else "  MISSING ") + "present: " + pattern)
    for pattern, ok in report["absent"].items():
        say(("  OK      " if ok else "  PRESENT ") + "absent : " + pattern)
    text = pathlib.Path(paths["engine_log"]).read_text(errors="replace") if \
        pathlib.Path(paths["engine_log"]).is_file() else ""
    for line in text.splitlines():
        if line.startswith("INFO logit_bias=") or "expert-helper proxy:" in line \
                or "conversation cache: disk tier" in line or "file page-cache release" in line \
                or line.startswith("dflash: "):
            say("  " + line[:220])


def fingerprint(since, until=None):
    """The passive fingerprint: kernel slice + the engine's own fault lines. Read-only."""
    until = until or time.time()
    try:
        proc = subprocess.run(["journalctl", "-k", "--since", "@" + str(int(since)),
                               "--until", "@" + str(int(until)), "--no-pager", "-q",
                               "-o", "cat"], capture_output=True, text=True, timeout=30)
        matches = [l for l in proc.stdout.splitlines() if re.search(KERNEL_FAULT, l)]
        kernel = {"available": proc.returncode == 0, "matches": matches[-20:], "count": len(matches)}
    except (OSError, subprocess.SubprocessError) as error:
        kernel = {"available": False, "reason": str(error), "matches": [], "count": 0}
    return {"since": since, "until": until, "kernel": kernel}


def cmd_status(leg, out, as_json):
    rec = read_state(leg, out)
    cfg, paths, _ = load(leg, out)
    engine_log = pathlib.Path(paths["engine_log"])
    text = engine_log.read_bytes().replace(b"\0", b"").decode("utf-8", "replace") if \
        engine_log.is_file() else ""
    engine_alive = alive(rec.get("engine_pid"), rec.get("engine_start"))
    server_alive = alive(rec.get("server_pid"), rec.get("server_start"))
    helper_alive = alive(rec.get("helper_pid"), rec.get("helper_start"))
    faults = [l for l in text.splitlines() if re.search(ENGINE_FAULT, l)]
    lines = {
        "parks": len(re.findall(r"conversation cache: parked ", text)),
        "disk_parks": len(re.findall(r"disk park streamed", text)),
        "pages_released": [int(x) for x in re.findall(r"pages-released=(\d+)", text)][-8:],
        "judge": re.findall(r"drafts accepted (\d+) of (\d+)", text)[-4:],
    }
    fp = fingerprint(rec.get("started_epoch") or time.time())
    report = {"leg": leg, "alive": {"engine": engine_alive, "server": server_alive,
                                    "helper": helper_alive},
              "engine_pid": rec.get("engine_pid"),
              "uptime_s": round(time.time() - (rec.get("started_epoch") or time.time()), 1),
              "hsa_xnack": rec.get("hsa_xnack"), "engine_fault_lines": faults[-10:],
              "engine_fault_count": len(faults), "log_counts": lines,
              "kernel_fingerprint": fp, "expected": boot_lines(paths, fc.LEGS[leg])}
    if as_json:
        print(json.dumps(report, indent=1, sort_keys=True))
        return 0
    say(leg + ": engine " + ("alive" if engine_alive else "DOWN") + ", server "
        + ("alive" if server_alive else "DOWN") + ", helper " + ("alive" if helper_alive else "DOWN")
        + "; uptime " + str(report["uptime_s"]) + " s; HSA_XNACK "
        + str(rec.get("hsa_xnack")))
    say("engine fault lines: " + (str(len(faults)) if faults else "none"))
    for line in faults[-5:]:
        say("  " + line[:220])
    say("kernel: " + str(fp["kernel"]["count"]) + " fault-class line(s)"
        + ("" if fp["kernel"]["available"] else " (journal unavailable: "
           + str(fp["kernel"].get("reason", "")) + ")"))
    for line in fp["kernel"]["matches"][-8:]:
        say("  " + line[:220])
    say("parks " + str(lines["parks"]) + ", disk parks " + str(lines["disk_parks"])
        + ", pages-released (last) " + str(lines["pages_released"]))
    say("drafts accepted of offered (last): " + str(lines["judge"]))
    return 0


def cmd_stop(leg, out):
    rec = read_state(leg, out)
    cfg, paths, _ = load(leg, out)

    def term(pid, start, what):
        if not alive(pid, start):
            say(what + " " + str(pid) + " already gone")
            return
        cmd = proc_cmd(pid)
        if str(ROOT) not in cmd and "server.py" not in cmd:
            say(what + " " + str(pid) + " was recycled to another program; leaving it alone")
            return
        say("TERM " + what + " " + str(pid))
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return
        for _ in range(60):
            if not alive(pid, start):
                return
            time.sleep(1)
        say(what + " " + str(pid) + " did not exit in 60 s")

    term(rec.get("server_pid"), rec.get("server_start"), "server")
    engine_pid, engine_start = rec.get("engine_pid"), rec.get("engine_start")
    if engine_pid is None:
        engine_pid, engine_start = find_engine(paths["socket"])
    term(engine_pid, engine_start, "engine")
    term(rec.get("helper_pid"), rec.get("helper_start"), "helper")
    for sock in (paths["socket"], paths["socket2"]):
        pathlib.Path(sock).unlink(missing_ok=True)
    ended = time.time()
    fp = fingerprint(rec.get("started_epoch") or ended, ended)
    engine_log = pathlib.Path(paths["engine_log"])
    text = engine_log.read_bytes().replace(b"\0", b"").decode("utf-8", "replace") if \
        engine_log.is_file() else ""
    faults = [l for l in text.splitlines() if re.search(ENGINE_FAULT, l)]
    rec.update({"ended_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "ended_epoch": ended,
                "ran_s": round(ended - (rec.get("started_epoch") or ended), 1),
                "engine_fault_lines": faults[-20:],
                "engine_fault_count": len(faults),
                "kernel_fingerprint": fp,
                "expected": boot_lines(paths, fc.LEGS[leg])})
    write_state(leg, out, rec)
    say("stopped " + leg + " after " + str(rec["ran_s"]) + " s; engine fault lines "
        + str(len(faults)) + ", kernel fault-class lines " + str(fp["kernel"]["count"]))
    say("record: " + str(state_path(leg, out)))
    left = [p for p in (rec.get("server_pid"), engine_pid, rec.get("helper_pid"))
            if alive(p)]
    if left:
        say("NOT QUIET: still alive " + str(left) + " - inspect before restarting anything")
        return 1
    say("QUIET")
    return 0


def cmd_load(leg, out, minutes, ctx_pair):
    """Sequential serving load (one request in flight, the operator's no-concurrency rule):
    two conversations, alternating, so every switch parks and restores. NOT a measurement."""
    rec = read_state(leg, out)
    cfg, paths, _ = load(leg, out)
    if not alive(rec.get("server_pid"), rec.get("server_start")):
        die("the leg is not serving: boot it first")
    engine_pid = rec.get("engine_pid") if alive(rec.get("engine_pid"), rec.get("engine_start")) \
        else find_engine(paths["socket"])[0]
    if not engine_pid:
        die("no engine pid: the server may be restarting the engine")
    records = pathlib.Path(paths["records"]) / "load"
    records.mkdir(parents=True, exist_ok=True)
    url = "http://127.0.0.1:" + str(cfg["port"]) + "/v1/chat/completions"
    a_ctx, b_ctx = ctx_pair
    deadline = time.time() + minutes * 60
    n = 0
    while time.time() < deadline:
        ctx = a_ctx if n % 2 == 0 else b_ctx
        n += 1
        argv = [PY, str(HERE / "ab9_driver.py"), "--arm", "faultdiff",
                "--leg", leg + "-n" + str(n), "--kind", "tg", "--ctx", str(ctx),
                "--max-tokens", "1024", "--out", str(records),
                "--elog", paths["engine_log"], "--hlog", paths["helper_log"],
                "--url", url, "--engine-pid", str(engine_pid)]
        say("load request " + str(n) + " (ctx " + str(ctx) + ") at "
            + time.strftime("%H:%M:%S"))
        rc = subprocess.run(argv).returncode
        if rc != 0:
            say("load request " + str(n) + " returned " + str(rc)
                + " - the engine may have faulted; run --status " + leg)
            if not alive(engine_pid):
                return 1
    say("load done: " + str(n) + " request(s); records in " + str(records))
    return 0


def main():
    ap = argparse.ArgumentParser(description="boot/watch/load/stop one fault-track leg")
    ap.add_argument("--leg", required=True, choices=list(fc.LEG_ORDER))
    ap.add_argument("--out-dir", type=pathlib.Path, default=fc.OUT_DEFAULT)
    ap.add_argument("--boot", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--load", action="store_true")
    ap.add_argument("--minutes", type=float, default=30, help="--load wall time (default 30)")
    ap.add_argument("--ctx-pair", default=None,
                    help="--load: the two conversation sizes, alternating (default 80000,16000; "
                         "parking-off defaults to 80000,80000 - one conversation, because a "
                         "switch without the conversation cache re-prefills the whole prompt)")
    ap.add_argument("--json", action="store_true", help="--status: print the report as JSON")
    ap.add_argument("--dry-run", action="store_true", help="--boot: print the plan, start nothing")
    ap.add_argument("--waive-nonengine", action="store_true",
                    help="prerequisite 0's non-engine RSS sum, per invocation (the operator's "
                         "waiver; the reading is recorded)")
    ap.add_argument("--waive-exclusivity", action="store_true",
                    help="waive the /dev/kfd exclusivity probe, per invocation (recorded)")
    a = ap.parse_args()
    if sum([a.boot, a.status, a.stop, a.load]) != 1:
        ap.error("choose exactly one of --boot, --status, --stop, --load")
    if a.boot:
        return cmd_boot(a.leg, a.out_dir, a.waive_nonengine, a.waive_exclusivity, a.dry_run)
    if a.status:
        return cmd_status(a.leg, a.out_dir, a.json)
    if a.stop:
        return cmd_stop(a.leg, a.out_dir)
    pair = a.ctx_pair or ("80000,80000" if a.leg == "parking-off" else "80000,16000")
    try:
        parts = [int(x) for x in pair.split(",")]
        if len(parts) != 2:
            raise ValueError
    except ValueError:
        ap.error("--ctx-pair takes two comma-separated token counts")
    return cmd_load(a.leg, a.out_dir, a.minutes, (parts[0], parts[1]))


if __name__ == "__main__":
    raise SystemExit(main())
