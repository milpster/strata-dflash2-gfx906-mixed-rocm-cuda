#!/usr/bin/env python3
"""Engine-log slicing and TG/PP summary math for the champions panel.

The engine prints one timing line per model call:

  strata serve: prompt N tokens = R reused + K read in P ms (X tok/s),
                   G generated in D ms (Y tok/s), ... timeouts=.. fallbacks=..

A driver leg covers TWO calls (fresh, then cached re-run) and records the byte
window of each. Continuation-shaped lines of the same form can appear inside a
request window, so a leg is only accepted when the two main lines can be matched
by their actual context counts, never by "take the last two grep hits".
"""
from __future__ import annotations

import json
import math
import re
import statistics
from dataclasses import asdict, dataclass, field
from pathlib import Path

# The unit is captured, not assumed: a seconds-valued line must not be read as ms.
PROMPT_LINE = re.compile(
    r"serve: prompt (\d+) tokens = (\d+) reused \+ (\d+) read in ([\d.]+) (\w+) "
    r"\(([\d.]+) tok/s\), (\d+) generated in ([\d.]+) (\w+) \(([\d.]+) tok/s\)"
    r"(?P<tail>[^\n]*)")
COUNTERS = re.compile(r"timeouts=(\d+).*?fallbacks=(\d+)")


@dataclass(frozen=True)
class EnginePass:
    """One engine timing line, already NUL-stripped."""

    tokens: int
    reused: int
    read: int
    prompt_ms: float
    pp_tps: float
    generated: int
    gen_ms: float
    tg_tps: float
    timeouts: int
    fallbacks: int
    line: str


def to_ms(value: float, unit: str) -> float:
    if unit.startswith("ms"):
        return value
    if unit.startswith("s"):
        return value * 1000.0
    raise ValueError(f"unknown timing unit {unit!r}")


def parse_window(blob: bytes) -> list[EnginePass]:
    """Every timing line inside one request's byte window."""
    text = blob.replace(b"\0", b"").decode("utf-8", "replace")
    passes = []
    for m in PROMPT_LINE.finditer(text):
        counters = COUNTERS.search(m.group("tail"))
        passes.append(EnginePass(
            tokens=int(m.group(1)), reused=int(m.group(2)), read=int(m.group(3)),
            prompt_ms=to_ms(float(m.group(4)), m.group(5)), pp_tps=float(m.group(6)),
            generated=int(m.group(7)), gen_ms=to_ms(float(m.group(8)), m.group(9)),
            tg_tps=float(m.group(10)),
            timeouts=int(counters.group(1)) if counters else 0,
            fallbacks=int(counters.group(2)) if counters else 0,
            line=m.group(0).strip(),
        ))
    return passes


@dataclass
class LegResult:
    """Outcome of one fresh+cached pair, with every rejection reason recorded."""

    arm: str
    leg: str
    shape: str
    pair: int
    measured: bool
    max_tokens: int
    prompt_bytes: int = 0
    intended_bytes: int = 0
    fresh: EnginePass | None = None
    cached: EnginePass | None = None
    fresh_reason: str = "ok"
    cached_reason: str = "ok"
    unmatched: list[str] = field(default_factory=list)
    walls: list[float] = field(default_factory=list)
    prompt_sha256: str = ""
    content_sha256: list[str] = field(default_factory=list)
    mem: dict = field(default_factory=dict)
    engine_retries: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def pp_usable(self) -> bool:
        return self.fresh is not None and self.fresh_reason == "ok"

    @property
    def tg_usable(self) -> bool:
        return self.cached is not None and self.cached_reason == "ok"

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def finite(self) -> bool:
        """Engine counters only. Completion TEXT is never inspected: the word "inf" in a
        reply is ordinary prose, not an arithmetic fault."""
        return finite_values([self])

    @property
    def context_tokens(self) -> int:
        return self.fresh.tokens if self.fresh else 0

    @property
    def reasons(self) -> list[str]:
        """Exact, human-readable cause for every way this pair is unusable."""
        reasons = list(self.problems)
        if self.fresh_reason != "ok":
            reasons.append(self.fresh_reason)
        if self.cached_reason != "ok":
            reasons.append(self.cached_reason)
        if self.fresh and self.cached and self.fresh.tokens != self.cached.tokens:
            reasons.append(f"context-mismatch-{self.fresh.tokens}-vs-{self.cached.tokens}")
        if self.fresh and self.context_tokens <= 0:
            reasons.append("zero-context-tokens")
        if not self.finite:
            reasons.append("non-finite-timing")
        if self.intended_bytes and self.prompt_bytes != self.intended_bytes:
            reasons.append(f"prompt-{self.prompt_bytes}B-vs-intended-{self.intended_bytes}B")
        return reasons

    @property
    def complete(self) -> bool:
        """Usable fresh AND cached timing, finite numbers, consistent context, no bad
        request. Anything less fails the arm rather than padding a median."""
        return not self.reasons

    @classmethod
    def from_dict(cls, data: dict) -> LegResult:
        """Round-trip a legs.jsonl row: nested timing lines come back as plain dicts."""
        leg = cls(**data)
        for key in ("fresh", "cached"):
            if isinstance(leg.__dict__[key], dict):
                leg.__dict__[key] = EnginePass(**leg.__dict__[key])
        return leg


def match_pair(first: list[EnginePass], second: list[EnginePass]) -> tuple[EnginePass | None,
                                                                          EnginePass | None,
                                                                          list[str]]:
    """Pick the two main-workload lines by actual context and reuse counts.

    Both legs re-read the same prompt, so they must agree on `tokens`, the first must
    report zero reuse and the second must report some. That combination is unique for a
    real pair; an internal continuation line repeats the shape but cannot satisfy it in
    both windows at once. Zero or several candidates means "unmatched", and the leg is
    dropped rather than resolved by taking the last grep hits.
    """
    candidates = [(a, b) for a in first for b in second
                  if a.tokens == b.tokens and a.reused == 0 and b.reused > 0
                  and a.read >= b.read and a.generated > 0 and b.generated > 0]
    if len(candidates) == 1:
        (a, b) = candidates[0]
        matched = {id(a), id(b)}
        unmatched = [p.line for p in first + second if id(p) not in matched]
        return a, b, unmatched
    return None, None, [p.line for p in first + second]


def classify(leg: LegResult, first: EnginePass | None, second: EnginePass | None) -> LegResult:
    """Apply the fresh/cached invariants. A cache hit is never counted as fresh PP."""
    if first is None or second is None:
        leg.fresh = leg.cached = None
        leg.fresh_reason = leg.cached_reason = "unmatched-timing-lines"
        return leg
    leg.fresh, leg.cached = first, second
    if first.reused != 0:
        leg.fresh_reason = f"fresh-leg-reused-{first.reused}"
    elif first.read <= 0:
        # Zero newly-read tokens is a re-read of a warm cache, not a prefill. Never
        # relabel it as fresh PP throughput.
        leg.fresh_reason = "fresh-leg-zero-read"
    elif first.generated <= 0:
        leg.fresh_reason = "fresh-leg-zero-generated"
    if second.reused <= 0:
        leg.cached_reason = "cached-leg-zero-reuse"
    elif second.generated <= 0:
        leg.cached_reason = "cached-leg-zero-generated"
    elif first.tokens != second.tokens:
        leg.cached_reason = "cached-leg-context-mismatch"
    return leg


def leg_from_record(record: dict, windows: list[bytes]) -> LegResult:
    """Turn one driver record plus its per-request byte windows into a LegResult."""
    requests = record.get("requests") or []
    first = parse_window(windows[0]) if len(windows) > 0 else []
    second = parse_window(windows[1]) if len(windows) > 1 else []
    fresh, cached, unmatched = match_pair(first, second)
    leg = LegResult(
        arm=record["arm"], leg=record["leg"], shape=record["champs"]["shape"],
        pair=record["champs"]["pair"], measured=record["champs"]["measured"],
        max_tokens=record["max_tokens"], unmatched=unmatched,
        prompt_bytes=record.get("prompt_bytes", 0),
        walls=[r["wall_s"] for r in requests],
        prompt_sha256=record.get("prompt_sha256", ""),
        content_sha256=[r["response_sha256"] for r in requests],
        mem=record.get("mem", {}),
        engine_retries=record["champs"].get("retries", 0),
    )
    if len(windows) != 2:
        leg.problems.append(f"expected 2 request windows, got {len(windows)}")
    if len(requests) != 2:
        leg.problems.append(f"expected 2 requests, got {len(requests)}")
    # A 200 response with no text is a failed measurement, not a slow one.
    for index, request in enumerate(requests):
        if request.get("content_from") == "empty" or not request.get("wall_s"):
            leg.problems.append(f"request {index} returned no usable response")
    # `unmatched` continuation-shaped lines are expected and stay on the record as
    # information; they are only a fault when the main pair itself failed to match.
    if fresh is None or cached is None:
        leg.problems.append(f"unmatched-timing-lines={len(unmatched)}")
    return classify(leg, fresh, cached)


def read_windows(record: dict) -> list[bytes]:
    """Byte-exact engine-log slice for each request of a leg."""
    requests = record.get("requests") or [{"elog_start": record["elog_start"],
                                          "elog_end": record["elog_end"]}]
    out = []
    for req in requests:
        with open(record["elog"], "rb") as f:
            f.seek(req["elog_start"])
            out.append(f.read(req["elog_end"] - req["elog_start"]))
    return out


def stats(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    return {"n": len(values), "median": round(statistics.median(values), 3),
            "min": round(min(values), 3), "max": round(max(values), 3),
            "spread": round(max(values) - min(values), 3)}


def usable_fresh(leg: LegResult) -> EnginePass | None:
    return leg.fresh if leg.pp_usable and leg.fresh else None


def usable_cached(leg: LegResult) -> EnginePass | None:
    return leg.cached if leg.tg_usable and leg.cached else None


def summarise(legs: list[LegResult]) -> dict:
    """Per-shape TG/PP medians from measured legs only, with exclusions itemised."""
    shapes: dict[str, dict] = {}
    for shape in sorted({leg.shape for leg in legs}):
        group = [leg for leg in legs if leg.shape == shape]
        measured = [leg for leg in group if leg.measured]
        fresh = [p for leg in measured if (p := usable_fresh(leg))]
        cached = [p for leg in measured if (p := usable_cached(leg))]
        accepted = sum(1 for leg in measured if leg.pp_usable and leg.tg_usable)
        shapes[shape] = {
            "pairs_total": len(group), "pairs_measured": len(measured),
            "pairs_excluded_warmup": len(group) - len(measured),
            "pairs_accepted": accepted,
            "pairs_dropped": [
                {"leg": leg.leg, "reasons": ([leg.fresh_reason] if not leg.pp_usable else [])
                 + ([leg.cached_reason] if not leg.tg_usable else []) + leg.problems}
                for leg in measured if not (leg.pp_usable and leg.tg_usable)],
            "pp_tok_s": stats([p.pp_tps for p in fresh]),
            "tg_tok_s": stats([p.tg_tps for p in cached]),
            "cold_tg_tok_s": stats([p.tg_tps for p in fresh]),
            "fresh_read": stats([p.read for p in fresh]),
            "context_tokens": sorted({p.tokens for p in fresh}),
            "fresh_reused": sorted({leg.fresh.reused for leg in measured if leg.fresh}),
            "cached_reused": sorted({leg.cached.reused for leg in measured if leg.cached}),
            "generated": stats([p.generated for p in cached]),
            "max_rss_mib": max_or_none([leg.mem.get("max_rss_mib") for leg in measured]),
            "max_vram_mib": max_vram([leg.mem.get("max_vram_mib") or {} for leg in measured]),
            "min_mem_available_mib": min_or_none([leg.mem.get("min_avail_mib") for leg in measured]),
            "timeouts": sum(p.timeouts for p in fresh + cached),
            "fallbacks": sum(p.fallbacks for p in fresh + cached),
            "walls_s": stats([max(leg.walls) for leg in measured if leg.walls]),
        }
    return {"shapes": shapes,
            "legs": [asdict(leg) for leg in legs],
            "finite_only": finite_values(legs)}


def finite_values(legs: list[LegResult]) -> bool:
    """Engine-reported rates must be finite. Generated TEXT is never scanned here: an
    ordinary completion that happens to contain the word "inf" is not an arithmetic error."""
    for leg in legs:
        for pass_ in (leg.fresh, leg.cached):
            if pass_ is None:
                continue
            for value in (pass_.pp_tps, pass_.tg_tps, pass_.prompt_ms, pass_.gen_ms):
                if not math.isfinite(value):
                    return False
    return True


def max_or_none(values: list) -> float | None:
    numbers = [v for v in values if isinstance(v, (int, float))]
    return max(numbers) if numbers else None


def min_or_none(values: list) -> float | None:
    numbers = [v for v in values if isinstance(v, (int, float))]
    return min(numbers) if numbers else None


def max_vram(per_leg: list[dict]) -> dict:
    merged: dict[str, int] = {}
    for card_map in per_leg:
        for card, mib in card_map.items():
            merged[card] = max(merged.get(card, 0), int(mib))
    return merged


def write_summary(arm: str, summary: dict, out: Path) -> None:
    """Per-arm JSON + CSV, written as soon as that arm finishes."""
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{arm}.summary.json").write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n")
    rows = ["arm,complete,pairs_complete,pairs_planned,shape,pairs_measured,pairs_accepted,"
            "pp_median,pp_spread,pp_n,tg_median,tg_spread,tg_n,cold_tg_median,"
            "context_tokens,fresh_read_median,cached_reused,max_rss_mib,"
            "min_mem_available_mib,timeouts,fallbacks"]
    for shape, s in summary["shapes"].items():
        pp, tg, cold = s["pp_tok_s"], s["tg_tok_s"], s["cold_tg_tok_s"]
        reused = "|".join(str(r) for r in s["cached_reused"])
        contexts = "|".join(str(t) for t in s["context_tokens"])
        rows.append(f"{arm},{summary.get('complete', False)},"
                    f"{summary.get('pairs_complete', 0)},{summary.get('pairs_planned', 0)},"
                    f"{shape},{s['pairs_measured']},{s['pairs_accepted']},"
                    f"{pp.get('median', '')},{pp.get('spread', '')},{pp.get('n', 0)},"
                    f"{tg.get('median', '')},{tg.get('spread', '')},{tg.get('n', 0)},"
                    f"{cold.get('median', '')},{contexts},{s['fresh_read'].get('median', '')},"
                    f"{reused},{s['max_rss_mib'] or ''},{s['min_mem_available_mib'] or ''},"
                    f"{s['timeouts']},{s['fallbacks']}")
    (out / f"{arm}.summary.csv").write_text("\n".join(rows) + "\n")


def print_summary(arm: str, summary: dict) -> None:
    complete = "COMPLETE" if summary.get("complete") else "INCOMPLETE"
    print(f"== {arm} [{complete} {summary.get('pairs_complete', 0)}/"
          f"{summary.get('pairs_planned', 0)} planned pairs]")
    for shape, s in summary["shapes"].items():
        pp, tg = s["pp_tok_s"], s["tg_tok_s"]
        pp_txt = (f"{pp['median']} tok/s (spread {pp['spread']}, n {pp['n']})"
                  if pp.get("n") else "NO USABLE PP")
        tg_txt = (f"{tg['median']} tok/s (spread {tg['spread']}, n {tg['n']})"
                  if tg.get("n") else "NO USABLE TG")
        print(f"  {shape:<9} PP {pp_txt:<38} TG {tg_txt}")
        print(f"              read {s['fresh_read'].get('median', '-')} tok  "
              f"fresh reused {s['fresh_reused']}  cached reused {s['cached_reused']}  "
              f"gen {s['generated'].get('median', '-')}  "
              f"RSS {s['max_rss_mib'] or '-'} MiB  minAvail {s['min_mem_available_mib'] or '-'} MiB  "
              f"t/o {s['timeouts']} fb {s['fallbacks']}")
        for drop in s["pairs_dropped"]:
            print(f"              dropped {drop['leg']}: {', '.join(drop['reasons'])}")
