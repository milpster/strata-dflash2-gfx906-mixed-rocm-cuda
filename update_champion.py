p = 'CHAMPION.md'
s = open(p).read()
old = 'Last updated 2026-11-21. Supersedes every earlier "winner", "champion" or launcher default.'
new = '''Last updated 2026-11-23. Supersedes every earlier "winner", "champion" or launcher default.

## What is serving since 2026-11-22/23 (journal-verified, parent-probed)

Engine 4b8251e9 (run/fsclamp-bin, the clamped merge build) + helper 9d16742d, config
prod-config-ud-iq3xxs-dflash-w4-q4_0-mixed-fson.json at reserves 144/144 (swap clamped off; ~600/600
re-enables it), STRATA_RAM_BUDGET_MIB=36864 (the +4/+2 GiB raises were reverted: the XDMA fault era),
context 262144, boot via launch-prod-ud-iq3xxs-dflash-w4-q4_0-mixed.sh. Decode band 20-32 tok/s,
prefill ~284-356 tok/s. The 0.2.18 PORT (branch port-0218, 4bbf7444dd) is fully runtime-validated
(quality-gate parity, 240k, helper, park/restore - journal 2026-11-23) and awaits the operator's
promotion decision; nothing in this file changes until that order. The 2026-11-21 sections below
remain the measurement record for the margin ladder and the clamp fix.'''
assert old in s, 'pattern not found'
open(p, 'w').write(s.replace(old, new))
print('CHAMPION.md updated')
