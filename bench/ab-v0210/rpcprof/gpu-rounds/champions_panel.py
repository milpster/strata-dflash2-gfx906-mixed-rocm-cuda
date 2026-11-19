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
PUMP_S = 2
READY = re.compile(r"ready: http://")
# The launcher's own stdout names the two files it just opened. Take them from there,
# never from a date-prefix glob over logs/ that would happily match yesterday's log.
LAUNCHER_ANNOUNCE = re.compile(r"^Starting mixed backend on .*log: (\S+); helper: (\S+)$",
                               re.MULTILINE)
# Live progress: boot milestones, the band/limiter line the CURRENT arm is expected to
# log, engine timing/DONE lines, and anything that smells like trouble. Generated text is
# never matched, so completions cannot spew into the terminal; numeric checks elsewhere
# read engine counters only, never this text (the word "inf" in a reply is not a number).
INTEREST = re.compile(
    r"ready:|serve: prompt|listening|staging|stage |headroom|band|limiter|advis|reclaim|"
    r"\bdone\b|tok/s|tokens/s|prefill|decode|resident|ERROR|rror:|fail|fallback|timeout|"
    r"watchdog|abort|REFUSING|exited", re.IGNORECASE)
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
        self.live: list[Path] = []

    def arm_dir(self, arm: str) -> Path:
        return self.out / "arms" / arm

    # ---------- stop / start / restore -------------------------------------------
    def stop_all(self) -> bool:
        """Delegate to the box's own stop-all through the wrapper (see its header).

        Both conditions are required: a zero exit AND the ALL STOPPED verdict. Either
        alone can pass while a pair is still resident, and starting a second pair on a
        62 GiB box is the failure we are most careful about.
        """
        result = subprocess.run(["bash", str(STOP)], capture_output=True, text=True, timeout=900)
        for line in result.stdout.splitlines():
            if line.startswith("stop-all:"):
                progress(f"stop-all: {line.split(':', 1)[1].strip()}")
        if result.returncode != 0 or "ALL STOPPED" not in result.stdout:
            progress(f"stop-all not clean (exit {result.returncode}) — refusing to start a "
                     "second stack")
            return False
        return True

    def fresh_run_dir(self, arm: str) -> Path:
        """Start each arm from an empty evidence dir; never delete a previous measurement.

        Appending to yesterday's supervisor.out or legs.jsonl would let a stale announce
        line satisfy the boot gate and old samples inflate n. Prior evidence is renamed
        aside inside out/, so it stays inspectable.
        """
        directory = self.arm_dir(arm)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        archived = []
        if directory.exists() and any(directory.iterdir()):
            target = self.out / "archive" / f"{arm}-{stamp}"
            target.parent.mkdir(parents=True, exist_ok=True)
            while target.exists():
                target = target.with_name(target.name + "x")
            directory.rename(target)
            archived.append(str(target))
        directory.mkdir(parents=True, exist_ok=True)

        # The raw engine log sits outside the arm dir: move it aside rather than unlinking
        # it, so a rerun starts clean without destroying the earlier raw record.
        engine_log = Path(self.manifest["arms"][arm]["engine_log"])
        if engine_log.exists():
            stash = self.out / "archive" / "logs" / f"{stamp}"
            stash.mkdir(parents=True, exist_ok=True)
            engine_log.rename(stash / engine_log.name)
            archived.append(str(stash / engine_log.name))
        summary = self.out / "summary" / f"{arm}.summary.json"
        if summary.exists():
            stash = self.out / "archive" / "summary" / f"{stamp}"
            stash.mkdir(parents=True, exist_ok=True)
            for prior in (self.out / "summary").glob(f"{arm}.summary.*"):
                prior.rename(stash / prior.name)
            archived.append(str(stash))
        if archived:
            progress(f"{arm}: previous evidence preserved at {', '.join(archived)}")
        return directory

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
        # "wb", not "ab": a truncated file cannot hand this gate yesterday's announce line.
        with supervisor_out.open("wb") as sink:
            subprocess.run(["setsid", "--fork", "bash", str(launcher)], cwd=str(PREP.ROOT),
                           stdout=sink, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, check=True)
        progress(f"booting {arm} via {launcher.name}")

        deadline = time.monotonic() + BOOT_TIMEOUT_S
        self.live = [supervisor_out, Path(spec["engine_log"])]
        self.offsets = {str(p): 0 for p in self.live}
        announce = None
        while time.monotonic() < deadline and not announce:
            announce = self._announce(supervisor_out)
            self._pump_all()
            if not announce:
                time.sleep(5)
        if not announce:
            raise BootFailed(f"{arm}: launcher never announced its log paths "
                             f"within {BOOT_TIMEOUT_S} s, see {supervisor_out}")
        boot_log, helper_log = announce
        self.live.extend([Path(boot_log), Path(helper_log)])
        self.offsets.update({str(p): 0 for p in self.live})
        spec["helper_log"] = helper_log
        progress(f"{arm}: boot log {Path(boot_log).name}, helper log {Path(helper_log).name}")

        ready = ""
        while time.monotonic() < deadline:
            self._pump_all()
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

    def _pump_all(self) -> list[str]:
        """Stream every live log forward to the terminal, NUL-stripped and filtered.

        Called during boot AND while a driver request is in flight, so the terminal is
        never silent until the pair returns. Returns the lines it printed.
        """
        shown: list[str] = []
        for path in self.live:
            shown.extend(self._pump(path))
        return shown

    def _pump(self, path: Path) -> list[str]:
        """Stream one file forward to the terminal, NUL-stripped, filtered."""
        key = str(path)
        if not path.is_file():
            return []
        start, size = self.offsets.get(key, 0), path.stat().st_size
        if size <= start:
            self.offsets[key] = size
            return []
        with path.open("rb") as f:
            f.seek(start)
            blob = f.read(size - start).replace(b"\0", b"")
        self.offsets[key] = size
        shown = []
        for line in blob.decode("utf-8", "replace").splitlines():
            if INTEREST.search(line):
                print(f"    | {line[:400]}", flush=True)
                shown.append(line)
        return shown

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
        """Canonical production back, exactly once, reported honestly.

        The restore's own logs go to a FRESH directory under this run's out tree. Left to
        its default, champions_restore.sh writes into the frozen measurement tree it is
        named after, so a rerun would leave restore evidence beside completed arms — and a
        test could overwrite real measurements. The pinned engine hash, deadline and poll
        come from the manifest, so acceptance is bound to the image this profile measured.
        """
        if self.restored:
            return True
        self.restored = True
        pin = self.manifest.get("restore") or {}
        directory = self.out / "restore" / time.strftime("%Y%m%d-%H%M%S")
        while directory.exists():
            directory = directory.with_name(directory.name + "x")
        directory.mkdir(parents=True, exist_ok=True)
        progress(f"restoring canonical production: {pin.get('launcher', PREP.ROOT / 'launch-v0238.sh')}, "
                 "original config, pinned engine")
        cmd = ["bash", str(RESTORE), "--out", str(directory)]
        for flag, key in (("--engine-sha", "engine_sha"), ("--timeout", "timeout_s"),
                          ("--poll", "poll_s")):
            if pin.get(key) is not None:
                cmd += [flag, str(pin[key])]
        if self.health:
            cmd += ["--health"]
        progress(f"restore evidence -> {directory}")
        with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True) as proc:
            for line in proc.stdout or []:
                print(f"  {line.rstrip()}", flush=True)
            code = proc.wait()
        progress(f"restore exited {code}")
        return code == 0

    # ---------- measurement -------------------------------------------------------
    def accept_pair(self, arm: str, shape: dict, pair: int, result: "LOG.LegResult") -> str:
        """Last gate before a parsed pair is allowed to count. Empty string accepts.

        It exists because a pair can only be trusted once the evidence on the OTHER side
        of it agrees — for the reserve panel, once the pair supervisor is still declaring
        the same generation it declared before the pair. A reason here means the pair is
        NOT written to legs.jsonl at all: the summariser filters on measured rates, so a
        problem flag bolted onto a stored leg would still feed the medians.
        """
        return ""

    def reject_pair(self, arm: str, leg: str, result: "LOG.LegResult", reason: str) -> None:
        """Keep a rejected pair as evidence, out of the counted set, with its reason."""
        row = result.to_dict()
        row["rejected"] = reason
        with (self.arm_dir(arm) / "rejected.jsonl").open("a") as sink:
            sink.write(json.dumps(row, sort_keys=True) + "\n")
        progress(f"{arm} {leg}: pair REJECTED, not counted — {reason}")

    def run_pair(self, arm: str, shape: dict, pair: int) -> LOG.LegResult | None:
        """Run one fresh+cached pair, streaming the engine logs the whole time.

        There is deliberately no retry. Killing the client does not cancel the work the
        server already accepted, so launching a second attempt while the first is still
        executing would overlap benchmark requests and corrupt the very number being
        measured. Any anomaly ends the arm; the evidence collected so far is kept.
        """
        spec = self.manifest["arms"][arm]
        entry = next(c for c in self.manifest["corpus"]
                     if c["shape"] == shape["name"] and c["pair"] == pair)
        leg = f"{shape['name']}-p{pair}"
        record_path = self.arm_dir(arm) / "records" / f"{arm}-{leg}.json"
        driver_out = self.arm_dir(arm) / "driver" / f"{leg}.out"
        driver_out.parent.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, str(DRIVER), "--arm", arm, "--leg", leg, "--kind", "tg",
               "--ctx", str(entry["bytes"]), "--max-tokens", str(shape["max_tokens"]),
               "--prompt-file", entry["path"], "--prompt-sha256", entry["sha256"],
               "--out", str(self.arm_dir(arm) / "records"),
               "--elog", spec["engine_log"], "--hlog", spec["helper_log"],
               "--champs-shape", shape["name"], "--champs-pair", str(pair)]
        if pair >= shape["warmup_pairs"]:
            cmd.append("--champs-measured")
        template = self.manifest.get("request_template") or {}
        if template:
            cmd += ["--request-template", template["path"],
                    "--request-template-sha256", template["sha256"]]
        progress(f"{arm} {leg}: {entry['bytes']} B, max_tokens {shape['max_tokens']}, "
                 "fresh then cached")
        deadline = time.monotonic() + shape["request_timeout_s"]
        with driver_out.open("wb") as sink:
            process = subprocess.Popen(cmd, stdout=sink, stderr=subprocess.STDOUT)
            timed_out = False
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    timed_out = True
                    process.kill()
                    break
                self._pump_all()          # live output for the whole request, not just boot
                time.sleep(PUMP_S)
            returncode = process.wait()
        self._pump_all()                  # flush whatever printed on the way out

        if timed_out:
            progress(f"{arm} {leg}: driver exceeded its {shape['request_timeout_s']} s deadline "
                     "and was killed; aborting the arm — server-side work from this request may "
                     "still be running, so retrying now would overlap benchmark traffic")
            return None
        if returncode != 0:
            tail = "\n".join(driver_out.read_text(errors="replace").splitlines()[-8:])
            progress(f"{arm} {leg}: driver exited {returncode}: {tail}")
            return None
        if not record_path.is_file():
            progress(f"{arm} {leg}: driver exited 0 but produced no record")
            return None

        record = json.loads(record_path.read_text())
        if record.get("prompt_sha256") != entry["sha256"]:
            progress(f"{arm} {leg}: driver read a different prompt than the corpus pins")
            return None
        result = LOG.leg_from_record(record, LOG.read_windows(record))
        result.intended_bytes = entry["bytes"]
        if result.unmatched:
            progress(f"{arm} {leg}: {len(result.unmatched)} continuation-shaped timing line(s) "
                     "in the window — excluded, the main pair was matched by counts")
        if result.complete:
            progress(f"{arm} {leg}: ctx {result.context_tokens} tok, fresh read "
                     f"{result.fresh.read} @{result.fresh.pp_tps} tok/s; cached reused "
                     f"{result.cached.reused} @{result.cached.tg_tps} tok/s; walls {result.walls}")
        else:
            progress(f"{arm} {leg}: UNUSABLE — {', '.join(result.reasons)}")
        reason = self.accept_pair(arm, shape, pair, result)
        if reason:
            self.reject_pair(arm, leg, result, reason)
            return None
        with (self.arm_dir(arm) / "legs.jsonl").open("a") as sink:
            sink.write(json.dumps(result.to_dict(), sort_keys=True) + "\n")
        return result

    def run_arm(self, arm: str) -> bool:
        spec = self.manifest["arms"][arm]
        self.fresh_run_dir(arm)
        if not self.stop_all():
            return False
        self.unlink_sockets(arm)
        try:
            self.boot(arm)
        except BootFailed as failure:
            progress(f"BOOT FAIL: {failure}")
            self.collect(arm)
            return False

        for shape in self.manifest["shapes"]:
            for pair in range(shape["pairs"]):
                result = self.run_pair(arm, shape, pair)
                if result is None or not result.complete:
                    reasons = ", ".join(result.reasons) if result else "no usable record"
                    progress(f"{arm} {shape['name']}-p{pair}: unusable — {reasons}")
                    progress(f"{arm}: aborting the arm; partial evidence retained, no retry")
                    self.collect(arm)
                    return False
        complete = self.collect(arm)
        if not complete:
            progress(f"{arm}: planned pairs are not all present — arm incomplete")
        return complete

    def planned_pairs(self) -> int:
        return sum(shape["pairs"] for shape in self.manifest["shapes"])

    def collect(self, arm: str) -> bool:
        """Summarise whatever exists, write it out at once, and report completeness.

        Completeness is planned pairs vs complete pairs. Zero or missing samples can never
        read as success, and no fallback number is invented for a gap.
        """
        path = self.arm_dir(arm) / "legs.jsonl"
        legs = [LOG.LegResult.from_dict(json.loads(line)) for line in path.read_text().splitlines()] \
            if path.is_file() else []
        complete = [leg for leg in legs if leg.complete]
        summary = LOG.summarise(legs)
        summary["arm"] = arm
        summary["pairs_planned"] = self.planned_pairs()
        summary["pairs_complete"] = len(complete)
        summary["complete"] = len(complete) >= self.planned_pairs()
        boot = self.arm_dir(arm) / "boot.json"
        summary["boot"] = json.loads(boot.read_text()) if boot.is_file() else None
        LOG.write_summary(arm, summary, self.out / "summary")
        LOG.print_summary(arm, summary)
        progress(f"{arm}: {len(complete)}/{self.planned_pairs()} planned pairs complete; "
                 f"summary at {self.out / 'summary' / (arm + '.summary.csv')}")
        return summary["complete"]


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
    order = [name for name in (panel.manifest.get("arm_order") or list(PREP.ARM_ORDER))
             if name in arms]
    try:
        for arm in order:
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
