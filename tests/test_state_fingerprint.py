#!/usr/bin/env python3
"""
State is namespaced per GitHub token: state/by-token/<fp>/<subtree>, so rotating
a token swaps in a fresh tree and rotating back remounts the cached one. Two
invariants are load-bearing and tested here:

  1. The Python fingerprint (gen_devcontainer.token_fingerprint) and the bash one
     (scripts/_state_lib.sh:leash_token_fp) agree EXACTLY. They must, because
     gen_devcontainer.py creates the mount and plan-promote.sh / approve-publish.sh
     / setup.sh resolve the same directory on the host — a mismatch would point
     the host scripts at a different tree than the container mounts.

  2. render() actually emits the state mounts under state/by-token/<fp>/, with
     each env keyed by its OWN token (research by the research token, apply by
     the apply token).

Run: python3 tests/test_state_fingerprint.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import gen_devcontainer as gd  # noqa: E402
import research_access  # noqa: E402

STATE_LIB = SCRIPTS / "_state_lib.sh"

TOKENS = [
    "ghp_exampletoken1234567890",
    "github_pat_11ABCDEFG_longerlookingtokenwithunderscores",
    "x",                       # short
    "a b c",                   # internal whitespace (PATs never have this, but be safe)
    "ümlaut-token-é",          # non-ascii
]


def bash_fp(token: str) -> str:
    """Fingerprint as the host bash helpers compute it."""
    script = f'source "{STATE_LIB}"; leash_token_fp "$1"'
    out = subprocess.run(
        ["bash", "-c", script, "bash", token],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


class FingerprintParityTest(unittest.TestCase):
    def test_python_and_bash_agree(self):
        for tok in TOKENS:
            with self.subTest(token=tok):
                py = gd.token_fingerprint(tok)
                sh = bash_fp(tok)
                self.assertEqual(py, sh, f"py={py} sh={sh} for {tok!r}")
                self.assertRegex(py, r"^[0-9a-f]{16}$")

    def test_strip_is_applied(self):
        # Surrounding whitespace must not change the fingerprint (the token file
        # often has a trailing newline; both sides strip it).
        self.assertEqual(
            gd.token_fingerprint("  ghp_abc\n"), gd.token_fingerprint("ghp_abc")
        )

    def test_distinct_tokens_distinct_fp(self):
        fps = {gd.token_fingerprint(t) for t in TOKENS}
        self.assertEqual(len(fps), len(TOKENS), "tokens collided to the same fp")


class RenderNamespacesByTokenTest(unittest.TestCase):
    """render() must place each env's state mounts under by-token/<fp-of-its-token>."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.gen_dir = self.tmp / "gen"
        self.gen_dir.mkdir()
        # Redirect STATE_ROOT so render() scaffolds into the temp dir, not the
        # real repo state/.
        self._orig_state_root = gd.STATE_ROOT
        gd.STATE_ROOT = self.tmp / "state"
        os.environ.pop("ALLOWED_DOMAINS_EXTRA", None)

    def tearDown(self):
        gd.STATE_ROOT = self._orig_state_root
        self._tmp.cleanup()

    def _render(self, name: str, token: str) -> list[str]:
        src = next(t for t in gd.TARGETS if t["name"] == name)
        target = dict(src)
        target["output"] = self.tmp / f"{name}.json"
        gd.render(
            target,
            repos=[],
            gen_dir=self.gen_dir,
            token=token,
            research_scope=[],
            research_access=research_access.Resolved(),
        )
        import json
        body = "\n".join(
            ln for ln in target["output"].read_text().splitlines()
            if not ln.lstrip().startswith("//")
        )
        return json.loads(body)["mounts"]

    def test_research_state_under_its_token_fp(self):
        token = "ghp_research_token"
        fp = gd.token_fingerprint(token)
        mounts = self._render("research", token)
        state = [m for m in mounts if "/workspace/target-state/research" in m]
        self.assertTrue(state, "research must mount its research subtree")
        for m in state:
            self.assertIn(f"/state/by-token/{fp}/research", m,
                          f"research state source not under its token fp: {m}")

    def test_apply_state_under_its_token_fp(self):
        token = "ghp_apply_token"
        fp = gd.token_fingerprint(token)
        mounts = self._render("apply", token)
        state = [m for m in mounts
                 if "/workspace/target-state/approved-plans" in m
                 or "/workspace/target-state/audit" in m]
        self.assertTrue(state, "apply must mount approved-plans + audit")
        for m in state:
            self.assertIn(f"/state/by-token/{fp}/", m,
                          f"apply state source not under its token fp: {m}")

    def test_different_tokens_isolate_state(self):
        fp_a = gd.token_fingerprint("token-A")
        fp_b = gd.token_fingerprint("token-B")
        self.assertNotEqual(fp_a, fp_b)
        a = self._render("research", "token-A")
        b = self._render("research", "token-B")
        self.assertTrue(any(fp_a in m for m in a))
        self.assertTrue(any(fp_b in m for m in b))
        self.assertFalse(any(fp_a in m for m in b),
                         "token-B's render leaked token-A's state dir")


if __name__ == "__main__":
    unittest.main(verbosity=2)
