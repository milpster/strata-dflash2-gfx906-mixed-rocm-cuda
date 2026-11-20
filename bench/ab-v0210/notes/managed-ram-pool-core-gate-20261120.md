# Managed RAM Pool core gate — L1 attempt 2 (2026-11-20)

**Verdict: PASS (limited CPU foundation).** Proceed to expert-storage integration; whole-stack serving remains blocked.

## Basis (source reads are tool successes; no rerun/invention)

Worktree `/mnt/srcds/Strata-wt-helper-ipc`, branch `helper-ipc-diet` @ `3cfc554`. Files reviewed: `include/strata/core/ram_pool.hpp`, `src/core/ram_pool.cpp`, `tests/core/ram_pool_test.cpp`, `tests/core/ram_pool_nonlinux_probe.cpp`, CMakeLists `STRATA_BUILD_RAM_POOL_TEST` block. Parent evidence in `bench/.../final-cpu/`: `status.txt` (configure/build/ctest/sanitized all exit=0; OFF target+test checks PASS), `ctest.log` (14/14 pass, 0 failures, 0 skips), `build.log`, `build-sanitized.log`. Parent-observed: VmLck KiB 0→64→64→64→0, borrower valid after owner destruction, exactly one final unlock+unmap, four metadata-allocation fault stages + two create-stage faults with populated output and all stats unchanged, mock peak 1 MiB / actual lock peak 64 KiB, 400 concurrent allocations with full-payload checks, exact 40 KiB prefix cleanup, forced non-64-KiB mapping, ASan+UBSan mock+nonLinux probe 2/2 exit 0, default-OFF full Ninja graph omits both targets and CTest filter returns zero tests.

## Release-blocking invariant check (none found)

- **Shared lifetime / partition invariant:** `LeaseState` holds a stable `std::list<Range>::iterator` into `PoolState::partition` plus shared `PoolState` ownership; mapping stays alive until last lease, even after `Pool` wrapper destroyed (hpp:62-63, cpp:87-89). Last lease marks its node free and coalesces by erase-only (no allocation): merges next then prev (cpp:101-114). Alloc commits via non-throwing `splice` then `erase` of unarmed lead/alloc/trail nodes; `ls->node` assigned to the in-partition alloc iterator after splice (cpp:340-343). Iterators stable; no dangling.
- **Exception transaction:** arg validation and slot search are read-only (cpp:269-306). Metadata prep (`make_shared<LeaseState>` + `repl.push_back` list nodes) runs in try/catch `bad_alloc`/`length_error` → `errc::not_enough_memory`, state untouched (cpp:316-335). Commit (splice/erase/stats) is non-throwing; `out_lease.impl_ = std::move(ls)` only after `g.unlock()` (cpp:355-356). Hook `before_metadata_allocation()` invoked pre-each allocation, caught transactionally (cpp:65-69, 206-216). Failure preserves output and stats; no exception escapes `error_code` API.
- **Alignment / overflow:** returned address = absolute `base + start`; `start` derived from `round_up(base_addr + s0, alignment) - base_addr`, so alignment applies to the returned address, not offset (cpp:287-299). Overflow checked at create (`base + pool_bytes <= UINTPTR_MAX`, cpp:229-232) and `abs` bounded by that; `round_up` overflow-checked (cpp:41-46). `alignment >= page_bytes` enforced (cpp:271); `reserved > e0 - start` and `reserved > capacity - reserved_live_bytes` preclude underflow/oversubscription (cpp:300-301). Huge alignment on short tail fails preserving stats/output (test line 398-402).
- **Complete-lock / cleanup owner:** `create` pre-faults and mlocks in bounded chunks; on any failure `p` stays empty and the sole local `shared_ptr<PoolState>` is destroyed, so `PoolStateDeleter` runs exactly once: `munlock(locked_bytes prefix)` + `munmap(base, size)` (cpp:169-180, 240-247). Partial prefix unlocked, nothing left mapped/locked. On success `locked_bytes = pool_bytes`, deleter unlocks full once at last reference drop (cpp:248, test lines 334-340, 679-681).
- **Default-off boundaries:** target/tests gated by `STRATA_BUILD_RAM_POOL_TEST` default OFF (CMakeLists:544-573); default-OFF Ninja graph omits `ram_pool_test` and `ram_pool_nonlinux_probe`, CTest filter returns zero tests (targets-off-all.log, tests-off.log). No engine wiring, no GPU headers/HIP/CUDA, no env knob, no default-path change.

## Stated limits (not established by this gate)

Whole-stack budget enforcement, expert raw pointers / GPU fences, runtime resizing, 52 GiB allocation, real non-Linux toolchains, low-RLIMIT skip branch, GPU performance, production hang, and HIP pinning remain out of scope; expert/KV integration is not authorized by this gate.

## Next step

Expert storage may be built against this core (opt-in, behind its own flag). Each integration commit serial; parent owns acceptance. No source/git changes made by this review; only this report written.

**Parent acceptance:** the limited CPU foundation is accepted. The earlier
"not authorized" wording does not grant or revoke source-work scope; it
means expert/KV behavior and deployment are not validated by this gate.
Source integration continues under the operator's feature request. No boot,
GPU window, promotion, or whole-stack budget guarantee is authorized here.
