# Apply-env house conventions

Trusted, harness-owned style guide for the APPLY agent. Mounted READ-ONLY into
the apply container at `/workspace/apply-conventions.md` and surfaced to the
agent at session start. The agent cannot edit it (kernel-enforced read-only +
settings deny), so unlike the agent-authored plan it is a safe place to define
cross-cutting house style. Apply these to every commit and PR you create.

## Code patterns

- Match the surrounding code: naming, formatting, comment density, and idioms
  already present in the file you're editing. Don't introduce a new style.
- Keep diffs minimal and scoped to the plan. Don't reformat untouched lines,
  reorder imports, or "drive-by" refactor code the plan didn't ask you to touch.
- No new dependencies unless the plan explicitly calls for them.
- Don't add comments that merely restate the code. Comment the non-obvious
  *why*, not the *what*.
- Leave no debugging artifacts: no stray `print`/`console.log`, commented-out
  code, or TODO markers that aren't in the plan.

## Commit messages (Conventional Commits)

Format: `<type>(<scope>): <summary>`

- **type**: one of `feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `perf`,
  `build`, `ci`.
- **scope**: optional; the affected component or repo slug.
- **summary**: imperative mood, lower-case, no trailing period, ≤ 72 chars.
- Body (optional, after a blank line): wrap at 72 cols; explain *why*, not *what*.
- One logical change per commit. Reference the plan in the body: `Plan: <plan_id>`.

Example:

```
fix(auth): reject expired refresh tokens before issuing a session

Tokens past their `exp` were silently renewed, extending sessions
indefinitely. Validate `exp` up front and return 401 instead.

Plan: 2026-06-12-auth-token-hardening
```

## PR description template

If the plan provides a `pr_body` for a repo, use it VERBATIM (write it to a temp
file and pass `gh pr create --body-file`; do not paraphrase). Otherwise, build
the PR body from this template:

```
## Summary

<1–3 sentences: what changed and why>

## Changes

- <bullet per notable change>

## Plan

Applied from approved plan `<plan_id>`. See the plan for full step-by-step intent.

## Testing

<commands run and their result, or "no tests run — explain why">
```

Keep the PR title aligned with the plan's `pr_title`.
