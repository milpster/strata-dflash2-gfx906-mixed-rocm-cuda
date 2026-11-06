"""Ownership and real CPU process-failure checks. No GPU or production paths."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools/mixed_gpu/pair_supervisor.py"
FAKE = Path(__file__).parent / "fixtures/mixed_pair_fake.py"
spec = importlib.util.spec_from_file_location("pair_supervisor", SCRIPT)
pair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pair)


def wait_for(fn, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = fn()
            if value:
                return value
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        time.sleep(0.03)
    raise AssertionError("deadline expired")


@pytest.fixture
def running(tmp_path):
    launcher = tmp_path / "launch.sh"
    launcher.write_text('exec "$FAKE_PY" "$FAKE_PAIR" launcher\n')
    state = tmp_path / "state.json"
    env = dict(os.environ, FAKE_PY=sys.executable, FAKE_PAIR=str(FAKE),
               FAKE_PAIR_ROOT=str(tmp_path), PAIR_SUPERVISOR=str(SCRIPT))
    cmd = [sys.executable, str(SCRIPT), "run", "--launcher", str(launcher),
           "--state", str(state), "--lock", str(tmp_path / "lock"),
           "--socket", str(tmp_path / "one.sock"), "--socket", str(tmp_path / "two.sock"),
           "--interval", "0.05", "--stop-timeout", "0.2", "--backoff", "0.05",
           "--max-backoff", "0.2", "--startup-timeout", "2"]
    log = (tmp_path / "supervisor.log").open("w")
    proc = subprocess.Popen(cmd, env=env, stdout=log, stderr=log)
    try:
        first = wait_for(lambda: json.loads(state.read_text()).get("state") == "attached")
        yield proc, state, cmd, env, tmp_path
    finally:
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=8)
        log.close()


@pytest.mark.parametrize("peer_name", ["helper", "server"])
def test_peer_kill_restarts_both_and_cleans_isolated_engine(running, peer_name):
    proc, state, _, _, root = running
    before = json.loads(state.read_text())
    engine = wait_for(lambda: json.loads((root / "engine.json").read_text())["pid"])
    engine_start = pair.process_token(engine)[1]
    # Allow at least one ownership scan after the fake server's fork.
    time.sleep(0.15)
    os.kill(before[f"{peer_name}_pid"], signal.SIGKILL)

    def recovered():
        now = json.loads(state.read_text())
        return now if now["generation"] >= 2 and now["state"] == "attached" else None

    after = wait_for(recovered)
    assert after["helper_pid"] != before["helper_pid"]
    assert after["server_pid"] != before["server_pid"]
    assert not pair.alive(engine, engine_start)
    proc.terminate()
    assert proc.wait(timeout=8) == 0
    assert json.loads(state.read_text())["state"] == "stopped"
    assert not (root / "one.sock").exists()
    assert not (root / "two.sock").exists()


def test_channel_loss_restarts_live_helper(running):
    _, state, _, _, root = running
    before = json.loads(state.read_text())
    with (root / "helper.log").open("a") as log:
        log.write("expert-helper: channel: closed; exiting\n")
    wait_for(lambda: json.loads(state.read_text())["generation"] >= 2)
    after = wait_for(lambda: (d if (d := json.loads(state.read_text()))["state"] == "attached" else None))
    assert after["helper_pid"] != before["helper_pid"]


def test_duplicate_lock_does_not_touch_pair(running):
    proc, state, cmd, env, _ = running
    before = json.loads(state.read_text())
    duplicate = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=3)
    assert duplicate.returncode != 0
    assert "Another pair supervisor" in duplicate.stderr
    assert proc.poll() is None
    assert json.loads(state.read_text())["helper_pid"] == before["helper_pid"]


def test_replaced_socket_is_preserved(tmp_path):
    path = tmp_path / "sock"
    original = socket.socket(socket.AF_UNIX)
    original.bind(str(path))
    owned = {path: pair.socket_token(path)}
    path.unlink()
    replacement = socket.socket(socket.AF_UNIX)
    replacement.bind(str(path))
    pair.release_sockets(owned)
    assert path.exists()
    path.unlink()
    path.write_text("not a socket")
    pair.release_sockets(owned)
    assert path.read_text() == "not a socket"
    original.close()
    replacement.close()


def test_startup_exit_refuses_without_retry_or_foreign_cleanup(tmp_path):
    launcher = tmp_path / "refuse.sh"
    launcher.write_text("exit 3\n")
    state = tmp_path / "state.json"
    result = subprocess.run([sys.executable, str(SCRIPT), "run", "--launcher", str(launcher),
                             "--state", str(state), "--lock", str(tmp_path / "lock"),
                             "--interval", "0.02", "--stop-timeout", "0.1"],
                            capture_output=True, text=True, timeout=4)
    assert result.returncode == 1
    assert json.loads(state.read_text())["state"] == "failed"
    assert json.loads(state.read_text())["generation"] == 1


def test_wrong_start_time_is_not_alive():
    assert not pair.alive(os.getpid(), "not-this-start")


def test_dead_ancestor_pid_cannot_adopt_foreign_descendants(monkeypatch):
    current = pair.process_token(os.getpid())[1]
    known = pair.descendants(os.getpid(), {os.getpid(): "old-start-token"})
    assert known == {os.getpid(): "old-start-token"}
    assert current != known[os.getpid()]


def test_startup_deadline_cleans_unpublished_launcher(tmp_path):
    launcher = tmp_path / "hang.sh"
    launcher.write_text("sleep 60\n")
    state = tmp_path / "state.json"
    result = subprocess.run([sys.executable, str(SCRIPT), "run", "--launcher", str(launcher),
                             "--state", str(state), "--lock", str(tmp_path / "lock"),
                             "--interval", "0.02", "--startup-timeout", "0.1",
                             "--stop-timeout", "0.1"], capture_output=True, text=True, timeout=4)
    assert result.returncode == 1
    assert "deadline" in json.loads(state.read_text())["reason"]


def test_preexisting_path_is_never_removed(tmp_path):
    path = tmp_path / "sock"
    path.write_text("foreign")
    result = subprocess.run([sys.executable, str(SCRIPT), "run", "--launcher", "/does/not/exist",
                             "--state", str(tmp_path / "state"), "--lock", str(tmp_path / "lock"),
                             "--socket", str(path)], capture_output=True, text=True, timeout=3)
    assert result.returncode != 0
    assert path.read_text() == "foreign"
