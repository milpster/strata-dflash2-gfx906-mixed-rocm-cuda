#!/usr/bin/env bash
# queue_fs_arm.sh — wait for queue_post_mmq.sh to finish, pin STRATA_PREFILL_MMQ=0 (A/B verdict),
# then run the FS_SLOTS 2,8 arm on the winning configuration.
R=/mnt/srcds/Strata
GR=$R/bench/ab-v0210/rpcprof/gpu-rounds

while pgrep -f 'queue_post_mmq\.sh' >/dev/null 2>&1; do sleep 20; done
sleep 5

python3 - << 'EOF'
import json
p = '/mnt/srcds/Strata/prod-config-ud-iq3xxs-dflash-w4-q4_0-mixed.json'
c = json.load(open(p))
c.setdefault('env', {})['STRATA_PREFILL_MMQ'] = '0'
json.dump(c, open(p, 'w'), indent=2)
print('pinned STRATA_PREFILL_MMQ=0 (A/B winner) | layer_split:', c['layer_split'], flush=True)
EOF
cd $R && git add prod-config-ud-iq3xxs-dflash-w4-q4_0-mixed.json && git commit -q -m "bench(prod): pin STRATA_PREFILL_MMQ=0 (A/B: OFF +7% PP, +12% TG vs ON, slots equal)"

bash $GR/fs_arm.sh 2,8 > $R/logs/fs-arm-28.log 2>&1
echo "=== FS ARM CHAIN COMPLETE ($(date +%H:%M:%S)) ==="
