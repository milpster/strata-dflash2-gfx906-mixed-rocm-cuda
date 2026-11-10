# ab9 results

git: `ab9-window-harness f53231b2f bench(ab9): engine-span decomposition - router 64.6% of layer time; helper wait 9.1%; attn 4%; router is the next lever`

host: `7.3.3-x64v3-xanmod1`; nvidia: `NVIDIA GeForce RTX 3080 Laptop GPU, 45 MiB, 8192 MiB`

| arm | ctx | legs | PP med | spread | claims | rows | Σbegin_us | Σreply_us | Σd2h_us | Σlock_us | Σqueue_us | wall_s | transfer_share | helper_side_us | rows/claim | fixed_cost/claim | gain | pipe_recovery | drift_vs_C | rxpci GB/s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A | 8000 | 3 | 195.6 | 3.7 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 25.57 | 0.0 | 0 | 0.0 | 0.0 |  |  |  | 0.0 |
| B | 8000 | 3 | 199.7 | 23.5 | 96 | 224075 | 426851 | 5024188 | 10360 | 4558124 | 0 | 26.07 | 0.209482 | 4558124 | 2334.11 | 56781.7 | 0.021 |  |  | 0.0 |
| D | 8000 | 3 | 204.6 | 3.2 | 96 | 223611 | 391507 | 4011997 | 4290 | 3982959 | 0 | 24.73 | 0.178244 | 3982959 | 2329.28 | 45869.8 | 0.046 |  |  | 0.0 |
| E | 8000 | 3 | 195.1 | 4.5 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 25.64 | 0.0 | 0 | 0.0 | 0.0 | -0.0026 |  |  | 0.0 |
| A | 80000 | 3 | 309.1 | 1.5 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 145.3 | 0.0 | 0 | 0.0 | 0.0 |  |  |  | 0.0 |
| B | 80000 | 3 | 305.4 | 2.1 | 96 | 24576 | 74421 | 886508 | 9935 | 686272 | 0 | 147.08 | 0.006601 | 686272 | 256.0 | 10009.7 | -0.012 |  |  | 0.0 |
| D | 80000 | 3 | 306.0 | 1.8 | 96 | 24576 | 56508 | 792767 | 3719 | 612426 | 0 | 146.68 | 0.005815 | 612426 | 256.0 | 8846.6 | -0.01 |  |  | 0.0 |
| E | 80000 | 3 | 309.7 | 1.8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 142.4 | 0.0 | 0 | 0.0 | 0.0 | 0.0019 |  |  | 0.0 |

## TG sanity (cached rerun; walls = run1/run2 s)

- A tg1024 ctx=2048 max_tokens=1024 walls=[26.346, 23.464]
- B tg1024 ctx=2048 max_tokens=1024 walls=[25.78, 8.814]
- D tg1024 ctx=2048 max_tokens=1024 walls=[28.677, 17.119]
- E tg1024 ctx=2048 max_tokens=1024 walls=[31.676, 14.251]

Fresh-judge invariant (0 reused on every fresh leg): True
Confound flags (leg slices carrying 'below 512 MiB'): []
