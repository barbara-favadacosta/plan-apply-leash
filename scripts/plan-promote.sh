#!/usr/bin/env bash
# plan-promote.sh — host-side helper to safely promote a researched draft plan
# into state/approved-plans/current.yaml.
#
# This script runs on the HOST and is the security-critical handoff between
# research and apply. It refuses to promote a plan that fails validation,
# shows you the diff against the current approved plan, and requires explicit
# confirmation before overwriting.
#
# Multi-repo plans: validation checks that step.repo references match
# scope.repos keys, and that the GitHub PATs for in-scope repos look OK.
#
# Usage:
#   scripts/plan-promote.sh <draft>
# where <draft> can be:
#   - an absolute path to a draft yaml
#   - a path relative to the cwd
#   - just the filename inside the drafts dir (also falls back to the default/
#     project), or a "<project>/<filename>" relative to it. For a draft in a
#     NAMED project, the project-relative form or its full path both work.
#
# State is namespaced per GitHub token (state/by-token/<fp>/…), so the drafts and
# approved-plans dirs are resolved from your current tokens via _state_lib.sh —
# don't hardcode state/research/drafts paths; pass a bare or project-relative
# filename and let the script find it under the active token's tree.

set -euo pipefail

DRAFT_ARG="${1:-}"
if [ -z "${DRAFT_ARG}" ]; then
  cat >&2 <<EOF
usage: $0 <draft-plan>
  draft can be absolute, cwd-relative, or relative to the active token's drafts
  dir — a bare filename (falls back to the default/ project) or "<project>/<file>".

examples:
  $0 2026-05-22-bump-logger.yaml          # resolved against drafts/ then drafts/default/
  $0 myproj/2026-05-22-bump-logger.yaml   # named project, relative to the drafts dir
EOF
  exit 64
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${REPO_ROOT}"
# shellcheck source=scripts/_state_lib.sh
source "${REPO_ROOT}/scripts/_state_lib.sh"

SCHEMA="${REPO_ROOT}/app/plans/schema.json"
VALIDATOR="${REPO_ROOT}/app/hooks/validate_plan.py"

# Drafts come from the RESEARCH token's tree; the approved plan lands in the
# APPLY token's tree — each side keyed by the token its container uses.
RESEARCH_STATE="$(leash_state_root research)" || true
APPLY_STATE="$(leash_state_root apply)" || true
if [ -z "${RESEARCH_STATE}" ] || [ -z "${APPLY_STATE}" ]; then
  echo "✗ could not resolve the per-token state dirs from your creds file." >&2
  echo "  Check GH_TOKEN_RESEARCH_FILE / GH_TOKEN_APPLY_FILE, then re-run scripts/setup.sh." >&2
  exit 69
fi
DRAFTS_DIR="${RESEARCH_STATE}/research/drafts"
APPROVED_DIR="${APPLY_STATE}/approved-plans"
HISTORY_DIR="${APPROVED_DIR}/history"
CURRENT="${APPROVED_DIR}/current.yaml"

mkdir -p "${APPROVED_DIR}" "${HISTORY_DIR}" "${DRAFTS_DIR}"

if [ -f "${DRAFT_ARG}" ]; then
  DRAFT="$(cd "$(dirname "${DRAFT_ARG}")" && pwd)/$(basename "${DRAFT_ARG}")"
elif [ -f "${DRAFTS_DIR}/${DRAFT_ARG}" ]; then
  DRAFT="${DRAFTS_DIR}/${DRAFT_ARG}"
elif [ -f "${DRAFTS_DIR}/default/${DRAFT_ARG}" ]; then
  # Convenience: a bare filename with no project subfolder resolves against the
  # always-present default/ project. Named projects are NOT auto-searched —
  # pass their full path to avoid ambiguity between same-named drafts.
  DRAFT="${DRAFTS_DIR}/default/${DRAFT_ARG}"
  echo "→ resolved bare filename against the default project: ${DRAFT#${REPO_ROOT}/}" >&2
  echo "  (for a named project, use the project-relative form, e.g." >&2
  echo "   $0 <project>/${DRAFT_ARG})" >&2
else
  echo "✗ draft not found at: ${DRAFT_ARG}" >&2
  echo "  also tried: ${DRAFTS_DIR}/${DRAFT_ARG}" >&2
  echo "  also tried: ${DRAFTS_DIR}/default/${DRAFT_ARG}" >&2
  echo "  for a draft inside a named project, use the project-relative form:" >&2
  echo "    $0 <project>/${DRAFT_ARG}" >&2
  exit 66
fi

echo "→ draft:        ${DRAFT}"
echo "→ promote to:   ${CURRENT}"
echo ""

if ! command -v python3 >/dev/null 2>&1; then
  echo "✗ python3 not found on PATH (validator can't run)" >&2
  exit 69
fi

if ! python3 -c "import yaml, jsonschema" >/dev/null 2>&1; then
  cat >&2 <<EOF
✗ python deps missing on host. Install with:
    pip3 install --user pyyaml jsonschema
EOF
  exit 69
fi

TMP_COMPILED="$(mktemp -t plan-promote.XXXXXX)"
cleanup() { rm -f "${TMP_COMPILED}"; }
trap cleanup EXIT

echo "→ validating draft..."
if ! python3 "${VALIDATOR}" \
      --plan "${DRAFT}" \
      --schema "${SCHEMA}" \
      --compile-to "${TMP_COMPILED}" \
      --apply-repos "${REPO_ROOT}/repos.yaml"; then
  echo "✗ validation failed; refusing to promote" >&2
  exit 1
fi
echo "✓ validation passed"
echo

echo "→ compiled allowlist preview:"
# Quoted heredoc (<<'PY') + paths via the environment: nothing from the shell is
# interpolated into the Python source. The per-repo block is rendered by the
# shared validate_plan.format_compiled_summary, the same one load-plan.sh uses.
LEASH_HOOKS_DIR="${VALIDATOR%/*}" LEASH_COMPILED="${TMP_COMPILED}" python3 - <<'PY'
import json, os, sys
sys.path.insert(0, os.environ["LEASH_HOOKS_DIR"])
from validate_plan import format_compiled_summary
with open(os.environ["LEASH_COMPILED"]) as f:
    c = json.load(f)
print(f"   plan_id: {c['plan_id']}")
print(format_compiled_summary(c, indent="   "))
PY
echo

if [ -f "${CURRENT}" ]; then
  echo "→ diff against current approved plan:"
  if diff -u "${CURRENT}" "${DRAFT}" >/tmp/plan-promote-diff.$$ 2>&1; then
    echo "   (no changes — draft is identical to current.yaml)"
  else
    sed 's/^/   /' /tmp/plan-promote-diff.$$
  fi
  rm -f /tmp/plan-promote-diff.$$
  echo
else
  echo "→ no current.yaml exists yet — this will be the first approved plan"
  echo
fi

read -r -p "Promote this draft? [y/N] " ans
case "${ans}" in
  y|Y|yes|YES) ;;
  *) echo "aborted (no changes made)"; exit 0 ;;
esac

if [ -f "${CURRENT}" ] && [ ! -L "${CURRENT}" ]; then
  TS="$(date -u +%Y%m%dT%H%M%SZ)"
  # Try to use the previous plan's plan_id in the backup name; fall back to
  # timestamp. CURRENT is passed via the environment, not interpolated into the
  # Python source, so a path with a quote/newline can't break out of the string.
  PREV_ID=$(CURRENT_PLAN="${CURRENT}" python3 -c "
import os, yaml
try:
    print(yaml.safe_load(open(os.environ['CURRENT_PLAN']))['plan_id'])
except Exception:
    pass
" 2>/dev/null)
  if [ -n "${PREV_ID}" ]; then
    BACKUP="${HISTORY_DIR}/${TS}-${PREV_ID}.yaml"
  else
    BACKUP="${HISTORY_DIR}/${TS}.yaml"
  fi
  cp "${CURRENT}" "${BACKUP}"
  echo "→ backed up previous current.yaml to ${BACKUP#${REPO_ROOT}/}"
fi

NEW_TMP="${APPROVED_DIR}/.current.yaml.new"
cp "${DRAFT}" "${NEW_TMP}"
mv -f "${NEW_TMP}" "${CURRENT}"

echo "✓ promoted to ${CURRENT#${REPO_ROOT}/}"
echo "→ in the apply window, run 'Developer: Reload Window' to pick up the new plan"
echo "  (Rebuild Container is only needed after re-running setup.sh, which"
echo "   regenerates devcontainer.json)"
