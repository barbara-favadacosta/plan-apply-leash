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
  # shellcheck source=scripts/_state_lib.sh
  source "${REPO_ROOT}/scripts/_state_lib.sh"
  # The audit dir is namespaced by the apply token (state/by-token/<fp>/audit),
  # the same tree the apply container mounts at /workspace/target-state/audit.
  APPLY_STATE="$(leash_state_root apply)" || true
  if [ -z "${APPLY_STATE}" ]; then
    echo "✗ could not resolve the apply state dir from your creds file." >&2
    echo "  Check GH_TOKEN_APPLY_FILE, then re-run scripts/setup.sh." >&2
    exit 69
  fi
  SENTINEL="${APPLY_STATE}/audit/publish-approved"
fi

mkdir -p "$(dirname "${SENTINEL}")"
printf '%s approved by %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(whoami)" > "${SENTINEL}"

echo "✓ publish approved → ${SENTINEL}"
echo "  Tell the apply agent to continue — it can now commit, push, and open the PR."
echo "  (Resets automatically on the next apply container start.)"
