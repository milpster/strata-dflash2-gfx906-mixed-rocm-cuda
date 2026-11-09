// tests/core/expert_helper_socket_test.cpp - R2 (opt-in CUDA expert helper): the engine's client
// against a FAKE helper over a socketpair.  CPU-only by construction: the client, the frame
// transport and this driver link no GPU runtime (the readelf/nm evidence is taken on the built
// objects), and nothing here opens a device.  The fake helper answers the real frames with real
// payloads, so every fault the engine can meet on the wire is met HERE, in the exact code path the
// engine runs: timeouts and stalls, disconnects, oversized/malformed/duplicate/non-finite rows,
// zero-row replies, request-id mismatches, and the path/bind/peer rules of the helper's socket.
//
// Exit 0 = every check passed.  Linux only (AF_UNIX): the CMake target is guarded.  The only library
// linked is ggml-base (CPU-only: ggml's fp16 conversion, for section (8)'s quantizer pin) - no
// ggml-cpu, no ggml-cuda, no ggml-hip, no device runtime.
#include "strata/core/expert_helper_socket.hpp"

#include <atomic>
#include <cerrno>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <set>
#include <string>
#include <thread>
#include <vector>

#include <fcntl.h>
#include <poll.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/uio.h>
#include <sys/wait.h>
#include <csignal>
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
const size_t kQ8 = (size_t) kNTok * (kH / 32) * 36;   // 23,040 B: the window's q8_1 blocks
const size_t kResBytes = (size_t) ((kNTok * kExperts + 7) / 8);   // 512 B primary bitmap

// H8's pinned flat indices (layer*n_expert+expert) in the engine's real 48x512 geometry: byte 0's
// first bit, byte 0's last bit, byte 1's first bit, byte 2047's last bit, byte 2048's first bit (the
// helper's own range, flat >= 16384, starts here), and byte 3071's last bit (the packed size's end).
constexpr size_t kClaimFlat[] = {0, 7, 8, 16383, 16384, 24575};
bool kClaimAt(size_t i) {
    for (const size_t s : kClaimFlat) if (s == i) return true;
    return false;
}

int g_server_fd = -1;   // the fake helper's end of the current socketpair

/// Build a kRows payload: `idx` original entry indices, row values from v(index, column).
std::vector<uint8_t> rows_payload(const std::vector<int32_t>& idx, double (*v)(int, int64_t)) {
    RowsResp h;
    h.n_rows = (int32_t) idx.size();
    std::vector<uint8_t> payload(sizeof(h));
    std::memcpy(payload.data(), &h, sizeof(h));
    for (const int32_t i : idx) {
        const int32_t idx32 = i;
        const size_t at = payload.size();
        payload.resize(at + 4 + (size_t) kH * 4, 0);
        std::memcpy(payload.data() + at, &idx32, 4);
        float* row = (float*) (payload.data() + at + 4);
        for (int64_t c = 0; c < kH; ++c) row[c] = (float) v(i, c);
    }
    return payload;
}
double row_val(int i, int64_t c) { return 0.25 * (double) i + 0.001 * (double) c; }
double row_nan(int i, int64_t c) { return c == 17 ? std::nanf("x") : row_val(i, c); }

// ---- the q8_1 quantizer's golden inputs (R2 gate M2's pin; see section (8) in main) ----
// Three deterministic blocks.  The GOLDEN BYTES below are frozen from a ONE-TIME run of
// quantize_q8_1_cpu itself (`expert_helper_socket_test --dump-golden` prints them): a regression pin
// against accidental change of the code, NOT a claim that the CUDA kernel writes the same bytes.
// amax/d/q ARE provably device-identical; the fp16 sum field is ULP-close, not proven bit-equal.
const int64_t kG1 = 32, kG2 = 32, kG3 = 64;
float gx1[32];                 // (1) the all-zero block
float gx2[32];                 // (2) the constant block (every value 0.5f)
float gx3[64];                 // (3) the mixed block: rounding, the +-127 clamp, the #606 scale clamp
void init_golden_inputs() {
    for (int i = 0; i < 32; ++i) gx1[i] = 0.0f;
    for (int i = 0; i < 32; ++i) gx2[i] = 0.5f;
    // block A: 32 values -12.00 .. 11.75 in steps of 0.25, x[0] replaced by 1e30 (the scale clamps to
    // 65504, its own quantized value comes back small); block B: 32 values around the +-127 clamp of q
    // (a handful sit far past one scale step, so roundf(x/d) saturates), with a -0.0 and a subnormal.
    for (int i = 0; i < 32; ++i) gx3[i] = (float) (i - 16) * 0.25f;
    gx3[0] = 1e30f;
    for (int i = 0; i < 32; ++i) gx3[32 + i] = (float) (i % 8) * 1.5f - 5.0f;
    gx3[32] = -0.0f;
    gx3[33] = 1.0e-39f;                 // a subnormal float
    gx3[34] = 3000.0f;                  // far past 127*d: q saturates at +127
    gx3[35] = -3000.0f;                 // and at -127
}
std::vector<uint8_t> q8_of(const float* x, int64_t n) {
    std::vector<uint8_t> y((size_t) (n / 32) * 36u, 0xCD);   // 0xCD: a block the code must overwrite
    quantize_q8_1_cpu(x, n, y.data());
    return y;
}
void dump_golden(const char* name, const float* x, int64_t n) {
    const std::vector<uint8_t> y = q8_of(x, n);
    std::printf("  %s [%zu] = {", name, y.size());
    for (size_t i = 0; i < y.size(); ++i) std::printf("0x%02X%s", y[i], i + 1 < y.size() ? ", " : "");
    std::printf("}\n");
}
// the frozen bytes (the run above, 2026-10-27, host C++/ggml's ggml_fp32_to_fp16):
//   kGoldenZero: ds = (0,0), 32 zeros.            kGoldenConst: ds = (0x1C08 = 0.0039368, 0x4C00 = 16.0), 32x 0x7F.
//   kGoldenMixed block A: ds = (0x7BFF = 65504, 0x7BFF = 65504), q = 0x7F then zeros.
//   kGoldenMixed block B: ds = (0x4DE8 = 23.625, 0x4CC0 = 19.0), q = 0,0,+127,-127, then the cycle.
const uint8_t kGoldenZero[36] = {
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00};
const uint8_t kGoldenConst[36] = {
    0x08, 0x1C, 0x00, 0x4C, 0x7F, 0x7F, 0x7F, 0x7F, 0x7F, 0x7F, 0x7F, 0x7F, 0x7F, 0x7F, 0x7F, 0x7F,
    0x7F, 0x7F, 0x7F, 0x7F, 0x7F, 0x7F, 0x7F, 0x7F, 0x7F, 0x7F, 0x7F, 0x7F, 0x7F, 0x7F, 0x7F, 0x7F,
    0x7F, 0x7F, 0x7F, 0x7F};
const uint8_t kGoldenMixed[72] = {
    0xFF, 0x7B, 0xFF, 0x7B, 0x7F, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0xE8, 0x4D, 0xC0, 0x4C, 0x00, 0x00, 0x7F, 0x81, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00};

/// The fake helper answers the outstanding request with a kRows frame carrying `payload`.
/// `req_id` is the client's request counter (the begin is the Nth request since connect).
void write_rows(const uint8_t* payload, size_t n, uint32_t req_id) {
    Socket srv;
    srv.adopt(g_server_fd);
    srv.write_frame(kRows, req_id, payload, n, deadline_after(5000));

}

/// hello + fill, run to the end: the fake helper answers on a WORKER THREAD (the client blocks
/// for its reply, so the two sides must be live at once - exactly the two-process reality).
/// kOk when the client is connected, handshaken and filled; the hello/fill payloads the client
/// sent are verified on the wire by the worker.
int handshake(ExpertHelperClient& c, uint32_t extra_feats = 0) {
    int fds[2];
    if (::socketpair(AF_UNIX, SOCK_STREAM, 0, fds) != 0) return kErrInternal;
    {
        Socket s;
        s.adopt(fds[1]);
        g_server_fd = s.fd();
        s.release();
    }
    int wire = 0;   // the worker's view of the client's payloads: 0 = all good
    std::thread srv([fd = fds[1], &wire, extra_feats] {
        Socket s;
        s.adopt(fd);
        Header head;
        RxBuffer rx;
        PayloadView body;
        if (s.read_frame(head, rx, body, deadline_after(5000)) != kOk || head.type != kHello ||
            body.size() < sizeof(HelloReq)) { wire = 1; return; }
        HelloReq req;
        std::memcpy(&req, body.data(), sizeof(req));
        if (req.n_layers != (int32_t) kLayers || req.n_expert != (int32_t) kExperts ||
            req.n_embd != (int32_t) kH || req.n_ff != (int32_t) kFF ||
            req.slots != 1024 || req.reserve_mib != 512 || req.reserved0 != 0 ||
            req.claimed_bytes != (uint32_t) ((kLayers * kExperts + 7) / 8) ||
            body.size() != sizeof(req) + (size_t) req.claimed_bytes) { wire = 2; return; }
        HelloAck ack;
        ack.max_tok = (int32_t) kMaxTok;
        ack.k = (int32_t) kK;
        ack.max_entries = (int32_t) kMaxEntries;
        ack.status = 0;
        ack.free_mib = 4096;
        ack.features = extra_feats;   // (19): a peer advertising e.g. the Q8_K wire
        if (s.write_frame(kHelloAck, head.req_id, (const uint8_t*) &ack, sizeof(ack), deadline_after(5000)) != kOk) { wire = 3; return; }
        if (s.read_frame(head, rx, body, deadline_after(5000)) != kOk || head.type != kFill ||
            body.size() < sizeof(FillReq)) { wire = 4; return; }
        FillReq f;
        std::memcpy(&f, body.data(), sizeof(f));
        if (f.n_pairs != 2 || body.size() != sizeof(f) + 16 || f.reserved0 != 0) { wire = 5; return; }
        FillReport rep;
        rep.resident = 2;
        rep.bytes = 2 * 1310720;
        rep.scratch_bytes = 1 << 20;
        rep.buffers_bytes = 2 << 20;
        rep.n_pairs = 2;
        std::vector<uint8_t> payload(sizeof(rep) + 16, 0);
        std::memcpy(payload.data(), &rep, sizeof(rep));
        const int32_t pairs[4] = {kLayer, 7, 4, 9};
        std::memcpy(payload.data() + sizeof(rep), pairs, 16);
        if (s.write_frame(kFillReport, head.req_id, payload.data(), payload.size(), deadline_after(5000)) != kOk) { wire = 6; return; }
        s.release();
    });
    const int rc0 = c.attach_socket(fds[0], true);
    if (rc0 != kOk) { srv.join(); return rc0; }
    int64_t free_mib = 0;
    int rc = c.hello(kLayers, kExperts, kH, kFF, 0, 47, 1024, 512,
                     std::vector<uint8_t>((size_t) kLayers * kExperts, 0), 5000, free_mib);
    if (rc != kOk) { srv.join(); return rc; }
    std::vector<std::pair<int32_t, int32_t>> resident;
    int64_t bytes = 0;
    rc = c.fill_report({{kLayer, 7}, {4, 9}}, 5000, resident, bytes);
    srv.join();
    if (rc != kOk) return rc;
    if (wire != 0) return kErrProtocol;
    if (resident.size() != 2 || resident[0].first != kLayer || resident[0].second != 7 ||
        resident[1].first != 4 || resident[1].second != 9 || bytes != 2 * 1310720)
        return kErrShort;
    return kOk;
}

/// One window's kBegin, sent by the client and decoded by the fake helper (the geometry the
/// helper will check): 80 ids of expert 7, expert 5 marked primary-resident, 23,040 q8 bytes.
bool send_begin(ExpertHelperClient& c) {
    std::vector<int32_t> ids((size_t) kNTok * 10, 7);
    std::vector<int32_t> primary((size_t) kLayers * kExperts, -1);
    primary[(size_t) kLayer * kExperts + 5] = 0;   // resident on the primary: not the helper's row
    std::vector<uint8_t> q8(kQ8, 0x5A);
    const int rc = c.begin(kLayer, kNTok, ids.data(), primary.data(), q8.data(), kQ8, 2000);
    if (rc != kOk) return false;
    Socket srv;
    srv.adopt(g_server_fd);
    Header head;
    RxBuffer rx;
    PayloadView body;
    const int rrc = srv.read_frame(head, rx, body, deadline_after(2000));
    srv.release();
    if (rrc != kOk || head.type != kBegin) return false;
    if (body.size() < sizeof(BeginReq)) return false;
    BeginReq req;
    std::memcpy(&req, body.data(), sizeof(req));
    if (req.layer != kLayer || req.n_tok != kNTok || req.k != (int32_t) kK || req.reserved0 != 0 ||
        req.ids_bytes != (int32_t) (kNTok * 10 * 4) || req.n_q8 != (int32_t) kQ8 ||
        body.size() != sizeof(req) + (size_t) req.ids_bytes + kResBytes + kQ8)
        return false;
    const int32_t* got_ids = (const int32_t*) (body.data() + sizeof(req));
    for (int64_t i = 0; i < kNTok * 10; ++i)
        if (got_ids[i] != 7) return false;
    const uint8_t* bits = body.data() + sizeof(req) + (size_t) req.ids_bytes;
    if (!(bits[5 >> 3] & (uint8_t) (1u << (5 & 7)))) return false;      // the primary bit is set
    if (bits[7 >> 3] & (uint8_t) (1u << (7 & 7))) return false;         // and not of a helper expert
    const uint8_t* got_q8 = bits + kResBytes;
    for (size_t i = 0; i < kQ8; i += 4096)
        if (got_q8[i] != 0x5A) return false;
    return true;
}

// ---- H15: the SIGPIPE-DEFAULT children (see section (11) in main) ----
// A TEST-LOCAL copy of the transport's PRE-fix write loop (r25a and earlier, H15): `writev`, which
// takes no message flags - so the MSG_NOSIGNAL this transport's contract claims was never on the wire,
// and a dead peer killed the writer with SIGPIPE (the engine's observed exit -13).  Built only by this
// test, as the fixture's negative control: it is not production code and production never calls it.
int h15_writev_pre_fix(int fd, const uint8_t* head, size_t hn, const uint8_t* payload, size_t pn) {
    struct iovec v[2];
    v[0].iov_base = (void*) head;    v[0].iov_len = hn;
    v[1].iov_base = (void*) payload; v[1].iov_len = pn;
    size_t off = 0;
    while (off < hn + pn) {
        pollfd p{fd, POLLOUT, 0};
        if (::poll(&p, 1, 2000) <= 0) return -1;
        const ssize_t w = ::writev(fd, v, 2);
        if (w <= 0) return -1;
        off += (size_t) w;
    }
    return 0;
}

/// The fixture BOTH H15 children build: a socketpair whose WRITER has its own write side shut down
/// (SHUT_WR) while the peer stays OPEN.  Measured on this host's kernel (7.2.12-x64v3-xanmod1) with
/// probe-h15.c: poll(POLLOUT) answers revents 0x4 - POLLOUT, no POLLHUP - so the transport's poll
/// passes and the write syscall IS reached, and that write answers EPIPE.  A fully closed peer answers
/// POLLOUT|POLLHUP (0x14), which poll_once's POLLHUP rule can short-circuit into a "unhealthy" that
/// never wrote anything; this shape cannot, so the child's outcome is evidence about the flags, not
/// about the poll.  Returns 0 = fixture live; 11..14 = which step of it failed.
int h15_fixture(int fds[2]) {
    if (::socketpair(AF_UNIX, SOCK_STREAM, 0, fds) != 0) return 11;
    set_io_nonblocking(fds[0]);
    if (::shutdown(fds[0], SHUT_WR) != 0) return 12;
    pollfd p{fds[0], POLLOUT, 0};
    const int pr = ::poll(&p, 1, 0);
    if (pr != 1 || !(p.revents & POLLOUT) || (p.revents & (POLLHUP | POLLERR))) return 13;
    // Prove the write path is live BEFORE pinning the transport: a raw sendmsg carrying the flag must
    // reach the kernel and come back EPIPE.  Without this, a kErrUnhealthy could be a poll artifact.
    char b[8] = {1};
    struct iovec v;
    v.iov_base = (void*) b;
    v.iov_len = sizeof(b);
    struct msghdr mh = {};
    mh.msg_iov = &v;
    mh.msg_iovlen = 1;
    const ssize_t w = ::sendmsg(fds[0], &mh, MSG_NOSIGNAL);
    if (w >= 0 || errno != EPIPE) return 14;
    return 0;
}

struct ChildOutcome { bool signaled; int signal; int code; };
// code < 0 is never a child's status: -1 = fork failed, -2 = waitpid failed.  Both fail every H15
// check, so a broken harness can never read as the expected outcome.

/// Fork one H15 child (mode 0 = the transport as built today, mode 1 = the pre-fix writev copy) and
/// wait for it.  SIGPIPE is restored to SIG_DFL IN THE CHILD ONLY - the harness keeps its own
/// disposition.  The child writes nothing to stdio and exits with _exit, so the parent's buffered
/// output is never duplicated.
ChildOutcome run_h15_child(int mode) {
    std::fflush(nullptr);
    const pid_t pid = ::fork();
    if (pid < 0) return ChildOutcome{false, 0, -1};      // no child: unmistakable failure, never a pass
    if (pid == 0) {
        std::signal(SIGPIPE, SIG_DFL);              // the proof runs under the DEFAULT disposition
        int fds[2];
        const int frc = h15_fixture(fds);
        if (frc != 0) _exit(frc);                   // the fixture itself broke: report, never a false pass
        Header h;
        h.type = kRows;
        h.req_id = 1;
        h.len = 4;
        uint8_t head[kHeaderBytes];
        encode_header(h, head);
        static const uint8_t body[4] = {0, 0, 0, 0};
        if (mode == 1) {
            (void) h15_writev_pre_fix(fds[0], head, kHeaderBytes, body, sizeof(body));
            _exit(15);                              // the pre-fix path must die by SIGPIPE before this
        }
        Socket s;
        s.adopt(fds[0]);
        const int rc = s.write_frame(kRows, 1, body, sizeof(body), deadline_after(2000));
        s.close();                                  // the child closes both ends before it exits
        ::close(fds[1]);
        _exit(rc == kErrUnhealthy ? 0 : 20 - rc);   // 0 = the named status; 20-rc names what came back
    }
    int st = 0;
    pid_t got = -1;
    do { got = ::waitpid(pid, &st, 0); } while (got < 0 && errno == EINTR);   // signals are the harness's own
    if (got != pid) return ChildOutcome{false, 0, -2};   // the child's state is unknown: fail, never pass
    return ChildOutcome{WIFSIGNALED(st), WIFSIGNALED(st) ? WTERMSIG(st) : 0,
                        WIFEXITED(st) ? WEXITSTATUS(st) : -1};
}

// ================= R5 P1: the prompt tier's fixtures (sections (12)..(18)) =================
// The tier's own pinned rows, as THIS test filled them (wire_prompt_ok compares the frame's q8 image
// against these bytes through the same quantizer the client used, so the frame is pinned to the
// buffer the tier owns - not to a copy the test made for the check).
const float* g_prompt_x = nullptr;
int64_t g_prompt_embd = 0;

/// Put `n` deterministic rows in the tier's OWN pinned buffer (row r = the plan's r-th selected
/// token's activation, the order begin_prompt quantizes in).  The caps are checked BEFORE the
/// buffer is read, so a cap case fills at most the mapping's rows: never a write past the mapping.
void fill_prompt_buffer(ExpertHelperClient& c, int n) {
    if (c.prompt_buffer_ensure(kH) != kOk) return;
    int rows = n;
    if (rows > (int) kMaxSelTok) rows = (int) kMaxSelTok;      // the cap is the mapping's size
    float* x = const_cast<float*> (c.prompt_buffer());
    g_prompt_x = x;
    g_prompt_embd = kH;
    for (int r = 0; r < rows; ++r)
        for (int64_t col = 0; col < kH; ++col) x[(size_t) r * kH + (size_t) col] = (float) row_val(r, col);
}

/// hello with the v3 shape only: the ack is 32 bytes carrying `features = kFeaturePromptRows` when
/// `features`, and the R2 helper's 24-byte LEGACY ack (kHelloAckLegacyBytes, no features field)
/// otherwise.  No fill: the tier needs the hello geometry, and the layer window ENDS AT kLayer (3)
/// so the cap section's layer 4 is outside it.  The ack is answered on a worker thread (the client's
/// hello blocks for it) and the peer's end stays in g_server_fd for the frames the test reads itself.
int handshake_v3(ExpertHelperClient& c, bool features, uint32_t extra_feats = 0) {
    int fds[2];
    if (::socketpair(AF_UNIX, SOCK_STREAM, 0, fds) != 0) return kErrInternal;
    {
        Socket s;
        s.adopt(fds[1]);
        g_server_fd = s.fd();
        s.release();
    }
    std::atomic<int> wire{0};
    std::thread srv([fd = fds[1], features, extra_feats, &wire] {
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
        ack.features = features ? ((uint64_t) kFeaturePromptRows | (uint64_t) extra_feats) : 0u;
        const size_t n = features ? sizeof(ack) : kHelloAckLegacyBytes;   // the negotiated SHAPE
        if (s.write_frame(kHelloAck, head.req_id, (const uint8_t*) &ack, n, deadline_after(5000)) != kOk)
            wire = 2;
        s.release();   // the peer's end lives on: the test reads the tier's frames from it
    });
    const int rc0 = c.attach_socket(fds[0], true);
    if (rc0 != kOk) { srv.join(); return rc0; }
    int64_t free_mib = 0;
    const int rc = c.hello(kLayers, kExperts, kH, kFF, 0, kLayer, 1024, 512,
                           std::vector<uint8_t>((size_t) kLayers * kExperts, 0), 5000, free_mib);
    srv.join();
    if (wire != 0) return kErrProtocol;
    return rc;
}

/// Decode the kBeginP the client sent and check it EXACTLY: the header's frame type, every BeginPReq
/// field, the byte total, the pair list as the image's (token row, expert), the layer's PRIMARY
/// residency bits, and the image byte-for-byte - q8 against quantize_q8_1_cpu of the tier's own rows,
/// fp32 (M4c (b)) against those rows verbatim.
bool prompt_body_ok(PayloadView body, const ExpertHelperPromptPlan& plan,
                    const int32_t* primary, int rows, bool f32) {
    if (body.size() < sizeof(BeginPReq)) return false;
    BeginPReq req;
    std::memcpy(&req, body.data(), sizeof(req));
    const int64_t n_sel = plan.n_sel_tok();
    const int64_t n_img = f32 ? n_sel * kH * (int64_t) sizeof(float) : n_sel * (kH / 32) * 36;
    const size_t res_bytes = (size_t) ((n_sel * kExperts + 7) / 8);
    if (req.layer != kLayer || req.n_sel_tok != (int32_t) n_sel || req.k != (int32_t) kK ||
        req.n_pairs != rows || req.n_q8 != (int32_t) n_img || req.image_format != (f32 ? 1 : 0))
        return false;
    if (body.size() != sizeof(req) + (size_t) rows * 8 + res_bytes + (size_t) n_img) return false;
    for (int i = 0; i < rows; ++i) {
        const int32_t tok = (int32_t) plan.image_row(plan.pair_token[(size_t) i]);
        int32_t got_tok = 0, got_expert = 0;
        std::memcpy(&got_tok, body.data() + sizeof(req) + (size_t) i * 8, 4);
        std::memcpy(&got_expert, body.data() + sizeof(req) + (size_t) i * 8 + 4, 4);
        if (got_tok != tok || got_expert != plan.pair_expert[(size_t) i]) return false;
    }
    const uint8_t* res = body.data() + sizeof(req) + (size_t) rows * 8;
    for (int64_t e = 0; e < kExperts; ++e) {   // kBegin's own residency semantics, expert-indexed bits
        const bool want = primary != nullptr && primary[(size_t) kLayer * kExperts + (size_t) e] >= 0;
        const bool got = (res[(size_t) (e >> 3)] >> (e & 7)) & 1;
        if (want != got) return false;
    }
    for (size_t i = (size_t) ((kExperts + 7) / 8); i < res_bytes; ++i)
        if (res[i] != 0) return false;   // the image's spare bits stay clear
    if (g_prompt_x == nullptr || g_prompt_embd != kH) return false;
    const uint8_t* q8 = res + res_bytes;
    if (f32) {
        // M4c (b): the fp32 image is the tier's OWN rows verbatim - ONE contiguous memcmp, no
        // quantizer on the engine side.  The helper-side twin is bit-identical to the golden-byte
        // quantizer section (12) pins, so this closes the fp32 wire contract with no GPU in sight.
        return std::memcmp(q8, g_prompt_x, (size_t) (n_sel * kH * (int64_t) sizeof(float))) == 0;
    }
    std::vector<uint8_t> want((size_t) (kH / 32) * 36, 0);
    for (int64_t r = 0; r < n_sel; ++r) {
        quantize_q8_1_cpu(g_prompt_x + (size_t) r * kH, kH, want.data());
        if (std::memcmp(q8 + (size_t) r * (size_t) (kH / 32) * 36, want.data(), want.size()) != 0)
            return false;
    }
    return true;
}

/// Read a SMALL kBeginP off the wire and hand the captured body to prompt_body_ok.  The fake peer
/// reads only AFTER begin_prompt returns, so a frame past the socket buffer (~208-256 KB) deadlocks
/// the client's write into its deadline - those sizes (the cap-shape q8 of 12b, the fp32 of 12c)
/// go through begin_prompt_drained + prompt_body_ok(g_req_body, ...) instead.
bool wire_prompt_ok(const ExpertHelperPromptPlan& plan, const int32_t* primary, int rows, bool f32 = false) {
    Socket srv;
    srv.adopt(g_server_fd);
    Header head;
    RxBuffer rx;
    PayloadView body;
    const int rc = srv.read_frame(head, rx, body, deadline_after(2000));
    srv.release();
    if (rc != kOk || head.type != kBeginP) return false;
    return prompt_body_ok(body, plan, primary, rows, f32);
}

std::vector<int32_t> make_range(int n) {
    std::vector<int32_t> v((size_t) n);
    for (int i = 0; i < n; ++i) v[(size_t) i] = i;
    return v;
}

/// The kRowsP body: kRows' layout verbatim (RowsPResp and RowsResp are the same 8-byte shape, then
/// (int32 index, n_embd floats) per row), so the payload builder is the decode one.
std::vector<uint8_t> rowsp_payload(const std::vector<int32_t>& idx, double (*v)(int, int64_t)) {
    return rows_payload(idx, v);
}

/// Answer the outstanding kBeginP on a WORKER THREAD (the client blocks for its reply - exactly the
/// two-process reality): write the kRowsP reply echoing the request's identity, then join (the join
/// is the ownership boundary: the reply is queued before the client reads) and run the client's
/// finish_prompt.  The identity is the client's SECOND request (hello=1, kBeginP=2) and the sequence
/// its per-call socket assigns it (1): the seq-fault section pins exactly that pair, so a reply that
/// echoes anything else fails as kErrSeq and never reads as a pass.
/// Returns the client's status; the fake helper's own failure never reads as a pass.
int write_prompt_reply_thread(const std::vector<int32_t>& idx, double (*v)(int, int64_t), int64_t timeout_ms,
                              ExpertHelperClient& c, float* out, const std::vector<int32_t>& claims,
                              int32_t& rows) {
    std::atomic<int> srv_rc{kErrInternal};
    std::thread srv([fd = g_server_fd, idx, v, &srv_rc] {
        std::this_thread::sleep_for(std::chrono::milliseconds(50));   // the client is reading by now
        Socket s;
        s.adopt(fd);
        const auto payload = rowsp_payload(idx, v);
        srv_rc = s.write_reply(kRowsP, 2, 1, payload.data(), payload.size(), deadline_after(20000));
        s.release();
    });
    const int frc = c.finish_prompt(out, kH, claims, timeout_ms, rows);
    srv.join();      // the writer is done: nothing is in flight when the client's read returns
    if (srv_rc != kOk) return kErrInternal;      // the peer broke: never a pass
    return frc;
}

/// The CAP-SIZE request needs a peer that drains while the client writes: a 737 KB kBeginP is far
/// more than the AF_UNIX buffer, so a nonblocking write to a peer that is not reading is a full
/// socket buffer, and the transport's answer to that is its named timeout - not a bug.  This helper
/// runs the read on a thread CONCURRENT with begin_prompt (the two-process reality) and keeps the
/// frame the peer received for the test to inspect.
Header g_req_head;
std::vector<uint8_t> g_req_body;
std::atomic<int> g_req_rc{kErrInternal};
int begin_prompt_drained(ExpertHelperClient& c, const ExpertHelperPromptPlan& plan, const int32_t* primary,
                         int64_t deadline_ms) {
    g_req_rc = kErrInternal;
    std::thread reader([fd = g_server_fd, &c] {
        Socket s;
        s.adopt(fd);
        Header head;
        RxBuffer rx;
        PayloadView body;
        const int rc = s.read_frame(head, rx, body, deadline_after(20000));
        if (rc == kOk && head.type == kBeginP) {
            g_req_head = head;
            g_req_body.assign(body.begin(), body.end());
            g_req_rc = kOk;
        } else {
            g_req_rc = rc;
        }
        s.release();
    });
    const int rc = c.begin_prompt(kLayer, plan, primary, deadline_ms);
    reader.join();
    return rc;
}

/// Did the fake peer receive NOTHING within `ms`?  Only the poll's own expiry answers true: a ready
/// descriptor, a closed channel, an error - all answer false, so a client that wrote bytes, or that
/// closed the channel on a refusal, fails the check instead of passing it.
bool nothing_on_wire(int ms) {
    if (g_server_fd < 0) return false;
    pollfd p{g_server_fd, POLLIN, 0};
    return ::poll(&p, 1, ms) == 0;
}

// ---- the SIGTERM begin-wait children (the item-0 fix, section (18)) ----
// No GPU, no helper process, no live window: a socketpair whose READ end is waited on with the
// driver's own 30-minute --begin-timeout, the same self-pipe + handler the CUDA helper installs
// (helper_main.cpp), and the transport's real poll_once.  The harness keeps its own signal
// dispositions; every child sets its own and reports over a pipe, exiting with _exit (no stdio in
// the child, so the parent's buffered output is never duplicated).
enum { kSigWoken = 0, kSigControlNoWake = 1, kSigMidFrame = 2, kSigRealSignal = 3 };
constexpr long long kSigBeginTimeoutMs = 1800000;   // the driver's --begin-timeout 1800000 verbatim
constexpr long long kSigControlDeadlineMs = 300;    // the control's short, honored deadline
int g_sig_wake_write = -1;                          // the handler's wake-pipe write end
/// The handler's write, and the test timer's: a failed wake byte is a broken fixture, so the child
/// says so with its exit code instead of letting a deadline expiry read as the wake.
void sig_wake_byte() {
    const char b = 1;
    const ssize_t wr = ::write(g_sig_wake_write, &b, 1);
    if (wr != 1) _exit(14);
}
void sigterm_handler(int) {                         // write() is async-signal-safe: the design's rule
    sig_wake_byte();
}
struct SigOutcome { bool woken; long long ms; int rc; bool child_signaled; };
struct SigReport { int rc; long long ms; };        // the child's measured outcome

/// mode kSigWoken:        the wake byte alone (no signal) ends the 30-minute wait.
/// mode kSigControlNoWake: the SAME handler and the SAME byte, with NO wake installed
///                          (set_poll_wake stays -1, the engine's client's state) and a 300 ms
///                          deadline: the deadline is honored, so this isolates the installed wake as
///                          the single variable - a signal by itself never ended the wait.
/// mode kSigMidFrame:     the peer sent a header + a partial body, then the wake arrives: the
///                          transfer aborts as kErrUnhealthy, never as a half-consumed frame.
/// mode kSigRealSignal:   the parent sends a REAL SIGTERM (kill, the driver's exact signal); the
///                          child's handler writes the wake byte - the end-to-end path.
SigOutcome run_sigterm_child(int mode) {
    int report[2];
    if (::pipe2(report, O_CLOEXEC) != 0) return SigOutcome{false, -1, kErrInternal, false};
    std::fflush(nullptr);
    const pid_t pid = ::fork();
    if (pid < 0) { ::close(report[0]); ::close(report[1]); return SigOutcome{false, -1, kErrInternal, false}; }
    if (pid == 0) {
        ::close(report[0]);
        int fds[2];
        if (::socketpair(AF_UNIX, SOCK_STREAM, 0, fds) != 0) _exit(11);
        int wp[2];
        if (::pipe2(wp, O_CLOEXEC | O_NONBLOCK) != 0) _exit(12);   // the helper's exact self-pipe
        g_sig_wake_write = wp[1];
        if (mode != kSigControlNoWake) set_poll_wake(wp[0]);       // the control leaves it -1
        if (mode == kSigControlNoWake || mode == kSigRealSignal) {
            struct sigaction sa{};
            sa.sa_handler = sigterm_handler;
            sigemptyset(&sa.sa_mask);
            sa.sa_flags = 0;                        // NO SA_RESTART: the design relies on the byte
            if (::sigaction(SIGTERM, &sa, nullptr) != 0) _exit(13);
        }
        if (mode == kSigMidFrame) {   // the peer: a whole header, then PART of the body
            std::thread peer([fd = fds[1]] {
                Header h;
                h.type = kRowsP;
                h.req_id = 1;
                h.seq = 1;
                h.len = 1000;
                uint8_t raw[kHeaderBytes];
                encode_header(h, raw);
                uint8_t part[100];
                for (int i = 0; i < 100; ++i) part[i] = (uint8_t) i;
                Socket s;
                s.adopt(fd);
                s.write_all(raw, sizeof(raw), deadline_after(1000));
                s.write_all(part, sizeof(part), deadline_after(1000));
                s.release();      // the child keeps fds[1] open: no EOF, a stalled helper
            });
            peer.join();
        }
        std::thread timer;
        if (mode != kSigRealSignal) {   // the wake byte / the control's signal, DURING the wait:
            timer = std::thread([mode] {   // the thread is NOT joined before the read - a wake that
                std::this_thread::sleep_for(std::chrono::milliseconds(200));
                if (mode == kSigControlNoWake) ::kill(::getpid(), SIGTERM);   // the byte, NO wake
                else sig_wake_byte();      // arrives before the wait begins is a woken poll, not the
            });                            // driver's event, and a 30-minute wait would read as 0 ms
        }
        Socket s;
        s.adopt(fds[0]);
        const auto t0 = std::chrono::steady_clock::now();
        Header head;
        RxBuffer rx;
        PayloadView body;
        const int rc = s.read_frame(head, rx, body,
                                    deadline_after(mode == kSigControlNoWake ? kSigControlDeadlineMs
                                                                           : kSigBeginTimeoutMs));
        const long long ms = (long long) std::chrono::duration_cast<std::chrono::milliseconds>(
                                 std::chrono::steady_clock::now() - t0).count();
        if (timer.joinable()) timer.join();
        s.close();
        ::close(fds[1]);
        ::close(wp[0]);
        ::close(wp[1]);
        SigReport rep{rc, ms};
        size_t off = 0;
        while (off < sizeof(rep)) {
            const ssize_t w = ::write(report[1], (const uint8_t*) &rep + off, sizeof(rep) - off);
            if (w <= 0) break;
            off += (size_t) w;
        }
        ::close(report[1]);
        _exit(0);
    }
    ::close(report[1]);
    if (mode == kSigRealSignal) {   // the driver's signal, from OUTSIDE the waiting process
        std::this_thread::sleep_for(std::chrono::milliseconds(300));
        ::kill(pid, SIGTERM);
    }
    int st = 0;
    pid_t got = -1;
    do { got = ::waitpid(pid, &st, 0); } while (got < 0 && errno == EINTR);
    SigReport rep{kErrInternal, -1};
    size_t off = 0;
    while (off < sizeof(rep)) {
        const ssize_t r = ::read(report[0], (uint8_t*) &rep + off, sizeof(rep) - off);
        if (r <= 0) break;
        off += (size_t) r;
    }
    ::close(report[0]);
    const bool signaled = WIFSIGNALED(st);
    if (got != pid || off != sizeof(rep)) return SigOutcome{false, -1, kErrInternal, signaled};
    // "woken" = the wait ended EARLY, at the wake, not at its deadline: for the control that is false
    // by construction (its own 300 ms deadline IS the expiry).
    const bool early = rep.ms < 2000;
    const bool woken = (mode == kSigControlNoWake) ? false : (early && rep.rc == kErrTimeout);
    return SigOutcome{woken, rep.ms, rep.rc, signaled};
}

// ---- the SIGTERM-survives-the-re-listen child (mode kSigPeerLeavesBeforeHello) ----
// The live defect of 2026-10-29 ~06:07 (build-mixed-gpu/p2-measure-window1/helper2.log): ONE SIGTERM
// printed the helper's own "SIGTERM/SIGINT received; exiting cleanly" line ~10 times, and each time the
// "the peer left before hello; NOT exiting, listening again" branch re-bound the socket, so the helper
// survived 60 s to a SIGKILL.  This child reproduces that edge with the real signal and the real
// transport, and NOTHING else: no GPU, no helper process, no socket on a path, no signal outside the
// child.  The loop is the helper's control flow (helper_main.cpp:763-914: accept, the loop-top latch
// check, the read, the pre-hello re-listen edge through the shared predicate).  A peer that connects and
// leaves before hello is a socketpair whose peer end closes at once, which is exactly the hot
// connect/leave cycle the live log shows.
//   exit 0  = the latch won: the child took the clean exit path
//   exit 42 = the child kept re-listening (the live defect)
//   11..14  = the fixture itself broke
std::atomic<int> g_child_stop{0};
int g_child_wake = -1;
void child_latch(int) {
    g_child_stop.store(1);
    const char b = 1;
    if (g_child_wake >= 0) {
        const ssize_t wr = ::write(g_child_wake, &b, 1);   // the helper's async-signal-safe wake byte
        (void) wr;
    }
}
int run_relisten_child() {
    g_child_stop.store(0);
    std::fflush(nullptr);
    const pid_t pid = ::fork();
    if (pid < 0) return -1;
    if (pid == 0) {
        int wp[2];
        if (::pipe2(wp, O_CLOEXEC | O_NONBLOCK) != 0) _exit(12);
        g_child_wake = wp[1];
        set_poll_wake(wp[0]);
        struct sigaction sa{};
        sa.sa_handler = child_latch;
        sigemptyset(&sa.sa_mask);
        sa.sa_flags = 0;
        if (::sigaction(SIGTERM, &sa, nullptr) != 0) _exit(13);
        bool hello_seen = false;
        // A round is a socketpair + a read that answers EOF at once, so the connect/leave cycle is hot
        // (the live log's pattern).  The cap is far above what 300 ms of spinning reaches: the child
        // ends on the SIGNAL, never on the cap - reaching the cap is the live defect (exit 42).
        for (int round = 0; round < 5000000; ++round) {
            int fds[2];
            if (::socketpair(AF_UNIX, SOCK_STREAM, 0, fds) != 0) _exit(11);
            Socket s;
            s.adopt(fds[0]);        // the accept: a peer is there
            ::close(fds[1]);        // it left before hello (EOF on the channel)
            bool relisten = false;
            for (;;) {
                if (g_child_stop.load()) break;                     // the loop-top latch check
                Header head;
                RxBuffer rx;
                PayloadView body;
                const int rc = s.read_frame(head, rx, body, deadline_after(5000));
                if (rc == kErrTimeout) {
                    if (g_child_stop.load()) break;                 // the wake byte, not an idle engine
                    break;                                          // the engine idled: the helper exits
                }
                if (rc != kOk) break;                               // the channel died: the helper exits
                if (head.type == kHello) { hello_seen = true; continue; }
                break;                                              // any other frame ends the loop
            }
            const bool stop = g_child_stop.load() != 0;
            s.close();
            relisten = helper_relisten_allowed(stop, hello_seen, false);   // the helper's own guard
            if (!relisten) _exit(0);                                // the clean exit: release, unlink
        }
        _exit(42);                                                  // never stopped: the live defect
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(300));
    ::kill(pid, SIGTERM);                                           // the driver's exact signal
    for (int i = 0; i < 400; ++i) {                                 // bounded: 4 s, the live test's 60 s
        int st = 0;
        const pid_t got = ::waitpid(pid, &st, WNOHANG);
        if (got == pid) return WIFEXITED(st) ? WEXITSTATUS(st) : -2;   // -2: died by a signal
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    ::kill(pid, SIGKILL);   // the live run's last resort; the child must never need it
    for (int i = 0; i < 200; ++i) {
        int st = 0;
        if (::waitpid(pid, &st, WNOHANG) == pid) break;
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    return -3;                                                      // survived the signal: the live FAIL
}

/// Section (19): the Q8_K wire's window frame - begin_q8k's transport contract.  The same window
/// send_begin pins (80 ids of expert 7, expert 5 primary-resident), with the image as Q8_K blocks:
/// kNTok * (kH/256) * 292 bytes (23,360 at this geometry), frame type kBeginQ8K, n_q8 = the image.
bool send_begin_q8k(ExpertHelperClient& c) {
    std::vector<int32_t> ids((size_t) kNTok * 10, 7);
    std::vector<int32_t> primary((size_t) kLayers * kExperts, -1);
    primary[(size_t) kLayer * kExperts + 5] = 0;   // resident on the primary: not the helper's row
    const int64_t img = kNTok * (kH / 256) * 292;
    std::vector<uint8_t> q8k((size_t) img, 0x5A);
    const int rc = c.begin_q8k(kLayer, kNTok, ids.data(), primary.data(), q8k.data(), (size_t) img, 2000);
    if (rc != kOk) return false;
    Socket srv;
    srv.adopt(g_server_fd);
    Header head;
    RxBuffer rx;
    PayloadView body;
    const int rrc = srv.read_frame(head, rx, body, deadline_after(2000));
    srv.release();
    if (rrc != kOk || head.type != kBeginQ8K) return false;
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
    for (int64_t i = 0; i < img; i += 4096)
        if (got[(size_t) i] != 0x5A) return false;                      // the image is verbatim
    return true;
}

/// Section (19): read a SMALL kBeginP off the wire and verify it as an image_format 2 frame: the
/// header's exact fields, the pinned image size (n_sel * (kH/256) * 292), and the image bytes
/// themselves - quantize_row_q8k_cpu of the tier's OWN rows, in selection order (the same rule
/// wire_prompt_ok pins for the q8_1/f32 images).
bool wire_prompt_q8k_ok(const ExpertHelperPromptPlan& plan, const int32_t* primary) {
    Socket srv;
    srv.adopt(g_server_fd);
    Header head;
    RxBuffer rx;
    PayloadView body;
    const int rc = srv.read_frame(head, rx, body, deadline_after(2000));
    srv.release();
    if (rc != kOk || head.type != kBeginP || body.size() < sizeof(BeginPReq)) return false;
    BeginPReq req;
    std::memcpy(&req, body.data(), sizeof(req));
    const int64_t n_sel = (int64_t) plan.tokens.size();
    const int64_t n_pairs = (int64_t) plan.pair_token.size();
    const size_t res_bytes = (size_t) ((n_sel * kExperts + 7) / 8);
    const int64_t img_row = (kH / 256) * 292;
    if (req.layer != kLayer || req.n_sel_tok != n_sel || req.k != (int32_t) kK ||
        req.n_pairs != n_pairs || req.image_format != 2 || req.n_q8 != n_sel * img_row)
        return false;
    if (body.size() != sizeof(req) + (size_t) n_pairs * 8 + res_bytes + (size_t) (n_sel * img_row))
        return false;
    const uint8_t* img = body.data() + sizeof(req) + (size_t) n_pairs * 8 + res_bytes;
    std::vector<uint8_t> want((size_t) (n_sel * img_row), 0);
    for (int64_t r = 0; r < n_sel; ++r)
        quantize_row_q8k_cpu(g_prompt_x + r * g_prompt_embd, g_prompt_embd,
                             want.data() + (size_t) r * (size_t) img_row);
    return std::memcmp(img, want.data(), want.size()) == 0;
}

}  // namespace

int main(int argc, char** argv) {
    // the test harness deliberately writes through sockets whose peer dies mid-window, so it ignores
    // SIGPIPE for ITSELF.  Note the honesty of the claim: every send the transport makes carries
    // MSG_NOSIGNAL only since H15 (r25a) - before it, write_seq used writev, which takes no flags.
    // This global SIG_IGN MASKS exactly that regression: with SIGPIPE ignored, a send with no
    // MSG_NOSIGNAL to a dead peer is no longer fatal - the kernel raises the signal, the harness
    // discards it, the write returns EPIPE, and the transport reports the ordinary kErrUnhealthy, so
    // the suite passes WITH the bug.  Under SIG_DFL the same write kills the process with signal 13
    // (the engine's observed exit -13).  Section (11) therefore proves the flag in FORKED CHILDREN
    // that restore SIGPIPE to SIG_DFL; the engine process itself never changes its disposition.
    std::signal(SIGPIPE, SIG_IGN);
    std::printf("expert_helper_socket_test:\n");
    init_golden_inputs();
    if (argc > 1 && std::strcmp(argv[1], "--dump-golden") == 0) {   // the one-time golden generator
        dump_golden("kGoldenZero", gx1, kG1);
        dump_golden("kGoldenConst", gx2, kG2);
        dump_golden("kGoldenMixed", gx3, kG3);
        return 0;
    }

    // ---- (1) the full valid exchange: hello, fill, begin, rows at the ORIGINAL indices ----
    {
        ExpertHelperClient c;
        check_status(handshake(c), kOk, "hello + fill handshake (payloads verified on the wire)");
        check(send_begin(c), "kBegin carries ids/primary bitmap/q8 with exact lengths");
        const std::vector<int32_t> claims = {0, 10, 20};
        const auto payload = rows_payload({20, 0, 10}, row_val);
        write_rows(payload.data(), payload.size(), 3);   // begin is request 3 (hello=1, fill=2)
        std::vector<float> out((size_t) kNTok * 10 * kH, -1.0f);
        int32_t rows = -1;
        check_status(c.finish(out.data(), kH, kNTok, 10, claims, 5000, rows), kOk,
                     "a valid kRows reply is accepted");
        check(rows == 3, "the reply reports its 3 rows");
        bool placed = true;
        for (const int32_t i : claims)
            for (int64_t col = 0; col < kH; ++col)
                if (out[(size_t) i * kH + (size_t) col] != (float) row_val(i, col)) placed = false;
        check(placed, "rows land at their ORIGINAL entry indices, out of frame order");
        check(out[(size_t) 3 * kH] == -1.0f, "an unclaimed row is untouched");
        check(c.stat_requests() == 1 && c.stat_rows() == 3 &&
              c.stat_bytes() == 3ull * kH * 4ull, "request/row/byte stats count the window");
        c.bye();
    }

    // ---- (2) zero rows: success, the CPU serves the window (the zero-claims contract) ----
    {
        ExpertHelperClient c;
        check_status(handshake(c), kOk, "handshake for the zero-row window");
        check(send_begin(c), "kBegin sent");
        RowsResp h;
        h.n_rows = 0;
        std::vector<uint8_t> payload(sizeof(h), 0);
        std::memcpy(payload.data(), &h, sizeof(h));
        write_rows(payload.data(), payload.size(), 3);
        std::vector<float> out((size_t) kNTok * 10 * kH, -1.0f);
        int32_t rows = -1;
        check_status(c.finish(out.data(), kH, kNTok, 10, {}, 5000, rows), kOk,
                     "a zero-row reply is accepted with no claims");
        check(rows == 0 && out[0] == -1.0f, "a zero-row reply writes nothing");
    }

    // ---- (3) a stalled helper: one bounded poll, then the channel is closed ----
    {
        ExpertHelperClient c;
        check_status(handshake(c), kOk, "handshake for the stall case");
        check(send_begin(c), "kBegin sent");
        std::vector<float> out((size_t) kNTok * 10 * kH, 0.0f);
        int32_t rows = -1;
        const auto t0 = std::chrono::steady_clock::now();
        const int rc = c.finish(out.data(), kH, kNTok, 10, {0}, 150, rows);
        const long long ms = (long long) std::chrono::duration_cast<std::chrono::milliseconds>(
                                 std::chrono::steady_clock::now() - t0).count();
        check_status(rc, kErrTimeout, "a stalled helper costs the named timeout");
        check(ms >= 140 && ms < 3000, "the bounded poll waits about the deadline, not unbounded");
        check(c.stat_timeouts() == 1, "the timeout is counted for the DONE line");
        check_status(c.finish(out.data(), kH, kNTok, 10, {0}, 100, rows), kErrUnhealthy,
                     "after a timeout the channel is closed, not retried");
    }

    // ---- (4) a helper kError for the outstanding begin ----
    {
        ExpertHelperClient c;
        check_status(handshake(c), kOk, "handshake for the kError case");
        check(send_begin(c), "kBegin sent");
        ErrResp e;
        e.status = kErrGeometry;
        std::strncpy(e.text, "layer outside the helper's range", sizeof(e.text) - 1);
        Socket srv;
        srv.adopt(g_server_fd);
        srv.write_frame(kError, 3, (const uint8_t*) &e, sizeof(e), deadline_after(2000));

        srv.release();
        std::vector<float> out((size_t) kNTok * 10 * kH, 0.0f);
        int32_t rows = -1;
        check_status(c.finish(out.data(), kH, kNTok, 10, {0}, 5000, rows), kErrRequest,
                     "the helper's named refusal surfaces as kErrRequest");
    }

    // ---- (5) rows payload faults: applied WHOLE or not at all ----
    {
        const std::vector<int32_t> claims = {0, 10, 20};
        std::vector<float> out((size_t) kNTok * 10 * kH, -7.0f);
        int32_t rows = -1;
        const auto good = rows_payload({20, 0, 10}, row_val);
        check_status(validate_rows_payload(good.data(), good.size(), kH, claims, out.data(), rows),
                     kOk, "validate: the valid payload passes");
        check(rows == 3, "validate: row count");
        std::fill(out.begin(), out.end(), -7.0f);
        auto p = rows_payload({20, 0, 99}, row_val);
        check_status(validate_rows_payload(p.data(), p.size(), kH, claims, out.data(), rows), kErrBadId,
                     "validate: an index outside the claims is refused");
        p = rows_payload({0, 0, 20}, row_val);
        check_status(validate_rows_payload(p.data(), p.size(), kH, claims, out.data(), rows), kErrBadId,
                     "validate: a duplicated row is refused");
        p = rows_payload({0, 10}, row_val);
        check_status(validate_rows_payload(p.data(), p.size(), kH, claims, out.data(), rows), kErrMissing,
                     "validate: a missing claimed row is refused");
        p = rows_payload({20, 0, 10}, row_nan);
        check_status(validate_rows_payload(p.data(), p.size(), kH, claims, out.data(), rows), kErrNonFinite,
                     "validate: a non-finite row is refused");
        p = rows_payload({0, 10, 20}, row_val);
        check_status(validate_rows_payload(p.data(), p.size() - 1, kH, claims, out.data(), rows), kErrShort,
                     "validate: a short payload is refused");
        p.resize(p.size() + 4, 0);
        check_status(validate_rows_payload(p.data(), p.size(), kH, claims, out.data(), rows), kErrShort,
                     "validate: a long payload is refused");
        p = rows_payload({0, 10, 20, 30}, row_val);
        check_status(validate_rows_payload(p.data(), p.size(), kH, claims, out.data(), rows), kErrShort,
                     "validate: more rows than claims is refused");
        RowsResp h;
        h.n_rows = 3;
        h.reserved0 = 1;
        p.assign(sizeof(h), 0);
        std::memcpy(p.data(), &h, sizeof(h));
        p.resize(sizeof(h) + 3 * (4 + (size_t) kH * 4), 0);
        check_status(validate_rows_payload(p.data(), p.size(), kH, claims, out.data(), rows), kErrShort,
                     "validate: a nonzero reserved word is refused");
        check(rows == 0, "validate: rows resets to 0 for every refusal");
        bool untouched = true;
        for (float v : out) if (v != -7.0f) untouched = false;
        check(untouched, "validate: every refusal writes NO row (whole-or-nothing)");
    }

    // ---- (6) wire faults: sequence, magic, version, frame size, no outstanding begin ----
    {
        ExpertHelperClient c;
        check_status(handshake(c), kOk, "handshake for the sequence fault");
        check(send_begin(c), "kBegin sent");
        const std::vector<int32_t> claims = {0};
        const auto payload = rows_payload({0}, row_val);
        {
            Socket srv;
            srv.adopt(g_server_fd);
            Header head;
            head.type = kRows;
            head.seq = 99;             // not the begin's sequence
            head.req_id = 3;
            head.len = (uint32_t) payload.size();
            uint8_t raw[kHeaderBytes];
            encode_header(head, raw);
            srv.write_all(raw, sizeof(raw), deadline_after(2000));

            srv.write_all(payload.data(), payload.size(), deadline_after(2000));

            srv.release();
        }
        std::vector<float> out((size_t) kNTok * 10 * kH, 0.0f);
        int32_t rows = -1;
        check_status(c.finish(out.data(), kH, kNTok, 10, claims, 1000, rows), kErrSeq,
                     "a reply with the begin's id but a foreign sequence is refused");
    }
    {
        ExpertHelperClient c;
        check_status(handshake(c), kOk, "handshake for the magic fault");
        check(send_begin(c), "kBegin sent");
        Socket srv;
        srv.adopt(g_server_fd);
        Header head;
        head.magic = 0x53544849u;   // not STHH
        head.version = kProtoVersion;
        head.type = kRows;
        head.seq = 4;
        head.req_id = 4;
        head.len = 0;
        uint8_t raw[kHeaderBytes];
        encode_header(head, raw);
        srv.write_all(raw, sizeof(raw), deadline_after(2000));

        srv.release();
        std::vector<float> out((size_t) kNTok * 10 * kH, 0.0f);
        int32_t rows = -1;
        check_status(c.finish(out.data(), kH, kNTok, 10, {0}, 1000, rows), kErrProtocol,
                     "a foreign magic is a protocol failure");
    }
    {
        ExpertHelperClient c;
        check_status(handshake(c), kOk, "handshake for the version fault");
        check(send_begin(c), "kBegin sent");
        Socket srv;
        srv.adopt(g_server_fd);
        Header head;
        head.type = kRows;
        head.version = kProtoVersion + 1;
        head.seq = 4;
        head.req_id = 4;
        head.len = 0;
        uint8_t raw[kHeaderBytes];
        encode_header(head, raw);
        srv.write_all(raw, sizeof(raw), deadline_after(2000));

        srv.release();
        std::vector<float> out((size_t) kNTok * 10 * kH, 0.0f);
        int32_t rows = -1;
        check_status(c.finish(out.data(), kH, kNTok, 10, {0}, 1000, rows), kErrProtocol,
                     "a foreign protocol version is refused");
    }
    {
        ExpertHelperClient c;
        check_status(handshake(c), kOk, "handshake for the oversized frame");
        check(send_begin(c), "kBegin sent");
        Socket srv;
        srv.adopt(g_server_fd);
        Header head;
        head.type = kRows;
        head.seq = 4;
        head.req_id = 4;
        head.len = (uint32_t) (kMaxFramePayload + 1);   // beyond the reader's bound
        uint8_t raw[kHeaderBytes];
        encode_header(head, raw);
        srv.write_all(raw, sizeof(raw), deadline_after(2000));

        srv.release();
        std::vector<float> out((size_t) kNTok * 10 * kH, 0.0f);
        int32_t rows = -1;
        check_status(c.finish(out.data(), kH, kNTok, 10, {0}, 1000, rows), kErrProtocol,
                     "an oversized frame length is refused before allocating the payload");
    }
    {
        ExpertHelperClient c;
        check_status(handshake(c), kOk, "handshake for the no-begin case");
        std::vector<float> out((size_t) kNTok * 10 * kH, 0.0f);
        int32_t rows = -1;
        check_status(c.finish(out.data(), kH, kNTok, 10, {0}, 100, rows), kErrSeq,
                     "a finish with no outstanding begin is refused");
        check(c.stat_requests() == 0, "no begin was counted");
    }
    {
        // the peer disconnected mid-request: EOF is kErrUnhealthy, never a hang
        ExpertHelperClient c;
        check_status(handshake(c), kOk, "handshake for the disconnect case");
        check(send_begin(c), "kBegin sent");
        {
            Socket srv;
            srv.adopt(g_server_fd);
            srv.close();          // the fake helper dies mid-window
            g_server_fd = -1;
        }
        std::vector<float> out((size_t) kNTok * 10 * kH, 0.0f);
        int32_t rows = -1;
        const auto t0 = std::chrono::steady_clock::now();
        const int rc = c.finish(out.data(), kH, kNTok, 10, {0}, 4000, rows);
        const long long ms = (long long) std::chrono::duration_cast<std::chrono::milliseconds>(
                                 std::chrono::steady_clock::now() - t0).count();
        check_status(rc, kErrUnhealthy, "a mid-window disconnect is kErrUnhealthy");
        check(ms < 1000, "EOF is seen at once, not at the deadline");
    }

    // ---- (7) the helper's socket rules: path, bind mode, peer uid, unlink ----
    {
        std::string why;
        check(socket_path_ok("/tmp/strata-eh-test-sock", why), "an absolute fresh path is accepted");
        check(!socket_path_ok("relative/path", why), "a relative path is refused");
        check(!socket_path_ok("/tmp", why), "an existing path is refused (the helper binds its own)");
        check(!socket_path_ok("/tmp/strata-eh\atrace", why), "a path with control bytes is refused");
        check(!socket_path_ok(std::string("/tmp/") + std::string(200, 'x'), why),
              "a path past sun_path is refused");
        const std::string path = "/tmp/strata-eh-test-sock-" + std::to_string((long) getpid());
        Socket listener;
        check_status(listener.listen_bind(path), kOk, "the helper binds its fresh path");
        struct stat st;
        check(::stat(path.c_str(), &st) == 0 && (st.st_mode & 0777) == 0600,
              "the bound socket is 0600");
        // H7: the engine's CONNECT-side predicate.  The documented flow starts the helper before the
        // engine, so the helper's bound socket file is ALWAYS there when the engine checks it: a
        // bound AF_UNIX socket must pass.  The BIND-side predicate keeps refusing that same existing
        // path (the helper owns a fresh one).  A foreign non-socket is refused by both - and is never
        // unlinked by a refusal.
        std::string cwhy;
        check(socket_path_connect_ok(path, cwhy),
              "H7: the engine's connect check ACCEPTS the helper's bound socket");
        check(!socket_path_ok(path, cwhy),
              "H7: the bind check still refuses the same path (an existing path is never bound over)");
        {
            const std::string foreign = path + ".foreign";
            const std::string link = path + ".link";
            ::unlink(foreign.c_str());
            ::unlink(link.c_str());
            const int ff = ::open(foreign.c_str(), O_CREAT | O_WRONLY | O_CLOEXEC, 0644);
            check(ff >= 0, "H7: a foreign regular file is created at a second path");
            if (ff >= 0) ::close(ff);
            check(::symlink(path.c_str(), link.c_str()) == 0, "H7: a symlink to the bound socket is created");
            check(!socket_path_connect_ok(foreign, cwhy),
                  "H7: the connect check refuses a foreign REGULAR file (lstat: not a socket)");
            check(!socket_path_ok(foreign, cwhy), "H7: the bind check refuses the foreign regular file");
            check(!socket_path_connect_ok(link, cwhy),
                  "H7: the connect check refuses a SYMLINK (a link is not a socket, not followed)");
            check(!socket_path_ok(link, cwhy), "H7: the bind check refuses the symlink");
            struct stat fs;
            check(::stat(foreign.c_str(), &fs) == 0 && S_ISREG(fs.st_mode),
                  "H7: every refusal LEFT the foreign regular file in place (socket checks never unlink)");
            check(::lstat(link.c_str(), &fs) == 0 && S_ISLNK(fs.st_mode),
                  "H7: the symlink is left in place too");
            ::unlink(link.c_str());
            ::unlink(foreign.c_str());
        }
        {
            Socket probe;
            check_status(probe.connect(path, 500), kOk, "the engine connects to the bound path");
            const int fd = listener.accept(deadline_after(2000));
            check(fd >= 0, "the helper accepts one connection");
            check(Socket::peer_uid(fd) == (int) geteuid(), "SO_PEERCRED shows the same uid");
            check(Socket::peer_ok(fd, geteuid()) == kOk, "a same-uid peer is accepted");
            check(Socket::peer_ok(fd, (uid_t) (geteuid() + 12345)) == kErrPeer,
                  "a foreign uid would be refused");
            ::close(fd);
        }
        check_status(listener.listen_bind(path), kErrConnect, "binding an existing path is refused");
        listener.unbind();
        check(::stat(path.c_str(), &st) != 0, "unbind unlinks ONLY the path this socket bound");
        {
            ExpertHelperClient c;
            check_status(c.connect(path, 300), kErrConnect,
                         "an absent helper (no socket at the path) is a named connect failure");
        }
    }

    // ---- (8) the proxy's q8_1 quantizer: golden bytes pinned (R2 gate M2) ----
    // WHAT THIS PIN IS: the exact bytes quantize_q8_1_cpu writes today, frozen from a one-time run of
    // the function itself (rerun with --dump-golden).  A REGRESSION pin of this code, so a change to
    // the quantizer is seen as a change of the payload the helper receives.  It is NOT device
    // bit-parity: amax/d/q are provably identical to quantize_q8_1_kernel's blocks, the fp16 SUM field
    // is ULP-close (the device reduces with the shfl_xor butterfly tree, this loop accumulates in
    // index order; IEEE-754 addition is not associative).  The device parity is measured on hardware
    // by comparing the helper's ROWS with the same seed (docs/MIXED_GPU_HELPER.md, hardware item 4).
    {
        const auto zero = q8_of(gx1, kG1);
        check(zero.size() == 36 && std::memcmp(zero.data(), kGoldenZero, 36) == 0,
              "q8_1 golden: the all-zero block (amax 0, d 0, every q 0)");
        check(zero[0] == 0 && zero[1] == 0 && zero[2] == 0 && zero[3] == 0,
              "q8_1 golden: an all-zero block's ds is two zero halfs");
        const auto cons = q8_of(gx2, kG2);
        check(cons.size() == 36 && std::memcmp(cons.data(), kGoldenConst, 36) == 0,
              "q8_1 golden: the constant block (every value 0.5f)");
        // hand-derivable parts of the constant block: d = 0.5/127, so every q = roundf(0.5/d) = 127,
        // and the sum is 16.0, exactly representable as a half: 0x4C00 (sign 0, exp 19, zero mantissa).
        bool all127 = true;
        for (int i = 0; i < 32; ++i) if (cons[4 + i] != 0x7F) all127 = false;
        check(all127, "q8_1 golden: a constant block quantizes to 127 (q = roundf(x/(x/127)))");
        check(cons[2] == 0x00 && cons[3] == 0x4C, "q8_1 golden: the sum field is the half 16.0 (0x4C00)");
        const auto mixed = q8_of(gx3, kG3);
        check(mixed.size() == 72 && std::memcmp(mixed.data(), kGoldenMixed, 72) == 0,
              "q8_1 golden: the mixed block, 2 blocks pinned byte for byte");
        // the mixed block's hand-checkable structure: x[0] = 1e30 makes amax/127 overflow the half, so
        // ds[0] is the #606 clamp 65504 = 0x7BFF, and its own q is small; the second block's 3000/-3000
        // saturate q at +127/-127, and a -0.0 quantizes to 0.
        check(mixed[0] == 0xFF && mixed[1] == 0x7B, "q8_1 golden: an over-wide scale clamps to 65504 (0x7BFF)");
        // the SECOND block starts at byte 36: ds at 36..39, its 32 q bytes at 40..71
        check(mixed[36] == 0xE8 && mixed[37] == 0x4D && mixed[38] == 0xC0 && mixed[39] == 0x4C,
              "q8_1 golden: block 2's ds is the halfs 23.625 (0x4DE8) and 19.0 (0x4CC0)");
        check(mixed[40 + 2] == 0x7F && mixed[40 + 3] == 0x81,
              "q8_1 golden: q saturates at +127 (0x7F) / -127 (0x81)");
        check(mixed[40 + 0] == 0x00, "q8_1 golden: -0.0 quantizes to 0");
        // a tail the block loop must leave alone: 40 values = 1 block, the 8 tail values write nothing
        std::vector<uint8_t> tail((size_t) (40 / 32) * 36u, 0xCD);
        quantize_q8_1_cpu(gx3, 40, tail.data());
        check(tail.size() == 36, "q8_1: 40 values are one 32-value block (a tail of whole floats is left)");
        check(std::memcmp(tail.data(), mixed.data(), 36) == 0,
              "q8_1: the same first 32 values give the same 36 bytes with a tail");
    }

    // ---- (9) H2: a valid frame after MORE silence than the 2 s poll cap, inside a 5 s deadline ----
    // poll_once caps ONE ::poll at 2000 ms; the cap is not the deadline.  Pre-fix the cap expiring was
    // answered as "the deadline expired", so a helper that computed for 2.25 s lost its reply at 2 s
    // (section (3)'s 150 ms stall case is under the cap and cannot see this).  The pin: one read_frame
    // with a 5000 ms deadline whose frame is written by a JOINED thread after 2250 ms of silence.
    {
        int fds[2];
        check(::socketpair(AF_UNIX, SOCK_STREAM, 0, fds) == 0, "H2: socketpair for the 2.25 s silence case");
        std::atomic<int> werr{kErrInternal};
        std::thread writer([fd = fds[1], &werr] {
            std::this_thread::sleep_for(std::chrono::milliseconds(2250));   // past the 2000 ms poll cap
            HelloAck ack;
            ack.max_tok = (int32_t) kMaxTok;
            ack.k = (int32_t) kK;
            ack.max_entries = (int32_t) kMaxEntries;
            ack.status = 0;
            ack.free_mib = 2048;
            Socket s;
            s.adopt(fd);
            werr = s.write_frame(kHelloAck, 1, (const uint8_t*) &ack, sizeof(ack), deadline_after(2000));
            s.close();      // the writer's join is the ownership boundary: this thread closes fds[1]
        });
        Socket cli;
        cli.adopt(fds[0]);                     // the reader owns fds[0] for the whole exchange
        const auto t0 = std::chrono::steady_clock::now();
        Header head;
        RxBuffer rx;
        PayloadView body;
        const int rc = cli.read_frame(head, rx, body, deadline_after(5000));
        const long long ms = (long long) std::chrono::duration_cast<std::chrono::milliseconds>(
                                 std::chrono::steady_clock::now() - t0).count();
        writer.join();                         // nothing can still be in flight when the reader closes
        cli.close();
        check(rc == kOk && head.type == kHelloAck && body.size() == sizeof(HelloAck),
              "H2: the frame after 2.25 s of silence is read kOk inside the 5 s deadline (not kErrTimeout)");
        check(ms >= 2000 && ms < 4800, "H2: the read waited past the 2 s poll cap, about the arrival, not the deadline");
        check(werr == kOk, "H2: the joined sender wrote its frame");
    }

    // ---- (10) H8: the kHello claimed bitmap's unpack, run exactly as the CUDA helper runs it ----
    // The engine (hello, expert_helper_socket.cpp) packs bit-per-pair; serve_fill indexes byte-per-pair;
    // unpack_claimed_bitmap (helper_frame.cpp, the helper's own expression) bridges them.  Pre-fix the
    // raw 3072 bytes were stored as-is, so every pair from 3072 on - the helper's whole range, flat
    // >= 16384 - read past it as UNCLAIMED and the fill took work it was told to skip.
    // 48 layers x 512 experts = 24576 pairs = (24576+7)/8 = 3072 packed bytes, the real geometry.
    {
        const size_t n = (size_t) kLayers * (size_t) kExperts;
        const size_t nbytes = (n + 7) / 8;
        check(n == 24576 && nbytes == 3072, "H8: 48x512 pairs pack to exactly 3072 bytes");
        std::vector<uint8_t> claimed(n, 0);
        for (const size_t i : kClaimFlat) claimed[i] = 1;
        // the wire path: the engine's own hello() sends the bitmap, the fake helper decodes the frame
        // and runs the shared unpack on the payload - the exact bytes the CUDA helper sees.
        int fds[2];
        check(::socketpair(AF_UNIX, SOCK_STREAM, 0, fds) == 0, "H8: socketpair for the kHello bitmap");
        std::atomic<int> seen{kErrInternal};
        std::thread srv([fd = fds[1], &seen] {
            Socket s;
            s.adopt(fd);
            Header head;
            RxBuffer rx;
            PayloadView body;
            if (s.read_frame(head, rx, body, deadline_after(5000)) != kOk || head.type != kHello ||
                body.size() < sizeof(HelloReq)) { seen = kErrProtocol; return; }
            HelloReq req;
            std::memcpy(&req, body.data(), sizeof(req));
            std::vector<uint8_t> flat;
            unpack_claimed_bitmap(body.data() + sizeof(req), (size_t) req.claimed_bytes,
                                  (size_t) req.n_layers * (size_t) req.n_expert, flat);
            bool exact = req.claimed_bytes == (uint32_t) ((kLayers * kExperts + 7) / 8) &&
                         flat.size() == (size_t) kLayers * (size_t) kExperts;
            for (size_t i = 0; i < flat.size() && exact; ++i)
                if (flat[i] != (kClaimAt(i) ? 1 : 0)) exact = false;
            seen = exact ? kOk : kErrShort;
            HelloAck ack;
            ack.max_tok = (int32_t) kMaxTok;
            ack.k = (int32_t) kK;
            ack.max_entries = (int32_t) kMaxEntries;
            ack.status = 0;
            ack.free_mib = 1024;
            s.write_reply(kHelloAck, head.req_id, head.seq, (const uint8_t*) &ack, sizeof(ack), deadline_after(5000));
            s.close();   // the reply is queued; this thread closes fds[1] (EOF follows the reply)
        });
        ExpertHelperClient c;
        check_status(c.attach_socket(fds[0], true), kOk, "H8: client attached for the kHello bitmap");
        int64_t free_mib = 0;
        check_status(c.hello(kLayers, kExperts, kH, kFF, 0, 47, 1024, 512, claimed, 5000, free_mib), kOk,
                     "H8: the engine's kHello carries the packed claimed bitmap");
        srv.join();
        c.close();   // attach_socket(owned) makes the client the owner: it closes fds[0] here
        check_status(seen, kOk, "H8: the helper-side unpack lands pairs 0,7,8,16383,16384,24575 and nothing else");
        // the unpack's own semantics, with no socket in sight
        std::vector<uint8_t> out;
        unpack_claimed_bitmap(nullptr, 0, 16, out);
        bool all0 = out.size() == 16;
        for (uint8_t v : out) if (v != 0) all0 = false;
        check(all0, "H8: an empty bitmap claims nothing (no byte is read past the caller's bytes)");
        std::vector<uint8_t> two(2, 0);
        two[0] = 0x80;                       // bit 7 of byte 0 = pair 7 claimed, nothing else
        unpack_claimed_bitmap(two.data(), 2, 3000, out);
        check(out.size() == 3000 && out[7] == 1 && out[0] == 0 && out[6] == 0 && out[8] == 0 &&
                  out[15] == 0 && out[16] == 0 && out[2999] == 0,
              "H8: 2 packed bytes cover pairs 0..15 only (bit 7 of byte 0); 16..2999 read as unclaimed");
        // a full packed bitmap: the byte/bit boundaries the helper's range sits on
        std::vector<uint8_t> packed(nbytes, 0);
        for (const size_t i : kClaimFlat) packed[i / 8] |= (uint8_t) (1u << (i % 8));
        unpack_claimed_bitmap(packed.data(), nbytes, n, out);
        check(out.size() == n && out[7] == 1 && out[8] == 1,
              "H8: the bit 7 / bit 0 byte boundary (pair 7 in byte 0, pair 8 in byte 1)");
        check(out.size() == n && out[16383] == 1 && out[16384] == 1,
              "H8: pair 16384 is byte 2048 bit 0 - inside the 3072 packed bytes, read as claimed");
        check(out.size() == n && out[24575] == 1, "H8: the last pair 24575 unpacks from the last byte's bit 7");
        check(out.size() == n && out[16382] == 0 && out[16385] == 0 && out[24574] == 0,
              "H8: the neighbours of the pinned bits stay unclaimed");
    }

    // ---- (11) H15: the shared sendmsg(MSG_NOSIGNAL) transport under a SIGPIPE that is NOT ignored ----
    // The harness's own SIG_IGN (top of main) MASKS this regression: with SIGPIPE ignored, a send with
    // no MSG_NOSIGNAL to a dead peer is not fatal - the signal is discarded, the write returns EPIPE and
    // the transport answers the ordinary kErrUnhealthy, so a suite passes WITH the bug.  Under SIG_DFL
    // the same write kills the process with signal 13.  These children restore SIGPIPE to SIG_DFL and
    // write through a fixture whose poll reports POLLOUT with NO POLLHUP (SHUT_WR on the writer, peer
    // left open), so the write syscall is reached for real - see h15_fixture's measured probe.  The
    // pre-fix writev copy is the negative control: it must die on the same fixture, which is what makes
    // the fixed child's normal exit evidence.
    {
        const auto prev = std::signal(SIGPIPE, SIG_IGN);   // same disposition: records what we run with
        check(prev == SIG_IGN, "the harness runs with SIGPIPE ignored (the children below restore SIG_DFL)");
        const ChildOutcome fixed = run_h15_child(0);
        check(!fixed.signaled && fixed.code == 0,
              "H15: under SIGPIPE DEFAULT the transport's write to a shut peer is kErrUnhealthy, child exits normally");
        if (fixed.signaled || fixed.code != 0)
            std::printf("      H15 child: signaled=%d sig=%d exit=%d (-1 = fork failed, -2 = waitpid failed,"
                        " 11-14 = fixture step, 20-rc = the status the transport returned)\n",
                        fixed.signaled, fixed.signal, fixed.code);
        check(!fixed.signaled, "H15: no SIGPIPE (13) from the sendmsg(MSG_NOSIGNAL) transport");
        const ChildOutcome old = run_h15_child(1);
        check(old.signaled && old.signal == SIGPIPE,
              "H15 negative control: the PRE-FIX writev path dies with SIGPIPE (13) on the SAME fixture");
        if (!(old.signaled && old.signal == SIGPIPE))
            std::printf("      H15 control child: signaled=%d sig=%d exit=%d (-1 = fork failed, -2 = waitpid"
                        " failed, 15 = writev returned: the fixture is too weak)\n",
                        old.signaled, old.signal, old.code);
    }

    // ================= R5 P1: the selected-token PROMPT tier (kBeginP / kRowsP) =================
    // Sections (12)..(18) below run the REAL client (src/core/expert_helper_socket.cpp) over a
    // socketpair against a fake helper, with no GPU and no device runtime linked: the v3 round-trip
    // and its exact framing, the caps with hard refusals, the OLD-PEER negotiation (typed refusal,
    // no misparse, no hang), the reply-validation faults, the tier's OWN pinned buffer, the selection
    // rule's arithmetic, and the SIGTERM begin-wake.  Exit 0 = all of it passed.

    // ---- (12) the v3 round-trip: a 40-row layer, rows scattered at their PAIR indices ----
    {
        ExpertHelperClient c;
        check_status(handshake_v3(c, true), kOk, "v3 handshake: the ack advertises kFeaturePromptRows");
        check(c.prompt_rows_supported(), "the client records the advertised tier");
        ExpertHelperPromptPlan plan;
        for (int i = 0; i < 40; ++i) { plan.pair_token.push_back(i); plan.pair_expert.push_back(7 + (i % 3)); }
        for (int i = 0; i < 40; ++i) plan.tokens.push_back(i);
        fill_prompt_buffer(c, 40);                       // the tier's own rows, deterministic
        std::vector<int32_t> primary((size_t) kLayers * kExperts, -1);
        primary[(size_t) kLayer * kExperts + 5] = 0;    // primary-resident: the helper claims none of those
        check_status(c.begin_prompt(kLayer, plan, primary.data(), 4000), kOk, "kBeginP is sent");
        check(wire_prompt_ok(plan, primary.data(), 40),
              "the frame carries the pair list, the primary bitmap and the q8 image with exact lengths");
        // the claims are the PAIR indices the engine asked for; the reply answers exactly them, in a
        // shuffled order.  A reply that leaves a claim unanswered is kErrMissing (section (15) pins
        // that), so the claims here are the 4 answered pairs, not all 40 rows of the out array.
        const std::vector<int32_t> claims = {0, 3, 4, 17};
        std::vector<float> out((size_t) 40 * kH, -1.0f);
        int32_t rows = -1;
        check_status(write_prompt_reply_thread({3, 0, 4, 17}, row_val, 4000, c, out.data(), claims, rows),
                     kOk, "a valid kRowsP reply is accepted");
        check(rows == 4, "the reply reports its 4 rows");
        bool placed = true;
        for (const int32_t i : {3, 0, 4, 17})
            for (int64_t col = 0; col < kH; ++col)
                if (out[(size_t) i * kH + (size_t) col] != (float) row_val(i, col)) placed = false;
        check(placed, "rows land at their PAIR indices, out of frame order");
        check(out[(size_t) 30 * kH] == -1.0f, "an unclaimed pair is untouched");
        check(c.stat_prompt_requests() == 1 && c.stat_prompt_rows() == 4 &&
                  c.stat_prompt_bytes() == 4ull * kH * 4ull,
              "the prompt tier's counters count rows SEPARATELY from the decode windows");
        check(c.stat_rows() == 0, "a prompt row does not enter the decode window's row counter");
    }

    // ---- (12b) the design's CAP shape end to end: 256 rows, 256 selected tokens (737 KB request,
    // 2.62 MB reply) - the sizes the design names, proven on the transport with no GPU in sight ----
    {
        ExpertHelperClient c;
        check_status(handshake_v3(c, true), kOk, "v3 handshake for the cap-shape exchange");
        ExpertHelperPromptPlan plan;
        for (int i = 0; i < (int) kMaxSelTok; ++i) {
            plan.pair_token.push_back(i);
            plan.pair_expert.push_back(7 + (i % 64));
        }
        for (int i = 0; i < (int) kMaxSelTok; ++i) plan.tokens.push_back(i);
        fill_prompt_buffer(c, kMaxSelTok);
        std::vector<int32_t> primary((size_t) kLayers * kExperts, -1);
        check_status(begin_prompt_drained(c, plan, primary.data(), 8000), kOk,
                     "a 256-pair / 256-token kBeginP is sent (737 KB, inside the 8 MiB frame cap)");
        check(g_req_rc == kOk && g_req_head.type == kBeginP && g_req_head.len == g_req_body.size(),
              "the peer drained the whole 737 KB request (one frame, no fragmentation)");
        check(g_req_body.size() == sizeof(BeginPReq) + (size_t) kMaxRowsPerLayer * 8 +
                  (size_t) ((kMaxSelTok * kExperts + 7) / 8) + (size_t) kMaxSelTok * (kH / 32) * 36,
              "the cap-shape frame carries its exact byte total (755,192 B)");
        const std::vector<int32_t> claims = make_range((int) kMaxRowsPerLayer);
        std::vector<float> out((size_t) kMaxRowsPerLayer * kH, -1.0f);
        int32_t rows = -1;
        check_status(write_prompt_reply_thread(claims, row_val, 15000, c, out.data(), claims, rows), kOk,
                     "the 256-row reply (2.62 MB, one frame) is accepted");
        check(rows == (int32_t) kMaxRowsPerLayer, "every claimed row is answered");
        bool placed = true;
        for (int32_t i : claims)
            if (out[(size_t) i * kH + 7] != (float) row_val(i, 7)) placed = false;
        check(placed, "all 256 rows land at their pair indices");
        check(c.stat_prompt_rows() == (uint64_t) kMaxRowsPerLayer, "the row counter matches the claim count");
    }

    // ---- (12c) M4c (b): the FP32 image - a peer that advertises kFeaturePromptF32 gets the tier's
    // rows VERBATIM (image_format 1, one contiguous float image, no engine-side quantize), while the
    // q8 contract of (12) stays exactly what a bit-0-only peer sees.  The helper-side twin quantizer
    // is bit-identical by construction, so this pins the whole fp32 wire contract with no GPU. ----
    {
        ExpertHelperClient c;
        check_status(handshake_v3(c, true, kFeaturePromptF32), kOk,
                     "v3 handshake: the ack advertises kFeaturePromptF32");
        check(c.prompt_f32_supported(), "the client latches the fp32 feature");
        check(c.prompt_rows_supported(), "the fp32 bit implies the tier itself");
        ExpertHelperPromptPlan plan;
        for (int i = 0; i < 40; ++i) { plan.pair_token.push_back(i); plan.pair_expert.push_back(7 + (i % 3)); }
        for (int i = 0; i < 40; ++i) plan.tokens.push_back(i);
        fill_prompt_buffer(c, 40);
        std::vector<int32_t> primary((size_t) kLayers * kExperts, -1);
        check_status(begin_prompt_drained(c, plan, primary.data(), 8000), kOk,
                     "the fp32 kBeginP is sent (drained: 40 * kH * 4 = 400 KB passes the socket buffer)");
        check(g_req_rc == kOk && g_req_head.type == kBeginP && g_req_head.len == g_req_body.size(),
              "the drained reader captured the whole fp32 frame (one frame, no fragmentation)");
        check(prompt_body_ok(g_req_body, plan, primary.data(), 40, true),
              "the frame carries the pairs, the bitmap and the FP32 image verbatim (40 * kH * 4 bytes)");
        const std::vector<int32_t> claims = {1, 5, 9};
        std::vector<float> out((size_t) 40 * kH, -1.0f);
        int32_t rows = -1;
        check_status(write_prompt_reply_thread({9, 1, 5}, row_val, 4000, c, out.data(), claims, rows),
                     kOk, "a kRowsP reply on the fp32 path is accepted (the reply is format-independent)");
        check(rows == 3, "the reply reports its 3 rows");
    }

    // ---- (13) the NEGOTIATION: an R2 peer (the 24-byte legacy ack) is refused LOCALLY, by name,
    // and the new frame type NEVER reaches the wire - so there is nothing to misparse and nothing
    // to wait on.  This is the accepted design's old-peer refusal. ----
    {
        ExpertHelperClient c;
        check_status(handshake_v3(c, false), kOk,
                     "an R2-shaped ack (kHelloAckLegacyBytes, no features field) is accepted by the new client");
        check(!c.prompt_rows_supported(), "no feature bit in the ack: the tier is OFF");
        ExpertHelperPromptPlan plan;
        plan.pair_token.push_back(0);
        plan.pair_expert.push_back(7);
        plan.tokens.push_back(0);
        fill_prompt_buffer(c, 1);
        const int rc = c.begin_prompt(kLayer, plan, nullptr, 2000);
        check_status(rc, kErrRequest, "kBeginP to a peer that never advertised it: typed refusal");
        check_status(c.stat_prompt_requests(), (uint64_t) 0, "the refused request is not counted (nothing was sent)");
        check(nothing_on_wire(200), "and the fake peer received NOTHING: the refusal is local, not a race with a misparse");
    }
    {
        // The same, with the frames forced onto the wire BY THE TEST: an R2 helper that meets an
        // unknown frame type answers its typed protocol status and closes; the new client reports it
        // as a named status in bounded time (no hang).  This is the mixed-build fallback, not the
        // normal path: the client above never sends kBeginP unadvertised.
        ExpertHelperClient c;
        check_status(handshake_v3(c, true), kOk, "v3 handshake for the forced-wire refusal test");
        ExpertHelperPromptPlan plan;
        plan.pair_token.push_back(0);
        plan.pair_expert.push_back(7);
        plan.tokens.push_back(0);
        fill_prompt_buffer(c, 1);
        check_status(c.begin_prompt(kLayer, plan, nullptr, 2000), kOk, "kBeginP is on the wire");
        {
            Socket srv;
            srv.adopt(g_server_fd);
            Header head;
            RxBuffer rx;
            PayloadView body;
            check_status(srv.read_frame(head, rx, body, deadline_after(2000)), kOk, "the fake peer reads the kBeginP frame");
            check(head.type == kBeginP, "the frame type on the wire is kBeginP");
            // An R2 helper's own answer for a frame type it does not know (helper_main.cpp: typed
            // kErrProtocol, then the channel closes).
            ErrResp e;
            e.status = kErrProtocol;
            std::snprintf(e.text, sizeof(e.text), "frame type %u is not part of this protocol", head.type);
            srv.write_reply(kError, head.req_id, head.seq, (const uint8_t*) &e, sizeof(e), deadline_after(2000));
            srv.release();
        }
        std::vector<float> out((size_t) kH, 0.0f);
        int32_t rows = -1;
        const auto t0 = std::chrono::steady_clock::now();
        const int rc = c.finish_prompt(out.data(), kH, {0}, 4000, rows);
        const long long ms = (long long) std::chrono::duration_cast<std::chrono::milliseconds>(
                                 std::chrono::steady_clock::now() - t0).count();
        check_status(rc, kErrRequest, "the peer's typed refusal arrives as kErrRequest (a named status)");
        check(ms < 2000, "and in bounded time: the refusal is the reply, not a deadline (no hang)");
        check(rows == 0, "no row is applied");
    }

    // ---- (14) the caps are HARD: every breach is refused before a byte is written ----
    {
        auto caps = [&](int64_t rows, int64_t n_sel, int64_t per_expert_rows, const char* what) {
            ExpertHelperClient c;
            if (handshake_v3(c, true) != kOk) { check(false, "handshake for a cap case"); return; }
            ExpertHelperPromptPlan plan;
            for (int64_t i = 0; i < rows; ++i) {
                plan.pair_token.push_back((int32_t) (i % n_sel));
                plan.pair_expert.push_back(per_expert_rows > 0 ? 7 : (int32_t) (7 + (i % 64)));
            }
            for (int64_t i = 0; i < n_sel; ++i) plan.tokens.push_back((int32_t) i);
            fill_prompt_buffer(c, n_sel);
            check_status(c.begin_prompt(kLayer, plan, nullptr, 2000), kErrGeometry, what);
            check(nothing_on_wire(200), "and nothing was sent (a cap refusal is a refusal, not a truncated request)");
            c.close();
        };
        // M4b: the fixtures derive from the constants (the caps rose 256/256/64 -> 8192/8192/256);
        // every breach is still refused before a byte is written, and each +1 IS the boundary, so
        // the caps are inclusive by construction.
        const int64_t cap_l = kMaxRowsPerLayer, cap_s = kMaxSelTok, cap_e = kMaxRowsPerExpert;
        caps(cap_l + 1, cap_s, 0, "rows beyond kMaxRowsPerLayer are refused");
        caps(8, cap_s + 1, 1, "selected tokens beyond kMaxSelTok are refused");
        // cap_e + 1 pairs on ONE expert: past kMaxRowsPerExpert
        {
            ExpertHelperClient c;
            if (handshake_v3(c, true) == kOk) {
                ExpertHelperPromptPlan plan;
                for (int64_t i = 0; i < cap_e + 1; ++i) { plan.pair_token.push_back((int32_t) i); plan.pair_expert.push_back(9); }
                for (int64_t i = 0; i < cap_e + 1; ++i) plan.tokens.push_back((int32_t) i);
                fill_prompt_buffer(c, (int) (cap_e + 1));
                check_status(c.begin_prompt(kLayer, plan, nullptr, 2000), kErrGeometry,
                             "more than kMaxRowsPerExpert pairs on one expert are refused");
                check(nothing_on_wire(200), "and nothing was sent");
            }
        }
        {   // the layer window: a request outside the hello window is refused before the send
            ExpertHelperClient c;
            check_status(handshake_v3(c, true), kOk, "handshake for the layer-window cap");
            ExpertHelperPromptPlan plan;
            plan.pair_token.push_back(0);
            plan.pair_expert.push_back(7);
            plan.tokens.push_back(0);
            fill_prompt_buffer(c, 1);
            check_status(c.begin_prompt(4, plan, nullptr, 2000), kErrGeometry,
                         "a layer outside the hello window (the tier serves 32..47 there) is refused");
            check(nothing_on_wire(200), "and nothing was sent");
        }
        {   // no activation buffer: the tier cannot pack a request it has no rows for
            ExpertHelperClient c;
            check_status(handshake_v3(c, true), kOk, "handshake for the no-buffer case");
            ExpertHelperPromptPlan plan;
            plan.pair_token.push_back(0);
            plan.pair_expert.push_back(7);
            plan.tokens.push_back(0);
            check_status(c.begin_prompt(kLayer, plan, nullptr, 2000), kErrGeometry,
                         "with no pinned activation buffer the request is refused (the tier owns its rows)");
            check(nothing_on_wire(200), "and nothing was sent");
        }
        {   // a pair whose token is not in the plan: no activation row to send
            ExpertHelperClient c;
            check_status(handshake_v3(c, true), kOk, "handshake for the dangling-token case");
            ExpertHelperPromptPlan plan;
            plan.pair_token.push_back(3);        // not in tokens
            plan.pair_expert.push_back(7);
            plan.tokens.push_back(0);
            plan.tokens.push_back(1);
            fill_prompt_buffer(c, 2);
            check_status(c.begin_prompt(kLayer, plan, nullptr, 2000), kErrGeometry,
                         "a pair whose token has no image row is refused");
        }
    }

    // ---- (15) the kRowsP reply's validation faults (the same whole-or-nothing contract as kRows) ----
    {
        auto reply = [&](const std::vector<int32_t>& idx, const std::vector<int32_t>& claims,
                         double (*v)(int, int64_t), int want, const char* what) {
            ExpertHelperClient c;
            if (handshake_v3(c, true) != kOk) { check(false, "handshake for a reply fault"); return; }
            ExpertHelperPromptPlan plan;
            for (int i = 0; i < 8; ++i) { plan.pair_token.push_back(i); plan.pair_expert.push_back(7); }
            for (int i = 0; i < 8; ++i) plan.tokens.push_back(i);
            fill_prompt_buffer(c, 8);
            if (c.begin_prompt(kLayer, plan, nullptr, 2000) != kOk) { check(false, "kBeginP for a reply fault"); return; }
            std::vector<float> out((size_t) 8 * kH, -9.0f);
            int32_t rows = -1;
            check_status(write_prompt_reply_thread(idx, v, 4000, c, out.data(), claims, rows), want, what);
            check(rows == 0 || rows == (int32_t) idx.size(), "the row count is reported only for an accepted reply");
            if (want != kOk) {
                bool untouched = true;
                for (float f : out) if (f != -9.0f) untouched = false;
                check(untouched, "the refused reply applies NOTHING (whole-or-nothing)");
            }
        };
        reply({0, 1, 2}, {0, 1, 2}, row_val, kOk, "validate: the valid prompt reply passes");
        reply({0, 1, 9}, {0, 1, 2}, row_val, kErrBadId, "validate: an index outside the claims is refused");
        reply({0, 0, 2}, {0, 1, 2}, row_val, kErrBadId, "validate: a duplicated pair index is refused");
        reply({0, 1}, {0, 1, 2}, row_val, kErrMissing, "validate: a missing claimed row is refused");
        reply({0, 1, 2}, {0, 1, 2}, row_nan, kErrNonFinite, "validate: a non-finite row is refused");
        reply({0, 1, 2, 3}, {0, 1, 2}, row_val, kErrShort, "validate: more rows than claims is refused");
    }
    {   // the reply's identity: a foreign sequence with the right request id
        ExpertHelperClient c;
        check_status(handshake_v3(c, true), kOk, "handshake for the prompt seq fault");
        ExpertHelperPromptPlan plan;
        plan.pair_token.push_back(0);
        plan.pair_expert.push_back(7);
        plan.tokens.push_back(0);
        fill_prompt_buffer(c, 1);
        check_status(c.begin_prompt(kLayer, plan, nullptr, 2000), kOk, "kBeginP sent");
        const auto payload = rowsp_payload({0}, row_val);
        Socket srv;
        srv.adopt(g_server_fd);
        Header head;
        head.type = kRowsP;
        head.seq = 77;                      // not the request's echoed sequence
        head.req_id = 3;
        head.len = (uint32_t) payload.size();
        uint8_t raw[kHeaderBytes];
        encode_header(head, raw);
        srv.write_all(raw, sizeof(raw), deadline_after(2000));
        srv.write_all(payload.data(), payload.size(), deadline_after(2000));
        srv.release();
        std::vector<float> out((size_t) kH, 0.0f);
        int32_t rows = -1;
        check_status(c.finish_prompt(out.data(), kH, {0}, 2000, rows), kErrSeq,
                     "a prompt reply with the request id and a foreign sequence is refused");
    }
    {   // a stalled helper: one bounded deadline, the counter, and no retry
        ExpertHelperClient c;
        check_status(handshake_v3(c, true), kOk, "handshake for the prompt stall");
        ExpertHelperPromptPlan plan;
        plan.pair_token.push_back(0);
        plan.pair_expert.push_back(7);
        plan.tokens.push_back(0);
        fill_prompt_buffer(c, 1);
        check_status(c.begin_prompt(kLayer, plan, nullptr, 2000), kOk, "kBeginP sent");
        std::vector<float> out((size_t) kH, 0.0f);
        int32_t rows = -1;
        const auto t0 = std::chrono::steady_clock::now();
        const int rc = c.finish_prompt(out.data(), kH, {0}, 150, rows);
        const long long ms = (long long) std::chrono::duration_cast<std::chrono::milliseconds>(
                                 std::chrono::steady_clock::now() - t0).count();
        check_status(rc, kErrTimeout, "a stalled prompt tier costs the named timeout");
        check(ms >= 140 && ms < 3000, "the bounded poll waits about the deadline, not unbounded");
        check(c.stat_timeouts() == 1, "the timeout is counted for the DONE line");
        check_status(c.finish_prompt(out.data(), kH, {0}, 100, rows), kErrUnhealthy,
                     "after a timeout the channel is closed, not retried (the shared latch's rule)");
    }

    // ---- (16) the tier's OWN pinned activation buffer (never cpu_x) ----
    {
        ExpertHelperClient c;
        check(c.prompt_buffer() == nullptr, "no buffer before the tier asks for one");
        check_status(c.prompt_buffer_ensure(kH), kOk, "the buffer is allocated (pinned: mmap + mlock)");
        const size_t cap = (size_t) kMaxSelTok * (size_t) kH * sizeof(float);   // 256*2560*4 = 2.5 MB
        check(c.prompt_buffer_bytes() == cap, "capacity is kMaxSelTok * n_embd * 4 - the design's 2.6 MB, bounded");
        check(c.prompt_buffer_embd() == kH, "the geometry is recorded");
        check(c.prompt_buffer_owns(c.prompt_buffer()), "the buffer's first row is the tier's own");
        check(c.prompt_buffer_owns(c.prompt_buffer() + (size_t) 255 * kH),
              "the LAST row the caps allow is inside the tier's mapping");
        check(!c.prompt_buffer_owns(c.prompt_buffer() + cap / sizeof(float)),
              "a pointer past the mapping is NOT the tier's (ownership is a range, not a name)");
        float foreign = 1.0f;
        check(!c.prompt_buffer_owns(&foreign), "a caller's stack row is NOT the tier's (never an alias of cpu_x)");
        check_status(c.prompt_buffer_ensure(kH), kOk, "ensure is idempotent (grown ONCE)");
        check(c.prompt_buffer_bytes() == cap, "the mapping did not grow");
        check_status(c.prompt_buffer_ensure(2048), kErrGeometry,
                     "a second geometry on a live tier is refused (no silent reallocation)");
        check_status(c.prompt_buffer_ensure(0), kErrGeometry, "a zero-width geometry is refused");
        c.release_prompt_buffer();
        check(c.prompt_buffer() == nullptr && c.prompt_buffer_bytes() == 0,
              "the tier's buffer has the tier's lifetime (released with it)");
    }

    // ---- (17) the selection rule's arithmetic, with no socket in sight ----
    {
        auto sel_run = [&](int64_t n_expert, const std::vector<int32_t>& cnt, const std::vector<int32_t>& toks,
                           const std::vector<int32_t>& res, const std::vector<uint8_t>& holds,
                           const std::vector<uint8_t>& stable, int64_t k, int64_t per_e, int64_t per_l,
                           int64_t max_tok, ExpertHelperPromptPlan& plan) {
            return select_prompt_rows(cnt.data(), toks.data(), res.data(), kLayer, holds.data(), stable.data(),
                                      n_expert, k, per_e, per_l, max_tok, plan);
    };
    {   // the pack's geometry: 512 experts, counts built to the caps, all eligible
        const int64_t NE = kExperts;
        // host_res is the FULL (layers x experts) residency array - the shape the contract indexes
        // (l*n_expert + e), sized like the engine's own, so no read can land past it.
        std::vector<int32_t> cnt((size_t) NE, 0), res((size_t) kLayers * kExperts, -1);
        std::vector<uint8_t> holds((size_t) NE, 1), stable((size_t) NE, 1);
        std::vector<int32_t> toks;
        // 4 experts x 64 tokens (the per-expert cap exactly), on 256 distinct tokens: the full 256 rows
        int tok = 0;
        for (int e = 1; e <= 4; ++e) {
            cnt[(size_t) e] = 64;
            for (int i = 0; i < 64; ++i) toks.push_back(tok++);
        }
        ExpertHelperPromptPlan plan;
        check_status(sel_run(NE, cnt, toks, res, holds, stable, 10, kMaxRowsPerExpert, kMaxRowsPerLayer,
                             kMaxSelTok, plan), kOk, "selection: the design's full 256-row claim set is built");
        check(plan.rows() == 256 && plan.n_sel_tok() == 256, "256 rows on 256 distinct tokens");
        bool asc = true;
        for (size_t i = 1; i < plan.tokens.size(); ++i)
            if (plan.tokens[i] <= plan.tokens[i - 1]) asc = false;
        check(asc, "the plan's tokens are ASCENDING (the q8 image's row order)");
        // determinism: the same input twice, the same plan; and the order is count DESC, expert ASC
        ExpertHelperPromptPlan plan2;
        sel_run(NE, cnt, toks, res, holds, stable, 10, kMaxRowsPerExpert, kMaxRowsPerLayer, kMaxSelTok, plan2);
        check(plan.pair_token == plan2.pair_token && plan.pair_expert == plan2.pair_expert &&
                  plan.tokens == plan2.tokens, "the same input yields the SAME plan (a run is reproducible)");
        std::vector<int64_t> seen_experts;
        for (size_t i = 0; i < plan.pair_expert.size(); ++i)
            if (i == 0 || plan.pair_expert[i] != plan.pair_expert[i - 1]) seen_experts.push_back(plan.pair_expert[i]);
        check(seen_experts.size() == 4 && seen_experts[0] == 1 && seen_experts[3] == 4,
              "experts appear in the deterministic order (equal counts: expert ascending), whole");
        // the eligibility screen: every exclusion the design names.  A FIFTH expert (64 tokens, its
        // list appended in expert order) is the survivor: only it may enter the tier.
        std::vector<int32_t> cntS((size_t) NE, 0), toksS, resS = res;
        std::vector<uint8_t> hS((size_t) NE, 1), sS((size_t) NE, 1);
        const int per[5] = {65, 64, 64, 64, 64};   // 1: over the cap, 2..4: the screens, 5: eligible
        for (int e = 1; e <= 5; ++e) {
            cntS[(size_t) e] = per[e - 1];
            for (int i = 0; i < per[e - 1]; ++i) toksS.push_back(e * 100 + i);
        }
        resS[(size_t) kLayer * kExperts + 2] = 5;   // (layer,2) resident on the PRIMARY
        hS[3] = 0;
        sS[4] = 0;
        ExpertHelperPromptPlan plan3;
        sel_run(NE, cntS, toksS, resS, hS, sS, 10, 64, 256, 256, plan3);   // M4b: fixture caps (65 > 64), independent of the protocol's
        std::set<int32_t> got(plan3.pair_expert.begin(), plan3.pair_expert.end());
        check(got.find(1) == got.end(), "an expert over kMaxRowsPerExpert never enters the tier");
        check(got.find(2) == got.end(), "a PRIMARY-resident expert never enters the tier");
        check(got.find(3) == got.end(), "an expert the proxy does not serve never enters the tier");
        check(got.find(4) == got.end(), "an expert with no stable blob never enters the tier (fallback must stay possible)");
        check(plan3.rows() == 64, "only the surviving eligible expert is claimed, whole");
        check(got.find(5) != got.end() && got.size() == 1, "the survivor is claimed WHOLE, and it alone");
        // the Σ cap: 5 experts x 64 = 320 rows > 256; the scan skips, never truncates
        std::vector<int32_t> cnt5((size_t) NE, 0), toks5;
        for (int e = 1; e <= 5; ++e) {
            cnt5[(size_t) e] = 64;
            for (int i = 0; i < 64; ++i) toks5.push_back(e * 64 + i);
        }
        std::vector<int32_t> res5((size_t) kLayers * kExperts, -1);
        std::vector<uint8_t> h5((size_t) NE, 1), s5((size_t) NE, 1);
        ExpertHelperPromptPlan plan4;
        sel_run(NE, cnt5, toks5, res5, h5, s5, 10, 64, 256, 256, plan4);   // M4b: fixture caps (320 > 256), independent of the protocol's
        check(plan4.rows() == 256, "the Σ cap: 320 eligible rows claim exactly 256");
        std::map<int32_t, int> cnt_per;
        for (size_t i = 0; i < plan4.pair_expert.size(); ++i) cnt_per[plan4.pair_expert[i]]++;
        int64_t whole = 0;   // the experts claimed WHOLE (all 64 of their rows)
        for (const auto& kv : cnt_per) if (kv.second == 64) ++whole;
        check(cnt_per.size() == 4 && whole == 4, "and the 256 rows are WHOLE experts (4 x 64), never a split row set");
        // the distinct-token cap: 4 experts x 64 rows that OVERLAP on the same 64 tokens.  A per-expert
        // list is a token list, so one expert can serve a token once - 256 rows on ONE token is not a
        // shape the contract can express (the library refuses a repeated pair).  The cap that matters
        // is the IMAGE: 64 distinct tokens carry the 256 rows' activations.
        std::vector<int32_t> cnt6((size_t) NE, 0), toks6, res6((size_t) kLayers * kExperts, -1);
        std::vector<uint8_t> h6((size_t) NE, 1), s6((size_t) NE, 1);
        for (int e = 1; e <= 4; ++e) {
            cnt6[(size_t) e] = 64;
            for (int i = 0; i < 64; ++i) toks6.push_back(i);      // every expert claims tokens 0..63
        }
        ExpertHelperPromptPlan plan5;
        sel_run(NE, cnt6, toks6, res6, h6, s6, 10, kMaxRowsPerExpert, kMaxRowsPerLayer, 64, plan5);
        check(plan5.n_sel_tok() == 64, "64 distinct tokens carry all 256 rows (the image is bounded by the TOKENS)");
        check(plan5.rows() == 256, "the rows are all claimed - the caps are on the IMAGE, not the rows");
        // the cap as a SKIP, the design's rule: with a 63-token image cap, every expert whose claim
        // would need all 64 tokens breaches it, so the layer claims NOTHING - and an empty plan is a
        // success (the layer stays local), never a truncation to fit.
        ExpertHelperPromptPlan plan5b;
        check_status(sel_run(NE, cnt6, toks6, res6, h6, s6, 10, kMaxRowsPerExpert, kMaxRowsPerLayer,
                             63, plan5b), kOk,
                      "an image cap no expert can fit is a SKIP, not a truncation (the scan continues)");
        check(plan5b.rows() == 0 && plan5b.n_sel_tok() == 0, "and the skipped layer claims nothing at all");
        // refusals of inputs that are not data
        ExpertHelperPromptPlan plan6;
        check_status(sel_run(NE, cnt5, toks5, res5, h5, s5, 9, kMaxRowsPerExpert, kMaxRowsPerLayer,
                             kMaxSelTok, plan6), kErrGeometry, "k other than the pack's 10 is refused");
        std::vector<int32_t> neg = cnt5;
        neg[3] = -5;
        check_status(sel_run(NE, neg, toks5, res5, h5, s5, 10, kMaxRowsPerExpert, kMaxRowsPerLayer,
                             kMaxSelTok, plan6), kErrGeometry, "a negative count is refused, not clamped");
        std::vector<int32_t> bad_tok = toks5;
        bad_tok[7] = -1;
        check_status(sel_run(NE, cnt5, bad_tok, res5, h5, s5, 10, kMaxRowsPerExpert, kMaxRowsPerLayer,
                             kMaxSelTok, plan6), kErrGeometry, "a token row that does not exist is refused");
        // an eligible-empty layer: the empty plan is a SUCCESS (the layer stays fully local)
        std::vector<int32_t> zero((size_t) NE, 0);
        ExpertHelperPromptPlan plan7;
        check_status(sel_run(NE, zero, toks5, res5, h5, s5, 10, kMaxRowsPerExpert, kMaxRowsPerLayer,
                             kMaxSelTok, plan7), kOk, "an empty claim set is a success, not a refusal");
        check(plan7.rows() == 0 && plan7.n_sel_tok() == 0, "the empty plan carries no rows and no tokens");
        // the per-expert list is concatenated in EXPERT order (the contract's shape)
        std::vector<int32_t> cnt7((size_t) NE, 0), toks7, res7((size_t) kLayers * kExperts, -1);
        std::vector<uint8_t> h7((size_t) NE, 1), s7((size_t) NE, 1);
        cnt7[2] = 3;
        for (int i = 0; i < 3; ++i) toks7.push_back(10 + i);
        cnt7[1] = 2;
        for (int i = 0; i < 2; ++i) toks7.push_back(20 + i);
        ExpertHelperPromptPlan plan8;
        sel_run(NE, cnt7, toks7, res7, h7, s7, 10, kMaxRowsPerExpert, kMaxRowsPerLayer, kMaxSelTok, plan8);
        check(plan8.rows() == 5 && plan8.n_sel_tok() == 5, "the concatenated per-expert lists are read at the offsets");
        bool expert2_first = plan8.rows() == 5 && plan8.pair_expert[0] == 2;
        check(expert2_first, "the higher count (3 on expert 2) is considered FIRST (count DESC)");
    }   // section (17)'s scope (the sel_run lambda's own block)
    }

    // ---- (18) SIGTERM during the begin-wait: the wake ends it at once (the item-0 fix), the
    // default path (no wake installed) keeps its deadline, and an in-flight frame is never
    // half-consumed.  CPU-only, no GPU, no real helper process. ----
    {
        const auto a = run_sigterm_child(kSigWoken);
        check(a.woken && a.ms < 2000,
              "SIGTERM: the handler's wake byte ends a 30-minute begin-wait in well under 2 s");
        check(!a.child_signaled, "the woken child exits on its own path (no signal death)");
        check(a.rc == kErrTimeout, "and the wait returns the transport's named timeout status");
        const auto b = run_sigterm_child(kSigControlNoWake);
        check(!b.woken && b.rc == kErrTimeout && b.ms >= 280,
              "control: with NO wake installed the same deadline is honored (the engine-side waits are unchanged)");
        const auto d = run_sigterm_child(kSigMidFrame);
        check(d.rc == kErrUnhealthy,
              "mid-frame: the wake aborts the transfer as a named unhealthy channel, never a partial frame");
        if (d.rc != kErrUnhealthy)
            std::printf("      mid-frame child: rc=%d (%s) ms=%lld signaled=%d "
                        "(-1 ms = the child reported nothing)\n", d.rc, status_name(d.rc), d.ms,
                        (int) d.child_signaled);
        const auto s = run_sigterm_child(kSigRealSignal);
        check(s.woken && s.ms < 2000 && !s.child_signaled,
              "a REAL SIGTERM (kill, the driver's exact signal) wakes the same wait promptly");

        // (18e) the latch WINS over the pre-hello re-listen (helper2.log, 2026-10-29 ~06:07: the exit
        // line ~10 times, 60 s of SIGTERM, then SIGKILL).  The guard is one predicate, and the child
        // reproduces the edge with the real signal: a peer that connects and leaves before hello.
        check(helper_relisten_allowed(false, false, false),
              "no latch, no hello, no --once: the prober's re-listen stays (the edge's whole point)");
        check(!helper_relisten_allowed(true, false, false),
              "the latch is latched: NO re-listen, whatever the peer did (the fix)");
        check(!helper_relisten_allowed(true, true, false) && !helper_relisten_allowed(false, true, false),
              "a peer that said hello never re-listens");
        check(!helper_relisten_allowed(false, false, true),
              "--once never re-listens");
        const int rc18 = run_relisten_child();
        check(rc18 == 0, "SIGTERM latched, peer connected and left before hello: the child EXITS, it does not re-listen");
        if (rc18 != 0)
            std::printf("      relisten child: exit=%d (0 = the clean exit, 42 = it kept re-listening, "
                        "-2 = signal death, -3 = it survived 4 s of SIGTERM, 11..14 = fixture)\n", rc18);
    }

    // ---- (19) the Q8_K activation wire (default OFF: STRATA_EXPERT_HELPER_Q8K + the peer's
    // kFeatureQ8K): the negotiation gates, the frame types, the twin quantizer's golden structure,
    // and the default-OFF byte identity of the prompt tier's image.  CPU-only, no GPU in sight;
    // the hardware acceptance (the helper's rows through native_expert_grouped_q8k) is
    // docs/work/MIXED_P2_REFERENCE.md's measured chain. ----
    {
        // (19a) quantize_row_q8k_cpu's structure, pinned on hand-checkable blocks (ggml's reference
        // semantics: strict > amax, iscale = -127/max, the magic-number nearest_int with
        // half-to-even, MIN(127, v), d = 1/iscale the RECIPROCAL, bsums per 16).
        {
            std::vector<float> x((size_t) kH, 0.0f);
            std::vector<uint8_t> y((size_t) (kH / 256) * 292, 0xFF);
            quantize_row_q8k_cpu(x.data(), kH, y.data());
            bool ok = true;
            for (int i = 0; i < 4; ++i) ok = ok && y[(size_t) i] == 0;           // d == 0
            for (size_t i = 4; i < 260; ++i) ok = ok && y[i] == 0;               // qs zeroed
            for (size_t i = 260; i < y.size(); ++i) ok = ok && y[i] == 0;        // bsums zeroed
            check(ok, "q8k golden: the all-zero block writes d 0, zeroed qs and zeroed bsums");

            std::fill(x.begin(), x.end(), 0.5f);
            std::fill(y.begin(), y.end(), 0xFF);
            quantize_row_q8k_cpu(x.data(), kH, y.data());
            const float d_const = -1.0f / 254.0f;   // 1/iscale = 1/(-127/0.5): the reciprocal
            float d_got = 0;
            std::memcpy(&d_got, y.data(), 4);
            ok = d_got == d_const;
            for (size_t i = 4; i < 260; ++i) ok = ok && y[i] == 0x81;            // qs all -127
            for (int j = 0; j < 16; ++j) {
                int16_t b = 0;
                std::memcpy(&b, y.data() + 260 + (size_t) j * 2, 2);
                ok = ok && b == -2032;                                           // 16 * -127
            }
            check(ok, "q8k golden: the constant 0.5 block (d = 1/(-254), qs -127, bsums -2032)");

            // mixed {2, -1, 0.25, 0...}: max 2 (strict >, the FIRST max wins), iscale -63.5,
            // half-even rounding: 63.5 -> 64, -15.875 -> -16, -0.0 -> 0.
            x[0] = 2.0f; x[1] = -1.0f; x[2] = 0.25f;
            for (size_t i = 3; i < (size_t) kH; ++i) x[i] = 0.0f;
            std::fill(y.begin(), y.end(), 0xFF);
            quantize_row_q8k_cpu(x.data(), kH, y.data());
            const int8_t* qs = (const int8_t*) (y.data() + 4);
            int16_t b0 = 0;
            std::memcpy(&b0, y.data() + 260, 2);
            std::memcpy(&d_got, y.data(), 4);
            check(qs[0] == -127 && qs[1] == 64 && qs[2] == -16 && qs[3] == 0 &&
                      b0 == -79 && d_got == 1.0f / (-127.0f / 2.0f),
                  "q8k golden: the mixed block (iscale -127/max, half-even rounding, d = 1/iscale)");

            // negative max {-2, -1, 0...}: max -2, iscale +63.5 - the SIGN semantics of ggml's
            // -127/max trick (q*d reconstructs x: -127 * (1/63.5) = -2).
            x[0] = -2.0f; x[1] = -1.0f;
            for (size_t i = 2; i < (size_t) kH; ++i) x[i] = 0.0f;
            std::fill(y.begin(), y.end(), 0xFF);
            quantize_row_q8k_cpu(x.data(), kH, y.data());
            std::memcpy(&d_got, y.data(), 4);
            check(qs[0] == -127 && qs[1] == -64 && d_got == 1.0f / 63.5f && d_got > 0.0f,
                  "q8k golden: the negative-max block (iscale +63.5, d positive, q*d = x)");

            // the row STRIDE is 292: a 512-value row is two blocks, block 2's qs at byte 292+4.
            x.assign(512, 0.0f);
            std::fill(x.begin() + 256, x.end(), 0.5f);
            y.assign((size_t) 2 * 292, 0xFF);
            quantize_row_q8k_cpu(x.data(), 512, y.data());
            check(y[4] == 0 && y[292 + 4] == (uint8_t) (int8_t) -127,
                  "q8k golden: the 292-byte stride (block 2's qs at 292+4)");
        }

        // (19b) the negotiation gate: begin_q8k is refused LOCALLY, nothing reaches the wire.
        {
            ExpertHelperClient c;
            check_status(handshake(c), kOk, "plain handshake (no feature bits)");
            std::vector<int32_t> ids((size_t) kNTok * 10, 7);
            std::vector<int32_t> primary((size_t) kLayers * kExperts, -1);
            std::vector<uint8_t> q8k((size_t) (kNTok * (kH / 256) * 292), 0x5A);
            check_status(c.begin_q8k(kLayer, kNTok, ids.data(), primary.data(), q8k.data(), q8k.size(), 2000),
                         kErrRequest, "begin_q8k without the advertised bit: the local refusal");
            Socket srv;
            srv.adopt(g_server_fd);
            Header head;
            RxBuffer rx;
            PayloadView body;
            check_status(srv.read_frame(head, rx, body, deadline_after(300)), kErrTimeout,
                         "and the wire stayed silent (the frame never reaches an unadvertising peer)");
            srv.release();
        }

        // (19c) with the bit advertised: the kBeginQ8K frame is exact (fields, residency, image).
        {
            ExpertHelperClient c;
            check_status(handshake(c, kFeatureQ8K), kOk, "handshake advertising kFeatureQ8K");
            check(c.q8k_supported(), "the client records the advertised Q8_K wire");
            check(send_begin_q8k(c), "the kBeginQ8K frame carries the window's exact shape and image");
        }

        // (19d) the prompt tier: env + bit ships image_format 2 with the twin's bytes; the DEFAULT
        // (env off, or the bit not advertised) keeps today's f32 image, byte for byte.
        {
            std::vector<int32_t> primary((size_t) kLayers * kExperts, -1);
            ExpertHelperClient c;
            check_status(handshake_v3(c, true, kFeaturePromptF32 | kFeatureQ8K), kOk,
                         "v3 handshake advertising fp32 AND the Q8_K bit");
            ExpertHelperPromptPlan plan;
            for (int i = 0; i < 4; ++i) { plan.pair_token.push_back(i); plan.pair_expert.push_back(7); }
            for (int i = 0; i < 4; ++i) plan.tokens.push_back(i);
            fill_prompt_buffer(c, 4);
            ::setenv("STRATA_EXPERT_HELPER_Q8K", "1", 1);
            check_status(c.begin_prompt(kLayer, plan, primary.data(), 4000), kOk,
                         "kBeginP with image_format 2 is sent");
            check(wire_prompt_q8k_ok(plan, primary.data()),
                  "the frame's Q8_K image is quantize_row_q8k_cpu of the tier's own rows, exact");
            ::unsetenv("STRATA_EXPERT_HELPER_Q8K");

            // env off (the default): the same advertising peer, the same rows - today's f32 image.
            ExpertHelperClient c2;
            check_status(handshake_v3(c2, true, kFeaturePromptF32 | kFeatureQ8K), kOk,
                         "v3 handshake again (the default-path control)");
            ExpertHelperPromptPlan plan2;
            for (int i = 0; i < 4; ++i) { plan2.pair_token.push_back(i); plan2.pair_expert.push_back(7); }
            for (int i = 0; i < 4; ++i) plan2.tokens.push_back(i);
            fill_prompt_buffer(c2, 4);
            check_status(c2.begin_prompt(kLayer, plan2, primary.data(), 4000), kOk,
                         "kBeginP with the env off is sent");
            check(wire_prompt_ok(plan2, primary.data(), 4, /*f32=*/true),
                  "the default keeps the f32 image_format 1 frame, byte for byte");

            // env on but the bit NOT advertised: the default image again (a mixed-build peer).
            ExpertHelperClient c3;
            check_status(handshake_v3(c3, true, kFeaturePromptF32), kOk,
                         "v3 handshake without the Q8_K bit (the mixed build)");
            ExpertHelperPromptPlan plan3;
            for (int i = 0; i < 4; ++i) { plan3.pair_token.push_back(i); plan3.pair_expert.push_back(7); }
            for (int i = 0; i < 4; ++i) plan3.tokens.push_back(i);
            fill_prompt_buffer(c3, 4);
            ::setenv("STRATA_EXPERT_HELPER_Q8K", "1", 1);
            check_status(c3.begin_prompt(kLayer, plan3, primary.data(), 4000), kOk,
                         "kBeginP to a non-advertising peer is sent");
            check(wire_prompt_ok(plan3, primary.data(), 4, /*f32=*/true),
                  "without the peer's bit the image stays f32 (format 1), never a misparse");
            ::unsetenv("STRATA_EXPERT_HELPER_Q8K");
        }
    }

    std::printf("expert_helper_socket_test: %s\n", failures == 0 ? "PASS" : "FAIL");
    return failures == 0 ? 0 : 1;
}
