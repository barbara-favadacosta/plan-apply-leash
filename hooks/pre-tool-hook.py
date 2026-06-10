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
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOK_DIR))
import _paths
from validate_plan import (
    REPOS_PREFIX,
    branch_gate_violation,
    check_path_allowed,
    check_command_allowed,
    command_shell_violation,
    classify_git_command,
    git_push_target,
    plan_branches,
    repo_slug_for_path,
)


TALLY_PATH = _paths.tally_path()

# Publish checkpoint. The branch → edit → commit cycle runs autonomously (commit
# is a LOCAL git op — see classify_git_command), but the things that leave the
# machine — git push, gh pr create / ready, gh release — are PAUSED until a human
# approves by creating PUBLISH_APPROVED_FILE (the agent can't: target-state is
# write-denied and the approve script isn't an allowed command). Approval resets
# each apply session (load-plan.sh removes the file when a new plan loads). Set
# APPLY_REQUIRE_PUBLISH_APPROVAL=0 to opt into fully autonomous publishing.
PUBLISH_APPROVED_FILE = _paths.publish_approved_file()
REQUIRE_PUBLISH_APPROVAL = _paths.require_publish_approval()


def is_publish_command(cmd: str) -> bool:
    """True for commands held behind the publish gate: git push, gh pr
    create/ready, gh release. Local git (branch/add/commit/inspection) is NOT a
    publish command — it runs autonomously. Classification (including the
    `git -C <repo> …` form) is centralized in validate_plan.classify_git_command
    so the allowlist and this gate always agree on what 'publish' means."""
    return classify_git_command(cmd) == "publish"


def publish_approved() -> bool:
    return (not REQUIRE_PUBLISH_APPROVAL) or PUBLISH_APPROVED_FILE.exists()


def current_git_branch(repo_dir: Path) -> str | None:
    """The repo's current branch (`git rev-parse --abbrev-ref HEAD`), or None if
    it can't be determined (not a git repo, detached HEAD, git missing). The
    branch-first gate treats None as 'not on the declared branch' and blocks —
    fail closed, since we can't confirm a new branch was created."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    branch = proc.stdout.strip()
    return branch or None


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
        # Branch-first: a change may only land on the plan's NEW branch. Block the
        # edit until the repo's HEAD is on the declared branch, forcing the agent
        # to `git checkout -b <branch>` before applying anything. This is the
        # built-in guarantee that every plan branches before it edits — not prose
        # the agent can ignore.
        slug = repo_slug_for_path(path)
        if slug is not None:
            declared = (compiled.get("repos", {}).get(slug) or {}).get("branch")
            current = current_git_branch(Path(REPOS_PREFIX) / slug)
            violation = branch_gate_violation(slug, declared, current)
            if violation is not None:
                decide(session_id, tool, path, False, f"BRANCH-FIRST: {violation}")
        decide(session_id, tool, path, True, "in scope.file_paths, on the plan's branch")

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
                f"allowed command at a time, with no operators. NOTE: operators "
                f"INSIDE quotes are fine (e.g. git commit -m \"fix a & b\") — if this "
                f"is a commit message or PR body, quote it; for bodies with backticks "
                f"or $(...), single-quote them or use `gh pr create --body-file`."
            )
        if not check_command_allowed(cmd, compiled):
            decide(
                session_id, tool, cmd, False,
                f"Bash command is neither the built-in git workflow (branch / add / "
                f"commit / push / gh pr create) nor any of the plan's "
                f"allowed_command_prefixes ({len(compiled.get('allowed_command_prefixes', []))} "
                f"allowed). Note: `cd` is not allowed — operate on a repo with "
                f"`git -C /workspace/repos/<slug> …`."
            )
        if is_publish_command(cmd) and not publish_approved():
            decide(
                session_id, tool, cmd, False,
                "PUBLISH PAUSED — branching, editing, testing, and COMMITTING are "
                "autonomous, but git push / gh pr create need human sign-off. STOP now: "
                "confirm every in-scope repo is committed, summarize what changed per "
                "repo, and ASK the user to approve publishing. Do NOT retry or work "
                "around this. Once they run scripts/approve-publish.sh you may push and "
                "open the PR."
            )
        # A push may only target a branch the plan declared — never main/master,
        # even after publish approval. The plan's branch is the contract; this is
        # the pin the old literal `git push origin <branch>` prefix used to give.
        is_push, target = git_push_target(cmd)
        if is_push:
            branches = plan_branches(compiled)
            if target is None:
                decide(
                    session_id, tool, cmd, False,
                    "git push must name the remote and the plan's branch explicitly, "
                    "e.g. `git -C /workspace/repos/<slug> push -u origin <branch>` — "
                    "the target branch couldn't be determined from this command."
                )
            if target not in branches:
                decide(
                    session_id, tool, cmd, False,
                    f"git push targets {target!r}, but the plan only declares "
                    f"branch(es) {sorted(branches)}. Push to the plan's branch, "
                    f"never to main/master or an undeclared branch."
                )
        decide(session_id, tool, cmd, True, "matches the allowed git workflow or an allowed_command_prefix")

    decide(session_id, tool, "", True, "tool not gated by this hook")


if __name__ == "__main__":
    main()
