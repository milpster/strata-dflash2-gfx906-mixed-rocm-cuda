#!/usr/bin/env python3
"""t0 single-arm DFlash numerical adapter (finite arms, raw evidence, no claims).

ONE arm per invocation: `--arm <name>` from a closed in-file table; never a stage list, never
the accepted 6-chain run.  The accepted driver `tools/mixed_gpu/run_acceptance.py` and the
capture utility `tools/mixed_gpu/acceptance.py` are IMPORTED UNCHANGED (byte-frozen) and reused
as-is: prepare, preflight, acquire_lock, Driver.start_chain / utility / log_args / stop_chain /
cleanup_active / stop_production / restore_production.  Nothing in them is edited, wrapped or
monkeypatched.  The ONE sanctioned module binding is `drv.CAND_ENGINE`: the adapter rebinds that
path constant in its OWN process to the arm's typed role binary BEFORE prepare/preflight - in
BOTH plan and execute - records from/to, and asserts that the prepared config, the composite
build-id and the ids agree on exactly that frozen role.  The signal, cleanup, lock, ownership
and restore code paths are the driver's, untouched.

Finite arms (design r5-three-gpu-design.md §3/§4, parent-runnable mapping only):
  num-red-graphon   red  helper off  env none                        2 warmups + 3 samples
  num-red-graphoff  red  helper off  STRATA_DFLASH_GRAPH=0           2 + 3
  num-red-map       red  helper off  STRATA_DF_DBG=1 + GRAPH=0       2 + 3   (cycle mapping)
  num-red-parity    DRY PLAN ONLY: --execute is REFUSED at this gate (gate t0-numeric-gate.md
                               §1.4: the byte cap is MONITORED, NOT ENFORCED - the capture seam
                               is blocking and bounded main-thread cancellation is not built;
                               and §1.5: the oracle needs BF16 type-30 weights)
  num-cand-graphon  cand helper off  env none                        2 + 3   + 1 follow-up
  num-cand-graphoff cand helper off  STRATA_DFLASH_GRAPH=0           2 + 3
  decode-baseline   cand helper on   (the accepted on-arm shape)     2 + 3

Role binaries (full SHA256, verified BEFORE any downtime, re-verified at the start boundary
INSIDE the lifecycle AFTER the production stop and BEFORE the candidate start; a post-stop
mismatch refuses the start, cleans up and runs the canonical restore):
  red   build-mixed-gpu/r4-dflash-before/bin/strata-hip 31ec15428d45d5405929c04aa163d01e09b05b03449e9383ef39c145fcbadda3
  cand  build-mixed-gpu/r4-dflash-after/bin/strata-hip  f83039d35fe8b34c77fcdcd172a918f67d4553f34896dd08ba8063b7c8d06662
`--cand-engine/--red-engine` override a role path ONLY together with an explicit full
`--*-sha256`; the pinned value and the used value are both recorded (no role mixup).

Requests: the accepted pinned sample only (SHA256
93d4b9ca1524c26e0d03309d044a3c6898289e0a52fa9d19aa8daabae07dfe8d, temperature 0 / seed 271828 /
top_k 1 / top_p 1 / max_tokens 256, never edited).  Each request is its own `acceptance.py
sample --warmups 0 --samples 1` capture with a UNIQUE label; every artifact (including a failed
warm-up) is kept.  The remaining requests STOP at the first non-clean request - a non-zero
capture rc, missing record/response/engine-log-slice artifacts, transport/JSON/usage/log
problems all count - and there is NO retry-until-pass.

Refusal and completion classification per request (raw, from the utility's own record):
  2xx AND record.problems EMPTY AND record.json + response.bin + log-engine.slice.bin all
      present AND utility rc 0                              -> clean-200
  2xx with the engine-log-slice notice "candidate tokens outside the vocabulary"
                                                            -> numeric-refusal-handled-200
                                                              (availability ONLY, never numeric)
  400 whose body carries "dflash: non-finite candidate scores" -> numeric-refusal-fatal-400
  everything else (incomplete-200, capture-missing, capture-failed rc!=0, transport-error,
  other-error) -> an error: the arm STOPS there, raw evidence retained.

Acceptance counters (follow-up): PRIMARY = the captured response's `timings.draft_n` /
`timings.draft_n_accepted` (serve publishes them from the engine's DONE line,
serve/server.py request_timings; they arrive in the response the adapter already keeps).
SECONDARY = a DONE-line parser over the engine log slice (a parser contract only: serve consumes
the engine's stdout pipe, so a real engine log slice carries NO DONE line).  Absent fields are
"unknown"; a 200 response alone never claims drafting.

Output SHA evidence (temp-0 runs, every clean capture): `sha256(response.bin)` is computed AT CAPTURE
time and recorded TWICE - in the per-sample record row as `output_sha256` (plus the content-level
`output_content_sha256`: the generated text, the discipline of tools/mixed_gpu/output_sha_table.py,
which the pinned temp-0 table build-mixed-gpu/temp0-output-sha-table.txt uses because the raw JSON
carries per-request timing fields), and as one line in the run's `output-shas.tsv` (arm, role, kind,
index, label, position, request sha256, response sha256, content sha256).  CLAIM SCOPE: SAME-ARM
repeat stability is the claim; OFF-vs-ON and red-vs-cand SHAs are recorded as EVIDENCE, NOT required
equal; the SHA identity key is (fresh engine, identical request sequence, position).  Rows exist for
clean-200 captures ONLY: a missing, refused, non-clean or incomplete record writes NO row and NO
sha field - never a placeholder - and the arm's stop-at-first-failure semantics are unchanged.

Survival attribution: the follow-up runs only after a handled HTTP 200 refusal and only while a
RECORDED engine child is alive by identity (exe inode + dev + starttime) - checked BEFORE the
request and again AFTER it.  `attributable = alive_before AND alive_after AND same_identity`;
a mid-request replacement makes the response "not attributable to the recorded engine
(survival unknown)".  A watchdog (lazy) restart never counts as survival.

Parity: the dump dir is run-owned and fresh.  The monitor is an OBSERVER (bytes/polls/breach
recorded, daemon thread, bounded join, partials preserved) - `cap_monitored_not_enforced: true`
- and live parity is refused at this gate.  The stage tool runs ONLY when dump fixtures exist
AND the drafter GGUF's tensors are all original BF16 (type 30, tools/dflash2_stage_parity.py:66);
a quantized drafter is UNRUN_TYPE (exact command recorded, not invoked, no download).  Verdicts
separate UNRUN_TYPE / UNRUN_FIXTURES / TOOL_REPORT_RC0 / TOOL_REPORT_RC1 / TOOL_CRASH.
`--parity-gguf PATH --parity-gguf-sha256 <64hex>` accepts a parent-supplied BF16 artifact only.

Lifecycle safety is the accepted driver's, reused verbatim: global exclusive flock, wrapper
children-only classification, identity inode/starttime/argv/cwd, pidfd-only signals, registration
before start, restoration_required before the production signal, blocked cleanup stops advance,
canonical restore ONLY /mnt/user/Strata/launch-prod-swift15-dflash-w4-q4_0.sh.  Fresh wrapper
identities are required on --execute.  Cleanup + restore run on every failure, interrupt, hash
change and boundary refusal.  No socket probe, no pgrep/pgroup, no os.kill, no start_new_session,
no arbitrary commands, no user env, no commits, no engine/helper built here.

Limits stated honestly: this adapter captures evidence.  It proves nothing about the numerical
root cause (the first non-finite stage is unproven), about prefill, about all-three-GPU phase
coverage, or about speed.  Refusal handling is availability, not numeric correctness.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import importlib.util
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(os.environ.get("STRATA_DFLASH_T0_ROOT",
                           str(Path(__file__).resolve().parents[2])))
# STRATA_DFLASH_T0_ROOT exists for the evidence runs that exercise an adapter SNAPSHOT from an
# evidence directory; the real file never uses the override (default = the repo root).

_SPEC = importlib.util.spec_from_file_location("accepted_mixed_gpu_driver",
                                               ROOT / "tools" / "mixed_gpu" / "run_acceptance.py")
assert _SPEC and _SPEC.loader
drv = importlib.util.module_from_spec(_SPEC)          # the byte-frozen accepted driver
sys.modules[_SPEC.name] = drv
_SPEC.loader.exec_module(drv)

TOOL_VERSION = "t0-numeric-2"

# ---------------------------------------------------------------- pinned frozen identities
DRIVER_SHA256 = "dea0e7d73f15b082cb7b5f3c98ddad5bd8e12fac89e064d9ca030109835238f2"
CAPTURE_SHA256 = "06e9fbd237055196ea89345a94594664591600c8357ff6dd9ccbc1aa0f0a4af7"
CONFIG_SHA256 = "4dbe460a484c53b435ecd8eeb909fb2247cff85c7ccdfb5573a332259a347afa"
SAMPLE_SHA256 = "93d4b9ca1524c26e0d03309d044a3c6898289e0a52fa9d19aa8daabae07dfe8d"

ROLES: dict[str, dict[str, str]] = {
    "red": {"path": str(ROOT / "build-mixed-gpu/r4-dflash-before/bin/strata-hip"),
            "sha256": "31ec15428d45d5405929c04aa163d01e09b05b03449e9383ef39c145fcbadda3"},
    "cand": {"path": str(ROOT / "build-mixed-gpu/r4-dflash-after/bin/strata-hip"),
             "sha256": "f83039d35fe8b34c77fcdcd172a918f67d4553f34896dd08ba8063b7c8d06662"},
}
PARITY_TOOL = ROOT / "tools" / "dflash2_stage_parity.py"
GGUF_READER = ROOT / "tools" / "gguf_reader.py"
BF16_TYPE_ID = 30                     # dflash2_stage_parity.py:66 - type 30 only

GRAPH_CONTROL_KEYS = ("STRATA_DFLASH_GRAPH", "STRATA_DF_DBG", "STRATA_DF_PARITY",
                      "STRATA_DF_EVENTS")
ENV_ALLOWLIST = frozenset({"STRATA_DFLASH_GRAPH", "STRATA_DF_DBG", "STRATA_DF_PARITY",
                           "STRATA_DF_PARITY_CYCLE", "STRATA_VERIFY_PROFILE",
                           "LOOKAHEAD_STATS", "STRATA_DECODE_TIMING"})
NUMERIC_FATAL_MARK = "dflash: non-finite candidate scores"
REFUSAL_NOTICE_MARK = "candidate tokens outside the vocabulary"
PARITY_BYTE_CAP = 2 * 1024 ** 3       # OBSERVED size, never an enforced stop (see §1.4 of the gate)
CAP_POLL_S = 2.0
PARITY_REFUSAL_MSG = ("num-red-parity --execute is refused at this gate: the parity byte cap is "
                      "MONITORED, NOT ENFORCED (the capture seam is blocking; bounded main-thread "
                      "cancellation via accepted ownership is not built), and the stage oracle "
                      "needs original BF16 (type 30) drafter weights. Dry plan is allowed.")
REPORT_SIGNATURE = "FIRST DIVERGENCE"  # the tool's Report.print() always emits this line
OUTPUT_SHA_FILE = "output-shas.tsv"    # per-run evidence file, created at the arm's start
OUTPUT_SHA_COLUMNS = ("arm", "role", "kind", "index", "label", "position",
                      "request_sha256", "response_sha256", "content_sha256")
OUTPUT_SHA_HEADER = (
    "# t0 numeric adapter output-SHA evidence.  Columns (tab-separated): "
    + " ".join(OUTPUT_SHA_COLUMNS) + ".",
    "# kind = warmup | sample | followup; index = 1-based ordinal INSIDE its kind; "
    "position = 1-based ordinal in the arm's whole request sequence (a stopped, non-row request "
    "still occupies its position).",
    "# CLAIM SCOPE: same-arm repeat stability is the claim.  OFF-vs-ON and red-vs-cand SHAs are "
    "recorded as EVIDENCE, NOT required equal - a difference here proves nothing by itself.",
    "# SHA identity key: (fresh engine, identical request sequence, position).  The request SHA "
    "column is the pinned sample request (identical bytes across arms).",
    "# response_sha256 = sha256 of the captured response.bin bytes verbatim.  content_sha256 = "
    "sha256 of the generated content (choices[0].text / choices[0].message.content), the discipline "
    "of tools/mixed_gpu/output_sha_table.py; an empty content field means the clean body carried no "
    "text content (e.g. a tool-calls-only completion).  Live responses carry server timing fields "
    "(serve/server.py request_timings), so a raw-byte difference can be metadata, not output "
    "divergence; the repeat-stability claim is checked on content_sha256.",
    "# Rows exist ONLY for clean-200 captures (record.json + response.bin + "
    "log-engine.slice.bin present, record.problems empty, utility rc 0).  A missing, refused, "
    "non-clean, transport-failed or incomplete record writes NO row and never a placeholder; the "
    "arm stops at the first non-clean request, so no row ever follows stopped_at.")


@dataclasses.dataclass(frozen=True)
class Arm:
    role: str                     # "red" | "cand" - fixed by the arm, never by the caller
    helper: str                   # "off" | "on" (accepted arm shapes; "on" = decode-baseline)
    env: dict[str, str]           # pinned keys, allowlisted, merged AFTER the accepted filters
    warmups: int
    samples: int
    followup: bool = False        # candidate graph-on only, after a handled HTTP 200 refusal
    parity: bool = False          # dry plan only: live execution refused at this gate


ARMS: dict[str, Arm] = {
    "num-red-graphon":   Arm("red", "off", {}, 2, 3),
    "num-red-graphoff":  Arm("red", "off", {"STRATA_DFLASH_GRAPH": "0"}, 2, 3),
    "num-red-map":       Arm("red", "off", {"STRATA_DF_DBG": "1",
                                            "STRATA_DFLASH_GRAPH": "0"}, 2, 3),
    "num-red-parity":    Arm("red", "off", {"STRATA_DF_PARITY_CYCLE": "<--parity-cycle>",
                                            "STRATA_DFLASH_GRAPH": "0"}, 0, 1, parity=True),
    "num-cand-graphon":  Arm("cand", "off", {}, 2, 3, followup=True),
    "num-cand-graphoff": Arm("cand", "off", {"STRATA_DFLASH_GRAPH": "0"}, 2, 3),
    "decode-baseline":   Arm("cand", "on", {}, 2, 3),
}


# ---------------------------------------------------------------- helpers

def _sha(h: Any, path: Path) -> str:
    return str(h.sha256(Path(path)))


def role_plan(args: argparse.Namespace) -> tuple[str, Path, str, bool]:
    """Resolve (role, path, expected sha256, override) from the arm and the typed overrides."""
    arm = ARMS[args.arm]
    role = arm.role
    pinned = Path(ROLES[role]["path"])
    pinned_sha = ROLES[role]["sha256"]
    override_path = args.red_engine if role == "red" else args.cand_engine
    override_sha = args.red_sha256 if role == "red" else args.cand_sha256
    if override_path is not None:
        if not override_sha:
            raise drv.Refusal(f"--{role}-engine needs --{role}-sha256 with the FULL 64-hex digest")
        if len(override_sha) != 64 or any(c not in "0123456789abcdef" for c in override_sha):
            raise drv.Refusal(f"--{role}-sha256 must be a full 64-hex digest, got {override_sha!r}")
        return role, Path(override_path), override_sha, True
    if override_sha:
        raise drv.Refusal(f"--{role}-sha256 without --{role}-engine: a role override needs BOTH")
    return role, pinned, pinned_sha, False


def drafter_gguf(prod: dict[str, Any]) -> Path:
    args = [str(a) for a in (prod.get("args") or [])]
    if "--dflash" not in args:
        raise drv.Refusal("the canonical config has no --dflash artifact; the numeric arms need it")
    return Path(args[args.index("--dflash") + 1])


def gguf_tensor_type_ids(path: Path) -> list[int]:
    """Tensor type ids from the repo's own reader (header only, weights are never read).
    tools/gguf_reader.py imports as a bare module (the same convention the parity tool needs:
    its `import gguf_reader` works when the repo root is the cwd)."""
    td = str(GGUF_READER.parent)
    if td not in sys.path:
        sys.path.insert(0, td)
    import gguf_reader as rd
    g = rd.GGUFFile(Path(path))
    return [int(t.type_id) for t in g.tensors]


def bf16_compatible(path: Path) -> tuple[bool, str]:
    """The parity oracle's typed precondition (tools/dflash2_stage_parity.py:66-73 raises on any
    tensor whose type id != 30).  True only when EVERY tensor is original BF16."""
    try:
        ids = gguf_tensor_type_ids(path)
    except Exception as exc:
        return False, f"unreadable GGUF header ({type(exc).__name__}: {exc})"
    if not ids:
        return False, "the GGUF declares no tensors"
    bad = sorted(set(t for t in ids if t != BF16_TYPE_ID))
    if bad:
        return False, f"quantized drafter weights (tensor type ids {bad[:6]} != {BF16_TYPE_ID})"
    return True, "all tensors original BF16 type 30"


def arm_env(base: dict[str, Any], arm: Arm, args: argparse.Namespace, stage: str) -> dict[str, str]:
    """The supported seam: derive_base's env (canonical config env + the accepted TELEMETRY) with
    the arm's pinned keys merged on top.  The accepted DROP filters keep applying to the WRAPPER
    PROCESS env through clean_env (the driver does that at start_chain, unchanged); the engine's
    env comes from this config's env.  Canonical keys stay: STRATA_DFLASH_SAMPLED=1 and
    CUDA_VISIBLE_DEVICES=0,1 (no topology change); nothing here is removed from the engine env."""
    env = dict(base.get("env") or {})
    pins = dict(arm.env)
    if arm.parity:
        pins["STRATA_DF_PARITY"] = str(Path(args.out_dir) / "parity" / stage)
        pins["STRATA_DF_PARITY_CYCLE"] = str(args.parity_cycle)
    for k in pins:
        if k not in ENV_ALLOWLIST:
            raise drv.Refusal(f"arm env key {k!r} is not on the fixed allowlist")
    env.update(pins)
    return env


def graph_audit(env: dict[str, str]) -> dict[str, Any]:
    """Recorded audit of the controls that force the graph path off (never inherited silently)."""
    return {k: env.get(k, "<absent>") for k in GRAPH_CONTROL_KEYS}


def parity_dir_size(path: Path) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.stat(os.path.join(root, name)).st_size
            except OSError:
                continue
    return total


def start_cap_observer(path: Path, cap: int) -> tuple[threading.Event, dict[str, Any]]:
    """OBSERVER ONLY (gate §1.4): a read-only size scan of the run-owned dump dir in a daemon
    thread.  It never signals, never writes, and CANNOT stop a blocking capture - the breach is
    recorded and read at the arm's end; the main thread joins with a bounded timeout.  The main
    thread acts on the record.  Live parity execution is refused at this gate; nothing here is
    presented as an enforced cap."""
    stop = threading.Event()
    state: dict[str, Any] = {"cap_observed_bytes": cap, "bytes": 0, "breach_observed": None,
                             "polls": 0, "cap_monitored_not_enforced": True}

    def poll() -> None:
        while not stop.is_set():
            size = parity_dir_size(path)
            state["bytes"], state["polls"] = size, state["polls"] + 1
            if size > cap:
                state["breach_observed"] = {"bytes": size, "cap_observed_bytes": cap,
                                            "at": time.time(),
                                            "note": "observed; capture was in flight, "
                                                    "no signal was sent"}
                return
            stop.wait(CAP_POLL_S)

    threading.Thread(target=poll, daemon=True).start()
    return stop, state


# ---------------------------------------------------------------- capture contract

REQUIRED_ARTIFACTS = ("record.json", "response.bin", "log-engine.slice.bin")


def read_sample_record(d: Any, label: str, utility_rc: int) -> dict[str, Any]:
    """The utility's own per-request evidence, classified strictly (gate §1.3):
    clean-200 requires a 2xx status, EMPTY record.problems, ALL required artifacts, rc 0.
    A clean row additionally carries the capture-time `output_sha256` (raw response.bin) and,
    when the body carries text, `output_content_sha256`; every other class carries NO sha field."""
    rec_dir = d.run_dir / f"evidence-{label}" / "sample-001"
    out: dict[str, Any] = {"label": label, "evidence_dir": str(rec_dir), "status": None,
                           "class": "capture-missing", "problems": [], "marks": {},
                           "utility_rc": utility_rc, "artifacts": {}}
    rec_path = rec_dir / "record.json"
    if not rec_path.exists():
        out["problems"].append(f"no record.json under {rec_dir}")
        return out
    rec = json.loads(rec_path.read_text(encoding="utf-8"))
    out["status"] = (rec.get("http") or {}).get("status")
    out["problems"] = list(rec.get("problems") or [])
    out["artifacts"] = {name: (rec_dir / name).exists() for name in REQUIRED_ARTIFACTS}
    body = b""
    if (rec_dir / "response.bin").exists():
        body = (rec_dir / "response.bin").read_bytes()
    slice_bytes = b""
    if (rec_dir / "log-engine.slice.bin").exists():
        slice_bytes = (rec_dir / "log-engine.slice.bin").read_bytes()
    fatal_in_body = NUMERIC_FATAL_MARK.encode() in body
    notice_in_log = REFUSAL_NOTICE_MARK.encode() in slice_bytes
    out["marks"] = {"fatal_mark_in_body": fatal_in_body,
                    "notice_mark_in_engine_log": notice_in_log}
    status = out["status"]
    complete = all(out["artifacts"][name] for name in REQUIRED_ARTIFACTS)
    if status is None:
        out["class"] = "transport-error"
    elif 200 <= int(status) < 300:
        if notice_in_log:
            out["class"] = "numeric-refusal-handled-200"
        elif out["problems"] or not complete:
            out["class"] = "incomplete-200"
        elif utility_rc != 0:
            out["class"] = "capture-failed"
        else:
            out["class"] = "clean-200"
    elif 400 <= int(status) < 500:
        out["class"] = "numeric-refusal-fatal-400" if fatal_in_body else "other-error-400"
    else:
        out["class"] = "other-error"
    # Output SHA evidence (capture-time, never a placeholder): recorded for a CLEAN capture only.
    # output_sha256 = the raw response.bin bytes; output_content_sha256 = the generated content
    # (the temp-0 table's discipline).  Any other class keeps NO sha field at all.
    if out["class"] == "clean-200":
        out["output_sha256"] = hashlib.sha256(body).hexdigest()
        content_sha = content_sha256_of(body)
        if content_sha is not None:
            out["output_content_sha256"] = content_sha
    return out


def content_sha256_of(body: bytes) -> str | None:
    """Content-level SHA256 of a chat completion: the generated text of choices[0] (text, else
    message.content) - the discipline of tools/mixed_gpu/output_sha_table.py, which hashes CONTENT
    because the raw JSON carries per-request timing fields.  None when the body carries no readable
    text content (a tool-calls-only completion, non-JSON, empty choices): NEVER a placeholder."""
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    choices = parsed.get("choices") if isinstance(parsed, dict) else None
    choices = choices if isinstance(choices, list) else []
    first = choices[0] if choices and isinstance(choices[0], dict) else {}
    text = first.get("text")
    if text is None:
        msg = first.get("message")
        text = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(text, str) or text == "":
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ensure_output_sha_header(path: Path) -> Path:
    """Write the TSV header exactly once (the run dir is fresh: prepare refuses a reuse)."""
    p = Path(path) / OUTPUT_SHA_FILE
    if not p.exists():
        p.write_text("\n".join(OUTPUT_SHA_HEADER) + "\n", encoding="utf-8")
    return p


def write_output_sha_row(out_dir: Path, arm: Arm, arm_name: str, rec: dict[str, Any],
                         kind: str, index: int, position: int) -> bool:
    """One evidence line per CLEAN capture; a non-clean record writes NOTHING (never a placeholder).
    Returns whether a row was written."""
    if rec["class"] != "clean-200" or "output_sha256" not in rec:
        return False
    path = ensure_output_sha_header(out_dir)
    row = (arm_name, arm.role, kind, str(index), rec["label"], str(position), SAMPLE_SHA256,
           rec["output_sha256"], rec.get("output_content_sha256", ""))
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\t".join(row) + "\n")
    return True


def parse_timings_acceptance(body: bytes) -> dict[str, Any]:
    """PRIMARY acceptance observable: the captured response's timings.draft_n /
    timings.draft_n_accepted (serve/server.py request_timings, fed by the engine's DONE line).
    Absent fields are 'unknown' - a 200 response alone never claims drafting."""
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"present": False, "draft_n": "unknown", "draft_n_accepted": "unknown",
                "note": "response is not JSON"}
    timings = parsed.get("timings") if isinstance(parsed, dict) else None
    timings = timings if isinstance(timings, dict) else {}
    has = "draft_n" in timings and "draft_n_accepted" in timings
    return {"present": bool(has),
            "draft_n": timings.get("draft_n", "unknown"),
            "draft_n_accepted": timings.get("draft_n_accepted", "unknown"),
            "note": "response.bin timings fields verbatim (serve/server.py request_timings)"}


def parse_done_acceptance(slice_bytes: bytes) -> dict[str, Any]:
    """SECONDARY, parser-only: serve consumes the engine's stdout PIPE, so a real engine log
    slice carries NO DONE line; this parses one when present and says 'unknown' when not.
    Never a claim on its own (gate §1.7)."""
    lines = [ln for ln in slice_bytes.decode("utf-8", "replace").splitlines()
             if ln.startswith("DONE ")]
    if not lines:
        return {"done_lines": [], "draft_accepted": "unknown", "draft_offered": "unknown",
                "note": "no DONE line in this request's engine log slice (expected: serve reads "
                        "the engine's stdout pipe; DONE lines live in the wrapper log, not here)"}
    raw = lines[-1]
    f = raw.split()
    acc = f[6] if len(f) > 6 else "unknown"
    off = f[7] if len(f) > 7 else "unknown"
    return {"done_lines": [raw], "draft_accepted": acc, "draft_offered": off,
            "note": "parser-only: DONE fields 7 and 8 verbatim (DONE produced_n n prompt_ms "
                    "decode_ms finish accepted offered ...)"}


def child_identity(kid: Any) -> tuple:
    return (kid.kind, kid.pid, kid.exe, kid.dev, kid.ino, kid.starttime)


def exact_children(d: Any, chain: Any) -> list[dict[str, Any]]:
    """The engine children the driver RECORDED at start, re-checked by identity: exe inode + dev
    + starttime + alive.  A watchdog (lazy) restart has a different identity and is NOT survival."""
    live = []
    for kid in chain.engine:
        try:
            exe, dev, ino = d.h.exe(kid.pid)
            st = d.h.starttime(kid.pid)
            if d.h.alive(kid.pid) and (kid.exe, kid.dev, kid.ino) == (exe, dev, ino) \
                    and kid.starttime == st:
                live.append(dict(kid.brief(), identity=child_identity(kid)))
        except OSError:
            continue
    return live


def sample_argv(d: Any, chain: Any, arm: Arm, phase: str, build_sha: str) -> list[str]:
    return ["sample", "--url", drv.URL, "--request", str(d.run_dir / "request-sample.json"),
            "--phase", phase, "--chain", chain.stage, "--build-id", build_sha,
            "--helper-state", arm.helper, "--samples", "1", "--warmups", "0",
            "--timeout", str(drv.SAMPLE_TIMEOUT_S)] + \
        d.log_args(chain, ("wrapper", "engine", "helper"))


# ---------------------------------------------------------------- validation gate (BEFORE any downtime)

def gate(args: argparse.Namespace, h: Any) -> dict[str, Any]:
    """Every hash and path checked before the lock, before preflight, before any signal:
    the imported driver and capture bytes, the canonical config, the arm's role binary, the
    drafter GGUF, the parity tool and the parity weight type.  Returns the binding view."""
    arm = ARMS[args.arm]
    role, path, sha, override = role_plan(args)
    if args.parity_cycle is not None:
        if not arm.parity:
            raise drv.Refusal(f"--parity-cycle is valid for num-red-parity only, not {args.arm}")
        if args.parity_cycle < 0:
            raise drv.Refusal(f"--parity-cycle must be >= 0, got {args.parity_cycle}")
    if arm.parity and args.parity_cycle is None:
        raise drv.Refusal("num-red-parity needs the typed --parity-cycle N (N from num-red-map's log)")
    if (args.parity_gguf is None) != (args.parity_gguf_sha256 is None):
        raise drv.Refusal("--parity-gguf and --parity-gguf-sha256 must be given TOGETHER")
    if args.parity_gguf_sha256 is not None and \
            (len(args.parity_gguf_sha256) != 64 or
             any(c not in "0123456789abcdef" for c in args.parity_gguf_sha256)):
        raise drv.Refusal("--parity-gguf-sha256 must be a full 64-hex digest")
    for name, p in (("driver", ROOT / "tools" / "mixed_gpu" / "run_acceptance.py"),
                    ("capture", drv.UTILITY), ("role_binary", path), ("parity_tool", PARITY_TOOL),
                    ("gguf_reader", GGUF_READER)):
        if not Path(p).exists():
            raise drv.Refusal(f"required path missing: {p}")
    d_sha, c_sha = _sha(h, ROOT / "tools" / "mixed_gpu" / "run_acceptance.py"), _sha(h, drv.UTILITY)
    if d_sha != DRIVER_SHA256:
        raise drv.Refusal(f"imported driver hash {d_sha} != the accepted frozen {DRIVER_SHA256}")
    if c_sha != CAPTURE_SHA256:
        raise drv.Refusal(f"imported capture hash {c_sha} != the accepted frozen {CAPTURE_SHA256}")
    cfg_sha = _sha(h, Path(args.config))
    if cfg_sha != CONFIG_SHA256:
        raise drv.Refusal(f"canonical config hash {cfg_sha} != the pinned {CONFIG_SHA256}; the "
                          f"config reconciliation is the parent's, this adapter refuses to run on it")
    role_sha = _sha(h, path)
    if role_sha != sha:
        raise drv.Refusal(f"role {role} binary {path} hash {role_sha} != the required {sha}")
    prod = json.loads(Path(args.config).read_text(encoding="utf-8"))
    gguf = drafter_gguf(prod)
    if not gguf.exists():
        raise drv.Refusal(f"the config's drafter artifact is missing: {gguf}")
    parity_gguf, parity_gguf_sha = gguf, None
    if args.parity_gguf is not None:
        parity_gguf = Path(args.parity_gguf)
        if not parity_gguf.exists():
            raise drv.Refusal(f"--parity-gguf path missing: {parity_gguf}")
        parity_gguf_sha = _sha(h, parity_gguf)
        if parity_gguf_sha != args.parity_gguf_sha256:
            raise drv.Refusal(f"--parity-gguf hash {parity_gguf_sha} != the required "
                              f"{args.parity_gguf_sha256}")
    ok, why = bf16_compatible(parity_gguf)
    return {"arm": args.arm, "role": role, "role_path": str(path), "role_sha256_used": role_sha,
            "role_sha256_pinned": ROLES[role]["sha256"], "role_override": override,
            "driver_sha256": d_sha, "capture_sha256": c_sha, "config_sha256": cfg_sha,
            "drafter_gguf": str(gguf), "drafter_gguf_sha256": _sha(h, gguf),
            "parity_gguf_used": str(parity_gguf),
            "parity_gguf_sha256": parity_gguf_sha or _sha(h, parity_gguf),
            "parity_gguf_bf16_compatible": ok, "parity_weight_state": "OK" if ok else "UNRUN_TYPE",
            "parity_weight_reason": why,
            "parity_tool_sha256": _sha(h, PARITY_TOOL),
            "cap_monitored_not_enforced": True,
            "notice_stderr_precondition": (
                f"the numeric-refusal notice '{REFUSAL_NOTICE_MARK}' is written to the engine's "
                f"STDERR log slice (generate.cpp serve notice path); the parent must confirm the "
                f"window's binary prints it, else handled-200 degrades to incomplete/clean and "
                f"the follow-up never runs")}


def adapter_ids(args: argparse.Namespace, h: Any, binding: dict[str, Any], arm: Arm) -> dict[str, Any]:
    parts = {"adapter_py": Path(__file__), "driver_py": ROOT / "tools" / "mixed_gpu" /
             "run_acceptance.py", "capture_py": drv.UTILITY, "canonical_config": Path(args.config),
             "role_binary": Path(binding["role_path"]), "drafter_gguf": Path(binding["drafter_gguf"]),
             "parity_tool": PARITY_TOOL, "parity_gguf_used": Path(binding["parity_gguf_used"])}
    if arm.helper == "on":
        parts["helper_cuda"] = drv.CAND_HELPER
    digests = {k: _sha(h, v) for k, v in parts.items()}
    blob = json.dumps(digests, sort_keys=True).encode("utf-8")
    return {"parts": {k: str(v) for k, v in parts.items()}, "sha256": digests,
            "adapter_id": hashlib.sha256(blob).hexdigest()[:24]}


# ---------------------------------------------------------------- the single arm

def run_parity_analysis(d: Any, arm: Arm, args: argparse.Namespace,
                        binding: dict[str, Any], pdir: Path) -> dict[str, Any]:
    """Parity outcome from the dump dir + the typed weight gate (gate §1.5).  The stage tool is
    invoked ONLY when fixtures exist AND the drafter weights are original BF16 (type 30); a
    quantized drafter is UNRUN_TYPE with the exact command recorded, NOT invoked, no download.
    Verdicts keep the report contract apart: TOOL_REPORT_RC0 / TOOL_REPORT_RC1 / TOOL_CRASH."""
    m = d.m
    out: dict[str, Any] = {"dir": str(pdir)}
    meta = pdir / "meta.bin"
    out["fixtures_present"] = meta.exists() and meta.stat().st_size > 0
    out["bytes_at_end"] = parity_dir_size(pdir)
    out["cap_monitored_not_enforced"] = True
    cmd = [str(drv.PY), str(PARITY_TOOL), "--dflash", binding["parity_gguf_used"],
           "--dir", str(pdir)]
    out["command"] = cmd
    out["weight_state"] = binding["parity_weight_state"]
    out["weight_reason"] = binding["parity_weight_reason"]
    if not binding["parity_gguf_bf16_compatible"]:
        out["verdict"] = "UNRUN_TYPE"
        out["note"] = ("the independent BF16 oracle cannot run on this drafter "
                       f"({binding['parity_weight_reason']}); the command above was recorded, NOT "
                       "invoked; no download, no model, no invented invocation")
        m.event("parity_unrun_type", arm=args.arm, cmd=cmd,
                reason=binding["parity_weight_reason"])
        return out
    if not out["fixtures_present"]:
        out["verdict"] = "UNRUN_FIXTURES"
        out["note"] = ("no dump fixtures (meta.bin missing/empty): the stage parity analysis is "
                       "NOT run and NOT invented; the exact command is recorded for the parent")
        m.event("parity_unrun_fixtures", arm=args.arm, cmd=cmd, dir=str(pdir),
                bytes=out["bytes_at_end"])
        return out
    rc, text_out, text_err = d.h.capture(cmd, str(ROOT), drv.CAPTURE_TIMEOUT_S)
    for suffix, text in (("rc", str(rc)), ("stdout", text_out), ("stderr", text_err)):
        (Path(args.out_dir) / f"parity-{pdir.name}.{suffix}").write_text(text, encoding="utf-8")
    report = REPORT_SIGNATURE in text_out
    out["rc"] = rc
    out["report_line_present"] = report
    if rc not in (0, 1):
        out["verdict"] = f"TOOL_ERROR_{rc}"
    elif not report:
        out["verdict"] = "TOOL_CRASH"
        out["note"] = (f"no report line ('{REPORT_SIGNATURE}') in the tool's stdout: a traceback "
                       "is NOT a report and NOT a PASS")
    else:
        out["verdict"] = "TOOL_REPORT_RC0" if rc == 0 else "TOOL_REPORT_RC1"
    m.event("parity_tool", arm=args.arm, rc=rc, verdict=out["verdict"], cmd=cmd,
            report_line_present=report)
    return out


def run_arm(d: Any, arm: Arm, args: argparse.Namespace, build_sha: str, chain: Any,
            binding: dict[str, Any]) -> dict[str, Any]:
    """Requests, detection, parity observer/analysis, follow-up.  Records everything; claims
    nothing.  Live parity never reaches here (refused in main)."""
    m = d.m
    stage = chain.stage
    requests: list[dict[str, Any]] = []
    outcome: dict[str, Any] = {"arm": args.arm, "requests": requests, "stopped_at": None,
                               "followup": {"ran": False}, "parity": {"applicable": arm.parity}}

    def one(label: str, phase: str, kind: str, index: int) -> dict[str, Any]:
        argv = sample_argv(d, chain, arm, phase, build_sha)
        row = d.utility(argv, label)
        rc = int(row.get("rc", 0)) if isinstance(row, dict) else 0
        rec = read_sample_record(d, label, rc)
        requests.append(rec)
        position = len(requests)                      # 1-based ordinal in the arm's sequence
        sha_fields = {k: v for k, v in (("output_sha256", rec.get("output_sha256")),
                                        ("output_content_sha256",
                                         rec.get("output_content_sha256"))) if v is not None}
        m.event("arm_request", arm=args.arm, label=label, phase=phase, kind=kind, index=index,
                position=position, status=rec["status"],
                klass=rec["class"], marks=rec["marks"], problems=rec["problems"],
                utility_rc=rc, artifacts=rec["artifacts"], **sha_fields)
        write_output_sha_row(Path(args.out_dir), arm, args.arm, rec, kind, index, position)
        return rec

    ensure_output_sha_header(Path(args.out_dir))      # header exactly once, at the arm's start
    m.event("output_sha_file", arm=args.arm, file=str(Path(args.out_dir) / OUTPUT_SHA_FILE),
            columns=list(OUTPUT_SHA_COLUMNS), rows_only_for="clean-200",
            identity_key="(fresh engine, identical request sequence, position)",
            claim_scope="same-arm repeat stability; OFF-vs-ON and red-vs-cand SHAs are evidence, "
                        "not required equal", placeholder_policy="never",
            header_lines=len(OUTPUT_SHA_HEADER))

    cap_stop = cap_state = None
    if arm.parity:
        pdir = Path(args.out_dir) / "parity" / stage
        if pdir.exists():
            raise drv.Refusal(f"parity dump dir exists, refusing to reuse: {pdir}")
        pdir.mkdir(parents=True)
        cap_stop, cap_state = start_cap_observer(pdir, PARITY_BYTE_CAP)
        m.event("parity_dir", arm=args.arm, dir=str(pdir), cap_observed_bytes=PARITY_BYTE_CAP,
                cap_monitored_not_enforced=True, cycle=args.parity_cycle, created="fresh")

    try:
        for kind, n in (("warm", arm.warmups), ("sample", arm.samples)):
            for i in range(1, n + 1):
                phase = "cold" if (kind == "warm" and i == 1) else "warm"
                row = one(f"{stage}-{kind}{i}", phase, "warmup" if kind == "warm" else "sample", i)
                if row["class"] != "clean-200":
                    outcome["stopped_at"] = {"label": row["label"], "class": row["class"],
                                             "status": row["status"], "utility_rc": row["utility_rc"]}
                    break
            if outcome["stopped_at"] is not None:
                break

        if arm.parity:
            pdir = Path(args.out_dir) / "parity" / stage
            outcome["parity"].update(run_parity_analysis(d, arm, args, binding, pdir))

        # follow-up: candidate graph-on, ONLY after a handled HTTP 200 refusal, ONLY while the
        # exact recorded engine child is alive (before AND after; a lazy restart never counts)
        if arm.followup:
            last = requests[-1] if requests else None
            handled = last is not None and last["class"] == "numeric-refusal-handled-200"
            alive_before = exact_children(d, chain)
            outcome["followup"] = {"handled_200_refusal": handled,
                                   "alive_before": alive_before, "ran": False}
            if handled and alive_before:
                row = one(f"{stage}-followup", "warm", "followup", 1)
                alive_after = exact_children(d, chain)
                same = {k["identity"] for k in alive_before} == {k["identity"] for k in alive_after}
                attributable = bool(alive_before) and bool(alive_after) and same
                body = b""
                rec_dir = d.run_dir / f"evidence-{row['label']}" / "sample-001"
                if (rec_dir / "response.bin").exists():
                    body = (rec_dir / "response.bin").read_bytes()
                slice_bytes = (rec_dir / "log-engine.slice.bin").read_bytes() \
                    if (rec_dir / "log-engine.slice.bin").exists() else b""
                outcome["followup"].update(
                    ran=True, label=row["label"], status=row["status"], klass=row["class"],
                    position=len(requests), kind="followup", index=1,
                    **{k: v for k, v in (("output_sha256", row.get("output_sha256")),
                                         ("output_content_sha256",
                                          row.get("output_content_sha256"))) if v is not None},
                    alive_after=alive_after, same_identity=same, attributable=attributable,
                    acceptance_timings=parse_timings_acceptance(body),
                    acceptance_done_parser_only=parse_done_acceptance(slice_bytes))
                if not attributable:
                    outcome["followup"]["attributability"] = \
                        "NOT attributable to the recorded engine (survival unknown): the child " \
                        "was replaced during the request (a lazy restart is not survival)"
                m.event("followup_decision", arm=args.arm,
                        **{k: v for k, v in outcome["followup"].items() if k != "ran"})
            else:
                outcome["followup"]["alive_after"] = []
                outcome["followup"]["attributable"] = False
                outcome["followup"]["skipped_reason"] = \
                    ("no handled 200 refusal to follow" if not handled
                     else "no recorded engine child is alive (a watchdog restart is not survival)")
                m.event("followup_decision", arm=args.arm, **outcome["followup"])
    finally:
        if cap_stop is not None:
            cap_stop.set()
            cap_stop.wait(timeout=CAP_POLL_S + 1.0)      # bounded join of the OBSERVER only
            outcome["parity_observer"] = {k: v for k, v in cap_state.items()}
            if cap_state.get("breach_observed") is not None:
                d.capture_failures.append(
                    f"parity byte cap OBSERVED as exceeded ({cap_state['breach_observed']['bytes']} "
                    f"> {cap_state['breach_observed']['cap_observed_bytes']}); live parity is "
                    f"refused at this gate, partial dumps kept")
                m.event("parity_cap_observed", arm=args.arm,
                        cap_monitored_not_enforced=True, **cap_state["breach_observed"],
                        preserved_dir=str(Path(args.out_dir) / "parity" / stage))
    return outcome


# ---------------------------------------------------------------- plan / execute

def _bind(role_binding: dict[str, Any]) -> str:
    """The ONE sanctioned module binding, applied in BOTH plan and execute before prepare."""
    from_default = str(drv.CAND_ENGINE)
    drv.CAND_ENGINE = Path(role_binding["role_path"])
    role_binding["cand_engine_bound_from"] = from_default
    role_binding["cand_engine_bound_to"] = str(drv.CAND_ENGINE)
    return from_default


def _assert_identity_consistency(p: dict[str, Any], ids: dict[str, Any],
                                 binding: dict[str, Any]) -> dict[str, Any]:
    """The prepared config, the composite build-id and the ids must ALL name the frozen role."""
    checks = {
        "role_path == drv.CAND_ENGINE": str(drv.CAND_ENGINE) == binding["role_path"],
        "ids.parts.role_binary == role_path": ids["parts"]["role_binary"] == binding["role_path"],
        "ids.sha256.role_binary == role_sha256_used":
            ids["sha256"]["role_binary"] == binding["role_sha256_used"],
        "driver composite engine_hip == role_path":
            str(p["ids"]["parts"]["engine_hip"]) == binding["role_path"],
        "driver composite engine_hip sha == role_sha256_used":
            p["ids"]["sha256"]["engine_hip"] == binding["role_sha256_used"],
        "derived off-config exe == role_path": str(p["off"]["exe"]) == binding["role_path"],
        "derived on-config exe == role_path": str(p["on"]["exe"]) == binding["role_path"],
    }
    bad = {k: v for k, v in checks.items() if not v}
    if bad:
        raise drv.Refusal(f"identity-consistency check failed: {bad}")
    return checks


def plan(args: argparse.Namespace, h: Any) -> int:
    binding = gate(args, h)
    _bind(binding)                                    # bind BEFORE prepare (gate §1.1)
    p = drv.prepare(args, h)
    arm = ARMS[args.arm]
    stage = args.arm
    env = arm_env(p["base"], arm, args, stage)
    canon_env = dict(p["prod"].get("env") or {})
    ids = adapter_ids(args, h, binding, arm)
    consistency = _assert_identity_consistency(p, ids, binding)
    sample_sha = _sha(h, args.out_dir / "request-sample.json")
    if sample_sha != SAMPLE_SHA256:
        raise drv.Refusal(f"the prepared pinned sample {sample_sha} != {SAMPLE_SHA256}")
    p["manifest"].event("adapter_binding", tool_version=TOOL_VERSION, mode="plan", **binding,
                        identity_consistency=consistency)
    view = {"mode": "plan", "tool_version": TOOL_VERSION, "arm": args.arm,
            "run_dir": str(args.out_dir), "binding": binding,
            "identity_consistency": consistency,
            "arm_spec": {"role": arm.role, "helper": arm.helper, "warmups": arm.warmups,
                         "samples": arm.samples, "followup": arm.followup, "parity": arm.parity,
                         "parity_execute_refused_at_gate": arm.parity},
            "parity_cycle": args.parity_cycle,
            "engine_env": env, "graph_controls_effective": graph_audit(env),
            "env_delta_vs_canonical": drv._diff(canon_env, env),
            "requests": [{"label": f"{stage}-{k}{i}", "phase": ("cold" if k == "warm" and i == 1
                                                                else "warm"), "samples": 1,
                          "warmups": 0, "request_sha256": SAMPLE_SHA256}
                         for k, n in (("warm", arm.warmups), ("sample", arm.samples))
                         for i in range(1, n + 1)] +
                        ([{"label": f"{stage}-followup", "phase": "warm", "samples": 1,
                           "warmups": 0, "request_sha256": SAMPLE_SHA256}] if arm.followup else []),
            "parity_byte_cap_observed": PARITY_BYTE_CAP if arm.parity else None,
            "cap_monitored_not_enforced": True,
            "output_sha_evidence": {
                "file": str(Path(args.out_dir) / OUTPUT_SHA_FILE),
                "columns": list(OUTPUT_SHA_COLUMNS),
                "rows_only_for": "clean-200 captures with a response.bin (never a placeholder; "
                                 "missing/refused/non-clean/incomplete records write no row)",
                "identity_key": "(fresh engine, identical request sequence, position)",
                "claim_scope": "same-arm repeat stability; OFF-vs-ON and red-vs-cand SHAs are "
                               "recorded as EVIDENCE, not required equal",
                "sha_fields": {"response_sha256": "sha256(response.bin) verbatim, computed at "
                                                  "capture time",
                               "content_sha256": "sha256 of the generated content "
                                                 "(tools/mixed_gpu/output_sha_table.py discipline; "
                                                 "empty when a clean body carries no text)"},
                "manifest": "per-request rows of manifest.jsonl carry output_sha256 / "
                            "output_content_sha256 for clean captures only"},
            "ids": ids, "driver_composite_build_sha256": p["build_sha"],
            "lock_path": str(drv.LOCK_PATH),
            "restore": {"launcher": str(drv.LAUNCHER), "port": drv.PROD_PORT},
            "claims": {"numeric_pass": False, "root_cause_found": False, "availability_only": True,
                       "speed_claim": False, "all3gpu_phase_proven": False,
                       "measured_by_this_tool": False}}
    drv.write_json(Path(args.out_dir) / "plan.json", view)
    print(json.dumps(view, indent=1, sort_keys=True, default=str))
    return drv.EXIT_DONE


def execute(args: argparse.Namespace, h: Any) -> int:
    binding = gate(args, h)
    arm = ARMS[args.arm]
    _bind(binding)                                    # the same binding as plan, before prepare
    p = drv.prepare(args, h)
    prod, base, build_sha, m = p["prod"], p["base"], p["build_sha"], p["manifest"]
    ids = adapter_ids(args, h, binding, arm)
    consistency = _assert_identity_consistency(p, ids, binding)
    m.event("adapter_binding", tool_version=TOOL_VERSION, mode="execute", **binding,
            identity_consistency=consistency)
    sample_sha = _sha(h, Path(args.out_dir) / "request-sample.json")
    if sample_sha != SAMPLE_SHA256:
        m.event("run_refused", error=f"pinned sample hash {sample_sha} != {SAMPLE_SHA256}")
        return drv.EXIT_REFUSED
    stage = args.arm
    env = arm_env(base, arm, args, stage)
    base["env"] = env
    drv.write_json(Path(args.out_dir) / "build-sha-adapter.json", dict(ids,
                                                                       tool_version=TOOL_VERSION,
                                                                       driver_build_sha256=build_sha))
    m.event("adapter_ids", **ids, driver_build_sha256=build_sha,
            engine_env=env, graph_controls_effective=graph_audit(env),
            env_delta_vs_canonical=drv._diff(prod["env"], env),
            cap_monitored_not_enforced=True,
            notice_stderr_precondition=binding["notice_stderr_precondition"])

    lock = drv.acquire_lock(h)
    status = drv.EXIT_DONE
    d = drv.Driver(args, h, m, base)
    outcome: dict[str, Any] = {}
    try:
        try:
            ident, kids = drv.preflight(d, args, prod)
        except drv.Refusal as exc:                       # refusal: NOTHING was signalled
            m.event("run_refused", error=str(exc))
            status = drv.EXIT_REFUSED
        else:
            try:
                d.stop_production(ident, kids)
                # start-boundary recheck (gate §1.2): AFTER the production stop, INSIDE the
                # lifecycle, BEFORE any candidate start.  A mutation during preflight/production
                # stop refuses the start here; the accepted cleanup + canonical restore run below.
                live_sha = _sha(h, Path(binding["role_path"]))
                if live_sha != binding["role_sha256_used"]:
                    m.event("start_boundary_refusal", arm=args.arm, expected=
                            binding["role_sha256_used"], found=live_sha,
                            note="role binary changed after the production stop; the candidate "
                                 "was never started; cleanup + canonical restore follow")
                    d.capture_failures.append("role binary hash changed after the production "
                                              "stop; candidate start refused")
                    status = drv.EXIT_REFUSED
                else:
                    chain = d.start_chain(stage, arm.helper, build_sha)
                    try:
                        outcome = run_arm(d, arm, args, build_sha, chain, binding)
                    except (KeyboardInterrupt, SystemExit) as exc:
                        m.event("run_interrupted", arm=args.arm,
                                error=f"{type(exc).__name__}: {exc}")
                        d.capture_failures.append(f"interrupted at {args.arm}: {exc}")
                        raise
                    except (drv.Refusal, drv.StageFailure, OSError) as exc:
                        m.event("arm_failed", arm=args.arm, error=f"{type(exc).__name__}: {exc}")
                        d.capture_failures.append(f"arm {args.arm}: {exc}")
                    d.stop_chain(chain, f"end-of-{args.arm}")
            except (KeyboardInterrupt, SystemExit, drv.Refusal, drv.StageFailure, drv.OwnedSignal,
                    OSError) as exc:
                m.event("run_aborted", error=f"{type(exc).__name__}: {exc}")
                d.capture_failures.append(f"aborted: {exc}")
                status = drv.EXIT_FAIL
            except BaseException as exc:
                m.event("run_unexpected", error=f"{type(exc).__name__}: {exc}")
                d.capture_failures.append(f"unexpected: {exc}")
                status = drv.EXIT_FAIL
    finally:                                            # cleanup + restore ALWAYS run
        try:
            d.cleanup_active("run-end")
        except BaseException as exc:
            d.capture_failures.append(f"cleanup raised: {type(exc).__name__}: {exc}")
            m.event("cleanup_exception", error=f"{type(exc).__name__}: {exc}")
        try:
            d.restore_production(prod, build_sha)
        except BaseException as exc:
            d.restore.update(ok=False, status=f"restore raised {type(exc).__name__}: {exc}")
            m.event("restore_failed", error=f"{type(exc).__name__}: {exc}")
        if d.blocked:
            d.capture_failures.append("run blocked by a surviving process")
        if d.capture_failures or not d.restore["ok"]:
            status = drv.EXIT_FAIL if status != drv.EXIT_REFUSED else drv.EXIT_REFUSED
        summary = {"mode": "execute", "tool_version": TOOL_VERSION, "exit": status,
                   "arm": args.arm, "run_dir": str(d.run_dir), "binding": binding,
                   "arm_outcome": outcome, "restoration_required": d.restoration_required,
                   "production_stopped": d.production_stopped, "blocked": d.blocked,
                   "capture_failures": d.capture_failures, "restore": d.restore,
                   "labels": {"availability_only": True, "numeric_pass": False,
                              "root_cause_found": False, "cap_monitored_not_enforced": True},
                   "claims": {"numeric_pass": False, "root_cause_found": False,
                              "availability_only": True, "speed_claim": False,
                              "all3gpu_phase_proven": False, "measured_by_this_tool": False},
                   "manifest": str(m.path)}
        drv.write_json(Path(d.run_dir) / "summary.json", summary)
        m.event("summary", **summary)
        lock.close()
        print(json.dumps(summary, indent=1, sort_keys=True, default=str))
    return status


# ---------------------------------------------------------------- cli

def main(argv: list[str] | None = None, hooks: Any | None = None) -> int:
    ap = argparse.ArgumentParser(prog="run_dflash_numeric.py",
                                 description="t0 single-arm DFlash numerical adapter (raw evidence)")
    ap.add_argument("--arm", required=True, choices=sorted(ARMS),
                    help="exactly ONE finite arm per invocation")
    ap.add_argument("--parity-cycle", type=int, default=None,
                    help="num-red-parity: the exact drafter cycle N from num-red-map's log")
    ap.add_argument("--parity-gguf", type=Path, default=None,
                    help="a parent-supplied BF16 (type 30) drafter artifact for the stage oracle; "
                         "needs --parity-gguf-sha256; no download, no new model")
    ap.add_argument("--parity-gguf-sha256", default=None)
    ap.add_argument("--execute", action="store_true",
                    help="actually run; the default is a dry plan with no downtime")
    ap.add_argument("--expected-wrapper-pid", type=int, default=0)
    ap.add_argument("--expected-wrapper-starttime", type=int, default=0)
    ap.add_argument("--config", type=Path, default=None)
    ap.add_argument("--out-dir", type=Path, default=None)
    ap.add_argument("--expect-aux", action="append", default=[])
    ap.add_argument("--red-engine", type=Path, default=None,
                    help="override the red role path; needs --red-sha256 (full digest)")
    ap.add_argument("--red-sha256", default=None)
    ap.add_argument("--cand-engine", type=Path, default=None,
                    help="override the cand role path; needs --cand-sha256 (full digest)")
    ap.add_argument("--cand-sha256", default=None)
    a = ap.parse_args(argv if argv is not None else sys.argv[1:])
    if a.config is None:
        a.config = drv.PROD_CONFIG
    h = hooks or drv.Hooks()
    if a.execute and ARMS[a.arm].parity:                # BEFORE any ownership/downtime action
        print(f"REFUSED: {PARITY_REFUSAL_MSG}", file=sys.stderr)
        return drv.EXIT_REFUSED
    if a.out_dir is None:
        a.out_dir = drv.default_run_dir()
    if a.execute and (a.expected_wrapper_pid <= 0 or a.expected_wrapper_starttime <= 0):
        print("REFUSED: --execute needs --expected-wrapper-pid and --expected-wrapper-starttime "
              "(fresh identities)", file=sys.stderr)
        return drv.EXIT_REFUSED
    if a.execute and Path(a.config).resolve() != drv.PROD_CONFIG.resolve():
        print(f"REFUSED: --execute requires the canonical config {drv.PROD_CONFIG}, not {a.config}",
              file=sys.stderr)
        return drv.EXIT_REFUSED
    if a.execute:
        def _term(*_: Any) -> None:
            raise KeyboardInterrupt("SIGTERM")
        signal.signal(signal.SIGTERM, _term)
    try:
        drv.parse_expect_aux(a.expect_aux)
        return plan(a, h) if not a.execute else execute(a, h)
    except drv.Refusal as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return drv.EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
