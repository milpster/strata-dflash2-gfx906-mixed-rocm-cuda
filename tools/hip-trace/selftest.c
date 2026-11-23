/*
 * selftest.c - CPU-only proof that hip_trace.so captures ordering, reports
 * return codes and arguments, and stays transparent.
 *
 * No GPU, no ROCm, no driver: the HIP calls land in fakehip.so and the host VM
 * calls land in libc. Nothing here touches /dev/kfd or an engine.
 *
 * Run it with no arguments (it re-executes itself twice under LD_PRELOAD):
 *
 *   ./selftest            # phase 1: ordering + pass-through
 *                         # phase 2: HIP_TRACE_WATCH overlap reporting
 *
 * Exit status 0 means every check passed.
 */

#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

#ifndef MAP_FIXED_NOREPLACE
#define MAP_FIXED_NOREPLACE 0x100000
#endif

/*
 * A fixed mapping address, so the watch range computed in phase 1 is the same
 * range phase 2 actually maps. Unusual enough not to collide with a loaded
 * library; if it does, the test says so and stops.
 */
#define SELFTEST_MAP_ADDR 0x6f5a00000000ULL

#define TEST_DIR_DEFAULT "/tmp/opencode/hip-trace-selftest"
#define LOG_NAME_1 "selftest.log"
#define LOG_NAME_2 "selftest-watch.log"

/* Filled in by fakehip.so when a call reaches it (pass-through evidence). */
extern int fake_hip_call_count;
extern unsigned long long fake_hip_registered_bytes;
extern void *fake_hip_last_registered;
extern void *fake_hip_last_memcpy_dst;
extern const void *fake_hip_last_memcpy_src;
extern unsigned long long fake_hip_last_memcpy_size;
extern int fake_hip_last_memcpy_kind;
extern int fake_hip_unregister_count;
extern int fake_hip_sync_count;

/* Real HIP prototypes, called from here but served by hip_trace.so first. */
int hipHostRegister(void *p, size_t n, unsigned int flags);
int hipHostUnregister(void *p);
int hipMalloc(void **p, size_t n);
int hipFree(void *p);
int hipMemcpyAsync(void *dst, const void *src, size_t n, int kind, void *stream);
int hipStreamSynchronize(void *stream);

static int g_fail;
static char g_log[512 * 1024];
static char g_dir[4096];
static char g_log_path[4300];

static void ok(int cond, const char *what)
{
    printf("  %s  %s\n", cond ? "PASS" : "FAILED", what);
    if (!cond)
        g_fail++;
}

static int exe_dir(char *out, size_t cap)
{
    ssize_t n = readlink("/proc/self/exe", out, cap - 1);
    char *slash;

    if (n <= 0)
        return -1;
    out[n] = '\0';
    slash = strrchr(out, '/');
    if (!slash)
        return -1;
    *slash = '\0';
    return 0;
}

static const char *test_dir(void)
{
    const char *td = getenv("HIPTRACE_SELFTEST_DIR");

    return (td && *td) ? td : TEST_DIR_DEFAULT;
}

/* Point LD_PRELOAD, HIP_TRACE_LOG and (optionally) HIP_TRACE_WATCH at this run. */
static int prepare_env(const char *logname, const char *watch)
{
    char so[4300];
    char dir[4096];

    snprintf(g_dir, sizeof g_dir, "%s", test_dir());
    if (mkdir(g_dir, 0755) != 0 && errno != EEXIST) {
        perror("mkdir");
        return -1;
    }
    if (exe_dir(dir, sizeof dir) != 0) {
        fprintf(stderr, "selftest: cannot locate own directory\n");
        return -1;
    }
    snprintf(so, sizeof so, "%s/hip_trace.so", dir);
    if (access(so, R_OK) != 0) {
        fprintf(stderr, "selftest: %s is missing - build it with make\n", so);
        return -1;
    }
    snprintf(g_log_path, sizeof g_log_path, "%s/%s", g_dir, logname);
    if (unlink(g_log_path) != 0 && errno != ENOENT) {
        perror("unlink log");
        return -1;
    }
    setenv("LD_PRELOAD", so, 1);
    setenv("HIP_TRACE_LOG", g_log_path, 1);
    if (watch)
        setenv("HIP_TRACE_WATCH", watch, 1);
    else
        unsetenv("HIP_TRACE_WATCH");
    return 0;
}

static int reexec(const char *phase)
{
    fflush(NULL);                       /* execv does not flush stdio buffers */
    setenv("HIPTRACE_SELFTEST_PHASE", phase, 1);
    execv("/proc/self/exe", (char *const[]){ (char *)"/proc/self/exe", NULL });
    /* only reached on failure, and the preload is gone from this exec'd image */
    perror("execv /proc/self/exe");
    return 2;
}

/*
 * The sequence under test. Deliberately pathological, and harmless here: the
 * host range is unmapped while the (fake) registration is still live and one
 * async copy is still outstanding - the ordering the engine hit on hardware.
 */
static int run_sequence(void)
{
    size_t psz = (size_t)sysconf(_SC_PAGESIZE);
    size_t total = 3 * psz;
    void *base;
    void *host;
    void *dev = NULL;
    char tmp[4200];
    int fd;
    int rc = 0;

    base = mmap((void *)(uintptr_t)SELFTEST_MAP_ADDR, total,
                PROT_READ | PROT_WRITE,
                MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED_NOREPLACE, -1, 0);
    if (base == MAP_FAILED) {
        fprintf(stderr,
                "selftest: mmap at 0x%llx failed: %s - edit SELFTEST_MAP_ADDR\n",
                (unsigned long long)SELFTEST_MAP_ADDR, strerror(errno));
        return -1;
    }
    memset(base, 0x5a, total);          /* make the range resident */
    host = (char *)base + psz;

    rc |= hipHostRegister(host, psz, 1u);
    rc |= hipMalloc(&dev, psz);
    rc |= hipMemcpyAsync(dev, host, psz, 1 /* H2D */, NULL);

    /* Host-side operations that can revoke a translation. */
    if (madvise(host, psz, MADV_DONTNEED) != 0)
        perror("madvise");
    snprintf(tmp, sizeof tmp, "%s/fadvise-probe.bin", test_dir());
    fd = open(tmp, O_RDWR | O_CREAT | O_TRUNC, 0600);
    if (fd < 0) {
        perror("open fadvise probe");
        return -1;
    }
    if (write(fd, "x", 1) != 1)
        perror("write probe");
    if (posix_fadvise(fd, 0, (off_t)psz, POSIX_FADV_DONTNEED) != 0)
        perror("posix_fadvise");
    close(fd);

    /* Unmap first, unregister only afterwards: the pathological order. */
    if (munmap(base, total) != 0)
        perror("munmap");
    rc |= hipStreamSynchronize(NULL);
    rc |= hipHostUnregister(host);
    rc |= hipFree(dev);

    /*
     * hipMemAdvise is not in the stub library, so the shim's
     * dlsym(RTLD_NEXT, ...) fails for it: it must log that once and pass the
     * call through without crashing. Called twice to prove "once".
     */
    {
        int (*advise)(const void *, size_t, int, int) =
            (int (*)(const void *, size_t, int, int))dlsym(RTLD_DEFAULT, "hipMemAdvise");
        int r1;
        int r2;

        if (!advise) {
            fprintf(stderr, "selftest: the shim does not export hipMemAdvise\n");
            return -1;
        }
        r1 = advise(host, psz, 1, 0);
        r2 = advise(host, psz, 1, 0);
        if (r1 != 0 || r2 != 0) {
            fprintf(stderr, "selftest: absent symbol did not pass through: %d %d\n", r1,
                    r2);
            return -1;
        }
    }

    printf("  sequence rc=%d (fake HIP, nothing touched the GPU)\n", rc);
    return 0;
}

/* --------------------------------------------------------- log inspection */

static char *find_call(char *log, const char *call)
{
    char needle[80];

    snprintf(needle, sizeof needle, " %s ", call);
    return strstr(log, needle);
}

static int before(char *log, const char *a, const char *b, const char *what)
{
    char *pa = find_call(log, a);
    char *pb = find_call(log, b);

    ok(pa && pb && pa < pb, what);
    return 0;
}

/* First line that carries " call ", without its newline. */
static int line_of(char *log, const char *call, char *out, size_t cap)
{
    char *p = find_call(log, call);
    size_t n;

    if (!p)
        return 0;
    p++;
    n = strcspn(p, "\n");
    if (n >= cap)
        n = cap - 1;
    memcpy(out, p, n);
    out[n] = '\0';
    return 1;
}

static int count_occurrences(const char *hay, const char *needle)
{
    int n = 0;
    const char *p = hay;

    while ((p = strstr(p, needle)) != NULL) {
        n++;
        p++;
    }
    return n;
}

static int load_log(const char *path)
{
    FILE *f = fopen(path, "r");
    size_t n;

    if (!f) {
        perror(path);
        return -1;
    }
    n = fread(g_log, 1, sizeof g_log - 1, f);
    fclose(f);
    g_log[n] = '\0';
    return 0;
}

static void print_log(const char *path)
{
    printf("\n--- %s ---\n", path);
    fputs(g_log, stdout);
    printf("--- end %s ---\n\n", path);
}

static void verify_common(int phase)
{
    char l[1600];

    printf(" checks:\n");
    ok(find_call(g_log, "mmap") != NULL, "mmap line logged");
    ok(find_call(g_log, "hipHostRegister") != NULL, "hipHostRegister line logged");
    ok(find_call(g_log, "hipMemcpyAsync") != NULL, "hipMemcpyAsync line logged");
    ok(find_call(g_log, "madvise") != NULL, "madvise line logged");
    ok(find_call(g_log, "posix_fadvise") != NULL, "posix_fadvise line logged");
    ok(find_call(g_log, "munmap") != NULL, "munmap line logged");
    ok(find_call(g_log, "hipStreamSynchronize") != NULL,
       "hipStreamSynchronize line logged");
    ok(find_call(g_log, "hipHostUnregister") != NULL, "hipHostUnregister line logged");
    ok(find_call(g_log, "hipFree") != NULL, "hipFree line logged");

    before(g_log, "mmap", "hipHostRegister", "order: mmap before hipHostRegister");
    before(g_log, "hipHostRegister", "hipMemcpyAsync",
           "order: registration before the async copy");
    before(g_log, "hipMemcpyAsync", "munmap", "order: async copy before munmap");
    before(g_log, "munmap", "hipStreamSynchronize",
           "order: unmap before synchronise (the ordering under study)");

    if (line_of(g_log, "hipHostRegister", l, sizeof l)) {
        ok(strstr(l, "ret=0") != NULL, "hipHostRegister ret=0 captured");
        ok(strstr(l, "size=4096") != NULL, "hipHostRegister size captured");
    }
    if (line_of(g_log, "hipMemcpyAsync", l, sizeof l)) {
        ok(strstr(l, "ret=0") != NULL, "hipMemcpyAsync ret=0 captured");
        ok(strstr(l, "kindname=H2D") != NULL, "hipMemcpyAsync kind decoded as H2D");
    }
    if (line_of(g_log, "munmap", l, sizeof l)) {
        ok(strstr(l, "ret=0") != NULL, "munmap returned 0 (pass-through)");
        ok(strstr(l, "live_overlap=1") != NULL,
           "munmap reports the live registration overlapping the range");
        ok(strstr(l, "async_since_sync=1") != NULL,
           "munmap reports one async copy since the last sync");
    }
    if (line_of(g_log, "madvise", l, sizeof l))
        ok(strstr(l, "live_overlap=1") != NULL,
           "madvise reports the live registration overlapping the range");
    if (line_of(g_log, "posix_fadvise", l, sizeof l))
        ok(strstr(l, "advice=4") != NULL, "posix_fadvise advice captured");

    /* Transparency: every call reached the real implementation. */
    ok(count_occurrences(g_log, "note=no_symbol") == 1,
       "absent symbol logged exactly once (two calls, one notice)");

    /* Transparency: every call reached the real implementation. */
    ok(fake_hip_call_count >= 5, "real HIP stubs were called (pass-through)");
    ok(fake_hip_last_registered != NULL && fake_hip_registered_bytes == 4096,
       "real hipHostRegister saw the same pointer and size");
    ok(fake_hip_last_memcpy_kind == 1, "real hipMemcpyAsync saw kind=H2D");
    ok(fake_hip_unregister_count == 1, "real hipHostUnregister was called once");
    ok(fake_hip_sync_count == 1, "real hipStreamSynchronize was called once");
    (void)phase;
}

int main(int argc, char **argv)
{
    const char *phase = getenv("HIPTRACE_SELFTEST_PHASE");
    char l[1600];
    char watch[64];
    size_t psz = (size_t)sysconf(_SC_PAGESIZE);

    (void)argc;
    (void)argv;

    if (!phase) {                       /* first entry: set up and re-exec */
        if (prepare_env(LOG_NAME_1, NULL) != 0)
            return 2;
        return reexec("1");
    }

    if (strcmp(phase, "2") == 0) {
        printf("phase 2: watch mode (HIP_TRACE_WATCH=%s)\n",
               getenv("HIP_TRACE_WATCH") ? getenv("HIP_TRACE_WATCH") : "(unset)");
        if (run_sequence() != 0)
            return 2;
        if (load_log(getenv("HIP_TRACE_LOG")) != 0)
            return 2;
        print_log(getenv("HIP_TRACE_LOG"));
        verify_common(2);
        if (line_of(g_log, "munmap", l, sizeof l))
            ok(strstr(l, "watch=1") != NULL,
               "munmap of the watched range is marked watch=1");
        if (line_of(g_log, "hipMemcpyAsync", l, sizeof l))
            ok(strstr(l, "watch=1 arg=2") != NULL,
               "async copy names the watched range as its source (arg=2)");
        if (line_of(g_log, "hipFree", l, sizeof l))
            ok(strstr(l, "watch=0") != NULL,
               "a non-overlapping pointer is marked watch=0");
        printf("\n%s\n", g_fail ? "SELFTEST FAILED" : "SELFTEST PASSED (watch mode)");
        return g_fail ? 1 : 0;
    }

    /* phase 1 */
    printf("phase 1: ordering, arguments and pass-through\n");
    if (run_sequence() != 0)
        return 2;
    if (load_log(getenv("HIP_TRACE_LOG")) != 0)
        return 2;
    print_log(getenv("HIP_TRACE_LOG"));
    verify_common(1);
    if (g_fail) {
        printf("\nSELFTEST FAILED (phase 1), stopping before the watch phase\n");
        return 1;
    }

    printf("\nphase 1 passed; starting the watch-mode phase\n");
    snprintf(watch, sizeof watch, "0x%llx:%zu",
             (unsigned long long)SELFTEST_MAP_ADDR + psz, psz);
    if (prepare_env(LOG_NAME_2, watch) != 0)
        return 2;
    return reexec("2");
}
