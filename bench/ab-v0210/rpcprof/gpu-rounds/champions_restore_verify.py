#!/usr/bin/env python3
"""champions_restore_verify.py — the verdict half of champions_restore.sh, offline.

It answers booleans the shell cannot decide honestly on its own:

  identity  is the supervisor's CURRENT generation serving, and is the running
            process really the pinned production binary?
  models    did /v1/models answer as an HTTP success with a real model list?
  health    was the completion a real finite generation?

Nothing here opens a socket, spawns a process or signals one. The shell owns curl and the
deadline; this file only reads the supervisor's own state file, /proc, and log files, and
turns them into one verdict per call. Splitting it out is what makes the acceptance rules
testable offline against a scratch state file and a scratch /proc tree, which a bash
heredoc is not.

Why the state file is the authority: `launch-v0238.sh` runs the pair through
tools/mixed_gpu/pair_supervisor.py, which retries a failed boot in a NEW generation with a
NEW engine log. On 2026-11-18 a restore latched onto generation 1's log and timed out at
17:02 while generation 2 was already serving. So every verdict here is keyed to what the
supervisor currently declares — generation, engine PID, its /proc start time, and its log —
never to the first match found by scanning /proc.

  usage:
    champions_restore_verify.py identity --state F --engine PATH --expected-sha SHA
                                         [--log PATH] [--expect TOKEN] [--proc-root DIR]
    champions_restore_verify.py models   --status CODE --json F
    champions_restore_verify.py health   --status CODE --json F

Both HTTP verdicts need the status curl reported: a response body saved by a request that
failed or answered 5xx is not evidence of a serving pair.

Exits: 0 accepted, 1 rejected (retryable inside the restore's deadline),
3 rejected fatally (a wrong binary will not fix itself by waiting), 2 bad arguments.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

READY_MARK = "ready: http://"
TOKEN_FIELDS = ("generation", "pid", "start", "log")


class Rejected(Exception):
    """The requested check did not hold. exit=1 retry, exit=3 do not retry."""

    def __init__(self, reason: str, *, fatal: bool = False) -> None:
        super().__init__(reason)
        self.exit_code = 3 if fatal else 1


class Verdict:
    def __init__(self) -> None:
        self.fields: dict[str, str] = {}

    def set(self, key: str, value: object) -> "Verdict":
        self.fields[key] = str(value)
        return self

    def lines(self) -> list[str]:
        return [f"{key}={value}" for key, value in self.fields.items()]


# ---------------------------------------------------------------- supervisor state


def read_state(path: Path) -> dict[str, str]:
    """The supervisor's current declared pair, as strings.

    A missing or half-written file, or a generation whose engine the supervisor has not
    attached yet, is retryable: the next poll may well see it. It is never a pass.
    """
    try:
        raw = json.loads(path.read_text(errors="replace"))
    except FileNotFoundError as error:
        raise Rejected(f"supervisor state {path} is absent") from error
    except json.JSONDecodeError as error:
        raise Rejected(f"supervisor state {path} is not readable JSON: {error}") from error
    if not isinstance(raw, dict):
        raise Rejected(f"supervisor state {path} is not an object")
    generation = raw.get("generation")
    pid = raw.get("engine_pid")
    start = raw.get("engine_start")
    log = raw.get("engine_log")
    if generation is None or str(generation) == "":
        raise Rejected("supervisor state carries no generation")
    if not pid:
        raise Rejected("supervisor has not selected an engine PID for this generation yet")
    if not start:
        raise Rejected("supervisor state carries no engine start identity")
    if not log:
        raise Rejected("supervisor state carries no engine log for this generation")
    return {"generation": str(generation), "pid": str(pid), "start": str(start), "log": str(log)}


def announce_log(boot_output: Path) -> str:
    """The newest log path the launcher announced on its own stdout, or "".

    One announcement per generation, appended in order to the same stdout: tail -1 is the
    generation the launcher most recently started, which is exactly the one to wait on.
    """
    try:
        text = boot_output.read_text(errors="replace")
    except OSError:
        return ""
    newest = ""
    for line in text.splitlines():
        if not line.startswith("Starting mixed backend on "):
            continue
        marker = "; log: "
        head = line.split(marker, 1)
        if len(head) != 2:
            continue
        candidate = head[1].split("; helper:", 1)[0].strip()
        if candidate:
            newest = candidate
    return newest


# ---------------------------------------------------------------------------- /proc


def proc_start(proc_root: Path, pid: str) -> str:
    """Field 22 of /proc/<pid>/stat: the start time that makes a PID unambiguous.

    The comm field is bracketed and may hold spaces or ')', so split only after the last
    ')'. A zombie is refused; a recycled PID carries a different start time.
    """
    try:
        fields = (proc_root / pid / "stat").read_text(errors="replace").rsplit(")", 1)[1].split()
    except (OSError, IndexError) as error:
        raise Rejected(f"engine pid {pid} is not readable in {proc_root}") from error
    if fields[0] == "Z":
        raise Rejected(f"engine pid {pid} is a zombie")
    return fields[19]


def live_exe(proc_root: Path, pid: str) -> tuple[str, str]:
    """Where /proc/<pid>/exe points, and the sha256 of the image actually running.

    The hash is taken through the symlink, so it is the image the kernel has mapped, not
    whatever the on-disk path happens to hold right now: a replaced binary keeps serving
    from its old inode, and that is the difference between "the file on disk is correct"
    and "this process is correct".
    """
    link = proc_root / pid / "exe"
    try:
        target = os.readlink(link)
    except OSError as error:
        raise Rejected(f"cannot read {link}: {error.strerror}") from error
    if target.endswith(" (deleted)"):
        target = target[: -len(" (deleted)")]
    digest = hashlib.sha256()
    try:
        with open(link, "rb") as image:
            for block in iter(lambda: image.read(1 << 20), b""):
                digest.update(block)
    except OSError as error:
        raise Rejected(f"cannot read the running image of pid {pid}: {error.strerror}") from error
    return target, digest.hexdigest()


def last_ready(path: str) -> str:
    """The last ready line of one generation's wrapper log, "" when it has none.

    Engine logs carry NUL bytes, hence the translation. Only this generation's own file is
    ever consulted, so a ready line from a retired generation cannot satisfy a later one.
    """
    try:
        text = Path(path).read_text(errors="replace")
    except OSError:
        return ""
    ready = [line for line in text.replace("\0", "").splitlines() if READY_MARK in line]
    return ready[-1].strip() if ready else ""


# --------------------------------------------------------------------- HTTP payloads


def entry_id(entry: object) -> str:
    """The id of one model-list entry, "" when it has no usable one."""
    if not isinstance(entry, dict):
        return ""
    identifier = entry.get("id")
    return identifier.strip() if isinstance(identifier, str) else ""


def http_problem(status: str) -> str:
    """Why this HTTP status is not a success; "" when it is one.

    The transport is part of the evidence: a response file left on disk by a request that
    failed, timed out or came back 5xx says nothing about a serving pair.
    """
    try:
        code = int(status)
    except ValueError:
        return f"reported a non-numeric HTTP status {status!r}"
    if not 200 <= code < 300:
        return f"answered HTTP {code}"
    return ""


def check_models(status: str, payload: str) -> Verdict:
    """An HTTP success carrying a real model list, not merely a non-empty body.

    The old gate accepted any non-empty string, so a 502 page or an error body counted as
    a serving engine. Status, JSON shape and a non-empty id list must all hold.
    """
    problem = http_problem(status)
    if problem:
        raise Rejected(f"/v1/models {problem}")
    try:
        body = json.loads(payload)
    except json.JSONDecodeError as error:
        raise Rejected(f"/v1/models is not JSON ({error}); refusing to call an error page a "
                       "serving engine") from error
    if not isinstance(body, dict):
        raise Rejected(f"/v1/models returned a {type(body).__name__}, not an object")
    if body.get("error"):
        raise Rejected(f"/v1/models reported an error: {str(body['error'])[:200]}")
    data = body.get("data")
    if not isinstance(data, list) or not data:
        raise Rejected(f"/v1/models returned no model list (data={type(data).__name__})")
    ids = [identifier for identifier in (entry_id(entry) for entry in data) if identifier]
    if not ids:
        raise Rejected("/v1/models entries carry no usable model id")
    return Verdict().set("models", len(ids)).set("model_ids", ",".join(ids)[:200])


def check_health(status: str, payload: str) -> Verdict:
    """One real finite generation: an HTTP success, then text, a finish reason and a
    positive completion count.

    A saved body is not the completion. A 5xx page, a truncated reply or a 200 carrying an
    error document all have to fail here, or --health would vouch for a pair that only
    looked busy.
    """
    problem = http_problem(status)
    if problem:
        raise Rejected(f"the completion request {problem}")
    if not payload.strip():
        raise Rejected("the completion request returned an empty body")
    try:
        body = json.loads(payload)
    except json.JSONDecodeError as error:
        raise Rejected(f"unparseable health response: {error}") from error
    if not isinstance(body, dict):
        raise Rejected(f"health response is a {type(body).__name__}, not an object")
    if body.get("error"):
        raise Rejected(f"health request reported an error: {str(body['error'])[:200]}")
    try:
        choice = body["choices"][0]
        message = choice["message"]
        text = str(message.get("content") or message.get("reasoning_content") or "").strip()
        finish = choice.get("finish_reason")
        tokens = int((body.get("usage") or {}).get("completion_tokens", 0))
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise Rejected(f"unparseable health response: {error}") from error
    if not text:
        raise Rejected("health generation returned no text")
    if not finish:
        raise Rejected("health generation has no finish_reason")
    if tokens <= 0:
        raise Rejected(f"health generation reported {tokens} completion tokens")
    return Verdict().set("health", repr(text[:24])).set("finish", finish).set(
        "completion_tokens", tokens)


# ------------------------------------------------------------------------ identity


def check_identity(state_path: Path, engine_path: str, expected_sha: str, proc_root: Path,
                   announce_path: Path | None, expect: str) -> Verdict:
    """Everything that must be true of the CURRENT generation, in one pass.

    The announced log and the log the supervisor declares must be the same file: when they
    differ, a retry is in flight and neither is a trustworthy witness yet.
    """
    state = read_state(state_path)
    pid, start = state["pid"], state["start"]
    verdict = Verdict().set("generation", state["generation"]).set("pid", pid).set(
        "start", start).set("log", state["log"])
    if proc_start(proc_root, pid) != start:
        raise Rejected(f"engine pid {pid} start identity changed: /proc says "
                       f"{proc_start(proc_root, pid)}, supervisor declared {start}")
    target, sha = live_exe(proc_root, pid)
    want = os.path.realpath(engine_path)
    if os.path.realpath(target) != want:
        raise Rejected(f"pid {pid} is running {target}, not the production engine {want}",
                       fatal=True)
    if sha != expected_sha:
        raise Rejected(f"pid {pid} runs image sha {sha}, not the pinned {expected_sha}",
                       fatal=True)
    announced = announce_log(announce_path) if announce_path else ""
    if announced and announced != state["log"]:
        raise Rejected(f"launcher announced {announced} but the supervisor's current generation "
                       f"runs {state['log']}: a retry is in flight, not one serving pair")
    ready = last_ready(state["log"])
    if not ready:
        raise Rejected(f"no ready line yet in {state['log']}")
    verdict.set("exe", target).set("sha", sha[:16] + "…").set("ready", ready)
    token = ":".join(state[field] for field in TOKEN_FIELDS)
    if expect and expect != token:
        raise Rejected(f"generation changed under us: was {expect}, now {token}")
    return verdict.set("token", token)


# ----------------------------------------------------------------------------- cli


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="champions_restore_verify.py",
        description="verdict half of champions_restore.sh: identity, models and health checks")
    sub = parser.add_subparsers(dest="command", required=True)

    identity = sub.add_parser("identity", help="current-generation readiness + live engine image")
    identity.add_argument("--state", type=Path, required=True,
                          help="pair supervisor state file")
    identity.add_argument("--engine", required=True,
                          help="canonical production engine path")
    identity.add_argument("--expected-sha", required=True, help="pinned engine image sha256")
    identity.add_argument("--log", type=Path, default=None,
                          help="launcher stdout carrying the generation announcements")
    identity.add_argument("--expect", default="",
                          help="token from an earlier call; a different one is a generation change")
    identity.add_argument("--proc-root", type=Path, default=Path("/proc"),
                          help="test seam: an alternative /proc tree; production leaves it unset")

    models = sub.add_parser("models", help="validate a captured /v1/models payload")
    models.add_argument("--status", required=True, help="HTTP status code curl reported")
    models.add_argument("--json", type=Path, required=True, help="file holding the response body")

    health = sub.add_parser("health", help="validate a captured completion payload")
    health.add_argument("--status", required=True, help="HTTP status code curl reported")
    health.add_argument("--json", type=Path, required=True, help="file holding the response body")
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "identity":
            verdict = check_identity(args.state, args.engine, args.expected_sha,
                                     args.proc_root, args.log, args.expect)
        elif args.command == "models":
            verdict = check_models(args.status, args.json.read_text(errors="replace"))
        else:
            verdict = check_health(args.status, args.json.read_text(errors="replace"))
    except Rejected as error:
        print(f"{args.command.upper()}-FAIL {error}")
        return error.exit_code
    except OSError as error:
        print(f"{args.command.upper()}-FAIL cannot read {args.json}: {error}")
        return 1
    print(args.command.upper() + "-OK")
    for line in verdict.lines():
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
