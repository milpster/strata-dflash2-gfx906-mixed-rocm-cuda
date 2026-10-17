#!/usr/bin/env python3
"""Compile actual host-side device guards with a fake CUDA/HIP property query."""
from pathlib import Path
import os
import subprocess
import tempfile
ROOT = Path(__file__).resolve().parents[1]
PRE = r'''
#include <cstring>
#include <cstdlib>
#include <cstdio>
struct cudaDeviceProp { int warpSize; char gcnArchName[64]; };
constexpr int cudaSuccess=0;
int device=0, fail_device=0, fail_props=0;
cudaDeviceProp prop{};
int cudaGetDevice(int* p) {*p=device; return fail_device;}
int cudaGetLastError() {return 0;}
int cudaGetDeviceProperties(cudaDeviceProp* p,int) {*p=prop;return fail_props;}
'''
MAIN = r'''
int main() {
  const char* names[]={"gfx906","gfx906:sramecc+:xnack-","gfx9060","gfx906x","gfx90a","gfx1030","","gfx9"};
  for(int i=0;i<8;++i) for(int wave:{32,64}) {
    ++device; std::strcpy(prop.gcnArchName,names[i]);prop.warpSize=wave;
    bool want=i<2 && wave==64;
    if(GUARD()!=want) return 1;
  }
  ++device;std::strcpy(prop.gcnArchName,"gfx906");prop.warpSize=64;
  fail_props=1;if(GUARD())return 2;
  fail_props=0;if(!GUARD())return 3;
  fail_device=1;if(GUARD())return 4;
  fail_device=0; device=-1;if(GUARD())return 5;
  device=64;if(GUARD())return 6;
  puts("PASS exact gfx906 wave64 device guard, failures and switching");
}
'''
with tempfile.TemporaryDirectory() as tmp:
    for file,name in [('iq_kernels.cu','q2_modes_current_device_is_gfx906'),('qsa_select.cu','topk_current_device_is_gfx906')]:
        source=(ROOT/'src/kernels/cuda'/file).read_text()
        start=source.index('bool '+name+'() {')
        end=source.index('\n}',start)+2
        cpp=Path(tmp)/'guard.cpp';exe=Path(tmp)/'guard'
        cpp.write_text('#include <initializer_list>\n'+PRE+source[start:end]+MAIN.replace('GUARD',name))
        subprocess.run([os.environ.get('CXX','g++'),'-std=c++17','-O2',str(cpp),'-o',str(exe)],check=True)
        subprocess.run([str(exe)],check=True)
# Exhaustive packed-byte signed spreading identity, including high-byte contamination.
for q in range(65536):
    c=(q | (q<<12)) & 0x000f000f
    c=(c | (c<<6)) & 0x03030303
    got=((c+0x7f7f7f7f)^0x80808080)&0xffffffff
    want=sum(((((q>>(2*j))&3)-1)&255)<<(8*j) for j in range(4))
    assert got==want
print('PASS 65536 Q2 signed byte-spread inputs')
