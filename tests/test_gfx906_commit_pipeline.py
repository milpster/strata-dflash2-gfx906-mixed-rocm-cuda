#!/usr/bin/env python3
"""Host-only regression tests for gfx906 commit ownership and empty PCIe guards.

Run from the repository root: python3 tests/test_gfx906_commit_pipeline.py
Requires Linux x86-64, Python 3, and a C++17 compiler (CXX, default c++).
Compiles actual extracted production methods against a deterministic fake runtime
under both HIP and non-HIP guards. No GPU, HIP SDK, or model is required.
Expected negative publication tests abort child processes; core dumps are disabled.
These tests do not establish GPU ordering, token parity, or performance.
"""
from pathlib import Path
import os, resource, shlex, subprocess, tempfile
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
ROOT = Path(__file__).resolve().parents[1]
src=(ROOT/'src/core/verify.cpp').read_text()
gen=(ROOT/'src/program/generate.cpp').read_text()
hdr=(ROOT/'include/strata/core/verify.hpp').read_text()
def extract(s, marker):
    start=s.index(marker); op=s.index('{',start); depth=1; pos=op+1
    while depth:
        depth += (s[pos]=='{')-(s[pos]=='}'); pos+=1
    return s[start:pos]
methods='\n'.join(extract(src,m) for m in ['bool gfx906_opt_in(', 'bool no_pcie_group_device(', 'bool Verifier::can_defer_primary_commit(', 'bool Verifier::commit(', 'bool Verifier::wait_commit(', 'void Verifier::publish_plan('])
context=extract(hdr,'struct PrimaryCommitContext')+';'
scope=extract(gen,'struct PrimaryCommitScope')+';'
preamble=r'''
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>
#include <thread>
#include <stdexcept>
#include <sys/mman.h>
#include <sys/wait.h>
#include <unistd.h>
#include <csignal>
#include <xmmintrin.h>
#define CHECK(x) do { if (!(x)) { std::fprintf(stderr,"FAIL line %d: %s\n", __LINE__, #x); std::exit(1); } } while(0)
using Clock=std::chrono::steady_clock;
using cudaError_t=int; using cudaEvent_t=void*;
constexpr int cudaSuccess=0;
struct cudaDeviceProp { int warpSize=64; char gcnArchName[64]="gfx906:sramecc+"; };
static cudaDeviceProp props[2];
static int prop_error=0, launch_error=0, record_error=0;
static int stream_error[2]={},event_error[2]={},syncs[2]={},events[2]={};
static int device=0; static std::vector<std::string> order;
struct OnDevice { int prev; OnDevice(int d):prev(device){device=d;} ~OnDevice(){device=prev;} };
int cudaGetDeviceProperties(cudaDeviceProp* p,int d){if(prop_error || d<0 || d>1)return 9; *p=props[d];return 0;}
int cudaGetLastError(){return 0;}
const char* cudaGetErrorString(int){return "fake failure";}
int cudaStreamSynchronize(void*){++syncs[device];order.push_back("stream"+std::to_string(device));return stream_error[device];}
int cudaEventSynchronize(void*){++events[device];order.push_back("event"+std::to_string(device));return event_error[device];}
int cudaGraphLaunch(void*,void*){order.push_back("launch"+std::to_string(device));return launch_error;}
int cudaEventRecord(void*,void*){return record_error;}
int cudaStreamQuery(void*){return 0;}
int cudaEventCreate(void** p){*p=(void*)1;return 0;}
int cudaEventElapsedTime(float* p,void*,void*){*p=0;return 0;}
double ms_since(Clock::time_point){return 0;}
namespace strata { namespace kernels {
bool gfx_arch_is(const char* s,const char* expected){size_t n=std::strlen(expected);return std::strncmp(s,expected,n)==0&&(s[n]==0||s[n]==':');}
}}
static bool g_commit_async=false;
struct Verifier {
CONTEXT
 bool can_defer_primary_commit(const PrimaryCommitContext&) const;
 bool commit(int,std::string&,const PrimaryCommitContext* =nullptr);
 bool wait_commit(std::string&);
 static void publish_plan(void*);
 Verifier* next_=nullptr; int device_=0; bool last_batch_=false,batch_rec_=false,always_publish_=false;
 void* ext_stream_=nullptr; std::atomic<bool> released_{false};
 bool primary_commit_live_=false,commit_pending_=false; int last_t_=2,max_t_=2,last_pos0_=0;
 int params[4]={}; int* h_commit_=params; void *cs_=(void*)1,*commit_exec_=(void*)1,*commit_done_=(void*)1;
 int last_tokens_[2]={}; struct Session{int ple_prev[2]={};} session; Session* ss_=&session;
 double ms_commit=0; bool self_commit=false;
 bool one_token_self_commit()const{return self_commit;} bool ple_stage()const{return false;}
 bool no_pcie_group_=false; struct Sink{int* counts=nullptr;}sink_;
 uint32_t cur_layer_=3,flag=0; uint32_t* h_flagA_=&flag; int* released_count=nullptr;
 void release_gpu_waits(int){if(released_count)++*released_count;}
};
namespace strata { namespace core { using Verifier=::Verifier; }}
'''.replace('CONTEXT',context)
tests=r'''
static void reset() {
 for(const char* n:{"STRATA_PRIMARY_COMMIT_OVERLAP","STRATA_NO_PCIE_GROUP","STRATA_COMMIT_SYNC","STRATA_COMMIT_TIMING","STRATA_SPLIT_OVERLAP"})unsetenv(n);
 props[0]=cudaDeviceProp{};props[1]=cudaDeviceProp{};
 prop_error=launch_error=record_error=0;g_commit_async=false;device=0;order.clear();
 for(int i=0;i<2;++i){stream_error[i]=event_error[i]=syncs[i]=events[i]=0;}
}
static Verifier::PrimaryCommitContext eligible(){return {true,1,0,0,false,false};}
int main(){
 reset(); Verifier a,b; a.next_=&b;b.device_=1;auto ctx=eligible();std::string err;
 for(const char* value:{"","0","true","10","01"}){setenv("STRATA_PRIMARY_COMMIT_OVERLAP",value,1);CHECK(!a.can_defer_primary_commit(ctx));setenv("STRATA_NO_PCIE_GROUP",value,1);CHECK(!no_pcie_group_device(0));}
 setenv("STRATA_PRIMARY_COMMIT_OVERLAP","1",1);setenv("STRATA_NO_PCIE_GROUP","1",1);
#ifdef STRATA_USE_HIP
 CHECK(a.can_defer_primary_commit(ctx));CHECK(no_pcie_group_device(0));
 props[1].warpSize=32;CHECK(!a.can_defer_primary_commit(ctx));CHECK(!no_pcie_group_device(1));props[1].warpSize=64;
 std::strcpy(props[1].gcnArchName,"gfx1100");CHECK(!a.can_defer_primary_commit(ctx));CHECK(!no_pcie_group_device(1));props[1]=cudaDeviceProp{};
 prop_error=1;CHECK(!a.can_defer_primary_commit(ctx));CHECK(!no_pcie_group_device(0));prop_error=0;
 for(const char* n:{"STRATA_COMMIT_SYNC","STRATA_COMMIT_TIMING","STRATA_SPLIT_OVERLAP"}){setenv(n,"1",1);CHECK(!a.can_defer_primary_commit(ctx));unsetenv(n);}
 for(int k=0;k<6;++k){auto c=ctx;if(k==0)c.mtp=false;if(k==1)c.draft_device=0;if(k==2)c.batch=1;if(k==3)c.pipeline_windows=1;if(k==4)c.pipeline_active=true;if(k==5)c.pipeline_ran=true;CHECK(!a.can_defer_primary_commit(c));}
 for(int k=0;k<2;++k){Verifier& v=k?b:a;v.last_batch_=true;CHECK(!a.can_defer_primary_commit(ctx));v.last_batch_=false;v.batch_rec_=true;CHECK(!a.can_defer_primary_commit(ctx));v.batch_rec_=false;v.always_publish_=true;CHECK(!a.can_defer_primary_commit(ctx));v.always_publish_=false;v.ext_stream_=(void*)1;CHECK(!a.can_defer_primary_commit(ctx));v.ext_stream_=nullptr;v.released_=true;CHECK(!a.can_defer_primary_commit(ctx));v.released_=false;}
 CHECK(a.commit(2,err,&ctx));CHECK(a.primary_commit_live_&&a.commit_pending_);CHECK(syncs[0]==0&&syncs[1]==1);CHECK(device==0);
 CHECK(!a.commit(1,err,&ctx));CHECK(a.h_commit_[0]==2);CHECK(a.wait_commit(err));CHECK(!a.primary_commit_live_&&!a.commit_pending_);CHECK(events[0]==1);
 // A globally async last stage must still finish before the draft starts.
 g_commit_async=true;CHECK(a.commit(2,err,&ctx));CHECK(!b.commit_pending_);CHECK(events[1]==1);CHECK(a.wait_commit(err));g_commit_async=false;
 // Event record failure owns the graph until a direct stream drain succeeds.
 record_error=3;stream_error[0]=4;CHECK(!a.commit(2,err,&ctx));CHECK(a.primary_commit_live_);stream_error[0]=0;CHECK(a.wait_commit(err));CHECK(!a.primary_commit_live_);record_error=0;
 // Preserve the first event error while draining both stages and clearing ownership on successful fallback.
 CHECK(a.commit(2,err,&ctx));b.commit_pending_=true;event_error[0]=5;int before=events[1];CHECK(!a.wait_commit(err));CHECK(!a.primary_commit_live_);CHECK(events[1]==before+1);CHECK(err.find("primary commit")!=std::string::npos);event_error[0]=0;
 // Exception and skipped/EOS draft paths both drain before a following window can reuse parameters.
 SCOPE
 bool joined=false;std::thread worker([&]{joined=true;});
 try { CHECK(a.commit(2,err,&ctx));PrimaryCommitScope scope{a,true,&worker};throw std::runtime_error("draft"); }catch(const std::runtime_error&){}
 CHECK(!a.primary_commit_live_&&!worker.joinable()&&joined);
 CHECK(a.commit(2,err,&ctx));{PrimaryCommitScope scope{a,true,nullptr};}CHECK(!a.primary_commit_live_);
#else
 CHECK(!a.can_defer_primary_commit(ctx));CHECK(!no_pcie_group_device(0));
#endif
 // With no context, the existing primary sync and final-stage async behavior remain unchanged.
 reset();CHECK(a.commit(2,err));CHECK(syncs[0]==1&&syncs[1]==1);g_commit_async=true;CHECK(a.commit(2,err));CHECK(syncs[0]==2&&b.commit_pending_);CHECK(a.wait_commit(err));
 // Shared publish_plan covers run/service/batch; reject null or nonzero count before publishing A.
 int counts[3]={0,0,0};a.no_pcie_group_=true;a.sink_.counts=counts;a.publish_plan(&a);CHECK(a.flag==4);
 auto* shared=(uint32_t*)mmap(nullptr,4096,PROT_READ|PROT_WRITE,MAP_SHARED|MAP_ANONYMOUS,-1,0);CHECK(shared!=MAP_FAILED);
 for(int mode=0;mode<2;++mode){shared[0]=17;shared[1]=0;pid_t child=fork();CHECK(child>=0);if(child==0){a.h_flagA_=shared;a.released_count=(int*)(shared+1);counts[2]=1;a.sink_.counts=mode?nullptr:counts;a.publish_plan(&a);_exit(99);}int status=0;CHECK(waitpid(child,&status,0)==child);CHECK(WIFSIGNALED(status)&&WTERMSIG(status)==SIGABRT);CHECK(shared[0]==17&&shared[1]==1);}
 munmap(shared,4096);a.no_pcie_group_=false;counts[2]=5;a.sink_.counts=counts;a.publish_plan(&a);CHECK(a.flag==4);
 std::puts("PASS: real extracted eligibility, ownership, drain, RAII, fallback, and publication methods");
}
'''.replace('SCOPE',scope)
# Also bind insertion sites which the reduced host class cannot exercise.
assert 'if (primary_commit_live_) { err = "verify: primary commit must drain before the next run";' in src
assert 'no_pcie_group_ = device_ok && bal_ == nullptr && no_pcie_group_device(device_);' in src
assert 'const bool defer_primary_commit = primary_commit_eligible && !merged;' in gen
assert gen.index('// Includes skipped drafts') < gen.index('pcie_auto_step(req_pcie_frac);',gen.index('// Includes skipped drafts'))
with tempfile.TemporaryDirectory() as tmp:
 p=Path(tmp); (p/'test.cpp').write_text(preamble+'\n'+methods+'\n'+tests)
 for backend,flags in [('hip',['-DSTRATA_USE_HIP']),('fallback',[])]:
  subprocess.run([*shlex.split(os.environ.get('CXX', 'c++')),'-std=c++17','-O2','-pthread',*flags,str(p/'test.cpp'),'-o',str(p/backend)],check=True)
  subprocess.run([str(p/backend)],check=True)
print('PASS: source binding and both host backend builds')
