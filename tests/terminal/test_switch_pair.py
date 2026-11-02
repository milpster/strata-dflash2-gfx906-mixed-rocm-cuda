"""CPU-only guards for the IQ3_S pair switch: mocked processes, real unix sockets, no
GPU, no production outage, no HTTP. The real launcher/config files are only parsed."""
import hashlib
import importlib
import importlib.util
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools/mixed_gpu/switch_pair.py"
sys.path.insert(0, str(ROOT / "tools/mixed_gpu"))
spec = importlib.util.spec_from_file_location("switch_pair", SCRIPT)
switch_pair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(switch_pair)

requires_fixtures = pytest.mark.skipif(
    not Path(switch_pair.TARGET["launcher"]).exists()
    or not Path(switch_pair.TARGET["config"]).exists(),
    reason="local iq3_s launcher/config fixtures unavailable")

PAIR_SCRIPT = str(switch_pair.PAIR_SCRIPT)
TARGET_EXE = str(json.loads(Path(switch_pair.TARGET["config"]).read_text())["exe"])
TARGET_FILL = dict(slots=3860, gib_rounded=5.91, stop_reason="fill-mib",
                   device_free_mib=1234, device_total_mib=7842)
OLD_PEERS = dict(generation=3, state="attached", helper_pid=101, helper_start="s101",
                 server_pid=102, server_start="s2")


def args_for(family="t-switch", startup=0.05):
    return SimpleNamespace(family=family, expected_supervisor=100, execute=True,
                           startup_timeout=startup, stop_timeout=0.2, probe_timeout=1,
                           restore_timeout=0.5)


def entry(model_name, value="loaded"):
    return {"id": model_name, "object": "model", "status": {"value": value}}


class FakeChild:
    def __init__(self, pid):
        self.pid = pid

    def poll(self):
        return None

    def wait(self, timeout):
        return 0


@requires_fixtures
def make_world(monkeypatch, tmp_path, *, target_models=("swift-iq3_s",), target_loaded=True,
               attach_pid=200, completion_fails=False, interrupt_smoke=False,
               old_stop_survives=False, foreign_old_socket=False, keep_target_listener=False,
               flip_token_at_stop=False, foreign_listener=False):
    """A fake machine: old supervisor 100, target supervisor 200, restore supervisor 300."""
    old_socks = [tmp_path / "old.sock", tmp_path / "old2.sock"]
    tgt_socks = [tmp_path / "tgt.sock", tmp_path / "tgt2.sock"]
    monkeypatch.setitem(switch_pair.OLD, "sockets", old_socks)
    monkeypatch.setitem(switch_pair.TARGET, "sockets", tgt_socks)
    monkeypatch.setitem(switch_pair.OLD, "state", tmp_path / "old-supervisor.json")
    monkeypatch.setitem(switch_pair.TARGET, "state", tmp_path / "tgt-supervisor.json")
    monkeypatch.setattr(switch_pair, "EV", tmp_path)
    monkeypatch.setattr(switch_pair, "check_binaries", lambda: None)
    monkeypatch.setattr(switch_pair, "check_assets", lambda prof: None)
    monkeypatch.setattr(switch_pair, "check_profiles", lambda: None)
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)

    world = dict(live={100: "s100", 101: "s101", 102: "s2", 200: "s200", 201: "s201",
                       202: "s202", 203: "s203", 300: "s300"},
                 listeners={102}, launches=[], stops=[], completions=[], model_calls=[],
                 bound=[], restoring=False, restore_status=[])
    for path in old_socks:  # real AF_UNIX inodes owned by this uid
        sock = socket.socket(socket.AF_UNIX)
        sock.bind(str(path))
        world["bound"].append(sock)

    monkeypatch.setattr(switch_pair, "port_listeners", lambda port=8009: set(world["listeners"]))
    monkeypatch.setattr(switch_pair.pair, "alive", lambda pid, tok: world["live"].get(pid) == tok)
    monkeypatch.setattr(switch_pair.pair, "process_token",
                        lambda pid: ("S", world["live"][pid]) if pid in world["live"] else None)

    old_tree = {100: "s100", 101: "s101", 102: "s2"}
    target_tree = {200: "s200", 201: "s201", 202: "s202", 203: "s203"}
    restore_tree = {300: "s300", 101: "s101", 102: "s2"}

    def descendants(root, known):
        # Real pair_supervisor contract: an unseeded scan fabricates NOTHING.
        if known.get(root) is None:
            return {}
        tree = target_tree if root == 200 else restore_tree if root == 300 else old_tree
        out = dict(known)
        out.update({p: t for p, t in tree.items() if p not in out})
        return out

    monkeypatch.setattr(switch_pair.pair, "descendants", descendants)

    def command(pid):
        argvs = {100: [sys.executable, PAIR_SCRIPT, "run", "--launcher", str(switch_pair.OLD["launcher"]),
                       "--state", str(tmp_path / "old-supervisor.json")],
                 200: ["bash", str(switch_pair.TARGET["launcher"]), "--recover"],
                 203: [TARGET_EXE, "--gpu", "0,1"],
                 300: ["bash", str(switch_pair.OLD["launcher"]), "--recover"]}
        if pid == 100:
            world["identity_calls"] = world.get("identity_calls", 0) + 1
            if flip_token_at_stop and world["identity_calls"] == 2:
                # same argv, new process immediately before the signal: stop_old must refuse
                world["live"][100] = "reused-by-a-stranger"
            return argvs[100]
        return argvs.get(pid, [])

    monkeypatch.setattr(switch_pair.runtime, "command", command)

    def stop_group(child, timeout, known):
        world["stops"].append((child.pid, sorted(known)))
        if old_stop_survives and child.pid == 100:
            raise RuntimeError("Owned processes did not exit; refusing another generation")
        for pid in ([100, 101, 102] if child.pid == 100 else
                    [200, 201, 202, 203] if child.pid == 200 else []):
            world["live"].pop(pid, None)
        if child.pid == 100:  # the port is free the moment the old pair is gone
            world["listeners"] = set()
            if foreign_old_socket:  # a stranger recreates the old path as a plain file
                sock = world["bound"].pop(0)
                sock.close()
                (tmp_path / "old.sock").unlink()
                (tmp_path / "old.sock").write_text("foreign")
        if child.pid == 200:
            world["listeners"] = {202} if keep_target_listener else set()

    monkeypatch.setattr(switch_pair.pair, "stop_group", stop_group)

    target_state = dict(generation=1, state="attached", supervisor_pid=200, helper_pid=201,
                        helper_start="s201", server_pid=202, server_start="s202",
                        helper_log=str(tmp_path / "helper.log"))
    old_state = dict(generation=1, state="attached", supervisor_pid=300, helper_pid=101,
                     helper_start="s101", server_pid=102, server_start="s2",
                     helper_log=str(tmp_path / "helper.log"))

    def attached_local(child_pid, state_path):
        if world["restoring"] and child_pid == 300 and Path(state_path) == Path(switch_pair.OLD["state"]):
            world["restore_status"].append(json.loads((tmp_path / "t-switch-status.json").read_text()))
            return dict(old_state)
        if child_pid == attach_pid and Path(state_path) == Path(switch_pair.TARGET["state"]):
            return dict(target_state)
        return None

    monkeypatch.setattr(switch_pair, "attached_local", attached_local)
    monkeypatch.setattr(switch_pair.urllib.request, "urlopen",
                        lambda url, timeout=None: (_ for _ in ()).throw(
                            AssertionError("HTTP attempted before owned-listener proof")))
    monkeypatch.setattr(switch_pair.runtime, "check_fill", lambda state: None)
    monkeypatch.setattr(switch_pair.runtime, "engine_of", lambda state: 103)
    monkeypatch.setattr(switch_pair.slots_window, "fill_report", lambda log: dict(TARGET_FILL))

    def launch(prof, log_path):
        world["launches"].append((str(prof["launcher"]), str(log_path)))
        if prof is switch_pair.OLD:
            world["restoring"] = True
            world["live"].update(restore_tree)
            world["listeners"] = {102}  # the restored old server owns the production port
            return FakeChild(300)
        for path in tgt_socks:  # the target helper creates its sockets as it attaches
            sock = socket.socket(socket.AF_UNIX)
            sock.bind(str(path))
            world["bound"].append(sock)
        world["listeners"] = {777} if foreign_listener else {202}  # target server owns the port
        return FakeChild(200)

    monkeypatch.setattr(switch_pair, "launch", launch)

    def model_list(port=8009):
        world["model_calls"].append(("restore" if world["restoring"] else "target"))
        names = target_models if not world["restoring"] else ("swift-1.5-iq3_xxs",)
        return [entry(name, "loaded" if (target_loaded or world["restoring"]) else "unloaded")
                for name in names]

    monkeypatch.setattr(switch_pair, "model_list", model_list)

    def completion(prof, raw_path, timeout):
        world["completions"].append(switch_pair.model(prof))
        if interrupt_smoke and prof is switch_pair.TARGET:
            raise KeyboardInterrupt
        if completion_fails and prof is switch_pair.TARGET:
            raise RuntimeError("Completion gate failed: http=200 finish=stop tokens=0")
        Path(raw_path).write_text(json.dumps({"ok": True}))
        return dict(http=200, completion_tokens=9, finish="stop", model=switch_pair.model(prof))

    monkeypatch.setattr(switch_pair, "completion", completion)
    (tmp_path / "helper.log").write_text("fake helper log\n")
    (tmp_path / "old-supervisor.json").write_text(json.dumps(dict(supervisor_pid=100, **OLD_PEERS)))
    (tmp_path / "tgt-supervisor.json").write_text(json.dumps(target_state))
    return world


def close_bound(world):
    for sock in world["bound"]:
        sock.close()


def test_plan_mode_mutates_nothing(monkeypatch):
    family = "iq3s-switch-plan"
    status = switch_pair.EV / f"{family}-status.json"
    monkeypatch.setattr(sys, "argv", ["switch_pair.py", "--expected-supervisor", "779473",
                                      "--family", family])
    assert switch_pair.main() == 0
    assert not status.exists()


def test_profiles_use_their_own_config_models():
    assert switch_pair.model(switch_pair.OLD) == "swift-1.5-iq3_xxs"
    assert switch_pair.model(switch_pair.TARGET) == "swift-iq3_s"


@requires_fixtures
def test_launcher_config_and_asset_validation_passes_for_both_profiles():
    switch_pair.check_profiles()
    switch_pair.check_assets(switch_pair.TARGET)


def test_family_overwrite_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(switch_pair, "EV", tmp_path)
    (tmp_path / "t-switch-status.json").write_text("{}")
    with pytest.raises(RuntimeError, match="Refusing to overwrite"):
        switch_pair.run(args_for())


def test_foreign_supervisor_refused_before_outage(monkeypatch):
    monkeypatch.setattr(switch_pair.runtime, "command", lambda pid: ["python", "unrelated.py"])
    result = {}
    with pytest.raises(RuntimeError, match="identity mismatch"):
        switch_pair.old_identity(1234, result)
    assert result == {}


def test_not_group_leader_refused_before_outage(monkeypatch, tmp_path):
    pid = os.getpid()
    monkeypatch.setattr(switch_pair.runtime, "command", lambda p: [
        sys.executable, PAIR_SCRIPT, "run", "--launcher", str(switch_pair.OLD["launcher"]),
        "--state", str(tmp_path / "state.json")])
    monkeypatch.setattr(os, "getpgid", lambda p: 1)  # leader is someone else: a group kill would be collateral
    monkeypatch.setitem(switch_pair.OLD, "state", tmp_path / "state.json")
    (tmp_path / "state.json").write_text(json.dumps(dict(supervisor_pid=pid, generation=1,
                                                         state="attached", helper_pid=pid,
                                                         helper_start=switch_pair.pair.process_token(pid)[1],
                                                         server_pid=pid,
                                                         server_start=switch_pair.pair.process_token(pid)[1])))
    with pytest.raises(RuntimeError, match="process-group leader"):
        switch_pair.old_identity(pid, {})


def test_state_owned_by_other_supervisor_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(switch_pair.runtime, "command", lambda pid: [
        sys.executable, PAIR_SCRIPT, "run", "--launcher", str(switch_pair.OLD["launcher"]),
        "--state", str(tmp_path / "state.json")])
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    monkeypatch.setitem(switch_pair.OLD, "state", tmp_path / "state.json")
    (tmp_path / "state.json").write_text(json.dumps({"supervisor_pid": 999}))
    with pytest.raises(RuntimeError, match="another supervisor"):
        switch_pair.old_identity(1234, {})


def test_reused_or_dead_pid_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(switch_pair.runtime, "command", lambda pid: [
        sys.executable, PAIR_SCRIPT, "run", "--launcher", str(switch_pair.OLD["launcher"]),
        "--state", str(tmp_path / "state.json")])
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    monkeypatch.setitem(switch_pair.OLD, "state", tmp_path / "state.json")
    (tmp_path / "state.json").write_text(json.dumps(dict(supervisor_pid=1234, **OLD_PEERS)))
    monkeypatch.setattr(switch_pair.pair, "process_token", lambda pid: None)
    with pytest.raises(RuntimeError, match="not a live process"):
        switch_pair.old_identity(1234, {})


def test_state_peer_token_mismatch_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(switch_pair.runtime, "command", lambda pid: [
        sys.executable, PAIR_SCRIPT, "run", "--launcher", str(switch_pair.OLD["launcher"]),
        "--state", str(tmp_path / "state.json")])
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(switch_pair.pair, "process_token", lambda pid: ("S", "tok"))
    monkeypatch.setattr(switch_pair.pair, "alive", lambda pid, tok: True)
    monkeypatch.setattr(switch_pair.pair, "descendants", lambda root, known: dict(known))
    monkeypatch.setitem(switch_pair.OLD, "state", tmp_path / "state.json")
    (tmp_path / "state.json").write_text(json.dumps(dict(supervisor_pid=1234, **OLD_PEERS)))
    with pytest.raises(RuntimeError, match="not the scanned tree token"):
        switch_pair.old_identity(1234, {})


def test_reused_supervisor_token_change_refused_before_signal(monkeypatch, tmp_path):
    world = make_world(monkeypatch, tmp_path, flip_token_at_stop=True)
    with pytest.raises(RuntimeError, match="start token changed"):
        switch_pair.run(args_for())
    status = json.loads((tmp_path / "t-switch-status.json").read_text())
    assert status.get("outage_started") is not True  # refused BEFORE the outage/signal
    assert world["stops"] == [] and world["launches"] == []
    close_bound(world)


def test_port_listener_outside_tree_refused(monkeypatch, tmp_path):
    pid = os.getpid()
    start = switch_pair.pair.process_token(pid)[1]
    monkeypatch.setattr(switch_pair.runtime, "command", lambda p: [
        sys.executable, PAIR_SCRIPT, "run", "--launcher", str(switch_pair.OLD["launcher"]),
        "--state", str(tmp_path / "state.json")])
    monkeypatch.setattr(os, "getpgid", lambda p: p)
    monkeypatch.setattr(switch_pair.pair, "alive", lambda pid, tok: True)
    monkeypatch.setattr(switch_pair, "port_listeners", lambda port=8009: {999999})
    monkeypatch.setitem(switch_pair.OLD, "state", tmp_path / "state.json")
    (tmp_path / "state.json").write_text(json.dumps(dict(supervisor_pid=pid, generation=3,
                                                         state="attached", helper_pid=pid,
                                                         helper_start=start, server_pid=pid,
                                                         server_start=start)))
    with pytest.raises(RuntimeError, match="outside the owned tree"):
        switch_pair.old_identity(pid, {})


def test_no_listener_means_no_pair_to_switch(monkeypatch, tmp_path):
    pid = os.getpid()
    start = switch_pair.pair.process_token(pid)[1]
    monkeypatch.setattr(switch_pair.runtime, "command", lambda p: [
        sys.executable, PAIR_SCRIPT, "run", "--launcher", str(switch_pair.OLD["launcher"]),
        "--state", str(tmp_path / "state.json")])
    monkeypatch.setattr(os, "getpgid", lambda p: p)
    monkeypatch.setattr(switch_pair.pair, "alive", lambda pid, tok: True)
    monkeypatch.setattr(switch_pair, "port_listeners", lambda port=8009: set())
    monkeypatch.setitem(switch_pair.OLD, "state", tmp_path / "state.json")
    (tmp_path / "state.json").write_text(json.dumps(dict(supervisor_pid=pid, generation=3,
                                                         state="attached", helper_pid=pid,
                                                         helper_start=start, server_pid=pid,
                                                         server_start=start)))
    with pytest.raises(RuntimeError, match="no pair to switch"):
        switch_pair.old_identity(pid, {})


def test_real_descendants_contract_seeds_and_negative_control():
    real = importlib.import_module("pair_supervisor")  # the real, unmodified primitive
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    time.sleep(0.3)
    try:
        start = real.process_token(child.pid)[1]
        seed = {child.pid: start}
        scan = real.descendants(child.pid, seed)
        assert child.pid in scan and scan[child.pid] == start
        # The original bug: clearing the map before the scan drops the root seed, and the
        # real primitive then fabricates NOTHING, so all recorded ownership is lost.
        buggy = dict(seed)
        buggy.clear()
        buggy.update(real.descendants(child.pid, buggy))
        assert buggy == {}
        # The fixed pattern: seed-preserving update keeps root, history and the new tree.
        fixed = dict(seed)
        fixed.update(real.descendants(child.pid, fixed))
        assert fixed[child.pid] == start and child.pid in fixed
        assert real.descendants(child.pid, {}) == {}
    finally:
        for proc in (child, grandchild):
            proc.kill()  # SIGKILL: bounded, cannot be deferred by the sleep
            proc.wait(timeout=10)


def test_existing_target_socket_refused(monkeypatch, tmp_path):
    foreign = tmp_path / "foreign.sock"
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(foreign))
    monkeypatch.setitem(switch_pair.TARGET, "sockets", [foreign])
    with pytest.raises(RuntimeError, match="existing socket"):
        switch_pair.check_target_sockets()
    assert foreign.exists()
    sock.close()


def test_binary_hash_gate(tmp_path, monkeypatch):
    hashes = {}
    for name in ("rpcw-hip/strata", "rpcw-cuda/strata-expert-helper"):
        binary = tmp_path / "build-mixed-gpu" / name
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(name.encode())
        hashes[str(binary)] = hashlib.sha256(name.encode()).hexdigest()
    manifest = dict(fingerprints=[dict(path=p, sha256=s) for p, s in hashes.items()])
    monkeypatch.setattr(switch_pair, "EV", tmp_path)
    monkeypatch.setattr(switch_pair, "WORK", tmp_path)
    (tmp_path / "ab2-manifest.json").write_text(json.dumps(manifest))
    switch_pair.check_binaries()
    (tmp_path / "build-mixed-gpu/rpcw-hip/strata").write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="Validated binary changed"):
        switch_pair.check_binaries()


def test_actual_fill_gate_records_reported_numbers():
    log = ("expert-helper: filled 3860 slots (5.91 GiB), scratch 1 B, buffers 2 B\n"
           "expert-helper: fill stopped: fill-mib; device free now 1234 MiB of 7842 MiB "
           "(3860 slots, 5.91 GiB uploaded)\n"
           "expert-helper: second-channel engine connected (peer uid 1000)\n")
    assert switch_pair.slots_window.fill_report(log) == TARGET_FILL
    assert switch_pair.loaded_models([entry("swift-iq3_s")]) == ["swift-iq3_s"]
    assert switch_pair.loaded_models([entry("swift-iq3_s", "unloaded")]) == []
    assert switch_pair.loaded_models([{**entry("swift-1.5-iq3_xxs"), "alias_of": "swift-iq3_s"}]) == []


def test_completion_gate_requires_nonempty_final_content(monkeypatch, tmp_path):
    class Resp:
        def __init__(self, raw, code):
            self.raw, self.code = raw, code

        def read(self):
            return self.raw

        @property
        def status(self):
            return self.code

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    sent, raw_reply = [], [b""]

    def reply(content, tokens, finish, model_name=None):
        body = {"choices": [{"message": {"content": content}, "finish_reason": finish}],
                "usage": {"completion_tokens": tokens}}
        if model_name is not None:
            body["model"] = model_name
        return json.dumps(body).encode()

    def urlopen(request, timeout=None):
        sent.append(json.loads(request.data))
        return Resp(raw_reply[0], 200)

    monkeypatch.setattr(switch_pair.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(switch_pair, "model", lambda prof: "swift-iq3_s")

    def gate(raw):
        raw_reply[0] = raw
        return switch_pair.completion(switch_pair.TARGET, tmp_path / "c.json", 1)

    out = gate(reply("ready", 5, "stop", "swift-iq3_s"))
    assert out["http"] == 200 and out["model"] == "swift-iq3_s"
    assert sent[-1]["model"] == "swift-iq3_s"
    for raw in (reply("", 5, "stop"), reply("ready", 0, "stop"), reply("x", 5, "content_filter"),
                reply("ready", 5, "stop", "swift-1.5-iq3_xxs"), reply("ready", 5000, "length")):
        with pytest.raises(RuntimeError, match="Completion gate failed"):
            gate(raw)


@requires_fixtures
def test_owned_stop_and_target_success_leaves_target_serving(monkeypatch, tmp_path):
    world = make_world(monkeypatch, tmp_path)
    try:
        assert switch_pair.run(args_for()) == 0
        status = json.loads((tmp_path / "t-switch-status.json").read_text())
        assert status["success"] is True and status["outage_started"] is True
        assert world["stops"] == [(100, [100, 101, 102])]  # stopped once; target left serving
        assert not (tmp_path / "old.sock").exists() and not (tmp_path / "old2.sock").exists()
        assert (tmp_path / "tgt.sock").exists()  # target sockets survive a successful switch
        assert world["launches"] == [(str(switch_pair.TARGET["launcher"]),
                                      str(tmp_path / "t-switch-console.log"))]
        assert world["completions"] == ["swift-iq3_s"]
        assert status["target_fill"] == TARGET_FILL  # actual slots/GiB/free, not 4000/6.07
        assert status["target_state"]["supervisor_pid"] == 200
        assert status["target_state"]["engine_pid"] == 203
        assert (tmp_path / "t-switch-target-completion.json").exists()
        assert signal.getsignal(signal.SIGINT) == signal.SIG_IGN  # cleanup is bounded
    finally:
        close_bound(world)


@requires_fixtures
def test_completion_failure_rolls_back_to_saved_old_profile(monkeypatch, tmp_path):
    world = make_world(monkeypatch, tmp_path, completion_fails=True)
    try:
        with pytest.raises(RuntimeError, match="Completion gate failed"):
            switch_pair.run(args_for())
        status = json.loads((tmp_path / "t-switch-status.json").read_text())
        assert status["success"] is False
        assert "Completion gate failed" in status["error"]
        assert world["stops"] == [(100, [100, 101, 102]), (200, [200, 201, 202, 203])]
        assert not (tmp_path / "tgt.sock").exists()  # only observed target sockets released
        assert world["launches"][1][0] == str(switch_pair.OLD["launcher"])  # restore ran
        assert world["completions"] == ["swift-iq3_s", "swift-1.5-iq3_xxs"]
        assert "did NOT succeed" in status["production"]
        assert status["restored"]["supervisor_pid"] == 300
    finally:
        close_bound(world)


@requires_fixtures
def test_interrupt_during_smoke_cleans_partial_target_then_restores(monkeypatch, tmp_path):
    world = make_world(monkeypatch, tmp_path, interrupt_smoke=True)
    try:
        with pytest.raises(KeyboardInterrupt):
            switch_pair.run(args_for())
        status = json.loads((tmp_path / "t-switch-status.json").read_text())
        assert status["success"] is False
        assert world["stops"][-1] == (200, [200, 201, 202, 203])  # owned partial tree, no collateral
        assert not (tmp_path / "tgt.sock").exists()
        assert status["target_processes"]  # partial-start ownership retained durably
        assert world["launches"][1][0] == str(switch_pair.OLD["launcher"])
        assert "did NOT succeed" in status["production"]
    finally:
        close_bound(world)


@requires_fixtures
def test_readiness_deadline_retains_partial_ownership(monkeypatch, tmp_path):
    world = make_world(monkeypatch, tmp_path, attach_pid=999)  # state names another owner
    try:
        with pytest.raises(RuntimeError, match="readiness deadline expired"):
            switch_pair.run(args_for())
        status = json.loads((tmp_path / "t-switch-status.json").read_text())
        assert "200" in status["target_processes"]  # root ownership recorded from launch on
        assert world["stops"] == [(100, [100, 101, 102]), (200, [200, 201, 202, 203])]
        assert world["launches"][1][0] == str(switch_pair.OLD["launcher"])
        assert not (tmp_path / "tgt.sock").exists()
    finally:
        close_bound(world)


@requires_fixtures
def test_unloaded_target_model_refused_then_restored(monkeypatch, tmp_path):
    world = make_world(monkeypatch, tmp_path, target_loaded=False)
    try:
        with pytest.raises(RuntimeError, match="loaded config model"):
            switch_pair.run(args_for())
        status = json.loads((tmp_path / "t-switch-status.json").read_text())
        assert status["success"] is False
        assert world["launches"][1][0] == str(switch_pair.OLD["launcher"])
    finally:
        close_bound(world)


@requires_fixtures
def test_stale_model_list_refused_then_restored(monkeypatch, tmp_path):
    world = make_world(monkeypatch, tmp_path, target_models=("swift-1.5-iq3_xxs",))
    try:
        with pytest.raises(RuntimeError, match="model list lacks"):
            switch_pair.run(args_for())
        status = json.loads((tmp_path / "t-switch-status.json").read_text())
        assert status["success"] is False
        assert world["launches"][1][0] == str(switch_pair.OLD["launcher"])
        assert world["completions"][-1] == "swift-1.5-iq3_xxs"
    finally:
        close_bound(world)


@requires_fixtures
def test_survivor_refuses_any_new_production_start(monkeypatch, tmp_path):
    world = make_world(monkeypatch, tmp_path, old_stop_survives=True)
    try:
        with pytest.raises(RuntimeError, match="did not exit"):
            switch_pair.run(args_for())
        status = json.loads((tmp_path / "t-switch-status.json").read_text())
        assert status["success"] is False
        assert "survived cleanup" in status["restore_error"]
        assert world["launches"] == []  # nothing started while owned processes survived
        assert status["outage_started"] is True
        assert status["old_pair"]["supervisor_pid"] == 100  # ownership info retained
        assert "NOT claimed" in status["production"]
    finally:
        close_bound(world)


@requires_fixtures
def test_foreign_socket_blocks_restore(monkeypatch, tmp_path):
    world = make_world(monkeypatch, tmp_path, completion_fails=True, foreign_old_socket=True)
    try:
        with pytest.raises(RuntimeError, match="Residual socket after old pair exit"):
            switch_pair.run(args_for())
        status = json.loads((tmp_path / "t-switch-status.json").read_text())
        assert "Foreign socket blocks the old restore" in status["restore_error"]
        assert world["launches"] == []  # no target, no restore past the foreign inode
        assert (tmp_path / "old.sock").read_text() == "foreign"  # foreign file preserved
        assert "NOT claimed" in status["production"]
    finally:
        close_bound(world)

@requires_fixtures
def test_occupied_port_blocks_restore(monkeypatch, tmp_path):
    world = make_world(monkeypatch, tmp_path, completion_fails=True, keep_target_listener=True)
    try:
        with pytest.raises(RuntimeError, match="Completion gate failed"):
            switch_pair.run(args_for())
        status = json.loads((tmp_path / "t-switch-status.json").read_text())
        assert "occupied; refusing the old restore" in status["restore_error"]
        assert len(world["launches"]) == 1
        assert "NOT claimed" in status["production"]
    finally:
        close_bound(world)


@requires_fixtures
def test_rollback_ownership_recorded_before_waiting(monkeypatch, tmp_path):
    world = make_world(monkeypatch, tmp_path, completion_fails=True)
    try:
        with pytest.raises(RuntimeError, match="Completion gate failed"):
            switch_pair.run(args_for())
        pre = world["restore_status"][0]
        assert pre["stage"] == "restoring_old"
        assert pre["restore_ownership"]["supervisor_pid"] == 300
        status = json.loads((tmp_path / "t-switch-status.json").read_text())
        assert status["restore_ownership"]["launcher"] == str(switch_pair.OLD["launcher"])
    finally:
        close_bound(world)


TREE = '''
import json, os, signal, subprocess, sys, time
mode, out = sys.argv[1], sys.argv[2]
kids = []
if mode == "root":
    kids.append(subprocess.Popen([sys.executable, __file__, "leaf", out + ".leaf"]))
    kids.append(subprocess.Popen([sys.executable, __file__, "engine", out + ".engine"]))
    while not (os.path.exists(out + ".leaf") and os.path.exists(out + ".engine")):
        time.sleep(0.05)
    json.dump({"root": os.getpid(), "leaf": kids[0].pid, "engine": kids[1].pid}, open(out, "w"))
    def stop(*_):
        for kid in kids:
            kid.terminate()
        for kid in kids:
            try:
                kid.wait(timeout=5)
            except Exception:
                kid.kill()
                kid.wait(timeout=5)
        os._exit(0)
    signal.signal(signal.SIGTERM, stop)
else:
    json.dump({mode: os.getpid()}, open(out, "w"))
while True:
    time.sleep(3600)
'''


@requires_fixtures
def test_real_proc_tree_readiness_and_real_rollback(monkeypatch, tmp_path):
    """Real CPU children under real pair_supervisor scans; HTTP/signals stay mocked."""
    tree = tmp_path / "tree.py"
    tree.write_text(TREE)
    monkeypatch.setattr(switch_pair, "EV", tmp_path)
    monkeypatch.setattr(switch_pair, "check_binaries", lambda: None)
    monkeypatch.setattr(switch_pair, "check_assets", lambda prof: None)
    monkeypatch.setattr(switch_pair, "check_profiles", lambda: None)
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(switch_pair.slots_window, "fill_report", lambda log: dict(TARGET_FILL))
    monkeypatch.setattr(switch_pair.runtime, "check_fill", lambda state: None)
    monkeypatch.setattr(switch_pair.runtime, "engine_of", lambda state: 103)
    monkeypatch.setattr(switch_pair, "model_list",
                        lambda port=8009: [entry("swift-iq3_s"), entry("swift-1.5-iq3_xxs")])
    calls = {"completion": [], "launches": [], "stops": []}
    monkeypatch.setattr(switch_pair, "completion",
                        lambda prof, raw_path, timeout: (
                            calls["completion"].append(switch_pair.model(prof)),
                            Path(raw_path).write_text("{}"),
                            dict(http=200, completion_tokens=7, finish="stop",
                                 model=switch_pair.model(prof)))[1])

    launched, listeners = {}, {"value": set()}

    def kill(proc):
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        deadline = time.monotonic() + 10
        while switch_pair.pair.process_token(proc.pid) is not None:  # reaped: /proc entry gone
            if time.monotonic() >= deadline:
                raise AssertionError("bounded tree cleanup timed out")
            time.sleep(0.05)

    def write_state(state_path, root_pid, marker, generation):
        for _ in range(50):
            if (tmp_path / marker).exists():
                break
            time.sleep(0.1)
        tree = json.loads((tmp_path / marker).read_text())
        state_path.write_text(json.dumps(dict(
            supervisor_pid=root_pid, generation=generation, state="attached",
            helper_pid=tree["leaf"],
            helper_start=switch_pair.pair.process_token(tree["leaf"])[1],
            server_pid=tree["engine"],
            server_start=switch_pair.pair.process_token(tree["engine"])[1],
            helper_log=str(tmp_path / "helper.log"))))
        return tree

    def launch(prof, log_path):
        marker = "target.marker" if prof is switch_pair.TARGET else "restore.marker"
        (tmp_path / marker).unlink(missing_ok=True)
        proc = subprocess.Popen([sys.executable, str(tree), "root", str(tmp_path / marker)])
        launched[proc.pid] = proc
        calls["launches"].append((str(prof["launcher"]), proc.pid))
        if prof is switch_pair.TARGET:
            for sock_path in (tmp_path / "tgt.sock", tmp_path / "tgt2.sock"):
                sock = socket.socket(socket.AF_UNIX)
                sock.bind(str(sock_path))
            procs = write_state(Path(switch_pair.TARGET["state"]), proc.pid, marker, 1)
            listeners["value"] = {procs["engine"]}
        else:
            procs = write_state(Path(switch_pair.OLD["state"]), proc.pid, marker, 4)
            listeners["value"] = {procs["engine"]}
        return proc

    def stop_group(child, timeout, known):
        calls["stops"].append(sorted(known))
        proc = launched.get(child.pid)
        if proc is not None:
            kill(proc)
        for sock_path in (tmp_path / "tgt.sock", tmp_path / "tgt2.sock"):
            sock_path.unlink(missing_ok=True)
        listeners["value"] = set()

    monkeypatch.setattr(switch_pair, "launch", launch)
    monkeypatch.setattr(switch_pair.pair, "stop_group", stop_group)
    monkeypatch.setattr(switch_pair, "port_listeners", lambda port=8009: set(listeners["value"]))

    # Real old tree and its live state, with the engine child owning the production port.
    old = subprocess.Popen([sys.executable, str(tree), "root", str(tmp_path / "old.marker")])
    launched[old.pid] = old
    time.sleep(0.6)
    old_state_path = tmp_path / "old-supervisor.json"
    monkeypatch.setitem(switch_pair.OLD, "state", old_state_path)
    monkeypatch.setitem(switch_pair.OLD, "sockets", [tmp_path / "old.sock", tmp_path / "old2.sock"])
    monkeypatch.setitem(switch_pair.TARGET, "sockets", [tmp_path / "tgt.sock", tmp_path / "tgt2.sock"])
    monkeypatch.setitem(switch_pair.TARGET, "state", tmp_path / "tgt-supervisor.json")
    write_state(old_state_path, old.pid, "old.marker", 3)
    for sock_path in (tmp_path / "old.sock", tmp_path / "old2.sock"):
        sock = socket.socket(socket.AF_UNIX)
        sock.bind(str(sock_path))
    listeners["value"] = {json.loads((tmp_path / "old.marker").read_text())["engine"]}

    def command(pid):
        if pid == old.pid:
            return [sys.executable, PAIR_SCRIPT, "run", "--launcher", str(switch_pair.OLD["launcher"]),
                    "--state", str(old_state_path)]
        for marker in ("target.marker", "restore.marker"):
            if (tmp_path / marker).exists():
                tree = json.loads((tmp_path / marker).read_text())
                if pid == tree["engine"]:
                    return [TARGET_EXE]
        return []

    monkeypatch.setattr(switch_pair.runtime, "command", command)
    (tmp_path / "helper.log").write_text("fake\n")
    try:
        args = args_for(family="t-real", startup=5)
        args.expected_supervisor = old.pid
        assert switch_pair.run(args) == 0
        status = json.loads((tmp_path / "t-real-status.json").read_text())
        assert status["success"] is True
        target_root = calls["launches"][0][1]
        assert str(target_root) in status["target_processes"]  # real /proc descendants recorded
        assert target_root not in [pid for stop in calls["stops"] for pid in stop]  # target never stopped
        assert calls["completion"] == ["swift-iq3_s"]  # success, no rollback completion
    finally:
        for proc in list(launched.values()):
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                pass
        for sock_path in ("old.sock", "old2.sock", "tgt.sock", "tgt2.sock"):
            (tmp_path / sock_path).unlink(missing_ok=True)


@requires_fixtures
def test_foreign_listener_refused_before_any_http(monkeypatch, tmp_path):
    world = make_world(monkeypatch, tmp_path, foreign_listener=True)
    try:
        with pytest.raises(RuntimeError, match="not owned by the newly launched server"):
            switch_pair.run(args_for())
        assert [c for c in world["model_calls"] if c == "target"] == []  # /v1/models never asked of a stranger
        assert world["completions"] == ["swift-1.5-iq3_xxs"]  # only the rollback proof, never the stranger
        assert world["launches"][1][0] == str(switch_pair.OLD["launcher"])  # rollback ran
        assert "swift-1.5-iq3_xxs" in world["completions"]  # rollback proof uses saved profile
    finally:
        close_bound(world)


@requires_fixtures
def test_rollback_refuses_stranger_endpoint_before_http(monkeypatch, tmp_path):
    world = make_world(monkeypatch, tmp_path, completion_fails=True, keep_target_listener=True)
    try:
        with pytest.raises(RuntimeError, match="Completion gate failed"):
            switch_pair.run(args_for())
        status = json.loads((tmp_path / "t-switch-status.json").read_text())
        assert "occupied; refusing the old restore" in status["restore_error"]
        assert [call for call in world["model_calls"] if call == "restore"] == []
        assert world["completions"] == ["swift-iq3_s"]  # only the target probe, no restore HTTP
    finally:
        close_bound(world)


@requires_fixtures
def test_durable_status_is_written_before_the_first_signal(monkeypatch, tmp_path):
    world = make_world(monkeypatch, tmp_path)
    seen = []

    def stop_group(child, timeout, known):
        seen.append((child.pid, json.loads((tmp_path / "t-switch-status.json").read_text())))
        world["stops"].append(child.pid)
        for pid in (100, 101, 102):
            world["live"].pop(pid, None)
        world["listeners"] = set()

    monkeypatch.setattr(switch_pair.pair, "stop_group", stop_group)
    try:
        assert switch_pair.run(args_for()) == 0
        pid, status = seen[0]
        assert pid == 100
        assert status["stage"] == "stopping_owned_supervisor"
        assert status["outage_started"] is True
    finally:
        close_bound(world)


@requires_fixtures
@pytest.mark.parametrize("restore", [False, True], ids=["target", "rollback"])
def test_attached_state_precedes_owned_listener(monkeypatch, tmp_path, restore):
    """The real supervisor publishes attached before the HTTP server binds."""
    world = make_world(monkeypatch, tmp_path, completion_fails=restore)
    original = switch_pair.port_listeners
    attempts, sleeps = [], []
    phase = "restore" if restore else "target"

    def delayed_listener(port=8009):
        if world["launches"] and world["restoring"] == restore:
            attempts.append(phase)
            if len(attempts) <= 2:
                assert phase not in world["model_calls"]
                return set()
        return original(port)

    monkeypatch.setattr(switch_pair, "port_listeners", delayed_listener)
    monkeypatch.setattr(switch_pair.time, "sleep", sleeps.append)
    try:
        if restore:
            with pytest.raises(RuntimeError, match="Completion gate failed"):
                switch_pair.run(args_for(startup=2))
            status = json.loads((tmp_path / "t-switch-status.json").read_text())
            assert "restore_error" not in status
            assert status["restored"]["completion"]["http"] == 200
        else:
            assert switch_pair.run(args_for(startup=2)) == 0
            status = json.loads((tmp_path / "t-switch-status.json").read_text())
            assert status["success"] is True
        assert len(attempts) >= 3
        assert sleeps == [1, 1]
        assert phase in world["model_calls"]
    finally:
        close_bound(world)


@pytest.mark.parametrize("label", ["newly launched", "restored"])
@pytest.mark.parametrize("failure, message", [
    ("foreign", "not owned"), ("unattributable", "not owned"),
    ("timeout", "deadline expired"), ("helper", "token changed"),
    ("server", "token changed"), ("supervisor", "exited"),
])
def test_listener_wait_fails_closed_without_http(monkeypatch, label, failure, message):
    child = FakeChild(200)
    state = dict(helper_pid=201, helper_start="h", server_pid=202, server_start="s")
    known = {200: "root", 201: "h", 202: "s"}
    live, clock = dict(known), [0]
    monkeypatch.setattr(switch_pair.pair, "descendants", lambda root, seed: dict(seed))
    monkeypatch.setattr(switch_pair.pair, "alive", lambda pid, token: live.get(pid) == token)
    monkeypatch.setattr(switch_pair.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(switch_pair.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(switch_pair.urllib.request, "urlopen",
                        lambda *a, **kw: pytest.fail("HTTP before owned-listener proof"))
    listeners = {777} if failure == "foreign" else {-1} if failure == "unattributable" else set()
    monkeypatch.setattr(switch_pair, "port_listeners", lambda: listeners)
    if failure in ("helper", "server"):
        live[state[f"{failure}_pid"]] = "reused"
    if failure == "supervisor":
        monkeypatch.setattr(child, "poll", lambda: 0)
    with pytest.raises(RuntimeError, match=message):
        switch_pair.wait_owned_listener(child, state, known, 2, label)
    assert known[200] == "root"
    assert clock[0] == (2 if failure == "timeout" else 0)
