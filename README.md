# plan-apply-leash

**Let a coding agent run plan-and-apply unattended — on a token leash.** Turn on
the agent's bypass mode (where it stops asking your permission before each
action), point it at your repos, and walk away. The worst it can do is set by the
credentials you hand it, not by how closely you watch.

It works like Terraform's `plan` / `apply`, for one repo or many:

1. **Research / plan** — read-only. The agent explores your code and writes up a
   plan. Bypass is safe here, because its token can't write anything.
2. **Apply** — write-scoped. The agent carries out the *approved* plan: editing
   files, running tests, committing, and opening one pull request per repo.
   Bypass is safe here too, because its token can only touch the repos the plan
   named.

A human steps in exactly once, **between** the two phases, to review the plan.
The leash is a **scoped GitHub access token** — specifically a fine-grained
Personal Access Token (PAT), not your Claude or API key, and not a permission
prompt. Scope that token tightly and the agent can do that much and no more.

```
┌──────────────────────┐    human          ┌─────────────────────────┐
│    research env      │   review +        │       apply env         │
│  ──────────────────  │  plan-promote     │  ─────────────────────  │
│  GitHub (read, all)  │ ─────────────────▶│  GitHub (write, scoped) │
│  platforms: read RO  │                   │  Edits + PRs (1+ repos) │
│  repos mounted RO    │                   │  repos mounted RW       │
└──────────────────────┘                   │  hook-gated per call    │
       draft plan                          └─────────────────────────┘
```

**Any agent works.** The real boundary is the token's scope plus the container
it runs in, and those constrain any program — not just Claude. This repo is a
reference setup that runs Claude Code (the dev containers, the bypass config, the
per-call checks). If you swap in a different agent, the token scope, read-only
mounts, and network firewall still hold; the only piece you'd rewire is the
per-call check, which is an extra safety net, not the boundary itself.

**Contents:** [Is this for you?](#is-this-for-you) · [Setup](#setup-10-min) ·
[Daily workflow](#daily-workflow) · [Why it's safe](#why-its-safe-under-bypass-mode) ·
[Cost](#cost) · [Troubleshooting](#troubleshooting) · [Repository tour](#repository-tour)

## Is this for you?

**Use it** if you run an agent against real code with real credentials and want
a contained change — to a single repo, or the same change across many. The sweet
spot is a repeated edit across a lot of services (say, a shared config bump
across 15 of them, one pull request each), without you approving every step.

**Skip it** for toy projects; for one-off sessions where approving each action
is fine; for untrusted code (use a full virtual machine instead — Docker shares
the host's kernel, so it isn't a true sandbox); or for multi-user setups (this is
built for a single person).

## Setup (~10 min)

What you need first: macOS or Linux; Docker Desktop or OrbStack; VS Code with the
Dev Containers extension; `python3` (then run `pip3 install --user pyyaml
jsonschema` once); and a GitHub account so you can create fine-grained access
tokens (PATs).

**1. Clone + copy templates**

```bash
git clone <this repo> ~/code/plan-apply-leash && cd ~/code/plan-apply-leash
cp repos.yaml.example repos.yaml
mkdir -p ~/.config/plan-apply-leash
cp creds.env.example ~/.config/plan-apply-leash/creds.env
```

**2. List your repos** in `repos.yaml`. Two keys:

- `apply:` (required) — host paths the apply agent may write. Each mounts at
  `/workspace/repos/<basename>/`, and that basename is the slug your plan
  references. This list is also an enforced allowlist: a plan naming a repo
  that isn't here is rejected.
- `research:` (optional) — `owner/repo` entries the research agent may read.

```yaml
apply:
  - /Users/you/code/my-project
```

**3. Create two fine-grained tokens (PATs)** — one read-only for research, and
one with write access **scoped to exactly your apply repos**. Save each token in
its own file outside the repo, then point `creds.env` at those files. (`creds.env`
stores only the file paths, never the token text itself.) The exact GitHub
permissions to tick are listed in
[`examples/github-pat-scopes.md`](examples/github-pat-scopes.md).

```bash
printf '%s' '<github_pat_token_READ_value>'  > ~/.config/plan-apply-leash/gh-research.token
printf '%s' '<github_pat_token_WRITE_value>' > ~/.config/plan-apply-leash/gh-apply.token
chmod 600 ~/.config/plan-apply-leash/gh-*.token
```

> **Nothing secret goes inside the repo folder.** Both containers mount that
> folder at `/workspace`, so a token saved there would be readable from the
> read-only research env — which would defeat the whole split. Instead,
> `setup.sh` runs on your own machine, reads the tokens, and hands them to each
> container through a separate file kept outside `/workspace`. (Your Claude login
> isn't in `creds.env` either — see step 6.)

**4. (Optional) Give research read access to a platform** — AWS, Kubernetes, or
any HTTP API. Copy `research-access.yaml.example` to `research-access.yaml` and
add one entry per credential. There are two styles: config-only
([Tier A](research-access.yaml.example), for plain HTTP APIs) and "catalog CLIs"
(Tier B, which also installs a command-line tool into the container). The apply
env never gets any of this. Making the credential genuinely read-only is up to
you — here are end-to-end walkthroughs for
[AWS](examples/aws-research-setup.md) and
[Kubernetes](examples/kubectl-readonly-rbac.yaml).

**5. Verify** — `scripts/setup.sh` checks everything and regenerates the
devcontainers; expect all-green. Re-run it and **Rebuild Container** after
editing any config.

**6. Open** the folder in VS Code → **Reopen in Container**. VS Code shows a
picker listing both envs — choose **leash-research** for now (you'll pick
**leash-apply** later). The first time you open each env, run `claude` in the
container terminal and log in with your Claude account. The login is stored in a
per-env Docker volume and survives rebuilds — there's deliberately no
`ANTHROPIC_API_KEY`.

## Daily workflow

**The practical setup: keep both envs open at once.** Open the folder twice in VS
Code — one window in **leash-research**, one in **leash-apply** — and keep a third
plain host terminal for the promote step. The loop is then: research drafts a plan
→ you promote it on the host → you **Reload Window** in the apply env to pick it up.
No flipping a single window in and out of the container, and no full rebuild between
plans (the plan is recompiled on every attach — see step 3).

**1 · Research** — Reopen in Container → **leash-research**, run `claude`, and
ask it to investigate and draft a plan. For example:

> *Investigate how foo-service and bar-cli use @acme/logger, then draft a plan to
> bump it to v2 across both.*

Drafts are organized into per-project subfolders. By default the agent writes to
`target-state/research/drafts/default/<plan_id>.yaml`; run `/project <name>` in
the session to switch to a named project (`drafts/<name>/…` instead). See the
[schema](plans/schema.json) for every field and [example-plan.yaml](plans/example-plan.yaml)
for a full plan. Bypass is safe here — the env is read-only.

A plan is the handoff; anything outside `file_paths` / `allowed_command_prefixes`
is blocked during apply:

```yaml
plan_id: 2026-05-22-bump-logger
scope:
  repos:
    foo-service:
      github: acme/foo-service
      branch: feature/bump-logger-v2
      file_paths: [package.json, src/logger.ts, src/handler.ts]
  allowed_command_prefixes: [npm install, npm test, git push origin, gh pr create]
steps:
  - { id: 1, repo: foo-service, type: file_edit, path: package.json }
```

**2 · Review + promote** — on the host (a plain terminal works; you don't need to
take the research window out of its container):

```bash
scripts/plan-promote.sh <plan_id>.yaml                       # default project
scripts/plan-promote.sh target-state/research/drafts/<name>/<plan_id>.yaml  # named project
```

A bare filename resolves against the `default/` project. For a draft in a named
project, pass its full path. The script validates the draft, previews the
compiled per-repo allowlist, and diffs it against the current plan. It then asks
you to confirm and atomically promotes to
`target-state/approved-plans/current.yaml`. **This is the trust boundary** — your
review catches coordination mistakes no hook can.

**3 · Apply** — in the **leash-apply** window, run **Developer: Reload Window** to
pick up the freshly-promoted plan (the `postAttachCommand` recompiles
`current.yaml` into the enforced allowlist on every attach), then run `claude` and
tell it to carry out `current.yaml`. It edits and tests on its own. Before each `Bash`,
`Edit`, or `Write`, a check (the per-call hook) compares the action against the
per-repo allowlist, and every action is recorded to
`target-state/audit/tally.jsonl`.

> **Reload vs. rebuild.** A plain **Reload Window** is all it takes to load a
> newly-promoted plan — no rebuild between plans. You only need **Rebuild
> Container** when the container config itself changes, i.e. after re-running
> `setup.sh` (which regenerates `devcontainer.json`). If a promoted plan fails
> validation, the apply env comes up *locked* (every call blocked) rather than
> running the previous plan — fix the plan, re-promote, and reload.

Publishing is held back on purpose: `git commit`/`push` and `gh pr create` stay
**paused** until you run `scripts/approve-publish.sh` on the host. That script
creates a small marker file the agent itself isn't allowed to create — which is
what makes the pause real. You approve once per session. If you'd rather let it
publish fully on its own, set `APPLY_REQUIRE_PUBLISH_APPROVAL=0` in
[`.devcontainer/apply/devcontainer.template.json`](.devcontainer/apply/devcontainer.template.json)
and rebuild.

**4 · Review the PRs** — one branch + PR per repo, back on the host.

> **What gets blocked.** Each entry in `allowed_command_prefixes` has to match
> the *start* of a single, simple command. Characters that let one command chain
> into another are rejected — no `;`, `&&`, `|`, `>`, `` ` ``, `$(…)`, or
> newlines. (Otherwise an allowed `npm test` could sneak in `npm test &&
> curl…|sh`.) Any edit outside the plan, or any command that doesn't match, is
> blocked, reported back to the agent, and logged. To move forward, either widen
> the plan and re-promote it, or stop the agent — it's trying to do more than the
> plan allows.

## Why it's safe under bypass mode

Listed strongest first — each layer still holds even if the one above it fails:

1. **Token scope** (enforced by GitHub itself, so the strongest) — `GH_TOKEN_APPLY`
   can only write to the repos you scoped it to; GitHub rejects anything else.
   This is *the* real boundary. Scope it as carefully as you would a production
   access policy.
2. **Nothing extra to abuse** — the apply env has no platform tools or
   credentials, so there's nothing for it to reach out to.
3. **Container + network limits** — research mounts your repos read-only, and a
   proxy (Squid) only lets the container reach an allowlist of hostnames
   (Anthropic and GitHub APIs, package registries, and any platform domains you
   declared).
4. **Per-call check** — the one Claude-specific layer; a tripwire sitting on top
   of the boundary, rewired if you swap agents.
5. **Your review at promotion** — catches a plan that's valid but wrong.
   Promotion also rejects any repo that isn't in your `repos.yaml` `apply:` list.

| | GitHub | Platforms | Bypass-safe? |
|--|--|--|--|
| **research** | required, read | optional, read-only | yes (read-only) |
| **apply** | required, write | **none** | yes (token-scoped) |

### Honest limits

- **Docker isn't a true sandbox** — it shares the host's kernel. For genuinely
  hostile code, use a virtual machine instead.
- **Prompt-injection detection isn't bulletproof** — the strict plan schema is
  the real defense; the heuristic checks are only a tripwire.
- **From apply's point of view, the plan is untrusted input** — so your review
  before promotion is the defense.
- **Coordinating changes across repos is the agent's job** — your per-PR review
  is what catches mix-ups that file paths alone can't.
- **A platform credential is only as read-only as you made it** — the harness
  mounts it read-only on disk, but it can't prove the credential itself can't
  write.
- **The network proxy filters by hostname, not by content** — it can't stop data
  from being sent to an *allowed* domain (such as an `.amazonaws.com` bucket).
  Read-only credential scope is the real protection.
- **Catalog (Tier-B) CLIs mean trusting their installers at build time** — the
  official ones ship pinned to a version; pin any community one to a specific
  `@sha256`.

## Cost

Apply cost grows roughly with `repos × file_paths × steps`. A 2-repo, ~5-file
plan lands around 150–400k tokens. If you're on a metered plan, start with a
single repo.

## Troubleshooting

- **`/workspace/repos/` empty, `GH_TOKEN` empty, or `secrets.research.env`
  missing** → you opened the container before (re-)running `setup.sh`. Run it,
  then **Rebuild Container**.
- **`basename collision` / `repo path does not exist`** → fix `repos.yaml`,
  re-run `setup.sh`.
- **Claude can't reach `claude.ai`** → container built from an old revision;
  **Rebuild Container**, then `claude` login.
- **`no approved plan at current.yaml`** → run `plan-promote.sh <draft>` first.
- **`BLOCKED: outside scope.file_paths`** → this means the hook is doing its job.
  Either widen the plan and re-promote it, or stop the agent. Don't hand-edit the
  generated allowlist — it's rebuilt from the plan every time apply starts.
- **Audit log huge** → `target-state/audit/tally.jsonl` is append-only; archive
  it.

## Repository tour

- `.devcontainer/{research,apply}/` — the two envs; `devcontainer.json` is
  gitignored, regenerated from `repos.yaml` + `creds.env`.
- `hooks/` — agent-agnostic gate: `pre-tool-hook.py` (per-call), plus
  `session-start.py`, `stop-hook.py`, `validate_plan.py`.
- `.claude/` — reference-agent wiring; swap for another agent's equivalent.
- `plans/` — `schema.json` (field-by-field spec), `example-plan.yaml`.
- `scripts/` — `setup.sh`, `gen_devcontainer.py`, `research_access.py`,
  `plan-promote.sh`, `approve-publish.sh`.
- `examples/` — PAT scopes, AWS and Kubernetes read-only walkthroughs.
- `target-state/`, `repos.yaml`, `research-access.yaml`, and your real
  `creds.env` — gitignored / outside the repo, per user.
