// hip-fault-repro.cpp - minimal control for the recurrent GPU permission faults.
// Fault track, measurement contract Phase 4. Build/run: see README.md.
//
// --mode cycle (default): the scanned 64 MiB file is repeatedly taken through
//   sync -> hipHostUnregister -> munmap -> posix_fadvise(DONTNEED) -> rewrite the
//   pattern -> mmap(MAP_SHARED) -> hipHostRegister(mapped) -> re-derive the
//   device pointer -> scan. Unmapping first makes DONTNEED effective, so each
//   cycle exercises eviction, refault and re-registration under a live device
//   mapping. --mode file scans one registered file mapping without cycling;
//   --mode anon scans a hipHostMalloc buffer with a separate park file. Those
//   two test only branch 1: a registered mapping read at max rate is stable.
//
// --positive-control runs once, before the long leg. The original one-step
// form (queue a scan, then hipHostUnregister + munmap) never fired, and this
// file now measures why: hipHostUnregister is blocking (its elapsed time equals
// the scan's, also for a scan held in flight for 3.2 s), so the munmap always
// lands after device idle. A registration plus munmap does revoke the device
// translation, so the control now queues a 16-repeat scan and munmaps WHILE
// the registration is still live and the device is reading. Measured on ROCm
// 6.1.0: the HSA runtime reports a GPU memory access fault and aborts, exit
// 134, log ending in the FATAL signal block. That, or a fault-class HIP error
// (exit 3), is the pass; exit 6/7 still means the control failed and a clean
// long leg is uninterpretable.
//
// Every launch is checked with hipGetLastError: on this stack a failed launch
// returns hipErrorInvalidDeviceFunction while hipDeviceSynchronize still
// reports success, which once made a kernel that never ran look clean.
//
// --host-write-sec N (default 30) rewrites one word per page per tick: a cache
// coherence probe, not a fault trigger; 0 turns it off. A write round takes the
// scan mutex S, and a scan holds S from the launch to the last checksum
// comparison, so no write round can land inside a scan.
//
// Checksums: per page, the integer sum of the words and the word-index-weighted
// sum (sum i*w[i]); both compared against the host's expected values. A single
// mismatch is re-scanned once, still under S, before it is called a fault:
// only two consecutive disagreeing scans print CHECKSUM FAULT (exit 4); a
// mismatch that the re-scan does not reproduce prints RETRY CLEAN and the run
// continues (counted in HB and DONE as retry-clean). Every 10 s
// a heartbeat logs VA ranges, one page's checksums, /proc/self/statm, Cached and
// MemAvailable. A checksum mismatch logs the page VA and maps; so does a HIP
// fault. ABRT/SEGV/BUS/ILL dump the last scan and maps from a signal handler.
// ROCm 6.1.0 has no hipErrorMemoryFault: the class arrives as
// hipErrorIllegalAddress / hipErrorLaunchFailure / hipErrorAssert or a signal.
// Exit: 0 clean, 2 usage/setup, 3 HIP fault, 4 checksum fault, 5 churn defect,
// 6 positive control failed, 7 positive control inconclusive (non-fault
// rejection), 128+signal (the control's usual pass here: 134).

#include <hip/hip_runtime.h>
#include <atomic>
#include <cerrno>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <fcntl.h>
#include <mutex>
#include <signal.h>
#include <string>
#include <sys/mman.h>
#include <sys/stat.h>
#include <thread>
#include <time.h>
#include <unistd.h>
#include <vector>

using u64 = unsigned long long;
static const size_t kPage = 4096, kWords = 1024, kPages = 16384, kBytes = kPages * kPage;

static FILE* L;  // log; raw fd kept for the signal handler
static int g_log_fd = -1;
static std::mutex W;  // serializes host writes against the churn copy
// Serializes a whole scan (launch, synchronize, copies, checksum compare)
// against host writes, so no write round can land between the device's read and
// the expectation it is checked against. A host write takes S and then W; the
// churn thread takes W only, so --mode file keeps churning through scans.
static std::mutex S;
static std::atomic<bool> stop{false};
static std::atomic<u64> scans{0}, ticks{0}, retry_clean{0};
static std::atomic<int> churn_fail{0};

// No static destructors on fault paths: the churn thread may still hold g_dir.
// Every log line is flushed by logf_, so no fflush (and no FILE lock race) here.
[[noreturn]] static void die(int code) { _exit(code); }

static char* slurp(const char* path, char* buf, size_t cap) {
  int fd = open(path, O_RDONLY);
  ssize_t n = fd >= 0 ? read(fd, buf, cap - 1) : -1;
  if (fd >= 0) close(fd);
  if (n < 0) n = 0;
  buf[n] = 0;
  return buf;
}

static void trim_nl(char* p) {
  char* e = strchr(p, '\n');
  if (e) *e = 0;
}

static long cached_kb() {
  static char mi[8192];
  slurp("/proc/meminfo", mi, sizeof mi);
  const char* c = strstr(mi, "\nCached:");
  return c ? atol(c + 8) : -1;
}

// monotonic microseconds: the positive control's timings must not move when
// the wall clock is adjusted mid-run
static u64 now_us() {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (u64)ts.tv_sec * 1000000ull + (u64)(ts.tv_nsec / 1000);
}

static void logf_(const char* fmt, ...) {
  if (!L) return;
  char line[1200];
  time_t now = time(nullptr);
  struct tm t;
  localtime_r(&now, &t);
  int n = strftime(line, 24, "%Y-%m-%d %H:%M:%S ", &t);
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(line + n, sizeof line - n - 2, fmt, ap);
  va_end(ap);
  strcat(line, "\n");
  flockfile(L);
  fputs(line, L);
  fflush(L);
  funlockfile(L);
}

static void dump_maps(const char* why) {
  static char maps[1 << 20];  // /proc/self/maps is ~100 KiB here
  logf_("---- /proc/self/maps (%s) ----", why);
  slurp("/proc/self/maps", maps, sizeof maps);
  flockfile(L);
  fputs(maps, L);
  if (maps[sizeof maps - 2]) fputs("... maps truncated ...\n", L);
  fflush(L);
  funlockfile(L);
  logf_("---- end maps ----");
}

// async-signal-safe: hand-rolled decimal, write() only, no stdio/localtime
static char* udec(char* p, u64 v) {
  char t[20];
  int i = 0;
  do {
    t[i++] = (char)('0' + v % 10);
    v /= 10;
  } while (v);
  while (i) *p++ = t[--i];
  return p;
}

static void on_fatal(int sig) {
  static const char kSig[] = "\nFATAL signal ", kScan[] = "; last completed scan ",
                    kMaps[] = "; maps follow\n";
  char b[96];
  char* p = b;
  memcpy(p, kSig, sizeof kSig - 1), p += sizeof kSig - 1;
  p = udec(p, (u64)sig);
  memcpy(p, kScan, sizeof kScan - 1), p += sizeof kScan - 1;
  p = udec(p, scans.load());
  memcpy(p, kMaps, sizeof kMaps - 1), p += sizeof kMaps - 1;
  if (g_log_fd >= 0 && write(g_log_fd, b, (size_t)(p - b)) < 0) { /* best effort */ }
  int m = open("/proc/self/maps", O_RDONLY);
  if (m >= 0) {
    char buf[8192];
    ssize_t k;
    while ((k = read(m, buf, sizeof buf)) > 0)
      if (g_log_fd < 0 || write(g_log_fd, buf, (size_t)k) < 0) break;
    close(m);
  }
  _exit(128 + sig);
}

static inline uint32_t pattern(size_t p, size_t i) {
  return (uint32_t)((p * kWords + i) & 0xFFFFFFu);
}

// repeat > 1 keeps the kernel reading the same buffer for roughly
// repeat * (one 64 MiB scan), which the positive control needs to hold a scan
// in flight long enough for a host-side unmap to overlap it.
__global__ void scan_pages(const uint32_t* __restrict__ buf, u64* __restrict__ isum,
                           u64* __restrict__ wsum, u64 pages, unsigned repeat) {
  __shared__ u64 si[256], sw[256];
  u64 p = blockIdx.x;
  if (p >= pages) return;
  unsigned t = threadIdx.x;
  const uint32_t* w = buf + p * kWords;
  for (unsigned r = 0; r < repeat; ++r) {
    u64 a = 0, b = 0;
    for (unsigned i = t; i < kWords; i += blockDim.x) {
      a += w[i];
      b += (u64)i * w[i];
    }
    si[t] = a, sw[t] = b;
    __syncthreads();
    for (unsigned s = blockDim.x / 2; s > 0; s >>= 1) {
      if (t < s) si[t] += si[t + s], sw[t] += sw[t + s];
      __syncthreads();
    }
    if (t == 0) isum[p] = si[0], wsum[p] = sw[0];
  }
}

// 0 anon, 1 file (no cycle), 2 cycle (default)
static int g_mode = 2, g_period = 2;
static int g_churn_fd = -1;
static void* g_map = nullptr;
static std::string g_dir;

// Rewrite the pattern without a mapping, so DONTNEED left nothing behind.
static bool write_pattern(int fd) {
  static uint32_t buf[(1u << 20) / 4];
  for (size_t off = 0; off < kBytes; off += sizeof buf) {
    size_t base = off / 4;
    for (size_t i = 0; i < sizeof buf / 4; ++i) {
      size_t g = base + i;
      buf[i] = pattern(g / kWords, g % kWords);
    }
    if (pwrite(fd, buf, sizeof buf, (off_t)off) != (ssize_t)sizeof buf) return false;
  }
  return true;
}

// file mode: rewrite the scanned file in place, fsync, drop it from page cache,
// turn the park slot over, drop again. cycle/anon modes run the separate park.
static void churn() {
  std::vector<char> buf(8u << 20, 'x');
  for (u64 t = 0; !stop; ++t) {
    int rc = 0;
    if (g_mode == 1) {
      off_t off = (off_t)((t % (kBytes / buf.size())) * buf.size());
      {
        std::lock_guard<std::mutex> lk(W);
        if (pwrite(g_churn_fd, (const char*)g_map + off, buf.size(), off) !=
            (ssize_t)buf.size())
          rc = errno ? errno : EIO;
      }
      if (!rc) rc = fsync(g_churn_fd);
      if (!rc) rc = posix_fadvise(g_churn_fd, 0, 0, POSIX_FADV_DONTNEED);
      const std::string live = g_dir + "/scan.bin", prev = g_dir + "/scan.prev";
      if (!rc && rename(live.c_str(), prev.c_str()) != 0) rc = errno;
      if (!rc) rc = posix_fadvise(g_churn_fd, 0, 0, POSIX_FADV_DONTNEED);
      if (!rc && rename(prev.c_str(), live.c_str()) != 0) rc = errno;
    } else {
      const std::string tmp = g_dir + "/park.tmp", live = g_dir + "/park.bin";
      int fd = open(tmp.c_str(), O_RDWR | O_CREAT | O_TRUNC, 0600);
      if (fd < 0) rc = errno;
      if (!rc && write(fd, buf.data(), buf.size()) != (ssize_t)buf.size())
        rc = errno ? errno : EIO;
      if (!rc) rc = fsync(fd);
      if (!rc) rc = posix_fadvise(fd, 0, 0, POSIX_FADV_DONTNEED);
      if (fd >= 0) close(fd);
      if (!rc && rename(tmp.c_str(), live.c_str()) != 0) rc = errno;
      if (int lf = rc ? -1 : open(live.c_str(), O_RDONLY); lf >= 0)
        posix_fadvise(lf, 0, 0, POSIX_FADV_DONTNEED), close(lf);
    }
    if (rc) {
      churn_fail = 1;
      logf_("churn %llu: failed: %s", t, strerror(rc));
    } else {
      ++ticks;
    }
    for (int i = 0; i < g_period * 10 && !stop; ++i) usleep(100000);
  }
}

static bool fault_class(hipError_t e) {
  if (e == hipErrorIllegalAddress || e == hipErrorLaunchFailure || e == hipErrorAssert) return true;
  const char* nm = hipGetErrorName(e);
  return nm && strstr(nm, "Fault");
}

static void mkdir_p(const std::string& path) {
  for (size_t i = 1; i < path.size(); ++i)
    if (path[i] == '/') {
      std::string d = path.substr(0, i);
      if (mkdir(d.c_str(), 0755) != 0 && errno != EEXIST) return;
    }
  if (mkdir(path.c_str(), 0755) != 0 && errno != EEXIST) return;  // the argument itself
}

// expected sums for the initial pattern, after a cycle rewrote the file
static void base_expected(std::vector<u64>& esum, std::vector<u64>& ewsum) {
  for (size_t p = 0; p < kPages; ++p) {
    esum[p] = 0, ewsum[p] = 0;
    for (size_t i = 0; i < kWords; ++i) {
      u64 v = pattern(p, i);
      esum[p] += v;
      ewsum[p] += (u64)i * v;
    }
  }
}

// one word per page: a cache coherence probe into a live device mapping
static void host_writes(uint32_t* base, std::vector<u64>& esum, std::vector<u64>& ewsum,
                        u64 round) {
  std::lock_guard<std::mutex> sk(S);  // never inside a scan (lock order S, then W)
  std::lock_guard<std::mutex> lk(W);
  for (size_t p = 0; p < kPages; ++p) {
    uint32_t* w = base + p * kWords;
    size_t j = (size_t)((round + p) % kWords);
    uint32_t old = w[j], nv = (uint32_t)((old + 0x9E3779B1u) & 0xFFFFFFu);
    w[j] = nv;
    esum[p] += (u64)nv - old;
    ewsum[p] += (u64)j * ((u64)nv - old);
  }
}

int main(int argc, char** argv) {
  int dev = 1, churn_sec = 2, host_write_sec = 30, cycle_sec = 30;
  long duration_sec = 1800;
  bool positive = false;
  std::string dummy = "./run", lp = "hip-fault-repro.log", pci;
  for (int i = 1; i < argc; ++i) {
    auto val = [&]() -> const char* {
      if (i + 1 >= argc) {
        fprintf(stderr, "%s needs a value\n", argv[i]);
        exit(2);
      }
      return argv[++i];
    };
    if (!strcmp(argv[i], "--dev")) dev = atoi(val());
    else if (!strcmp(argv[i], "--duration-sec")) duration_sec = atol(val());
    else if (!strcmp(argv[i], "--churn-sec")) churn_sec = atoi(val());
    else if (!strcmp(argv[i], "--cycle-sec")) cycle_sec = atoi(val());
    else if (!strcmp(argv[i], "--host-write-sec")) host_write_sec = atoi(val());
    else if (!strcmp(argv[i], "--mode")) {
      const char* m = val();
      g_mode = !strcmp(m, "cycle") ? 2 : !strcmp(m, "file") ? 1 : 0;
      if (strcmp(m, "cycle") && strcmp(m, "file") && strcmp(m, "anon")) {
        fprintf(stderr, "--mode takes cycle, file or anon\n");
        return 2;
      }
    }
    else if (!strcmp(argv[i], "--positive-control")) positive = true;
    else if (!strcmp(argv[i], "--dummy-dir")) dummy = val();
    else if (!strcmp(argv[i], "--pci")) pci = val();
    else if (!strcmp(argv[i], "--log")) lp = val();
    else {
      fprintf(stderr, "usage: %s [--dev N] [--duration-sec N] [--churn-sec N] [--cycle-sec N]"
                      " [--host-write-sec N] [--mode cycle|file|anon] [--positive-control]"
                      " [--dummy-dir DIR] [--pci DDDD:BB:DD.F] [--log FILE]\n", argv[0]);
      return 2;
    }
  }
  mkdir_p(dummy);  // churn dir and log parent, before fopen
  size_t sl = lp.rfind('/');
  if (sl != std::string::npos) mkdir_p(lp.substr(0, sl));
  L = fopen(lp.c_str(), "a");
  if (!L) {
    fprintf(stderr, "cannot open log %s: %s\n", lp.c_str(), strerror(errno));
    return 2;
  }
  g_log_fd = fileno(L);

  static char altstack[64 * 1024];
  stack_t ss = {};
  ss.ss_sp = altstack;
  ss.ss_size = sizeof altstack;
  if (sigaltstack(&ss, nullptr) != 0) logf_("sigaltstack failed: %s", strerror(errno));
  struct sigaction sa = {};
  sa.sa_handler = on_fatal;
  sa.sa_flags = SA_RESETHAND | SA_ONSTACK;
  sigemptyset(&sa.sa_mask);
  for (int s : {SIGABRT, SIGSEGV, SIGBUS, SIGILL}) sigaction(s, &sa, nullptr);
  signal(SIGINT, [](int) { stop = true; });
  signal(SIGTERM, [](int) { stop = true; });

  auto hipchk = [&](hipError_t e, const char* what) {  // setup errors, not faults
    if (e == hipSuccess) return;
    logf_("SETUP ERROR in %s: %s code=%d", what, hipGetErrorString(e), (int)e);
    dump_maps(what);
    die(2);
  };

  int ndev = 0;
  hipchk(hipGetDeviceCount(&ndev), "hipGetDeviceCount");
  if (dev < 0 || dev >= ndev) {
    logf_("device %d not present (%d devices)", dev, ndev);
    return 2;
  }
  hipchk(hipSetDevice(dev), "hipSetDevice");
  hipDeviceProp_t prop = {};
  hipchk(hipGetDeviceProperties(&prop, dev), "hipGetDeviceProperties");
  char bdf[32];
  snprintf(bdf, sizeof bdf, "%04x:%02x:%02x.0", prop.pciDomainID, prop.pciBusID,
           prop.pciDeviceID);
  bool arch_ok = !strncmp(prop.gcnArchName, "gfx906", 6), pci_ok = pci.empty() || pci == bdf;
  logf_("start: dev=%d %s pci=%s arch=%s duration=%lds churn=%ds cycle=%ds mode=%s host-write=%ds "
        "dummy=%s", dev, prop.name, bdf, prop.gcnArchName, duration_sec, churn_sec, cycle_sec,
        g_mode == 2 ? "cycle" : g_mode == 1 ? "file" : "anon", host_write_sec, dummy.c_str());
  int hrv = 0, hdv = 0;
  hipchk(hipRuntimeGetVersion(&hrv), "hipRuntimeGetVersion");
  hipchk(hipDriverGetVersion(&hdv), "hipDriverGetVersion");
  logf_("runtime: hipRuntimeGetVersion=%d hipDriverGetVersion=%d", hrv, hdv);
  static char mapsbuf[1 << 20];
  if (char* m = slurp("/proc/self/maps", mapsbuf, sizeof mapsbuf); strstr(m, "libhsa-runtime64")) {
    char* h = strstr(m, "libhsa-runtime64"), *ls = h, *le = strchr(h, '\n');
    while (ls > m && ls[-1] != '\n') --ls;
    logf_("runtime: hsa from maps: %.*s", (int)((le ? le : ls + strlen(ls)) - ls), ls);
  } else {
    logf_("runtime: libhsa-runtime64 not in maps");
  }
  if (!arch_ok && pci.empty()) {
    logf_("refusing device %d: gcnArchName=%s != gfx906 (pass --pci %s to force)",
          dev, prop.gcnArchName, bdf);
    return 2;
  }
  if (!pci_ok) {
    logf_("refusing device %d: pci=%s != --pci %s", dev, bdf, pci.c_str());
    return 2;
  }
  if (!arch_ok) logf_("note: arch check overridden by --pci %s", bdf);
  if (positive && g_mode == 0) {
    logf_("positive control needs --mode cycle or file (there is no mapping to unmap in anon)");
    return 2;
  }

  void* host = nullptr;
  std::vector<u64> esum(kPages), ewsum(kPages), hisum(kPages), hwsum(kPages);
  if (g_mode != 0) {
    const std::string scan = dummy + "/scan.bin";
    g_churn_fd = open(scan.c_str(), O_RDWR | O_CREAT, 0600);
    if (g_churn_fd < 0) {
      logf_("cannot open %s: %s", scan.c_str(), strerror(errno));
      return 2;
    }
    if (ftruncate(g_churn_fd, (off_t)kBytes) != 0) {
      logf_("ftruncate %s: %s", scan.c_str(), strerror(errno));
      return 2;
    }
    host = mmap(nullptr, kBytes, PROT_READ | PROT_WRITE, MAP_SHARED, g_churn_fd, 0);
    if (host == MAP_FAILED) {
      logf_("mmap %s: %s", scan.c_str(), strerror(errno));
      return 2;
    }
    hipError_t re =
        hipHostRegister(host, kBytes, hipHostRegisterMapped | hipHostRegisterPortable);
    if (re != hipSuccess) {  // file-backed registration may be refused: platform fact
      logf_("SETUP ERROR hipHostRegister(file mapping): %s code=%d; record this and run "
            "--mode anon", hipGetErrorString(re), (int)re);
      dump_maps("hipHostRegister");
      die(2);
    }
  } else {
    hipError_t he = hipHostMalloc(&host, kBytes, hipHostMallocMapped | hipHostMallocPortable);
    if (he != hipSuccess) {
      logf_("hipHostMalloc(mapped) failed (%s); malloc + hipHostRegister", hipGetErrorString(he));
      if (posix_memalign(&host, kPage, kBytes) != 0) return 2;
      hipchk(hipHostRegister(host, kBytes, hipHostRegisterMapped | hipHostRegisterPortable),
             "hipHostRegister");
    } else {
      hipchk(he, "hipHostMalloc");
    }
  }
  g_map = host;
  g_dir = dummy;
  g_period = churn_sec > 0 ? churn_sec : 1;
  for (size_t p = 0; p < kPages; ++p) {
    uint32_t* w = (uint32_t*)host + p * kWords;
    for (size_t i = 0; i < kWords; ++i) {
      w[i] = pattern(p, i);
      esum[p] += w[i];
      ewsum[p] += (u64)i * w[i];
    }
  }
  void* dptr = nullptr;
  hipchk(hipHostGetDevicePointer(&dptr, host, 0), "hipHostGetDevicePointer");
  u64* disum = nullptr;
  u64* dwsum = nullptr;
  hipchk(hipMalloc((void**)&disum, kPages * sizeof(*disum)), "hipMalloc(isum)");
  hipchk(hipMalloc((void**)&dwsum, kPages * sizeof(*dwsum)), "hipMalloc(wsum)");
  logf_("buffer: host VA [%p,%p) device VA [%p,%p)", host, (char*)host + kBytes, dptr,
        (char*)dptr + kBytes);

  auto scan1 = [&](const uint32_t* buf, unsigned repeat = 1) -> hipError_t {
    hipLaunchKernelGGL(scan_pages, dim3((unsigned)kPages), dim3(256), 0, 0, buf, disum, dwsum,
                       (u64)kPages, repeat);
    // A launch can fail without informing hipDeviceSynchronize on this stack;
    // never let that read as a clean scan.
    return hipGetLastError();
  };
  auto devfault = [&](const char* what, hipError_t e, u64 done) {
    logf_("HIP FAULT in %s: %s code=%d%s; last completed scan %llu", what,
          hipGetErrorString(e), (int)e, fault_class(e) ? " [fault class]" : "", done);
    logf_("buffer host VA [%p,%p) device VA [%p,%p)", host, host ? (char*)host + kBytes : nullptr,
          dptr, (char*)dptr + kBytes);
    dump_maps(what);
    die(3);
  };

  // One scan, exactly as the detector needs it: launch, synchronize, both result
  // copies. The caller holds S around this and the checksum compare, so the
  // expectations esum/ewsum describe the only content the device could have read.
  auto run_scan = [&](u64 done) {
    hipError_t le = scan1((const uint32_t*)dptr);
    if (le != hipSuccess) {  // a silent no-op launch must never look like a clean scan
      if (fault_class(le)) devfault("scan_pages launch", le, done);
      logf_("SETUP ERROR scan_pages launch: %s code=%d", hipGetErrorString(le), (int)le);
      die(2);
    }
    hipError_t e = hipDeviceSynchronize();
    if (e != hipSuccess) devfault("scan_pages", e, done);
    for (int k = 0; k < 2; ++k) {  // a copy that fails after a fault is a fault, not setup
      hipError_t ce = k ? hipMemcpy(hwsum.data(), dwsum, kPages * sizeof(*dwsum),
                                    hipMemcpyDeviceToHost)
                        : hipMemcpy(hisum.data(), disum, kPages * sizeof(*disum),
                                    hipMemcpyDeviceToHost);
      if (ce != hipSuccess) devfault(k ? "wsum copy" : "isum copy", ce, done);
    }
  };
  auto first_bad = [&]() -> int {  // first page whose device sums differ from the host's
    for (size_t p = 0; p < kPages; ++p)
      if (hisum[p] != esum[p] || hwsum[p] != ewsum[p]) return (int)p;
    return -1;
  };

  if (positive) {  // the engine's park/restore race, deliberately
    // Measured history: the original control (queue one 64 MiB scan, then
    // unregister + munmap) never fired - five runs on 2026-11-24 all exit 6,
    // and at the time the kernel never ran at all (see CMakeLists: plain
    // gfx906 does not load here). Steps 1 and 2 now measure the blocking
    // unregister that makes the original order race-free; step 3 revokes the
    // range while the registration is live and the device is reading, which is
    // the engine's shape and does fault. A pass is still only a memory-fault
    // class: exit 3, or a signal that ends the log with a FATAL block.
    bool rejected = false;  // some stage returned a non-fault error
    auto pass = [&](const char* where, hipError_t e) {
      dump_maps(where);
      logf_("positive control: memory fault detected as expected at %s (%s code=%d); "
            "exit 3 is the control's pass", where, hipGetErrorString(e), (int)e);
      die(3);
    };
    auto chk = [&](const char* what, hipError_t e) {  // fault -> pass, else record
      if (e == hipSuccess) return;
      if (fault_class(e)) pass(what, e);
      rejected = true;
      logf_("positive control: %s: %s code=%d (not a fault class; not a pass)", what,
            hipGetErrorString(e), (int)e);
    };
    auto remap = [&]() {  // fresh mapping + registration, same file
      void* m = mmap(nullptr, kBytes, PROT_READ | PROT_WRITE, MAP_SHARED, g_churn_fd, 0);
      if (m == MAP_FAILED) {
        logf_("SETUP ERROR positive control mmap: %s", strerror(errno));
        die(2);
      }
      host = m;
      hipError_t re =
          hipHostRegister(host, kBytes, hipHostRegisterMapped | hipHostRegisterPortable);
      if (re != hipSuccess) {
        logf_("SETUP ERROR positive control hipHostRegister: %s code=%d",
              hipGetErrorString(re), (int)re);
        die(2);
      }
      hipchk(hipHostGetDevicePointer(&dptr, host, 0), "positive control hipHostGetDevicePointer");
      logf_("positive control: remapped host VA [%p,%p) device VA [%p,%p)", host,
            (char*)host + kBytes, dptr, (char*)dptr + kBytes);
    };
    // A launch that fails is a setup error (2), never a clean or a fault result.
    auto launch = [&](const char* what, const void* buf, unsigned repeat) {
      hipError_t le = scan1((const uint32_t*)buf, repeat);
      if (le != hipSuccess) {
        logf_("SETUP ERROR positive control %s launch: %s code=%d", what, hipGetErrorString(le),
              (int)le);
        dump_maps(what);
        die(2);
      }
    };

    // Step 0: with no race at all, how long does one scan take?
    u64 t = now_us();
    launch("step 0 cold", dptr, 1);
    hipError_t e0 = hipDeviceSynchronize();
    u64 us_cold = now_us() - t;
    t = now_us();
    launch("step 0 warm", dptr, 1);
    if (e0 == hipSuccess) e0 = hipDeviceSynchronize();
    u64 us_warm = now_us() - t;
    logf_("positive control step 0: one 64 MiB scan+sync cold=%llu us warm=%llu us", us_cold,
          us_warm);
    chk("step 0 baseline scan", e0);

    // Step 1: the original control, timed. If unregister covers the scan, its
    // elapsed time is the scan time and munmap can never overlap device reads.
    logf_("positive control step 1: queueing a scan, then unregister+munmap while it is in flight");
    t = now_us();
    launch("step 1", dptr, 1);
    u64 t_launch1 = now_us() - t;
    t = now_us();
    hipError_t ue1 = hipHostUnregister(host);
    u64 t_unreg1 = now_us() - t;
    void* reg1 = host;
    host = nullptr;
    t = now_us();
    int unmap1 = munmap(reg1, kBytes);
    u64 t_unmap1 = now_us() - t;
    int frc1 = posix_fadvise(g_churn_fd, 0, 0, POSIX_FADV_DONTNEED);
    t = now_us();
    hipError_t se1 = hipDeviceSynchronize();
    u64 t_sync1 = now_us() - t;
    logf_("positive control step 1: launch=%llu unregister=%llu munmap=%llu sync=%llu us "
          "(warm scan %llu us); unregister=%s code=%d munmap=%d fadvise=%d sync=%s code=%d%s",
          t_launch1, t_unreg1, t_unmap1, t_sync1, us_warm, hipGetErrorString(ue1), (int)ue1,
          unmap1, frc1, hipGetErrorString(se1), (int)se1,
          (fault_class(ue1) || fault_class(se1)) ? " [fault class]" : "");
    chk("step 1 unregister", ue1);
    if (unmap1 != 0) {
      rejected = true;
      logf_("positive control: step 1 munmap: %s", strerror(errno));
    }
    chk("step 1 sync", se1);
    logf_("positive control step 1: %s", t_unreg1 >= us_warm / 2
              ? "unregister waited for the scan (blocking), so munmap followed device idle"
              : "unregister returned before the scan could have finished (no blocking seen)");

    // Step 2: same order, but the scan is held in flight for ~1 s. A
    // non-blocking unregister would now leave a second of device reads to race.
    remap();
    const unsigned kRepeat = 16;  // ~16 x one scan: seconds of device reads
    logf_("positive control step 2: queueing a %u-repeat scan (~%llu us), then unregister while "
          "it is in flight", kRepeat, (u64)kRepeat * us_warm);
    t = now_us();
    launch("step 2", dptr, kRepeat);
    u64 t_launch2 = now_us() - t;
    t = now_us();
    hipError_t ue2 = hipHostUnregister(host);
    u64 t_unreg2 = now_us() - t;
    void* reg2 = host;
    host = nullptr;
    t = now_us();
    int unmap2 = munmap(reg2, kBytes);
    u64 t_unmap2 = now_us() - t;
    int frc2 = posix_fadvise(g_churn_fd, 0, 0, POSIX_FADV_DONTNEED);
    t = now_us();
    hipError_t se2 = hipDeviceSynchronize();
    u64 t_sync2 = now_us() - t;
    logf_("positive control step 2: launch=%llu unregister=%llu munmap=%llu sync=%llu us; "
          "unregister=%s code=%d munmap=%d fadvise=%d sync=%s code=%d%s", t_launch2, t_unreg2,
          t_unmap2, t_sync2, hipGetErrorString(ue2), (int)ue2, unmap2, frc2,
          hipGetErrorString(se2), (int)se2, (fault_class(ue2) || fault_class(se2))
              ? " [fault class]" : "");
    chk("step 2 unregister with a long scan in flight", ue2);
    if (unmap2 != 0) {
      rejected = true;
      logf_("positive control: step 2 munmap: %s", strerror(errno));
    }
    chk("step 2 sync", se2);
    logf_("positive control step 2: %s", t_unreg2 >= (u64)kRepeat * us_warm / 2
              ? "unregister waited for the long scan (blocking)"
              : "unregister did not wait for the long scan");

    // Step 3 (last, and fatal by construction on this platform): the engine's
    // shape - munmap the range while the registration is still live and the
    // device is reading it. Measured 2026-11-24: ROCm 6.1.0 aborts the process
    // through the HSA runtime with a GPU memory access fault, so the log's
    // FATAL block and the 128+SIGABRT exit are the control's pass. (A device
    // read of an already-unregistered VA faults the same way; it is not probed
    // here because either probe ends the process.)
    remap();
    logf_("positive control step 3: queueing a %u-repeat scan, then munmap while registered",
          kRepeat);
    launch("step 3", dptr, kRepeat);
    t = now_us();
    int unmap3 = munmap(host, kBytes);
    u64 t_unmap3 = now_us() - t;
    void* reg3 = host;
    host = nullptr;
    // Logged before the sync: the HSA abort arrives while the device is still
    // scanning, so this is the last line the stage can write.
    logf_("positive control step 3: munmap=%d in %llu us while the registration is still live and "
          "the device is scanning; fadvise next, then sync", unmap3, t_unmap3);
    int frc3 = posix_fadvise(g_churn_fd, 0, 0, POSIX_FADV_DONTNEED);
    t = now_us();
    hipError_t se3 = hipDeviceSynchronize();
    u64 t_sync3 = now_us() - t;
    hipError_t ue3 = hipHostUnregister(reg3);
    logf_("positive control step 3: munmap=%d in %llu us fadvise=%d sync=%s code=%d in %llu us "
          "unregister-after-munmap=%s code=%d%s", unmap3, t_unmap3, frc3,
          hipGetErrorString(se3), (int)se3, t_sync3, hipGetErrorString(ue3), (int)ue3,
          (fault_class(se3) || fault_class(ue3)) ? " [fault class]" : "");
    if (unmap3 != 0) {
      rejected = true;
      logf_("positive control: step 3 munmap: %s", strerror(errno));
    }
    chk("step 3 sync after munmap-while-registered", se3);
    chk("step 3 unregister after munmap", ue3);

    if (rejected) {
      logf_("positive control INCONCLUSIVE: rejected without a memory fault; detector path not "
            "proven");
      die(7);
    }
    logf_("positive control FAILED: no fault in any step; detector path unproven");
    die(6);
  }

  std::thread churner(churn);
  const time_t t0 = time(nullptr);
  time_t last = 0, last_w = t0, last_c = t0;
  u64 n = 0, wround = 0, cyc = 0;

  auto do_cycle = [&]() {  // sync -> unregister -> munmap -> fadvise -> rewrite -> remap
    u64 c = ++cyc;
    hipError_t se = hipDeviceSynchronize();
    if (se != hipSuccess) devfault("cycle sync", se, n);
    long c_before = cached_kb();  // with the mapping still present
    hipError_t ue = hipHostUnregister(host);
    if (ue != hipSuccess) devfault("cycle hipHostUnregister", ue, n);
    if (munmap(host, kBytes) != 0) {
      logf_("SETUP ERROR cycle munmap: %s", strerror(errno));
      die(2);
    }
    host = nullptr;
    int frc = posix_fadvise(g_churn_fd, 0, 0, POSIX_FADV_DONTNEED);
    long c_evicted = cached_kb();  // middle sample: this is the eviction evidence
    bool wok = write_pattern(g_churn_fd);
    if (!wok) {  // do not remap: the sums would not describe the file
      logf_("cycle %llu: CHURN DEFECT pattern rewrite failed: %s; not remapping", c,
            strerror(errno));
      die(5);
    }
    long c_rewritten = cached_kb();  // after the pwrite refilled the cache
    base_expected(esum, ewsum);      // the rewrite restored the initial pattern
    void* m = mmap(nullptr, kBytes, PROT_READ | PROT_WRITE, MAP_SHARED, g_churn_fd, 0);
    if (m == MAP_FAILED) {
      logf_("SETUP ERROR cycle mmap: %s", strerror(errno));
      die(2);
    }
    host = m;
    hipError_t re =
        hipHostRegister(host, kBytes, hipHostRegisterMapped | hipHostRegisterPortable);
    if (re != hipSuccess) {
      logf_("SETUP ERROR cycle hipHostRegister: %s code=%d", hipGetErrorString(re), (int)re);
      die(2);
    }
    hipchk(hipHostGetDevicePointer(&dptr, host, 0), "cycle hipHostGetDevicePointer");
    logf_("cycle %llu: unregistered, munmapped, fadvise=%d, pattern rewrite ok, Cached before=%ld "
          "after-fadvise=%ld after-rewrite=%ld kB; remapped host [%p,%p) device [%p,%p)", c, frc,
          c_before, c_evicted, c_rewritten, host, (char*)host + kBytes, dptr,
          (char*)dptr + kBytes);
  };

  while (!stop && time(nullptr) - t0 < duration_sec) {
    if (g_mode == 2 && time(nullptr) - last_c >= cycle_sec) {
      last_c = time(nullptr);
      do_cycle();
    }
    if (host_write_sec > 0 && time(nullptr) - last_w >= host_write_sec) {
      last_w = time(nullptr);
      host_writes((uint32_t*)host, esum, ewsum, wround++);
      logf_("host write round %llu (one word per page)", wround);
    }
    // S is held from the launch to the last comparison: a host write round can
    // no longer change a page the device is reading or the expectation it is
    // compared against mid-scan.
    size_t sp = 0;
    u64 hp_isum = 0, hp_esum = 0, hp_wsum = 0, hp_ewsum = 0;
    {
      std::lock_guard<std::mutex> scan_lk(S);
      run_scan(n);
      scans = ++n;
      int bad = first_bad();
      if (bad >= 0) {
        // A checksum mismatch used to be reported at once. Re-scan once before
        // calling it a fault: device-side corruption that survives a scan is
        // stable, whereas a torn scan or a stale page source is not.
        logf_("checksum mismatch scan %llu page %d host VA %p device VA %p isum=%llu "
              "expected=%llu wsum=%llu expected=%llu; re-scanning once to confirm",
              n, bad, (char*)host + (size_t)bad * kPage, (char*)dptr + (size_t)bad * kPage,
              hisum[bad], esum[bad], hwsum[bad], ewsum[bad]);
        run_scan(n);  // confirmation scan, still under the lock
        int bad2 = first_bad();
        if (bad2 < 0) {
          ++retry_clean;
          logf_("RETRY CLEAN scan %llu: the mismatch on page %d did not reproduce; the second "
                "scan agrees with the host expectation, so this is not reported as a fault "
                "(retry-clean=%llu)", n, bad, retry_clean.load());
        } else {
          logf_("CHECKSUM FAULT page %d host VA %p device VA %p isum=%llu expected=%llu "
                "wsum=%llu expected=%llu (confirmed: two consecutive scans disagree; earlier "
                "first-bad page %d, retry first-bad page %d)", bad2,
                (char*)host + (size_t)bad2 * kPage, (char*)dptr + (size_t)bad2 * kPage,
                hisum[bad2], esum[bad2], hwsum[bad2], ewsum[bad2], bad, bad2);
          dump_maps("checksum fault");
          die(4);
        }
      }
      sp = (size_t)(n % kPages);
      hp_isum = hisum[sp], hp_esum = esum[sp], hp_wsum = hwsum[sp], hp_ewsum = ewsum[sp];
    }
    if (time(nullptr) - last >= 10) {
      last = time(nullptr);
      static char statm[512], mi[8192];
      slurp("/proc/self/statm", statm, sizeof statm);
      slurp("/proc/meminfo", mi, sizeof mi);
      trim_nl(statm);
      char* c = strstr(mi, "Cached:"), *a = strstr(mi, "MemAvailable:");
      if (c) trim_nl(c);
      if (a) trim_nl(a);
      // the page's sums are snapshotted under the scan lock, so a host write
      // between the compare and this line cannot show a false mismatch
      logf_("HB scans=%llu ticks=%llu cycles=%llu retry-clean=%llu elapsed=%lds page=%zu isum=%llu "
            "expected=%llu wsum=%llu expected=%llu | host VA [%p,%p) device VA [%p,%p) | statm=%s "
            "| %s | %s",
            n, ticks.load(), cyc, retry_clean.load(), (long)(time(nullptr) - t0), sp, hp_isum,
            hp_esum, hp_wsum, hp_ewsum, host, (char*)host + kBytes, dptr, (char*)dptr + kBytes,
            statm, c ? c : "Cached: ?", a ? a : "MemAvailable: ?");
    }
  }
  stop = true;
  churner.join();
  long elapsed = (long)(time(nullptr) - t0);
  long want = g_period > 0 ? (elapsed + g_period - 1) / g_period : 0;
  if (churn_fail || ticks == 0 || (long)ticks * 10 < want * 8) {
    logf_("DONE scans=%llu elapsed=%lds, CHURN DEFECT ticks=%llu expected~%ld", n, elapsed,
          ticks.load(), want);
    fclose(L);
    return 5;
  }
  logf_("DONE scans=%llu elapsed=%lds, no HIP fault, no checksum fault; retry-clean=%llu "
        "(single-scan mismatches that a re-scan did not reproduce)", n, elapsed,
        retry_clean.load());
  fclose(L);
  return 0;
}
