#!/usr/bin/env python3
"""
validate_plan.py — multi-repo apply-side plan validation.

Two roles in one script:
  1) Pre-flight (run from apply post-create.sh): validates the approved plan against
     the JSON schema and a battery of content heuristics, then compiles a strict
     allowlist file used by the per-call hook.
  2) Library: pre-tool-hook.py imports check_path_allowed / check_command_allowed /
     classify_git_command / git_push_target / plan_branches.

The schema is the load-bearing defense. The heuristics catch obvious malware,
exfil patterns, and prompt-injection markers — they are NOT a robust prompt-injection
detector. The structural constraint (steps must be typed entries with bounded
fields, referencing scope.repos keys) is what actually limits what the agent can do.

Path semantics for multi-repo:
  scope.repos is a map of slug → {github, branch, pr_title, file_paths}.
  Tool calls have paths like /workspace/repos/<slug>/<rel>.
  check_path_allowed splits the slug, looks it up, validates <rel> against
  that repo's file_paths.

Exit codes (pre-flight mode):
  0  ok
  1  schema validation failed
  2  heuristic failure (suspicious content) — higher severity, review the plan manually
  3  invocation / IO error
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shlex
import sys
import unicodedata
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft7Validator


SUSPICIOUS_SHELL_PATTERNS = [
    (r"\bcurl\b[^|]*\|\s*(sudo\s+)?(ba)?sh\b", "curl-pipe-to-shell"),
    (r"\bwget\b[^|]*\|\s*(sudo\s+)?(ba)?sh\b", "wget-pipe-to-shell"),
    (r"\beval\s*[\"'`(]?\s*\$\(", "eval-of-subshell"),
    (r"\bbase64\s+-d\b.*\|\s*(ba)?sh\b", "base64-decode-to-shell"),
    (r"/dev/tcp/", "bash-network-redirect"),
    (r"\bnc\s+(-[a-z]+\s+)?\S+\s+\d+", "netcat-to-host"),
    (r"\bpython3?\s+-c\s+['\"]\s*import\s+(os|subprocess|socket)", "python-inline-syscall"),
    (r"\bperl\s+-e\b", "perl-inline-exec"),
    (r"\brm\s+-rf\s+(/|~|\$HOME)", "rm-rf-home-or-root"),
    (r":\(\)\{\s*:\|:\s*&\s*\};:", "fork-bomb"),
]

INJECTION_MARKERS = [
    r"\bignore\s+(all\s+)?(previous|prior|above)\s+instructions?\b",
    r"\bdisregard\s+(all\s+)?(previous|prior|above)\b",
    r"\byou\s+are\s+now\s+(in\s+)?(developer|dan|jailbreak)\s+mode\b",
    r"\bsystem\s*[:\-]\s*you\s+are\b",
    r"\bnew\s+instructions?\s*:",
    r"\boverride\s+(your\s+)?(prior|previous|system)\s+(prompt|instructions?)\b",
    r"\bact\s+as\s+if\s+you\s+(have\s+)?no\s+restrictions?\b",
    r"\b(claude|assistant|agent|ai|system)\s*[,:]\s*ignore\b",
    r"<\s*/?\s*(system|assistant|user|admin)\s*>",
    r"\[\s*(system|admin)\s+override\s*\]",
]

EXFIL_HOST_PATTERNS = [
    r"webhook\.site",
    r"requestbin\.",
    r"requestcatcher\.com",
    r"pipedream\.net",
    r"ngrok\.io",
    r"ngrok-free\.app",
    r"trycloudflare\.com",
    r"\bpastebin\.com",
    r"\btransfer\.sh",
    r"\bcatbox\.moe",
    r"\bbashupload\.com",
    r"\bfile\.io\b",
]

ALLOWED_URL_HOSTS = {
    "api.anthropic.com",
    "api.github.com",
    "github.com",
    "raw.githubusercontent.com",
    "objects.githubusercontent.com",
}

# Zero-width / bidi / invisible code points a plan must never contain. Written
# entirely as \u escapes ON PURPOSE: the literal characters are themselves
# invisible, so an editor or reviewer can't see them in the source (and this
# very line could otherwise smuggle one). Covers the zero-width/bidi marks
# (U+200B..U+200F), the bidi embedding/override controls (U+202A..U+202E) and
# isolates (U+2066..U+2069), the BOM / zero-width no-break space (U+FEFF), and
# the soft hyphen (U+00AD).
BIDI_AND_INVISIBLE = re.compile(
    r"[\u200B-\u200F\u202A-\u202E\u2066-\u2069\uFEFF\u00AD]"
)

BASE64_RUN = re.compile(r"[A-Za-z0-9+/]{200,}={0,2}")

REPOS_PREFIX = "/workspace/repos/"

# Shell control / redirection operators. The command allowlist is a prefix
# match, so without rejecting these an allowed prefix like "npm test" smuggles
# arbitrary work past the gate — "npm test && curl … | sh" (defeats the
# allowlist AND the publish checkpoint, since the chained command never starts
# with a publish verb) or "npm test > /workspace/repos/x/evil" (defeats the
# per-file scope, which is only enforced on the Edit/Write tools, not on Bash).
# Apply commands must therefore be a single, simple command: no chaining, pipes,
# redirection, command substitution, or backgrounding.
#
# Crucially, these are only dangerous when the SHELL would act on them — i.e.
# when they appear UNQUOTED. A commit message or PR body legitimately contains
# them ('git commit -m "fix: handle a & b"', 'gh pr create --body "see | the
# table > here"'), and the shell treats those as literal text. A naive substring
# scan rejects every such command; command_shell_violation instead tracks quote
# state and flags an operator only where it is shell-active.

# Operators that the shell only honors OUTSIDE any quoting. Longest-match-first
# so "&&"/"||"/">>" report via their single-char entry but multi-char forms like
# "$(" and "<(" win over a bare "<".
_UNQUOTED_OPERATORS = [
    ("$(", "$(…) command substitution"),
    ("<(", "process substitution '<(…)'"),
    (";", "command separator ';'"),
    ("&", "background / chain '&' or '&&'"),
    ("|", "pipe '|' or '||'"),
    ("`", "backtick command substitution"),
    (">", "output redirection '>' / '>>'"),
    ("<", "input redirection '<'"),
    ("\n", "newline (multiple commands)"),
    ("\r", "carriage return (multiple commands)"),
]


def command_shell_violation(command: str) -> str | None:
    """Return a human-readable label for the first SHELL-ACTIVE control/redirection
    operator in `command`, or None if the command is a single, simple command.

    Quoting is honored exactly as a POSIX shell would: inside single quotes
    nothing is special; inside double quotes only command substitution ($(…) and
    backticks) stays active (a `\\` escapes the next char there). So `git commit
    -m "fix: a & b"` and `gh pr create --body "a | b > c"` are allowed, while
    `git commit -m x && curl …`, `echo $(id)`, and a double-quoted backtick are
    rejected. An unbalanced quote is itself a violation — the shell would error on
    it, and leaving it unflagged would let an operator hide in an unterminated
    string."""
    in_single = False  # within '…' — everything literal until the next '
    in_double = False  # within "…" — only $(/backtick stay active
    i, n = 0, len(command)
    while i < n:
        c = command[i]
        if in_single:
            if c == "'":
                in_single = False
            i += 1
            continue
        if in_double:
            if c == "\\" and i + 1 < n:   # backslash escapes the next char in "…"
                i += 2
                continue
            if c == '"':
                in_double = False
                i += 1
                continue
            if c == "`":
                return "backtick command substitution"
            if c == "$" and i + 1 < n and command[i + 1] == "(":
                return "$(…) command substitution"
            i += 1
            continue
        # Unquoted context.
        if c == "\\" and i + 1 < n:        # backslash-escaped char is literal
            i += 2
            continue
        if c == "'":
            in_single = True
            i += 1
            continue
        if c == '"':
            in_double = True
            i += 1
            continue
        for token, label in _UNQUOTED_OPERATORS:
            if command.startswith(token, i):
                return label
        i += 1
    if in_single or in_double:
        return "unbalanced quote (unterminated string)"
    return None


# --- Git / gh workflow classification ----------------------------------------
# The branch → stage → commit → push → PR cycle is scaffolding EVERY apply plan
# needs, so the harness owns it rather than making each plan re-list it. Listing
# it in allowed_command_prefixes never worked well anyway: the prefix match is
# literal, so a plan's "git add" never matched the `git -C /workspace/repos/<slug>
# add` form the agent is FORCED to use (it can't `cd` — that's not an allowed
# prefix — and it can't chain `cd … && git …` — shell operators are rejected).
#
# Commands are classified by their git/gh subcommand, read AFTER skipping
# `git -C <path>` and the other global options, into:
#   "local"   — autonomous, no approval: create-branch, add, commit, fetch, and
#               read-only inspection (status/diff/log/show/rev-parse/branch/
#               ls-files/ls-tree/cat-file/describe/shortlog/blame). For gh:
#               read-only PR/repo queries (gh pr view/status/diff/checks/list,
#               gh repo view) — none of these mutate anything.
#   "publish" — allowed, but HELD by the publish-approval gate in
#               pre-tool-hook.py: git push, gh pr create/ready/edit/comment,
#               gh release. gh pr edit/comment only touch an already-open PR,
#               which exists only AFTER approval — gating them costs nothing and
#               stops them firing pre-approval.
#   None      — not a recognized git/gh workflow verb; falls through to the
#               plan's allowed_command_prefixes (e.g. npm test, terraform fmt).
#
# Deliberately NOT auto-allowed (they fall through to the plan): reset, clean,
# rm, and checkout/switch/restore of PATHS — each can destroy uncommitted work
# in the user's REAL, bind-mounted repos. A plan may still grant them explicitly.
_GIT_GLOBAL_OPTS_WITH_VALUE = {"-C", "--git-dir", "--work-tree", "-c", "--namespace", "--config-env"}
_GIT_LOCAL_SUBCMDS = {
    "add", "commit", "status", "diff", "log", "show", "rev-parse", "branch",
    "fetch", "ls-files", "ls-tree", "cat-file", "describe", "shortlog", "blame",
}
_GIT_BRANCH_CREATE_FLAGS = {"-b", "-B", "-c", "-C"}

# Global options that inject inline git config. core.pager / diff.external /
# core.hooksPath / core.sshCommand / core.fsmonitor (etc.) each make git execute
# an external command, so `git -c <cfg> <verb>` is arbitrary code execution behind
# an innocent verb. The built-in workflow never needs them; a command carrying one
# is refused (→ falls through to the plan allowlist, which can't express it).
_GIT_INLINE_CONFIG_OPTS = {"-c", "--config-env"}


def _git_subcommand(tokens: list[str]) -> tuple[str | None, list[str], bool]:
    """Return (subcommand, args_after_it, uses_inline_config). Skips git's global
    options (and the values of value-taking ones) to find the subcommand. The third
    field is True iff the GLOBAL options include an inline-config flag — see
    _GIT_INLINE_CONFIG_OPTS. Subcommand-level -c (e.g. `git switch -c`) is NOT
    flagged because it appears after the subcommand, outside this loop."""
    i = 0
    inline_config = False
    while i < len(tokens):
        t = tokens[i]
        if t in _GIT_INLINE_CONFIG_OPTS or t.startswith("--config-env="):
            inline_config = True
        if t in _GIT_GLOBAL_OPTS_WITH_VALUE:
            i += 2
            continue
        if t.startswith("-"):
            i += 1
            continue
        return t, tokens[i + 1:], inline_config
    return None, [], inline_config


def classify_git_command(command: str) -> str | None:
    """Classify a single, simple git/gh command as 'local', 'publish', or None.
    Assumes command_shell_violation(command) is None (one simple command, so
    shlex.split is safe and there's nothing chained after the verb)."""
    try:
        toks = shlex.split(command.strip())
    except ValueError:
        return None
    if not toks:
        return None
    prog = os.path.basename(toks[0])

    if prog == "git":
        sub, args, inline_config = _git_subcommand(toks[1:])
        if sub is None:
            return None
        if inline_config:
            return None  # `git -c <cfg> …` can run arbitrary code; never auto-trust
        if sub == "push":
            return "publish"
        if sub in _GIT_LOCAL_SUBCMDS:
            return "local"
        if sub in ("checkout", "switch"):
            # Only branch CREATION is autonomous; a bare checkout/switch of a
            # path or existing branch can revert working-tree changes, so it
            # falls through to the plan's allowlist.
            return "local" if any(a in _GIT_BRANCH_CREATE_FLAGS for a in args) else None
        return None

    if prog == "gh":
        nonopt = [t for t in toks[1:] if not t.startswith("-")]
        pair = nonopt[:2]
        if pair in (["pr", "view"], ["pr", "status"], ["pr", "diff"],
                    ["pr", "checks"], ["pr", "list"], ["repo", "view"]):
            return "local"
        if pair in (["pr", "create"], ["pr", "ready"],
                    ["pr", "edit"], ["pr", "comment"]):
            return "publish"
        if nonopt[:1] == ["release"]:
            return "publish"
        return None

    return None


def git_push_target(command: str) -> tuple[bool, str | None]:
    """Inspect a command for `git … push …`. Returns:
      (False, None) — not a git push at all.
      (True, None)  — a push whose destination branch can't be determined
                      (e.g. bare `git push`, or `git push origin` with no refspec).
      (True, name)  — a push to destination branch `name`.
    Used to enforce that pushes only ever target a branch the plan declared."""
    try:
        toks = shlex.split(command.strip())
    except ValueError:
        return (False, None)
    if not toks or os.path.basename(toks[0]) != "git":
        return (False, None)
    sub, args, inline_config = _git_subcommand(toks[1:])
    if sub != "push":
        return (False, None)
    if inline_config:
        return (True, None)  # a push we can't vouch for → blocked by the hook
    # Positional args after 'push' — forms: `push <remote> <refspec>`,
    # `push <remote>`, `push`. Skip flags (none of push's flags take a separate
    # positional value we'd mistake for a refspec in normal use).
    positionals = [a for a in args if not a.startswith("-")]
    if len(positionals) < 2:
        return (True, None)
    refspec = positionals[1].lstrip("+")          # +src:dst force form
    dst = refspec.split(":", 1)[1] if ":" in refspec else refspec
    if dst.startswith("refs/heads/"):
        dst = dst[len("refs/heads/"):]
    return (True, dst or None)


def plan_branches(compiled: dict[str, Any]) -> set[str]:
    """All branch names declared across the plan's repos."""
    out: set[str] = set()
    for cfg in compiled.get("repos", {}).values():
        if isinstance(cfg, dict) and cfg.get("branch"):
            out.add(cfg["branch"])
    return out


# --- Branch-first enforcement ------------------------------------------------
# The harness REQUIRES that every change land on a new branch: an Edit/Write into
# a repo is blocked until that repo's HEAD is on the plan's declared branch (see
# branch_gate_violation, called from pre-tool-hook.py). For that to mean "a new
# branch" rather than "main", the plan's branch must not BE a default branch —
# heuristic_scan rejects main/master/HEAD so a plan can't declare its way around
# the gate.
DEFAULT_BRANCH_NAMES = {"main", "master", "head"}


def is_default_branch_name(name: str) -> bool:
    return name.strip().lower() in DEFAULT_BRANCH_NAMES


def repo_slug_for_path(path: str) -> str | None:
    """The repo slug a tool path lands in, or None if the path is not under
    /workspace/repos/<slug>/<rel>. Mirrors check_path_allowed's prefix parsing."""
    if not path.startswith(REPOS_PREFIX):
        return None
    remainder = path[len(REPOS_PREFIX):]
    if "/" not in remainder:
        return None
    return remainder.split("/", 1)[0]


def branch_gate_violation(slug: str, declared: str | None, current: str | None) -> str | None:
    """Decide whether editing repo `slug` is allowed under branch-first. Returns
    None if allowed (no branch declared to enforce, or the repo is already on its
    declared branch); otherwise a human-readable reason to block. Pure on purpose
    — the hook resolves `current` via git and passes it in, so this is testable
    without a working tree."""
    if not declared:
        return None  # nothing to pin against (schema requires branch, so rare)
    if current == declared:
        return None
    return (
        f"repo {slug!r} is on branch {current or '(unknown)'!r}, not the plan's "
        f"branch {declared!r}. The harness requires every change to land on a NEW "
        f"branch, so edits are blocked until the repo is on it. Run "
        f"`git -C {REPOS_PREFIX}{slug} checkout -b {declared}` first, then retry."
    )


def fail(code: int, msg: str) -> None:
    print(f"validate-plan: {msg}", file=sys.stderr)
    sys.exit(code)


def load_plan(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as f:
            raw_bytes = f.read()
    except OSError as e:
        fail(3, f"cannot read plan: {e}")

    try:
        raw_text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as e:
        fail(2, f"plan is not valid UTF-8 ({e}); refusing to process")

    if BIDI_AND_INVISIBLE.search(raw_text):
        fail(2, "plan contains bidi/invisible unicode characters; refusing")

    for ch in raw_text:
        cat = unicodedata.category(ch)
        if cat == "Cc" and ch not in ("\n", "\r", "\t"):
            fail(2, f"plan contains disallowed control character U+{ord(ch):04X}")

    try:
        return yaml.safe_load(raw_text)
    except yaml.YAMLError as e:
        fail(1, f"plan is not valid YAML: {e}")


def load_apply_slugs(path: Path) -> set[str] | None:
    """Read the `apply:` (or legacy `repos:`) list from repos.yaml and return the
    set of allowed slugs (basenames). Returns None if the file can't be read or
    parsed — the caller then skips the allowlist check (it's defense-in-depth on
    top of the per-path hook and the write token, not the only guard)."""
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    raw = data.get("apply")
    if raw is None:
        raw = data.get("repos")
    if not isinstance(raw, list):
        return None
    slugs: set[str] = set()
    for entry in raw:
        if isinstance(entry, str) and entry.strip():
            slugs.add(Path(entry.strip()).name)
    return slugs


def validate_schema(plan: dict[str, Any], schema_path: Path) -> None:
    try:
        with schema_path.open() as f:
            schema = json.load(f)
    except OSError as e:
        fail(3, f"cannot read schema: {e}")

    validator = Draft7Validator(schema)
    errors = sorted(validator.iter_errors(plan), key=lambda e: e.absolute_path)
    if errors:
        print("validate-plan: schema validation failed:", file=sys.stderr)
        for err in errors:
            loc = ".".join(str(p) for p in err.absolute_path) or "<root>"
            print(f"  {loc}: {err.message}", file=sys.stderr)
        sys.exit(1)


def heuristic_scan(
    plan_text: str,
    plan: dict[str, Any],
    allowed_apply_slugs: set[str] | None = None,
) -> list[str]:
    findings: list[str] = []
    lowered = plan_text.lower()

    for pat, label in SUSPICIOUS_SHELL_PATTERNS:
        if re.search(pat, lowered):
            findings.append(f"HARD: suspicious shell pattern [{label}]")

    for pat in INJECTION_MARKERS:
        if re.search(pat, lowered):
            findings.append(f"HARD: prompt-injection marker matched: {pat!r}")

    for pat in EXFIL_HOST_PATTERNS:
        if re.search(pat, lowered):
            findings.append(f"HARD: exfil-host pattern matched: {pat!r}")

    for blob in BASE64_RUN.findall(plan_text):
        try:
            decoded = base64.b64decode(blob, validate=True)
            findings.append(
                f"HARD: large base64 blob ({len(blob)} chars, decodes to {len(decoded)} bytes)"
            )
        except (ValueError, base64.binascii.Error):
            findings.append(f"WARN: long base64-looking run ({len(blob)} chars, not valid b64)")

    for url_match in re.finditer(r"https?://([A-Za-z0-9.-]+)", plan_text):
        host = url_match.group(1).lower()
        if host not in ALLOWED_URL_HOSTS and not host.endswith(".amazonaws.com"):
            findings.append(f"WARN: URL to non-allowlisted host: {host}")

    if len(plan_text) > 200_000:
        findings.append(f"HARD: plan exceeds 200KB ({len(plan_text)} bytes) — refusing")

    # Cross-reference: every step.repo must match a key in scope.repos.
    scope_repos = (plan.get("scope") or {}).get("repos", {})
    declared_repos = set(scope_repos.keys())

    # Branch-first: each repo's declared branch must be a NEW branch, never a
    # default one. The apply hook blocks edits until the repo is on this branch,
    # so a plan declaring `branch: main` would let changes land on main and defeat
    # the gate. (The schema already requires `branch`; this catches the value.)
    for slug, cfg in scope_repos.items():
        if isinstance(cfg, dict):
            branch = cfg.get("branch")
            if isinstance(branch, str) and is_default_branch_name(branch):
                findings.append(
                    f"HARD: repo {slug!r} declares branch {branch!r}, a default "
                    f"branch; the plan's branch must be a NEW branch so changes "
                    f"don't land on main/master"
                )
            # A '..' in a declared path escapes the repo subtree at edit time
            # (check_path_allowed also blocks it, but flag at promote so a path
            # that means to traverse never gets approved in the first place).
            for fp in cfg.get("file_paths", []):
                if isinstance(fp, str) and ".." in fp.split("/"):
                    findings.append(
                        f"HARD: repo {slug!r} file_path {fp!r} contains a '..' "
                        f"segment; paths must stay within the repo subtree"
                    )

    # Config-level allowlist: scope.repos may only name slugs declared in
    # repos.yaml's `apply:` list. Bounds what apply can touch at the config
    # level, on top of the write token's server-side scope.
    if allowed_apply_slugs is not None:
        for slug in sorted(declared_repos - allowed_apply_slugs):
            findings.append(
                f"HARD: scope.repos names {slug!r}, which is not in repos.yaml "
                f"'apply:' ({sorted(allowed_apply_slugs)}); apply may only touch "
                f"declared repos"
            )

    for step in plan.get("steps", []):
        sid = step.get("id", "?")
        stype = step.get("type", "?")
        srepo = step.get("repo")
        if stype in ("file_edit", "file_create", "file_delete", "github_action"):
            if not srepo:
                findings.append(f"HARD: step {sid} (type {stype}) missing 'repo'")
            elif srepo not in declared_repos:
                findings.append(
                    f"HARD: step {sid} references repo {srepo!r} not in scope.repos ({sorted(declared_repos)})"
                )
        if stype == "command":
            cp = step.get("command_prefix", "")
            allowed = (plan.get("scope") or {}).get("allowed_command_prefixes", [])
            if cp and not any(cp.startswith(a) or a.startswith(cp) for a in allowed):
                findings.append(
                    f"HARD: step {sid} command_prefix {cp!r} not in scope.allowed_command_prefixes"
                )

    return findings


def compile_allowlist(plan: dict[str, Any], out_path: Path) -> None:
    scope = plan.get("scope") or {}
    compiled = {
        "plan_id": plan["plan_id"],
        "repos": scope.get("repos", {}),
        "allowed_command_prefixes": scope.get("allowed_command_prefixes", []),
        "aws_resources": scope.get("aws_resources", []),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(compiled, indent=2))


def format_compiled_summary(
    compiled: dict[str, Any], *, indent: str = "  ", max_files: int = 5, max_cmds: int = 8
) -> str:
    """Render the in-scope-repos / commands / aws portion of a compiled allowlist
    as a human-readable block. Single source of truth for the plan summary the
    apply load banner (load-plan.sh) and the host promote preview (plan-promote.sh)
    both print — so the two never drift, and neither has to interpolate the
    compiled path into a heredoc (they read it from the env and call this)."""
    def _fmt_list(items: list, limit: int) -> str:
        if not items:
            return "(none)"
        shown = ", ".join(str(x) for x in items[:limit])
        extra = len(items) - limit
        return shown + (f" … (+{extra} more)" if extra > 0 else "")

    repos = compiled.get("repos", {})
    cmds = compiled.get("allowed_command_prefixes", [])
    aws = compiled.get("aws_resources", [])

    lines = [f"{indent}in-scope repos ({len(repos)}):"]
    if not repos:
        lines.append(f"{indent}  (none)")
    for slug, cfg in repos.items():
        cfg = cfg if isinstance(cfg, dict) else {}
        files = cfg.get("file_paths", [])
        lines.append(f"{indent}  • {slug} → {cfg.get('github', '?')}")
        lines.append(f"{indent}      branch:     {cfg.get('branch', '?')}")
        lines.append(f"{indent}      file_paths: {_fmt_list(files, max_files)}")
    lines.append(f"{indent}commands ({len(cmds)}): {_fmt_list(cmds, max_cmds)}")
    lines.append(
        f"{indent}aws_resources: {len(aws)} (informational — apply env has no AWS access)"
    )
    return "\n".join(lines)


def check_path_allowed(path: str, compiled: dict[str, Any]) -> bool:
    """
    Plan scope is per-repo: scope.repos.<slug>.file_paths is the list for that repo.
    Tool calls land at /workspace/repos/<slug>/<rel>.
    Strip the prefix, extract slug, look up that repo's file_paths, match <rel>.

    Anything outside /workspace/repos/ is rejected — the agent has no business
    writing to /workspace/hooks/ or /workspace/target-state/ etc.
    """
    # Reject any '..' path segment BEFORE matching. The file_paths globs match on
    # a string prefix (`src/**` → startswith("src/")), so without this an approved
    # `src/**` plan would allow `src/../../../etc/cron.d/pwn`,
    # `src/../../other-repo/...`, or `src/../../../home/dev/.claude/settings.json`
    # (overwriting this hook's own config). The '..' lives only in the tool-call
    # path, which the human never reviews — the promote diff shows only `src/**` —
    # so this is the sole line of defense at edit time.
    if ".." in path.split("/"):
        return False

    if not path.startswith(REPOS_PREFIX):
        return False

    remainder = path[len(REPOS_PREFIX):]
    if "/" not in remainder:
        return False
    slug, rel = remainder.split("/", 1)

    repo_scope = compiled.get("repos", {}).get(slug)
    if not isinstance(repo_scope, dict):
        return False

    for allowed in repo_scope.get("file_paths", []):
        if rel == allowed:
            return True
        if allowed.endswith("/**"):
            prefix = allowed[:-3]
            if rel == prefix or rel.startswith(prefix + "/"):
                return True
    return False


def check_command_allowed(command: str, compiled: dict[str, Any]) -> bool:
    cmd = command.strip()
    # A command carrying any shell operator can escape the prefix allowlist, so
    # reject it before matching. (pre-tool-hook.py checks this first to give the
    # agent a precise reason; the guard is repeated here so every caller of this
    # library function is protected too.)
    if command_shell_violation(cmd) is not None:
        return False
    # The git/gh workflow (branch → add → commit, plus push and open-PR) is
    # built in for every plan, so it need not appear in allowed_command_prefixes.
    # Publish verbs are allowed HERE but separately held by the publish-approval
    # gate (and a push is additionally checked against the plan's branches) in
    # pre-tool-hook.py.
    if classify_git_command(cmd) is not None:
        return True
    for prefix in compiled.get("allowed_command_prefixes", []):
        if cmd.startswith(prefix):
            return True
    return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True, type=Path)
    ap.add_argument("--schema", required=True, type=Path)
    ap.add_argument("--compile-to", required=True, type=Path)
    ap.add_argument(
        "--apply-repos", type=Path, default=None,
        help="path to repos.yaml; when given, scope.repos is checked against its "
             "'apply:' allowlist (a repo not listed there is a HARD failure)",
    )
    args = ap.parse_args()

    plan_text = args.plan.read_text(encoding="utf-8")
    plan = load_plan(args.plan)
    if not isinstance(plan, dict):
        fail(1, "plan root must be a YAML mapping")

    validate_schema(plan, args.schema)

    allowed_apply_slugs = None
    if args.apply_repos is not None:
        allowed_apply_slugs = load_apply_slugs(args.apply_repos)
        if allowed_apply_slugs is None:
            print(
                f"validate-plan: WARN: could not read apply allowlist from "
                f"{args.apply_repos}; skipping the repos.yaml allowlist check",
                file=sys.stderr,
            )

    findings = heuristic_scan(plan_text, plan, allowed_apply_slugs)
    hard = [f for f in findings if f.startswith("HARD:")]
    warn = [f for f in findings if f.startswith("WARN:")]

    for f in findings:
        print(f"validate-plan: {f}", file=sys.stderr)

    if hard:
        fail(2, f"{len(hard)} hard heuristic failure(s); refusing to compile allowlist")

    compile_allowlist(plan, args.compile_to)
    n_repos = len((plan.get("scope") or {}).get("repos", {}))
    print(
        f"validate-plan: ok — plan {plan['plan_id']} compiled to {args.compile_to} "
        f"({n_repos} repo(s), {len(plan['steps'])} steps, {len(warn)} warning(s))"
    )


if __name__ == "__main__":
    main()
