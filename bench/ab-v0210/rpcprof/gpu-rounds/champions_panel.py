#!/usr/bin/env python3
"""Bounded serial TG/PP panel for the 2026-11-18 three-champion comparison.

  --prepare   write the frozen artifacts (delegates to champions_prepare)
  --dry-run   DEFAULT. Verify every pin and print the plan. Zero endpoint requests,
              zero health probes, zero process control, zero engine start.
  --run       PARENT ONLY. Boot one arm at a time, run the frozen pairs, then
              restore canonical production — on success and on failure alike.

Safety properties, in the order they matter here:
  * one engine/helper pair at a time, never two stacks on this memory-limited box;
  * production comes back on /mnt/srcds/Strata/launch-v0238.sh (original config plus
    the adopted b0ec engine) from a finally block and from signal handlers;
  * stop-all is invoked through an on-disk wrapper, so this process' own argv never
    carries a pattern that stop-all's pgrep -f sweep would match and self-terminate;
  * only the two per-arm socket paths this panel created are ever unlinked;
  * finite boot and request deadlines, at most one bounded retry of a pair that is
    genuinely dead, and no OS or GPU reset attempts. A hard kernel hang still needs
    an operator: that residual is stated, not papered over.
"""
from __future__ import annotations

# pyright: reportMissingImports=false
import argparse
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import champions_log as LOG  # noqa: E402
import champions_prepare as PREP  # noqa: E402

HERE = Path(__file__).resolve().parent
DRIVER = HERE.parent / "ab9_driver.py"
RESTORE = HERE / "champions_restore.sh"
STOP = HERE / "champions_stop.sh"
SUPERVISOR_STATE = "run/prod-swift15-f16kv-mixed-supervisor.json"
BOOT_TIMEOUT_S = 1500
POLL_S = 15
READY = re.compile(r"ready: http://")
# The launcher's own stdout names the two files it just opened. Take them from there,
# never from a date-prefix glob over logs/ that would happily match yesterday's log.
LAUNCHER_ANNOUNCE = re.compile(r"^Starting mixed backend on .*log: (\S+); helper: (\S+)$",
                               re.MULTILINE)
# Live progress: boot milestones, the band/limiter line the CURRENT arm is expected to
# log, timing lines, and anything that smells like trouble.
INTEREST = re.compile(
    r"ready:|serve: prompt|listening|staging|stage |headroom|band|limiter|advis|reclaim|"
    r"ERROR|rror:|fail|fallback|timeout|watchdog|abort|REFUSING|exited", re.IGNORECASE)
RAM_POLICY = re.compile(r"headroom|band|limiter", re.IGNORECASE)


def now() -> str:
    return time.strftime("%H:%M:%S")


def progress(message: str) -> None:
    print(f"[{now()}] {message}", flush=True)


class BootFailed(RuntimeError):
    pass


class Panel:
    """One --run session: boots, measures, summarises and always attempts restoration."""

    def __init__(self, out: Path, health: bool):
        self.out = out
        self.health = health
        self.manifest = json.loads((out / "manifest.json").read_text())
        self.restored = False
        self.offsets: dict[str, int] = {}

    def arm_dir(self, arm: str) -> Path:
        return self.out / "arms" / arm

    # ---------- stop / start / restore -------------------------------------------
    def stop_all(self) -> bool:
        """Delegate to the box's own stop-all through the wrapper (see its header)."""
        result = subprocess.run(["bash", str(STOP)], capture_output=True, text=True, timeout=900)
        for line in result.stdout.splitlines():
            if line.startswith("stop-all:"):
                progress(f"stop-all: {line.split(':', 1)[1].strip()}")
        if "ALL STOPPED" not in result.stdout:
            progress("stop-all did not report ALL STOPPED — refusing to start a second stack")
            return False
        return True

    def unlink_sockets(self, arm: str) -> None:
        """Remove exactly the two socket paths this panel owns. No globs, ever."""
        for path in (Path(p) for p in self.manifest["arms"][arm]["sockets"]):
            if not (path.exists() or path.is_symlink()):
                continue
            if not path.is_symlink() and not stat.S_ISSOCK(os.stat(path).st_mode):
                raise SystemExit(f"refusing to remove a non-socket: {path}")
            path.unlink()
            progress(f"removed owned socket {path.name}")

    def boot(self, arm: str) -> dict:
        """Start the arm's frozen launcher detached, then gate on a FRESH ready line."""
        spec = self.manifest["arms"][arm]
        launcher = Path(spec["frozen_launcher"]["path"])
        supervisor_out = self.arm_dir(arm) / "supervisor.out"
        supervisor_out.parent.mkdir(parents=True, exist_ok=True)
        # setsid --fork orphans the launcher into its own session. A plain backgrounded
        # child left the pair supervisor reparented under the caller, which then hung in
        # do_wait forever — the incident lane_boot.sh records.
        with supervisor_out.open("ab") as sink:
            subprocess.run(["setsid", "--fork", "bash", str(launcher)], cwd=str(PREP.ROOT),
                           stdout=sink, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, check=True)
        progress(f"booting {arm} via {launcher.name}")

        deadline = time.monotonic() + BOOT_TIMEOUT_S
        self.offsets = {str(supervisor_out): 0, spec["engine_log"]: 0}
        announce = None
        while time.monotonic() < deadline and not announce:
            announce = self._announce(supervisor_out)
            self._pump(supervisor_out)
            if not announce:
                time.sleep(5)
        if not announce:
            raise BootFailed(f"{arm}: launcher never announced its log paths "
                             f"within {BOOT_TIMEOUT_S} s, see {supervisor_out}")
        boot_log, helper_log = announce
        self.offsets[boot_log] = 0
        spec["helper_log"] = helper_log
        progress(f"{arm}: boot log {Path(boot_log).name}, helper log {Path(helper_log).name}")

        ready = ""
        while time.monotonic() < deadline:
            self._pump(Path(boot_log))
            self._pump(Path(spec["engine_log"]))
            text = Path(boot_log).read_bytes().replace(b"\0", b"").decode("utf-8", "replace")
            hit = READY.search(text)
            if hit:
                ready = text[hit.start():].split("\n", 1)[0].strip()
                break
            time.sleep(POLL_S)
        if not ready:
            raise BootFailed(f"{arm}: no fresh ready line within {BOOT_TIMEOUT_S} s, see {boot_log}")
        engine = self.engine_identity()
        want = os.path.realpath(spec["engine"]["path"])
        if not engine or engine["exe"] != want:
            raise BootFailed(f"{arm}: engine identity mismatch {engine} want {want}")
        # exactly the two helper sockets this arm was provisioned with, nothing assumed
        for sock in spec["sockets"]:
            if not Path(sock).exists():
                raise BootFailed(f"{arm}: expected helper socket {sock} is absent")

        record = {
            "arm": arm, "ready_line": ready, "engine": engine,
            "boot_log": boot_log, "helper_log": helper_log,
            "engine_log": spec["engine_log"], "supervisor_out": str(supervisor_out),
            "sockets": spec["sockets"], "conversation_cache_dir": spec["conversation_cache_dir"],
            "supervisor_state": str(PREP.ROOT / SUPERVISOR_STATE),
            "changed_path_evidence": self._changed_path_evidence(Path(boot_log)),
        }
        (self.arm_dir(arm) / "boot.json").write_text(json.dumps(record, indent=1) + "\n")
        progress(f"{arm}: READY engine pid {engine['pid']} exe {engine['exe']}")
        if arm == "current" and not record["changed_path_evidence"]:
            progress(f"{arm}: WARNING the boot log carries no headroom-band/limiter line, so "
                     "the RAM policy's live acceptance evidence is absent — do not infer it")
        return record

    def _announce(self, supervisor_out: Path) -> tuple[str, str] | None:
        if not supervisor_out.is_file():
            return None
        match = LAUNCHER_ANNOUNCE.search(supervisor_out.read_text(errors="replace"))
        return (match.group(1), match.group(2)) if match else None

    @staticmethod
    def engine_identity() -> dict | None:
        """The running engine, identified through /proc rather than through a log line."""
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                cmdline = Path(f"/proc/{entry}/cmdline").read_bytes()
            except OSError:
                continue
            if cmdline.split(b"\x00", 1)[0].endswith(b"/strata") and b"\x00--serve\x00" in cmdline:
                try:
                    exe = os.path.realpath(os.readlink(f"/proc/{entry}/exe"))
                except OSError:
                    continue
                return {"pid": int(entry), "exe": exe}
        return None

    def _pump(self, path: Path) -> None:
        """Stream one file forward to the terminal, NUL-stripped, filtered."""
        key = str(path)
        if not path.is_file():
            return
        start, size = self.offsets.get(key, 0), path.stat().st_size
        if size <= start:
            self.offsets[key] = size
            return
        with path.open("rb") as f:
            f.seek(start)
            blob = f.read(size - start).replace(b"\0", b"")
        self.offsets[key] = size
        for line in blob.decode("utf-8", "replace").splitlines():
            if INTEREST.search(line):
                print(f"    | {line[:400]}", flush=True)

    @staticmethod
    def _changed_path_evidence(boot_log: Path) -> list[str]:
        """Verbatim lines showing whether the arm's changed path announced itself.

        Captured, never interpreted. Absent for the historical arms is expected — the
        feature did not exist yet. Absent for CURRENT means the RAM-policy acceptance
        evidence was not produced, and that must stay visible.
        """
        if not boot_log.is_file():
            return []
        text = boot_log.read_bytes().replace(b"\0", b"").decode("utf-8", "replace")
        return [line.strip() for line in text.splitlines() if RAM_POLICY.search(line)][:20]

    def restore(self) -> bool:
        """Canonical production back, exactly once, reported honestly."""
        if self.restored:
            return True
        self.restored = True
        progress("restoring canonical production: launch-v0238.sh, original config, b0ec engine")
        cmd = ["bash", str(RESTORE)] + (["--health"] if self.health else [])
        with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True) as proc:
            for line in proc.stdout or []:
                print(f"  {line.rstrip()}", flush=True)
            code = proc.wait()
        progress(f"restore exited {code}")
        return code == 0

    # ---------- measurement -------------------------------------------------------
    def run_pair(self, arm: str, shape: dict, pair: int, retry: int) -> LOG.LegResult | None:
        spec = self.manifest["arms"][arm]
        entry = next(c for c in self.manifest["corpus"]
                     if c["shape"] == shape["name"] and c["pair"] == pair)
        leg = f"{shape['name']}-p{pair}" + (f"r{retry}" if retry else "")
        record_path = self.arm_dir(arm) / "records" / f"{arm}-{leg}.json"
        record_path.unlink(missing_ok=True)  # never read a stale attempt as this one's result
        cmd = [sys.executable, str(DRIVER), "--arm", arm, "--leg", leg, "--kind", "tg",
               "--ctx", str(entry["bytes"]), "--max-tokens", str(shape["max_tokens"]),
               "--prompt-file", entry["path"], "--prompt-sha256", entry["sha256"],
               "--out", str(self.arm_dir(arm) / "records"),
               "--elog", spec["engine_log"], "--hlog", spec["helper_log"],
               "--champs-shape", shape["name"], "--champs-pair", str(pair),
               "--champs-retry", str(retry)]
        if pair >= shape["warmup_pairs"]:
            cmd.append("--champs-measured")
        progress(f"{arm} {leg}: {entry['bytes']} B, max_tokens {shape['max_tokens']}, "
                 "fresh then cached")
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=shape["request_timeout_s"])
        except subprocess.TimeoutExpired:
            progress(f"{arm} {leg}: driver exceeded its {shape['request_timeout_s']} s deadline")
            return None
        if not record_path.is_file():
            progress(f"{arm} {leg}: driver produced no record")
            return None
        record = json.loads(record_path.read_text())
        result = LOG.leg_from_record(record, LOG.read_windows(record))
        if result.unmatched:
            progress(f"{arm} {leg}: {len(result.unmatched)} unmatched timing line(s) in the "
                     "window — not guessed at")
        if result.fresh and result.cached:
            progress(f"{arm} {leg}: fresh read {result.fresh.read} @{result.fresh.pp_tps} tok/s; "
                     f"cached reused {result.cached.reused} @{result.cached.tg_tps} tok/s; "
                     f"walls {result.walls}")
        else:
            progress(f"{arm} {leg}: NO usable timing pair "
                     f"({result.fresh_reason} / {result.cached_reason})")
        with (self.arm_dir(arm) / "legs.jsonl").open("a") as sink:
            sink.write(json.dumps(result.to_dict(), sort_keys=True) + "\n")
        return result

    def run_arm(self, arm: str) -> bool:
        spec = self.manifest["arms"][arm]
        (self.arm_dir(arm) / "records").mkdir(parents=True, exist_ok=True)
        if not self.stop_all():
            return False
        self.unlink_sockets(arm)
        engine_log = Path(spec["engine_log"])
        if engine_log.exists():
            engine_log.unlink()  # a leg's byte window must never span a previous boot
        try:
            self.boot(arm)
        except BootFailed as failure:
            progress(f"BOOT FAIL: {failure}")
            self.collect(arm)
            return False

        for shape in self.manifest["shapes"]:
            for pair in range(shape["pairs"]):
                result = self.run_pair(arm, shape, pair, retry=0)
                if result is None:
                    # Retry only while the engine is still there. If it died this is wedge
                    # territory: record it, restore, and leave the OS alone.
                    if self.engine_identity() is None:
                        progress(f"{arm}: engine is gone — arm aborted, no further retries")
                        self.collect(arm)
                        return False
                    progress(f"{arm}: retrying this pair ONCE (bounded, recorded)")
                    if self.run_pair(arm, shape, pair, retry=1) is None:
                        progress(f"{arm}: retry also failed — arm aborted")
                        self.collect(arm)
                        return False
        self.collect(arm)
        return True

    def collect(self, arm: str) -> None:
        """Summarise whatever exists and write it out immediately, per arm."""
        path = self.arm_dir(arm) / "legs.jsonl"
        legs = [LOG.LegResult.from_dict(json.loads(line)) for line in path.read_text().splitlines()] \
            if path.is_file() else []
        summary = LOG.summarise(legs)
        summary["arm"] = arm
        boot = self.arm_dir(arm) / "boot.json"
        summary["boot"] = json.loads(boot.read_text()) if boot.is_file() else None
        LOG.write_summary(arm, summary, self.out / "summary")
        LOG.print_summary(arm, summary)
        progress(f"{arm}: summary written to {self.out / 'summary' / (arm + '.summary.csv')}")


def selftest(out: Path) -> None:
    """A broken parser must not be able to pass a dry-run."""
    main = ("serve: prompt 16000 tokens = {r} reused + {k} read in 90000 ms (177.8 tok/s), "
            "512 generated in 21000 ms (24.4 tok/s), timeouts=0 fallbacks=0")
    cont = ("serve: prompt 1200 tokens = 0 reused + 1200 read in 100 ms (12.0 tok/s), "
            "8 generated in 400 ms (20.0 tok/s)")
    fresh = LOG.parse_window(f"{main.format(r=0, k=16000)}\n{cont}\n".encode())
    cached = LOG.parse_window(f"{cont}\n{main.format(r=16000, k=0)}\n".encode())
    first, second, unmatched = LOG.match_pair(fresh, cached)
    assert first and second and first.reused == 0 and second.reused == 16000 and len(unmatched) == 2
    # Genuine ambiguity: two plausible fresh lines and two plausible cached lines of the same
    # context size. The parser must refuse to choose rather than guess the last hit.
    noisy = LOG.parse_window(f"{main.format(r=0, k=16000)}\n{main.format(r=0, k=16000)}\n".encode())
    noisy_cached = LOG.parse_window(f"{main.format(r=16000, k=0)}\n"
                                    f"{main.format(r=16000, k=0)}\n".encode())
    dropped, also, noise = LOG.match_pair(noisy, noisy_cached)
    assert dropped is None and also is None and len(noise) == 4

    manifest = json.loads((out / "manifest.json").read_text())
    for entry in manifest["corpus"]:
        blob = Path(entry["path"]).read_bytes()
        assert blob == PREP.build_paired_prompt(entry["shape"], entry["pair"], entry["bytes"]), entry
        assert len(blob) == entry["bytes"] and entry["head_tag"].startswith("[champs ")
    tags = [entry["head_tag"] for entry in manifest["corpus"]]
    assert len(set(tags)) == len(tags), "corpus identity tags must be unique per pair"
    zero_read = LOG.classify(LOG.LegResult("x", "l", "short16k", 1, True, 512),
                             LOG.EnginePass(16000, 0, 0, 10.0, 0.0, 8, 100.0, 20.0, 0, 0, ""),
                             LOG.EnginePass(16000, 16000, 0, 1.0, 0.0, 8, 100.0, 20.0, 0, 0, ""))
    assert not zero_read.pp_usable and zero_read.fresh_reason == "fresh-leg-zero-read"
    print("dry-run self-test: parser matched through continuation lines, refused an ambiguous "
          "window, refused a zero-read 'fresh' leg, corpus reproducible byte for byte")


def dry_run(out: Path) -> int:
    """Verify pins and print the plan. No socket, no endpoint, no process table touched."""
    problems = PREP.verify(out)
    for problem in problems:
        print(f"PROBLEM: {problem}")
    print(PREP.plan_text(out))
    selftest(out)
    print("dry-run: zero endpoint requests, zero health probes, zero stop-all, zero engines started")
    return 1 if problems else 0


def run(out: Path, arms: list[str], health: bool) -> int:
    problems = PREP.verify(out)
    for problem in problems:
        print(f"DRIFT: {problem}")
    if problems:
        print("refusing to start an engine: a frozen input drifted")
        return 2
    panel = Panel(out, health)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    outcome = "ok"
    try:
        for arm in [name for name in PREP.ARM_ORDER if name in arms]:
            progress(f"=== arm {arm} ===")
            if not panel.run_arm(arm):
                outcome = f"{arm} incomplete"
    except (KeyboardInterrupt, Exception) as failure:  # noqa: BLE001 - restore is the point
        outcome = f"aborted: {type(failure).__name__}: {failure}"
        progress(outcome)
    finally:
        if not panel.restore():
            outcome += "; RESTORE FAILED — production may still be down, investigate now"
        elif outcome == "ok":
            outcome = "ok, production restored"
    print(f"panel outcome: {outcome}")
    return 0 if outcome == "ok, production restored" else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Bounded serial TG/PP champion panel (the default is a safe dry-run)")
    ap.add_argument("--out", type=Path, default=PREP.DEFAULT_OUT)
    ap.add_argument("--arm", action="append", choices=list(PREP.ARM_ORDER),
                    help="restrict --run to these arms (default: all, in frozen order)")
    ap.add_argument("--prepare", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="the default")
    ap.add_argument("--run", action="store_true", help="PARENT ONLY: starts engines")
    ap.add_argument("--no-health", action="store_true",
                    help="skip the post-restore finite-output check")
    a = ap.parse_args()

    if a.prepare:
        subprocess.run([sys.executable, str(PREP.HERE / "champions_prepare.py"),
                        "--out", str(a.out)], check=True)
        return 0
    if not a.run:
        return dry_run(a.out)
    if not (a.out / "manifest.json").is_file():
        print(f"refusing to run: {a.out / 'manifest.json'} missing — run --prepare first")
        return 2
    return run(a.out, a.arm or list(PREP.ARM_ORDER), not a.no_health)


if __name__ == "__main__":
    raise SystemExit(main())
