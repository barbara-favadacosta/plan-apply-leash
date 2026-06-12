#!/usr/bin/env bash
set -euo pipefail

# Pass the compiled egress allowlist (ALL_ALLOWED_DOMAINS, set in containerEnv by
# gen_devcontainer.py) as an ARGUMENT, not via the environment: sudo's env_reset
# strips it, which would silently drop every platform domain from the allowlist.
# Arguments survive sudo.
sudo /usr/local/bin/init-firewall.sh "${ALL_ALLOWED_DOMAINS:-}"

# Install env-specific settings into the agent's user-level config dir (in the
# agent-auth-research volume). ~/.claude is the reference agent's config home.
# The settings file and slash commands are baked into the image (see Dockerfile)
# because the repo's .devcontainer/ is outside the app/→/workspace bind and so
# not visible inside the container.
mkdir -p ~/.claude
cp /usr/local/share/research-agent-settings.json ~/.claude/settings.json

# Install env-specific slash commands into the agent's personal command dir.
# (~/.claude lives in the agent-auth-research volume, same as settings.json above.)
mkdir -p ~/.claude/commands
cp -r /usr/local/share/research-commands/. ~/.claude/commands/

# Ensure the research workspace dirs exist, including the always-present
# `default` project used when the user never runs `/project`.
# (mkdir -p .../<d>/default creates the parent drafts/clones/notes too.)
for d in drafts clones notes; do
  mkdir -p "/workspace/target-state/research/$d/default"
done

echo "──────────────────────────────────────────────"
echo "research env ready"
echo ""
echo "  /workspace:        harness checkout"
echo "  /workspace/repos:  each repo from repos.yaml mounted by slug (READ-ONLY)"
echo "  /workspace/target-state/research/: drafts, clones, notes (READ-WRITE)"
echo ""

# GitHub — required (injected via --env-file from your creds file)
if [ -n "${GH_TOKEN:-}" ]; then
  echo "  ✓ GitHub:  GH_TOKEN set (read-only PAT expected, broad scope)"
else
  echo "  ✗ GitHub:  GH_TOKEN unset — set GH_TOKEN_RESEARCH_FILE in your creds file and re-run scripts/setup.sh"
fi

# Platform access — read-only mounts declared in research-access.yaml.
# RESEARCH_PLATFORMS is pipe-joined "name<TAB>mount_at" (set by
# gen_devcontainer.py). All are read-only; scope each credential read-only too.
if [ -n "${RESEARCH_PLATFORMS:-}" ]; then
  IFS='|' read -ra _platform_entries <<< "${RESEARCH_PLATFORMS}"
  for entry in "${_platform_entries[@]}"; do
    [ -z "${entry}" ] && continue
    pname="${entry%%$'\t'*}"
    ptarget="${entry#*$'\t'}"
    if [ -e "${ptarget}" ]; then
      echo "  ✓ ${pname}: read-only at ${ptarget}"
    else
      echo "  ✗ ${pname}: expected mount ${ptarget} missing — re-run setup.sh + Rebuild Container"
    fi
  done

  # Live self-test: prove each platform is actually REACHABLE through the egress
  # proxy (the thing IP-pinning silently broke) and, where a non-mutating probe
  # exists, that the credential is READ-ONLY. Every probe is read-only — none
  # creates, modifies, or deletes anything.
  echo ""
  echo "  platform reachability (through the egress proxy):"
  for entry in "${_platform_entries[@]}"; do
    [ -z "${entry}" ] && continue
    pname="${entry%%$'\t'*}"
    case "${pname}" in
      aws)
        if id=$(aws sts get-caller-identity --query Arn --output text \
                  --cli-connect-timeout 8 --cli-read-timeout 12 2>/tmp/aws_probe.err); then
          echo "    ✓ aws: reachable — caller ${id}"
          echo "        (confirm read-only scope with:"
          echo "         aws iam simulate-principal-policy --policy-source-arn ${id} --action-names s3:DeleteObject)"
        else
          echo "    ✗ aws: STS call failed — $(tr -d '\n' </tmp/aws_probe.err | tail -c 200)"
        fi
        ;;
      k8s)
        if kubectl version --request-timeout=10s >/dev/null 2>/tmp/k8s_probe.err; then
          can_write=$(kubectl auth can-i create namespaces --request-timeout=10s 2>/dev/null || echo "unknown")
          if [ "${can_write}" = "no" ]; then
            echo "    ✓ k8s: reachable — and read-only (cannot create namespaces)"
          else
            echo "    ! k8s: reachable — but 'can-i create namespaces' = ${can_write} (expected 'no' for a read-only SA)"
          fi
        else
          echo "    ✗ k8s: API unreachable — $(tr -d '\n' </tmp/k8s_probe.err | tail -c 200)"
        fi
        ;;
      *)
        echo "    – ${pname}: no built-in probe (Tier-A platform — verify by hand)"
        ;;
    esac
  done
  rm -f /tmp/aws_probe.err /tmp/k8s_probe.err
else
  echo "  – platforms: none (GitHub only — add entries to research-access.yaml to enable)"
fi

echo ""
# List available local repos for the agent's reference.
repo_count=$(find /workspace/repos -maxdepth 1 -mindepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')
echo "  local repos available at /workspace/repos/ (${repo_count} found):"
find /workspace/repos -maxdepth 1 -mindepth 1 -type d 2>/dev/null | sort | head -20 | sed 's|/workspace/repos/|    • |'
if [ "${repo_count}" -gt 20 ]; then
  echo "    … +$((repo_count - 20)) more"
fi
echo ""
echo "  drafts:       /workspace/target-state/research/drafts/<plan_id>.yaml"
echo "  plan schema:  /workspace/plans/schema.json"
echo "──────────────────────────────────────────────"
