#!/usr/bin/env bash
set -euo pipefail

# Pass the extra-domains allowlist as an ARGUMENT, not via the environment:
# sudo's env_reset strips it. Arguments survive sudo. (Base GitHub/Anthropic
# domains are hardcoded in the firewall; this only carries optional extras.)
sudo /usr/local/bin/init-firewall.sh "${ALLOWED_DOMAINS_EXTRA:-}"

# Install env-specific settings into the agent's user-level config dir.
# ~/.claude is the reference agent's config home.
mkdir -p ~/.claude
cp /workspace/.devcontainer/apply/agent-settings.json ~/.claude/settings.json

# Ensure audit dir exists (for the compiled allowlist + tally).
mkdir -p /workspace/target-state/audit

# Reset the publish approval each session: editing is autonomous, but the
# commit/push/PR cycle waits until the human runs scripts/approve-publish.sh.
rm -f "${APPLY_PUBLISH_APPROVED_FILE:-/workspace/target-state/audit/publish-approved}"

if [ ! -f "${APPLY_PLAN_PATH}" ]; then
  echo "FATAL: no approved plan at ${APPLY_PLAN_PATH}"
  echo "Run 'scripts/plan-promote.sh <draft>' on the host first."
  exit 1
fi

echo "validating plan: ${APPLY_PLAN_PATH}"
python3 /workspace/hooks/validate_plan.py \
  --plan "${APPLY_PLAN_PATH}" \
  --schema /workspace/plans/schema.json \
  --compile-to "${APPLY_COMPILED_PATH}" \
  --apply-repos /workspace/repos.yaml

# Extract repo summary from the compiled allowlist for the banner.
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
