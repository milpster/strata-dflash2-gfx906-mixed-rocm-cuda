#!/usr/bin/env bash
# page_stability.sh - root knobs for the amdgpu no-retry permission-fault test.
#
# Why: the engine's recurring fault (amdgpu [mmhub0] no-retry page fault, XDMA
# client, PERMISSION_FAULTS 0x3, RW 0x0) was measured on 2026-11-24 00:49:26 to
# hit a host VA inside the engine's anonymous 32 GiB RAM pool
# (pool_bytes=34359738368) WHILE that VMA was still mapped - i.e. the CPU mapping
# survived and only the GPU's translation was revoked. A registered host range is
# a KFD userptr: amdgpu invalidates the GPU queue's view of it whenever the CPU
# page tables under it change. On this box THP is 'always',
# vm.compaction_proactiveness is 20, and the run sits at ~7 GiB of swap when it
# dies. THP collapse/split, compaction migration and anon reclaim all change those
# page tables. This script sets the four knobs that drive them, and can restore
# the old values exactly.
#
# Usage (root):
#   sudo bench/fault-va/page_stability.sh status     # show current values
#   sudo bench/fault-va/page_stability.sh stable     # apply the test profile
#   sudo bench/fault-va/page_stability.sh thp-only   # THP off, rest untouched
#   sudo bench/fault-va/page_stability.sh restore    # back to the saved values
#
# 'stable' saves the current values to state file before changing anything, so
# 'restore' always returns the box to what it was. No reboot needed either way.
set -euo pipefail

STATE=${STATE:-/var/tmp/page_stability.saved}
THP=/sys/kernel/mm/transparent_hugepage
die() { echo "page_stability: $*" >&2; exit 1; }
need_root() { [[ ${EUID:-$(id -u)} -eq 0 ]] || die "must run as root (sudo)"; }

read_thp() {
  # the bracket marks the active setting, e.g. 'always [defer] defer+madvise'
  sed -E 's/.*\[([^]]+)\].*/\1/' "$1" 2>/dev/null || true
}

show() {
  printf '%-42s %s\n' 'transparent_hugepage/enabled' "$(cat $THP/enabled 2>/dev/null || echo '? (absent)')"
  printf '%-42s %s\n' 'transparent_hugepage/defrag' "$(cat $THP/defrag 2>/dev/null || echo '? (absent)')"
  printf '%-42s %s\n' 'khugepaged/defrag' "$(cat $THP/khugepaged/defrag 2>/dev/null || echo '? (absent)')"
  printf '%-42s %s\n' 'vm.compaction_proactiveness' "$(sysctl -n vm.compaction_proactiveness 2>/dev/null || echo '? (absent)')"
  printf '%-42s %s\n' 'vm.swappiness' "$(sysctl -n vm.swappiness 2>/dev/null || echo '? (absent)')"
  printf '%-42s %s\n' 'MemAvailable' "$(awk '/^MemAvailable:/{printf "%.1f GiB", $2/1048576}' /proc/meminfo)"
  printf '%-42s %s\n' 'swap used' "$(free -m | awk '/^Swap:/{printf "%.1f GiB", $3/1024}')"
  printf '%-42s %s\n' 'AnonHugePages' "$(awk '/^AnonHugePages:/{printf "%.2f GiB", $2/1048576}' /proc/meminfo)"
}

save() {
  [[ -e $STATE ]] && die "$STATE already exists - run 'restore' first, or delete it if you mean to overwrite"
  {
    echo "thp_enabled=$(read_thp $THP/enabled)"
    echo "thp_defrag=$(read_thp $THP/defrag)"
    echo "khugepaged_defrag=$(cat $THP/khugepaged/defrag 2>/dev/null)"
    echo "vm_compaction_proactiveness=$(sysctl -n vm.compaction_proactiveness)"
    echo "vm_swappiness=$(sysctl -n vm.swappiness)"
    echo "saved_at=$(date -Is)"
  } > "$STATE"
  chmod 600 "$STATE"
  echo "page_stability: saved current values to $STATE"
  sed -n '1,4p' "$STATE" | sed 's/^/  /'
}

apply_stable() {
  save
  echo never > "$THP/enabled"
  echo never > "$THP/defrag"
  echo 0     > "$THP/khugepaged/defrag" 2>/dev/null || true
  sysctl -qw vm.compaction_proactiveness=0
  sysctl -qw vm.swappiness=0
  echo 'page_stability: stable profile applied (THP off, compaction proactive off, swappiness 0)'
}

apply_thp_only() {
  save
  echo never > "$THP/enabled"
  echo never > "$THP/defrag"
  echo 'page_stability: THP disabled only (compaction/swappiness untouched)'
}

restore() {
  [[ -f $STATE ]] || die "no saved state at $STATE - nothing to restore"
  local thp_enabled thp_defrag khugepaged_defrag vm_compaction_proactiveness vm_swappiness
  # shellcheck disable=SC1090
  . "$STATE"
  echo "$thp_enabled" > "$THP/enabled"
  echo "$thp_defrag"  > "$THP/defrag"
  # The probe sets this one too, so it must be saved and restored like the rest
  # (it was missed in the first version: an operator had to put it back by hand).
  [[ -n ${khugepaged_defrag:-} ]] && echo "$khugepaged_defrag" > "$THP/khugepaged/defrag" || true
  sysctl -qw "vm.compaction_proactiveness=$vm_compaction_proactiveness"
  sysctl -qw "vm.swappiness=$vm_swappiness"
  echo 'page_stability: restored'
  show
  echo "page_stability: saved state kept at $STATE (delete it when satisfied)"
}

need_root
case "${1:-}" in
  status)  show ;;
  stable)  apply_stable; echo; show ;;
  thp-only) apply_thp_only; echo; show ;;
  restore) restore ;;
  *) die "usage: $0 status | stable | thp-only | restore" ;;
esac
