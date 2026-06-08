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

# Compile the approved plan into the enforced allowlist. This SAME step also runs
# on every container attach / VS Code "Reload Window" (postAttachCommand), so a
# freshly-promoted plan is picked up without a full rebuild. It resets the publish
# gate when the plan changes and fails closed if the plan is missing/invalid.
bash /workspace/.devcontainer/apply/load-plan.sh
