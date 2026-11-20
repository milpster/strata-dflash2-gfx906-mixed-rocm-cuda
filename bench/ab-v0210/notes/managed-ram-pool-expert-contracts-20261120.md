# Managed RAM pool — expert integration contracts (revised 2026-11-20)

Source facts first, proposals second. Repo /mnt/srcds/Strata; engine worktree /mnt/srcds/Strata-wt-helper-ipc HEAD cc45181 (code unchanged from 3cfc554; ram_pool core default-off, unchanged). Read-only recon; no builds.

## 1. Confirmed source facts

Selected pack: /mnt/srcds/ai/iq3xxs-ud/experts.bin = 50,294,988,800 bytes, with native_experts.txt present → NATIVE pack, not canonical Q2_0. Layout is dynamic: expert_layout_load() reads pack_dir/native_experts.txt (expert_layout.cpp:435), sets ExpertLayout.native=true, per-layer fmt/offset/bytes/gguf_off/gguf_file (expert_layout.hpp:18-42). FileExpertSource::open() (expert_source.cpp:1411) checks size vs layout.total, builds layer_offsets_/layer_blob_bytes_ per layer. No fixed BLOB/48x512 geometry; no claim the user size mismatches the source.

ExpertSource::blob_stable() promises validity while source open (hpp:202); blob_parts() same lifetime (hpp:218).

FileExpertSource::note_async_read() (cpp:2122): looks up blob in stage_idx_; returns early if not a stage buffer (mapping or resident arena); no-op for pageable buffers; for pinned stage buffers records a cudaEvent on the stream. Protects ONLY its own recycled stage buffers, not all blob_stable users.

## 2. Real consumers: acquisition → retention → completion → required adaptation

| Acquisition | Retention | Completion/release | Gap for pool lease |
|---|---|---|---|
| generate.cpp:5611 blob() → fill_slot() | pointer queued to GPU cache fill on stream cs | note_async_read(b,cs) at :5620; sync_queued after | note_async_read only fences stage buffers; mmap/arena pointers pass unprotected |
| generate.cpp:17117 blob_stable() → fill_slot_queued_staged() | pointer handed to queued slot copy | note_async_read(b,nullptr) at :17123; xcache.sync_queued() at :17127 | same: raw pointer crosses completion boundary |
| generate.cpp:20300 blob_stable() → fill_slot_queued_staged/blocking | pointer retained across copy | note_async_read(b,nullptr) at :20325 | same |
| generate.cpp:23985 blob_stable() → fill_slot_blocking/queued_staged | pointer retained across copy | note_async_read(b,nullptr) at :23992; xcache.sync_queued(err) at :23995 | same |
| generate.cpp:9346/:9522 blob_stable() → nib_x.check/nib_c.check | used synchronously in call | call return | no async issue; copy-only adapter safe |
| generate.cpp:15638 swap_blobs_batch() → caller dst | written into caller buffer, no retention | call return | already copy-based; pool lease could own dst |

Bypasses virtual access: ArenaExpertSource::blob() (cpp:6727) returns base_+arena_off() directly — raw arena pointer, no staging; its note_async_read (cpp:7538) delegates to file_->note_async_read(), protecting only file-source stage buffers.

## 3. Immutable selected experts.bin bounded I/O

read_direct() (cpp:2844): Linux O_DIRECT + one io_submit per batch; windows sorted by offset, merged ≤32 MiB; thread-local aligned scratch; 4 KiB sectors. Consumes experts.bin (role_ptr_ empty → one contiguous range per blob at cpp:2993-2997) AND native GGUF role slices (cpp:3001-3009: 3 spans per blob, per-role file/offset). copy_blobs() (cpp:3549) uses read_direct ONLY when direct_ or pdirect_ non-empty (unbuffered mode, n>1); otherwise falls back to per-blob copy_blob→copy_from_files→mapped memcpy (cpp:3550). Default path is mapped-file memcpy, NOT guaranteed direct/page-cache-free. Scratch upper bound: one batch, merged ≤32 MiB per thread, thread-local and reused. Fallback: per-blob copy_from_files (mapped memcpy or direct single-blob read).

## 4. Recommended ONE slice (bounded, existing consumer)

Do NOT bundle budget knob, all expert source, host-KV, or generate.cpp wiring. KV host allocation is layer.cpp:585 kv_host_alloc() (cudaHostAllocMapped|Portable, g_kv_host_bytes), NOT generate.cpp.

Slice: add a pool-backed copy-only read to FileExpertSource — a method taking (layer, expert, Pool&) that copy_blobs one blob into a fresh pool lease and returns the lease; bind it to the lent-slot refill consumer at generate.cpp:23985 (replace blob_stable→fill_slot_queued_staged with pool-alloc→copy_blob→fill from lease). Completion: xcache.sync_queued() + the lease's own lifetime (no note_async_read needed since data is copied, not aliased). Does NOT cover: verify/nibble blob_stable users (:9346 etc.), CPU dispatch fill (:5611), VRAM refill (:17117), ArenaExpertSource arena pointers, or host-KV redirection. Those are later lanes.

## 5. CPU test fixture / obstruction

No CPU-instantiable ExpertSource test fixture exists for byte-equivalence against experts.bin. ram_pool_test.cpp tests the pool only (no ExpertSource I/O). Obstruction: expert_source.cpp includes <cuda_runtime.h> (cpp:29) and calls cudaHostAlloc/cudaEventCreateWithFlags/cudaEventRecord in stage buffer management (cpp:1935, 2131, 2138), so linking a pure-CPU ExpertSource test requires CUDA/HIP headers and libraries. Smallest isolated CPU seam: test FileExpertSource::copy_blobs/copy_from_files byte-equivalence against the mapped blob via a harness stubbing cuda_* calls, or test read_direct offset/alignment arithmetic directly. No new abstraction advertised as integration.

## Remaining blockers (unresolved)

- No existing CPU test instantiates ExpertSource against experts.bin for byte-equivalence/eviction/live-borrower refusal.
- note_async_read cannot be inherited by a pool-backed source for all blob_stable users; the copy-only adapter sidesteps this for the chosen consumer only.
- read_direct default path is mapped memcpy; bounded/direct I/O requires unbuffered mode (direct_/pdirect_).
- Whole-stack budget/controller and host-KV admission (layer.cpp:585) are explicitly later lanes.