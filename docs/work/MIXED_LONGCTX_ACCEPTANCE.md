# Mixed-backend long-context speed acceptance

Real-world speed of the canonical mixed production (two Radeon VII gfx906 engines plus
the RTX 3080 expert helper, the adopted 4200-slot / 6400 MiB fill identity) at long
context, measured on the live server. No outage: the production pair under test is the
system measured; its supervisor is identity-checked before and after each request.

## Method

`tools/mixed_gpu/finish_window.py` phase `longctx` (CPU-tested): one deterministic
thinking-off prompt (temperature 0, reasoning budget 0) sized past 80k tokens with
headroom, one non-streaming chat completion with a 6,000-token cap, acceptance decided
ONLY on the server-reported actual `usage.prompt_tokens >= 80,000`,
`usage.completion_tokens >= 5,000` and non-empty final content. The prompt-size estimate
never substitutes for the reported counts. Evidence per attempt: prompt bytes, request
bytes, raw reply and the result JSON under `build-mixed-gpu/p2-finish/longctx*/`.

## Measured result — 2026-11-02 (longctx2)

One request against the live canonical production (supervisor 2823343, attached and
serving before and after):

- Actual prompt tokens: 104,845 (acceptance floor 80,000 — pass).
- Actual completion tokens: 6,000 of a 6,000 cap (floor 5,000 — pass; `finish_reason`
  "length": the model was still writing at the cap).
- Final content: 31,160 characters, all checks green, HTTP 200.
- Wall time for the whole request: 494.9 s. Combined prompt+output rate 223.96 tok/s;
  output rate over the whole wall 12.12 tok/s (a lower bound on decode speed: the wall
  includes the 80k-token prefill and the request is non-streaming, so no
  prefill/decode split is claimed).
- The production pair stayed attached and served `/v1/models` 200 throughout; nothing
  was stopped or restored.

A first attempt (`longctx1`, evidence retained) used a task without a per-block
structure: same prompt size (104,730 tokens, pass), HTTP 200, content present, wall
250.8 s, but the model chose to stop at 1,464 completion tokens and failed the 5,000
floor. That is a property of the output task, not of the system's speed; the reworked
task (42 fifty-section blocks, at least 130 words per block paragraph, numbered
"Block N of 42", plus three closing parts) structurally demands output past the floor.

## Limits

One request, one config, one day; the decode rate above is a whole-wall lower bound;
attempt 1 shows the output-token count depends on the task given. This is real-world
speed evidence for the adopted mixed configuration, not a benchmark suite and not a
numerical-equivalence claim (that gate is separate and still fails; see
MIXED_P2_REFERENCE.md).
