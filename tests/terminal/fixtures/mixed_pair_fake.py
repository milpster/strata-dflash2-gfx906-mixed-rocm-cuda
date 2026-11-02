"""CPU-only process fixture for the mixed pair supervisor; no engine imports."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

root = Path(os.environ["FAKE_PAIR_ROOT"])
mode = sys.argv[1]
if mode == "peer":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(120)
elif mode == "helper":
    sockets = []
    for name in ("one.sock", "two.sock"):
        sock = socket.socket(socket.AF_UNIX)
        sock.bind(str(root / name))
        sock.listen()
        sockets.append(sock)
    (root / "helper.log").write_text("expert-helper: filled 4093 slots (6.25 GiB)\nsecond-channel engine connected\n")
    time.sleep(120)
elif mode == "server":
    engine = subprocess.Popen([sys.executable, __file__, "peer"], start_new_session=True)
    (root / "engine.json").write_text(json.dumps({"pid": engine.pid}))
    time.sleep(120)
elif mode == "launcher":
    (root / "helper.log").write_text("")
    helper = subprocess.Popen([sys.executable, __file__, "helper"])
    server = subprocess.Popen([sys.executable, __file__, "server"])
    supervisor = os.environ["PAIR_SUPERVISOR"]
    subprocess.run([sys.executable, supervisor, "record", str(helper.pid), str(server.pid),
                    str(root / "engine.log"), str(root / "helper.log")], check=True)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    server.wait()
