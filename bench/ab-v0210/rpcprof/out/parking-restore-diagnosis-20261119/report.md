# Parking Restore Diagnosis Report

**Commit analyzed:** 42cb41b314  
**Date:** 2026-11-19  
**Test case:** T3 checkpoint restart recovery (parking-validation-20261119-173003)

---

## 1. Request-Path Trace (file:line evidence)

When a `/v1/chat/completions` request arrives, the serve loop in `src/program/generate.cpp` performs conversation cache lookups in this order:

### 1.1 Live session + checkpoint chain (lines 19280–19322)
- `resume` = deepest prefix from live session's checkpoints that matches the prompt
- `slot_tokens` / `slot_ck` = best match from batch slots (if `--batch` enabled)
- Image-key mismatch detection (#2228) logs a warning but falls back to shorter prefix

### 1.2 RAM-tier parked conversations (line 19324)
```cpp
const auto parked = conversations.best(ids, req_imgs, want_cvec);
```
- `ConversationCache::best()` (include/strata/core/conversation_cache.hpp:326–341) iterates all parked entries' **live state AND every checkpoint**, returns `Match{index, tokens, live, serial}` with the deepest token prefix.

### 1.3 Disk-tier published generations (lines 19333–19346)
```cpp
if (disk_on()) {
    const strata::core::DiskGeneration* gen = disk->best(ids, req_imgs, want_cvec);
    if (gen != nullptr) {
        disk_tokens = (int64_t) gen->tokens.size();   // BUG: uses full generation length
        disk_start = gen->path;
        disk_serial = gen->serial;
    }
}
```
- `DiskCache::best()` (include/strata/core/conversation_disk_cache.hpp:156–173) iterates `gens_` vector (index rebuilt at boot from committed manifests only).
- **Critical limitation:** it only considers `g.tokens.size()` (the **full conversation length at publish time**), not the checkpoints *inside* the generation.

### 1.4 Depth competition (lines 19348–19361)
```cpp
const bool disk_wins = disk_tokens > std::max(std::max(resume, slot_tokens), parked.tokens);
if (!disk_wins) disk_start.clear();
if (disk_wins) { disk->guard(disk_serial); /* ... */ }
```
- Disk generation only wins if its **full token length** exceeds every other source's prefix.
- If `disk_wins`, `disk_restore()` (line 14285) is called later (lines 19748–19783) to stream the generation into live pools.

### 1.5 Disk restore path (lines 14285–14853)
- `disk_restore()` reads manifest via `session_file_manifest_v2()` (pre-mutation validation).
- Validates layout, draft identity, region directory, KV growth, copy-stream readiness.
- On success: streams all regions (live state, KV, draft ring, checkpoints) into live pools via `SessionRegionSink`.
- Returns `tokens_out = T` (full generation length) and a classification report.

---

## 2. Root Cause (proven)

### 2.1 What happened in T3
| Event | Tokens | Details |
|-------|--------|---------|
| Conv-50 published (idle-save) | 16,148 | `anchor=16,015`, `pair=16,015`, `tail-draft=1` |
| Production stopped & rebooted | — | Same binary, same config, same cache dir |
| Boot scan | — | `generations=4 bytes=2334 sidecars=4 corrupt=0 foreign=0` — conv-50 **was indexed** |
| First continuation request (T3) | 16,133 | **Prompt shorter than published generation** (16,133 < 16,148) |

### 2.2 Why the disk tier was not consulted
In `DiskCache::best()` (conversation_disk_cache.hpp:156–173):
```cpp
const int64_t L = (int64_t) g.tokens.size();
if (L < 1 || L >= (int64_t) prompt.size()) continue;   // ← conv-50: L=16148, prompt=16133 → SKIPPED
```
The generation's **full token length (16,148)** exceeds the prompt length (16,133), so the `continue` fires and conv-50 is never considered — **even though its `anchor=16,015` is a valid, compatible prefix of the prompt**.

No other generation in the index had a full length ≤ 16,133 (earlier generations were retired by `retire_superseded` at conv-55+). Result: `disk->best()` returns `nullptr`, `disk_tokens=0`, `disk_wins=false`, and the disk restore path is **never entered**. No log line is emitted — the silence is itself the defect.

### 2.3 Why the guarantee was violated
The documented guarantee (generate.cpp:1113–1115, `--conversation-cache-disk-idle-save` help):
> "Guarantee: a restart reuses the latest compatible PUBLISHED checkpoint - a tail that was never published (or was advanced after the last publish) is read again."

The "latest compatible PUBLISHED checkpoint" for a 16,133-token prompt is conv-50's **anchor at 16,015 tokens** — but the lookup only examines the generation's *full* token vector, not its checkpoints. The RAM cache (`ConversationCache::best`) correctly considers every checkpoint (line 337–338); the disk cache does not.

### 2.4 Candidates eliminated
- **(a) RAM-side record cleared at restart** — No. The boot scan indexed conv-50 (generations=4, bytes=2334 MiB includes it). The index exists; the lookup logic ignores it.
- **(b) Silent compatibility gate** — No. `disk_restore()`'s pre-mutation checks (layout, draft, KV growth, etc.) are never reached because `best()` returns `nullptr`. The refusal logging in `disk_restore` (line 14327, 19770) is correct but moot.
- **(c) Restore path only wired for different entry point** — No. `disk_restore` is called from the stateless request path (line 19762) and works for within-boot restores (T1/T2 passed). The gap is purely in the *lookup*.
- **(d) Cross-backend / identity hash mismatch** — No. Boot scan accepted the files (`corrupt=0 foreign=0`); `SessionV2Requirement` uses `check_draft=false` at both scan and restore.

---

## 3. Minimal Fix Plan

### 3.1 Files to change
1. `include/strata/core/conversation_disk_cache.hpp` — extend `DiskCache::best()` return type and logic
2. `src/core/conversation_disk_cache.cpp` — implement new `best()`
3. `src/program/generate.cpp` — consume new return value, pass `target_prefix` to `disk_restore()`
4. `src/program/generate.cpp` — modify `disk_restore()` to restore up to `target_prefix` (checkpoint) instead of full generation

### 3.2 Shape of the change

#### A. New match result type (conversation_disk_cache.hpp)
```cpp
struct DiskBestMatch {
    const DiskGeneration* gen = nullptr;
    int64_t match_tokens = 0;          // prefix length that matched (≤ prompt.size())
    bool from_anchor = false;          // true if match_tokens == gen->anchor
    bool from_pair = false;            // true if match_tokens == gen->pair_prefix
};
```

#### B. `DiskCache::best()` returns `DiskBestMatch` (conversation_disk_cache.hpp:156, .cpp)
- Check three candidates in descending depth order:
  1. Full generation tokens (current behavior) — if `L < prompt.size()` and prefix matches
  2. `gen->anchor` (deepest checkpoint) — if `anchor > 0 && anchor < prompt.size()` and prefix matches
  3. `gen->pair_prefix` (deepest checkpoint with legal pair) — same condition
- Pick the deepest match across all generations (tie-break: most recent `used` stamp, as today).
- Return `DiskBestMatch{gen, match_tokens, from_anchor, from_pair}` or empty.

#### C. Caller in generate.cpp (lines 19341–19361)
```cpp
auto disk_match = disk->best(ids, req_imgs, want_cvec);
if (disk_match.gen != nullptr) {
    disk_tokens = disk_match.match_tokens;   // use match length, not full length
    disk_start = disk_match.gen->path;
    disk_serial = disk_match.gen->serial;
}
const bool disk_wins = disk_tokens > std::max(std::max(resume, slot_tokens), parked.tokens);
```

#### D. `disk_restore()` accepts `target_prefix` (generate.cpp:14285)
- New parameter: `int64_t target_prefix` (the `match_tokens` from best match).
- After reading manifest, find the checkpoint with `prefix == target_prefix` (must exist: anchor/pair_prefix come from manifest).
- **Restore strategy for `target_prefix < T` (full generation length):**
  - Live state ← that checkpoint's `checkpoint_state` regions (not the generation's `live_state`)
  - KV ← stream KV regions but **only up to `target_prefix` tokens** (truncate writes at `target_prefix`)
  - Checkpoints ← restore only checkpoints up to and including the matched one
  - Draft ring ← if `from_pair`, restore the paired ring at that checkpoint; else if `tail_draft` and `target_prefix == T`, restore tail ring; else no draft restore
- Set `tokens_out = target_prefix`, `resume = target_prefix`.
- All existing validation/refusal logging remains; add log line when restoring from checkpoint vs full generation.

#### E. Refusal logging (already present, now reachable)
- `disk_restore` returns `1` (safe miss) with `why` for any pre-mutation failure.
- Caller at line 19770 logs: `"disk restore skipped (%s); rereading the prompt"`.
- **New:** log when a generation is skipped by `best()` because its full length > prompt but anchor/pair could have matched (debug/trace level).

### 3.3 Default path byte-identical
- When prompt ≥ generation length (normal growing conversation), `match_tokens == gen->tokens.size()`, `target_prefix == T` — restore behavior unchanged.
- RAM cache lookup, batch slots, checkpoint resumes, park/evict logic untouched.
- Only the disk-tier lookup+restore gains checkpoint-awareness.

---

## 4. Regression Test Plan

### 4.1 Unit test (src/core/conversation_cache_test.cpp style)
Add test case `DiskCache_best_checkpoint_match`:
- Create `DiskCache` with temp dir, publish a generation with:
  - Full tokens: 1000
  - Checkpoints at prefixes: 200, 500, 800 (anchor=800, pair_prefix=500)
- Call `best()` with prompt of 600 tokens matching the first 600 of the generation.
- Assert: returns match with `match_tokens == 500` (pair_prefix) or `800` (anchor) — **not** 1000.
- Verify `from_anchor` / `from_pair` flags correct.

### 4.2 Integration test (tools/test_conversation_cache_*.py style)
- Start engine with `--conversation-cache-dir --conversation-cache-disk-mib --conversation-cache-disk-idle-save`.
- Run a conversation to ~2000 tokens, let idle-save publish (generation length > prompt at next turn).
- Restart engine (same binary, same cache dir).
- Send continuation with **shorter prompt** (simulate history rewrite or truncation).
- Assert: response shows reused tokens = anchor/pair_prefix (not 0), wall time << cold read.
- Log must contain `"restored from disk"` with classification `"complete target+draft hit (pair: checkpoint at ...)"` or similar.

### 4.3 Testability notes
- `DiskCache::best()` is pure host code — fully unit-testable without GPU.
- `disk_restore()` requires live pools; integration test needs `--serve` boot.
- Existing `conversation_disk_cache_test.cpp` covers scan/publish/evict; extend for `best()` checkpoint matching.

---

## 5. Confidence Notes

| Aspect | Verified | Inferred |
|--------|----------|----------|
| Boot scan indexed conv-50 | ✓ Log: `generations=4 bytes=2334` at restart | |
| Conv-50 anchor=16015, full=16148 | ✓ Park log: `anchor=16015 pair=16015` | |
| T3 prompt=16133 < 16148 | ✓ turns.jsonl: `prompt_tokens: 16133` | |
| `DiskCache::best()` skips L ≥ prompt | ✓ Code: line 162 `if (L >= prompt.size()) continue` | |
| No restore attempt logged | ✓ Engine log: no `disk restore` line, only cold read | |
| Within-boot restores work | ✓ T1/T2 PASS, log shows `restored 16015 reused` | |
| `disk_restore` supports prefix restore | | Partial: `conversation_snapshot_restore_prefix` exists for RAM; disk path would need adaptation |
| No cross-backend / identity issue | ✓ Boot scan `corrupt=0 foreign=0` | |
| Fix scope localized to disk lookup+restore | ✓ RAM cache, batch, park logic separate | |

**Confidence:** High. The root cause is a single logic gap in `DiskCache::best()` (ignoring checkpoints inside generations). The fix is surgical: extend the lookup to consider anchor/pair_prefix, and thread the match length through to a checkpoint-aware restore. All other machinery (manifest format, checkpoint regions, prefix restore primitives) already exists.

---

## Summary

**Root cause:** `DiskCache::best()` only matches a generation's *full* token length against the prompt. When a published generation (16,148 tokens) is longer than the next request's prompt (16,133 tokens) — which occurs when a conversation is rewritten/truncated after an idle-save — the generation is skipped entirely. Its valid checkpoint (anchor=16,015) is never offered, so the disk restore path is never entered and the request is served cold with no log explanation.

**Fix:** Make `DiskCache::best()` checkpoint-aware (like `ConversationCache::best()`). Return the deepest matching prefix (full tokens, anchor, or pair_prefix) and thread that length to `disk_restore()`, which restores the corresponding checkpoint state + truncated KV instead of the full generation. Add refusal logging at every decision point. Default growing-conversation path unchanged.