#!/usr/bin/env python3
"""
Guards the apply-env "reload picks up a new plan" wiring.

The whole point of load-plan.sh is that it runs on every container attach
(VS Code "Reload Window"), so a freshly-promoted plan takes effect without a
full rebuild. That is only SAFE if a couple of invariants hold — this test
locks them in so a future edit can't silently break them:

  1. The apply template wires postAttachCommand to load-plan.sh (so reload
     recompiles the plan at all), and still keeps the firewall on postCreate.
  2. load-plan.sh does NOT run the firewall (init-firewall / sudo): re-applying
     iptables on every attach risks duplicate/failed rules.
  3. load-plan.sh fails CLOSED — on a missing/invalid plan it removes the live
     allowlist (rm of APPLY_COMPILED_PATH) rather than leaving the stale one in
     place, so apply comes up locked, not silently enforcing the old plan.

Run: python3 tests/test_apply_reload_wiring.py  (or via tests/run.sh)
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
APPLY_DC = REPO_ROOT / ".devcontainer/apply"
TEMPLATE = APPLY_DC / "devcontainer.template.json"
LOAD_PLAN = APPLY_DC / "load-plan.sh"
POST_CREATE = APPLY_DC / "post-create.sh"


class ApplyReloadWiringTest(unittest.TestCase):
    def setUp(self):
        self.template = json.loads(TEMPLATE.read_text())
        self.load_plan = LOAD_PLAN.read_text()
        self.post_create = POST_CREATE.read_text()
        # Code only — drop comment lines so prose mentioning "sudo"/"firewall"
        # in explanatory comments doesn't trip the safety assertions below.
        self.load_plan_code = "\n".join(
            ln for ln in self.load_plan.splitlines() if not ln.lstrip().startswith("#")
        )

    def test_post_attach_recompiles_plan(self):
        attach = self.template.get("postAttachCommand", "")
        self.assertIn("load-plan.sh", attach,
                      "postAttachCommand must run load-plan.sh so Reload Window picks up a new plan")

    def test_firewall_runs_on_create_not_attach(self):
        # Firewall belongs to the one-time create path...
        self.assertIn("init-firewall", self.post_create)
        # ...and must NOT be in the per-attach path.
        self.assertNotIn("init-firewall", self.load_plan_code,
                         "load-plan.sh runs on every attach; re-applying the firewall there is unsafe")
        self.assertNotRegex(self.load_plan_code, r"\bsudo\b",
                            "load-plan.sh must not need sudo — it runs on every attach")

    def test_load_plan_fails_closed(self):
        # On a missing or invalid plan it must remove the live allowlist so the
        # PreToolUse hook blocks everything, rather than enforce a stale plan.
        rm_count = len(re.findall(r"rm -f \"\$\{APPLY_COMPILED_PATH\}\"", self.load_plan))
        self.assertGreaterEqual(rm_count, 2,
                                "load-plan.sh must rm the compiled allowlist on BOTH the "
                                "missing-plan and failed-validation paths (fail closed)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
