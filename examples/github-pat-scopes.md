# GitHub PAT scopes for the research/apply harness

Create **two fine-grained personal access tokens** (not classic PATs) at:
<https://github.com/settings/tokens?type=beta>

Fine-grained tokens let you constrain scope per-repository and per-permission,
which is what makes the two-env design actually safer than classic PATs.

---

## `GH_TOKEN_RESEARCH` — broad read

Goal: let the research agent investigate any repo in your org(s) without
being able to write anything.

- **Token name**: `leash-research`
- **Resource owner**: your GitHub username **and/or** each org you want to research
- **Expiration**: 30 or 90 days (rotate regularly)
- **Repository access**: **All repositories**
- **Repository permissions** (set the rest to "No access"):
  - Contents: **Read-only**
  - Metadata: **Read-only** *(mandatory — GitHub requires this on any token)*
  - Pull requests: **Read-only**
  - Issues: **Read-only**
  - Actions: **Read-only** *(only if research needs to look at CI runs)*
- **Account permissions**: usually none. If you need to investigate orgs:
  - Members: **Read-only** *(optional)*
- **Organization permissions**: **No access** for almost everything.

This token cannot push, cannot open PRs, cannot modify CI, cannot change
secrets, cannot manage teams. If it leaks, the worst outcome is someone
reading code you already have read access to.

---

## `GH_TOKEN_APPLY` — narrow write

Goal: let the apply agent push commits, open PRs, and modify files **only
on a small set of repos** corresponding to your plans' `scope.repos`.

- **Token name**: `leash-apply`
- **Resource owner**: the org or user that owns the target repos
- **Expiration**: 30 days max (rotate aggressively — this one has write power)
- **Repository access**: **Only select repositories** → pick exactly the repos
  listed in any plan's `scope.repos`. Do NOT pick "All repositories."
- **Repository permissions**:
  - Contents: **Read and write**
  - Metadata: **Read-only**
  - Pull requests: **Read and write**
  - Issues: **Read and write** *(optional, only if plans open issues)*
  - Actions: **No access** *(unless plans must edit workflows — usually no)*
  - Workflows: **No access** *(critical — workflow scope means CI changes,
    which can leak secrets to attackers via PRs)*
- **Account permissions**: none.
- **Organization permissions**: none.

If this token leaks, damage is limited to: write access to the specific
repos you listed, no workflow changes, no org-wide changes. Pair this with
**branch protection** on `main` server-side so even the apply env can't push
directly — it has to open a PR.

---

## After generating the tokens

Write each token to its own file (outside the repo) and point `creds.env` at
the paths — `creds.env` itself never holds token text:

```bash
printf '%s' 'github_pat_READ...'  > ~/.config/plan-apply-leash/gh-research.token
printf '%s' 'github_pat_WRITE...' > ~/.config/plan-apply-leash/gh-apply.token
chmod 600 ~/.config/plan-apply-leash/gh-*.token

# in ~/.config/plan-apply-leash/creds.env:
export GH_TOKEN_RESEARCH_FILE="$HOME/.config/plan-apply-leash/gh-research.token"
export GH_TOKEN_APPLY_FILE="$HOME/.config/plan-apply-leash/gh-apply.token"
```

Then run `scripts/setup.sh` to verify both tokens authenticate correctly. It
reads each PAT from its file and writes it to a per-env `--env-file` that Docker
injects at container-create time, so the tokens never enter the workspace.

---

## Rotation discipline

- Set calendar reminders for token expiration. Tokens silently start
  returning 401 the moment they expire.
- Treat token leakage like a credential incident: revoke at
  <https://github.com/settings/tokens?type=beta>, generate a new one, overwrite
  the token file, re-run `scripts/setup.sh`, then **Rebuild Container**.
- Never put real token values in any file inside the project tree — keep the
  token files (and `creds.env`) outside the repo. The project folder is mounted
  into both containers, so a token in the tree is readable from the read-only env.

---

## If you don't want to maintain tokens

For a team workflow, replace fine-grained PATs with a **GitHub App** installed
on the org. Apps:

- Issue short-lived (1-hour) installation tokens
- Scope per-installation, not per-user
- Don't tie credentials to a person leaving the org
- Have a clearer audit trail in GitHub's logs

The setup is heavier (you generate a private key, sign a JWT, exchange for
an installation token) but the security model is meaningfully stronger.
Out of scope for this starter; mentioned so you know where to go when you
outgrow PATs.
