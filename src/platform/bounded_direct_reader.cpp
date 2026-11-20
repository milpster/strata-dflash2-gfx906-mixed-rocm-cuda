// src/platform/bounded_direct_reader.cpp - see include/strata/platform/bounded_direct_reader.hpp.
#include "strata/platform/bounded_direct_reader.hpp"

#include <algorithm>
#include <cerrno>
#include <cstring>
#include <limits>

#if defined(__linux__)
#include <sys/types.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/stat.h>
#endif

namespace strata::platform {

namespace {

#if defined(__linux__)
/// Conservative alignment: max(4096, Linux page size), power-of-two.
uint64_t reader_alignment() noexcept {
    const long page = ::sysconf(_SC_PAGESIZE);
    if (page <= 0) return 0;
    const uint64_t p = std::max(uint64_t{4096}, static_cast<uint64_t>(page));
    if ((p & (p - 1)) != 0) return 0;
    return p;
}

/// pread that retries on EINTR. Returns -1 with errno set on real failure.
ssize_t pread_retry(int fd, void* buf, size_t count, off_t offset) noexcept {
    for (;;) {
        const ssize_t got = ::pread(fd, buf, count, offset);
        if (got >= 0 || errno != EINTR) return got;
    }
}
#endif

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
    if (align == 0) {
        ::close(fd);
        return std::make_error_code(std::errc::operation_not_supported);
    }
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
    }
#endif
    fd_ = -1;
    size_ = 0;
    alignment_ = 0;
}

std::error_code BoundedDirectReader::read_exact(uint64_t offset, uint64_t bytes,
                                                uint8_t* dst, uint8_t* scratch,
                                                uint64_t scratch_bytes) const noexcept {
#if !defined(__linux__)
    (void) offset; (void) bytes; (void) dst; (void) scratch; (void) scratch_bytes;
    return std::make_error_code(std::errc::operation_not_supported);
#else
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
    if (end > static_cast<uint64_t>(std::numeric_limits<off_t>::max()))
        return std::make_error_code(errc::result_out_of_range);

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
    const uint64_t max_io = static_cast<uint64_t>(std::numeric_limits<ssize_t>::max()) / align * align;
    const uint64_t io_cap = std::min(scratch_bytes, max_io);
    if (io_cap == 0) return std::make_error_code(errc::result_out_of_range);
    uint64_t src_off = offset;
    uint8_t* d = dst;
    uint64_t rem = bytes;

    while (rem > 0) {
        // Aligned covering window for src_off; length <= scratch cap (aligned).
        const uint64_t win_start = src_off - (src_off % align);
        const uint64_t off_in_win = src_off - win_start;
        const uint64_t take = std::min(rem, io_cap - off_in_win);
        const uint64_t needed = off_in_win + take;
        const uint64_t win_len = needed + (align - needed % align) % align;

        // The offset fits off_t; the aligned count fits ssize_t and the scratch.
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
#endif
}

}  // namespace strata::platform
