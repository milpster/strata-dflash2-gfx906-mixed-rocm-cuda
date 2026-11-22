# cmake/strata_lane_tests.cmake - the serving lane's own CPU tests.
#
# Transplanted from the fork's CMakeLists.txt one program at a time, with each block read: the file
# list is in PORT-MANIFEST-TESTS.txt (tools-port/lane-tests.py prints it from the two trees), but the
# blocks cannot be copied wholesale - they name different libraries, some with -Wl,--wrap=... and
# STRATA_RAM_POOL_TEST, and a guess would build something that tests the wrong thing.
#
# What is here is the part that needs nothing but its own translation units: no GPU, no model, no
# library at all beyond the standard thread and math ones - exactly what each fork block declares
# ("the test links NO library at all"). Called from CMakeLists.txt under STRATA_BUILD_TESTS.
#
# Still owed, each with its fork block to read first: the RAM pool group (pool_expert_cache_test,
# pool_expert_store_test, bounded_direct_reader_test, ram_pool_test - they wrap mmap/mlock/pread at
# link time and prefight the real filesystem), the helper socket and frame tests (they need
# tools/mixed_gpu/sidecar/helper_frame.cpp and the socket TU, and p2_measure_test links
# strata_kernels_cpu and ggml-base), the dflash tap tests (they only mean something with the lane's
# drafter compiled in, which a gfx906 build of this port does not do today - see PORT-NOTES.md), the
# snapshot/validation/transfer conversation tests (they link strata_engine, and the transfer one needs
# a second target of the same source with CONVERSATION_TEST_TRANSFERS), and the ds4_* rule tests,
# which in the fork sit inside the STRATA_BUILD_DSV41 guard that a gfx906 build keeps off.

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

# Two programs of this lane were already covered: pool_plan_test and window_rule_test are wired by
# upstream v0.2.18 itself (cmake/backends/cpu.cmake), so they are not repeated here - an attempt to
# add them failed the configure on the duplicate target names, which is the proof.
