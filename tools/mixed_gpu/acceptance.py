#!/usr/bin/env python3
"""RAW EVIDENCE capture for the mixed HIP/CUDA acceptance runs - not a PASS detector.

This tool records what happened.  It never decides that the helper works, that a phase was
proven, or that two runs are comparable.  The parent owns phase classification, A/B pairing
and every acceptance claim.

  sample : send explicit pinned NON-streaming requests to an ISOLATED loopback endpoint and
           record the full request bytes, the full response (status/headers/body/parsed
           JSON/usage/finish reason), wall+monotonic start/finish/duration, the output text
           and hash, and EVERY byte each named log file appended DURING that one request.
  fault  : start one pinned streaming request, anchor the fault to the first non-empty
           content/reasoning delta OR to a newly-appended log marker (watched independently
           of token arrival, so it can fire during prefill), send SIGKILL to a pidfd-bound
           helper, and record the raw SSE stream with per-chunk receive times, the kill
           syscall timestamps and the pidfd exit event.

Fail-closed rules, all enforced in code and covered by tests:

  * Endpoint: loopback literals only (127.0.0.1, ::1), scheme http, and port 8009 is
    refused outright - that is the production service.  Nothing is resolved through DNS.
  * Sampling is pinned by the request itself: temperature == 0 and an integer seed, both
    present in the caller's JSON.  The request bytes are sent verbatim; this tool never
    adds, removes or rewrites a sampling field.  `sample` refuses `"stream": true` and
    `fault` requires it, both refused BEFORE any network contact.
  * Every argument is validated before an evidence directory is created: counts, timeouts,
    sample/warmup counts, and `--log` labels (safe characters only, no slashes, no `..`).
  * Evidence directories are never reused: --out must not exist.  An existing directory is
    a refusal (exit 2), never an overwrite.
  * Log slices are bounded byte ranges, read from the opened fd: the file is opened,
    `os.fstat`'d on the fd, the range is read up to the size sampled on that fd, and the fd
    is `fstat`'d again.  A dev/inode change on the fd is rotation.  A size below the start
    offset is truncation.  Bounded reads mean a concurrent harmless append is not a false
    failure, and there is no stat-then-read TOCTOU.  Detection is best-effort, not a proof:
    same-inode truncate-and-regrow past the start offset is caught only by the prefix and
    tail-anchor fingerprints (a bounded window each), so a rewrite of the whole file with a
    re-grown tail is NOT comprehensively provable.  A vanished log after preflight is
    reported, never read as zero work.
  * A bad HTTP status, an unparseable body, an error body, a non-object body, a missing
    explicit finish_reason, an "error" finish_reason, missing usage or a malformed nested
    shape is saved in full first, then the process exits non-zero (exit 3).  Arbitrary
    JSON shapes (array, null, nested junk) are validated safely, never by crashing.
  * Incremental streaming: the real transport reads with `read1`, never a full-size
    blocking `read`, and stamps wall+monotonic AFTER the bytes are returned.  The
    connection is closed on EOF, on error, and on generator close.
  * fault signals ONLY through pidfd_open + pidfd_send_signal(SIGKILL).  The pidfd is
    opened FIRST, then the PID is bound to the stat of `/proc/<pid>/exe` itself (dev+ino,
    not a readlink pathname) and to `/proc/<pid>/stat` starttime.  The identity is
    RECHECKED immediately before the signal.  No pgrep, no pkill, no `kill`, no `os.kill`,
    no process groups, no SIGTERM, no grace, no health gate, no sleep schedule.
  * A completed stream is never killed: events are examined in arrival order, and if the
    batch in hand already carries a terminal event (finish_reason, `data: [DONE]`, an
    error object, unparseable/non-object payload), no signal is sent.  A non-2xx stream is
    never signalled.
  * `signal_sent` is true only when the SIGKILL syscall succeeded.  A failed send keeps its
    real error, sends nothing, and is INCONCLUSIVE.  After a successful send the pidfd
    event is taken IMMEDIATELY (before the stream is drained to EOF) so the exit timestamp
    is real.  POLLIN proves the process EXITED; it does not prove it was reaped.  /proc
    absence is recorded at the death moment and at the end as a diagnostic, never used to
    claim a live PID was alive.
  * Trigger anchors: pre-existing log lines are ignored (the slice starts at the offset
    recorded before the request).  A request that completes before the trigger fires is
    exit 4 with no signal.
  * phase_proven is false by default and cannot be raised: a first token alone does not
    prove the engine was still decoding (it may be buffered), and a log marker does not
    prove a phase.  Engine/helper fallback evidence is preserved as raw bytes.
  * Helper tails (`helper=reqs=.. rows=.. bytes=.. timeouts=.. fallbacks=..`) are
    PER-ENGINE-CALL DELTAS: src/program/generate.cpp:14910-14914 snapshots the counters at
    each engine call and :16155-16160 prints the differences.  They are NOT lifetime
    counters, this tool never subtracts one DONE line from another, one HTTP request can
    emit several engine calls, and lines from different logs are never combined into one
    series.  No tail implies no claim: `proves_helper_gpu_compute` is false.
  * No service startup, stop, restore, retry, reconnect, state reset, health gate,
    service, dependency or logging surface.  stdlib only.  Threads are bounded: a reader
    thread and (for log-marker triggers) a marker watcher, both with a deadline, a stop
    event, a join in `finally`, and the pidfd closed on every exit path.

Cold/warm, chain labels, build ids and helper state (off/on) are OPERATOR DECLARATIONS
passed on the command line.  They are stored under "declared" with
"measured_by_this_tool": false, and every warmup and sample is retained individually.
"comparable" is always false: this tool never claims two runs are A/B comparable.

Exit codes: 0 raw evidence captured and validated, 2 refused (precondition missing,
endpoint, sampling pin, argument shape, existing output dir - nothing was sent), 3 evidence
invalid (bad HTTP/JSON/empty completion, log rotation/truncation/rewrite, killed helper
with an invalid response), 4 fault not anchored (no trigger, stream completed first,
non-2xx, identity mismatch at preflight or at the recheck, pidfd unavailable, send failed,
exit not confirmed).
"""

from __future__ import annotations

import argparse
import ctypes
import dataclasses
import datetime
import hashlib
import http.client
import json
import os
import queue
import re
import select
import signal
import stat
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator

TOOL_VERSION = "r3-2"
EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_EVIDENCE_BAD = 3
EXIT_NOT_ANCHORED = 4

LOOPBACK_HOSTS = {"127.0.0.1", "::1"}
REFUSED_PORTS = {8009}          # the production service; this tool must never touch it
DEFAULT_PORT = 8019
LABEL_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
FINGERPRINT = 4096              # prefix / tail-anchor window for rewrite detection

Connect = Callable[[str, int, float], Any]
Transport = Callable[[str, int, str, float, bytes, Connect],
                    tuple[dict[str, Any], Iterator[tuple[float, float, bytes]]]]


def _utc_stamp(wall: float) -> str:
    return datetime.datetime.fromtimestamp(wall, datetime.timezone.utc).isoformat()


# ---------------------------------------------------------------- pidfd (Linux)

class PidfdUnavailable(RuntimeError):
    pass


def _load_libc() -> Any:
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    for name in ("pidfd_open", "pidfd_send_signal"):
        if not hasattr(libc, name):
            raise PidfdUnavailable(f"libc has no {name}; pidfd is required, no fallback")
    return libc


def real_pidfd_open(pid: int) -> int:
    libc = _load_libc()
    libc.pidfd_open.argtypes = [ctypes.c_int, ctypes.c_uint]
    libc.pidfd_open.restype = ctypes.c_int
    fd = libc.pidfd_open(pid, 0)
    if fd < 0:
        err = ctypes.get_errno()
        raise PidfdUnavailable(f"pidfd_open({pid}) failed: errno {err} ({os.strerror(err)})")
    return fd


def real_pidfd_kill(pidfd: int) -> None:
    libc = _load_libc()
    libc.pidfd_send_signal.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
    libc.pidfd_send_signal.restype = ctypes.c_int
    rc = libc.pidfd_send_signal(pidfd, signal.SIGKILL, None, 0)
    if rc != 0:
        err = ctypes.get_errno()
        raise PidfdUnavailable(f"pidfd_send_signal(SIGKILL) failed: errno {err} ({os.strerror(err)})")


def real_pidfd_death(pidfd: int, timeout: float) -> bool:
    """POLLIN on a pidfd proves the process EXITED.  It does NOT prove it was reaped."""
    poller = select.poll()
    poller.register(pidfd, select.POLLIN)
    events = poller.poll(max(0, int(timeout * 1000)))
    return bool(events) and bool(events[0][1] & select.POLLIN)


# ---------------------------------------------------------------- transport

def real_connect(host: str, port: int, timeout: float) -> Any:
    return http.client.HTTPConnection(host, port, timeout=timeout)


def real_transport(host: str, port: int, path: str, timeout: float, body: bytes,
                   connect: Connect = real_connect
                   ) -> tuple[dict[str, Any], Iterator[tuple[float, float, bytes]]]:
    """POST body; yield (mono, wall, chunk) per received piece.

    `read1` is the point: a full-size blocking `read(65536)` holds a small SSE stream until
    EOF, so the fault trigger would fire after the inference was already complete.  read1
    returns as soon as any bytes are available.  The stamps are taken AFTER the bytes come
    back, so they are receive times, not "time before a blocking call".
    """
    conn = connect(host, port, timeout)
    info: dict[str, Any] = {"status": None, "reason": None, "headers": []}

    def pieces() -> Iterator[tuple[float, float, bytes]]:
        try:
            conn.request("POST", path, body=body,
                         headers={"Content-Type": "application/json",
                                  "Accept": "application/json",
                                  "Content-Length": str(len(body)),
                                  "Connection": "close"})
            resp = conn.getresponse()
            info["status"], info["reason"] = resp.status, resp.reason
            info["headers"] = list(resp.getheaders())
            while True:
                part = resp.read1(65536)                # incremental, never blocking-full
                if not part:
                    return
                yield (time.monotonic(), time.time(), part)   # stamped AFTER the bytes
        finally:
            conn.close()                                 # EOF, error and early close

    return info, pieces()


# ---------------------------------------------------------------- hooks

@dataclasses.dataclass
class FaultHooks:
    """Every OS touch of sample/fault goes through one of these seams; tests inject fakes."""

    transport: Transport = real_transport
    connect: Connect = real_connect
    pidfd_open: Callable[[int], int] = real_pidfd_open
    pidfd_kill: Callable[[int], None] = real_pidfd_kill
    pidfd_death: Callable[[int, float], bool] = real_pidfd_death
    pidfd_close: Callable[[int], None] = os.close
    proc_exists: Callable[[int], bool] = lambda pid: os.path.exists(f"/proc/{pid}")
    read_proc: Callable[[int, str], bytes] = lambda pid, name: Path(f"/proc/{pid}/{name}").read_bytes()
    stat_path: Callable[[str], os.stat_result] = os.stat
    wall: Callable[[], float] = time.time
    mono: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    death_timeout: float = 10.0
    marker_poll: float = 0.05


def default_fault_hooks() -> FaultHooks:
    return FaultHooks()


# ---------------------------------------------------------------- endpoint + args

def parse_endpoint(url: str) -> tuple[str, int, str]:
    """Return (host, port, path).  Refuse anything that is not an explicit loopback http URL."""
    if "://" not in url:
        raise ValueError(f"endpoint {url!r}: scheme http:// required")
    scheme, rest = url.split("://", 1)
    if scheme != "http":
        raise ValueError(f"endpoint {url!r}: only http:// is allowed, not {scheme!r}")
    hostport, _, tail = rest.partition("/")
    path = "/" + tail
    host, port = hostport, DEFAULT_PORT
    if hostport.startswith("["):                       # [::1]:8019
        close = hostport.find("]")
        if close < 0:
            raise ValueError(f"endpoint {url!r}: unterminated IPv6 literal")
        host = hostport[1:close]
        rest2 = hostport[close + 1:]
        if rest2.startswith(":"):
            port = int(rest2[1:])
    elif ":" in hostport:
        host, _, p = hostport.partition(":")
        if not p.isdigit():
            raise ValueError(f"endpoint {url!r}: port {p!r} is not numeric")
        port = int(p)
    if host not in LOOPBACK_HOSTS:
        raise ValueError(f"endpoint {url!r}: host {host!r} is not a loopback literal; refusing")
    if port in REFUSED_PORTS:
        raise ValueError(f"endpoint {url!r}: port {port} is the production service; refusing")
    if not 1 <= port <= 65535:
        raise ValueError(f"endpoint {url!r}: port {port} out of range")
    return host, port, path


def check_sampling_pin(raw: bytes, stream: bool) -> dict[str, Any]:
    """Require temperature == 0 and an integer seed in the CALLER's JSON.  Never edits it.

    stream=True (fault): the request's own "stream" must be true.
    stream=False (sample): "stream" must NOT be true - sample records whole completions.
    """
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"request is not valid JSON: {exc}")
    if not isinstance(obj, dict):
        raise ValueError(f"request JSON must be an object, got {type(obj).__name__}")
    if "temperature" not in obj:
        raise ValueError("request must set temperature: 0 explicitly; this tool will not add it")
    temp = obj["temperature"]
    if isinstance(temp, bool) or not isinstance(temp, (int, float)) or float(temp) != 0.0:
        raise ValueError(f"temperature must be 0, got {temp!r}")
    if "seed" not in obj:
        raise ValueError("request must set an integer seed explicitly; this tool will not add it")
    seed = obj["seed"]
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError(f"seed must be an integer, got {seed!r}")
    for key in ("max_tokens", "max_completion_tokens", "n"):
        if key in obj:
            val = obj[key]
            if isinstance(val, bool) or not isinstance(val, int) or val < (0 if key == "n" else 0):
                raise ValueError(f"request {key} must be a non-negative int, got {val!r}")
    is_stream = obj.get("stream", False)
    if not isinstance(is_stream, bool):
        raise ValueError(f"request stream must be a bool, got {is_stream!r}")
    opts = obj.get("stream_options")
    if opts is not None and not isinstance(opts, dict):
        raise ValueError(f"request stream_options must be an object when present, "
                         f"got {type(opts).__name__}")
    if isinstance(opts, dict) and "include_usage" in opts and not isinstance(
            opts["include_usage"], bool):
        raise ValueError(f"stream_options.include_usage must be a bool, "
                         f"got {opts['include_usage']!r}")
    if stream and is_stream is not True:
        raise ValueError('fault needs the request\'s own "stream": true')
    if not stream and is_stream is True:
        raise ValueError('sample refuses "stream": true; use fault for a streaming request')
    return obj


def parse_logs(specs: list[str]) -> dict[str, str]:
    """--log label=path  ->  {label: path}; labels are filename-safe, paths are checked."""
    out: dict[str, str] = {}
    for spec in specs:
        label, sep, path = spec.partition("=")
        if not sep or not label or not path:
            raise ValueError(f"--log needs label=path, got {spec!r}")
        if not LABEL_RE.match(label):
            raise ValueError(f"--log label {label!r} must be [A-Za-z0-9_.-]+ with no path separators")
        if label in out:
            raise ValueError(f"--log label {label!r} given twice")
        p = Path(path)
        if ".." in p.parts:
            raise ValueError(f"--log {label}: path {path} must not contain '..'")
        if not p.is_file():
            raise ValueError(f"--log {label}: {path} is not a readable file")
        out[label] = str(p)
    return out


def check_args(a: argparse.Namespace, logs: dict[str, str]) -> None:
    """Every numeric/shape argument is validated before any directory is created."""
    if a.timeout <= 0:
        raise ValueError(f"--timeout must be > 0, got {a.timeout}")
    if a.cmd == "sample":
        if a.samples < 1:
            raise ValueError(f"--samples must be >= 1, got {a.samples}")
        if a.warmups < 0:
            raise ValueError(f"--warmups must be >= 0, got {a.warmups}")
    else:
        if a.death_timeout <= 0:
            raise ValueError(f"--death-timeout must be > 0, got {a.death_timeout}")
        if a.marker_poll < 0.01:
            raise ValueError(f"--marker-poll must be >= 0.01 s, got {a.marker_poll}")
        if a.trigger == "log-marker" and not a.marker:
            raise ValueError("trigger log-marker needs --marker")
        if a.pid <= 0:
            raise ValueError(f"--pid must be a positive pid, got {a.pid}")


# ---------------------------------------------------------------- log identity

def _read_window(fh: Any, offset: int, length: int) -> bytes:
    fh.seek(offset)
    return fh.read(length)


def snapshot(path: str, hooks: FaultHooks) -> dict[str, Any]:
    """Identity of a log file taken ON THE OPENED FD, with fixed-length fingerprint windows."""
    with open(path, "rb") as fh:
        st = os.fstat(fh.fileno())
        size = st.st_size
        head = _read_window(fh, 0, FINGERPRINT)
        tail_offset = max(0, size - FINGERPRINT)
        tail = _read_window(fh, tail_offset, FINGERPRINT)
    return {"path": str(path), "dev": st.st_dev, "ino": st.st_ino, "size": size,
            "offset": size, "mtime_ns": st.st_mtime_ns, "mode": stat.S_IMODE(st.st_mode),
            "prefix_sha256": hashlib.sha256(head).hexdigest(), "prefix_len": len(head),
            "tail_sha256": hashlib.sha256(tail).hexdigest(), "tail_len": len(tail),
            "tail_offset": tail_offset}


def snapshot_logs(logs: dict[str, str], hooks: FaultHooks
                  ) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Preflight snapshots; a log that vanished since --log validation is a problem, not a crash."""
    starts: dict[str, dict[str, Any]] = {}
    problems: list[str] = []
    for name, p in logs.items():
        try:
            starts[name] = snapshot(p, hooks)
        except OSError as exc:
            problems.append(f"log {name}: log_missing_before_request: {exc}")
    return starts, problems


def read_appended(path: str, start: dict[str, Any], hooks: FaultHooks) -> tuple[dict[str, Any], bytes]:
    """The bytes appended since `start`, read on one fd with fstat at both ends.

    The range is bounded by the size sampled on that fd, so bytes a live writer appends
    after our sample are not part of the slice.  The fingerprint windows are read at their
    recorded offsets and lengths, so appends never move them.  Rotation/truncation/rewrite
    are reported, never smoothed into "no work".  Rewrite detection is best-effort inside
    those windows, NOT a proof over the whole file: a rewrite of bytes outside both windows
    is not detectable, and a same-inode truncate that re-grows past the start offset is
    caught only if it changed a window.
    """
    start_off = int(start["offset"])
    meta: dict[str, Any] = {"start": start, "end": None, "bytes_len": 0, "sha256": "",
                            "problems": []}
    try:
        with open(path, "rb") as fh:
            st_open = os.fstat(fh.fileno())
            meta["end"] = {"dev": st_open.st_dev, "ino": st_open.st_ino,
                           "size": st_open.st_size, "mtime_ns": st_open.st_mtime_ns}
            if (st_open.st_dev, st_open.st_ino) != (int(start["dev"]), int(start["ino"])):
                meta["problems"].append("rotation_during_request")
            elif st_open.st_size < start_off:
                meta["problems"].append("truncation_during_request")
            elif hashlib.sha256(_read_window(fh, 0, int(start["prefix_len"]))).hexdigest() \
                    != start["prefix_sha256"]:
                meta["problems"].append("prefix_rewrite_detected")
            elif hashlib.sha256(_read_window(fh, int(start["tail_offset"]),
                                             int(start["tail_len"]))).hexdigest() \
                    != start["tail_sha256"]:
                meta["problems"].append("tail_anchor_rewrite_detected")
            else:
                data = _read_window(fh, start_off, max(0, st_open.st_size - start_off))
                st_after = os.fstat(fh.fileno())
                if (st_after.st_dev, st_after.st_ino) != (st_open.st_dev, st_open.st_ino):
                    meta["problems"].append("rotation_during_read")
                elif st_after.st_size < st_open.st_size:
                    meta["problems"].append("truncation_during_read")
                meta["bytes_len"] = len(data)
                meta["sha256"] = hashlib.sha256(data).hexdigest()
                return meta, data
    except FileNotFoundError as exc:
        meta["problems"].append(f"log_missing_after_preflight: {exc}")
    except OSError as exc:
        meta["problems"].append(f"log_read_failed: {type(exc).__name__}: {exc}")
    return meta, b""


def scan_logs(logs: dict[str, str], starts: dict[str, Any], hooks: FaultHooks
              ) -> tuple[dict[str, dict[str, Any]], dict[str, bytes], list[str]]:
    slices_: dict[str, dict[str, Any]] = {}
    data: dict[str, bytes] = {}
    problems: list[str] = []
    for name, p in logs.items():
        if name not in starts:                       # vanished before its own snapshot
            problems.append(f"log {name}: log_missing_before_request")
            slices_[name] = {"start": None, "end": None, "bytes_len": 0, "sha256": "",
                             "problems": ["log_missing_before_request"]}
            data[name] = b""
            continue
        meta, raw = read_appended(p, starts[name], hooks)
        slices_[name] = meta
        data[name] = raw
        problems.extend(f"log {name}: {prob}" for prob in meta["problems"])
    return slices_, data, problems


# ---------------------------------------------------------------- completion checks

def validate_usage_counts(usage: Any, where: str = "usage") -> list[str]:
    """The ONE shared count check, used by sample completions and fault SSE usage alike."""
    problems: list[str] = []
    if not isinstance(usage, dict):
        return [f"{where} is not an object, got {type(usage).__name__}"]
    for key in ("prompt_tokens", "completion_tokens"):
        val = usage.get(key)
        if not isinstance(val, int) or isinstance(val, bool) or val < 0:
            problems.append(f"{where}.{key} is not a non-negative int: {val!r}")
    total = usage.get("total_tokens")
    if total is not None and (isinstance(total, bool) or not isinstance(total, int) or total < 0):
        problems.append(f"{where}.total_tokens is not a non-negative int: {total!r}")
    details = usage.get("prompt_tokens_details")
    if details is not None and not isinstance(details, dict):
        problems.append(f"{where}.prompt_tokens_details is not an object: {type(details).__name__}")
    elif isinstance(details, dict):
        cached = details.get("cached_tokens")
        if cached is not None and (isinstance(cached, bool)
                                   or not isinstance(cached, int) or cached < 0):
            problems.append(f"{where}.prompt_tokens_details.cached_tokens is not a "
                            f"non-negative int: {cached!r}")
    return problems


def check_completion(parsed: Any) -> list[str]:
    """Structural checks for a non-streaming chat completion, safe for any JSON shape."""
    if not isinstance(parsed, dict):
        return [f"body is not a JSON object, got {type(parsed).__name__}"]
    problems: list[str] = []
    if parsed.get("error"):
        problems.append(f"error body: {json.dumps(parsed['error'], default=str)[:300]}")
    choices = parsed.get("choices")
    if not isinstance(choices, list) or not choices:
        problems.append("no non-empty choices[] in the completion")
        return problems
    first = choices[0]
    if not isinstance(first, dict):
        problems.append(f"choices[0] is not an object, got {type(first).__name__}")
        return problems
    msg = first.get("message")
    if msg is not None and not isinstance(msg, dict):
        problems.append(f"choices[0].message is not an object, got {type(msg).__name__}")
        msg = {}
    msg = msg or {}
    content = msg.get("content")
    calls = msg.get("tool_calls")
    has_call = isinstance(calls, list) and bool(calls)
    if (not isinstance(content, str) or content == "") and not has_call:
        problems.append("empty completion: no content and no tool_calls")
    finish = first.get("finish_reason")
    if finish is None:
        problems.append("no explicit finish_reason")
    elif finish == "error":
        problems.append("finish_reason is error")
    usage = parsed.get("usage")
    if usage is None:
        problems.append("no usage object")
    else:
        problems.extend(validate_usage_counts(usage))
    return problems


def usage_view(usage: Any) -> dict[str, Any]:
    if not isinstance(usage, dict):
        return {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None,
                "cached_tokens": None, "usage_keys": [], "usage_is_object": False}
    details = usage.get("prompt_tokens_details")
    details = details if isinstance(details, dict) else {}
    return {"prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "cached_tokens": details.get("cached_tokens"),
            "usage_keys": sorted(str(k) for k in usage),
            "usage_is_object": True}


HELPER_FIELDS = ("reqs", "rows", "bytes", "timeouts", "fallbacks")


def parse_helper_tail(line: str) -> dict[str, Any] | None:
    """`helper=reqs=1 rows=4 bytes=900 timeouts=0 fallbacks=1` at a raw DONE/wrapper tail."""
    at = line.find(" helper=")
    if at < 0:
        return None
    vals: dict[str, Any] = {}
    for tok in line[at + len(" helper="):].split():
        key, sep, raw = tok.partition("=")
        if not sep:
            continue
        try:
            vals[key] = int(raw)
        except ValueError:
            vals[key] = raw
    return vals or None


def helper_line_records(text: str, provenance: str) -> list[dict[str, Any]]:
    """Every helper tail in ONE log's raw slice, in order.

    These are per-engine-call DELTAS (src/program/generate.cpp:14910-14914 snapshots the
    counters, :16155-16160 prints the differences), so this function does no arithmetic at
    all: no subtraction between lines, no lifetime claim, and no series across logs.  One
    HTTP request can carry several engine calls, so one slice can carry several records.
    """
    recs: list[dict[str, Any]] = []
    for idx, raw in enumerate(text.splitlines()):
        vals = parse_helper_tail(raw)
        if vals is None:
            continue
        recs.append({"line": raw.strip(), "line_index": idx, "provenance": provenance,
                     "per_engine_call_delta": True,
                     "counters": {k: vals.get(k) for k in HELPER_FIELDS},
                     "lifetime_cumulative": False,
                     "proves_helper_gpu_compute": False})
    return recs


# ---------------------------------------------------------------- evidence dir

def make_evidence_dir(out: str) -> Path:
    p = Path(out)
    if p.exists():
        raise FileExistsError(f"output directory {p} already exists; refusing to overwrite "
                              f"evidence; give a fresh --out path")
    p.mkdir(parents=True)
    return p


def write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def tool_identity() -> dict[str, Any]:
    return {"tool": "tools/mixed_gpu/acceptance.py", "tool_version": TOOL_VERSION,
            "python": sys.version.split()[0], "argv": list(sys.argv)}


def write_chunks_index(path: Path, chunks: list[tuple[float, float, bytes]]) -> None:
    lines = [json.dumps({"i": i, "mono": m, "wall": w, "wall_utc": _utc_stamp(w),
                         "len": len(c), "sha256": hashlib.sha256(c).hexdigest()})
             for i, (m, w, c) in enumerate(chunks)]
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")


# ---------------------------------------------------------------- sample

def record_exchange(label: str, kind: str, index: int, out_dir: Path, host: str, port: int,
                    path: str, req_bytes: bytes, logs: dict[str, str], hooks: FaultHooks,
                    timeout: float) -> dict[str, Any]:
    """One request -> full request/response/timing/log-slice record, saved before reporting."""
    rec_dir = out_dir / f"{label}-{index:03d}"
    rec_dir.mkdir(parents=True, exist_ok=True)
    (rec_dir / "request.json").write_bytes(req_bytes)

    starts, pre_problems = snapshot_logs(logs, hooks)
    t_wall_start, t_mono_start = hooks.wall(), hooks.mono()

    chunks: list[tuple[float, float, bytes]] = []
    info: dict[str, Any] = {"status": None, "reason": None, "headers": []}
    transport_error: str | None = None
    gen: Any = None
    try:
        info, gen = hooks.transport(host, port, path, timeout, req_bytes, hooks.connect)
        for item in gen:
            chunks.append(item)
    except Exception as exc:                              # connect / head / mid-stream failure
        transport_error = f"{type(exc).__name__}: {exc}"
    finally:
        if gen is not None and hasattr(gen, "close"):
            gen.close()                                   # runs the transport's conn.close()
    body = b"".join(c[2] for c in chunks)
    t_wall_finish, t_mono_finish = hooks.wall(), hooks.mono()

    (rec_dir / "response.bin").write_bytes(body)
    write_chunks_index(rec_dir / "response_chunks.jsonl", chunks)

    problems: list[str] = []
    parsed: Any = None
    if transport_error:
        problems.append(f"http transport failed: {transport_error}")
    elif info.get("status") is None:
        problems.append("no HTTP status received")
    elif not (200 <= int(info["status"]) < 300):
        problems.append(f"http status {info['status']} {info.get('reason')}")
    if not problems:
        parse_ok = False
        try:
            parsed = json.loads(body.decode("utf-8"))
            parse_ok = True
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            problems.append(f"body is not JSON: {exc}")
        if parse_ok:
            problems.extend(check_completion(parsed))      # a JSON null body is checked too

    log_slices, log_data, log_problems = scan_logs(logs, starts, hooks)
    log_problems = pre_problems + log_problems
    for name, raw in log_data.items():
        (rec_dir / f"log-{name}.slice.bin").write_bytes(raw)
    problems.extend(log_problems)

    helper_lines: list[dict[str, Any]] = []
    for name in logs:                      # per log, never merged into one series
        helper_lines.extend(helper_line_records(log_data[name].decode("utf-8", "replace"), name))

    record: dict[str, Any] = {
        "label": label, "kind": kind, "index": index,
        "endpoint": f"http://{host}:{port}{path}",
        "request_sha256": hashlib.sha256(req_bytes).hexdigest(),
        "request_bytes_len": len(req_bytes),
        "http": {"status": info.get("status"), "reason": info.get("reason"),
                 "headers": info.get("headers", []), "transport_error": transport_error},
        "response_sha256": hashlib.sha256(body).hexdigest(),
        "response_bytes_len": len(body),
        "timing": {"wall_start": t_wall_start, "wall_start_utc": _utc_stamp(t_wall_start),
                   "wall_finish": t_wall_finish, "wall_finish_utc": _utc_stamp(t_wall_finish),
                   "mono_start": t_mono_start, "mono_finish": t_mono_finish,
                   "duration_mono_s": t_mono_finish - t_mono_start,
                   "duration_wall_s": t_wall_finish - t_wall_start,
                   "first_chunk_mono": chunks[0][0] if chunks else None,
                   "first_chunk_wall": chunks[0][1] if chunks else None,
                   "chunks": len(chunks)},
        "log_slices": log_slices,
        "helper_lines": helper_lines,
        "problems": problems,
    }
    if parsed is not None and isinstance(parsed, dict):
        choices = parsed.get("choices")
        choices = choices if isinstance(choices, list) else []
        first = choices[0] if choices and isinstance(choices[0], dict) else {}
        msg = first.get("message")
        msg = msg if isinstance(msg, dict) else {}
        text = msg.get("content")
        record["parsed_json"] = parsed
        record["usage"] = usage_view(parsed.get("usage"))
        record["finish_reason"] = first.get("finish_reason")
        record["output_text_len"] = len(text) if isinstance(text, str) else None
        record["output_text_sha256"] = (hashlib.sha256(text.encode()).hexdigest()
                                         if isinstance(text, str) else None)
    write_json(rec_dir / "record.json", record)
    return record


def cmd_sample(a: argparse.Namespace, hooks: FaultHooks) -> int:
    try:
        host, port, path = parse_endpoint(a.url)
        req_raw = Path(a.request).read_bytes()
        check_sampling_pin(req_raw, stream=False)
        logs = parse_logs(a.log)
        check_args(a, logs)
        out_dir = make_evidence_dir(a.out)
    except (ValueError, FileExistsError, OSError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    declared = {"declared_by": "operator command line", "measured_by_this_tool": False,
                "phase": a.phase, "chain_label": a.chain, "build_id": a.build_id,
                "helper_state": a.helper_state, "comparable": False,
                "note": "cold/warm, chain, build and helper state are declarations; fresh "
                        "same-build OFF/ON chains are the parent's responsibility; no A/B "
                        "claim is made here"}
    manifest = {**tool_identity(), "command": "sample", "declared": declared,
                "endpoint": f"http://{host}:{port}{path}", "warmups": a.warmups,
                "samples": a.samples,
                "request_file": str(Path(a.request).resolve()),
                "request_sha256": hashlib.sha256(req_raw).hexdigest(),
                "sampling_pin": {"temperature": 0, "seed_source": "request JSON, verbatim",
                                 "request_bytes_modified_by_tool": False},
                "records": [], "problems": []}
    write_json(out_dir / "manifest.json", manifest)

    problems: list[str] = []
    for i in range(1, a.warmups + 1):
        rec = record_exchange("warmup", "warmup", i, out_dir, host, port, path, req_raw,
                              logs, hooks, a.timeout)
        manifest["records"].append({"kind": "warmup", "index": i, "problems": rec["problems"],
                                    "record_dir": f"warmup-{i:03d}",
                                    "response_sha256": rec["response_sha256"],
                                    "usage": rec.get("usage")})
        problems.extend(f"warmup {i}: {p}" for p in rec["problems"])
    for i in range(1, a.samples + 1):
        rec = record_exchange("sample", "sample", i, out_dir, host, port, path, req_raw,
                              logs, hooks, a.timeout)
        manifest["records"].append({"kind": "sample", "index": i, "problems": rec["problems"],
                                    "record_dir": f"sample-{i:03d}",
                                    "response_sha256": rec["response_sha256"],
                                    "usage": rec.get("usage")})
        problems.extend(f"sample {i}: {p}" for p in rec["problems"])

    manifest["problems"] = problems
    write_json(out_dir / "manifest.json", manifest)
    print(f"evidence: {out_dir}")
    if problems:
        print(f"EVIDENCE INVALID: {len(problems)} problem(s); raw artifacts saved", file=sys.stderr)
        for p in problems[:10]:
            print(f"  - {p}", file=sys.stderr)
        return EXIT_EVIDENCE_BAD
    return EXIT_OK


# ---------------------------------------------------------------- fault: identity

def verify_helper_identity(pid: int, exe: str, starttime: int, hooks: FaultHooks,
                           bound: dict[str, Any] | None = None) -> dict[str, Any]:
    """Bind PID -> the inode of `/proc/<pid>/exe` ITSELF (via hooks.stat_path) -> starttime.

    The pathname the symlink points at is recorded for reading only: the identity is the
    dev+ino of the stat on /proc/<pid>/exe, so a PID reused by another process, a moved
    binary, or an unlinked binary is caught.  With `bound`, the same checks re-run and any
    change is a problem: that is the pre-signal recheck.
    """
    report: dict[str, Any] = {"pid": pid, "exe_declared": str(exe),
                              "starttime_declared": starttime, "problems": []}
    if pid <= 0:
        report["problems"].append(f"pid {pid} is not a positive pid")
        return report
    if not hooks.proc_exists(pid):
        report["problems"].append(f"/proc/{pid} does not exist")
        return report
    exe_path = Path(exe).resolve()
    if not exe_path.is_file():
        report["problems"].append(f"declared exe {exe_path} is not a file")
        return report
    try:
        st_proc = hooks.stat_path(f"/proc/{pid}/exe")          # the procfs exe inode itself
        st_exe = hooks.stat_path(str(exe_path))
    except OSError as exc:
        report["problems"].append(f"cannot stat /proc/{pid}/exe: {exc}")
        return report
    report["proc_exe_dev"], report["proc_exe_ino"] = st_proc.st_dev, st_proc.st_ino
    report["declared_exe_dev"], report["declared_exe_ino"] = st_exe.st_dev, st_exe.st_ino
    report["proc_exe_current"] = str(Path(exe).resolve())
    try:
        report["proc_exe_current"] = os.readlink(f"/proc/{pid}/exe")   # informational only
    except OSError as exc:
        report["proc_exe_current"] = f"unreadable: {exc}"
    if (st_proc.st_dev, st_proc.st_ino) != (st_exe.st_dev, st_exe.st_ino):
        report["problems"].append(f"/proc/{pid}/exe inode ({st_proc.st_dev}:{st_proc.st_ino}) "
                                  f"is not the declared exe {exe_path} "
                                  f"({st_exe.st_dev}:{st_exe.st_ino}); pid reuse, a moved "
                                  f"binary or an unlinked binary")
    try:
        stat_txt = hooks.read_proc(pid, "stat").decode("utf-8", "replace")
        fields = stat_txt[stat_txt.rfind(")") + 1:].split()
        got = int(fields[19])                     # starttime = field 22 of stat (1-based)
        report["proc_starttime"] = got
        if got != starttime:
            report["problems"].append(f"/proc/{pid}/stat starttime is {got}, declared {starttime}")
    except (OSError, IndexError, ValueError) as exc:
        report["problems"].append(f"cannot read starttime from /proc/{pid}/stat: {exc}")
    report["bound_expected"] = {"dev": st_proc.st_dev, "ino": st_proc.st_ino,
                               "starttime": starttime}
    if bound is not None:
        if (st_proc.st_dev, st_proc.st_ino) != (bound["dev"], bound["ino"]):
            report["problems"].append(f"/proc/{pid}/exe inode changed since binding "
                                      f"({bound['dev']}:{bound['ino']} -> "
                                      f"{st_proc.st_dev}:{st_proc.st_ino})")
        if report.get("proc_starttime") != bound["starttime"]:
            report["problems"].append(f"/proc/{pid} starttime changed since binding "
                                      f"{bound['starttime']} -> {report.get('proc_starttime')}")
    return report


# ---------------------------------------------------------------- fault: SSE

def scan_sse(buf: bytes, scan_from: int) -> tuple[list[dict[str, Any]], int]:
    """Complete `data:` events from scan_from; an unterminated tail stays unconsumed."""
    events: list[dict[str, Any]] = []
    pos = scan_from
    while True:
        nl = buf.find(b"\n", pos)
        if nl < 0:
            return events, pos
        line = buf[pos:nl].strip()
        pos = nl + 1
        if not line:
            continue
        if not line.startswith(b"data:"):
            continue                                   # comments/heartbeats stay in the raw
        payload = line[5:].strip()
        if payload == b"[DONE]":
            events.append({"__done__": True})
            continue
        try:
            obj = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            events.append({"__bad_json__": payload[:200].decode("utf-8", "replace")})
            continue
        if isinstance(obj, dict):
            events.append(obj)
        else:
            events.append({"__non_object__": type(obj).__name__,
                           "value": repr(obj)[:200]})


def classify_event(ev: dict[str, Any]) -> tuple[str, bool]:
    """(kind, terminal).  Terminal events end the stream: they must never be killed."""
    if ev.get("__done__"):
        return "done", True
    if ev.get("__bad_json__"):
        return "bad_json", True
    if "__non_object__" in ev:
        return "non_object", True
    if ev.get("error"):
        return "error", True
    choices = ev.get("choices")
    if choices is not None and not isinstance(choices, list):
        return "bad_choices", True
    for ch in choices or []:
        if isinstance(ch, dict) and ch.get("finish_reason"):
            return "finish", True
    return "delta", False


def is_nonempty_delta(ev: dict[str, Any]) -> bool:
    """The trigger's ONLY content test: a non-empty content/reasoning delta string."""
    choices = ev.get("choices")
    if not isinstance(choices, list):
        return False
    for ch in choices:
        if not isinstance(ch, dict):
            continue
        d = ch.get("delta")
        if not isinstance(d, dict):
            continue
        for key in ("content", "reasoning_content", "reasoning"):
            val = d.get(key)
            if isinstance(val, str) and val != "":
                return True
    return False


def stream_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    content: list[str] = []
    reasoning: list[str] = []
    tool_calls = 0
    finish_reasons: list[str] = []
    usage_final: Any = None
    done_seen = False
    problems: list[str] = []
    for ev in events:
        kind, _ = classify_event(ev)
        if kind == "done":
            done_seen = True
            continue
        if kind == "bad_json":
            problems.append(f"unparseable SSE data payload: {ev['__bad_json__']!r}")
            continue
        if kind == "non_object":
            problems.append(f"SSE data payload is not an object: {ev['__non_object__']}")
            continue
        if kind == "bad_choices":
            problems.append("SSE event choices is not a list")
            continue
        if ev.get("error"):
            problems.append(f"stream carried an error object: {json.dumps(ev['error'], default=str)[:200]}")
            continue
        if ev.get("usage") is not None:
            usage_final = ev["usage"]
        for ch in ev.get("choices") or []:
            if not isinstance(ch, dict):
                problems.append(f"SSE choices entry is not an object: {type(ch).__name__}")
                continue
            d = ch.get("delta")
            d = d if isinstance(d, dict) else {}
            if isinstance(d.get("content"), str):
                content.append(d["content"])
            if isinstance(d.get("reasoning_content"), str):
                reasoning.append(d["reasoning_content"])
            if isinstance(d.get("reasoning"), str):
                reasoning.append(d["reasoning"])
            if d.get("tool_calls"):
                tool_calls += 1
            if ch.get("finish_reason"):
                finish_reasons.append(str(ch["finish_reason"]))
    return {"content": "".join(content), "reasoning": "".join(reasoning),
            "tool_call_deltas": tool_calls, "finish_reasons": finish_reasons,
            "usage_final": usage_final, "done_seen": done_seen, "problems": problems,
            "events": len(events)}


# ---------------------------------------------------------------- fault

def cmd_fault(a: argparse.Namespace, hooks: FaultHooks) -> int:
    """One streaming request, pidfd SIGKILL anchored to a trigger, raw evidence out."""
    try:
        host, port, path = parse_endpoint(a.url)
        req_raw = Path(a.request).read_bytes()
        check_sampling_pin(req_raw, stream=True)
        logs = parse_logs(a.log)
        check_args(a, logs)
        out_dir = make_evidence_dir(a.out)
    except (ValueError, FileExistsError, OSError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    include_usage = bool((json.loads(req_raw).get("stream_options") or {}).get("include_usage"))
    manifest: dict[str, Any] = {
        **tool_identity(), "command": "fault",
        "endpoint": f"http://{host}:{port}{path}",
        "declared": {"declared_by": "operator command line", "measured_by_this_tool": False,
                     "chain_label": a.chain, "build_id": a.build_id},
        "request_file": str(Path(a.request).resolve()),
        "request_sha256": hashlib.sha256(req_raw).hexdigest(),
        "trigger_declared": a.trigger, "marker": a.marker,
        "include_usage_requested": include_usage,
        "phase_proven": False, "signal_sent": False, "problems": []}

    def finish(code: int) -> int:
        write_json(out_dir / "manifest.json", manifest)
        print(f"evidence: {out_dir}")
        return code

    (out_dir / "request.json").write_bytes(req_raw)
    starts, pre_problems = snapshot_logs(logs, hooks)
    manifest["problems"].extend(pre_problems)
    log_disarmed: list[str | None] = [pre_problems[0] if pre_problems else None]
    manifest["log_disarmed"] = log_disarmed[0]

    # --- pidfd first, then identity binding: no signal is ever sent on an unbound pid.
    try:
        pidfd = hooks.pidfd_open(a.pid)
    except PidfdUnavailable as exc:
        manifest["problems"].append(f"pidfd: {exc}")
        manifest["verdict"] = "INCONCLUSIVE: pidfd unavailable, no signal sent"
        print(f"INCONCLUSIVE: {exc}; refusing to fall back to kill/pkill/os.kill", file=sys.stderr)
        return finish(EXIT_NOT_ANCHORED)
    manifest["pidfd_opened"] = True

    def close_fd() -> None:
        try:
            hooks.pidfd_close(pidfd)
        except OSError as exc:
            manifest["problems"].append(f"pidfd close failed: {exc}")

    ident = verify_helper_identity(a.pid, a.exe, a.starttime, hooks)
    manifest["helper_identity"] = ident
    if ident["problems"]:
        manifest["problems"].extend("helper identity: " + p for p in ident["problems"])
        manifest["verdict"] = "INCONCLUSIVE: helper identity not verified, no signal sent"
        close_fd()
        print("INCONCLUSIVE: helper identity not verified; nothing was signalled", file=sys.stderr)
        return finish(EXIT_NOT_ANCHORED)
    bound = dict(ident["bound_expected"])

    chunks: list[tuple[float, float, bytes]] = []
    buf = b""
    scan_from = 0
    trigger: dict[str, Any] | None = None
    killed: dict[str, Any] | None = None
    stream_complete = False
    terminal_events: list[dict[str, Any]] = []
    transport_error: str | None = None
    deadline_hit = False
    refused_no_head = False
    info: dict[str, Any] = {"status": None, "reason": None, "headers": []}
    t_wall_start, t_mono_start = hooks.wall(), hooks.mono()
    deadline = t_mono_start + a.timeout
    q: "queue.Queue[tuple[str, Any]]" = queue.Queue()
    stop = threading.Event()

    def reader() -> None:
        nonlocal info
        try:
            conn_info, gen = hooks.transport(host, port, path, a.timeout, req_raw, hooks.connect)
            gens.append(gen)
            # THE SAME dict the transport mutates when the head arrives: a 200 head is visible
            # to the main loop (and to fire_kill) while the body is still blocked.
            info = conn_info
            for item in gen:
                if stop.is_set():
                    break
                q.put(("chunk", item))
            q.put(("eof", None))
        except Exception as exc:
            q.put(("error", f"{type(exc).__name__}: {exc}"))
        finally:
            for g in gens:                   # EOF, error and early stop all clean up
                if hasattr(g, "close"):
                    g.close()

    def watcher() -> None:
        """Watches the log slices for the marker INDEPENDENTLY of token arrival.

        Only complete lines (terminated by a newline) are matched, so a marker split across
        write chunks is matched once, on the line that finishes it - never twice.  A log
        problem (rotation, truncation, rewrite, vanished) disarms the fault: the evidence the
        trigger depends on is gone, so no signal is sent.
        """
        watched = {n: s for n, s in starts.items() if n in logs}
        consumed = {name: 0 for name in watched}
        carry = {name: "" for name in watched}
        try:
            while not stop.is_set():
                for name, start in watched.items():
                    meta, raw = read_appended(logs[name], start, hooks)
                    for prob in meta["problems"]:
                        q.put(("watcher_log_problem", f"log {name}: {prob}"))
                    new = carry[name] + raw[consumed[name]:].decode("utf-8", "replace")
                    consumed[name] = len(raw)
                    parts = new.split("\n")
                    carry[name] = parts.pop()               # the unterminated tail, kept
                    for line in parts:
                        if a.marker in line:
                            q.put(("marker", {"kind": "log_marker", "marker": a.marker,
                                              "log": name, "line": line,
                                              "mono": hooks.mono(), "wall": hooks.wall(),
                                              "wall_utc": _utc_stamp(hooks.wall())}))
                hooks.sleep(a.marker_poll)
        except Exception as exc:
            q.put(("watcher_error", f"{type(exc).__name__}: {exc}"))
        finally:
            for name in watched:
                if carry[name]:
                    q.put(("watcher_partial_line",
                           f"log {name}: unterminated tail {carry[name][:80]!r}"))

    def fire_kill(cause: str, detail: str) -> None:
        """Recheck identity, then SIGKILL.  signal_sent is true ONLY on a successful send."""
        nonlocal killed
        recheck = verify_helper_identity(a.pid, a.exe, a.starttime, hooks, bound)
        manifest["identity_recheck"] = recheck
        if recheck["problems"]:
            killed = {"signal": "SIGKILL", "cause": cause, "detail": detail,
                      "sent": False, "refused_before_send": "identity recheck failed",
                      "recheck_problems": recheck["problems"], "signal_count_as_term": False}
            manifest["kill"] = killed
            manifest["problems"].append("kill refused: identity recheck failed "
                                        "(pid reuse, moved/unlinked binary, restart)")
            stop.set()
            return
        pre_wall, pre_mono = hooks.wall(), hooks.mono()
        status_now = info.get("status")
        if status_now is None:
            killed = {"signal": "SIGKILL", "cause": cause, "detail": detail, "sent": False,
                      "refused_before_send": "no HTTP status known yet",
                      "signal_count_as_term": False}
            manifest["kill"] = killed
            manifest["problems"].append("kill refused: no HTTP status received; a stream with "
                                        "no valid head is never signalled")
            stop.set()
            return
        if not (200 <= int(status_now) < 300):
            killed = {"signal": "SIGKILL", "cause": cause, "detail": detail, "sent": False,
                      "refused_before_send": f"http status {status_now} is not 2xx",
                      "signal_count_as_term": False}
            manifest["kill"] = killed
            manifest["problems"].append(f"kill refused: the stream is http {status_now}, "
                                        f"an invalid response is never signalled")
            stop.set()
            return
        if log_disarmed[0]:
            killed = {"signal": "SIGKILL", "cause": cause, "detail": detail, "sent": False,
                      "refused_before_send": f"log evidence lost: {log_disarmed[0]}",
                      "signal_count_as_term": False}
            manifest["kill"] = killed
            manifest["problems"].append("kill refused: the log evidence the fault is anchored "
                                        "to is broken; nothing is signalled")
            stop.set()
            return
        try:
            hooks.pidfd_kill(pidfd)
        except Exception as exc:
            post_wall, post_mono = hooks.wall(), hooks.mono()
            killed = {"signal": "SIGKILL", "cause": cause, "detail": detail, "sent": False,
                      "syscall_wall_before": pre_wall, "syscall_mono_before": pre_mono,
                      "syscall_wall_after": post_wall, "syscall_mono_after": post_mono,
                      "error": f"{type(exc).__name__}: {exc}", "signal_count_as_term": False}
            manifest["kill"] = killed
            manifest["problems"].append(f"SIGKILL send failed: {killed['error']}")
            stop.set()
            return
        post_wall, post_mono = hooks.wall(), hooks.mono()
        killed = {"signal": "SIGKILL", "cause": cause, "detail": detail, "sent": True,
                  "syscall_wall_before": pre_wall, "syscall_wall_before_utc": _utc_stamp(pre_wall),
                  "syscall_mono_before": pre_mono,
                  "syscall_wall_after": post_wall, "syscall_wall_after_utc": _utc_stamp(post_wall),
                  "syscall_mono_after": post_mono,
                  "syscall_duration_mono_s": post_mono - pre_mono,
                  "signal_count_as_term": False, "error": None}
        manifest["kill"] = killed
        manifest["signal_sent"] = True
        # The pidfd poll runs NOW, before the stream is drained.  The poll START is only a
        # lower bound; the observation stamp is taken AFTER the poll returns, because the call
        # blocks until the event arrives (or the timeout expires).
        poll_start_mono, poll_start_wall = hooks.mono(), hooks.wall()
        exit_event = hooks.pidfd_death(pidfd, a.death_timeout)
        poll_end_mono, poll_end_wall = hooks.mono(), hooks.wall()
        proc_at_death = hooks.proc_exists(a.pid)
        manifest["death"] = {
            "poll_start_mono": poll_start_mono,
            "poll_start_wall_utc": _utc_stamp(poll_start_wall),
            "observed_at_mono": poll_end_mono, "observed_at_wall": poll_end_wall,
            "observed_at_wall_utc": _utc_stamp(poll_end_wall),
            "poll_duration_mono_s": poll_end_mono - poll_start_mono,
            "observed_after_syscall_mono_s": poll_end_mono - post_mono,
            "syscall_to_observation_mono_s": poll_end_mono - post_mono,
            "pidfd_event": exit_event,
            "exit_proven_by_pidfd": bool(exit_event),
            "proc_present_at_death": proc_at_death,
            "proc_absent_at_death": not proc_at_death,
            "confirmed": bool(exit_event),
            "note": ("POLLIN on the pidfd proves the process EXITED; it does not prove the "
                     "parent reaped it.  observed_at_* is the time the poll RETURNED, i.e. the "
                     "earliest moment this tool can know about the exit; poll_start_mono is "
                     "only the lower bound.  /proc presence at that moment is a separate "
                     "diagnostic: a present /proc with POLLIN means exited, not yet reaped."),
        }

    threads: list[threading.Thread] = []
    gens: list[Any] = []
    try:
        reader_thread = threading.Thread(target=reader, daemon=True, name="acceptance-reader")
        reader_thread.start()
        threads.append(reader_thread)
        if a.trigger == "log-marker":
            watch_thread = threading.Thread(target=watcher, daemon=True, name="acceptance-marker")
            watch_thread.start()
            threads.append(watch_thread)

        while True:
            if refused_no_head:
                break                                   # refused blind: end, no signal, ever
            if hooks.mono() > deadline:
                deadline_hit = True
                break
            try:
                kind, payload = q.get(timeout=max(0.01, a.marker_poll))
            except queue.Empty:
                continue
            if kind == "eof":
                break
            if kind == "error":
                transport_error = str(payload)
                break
            if kind == "watcher_error":
                manifest["problems"].append(f"marker watcher failed: {payload}")
                log_disarmed[0] = log_disarmed[0] or f"watcher error: {payload}"
                continue
            if kind == "watcher_log_problem":
                manifest["problems"].append(str(payload))
                log_disarmed[0] = log_disarmed[0] or str(payload)
                continue
            if kind == "watcher_partial_line":
                manifest["watcher_partial_lines"] = manifest.get(
                    "watcher_partial_lines", []) + [str(payload)]
                continue
            if kind == "marker":
                if stream_complete:
                    manifest["marker_after_completion_ignored"] = payload
                    continue
                if log_disarmed[0]:
                    manifest["markers_after_log_loss_ignored"] = manifest.get(
                        "markers_after_log_loss_ignored", []) + [payload]
                    continue
                if trigger is not None:
                    manifest["extra_markers_ignored"] = manifest.get(
                        "extra_markers_ignored", []) + [payload]
                    continue
                if info.get("status") is None:
                    # NO head has arrived, so nothing about this response is known.  The fault
                    # is refused for this request and is never re-armed later: a marker seen
                    # blind must not become a signal after the fact.
                    manifest["marker_refused_unknown_status"] = dict(payload)
                    manifest["problems"].append(
                        "marker seen with no HTTP head: refused as inconclusive, no signal is "
                        "sent for this request, later, even if a head arrives")
                    stream_complete = True        # terminal for this request: never killed
                    refused_no_head = True
                    stop.set()
                    continue
                trigger = dict(payload)
                fire_kill("log_marker", json.dumps(payload, default=str)[:400])
                continue
            mono, wall, chunk = payload
            chunks.append((mono, wall, chunk))
            buf += chunk
            if stream_complete:
                continue                                   # drain, never re-arm
            events, scan_from = scan_sse(buf, scan_from)
            # A batch is examined whole before any signal: if the bytes already in hand
            # carry a terminal event, this stream is complete and is never killed.
            terminal_here = [(i, ev, classify_event(ev)) for i, ev in enumerate(events)]
            hit = next(((i, ev, kind) for i, ev, (kind, term) in terminal_here if term), None)
            if hit is not None:
                idx, ev, k = hit
                stream_complete = True
                terminal_events.append({"type": k, "event": ev, "mono": mono, "wall": wall,
                                        "batch_position": idx,
                                        "same_batch_also_had_deltas": any(
                                            is_nonempty_delta(e) for e in events[:idx]),
                                        "killed": False})
                continue
            if a.trigger == "first-delta" and trigger is None:
                for ev in events:
                    if is_nonempty_delta(ev):
                        trigger = {"kind": "first_nonempty_delta", "mono": mono, "wall": wall,
                                   "wall_utc": _utc_stamp(wall), "bytes_seen": len(buf),
                                   "event": ev}
                        fire_kill("first_nonempty_delta", json.dumps(ev, default=str)[:400])
                        break
        if deadline_hit:
            manifest["problems"].append("deadline exceeded before the stream ended")
    finally:
        stop.set()
        for th in threads:
            th.join(timeout=5.0)
        # Late watcher metadata is drained after the joins so the manifest reports what the
        # watcher saw.  Drain records ONLY metadata; a drained marker NEVER fires a signal.
        while True:
            try:
                kind, payload = q.get_nowait()
            except queue.Empty:
                break
            if kind == "watcher_log_problem":
                manifest["problems"].append(str(payload))
                log_disarmed[0] = log_disarmed[0] or str(payload)
            elif kind == "watcher_error":
                manifest["problems"].append(f"marker watcher failed: {payload}")
                log_disarmed[0] = log_disarmed[0] or f"watcher error: {payload}"
            elif kind == "watcher_partial_line":
                manifest["watcher_partial_lines"] = manifest.get(
                    "watcher_partial_lines", []) + [str(payload)]
            elif kind == "marker":
                manifest["markers_after_end_ignored"] = manifest.get(
                    "markers_after_end_ignored", []) + [payload]
        close_fd()

    # --- saved artifacts, on every path
    manifest["log_disarmed"] = log_disarmed[0]
    (out_dir / "response_sse.bin").write_bytes(buf)
    write_chunks_index(out_dir / "sse_chunks.jsonl", chunks)
    log_slices, log_data, log_problems = scan_logs(logs, starts, hooks)
    for name, raw in log_data.items():
        (out_dir / f"log-{name}.slice.bin").write_bytes(raw)
    manifest["log_slices"] = log_slices
    manifest["log_problems"] = log_problems

    status = info.get("status")
    manifest["http"] = {"status": status, "reason": info.get("reason"),
                        "headers": info.get("headers", []), "transport_error": transport_error}
    manifest["timing"] = {"wall_start": t_wall_start, "wall_finish": hooks.wall(),
                          "wall_finish_utc": _utc_stamp(hooks.wall()),
                          "mono_start": t_mono_start, "mono_finish": hooks.mono(),
                          "chunks": len(chunks), "response_bytes": len(buf),
                          "first_chunk_mono": chunks[0][0] if chunks else None,
                          "deadline_hit": deadline_hit}

    all_events, _ = scan_sse(buf, 0)
    summ = stream_summary(all_events)
    text = summ["content"]
    manifest["stream"] = {"events": summ["events"], "content_len": len(text),
                          "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
                          "reasoning_len": len(summ["reasoning"]),
                          "tool_call_deltas": summ["tool_call_deltas"],
                          "finish_reasons": summ["finish_reasons"],
                          "done_seen": summ["done_seen"],
                          "usage_final": summ["usage_final"],
                          "usage": usage_view(summ["usage_final"]),
                          "terminal_events": terminal_events}
    (out_dir / "output.txt").write_text(text, encoding="utf-8")
    for name in logs:
        manifest.setdefault("helper_lines", []).extend(
            helper_line_records(log_data[name].decode("utf-8", "replace"), name))

    sse_problems = list(summ["problems"])
    if transport_error:
        sse_problems.append(f"http transport failed: {transport_error}")
    if deadline_hit:
        sse_problems.append("stream did not end within the deadline")
    if status is not None and not (200 <= int(status) < 300):
        sse_problems.append(f"http status {status}")
    if not summ["finish_reasons"]:
        sse_problems.append("no finish_reason observed in the stream")
    elif "error" in summ["finish_reasons"]:
        sse_problems.append("finish_reason is error")
    if not summ["done_seen"]:
        sse_problems.append("no data: [DONE] terminator observed")
    if not text and not summ["reasoning"] and not summ["tool_call_deltas"]:
        sse_problems.append("stream produced no content, no reasoning and no tool_calls")
    if include_usage and summ["usage_final"] is None:
        sse_problems.append("include_usage requested but no usage object arrived")
    if summ["usage_final"] is not None:
        sse_problems.extend(validate_usage_counts(summ["usage_final"], "stream usage"))
    sse_problems.extend(log_problems)          # log rotation/truncation/loss fails closed too
    manifest["response_problems"] = sse_problems
    manifest["problems"].extend("response: " + p for p in sse_problems)

    manifest["phase_proven"] = False
    manifest["phase_note"] = ("a first non-empty delta does not prove the engine was still "
                              "decoding (it can be buffered), and a log marker does not prove "
                              "a phase; the engine/helper fallback evidence is the raw slice, "
                              "read by the parent")

    # --- verdict, keeping signal truth and completion truth apart
    if killed is not None and killed.get("sent") is not True:
        reason = killed.get("refused_before_send") or killed.get("error")
        manifest["trigger_fired"] = trigger
        manifest["verdict"] = f"INCONCLUSIVE: SIGKILL not sent ({reason}); raw artifacts saved"
        print(f"INCONCLUSIVE: SIGKILL not sent ({reason})", file=sys.stderr)
        return finish(EXIT_NOT_ANCHORED)

    if killed is None:
        manifest["trigger_fired"] = None
        if stream_complete and terminal_events:
            manifest["problems"].append("fault not anchored: the stream reached a terminal "
                                        "event (finish_reason/[DONE]/error/malformed) before "
                                        "any trigger; a completed request is never killed")
        if refused_no_head:
            manifest["problems"].append("fault refused: a marker arrived with no HTTP head; "
                                        "this request is never signalled, later, either")
        manifest["problems"].append("fault not anchored: no trigger fired")
        manifest["verdict"] = "INCONCLUSIVE: no signal sent"
        print("INCONCLUSIVE: trigger never fired; nothing was signalled", file=sys.stderr)
        return finish(EXIT_NOT_ANCHORED)

    manifest["trigger_fired"] = trigger
    manifest["proc_absent_at_end"] = not hooks.proc_exists(a.pid)
    if not manifest["death"]["confirmed"]:
        manifest["verdict"] = ("INCONCLUSIVE: SIGKILL sent, exit not confirmed by the pidfd "
                              "event; raw artifacts saved")
        print("INCONCLUSIVE: exit not confirmed by the pidfd event", file=sys.stderr)
        return finish(EXIT_NOT_ANCHORED)
    if sse_problems:
        manifest["verdict"] = ("INCONCLUSIVE: SIGKILL sent with exit confirmed, request "
                               "response invalid; raw artifacts saved")
        print("INCONCLUSIVE: request did not finish as a valid response", file=sys.stderr)
        return finish(EXIT_EVIDENCE_BAD)
    manifest["verdict"] = ("RAW EVIDENCE ONLY: SIGKILL anchored to the trigger, exit "
                           "confirmed, request finished with a valid response; phase not proven")
    return finish(EXIT_OK)


# ---------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="acceptance.py",
                                 description="raw acceptance evidence for the mixed GPU runs")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sample", help="record pinned non-streaming requests, loopback only")
    s.add_argument("--url", default=f"http://127.0.0.1:{DEFAULT_PORT}/v1/chat/completions")
    s.add_argument("--request", required=True, help="path to the request JSON (sent verbatim)")
    s.add_argument("--out", required=True, help="fresh evidence directory (must not exist)")
    s.add_argument("--phase", required=True, choices=["cold", "warm"])
    s.add_argument("--chain", required=True, help="operator label for the chain (declaration)")
    s.add_argument("--build-id", required=True, help="operator label for the build (declaration)")
    s.add_argument("--helper-state", required=True, choices=["off", "on", "unknown"])
    s.add_argument("--samples", type=int, default=1)
    s.add_argument("--warmups", type=int, default=0)
    s.add_argument("--log", action="append", default=[], help="label=path, repeatable")
    s.add_argument("--timeout", type=float, default=600.0)

    f = sub.add_parser("fault", help="pidfd SIGKILL anchored to a live streaming request")
    f.add_argument("--url", default=f"http://127.0.0.1:{DEFAULT_PORT}/v1/chat/completions")
    f.add_argument("--request", required=True)
    f.add_argument("--out", required=True)
    f.add_argument("--pid", type=int, required=True)
    f.add_argument("--exe", required=True, help="canonical helper exe path to bind the pid to")
    f.add_argument("--starttime", type=int, required=True, help="/proc/<pid>/stat starttime field")
    f.add_argument("--trigger", required=True, choices=["first-delta", "log-marker"])
    f.add_argument("--marker", help="log line marker (trigger=log-marker)")
    f.add_argument("--marker-poll", type=float, default=0.05)
    f.add_argument("--log", action="append", default=[], help="label=path, repeatable")
    f.add_argument("--chain", default="undeclared")
    f.add_argument("--build-id", default="undeclared")
    f.add_argument("--timeout", type=float, default=600.0)
    f.add_argument("--death-timeout", type=float, default=10.0)
    return ap


def main(argv: list[str] | None = None, hooks: FaultHooks | None = None) -> int:
    a = build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    h = hooks or default_fault_hooks()
    if a.cmd == "sample":
        return cmd_sample(a, h)
    h.death_timeout = a.death_timeout
    h.marker_poll = a.marker_poll
    return cmd_fault(a, h)


if __name__ == "__main__":
    sys.exit(main())
