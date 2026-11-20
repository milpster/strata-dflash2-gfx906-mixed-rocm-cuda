// src/platform/bounded_direct_reader.cpp - see include/strata/platform/bounded_direct_reader.hpp.
#include "strata/platform/bounded_direct_reader.hpp"

#include <cerrno>
#include <cstring>
#include <sys/types.h>
#include <unistd.h>

#if defined(__linux__)
#include <fcntl.h>
#include <sys/stat.h>
#endif

namespace strata::platform {

namespace {

/// Conservative alignment: max(4096, Linux page size), power-of-two.
uint64_t reader_alignment() noexcept {
#if defined(__linux__)
    const long page = ::sysconf(_SC_PAGESIZE);
    uint64_t p = (page > 0) ? static_cast<uint64_t>(page) : 4096ull;
#else
    uint64_t p = 4096ull;
#endif
    if (p < 4096ull) p = 4096ull;
    return p;
}

/// pread that retries on EINTR. Returns -1 with errno set on real failure.
ssize_t pread_retry(int fd, void* buf, size_t count, off_t offset) noexcept {
    for (;;) {
        const ssize_t got = ::pread(fd, buf, count, offset);
        if (got >= 0 || errno != EINTR) return got;
    }
}

}  // namespace

std::error_code BoundedDirectReader::open(const char* path) noexcept {
    if (path == nullptr)
        return std::make_error_code(std::errc::invalid_argument);

#if !defined(__linux__)
    // No native Windows/Mac claim: O_DIRECT is Linux-only here.
    return std::make_error_code(std::errc::operation_not_supported);
#else
    // Transactional replace: open + fstat the new file first; only take it on
    // full success so a previous opened file is preserved on any failure.
    int fd = ::open(path, O_RDONLY | O_DIRECT | O_CLOEXEC);
    if (fd < 0)
        return std::error_code(errno, std::generic_category());

    struct stat st;
    if (::fstat(fd, &st) != 0) {
        const int saved = errno;
        ::close(fd);
        return std::error_code(saved, std::generic_category());
    }
    if (!S_ISREG(st.st_mode) || st.st_size < 0) {
        ::close(fd);
        return std::make_error_code(std::errc::not_supported);
    }

    // alignment_ is set only after the new file is fully validated.
    const uint64_t align = reader_alignment();
    close();
    fd_ = fd;
    size_ = static_cast<uint64_t>(st.st_size);
    alignment_ = align;
    return {};
#endif
}

void BoundedDirectReader::close() noexcept {
#if defined(__linux__)
    if (fd_ >= 0) {
        ::close(fd_);
        fd_ = -1;
    }
#endif
    size_ = 0;
    alignment_ = 0;
}

std::error_code BoundedDirectReader::read_exact(uint64_t offset, uint64_t bytes,
                                                uint8_t* dst, uint8_t* scratch,
                                                uint64_t scratch_bytes) const noexcept {
    using std::errc;

    if (!is_open()) return std::make_error_code(errc::not_connected);   // bad fd / not open
    if (bytes == 0) return std::make_error_code(errc::invalid_argument);
    if (dst == nullptr || scratch == nullptr)
        return std::make_error_code(errc::invalid_argument);
    if (alignment_ == 0) return std::make_error_code(errc::not_supported);

    // size_t / address / offset overflow.
    if (bytes > SIZE_MAX - reinterpret_cast<uintptr_t>(dst))
        return std::make_error_code(errc::result_out_of_range);
    if (scratch_bytes > SIZE_MAX - reinterpret_cast<uintptr_t>(scratch))
        return std::make_error_code(errc::result_out_of_range);
    if (bytes > UINT64_MAX - offset)
        return std::make_error_code(errc::result_out_of_range);
    const uint64_t end = offset + bytes;
    if (end > size_)
        return std::make_error_code(errc::invalid_argument);   // extent beyond file

    // Scratch: aligned, at least one alignment, multiple of alignment.
    if (scratch_bytes < alignment_ || (scratch_bytes % alignment_) != 0)
        return std::make_error_code(errc::invalid_argument);
    if (reinterpret_cast<uintptr_t>(scratch) % alignment_ != 0)
        return std::make_error_code(errc::invalid_argument);

    // Disjoint dest/scratch spans (both ends already overflow-checked).
    const uintptr_t d_begin = reinterpret_cast<uintptr_t>(dst);
    const uintptr_t d_end = d_begin + bytes;
    const uintptr_t s_begin = reinterpret_cast<uintptr_t>(scratch);
    const uintptr_t s_end = s_begin + scratch_bytes;
    if (!(d_end <= s_begin || s_end <= d_begin))
        return std::make_error_code(errc::invalid_argument);

    const uint64_t align = alignment_;
    uint64_t src_off = offset;
    uint8_t* d = dst;
    uint64_t rem = bytes;

    while (rem > 0) {
        // Aligned covering window for src_off; length <= scratch cap (aligned).
        const uint64_t win_start = src_off - (src_off % align);
        const uint64_t off_in_win = src_off - win_start;
        const uint64_t win_len = scratch_bytes;   // aligned multiple, <= cap
        uint64_t take = rem;
        if (take > win_len - off_in_win) take = win_len - off_in_win;

        // off_t range: win_start and win_len fit (extent <= size_ <= off_t max).
        const ssize_t got = pread_retry(fd_, scratch, (size_t)win_len,
                                        (off_t)win_start);
        if (got < 0)
            return std::error_code(errno, std::generic_category());  // EIO etc.

        // Need off_in_win + take bytes delivered; short means insufficient/EOF.
        if ((uint64_t)got < off_in_win + take)
            return std::make_error_code(errc::io_error);

        ::memcpy(d, scratch + off_in_win, take);
        d += take;
        src_off += take;
        rem -= take;
    }
    return {};
}

}  // namespace strata::platform
