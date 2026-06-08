# plan-apply-leash

**Let a coding agent run plan-and-apply unattended — on a token leash.** Turn on
bypass / auto-approve mode, point it at your repos, and walk away: no babysitting,
no approving every edit and shell command. You can leave it running until it runs
out of work, because the worst it can do is bounded by the credentials you handed
it — not by how closely you watched.

It mirrors Terraform's `plan` / `apply`, but for a coding agent, and it works for
one repo or many:

1. **Research / plan** — read-only. The agent explores your code and drafts a
   plan. Bypass mode is safe here: its credentials can't write anything.
2. **Apply** — write-scoped. The agent executes the *approved* plan: edits,
   tests, commits, and opens one PR per repo. Bypass mode is safe here too: its
   token can only touch the repos the plan named.

A human steps in exactly once — **between** the two phases, to review the plan.
Everything else runs unattended. The leash is the **scoped token**, not a
permission prompt; that is what makes running the agent wide open actually fine.

> The "leash" is a fine-grained GitHub PAT you grant the agent (not an LLM or API
> token). Scope it tightly and the agent can do exactly that much, and no more.

## Works with any coding agent

The security model doesn't depend on which agent you run. The boundary is the
GitHub token scope and the container — both constrain *any* process inside them,
whether that's Claude Code, another CLI agent, or your own script.

The setup in this repo is a **reference implementation that runs Claude Code**
(it ships the devcontainers, the bypass-mode config, and per-call hooks wired to
Claude Code). To run a different agent, drop it into the same containers; the
token scope, read-only mounts, capability limits, and egress firewall all still
hold. The one Claude-specific piece — the per-call hook that gates each tool
call against the plan — is defense-in-depth, not the boundary, and you'd re-wire
it to your agent's equivalent. See [Why this is safe under bypass
mode](#why-this-is-safe-under-bypass-mode).

## One repo or many

Use it for a single repo or coordinate a change across many — the workflow is
the same.

- **One repo.** Point it at a single project and let it run autonomously on a
  bounded change.
- **Many repos.** The sweet spot: a change that has to land the same way
  everywhere. *Example:* your team owns 15 services and you need to update a
  shared config (a CI setting, a linter rule, a dependency pin) in every one. The
  agent makes the edit in each repo and opens one PR per repo, coordinated under
  a single plan.

You list the repos in `repos.yaml`; one entry or twenty, the harness treats them
the same.

## Is this for you?

**Use this if you're:**
- Running a coding agent against real code with real credentials.
- Making a bounded change to one repo, or the same change across many.
- Tired of approving every tool call but uncomfortable with naked bypass mode.

**Skip if you're:**
- On a toy project without real credentials.
- Doing a one-off session where approving each call is fine.
- Sandboxing hostile code (use a VM — Docker shares the kernel).
- Building a multi-tenant agent platform (this is a single-user tool).

---

## Mental model

Two devcontainers, one human checkpoint between them.

```
┌──────────────────────┐    human          ┌─────────────────────────┐
│    research env      │   review +        │       apply env         │
│  ──────────────────  │  plan-promote     │  ─────────────────────  │
│  GitHub (read, all)  │ ─────────────────▶│  GitHub (write, scoped) │
│  platforms: read RO  │                   │  Edits + PRs (1+ repos) │
│  (research-access)   │                   │  Hook-gated per call    │
│  repos mounted RO    │                   │  repos mounted RW       │
└──────────────────────┘                   └─────────────────────────┘
       draft plan                                  edits + commits + PRs
```

The handoff is a YAML plan. Shape (full example at
[`plans/example-plan.yaml`](plans/example-plan.yaml)):

<details>
<summary>What a plan looks like (8-line excerpt)</summary>

```yaml
plan_id: 2026-05-22-bump-logger
scope:
  repos:
    foo-service:
      github: acme/foo-service
      branch: feature/bump-logger-v2
      pr_title: "Bump @acme/logger to v2"
      file_paths: [package.json, src/logger.ts, src/handler.ts]
    bar-cli:
      github: acme/bar-cli
      branch: feature/match-new-logger
      file_paths: [go.mod, go.sum, cmd/main.go]
  allowed_command_prefixes: [npm install, npm test, git push origin, gh pr create]
steps:
  - { id: 1, repo: foo-service, type: file_edit, path: package.json }
  # ...
```
</details>

A single-repo plan is the same shape with one entry under `scope.repos`.
Anything outside `scope.repos.*.file_paths` or `allowed_command_prefixes` is
blocked at hook time during apply.

---

## Hello world (≈10 minutes, one repo, trivial change)

End-to-end so you can picture the cycle before the longer setup:

1. `git clone <this repo> ~/code/plan-apply-leash && cd ~/code/plan-apply-leash`
2. `cp repos.yaml.example repos.yaml`
3. Edit `repos.yaml` to one entry:
   ```yaml
   apply:
     - /Users/you/code/my-project
   ```
4. Create your credentials **outside the repo**, write each GitHub fine-grained
   PAT to its own file, and point `creds.env` at them:
   ```bash
   mkdir -p ~/.config/plan-apply-leash
   cp creds.env.example ~/.config/plan-apply-leash/creds.env
   printf '%s' 'github_pat_READ...'  > ~/.config/plan-apply-leash/gh-research.token
   printf '%s' 'github_pat_WRITE...' > ~/.config/plan-apply-leash/gh-apply.token
   chmod 600 ~/.config/plan-apply-leash/gh-*.token
   # creds.env already points GH_TOKEN_*_FILE at those paths — edit if you moved them
   ```
5. `scripts/setup.sh` — expect all-green (sample output below).
6. Open the folder in VS Code however you like (Dock, recent, `code .`) →
   `Cmd+Shift+P` → **Reopen in Container** → **leash-research**. First time in
   this container, run `claude` and log in with your Claude account (once per
   env — it persists across rebuilds).
7. Ask the agent:
   > *"Draft a plan to add a 'Quickstart' sentence under the README intro in
   > my-project. Write it to
   > `/workspace/target-state/research/drafts/quickstart.yaml`."*
8. `Cmd+Shift+P` → **Reopen Folder Locally**, then on the host:
   ```bash
   scripts/plan-promote.sh quickstart.yaml
   ```
   Inspect the diff, type `y`.
9. **Reopen in Container** → **leash-apply** (first time here, run `claude` and
   log in again — apply is a separate env with its own auth volume). Tell the
   agent:
   > *"Execute the plan at
   > `/workspace/target-state/approved-plans/current.yaml`."*
10. Back to host: a PR is open on `my-project`. Review and merge.

If anything in step 5 fails, see [Troubleshooting](#troubleshooting).

---

## Prerequisites

- **macOS** or **Linux** host
- **Docker Desktop** or **OrbStack**
- **VS Code** + the **Dev Containers** extension
- **Python 3** with `pyyaml` and `jsonschema`:
  ```bash
  pip3 install --user pyyaml jsonschema
  ```
- **GitHub account** for fine-grained PATs (required for both envs)
- A coding agent that supports a non-interactive / auto-approve mode. The
  reference setup uses **Claude Code** (`--dangerously-skip-permissions`) and
  installs it into both containers for you.
  - **Anthropic login — uses your own Claude account.** There is deliberately
    no `ANTHROPIC_API_KEY` in `creds.env`. The first time you open each
    container, run `claude` and complete the login (it prints a URL to open on
    your host and a code to paste back; sign in with the Claude account you
    already use). Each env keeps its login in its own Docker volume
    (`agent-auth-research` / `agent-auth-apply`, mounted at `~/.claude`), so you
    authenticate **once per env** and it survives container rebuilds. Both
    egress allowlists permit `claude.ai` and `api.anthropic.com`, so login and
    the agent work behind the proxy. (Prefer an API key instead? Export
    `ANTHROPIC_API_KEY` in the container — but the account login is the
    supported path.)
- Optional: read-only platform access for research (AWS, Kubernetes, or
  anything reachable over HTTP), declared in `research-access.yaml` — see
  [Read-only platform access](#4-optional-read-only-platform-access-for-research)

---

## One-time setup

### 1. Clone, copy templates

```bash
git clone <this repo> ~/code/plan-apply-leash
cd ~/code/plan-apply-leash
cp repos.yaml.example repos.yaml
mkdir -p ~/.config/plan-apply-leash
cp creds.env.example ~/.config/plan-apply-leash/creds.env
```

All credentials and config live in **one file, `creds.env`, kept outside the
repo** (default `~/.config/plan-apply-leash/creds.env`; override with
`$LEASH_CREDS`). The variables in `creds.env.example`:

| Variable                 | Purpose                                                          |
| ------------------------ | ---------------------------------------------------------------- |
| `GH_TOKEN_RESEARCH_FILE` | **Required.** Path to a file holding the read-only PAT.          |
| `GH_TOKEN_APPLY_FILE`    | **Required.** Path to a file holding the write, repo-scoped PAT. |
| `ALLOWED_DOMAINS_EXTRA`  | Optional space-separated extra egress domains for research.      |

Read-only platform access for research (AWS, Kubernetes, anything else) is **not**
in `creds.env` — it's a structured list, declared in
[`research-access.yaml`](#4-optional-read-only-platform-access-for-research).

The **Anthropic credential is not in `creds.env` either.** Claude Code signs in
with your own Claude account interactively (`claude` login) and stores that
login in a per-env Docker volume mounted at `~/.claude` — never in the repo or
the creds file. You do it once per container; see [Prerequisites](#prerequisites).

Every entry in `creds.env` is a **pointer or plain config — never a secret
value**. The GitHub entries point at files holding the PATs (just as a
`research-access.yaml` platform points `credential:` at a host dir or file).

> ### Why credentials live outside the repo
>
> Both containers bind-mount this project folder at `/workspace`, and the
> research container can **read** everything there — that's the point; it reads
> your code. So if a token file sat anywhere in the project tree, the read-only
> research env could read the **write-scoped apply token** and use it. That one
> leak would collapse the whole research/apply split.
>
> The rule that prevents it is simple: **nothing secret ever goes inside the
> repo.** Concretely:
>
> - `creds.env` lives outside the tree (default `~/.config/plan-apply-leash/`).
> - Even `creds.env` holds no token text — only paths to the token files (also
>   outside the tree) plus non-secret config like extra egress domains.
> - `scripts/setup.sh` runs on your **host**, not in a container. It reads
>   `creds.env`, follows the pointers to load each PAT, and hands the tokens to
>   Docker through a per-container `--env-file` kept outside `/workspace`.
>
> The result: the agent sees `GH_TOKEN` as an environment variable, but there is
> no token file anywhere it can read or copy. Because all of this happens on the
> host before the container starts, "Reopen in Container" works the same on macOS
> and Linux regardless of how you launched VS Code.

### 2. List your repos in `repos.yaml`

`repos.yaml` has two keys: `apply` (required) and `research` (optional).

```yaml
# apply: the repos the APPLY agent may write to. One entry or many.
apply:
  - /Users/you/code/foo-service
  - /Users/you/code/bar-cli

# research: OPTIONAL. GitHub repos the RESEARCH agent may read. Omit to disable.
research:
  - acme/shared-config
```

**`apply` (required)** — each path mounts at `/workspace/repos/<basename>/`
(read-write in apply, read-only in research). The basename is the slug your plan
references in `scope.repos.<slug>`. Same-basename collisions are a hard error —
rename one locally. This list is also an **enforced allowlist**: `plan-promote`
(and the apply container at startup) reject any plan whose `scope.repos` names a
slug not listed here, so what apply can touch is bounded at the config level,
not only by the write token. (`repos:` still works as a deprecated alias.)

**`research` (optional)** — GitHub repositories (`owner/repo`, exactly as on
GitHub; they need *not* be cloned locally) the research agent is allowed to read
over the API.

- **Non-empty** → research is told to look **only** at these, even though its
  read-only PAT can reach more. Useful when the PAT spans a whole personal
  account but this project is only a few of those repos. Your `apply` repos are
  **auto-included** (resolved from each one's GitHub `origin` remote), so your
  change targets are always in scope.
- **Empty / omitted** → research may read anything its read-only PAT can reach
  (the original behavior).

This is a **focus guardrail**, not a hard boundary: it's injected into the
research agent's context, and research credentials are read-only. The only true
boundary on GitHub reach is a PAT scoped to these repos — scope the token too if
the separation matters for security rather than just focus.

### 3. Create GitHub fine-grained PATs

Detailed scopes in
[`examples/github-pat-scopes.md`](examples/github-pat-scopes.md). Write each to
its own file and point `creds.env` at them:

- `GH_TOKEN_RESEARCH_FILE` → a file with a PAT that reads across the orgs you
  investigate.
- `GH_TOKEN_APPLY_FILE` → a file with a PAT that writes, **scoped to exactly the
  repos your plans will modify**.

This is the boundary. Scope `GH_TOKEN_APPLY` like production IAM — anything
beyond what the plan needs, the harness cannot make safe.

### 4. (Optional) Read-only platform access for research

If you want the research agent to read AWS, Kubernetes, or any other platform,
copy `research-access.yaml.example` to `research-access.yaml` and list one
`platforms:` entry per credential. Omit the file and research runs with GitHub
only. **The apply env never reads it** — apply has a scoped GitHub token and
nothing else.

```yaml
platforms:
  - name: aws
    install: aws-cli              # Tier B: catalog CLI via a devcontainer Feature
    credential: ~/.aws            # host dir, bind-mounted READ-ONLY
    env: { AWS_PROFILE: research-readonly, AWS_REGION: us-east-1 }

  - name: gcp                     # Tier A: no CLI — agent reads the API over HTTP
    credential: ~/.config/gcloud
    mount_at: /home/dev/.config/gcloud
    allow_domains: [oauth2.googleapis.com, cloudresourcemanager.googleapis.com]
```

Each platform is one of two tiers:

- **Tier A — config-only (no CLI).** Omit `install`. Mount the credential, set
  `env`, list `allow_domains`. The agent reaches the platform over HTTP; nothing
  is added to the image. Best for REST APIs and internal services.
- **Tier B — a known CLI from the catalog.** `install: <name>` pulls a
  **devcontainer Feature** that installs the CLI, plus default `mount_at` and
  egress domains. The catalog ships official Features for `aws-cli`, `kubectl`,
  and `azure-cli` (see `CATALOG` in
  [`scripts/research_access.py`](scripts/research_access.py)). An `install:`
  value not in the catalog is a hard error — there is no arbitrary-installer
  escape hatch.

Two things this asks of you:

- **You own the read-only scoping.** The harness mounts the credential read-only
  on the *filesystem*, but it can't prove the credential itself is read-only.
  Scope it like the apply PAT. Starter least-privilege policies:
  [`examples/iam-research-policy.json`](examples/iam-research-policy.json) (for
  `aws-cli`) and
  [`examples/kubectl-readonly-rbac.yaml`](examples/kubectl-readonly-rbac.yaml)
  (for `kubectl`, which authenticates with a static read-only ServiceAccount
  token).
- **Tier B trusts the Feature publisher.** The shipped catalog uses official
  `devcontainers/features` images; if you add a community Feature, pin it by
  digest. See [Honest limits](#honest-limits).

### 5. Verify

```bash
scripts/setup.sh
```

A clean run looks roughly like this (truncated):

```
── Docker engine ──
  ✓ Docker daemon reachable (24.0.7)

── VS Code + Dev Containers extension ──
  ✓ code CLI on PATH
  ✓ Dev Containers extension installed

── Python deps for plan-promote.sh ──
  ✓ python3 installed (Python 3.11.6)
  ✓ pyyaml + jsonschema importable

── Credentials file (/Users/you/.config/plan-apply-leash/creds.env) ──
  ✓ creds file present: /Users/you/.config/plan-apply-leash/creds.env

── Repos config (repos.yaml → devcontainer mounts) ──
  ✓ repos.yaml present
  ✓ devcontainer.json regenerated for research and apply
    • foo-service → /workspace/repos/foo-service
    • bar-cli     → /workspace/repos/bar-cli
    platforms: (none — research uses GitHub only)

── Research env — GitHub PAT (required) ──
  ✓ GH_TOKEN_RESEARCH_FILE → ~/.config/plan-apply-leash/gh-research.token (github_p...)

── Research env — platform access (optional, research-access.yaml) ──
  – no research-access.yaml (research uses GitHub only)

── Apply env — GitHub PAT ──
  ✓ GH_TOKEN_APPLY_FILE → ~/.config/plan-apply-leash/gh-apply.token (github_p...)

  ✓ GH_TOKEN_RESEARCH authenticates as you-on-github
  ✓ GH_TOKEN_APPLY authenticates as you-on-github

──────────────────────────────────────
passed: 15   failed: 0   warnings: 0   skipped (optional): 1
ready to go — open this folder in VS Code (any way you like) and 'Reopen in Container'.
```

Red `✗` blocks you; yellow `!` is non-fatal but worth fixing.

### 6. Open VS Code on the harness folder

Open the folder any way you normally do — Dock/Start menu, a recent-folders
entry, or `code .` from a terminal. Credentials are wired up on the host before
the container starts, so the GUI path works the same on macOS and Linux.

After editing `repos.yaml`, `research-access.yaml`, or `creds.env` later, re-run
`scripts/setup.sh` and then **Rebuild Container** to pick up the changes.

---

## Daily workflow

### Step 1 — research

`Cmd+Shift+P` → **Dev Containers: Reopen in Container** → **leash-research**.

The post-create banner lists every mounted repo and confirms which credentials
are wired up. Ask the agent to investigate and draft a plan to
`/workspace/target-state/research/drafts/<plan_id>.yaml` matching
[`plans/schema.json`](plans/schema.json).

Bypass / auto-approve mode is safe here — credentials are read-only.

### Step 2 — human review & promotion

`Cmd+Shift+P` → **Reopen Folder Locally**.

```bash
scripts/plan-promote.sh <plan_id>.yaml
```

Validates schema + heuristics, shows a per-repo compiled allowlist preview,
diffs against the current approved plan, asks for confirmation, then atomically
promotes to `target-state/approved-plans/current.yaml`.

**This is the trust boundary.** Your review catches coordination mistakes —
across one repo or many — that no hook can.

### Step 3 — apply

`Cmd+Shift+P` → **Reopen in Container** → **leash-apply**.

The session-start banner lists every in-scope repo with its GitHub identifier,
branch, and file count. Tell the agent:

> *"Execute the plan at `/workspace/target-state/approved-plans/current.yaml`.
> If a step fails, stop and report — don't improvise. Complete each repo's
> changes before pushing."*

For each in-scope repo, the agent edits and runs tests autonomously. Every
`Bash`/`Edit`/`Write`/`NotebookEdit` runs through the PreToolUse hook against
the per-repo allowlist. The tally goes to `target-state/audit/tally.jsonl`; the
Stop hook prints a per-turn summary.

**Publish checkpoint.** Editing is autonomous, but `git commit`, `git push`, and
`gh pr create` are **paused** until you approve — the agent stops, summarizes
what it changed, and asks. Review the local changes, then on the host (or a
non-agent shell in the container) run:

```bash
scripts/approve-publish.sh
```

and tell the agent to continue; it then commits, pushes, and opens the PR.
`approve-publish.sh` writes a sentinel at
`target-state/audit/publish-approved`; the agent can't create it (its own writes
to `target-state/` are denied), which is what makes the pause real. One approval
covers the rest of the session — the apply container deletes the sentinel on
every start, so each session begins paused again.

The gate is on by default. To publish fully autonomously, set
`APPLY_REQUIRE_PUBLISH_APPROVAL` to `0` in the `containerEnv` block of
[`.devcontainer/apply/devcontainer.template.json`](.devcontainer/apply/devcontainer.template.json)
(it ships as `"1"`), then **Rebuild Container**. Edit the *template*, not the
generated `devcontainer.json` — `setup.sh` regenerates the latter from it.

### Step 4 — review the PRs

Back to host. Each in-scope repo has its branch pushed and a PR open. Audit
history is preserved in `target-state/audit/`.

---

## What gets blocked (concrete example)

When the agent tries something outside the plan, the PreToolUse hook fails the
call and feeds this back to the agent:

```
[apply-harness] BLOCKED: Edit on '/workspace/repos/foo-service/.github/workflows/ci.yml'
                is outside scope.file_paths (8 allowed)
```

```
[apply-harness] BLOCKED: Bash command does not match any allowed_command_prefixes
                (10 allowed)
```

```
[apply-harness] BLOCKED: Bash command contains a disallowed shell operator:
                background / chain '&' or '&&'
```

Because `allowed_command_prefixes` is a **prefix** match, the hook requires each
Bash call to be a single, simple command: no `;`, `&&`, `|`, redirection (`>`),
command substitution (`` ` `` / `$(…)`), or newlines. Otherwise an allowed
prefix like `npm test` could smuggle work past the gate (`npm test && curl … |
sh`, or `npm test > some/other/file` to write outside `scope.file_paths`, which
is only enforced on the `Edit`/`Write` tools). Run one allowed command at a
time; if a step genuinely needs a pipeline, wrap it in a script the plan
allow-lists as its own prefix.

The agent reads the error, stops, and surfaces it to you. To proceed: either
widen the plan and re-promote, or reject the request — the agent is
overreaching. Every allow/block is recorded in
`target-state/audit/tally.jsonl`, keyed by session ID.

---

## Credential matrix

|              | GitHub          | Platforms (AWS, k8s, …)                       | Bypass-mode safe? |
| ------------ | --------------- | --------------------------------------------- | ----------------- |
| **research** | required, read  | optional, read-only (`research-access.yaml`)  | yes (read-only)   |
| **apply**    | required, write | **none**                                      | yes (token-scoped)|

Apply gets a scoped GitHub token and **no platform access at all** — no
`research-access.yaml`, and no platform CLI in its image. That's defense in
depth on top of token scoping: a misbehaving apply agent has nothing to shell
out to even if a credential were ever misconfigured. If a plan mentions AWS/k8s
work it's informational — run it outside the harness.

---

## Why this is safe under bypass mode

In order of "what survives if the previous layer fails":

1. **Token scope (server-side, strongest, agent-agnostic).** Your
   `GH_TOKEN_APPLY` only accepts writes to its scoped repos. GitHub's API
   rejects everything else. Holds against every other layer failing — this is
   the boundary, and it constrains any agent identically.
2. **Capability absence (agent-agnostic).** Apply has no platform CLIs (no
   `aws-cli`, no `kubectl`, …) and no platform credentials. Nothing to invoke.
3. **Container + filesystem boundaries (agent-agnostic).** Research mounts
   repos read-only; an egress allowlist drops everything outside a handful of
   domains (the Anthropic and GitHub APIs, the base package registries, plus
   each research platform's declared `allow_domains`). Filtering is by
   **hostname**, via a local Squid forward proxy that every client reaches
   through `HTTPS_PROXY`: it allows each connection on the destination host in
   the `CONNECT` (so `.amazonaws.com` and other rotating-IP cloud endpoints work
   reliably) without decrypting traffic. Direct egress is dropped except to the
   pinned IPs of the stable base domains, a fallback so the agent runtime keeps
   working if it ignores the proxy variable.
4. **Per-call hook against the approved plan (reference-impl layer).** In the
   Claude Code setup, every gated tool call is checked against
   `compiled.repos[<slug>].file_paths` and `allowed_command_prefixes`. This is
   the one Claude-specific layer; run a different agent and you'd re-wire it to
   that agent's hook mechanism. It's a tripwire on top of the boundary, not the
   boundary itself.
5. **Your human review at promotion (agent-agnostic).** What catches the things
   hooks can't — the plan that passes the schema but modifies the wrong files.
   Promotion (and the apply container at startup) also rejects any plan whose
   `scope.repos` names a repo outside your `repos.yaml` `apply:` list, so a plan
   can't even propose touching a repo you never declared.

The worst the agent can do is whatever its tokens authorize. Scope tokens like
the agent will hand them to a stranger; in bypass mode, functionally that's
what happens.

---

## Cost notes

Bypass-mode apply runs can burn through model tokens fast — the agent is
reading, editing, testing, committing, and pushing without interruption.

- **Research:** comparable to any other long agent session.
- **Apply:** scales roughly with `len(repos) × file_paths × steps`. A 2-repo
  plan with ~5 files each lands around 150–400k tokens depending on test
  verbosity.

On a metered plan, run apply against one repo at a time until you have a feel
for what your plans cost.

---

## Honest limits

- **Docker is not a sandbox.** Containers share the host kernel. For hostile
  inputs use a VM.
- **Prompt-injection detection is not robust.** The schema is the real
  defense — anything not expressible in `scope.repos`/`file_paths`/
  `allowed_command_prefixes` cannot execute. Heuristics are a tripwire.
- **The plan is attacker-controlled input from apply's POV.** Your review at
  promotion time is the defense. Skim it and the harness gives you nothing.
- **Cross-repo coordination is the agent's responsibility.** If the plan says
  "A in foo, B in bar" and the agent does B in foo and A in bar, file paths
  alone won't catch that — your per-PR review will.
- **Hooks are in-process** and agent-specific. Token scoping is what survives a
  fully compromised harness.
- **A research platform credential is only as read-only as you scoped it.** The
  harness mounts it read-only on the filesystem but can't prove the credential
  itself can't write. Scope it like the apply PAT (least-privilege IAM, a
  read-only ServiceAccount). This is your responsibility, not the harness's.
  The research env runs a read-only self-test at startup (`aws sts
  get-caller-identity`, `kubectl auth can-i create namespaces`) so you can see
  reachability and confirm the credential, but proving the *full* IAM scope
  still needs a live `aws iam simulate-principal-policy` — the banner prints the
  exact command.
- **The egress proxy filters by hostname, not by content.** Squid allows each
  connection on its `CONNECT` host and tunnels TLS without decrypting it (no
  MITM, no injected cert). That's the right privacy posture, but it means the
  proxy can't stop exfiltration *to an allowed domain*. The default `aws-cli`
  allowlist is the wildcard `.amazonaws.com` — convenient, but any
  AWS-hosted endpoint (an attacker's S3 bucket) is then reachable. If that
  matters for your threat model, replace it with the specific service hosts you
  need in the platform's `allow_domains`. The read-only credential scope, not
  the proxy, is the real boundary.
- **Tier B platforms add build-time supply-chain trust.** `install:` pulls a
  devcontainer Feature that runs third-party build code in the research image.
  The shipped catalog uses official `devcontainers/features`; pin any community
  Feature you add by `@sha256` digest. A Tier-A (config-only) platform avoids
  this entirely.

---

## Troubleshooting

### `gen-devcontainer: apply repo path does not exist`

A path in `repos.yaml` is wrong or has been moved. Fix it (or remove the entry)
and re-run `scripts/setup.sh`.

### `gen-devcontainer: basename collision`

Two paths in `repos.yaml` share a basename. The slug must be unique. Rename
one locally, or move one behind a different parent name.

### Container starts but `/workspace/repos/` is empty

You edited `repos.yaml` but didn't rebuild after re-running `setup.sh`.
`Cmd+Shift+P` → **Dev Containers: Rebuild Container**.

### "`GH_TOKEN` is empty inside the container"

You opened the container before `setup.sh` baked your tokens into the per-env
`--env-file`. Check `GH_TOKEN_RESEARCH_FILE` / `GH_TOKEN_APPLY_FILE` in
`creds.env` point at non-empty token files, re-run `scripts/setup.sh`, then
**Rebuild Container**.

### `docker: open .../secrets.research.env: no such file` on "Reopen in Container"

The generated `devcontainer.json` points `--env-file` at a secret file that
`setup.sh` writes. You haven't run `scripts/setup.sh` yet (or your
`$LEASH_CREDS` path changed). Run it, then **Rebuild Container**.

### Claude Code won't log in / can't reach `claude.ai` inside the container

The login flow needs `claude.ai` on the egress allowlist. It's on both envs now,
but if you built a container from an older revision, **Rebuild Container** so the
firewall picks it up. Then run `claude` and complete the login. Auth is stored in
the per-env Docker volume (`agent-auth-research` / `agent-auth-apply`), so it's a
one-time step per env; if you ever want to wipe it, remove that volume.

### "FATAL: no approved plan at `/workspace/target-state/approved-plans/current.yaml`"

Apply refuses to start without a validated plan. Run
`scripts/plan-promote.sh <draft>` first.

### "BLOCKED: outside scope.file_paths"

PreToolUse hook is doing its job. Either widen the plan and re-promote, or
stop the agent — it's overreaching. Don't hand-edit the compiled allowlist;
it's regenerated each container start.

### The audit log is huge

`target-state/audit/tally.jsonl` is append-only. Archive periodically:

```bash
mv target-state/audit/tally.jsonl target-state/audit/tally-archive-$(date +%Y%m).jsonl
```

---

## Repository tour

```
.
├── .devcontainer/
│   ├── research/                  # read-only env (broad reads via PAT)
│   │   ├── devcontainer.template.json   # committed baseline; filled by gen_devcontainer.py
│   │   └── devcontainer.json            # GITIGNORED; regenerated from repos.yaml + creds.env
│   └── apply/                     # GitHub-only write env (no platform CLIs)
│       ├── devcontainer.template.json
│       └── devcontainer.json            # GITIGNORED
├── hooks/                         # agent-agnostic enforcement logic (the gate)
│   ├── session-start.py           # banner + per-repo plan summary; research GitHub-scope + platforms notice
│   ├── pre-tool-hook.py           # gates every Bash/Edit/Write per-repo per-path
│   ├── stop-hook.py               # per-session summary from persistent tally
│   ├── validate_plan.py           # schema + heuristics + step.repo cross-ref + apply-allowlist + compile
│   └── _paths.py                  # shared apply-env path/env-var contract (single-sourced defaults)
├── .claude/                       # reference-agent (Claude Code) config dir — the thin
│                                  #   wiring that points the agent at hooks/; swap for
│                                  #   another agent's equivalent to run a different agent
├── plans/
│   ├── schema.json                # plan schema (one repo or many)
│   └── example-plan.yaml          # reference shape: 2-repo migration
├── scripts/
│   ├── setup.sh                   # host-side diagnostic + devcontainer generation
│   ├── gen_devcontainer.py        # bakes config + writes per-env --env-file secrets
│   ├── research_access.py         # research-access.yaml resolver + the Tier-B catalog
│   ├── plan-promote.sh            # validate + diff + promote
│   └── approve-publish.sh         # lift the apply publish gate (commit/push/PR)
├── tests/
│   ├── plans/{good,bad}/          # adversarial test plans
│   ├── test_research_access.py    # resolver unit tests
│   ├── test_gen_devcontainer_wiring.py  # apply-gets-no-platforms invariant
│   └── run.sh                     # asserts specific exit codes
├── examples/
│   ├── iam-research-policy.json   # least-privilege starter for the aws-cli platform
│   ├── kubectl-readonly-rbac.yaml # read-only ServiceAccount for the kubectl platform
│   └── github-pat-scopes.md
├── target-state/                  # GITIGNORED — drafts, approved plans, audit
├── repos.yaml                     # GITIGNORED — apply paths + optional research scope (per user)
├── repos.yaml.example
├── research-access.yaml           # GITIGNORED — read-only research platform mounts (per user)
├── research-access.yaml.example
└── creds.env.example              # template for ~/.config/plan-apply-leash/creds.env
```

Your real `creds.env` and the per-env `--env-file` secrets it generates live
*outside* this repo (default `~/.config/plan-apply-leash/`), so a compromised or
mounted workspace never exposes them.
