// hip-fault-repro.cpp - minimal control for the recurrent GPU permission faults.
// Fault track, measurement contract Phase 4. Build/run: see README.md.
// Device 1 reads a 64 MiB hipHostMalloc-mapped buffer continuously while a host
// thread churns page cache (write, fsync, fadvise(DONTNEED), rename). Every 10 s:
// VA ranges, a device-read page checksum, /proc/self/statm. A HIP fault or a
// checksum mismatch logs the page VA and /proc/self/maps; so do ABRT/SEGV/BUS.
// ROCm 6.1.0 has no hipErrorMemoryFault: the class arrives as
// hipErrorIllegalAddress / hipErrorLaunchFailure / hipErrorAssert or a signal.

#include <hip/hip_runtime.h>
#include <atomic>
#include <cerrno>
#include <cmath>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <fcntl.h>
#include <signal.h>
#include <string>
#include <sys/stat.h>
#include <thread>
#include <unistd.h>
#include <vector>

using u64 = unsigned long long;
static const size_t kPage = 4096, kWords = 1024, kPages = 16384, kBytes = kPages * kPage;

static FILE* L;  // also written from the signal handler
static std::atomic<bool> stop{false};
static std::atomic<u64> scans{0};

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
  logf_("---- /proc/self/maps (%s) ----", why);
  int m = open("/proc/self/maps", O_RDONLY);
  char b[65536];
  ssize_t n;
  while (m >= 0 && (n = read(m, b, sizeof b)) > 0) {
    flockfile(L);
    fwrite(b, 1, (size_t)n, L);
    fflush(L);
    funlockfile(L);
  }
  if (m >= 0) close(m);
  logf_("---- end maps ----");
}

static void on_fatal(int sig) {  // async-signal-safe writes only
  char b[192];
  int n = snprintf(b, sizeof b, "\nFATAL signal %d; last completed scan %llu; maps follow\n", sig,
                   scans.load());
  int fd = fileno(L);
  if (write(fd, b, (size_t)n) > 0) { /* best effort */ }
  int m = open("/proc/self/maps", O_RDONLY);
  if (m >= 0) {
    char buf[8192];
    ssize_t k;
    while ((k = read(m, buf, sizeof buf)) > 0)
      if (write(fd, buf, (size_t)k) <= 0) break;
    close(m);
  }
  _exit(128 + sig);
}

static inline uint32_t pattern(size_t p, size_t i) {  // finite floats, no NaN
  return (uint32_t)((p * kWords + i) & 0xFFFFFFu);
}

__global__ void scan_pages(const uint32_t* __restrict__ buf, u64* __restrict__ isum,
                           double* __restrict__ fsum, u64 pages) {
  __shared__ u64 si[256];
  __shared__ double sf[256];
  u64 p = blockIdx.x;
  if (p >= pages) return;
  unsigned t = threadIdx.x;
  const uint32_t* w = buf + p * kWords;
  u64 a = 0;
  double b = 0.0;
  for (unsigned i = t; i < kWords; i += blockDim.x) {
    a += w[i];
    b += (double)__uint_as_float(w[i]);
  }
  si[t] = a, sf[t] = b;
  __syncthreads();
  for (unsigned s = blockDim.x / 2; s > 0; s >>= 1) {
    if (t < s) si[t] += si[t + s], sf[t] += sf[t + s];
    __syncthreads();
  }
  if (t == 0) isum[p] = si[0], fsum[p] = sf[0];
}

// The conversation-cache park, without the engine: write 8 MiB, fsync, drop it
// from page cache, rename live, drop again, sleep.
static void churn(std::string dir, int period_s) {
  if (mkdir(dir.c_str(), 0755) != 0 && errno != EEXIST)
    logf_("churn: mkdir %s failed: %s", dir.c_str(), strerror(errno));
  const std::string tmp = dir + "/park.tmp", live = dir + "/park.bin";
  std::vector<char> buf(8u << 20, 'x');
  for (u64 tick = 0; !stop; ++tick) {
    int fd = open(tmp.c_str(), O_RDWR | O_CREAT | O_TRUNC, 0600), rc = 0;
    if (fd < 0) {
      logf_("churn %llu: open failed: %s", tick, strerror(errno));
      return;
    }
    if (write(fd, buf.data(), buf.size()) != (ssize_t)buf.size()) rc = errno ? errno : EIO;
    if (!rc) rc = fsync(fd);
    if (!rc) rc = posix_fadvise(fd, 0, 0, POSIX_FADV_DONTNEED);
    close(fd);
    if (!rc && rename(tmp.c_str(), live.c_str()) != 0) rc = errno;
    if (int lf = rc ? -1 : open(live.c_str(), O_RDONLY); lf >= 0)
      posix_fadvise(lf, 0, 0, POSIX_FADV_DONTNEED), close(lf);
    logf_("churn %llu: %s", tick, rc ? strerror(rc) : "8 MiB written, fsync, fadvise, rename");
    for (int i = 0; i < period_s * 10 && !stop; ++i) usleep(100000);
  }
}

static bool fault_class(hipError_t e) {
  if (e == hipErrorIllegalAddress || e == hipErrorLaunchFailure || e == hipErrorAssert) return true;
  const char* nm = hipGetErrorName(e);
  return nm && strstr(nm, "Fault");
}

static std::string line_of(const char* path, const char* key) {
  FILE* f = fopen(path, "r");
  char b[512];
  std::string r = "?";
  if (f)
    while (fgets(b, sizeof b, f))
      if (!key || !strncmp(b, key, strlen(key))) {
        size_t n = strlen(b);
        while (n && (b[n - 1] == '\n' || b[n - 1] == '\r')) b[--n] = 0;
        r = b;
        if (key) break;
      }
  if (f) fclose(f);
  return r;
}

int main(int argc, char** argv) {
  int dev = 1, churn_sec = 2;
  long duration_sec = 1800;
  std::string dummy = "./run", lp = "hip-fault-repro.log";
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
    else if (!strcmp(argv[i], "--dummy-dir")) dummy = val();
    else if (!strcmp(argv[i], "--log")) lp = val();
    else {
      fprintf(stderr, "usage: %s [--dev N] [--duration-sec N] [--churn-sec N]"
                      " [--dummy-dir DIR] [--log FILE]\n", argv[0]);
      return 2;
    }
  }
  L = fopen(lp.c_str(), "a");
  if (!L) {
    fprintf(stderr, "cannot open log %s: %s\n", lp.c_str(), strerror(errno));
    return 2;
  }
  struct sigaction sa = {};
  sa.sa_handler = on_fatal;
  sigemptyset(&sa.sa_mask);
  for (int s : {SIGABRT, SIGSEGV, SIGBUS, SIGILL}) sigaction(s, &sa, nullptr);
  signal(SIGINT, [](int) { stop = true; });
  signal(SIGTERM, [](int) { stop = true; });

  auto hipchk = [&](hipError_t e, const char* what) {
    if (e == hipSuccess) return;
    logf_("HIP FAULT in %s: %s code=%d%s", what, hipGetErrorString(e), (int)e,
          fault_class(e) ? " [fault class]" : "");
    dump_maps(what);
    exit(3);
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
  logf_("start: dev=%d %s pci=%04x:%02x:%02x.0 arch=%s duration=%lds churn=%ds dummy=%s", dev,
        prop.name, prop.pciDomainID, prop.pciBusID, prop.pciDeviceID, prop.gcnArchName,
        duration_sec, churn_sec, dummy.c_str());

  void* host = nullptr;
  hipError_t he = hipHostMalloc(&host, kBytes, hipHostMallocMapped | hipHostMallocPortable);
  if (he != hipSuccess) {  // some builds reject the mapped flag
    logf_("hipHostMalloc(mapped) failed (%s); malloc + hipHostRegister", hipGetErrorString(he));
    if (posix_memalign(&host, kPage, kBytes) != 0) return 2;
    hipchk(hipHostRegister(host, kBytes, hipHostRegisterMapped | hipHostRegisterPortable),
           "hipHostRegister");
  } else {
    hipchk(he, "hipHostMalloc");
  }
  std::vector<u64> expected(kPages), hisum(kPages);
  std::vector<double> hfsum(kPages);
  for (size_t p = 0; p < kPages; ++p) {
    uint32_t* w = (uint32_t*)host + p * kWords;
    for (size_t i = 0; i < kWords; ++i) w[i] = pattern(p, i), expected[p] += w[i];
  }
  void* dptr = nullptr;
  hipchk(hipHostGetDevicePointer(&dptr, host, 0), "hipHostGetDevicePointer");
  u64* disum = nullptr;
  double* dfsum = nullptr;
  hipchk(hipMalloc((void**)&disum, kPages * sizeof(*disum)), "hipMalloc(isum)");
  hipchk(hipMalloc((void**)&dfsum, kPages * sizeof(*dfsum)), "hipMalloc(fsum)");
  logf_("buffer: host VA [%p,%p) device VA [%p,%p)", host, (char*)host + kBytes, dptr,
        (char*)dptr + kBytes);

  std::thread churner(churn, dummy, churn_sec);
  const time_t t0 = time(nullptr);
  time_t last = 0;
  u64 n = 0;
  while (!stop && time(nullptr) - t0 < duration_sec) {
    hipLaunchKernelGGL(scan_pages, dim3((unsigned)kPages), dim3(256), 0, 0,
                       (const uint32_t*)dptr, disum, dfsum, (u64)kPages);
    hipError_t e = hipDeviceSynchronize();
    if (e != hipSuccess) {
      logf_("HIP FAULT in scan_pages: %s code=%d%s; last completed scan %llu", hipGetErrorString(e),
            (int)e, fault_class(e) ? " [fault class]" : "", n);
      logf_("buffer host VA [%p,%p) device VA [%p,%p)", host, (char*)host + kBytes, dptr,
            (char*)dptr + kBytes);
      dump_maps("device scan fault");
      exit(3);
    }
    hipchk(hipMemcpy(hisum.data(), disum, kPages * sizeof(*disum), hipMemcpyDeviceToHost), "isum");
    hipchk(hipMemcpy(hfsum.data(), dfsum, kPages * sizeof(*dfsum), hipMemcpyDeviceToHost), "fsum");
    scans = ++n;
    int bad = -1;
    for (size_t p = 0; p < kPages && bad < 0; ++p)
      if (hisum[p] != expected[p] || !std::isfinite(hfsum[p])) bad = (int)p;
    if (bad >= 0) {
      logf_("CHECKSUM FAULT page %d host VA %p device VA %p isum=%llu expected=%llu fsum=%g",
            bad, (char*)host + (size_t)bad * kPage, (char*)dptr + (size_t)bad * kPage, hisum[bad],
            expected[bad], hfsum[bad]);
      dump_maps("checksum fault");
      exit(4);
    }
    if (time(nullptr) - last >= 10) {
      last = time(nullptr);
      size_t sp = (size_t)(n % kPages);
      logf_("HB scans=%llu elapsed=%lds page=%zu isum=%llu expected=%llu fsum=%.6e | host "
            "VA [%p,%p) device VA [%p,%p) | statm=%s | %s | %s", n,
            (long)(time(nullptr) - t0), sp, hisum[sp], expected[sp], hfsum[sp], host,
            (char*)host + kBytes, dptr, (char*)dptr + kBytes,
            line_of("/proc/self/statm", nullptr).c_str(),
            line_of("/proc/meminfo", "Cached:").c_str(),
            line_of("/proc/meminfo", "MemAvailable:").c_str());
    }
  }
  stop = true;
  churner.join();
  logf_("DONE scans=%llu elapsed=%lds, no fault", n, (long)(time(nullptr) - t0));
  fclose(L);
  return 0;
}
