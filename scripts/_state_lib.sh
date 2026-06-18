#!/usr/bin/env bash
# _state_lib.sh — shared host-side helpers for resolving the PER-TOKEN state
# directory. Sourced by setup.sh, plan-promote.sh, and approve-publish.sh.
#
# Why: state under state/ is namespaced by a fingerprint of the GitHub token the
# owning container uses, so rotating a token swaps in a FRESH state tree and
# rotating back remounts the cached one — nothing is ever deleted. The mount
# TARGETS inside the container are unchanged (/workspace/target-state/…); only
# the host SOURCE moves to state/by-token/<fp>/<subtree>. Each env's state is
# keyed by ITS OWN token: research subtree by GH_TOKEN_RESEARCH, approved-plans
# and audit by GH_TOKEN_APPLY. scripts/gen_devcontainer.py computes the IDENTICAL
# fingerprint in Python — keep the two in sync (tests/test_state_fingerprint.py
# pins that they agree).

# Resolve our own repo root from this file's location, regardless of caller cwd.
_LEASH_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LEASH_REPO_ROOT="$(cd "${_LEASH_LIB_DIR}/.." && pwd)"

# Resolve creds.env the same way setup.sh documents: $LEASH_CREDS wins, then an
# in-tree creds.env (holds only pointers; never enters a container), then the
# home config dir.
leash_creds_path() {
  if [ -n "${LEASH_CREDS:-}" ]; then
    printf '%s\n' "${LEASH_CREDS}"
  elif [ -f "${LEASH_REPO_ROOT}/creds.env" ]; then
    printf '%s\n' "${LEASH_REPO_ROOT}/creds.env"
  else
    printf '%s\n' "${HOME}/.config/plan-apply-leash/creds.env"
  fi
}

# Source creds.env into the current shell so GH_TOKEN_*_FILE are populated.
# Safe to call repeatedly; a no-op if the file is absent.
leash_load_creds() {
  local creds; creds="$(leash_creds_path)"
  if [ -f "${creds}" ]; then
    set -a
    # shellcheck disable=SC1090
    source "${creds}"
    set +a
  fi
}

# sha256 of the exact (whitespace-stripped) token, first 16 hex chars. PATs hold
# no internal whitespace, so this matches Python's token.strip(); printf %s emits
# NO trailing newline, so bash and Python hash identical bytes. 16 hex chars is
# enough to avoid collisions and far too short to be a useful token leak.
leash_token_fp() {
  local token="$1"
  if command -v shasum >/dev/null 2>&1; then
    printf '%s' "${token}" | shasum -a 256 | cut -c1-16
  else
    printf '%s' "${token}" | openssl dgst -sha256 | sed 's/^.*= *//' | cut -c1-16
  fi
}

# Read+strip a token from the file named by a *_FILE env var and echo its
# fingerprint. Empty output if the var is unset/empty or the file missing/empty.
leash_fp_for_token_file_var() {
  local var="$1" path token
  path="${!var:-}"
  [ -n "${path}" ] || { printf ''; return; }
  path="${path/#\~/$HOME}"
  [ -f "${path}" ] || { printf ''; return; }
  token="$(tr -d '[:space:]' < "${path}")"
  [ -n "${token}" ] || { printf ''; return; }
  leash_token_fp "${token}"
}

# Echo the absolute per-token state root for a target (research|apply). Loads
# creds.env first. Prints nothing and returns non-zero if the token can't be
# resolved, so callers can `|| true` under `set -e` and check for emptiness.
leash_state_root() {
  local target="$1" var fp
  leash_load_creds
  case "${target}" in
    research) var="GH_TOKEN_RESEARCH_FILE" ;;
    apply)    var="GH_TOKEN_APPLY_FILE" ;;
    *) printf ''; return 2 ;;
  esac
  fp="$(leash_fp_for_token_file_var "${var}")"
  [ -n "${fp}" ] || { printf ''; return 1; }
  printf '%s\n' "${LEASH_REPO_ROOT}/state/by-token/${fp}"
}

# One-time migration from the pre-namespacing flat layout (state/research,
# state/approved-plans, state/audit) into the current tokens' trees, so existing
# notes/plans/audit aren't orphaned the first time namespacing takes effect. Only
# moves a legacy subtree when the destination doesn't already exist, so it's
# idempotent and never clobbers namespaced state. Research subtree → research
# token; approved-plans + audit → apply token.
leash_migrate_legacy_state() {
  leash_load_creds
  local rfp afp root sub
  rfp="$(leash_fp_for_token_file_var GH_TOKEN_RESEARCH_FILE)"
  afp="$(leash_fp_for_token_file_var GH_TOKEN_APPLY_FILE)"

  if [ -n "${rfp}" ] && [ -d "${LEASH_REPO_ROOT}/state/research" ]; then
    root="${LEASH_REPO_ROOT}/state/by-token/${rfp}"
    if [ ! -e "${root}/research" ]; then
      mkdir -p "${root}"
      mv "${LEASH_REPO_ROOT}/state/research" "${root}/research"
      printf '    migrated legacy state/research → state/by-token/%s/research\n' "${rfp}" >&2
    fi
  fi

  if [ -n "${afp}" ]; then
    root="${LEASH_REPO_ROOT}/state/by-token/${afp}"
    for sub in approved-plans audit; do
      if [ -d "${LEASH_REPO_ROOT}/state/${sub}" ] && [ ! -e "${root}/${sub}" ]; then
        mkdir -p "${root}"
        mv "${LEASH_REPO_ROOT}/state/${sub}" "${root}/${sub}"
        printf '    migrated legacy state/%s → state/by-token/%s/%s\n' "${sub}" "${afp}" "${sub}" >&2
      fi
    done
  fi
}
