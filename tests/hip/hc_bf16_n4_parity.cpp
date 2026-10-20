// Synthetic finite full-word regression against THIS build's original Gemm::bf16.
// No model files, timing, output values or solution overrides. A library change may
// fail this test even on gfx906; that failure must not be replaced with a tolerance.
#include <cuda_runtime.h>
#include "strata/prefill/gemm.hpp"
#include <algorithm>
#include <charconv>
#include <chrono>
#include <cerrno>
#if defined(__linux__)
#include <poll.h>
#include <unistd.h>
#endif
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>
extern "C" int strata_hc_bf16_n4_supported(int);
extern "C" int strata_hc_bf16_n4_launch(const uint16_t*, const uint16_t*, float*, float*, int, void*);
static void check(cudaError_t e, const char* what) {
    if (e != cudaSuccess) { std::fprintf(stderr, "%s: %s\n", what, cudaGetErrorString(e)); std::exit(2); }
}
// Optional supervisor admission. Ordinary CTest invocation has no arguments and
// does not enter this path. No Stream, Gemm, or device-buffer allocation precedes it.
static bool controlled_arguments(int argc, char** argv, bool& controlled, int& device) {
    controlled = false;
    if (argc == 1) return true;
    if (argc != 4 || std::strcmp(argv[1], "--controlled") || std::strcmp(argv[2], "--device")) return false;
    const char* end = argv[3] + std::strlen(argv[3]);
    if (end == argv[3]) return false;
    for (const char* p = argv[3]; p != end; ++p) if (*p < '0' || *p > '9') return false;
    const auto parsed = std::from_chars(argv[3], end, device);
    if (parsed.ec != std::errc{} || parsed.ptr != end || device < 0) return false;
    controlled = true;
    return true;
}
static bool controlled_start() {
#if defined(__linux__)
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(15);
    char input[7]{};
    size_t used = 0;
    while (used < sizeof(input)) {
        const auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(
            deadline - std::chrono::steady_clock::now()).count();
        if (remaining <= 0) return false;
        pollfd p{STDIN_FILENO, POLLIN, 0};
        const int ready = poll(&p, 1, static_cast<int>(remaining));
        if (ready < 0 && errno == EINTR) continue;
        if (ready <= 0 || (p.revents & (POLLERR | POLLNVAL))) return false;
        const auto n = read(STDIN_FILENO, input + used, sizeof(input) - used);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return false;
        used += static_cast<size_t>(n);
        if (std::memchr(input, '\n', used)) return used == 6 && !std::memcmp(input, "START\n", 6);
    }
#endif
    return false;
}
static bool controlled_identity(int device) {
#if defined(STRATA_HIP_GFX906) && defined(__linux__)
    cudaDeviceProp prop{};
    int visible = 0;
    char pci[64]{};
    check(cudaGetDeviceProperties(&prop, device), "controlled properties");
    check(cudaGetDeviceCount(&visible), "controlled device count");
    check(hipDeviceGetPCIBusId(pci, sizeof(pci), device), "controlled PCI identity");
    if (std::printf("DEVICE ordinal=%d pci=%s arch=%s visible=%d\n"
                    "READY admission=required timeout_seconds=15\n", device, pci, prop.gcnArchName, visible) < 0 ||
        std::fflush(stdout) != 0) return false;
    return controlled_start();
#else
    (void)device;
    return false;
#endif
}
static uint32_t rng(uint32_t& s) { s ^= s<<13; s ^= s>>17; s ^= s<<5; return s; }
template<class U> struct Buffer {
    static constexpr size_t guard = 128;  // 256-byte-aligned interior for BF16 and FP32
    U* base = nullptr;
    size_t n;
    std::vector<U> host;
    explicit Buffer(size_t count) : n(count), host(count+2*guard) {
        std::memset(host.data(), 0xa5, host.size()*sizeof(U));
        check(cudaMalloc(reinterpret_cast<void**>(&base), host.size()*sizeof(U)), "allocation");
        upload();
    }
    U* device() { return base+guard; }
    U* data() { return host.data()+guard; }
    void upload() { check(cudaMemcpy(base,host.data(),host.size()*sizeof(U),cudaMemcpyHostToDevice),"upload"); }
    std::vector<U> read(bool immutable=false) {
        std::vector<U> got(host.size());
        check(cudaMemcpy(got.data(),base,got.size()*sizeof(U),cudaMemcpyDeviceToHost),"readback");
        if (std::memcmp(got.data(),host.data(),guard*sizeof(U)) ||
            std::memcmp(got.data()+guard+n,host.data()+guard+n,guard*sizeof(U)) ||
            (immutable && std::memcmp(got.data(),host.data(),got.size()*sizeof(U)))) {
            std::fputs("input or guard changed\n",stderr); std::exit(1);
        }
        return got;
    }
    ~Buffer() { if(base)check(cudaFree(base),"free"); }
};
struct Stream {
    cudaStream_t value = nullptr;
    Stream() { check(cudaStreamCreate(&value),"stream"); }
    ~Stream() { check(cudaStreamSynchronize(value),"final drain");check(cudaStreamDestroy(value),"stream destroy"); }
};
static void fixture(std::vector<uint16_t>& x, std::vector<uint16_t>& w, int t, int id) {
    std::fill(x.begin(),x.end(),uint16_t(0));std::fill(w.begin(),w.end(),uint16_t(0x3f80));
    if(id==0)for(size_t i=0;i<x.size();++i)x[i]=(i&1)?0x8000:0;
    if(id==1||id==2)for(int row=0;row<t;++row){auto p=x.data()+size_t(row)*10240;p[0]=0x3f80;p[id==1?16:1]=uint16_t((127-25)<<7);p[id==1?32:2]=0xbf80;}
    if(id==3)for(int row=0;row<t;++row)x[size_t(row)*10240]=(row&1)?0x8001:0x0001;
    if(id==4){for(int row=0;row<t;++row)x[size_t(row)*10240]=uint16_t(((row&1)<<15)|((127-70)<<7));std::fill(w.begin(),w.end(),uint16_t((127-70)<<7));}
    if(id==5){for(size_t i=0;i<x.size();++i)x[i]=uint16_t((((i/10240)&1)<<15)|(254<<7));std::fill(w.begin(),w.end(),uint16_t((127-120)<<7));}
    if(id==6){for(size_t i=0;i<x.size();++i)x[i]=uint16_t(((i&1)<<15)|((127-120)<<7)|(i%128));std::fill(w.begin(),w.end(),uint16_t((127+110)<<7));}
    if(id==7){for(size_t i=0;i<x.size();++i)x[i]=uint16_t(((i&1)<<15)|((127+60)<<7));std::fill(w.begin(),w.end(),uint16_t((127+60)<<7));}
    if(id==8){uint32_t s=0x6ac18309u;for(auto& a:x){auto v=rng(s);a=uint16_t(((v>>31)<<15)|((65+v%116)<<7)|((v>>8)&127));}for(auto& a:w){auto v=rng(s);a=uint16_t(((v>>31)<<15)|((65+v%116)<<7)|((v>>8)&127));}}
    if(id==9){std::fill(w.begin(),w.end(),uint16_t(0));w[0]=1;w[10240]=0x8001;for(int row=0;row<t;++row)x[size_t(row)*10240]=0x3f80;}
}
int main(int argc, char** argv) {
    bool controlled = false;
    int selected = -1;
    if (!controlled_arguments(argc, argv, controlled, selected)) {
        std::fputs("invalid arguments: expected no arguments or --controlled --device ORDINAL\n", stderr); return 2;
    }
    if (controlled) check(cudaSetDevice(selected), "controlled device selection");
    int device=-1;check(cudaGetDevice(&device),"device");
    if (controlled && device != selected) return 2;
    const int supported=strata_hc_bf16_n4_supported(device);
    if(supported<0)return 2;
    if(!supported){std::puts("SKIP: requires gfx906 wave64");return controlled ? 2 : 77;}
    if (controlled && !controlled_identity(device)) {
        std::fputs("controlled admission failed (identity/platform, START, EOF, or timeout)\n", stderr); return 2;
    }
    Stream stream;
    strata::prefill::Gemm gemm;
    std::string err;
    if(!gemm.init(stream.value,1<<20,err)){std::fprintf(stderr,"Gemm::init: %s\n",err.c_str());return 2;}
    // The reference is the original BF16/F32 call, with exact production strides.
    gemm.set_f16_io(false);
    for(int t:{4096,4095}) {
        const size_t outputs=size_t(t)*4;
        Buffer<uint16_t> dx(size_t(t)*10240),dw(4*10240);
        Buffer<uint32_t> ref(outputs),got(outputs),partial(outputs*3);
        std::vector<uint16_t> x(dx.n),w(dw.n);
        for(int id=0;id<10;++id) {
            fixture(x,w,t,id);std::copy(x.begin(),x.end(),dx.data());std::copy(w.begin(),w.end(),dw.data());
            // Distinct NaN poisons: missing writes cannot pass merely by matching old bytes.
            std::fill_n(ref.data(),outputs,0x7fc00001u);std::fill_n(got.data(),outputs,0x7fc00002u);
            std::fill_n(partial.data(),outputs*3,0x7fc00003u);
            dx.upload();dw.upload();ref.upload();got.upload();partial.upload();
            gemm.bf16(dx.device(),dw.device(),reinterpret_cast<float*>(ref.device()),t,4,10240,4,0.0f,0);
            check(cudaGetLastError(),"reference launch");check(cudaStreamSynchronize(stream.value),"reference drain");
            check(static_cast<cudaError_t>(strata_hc_bf16_n4_launch(dx.device(),dw.device(),reinterpret_cast<float*>(got.device()),
                reinterpret_cast<float*>(partial.device()),t,stream.value)),"candidate launch");
            check(cudaStreamSynchronize(stream.value),"candidate drain");
            const auto a=ref.read(),b=got.read();partial.read();dx.read(true);dw.read(true);
            size_t different=0,nonfinite=0;
            for(size_t i=0;i<outputs;++i){const auto aa=a[i+ref.guard],bb=b[i+got.guard];different+=aa!=bb;
                nonfinite+=((aa&0x7f800000u)==0x7f800000u)||((bb&0x7f800000u)==0x7f800000u);}
            std::printf("HC_N4_PARITY T=%d fixture=%d words=%zu different=%zu nonfinite=%zu\n",t,id,outputs,different,nonfinite);
            if(different||nonfinite)return 1;
        }
    }
    std::puts("PASS: 20 synthetic full-word cases; actual-weight/shadow/model qualification remains separate");
}
