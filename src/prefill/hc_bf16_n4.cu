#include <hip/hip_runtime.h>
#include <cstdint>
#include <cstring>
// Exact installed BF16 main schedule: split r uses k=16*r+48*j+q, q ascending.
// Preserve the observed valid-output multiplicand order, including the second tile half.
__device__ __forceinline__ float ordered_fmac(float acc,float w,float x,bool reverse){
 if(reverse)asm volatile("v_fmac_f32 %0, %1, %2" : "+v"(acc) : "v"(x),"v"(w));
 else asm volatile("v_fmac_f32 %0, %1, %2" : "+v"(acc) : "v"(w),"v"(x));
 return acc;
}
extern "C" __global__ void hc_bf16_n4_main(const uint16_t* x,const uint16_t* w,float* partial,int t){
 __shared__ float tile_x[64*16],tile_w[4*16]; // exactly 4352 bytes
 unsigned lane=threadIdx.x,o=blockIdx.x*256+lane,r=blockIdx.y;
 unsigned j=o/4,i=o%4;bool valid=o<unsigned(t)*4;float acc=__uint_as_float(0);
 for(unsigned base=16*r;base<10240;base+=48){
  // Every lane cooperates, including the inactive final four lanes at T4095.
  for(unsigned u=lane;u<64*16;u+=256){unsigned token=blockIdx.x*64+u/16;
   uint16_t v=token<unsigned(t)?x[size_t(token)*10240+base+u%16]:uint16_t(0);
   unsigned row=u/16,q=u%16;tile_x[row*16+(q^((row>>1)&15))]=__uint_as_float(uint32_t(v)<<16);
  }
  if(lane<4*16){unsigned row=lane/16,q=lane%16;tile_w[row*16+(q^((row>>1)&15))]=__uint_as_float(uint32_t(w[row*10240+base+q])<<16);}
  __syncthreads();
  if(valid){
#pragma unroll
   for(unsigned q=0;q<16;++q){unsigned row=lane/4;float a=tile_w[i*16+(q^((i>>1)&15))],b=tile_x[row*16+(q^((row>>1)&15))];
    acc=ordered_fmac(acc,a,b,(j%32>=16)&&(q>=7));
   }
  }
  __syncthreads();
 }
 if(valid)partial[size_t(r)*t*4+o]=acc;
}
extern "C" __global__ void hc_bf16_n4_post(const float* p,float* y,int t){
 unsigned o=blockIdx.x*blockDim.x+threadIdx.x;if(o<unsigned(t)*4){
  float a=__uint_as_float(0),v0=p[o],v1=p[size_t(t)*4+o],v2=p[size_t(t)*8+o],alpha=1.0f;
  asm volatile("v_add_f32 %0, %1, %2" : "=v"(a) : "v"(a),"v"(v0));
  asm volatile("v_add_f32 %0, %1, %2" : "=v"(a) : "v"(a),"v"(v1));
  asm volatile("v_add_f32 %0, %1, %2" : "=v"(a) : "v"(a),"v"(v2));
  asm volatile("v_mul_f32 %0, %1, %2" : "=v"(a) : "v"(alpha),"v"(a));
  y[o]=a;
 }
}

extern "C" int strata_hc_bf16_n4_supported(int device){
 int current=-1;if(hipGetDevice(&current)!=hipSuccess||current!=device)return -1;
 hipDeviceProp_t p{};if(hipGetDeviceProperties(&p,device)!=hipSuccess)return -1;
 return std::strncmp(p.gcnArchName,"gfx906",6)==0&&(p.gcnArchName[6]==0||p.gcnArchName[6]==':')&&p.warpSize==64?1:0;
}
extern "C" int strata_hc_bf16_n4_launch(const uint16_t* x,const uint16_t* w,float* y,float* partial,int t,void* stream){
 const hipStream_t st=static_cast<hipStream_t>(stream);const unsigned out=unsigned(t)*4;
 hipLaunchKernelGGL(hc_bf16_n4_main,dim3((out+255)/256,3),dim3(256),0,st,x,w,partial,t);
 hipError_t e=hipGetLastError();if(e!=hipSuccess)return int(e);
 hipLaunchKernelGGL(hc_bf16_n4_post,dim3((out+255)/256),dim3(256),0,st,partial,y,t);
 return int(hipGetLastError());
}
