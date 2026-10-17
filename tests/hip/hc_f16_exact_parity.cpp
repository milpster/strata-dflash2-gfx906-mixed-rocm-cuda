// Actual bound Gemm::bf16 versus THIS build's unbound BF16 reference.
// Synthetic full-word component evidence only; never shadow-overwrite the candidate.
#include <cuda_runtime.h>
#include "strata/prefill/gemm.hpp"
#include "hc_f16_exact.hpp"
#include "hc_f16_exact_checks.hpp"
#include <algorithm>
#include <array>
#include <charconv>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>
#ifndef STRATA_HC_F16_EXACT_BUILT
#error "This test requires the real HC F16 exact Gemm path"
#endif
namespace hc = strata::prefill::hc_f16_exact;
using hc_test::require;
static void check(cudaError_t code,const char* what) {
    if(code!=cudaSuccess) { std::fprintf(stderr,"HC_F16_TEST runtime %s: %s\n",what,cudaGetErrorString(code));std::exit(2); }
}
static void env(const char* key,const char* value) {
#ifdef _WIN32
    require(_putenv_s(key,value?value:"")==0,"set environment");
#else
    require((value?setenv(key,value,1):unsetenv(key))==0,"set environment");
#endif
}
struct Stream {
    cudaStream_t value=nullptr;
    Stream(){check(cudaStreamCreate(&value),"create stream");}
    ~Stream(){check(cudaStreamSynchronize(value),"last stream drain");check(cudaStreamDestroy(value),"destroy stream");}
};
template<class U> struct Buffer {
    static constexpr size_t guard=128; U* base=nullptr;size_t count;cudaStream_t stream;std::vector<U> host;
    Buffer(size_t n,cudaStream_t s):count(n),stream(s),host(n+2*guard) {
        std::memset(host.data(),0xa5,host.size()*sizeof(U));check(cudaMalloc(reinterpret_cast<void**>(&base),host.size()*sizeof(U)),"buffer allocation");
    }
    ~Buffer(){if(base){check(cudaStreamSynchronize(stream),"drain before buffer free");check(cudaFree(base),"buffer free");}}
    U* device(){return base+guard;} U* data(){return host.data()+guard;}
    void upload(){check(cudaMemcpyAsync(base,host.data(),host.size()*sizeof(U),cudaMemcpyHostToDevice,stream),"upload");}
    std::vector<U> read(bool immutable=false) {
        std::vector<U> got(host.size());check(cudaMemcpyAsync(got.data(),base,got.size()*sizeof(U),cudaMemcpyDeviceToHost,stream),"readback");check(cudaStreamSynchronize(stream),"readback drain");
        require(!std::memcmp(got.data(),host.data(),guard*sizeof(U)) && !std::memcmp(got.data()+guard+count,host.data()+guard+count,guard*sizeof(U)),"allocation guard changed");
        if(immutable)require(!std::memcmp(got.data(),host.data(),got.size()*sizeof(U)),"input words changed");
        return got;
    }
};
struct Workspace {
    hc::State state;strata::prefill::Gemm& gemm;cudaStream_t stream;
    Workspace(strata::prefill::Gemm& g,int device,cudaStream_t s,const char* enable,bool reference=false):gemm(g),stream(s) {
        env("STRATA_HC_F16_EXACT",enable);env("STRATA_HC_F16_EXACT_SHADOW","0");env("STRATA_HC_F16_EXACT_REFERENCE_ONLY",reference?"1":"0");
        std::string error;if(!state.prepare(device,s,true,error))throw std::runtime_error(error);require(!state.shadow,"shadow is not an independent output");
        if(enable&&std::strcmp(enable,"1")==0)require(state.scratch&&std::strcmp(state.reason,"active")==0,"enabled path failed to provision its independent workspace");
        else require(!state.enabled&&!state.scratch,"default OFF allocated/enabled HC storage");
        gemm.bind_hc_f16_exact(&state);
    }
    ~Workspace(){check(cudaStreamSynchronize(stream),"drain before HC release");gemm.bind_hc_f16_exact(nullptr);state.release();}
};
static hc_test::Counts counts(const hc::State& s){return {s.calls,s.takes,s.guard_fallback,s.other_fallback};}
struct Shape { int64_t t,n,k; };
static void run_case(strata::prefill::Gemm& reference,strata::prefill::Gemm& candidate,Workspace& workspace,
                     Shape shape,hc_test::Fixture fixture,const char* mode,Buffer<uint16_t>& x,Buffer<uint16_t>& w,
                     Buffer<uint32_t>& ref,Buffer<uint32_t>& got,bool padded=false,bool x_f16=false,bool f16_io=false) {
    const size_t t=shape.t,n=shape.n,k=shape.k,ld=n+(padded?8:0),outputs=t*ld;
    constexpr uint32_t poison_ref=0x7fc000a1u,poison_got=0x7fc000b2u;
    hc_test::fill(x.data(),t,k,w.data(),n,fixture);std::fill_n(ref.data(),ref.count,poison_ref);std::fill_n(got.data(),got.count,poison_got);
    x.upload();w.upload();ref.upload();got.upload();check(cudaStreamSynchronize(workspace.stream),"fixture upload drain");
    reference.set_f16_io(f16_io);candidate.set_f16_io(f16_io);
    const auto before=counts(workspace.state);
    reference.bf16(x.device(),w.device(),reinterpret_cast<float*>(ref.device()),shape.t,shape.n,shape.k,ld,0.0f,0,x_f16);
    check(cudaGetLastError(),"reference launch");check(cudaStreamSynchronize(workspace.stream),"reference drain");
    const auto a=ref.read();x.read(true);w.read(true);
    candidate.bf16(x.device(),w.device(),reinterpret_cast<float*>(got.device()),shape.t,shape.n,shape.k,ld,0.0f,0,x_f16);
    check(cudaGetLastError(),"candidate launch");check(cudaStreamSynchronize(workspace.stream),"candidate drain");
    const auto after=counts(workspace.state);const bool bypass=!workspace.state.enabled||x_f16||f16_io;
    hc_test::Counts wanted{};
    if(!bypass) { wanted.calls=1;if(hc_test::is_guard(fixture))wanted.guard=1;else if(workspace.state.reference_only)wanted.other=1;else wanted.takes=1; }
    hc_test::delta(before,after,wanted);
    if(!bypass) {
        require((n*k+t*k)*sizeof(uint16_t)+64<=hc::kBytes,"conversion/stat workspace bound");
        unsigned guard[10]{};auto stats=static_cast<uint16_t*>(workspace.state.scratch)+n*k+t*k;
        check(cudaMemcpyAsync(guard,stats,sizeof(guard),cudaMemcpyDeviceToHost,workspace.stream),"conversion guard read");check(cudaStreamSynchronize(workspace.stream),"guard read drain");hc_test::check_guard(fixture,guard);
    }
    const auto b=got.read();x.read(true);w.read(true);const auto c=hc_test::compare(a.data()+ref.guard,b.data()+got.guard,t,n,ld,poison_ref,poison_got);
    // The allocation is sized for the padded variant; contiguous cases must leave its tail alone.
    for(size_t i=outputs;i<ref.count;++i)require(a[i+ref.guard]==poison_ref&&b[i+got.guard]==poison_got,"inactive output allocation tail changed");
    std::printf("HC_F16_COMPONENT T=%lld N=%lld K=%lld ldy=%zu mode=%s fixture=%s words=%zu different=%zu nonfinite=%zu calls=%llu takes=%llu guard_fallback=%llu other_fallback=%llu x_f16=%d f16_io=%d\n",
        static_cast<long long>(shape.t),static_cast<long long>(shape.n),static_cast<long long>(shape.k),ld,mode,hc_test::name(fixture),c.words,c.different,c.nonfinite,
        static_cast<unsigned long long>(after.calls-before.calls),static_cast<unsigned long long>(after.takes-before.takes),
        static_cast<unsigned long long>(after.guard-before.guard),static_cast<unsigned long long>(after.other-before.other),int(x_f16),int(f16_io));
    require(c.different==0,"full F32 word mismatch");
    require(hc_test::allows_nonfinite(fixture)||c.nonfinite==0,"unexpected nonfinite output");
    reference.set_f16_io(false);candidate.set_f16_io(false);
}
int main(int argc,char** argv) {
    try {
        require(std::getenv("HSA_OVERRIDE_GFX_VERSION")==nullptr,"HSA_OVERRIDE_GFX_VERSION must be absent");
        int device=0;
        if(argc!=1) { require(argc==3&&!std::strcmp(argv[1],"--device"),"usage: hc_f16_exact_parity [--device ORDINAL]");const char* end=argv[2]+std::strlen(argv[2]);auto r=std::from_chars(argv[2],end,device);require(r.ec==std::errc{}&&r.ptr==end&&device>=0&&argv[2]!=end,"invalid device ordinal"); }
        // Isolate this route from optional earlier dispatch/tuning overrides before any handle exists.
        env("STRATA_WMMA_GEMM","0");env("STRATA_HIPBLASLT_TUNING",nullptr);env("STRATA_ROCBLAS_TUNING",nullptr);env("STRATA_HIP_PROMPT_F16","0");
        check(cudaSetDevice(device),"select device");const int supported=strata_hc_f16_exact_supported(device);
        require(supported>=0,"architecture query failed");if(!supported){std::puts("SKIP HC_F16_COMPONENT requires gfx906 wave64");return 77;}
        std::printf("HC_F16_COMPONENT_DEVICE ordinal=%d gfx906_wave64=1 arch_override_absent=1\n",device);
        Stream stream;strata::prefill::Gemm reference,candidate;std::string error;
        if(!reference.init(stream.value,1<<20,error))throw std::runtime_error(error);
        if(!candidate.init(stream.value,1<<20,error))throw std::runtime_error(error);
        reference.bind_hc_f16_exact(nullptr);reference.set_f16_io(false);candidate.set_f16_io(false);
        size_t cases=0;
        for(int64_t t:{4095,4096}) for(auto shape:{Shape{t,320,10240},Shape{t,10240,320}}) {
            Buffer<uint16_t> x(size_t(t)*shape.k,stream.value),w(size_t(shape.n)*shape.k,stream.value);
            Buffer<uint32_t> a(size_t(t)*(shape.n+8),stream.value),b(size_t(t)*(shape.n+8),stream.value);
            for(const char* off:{static_cast<const char*>(nullptr),"0"}) { Workspace session(candidate,device,stream.value,off);run_case(reference,candidate,session,shape,hc_test::Fixture::dense,off?"explicit_off":"default_off",x,w,a,b);++cases; }
            {
                Workspace session(candidate,device,stream.value,"1");
                for(auto f:{hc_test::Fixture::dense,hc_test::Fixture::cancellation,hc_test::Fixture::signed_zero,
                            hc_test::Fixture::w_inf,hc_test::Fixture::x_nan,hc_test::Fixture::w_range,hc_test::Fixture::x_range,hc_test::Fixture::w_loss,hc_test::Fixture::x_loss}) {
                    run_case(reference,candidate,session,shape,f,"enabled",x,w,a,b,f==hc_test::Fixture::dense);++cases;
                }
                run_case(reference,candidate,session,shape,hc_test::Fixture::x_f16_bits,"x_f16_excluded",x,w,a,b,false,true,false);++cases;
                run_case(reference,candidate,session,shape,hc_test::Fixture::x_f16_bits,"f16_io_excluded",x,w,a,b,false,false,true);++cases;
            }
            { Workspace session(candidate,device,stream.value,"1",true);run_case(reference,candidate,session,shape,hc_test::Fixture::dense,"reference_only",x,w,a,b);++cases; }
        }
        require(cases==56,"fixed complete component case census");
        std::puts("PASS HC_F16_COMPONENT cases=56 positive_takes=12 guard_fallbacks=24 bypasses=16 reference_only=4 fullword=1 model_trajectory=0 timing=0");return 0;
    } catch(const std::exception& e) { std::fprintf(stderr,"HC_F16_COMPONENT FAIL: %s\n",e.what());return 1; }
}
