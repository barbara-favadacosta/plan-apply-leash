#!/usr/bin/env python3
"""
pre-tool-hook.py — PreToolUse hook for the apply env (reference agent: Claude Code).

Reads tool-call JSON on stdin, checks it against the compiled plan allowlist
written by validate_plan.py during container startup, and exits non-zero to block
anything outside the approved scope.

Side-effect: appends one line to APPLY_TALLY_PATH (JSONL) per decision. The Stop
hook reads this to produce a session summary. Now that the tally lives in
target-state/<slug>/audit/ on the host, it persists across container rebuilds
and across sessions — entries are filtered by session_id when summarising,
so each Stop hook still gets per-session output even though the log itself
grows over time.

Exit 0  : allow the call
Exit 2  : block — stderr message is fed back to the model so it knows why

Hook input shape (PreToolUse):
  {
    "tool_name": "Bash" | "Edit" | "Write" | "NotebookEdit" | ...,
    "tool_input": { ... tool-specific ... },
    "session_id": "..."
  }
"""
from __future__ import annotations

import json
import os
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOK_DIR))
import _paths
from validate_plan import (
    REPOS_PREFIX,
    check_path_allowed,
    check_command_allowed,
    command_shell_violation,
)


TALLY_PATH = _paths.tally_path()

# Publish checkpoint: editing/testing runs autonomously, but the commit →
# push → open-PR cycle is PAUSED until a human approves by creating
# PUBLISH_APPROVED_FILE (the agent can't: target-state is write-denied and the
# approve script isn't an allowed command). Approval resets each apply session
# (post-create.sh removes the file at startup). Set
# APPLY_REQUIRE_PUBLISH_APPROVAL=0 to opt into fully autonomous publishing.
PUBLISH_PREFIXES = ("git commit", "git push", "gh pr create", "gh pr ready", "git tag", "gh release")
PUBLISH_APPROVED_FILE = _paths.publish_approved_file()
REQUIRE_PUBLISH_APPROVAL = _paths.require_publish_approval()


def is_publish_command(cmd: str) -> bool:
    """True if the command publishes (commit / push / tag / open-PR / release).

    A literal prefix match is the fast path, but it misses publish commands that
    lead with global options — `git -C repo push`, `git --git-dir=… commit`. By
    the time this runs the command has already passed command_shell_violation(),
    so it is a single simple command we can tokenize and inspect for the
    git/gh subcommand wherever it falls.
    """
    c = cmd.strip()
    if any(c.startswith(p) for p in PUBLISH_PREFIXES):
        return True

    try:
        toks = shlex.split(c)
    except ValueError:
        return True  # unparseable → fail toward the gate, not around it
    if not toks:
        return False

    prog = os.path.basename(toks[0])
    rest = toks[1:]

    if prog == "git":
        # Skip git's global options (and their values) to reach the subcommand.
        opts_with_value = {"-C", "--git-dir", "--work-tree", "-c", "--namespace"}
        i = 0
        while i < len(rest):
            t = rest[i]
            if t in opts_with_value:
                i += 2
                continue
            if t.startswith("-"):
                i += 1
                continue
            return t in ("push", "commit", "tag")
        return False

    if prog == "gh":
        nonopt = [t for t in rest if not t.startswith("-")]
        if nonopt[:2] == ["pr", "create"] or nonopt[:2] == ["pr", "ready"]:
            return True
        if nonopt[:1] == ["release"]:
            return True
        return False

    return False


def publish_approved() -> bool:
    return (not REQUIRE_PUBLISH_APPROVAL) or PUBLISH_APPROVED_FILE.exists()


def allowed_paths_hint(path: str, compiled: dict) -> str:
    """Human-readable count of allowed file_paths for a blocked Edit/Write.

    The compiled allowlist is per-repo (compiled["repos"][slug]["file_paths"]),
    so there is no top-level "file_paths" to count. When the path resolves to a
    known repo slug, report that repo's allowance; otherwise report the total
    across every in-scope repo (the path is outside /workspace/repos/ entirely,
    or names an unknown slug)."""
    repos = compiled.get("repos", {})
    if path.startswith(REPOS_PREFIX):
        remainder = path[len(REPOS_PREFIX):]
        slug = remainder.split("/", 1)[0] if "/" in remainder else remainder
        repo = repos.get(slug)
        if isinstance(repo, dict):
            return f"{len(repo.get('file_paths', []))} allowed in repo {slug!r}"
    total = sum(len(c.get("file_paths", [])) for c in repos.values() if isinstance(c, dict))
    return f"{total} allowed across {len(repos)} in-scope repo(s)"


def record(session_id: str, tool: str, target: str, decision: str, reason: str) -> None:
    try:
        TALLY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with TALLY_PATH.open("a") as f:
            f.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "session_id": session_id,
                "tool": tool,
                "target": target,
                "decision": decision,
                "reason": reason,
            }) + "\n")
    except OSError:
        pass


def decide(session_id: str, tool: str, target: str, allowed: bool, reason: str) -> None:
    record(session_id, tool, target, "allow" if allowed else "block", reason)
    if allowed:
        sys.exit(0)
    print(f"[apply-harness] BLOCKED: {reason}", file=sys.stderr)
    sys.exit(2)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        # No session_id available; record under "?" and block.
        decide("?", "?", "?", False, f"hook received non-JSON stdin: {e}")

    session_id = payload.get("session_id", "?")

    compiled_path = _paths.compiled_path()
    if not compiled_path.exists():
        decide(session_id, "?", "?", False, f"no compiled allowlist at {compiled_path}; apply env uninitialized")

    try:
        compiled = json.loads(compiled_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        decide(session_id, "?", "?", False, f"could not load compiled allowlist: {e}")

    tool = payload.get("tool_name", "")
    inp = payload.get("tool_input", {}) or {}

    if tool in ("Edit", "Write", "NotebookEdit"):
        path = inp.get("file_path") or inp.get("notebook_path") or ""
        if not path:
            decide(session_id, tool, "", False, f"{tool} call missing file_path")
        if not check_path_allowed(path, compiled):
            decide(
                session_id, tool, path, False,
                f"{tool} on {path!r} is outside scope.file_paths "
                f"({allowed_paths_hint(path, compiled)})"
            )
        decide(session_id, tool, path, True, "in scope.file_paths")

    if tool == "Bash":
        cmd = (inp.get("command") or "").strip()
        if not cmd:
            decide(session_id, tool, "", False, "Bash call missing command")
        violation = command_shell_violation(cmd)
        if violation is not None:
            decide(
                session_id, tool, cmd, False,
                f"Bash command contains a disallowed shell operator: {violation}. "
                f"The command allowlist is a prefix match, so chaining, pipes, "
                f"redirection, and command substitution would smuggle work past it "
                f"(and let Bash write files outside scope.file_paths). Run ONE "
                f"allowed command at a time, with no operators."
            )
        if not check_command_allowed(cmd, compiled):
            decide(
                session_id, tool, cmd, False,
                f"Bash command does not match any allowed_command_prefixes "
                f"({len(compiled.get('allowed_command_prefixes', []))} allowed)"
            )
        if is_publish_command(cmd) and not publish_approved():
            decide(
                session_id, tool, cmd, False,
                "PUBLISH PAUSED — editing is autonomous, but git commit / git push / "
                "gh pr create need human sign-off. STOP now: summarize what you changed "
                "in each in-scope repo and ASK the user to approve publishing. Do NOT "
                "retry or work around this. Once they run scripts/approve-publish.sh you "
                "may commit, push, and open the PR."
            )
        decide(session_id, tool, cmd, True, "matches an allowed_command_prefix")

    decide(session_id, tool, "", True, "tool not gated by this hook")


if __name__ == "__main__":
    main()
