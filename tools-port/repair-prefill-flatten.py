#!/usr/bin/env python3
"""Repair two blocks that the first pass of the prefill merge flattened onto one line.

The bug: a rule rejoined `theirs.split("\n")[1:]` with "" instead of "\n". The result was a single
line whose first characters are "//", so everything after it - including `static const bool es_on`,
`int es_gpu`, `const core::ModelGeometry& g` and `bool cpu_arm` - was a comment. The compiler said
"'g' was not declared in this scope", which is the right way to find that out.

Usage: python3 tools-port/repair-prefill-flatten.py
"""
import pathlib
import sys

P = pathlib.Path(__file__).resolve().parent.parent / "engine/src/prefill/prefill.cpp"

SPAN_BLOCK = """    // L5 (STRATA_ENGINE_SPANS=1): the engine-side spans' gate, read once per run.  Off: no clock is
    // read and no line is written.  It covers EVERY walked layer - the claim=0 lines name the layers
    // that never claim (the peer-served half of a layer split), each tagged with its attention class
    // - plus the per-chunk intake/walk/tail and the whole run, so a window attributes essentially all
    // of the prompt-processing wall.
    static const bool es_on = std::getenv("STRATA_ENGINE_SPANS") != nullptr;
    const auto es_run0 = es_on ? Clock::now() : Clock::time_point{};
    int es_gpu = -1;
    if (es_on) cudaGetDevice(&es_gpu);
    const core::ModelGeometry& g = *m.g;
"""

HELPER_BLOCK = """                    // M2 (STRATA_PREFILL_HELPER): this layer's helper claim, declared at the CPU share's own
                    // level so the combine tail (a brace level up from the walk) reads it too
                    std::vector<char> on_helper;
                    core::ExpertHelperPromptPlan hp_plan;
                    std::vector<int32_t> hp_claims, hp_pos;   // pair index -> the helper block's row
                    int64_t rows_helper = 0;
                    bool hp_sent = false;
                    // L3 (STRATA_RPC_PROFILE=1): per-claim send phases, filled at the send site and read at
                    // the combine's reply (same walk scope); zero unless the env is set.
                    int64_t hp_d2h_us = 0, hp_begin_us = 0;
                    bool cpu_arm = true;                  // auto: this layer may share (false: the arm without)
"""


def main():
    s = P.read_text(encoding="utf-8")
    lines = s.split("\n")
    fixed = 0
    for i, line in enumerate(lines):
        flat = line.startswith("    // ") or line.startswith("                    // ")
        if not flat or "\n" in line or len(line) < 400:
            continue
        # a flattened block carries a declaration inside a comment line: match it by its first and last line
        for prefix, marker, repl, ends in (
                ("    // L5 (STRATA_ENGINE_SPANS=1)", "static const bool es_on", SPAN_BLOCK, "const core::ModelGeometry& g = *m.g;"),
                ("                    // M2 (STRATA_PREFILL_HELPER)", "std::vector<char> on_helper", HELPER_BLOCK,
                 "(false: the arm without)")):
            if line.startswith(prefix) and marker in line:
                if not line.rstrip().endswith(ends):
                    sys.exit(f"line {i + 1}: flattened block does not end with {ends!r}")
                lines[i] = repl.rstrip("\n")
                fixed += 1
    if fixed != 2:
        sys.exit(f"expected 2 flattened blocks, repaired {fixed}")
    new = "\n".join(lines)
    # expected counts are the fork file's own (src/prefill/prefill.cpp at 564e1dbbea): es_on belongs to
    # Prefill::run and Prefill::run_impl, es_gpu only to run_impl, `g` to five functions
    for must, want in (("static const bool es_on = std::getenv(\"STRATA_ENGINE_SPANS\") != nullptr;", 2),
                       ("int es_gpu = -1;", 1),
                       ("const core::ModelGeometry& g = *m.g;", 5),
                       ("std::vector<char> on_helper;", 1),
                       ("bool cpu_arm = true;", 1)):
        if new.count(must) != want:
            sys.exit(f"after repair, {must!r} appears {new.count(must)} times, expected {want}")
    P.write_text(new, encoding="utf-8")
    print("repaired 2 flattened blocks")


main()
