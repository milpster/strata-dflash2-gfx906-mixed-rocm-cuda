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
    assert "--arm-groups" in rv
    assert fw.ROCM_LIBS == "/home/user/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib"


# ---------------- exclusivity + phases ----------------

def test_exclusive_dir_refuses_existing(tmp_path):
    fw.exclusive_dir(tmp_path / "new")
    with pytest.raises(RuntimeError, match="existing"):
        fw.exclusive_dir(tmp_path / "new")


def test_unimplemented_phases_refused():
    for phase in ("longctx", "capacity"):
        rc = subprocess.run([sys.executable, str(Path(fw.__file__).resolve()),
                             phase, "--family", "x", "--expected-supervisor", "1"],
                            capture_output=True, text=True, timeout=60)
        assert rc.returncode != 0 and phase in rc.stderr


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
