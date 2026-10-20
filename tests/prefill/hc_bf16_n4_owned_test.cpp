#include "hc_bf16_n4.hpp"
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <cassert>
#include <algorithm>
#include <iostream>
#include <future>
#include <stdexcept>
using strata::prefill::hc_n4::State;
namespace n4 = strata::prefill::hc_n4;
namespace {
int calls=0, current=7, allocs=0, frees=0, launches=0, supports=1, malloc_status=0;
int mem_status=0, launch_status=0, sync_status=0, fail_set=-1;
size_t free_bytes=n4::kHeadroom+n4::kCharge, charged=n4::kCharge;
alignas(256) unsigned char storage[n4::kBytes];
std::vector<unsigned> order;
void flags(const char* on, const char* shadow="0") {
#ifdef _WIN32
    _putenv_s("STRATA_HC_BF16_N4", on ? on : "");
    _putenv_s("STRATA_HC_BF16_N4_SHADOW", shadow);
#else
    if(on) setenv("STRATA_HC_BF16_N4",on,1);else unsetenv("STRATA_HC_BF16_N4");
    setenv("STRATA_HC_BF16_N4_SHADOW",shadow,1);
#endif
}
struct Project {
    void* stream() const { return reinterpret_cast<void*>(0x80); }
    void bf16(const uint16_t*, const uint16_t*, float* y, int64_t t, int64_t n, int64_t k,
              int64_t ldy, float beta, int64_t ldx) {
        assert(n==4 && k==10240 && ldy==4 && beta==0 && ldx==0);
        order.push_back(2);
        std::fill_n(y, size_t(t)*4, 1.0f);
    }
};
}
int cudaGetDevice(int* p) { ++calls; *p=current; return 0; }
int cudaSetDevice(int d) { ++calls; if(d==fail_set)return 9; current=d; return 0; }
int cudaMemGetInfo(size_t* p,size_t* total) { ++calls;*p=free_bytes;*total=size_t(16)<<30;return mem_status; }
int cudaMalloc(void** p,size_t n) { ++calls;assert(current==1 && n==n4::kBytes);if(malloc_status)return malloc_status;
    ++allocs;*p=storage;free_bytes-=charged;return 0; }
int cudaFree(void* p) { ++calls;assert(current==1 && p==storage);++frees;free_bytes+=charged;return 0; }
int cudaGetLastError() { ++calls;return 0; }
int cudaMemcpyAsync(void* d,const void* s,size_t n,int kind,cudaStream_t) { ++calls;assert(kind==cudaMemcpyDeviceToHost);std::memcpy(d,s,n);return 0; }
int cudaStreamSynchronize(cudaStream_t) { ++calls;return sync_status; }
extern "C" int strata_hc_bf16_n4_supported(int d) { ++calls;assert(d==1 && current==1);return supports; }
extern "C" int strata_hc_bf16_n4_launch(const uint16_t*,const uint16_t*,float* y,float* p,int t,void*) {
    assert(current==1 && p==reinterpret_cast<float*>(storage));++launches;order.push_back(1);
    if(launch_status)return launch_status;
    std::fill_n(y,size_t(t)*4,1.0f);return 0;
}
int main(int argc,char** argv) {
    // Exact cleanup guard: the primary may have no allocation/direct future while
    // a descendant owns scratch and has an outstanding successor task.
    int drained = 0;
    struct Node {
        bool owns = false;
        Node* next = nullptr;
        std::future<bool> job;
        int* drained = nullptr;
        bool active_chain() const { return owns || (next && next->active_chain()); }
        bool drain() {
            bool ok = true;
            if (job.valid()) { ok = job.get(); ++*drained; }
            if (next) ok = next->drain() && ok;
            return ok;
        }
    } peer, primary;
    peer.owns = true; peer.drained = &drained;
    primary.next = &peer; primary.drained = &drained;
    peer.job = std::async(std::launch::async, [] { return true; });
    try {
        n4::DrainOnExit guard{[&] { return primary.drain(); }, primary.active_chain()};
        throw std::runtime_error("partial parent exit");
    } catch (const std::runtime_error&) {}
    assert(drained == 1 && !peer.job.valid());
    { n4::DrainOnExit guard{[&] { ++drained; return true; }, false}; }
    assert(drained == 1);
    { n4::DrainOnExit guard{[&] { ++drained; return true; }, true}; guard.dismiss(); }
    assert(drained == 1);
    if (argc > 1 && std::string(argv[1]) == "drain-failure") {
        n4::DrainOnExit guard{[] { return false; }, true};
        return 0;  // destructor must terminate before this successful return completes
    }
    if (argc > 1 && std::string(argv[1]) == "drain-exception") {
        n4::DrainOnExit guard{[]() -> bool { throw std::runtime_error("future"); }, true};
        return 0;
    }
    std::string err; Project gm;
    // OFF does not query the device or allocate; malformed controls reject before API use.
    flags("0");{State s;int n=calls;assert(s.prepare(1,true,err));assert(calls==n && !s.scratch);}
    flags("yes");{State s;int n=calls;assert(!s.prepare(1,true,err));assert(calls==n);}
    flags("0","1");{State s;assert(!s.prepare(1,true,err));}
    flags("1");{State s;int n=calls;assert(s.prepare(1,false,err));assert(calls==n && !s.scratch);}
    supports=0;{State s;assert(s.prepare(1,true,err) && !s.scratch);assert(current==7);}supports=1;
    supports=-1;{State s;assert(s.prepare(1,true,err) && !s.scratch);assert(current==7);
        assert(std::string(s.reason)=="architecture-query");}supports=1;
    free_bytes=n4::kHeadroom+n4::kCharge-1;
    {State s;int a=allocs;assert(s.prepare(1,true,err) && !s.scratch);assert(allocs==a && current==7);}
    free_bytes=n4::kHeadroom+n4::kCharge;
    malloc_status=cudaErrorMemoryAllocation;{State s;assert(s.prepare(1,true,err) && !s.scratch);assert(current==7);}malloc_status=0;
    mem_status=9;{State s;int a=allocs;assert(s.prepare(1,true,err) && !s.scratch && a==allocs);}mem_status=0;
    // Refuse an allocator charge beyond the disclosed budget, and free on the owner.
    charged=n4::kCharge+256;{State s;assert(s.prepare(1,true,err) && !s.scratch);assert(current==7);}charged=n4::kCharge;
    if(argc>1 && std::string(argv[1])=="shadow")flags("1","1");
    State s;assert(s.prepare(1,true,err) && s.scratch);assert(current==7);
    const int a=allocs,n=calls;assert(s.prepare(1,true,err));assert(allocs==a && calls==n);
    alignas(256) static uint16_t x[4096*10240],w[4*10240];
    alignas(256) static float y[4096*4];
    strata::core::WeightRef weight;weight.data=w;weight.bytes=sizeof(w);weight.ne0=10240;weight.ne1=4;
    weight.kind=strata::core::WeightKind::Bf16InF32;
    auto call=[&](int t,bool f16=false,const uint16_t* low=nullptr,int ldx=10240,float* out=nullptr) {
        return s.call(gm,&weight,x,out ? out : y,t,f16,low,ldx,gm.stream(),err);
    };
    current=1;
    if(argc>1 && std::string(argv[1])=="launch-failure")launch_status=9;
    assert(call(4096)==1);assert(call(4095)==1);
    if(s.shadow){assert(order==std::vector<unsigned>({1,2,1,2}));assert(s.counts[0].words==16384);}
    const int l=launches;
    assert(call(3391)==0 && call(1)==0 && call(0)==0 && call(-1)==0);
    assert(call(4096,true)==0 && call(4096,false,x)==0 && call(4096,false,nullptr,10304)==0);
    weight.ne1=5;assert(call(4096)==0);weight.ne1=4;
    weight.ne0=10239;assert(call(4096)==0);weight.ne0=10240;
    weight.bytes=81919;assert(call(4096)==0);weight.bytes=81920;
    weight.kind=strata::core::WeightKind::F32;assert(call(4096)==0);weight.kind=strata::core::WeightKind::Bf16InF32;
    assert(call(4096,false,nullptr,10240,reinterpret_cast<float*>(x))==0);
    assert(call(4096,false,nullptr,10240,s.scratch)==0);
    assert(s.call(gm,&weight,x,y,4096,false,nullptr,10240,nullptr,err)==0);
    assert(launches==l);
    current=7;assert(call(4096)==-1);assert(launches==l);
    uintptr_t lo=0,hi=0;assert(!State::span(reinterpret_cast<void*>(UINTPTR_MAX-1),4,lo,hi));
    if(argc>1 && std::string(argv[1])=="restore-failure")fail_set=7;
    s.release();assert(!s.scratch && current==7);int f=frees;s.release();assert(f==frees);
    std::cout<<"HC N4 owned policy, dispatch, owner and lifecycle checks passed\n";
}
