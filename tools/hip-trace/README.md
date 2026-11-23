# hip-trace - call-order evidence for the XDMA page fault

An `LD_PRELOAD` shim that writes one line per HIP buffer-lifecycle call and per
host virtual-memory call that can revoke a page translation. It exists so that a
run can answer one question from evidence instead of from reading code:

> **was a device copy still outstanding when this host range was unmapped?**

The fault it was built for is `amdgpu ... [mmhub0] no-retry page fault`,
`Faulty UTCL2 client ID: XDMA`, `PERMISSION_FAULTS: 0x3`, on host VA the engine
had registered. A minimal HIP repro produces that fault class when a host range
is unmapped while a registration is live and the copy engine is reading it.
This tool records the order, the return codes and the arguments of the calls
that lead there.

It never changes behaviour: every call returns the real implementation's value,
`errno` is restored, and no lock is held across a real call.

## Build

```sh
make            # hip_trace.so, libfakehip.so, selftest
make check      # CPU-only self-test, prints the log it produced
```

No ROCm headers are needed, by design: the few HIP types are declared in
`hip_trace.c` (`typedef int hipError_t;`, `void *` handles), so the shim cannot
disagree with the installed headers. Equivalent by hand:

```sh
gcc -O2 -g -Wall -fPIC -shared -pthread -o hip_trace.so hip_trace.c -ldl
```

## Use

```sh
mkdir -p /tmp/opencode
LD_PRELOAD=$PWD/hip_trace.so \
HIP_TRACE_LOG=/tmp/opencode/hip-trace.log \
  ./strata --serve ...

# optional: follow one host buffer
LD_PRELOAD=$PWD/hip_trace.so \
HIP_TRACE_LOG=/tmp/opencode/hip-trace.log \
HIP_TRACE_WATCH=0x7f6a4c000000:4096 \
  ./strata --serve ...
```

| variable | default | meaning |
| --- | --- | --- |
| `HIP_TRACE_LOG` | `/tmp/opencode/hip-trace.log` | output file, opened `O_APPEND` at start-up |
| `HIP_TRACE_WATCH` | off | `ptr:size`; adds `watch=1 arg=N` when an argument overlaps |
| `HIP_TRACE_MMAP` | `1` | `0` silences `mmap`/`munmap`/`madvise`/`posix_fadvise` lines |
| `HIP_TRACE_MMAP_MIN` | `0` | do not log libc VM calls shorter than this, in bytes |

Lines are written with a single `write(2)` each, so the file is complete up to
the moment of a crash - which is the point, since the fault kills the process.
The file is opened `O_APPEND` and is never truncated: remove it between runs.
`HIP_TRACE_WATCH` takes `ptr` and `size` in hex or decimal (`0x7f6a...:4096` or
`1407...:4096`); a range only matches when both are non-zero.

## What a line means

```
<monotonic_us> <tid> <call> ret=<value> errno=<errno> dur_us=<call_duration> <args...> [watch=..]
```

| call | key arguments |
| --- | --- |
| `hipHostRegister` | `ptr size flags` |
| `hipHostUnregister` | `ptr reg_size live_regs live_overlap async_since_sync` |
| `hipHostMalloc`, `hipMallocManaged` | `ptr out size flags` (`out` = returned allocation) |
| `hipHostFree`, `hipFree` | `ptr live_overlap async_since_sync` |
| `hipMalloc` | `ptr out size` |
| `hipMemcpy`, `hipMemcpyAsync`, `hipMemcpyHtoD[Async]`, `hipMemcpyDtoH[Async]` | `dst src size kind kindname stream` |
| `hipMemAdvise` | `ptr size advice device` |
| `hipStreamSynchronize`, `hipDeviceSynchronize` | `stream`, `async_before` |
| `hipEventRecord`, `hipStreamWaitEvent` | `event stream flags` |
| `mmap` | `addr retp len prot flags fd off` |
| `munmap`, `madvise` | `addr len live_overlap live_regs async_since_sync [live_reg0=ptr+size]` |
| `posix_fadvise` | `fd off len advice` |

`ret` is the call's own return value: a HIP error code, or for the libc calls
the usual 0/-1 (for `posix_fadvise`, the error code itself). `errno` is the
process errno right after the real call.

`live_overlap` is the number of live `hipHostRegister` ranges intersecting the
range being unmapped or advised, counted at that moment. It is the direct
answer to "was this host range still registered when it went away": a
`munmap` line with `live_overlap=1` is the pattern that faults.

`async_since_sync` counts successful async copies since the last successful
`hipStreamSynchronize`, `hipDeviceSynchronize` or `hipStreamWaitEvent`. It is a
proxy - see the limits below.

`dur_us` is how long the host API call itself took. For `hipMemcpyAsync` that is
the enqueue time, not the transfer.

`watch=1 arg=N` names the argument position (1 = `dst`/first pointer,
2 = `src`) that overlapped `HIP_TRACE_WATCH`.

## The grep recipe

The whole sequence around the claim, in file order:

```sh
LOG=/tmp/opencode/hip-trace.log
grep -n -e ' hipHostRegister ' -e ' hipHostUnregister ' \
        -e ' hipMemcpyAsync ' -e ' hipMemcpyHtoDAsync ' -e ' hipMemcpyDtoHAsync ' \
        -e ' hipStreamSynchronize ' -e ' hipDeviceSynchronize ' -e ' hipStreamWaitEvent ' \
        -e ' munmap ' -e ' madvise ' "$LOG"
```

Then let awk pair each registration with what happened to it:

```sh
awk '
  $3 == "hipHostRegister"  { reg = $0; pend = 0; next }
  $3 ~ /Async$/ && reg      { pend++; copy = $0; next }
  $3 ~ /^(hipStreamSynchronize|hipDeviceSynchronize|hipStreamWaitEvent)$/ { pend = 0; next }
  $3 == "munmap" || $3 == "madvise" || $3 == "hipHostUnregister" {
      if (reg && pend > 0)
          printf "OUTSTANDING COPY AT %s\n  registered: %s\n  copy:       %s\n  then:       %s\n\n", $3, reg, copy, $0
      if ($3 == "hipHostUnregister") reg = ""
  }
' "$LOG"
```

An empty output means: for every registration that was followed by a
`munmap`/`madvise`/`hipHostUnregister`, a synchronisation or stream-wait
happened in between. Any stanza is a candidate for the fault class. To find the
strongest form directly:

```sh
grep -n ' munmap ' "$LOG" | grep -v 'live_overlap=0'
```

## Self-test (no GPU)

`./selftest` re-executes itself twice under the shim and checks, against the
log it just produced:

- ordering: `mmap` before `hipHostRegister` before `hipMemcpyAsync` before
  `munmap`, and `munmap` before the synchronise;
- arguments and return codes: sizes, `kindname=H2D`, `advice=4`, `ret=0`;
- the diagnostic: the `munmap` line carries `live_overlap=1` and
  `async_since_sync=1`;
- an absent symbol: `hipMemAdvise` is left out of the stub library, so the
  shim's `dlsym(RTLD_NEXT, ...)` fails; it must log that once (two calls, one
  notice) and pass the call through without crashing;
- transparency: the stub library recorded the same pointer, size, kind and call
  counts it would have seen without the shim;
- watch mode: `watch=1 arg=2` on the copy, `watch=1` on the unmap, `watch=0`
  on an unrelated pointer.

The HIP symbols come from `libfakehip.so`, which is a shared library and not a
definition in the test binary: the executable's own definitions are never
preempted by a preload, so stubs have to live after the shim in the search
order. Logs are kept in `/tmp/opencode/hip-trace-selftest/`
(`HIPTRACE_SELFTEST_DIR` overrides). Nothing in the self-test touches
`/dev/kfd`, a driver or an engine.

## Limits

- **Calls, not completion.** The shim sees that `hipMemcpyAsync` was enqueued.
  It cannot see the copy engine retire it. An outstanding copy is *inferred*
  from the absence of a synchronise or stream-wait between the enqueue and the
  `munmap`. That inference is the whole evidence base; treat it as such.
- **`async_since_sync` is a global proxy**, not per stream: it counts all async
  copies since *any* sync, so a sync on stream A clears a copy queued on stream
  B. Use the awk recipe, which is per registration, before concluding anything;
  the counter is a hint that says where to look.
- **A copy may complete without any synchronise**, so a small
  `async_since_sync` at `munmap` does not prove safety, and a large one does
  not by itself prove the fault.
- **Registration table** is 1024 live entries, ring-reused: in a process with
  more than 1024 concurrent registrations the oldest entry is forgotten and
  `live_overlap` can undercount. A `live_overlap=1` is always real; a `0` can
  be a table miss.
- **`live_overlap` says the range was registered, not that the driver was
  touching it.** Combine it with the copy lines.
- **Direct syscalls and intra-libc calls are not interposed.** Code that calls
  `syscall(SYS_munmap, ...)`, or libc internals that call their own alias,
  bypass the hook. HIP library internals normally call libc through the PLT, so
  they are seen.
- **`malloc`/`free` are deliberately not intercepted**: they are on the hot
  path, and a hook there is re-entered by `dlsym` and by any library used
  before libc is ready to satisfy the log path. Host ranges obtained from
  `malloc` are still visible through the `mmap`/`munmap`/`madvise` calls that
  back them.
- **Symbols not in the list are not logged**, and some of them move host
  bytes: `hipMemcpyToSymbol`, `hipMemcpy2D[Async]`, `hipMemcpy3D[Async]`,
  `hipMemcpyPeer[Async]`, `hipMemcpyDtoD`. A read-only grep of the engine
  sources finds `hipMemcpyToSymbol` and `hipMemcpy2DAsync` referenced there
  (unconfirmed static analysis, not a measurement), so a host-to-symbol or
  host-to-device 2D copy would not appear in this log. `hipMemset` and
  `hipMemsetAsync` are not logged either, but they write device memory and so
  cannot be the host-page read this tool is about.
  `hipHostGetDevicePointer` is not logged, so the device-side address of a
  registered range has to be read from the engine's own log, not from here.
- **A symbol that `dlsym(RTLD_NEXT)` cannot find** produces one
  `note=no_symbol` line and the call is passed through with `hipSuccess`; for
  `mmap`/`munmap`/`madvise`/`posix_fadvise` it goes to a raw syscall instead,
  so a resolution failure cannot change the libc behaviour.
- **Volume**: libc VM calls are logged process-wide, including the loader's and
  allocator's. On a long run use `HIP_TRACE_MMAP_MIN=65536` to keep only large
  mappings, and remember that filtering can hide the range you care about.
