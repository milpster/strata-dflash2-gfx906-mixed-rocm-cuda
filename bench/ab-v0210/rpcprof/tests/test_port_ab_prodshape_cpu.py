#!/usr/bin/env python3
"""CPU-only tests for the W3p PROD-SHAPE TG leg. No GPU, no endpoint, no boot.

What this leg exists for: the thinking-off pp239k leg asks for 1,024 tokens but the model ends
its reply at EOS after ~518, so its TG rate never clears the 1,000-generated floor. Prod serves
with thinking ON (reasoning_budget_tokens 2000, reasoning_effort high), and with a budget the
model works through, so generation runs to the cap. The leg reproduces that serving shape on a
SEPARATE window, with a SEPARATE config variant and a SEPARATE request template, so no claim
config, pin, record or shape can be touched by it.

Everything here is static: configs are derived in a scratch out-dir, nothing boots. The
measurement tree out/port-ab-20261123 holds completed evidence and is only READ.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
import sys
import unittest

RPC = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RPC))
import port_ab_configs as PC  # noqa: E402
import port_ab_contract as C  # noqa: E402
import port_ab_legs as LEGS  # noqa: E402

SCRATCH = RPC / "out" / "tests-tmp"
MEASURED = PC.OUT_DEFAULT
CLAIM_TEMPLATE_SHA = "1367e0c041b3e7e6abceef27587f9ffbb8ff1d8b2ea4cc7d0141d9e945a18abd"


def derive(arm="B", window="W3p", rep=1, out=None, boot="pp"):
    return PC.derive(arm, window=window, rep=rep, out=out or (SCRATCH / "w3p"), boot=boot)


class ProdShapeWindow(unittest.TestCase):
    """The window identity: labelled, one arm, its own boot keys, its own template."""

    def test_window_exists_and_is_marked_not_poolable(self):
        self.assertIn("W3p", C.WINDOWS)
        self.assertEqual(C.config_variant("W3p"), "prodshape")
        self.assertFalse(C.WINDOWS["W3p"].get("poolable_with_claim_legs"))
        self.assertIn("NOT A CLAIM LEG", C.WINDOWS["W3p"]["claim"])
        self.assertIn("not poolable", C.WINDOWS["W3p"]["measurement_caveat"])

    def test_boots_arm_b_only_with_its_own_keys(self):
        self.assertEqual(C.window_arms("W3p"), ("B",))
        self.assertEqual(("A", "B"), C.window_arms("W3"))
        self.assertEqual(C.claim_pairs("W3p"), (("B", 1), ("B", 2)))
        keys = [r["arm"] + str(r["rep"]) for r in LEGS.schedule("W3p")]
        self.assertEqual(["B0", "B1", "B2"], keys)
        for row in LEGS.schedule("W3p"):
            self.assertTrue(PC.boot_key("W3p", row["arm"], row["rep"], row["boot"])
                            .startswith("W3p-"))

    def test_leg_is_tg_on_the_pp239k_corpus_with_the_same_cap(self):
        leg = C.LEGS["pp239k-prodshape"]
        self.assertEqual(leg["kind"], "tg")
        self.assertEqual(leg["corpus"], "pp239k")
        self.assertEqual(C.kind_for("pp239k-prodshape", "W3p"), "tg")
        self.assertEqual(LEGS.leg_cap("pp239k-prodshape", "W3p"), 1024)
        self.assertEqual(C.corpus_shape("pp239k-prodshape"), "pp239k")
        self.assertEqual(C.SHAPES["pp239k"]["bytes"], 323200)

    def test_claims_tg_true_and_the_floor_applies(self):
        self.assertTrue(C.claims_tg("pp239k-prodshape", "W3p"))
        self.assertGreaterEqual(LEGS.leg_cap("pp239k-prodshape", "W3p"),
                                C.TG_CLAIM_MIN_GENERATED)
        self.assertEqual(C.TG_CLAIM_MIN_GENERATED, 1000)

    def test_the_claim_windows_are_untouched(self):
        for window in ("W1", "W1d", "W1f", "W2", "W3", "W4"):
            self.assertIsNone(C.config_variant(window), window)
            self.assertEqual(C.request_template(window), "claim", window)
            self.assertEqual(C.window_arms(window), ("A", "B"), window)
            self.assertTrue(C.WINDOWS[window].get("poolable_with_claim_legs", True), window)
        self.assertEqual(C.config_variant("W3p"), "prodshape")
        self.assertEqual(C.request_template("W3p"), "prodshape")


class ProdShapeConfig(unittest.TestCase):
    """The variant: everything identical to arm B claim config but the two shape keys."""

    def setUp(self):
        self.out = SCRATCH / ("w3p-" + self._testMethodName)
        self.addCleanup(lambda: shutil.rmtree(self.out, ignore_errors=True))

    def test_variant_overrides_budget_and_keeps_the_tuple_effort(self):
        self.assertEqual(
            {k: v for k, v in PC.VARIANTS["prodshape"].items() if k != "note"},
            {"reasoning_budget_tokens": 2000, "reasoning_effort": "high"})
        # the promoted prod config serves exactly these two values
        self.assertEqual(PC.VARIANTS["prodshape"]["reasoning_budget_tokens"], 2000)
        self.assertEqual(PC.VARIANTS["prodshape"]["reasoning_effort"], PC.TUPLE["reasoning_effort"])

    def test_config_delta_vs_the_claim_arm_is_one_setting(self):
        claim, _, _ = derive("B", window="W3", out=self.out)
        prod, _, _ = derive("B", window="W3p", out=self.out)
        a = PC.settings_only(PC.blank_declared(claim, "test"))
        b = PC.settings_only(PC.blank_declared(prod, "test"))
        a.pop("trial_note")
        b.pop("trial_note")
        diffs = [k for k in sorted(set(a) | set(b))
                 if json.dumps(a.get(k), sort_keys=True) != json.dumps(b.get(k), sort_keys=True)]
        self.assertEqual(["reasoning_budget_tokens"], diffs)
        self.assertEqual(a["args"], b["args"], "engine argv moved")
        self.assertEqual(a["env"], b["env"], "engine env moved")
        self.assertEqual(claim["exe"], prod["exe"])
        self.assertEqual(claim["cwd"], prod["cwd"])
        self.assertEqual(claim["port"], prod["port"])
        self.assertEqual(claim["model_name"], prod["model_name"])
        self.assertEqual(claim["reasoning_effort"], prod["reasoning_effort"])
        self.assertEqual(claim["layer_split"], prod["layer_split"])
        self.assertEqual(claim["expert_helper_slots"], prod["expert_helper_slots"])

    def test_variant_config_passes_its_check_and_the_claim_window_rejects_a_leak(self):
        cfg, deltas, _ = derive("B", window="W3p", out=self.out)
        self.assertEqual(cfg["reasoning_budget_tokens"], 2000)
        self.assertEqual([p for p in PC.check_tuple(cfg, "B", window="W3p")], [])
        kinds = {(d["field"], d["kind"]) for d in deltas}
        self.assertIn(("reasoning_budget_tokens", "variant"), kinds)
        leaked, _, _ = derive("B", window="W3", out=self.out)
        leaked["reasoning_budget_tokens"] = 2000
        self.assertTrue([p for p in PC.check_tuple(leaked, "B", window="W3")
                         if "reasoning_budget_tokens" in p])
        _, claim_deltas, _ = derive("B", window="W3", out=self.out)
        self.assertFalse([d for d in claim_deltas if d.get("kind") == "variant"])

    def test_one_arm_window_refuses_the_other_arm(self):
        with self.assertRaises(SystemExit):
            PC.derive("A", window="W3p", rep=1, out=self.out, boot="pp")


class ProdShapeTemplate(unittest.TestCase):
    """Thinking on, temperature 0, stream false, and no effort override."""

    def test_settings_are_the_prod_shape(self):
        self.assertEqual(C.PRODSHAPE_TEMPLATE,
                         {"temperature": 0, "enable_thinking": True, "stream": False})
        self.assertNotIn("reasoning_effort", C.PRODSHAPE_TEMPLATE,
                         "the config own high must apply, never a request override")
        self.assertNotIn("messages", C.PRODSHAPE_TEMPLATE)
        self.assertNotIn("max_tokens", C.PRODSHAPE_TEMPLATE)

    def test_claim_template_is_untouched(self):
        self.assertEqual(C.REQUEST_TEMPLATE,
                         {"temperature": 0, "enable_thinking": False,
                          "reasoning_effort": "low", "stream": False})

    def test_two_artifacts_two_paths(self):
        files = {n: s["file"] for n, s in C.REQUEST_TEMPLATES.items()}
        self.assertEqual(files["claim"], "request-template.json")
        self.assertEqual(files["prodshape"], "request-template-prodshape.json")
        self.assertNotEqual(files["claim"], files["prodshape"])
        claim = MEASURED / files["claim"]
        self.assertTrue(claim.is_file())
        self.assertEqual(hashlib.sha256(claim.read_bytes()).hexdigest(), CLAIM_TEMPLATE_SHA)

    def test_template_path_follows_the_window(self):
        self.assertEqual(LEGS.template_path(MEASURED, "W3").name, "request-template.json")
        self.assertEqual(LEGS.template_path(MEASURED, "W3p").name,
                         "request-template-prodshape.json")
        # the default is the claim template: no existing caller changes shape by omission
        self.assertEqual(LEGS.template_path(MEASURED).name, "request-template.json")


class ProdShapeCommand(unittest.TestCase):
    """The command proves the shape before it is fired."""

    def test_command_pins_the_prod_shape_template_and_cap(self):
        _, _, cmds = LEGS.commands(MEASURED, "W3p", "B", 1, ["pp239k-prodshape"], boot="pp")
        cmd = cmds[0]["cmd"]
        self.assertEqual(cmds[0]["shape"], "pp239k-prodshape")
        self.assertEqual(cmd[cmd.index("--kind") + 1], "tg")
        self.assertEqual(cmd[cmd.index("--max-tokens") + 1], "1024")
        self.assertEqual(cmd[cmd.index("--ctx") + 1], "323200")
        tpl = cmd[cmd.index("--request-template") + 1]
        self.assertTrue(tpl.endswith("request-template-prodshape.json"), tpl)
        sha = cmd[cmd.index("--request-template-sha256") + 1]
        self.assertEqual(sha, hashlib.sha256(pathlib.Path(tpl).read_bytes()).hexdigest())
        self.assertEqual(cmd[cmd.index("--leg") + 1], "pp239k-prodshape")
        self.assertEqual(cmd[cmd.index("--prompt-file") + 1],
                         str(MEASURED / "corpus" / "pp239k.ctx323200.bin"))

    def test_the_claim_leg_still_uses_the_claim_template(self):
        _, _, cmds = LEGS.commands(MEASURED, "W3", "B", 1, ["pp239k"], boot="pp")
        cmd = cmds[0]["cmd"]
        self.assertTrue(cmd[cmd.index("--request-template") + 1]
                        .endswith("request-template.json"))

    def test_runbook_says_it_is_not_the_claim_shape(self):
        text = LEGS.runbook(MEASURED, "W3p")
        self.assertIn("CONFIG VARIANT prodshape", text)
        self.assertIn("never pool with a claim leg", text)
        self.assertIn("W3p-B-pp-r1", text)
        self.assertIn("B only", text)
        self.assertNotIn("W3p-A-", text)
        # an existing window runbook must not gain a prod-shape sentence
        self.assertNotIn("CONFIG VARIANT", LEGS.runbook(MEASURED, "W3"))


if __name__ == "__main__":
    unittest.main()
