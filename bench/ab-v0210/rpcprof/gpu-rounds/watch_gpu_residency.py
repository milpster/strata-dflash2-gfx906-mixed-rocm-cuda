#!/usr/bin/env python3
"""Bounded passive DRM-residency and existing-engine-log capture; stdlib only.

Writes only evidence under a fresh --out directory. No requests or process control.
Log observation times bound when bytes were noticed, not when requests began/ended.
"""
from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import sys
import time
from pathlib import Path

DEFAULT_STATE = "/mnt/srcds/Strata/run/prod-swift15-f16kv-mixed-supervisor.json"
MAX_READ = 1024 * 1024


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="fresh evidence directory")
    parser.add_argument("--duration", type=nonnegative, default=3600.0)
    parser.add_argument("--interval", type=positive, default=30.0)
    parser.add_argument("--state", default=DEFAULT_STATE)
    parser.add_argument("--proc-root", default="/proc")
    parser.add_argument("--sys-root", default="/sys")
    args = parser.parse_args(argv)
    return collect(Path(args.out), args.duration, args.interval, args.state,
                   args.proc_root, args.sys_root)


def collect(out, duration, interval, state_path, proc_root, sys_root,
            monotonic=time.monotonic, sleep=time.sleep, wall=time.time):
    if not math.isfinite(duration) or duration < 0 or not math.isfinite(interval) or interval <= 0:
        print("watch: duration must be finite/nonnegative and interval finite/positive", file=sys.stderr)
        return 2
    try:
        out.mkdir(parents=True)
    except OSError as exc:
        print(f"watch: fresh --out required: {exc}", file=sys.stderr)
        return 2

    t0 = monotonic()
    started = wall()
    deadline = t0 + duration
    samples = invalid = prompt_rows = 0
    outcome, error, code = "completed", None, 0
    try:
        identity, valid, problem = select_and_verify(state_path, proc_root)
        tailer = LogTailer(out / "engine-increment.log")
        initial_log = tailer.read(identity.get("log_path") if valid else None)
        (out / "metadata.json").write_text(json.dumps({
            "parameters": {"duration_s": duration, "interval_s": interval,
                           "state": str(state_path), "proc_root": str(proc_root),
                           "sys_root": str(sys_root)},
            "started_wall": started, "started_at": clock(started),
            "initial_identity": identity, "initial_identity_valid": valid,
            "initial_identity_error": problem, "initial_log": initial_log,
        }, indent=2) + "\n")
        scheduled = t0
        with (out / "samples.jsonl").open("w") as sink:
            while True:
                wait = scheduled - monotonic()
                if wait > 0:
                    sleep(wait)
                sample = take_sample(wall, monotonic() - t0, state_path, proc_root, sys_root, tailer)
                sink.write(json.dumps(sample) + "\n")
                sink.flush()
                report(sample)
                samples += 1
                invalid += not sample["identity_valid"]
                prompt_rows += len(sample["log"]["prompt_lines"])
                now = monotonic()
                if now >= deadline:
                    break
                # Skip missed ticks rather than manufacturing catch-up observations.
                scheduled = min(t0 + (math.floor((now - t0) / interval) + 1) * interval, deadline)
    except KeyboardInterrupt:
        outcome, code = "interrupted", 130
    except (OSError, ValueError) as exc:
        outcome, error, code = "error", str(exc), 1
    ended = wall()
    summary = {"outcome": outcome, "error": error, "samples": samples,
               "invalid_identity_samples": invalid, "prompt_rows_observed": prompt_rows,
               "started_wall": started, "ended_wall": ended, "ended_at": clock(ended),
               "elapsed_s": monotonic() - t0}
    try:
        (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    except OSError as exc:
        print(f"watch: cannot save summary: {exc}", file=sys.stderr, flush=True)
        code = 1
    print(f"[watch] done: samples={samples} invalid={invalid} prompt_rows={prompt_rows} "
          f"outcome={outcome} error={error}", flush=True)
    return code


def take_sample(wall, elapsed, state_path, proc_root, sys_root, tailer):
    observed = wall()
    identity, valid, problem = select_and_verify(state_path, proc_root)
    clients, raw, errors = read_fdinfo_clients(identity["engine_pid"], proc_root) if valid else (
        [], [], [f"fdinfo skipped: {problem}"])
    gpu, gpu_errors = read_gpu_devices(sys_root)
    log = tailer.read(identity.get("log_path") if valid else None)
    after, after_valid, after_problem = select_and_verify(state_path, proc_root)
    if valid and (not after_valid or after != identity):
        valid, problem = False, after_problem or "selected identity changed during sample"
    return {"wall": observed, "observed_at": clock(observed), "elapsed_s": round(elapsed, 3),
            "identity": identity, "identity_after": after, "identity_valid": valid,
            "identity_error": problem, "drm": {"clients": clients, "raw": raw, "errors": errors},
            "gpu": gpu, "gpu_errors": gpu_errors, "log": log}


def select_and_verify(state_path, proc_root):
    identity: dict[str, int | str | None] = {"supervisor_pid": None, "generation": None, "engine_pid": None,
                "declared_start": None, "proc_start": None, "log_path": None, "wrapper_log": None}
    try:
        state = json.loads(Path(state_path).read_text())
    except (OSError, ValueError) as exc:
        return identity, False, f"state: {exc}"
    if not isinstance(state, dict):
        return identity, False, "state is not an object"
    identity.update(supervisor_pid=state.get("supervisor_pid"), generation=state.get("generation"),
                    engine_pid=state.get("engine_pid"), declared_start=state.get("engine_start"),
                    log_path=state.get("engine_stderr_log"), wrapper_log=state.get("engine_log"))
    for name in ("supervisor_pid", "generation", "engine_pid"):
        value = identity[name]
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            return identity, False, f"missing/invalid selected {name}"
    start = identity["declared_start"]
    if not isinstance(start, (str, int)) or isinstance(start, bool) or not str(start).isdigit():
        return identity, False, "missing/invalid engine_start"
    identity["declared_start"] = str(start)
    for name in ("log_path", "wrapper_log"):
        if not isinstance(identity[name], str) or not identity[name]:
            return identity, False, f"missing/invalid {name}"
    actual, problem = read_proc_start(identity["engine_pid"], proc_root)
    identity["proc_start"] = actual
    if problem:
        return identity, False, problem
    if actual != identity["declared_start"]:
        return identity, False, f"start identity mismatch: declared {start!r}, proc {actual!r}"
    return identity, True, None


def read_proc_start(pid, proc_root):
    try:
        text = Path(proc_root, str(pid), "stat").read_text()
    except OSError as exc:
        return None, f"proc stat: {exc}"
    end = text.rfind(")")
    fields = text[end + 1:].split() if end >= 0 else []
    if len(fields) < 20 or not fields[19].isdigit():
        return None, "proc stat: missing numeric start field after comm"
    return fields[19], None


def read_fdinfo_clients(pid, proc_root):
    try:
        files = sorted(Path(proc_root, str(pid), "fdinfo").iterdir())
    except OSError as exc:
        return [], [], [f"fdinfo: {exc}"]
    clients, raw, errors, seen = [], [], [], {}
    for path in files:
        try:
            text = path.read_text()
        except OSError as exc:
            errors.append(f"fdinfo/{path.name}: {exc}")
            continue
        fields = {key.strip(): value.strip() for line in text.splitlines()
                  for key, sep, value in [line.partition(":")] if sep}
        if "drm-pdev" not in fields and "drm-client-id" not in fields:
            continue
        raw.append({"fd": path.name, "text": text, "fields": fields})
        key = (fields.get("drm-pdev"), fields.get("drm-client-id"))
        if None in key:
            errors.append(f"fdinfo/{path.name}: missing PCI/client identity")
            continue
        if key in seen:
            seen[key]["fds"].append(path.name)
            continue
        entry = {"pci": key[0], "client_id": key[1], "fds": [path.name],
                 "requested_vram_bytes": parse_size(fields.get("amd-requested-vram")),
                 "evicted_bytes": parse_size(fields.get("amd-evicted-vram")),
                 "resident_vram_bytes": parse_size(fields.get("drm-resident-vram")),
                 "resident_gtt_bytes": parse_size(fields.get("drm-resident-gtt"))}
        seen[key] = entry
        clients.append(entry)
    return clients, raw, errors


def read_gpu_devices(sys_root):
    try:
        cards = sorted(Path(sys_root, "class", "drm").iterdir())
    except OSError as exc:
        return [], [f"drm sysfs: {exc}"]
    devices, errors = [], []
    for card in cards:
        if not card.name.startswith("card") or not card.name[4:].isdigit():
            continue
        device = (card / "device").resolve()
        record: dict[str, str | int | None] = {"card": card.name, "pci": device.name}
        try:
            for line in (device / "uevent").read_text().splitlines():
                if line.startswith("PCI_SLOT_NAME="):
                    record["pci"] = line.split("=", 1)[1]
        except OSError as exc:
            errors.append(f"{card.name}/uevent: {exc}")
        for output, source in (("vram_total_bytes", "mem_info_vram_total"),
                               ("vram_used_bytes", "mem_info_vram_used"),
                               ("gtt_used_bytes", "mem_info_gtt_used"),
                               ("busy_percent", "gpu_busy_percent")):
            try:
                record[output] = int((device / source).read_text().strip())
            except (OSError, ValueError) as exc:
                record[output] = None
                errors.append(f"{card.name}/{source}: {exc}")
        devices.append(record)
    return devices, errors


class LogTailer:
    """Baseline at the initial EOF; retain raw increments and explicit source resets."""

    def __init__(self, increment_path, max_read=MAX_READ):
        self.increment_path = Path(increment_path)
        self.increment_path.touch(exist_ok=False)
        self.max_read = max_read
        self.source = None
        self.offset = 0
        self.captured = 0
        self.pending = b""

    def read(self, log_path):
        record = {"source_path": log_path, "observed_at": clock(time.time()), "inode": None,
                  "device": None, "from": None, "to": None, "new_bytes": 0,
                  "backlog_bytes": None, "reset": None, "prompt_lines": [], "error": None,
                  "capture_from": self.captured, "capture_to": self.captured}
        if not log_path:
            record["error"] = "no valid selected engine stderr log"
            return record
        try:
            with open(log_path, "rb") as source:
                stat = os.fstat(source.fileno())
                identity = (str(log_path), stat.st_dev, stat.st_ino)
                record.update(inode=stat.st_ino, device=stat.st_dev)
                if self.source is None:
                    self.source, self.offset = identity, stat.st_size
                    record.update(**{"from": self.offset, "to": self.offset, "backlog_bytes": 0})
                    return record
                if identity != self.source:
                    record["reset"] = "path" if identity[0] != self.source[0] else "rotate"
                    self.source, self.offset, self.pending = identity, 0, b""
                elif stat.st_size < self.offset:
                    record["reset"] = "truncate"
                    self.offset, self.pending = 0, b""
                start = self.offset
                source.seek(start)
                data = source.read(min(self.max_read, max(0, stat.st_size - start)))
        except OSError as exc:
            record["error"] = f"engine log read: {exc}"
            return record
        with self.increment_path.open("ab") as sink:
            sink.write(data)
        self.offset += len(data)
        self.captured += len(data)
        record.update(**{"from": start, "to": self.offset, "new_bytes": len(data),
                         "backlog_bytes": max(0, stat.st_size - self.offset),
                         "capture_to": self.captured})
        lines = (self.pending + data).split(b"\n")
        self.pending = lines.pop()
        record["prompt_lines"] = [text for line in lines
                                  for text in [line.replace(b"\0", b"").decode("utf-8", "replace")]
                                  if text.startswith("strata serve: prompt ") and "generated" in text]
        return record


def report(sample):
    identity = sample["identity"]
    clients = "; ".join(f"{client['pci']} evicted={kib(client['evicted_bytes'])} "
                        f"gtt={kib(client['resident_gtt_bytes'])}" for client in sample["drm"]["clients"])
    log = sample["log"]
    errors = sample["drm"]["errors"] + sample["gpu_errors"]
    print(f"[{sample['observed_at']} +{sample['elapsed_s']:.1f}s] "
          f"pid={identity['engine_pid']} gen={identity['generation']} start={identity['proc_start']} "
          f"valid={sample['identity_valid']} {clients or 'no-drm-clients'} "
          f"log={log['from']}:{log['to']} new={log['new_bytes']}B backlog={log['backlog_bytes']} "
          f"reset={log['reset']} errors={len(errors)} "
          f"problem={sample['identity_error'] or log['error']}", flush=True)
    for line in log["prompt_lines"]:
        print(f"    {log['observed_at']} (observed) {line}", flush=True)


def parse_size(raw):
    if raw is None:
        return None
    parts = raw.split()
    factors = {"B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3}
    if not 1 <= len(parts) <= 2 or not parts[0].isdigit():
        return None
    factor = factors.get(parts[1], None) if len(parts) == 2 else 1
    return int(parts[0]) * factor if factor is not None else None


def nonnegative(value):
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("must be finite and nonnegative")
    return number


def positive(value):
    number = nonnegative(value)
    if number == 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def kib(value):
    return "NA" if value is None else f"{value // 1024}KiB"


def clock(epoch):
    return datetime.datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


if __name__ == "__main__":
    raise SystemExit(main())
