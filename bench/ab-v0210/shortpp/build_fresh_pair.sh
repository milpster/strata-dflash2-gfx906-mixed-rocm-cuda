#!/usr/bin/env bash
# Fallback build lane (the subagent host connection keeps dying): two fresh HIP engines for
# A/B3 - one from f4e0bc3d (V5's build commit, via the existing worktree), one from HEAD.
# Memory-gentle (background load on this box): nice -j2, drops to -j1 below 3 GiB MemAvailable.
# Configuration is replicated programmatically from build-v0210/rpcw-hip/CMakeCache.txt
# (STRATA_* + HIP arch/compiler + build type); nothing under build-v0210 is touched.
set -u
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)"
WT=$ROOT-wt-f4e0bc3d
REF=$ROOT/build-v0210/rpcw-hip/CMakeCache.txt
log(){ echo "=== $* $(date +%T) ==="; }
sha16(){ sha256sum "$1" 2>/dev/null | awk '{print substr($1,1,16)}'; }
jfor(){ local m; m=$(free -m | awk '/^Mem:/{print $7}'); if [ "${m:-0}" -lt 3000 ]; then echo 1; else echo 2; fi; }

ds_from_ref(){   # print -D flags for the vars worth replicating
  awk -F: '/^(STRATA_[A-Za-z0-9_]+|CMAKE_HIP_ARCHITECTURES|CMAKE_HIP_COMPILER):/{
      line=$0; var=$1; rest=substr(line, index(line,":")+1); type=substr(rest,1,index(rest,"=")-1); val=substr(rest, index(rest,"=")+1);
      if (type=="INTERNAL" || type=="STATIC") next;
      if (type=="UNINITIALIZED") type="STRING";
      printf "-D%s:%s=%s\n", var, type, val;
    }' "$REF"
}

configure(){   # configure <srcdir> <builddir> <label>
  local src=$1 bld=$2 lbl=$3
  local flags=()
  while IFS= read -r f; do flags+=("$f"); done < <(ds_from_ref)
  flags+=(-G Ninja -DCMAKE_BUILD_TYPE=Release -B "$bld" -S "$src")
  log "$lbl: configuring ($(printf %s\\n "${flags[@]}" | tr '\\n' ' '))"
  ( export PATH=/opt/rocm-6.1.0/bin:$PATH
    cmake "${flags[@]}" > "$bld-cmake.log" 2>&1 ) \
    || { echo "$lbl CONFIGURE FAILED (see $bld-cmake.log)"; tail -5 "$bld-cmake.log"; return 1; }
  echo "$lbl: configured ok"
}

build_one(){   # build_one <srcdir> <builddir> <label>
  local src=$1 bld=$2 lbl=$3 j try rc=1
  mkdir -p "$bld"
  configure "$src" "$bld" "$lbl" || return 1
  for try in 1 2 3; do
    j=$(jfor)
    log "$lbl: nice ninja -j$j strata (attempt $try)"
    nice -n 19 ninja -j"$j" -C "$bld" strata 2>&1 | tee "$bld/build.log" | tail -2
    rc=${PIPESTATUS[0]}
    [ "$rc" -eq 0 ] && break
    log "$lbl: ninja rc=$rc - waiting 20s and retrying"; sleep 20
  done
  if [ "$rc" -ne 0 ]; then echo "$lbl BUILD FAILED rc=$rc"; return 1; fi
  echo "$lbl: binary $bld/strata sha $(sha16 "$bld/strata")"
}

log "fresh-build pair start (nice/-j2, -j1 under 3GiB MemAvailable)"
[ -f "$WT/CMakeLists.txt" ] || { echo "WORKTREE MISSING at $WT"; exit 1; }
build_one "$WT" "$ROOT/build-fresh-f4e0/rpcw-hip" fresh-f4e0bc3d
R4=$?
build_one "$ROOT" "$ROOT/build-fresh-head/rpcw-hip" fresh-head
RH=$?
log "warning diff (f4e0 vs head)"
if [ -f "$ROOT/build-fresh-f4e0/rpcw-hip/build.log" ] && [ -f "$ROOT/build-fresh-head/rpcw-hip/build.log" ]; then
  diff <(grep -i 'warning' "$ROOT/build-fresh-f4e0/rpcw-hip/build.log" | sed 's/[0-9]\\+/N/g' | sort -u) \
       <(grep -i 'warning' "$ROOT/build-fresh-head/rpcw-hip/build.log" | sed 's/[0-9]\\+/N/g' | sort -u) \
    && echo 'warning sets identical' | tee "$ROOT/build-fresh-warnings.diff"
fi
log "SUMMARY"
echo "fresh-f4e0 : rc=$R4 sha=$(sha16 "$ROOT/build-fresh-f4e0/rpcw-hip/strata") (V5 reference: 331c2e89d00dd4f1)"
echo "fresh-head : rc=$RH sha=$(sha16 "$ROOT/build-fresh-head/rpcw-hip/strata") (regression bench ref: f5fb0738218485a2)"
log "fresh-build pair end"
