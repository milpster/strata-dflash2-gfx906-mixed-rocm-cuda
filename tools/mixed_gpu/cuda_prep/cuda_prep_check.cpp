// tools/mixed_gpu/cuda_prep/cuda_prep_check.cpp - the parity driver: COMPILE-ONLY this phase.
//
// This program is built in the CPU configure (it links only libdl/libstdc++) so Gate2 can review real
// compiled code, and it is deliberately NOT run in Phase 2: it dlopens the CUDA/HIP libraries, and every
// library load is a runtime action the current authorization forbids - not even --help.  It refuses to do
// anything at all unless the environment carries STRATA_PREP_RUNTIME_GATE=1, which a human sets only in an
// explicitly authorized primary-managed runtime window.  The refusal happens before any dlopen, any dlsym and
// any file read, and returns 77.
//
// Modes (R1, design gate C10 - the coexistence hazard):
//   * SINGLE-LIBRARY MODE (the mode the real-blob window must use): exactly one library argument plus
//     STRATA_PREP_LIB=cuda or STRATA_PREP_LIB=hip.  The named backend is verified against the library's own
//     get_info backend, and exactly ONE dlopen happens: a CUDA-only window never loads libamdhip64 into the
//     process, and a HIP-only window never loads libcudart.  Loading CUDA and HIP into one process has a
//     proven coexistence crash (build-mixed-gpu/cuda-prep/evidence/s0-hip-*.log), so the driver refuses to do
//     it implicitly.
//   * DUAL MODE (historical, kept for the ABI-isolation evidence only): two library arguments.  It prints a
//     loud warning naming the known coexistence crash and is kept so the Phase-3 evidence stays reproducible.
//     The expert helper is CUDA-only; nothing in R1 needs this mode.
//
// What it does in the window: dlopen(RTLD_LOCAL) the library/libraries (local symbols: neither library's
// internals are visible to the other, and neither interposes on the other), dlsym the v1 entry points, read a
// fixture manifest + blobs, call strata_prep_v1_expert per fixture, compare the raw per-expert vectors against
// the recorded CPU oracle (the ggml-cpu decode the exporter recorded), call strata_prep_v1_combine and compare
// against the recorded exactly-once oracle.
//
// Tolerance is NOT invented here: the parity tool's own thresholds are relative L1 3e-2 for the quantized
// paths against the float reference (native_expert_parity.cpp:10-13, 476), and it documents that (b) and (c)
// differ from (a) by their activation rounding.  Bitwise CPU==GPU equality is NOT claimed.  Combine is
// rel-L1 < 1e-5 (the same arithmetic on the same inputs).
//
// TRUTHFUL REPORTING (R1): every result line carries the fixture's synthetic flag; the summary counts the
// REAL fixtures (synthetic=0, the model's own bytes) and the SYNTHETIC fixtures (ggml-quantized random
// weights) separately and says which kind of evidence each one is.  A synthetic fixture never stands for a
// real-blob parity, and the summary never calls a real fixture synthetic.
//
// KEEPER FIX (R1 gate, r1-gate.md §4): --all enumerates the fixture list before the mode is decided and
// before any dlopen; an absent/not-a-directory/empty/manifest-less/unreadable fixtures dir refuses with
// exit 2 and loads nothing, so a zero-fixture run can never exit 0 (a PASS over nothing) and a directory
// enumeration never happens after the GPU library was opened.
#include "abi.hpp"
#include "manifest.hpp"
#include "sha256.hpp"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <dlfcn.h>
#include <filesystem>
#include <fstream>
#include <string>
#include <vector>

namespace prep = strata::prep;
namespace fs = std::filesystem;

namespace {

typedef uint32_t (*abi_version_fn)();
typedef int (*get_info_fn)(StrataPrepV1Info*);
typedef int (*expert_fn)(const StrataPrepV1Expert*);
typedef int (*combine_fn)(const StrataPrepV1Combine*);
typedef const char* (*strerror_fn)(int);

struct Lib {
    void* h = nullptr;
    abi_version_fn abi_version = nullptr;
    get_info_fn get_info = nullptr;
    expert_fn expert = nullptr;
    combine_fn combine = nullptr;
    strerror_fn strerror_ = nullptr;
    int backend = -1;
    std::string path;
};

// The ten native pair classes (the production pack's gu x d set) and their synthetic fixture labels, so a
// window can name the whole real family without typing twenty labels.
const char* const kRealPairLabels[] = {"iq2_xxs_iq4_nl", "iq2_xxs_q2_0", "iq2_xs_iq4_nl", "iq2_xs_q2_0",
                                       "iq3_xxs_iq4_nl", "iq3_xxs_q2_0", "iq3_s_iq4_nl",  "iq3_s_q2_0",
                                       "iq2_s_iq4_nl",   "iq2_s_q2_0"};
const char* const kLegacyLabels[] = {"q4_0_q4_0", "q4_0_q4_1", "q4_K_q5_1", "q4_K_q8_0", "q5_0_q8_0",
                                     "q5_K_q8_0", "q8_0_q8_0", "q4_K_q5_0", "q6_K_q8_0"};

std::vector<uint8_t> read_bin(const std::string& path, size_t expect_bytes) {
    std::ifstream in(path, std::ios::binary);
    if (!in) return {};
    in.seekg(0, std::ios::end);
    const size_t n = (size_t) in.tellg();
    if (n != expect_bytes) return {};  // an exact byte-count check, the same contract the ABI guards enforce
    in.seekg(0, std::ios::beg);
    std::vector<uint8_t> b(n);
    in.read((char*) b.data(), (std::streamsize) n);
    return b;
}

double rel_l1(const std::vector<float>& a, const std::vector<float>& b) {
    double n = 0, d = 0;
    for (size_t i = 0; i < a.size(); ++i) {
        n += std::fabs((double) a[i] - (double) b[i]);
        d += std::fabs((double) b[i]);
    }
    return n / (d + 1e-30);
}

bool load_lib(Lib& lib, const std::string& path, std::string& err) {
    lib.h = dlopen(path.c_str(), RTLD_NOW | RTLD_LOCAL);  // LOCAL: the library's internals stay private
    if (lib.h == nullptr) {
        err = std::string("dlopen ") + path + ": " + dlerror();
        return false;
    }
    lib.path = path;
    lib.abi_version = (abi_version_fn) dlsym(lib.h, "strata_prep_v1_abi_version");
    lib.get_info = (get_info_fn) dlsym(lib.h, "strata_prep_v1_get_info");
    lib.expert = (expert_fn) dlsym(lib.h, "strata_prep_v1_expert");
    lib.combine = (combine_fn) dlsym(lib.h, "strata_prep_v1_combine");
    lib.strerror_ = (strerror_fn) dlsym(lib.h, "strata_prep_v1_strerror");
    if (!lib.abi_version || !lib.get_info || !lib.expert || !lib.combine || !lib.strerror_) {
        err = "dlsym: " + path + " does not export the full strata_prep_v1_* surface";
        return false;
    }
    if (lib.abi_version() != STRATA_PREP_V1_ABI_VERSION) {
        err = "abi version mismatch: " + path;
        return false;
    }
    StrataPrepV1Info info{};
    info.abi_version = STRATA_PREP_V1_ABI_VERSION;
    info.struct_size = sizeof(StrataPrepV1Info);
    if (lib.get_info(&info) != STRATA_PREP_V1_OK) {
        err = "get_info refused: " + path;
        return false;
    }
    lib.backend = info.backend;
    return true;
}

int run_fixture(const prep::Manifest& m, const std::string& base, const std::vector<Lib*>& libs, std::string& err) {
    const auto blob = read_bin(base + "/blob.bin", m.blob_bytes);
    const auto x = read_bin(base + "/x.bin", (size_t) m.n_embd * sizeof(float));
    const auto cpu_out = read_bin(base + "/cpu_out.bin", (size_t) m.n_embd * sizeof(float));
    const auto parts = read_bin(base + "/combine_parts.bin", (size_t) m.k * m.n_embd * sizeof(float));
    const auto weights = read_bin(base + "/combine_weights.bin", (size_t) m.k * sizeof(float));
    const auto shared = read_bin(base + "/combine_shared.bin", (size_t) m.n_embd * sizeof(float));
    const auto combine_oracle = read_bin(base + "/combine_oracle.bin", (size_t) m.n_embd * sizeof(float));
    if (blob.empty() || x.empty() || cpu_out.empty() || parts.empty() || weights.empty() || shared.empty() ||
        combine_oracle.empty()) {
        err = "fixture files missing or byte counts wrong";
        return 1;
    }
    const float* xf = (const float*) x.data();
    const float* cpu_out_f = (const float*) cpu_out.data();
    const int n_embd = (int) m.n_embd;

    int failures = 0;
    for (Lib* lib : libs) {
        std::vector<float> out((size_t) m.n_embd, 0.f);
        StrataPrepV1Expert req{};
        req.abi_version = STRATA_PREP_V1_ABI_VERSION;
        req.struct_size = sizeof(StrataPrepV1Expert);
        req.gu_type = m.gu_type;
        req.d_type = m.d_type;
        req.n_embd = m.n_embd;
        req.n_ff = m.n_ff;
        req.nt = 1;
        req.blob_bytes = m.blob_bytes;
        req.gu_row = m.gu_row;
        req.d_row = m.d_row;
        req.up_off = m.up_off;
        req.down_off = m.down_off;
        req.tail_off = 0;
        req.blob = blob.data();
        req.x = xf;
        req.out = out.data();
        const int rc = lib->expert(&req);
        if (rc != STRATA_PREP_V1_OK) {
            std::printf("  %-32s expert rc=%d %s\n", m.label.c_str(), rc, lib->strerror_(rc));
            ++failures;
            continue;
        }
        const double e = rel_l1(out, std::vector<float>(cpu_out_f, cpu_out_f + m.n_embd));
        // The parity tool's documented threshold for a quantized path against its reference (3e-2 rel L1).
        const bool ok = e < 3e-2 && std::isfinite(e);
        std::printf("  %-32s backend %d raw-vs-cpu-oracle rel-L1 %.2e %s  SYNTHETIC=%d gu=%d d=%d\n", m.label.c_str(),
                    lib->backend, e, ok ? "ok" : "FAIL", m.synthetic, m.gu_type, m.d_type);
        if (!ok) ++failures;

        std::vector<float> cout((size_t) m.n_embd, 0.f);
        StrataPrepV1Combine c{};
        c.abi_version = STRATA_PREP_V1_ABI_VERSION;
        c.struct_size = sizeof(StrataPrepV1Combine);
        c.n_embd = m.n_embd;
        c.k = (int) m.k;
        c.has_shared = 1;
        c.parts = (const float*) parts.data();
        c.weights = (const float*) weights.data();
        c.shared = (const float*) shared.data();
        c.out = cout.data();
        const int crc = lib->combine(&c);
        if (crc != STRATA_PREP_V1_OK) {
            std::printf("  %-32s combine rc=%d %s\n", m.label.c_str(), crc, lib->strerror_(crc));
            ++failures;
            continue;
        }
        const double ce = rel_l1(cout, std::vector<float>((const float*) combine_oracle.data(),
                                                          (const float*) combine_oracle.data() + m.n_embd));
        const bool cok = ce < 1e-5 && std::isfinite(ce);  // the combine is exact arithmetic on the same inputs
        std::printf("  %-32s backend %d combine-vs-oracle rel-L1 %.2e %s  SYNTHETIC=%d\n", m.label.c_str(),
                    lib->backend, ce, cok ? "ok" : "FAIL", m.synthetic);
        if (!cok) ++failures;
    }
    return failures;
}

void print_usage() {
    std::printf(
        "usage (SINGLE-LIBRARY mode, the mode the real-blob window uses):\n"
        "  STRATA_PREP_LIB=cuda cuda_prep_check <fixtures-dir> <strata_prep_cuda.so> [--all | label ...]\n"
        "  STRATA_PREP_LIB=hip  cuda_prep_check <fixtures-dir> <strata_prep_hip.so>  [--all | label ...]\n"
        "    One dlopen only.  STRATA_PREP_LIB must name the library's own backend, else E_BACKEND refusal.\n"
        "    --all runs every fixture dir under <fixtures-dir> that has a manifest.txt; an absent, empty or\n"
        "    manifest-less <fixtures-dir> is refused with exit 2 BEFORE any library is opened (0 fixtures is\n"
        "    never a PASS).\n"
        "    --classes prints the ten native pair classes' synthetic labels and exits.\n"
        "usage (DUAL mode, historical ABI-isolation evidence; known CUDA+HIP coexistence crash, warns):\n"
        "  cuda_prep_check <fixtures-dir> <strata_prep_cuda.so> <strata_prep_hip.so> [--all | label ...]\n"
        "Needs STRATA_PREP_RUNTIME_GATE=1 (set by a human in an authorized runtime window); without it this\n"
        "program loads nothing, touches nothing and exits 77.\n");
}

}  // namespace

int main(int argc, char** argv) {
    if (std::getenv("STRATA_PREP_RUNTIME_GATE") == nullptr ||
        std::strcmp(std::getenv("STRATA_PREP_RUNTIME_GATE"), "1") != 0) {
        std::fprintf(stderr,
                     "cuda_prep_check: REFUSED.  This driver dlopens the CUDA/HIP libraries and loads them into\n"
                     "this process.  Phase 2 authorization is compile-only: set STRATA_PREP_RUNTIME_GATE=1 only\n"
                     "in an explicitly authorized primary-managed runtime window.  Nothing was loaded, no GPU code ran.\n");
        return 77;
    }
    // The fixture hashes this driver compares are only meaningful if the hash is FIPS-correct: the published
    // vectors run before any file is read (exit 3 = selftest failed; nothing was read, nothing was loaded).
    {
        std::string srep;
        if (!prep::sha256_selftest(srep)) {
            std::fprintf(stderr, "cuda_prep_check: sha256 selftest FAILED, refusing to check any fixture:\n%s",
                         srep.c_str());
            return 3;
        }
        std::printf("sha256 selftest: PASS (15 published vectors, whole-buffer and streamed)\n");
    }
    std::vector<std::string> args;
    for (int i = 1; i < argc; ++i) args.emplace_back(argv[i]);
    const bool list_classes = std::find(args.begin(), args.end(), "--classes") != args.end();
    if (list_classes) {
        std::printf("the ten native pair classes (gu x d ggml ids) and their synthetic fixture labels:\n");
        const int cls[][2] = {{16, 20}, {16, 42}, {17, 20}, {17, 42}, {18, 20},
                              {18, 42}, {21, 20}, {21, 42}, {22, 20}, {22, 42}};
        for (size_t i = 0; i < sizeof(cls) / sizeof(cls[0]); ++i)
            std::printf("  gu=%2d d=%2d  label=%s\n", cls[i][0], cls[i][1], kRealPairLabels[i]);
        return 0;
    }
    const bool all = std::find(args.begin(), args.end(), "--all") != args.end();
    std::vector<std::string> pos;
    for (const std::string& a : args)
        if (a != "--all") pos.push_back(a);
    if (pos.size() < 2) {
        print_usage();
        return 2;
    }
    const std::string fixtures = pos[0];

    // --all: the fixture list is resolved BEFORE the mode is decided and before any library is opened.
    // The R1 keeper fix: an empty, absent, unreadable or manifest-less directory used to enumerate zero
    // labels, the loop never ran and the driver exited 0 = PASS over nothing - after the single-library
    // mode had already dlopened the GPU library.  A zero-fixture --all enumeration is a usage refusal
    // (exit 2) that loads nothing; a real enumeration failure (a permission error mid-iteration) is
    // reported the same way and never falls through to a PASS.
    std::vector<std::string> all_labels;
    if (all) {
        std::error_code ec;
        if (!fs::is_directory(fixtures, ec)) {
            std::fprintf(stderr,
                         "cuda_prep_check: --all refused, %s is not a directory (%s).  Nothing was loaded, "
                         "no fixture was run.\n",
                         fixtures.c_str(), ec ? ec.message().c_str() : "not a directory");
            return 2;
        }
        bool enum_ok = true;
        try {
            for (const auto& de : fs::directory_iterator(fixtures))
                if (de.is_directory() && fs::exists(de.path() / "manifest.txt", ec))
                    all_labels.push_back(de.path().filename().string());
        } catch (const fs::filesystem_error& e) {
            enum_ok = false;
            std::fprintf(stderr, "cuda_prep_check: --all enumeration failed under %s: %s\n", fixtures.c_str(),
                         e.what());
        }
        std::sort(all_labels.begin(), all_labels.end());
        if (!enum_ok || all_labels.empty()) {
            std::fprintf(stderr,
                         "cuda_prep_check: --all refused, %s %s.  A run over zero fixtures is not a PASS.  "
                         "Nothing was loaded, no fixture was run.\n",
                         fixtures.c_str(),
                         enum_ok ? "holds no fixture directory with a manifest.txt" : "could not be enumerated");
            return 2;
        }
    }

    // The mode, decided from the library-argument count plus STRATA_PREP_LIB.  No implicit co-loading.
    // A library argument is the positional argument naming a shared object; labels never name one.
    const auto is_lib = [](const std::string& s) { return s.find(".so") != std::string::npos; };
    if (!is_lib(pos[1])) {
        print_usage();
        return 2;
    }
    size_t nlibs = 1;
    if (pos.size() >= 3 && is_lib(pos[2])) nlibs = 2;
    const char* only_env = std::getenv("STRATA_PREP_LIB");
    std::string only = only_env ? only_env : "";
    std::transform(only.begin(), only.end(), only.begin(), [](unsigned char c) { return (char) std::tolower(c); });

    std::vector<Lib> libs;
    std::string err;
    if (nlibs == 1) {
        if (only != "cuda" && only != "hip") {
            std::fprintf(stderr,
                         "cuda_prep_check: single-library mode needs STRATA_PREP_LIB=cuda or STRATA_PREP_LIB=hip\n"
                         "to name the one library's backend (the real-blob window uses STRATA_PREP_LIB=cuda, so\n"
                         "libamdhip64 is never loaded).  Nothing was loaded.\n");
            return 2;
        }
        Lib l;
        if (!load_lib(l, pos[1], err)) {
            std::fprintf(stderr, "cuda_prep_check: %s\n", err.c_str());
            return 1;
        }
        const int want = only == "cuda" ? 1 : 2;
        if (l.backend != want) {
            std::fprintf(stderr,
                         "cuda_prep_check: backend mismatch (E_BACKEND): STRATA_PREP_LIB=%s asks for backend %d,\n"
                         "%s reports backend %d.  The library was not used; nothing was run.\n", only.c_str(), want,
                         l.path.c_str(), l.backend);
            return 1;
        }
        std::printf("cuda_prep_check: SINGLE-LIBRARY mode, STRATA_PREP_LIB=%s, backend %d, one dlopen (%s).\n",
                    only.c_str(), l.backend, l.path.c_str());
        libs.push_back(l);
    } else if (nlibs == 2) {
        std::fprintf(stderr,
                     "cuda_prep_check: WARNING - DUAL mode loads CUDA and HIP libraries into ONE process.  That\n"
                     "exact configuration has a proven coexistence crash in this project's evidence\n"
                     "(build-mixed-gpu/cuda-prep/evidence/s0-hip-calltrace.log, s0-hip-malloc-err.log); the CUDA\n"
                     "expert helper never uses this mode.  Kept only to reproduce the Phase-3 ABI-isolation\n"
                     "evidence.  The real-blob window must use single-library mode (STRATA_PREP_LIB=cuda).\n");
        Lib c, h;
        if (!load_lib(c, pos[1], err) || !load_lib(h, pos[2], err)) {
            std::fprintf(stderr, "cuda_prep_check: %s\n", err.c_str());
            return 1;
        }
        if (c.backend != 1 || h.backend != 2) {
            std::fprintf(stderr,
                         "cuda_prep_check: dual mode wants a CUDA library (backend 1) first and a HIP library\n"
                         "(backend 2) second; got backend %d (%s) and backend %d (%s).\n", c.backend, c.path.c_str(),
                         h.backend, h.path.c_str());
            return 1;
        }
        std::printf("cuda_prep_check: DUAL mode (historical), backends %d and %d.\n", c.backend, h.backend);
        libs.push_back(c);
        libs.push_back(h);
    }

    std::vector<Lib*> ptrs;
    for (auto& l : libs) ptrs.push_back(&l);

    // The fixture label list: --all (enumerated and refused above, before any library was opened),
    // explicit labels, or the nine historical synthetic pairs.
    std::vector<std::string> labels;
    if (all) {
        labels = all_labels;
        std::printf("cuda_prep_check: --all enumerated %zu fixture(s) under %s\n", labels.size(), fixtures.c_str());
    } else if (pos.size() > 1 + nlibs) {
        labels = std::vector<std::string>(pos.begin() + 1 + nlibs, pos.end());
    } else {
        labels = std::vector<std::string>(kLegacyLabels, kLegacyLabels + 9);
    }

    int failures = 0, n_real = 0, n_synthetic = 0;
    for (const std::string& label : labels) {
        prep::Manifest m;
        std::string merr;
        if (!prep::manifest_parse(fixtures + "/" + label + "/manifest.txt", m, merr)) {
            std::fprintf(stderr, "  %s: manifest refused: %s\n", label.c_str(), merr.c_str());
            ++failures;
            continue;
        }
        if (m.synthetic == 0)
            ++n_real;
        else
            ++n_synthetic;
        failures += run_fixture(m, fixtures + "/" + label, ptrs, err);
        if (!err.empty()) {
            std::fprintf(stderr, "  %s: %s\n", label.c_str(), err.c_str());
            err.clear();
        }
    }
    std::printf("cuda_prep_check: %d failure(s) over %zu fixture(s): %d REAL (synthetic=0, the model's own bytes "
                "sliced from a real GGUF) and %d SYNTHETIC (ggml-quantized random weights).\n",
                failures, labels.size(), n_real, n_synthetic);
    if (n_real)
        std::printf("cuda_prep_check: the REAL fixtures above ARE real-blob parity evidence for the pair classes "
                    "they cover; their lines print SYNTHETIC=0.\n");
    if (n_synthetic)
        std::printf("cuda_prep_check: the SYNTHETIC fixtures above prove the pipeline on generated weights and "
                    "prove NOTHING about real model bytes.\n");
    return failures ? 1 : 0;
}
