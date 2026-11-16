// tests/core/expert_helper_xq_ring_test.cpp - F2 (STRATA_HELPER_XQ_RING, default OFF): the mapped
// q8_1 ring's claim-path legs, host-only:
//   (1) the knob: exactly "1" enables;
//   (2) default-off equivalence - the gate unset sends today's CPU-quantized frame bytes;
//   (3) gate on + a valid ring image - the frame bytes are IDENTICAL to (2) for the same input
//       activations (the whole F2 proof: producer change, bytes equal - build_window_payload memcpys
//       whatever image it is given, so the test stands the GPU's quantize_q8_1_rows image in with
//       its own byte-identical CPU reference, exactly the f32_window test's golden-dump harness);
//   (4) every miss - ring invalid, byte-count mismatch, a FOLDED layer - falls back to (2)'s bytes
//       even when the ring holds different bytes (the fallback is proven, not assumed);
//   (5) the multi-window stale-seq discipline: the per-window m_xq_seq_ reset (stage_inputs' own
//       code path, shared through xq_ring_reset/xq_ring_ready) must NOT pass the wait on window 1's
//       stale values, and the window-2 claim frame carries window 2's image.  The review's blocking
//       bug class, caught here instead of on the lane; the graph-replay leg runs with the engine
//       gates (the plan's ladder step c).
// CPU-only by construction, exactly the expert_helper_ownership_test pattern: proxy mode makes no
// device call, the fake AF_UNIX helper answers hello/fill and records the kBegin frames, and the
// native layout is a v5 fixture pack (native_experts.txt + hadamard.txt) the test writes itself.
//
// Exit 0 = every check passed.  Linux only (AF_UNIX): the CMake target is guarded.
#include "strata/core/expert_helper_socket.hpp"
#include "strata/core/expert_helper_xq_ring.hpp"
#include "strata/core/remote_experts.hpp"
#include "strata/kernels/cpu/expert_layout.hpp"
#include "strata/kernels/cpu/native_expert.hpp"

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <random>
#include <string>
#include <thread>
#include <vector>

#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#include "ggml.h"

using namespace strata::core;
using namespace strata::core::expert_helper;
using strata::kernels::cpu::NativeFmt;

namespace {

int failures = 0;
void check(bool ok, const char* what) {
    std::printf("  %-70s %s\n", what, ok ? "ok" : "FAIL");
    if (!ok) ++failures;
}

constexpr int64_t kLayers = 48, kExperts = 512, kH = 2560, kFF = 640;
constexpr int64_t kNTok = 8, kK = 10, kN = kNTok * kK;
const int32_t kLayer = 3, kLayerFolded = 4;   // (3,10) and (4,20) are helper-held (the fill below)
constexpr int64_t kImgBytes = kNTok * (kH / 32) * 36;
constexpr size_t kResBytes = (size_t) ((kNTok * kExperts + 7) / 8);   // 512 B primary bitmap
constexpr size_t kImageAt = sizeof(BeginReq) + (size_t) kN * 4 + kResBytes;

/// The v5 fixture pack: 48 Q8_0/Q8_0 layers with contiguous offsets (the blob size computed by the
/// engine's own native_fmt, so the fixture is right on every build) and layer 4 Hadamard-folded on
/// gate/up (the had_gu leg of the fallback checks).  Q8_0's vec_dot_type is Q8_0: gu_act != Q8_K,
/// the ring-eligible shape.
bool load_fixture_layout(const std::string& dir) {
    NativeFmt f;
    std::string err;
    if (!strata::kernels::cpu::native_fmt((int) GGML_TYPE_Q8_0, (int) GGML_TYPE_Q8_0, kH, kFF, f, err)) {
        std::fprintf(stderr, "native_fmt: %s\n", err.c_str());
        return false;
    }
    const std::string ne = dir + "/native_experts.txt";
    std::FILE* o = std::fopen(ne.c_str(), "w");
    if (o == nullptr) return false;
    std::fprintf(o, "# strata native experts v5 (n_expert %lld)\n", (long long) kExperts);
    uint64_t at = 0;
    for (int64_t l = 0; l < kLayers; ++l) {
        std::fprintf(o, "%lld %d %d %llu %llu\n", (long long) l, (int) GGML_TYPE_Q8_0,
                     (int) GGML_TYPE_Q8_0, (unsigned long long) at, (unsigned long long) f.bytes);
        at += f.bytes * (uint64_t) kExperts;
    }
    std::fclose(o);
    const std::string hd = dir + "/hadamard.txt";
    o = std::fopen(hd.c_str(), "w");
    if (o == nullptr) return false;
    std::fprintf(o, "# strata hadamard v1\nblock 256\nlayer %d 1 1 0\n", (int) kLayerFolded);
    std::fclose(o);
    return strata::kernels::cpu::expert_layout_load(dir, kLayers, kExperts, err);
}

std::mutex frames_mtx;
std::vector<std::vector<uint8_t>> frames;   // the kBegin bodies the fake helper read, in order

/// The ownership test's fake helper, extended: kBegin frames are recorded instead of killing the
/// channel, so one connection answers a whole test's begins.
void fake_helper(const std::string& path, std::atomic<bool>& up) {
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
        if (conn.read_frame(head, rx, body, deadline_after(15000)) != kOk) break;
        if (head.type == kHello) {
            HelloAck ack;
            ack.max_tok = (int32_t) kMaxTok;
            ack.k = (int32_t) kK;
            ack.max_entries = (int32_t) kMaxEntries;
            ack.status = 0;
            ack.free_mib = 7000;
            ack.features = 0;   // no f32-window bit: the default kBegin wire, exactly F2's tier
            if (conn.write_reply(kHelloAck, head.req_id, head.seq, (const uint8_t*) &ack, sizeof(ack),
                                 deadline_after(2000)) != kOk) break;
        } else if (head.type == kFill) {
            std::vector<uint8_t> payload(sizeof(FillReport));
            FillReport rep;
            rep.resident = 3;
            rep.bytes = 3 * 2329856;
            rep.scratch_bytes = 0;
            rep.buffers_bytes = 0;
            rep.n_pairs = 3;
            std::memcpy(payload.data(), &rep, sizeof(rep));
            const int32_t pairs[3][2] = { { 3, 10 }, { 4, 20 }, { 41, 5 } };
            for (int i = 0; i < 3; ++i) {
                const size_t at = payload.size();
                payload.resize(at + 8);
                std::memcpy(payload.data() + at, &pairs[i][0], 4);
                std::memcpy(payload.data() + at + 4, &pairs[i][1], 4);
            }
            if (conn.write_reply(kFillReport, head.req_id, head.seq, payload.data(), payload.size(),
                                 deadline_after(2000)) != kOk) break;
        } else if (head.type == kBegin) {
            std::lock_guard<std::mutex> lk(frames_mtx);
            frames.emplace_back(body.begin(), body.end());
        } else {
            break;
        }
    }
    conn.close();
    srv.close();
    srv.unbind();
}

std::string test_path(const char* tag) {
    return std::string("/tmp/opencode/strata-xq-") + tag + "-" + std::to_string((long) getpid()) + ".sock";
}

/// One window's claim frame: every entry routes to the layer's helper-held expert, so the tier
/// claims all 80 and the frame is written.  Returns the frame BODY the helper recorded.
bool send_begin(RemoteExperts& t, const float* x, const uint8_t* xq, int64_t xq_bytes, int32_t layer,
                std::vector<uint8_t>& body) {
    const size_t before = frames.size();
    std::vector<int32_t> ids((size_t) kN, layer == kLayer ? 10 : 20);
    std::string err;
    std::vector<int32_t> claims;
    const bool ok = t.begin(layer, x, ids.data(), kNTok, kK, nullptr, nullptr, err, claims, xq, xq_bytes);
    if (!ok || claims.size() != (size_t) kN) return false;
    for (int i = 0; i < 200 && frames.size() == before; ++i)
        std::this_thread::sleep_for(std::chrono::milliseconds(2));
    std::lock_guard<std::mutex> lk(frames_mtx);
    if (frames.size() != before + 1) return false;
    body = frames.back();
    return true;
}

/// The image slice of a recorded kBegin body (the builder's own layout: req | ids | bitmap | image).
std::vector<uint8_t> image_of(const std::vector<uint8_t>& body) {
    if (body.size() < kImageAt) return {};
    const BeginReq* req = (const BeginReq*) body.data();
    if ((size_t) req->n_q8 != body.size() - kImageAt) return {};
    return std::vector<uint8_t>(body.begin() + (long) kImageAt, body.end());
}

}  // namespace

int main() {
    std::printf("(0) the knob\n");
    {
        ::unsetenv("STRATA_HELPER_XQ_RING");
        check(!helper_xq_ring_enabled(), "xq ring knob: absent = OFF (today's bytes)");
        ::setenv("STRATA_HELPER_XQ_RING", "0", 1);
        check(!helper_xq_ring_enabled(), "xq ring knob: 0 = OFF");
        ::setenv("STRATA_HELPER_XQ_RING", "1", 1);
        check(helper_xq_ring_enabled(), "xq ring knob: 1 = ON");
        ::setenv("STRATA_HELPER_XQ_RING", "12", 1);
        check(!helper_xq_ring_enabled(), "xq ring knob: 12 = OFF (exactly \"1\", not a prefix)");
        ::setenv("STRATA_HELPER_XQ_RING", "", 1);
        check(!helper_xq_ring_enabled(), "xq ring knob: empty = OFF");
        ::unsetenv("STRATA_HELPER_XQ_RING");
    }

    // the fixture layout (native, one folded layer) - the engine's own loader reads it
    const std::string dir = "/tmp/opencode/strata-xq-pack-" + std::to_string((long) getpid());
    ::mkdir("/tmp/opencode", 0755);
    ::mkdir(dir.c_str(), 0755);
    if (!load_fixture_layout(dir)) {
        std::printf("fixture pack: FAILED to load (native experts unavailable?)\n");
        return 1;
    }
    const auto& lay = strata::kernels::cpu::expert_layout();
    check(lay.native && (int64_t) lay.fmt.size() == kLayers, "fixture pack: a native v5 layout loaded");
    check(!lay.fmt[(size_t) kLayer].had_gu && lay.fmt[(size_t) kLayer].gu_act != (int) GGML_TYPE_Q8_K,
          "fixture pack: layer 3 is unfolded, not Q8_K-act (ring-eligible)");
    check(lay.fmt[(size_t) kLayerFolded].had_gu, "fixture pack: layer 4 is Hadamard-folded");

    // the deterministic window: two DIFFERENT activation sets (window 1 / window 2) and the ring
    // images that stand in for the GPU's quantize_q8_1_rows output of each
    std::mt19937 rng(1234);
    std::uniform_real_distribution<float> d(-1.f, 1.f);
    std::vector<float> x1((size_t) kNTok * kH), x2((size_t) kNTok * kH);
    for (auto& v : x1) v = d(rng);
    for (auto& v : x2) v = d(rng);
    std::vector<uint8_t> golden1(kImgBytes), golden2(kImgBytes);
    quantize_q8_1_cpu(x1.data(), kNTok * kH, golden1.data());
    quantize_q8_1_cpu(x2.data(), kNTok * kH, golden2.data());

    const std::string path = test_path("ring");
    ::unlink(path.c_str());
    std::atomic<bool> up{ false };
    std::thread th(fake_helper, path, std::ref(up));
    for (int i = 0; i < 500 && !up; ++i) std::this_thread::sleep_for(std::chrono::milliseconds(4));

    std::vector<std::pair<int32_t, int32_t>> ranked{ { 3, 10 }, { 4, 20 }, { 41, 5 } };
    std::vector<uint8_t> claimed((size_t) kLayers * kExperts, 0);
    RemoteExperts t;
    std::string err;
    const bool opened = t.open_socket(path, 3, kLayers, kExperts, kH, kFF, 0, kLayers - 1,
                                      512, 2000, ranked, claimed, err);
    check(opened, "open_socket registers the proxy over the fake helper");
    if (!opened) {
        std::printf("      open_socket: %s\n", err.c_str());
        th.join();
        return 1;
    }

    std::printf("(2) default-off equivalence\n");
    std::vector<uint8_t> body_off;
    check(send_begin(t, x1.data(), nullptr, 0, kLayer, body_off), "gate off: the begin claims and frames");
    check(body_off.size() == kImageAt + golden1.size(), "gate off: the frame is today's kBegin shape");
    check(image_of(body_off) == golden1, "gate off: the image is quantize_q8_1_cpu's bytes (golden)");

    std::printf("(3) gate on + a valid ring image: the frame is byte-identical\n");
    {
        ::setenv("STRATA_HELPER_XQ_RING", "1", 1);
        std::vector<uint8_t> body_ring;
        check(send_begin(t, x1.data(), golden1.data(), kImgBytes, kLayer, body_ring),
              "gate on, ring valid: the begin claims and frames");
        check(body_ring == body_off, "gate on, ring valid: the WHOLE frame equals the default-off frame");
        ::unsetenv("STRATA_HELPER_XQ_RING");
    }

    std::printf("(4) every miss falls back to today's bytes\n");
    {
        // a ring image that DIFFERS from the golden: if any miss path consumed it, the frame would
        // differ; each check pins the fallback, not the coincidence
        std::vector<uint8_t> poison = golden1;
        for (auto& b : poison) b ^= 0x5a;
        ::setenv("STRATA_HELPER_XQ_RING", "1", 1);
        std::vector<uint8_t> body;
        check(send_begin(t, x1.data(), nullptr, 0, kLayer, body), "ring invalid: the begin claims and frames");
        check(body == body_off, "ring invalid (null image): the fallback frame equals (2)");
        check(send_begin(t, x1.data(), poison.data(), kImgBytes - 36, kLayer, body),
              "byte-count mismatch: the begin claims and frames");
        check(body == body_off, "byte-count mismatch: the fallback frame equals (2)");
        check(send_begin(t, x1.data(), poison.data(), kImgBytes, kLayerFolded, body),
              "folded layer: the begin claims and frames");
        check(body.size() == kImageAt + golden1.size() && image_of(body) == golden1,
              "folded layer: the ring is NOT consumed - the image is the CPU quantize, not the poison");
        ::setenv("STRATA_HELPER_XQ_RING", "0", 1);
        check(send_begin(t, x1.data(), golden1.data(), kImgBytes, kLayer, body),
              "gate explicitly off with a valid image: the begin claims and frames");
        check(body == body_off, "gate off + valid image: the fallback frame equals (2)");
        ::unsetenv("STRATA_HELPER_XQ_RING");
    }

    std::printf("(5) the multi-window stale-seq discipline\n");
    {
        // the protocol level, through the same reset/readiness helpers stage_inputs and the service
        // loop use: window 1 leaves both seq words stale; WITHOUT a reset the wait would pass
        // instantly on them (the counterfactual), WITH stage_inputs' reset it must not, until
        // window 2's own publish rings the slot.
        uint32_t seq[2] = { 0, 0 };
        const uint32_t steps1 = 5, want2 = (uint32_t) 2;   // window 2 waits at its 2nd step
        for (uint32_t w = 1; w <= steps1; ++w) *(volatile uint32_t*) (seq + (w & 1)) = w;
        check(xq_ring_ready(seq, 0, want2) && xq_ring_ready(seq, 1, want2),
              "window 1 leaves both slots' seq >= window 2's first wants (the stale values)");
        xq_ring_reset(seq);   // stage_inputs' own call
        check(seq[0] == 0 && seq[1] == 0, "the reset zeroes both slots");
        check(!xq_ring_ready(seq, 0, want2) && !xq_ring_ready(seq, 1, want2),
              "after the reset the wait does NOT pass on window 1's stale values (the blocking bug)");
        *(volatile uint32_t*) (seq + (int) (1 & 1)) = want2;   // window 2's publish of step 1 (slot 1)
        check(xq_ring_ready(seq, 1, want2) && !xq_ring_ready(seq, 0, want2),
              "window 2's publish rings its own slot only - the other parity is still fresh-gated");

        // the tier level: window 2's claim frame carries window 2's image, not window 1's
        ::setenv("STRATA_HELPER_XQ_RING", "1", 1);
        std::vector<uint8_t> body_w2;
        check(send_begin(t, x2.data(), golden2.data(), kImgBytes, kLayer, body_w2),
              "window 2 (gate on, ring valid): the begin claims and frames");
        check(image_of(body_w2) == golden2 && image_of(body_w2) != golden1,
              "window 2's frame carries window 2's image (not window 1's)");
        ::unsetenv("STRATA_HELPER_XQ_RING");
    }

    t.close();
    th.join();
    ::unlink(path.c_str());
    ::unlink((dir + "/native_experts.txt").c_str());
    ::unlink((dir + "/hadamard.txt").c_str());
    ::rmdir(dir.c_str());
    std::printf("%s: %d failure(s)\n",
                failures == 0 ? "expert_helper_xq_ring_test PASS" : "FAIL", failures);
    return failures == 0 ? 0 : 1;
}
