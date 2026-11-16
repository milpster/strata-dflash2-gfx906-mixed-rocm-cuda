// crashrec.c — LD_PRELOAD fatal-signal recorder for the #267 crash hunt.
// On SIGSEGV/SIGBUS/SIGABRT/SIGFPE: backtrace_symbols_fd() to fd 2 (stderr),
// then restores the default disposition and re-raises so the process still
// dies honestly. Installed from a constructor; if the host program later
// installs its own handlers, ours are replaced (accepted limitation).
// Build: gcc -shared -fPIC -O1 -o crashrec.so crashrec.c
#define _GNU_SOURCE
#include <execinfo.h>
#include <signal.h>
#include <unistd.h>
#include <string.h>

static void crashrec_handler(int sig, siginfo_t *si, void *uc) {
    (void) uc;
    static const char msg[] = "\n=== crashrec: fatal signal ";
    char buf[128];
    int n = 0;
    n += sizeof(msg) - 1;
    memcpy(buf, msg, sizeof(msg) - 1);
    // signal number and fault address without libc printf machinery
    const char digits[] = "0123456789";
    buf[n++] = digits[sig / 10 % 10];
    buf[n++] = digits[sig % 10];
    const char at[] = " at ~ ===\n";
    memcpy(buf + n, at, sizeof(at));
    n += sizeof(at) - 1;
    write(2, buf, n);
    void *frames[64];
    int f = backtrace(frames, 64);
    backtrace_symbols_fd(frames, f, 2);
    write(2, "=== crashrec: end ===\n", 22);
    signal(sig, SIG_DFL);
    raise(sig);
}

__attribute__((constructor)) static void crashrec_init(void) {
    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_sigaction = crashrec_handler;
    sa.sa_flags = SA_SIGINFO;
    sigaction(SIGSEGV, &sa, 0);
    sigaction(SIGBUS, &sa, 0);
    sigaction(SIGABRT, &sa, 0);
    sigaction(SIGFPE, &sa, 0);
    write(2, "=== crashrec: fatal-signal recorder armed ===\n", 45);
}
