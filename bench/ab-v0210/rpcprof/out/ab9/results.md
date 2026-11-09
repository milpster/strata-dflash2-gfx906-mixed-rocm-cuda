# ab9 results

git: `ab9-window-harness 5bd1aafe4 fix(bench): batching prize = eliminated RTTs x fixed floor`

host: `7.3.3-x64v3-xanmod1`; nvidia: `NVIDIA GeForce RTX 3080 Laptop GPU, 86 MiB, 8192 MiB`

| arm | ctx | legs | PP med | spread | claims | rows | Σbegin_us | Σreply_us | Σd2h_us | Σlock_us | Σqueue_us | wall_s | transfer_share | helper_side_us | rows/claim | fixed_cost/claim | gain | pipe_recovery | drift_vs_C | rxpci GB/s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A | 8000 | 3 | 195.6 | 3.7 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 25.57 | 0.0 | 0 | 0.0 | 0.0 |  |  |  | 0.0 |
| B | 8000 | 3 | 199.7 | 23.5 | 96 | 224075 | 426851 | 5024188 | 10360 | 4558124 | 0 | 26.07 | 0.209482 | 4558124 | 2334.11 | 56781.7 | 0.021 |  |  | 0.0 |
| D | 8000 | 3 | 204.1 | 1.1 | 96 | 224230 | 438639 | 4279734 | 10582 | 4206141 | 3 | 24.65 | 0.191828 | 4206144 | 2335.73 | 49149.7 | 0.0435 |  |  | 0.0 |
| E | 8000 | 3 | 195.1 | 4.5 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 25.64 | 0.0 | 0 | 0.0 | 0.0 | -0.0026 |  |  | 0.0 |
| A | 80000 | 3 | 309.1 | 1.5 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 145.3 | 0.0 | 0 | 0.0 | 0.0 |  |  |  | 0.0 |
| B | 80000 | 3 | 305.4 | 2.1 | 96 | 24576 | 74421 | 886508 | 9935 | 686272 | 0 | 147.08 | 0.006601 | 686272 | 256.0 | 10009.7 | -0.012 |  |  | 0.0 |
| D | 80000 | 3 | 306.3 | 3.6 | 96 | 24576 | 59338 | 745352 | 9929 | 633687 | 1 | 146.83 | 0.005548 | 633688 | 256.0 | 8382.2 | -0.0091 |  |  | 0.0 |
| E | 80000 | 3 | 309.7 | 1.8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 142.4 | 0.0 | 0 | 0.0 | 0.0 | 0.0019 |  |  | 0.0 |

## TG sanity (cached rerun; walls = run1/run2 s)

- A tg1024 ctx=2048 max_tokens=1024 walls=[26.346, 23.464]
- B tg1024 ctx=2048 max_tokens=1024 walls=[25.78, 8.814]
- D tg1024 ctx=2048 max_tokens=1024 walls=[19.498, 20.782]
- E tg1024 ctx=2048 max_tokens=1024 walls=[31.676, 14.251]

Fresh-judge invariant (0 reused on every fresh leg): True
Confound flags (leg slices carrying 'below 512 MiB'): []
