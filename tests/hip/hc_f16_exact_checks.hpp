#pragma once
// Pure host checks shared by the real GPU fixture and its CPU regression.
#include <cstddef>
#include <cstdint>
#include <stdexcept>
#include <string>
#include <vector>
namespace hc_test {
inline void require(bool value, const char* what) { if (!value) throw std::runtime_error(what); }
struct Counts { uint64_t calls, takes, guard, other; };
inline void delta(Counts before, Counts after, Counts wanted) {
    require(after.calls >= before.calls && after.takes >= before.takes &&
            after.guard >= before.guard && after.other >= before.other, "counter decreased");
    require(after.calls-before.calls == wanted.calls && after.takes-before.takes == wanted.takes &&
            after.guard-before.guard == wanted.guard && after.other-before.other == wanted.other,
            "wrong actual Gemm HC route/counter delta");
}
struct Comparison { size_t words=0, different=0, nonfinite=0; };
inline Comparison compare(const uint32_t* a, const uint32_t* b, size_t t, size_t n, size_t ld,
                          uint32_t poison_a, uint32_t poison_b) {
    require(t && n && ld >= n, "invalid comparison geometry"); Comparison r;
    for (size_t row=0; row<t; ++row) {
        for (size_t col=0; col<n; ++col) {
            auto x=a[row*ld+col], y=b[row*ld+col]; ++r.words;
            r.different += x != y;
            r.nonfinite += (x&0x7f800000u)==0x7f800000u || (y&0x7f800000u)==0x7f800000u;
        }
        for (size_t col=n; col<ld; ++col)
            require(a[row*ld+col]==poison_a && b[row*ld+col]==poison_b, "output row padding changed");
    }
    return r;
}
inline uint32_t random(uint32_t& s) { s^=s<<13; s^=s>>17; s^=s<<5; return s; }
enum class Fixture { dense, cancellation, signed_zero, w_inf, x_nan, w_range, x_range, w_loss, x_loss, x_f16_bits };
inline const char* name(Fixture f) {
    switch(f) {
    case Fixture::dense:return "dense"; case Fixture::cancellation:return "cancellation";
    case Fixture::signed_zero:return "signed_zero"; case Fixture::w_inf:return "weight_nonfinite";
    case Fixture::x_nan:return "input_nonfinite"; case Fixture::w_range:return "weight_range";
    case Fixture::x_range:return "input_range"; case Fixture::w_loss:return "weight_roundtrip";
    case Fixture::x_loss:return "input_roundtrip"; case Fixture::x_f16_bits:return "x_f16_bits";
    } throw std::runtime_error("unknown fixture");
}
inline bool is_guard(Fixture f) { return f>=Fixture::w_inf && f<=Fixture::x_loss; }
inline bool allows_nonfinite(Fixture f) { return f==Fixture::w_inf || f==Fixture::x_nan; }
inline void fill(uint16_t* x, size_t t, size_t k, uint16_t* w, size_t n, Fixture f) {
    uint32_t seed=0x4d5f3906u;
    for(size_t i=0;i<t*k;++i) { auto v=random(seed); x[i]=uint16_t(((v>>31)<<15)|((120+v%8)<<7)|((v>>8)&127)); }
    for(size_t i=0;i<n*k;++i) { auto v=random(seed); w[i]=uint16_t(((v>>31)<<15)|((119+v%7)<<7)|((v>>8)&127)); }
    if(f==Fixture::cancellation) {
        for(size_t i=0;i<t*k;++i)x[i]=0;
        for(size_t row=0;row<t;++row) { x[row*k]=0x3f00; x[row*k+1]=uint16_t((127-25)<<7); x[row*k+2]=0xbf00; }
        for(size_t i=0;i<n*k;++i)w[i]=0x3d80; // 1/16
    } else if(f==Fixture::signed_zero) {
        for(size_t i=0;i<t*k;++i)x[i]=(i&1)?0x8000:0;
        for(size_t i=0;i<n*k;++i)w[i]=(i&1)?0x8000:0;
    } else if(f==Fixture::w_inf) w[n*k-1]=0x7f80;
    else if(f==Fixture::x_nan) x[t*k-1]=0x7fc1;
    else if(f==Fixture::w_range) w[n*k-1]=0x4180; // 16 * 4096 > max half
    else if(f==Fixture::x_range) x[t*k-1]=0x4300; // 128 * 512 > max half
    else if(f==Fixture::w_loss) w[n*k-1]=uint16_t((127-40)<<7);
    else if(f==Fixture::x_loss) x[t*k-1]=uint16_t((127-40)<<7);
    else if(f==Fixture::x_f16_bits) for(size_t i=0;i<t*k;++i)x[i]=0x3000; // FP16 1/8; only exclusion is claimed
}
inline void check_guard(Fixture f, const unsigned g[10]) {
    bool nonfinite=g[1]||g[6], range=g[2]||g[7], loss=g[3]||g[8];
    if(!is_guard(f)) { require(!nonfinite&&!range&&!loss,"admissible fixture conversion guard failed"); return; }
    require(nonfinite||range||loss,"missing conversion rejection");
    if(f==Fixture::w_inf)require(g[1]&&g[2]&&!g[6]&&!g[7],"weight nonfinite guard category");
    if(f==Fixture::x_nan)require(g[6]&&g[7]&&!g[1]&&!g[2],"input nonfinite guard category");
    if(f==Fixture::w_range)require(g[2]&&!nonfinite&&!g[7],"weight range guard category");
    if(f==Fixture::x_range)require(g[7]&&!nonfinite&&!g[2],"input range guard category");
    if(f==Fixture::w_loss)require(g[3]&&!nonfinite&&!range&&!g[8],"weight roundtrip guard category");
    if(f==Fixture::x_loss)require(g[8]&&!nonfinite&&!range&&!g[3],"input roundtrip guard category");
}
} // namespace hc_test
