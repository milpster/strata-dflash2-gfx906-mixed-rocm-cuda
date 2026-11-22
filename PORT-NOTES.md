# Porting the serving lane onto upstream v0.2.18

Branch `port-0218` in `/mnt/srcds/Strata-wt-0218`, started at the tag `v0.2.18` (commit `7d14a9ab10`).
Source of the fork's content: `/mnt/srcds/Strata-wt-merge-ipc`, branch `merge-helper-ipc-diet`, commit
`564e1dbbea`, read-only. Plan: `docs/PLAN-UPSTREAM-0218.md` (Phases 1 and 2). Nothing here was run on a
GPU; the engine was compiled for gfx906 only, and no process on port 8009 was touched.

## What was ported

| Group | Files | How |
| --- | --- | --- |
| Files only the fork has | 140 (`PORT-MANIFEST.txt`) | copied straight in, `src/` to `engine/src/`, `include/` to `engine/include/`, `sycl/` to `engine/sycl/`, `serve/` to `terminal/serve/` |
| Expert helper's own sources | 3 (`PORT-MANIFEST-HELPER.txt`) | copied to `engine/tools/mixed_gpu/sidecar/`; the plan's 140 did not count them, but the engine needs one of them and the helper target needs two |
| The lane's own test programs | 49 (`PORT-MANIFEST-TESTS.txt`) | copied to `engine/tests/`; `tools-port/lane-tests.py` prints that list from the two trees. Wired in `engine/cmake/strata_lane_tests.cmake` and run (item 3 and Verification) |
| What those programs need but no `add_executable` names | 5 (`PORT-MANIFEST-TESTSUP.txt`) | `tests/core/cuda_runtime.h` (the CPU-only shim `pin_budget.hpp`'s unconditional CUDA include needs) and the four sources of the lane's own deviation harness under `tools/mixed_gpu/p2_measure/` |
| DeepSeek V4.1's remaining pieces | 6 (`PORT-MANIFEST-DSV41.txt`) | its two `.cu` test programs and the two vendored attribution trees (Project Maya, antirez-ds4) |
| Headers both trees have | 30 | 24 applied cleanly with a three-way merge of the fork's difference against v0.2.18; 6 decided by hand |
| Sources both trees have | 37 | 29 applied cleanly; 8 by hand (mtp.cpp, peer_experts.cpp, pinned.cu, q36_engine.cu, iq_kernels.cu, remote_experts.cpp, conversation_cache_test.cpp, verify.hpp side) |
| `engine/src/core/expert_source.cpp` | 1 | hand merge: the fork's difference replayed with a three-way merge (52 of 57 pieces clean, 5 by hand) |
| The server | 10 behaviors | replayed by `tools-port/port-server.py` onto upstream's split server (`server_parts/base.py`, `server_parts/engines.py`, `server.py`) |

Checklist gate: `tools-port/apply-manifest.sh` copies from the manifest and stops if a file is missing in
the fork tree. Re-running it refreshes the copies.

## What is NOT carried, and why

1. **`engine/src/prefill/prefill.cpp` and `engine/src/program/generate.cpp` keep upstream v0.2.18's
   version unchanged.** The fork changes them by 15 and 40 pieces. Those pieces sit inside the dispatch
   loop that upstream rewrote in 0.2.18 (the request-owned decode path), and after several attempts no
   merge could be shown correct by reading. A half-merged decode loop is a worse outcome than a named
   missing feature, so the two files stayed upstream's. Measured 2026-11-22 to size the job: against
   v0.2.18 the fork's `generate.cpp` differs by 397 hunks (+5645 / -4107 lines; upstream's file is
   24627 lines, the fork's 26165) and `prefill.cpp` by 175 hunks (+1319 / -2081). Most of that is
   upstream's own movement and features this port does not carry, not the lane's.
   `tools-port/port-worklist.py` splits it by feature and writes the lane's own pieces, with their
   code, into `PORT-WORKLIST-UNPORTED.md` - in `generate.cpp`: 15 expert-helper hunks, 6 CPU-pool,
   6 VRAM-clamp, 59 drafter; in `prefill.cpp`: 6 expert-helper, 53 prompt-kernel. That is the list to
   work from, one feature at a time, with a boot and an output comparison after each.
   *What this costs:* the prompt tier does not hand claimed expert rows to the helper, and the decode
   loop does not attach to a running helper. Everything the lane needs *inside* the engine's other files
   is in place (the client, the frame transport, the eligibility rules, the wire protocol), but the two
   call sites are not. End-to-end helper serving is therefore not reachable on this port yet.
   *How close the fork's own build pair is, measured 2026-11-22 in `build-lane-check` (a scratch
   configure, deleted afterwards; the logs are in `logs-port/`): with
   `-DSTRATA_ENABLE_DFLASH2=ON -DSTRATA_HAVE_DFLASH2=OFF` - the pair this box's production build uses -
   `strata_kernels` (with the lane's `dflash_inco.cu`) and `strata_engine` (with `dflash.cpp`,
   `dflash_runtime.cpp`, `dflash_tap_collect.cpp`) **compile clean**, and `strata` fails at one file:
   `generate.cpp`, five error sites, all one cause - upstream's `generate.cpp` names
   `strata::core::Dflash2Drafter` and `defer_df2`, and those exist only when upstream's drafter is
   compiled. One verifier may have only one tap producer, so the two cannot both be compiled
   (engine/CMakeLists.txt refuses that pair by name). In other words: the lane's DFlash2 runtime and the
   rest of the lane are buildable today; `generate.cpp` is the single file in the way, and it is one of
   the two un-ported ones. That also means the same file holds both remaining gaps.
2. **The DeepSeek V4.1 engine (`engine/src/dsv41/`, 51 files in the manifest) is now in the build, under
   the fork's own switch and the fork's own guard.** `option(STRATA_BUILD_DSV41 ... OFF)`, and the whole
   block is inside `if(STRATA_BUILD_DSV41 AND NOT STRATA_HIP_GFX906)` - which is how the fork builds it:
   the family needs the vendored EXL3 decode kernels, and a gfx906 card does not get them. Its tests and
   the two vendored attribution trees (`third_party/project-maya`, `third_party/antirez-ds4`) came across
   with `PORT-MANIFEST-DSV41.txt`. One path is this tree's: the fork's `third_party/ggml` include is
   `STRATA_GGML_DIR` here, which is what v0.2.18 uses for the vendored ggml. One file belongs to none of
   the manifests: the two `ds4_tq_test` programs compile `tools/trellis/tqenc.cpp` (the trellis encoder
   and its double reference), so `engine/tools/trellis/tqenc.cpp` and `tqenc.hpp` came across too - the
   plan's 140 does not count them and neither does the DSV41 group.
   *What this means for verification:* a gfx906 build compiles none of it, exactly as the fork's build on
   this card, so nothing in it is proven by this port - it needs a CUDA build, and this box cannot make
   one today (item 5).
3. **The lane's own test programs are wired and run.** `PORT-MANIFEST.txt` covers `src/`, `include/`,
   `sycl/` and `serve/` - the plan's count - so the lane's tests under `tests/` were nobody's: 49
   fork-only `.cpp` files under `tests/` (programs, fixtures and stubs), copied as
   `PORT-MANIFEST-TESTS.txt`. Copying their build blocks wholesale is wrong: the blocks name different
   libraries, and some link with
   `-Wl,--wrap=mmap,mlock,munmap,munlock` (plus `--wrap=pread`) with a `STRATA_RAM_POOL_TEST` define - the
   shims *are* the test, since they answer the syscalls and count them. So each was transplanted with its
   own fork block read, into `engine/cmake/strata_lane_tests.cmake` (included from `CMakeLists.txt` under
   `STRATA_BUILD_TESTS`). `tools-port/lane-test-blocks.py` prints, for every fork test target, the sources
   it compiles and whether this tree has them, its libraries / link options / defines, the option that
   guards it, and whether this tree already builds that name - which matters because a duplicate
   `add_executable` fails the whole configure (CMP0002), not just that program.

   `engine/cmake/strata_lane_tests.cmake` now registers **34 of the lane's test programs**, 25 of them
   added in this pass. With the two opt-ins ON, 33 compile and link (`dflash_head_bf16_test` does not -
   item 7); those 33 make **43 ctest cases** (`native_mlock_test` runs as ten, `ram_pool_test` as two),
   and **39 passed, 2 skipped themselves for want of a device, 2 failed**.
   The exceptions, with the reason each:

   * `dflash_head_bf16_test` - wired but `EXCLUDE_FROM_ALL`; it does not compile, for a reason that is a
     defect in the **fork**, not in this port (item 7).
   * `dflash_taps_test`, `dflash_rope_parity`, `dflash_load_test`, `dflash_inco_parity` - these compile
     the lane's device-side tap kernels (`src/kernels/cuda/dflash_inco.cu`), and this tree compiles that
     only under `STRATA_ENABLE_DFLASH2`, which is OFF here because the serve path that drives it is in the
     two un-carried files (item 1). The fork reaches them because the lane's runtime is always in its build.
   * the `ds4_*` rule tests (7 programs) - the fork keeps them inside `STRATA_BUILD_DSV41`, and a gfx906
     build of this port keeps that guard off, as the fork's build on this card does. They are ordinary
     host programs that need no DSV41 kernel, so they could be moved out of that guard - a fork-side
     choice, not a port change, so it is left to the operator (Open decisions).
   * the CUDA MMQ tests (`tests/cuda/prefill_mmq_*.cpp`, `tests/hip/prefill_mmq_parity.cpp`) - they test
     the MMQ libraries this port does not wire (item 4).
   * `conversation_snapshot_test`, `conversation_validation_test`, `conversation_transfer_test`,
     `pool_plan_test`, `window_rule_test` - **never owed**: upstream v0.2.18 wires all five itself
     (`cmake/strata_gpu_engine.cmake:485-520` and `cmake/backends/cpu.cmake`), block for block the same as
     the fork's. Adding any of them a second time fails the configure on the duplicate target name, which
     is how this was found - the earlier note here claimed those three were owed, and it was wrong.
4. **The CUDA-only MMQ libraries are not wired** (`strata_mmq`, `strata_mmq_w4a4`, `strata_kq`: the
   `mmq_*.cu`, `moe_mmq.cu`, `moe_ws*.cu`, `ggml_cuda_host.cu` sources). The fork does not build them for
   a HIP card either (`moe_mmq.cu` drops its symbols when `GGML_USE_HIP` is defined), so the gfx906 engine
   is complete without them. On a CUDA build of this port they would be missing.
5. **The expert helper binary was not built here.** The target is wired (see below) and refused under
   `STRATA_HIP_GFX906` with a named reason, exactly as the fork's build does, because the sidecar is
   CUDA-only. It also could not be built in a CUDA configure on this box: `nvcc` 13.1 fails on this
   machine's own headers - `rsqrt` and `rsqrtf` declared in `/usr/include/x86_64-linux-gnu/bits/mathcalls.h`
   (glibc 2.43) clash with CUDA's `crt/math_functions.h`, so even a one-line `.cu` does not compile
   (2026-11-22; no CUDA toolkit other than 13.1 is installed). This is a machine problem, not a port
   problem, and it is worth reporting to whoever maintains the CUDA lane on this box.
6. **Four `engine/sycl/` files were returned to upstream's version.** An earlier merge script of this port
   had edited them by accident (it inserted an `import os` line into the comments of
   `engine/sycl/tools/gpustat.py` and `engine/sycl/tools/migrate.sh`, and touched two of its sources).
   SYCL is not part of this port; upstream's files now stand untouched.
7. **One fork program does not build, and the cause is a defect in the fork, found by this port.**
   `engine/tests/core/cuda_runtime.h` - a 37-line shim added in the fork's `7baf1501c3` on 2026-11-19 so
   the CPU-only `native_mlock_test` can parse `include/strata/core/pin_budget.hpp`, which includes
   `<cuda_runtime.h>` unconditionally - sits in the same directory as `gguf_fixture.hpp`, and the fork's
   blocks for `dflash_head_bf16_test` and `dflash_tap_collect_test` put that directory on the include
   path. A `-I` directory is searched before an `-isystem` one, so the shim shadows
   `include/strata/platform/hip_compat/cuda_runtime.h` - the header that maps the CUDA runtime names onto
   HIP on this card - and `cudaMalloc`, `cudaStream_t` and `cudaMemcpy` are never declared: 37 errors.
   Measured on the fork's tree with the fork's own include order: 37 errors with `-I tests/core`, 0
   without it. And the shim, not this port, is what broke it: the fork's own last gfx906 build of that
   file (`build-mixed-gpu/rpcw-hip-fix16b`, object dated 2026-11-13) records
   `hip_compat/cuda_runtime.h` in its dep file, six days before the shim landed. It is not visible in the
   fork's lane builds because the lane builds the engine, not the test suite.
   *What this port does about it:* `dflash_tap_collect_test` drops `tests/core` from its include path -
   provably nothing to that translation unit (its includes are `strata/core/dflash_tap_collect.hpp`,
   `strata/core/on_device.hpp` and `<cuda_runtime.h>`, and the fork's own gfx906 object lists no file from
   `tests/core`). `dflash_head_bf16_test` genuinely needs `gguf_fixture.hpp` from there, so it is wired as
   the fork wires it and marked `EXCLUDE_FROM_ALL`: the default build stays green, `ninja
   dflash_head_bf16_test` fails visibly, and the two possible repairs - move the shim out of `tests/core`,
   or keep `tests/core` off those two blocks - are the fork maintainer's call, not one to make inside a
   port branch.
8. **Two of upstream v0.2.18's own test targets do not link on a gfx906 configure: `native_moe_gated_test`
   and `prefill_decode_golden`.** Both come from upstream (`cmake/strata_gpu_engine.cmake:1352` and
   `cmake/backends/cuda.cmake:135`) over upstream-only sources (`tests/cuda/native_moe_gated_test.cu`,
   `tests/cuda/prefill_decode_golden.cu`), neither is in the fork, and this port changes nothing near
   them. Their link lines carry no object at all, so the link fails with `undefined symbol: main`: on this
   configure no `.cu` under `tests/cuda/` gets a compile rule (the engine's own `.cu` sources do).
   *Proved inherited, not caused here:* clean tag `v0.2.18` was checked out into a scratch worktree and
   configured on this same card with the same options - its `build.ninja` shows the same two link lines
   with no objects and the same zero rules for `tests/cuda/*.cu` (scratch tree and its build log are
   deleted; this port's build log is `logs-port/default-all-build-1.log`). Consequence for the default
   build claim: `ninja` reaches 566 of 566 targets and fails on these two, and would fail the same way on
   the unmodified tag.

## Deliberate drops

* The fork's stop-race guard and its per-request `_last_done` tracking were **not** re-added to the
  server: upstream 0.2.18 fixed both its own way (the `proc0` guard from #5573, and `_RequestOwned`).
* `STRATA_MTP_BATCH_F16`: upstream 0.2.18 changed the default from the MMQ path to FP16. Upstream's
  default was kept. Anyone comparing a decode number against the fork's production build should know the
  default differs.
* `include/strata/kernels/iq_kernels.hpp` upstream declares `native_expert_i8_*` (a path the fork never
  had); the fork's header declares `native_expert_grouped_q8k` and `native_expert_q8k_supported`. Both
  are now present: upstream's file plus the fork's two declarations.

## Build changes this port makes to upstream's CMake (all default-preserving)

* `STRATA_HAVE_DFLASH2` (upstream's DFlash2 drafter, `src/core/dflash2.cpp` + `src/kernels/cuda/dflash2.cu`)
  was an unconditional definition in v0.2.18. It is an option now, **default ON**, so a default build
  compiles exactly what v0.2.18 compiled. The two sources move into it with `target_sources`, as the
  fork's build does.
* `STRATA_ENABLE_DFLASH2` (the lane's own drafter, `src/kernels/cuda/dflash_inco.cu`,
  `src/core/dflash.{cpp,hpp}`) is a new option, **default OFF**. Turning both ON is refused at configure
  time by name, because a verifier may have exactly one tap producer. Every configure prints
  `DFlash2 runtimes compiled: ...` so the built runtime is never guessed from a flag file.
* `STRATA_BUILD_EXPERT_HELPER` (default OFF) builds the CUDA-only `strata-expert-helper` sidecar; refused
  under `STRATA_HIP_GFX906` with a reason.
* The 14 lane sources the engine needs went into `strata_core` (the RAM arena and the CPU expert pool on
  it) and `strata_engine` (helper client, fallback and eligibility, per-card memory guard, conversation
  cache stream state and disk tier, the lane's drafter, and the helper's frame transport). Before that the
  link of `strata` failed with 20 unresolved names; `tools-port/cmake-lists.py` compares a target's source
  list between the fork's `CMakeLists.txt` and upstream's `engine/cmake/strata_gpu_engine.cmake`.
* `cmake/strata_lane_tests.cmake` is new, included from `CMakeLists.txt` inside the existing
  `if(STRATA_BUILD_TESTS)` - it adds targets, it changes nothing that was already built. Inside it the
  fork's own two opt-ins are kept as the fork has them: `STRATA_BUILD_RAM_POOL_TEST` (**default OFF**, so
  the RAM pool group and its `--wrap` link blocks are compiled only when asked for) and the already
  existing `STRATA_BUILD_CONVERSATION_TESTS` (**default OFF**), which upstream uses for its own
  conversation tests and the fork extends with two more programs. A default configure compiles none of
  those six. One line is this tree's: `find_package(Threads QUIET)`, because here the thread package is
  found under the CPU backends and the conversation block, and the RAM pool blocks link it.

## Configuring on this box (2 x Radeon VII, gfx906)

`sh tools-port/configure-906.sh build-906` - and read its header comment. Two pins are needed and are not
in `AGENTS.md`'s recipe; both are environment adaptations, not port changes:

* `--gcc-install-dir=/usr/lib/gcc/x86_64-linux-gnu/15` - clang 17 picks the newest libstdc++ it finds and
  this box has a stray `/usr/include/c++/16` tree with no matching compiler; libstdc++ 16 does not build
  under clang 17.
* `-isystem /opt/rocm-6.1.0/include` - the distro package `libamdhip64-dev` put a newer ROCm's headers in
  `/usr/include/hip`, and they want `__builtin_amdgcn_wavefrontsize`, which clang 17 does not have.

The source directory is `engine/`, not the tree root (v0.2.18 moved the engine build).

## Verification

Run:

* `sh tools-port/configure-906.sh build-906` - clean configure; prints
  `DFlash2 runtimes compiled: upstream (src/core/dflash2.cpp, src/kernels/cuda/dflash2.cu)`.
* `ninja -C build-906 strata` - succeeds. Engine `build-906/strata`, 29.2 MB.
* `ninja -C build-906` (the whole default build, lane options OFF) - 566 targets, and it stops on two of
  **upstream's**: `native_moe_gated_test` and `prefill_decode_golden`, item 8. Everything else, including
  every target this port adds, builds and links (`logs-port/default-all-build-1.log`).
* **The engine's hash is not a content hash.** v0.2.18 stamps the build with the commit it was
  configured from (`git rev-parse --short=9 HEAD`, run at configure time in `cmake/strata_gpu_engine.cmake`),
  so committing and reconfiguring changes the bytes of every binary with no source change. Quote a build
  as the pair (commit, sha256). This port's builds: at `43ff7f30b` the engine was
  `36f8a503b79c19fc66cf88bd6aa3eb6c54e0a61f35d85fb259791bad2aad2848`, then
  `aa3c67a3154c93a33a5466ebf26e0d719a86af54da23876f76b96627c4499375`; reconfigured and rebuilt at
  `84d066665` it is `7d6873e5e435960399dbd5853e62e4e9feb8056ce20a17c5dd0f0f6877a8035c`; at `520532ca5`
  `40c5018813740097a66a1042f4457fea4a185fdae5b587a627c6988c6b7ccdf4`; at `ac71c8dab` (the lane's test
  programs wired, default options) `b634e1471017005c4ff5fe5657487cdd43663675c47bbe41e44dde8a7fa2c076`.
  `strings` finds the stamped commit inside the binary.
* `ldd build-906/strata` with `LD_LIBRARY_PATH=/home/srcds/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib`:
  `libhipblas.so.2` and `libamdhip64.so.6` from `/opt/rocm-6.1.0/lib`, `librocblas.so.4` from
  `/home/srcds/rocm-gfx906-xnack/lib` - what `AGENTS.md` says a correct gfx906 link looks like.
* The lane's own tests (`logs-port/lane-tests-build-3.log`, `logs-port/lane-tests-run-3.log`,
  `logs-port/lane-tests-fail-1.log`; `logs-port/` is gitignored by upstream's `*.log` rule, so these are
  on this box, not in the branch). Configure as for the engine, plus the two opt-ins ON
  (`-DSTRATA_BUILD_RAM_POOL_TEST=ON -DSTRATA_BUILD_CONVERSATION_TESTS=ON`), and every program run under
  `HIP_VISIBLE_DEVICES=-1 CUDA_VISIBLE_DEVICES=-1` so nothing can touch the cards the serving engine
  holds: **43 test cases - 39 passed, 2 skipped by the test itself for want of a device, 2 failed.** The
  two failures are the static text contracts on `src/program/generate.cpp`, the un-carried file (item 1);
  nothing failed that indicates a wrong algorithm or a bad merge.

  | Program | Result | Program | Result |
  | --- | --- | --- | --- |
  | `vram_guard_test` | passed | `ram_pool_test_mock` | passed |
  | `expert_helper_reserve_test` | passed | `ram_pool_test_real_mock` | passed |
  | `expert_helper_arena_cap_test` | passed | `ram_pool_nonlinux_probe` | passed |
  | `arena_pin_ranges_test` | passed | `pool_expert_cache_test` | passed |
  | `conv_cache_knobs_test` | passed | `pool_expert_store_test` | passed |
  | `swapin_depth_test` | passed | `pool_expert_source_adapter_test` | passed |
  | `tail_skip_guard_test` | passed | `bounded_direct_reader_test` | passed |
  | `read_once_test` | passed | `native_mlock_test_*` (10 cases) | passed |
  | `pipeline_chunks_test` | passed | `native_mlock_nonlinux_probe` | passed |
  | `cache_spill_test` | passed | `expert_helper_socket_test` | passed |
  | `dflash_tap_plan_test` | passed | `expert_helper_f32_window_test` | passed |
  | `dflash_policy_test` | passed | `expert_helper_fallback_test` | passed |
  | `dflash_ring_test` | passed | `expert_helper_ownership_test` | passed |
  | `conversation_disk_cache_test` | passed | `helper_microbench` | passed |
  | `dflash_tap_slot_test` | passed | `p2_measure_test` | passed |
  | `expert_helper_candidates_test` | **failed** (below) | `dflash_refusal_test` | **failed** (below) |
  | `dflash_tap_collect_test` | skipped - no device | `dflash_owned_kv_test` | skipped - no device |

  `expert_helper_candidates_test`: its six executed sections - 100 checks over the aligned slot arithmetic,
  the eligibility window, the owner and Hadamard exclusions, the byte budget and its strict CLI parsing,
  and the `STRATA_DISJOINT_ADAPT` precedence - pass, and its 14 checks on the helper's own source pass. Its
  seventh section is a *static text contract*, and 12 of its checks look in `src/program/generate.cpp` for
  the calls that hand rows to the helper (the candidate builder, the budget planner, the MiB-to-bytes
  conversion, the strict `--expert-helper-mib` parse, the socket opt-in). That file is still upstream's, so
  the calls are not there.

  `dflash_refusal_test`: 96 of 112 checks pass - 64 of them call the production predicate, classification
  and host handoff in `dflash_refusal.hpp`, and 19 walk the latch model. The 16 failures are all
  `STATIC` source-text pins on the serve and CLI call sites in `generate.cpp` (the request-scope latch, the
  recovery branch that latches and restores `drafted`, the `ctx_ok` short-circuit, the shared
  `dflash_refusal_is_fatal()` classification). The test prints its own evidence split and labels those
  checks "NOT executed behaviour", so the result is: the rules are right, the call sites are missing.

  Both failures therefore say the same thing as item 1, in the two programs that check for it - which makes
  them the acceptance test for porting `generate.cpp`: `expert_helper_candidates_test`'s 12 and
  `dflash_refusal_test`'s 16 are the count of what that file still owes.
* The server's own tests (`terminal/serve`): 94 passed, 6 failed; the same 6 fail on clean upstream
  (`test_dflash_engine_args.py` mocks a name that no longer exists upstream), so the port adds no failure.
* Not run: any boot, any model, any GPU work, any bench. Those are the operator's windows. Two lane
  programs (`dflash_tap_collect_test`, `dflash_owned_kv_test`) would run on a device if one were visible;
  under `HIP_VISIBLE_DEVICES=-1` they exit with the fork's own skip code 77, which is why they are recorded
  as skipped rather than passed.

## Open decisions for the operator

1. `prefill.cpp` and `generate.cpp`: port the fork's pieces by hand against upstream's rewritten dispatch
   loop (the list with its code is in `PORT-WORKLIST-UNPORTED.md`: 15 expert-helper, 6 CPU-pool and 6
   VRAM-clamp hunks in `generate.cpp`, 6 expert-helper in `prefill.cpp`), or accept a port without helper
   serving. It needs a boot to prove, so it is a joint window, not a Fixer job. Two lane tests now say
   when it is done, without any extra work: `expert_helper_candidates_test` (12 static checks) and
   `dflash_refusal_test` (16) fail today on exactly the `generate.cpp` call sites that are missing, and
   they go green as the hunks land.
2. Wire the DeepSeek V4.1 engine target, or record the family as out of this port. *The target is wired
   (item 2 of "What is NOT carried") and a gfx906 build compiles none of it; the decision left is whether
   to open a CUDA configure to build and run it, which this box cannot do today (item 3 below).*
3. Fix or replace this box's CUDA 13.1 toolkit so the expert helper can be built here at all.
4. Two upstream v0.2.18 test targets do not link on a gfx906 configure (`native_moe_gated_test`,
   `prefill_decode_golden` - item 8, reproduced on the clean tag). They make `ninja` fail on both the port
   and the untouched tag. Decide whether the port should carry a local guard that skips them on this card
   (a build change this port does not want to make silently), or whether this goes to the upstream
   maintainers as a bug report - the evidence for the second is already written.
5. The fork's `tests/core/cuda_runtime.h` shadows the HIP compatibility header for every test target whose
   include path contains `tests/core` (item 7). One lane program (`dflash_head_bf16_test`) does not compile
   because of it and is `EXCLUDE_FROM_ALL` here. Decide the repair in the fork - move the shim out of
   `tests/core`, or take that directory off the two `dflash` blocks - and this port will follow it.
6. The seven `ds4_*` rule tests are pure host programs that the fork happens to keep inside
   `STRATA_BUILD_DSV41`, so a gfx906 build never builds them. Moving them out of that guard would give this
   card their coverage; it changes which features a given configure compiles, so it is not this port's call.
