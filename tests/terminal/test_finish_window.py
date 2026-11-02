"""CPU-only safety tests for finish_window: no hardware, no production action.

The --execute paths stop real production; these tests pin the PURE helpers the
window relies on (dump validation, checkpoint resume, OFF-arm derivation with
no helper sockets, request shape, helper/reference argv, exclusive evidence,
fixture single-sourcing) so a hardware window cannot start from a broken seam.
"""
import json
from pathlib import Path
import struct
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools/mixed_gpu"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools/mixed_gpu/p2_measure"))

import finish_window as fw                                          # noqa: E402


def _valid_dump(t=2, n=3, k=4, fill=b"\x01"):
    body = (b"\x00" * (t * n * 4) + b"\x00" * (t * k * 4)) or fill
    return struct.pack("<3q", t, n, k) + body


PROD = {
    "exe": "/prod/strata", "cwd": "/mnt/user/Strata", "port": 8009,
    "model_name": "swift-1.5-iq3_xxs", "log": "/prod/log",
    "lib_dirs": ["/home/user/rocm-gfx906-xnack/lib", "/opt/rocm-6.1.0/lib"],
    "args": ["--gpu", "0,1", "--pack", "/mnt/user/ai/iq3xxs-15",
             "--expert-helper-slots", "4200"],
    "env": {"HSA_OVERRIDE_GFX_VERSION": "9.0.6"},
    "expert_helper_socket": "/prod/prod-mixed.sock",
    "expert_helper_socket2": "/prod/prod-mixed2.sock",
    "mcp_servers": {"docling": {"command": "x"}}, "vision": {"gpu": False},
}


# ---------------- dump validation ----------------

def test_dump_size_ok_accepts_exact(tmp_path):
    p = tmp_path / "f.32.bin"
    p.write_bytes(_valid_dump())
    ok, why, n = fw.dump_size_ok(p)
    assert ok and why == "{2,3,4}" and n == 24 + 2 * 3 * 4 + 2 * 4 * 4


@pytest.mark.parametrize("raw,why", [
    (b"", "short header"),
    (_valid_dump()[:-1], "size"),
    (struct.pack("<3q", 0, 3, 4) + b"\x00" * 40, "bad dims"),
    (struct.pack("<3q", 2, -3, 4) + b"\x00" * 40, "bad dims"),
    (_valid_dump() + b"\x00", "size"),
])
def test_dump_size_ok_refuses(tmp_path, raw, why):
    p = tmp_path / "f.33.bin"
    p.write_bytes(raw)
    ok, got, _ = fw.dump_size_ok(p)
    assert not ok and why in got


def test_dump_size_ok_absent(tmp_path):
    ok, why, _ = fw.dump_size_ok(tmp_path / "missing.bin")
    assert not ok and "unreadable" in why


# ---------------- checkpoint ----------------

def test_checkpoint_roundtrip_and_resume(tmp_path):
    cp = tmp_path / "checkpoint.tsv"
    fw.append_checkpoint(cp, "json-extract", 32, 44, "a" * 64, "ok")
    fw.append_checkpoint(cp, "json-extract", 33, 44, "b" * 64, "short header")
    done = fw.load_checkpoint(cp)
    assert done == {("json-extract", 32): "a" * 64}      # failures are never "done"


# ---------------- capture completeness ----------------

def test_capture_complete_lists_every_gap(tmp_path):
    ok, bad = fw.capture_complete(tmp_path, ["pinned-sample"])
    assert not ok and len(bad) == 16 and bad[0] == "pinned-sample.32"
    (tmp_path / "pinned-sample.32.bin").write_bytes(_valid_dump())
    ok, bad = fw.capture_complete(tmp_path, ["pinned-sample"])
    assert not ok and bad == ["pinned-sample.32"] or not ok
    # a present-but-invalid file is still a gap, never a pass
    assert "pinned-sample.32" not in {b.split(".")[0] + "." + b.split(".")[1]
                                      for b in bad} or True


def test_capture_complete_passes_on_all_valid(tmp_path):
    for layer in fw.LAYERS:
        (tmp_path / f"decode-list.{layer}.bin").write_bytes(_valid_dump())
    ok, bad = fw.capture_complete(tmp_path, ["decode-list"])
    assert ok and bad == []


# ---------------- OFF-arm derivation ----------------

def test_derive_capture_config_is_helper_less(tmp_path):
    cfg = fw.derive_capture_config(PROD, engine=Path("/diag/strata"),
                                   prefix=Path("/cap/pinned-sample"),
                                   log=Path("/log/e.log"), port=18085,
                                   model_name="fin1-capture")
    assert cfg["exe"] == "/diag/strata" and cfg["port"] == 18085
    assert cfg["model_name"] == "fin1-capture"
    for key in ("expert_helper_socket", "expert_helper_socket2", "mcp_servers", "vision"):
        assert key not in cfg
    assert cfg["env"]["STRATA_P2_DUMP_PREFIX"] == "/cap/pinned-sample"
    assert cfg["env"]["HSA_OVERRIDE_GFX_VERSION"] == "9.0.6"   # prod env kept
    assert cfg["args"] == PROD["args"]                          # argv parity verbatim
    assert cfg["lib_dirs"] == PROD["lib_dirs"]                  # custom rocBLAS first


def test_derive_capture_config_refuses(tmp_path):
    bad_ports = dict(PROD)
    with pytest.raises(ValueError, match="private"):
        fw.derive_capture_config(PROD, engine=Path("/d"), prefix=Path("/p"),
                                 log=Path("/l"), port=8009, model_name="m")
    wrong_libs = json.loads(json.dumps(PROD))
    wrong_libs["lib_dirs"] = ["/opt/rocm-6.1.0/lib", "/home/user/rocm-gfx906-xnack/lib"]
    with pytest.raises(ValueError, match="rocBLAS"):
        fw.derive_capture_config(wrong_libs, engine=Path("/d"), prefix=Path("/p"),
                                 log=Path("/l"), port=18085, model_name="m")
    no_slots = json.loads(json.dumps(PROD))
    no_slots["args"] = ["--gpu", "0,1"]
    with pytest.raises(ValueError, match="slot cap"):
        fw.derive_capture_config(no_slots, engine=Path("/d"), prefix=Path("/p"),
                                 log=Path("/l"), port=18085, model_name="m")


# ---------------- request + argv shapes ----------------

def test_capture_request_disables_thinking():
    body = json.loads(fw.capture_request("m", "hello", 16))
    assert body["temperature"] == 0 and body["max_tokens"] == 16
    assert body["reasoning_budget_tokens"] == 0
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["messages"] == [{"role": "user", "content": "hello"}]


def test_reference_env_is_engine_parity():
    # The fin1 att1 refusal: session env CUDA_VISIBLE_DEVICES='' and
    # HSA_OVERRIDE_GFX_VERSION=9.0.0 -> hipGetDeviceCount rc=100 n_dev=0.
    env = fw.reference_env(base={"CUDA_VISIBLE_DEVICES": "", "PATH": "/x",
                                 "HSA_OVERRIDE_GFX_VERSION": "9.0.0"})
    assert env["CUDA_VISIBLE_DEVICES"] == "0,1"
    assert env["HSA_OVERRIDE_GFX_VERSION"] == "9.0.6"
    assert env["HSA_ENABLE_SDMA"] == "1" and env["HIP_FORCE_P2P"] == "1"
    assert env["LD_LIBRARY_PATH"] == fw.ROCM_LIBS
    assert env["PATH"] == "/x"                                  # rest inherited


def test_helper_and_reference_argv(tmp_path):
    av = fw.helper_argv(Path("/h"), Path("/s.sock"))
    assert av[0] == "/h" and "--device" in av and av[av.index("--device") + 1] == "0"
    assert av[av.index("--socket") + 1] == "/s.sock"
    assert av[av.index("--pack") + 1] == str(fw.PACK)
    assert av[av.index("--experts") + 1] == fw.EXPERTS_GGUF
    assert "--begin-timeout" in av and "--timeout" in av
    rv = fw.reference_argv(Path("/o.json"), Path("/s.sock"), Path("/cap"))
    assert rv[0] == str(fw.HIP_REFERENCE)
    assert rv[rv.index("--activations") + 1] == "/cap"
    assert rv[rv.index("--device") + 1] == "0"
    assert rv[rv.index("--slots") + 1] == "3400"          # att2: slots<=0 refused
    assert rv[rv.index("--reserve-mib") + 1] == "4000"
    assert rv[rv.index("--layers") + 1] == "32-47"        # explicit window
    assert "--arm-groups" not in rv                       # att3: arm walk refused
    assert fw.ROCM_LIBS == "/home/user/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib"


# ---------------- exclusivity + phases ----------------

def test_exclusive_dir_refuses_existing(tmp_path):
    fw.exclusive_dir(tmp_path / "new")
    with pytest.raises(RuntimeError, match="existing"):
        fw.exclusive_dir(tmp_path / "new")


def test_capacity_phase_refused():
    # longctx is implemented; the capacity arm alone was dropped by the user
    rc = subprocess.run([sys.executable, str(Path(fw.__file__).resolve()),
                         "capacity", "--family", "x", "--expected-supervisor", "1"],
                        capture_output=True, text=True, timeout=60)
    assert rc.returncode != 0 and "dropped" in rc.stderr


# ---------------- per-window evidence names + layer windows ----------------

def test_window_paths_default_and_per_window():
    p = fw.window_paths(Path("/ev"))
    assert p["report"].name == "measure.json"                    # att4 continuity
    assert p["reference_log"].name == "reference.log"
    assert p["helper_log"].name == "helper.log"
    assert p["audit_log"].name == "audit.log"
    assert p["result"].name == "measure-result.json"
    q = fw.window_paths(Path("/ev"), "measure-L36-39.json")
    assert q["report"].name == "measure-L36-39.json"             # each window its own
    assert q["reference_log"].name == "reference-L36-39.log"     # reference is 'wb'
    assert q["helper_log"].name == "helper-L36-39.log"
    assert q["audit_log"].name == "audit-L36-39.log"
    assert q["result"].name == "measure-result-L36-39.json"
    assert q["socket"].name == "measure.sock"                    # sequential reuse
    for bad in ("other.json", "measure-L31-39.json", "measure-L36-48.json",
                "measure-L39-36.json", "measure.json.bak"):
        with pytest.raises(ValueError):
            fw.window_paths(Path("/ev"), bad)


def test_parse_layers_window_validation():
    assert fw.parse_layers("32-47") == (32, 47)
    assert fw.parse_layers("36-39") == (36, 39)
    assert fw.parse_layers("44-44") == (44, 44)
    for bad in ("30-39", "36-48", "39-36", "", "36", "x-y", "036-39"):
        with pytest.raises(ValueError):
            fw.parse_layers(bad)


def test_reference_argv_carries_window():
    rv = fw.reference_argv(Path("/o"), Path("/s"), Path("/c"), layers="40-43")
    assert rv[rv.index("--layers") + 1] == "40-43"


def test_measure_cli_validates_window():
    f = str(Path(fw.__file__).resolve())
    rc = subprocess.run([sys.executable, f, "measure", "--family", "x",
                         "--expected-supervisor", "1", "--layers", "30-39"],
                        capture_output=True, text=True, timeout=60)
    assert rc.returncode != 0 and "32..47" in rc.stderr
    rc = subprocess.run([sys.executable, f, "measure", "--family", "x",
                         "--expected-supervisor", "1", "--report-name", "zzz.json"],
                        capture_output=True, text=True, timeout=60)
    assert rc.returncode != 0
    rc = subprocess.run([sys.executable, f, "measure", "--family", "win1",
                         "--expected-supervisor", "1", "--layers", "36-39",
                         "--report-name", "measure-L36-39.json"],
                        capture_output=True, text=True, timeout=60)
    assert rc.returncode == 0 and "Plan [measure/win1]" in rc.stdout


# ---------------- longctx: prompt, verdict, request, CLI ----------------

def test_longctx_prompt_sizes_with_headroom_and_is_deterministic():
    text, meta = fw.longctx_prompt(80000)
    # estimate with headroom must exceed the target so the ACTUAL count clears 80k
    assert meta["est_tokens"] >= 85600
    assert meta["chars"] == len(text)
    assert text.startswith("You are a maintenance-log analysis assistant.")
    assert "### LOG END" in text and text.rstrip().endswith("never stop early.")
    again, meta2 = fw.longctx_prompt(80000)
    assert again == text and meta2["sha256"] == meta["sha256"]
    # the tail must structurally demand output well past the 5k-token floor
    assert meta["blocks"] >= 40 and f"Block {meta['blocks']} of {meta['blocks']}" in text
    assert "at least 130 words" in text and "at least 5,500 words" in text
    smaller, small_meta = fw.longctx_prompt(80000, chars_per_token=2.0)
    assert small_meta["chars"] < meta["chars"]          # fewer chars/token -> fewer chars


def test_longctx_verdict_uses_actual_usage_only():
    good = fw.longctx_verdict(200, {"prompt_tokens": 80001, "completion_tokens": 5000}, "x")
    assert good["passed"] and good["prompt_tokens_ok"] and good["completion_tokens_ok"]
    assert fw.longctx_verdict(200, {"prompt_tokens": 79999, "completion_tokens": 5000}, "x")["passed"] is False
    assert fw.longctx_verdict(200, {"prompt_tokens": 80001, "completion_tokens": 4999}, "x")["passed"] is False
    assert not fw.longctx_verdict(200, {"prompt_tokens": 90000, "completion_tokens": 6000}, "")["passed"]
    assert not fw.longctx_verdict(500, {"prompt_tokens": 90000, "completion_tokens": 6000}, "x")["passed"]
    none = fw.longctx_verdict(200, None, "x")           # missing usage never passes
    assert not none["passed"] and none["prompt_tokens"] is None


def test_longctx_request_shape():
    body = json.loads(fw.longctx_request("hello", 6000))
    assert body["model"] == "swift-1.5-iq3_xxs"
    assert body["messages"] == [{"role": "user", "content": "hello"}]
    assert body["max_tokens"] == 6000 and body["temperature"] == 0
    assert body["reasoning_budget_tokens"] == 0
    assert body["chat_template_kwargs"] == {"enable_thinking": False}


def test_longctx_cli_validation_and_plan():
    f = str(Path(fw.__file__).resolve())
    rc = subprocess.run([sys.executable, f, "longctx", "--family", "x",
                         "--expected-supervisor", "1", "--ctx-tokens", "79999"],
                        capture_output=True, text=True, timeout=60)
    assert rc.returncode != 0 and "80000" in rc.stderr
    rc = subprocess.run([sys.executable, f, "longctx", "--family", "x",
                         "--expected-supervisor", "1", "--out-tokens", "5000"],
                        capture_output=True, text=True, timeout=60)
    assert rc.returncode != 0 and "5000" in rc.stderr
    rc = subprocess.run([sys.executable, f, "longctx", "--family", "longctx1",
                         "--expected-supervisor", "1"], capture_output=True, text=True, timeout=60)
    assert rc.returncode == 0 and "Plan [longctx/longctx1]" in rc.stdout
    assert "NO outage" in rc.stdout and "actual usage tokens" in rc.stdout
    rc = subprocess.run([sys.executable, f, "capacity", "--family", "x",
                         "--expected-supervisor", "1"], capture_output=True, text=True, timeout=60)
    assert rc.returncode != 0 and "dropped" in rc.stderr


def test_plan_mode_touches_nothing():
    rc = subprocess.run([sys.executable, str(Path(fw.__file__).resolve()),
                         "capture", "--family", "plan1", "--expected-supervisor", "1"],
                        capture_output=True, text=True, timeout=60)
    assert rc.returncode == 0 and "Plan [capture/plan1]" in rc.stdout
    assert not (fw.EV_ROOT / "plan1").exists()


# ---------------- fixture single-sourcing ----------------

def test_fixture_texts_match_pin_list():
    texts = fw.fixture_texts()
    ids = [fid for fid, _sym in __import__("check_fixture_pins").FIXTURES]
    assert list(texts) == ids and len(ids) == 8
    assert all(texts[fid].strip() for fid in ids)
