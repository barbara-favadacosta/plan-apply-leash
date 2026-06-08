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

# Banner: summarize the in-effect plan from the compiled allowlist.
python3 - <<'PY'
import json, os
compiled = json.load(open(os.environ["APPLY_COMPILED_PATH"]))
print("──────────────────────────────────────────────")
print(f"apply env ready")
print(f"  plan_id:        {compiled['plan_id']}")
print()
print(f"  /workspace:        harness checkout")
print(f"  /workspace/repos:  each repo from repos.yaml mounted by slug (read-write, hook-gated)")
print(f"  /workspace/target-state/audit/: tally + compiled allowlist (persistent)")
print()
print(f"  capabilities:   GitHub (via GH_TOKEN), code edits to /workspace/repos/<slug>/")
print(f"  NOT available:  AWS CLI, kubectl")
_req = os.environ.get("APPLY_REQUIRE_PUBLISH_APPROVAL", "1").strip().lower() not in ("0", "false", "no", "")
print("  publish:        " + ("commit/push/PR PAUSED until you run scripts/approve-publish.sh"
                              if _req else "auto (approval disabled)"))
print()
print(f"  in-scope repos ({len(compiled.get('repos', {}))}):")
for slug, cfg in compiled.get("repos", {}).items():
    branch = cfg.get("branch", "?")
    n_files = len(cfg.get("file_paths", []))
    print(f"    • {slug:25s} → {cfg.get('github', '?')}  ({n_files} files, branch {branch})")
print()
print(f"  compiled plan:  {os.environ['APPLY_COMPILED_PATH']}")
print(f"  tally:          {os.environ['APPLY_TALLY_PATH']}")
print("──────────────────────────────────────────────")
PY
