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

import json
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


class StateMountTest(unittest.TestCase):
    """The structural isolation invariant of Plan A: state/ lives outside the
    app/→/workspace bind, and each env mounts only the subtrees it needs. Apply
    must NEVER get a research-state mount; research must NEVER get approved-plans
    or audit. We drive the real render() with the real TARGETS config so the
    assertions track whatever the shipped config says."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.gen_dir = self.tmp / "gen"
        self.gen_dir.mkdir()
        os.environ.pop("ALLOWED_DOMAINS_EXTRA", None)

    def tearDown(self):
        self._tmp.cleanup()

    def _render(self, name: str) -> dict:
        """Render the named real target to a temp output and return the parsed
        devcontainer config (the // banner lines are stripped before json.loads)."""
        src = next(t for t in gd.TARGETS if t["name"] == name)
        target = dict(src)
        target["output"] = self.tmp / f"{name}.devcontainer.json"
        gd.render(
            target,
            repos=[],
            gen_dir=self.gen_dir,
            token="dummy-token",
            research_scope=[],
            research_access=research_access.Resolved(),
        )
        text = target["output"].read_text()
        body = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("//"))
        return json.loads(body)

    def test_workspace_mount_points_at_app(self):
        for name in ("research", "apply"):
            cfg = self._render(name)
            self.assertRegex(
                cfg["workspaceMount"], r"source=[^,]*/app,target=/workspace",
                f"{name}: workspaceMount source must end in /app",
            )

    def test_apply_never_mounts_research_state(self):
        mounts = self._render("apply")["mounts"]
        self.assertFalse(
            any("/workspace/target-state/research" in m for m in mounts),
            "apply must NOT mount any research state — that's the whole point",
        )

    def test_apply_gets_readonly_approved_plans_rw_audit_and_repos_yaml(self):
        mounts = self._render("apply")["mounts"]

        def find(target_path):
            return [m for m in mounts if f"target={target_path}," in m or m.endswith(f"target={target_path}")]

        approved = find("/workspace/target-state/approved-plans")
        self.assertTrue(approved, "apply must mount approved-plans")
        self.assertTrue(all("readonly" in m for m in approved),
                        "approved-plans must be read-only in apply")

        audit = find("/workspace/target-state/audit")
        self.assertTrue(audit, "apply must mount the audit dir")
        self.assertTrue(all("readonly" not in m for m in audit),
                        "audit must be read-write in apply (tally appends)")

        repos_yaml = [m for m in mounts if "target=/workspace/repos.yaml," in m]
        self.assertTrue(repos_yaml, "apply must mount repos.yaml for the --apply-repos re-check")
        self.assertTrue(all("readonly" in m for m in repos_yaml),
                        "repos.yaml must be read-only in apply")

    def test_research_mounts_only_research_state(self):
        mounts = self._render("research")["mounts"]
        self.assertTrue(
            any("/workspace/target-state/research" in m for m in mounts),
            "research must mount its research-state subtree",
        )
        self.assertFalse(
            any("/workspace/target-state/approved-plans" in m or "/workspace/target-state/audit" in m
                for m in mounts),
            "research must NOT see approved-plans or audit",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
