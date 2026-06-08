#!/usr/bin/env python3
"""
validate_plan.py — multi-repo apply-side plan validation.

Two roles in one script:
  1) Pre-flight (run from apply post-create.sh): validates the approved plan against
     the JSON schema and a battery of content heuristics, then compiles a strict
     allowlist file used by the per-call hook.
  2) Library: pre-tool-hook.py imports check_path_allowed / check_command_allowed.

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

BIDI_AND_INVISIBLE = re.compile(
    r"[​-\u200F\u202A-\u202E\u2066-\u2069﻿­]"
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
SHELL_OPERATORS = [
    (";", "command separator ';'"),
    ("&", "background / chain '&' or '&&'"),
    ("|", "pipe '|' or '||'"),
    ("`", "backtick command substitution"),
    ("$(", "$(…) command substitution"),
    ("<(", "process substitution '<(…)'"),
    (">", "output redirection '>' / '>>'"),
    ("<", "input redirection '<'"),
    ("\n", "newline (multiple commands)"),
    ("\r", "carriage return (multiple commands)"),
]


def command_shell_violation(command: str) -> str | None:
    """Return a human-readable label for the first disallowed shell operator in
    `command`, or None if the command is a single, simple, non-redirecting
    command. Used to stop an allowed command prefix from carrying extra
    commands or arbitrary file writes past the allowlist."""
    for token, label in SHELL_OPERATORS:
        if token in command:
            return label
    return None


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
    declared_repos = set((plan.get("scope") or {}).get("repos", {}).keys())

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


def check_path_allowed(path: str, compiled: dict[str, Any]) -> bool:
    """
    Plan scope is per-repo: scope.repos.<slug>.file_paths is the list for that repo.
    Tool calls land at /workspace/repos/<slug>/<rel>.
    Strip the prefix, extract slug, look up that repo's file_paths, match <rel>.

    Anything outside /workspace/repos/ is rejected — the agent has no business
    writing to /workspace/hooks/ or /workspace/target-state/ etc.
    """
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
