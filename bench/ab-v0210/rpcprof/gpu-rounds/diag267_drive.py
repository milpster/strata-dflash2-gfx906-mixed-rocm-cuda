#!/usr/bin/env python3
# diag267_drive.py — speak the engine's stdin protocol (GEN ... / DONE|ERR), drive
# requests until a wedge (boot class or request class), then capture forensics and
# exec gdb on the engine (our own child -> PTRACE_ATTACH legal under yama=1).
# Before boot it sweeps orphaned 'sleep 300' cleanup watchdogs from earlier runs.
# Usage: diag267_drive.py <out_dir> [ids_json]
import json, os, queue, signal, subprocess, sys, threading, time

OUT = sys.argv[1]
os.makedirs(OUT, exist_ok=True)
CFG = "/mnt/srcds/Strata/bench/ab-v0210/quality/diag267.json"
IDS = sys.argv[2] if len(sys.argv) > 2 else "/mnt/srcds/Strata/data/wikitext2/set.ids.json"
DRV = os.path.join(OUT, "driver.log")

def log(m):
    with open(DRV, "a") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {m}\n")

cfg = json.load(open(CFG))
cmd = [cfg["exe"]] + cfg.get("args", [])
env = dict(os.environ)
env.update({k: str(v) for k, v in cfg.get("env", {}).items()})

# orphan-watchdog sweep: a leftover 'sleep 300' from an earlier driver would SIGKILL
# this run's engine up to 5 minutes in
for pid in os.listdir("/proc"):
    if not pid.isdigit():
        continue
    try:
        if open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")[0] == b"sleep 300":
            os.kill(int(pid), signal.SIGKILL)
            log(f"swept orphan watchdog sleep pid {pid}")
    except OSError:
        pass

log(f"booting engine; env={cfg.get('env')}")
errf = open(os.path.join(OUT, "engine.stderr.txt"), "w")
t0 = time.time()
p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                     stderr=errf, text=True, bufsize=1, env=env)

q: "queue.Queue[str | None]" = queue.Queue()
def reader():
    for line in p.stdout:
        q.put(line)
    q.put(None)
threading.Thread(target=reader, daemon=True).start()

def next_line(timeout):
    try:
        return q.get(timeout=timeout)
    except queue.Empty:
        return None

ready = False
while time.time() - t0 < 900:
    line = next_line(5)
    if line is None and p.poll() is not None:
        break
    if line is None:
        continue
    if line.startswith("READY"):
        ready = True
        break
log(f"ready={ready} pid={p.pid} boot={time.time()-t0:.0f}s")

segments = json.load(open(IDS))["segments"]
wedge = "boot" if not ready else "none"
if ready:
    n = 0
    while time.time() - t0 < 3300:
        n += 1
        ids = segments[(n - 1) % len(segments)]["ids"][:900]
        try:
            p.stdin.write("GEN 1 " + ",".join(map(str, ids)) + "\n")
            p.stdin.flush()
        except (BrokenPipeError, ValueError):
            wedge = "engine-gone"
            break
        done, t_req = None, time.time()
        while time.time() - t_req < 170:
            line = next_line(10)
            if line is None:
                continue
            if line.startswith(("DONE", "ERR")):
                done = line.strip()
                break
        log(f"req#{n} answer={str(done)[:70]!r} in {time.time()-t_req:.0f}s")
        if done is None:
            wedge = "request"
            break
        if p.poll() is not None:
            wedge = "engine-gone"
            break
        time.sleep(0.5)
log(f"WEDGE class={wedge} - capturing forensics")
errf.flush()

EP = p.pid
try:
    lines = []
    for t in os.listdir(f"/proc/{EP}/task"):
        try:
            comm = open(f"/proc/{EP}/task/{t}/comm").read().strip()
            st = open(f"/proc/{EP}/task/{t}/stat").read().split()[2]
            try:
                wc = open(f"/proc/{EP}/task/{t}/wchan").read().strip()
            except OSError:
                wc = "?"
            lines.append(f"{t} {comm} state={st} wchan={wc}")
        except OSError:
            pass
    kfd = ""
    kd = f"/sys/class/kfd/kfd/proc/{EP}"
    if os.path.isdir(kd):
        for f in sorted(os.listdir(kd)):
            try:
                kfd += f"{f}: {open(kd + '/' + f).read()[:200]}\n"
            except OSError:
                pass
    tail = open(os.path.join(OUT, "engine.stderr.txt"), "rb").read()[-4000:].decode(errors="replace")
    with open(os.path.join(OUT, "wedge_forensics.txt"), "w") as fh:
        fh.write(f"=== diag267 drive forensics {time.asctime()} class={wedge} pid={EP} ===\n")
        fh.write("\n".join(lines) + "\n--- kfd:\n" + kfd + "\n--- log tail:\n" + tail)
except OSError as e:
    log(f"forensics partial: {e}")

# cleanup watchdog (own child), then exec gdb: the engine becomes gdb's child
if os.fork() == 0:
    time.sleep(300)
    try:
        os.kill(EP, signal.SIGKILL)
    except OSError:
        pass
    os._exit(0)
log(f"exec gdb -> wedge_stacks.txt (pid {EP})")
errf.close()
gdb_out = open(os.path.join(OUT, "wedge_stacks.txt"), "w")
os.dup2(gdb_out.fileno(), 1)
os.dup2(gdb_out.fileno(), 2)
os.execvp("gdb", ["gdb", "-q", "-batch", "-p", str(EP),
                  "-ex", "set pagination off", "-ex", "info threads",
                  "-ex", "thread apply all bt"])
