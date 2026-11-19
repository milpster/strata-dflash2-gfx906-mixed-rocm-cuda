#!/usr/bin/env python3
"""CPU-only preparation for the 2026-11-18 three-champion TG/PP comparison.

Materialises frozen per-arm configs, launchers, binary slots and the paired
prompt corpus under out/champions-20261118, and pins every input hash in
manifest.json. Historical configs are git-blob snapshots of the commit that
adopted them, never today's config relabelled.

This module is allowed to touch only the artifact tree plus read-only stat /
sha256 / `git show` of frozen inputs. It performs NO network I/O, NO engine or
helper execution, NO process control and NO writes outside OUT.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path("/mnt/srcds/Strata")
HERE = Path(__file__).resolve().parent
DEFAULT_OUT = HERE / "out" / "champions-20261118"

# Canonical production stack — what every panel hands back to on restore. Read, hashed
# and recorded; never written by this module.
PROD_CONFIG = ROOT / "prod-config-v0238.json"
PROD_LAUNCHER = ROOT / "launch-v0238.sh"
CANONICAL_ENGINE = ROOT / "v0238p0/rpcw-hip/strata"
CANONICAL_ENGINE_SHA = "b0ec0109ebc50c982512c371d81b2c15a8f72c70edf8a461a4ff10337da52f02"
SUPERVISOR_STATE = "run/prod-swift15-f16kv-mixed-supervisor.json"
RESTORE_TIMEOUT_S = 1800
RESTORE_POLL_S = 5

# The two engine VRAM-guard reserves, in the order the report lists them. Their engine
# defaults are 700 for the first card and (absent -> the same as the first) for the later
# card: src/program/generate.cpp `int vram_reserve_mib = 700;` /
# `int vram_reserve_later_mib = -1;` at the pinned source 42cb41b314.
RESERVE_FLAGS = ("--vram-reserve-mib", "--vram-reserve-later-mib")

# Shared runtime identities. The helper is byte-identical across all three eras
# (journal 18br/18cr sha-verified); the serving wrapper is today's and shared.
HELPER = ROOT / "v0238p0/rpcw-cuda/strata-expert-helper-patient"
HELPER_SHA = "553f3bf48219c99298e4620547507eac7eefa29d660ee32e806bb9780a9ab361"
SERVER = ROOT / "v0215/serve/server.py"
SERVER_SHA = "b6463e7b99a0d74cb5bee783e41e401bb5728abccf8870fea13f5c44951c1182"

# stop-all.sh sweeps these literal patterns with pgrep -f, so no generated file
# name may contain them (a wrapper named launch-prod-*.sh would kill its own caller).
FORBIDDEN_NAME_PARTS = ("launch-prod-", "launch-trial-", "strata-expert-helper")

# Only a top-level VAR= assignment is retargeted, never a mention in a comment.
ASSIGNMENT = re.compile(r"^([A-Z_][A-Z0-9_]*)=")


@dataclass(frozen=True)
class Arm:
    """One frozen champion stack: era-exact binary, config blob and launcher blob."""

    name: str
    label: str
    request_date: str
    engine: Path
    engine_sha: str
    source_commit: str
    lineage: str
    config_ref: str  # git <commit>:<path> snapshot, adopted state
    config_blob: str
    launcher_ref: str
    launcher_blob: str
    notes: tuple[str, ...] = ()
    # Config args this profile moves away from the frozen snapshot, as (flag, value).
    # Applied by retarget_config and re-checked by verify: a slot whose setting never
    # reached the config the server execs is the defect class this guards.
    arg_overrides: tuple[tuple[str, str], ...] = ()


ARMS: tuple[Arm, ...] = (
    Arm(
        name="current",
        label="2026-11-18 approved RAM-policy candidate",
        request_date="2026-11-17",
        engine=Path("/mnt/srcds/Strata-wt-integ/build-mixed-gpu/rpcw-hip/strata"),
        engine_sha="8f19baaf150237e5d8085c12c842a4b5728d10bd00ce315ab0ed52ff9bd819ed",
        source_commit="af1e64a5cb4b617d52c0f9b416af76d28c42bc4f",
        lineage="conv-cache-ram-guarantee on 42cb41b314",
        config_ref="27728dc1ff:prod-config-v0238.json",
        config_blob="82dbd3adc5028791815d8f5f057e1319ec8ea548",
        launcher_ref="27728dc1ff:launch-v0238.sh",
        launcher_blob="34f45a8f5a3eea89996662973f1e62efe2d06ab2",
        notes=(
            "Production NOT deployed: boot from a comparison slot only, never the "
            "v0238p0 slot.",
            "RAM restore policy is live-acceptance pending: the boot log must show the "
            "headroom band and its named limiter; capture it, do not infer it.",
        ),
    ),
    Arm(
        name="nov16",
        label="2026-11-16 adopted v0238p0 champion",
        request_date="2026-11-16",
        engine=ROOT / "v0238p0/rpcw-hip/strata",
        engine_sha="b0ec0109ebc50c982512c371d81b2c15a8f72c70edf8a461a4ff10337da52f02",
        source_commit="42cb41b314",
        lineage="v0238p0",
        config_ref="d2e94b9b08:prod-config-v0238.json",
        config_blob="35117683a0ee9eb30819087dd61b230d48101680",
        launcher_ref="d2e94b9b08:launch-v0238.sh",
        launcher_blob="34f45a8f5a3eea89996662973f1e62efe2d06ab2",
        notes=(
            "Config is the AS-ADOPTED blob at the adoption commit, not the on-disk file: "
            "the on-disk copy was mutated by the 2026-11-17 conv-cache-dir move.",
        ),
    ),
    Arm(
        name="nov15",
        label="2026-11-15 adopted v0233 champion",
        request_date="2026-11-15",
        engine=ROOT / "v0233/build-mixed-gpu/rpcw-hip/strata",
        engine_sha="36ecbe983837828dd38a9f8d452cda519338369d7f42d8ad87b2830b494784b4",
        source_commit="81e023bfa0",
        lineage="v0233.2 (8e3ef28c) + 457a7e95d8 + 81e023bfa0",
        config_ref="fcd1b55325:prod-config-ud-iq3xxs-dflash-w4-q4_0-mixed.json",
        config_blob="1b6d2497acce8ccdb18455f68de9e3f81f55c00a",
        launcher_ref="fcd1b55325:launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh",
        launcher_blob="d46a0d13e4201f7c4e9bddef7a32542abf2b2e14",
        notes=(
            "journal lines 3544/3554 sha-verified LIVE at end of 2026-11-15; adoption "
            "18br 21:10 set STRATA_MMVF_ROWS.",
            "v0233/build-mixed-gpu/SHA256SUMS is stale (df415658); the frozen "
            "engine_sha above is the measured hash — do not overwrite the old slot manifest.",
            "Predates the #267 wedge fix: the watchdog may abort the boot or a request. "
            "Bounded deadlines only, no retries beyond one, no OS/GPU reset.",
        ),
    ),
)

# Frozen corpus filler vocabulary: identical to ab9_driver.WORDS so the paired
# corpus stays in the same distribution as every earlier ab9 leg.
WORDS = ("cache eviction policy ring buffer page table slot arena transfer prefill layer "
         "expert token stream socket reply frame claim row vector hidden tensor block copy "
         "engine helper window pinned queue lock begin finish prompt tier device stream "
         "kernel copy engine token expert layer prefill socket reply claim row hidden block").split()


@dataclass(frozen=True)
class Shape:
    """One frozen workload shape: exact prompt bytes, generation cap, pair count."""

    name: str
    ctx: int
    max_tokens: int
    warmup_pairs: int
    measured_pairs: int
    request_timeout_s: int

    @property
    def pairs(self) -> int:
        return self.warmup_pairs + self.measured_pairs


# Request-relative freeze, declared before any measurement. kind=tg issues the
# same prompt twice: request 0 is the fresh prefill (PP + TG), request 1 the
# cached re-run (TG only). pair 0 of every shape is warm-up and excluded.
SHAPES: tuple[Shape, ...] = (
    Shape("warm512", ctx=512, max_tokens=64, warmup_pairs=1, measured_pairs=0, request_timeout_s=600),
    Shape("short16k", ctx=88400, max_tokens=512, warmup_pairs=1, measured_pairs=3, request_timeout_s=2400),
    Shape("long80k", ctx=442400, max_tokens=1024, warmup_pairs=1, measured_pairs=3, request_timeout_s=5400),
)

# Arm order is fixed: today's champion first so a historical boot failure cannot
# cost the comparison that matters most; the pre-#267 arm last.
ARM_ORDER = ("current", "nov16", "nov15")

# What every panel's request model is, unless a profile says otherwise.
REQUEST_MODEL = ("one driver call per pair, ab9 kind=tg: request 0 fresh prefill "
                 "(PP + TG), request 1 cached re-run (TG only); pair 0 excluded")

CHAMPIONS_DEVIATIONS = (
    "conversation-cache-dir retargeted to a per-arm empty directory: the arms boot "
    "cold and never mutate the production cache inventory. Identical for all three arms.",
    "engine/helper/server sockets and the engine log are per-arm; the engine is "
    "reached through a symlink slot so no production slot is written.",
    "helper and serving server are today's byte-identical builds on all three arms "
    "(helper is era-invariant); only the HIP engine binary and the adopted config differ.",
    "the boot log destination is per-arm so a boot gate never globs a stale shared log.",
)


@dataclass(frozen=True)
class Profile:
    """One frozen measurement plan: which arms, which shapes, in what order, what varies.

    The 2026-11-18 champion comparison and the 2026-11-19 reserve comparison share one
    preparer. A Profile carries the difference so neither has to copy the other, and so
    the manifest states what actually varies instead of assuming three engine binaries.
    """

    key: str
    plan: str
    arms: tuple[Arm, ...]
    shapes: tuple[Shape, ...]
    arm_order: tuple[str, ...]
    deviations: tuple[str, ...] = CHAMPIONS_DEVIATIONS
    request_model: str = REQUEST_MODEL
    # The lever under test, recorded so the report can name it: flag -> {arm: value}.
    profile_delta: dict = field(default_factory=dict)
    # Optional request body pins for the driver. "messages" and "max_tokens" belong to
    # the driver (the corpus and the shape own them); a profile pins the sampling and
    # the reasoning effort so a speed leg does not inherit the driver's parity defaults.
    request_template: dict = field(default_factory=dict)


CHAMPIONS = Profile(
    key="champions-3stack",
    plan="Three-stack TG/PP champion comparison, requested 2026-11-17",
    arms=ARMS,
    shapes=SHAPES,
    arm_order=ARM_ORDER,
)


def sha256_path(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def build_paired_prompt(work: str, pair: int, ctx: int) -> bytes:
    """Deterministic paired-corpus prompt of EXACTLY ctx bytes, identical for every arm.

    The unique identity tag sits at the HEAD of the prompt. That matters: the old
    ab9 tag was a tail line, so the leading tokens still matched and a "fresh" leg
    could be served from the conversation cache. A head tag diverges at token zero,
    which is what actually forces a fresh prefill. No per-arm text appears anywhere,
    so every arm reads byte-identical input.
    """
    tag = f"[champs work={work} pair={pair} ctx={ctx}]\n"
    rng = random.Random(1729)  # same seed and draw order as ab9_driver.build_prompt
    parts = [tag]
    total = len(tag.encode())
    while total < ctx:
        line = " ".join(rng.choice(WORDS) for _ in range(9)) + "\n"
        parts.append(line)
        total += len(line.encode())
    blob = "".join(parts).encode()[:ctx]  # the corpus is pure ASCII by construction
    text = blob.decode()
    text += "." * (ctx - len(text.encode()))  # dots tokenize as ordinary text
    prompt = text.encode()
    assert len(prompt) == ctx, (work, pair, ctx, len(prompt))
    return prompt


def corpus_files(out: Path, shapes: tuple[Shape, ...] | None = None) -> list[tuple[Shape, int, Path]]:
    shapes = SHAPES if shapes is None else shapes
    return [(shape, pair, out / "corpus" / f"{shape.name}-p{pair}.ctx{shape.ctx}.bin")
            for shape in shapes for pair in range(shape.pairs)]


def write_corpus(out: Path, shapes: tuple[Shape, ...] | None = None) -> list[dict]:
    (out / "corpus").mkdir(parents=True, exist_ok=True)
    entries = []
    for shape, pair, path in corpus_files(out, shapes):
        prompt = build_paired_prompt(shape.name, pair, shape.ctx)
        path.write_bytes(prompt)
        entries.append({
            "shape": shape.name, "pair": pair, "measured": pair >= shape.warmup_pairs,
            "path": str(path), "bytes": len(prompt), "sha256": sha256_bytes(prompt),
            "head_tag": prompt.split(b"\n", 1)[0].decode(),
        })
    return entries


def git_blob(ref: str) -> bytes:
    """Literal blob bytes for <commit>:<path>. `git show` is what the historical
    snapshots were reviewed with, so the same command proves the pin."""
    return subprocess.run(["git", "-C", str(ROOT), "show", ref],
                          check=True, capture_output=True).stdout


def git_blob_id(blob: bytes) -> str:
    return subprocess.run(["git", "-C", str(ROOT), "hash-object", "--stdin"],
                          input=blob, check=True, capture_output=True).stdout.decode().strip()


def arg_value(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


def retarget_config(raw: bytes, arm: Arm, out: Path) -> tuple[bytes, list[dict]]:
    """Rewrite only the harness-owned paths (plus the profile's declared flags) of a
    frozen config; record every delta, tagged with who owns it."""
    cfg = json.loads(raw)
    deltas: list[dict] = []

    def retarget(key: str, value: str) -> None:
        old = cfg.get(key)
        if old != value:
            deltas.append({"field": key, "from": old, "to": value, "kind": "harness"})
        cfg[key] = value

    # exe is what selects the engine; the launcher's BIN alone does NOT (a known
    # class of defect: a slot change that never reaches the config the server execs).
    retarget("exe", str(out / "slots" / arm.name / "rpcw-hip" / "strata"))
    retarget("log", str(out / "logs" / f"{arm.name}.engine.log"))
    retarget("expert_helper_socket", str(out / "run" / f"{arm.name}.sock"))
    retarget("expert_helper_socket2", str(out / "run" / f"{arm.name}2.sock"))

    args = cfg["args"]
    cache_dir = str(out / "cache" / arm.name)
    idx = args.index("--conversation-cache-dir")
    if args[idx + 1] != cache_dir:
        deltas.append({"field": "args/--conversation-cache-dir",
                       "from": args[idx + 1], "to": cache_dir, "kind": "harness"})
    args[idx + 1] = cache_dir

    # A profile's lever: only flags the frozen config already carries, and only the ones
    # the arm declares. Inserting a flag would silently change argument order; an
    # override that never lands in this file is invisible to the running engine.
    for flag, value in arm.arg_overrides:
        if flag not in args:
            raise SystemExit(f"{arm.name}: profile override {flag} is not an argument of "
                             f"{arm.config_ref}; refusing to invent it")
        position = args.index(flag)
        if args[position + 1] != value:
            deltas.append({"field": f"args/{flag}", "from": args[position + 1],
                           "to": value, "kind": "profile"})
        args[position + 1] = value

    # Every other key stays exactly as adopted: quantization, precision, context
    # capacity, cache budgets, GPU pinning and the whole env block are frozen.
    if quality_keys(cfg) != quality_keys(json.loads(raw)):
        raise SystemExit(f"{arm.name}: internal error, a quality-bearing key changed")
    return (json.dumps(cfg, indent=1, sort_keys=True) + "\n").encode(), deltas


def quality_keys(cfg: dict) -> dict:
    """The keys a speed claim must never move: precision, capacity, model identity."""
    args = cfg["args"]
    watched = ("--max-context", "--kv", "--prefill", "--kv-resident", "--spec",
               "--conversation-cache-mib", "--conversation-cache-ram-mib",
               "--conversation-cache-disk-mib", "--expert-helper-slots")
    return {
        "backend": cfg.get("backend"),
        "gpu": cfg.get("gpu"),
        "model_name": cfg.get("model_name"),
        "pack": arg_value(args, "--pack"),
        "native": arg_value(args, "--native"),
        "ple": arg_value(args, "--ple-gguf"),
        "dflash": arg_value(args, "--dflash"),
        "env": cfg.get("env"),
        **{flag: arg_value(args, flag) for flag in watched if flag in args},
    }


# The config keys the harness re-points per arm. Everything else is the frozen setting.
HARNESS_CONFIG_FIELDS = ("exe", "log", "expert_helper_socket", "expert_helper_socket2")
PER_ARM_PLACEHOLDER = "<per-arm>"


def settings_only(cfg: dict, cache_flag: str = "--conversation-cache-dir") -> dict:
    """The config with every harness-owned path blanked, so two arms compare on settings.

    The args list keeps its positions: a diff is then positional and cannot hide a
    reordered argument list behind a set comparison.
    """
    blanked = {key: (PER_ARM_PLACEHOLDER if key in HARNESS_CONFIG_FIELDS else value)
               for key, value in cfg.items()}
    args = list(cfg["args"])
    if cache_flag in args:
        args[args.index(cache_flag) + 1] = PER_ARM_PLACEHOLDER
    blanked["args"] = args
    return blanked


def cross_arm_setting_diffs(by_arm: dict[str, dict]) -> dict[str, dict]:
    """Fields whose value is not shared by every arm, after the harness paths are blanked.

    This is the fail-closed check a "only the lever moved" claim rests on: any setting
    outside the declared lever shows up here, and a dry-run must refuse to run on it.
    """
    names = sorted(by_arm)
    diffs: dict[str, dict] = {}
    for key in sorted({k for cfg in by_arm.values() for k in cfg} - {"args"}):
        values = {name: by_arm[name].get(key) for name in names}
        if len({json.dumps(value, sort_keys=True) for value in values.values()}) > 1:
            diffs[key] = values
    reference = by_arm[names[0]]["args"]
    for index in range(max(len(cfg["args"]) for cfg in by_arm.values())):
        values = {name: (cfg["args"][index] if index < len(cfg["args"]) else None)
                  for name, cfg in by_arm.items()}
        if len({json.dumps(value, sort_keys=True) for value in values.values()}) > 1:
            flag = next((token for token in reversed(reference[:index + 1])
                         if token.startswith("--")), f"position-{index}")
            diffs[f"args/{flag}[{index}]"] = values
    return diffs


def retarget_launcher(raw: bytes, arm: Arm, out: Path) -> tuple[bytes, list[dict]]:
    """Rewrite only BIN/CONFIG/SOCKET/SOCKET2/LOG in the era's own launcher text.

    Flags, helper args, env, the supervisor --state/--lock pair (stop-all finds the
    pair through those canonical paths) and every era-specific override are preserved
    verbatim, including the launcher's own comments.
    """
    slot = out / "slots" / arm.name
    wanted = {
        "BIN": f'"{slot}"',
        "CONFIG": f'"{out / "configs" / f"{arm.name}.json"}"',
        "SOCKET": f'"{out / "run" / f"{arm.name}.sock"}"',
        "SOCKET2": f'"{out / "run" / f"{arm.name}2.sock"}"',
        "LOG": f'"{out / "logs" / f"{arm.name}-boot"}-$(date +%Y%m%d-%H%M%S)-$$.log"',
    }
    lines = raw.decode().splitlines(keepends=True)
    seen: dict[str, int] = {}
    deltas: list[dict] = []
    for i, line in enumerate(lines):
        match = ASSIGNMENT.match(line)
        var = match and match.group(1)
        if var not in wanted:
            continue
        seen[var] = seen.get(var, 0) + 1
        lines[i] = f"{var}={wanted[var]}\n"
        deltas.append({"field": f"launcher/{var}", "from": line.rstrip("\n"),
                       "to": lines[i].rstrip("\n"), "kind": "harness"})
    missing = sorted(k for k, n in seen.items() if n != 1)
    if missing:
        raise SystemExit(f"launcher {arm.launcher_ref}: assignment not unique: {missing}")
    for var in wanted:
        if var not in seen:
            raise SystemExit(f"launcher {arm.launcher_ref}: missing assignment {var}=")
    out_text = "".join(lines)
    for part in FORBIDDEN_NAME_PARTS:
        if part in out_text and part not in raw.decode():
            raise SystemExit(f"generated launcher would introduce sweep pattern {part!r}")
    return out_text.encode(), deltas


def write_slot(out: Path, arm: Arm) -> dict:
    """Comparison slot of symlinks to the era-exact binaries. Never copies 48 MB and
    never writes inside v0238p0 / v0233 / the integration worktree."""
    slot = out / "slots" / arm.name
    links = {
        slot / "rpcw-hip/strata": arm.engine,
        slot / "rpcw-cuda/strata-expert-helper-patient": HELPER,
    }
    for link, target in links.items():
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink() or link.exists():
            if link.is_symlink() and Path(os.readlink(link)) == target:
                continue
            link.unlink()
        link.symlink_to(target)
    return {"slot": str(slot),
            "links": {str(k): str(v) for k, v in links.items()}}


def write_request_template(out: Path, template: dict) -> dict:
    """Freeze a profile's request-body pins as the file the driver is pointed at.

    The driver injects messages and max_tokens; a template that carried them would make
    the corpus and the shape ambiguous, so it is refused here rather than at request time.
    """
    if not template:
        return {}
    owned = sorted({"messages", "max_tokens"} & set(template))
    if owned:
        raise SystemExit(f"request template must not pin {owned}: the corpus and the "
                         "shape own them")
    path = out / "request-template.json"
    path.write_text(json.dumps(template, indent=1, sort_keys=True) + "\n")
    return {"path": str(path), "sha256": sha256_path(path), "settings": template}


def restore_pin(canonical_engine_sha: str) -> dict:
    """What a panel hands production back to, pinned at prepare time.

    Recorded so the runner can pass --out, --engine-sha, --timeout and --poll explicitly
    instead of letting a restore write its evidence into a frozen measurement tree.
    """
    if canonical_engine_sha != CANONICAL_ENGINE_SHA:
        raise SystemExit(f"canonical engine {CANONICAL_ENGINE} sha {canonical_engine_sha} "
                         f"!= pinned {CANONICAL_ENGINE_SHA}")
    return {
        "launcher": str(PROD_LAUNCHER),
        "launcher_sha256": sha256_path(PROD_LAUNCHER),
        "config": str(PROD_CONFIG),
        "config_sha256": sha256_path(PROD_CONFIG),
        "engine": str(CANONICAL_ENGINE),
        "engine_sha": canonical_engine_sha,
        "supervisor_state": str(ROOT / SUPERVISOR_STATE),
        "timeout_s": RESTORE_TIMEOUT_S,
        "poll_s": RESTORE_POLL_S,
    }


def prepare(out: Path, profile: Profile = CHAMPIONS) -> dict:
    (out / "configs").mkdir(parents=True, exist_ok=True)
    (out / "bin").mkdir(parents=True, exist_ok=True)
    (out / "logs").mkdir(parents=True, exist_ok=True)
    (out / "run").mkdir(parents=True, exist_ok=True)
    (out / "cache").mkdir(parents=True, exist_ok=True)

    corpus = write_corpus(out, profile.shapes)
    request_template = write_request_template(out, profile.request_template)
    arms = {}
    for arm in profile.arms:
        cfg_raw = git_blob(arm.config_ref)
        blob = git_blob_id(cfg_raw)
        if blob != arm.config_blob:
            raise SystemExit(f"{arm.name}: config blob {blob} != frozen {arm.config_blob}")
        cfg_bytes, cfg_deltas = retarget_config(cfg_raw, arm, out)
        cfg_path = out / "configs" / f"{arm.name}.json"
        cfg_path.write_bytes(cfg_bytes)

        lau_raw = git_blob(arm.launcher_ref)
        lblob = git_blob_id(lau_raw)
        if lblob != arm.launcher_blob:
            raise SystemExit(f"{arm.name}: launcher blob {lblob} != frozen {arm.launcher_blob}")
        lau_bytes, lau_deltas = retarget_launcher(lau_raw, arm, out)
        lau_path = out / "bin" / f"champs-{arm.name}.sh"
        if any(part in lau_path.name for part in FORBIDDEN_NAME_PARTS):
            raise SystemExit(f"generated launcher name matches stop-all sweep: {lau_path.name}")
        lau_path.write_bytes(lau_bytes)
        lau_path.chmod(0o755)

        engine_sha = sha256_path(arm.engine)
        if engine_sha != arm.engine_sha:
            raise SystemExit(f"{arm.name}: engine {arm.engine} sha {engine_sha} != frozen {arm.engine_sha}")

        stat_ = arm.engine.stat()
        arms[arm.name] = {
            "label": arm.label,
            "request_date": arm.request_date,
            "source_commit": arm.source_commit,
            "lineage": arm.lineage,
            "engine": {"path": str(arm.engine), "sha256": engine_sha,
                       "bytes": stat_.st_size, "mtime_epoch": int(stat_.st_mtime)},
            "helper": {"path": str(HELPER), "sha256": sha256_path(HELPER)},
            "server": {"path": str(SERVER), "sha256": sha256_path(SERVER)},
            "config_snapshot": {"git_ref": arm.config_ref, "blob": arm.config_blob,
                                "sha256_literal": sha256_bytes(cfg_raw)},
            "launcher_snapshot": {"git_ref": arm.launcher_ref, "blob": arm.launcher_blob,
                                  "sha256_literal": sha256_bytes(lau_raw)},
            "frozen_config": {"path": str(cfg_path), "sha256": sha256_bytes(cfg_bytes)},
            "quality_identity": quality_keys(json.loads(cfg_raw)),
            "frozen_launcher": {"path": str(lau_path), "sha256": sha256_bytes(lau_bytes)},
            "sockets": [str(out / "run" / f"{arm.name}.sock"), str(out / "run" / f"{arm.name}2.sock")],
            "conversation_cache_dir": str(out / "cache" / arm.name),
            "engine_log": str(out / "logs" / f"{arm.name}.engine.log"),
            "boot_log_dir": str(out / "logs"),
            "config_deltas": cfg_deltas,
            "launcher_deltas": lau_deltas,
            "profile_arg_overrides": dict(arm.arg_overrides),
            "notes": list(arm.notes),
            **write_slot(out, arm),
        }
        if arms[arm.name]["helper"]["sha256"] != HELPER_SHA:
            raise SystemExit(f"{arm.name}: helper sha drifted from the frozen identity")
        if arms[arm.name]["server"]["sha256"] != SERVER_SHA:
            raise SystemExit(f"{arm.name}: server sha drifted from the frozen identity")

    order = list(profile.arm_order)
    quality_deltas = cross_arm_quality_diffs(arms)
    engine_shas = {name: arms[name]["engine"]["sha256"] for name in order}
    manifest = {
        "profile": profile.key,
        "plan": profile.plan,
        "plan_document": str(HERE / "gpu-rounds" / "serving-plan-current.md"),
        "git_head_at_prepare": subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"],
                                              check=True, capture_output=True).stdout.decode().strip(),
        "arm_order": order,
        "shapes": [{"name": s.name, "ctx_bytes": s.ctx, "max_tokens": s.max_tokens,
                    "warmup_pairs": s.warmup_pairs, "measured_pairs": s.measured_pairs,
                    "pairs": s.pairs, "request_timeout_s": s.request_timeout_s}
                   for s in profile.shapes],
        "request_model": profile.request_model,
        "request_template": request_template,
        "corpus": corpus,
        "arms": arms,
        "quality_key_deltas": quality_deltas,
        # Measured fact, not an assumption: whatever is reported here is what the panel
        # actually isolated, and nothing else is claimed to be equal.
        "config_identical_across_arms": not quality_deltas,
        "profile_delta": profile.profile_delta,
        "restore": restore_pin(sha256_path(CANONICAL_ENGINE)),
        "effective_deltas": {
            "engine_binary": engine_shas,
            "engine_binary_identical": len(set(engine_shas.values())) == 1,
            "engine_source": {name: arms[name]["source_commit"] for name in order},
            "shared_unchanged": {
                "helper": HELPER_SHA, "server": SERVER_SHA,
                "quality_bearing_config": (
                    f"identical across all {len(order)} arms" if not quality_deltas
                    else "differs: " + ", ".join(sorted(quality_deltas))),
            },
        },
        "deviations": list(profile.deviations),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    (out / "manifest.sha256").write_text(
        f"{sha256_path(out / 'manifest.json')}  manifest.json\n")
    return manifest


def verify(out: Path) -> list[str]:
    """Re-derive every pinned hash from disk. Returns problem strings (empty = clean)."""
    problems: list[str] = []
    manifest_path = out / "manifest.json"
    if not manifest_path.is_file():
        return [f"missing {manifest_path}: run --prepare first"]
    manifest = json.loads(manifest_path.read_text())
    pinned = (out / "manifest.sha256").read_text().split()[0] if (out / "manifest.sha256").is_file() else ""
    if pinned and pinned != sha256_path(manifest_path):
        problems.append("manifest.sha256 does not match manifest.json")

    for entry in manifest["corpus"]:
        path = Path(entry["path"])
        if not path.is_file():
            problems.append(f"corpus missing: {path}")
            continue
        blob = path.read_bytes()
        if len(blob) != entry["bytes"]:
            problems.append(f"corpus length drift {path}: {len(blob)} != {entry['bytes']}")
        if sha256_bytes(blob) != entry["sha256"]:
            problems.append(f"corpus sha drift {path}")
        expected = build_paired_prompt(entry["shape"], entry["pair"], entry["bytes"])
        if blob != expected:
            problems.append(f"corpus not reproducible from seed: {path}")
    tags = {e["path"]: e["head_tag"] for e in manifest["corpus"]}
    if len(set(tags.values())) != len(tags):
        problems.append("corpus head tags are not unique per pair")

    for name, arm in manifest["arms"].items():
        for kind in ("frozen_config", "frozen_launcher"):
            path = Path(arm[kind]["path"])
            if not path.is_file():
                problems.append(f"{name}: missing {kind} {path}")
                continue
            if sha256_path(path) != arm[kind]["sha256"]:
                problems.append(f"{name}: {kind} sha drift {path}")
        if any(part in Path(arm["frozen_launcher"]["path"]).name for part in FORBIDDEN_NAME_PARTS):
            problems.append(f"{name}: frozen launcher name matches the stop-all sweep")
        engine = Path(arm["engine"]["path"])
        if not engine.is_file():
            problems.append(f"{name}: engine missing {engine}")
        elif sha256_path(engine) != arm["engine"]["sha256"]:
            problems.append(f"{name}: engine sha drift {engine}")
        elif (s := engine.stat()).st_size != arm["engine"]["bytes"]:
            problems.append(f"{name}: engine size drift {engine} {s.st_size}")
        for kind, frozen in (("helper", HELPER_SHA), ("server", SERVER_SHA)):
            path = Path(arm[kind]["path"])
            if not path.is_file() or sha256_path(path) != frozen:
                problems.append(f"{name}: {kind} identity drifted ({path})")
        for link, target in arm["links"].items():
            link_path = Path(link)
            if not link_path.is_symlink() or os.readlink(link_path) != target:
                problems.append(f"{name}: slot link wrong {link} -> {target}")
        cfg = json.loads(Path(arm["frozen_config"]["path"]).read_text())
        if cfg["exe"] != str(out / "slots" / name / "rpcw-hip" / "strata"):
            problems.append(f"{name}: config exe not retargeted ({cfg['exe']})")
        if cfg["log"] != arm["engine_log"]:
            problems.append(f"{name}: config log not retargeted ({cfg['log']})")
        if cfg["expert_helper_socket"] not in arm["sockets"] or \
           cfg["expert_helper_socket2"] not in arm["sockets"]:
            problems.append(f"{name}: config helper sockets not retargeted")
        if arg_value(cfg["args"], "--conversation-cache-dir") != arm["conversation_cache_dir"]:
            problems.append(f"{name}: conversation-cache-dir not retargeted")
        # The lever must be IN the config the server execs: an override that never
        # reaches this file changes nothing and would silently compare two identical runs.
        for flag, value in (arm.get("profile_arg_overrides") or {}).items():
            try:
                actual = arg_value(cfg["args"], flag)
            except (ValueError, IndexError):
                actual = "<absent>"
            if actual != value:
                problems.append(f"{name}: profile lever {flag} is {actual!r}, not {value!r}")
        if quality_keys(cfg) != arm["quality_identity"]:
            problems.append(f"{name}: a quality-bearing key differs from the frozen snapshot")
        for sock in arm["sockets"]:
            p = Path(sock)
            if p.exists() or p.is_symlink():
                problems.append(f"{name}: socket path already present, stale boot? {sock}")
    return problems


def cross_arm_quality_diffs(arms: dict) -> dict:
    """Quality-bearing keys whose value is NOT shared by all three arms. Part of the
    stack comparison: report them, do not attribute the whole delta to one code change."""
    keys = sorted({k for arm in arms.values() for k in arm["quality_identity"]})
    return {k: {name: arms[name]["quality_identity"].get(k) for name in arms}
            for k in keys
            if len({json.dumps(arm["quality_identity"].get(k), sort_keys=True)
                    for arm in arms.values()}) > 1}


def plan_text(out: Path) -> str:
    """Print what the frozen manifest actually varies.

    Every line comes from the manifest, so a profile that holds the engine fixed cannot
    inherit the champion plan's wording about three engine binaries, and a manifest
    written before the newer keys appeared still prints.
    """
    manifest = json.loads((out / "manifest.json").read_text())
    order = list(manifest.get("arm_order") or ARM_ORDER)
    arms = manifest["arms"]
    engine_shas = {name: arms[name]["engine"]["sha256"] for name in order}
    same_engine = len(set(engine_shas.values())) == 1
    lines = [f"panel plan (profile {manifest.get('profile', 'champions-3stack')}, out={out})",
             f"  arms in order: {' -> '.join(order)}",
             "  per shape: pair 0 warm-up (excluded), then measured pairs; "
             "each pair = fresh leg + cached leg"]
    for shape in manifest["shapes"]:
        lines.append(f"    {shape['name']:<9} ctx={shape['ctx_bytes']:<7} "
                     f"max_tokens={shape['max_tokens']:<5} pairs={shape['pairs']} "
                     f"(measured {shape['measured_pairs']}) timeout={shape['request_timeout_s']}s")
    legs = sum(s["pairs"] for s in manifest["shapes"])
    lines.append(f"  driver calls per arm: {legs}; requests per arm: {legs * 2}; "
                 f"arms: {len(arms)}")
    template = manifest.get("request_template") or {}
    if template:
        lines.append(f"  request template: {template['path']} "
                     f"sha256 {template['sha256'][:16]}")
        lines.append("    pins " + ", ".join(f"{key}={value}"
                                             for key, value in sorted(template["settings"].items())))
        lines.append("    the driver adds messages + max_tokens only; every other field is the template's")
    for name in order:
        arm = arms[name]
        lines.append(f"  {name}: engine {arm['engine']['path']}")
        lines.append(f"    sha256 {arm['engine']['sha256']}  source {arm['source_commit']}")
        lines.append(f"    config snapshot {arm['config_snapshot']['git_ref']} "
                     f"blob {arm['config_snapshot']['blob']}")
        lines.append(f"    launcher snapshot {arm['launcher_snapshot']['git_ref']} "
                     f"blob {arm['launcher_snapshot']['blob']}")
        lines.append(f"    frozen launcher {arm['frozen_launcher']['path']}")
    lines.append("  what actually differs between the arms:")
    if same_engine:
        sources = {arms[name]["source_commit"] for name in order}
        lines.append(f"    engine binary: identical in all {len(order)} arms "
                     f"({next(iter(engine_shas.values()))[:16]}"
                     + (f", source {next(iter(sources))}" if len(sources) == 1 else "") + ")")
    else:
        for name in order:
            lines.append(f"    engine {name}: {engine_shas[name][:16]} "
                         f"source {arms[name]['source_commit']}")
    if manifest.get("config_identical_across_arms", True):
        lines.append("    config: identical in every quality-bearing key (precision, capacity,"
                     " cache budgets, env, model paths) apart from the harness paths")
    else:
        for key, by_arm in (manifest.get("quality_key_deltas") or {}).items():
            lines.append(f"    config {key}: " +
                         " | ".join(f"{name}={by_arm[name]}" for name in order))
    for flag, by_arm in (manifest.get("profile_delta") or {}).items():
        lines.append("    LEVER " + str(flag) + ": " +
                     " | ".join(f"{name}={by_arm[name]}" for name in order if name in by_arm))
    shared = manifest["effective_deltas"]["shared_unchanged"]
    lines.append(f"    shared: helper {shared['helper'][:16]} server {shared['server'][:16]} "
                 f"({shared['quality_bearing_config']})")
    restore = manifest.get("restore") or {}
    lines.append(f"  restore target: {restore.get('launcher', ROOT / 'launch-v0238.sh')} with "
                 f"{restore.get('config', ROOT / 'prod-config-v0238.json')}"
                 + (f" (config sha {restore['config_sha256'][:16]}, engine sha "
                    f"{restore['engine_sha'][:16]})" if restore else ""))
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="CPU-only preparation for the 2026-11-18 three-champion TG/PP comparison")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--prepare", action="store_true", help="write frozen artifacts (default)")
    mode.add_argument("--verify", action="store_true", help="re-check every pinned hash")
    mode.add_argument("--plan", action="store_true", help="print the frozen plan")
    a = ap.parse_args()
    if a.verify:
        problems = verify(a.out)
        for p in problems:
            print(f"PROBLEM: {p}")
        print(f"verify: {'OK' if not problems else f'{len(problems)} problem(s)'}")
        return 0 if not problems else 1
    if a.plan:
        print(plan_text(a.out))
        return 0
    manifest = prepare(a.out)
    print(f"prepared {a.out} for arms {', '.join(manifest['arm_order'])}")
    for name in manifest["arm_order"]:
        arm = manifest["arms"][name]
        print(f"  {name}: engine {arm['engine']['sha256'][:12]} "
              f"config-deltas {len(arm['config_deltas'])} launcher-deltas {len(arm['launcher_deltas'])}")
    problems = verify(a.out)
    for p in problems:
        print(f"PROBLEM: {p}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
