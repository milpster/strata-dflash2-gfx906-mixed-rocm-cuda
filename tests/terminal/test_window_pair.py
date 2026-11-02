"""File-only private-window parity and refusal checks; no production mutation."""
import hashlib
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/mixed_gpu"))
import window_pair


@pytest.mark.skipif(not window_pair.PROD_LAUNCHER.exists(), reason="local production fixture unavailable")
@pytest.mark.parametrize("slots", [4000, 4200, 4400, 4800])
def test_private_pair_preserves_production_except_named_substitutions(tmp_path, slots):
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest()
              for p in (window_pair.PROD_CONFIG, window_pair.PROD_LAUNCHER)}
    # Keep AF_UNIX paths short even when pytest's temporary root is long.
    result = window_pair.prepare("t", slots=slots, evidence=tmp_path)
    config = json.loads(Path(result.config).read_text())
    prod = json.loads(window_pair.PROD_CONFIG.read_text())
    expected_args = list(prod["args"])
    expected_args[expected_args.index("--expert-helper-slots") + 1] = str(slots)
    assert config["args"] == expected_args
    for key, value in prod.items():
        if key not in {"args", "cwd", "port", "log", "expert_helper_socket", "expert_helper_socket2"}:
            assert config[key] == value
    assert config["port"] == 18080
    assert config["expert_helper_socket"] == result.socket
    assert config["expert_helper_socket2"] == result.socket2
    text = Path(result.launcher).read_text()
    assert "--port 18080" in text and "sport = :18080" in text
    assert f"--device 0 --slots {slots}" in text
    assert "--fill-mib 6400" in text
    assert "prod-mixed.sock" not in text and "prod-mixed2.sock" not in text
    assert "prod-mixed-supervisor.json" not in text
    assert result.parity_failures == []
    assert all(hashlib.sha256(p.read_bytes()).hexdigest() == digest for p, digest in before.items())
    with pytest.raises(ValueError, match="overwrite"):
        window_pair.prepare("t", slots=slots, evidence=tmp_path)


@pytest.mark.skipif(not window_pair.PROD_LAUNCHER.exists(), reason="local production fixture unavailable")
def test_private_pair_fill_budget_override(tmp_path):
    result = window_pair.prepare("tf", slots=4800, fill_mib=6800, evidence=tmp_path)
    text = Path(result.launcher).read_text()
    assert "--fill-mib 6800 --socket2" in text
    assert "--fill-mib 6400 --socket2" not in text
    assert "--device 0 --slots 4800" in text
    assert result.fill_mib == 6800


@pytest.mark.skipif(not window_pair.PROD_LAUNCHER.exists(), reason="local production fixture unavailable")
def test_private_pair_l3_candidate_override(tmp_path):
    engine = window_pair.WORK / "build-mixed-gpu/rpcw-hip-l3/strata"
    helper = window_pair.WORK / "build-mixed-gpu/rpcw-cuda-l3/strata-expert-helper"
    if not (engine.is_file() and helper.is_file()):
        pytest.skip("l3 candidate binaries not built")
    result = window_pair.prepare("tl3", slots=4200, evidence=tmp_path,
                                 engine=engine, helper=helper, rpc_profile=True)
    config = json.loads(Path(result.config).read_text())
    assert config["exe"] == str(engine)
    text = Path(result.launcher).read_text()
    assert f"HELPER={helper}" in text
    assert "rpcw-cuda/strata-expert-helper" not in text
    assert str(engine) in text                       # executable preflight checks the candidate
    assert '"$WORK/build-mixed-gpu/rpcw-hip/strata"' not in text
    assert "export STRATA_RPC_PROFILE=1" in text     # reaches the engine via the server env
    assert 'env -i STRATA_RPC_PROFILE=1 PATH="$PATH"' in text  # helper survives env -i
    assert result.rpc_profile is True
    assert result.engine == str(engine) and result.helper == str(helper)
    assert result.parity_failures == []


@pytest.mark.skipif(not window_pair.PROD_LAUNCHER.exists(), reason="local production fixture unavailable")
def test_l3_candidate_override_refusals(tmp_path):
    prod = json.loads(window_pair.PROD_CONFIG.read_text())
    with pytest.raises(ValueError, match="live production"):
        window_pair.prepare("tr1", evidence=tmp_path, engine=prod["exe"])
    with pytest.raises(ValueError, match="missing or not executable"):
        window_pair.prepare("tr2", evidence=tmp_path,
                            engine=window_pair.WORK / "build-mixed-gpu/rpcw-hip-l3/nonexistent")
    with pytest.raises(ValueError, match="missing or not executable"):
        window_pair.prepare("tr3", evidence=tmp_path, helper=window_pair.WORK / "no-such-helper")
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("kwargs", [{"port": 8009}, {"slots": 4801}, {"slots": 0}, {"port": 80},
                                    {"fill_mib": 7100}, {"fill_mib": 2048}])
def test_unsafe_windows_refused_before_files(tmp_path, kwargs):
    with pytest.raises(ValueError):
        window_pair.prepare("t", evidence=tmp_path, **kwargs)
    assert not list(tmp_path.iterdir())


def test_ambiguous_launcher_field_is_refused():
    with pytest.raises(ValueError):
        window_pair.replace_once("same same", "same", "other")
    with pytest.raises(ValueError):
        window_pair.replace_once("missing", "same", "other")


@pytest.mark.skipif(not window_pair.PROD_LAUNCHER.exists(), reason="local production fixture unavailable")
@pytest.mark.parametrize("enabled", [False, True])
def test_draft_pair(tmp_path, enabled):
    prod = json.loads(window_pair.PROD_CONFIG.read_text())
    before = {p: p.read_bytes() for p in (window_pair.PROD_CONFIG, window_pair.PROD_LAUNCHER)}
    result = window_pair.prepare("td", slots=4200, evidence=tmp_path, draft_helper=enabled)
    config = json.loads(Path(result.config).read_text())
    expected_env = dict(prod["env"], STRATA_DRAFT_HELPER="1" if enabled else "0")
    if enabled:   # l1w5: the draft lane's deadline override (2 s floor is too tight mid-prefill)
        expected_env["STRATA_DRAFT_TIMEOUT_MS"] = "30000"
    assert config["env"] == expected_env
    assert config["args"] == prod["args"]
    assert config["lib_dirs"] == ["/home/user/rocm-gfx906-xnack/lib", "/opt/rocm-6.1.0/lib"]
    launcher = Path(result.launcher).read_text()
    artifact = prod["args"][prod["args"].index("--dflash") + 1]
    assert (f"--dflash {artifact} --dflash-window 262144" in launcher) is enabled
    assert "--socket2" in launcher and "--fill-mib 6400" in launcher
    assert "LD_LIBRARY_PATH=/home/user/rocm-gfx906-xnack/lib:/opt/rocm-6.1.0/lib" in launcher
    assert result.draft_helper is enabled
    assert all(p.read_bytes() == content for p, content in before.items())


@pytest.mark.parametrize("value", [1, "1", [], {}])
def test_bad_draft_type(tmp_path, value):
    with pytest.raises(ValueError, match="Draft override"):
        window_pair.prepare("td", evidence=tmp_path, draft_helper=value)
    assert not list(tmp_path.iterdir())


@pytest.mark.skipif(not window_pair.PROD_CONFIG.exists(), reason="local production fixture unavailable")
def test_bad_draft_artifact(tmp_path):
    prod = json.loads(window_pair.PROD_CONFIG.read_text())
    prod["args"][prod["args"].index("--dflash") + 1] = str(tmp_path / "absent.gguf")
    source = tmp_path / "source.json"
    source.write_text(json.dumps(prod))
    with pytest.raises(ValueError, match="artifact"):
        window_pair.prepare("td", slots=4200, evidence=tmp_path, prod_config=source, draft_helper=True)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["source.json"]


@pytest.mark.skipif(not window_pair.PROD_LAUNCHER.exists(), reason="local production fixture unavailable")
def test_knob_value_replaced_in_engine_argv_only(tmp_path):
    prod = json.loads(window_pair.PROD_CONFIG.read_text())
    if "--pcie-frac" not in prod["args"]:
        pytest.skip("pcie-frac absent from production argv")
    assert prod["args"][prod["args"].index("--pcie-frac") + 1] == "0"
    result = window_pair.prepare("tk", slots=4200, evidence=tmp_path, knob=("pcie-frac", "0.3"))
    config = json.loads(Path(result.config).read_text())
    args = config["args"]
    assert args[args.index("--pcie-frac") + 1] == "0.3"
    assert args.count("--pcie-frac") == 1 and len(args) == len(prod["args"])
    assert result.knob == dict(name="pcie-frac", value="0.3")
    assert "--pcie-frac" not in Path(result.launcher).read_text()  # engine argv only


@pytest.mark.skipif(not window_pair.PROD_LAUNCHER.exists(), reason="local production fixture unavailable")
def test_knob_flag_roundtrip_matches_production(tmp_path):
    prod = json.loads(window_pair.PROD_CONFIG.read_text())
    for i, name in enumerate(window_pair.KNOB_FLAG_ARGS):
        if f"--{name}" in prod["args"]:
            with pytest.raises(ValueError, match="already set"):
                window_pair.prepare(f"tkf{i}a", evidence=tmp_path, knob=(name, "on"))
            result = window_pair.prepare(f"tkf{i}b", evidence=tmp_path, knob=(name, "off"))
            assert f"--{name}" not in json.loads(Path(result.config).read_text())["args"]
        else:
            with pytest.raises(ValueError, match="exactly once to be removable"):
                window_pair.prepare(f"tkf{i}c", evidence=tmp_path, knob=(name, "off"))
            result = window_pair.prepare(f"tkf{i}d", evidence=tmp_path, knob=(name, "on"))
            args = json.loads(Path(result.config).read_text())["args"]
            assert args[-1] == f"--{name}" and args.count(f"--{name}") == 1


@pytest.mark.skipif(not window_pair.PROD_LAUNCHER.exists(), reason="local production fixture unavailable")
def test_knob_refusals(tmp_path):
    with pytest.raises(ValueError, match="approved L6 sweep set"):
        window_pair.prepare("tkr1", evidence=tmp_path, knob=("expert-helper-slots", "100"))
    with pytest.raises(ValueError, match="approved L6 sweep set"):
        window_pair.prepare("tkr2", evidence=tmp_path, knob=("no-such-knob", "1"))
    with pytest.raises(ValueError, match="approved L6 sweep set"):
        window_pair.prepare("tkr3", evidence=tmp_path, knob=("pcie-frac",))
    with pytest.raises(ValueError, match="approved L6 sweep set"):
        window_pair.prepare("tkr4", evidence=tmp_path, knob=("pcie-frac", " "))
    with pytest.raises(ValueError, match="only on/off"):
        window_pair.prepare("tkr5", evidence=tmp_path, knob=("prompt-cache-spread", "0.5"))
    prod = json.loads(window_pair.PROD_CONFIG.read_text())
    if "--pcie-frac" in prod["args"]:
        with pytest.raises(ValueError, match="new-value"):
            window_pair.prepare("tkr6", evidence=tmp_path, knob=("pcie-frac", "on"))
    absent = [n for n in window_pair.KNOB_VALUE_ARGS if f"--{n}" not in prod["args"]]
    if absent:
        with pytest.raises(ValueError, match="exactly once in production"):
            window_pair.prepare("tkr7", evidence=tmp_path, knob=(absent[0], "1"))
    assert not list(tmp_path.iterdir())


@pytest.mark.skipif(not window_pair.PROD_LAUNCHER.exists(), reason="local production fixture unavailable")
def test_prefill_timing_export_engine_side_only(tmp_path):
    result = window_pair.prepare("tpt", slots=4200, evidence=tmp_path, prefill_timing=True)
    text = Path(result.launcher).read_text()
    assert "export STRATA_PREFILL_TIMING=1" in text
    assert "env -i STRATA_PREFILL_TIMING" not in text   # engine-side only: no env -i for the helper
    assert "STRATA_RPC_PROFILE" not in text
    assert result.prefill_timing is True


@pytest.mark.skipif(not window_pair.PROD_LAUNCHER.exists(), reason="local production fixture unavailable")
def test_rpc_profile_and_prefill_timing_combine(tmp_path):
    result = window_pair.prepare("tpt2", slots=4200, evidence=tmp_path,
                                  rpc_profile=True, prefill_timing=True)
    text = Path(result.launcher).read_text()
    assert "export STRATA_RPC_PROFILE=1 STRATA_PREFILL_TIMING=1" in text
    assert 'env -i STRATA_RPC_PROFILE=1 PATH="$PATH"' in text
    assert result.rpc_profile is True and result.prefill_timing is True


@pytest.mark.skipif(not window_pair.PROD_LAUNCHER.exists(), reason="local production fixture unavailable")
def test_l2_helper_streams_env_injection_helper_side_only(tmp_path):
    result = window_pair.prepare("tl2", slots=4200, evidence=tmp_path, helper_streams=True)
    text = Path(result.launcher).read_text()
    assert 'env -i STRATA_HELPER_STREAMS=1 PATH="$PATH"' in text
    assert "export STRATA_HELPER_STREAMS" not in text   # helper-side only: no engine export
    assert result.helper_streams is True
    assert result.parity_failures == []


@pytest.mark.skipif(not window_pair.PROD_LAUNCHER.exists(), reason="local production fixture unavailable")
def test_l2_helper_streams_default_is_unset(tmp_path):
    result = window_pair.prepare("tl2o", slots=4200, evidence=tmp_path)
    text = Path(result.launcher).read_text()
    assert "STRATA_HELPER_STREAMS" not in text
    assert result.helper_streams is False


@pytest.mark.parametrize("value", [1, "1", [], {}])
def test_bad_helper_streams_type(tmp_path, value):
    with pytest.raises(ValueError, match="Helper-stream override"):
        window_pair.prepare("tl2b", evidence=tmp_path, helper_streams=value)
    assert not list(tmp_path.iterdir())


@pytest.mark.skipif(not window_pair.PROD_LAUNCHER.exists(), reason="local production fixture unavailable")
def test_draft_tap_bf16_env_engine_side_only(tmp_path):
    result = window_pair.prepare("tb1", slots=4200, evidence=tmp_path,
                                  draft_helper=True, draft_tap_bf16=True)
    config = json.loads(Path(result.config).read_text())
    assert config["env"]["STRATA_DRAFT_TAP_BF16"] == "1"
    # Engine-side only: the env rides the config's env (the server applies it to the
    # engine); the helper follows the frame kind, so the launcher never carries it.
    assert "STRATA_DRAFT_TAP_BF16" not in Path(result.launcher).read_text()
    assert result.draft_tap_bf16 is True


@pytest.mark.skipif(not window_pair.PROD_LAUNCHER.exists(), reason="local production fixture unavailable")
def test_draft_tap_bf16_default_absent(tmp_path):
    result = window_pair.prepare("tb2", slots=4200, evidence=tmp_path, draft_helper=True)
    assert "STRATA_DRAFT_TAP_BF16" not in json.loads(Path(result.config).read_text())["env"]
    assert result.draft_tap_bf16 is False


def test_draft_tap_bf16_needs_draft_lane(tmp_path):
    with pytest.raises(ValueError, match="needs the remote draft lane"):
        window_pair.prepare("tb3", evidence=tmp_path, draft_tap_bf16=True)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("value", [1, "1", [], {}])
def test_bad_draft_tap_bf16_type(tmp_path, value):
    with pytest.raises(ValueError, match="bf16 override"):
        window_pair.prepare("tb4", evidence=tmp_path, draft_helper=True, draft_tap_bf16=value)
    assert not list(tmp_path.iterdir())
