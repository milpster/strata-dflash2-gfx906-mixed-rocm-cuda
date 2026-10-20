#pragma once
// Independent optional HC down/up storage. Never lent to/from cache, carve or N4.
#include <cuda_runtime.h>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
extern "C" int strata_hc_f16_exact_supported(int owner);
namespace strata::prefill::hc_f16_exact {
constexpr size_t kBytes=90440448, kCharge=96u<<20, kFloor=256u<<20;
[[noreturn]] inline void fatal(const char* why) {
 std::fprintf(stderr,"HC_F16_EXACT fatal: %s\n",why);std::exit(1);
}
struct Owner {
 int previous=-1;
 Owner(int owner,bool active) {
  if(!active)return;
  int current=-1;if(owner<0||cudaGetDevice(&current)!=cudaSuccess)fatal("owner query");
  if(current!=owner){if(cudaSetDevice(owner)!=cudaSuccess)fatal("owner switch");previous=current;}
 }
 ~Owner(){if(previous>=0&&cudaSetDevice(previous)!=cudaSuccess)fatal("owner restore");}
 Owner(const Owner&)=delete;Owner& operator=(const Owner&)=delete;
};
template<class Drain> struct DrainOnExit {
 Drain drain;bool active;
 void dismiss(){active=false;}
 ~DrainOnExit(){if(active){try{if(!drain())fatal("exceptional chain drain");}catch(...){fatal("exceptional chain exception");}}}
};
template<class Drain> DrainOnExit(Drain, bool) -> DrainOnExit<Drain>;
struct State {
 void* scratch=nullptr;void* stream=nullptr;int owner=-1;
 bool prepared=false,enabled=false,shadow=false,reference_only=false,report=false;
 uint64_t calls=0,takes=0,guard_fallback=0,other_fallback=0;
 const char* reason="unprepared";
 static bool flag(const char* key,bool& valid){const char* v=std::getenv(key);if(!v||!std::strcmp(v,"0"))return false;if(!std::strcmp(v,"1"))return true;valid=false;return false;}
 bool prepare(int device,void* compute,bool compatible,std::string& err){
  if(prepared)return true;
  bool valid=true;enabled=flag("STRATA_HC_F16_EXACT",valid);shadow=flag("STRATA_HC_F16_EXACT_SHADOW",valid);reference_only=flag("STRATA_HC_F16_EXACT_REFERENCE_ONLY",valid);
  if(!valid||((shadow||reference_only)&&!enabled)||(shadow&&reference_only)){err="HC_F16_EXACT: invalid mode";return false;}
  prepared=true;owner=device;stream=compute;report=enabled||std::getenv("STRATA_HC_F16_EXACT");reason=enabled?"ineligible":"disabled";
  if(!enabled||!compatible)return true;
  Owner scope(owner,true);int supported=strata_hc_f16_exact_supported(owner);
  if(supported<0){err="HC_F16_EXACT: architecture query failed";return false;}
  if(!supported){reason="architecture";return true;}
  size_t before=0,total=0,after=0;
  if(cudaMemGetInfo(&before,&total)!=cudaSuccess){err="HC_F16_EXACT: memory query failed";return false;}
  reason="headroom";if(before<kFloor+kCharge)return true;
  void* p=nullptr;auto status=cudaMalloc(&p,kBytes);
  if(status!=cudaSuccess){if(status!=cudaErrorMemoryAllocation){err="HC_F16_EXACT: allocation API failure";return false;}(void)cudaGetLastError();reason="allocation";return true;}
  bool queried=cudaMemGetInfo(&after,&total)==cudaSuccess;
  if(!queried||after<kFloor||(before>after&&before-after>kCharge)||(reinterpret_cast<uintptr_t>(p)&255)){
   if(cudaFree(p)!=cudaSuccess)fatal("refused allocation free");
   if(!queried){err="HC_F16_EXACT: post-allocation query failed";return false;}
   reason="post-budget";return true;
  }
  scratch=p;reason="active";
  std::fprintf(stderr,"HC_F16_EXACT_ALLOC owner=%d bytes=%zu charge=%zu floor=%zu before=%zu after=%zu\n",owner,kBytes,kCharge,kFloor,before,after);return true;
 }
 static bool span(const void* p,uint64_t bytes,uintptr_t& begin,uintptr_t& end){begin=reinterpret_cast<uintptr_t>(p);if(!p||!bytes||bytes>UINTPTR_MAX-begin)return false;end=begin+bytes;return true;}
 static bool separate(uintptr_t a,uintptr_t b,uintptr_t c,uintptr_t d){return b<=c||d<=a;}
 bool eligible(const uint16_t* x,const uint16_t* w,float* y,int64_t t,int64_t n,int64_t k,int64_t ldy,float beta,int64_t ldx,bool f16,void* compute){
  if(!enabled)return false;
  ++calls;
  if(!scratch||f16||beta!=0.0f||ldx!=0||(t!=4095&&t!=4096)||!((n==320&&k==10240)||(n==10240&&k==320))||ldy<n||ldy>INT32_MAX){++other_fallback;return false;}
  uintptr_t xb,xe,wb,we,yb,ye,sb,se;
  if(!span(x,uint64_t(t)*k*2,xb,xe)||!span(w,uint64_t(n)*k*2,wb,we)||!span(y,uint64_t(t)*ldy*4,yb,ye)||!span(scratch,kBytes,sb,se)||(xb&1)||(wb&1)||(yb&3)||(sb&255)||!separate(sb,se,xb,xe)||!separate(sb,se,wb,we)||!separate(sb,se,yb,ye)||!separate(xb,xe,wb,we)||!separate(xb,xe,yb,ye)||!separate(wb,we,yb,ye)){++other_fallback;return false;}
  int current=-1;if(cudaGetDevice(&current)!=cudaSuccess||current!=owner||compute!=stream)fatal("call owner/stream changed");
  return true;
 }
 // Caller joined stage chain and checked compute drain on owner; clear Gemm view first.
 void release(){if(!scratch)return;Owner scope(owner,true);if(cudaFree(scratch)!=cudaSuccess)fatal("workspace free");scratch=nullptr;}
 ~State(){if(scratch)fatal("workspace lost without drain");if(report)std::fprintf(stderr,"HC_F16_EXACT_STATE owner=%d enabled=%d shadow=%d reference_only=%d reason=%s calls=%llu takes=%llu guard_fallback=%llu other_fallback=%llu\n",owner,int(enabled),int(shadow),int(reference_only),reason,(unsigned long long)calls,(unsigned long long)takes,(unsigned long long)guard_fallback,(unsigned long long)other_fallback);}
};
}
