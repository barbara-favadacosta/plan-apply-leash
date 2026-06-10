#!/usr/bin/env bash
# load-plan.sh — (re)compile the approved plan into the enforced allowlist.
#
# Idempotent and side-effect-light ON PURPOSE: this runs both at container
# create (from post-create.sh) AND on every container attach / VS Code
# "Reload Window" (via postAttachCommand). That is what lets a freshly-promoted
# plan take effect without a full rebuild — promote on the host, reload the
# apply window, done.
#
# What it deliberately does NOT do, so it's safe to run on every attach:
#   - it does NOT touch the firewall (sudo/iptables stays in post-create.sh;
#     re-applying it per attach risks duplicate or failed rules);
#   - it does NOT reinstall agent settings.
#
# Fail-closed: if the approved plan is missing or fails validation, the live
# allowlist is REMOVED rather than left stale — so apply comes up locked (the
# PreToolUse hook blocks every call when the allowlist is absent) instead of
# silently enforcing the PREVIOUS plan.
set -euo pipefail

mkdir -p /workspace/target-state/audit

if [ ! -f "${APPLY_PLAN_PATH}" ]; then
  echo "FATAL: no approved plan at ${APPLY_PLAN_PATH}"
  echo "Run 'scripts/plan-promote.sh <draft>' on the host first."
  # fail closed: no plan -> drop any stale allowlist so apply is locked, not stale.
  rm -f "${APPLY_COMPILED_PATH}"
  exit 1
fi

echo "validating plan: ${APPLY_PLAN_PATH}"

TMP_COMPILED="$(mktemp -t compiled-allowlist.XXXXXX)"
cleanup() { rm -f "${TMP_COMPILED}"; }
trap cleanup EXIT

if ! python3 /workspace/hooks/validate_plan.py \
      --plan "${APPLY_PLAN_PATH}" \
      --schema /workspace/plans/schema.json \
      --compile-to "${TMP_COMPILED}" \
      --apply-repos /workspace/repos.yaml; then
  echo "FATAL: plan failed validation; refusing to load it."
  # fail closed: drop the stale allowlist so we don't keep enforcing the OLD plan.
  rm -f "${APPLY_COMPILED_PATH}"
  exit 1
fi

# Only reset the publish gate when the compiled allowlist actually changed, so an
# incidental Reload Window doesn't wipe an approval you just granted. A genuinely
# new/changed plan SHOULD require re-approval before commit/push/PR.
PUBLISH_FILE="${APPLY_PUBLISH_APPROVED_FILE:-/workspace/target-state/audit/publish-approved}"
if [ ! -f "${APPLY_COMPILED_PATH}" ] || ! cmp -s "${TMP_COMPILED}" "${APPLY_COMPILED_PATH}"; then
  mv -f "${TMP_COMPILED}" "${APPLY_COMPILED_PATH}"
  rm -f "${PUBLISH_FILE}"
  echo "→ loaded a new/changed plan; publish approval reset"
else
  echo "→ plan unchanged; allowlist and publish approval left as-is"
fi

# Banner: summarize the in-effect plan from the compiled allowlist. The per-repo
# scope block comes from validate_plan.format_compiled_summary — the single
# renderer shared with plan-promote.sh, so the two banners never drift. The
# heredoc is quoted (<<'PY') and reads every path from the environment, so no
# shell value is ever interpolated into the Python source.
python3 - <<'PY'
import json, os, sys
sys.path.insert(0, os.path.join(os.environ.get("HARNESS_PATH", "/workspace"), "hooks"))
from validate_plan import format_compiled_summary
compiled = json.load(open(os.environ["APPLY_COMPILED_PATH"]))
_req = os.environ.get("APPLY_REQUIRE_PUBLISH_APPROVAL", "1").strip().lower() not in ("0", "false", "no", "")
print("──────────────────────────────────────────────")
print("apply env ready")
print(f"  plan_id:        {compiled['plan_id']}")
print()
print("  /workspace:        harness checkout")
print("  /workspace/repos:  each repo from repos.yaml mounted by slug (read-write, hook-gated)")
print("  /workspace/target-state/audit/: tally + compiled allowlist (persistent)")
print()
print("  capabilities:   GitHub (via GH_TOKEN), code edits to /workspace/repos/<slug>/")
print("  NOT available:  AWS CLI, kubectl")
print("  workflow:       branch-first ENFORCED — edits blocked until the repo is on the plan's branch")
print("  publish:        " + ("branch/edit/commit autonomous; push & PR PAUSED until you run scripts/approve-publish.sh"
                              if _req else "auto (approval disabled)"))
print()
print(format_compiled_summary(compiled))
print()
print(f"  compiled plan:  {os.environ['APPLY_COMPILED_PATH']}")
print(f"  tally:          {os.environ['APPLY_TALLY_PATH']}")
print("──────────────────────────────────────────────")
PY
