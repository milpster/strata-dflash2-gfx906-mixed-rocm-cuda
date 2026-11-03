#!/usr/bin/env python3
"""Deterministic corpora for the v0.2.10 A/B (no deps, seeded).

Token counts are targets at ~3.7 chars/token English; the exact N is taken from the
response's usage.prompt_tokens and the engine's own log lines judge the rate."""
import random

_WORDS = ("the quick brown fox jumps over a lazy dog while silicon winds through copper "
          "lanes and a kernel schedules warps beneath the memory controller's watch ").split()


def _para(rnd, n_words):
    return " ".join(rnd.choice(_WORDS) for _ in range(n_words)).capitalize() + "."


def corpus(chars):
    rnd = random.Random(20261103)
    out = []
    got = 0
    while got < chars:
        p = _para(rnd, 60)
        out.append(p)
        got += len(p) + 1
    return " ".join(out)


PROBE_PROMPT = ("In exactly one short paragraph, explain why speculative decoding keeps the "
                "target model's output distribution. Answer in plain words.")

TG_PROMPT = "Write a technical diary entry about porting a CUDA engine to HIP on gfx906. Be concrete."

PP_CHARS = 16384 * 6          # ~16384 tokens (measured ~5.4 chars/token on this tokenizer)
INFILL_CHARS = 80000 * 6      # ~80k tokens


def infill_task(chars):
    """A deep continuation: 80k tokens of document, then a cut mid-paragraph the model
    continues from (infill at depth; fresh prefill line judges the read)."""
    body = corpus(chars - 512)
    cut = body.rfind(" ", 0, len(body) - 40)
    head, tail = body[:cut], body[cut + 1:]
    return ("Continue the following technical log exactly where it breaks mid-sentence, "
            "completing the broken sentence first, then continue the log for the rest of "
            "the paragraph.\n\n<log>\n" + head + "\n<<CONTINUE HERE>>\n" + tail + "\n</log>")
