#!/usr/bin/env python3
"""Extract the actual option initializer and dispatcher; spy on row calls.
No production instrumentation or x86 instructions are needed for this test.
"""
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src/kernels/cpu/q2_avx2.cpp"
source = SOURCE.read_text()
start = source.index("const bool kUnpackSpread = [] {")
option = source[start:source.index("\n}();", start) + len("\n}();")]
start = source.index("void q2_0_gguf_rows_multi_avx2_v(")
dispatcher = source[start:source.index("\nvoid q2_0_gguf_rows_multi_avx2_legacy(", start)]
preamble = r"""
#include <cstdint>
#include <cstddef>
#include <cstdlib>
#include <cstring>
#include <vector>
#include <cstdio>
struct ActQ {};
struct Call { int kind, nt, a_offset, out_offset, r0, r1; };
std::vector<Call> calls;
const ActQ* const* base_a;
float* const* base_out;
const uint8_t* expected_w;
size_t expected_stride;
int expected_nb;
void record(int kind, const uint8_t* w,size_t rb,int nb,const ActQ* const* a,int nt,float* const* out,int r0,int r1) {
    if(w!=expected_w || rb!=expected_stride || nb!=expected_nb) std::abort();
    calls.push_back({kind,nt,int(a-base_a),int(out-base_out),r0,r1});
}
#define SPY(NS,KIND) namespace NS { void rows_nt(const uint8_t* w,size_t rb,int nb,const ActQ* const* a,int nt,float* const* out,int r0,int r1) { record(KIND,w,rb,nb,a,nt,out,r0,r1); } }
SPY(plain,0)
SPY(spread,1)
SPY(vnni,2)
"""
main = r"""
int main(int argc,char**argv) {
    if(argc!=2) return 2;
    bool enabled=std::strcmp(argv[1],"on")==0;
    if(kUnpackSpread!=enabled) return 3;
    ActQ storage[13]; const ActQ* acts[13]; float values[13]; float* outs[13];
    for(int i=0;i<13;++i) {acts[i]=&storage[i]; outs[i]=&values[i];}
    base_a=acts; base_out=outs;
    uint8_t weights[1]; expected_w=weights; expected_stride=727;
    size_t cases=0, spread_calls=0,vnni_calls=0,plain_calls=0;
    for(int nb:{0,1,9,10,11,39,40,41})
    for(int nt=0;nt<=13;++nt)
    for(bool vn:{false,true}) {
        expected_nb=nb; calls.clear();
        q2_0_gguf_rows_multi_avx2_v(vn,weights,expected_stride,nb,acts,nt,outs,3,17);
        std::vector<Call> want;
        if(STRATA_AVXVNNI && vn) want.push_back({2,nt,0,0,3,17});
        else if(enabled && (nb==10 || nb==40) && nt%4) {
            for(int offset=0;offset<nt;offset+=4) {
                int count=nt-offset<4?nt-offset:4;
                want.push_back({count==4?0:1,count,offset,offset,3,17});
            }
        } else want.push_back({0,nt,0,0,3,17});
        if(calls.size()!=want.size()) return 4;
        for(size_t j=0;j<want.size();++j) {
            const auto &x=calls[j], &y=want[j];
            if(x.kind!=y.kind || x.nt!=y.nt || x.a_offset!=y.a_offset ||
               x.out_offset!=y.out_offset || x.r0!=y.r0 || x.r1!=y.r1) return 5;
            spread_calls+=x.kind==1; vnni_calls+=x.kind==2;plain_calls+=x.kind==0;
        }
        ++cases;
    }
    if(enabled && spread_calls==0) return 6;
    if(STRATA_AVXVNNI && vnni_calls==0) return 7;
    std::printf("PASS dispatch cases=%zu enabled=%d VNNI_build=%d plain=%zu spread=%zu vnni=%zu\n",
                cases,int(enabled),STRATA_AVXVNNI,plain_calls,spread_calls,vnni_calls);
}
"""
def build_and_run(folder, text, vnni, value, expected, tag):
    cpp = folder / (tag + ".cpp")
    exe = folder / tag
    cpp.write_text(preamble + option + text + main)
    subprocess.run([os.environ.get("CXX","g++"),"-std=c++17","-O2",
                    f"-DSTRATA_AVXVNNI={vnni}",str(cpp),"-o",str(exe)],check=True)
    env = os.environ.copy()
    env.pop("STRATA_Q2_AVX2_SPREAD",None)
    if value is not None: env["STRATA_Q2_AVX2_SPREAD"]=value
    return subprocess.run([str(exe),expected],env=env,capture_output=True,text=True)

with tempfile.TemporaryDirectory(prefix="q2-dispatch-") as tmp:
    folder=Path(tmp)
    values=[None,"","0","1","true","1x","01"," 1","1 "]
    for vnni in [0,1]:
        for i,value in enumerate(values):
            result=build_and_run(folder,dispatcher,vnni,value,"on" if value=="1" else "off",f"v{vnni}e{i}")
            if result.returncode:
                raise SystemExit(f"FAIL VNNI={vnni} value={value!r}: {result.returncode}")
            print(result.stdout,end="")
    # Show that branch-dead/guard-loss/tail-loss defects are detected, not merely parity.
    mutations={
        "dead_spread": dispatcher.replace("if (kUnpackSpread &&", "if (false &&"),
        "lost_vnni": dispatcher.replace("if (vnni_rows)", "if (false)"),
        "lost_shape_guard": dispatcher.replace("(nblocks == 10 || nblocks == 40)", "true"),
        "lost_tail_spread": dispatcher.replace("else spread::rows_nt", "else plain::rows_nt"),
    }
    for tag,text in mutations.items():
        assert text!=dispatcher
        result=build_and_run(folder,text,1,"1","on",tag)
        if result.returncode==0: raise SystemExit("FAIL mutation survived: "+tag)
        print("PASS rejected mutation:",tag,"exit",result.returncode)
print("source_sha256="+hashlib.sha256(source.encode()).hexdigest())
print("dispatcher_sha256="+hashlib.sha256(dispatcher.encode()).hexdigest())
