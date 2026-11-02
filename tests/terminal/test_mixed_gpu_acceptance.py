"""CPU-only tests for tools/mixed_gpu/acceptance.py (raw mixed-GPU acceptance evidence).

Everything is fake-transport / fake-procfs: no real network, no service, no GPU, no engine.
`real_transport` IS exercised here (its own code runs) against a mocked HTTPConnection /
HTTPResponse, so the read1 path, the post-byte stamps and the connection cleanup are tested,
not bypassed.  The one real process touched is a CPU dummy spawned by this suite and owned by
it (python -c sleep), killed through pidfd.

The pinned contract these tests enforce:

  transport : `read1` only - a full-size blocking `read` raises in the fake response; wall and
              monotonic stamps are taken AFTER the bytes return; the connection is closed on
              EOF and on error.
  sample    : loopback-only endpoint (8009 refused), non-streaming only, sampling pinned by
              the caller's own JSON, every argument validated BEFORE an evidence dir exists,
              fresh evidence dir only, per-request log slices bounded to the fd-sampled size,
              rotation/truncation/rewrite/vanished log fail closed with the raw bytes saved,
              arbitrary JSON shapes validated without crashing, bad evidence saved then exit 3.
  fault     : pidfd opened BEFORE identity binding; identity is the inode of
              `/proc/<pid>/exe` itself, rechecked immediately before the signal; a completed
              stream (finish_reason / [DONE] / error / malformed payload) is NEVER killed; a
              non-2xx stream is never killed; `signal_sent` is true only on a successful
              SIGKILL; the pidfd exit event is taken immediately after the send, before the
              stream is drained; POLLIN proves EXIT, not reaping; the marker trigger is
              watched independently of token arrival, so it can fire during prefill;
              `phase_proven` stays false.
  counters  : the DONE `helper=` tail is a PER-ENGINE-CALL delta
              (src/program/generate.cpp:14910-14914 snapshot, :16155-16160 differences): no
              subtraction between lines, no lifetime claim, no series merged across logs, one
              HTTP request may carry several engine calls.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("acceptance_under_test",
                                              ROOT / "tools" / "mixed_gpu" / "acceptance.py")
assert SPEC and SPEC.loader
acc = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = acc
SPEC.loader.exec_module(acc)

REQ = json.dumps({"model": "swift-1.5-iq3_xxs",
                  "messages": [{"role": "user", "content": "hi"}],
                  "max_tokens": 16, "temperature": 0, "seed": 42}).encode()
REQ_STREAM = json.dumps({"model": "swift-1.5-iq3_xxs",
                         "messages": [{"role": "user", "content": "hi"}],
                         "max_tokens": 16, "temperature": 0, "seed": 42,
                         "stream": True,
                         "stream_options": {"include_usage": True}}).encode()

COMPLETION = {
    "id": "chatcmpl-1", "object": "chat.completion", "model": "swift-1.5-iq3_xxs",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "answer text"},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 120, "completion_tokens": 34, "total_tokens": 154,
              "prompt_tokens_details": {"cached_tokens": 96}},
}
OK_BODY = json.dumps(COMPLETION).encode()

STAT_TEXT = b"999 (strata-expert-helper) S 1000 1000 1000 0 -1 41 0 0 0 0 1 2 0 0 20 0 1 0 12345 0"
START = 12345
PID = 999


def sse(*events: dict) -> list[bytes]:
    return [b"data: " + json.dumps(ev).encode() + b"\n" for ev in events]


def delta(text: str) -> dict:
    return {"object": "chat.completion.chunk", "choices": [{"index": 0,
                                                             "delta": {"content": text}}]}


def role() -> dict:
    return {"object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"role": "assistant"}}]}


def finish(reason: str = "stop") -> dict:
    return {"object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}


DONE = b"data: [DONE]\n"
USAGE_EV = {"object": "chat.completion.chunk", "choices": [],
            "usage": {"prompt_tokens": 40, "completion_tokens": 8,
                      "prompt_tokens_details": {"cached_tokens": 24}}}


# ---------------------------------------------------------------- mocked HTTP

class FakeResponse:
    """An HTTPResponse stand-in: read1 only (a blocking read raises), delay between pieces."""

    def __init__(self, status: int, reason: str, headers: list[tuple[str, str]],
                 pieces: list[bytes], delay: float = 0.0, sleep=time.sleep,
                 head_delay: float = 0.0):
        self.status, self.reason, self._headers = status, reason, headers
        self.pieces = list(pieces)
        self.delay = delay
        self.head_delay = head_delay
        self.sleep = sleep
        self.read1_calls: list[int] = []
        self.blocking_read_calls = 0

    def getheaders(self) -> list[tuple[str, str]]:
        return list(self._headers)

    def read1(self, n: int) -> bytes:
        self.read1_calls.append(n)
        if not self.pieces:
            return b""
        if self.delay:
            self.sleep(self.delay)                # data "arrives" late, as in real streaming
        return self.pieces.pop(0)

    def read(self, n: int = -1) -> bytes:          # the rejected pattern
        self.blocking_read_calls += 1
        raise AssertionError("real_transport must not use a full-size blocking read()")


class FakeConn:
    def __init__(self, response: FakeResponse | None, raise_on_request: str | None = None):
        self.response = response
        self.raise_on_request = raise_on_request
        self.requests: list[tuple[str, str, bytes | None]] = []
        self.closed = False

    def request(self, method, path, body=None, headers=None):
        if self.raise_on_request:
            raise ConnectionRefusedError(self.raise_on_request)
        self.requests.append((method, path, body))

    def getresponse(self) -> FakeResponse:
        assert self.response is not None, "getresponse before request"
        if self.response.head_delay:                       # the head arrives late, as in prefill
            self.response.sleep(self.response.head_delay)
        return self.response

    def close(self) -> None:
        self.closed = True


def fake_connect(conn: FakeConn):
    calls: list[FakeConn] = []

    def connect(host: str, port: int, timeout: float) -> FakeConn:
        calls.append(conn)
        return conn

    connect.calls = calls                                            # type: ignore[attr-defined]
    return connect


# ---------------------------------------------------------------- fake pidfd / procfs

class FakePidfd:
    """Stands in for pidfd_open / pidfd_send_signal(SIGKILL) / pidfd poll / close."""

    def __init__(self, death=True, open_ok=True, kill_raises=False, proc_gone=False):
        self.opens: list[int] = []
        self.signals: list[str] = []           # kill signals ONLY - an open is not a signal
        self.death_result = death
        self.open_ok = open_ok
        self.kill_raises = kill_raises
        self.proc_gone = proc_gone             # False = alive: identity preflight must pass
        self.closed = 0

    def open(self, pid):
        if not self.open_ok:
            raise acc.PidfdUnavailable("fake: pidfd unavailable")
        self.opens.append(pid)
        return 7

    def kill(self, pidfd):
        if self.kill_raises:
            raise acc.PidfdUnavailable("fake: pidfd_send_signal failed")
        self.signals.append("SIGKILL")
        self.proc_gone = True

    def death(self, pidfd, timeout):
        return self.death_result

    def close(self, pidfd):
        self.closed += 1


def fake_stat_map(exe: Path, proc_ino: list[tuple[int, int]]):
    """stat_path seam: /proc/<pid>/exe is answered from a mutable (dev, ino) the test controls."""
    real = os.stat(str(exe))

    def stat_path(p: str):
        if p.startswith("/proc/"):
            return FakeStatResult(proc_ino[0][0], proc_ino[0][1])
        return os.stat(p)
    return stat_path


class FakeStatResult:
    def __init__(self, dev: int, ino: int):
        self.st_dev, self.st_ino = dev, ino
        self.st_size, self.st_mtime_ns, self.st_mode = 0, 0, 0


def make_hooks(pieces: list[Any] | None = None, status: int = 200, side_effect=None,
               pidfd=None, exe: Path | None = None, proc_ino=None, delay: float = 0.0,
               raise_on_request=None):
    """Hooks for fault tests: fake transport with REAL sleeps/stamps, fake pidfd, fake procfs.

    `pieces` entries are bytes, or (bytes, per_piece_delay_seconds).
    """
    p = pidfd or FakePidfd()
    if proc_ino is not None:
        ino = proc_ino
    elif exe is not None:
        st = os.stat(str(exe))
        ino = [(st.st_dev, st.st_ino)]                    # identity passes unless a test flips it
    else:
        ino = [(1, 1)]

    def transport(host, port, path, timeout, body, connect):
        i = len(calls)
        calls.append({"i": i, "host": host, "port": port, "path": path, "body": body})
        info = {"status": status, "reason": "OK" if 200 <= status < 300 else "Error",
                "headers": [("content-type", "application/json")]}
        if side_effect is not None:
            side_effect(i, host, port, path, body, info)

        def gen():
            for piece in pieces or []:
                data, d = piece if isinstance(piece, tuple) else (piece, delay)
                if d:
                    time.sleep(d)                                  # real arrival pacing
                yield (time.monotonic(), time.time(), data)        # stamped after the bytes
        return info, gen()

    state = {"calls": calls}

    def stat_path(path: str):
        if str(path).startswith("/proc/"):
            return FakeStatResult(ino[0][0], ino[0][1])
        return os.stat(path)

    hooks = acc.FaultHooks(transport=transport,
                           pidfd_open=p.open, pidfd_kill=p.kill, pidfd_death=p.death,
                           pidfd_close=p.close, proc_exists=lambda pid: not p.proc_gone,
                           read_proc=lambda pid, name: STAT_TEXT, stat_path=stat_path,
                           wall=time.time, mono=time.monotonic, sleep=time.sleep)
    hooks.state = state                                        # type: ignore[attr-defined]
    hooks.pidfd_obj = p                                        # type: ignore[attr-defined]
    hooks.conn = None                                          # type: ignore[attr-defined]
    return hooks


calls: list[dict] = []


def real_hooks(conn: FakeConn, exe: Path, pidfd=None, proc_ino=None):
    """real_transport under test, with ONLY the connection factory injected."""
    p = pidfd or FakePidfd()
    ino = proc_ino if proc_ino is not None else [os.stat(str(exe)).st_ino]
    dev = os.stat(str(exe)).st_dev

    def stat_path(path: str):
        if str(path).startswith("/proc/"):
            return FakeStatResult(dev, ino[0])
        return os.stat(path)

    return acc.FaultHooks(transport=acc.real_transport, connect=fake_connect(conn),
                          pidfd_open=p.open, pidfd_kill=p.kill, pidfd_death=p.death,
                          pidfd_close=p.close,
                          proc_exists=lambda pid: not p.proc_gone,
                          read_proc=lambda pid, name: STAT_TEXT, stat_path=stat_path)


def sample_argv(out: Path, log: Path | None = None, req: Path | None = None,
                parent: Path | None = None, extra: list[str] | None = None) -> list[str]:
    argv = ["sample", "--request", str(req or write_req(parent or out.parent, REQ)),
            "--out", str(out), "--phase", "warm", "--chain", "chain-on",
            "--build-id", "build-x", "--helper-state", "on", "--samples", "2",
            "--warmups", "1"]
    if log is not None:
        argv += ["--log", f"engine={log}"]
    return argv + (extra or [])


def fault_argv(out: Path, tmp: Path, exe: Path, extra: list[str] | None = None,
               req: bytes = REQ_STREAM, trigger: str = "first-delta") -> list[str]:
    argv = ["fault", "--request", str(write_req(tmp, req)), "--out", str(out),
            "--pid", str(PID), "--exe", str(exe), "--starttime", str(START),
            "--trigger", trigger, "--death-timeout", "0.05", "--marker-poll", "0.02"]
    return argv + (extra or [])


def write_req(dir_: Path, body: bytes) -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    p = dir_ / "req.json"
    p.write_bytes(body)
    return p


def manifest(out: Path) -> dict:
    return json.loads((out / "manifest.json").read_text())


def sample_hooks(pieces: list[bytes], status: int = 200, side_effect=None):
    def transport(host, port, path, timeout, body, connect):
        i = len(calls)
        calls.append({"i": i, "host": host, "port": port, "path": path, "body": body})
        info = {"status": status, "reason": "OK" if 200 <= status < 300 else "Error",
                "headers": [("content-type", "application/json")]}
        if side_effect is not None:
            side_effect(i, host, port, path, body, info)
        return info, iter([(100.0 + k, 1000.0 + k, c) for k, c in enumerate(pieces)])
    return acc.FaultHooks(transport=transport, sleep=time.sleep)


@pytest.fixture(autouse=True)
def _reset_calls():
    calls.clear()


# ---------------------------------------------------------------- endpoint + args

def test_endpoint_accepts_loopback_literals_only():
    assert acc.parse_endpoint("http://127.0.0.1:8019/v1/chat/completions") == (
        "127.0.0.1", 8019, "/v1/chat/completions")
    assert acc.parse_endpoint("http://[::1]:8019/v1/x")[0] == "::1"
    for bad in ("http://127.0.0.1:8009/v1/chat/completions",       # production service
                "http://localhost:8019/v1/x", "http://10.0.0.5:8019/v1/x",
                "http://example.com/v1/x", "https://127.0.0.1:8019/v1/x",
                "127.0.0.1:8019/v1/x", "http://127.0.0.1:0/v1/x",
                "http://[::1/v1/x"):
        with pytest.raises(ValueError):
            acc.parse_endpoint(bad)


def test_sample_refuses_production_port_and_sends_nothing(tmp_path):
    hooks = sample_hooks([OK_BODY])
    out = tmp_path / "ev"
    argv = sample_argv(out, req=write_req(tmp_path, REQ), parent=tmp_path)
    argv += ["--url", "http://127.0.0.1:8009/v1/chat/completions"]
    assert acc.main(argv, hooks) == acc.EXIT_REFUSED
    assert calls == []
    assert not out.exists()


def test_sample_refuses_stream_request_before_network(tmp_path):
    hooks = sample_hooks([OK_BODY])
    out = tmp_path / "ev"
    req = json.loads(REQ)
    req["stream"] = True
    argv = sample_argv(out, req=write_req(tmp_path, json.dumps(req).encode()), parent=tmp_path)
    assert acc.main(argv, hooks) == acc.EXIT_REFUSED
    assert calls == []
    assert not out.exists()


@pytest.mark.parametrize("kwargs,expect", [
    ({"--samples": "0"}, "samples"),
    ({"--warmups": "-1"}, "warmups"),
    ({"--timeout": "0"}, "timeout"),
])
def test_sample_validates_counts_before_any_directory(tmp_path, kwargs, expect):
    hooks = sample_hooks([OK_BODY])
    out = tmp_path / "ev"
    argv = sample_argv(out, req=write_req(tmp_path, REQ), parent=tmp_path)
    for k, v in kwargs.items():
        argv += [k, v]
    assert acc.main(argv, hooks) == acc.EXIT_REFUSED
    assert not out.exists()
    assert calls == []


@pytest.mark.parametrize("label", ["a/b", "../x", "a b", "a=b=c", ""])
def test_log_labels_are_filename_safe(tmp_path, label):
    log = tmp_path / "engine.log"
    log.write_bytes(b"x\n")
    with pytest.raises(ValueError):
        acc.parse_logs([f"{label}={log}"])
    assert acc.parse_logs([f"engine-log.1={log}"]) == {"engine-log.1": str(log)}


def test_log_path_with_dotdot_is_refused(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "e.log").write_bytes(b"x\n")
    with pytest.raises(ValueError):
        acc.parse_logs([f"engine={tmp_path / 'logs' / '..' / 'logs' / 'e.log'}"])


@pytest.mark.parametrize("req", [
    json.dumps({"messages": [{"role": "user", "content": "hi"}], "seed": 7}).encode(),
    json.dumps({"messages": [{"role": "user", "content": "hi"}], "temperature": 0.7,
                "seed": 7}).encode(),
    json.dumps({"messages": [{"role": "user", "content": "hi"}], "temperature": 0,
                "seed": "7"}).encode(),
    json.dumps({"messages": [{"role": "user", "content": "hi"}], "temperature": 0,
                "seed": 1, "max_tokens": -5}).encode(),
    json.dumps({"messages": [{"role": "user", "content": "hi"}], "temperature": 0,
                "seed": 1, "stream": "yes"}).encode(),
    json.dumps([{"temperature": 0, "seed": 1}]).encode(),
    b"{not json",
])
def test_sample_refuses_unpinned_or_malformed_request(tmp_path, req):
    hooks = sample_hooks([OK_BODY])
    out = tmp_path / "ev"
    assert acc.main(sample_argv(out, req=write_req(tmp_path, req), parent=tmp_path),
                    hooks) == acc.EXIT_REFUSED
    assert calls == [] and not out.exists()


def test_sample_sends_the_request_verbatim(tmp_path):
    hooks = sample_hooks([OK_BODY])
    out = tmp_path / "ev"
    body = json.loads(REQ)
    req_file = write_req(tmp_path, json.dumps(body).encode())
    assert acc.main(sample_argv(out, req=req_file, parent=tmp_path), hooks) == acc.EXIT_OK
    assert calls[0]["body"] == req_file.read_bytes()
    assert json.loads(calls[0]["body"])["seed"] == body["seed"]


# ---------------------------------------------------------------- real transport

def test_real_transport_reads_incrementally_and_stamps_after_bytes(tmp_path):
    """Exercises acceptance.real_transport itself: read1 only, stamps after bytes, cleanup."""
    exe = tmp_path / "helper.bin"
    exe.write_bytes(b"#!x")
    pieces = sse(delta("a")) + sse(delta("b"))
    conn = FakeConn(FakeResponse(200, "OK", [("content-type", "text/event-stream")],
                                pieces, delay=0.05))
    hooks = real_hooks(conn, exe)
    got: list[tuple[float, float, bytes]] = []
    info, gen = acc.real_transport("127.0.0.1", 8019, "/v1/chat/completions", 5.0, REQ,
                                   fake_connect(conn))
    t_before = time.monotonic()
    for item in gen:
        got.append(item)
    assert info["status"] == 200
    assert b"".join(c[2] for c in got) == b"".join(pieces)
    assert conn.response.read1_calls and conn.response.read1_calls[0] == 65536
    assert conn.response.blocking_read_calls == 0                    # read() never touched
    # the stamps are receive times: the first stamp is later than the delayed arrival
    assert got[0][0] - t_before >= 0.05
    assert got[1][0] > got[0][0]
    assert conn.closed is True


def test_real_transport_closes_connection_on_request_failure():
    conn = FakeConn(None, raise_on_request="nothing listening")
    info, gen = acc.real_transport("127.0.0.1", 8019, "/v1/x", 1.0, REQ, fake_connect(conn))
    with pytest.raises(ConnectionRefusedError):
        list(gen)
    assert conn.closed is True


def test_fault_runs_through_real_transport_and_never_kills_a_coalesced_response(tmp_path):
    """One chunk carrying delta + finish + [DONE] is an already-complete response: no kill."""
    exe = tmp_path / "helper.bin"
    exe.write_bytes(b"#!x")
    coalesced = [b"".join(sse(role()) + sse(delta("all at once")) + sse(finish())
                          + [USAGE_EV] * 0 + sse(USAGE_EV) + [DONE])]
    conn = FakeConn(FakeResponse(200, "OK", [("content-type", "text/event-stream")],
                                coalesced, delay=0.0))
    p = FakePidfd()
    hooks = real_hooks(conn, exe, pidfd=p)
    out = tmp_path / "ev"
    rc = acc.main(fault_argv(out, tmp_path, exe), hooks)
    assert rc == acc.EXIT_NOT_ANCHORED
    assert p.signals == []                                          # nothing signalled
    m = manifest(out)
    assert m["signal_sent"] is False and m["trigger_fired"] is None
    assert any("terminal event" in p for p in m["problems"])
    term = m["stream"]["terminal_events"]
    assert term[0]["type"] == "finish"
    assert term[0]["killed"] is False
    assert term[0]["same_batch_also_had_deltas"] is True      # the delta was in the same batch
    assert m["stream"]["content_sha256"] == hashlib.sha256(b"all at once").hexdigest()
    assert conn.closed is True
    assert p.closed == 1


def test_fault_real_transport_increments_kill_before_the_response_finishes(tmp_path):
    exe = tmp_path / "helper.bin"
    exe.write_bytes(b"#!x")
    pieces = (sse(role()) + sse(delta("gen")) + sse(delta("erating")) + sse(finish())
              + sse(USAGE_EV) + [DONE])
    conn = FakeConn(FakeResponse(200, "OK", [("content-type", "text/event-stream")],
                                pieces, delay=0.04))
    p = FakePidfd()
    hooks = real_hooks(conn, exe, pidfd=p)
    out = tmp_path / "ev"
    rc = acc.main(fault_argv(out, tmp_path, exe), hooks)
    assert rc == acc.EXIT_OK, json.dumps(manifest(out))[:1500]
    m = manifest(out)
    assert p.signals == ["SIGKILL"] and m["signal_sent"] is True
    assert m["kill"]["cause"] == "first_nonempty_delta"
    # the pidfd observation is taken when the poll RETURNS, not at EOF
    assert m["death"]["observed_after_syscall_mono_s"] < 0.5
    assert m["death"]["observed_at_mono"] < m["timing"]["mono_finish"]
    assert m["timing"]["chunks"] == len(pieces)                     # drained to EOF
    assert m["stream"]["done_seen"] is True and m["stream"]["finish_reasons"] == ["stop"]
    assert m["stream"]["usage"]["cached_tokens"] == 24
    assert (out / "output.txt").read_text() == "generating"
    assert conn.closed is True


# ---------------------------------------------------------------- fault: truth of the signal

def test_fault_failed_send_reports_no_signal_and_keeps_the_error(tmp_path):
    exe = tmp_path / "helper.bin"
    exe.write_bytes(b"#!x")
    pieces = sse(role()) + sse(delta("x")) + sse(finish()) + sse(USAGE_EV) + [DONE]
    hooks = make_hooks(pieces, exe=exe, pidfd=FakePidfd(kill_raises=True))
    out = tmp_path / "ev"
    rc = acc.main(fault_argv(out, tmp_path, exe), hooks)
    assert rc == acc.EXIT_NOT_ANCHORED
    m = manifest(out)
    assert m["signal_sent"] is False                                # never true on a failed send
    assert m["kill"]["sent"] is False
    assert "pidfd_send_signal failed" in m["kill"]["error"]
    assert any("SIGKILL send failed" in p for p in m["problems"])
    assert m["verdict"].startswith("INCONCLUSIVE: SIGKILL not sent")
    assert hooks.pidfd_obj.closed == 1                              # fd closed on this path


def test_fault_kill_refused_when_identity_changes_after_binding(tmp_path):
    """PID reuse / exe swap between binding and the kill point: recheck blocks the signal."""
    exe = tmp_path / "helper.bin"
    exe.write_bytes(b"#!x")
    other = tmp_path / "other.bin"
    other.write_bytes(b"#!y")
    good, bad = os.stat(str(exe)), os.stat(str(other))
    ino = [[good.st_dev, good.st_ino]]

    def flip(i, host, port, path, body, info):
        ino[0] = [bad.st_dev, bad.st_ino]                            # reused PID mid-request

    pieces = sse(role()) + sse(delta("x")) + sse(finish()) + sse(USAGE_EV) + [DONE]
    hooks = make_hooks(pieces, exe=exe, side_effect=flip, proc_ino=None)
    hooks.stat_path = lambda p: (FakeStatResult(ino[0][0], ino[0][1])
                                 if str(p).startswith("/proc/") else os.stat(p))
    out = tmp_path / "ev"
    rc = acc.main(fault_argv(out, tmp_path, exe), hooks)
    assert rc == acc.EXIT_NOT_ANCHORED
    m = manifest(out)
    assert m["signal_sent"] is False
    assert m["kill"]["sent"] is False
    assert m["kill"]["refused_before_send"] == "identity recheck failed"
    assert "inode changed since binding" in json.dumps(m["identity_recheck"])
    assert hooks.pidfd_obj.signals == []


def test_fault_refuses_non2xx_stream_without_signalling(tmp_path):
    exe = tmp_path / "helper.bin"
    exe.write_bytes(b"#!x")
    pieces = sse(delta("partial body")) + sse(finish()) + [DONE]
    hooks = make_hooks(pieces, exe=exe, status=500)
    out = tmp_path / "ev"
    rc = acc.main(fault_argv(out, tmp_path, exe), hooks)
    assert rc == acc.EXIT_NOT_ANCHORED
    m = manifest(out)
    assert m["signal_sent"] is False
    assert "not 2xx" in m["kill"]["refused_before_send"]
    assert any("kill refused" in p for p in m["problems"])
    assert hooks.pidfd_obj.signals == []
    assert (out / "response_sse.bin").read_bytes() == b"".join(pieces)


def test_fault_never_kills_a_stream_with_no_content_delta(tmp_path):
    exe = tmp_path / "helper.bin"
    exe.write_bytes(b"#!x")
    pieces = sse(role()) + sse(finish()) + sse(USAGE_EV) + [DONE]     # never a non-empty delta
    hooks = make_hooks(pieces, exe=exe)
    out = tmp_path / "ev"
    rc = acc.main(fault_argv(out, tmp_path, exe), hooks)
    assert rc == acc.EXIT_NOT_ANCHORED
    m = manifest(out)
    assert m["signal_sent"] is False
    assert any("no trigger fired" in p for p in m["problems"])
    assert hooks.pidfd_obj.signals == []


def test_fault_ignores_a_marker_that_arrives_after_completion(tmp_path):
    exe = tmp_path / "helper.bin"
    exe.write_bytes(b"#!x")
    log = tmp_path / "engine.log"
    log.write_bytes(b"")
    # the stream is complete at ~0.02 s; the [DONE] tail keeps the request open until ~0.35 s,
    # so the marker (appended at ~0.12 s) is observed AFTER completion
    pieces = [(sse(role())[0], 0.01), (sse(delta("short"))[0], 0.01), (sse(finish())[0], 0.01),
              (sse(USAGE_EV)[0], 0.0), (DONE, 0.3)]
    hooks = make_hooks(pieces, exe=exe)
    out = tmp_path / "ev"

    def late_append():
        time.sleep(0.12)
        with open(log, "ab") as fh:
            fh.write(b"[strata] helper fallback fired\n")

    th = threading.Thread(target=late_append)
    th.start()
    rc = acc.main(fault_argv(out, tmp_path, exe, extra=[
        "--log", f"engine={log}", "--marker", "helper fallback"], trigger="log-marker"), hooks)
    th.join()
    assert rc == acc.EXIT_NOT_ANCHORED
    m = manifest(out)
    assert m["signal_sent"] is False
    assert m["stream"]["finish_reasons"] == ["stop"]
    assert m.get("marker_after_completion_ignored"), json.dumps(m)[:1200]
    assert m["marker_after_completion_ignored"]["line"] == "[strata] helper fallback fired"
    assert hooks.pidfd_obj.signals == []


def test_fault_marker_trigger_fires_during_prefill_before_any_token(tmp_path):
    """The marker is watched INDEPENDENTLY of chunk arrival: prefill has no tokens to wait for."""
    exe = tmp_path / "helper.bin"
    exe.write_bytes(b"#!x")
    log = tmp_path / "engine.log"
    log.write_bytes(b"[strata] old run had a helper fallback request line\n")   # ignored
    # the prefill window: the first bytes arrive only at ~0.5 s; the marker lands at ~0.15 s
    pieces = [(sse(role())[0], 0.5), (sse(delta("ge"))[0], 0.0),
              (sse(delta("nerating"))[0], 0.0), (sse(finish())[0], 0.0),
              (sse(USAGE_EV)[0], 0.0), (DONE, 0.0)]
    hooks = make_hooks(pieces, exe=exe)
    out = tmp_path / "ev"
    threads_before = threading.active_count()

    def append_marker():
        time.sleep(0.15)                                     # during the prefill window
        with open(log, "ab") as fh:
            fh.write(b"[strata] helper row validation failed: fallback request\n")

    th = threading.Thread(target=append_marker)
    th.start()
    rc = acc.main(fault_argv(out, tmp_path, exe, extra=[
        "--log", f"engine={log}", "--marker", "fallback request"], trigger="log-marker"), hooks)
    th.join()
    assert rc == acc.EXIT_OK, json.dumps(manifest(out))[:1500]
    m = manifest(out)
    assert m["kill"]["cause"] == "log_marker"
    assert m["trigger_fired"]["line"] == "[strata] helper row validation failed: fallback request"
    assert "old run" not in json.dumps(m["trigger_fired"])
    # the kill landed before the first content token arrived
    raw = (out / "response_sse.bin").read_bytes()
    lines = [json.loads(l) for l in (out / "sse_chunks.jsonl").read_text().splitlines()]
    cum = 0
    first_content_mono = None
    for line in lines:
        piece = raw[cum:cum + line["len"]]
        cum += line["len"]
        if b'"content"' in piece:
            first_content_mono = line["mono"]
            break
    assert first_content_mono is not None
    assert m["kill"]["syscall_mono_before"] < first_content_mono
    assert m["signal_sent"] is True
    assert threading.active_count() <= threads_before              # threads stopped cleanly


def test_fault_death_timestamp_is_taken_immediately_after_the_send(tmp_path):
    exe = tmp_path / "helper.bin"
    exe.write_bytes(b"#!x")
    pieces = sse(role()) + sse(delta("a")) + sse(delta("b")) + sse(finish()) + sse(USAGE_EV) + [DONE]
    hooks = make_hooks(pieces, exe=exe, delay=0.05)
    out = tmp_path / "ev"
    assert acc.main(fault_argv(out, tmp_path, exe), hooks) == acc.EXIT_OK
    m = manifest(out)
    assert m["death"]["observed_after_syscall_mono_s"] < 0.2
    assert m["death"]["pidfd_event"] is True and m["death"]["exit_proven_by_pidfd"] is True
    assert m["death"]["observed_at_mono"] < m["timing"]["mono_finish"]
    assert "EXITED" in m["death"]["note"] and "does not prove" in m["death"]["note"]
    assert m["proc_absent_at_end"] is True


def test_fault_exit_not_confirmed_is_inconclusive_with_signal_truth(tmp_path):
    exe = tmp_path / "helper.bin"
    exe.write_bytes(b"#!x")
    pieces = sse(role()) + sse(delta("a")) + sse(finish()) + sse(USAGE_EV) + [DONE]
    hooks = make_hooks(pieces, exe=exe, pidfd=FakePidfd(death=False, proc_gone=False))
    out = tmp_path / "ev"
    rc = acc.main(fault_argv(out, tmp_path, exe), hooks)
    assert rc == acc.EXIT_NOT_ANCHORED
    m = manifest(out)
    assert m["signal_sent"] is True                                # the send DID happen
    assert m["death"]["exit_proven_by_pidfd"] is False
    assert m["verdict"].startswith("INCONCLUSIVE: SIGKILL sent, exit not confirmed")


def test_fault_fd_closed_even_when_the_validator_and_log_fail(tmp_path):
    exe = tmp_path / "helper.bin"
    exe.write_bytes(b"#!x")
    log = tmp_path / "engine.log"
    log.write_bytes(b"a" * 200 + b"\n")
    pieces = sse(role()) + sse(delta("a")) + sse(finish()) + sse(USAGE_EV) + [DONE]

    def rotate(i, host, port, path, body, info):
        new = tmp_path / "new.log"
        new.write_bytes(b"rotated\n")
        os.replace(new, log)

    hooks = make_hooks(pieces, exe=exe, side_effect=rotate)
    out = tmp_path / "ev"
    rc = acc.main(fault_argv(out, tmp_path, exe, extra=["--log", f"engine={log}"]), hooks)
    assert rc == acc.EXIT_EVIDENCE_BAD
    m = manifest(out)
    assert m["signal_sent"] is True
    assert any("rotation_during_request" in p for p in m["problems"])
    assert hooks.pidfd_obj.closed == 1


def test_fault_missing_log_after_preflight_is_saved_not_fatal(tmp_path):
    exe = tmp_path / "helper.bin"
    exe.write_bytes(b"#!x")
    log = tmp_path / "engine.log"
    log.write_bytes(b"first\n")
    pieces = sse(role()) + sse(delta("a")) + sse(finish()) + sse(USAGE_EV) + [DONE]

    def remove(i, host, port, path, body, info):
        os.remove(log)

    hooks = make_hooks(pieces, exe=exe, side_effect=remove)
    out = tmp_path / "ev"
    rc = acc.main(fault_argv(out, tmp_path, exe, extra=["--log", f"engine={log}"]), hooks)
    assert rc == acc.EXIT_EVIDENCE_BAD
    m = manifest(out)
    assert any("log_missing_after_preflight" in p for p in m["problems"])
    assert (out / "log-engine.slice.bin").read_bytes() == b""
    assert hooks.pidfd_obj.closed == 1


def test_fault_malformed_sse_is_saved_and_reported_without_crash(tmp_path):
    exe = tmp_path / "helper.bin"
    exe.write_bytes(b"#!x")
    pieces = [b"data: {not json}\n", b'data: [1,2]\n', b'data: "plain string"\n',
              b"data: not-json\n"]
    hooks = make_hooks(pieces, exe=exe)
    out = tmp_path / "ev"
    rc = acc.main(fault_argv(out, tmp_path, exe), hooks)
    assert rc == acc.EXIT_NOT_ANCHORED
    m = manifest(out)
    kinds = [t["type"] for t in m["stream"]["terminal_events"]]
    assert kinds[0] == "bad_json"
    assert m["signal_sent"] is False
    assert (out / "response_sse.bin").read_bytes() == b"".join(pieces)


def test_fault_deadline_is_recorded_and_fd_closed(tmp_path):
    exe = tmp_path / "helper.bin"
    exe.write_bytes(b"#!x")

    def slow(host, port, path, timeout, body, connect):
        info = {"status": 200, "reason": "OK", "headers": []}

        def gen():
            time.sleep(0.4)                                   # longer than the deadline
            yield (time.monotonic(), time.time(), sse(delta("late"))[0])
        return info, gen()

    hooks = make_hooks([], exe=exe)
    hooks.transport = slow
    out = tmp_path / "ev"
    argv = fault_argv(out, tmp_path, exe, extra=["--timeout", "0.2"])
    assert acc.main(argv, hooks) == acc.EXIT_NOT_ANCHORED
    m = manifest(out)
    assert m["timing"]["deadline_hit"] is True
    assert any("deadline exceeded" in p for p in m["problems"])
    assert hooks.pidfd_obj.closed == 1


def test_fault_refuses_existing_out_dir_and_markerless_log_marker(tmp_path):
    exe = tmp_path / "helper.bin"
    exe.write_bytes(b"#!x")
    out = tmp_path / "ev"
    out.mkdir()
    keep = out / "keep.txt"
    keep.write_text("v")
    hooks = make_hooks(sse(delta("a")), exe=exe)
    assert acc.main(fault_argv(out, tmp_path, exe), hooks) == acc.EXIT_REFUSED
    assert keep.read_text() == "v" and hooks.pidfd_obj.signals == []
    argv = fault_argv(tmp_path / "ev2", tmp_path, exe, extra=["--trigger", "log-marker"])
    assert acc.main(argv, hooks) == acc.EXIT_REFUSED              # marker required
    assert hooks.pidfd_obj.signals == []
    assert acc.main(fault_argv(tmp_path / "ev3", tmp_path, exe, extra=[
        "--death-timeout", "0"]), hooks) == acc.EXIT_REFUSED
    assert acc.main(fault_argv(tmp_path / "ev4", tmp_path, exe, extra=[
        "--marker-poll", "0.001"]), hooks) == acc.EXIT_REFUSED


def test_fault_refuses_non_streaming_request_before_anything(tmp_path):
    exe = tmp_path / "helper.bin"
    exe.write_bytes(b"#!x")
    hooks = make_hooks(sse(delta("a")), exe=exe)
    out = tmp_path / "ev"
    argv = fault_argv(out, tmp_path, exe, req=REQ)
    assert acc.main(argv, hooks) == acc.EXIT_REFUSED
    assert hooks.pidfd_obj.signals == [] and hooks.pidfd_obj.opens == []
    assert not out.exists()


def test_fault_opens_pidfd_before_identity_and_refuses_bad_identity(tmp_path):
    exe = tmp_path / "helper.bin"
    exe.write_bytes(b"#!x")
    other = tmp_path / "other.bin"
    other.write_bytes(b"#!y")
    bad = os.stat(str(other))
    hooks = make_hooks(sse(delta("a")), exe=exe, proc_ino=[(bad.st_dev, bad.st_ino)])
    out = tmp_path / "ev"
    assert acc.main(fault_argv(out, tmp_path, exe), hooks) == acc.EXIT_NOT_ANCHORED
    m = manifest(out)
    assert m["pidfd_opened"] is True                                # opened first
    assert m["signal_sent"] is False
    assert "is not the declared exe" in json.dumps(m["problems"])
    assert hooks.pidfd_obj.closed == 1


def test_fault_starttime_mismatch_and_dead_pid_refuse_with_fd_closed(tmp_path):
    exe = tmp_path / "helper.bin"
    exe.write_bytes(b"#!x")
    hooks = make_hooks(sse(delta("a")), exe=exe)
    hooks.read_proc = lambda pid, name: b"999 (h) S 1 1 1 0 -1 41 0 0 0 0 1 2 0 0 20 0 1 0 999 0"
    out = tmp_path / "ev"
    assert acc.main(fault_argv(out, tmp_path, exe), hooks) == acc.EXIT_NOT_ANCHORED
    assert any("starttime" in p for p in manifest(out)["problems"])
    assert hooks.pidfd_obj.signals == [] and hooks.pidfd_obj.closed == 1

    hooks2 = make_hooks(sse(delta("a")), exe=exe)
    hooks2.proc_exists = lambda pid: False
    out2 = tmp_path / "ev2"
    assert acc.main(fault_argv(out2, tmp_path, exe), hooks2) == acc.EXIT_NOT_ANCHORED
    assert any("/proc/999 does not exist" in p for p in manifest(out2)["problems"])
    assert hooks2.pidfd_obj.closed == 1


def test_fault_refuses_without_pidfd_and_never_falls_back(tmp_path):
    exe = tmp_path / "helper.bin"
    exe.write_bytes(b"#!x")
    hooks = make_hooks(sse(delta("a")), exe=exe, pidfd=FakePidfd(open_ok=False))
    out = tmp_path / "ev"
    assert acc.main(fault_argv(out, tmp_path, exe), hooks) == acc.EXIT_NOT_ANCHORED
    assert hooks.pidfd_obj.signals == []
    assert "pidfd" in json.dumps(manifest(out)["problems"])


def test_module_has_no_kill_pkill_pgrep_or_process_group_paths():
    """AST-level: the module contains no signal-sending path other than pidfd_send_signal."""
    tree = ast.parse((ROOT / "tools" / "mixed_gpu" / "acceptance.py").read_text())
    banned_attrs = {"kill", "killpg", "killall", "pkill", "pgrep", "system", "SIGTERM",
                    "SIGINT", "SIGHUP", "SIGSTOP"}
    banned_modules = {"subprocess", "ctypes"}        # ctypes is allowed only for libc pidfd
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in banned_attrs:
            hits.append(node.attr)
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    hits.append("import subprocess")
        if isinstance(node, ast.ImportFrom) and (node.module or "") in banned_modules:
            hits.append(f"from {node.module}")
    assert hits == [], hits
    # the only signal constant used is SIGKILL, and the only syscall path is pidfd
    src = (ROOT / "tools" / "mixed_gpu" / "acceptance.py").read_text()
    assert src.count("signal.SIGKILL") == 1
    assert "pidfd_send_signal" in src and "pidfd_open" in src


# ---------------------------------------------------------------- death stamp timing

def test_fault_death_stamp_is_recorded_after_the_poll_returns(tmp_path):
    """A blocking pidfd poll must stamp the OBSERVATION at poll return, not at its start."""
    exe = tmp_path / "helper.bin"
    exe.write_bytes(b"#!x")
    pieces = sse(role()) + sse(delta("a")) + sse(finish()) + sse(USAGE_EV) + [DONE]
    hooks = make_hooks(pieces, exe=exe, delay=0.05)

    def slow_death(pidfd, timeout):
        time.sleep(0.1)                                   # the poll blocks, as the real one does
        return True

    hooks.pidfd_death = slow_death
    out = tmp_path / "ev"
    assert acc.main(fault_argv(out, tmp_path, exe), hooks) == acc.EXIT_OK
    m = manifest(out)
    d = m["death"]
    assert d["poll_duration_mono_s"] >= 0.1               # the stamp is taken after the poll
    assert d["poll_start_mono"] < d["observed_at_mono"]
    assert m["kill"]["syscall_mono_after"] <= d["poll_start_mono"]   # syscall stamps stay apart
    assert d["observed_at_mono"] < m["timing"]["mono_finish"]   # before the response finished
    assert d["observed_at_wall_utc"].endswith("+00:00")
    assert "earliest moment" in d["note"] and "lower bound" in d["note"]
    assert m["kill"]["syscall_mono_before"] <= m["kill"]["syscall_mono_after"]


# ---------------------------------------------------------------- head truth before firing

def test_fault_marker_seen_with_no_head_is_refused_and_never_fires_later(tmp_path):
    """No head = nothing known about the response: the fault is refused for this request."""
    exe = tmp_path / "helper.bin"
    exe.write_bytes(b"#!x")
    log = tmp_path / "engine.log"
    log.write_bytes(b"")
    pieces = [sse(role())[0], sse(delta("body"))[0], sse(finish())[0], sse(USAGE_EV)[0], DONE]
    conn = FakeConn(FakeResponse(200, "OK", [("content-type", "text/event-stream")],
                                pieces, head_delay=0.5))          # the head is late, pre-marker
    p = FakePidfd()
    hooks = real_hooks(conn, exe, pidfd=p)
    out = tmp_path / "ev"

    def append_marker():
        time.sleep(0.15)                                         # head is still unknown here
        with open(log, "ab") as fh:
            fh.write(b"[strata] helper row validation failed: fallback request\n")

    th = threading.Thread(target=append_marker)
    th.start()
    rc = acc.main(fault_argv(out, tmp_path, exe, extra=[
        "--log", f"engine={log}", "--marker", "fallback request"], trigger="log-marker"), hooks)
    th.join()
    assert rc == acc.EXIT_NOT_ANCHORED
    m = manifest(out)
    assert p.signals == []                                       # no signal, ever, this request
    assert m["signal_sent"] is False
    assert m["marker_refused_unknown_status"]["kind"] == "log_marker"
    assert "no HTTP head" in " ".join(m["problems"])
    assert m["verdict"] == "INCONCLUSIVE: no signal sent"
    assert conn.closed is True and p.closed == 1


def test_fault_marker_fires_while_the_body_is_blocked_once_the_2xx_head_is_known(tmp_path):
    """200 headers known (shared conn_info, head before body) + marker mid-body -> signal."""
    exe = tmp_path / "helper.bin"
    exe.write_bytes(b"#!x")
    log = tmp_path / "engine.log"
    log.write_bytes(b"")
    pieces = [sse(role())[0], sse(delta("body"))[0], sse(finish())[0], sse(USAGE_EV)[0], DONE]
    conn = FakeConn(FakeResponse(200, "OK", [("content-type", "text/event-stream")],
                                pieces, head_delay=0.05, delay=0.35))   # head early, body blocked
    p = FakePidfd()
    hooks = real_hooks(conn, exe, pidfd=p)
    out = tmp_path / "ev"

    def append_marker():
        time.sleep(0.15)                                         # head 200 known, body blocked
        with open(log, "ab") as fh:
            fh.write(b"[strata] helper row validation failed: fallback request\n")

    th = threading.Thread(target=append_marker)
    th.start()
    rc = acc.main(fault_argv(out, tmp_path, exe, extra=[
        "--log", f"engine={log}", "--marker", "fallback request"], trigger="log-marker"), hooks)
    th.join()
    m = manifest(out)
    assert p.signals == ["SIGKILL"] and m["signal_sent"] is True
    assert m["kill"]["cause"] == "log_marker"
    assert m["http"]["status"] == 200
    assert m["death"]["exit_proven_by_pidfd"] is True
    # the kill fired at ~0.17s, before the first body byte (~0.40s); the body is then DRAINED
    # to EOF by design, so the completed stream is reported honestly and never re-killed
    assert m["kill"]["syscall_mono_after"] < m["timing"]["first_chunk_mono"]
    assert m["stream"]["done_seen"] is True and m["stream"]["finish_reasons"] == ["stop"]
    assert rc == acc.EXIT_OK
    assert "marker_refused_unknown_status" not in m


def test_fault_marker_is_refused_when_the_known_head_is_500(tmp_path):
    exe = tmp_path / "helper.bin"
    exe.write_bytes(b"#!x")
    log = tmp_path / "engine.log"
    log.write_bytes(b"")
    pieces = [sse(role())[0], sse(delta("body"))[0], sse(finish())[0], sse(USAGE_EV)[0], DONE]
    conn = FakeConn(FakeResponse(500, "Server Error", [("content-type", "text/event-stream")],
                                pieces, head_delay=0.05, delay=0.35))
    p = FakePidfd()
    hooks = real_hooks(conn, exe, pidfd=p)
    out = tmp_path / "ev"

    def append_marker():
        time.sleep(0.15)
        with open(log, "ab") as fh:
            fh.write(b"[strata] helper row validation failed: fallback request\n")

    th = threading.Thread(target=append_marker)
    th.start()
    rc = acc.main(fault_argv(out, tmp_path, exe, extra=[
        "--log", f"engine={log}", "--marker", "fallback request"], trigger="log-marker"), hooks)
    th.join()
    assert rc == acc.EXIT_NOT_ANCHORED
    m = manifest(out)
    assert p.signals == []                                       # non-2xx is never signalled
    assert m["kill"]["refused_before_send"] == "http status 500 is not 2xx"
    assert "marker_refused_unknown_status" not in m              # head was known: plain refusal
    assert m["verdict"].startswith("INCONCLUSIVE: SIGKILL not sent")


# ---------------------------------------------------------------- watcher fail-closed

def test_fault_watcher_log_problem_disarms_the_fault(tmp_path):
    exe = tmp_path / "helper.bin"
    exe.write_bytes(b"#!x")
    log = tmp_path / "engine.log"
    log.write_bytes(b"first line\n")
    pieces = [(sse(role())[0], 0.3), (sse(finish())[0], 0.0), (sse(USAGE_EV)[0], 0.0), (DONE, 0.0)]

    def rotate(i, host, port, path, body, info):
        new = tmp_path / "new.log"
        new.write_bytes(b"rotated target\n")
        os.replace(new, log)

    hooks = make_hooks(pieces, exe=exe, side_effect=rotate)
    out = tmp_path / "ev"

    def append_marker():
        time.sleep(0.1)
        with open(log, "ab") as fh:
            fh.write(b"[strata] helper row validation failed: fallback request\n")

    th = threading.Thread(target=append_marker)
    th.start()
    rc = acc.main(fault_argv(out, tmp_path, exe, extra=[
        "--log", f"engine={log}", "--marker", "fallback request"], trigger="log-marker"), hooks)
    th.join()
    assert rc == acc.EXIT_NOT_ANCHORED
    m = manifest(out)
    assert p_signals(hooks) == []
    assert m["signal_sent"] is False
    assert m["log_disarmed"] and "rotation_during_request" in m["log_disarmed"]
    assert any("log engine" in p for p in m["problems"])
    assert hooks.pidfd_obj.closed == 1


def test_fault_preflight_log_problem_disarms_the_fault(tmp_path, monkeypatch):
    """A log that cannot be snapshotted before the request disarms the fault: no signal."""
    exe = tmp_path / "helper.bin"
    exe.write_bytes(b"#!x")
    log = tmp_path / "engine.log"
    log.write_bytes(b"first line\n")
    pieces = sse(role()) + sse(delta("a")) + sse(finish()) + sse(USAGE_EV) + [DONE]
    hooks = make_hooks(pieces, exe=exe)
    out = tmp_path / "ev"

    real_snapshot = acc.snapshot

    def blocked_snapshot(path, hk):                      # simulate an unreadable/vanished log
        if str(path) == str(log):
            raise OSError(13, "Permission denied")
        return real_snapshot(path, hk)

    monkeypatch.setattr(acc, "snapshot", blocked_snapshot)
    rc = acc.main(fault_argv(out, tmp_path, exe, extra=[
        "--log", f"engine={log}", "--marker", "fallback request"]), hooks)
    m = manifest(out)
    assert rc == acc.EXIT_NOT_ANCHORED
    assert hooks.pidfd_obj.signals == []
    assert m["signal_sent"] is False
    assert m["log_disarmed"] and "log engine" in m["log_disarmed"]
    assert m["kill"]["refused_before_send"].startswith("log evidence lost")
    assert (out / "response_sse.bin").read_bytes() == b"".join(pieces)   # raw still saved


def test_fault_marker_split_across_writes_fires_exactly_once(tmp_path):
    exe = tmp_path / "helper.bin"
    exe.write_bytes(b"#!x")
    log = tmp_path / "engine.log"
    log.write_bytes(b"")
    pieces = [(sse(role())[0], 0.35), (sse(finish())[0], 0.0), (sse(USAGE_EV)[0], 0.0), (DONE, 0.0)]
    hooks = make_hooks(pieces, exe=exe)
    out = tmp_path / "ev"

    def append_split_marker():
        time.sleep(0.1)
        with open(log, "ab") as fh:
            fh.write(b"[strata] helper row validation failed: fallback re")   # no newline yet
        time.sleep(0.1)
        with open(log, "ab") as fh:
            fh.write(b"quest\n")                                             # completes the line

    th = threading.Thread(target=append_split_marker)
    th.start()
    rc = acc.main(fault_argv(out, tmp_path, exe, extra=[
        "--log", f"engine={log}", "--marker", "fallback request"], trigger="log-marker"), hooks)
    th.join()
    m = manifest(out)
    assert hooks.pidfd_obj.signals == ["SIGKILL"]         # exactly one, no re-signal
    assert m["signal_sent"] is True
    assert m["trigger_fired"]["line"] == "[strata] helper row validation failed: fallback request"
    assert "extra_markers_ignored" not in m
    assert rc == acc.EXIT_EVIDENCE_BAD                     # drained, no content: raw saved, nonzero
    assert m["stream"]["done_seen"] is True                # the drained stream is never re-killed


def test_fault_partial_line_is_recorded_not_matched(tmp_path):
    exe = tmp_path / "helper.bin"
    exe.write_bytes(b"#!x")
    log = tmp_path / "engine.log"
    log.write_bytes(b"")
    pieces = [(sse(role())[0], 0.25), (sse(finish())[0], 0.0), (sse(USAGE_EV)[0], 0.0), (DONE, 0.0)]
    hooks = make_hooks(pieces, exe=exe)
    out = tmp_path / "ev"

    def append_unterminated():
        time.sleep(0.05)
        with open(log, "ab") as fh:
            fh.write(b"[strata] helper row validation failed: fallback request")   # no newline

    th = threading.Thread(target=append_unterminated)
    th.start()
    rc = acc.main(fault_argv(out, tmp_path, exe, extra=[
        "--log", f"engine={log}", "--marker", "fallback request"], trigger="log-marker"), hooks)
    th.join()
    m = manifest(out)
    assert hooks.pidfd_obj.signals == []
    assert m["signal_sent"] is False
    assert m.get("watcher_partial_lines"), "the unterminated tail must be reported"
    assert rc == acc.EXIT_NOT_ANCHORED


# ---------------------------------------------------------------- usage + request shape

def test_fault_refuses_bad_stream_options_before_any_directory(tmp_path):
    exe = tmp_path / "helper.bin"
    exe.write_bytes(b"#!x")
    req = json.loads(REQ_STREAM)
    req["stream_options"] = ["include_usage"]
    out = tmp_path / "ev"
    hooks = make_hooks(sse(delta("a")), exe=exe)
    assert acc.main(fault_argv(out, tmp_path, exe, req=json.dumps(req).encode()),
                    hooks) == acc.EXIT_REFUSED
    assert not out.exists() and hooks.pidfd_obj.signals == []
    req["stream_options"] = {"include_usage": "yes"}
    out2 = tmp_path / "ev2"
    assert acc.main(fault_argv(out2, tmp_path, exe, req=json.dumps(req).encode()),
                    hooks) == acc.EXIT_REFUSED
    assert not out2.exists()


def test_fault_invalid_sse_usage_counts_is_saved_then_nonzero(tmp_path):
    exe = tmp_path / "helper.bin"
    exe.write_bytes(b"#!x")
    bad_usage = {"prompt_tokens": "many", "completion_tokens": -3,
                 "prompt_tokens_details": {"cached_tokens": "lots"}}
    pieces = sse(role()) + sse(delta("a")) + sse(finish()) + sse({"choices": [],
                                                                 "usage": bad_usage}) + [DONE]
    hooks = make_hooks(pieces, exe=exe)
    out = tmp_path / "ev"
    rc = acc.main(fault_argv(out, tmp_path, exe), hooks)
    assert rc == acc.EXIT_EVIDENCE_BAD
    m = manifest(out)
    assert m["signal_sent"] is True and m["death"]["exit_proven_by_pidfd"] is True
    joined = " ".join(m["response_problems"])
    assert "stream usage.prompt_tokens is not a non-negative int" in joined
    assert "stream usage.completion_tokens is not a non-negative int" in joined
    assert "cached_tokens is not a non-negative int" in joined
    assert (out / "response_sse.bin").read_bytes() == b"".join(pieces)     # raw kept
    assert m["verdict"].startswith("INCONCLUSIVE")


def p_signals(hooks) -> list[str]:
    return hooks.pidfd_obj.signals


# ---------------------------------------------------------------- counters

def test_helper_tail_is_per_engine_call_delta_with_no_arithmetic():
    lines = (
        "[strata] done: 200 tokens in 12 s (16.7 tok/s) (stop, cancel=False) "
        "helper=reqs=1 rows=4 bytes=9000 timeouts=0 fallbacks=1\n"
        "[strata] DONE 30 210 900.0 40.0 stop 4 6 0 -1 -1 0 0 0.0 30 helper=reqs=1 rows=8 "
        "bytes=12000 timeouts=1 fallbacks=0\n"
    )
    recs = acc.helper_line_records(lines, "engine")
    assert len(recs) == 2                                          # two engine calls, kept apart
    assert recs[0]["per_engine_call_delta"] is True
    assert recs[0]["lifetime_cumulative"] is False
    assert recs[0]["counters"] == {"reqs": 1, "rows": 4, "bytes": 9000, "timeouts": 0,
                                   "fallbacks": 1}
    assert recs[1]["counters"]["rows"] == 8                        # no subtraction happened
    assert recs[1]["line_index"] == 1
    for rec in recs:
        assert "delta_from_previous_line_in_slice" not in rec
        assert rec["proves_helper_gpu_compute"] is False
        assert rec["provenance"] == "engine"


def test_helper_tail_missing_field_is_null_not_zero():
    recs = acc.helper_line_records("[strata] done: 5 tokens helper=reqs=2 rows=7\n", "w")
    assert recs[0]["counters"]["fallbacks"] is None
    assert recs[0]["counters"]["reqs"] == 2
    assert acc.parse_helper_tail("no tail here") is None


def test_sample_keeps_helper_lines_per_log_and_never_merges_logs(tmp_path):
    eng = tmp_path / "engine.log"
    wrap = tmp_path / "wrapper.log"
    eng.write_bytes(b"")
    wrap.write_bytes(b"")

    def append(i, host, port, path, body, info):
        with open(eng, "ab") as fh:
            fh.write(f"DONE req{10 + i} helper=reqs=1 rows={i + 1} bytes=100 timeouts=0 "
                     f"fallbacks=0\n".encode())
        with open(wrap, "ab") as fh:
            fh.write(f"[strata] done: req{10 + i} helper=reqs=1 rows={i + 1} bytes=100 "
                     f"timeouts=0 fallbacks=0\n".encode())

    hooks = sample_hooks([OK_BODY], side_effect=append)
    out = tmp_path / "ev"
    argv = sample_argv(out, log=eng, parent=tmp_path) + ["--log", f"wrapper={wrap}"]
    assert acc.main(argv, hooks) == acc.EXIT_OK
    rec = json.loads((out / "sample-001" / "record.json").read_text())
    provenances = [r["provenance"] for r in rec["helper_lines"]]
    assert provenances == ["engine", "wrapper"]                    # two logs, never merged
    assert {r["counters"]["rows"] for r in rec["helper_lines"]} == {2}
    assert all(r["per_engine_call_delta"] is True for r in rec["helper_lines"])


def test_sample_retains_every_call_slice_and_warmup_individually(tmp_path):
    log = tmp_path / "engine.log"
    log.write_bytes(b"pre-existing line not part of any slice\n")

    def append(i, host, port, path, body, info):
        with open(log, "ab") as fh:
            fh.write(f"[strata] done: request {i}\n".encode())

    hooks = sample_hooks([OK_BODY], side_effect=append)
    out = tmp_path / "ev"
    assert acc.main(sample_argv(out, log=log, parent=tmp_path), hooks) == acc.EXIT_OK

    m = manifest(out)
    assert [(r["kind"], r["index"]) for r in m["records"]] == [
        ("warmup", 1), ("sample", 1), ("sample", 2)]
    assert m["declared"]["phase"] == "warm" and m["declared"]["comparable"] is False
    assert m["declared"]["measured_by_this_tool"] is False
    assert m["sampling_pin"]["request_bytes_modified_by_tool"] is False

    base = len(b"pre-existing line not part of any slice\n")
    for rec_dir, i in [("warmup-001", 0), ("sample-001", 1), ("sample-002", 2)]:
        expected = f"[strata] done: request {i}\n".encode()
        assert (out / rec_dir / "log-engine.slice.bin").read_bytes() == expected
        rec = json.loads((out / rec_dir / "record.json").read_text())
        start = rec["log_slices"]["engine"]["start"]
        assert start["offset"] == base + sum(
            len(f"[strata] done: request {k}\n".encode()) for k in range(i))
        assert start["ino"] > 0 and start["dev"] >= 0
        assert rec["log_slices"]["engine"]["end"]["size"] == start["offset"] + len(expected)
    assert b"request 2" in log.read_bytes()


def test_sample_slice_is_bounded_to_the_fd_sampled_size(tmp_path):
    """Bytes a writer appends after the fd was fstat'ed are not part of the slice."""
    log = tmp_path / "engine.log"
    log.write_bytes(b"start\n")

    def append(i, host, port, path, body, info):
        with open(log, "ab") as fh:
            fh.write(b"during\n")
        extra = tmp_path / "extra.txt"
        extra.write_bytes(b"queued")                              # proof of the bound only

    hooks = sample_hooks([OK_BODY], side_effect=append)
    out = tmp_path / "ev"
    assert acc.main(sample_argv(out, log=log, parent=tmp_path), hooks) == acc.EXIT_OK
    sl = (out / "sample-001" / "log-engine.slice.bin")
    assert sl.read_bytes() == b"during\n"


def test_sample_records_cached_tokens_usage_and_output_hash(tmp_path):
    hooks = sample_hooks([OK_BODY])
    out = tmp_path / "ev"
    assert acc.main(sample_argv(out, parent=tmp_path), hooks) == acc.EXIT_OK
    rec = json.loads((out / "sample-001" / "record.json").read_text())
    assert rec["usage"]["cached_tokens"] == 96
    assert rec["usage"]["prompt_tokens"] == 120
    assert rec["finish_reason"] == "stop"
    assert rec["output_text_sha256"] == hashlib.sha256(b"answer text").hexdigest()
    assert (out / "sample-001" / "response.bin").read_bytes() == OK_BODY
    lines = (out / "sample-001" / "response_chunks.jsonl").read_text().splitlines()
    assert json.loads(lines[0])["sha256"] == hashlib.sha256(OK_BODY).hexdigest()


@pytest.mark.parametrize("body,expect", [
    (b'{"error":{"message":"boom"}}', "error body"),
    (b"[1,2,3]", "body is not a JSON object, got list"),
    (b"null", "body is not a JSON object, got NoneType"),
    (b'"a string"', "body is not a JSON object, got str"),
    (json.dumps({"choices": {"message": {"content": "x"}}}).encode(), "no non-empty choices[]"),
    (json.dumps({"choices": ["not a dict"]}).encode(), "choices[0] is not an object"),
    (json.dumps({"choices": [{"message": "junk", "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode(),
     "message is not an object"),
    (json.dumps({"choices": [{"message": {"content": "x"}, "finish_reason": None}],
                 "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode(),
     "no explicit finish_reason"),
    (json.dumps({"choices": [{"message": {"content": "x"}, "finish_reason": "error"}],
                 "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode(),
     "finish_reason is error"),
    (json.dumps({"choices": [{"message": {"content": "x"}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 5, "completion_tokens": "x"}}).encode(),
     "usage.completion_tokens is not a non-negative int"),
    (json.dumps({"choices": [{"message": {"content": "x"}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 5, "completion_tokens": 1,
                           "prompt_tokens_details": "junk"}}).encode(),
     "prompt_tokens_details is not an object"),
    (json.dumps({"choices": [{"message": {"content": ""}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 5, "completion_tokens": 0}}).encode(),
     "empty completion"),
])
def test_sample_saves_bad_evidence_then_exits_3(tmp_path, body, expect):
    hooks = sample_hooks([body])
    out = tmp_path / "ev"
    assert acc.main(sample_argv(out, parent=tmp_path), hooks) == acc.EXIT_EVIDENCE_BAD
    rec = json.loads((out / "sample-001" / "record.json").read_text())
    assert any(expect in p for p in rec["problems"]), rec["problems"]
    assert (out / "sample-001" / "response.bin").read_bytes() == body
    assert any(expect in p for p in manifest(out)["problems"])


def test_sample_bad_http_status_is_saved_then_exit_3(tmp_path):
    hooks = sample_hooks([b'{"error":{"message":"boom"}}'], status=500)
    out = tmp_path / "ev"
    assert acc.main(sample_argv(out, parent=tmp_path), hooks) == acc.EXIT_EVIDENCE_BAD
    rec = json.loads((out / "sample-001" / "record.json").read_text())
    assert any("http status 500" in p for p in rec["problems"])
    assert (out / "sample-001" / "response.bin").read_bytes() == b'{"error":{"message":"boom"}}'


def test_sample_transport_failure_is_saved_then_nonzero(tmp_path):
    def boom(host, port, path, timeout, body, connect):
        raise ConnectionRefusedError("fake: nothing listening")
    hooks = acc.FaultHooks(transport=boom, sleep=time.sleep)
    out = tmp_path / "ev"
    assert acc.main(sample_argv(out, parent=tmp_path), hooks) == acc.EXIT_EVIDENCE_BAD
    rec = json.loads((out / "sample-001" / "record.json").read_text())
    assert any("ConnectionRefusedError" in p for p in rec["problems"])


def test_sample_refuses_existing_output_dir_and_keeps_it_intact(tmp_path):
    out = tmp_path / "ev"
    out.mkdir()
    keep = out / "previous-evidence.json"
    keep.write_text("precious")
    hooks = sample_hooks([OK_BODY])
    assert acc.main(sample_argv(out, parent=tmp_path), hooks) == acc.EXIT_REFUSED
    assert list(out.iterdir()) == [keep] and keep.read_text() == "precious"
    assert calls == []


def test_sample_fails_closed_on_log_rotation(tmp_path):
    log = tmp_path / "engine.log"
    log.write_bytes(b"first file\n")

    def rotate(i, host, port, path, body, info):
        if i == 2:
            new = tmp_path / "new.log"
            new.write_bytes(b"rotated target, new inode\n")
            os.replace(new, log)

    hooks = sample_hooks([OK_BODY], side_effect=rotate)
    out = tmp_path / "ev"
    assert acc.main(sample_argv(out, log=log, parent=tmp_path), hooks) == acc.EXIT_EVIDENCE_BAD
    rec = json.loads((out / "sample-002" / "record.json").read_text())
    problems = [p for p in rec["problems"] if "log engine" in p]
    assert problems and all("rotation_during_request" in p for p in problems)
    assert (out / "sample-002" / "log-engine.slice.bin").read_bytes() == b""


def test_sample_fails_closed_on_log_truncation(tmp_path):
    log = tmp_path / "engine.log"
    log.write_bytes(b"a" * 400 + b"\n")

    def truncate(i, host, port, path, body, info):
        if i == 2:
            with open(log, "r+b") as fh:
                fh.truncate(10)

    hooks = sample_hooks([OK_BODY], side_effect=truncate)
    out = tmp_path / "ev"
    assert acc.main(sample_argv(out, log=log, parent=tmp_path), hooks) == acc.EXIT_EVIDENCE_BAD
    rec = json.loads((out / "sample-002" / "record.json").read_text())
    assert any("truncation_during_request" in p for p in rec["problems"])
    assert rec["log_slices"]["engine"]["end"]["size"] == 10


def test_sample_detects_same_inode_rewrite_by_the_prefix_fingerprint(tmp_path):
    """copytruncate style: same inode, content rewritten inside the fingerprint window."""
    log = tmp_path / "engine.log"
    log.write_bytes(b"EVIDENCE " * 500 + b"\n" + b"tail\n")

    def rewrite(i, host, port, path, body, info):
        if i == 2:
            with open(log, "r+b") as fh:                        # truncate + regrow
                fh.truncate(0)
                fh.write(b"REWRITTEN " * 500 + b"\n" + b"tail\n")

    hooks = sample_hooks([OK_BODY], side_effect=rewrite)
    out = tmp_path / "ev"
    assert acc.main(sample_argv(out, log=log, parent=tmp_path), hooks) == acc.EXIT_EVIDENCE_BAD
    rec = json.loads((out / "sample-002" / "record.json").read_text())
    assert any("prefix_rewrite_detected" in p for p in rec["problems"])


def test_sample_missing_log_after_preflight_is_reported_not_zero(tmp_path):
    log = tmp_path / "engine.log"
    log.write_bytes(b"first\n")

    def remove(i, host, port, path, body, info):
        if i == 1:
            os.remove(log)

    hooks = sample_hooks([OK_BODY], side_effect=remove)
    out = tmp_path / "ev"
    assert acc.main(sample_argv(out, log=log, parent=tmp_path), hooks) == acc.EXIT_EVIDENCE_BAD
    rec = json.loads((out / "sample-001" / "record.json").read_text())
    assert any("log_missing_after_preflight" in p for p in rec["problems"])


def test_read_appended_bounds_the_read_to_the_sampled_size(tmp_path):
    log = tmp_path / "engine.log"
    log.write_bytes(b"a" * 100 + b"\n")
    hooks = acc.FaultHooks(sleep=time.sleep)
    start = acc.snapshot(str(log), hooks)
    with open(log, "ab") as fh:
        fh.write(b"x" * 50)                                      # appended after the snapshot
    meta, data = acc.read_appended(str(log), start, hooks)
    assert meta["problems"] == []
    assert len(data) == 50
    with open(log, "ab") as fh:
        fh.write(b"y" * 50)                                      # appended after this read
    meta2, data2 = acc.read_appended(str(log), start, hooks)
    assert len(data2) == 100                                     # each slice bounded to its own fd


def test_read_appended_reports_vanished_log_on_the_opened_fd(tmp_path):
    log = tmp_path / "engine.log"
    log.write_bytes(b"first\n")
    hooks = acc.FaultHooks(sleep=time.sleep)
    start = acc.snapshot(str(log), hooks)
    os.remove(log)
    meta, data = acc.read_appended(str(log), start, hooks)
    assert data == b"" and any("log_missing_after_preflight" in p for p in meta["problems"])


# ---------------------------------------------------------------- fault stream validation

def test_fault_requires_the_requested_usage_and_a_done_terminator(tmp_path):
    exe = tmp_path / "helper.bin"
    exe.write_bytes(b"#!x")
    pieces = sse(role()) + sse(delta("a")) + sse(finish())          # no usage, no [DONE]
    hooks = make_hooks(pieces, exe=exe)
    out = tmp_path / "ev"
    rc = acc.main(fault_argv(out, tmp_path, exe), hooks)
    assert rc == acc.EXIT_EVIDENCE_BAD
    m = manifest(out)
    assert m["signal_sent"] is True
    assert "include_usage requested but no usage object arrived" in m["response_problems"]
    assert "no data: [DONE] terminator observed" in m["response_problems"]
    assert m["verdict"].startswith("INCONCLUSIVE")


def test_fault_accepts_reasoning_and_tool_call_only_responses(tmp_path):
    exe = tmp_path / "helper.bin"
    exe.write_bytes(b"#!x")
    pieces = ([b'data: {"choices":[{"index":0,"delta":{"reasoning_content":"thinking"}}]}\n']
              + [b'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0}]}}]}\n']
              + sse(finish("tool_calls")) + sse(USAGE_EV) + [DONE])
    hooks = make_hooks(pieces, exe=exe)
    out = tmp_path / "ev"
    assert acc.main(fault_argv(out, tmp_path, exe), hooks) == acc.EXIT_OK
    m = manifest(out)
    assert m["kill"]["cause"] == "first_nonempty_delta"             # reasoning fires the trigger
    assert m["stream"]["reasoning_len"] == len("thinking")
    assert m["stream"]["tool_call_deltas"] == 1
    assert m["stream"]["content_len"] == 0
    assert m["phase_proven"] is False


def test_fault_records_raw_sse_with_per_chunk_receive_times(tmp_path):
    exe = tmp_path / "helper.bin"
    exe.write_bytes(b"#!x")
    pieces = sse(role()) + sse(delta("a")) + sse(delta("b")) + sse(finish()) + sse(USAGE_EV) + [DONE]
    hooks = make_hooks(pieces, exe=exe, delay=0.01)
    out = tmp_path / "ev"
    assert acc.main(fault_argv(out, tmp_path, exe), hooks) == acc.EXIT_OK
    assert (out / "response_sse.bin").read_bytes() == b"".join(pieces)
    lines = [json.loads(l) for l in (out / "sse_chunks.jsonl").read_text().splitlines()]
    assert [l["len"] for l in lines] == [len(c) for c in pieces]
    assert all(lines[i + 1]["mono"] > lines[i]["mono"] for i in range(len(lines) - 1))
    assert lines[0]["wall_utc"].endswith("+00:00")


def test_verify_helper_identity_binds_the_procfs_exe_inode(tmp_path):
    exe = tmp_path / "helper.bin"
    exe.write_bytes(b"#!x")
    st = os.stat(str(exe))
    hooks = acc.FaultHooks(transport=lambda *a: ({}, iter([])),
                           read_proc=lambda pid, name: STAT_TEXT,
                           proc_exists=lambda pid: True,
                           stat_path=lambda p: (FakeStatResult(st.st_dev, st.st_ino)
                                                if str(p).startswith("/proc/") else os.stat(p)))
    rep = acc.verify_helper_identity(PID, str(exe), START, hooks)
    assert rep["problems"] == []
    assert rep["proc_exe_ino"] == st.st_ino and rep["proc_exe_dev"] == st.st_dev
    assert rep["bound_expected"] == {"dev": st.st_dev, "ino": st.st_ino, "starttime": START}
    rep2 = acc.verify_helper_identity(PID, str(exe), START, hooks,
                                      {"dev": 7, "ino": 7, "starttime": START})
    assert any("inode changed since binding" in p for p in rep2["problems"])


# ---------------------------------------------------------------- real pidfd

def test_real_pidfd_kill_of_a_test_owned_cpu_dummy():
    """The only real process this suite touches is the dummy it spawned itself."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    pid = proc.pid
    try:
        fd = acc.real_pidfd_open(pid)
        assert fd >= 0
        t0 = time.monotonic()
        acc.real_pidfd_kill(fd)
        assert time.monotonic() - t0 < 5.0
        assert acc.real_pidfd_death(fd, 10.0) is True
        os.close(fd)
        proc.wait(timeout=10)
        assert proc.returncode < 0                             # killed by a signal
        assert not Path(f"/proc/{pid}").exists()               # the /proc side of the proof
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_real_pidfd_open_of_an_absent_pid_refuses():
    with pytest.raises(acc.PidfdUnavailable):
        acc.real_pidfd_open(0x7FFFFFFF)


def test_cli_is_import_and_parse_only():
    ap = acc.build_parser()
    ns = ap.parse_args(["sample", "--request", "r.json", "--out", "o", "--phase", "cold",
                        "--chain", "c", "--build-id", "b", "--helper-state", "off"])
    assert ns.samples == 1 and ns.warmups == 0 and ns.timeout == 600.0
    ns2 = ap.parse_args(["fault", "--request", "r.json", "--out", "o", "--pid", "1",
                         "--exe", "/x", "--starttime", "5", "--trigger", "first-delta"])
    assert ns2.death_timeout == 10.0 and ns2.marker_poll == 0.05
