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

read_direct() uses Linux O_DIRECT/AIO, sorted windows and 4 KiB sectors. It consumes contiguous experts.bin ranges and three native GGUF role slices per blob. copy_blobs() uses this path only in unbuffered mode; default/per-blob fallback can use mapped memcpy, not guaranteed page-cache-free.

**Parent source correction at 38fe50f:** 32 MiB limits a merged request, NOT retained scratch per thread. src/core/expert_source.cpp:2998-3003 grows thread-local scratch to the batch total before the shared deep-window decision at :3011-3021. The earlier ≤32 MiB/thread claim was false; this is not a bounded pool backing reader.

## 4. Recommended ONE slice (bounded, existing consumer)

Do NOT bundle budget knob, all expert source, host-KV, or generate.cpp wiring. KV host allocation is layer.cpp:585 kv_host_alloc() (cudaHostAllocMapped|Portable, g_kv_host_bytes), NOT generate.cpp.

Historical proposal, **not accepted as the final storage solution**: a copy-only pool read for one lent-slot refill. Accepted direction is a primary lease-owned cache and bounded immutable backing reader; see the authoritative parent addendum in the L2 boundary report. Copying bytes from a file does not make a subsequent GPU copy synchronous: retain the lease through proven completion or teardown. Current refill is generate.cpp:24032-24056. All other raw/alias consumers still need adaptation before managed-source activation.

## 5. CPU test fixture / obstruction

expert_source.cpp is GPU-linked through CUDA/HIP allocation/event calls; no CPU fixture instantiating that source was established. The accepted CPU PoolExpertCache fixture at 38fe50f proves cache ownership/reuse, not native backing integration. Next is a real synchronous direct reader with caller-owned pool scratch, tested through the actual cache; stubbing a large GPU runtime is not accepted evidence.

## Remaining blockers (unresolved)

- No existing CPU test instantiates ExpertSource against experts.bin for byte-equivalence/eviction/live-borrower refusal.
- note_async_read cannot be inherited by a pool-backed source for all blob_stable users; the copy-only adapter sidesteps this for the chosen consumer only.
- read_direct default path is mapped memcpy; bounded/direct I/O requires unbuffered mode (direct_/pdirect_).
- Whole-stack budget/controller and host-KV admission (layer.cpp:585) are explicitly later lanes.
