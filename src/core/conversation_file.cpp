#include "strata/core/conversation_file.hpp"

#include <algorithm>
#include <bit>
#include <cerrno>
#include <chrono>
#include <climits>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <new>
#include <unordered_map>
#include <optional>
#include <filesystem>
#include <limits>
#include <memory>
#include <random>
#include <string>
#include <system_error>
#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <malloc.h>
#include <process.h>
#else
#include <fcntl.h>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <sys/types.h>
#include <unistd.h>
#endif

// Format v1 stores integers and floats as this machine holds them in memory; it is defined as little-endian with
// IEEE-754 floats, so a build where that is not the memory layout is refused rather than writing another format.
static_assert(std::endian::native == std::endian::little, "session files (format v1) are little-endian only");
static_assert(std::numeric_limits<float>::is_iec559 && std::numeric_limits<double>::is_iec559,
              "session files (format v1) need IEEE-754 floats");

namespace strata::core {
namespace {
constexpr char kMagic[8] = {'S', 'T', 'R', 'S', 'E', 'S', 'S', '\x01'};
constexpr char kEnd[8] = {'S', 'T', 'R', 'S', 'E', 'N', 'D', '\x01'};
constexpr uint32_t kVersion = 1;
constexpr size_t kHeader = 64, kTrailer = 16;

// Progress for a transfer: one report after every block that was handed to (or read from) the OS, whatever its size
// - the last, partial block included - so a small file reports too, and each report follows real movement.
class Ticker {
public:
    explicit Ticker(const SessionProgress& fn) : fn_(fn) {}
    void operator()(uint64_t done, uint64_t total) { if (fn_) fn_(done, total); }
private:
    SessionProgress fn_;
};

SessionError errno_kind(int e) {
    switch (e) {
        case ENOSPC:
#ifdef EDQUOT
        case EDQUOT:
#endif
        case EFBIG: return SessionError::storage;
        case ENOMEM: return SessionError::memory;
        default: return SessionError::io;
    }
}

constexpr uint64_t P1 = 0x9E3779B185EBCA87ull, P2 = 0xC2B2AE3D27D4EB4Full, P3 = 0x165667B19E3779F9ull,
                   P4 = 0x85EBCA77C2B2AE63ull, P5 = 0x27D4EB2F165667C5ull;
uint64_t rotl(uint64_t x, int r) { return (x << r) | (x >> (64 - r)); }
uint64_t round1(uint64_t acc, uint64_t w) { return rotl(acc + w * P2, 31) * P1; }
uint64_t load64(const uint8_t* p) { uint64_t v; std::memcpy(&v, p, 8); return v; }

// ---- file I/O in aligned 16 MiB blocks.  Linux: O_DIRECT when the filesystem takes it (no page cache, so a
// 1.2 GB session neither evicts other files nor is charged to the engine's memory cgroup), buffered I/O otherwise
// (tmpfs, a filesystem that refuses an aligned direct transfer, or STRATA_SESSION_BUFFERED=1).  Windows and other
// systems: ordinary buffered I/O, same blocks.
constexpr size_t kAlign = 4096, kBlock = 16u << 20;
#ifdef _WIN32
struct AlignedFree { void operator()(uint8_t* p) const { _aligned_free(p); } };
using Aligned = std::unique_ptr<uint8_t, AlignedFree>;
Aligned aligned_block() { return Aligned(static_cast<uint8_t*>(_aligned_malloc(kBlock, kAlign))); }
#else
struct AlignedFree { void operator()(uint8_t* p) const { std::free(p); } };
using Aligned = std::unique_ptr<uint8_t, AlignedFree>;
Aligned aligned_block() { return Aligned(static_cast<uint8_t*>(std::aligned_alloc(kAlign, kBlock))); }
#endif

uint64_t random64() {
    std::random_device rd;
    uint64_t r = (uint64_t(rd()) << 32) ^ rd();
    r ^= (uint64_t) std::chrono::steady_clock::now().time_since_epoch().count() * P1;
#ifdef _WIN32
    r ^= (uint64_t) _getpid() * P2;
#else
    r ^= (uint64_t) ::getpid() * P2;
#endif
    return r;
}

// The temporary name beside the destination: hidden (a leading dot, which the server never accepts as a session
// name), unique, and short enough for any filesystem whatever the destination's length.
std::string temp_name(const std::string& leaf) {
    size_t keep = std::min<size_t>(leaf.size(), 64);
    while (keep > 0 && keep < leaf.size() && (uint8_t(leaf[keep]) & 0xC0) == 0x80) --keep;   // whole UTF-8 chars
    char hex[17];
    std::snprintf(hex, sizeof hex, "%016llx", (unsigned long long) random64());
    return "." + leaf.substr(0, keep) + "." + hex + ".tmp";
}

// a test's injected failure at `step`: its errno value (0 = none)
int injected(const std::function<int(const char*)>* fault, const char* step) {
    return fault && *fault ? (*fault)(step) : 0;
}

bool split_path(const std::string& path, std::string& dir, std::string& leaf, std::string& error) {
    const size_t cut = path.find_last_of(
#ifdef _WIN32
        "/\\"
#else
        "/"
#endif
    );
    dir = cut == std::string::npos ? std::string() : path.substr(0, cut == 0 ? 1 : cut);
    leaf = cut == std::string::npos ? path : path.substr(cut + 1);
    if (leaf.empty() || leaf == "." || leaf == "..") { error = "session file: " + path + " names no file"; return false; }
    return true;
}

#ifdef _WIN32
// paths reach the engine as UTF-8 (the server writes its commands as UTF-8); invalid UTF-8 is refused, not replaced
bool wide(const std::string& s, std::wstring& w) {
    w.clear();
    if (s.empty()) return true;
    if (s.size() > (size_t) INT_MAX) return false;
    const int n = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, s.data(), (int) s.size(), nullptr, 0);
    if (n <= 0) return false;
    w.assign((size_t) n, L'\0');
    return MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, s.data(), (int) s.size(), w.data(), n) == n;
}

SessionError win_kind(DWORD code) {
    switch (code) {
        case ERROR_DISK_FULL: case ERROR_HANDLE_DISK_FULL: case ERROR_FILE_TOO_LARGE: return SessionError::storage;
        case ERROR_NOT_ENOUGH_MEMORY: case ERROR_OUTOFMEMORY: return SessionError::memory;
        default: return SessionError::io;
    }
}

std::string last_error(DWORD code = GetLastError()) {
    char msg[256] = {};
    const DWORD n = FormatMessageA(FORMAT_MESSAGE_FROM_SYSTEM | FORMAT_MESSAGE_IGNORE_INSERTS, nullptr, code, 0, msg,
                                   sizeof msg, nullptr);
    std::string s(msg, n);
    while (!s.empty() && (s.back() == '\n' || s.back() == '\r' || s.back() == ' ' || s.back() == '.')) s.pop_back();
    return s.empty() ? "error " + std::to_string(code) : s;
}

class RawFile {
public:
    ~RawFile() { close(); }
    // a new file that did not exist (CREATE_NEW: never an existing file or link)
    bool create_new(const std::wstring& path) {
        h_ = CreateFileW(path.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_NEW,
                         FILE_ATTRIBUTE_NORMAL | FILE_FLAG_SEQUENTIAL_SCAN, nullptr);
        if (h_ == INVALID_HANDLE_VALUE) { { const DWORD c_ = GetLastError(); kind_ = win_kind(c_); error_ = last_error(c_); } return false; }
        return true;
    }
    // a session to restore: the name itself is opened, never what a link or reparse point there points to; it must be
    // an ordinary disk file with one name
    bool open_session(const std::string& path, uint64_t& size) {
        std::wstring w;
        if (!wide(path, w)) { refused_ = true; error_ = "the path is not valid UTF-8"; return false; }
        h_ = CreateFileW(w.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING,
                         FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_SEQUENTIAL_SCAN, nullptr);
        if (h_ == INVALID_HANDLE_VALUE) { { const DWORD c_ = GetLastError(); kind_ = win_kind(c_); error_ = last_error(c_); } return false; }
        BY_HANDLE_FILE_INFORMATION info{};
        if (!GetFileInformationByHandle(h_, &info)) { { const DWORD c_ = GetLastError(); kind_ = win_kind(c_); error_ = last_error(c_); } return false; }
        if (info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT) { refused_ = true; error_ = "a link or reparse point, not a file"; return false; }
        if ((info.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) || GetFileType(h_) != FILE_TYPE_DISK) {
            refused_ = true;
            error_ = "not a regular file";
            return false;
        }
        if (info.nNumberOfLinks != 1) { refused_ = true; error_ = "the file has more than one name (hard link)"; return false; }
        size = (uint64_t(info.nFileSizeHigh) << 32) | info.nFileSizeLow;
        return true;
    }
    // a model input: links followed (model folders are often linked)
    bool open_model(const std::string& path, uint64_t& size) {
        std::wstring w;
        if (!wide(path, w)) { refused_ = true; error_ = "the path is not valid UTF-8"; return false; }
        h_ = CreateFileW(w.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL,
                         nullptr);
        if (h_ == INVALID_HANDLE_VALUE) {
            const DWORD code = GetLastError();
            missing_ = code == ERROR_FILE_NOT_FOUND || code == ERROR_PATH_NOT_FOUND;
            kind_ = win_kind(code); error_ = last_error(code);
            return false;
        }
        LARGE_INTEGER s{};
        if (!GetFileSizeEx(h_, &s)) { { const DWORD c_ = GetLastError(); kind_ = win_kind(c_); error_ = last_error(c_); } return false; }
        size = (uint64_t) s.QuadPart;
        return true;
    }
    bool is_open() const { return h_ != INVALID_HANDLE_VALUE; }
    bool direct() const { return false; }
    bool write_all(const uint8_t* p, size_t n) {
        while (n) {
            DWORD w = 0;
            const DWORD want = (DWORD) std::min<size_t>(n, size_t(1) << 30);
            if (!WriteFile(h_, p, want, &w, nullptr) || w == 0) { { const DWORD c_ = GetLastError(); kind_ = win_kind(c_); error_ = last_error(c_); } return false; }
            p += w; n -= w;
        }
        return true;
    }
    // bytes read, 0 at the end of the file, -1 on error
    long long read_some(uint8_t* p, size_t n) {
        DWORD r = 0;
        if (!ReadFile(h_, p, (DWORD) std::min<size_t>(n, size_t(1) << 30), &r, nullptr)) {
            { const DWORD c_ = GetLastError(); kind_ = win_kind(c_); error_ = last_error(c_); }
            return -1;
        }
        return (long long) r;
    }
    bool read_at(uint64_t offset, uint8_t* p, size_t n) {
        LARGE_INTEGER at{};
        at.QuadPart = (LONGLONG) offset;
        if (!SetFilePointerEx(h_, at, nullptr, FILE_BEGIN)) { { const DWORD c_ = GetLastError(); kind_ = win_kind(c_); error_ = last_error(c_); } return false; }
        while (n) {
            const long long r = read_some(p, n);
            if (r <= 0) { if (r == 0) error_ = "file ends early"; return false; }
            p += r; n -= (size_t) r;
        }
        return true;
    }
    // the v2 streaming read's positioning: reads at `offset` without disturbing the handle's own sequential position
    bool pread_bytes(uint64_t offset, uint8_t* p, size_t n) { return read_at(offset, p, n); }
    bool truncate(uint64_t n) {
        LARGE_INTEGER at{};
        at.QuadPart = (LONGLONG) n;
        if (SetFilePointerEx(h_, at, nullptr, FILE_BEGIN) && SetEndOfFile(h_)) return true;
        { const DWORD c_ = GetLastError(); kind_ = win_kind(c_); error_ = last_error(c_); }
        return false;
    }
    bool sync() {
        if (FlushFileBuffers(h_)) return true;
        { const DWORD c_ = GetLastError(); kind_ = win_kind(c_); error_ = last_error(c_); }
        return false;
    }
    bool close() {
        if (h_ == INVALID_HANDLE_VALUE) return true;
        const bool ok = CloseHandle(h_) != 0;
        if (!ok) { const DWORD c_ = GetLastError(); kind_ = win_kind(c_); error_ = last_error(c_); }
        h_ = INVALID_HANDLE_VALUE;
        return ok;
    }
    const std::string& error() const { return error_; }
    bool missing() const { return missing_; }
    SessionError kind() const { return kind_; }
    // a validation refusal (a link, not a regular file, ...): bad input, not an I/O failure
    bool refused() const { return refused_; }
private:
    HANDLE h_ = INVALID_HANDLE_VALUE;
    SessionError kind_ = SessionError::io;
    bool refused_ = false;
    bool missing_ = false;
    std::string error_;
};

// Where the file is published.  MoveFileExW replaces an existing destination (std::rename does not on Windows); the
// replacement is a rename on one volume, but Windows promises no transaction on every filesystem.  A directory cannot
// be flushed on Windows: the file's data was flushed (FlushFileBuffers) before the move, and the move is write-through.
class Publisher {
public:
    bool open(const std::string& path, std::string& error) {
        std::string dir, leaf;
        if (!split_path(path, dir, leaf, error)) return false;
        std::wstring wdir, wleaf;
        if (!wide(dir, wdir) || !wide(leaf, wleaf)) { error = "session file: the path is not valid UTF-8"; return false; }
        prefix_ = wdir.empty() ? std::wstring() : (wdir.back() == L'\\' || wdir.back() == L'/' ? wdir : wdir + L"\\");
        final_ = prefix_ + wleaf;
        leaf_ = leaf;
        return true;
    }
    bool create_temp(RawFile& f, std::string& error) {
        for (int attempt = 0; attempt < 8; ++attempt) {
            std::wstring name;
            wide(temp_name(leaf_), name);
            temp_ = prefix_ + name;
            if (f.create_new(temp_)) { own_ = true; return true; }
            if (GetLastError() != ERROR_FILE_EXISTS && GetLastError() != ERROR_ALREADY_EXISTS) break;
        }
        error = f.error();
        return false;
    }
    bool publish(std::string& error, bool, SessionStatus& st, const std::function<int(const char*)>* fault) {
        if (const int e = injected(fault, "rename")) { error = std::strerror(e); st.error = errno_kind(e); return false; }
        if (MoveFileExW(temp_.c_str(), final_.c_str(), MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)) {
            own_ = false;
            st.published = true;
            // Windows has no folder flush; a test can still make this step fail after the move
            if (const int e = injected(fault, "dir_flush")) {
                error = std::string("flushing the directory: ") + std::strerror(e);
                st.error = errno_kind(e);
                return false;
            }
            return true;
        }
        const DWORD c = GetLastError();
        st.error = win_kind(c);
        error = last_error(c);
        return false;
    }
    SessionError kind() const { return SessionError::io; }
    ~Publisher() { if (own_) DeleteFileW(temp_.c_str()); }   // only the temporary file this call created
private:
    std::wstring prefix_, final_, temp_;
    std::string leaf_;
    bool own_ = false;
};

bool free_space(const std::string& path, uint64_t& out) {
    std::wstring w;
    if (!wide(session_free_space_dir(path, true), w)) return false;
    ULARGE_INTEGER avail{};
    if (!GetDiskFreeSpaceExW(w.empty() ? nullptr : w.c_str(), &avail, nullptr, nullptr)) return false;
    out = avail.QuadPart;
    return true;
}
#else
#ifndef O_DIRECT
#define O_DIRECT 0            // not offered by this system: buffered I/O
#endif

bool buffered_forced() {
    const char* e = std::getenv("STRATA_SESSION_BUFFERED");
    return e != nullptr && e[0] == '1';
}

static_assert(sizeof(off_t) >= 8, "session files need 64-bit file offsets (large-file support)");

class RawFile {
public:
    ~RawFile() { close(); }
    // a new file that did not exist (O_CREAT|O_EXCL: never an existing file or link), readable by its owner only
    bool create_new(int dirfd, const std::string& name) {
        fd_ = ::openat(dirfd, name.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
        if (fd_ < 0) { kind_ = errno_kind(errno); error_ = std::strerror(errno); return false; }
        try_direct();
        return true;
    }
    // a session to restore: the name itself is opened, never what a symbolic link there points to; it must be a
    // regular file with one name (no hard link to a file elsewhere).  Checked on the opened descriptor.
    bool open_session(const std::string& path, uint64_t& size) {
        fd_ = ::open(path.c_str(), O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC);   // NONBLOCK: a FIFO cannot hang
        if (fd_ < 0) {
            refused_ = errno == ELOOP;
            kind_ = errno_kind(errno);
            error_ = errno == ELOOP ? std::string("a symbolic link, not a file") : std::string(std::strerror(errno));
            return false;
        }
        struct stat st {};
        if (::fstat(fd_, &st) != 0) { error_ = std::strerror(errno); return false; }
        if (!S_ISREG(st.st_mode)) { refused_ = true; error_ = "not a regular file"; return false; }
        if (st.st_nlink != 1) { refused_ = true; error_ = "the file has more than one name (hard link)"; return false; }
        const int fl = ::fcntl(fd_, F_GETFL);
        if (fl >= 0) ::fcntl(fd_, F_SETFL, fl & ~O_NONBLOCK);
        size = (uint64_t) st.st_size;
        try_direct();
        return true;
    }
    // a model input: links followed (model folders are often linked)
    bool open_model(const std::string& path, uint64_t& size) {
        fd_ = ::open(path.c_str(), O_RDONLY | O_CLOEXEC);
        if (fd_ < 0) { missing_ = errno == ENOENT; kind_ = errno_kind(errno); error_ = std::strerror(errno); return false; }
        struct stat st {};
        if (::fstat(fd_, &st) != 0) { error_ = std::strerror(errno); return false; }
        if (!S_ISREG(st.st_mode)) { error_ = "not a regular file"; return false; }
        size = (uint64_t) st.st_size;
        return true;
    }
    bool is_open() const { return fd_ >= 0; }
    bool direct() const { return direct_; }
    bool write_all(const uint8_t* p, size_t n) {
        while (n) {
            const ssize_t w = ::write(fd_, p, n);
            if (w < 0 && errno == EINTR) continue;
            // a filesystem that took O_DIRECT at open but refuses this transfer: nothing was written, go buffered
            if (w < 0 && errno == EINVAL && direct_ && leave_direct()) continue;
            if (w <= 0) { kind_ = w < 0 ? errno_kind(errno) : SessionError::io; error_ = w < 0 ? std::strerror(errno) : "short write"; return false; }
            p += w; n -= (size_t) w;
        }
        return true;
    }
    // bytes read, 0 at the end of the file, -1 on error
    long long read_some(uint8_t* p, size_t n) {
        for (;;) {
            const ssize_t r = ::read(fd_, p, n);
            if (r < 0 && errno == EINTR) continue;
            if (r < 0 && errno == EINVAL && direct_ && leave_direct()) continue;
            if (r < 0) { kind_ = errno_kind(errno); error_ = std::strerror(errno); }
            return (long long) r;
        }
    }
    bool read_at(uint64_t offset, uint8_t* p, size_t n) {
        while (n) {
            const ssize_t r = ::pread(fd_, p, n, (off_t) offset);
            if (r < 0 && errno == EINTR) continue;
            if (r <= 0) { kind_ = r < 0 ? errno_kind(errno) : SessionError::io; error_ = r < 0 ? std::strerror(errno) : "file ends early"; return false; }
            p += r; n -= (size_t) r; offset += (uint64_t) r;
        }
        return true;
    }
    // the v2 streaming read's positioning: reads at `offset` and leaves the descriptor's own sequential position
    // alone.  A filesystem that refuses this aligned transfer (EINVAL) drops direct I/O and continues buffered.
    bool pread_bytes(uint64_t offset, uint8_t* p, size_t n) {
        while (n) {
            const ssize_t r = ::pread(fd_, p, n, (off_t) offset);
            if (r < 0 && errno == EINTR) continue;
            if (r < 0 && errno == EINVAL && direct_ && leave_direct()) continue;
            if (r <= 0) { kind_ = r < 0 ? errno_kind(errno) : SessionError::io; error_ = r < 0 ? std::strerror(errno) : "file ends early"; return false; }
            p += r; n -= (size_t) r; offset += (uint64_t) r;
        }
        return true;
    }
    bool truncate(uint64_t n) {
        if (::ftruncate(fd_, (off_t) n) == 0) return true;
        kind_ = errno_kind(errno);
        error_ = std::strerror(errno);
        return false;
    }
    bool sync() {
        if (::fsync(fd_) == 0) return true;
        kind_ = errno_kind(errno);
        error_ = std::strerror(errno);
        return false;
    }
    bool close() {
        if (fd_ < 0) return true;
        const bool ok = ::close(fd_) == 0;
        if (!ok) { kind_ = errno_kind(errno); error_ = std::strerror(errno); }
        fd_ = -1;
        return ok;
    }
    const std::string& error() const { return error_; }
    bool missing() const { return missing_; }
    SessionError kind() const { return kind_; }
    // a validation refusal (a link, not a regular file, ...): bad input, not an I/O failure
    bool refused() const { return refused_; }
private:
    // O_DIRECT is switched on the open descriptor: a filesystem that refuses it (EINVAL, e.g. tmpfs) keeps buffered
    // I/O, and the file is never opened twice
    void try_direct() {
        if (O_DIRECT == 0 || buffered_forced()) return;
        const int fl = ::fcntl(fd_, F_GETFL);
        direct_ = fl >= 0 && ::fcntl(fd_, F_SETFL, fl | O_DIRECT) == 0;
    }
    bool leave_direct() {
        const int fl = ::fcntl(fd_, F_GETFL);
        if (fl < 0 || ::fcntl(fd_, F_SETFL, fl & ~O_DIRECT) != 0) return false;
        direct_ = false;
        return true;
    }
    int fd_ = -1;
    bool direct_ = false, missing_ = false, refused_ = false;
    SessionError kind_ = SessionError::io;
    std::string error_;
};

// The destination directory is opened once; the temporary file is created in it exclusively, renamed over the
// final name in the same directory (a symbolic link at the final name is replaced, never written through), and the
// directory is flushed after the rename so the new name survives a power loss.
class Publisher {
public:
    bool open(const std::string& path, std::string& error) {
        std::string dir;
        if (!split_path(path, dir, leaf_, error)) return false;
        dirfd_ = ::open(dir.empty() ? "." : dir.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC);
        if (dirfd_ < 0) {
            kind_ = errno_kind(errno);
            error = "session file: cannot open the directory of " + path + ": " + std::strerror(errno);
            return false;
        }
        return true;
    }
    bool create_temp(RawFile& f, std::string& error) {
        for (int attempt = 0; attempt < 8; ++attempt) {
            temp_ = temp_name(leaf_);
            if (f.create_new(dirfd_, temp_)) { own_ = true; return true; }
            if (errno != EEXIST) break;
        }
        error = f.error();
        return false;
    }
    bool publish(std::string& error, bool durable, SessionStatus& st, const std::function<int(const char*)>* fault) {
        int e = injected(fault, "rename");
        if (e || ::renameat(dirfd_, temp_.c_str(), dirfd_, leaf_.c_str()) != 0) {
            if (!e) e = errno;
            error = std::strerror(e);
            st.error = errno_kind(e);
            return false;
        }
        own_ = false;
        st.published = true;   // the new file is at its name now: a failure below cannot bring the old one back
        if (!durable) return true;
        e = injected(fault, "dir_flush");
        if (!e && ::fsync(dirfd_) != 0) e = errno;
        if (e == EINVAL) { st.dir_flush_unsupported = true; return true; }   // this filesystem cannot flush a folder
        if (e) {
            error = std::string("flushing the directory: ") + std::strerror(e);
            st.error = errno_kind(e);
            return false;
        }
        return true;
    }
    SessionError kind() const { return kind_; }
    ~Publisher() {
        if (own_) ::unlinkat(dirfd_, temp_.c_str(), 0);   // only the temporary file this call created
        if (dirfd_ >= 0) ::close(dirfd_);
    }
private:
    int dirfd_ = -1;
    std::string leaf_, temp_;
    bool own_ = false;
    SessionError kind_ = SessionError::io;
};

bool free_space(const std::string& path, uint64_t& out) {
    struct statvfs s {};
    if (::statvfs(session_free_space_dir(path, false).c_str(), &s) != 0) return false;
    out = (uint64_t) s.f_bavail * (uint64_t) s.f_frsize;
    return true;
}
#endif

class FileSink {
public:
    explicit FileSink(RawFile& f) : buf_(aligned_block()), f_(f) {}
    bool ok() const { return buf_ != nullptr; }
    std::string error() const {
        if (!buf_) return "out of memory for the write buffer";
        return fault_ ? fault_msg_ : f_.error();
    }
    SessionError kind() const { return buf_ ? (fault_ ? fault_kind_ : f_.kind()) : SessionError::memory; }
    // room in the block for in-place production (avoids a staging copy)
    uint8_t* span(size_t& room) { room = kBlock - used_; return buf_.get() + used_; }
    bool commit(size_t n) { used_ += n; total_ += n; return used_ < kBlock || drain(); }
    bool write(const void* p, size_t n) {
        const uint8_t* s = static_cast<const uint8_t*>(p);
        while (n) {
            size_t room = 0;
            uint8_t* d = span(room);
            const size_t c = std::min(room, n);
            std::memcpy(d, s, c);
            if (!commit(c)) return false;
            s += c; n -= c;
        }
        return true;
    }
    // last block padded to the alignment for direct I/O, then the file cut back to its exact size and flushed
    bool finish(bool durable) {
        if (!f_.is_open()) return false;
        if (fault_) { f_.close(); return false; }
        const size_t padded = f_.direct() ? (used_ + kAlign - 1) / kAlign * kAlign : used_;
        std::memset(buf_.get() + used_, 0, padded - used_);
        bool ok = f_.write_all(buf_.get(), padded);
        if (ok) report_mode();
        if (ok && used_ && progress) (*progress)(total_, expected);   // the last, partial block
        ok = ok && (padded == used_ || f_.truncate(total_));
        if (ok && durable && phase) phase("flush", total_);
        if (ok && durable && flush_fault) { fail_with(flush_fault); ok = false; }
        ok = ok && (!durable || f_.sync());
        ok = f_.close() && ok;
        return ok;
    }
    std::optional<Ticker> progress;
    SessionPhase phase;
    std::function<void(bool)> mode;   // SessionWriteOptions::mode, reported at the first block that reaches the OS
    uint64_t expected = 0;
    int write_fault = 0, flush_fault = 0;   // tests: errno values injected at the first block write / the file flush
    bool direct() const { return f_.direct(); }
private:
    void fail_with(int e) { fault_ = true; fault_kind_ = errno_kind(e); fault_msg_ = std::strerror(e); }
    // the mode is reported once, and again only if it changes: leave_direct() can flip the transfer mid-write
    void report_mode() {
        if (!mode) return;
        const bool direct = f_.direct();
        if (mode_ && *mode_ == direct) return;
        mode_ = direct;
        mode(direct);
    }
    bool drain() {
        if (write_fault) { fail_with(write_fault); return false; }
        if (!f_.write_all(buf_.get(), used_)) return false;
        used_ = 0;
        report_mode();
        if (progress) (*progress)(total_, expected);
        return true;
    }
    Aligned buf_;
    RawFile& f_;
    bool fault_ = false;
    std::optional<bool> mode_;
    SessionError fault_kind_ = SessionError::io;
    std::string fault_msg_;
    size_t used_ = 0;
    uint64_t total_ = 0;
};

class FileSource {
public:
    explicit FileSource(RawFile& f) : buf_(aligned_block()), f_(f) {}
    bool ok() const { return buf_ != nullptr; }
    std::string error() const { return buf_ ? f_.error() : std::string("out of memory for the read buffer"); }
    SessionError kind() const { return buf_ ? f_.kind() : SessionError::memory; }
    bool read(void* p, size_t n) {
        uint8_t* d = static_cast<uint8_t*>(p);
        while (n) {
            if (at_ == have_ && !fill()) return false;
            const size_t c = std::min(n, have_ - at_);
            std::memcpy(d, buf_.get() + at_, c);
            at_ += c; d += c; n -= c;
        }
        return true;
    }
    std::optional<Ticker> progress;
    uint64_t expected = 0;
private:
    bool fill() {
        at_ = have_ = 0;
        while (have_ < kBlock) {
            const long long r = f_.read_some(buf_.get() + have_, kBlock - have_);
            if (r < 0) return false;
            if (r == 0) break;
            have_ += (size_t) r;
            if (f_.direct() && have_ % kAlign) break;   // short aligned read: end of file
        }
        const uint64_t before = total_;
        total_ += have_;
        if (progress && total_ > before) (*progress)(total_, expected);
        return have_ > 0;
    }
    Aligned buf_;
    RawFile& f_;
    size_t at_ = 0, have_ = 0;
    uint64_t total_ = 0;
};

// ---- serialization: the same walk counts (f == nullptr) and writes
struct Out {
    FileSink* f = nullptr;
    SessionHasher* hash = nullptr;
    SessionHasher* meta = nullptr;   // v2: a second hash over the manifest bytes only (v1 leaves it null)
    uint64_t n = 0;
    bool ok = true, overflow = false;
    void count(uint64_t c) {
        if (c > UINT64_MAX - n) { overflow = true; ok = false; return; }
        n += c;
    }
    void raw(const void* p, size_t c) {
        count(c);
        if (!f || !ok || !c) return;
        if (hash) hash->update(p, c);
        if (meta) meta->update(p, c);
        ok = f->write(p, c);
    }
    void u64(uint64_t v) { raw(&v, 8); }
    void i64(int64_t v) { raw(&v, 8); }
    template<class T> void vec(const std::vector<T>& v) { u64(v.size()); raw(v.data(), v.size() * sizeof(T)); }
    void buffer(const ConversationBuffer& b) {
        u64(b.size());
        b.visit(0, b.size(), [&](const uint8_t* p, size_t c, size_t) { raw(p, c); return ok; });
    }
    // a K/V part pulled from its owner straight into the file block (sizing pass: counted, never read)
    void source(const SessionKvSource& s, size_t part) {
        const size_t size = s.sizes[part];
        u64(size);
        if (!f) { count(size); return; }
        for (size_t at = 0; at < size && ok;) {
            size_t room = 0;
            uint8_t* d = f->span(room);
            const size_t c = std::min(room, size - at);
            if (!s.read || !s.read(part, at, d, c)) {
                ok = false;
                read_failed = true;
                if (s.error) read_error = s.error();
                return;
            }
            if (hash) hash->update(d, c);
            count(c);
            ok = ok && f->commit(c);
            at += c;
        }
    }
    bool read_failed = false;
    std::string read_error;
};

void put_kv_header(Out& o, int format, int64_t cells, int64_t heads, int64_t head_dim, int64_t page_size,
                   int64_t pooled_rows, int64_t idx_dim) {
    for (int64_t v : {int64_t(format), cells, heads, head_dim, page_size, pooled_rows, idx_dim}) o.i64(v);
}

void put_checkpoint(Out& o, const ConversationCheckpoint& c) {
    o.vec(c.ids);
    o.u64(c.imgs.size());
    for (const auto& k : c.imgs) { o.i64(k.start); o.u64(k.hash); }
    o.vec(c.gdn); o.vec(c.ple); o.vec(c.tails); o.vec(c.dead); o.vec(c.block_pos);
    o.u64(c.used);
}

void put_payload(Out& o, const SavedConversation& s, const std::vector<SessionKvSource>* sources = nullptr) {
    for (int64_t g : s.geometry) o.i64(g);
    o.i64(s.layer_lo); o.i64(s.layer_hi); o.u64(s.cvec ? 1 : 0);
    put_checkpoint(o, s.live);
    o.u64(s.checkpoints.size());
    for (const auto& c : s.checkpoints) put_checkpoint(o, c);
    if (sources) {
        o.u64(sources->size());
        for (const auto& k : *sources) {
            put_kv_header(o, k.format, k.cells, k.heads, k.head_dim, k.page_size, k.pooled_rows, k.idx_dim);
            for (size_t part = 0; part < 5; ++part) o.source(k, part);
        }
        return;
    }
    o.u64(s.kv.size());
    for (const auto& k : s.kv) {
        put_kv_header(o, k.format, k.cells, k.heads, k.head_dim, k.page_size, k.pooled_rows, k.idx_dim);
        o.buffer(k.k); o.buffer(k.v); o.buffer(k.k_scale); o.buffer(k.v_scale); o.buffer(k.pooled);
    }
}

// ---- parsing: every count is checked against the bytes left and the caller's limits before anything is allocated
struct In {
    FileSource* f;
    SessionHasher hash;
    uint64_t left;
    std::string& error;
    const SessionReadLimits& limits;
    SessionError kind = SessionError::invalid;
    size_t kv_layer = 0;
    bool fail(const std::string& m) { if (error.empty()) error = "session file: " + m; return false; }
    bool raw(void* p, size_t c) {
        if (c > left) return fail("payload ends early");
        if (c && !f->read(p, c)) { kind = f->kind(); return fail("read error: " + f->error()); }
        hash.update(p, c);
        left -= c;
        return true;
    }
    bool u64(uint64_t& v) { return raw(&v, 8); }
    bool i64(int64_t& v) { return raw(&v, 8); }
    // `elem`: the fewest file bytes one element takes; `limit`: the most elements the caller accepts
    bool count(uint64_t& n, size_t elem, uint64_t limit = UINT64_MAX) {
        if (!u64(n)) return false;
        if (elem && n > left / elem) return fail("element count exceeds the payload");
        if (n > limit) return fail("element count " + std::to_string(n) + " exceeds this engine's limit " +
                                   std::to_string(limit));
        if (n > (uint64_t) std::numeric_limits<size_t>::max() / (elem ? elem : 1)) return fail("element count too large");
        return true;
    }
    template<class T> bool vec(std::vector<T>& v, uint64_t limit = UINT64_MAX) {
        uint64_t n = 0;
        if (!count(n, sizeof(T), limit)) return false;
        v.resize((size_t) n);
        return raw(v.data(), (size_t) n * sizeof(T));
    }
    bool buffer(ConversationBuffer& b, size_t part) {
        uint64_t n = 0;
        const uint64_t limit = kv_layer < limits.max_kv_bytes.size() ? limits.max_kv_bytes[kv_layer][part] : UINT64_MAX;
        if (!count(n, 1, limit)) return false;
        b = {};
        b.resize((size_t) n);
        return b.visit(0, b.size(), [&](uint8_t* p, size_t c, size_t) { return raw(p, c); });
    }
};

bool get_checkpoint(In& in, ConversationCheckpoint& c) {
    if (!in.vec(c.ids, in.limits.max_tokens)) return false;
    uint64_t n = 0;
    if (!in.count(n, 16, in.limits.max_tokens)) return false;
    c.imgs.resize((size_t) n);
    for (auto& k : c.imgs) if (!in.i64(k.start) || !in.u64(k.hash)) return false;
    const auto& m = in.limits.max_state_bytes;   // byte arrays: element and byte counts are the same
    return in.vec(c.gdn, m[0]) && in.vec(c.ple, m[1]) && in.vec(c.tails, m[2]) && in.vec(c.dead, m[3]) &&
           in.vec(c.block_pos, m[4]) && in.u64(c.used);
}

bool get_payload(In& in, SavedConversation& s) {
    for (auto& g : s.geometry) if (!in.i64(g)) return false;
    // checked before any state array is read: a file of another geometry allocates nothing
    if (in.limits.geometry && *in.limits.geometry != s.geometry)
        return in.fail("saved with another model geometry than this runtime's");
    uint64_t cvec = 0;
    if (!in.i64(s.layer_lo) || !in.i64(s.layer_hi) || !in.u64(cvec)) return false;
    if (in.limits.layer_range &&
        (in.limits.layer_range->first != s.layer_lo || in.limits.layer_range->second != s.layer_hi))
        return in.fail("saved with another layer range than this runtime's");
    if (cvec > 1) return in.fail("invalid cvec flag");
    s.cvec = cvec == 1;
    if (!get_checkpoint(in, s.live)) return false;
    uint64_t n = 0;
    // a checkpoint is at least 8 counts + used = 72 bytes; a K/V layer at least 7 + 5 = 96
    if (!in.count(n, 72, in.limits.max_checkpoints)) return false;
    s.checkpoints.resize((size_t) n);
    for (auto& c : s.checkpoints) if (!get_checkpoint(in, c)) return false;
    if (!in.count(n, 96, in.limits.max_kv_layers)) return false;
    s.kv.resize((size_t) n);
    for (auto& k : s.kv) {
        in.kv_layer = (size_t) (&k - s.kv.data());
        int64_t format = 0;
        if (!in.i64(format) || !in.i64(k.cells) || !in.i64(k.heads) || !in.i64(k.head_dim) ||
            !in.i64(k.page_size) || !in.i64(k.pooled_rows) || !in.i64(k.idx_dim)) return false;
        if (format < INT32_MIN || format > INT32_MAX) return in.fail("invalid K/V format");
        k.format = (int) format;
        if (!in.buffer(k.k, 0) || !in.buffer(k.v, 1) || !in.buffer(k.k_scale, 2) || !in.buffer(k.v_scale, 3) ||
            !in.buffer(k.pooled, 4)) return false;
    }
    return true;
}

bool has_stage_parts(const SavedConversation& s) {
    if (!s.live.stage_parts.empty()) return true;
    for (const auto& c : s.checkpoints) if (!c.stage_parts.empty()) return true;
    return false;
}

// header v1, little-endian: magic[8] version:u32 header_size:u32 model:u64 config:u64 payload:u64 reserved:u64[2]
// header_hash:u64 (session_hash64 of bytes 0..55, seed 0)
void header_bytes(uint8_t* h, const SessionFileIdentity& id, uint64_t payload) {
    std::memset(h, 0, kHeader);
    std::memcpy(h, kMagic, 8);
    const uint32_t version = kVersion, size = kHeader;
    std::memcpy(h + 8, &version, 4);
    std::memcpy(h + 12, &size, 4);
    std::memcpy(h + 16, &id.model, 8);
    std::memcpy(h + 24, &id.config, 8);
    std::memcpy(h + 32, &payload, 8);
    const uint64_t hh = session_hash64(h, 56, 0);
    std::memcpy(h + 56, &hh, 8);
}
} // namespace

SessionHasher::SessionHasher(uint64_t seed)
    : lane_{seed + P1 + P2, seed + P2, seed, seed - P1}, pending_{} {}

void SessionHasher::block(const uint8_t* p) {
    for (int i = 0; i < 4; ++i) lane_[i] = round1(lane_[i], load64(p + 8 * i));
}

void SessionHasher::update(const void* data, size_t n) {
    const uint8_t* p = static_cast<const uint8_t*>(data);
    total_ += n;
    if (npending_) {
        const size_t take = std::min(n, sizeof pending_ - npending_);
        std::memcpy(pending_ + npending_, p, take);
        npending_ += take; p += take; n -= take;
        if (npending_ < sizeof pending_) return;
        block(pending_);
        npending_ = 0;
    }
    for (; n >= 32; p += 32, n -= 32) block(p);
    if (n) std::memcpy(pending_, p, n);
    npending_ = n;
}

uint64_t SessionHasher::digest() const {
    uint64_t h = rotl(lane_[0], 1) + rotl(lane_[1], 7) + rotl(lane_[2], 12) + rotl(lane_[3], 18);
    for (uint64_t l : lane_) { h ^= round1(0, l); h = h * P1 + P4; }
    h += total_;
    size_t i = 0;
    for (; i + 8 <= npending_; i += 8) { h ^= round1(0, load64(pending_ + i)); h = rotl(h, 27) * P1 + P4; }
    for (; i < npending_; ++i) { h ^= pending_[i] * P5; h = rotl(h, 11) * P1; }
    h ^= h >> 33; h *= P2; h ^= h >> 29; h *= P3; h ^= h >> 32;
    return h;
}

uint64_t session_hash64(const void* data, size_t n, uint64_t seed) {
    SessionHasher h(seed);
    h.update(data, n);
    return h.digest();
}

void SessionIdentityBuilder::field(const std::string& tag, uint8_t type, const void* data, uint64_t n) {
    const uint64_t tn = tag.size();
    hash_.update(&tn, 8);
    hash_.update(tag.data(), tag.size());
    hash_.update(&type, 1);
    hash_.update(&n, 8);
    if (n) hash_.update(data, (size_t) n);
}
void SessionIdentityBuilder::str(const std::string& tag, const std::string& v) { field(tag, 's', v.data(), v.size()); }
void SessionIdentityBuilder::i64(const std::string& tag, int64_t v) { field(tag, 'i', &v, 8); }
void SessionIdentityBuilder::u64(const std::string& tag, uint64_t v) { field(tag, 'u', &v, 8); }
void SessionIdentityBuilder::f64(const std::string& tag, double v) {
    uint64_t bits = 0;
    std::memcpy(&bits, &v, 8);
    field(tag, 'f', &bits, 8);
}
void SessionIdentityBuilder::bytes(const std::string& tag, const void* data, size_t n) { field(tag, 'b', data, n); }

uint64_t session_config_fingerprint(const SessionConfig& c) {
    SessionIdentityBuilder b(0x434f4e4649473031ull);   // "CONFIG01"
    b.str("engine", c.engine_version);
    b.str("backend", c.backend);
    b.str("kv", c.kv);
    b.i64("max_context", c.max_context);
    b.i64("kv_resident", c.kv_resident);
    b.i64("mtp_window", c.mtp_window);
    b.i64("kv_rot", c.kv_rot ? 1 : 0);
    b.i64("rope.type", c.rope.type);
    b.f64("rope.freq_base", c.rope.freq_base);
    b.f64("rope.factor", c.rope.factor);
    b.f64("rope.freq_scale", c.rope.freq_scale);
    b.f64("rope.orig_ctx", c.rope.orig_ctx);
    b.f64("rope.ext_factor", c.rope.ext_factor);
    b.f64("rope.attn_factor", c.rope.attn_factor);
    b.f64("rope.beta_fast", c.rope.beta_fast);
    b.f64("rope.beta_slow", c.rope.beta_slow);
    b.u64("cvec", c.cvec);
    b.u64("switches", c.switches.size());
    for (const auto& [name, value] : c.switches) b.i64("switch." + name, value);
    return b.digest();
}

const char* session_error_name(SessionError e) {
    switch (e) {
        case SessionError::none: return "none";
        case SessionError::invalid: return "invalid";
        case SessionError::storage: return "storage";
        case SessionError::memory: return "memory";
        case SessionError::io: return "io";
    }
    return "io";
}

std::string session_free_space_dir(const std::string& path, bool windows_rules) {
    if (!windows_rules) {
        const size_t cut = path.find_last_of('/');
        if (cut == std::string::npos) return ".";
        return path.substr(0, cut == 0 ? 1 : cut);
    }
    std::string p = path;
    for (char& c : p) if (c == '/') c = '\\';
    const size_t cut = p.find_last_of('\\');
    if (cut == std::string::npos) {
        // "C:chat.bin" (drive-relative): the drive root answers for the same volume
        if (p.size() >= 2 && p[1] == ':') return p.substr(0, 2) + "\\";
        return std::string();   // the current folder
    }
    return p.substr(0, cut + 1);   // with its trailing backslash: "\\server\share\dir\", "C:\"
}

std::vector<SessionModelFile> session_model_inputs(const SessionInputs& in) {
    std::vector<SessionModelFile> f;
    for (size_t i = 0; i < in.native_shards.size(); ++i) f.push_back({"native shard " + std::to_string(i), in.native_shards[i]});
    for (size_t i = 0; i < in.native_dense.size(); ++i) f.push_back({"native dense " + std::to_string(i), in.native_dense[i]});
    for (size_t i = 0; i < in.native_head.size(); ++i) f.push_back({"native head " + std::to_string(i), in.native_head[i]});
    if (!in.embedding.empty()) f.push_back({"embedding", in.embedding});
    if (!in.ple.empty()) f.push_back({"ple", in.ple});
    if (!in.mtp.empty()) f.push_back({"mtp", in.mtp});
    if (!in.pack.empty()) f.push_back({"pack native_experts.txt", in.pack + "/native_experts.txt", true});
    for (const auto& [role, path] : in.experts) f.push_back({role, path});
    return f;
}

bool session_model_fingerprint(const std::vector<SessionModelFile>& files, uint64_t& fingerprint, std::string& error,
                               const std::function<void()>& beat) {
    SessionIdentityBuilder h(0x5354524154414d44ull);
    std::vector<uint8_t> buf(1u << 20);
    // a file in several roles is read once; its sample enters the fingerprint under every role
    std::unordered_map<std::string, std::pair<bool, uint64_t>> seen;   // path -> (present, sample digest)
    h.u64("files", files.size());
    for (const auto& file : files) {
        auto it = seen.find(file.path);
        if (it == seen.end()) {
            RawFile f;
            uint64_t size = 0;
            std::pair<bool, uint64_t> d{false, 0};
            if (!f.open_model(file.path, size)) {
                if (!(file.optional && f.missing())) {
                    error = "session fingerprint: " + file.role + " " + file.path + ": " + f.error();
                    return false;
                }
            } else {
                SessionIdentityBuilder one(0x46494c4553414d50ull);   // "FILESAMP"
                one.u64("size", size);
                const uint64_t head = std::min<uint64_t>(size, buf.size());
                if (!f.read_at(0, buf.data(), (size_t) head)) {
                    error = "session fingerprint: reading " + file.path + ": " + f.error();
                    return false;
                }
                one.bytes("head", buf.data(), (size_t) head);
                if (size > buf.size()) {
                    const uint64_t tail = std::min<uint64_t>(size - head, buf.size());
                    if (!f.read_at(size - tail, buf.data(), (size_t) tail)) {
                        error = "session fingerprint: reading " + file.path + ": " + f.error();
                        return false;
                    }
                    one.bytes("tail", buf.data(), (size_t) tail);
                }
                d = {true, one.digest()};
            }
            it = seen.emplace(file.path, d).first;
            if (beat) beat();
        }
        if (!it->second.first) {
            if (!file.optional) { error = "session fingerprint: " + file.role + " " + file.path + " is missing"; return false; }
            h.str("absent", file.role);
            continue;
        }
        h.str("role", file.role);
        h.u64("sample", it->second.second);
    }
    fingerprint = h.digest();
    return true;
}

namespace {
uint64_t sat_add(uint64_t a, uint64_t b) { return a > UINT64_MAX - b ? UINT64_MAX : a + b; }
uint64_t sat_mul(uint64_t a, uint64_t b) { return a && b > UINT64_MAX / a ? UINT64_MAX : a * b; }
} // namespace

uint64_t session_read_max_file_bytes(const SessionReadLimits& l) {
    const uint64_t open = UINT64_MAX;
    if (l.max_tokens == open || l.max_checkpoints == open || l.max_kv_layers == open) return l.max_file_bytes;
    for (uint64_t b : l.max_state_bytes) if (b == open) return l.max_file_bytes;
    if (l.max_kv_bytes.size() < l.max_kv_layers) return l.max_file_bytes;
    // one checkpoint: ids (8 + 4 per token), images (8 + 16 per image), five state arrays (8 + bytes), used (8)
    uint64_t cp = sat_add(16, sat_mul(l.max_tokens, 4 + 16));
    for (uint64_t b : l.max_state_bytes) cp = sat_add(cp, sat_add(8, b));
    cp = sat_add(cp, 8);
    uint64_t n = kHeader + kTrailer + 18 * 8 + 3 * 8;             // geometry, layer range, cvec
    n = sat_add(n, cp);                                            // live
    n = sat_add(n, sat_add(8, sat_mul(l.max_checkpoints, cp)));    // checkpoints
    n = sat_add(n, 8);
    for (uint64_t i = 0; i < l.max_kv_layers; ++i) {
        n = sat_add(n, 7 * 8);
        for (uint64_t b : l.max_kv_bytes[(size_t) i]) n = sat_add(n, sat_add(8, b));
    }
    return std::min(n, l.max_file_bytes);
}

uint64_t session_read_peak_bytes(uint64_t file_bytes) {
    const uint64_t segments = file_bytes / kBlock + 1;
    // the parsed image (about the payload), the read buffer, a page per buffer segment, the small vectors
    return sat_add(sat_add(sat_add(file_bytes, kBlock), sat_mul(segments, 4096)), uint64_t(1) << 20);
}

namespace {
bool write_impl(const std::string& path, const SavedConversation& image, const std::vector<SessionKvSource>* sources,
                const SessionFileIdentity& id, size_t& bytes, std::string& error, const SessionWriteOptions& opt,
                SessionStatus& st) {
    st = {};
    st.error = SessionError::invalid;   // refusals before any disk access
    if (has_stage_parts(image)) { error = "session file: layer-split state cannot be saved"; return false; }
    if (sources && !image.kv.empty()) { error = "session file: K/V given both in the image and as sources"; return false; }
    Out sizing;
    put_payload(sizing, image, sources);
    if (sizing.overflow || sizing.n > UINT64_MAX - kHeader - kTrailer ||
        sizing.n + kHeader + kTrailer > (uint64_t) std::numeric_limits<size_t>::max()) {
        error = "session file: the state is too large for this build";
        return false;
    }
    const uint64_t payload = sizing.n, total = kHeader + payload + kTrailer;
    st.error = SessionError::io;
    if (opt.min_free_bytes) {
        uint64_t avail = 0;
        if (!free_space(path, avail)) { error = "session file: cannot read the free disk space for " + path; return false; }
        if (avail < total || avail - total < opt.min_free_bytes) {
            st.error = SessionError::storage;
            error = "session file: not enough disk space (" + std::to_string(total >> 20) + " MiB plus a reserve of " +
                    std::to_string(opt.min_free_bytes >> 20) + " MiB needed, " + std::to_string(avail >> 20) +
                    " MiB free)";
            return false;
        }
    }
    Publisher pub;
    if (!pub.open(path, error)) { st.error = pub.kind(); return false; }
    RawFile raw;
    if (!pub.create_temp(raw, error)) {
        st.error = raw.kind();
        error = "session file: cannot create a temporary file beside " + path + ": " + error;
        return false;
    }
    FileSink f(raw);
    if (!f.ok()) { st.error = SessionError::memory; error = "session file: " + f.error(); return false; }
    if (opt.progress) f.progress.emplace(opt.progress);
    f.phase = opt.phase;
    f.mode = opt.mode;
    f.expected = total;
    if (f.progress) (*f.progress)(0, total);   // the temporary file exists: the transfer starts
    const std::function<int(const char*)>* fault = opt.fault ? &opt.fault : nullptr;
    f.write_fault = injected(fault, "write");
    f.flush_fault = injected(fault, "file_flush");
    uint8_t h[kHeader];
    header_bytes(h, id, payload);
    bool ok = f.write(h, kHeader);
    SessionHasher hash(0);
    Out out;
    out.f = &f;
    out.hash = &hash;
    if (ok) put_payload(out, image, sources);
    ok = ok && out.ok && out.n == payload;
    if (ok) {
        const uint64_t ph = hash.digest();
        ok = f.write(&ph, 8) && f.write(kEnd, 8);
    }
    ok = f.finish(opt.durable) && ok;
    std::string why;
    if (!ok) {
        why = out.read_failed ? "K/V source read" + (out.read_error.empty() ? std::string() : ": " + out.read_error)
                              : f.error();
        st.error = out.read_failed ? SessionError::io : f.kind();
    } else {
        if (opt.phase) opt.phase("publish", total);
        if (pub.publish(why, opt.durable, st, fault)) {
            st.error = SessionError::none;
            bytes = (size_t) total;
            return true;
        }
    }
    error = "session file: writing " + path + " failed" + (why.empty() ? std::string() : ": " + why);
    if (st.published) error += " (the new file has already replaced the old one; its folder entry may not survive a power loss)";
    return false;   // Publisher removes this call's temporary file, and only that
}

bool write_guarded(const std::string& path, const SavedConversation& image, const std::vector<SessionKvSource>* sources,
                   const SessionFileIdentity& id, size_t& bytes, std::string& error, const SessionWriteOptions& opt,
                   SessionStatus* status) {
    SessionStatus st;
    bool ok = false;
    try {
        ok = write_impl(path, image, sources, id, bytes, error, opt, st);
    } catch (const std::bad_alloc&) {
        error = "session file: out of memory while writing " + path;
        st.error = SessionError::memory;
        ok = false;
    }
    if (status) *status = st;
    return ok;
}
} // namespace

std::vector<ConversationCheckpoint> session_checkpoints_to_save(const std::vector<ConversationCheckpoint>& chain) {
    const ConversationCheckpoint* deepest = session_deepest_checkpoint(chain);
    if (!deepest) return {};
    return {*deepest};
}

const ConversationCheckpoint* session_deepest_checkpoint(const std::vector<ConversationCheckpoint>& chain) {
    // A pinned shared prefix (pin=N) is the resume point the next queries want, so with one in the chain the file
    // keeps the deepest PINNED checkpoint, not a deeper periodic one a long private suffix made past it.  No pin:
    // the deepest, as before.
    const ConversationCheckpoint* deepest = nullptr;
    bool any_pinned = false;
    for (const auto& c : chain) any_pinned = any_pinned || c.pinned;
    for (const auto& c : chain)
        if ((!any_pinned || c.pinned) && (!deepest || c.ids.size() > deepest->ids.size())) deepest = &c;
    return deepest;
}

uint64_t session_save_peak_bytes(const ConversationCheckpoint* deepest, const SessionSaveLive& live) {
    uint64_t n = sat_add(kBlock, uint64_t(1) << 20);                          // write buffer, small vectors
    if (deepest) n = sat_add(n, sat_mul(2, deepest->bytes()));                // the list and the image's copy
    n = sat_add(n, live.state_bytes);                                         // the live running state
    n = sat_add(n, sat_mul(live.tokens, sizeof(int32_t)));                    // its token list
    n = sat_add(n, sat_mul(live.images, sizeof(ConversationImageKey)));       // its images
    n = sat_add(n, sat_mul(live.kv_layers, sizeof(SessionKvSource) + 256));   // the K/V source directory
    return n;
}

bool session_save_checkpoints(const std::vector<ConversationCheckpoint>& chain, const SessionSaveLive& live,
                              const std::function<bool(uint64_t need_bytes, std::string& why)>& admit,
                              std::vector<ConversationCheckpoint>& out, std::string& why) {
    out.clear();
    const ConversationCheckpoint* deepest = session_deepest_checkpoint(chain);
    if (admit && !admit(session_save_peak_bytes(deepest, live), why)) return false;
    if (deepest) out.push_back(*deepest);
    return true;
}

int64_t session_phase_limit_s(uint64_t bytes) {
    return std::min<int64_t>(3600, 60 + (int64_t) std::min<uint64_t>(bytes >> 22, 3600));
}

bool session_file_write(const std::string& path, const SavedConversation& meta, const std::vector<SessionKvSource>& kv,
                        const SessionFileIdentity& id, size_t& bytes, std::string& error,
                        const SessionWriteOptions& options, SessionStatus* status) {
    return write_guarded(path, meta, &kv, id, bytes, error, options, status);
}

bool session_file_write(const std::string& path, const SavedConversation& image, const SessionFileIdentity& id,
                        size_t& bytes, std::string& error, const SessionWriteOptions& options, SessionStatus* status) {
    return write_guarded(path, image, nullptr, id, bytes, error, options, status);
}

namespace {
bool read_impl(const std::string& path, const SessionFileIdentity& id, SavedConversation& image,
               size_t& bytes, std::string& error, const SessionReadLimits& limits, SessionStatus& st) {
    error.clear();
    st = {};
    st.error = SessionError::invalid;   // every refusal below, unless the step says otherwise
    RawFile raw;
    uint64_t size = 0;
    if (!raw.open_session(path, size)) {
        st.error = raw.refused() ? SessionError::invalid : raw.kind();
        error = "session file: " + path + ": " + raw.error();
        return false;
    }
    if (size < kHeader + kTrailer) { error = "session file: size " + std::to_string(size) + " is below the minimum"; return false; }
    const uint64_t max_size = session_read_max_file_bytes(limits);
    if (size > max_size || size > (uint64_t) std::numeric_limits<size_t>::max()) {
        error = "session file: size " + std::to_string(size) + " exceeds this engine's limit" +
                (max_size == UINT64_MAX ? std::string() : " of " + std::to_string(max_size));
        return false;
    }
    FileSource f(raw);
    if (!f.ok()) { st.error = SessionError::memory; error = "session file: " + f.error(); return false; }
    // installed before the first block (the header's) is read: every block reports, the first one included
    if (limits.progress) f.progress.emplace(limits.progress);
    f.expected = size;
    uint8_t h[kHeader];
    if (!f.read(h, kHeader)) { st.error = f.kind(); error = "session file: header read error: " + f.error(); return false; }
    uint32_t version = 0, hsize = 0;
    uint64_t model = 0, config = 0, payload = 0, r0 = 0, r1 = 0, hh = 0;
    std::memcpy(&version, h + 8, 4); std::memcpy(&hsize, h + 12, 4);
    std::memcpy(&model, h + 16, 8); std::memcpy(&config, h + 24, 8); std::memcpy(&payload, h + 32, 8);
    std::memcpy(&r0, h + 40, 8); std::memcpy(&r1, h + 48, 8); std::memcpy(&hh, h + 56, 8);
    if (std::memcmp(h, kMagic, 8) != 0) { error = "session file: not a Strata session file (magic)"; return false; }
    if (hh != session_hash64(h, 56, 0)) { error = "session file: header checksum mismatch"; return false; }
    if (version != kVersion) { error = "session file: unsupported version " + std::to_string(version); return false; }
    if (hsize != kHeader || r0 || r1) { error = "session file: invalid header fields"; return false; }
    if (model != id.model) { error = "session file: saved with another model (model fingerprint differs)"; return false; }
    if (config != id.config) { error = "session file: saved with another engine configuration (config fingerprint differs)"; return false; }
    if (payload > size || size - kHeader - kTrailer != payload) {
        error = "session file: size " + std::to_string(size) + " does not match the header (" +
                std::to_string(kHeader + payload + kTrailer) + ")";
        return false;
    }
    if (limits.admit) {
        std::string why;
        if (!limits.admit(session_read_peak_bytes(size), why)) {
            st.error = SessionError::memory;
            error = "session file: " + why;
            return false;
        }
    }
    SavedConversation parsed;
    In in{&f, SessionHasher(0), payload, error, limits};
    if (!get_payload(in, parsed)) { st.error = in.kind; return false; }
    if (in.left) { error = "session file: payload has " + std::to_string(in.left) + " unparsed bytes"; return false; }
    uint8_t t[kTrailer];
    if (!f.read(t, kTrailer)) { st.error = f.kind(); error = "session file: trailer read error: " + f.error(); return false; }
    uint64_t ph = 0;
    std::memcpy(&ph, t, 8);
    if (ph != in.hash.digest()) { error = "session file: payload checksum mismatch"; return false; }
    if (std::memcmp(t + 8, kEnd, 8) != 0) { error = "session file: bad end marker"; return false; }
    image = std::move(parsed);
    bytes = (size_t) size;
    st.error = SessionError::none;
    return true;
}
} // namespace

bool session_file_read(const std::string& path, const SessionFileIdentity& id, SavedConversation& image,
                       size_t& bytes, std::string& error, const SessionReadLimits& limits, SessionStatus* status) {
    SessionStatus st;
    bool ok = false;
    try {
        ok = read_impl(path, id, image, bytes, error, limits, st);
    } catch (const std::bad_alloc&) {
        error = "session file: out of memory while reading " + path;
        st.error = SessionError::memory;
        ok = false;
    }
    if (status) *status = st;
    return ok;
}

// ============================ format v2 ============================
namespace {
constexpr char kMagic2[8] = {'S', 'T', 'R', 'S', 'E', 'S', 'S', '\x02'};
constexpr char kEnd2[8] = {'S', 'T', 'R', 'S', 'E', 'N', 'D', '\x02'};
constexpr uint32_t kVersion2 = 2;
constexpr size_t kTrailer2 = 32;   // payload, manifest and region-table hashes, then the committed marker
constexpr uint64_t kSectionFrame = 12;   // tag:u32 + body length:u64
constexpr uint32_t kSecLayout = 0x5459414Cu, kSecDraft = 0x54465244u, kSecTokens = 0x4E4B4F54u,
                   kSecImages = 0x47414D49u, kSecChecks = 0x54535043u, kSecRegions = 0x4B534452u, kSecEnd = 0;
constexpr int64_t kPlanFields = 7;   // chunk, from, to, lend, pipe bits, single, chunked
constexpr int64_t kPairFields = 8;   // ring id, bytes, checksum, cells, window, capacity, committed end, appended through
constexpr int64_t kCheckpointFields = 4 + kPlanFields + kPairFields;   // prefix, used, pinned, stage parts + the plan + the pair
constexpr int64_t kRegionFields = 7;   // kind, stage, index, part, offset, bytes, checksum
const uint64_t kFullParts = (uint64_t(1) << kSessionStateParts) - 1;

bool region_less(const SessionRegion& a, const SessionRegion& b) {
    if (a.kind != b.kind) return a.kind < b.kind;
    if (a.stage != b.stage) return a.stage < b.stage;
    if (a.index != b.index) return a.index < b.index;
    return a.part < b.part;
}

bool known_kind(int32_t kind) {
    return kind >= int32_t(SessionRegionKind::live_state) && kind <= int32_t(SessionRegionKind::dflash_pair);
}

// the regions naming one (kind, stage, index): how many, and which parts they cover
void group_parts(const std::vector<SessionRegion>& regions, int32_t kind, int64_t stage, int64_t index,
                 uint64_t& count, uint64_t& mask) {
    count = 0;
    mask = 0;
    for (const auto& r : regions) {
        if (r.kind != kind || r.stage != stage || r.index != index) continue;
        ++count;
        if (r.part >= 0 && r.part < kSessionStateParts) mask |= uint64_t(1) << r.part;
    }
}

// Every rule a v2 manifest must satisfy, checked before a write starts and again on a read before the sink sees a
// byte.  A manifest that does not describe a complete, unambiguous set of regions describes a file to refuse:
// half a stage's live state, half a layer, a checkpoint part of a checkpoint that covers none, two regions for
// one part, a stage range that cannot name its layers.
bool check_manifest_v2(const SessionManifestV2& m, std::string& error) {
    auto refuse = [&](const std::string& what) {
        if (error.empty()) error = "session file v2: " + what;
        return false;
    };
    const int64_t stages = (int64_t) m.layout.stages.size();
    if (!stages) return refuse("the layout names no stage");
    if (stages > int64_t(kSessionMaxStages)) return refuse("the layout names " + std::to_string(stages) + " stages");
    for (const auto& s : m.layout.stages)
        if (s.layer_lo < 0 || s.layer_hi < s.layer_lo)
            return refuse("stage layer range " + std::to_string(s.layer_lo) + ".." + std::to_string(s.layer_hi));
    const auto& d = m.draft;
    if (d.type < 0) return refuse("negative draft type");
    if (d.type == 0) {
        if (d.ring_cells || d.window || d.capacity) return refuse("no draft with a ring layout");
        if (d.committed_end != -1 || d.appended_through != -1) return refuse("no draft with ring counters");
    } else if (d.capacity <= 0 || d.window < 0 || d.ring_cells < 0 || d.committed_end < 0 ||
               d.appended_through < d.committed_end || d.committed_end > d.capacity || d.appended_through > d.capacity) {
        return refuse("draft counters outside the ring capacity");
    }
    for (size_t i = 0; i < m.images.size(); ++i)
        if (m.images[i].start < 0 || (i && m.images[i].start <= m.images[i - 1].start))
            return refuse("image keys must start above zero and ascend");
    for (const auto& c : m.checkpoints) {
        if (c.prefix > m.tokens.size()) return refuse("a checkpoint prefix reaches past the stored tokens");
        if (c.stage_parts != 0 && c.stage_parts != stages - 1)
            return refuse("a checkpoint covers " + std::to_string(c.stage_parts) + " of " +
                          std::to_string(stages - 1) + " later stages");
    }
    for (size_t i = 1; i < m.regions.size(); ++i) {
        const auto& prev = m.regions[i - 1];
        const auto& cur = m.regions[i];
        if (prev.kind == cur.kind && prev.stage == cur.stage && prev.index == cur.index && prev.part == cur.part)
            return refuse("the region (" + std::string(session_region_kind_name(cur.kind)) + ", stage " +
                          std::to_string(cur.stage) + ", index " + std::to_string(cur.index) + ", part " +
                          std::to_string(cur.part) + ") is declared twice");
        if (!region_less(prev, cur))
            return refuse("the region directory is not in canonical order (kind, stage, index, part)");
    }
    for (const auto& r : m.regions) {
        if (!known_kind(r.kind)) return refuse(std::string("region kind ") + session_region_kind_name(r.kind));
        if (r.stage < 0 || r.stage >= stages) return refuse("region stage " + std::to_string(r.stage) + " outside the layout");
        if (r.kind == int32_t(SessionRegionKind::dflash)) {
            if (r.stage || r.index || r.part) return refuse("the dflash region names a stage, layer or part");
            continue;
        }
        if (r.part < 0 || r.part >= kSessionStateParts) return refuse("region part " + std::to_string(r.part) + " outside 0..4");
    }
    if (!m.regions.empty() && m.regions.front().kind != int32_t(SessionRegionKind::live_state))
        return refuse("the region directory names no live state");
    uint64_t count = 0, mask = 0;
    for (int64_t s = 0; s < stages; ++s) {
        group_parts(m.regions, int32_t(SessionRegionKind::live_state), s, 0, count, mask);
        if (count != uint64_t(kSessionStateParts) || mask != kFullParts)
            return refuse("stage " + std::to_string(s) + " does not hold its full live state");
        group_parts(m.regions, int32_t(SessionRegionKind::mtp), s, 0, count, mask);
        if (count > uint64_t(kSessionKvParts) || (count && mask != kFullParts))
            return refuse("stage " + std::to_string(s) + " holds a partial MTP region");
    }
    int dflash_regions = 0, pair_regions = 0;
    for (size_t i = 0; i < m.regions.size(); ++i) {
        const auto& r = m.regions[i];
        if (r.kind == int32_t(SessionRegionKind::dflash)) {
            ++dflash_regions;
            continue;
        }
        if (r.kind == int32_t(SessionRegionKind::dflash_pair)) {
            if (r.stage || r.part) return refuse("a checkpoint pair region names a stage or part");
            if (r.index >= int64_t(m.checkpoints.size()))
                return refuse("a checkpoint pair region names checkpoint " + std::to_string(r.index) +
                              ", the manifest holds " + std::to_string(m.checkpoints.size()));
            if (m.checkpoints[(size_t) r.index].ring_bytes != r.bytes)
                return refuse("the pair region of checkpoint " + std::to_string(r.index) + " is " +
                              std::to_string(r.bytes) + " bytes, its meta declares " +
                              std::to_string(m.checkpoints[(size_t) r.index].ring_bytes));
            ++pair_regions;
            continue;
        }
        if (r.kind == int32_t(SessionRegionKind::kv)) {
            const auto& s = m.layout.stages[(size_t) r.stage];
            if (r.index >= s.layer_hi - s.layer_lo)
                return refuse("a K/V region names layer " + std::to_string(r.index) + " outside stage " +
                              std::to_string(r.stage) + " [" + std::to_string(s.layer_lo) + "," +
                              std::to_string(s.layer_hi) + ")");
            group_parts(m.regions, r.kind, r.stage, r.index, count, mask);
            if (count != uint64_t(kSessionKvParts) || mask != kFullParts)
                return refuse("stage " + std::to_string(r.stage) + " layer " + std::to_string(r.index) +
                              " is saved in part");
            continue;
        }
        if (r.kind == int32_t(SessionRegionKind::checkpoint_state)) {
            if (r.index >= int64_t(m.checkpoints.size()))
                return refuse("a checkpoint region names checkpoint " + std::to_string(r.index) +
                              ", the manifest holds " + std::to_string(m.checkpoints.size()));
            group_parts(m.regions, r.kind, r.stage, r.index, count, mask);
            if (count != uint64_t(kSessionStateParts) || mask != kFullParts)
                return refuse("checkpoint " + std::to_string(r.index) + " of stage " + std::to_string(r.stage) +
                              " is saved in part");
        }
    }
    for (size_t i = 0; i < m.checkpoints.size(); ++i) {
        const bool parts = m.checkpoints[i].stage_parts != 0;
        for (int64_t s = 1; s < stages; ++s) {
            group_parts(m.regions, int32_t(SessionRegionKind::checkpoint_state), s, int64_t(i), count, mask);
            if (parts && (count != uint64_t(kSessionStateParts) || mask != kFullParts))
                return refuse("checkpoint " + std::to_string(i) + " of stage " + std::to_string(s) +
                              " is missing its part");
            if (!parts && count)
                return refuse("checkpoint " + std::to_string(i) + " saves a part of stage " + std::to_string(s) +
                              " it does not cover");
        }
        // the paired draft capture: target-only is all-zero; a pair is captured AT the checkpoint
        // position, on the file's own draft layout, with self-consistent counters
        const auto& c = m.checkpoints[i];
        if (c.ring_id == 0) {
            if (c.ring_bytes || c.ring_checksum)
                return refuse("checkpoint " + std::to_string(i) + " is target-only with pair bytes");
            if (c.ring_cells || c.ring_window || c.ring_capacity || c.ring_committed_end != -1 ||
                c.ring_appended_through != -1)
                return refuse("checkpoint " + std::to_string(i) + " is target-only with pair counters");
            continue;
        }
        if (d.type == 0) return refuse("a checkpoint pair with no draft in the file");
        if (c.ring_bytes == 0) return refuse("checkpoint " + std::to_string(i) + " has a pair of no bytes");
        if (c.ring_committed_end != int64_t(c.prefix))
            return refuse("checkpoint " + std::to_string(i) + " pairs the ring at position " +
                          std::to_string(c.ring_committed_end) + ", the checkpoint sits at " +
                          std::to_string(c.prefix));
        if (c.ring_cells != d.ring_cells || c.ring_window != d.window || c.ring_capacity != d.capacity)
            return refuse("checkpoint " + std::to_string(i) + " pairs a ring layout that is not the file's draft");
        if (c.ring_committed_end < 0 || c.ring_appended_through < c.ring_committed_end ||
            c.ring_committed_end > d.capacity || c.ring_appended_through > d.capacity)
            return refuse("checkpoint " + std::to_string(i) + " pair counters outside the ring capacity");
        const SessionRegion* region = nullptr;
        for (const auto& r : m.regions)
            if (r.kind == int32_t(SessionRegionKind::dflash_pair) && r.index == int64_t(i)) { region = &r; break; }
        if (region == nullptr) return refuse("checkpoint " + std::to_string(i) + " has a pair with no region");
        if (region->checksum && region->checksum != c.ring_checksum)
            return refuse("checkpoint " + std::to_string(i) + " pair region checksum differs from its meta");
    }
    if (dflash_regions > 1) return refuse(std::to_string(dflash_regions) + " dflash regions");
    if (dflash_regions && d.type == 0) return refuse("a dflash region with no draft");
    int paired = 0;
    for (const auto& c : m.checkpoints) paired += c.ring_id ? 1 : 0;
    if (pair_regions != paired)
        return refuse(std::to_string(pair_regions) + " pair regions for " + std::to_string(paired) +
                      " paired checkpoints");
    return true;
}

// the manifest's exact byte length, saturating (a manifest that does not fit is a refusal, not a truncated file)
uint64_t v2_manifest_bytes(const SessionManifestV2& m, bool& overflow) {
    uint64_t n = 0;
    auto section = [&](uint64_t body) { n = sat_add(n, sat_add(kSectionFrame, body)); };
    section(sat_add(18 * 8, sat_add(8, sat_mul(m.layout.stages.size(), 24))));
    section(7 * 8);
    section(sat_add(16, sat_mul(m.tokens.size(), 4)));
    section(sat_add(8, sat_mul(m.images.size(), 16)));
    section(sat_add(8, sat_mul(m.checkpoints.size(), kCheckpointFields * 8)));
    section(sat_add(8, sat_mul(m.regions.size(), kRegionFields * 8)));
    n = sat_add(n, kSectionFrame);   // the terminator
    overflow = n == UINT64_MAX;
    return n;
}

void put_manifest_v2(Out& o, const SessionManifestV2& m) {
    auto section = [&](uint32_t tag, uint64_t body) { o.raw(&tag, 4); o.u64(body); };
    section(kSecLayout, sat_add(18 * 8, sat_add(8, sat_mul(m.layout.stages.size(), 24))));
    for (int64_t g : m.layout.geometry) o.i64(g);
    o.u64(m.layout.stages.size());
    for (const auto& s : m.layout.stages) { o.i64(s.layer_lo); o.i64(s.layer_hi); o.i64(s.kv_format); }
    section(kSecDraft, 7 * 8);
    o.i64(m.draft.type); o.u64(m.draft.artifact); o.i64(m.draft.ring_cells); o.i64(m.draft.window);
    o.i64(m.draft.capacity); o.i64(m.draft.committed_end); o.i64(m.draft.appended_through);
    section(kSecTokens, sat_add(16, sat_mul(m.tokens.size(), 4)));
    o.u64(m.cvec ? 1 : 0);
    o.vec(m.tokens);
    section(kSecImages, sat_add(8, sat_mul(m.images.size(), 16)));
    o.u64(m.images.size());
    for (const auto& k : m.images) { o.i64(k.start); o.u64(k.hash); }
    section(kSecChecks, sat_add(8, sat_mul(m.checkpoints.size(), kCheckpointFields * 8)));
    o.u64(m.checkpoints.size());
    for (const auto& c : m.checkpoints) {
        uint64_t bits = 0;
        std::memcpy(&bits, &c.plan.pipe, 8);   // the pipe value by its exact bits, never by a decimal
        o.u64(c.prefix); o.u64(c.used); o.u64(c.pinned ? 1 : 0); o.i64(c.stage_parts);
        o.i64(c.plan.chunk); o.i64(c.plan.from); o.i64(c.plan.to); o.i64(c.plan.lend); o.u64(bits);
        o.u64(c.plan.single ? 1 : 0); o.u64(c.plan.chunked ? 1 : 0);
        o.u64(c.ring_id); o.u64(c.ring_bytes); o.u64(c.ring_checksum);
        o.i64(c.ring_cells); o.i64(c.ring_window); o.i64(c.ring_capacity);
        o.i64(c.ring_committed_end); o.i64(c.ring_appended_through);
    }
    section(kSecRegions, sat_add(8, sat_mul(m.regions.size(), kRegionFields * 8)));
    o.u64(m.regions.size());
    for (const auto& r : m.regions) {
        o.i64(r.kind); o.i64(r.stage); o.i64(r.index); o.i64(r.part);
        o.u64(r.offset); o.u64(r.bytes); o.u64(r.checksum);
    }
    section(kSecEnd, 0);
}

// the manifest's bounded parse: every count is checked against the section's declared length and the caller's
// limits before its array is allocated, and a section whose fields do not fill it is a refusal
struct SpanIn {
    const uint8_t* p = nullptr;
    uint64_t left = 0;
    std::string* error = nullptr;
    bool fail(const std::string& what) {
        if (error->empty()) *error = "session file v2: " + what;
        return false;
    }
    bool take(void* dst, uint64_t n) {
        if (n > left) return fail("the manifest ends early");
        std::memcpy(dst, p, n);
        p += n;
        left -= n;
        return true;
    }
    bool u64(uint64_t& v) { return take(&v, 8); }
    bool i64(int64_t& v) { return take(&v, 8); }
    bool u32(uint32_t& v) { return take(&v, 4); }
    bool count(uint64_t& n, uint64_t elem, uint64_t limit) {
        if (!u64(n)) return false;
        if (elem && n > left / elem) return fail("element count exceeds the manifest");
        if (n > limit) return fail("element count " + std::to_string(n) + " exceeds this engine's limit " + std::to_string(limit));
        if (n > (uint64_t) std::numeric_limits<size_t>::max() / (elem ? elem : 1)) return fail("element count too large");
        return true;
    }
};

bool parse_manifest_v2(const uint8_t* data, uint64_t n, const SessionReadV2Limits& limits, SessionManifestV2& m,
                       std::string& error) {
    auto refuse = [&](const std::string& what) {
        if (error.empty()) error = "session file v2: " + what;
        return false;
    };
    SpanIn in{data, n, &error};
    uint32_t seen = 0;
    auto mark = [&](uint32_t bit) {
        if (seen & bit) return refuse("repeated manifest section");
        seen |= bit;
        return true;
    };
    constexpr uint32_t kAllSections = 1u | 2u | 4u | 8u | 16u | 32u;
    for (;;) {
        uint32_t tag = 0;
        uint64_t body = 0;
        if (!in.u32(tag) || !in.u64(body)) return false;
        if (body > in.left) return refuse("a section length exceeds the manifest");
        if (tag == kSecEnd) {
            if (body) return refuse("the terminator names bytes");
            break;
        }
        SpanIn sec{in.p, body, &error};
        in.p += body;
        in.left -= body;
        uint64_t count = 0;
        switch (tag) {
            case kSecLayout: {
                if (!mark(1u)) return false;
                for (auto& g : m.layout.geometry) if (!sec.i64(g)) return false;
                if (!sec.count(count, 24, limits.max_stages)) return false;
                m.layout.stages.resize((size_t) count);
                for (auto& s : m.layout.stages)
                    if (!sec.i64(s.layer_lo) || !sec.i64(s.layer_hi) || !sec.i64(s.kv_format)) return false;
                break;
            }
            case kSecDraft: {
                if (!mark(2u)) return false;
                if (!sec.i64(m.draft.type) || !sec.u64(m.draft.artifact) || !sec.i64(m.draft.ring_cells) ||
                    !sec.i64(m.draft.window) || !sec.i64(m.draft.capacity) || !sec.i64(m.draft.committed_end) ||
                    !sec.i64(m.draft.appended_through)) return false;
                break;
            }
            case kSecTokens: {
                if (!mark(4u)) return false;
                uint64_t cvec = 0;
                if (!sec.u64(cvec)) return false;
                if (cvec > 1) return refuse("invalid cvec flag");
                m.cvec = cvec == 1;
                if (!sec.count(count, 4, limits.max_tokens)) return false;
                m.tokens.resize((size_t) count);
                if (!sec.take(m.tokens.data(), count * 4)) return false;
                break;
            }
            case kSecImages: {
                if (!mark(8u)) return false;
                if (!sec.count(count, 16, limits.max_images)) return false;
                m.images.resize((size_t) count);
                for (auto& k : m.images) if (!sec.i64(k.start) || !sec.u64(k.hash)) return false;
                break;
            }
            case kSecChecks: {
                if (!mark(16u)) return false;
                if (!sec.count(count, kCheckpointFields * 8, limits.max_checkpoints)) return false;
                m.checkpoints.resize((size_t) count);
                for (auto& c : m.checkpoints) {
                    uint64_t pinned = 0, single = 0, chunked = 0, bits = 0;
                    if (!sec.u64(c.prefix) || !sec.u64(c.used) || !sec.u64(pinned) || !sec.i64(c.stage_parts) ||
                        !sec.i64(c.plan.chunk) || !sec.i64(c.plan.from) || !sec.i64(c.plan.to) ||
                        !sec.i64(c.plan.lend) || !sec.u64(bits) || !sec.u64(single) || !sec.u64(chunked) ||
                        !sec.u64(c.ring_id) || !sec.u64(c.ring_bytes) || !sec.u64(c.ring_checksum) ||
                        !sec.i64(c.ring_cells) || !sec.i64(c.ring_window) || !sec.i64(c.ring_capacity) ||
                        !sec.i64(c.ring_committed_end) || !sec.i64(c.ring_appended_through)) return false;
                    if (pinned > 1 || single > 1 || chunked > 1) return refuse("invalid checkpoint flag");
                    c.pinned = pinned == 1;
                    c.plan.single = single == 1;
                    c.plan.chunked = chunked == 1;
                    std::memcpy(&c.plan.pipe, &bits, 8);
                }
                break;
            }
            case kSecRegions: {
                if (!mark(32u)) return false;
                if (!sec.count(count, kRegionFields * 8, limits.max_regions)) return false;
                m.regions.resize((size_t) count);
                for (auto& r : m.regions) {
                    int64_t kind = 0;
                    if (!sec.i64(kind) || !sec.i64(r.stage) || !sec.i64(r.index) || !sec.i64(r.part) ||
                        !sec.u64(r.offset) || !sec.u64(r.bytes) || !sec.u64(r.checksum)) return false;
                    if (kind < int64_t(INT32_MIN) || kind > int64_t(INT32_MAX)) return refuse("invalid region kind");
                    r.kind = (int32_t) kind;
                    if (r.bytes > limits.max_region_bytes)
                        return refuse("region of " + std::to_string(r.bytes) + " bytes over the runtime limit " +
                                      std::to_string(limits.max_region_bytes));
                }
                break;
            }
            default:
                return refuse("unknown manifest section " + std::to_string(tag));
        }
        if (sec.left) return refuse("a manifest section has unparsed bytes");
    }
    if (in.left) return refuse("the manifest has unparsed bytes");
    if (seen != kAllSections) return refuse("the manifest is missing a required section");
    if (!check_manifest_v2(m, error)) return false;
    // the region block is tiled by the directory, with no gap, overlap or overflow
    uint64_t at = 0;
    for (const auto& r : m.regions) {
        if (r.offset != at)
            return refuse("region (" + std::string(session_region_kind_name(r.kind)) + ", " + std::to_string(r.stage) +
                          ", " + std::to_string(r.index) + ", " + std::to_string(r.part) + ") starts at " +
                          std::to_string(r.offset) + " where " + std::to_string(at) + " is expected");
        at = sat_add(at, r.bytes);
        if (at == UINT64_MAX) return refuse("region lengths overflow");
    }
    return true;
}

// header v2, little-endian: magic[8] version:u32 header_size:u64[?] model:u64 config:u64 payload:u64
// manifest_bytes:u64 reserved:u64 header_hash:u64 (session_hash64 of bytes 0..55, seed 0).  The same field
// offsets as v1, so one header parse reads both, and the reserved field must stay zero.
void header_bytes_v2(uint8_t* h, const SessionFileIdentity& id, uint64_t payload, uint64_t manifest_bytes) {
    std::memset(h, 0, kHeader);
    std::memcpy(h, kMagic2, 8);
    const uint32_t version = kVersion2, size = kHeader;
    std::memcpy(h + 8, &version, 4);
    std::memcpy(h + 12, &size, 4);
    std::memcpy(h + 16, &id.model, 8);
    std::memcpy(h + 24, &id.config, 8);
    std::memcpy(h + 32, &payload, 8);
    std::memcpy(h + 40, &manifest_bytes, 8);
    const uint64_t hh = session_hash64(h, 56, 0);
    std::memcpy(h + 56, &hh, 8);
}

// One bounded transfer buffer over the opened file, positioned by pread: the v2 read's streaming walk.  Blocks are
// at most 16 MiB, so its memory is fixed whatever the conversation is.
class RegionStream {
public:
    explicit RegionStream(RawFile& f) : buf_(aligned_block()), f_(f) {}
    bool ok() const { return buf_ != nullptr; }
    std::string error() const { return buf_ ? f_.error() : std::string("out of memory for the transfer buffer"); }
    SessionError kind() const { return buf_ ? f_.kind() : SessionError::memory; }
    bool direct() const { return f_.direct(); }
    bool bytes(uint64_t at, uint8_t* dst, size_t n) { return !n || f_.pread_bytes(at, dst, n); }
    // [at, at+n) in blocks of at most 16 MiB, handed to fn block by block
    bool stream(uint64_t at, uint64_t n, const std::function<bool(const uint8_t*, size_t)>& fn) {
        uint64_t done = 0;
        while (done < n) {
            const size_t c = std::min<uint64_t>(kBlock, n - done);
            if (!f_.pread_bytes(at + done, buf_.get(), c)) return false;
            if (!fn(buf_.get(), c)) return false;
            done += c;
        }
        return true;
    }
private:
    Aligned buf_;
    RawFile& f_;
};

bool write_v2_impl(const std::string& path, const SessionFileIdentity& id, const SessionManifestV2& manifest,
                   const std::vector<SessionRegionSource>& sources, size_t& bytes, std::string& error,
                   const SessionWriteOptions& opt, SessionStatus& st) {
    st = {};
    st.error = SessionError::invalid;   // refusals before any disk access
    if (sources.size() != manifest.regions.size()) {
        error = "session file v2: " + std::to_string(manifest.regions.size()) + " regions declared, " +
                std::to_string(sources.size()) + " sources given";
        return false;
    }
    // canonical order, a source moving with its region: the caller's order is not part of the format
    std::vector<size_t> order(manifest.regions.size());
    for (size_t i = 0; i < order.size(); ++i) order[i] = i;
    std::sort(order.begin(), order.end(), [&](size_t a, size_t b) { return region_less(manifest.regions[a], manifest.regions[b]); });
    SessionManifestV2 m = manifest;
    std::vector<SessionRegionSource> src;
    src.reserve(order.size());
    std::vector<SessionRegion> regions;
    regions.reserve(order.size());
    for (size_t i : order) {
        regions.push_back(manifest.regions[i]);
        src.push_back(sources[i]);
    }
    m.regions = std::move(regions);
    if (!check_manifest_v2(m, error)) return false;
    for (size_t i = 0; i < m.regions.size(); ++i) {
        const auto& r = m.regions[i];
        const auto& s = src[i];
        const std::string name = std::string(" (") + session_region_kind_name(r.kind) + ", stage " + std::to_string(r.stage) +
                                 ", index " + std::to_string(r.index) + ", part " + std::to_string(r.part) + ")";
        if (s.kind != r.kind || s.stage != r.stage || s.index != r.index || s.part != r.part) {
            error = "session file v2: the source for region" + name + " names another region";
            return false;
        }
        if (s.size != r.bytes) {
            error = "session file v2: the source for region" + name + " holds " + std::to_string(s.size) +
                    " bytes, the manifest declares " + std::to_string(r.bytes);
            return false;
        }
        if (!s.read) {
            error = "session file v2: the source for region" + name + " cannot be read";
            return false;
        }
    }
    uint64_t region_bytes = 0;
    for (auto& r : m.regions) {
        r.offset = region_bytes;
        r.checksum = 0;   // the write fills it in the file's region table
        region_bytes = sat_add(region_bytes, r.bytes);
    }
    if (region_bytes == UINT64_MAX) { error = "session file v2: region lengths overflow"; return false; }
    bool over = false;
    const uint64_t manifest_bytes = v2_manifest_bytes(m, over);
    const uint64_t table_bytes = sat_add(8, sat_mul(m.regions.size(), 8));
    const uint64_t payload = sat_add(manifest_bytes, region_bytes);
    const uint64_t total = sat_add(kHeader, sat_add(payload, sat_add(table_bytes, kTrailer2)));
    if (over || manifest_bytes == UINT64_MAX || table_bytes == UINT64_MAX || payload == UINT64_MAX ||
        total == UINT64_MAX || total > (uint64_t) std::numeric_limits<size_t>::max()) {
        error = "session file v2: the state is too large for this build";
        return false;
    }
    st.error = SessionError::io;
    if (opt.min_free_bytes) {
        uint64_t avail = 0;
        if (!free_space(path, avail)) { error = "session file v2: cannot read the free disk space for " + path; return false; }
        if (avail < total || avail - total < opt.min_free_bytes) {
            st.error = SessionError::storage;
            error = "session file v2: not enough disk space (" + std::to_string(total >> 20) + " MiB plus a reserve of " +
                    std::to_string(opt.min_free_bytes >> 20) + " MiB needed, " + std::to_string(avail >> 20) + " MiB free)";
            return false;
        }
    }
    Publisher pub;
    if (!pub.open(path, error)) { st.error = pub.kind(); return false; }
    RawFile raw;
    if (!pub.create_temp(raw, error)) {
        st.error = raw.kind();
        error = "session file v2: cannot create a temporary file beside " + path + ": " + error;
        return false;
    }
    FileSink f(raw);
    if (!f.ok()) { st.error = SessionError::memory; error = "session file v2: " + f.error(); return false; }
    if (opt.progress) f.progress.emplace(opt.progress);
    f.phase = opt.phase;
    f.mode = opt.mode;
    f.expected = total;
    if (f.progress) (*f.progress)(0, total);
    const std::function<int(const char*)>* fault = opt.fault ? &opt.fault : nullptr;
    f.write_fault = injected(fault, "write");
    f.flush_fault = injected(fault, "file_flush");
    uint8_t h[kHeader];
    header_bytes_v2(h, id, payload, manifest_bytes);
    bool ok = f.write(h, kHeader);
    SessionHasher payload_hash(0), manifest_hash(0);
    Out out;
    out.f = &f;
    out.hash = &payload_hash;
    out.meta = &manifest_hash;
    if (ok) put_manifest_v2(out, m);
    ok = ok && out.ok && out.n == manifest_bytes;
    out.meta = nullptr;   // the region bytes belong to the payload hash only
    std::vector<uint64_t> sums(m.regions.size(), 0);
    std::string source_why;   // a source that fails to read names itself; the tail keeps that reason
    for (size_t i = 0; i < m.regions.size() && ok; ++i) {
        const auto& s = src[i];
        SessionHasher one(0);
        uint64_t done = 0;
        while (done < s.size && ok) {
            size_t room = 0;
            uint8_t* d = f.span(room);
            const size_t c = std::min(room, size_t(s.size - done));
            if (!s.read(done, d, c)) {
                ok = false;
                source_why = "reading the source of region (" + std::string(session_region_kind_name(s.kind)) +
                             ", stage " + std::to_string(s.stage) + ", index " + std::to_string(s.index) + ", part " +
                             std::to_string(s.part) + ") at " + std::to_string(done) +
                             (s.error ? ": " + s.error() : std::string());
                break;
            }
            one.update(d, c);
            payload_hash.update(d, c);
            ok = f.commit(c);
            done += c;
        }
        sums[i] = one.digest();
    }
    SessionHasher table_hash(0);
    const uint64_t count = m.regions.size();
    ok = ok && f.write(&count, 8);
    if (ok) table_hash.update(&count, 8);
    for (size_t i = 0; ok && i < sums.size(); ++i) {
        ok = f.write(&sums[i], 8);
        if (ok) table_hash.update(&sums[i], 8);
    }
    if (ok) {
        const uint64_t ph = payload_hash.digest(), mh = manifest_hash.digest(), th = table_hash.digest();
        ok = f.write(&ph, 8) && f.write(&mh, 8) && f.write(&th, 8) && f.write(kEnd2, 8);
    }
    ok = f.finish(opt.durable) && ok;
    std::string why;
    if (!ok) {
        why = source_why.empty() ? f.error() : source_why;
        st.error = source_why.empty() ? f.kind() : SessionError::io;
    } else {
        if (opt.phase) opt.phase("publish", total);
        if (pub.publish(why, opt.durable, st, fault)) {
            st.error = SessionError::none;
            bytes = (size_t) total;
            return true;
        }
    }
    error = "session file v2: writing " + path + " failed" + (why.empty() ? std::string() : ": " + why);
    if (st.published) error += " (the new file has already replaced the old one; its folder entry may not survive a power loss)";
    return false;   // Publisher removes this call's temporary file, and only that
}

bool read_v2_impl(const std::string& path, const SessionFileIdentity& id, const SessionV2Requirement& want,
                  SessionManifestV2& manifest, size_t& bytes, std::string& error, const SessionReadV2Limits& limits,
                  SessionRegionSink* sink, SessionStatus& st) {
    error.clear();
    st = {};
    st.error = SessionError::invalid;   // every refusal below, unless the step says otherwise
    RawFile raw;
    uint64_t size = 0;
    if (!raw.open_session(path, size)) {
        st.error = raw.refused() ? SessionError::invalid : raw.kind();
        error = "session file v2: " + path + ": " + raw.error();
        return false;
    }
    if (size < kHeader + kTrailer2) { error = "session file v2: size " + std::to_string(size) + " is below the minimum"; return false; }
    if (size > limits.max_file_bytes) {
        error = "session file v2: size " + std::to_string(size) + " exceeds this engine's limit of " +
                std::to_string(limits.max_file_bytes);
        return false;
    }
    RegionStream f(raw);
    if (!f.ok()) { st.error = SessionError::memory; error = "session file v2: " + f.error(); return false; }
    std::optional<Ticker> progress;
    if (limits.progress) progress.emplace(limits.progress);
    uint8_t h[kHeader];
    if (!raw.pread_bytes(0, h, kHeader)) { st.error = raw.kind(); error = "session file v2: header read error: " + raw.error(); return false; }
    if (limits.mode) limits.mode(raw.direct());   // the mode of the opened file at its first transfer
    uint32_t version = 0, hsize = 0;
    uint64_t model = 0, config = 0, payload = 0, manifest_bytes = 0, r1 = 0, hh = 0;
    std::memcpy(&version, h + 8, 4); std::memcpy(&hsize, h + 12, 4);
    std::memcpy(&model, h + 16, 8); std::memcpy(&config, h + 24, 8); std::memcpy(&payload, h + 32, 8);
    std::memcpy(&manifest_bytes, h + 40, 8); std::memcpy(&r1, h + 48, 8); std::memcpy(&hh, h + 56, 8);
    if (std::memcmp(h, kMagic2, 8) != 0) { error = "session file v2: not a Strata v2 session file (magic)"; return false; }
    if (hh != session_hash64(h, 56, 0)) { error = "session file v2: header checksum mismatch"; return false; }
    if (version != kVersion2) { error = "session file v2: unsupported version " + std::to_string(version); return false; }
    if (hsize != kHeader || r1) { error = "session file v2: invalid header fields"; return false; }
    if (model != id.model) { error = "session file v2: saved with another model (model fingerprint differs)"; return false; }
    if (config != id.config) { error = "session file v2: saved with another engine configuration (config fingerprint differs)"; return false; }
    if (payload > size - kHeader - kTrailer2 || manifest_bytes > size - kHeader - kTrailer2) {
        error = "session file v2: size " + std::to_string(size) + " does not match the header (" +
                std::to_string(kHeader + payload + kTrailer2) + " at least)";
        return false;
    }
    if (manifest_bytes > limits.max_manifest_bytes) {
        error = "session file v2: manifest of " + std::to_string(manifest_bytes) + " bytes over the runtime limit " +
                std::to_string(limits.max_manifest_bytes);
        return false;
    }
    if (limits.admit) {
        std::string why;
        if (!limits.admit(session_read_v2_peak_bytes(manifest_bytes, limits.max_regions), why)) {
            st.error = SessionError::memory;
            error = "session file v2: " + why;
            return false;
        }
    }
    if (sink && (!sink->validate_region || !sink->write || !sink->finish)) {
        error = "session file v2: the sink is incomplete";
        return false;
    }
    std::vector<uint8_t> md((size_t) manifest_bytes);
    if (!f.bytes(kHeader, md.data(), md.size())) { st.error = f.kind(); error = "session file v2: manifest read error: " + f.error(); return false; }
    uint64_t beat = kHeader + manifest_bytes;
    SessionManifestV2 parsed;
    if (!parse_manifest_v2(md.data(), md.size(), limits, parsed, error)) return false;
    const uint64_t region_base = kHeader + manifest_bytes;
    uint64_t region_bytes = 0;
    for (const auto& r : parsed.regions) region_bytes = sat_add(region_bytes, r.bytes);
    const uint64_t table_bytes = sat_add(8, sat_mul(parsed.regions.size(), 8));
    if (region_bytes != payload - manifest_bytes || kHeader + payload + table_bytes + kTrailer2 != size) {
        error = "session file v2: size " + std::to_string(size) + " does not match the directory (" +
                std::to_string(kHeader + payload + table_bytes + kTrailer2) + " expected)";
        return false;
    }
    // the read moves the file's bytes once for validation and, with a sink, once more for the regions, so its
    // progress counts both passes; a validation-only read (no sink) reports out of the file size
    const uint64_t beat_total = sink ? sat_add(size, region_bytes) : size;
    if (progress) (*progress)(beat, beat_total);
    if (want.check_layout && want.layout != parsed.layout) {
        error = "session file v2: saved with another split layout than this runtime's";
        return false;
    }
    if (want.check_draft && want.draft != parsed.draft) {
        error = "session file v2: saved with another draft than this runtime's";
        return false;
    }
    // pass 1: every region's bytes are streamed and checksummed, and every destination is validated.  Nothing is
    // written anywhere, so a damaged region anywhere in the payload leaves the sink untouched.
    SessionHasher payload_hash(0), manifest_hash(0);
    payload_hash.update(md.data(), md.size());
    manifest_hash.update(md.data(), md.size());
    std::vector<uint64_t> sums(parsed.regions.size(), 0);
    for (size_t i = 0; i < parsed.regions.size(); ++i) {
        const auto& r = parsed.regions[i];
        std::string why;
        if (sink && !sink->validate_region(r, why)) {
            error = "session file v2: " + (why.empty() ? "the sink refuses this region" : why);
            return false;
        }
        SessionHasher one(0);
        if (!f.stream(region_base + r.offset, r.bytes, [&](const uint8_t* p, size_t c) {
                one.update(p, c);
                payload_hash.update(p, c);
                beat += c;
                if (progress) (*progress)(beat, beat_total);
                return true;
            })) {
            st.error = f.kind();
            error = "session file v2: reading region " + std::string(session_region_kind_name(r.kind)) + " at " +
                    std::to_string(region_base + r.offset) + ": " + f.error();
            return false;
        }
        sums[i] = one.digest();
    }
    const uint64_t table_at = region_base + region_bytes;
    uint64_t tcount = 0;
    if (!f.bytes(table_at, (uint8_t*) &tcount, 8)) { st.error = f.kind(); error = "session file v2: region table read error: " + f.error(); return false; }
    beat += 8;
    if (progress) (*progress)(beat, beat_total);
    SessionHasher table_hash(0);
    table_hash.update(&tcount, 8);
    if (tcount != parsed.regions.size()) { error = "session file v2: the region table names " + std::to_string(tcount) + " regions, the manifest " + std::to_string(parsed.regions.size()); return false; }
    std::vector<uint64_t> table(parsed.regions.size(), 0);
    uint64_t filled = 0;
    if (!f.stream(table_at + 8, table_bytes - 8, [&](const uint8_t* p, size_t c) {
            table_hash.update(p, c);
            std::memcpy((uint8_t*) table.data() + filled, p, c);
            filled += c;
            beat += c;
            if (progress) (*progress)(beat, beat_total);
            return true;
        })) { st.error = f.kind(); error = "session file v2: region table read error: " + f.error(); return false; }
    for (size_t i = 0; i < table.size(); ++i)
        if (table[i] != sums[i]) {
            const auto& r = parsed.regions[i];
            error = "session file v2: region (" + std::string(session_region_kind_name(r.kind)) + ", stage " +
                    std::to_string(r.stage) + ", index " + std::to_string(r.index) + ", part " + std::to_string(r.part) +
                    ") checksums to " + std::to_string(sums[i]) + ", the file says " + std::to_string(table[i]);
            return false;
        }
    // the manifest carries the directory without checksums (the write computes them while streaming); the read
    // hands back the verified values, so the caller sees the checksums that belong to each region
    for (size_t i = 0; i < table.size(); ++i) parsed.regions[i].checksum = table[i];
    uint8_t t[kTrailer2];
    if (!f.bytes(size - kTrailer2, t, kTrailer2)) { st.error = f.kind(); error = "session file v2: trailer read error: " + f.error(); return false; }
    beat += kTrailer2;
    if (progress) (*progress)(beat, beat_total);
    uint64_t ph = 0, mh = 0, th = 0;
    std::memcpy(&ph, t, 8); std::memcpy(&mh, t + 8, 8); std::memcpy(&th, t + 16, 8);
    if (ph != payload_hash.digest()) { error = "session file v2: payload checksum mismatch"; return false; }
    if (mh != manifest_hash.digest()) { error = "session file v2: manifest checksum mismatch"; return false; }
    if (th != table_hash.digest()) { error = "session file v2: region table checksum mismatch"; return false; }
    if (std::memcmp(t + 24, kEnd2, 8) != 0) { error = "session file v2: bad committed marker"; return false; }
    // pass 2: the validated bytes go to their destinations
    for (size_t i = 0; sink && i < parsed.regions.size(); ++i) {
        const auto& r = parsed.regions[i];
        uint64_t done = 0;
        std::string why;
        if (!f.stream(region_base + r.offset, r.bytes, [&](const uint8_t* p, size_t c) {
                if (!sink->write(r, done, p, c, why)) return false;
                done += c;
                beat += c;
                if (progress) (*progress)(beat, beat_total);
                return true;
            })) {
            st.error = SessionError::io;
            error = "session file v2: writing region " + std::string(session_region_kind_name(r.kind)) + " at " +
                    std::to_string(done) + (why.empty() ? std::string() : ": " + why);
            return false;
        }
    }
    std::string why;
    if (sink && !sink->finish(why)) {
        st.error = SessionError::io;
        error = "session file v2: " + (why.empty() ? "the sink failed to finish" : why);
        return false;
    }
    manifest = std::move(parsed);
    bytes = (size_t) size;
    st.error = SessionError::none;
    return true;
}
} // namespace

const char* session_region_kind_name(int32_t kind) {
    switch (kind) {
        case int32_t(SessionRegionKind::live_state): return "live_state";
        case int32_t(SessionRegionKind::kv): return "kv";
        case int32_t(SessionRegionKind::checkpoint_state): return "checkpoint_state";
        case int32_t(SessionRegionKind::mtp): return "mtp";
        case int32_t(SessionRegionKind::dflash): return "dflash";
        case int32_t(SessionRegionKind::dflash_pair): return "dflash_pair";
        default: return "unknown";
    }
}

uint64_t session_read_v2_peak_bytes(uint64_t manifest_bytes, uint64_t regions) {
    // the manifest, the region directory and table, one 16 MiB transfer buffer, the small vectors
    return sat_add(sat_add(sat_add(manifest_bytes, kBlock), sat_mul(regions, 128)), uint64_t(1) << 20);
}

bool session_file_write_v2(const std::string& path, const SessionFileIdentity& id, const SessionManifestV2& manifest,
                           const std::vector<SessionRegionSource>& sources, size_t& bytes, std::string& error,
                           const SessionWriteOptions& options, SessionStatus* status) {
    SessionStatus st;
    bool ok = false;
    try {
        ok = write_v2_impl(path, id, manifest, sources, bytes, error, options, st);
    } catch (const std::bad_alloc&) {
        error = "session file v2: out of memory while writing " + path;
        st.error = SessionError::memory;
        ok = false;
    }
    if (status) *status = st;
    return ok;
}

bool session_file_read_v2(const std::string& path, const SessionFileIdentity& id, const SessionV2Requirement& want,
                          SessionManifestV2& manifest, size_t& bytes, std::string& error,
                          const SessionReadV2Limits& limits, SessionRegionSink* sink, SessionStatus* status) {
    SessionStatus st;
    bool ok = false;
    try {
        ok = read_v2_impl(path, id, want, manifest, bytes, error, limits, sink, st);
    } catch (const std::bad_alloc&) {
        error = "session file v2: out of memory while reading " + path;
        st.error = SessionError::memory;
        ok = false;
    }
    if (status) *status = st;
    return ok;
}

// ---- the checkpoint-boundary ring sidecar (lane 2b: paired draft capture) ----
namespace {
constexpr char kRingMagic[8] = {'S', 'T', 'R', 'D', 'R', 'I', 'N', '2'};
constexpr size_t kRingHeader = 88;                 // magic, version, size, tokens, 5 counters, bytes, checksum, hash
constexpr uint64_t kRingMaxBytes = 64ull << 20;    // the sidecar is ring-sized; this is its hard cap
} // namespace

bool session_ring_sidecar_write(const std::string& path, const DFlashSnapshotMeta& meta, int64_t tokens,
                                const void* bytes, uint64_t n, std::string& error) {
    error.clear();
    if (n > kRingMaxBytes) {
        error = "ring sidecar: " + std::to_string(n) + " bytes over the " + std::to_string(kRingMaxBytes >> 20) +
                " MiB cap";
        return false;
    }
    uint8_t h[kRingHeader];
    std::memset(h, 0, sizeof h);
    std::memcpy(h, kRingMagic, 8);
    const uint32_t version = 2, hsize = (uint32_t) kRingHeader;
    const uint64_t checksum = session_hash64(bytes, n, 0);
    std::memcpy(h + 8, &version, 4);
    std::memcpy(h + 12, &hsize, 4);
    std::memcpy(h + 16, &tokens, 8);
    std::memcpy(h + 24, &meta.committed_end, 8);
    std::memcpy(h + 32, &meta.appended_through, 8);
    std::memcpy(h + 40, &meta.ring_cells, 8);
    std::memcpy(h + 48, &meta.window, 8);
    std::memcpy(h + 56, &meta.capacity, 8);
    std::memcpy(h + 64, &n, 8);
    std::memcpy(h + 72, &checksum, 8);
    const uint64_t hh = session_hash64(h, kRingHeader - 8, 0);
    std::memcpy(h + 80, &hh, 8);
    std::ofstream f(std::filesystem::path(path), std::ios::binary | std::ios::trunc);
    if (!f) { error = "ring sidecar: cannot create " + path; return false; }
    f.write(reinterpret_cast<const char*>(h), (std::streamsize) kRingHeader);
    if (n) f.write(static_cast<const char*>(bytes), (std::streamsize) n);
    f.flush();
    if (!f) {
        f.close();
        std::error_code rec;
        std::filesystem::remove(std::filesystem::path(path), rec);
        error = "ring sidecar: writing " + path + " failed";
        return false;
    }
    f.close();
    return !f.fail();
}

bool session_ring_sidecar_read(const std::string& path, const DFlashSnapshotMeta& want, int64_t tokens,
                               std::vector<uint8_t>& out, std::string& error) {
    error.clear();
    const auto refuse = [&](const std::string& what) {
        error = "ring sidecar: " + what;
        return false;
    };
    std::ifstream f(std::filesystem::path(path), std::ios::binary);
    if (!f) return refuse("cannot open " + path);
    uint8_t h[kRingHeader];
    f.read(reinterpret_cast<char*>(h), (std::streamsize) kRingHeader);
    if (f.gcount() != (std::streamsize) kRingHeader) return refuse(path + " is shorter than its header");
    if (std::memcmp(h, kRingMagic, 8) != 0) return refuse(path + " is not a ring sidecar (magic)");
    uint32_t version = 0, hsize = 0;
    std::memcpy(&version, h + 8, 4);
    std::memcpy(&hsize, h + 12, 4);
    if (version != 2 || hsize != kRingHeader) return refuse(path + " has a version this engine does not read");
    uint64_t hh = 0;
    std::memcpy(&hh, h + 80, 8);
    if (hh != session_hash64(h, kRingHeader - 8, 0)) return refuse(path + " header is corrupt");
    int64_t tokens_file = 0;
    DFlashSnapshotMeta m;
    std::memcpy(&tokens_file, h + 16, 8);
    std::memcpy(&m.committed_end, h + 24, 8);
    std::memcpy(&m.appended_through, h + 32, 8);
    std::memcpy(&m.ring_cells, h + 40, 8);
    std::memcpy(&m.window, h + 48, 8);
    std::memcpy(&m.capacity, h + 56, 8);
    uint64_t n = 0, checksum = 0;
    std::memcpy(&n, h + 64, 8);
    std::memcpy(&checksum, h + 72, 8);
    if (tokens_file != tokens) return refuse(path + " captured " + std::to_string(tokens_file) +
                                             " tokens, the checkpoint sits at " + std::to_string(tokens));
    if (m.committed_end != want.committed_end || m.appended_through != want.appended_through ||
        m.ring_cells != want.ring_cells || m.window != want.window || m.capacity != want.capacity)
        return refuse(path + " counters are not the pair's");
    if (n > kRingMaxBytes) return refuse(path + " claims " + std::to_string(n) + " bytes, over the cap");
    std::vector<uint8_t> data(n);
    if (n) {
        f.read(reinterpret_cast<char*>(data.data()), (std::streamsize) n);
        if ((uint64_t) f.gcount() != n) return refuse(path + " is truncated inside its ring bytes");
    }
    char extra = 0;
    f.read(&extra, 1);
    if (f.gcount() == 1) return refuse(path + " has trailing bytes");
    if (checksum != session_hash64(data.data(), n, 0)) return refuse(path + " ring bytes do not match their checksum");
    out = std::move(data);
    return true;
}

} // namespace strata::core
