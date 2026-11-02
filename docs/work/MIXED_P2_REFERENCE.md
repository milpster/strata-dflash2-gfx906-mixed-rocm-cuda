# Mixed-backend numerical measurement

The original relative-error gate is still open. The older CUDA-only harness is not
an AMD reference. No measured GPU accuracy result follows from the builds below.

## Opt-in tools

`STRATA_P2_DIAGNOSTICS=ON` requires a native-expert gfx906 build. It adds
`p2_measure_hip_reference`, linking the same HIP MMQ library as the primary engine.
The CUDA helper is a separate process. Reference backend, ordinal, device name and
architecture are recorded in report notes. No NVIDIA runtime is linked into the
HIP reference process.

The option also enables `STRATA_P2_DUMP_PREFIX` in that build's engine. On the first
prefill chunk it writes `<prefix>.<layer>.bin` for layers32–47. Each file contains
three int64 dimensions, float activations and int32 routed expert IDs. Device copies
and writes are checked. Existing files are refused, not overwritten. Check every
expected file and the capture log; a partial capture is not a valid fixture.

The option is off by default. The legacy single-layer capture is unchanged. The
new hook is compiled out, not merely disabled by an environment variable.

## Validation produced here

On 2026-11-02, `build-mixed-gpu/nmse-reference1/build.log` recorded:

- Full default CUDA/gfx906 builds, followed by the opt-in gfx906 build, with `-j11`.
- Default primary and helper executable hashes identical to the preceding build.
- Six CTests per backend passed, including the real CPU reporting test.
- All52 Python writer/build-boundary/report tests passed in1.66seconds.
- HIP runtime from ROCm6.1 and engine rocBLAS from `~/rocm-gfx906-xnack`.
- Source pins and all four protected executable hashes matched after testing.

The first CPU invocation selected an old reporting binary automatically and raised
fixture errors. That failed log is retained. The passing runs explicitly selected
the real CMake CPU binary with `STRATA_P2_MEASURE_TEST`.

`tools/mixed_gpu/build_p2_diagnostics.sh` is the bounded local build recipe. It uses
the named unprotected validation trees; it never deletes a tree or fetches sources.
Its saved manifests and build log are local evidence, not a portable installer.

## Remaining physical evidence

Use fresh real inputs from at least eight fixtures and all layers32–47. Name the
reference backend and preserve every refusal and coverage gap. Run the strict-v2
auditor before interpreting rows. Keep relative p99.9≤0.005 **and** max≤0.02. Report
absolute error and sum-energy tensor NMSE separately; no NMSE threshold is defined.
MMQ product agreement does not establish complete task-output equivalence.

Do not use the legacy measurement scripts unchanged: their process cleanup and
production restoration do not meet the current supervised canonical contract.
