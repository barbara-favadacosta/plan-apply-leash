#!/usr/bin/env bash
# approve-publish.sh — human approval for the apply agent's publish phase.
#
# The apply agent branches, edits, tests, and COMMITS autonomously, but
# `git push` and `gh pr create` are PAUSED by the PreToolUse hook until you
# approve. Review the agent's local commits first, then run this; the agent can
# then push (only to the plan's branch) and open the PR. Approval is reset when
# a new/changed plan loads (one approval per plan).
#
# Run it from the host (in the repo) OR from a non-agent shell in the container.
# The agent itself cannot run this (it isn't an allowed command) or create the
# sentinel (the state dir is write-denied) — that's what makes the pause real.
set -euo pipefail

if [ -n "${APPLY_PUBLISH_APPROVED_FILE:-}" ]; then
  SENTINEL="${APPLY_PUBLISH_APPROVED_FILE}"                    # inside the container
else
  REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"               # on the host
  SENTINEL="${REPO_ROOT}/state/audit/publish-approved"
fi

mkdir -p "$(dirname "${SENTINEL}")"
printf '%s approved by %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(whoami)" > "${SENTINEL}"

echo "✓ publish approved → ${SENTINEL}"
echo "  Tell the apply agent to continue — it can now commit, push, and open the PR."
echo "  (Resets automatically on the next apply container start.)"
