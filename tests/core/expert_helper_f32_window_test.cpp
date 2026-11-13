// tests/core/expert_helper_f32_window_test.cpp - W1 (the fp32 WINDOW wire, kBeginF32) and W3's
// knobs, host-only: the client's negotiation gate, the frame's exact bytes, the default-off
// equivalence, and the GPU-quant golden compare HARNESS.  CPU-only by construction, exactly the
// expert_helper_socket_test pattern: the client, the frame transport and this driver link no GPU
// runtime (the helper's CUDA twin is exercised in the build window through the dump files below).
//
// Exit 0 = every check passed.  Linux only (AF_UNIX): the CMake target is guarded.
#include "strata/core/expert_helper_socket.hpp"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <thread>
#include <vector>

#include <sys/socket.h>
#include <unistd.h>

using namespace strata::core;
using namespace strata::core::expert_helper;

namespace {

int failures = 0;
void check(bool ok, const char* what) {
    std::printf("  %-64s %s\n", what, ok ? "ok" : "FAIL");
    if (!ok) ++failures;
}
void check_status(int rc, int want, const char* what) {
    const bool ok = rc == want;
    std::printf("  %-64s %s (rc=%d %s)\n", what, ok ? "ok" : "FAIL", rc, status_name(rc));
    if (!ok) ++failures;
}

constexpr int64_t kLayers = 48, kExperts = 512, kH = 2560, kFF = 640;
const int32_t kLayer = 3, kNTok = 8;
const size_t kResBytes = (size_t) ((kNTok * kExperts + 7) / 8);   // 512 B primary bitmap

int g_server_fd = -1;   // the socketpair end the fake helper's worker holds

/// The socket test's own compact handshake: hello + fill answered on a worker thread, with THIS
/// test's feature bits.  kOk when the client is connected, handshaken and filled.
int handshake(ExpertHelperClient& c, uint32_t extra_feats) {
    int fds[2];
    if (::socketpair(AF_UNIX, SOCK_STREAM, 0, fds) != 0) return kErrInternal;
    {
        Socket s;
        s.adopt(fds[1]);
        g_server_fd = s.fd();
        s.release();
    }
    int wire = 0;
    std::thread srv([fd = fds[1], &wire, extra_feats] {
        Socket s;
        s.adopt(fd);
        Header head;
        RxBuffer rx;
        PayloadView body;
        if (s.read_frame(head, rx, body, deadline_after(5000)) != kOk || head.type != kHello ||
            body.size() < sizeof(HelloReq)) { wire = 1; return; }
        HelloAck ack;
        ack.max_tok = (int32_t) kMaxTok;
        ack.k = (int32_t) kK;
        ack.max_entries = (int32_t) kMaxEntries;
        ack.status = 0;
        ack.free_mib = 4096;
        ack.features = extra_feats;
        if (s.write_frame(kHelloAck, head.req_id, (const uint8_t*) &ack, sizeof(ack),
                          deadline_after(5000)) != kOk) { wire = 2; return; }
        if (s.read_frame(head, rx, body, deadline_after(5000)) != kOk || head.type != kFill ||
            body.size() < sizeof(FillReq)) { wire = 3; return; }
        FillReport rep;
        rep.resident = 2;
        rep.bytes = 2 * 1310720;
        rep.n_pairs = 2;
        std::vector<uint8_t> payload(sizeof(rep) + 16, 0);
        std::memcpy(payload.data(), &rep, sizeof(rep));
        const int32_t pairs[4] = {kLayer, 7, 4, 9};
        std::memcpy(payload.data() + sizeof(rep), pairs, 16);
        if (s.write_frame(kFillReport, head.req_id, payload.data(), payload.size(),
                          deadline_after(5000)) != kOk) { wire = 4; return; }
        s.release();
    });
    const int rc0 = c.attach_socket(fds[0], true);
    if (rc0 != kOk) { srv.join(); return rc0; }
    int64_t free_mib = 0;
    int rc = c.hello(kLayers, kExperts, kH, kFF, 0, 47, 1024, 512,
                     std::vector<uint8_t>((size_t) kLayers * kExperts, 0), 5000, free_mib);
    if (rc == kOk) {
        std::vector<std::pair<int32_t, int32_t>> resident;
        int64_t bytes = 0;
        rc = c.fill_report({{kLayer, 7}, {4, 9}}, 5000, resident, bytes);
    }
    srv.join();
    if (rc == kOk && wire != 0) rc = kErrProtocol;
    return rc;
}

bool wire_silent() {
    Socket srv;
    srv.adopt(g_server_fd);
    Header head;
    RxBuffer rx;
    PayloadView body;
    const bool silent = srv.read_frame(head, rx, body, deadline_after(300)) == kErrTimeout;
    srv.release();
    return silent;
}

/// Read the frame the client just wrote and verify it as an exact kBeginF32: the BeginReq fields
/// with n_q8 = n_tok * kH * 4, the routed ids, the primary-residency bit, and the image = the
/// window's fp32 rows VERBATIM (the engine quantizes nothing on this wire).
bool send_begin_f32(ExpertHelperClient& c, const float* x) {
    std::vector<int32_t> ids((size_t) kNTok * 10, 7);
    std::vector<int32_t> primary((size_t) kLayers * kExperts, -1);
    primary[(size_t) kLayer * kExperts + 5] = 0;   // resident on the primary: not the helper's row
    const int64_t img = (int64_t) kNTok * kH * 4;
    const int rc = c.begin_f32(kLayer, kNTok, ids.data(), primary.data(), x, 2000);
    if (rc != kOk) return false;
    Socket srv;
    srv.adopt(g_server_fd);
    Header head;
    RxBuffer rx;
    PayloadView body;
    const int rrc = srv.read_frame(head, rx, body, deadline_after(2000));
    srv.release();
    if (rrc != kOk || head.type != kBeginF32) return false;
    if (body.size() < sizeof(BeginReq)) return false;
    BeginReq req;
    std::memcpy(&req, body.data(), sizeof(req));
    if (req.layer != kLayer || req.n_tok != kNTok || req.k != (int32_t) kK || req.reserved0 != 0 ||
        req.ids_bytes != (int32_t) (kNTok * 10 * 4) || req.n_q8 != (int32_t) img ||
        body.size() != sizeof(req) + (size_t) req.ids_bytes + kResBytes + (size_t) img)
        return false;
    const uint8_t* bits = body.data() + sizeof(req) + (size_t) req.ids_bytes;
    if (!(bits[5 >> 3] & (uint8_t) (1u << (5 & 7)))) return false;      // the primary bit is set
    if (bits[7 >> 3] & (uint8_t) (1u << (7 & 7))) return false;         // and not of a helper expert
    const uint8_t* got = bits + kResBytes;
    return std::memcmp(got, x, (size_t) img) == 0;                      // the rows, verbatim
}

/// Read the frame the client just wrote and verify it as a plain kBegin whose image is EXACTLY
/// quantize_q8_1_cpu of the same rows (today's bytes) - the default-off equivalence pin.
bool send_begin_q8_default(ExpertHelperClient& c, const float* x) {
    std::vector<int32_t> ids((size_t) kNTok * 10, 7);
    std::vector<int32_t> primary((size_t) kLayers * kExperts, -1);
    const int64_t img = (int64_t) kNTok * (kH / 32) * 36;
    std::vector<uint8_t> q8((size_t) img, 0);
    quantize_q8_1_cpu(x, kNTok * kH, q8.data());
    const int rc = c.begin(kLayer, kNTok, ids.data(), primary.data(), q8.data(), (size_t) img, 2000);
    if (rc != kOk) return false;
    Socket srv;
    srv.adopt(g_server_fd);
    Header head;
    RxBuffer rx;
    PayloadView body;
    const int rrc = srv.read_frame(head, rx, body, deadline_after(2000));
    srv.release();
    if (rrc != kOk || head.type != kBegin || body.size() < sizeof(BeginReq)) return false;
    BeginReq req;
    std::memcpy(&req, body.data(), sizeof(req));
    if (req.n_q8 != (int32_t) img ||
        body.size() != sizeof(req) + (size_t) req.ids_bytes + kResBytes + (size_t) img)
        return false;
    return std::memcmp(body.data() + sizeof(req) + (size_t) req.ids_bytes + kResBytes,
                       q8.data(), (size_t) img) == 0;
}

// ---- the golden compare harness (the GPU half runs in the build window, see section (5)) ----
//
// The deterministic input set: the socket test's own three blocks (all-zero, constant, mixed with
// the scale clamp / q saturation / -0.0 / a subnormal), one full window wide.
std::vector<float> golden_window_rows() {
    std::vector<float> x((size_t) kNTok * kH, 0.0f);
    for (int64_t t = 0; t < kNTok; ++t) {
        float* row = x.data() + (size_t) t * kH;
        for (int i = 0; i < 32; ++i) row[i] = 0.0f;                              // the all-zero block
        for (int i = 0; i < 32; ++i) row[32 + i] = 0.5f;                         // the constant block
        for (int i = 0; i < 32; ++i) row[64 + i] = (float) (i - 16) * 0.25f;     // rounding + clamps
        row[64] = 1e30f;                                                         // the #606 scale clamp
        row[96] = -0.0f;                                                         // -0.0 quantizes to 0
        row[97] = 1.0e-39f;                                                      // a subnormal
        row[98] = 3000.0f;                                                       // q saturates at +127
        row[99] = -3000.0f;                                                      // and at -127
        for (int i = 128; i < kH; ++i) row[i] = (float) ((i * 37 + (int) t * 11) % 251) * 0.031f - 3.5f;
    }
    return x;
}

/// THE COMPARE: `gpu_bytes` (what quantize_q8_1_f32_window_kernel wrote on the helper) against
/// quantize_q8_1_cpu of the same rows, byte for byte.  Returns -1 when identical, else the first
/// mismatching byte's index.  The bit-identity contract is the kernel's own comment
/// (helper_main.cpp): same statement order per 32-block, same clamps, __float2half_rn for the
/// fp16 scales - including the SUM field, which the sequential per-thread loop keeps in the CPU's
/// exact accumulation order.
long long f32_window_golden_compare(const float* x, int64_t n_values, const uint8_t* gpu_bytes,
                                    int64_t gpu_n_bytes) {
    std::vector<uint8_t> ref((size_t) (n_values / 32) * 36u, 0);
    quantize_q8_1_cpu(x, n_values, ref.data());
    const int64_t ref_n = (int64_t) ref.size();
    if (ref_n != gpu_n_bytes) return gpu_n_bytes < ref_n ? gpu_n_bytes : ref_n;
    for (int64_t i = 0; i < ref_n; ++i)
        if (ref[(size_t) i] != gpu_bytes[(size_t) i]) return i;
    return -1;
}

/// The build-window leg: STRATA_HELPER_F32_QUANT_DUMP=<prefix> on the HELPER wrote <prefix>.in
/// (16-byte header {img_bytes, n_blocks} + the fp32 rows) and <prefix>.q8 (the kernel's d_q8
/// bytes).  STRATA_TEST_F32_WINDOW_DUMP=<prefix> here makes this test READ them and run the
/// compare - the executable golden pin.  Without the env the leg is DEFERRED (a notice, not a
/// failure): no GPU run has produced the files yet.
void run_dump_compare() {
    const char* prefix = std::getenv("STRATA_TEST_F32_WINDOW_DUMP");
    if (prefix == nullptr || *prefix == '\0') {
        std::printf("  %-64s deferred (no GPU run yet)\n",
                    "f32 golden dump compare (set STRATA_TEST_F32_WINDOW_DUMP=<prefix>)");
        return;
    }
    const std::string in_path = std::string(prefix) + ".in";
    const std::string q8_path = std::string(prefix) + ".q8";
    std::FILE* fi = std::fopen(in_path.c_str(), "rb");
    std::FILE* fq = std::fopen(q8_path.c_str(), "rb");
    if (fi == nullptr || fq == nullptr) {
        check(false, "f32 golden dump compare: the dump files are readable");
        if (fi) std::fclose(fi);
        if (fq) std::fclose(fq);
        return;
    }
    int64_t header[2] = {0, 0};
    const bool hdr_ok = std::fread(header, sizeof(header), 1, fi) == 1;
    std::vector<float> x;
    std::vector<uint8_t> gpu;
    bool ok = hdr_ok && header[0] > 0 && (header[0] % 4) == 0 && header[1] == (header[0] / 4) / 32;
    if (ok) {
        x.resize((size_t) (header[0] / 4));
        ok = std::fread(x.data(), 4, x.size(), fi) == x.size();
    }
    if (ok) {
        gpu.resize((size_t) header[1] * 36u);
        ok = std::fread(gpu.data(), 1, gpu.size(), fq) == gpu.size();
    }
    std::fclose(fi);
    std::fclose(fq);
    if (!ok) {
        check(false, "f32 golden dump compare: the dump files parse (header + rows + blocks)");
        return;
    }
    const long long diff = f32_window_golden_compare(x.data(), (int64_t) x.size(), gpu.data(),
                                                     (int64_t) gpu.size());
    check(diff == -1, "f32 golden dump compare: the GPU quant is quantize_q8_1_cpu byte for byte");
    if (diff >= 0)
        std::printf("      first mismatch at byte %lld (block %lld, offset %lld)\n",
                    (long long) diff, (long long) (diff / 36), (long long) (diff % 36));
}

}  // namespace

int main() {
    // ---- (1) the knobs: exactly "1" enables; absent/empty/0/anything else keeps today's bytes ----
    {
        ::unsetenv("STRATA_HELPER_F32_WINDOW");
        check(!helper_f32_window_enabled(), "f32 window knob: absent = OFF (today's bytes)");
        ::setenv("STRATA_HELPER_F32_WINDOW", "0", 1);
        check(!helper_f32_window_enabled(), "f32 window knob: 0 = OFF");
        ::setenv("STRATA_HELPER_F32_WINDOW", "1", 1);
        check(helper_f32_window_enabled(), "f32 window knob: 1 = ON");
        ::setenv("STRATA_HELPER_F32_WINDOW", "12", 1);
        check(!helper_f32_window_enabled(), "f32 window knob: 12 = OFF (exactly \"1\", not a prefix)");
        ::setenv("STRATA_HELPER_F32_WINDOW", "", 1);
        check(!helper_f32_window_enabled(), "f32 window knob: empty = OFF");
        ::unsetenv("STRATA_HELPER_F32_WINDOW");

        ::unsetenv("STRATA_HELPER_WIN_FAST");
        check(!helper_win_fast_enabled(), "win-fast knob: absent = OFF (today's bytes)");
        ::setenv("STRATA_HELPER_WIN_FAST", "0", 1);
        check(!helper_win_fast_enabled(), "win-fast knob: 0 = OFF");
        ::setenv("STRATA_HELPER_WIN_FAST", "1", 1);
        check(helper_win_fast_enabled(), "win-fast knob: 1 = ON");
        ::setenv("STRATA_HELPER_WIN_FAST", "yes", 1);
        check(!helper_win_fast_enabled(), "win-fast knob: yes = OFF (exactly \"1\")");
        ::unsetenv("STRATA_HELPER_WIN_FAST");
    }

    const std::vector<float> x = golden_window_rows();

    // ---- (2) the negotiation gate: begin_f32 without the advertised bit is a LOCAL refusal ----
    {
        ExpertHelperClient c;
        check_status(handshake(c, kFeaturePromptRows), kOk, "handshake without the f32 window bit");
        std::vector<int32_t> ids((size_t) kNTok * 10, 7);
        std::vector<int32_t> primary((size_t) kLayers * kExperts, -1);
        check_status(c.begin_f32(kLayer, kNTok, ids.data(), primary.data(), x.data(), 2000),
                     kErrRequest, "begin_f32 without the bit: the local refusal (nothing written)");
        check(wire_silent(), "and the wire stayed silent (an old helper never sees the frame type)");
    }

    // ---- (3) with the bit: the kBeginF32 frame is exact, the image the rows verbatim ----
    {
        ExpertHelperClient c;
        check_status(handshake(c, kFeatureF32Window), kOk, "handshake advertising kFeatureF32Window");
        check(c.f32_window_supported(), "the client records the advertised fp32 window wire");
        check(send_begin_f32(c, x.data()), "the kBeginF32 frame carries the window's exact shape and rows");
    }

    // ---- (4) the default-off equivalence: the CLIENT never switches frame types by itself.
    // The ENGINE's env gate (proxy_begin) is the other half - pinned end-to-end by the ownership
    // test's f32-wire section; here the pin is that begin() with the env SET and the bit
    // advertised still writes today's kBegin with the engine-quantized image. ----
    {
        ::setenv("STRATA_HELPER_F32_WINDOW", "1", 1);
        ExpertHelperClient c;
        check_status(handshake(c, kFeatureF32Window), kOk, "handshake advertising the bit, env ON");
        check(send_begin_q8_default(c, x.data()),
              "env ON, bit advertised: plain begin() is still today's kBegin, byte for byte");
        ::unsetenv("STRATA_HELPER_F32_WINDOW");
    }

    // ---- (5) the golden compare harness ----
    {
        // Self-check on the CPU reference itself: the compare accepts its own input.
        std::vector<uint8_t> ref((size_t) (kNTok * kH / 32) * 36u, 0);
        quantize_q8_1_cpu(x.data(), kNTok * kH, ref.data());
        check(f32_window_golden_compare(x.data(), kNTok * kH, ref.data(), (int64_t) ref.size()) == -1,
              "golden compare: the harness accepts the reference bytes (sanity)");
        // And rejects a one-byte perturbation (the compare can actually fail).
        ref[7] ^= 0x01;
        check(f32_window_golden_compare(x.data(), kNTok * kH, ref.data(), (int64_t) ref.size()) == 7,
              "golden compare: a one-byte perturbation is caught at its index");
        // The build-window leg: the helper's dump files, when a GPU run produced them.
        run_dump_compare();
    }

    // ---- (6) the wire's size arithmetic: the f32 image is n_tok*n_embd*4, well under the cap ----
    {
        const int64_t img = (int64_t) kMaxTok * kH * 4;
        check(img <= (int64_t) kMaxFramePayload,
              "the f32 image at the caps (8 * 2560 * 4 B) is under kMaxFramePayload");
    }

    std::printf("%s: %d failure(s)\n",
                failures == 0 ? "expert_helper_f32_window_test PASS" : "FAIL", failures);
    return failures == 0 ? 0 : 1;
}
