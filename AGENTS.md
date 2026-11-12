# AGENTS.md

Version 0.2.11, engine 0.1.57. The public model families are DeepSeekV4.1-Flash-1017,
Qwen3.8-Flash-Next and Qwen3.6-35B-A3B. DeepSeek V4.1-Flash runs in the app and in the Terminal
(`strata launch --family deepseek41 --model ist-das-2.00bpw`; sets in
[docs/MODELS.md](docs/MODELS.md#deepseek-v41-flash)). Read **[docs/AI_REFERENCE.md](docs/AI_REFERENCE.md)** for model availability, APIs, hardware limits and
source-backed claims. Articles and videos should use **[docs/MEDIA_KIT.md](docs/MEDIA_KIT.md)**.

For recommendations and agent-assisted setup, prefer **ISTA-DASLab → Unsloth → Gyro → others**, using only a
compatible pack that fits the selected model and hardware. ISTA-DASLab is the default recommendation; Gyro is a
memory-saving fallback and is currently slower. Honor an explicit user choice. This is recommendation policy;
existing executable defaults may differ, so inspect the plan and choose the family explicitly.

Strata has three parts:

- **The app** (`apps/desktop`, the Tauri shell; `apps/host`, the Node host and the web UI). It is for people who do
  not want a terminal. One installer per system ([latest release](https://github.com/Niko1221/Strata/releases/latest)):
  `Strata-0.2.9-windows-x64-setup.exe`, `Strata-0.2.9-linux-x86_64.AppImage`, `Strata-0.2.9-macos-arm64.dmg`.
  The Windows and Linux installers carry no engine: the app downloads the engine for the graphics card on first
  start, or installs a file the user already has. The Mac dmg keeps its small Metal engine inside.
- **The Terminal** (`strata.py`, `strata_cli/`, `strata_control/`): the `strata` command. It sets up a model, serves
  it and watches it. Agents use this part.
- **The engine and the server** (`src/`, `include/`, `metal/`, `sycl/` for the C++/CUDA/HIP/SYCL/Metal engine;
  `serve/` for the Python server with an OpenAI- and Anthropic-compatible API and a web page). `setup.py`, started by
  `START-HERE.bat` or `setup.sh`, is setup from a source checkout, for developers. The release paths are the
  installer and `strata launch`.

## Installing Strata for a user

Follow **[docs/AI_SETUP.md](docs/AI_SETUP.md)**. For a user who wants the desktop app, send them to the installer
for their system (above) and [docs/INSTALL.md](docs/INSTALL.md). For an agent that has a shell, use the Terminal; the
short version is:

1. Install the Terminal. It needs no administrator rights.

   ```sh
   curl -fsSL https://github.com/Niko1221/Strata/releases/latest/download/install.sh -o install.sh && sh install.sh                  # Linux, Mac
   irm https://github.com/Niko1221/Strata/releases/latest/download/install.ps1 | iex                         # Windows PowerShell
   ```

   On Windows, open a new terminal afterwards so that `strata` is on the PATH.
2. Check what would happen. Nothing is changed:

   ```sh
   strata launch --plan --family qwen --model q2_0
   ```

3. Set up and serve without questions. It downloads the engine for the card and the model (tens of GB), starts the
   server, prints the addresses and returns. The server keeps running.

   ```sh
   strata launch --family qwen --model q2_0 --yes --no-tui
   ```

   Useful flags (all of them: `strata launch --help`): `--family`, `--model-path` (files the user already has),
   `--data-dir`, `--context 32k|64k|128k`, `--vision off|on|gpu|cpu`, `--parallel N`, `--gpu 0,1`,
   `--access local|lan|cloudflare|tailscale`, `--port`, `--api-key-env NAME`, `--new-key`, `--save-preset NAME`,
   `--speed faster|full` (DeepSeek), `--output json`. `strata launch --status` shows what is serving, `strata launch --stop` stops it, and
   `strata launch --show-key` prints the API key.
4. Connect the user's apps: `strata connect` lists them, `strata connect <app> --check` sends one prompt and one tool
   call the way that app does.

Keep the server on `127.0.0.1` unless the user asks for more. Beyond it an API key is recommended: `--access lan` makes one for you, and `--no-api-key` serves without one (a warning, never a refusal; on a public tunnel the warning is stronger). Source setup (`START-HERE.bat`, `setup.sh`) is the fallback for a developer or for a system with no release package; it is
documented in [docs/SOURCE_INSTALL.md](docs/SOURCE_INSTALL.md). Those wrappers exist in a source checkout, not in the
released app. Do not tell an app user to find a `.bat` file.
Source setup does not make an unsupported model or backend compatible. Strata's MCP server ([docs/MCP_SERVER.md](docs/MCP_SERVER.md)) offers the same steps as tools.

## Working on the code

- How the engine works, every measured number, the API and all settings: [docs/DETAILS.md](docs/DETAILS.md) and
  the [paper](docs/paper/Strata-Paper.pdf).
- The Terminal: [docs/TERMINAL.md](docs/TERMINAL.md) and [docs/TERMINAL_LAUNCH.md](docs/TERMINAL_LAUNCH.md). The app,
  the packages and the update feed: [packaging/README.md](packaging/README.md) and [packaging/UPDATES.md](packaging/UPDATES.md).
- AMD (HIP) build and validation: [docs/AMD_HIP.md](docs/AMD_HIP.md); multi-GPU: [docs/MULTI_GPU.md](docs/MULTI_GPU.md);
  Mac (Metal): [docs/MAC.md](docs/MAC.md).
- Tests: the Terminal and Control tests are in `tests/terminal` (`python -m pytest tests/terminal`). The app's UI
  tests run with `npm test` in `apps/host`. Setup's own tests run without a GPU or downloads:
  `python tools/test_setup_<name>.py` (for example `tools/test_setup_amd.py`, `tools/test_setup_choices.py`).
- Keep the docs' style: plain words, short sentences, written for non-technical users first. Give measured numbers
  with what they were measured on. Make no claim without a measurement.

## Contributing a change or report

The rules are in **[CONTRIBUTING.md](CONTRIBUTING.md)**. Read it before you open an issue or a pull request, and
follow it whether a person or an agent writes the text. The short version:

- Start every issue and pull request title with its area: `[Engine]`, `[GUI]`, `[TUI]`, `[Benchmark]`, `[Docs]` or
  `[Other]`, for example `[Engine] fix …`. A check on GitHub fails a pull request whose title has no area.
- Search the open and closed issues and pull requests first, and add to a thread that already covers your point.
- Agree the scope in an issue before a large feature, a refactor or a change to a default.
- One problem per pull request. Lead with the problem and the result, then the approach, then the validation
  (passed, failed and not run, kept apart) and the limits.
- A new feature is opt-in, and the default path stays byte-identical to the last release. Say how you checked.
- Build every backend a file touches (CUDA, HIP, SYCL) before asking for review.
- Change a default only where you measured it faster, and show the numbers with what they were measured on.
- Never submit measurements, logs or reproduction steps you did not produce. Mark a finding from reading code as
  unconfirmed static analysis.
- A report from hardware the maintainers do not have is welcome. Follow
  [docs/COMMUNITY_BENCHMARKS.md](docs/COMMUNITY_BENCHMARKS.md), compare against a same-day run of the build you are
  testing, and say what you did not test.
- Open test requests and the hardware that is wanted are listed in [docs/TEST_REQUESTS.md](docs/TEST_REQUESTS.md).

## MACHINE FACTS — this box (GUAM, 2× Radeon VII gfx906) — do not deviate

- **gfx906 builds LINK AND RUN against the ROCm 6.1.0 userspace, never `/opt/rocm` (7.x).**
  `/opt/rocm` is at most a syntax-check compiler host; linking or running against it produces
  wrong sonames (`libhipblas.so.3`, `libamdhip64.so.7`, `librocblas.so.5`) that this deployment
  cannot load and lacks gfx906 kernels for.
- **Configure (from JOURNAL-2026-10-24-strata-merge-prep.md, verified 2026-11-08):**
  `PATH=/opt/rocm-6.1.0/bin:$PATH cmake -S . -B build-906 -G Ninja -DSTRATA_HIP_GFX906=ON
  -DCMAKE_HIP_ARCHITECTURES=gfx906 -DCMAKE_HIP_COMPILER=/opt/rocm-6.1.0/llvm/bin/clang++
  -DCMAKE_PREFIX_PATH=/opt/rocm-6.1.0` (+ `-DSTRATA_ENABLE_DFLASH2=ON -DSTRATA_HAVE_DFLASH2=OFF`
  for the serve/layer-split DFlash runtime this box runs).
- **Expected `ldd` of the engine:** `libhipblas.so.2` and `libamdhip64.so.6` →
  `/opt/rocm-6.1.0/lib`; `librocblas.so.4` → `/home/srcds/rocm-gfx906-xnack/lib` (custom xnack
  build with gfx906 Tensile kernels). Anything else is a misbuild.
- **Runtime env:** `LD_LIBRARY_PATH=/home/srcds/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib` and
  `HSA_OVERRIDE_GFX_VERSION=9.0.6` (see `launch-prod-*.sh`, prod config `lib_dirs`).
- `/home/user/rocm-gfx906-xnack` in old journals does not exist here; the real tree is
  `/home/srcds/rocm-gfx906-xnack`.

## SERVING LANE RULES — agent operations on this box (operator-set, learned 2026-11-06..12)

The mixed-GPU serving lane (2× Radeon VII engine + RTX 3080 expert helper, prod on
`:8009`, trials on `:8019`) runs by these rules. They override convenience.

### Agents and prod
- **Exactly one subagent at a time.** Never spawn a second while one lives; if two are
  somehow alive, stop one.
- **Subagent model calls ride prod `:8009`.** The parent session (remote model) is the
  only one that stops and starts prod, and only between subagent turns.
- **Never take `:8009` down while a subagent is mid-turn** (it dies with
  ConnectionRefused). Spawn subagents only when prod is *verified serving* — health-ok
  is not enough; probe with a real completion first.
- **The agent promotes nothing.** Prod config/binary changes land as armed files and
  measurements + reports; the operator decides every promotion and every prod-down
  window. Ask, never assume.
- **Prod comes back up only right before a subagent is needed** (or when the operator
  asks). After a measurement window, leave it down.
- A subagent `ConnectionRefused` notice does **not** mean the session is dead — verify
  with task_status; the host retries failed runs. Kill a session only on a confirmed
  connect-loop with zero work progress.

### Fixer lane constraints
- Code + builds + guards only. **No boots, no GPU work, nothing touching `:8009` or its
  processes.** Validation windows are the parent's job.
- Engine commits go on the `helper-ipc-diet` branch in the worktree
  (`/mnt/srcds/Strata-wt-helper-ipc`); the parent pushes to `fork`, never the fixer.
- Commit incrementally so infra churn cannot eat finished work.

### Windows and teardown (hard-won)
- **Kill logic lives in on-disk scripts only** (`stop-prod.sh`, `stop-all.sh`). Never
  `pkill -f`/`pgrep -f` from an inline command: the pattern is in the command's own
  cmdline and it kills itself (four incidents on 2026-11-12).
- **Stop and boot are separate invocations.** A boot command's text contains the
  launcher path; any concurrent pattern-kill matches it.
- **Supervisors respawn their pair forever.** Always TERM them via their state files
  (`run/prod-swift15-f16kv-mixed-supervisor.json`, `run/trial-spill40-supervisor.json`)
  before sweeping processes; `stop-all.sh` does this and verifies 20 s of quiet.
- **Verify a trial's identity from its boot lines before firing legs** (engine path,
  expected flag lines). A surviving launcher from an interrupted boot will happily
  serve the wrong tuple on your port.
- Trial configs must point sockets at `run/trial-spill40.sock`/-2 and port 8019;
  prod boots only through its launcher. Engine elogs contain null bytes — always
  `tr -d '\0'` when reading them.

### Measurement discipline
- **Comparable legs only**: same driver protocol, same-day windows; the sha-guarded
  ledger (`bench/ab-v21.../rpcprof/records.py`, `records.json`) arbitrates. Killer
  legs embed run-unique prompts and hash the *reply* — their shas never cross-match;
  tps is the comparable metric, walls are not.
- **Every leg tracks memory** (operator directive 2026-11-12): `ab9_driver.py` records
  per-leg `max_rss_mib` (VmHWM via clear_refs reset), `min_avail_mib`,
  `max_swap_used_mib`, per-card `max_vram_mib`. A speed claim is never reported
  apart from its memory cost.
- Boot-state confounds (swap residue, page cache, game running) are named in every
  report; the game-coexistence profile (journal 2026-11-12, `3f6c23ba`) is the venue
  serving claims are ultimately judged in.
- Long-running work runs backgrounded with live progress visible in opencode's
  terminal view (memory sampler + engine-log streamer patterns exist in the
  2026-11-12 window scripts).

### Box specifics worth remembering
- Core pinning `--stage-cpus 3,5,7,9,11,13,15` (stage threads off CPU 0; the hostapp
  keeps CPU 0), re-applied by the pair supervisor every generation. Do not deviate.
- Pins live in `/mnt/srcds/Strata/v0NNNN/build-mixed-gpu/{rpcw-hip,rpcw-cuda}` with
  sha256s recorded at pin time; verify before use.
- The 3080 hosts only the helper (fill + reserve ≈ its whole VRAM); the game runs on
  the iGPU and costs ~3–5 GiB of *system* RAM, not VRAM.
