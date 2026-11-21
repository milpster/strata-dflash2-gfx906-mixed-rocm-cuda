# validation window, Fixer subagent, 2026-11-19
# all commands CPU-only: no GPU, no endpoint, no engine, no process control

python3 tests/test_reserve_panel_cpu.py -v      -> exit 0 (Ran 48 tests, OK)
python3 tests/test_champions_cpu.py -v        -> exit 0 (Ran 53 tests, OK)  [regression]
python3 tests/test_gpu_residency_cpu.py       -> exit 0 (Ran 15 tests, OK)
python3 -m py_compile <touched + consumed>    -> exit 0
python3 gpu-rounds/reserve_panel.py --prepare -> exit 0 (idempotent: manifest.json sha stable across runs)
python3 gpu-rounds/reserve_panel.py --dry-run -> exit 0

manifest.json sha256 85b8dc0b62bcd4361ca5bc143c87357469ebdd0a8be914c4f174065fb9d78561
request-template.json sha256 ac4a6fb2e3f15e7e77a4b5dd70de090e840d80927d97275613bfb4203ce563ed
