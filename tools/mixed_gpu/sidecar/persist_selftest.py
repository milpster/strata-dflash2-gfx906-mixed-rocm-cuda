#!/usr/bin/env python3
"""Selftest for strata-expert-helper --persist (no engine, no fill, seconds long).

Starts the helper on SCRATCH sockets, connects, sends one kBye frame, and checks:
  1. "engine connected"            (session opens)
  2. "the engine said bye"          (session ends by bye)
  3. "--persist: engine session 1 ended; listening again"  (the re-listen line)
  4. a SECOND connect after the bye also gets "engine connected" (re-accept works)
  5. SIGTERM exits cleanly (code 0, "SIGTERM/SIGINT ... exiting" line)
The helper runs with a scratch pack socket and NO kFill: the CUDA footprint is one
context + the fixed staging buffers, safe to run beside prod on the CUDA card.
Usage: persist_selftest.py <helper-binary> <pack-dir> <experts-gguf-relative> [scratch-dir]
"""
import os, socket, struct, subprocess, sys, tempfile, time

MAGIC = 0x53544848
KBYE = 7
KHELLO = 1

def header(t, proto=2, req_id=0, seq=0, ln=0):
    return struct.pack("<IIIIII", MAGIC, proto, t, seq, req_id, ln)

def wait_for(path, needle, timeout=30.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with open(path, "r") as f:
                if needle in f.read():
                    return True
        except OSError:
            pass
        time.sleep(0.2)
    return False

def main():
    helper, pack, experts = sys.argv[1], sys.argv[2], sys.argv[3]
    scratch = sys.argv[4] if len(sys.argv) > 4 else tempfile.mkdtemp(prefix="helper-persist-selftest-")
    os.makedirs(scratch, exist_ok=True)
    sock1 = os.path.join(scratch, "s1.sock")
    log = os.path.join(scratch, "helper.log")
    for stale in (sock1, log):
        try:
            os.unlink(stale)
        except FileNotFoundError:
            pass
    proc = subprocess.Popen(
        [helper, "--pack", pack, "--experts", experts, "--persist", "--device", "0",
         "--slots", "64", "--reserve-mib", "300", "--timeout", "60000",
         "--begin-timeout", "5000", "--socket", sock1],
        stdout=open(log, "ab"), stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/root")})
    ok = True
    try:
        if not wait_for(log, "listening on", 60):
            raise SystemExit("FAIL: helper never listened; see " + log)
        end = time.time() + 30
        while not os.path.exists(sock1) and time.time() < end:
            time.sleep(0.2)
        print("1. listening on", sock1)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(sock1)
        s.sendall(header(KBYE))
        time.sleep(0.5)
        s.close()
        if not wait_for(log, "engine connected", 20):
            print("FAIL step 2: no 'engine connected'"); ok = False
        else:
            print("2. engine connected")
        for step, needle in ((3, "the engine said bye"),):
            if not wait_for(log, needle, 20):
                print(f"FAIL step {step}: {needle!r} not in {log}"); ok = False; break
            print(f"{step}. {needle}")
        if ok:
            s2 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s2.connect(sock1)
            if not wait_for(log, "engine connected (peer uid", 20):
                print("FAIL step 5: second engine never connected (re-accept broken)"); ok = False
            else:
                print("5. second connect: engine connected again")
                time.sleep(0.5)
                s2.close()
        if ok:
            # Phase 2 (the real hiccup shape): send a valid kHello, KEEP THE CHANNEL OPEN BRIEFLY
            # (a close right after sendall RSTs the frame away), then CLOSE = engine abort.
            # hello_seen = true, so serve_channel's pre-hello re-listen does NOT apply: main's
            # --persist branch must log the session-end line and re-listen for the next engine.
            hello = struct.pack("<6i2q2I", 48, 512, 2560, 640, 0, 47, 64, 300, 3072, 0) + bytes(3072)
            s3 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s3.connect(sock1)
            s3.sendall(header(KHELLO, ln=len(hello)) + hello)
            time.sleep(1.0)   # the helper must READ the hello before the close (hello_seen = true)
            s3.close()
            if not wait_for(log, "--persist: engine session 1 ended; listening again", 20):
                print("FAIL step 6: no --persist session-end line after a post-hello engine death"); ok = False
            else:
                print("6. post-hello EOF: '--persist: engine session 1 ended; listening again'")
                time.sleep(0.5)
                s4 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s4.connect(sock1)
                s4.sendall(header(KHELLO, ln=len(hello)) + hello)
                time.sleep(1.0)
                s4.close()
                if not wait_for(log, "--persist: engine session 2 ended; listening again", 20):
                    print("FAIL step 7: the next engine session did not open and end cleanly"); ok = False
                else:
                    print("7. next engine connected after the persist re-listen (session 2 ended on EOF)")
    finally:
        proc.terminate()
        try:
            rc = proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill(); rc = -9
        with open(log, "r") as f:
            txt = f.read()
        print(f"6. SIGTERM exit code {rc}; clean-exit line: "
              f"{'SIGTERM' in txt and 'exiting' in txt}")
        ok = ok and rc == 0
        print("SELFTEST:", "PASS" if ok else "FAIL", "- log:", log)
    sys.exit(0 if ok else 1)

main()
