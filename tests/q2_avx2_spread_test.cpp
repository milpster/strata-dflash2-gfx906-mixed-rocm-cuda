// Standalone CPU parity test: compiles the actual production translation unit.
// Link no other q2_avx2 object into this test.
#include "../src/kernels/cpu/q2_avx2.cpp"
#include <array>
#include <cstdio>
#include <vector>
#include <random>
#include <stdexcept>
using namespace strata::kernels::cpu;
// The production ISA detector is not linked into this standalone test.
// Direct dispatch explicitly selects plain AVX2; actual VNNI is tested only if supported.
namespace strata::kernels::cpu {
bool cpu_avxvnni_ok() {
#if STRATA_AVXVNNI
    return __builtin_cpu_supports("avxvnni");
#else
    return false;
#endif
}
}
static size_t checks = 0;
static void require(bool ok, const char* msg) {
    ++checks;
    if (!ok) throw std::runtime_error(msg);
}
static void unpack_tests() {
    alignas(32) uint8_t codes[48], old[64], fresh[64];
    // Every byte position, every packed-byte value, and unaligned addresses.
    for (int offset = 0; offset < 32; ++offset)
    for (int lane = 0; lane < 16; ++lane)
    for (int v = 0; v < 256; ++v) {
        for (int j = 0; j < 48; ++j) codes[j] = uint8_t(j * 137 + v * 17);
        codes[offset + lane] = uint8_t(v);
        __m256i a,b,c,d;
        unpack64(codes + offset,a,b); unpack64_spread(codes + offset,c,d);
        _mm256_store_si256((__m256i*)old,a); _mm256_store_si256((__m256i*)(old+32),b);
        _mm256_store_si256((__m256i*)fresh,c); _mm256_store_si256((__m256i*)(fresh+32),d);
        require(std::memcmp(old,fresh,64)==0, "unpack old/new mismatch");
        for (int i=0;i<64;++i)
            require(fresh[i] == ((codes[offset+i/4] >> (2*(i%4))) & 3), "scalar unpack mismatch");
    }
}
static void dot_tests() {
    alignas(32) uint8_t codes[32];
    alignas(32) int8_t acts[32];
    alignas(32) int32_t sums[8];
    for (int packed=0; packed<256; ++packed)
    for (int act=-128; act<128; ++act) {
        for (int i=0; i<32; ++i) {
            codes[i]=(packed >> (2*(i%4))) & 3;
            acts[i]=int8_t(act);
        }
        auto dot=plain::dot4(_mm256_load_si256((__m256i*)codes),_mm256_load_si256((__m256i*)acts));
        _mm256_store_si256((__m256i*)sums,dot);
        int expected=0; for(int i=0;i<4;++i) expected+=codes[i]*act;
        for(int i=0;i<8;++i) require(sums[i]==expected,"scalar dot mismatch");
    }
}
static void row_tests() {
    std::mt19937 rng(0x906);
    constexpr int maxnt=13;
    std::array<ActQ,maxnt> acts{};
    std::array<const ActQ*,maxnt> ap{};
    std::array<float*,maxnt> op{}, rp{}, sp{};
    const uint16_t scales[]={0,0x8000,1,0x3ff,0x400,0x1400,0x3c00,0xbc00,0x7bff};
    // Unsupported block sizes and exact supported 10/40; all tail groups.
    for (int nb : {1,2,9,10,11,39,40})
    for (int nt=1;nt<=maxnt;++nt)
    for (int rows : {1,2,3,17,640}) {
        const size_t stride=18*nb+7; // exercise padded rows / unaligned block starts
        std::vector<uint8_t> weights(stride*(rows+2)+1);
        uint8_t* w=weights.data()+1;
        for(int r=0;r<rows+2;++r) for(int b=0;b<nb;++b) {
            auto p=w+r*stride+b*18;
            uint16_t h=scales[(r+b)%9]; std::memcpy(p,&h,2);
            for(int i=0;i<16;++i) p[2+i]=uint8_t(rng());
        }
        std::vector<std::vector<float>> got(nt,std::vector<float>(rows+2,-12345.5f));
        auto ref=got, spread_only=got;
        for(int t=0;t<nt;++t) {
            ap[t]=&acts[t]; op[t]=got[t].data();rp[t]=ref[t].data();sp[t]=spread_only[t].data();
            acts[t].nchunks=nb*2;
            for(int c=0;c<nb*2;++c) {
                int sum=0;
                for(int j=0;j<32;++j) {
                    // Includes -128 stress value as well as production [-127,127].
                    int8_t q=int8_t(uint8_t(rng())); acts[t].q[c*32+j]=q;sum+=q;
                }
                acts[t].scale[c]=std::ldexp(float(1+rng()%127),-int(8+rng()%16));
                acts[t].sum[c]=sum; acts[t].hx[c]=acts[t].scale[c]*float(sum);
            }
        }
        plain::rows_nt(w,stride,nb,ap.data(),nt,rp.data(),1,rows+1);
        spread::rows_nt(w,stride,nb,ap.data(),nt,sp.data(),1,rows+1);
        q2_0_gguf_rows_multi_avx2_v(false,w,stride,nb,ap.data(),nt,op.data(),1,rows+1);
        for(int t=0;t<nt;++t) {
            require(std::memcmp(rp[t],sp[t],(rows+2)*sizeof(float))==0,"direct spread bitwise mismatch");
            require(std::memcmp(rp[t],op[t],(rows+2)*sizeof(float))==0,"dispatch bitwise mismatch");
            require(got[t][0]==-12345.5f && got[t][rows+1]==-12345.5f,"row boundary overwritten");
        }
#if STRATA_AVXVNNI
        if (cpu_avxvnni_ok()) {
            q2_0_gguf_rows_multi_avx2_v(true,w,stride,nb,ap.data(),nt,op.data(),1,rows+1);
            for(int t=0;t<nt;++t) require(std::memcmp(rp[t],op[t],(rows+2)*sizeof(float))==0,"VNNI mismatch");
        }
#endif
    }
}
int main(int argc,char**argv) {
    try {
        require(argc==2,"expected enable argument");
        require(kUnpackSpread==(std::strcmp(argv[1],"on")==0),"environment default/enable mismatch");
        unpack_tests(); dot_tests(); row_tests();
        std::printf("PASS checks=%zu spread=%d VNNI_compiled=%d VNNI_runtime=%d\n",
                    checks,int(kUnpackSpread),STRATA_AVXVNNI,int(cpu_avxvnni_ok()));
    } catch(const std::exception& e) {std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}
}
