// Pure host tests of the GPU fixture's comparison/counter/fixture checks.
#include "hc_f16_exact_checks.hpp"
#include <algorithm>
#include <cstdio>
#include <functional>
using namespace hc_test;
static unsigned checks=0;
static void ok(bool v){++checks;require(v,"host helper regression");}
static void refuses(const std::function<void()>& f){bool rejected=false;try{f();}catch(const std::runtime_error&){rejected=true;}ok(rejected);}
int main() {
    try {
        constexpr uint32_t pa=0x7fc000a1u,pb=0x7fc000b2u;
        std::vector<uint32_t>a={0,0x80000000,pa,0x3f800000,0xbf800000,pa},b={0,0x80000000,pb,0x3f800000,0xbf800000,pb};
        auto c=compare(a.data(),b.data(),2,2,3,pa,pb);ok(c.words==4&&c.different==0&&c.nonfinite==0);
        for(size_t i:{0u,1u,3u,4u}) {b[i]^=1;c=compare(a.data(),b.data(),2,2,3,pa,pb);ok(c.different==1);b[i]^=1;}
        b[1]=0;c=compare(a.data(),b.data(),2,2,3,pa,pb);ok(c.different==1);b[1]=0x80000000;
        a[0]=b[0]=0x7fc00000;c=compare(a.data(),b.data(),2,2,3,pa,pb);ok(c.nonfinite==1&&c.different==0);a[0]=b[0]=0;
        for(size_t i:{2u,5u}) {b[i]^=1;refuses([&]{compare(a.data(),b.data(),2,2,3,pa,pb);});b[i]^=1;}
        refuses([&]{compare(a.data(),b.data(),2,3,2,pa,pb);});refuses([&]{compare(a.data(),b.data(),0,2,3,pa,pb);});
        Counts before{10,4,3,3};delta(before,{11,5,3,3},{1,1,0,0});delta(before,{11,4,4,3},{1,0,1,0});delta(before,{11,4,3,4},{1,0,0,1});delta(before,before,{});checks+=4;
        for(auto bad:{Counts{11,4,3,3},Counts{11,6,3,3},Counts{11,5,4,3},Counts{11,5,3,4},Counts{9,5,3,3}})refuses([&]{delta(before,bad,{1,1,0,0});});
        std::vector<uint16_t>x(64),w(96);fill(x.data(),2,32,w.data(),3,Fixture::signed_zero);ok(x.front()==0&&x.back()==0x8000&&w.front()==0&&w.back()==0x8000);
        fill(x.data(),2,32,w.data(),3,Fixture::cancellation);ok(x[0]==0x3f00&&x[1]==uint16_t((127-25)<<7)&&x[2]==0xbf00&&x[32]==0x3f00&&w.back()==0x3d80);
        fill(x.data(),2,32,w.data(),3,Fixture::dense);auto oldx=x,oldw=w;fill(x.data(),2,32,w.data(),3,Fixture::dense);ok(x==oldx&&w==oldw);
        unsigned zero[10]{};check_guard(Fixture::dense,zero);++checks;
        for(auto f:{Fixture::w_inf,Fixture::x_nan,Fixture::w_range,Fixture::x_range,Fixture::w_loss,Fixture::x_loss}) {
            fill(x.data(),2,32,w.data(),3,f);unsigned g[10]{};
            switch(f) {
            case Fixture::w_inf:ok(w.back()==0x7f80&&x==oldx);g[1]=g[2]=1;break;
            case Fixture::x_nan:ok(x.back()==0x7fc1&&w==oldw);g[6]=g[7]=g[8]=1;break;
            case Fixture::w_range:ok(w.back()==0x4180&&x==oldx);g[2]=g[3]=1;break;
            case Fixture::x_range:ok(x.back()==0x4300&&w==oldw);g[7]=g[8]=1;break;
            case Fixture::w_loss:ok(w.back()==uint16_t((127-40)<<7)&&x==oldx);g[3]=1;break;
            case Fixture::x_loss:ok(x.back()==uint16_t((127-40)<<7)&&w==oldw);g[8]=1;break;
            default:throw std::runtime_error("fixture");
            }
            check_guard(f,g);++checks;refuses([&]{check_guard(f,zero);});refuses([&]{check_guard(Fixture::dense,g);});
        }
        fill(x.data(),2,32,w.data(),3,Fixture::x_f16_bits);ok(std::all_of(x.begin(),x.end(),[](uint16_t v){return v==0x3000;}));
        std::printf("PASS HC_F16 fixture helper checks=%u\n",checks);return 0;
    } catch(const std::exception& e){std::fprintf(stderr,"FAIL helper: %s\n",e.what());return 1;}
}
