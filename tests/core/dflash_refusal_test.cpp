// tests/core/dflash_refusal_test.cpp - CPU checks for the DFlash draft proposal's failure
// classification (include/strata/core/dflash_refusal.hpp).  These call the PRODUCTION functions the
// emitter in src/core/dflash_runtime.cpp calls and the two decode call sites in
// src/program/generate.cpp consume: the vocabulary predicate on the mapped host candidate ids, the
// typed fatality classification, and the validate-then-copy host handoff.  No mock logic and no
// device data: the drafter's candidates are host ints (mapped host memory in the runtime), so the
// whole seam is provable with no GPU.
//
// Evidence is counted and printed in THREE separated kinds, so no kind can be read as another:
//   PRODUCTION - checks that call the functions in strata/core/dflash_refusal.hpp (the predicate,
//     the classification, the validate-then-copy handoff) - the same code the emitter runs;
//   MODEL - the checks inside latch_sequence(), a policy model of the serve/CLI latch gates.  It
//     uses the production predicate/classification, but it is NOT engine behaviour and is never
//     reported as such: no serve/CLI loop runs here;
//   STATIC - checks that read the ACTUAL source text of src/program/generate.cpp,
//     src/core/dflash_runtime.cpp and include/strata/core/dflash.hpp under
//     STRATA_DFLASH_SOURCE_ROOT (the CMake registration defines it).  Text contract on the real
//     files, NOT executed behaviour, and printed as such (STATIC ... / STATIC SKIPPED).
// The request-local latch lives in the serve loop and the CLI greedy loop, which need a target, a
// verifier and a drafter: no CPU process here can run them, so they are pinned, not executed.
#include "strata/core/dflash_refusal.hpp"

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>

using strata::core::DflashRefusal;
using strata::core::dflash_candidates_refused;
using strata::core::dflash_emit_candidates;
using strata::core::dflash_refusal_is_fatal;

static int failures = 0;
static int checks = 0;
// The three evidence kinds are counted SEPARATELY and reported separately, so nothing the MODEL
// (latch_sequence) proves can be read as a proof about the production seam, and nothing the STATIC
// pins prove can be read as runtime behaviour:
//   kProd  - checks that call the production functions in strata/core/dflash_refusal.hpp
//   kModel - checks inside latch_sequence(): a policy MODEL of the serve/CLI gates, not engine code
//   kStatic- - checks that read the real source text (text contract, not executed behaviour)
enum CheckScope { kProd = 0, kModel, kStatic, kScopeCount };
static int checks_by_scope[kScopeCount] = {};
static int check_scope = kProd;
static void check(bool ok, const char* what) {
    ++checks;
    ++checks_by_scope[check_scope];
    if (!ok) {
        std::printf("FAIL: %s\n", what);
        ++failures;
    }
}

// The sentinel the caller's buffers carry BEFORE a propose: a refused proposal must leave every
// byte of them exactly as it found them.
static const int32_t kOutSentinel = 0x5A5A5A5A;
static const float kProbSentinel = 1.2345678f;

struct Seam {
    // The emitter's host side: mapped candidate ids, the mapped probability rows behind them
    // (h_out_ + max_rows_ in the runtime), and the caller's out/probabilities.
    std::vector<int32_t> picks;
    std::vector<int32_t> probs_i;
    int32_t out[16];
    float prob[16];
    int block = 0;
    int64_t vocab = 0;

    explicit Seam(int block_, int64_t vocab_) : block(block_), vocab(vocab_) {
        picks.assign((size_t) block_, 0);
        probs_i.assign((size_t) block_, 0);
        for (int i = 0; i < 16; ++i) { out[i] = kOutSentinel; prob[i] = kProbSentinel; }
    }
    const int32_t* host_probs() const { return probs_i.data(); }
    bool out_untouched() const {
        for (int i = 0; i < 16; ++i)
            if (out[i] != kOutSentinel) return false;
        return true;
    }
    bool prob_untouched() const {
        for (int i = 0; i < 16; ++i)
            if (prob[i] != kProbSentinel) return false;
        return true;
    }
    /// The caller's bytes FROM index `from` (the block's end) to the end of the buffer are
    /// untouched.  Bounds-safe: `from` is clamped to the buffer.
    bool out_tail_sentinel(int from) const {
        for (int i = from < 0 ? 0 : (from > 16 ? 16 : from); i < 16; ++i)
            if (out[i] != kOutSentinel) return false;
        return true;
    }
    bool prob_tail_sentinel(int from) const {
        for (int i = from < 0 ? 0 : (from > 16 ? 16 : from); i < 16; ++i)
            if (prob[i] != kProbSentinel) return false;
        return true;
    }
};

// Bounds-safe comparisons against the SOURCE the handoff copies from: picks/probs_i hold exactly
// `block` entries, so asking for more than the block is a malformed request (false), never a
// read past the end of the source.
static bool out_equals(const Seam& s, int n) {
    if (n < 1 || n > s.block) return false;
    for (int i = 0; i < n; ++i)
        if (s.out[i] != s.picks[(size_t) i]) return false;
    return true;
}
// The mapped probability rows are a float area ALIASED as int32_t in the runtime (out_ +
// max_rows_), and the handoff is a byte copy - so the check compares BIT PATTERNS, not numbers.
static bool prob_equals(const Seam& s, int n) {
    if (n < 1 || n > s.block) return false;
    for (int i = 0; i < n; ++i) {
        int32_t bits = 0;
        std::memcpy(&bits, &s.prob[i], sizeof(bits));
        if (bits != s.probs_i[(size_t) i]) return false;
    }
    return true;
}

// ---- static source-contract pins (the real call sites; NOT runtime evidence) ----------------
#ifdef STRATA_DFLASH_SOURCE_ROOT
static bool read_all(const char* path, std::string& out) {
    std::ifstream f(path, std::ios::binary);
    if (!f) return false;
    std::ostringstream ss;
    ss << f.rdbuf();
    out = ss.str();
    return true;
}
static bool has(const std::string& text, const char* needle) { return text.find(needle) != std::string::npos; }
static int count_of(const std::string& text, const char* needle) {
    int n = 0;
    for (size_t pos = 0; (pos = text.find(needle, pos)) != std::string::npos; pos += std::strlen(needle)) ++n;
    return n;
}
/// Does `needle` appear within `span` characters AFTER `anchor`?  Used to pin what a specific
/// branch does (the recovery branch's own statements), not merely that it exists somewhere.
static bool window_has(const std::string& text, const char* anchor, const char* needle, size_t span) {
    const size_t at = text.find(anchor);
    if (at == std::string::npos) return false;
    const std::string win = text.substr(at, span);
    return win.find(needle) != std::string::npos;
}
static void static_contract_checks() {
    check_scope = kStatic;   // text contract on the real source - NOT executed behaviour
    std::string gen, rt, hpp;
    const bool ok_gen = read_all(STRATA_DFLASH_SOURCE_ROOT "/src/program/generate.cpp", gen);
    const bool ok_rt = read_all(STRATA_DFLASH_SOURCE_ROOT "/src/core/dflash_runtime.cpp", rt);
    const bool ok_hpp = read_all(STRATA_DFLASH_SOURCE_ROOT "/include/strata/core/dflash.hpp", hpp);
    check(ok_gen, "static: src/program/generate.cpp is readable under STRATA_DFLASH_SOURCE_ROOT");
    check(ok_rt, "static: src/core/dflash_runtime.cpp is readable under STRATA_DFLASH_SOURCE_ROOT");
    check(ok_hpp, "static: include/strata/core/dflash.hpp is readable under STRATA_DFLASH_SOURCE_ROOT");
    if (!ok_gen || !ok_rt) return;

    // The drafter clears its per-call class and message at propose()'s ENTRY, before the first
    // return, and only the numeric guard sets the recoverable class.
    const size_t entry = rt.find("bool DFlashDrafter::propose(");
    check(entry != std::string::npos, "static: propose()'s definition is found");
    if (entry != std::string::npos) {
        const size_t first_return = rt.find("return false;", entry);
        const size_t clear_status = rt.find("last_refusal_ = DflashRefusal::kNone;", entry);
        const size_t clear_err = rt.find("err.clear();", entry);
        check(clear_status != std::string::npos && clear_status < first_return,
              "static: propose() clears last_refusal_ BEFORE any return (no stale class)");
        check(clear_err != std::string::npos && clear_err < first_return,
              "static: propose() clears the message BEFORE any return (no stale err)");
    }
    check(count_of(rt, "last_refusal_ = DflashRefusal::kNonFiniteCandidates;") == 1,
          "static: exactly ONE site sets the recoverable class (the host vocabulary guard)");
    check(has(rt, "dflash_candidates_refused(h_out_, block, dg.vocab)") &&
              has(rt, "dflash_emit_candidates(h_out_, block, dg.vocab, out, h_out_ + max_rows_, probabilities)"),
          "static: the emitter validates the mapped host picks with the SHARED predicate, then copies "
          "with the SHARED validate-then-copy");
    check(!has(rt, "std::memcpy(out, h_out_, (size_t) block * 4);"),
          "static: the emitter no longer copies before it validates");
    check(has(hpp, "DflashRefusal last_refusal() const") &&
              has(hpp, "DflashRefusal last_refusal_ = DflashRefusal::kNone;"),
          "static: the drafter exposes the typed last_refusal() with the cleared default");

    // Serve loop: request-scope latch, gated K recompute, gated readiness, gated truncation,
    // recovery ONLY for the typed numeric class, every other failure keeps the ERR fatal.
    check(count_of(gen, "bool df_numeric_refusal = false;") == 2,
          "static: serve AND CLI each declare a request-scope latch (exactly two)");
    check(has(gen, "df_pending_k = df_numeric_refusal ? 0 : (df_auto ? df_policy.choose() : req_dflash_k);"),
          "static: serve's per-round K honours the latch (a latched request cannot re-arm)");
    check(has(gen, "dflash_ready = !df_numeric_refusal && req_dflash && drafted &&"),
          "static: serve's readiness gate honours the latch");
    check(has(gen, "if (dflash_ready && drafted && req_spec_min_p > 0 && !df_numeric_refusal)"),
          "static: probability truncation is gated, so a refused proposal never reads stale dprob");
    check(has(gen, "df_pending_ms = (proposed && !df_numeric_refusal) ?"),
          "static: a latched round contributes no proposal measurement");
    // The sync_context rule (gate r4-dflash-gate.md section 2): a latched round runs NO following
    // propose, so its add_context_f32 must synchronize - an unsynchronized context append requires an
    // immediate propose()/idle() before the caller reuses the taps (dflash.hpp:183).  Text contract on
    // the real file; it is not a race proof (the model above is the behavioural statement).
    check(has(gen, "const bool sync_context = df_numeric_refusal || !req_dflash || df_auto || eos || produced_n >= max_new || !strata::spec::dflash_proposal_fits(") &&
              !has(gen, "const bool sync_context = !req_dflash || df_auto || eos || produced_n >= max_new || !strata::spec::dflash_proposal_fits("),
          "static: serve's sync_context carries the latch term (a latched round's append waits); the term-less shape is absent");
    check(count_of(gen, "const bool sync_context = ") == 1 &&
              window_has(gen, "drafted = dflash.add_context_f32(", "p, a + 1, err, sync_context);", 500),
          "static: serve has ONE sync_context decision and passes it as add_context_f32's synchronize argument");
    // Recovery classifies through the SHARED function (so the mutation evidence on
    // dflash_refusal_is_fatal is evidence about the real recovery policy, not only about a header),
    // and never by comparing the enum directly at a call site.
    check(count_of(gen, "!strata::core::dflash_refusal_is_fatal(dflash.last_refusal())") == 2 &&
              count_of(gen, "dflash.last_refusal() == strata::core::DflashRefusal::kNonFiniteCandidates") == 0,
          "static: BOTH call sites classify through the SHARED dflash_refusal_is_fatal() (no direct enum compare)");
    check(window_has(gen, "if (!drafted && !strata::core::dflash_refusal_is_fatal(dflash.last_refusal())) {",
                     "df_numeric_refusal = true;", 600) &&
              window_has(gen, "if (!drafted && !strata::core::dflash_refusal_is_fatal(dflash.last_refusal())) {",
                         "drafted = true;", 600) &&
              window_has(gen, "if (!drafted && !strata::core::dflash_refusal_is_fatal(dflash.last_refusal())) {",
                         "err.clear();", 600),
          "static: serve's recovery branch latches, restores drafted and clears the consumed err");
    check(window_has(gen, "if (ctx_ok && dflash_ready && !drafted &&",
                     "df_numeric_refusal = true;", 600) &&
              window_has(gen, "if (ctx_ok && dflash_ready && !drafted &&", "drafted_ok = true;", 600) &&
              window_has(gen, "if (ctx_ok && dflash_ready && !drafted &&", "err.clear();", 600),
          "static: CLI's recovery branch latches, restores drafted/drafted_ok and clears the consumed err");
    // One notice per REFUSED REQUEST: no process-wide suppression flag anywhere, and the notice is
    // inside each recovery branch (the latch makes that branch run at most once per request).
    check(!has(gen, "df_refusal_reported") && !has(gen, "df_cli_refusal_reported"),
          "static: no process-wide notice-suppression flag (notices are not muted for later requests)");
    check(count_of(gen, "candidate tokens outside the vocabulary") == 2 &&
              count_of(gen, "for the rest of this request and the model keeps") == 2 &&
              window_has(gen, "if (!drafted && !strata::core::dflash_refusal_is_fatal(dflash.last_refusal())) {",
                         "std::fprintf(stderr, \"strata generate: at position %lld the drafter returned", 600) &&
              window_has(gen, "if (ctx_ok && dflash_ready && !drafted &&",
                         "std::fprintf(stderr, \"strata generate: at position %lld the drafter returned", 600),
          "static: each recovery branch prints its own position-bearing notice (one per refused request)");
    check(has(gen, "std::printf(\"ERR %s\\n\", err.c_str());") && has(gen, "std::printf(\"ERR an adaptive refill failed\\n\");"),
          "static: the serve fatals (drafted, adapt) are retained with their messages");

    // CLI: the ORIGINAL append short-circuit survives - propose() is reached only through ctx_ok -
    // and an append failure can never be masked by a stale last_refusal().
    check(has(gen, "const bool ctx_ok = dflash.add_context_f32("),
          "static: the CLI's append result is captured as const ctx_ok");
    check(has(gen, "bool drafted_ok = !dflash_ready || (ctx_ok && (drafted = dflash.propose("),
          "static: the CLI calls propose() ONLY when ctx_ok is true (append short-circuit preserved)");
    check(has(gen, "const bool ok = ctx_ok && drafted_ok;"),
          "static: the CLI's fatal feeds on ctx_ok AND the proposal result");
    check(!has(gen, "(!dflash_ready || (drafted = dflash.propose(x, p, dflash_k, drafts.data(), err,"),
          "static: the unguarded append/proposal chain is gone (propose cannot run after a failed append)");
    check(has(gen, "if (ctx_ok && dflash_ready && !drafted &&"),
          "static: the CLI's recovery requires a proposal that ACTUALLY RAN (ctx_ok && dflash_ready)");
    check(has(gen, "dflash_ready = !df_numeric_refusal && strata::spec::dflash_proposal_fits("),
          "static: the CLI's readiness gate honours the latch");
}
#define STATIC_REPORT "STATIC ... call-site latch pins ran against the real source text (static contract checks, NOT runtime evidence)"
#else
static void static_contract_checks() { std::printf("STATIC SKIPPED: STRATA_DFLASH_SOURCE_ROOT is not defined "
                                                   "(CMake defines it; the call-site pins were NOT run)\n"); }
#define STATIC_REPORT "STATIC SKIPPED"
#endif

// The request-local latch sequence, expressed with the SAME predicate and classification the call
// sites use.  This is a MODEL of the serve/CLI gates (the gates themselves are pinned above against
// the real source; only a live engine can run them), so that the disable-and-stay-disabled rule is
// executable on CPU: once a proposal is refused, every later round of the SAME request classifies
// as target-only, and a fresh request starts with no latch and is eligible again.
static void latch_sequence() {
    check_scope = kModel;   // every check below is the MODEL's, not a production-seam proof
    const int64_t vocab = 1024;
    bool refusal_latch = false;   // the request-scope latch (serve: df_numeric_refusal)
    const int k = 5;

    // Round 1..2: valid proposals.  T is the window width: k+1 drafted rows, else target-only.
    for (int round = 0; round < 2; ++round) {
        Seam s(k, vocab);
        for (int i = 0; i < k; ++i) s.picks[(size_t) i] = 7 + i;
        const bool refused = dflash_candidates_refused(s.picks.data(), k, vocab);
        const bool ready = !refusal_latch && !refused;
        check(ready, "latch: a valid proposal keeps the drafter ready");
        check(dflash_emit_candidates(s.picks.data(), k, vocab, s.out, s.host_probs(), s.prob),
              "latch: a valid proposal's handoff succeeds");
    }
    // Round 3: the proposal refuses.  The class is typed, and it is NOT fatal.
    {
        Seam s(k, vocab);
        for (int i = 0; i < k; ++i) s.picks[(size_t) i] = 7 + i;
        s.picks[2] = -1;
        const bool ok = dflash_emit_candidates(s.picks.data(), k, vocab, s.out, s.host_probs(), s.prob);
        const DflashRefusal cls = ok ? DflashRefusal::kNone : DflashRefusal::kNonFiniteCandidates;
        check(!ok && !dflash_refusal_is_fatal(cls), "latch: the numeric refusal is the recoverable class");
        refusal_latch = true;
    }
    // Rounds 4..9: the latch holds.  Pending K is 0, readiness is false for the whole request, so
    // the window is target-only (T = 1) and no later round re-arms the drafter.
    int drafted_rounds = 0;
    for (int round = 4; round <= 9; ++round) {
        const int pending_k = refusal_latch ? 0 : k;
        const bool ready = !refusal_latch && pending_k > 0;
        if (ready) ++drafted_rounds;
        check(!ready && pending_k == 0, "latch: no re-arming for the rest of the request");
    }
    check(drafted_rounds == 0, "latch: every post-refusal round of the request is target-only");
    // The next request re-declares the latch (request scope), so the drafter is eligible again.
    const bool fresh_request_latch = false;
    Seam s(k, vocab);
    for (int i = 0; i < k; ++i) s.picks[(size_t) i] = 11 + i;
    check(!fresh_request_latch && !dflash_candidates_refused(s.picks.data(), k, vocab),
          "latch: a fresh request evaluates the drafter fresh (no engine-wide drafter-off)");
    check_scope = kProd;
}

// ---- model: the serve add_context_f32 / sync_context ordering invariant ----------------------
// A CPU MODEL of serve's `sync_context` term and the readiness gate, for the documented rule that
// an add_context_f32 with synchronize=false requires an immediate propose()/idle() before the
// caller reuses the taps (include/strata/core/dflash.hpp:183).  This is a MODEL of a stream-ordering
// rule: it shows the latched round leaves no in-flight append without a following drafter operation
// and that the canonical non-latched round is unchanged.  It is NOT a race proof and NOT runtime
// evidence - the real statements are pinned statically (the pins are counted as kStatic).
static void sync_context_invariant() {
    check_scope = kModel;
    const int req_k = 5;   // canonical --dflash-block 5
    const bool req_dflash = true, df_auto = false, eos = false, under_cap = true;
    // dflash_proposal_fits requires k >= 1 (include/strata/spec/dflash_policy.hpp); with the
    // canonical width mid-context it fits, so the disjunction's last term is false.
    const bool fits = req_k >= 1;
    auto propose_ready = [&](bool latched) {   // serve's readiness gate, reduced to this invariant
        return !latched && req_dflash && !eos && under_cap && req_k > 0 && fits;
    };
    // (a) canonical NON-latched round: no explicit wait, and a propose DOES follow on the stream.
    const bool sync_nolatch = !req_dflash || df_auto || eos || !under_cap || !fits;
    check(!sync_nolatch && propose_ready(false),
          "model sync: canonical non-latched round keeps the unsynchronized append AND a following propose");
    check(sync_nolatch || propose_ready(false),
          "model sync: the non-latched round satisfies 'waits, or a drafter op immediately follows'");
    // (b) latched round WITH the fixed term: no propose follows, so the append must wait.
    const bool latched = true;
    const bool sync_fixed = latched || !req_dflash || df_auto || eos || !under_cap || !fits;
    check(!propose_ready(latched), "model sync: a latched round runs no following propose");
    check(sync_fixed, "model sync: with the latch term the latched round's add_context_f32 synchronizes");
    check(sync_fixed || propose_ready(latched),
          "model sync: the latched round satisfies 'waits, or a drafter op immediately follows'");
    // (c) the pre-fix shape (no latch term): the latched append is unsynchronized with NOTHING
    //     following on the stream - the defect the gate found, reproduced inside the model.
    const bool sync_prefix_free = !req_dflash || df_auto || eos || !under_cap || !fits;
    check(!sync_prefix_free && !propose_ready(latched),
          "model sync: WITHOUT the latch term the latched append is unsynchronized with nothing following (the defect)");
    check_scope = kProd;
}

int main() {
    const int64_t V = 248320;   // the canonical Flash-Next vocabulary (dflash.hpp header note)

    // ---- the predicate: exactly the emitter's historic out-of-range guard ---------------------
    {
        const int32_t all_valid[5] = {0, 1, 12345, (int32_t) V - 2, (int32_t) V - 1};
        check(!dflash_candidates_refused(all_valid, 5, V), "all in-range ids: no refusal (both bounds included)");
        for (int i = 0; i < 5; ++i) {
            int32_t row[5] = {5, 6, 7, 8, 9};
            row[i] = -1;
            check(dflash_candidates_refused(row, 5, V), "id -1 at any row of the block: refusal");
            row[i] = (int32_t) V;
            check(dflash_candidates_refused(row, 5, V), "id == vocab at any row of the block: refusal");
            row[i] = (int32_t) V + 1;
            check(dflash_candidates_refused(row, 5, V), "id above vocab: refusal");
            row[i] = INT32_MIN;
            check(dflash_candidates_refused(row, 5, V), "INT32_MIN: refusal (no unsigned wrap)");
            row[i] = INT32_MAX;
            check(dflash_candidates_refused(row, 5, V), "INT32_MAX: refusal");
        }
        const int32_t single_bad[8] = {1, 2, 3, 4, 5, 6, 7, (int32_t) V};
        check(dflash_candidates_refused(single_bad, 8, V), "the LAST row of an 8-row block is checked too");
        const int32_t one_ok[1] = {0};
        check(!dflash_candidates_refused(one_ok, 1, 1), "vocab 1 with id 0: valid (contract: [0, vocab))");
        const int32_t one_bad[1] = {1};
        check(dflash_candidates_refused(one_bad, 1, 1), "vocab 1 with id 1: refusal");
    }
    // Malformed inputs are NOT a numeric refusal: the caller keeps its fatal class.  The contract
    // (which ids are valid) is untouched by these guards.
    {
        const int32_t row[2] = {0, 1};
        check(!dflash_candidates_refused(nullptr, 2, V), "malformed: no candidate buffer is not a refusal");
        check(!dflash_candidates_refused(row, 0, V), "malformed: empty block is not a refusal");
        check(!dflash_candidates_refused(row, -3, V), "malformed: negative block is not a refusal");
        check(!dflash_candidates_refused(row, 2, 0), "malformed: empty vocabulary is not a refusal");
        check(!dflash_candidates_refused(row, 2, -1), "malformed: negative vocabulary is not a refusal");
    }

    // ---- classification: only the numeric class is recoverable ----------------------------------
    {
        check(dflash_refusal_is_fatal(DflashRefusal::kNone), "kNone is fatal (device/stream/geometry/ring/append stay fatal)");
        check(!dflash_refusal_is_fatal(DflashRefusal::kNonFiniteCandidates), "kNonFiniteCandidates is recoverable");
        check((int) (unsigned char) DflashRefusal::kNone == 0 &&
                  (int) (unsigned char) DflashRefusal::kNonFiniteCandidates == 1,
              "the status is a 1-byte tag with the documented values");
        check(dflash_refusal_is_fatal(static_cast<DflashRefusal>(2)), "any other value is fatal (no blanket catch)");
        check(dflash_refusal_is_fatal(static_cast<DflashRefusal>(255)), "the widest out-of-range value is fatal");
    }

    // ---- the host handoff: validate BEFORE any copy ---------------------------------------------
    {
        Seam s(5, V);
        for (int i = 0; i < 5; ++i) { s.picks[(size_t) i] = 100 + i; s.probs_i[(size_t) i] = 900 + i; }
        check(dflash_emit_candidates(s.picks.data(), 5, V, s.out, s.host_probs(), s.prob),
              "valid block: the handoff succeeds");
        check(out_equals(s, 5), "valid block: out is an exact in-block copy of the ids (bounds-safe)");
        check(s.out_tail_sentinel(5) && s.prob_tail_sentinel(5),
              "valid block: nothing past the block is written (explicit sentinel check beyond the block)");
        check(prob_equals(s, 5), "valid block: probabilities come from the mapped rows behind the ids");
        check(!out_equals(s, 6) && !prob_equals(s, 6),
              "bounds-safe helper: asking for more rows than the block is refused, never read past the source");
    }
    {
        Seam s(5, V);
        for (int i = 0; i < 5; ++i) { s.picks[(size_t) i] = 100 + i; s.probs_i[(size_t) i] = 900 + i; }
        s.picks[3] = -1;
        check(!dflash_emit_candidates(s.picks.data(), 5, V, s.out, s.host_probs(), s.prob),
              "refusal (-1): the handoff fails");
        check(s.out_untouched() && s.prob_untouched(),
              "refusal (-1): the caller's out AND probabilities keep their sentinel bytes (nothing written)");
    }
    {
        Seam s(5, V);
        for (int i = 0; i < 5; ++i) { s.picks[(size_t) i] = 100 + i; s.probs_i[(size_t) i] = 900 + i; }
        s.picks[4] = (int32_t) V;
        check(!dflash_emit_candidates(s.picks.data(), 5, V, s.out, s.host_probs(), s.prob),
              "refusal (id == vocab): the handoff fails");
        check(s.out_untouched() && s.prob_untouched(),
              "refusal (id == vocab): out AND probabilities untouched");
    }
    {
        Seam s(4, V);
        for (int i = 0; i < 4; ++i) { s.picks[(size_t) i] = 10 + i; s.probs_i[(size_t) i] = 77 + i; }
        s.picks[0] = INT32_MIN;
        check(!dflash_emit_candidates(s.picks.data(), 4, V, s.out, s.host_probs(), nullptr),
              "refusal without probabilities requested: the handoff fails");
        check(s.out_untouched() && s.prob_untouched(), "refusal: the caller's drafts keep their previous bytes");
    }
    {
        Seam s(3, V);
        for (int i = 0; i < 3; ++i) { s.picks[(size_t) i] = 1 + i; s.probs_i[(size_t) i] = 500 + i; }
        check(dflash_emit_candidates(s.picks.data(), 3, V, s.out, s.host_probs(), nullptr),
              "no probabilities asked for: the ids copy succeeds");
        check(out_equals(s, 3) && s.prob_untouched(), "no probabilities asked for: only out is written");
    }
    {
        Seam s(2, V);
        s.picks[0] = 3; s.picks[1] = 4; s.probs_i[0] = 1; s.probs_i[1] = 2;
        check(!dflash_emit_candidates(s.picks.data(), 2, V, s.out, nullptr, s.prob),
              "malformed: probabilities asked for with no source fails");
        check(s.out_untouched() && s.prob_untouched(), "malformed handoff: nothing is written, and it is not a refusal");
        check(!dflash_emit_candidates(s.picks.data(), 2, V, nullptr, s.host_probs(), s.prob), "malformed: no out buffer fails");
        check(!dflash_emit_candidates(s.picks.data(), 0, V, s.out, s.host_probs(), s.prob), "malformed: empty block fails");
        check(!dflash_emit_candidates(s.picks.data(), 2, 0, s.out, s.host_probs(), s.prob), "malformed: empty vocabulary fails");
        check(s.out_untouched() && s.prob_untouched(), "malformed: the caller's buffers survive every malformed call");
    }
    // Bulk equivalence: a deterministic sweep of valid blocks copies byte-identically, and a single
    // injected out-of-range id in ANY row of ANY block refuses with zero writes.
    {
        uint64_t st = 88172645463325252ull;
        auto rnd = [&]() { st ^= st << 13, st ^= st >> 7, st ^= st << 17; return st; };
        int bulk_ok = 0, bulk_refused = 0, bulk_no_write = 0;
        for (int trial = 0; trial < 400; ++trial) {
            const int block = 1 + (int) (rnd() % 8);
            Seam s(block, V);
            for (int i = 0; i < block; ++i) {
                s.picks[(size_t) i] = (int32_t) (rnd() % (uint64_t) V);
                s.probs_i[(size_t) i] = (int32_t) (rnd() % 1000);
            }
            if (dflash_emit_candidates(s.picks.data(), block, V, s.out, s.host_probs(), s.prob) &&
                out_equals(s, block) && prob_equals(s, block) &&
                !dflash_candidates_refused(s.picks.data(), block, V)) ++bulk_ok;
            // Inject one refusal at a random row: the handoff must fail and write nothing.  The
            // injected id is out of range on a FRESH sentinel buffer, so "untouched" is meaningful.
            Seam r(block, V);
            r.picks = s.picks;
            r.probs_i = s.probs_i;
            r.picks[(size_t) (rnd() % (uint64_t) block)] = (int32_t) V + (int32_t) (rnd() % 16);
            if (!dflash_emit_candidates(r.picks.data(), block, V, r.out, r.host_probs(), r.prob)) {
                ++bulk_refused;
                if (r.out_untouched() && r.prob_untouched()) ++bulk_no_write;
            }
        }
        check(bulk_ok == 400, "bulk: 400 valid blocks copy byte-identically (ids and probabilities)");
        check(bulk_refused == 400, "bulk: an injected out-of-range id in any row of any block refuses");
        check(bulk_no_write == 400, "bulk: a refused block writes nothing to the caller's buffers");
    }

    // The per-call state policy that is provable on CPU: these functions are pure (they carry no
    // state between calls), which is what makes a cleared per-call status meaningful at the call
    // sites; the clear itself is pinned against propose()'s text in the static block.
    {
        Seam a(2, V); a.picks[0] = 1; a.picks[1] = 2;
        Seam b(2, V); b.picks[0] = -5; b.picks[1] = 2;
        check(dflash_emit_candidates(a.picks.data(), 2, V, a.out, a.host_probs(), a.prob), "pure: a valid call succeeds");
        check(!dflash_emit_candidates(b.picks.data(), 2, V, b.out, b.host_probs(), b.prob), "pure: a refusal after a success");
        check(dflash_emit_candidates(a.picks.data(), 2, V, a.out, a.host_probs(), a.prob),
              "pure: the predicate and copy keep no state between calls (no sticky refusal)");
    }

    latch_sequence();
    sync_context_invariant();
    static_contract_checks();

    const int prod = checks_by_scope[kProd];
    const int model = checks_by_scope[kModel];
    const int st = checks_by_scope[kStatic];
    std::printf("%s\n", STATIC_REPORT);
    std::printf("EVIDENCE SPLIT: %d checks call the PRODUCTION functions in strata/core/dflash_refusal.hpp "
                "(predicate, classification, host handoff); %d checks are the LATCH MODEL in latch_sequence() "
                "(a model of the serve/CLI gates, NOT engine behaviour); %d checks are STATIC source-text "
                "pins on the real call sites (text contract, NOT executed behaviour).\n", prod, model, st);
    if (failures) {
        std::printf("dflash_refusal_test: %d of %d checks FAILED (production %d, model %d, static %d)\n",
                    failures, checks, prod, model, st);
        return 1;
    }
    std::printf("dflash_refusal_test: all %d checks passed (production %d, model %d, static %d)\n",
                checks, prod, model, st);
    return 0;
}
