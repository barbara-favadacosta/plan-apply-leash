#!/usr/bin/env python3
"""
Integration test for gen_devcontainer.py's research-access wiring.

The load-bearing invariant: the APPLY target gets NO platform credentials, ever
— no mounts, no Features, no platform env — even when research-access.yaml is
fully populated. RESEARCH gets the full wiring. We assert both by resolving a
temp research-access.yaml and driving the real inject_research_access().

Needs pyyaml (the resolver reads YAML). Run:
    python3 tests/test_gen_devcontainer_wiring.py
(or run the whole suite with tests/run.sh)
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))  # resolve `import research_access` / `import gen_devcontainer`

import research_access  # noqa: E402
import gen_devcontainer as gd  # noqa: E402


RESEARCH_ACCESS = """\
platforms:
  - name: aws
    install: aws-cli
    credential: {cred}
    env: {{ AWS_PROFILE: ro, AWS_REGION: us-east-1 }}
  - name: metrics
    credential: {cred}
    mount_at: /home/dev/.config/m.token
    allow_domains: [metrics.acme.internal]
"""


class WiringTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.workspace = root / "workspace"
        self.outside = root / "outside"
        self.workspace.mkdir()
        self.outside.mkdir()
        cred = self.outside / "cred"
        cred.mkdir()

        ra = root / "research-access.yaml"
        ra.write_text(RESEARCH_ACCESS.format(cred=cred))
        # Resolve once; inject_research_access takes the resolved object.
        self.resolved = research_access.load(ra, self.workspace)
        os.environ.pop("ALLOWED_DOMAINS_EXTRA", None)

    def tearDown(self):
        self._tmp.cleanup()

    def test_research_gets_full_wiring(self):
        config = {}
        gd.inject_research_access({"name": "research", "wants_infra": True}, config, self.resolved)

        self.assertEqual(len(config["mounts"]), 2)
        self.assertTrue(all("readonly" in m for m in config["mounts"]))
        self.assertIn("ghcr.io/devcontainers/features/aws-cli:1", config["features"])
        env = config["containerEnv"]
        self.assertEqual(env["AWS_PROFILE"], "ro")
        self.assertIn(".amazonaws.com", env["ALL_ALLOWED_DOMAINS"])
        self.assertIn("metrics.acme.internal", env["ALL_ALLOWED_DOMAINS"])
        self.assertIn("aws\t/home/dev/.aws", env["RESEARCH_PLATFORMS"])
        # RESEARCH_PLATFORMS must NOT contain a literal newline — it ends up
        # in Dockerfile-with-features as an ENV instruction when Features are
        # present, and a newline there is parsed as a new (unknown) instruction.
        self.assertNotIn("\n", env["RESEARCH_PLATFORMS"])
        # Two platforms in the fixture → the separator must appear between them.
        self.assertIn("|", env["RESEARCH_PLATFORMS"])

    def test_apply_gets_nothing(self):
        config = {}
        gd.inject_research_access({"name": "apply", "wants_infra": False}, config, self.resolved)
        # Untouched: no mounts, no features, no containerEnv — even though the
        # very same resolved research-access is passed in.
        self.assertEqual(config, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
