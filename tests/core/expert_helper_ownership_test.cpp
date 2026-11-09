// tests/core/expert_helper_ownership_test.cpp - M1 (capacity, parent correction 2): the RESIDENCY
// lifecycle of the out-of-process helper proxy, driven over a REAL AF_UNIX channel against a
// scripted fake helper.  The promotion gate's rule (src/program/generate.cpp: `t->is_proxy() &&
// !t->proxy_residency_live()`) turns on ONE distinction the dispatch predicate does not make:
//
//   * a tier disabled for the REST OF A REQUEST (a failed finish: note_failure) computes nothing,
//     but its helper PROCESS is alive with its slots ALLOCATED - its pairs must stay out of the
//     primary's promotion candidates, or the same expert sits in both cards' caches;
//   * a CLOSED CHANNEL (the peer died, close()) releases the residency - the helper process is
//     gone, its VRAM is freed - and the gate must stop consulting it, or a dead helper's stale
//     fill report blocks the very promotions that recover its pairs.
//
// This test shows the transitions on the real state (RemoteExperts proxy mode), not as a text
// count: registered fill -> mid-request disable -> per-request re-arm -> channel death -> close,
// each step asserting the gate's own predicate.  Proxy mode makes NO device call (the helper owns
// all NVIDIA memory; on the HIP engine no CUDA runtime is mapped at all), so this runs with the
// engine library linked and no GPU work: the fake helper answers the wire's real frames.
//
// Exit 0 = every check passed.  Linux only (AF_UNIX).
#include "strata/core/remote_experts.hpp"

#include "strata/core/expert_helper_protocol.hpp"   // Socket: the fake helper's end of the channel

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <string>
#include <thread>
#include <vector>

#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

using namespace strata::core;
using namespace strata::core::expert_helper;

namespace {

int failures = 0;
void check(bool ok, const char* what) {
    std::printf("  %-74s %s\n", what, ok ? "ok" : "FAIL");
    if (!ok) ++failures;
}

constexpr int64_t kLayers = 48, kExperts = 512, kH = 2560, kFF = 640;   // the pack's fixed geometry

// The gate's own expression (generate.cpp): consult a tier when it is not a proxy, or when its
// residency is live; skip it only when a proxy's channel closed.
bool gate_consults(const RemoteExperts& t) { return !t.is_proxy() || t.proxy_residency_live(); }

/// THE FAKE HELPER: bind the path, accept the engine, answer kHello with kHelloAck and kFill with
/// kFillReport (`filled` pairs).  It answers NOTHING after the fill: the first kBegin is read and
/// met with SILENCE (the channel closes on it) - the window's REQUEST goes out, its REPLY meets
/// EOF, which is the peer-death the engine must read as PERMANENT residency release.  Answers with
/// the same write_reply the real helper uses (the reply echoes the request's sequence, H13).
/// `fill_n` = 0 fills nothing: the registration refusal path.
void fake_helper(const std::string& path, int fill_n, std::atomic<bool>& up) {
    up = false;
    Socket srv;
    if (srv.listen_bind(path) != kOk) return;
    // accept() RETURNS the connected descriptor and leaves `srv` the listener (the helper's own
    // pattern, helper_main.cpp:764-775): the frame loop must run on a channel socket that ADOPTS
    // it, never on the listener itself.
    const int peer = srv.accept(deadline_after(25000));
    if (peer < 0) { srv.unbind(); return; }
    Socket conn;
    conn.adopt(peer);
    up = true;
    RxBuffer rx;
    for (;;) {
        Header head;
        PayloadView body;
        if (conn.read_frame(head, rx, body, deadline_after(4000)) != kOk) break;
        if (head.type == kHello) {
            HelloAck ack;
            ack.max_tok = (int32_t) kMaxTok;
            ack.k = (int32_t) kK;
            ack.max_entries = (int32_t) kMaxEntries;
            ack.status = 0;
            ack.free_mib = 7000;
            ack.features = kFeaturePromptRows;
            const uint8_t* p = reinterpret_cast<const uint8_t*>(&ack);
            if (conn.write_reply(kHelloAck, head.req_id, head.seq, p, sizeof(ack),
                                 deadline_after(2000)) != kOk) break;
        } else if (head.type == kFill) {
            std::vector<uint8_t> payload(sizeof(FillReport));
            FillReport rep;
            rep.resident = fill_n;
            rep.bytes = (int64_t) fill_n * 2329856;   // the aligned bytes a real fill would spend
            rep.scratch_bytes = 0;
            rep.buffers_bytes = 0;
            rep.n_pairs = fill_n;
            std::memcpy(payload.data(), &rep, sizeof(rep));
            const int32_t pairs[3][2] = { { 3, 10 }, { 7, 20 }, { 41, 5 } };
            for (int i = 0; i < fill_n; ++i) {
                const int32_t l = pairs[i][0], e = pairs[i][1];
                const size_t at = payload.size();
                payload.resize(at + 8);
                std::memcpy(payload.data() + at, &l, 4);
                std::memcpy(payload.data() + at + 4, &e, 4);
            }
            if (conn.write_reply(kFillReport, head.req_id, head.seq, payload.data(), payload.size(),
                                 deadline_after(2000)) != kOk) break;
        } else {
            break;   // THE PEER DIES HERE: the kBegin is read, then the channel closes with no reply
        }
    }
    conn.close();
    srv.close();
    srv.unbind();
}

std::string test_path(const char* tag) {
    return std::string("/tmp/agent/strata-own-") + tag + "-" + std::to_string((long) getpid()) + ".sock";
}

void run_registered_lifecycle() {
    std::printf("(A) registered fill -> mid-request disable -> re-arm -> peer death -> release\n");
    const std::string path = test_path("a");
    unlink(path.c_str());
    std::atomic<bool> up{ false };
    std::thread th(fake_helper, path, 3, std::ref(up));
    for (int i = 0; i < 500 && !up; ++i) std::this_thread::sleep_for(std::chrono::milliseconds(4));

    std::vector<std::pair<int32_t, int32_t>> ranked{ { 3, 10 }, { 7, 20 }, { 41, 5 } };
    std::vector<uint8_t> claimed((size_t) kLayers * kExperts, 0);   // no stage owns any pair
    RemoteExperts t;
    std::string err;
    const bool opened = t.open_socket(path, 3, kLayers, kExperts, kH, kFF, 0, kLayers - 1,
                                      512, 2000, ranked, claimed, err);
    check(opened, "open_socket registers the proxy over the fake helper (hello + fill report)");
    if (!opened) std::printf("      open_socket: %s\n", err.c_str());

    // STATE 1: registered and filled.  Residency live: the gate consults, the pairs are protected.
    check(t.is_proxy(), "registered: the tier answers as a proxy");
    check(t.proxy_residency_live(), "STATE 1 (registered, filled): residency live - the gate consults");
    check(t.helper_usable(), "STATE 1: dispatch is on too");
    check(t.resident() == 3 && t.holds(3, 10) && t.holds(41, 5) && !t.holds(3, 11),
          "the mirrored fill report answers holds() (layer 41 included - M1's all-layer fill)");
    check(gate_consults(t), "STATE 1: the promotion gate consults this tier");

    // STATE 2: a finish failed mid-request -> the tier is disabled for the REST of the request.
    t.note_failure();
    check(!t.helper_usable(), "STATE 2 (mid-request disable): dispatch skips the tier");
    check(t.proxy_residency_live(),
          "STATE 2: residency is STILL LIVE - the helper process is alive with its slots allocated");
    check(gate_consults(t), "STATE 2: the gate STILL consults - the helper's pairs stay protected");
    std::vector<int32_t> claims;
    std::vector<float> x((size_t) kH, 0.0f);
    int32_t ids[10] = { 10, 1, 2, 4, 1, 2, 4, 1, 2, 4 };   // entry 0 routes to helper-owned (3,10)
    bool ok = t.begin(3, x.data(), ids, 1, 10, nullptr, nullptr, err, claims);
    check(ok && claims.empty(), "STATE 2: the disabled tier claims ZERO rows (dispatch off, residency on)");

    // STATE 3: the request boundary re-arms dispatch.  Residency was never dropped in between.
    const bool rearmed = t.note_request_start();
    check(rearmed && t.helper_usable(), "STATE 3 (next request): note_request_start re-arms dispatch");
    check(t.proxy_residency_live(), "STATE 3: residency stayed live across the disable/re-arm");
    check(gate_consults(t), "STATE 3: the gate consults - protection never needed to drop to survive a disable");

    // STATE 4: the peer DIES (the fake helper goes SILENT at the first begin, closing the channel).
    // The window's REQUEST went out; its REPLY meets EOF: that is the channel closing, i.e. the
    // PERMANENT loss the gate must recover from.
    ok = t.begin(3, x.data(), ids, 1, 10, nullptr, nullptr, err, claims);
    check(ok && claims.size() == 1, "STATE 4: re-armed, the tier claims the helper-owned entry again");
    std::vector<float> out((size_t) 80 * kH, 0.0f);
    const bool fin = t.finish(out.data(), err);
    check(!fin, "STATE 4: the peer is gone - the window's finish fails with the named channel error");
    check(!t.proxy_residency_live(),
          "STATE 4 (channel closed): residency RELEASED - the helper's VRAM is freed with the process");
    check(!t.helper_usable(), "STATE 4: dispatch is off");
    check(!gate_consults(t), "STATE 4: the gate STOPS consulting - the primary may promote its pairs (recovery)");
    check(t.resident() == 3 && t.holds(3, 10),
          "the mirror stays readable for the DONE line's accounting, but it is NOT consulted (the fix)");
    th.join();
    t.close();
}

void run_engine_close() {
    std::printf("(B) the engine closes a LIVE proxy: residency released, the tier leaves the gate\n");
    const std::string path = test_path("b");
    unlink(path.c_str());
    std::atomic<bool> up{ false };
    std::thread th(fake_helper, path, 3, std::ref(up));
    for (int i = 0; i < 500 && !up; ++i) std::this_thread::sleep_for(std::chrono::milliseconds(4));
    std::vector<std::pair<int32_t, int32_t>> ranked{ { 3, 10 }, { 7, 20 }, { 41, 5 } };
    std::vector<uint8_t> claimed((size_t) kLayers * kExperts, 0);
    RemoteExperts t;
    std::string err;
    check(t.open_socket(path, 3, kLayers, kExperts, kH, kFF, 0, kLayers - 1, 512, 2000, ranked, claimed, err),
          "registered live (the channel is OPEN, no failure happened)");
    check(t.proxy_residency_live() && gate_consults(t), "live proxy: residency live, the gate consults");
    t.close();
    check(!t.is_proxy() && !t.proxy_residency_live(),
          "close(): the tier is no longer a proxy and its residency is released");
    check(!(gate_consults(t) && t.holds(3, 10)),
          "close(): no hold remains - a closed tier is consulted empty, so the pairs are promotable again");
    check(!t.holds(3, 10), "close(): the mirror is cleared with the channel");
    th.join();
}

void run_empty_fill() {
    std::printf("(C) a helper that fills NOTHING never blocks a promotion\n");
    const std::string path = test_path("c");
    unlink(path.c_str());
    std::atomic<bool> up{ false };
    std::thread th(fake_helper, path, 0, std::ref(up));
    for (int i = 0; i < 500 && !up; ++i) std::this_thread::sleep_for(std::chrono::milliseconds(4));
    std::vector<std::pair<int32_t, int32_t>> ranked{ { 3, 10 }, { 7, 20 }, { 41, 5 } };
    std::vector<uint8_t> claimed((size_t) kLayers * kExperts, 0);
    RemoteExperts t;
    std::string err;
    const bool opened = t.open_socket(path, 3, kLayers, kExperts, kH, kFF, 0, kLayers - 1,
                                      512, 2000, ranked, claimed, err);
    check(!opened, "an empty fill report refuses the registration (the tier never registers)");
    check(!t.proxy_residency_live() && !t.helper_usable() && !(gate_consults(t) && t.holds(3, 10)),
          "the refused tier holds nothing - a helper that fills nothing cannot block local promotion");
    check(t.resident() == 0, "zero residency, with the named reason the caller prints");
    th.join();
}

// ================= The TWO-CHANNEL split (opt-in --socket2 / --expert-helper-socket2) =================
//
// The window tier (kBegin/kRows) and the prompt tier (kBeginP/kRowsP) are logically independent, and
// on ONE socket each tier's frames queue behind the other's in-flight request.  These two scenarios
// drive the REAL proxy over TWO real AF_UNIX channels with two scripted fake helpers, and assert the
// property the split exists for: each channel fails on its own.  No GPU and no device call: proxy
// mode makes none (the helper owns all NVIDIA memory), exactly like scenarios (A)-(C) above.
//
//   (D) the PROMPT channel dies mid-run: the prompt tier refuses by name and re-arms on the primary
//       channel at the next request, and the window tier serves every window in between;
//   (E) the WINDOW channel dies: the window tier claims zero rows and the promotion gate stops
//       consulting it (residency released), while the prompt tier keeps serving on its own channel.
constexpr int32_t kFillPairs[3][2] = { { 3, 10 }, { 7, 20 }, { 41, 5 } };
constexpr int64_t kFillN = 3;

bool fill_holds(int64_t l, int32_t e) {
    for (int i = 0; i < (int) kFillN; ++i)
        if (kFillPairs[i][0] == l && kFillPairs[i][1] == e) return true;
    return false;
}

/// The rows body of kRows AND kRowsP: they are the same shape (an 8-byte header, then
/// (int32 index, n_embd floats) per row), so ONE builder serves both tiers.
std::vector<uint8_t> rows_body_for(const std::vector<int32_t>& idx) {
    std::vector<uint8_t> p(sizeof(RowsResp));
    RowsResp h;
    h.n_rows = (int32_t) idx.size();
    h.reserved0 = 0;
    std::memcpy(p.data(), &h, sizeof(h));
    for (const int32_t i : idx) {
        const size_t at = p.size();
        p.resize(at + 4 + (size_t) kH * 4);
        std::memcpy(p.data() + at, &i, 4);
        float* row = (float*) (p.data() + at + 4);
        for (int64_t v = 0; v < kH; ++v) row[v] = (float) (i * 7 + v) * 0.25f;
    }
    return p;
}

/// ONE fake channel: bind, accept, answer kHello (the prompt tier's feature bit advertised), and
/// serve kBegin (kRows, with the claims computed exactly as the engine computes them) and kBeginP
/// (kRowsP, one row per claimed pair).  `do_fill` marks the PRIMARY channel (only it receives the
/// kFill; the second channel's engine client sends none).  The channel closes when `stop` is set,
/// which is how the test kills one tier's channel while the other stays open.
void fake_channel(const std::string& path, std::atomic<bool>& up, std::atomic<bool>& stop,
                  std::atomic<int>& windows, std::atomic<int>& prompts, bool do_fill) {
    up = false;
    Socket srv;
    if (srv.listen_bind(path) != kOk) return;
    const int peer = srv.accept(deadline_after(25000));
    if (peer < 0) { srv.unbind(); return; }
    Socket conn;
    conn.adopt(peer);
    up = true;
    RxBuffer rx;
    for (;;) {
        Header head;
        PayloadView body;
        // A SHORT read with the stop flag checked at every round: the test kills one channel by
        // setting `stop`, and the fake then closes THAT channel within one poll - deterministically,
        // with no frame served after the kill.  `up` drops with it, which is what the test waits on.
        const int rrc = conn.read_frame(head, rx, body, deadline_after(300));
        if (rrc == kErrTimeout) {
            if (stop) break;
            continue;
        }
        if (rrc != kOk) break;
        if (head.type == kHello) {
            HelloAck ack;
            ack.max_tok = (int32_t) kMaxTok;
            ack.k = (int32_t) kK;
            ack.max_entries = (int32_t) kMaxEntries;
            ack.status = 0;
            ack.free_mib = 7000;
            ack.features = kFeaturePromptRows;
            const uint8_t* p = reinterpret_cast<const uint8_t*>(&ack);
            if (conn.write_reply(kHelloAck, head.req_id, head.seq, p, sizeof(ack),
                                 deadline_after(2000)) != kOk) break;
        } else if (head.type == kFill && do_fill) {
            std::vector<uint8_t> payload(sizeof(FillReport));
            FillReport rep;
            rep.resident = kFillN;
            rep.bytes = (int64_t) kFillN * 2329856;
            rep.scratch_bytes = 0;
            rep.buffers_bytes = 0;
            rep.n_pairs = (int32_t) kFillN;
            std::memcpy(payload.data(), &rep, sizeof(rep));
            for (int i = 0; i < (int) kFillN; ++i) {
                const size_t at = payload.size();
                payload.resize(at + 8);
                std::memcpy(payload.data() + at, &kFillPairs[i][0], 4);
                std::memcpy(payload.data() + at + 4, &kFillPairs[i][1], 4);
            }
            if (conn.write_reply(kFillReport, head.req_id, head.seq, payload.data(), payload.size(),
                                 deadline_after(2000)) != kOk) break;
        } else if (head.type == kBegin) {
            if (body.size() < sizeof(BeginReq)) break;
            BeginReq req;
            std::memcpy(&req, body.data(), sizeof(req));
            const int64_t n = (int64_t) req.n_tok * req.k;
            const int32_t* ids = (const int32_t*) (body.data() + sizeof(BeginReq));
            std::vector<int32_t> claims;
            for (int64_t i = 0; i < n; ++i)
                if (fill_holds(req.layer, ids[i])) claims.push_back((int32_t) i);
            const std::vector<uint8_t> p = rows_body_for(claims);
            if (conn.write_reply(kRows, head.req_id, head.seq, p.data(), p.size(),
                                 deadline_after(8000)) != kOk) break;
            ++windows;
            if (stop) break;
        } else if (head.type == kBeginP) {
            if (body.size() < sizeof(BeginPReq)) break;
            BeginPReq req;
            std::memcpy(&req, body.data(), sizeof(req));
            std::vector<int32_t> claims;
            for (int32_t i = 0; i < req.n_pairs; ++i) claims.push_back(i);
            const std::vector<uint8_t> p = rows_body_for(claims);
            if (conn.write_reply(kRowsP, head.req_id, head.seq, p.data(), p.size(),
                                 deadline_after(8000)) != kOk) break;
            ++prompts;
            if (stop) break;
        } else {
            break;
        }
    }
    conn.close();
    srv.close();
    srv.unbind();
    up = false;   // the channel is closed: the test's kill is visible to the engine's client now
}

/// The window the engine sends: one token, k=10, routed to experts 10 (held on layer 3, five
/// routed indices) and 11 (held nowhere, five more).  The proxy claims exactly the five held ones.
constexpr int64_t kWinTok = 1;
int32_t win_ids[10] = { 10, 10, 10, 10, 10, 11, 11, 11, 11, 11 };
constexpr int64_t kWinClaims = 5;
constexpr int64_t kWinLayer = 3;

float x_win[kWinTok * kH];
float out_win[kWinTok * 10 * kH];

bool serve_window(RemoteExperts& t, std::string& err, bool expect_rows = true) {
    std::vector<int32_t> claims;
    std::vector<int32_t> primary((size_t) kLayers * kExperts, -1);   // no primary residency anywhere
    for (int64_t i = 0; i < kWinTok * kH; ++i) x_win[i] = (float) (i % 97) * 0.5f;
    if (!t.begin(kWinLayer, x_win, win_ids, kWinTok, 10, nullptr, primary.data(), err, claims)) return false;
    // The tier's own rule: an unusable tier claims ZERO rows and SUCCEEDS (the dispatch's plan keeps
    // the entries CPU-served), so the dead-channel arm asserts the EMPTY claim list, not rows.
    if (expect_rows && (int64_t) claims.size() != kWinClaims) return false;
    if (!expect_rows && !claims.empty()) return false;
    for (int64_t i = 0; i < kWinTok * 10 * kH; ++i) out_win[i] = -1.0f;
    if (!t.finish(out_win, err)) return false;
    if (!expect_rows) return true;
    for (int64_t i = 0; i < kWinClaims; ++i)
        if (out_win[(size_t) i * kH + 3] != (float) (i * 7 + 3) * 0.25f) return false;
    return true;
}

ExpertHelperPromptPlan make_plan() {
    ExpertHelperPromptPlan plan;
    for (int i = 0; i < 4; ++i) { plan.pair_token.push_back(i); plan.pair_expert.push_back(i < 3 ? 10 : 20); }
    for (int i = 0; i < 4; ++i) plan.tokens.push_back(i);
    return plan;
}

float out_p[4 * kH];

/// One prompt layer: stage the plan's rows into the tier's OWN pinned buffer (the caller-fills
/// contract), send the plan, take the rows at their pair indices.  Returns the client's status.
int serve_prompt(RemoteExperts& t, int64_t timeout_ms, int32_t& rows) {
    const ExpertHelperPromptPlan plan = make_plan();
    std::vector<int32_t> primary((size_t) kLayers * kExperts, -1);
    float* buf = const_cast<float*>(t.prompt_buffer_data());
    if (buf == nullptr) return expert_helper::kErrInternal;
    for (int r = 0; r < 4; ++r)
        for (int64_t v = 0; v < kH; ++v) buf[(size_t) r * kH + (size_t) v] = (float) (r * 11 + v) * 0.125f;
    const int rc = t.begin_prompt((int32_t) kWinLayer, plan, primary.data(), 4000);
    if (rc != expert_helper::kOk) return rc;
    for (int64_t i = 0; i < 4 * kH; ++i) out_p[i] = -1.0f;
    const int rc2 = t.finish_prompt(out_p, kH, std::vector<int32_t>{ 0, 1, 2, 3 }, timeout_ms, rows);
    if (rc2 != expert_helper::kOk) return rc2;
    for (int i = 0; i < 4; ++i)
        if (out_p[(size_t) i * kH + 5] != (float) (i * 7 + 5) * 0.25f) return expert_helper::kErrBadId;
    return expert_helper::kOk;
}

void run_prompt_channel_dies() {
    std::printf("(D) two channels: the PROMPT channel dies, the window tier keeps serving\n");
    const std::string pa = test_path("d1"), pb = test_path("d2");
    unlink(pa.c_str());
    unlink(pb.c_str());
    std::atomic<bool> upA{ false }, upB{ false }, stopA{ false }, stopB{ false };
    std::atomic<int> wA{ 0 }, pA{ 0 }, wB{ 0 }, pB{ 0 };
    std::thread ta(fake_channel, pa, std::ref(upA), std::ref(stopA), std::ref(wA), std::ref(pA), true);
    std::thread tb(fake_channel, pb, std::ref(upB), std::ref(stopB), std::ref(wB), std::ref(pB), false);
    for (int i = 0; i < 500 && (!upA || !upB); ++i) std::this_thread::sleep_for(std::chrono::milliseconds(4));

    std::vector<std::pair<int32_t, int32_t>> ranked{ { 3, 10 }, { 7, 20 }, { 41, 5 } };
    std::vector<uint8_t> claimed((size_t) kLayers * kExperts, 0);
    RemoteExperts t;
    std::string err;
    const bool opened = t.open_socket(pa, 3, kLayers, kExperts, kH, kFF, 0, kLayers - 1,
                                      512, 4000, ranked, claimed, err, pb);
    check(opened, "open_socket with a second path registers the proxy on TWO channels");
    check(upA && upB, "both channels are connected (the second one handshakes, it never fills)");
    check(t.is_proxy() && t.helper_usable() && t.resident() == 3,
          "the fill report is the FIRST channel's: the mirrored set is unchanged by the split");
    check(t.prompt_capable() && t.prompt_usable(), "the prompt tier is capable and usable");
    check(t.prompt_buffer_ensure(kH) == expert_helper::kOk,
          "the prompt tier's pinned activation buffer maps on BOTH channels (same geometry, so they "
          "map together - the rows are always staged into the pinned channel's buffer)");

    // Request 1: the window's rows on channel A, the prompt's rows on channel B.
    t.note_request_start();
    check(serve_window(t, err), "window tier: kBegin/kRows served on the FIRST channel");
    int32_t rows = -1;
    check(serve_prompt(t, 4000, rows) == expert_helper::kOk && rows == 4,
          "prompt tier: kBeginP/kRowsP served on the SECOND channel");
    check(wA == 1 && pB == 1 && pA == 0 && wB == 0,
          "the two tiers' frames travelled on DIFFERENT sockets (the split is real, not a label)");
    check(t.prompt_requests() == 1 && t.prompt_rows() == 4 && t.helper_requests() == 1,
          "each tier's counters count its own channel's traffic");

    // Kill the prompt channel.
    stopB = true;
    for (int i = 0; i < 400 && upB; ++i) std::this_thread::sleep_for(std::chrono::milliseconds(4));
    check(!upB, "the prompt channel's peer is closed (the test's kill)");
    t.note_request_start();
    int32_t r2 = -1;
    const int rc = serve_prompt(t, 4000, r2);
    check(rc == expert_helper::kErrUnhealthy,
          "the dead prompt channel refuses by name (no hang, no crash, nothing applied)");
    check(!t.prompt_usable(), "the prompt tier's own latch is set - and ONLY its latch");
    check(t.helper_usable() && t.proxy_residency_live(),
          "the WINDOW tier is untouched by the prompt channel's death");
    check(serve_window(t, err) && wA == 2, "the window tier serves the request the prompt tier died in");

    // The next request re-arms and re-pins: the prompt tier falls back to the primary channel.
    t.note_request_start();
    check(t.prompt_usable(), "the prompt tier re-arms on the primary channel at the request boundary");
    int32_t r3 = -1;
    check(serve_prompt(t, 4000, r3) == expert_helper::kOk && r3 == 4 && pA == 1,
          "the fallback prompt rows are served on the primary channel");
    check(t.helper_usable() && serve_window(t, err) && wA == 3,
          "the window tier keeps serving on its own channel through all of it");
    t.close();
    ta.join();
    tb.join();
}

void run_window_channel_dies() {
    std::printf("(E) two channels: the WINDOW channel dies, the prompt tier keeps serving\n");
    const std::string pa = test_path("e1"), pb = test_path("e2");
    unlink(pa.c_str());
    unlink(pb.c_str());
    std::atomic<bool> upA{ false }, upB{ false }, stopA{ false }, stopB{ false };
    std::atomic<int> wA{ 0 }, pA{ 0 }, wB{ 0 }, pB{ 0 };
    std::thread ta(fake_channel, pa, std::ref(upA), std::ref(stopA), std::ref(wA), std::ref(pA), true);
    std::thread tb(fake_channel, pb, std::ref(upB), std::ref(stopB), std::ref(wB), std::ref(pB), false);
    for (int i = 0; i < 500 && (!upA || !upB); ++i) std::this_thread::sleep_for(std::chrono::milliseconds(4));

    std::vector<std::pair<int32_t, int32_t>> ranked{ { 3, 10 }, { 7, 20 }, { 41, 5 } };
    std::vector<uint8_t> claimed((size_t) kLayers * kExperts, 0);
    RemoteExperts t;
    std::string err;
    const bool opened = t.open_socket(pa, 3, kLayers, kExperts, kH, kFF, 0, kLayers - 1,
                                      512, 4000, ranked, claimed, err, pb);
    check(opened, "open_socket with a second path registers the proxy on TWO channels");
    check(t.prompt_buffer_ensure(kH) == expert_helper::kOk,
          "the prompt tier's pinned activation buffer maps on BOTH channels");
    t.note_request_start();
    check(serve_window(t, err) && wA == 1, "window tier served on the first channel");
    int32_t rows = -1;
    check(serve_prompt(t, 4000, rows) == expert_helper::kOk && pB == 1,
          "prompt tier served on the second channel");

    // Kill the WINDOW channel (the peer closes after the first window).
    stopA = true;
    for (int i = 0; i < 400 && upA; ++i) std::this_thread::sleep_for(std::chrono::milliseconds(4));
    check(!upA, "the window channel's peer is closed (the test's kill)");
    t.note_request_start();
    check(t.prompt_usable(), "the prompt tier is untouched by the window channel's death");
    int32_t r2 = -1;
    check(serve_prompt(t, 4000, r2) == expert_helper::kOk && r2 == 4 && pB == 2,
          "the prompt tier keeps serving on its own channel, request after request");
    // The window tier's own rule: an unusable tier claims ZERO rows and SUCCEEDS (the CPU computes
    // them), and its residency is released - the promotion gate must stop consulting it.
    check(serve_window(t, err, false), "window tier: a dead channel claims zero rows and the window stays served locally");
    check(!t.helper_usable(), "the window tier latched - and only the window tier");
    check(!t.proxy_residency_live(), "the gate stops consulting the dead window channel (residency released)");
    check(t.prompt_usable(), "the prompt tier is STILL usable after the window channel closed");
    t.close();
    ta.join();
    tb.join();
}

}  // namespace

int main() {
    std::string why;
    check(socket_path_ok(test_path("probe").c_str(), why), "the test's socket paths satisfy the bind rules");
    run_registered_lifecycle();
    run_engine_close();
    run_empty_fill();
    run_prompt_channel_dies();
    run_window_channel_dies();
    std::printf("%s: %d failure(s)\n",
                failures == 0 ? "expert_helper_ownership_test PASS" : "FAIL", failures);
    return failures == 0 ? 0 : 1;
}
