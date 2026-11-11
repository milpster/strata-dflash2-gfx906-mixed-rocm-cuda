# ab9 results

git: `ab9-window-harness 7c5cb8490 fix(bench): arm D strips the 4 conversation-cache-disk flags (disk-cache-streaming branch only; inert within a fresh boot)`

host: `7.3.3-x64v3-xanmod1`; nvidia: `NVIDIA GeForce RTX 3080 Laptop GPU, 7143 MiB, 8192 MiB`

| arm | ctx | legs | PP med | spread | claims | rows | Σbegin_us | Σreply_us | Σd2h_us | Σlock_us | Σqueue_us | wall_s | transfer_share | helper_side_us | rows/claim | fixed_cost/claim | gain | pipe_recovery | drift_vs_C | rxpci GB/s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A | 8000 | 3 | 195.3 | 2.3 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 25.71 | 0.0 | 0 | 0.0 | 0.0 |  |  |  | 0.0 |
| D | 8000 | 3 | 208.2 | 4.2 | 96 | 223686 | 404730 | 4199556 | 10329 | 4214522 | 2 | 24.28 | 0.190066 | 4214524 | 2330.06 | 47961.3 | 0.0661 |  |  | 0.0 |
| A | 80000 | 3 | 308.4 | 1.9 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 142.92 | 0.0 | 0 | 0.0 | 0.0 |  |  |  | 0.0 |
| D | 80000 | 3 | 306.9 | 1.6 | 96 | 24576 | 55688 | 708697 | 9904 | 645117 | 0 | 143.6 | 0.005392 | 645117 | 256.0 | 7962.3 | -0.0049 |  |  | 0.0 |

## TG sanity (cached rerun; walls = run1/run2 s)

- A tg1024 ctx=2048 max_tokens=1024 walls=[26.809, 23.407]
- D tg1024 ctx=2048 max_tokens=1024 walls=[26.197, 21.662]

Fresh-judge invariant (0 reused on every fresh leg): True
Confound flags (leg slices carrying 'below 512 MiB'): []
