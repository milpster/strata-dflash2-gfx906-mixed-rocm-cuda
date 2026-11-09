// tools/mixed_gpu/sidecar/helper_microbench.cpp - R2: the framed AF_UNIX transport's CPU-only cost.
//
// The helper's own numbers, measured with NO GPU in sight: one window is a kBegin frame of
// ~23.4 KB out and a kRows frame of n_rows x (4 + 4*2560) B back (80 rows = 819,280 B), over a
// socketpair - the same Socket/frame code the engine's proxy client and the CUDA helper run
// verbatim.  This says what the channel costs per window so the parent's GPU measurement can
// subtract it; it makes no claim about the GPU's own time, and it runs on a CPU-only gate.
//
// CPU-only by construction: no GPU header, no GPU library.  Exit 0 = the frames round-tripped.
#include "strata/core/expert_helper_protocol.hpp"

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <thread>
#include <vector>

#include <sys/socket.h>
#include <unistd.h>

using namespace strata::core::expert_helper;

namespace {

constexpr int64_t kH = 2560;

std::vector<uint8_t> begin_payload(int64_t n_tok) {
    BeginReq req;
    req.layer = 3;
    req.n_tok = (int32_t) n_tok;
    req.k = (int32_t) kK;
    req.ids_bytes = (int32_t) (n_tok * kK * 4);
    req.n_q8 = (int32_t) (n_tok * (kH / 32) * 36);
    const size_t res_bytes = (size_t) ((n_tok * 512 + 7) / 8);
    std::vector<uint8_t> payload(sizeof(req) + (size_t) req.ids_bytes + res_bytes + (size_t) req.n_q8, 0x5A);
    std::memcpy(payload.data(), &req, sizeof(req));
    return payload;
}

std::vector<uint8_t> rows_payload(int32_t n_rows) {
    RowsResp h;
    h.n_rows = n_rows;
    std::vector<uint8_t> payload(sizeof(h) + (size_t) n_rows * (4 + (size_t) kH * 4), 0x3C);
    std::memcpy(payload.data(), &h, sizeof(h));
    for (int32_t i = 0; i < n_rows; ++i) {
        const int32_t idx = i;
        std::memcpy(payload.data() + sizeof(h) + (size_t) i * (4 + (size_t) kH * 4), &idx, 4);
    }
    return payload;
}

double run_rounds(int client_fd, int server_fd, int64_t n_tok, int32_t n_rows, int rounds) {
    const auto begin = begin_payload(n_tok);
    const auto rows = rows_payload(n_rows);
    // The HELPER side runs on its own thread, as it does in reality (a second process): a 0.83 MB kRows
    // frame does not fit a socketpair's buffer, so a single-threaded driver that writes it before reading
    // it deadlocks in the kernel's send path.  The transport is unchanged - both ends use the same Socket
    // code the engine's proxy client and the CUDA helper run.
    std::thread peer([&, server_fd] {
        Socket server;
        server.adopt(server_fd);
        RxBuffer rx;
        for (int r = 0; r < rounds; ++r) {
            Header head;
            std::span<const uint8_t> body;
            const uint32_t req_id = (uint32_t) (r + 1);
            if (server.read_frame(head, rx, body, deadline_after(30000)) != kOk || head.type != kBegin) {
                std::fprintf(stderr, "helper_microbench: begin read failed\n");
                std::exit(1);
            }
            if (server.write_frame(kRows, req_id, rows.data(), rows.size(), deadline_after(30000)) != kOk) {
                std::fprintf(stderr, "helper_microbench: rows write failed\n");
                std::exit(1);
            }
        }
        server.release();
    });
    Socket client;
    client.adopt(client_fd);
    const auto t0 = std::chrono::steady_clock::now();
    RxBuffer rx;
    for (int r = 0; r < rounds; ++r) {
        const uint32_t req_id = (uint32_t) (r + 1);
        if (client.write_frame(kBegin, req_id, begin.data(), begin.size(), deadline_after(30000)) != kOk) {
            std::fprintf(stderr, "helper_microbench: begin write failed\n");
            std::exit(1);
        }
        Header head;
        std::span<const uint8_t> body;
        if (client.read_frame(head, rx, body, deadline_after(30000)) != kOk || head.type != kRows) {
            std::fprintf(stderr, "helper_microbench: rows read failed\n");
            std::exit(1);
        }
    }
    const double ms = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0).count();
    peer.join();
    client.release();
    return ms;
}

}  // namespace

int main(int argc, char** argv) {
    int rounds = 200;
    if (argc > 1) rounds = std::atoi(argv[1]);
    if (rounds < 1 || rounds > 100000) return 2;
    std::printf("helper_microbench: the framed AF_UNIX transport, CPU-only (socketpair), %d rounds\n", rounds);
    bool ok = true;
    for (const auto [n_tok, n_rows] : std::vector<std::pair<int64_t, int32_t>>{{1, 10}, {8, 40}, {8, 80}}) {
        int fds[2];
        if (::socketpair(AF_UNIX, SOCK_STREAM, 0, fds) != 0) return 1;
        const double ms = run_rounds(fds[0], fds[1], n_tok, n_rows, rounds);
        const size_t out = sizeof(BeginReq) + (size_t) (n_tok * kK * 4) + (size_t) ((n_tok * 512 + 7) / 8) +
                           (size_t) (n_tok * (kH / 32) * 36) + kHeaderBytes;
        const size_t in = sizeof(RowsResp) + (size_t) n_rows * (4 + (size_t) kH * 4) + kHeaderBytes;
        std::printf("  n_tok=%lld rows=%-3d begin=%7zu B rows=%8zu B: %7.3f ms/window (%.2f GB/s both ways)\n",
                    (long long) n_tok, (int) n_rows, out, in, ms / rounds,
                    (double) (out + in) * (double) rounds / (ms * 1e6));
        ::close(fds[0]);
        ::close(fds[1]);
        if (!(ms > 0.0)) ok = false;
    }
    std::printf("helper_microbench: %s\n", ok ? "PASS" : "FAIL");
    return ok ? 0 : 1;
}
