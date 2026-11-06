# xxs i8kv + persistent expert helper - 2026-11-06

## Root cause: the ud-iq3xxs silent exit-1 (Nov 5 22:47-22:52 crash-loop, 12 gens)

The config pinned `--native-dense-gguf` to shard 1 of the 4-way split model GGUF.
Raw GGUF metadata parse (tensor-info section, `python3`, this session):

- `Qwen3.8-Flash-Next-UD-IQ3_XXS-00001-of-00004.gguf`: **n_tensors = 0** (metadata-only first shard)
- shard 2: 1210 tensors incl. ALL `blk.N.attn_gate/attn_qkv/ssm_out` (types 14/8), shard 3/4: the rest

Chain: `NativeDense::served_names` (src/core/native_dense.cpp:178) opens ONLY the passed shards ->
empty skip set -> `WeightTable::load` (src/core/weights.cpp:303) hits the shape-only row
`blk.0.attn_gate.weight` (code_bits != 0, dst_bytes == 0 - present in BOTH packs, harmless when
skipped, and the iq3s pack's rows ARE skipped by the single-GGUF served set) -> loader aborts ->
generate.cpp:4143 prints the warning (`this pack holds the tensor only in its GGUF form
(run with --native SHARD1)`) and `return 1`. Silent: the warning IS the last engine line;
the `load_hparams`/`strata-vision: on the CPU` lines after it in the engine log belong to the
vision sidecar process (server.py starts it concurrently writing the same log).

Engine code is right: with `--native-dense-gguf` OMITTED, generate.cpp:3487 expands to all shards
via `gguf_split_paths`. `NativeDense::load` validates the split metadata and requires shard 1
(split.no == 0) as the first path. **Fix = config lists all 4 shards** (the flag is repeatable:
"--native ... repeat for each source model shard"). No engine source change; the promoted
memo engine 66446e21 stays byte-identical in v0210.

Vision note: the sidecar opens shard 1 for metadata only (mtmd loads weights from mmproj) -
it works and printed "on the CPU, 8 threads" in every crash generation. Unrelated.

## Helper death chain (Nov 6 01:59) and the fix

kernel `amdgpu 0000:04:00.0: Fence fallback timer expired` -> GPU reset -> engine abort ->
helper primary channel EOF -> serve_channel break -> main releases the card and exits ->
every later engine logs `expert-helper: connect ... failed ... (start the helper before the
engine; the CPU path stays in use)` -> prod on the CPU expert path since 02:00.

**Fix: `--persist` (opt-in, default OFF = byte-for-byte the old single-session helper).**
main() now loops: engine session ends (peer EOF / bye / begin-timeout) and no signal ->
reset the engine-bound handshake state exactly as the existing pre-hello re-listen does
(g_claimed, g_reserve, g_slots, hp.layer_lo/hi; `filled` is per-session), drain the wake pipe,
clear main's own stop latch, print `--persist: engine session N ended; listening again`,
re-spawn the second-channel thread, and accept the next engine on the SAME bound listeners
(no rebind; socket paths stay, launcher's stale-socket checks keep working).
SIGTERM/SIGINT: a separate `g_sig_count` fact gates the loop (main latches g_stop itself at
session end, so the latch cannot answer "was it a signal" - the first implementation used
`g_stop.exchange(0)` and the selftest caught it exiting right after session 1); a signal
during teardown exits cleanly, a signal during the new accept takes the pre-existing named
exit. R5 P1's "latched stop wins over re-listen" is preserved (helper_relisten_allowed untouched).
kFill after reconnect: `serve_fill` skips resident pairs; `ExpertCache::open_sized` -> `open`
-> `close()` first, so the new engine's fill re-allocates and re-uploads cleanly (the refill
costs ~the fill time, the tier comes back by itself).

Selftest (new, reusable): `tools/mixed_gpu/sidecar/persist_selftest.py` - scratch sockets,
no fill, seconds long; PASS on the deployed binary: connect/bye, re-accept, post-hello EOF
-> persist line -> reconnect -> session 2 EOF -> persist -> SIGTERM exit 0.
`ctest -R helper` in rpcw-cuda: 5/5 green (socket/fallback/microbench/candidates/ownership).

## Launcher: helper pack mismatch

The ud launcher started the helper with `--pack /mnt/user/ai/iq3s` + IQ3_S experts while the
engine runs the iq3xxs-ud pack: the helper would mirror the WRONG expert bytes (geometry passes,
blobs decode from the iq3s GGUF). Per sidecar semantics (helper_main.cpp pack.open ->
FileExpertSource::open_gguf, check_experts_gguf): `--experts` names shard 1;
`native_experts.txt` v4 resolves per-role shard files relative to that directory and validates
type/shape/offset per shard. New args: `--pack /mnt/user/ai/iq3xxs-ud --experts
../Qwen3.8-Flash-Next-UD-IQ3_XXS-00001-of-00004.gguf --persist`.
Pack layout verified: gu_type 22 / d_type 20, accepted by native_expert_supported (is_iq covers
both; same d type the iq3s helper serves in prod today).

## Deployed binaries

- v0210 rpcw-cuda `strata-expert-helper` = sha256 79abba1eee00 (from /home/user/dev/Strata
  build-mixed-gpu/rpcw-cuda, incremental off the deployed build's cache; source = sidecar in
  the dev tree, synced to the cuda-helper worktree tree). Old prod helper kept aside:
  `strata-expert-helper.afbf19f7-aside`.
- Engine rpcw-hip: untouched (66446e21... memo engine, promoted 2026-11-05).
- pair_supervisor.py: NOT touched - the persistent helper makes the supervisor's job
  unchanged (engine restarts reconnect); single-shot launcher semantics of the other
  variants untouched (the flag is opt-in).

## Cutover procedure (parent operator)

`/mnt/user/Strata/tools/xxs-i8kv-cutover-test.sh` - single entry point, idempotent,
STAGE A engine-only boot (fixed config, helper --persist, `session is up` + `engine connected`),
STAGE B full launcher boot + completion + kill-engine-only hiccup (requires the persist
re-listen + reconnect + completion #2), STAGE C verify ud-iq3_xxs on :8009.
Any failure restores swift-iq3_s-f16kv and verifies a completion. Never leaves :8009 down.
Final line: `VERDICT: PASS-CUTOVER` | `VERDICT: RESTORED iq3s-f16kv`.

Status at commit time: prod UP on swift-iq3_s-f16kv (parent-restored); cutover NOT yet run.
VERDICT + pids + log evidence appended to this journal after the parent runs the script.

## Round 2 (cutover run 20261106-071536): the fill kErrInternal + probe fixes

Run verdicts: stage A engine reached "session is up" and the --persist re-listen fired,
BUT the fill died: helper `request failed: helper internal`, engine fell to
`the expert helper is absent; the CPU path serves`. Stage B reached /v1/models =
ud-iq3_xxs + helper connected; completion #1 "no content" was the PROBE bug
(reasoning model: 24 tokens all burned on reasoning_content, content null,
finish_reason length) - restore_iq3s false-CRITICALed the same way on a verified-up prod.

### Fill root cause (static, proven)
serve_fill's H14 per-role GGUF assembly is gated `gguf_off ok && gguf_file.empty()` -
v3 packs only (single-file, absolute offsets in the mapped --experts file). The
iq3xxs-ud pack's native_experts.txt is **v4: a per-role shard column** naming shards
2-4 with SHARD-RELATIVE offsets, while hp.pack maps ONLY the primary --experts file =
shard 1 (metadata-only, 10.4 MB, 0 tensors). v4 -> per_role false -> the experts.bin
blob_offset branch -> `off + bytes > hp.pack.size` -> kErrInternal. The engine's
FileExpertSource::open_gguf maps every shard (role_ptr_, one base per file); the
sidecar's MappedPack never did. The swallowed detail (err went only into the kError
reply) is why the log said "helper internal".

### Fix (helper_main.cpp, deployed 5a949542c046; 79abba1e kept as -aside)
- Helper gains pack_path + shard_maps (a shard is mapped ONCE, lazily, linear <=4
  entries; only the fill thread touches it - kFill rides one channel).
- serve_fill assembly: gguf_pack (drop the gguf_file.empty gate) -> per role resolve
  the file via expert_role_path (mirrors expert_gguf_file: empty column = primary,
  single name covers all 3 roles - confirmed in expert_layout.cpp:500-522; comma trio
  per #255) + shard_map, bounds-check per file. v3 packs: every role resolves to the
  primary file -> byte-for-byte the r24 reads.
- Fill refusal now prints the reason to stderr (the kError reply alone hid it).
- persist_selftest PASS + ctest -R helper 5/5 on the new binary. Engine untouched.

### Script (tools/xxs-i8kv-cutover-test.sh; parent's --serve prepend + 600 s kept)
- completion_json/completion/gen_probe: request-level reasoning_effort=low +
  max_tokens 512 (server.py:5292 honors per-request effort). STAGE B PASS requires
  non-empty message.content; restore tolerates reasoning_content + finish_reason
  length (generation proven, content null is NOT a restore failure).
- STAGE A PASS gate: session is up + engine connected + helper "filled N slots" +
  NO engine "helper is absent|fill refused|channel closed after 0 requests".
- Live-validated against running prod (read-only): content "XXS-OK" returned.

Status: prod UP swift-iq3_s-f16kv (parent). Awaiting cutover round 2 verdict.

## Vision-HIP build (gfx906) - 2026-11-06

- Built build-vision-hip/bin/strata-vision (103.9 MB): tools/vision source, ggml-HIP backend, gfx906, ROCm 6.1.0 llvm clang++ 17, Release, -march=native, OpenMP.
- Recipe committed: tools/vision/build-hip.sh (d4f4d22, + f04bfbe -Xcompiler -fPIC fix: CMake HIP omits PIC on ggml-hip host objects, PIE link fails without it).
- ldd: hipblas/amdhip64 -> /opt/rocm-6.1.0/lib, rocblas -> /home/user/rocm-gfx906-xnack/lib (xnack stack wins), gomp system; no unresolved.
- Sanity: usage run exits 2 with usage line, no GPU context opened. NO --mmproj run performed (cards are prod's).
- Benchmark window (parent): LD_LIBRARY_PATH=/home/user/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib HSA_OVERRIDE_GFX_VERSION=9.0.6 strata-vision --mmproj ... --gpu
- CPU-mode caveat: source's CUDA_VISIBLE_DEVICES=-1 guard is ignored by HIP/ROCR; use HIP_VISIBLE_DEVICES="" ROCR_VISIBLE_DEVICES="" for a no-GPU run of this build.

## Radeon-vision prod variant (13:4x)
- HIP/gfx906 vision bench (camera img, 2048 tok): Q8 2.16 s / BF16 2.29 s vs CPU 33-36 s — ~16x. Root cause of earlier "no ROCm-capable device": unmasked Cezanne iGPU breaks enumeration; engine env pins HIP_VISIBLE_DEVICES=0,1 — wrapper tools/vision-hip.sh replicates it. Earlier "3080 CUDA" numbers (49-53 s) were silent 4-thread CPU fallbacks — never actual GPU.
- Helper health re-verified on live request: TG 43.4 tok/s, expert cache hit 71.9% (42021/58449), 8751 rows over PCIe; 3080 util 11-45%, power 16->26 W. /metrics hardware omits the 3080 (blind spot explains "no GPU load" impression). New warning: 231 MiB free on 3080 at request start -> engine suggests --vram-reserve-mib 576 (open decision).
- New variant committed: prod-config-...-radeonvision.json (vision exe=tools/vision-hip.sh, gpu=true, mmproj Q8, 2048 tok) + launch-...-radeonvision.sh (own sockets prod-swift15-f16kv-rv-mixed*.sock, own supervisor state/lock/logs). Trade: vision takes ~1.2 GiB on gfx906#0 at boot -> engine sizes expert cache around it (~900 fewer slots, ~9%, small TG tax). Not boot-tested yet.
