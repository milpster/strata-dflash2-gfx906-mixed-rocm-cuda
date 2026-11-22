// The caller-side halves of a format-v2 streaming session (conversation_file.hpp): the region list for one parked
// conversation, the non-owning region sources that feed the writer, and the region-sink dispatcher that routes a
// reader's bytes to their destinations.
//
// Pure host code: it never reads a device.  The engine supplies the byte counts its own sizing functions report for
// each stage (conversation_session_sizes, conversation_kv_part_sizes) and the callbacks that reach its pools, so the
// geometry rules live once in the sizing functions and the disk layout rules live once in format v2.
//
// Nothing here allocates a conversation's payload: a source is a view of bytes somebody else owns (a device pool, a
// host checkpoint vector), and a sink writes bytes the reader hands it, block by block, into storage the caller owns.
#pragma once

#include "strata/core/conversation_file.hpp"

#include <array>
#include <cstdint>
#include <functional>
#include <string>
#include <utility>
#include <vector>

namespace strata::core {

// One logical stage's byte counts, as the engine's sizing functions report them for that stage's carve.  Zero-size
// parts are declared too: format v2 requires every region of a layer to be named.
struct SessionStreamStage {
    SessionStageLayout layout{};                                    ///< layer range + KV format
    std::array<uint64_t, kSessionStateParts> live{};               ///< gdn, ple, tails, dead, block_pos
    std::vector<std::array<uint64_t, kSessionKvParts>> kv;         ///< one row per owned K/V layer, in layer order
    std::array<uint64_t, kSessionKvParts> mtp{};                   ///< the drafter's layer on this stage (when has_mtp)
    bool has_mtp = false;
};

// One checkpoint's body sizes: [stage][part], in the layout's stage order.  Stage 0 is the checkpoint's own running
// state; a later stage's row is the checkpoint's part for that stage.  `meta.stage_parts` says whether the later
// stages' rows are saved.
struct SessionStreamCheckpoint {
    SessionCheckpointMeta meta{};
    std::vector<std::array<uint64_t, kSessionStateParts>> state;
};

// Everything one parked conversation contributes to a v2 generation, as byte counts and identity metadata.  The
// engine fills it from the live session, the checkpoint chain, the K/V pools and the drafter - it is never a copy
// of the conversation's state.
struct SessionStreamSpec {
    std::array<int64_t, 18> geometry{};
    std::vector<SessionStreamStage> stages;          ///< stage 0 first, in the layout's order
    SessionDraftIdentity draft{};                    ///< type 0: no draft in this generation
    bool cvec = true;
    std::vector<int32_t> tokens;
    std::vector<ConversationImageKey> images;
    std::vector<SessionStreamCheckpoint> checkpoints;
    bool has_dflash = false;                         ///< the drafter's physical ring travels as one region
    uint64_t dflash_bytes = 0;
};

// The v2 manifest for a spec: identities, the token/image directory, the checkpoint references and the region
// directory (canonical order, sizes as given, offsets and checksums left for the writer).  Every inconsistency
// between the spec's sizes and format v2's rules is refused here, before a file is opened.
bool session_stream_manifest(const SessionStreamSpec& spec, SessionManifestV2& out, std::string& error);

// The reader every region's bytes come from: fill [offset, offset+n) of one region.  The engine's reader switches on
// the region's kind/stage/index/part; it must keep the source unchanged until the file is published.
using SessionStreamReader = std::function<bool(const SessionRegion& region, uint64_t offset, void* dst, size_t n)>;
// One source per declared region, in the manifest's order, all reading through `read`.  The sizes come from the
// manifest, so a source can never disagree with what the file declares.
bool session_stream_sources(std::vector<SessionRegionSource>& out, const SessionManifestV2& manifest,
                            const SessionStreamReader& read, std::string& error);

// A non-owning view of host bytes (a checkpoint body, a pinned host pool): the bytes stay where they are.
SessionRegionSource session_stream_host_source(int32_t kind, int64_t stage, int64_t index, int64_t part,
                                               const void* bytes, uint64_t size);

// One region as concatenated pieces, in order: the dflash ring (one region, the ring states' bytes in state order)
// and any other payload the destination holds in several buffers.
struct SessionStreamPiece {
    uint64_t size = 0;
    std::function<bool(uint64_t offset, void* dst, size_t n)> read;
};
bool session_stream_concat_source(SessionRegionSource& out, int32_t kind, int64_t stage, int64_t index, int64_t part,
                                  std::vector<SessionStreamPiece> pieces, std::string& error);

// A K/V layer's streamed source (conversation_kv_source, the pool access path) as its five region sources, appended
// in part order under (kind, stage, index).
bool session_stream_kv_sources(std::vector<SessionRegionSource>& out, int32_t kind, int64_t stage, int64_t index,
                               const SessionKvSource& kv, std::string& error);

// One destination of a streaming restore: the regions it takes, the check it makes in the validation pass, the
// writes of the transfer pass, and its finish (a stage synchronizes its device here).  `index` -1 takes every index
// of its kind and stage (a stage's layers, a chain's checkpoints).
struct SessionStreamDestination {
    int32_t kind = 0;
    int64_t stage = 0, index = 0;
    std::function<bool(const SessionRegion& region, std::string& why)> validate;
    std::function<bool(const SessionRegion& region, uint64_t offset, const void* bytes, size_t n, std::string& why)> write;
    std::function<bool(std::string& why)> finish;
};
// Route a v2 reader's regions to those destinations.  validate_region is asked for EVERY region before any byte is
// written (format v2's order), so a region nothing accepts, a destination that refuses an extent, and a checksum
// failure all leave every destination untouched.  finish runs per destination in the given order.
bool session_stream_sink(std::vector<SessionStreamDestination> dests, SessionRegionSink& sink, std::string& error);

} // namespace strata::core
