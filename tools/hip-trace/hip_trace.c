/*
 * hip_trace.c - LD_PRELOAD interposer for HIP buffer lifecycle and host VM calls.
 *
 * It records, in one file, the ORDER of the HIP buffer-lifecycle calls plus the
 * host-side memory operations that can revoke a page translation, so that a run
 * can answer one question from evidence instead of code reading:
 *
 *   "was a device copy still outstanding when this host range was unmapped?"
 *
 * Build (no ROCm headers needed, so no ABI mismatch is possible):
 *   gcc -O2 -g -Wall -Wextra -fPIC -shared -pthread -o hip_trace.so hip_trace.c -ldl
 *
 * Properties this file keeps:
 *   - the real implementation's return value is returned unchanged and errno is
 *     restored, so the interposition is transparent;
 *   - no lock is ever held across a real call: the log mutex is taken only to
 *     format and write one line;
 *   - a symbol dlsym() cannot find is logged once and passed through;
 *   - logging is write(2) of a stack buffer: no malloc, no stdio buffer, no
 *     re-entry into an intercepted symbol.
 */

#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

/*
 * The few HIP types we touch, declared locally on purpose. hipError_t is an
 * enum of int size on every supported ABI; handles and pointers are void *.
 * Declaring them here means this shim builds and loads without ROCm headers.
 */
typedef int hipError_t;
typedef void *hipStream_t;
typedef void *hipEvent_t;

#define DEF_SYM(n) static void *sym_##n; static int tried_##n
DEF_SYM(hipHostRegister);
DEF_SYM(hipHostUnregister);
DEF_SYM(hipHostMalloc);
DEF_SYM(hipHostFree);
DEF_SYM(hipMalloc);
DEF_SYM(hipFree);
DEF_SYM(hipMemcpy);
DEF_SYM(hipMemcpyAsync);
DEF_SYM(hipMemcpyHtoD);
DEF_SYM(hipMemcpyHtoDAsync);
DEF_SYM(hipMemcpyDtoH);
DEF_SYM(hipMemcpyDtoHAsync);
DEF_SYM(hipMallocManaged);
DEF_SYM(hipMemAdvise);
DEF_SYM(hipStreamSynchronize);
DEF_SYM(hipDeviceSynchronize);
DEF_SYM(hipEventRecord);
DEF_SYM(hipStreamWaitEvent);
DEF_SYM(mmap);
DEF_SYM(munmap);
DEF_SYM(madvise);
DEF_SYM(posix_fadvise);

/* ------------------------------------------------------------------ state */

static pthread_mutex_t g_mu = PTHREAD_MUTEX_INITIALIZER;
static int g_state;                 /* 0 = not opened yet, 1 = open, 2 = off */
static int g_fd = -1;
static int g_mmap_enabled = 1;
static unsigned long long g_mmap_min;
static struct {
    unsigned long long lo, hi;      /* [lo, hi) */
    int active;
} g_watch;

/*
 * Proxy counter: number of async copies seen since the last successful
 * stream/device synchronisation or stream-wait-event. It is global, not per
 * stream - see the limits section of README.md.
 */
static unsigned long long g_async_since_sync;

static __thread int t_resolving;    /* guards re-entry while dlsym() runs */

/* ------------------------------------------------------- small utilities */

static unsigned long long now_us(void)
{
    struct timespec ts;

    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0)
        return 0;
    return (unsigned long long)ts.tv_sec * 1000000ULL +
           (unsigned long long)ts.tv_nsec / 1000ULL;
}

static int appf(char *b, size_t cap, int off, const char *fmt, ...)
{
    va_list ap;
    int n;

    if (off < 0 || (size_t)off >= cap)
        return -1;
    va_start(ap, fmt);
    n = vsnprintf(b + off, cap - (size_t)off, fmt, ap);
    va_end(ap);
    if (n < 0 || (size_t)n >= cap - (size_t)off)
        return -1;
    return off + n;
}

/* ------------------------------------------------------- symbol resolving */

static void log_open_locked(void);
static void emit(unsigned long long t0, long dur_us, const char *call,
                 int ret, int err, int watch, int watch_arg, const char *args);

/*
 * Resolve the next definition of name after this object. dlsym() may itself
 * allocate, which may call mmap(); t_resolving makes that nested call skip
 * resolution instead of recursing. The libc hooks below fall back to a raw
 * syscall when resolution is unavailable, so nothing is lost in that window.
 */
static void *resolve_next(const char *name)
{
    void *p;

    if (t_resolving)
        return NULL;
    t_resolving = 1;
    p = dlsym(RTLD_NEXT, name);
    t_resolving = 0;
    return p;
}

static void *real_sym(void **slot, int *tried, const char *name)
{
    char a[160];
    int off;

    if (*slot)
        return *slot;
    if (*tried)
        return NULL;
    *tried = 1;
    *slot = resolve_next(name);
    if (!*slot) {
        off = appf(a, sizeof a, 0, "symbol=%s note=no_symbol", name);
        emit(now_us(), 0, name, -1, ENOSYS, -1, 0, off > 0 ? a : NULL);
    }
    return *slot;
}

#define SYM(n) real_sym(&sym_##n, &tried_##n, #n)

/* ------------------------------------------------------------------- log */

static void log_open_locked(void)
{
    const char *v;
    const char *path;
    char hdr[512];
    int fd;
    int off;

    if (g_state)
        return;
    g_state = 2;                    /* disabled unless open(2) succeeds */

    path = getenv("HIP_TRACE_LOG");
    if (!path || !*path)
        path = "/tmp/opencode/hip-trace.log";
    fd = open(path, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0644);
    if (fd < 0)
        return;
    g_fd = fd;
    g_state = 1;

    v = getenv("HIP_TRACE_MMAP");
    if (v && v[0] == '0')
        g_mmap_enabled = 0;
    v = getenv("HIP_TRACE_MMAP_MIN");
    if (v && *v)
        g_mmap_min = strtoull(v, NULL, 0);

    v = getenv("HIP_TRACE_WATCH");
    if (v && *v) {
        unsigned long long lo = strtoull(v, NULL, 0);
        const char *colon = strchr(v, ':');
        unsigned long long sz = colon ? strtoull(colon + 1, NULL, 0) : 0;

        if (lo && sz) {
            g_watch.lo = lo;
            g_watch.hi = lo + sz;
            g_watch.active = 1;
        }
    }

    off = appf(hdr, sizeof hdr, 0,
               "# hip_trace 1 pid=%ld watch=%d [0x%llx,0x%llx) mmap=%d mmap_min=%llu\n",
               (long)getpid(), g_watch.active ? 1 : 0,
               g_watch.active ? g_watch.lo : 0ULL,
               g_watch.active ? g_watch.hi : 0ULL,
               g_mmap_enabled, g_mmap_min);
    if (off > 0)
        (void)!write(g_fd, hdr, (size_t)off);
}

/*
 * One line per call:
 *   <monotonic_us> <tid> <call> ret=<value> errno=<errno> dur_us=<call time> <args>[ watch=..]
 * The log mutex is held only here, never across a real call.
 */
static void emit(unsigned long long t0, long dur_us, const char *call,
                 int ret, int err, int watch, int watch_arg, const char *args)
{
    char line[1400];
    int off;

    pthread_mutex_lock(&g_mu);
    log_open_locked();
    if (g_state != 1) {
        pthread_mutex_unlock(&g_mu);
        return;
    }
    off = appf(line, sizeof line, 0, "%llu %ld %s ret=%d errno=%d dur_us=%ld",
               t0, (long)syscall(SYS_gettid), call, ret, err, dur_us);
    if (args && args[0])
        off = appf(line, sizeof line, off, " %s", args);
    if (watch == 1)
        off = appf(line, sizeof line, off, " watch=1 arg=%d", watch_arg);
    else if (watch == 0)
        off = appf(line, sizeof line, off, " watch=0");
    if (off > 0 && off < (int)sizeof line - 1) {
        line[off++] = '\n';
        (void)!write(g_fd, line, (size_t)off);
    }
    pthread_mutex_unlock(&g_mu);
}

/* ------------------------------------------------------- watch range test */

/* -1 = watch mode off, 0 = no overlap, 1 = overlap. A null pointer never hits. */
static int watch_check(const void *p, size_t n)
{
    unsigned long long a, b;

    if (!g_watch.active)
        return -1;
    if (!p)
        return 0;
    a = (unsigned long long)(uintptr_t)p;
    b = a + (unsigned long long)(n ? n : 1);
    if (b < a)
        b = ~0ULL;
    return (a < g_watch.hi && b > g_watch.lo) ? 1 : 0;
}

/* Two-pointer variant for memcpy-shaped calls. */
static int watch2(const void *p1, size_t n1, const void *p2, size_t n2, int *which)
{
    int a = watch_check(p1, n1);
    int b = watch_check(p2, n2);

    if (a == 1) {
        *which = 1;
        return 1;
    }
    if (b == 1) {
        *which = 2;
        return 1;
    }
    if (a < 0 && b < 0)
        return -1;
    *which = 0;
    return 0;
}

/* -------------------------------------------- live host-registration table */

/*
 * hipHostRegister() gives a host range to the driver; hipHostUnregister() takes
 * it back. Keeping the live set lets every munmap/madvise line say whether it
 * hit a range that is still registered - which is exactly the fault class.
 */
#define REG_SLOTS 1024
static struct {
    void *p;
    size_t n;
} g_reg[REG_SLOTS];
static unsigned g_reg_next;
static pthread_mutex_t g_reg_mu = PTHREAD_MUTEX_INITIALIZER;

static void reg_add(void *p, size_t n)
{
    unsigned i;

    if (!p || !n)
        return;
    pthread_mutex_lock(&g_reg_mu);
    i = g_reg_next++ % REG_SLOTS;
    g_reg[i].p = p;
    g_reg[i].n = n;
    pthread_mutex_unlock(&g_reg_mu);
}

/* Returns 1 and the recorded size when p was a live registration. */
static int reg_remove(void *p, size_t *out_n)
{
    unsigned i;
    int found = 0;

    pthread_mutex_lock(&g_reg_mu);
    for (i = 0; i < REG_SLOTS; i++) {
        if (g_reg[i].p == p && p) {
            if (out_n)
                *out_n = g_reg[i].n;
            g_reg[i].p = NULL;
            g_reg[i].n = 0;
            found = 1;
            break;
        }
    }
    pthread_mutex_unlock(&g_reg_mu);
    return found;
}

static int reg_count(void)
{
    unsigned i;
    int c = 0;

    pthread_mutex_lock(&g_reg_mu);
    for (i = 0; i < REG_SLOTS; i++)
        if (g_reg[i].p)
            c++;
    pthread_mutex_unlock(&g_reg_mu);
    return c;
}

/*
 * Count live registrations overlapping [p, p+n) and report the first one.
 * Deliberately does NOT clear anything: an overlap at munmap time is the
 * evidence we are collecting, not a bookkeeping fix.
 */
static int reg_overlap(const void *p, size_t n, void **hit_p, size_t *hit_n)
{
    unsigned long long a, b;
    unsigned i;
    int c = 0;

    if (hit_p)
        *hit_p = NULL;
    if (hit_n)
        *hit_n = 0;
    if (!p)
        return 0;
    a = (unsigned long long)(uintptr_t)p;
    b = a + (unsigned long long)(n ? n : 1);
    if (b < a)
        b = ~0ULL;
    pthread_mutex_lock(&g_reg_mu);
    for (i = 0; i < REG_SLOTS; i++) {
        unsigned long long ra, rb;

        if (!g_reg[i].p)
            continue;
        ra = (unsigned long long)(uintptr_t)g_reg[i].p;
        rb = ra + g_reg[i].n;
        if (a < rb && b > ra) {
            if (c == 0) {
                if (hit_p)
                    *hit_p = g_reg[i].p;
                if (hit_n)
                    *hit_n = g_reg[i].n;
            }
            c++;
        }
    }
    pthread_mutex_unlock(&g_reg_mu);
    return c;
}

static const char *kind_name(int k)
{
    switch (k) {
    case 0: return "H2H";
    case 1: return "H2D";
    case 2: return "D2H";
    case 3: return "D2D";
    case 4: return "DEFAULT";
    default: return "OTHER";
    }
}

/* ------------------------------------------------------------- HIP calls */

hipError_t hipHostRegister(void *ptr, size_t size, unsigned int flags)
{
    void *f = SYM(hipHostRegister);
    unsigned long long t0 = now_us();
    hipError_t rc = 0;
    long dur;
    int e;
    char a[512];
    int off = 0;

    if (f)
        rc = ((hipError_t (*)(void *, size_t, unsigned int))f)(ptr, size, flags);
    dur = (long)(now_us() - t0);
    e = errno;
    if (rc == 0)
        reg_add(ptr, size);
    off = appf(a, sizeof a, off, "ptr=%p size=%zu flags=0x%x", ptr, size, flags);
    emit(t0, dur, "hipHostRegister", (int)rc, e, watch_check(ptr, size), 1,
         off > 0 ? a : NULL);
    errno = e;
    return rc;
}

hipError_t hipHostUnregister(void *ptr)
{
    void *f = SYM(hipHostUnregister);
    unsigned long long t0 = now_us();
    hipError_t rc = 0;
    long dur;
    int e;
    size_t rn = 0;
    char a[512];
    char hit[128];
    void *hp = NULL;
    size_t hn = 0;
    int ov = 0;
    int off = 0;

    if (f)
        rc = ((hipError_t (*)(void *))f)(ptr);
    dur = (long)(now_us() - t0);
    e = errno;
    if (rc == 0)
        reg_remove(ptr, &rn);
    ov = reg_overlap(ptr, rn ? rn : 1, &hp, &hn);
    hit[0] = '\0';
    if (ov > 0)
        (void)snprintf(hit, sizeof hit, " next_hit=%p+%zu", hp, hn);
    off = appf(a, sizeof a, off,
               "ptr=%p reg_size=%zu live_regs=%d live_overlap=%d async_since_sync=%llu%s",
                ptr, rn, reg_count(), ov, g_async_since_sync, hit);
    emit(t0, dur, "hipHostUnregister", (int)rc, e, watch_check(ptr, rn ? rn : 1), 1,
         off > 0 ? a : NULL);
    errno = e;
    return rc;
}

hipError_t hipHostMalloc(void **ptr, size_t size, unsigned int flags)
{
    void *f = SYM(hipHostMalloc);
    unsigned long long t0 = now_us();
    hipError_t rc = 0;
    long dur;
    int e;
    char a[512];
    int off = 0;

    if (f)
        rc = ((hipError_t (*)(void **, size_t, unsigned int))f)(ptr, size, flags);
    dur = (long)(now_us() - t0);
    e = errno;
    off = appf(a, sizeof a, off, "ptr=%p out=%p size=%zu flags=0x%x", (void *)ptr,
               (rc == 0 && ptr) ? *ptr : NULL, size, flags);
    emit(t0, dur, "hipHostMalloc", (int)rc, e, watch_check((rc == 0 && ptr) ? *ptr : NULL, size),
         1, off > 0 ? a : NULL);
    errno = e;
    return rc;
}

hipError_t hipHostFree(void *ptr)
{
    void *f = SYM(hipHostFree);
    unsigned long long t0 = now_us();
    hipError_t rc = 0;
    long dur;
    int e;
    char a[512];
    int off = 0;

    if (f)
        rc = ((hipError_t (*)(void *))f)(ptr);
    dur = (long)(now_us() - t0);
    e = errno;
    off = appf(a, sizeof a, off, "ptr=%p live_overlap=%d async_since_sync=%llu", ptr,
               reg_overlap(ptr, 1, NULL, NULL), g_async_since_sync);
    emit(t0, dur, "hipHostFree", (int)rc, e, watch_check(ptr, 1), 1, off > 0 ? a : NULL);
    errno = e;
    return rc;
}

hipError_t hipMalloc(void **ptr, size_t size)
{
    void *f = SYM(hipMalloc);
    unsigned long long t0 = now_us();
    hipError_t rc = 0;
    long dur;
    int e;
    char a[512];
    int off = 0;

    if (f)
        rc = ((hipError_t (*)(void **, size_t))f)(ptr, size);
    dur = (long)(now_us() - t0);
    e = errno;
    off = appf(a, sizeof a, off, "ptr=%p out=%p size=%zu", (void *)ptr,
               (rc == 0 && ptr) ? *ptr : NULL, size);
    emit(t0, dur, "hipMalloc", (int)rc, e, -1, 0, off > 0 ? a : NULL);
    errno = e;
    return rc;
}

hipError_t hipMallocManaged(void **ptr, size_t size, unsigned int flags)
{
    void *f = SYM(hipMallocManaged);
    unsigned long long t0 = now_us();
    hipError_t rc = 0;
    long dur;
    int e;
    char a[512];
    int off = 0;

    if (f)
        rc = ((hipError_t (*)(void **, size_t, unsigned int))f)(ptr, size, flags);
    dur = (long)(now_us() - t0);
    e = errno;
    off = appf(a, sizeof a, off, "ptr=%p out=%p size=%zu flags=0x%x", (void *)ptr,
               (rc == 0 && ptr) ? *ptr : NULL, size, flags);
    emit(t0, dur, "hipMallocManaged", (int)rc, e,
         watch_check((rc == 0 && ptr) ? *ptr : NULL, size), 1, off > 0 ? a : NULL);
    errno = e;
    return rc;
}

hipError_t hipFree(void *ptr)
{
    void *f = SYM(hipFree);
    unsigned long long t0 = now_us();
    hipError_t rc = 0;
    long dur;
    int e;
    char a[512];
    int off = 0;

    if (f)
        rc = ((hipError_t (*)(void *))f)(ptr);
    dur = (long)(now_us() - t0);
    e = errno;
    off = appf(a, sizeof a, off, "ptr=%p async_since_sync=%llu", ptr, g_async_since_sync);
    emit(t0, dur, "hipFree", (int)rc, e, watch_check(ptr, 1), 1, off > 0 ? a : NULL);
    errno = e;
    return rc;
}

hipError_t hipMemAdvise(const void *ptr, size_t size, int advice, int device)
{
    void *f = SYM(hipMemAdvise);
    unsigned long long t0 = now_us();
    hipError_t rc = 0;
    long dur;
    int e;
    char a[512];
    int off = 0;

    if (f)
        rc = ((hipError_t (*)(const void *, size_t, int, int))f)(ptr, size, advice,
                                                                 device);
    dur = (long)(now_us() - t0);
    e = errno;
    off = appf(a, sizeof a, off, "ptr=%p size=%zu advice=%d device=%d", ptr, size,
               advice, device);
    emit(t0, dur, "hipMemAdvise", (int)rc, e, watch_check(ptr, size), 1,
         off > 0 ? a : NULL);
    errno = e;
    return rc;
}

hipError_t hipMemcpy(void *dst, const void *src, size_t size, int kind)
{
    void *f = SYM(hipMemcpy);
    unsigned long long t0 = now_us();
    hipError_t rc = 0;
    long dur;
    int e;
    int warg = 0;
    int w;
    char a[512];
    int off = 0;

    if (f)
        rc = ((hipError_t (*)(void *, const void *, size_t, int))f)(dst, src, size, kind);
    dur = (long)(now_us() - t0);
    e = errno;
    w = watch2(dst, size, src, size, &warg);
    off = appf(a, sizeof a, off, "dst=%p src=%p size=%zu kind=%d kindname=%s", dst, src,
               size, kind, kind_name(kind));
    emit(t0, dur, "hipMemcpy", (int)rc, e, w, warg, off > 0 ? a : NULL);
    errno = e;
    return rc;
}

static void note_async(void)
{
    g_async_since_sync++;
}

static void note_sync(void)
{
    g_async_since_sync = 0;
}

hipError_t hipMemcpyAsync(void *dst, const void *src, size_t size, int kind,
                          hipStream_t stream)
{
    void *f = SYM(hipMemcpyAsync);
    unsigned long long t0 = now_us();
    hipError_t rc = 0;
    long dur;
    int e;
    int warg = 0;
    int w;
    char a[512];
    int off = 0;

    if (f)
        rc = ((hipError_t (*)(void *, const void *, size_t, int, hipStream_t))f)(
            dst, src, size, kind, stream);
    dur = (long)(now_us() - t0);
    e = errno;
    if (rc == 0)
        note_async();
    w = watch2(dst, size, src, size, &warg);
    off = appf(a, sizeof a, off,
               "dst=%p src=%p size=%zu kind=%d kindname=%s stream=%p async_since_sync=%llu",
               dst, src, size, kind, kind_name(kind), stream, g_async_since_sync);
    emit(t0, dur, "hipMemcpyAsync", (int)rc, e, w, warg, off > 0 ? a : NULL);
    errno = e;
    return rc;
}

#define DEF_PLAIN_MEMCPY(name, argtype, has_stream)                              \
    hipError_t name(void *dst, const void *src, size_t size)                     \
    {                                                                            \
        void *f = SYM(name);                                                     \
        unsigned long long t0 = now_us();                                        \
        hipError_t rc = 0;                                                       \
        long dur;                                                                \
        int e;                                                                   \
        int warg = 0;                                                            \
        int w;                                                                   \
        char a[512];                                                             \
        int off = 0;                                                             \
                                                                                 \
        if (f)                                                                   \
            rc = ((hipError_t (*)(void *, const void *, size_t))f)(dst, src, size); \
        dur = (long)(now_us() - t0);                                             \
        e = errno;                                                               \
        w = watch2(dst, size, src, size, &warg);                                 \
        off = appf(a, sizeof a, off, "dst=%p src=%p size=%zu", dst, src, size);   \
        emit(t0, dur, #name, (int)rc, e, w, warg, off > 0 ? a : NULL);            \
        errno = e;                                                               \
        return rc;                                                               \
    }
#define DEF_ASYNC_MEMCPY(name)                                                   \
    hipError_t name(void *dst, const void *src, size_t size, hipStream_t stream)  \
    {                                                                            \
        void *f = SYM(name);                                                     \
        unsigned long long t0 = now_us();                                        \
        hipError_t rc = 0;                                                       \
        long dur;                                                                \
        int e;                                                                   \
        int warg = 0;                                                            \
        int w;                                                                   \
        char a[512];                                                             \
        int off = 0;                                                             \
                                                                                 \
        if (f)                                                                   \
            rc = ((hipError_t (*)(void *, const void *, size_t, hipStream_t))f)(  \
                dst, src, size, stream);                                         \
        dur = (long)(now_us() - t0);                                             \
        e = errno;                                                               \
        if (rc == 0)                                                             \
            note_async();                                                        \
        w = watch2(dst, size, src, size, &warg);                                 \
        off = appf(a, sizeof a, off, "dst=%p src=%p size=%zu stream=%p async_since_sync=%llu", \
                   dst, src, size, stream, g_async_since_sync);                  \
        emit(t0, dur, #name, (int)rc, e, w, warg, off > 0 ? a : NULL);            \
        errno = e;                                                               \
        return rc;                                                               \
    }

DEF_PLAIN_MEMCPY(hipMemcpyHtoD, void *, 0)
DEF_PLAIN_MEMCPY(hipMemcpyDtoH, void *, 0)
DEF_ASYNC_MEMCPY(hipMemcpyHtoDAsync)
DEF_ASYNC_MEMCPY(hipMemcpyDtoHAsync)

hipError_t hipStreamSynchronize(hipStream_t stream)
{
    void *f = SYM(hipStreamSynchronize);
    unsigned long long t0 = now_us();
    unsigned long long before = g_async_since_sync;
    hipError_t rc = 0;
    long dur;
    int e;
    char a[512];
    int off = 0;

    if (f)
        rc = ((hipError_t (*)(hipStream_t))f)(stream);
    dur = (long)(now_us() - t0);
    e = errno;
    if (rc == 0)
        note_sync();
    off = appf(a, sizeof a, off, "stream=%p async_before=%llu", stream, before);
    emit(t0, dur, "hipStreamSynchronize", (int)rc, e, -1, 0, off > 0 ? a : NULL);
    errno = e;
    return rc;
}

hipError_t hipDeviceSynchronize(void)
{
    void *f = SYM(hipDeviceSynchronize);
    unsigned long long t0 = now_us();
    unsigned long long before = g_async_since_sync;
    hipError_t rc = 0;
    long dur;
    int e;
    char a[512];
    int off = 0;

    if (f)
        rc = ((hipError_t (*)(void))f)();
    dur = (long)(now_us() - t0);
    e = errno;
    if (rc == 0)
        note_sync();
    off = appf(a, sizeof a, off, "device=all async_before=%llu", before);
    emit(t0, dur, "hipDeviceSynchronize", (int)rc, e, -1, 0, off > 0 ? a : NULL);
    errno = e;
    return rc;
}

hipError_t hipEventRecord(hipEvent_t event, hipStream_t stream)
{
    void *f = SYM(hipEventRecord);
    unsigned long long t0 = now_us();
    hipError_t rc = 0;
    long dur;
    int e;
    char a[512];
    int off = 0;

    if (f)
        rc = ((hipError_t (*)(hipEvent_t, hipStream_t))f)(event, stream);
    dur = (long)(now_us() - t0);
    e = errno;
    off = appf(a, sizeof a, off, "event=%p stream=%p", event, stream);
    emit(t0, dur, "hipEventRecord", (int)rc, e, -1, 0, off > 0 ? a : NULL);
    errno = e;
    return rc;
}

hipError_t hipStreamWaitEvent(hipStream_t stream, hipEvent_t event, unsigned int flags)
{
    void *f = SYM(hipStreamWaitEvent);
    unsigned long long t0 = now_us();
    hipError_t rc = 0;
    long dur;
    int e;
    char a[512];
    int off = 0;

    if (f)
        rc = ((hipError_t (*)(hipStream_t, hipEvent_t, unsigned int))f)(stream, event,
                                                                        flags);
    dur = (long)(now_us() - t0);
    e = errno;
    if (rc == 0)
        note_sync();
    off = appf(a, sizeof a, off, "stream=%p event=%p flags=0x%x", stream, event, flags);
    emit(t0, dur, "hipStreamWaitEvent", (int)rc, e, -1, 0, off > 0 ? a : NULL);
    errno = e;
    return rc;
}

/* ------------------------------------------------------------ libc hooks */

static int mmap_skip(size_t len)
{
    if (!g_mmap_enabled)
        return 1;
    return (unsigned long long)len < g_mmap_min;
}

void *mmap(void *addr, size_t len, int prot, int flags, int fd, off_t off)
{
    void *f = SYM(mmap);
    unsigned long long t0 = now_us();
    void *rc;
    long dur;
    int e;
    char a[512];
    int ao = 0;
    int skip = mmap_skip(len);

    if (f)
        rc = ((void *(*)(void *, size_t, int, int, int, off_t))f)(addr, len, prot, flags,
                                                                  fd, off);
    else
        rc = (void *)syscall(SYS_mmap, addr, len, prot, flags, fd, off); /* recursion window */
    dur = (long)(now_us() - t0);
    e = errno;
    if (!skip) {
        ao = appf(a, sizeof a, ao, "addr=%p retp=%p len=%zu prot=0x%x flags=0x%x fd=%d off=%lld",
                  addr, rc, len, prot, flags, fd, (long long)off);
        emit(t0, dur, "mmap", rc == MAP_FAILED ? -1 : 0, e, watch_check(rc, len), 1,
             ao > 0 ? a : NULL);
    }
    errno = e;
    return rc;
}

int munmap(void *addr, size_t len)
{
    void *f = SYM(munmap);
    unsigned long long t0 = now_us();
    int rc;
    long dur;
    int e;
    char a[512];
    char hit[128];
    int ao = 0;
    int skip = mmap_skip(len);
    void *hp = NULL;
    size_t hn = 0;
    int ov = 0;

    if (f)
        rc = ((int (*)(void *, size_t))f)(addr, len);
    else
        rc = (int)syscall(SYS_munmap, addr, len);
    dur = (long)(now_us() - t0);
    e = errno;
    if (!skip) {
        ov = reg_overlap(addr, len, &hp, &hn);
        hit[0] = '\0';
        if (ov > 0)
            (void)snprintf(hit, sizeof hit, " live_reg0=%p+%zu", hp, hn);
        ao = appf(a, sizeof a, ao,
                  "addr=%p len=%zu live_overlap=%d live_regs=%d async_since_sync=%llu%s",
                  addr, len, ov, reg_count(), g_async_since_sync, hit);
        emit(t0, dur, "munmap", rc, e, watch_check(addr, len), 1, ao > 0 ? a : NULL);
    }
    errno = e;
    return rc;
}

int madvise(void *addr, size_t len, int advice)
{
    void *f = SYM(madvise);
    unsigned long long t0 = now_us();
    int rc;
    long dur;
    int e;
    char a[512];
    char hit[128];
    int ao = 0;
    int skip = mmap_skip(len);
    void *hp = NULL;
    size_t hn = 0;
    int ov = 0;

    if (f)
        rc = ((int (*)(void *, size_t, int))f)(addr, len, advice);
    else
        rc = (int)syscall(SYS_madvise, addr, len, advice);
    dur = (long)(now_us() - t0);
    e = errno;
    if (!skip) {
        ov = reg_overlap(addr, len, &hp, &hn);
        hit[0] = '\0';
        if (ov > 0)
            (void)snprintf(hit, sizeof hit, " live_reg0=%p+%zu", hp, hn);
        ao = appf(a, sizeof a, ao,
                  "addr=%p len=%zu advice=%d live_overlap=%d live_regs=%d async_since_sync=%llu%s",
                  addr, len, advice, ov, reg_count(), g_async_since_sync, hit);
        emit(t0, dur, "madvise", rc, e, watch_check(addr, len), 1, ao > 0 ? a : NULL);
    }
    errno = e;
    return rc;
}

int posix_fadvise(int fd, off_t off, off_t len, int advice)
{
    void *f = SYM(posix_fadvise);
    unsigned long long t0 = now_us();
    int rc;
    long dur;
    int e;
    char a[512];
    int ao = 0;

    if (f)
        rc = ((int (*)(int, off_t, off_t, int))f)(fd, off, len, advice);
#ifdef SYS_fadvise64
    else
        rc = (int)syscall(SYS_fadvise64, fd, off, len, advice);
#else
    else
        rc = ENOSYS;
#endif
    dur = (long)(now_us() - t0);
    e = errno;
    ao = appf(a, sizeof a, ao, "fd=%d off=%lld len=%lld advice=%d", fd, (long long)off,
              (long long)len, advice);
    emit(t0, dur, "posix_fadvise", rc, e, -1, 0, ao > 0 ? a : NULL);
    errno = e;
    return rc;
}

/* -------------------------------------------------------------- start-up */

/*
 * Open the log before main() so the watch range is live from the first call,
 * and so a crash mid-run still leaves a file.
 */
__attribute__((constructor)) static void hip_trace_ctor(void)
{
    pthread_mutex_lock(&g_mu);
    log_open_locked();
    pthread_mutex_unlock(&g_mu);
}
