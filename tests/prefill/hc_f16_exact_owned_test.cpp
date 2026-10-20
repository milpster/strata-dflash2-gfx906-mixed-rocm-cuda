// Host-only policy/ownership regression. This does not prove GPU arithmetic parity.
#include "hc_f16_exact.hpp"
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <cassert>
#include <stdexcept>
#include <iostream>
#include <utility>
namespace hc = strata::prefill::hc_f16_exact;
namespace {
int calls=0, current=7, support=1, allocation_status=0, query_status=0, allocations=0, frees=0;
size_t available=hc::kFloor+hc::kCharge, charge=hc::kCharge;
alignas(256) unsigned char storage[hc::kBytes];
void flags(const char* enable, const char* shadow="0", const char* reference="0") {
#ifdef _WIN32
    _putenv_s("STRATA_HC_F16_EXACT", enable ? enable : "");
    _putenv_s("STRATA_HC_F16_EXACT_SHADOW",shadow);
    _putenv_s("STRATA_HC_F16_EXACT_REFERENCE_ONLY",reference);
#else
    if(enable) setenv("STRATA_HC_F16_EXACT",enable,1); else unsetenv("STRATA_HC_F16_EXACT");
    setenv("STRATA_HC_F16_EXACT_SHADOW",shadow,1);
    setenv("STRATA_HC_F16_EXACT_REFERENCE_ONLY",reference,1);
#endif
}
void reset() { available=hc::kFloor+hc::kCharge; charge=hc::kCharge; allocation_status=query_status=0;support=1;current=7; }
}
int cudaGetDevice(int* p) { ++calls; *p=current; return cudaSuccess; }
int cudaSetDevice(int d) { ++calls; current=d; return cudaSuccess; }
int cudaMemGetInfo(size_t* p,size_t* total) { ++calls; *p=available; *total=size_t(16)<<30; return query_status; }
int cudaMalloc(void** p,size_t n) { ++calls; assert(current==1 && n==hc::kBytes); if(allocation_status)return allocation_status; ++allocations; *p=storage; available-=charge; return cudaSuccess; }
int cudaFree(void* p) { ++calls; assert(current==1 && p==storage); ++frees;available+=charge;return cudaSuccess; }
int cudaGetLastError() { ++calls;return cudaSuccess; }
extern "C" int strata_hc_f16_exact_supported(int d) { ++calls;assert(current==d && d==1);return support; }
int main() {
    static_assert(hc::kBytes==90440448 && hc::kCharge==(96u<<20) && hc::kFloor==(256u<<20));
    std::string err; void* stream=reinterpret_cast<void*>(0x80);
    for(const char* value : {static_cast<const char*>(nullptr),"0"}) {
        flags(value); hc::State state; const int before=calls;
        assert(state.prepare(1,stream,true,err));assert(!state.scratch);assert(calls==before);
    }
    for(const char* value : {"", "true", "01", "2", "-1"}) {
        flags(value);hc::State state;const int before=calls;
        assert(!state.prepare(1,stream,true,err));assert(calls==before);
    }
    for(auto pair : {std::pair{"0","1"},std::pair{"1","1"}}) {
        flags(pair.first,"1",pair.second);hc::State state;
        assert(!state.prepare(1,stream,true,err));
    }
    flags("1");
    { hc::State state;int before=calls;assert(state.prepare(1,stream,false,err));assert(calls==before && !state.scratch); }
    for(int test=0;test<7;++test) {
        reset();if(test==0)support=0;if(test==1)available=hc::kFloor+hc::kCharge-1;
        if(test==2)allocation_status=cudaErrorMemoryAllocation;
        if(test==3)allocation_status=9;
        if(test==4)query_status=9;
        if(test==5)support=-1;
        if(test==6){available=hc::kFloor+2*hc::kCharge;charge=hc::kCharge+1;}
        hc::State state;bool ok=state.prepare(1,stream,true,err);
        assert(ok==(test<3||test==6));assert(!state.scratch && current==7);
    }
    reset();
    {
        hc::State state;assert(state.prepare(1,stream,true,err));assert(state.scratch==storage && current==7);
        const int before=calls;assert(state.prepare(1,stream,true,err));assert(calls==before);
        current=1;
        // Deliberately fabricated non-dereferenced addresses for span/alias policy tests only.
        const auto* x=reinterpret_cast<const uint16_t*>(uintptr_t(0x10000000));
        const auto* w=reinterpret_cast<const uint16_t*>(uintptr_t(0x20000000));
        auto* y=reinterpret_cast<float*>(uintptr_t(0x30000000));
        auto eligible=[&](int64_t t,int64_t n,int64_t k,int64_t ld,float beta=0,int64_t lx=0,bool f16=false){
            return state.eligible(x,w,y,t,n,k,ld,beta,lx,f16,stream);
        };
        for(int64_t t : {4095,4096}) {assert(eligible(t,320,10240,320));assert(eligible(t,10240,320,10240));}
        for(int64_t t : {0,1,4094,4097})assert(!eligible(t,320,10240,320));
        assert(!eligible(4096,4,10240,4));assert(!eligible(4096,320,10239,320));
        assert(!eligible(4096,320,10240,319));assert(!eligible(4096,320,10240,int64_t(INT32_MAX)+1));
        assert(!eligible(4096,320,10240,320,1));assert(!eligible(4096,320,10240,320,0,10241));
        assert(!eligible(4096,320,10240,320,0,0,true));
        assert(!state.eligible(nullptr,w,y,4096,320,10240,320,0,0,false,stream));
        assert(!state.eligible(x,x,y,4096,320,10240,320,0,0,false,stream));
        assert(!state.eligible(x,w,reinterpret_cast<float*>(const_cast<uint16_t*>(x)),4096,320,10240,320,0,0,false,stream));
        assert(!state.eligible(reinterpret_cast<uint16_t*>(storage),w,y,4096,320,10240,320,0,0,false,stream));
        assert(!state.eligible(reinterpret_cast<uint16_t*>(uintptr_t(0x10000001)),w,y,4096,320,10240,320,0,0,false,stream));
        assert(!state.eligible(reinterpret_cast<uint16_t*>(UINTPTR_MAX-1),w,y,4096,320,10240,320,0,0,false,stream));
        current=7;state.release();assert(current==7 && !state.scratch);
    }
    assert(allocations==frees);
    int drains=0;
    try { hc::DrainOnExit guard{[&]{++drains;return true;},true};throw std::runtime_error("unwind"); } catch(const std::runtime_error&) {}
    assert(drains==1);
    { hc::DrainOnExit guard{[&]{++drains;return true;},false}; }
    { hc::DrainOnExit guard{[&]{++drains;return true;},true};guard.dismiss(); }
    assert(drains==1);
    std::cout<<"HC F16 host policy/ownership PASS\n";
}
