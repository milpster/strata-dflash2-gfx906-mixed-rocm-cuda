"""serve/test_dflash_engine_args.py - our --dflash serving flags survive engine_args() shaping (no GPU, no pack, no server).

The upstream-integration review (gate 1, risk 2) names the hazard: serve/server.py's engine_args() runs the config's
arguments through strata_control/drafting.py and strata_control/draft_layer.py.  draft_layer's MTP_FLAGS set contains
`--spec-min-p`, the flag our --dflash window gate reads (src/program/generate.cpp), and drafting.apply() rewrites the
args for the `"drafting"` key.  A config that serves DFlash must keep every drafter flag it chose, in every combination
of the two keys: with a `"drafting"` key, without one, with `"mtp": false`, and with the plain config.

    python -m unittest serve.test_dflash_engine_args -v
"""
from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from serve import server  # noqa: E402

# Our validated deployment shape (the L22/L23 profile): the --dflash drafter on two cards with the layer split.
DFLASH_ARGS = [
    "--native", "/synth/model.gguf", "--pack", "/synth/data/packs/SWIFT",
    "--max-context", "262144", "--gpu", "0,1", "--kv", "int8",
    "--spec", "4", "--spec-min-p", "0.7",
    "--dflash", "/synth/data/dflash.gguf", "--dflash-block", "3",
    "--dflash-vocab", "/synth/data/vocab.bin", "--dflash-head", "/synth/data/head.gguf",
    "--dflash-window", "262144", "--dflash-mask-token", "248077",
]
#: every flag the serving path needs to arrive at the engine, with the value that must travel with it
OUR_FLAGS = {
    "--dflash": "/synth/data/dflash.gguf",
    "--dflash-block": "3",
    "--dflash-vocab": "/synth/data/vocab.bin",
    "--dflash-head": "/synth/data/head.gguf",
    "--dflash-window": "262144",
    "--dflash-mask-token": "248077",
    "--spec": "4",
    "--spec-min-p": "0.7",
}


def shaped(cfg: dict) -> list[str]:
    """engine_args() as a server start calls it: no GPU, no sysfs probe (the card class is pinned to 'other', the
    class that suggests the MTP, so the chooser's own path is the one the config takes), nothing printed."""
    with mock.patch.object(server, "drafting_card_class", lambda: "other"), \
            mock.patch.object(server.drafting, "suggest", lambda klass: "mtp"), \
            mock.patch.object(server.drafting, "remember_note", lambda note: None), \
            mock.patch.object(server.drafting, "take_note", lambda: ""), \
            contextlib.redirect_stdout(io.StringIO()):
        return server.engine_args(dict(cfg))


def value_of(args: list[str], flag: str) -> str | None:
    """The value that follows a flag (None for a valueless flag): a scan, so a valueless flag can never misalign a zip."""
    if flag not in args:
        return None
    i = args.index(flag)
    v = args[i + 1] if i + 1 < len(args) else ""
    return None if v.startswith("--") else v


class DFlashArgsSurviveShaping(unittest.TestCase):
    def base(self) -> dict:
        return {"args": list(DFLASH_ARGS), "model_name": "swift-1.5-iq3_xxs",
                "gpu": [0, 1], "layer_split": "32"}

    def assert_ours_intact(self, args: list[str], what: str) -> None:
        for flag, value in OUR_FLAGS.items():
            self.assertIn(flag, args, f"{what}: {flag} left the engine arguments")
            self.assertEqual(value_of(args, flag), value, f"{what}: {flag} lost its value")
        # The --dflash block keeps the order the config wrote it in.  --spec / --spec-min-p may arrive at the end:
        # the guard in engine_args() re-appends --spec-min-p after the draft layer's strip, and the engine reads its
        # arguments by name, so only the block's own internal order is a property worth pinning.
        block = [f for f in DFLASH_ARGS if f.startswith("--dflash")]
        order = [args.index(f) for f in block]
        self.assertEqual(order, sorted(order), f"{what}: the --dflash block was reordered")

    def test_no_drafting_key(self):
        """The plain config (no "drafting" key, no "mtp" key) reaches the engine with every drafter flag."""
        self.assert_ours_intact(shaped(self.base()), "no drafting key")

    def test_drafting_key_mtp_and_auto(self):
        """A "drafting" key runs the chooser; it adds its own flags and never takes ours away."""
        for choice in ("mtp", "auto", "dflash2"):
            cfg = self.base()
            cfg["drafting"] = choice
            self.assert_ours_intact(shaped(cfg), f'key "drafting": {choice!r}')

    def test_drafting_key_off_keeps_the_drafter(self):
        """`"drafting": "off"` is the draft layer's switch (it sets "mtp": false): our drafter's flags stay, and the
        --spec-min-p our window gate reads is not taken with the draft layer."""
        cfg = self.base()
        cfg["drafting"] = "off"
        args = shaped(cfg)
        self.assert_ours_intact(args, 'key "drafting": "off"')
        self.assertNotIn("--mtp", args, 'the draft layer itself must go: "drafting": "off"')

    def test_mtp_false_keeps_the_drafter_flags(self):
        """`"mtp": false` strips the draft layer's own flags; --spec-min-p survives beside --dflash."""
        cfg = self.base()
        cfg["mtp"] = False
        args = shaped(cfg)
        self.assert_ours_intact(args, '"mtp": false')
        self.assertNotIn("--mtp", args, '"mtp": false leaves no draft layer')

    def test_without_dflash_the_switch_keeps_its_own_meaning(self):
        """A config with no --dflash keeps upstream's behavior exactly: "mtp": false strips --spec-min-p with the
        draft layer (the default path is untouched by the guard above)."""
        cfg = self.base()
        cfg["args"] = [a for a in cfg["args"] if not a.startswith("--dflash")]
        cfg["mtp"] = False
        args = shaped(cfg)
        self.assertNotIn("--mtp", args)
        self.assertNotIn("--spec-min-p", args, '"mtp": false without --dflash keeps draft_layer.strip as upstream wrote it')

    def test_layer_split_still_added(self):
        """The guard changed nothing about the split: a dual-GPU config still gets --layer-split from the server's own rule."""
        args = shaped(self.base())
        self.assertIn("--layer-split", args)
        self.assertEqual(value_of(args, "--layer-split"), "32")
        self.assert_ours_intact(args, "with the split added")


if __name__ == "__main__":
    unittest.main()
