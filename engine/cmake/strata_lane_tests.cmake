# cmake/strata_lane_tests.cmake - the serving lane's own CPU tests.
#
# Transplanted from the fork's CMakeLists.txt one program at a time, with each block read: the file
# list is in PORT-MANIFEST-TESTS.txt (tools-port/lane-tests.py prints it from the two trees), but the
# blocks cannot be copied wholesale - they name different libraries, some with -Wl,--wrap=... and
# STRATA_RAM_POOL_TEST, and a guess would build something that tests the wrong thing. The tool that
# prints what each fork block compiles, links, defines and guards - and whether this tree already
# builds that name, which is the one mistake a copied block cannot survive (CMP0002 fails the whole
# configure) - is tools-port/lane-test-blocks.py.
#
# The rule for what runs here: a test must reach its verdict without a device. Some of these programs
# link strata_engine, so they carry a GPU runtime; the fork marks them SKIP_RETURN_CODE 77 and runs
# some of them under CUDA_VISIBLE_DEVICES=-1, which is how they are run here too (see PORT-NOTES.md).
# A test that skips itself for want of a device is an honest result; a test run on the device this
# box serves from is not, because it takes memory from the running server.
#
# NOT wired, with the reason for each group:
#   * the device-parity programs (dflash_taps_test, dflash_rope_parity, dflash_load_test,
#     dflash_inco_parity) - they compile the lane's device-side tap kernels (src/kernels/cuda/
#     dflash_inco.cu), and this tree compiles that only under STRATA_ENABLE_DFLASH2, which is OFF here
#     because the serve path that drives it lives in the two un-carried files (PORT-NOTES.md, item 1);
#   * the ds4_* rule tests - the fork keeps them inside the STRATA_BUILD_DSV41 guard, and a gfx906
#     build of this port keeps that guard off, exactly as the fork's build on this card does;
#   * the CUDA MMQ tests (tests/cuda/prefill_mmq_*.cpp, tests/hip/prefill_mmq_parity.cpp) - they test
#     the MMQ libraries this port does not wire (PORT-NOTES.md, item 4);
#   * the conversation snapshot / validation / transfer tests - upstream v0.2.18 wires all three
#     itself (cmake/strata_gpu_engine.cmake:485-520), block for block the same as the fork's, so there
#     was nothing to carry. Adding them a second time fails the configure on the duplicate name, which
#     is the proof; the same is true of pool_plan_test and window_rule_test (cmake/backends/cpu.cmake).
#
# Threads is what the fork's own blocks link; it finds them under CPU backends and the conversation
# block there. QUIET, and only here, so a lane-test configure that reaches none of those still links.
find_package(Threads QUIET)

# ---------------------------------------------------------------------------------------------
# Programs that link no library at all: the fork's own words for these blocks are "the test links NO
# library at all" - header-only decisions and arithmetic, no GPU, no model.
# ---------------------------------------------------------------------------------------------

# The per-card reserve floor the #2065 check sites step in 64 MiB steps (include/strata/core/vram_guard.hpp):
# the arithmetic, the guard's own refusal rule, and what it prints. No device call in the unit under test.
add_executable(vram_guard_test tests/core/vram_guard_test.cpp src/core/vram_guard.cpp)
target_include_directories(vram_guard_test PRIVATE include)
add_test(NAME vram_guard_test COMMAND vram_guard_test)
set_tests_properties(vram_guard_test PROPERTIES TIMEOUT 60)

# --reserve-mib's honest clamp: the range it resolves to, the shapes it exists for, the boundary the
# startup warning keys on. The helper's own parse() is nvcc-only, so the decision is pinned here, in
# the pure header both sides compile.
add_executable(expert_helper_reserve_test tests/core/expert_helper_reserve_test.cpp)
target_include_directories(expert_helper_reserve_test PRIVATE include)
add_test(NAME expert_helper_reserve_test COMMAND expert_helper_reserve_test)
set_tests_properties(expert_helper_reserve_test PROPERTIES TIMEOUT 60)

# Which experts a CUDA helper may claim, and how many fit in its byte budget: eligibility, the budget
# arithmetic and the promotion gate, over masks and profile data. The test's STATIC section reads the
# real sources it wires, so the source-tree path is passed in (fork CMakeLists.txt:1103-1104); the
# section prints SKIPPED when the path is unreadable, so without it the test builds and says nothing.
add_executable(expert_helper_candidates_test
  tests/core/expert_helper_candidates_test.cpp src/core/expert_helper_candidates.cpp)
target_include_directories(expert_helper_candidates_test PRIVATE include)
target_compile_definitions(expert_helper_candidates_test PRIVATE
  STRATA_HELPER_CAND_SOURCE_ROOT="${CMAKE_CURRENT_SOURCE_DIR}")
add_test(NAME expert_helper_candidates_test COMMAND expert_helper_candidates_test)
set_tests_properties(expert_helper_candidates_test PROPERTIES TIMEOUT 60)

# The helper relief's arena cap plan (relief_arena_cap.hpp): the arithmetic behind
# --expert-helper-relief-arena-cap-mib - the hot set's protection, the coldest-first order, the
# caller's drop accounted once. Header-only, links nothing (fork CMakeLists.txt:1144-1147).
add_executable(expert_helper_arena_cap_test tests/core/expert_helper_arena_cap_test.cpp)
target_include_directories(expert_helper_arena_cap_test PRIVATE include)
add_test(NAME expert_helper_arena_cap_test COMMAND expert_helper_arena_cap_test)
set_tests_properties(expert_helper_arena_cap_test PROPERTIES TIMEOUT 60)

# The pin-range arithmetic the re-rank's promote uses (arena_pin_ranges.hpp): the unregistered gaps of
# a span against the sorted pin tables, and the union a coalescing re-registration covers.
add_executable(arena_pin_ranges_test tests/core/arena_pin_ranges_test.cpp)
target_include_directories(arena_pin_ranges_test PRIVATE include)
add_test(NAME arena_pin_ranges_test COMMAND arena_pin_ranges_test)
set_tests_properties(arena_pin_ranges_test PROPERTIES TIMEOUT 60)

# The two conversation-cache env knobs: the end-of-generation anchor behind STRATA_GEN_ANCHOR and the
# idle-park debounce behind STRATA_PARK_DEBOUNCE_MS (program/conv_cache.hpp), with the default-off
# equivalence walked exhaustively.
add_executable(conv_cache_knobs_test tests/core/conv_cache_knobs_test.cpp)
target_include_directories(conv_cache_knobs_test PRIVATE include)
add_test(NAME conv_cache_knobs_test COMMAND conv_cache_knobs_test)
set_tests_properties(conv_cache_knobs_test PROPERTIES TIMEOUT 60)

# STRATA_SWAPIN_DEPTH's parsing, clamps and the swapin_window resolution the pack-file stager applies,
# plus the batched-read accounting a fake source sees behind the window.
add_executable(swapin_depth_test tests/core/swapin_depth_test.cpp)
target_include_directories(swapin_depth_test PRIVATE include)
add_test(NAME swapin_depth_test COMMAND swapin_depth_test)
set_tests_properties(swapin_depth_test PROPERTIES TIMEOUT 60)

# STRATA_TAIL_SKIP_HELPER's parsing and the tail_skip_gate the dispatch's skip loop shares with the
# serve banner: the skip's silent suspension on helper rigs, default-off equivalence walked.
add_executable(tail_skip_guard_test tests/core/tail_skip_guard_test.cpp)
target_include_directories(tail_skip_guard_test PRIVATE include)
add_test(NAME tail_skip_guard_test COMMAND tail_skip_guard_test)
set_tests_properties(tail_skip_guard_test PROPERTIES TIMEOUT 60)

# STRATA_READONCE_MIB's parsing and precedence, the pin-guard reserve and the effective budget, and
# the request-scoped LRU window itself: consult/admit/evict under a byte budget, the RAM guard, the
# meter counters, the re-read collapse at the killer prefill's shape.
add_executable(read_once_test tests/core/read_once_test.cpp)
target_include_directories(read_once_test PRIVATE include)
add_test(NAME read_once_test COMMAND read_once_test)
set_tests_properties(read_once_test PROPERTIES TIMEOUT 60)

# STRATA_PIPELINE_CHUNKS: the explicit chunk count's math (equal chunks, the remainder last, the floor).
add_executable(pipeline_chunks_test tests/prefill/pipeline_chunks_test.cpp)
target_include_directories(pipeline_chunks_test PRIVATE include)
add_test(NAME pipeline_chunks_test COMMAND pipeline_chunks_test)

# STRATA_CACHE_SPILL: the gate's parse, the stage card it spills onto, and the slots that card can take.
add_executable(cache_spill_test tests/core/cache_spill_test.cpp)
target_include_directories(cache_spill_test PRIVATE include)
add_test(NAME cache_spill_test COMMAND cache_spill_test)

# The draft proposal's failure classification (include/strata/core/dflash_refusal.hpp): pure host
# predicate, classification and validate-then-copy handoff. STRATA_DFLASH_SOURCE_ROOT lets the test pin
# the serve/CLI latch statements in the REAL source text - static contract checks, printed as static,
# never claimed as runtime evidence (the same device this test shares with expert_helper_candidates_test:
# a statement missing from an un-carried file is a named gap, not a pass).
add_executable(dflash_refusal_test tests/core/dflash_refusal_test.cpp)
target_include_directories(dflash_refusal_test PRIVATE include)
target_compile_definitions(dflash_refusal_test PRIVATE
  STRATA_DFLASH_SOURCE_ROOT="${CMAKE_CURRENT_SOURCE_DIR}")
add_test(NAME dflash_refusal_test COMMAND dflash_refusal_test)

# The tap-ownership plan under --layer-split (include/strata/core/dflash_tap_plan.hpp): pure host
# arithmetic, the same build() the engine runs at drafter upload (docs/DFLASH2.md).
add_executable(dflash_tap_plan_test tests/core/dflash_tap_plan_test.cpp)
target_include_directories(dflash_tap_plan_test PRIVATE include)
add_test(NAME dflash_tap_plan_test COMMAND dflash_tap_plan_test)

# ---------------------------------------------------------------------------------------------
# The managed RAM pool and the CPU expert pool on top of it. The fork keeps this whole group behind
# its own opt-in (option OFF by default, so the default build adds nothing), and wraps mmap / mlock /
# munmap / munlock / pread at LINK time: the tests inject syscall failures and count the calls, and
# the shim that answers them lives in the test directory, which is why its include path comes FIRST.
# ---------------------------------------------------------------------------------------------
if(CMAKE_SYSTEM_NAME STREQUAL "Linux")
  option(STRATA_BUILD_RAM_POOL_TEST "Build the ram_pool CPU test (opt-in)" OFF)
  if(STRATA_BUILD_RAM_POOL_TEST)
    add_executable(ram_pool_test tests/core/ram_pool_test.cpp src/core/ram_pool.cpp)
    target_include_directories(ram_pool_test BEFORE PRIVATE tests/core include)
    target_compile_definitions(ram_pool_test PRIVATE STRATA_RAM_POOL_TEST)
    target_link_libraries(ram_pool_test PRIVATE Threads::Threads ${CMAKE_DL_LIBS})
    if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
      target_link_options(ram_pool_test PRIVATE
        -Wl,--wrap=mmap -Wl,--wrap=mlock -Wl,--wrap=munmap -Wl,--wrap=munlock)
    endif()
    add_test(NAME ram_pool_test_mock COMMAND ram_pool_test mock)
    set_tests_properties(ram_pool_test_mock PROPERTIES TIMEOUT 15)
    add_test(NAME ram_pool_test_real_mock COMMAND ram_pool_test real_mock)
    set_tests_properties(ram_pool_test_real_mock PROPERTIES TIMEOUT 15 SKIP_RETURN_CODE 77)

    # Simulated non-Linux guard probe: the probe AND the core implementation compiled with
    # -U__linux__, to show the __linux__-guarded paths degrade to a self-contained refusal contract.
    # Simulated preprocessor coverage on a Linux CPU host, not a Windows/macOS toolchain validation.
    add_executable(ram_pool_nonlinux_probe tests/core/ram_pool_nonlinux_probe.cpp src/core/ram_pool.cpp)
    target_include_directories(ram_pool_nonlinux_probe BEFORE PRIVATE tests/core include)
    if(CMAKE_SYSTEM_NAME STREQUAL "Linux" AND CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
      target_compile_options(ram_pool_nonlinux_probe PRIVATE -U__linux__)
    endif()
    target_link_libraries(ram_pool_nonlinux_probe PRIVATE Threads::Threads ${CMAKE_DL_LIBS})
    add_test(NAME ram_pool_nonlinux_probe COMMAND ram_pool_nonlinux_probe)
    set_tests_properties(ram_pool_nonlinux_probe PROPERTIES TIMEOUT 15)

    # CPU expert-cache foundation over the RAM pool (opt-in, same guard).
    add_executable(pool_expert_cache_test tests/core/pool_expert_cache_test.cpp src/core/pool_expert_cache.cpp src/core/ram_pool.cpp)
    target_include_directories(pool_expert_cache_test BEFORE PRIVATE tests/core include)
    target_compile_definitions(pool_expert_cache_test PRIVATE STRATA_RAM_POOL_TEST)
    target_link_libraries(pool_expert_cache_test PRIVATE Threads::Threads ${CMAKE_DL_LIBS})
    if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
      target_link_options(pool_expert_cache_test PRIVATE
        -Wl,--wrap=mmap -Wl,--wrap=mlock -Wl,--wrap=munmap -Wl,--wrap=munlock)
    endif()
    add_test(NAME pool_expert_cache_test COMMAND pool_expert_cache_test ${CMAKE_BINARY_DIR})
    set_tests_properties(pool_expert_cache_test PROPERTIES TIMEOUT 15)

    # BoundedDirectReader (O_DIRECT) + PoolExpertCache over the same RAM pool. Wraps pread as well, and
    # preflights bare O_DIRECT: it skips 77 only when the real filesystem lacks O_DIRECT.
    add_executable(bounded_direct_reader_test
                   tests/core/bounded_direct_reader_test.cpp
                   src/platform/bounded_direct_reader.cpp
                   src/core/pool_expert_cache.cpp
                   src/core/ram_pool.cpp)
    target_include_directories(bounded_direct_reader_test BEFORE PRIVATE tests/core include)
    target_compile_definitions(bounded_direct_reader_test PRIVATE STRATA_RAM_POOL_TEST)
    target_link_libraries(bounded_direct_reader_test PRIVATE Threads::Threads ${CMAKE_DL_LIBS})
    if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
      target_link_options(bounded_direct_reader_test PRIVATE
        -Wl,--wrap=mmap -Wl,--wrap=mlock -Wl,--wrap=munmap -Wl,--wrap=munlock -Wl,--wrap=pread)
    endif()
    add_test(NAME bounded_direct_reader_test COMMAND bounded_direct_reader_test ${CMAKE_BINARY_DIR})
    set_tests_properties(bounded_direct_reader_test PROPERTIES TIMEOUT 15 SKIP_RETURN_CODE 77)

    # PoolExpertStore over the same RAM pool (opt-in, same guard). Real fixture I/O.
    add_executable(pool_expert_store_test
                   tests/core/pool_expert_store_test.cpp
                   src/core/pool_expert_store.cpp
                   src/core/pool_expert_cache.cpp
                   src/platform/bounded_direct_reader.cpp
                   src/core/ram_pool.cpp)
    target_include_directories(pool_expert_store_test BEFORE PRIVATE tests/core include)
    target_compile_definitions(pool_expert_store_test PRIVATE STRATA_RAM_POOL_TEST)
    target_link_libraries(pool_expert_store_test PRIVATE Threads::Threads ${CMAKE_DL_LIBS})
    if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
      target_link_options(pool_expert_store_test PRIVATE
        -Wl,--wrap=mmap -Wl,--wrap=mlock -Wl,--wrap=munmap -Wl,--wrap=munlock -Wl,--wrap=pread)
    endif()
    add_test(NAME pool_expert_store_test COMMAND pool_expert_store_test ${CMAKE_BINARY_DIR})
    set_tests_properties(pool_expert_store_test PROPERTIES TIMEOUT 15)

    # PoolExpertSourceAdapter over the same RAM pool (opt-in, same guard). CPU-only TU: no CUDA headers;
    # the async-note hook is injected.
    add_executable(pool_expert_source_adapter_test
                   tests/core/pool_expert_source_adapter_test.cpp
                   src/core/pool_expert_source_adapter.cpp
                   src/core/pool_expert_store.cpp
                   src/core/pool_expert_cache.cpp
                   src/platform/bounded_direct_reader.cpp
                   src/core/ram_pool.cpp)
    target_include_directories(pool_expert_source_adapter_test BEFORE PRIVATE tests/core include)
    target_compile_definitions(pool_expert_source_adapter_test PRIVATE STRATA_RAM_POOL_TEST)
    target_link_libraries(pool_expert_source_adapter_test PRIVATE Threads::Threads ${CMAKE_DL_LIBS})
    if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
      target_link_options(pool_expert_source_adapter_test PRIVATE
        -Wl,--wrap=mmap -Wl,--wrap=mlock -Wl,--wrap=munmap -Wl,--wrap=munlock -Wl,--wrap=pread)
    endif()
    add_test(NAME pool_expert_source_adapter_test COMMAND pool_expert_source_adapter_test ${CMAKE_BINARY_DIR})
    set_tests_properties(pool_expert_source_adapter_test PROPERTIES TIMEOUT 15)
  endif()

  # STRATA_NATIVE_MLOCK: the native file-map lock helper under a CPU-only injection - OFF/no-syscall,
  # success, first-chunk failure, later-chunk failure with already-locked bytes retained, truthful
  # aggregate, knob=0 same as OFF. mlock/getrlimit/setrlimit are answered by this TU's own definitions,
  # which the linker prefers over libc's, so no real RLIMIT is touched. include/strata/core/pin_budget.hpp
  # includes <cuda_runtime.h> unconditionally, so the minimal test-only shim (tests/core/cuda_runtime.h,
  # PORT-MANIFEST-TESTSUP.txt) goes first on this target's include path. Each scenario is its own CTest
  # because native_mlock_detail::mode() caches the knob on first call.
  add_executable(native_mlock_test tests/core/native_mlock_test.cpp)
  target_include_directories(native_mlock_test BEFORE PRIVATE tests/core include)
  target_link_libraries(native_mlock_test PRIVATE ${CMAKE_DL_LIBS})
  add_test(NAME native_mlock_test_off      COMMAND native_mlock_test off)
  add_test(NAME native_mlock_test_success  COMMAND native_mlock_test success)
  add_test(NAME native_mlock_test_empty    COMMAND native_mlock_test empty)
  add_test(NAME native_mlock_test_first_fail COMMAND native_mlock_test first_fail)
  add_test(NAME native_mlock_test_later_fail COMMAND native_mlock_test later_fail)
  add_test(NAME native_mlock_test_mixed_span COMMAND native_mlock_test mixed_span)
  add_test(NAME native_mlock_test_knob_zero COMMAND native_mlock_test knob_zero)
  add_test(NAME native_mlock_test_arena_only COMMAND native_mlock_test arena_only)
  add_test(NAME native_mlock_test_all_spans COMMAND native_mlock_test all_spans)
  add_test(NAME native_mlock_test_word_all COMMAND native_mlock_test word_all)
endif()

# The same simulated non-Linux guard probe for the lock helper: compiled with -U__linux__ to show the
# guarded paths degrade to a self-contained OFF/no-op contract.
add_executable(native_mlock_nonlinux_probe tests/core/native_mlock_nonlinux_probe.cpp)
target_include_directories(native_mlock_nonlinux_probe BEFORE PRIVATE tests/core include)
if(CMAKE_SYSTEM_NAME STREQUAL "Linux" AND CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
  target_compile_options(native_mlock_nonlinux_probe PRIVATE -U__linux__)
endif()
target_link_libraries(native_mlock_nonlinux_probe PRIVATE ${CMAKE_DL_LIBS})
add_test(NAME native_mlock_nonlinux_probe COMMAND native_mlock_nonlinux_probe)

# ---------------------------------------------------------------------------------------------
# The helper's host-IPC side: the frame transport, the engine's proxy client and the CPU recompute of
# the rows the helper claimed. CPU-only BY CONSTRUCTION - the sources these compile call no device
# function and open no CUDA handle; the proof is nm on the objects, not a flag. Linux only (AF_UNIX).
# ---------------------------------------------------------------------------------------------
if(CMAKE_SYSTEM_NAME STREQUAL "Linux"
   AND EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/tests/core/expert_helper_socket_test.cpp")
  add_executable(expert_helper_socket_test
    tests/core/expert_helper_socket_test.cpp src/core/expert_helper_socket.cpp
    tools/mixed_gpu/sidecar/helper_frame.cpp)
  target_include_directories(expert_helper_socket_test PRIVATE include)
  # the q8_1 quantizer's golden pins (quantize_q8_1_cpu, R2 gate M2) need ggml's fp16 conversion:
  # ggml-base is CPU-only (no device, no ggml-cpu/ggml-cuda/ggml-hip) and carries ggml.h.
  target_link_libraries(expert_helper_socket_test PRIVATE ggml-base)
  add_test(NAME expert_helper_socket_test COMMAND expert_helper_socket_test)
  set_tests_properties(expert_helper_socket_test PROPERTIES TIMEOUT 120)

  # W1/W3 (window wire diet): the fp32 window wire's knobs, negotiation gate and exact kBeginF32 frame,
  # plus the GPU-quant golden compare harness - same three sources as above. The GPU leg runs in a build
  # window through dump files (the test's section 5); without a device that section says so.
  if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/tests/core/expert_helper_f32_window_test.cpp")
    add_executable(expert_helper_f32_window_test
      tests/core/expert_helper_f32_window_test.cpp src/core/expert_helper_socket.cpp
      tools/mixed_gpu/sidecar/helper_frame.cpp)
    target_include_directories(expert_helper_f32_window_test PRIVATE include)
    target_link_libraries(expert_helper_f32_window_test PRIVATE ggml-base)
    add_test(NAME expert_helper_f32_window_test COMMAND expert_helper_f32_window_test)
    set_tests_properties(expert_helper_f32_window_test PROPERTIES TIMEOUT 60)
  endif()

  add_executable(expert_helper_fallback_test tests/core/expert_helper_fallback_test.cpp
    src/core/expert_helper_fallback.cpp)
  target_include_directories(expert_helper_fallback_test PRIVATE include)
  target_link_libraries(expert_helper_fallback_test PRIVATE strata_kernels_cpu)
  add_test(NAME expert_helper_fallback_test COMMAND expert_helper_fallback_test)
  set_tests_properties(expert_helper_fallback_test PROPERTIES TIMEOUT 300)

  # the framed transport's own cost, so a GPU measurement can subtract the channel from a number
  add_executable(helper_microbench tools/mixed_gpu/sidecar/helper_microbench.cpp
    tools/mixed_gpu/sidecar/helper_frame.cpp)
  target_include_directories(helper_microbench PRIVATE include)
  add_test(NAME helper_microbench COMMAND helper_microbench 200)
  set_tests_properties(helper_microbench PROPERTIES TIMEOUT 120)

  # R5 P2: the deviation harness's own halves - the metric against hand-computed references, the
  # percentile rule, the pinned fixtures, the selection's determinism and caps, the JSON's byte
  # stability, and a framing smoke against an IN-PROCESS fake helper over a socketpair. It runs without
  # a GPU, without a helper process and without a pack. Its sources are the lane's own harness
  # (tools/mixed_gpu/p2_measure/, PORT-MANIFEST-TESTSUP.txt): the plan's 140 did not count them.
  if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/tools/mixed_gpu/p2_measure/p2_measure_test.cpp")
    add_executable(p2_measure_test
      tools/mixed_gpu/p2_measure/p2_measure_test.cpp
      tools/mixed_gpu/p2_measure/p2_measure.cpp
      src/core/expert_helper_socket.cpp
      tools/mixed_gpu/sidecar/helper_frame.cpp)
    target_include_directories(p2_measure_test PRIVATE include)
    target_link_libraries(p2_measure_test PRIVATE strata_kernels_cpu ggml-base)
    add_test(NAME p2_measure_test COMMAND p2_measure_test)
    set_tests_properties(p2_measure_test PROPERTIES TIMEOUT 120)
  endif()
endif()

# ---------------------------------------------------------------------------------------------
# The host-side DFlash programs that need the engine library (and so a GPU runtime on the link, which
# is why they carry the fork's SKIP_RETURN_CODE 77: run without a device, they report SKIPPED).
# ---------------------------------------------------------------------------------------------

# The draft policy's decisions (src/spec/dflash_policy_test.cpp): the same rules the controller applies.
add_executable(dflash_policy_test src/spec/dflash_policy_test.cpp)
target_link_libraries(dflash_policy_test PRIVATE strata_spec)
add_test(NAME dflash_policy_test COMMAND dflash_policy_test)

# DFlash tap packing: padded planes, 1..8 rows, BF16 and F32 sources.
#
# This one does NOT build on this box, and the reason is a defect in the FORK, found by this port - so
# it is wired exactly as the fork wires it and left out of the default build (EXCLUDE_FROM_ALL), rather
# than quietly repaired. tests/core/cuda_runtime.h - the 37-line shim that lets the CPU-only
# native_mlock_test parse pin_budget.hpp's unconditional <cuda_runtime.h>, added in the fork's
# 7baf1501c3 on 2026-11-19 - sits in the same directory as this test's gguf_fixture.hpp, and the fork's
# block puts that directory on the include path. A -I directory is searched before an -isystem one, so
# the shim shadows include/strata/platform/hip_compat/cuda_runtime.h, the header that maps the CUDA
# runtime names onto HIP on this card: cudaMalloc, cudaStream_t and cudaMemcpy then never get declared
# (37 errors). Measured on the fork's own tree, with the fork's own include order: 37 errors with
# -I tests/core, 0 without it. And it is the shim that broke it, not this port: the fork's last gfx906
# build of this file (build-mixed-gpu/rpcw-hip-fix16b, object dated 2026-11-13) records
# hip_compat/cuda_runtime.h in its dep file, six days before the shim landed. The fork's own fix is one
# of two moves - keep the shim out of tests/core, or keep tests/core off the two dflash blocks - and
# that is the fork maintainer's call, not one to make in a port branch.
add_executable(dflash_head_bf16_test EXCLUDE_FROM_ALL tests/core/dflash_head_bf16_test.cpp)
target_include_directories(dflash_head_bf16_test PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/tests/core)
target_link_libraries(dflash_head_bf16_test PRIVATE strata_engine)
add_test(NAME dflash_head_bf16_test COMMAND dflash_head_bf16_test)
set_tests_properties(dflash_head_bf16_test PROPERTIES SKIP_RETURN_CODE 77 TIMEOUT 120)

# Cross-device tap transport (include/strata/core/dflash_tap_collect.hpp): synthetic per-stage planes vs
# the CPU-expected assembled plane, slot-reuse rounds, refusals. The fork's block also names tests/core
# on the include path; here it does not, for the reason above - and it is provably nothing to this TU:
# its includes are strata/core/dflash_tap_collect.hpp, strata/core/on_device.hpp and <cuda_runtime.h>,
# and the fork's own gfx906 build of it (rpcw-hip-fix16b) resolved the runtime header to hip_compat,
# with no file from tests/core in its dep list.
add_executable(dflash_tap_collect_test tests/core/dflash_tap_collect_test.cpp)
target_link_libraries(dflash_tap_collect_test PRIVATE strata_engine)
add_test(NAME dflash_tap_collect_test COMMAND dflash_tap_collect_test)
set_tests_properties(dflash_tap_collect_test PROPERTIES SKIP_RETURN_CODE 77 TIMEOUT 180)

# The collector's slot lifecycle (acquire/eviction/reset), CPU-safe: ev == nullptr means the real
# acquire() makes no CUDA call.
add_executable(dflash_tap_slot_test tests/core/dflash_tap_slot_test.cpp)
target_include_directories(dflash_tap_slot_test PRIVATE include)
target_link_libraries(dflash_tap_slot_test PRIVATE strata_engine)
add_test(NAME dflash_tap_slot_test COMMAND dflash_tap_slot_test)

# The DFlash drafter's OWNED K/V state: pools inside the caller's arena, append/readback exact, elastic
# non-membership, five independent layers (docs/DFLASH.md).
add_executable(dflash_owned_kv_test tests/core/dflash_owned_kv_test.cpp)
target_link_libraries(dflash_owned_kv_test PRIVATE strata_engine)
add_test(NAME dflash_owned_kv_test COMMAND dflash_owned_kv_test)
set_tests_properties(dflash_owned_kv_test PROPERTIES SKIP_RETURN_CODE 77 TIMEOUT 120)

# Which expert a helper owns, and what happens when that answer arrives late or not at all.
add_executable(expert_helper_ownership_test tests/core/expert_helper_ownership_test.cpp)
target_link_libraries(expert_helper_ownership_test PRIVATE strata_engine)
add_test(NAME expert_helper_ownership_test COMMAND expert_helper_ownership_test)

# ---------------------------------------------------------------------------------------------
# The two programs the fork keeps inside its conversation-test option. Upstream v0.2.18 has the same
# option and its own set inside it; these are the two the fork added to that block, so they go inside
# the same option here and a default configure builds neither.
# ---------------------------------------------------------------------------------------------
if(STRATA_BUILD_CONVERSATION_TESTS)
  # The bounded SWA ring math (include/strata/core/dflash_ring.hpp): pure host arithmetic, no GPU
  # headers, no link - the CPU proof of the mapping the runtime and the snapshots share (docs/DFLASH.md).
  add_executable(dflash_ring_test tests/core/dflash_ring_test.cpp)
  target_include_directories(dflash_ring_test PRIVATE include)
  add_test(NAME dflash_ring_test COMMAND dflash_ring_test)

  # The automatic disk tier's host policy (include/strata/core/conversation_disk_cache.hpp): the bounded
  # index, the reserve-before-write quota with its protections, the publication order, the pair-sidecar
  # reclaim and the restart inventory - on real files in a temporary folder, with injected IO faults.
  add_executable(conversation_disk_cache_test tests/core/conversation_disk_cache_test.cpp
               src/core/conversation_disk_cache.cpp src/core/conversation_file.cpp)
  target_include_directories(conversation_disk_cache_test PRIVATE include)
  if(Threads_FOUND)
    target_link_libraries(conversation_disk_cache_test PRIVATE Threads::Threads)
  endif()
  add_test(NAME conversation_disk_cache_test COMMAND conversation_disk_cache_test)
endif()

# Two programs of this lane were already covered: pool_plan_test and window_rule_test are wired by
# upstream v0.2.18 itself (cmake/backends/cpu.cmake), and the conversation snapshot, validation and
# transfer tests by cmake/strata_gpu_engine.cmake. They are not repeated here - an attempt to add any
# of them failed the configure on the duplicate target name, which is the proof.
