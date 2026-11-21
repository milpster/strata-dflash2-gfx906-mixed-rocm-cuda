# Fix Specification: DiskCache Checkpoint-Aware Restore

**Commit:** 42cb41b314  
**Date:** 2026-11-19  
**Root Cause:** `DiskCache::best()` only matches a generation's *full* token length. When a published generation (e.g., 16,148 tokens) exceeds the next request's prompt (e.g., 16,133 tokens) — which happens when a conversation is rewritten/truncated after an idle-save — the generation is skipped entirely. Its valid checkpoint (anchor=16,015) is never offered, so the disk restore path is never entered.

---

## 1. DATA AVAILABILITY — Can a restore today target a CHECKPOINT inside a generation?

**Answer: NO — not today, but the data EXISTS in the file and the in-memory index carries the scalar prefixes needed for lookup.**

### What `DiskGeneration` carries (in-memory index, `conversation_disk_cache.hpp:41-57`):

```cpp
struct DiskGeneration {
    uint64_t serial = 0;
    std::string path;
    std::vector<int32_t> tokens;         // FULL conversation tokens (stored ONCE)
    std::vector<ConversationImageKey> images;
    bool cvec = true;
    uint64_t bytes = 0;
    std::vector<uint64_t> ring_ids;      // pair sidecars its checkpoint metas reference
    int64_t anchor = 0;                  // deepest checkpoint prefix (the target anchor)
    int64_t pair_prefix = 0;             // deepest checkpoint prefix with a legal pair (0: none)
    bool tail_draft = false;             // generation's own ring horizon retains resume at its end
    uint64_t used = 0;                   // LRU use stamp
    bool pinned = false;
    bool dir_flush_pending = false;
    bool guarded = false;
    bool referenced = false;
};
```

The index stores **only scalar prefixes** (`anchor`, `pair_prefix`), not per-checkpoint token vectors. The full `tokens` vector is the generation's complete conversation.

### What the `.sess` file (v2 manifest) carries (`conversation_file.hpp:335-362`, `conversation_file.cpp:1553-1562`):

```cpp
struct SessionCheckpointMeta {
    uint64_t prefix = 0;                 // prefix LENGTH into the file's single token vector
    uint64_t used = 0;
    bool pinned = false;
    int64_t stage_parts = 0;
    PromptPlan plan;
    // Pair metadata:
    uint64_t ring_id = 0, ring_bytes = 0, ring_checksum = 0;
    int64_t ring_cells = 0, ring_window = 0, ring_capacity = 0;
    int64_t ring_committed_end = -1, ring_appended_through = -1;
};
struct SessionManifestV2 {
    // ...
    std::vector<int32_t> tokens;                    // stored ONCE
    std::vector<ConversationImageKey> images;
    std::vector<SessionCheckpointMeta> checkpoints; // one per checkpoint, with its prefix
    std::vector<SessionRegion> regions;             // includes checkpoint_state regions per checkpoint
};
```

The manifest stores **every checkpoint's `prefix`** and its paired draft metadata. The `regions` array includes `checkpoint_state` regions (kind=3) indexed by checkpoint ordinal and stage.

### Publish path (`generate.cpp:14226-14228`, `conversation_disk_cache_test.cpp:177-183`):

```cpp
// In disk_park lambda, after building manifest:
for (const auto& cm : m.checkpoints) {
    meta.anchor = std::max<int64_t>(meta.anchor, (int64_t) cm.prefix);
    if (cm.ring_id) {
        meta.pair_prefix = std::max<int64_t>(meta.pair_prefix, (int64_t) cm.prefix);
        meta.ring_ids.push_back(cm.ring_id);
    }
}
meta.tail_draft = s.draft;
```

So `anchor` = deepest checkpoint prefix, `pair_prefix` = deepest checkpoint **with a legal pair** (`ring_id != 0`).

### Current restore path (`generate.cpp:14285-14853`):

`disk_restore()` reads the manifest via `session_file_manifest_v2()` (pre-mutation), validates layout/draft/regions, then streams **ALL regions** into live pools via `SessionRegionSink`. It returns `tokens_out = T` (full generation length). There is **no `target_prefix` parameter** — it always restores the full generation.

### Conclusion:
- **In-memory index** has the scalars needed for lookup (`anchor`, `pair_prefix`).
- **File manifest** has the full checkpoint metadata and regions to restore a specific checkpoint.
- **Today's `disk_restore` only does full-generation restore.** A `target_prefix` hook must be added to select the checkpoint's `checkpoint_state` regions and truncate KV at that prefix.

---

## 2. EXACT MATCH SEMANTICS FOR THE FIX

### New return type (`conversation_disk_cache.hpp`):

```cpp
struct DiskBestMatch {
    const DiskGeneration* gen = nullptr;
    int64_t match_tokens = 0;          // prefix length that matched (≤ prompt.size())
    bool from_anchor = false;          // true if match_tokens == gen->anchor
    bool from_pair = false;            // true if match_tokens == gen->pair_prefix
};
```

### Candidate checks per generation (in **descending depth order**):

| Candidate | Length `L` | Condition | Checks |
|-----------|------------|-----------|--------|
| Full generation | `g.tokens.size()` | `L < prompt.size()` | cvec, `std::equal(g.tokens[0..L), prompt[0..L))`, `disk_images_below(images, L) == g.images` |
| Anchor checkpoint | `g.anchor` | `anchor > 0 && anchor < prompt.size()` | cvec, `std::equal(g.tokens[0..anchor), prompt[0..anchor))`, `disk_images_below(images, anchor) == g.images[0..anchor)` |
| Pair checkpoint | `g.pair_prefix` | `pair_prefix > 0 && pair_prefix < prompt.size()` | cvec, `std::equal(g.tokens[0..pair_prefix), prompt[0..pair_prefix))`, `disk_images_below(images, pair_prefix) == g.images[0..pair_prefix)` |

**Note:** `disk_images_below` already takes a length `n` and returns images with `start < n` (`conversation_disk_cache.hpp:87-92`).

### Tie-breaking (matching RAM cache `ConversationCache::best`, `conversation_cache.hpp:326-341`):

1. **Deepest match wins** (highest `match_tokens`).
2. **On equal depth: newest `used` stamp wins** — `gens_` vector is in use-stamp order (least recent first), so walking with `>=` keeps the newest (see `conversation_disk_cache.hpp:168-170`).

### What the caller should use:

- `match_tokens` → `disk_tokens` (replaces `gen->tokens.size()`)
- `from_anchor` / `from_pair` → passed to `disk_restore()` to select draft restore strategy
- `gen->path`, `gen->serial` → unchanged

---

## 3. CALLER INTEGRATION

### Current caller (`generate.cpp:19341-19361`):

```cpp
const strata::core::DiskGeneration* gen = disk->best(ids, req_imgs, want_cvec);
if (gen != nullptr) {
    disk_tokens = (int64_t) gen->tokens.size();   // BUG: uses full length
    disk_start = gen->path;
    disk_serial = gen->serial;
}
const bool disk_wins = disk_tokens > std::max(std::max(resume, slot_tokens), parked.tokens);
if (disk_wins) { disk->guard(disk_serial); /* ... */ }
```

Later at `generate.cpp:19762`:

```cpp
const int rc = disk_restore(disk_start, disk_serial, *sid, kv_quiesce,
                            disk_moving, disk_blocking, dtokens, dwhy,
                            disk->budget(), dreport);
```

### Required changes:

**A. `generate.cpp:19341` — consume new return type:**

```cpp
auto disk_match = disk->best(ids, req_imgs, want_cvec);
if (disk_match.gen != nullptr) {
    disk_tokens = disk_match.match_tokens;   // use match length, not full length
    disk_start = disk_match.gen->path;
    disk_serial = disk_match.gen->serial;
    disk_from_anchor = disk_match.from_anchor;
    disk_from_pair = disk_match.from_pair;
}
const bool disk_wins = disk_tokens > std::max(std::max(resume, slot_tokens), parked.tokens);
```

**B. `disk_restore` signature — add `target_prefix` and checkpoint flags:**

```cpp
// New signature (line ~14285):
auto disk_restore = [&](const std::string& path, uint64_t serial,
                        const strata::core::SessionFileIdentity& id,
                        const std::function<void()>& quiesce,
                        const std::function<void(uint64_t, uint64_t)>& moving,
                        const std::function<void(const char*, uint64_t)>& blocking,
                        int64_t& tokens_out, std::string& why, uint64_t max_file_bytes,
                        std::string& report,
                        int64_t target_prefix,          // NEW: the matched prefix length
                        bool from_anchor,               // NEW
                        bool from_pair) -> int;         // NEW
```

**C. `disk_restore` implementation — truncated restore strategy:**

When `target_prefix < T` (full generation length):

1. **Find the checkpoint** in `manifest.checkpoints` with `prefix == target_prefix` (must exist: anchor/pair_prefix come from manifest).
2. **Live state** ← that checkpoint's `checkpoint_state` regions (not the generation's `live_state` regions).
3. **KV** ← stream KV regions but **truncate writes at `target_prefix` tokens** (the KV machinery already supports this via `conversation_kv_stream_write` with extent parameter).
4. **Checkpoints** ← restore only checkpoints up to and including the matched one (their `ids` vectors are already sliced to `prefix` in `built` vector at `generate.cpp:14456-14459`).
5. **Draft ring:**
   - If `from_pair`: restore the paired ring at that checkpoint (existing `pair_pick` logic at `generate.cpp:14422-14435` already does this for the full-generation case; adapt to use `target_prefix`).
   - Else if `tail_draft && target_prefix == T`: restore tail ring (current behavior).
   - Else: no draft restore (target-only).
6. Set `tokens_out = target_prefix`, `resume = target_prefix`.
7. Classification report: `"restored from disk (checkpoint at N)"` vs `"restored from disk (full generation)"`.

**D. Call site (`generate.cpp:19762`):**

```cpp
const int rc = disk_restore(disk_start, disk_serial, *sid, kv_quiesce,
                            disk_moving, disk_blocking, dtokens, dwhy,
                            disk->budget(), dreport,
                            disk_match.match_tokens,      // NEW
                            disk_match.from_anchor,       // NEW
                            disk_match.from_pair);        // NEW
```

---

## 4. THE .SESS FORMAT QUESTION

**Are anchor/pair token spans in the file's index/header such that `best()` could match them without streaming the whole file?**

**YES — the in-memory `DiskGeneration` index already carries `anchor` and `pair_prefix` as scalars.** These are computed at publish time from the manifest's checkpoint `prefix` values (`generate.cpp:14226-14228`). No file read is needed for the lookup — `best()` runs purely against the in-memory `gens_` vector.

The manifest (read during `disk_restore`'s pre-mutation pass at `generate.cpp:14318-14328`) contains the full `SessionCheckpointMeta` array with every checkpoint's `prefix`, and the `regions` array with `checkpoint_state` regions keyed by checkpoint ordinal. So the restore can select the correct checkpoint's regions by matching `prefix == target_prefix`.

**No file-format change is needed.** The data is already there; the code just needs to use it.

---

## 5. LOGGING — Existing Refusal/Why Patterns

### Disk restore skip log (caller, `generate.cpp:19770`):

```cpp
std::fprintf(stderr, "strata serve: conversation cache: disk restore skipped (%s); rereading the prompt\n", dwhy.c_str());
```

### Disk park skip log (`generate.cpp:14931`):

```cpp
std::fprintf(stderr, "strata serve: conversation cache: disk park skipped (%s)\n", dwhy.c_str());
```

### RAM restore skip log (`generate.cpp:19417`):

```cpp
std::fprintf(stderr, "strata serve: conversation cache: skip restore (%s has %lld MiB free and no copy stream yet, which needs up to %lld MiB); rereading the prompt\n", ...);
```

### `disk_restore` pre-mutation refusals (return 1, safe miss) — `why` strings at:
- `generate.cpp:14333`: "the conversation is outside this engine's context"
- `generate.cpp:14335`: "an image key starts past the stored tokens"
- `generate.cpp:14343`: "the drafter's ring in the file is not this drafter's..."
- `generate.cpp:14348`: "the file holds a drafter this engine does not have"
- `generate.cpp:14381`: `disk_build_spec` failure
- `generate.cpp:14387-14399`: region directory mismatch
- `generate.cpp:14446`: "the pair region is not this drafter's ring extent at its checkpoint"
- `generate.cpp:14452`: RAM admission failure
- `generate.cpp:14492`: "the K/V cannot grow to the conversation on disk: no VRAM is left"
- `generate.cpp:14518`: copy stream preflight failure

### New trace log for `best()` skipping a generation whose full length > prompt but anchor/pair could match (debug level):

```cpp
// Inside DiskCache::best(), when L >= prompt.size() but anchor/pair_prefix < prompt.size():
// (only if a debug/trace flag is enabled — not in hot path by default)
```

---

## 6. REGRESSION TEST

### Test location:
`tests/core/conversation_disk_cache_test.cpp` — CPU-only, no GPU, no model.

### New test case: `DiskCache_best_checkpoint_match`

```cpp
void test_best_checkpoint_match() {
    const fs::path d = dir("best_ckpt");
    std::string why;
    DiskCache c(opts(d, 256u << 20));
    check(open(c, why), "best_ckpt: open");

    // Publish a generation with:
    //   full tokens = 1000
    //   checkpoints at prefixes 200, 500, 800
    //   anchor = 800, pair_prefix = 500 (checkpoint at 500 has a pair)
    const Spec gen{1000, 1, {}, true, true, true, 1u << 13, 1, false};
    // Need to customize build() to set specific checkpoint prefixes and pair at 500
    // For the test, we can use the existing park() helper with a custom manifest

    Parked p;
    check(park(c, gen, why, &p), "best_ckpt: park generation");

    // Prompt of 600 tokens matching first 600 of the generation
    // Should match at pair_prefix = 500 (deepest valid checkpoint < 600)
    auto prompt = prompt_of(gen);
    prompt.resize(600);

    const DiskGeneration* g = c.best(prompt, {}, true);
    check(g != nullptr && g->serial == p.serial, "best_ckpt: generation found");

    // With the FIX: best() returns DiskBestMatch with match_tokens=500, from_pair=true
    // Without fix: best() returns nullptr (1000 >= 600)
}
```

**Note:** The test fixture `park()` uses a fixed checkpoint layout (at `n/2` and `n`). To test specific anchor/pair values, the test should build a custom `SessionManifestV2` with desired checkpoint prefixes and write it directly via `session_file_write_v2`, then call `DiskCache::open()` to index it, then call `best()`.

### Assertions:
1. `best()` returns a match with `match_tokens == 500` (pair_prefix), **not** 1000.
2. `from_pair == true`, `from_anchor == false`.
3. Same cvec/images checks apply.
4. Supersession behavior unchanged: `retire_superseded` still uses full token prefix (as today at `conversation_disk_cache.hpp:178`).

---

## IMPLEMENTATION PLAN — Step by Step

### Files to change:
1. `include/strata/core/conversation_disk_cache.hpp` — new `DiskBestMatch` struct, `best()` return type
2. `src/core/conversation_disk_cache.cpp` — implement new `best()` (template)
3. `src/program/generate.cpp` — caller integration + `disk_restore` truncated restore

> **Note:** `DiskCache::best()` is a template in the header (line 156). The implementation stays in the header.

### Step 1: `conversation_disk_cache.hpp` — Add `DiskBestMatch` and change `best()`

```cpp
// After DiskGeneration struct (around line 57)
struct DiskBestMatch {
    const DiskGeneration* gen = nullptr;
    int64_t match_tokens = 0;
    bool from_anchor = false;
    bool from_pair = false;
};

// Change best() signature (line 156):
template<class Token>
DiskBestMatch best(const std::vector<Token>& prompt,
                   const std::vector<ConversationImageKey>& images,
                   bool cvec) const;
```

### Step 2: Implement new `best()` in header (replaces lines 156-173)

```cpp
template<class Token>
DiskBestMatch best(const std::vector<Token>& prompt,
                   const std::vector<ConversationImageKey>& images,
                   bool cvec) const {
    DiskBestMatch best_match;
    for (const auto& g : gens_) {
        if (g.cvec != cvec) continue;

        // Candidate 1: full generation
        const int64_t L_full = (int64_t) g.tokens.size();
        if (L_full >= 1 && L_full < (int64_t) prompt.size()) {
            if (std::equal(g.tokens.begin(), g.tokens.end(), prompt.begin())) {
                std::vector<ConversationImageKey> under;
                disk_images_below(images, L_full, under);
                if (under == g.images) {
                    if (best_match.gen == nullptr || L_full > best_match.match_tokens ||
                        (L_full == best_match.match_tokens && g.used >= best_match.gen->used)) {
                        best_match = {&g, L_full, false, false};
                    }
                }
            }
        }

        // Candidate 2: anchor checkpoint (deeper than pair)
        if (g.anchor > 0 && g.anchor < (int64_t) prompt.size()) {
            if (std::equal(g.tokens.begin(), g.tokens.begin() + g.anchor, prompt.begin())) {
                std::vector<ConversationImageKey> under;
                disk_images_below(images, g.anchor, under);
                // g.images contains ALL images; filter to those below anchor
                std::vector<ConversationImageKey> expected;
                for (const auto& img : g.images) if (img.start < g.anchor) expected.push_back(img);
                if (under == expected) {
                    if (best_match.gen == nullptr || g.anchor > best_match.match_tokens ||
                        (g.anchor == best_match.match_tokens && g.used >= best_match.gen->used)) {
                        best_match = {&g, g.anchor, true, false};
                    }
                }
            }
        }

        // Candidate 3: pair checkpoint
        if (g.pair_prefix > 0 && g.pair_prefix < (int64_t) prompt.size()) {
            if (std::equal(g.tokens.begin(), g.tokens.begin() + g.pair_prefix, prompt.begin())) {
                std::vector<ConversationImageKey> under;
                disk_images_below(images, g.pair_prefix, under);
                std::vector<ConversationImageKey> expected;
                for (const auto& img : g.images) if (img.start < g.pair_prefix) expected.push_back(img);
                if (under == expected) {
                    if (best_match.gen == nullptr || g.pair_prefix > best_match.match_tokens ||
                        (g.pair_prefix == best_match.match_tokens && g.used >= best_match.gen->used)) {
                        best_match = {&g, g.pair_prefix, false, true};
                    }
                }
            }
        }
    }
    return best_match;
}
```

**Note:** Check order is full → anchor → pair, but tie-breaking by depth ensures deepest wins regardless of check order. The `used` stamp tie-break matches RAM cache behavior.

### Step 3: `generate.cpp` — Update caller at line 19341

```cpp
// Replace lines 19341-19361:
auto disk_match = disk->best(ids, req_imgs, want_cvec);
bool disk_from_anchor = false, disk_from_pair = false;
if (disk_match.gen != nullptr) {
    disk_tokens = disk_match.match_tokens;
    disk_start = disk_match.gen->path;
    disk_serial = disk_match.gen->serial;
    disk_from_anchor = disk_match.from_anchor;
    disk_from_pair = disk_match.from_pair;
}
const bool disk_wins = disk_tokens > std::max(std::max(resume, slot_tokens), parked.tokens);
```

### Step 4: `generate.cpp` — Modify `disk_restore` lambda (line 14285)

**Add parameters:**
```cpp
int64_t target_prefix,   // the matched prefix length
bool from_anchor,
bool from_pair,
```

**Inside `disk_restore`:**

1. **After manifest parse** (`generate.cpp:14332`): `T = manifest.tokens.size()`; keep `T` as full length.
2. **Find target checkpoint:**
   ```cpp
   int64_t target_ckpt_idx = -1;
   for (size_t i = 0; i < manifest.checkpoints.size(); ++i) {
       if ((int64_t) manifest.checkpoints[i].prefix == target_prefix) {
           target_ckpt_idx = (int64_t) i;
           break;
       }
   }
   if (target_prefix < T && target_ckpt_idx < 0) { why = "checkpoint for target prefix not in manifest"; return 1; }
   ```
3. **Build `cps` (SessionStreamCheckpoint) only for checkpoints up to `target_ckpt_idx`** (or all if `target_prefix == T`).
4. **For `target_prefix < T`:** Use `checkpoint_state` regions for `target_ckpt_idx` as the "live state" source. The `spec` building (`disk_build_spec`) must be called with the target prefix's checkpoint state as the base.
5. **KV truncation:** The `disk_build_spec` / `disk_dflash_sizes` calls should use `target_prefix` instead of `T` for the extent calculations. The `conversation_kv_stream_write` already takes an extent parameter.
6. **Draft restore strategy:**
   - If `from_pair`: use existing `pair_pick` logic but constrained to `target_ckpt_idx`.
   - If `from_anchor` (no pair): no draft restore (target-only).
   - If `target_prefix == T`: current behavior (tail ring if `tail_draft`).
7. **Report classification:** Include checkpoint info.

### Step 5: Add trace logging in `best()` (optional, behind debug flag)

```cpp
// When full generation skipped but anchor/pair could match:
if (L_full >= (int64_t) prompt.size() && (g.anchor > 0 || g.pair_prefix > 0)) {
    // Only if STRATA_DISK_CACHE_TRACE=1 or similar
}
```

### Step 6: Add regression test in `conversation_disk_cache_test.cpp`

Add `test_best_checkpoint_match()` and call it in `main()`.

---

## FLAGS — What Needs File-Format Change vs Code-Only

| Item | Status |
|------|--------|
| In-memory index (`anchor`, `pair_prefix`) | **Already exists** — code-only |
| Manifest checkpoint `prefix` | **Already in v2 format** — code-only |
| Manifest `checkpoint_state` regions per checkpoint | **Already in v2 format** — code-only |
| `disk_restore` truncated restore | **Code-only** — uses existing region streaming, KV extent params |
| `DiskBestMatch` return type | **Code-only** — new struct |
| `best()` checkpoint-aware logic | **Code-only** |
| Regression test | **Code-only** |

**No file-format change required.** All data structures already carry the necessary information.

---

## SUMMARY

**Decisive question answer:** **Checkpoint-restorable today: NO** — `disk_restore` only restores full generations. But the **data to enable it is already present** in the in-memory index (`anchor`, `pair_prefix`) and the on-disk manifest (checkpoint `prefix` values, `checkpoint_state` regions). The fix is **code-only**: extend `DiskCache::best()` to consider anchor/pair candidates, thread the matched prefix through to `disk_restore()`, and implement truncated restore using existing region-streaming and KV extent machinery.

**Citations:**
- `DiskGeneration` struct: `include/strata/core/conversation_disk_cache.hpp:41-57`
- `anchor`/`pair_prefix` publish: `src/program/generate.cpp:14226-14228`
- Manifest `SessionCheckpointMeta`: `include/strata/core/conversation_file.hpp:335-350`
- Current `best()`: `include/strata/core/conversation_disk_cache.hpp:156-173`
- RAM cache `best()` (reference): `include/strata/core/conversation_cache.hpp:326-341`
- `disk_restore` entry: `src/program/generate.cpp:14285`
- Caller lookup: `src/program/generate.cpp:19341-19361`
- Caller restore: `src/program/generate.cpp:19762`
- Test file: `tests/core/conversation_disk_cache_test.cpp`