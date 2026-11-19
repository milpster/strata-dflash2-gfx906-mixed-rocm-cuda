#!/usr/bin/env python3
"""Offline (CPU-only) RAM-acceptance foundations: stdlib, import-safe, no launch code.

This module validates DECLARED data for the bounded conversation-parking acceptance
window. It does NOT start an engine, helper or server, open a socket, import
`serve.server`/`StrataEngine`, read a production cache, or touch GPU sysfs/procfs.
Importing it performs no side effects; the sibling readers it reuses
(`champions_log.parse_window`, `champions_prepare.sha256_*`,
`watch_gpu_residency.read_fdinfo_clients`) are themselves import-safe.

Scope it provides (pure functions over caller-supplied inputs):

  1. matched per-arm TEST configs + a compact diff/hash provenance manifest;
  2. actual-source log predicates: per-operation refusal totals (reason-independent),
     PP/TG reused/read split, turn classes, engagement, raw token/state comparisons;
  3. exact, separately-frozen restore expectations (file identity + target depth +
     reused/read/draft-class), never derived from the row it scores;
  4. a guard that reuses the existing DRM `read_fdinfo_clients` reader for eviction.

It is OFFLINE PREPARATION, not a runnable GPU gate. The concrete runtime gates the
parent still owns are listed in OUTSTANDING_GATES and reported alongside every
verdict so nothing here can be mistaken for a pass.
"""
# pyright: reportMissingImports=false
from __future__ import annotations

import copy
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# Reuse the real primitives rather than re-deriving them. Sibling-module imports need
# the rpcprof root (champions_log, champions_prepare) and gpu-rounds (watch_gpu_residency).
_HERE = Path(__file__).resolve().parent
for _sibling in (str(_HERE), str(_HERE.parent)):
    if _sibling not in sys.path:
        sys.path.insert(0, _sibling)

from champions_log import parse_window  # noqa: E402  actual reused/read PP/TG parsing
from champions_prepare import sha256_path  # noqa: E402  hash-only, no champion assumptions
from watch_gpu_residency import read_fdinfo_clients  # noqa: E402  existing DRM eviction/residency reader

# --- immutable invariants -------------------------------------------------------

FLOOR_MIB = 2560  # the RAM floor is never a mutable knob, including in test configs.
STATE_HASH_ENV = ("STRATA_STATE_HASH", "STRATA_STATE_HASH_GDN")  # diagnostics; arms must match
ALLOWED_ENV_KEYS = frozenset(STATE_HASH_ENV)

OFFLINE_PREPARATION = True
OUTSTANDING_GATES = (
    "raw-transport bridge (HTTP text/state-hash/log-token vs raw StrataEngine token IDs) is a parent decision",
    "finite calibrated startup-only cap ladder + physical MemAvailable/swap bounds are parent-owned",
    "exact GPU deepest-PUBLISHED cold-restart and same-boot RAM checkpoint-selection need concrete GPU fixtures",
    "GPU acceptance remains paused; parent alone runs any explicit calibration/test window between agent turns",
)

# Verdict vocabulary used across the predicate helpers.
PASS, FAIL, UNENGAGED, BLOCKED, ABORT, UNKNOWN = (
    "PASS", "FAIL", "UNENGAGED", "BLOCKED", "ABORT", "UNKNOWN")

# A test config's paths must be isolated under the run directory; the production
# cache/socket roots are named so a retarget that forgot to move them is rejected.
PRODUCTION_PATH_MARKERS = ("/mnt/srcds/Strata/run/", "/home/srcds/strata-conv-cache")


class ConfigRejected(ValueError):
    """A config/provenance rule was violated; raised rather than returning a soft flag."""


# =============================================================================
# 1. CONFIG + PROVENANCE
# =============================================================================

def _flag_value(args: list[str], flag: str) -> str | None:
    """Tolerant read of a value-taking flag from an args list (None when absent)."""
    return args[args.index(flag) + 1] if flag in args else None


def _flag_count(args: list[str], flag: str) -> int:
    return args.count(flag)


def min_free_floor_mib(cfg: dict) -> int:
    """The effective RAM floor from a config.

    Absent the flag, the engine default (2560 MiB) applies; present, it must equal
    2560. The floor is never a knob, so any other value is rejected.
    """
    raw = _flag_value(cfg.get("args", []), "--conversation-cache-min-free-mib")
    if raw is None:
        return FLOOR_MIB
    if not raw.isdigit():
        raise ConfigRejected(f"--conversation-cache-min-free-mib must be integer MiB, got {raw!r}")
    return int(raw)


def _require_isolated(path: str, out_dir: Path, role: str) -> str:
    """Reject empty or production targets; an isolated test path must sit under out_dir."""
    if not path:
        raise ConfigRejected(f"{role}: isolated test path required")
    resolved = Path(path).resolve()
    base = out_dir.resolve()
    if base not in resolved.parents and resolved != base:
        raise ConfigRejected(f"{role}: {path} is not an isolated path under {out_dir}")
    for marker in PRODUCTION_PATH_MARKERS:
        if marker in str(resolved):
            raise ConfigRejected(f"{role}: {path} targets a production location")
    return str(resolved)


def build_test_config(base_cfg: dict, *, arm: str, out_dir: Path, exe: str,
                      socket1: str, socket2: str, cache_dir: str, engine_log: str,
                      port: int | None = None, memory_limit_mib: int | None = None,
                      env: dict | None = None) -> tuple[dict, list[dict]]:
    """Derive one arm's TEST config from the actual canonical schema, mutating nothing.

    Only the allowlisted retarget deltas are applied: exe identity, isolated cache
    directory, two distinct helper sockets, isolated engine log, optional test port,
    and an optional explicit memory-limit cap. Every other field (precision,
    quantization, context/KV capacity, spec/DFlash, sampling, cache budgets, the whole
    env block) is carried through unchanged. The input config is deep-copied; the
    caller's object is never modified. Caps are accepted purely as INPUT for a later
    calibration freeze — this helper never invents one.
    """
    if min_free_floor_mib(base_cfg) != FLOOR_MIB:
        raise ConfigRejected("the base config's RAM floor is not the immutable 2560 MiB")
    cfg = copy.deepcopy(base_cfg)
    args = cfg["args"]
    deltas: list[dict] = []

    def set_field(key: str, value) -> None:
        if cfg.get(key) != value:
            deltas.append({"field": key, "from": cfg.get(key), "to": value})
        cfg[key] = value

    if not str(exe).strip():
        raise ConfigRejected("exe identity required")
    set_field("exe", str(exe))

    if not str(arm).strip():
        raise ConfigRejected("arm name required")

    # Two DISTINCT isolated helper sockets; equal sockets would collide on one endpoint.
    sock1 = _require_isolated(socket1, out_dir, f"{arm}.expert_helper_socket")
    sock2 = _require_isolated(socket2, out_dir, f"{arm}.expert_helper_socket2")
    if sock1 == sock2:
        raise ConfigRejected(f"{arm}: helper sockets must be two distinct endpoints")
    set_field("expert_helper_socket", sock1)
    set_field("expert_helper_socket2", sock2)

    # Isolated engine log + optional test port descriptor.
    set_field("log", _require_isolated(engine_log, out_dir, f"{arm}.log"))
    if port is not None:
        if not isinstance(port, int) or isinstance(port, bool) or not (1024 <= port <= 65535):
            raise ConfigRejected(f"{arm}: invalid test port {port!r}")
        set_field("port", port)

    # Isolated conversation-cache directory (never the production cache inventory).
    idx = args.index("--conversation-cache-dir") if "--conversation-cache-dir" in args else -1
    new_cache = _require_isolated(cache_dir, out_dir, f"{arm}.conversation-cache-dir")
    if idx < 0:
        raise ConfigRejected(f"{arm}: base config has no --conversation-cache-dir to isolate")
    if args[idx + 1] != new_cache:
        deltas.append({"field": "args/--conversation-cache-dir",
                       "from": args[idx + 1], "to": new_cache})
        args[idx + 1] = new_cache

    # Optional explicit memory-limit cap (startup-only in the engine; accepted here as
    # frozen-later calibration INPUT, never derived from rounded logs).
    if memory_limit_mib is not None:
        if not isinstance(memory_limit_mib, int) or isinstance(memory_limit_mib, bool) or memory_limit_mib <= 0:
            raise ConfigRejected(f"{arm}: memory-limit cap must be a positive integer MiB")
        if _flag_count(args, "--memory-limit-mib") > 0:
            raise ConfigRejected(f"{arm}: duplicate --memory-limit-mib already present in base args")
        args += ["--memory-limit-mib", str(memory_limit_mib)]
        deltas.append({"field": "args/--memory-limit-mib", "from": None, "to": memory_limit_mib})

    # Duplicate relevant flags would silently win by last-read; reject them.
    for dup in ("--conversation-cache-dir", "--memory-limit-mib",
                "--conversation-cache-min-free-mib"):
        if _flag_count(args, dup) > 1:
            raise ConfigRejected(f"{arm}: duplicate conflicting {dup}")

    # Floor stays 2560 through everything we produced.
    if min_free_floor_mib(cfg) != FLOOR_MIB:
        raise ConfigRejected(f"{arm}: resulting RAM floor is not 2560 MiB")

    # env: unchanged except an allowlisted, explicitly supplied state-hash diagnostic.
    cfg_env = cfg.get("env") or {}
    if env:
        extra = set(env) - ALLOWED_ENV_KEYS
        if extra:
            raise ConfigRejected(f"{arm}: forbidden env drift {sorted(extra)}")
        for key in env:
            if cfg_env.get(key) != env[key]:
                deltas.append({"field": f"env/{key}", "from": cfg_env.get(key), "to": env[key]})
                cfg_env[key] = env[key]
        cfg["env"] = cfg_env

    return cfg, deltas


# Top-level keys that legitimately differ between arms; everything else must match.
_ALLOW_TOP = {"exe", "log", "port", "expert_helper_socket", "expert_helper_socket2"}
# Arg flags whose per-arm value is an isolated-path delta, removed before comparison.
_ALLOW_ARGS = {"--conversation-cache-dir"}


def verify_test_pair(baseline_cfg: dict, candidate_cfg: dict) -> list[str]:
    """Problems proving the two arm configs are equal except exe + isolated paths/cap.

    Returns [] when the arms are matched: identical quality args, identical env, the
    same memory-limit cap, and the immutable 2560 floor. The engine exe and the
    arm-specific cache directory are the only intentional differences.
    """
    problems: list[str] = []
    for label, cfg in (("baseline", baseline_cfg), ("candidate", candidate_cfg)):
        try:
            floor = min_free_floor_mib(cfg)
        except ConfigRejected as exc:
            problems.append(f"{label}: {exc}")
            continue
        if floor != FLOOR_MIB:
            problems.append(f"{label}: RAM floor {floor} != {FLOOR_MIB}")
        if cfg.get("expert_helper_socket") == cfg.get("expert_helper_socket2"):
            problems.append(f"{label}: helper sockets are not two distinct endpoints")

    if set(baseline_cfg) != set(candidate_cfg):
        problems.append(f"key sets differ: {sorted(set(baseline_cfg) ^ set(candidate_cfg))}")
        return problems

    for key in baseline_cfg:
        if key in _ALLOW_TOP:
            continue
        if key == "env":
            if baseline_cfg.get("env") != candidate_cfg.get("env"):
                problems.append("env block drifted between arms (state-hash diagnostics must match)")
        elif key == "args":
            problems.extend(_arg_differences(baseline_cfg["args"], candidate_cfg["args"]))
        elif baseline_cfg[key] != candidate_cfg[key]:
            problems.append(f"non-allowlisted config field drifted: {key}")

    if baseline_cfg.get("exe") == candidate_cfg.get("exe"):
        problems.append("arms share the same exe; the engine identity must differ")
    return problems


def _arg_differences(baseline_args: list[str], candidate_args: list[str]) -> list[str]:
    """Compare arg views after stripping arm-specific isolated-path flags."""
    base = _arg_view(baseline_args)
    cand = _arg_view(candidate_args)
    if base == cand:
        return []
    diffs = []
    for flag in sorted(set(base) | set(cand)):
        if base.get(flag) != cand.get(flag):
            diffs.append(f"quality/limit arg differs on {flag}: "
                         f"{base.get(flag)!r} vs {cand.get(flag)!r}")
    # Multiset of valueless positional tokens must also agree.
    base_flags = _flag_multiset(baseline_args)
    cand_flags = _flag_multiset(candidate_args)
    if base_flags != cand_flags:
        diffs.append("arg flag multiset differs between arms")
    return diffs


def _arg_view(args: list[str]) -> dict:
    """flag -> value map, excluding arm-isolated flags whose values legitimately differ."""
    view: dict[str, str | bool] = {}
    i = 0
    while i < len(args):
        token = args[i]
        if token.startswith("--"):
            nxt = args[i + 1] if i + 1 < len(args) else None
            if nxt is not None and not nxt.startswith("--"):
                if token not in _ALLOW_ARGS:
                    view[token] = nxt
                i += 2
                continue
            if token not in _ALLOW_ARGS:
                view[token] = True
        i += 1
    return view


def _flag_multiset(args: list[str]) -> dict:
    counts: dict[str, int] = {}
    for token in args:
        if token.startswith("--") and token not in _ALLOW_ARGS:
            counts[token] = counts.get(token, 0) + 1
    return counts


def provenance_manifest(files: dict[str, Path]) -> dict:
    """Compact role -> {path, sha256, bytes} manifest of explicitly supplied files.

    Covers base config, canonical launcher, wrapper/control sources, helper and engine
    binaries — recorded by file SHA256, never by a fictional commit pin (untracked
    sources have no git identity) and never by copying file contents or the full env.
    """
    manifest: dict[str, dict] = {}
    for role, path in files.items():
        p = Path(path)
        manifest[role] = {"path": str(p), "sha256": sha256_path(p), "bytes": p.stat().st_size}
    return {
        "offline_preparation": OFFLINE_PREPARATION,
        "files": manifest,
        "outstanding_gates": list(OUTSTANDING_GATES),
    }


def manifest_is_public(manifest: dict, *, secret_markers: tuple[str, ...] =
                       ("environ", "password", "secret", "api_key")) -> bool:
    """The committed manifest carries hashes + allowlisted deltas, never env/secrets.

    The markers target an inherited-environ dump and obvious credential keys; ordinary
    prose (e.g. "token IDs" in the gate list) is not treated as a secret leak.
    """
    blob = repr(manifest).lower()
    return not any(marker in blob for marker in secret_markers)


# =============================================================================
# 2. LOG / PREDICATE SUPPORT (actual-source log strings; each line counted once)
# =============================================================================

# A model-call timing line: reused/read split comes from the real parser, not first 'tok/s'.
_PARK_REFUSAL = re.compile(r"skip parking \(", re.IGNORECASE)
_PARK_REFUSAL_ALT = re.compile(r"disk park skipped \(", re.IGNORECASE)
_PARK_ALLOC = re.compile(r"allocation failed; skip parking", re.IGNORECASE)
_CKPT_SKIP = re.compile(r"checkpoint at \d+ skipped:", re.IGNORECASE)
_RESTORE_SKIP = re.compile(r"skip restore \(", re.IGNORECASE)
_RESTORE_SKIPPED = re.compile(r"disk restore skipped \(", re.IGNORECASE)
# Fix-ONLY engagement lines (present on the approved RAM source; a lower count is not a
# baseline comparison). Boot band, admitted-after, eviction-to-admit, RAM-guard admitted.
_ENGAGE = re.compile(r"(admitted after|is the limiter|RAM guard admitted|"
                     r"evicted \d+ parked conversation|for its \d+ MiB host copy)", re.IGNORECASE)
_FIX_ONLY_HEADROOM = re.compile(r"restore headroom", re.IGNORECASE)

# Publication + restore boundaries (distinct fields, never equated):
_PUB = re.compile(r"disk park streamed (\d+) tokens.*?"
                  r"anchor=(-?\d+) pair=(-?\d+) tail-draft=([01])", re.IGNORECASE)
_RESTORED = re.compile(r"restored (\d+) tokens from disk \((.*?)\) in [\d.]+ ms: (.*)$",
                       re.IGNORECASE | re.MULTILINE)


def _lines(blob: bytes) -> list[str]:
    text = blob.replace(b"\0", b"").decode("utf-8", "replace")
    return [line for line in text.splitlines() if line.strip()]


def prompt_passes(window: bytes) -> list:
    """Every real timing line in one request's byte window (reused/read PP/TG split)."""
    return parse_window(window)


def operation_totals(window: bytes) -> dict:
    """Per-operation refusal counts (reason-INDEPENDENT) plus reason diagnostics.

    Each line contributes to at most one operation bucket. Reason-specific counts are
    diagnostics only: the candidate's `restore headroom` refusal is a `park` or
    `checkpoint` refusal like the baseline's `physical RAM`, so totals stay comparable.
    """
    totals = {"park_refusal": 0, "checkpoint_skip": 0, "restore_refusal": 0}
    reasons: dict[str, int] = {}
    fix_only_engagement = 0
    for line in _lines(window):
        bucket = None
        if _RESTORE_SKIPPED.search(line) or _RESTORE_SKIP.search(line):
            bucket = "restore_refusal"
        elif _CKPT_SKIP.search(line):
            bucket = "checkpoint_skip"
        elif _PARK_REFUSAL.search(line) or _PARK_REFUSAL_ALT.search(line) or _PARK_ALLOC.search(line):
            bucket = "park_refusal"
        if bucket:
            totals[bucket] += 1
            reason = _reason(line)
            reasons[reason] = reasons.get(reason, 0) + 1
            if _FIX_ONLY_HEADROOM.search(line):
                reasons["_restore_headroom(diagnostic)"] = reasons.get("_restore_headroom(diagnostic)", 0) + 1
        elif _ENGAGE.search(line):
            fix_only_engagement += 1
    return {
        "offline_preparation": OFFLINE_PREPARATION,
        "totals": totals,
        "reasons": reasons,
        "refusal_grand_total": sum(totals.values()),
        "fix_only_engagement": fix_only_engagement,
    }


def _reason(line: str) -> str:
    """Coarse reason label from a refusal line, for diagnostics only (not a total)."""
    lowered = line.lower()
    for probe, label in (("restore headroom", "restore headroom"),
                         ("physical ram admission", "physical RAM admission"),
                         ("physical ram", "physical RAM"),
                         ("ram floor", "RAM floor"),
                         ("below the", "below-min-tokens"),
                         ("exceeds available budget", "budget"),
                         ("could not be copied out", "copy-out"),
                         ("allocation failed", "allocation"),
                         ("no copy stream", "restore no-stream")):
        if probe in lowered:
            return label
    return "other"


def parse_publication(window: bytes) -> list[dict]:
    """`disk park streamed N ... anchor= pair= tail-draft=` rows (T,anchor,pair distinct)."""
    return [{"target_depth": int(m.group(1)), "anchor": int(m.group(2)),
             "pair_prefix": int(m.group(3)), "tail_draft": m.group(4) == "1",
             "line": m.group(0).strip()}
            for m in _PUB.finditer(window.replace(b"\0", b"").decode("utf-8", "replace"))]


def parse_restore(window: bytes) -> dict:
    """Restore facts from one window: success depth + file + draft-class, or a skip reason.

    A window that restored and one that were skipped cannot both be reported; a skip
    present alongside a `restored` line is itself a defect.
    """
    text = window.replace(b"\0", b"").decode("utf-8", "replace")
    successes = [{"depth": int(m.group(1)), "file": m.group(2), "draft_class": m.group(3).strip()}
                 for m in _RESTORED.finditer(text)]
    skipped = bool(_RESTORE_SKIPPED.search(text))
    skip_reason = None
    skip_match = _RESTORE_SKIPPED.search(text)
    if skip_match:
        skip_reason = skip_match.group(0)
    return {"successes": successes, "skipped": skipped, "skip_reason": skip_reason}


# Caller-declared turn classes. A REFUSAL turn must not require restore success; a
# byte-eviction miss (FALLBACK) is not restore-headroom engagement.
REFUSAL, FALLBACK, RESTORE_SUCCESS = "REFUSAL", "FALLBACK", "RESTORE_SUCCESS"


def classify_turn(window: bytes, declared_class: str, *, expected_reused: int | None = None) -> dict:
    """Score one declared turn against the lines actually in its byte window."""
    if declared_class not in (REFUSAL, FALLBACK, RESTORE_SUCCESS):
        return {"verdict": FAIL, "problems": [f"unknown turn class {declared_class!r}"]}
    ops = operation_totals(window)
    restore = parse_restore(window)
    reused = sum(p.reused for p in prompt_passes(window))
    problems: list[str] = []
    if declared_class == REFUSAL:
        if ops["refusal_grand_total"] == 0:
            problems.append("declared REFUSAL turn shows no refusal line")
        # A refusal turn must NOT be scored on restore success; success is ignored here.
    elif declared_class == FALLBACK:
        if reused != 0:
            problems.append(f"declared FALLBACK (byte-eviction miss) turn reused {reused} tokens")
        if restore["successes"]:
            problems.append("declared FALLBACK turn shows a restore success (conflated classes)")
    elif declared_class == RESTORE_SUCCESS:
        if not restore["successes"]:
            problems.append("declared RESTORE_SUCCESS turn shows no `restored ... from disk` line")
        if restore["skipped"]:
            problems.append("declared RESTORE_SUCCESS turn shows `disk restore skipped`")
        if expected_reused is not None and reused != expected_reused:
            problems.append(f"reused {reused} != expected {expected_reused}")
    verdict = PASS if not problems else FAIL
    return {"verdict": verdict, "problems": problems, "engaged": bool(ops["fix_only_engagement"]
            or ops["refusal_grand_total"] or restore["successes"])}


def pressure_engagement(window: bytes) -> dict:
    """PASS only when a pressure window shows a guard/refusal/restore line; else UNENGAGED.

    Unengaged pressure is never a pass: a window with no guard activity proves nothing
    about the restore headroom band and must not be scored as inert-cap success.
    """
    ops = operation_totals(window)
    restore = parse_restore(window)
    engaged = ops["fix_only_engagement"] + ops["refusal_grand_total"]
    if engaged == 0 and not restore["successes"]:
        return {"verdict": UNENGAGED, "problems": ["no park/checkpoint/restore guard line observed"]}
    if ops["fix_only_engagement"] == 0 and ops["refusal_grand_total"] == 0 and restore["successes"]:
        # A successful restore with no refusal/engagement line: engagement is not proven.
        return {"verdict": UNENGAGED, "problems": ["restore success without any guard/engagement line"]}
    return {"verdict": PASS, "problems": []}


def compare_state(actual: dict, expected: dict, required_keys: list) -> dict:
    """Pure nonempty required-key comparison of caller-supplied state-hash maps.

    Vacuous coverage (empty required_keys), a missing required key, an empty hash
    value, or any mismatch on a required key fails. Nothing is inferred.
    """
    if not required_keys:
        return {"verdict": FAIL, "problems": ["vacuous comparison: required_keys is empty"]}
    problems = []
    for key in required_keys:
        if key not in actual or actual[key] is None or actual[key] == "":
            problems.append(f"missing/empty state key {key!r} in actual")
            continue
        if key not in expected:
            problems.append(f"missing/empty state key {key!r} in expected")
        elif actual[key] != expected[key]:
            problems.append(f"state key {key!r} mismatch")
    return {"verdict": PASS if not problems else FAIL, "problems": problems}


def compare_token_ids(actual: list, expected: list) -> dict:
    """Exact raw token-ID sequence equality; empty coverage is rejected, never a vacuous pass."""
    if not actual or not expected:
        return {"verdict": FAIL, "problems": ["vacuous comparison: empty token-id sequence"]}
    if actual != expected:
        return {"verdict": FAIL, "problems": ["raw token-ID sequence mismatch"]}
    return {"verdict": PASS, "problems": []}


# =============================================================================
# 3. EXACT EXPECTATIONS (frozen BEFORE testing; never derived from the scored row)
# =============================================================================

@dataclass(frozen=True)
class RestoreExpectation:
    """Expected published identity + target depth, plus optional reused/read/draft-class.

    Every field asserted only when the fixture supplied it before the run; None means
    "not supplied, not asserted". These are declared values, never recomputed from the
    same actual row this object scores. T (target depth), anchor and pair_prefix stay
    distinct; nothing here equates pair_prefix < anchor with a shallower cold resume.
    """
    published_file: str | None = None      # expected restored file identity
    target_depth: int | None = None        # expected deepest PUBLISHED depth (== T)
    draft_class: str | None = None         # expected resume classification substring
    expected_reused: int | None = None
    expected_read: int | None = None
    reject_restore_skipped: bool = True


def evaluate_restore(window: bytes, expectation: RestoreExpectation) -> dict:
    """Validate an actual restore window against the frozen expectation; no tolerance."""
    restore = parse_restore(window)
    problems: list[str] = []
    if expectation.reject_restore_skipped and restore["skipped"]:
        problems.append(f"restore skipped: {restore['skip_reason']}")
    if not restore["successes"]:
        problems.append("no `restored ... from disk` line to evaluate")
        return {"verdict": FAIL, "problems": problems}
    if len(restore["successes"]) > 1:
        problems.append(f"ambiguous: {len(restore['successes'])} restore-success lines in one window")

    success = restore["successes"][0]
    if expectation.published_file is not None and success["file"] != expectation.published_file:
        problems.append(f"restored file {success['file']!r} != expected {expectation.published_file!r}")
    if expectation.target_depth is not None:
        if success["depth"] < expectation.target_depth:
            problems.append(f"restored depth {success['depth']} is shallower than target "
                            f"{expectation.target_depth}")
        elif success["depth"] != expectation.target_depth:
            problems.append(f"restored depth {success['depth']} != target {expectation.target_depth}")
    if expectation.draft_class is not None and expectation.draft_class not in success["draft_class"]:
        problems.append(f"draft class {success['draft_class']!r} does not match declared "
                        f"{expectation.draft_class!r}")

    passes = prompt_passes(window)
    if expectation.expected_reused is not None or expectation.expected_read is not None:
        if not passes:
            problems.append("no reused/read line to compare against expectations")
        reused = sum(p.reused for p in passes)
        read = sum(p.read for p in passes)
        if expectation.expected_reused is not None and reused != expectation.expected_reused:
            problems.append(f"reused {reused} != expected {expectation.expected_reused} "
                            "(reused>0 alone is not a pass)")
        if expectation.expected_read is not None and read != expectation.expected_read:
            problems.append(f"read {read} != expected {expectation.expected_read}")

    return {"verdict": PASS if not problems else FAIL, "problems": problems}


# =============================================================================
# 4. EXISTING DRM READER GUARD (reuses read_fdinfo_clients; not a second reader)
# =============================================================================

def gpu_eviction_guard(pid: int, required_pcis, proc_root: str) -> dict:
    """Eviction guard for a caller-supplied PID + expected PCI identities via the real reader.

    BLOCKED on any fdinfo read error, missing client identity, or a declared target PCI
    that has no gauge data. ABORT on any positive evicted bytes for a required target.
    Zero eviction is accepted only when data exists for every declared target PCI.
    This closes the reader affordance, not physical safety policy: free VRAM is NOT an
    eviction proxy, and no host-swap policy or periodic monitor is added here.
    """
    required = [str(p) for p in required_pcis]
    if not required:
        return {"verdict": BLOCKED, "problems": ["no required PCI identities declared"]}
    clients, _raw, errors = read_fdinfo_clients(pid, proc_root)
    if errors:
        return {"verdict": BLOCKED, "problems": [f"fdinfo read error: {err}" for err in errors]}
    if not clients:
        return {"verdict": BLOCKED, "problems": ["no DRM client identity observed for the selected PID"]}

    by_pci: dict[str, dict] = {}
    for client in clients:
        by_pci.setdefault(str(client["pci"]), client)

    problems_blocked: list[str] = []
    abort: list[str] = []
    for pci in required:
        client = by_pci.get(pci)
        if client is None:
            problems_blocked.append(f"no gauge data for required PCI {pci}")
            continue
        evicted = client.get("evicted_bytes")
        if evicted is None:
            problems_blocked.append(f"eviction gauge missing for required PCI {pci}")
        elif evicted > 0:
            abort.append(f"PCI {pci} shows {evicted} evicted bytes")
    if abort:
        return {"verdict": ABORT, "problems": abort, "clients": _summary(clients)}
    if problems_blocked:
        return {"verdict": BLOCKED, "problems": problems_blocked, "clients": _summary(clients)}
    return {"verdict": PASS, "problems": [], "clients": _summary(clients)}


def _summary(clients) -> list[dict]:
    return [{"pci": c["pci"], "client_id": c["client_id"], "fds": c["fds"],
             "evicted_bytes": c["evicted_bytes"]} for c in clients]


__all__ = [
    "FLOOR_MIB", "STATE_HASH_ENV", "OFFLINE_PREPARATION", "OUTSTANDING_GATES",
    "ConfigRejected", "min_free_floor_mib", "build_test_config", "verify_test_pair",
    "provenance_manifest", "manifest_is_public", "prompt_passes", "operation_totals",
    "parse_publication", "parse_restore", "classify_turn", "pressure_engagement",
    "compare_state", "compare_token_ids", "RestoreExpectation", "evaluate_restore",
    "gpu_eviction_guard", "sha256_path",
    "PASS", "FAIL", "UNENGAGED", "BLOCKED", "ABORT", "UNKNOWN",
    "REFUSAL", "FALLBACK", "RESTORE_SUCCESS",
]
