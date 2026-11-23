/*
 * fakehip.c - a stand-in for libamdhip64.so, used only by the CPU-only
 * self-test of hip_trace.so.
 *
 * The shim resolves every HIP symbol with dlsym(RTLD_NEXT, ...); the main
 * executable's own definitions are never preempted by an LD_PRELOAD object, so
 * the stubs have to live in a shared library that is loaded after the preload.
 * This library is that stub set: no ROCm, no GPU, no driver. It records what
 * actually reached it, which is how the self-test proves pass-through.
 */

#include <stddef.h>

int fake_hip_call_count;
unsigned long long fake_hip_registered_bytes;
void *fake_hip_last_registered;
void *fake_hip_last_memcpy_dst;
const void *fake_hip_last_memcpy_src;
unsigned long long fake_hip_last_memcpy_size;
int fake_hip_last_memcpy_kind;
void *fake_hip_last_memcpy_stream;
int fake_hip_unregister_count;
int fake_hip_sync_count;

/* Stand-in "device" memory, so a fake hipMalloc() hands back a real address. */
static unsigned char fake_device[8192];
static void *fake_host_out;

int hipHostRegister(void *p, size_t n, unsigned int flags)
{
    (void)flags;
    fake_hip_call_count++;
    fake_hip_registered_bytes = (unsigned long long)n;
    fake_hip_last_registered = p;
    return 0;
}

int hipHostUnregister(void *p)
{
    (void)p;
    fake_hip_call_count++;
    fake_hip_unregister_count++;
    return 0;
}

int hipHostMalloc(void **p, size_t n, unsigned int flags)
{
    (void)flags;
    fake_hip_call_count++;
    fake_host_out = fake_device;
    if (p)
        *p = fake_device;   /* not a real host mapping; the test does not touch it */
    (void)n;
    return 0;
}

int hipHostFree(void *p)
{
    (void)p;
    fake_hip_call_count++;
    return 0;
}

int hipMalloc(void **p, size_t n)
{
    (void)n;
    fake_hip_call_count++;
    if (p)
        *p = fake_device;
    return 0;
}

int hipFree(void *p)
{
    (void)p;
    fake_hip_call_count++;
    return 0;
}

int hipMemcpy(void *dst, const void *src, size_t n, int kind)
{
    fake_hip_call_count++;
    fake_hip_last_memcpy_dst = dst;
    fake_hip_last_memcpy_src = src;
    fake_hip_last_memcpy_size = (unsigned long long)n;
    fake_hip_last_memcpy_kind = kind;
    fake_hip_last_memcpy_stream = NULL;
    return 0;
}

int hipMemcpyAsync(void *dst, const void *src, size_t n, int kind, void *stream)
{
    fake_hip_call_count++;
    fake_hip_last_memcpy_dst = dst;
    fake_hip_last_memcpy_src = src;
    fake_hip_last_memcpy_size = (unsigned long long)n;
    fake_hip_last_memcpy_kind = kind;
    fake_hip_last_memcpy_stream = stream;
    return 0;
}

int hipMemcpyHtoD(void *dst, const void *src, size_t n)
{
    return hipMemcpy(dst, src, n, 1);
}

int hipMemcpyDtoH(void *dst, const void *src, size_t n)
{
    return hipMemcpy(dst, src, n, 2);
}

int hipMemcpyHtoDAsync(void *dst, const void *src, size_t n, void *stream)
{
    return hipMemcpyAsync(dst, src, n, 1, stream);
}

int hipMemcpyDtoHAsync(void *dst, const void *src, size_t n, void *stream)
{
    return hipMemcpyAsync(dst, src, n, 2, stream);
}

int hipMallocManaged(void **p, size_t n, unsigned int flags)
{
    (void)n;
    (void)flags;
    fake_hip_call_count++;
    if (p)
        *p = fake_device;
    return 0;
}

/*
 * hipMemAdvise is deliberately NOT defined here: the self-test calls it to
 * exercise the shim's "dlsym(RTLD_NEXT) found nothing" path (log once, pass
 * through, do not crash).
 */

int hipStreamSynchronize(void *stream)
{
    (void)stream;
    fake_hip_call_count++;
    fake_hip_sync_count++;
    return 0;
}

int hipDeviceSynchronize(void)
{
    fake_hip_call_count++;
    fake_hip_sync_count++;
    return 0;
}

int hipEventRecord(void *event, void *stream)
{
    (void)event;
    (void)stream;
    fake_hip_call_count++;
    return 0;
}

int hipStreamWaitEvent(void *stream, void *event, unsigned int flags)
{
    (void)stream;
    (void)event;
    (void)flags;
    fake_hip_call_count++;
    fake_hip_sync_count++;
    return 0;
}
