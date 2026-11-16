#!/usr/bin/env python3
# diag267_crash.py — catch the mid-request engine DEATH red-handed. The engine runs
# as gdb's inferior from the start (gdb --args, batch): stdin/stdout speak the GEN
# protocol through a pty we own, stderr goes to a file. On SIGSEGV/SIGABRT gdb's
# batch prints the signal + all-thread backtrace to ITS stdout (captured here), the
# drive loop keeps sending GEN requests until the engine dies or the cap.
# Usage: diag267_crash.py <out_dir> [ids_json]
import json, os, pty, queue, select, shlex, signal, subprocess, sys, termios, threading, time

OUT = sys.argv[1]
os.makedirs(OUT, exist_ok=True)
CFG = "/mnt/srcds/Strata/bench/ab-v0210/quality/diag267.json"
IDS = sys.argv[2] if len(sys.argv) > 2 else "/mnt/srcds/Strata/data/quant_quality/wikitext2/set.ids.json"
DRV = os.path.join(OUT, "driver.log")
GDBLOG = os.path.join(OUT, "gdb_crash.txt")

def log(m):
    with open(DRV, "a") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {m}\n")

cfg = json.load(open(CFG))
cmd = [cfg["exe"]] + cfg.get("args", [])
env = dict(os.environ)
env.update({k: str(v) for k, v in cfg.get("env", {}).items()})
errf = os.path.join(OUT, "engine.stderr.txt")
engine_cmd = f"exec {shlex.join(cmd)} 2>>{shlex.quote(errf)}"

mfd, sfd = pty.openpty()
attrs = termios.tcgetattr(mfd)
attrs[3] &= ~termios.ECHO          # lflag: no echo
termios.tcsetattr(mfd, termios.TCSANOW, attrs)
slave = os.ttyname(sfd)

gdb_args = ["gdb", "-q", "-batch",
            "-ex", "set pagination off",
            "-ex", f"set inferior-tty {slave}",
            "-ex", "run",
            "-ex", "info threads",
            "-ex", "thread apply all bt",
            "-ex", "quit",
            "--args", "/bin/bash", "-c", engine_cmd]
log(f"booting under gdb; env={cfg.get('env')}")
t0 = time.time()
g = subprocess.Popen(gdb_args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                     stderr=subprocess.STDOUT, text=True, bufsize=1, env=env)

gq: "queue.Queue[str | None]" = queue.Queue()
def greader():
    for line in g.stdout:
        gq.put(line)
    gq.put(None)
threading.Thread(target=greader, daemon=True).start()

buf = ""
def pty_line(timeout):
    global buf
    r, _, _ = select.select([mfd], [], [], timeout)
    if not r:
        return None
    try:
        chunk = os.read(mfd, 65536)
    except OSError:
        return "EOF"
    if not chunk:
        return "EOF"
    buf += chunk.decode(errors="replace")
    if "\n" in buf:
        line, buf = buf.split("\n", 1)
        return line + "\n"
    return None

def gline(timeout):
    try:
        return gq.get(timeout=timeout)
    except Exception:
        return None

ready = False
while time.time() - t0 < 2700:
    line = pty_line(5)
    if line is not None and line.startswith("READY"):
        ready = True
        break
    if g.poll() is not None:
        break
log(f"ready={ready} gdb={g.pid} boot={time.time()-t0:.0f}s")

segments = json.load(open(IDS))["segments"]
wedge = "boot" if not ready else "none"
if ready:
    n = 0
    while time.time() - t0 < 6000 and g.poll() is None:
        n += 1
        ids = segments[(n - 1) % len(segments)]["ids"][:900]
        os.write(mfd, ("GEN 1 " + ",".join(map(str, ids)) + "\n").encode())
        done, t_req = None, time.time()
        while time.time() - t_req < 170:
            if g.poll() is not None:
                wedge = "death"
                break
            line = pty_line(10)
            if line is not None and line.startswith(("DONE", "ERR")):
                done = line.strip()
                break
        log(f"req#{n} answer={str(done)[:70]!r} in {time.time()-t_req:.0f}s alive={g.poll() is None}")
        if wedge == "death" or done is None and g.poll() is not None:
            wedge = "death"
            break
        if done is None:
            wedge = "stall-alive"
            break
        time.sleep(0.5)
log(f"OUTCOME class={wedge} — draining gdb output")
time.sleep(2)
lines = []
while True:
    l = gline(2)
    if l is None:
        break
    lines.append(l)
open(GDBLOG, "w").write("".join(lines))
tail = "".join(lines[-60:])
open(DRV, "a").write(f"[{time.strftime('%H:%M:%S')}] gdb tail:\n{tail}\n")
try:
    g.kill()
except OSError:
    pass
log("done")
