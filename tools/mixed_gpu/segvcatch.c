// Crash-site reporter for the H9 fault: install SIGSEGV/SIGABRT/SIGBUS handlers that print the
// faulting RIP and a backtrace to stderr (the serve wrapper captures engine stderr in its log),
// then exit.  Loaded with LD_PRELOAD next to the engine; no ptrace, no ASLR change, so the run
// is byte-identical to a normal launch apart from the report at the moment of the crash.
//   gcc -shared -fPIC -o segvcatch.so segvcatch.c
//   LD_PRELOAD=$PWD/segvcatch.so <engine launch>
#define _GNU_SOURCE
#include <dlfcn.h>
#include <execinfo.h>
#include <fcntl.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ucontext.h>
#include <unistd.h>

// Which mapping faulted?  /proc/self/maps around the fault address: inside a CUDA device BAR,
// the heap, or an unmapped gap settles the buffer-kind theories at a glance.
static void maps_around(unsigned long long a) {
    static char m[262144];
    int fd = open("/proc/self/maps", O_RDONLY);
    if (fd < 0) return;
    size_t got = 0;
    while (got < sizeof m - 1) {
        ssize_t r = read(fd, m + got, sizeof m - 1 - got);
        if (r <= 0) break;
        got += (size_t) r;
    }
    close(fd);
    m[got] = 0;
    char msg[768];
    char *line = m, *prev = NULL;
    int hit = 0;
    while (line && *line) {
        char *nl = strchr(line, '\n');
        if (nl) *nl = 0;
        unsigned long long s = strtoull(line, NULL, 16);
        char *dash = strchr(line, '-');
        unsigned long long e = dash ? strtoull(dash + 1, NULL, 16) : 0;
        if (!hit && s <= a && a < e) {
            int nb = snprintf(msg, sizeof msg, "segvcatch: fault address 0x%llx INSIDE %s\n", a, line);
            if (nb > 0) (void) write(2, msg, (size_t) nb);
            if (prev) {
                nb = snprintf(msg, sizeof msg, "segvcatch: map before  %s\n", prev);
                if (nb > 0) (void) write(2, msg, (size_t) nb);
            }
            hit = 1;
        } else if (hit) {
            int nb = snprintf(msg, sizeof msg, "segvcatch: map after   %s\n", line);
            if (nb > 0) (void) write(2, msg, (size_t) nb);
            break;
        }
        if (!hit) prev = line;
        line = nl ? nl + 1 : NULL;
    }
    if (!hit) {
        int nb = snprintf(msg, sizeof msg, "segvcatch: fault address 0x%llx is UNMAPPED (no maps line covers it)\n", a);
        if (nb > 0) (void) write(2, msg, (size_t) nb);
    }
}

static void report(int sig, siginfo_t *si, void *uc) {
    ucontext_t *ctx = (ucontext_t *) uc;
    char buf[640];
    int n = snprintf(buf, sizeof buf,
                     "\nsegvcatch: signal %d (%s), fault address %p, RIP 0x%llx\n",
                     sig, strsignal(sig), si->si_addr,
                     (unsigned long long) ctx->uc_mcontext.gregs[REG_RIP]);
    if (n > 0) (void) write(2, buf, (size_t) n);
    maps_around((unsigned long long) (uintptr_t) si->si_addr);

    void *frames[96];
    int nf = backtrace(frames, 96);
    if (nf > 1) frames[1] = (void *) ctx->uc_mcontext.gregs[REG_RIP];   // the faulting PC itself
    backtrace_symbols_fd(frames, nf, 2);

    for (int i = 1; i < nf && i < 48; ++i) {                            // module + base-relative offset
        Dl_info di;
        memset(&di, 0, sizeof di);
        if (dladdr(frames[i], &di) && di.dli_fname) {
            unsigned long long off = (unsigned long long) ((char *) frames[i] - (char *) di.dli_fbase);
            n = snprintf(buf, sizeof buf,
                         "segvcatch: frame %d at %s+0x%llx in %s (%s)\n", i,
                         di.dli_sname ? di.dli_sname : "", off, di.dli_fname,
                         di.dli_saddr
                             ? "+sym"
                             : (di.dli_sname ? "sym" : "nosym"));
            if (n > 0) (void) write(2, buf, (size_t) n);
        }
    }
    (void) write(2, "segvcatch: exiting after report\n", 32);
    _exit(128 + sig);
}

__attribute__((constructor)) static void segvcatch_install(void) {
    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_sigaction = report;
    sa.sa_flags = SA_SIGINFO | SA_RESETHAND;
    sigaction(SIGSEGV, &sa, NULL);
    sigaction(SIGABRT, &sa, NULL);
    sigaction(SIGBUS, &sa, NULL);
}
