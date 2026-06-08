#!/usr/bin/env python3
"""
Hermetic unit tests for scripts/research_access.py.

No Docker, no pyyaml: every test calls parse_platforms() with already-parsed
dicts, and uses a tempdir as the workspace plus tempdir credential files/dirs.

Run:  python3 tests/test_research_access.py
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from research_access import (  # noqa: E402
    CATALOG,
    ResearchAccessError,
    parse_platforms,
)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        # Workspace root, plus a sibling dir to hold "outside the tree"
        # credentials. They must be siblings so cred paths are genuinely
        # outside the workspace.
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.workspace = root / "workspace"
        self.outside = root / "outside"
        self.workspace.mkdir()
        self.outside.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def cred(self, name: str = "cred") -> str:
        """Create a credential dir outside the workspace, return its path."""
        p = self.outside / name
        p.mkdir(exist_ok=True)
        return str(p)


class TestValid(Base):
    def test_empty_and_none(self):
        self.assertEqual(parse_platforms(None, self.workspace).mounts, [])
        self.assertEqual(parse_platforms({}, self.workspace).mounts, [])
        self.assertEqual(parse_platforms({"platforms": []}, self.workspace).mounts, [])

    def test_tier_b_aws_resolves_feature_mount_and_wildcard_domains(self):
        res = parse_platforms({
            "platforms": [{
                "name": "aws",
                "install": "aws-cli",
                "credential": self.cred("aws"),
                "env": {"AWS_PROFILE": "ro", "AWS_REGION": "eu-west-2"},
            }]
        }, self.workspace)

        self.assertIn(CATALOG["aws-cli"]["feature"], res.features)
        self.assertEqual(len(res.mounts), 1)
        self.assertIn("target=/home/dev/.aws", res.mounts[0])
        self.assertIn("readonly", res.mounts[0])
        self.assertEqual(res.container_env["AWS_PROFILE"], "ro")
        # one wildcard suffix covers every AWS service in every region — the
        # egress proxy matches on hostname, so no per-service/region list.
        self.assertIn(".amazonaws.com", res.allow_domains)
        self.assertIn(".api.aws", res.allow_domains)
        self.assertEqual(res.summary, [("aws", "/home/dev/.aws")])

    def test_tier_b_aws_needs_no_env(self):
        # With wildcard domains there is nothing to template, so aws-cli resolves
        # even with no env (the CLI's own default region/profile still applies).
        res = parse_platforms({
            "platforms": [{
                "name": "aws",
                "install": "aws-cli",
                "credential": self.cred("aws"),
            }]
        }, self.workspace)
        self.assertIn(".amazonaws.com", res.allow_domains)

    def test_leading_dot_wildcard_domain_accepted(self):
        res = parse_platforms({
            "platforms": [{
                "name": "metrics",
                "credential": self.cred("m"),
                "mount_at": "/home/dev/.config/m.token",
                "allow_domains": [".metrics.acme.internal"],
            }]
        }, self.workspace)
        self.assertIn(".metrics.acme.internal", res.allow_domains)

    def test_tier_b_kubectl_sets_kubeconfig_and_merges_user_domains(self):
        res = parse_platforms({
            "platforms": [{
                "name": "k8s",
                "install": "kubectl",
                "credential": self.cred("kube"),
                "allow_domains": ["cluster.example.internal"],
            }]
        }, self.workspace)
        self.assertEqual(res.container_env["KUBECONFIG"], "/home/dev/.kube/config")
        self.assertIn("cluster.example.internal", res.allow_domains)

    def test_tier_a_config_only_has_no_feature(self):
        res = parse_platforms({
            "platforms": [{
                "name": "metrics",
                "credential": self.cred("m"),
                "mount_at": "/home/dev/.config/acme/m.token",
                "env": {"METRICS_TOKEN_FILE": "/home/dev/.config/acme/m.token"},
                "allow_domains": ["metrics.acme.internal"],
            }]
        }, self.workspace)
        self.assertEqual(res.features, {})
        self.assertIn("metrics.acme.internal", res.allow_domains)
        self.assertIn("target=/home/dev/.config/acme/m.token", res.mounts[0])

    def test_entry_env_overrides_catalog_container_env(self):
        res = parse_platforms({
            "platforms": [{
                "name": "k8s",
                "install": "kubectl",
                "credential": self.cred("kube"),
                "env": {"KUBECONFIG": "/home/dev/.kube/config"},  # explicit, same value
            }]
        }, self.workspace)
        self.assertEqual(res.container_env["KUBECONFIG"], "/home/dev/.kube/config")


class TestRejected(Base):
    def assertRejects(self, data, needle: str):
        with self.assertRaises(ResearchAccessError) as ctx:
            parse_platforms(data, self.workspace)
        self.assertIn(needle, str(ctx.exception))

    def test_install_not_in_catalog(self):
        self.assertRejects({
            "platforms": [{"name": "x", "install": "vault", "credential": self.cred()}]
        }, "not in the catalog")

    def test_credential_inside_workspace(self):
        inside = self.workspace / "secrets"
        inside.mkdir()
        self.assertRejects({
            "platforms": [{"name": "x", "install": "aws-cli", "credential": str(inside),
                           "env": {"AWS_REGION": "us-east-1"}}]
        }, "inside the workspace")

    def test_credential_missing(self):
        self.assertRejects({
            "platforms": [{"name": "x", "install": "aws-cli",
                           "credential": str(self.outside / "nope"),
                           "env": {"AWS_REGION": "us-east-1"}}]
        }, "does not exist")

    def test_mount_at_outside_home(self):
        self.assertRejects({
            "platforms": [{"name": "x", "credential": self.cred(), "mount_at": "/etc/passwd"}]
        }, "must be under /home/dev")

    def test_mount_at_reserved_collision(self):
        self.assertRejects({
            "platforms": [{"name": "x", "credential": self.cred(),
                           "mount_at": "/home/dev/.claude/foo"}]
        }, "reserved harness path")

    def test_tier_a_without_mount_at(self):
        self.assertRejects({
            "platforms": [{"name": "x", "credential": self.cred()}]
        }, "mount_at is required")

    def test_duplicate_name(self):
        self.assertRejects({
            "platforms": [
                {"name": "dup", "credential": self.cred("a"), "mount_at": "/home/dev/a"},
                {"name": "dup", "credential": self.cred("b"), "mount_at": "/home/dev/b"},
            ]
        }, "duplicate platform name")

    def test_duplicate_mount_at(self):
        self.assertRejects({
            "platforms": [
                {"name": "a", "credential": self.cred("a"), "mount_at": "/home/dev/x"},
                {"name": "b", "credential": self.cred("b"), "mount_at": "/home/dev/x"},
            ]
        }, "already used by platform")

    def test_bad_name(self):
        self.assertRejects({
            "platforms": [{"name": "Has Space", "credential": self.cred(), "mount_at": "/home/dev/x"}]
        }, "name")

    def test_domain_with_scheme_rejected(self):
        self.assertRejects({
            "platforms": [{"name": "x", "credential": self.cred(), "mount_at": "/home/dev/x",
                           "allow_domains": ["https://evil.example/path"]}]
        }, "bare hostname")

    def test_platforms_not_a_list(self):
        self.assertRejects({"platforms": "aws"}, "must be a list")


if __name__ == "__main__":
    unittest.main(verbosity=2)
