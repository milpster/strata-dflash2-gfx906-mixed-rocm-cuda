#!/usr/bin/env bash
# tools/mixed_gpu/cuda_prep/verify_real_bytes.sh - prove a REAL fixture's bytes are the model's own bytes.
#
# For every fixture under a real-fixture directory, read the provenance the manifest records (source GGUF,
# the tensor offsets, the per-part byte counts) and compare the fixture's blob.bin against the SAME byte
# ranges of the SAME GGUF file, part by part, with cmp.  A fixture passes only when every byte matches, the
# part sizes add up to the recorded blob_bytes, and the manifest's SHA-256 of blob.bin matches the file.
#
# Read-only over the model: this script opens the GGUF for reading and writes nothing to it.  No GPU, no
# driver, no build, no binary is executed.  Usage: verify_real_bytes.sh <real-fixtures-dir> [gguf]
set -u
export PYTHONDONTWRITEBYTECODE=1

DIR="${1:?usage: verify_real_bytes.sh <real-fixtures-dir> [gguf]}"
# [gguf] is optional: when it is given, every manifest's provenance is checked against THAT file (the manifest's
# own source_gguf is used when it is absent).  ${2:-} is the set -u safe way to read an optional argument.
GG="${2:-}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
pass=0; fail=0

key() { awk -F= -v k="$2" '$1==k {print $2; exit}' "$1"; }   # first key=value from a manifest

for f in "$DIR"/*/; do
  man="$f/manifest.txt"
  [ -f "$man" ] || continue
  lab="$(basename "$f")"
  src="$(key "$man" source_gguf)"
  [ -n "$GG" ] && src="$GG"
  go="$(key "$man" source_gate_off)";  uo="$(key "$man" source_up_off)"
  doo="$(key "$man" source_down_off)"
  gb="$(key "$man" source_gate_bytes)"; ub="$(key "$man" source_up_bytes)"; db="$(key "$man" source_down_bytes)"
  bb="$(key "$man" blob_bytes)"; want="$(key "$man" sha256_blob.bin)"
  [ -f "$src" ] || { echo "$lab: FAIL source GGUF missing ($src)"; fail=$((fail+1)); continue; }
  if [ $((gb + ub + db)) -ne "$bb" ]; then
    echo "$lab: FAIL part sizes $gb+$ub+$db != blob_bytes $bb"; fail=$((fail+1)); continue
  fi
  # Reassemble the blob from the GGUF at the recorded offsets, in the blob order [gate | up | down].
  # iflag=skip_bytes/count_bytes: byte-granular offsets with a 1 MiB block size (fast on a 76 GiB file).
  dd if="$src" of="$TMP/gate" bs=1M skip="$go" count="$gb" iflag=skip_bytes,count_bytes status=none
  dd if="$src" of="$TMP/up"   bs=1M skip="$uo" count="$ub" iflag=skip_bytes,count_bytes status=none
  dd if="$src" of="$TMP/down" bs=1M skip="$doo" count="$db" iflag=skip_bytes,count_bytes status=none
  cat "$TMP/gate" "$TMP/up" "$TMP/down" > "$TMP/reassembled"
  ok=1; why=""
  cmp -n "$bb" "$TMP/reassembled" "$f/blob.bin" > "$TMP/cmp.out" 2>&1 || { ok=0; why="$(cat "$TMP/cmp.out")"; }
  got="$(sha256sum "$f/blob.bin" | cut -d' ' -f1)"
  [ "$got" = "$want" ] || { ok=0; why="$why sha256(blob.bin) $got != manifest $want"; }
  if [ "$ok" = 1 ]; then
    pass=$((pass+1))
    echo "$lab: PASS byte-for-byte vs $src (gate@$go up@$uo down@$doo, $gb+$ub+$db = $bb)"
  else
    fail=$((fail+1))
    echo "$lab: FAIL $why"
  fi
done
echo "verify_real_bytes: $pass fixture(s) match the model's own bytes, $fail failure(s)"
[ "$fail" = 0 ] && [ "$pass" -gt 0 ]
