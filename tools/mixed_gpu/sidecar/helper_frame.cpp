// tools/mixed_gpu/sidecar/helper_frame.cpp - the framed AF_UNIX transport (CPU-only; see the header).
//
// Partial I/O, EINTR, EOF, EPIPE, MSG_NOSIGNAL, and one deadline-bearing poll per wait: every loop
// keeps an ABSOLUTE deadline so a stalled peer costs a named timeout, never a hung engine thread and
// never an unbounded read.  Nothing here opens a GPU runtime; nothing here is CUDA/HIP aware.  The
// engine's proxy client, the CUDA helper and the host-IPC tests all run THIS file's I/O.
#include "strata/core/expert_helper_protocol.hpp"

#include <atomic>
#include <cerrno>
#include <climits>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <fcntl.h>
#include <poll.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/uio.h>
#include <sys/un.h>
#include <unistd.h>

#ifndef MSG_NOSIGNAL
#define MSG_NOSIGNAL 0   // platforms without it: a write to a closed socket is handled by EPIPE checks
#endif

namespace strata::core::expert_helper {
namespace {

int64_t now_ms() {
    timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t) ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}
int64_t remaining_ms(int64_t deadline_ms) {
    const int64_t r = deadline_ms - now_ms();
    return r < 0 ? 0 : r;
}

// R5 P1: the wake descriptor (see the header's set_poll_wake).  -1 is the state every engine-side
// client runs in, and the state this file had before the helper's begin-wait fix: a one-fd poll,
// untouched.  The CUDA helper installs a self-pipe; its SIGTERM/SIGINT handler writes one byte.
std::atomic<int> g_poll_wake{-1};

// poll with EINTR retried against the ABSOLUTE deadline.  1 ready, 0 deadline expired, -1 error.
int poll_once(int fd, short events, int64_t deadline_ms) {
    for (;;) {
        const int64_t left = remaining_ms(deadline_ms);
        if (left <= 0) return 0;
        const int wake = g_poll_wake.load(std::memory_order_relaxed);
        pollfd p[2] = {pollfd{fd, events, 0}, pollfd{wake, POLLIN, 0}};
        const nfds_t n = (wake >= 0) ? 2 : 1;
        const int rc = ::poll(p, n, (int) (left > 2000 ? 2000 : left));
        // The wake descriptor answers with the SAME value an expired deadline answers (0), so every
        // caller takes its existing named-timeout path: the CUDA helper's 30-minute begin-wait ends
        // on the signal's byte, and the engine's client - which never installs a wake - is unchanged.
        if (wake >= 0 && (p[1].revents & (POLLIN | POLLHUP | POLLERR | POLLNVAL))) return 0;
        if (rc < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        if (rc == 0) {
            if (remaining_ms(deadline_ms) <= 0) return 0;   // the deadline really expired
            continue;   // only the 2 s poll cap expired with time left: keep waiting, NOT a timeout
        }
        if (p[0].revents & (POLLERR | POLLNVAL)) return -1;
        if (p[0].revents & POLLHUP) return (p[0].revents & events) != 0 ? 1 : -1;
        return (p[0].revents & events) != 0 ? 1 : 0;
    }
}

uint32_t rd32(const uint8_t* p) { uint32_t v = 0; std::memcpy(&v, p, 4); return v; }
void wr32(uint8_t* p, uint32_t v) { std::memcpy(p, &v, 4); }

// helper-ipc-diet (2026-11, the ab9 measurement): AF_UNIX's default socket buffers (net.core
// rmem/wmem_default, ~200 KB) fit a fraction of ONE 24 MB rows reply, so a kRowsP frame ping-pongs
// thousands of EAGAIN/poll round-trips between the writer's full buffer and the reader's drain.
// Ask for 8 MiB per side at socket creation on BOTH sides of the channel: the kernel clamps at
// net.core.rmem_max/wmem_max and stores 2*opt, so the achieved pair is read back at hello
// (log_socket_buffers) - which is what the channel runs with, and all that changes is the clamp.
// A listen-side set is inherited by every accepted channel (Linux clones sk_sndbuf/sk_rcvbuf at
// accept), so the helper's bound socket covers all its connections.
void set_socket_buffers(int fd) {
    const int want = 8 << 20;
    ::setsockopt(fd, SOL_SOCKET, SO_SNDBUF, &want, sizeof(want));
    ::setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &want, sizeof(want));
}

}  // namespace

void log_socket_buffers(int fd, const char* side) {
    int snd = 0, rcv = 0;
    socklen_t len = sizeof(int);
    ::getsockopt(fd, SOL_SOCKET, SO_SNDBUF, &snd, &len);
    ::getsockopt(fd, SOL_SOCKET, SO_RCVBUF, &rcv, &len);
    std::fprintf(stderr, "expert-helper: %s socket buffers achieved snd=%d rcv=%d\n", side, snd, rcv);
}

int64_t deadline_after(int64_t ms) { return now_ms() + (ms > 0 ? ms : 0); }

void set_io_nonblocking(int fd) {
    const long fl = ::fcntl(fd, F_GETFL, 0);
    if (fl >= 0) ::fcntl(fd, F_SETFL, fl | O_NONBLOCK);
}

void set_poll_wake(int fd) { g_poll_wake.store(fd, std::memory_order_relaxed); }

bool helper_relisten_allowed(bool stop_latched, bool hello_seen, bool once) {
    // The stop latch WINS: a signal nobody can undo must never be answered by re-binding the socket.
    // The pre-hello re-listen exists for a peer that left before hello (a prober, an engine that died
    // at startup) in a helper that was never asked to stop; with the latch set the loop falls through
    // to the existing clean exit (release(), unlink of the path THIS process bound).
    return !stop_latched && !hello_seen && !once;
}

const char* status_name(int status) {
    switch (status) {
        case kOk: return "ok";
        case kErrConnect: return "helper connect failed";
        case kErrPeer: return "helper peer refused";
        case kErrProtocol: return "helper protocol violation";
        case kErrGeometry: return "helper geometry refused";
        case kErrUnhealthy: return "helper channel closed";
        case kErrTimeout: return "helper timeout";
        case kErrShort: return "helper frame short";
        case kErrBadId: return "helper row index refused";
        case kErrMissing: return "helper row missing";
        case kErrNonFinite: return "helper row non-finite";
        case kErrSeq: return "helper request id mismatch";
        case kErrRequest: return "helper refused the request";
        // M7 (sect12a, BUG E): a client-side-only status (no peer ever sends it) - the drafter
        // lane's frame found an in-flight window/prompt request on the shared channel.
        case kErrBusy: return "helper channel busy with an in-flight request";
        default: return "helper internal";
    }
}

void encode_header(const Header& h, uint8_t* out) {
    wr32(out, h.magic);
    wr32(out + 4, h.version);
    wr32(out + 8, h.type);
    wr32(out + 12, h.seq);
    wr32(out + 16, h.req_id);
    wr32(out + 20, h.len);
}

int decode_header(const uint8_t* in, Header& out) {
    out.magic = rd32(in);
    out.version = rd32(in + 4);
    out.type = rd32(in + 8);
    out.seq = rd32(in + 12);
    out.req_id = rd32(in + 16);
    out.len = rd32(in + 20);
    if (out.magic != kMagic) return kErrProtocol;
    if (out.version != kProtoVersion) return kErrProtocol;
    if (out.len > kMaxFramePayload) return kErrProtocol;
    return kOk;
}

bool socket_path_ok(const std::string& path, std::string& why) {
#ifdef __linux__
    if (path.size() < 2 || path[0] != '/') { why = "the helper socket path must be absolute"; return false; }
    if (path.size() > sizeof(((sockaddr_un*) nullptr)->sun_path) - 1) { why = "the helper socket path is too long"; return false; }
    for (char c : path)
        if ((unsigned char) c < 0x20 || (unsigned char) c >= 0x7f) { why = "the helper socket path has control bytes"; return false; }
    struct stat st;
    if (::stat(path.c_str(), &st) == 0) { why = "the helper socket path already exists (the helper binds its own socket)"; return false; }
    return true;
#else
    (void) path;
    why = "the expert helper socket (AF_UNIX) is supported on Linux only";
    return false;
#endif
}

bool socket_path_connect_ok(const std::string& path, std::string& why) {
    // H7: the CONNECT-side shape check.  socket_path_ok above is bind safety (the helper owns a fresh path);
    // the engine used to run it before connecting, which refused the very socket the helper had bound — the
    // documented flow starts the helper BEFORE the engine, so a bound socket is the expected case here.
    // A foreign existing file is still refused (and never unlinked); connect() itself reports a stale socket.
#ifdef __linux__
    if (path.size() < 2 || path[0] != '/') { why = "the helper socket path must be absolute"; return false; }
    if (path.size() > sizeof(((sockaddr_un*) nullptr)->sun_path) - 1) { why = "the helper socket path is too long"; return false; }
    for (char c : path)
        if ((unsigned char) c < 0x20 || (unsigned char) c >= 0x7f) { why = "the helper socket path has control bytes"; return false; }
    struct stat st;
    if (::lstat(path.c_str(), &st) == 0 && !S_ISSOCK(st.st_mode)) {
        why = "the helper socket path exists and is not a socket (left alone; the helper binds its own path)";
        return false;
    }
    return true;
#else
    (void) path;
    why = "the expert helper socket (AF_UNIX) is supported on Linux only";
    return false;
#endif
}

// H8: the claimed bitmap's unpack (see the header).  The expression is the helper's own, verbatim:
// a pair with no packed byte, or a clear bit, is 0 (unclaimed); no byte past `nbytes` is ever read.
void unpack_claimed_bitmap(const uint8_t* bits, size_t nbytes, size_t n_pairs, std::vector<uint8_t>& out) {
    out.assign(n_pairs, 0);
    for (size_t i = 0; i < n_pairs; ++i)
        out[i] = (nbytes > i / 8 && ((bits[i / 8] >> (i % 8)) & 1)) ? 1 : 0;
}

Socket::~Socket() { close(); }

void Socket::close() {
    if (fd_ >= 0) { ::close(fd_); fd_ = -1; }
}

int Socket::connect(const std::string& path, int64_t connect_timeout_ms, bool check_peer) {
    close();
    sockaddr_un addr;
    std::memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    if (path.empty() || path.size() >= sizeof(addr.sun_path)) return kErrConnect;
    std::strncpy(addr.sun_path, path.c_str(), sizeof(addr.sun_path) - 1);
    int fd = ::socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd < 0) return kErrConnect;
    set_socket_buffers(fd);   // the 8 MiB ask (see above): this side owns the request
    const long fl = ::fcntl(fd, F_GETFL, 0);
    if (fl < 0 || ::fcntl(fd, F_SETFL, fl | O_NONBLOCK) != 0) { ::close(fd); return kErrConnect; }
    int rc = ::connect(fd, (sockaddr*) &addr, sizeof(addr));
    if (rc != 0 && errno == EINPROGRESS) {
        const int pr = poll_once(fd, POLLOUT, now_ms() + connect_timeout_ms);
        if (pr <= 0) { ::close(fd); return pr == 0 ? kErrTimeout : kErrConnect; }
        int err = 0;
        socklen_t el = sizeof(err);
        if (::getsockopt(fd, SOL_SOCKET, SO_ERROR, &err, &el) != 0 || err != 0) { ::close(fd); return kErrConnect; }
    } else if (rc != 0) {
        ::close(fd);
        return kErrConnect;
    }
    if (::fcntl(fd, F_SETFL, ::fcntl(fd, F_GETFL, 0) | O_NONBLOCK) != 0) { ::close(fd); return kErrConnect; }   // stays non-blocking: the polls ARE the bound
    if (check_peer && peer_ok(fd, geteuid()) != kOk) { ::close(fd); return kErrPeer; }
    fd_ = fd;
    return kOk;
}

int Socket::listen_bind(const std::string& path) {
    close();
    std::string why;
    if (!socket_path_ok(path, why)) return kErrConnect;   // never over an existing file: the helper owns its path
    sockaddr_un addr;
    std::memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    if (path.empty() || path.size() >= sizeof(addr.sun_path)) return kErrConnect;
    std::strncpy(addr.sun_path, path.c_str(), sizeof(addr.sun_path) - 1);
    int fd = ::socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd < 0) return kErrConnect;
    set_socket_buffers(fd);   // accepted channels inherit this buffer pair at accept (Linux clones it)
    const mode_t old = ::umask(0777);
    const int rc = ::bind(fd, (sockaddr*) &addr, sizeof(addr));
    ::umask(old);
    if (rc != 0) { ::close(fd); return kErrConnect; }
    ::chmod(path.c_str(), 0600);   // the created socket is 0600: no other user may connect to it
    if (::listen(fd, 4) != 0) { ::close(fd); ::unlink(path.c_str()); bound_path_.clear(); return kErrConnect; }
    fd_ = fd;
    bound_path_ = path;
    return kOk;
}

void Socket::unbind() {
    close();
    if (!bound_path_.empty()) { ::unlink(bound_path_.c_str()); bound_path_.clear(); }   // only the path THIS socket bound
}

int Socket::accept(int64_t deadline_ms, bool check_peer) {
    for (;;) {
        const int pr = poll_once(fd_, POLLIN, deadline_ms);
        if (pr <= 0) return pr == 0 ? kErrTimeout : kErrConnect;
        int fd = ::accept4(fd_, nullptr, nullptr, SOCK_CLOEXEC);
        if (fd < 0) {
            if (errno == EINTR) continue;
            return kErrConnect;
        }
        set_io_nonblocking(fd);   // the channel's I/O mode (see the header's Socket contract)
        if (check_peer && peer_ok(fd, geteuid()) != kOk) { ::close(fd); return kErrPeer; }
        return fd;
    }
}

int Socket::peer_uid(int fd) {
    struct ucred cred;
    socklen_t len = sizeof(cred);
    if (::getsockopt(fd, SOL_SOCKET, SO_PEERCRED, &cred, &len) != 0) return -1;
    return (int) cred.uid;
}

int Socket::peer_ok(int fd, uid_t expect) {
    const int uid = peer_uid(fd);
    if (uid < 0) return kErrPeer;
    if (uid != (int) expect) return kErrPeer;
    return kOk;
}

int Socket::write_all(const uint8_t* data, size_t n, int64_t deadline_ms) const {
    if (fd_ < 0) return kErrUnhealthy;
    size_t off = 0;
    while (off < n) {
        const int pr = poll_once(fd_, POLLOUT, deadline_ms);
        if (pr < 0) return kErrUnhealthy;
        if (pr == 0) return kErrTimeout;
        const ssize_t w = ::write(fd_, data + off, n - off);
        if (w < 0) {
            if (errno == EINTR) continue;
            if (errno == EAGAIN || errno == EWOULDBLOCK) continue;   // buffer full: re-poll against the same deadline
            return kErrUnhealthy;   // EPIPE / ECONNRESET / anything: the channel is done
        }
        if (w == 0) return kErrUnhealthy;
        off += (size_t) w;
    }
    return kOk;
}

int Socket::read_all(uint8_t* data, size_t n, int64_t deadline_ms) const {
    if (fd_ < 0) return kErrUnhealthy;
    size_t off = 0;
    while (off < n) {
        const int pr = poll_once(fd_, POLLIN, deadline_ms);
        if (pr < 0) return kErrUnhealthy;
        if (pr == 0) return kErrTimeout;
        const ssize_t r = ::recv(fd_, data + off, n - off, MSG_NOSIGNAL);
        if (r == 0) return kErrUnhealthy;   // EOF: the peer is gone
        if (r < 0) {
            if (errno == EINTR) continue;
            if (errno == EAGAIN || errno == EWOULDBLOCK) continue;   // spurious readiness: re-wait
            return kErrUnhealthy;
        }
        off += (size_t) r;
    }
    return kOk;
}

int Socket::write_frame(uint32_t type, uint32_t req_id, const uint8_t* payload, size_t n, int64_t deadline_ms) {
    return write_seq(++seq_out_, type, req_id, payload, n, deadline_ms);
}

int Socket::write_reply(uint32_t type, uint32_t req_id, uint32_t echo_seq, const uint8_t* payload, size_t n,
                        int64_t deadline_ms) {
    // H13: the reply carries the REQUEST's sequence, not our own counter.
    return write_seq(echo_seq, type, req_id, payload, n, deadline_ms);
}

int Socket::write_seq(uint32_t seq, uint32_t type, uint32_t req_id, const uint8_t* payload, size_t n,
                      int64_t deadline_ms) {
    if (n > kMaxFramePayload) return kErrProtocol;
    Header h;
    h.type = type;
    h.seq = seq;
    h.req_id = req_id;
    h.len = (uint32_t) n;
    uint8_t head[kHeaderBytes];
    encode_header(h, head);
    // writev from a cursor: partial sends resume where they stopped; MSG_NOSIGNAL keeps SIGPIPE off.
    size_t lens[2] = {kHeaderBytes, n};
    void* bases[2] = {head, (void*) payload};
    size_t off = 0;
    while (off < kHeaderBytes + n) {
        const int pr = poll_once(fd_, POLLOUT, deadline_ms);
        if (pr < 0) return kErrUnhealthy;
        if (pr == 0) return kErrTimeout;
        struct iovec v[2];
        int cnt = 0;
        size_t skip = off;
        for (int i = 0; i < 2; ++i) {
            if (skip >= lens[i]) { skip -= lens[i]; continue; }
            v[cnt].iov_base = (char*) bases[i] + skip;
            v[cnt].iov_len = lens[i] - skip;
            ++cnt;
            skip = 0;
        }
        // H15 (r25a): writev takes no flags, so the MSG_NOSIGNAL claim above was never true and a
        // dead peer killed the writer with SIGPIPE (engine exit -13 mid-fault-injection). sendmsg
        // carries the same iovec AND the flag; EPIPE now lands in the error path below ->
        // kErrUnhealthy -> channel closed -> the engine's proven fallback.
        struct msghdr mh = {};
        mh.msg_iov = v;
        mh.msg_iovlen = cnt;
        const ssize_t w = ::sendmsg(fd_, &mh, MSG_NOSIGNAL);
        if (w < 0) {
            if (errno == EINTR) continue;
            if (errno == EAGAIN || errno == EWOULDBLOCK) continue;   // buffer full: re-poll against the same deadline
            return kErrUnhealthy;
        }
        if (w == 0) return kErrUnhealthy;
        off += (size_t) w;
    }
    return kOk;
}

int Socket::read_frame(Header& head, std::vector<uint8_t>& payload, int64_t deadline_ms) const {
    uint8_t raw[kHeaderBytes];
    int rc = read_all(raw, kHeaderBytes, deadline_ms);
    if (rc != kOk) return rc;
    rc = decode_header(raw, head);
    if (rc != kOk) return rc;
    payload.assign(head.len, 0);
    if (head.len && read_all(payload.data(), head.len, deadline_ms) != kOk) return kErrUnhealthy;
    return kOk;
}

int Socket::peer_closed() const {
    if (fd_ < 0) return kErrUnhealthy;
    pollfd p{fd_, POLLIN | POLLHUP | POLLERR, 0};
    const int rc = ::poll(&p, 1, 0);
    if (rc < 0) return kErrUnhealthy;
    if (p.revents & (POLLERR | POLLNVAL)) return kErrUnhealthy;
    if ((p.revents & POLLHUP) && !(p.revents & POLLIN)) return kErrUnhealthy;   // closed, nothing buffered
    return kOk;
}

}  // namespace strata::core::expert_helper
