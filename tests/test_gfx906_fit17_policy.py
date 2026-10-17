#!/usr/bin/env python3
"""Host-only tests of the actual FIT17 predicate and dispatch policy.
Run from any directory. Requires Python 3 and a C++17 compiler (CXX or g++).
No GPU, copied baseline checkout, cloud paths or third-party Python packages.
"""
from pathlib import Path
import os
import shlex
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
source = (ROOT / "src/kernels/cuda/qsa_select.cu").read_text()
start = source.index("__device__ __forceinline__ bool gfx906_topk_window_fits17")
end = source.index("\n}", start) + 2
predicate = source[start:end].replace("__device__", "").replace("__forceinline__", "inline")
start = source.index("static const bool guarded_fit17 = [] {")
end = source.index("\n    }();", start) + len("\n    }();")
option = source[start:end].replace("static const bool guarded_fit17 = ", "return ", 1)
start = source.index("if (guarded_fit17 &&") + len("if (")
end = source.index(") {", start)
condition = source[start:end]

preamble = r'''
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <initializer_list>
constexpr int kStepCount=5, kStepNKv=1, kStepNBid=2, TK_T=1024, TK_PER=33, TK_PER_MAX=66;
constexpr int64_t GFX906_TOPK_SHORT_CELLS=24576;
bool current_device;
bool topk_current_device_is_gfx906() { return current_device; }
'''
main = r'''
int main(int argc,char** argv) {
 if(argc!=2) return 2;
 const bool guarded_fit17=enabled(), expected=std::strcmp(argv[1],"on")==0;
 if(guarded_fit17!=expected) return 3;
 for (bool old : {false,true}) for (bool counted : {false,true})
 for (bool device : {false,true}) for (int nq : {1,2,4,6,8,9,16})
 for (int64_t reach : {0,33791,33792,33793,65538,67584,67585,131074}) {
   current_device=device;
   bool want=expected && !old && !counted && device && nq<=8 && reach>33792 && reach<=67584;
   if(selected(guarded_fit17,old,counted,nq,reach)!=want) return 4;
 }
 int checks=0;
 for(int nq=1;nq<=8;++nq)
 for(int last : {4096,24575,24576,24577,65536,69631,69632,200000})
 for(int high : {0,6144,16384,17406,17407,17408,50000})
 for(int pos=0;pos<nq;++pos) {
   int32_t steps[8*kStepCount]={};
   steps[(nq-1)*kStepCount+kStepNKv]=last;
   steps[pos*kStepCount+kStepNBid]=high;
   bool reg=gfx906_topk_window_fits17(steps,nq);
   bool wide=!gfx906_topk_window_fits17(steps,nq);
   if(reg!=(last>24576 && high+1<=17408) || int(reg)+int(wide)!=1) return 5;
   ++checks;
 }
 return checks==2016 ? 0 : 6;
}
'''

def build(folder, name, pred=predicate, cond=condition):
    cpp=folder/(name+".cpp"); exe=folder/name
    cpp.write_text(preamble + pred + "\nbool enabled() { " + option + " }\n" +
                   "bool selected(bool guarded_fit17,bool old,bool counted,int64_t nq,int64_t reach) { return " +
                   cond + "; }\n" + main)
    subprocess.run(shlex.split(os.environ.get("CXX","g++")) +
                   ["-std=c++17","-O2",str(cpp),"-o",str(exe)],check=True)
    return exe

def run(exe, reg, fit):
    env=os.environ.copy()
    for key,value in [("STRATA_GFX906_TOPK_REG",reg),("STRATA_GFX906_TOPK_FIT17",fit)]:
        env.pop(key,None)
        if value is not None: env[key]=value
    return subprocess.run([str(exe),"on" if reg=="2" and fit=="1" else "off"],env=env).returncode

with tempfile.TemporaryDirectory(prefix="fit17-policy-") as tmp:
    folder=Path(tmp); exe=build(folder,"policy")
    values=[None,"","0","1","2","true","01","02","1x","2x"," 1","2 "]
    for reg in values:
        for fit in values:
            if run(exe,reg,fit): raise SystemExit(f"FAIL REG={reg!r} FIT17={fit!r}")
    print("PASS 144 exact-option combinations, 448 host-policy cases and 2016 device-predicate cases per combination")
    mutations={
        "lost_count_guard":(predicate,condition.replace("!counted","true")),
        "lost_old_guard":(predicate,condition.replace("!old","true")),
        "lost_device_guard":(predicate,condition.replace("topk_current_device_is_gfx906()","true")),
        "lost_window_guard":(predicate,condition.replace("nq <= 8","true")),
        "lost_capacity_guard":(predicate,condition.replace("reach <= (int64_t) TK_T * TK_PER_MAX","true")),
        "lost_short_boundary":(predicate.replace("<= GFX906_TOPK_SHORT_CELLS","< GFX906_TOPK_SHORT_CELLS"),condition),
        "lost_whole_window_scan":(predicate.replace("q < nq","q == nq - 1"),condition),
        "off_by_one_fit":(predicate.replace("max_bid + 1","max_bid"),condition),
    }
    for name,(pred,cond) in mutations.items():
        assert pred!=predicate or cond!=condition
        if run(build(folder,name,pred,cond),"2","1")==0:
            raise SystemExit("FAIL surviving mutation: "+name)
        print("PASS rejected mutation:",name)

# Guard wiring must use the same predicate in complementary directions before barriers.
for kernel,call in [
    ("block_topk_reg_kernel", "if (!gfx906_topk_window_fits17(steps, (int) gridDim.x)) return;"),
    ("block_topk_wide_kernel", "if (gfx906_topk_window_fits17(steps, (int) gridDim.x)) return;")]:
    start=source.index("__global__ void __launch_bounds__(TK_T) "+kernel)
    body=source[start:source.index("\n}\n",start)+2]
    assert body.index(call)<body.index("__syncthreads()")
assert "block_topk_reg_kernel<17, true>" in source
assert "block_topk_wide_kernel<false, true>" in source
assert "block_topk_kernel<true>" not in source
assert "block_topk_reg_kernel<TK_PER_MAX, true" not in source
print("PASS complementary pre-barrier guards; no old guarded reference/reg66 launches")
