#!/usr/bin/env python3
"""Test-only CMake/runtime ISA admission. No GPU, model or AVX2 instructions required.

Run with Python 3, CMake and a GCC/Clang-compatible C++ compiler (CXX, default c++).
The fake feature matrix compiles the actual launcher with only its three feature
queries substituted; the real launcher is also compiled and checked separately.
"""
from pathlib import Path
import itertools
import os
import shlex
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
CMAKE = (ROOT / 'CMakeLists.txt').read_text(encoding='utf-8-sig')
SOURCE = (ROOT / 'tests/q2_avx2_test_runner.cpp').read_text()

def need(value, message):
    if not value:
        raise RuntimeError(message)

def run(args, **kwargs):
    return subprocess.run(args, text=True, capture_output=True, **kwargs)

needle = '  # These direct AVX2 tests are not part of the older-CPU (avx/none) test profile.'
pos = CMAKE.index(needle)
condition = CMAKE[:pos].splitlines()[-1]
need(condition.startswith('if(') and condition.endswith(')'), 'CMake test gate missing')
block = CMAKE[pos:CMAKE.index('\nendif()', pos)]
need('SKIP_RETURN_CODE 77' in block, 'CTest must recognize unsupported-ISA skips')
for mode in ('on', 'off'):
    need(f'$<TARGET_FILE:q2_avx2_test_runner> $<TARGET_FILE:q2_avx2_spread_test> {mode}' in block,
         'CTest must enter through separate launcher')
need('-march=x86-64 -mtune=generic -mno-avx -mno-avx2 -mno-fma -mno-f16c' in block,
     'launcher must compile for baseline ISA')

with tempfile.TemporaryDirectory(prefix='q2-test-isa-') as name:
    work = Path(name)
    gate_cases = 0
    for enabled, msvc, floor in itertools.product((False, True), (False, True), ('', 'avx', 'none')):
        script = work / 'gate.cmake'
        script.write_text(f'set(STRATA_BUILD_TESTS {"ON" if enabled else "OFF"})\n'
                          f'set(MSVC {"ON" if msvc else "OFF"})\n'
                          f'set(STRATA_ISA_FLOOR "{floor}")\n{condition}\n'
                          'message("ADMITTED")\nelse()\nmessage("EXCLUDED")\nendif()\n')
        result = run(['cmake', '-P', str(script)])
        want = 'ADMITTED' if enabled and not msvc and floor == '' else 'EXCLUDED'
        need(result.returncode == 0 and result.stderr.strip() == want, 'CMake gate truth table')
        gate_cases += 1
    flags = [*shlex.split(os.environ.get('CXX', 'c++')), '-std=c++17', '-O2',
             '-march=x86-64', '-mtune=generic', '-mno-avx', '-mno-avx2', '-mno-fma', '-mno-f16c']
    real = work / 'real-runner'
    subprocess.run([*flags, str(ROOT/'tests/q2_avx2_test_runner.cpp'), '-o', str(real)], check=True)
    prefix = '''#include <cstdlib>
#include <cstring>
static int fake_feature(const char* feature) {
    const char* value=std::getenv("Q2_TEST_FEATURE_MASK");
    const int mask=value ? std::atoi(value) : 0;
    return mask & (!std::strcmp(feature,"avx2") ? 1 : !std::strcmp(feature,"fma") ? 2 : 4);
}
'''
    fake_source = SOURCE
    for feature, old in (('avx2', '__builtin_cpu_supports("avx2")'),
                         ('fma', '__builtin_cpu_supports("fma")'),
                         ('f16c', '(ecx & (1u << 29)) != 0')):
        need(fake_source.count(old) == 1, 'exact real feature query missing')
        fake_source = fake_source.replace(old, f'fake_feature("{feature}")')
    (work/'fake.cpp').write_text(prefix + fake_source)
    fake = work / 'fake-runner'
    subprocess.run([*flags, str(work/'fake.cpp'), '-o', str(fake)], check=True)
    # Compile the actual Windows branch against a narrow host-side CRT shim.
    # This verifies waiting/status/argv handling, not Windows platform execution.
    (work/'process.h').write_text('''#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#define _P_WAIT 0
static intptr_t _spawnv(int mode, const char* path, const char* const* args) {
    if(mode!=_P_WAIT || std::strcmp(path,std::getenv("Q2_TEST_CHILD_PATH")) ||
       std::strcmp(args[0],"q2_avx2_spread_test") || args[2]) return -1;
    const int status=std::atoi(std::getenv("Q2_TEST_CHILD_EXIT"));
    if(status==-1)return -1;
    FILE* f=std::fopen(std::getenv("Q2_TEST_CHILD_MARKER"),"w");
    if(!f)return -1;
    std::fprintf(f,"%s:%s",args[1],std::getenv("STRATA_Q2_AVX2_SPREAD"));
    std::fclose(f);return status;
}
''')
    windows = work / 'windows-branch-runner'
    subprocess.run([*flags, '-D_WIN32', '-I'+str(work), str(work/'fake.cpp'), '-o', str(windows)], check=True)
    # A harmless child verifies argv, inherited environment and exact exit propagation.
    child_source = '''#include <cstdio>
#include <cstdlib>
int main(int argc,char** argv) {
    const char* name=std::getenv("Q2_TEST_CHILD_MARKER");
    FILE* f=name ? std::fopen(name,"w") : nullptr;
    if(!f || argc!=2)return 99;
    std::fprintf(f,"%s:%s",argv[1],std::getenv("STRATA_Q2_AVX2_SPREAD"));
    std::fclose(f);return std::atoi(std::getenv("Q2_TEST_CHILD_EXIT"));
}
'''
    (work/'child.cpp').write_text(child_source)
    child = work/'child with spaces'
    subprocess.run([*flags, str(work/'child.cpp'), '-o', str(child)], check=True)
    marker = work/'child-ran'
    cases = 0
    for runner, mask, exit_code, mode in itertools.product((fake, windows), range(8), (0, 9), ('on', 'off')):
        marker.unlink(missing_ok=True)
        env = dict(os.environ, Q2_TEST_FEATURE_MASK=str(mask), Q2_TEST_CHILD_EXIT=str(exit_code),
                   Q2_TEST_CHILD_MARKER=str(marker), Q2_TEST_CHILD_PATH=str(child),
                   STRATA_Q2_AVX2_SPREAD='1' if mode=='on' else '0')
        result = run([str(runner), str(child), mode], env=env)
        if mask == 7:
            need(result.returncode == exit_code and marker.read_text() == mode+':'+env['STRATA_Q2_AVX2_SPREAD'],
                 'supported path must exec exact child without shell and preserve its status')
        else:
            need(result.returncode == 77 and not marker.exists() and result.stdout.startswith('SKIP '),
                 'missing ISA must skip before starting child')
        cases += 1
    need(run([str(real)]).returncode == 2, 'usage error')
    need(run([str(real), str(child), 'invalid']).returncode == 2, 'invalid mode must fail')
    marker.unlink(missing_ok=True)
    env.update(Q2_TEST_FEATURE_MASK='7', Q2_TEST_CHILD_EXIT='-1')
    need(run([str(windows), str(child), 'on'], env=env).returncode == 1 and not marker.exists(),
         'Windows spawn failure must fail')
    cpuid_call = '__get_cpuid(1, &eax, &ebx, &ecx, &edx)'
    need(fake_source.count(cpuid_call) == 1, 'CPUID query missing')
    (work/'no-cpuid.cpp').write_text(prefix + fake_source.replace(cpuid_call, '0'))
    no_cpuid = work / 'no-cpuid-runner'
    subprocess.run([*flags, str(work/'no-cpuid.cpp'), '-o', str(no_cpuid)], check=True)
    need(run([str(no_cpuid), str(child), 'on'], env=env).returncode == 77 and not marker.exists(),
         'unavailable CPUID must skip')
    marker.unlink(missing_ok=True)
    env.update(Q2_TEST_CHILD_EXIT='0', STRATA_Q2_AVX2_SPREAD='1')
    result = run([str(real), str(child), 'on'], env=env)
    need(result.returncode in (0, 77), 'real-host feature probe')
    need(marker.exists() == (result.returncode == 0), 'real-host probe must guard exec')
    supported = result.returncode == 0
    if supported:
        need(run([str(real), str(work/'missing-child'), 'on']).returncode == 1, 'exec failure must fail')
print(f'PASS CMake conditions={gate_cases}; feature/argv/exit cases={cases}; real_host_supported={int(supported)}')
