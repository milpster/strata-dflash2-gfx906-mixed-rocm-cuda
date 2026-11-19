#!/usr/bin/env python3
"""Reserve-only TG/PP panel: the engine's own VRAM margins against this box's tuned ones.

  --prepare   write the frozen artifacts for this profile (CPU only)
  --dry-run   DEFAULT. Re-derive every pin and print the plan. Zero endpoint requests,
              zero health probes, zero process control, zero engine start.
  --run       PARENT ONLY. Boot one arm at a time, run the frozen pairs, then restore
              canonical production — on success and on failure alike.

The question is narrow and the design keeps it narrow. One binary, one helper, one
serving wrapper, one config — all three arms. The only thing that moves is the pair of
engine VRAM reserves:

    default_before  --vram-reserve-mib 700 --vram-reserve-later-mib 700   (engine default)
    optimized       --vram-reserve-mib 256 --vram-reserve-later-mib 448   (adopted 2026-11-19)
    default_after   same as default_before                                (drift bracket)

Both values are passed EXPLICITLY on both cards. Left out, the later card inherits the
first and the expert cache can size itself through the auto-lowering path; naming both
keeps that confound out of the comparison. default_after repeats the first arm so the
run-to-run spread is measured in the same session rather than assumed.

A speed claim about a smaller reserve is only worth reading next to its memory cost and
its capacity: a different reserve can change the expert cache the engine actually
commits, even though every fixed budget in the config is identical. So this panel captures
the boot capacity lines, a passive residency sample at boot and around each pair, and the
running engine's own argv (which is where the reserve override has to be visible).

Safety inherits champions_panel.Panel: one pair at a time, stop-all through the on-disk
wrapper, per-arm sockets only this panel created, boot and measurement deadlines, no
retry that could overlap benchmark traffic, and restoration in a finally block.
"""
from __future__ import annotations

# pyright: reportMissingImports=false
import argparse
import dataclasses
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import champions_panel as PANEL  # noqa: E402
import champions_prepare as PREP  # noqa: E402

HERE = Path(__file__).resolve().parent
VERIFY = HERE / "champions_restore_verify.py"
WATCH = HERE / "watch_gpu_residency.py"
DEFAULT_OUT = PREP.HERE / "out" / "reserve-compare-20261119-134637"

# Frozen base: the config and launcher production is actually running, taken as git blobs
# at HEAD 3ad3b70034 — not a historical champion's adopted state, and never today's file
# relabelled. The engine is the canonical v0238p0 image every arm symlinks to.
BASE_COMMIT = "3ad3b700341650349762b6c126b5701984bf003b"
CONFIG_BLOB = "b21ff4ff18c1ca6306a7df4c271b32a7a6cacb1b"
LAUNCHER_BLOB = "5a735f97341f572dfca68bbd751b7e34a88ddd0d"
ACTIVE_CONFIG_SHA256 = "b221140fc394957d9a52cf475d1539340d42edc859ff7ce8e4d06d8d6bf35df8"
ACTIVE_LAUNCHER_SHA256 = "03535a66bdc6b72d26164619b920b5f30e24b1e01382085b36bf34db9761352c"
SOURCE_COMMIT = "42cb41b314"

# What the engine does when neither flag is given (src/program/generate.cpp at the pinned
# source: `int vram_reserve_mib = 700;`, `int vram_reserve_later_mib = -1;` → the later
# card takes the first card's value). Passed explicitly here so the auto path is not what
# is being measured.
ENGINE_DEFAULT_RESERVES = {"--vram-reserve-mib": "700", "--vram-reserve-later-mib": "700"}
# The 256/448 arm (production from 2026-11-19 morning, commit baaf65ba99, config sha
# 95ef3693…; the operator switched production to the 700/700 defaults the same evening
# after the margin comparison — config sha b221140f…).
OPTIMISED_RESERVES = {"--vram-reserve-mib": "256", "--vram-reserve-later-mib": "448"}

# Production's own request settings (prod-config-v0238.json "sampling" + "reasoning_effort").
# The comparison measures the settings the box actually serves under, not the driver's
# parity defaults: no temperature 0, no enable_thinking, no reasoning_effort low.
PRODUCTION_SAMPLING = {"temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.0,
                       "presence_penalty": 0.0, "repetition_penalty": 1.0}
PRODUCTION_REASONING_EFFORT = "high"
REQUEST_TEMPLATE = {"model": "ud-iq3_xxs", **PRODUCTION_SAMPLING,
                    "reasoning_effort": PRODUCTION_REASONING_EFFORT, "stream": False}

RESERVE_ORDER = ("default_before", "optimized", "default_after")

# One shape, the panel's existing short16k context size, declared before any measurement.
RESERVE_SHAPES = (PREP.Shape("short16k", ctx=88400, max_tokens=256, warmup_pairs=1,
                             measured_pairs=3, request_timeout_s=2400),)

# Boot-time lines that show what the reserve actually bought or cost. Captured verbatim,
# never interpreted by the harness.
CAPACITY = re.compile(r"expert|cache|slot|reserve|vram|guard|headroom|band|limiter|"
                      r"resident|keep|freed|capacity|kv ", re.IGNORECASE)
CAPACITY_MAX = 150
# Enough of the environment to reconstruct the run without hoarding unrelated secrets.
ENV_PREFIXES = ("STRATA_", "HSA_", "GPU_", "LD_LIBRARY", "CUDA_", "ROCM", "PATH", "HOME")

# /proc/<pid>/stat field numbers (1-based, as `man proc` counts them): the state code is
# field 3 and the start time that makes a PID unambiguous is field 22.
STATE_FIELD = 3
STARTTIME_FIELD = 22


def proc_stat_fields_after_comm(stat_text: str) -> list[str]:
    """The /proc/<pid>/stat fields after the bracketed comm, split the safe way.

    comm is wrapped in parentheses and may itself contain spaces or ')', so splitting the
    whole line is wrong: only split after the LAST ')'. The first field that yields is the
    state code (field 3), so starttime (field 22) sits at index STARTTIME_FIELD-STATE_FIELD.
    Mirrors the box's own champions_restore_verify.proc_start parser.
    """
    return stat_text.rsplit(")", 1)[1].split()


def reserve_arm(name: str, label: str, reserves: dict[str, str], notes: tuple[str, ...]) -> PREP.Arm:
    """One arm of the reserve profile: the canonical stack, its reserves named."""
    return dataclasses.replace(
        BASE_ARM, name=name, label=label, notes=notes,
        arg_overrides=tuple((flag, reserves[flag]) for flag in PREP.RESERVE_FLAGS))


BASE_ARM = PREP.Arm(
    name="base",
    label="canonical v0238p0 stack at HEAD 3ad3b70034",
    request_date="2026-11-19",
    engine=PREP.CANONICAL_ENGINE,
    engine_sha=PREP.CANONICAL_ENGINE_SHA,
    source_commit=SOURCE_COMMIT,
    lineage="v0238p0 (source 42cb41b314)",
    config_ref=f"{BASE_COMMIT}:prod-config-v0238.json",
    config_blob=CONFIG_BLOB,
    launcher_ref=f"{BASE_COMMIT}:launch-v0238.sh",
    launcher_blob=LAUNCHER_BLOB,
    notes=("Engine, helper, serving wrapper and config are byte-identical in every arm of "
           "this profile; only the two VRAM reserves differ.",),
)

ARMS = (
    reserve_arm("default_before", "engine default reserves 700/700", ENGINE_DEFAULT_RESERVES,
                BASE_ARM.notes + (
                    "Both cards named explicitly: the later card would otherwise inherit the "
                    "first through the auto path, which is not what is being measured.",)),
    reserve_arm("optimized", "adopted reserves 256/448 (prod config 95ef3693)", OPTIMISED_RESERVES,
                BASE_ARM.notes + (
                    "These are production's current values, so a win here is a win the box "
                    "is already meant to be getting.",)),
    reserve_arm("default_after", "engine default reserves 700/700, repeated last", ENGINE_DEFAULT_RESERVES,
                BASE_ARM.notes + (
                    "Drift bracket: identical bytes to default_before, run last. Its spread "
                    "against default_before is the noise floor every arm-to-arm delta is read "
                    "against — a delta inside it is not a speed effect.",)),
)

RESERVE_DEVIATIONS = (
    "the only settings that move are --vram-reserve-mib and --vram-reserve-later-mib; a "
    "dry-run refuses the run if any other field differs once the harness paths are blanked.",
    "all three arms symlink the SAME engine, helper and serving wrapper; no binary, build or "
    "precision differs, so nothing here is a quality claim — the unchanged binary and the "
    "matched sampling settings prove no intentional quality reduction, nothing more.",
    "conversation-cache-dir is retargeted to a per-arm empty directory: every arm boots cold "
    "and the production cache inventory is never mutated. Identical treatment for all arms.",
    "requests pin production's sampling and reasoning effort through the frozen request "
    "template, not the driver's temperature-0 parity defaults; enable_thinking and anything "
    "else unlisted is left to the current server. Hashes are recorded, completions are not "
    "compared.",
    "one stack at a time on 127.0.0.1:8009, the launcher's own port: production must be down "
    "before --run and comes back through the restore step, never as a second stack.",
    "the boot log, engine log, sockets and driver records are per-arm, so no gate can read a "
    "stale shared log and no arm can write another arm's evidence.",
)


def profile_delta() -> dict:
    return {flag: {arm.name: dict(arm.arg_overrides)[flag] for arm in ARMS}
            for flag in PREP.RESERVE_FLAGS}


PROFILE = PREP.Profile(
    key="reserve-only-20261119",
    plan="Reserve-only TG/PP comparison: engine default 700/700 vs adopted 256/448, "
         "with the default repeated last as the drift bracket (requested 2026-11-19)",
    arms=ARMS,
    shapes=RESERVE_SHAPES,
    arm_order=RESERVE_ORDER,
    deviations=RESERVE_DEVIATIONS,
    profile_delta=profile_delta(),
    request_template=REQUEST_TEMPLATE,
)


# --------------------------------------------------------------------- preparation


def prepare(out: Path) -> dict:
    return PREP.prepare(out, PROFILE)


def _reserve_flag(field: str) -> str:
    """`args/--vram-reserve-mib[39]` -> `--vram-reserve-mib`; anything else unchanged."""
    match = re.match(r"^args/(--[^[]+)\[\d+\]$", field)
    return match.group(1) if match else field


def profile_problems(out: Path) -> list[str]:
    """What only THIS profile can check. Empty means the run is allowed to be considered."""
    problems: list[str] = []
    manifest = json.loads((out / "manifest.json").read_text())
    order = list(manifest.get("arm_order") or [])
    if order != list(RESERVE_ORDER):
        problems.append(f"arm order {order} != the frozen {list(RESERVE_ORDER)}: "
                        "the drift bracket must sit last and identical to the first arm")
    arms = manifest.get("arms") or {}

    # 1. one image, three times. If this fails, the comparison is an engine comparison.
    for name, spec in arms.items():
        got = spec["engine"]["sha256"]
        if got != PREP.CANONICAL_ENGINE_SHA:
            problems.append(f"{name}: engine {got[:16]} != canonical {PREP.CANONICAL_ENGINE_SHA[:16]}")
        for kind, frozen in (("helper", PREP.HELPER_SHA), ("server", PREP.SERVER_SHA)):
            if spec[kind]["sha256"] != frozen:
                problems.append(f"{name}: {kind} sha {spec[kind]['sha256'][:16]} != frozen {frozen[:16]}")
        if Path(os.readlink(os.path.join(spec["slot"], "rpcw-hip", "strata"))) != PREP.CANONICAL_ENGINE:
            problems.append(f"{name}: slot does not point at the canonical engine")

    # 2. the baseline still exists as it did at prepare time, and the restore pin matches it.
    restore = manifest.get("restore") or {}
    if restore.get("engine_sha") != PREP.CANONICAL_ENGINE_SHA:
        problems.append(f"restore pin engine sha {restore.get('engine_sha')} != canonical")
    for field, path, expected in (("config_sha256", PREP.PROD_CONFIG, ACTIVE_CONFIG_SHA256),
                                  ("launcher_sha256", PREP.PROD_LAUNCHER, ACTIVE_LAUNCHER_SHA256)):
        on_disk = PREP.sha256_path(path) if path.is_file() else "<missing>"
        if on_disk != expected:
            problems.append(f"{path.name} on disk is {on_disk[:16]}, not the baseline {expected[:16]}: "
                            "production moved since this profile was frozen")
        if restore.get(field) != expected:
            problems.append(f"restore pin {field} is {str(restore.get(field))[:16]}, not {expected[:16]}")
    for name, spec in arms.items():
        for kind, expected in (("config_snapshot", CONFIG_BLOB), ("launcher_snapshot", LAUNCHER_BLOB)):
            if spec[kind]["blob"] != expected:
                problems.append(f"{name}: {kind} blob {spec[kind]['blob']} != baseline {expected}")
        if spec["source_commit"] != SOURCE_COMMIT:
            problems.append(f"{name}: source commit {spec['source_commit']} != {SOURCE_COMMIT}")

    # 3. the claim itself: blank the harness paths, and nothing but the lever may remain.
    settings = {name: PREP.settings_only(json.loads(Path(spec["frozen_config"]["path"]).read_text()))
                for name, spec in arms.items()}
    diffs = PREP.cross_arm_setting_diffs(settings)
    for field, by_arm in diffs.items():
        flag = _reserve_flag(field)
        if flag not in PREP.RESERVE_FLAGS:
            problems.append(f"arms differ outside the lever: {field} = {by_arm}")
    for flag in PREP.RESERVE_FLAGS:
        values = {name: dict(spec.get("profile_arg_overrides") or {}).get(flag)
                  for name, spec in arms.items()}
        if len({values[name] for name in order}) < 2:
            problems.append(f"lever {flag} does not vary across the arms ({values}): "
                            "the run would measure the same stack twice")
        expected = {name: PROFILE.profile_delta[flag][name] for name in order}
        if values != expected:
            problems.append(f"lever {flag} is {values}, the profile declared {expected}")

    # 4. the request template the driver is pointed at, and what production asks for.
    template = manifest.get("request_template") or {}
    if not template:
        problems.append("manifest carries no request_template: the legs would inherit the "
                        "driver's temperature-0 parity defaults")
    else:
        path = Path(template["path"])
        if not path.is_file():
            problems.append(f"request template missing: {path}")
        elif PREP.sha256_path(path) != template["sha256"]:
            problems.append(f"request template sha drift: {path}")
        elif template["settings"] != REQUEST_TEMPLATE:
            problems.append(f"request template settings {template['settings']} != the profile's "
                            f"{REQUEST_TEMPLATE}")
    live = json.loads(PREP.PROD_CONFIG.read_text()) if PREP.PROD_CONFIG.is_file() else {}
    if live.get("sampling") and live["sampling"] != PRODUCTION_SAMPLING:
        problems.append(f"production sampling is {live['sampling']}, the template pins "
                        f"{PRODUCTION_SAMPLING}: re-freeze the profile before comparing")
    if live.get("reasoning_effort") not in (None, PRODUCTION_REASONING_EFFORT):
        problems.append(f"production reasoning_effort is {live['reasoning_effort']!r}, the "
                        f"template pins {PRODUCTION_REASONING_EFFORT!r}")

    # 5. the sample plan, arithmetic first rather than after the fact.
    shapes = manifest.get("shapes") or []
    if [s["name"] for s in shapes] != [s.name for s in RESERVE_SHAPES]:
        problems.append(f"shapes {[s['name'] for s in shapes]} != {[s.name for s in RESERVE_SHAPES]}")
    for shape in shapes:
        if (shape["ctx_bytes"], shape["max_tokens"], shape["warmup_pairs"], shape["measured_pairs"]) \
                != (88400, 256, 1, 3):
            problems.append(f"shape {shape['name']} is not the declared 88400 B / 256 tok / "
                            f"1 warmup + 3 measured: {shape}")
    pairs = sum(s["pairs"] for s in shapes)
    corpus = [entry for entry in manifest.get("corpus", []) if entry.get("measured")]
    if len(corpus) != len(shapes) * 3:
        problems.append(f"measured corpus entries {len(corpus)} != {len(shapes) * 3}")
    tags = {entry["head_tag"] for entry in manifest.get("corpus", [])}
    if len(tags) != len(manifest.get("corpus", [])):
        problems.append("corpus head tags are not unique: a repeat could be served from cache")
    PANEL.progress(f"profile self-check: {len(order)} arms x {pairs} pairs "
                   f"({pairs * 2} requests per arm, {pairs * 2 * len(order)} total)")
    return problems


# ------------------------------------------------------------------ arm evidence


class ReservePanel(PANEL.Panel):
    """The champions lifecycle manager, plus the evidence a reserve claim needs.

    Nothing here measures: no requests, no engine calls, no process control beyond what
    Panel already owns. Each hook reads what the pair supervisor declared and what /proc
    says, and writes it under this run's own arm directory.
    """

    def __init__(self, out: Path, health: bool):
        super().__init__(out, health)
        self.tokens: dict[str, str] = {}

    # -- identity ----------------------------------------------------------------
    def identity(self, arm: str, phase: str) -> str:
        """Ask the restore verifier which generation is serving, and hold its token.

        The verifier is the acceptance half of the box's own restore path, so the arm is
        held to the same standard as production coming back: the pair the supervisor
        SELECTED, that PID's running image hashed from /proc/<pid>/exe, a ready line in
        the log the supervisor actually runs. No scanning /proc for the first strata.
        """
        spec = self.manifest["arms"][arm]
        pin = self.manifest.get("restore") or {}
        supervisor_out = self.arm_dir(arm) / "supervisor.out"
        directory = self.arm_dir(arm) / "identity"
        directory.mkdir(parents=True, exist_ok=True)
        log = directory / f"{phase}.log"
        command = [sys.executable, str(VERIFY), "identity",
                   "--state", pin.get("supervisor_state", str(PREP.ROOT / PREP.SUPERVISOR_STATE)),
                   "--engine", pin.get("engine", str(PREP.CANONICAL_ENGINE)),
                   "--expected-sha", spec["engine"]["sha256"],
                   "--log", str(supervisor_out)]
        if self.tokens.get(arm):
            command += ["--expect", self.tokens[arm]]
        result = subprocess.run(command, capture_output=True, text=True)
        log.write_text(result.stdout + result.stderr)
        fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        record = {"phase": phase, "arm": arm, "exit": result.returncode,
                  "token": fields.get("token", ""), "pid": fields.get("pid", ""),
                  "generation": fields.get("generation", ""), "log": fields.get("log", ""),
                  "verdict": (result.stdout.splitlines() or [""])[0], "file": str(log)}
        with (self.arm_dir(arm) / "identity.jsonl").open("a") as sink:
            sink.write(json.dumps(record, sort_keys=True) + "\n")
        if result.returncode != 0:
            PANEL.progress(f"{arm} {phase}: IDENTITY NOT ACCEPTED ({record['verdict']}) — see {log}")
            return ""
        token = fields.get("token", "")
        if not self.tokens.get(arm):
            self.tokens[arm] = token
        if token != self.tokens.get(arm):
            PANEL.progress(f"{arm} {phase}: generation changed under us "
                           f"({self.tokens.get(arm)} -> {token}); the pairs either side of it "
                           "are not one serving pair")
            return ""
        return token

    def boot(self, arm: str) -> dict:
        """Panel's boot (launcher announce + fresh ready line + engine identity), then bind
        the arm to the pair the supervisor selected and capture what it committed.

        Two things end the arm here rather than becoming a warning, because both mean the
        numbers would not belong to this arm: the supervisor's engine stderr is the file
        the timing parser reads, and the engine's own argv is where the arm's reserves have
        to be. Diagnostics are written first, then the boot fails, before any request.
        """
        record = super().boot(arm)
        spec = self.manifest["arms"][arm]
        state = self.supervisor_state()
        # The wrapper log carries the ready line; the metrics the parser reads are the
        # engine's own stderr. Absent or different, this arm has no trustworthy witness.
        declared = state.get("engine_stderr_log")
        if declared != spec["engine_log"]:
            raise PANEL.BootFailed(
                f"{arm}: supervisor's engine_stderr_log is {declared or '<missing>'}, not this "
                f"arm's engine log {spec['engine_log']}: the serve: timing lines this panel "
                "parses are not this arm's")
        if not self.identity(arm, "boot"):
            raise PANEL.BootFailed(f"{arm}: the selected pair did not pass identity at boot")
        runtime = self.capture_runtime(arm, state, "boot")
        self.capture_capacity(arm, record)
        if runtime.get("reserve_mismatch"):
            raise PANEL.BootFailed(
                f"{arm}: the running engine was started with {runtime['reserve_mismatch']}, not "
                f"this arm's {dict(spec.get('profile_arg_overrides') or {})} — see "
                f"{self.arm_dir(arm) / 'runtime.json'}; measuring a differently started engine "
                "would not be a reserve comparison")
        self.snapshot_residency(arm, "boot")
        self._pump_all()
        return record

    def run_pair(self, arm: str, shape: dict, pair: int):
        """Refuse to start a pair whose generation this arm cannot still vouch for.

        The matching check for AFTER the pair lives in accept_pair, so a pair that turns
        out to straddle a supervisor retry is never written to the counted evidence at all.
        """
        label = f"{shape['name']}-p{pair}"
        if not self.identity(arm, f"{label}-before"):
            PANEL.progress(f"{arm} {label}: refusing to start the pair — the serving generation "
                           "is not the one this arm booted")
            return None
        return super().run_pair(arm, shape, pair)

    def accept_pair(self, arm: str, shape: dict, pair: int, result) -> str:
        """Post-pair acceptance: residency sample, then the second identity check.

        Returning a reason here means Panel never persists the pair, so it cannot reach
        the medians or make the arm look complete.
        """
        label = f"{shape['name']}-p{pair}"
        self.snapshot_residency(arm, f"{label}-after")
        if not self.identity(arm, f"{label}-after"):
            return ("identity did not hold across the pair: the pair supervisor's selected "
                    "generation changed or stopped being accepted, so the fresh and cached "
                    "passes are not two halves of one pair on one serving engine")
        return ""

    # -- evidence ----------------------------------------------------------------
    def supervisor_state(self) -> dict:
        pin = self.manifest.get("restore") or {}
        path = Path(pin.get("supervisor_state", str(PREP.ROOT / PREP.SUPERVISOR_STATE)))
        try:
            return json.loads(path.read_text(errors="replace"))
        except (OSError, ValueError) as error:
            PANEL.progress(f"cannot read supervisor state {path}: {error}")
            return {}

    def capture_runtime(self, arm: str, state: dict, phase: str) -> dict:
        """What is really running: selected generation, argv, environment, mapped image.

        The reserve override has to be visible in the ENGINE's argv. The config file being
        right is not enough — the server is what execs the engine with those arguments.
        """
        spec = self.manifest["arms"][arm]
        runtime = {"arm": arm, "phase": phase, "supervisor_state": {
            key: state.get(key) for key in ("supervisor_pid", "generation", "state", "engine_pid",
                                            "engine_start", "helper_pid", "helper_start",
                                            "server_pid", "engine_log", "engine_stderr_log",
                                            "helper_log", "stage_affinity", "timestamp")},
            "manifest_engine_log": spec["engine_log"],
            "config": {"path": spec["frozen_config"]["path"],
                       "sha256": spec["frozen_config"]["sha256"],
                       "reserves": dict(spec.get("profile_arg_overrides") or {})},
            "processes": {}}
        for role, key, start_key in (("engine", "engine_pid", "engine_start"),
                                     ("helper", "helper_pid", "helper_start"),
                                     ("server", "server_pid", "server_start")):
            pid = state.get(key)
            if not pid:
                runtime["processes"][role] = {"pid": None, "error": "supervisor declared none"}
                continue
            runtime["processes"][role] = self._process_evidence(int(pid), str(state.get(start_key, "")))
        engine = runtime["processes"].get("engine") or {}
        runtime["reserve_args_seen"] = {flag: engine.get("args", {}).get(flag)
                                        for flag in PREP.RESERVE_FLAGS}
        mismatches = {flag: {"argv": runtime["reserve_args_seen"][flag],
                             "config": dict(spec.get("profile_arg_overrides") or {})[flag]}
                      for flag in PREP.RESERVE_FLAGS
                      if runtime["reserve_args_seen"][flag]
                      != dict(spec.get("profile_arg_overrides") or {})[flag]}
        runtime["reserve_mismatch"] = mismatches
        path = self.arm_dir(arm) / "runtime.json"
        path.write_text(json.dumps(runtime, indent=1, sort_keys=True) + "\n")
        if mismatches:
            PANEL.progress(f"{arm}: MISMATCH the running engine carries {mismatches}, not this "
                           "arm's reserves — see runtime.json; boot() refuses to measure it")
        else:
            PANEL.progress(f"{arm}: engine argv carries the arm's reserves "
                           + " ".join(f"{flag}={runtime['reserve_args_seen'][flag]}"
                                      for flag in PREP.RESERVE_FLAGS))
        return runtime

    @staticmethod
    def _process_evidence(pid: int, declared_start: str) -> dict:
        base = Path("/proc") / str(pid)
        evidence: dict = {"pid": pid, "declared_start": declared_start}
        try:
            after = proc_stat_fields_after_comm(base.joinpath("stat").read_text(errors="replace"))
        except OSError:
            after = []
        # A zombie carries a stale start time and is never a witness to a live arm.
        if after and after[0] == "Z":
            evidence["start"] = None
            evidence["zombie"] = True
        else:
            evidence["start"] = after[STARTTIME_FIELD - STATE_FIELD] if len(after) >= STARTTIME_FIELD else None
        evidence["start_matches"] = bool(evidence["start"]) and evidence["start"] == declared_start
        for name in ("exe", "cwd"):
            try:
                evidence[name] = os.readlink(base / name)
            except OSError:
                evidence[name] = None
        try:
            argv = (base / "cmdline").read_bytes().split(b"\x00")
            evidence["argv"] = [part.decode("utf-8", "replace") for part in argv if part]
        except OSError as error:
            evidence["argv_error"] = str(error)
            return evidence
        # flag -> following argument, for the arguments this profile cares about.
        args: dict[str, str] = {}
        for index, token in enumerate(evidence["argv"]):
            if token.startswith("--") and index + 1 < len(evidence["argv"]) \
                    and not evidence["argv"][index + 1].startswith("--"):
                args[token] = evidence["argv"][index + 1]
        evidence["args"] = {flag: args.get(flag) for flag in PREP.RESERVE_FLAGS}
        try:
            entries = (pair.decode("utf-8", "replace")
                       for pair in (base / "environ").read_bytes().split(b"\x00") if pair)
            evidence["env"] = {key: value
                               for key, sep, value in (entry.partition("=") for entry in entries)
                               if sep and key.startswith(ENV_PREFIXES)}
            evidence["env_captured"] = len(evidence["env"])
        except OSError as error:
            evidence["env_error"] = str(error)
        try:
            maps = (base / "maps").read_text(errors="replace").splitlines()
            mapped = sorted({line.split(maxsplit=5)[5] for line in maps if len(line.split(maxsplit=5)) == 6})
            evidence["maps_total"] = len(mapped)
            evidence["maps_binary"] = [path for path in mapped
                                       if path.endswith(("/strata", "/strata-expert-helper-patient",
                                                         "/server.py")) or "libhipblas" in path
                                       or "libamdhip64" in path or "librocblas" in path]
        except OSError as error:
            evidence["maps_error"] = str(error)
        return evidence

    def capture_capacity(self, arm: str, boot_record: dict) -> int:
        """The boot's capacity lines, NUL-stripped and verbatim, for later reading."""
        spec = self.manifest["arms"][arm]
        destination = self.arm_dir(arm) / "capacity.txt"
        destination.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        total = 0
        for source in (boot_record.get("boot_log"), boot_record.get("helper_log"),
                       spec["engine_log"]):
            path = Path(source) if source else None
            if not path or not path.is_file():
                continue
            text = path.read_bytes().replace(b"\0", b"").decode("utf-8", "replace")
            hits = [line.strip() for line in text.splitlines() if CAPACITY.search(line)]
            total += len(hits)
            lines.extend(f"[{path.name}] {line}" for line in hits)
        destination.write_text("\n".join(lines[:CAPACITY_MAX]) + "\n")
        (self.arm_dir(arm) / "capacity.json").write_text(json.dumps(
            {"arm": arm, "matches": total, "saved": min(total, CAPACITY_MAX),
             "file": str(destination)}, indent=1, sort_keys=True) + "\n")
        PANEL.progress(f"{arm}: {total} capacity/reserve line(s) captured -> {destination.name}")
        return total

    def snapshot_residency(self, arm: str, phase: str) -> None:
        """One passive residency sample, through the box's own watcher, in a fresh dir."""
        self._pump_all()
        directory = self.arm_dir(arm) / "residency"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{phase}-{time.strftime('%H%M%S')}"
        while target.exists():
            target = target.with_name(target.name + "x")
        result = subprocess.run([sys.executable, str(WATCH), "--out", str(target),
                                 "--duration", "0", "--interval", "30"],
                                capture_output=True, text=True)
        summary = {}
        try:
            summary = json.loads((target / "summary.json").read_text())
        except (OSError, ValueError):
            pass
        PANEL.progress(f"{arm}: residency {phase} -> {target.name} "
                       f"(exit {result.returncode}, outcome {summary.get('outcome', '?')}, "
                       f"samples {summary.get('samples', '?')}, "
                       f"invalid {summary.get('invalid_identity_samples', '?')})")


# ------------------------------------------------------------------------- running


def run(out: Path, arms: list[str], health: bool) -> int:
    """Boot, measure, summarise, restore. PARENT ONLY: this starts engines."""
    if not (out / "manifest.json").is_file():
        print(f"refusing to run: {out / 'manifest.json'} missing — run --prepare first")
        return 2
    problems = PREP.verify(out) + profile_problems(out)
    for problem in problems:
        print(f"DRIFT: {problem}")
    if problems:
        print("refusing to start an engine: a frozen input drifted or the profile is not "
              "the narrow one it claims to be")
        return 2
    panel = ReservePanel(out, health)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    order = [name for name in panel.manifest.get("arm_order") or list(RESERVE_ORDER)
             if name in arms]
    outcome = "ok"
    try:
        for arm in order:
            PANEL.progress(f"=== arm {arm} ===")
            if not panel.run_arm(arm):
                outcome = f"{arm} incomplete"
                # A failed arm ends the sequence: the next arm would boot on top of whatever
                # this one left behind (a half-stopped pair, an unrestored socket), and a
                # drift bracket that does not follow its first arm measures nothing. Restore
                # still runs in the finally block.
                PANEL.progress(f"{arm} failed: stopping the sequence, no further arm boots")
                break
            panel.snapshot_residency(arm, "arm-end")
    except (KeyboardInterrupt, Exception) as failure:  # noqa: BLE001 - restore is the point
        outcome = f"aborted: {type(failure).__name__}: {failure}"
        PANEL.progress(outcome)
    finally:
        if not panel.restore():
            outcome += "; RESTORE FAILED — production may still be down, investigate now"
        elif outcome == "ok":
            outcome = "ok, production restored"
    print(f"panel outcome: {outcome}")
    return 0 if outcome == "ok, production restored" else 1


def dry_run(out: Path) -> int:
    """Re-derive every pin and print the plan. No socket, no endpoint, no process table."""
    problems = PREP.verify(out)
    if problems and not (out / "manifest.json").is_file():
        for problem in problems:
            print(f"PROBLEM: {problem}")
        print(f"nothing to dry-run: {out / 'manifest.json'} missing — run --prepare first")
        return 1
    problems += profile_problems(out)
    for problem in problems:
        print(f"PROBLEM: {problem}")
    print(PREP.plan_text(out))
    PANEL.selftest(out)
    print("dry-run: zero endpoint requests, zero health probes, zero stop-all, zero engines started")
    return 1 if problems else 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Reserve-only TG/PP panel: engine default 700/700 vs adopted 256/448 "
                    "(the default is a safe dry-run)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--arm", action="append", choices=list(RESERVE_ORDER),
                    help="restrict --run to these arms (default: all, in frozen order)")
    ap.add_argument("--prepare", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="the default")
    ap.add_argument("--run", action="store_true", help="PARENT ONLY: starts engines")
    ap.add_argument("--no-health", action="store_true",
                    help="skip the post-restore finite-output generation check")
    a = ap.parse_args()

    if a.prepare:
        prepare(a.out)
        print(f"prepared {a.out} for arms {', '.join(RESERVE_ORDER)}")
        return 0 if not (PREP.verify(a.out) + profile_problems(a.out)) else 1
    if not a.run:
        return dry_run(a.out)
    if not (a.out / "manifest.json").is_file():
        print(f"refusing to run: {a.out / 'manifest.json'} missing — run --prepare first")
        return 2
    return run(a.out, a.arm or list(RESERVE_ORDER), not a.no_health)


if __name__ == "__main__":
    raise SystemExit(main())
