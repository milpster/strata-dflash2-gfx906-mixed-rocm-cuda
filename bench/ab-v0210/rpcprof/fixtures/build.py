#!/usr/bin/env python3
"""ab9 collector fixtures: SYNTHETIC, hand-computable engine+helper logs (never run on a GPU).

Purpose: prove `ab9_collect.py` derives the SPEC-ab9 table correctly on CPU only, with numbers
whose expected values can be computed by hand (see EXPECTED.md for every division).

Run:
  python3 build.py
  python3 ../ab9_collect.py --dir . --tsv fixture-results.tsv --md fixture-results.md
  python3 ../ab9_collect.py --dir . --gates
Every number below is a fixture, not a measurement. Line shapes are copied from the real
emitters (src/prefill/prefill.cpp:6435 claim, :6545 dbg, tools/mixed_gpu/sidecar/helper_main.cpp:910
serve tier=p), so the collector's regexes are exercised on their real grammar.

Fixture simplification to keep in mind for the real window: a real arm C (PIPE=1) emits NO
engine-side `rpcprofile: claim` lines (the sync-path-only comment in prefill.cpp:6434), so C's
transfer_share comes from the helper side; the fixtures give C claim lines anyway to exercise
the parser on all three arms.
"""
import hashlib
import json
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
LOGS = HERE / "elog"  # 'logs/' and '*.log' are gitignored repo-wide, so the fixture logs use the
                      # .elog/.hlog suffixes and live where they can be committed
SHA = hashlib.sha256(b"ab9-fixture-parity").hexdigest()  # identical across arms -> gate4 parity PASS
DBG_A = 0  # arm A runs with the prompt helper off: zero helper lines (gate3 requires this)

# (arm, ctx) -> engine PP, leg wall s, per-claim numbers, per-serve numbers, DBG line count
SPECS = {
    ("A", 8000):  dict(pp=400.0, wall=20.0, claims=dict(n=4, d2h=2500, begin=500, reply=1500),
                       serves=None, dbg=DBG_A, low_vram=1),
    ("B", 8000):  dict(pp=200.0, wall=40.0, claims=dict(n=4, d2h=2500, begin=500, reply=1500),
                       serves=dict(n=4, queue=4000, lock=1000, reply=1500), dbg=4, low_vram=0),
    ("C", 8000):  dict(pp=350.0, wall=23.0, claims=dict(n=4, d2h=2500, begin=250, reply=800),
                       serves=dict(n=4, queue=1000, lock=500, reply=800), dbg=4, low_vram=0),
    ("m2", 8000): dict(pp=360.0, wall=22.0, claims=dict(n=4, d2h=2500, begin=500, reply=1500),
                       serves=dict(n=4, queue=3000, lock=900, reply=1500), dbg=4, low_vram=0),
    ("A", 80000): dict(pp=410.0, wall=200.0, claims=dict(n=8, d2h=2500, begin=1000, reply=3000),
                       serves=None, dbg=DBG_A, low_vram=0),
    ("B", 80000): dict(pp=205.0, wall=400.0, claims=dict(n=8, d2h=2500, begin=1000, reply=3000),
                       serves=dict(n=8, queue=8000, lock=2000, reply=3000), dbg=8, low_vram=0),
    ("C", 80000): dict(pp=360.0, wall=230.0, claims=dict(n=8, d2h=2500, begin=500, reply=1500),
                       serves=dict(n=8, queue=2000, lock=1000, reply=1500), dbg=8, low_vram=0),
}

DMON = """# gpu    sm   mem   enc   dec   fb     fbmem   cpu   maj   min   rxpci  txpci  framerate
# Idx     %     %     %     %    MB     MB      %     /s    /s     MB/s    MB/s     fps
    0    50    30     0     0   7145   8192    10     0     0     1000    2000      0
    0    50    30     0     0   7145   8192    10     0     0     1500    2500      0
    0    50    30     0     0   7145   8192    10     0     0     2000    3000      0
"""


def engine_lines(arm: str, ctx: int, s: dict) -> str:
    ms = round(ctx / s["pp"] * 1000, 1)
    out = [f"strata serve: prompt {ctx} tokens = 0 reused + {ctx} read in {ms} ms ({s['pp']} tok/s)"]
    for i in range(s["claims"]["n"]):
        c = s["claims"]
        out.append(f"rpcprofile: claim layer={i + 1} rows=10 sel=8 d2h_us={c['d2h']} begin_us={c['begin']} "
                   f"reply_us={c['reply']} img_kb=40 reply_kb=20 fallback=0")
    for i in range(s["dbg"]):
        out.append(f"prefill helper: layer {i + 1}, 10 rows (helper)")
    if s["low_vram"]:
        out.append("strata serve: WARNING: only 404 MiB of VRAM free at the start of this request "
                   "(below 512 MiB): when the card is this full the driver can page GPU memory to system RAM")
    return "\n".join(out) + "\n"


def helper_lines(arm: str, s: dict) -> str:
    if not s["serves"]:  # arm A: prompt tier off, only the decode/window tier is served
        return ("rpcprofile: serve tier=w layer=1 rows=10 queue_us=100 lock_us=50 reply_us=200 rows_kb=160\n"
                "rpcprofile: serve tier=w layer=2 rows=10 queue_us=100 lock_us=50 reply_us=200 rows_kb=160\n")
    return "".join(
        f"rpcprofile: serve tier=p layer={i + 1} rows=10 queue_us={s['serves']['queue']} "
        f"lock_us={s['serves']['lock']} reply_us={s['serves']['reply']} rows_kb=160\n"
        for i in range(s["serves"]["n"]))


def write_leg(arm: str, leg: str, kind: str, ctx: int, s: dict, walls: list[float]) -> None:
    elog = LOGS / f"{arm}-{leg}-engine.elog"
    hlog = LOGS / f"{arm}-{leg}-helper.hlog"
    if kind == "tg":  # cached rerun: walls only, no fresh-prefill lines
        elog.write_text("strata serve: prompt 2048 tokens = 2048 reused + 0 read in 3 ms (682.7 tok/s)\n")
        hlog.write_text("")
    else:
        elog.write_text(engine_lines(arm, ctx, s))
        hlog.write_text(helper_lines(arm, s))
    rec = {
        "arm": arm, "leg": leg, "kind": kind, "ctx": ctx,
        "max_tokens": 1024 if kind == "tg" else 8, "ts": 1730000000.0,
        "walls": walls, "wall_s": round(sum(walls), 3), "prompt_bytes": ctx, "sha256": SHA,
        "elog": str(elog), "elog_start": 0, "elog_end": elog.stat().st_size,
        "hlog": str(hlog), "hlog_start": 0, "hlog_end": hlog.stat().st_size,
    }
    (HERE / f"{arm}-{leg}.json").write_text(json.dumps(rec, indent=1) + "\n")


def main() -> int:
    LOGS.mkdir(exist_ok=True)
    for (arm, ctx), s in SPECS.items():
        write_leg(arm, f"{ctx}-1", "prefill", ctx, s, [s["wall"]])
    for arm in ("A", "B", "C", "m2"):
        write_leg(arm, "tg1024", "tg", 2048, SPECS[(arm, 8000)], [12.0, 3.0])
    (HERE / "A-8000-1-dmon.txt").write_text(DMON)
    print(f"fixtures: {len(SPECS)} prefill legs + 4 tg legs + 1 dmon sidecar in {HERE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
