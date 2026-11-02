"""R2 (opt-in CUDA expert helper, docs/MIXED_GPU_HELPER.md): the server's engine arguments for the helper's
AF_UNIX channel.  `serve/server.py:expert_helper_args` is the whole server-side surface - the helper process is
started by hand, the engine never spawns it.  The contract this test pins: without "expert_helper_socket" - the
default, every shipped config - the engine's arguments are byte-for-byte the config's arguments; with it, exactly
the helper flags, and an engine option already in the args is never doubled.

`expert_helper_args` returns a DELTA (only the new flags, the way parallel_args/pool_cores_args do) because the
server composes it with `args += expert_helper_args(cfg, args)` (serve/server.py:3132).  The R2 gate's M1 finding
is exactly the mutation of that rule: a function returning the accumulated list under `+=` sends the engine every
argument TWICE.  So the default-path check below is done at ARGV LEVEL - replaying the real composition - and not
only by calling the function.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SPEC = importlib.util.spec_from_file_location("strata_server_for_helper_test", ROOT / "serve" / "server.py")
server = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = server
SPEC.loader.exec_module(server)


def helper(cfg: dict, base: list[str] | None = None) -> list[str]:
    """The function's own contract: the delta it wants added, nothing of the caller's list."""
    return server.expert_helper_args(cfg, list(base or []))


def compose(cfg: dict, args: list[str]) -> list[str]:
    """The server's real composition, verbatim from serve/server.py:3132 (`args += expert_helper_args(cfg, args)`)."""
    return args + server.expert_helper_args(cfg, args)


class DeltaContract(unittest.TestCase):
    """The corrected contract (R2 gate M1): the function returns ONLY the new flags."""

    def test_no_key_returns_an_empty_delta(self):
        base = ["--model", "/pack/model.gguf", "--ctx", "131072"]
        self.assertEqual(helper({}, base), [])
        self.assertEqual(helper({"experts": "cpu", "parallel": 4}, base), [])

    def test_empty_or_blank_key_returns_an_empty_delta(self):
        base = ["--model", "/pack/model.gguf"]
        for value in ("", "   ", None, False, 0, [], {}):
            self.assertEqual(helper({"expert_helper_socket": value}, base), [])

    def test_the_delta_never_carries_the_caller_s_arguments(self):
        base = ["--model", "/pack/model.gguf", "--ctx", "131072", "--lora", "/a/lora.gguf"]
        out = helper({"expert_helper_socket": "/tmp/h.sock"}, base)
        for arg in base:
            self.assertNotIn(arg, out)


class DefaultPathArgvLevel(unittest.TestCase):
    """M1's byte-identity proof at argv level: replaying `args += expert_helper_args(cfg, args)`.

    The mutation this pins out was measured by the gate: 6 arguments became 12 with no key and 14
    with the key.  Reproducing that replay here is the point of the test, not the function call."""

    def test_no_key_the_line_is_the_config_s_line_exactly(self):
        base = ["--model", "/pack/model.gguf", "--ctx", "131072", "--lora", "/a/lora.gguf"]
        for cfg in ({}, {"ctx": 131072}, {"experts": "cpu", "parallel": 4}):
            out = compose(cfg, list(base))
            self.assertEqual(out, base, f"cfg={cfg}")
            self.assertEqual(len(out), len(base), f"cfg={cfg}: the argv DOUBLED")

    def test_the_gate_s_measured_replay_no_longer_doubles(self):
        # the gate's own line: --model X --ctx 131072 --lora A
        base = ["--model", "X", "--ctx", "131072", "--lora", "A"]
        self.assertEqual(compose({}, list(base)), base)                      # was 12 entries
        self.assertEqual(len(compose({"expert_helper_socket": "/run/u/h.sock"}, list(base))), 8)   # was 14


class OptInFlags(unittest.TestCase):
    def test_socket_alone(self):
        out = helper({"expert_helper_socket": "/run/u/strata-helper.sock"})
        self.assertEqual(out, ["--expert-helper-socket", "/run/u/strata-helper.sock"])

    def test_socket_is_stripped(self):
        out = helper({"expert_helper_socket": "  /tmp/h.sock  "})
        self.assertEqual(out, ["--expert-helper-socket", "/tmp/h.sock"])

    def test_the_optional_numbers_follow(self):
        out = helper({
            "expert_helper_socket": "/tmp/h.sock",
            "expert_helper_layers": 40,
            "expert_helper_slots": 2,
            "expert_helper_reserve_mib": 3072,
            "expert_helper_timeout": 900,
        })
        self.assertEqual(out, [
            "--expert-helper-socket", "/tmp/h.sock",
            "--expert-helper-layers", "40",
            "--expert-helper-slots", "2",
            "--expert-helper-reserve-mib", "3072",
            "--expert-helper-timeout-ms", "900",
        ])

    def test_a_number_the_config_cannot_give_is_left_out(self):
        # a bool is not a number, a negative is not a value, a string is not a whole number
        for bad in (True, False, -1, "2", 2.5, None):
            out = helper({"expert_helper_socket": "/tmp/h.sock", "expert_helper_slots": bad})
            self.assertEqual(out, ["--expert-helper-socket", "/tmp/h.sock"], f"slots={bad!r}")
            self.assertNotIn("--expert-helper-slots", out)

    def test_zero_is_a_value(self):
        # "expert_helper_reserve_mib": 0 asks the helper to reserve nothing: passed through
        out = helper({"expert_helper_socket": "/tmp/h.sock", "expert_helper_reserve_mib": 0})
        self.assertEqual(out, ["--expert-helper-socket", "/tmp/h.sock", "--expert-helper-reserve-mib", "0"])


class OptInArgvLevel(unittest.TestCase):
    """With the key: the composed line grows by EXACTLY the helper's flags - two per setting, no
    flag twice, the config's own arguments in their own place."""

    def test_with_the_key_exactly_two_more_entries(self):
        base = ["--model", "/pack/model.gguf", "--ctx", "131072", "--lora", "/a/lora.gguf"]
        out = compose({"expert_helper_socket": "/tmp/h.sock"}, list(base))
        self.assertEqual(len(out), len(base) + 2)
        self.assertEqual(out[:len(base)], base)
        self.assertEqual(out[len(base):], ["--expert-helper-socket", "/tmp/h.sock"])

    def test_every_number_key_adds_exactly_its_pair(self):
        base = ["--model", "/pack/model.gguf", "--ctx", "131072"]
        out = compose({
            "expert_helper_socket": "/tmp/h.sock",
            "expert_helper_layers": 40,
            "expert_helper_slots": 2,
            "expert_helper_reserve_mib": 3072,
            "expert_helper_timeout": 900,
        }, list(base))
        self.assertEqual(len(out), len(base) + 10)
        self.assertEqual(out[:len(base)], base)
        for flag, value in (("--expert-helper-layers", "40"), ("--expert-helper-slots", "2"),
                            ("--expert-helper-reserve-mib", "3072"), ("--expert-helper-timeout-ms", "900")):
            self.assertEqual(out.count(flag), 1, flag)
            self.assertEqual(out.count(value), 1, value)

    def test_no_flag_of_the_config_s_line_is_doubled(self):
        base = ["--model", "/pack/model.gguf", "--ctx", "131072", "--lora", "/a/lora.gguf",
                "--lora", "/b/lora.gguf", "--gpu", "0,1"]
        out = compose({"expert_helper_socket": "/tmp/h.sock", "expert_helper_slots": 2}, list(base))
        self.assertEqual(len(out), len(base) + 4)
        for arg in base:
            self.assertEqual(out.count(arg), base.count(arg), arg)


class EngineOptionsWin(unittest.TestCase):
    def test_an_option_already_in_the_args_is_not_doubled(self):
        base = ["--expert-helper-socket", "/hand/picked.sock", "--expert-helper-slots", "3"]
        out = helper({"expert_helper_socket": "/cfg/other.sock", "expert_helper_slots": 8}, base)
        self.assertEqual(out, [])
        self.assertEqual(compose({"expert_helper_socket": "/cfg/other.sock",
                                  "expert_helper_slots": 8}, list(base)), base)

    def test_one_option_present_does_not_block_the_others(self):
        base = ["--expert-helper-slots", "3"]
        out = helper({"expert_helper_socket": "/tmp/h.sock", "expert_helper_slots": 8,
                      "expert_helper_reserve_mib": 1024}, base)
        self.assertEqual(out, [
            "--expert-helper-socket", "/tmp/h.sock",
            "--expert-helper-reserve-mib", "1024",
        ])
        composed = compose({"expert_helper_socket": "/tmp/h.sock", "expert_helper_slots": 8,
                            "expert_helper_reserve_mib": 1024}, list(base))
        self.assertEqual(composed, [
            "--expert-helper-slots", "3",
            "--expert-helper-socket", "/tmp/h.sock",
            "--expert-helper-reserve-mib", "1024",
        ])
        self.assertEqual(composed.count("--expert-helper-slots"), 1)


class ArgsAreAppendedNotRewritten(unittest.TestCase):
    def test_the_caller_s_list_is_not_mutated(self):
        base = ["--model", "/pack/model.gguf"]
        out = helper({"expert_helper_socket": "/tmp/h.sock"}, base)
        self.assertEqual(base, ["--model", "/pack/model.gguf"])
        self.assertEqual(out, ["--expert-helper-socket", "/tmp/h.sock"])


class FullEngineLine(unittest.TestCase):
    """The helper's flags are appended to the config's engine line at serve/server.py's last
    composition step (`args += expert_helper_args(cfg, args)`), so they sit where the other engine
    opt-ins sit.  The drafting/GPU composition above it is server.py's own business; this is the
    helper's part of the line."""

    def test_the_flags_land_on_the_config_s_line(self):
        base = ["--model", "/pack/model.gguf", "--ctx", "131072", "--split32", "--gpu", "0,1"]
        cfg = {"expert_helper_socket": "/tmp/h.sock", "expert_helper_reserve_mib": 3072}
        line = compose(cfg, list(base))
        self.assertEqual(line[:len(base)], base)
        self.assertEqual(line[len(base):],
                         ["--expert-helper-socket", "/tmp/h.sock", "--expert-helper-reserve-mib", "3072"])

    def test_the_same_config_without_the_key_leaves_the_line_as_it_was(self):
        base = ["--model", "/pack/model.gguf", "--ctx", "131072", "--split32", "--gpu", "0,1"]
        self.assertEqual(compose({"ctx": 131072}, list(base)), base)
        self.assertNotIn("--expert-helper-socket", compose({"ctx": 131072}, list(base)))


class TwoChannelSocket(unittest.TestCase):
    """Opt-in SECOND channel ("expert_helper_socket2": "<path>", the helper's --socket2): the prompt
    tier's own socket.  Absent - the default, every shipped config - the engine keeps one channel and
    the delta is exactly what it was before this option."""

    def test_the_second_key_adds_the_second_flag(self):
        out = helper({"expert_helper_socket": "/tmp/h.sock",
                      "expert_helper_socket2": "/tmp/h-prompt.sock"})
        self.assertEqual(out, ["--expert-helper-socket", "/tmp/h.sock",
                               "--expert-helper-socket2", "/tmp/h-prompt.sock"])

    def test_no_second_key_means_no_second_flag(self):
        out = helper({"expert_helper_socket": "/tmp/h.sock"})
        self.assertEqual(out, ["--expert-helper-socket", "/tmp/h.sock"])
        self.assertNotIn("--expert-helper-socket2", out)

    def test_the_second_path_must_differ_from_the_first(self):
        # the helper binds TWO distinct sockets; a repeated path is a bind failure at startup,
        # so the server never sends it
        out = helper({"expert_helper_socket": "/tmp/h.sock", "expert_helper_socket2": "/tmp/h.sock"})
        self.assertEqual(out, ["--expert-helper-socket", "/tmp/h.sock"])

    def test_the_second_key_alone_is_not_a_channel(self):
        self.assertEqual(helper({"expert_helper_socket2": "/tmp/h-prompt.sock"}), [])

    def test_a_second_flag_already_in_the_args_is_not_doubled(self):
        base = ["--expert-helper-socket", "/hand/1.sock", "--expert-helper-socket2", "/hand/2.sock"]
        out = helper({"expert_helper_socket": "/cfg/1.sock",
                      "expert_helper_socket2": "/cfg/2.sock"}, base)
        self.assertEqual(out, [])
        line = compose({"expert_helper_socket": "/cfg/1.sock",
                        "expert_helper_socket2": "/cfg/2.sock"}, list(base))
        self.assertEqual(line, base)


if __name__ == "__main__":
    unittest.main()
