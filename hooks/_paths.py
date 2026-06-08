#!/usr/bin/env python3
"""
_paths.py — the shared filesystem layout and env-var contract for the apply-env
hooks (pre-tool-hook, session-start, stop-hook).

These paths and env-var names used to be duplicated across all three hook files,
and the fallback defaults had drifted: pre-tool-hook.py and stop-hook.py fell
back to /workspace-state/audit/… (a path that is never mounted), while
session-start.py fell back to /workspace/target-state/audit/…. The devcontainer
always injects the real values via containerEnv, so the drift never bit at
runtime — but a reader couldn't tell which default was authoritative. Single-
sourcing them here makes the contract unambiguous and the fallbacks correct.

The defaults MUST stay in sync with the containerEnv block in
.devcontainer/apply/devcontainer.template.json, which is what actually sets
these variables inside the apply container.
"""
from __future__ import annotations

import os
from pathlib import Path

# Apply-env audit dir: lives under the host-mounted target-state/, so the tally
# and compiled allowlist persist across container rebuilds and sessions.
_AUDIT_DIR = "/workspace/target-state/audit"

DEFAULT_COMPILED_PATH = f"{_AUDIT_DIR}/compiled-allowlist.json"
DEFAULT_TALLY_PATH = f"{_AUDIT_DIR}/tally.jsonl"
DEFAULT_PUBLISH_APPROVED_FILE = f"{_AUDIT_DIR}/publish-approved"


def compiled_path() -> Path:
    """The compiled allowlist validate_plan.py wrote during container startup;
    the per-call hook enforces tool calls against it."""
    return Path(os.environ.get("APPLY_COMPILED_PATH", DEFAULT_COMPILED_PATH))


def tally_path() -> Path:
    """JSONL audit log — one line per gated tool decision, appended by the
    PreToolUse hook and summarised per-session by the Stop hook."""
    return Path(os.environ.get("APPLY_TALLY_PATH", DEFAULT_TALLY_PATH))


def publish_approved_file() -> Path:
    """Sentinel a human creates (scripts/approve-publish.sh) to lift the publish
    gate. post-create.sh removes it at startup, so approval is per apply session."""
    return Path(os.environ.get("APPLY_PUBLISH_APPROVED_FILE", DEFAULT_PUBLISH_APPROVED_FILE))


def require_publish_approval() -> bool:
    """Whether the commit/push/open-PR publish gate is active (default: yes).
    Set APPLY_REQUIRE_PUBLISH_APPROVAL=0 (or false/no) to publish autonomously."""
    raw = os.environ.get("APPLY_REQUIRE_PUBLISH_APPROVAL", "1").strip().lower()
    return raw not in ("0", "false", "no", "")
