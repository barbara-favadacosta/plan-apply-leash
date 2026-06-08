#!/usr/bin/env bash
# setup.sh — host-side diagnostic for the plan-apply-leash harness.
#
# Walks every prerequisite and prints a green/red checklist so you know
# exactly what's missing before opening a devcontainer. Also creates the
# target-state directory layout that the bind mounts expect to exist.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${REPO_ROOT}"

GREEN=$'\033[0;32m'; RED=$'\033[0;31m'; YEL=$'\033[0;33m'; DIM=$'\033[2m'; OFF=$'\033[0m'

PASS=0; FAIL=0; WARN=0; SKIP=0

ok()   { printf "  %s✓%s %s\n" "${GREEN}" "${OFF}" "$1"; PASS=$((PASS+1)); }
bad()  { printf "  %s✗%s %s\n" "${RED}"   "${OFF}" "$1"; FAIL=$((FAIL+1)); }
warn() { printf "  %s!%s %s\n" "${YEL}"   "${OFF}" "$1"; WARN=$((WARN+1)); }
skip() { printf "  %s–%s %s\n" "${DIM}"   "${OFF}" "$1"; SKIP=$((SKIP+1)); }
note() { printf "    %s%s%s\n" "${DIM}"  "$1" "${OFF}"; }

section() { printf "\n${DIM}── %s ──${OFF}\n" "$1"; }

# Single credentials file, kept OUTSIDE the workspace (so the research env can
# never read the apply token). Override the location with $LEASH_CREDS.
CREDS="${LEASH_CREDS:-$HOME/.config/plan-apply-leash/creds.env}"
export LEASH_CREDS="${CREDS}"

# ─── Docker engine ─────────────────────────────────────────────────────────
section "Docker engine"
if command -v docker >/dev/null 2>&1; then
  if docker info >/dev/null 2>&1; then
    ok "Docker daemon reachable ($(docker version --format '{{.Server.Version}}' 2>/dev/null || echo 'unknown'))"
  else
    bad "Docker installed but daemon not reachable — start Docker Desktop or OrbStack"
  fi
else
  bad "Docker not on PATH — install Docker Desktop or OrbStack"
fi

# ─── VS Code + Dev Containers extension ───────────────────────────────────
section "VS Code + Dev Containers extension"
if command -v code >/dev/null 2>&1; then
  ok "code CLI on PATH"
  if code --list-extensions 2>/dev/null | grep -q "ms-vscode-remote.remote-containers"; then
    ok "Dev Containers extension installed"
  else
    bad "Dev Containers extension missing"
    note "install: code --install-extension ms-vscode-remote.remote-containers"
  fi
else
  warn "code CLI not on PATH"
  note "in VS Code: Cmd+Shift+P → 'Shell Command: Install code command in PATH'"
fi

# ─── Python deps for plan-promote.sh ──────────────────────────────────────
section "Python deps for plan-promote.sh"
if command -v python3 >/dev/null 2>&1; then
  ok "python3 installed ($(python3 --version))"
  if python3 -c "import yaml, jsonschema" 2>/dev/null; then
    ok "pyyaml + jsonschema importable"
  else
    bad "pyyaml or jsonschema missing"
    note "fix: pip3 install --user pyyaml jsonschema"
  fi
else
  bad "python3 missing — install Python 3 first"
fi

# ─── Credentials file ─────────────────────────────────────────────────────
# Sourced in this shell (so $HOME/quotes are expanded) and handed to
# gen_devcontainer.py, which bakes the non-secret config into devcontainer.json
# and writes the per-env --env-file secret files outside the workspace.
section "Credentials file (${CREDS})"
if [ -f "${CREDS}" ]; then
  ok "creds file present: ${CREDS}"
  # shellcheck disable=SC1090
  set -a; source "${CREDS}"; set +a
else
  bad "creds file not found: ${CREDS}"
  note "fix: mkdir -p \"$(dirname "${CREDS}")\" && cp creds.env.example \"${CREDS}\" && \$EDITOR \"${CREDS}\""
fi

# ─── Repos config (repos.yaml → generated devcontainer.json) ──────────────
# repos.yaml's `apply:` lists the host paths apply may write (slug = basename),
# bind-mounted under /workspace/repos/<slug> (RW in apply, RO in research). Its
# optional `research:` list scopes what the research agent reads on GitHub. We
# render .devcontainer/{research,apply}/devcontainer.json from the templates.
section "Repos config (repos.yaml → devcontainer mounts + research scope)"

if [ ! -f "${REPO_ROOT}/repos.yaml" ]; then
  bad "repos.yaml not found"
  note "fix: cp repos.yaml.example repos.yaml && \$EDITOR repos.yaml"
else
  ok "repos.yaml present"
  if gen_output=$(python3 "${REPO_ROOT}/scripts/gen_devcontainer.py" 2>&1); then
    ok "devcontainer.json regenerated for research and apply"
    # Echo the generator's per-repo summary and research-scope line.
    echo "${gen_output}" | sed -n 's/^  • /    • /p'
    echo "${gen_output}" | sed -n 's/^  research-scope: /    research scope: /p'
    echo "${gen_output}" | sed -n 's/^  platforms: /    platforms: /p'
    # Surface any non-fatal warnings (e.g. an apply repo with no GitHub remote).
    echo "${gen_output}" | sed -n 's/^gen-devcontainer: WARNING: /    ! /p'
  else
    bad "gen_devcontainer.py failed:"
    while IFS= read -r line; do note "${line}"; done <<< "${gen_output}"
  fi
fi

# ─── Target-state scaffolding ─────────────────────────────────────────────
section "Target-state directory scaffolding"
for sub in research/drafts research/clones research/notes approved-plans approved-plans/history audit; do
  full="${REPO_ROOT}/target-state/${sub}"
  if [ -d "${full}" ]; then
    ok "target-state/${sub}/ exists"
  else
    mkdir -p "${full}" 2>/dev/null && ok "created target-state/${sub}/" || bad "could not create ${full}"
  fi
done

# ─── Research env: GitHub (required) ──────────────────────────────────────
# creds.env stores a *path* to a file that holds the PAT, not the token itself.
section "Research env — GitHub PAT (required)"
if [ -z "${GH_TOKEN_RESEARCH_FILE:-}" ]; then
  bad "GH_TOKEN_RESEARCH_FILE unset"
  note "set it in ${CREDS}: export GH_TOKEN_RESEARCH_FILE=\"\$HOME/.config/plan-apply-leash/gh-research.token\""
  note "the file holds just the PAT; see examples/github-pat-scopes.md"
elif [ ! -f "${GH_TOKEN_RESEARCH_FILE}" ]; then
  bad "GH_TOKEN_RESEARCH_FILE points at a missing file: ${GH_TOKEN_RESEARCH_FILE}"
else
  GH_TOKEN_RESEARCH="$(tr -d '[:space:]' < "${GH_TOKEN_RESEARCH_FILE}")"
  if [ -n "${GH_TOKEN_RESEARCH}" ]; then
    ok "GH_TOKEN_RESEARCH_FILE → ${GH_TOKEN_RESEARCH_FILE} (${GH_TOKEN_RESEARCH:0:8}...)"
  else
    bad "GH_TOKEN_RESEARCH_FILE file is empty: ${GH_TOKEN_RESEARCH_FILE}"
  fi
fi

# ─── Research env: platform access (optional) ─────────────────────────────
# AWS, Kubernetes, or anything else the research agent reads — declared in
# research-access.yaml and resolved + validated above by gen_devcontainer.py
# (it fails the step above on a bad entry). Each credential is bind-mounted
# READ-ONLY; scope it read-only on the platform side too — the harness mounts
# read-only on the filesystem but can't prove the credential itself is.
section "Research env — platform access (optional, research-access.yaml)"
if [ ! -f "${REPO_ROOT}/research-access.yaml" ]; then
  skip "no research-access.yaml (research uses GitHub only)"
  note "to enable: cp research-access.yaml.example research-access.yaml && \$EDITOR research-access.yaml"
elif ! grep -qE '^[[:space:]]*-[[:space:]]+name:' "${REPO_ROOT}/research-access.yaml" 2>/dev/null; then
  skip "research-access.yaml present but lists no platforms"
else
  ok "research-access.yaml present — platforms resolved + validated above"
  note "each credential is mounted read-only; scope it read-only on the platform side too"
fi

# ─── Apply env: GitHub (required) ─────────────────────────────────────────
section "Apply env — GitHub PAT (the only credential apply uses)"
if [ -z "${GH_TOKEN_APPLY_FILE:-}" ]; then
  bad "GH_TOKEN_APPLY_FILE unset"
  note "set it in ${CREDS}: export GH_TOKEN_APPLY_FILE=\"\$HOME/.config/plan-apply-leash/gh-apply.token\""
  note "scope the token tightly — see examples/github-pat-scopes.md"
elif [ ! -f "${GH_TOKEN_APPLY_FILE}" ]; then
  bad "GH_TOKEN_APPLY_FILE points at a missing file: ${GH_TOKEN_APPLY_FILE}"
else
  GH_TOKEN_APPLY="$(tr -d '[:space:]' < "${GH_TOKEN_APPLY_FILE}")"
  if [ -n "${GH_TOKEN_APPLY}" ]; then
    ok "GH_TOKEN_APPLY_FILE → ${GH_TOKEN_APPLY_FILE} (${GH_TOKEN_APPLY:0:8}...)"
  else
    bad "GH_TOKEN_APPLY_FILE file is empty: ${GH_TOKEN_APPLY_FILE}"
  fi
fi

# ─── Live GitHub token check ──────────────────────────────────────────────
if command -v curl >/dev/null 2>&1; then
  for var in GH_TOKEN_RESEARCH GH_TOKEN_APPLY; do
    token="${!var:-}"
    [ -z "${token}" ] && continue
    resp=$(curl -sS -o /tmp/gh-check.$$.json -w "%{http_code}" \
      -H "Authorization: Bearer ${token}" \
      -H "Accept: application/vnd.github+json" \
      https://api.github.com/user 2>/dev/null || echo "000")
    case "${resp}" in
      200)
        user=$(python3 -c "import json; print(json.load(open('/tmp/gh-check.$$.json'))['login'])" 2>/dev/null || echo "?")
        ok "${var} authenticates as ${user}"
        ;;
      401|403) bad "${var} rejected by GitHub (HTTP ${resp})" ;;
      000)     warn "${var} could not reach GitHub (network issue?)" ;;
      *)       warn "${var} returned HTTP ${resp}" ;;
    esac
    rm -f /tmp/gh-check.$$.json
  done
fi

# ─── Project layout sanity ────────────────────────────────────────────────
section "Harness layout sanity"
for d in .devcontainer/research .devcontainer/apply hooks plans scripts; do
  if [ -d "${d}" ]; then ok "${d}/ exists"; else bad "${d}/ missing"; fi
done

# ─── Summary ──────────────────────────────────────────────────────────────
echo
printf "${DIM}──────────────────────────────────────${OFF}\n"
printf "%spassed:%s %d   %sfailed:%s %d   %swarnings:%s %d   %sskipped (optional):%s %d\n" \
  "${GREEN}" "${OFF}" "${PASS}" "${RED}" "${OFF}" "${FAIL}" "${YEL}" "${OFF}" "${WARN}" "${DIM}" "${OFF}" "${SKIP}"
if [ "${FAIL}" -gt 0 ]; then
  echo "fix the failures above, then re-run scripts/setup.sh"
  exit 1
fi
echo "ready to go — open this folder in VS Code (any way you like) and 'Reopen in Container'."
echo "after editing repos.yaml or your creds file, re-run this script and 'Rebuild Container'."
