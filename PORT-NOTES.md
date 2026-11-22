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

1. **`engine/src/prefill/prefill.cpp` and `engine/src/program/generate.cpp` were left as upstream
   v0.2.18's from 2026-11-22 until the same day's evening.** They are no longer: both files are now a
   three-way merge that carries the lane's behaviour beside upstream's, and the section
   "The decode loop is merged" below replaces this item. The reason the first pass left them - and the
   measurement behind it - is kept there, because it explains why the merge is a merge and not a patch.
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
   and **39 passed, 2 skipped themselves for want of a device, 2 failed** (that pass's numbers; see the note above).
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

> The runs in this section are the ones taken **before** the decode loop was merged. They are kept
> as measured. The current numbers - lane suite 47 cases, 100% passed, both DFlash2 pairs - are in
> "The decode loop is merged" below, and the two failures named here are closed.

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
  programs wired, default options) `b634e1471017005c4ff5fe5657487cdd43663675c47bbe41e44dde8a7fa2c076`;
  at `e5651d66b` (the state this branch rests in: default options, everything above wired)
  `4149966aea6c100a5dbbcd3486eb408008b4bbbc7a2bb05af537e36e55a46a9e`.
  `strings` finds the stamped commit inside the binary.
* `ldd build-906/strata` with `LD_LIBRARY_PATH=/home/srcds/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib`:
  `libhipblas.so.2` and `libamdhip64.so.6` from `/opt/rocm-6.1.0/lib`, `librocblas.so.4` from
  `/home/srcds/rocm-gfx906-xnack/lib` - what `AGENTS.md` says a correct gfx906 link looks like.
* The lane's own tests - reproduce with `sh tools-port/lane-tests.sh build-906` (it configures the two
  opt-ins, builds the lane's programs with `-k 0`, and runs them under `HIP_VISIBLE_DEVICES=-1`). Recorded
  run (`logs-port/lane-tests-build-3.log`, `logs-port/lane-tests-run-3.log`,
  `logs-port/lane-tests-fail-1.log`; `logs-port/` is gitignored by upstream's `*.log` rule, so these are
  on this box, not in the branch). Nothing in the run may touch the cards the serving engine holds, so
  every program runs with both `HIP_VISIBLE_DEVICES=-1` and `CUDA_VISIBLE_DEVICES=-1` set. The result:
  **43 test cases - 39 passed, 2 skipped by the test itself for want of a device, 2 failed** (this pass's numbers; see the note above). The
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
  `dflash_refusal_test`'s 16 are the count of what that file still owes. **They went green when the file
  was merged (2026-11-22, same day) - that is the closure of this item, not a still-open count.**
* The server's own tests (`terminal/serve`): 94 passed, 6 failed; the same 6 fail on clean upstream
  (`test_dflash_engine_args.py` mocks a name that no longer exists upstream), so the port adds no failure.
* Not run: any boot, any model, any GPU work, any bench. Those are the operator's windows. Two lane
  programs (`dflash_tap_collect_test`, `dflash_owned_kv_test`) would run on a device if one were visible;
  under `HIP_VISIBLE_DEVICES=-1` they exit with the fork's own skip code 77, which is why they are recorded
  as skipped rather than passed.

## The decode loop is merged (2026-11-22): `generate.cpp` and `prefill.cpp`

Both files now carry the lane's behaviour and upstream's, in one file each. The fork's content is not
pasted over upstream's and upstream's is not trimmed to make room: each file is a three-way merge of

  * **ours** - upstream v0.2.18's file,
  * **base** - the same file at the fork's branch point `54828c7efb` (21139 lines),
  * **theirs** - the fork's file at `564e1dbbea`,

run with `git merge-file --diff3`. That is the only method that could tell "the fork changed this line"
apart from "upstream wrote this line after the fork left", and it is the reason `git merge-base` of the
two trees (also `54828c7efb`, pre-layout-split) was finally used as the file's base rather than ignored.

| file | upstream's | the fork's | merged | conflicts |
| --- | --- | --- | --- | --- |
| `engine/src/program/generate.cpp` | 24627 | 26165 | 29633 | 41 |
| `engine/src/prefill/prefill.cpp` | 9549 | 8787 | 10525 | 16 |

### Why the hunk patch was thrown away

The first attempt applied the fork's hunks onto upstream's file with `patch(1)`, filtered by feature. It
applied - and it was wrong. Applied with fuzz, the lane's own hunks deleted upstream code the fork never
had: `BSlot`'s members (`hold`, `w_n`, `w_lk`, `lk_n`, `lk_m`, `lk_at`, `caps`, `rounds`, `t_round`,
`drafts_off`, `drafts_acc`), five of upstream's includes (`prefill/equal_cut_rule.hpp`,
`prefill/fit_out_rule.hpp`, `prefill/loan_rules.hpp`, `program/bdone_drafts.hpp`,
`program/reply_branch.hpp`), the park-pool block, the SSD conversation tier and the `STRATA_FOLLOW`
token-forcing block. The file got smaller in exactly the places upstream had grown. Nothing but the
compiler caught it, and only some of it. So the method changed, and everything the patch had done was
discarded (the 33 add-only hunks it applied safely are inside the merged result too - the merge produced
the same content there).

### How the 57 conflicts were resolved

Sixteen in `generate.cpp` and six in `prefill.cpp` were plain add/add (both sides wrote new lines in the
same place); both sides were kept. The rest were read. Where both sides changed the same line, upstream's
newer decision wins and the lane's behaviour is added beside it, not instead of it:

| the line both sides wrote | what stands |
| --- | --- |
| `STRATA_ROUTE_TAIL_SKIP` default (fork: rank 7 on CUDA/gfx1200; upstream: off, owner decision 2026-11-19) | upstream's - the fork's is the older default |
| `STRATA_MTP_BATCH_F16` default (fork: MMQ unless `=1`; upstream: FP16 unless `=0`) | upstream's |
| the conversation spill tier (fork: `--conversation-cache-dir` DISK tier; upstream: the SSD tier) | **both**, side by side: upstream's SSD tier, plus the fork's disk tier with its `disk_start` / `disk_wins` lookup restored and its own condition. The two are now asked the same question; the RAM entry is only taken when neither wins. Interaction untested - see the risks below. **Decided 2026-11-22 (operator): in configs the spill tier is OFF (`--conversation-spill-mib 0`), the fork's DISK tier stays this rig's only disk tier** - journal/pool-admission-fix-2026-11-21.md (fork tree), `configs-port/trial-8019-spill-off.json` |
| the free-VRAM note, the GPU-pause lines, the `kfd_quiet` warm-up, `kv_grow_start` / `kv_grow_target`, `lm_streamed_experts`, `serve_stop_bound` with the GEN hold, `mmq_resident_sort_eligible`, `g_gpu_pause_idle`, the `READY ... stop hold holdb` protocol line | upstream's, unchanged - the lane's block is inserted around them (the register-last arena pin now runs just before upstream's READY print) |
| the elastic-KV ceiling (fork adds the drafter's scratch chunks; upstream wraps the rule in `kv_grow_*`) | upstream's rule, plus the drafter's terms guarded by `!o.dflash.empty()` so a run without `--dflash` asks the same question as before |
| the drafter latch in the decode condition (fork `!(draft_on || (has_dflash && req_dflash && dflash_ready))`, upstream `!draft_on`) | the fork's condition - the lane's drafter needs it |
| the CPU expert share's blob test (fork: `!transient`; upstream: `pinned || !transient`) | **the fork's `!transient`** - see the one default-path change below. **Ratified 2026-11-22 (operator): kept** - journal/pool-admission-fix-2026-11-21.md (fork tree) |

The merged `generate.cpp` parses the lane's options again: `--dflash`, `--dflash-head`,
`--dflash-vocab`, `--dflash-mask-token`, `--dflash-block`, `--dflash-remote-only`,
`--prompt-window-retry`, `--conversation-cache-dir`, `--conversation-cache-disk-mib`,
`--conversation-cache-disk-ram-mib`, `--conversation-cache-disk-io`, `--conversation-cache-ram-mib`.
They were not in upstream 0.2.18 at all, so this is new CLI surface, all of it inert until asked for.

### The one thing that changes the default path

`prefill.cpp`'s CPU-share test: upstream admits a blob the CPU can read from RAM when
`pinned || !transient`; the merged file admits it when `!transient` (the lane's CS-T rule,
`prefill.cpp:6466`). The difference is a source that is BOTH mlocked and assembles on demand - which is
what the managed RAM pool answers - and upstream has no such source today, so today the two tests pick
the same experts. It is kept because the lane's managed pool would otherwise hand a pool loan to a share
that holds it for a whole layer. Recorded here because it is the only default-path behaviour this merge
changes.

### The four DFlash programs, and two corrections that came with them

`dflash_taps_test`, `dflash_rope_parity`, `dflash_inco_parity` and `dflash_load_test` are wired in
`engine/cmake/strata_lane_tests.cmake` under `if(STRATA_ENABLE_DFLASH2)` - the fork's own blocks, which
are the first place this port had to fix two things the fork assumes:

1. `_strata_gpu_runtime_target` and `_strata_gpu_include_directories` come out of this box's configure
   as `CUDA::cudart` and `include/strata/platform/hip_compat`, while the engine this box builds is the
   HIP backend. `CUDA::cudart` carries no include directory, so `dflash.cpp`'s `#include
   <cuda_runtime.h>` found `tests/core`'s CPU-only stub, which has no `cudaStream_t`. The lane's blocks
   resolve to `strata_hip_runtime` and `include/strata/hip_compat` - the pair `strata_kernels` itself
   compiles with.
2. `tests/core/gguf_fixture.hpp` could not write what `dflash_load_test` asks for: GGUF types 10 / 11 /
   12 were missing, and its array branch hardcoded `elem = 4` while the test writes an F32 array
   (`elem = 6`). Both widened so the caller states the type; every caller that worked before writes the
   same bytes. The throw now names the type it refused.

`tools-port/lane-tests.sh` takes `PAIR=lane` now: it configures the pair this box serves with and adds
those four to the case set. Without it the script still configures upstream's pair and asks for the same
32 programs as before, because the four are not built there.

### Verification, both pairs, this box (2 x Radeon VII, gfx906), no device touched

| | the lane's pair | the default pair |
| --- | --- | --- |
| configure prints | `DFlash2 runtimes compiled: the serving lane's (dflash_inco: src/kernels/cuda/dflash_inco.cu, include/strata/kernels/dflash_inco.hpp)` | `DFlash2 runtimes compiled: upstream (src/core/dflash2.cpp, src/kernels/cuda/dflash2.cu)` |
| `strata` compiles and links | yes | yes |
| sha256 | `936efcc798aab3952551281f2f328c1b8b19311fee9d0298142e702809a58394` (30555416 B) | `17f643054bf42e44714658971b2a1fe9b0855efbc9c0ef37b5ff8012df5065a7` (31528984 B) |
| `ldd` (with the launcher's `LD_LIBRARY_PATH`) | `libhipblas.so.2` and `libamdhip64.so.6` -> `/opt/rocm-6.1.0/lib`, `librocblas.so.4` -> `/home/srcds/rocm-gfx906-xnack/lib` | same three sonames, same three paths |
| lane suite, devices hidden | **47 cases, 100% passed, 0 failed**, 5 SKIPPED at exit 77 (`dflash_tap_collect_test`, `dflash_owned_kv_test`, `dflash_inco_parity`, `dflash_taps_test`, `dflash_rope_parity`) | **43 cases, 100% passed, 0 failed**, 2 SKIPPED at exit 77 |
| the suite's build step | nothing failed to build | nothing failed to build |

Before this merge the same suite was 43 cases with 2 failures: `expert_helper_candidates_test` (its 12
static checks on `generate.cpp`) and `dflash_refusal_test` (16). Both pass now, and 4 programs joined the
run. The 5 skips are the honest kind: they need a device, they exit 77, and the box hides its devices
from them (`HIP_VISIBLE_DEVICES=-1 CUDA_VISIBLE_DEVICES=-1`) so they cannot take VRAM from the engine
this box serves from.

Both binaries above were configured and built at commit `f4f024749`, and a sha256 of a `strata`
binary is not a content hash on this engine: `v0.2.18` stamps `git rev-parse --short=9 HEAD` into it at
configure time, so the same source hashes differently after any commit. Quote a build as (commit,
sha256), never as a hash alone. The two earlier hashes of this session - `a37933fa9c...` and
`fde1f5ff49...` - are the same source at earlier commits, kept here so the logs still read true.

Logs: `logs-port/lane-tests-run.log` (the lane pair, `PAIR=lane`) and the same path after the default
run, with `logs-port/configure-lane-pair-2.log` and `logs-port/configure-default-2.log` beside them.

### The default-pair engine after the guard trio (2026-11-22)

Reconfigured with `tools-port/configure-906.sh` (the resting pair, `STRATA_ENABLE_DFLASH2=OFF` /
`STRATA_HAVE_DFLASH2=ON`) and rebuilt at the branch tip, after the three gfx906 guard commits
(`de422ee1f5` / `d63ff871f1` / `ef61c81bd8`) and the config/notes commits:

| | |
| --- | --- |
| commit | `4fde0cf2b6` |
| sha256 | `572284db0f107cb470dfaf0a76a5f592fb1a9bf81e84c23d84e84a7e716331ee` (31528984 B, `ninja -C build-906 strata` exit 0; the resting build after the lane suite re-run, cache restored with the opt-ins OFF) |
| `ldd` (launcher's `LD_LIBRARY_PATH`) | `libhipblas.so.2` and `libamdhip64.so.6` -> `/opt/rocm-6.1.0/lib`, `librocblas.so.4` -> `/home/srcds/rocm-gfx906-xnack/lib`; nothing `not found` |
| lane suite after the guards | default pair **43/43 passed, 0 failed** (2 honest device skips); `PAIR=lane` **47/47 passed, 0 failed** (5 honest device skips) - `logs-port/lane-suite-default-3.log`, `logs-port/lane-suite-lane-3.log` |

A correction to the paragraph above it: the `strata` binary DOES carry a configure-time git stamp - through the vendored ggml: `engine/third_party/strata-ggml/ggml/CMakeLists.txt` runs
`git rev-parse --short HEAD` (this repo abbreviates to 10 hex) and writes it into
`build-906/ggml/src/ggml-version.h` as `GGML_COMMIT`, compiled into `ggml.c` (verified in
`build-906/strata` at file offset 380498; oracle review 2026-11-22). The `DS4_BUILD_ID` stamp is separate
and lands in `strata-dsv41` alone (never built on gfx906). `--version` prints `strata 0.1.64`.

The two shas recorded above differ for that reason, not link nondeterminism: the mid-sequence build
(sha `e6ce1720f4`, recorded in `3e83be2b71`'s own tree, 23:12:18) was configured at HEAD `4fde0cf2b6`
(stamp `4fde0cf2b6`); the resting binary's configure ran 16 s after commit `3e83be2b71`
(`ggml-version.h` 23:12:34, stamp `3e83be2b71`), and the 23:15:44 restore kept it (content-identical,
not rewritten). Engine SOURCE is identical across
`4fde0cf2b6`/`3e83be2b71`/`e0d35839f6` (only this file differs), so (commit, sha) provenance stands - but
the diagnostic for any future same-source-different-sha question is the `GGML_COMMIT` string: read it
before blaming the linker. Configure-time commit of the resting binary: `3e83be2b71`.

### What is NOT verified

**The merged decode loop has never run.** No boot, no prompt, no GPU work went into this - it is static
analysis and a compiler. Specifically unproven:

* that the merged loop answers like the fork's build (the boot-and-compare on :8019 is still the next
  step, and it is the only thing that can show it);
* the two conversation tiers living together (SSD and DISK) - each was tested alone, never as a pair (config-side this rig never asks the pair now: the port's 8019 base runs `--conversation-spill-mib 0`, so the spill tier is off and the DISK tier is the only one; the engine path itself stays untested);
* that upstream's `--batch`, GEN `hold`, the checkpoint pool and the piped-lookup path still behave,
  now that the fork's decode-loop hunks sit among them (they compile, and the static pins hold);
* the lane's `--dflash` request path, which upstream 0.2.18 does not contain at all, so nothing of it is
  covered by an upstream test - the three device-parity programs skip without a device.


## The port's own :8019 base config (2026-11-22, commit `07a407d1a8`)

`configs-port/trial-8019-spill-off.json`: the champion measurement config
(`bench/ab-v0210/rpcprof/trial-managed-pool-38g-230k.json`, named by the fork's `CHAMPION.json`) with the
engine, cwd, helper sockets, log and the WRITTEN `--conversation-cache-dir` repointed at this tree, port
8019, and `--conversation-spill-mib 0` added (the operator's spill-tier decision above). The engine gate
that honours the 0 is `generate.cpp:15259` - the whole spill block runs only when `conversation_spill_mib
!= 0`, so 0 turns it off without touching the DISK tier.

**Key reach, verified device-free:** the ported server's arg path is `serve.engine_args` (`server.py:3589`,
consumed at `:8151` by `StrataEngine(exe, engine_args(cfg, exe) + ...)`). Called directly in Python with
this config (`announce=False`, `exe=None`, so nothing starts and nothing probes a device): all **9 of 9**
`--conversation-cache-*` keys of the base reach the built engine argv unchanged, `--conversation-spill-mib
0` lands in it, and the shaping layers (`user_args.merge`, `drafting.apply`, `vram_fit.fit_start`) add
their own flags without dropping ours. The server also parses the engine's conversation-cache log lines
(`server.py:631-639`), so the tier's own emissions are expected surface.

## The expert helper builds: `strata-expert-helper` from the CUDA tree (2026-11-22, commit `8dd40c9a98`)

The sidecar that owns the RTX 3080 in this box's mixed-GPU lane. `engine/cmake/strata_gpu_engine.cmake:610`
refuses it under `STRATA_HIP_GFX906` by design ("the sidecar is CUDA-only (NVIDIA). Build it in a CUDA
tree; the HIP engine runs its proxy client unchanged"), so it was never buildable in `build-906` or
`build-lane`. It now builds in its own tree.

**A separate configure, scripted:** `tools-port/configure-cuda-helper.sh` (new), run as

```
sh tools-port/configure-cuda-helper.sh build-cuda-helper        # exit 0
ninja -C build-cuda-helper strata-expert-helper                 # exit 0
```

| option | why |
| --- | --- |
| `STRATA_ENABLE_CUDA=ON`, `STRATA_HIP_GFX906=OFF`, `STRATA_ENABLE_HIP=OFF` | the NVIDIA toolchain, no ROCm; nothing here touches the gfx906 engine or its cards |
| `CMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc` (13.1.115), `CMAKE_CUDA_HOST_COMPILER=/usr/bin/g++`, `CMAKE_CXX_COMPILER=/usr/bin/g++` | g++ as nvcc's host, always - clang-as-nvcc-host is separately broken on this box (unrelated to this port) |
| `CMAKE_CUDA_ARCHITECTURES=86` | the RTX 3080. CMake's own CUDA 13 default is sm_75, so it is named |
| `STRATA_NATIVE_EXPERTS=ON` | CMake refuses the helper without it (the helper computes native IQ blobs) |
| `STRATA_BUILD_EXPERT_HELPER=ON` | the opt-in itself |
| `STRATA_ENABLE_DFLASH2=ON`, `STRATA_HAVE_DFLASH2=OFF` | **the lane's pair**, the same one `build-lane` and this box's serving engine use, so the libraries the helper links are configured as they are in the process it talks to. Both ON is a `FATAL_ERROR` by design (one verifier, one tap producer), and neither is not what the lane runs |
| `STRATA_BUILD_TESTS=ON` | **not** for the tests - for ggml. See below |

### The one thing that had to change, and why

The first configure asked for the release shape (`STRATA_BUILD_TESTS=OFF`, Strata's own CPU pool). That
shape makes upstream's own rule drop ggml entirely - `if(STRATA_BUILD_TESTS OR NOT
STRATA_OWN_CPU_QUANT)`, `engine/CMakeLists.txt:681`, printing "Strata: no ggml in this build (own CPU
pool, no MMQ, tests off)" - and the build died three times on `fatal error: ggml.h: No such file or
directory`:

```
src/core/remote_experts.cpp:6                 GGML_TYPE_Q8_K (the Q8_K wire's per-layer pack-format gate)
src/core/expert_helper_socket.cpp:9           the fp16 contract of the q8_1 activation blocks
tools/mixed_gpu/sidecar/helper_main.cpp:62    the same q8_1 contract
```

The CMake rule is upstream's; those three `#include "ggml.h"` lines are not (upstream's copies of these
files include ggml nowhere) - they came with the lane's files. So it is the lane's code that needs
ggml's headers, and the fix is to configure ggml in, exactly as `build-906` and `build-lane` already do
(`STRATA_BUILD_TESTS=ON`). The alternative `-DSTRATA_OWN_CPU_QUANT=OFF` would move the CPU
quantisation path this box serves with, so it was not used. With that, the target compiled with **no
port-side source change at all** - the three sidecar files are byte-identical to the fork's
(`helper_main.cpp`, `helper_frame.cpp`, `helper_microbench.cpp`; `diff -q` clean).

The CUDA precondition was the operator's: `/usr/local/cuda-13.1/targets/x86_64-linux/include/crt/math_functions.h`
patched for glibc 2.43 (`rsqrt` / `rsqrtf` colliding with the C++ standard's names). Checked before
configuring, not assumed: a one-line `.cu` through the real nvcc with `-ccbin /usr/bin/g++` compiled,
exit 0. Before that patch this configure died in `enable_language(CUDA)`'s own compiler-id test (the
half-written `build-cuda/` cache is that earlier attempt).

### The binary

| | |
| --- | --- |
| path | `build-cuda-helper/strata-expert-helper` |
| built at commit | `03600590f` (a `strata`-family hash is not a content hash: the engine stamps `git rev-parse --short=9 HEAD` at configure time) |
| sha256 | `58c3ae020a7479a13c622627544507f5b43f284f9be4b5222ac90c3421b88bb2` |
| size | 55541552 bytes |
| device code | 38 cubins, all `sm_86` (`cuobjdump -lelf`) |
| `NEEDED` | `libcudart.so.13`, `libstdc++`, `libm`, `libgcc_s`, `libc` - no BLAS, no HIP |
| `ldd` | `libcudart.so.13 => /usr/local/cuda/lib64/libcudart.so.13` (that symlink chain is `/usr/local/cuda` -> `/etc/alternatives/cuda` -> `/usr/local/cuda-13.1`) |
| `RUNPATH` | `/usr/local/cuda-13.1/targets/x86_64-linux/lib` - the versioned toolkit path, so the binary follows `cuda-13.1` itself, not the `/etc/alternatives` symlink. Worth knowing if the box's toolkit default is ever repointed |
| `libcuda.so.1` (the driver) | not in `NEEDED` and not in `ldd`. That is normal, not a defect: cudart dlopens the driver on the first CUDA call, so `ldd` cannot show it |

**What was run:** `strata-expert-helper --help` with `CUDA_VISIBLE_DEVICES=-1 HIP_VISIBLE_DEVICES=-1` -
it prints its usage and exits 0. It is the first statement of `main` (`helper_main.cpp:2141`, before
`parse()` and before any CUDA call), so nothing was attached. There is no `--version` or `--info`
switch in this binary; `--help` is the only informational output it has.

**What was NOT done, on purpose:** no socket bind, no attach, no `--pack`, no fill, no window, no
draft, no `--persist`, no device work of any kind, nothing on `:8009`. What is therefore still
unproven: that this binary starts on the 3080, that its fill fits the card's budget with this box's
`--reserve-mib`, and that the `sm_86` device code runs (it was compiled, never executed).

Logs: `logs-port/configure-cuda-helper-1.log` (the release-shape configure that printed "no ggml"),
`logs-port/build-cuda-helper-1.log` (the three `ggml.h` failures),
`logs-port/configure-cuda-helper-2.log` and `logs-port/build-cuda-helper-3.log` (the good pair, both
exit 0).


## Open decisions for the operator

1. ~~`prefill.cpp` and `generate.cpp`: port the fork's pieces by hand ... or accept a port without
   helper serving.~~ **Decided and done 2026-11-22**: the two files are merged (see "The decode loop is
   merged"). Both tests it named are green: `expert_helper_candidates_test` and `dflash_refusal_test`.
   What is still the operator's call is the one thing a merge cannot give - a boot on :8019 to compare
   the merged decode loop against the fork's build, since everything below is static analysis.
2. Wire the DeepSeek V4.1 engine target, or record the family as out of this port. *The target is wired
   (item 2 of "What is NOT carried") and a gfx906 build compiles none of it; the decision left is whether
   to open a CUDA configure to build and run it, which this box cannot do today (item 3 below).*
3. ~~Fix or replace this box's CUDA 13.1 toolkit so the expert helper can be built here at all.~~
   **Decided 2026-11-22**: the operator patched the toolkit's `crt/math_functions.h` for glibc 2.43, and the
   helper builds from this tree - see "The expert helper builds" below (commit `8dd40c9a98`).
4. ~~Two upstream v0.2.18 test targets do not link on a gfx906 configure (`native_moe_gated_test`,
   `prefill_decode_golden` - item 8, reproduced on the clean tag). They make `ninja` fail on both the port
   and the untouched tag. Decide whether the port should carry a local guard that skips them on this card
   (a build change this port does not want to make silently), or whether this goes to the upstream
   maintainers as a bug report - the evidence for the second is already written.~~
   **Decided 2026-11-22 (operator): local guards, no upstream filing** - journal/pool-admission-fix-2026-11-21.md
   (fork tree). Landed as the guard trio `de422ee1f5` / `d63ff871f1` / `ef61c81bd8` (the third target of the
   same class, `conversation_transfer_test`, is gated by `de422ee1f5`); the default `ninja -k 0` build is
   green with all three out of the graph, and no test source was touched.

   **A third target of the same class (measured 2026-11-22, after the decode-loop merge).** With
   `STRATA_BUILD_CONVERSATION_TESTS=ON`, a gfx906 configure also builds upstream's
   `conversation_transfer_test` and it does not compile: `src/core/conversation_validation_test.cpp`
   asks for `cudaErrorInvalidValue` and `cudaErrorUnknown`, which the HIP layer spells
   `hipErrorInvalidValue` / `hipErrorUnknown`. The cause is an option, not the merge: upstream guards
   that target `if(NOT STRATA_ENABLE_HIP AND UNIX ...)` (`cmake/strata_gpu_engine.cmake:511`), and this
   box's documented configure sets `STRATA_HIP_GFX906=ON` without `STRATA_ENABLE_HIP`, which stays
   `OFF` - so a CUDA-only test (it links with `-Wl,--wrap=cudaMemcpy`) is admitted on the HIP backend.
   Proved inherited, not caused here: `conversation_validation_test.cpp` is byte-identical to upstream
   v0.2.18, and the lane suite does not ask for this target (its 43 / 47 cases are unaffected). It
   surfaced only because `tools-port/lane-tests.sh` leaves the conversation opt-in `ON` in the build
   directory's cache. ~~Same decision as item 4: local guard here, or upstream report.~~
   **Decided with item 4 above: local guard** (`de422ee1f5`).
5. The fork's `tests/core/cuda_runtime.h` shadows the HIP compatibility header for every test target whose
   include path contains `tests/core` (item 7). One lane program (`dflash_head_bf16_test`) does not compile
   because of it and is `EXCLUDE_FROM_ALL` here. Decide the repair in the fork - move the shim out of
   `tests/core`, or take that directory off the two `dflash` blocks - and this port will follow it.
6. The seven `ds4_*` rule tests are pure host programs that the fork happens to keep inside
   `STRATA_BUILD_DSV41`, so a gfx906 build never builds them. Moving them out of that guard would give this
   card their coverage; it changes which features a given configure compiles, so it is not this port's call.
