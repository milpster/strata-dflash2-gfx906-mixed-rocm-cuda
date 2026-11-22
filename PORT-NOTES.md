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
| The lane's own test programs | 49 (`PORT-MANIFEST-TESTS.txt`) | copied to `engine/tests/`; `tools-port/lane-tests.py` prints that list from the two trees. Not built yet - their blocks are owed (below) |
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
3. **The lane's own test programs: 9 of 24 are wired and run; 15 are still not in any build target.**
   `PORT-MANIFEST.txt` covers `src/`, `include/`, `sycl/` and `serve/` - the plan's count - so the lane's
   tests under `tests/` were nobody's: 49 files, 24 test programs and their fixtures and stubs, copied
   (`PORT-MANIFEST-TESTS.txt`, `tools-port/lane-tests.py` prints that list again). Wiring them
   mechanically is wrong: each block names different libraries, some link with
   `-Wl,--wrap=mmap,mlock,pread` and a `STRATA_RAM_POOL_TEST` define, so each was transplanted with its
   own block read. `engine/cmake/strata_lane_tests.cmake` (included from `CMakeLists.txt` under
   `STRATA_BUILD_TESTS`) now holds the nine whose fork block links **no library at all** - the memory
   guard, the helper's reserve clamp, its candidate eligibility and budget, its relief-arena cap, the
   arena pin ranges, the conversation-cache knobs, the swap-in depth, the tail-skip gate and the
   read-once prefill window. Results are in Verification. Still owed, block by block: the RAM pool group
   (`pool_expert_cache_test`, `pool_expert_store_test`, `bounded_direct_reader_test`, `ram_pool_test` -
   they wrap `mmap`/`mlock`/`pread` at link time and prefight the real filesystem), the helper socket and
   frame tests (`p2_measure_test` links `strata_kernels_cpu` and `ggml-base`), the dflash tap tests (they
   only mean something with the lane's drafter compiled in, which a gfx906 build of this port cannot do
   today - item 1), the snapshot / validation / transfer conversation tests (they link `strata_engine`;
   the transfer one is a second target over the same source with `CONVERSATION_TEST_TRANSFERS`), and the
   `ds4_*` rule tests, which in the fork sit inside the `STRATA_BUILD_DSV41` guard a gfx906 build keeps
   off. Two more programs needed nothing: `pool_plan_test` and `window_rule_test` are already wired by
   upstream v0.2.18 itself in `cmake/backends/cpu.cmake` (adding them a second time failed the configure
   on the duplicate target names, which is the proof).
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
* **The engine's hash is not a content hash.** v0.2.18 stamps the build with the commit it was
  configured from (`git rev-parse --short=9 HEAD`, run at configure time in `cmake/strata_gpu_engine.cmake`),
  so committing and reconfiguring changes the bytes of every binary with no source change. Quote a build
  as the pair (commit, sha256). This port's builds: at `43ff7f30b` the engine was
  `36f8a503b79c19fc66cf88bd6aa3eb6c54e0a61f35d85fb259791bad2aad2848`, then
  `aa3c67a3154c93a33a5466ebf26e0d719a86af54da23876f76b96627c4499375`; reconfigured and rebuilt at
  `84d066665` it is `7d6873e5e435960399dbd5853e62e4e9feb8056ce20a17c5dd0f0f6877a8035c`; at `520532ca5`
  (the lane's CPU tests wired) it is `40c5018813740097a66a1042f4457fea4a185fdae5b587a627c6988c6b7ccdf4`.
  `strings` finds the stamped commit inside the binary.
* `ldd build-906/strata` with `LD_LIBRARY_PATH=/home/srcds/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib`:
  `libhipblas.so.2` and `libamdhip64.so.6` from `/opt/rocm-6.1.0/lib`, `librocblas.so.4` from
  `/home/srcds/rocm-gfx906-xnack/lib` - what `AGENTS.md` says a correct gfx906 link looks like.
* The lane's own CPU tests, run without a device (2026-11-22, `build-906`, logs
  `logs-port/lane-tests-build-2.log` and `logs-port/lane-tests-run-1.log`; `logs-port/` holds this port's
  configure and build logs and is gitignored by upstream's `*.log` rule, so those two are on this box,
  not in the branch): **8 of 9 pass, 1 fails, and the failure is the un-ported `generate.cpp`.**

  | Program | Result |
  | --- | --- |
  | `vram_guard_test` | passed |
  | `expert_helper_reserve_test` | passed |
  | `expert_helper_candidates_test` | **failed** - see below |
  | `expert_helper_arena_cap_test` | passed |
  | `arena_pin_ranges_test` | passed |
  | `conv_cache_knobs_test` | passed |
  | `swapin_depth_test` | passed |
  | `tail_skip_guard_test` | passed |
  | `read_once_test` | passed |

  `expert_helper_candidates_test` has seven sections. Its six executed sections - 100 checks over the
  aligned slot arithmetic, the eligibility window, the owner and Hadamard exclusions, the byte budget and
  its strict CLI parsing, and the `STRATA_DISJOINT_ADAPT` precedence - all pass. The seventh is a *static
  text contract* it runs against the real sources, and its 12 failures are all checks on
  `src/program/generate.cpp` (that the engine calls the candidate builder and the budget planner, that the
  budget goes to the planner in bytes, that `--expert-helper-mib` is parsed strictly, that the whole path
  sits inside the socket opt-in): item 1's file, still upstream's, so the calls it looks for are not there.
  Its 14 checks on the helper's own source pass. The test reports the section as a text contract, not as
  behaviour, so this is a named missing call site, not a broken algorithm.
* The server's own tests (`terminal/serve`): 94 passed, 6 failed; the same 6 fail on clean upstream
  (`test_dflash_engine_args.py` mocks a name that no longer exists upstream), so the port adds no failure.
* Not run: any boot, any model, any GPU work, any bench. Those are the operator's windows.

## Open decisions for the operator

1. `prefill.cpp` and `generate.cpp`: port the fork's pieces by hand against upstream's rewritten dispatch
   loop (the list with its code is in `PORT-WORKLIST-UNPORTED.md`: 15 expert-helper, 6 CPU-pool and 6
   VRAM-clamp hunks in `generate.cpp`, 6 expert-helper in `prefill.cpp`), or accept a port without helper
   serving. It needs a boot to prove, so it is a joint window, not a Fixer job. The lane test
   `expert_helper_candidates_test` will turn green on its own when the first group lands - its static
   section is the check that says whether those call sites exist.
2. Wire the DeepSeek V4.1 engine target, or record the family as out of this port. *The target is wired
   (item 2 above) and a gfx906 build compiles none of it; the decision left is whether to open a CUDA
   configure to build and run it, which this box cannot do today (item 4 below).*
3. Wire the rest of the lane's test executables so `ctest` covers every ported feature: 9 of 24 are in
   (above), 15 remain, each with the fork block named in `cmake/strata_lane_tests.cmake`.
4. Fix or replace this box's CUDA 13.1 toolkit so the expert helper can be built here at all.
