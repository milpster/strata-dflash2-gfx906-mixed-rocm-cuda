#!/usr/bin/env python3
"""Explicit-execute pair switch: swift15 mixed production -> IQ3_S mixed production.

Plan mode (the default) mutates nothing and signals nothing. --execute stops only the
verified, owned old supervisor (re-identified just before the signal), releases only
its observed socket inodes, starts the target launcher under --recover, and proves
readiness from the target's own supervisor-bound state, the owned server's listener,
the config model listed as loaded, its actual fill with both channels, and a nonempty
completion; the target keeps serving after the runner exits. Any post-outage failure
stops only the owned target tree, releases only its observed sockets, and restores the
saved old profile under --recover with fill/channel/completion proof. Ownership is
recorded durably before and during every signal, including partial startup; a failed
restore records everything and never claims success.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
import urllib.error
import urllib.request

import pair_supervisor as pair
import recovery_window as runtime
import slots_window

WORK = runtime.WORK
ROOT = runtime.ROOT
EV = runtime.EV
PORT = 8009
BASE = f"http://127.0.0.1:{PORT}"
PAIR_SCRIPT = WORK / "tools/mixed_gpu/pair_supervisor.py"


def profile(name: str) -> dict:
    if name == "swift15":
        return dict(name=name, launcher=runtime.LAUNCHER, config=runtime.CONFIG,
                    state=runtime.STATE, sockets=list(runtime.SOCKETS))
    if name == "iq3_s":
        return dict(name=name,
                    launcher=ROOT / "launch-prod-swift-iq3_s-dflash-w4-q4_0-mixed.sh",
                    config=ROOT / "prod-config-swift-iq3_s-dflash-w4-q4_0-mixed.json",
                    state=ROOT / "run/prod-swift-iq3_s-mixed-supervisor.json",
                    sockets=[ROOT / "run/prod-swift-iq3_s-mixed.sock",
                             ROOT / "run/prod-swift-iq3_s-mixed2.sock"])
    raise ValueError(f"Unknown profile: {name}")


OLD = profile("swift15")
TARGET = profile("iq3_s")


def load(prof: dict) -> dict:
    return json.loads(Path(prof["config"]).read_text())


def model(prof: dict) -> str:
    name = load(prof).get("model_name")
    if not name:
        raise RuntimeError(f"Profile {prof['name']} has no config model_name")
    return str(name)


def port_listeners(port: int = PORT) -> set[int]:
    """Listening PIDs on the production port; -1 marks an unattributable listener."""
    try:
        out = subprocess.run(["ss", "-H", "-ltnp", f"sport = :{port}"],
                             capture_output=True, text=True, check=True).stdout
    except FileNotFoundError:
        raise RuntimeError("Cannot prove port ownership without ss; refusing")
    pids = set()
    for line in out.splitlines():
        match = re.search(r"pid=(\d+)", line)
        pids.add(int(match.group(1)) if match else -1)
    return pids


def check_binaries() -> None:
    """The protected HIP/CUDA binaries must match the accepted AB2 manifest."""
    manifest = json.loads((EV / "ab2-manifest.json").read_text())
    for name in ("rpcw-hip/strata", "rpcw-cuda/strata-expert-helper"):
        binary = WORK / "build-mixed-gpu" / name
        expected = next(row["sha256"] for row in manifest["fingerprints"]
                        if row["path"] == str(binary))
        if hashlib.sha256(binary.read_bytes()).hexdigest() != expected:
            raise RuntimeError(f"Validated binary changed: {binary}")


def check_assets(prof: dict) -> None:
    conf = load(prof)
    for exe in (ROOT / ".venv/bin/python", WORK / "build-mixed-gpu/rpcw-cuda/strata-expert-helper",
                Path(conf["exe"])):
        if not os.access(exe, os.X_OK):
            raise RuntimeError(f"Missing executable for {prof['name']}: {exe}")
    if not (WORK / "serve/server.py").is_file():
        raise RuntimeError(f"Missing server: {WORK / 'serve/server.py'}")
    if not Path(conf["tokenizer"]).is_dir():
        raise RuntimeError(f"Missing tokenizer for {prof['name']}: {conf['tokenizer']}")
    args = conf["args"]

    def arg(flag: str) -> str:
        return args[args.index(flag) + 1]

    for flag in ("--native", "--native-dense-gguf", "--ple-gguf", "--dflash", "--expert-profile"):
        path = Path(arg(flag))
        if not path.is_file():
            raise RuntimeError(f"Missing referenced asset for {prof['name']}: {path}")
    vision = conf.get("vision") or {}
    for key in ("exe", "mmproj", "model"):
        if key in vision and not Path(vision[key]).is_file():
            raise RuntimeError(f"Missing vision asset for {prof['name']}: {vision[key]}")
    if not Path(arg("--pack")).is_dir():
        raise RuntimeError(f"Missing pack directory for {prof['name']}")


def check_profiles() -> None:
    for prof in (OLD, TARGET):
        subprocess.run(["bash", "-n", str(prof["launcher"])], check=True)
        conf = load(prof)
        sockets = (str(conf.get("expert_helper_socket")), str(conf.get("expert_helper_socket2")))
        if sockets != tuple(str(p) for p in prof["sockets"]):
            raise RuntimeError(f"Profile {prof['name']} config sockets do not match its launcher")
        if conf.get("port") != PORT:
            raise RuntimeError(f"Profile {prof['name']} config port is not {PORT}")
    if model(OLD) == model(TARGET):
        raise RuntimeError("Profiles must expose distinct config models")


def check_target_sockets() -> None:
    for path in map(Path, TARGET["sockets"]):
        if path.exists() or path.is_symlink():
            raise RuntimeError(f"Refusing target with existing socket path: {path}")


def supervisor_identity(pid: int, prof: dict) -> tuple[str, dict[int, str], dict]:
    """Live, group-leader, argv- and state-verified supervisor; raises on any mismatch."""
    argv = runtime.command(pid)
    required = [str(PAIR_SCRIPT), str(prof["launcher"]), str(prof["state"])]
    if not all(value in argv for value in required):
        raise RuntimeError("Production supervisor identity mismatch; refusing outage")
    try:
        if os.getpgid(pid) != pid:
            raise RuntimeError("Supervisor is not its own process-group leader; refusing outage")
    except ProcessLookupError:
        raise RuntimeError("Supervisor vanished during identity check; refusing outage") from None
    state = json.loads(Path(prof["state"]).read_text())
    if state.get("supervisor_pid") != pid:
        raise RuntimeError("Production state belongs to another supervisor")
    token = pair.process_token(pid)
    if token is None or token[0] == "Z":
        raise RuntimeError("Expected supervisor is not a live process")
    known = pair.descendants(pid, {pid: token[1]})
    for name in ("helper", "server"):
        peer, start = state.get(f"{name}_pid"), state.get(f"{name}_start")
        if not (peer and start and pair.alive(peer, start)):
            raise RuntimeError(f"State's {name} start token is not a live peer")
        if known.get(peer) != start:
            raise RuntimeError(f"State's {name} start token is not the scanned tree token")
    return token[1], known, state


def old_identity(pid: int, result: dict) -> dict:
    start, known, state = supervisor_identity(pid, OLD)
    listeners = port_listeners()
    if not listeners:
        raise RuntimeError(f"Nothing serves port {PORT}; there is no pair to switch")
    if -1 in listeners:
        raise RuntimeError(f"Port {PORT} listener is unattributable; refusing outage")
    if not listeners <= set(known):
        raise RuntimeError(f"Port {PORT} listener is outside the owned tree; refusing outage")
    result["old_profile"] = {key: str(value) for key, value in OLD.items()}
    result["old_pair"] = dict(supervisor_pid=pid, supervisor_start=start, generation=state["generation"],
                              processes={str(p): tok for p, tok in known.items()})
    return dict(pid=pid, start=start, known=known)


def stop_old(old: dict, result: dict, status_path: Path, timeout: float) -> None:
    # Re-verify the exact same live supervisor immediately before any signal: a reused PID
    # with matching argv but a changed start token is a stranger and must be refused.
    new_start, new_known, _ = supervisor_identity(old["pid"], OLD)
    if new_start != old["start"]:
        raise RuntimeError("Supervisor start token changed before the signal; refusing outage")
    old["known"].update(new_known)  # token-preserving refresh: never drop recorded start tokens
    owned: dict[Path, tuple[int, int]] = {}
    for path in map(Path, OLD["sockets"]):
        token = pair.socket_token(path)
        if token is not None:
            owned[path] = token
    result["outage_started"] = True
    result["stage"] = "stopping_owned_supervisor"
    pair.write_json(status_path, dict(result, timestamp=time.time()))  # durable BEFORE signaling
    pair.stop_group(slots_window.ExistingProcess(old["pid"], old["start"]), timeout, old["known"])
    pair.release_sockets(owned)
    for path in map(Path, OLD["sockets"]):
        if path.exists() or path.is_symlink():
            raise RuntimeError(f"Residual socket after old pair exit: {path}")
    listeners = port_listeners()
    if listeners:
        raise RuntimeError(f"Port {PORT} is still owned after old pair exit: {sorted(listeners)}")


def launch(prof: dict, log_path: Path):
    return runtime.launch(True, log_path, prof["launcher"])


def engine_in_tree(known: dict[int, str], exe: Path) -> int:
    engines = [pid for pid, start in known.items()
               if pair.alive(pid, start) and (argv := runtime.command(pid)) and argv[0] == str(exe)]
    if len(engines) != 1:
        raise RuntimeError(f"Expected exactly one target engine, found {engines}")
    return engines[0]


def model_list(port: int = PORT) -> list[dict]:
    with urllib.request.urlopen(f"{BASE}/v1/models", timeout=3) as response:
        if response.status != 200:
            raise RuntimeError("Model list is not HTTP 200")
        data = json.loads(response.read())
    return list(data.get("data", []))


def loaded_models(entries: list[dict]) -> list[str]:
    return sorted(e["id"] for e in entries if e.get("id") and "alias_of" not in e
                  and e.get("status", {}).get("value") == "loaded")


def completion(prof: dict, raw_path: Path, timeout: float) -> dict:
    """One ready prompt against the profile's own config model; content is mandatory."""
    wanted = model(prof)
    body = json.dumps({"model": wanted, "messages": [
        {"role": "user", "content": "Reply with just the word ready."}],
        "max_tokens": 192, "temperature": 0}).encode()
    request = urllib.request.Request(f"{BASE}/v1/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        code = response.status
    Path(raw_path).write_bytes(raw)
    reply = json.loads(raw)
    choice = reply["choices"][0]
    content = choice["message"].get("content")
    tokens = reply.get("usage", {}).get("completion_tokens", 0)
    finish = choice.get("finish_reason")
    reply_model = reply.get("model")
    valid = (code == 200 and isinstance(content, str) and content
             and finish in ("stop", "length") and type(tokens) is int and 0 < tokens <= 192
             and (reply_model is None or reply_model == wanted))
    if not valid:
        raise RuntimeError(f"Completion gate failed: http={code} finish={finish} "
                           f"tokens={tokens} model={reply_model}")
    return dict(http=code, completion_tokens=tokens, finish=finish, model=wanted,
                request_sha256=hashlib.sha256(body).hexdigest())


def attached_local(child_pid: int, state_path: Path) -> dict | None:
    """State-bound predicate with NO HTTP: the file must name this exact launched supervisor."""
    state = json.loads(Path(state_path).read_text())
    if state.get("supervisor_pid") != child_pid:
        return None
    if state.get("generation", 0) < 1 or state.get("state") != "attached":
        return None
    if not all(state.get(f"{name}_{key}") is not None
               for name in ("helper", "server") for key in ("pid", "start")):
        return None
    return state


def wait_owned_listener(child, state: dict, known: dict[int, str], deadline: float,
                        label: str) -> None:
    """Attached precedes HTTP binding; wait only while the port is unoccupied."""
    while True:
        known.update(pair.descendants(child.pid, known))
        if child.poll() is not None:
            raise RuntimeError(f"The {label} supervisor exited before the port was ready")
        for name in ("helper", "server"):
            pid, token = state[f"{name}_pid"], state[f"{name}_start"]
            if known.get(pid) != token or not pair.alive(pid, token):
                raise RuntimeError(f"The {label} {name} token changed before the port was ready")
        listeners = port_listeners()
        if listeners == {state["server_pid"]}:
            return
        if listeners:
            raise RuntimeError(f"Port {PORT} is not owned by the {label} server: {sorted(listeners)}")
        if time.monotonic() >= deadline:
            raise RuntimeError(f"The {label} listener readiness deadline expired")
        time.sleep(1)


def readiness(child, start: str, known: dict[int, str], result: dict, status_path: Path,
              observed: dict[Path, tuple[int, int]], args) -> None:
    deadline = time.monotonic() + args.startup_timeout
    state = None
    while state is None:
        known.update(pair.descendants(child.pid, known))  # seed keeps root+history tokens
        for path in map(Path, TARGET["sockets"]):
            token = pair.socket_token(path)
            if token is not None:
                observed.setdefault(path, token)
        try:
            state = attached_local(child.pid, TARGET["state"])
        except (FileNotFoundError, json.JSONDecodeError):
            state = None
        result["target_processes"] = {str(p): tok for p, tok in known.items()}
        result["target_sockets"] = {str(p): list(t) for p, t in observed.items()}
        pair.write_json(status_path, dict(result, timestamp=time.time()))
        if state is not None:
            break
        if child.poll() is not None:
            raise RuntimeError("Target launcher exited before attaching")
        if time.monotonic() >= deadline:
            raise RuntimeError("Target readiness deadline expired")
        time.sleep(1)
    # Bind the published state to exactly this launched tree: tokens must equal the scan.
    for name in ("helper", "server"):
        pid, token = state[f"{name}_pid"], state[f"{name}_start"]
        if known.get(pid) != token or not pair.alive(pid, token):
            raise RuntimeError(f"Target {name} token does not match the launched tree")
    # Prove the owned server owns the port BEFORE any HTTP; a stranger's endpoint is never asked.
    wait_owned_listener(child, state, known, deadline, "newly launched")
    state["engine_pid"] = engine_in_tree(known, Path(load(TARGET)["exe"]))
    fill = slots_window.fill_report(Path(state["helper_log"]).read_text())
    if fill["slots"] <= 0:
        raise RuntimeError("Target fill reports no served slots")
    result["target_fill"] = fill
    result["target_state"] = state
    pair.write_json(status_path, dict(result, timestamp=time.time()))
    wanted = model(TARGET)
    entries = None
    while True:  # bounded loaded-model wait; ownership rechecked on every attempt
        if port_listeners() != {state["server_pid"]}:
            raise RuntimeError("Port ownership changed before the model check")
        entries = model_list()
        if wanted in loaded_models(entries):
            break
        if time.monotonic() >= deadline:
            (EV / f"{args.family}-target-models.json").write_text(json.dumps(entries, indent=2) + "\n")
            raise RuntimeError(f"Target model list lacks a loaded config model {wanted}: "
                               f"{[e.get('id') for e in entries]}")
        time.sleep(1)
    (EV / f"{args.family}-target-models.json").write_text(json.dumps(entries, indent=2) + "\n")
    if port_listeners() != {state["server_pid"]}:
        raise RuntimeError("Port ownership changed before the completion")
    result["target_completion"] = completion(TARGET, EV / f"{args.family}-target-completion.json",
                                             args.probe_timeout)
    # Revalidate the whole tree after the smoke; a peer that died mid-smoke is not success.
    known.update(pair.descendants(child.pid, known))
    if child.poll() is not None:
        raise RuntimeError("Target supervisor exited during the smoke")
    fresh = json.loads(Path(TARGET["state"]).read_text())
    for key in ("supervisor_pid", "generation", "state"):
        if fresh.get(key) != state.get(key):
            raise RuntimeError(f"Target state changed during the smoke: {key}")
    for name in ("helper", "server"):
        pid, token = fresh[f"{name}_pid"], fresh[f"{name}_start"]
        if known.get(pid) != token or not pair.alive(pid, token):
            raise RuntimeError(f"Target {name} died or changed identity during the smoke")
    if port_listeners() != {state["server_pid"]}:
        raise RuntimeError("Port ownership changed during the smoke")


def restore_old(result: dict, args) -> None:
    """Restart the saved old profile and prove it is useful again; never claim success here."""
    for path in map(Path, OLD["sockets"]):
        if path.exists() or path.is_symlink():
            raise RuntimeError(f"Foreign socket blocks the old restore: {path}")
    if port_listeners():
        raise RuntimeError(f"Port {PORT} is occupied; refusing the old restore")
    restored = launch(OLD, EV / f"{args.family}-restore-console.log")
    start = pair.process_token(restored.pid)[1]
    owned = {restored.pid: start}
    result["restore_ownership"] = dict(supervisor_pid=restored.pid, supervisor_start=start,
                                       launcher=str(OLD["launcher"]))
    result["stage"] = "restoring_old"
    pair.write_json(status_path_for(args), dict(result, timestamp=time.time()))
    deadline = time.monotonic() + args.restore_timeout
    state = None
    while state is None:  # local predicate only: no HTTP before the owned listener is proven
        owned.update(pair.descendants(restored.pid, owned))  # seed keeps root+history tokens
        try:
            state = attached_local(restored.pid, OLD["state"])
        except (FileNotFoundError, json.JSONDecodeError):
            state = None
        result["restore_ownership"]["processes"] = {str(p): t for p, t in owned.items()}
        pair.write_json(status_path_for(args), dict(result, timestamp=time.time()))
        if state is not None:
            break
        if restored.poll() is not None:
            raise RuntimeError("Restore launcher exited before attaching")
        if time.monotonic() >= deadline:
            raise RuntimeError("Restore readiness deadline expired")
        time.sleep(1)
    for name in ("helper", "server"):
        pid, token = state[f"{name}_pid"], state[f"{name}_start"]
        if owned.get(pid) != token:
            raise RuntimeError(f"Restored {name} is outside the launched tree")
    wait_owned_listener(restored, state, owned, deadline, "restored")
    state["engine_pid"] = runtime.engine_of(state)
    runtime.check_fill(state)
    entries = None
    while True:  # bounded loaded-model wait; ownership rechecked on every attempt
        if port_listeners() != {state["server_pid"]}:
            raise RuntimeError("Restored port ownership changed before the model check")
        entries = model_list()
        if model(OLD) in loaded_models(entries):
            break
        if time.monotonic() >= deadline:
            (EV / f"{args.family}-restored-models.json").write_text(json.dumps(entries, indent=2) + "\n")
            raise RuntimeError(f"Restored model list lacks a loaded {model(OLD)}")
        time.sleep(1)
    (EV / f"{args.family}-restored-models.json").write_text(json.dumps(entries, indent=2) + "\n")
    smoke = completion(OLD, EV / f"{args.family}-restored-completion.json", args.probe_timeout)
    result["restored"] = dict(supervisor_pid=restored.pid, supervisor_start=start,
                              owned={str(p): t for p, t in owned.items()},
                              state=state, completion=smoke)


def status_path_for(args) -> Path:
    return EV / f"{args.family}-status.json"


def run(args) -> int:
    status_path = status_path_for(args)
    if any(EV.glob(f"{args.family}-*")):
        raise RuntimeError(f"Refusing to overwrite evidence family: {args.family}")
    result = dict(family=args.family, started=time.time(), target="iq3_s",
                  success=False, stage="preflight")
    pair.write_json(status_path, dict(result, timestamp=time.time()))
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    child = None
    known: dict[int, str] = {}
    observed: dict[Path, tuple[int, int]] = {}
    old_known: dict[int, str] = {}
    try:
        check_binaries()
        check_profiles()
        check_assets(OLD)
        check_assets(TARGET)
        check_target_sockets()
        old = old_identity(args.expected_supervisor, result)
        old_known = old["known"]
        result["stage"] = "preflight_passed"
        pair.write_json(status_path, dict(result, timestamp=time.time()))
        stop_old(old, result, status_path, args.stop_timeout)
        result["stage"] = "old_stopped_launching_target"
        pair.write_json(status_path, dict(result, timestamp=time.time()))
        child = launch(TARGET, EV / f"{args.family}-console.log")
        start = pair.process_token(child.pid)[1]
        known.clear()
        known.update(pair.descendants(child.pid, {child.pid: start}))  # ownership from launch on
        result["target"] = dict(supervisor_pid=child.pid, supervisor_start=start,
                                launcher=str(TARGET["launcher"]),
                                processes={str(p): t for p, t in known.items()})
        pair.write_json(status_path, dict(result, timestamp=time.time()))
        readiness(child, start, known, result, status_path, observed, args)
        check_binaries()  # protected binaries unchanged at the success gate
        result["stage"] = "target_serving"
        result["success"] = True
        result["production"] = ("IQ3_S pair serves 8009 under --recover; the runner exits "
                                "and leaves it serving")
    except BaseException as error:
        result["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, signal.SIG_IGN)  # cleanup is bounded, not re-abortable
        try:
            if not result["success"]:
                if child is not None:
                    pair.stop_group(child, args.stop_timeout, known)
                    pair.release_sockets(observed)
                if result.get("outage_started"):
                    survivors = sorted({str(p) for p, tok in {**old_known, **known}.items()
                                        if pair.alive(p, tok)})
                    if survivors:
                        raise RuntimeError(
                            f"Owned processes survived cleanup; refusing production start: {survivors}")
                    restore_old(result, args)
                    result["production"] = ("old swift15 profile restored under --recover after "
                                            "a failed switch; the switch did NOT succeed")
        except BaseException as error:
            result["restore_error"] = f"{type(error).__name__}: {error}"
            result["production"] = ("restore FAILED; status retains ownership info; success is "
                                    "NOT claimed")
        finally:
            result["finished"] = time.time()
            pair.write_json(status_path, dict(result, timestamp=time.time()))
    print(json.dumps(result, indent=2), flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--family", default="iq3s-switch1")
    parser.add_argument("--expected-supervisor", type=int, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--startup-timeout", type=float, default=900)
    parser.add_argument("--stop-timeout", type=float, default=45)
    parser.add_argument("--probe-timeout", type=float, default=300)
    parser.add_argument("--restore-timeout", type=float, default=900)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,20}", args.family):
        parser.error("family must be a short lowercase name")
    if not args.execute:
        print("Plan: verify swift15 supervisor identity, group leadership, state peers, owned "
              "port listeners, AB2 binary hashes, launcher syntax, configs, assets, and absent "
              "IQ3_S sockets; mutate, signal and request nothing. --execute re-verifies the "
              "supervisor immediately before the signal, persists durable status, stops only "
              "that owned supervisor once with the existing bounded cleanup, releases only its "
              "observed socket inodes, starts the IQ3_S launcher --recover with ownership "
              "recorded from launch, requires the supervisor-bound attached state, the owned "
              "server as the only port listener, the config model listed as loaded, actual "
              "fill with both channels, and a nonempty HTTP200 completion, revalidates peers "
              "after the smoke, and leaves the target serving. Any post-outage failure stops "
              "only the owned target tree, releases only its observed sockets, refuses restore "
              "on survivors/foreign sockets/occupied port, and restores the saved swift15 "
              "profile --recover with fill/channel/completion proof; a failed restore records "
              "ownership info and never claims success.")
        return 0
    if min(args.startup_timeout, args.stop_timeout, args.probe_timeout, args.restore_timeout) <= 0:
        parser.error("timeouts must be positive")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
