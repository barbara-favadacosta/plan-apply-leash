#!/usr/bin/env python3
"""
session-start.py — SessionStart hook (reference agent: Claude Code).

Two responsibilities:
  1) Tell the human (via stderr) which env they're in and, for apply, the
     multi-repo plan summary.
  2) Tell the agent (via JSON additionalContext on stdout) the same thing,
     so it knows its mode and constraints up front rather than learning by
     hitting blocked tool calls.

Branches on LEASH_ENV:
  research → short banner explaining read-only mode and scratch-dir convention
  apply    → loads compiled allowlist, summarises per-repo scope

The tally is persistent at /workspace/target-state/audit/tally.jsonl across
sessions; per-session summarisation happens in stop-hook.py via session_id
filtering.

Exit 0 always — this hook informs, it does not gate.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _paths


RESEARCH_CONTEXT = """\
You are running in the RESEARCH devcontainer of the plan-apply-leash harness.

Mode: READ-ONLY.
  - All repos under {repos_path} are mounted READ-ONLY at /workspace/repos/.
    You can read any of them with Read/Glob/Grep tools.
{github_scope}
{platforms}
  - You CAN write under /workspace/target-state/research/, which has three
    folders. Place every file you create according to this taxonomy:
        drafts/  — DELIVERABLES ONLY. Exactly two kinds of file belong here:
                   (1) the candidate plan, <plan_id>.yaml (schema-validated);
                   (2) apply-bound artifacts — the literal file contents the
                   plan instructs the apply env to write into a repo, or that a
                   downstream environment will execute (shell scripts, IAM/
                   bucket policies, lifecycle configs, k8s manifests, …).
                   Test for (2): "Does the plan reference this file as something
                   to be committed/applied/run elsewhere?" If yes → drafts/.
                   If it only exists to explain, justify, or support the plan,
                   it is NOT a deliverable — it goes in notes/.
        notes/   — EVERYTHING ELSE the research produced: investigation
                   write-ups, analyses, inventories, comparison tables,
                   disposition guides, scratch reasoning. Anything that informed
                   the plan but is neither the plan nor an apply-bound artifact
                   goes here, not in drafts/.
        clones/  — disposable git checkouts only.
    Rule of thumb: drafts/ is what gets reviewed and handed to the apply env;
    notes/ is your working record. If a file is not going to be applied
    anywhere, it must not sit in drafts/.
  - PROJECT SCOPING. Work is organized into per-project subfolders under each of
    the three folders: drafts/<project>/, notes/<project>/, clones/<project>/.
    No project has been selected for this session yet, so you are operating in
    the **default** project: write to drafts/default/, notes/default/, and
    clones/default/, and read prior context only from those. To use or create a
    named project instead, the user runs `/project <name>`, which switches the
    active project for the rest of the session. Until they do, stay in default/.
  - Edit/Write to /workspace/repos/** and the harness's own files
    (/workspace/hooks, /workspace/plans) is denied at the permission layer.
    Don't try; it'll fail.
  - Every mounted platform credential is READ-ONLY. git push, git commit,
    gh pr create, and any mutating/write call to a mounted platform (e.g.
    kubectl apply, an AWS mutating action) are denied. Use platforms for
    READS only.

Your job: investigate the user's question across the available repos,
then write a candidate plan to
/workspace/target-state/research/drafts/<project>/<plan_id>.yaml  (default/ if
the user hasn't run /project) using the schema at /workspace/plans/schema.json.

The plan you produce is MULTI-REPO. scope.repos is a map of subdir slug
(e.g. 'foo-service') → repo config (github identifier, branch, pr_title,
file_paths). Each step that touches a file must name its repo. The plan
will be reviewed by a human and, if approved, executed in the APPLY env.

Be concrete: list explicit file_paths per repo, explicit branch names,
explicit pr_titles, explicit allowed_command_prefixes. Don't use freeform
shell or arbitrary URLs. The plan you produce will be treated as untrusted
input on the apply side; the schema is strict on purpose.
"""

APPLY_CONTEXT_HEADER = """\
You are running in the APPLY devcontainer of the plan-apply-leash harness.

Mode: WRITE across the repos named in the approved plan.
  - Each repo listed in repos.yaml is mounted at /workspace/repos/<slug>/.
  - The plan's scope.repos lists which subdir slugs you may modify, and
    for each: file_paths (relative to that repo), branch, pr_title.
  - A PreToolUse hook BLOCKS every Bash/Edit/Write/NotebookEdit whose target
    is not in the compiled allowlist. If you need to do something not
    listed, the answer is "stop and report" — not "try a variation".
  - You do NOT have any platform CLIs (no AWS CLI, no kubectl, etc.). If the
    plan describes AWS or Kubernetes work, that's informational; deploy
    outside this harness.
  - Your GitHub token is scoped to the in-scope repos; calls outside
    scope are rejected server-side.
  - REQUIRED WORKFLOW for every plan, per in-scope repo, IN THIS ORDER:
      1. Create the branch the plan names FIRST — before any edit:
         `git -C /workspace/repos/<slug> checkout -b <branch>`
         This is ENFORCED, not advice: the hook BLOCKS every Edit/Write into a
         repo until that repo's HEAD is on the plan's declared branch. If you
         try to edit first, you'll be told to create the branch and retry.
      2. Make the edits and run any tests.
      3. Commit locally: `git -C /workspace/repos/<slug> add …` then
         `git -C /workspace/repos/<slug> commit -m "…"`.
    Branch / add / commit run autonomously — they are built into the harness
    and do NOT need to be in allowed_command_prefixes. `cd` is blocked; always
    operate on a repo with `git -C /workspace/repos/<slug> …`.
  - PUBLISH CHECKPOINT: branching, editing, testing, and committing are
    autonomous, but `git push` and `gh pr create` are PAUSED until the human
    approves. When every in-scope repo is committed, STOP, summarize what
    changed per repo, and ASK the user to approve publishing. Don't try to work
    around the pause — after they approve, push the plan's branch (pushes may
    only target the branch the plan declared) and open the PR.

Plan in effect:
"""


def banner(text: str) -> None:
    bar = "─" * 60
    print(f"\n{bar}\n{text}\n{bar}\n", file=sys.stderr, flush=True)


def emit_context(text: str) -> None:
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": text,
        }
    }
    json.dump(payload, sys.stdout)
    sys.stdout.write("\n")
    sys.stdout.flush()


def parse_repo_scope() -> list[str]:
    """RESEARCH_REPO_SCOPE is newline-joined owner/repo (set by gen_devcontainer
    only when research is scoped). Empty/unset means 'roam whatever the PAT allows'."""
    raw = os.environ.get("RESEARCH_REPO_SCOPE", "")
    return [line.strip() for line in raw.splitlines() if line.strip()]


def parse_platforms() -> list[tuple[str, str]]:
    """RESEARCH_PLATFORMS is pipe-joined "name<TAB>mount_at" (set by
    gen_devcontainer from research-access.yaml). Empty/unset = GitHub only."""
    raw = os.environ.get("RESEARCH_PLATFORMS", "")
    out: list[tuple[str, str]] = []
    for entry in raw.split("|"):
        if not entry.strip():
            continue
        name, _, target = entry.partition("\t")
        if name.strip():
            out.append((name.strip(), target.strip()))
    return out


def research_main() -> None:
    repos_path = os.environ.get("REPOS_PATH", "/workspace/repos")
    gh_set = bool(os.environ.get("GH_TOKEN"))
    gh_line = (
        "set"
        if gh_set
        else "MISSING — set GH_TOKEN_RESEARCH_FILE in your creds file, re-run scripts/setup.sh, then Rebuild Container"
    )

    platforms = parse_platforms()
    if platforms:
        banner_platforms = "  platforms:   " + ", ".join(f"{n} ({t})" for n, t in platforms)
        platforms_ctx = (
            "  - Read-only platform credentials are mounted (use their CLIs/APIs\n"
            "    for READS only):\n"
            + "\n".join(f"        - {n} → {t}" for n, t in platforms)
        )
    else:
        banner_platforms = "  platforms:   none (GitHub only)"
        platforms_ctx = "  - No extra platform credentials are mounted (GitHub only)."

    scope = parse_repo_scope()
    if scope:
        scope_listing = "\n".join(f"        - {ident}" for ident in scope)
        github_scope = (
            "  - GitHub scope: you may ONLY access these repositories via `gh api`,\n"
            "    `gh search`, `git clone`, and the like, even though your PAT can\n"
            "    reach more. Do NOT list, query, clone, or read any other repo:\n"
            f"{scope_listing}\n"
            "    (The locally-mounted repos above are always readable on disk\n"
            "    regardless of this scope.)"
        )
        banner_scope = f"  GitHub scope: ONLY {', '.join(scope)}"
    else:
        github_scope = (
            "  - You may also reach any repo your GitHub PAT allows via `gh api`,\n"
            "    `gh search`, and `git clone`."
        )
        banner_scope = "  GitHub scope: any repo the PAT allows (no research: scope set)"

    banner(
        f"[research env] read-only credentials in effect.\n"
        f"{banner_platforms}\n"
        f"  GH_TOKEN:    {gh_line}\n"
        f"{banner_scope}\n"
        f"  Active project: default — run /project <name> to switch\n"
        f"  Drafts → /workspace/target-state/research/drafts/\n"
        f"  Schema → /workspace/plans/schema.json"
    )
    emit_context(RESEARCH_CONTEXT.format(
        repos_path=repos_path, github_scope=github_scope, platforms=platforms_ctx
    ))


def apply_main() -> None:
    compiled_path = _paths.compiled_path()
    plan_path = os.environ.get("APPLY_PLAN_PATH", "(unset)")

    if not compiled_path.exists():
        banner(
            "[apply env] FATAL: no compiled allowlist found.\n"
            f"  expected at: {compiled_path}\n"
            "  did post-create.sh run validate_plan.py successfully?"
        )
        emit_context(
            "APPLY ENV IS UNINITIALIZED: no compiled plan allowlist. Every Bash/Edit/Write "
            "call will be blocked. Tell the user to rebuild after staging an approved plan."
        )
        return

    try:
        compiled = json.loads(compiled_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        banner(f"[apply env] FATAL: could not load compiled allowlist: {e}")
        emit_context(f"APPLY ENV BROKEN: cannot read compiled allowlist ({e}).")
        return

    plan_id = compiled.get("plan_id", "<unknown>")
    repos = compiled.get("repos", {})
    cmd_prefixes = compiled.get("allowed_command_prefixes", [])
    aws_resources = compiled.get("aws_resources", [])
    publish_gate = _paths.require_publish_approval()
    publish_line = ("push/PR PAUSED — commit runs autonomously; push & PR need "
                    "approval (scripts/approve-publish.sh)"
                    if publish_gate else "auto (approval disabled)")

    def fmt_list(items: list, limit: int = 5) -> str:
        if not items:
            return "(none)"
        shown = items[:limit]
        suffix = f" … +{len(items) - limit} more" if len(items) > limit else ""
        return ", ".join(str(x) for x in shown) + suffix

    repo_lines = []
    for slug, cfg in repos.items():
        n_files = len(cfg.get("file_paths", []))
        branch = cfg.get("branch", "?")
        gh = cfg.get("github", "?")
        repo_lines.append(f"    • {slug}: {gh} → {n_files} file(s), branch {branch}")

    aws_summary = "(none — apply env has no AWS access)"
    if aws_resources:
        aws_summary = (
            f"INFO ONLY: {len(aws_resources)} ARN(s) listed in plan; "
            f"apply cannot execute them"
        )

    banner(
        f"[apply env] PreToolUse hook ACTIVE — calls outside the plan will be blocked.\n"
        f"  plan_id:      {plan_id}\n"
        f"  source:       {plan_path}\n"
        f"  repos ({len(repos)}):\n" +
        ("\n".join(repo_lines) if repo_lines else "    (none)") + "\n"
        f"  commands:     {len(cmd_prefixes)} prefix(es) — {fmt_list(cmd_prefixes)}\n"
        f"  aws:          {aws_summary}\n"
        f"  publish:      {publish_line}"
    )

    context_repo_lines = []
    for slug, cfg in repos.items():
        files = cfg.get("file_paths", [])
        branch = cfg.get("branch", "?")
        pr_title = cfg.get("pr_title", "?")
        context_repo_lines.append(
            f"  • {slug} ({cfg.get('github', '?')}, branch {branch}, PR title {pr_title!r}):\n"
            f"      file_paths: {fmt_list(files, 20)}"
        )

    context_body = (
        f"  plan_id: {plan_id}\n"
        f"  in-scope repos ({len(repos)}):\n" +
        "\n".join(context_repo_lines) + "\n\n"
        f"  allowed_command_prefixes ({len(cmd_prefixes)}): {fmt_list(cmd_prefixes, 20)}\n"
        f"  aws_resources: {aws_summary}\n"
        "\n"
        "Read the full plan at /workspace/target-state/approved-plans/current.yaml "
        "for step-by-step intent. The PreToolUse hook enforces the per-repo scope "
        "above; do not attempt actions outside it."
        + ("\n\nPUBLISH CHECKPOINT: branch → edit → commit is autonomous, but git push "
           "and gh pr create are paused until the user approves (scripts/approve-publish.sh). "
           "When every in-scope repo is committed, STOP, summarize per repo, and ASK to "
           "publish. Pushes may only target the branch the plan declared."
           if publish_gate else "")
    )
    emit_context(APPLY_CONTEXT_HEADER + context_body)


def main() -> None:
    try:
        _ = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        pass

    env = os.environ.get("LEASH_ENV", "").lower()
    if env == "apply":
        apply_main()
    elif env == "research":
        research_main()
    else:
        banner(
            f"[session-start] LEASH_ENV={env!r} — neither 'research' nor 'apply'. "
            "Skipping harness banner."
        )
    sys.exit(0)


if __name__ == "__main__":
    main()
