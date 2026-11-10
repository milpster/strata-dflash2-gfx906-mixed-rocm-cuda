# ab9 results

git: `ab9-window-harness b8f777d34 bench(ab9): fmt0 verdict - quant span hidden not critical (lock -42%, wall -0.8%); fa733f62d kept gated-off`

host: `7.3.3-x64v3-xanmod1`; nvidia: `NVIDIA GeForce RTX 3080 Laptop GPU, 86 MiB, 8192 MiB`

| arm | ctx | legs | PP med | spread | claims | rows | Σbegin_us | Σreply_us | Σd2h_us | Σlock_us | Σqueue_us | wall_s | transfer_share | helper_side_us | rows/claim | fixed_cost/claim | gain | pipe_recovery | drift_vs_C | rxpci GB/s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A | 8000 | 3 | 195.6 | 3.7 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 25.57 | 0.0 | 0 | 0.0 | 0.0 |  |  |  | 0.0 |
| B | 8000 | 3 | 199.7 | 23.5 | 96 | 224075 | 426851 | 5024188 | 10360 | 4558124 | 0 | 26.07 | 0.209482 | 4558124 | 2334.11 | 56781.7 | 0.021 |  |  | 0.0 |
| D | 8000 | 3 | 205.8 | 3.4 | 96 | 223976 | 413054 | 4251203 | 10768 | 4218813 | 3 | 24.48 | 0.190973 | 4218816 | 2333.08 | 48586.0 | 0.0521 |  |  | 0.0 |
| E | 8000 | 3 | 195.1 | 4.5 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 25.64 | 0.0 | 0 | 0.0 | 0.0 | -0.0026 |  |  | 0.0 |
| A | 80000 | 3 | 309.1 | 1.5 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 145.3 | 0.0 | 0 | 0.0 | 0.0 |  |  |  | 0.0 |
| B | 80000 | 3 | 305.4 | 2.1 | 96 | 24576 | 74421 | 886508 | 9935 | 686272 | 0 | 147.08 | 0.006601 | 686272 | 256.0 | 10009.7 | -0.012 |  |  | 0.0 |
| D | 80000 | 3 | 306.4 | 2.2 | 96 | 24576 | 60682 | 783803 | 9834 | 642590 | 0 | 146.71 | 0.005823 | 642590 | 256.0 | 8796.7 | -0.0087 |  |  | 0.0 |
| E | 80000 | 3 | 309.7 | 1.8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 142.4 | 0.0 | 0 | 0.0 | 0.0 | 0.0019 |  |  | 0.0 |

## TG sanity (cached rerun; walls = run1/run2 s)

- A tg1024 ctx=2048 max_tokens=1024 walls=[26.346, 23.464]
- B tg1024 ctx=2048 max_tokens=1024 walls=[25.78, 8.814]
- D tg1024 ctx=2048 max_tokens=1024 walls=[15.266, 10.818]
- E tg1024 ctx=2048 max_tokens=1024 walls=[31.676, 14.251]

Fresh-judge invariant (0 reused on every fresh leg): True
Confound flags (leg slices carrying 'below 512 MiB'): []
