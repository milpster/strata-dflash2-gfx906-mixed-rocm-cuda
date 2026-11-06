"""CPU-only pair fixture. All files/sockets belong to the test's scratch tree."""
import ctypes
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time


mode = sys.argv[1]


def fixture_processes(scratch=None):
    """Identify only our CPU fixture roles and private test scratch paths."""
    found = {}
    logs = Path(__file__).resolve().parents[3] / "logs"
    fixture = os.fsencode(Path(__file__).resolve())
    executable = Path(sys.executable).resolve()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            # Reading a busy native engine's cmdline can wait on its mmap lock.
            # Only this Python executable can be one of our fixture roles.
            if (entry / "exe").resolve(strict=True) != executable:
                continue
            args = (entry / "cmdline").read_bytes().split(b"\0")
            if (len(args) < 3 or args[1] != fixture
                    or args[2] not in (b"helper", b"engine", b"server", b"launcher")):
                continue
            env = dict(item.split(b"=", 1) for item in (entry / "environ").read_bytes().split(b"\0") if b"=" in item)
            work = Path(os.fsdecode(env[b"FAKE_PAIR_ROOT"])).resolve()
            if work.parent != logs or not work.name.startswith("pair-test-"):
                continue
            if scratch is not None and work != Path(scratch).resolve():
                continue
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            if fields[0] != "Z":
                found[int(entry.name)] = fields[19]
        except (OSError, KeyError, IndexError, ValueError):
            continue
    return found


if mode in ("--check", "--cleanup"):
    scratch = sys.argv[2] if len(sys.argv) > 2 else None
    found = fixture_processes(scratch)
    for pid, start in found.items():
        print(f"CPU fixture still alive: PID {pid}, start {start}", flush=True)
        if mode == "--cleanup" and fixture_processes(scratch).get(pid) == start:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    if mode == "--cleanup":
        deadline = time.monotonic() + 5
        while fixture_processes(scratch) and time.monotonic() < deadline:
            time.sleep(0.05)
    raise SystemExit(bool(fixture_processes(scratch) if mode == "--cleanup" else found))

root = Path(os.environ["FAKE_PAIR_ROOT"])


def append(path, text):
    with Path(path).open("a") as log:
        log.write(text + "\n")


def publish(helper, server, helper_log):
    subprocess.run([sys.executable, os.environ["PAIR_SUPERVISOR"], "record",
                    str(helper), str(server), str(root / "server.log"), str(helper_log)], check=True)


if mode == "engine":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    libc = ctypes.CDLL(None, use_errno=True)

    def staging():
        libc.prctl(15, b"strata-stage", 0, 0, 0)
        if 0 in os.sched_getaffinity(0):  # Mirror the engine's CPU-0 staging pin.
            os.sched_setaffinity(0, {0})
        threading.Event().wait(120)

    threading.Thread(target=staging, daemon=True).start()
    channels = []
    for name in ("one.sock", "two.sock"):
        channel = socket.socket(socket.AF_UNIX)
        deadline = time.monotonic() + 5
        while True:
            try:
                channel.connect(str(root / name))
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
        channel.settimeout(5)
        channels.append(channel)
    channels[0].sendall(b"fill")
    filled = channels[0].recv(32) == b"4093"
    if filled and os.environ.get("FAKE_NO_ENGINE_ACK") != "1":
        append(root / "engine.log", "strata generate: expert-helper proxy: 4093 experts on one.sock, 6.25 GiB (fill report)")
    append(root / "engine.log", "strata generate: session is up")
    time.sleep(0.2)
    # The real engine renames itself strata-host only after early serve startup;
    # the supervisor must retry the affinity move through that window.
    libc.prctl(15, b"strata-host", 0, 0, 0)  # PR_SET_NAME
    threading.Event().wait(120)
elif mode == "helper":
    listeners = []
    for name in ("one.sock", "two.sock"):
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(root / name))
        listener.listen()
        listeners.append(listener)
    channels = [listener.accept()[0] for listener in listeners]
    helper_log = os.environ["FAKE_HELPER_LOG"]
    append(helper_log, "expert-helper: engine connected (peer uid 1000)")
    append(helper_log, "expert-helper: second-channel engine connected (peer uid 1000)")
    assert channels[0].recv(32) == b"fill"
    filled = os.environ.get("FAKE_NO_FILL") != "1"
    if filled:
        if os.environ.get("FAKE_ALT_FILL") == "1":
            append(helper_log, "expert-helper: fill stopped: budget; device free now 300 MiB of 8192 MiB (4093 slots, 6.25 GiB uploaded)")
        else:
            append(helper_log, "expert-helper: filled 4093 slots (6.25 GiB)")
    channels[0].sendall(b"4093" if filled else b"0")
    threading.Event().wait(120)
elif mode == "server":
    engine = subprocess.Popen([sys.executable, __file__, "engine"], start_new_session=True)
    temporary = root / "engine.json.new"
    temporary.write_text(json.dumps({"pid": engine.pid}))
    temporary.replace(root / "engine.json")
    if os.environ.get("FAKE_SERVER_EXIT") == "1":
        raise SystemExit(2)
    threading.Event().wait(120)
elif mode == "launcher":
    helper_log = root / f"helper-{os.getpid()}.log"
    helper_log.write_text("")
    helper = subprocess.Popen([sys.executable, __file__, "helper"],
                              env=dict(os.environ, FAKE_HELPER_LOG=str(helper_log)))
    publish(helper.pid, 0, helper_log)
    if os.environ.get("FAKE_HELPER_ONLY") == "1":
        threading.Event().wait(120)
    server = subprocess.Popen([sys.executable, __file__, "server"])
    publish(helper.pid, server.pid, helper_log)
    server.wait()
