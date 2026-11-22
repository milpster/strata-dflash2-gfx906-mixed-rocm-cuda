// include/strata/platform/bounded_direct_reader.hpp - synchronous bounded
// immutable file reader over O_DIRECT preads for the RAM-pool expert-cache
// slice.
//
// Contract: one fd, O_RDONLY|O_DIRECT|O_CLOEXEC on Linux; fstat requires a
// regular file with a nonnegative size.  Offsets and lengths need not be page
// aligned: a caller-supplied scratch (aligned, at least `alignment()` bytes)
// absorbs the page-aligned covering windows and exact slices are copied into
// the (possibly unaligned) destination.  No pimpl, no metadata allocation,
// no buffered fallback, no thread pool, no retained state beyond the scalar
// fd/size/alignment.  Synchronous, scratch/caller lifetime only.
// The caller keeps the file immutable externally and serializes shared scratch;
// a read-only descriptor cannot prevent another process from writing the file.
//
// Error contract: open/fstat failure is transactional - a previous opened file
// is preserved.  Invalid read_exact requests return an error without touching
// dst or scratch.  On an actual I/O failure dst may be partially changed; the
// consumer (cache) never publishes a partial buffer.
#pragma once

#include <cstdint>
#include <system_error>

namespace strata::platform {

/// Synchronous bounded immutable file reader feeding an existing pool cache
/// with caller scratch from the SAME pool.  Noncopyable, movable.
class BoundedDirectReader {
public:
    BoundedDirectReader() noexcept = default;
    ~BoundedDirectReader() noexcept { close(); }
    BoundedDirectReader(const BoundedDirectReader&) = delete;
    BoundedDirectReader& operator=(const BoundedDirectReader&) = delete;
    BoundedDirectReader(BoundedDirectReader&& other) noexcept
        : fd_(other.fd_), size_(other.size_), alignment_(other.alignment_) {
        other.fd_ = -1;
        other.size_ = 0;
        other.alignment_ = 0;
    }
    BoundedDirectReader& operator=(BoundedDirectReader&& other) noexcept {
        if (this != &other) {
            close();
            fd_ = other.fd_;
            size_ = other.size_;
            alignment_ = other.alignment_;
            other.fd_ = -1;
            other.size_ = 0;
            other.alignment_ = 0;
        }
        return *this;
    }

    /// Open `path` O_RDONLY|O_DIRECT|O_CLOEXEC (Linux).  fstat requires a
    /// regular file of nonnegative size.  Failure is transactional: a
    /// previously opened file stays open.  Non-Linux returns
    /// std::errc::operation_not_supported without touching the file system.
    std::error_code open(const char* path) noexcept;

    /// Close the fd (idempotent, no-op when not open).
    void close() noexcept;

    bool is_open() const noexcept { return fd_ >= 0; }
    uint64_t size() const noexcept { return size_; }
    /// Conservative alignment = max(4096, Linux page size), power-of-two.
    uint64_t alignment() const noexcept { return alignment_; }

    /// Read exactly `bytes` from `offset` into `dst` using `scratch` (aligned,
    /// capacity >= alignment and a multiple of alignment) as the O_DIRECT
    /// covering-window buffer.  Unaligned offset/length/dst are valid: exact
    /// slices are copied out of scratch.  Returns error before any I/O on an
    /// invalid request, leaving dst and scratch untouched.  On I/O failure dst
    /// may be partially changed.
    std::error_code read_exact(uint64_t offset, uint64_t bytes,
                               uint8_t* dst, uint8_t* scratch,
                               uint64_t scratch_bytes) const noexcept;

private:
    int fd_ = -1;
    uint64_t size_ = 0;
    uint64_t alignment_ = 0;
};

}  // namespace strata::platform
