#!/usr/bin/env python3
"""
gen_devcontainer.py — render .devcontainer/{research,apply}/devcontainer.json
from repos.yaml + your credentials file, and write the per-env secret files
that Docker injects via --env-file.

Why this exists: VS Code's ${localEnv:...} reads the *launch* environment,
which differs per OS and per launch method (GUI vs terminal). To make GUI
"Reopen in Container" work the same on macOS and Linux, we resolve everything
at setup time instead. This script bakes the non-secret config straight into
the generated devcontainer.json, and points each container's --env-file at a
secret file that lives OUTSIDE the mounted workspace. Docker reads that file
itself at container-create time, so nothing depends on the launch environment.

Inputs (read from the process environment — setup.sh sources creds.env first,
so $HOME and quoting are already expanded by the shell):
    GH_TOKEN_RESEARCH_FILE   required — path to a file holding the read-only PAT
    GH_TOKEN_APPLY_FILE      required — path to a file holding the write PAT
    ALLOWED_DOMAINS_EXTRA    optional — space-separated egress domains NOT tied to
                             any platform; merged with each platform's allow_domains
                             and exported to the research container as the compiled
                             ALL_ALLOWED_DOMAINS that its firewall consumes
    LEASH_CREDS              optional — path to creds.env (pointers only); may be
                                        in-tree since only app/ is mounted
    LEASH_GENERATED_DIR      optional — where the real-token secret files are
                                        written; defaults to ~/.config/plan-apply-leash/
                                        .generated, kept out of the repo tree

Read-only platform access (AWS, Kubernetes, anything else) for the RESEARCH env
is declared in research-access.yaml, resolved by research_access.py into mounts,
container env, devcontainer Features, and egress domains. The APPLY env never
reads it — apply gets a scoped GitHub token and nothing else.

repos.yaml shape:
    apply:                          # required — paths the APPLY agent may write
      - /Users/you/code/foo-service
      - /Users/you/code/bar-cli
    research:                       # optional — owner/repo the RESEARCH agent
      - acme/foo-service            #   may read; empty/omitted = read anything
      - acme/shared-config          #   the read-only PAT can reach

The slug for each apply repo is its basename. Two repos with the same basename
is a hard error — rename one locally. Each apply path must exist and be a git
repo. `repos:` is accepted as a deprecated alias for `apply:`.

The research scope, when non-empty, is baked into the research container as
RESEARCH_REPO_SCOPE (newline-joined) so session-start.py can tell the agent to
stay within it. The apply repos are auto-included in that scope via their
GitHub `origin` remote, so the change targets are always in research scope.

Exit 0 on success. Non-zero on any validation error; stderr explains.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

from research_access import load as load_research_access, ResearchAccessError, Resolved


REPO_ROOT = Path(__file__).resolve().parent.parent
REPOS_YAML = REPO_ROOT / "repos.yaml"
RESEARCH_ACCESS_YAML = REPO_ROOT / "research-access.yaml"
# Trusted house-style file mounted READ-ONLY into the apply container, surfaced
# to the agent by session-start.py. Committed to the harness (not user-specific),
# so it always exists; mounted read-only so the agent can't rewrite its own rules.
APPLY_CONVENTIONS_FILE = REPO_ROOT / "apply-conventions.md"

# Git identity the apply agent commits as. The apply agent commits but the base
# image sets no user.name/email; post-create.sh configures it from these and
# fails closed if unset. Sourced from creds.env (GIT_IDENTITY_*); not a secret,
# so baked into containerEnv (not the --env-file).
#
# The DEFAULT is a NEUTRAL harness identity on purpose — set your real (corporate)
# identity in your private creds.env. Two reasons: (1) gen_devcontainer.py is
# committed, so a personal email here would leak into the repo and make every
# operator's agent commit as that person; (2) honest provenance — an unattended
# agent commit should read as "the apply agent", not silently impersonate a human
# who never reviewed it. The noreply.github.com address is GitHub's convention for
# a non-personal author and won't link to anyone's account.
DEFAULT_GIT_IDENTITY_NAME = "plan-apply-leash apply agent"
DEFAULT_GIT_IDENTITY_EMAIL = "apply-agent@users.noreply.github.com"
# Mutable per-target state. Lives at the repo root, a SIBLING of app/ — and
# /workspace is bound to app/, so state/ is NOT inside the workspace bind. Each
# env gets only the subtrees it needs, mounted explicitly below with the right
# RW/RO. That is how research-state is kept out of apply: it is simply never
# mounted there, enforced by the kernel rather than by a settings deny.
STATE_ROOT = REPO_ROOT / "state"
SLUG_RE = re.compile(r"^[a-zA-Z0-9._-]+$")
# GitHub owner/repo, matching the plan schema's `github` pattern.
GH_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
# Pull owner/repo out of an https or ssh GitHub remote URL.
GH_REMOTE_RE = re.compile(
    r"^(?:https?://[^/]*github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"([A-Za-z0-9._-]+/[A-Za-z0-9._-]+?)(?:\.git)?/?$"
)

DEFAULT_CREDS = Path.home() / ".config" / "plan-apply-leash" / "creds.env"

TARGETS = [
    {
        "name": "research",
        "template": REPO_ROOT / ".devcontainer/research/devcontainer.template.json",
        "output": REPO_ROOT / ".devcontainer/research/devcontainer.json",
        "readonly": True,
        "token_file_var": "GH_TOKEN_RESEARCH_FILE",
        "secret_file": "secrets.research.env",
        "wants_infra": True,  # research mounts read-only platforms from research-access.yaml
        # (state_subtree, container_target, readonly). Research writes drafts/clones/
        # notes; it never sees approved-plans or audit.
        "state_mounts": [("research", "/workspace/target-state/research", False)],
        "mount_repos_yaml": False,
    },
    {
        "name": "apply",
        "template": REPO_ROOT / ".devcontainer/apply/devcontainer.template.json",
        "output": REPO_ROOT / ".devcontainer/apply/devcontainer.json",
        "readonly": False,
        "token_file_var": "GH_TOKEN_APPLY_FILE",
        "secret_file": "secrets.apply.env",
        "wants_infra": False,  # apply gets no platform credentials, ever
        # Apply gets the approved plan READ-ONLY (kernel-enforced — the agent
        # can't alter the approved plan even if a settings deny is missed) plus a
        # read-write audit dir for the tally + compiled allowlist. It does NOT get
        # research/ at all — that subtree is simply absent from this list.
        "state_mounts": [
            ("approved-plans", "/workspace/target-state/approved-plans", True),   # RO
            ("audit",          "/workspace/target-state/audit",          False),  # RW
        ],
        "mount_repos_yaml": True,  # load-plan's --apply-repos allowlist re-check
        "mount_conventions": True,  # read-only house-style file (see APPLY_CONVENTIONS_FILE)
    },
]


def die(msg: str, code: int = 1) -> None:
    print(f"gen-devcontainer: {msg}", file=sys.stderr)
    sys.exit(code)


def env_clean(name: str) -> str:
    """Read an env var and strip surrounding whitespace; '' if unset."""
    return (os.environ.get(name) or "").strip()


def token_fingerprint(token: str) -> str:
    """Stable, non-reversible id for a GitHub token. Per-target state lives under
    state/by-token/<fp>/, so rotating a token swaps in a fresh tree and rotating
    back remounts the cached one (nothing is deleted). 16 hex chars of sha256:
    enough to avoid collisions, far too short to be a useful token leak. Mirrored
    in bash by scripts/_state_lib.sh:leash_token_fp — keep the two in sync; their
    agreement is pinned by tests/test_state_fingerprint.py."""
    return hashlib.sha256(token.strip().encode()).hexdigest()[:16]


def write_state_label(state_base: Path, token: str) -> None:
    """Drop a small human-readable note in a token's state dir so the otherwise
    opaque by-token/<fp>/ directories are navigable. Records only the 8-char
    token prefix (already echoed by setup.sh), never the token itself."""
    try:
        (state_base / ".token-info").write_text(
            f"plan-apply-leash state for GitHub token {token.strip()[:8]}… "
            f"(fingerprint {state_base.name}).\n"
            "Rotating the token mounts a fresh sibling dir; rotating back remounts "
            "this one.\nSafe to delete to discard this token's cached state.\n"
        )
    except OSError:
        pass


def generated_dir() -> Path:
    # These files hold REAL token text, so they must live OUTSIDE the repo tree
    # even when LEASH_CREDS points at an in-tree creds.env (which holds only
    # pointers). Default to the home config dir, NOT the creds file's parent;
    # override with LEASH_GENERATED_DIR if you really want them elsewhere.
    override = os.environ.get("LEASH_GENERATED_DIR")
    return Path(override).expanduser() if override else DEFAULT_CREDS.parent / ".generated"


def resolve_token(file_var: str) -> str:
    """Read a PAT from the file that $<file_var> points at."""
    path = env_clean(file_var)
    if not path:
        die(
            f"{file_var} is empty — point it at a file containing your PAT "
            f"(see creds.env.example) and re-run scripts/setup.sh"
        )
    p = Path(path).expanduser()
    if not p.is_file():
        die(f"{file_var} points at a missing file: {p}")
    token = p.read_text().strip()
    if not token:
        die(f"{file_var} file is empty: {p}")
    return token


def load_repos() -> tuple[list[tuple[str, Path]], list[str]]:
    """Parse repos.yaml into (apply_repos, research_scope).

    apply_repos    — [(slug, path)] of write targets; mounted in both envs.
    research_scope — explicit list of owner/repo the research agent may read.
    """
    if not REPOS_YAML.exists():
        die(f"missing {REPOS_YAML.relative_to(REPO_ROOT)} — copy repos.yaml.example and edit")

    try:
        data = yaml.safe_load(REPOS_YAML.read_text()) or {}
    except yaml.YAMLError as e:
        die(f"repos.yaml is not valid YAML: {e}")

    if not isinstance(data, dict):
        die("repos.yaml must be a mapping with an 'apply:' key (see repos.yaml.example)")

    # `repos:` is the pre-split name for `apply:`; accept it for back-compat.
    raw = data.get("apply")
    if raw is None and "repos" in data:
        print(
            "gen-devcontainer: NOTE: 'repos:' is deprecated — rename it to 'apply:' "
            "in repos.yaml (see repos.yaml.example)",
            file=sys.stderr,
        )
        raw = data["repos"]
    if raw is None:
        die("repos.yaml must have a top-level 'apply:' key with a list of host paths")
    if not isinstance(raw, list) or not raw:
        die("'apply:' must be a non-empty list of host paths")

    seen: dict[str, str] = {}
    apply_repos: list[tuple[str, Path]] = []
    for entry in raw:
        if not isinstance(entry, str):
            die(f"each apply entry must be a string path, got {entry!r}")
        path = Path(entry).expanduser()
        if not path.is_absolute():
            die(f"apply repo path must be absolute: {entry!r}")
        if not path.exists():
            die(f"apply repo path does not exist: {path}")
        if not path.is_dir():
            die(f"apply repo path is not a directory: {path}")
        if not (path / ".git").exists():
            die(f"apply repo path is not a git repo (no .git/): {path}")

        slug = path.name
        if not SLUG_RE.match(slug):
            die(
                f"repo basename {slug!r} is not a valid slug "
                f"(must match {SLUG_RE.pattern}); rename the directory or symlink it"
            )
        if slug in seen:
            die(
                f"basename collision: both {seen[slug]} and {path} resolve to "
                f"slug {slug!r}. Rename one locally."
            )
        seen[slug] = str(path)
        apply_repos.append((slug, path))

    research_raw = data.get("research") or []
    if not isinstance(research_raw, list):
        die("'research:' must be a list of owner/repo identifiers (or omitted)")
    research_scope: list[str] = []
    for entry in research_raw:
        if not isinstance(entry, str):
            die(f"each research entry must be an 'owner/repo' string, got {entry!r}")
        ident = entry.strip()
        if not GH_REPO_RE.match(ident):
            die(
                f"research entry {entry!r} is not a valid GitHub 'owner/repo' "
                f"identifier (must match {GH_REPO_RE.pattern})"
            )
        research_scope.append(ident)

    return apply_repos, research_scope


def derive_github_identifier(path: Path) -> str | None:
    """Return the owner/repo of a repo's `origin` remote, or None if it can't
    be determined (no git, no origin, or a non-GitHub remote)."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    m = GH_REMOTE_RE.match(proc.stdout.strip())
    return m.group(1) if m else None


def compute_research_scope(
    explicit: list[str], apply_repos: list[tuple[str, Path]]
) -> list[str]:
    """When research is scoped (explicit list non-empty), the effective scope is
    the explicit list plus the GitHub identifier of every apply repo, deduped
    case-insensitively. An empty explicit list means 'roam freely' — return []."""
    if not explicit:
        return []

    scope: list[str] = []
    seen_lower: set[str] = set()

    def add(ident: str) -> None:
        key = ident.lower()
        if key not in seen_lower:
            seen_lower.add(key)
            scope.append(ident)

    for ident in explicit:
        add(ident)

    for slug, path in apply_repos:
        ident = derive_github_identifier(path)
        if ident:
            add(ident)
        else:
            print(
                f"gen-devcontainer: WARNING: could not derive a GitHub identifier "
                f"for apply repo {slug!r} ({path}) — it stays readable on disk but "
                f"won't be added to the research GitHub scope",
                file=sys.stderr,
            )
    return scope


def write_secret_file(gen_dir: Path, filename: str, token: str) -> Path:
    """Write GH_TOKEN=<token> as a Docker --env-file (no export, no quotes)."""
    gen_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(gen_dir, 0o700)
    except OSError:
        pass
    path = gen_dir / filename
    path.write_text(f"GH_TOKEN={token}\n")
    os.chmod(path, 0o600)
    return path


def inject_research_access(target: dict, config: dict, resolved: Resolved) -> None:
    """Merge a resolved research-access into a devcontainer config, in place.

    For platform-enabled targets (research) this adds read-only credential
    mounts, container env, the devcontainer Features that install each CLI, and
    the platform egress domains. For every other target (apply) it is a no-op,
    which is how the "apply gets no platform credentials, ever" invariant is
    enforced structurally — not just by documentation.
    """
    if not target.get("wants_infra"):
        return  # apply never gets platform credentials

    env = config.setdefault("containerEnv", {})
    config.setdefault("mounts", []).extend(resolved.mounts)
    env.update(resolved.container_env)

    if resolved.features:
        features = dict(config.get("features", {}))
        features.update(resolved.features)
        config["features"] = features

    # Egress: the optional creds.env input ALLOWED_DOMAINS_EXTRA (domains not
    # tied to any platform), merged with every platform's declared allow_domains.
    # The compiled union is exported under a DIFFERENT name, ALL_ALLOWED_DOMAINS,
    # so the input and the result never collide. (init-firewall.sh adds the base
    # infra domains on top of this — they are not enumerated here.)
    domains = env_clean("ALLOWED_DOMAINS_EXTRA").split()
    for d in resolved.allow_domains:
        if d not in domains:
            domains.append(d)
    if domains:
        env["ALL_ALLOWED_DOMAINS"] = " ".join(domains)

    # Pipe-joined "name\tmount_at" for the post-create / session-start banners.
    # Pipe (not newline) because containerEnv is baked into Dockerfile-with-features
    # as ENV instructions when devcontainer Features are present, and a literal
    # newline in the value spills onto a second line that the Dockerfile parser
    # then reads as a new (unknown) instruction.
    if resolved.summary:
        env["RESEARCH_PLATFORMS"] = "|".join(f"{n}\t{t}" for n, t in resolved.summary)


def render(
    target: dict,
    repos: list[tuple[str, Path]],
    gen_dir: Path,
    token: str,
    research_scope: list[str],
    research_access: Resolved,
) -> None:
    if not target["template"].exists():
        die(f"missing template: {target['template'].relative_to(REPO_ROOT)}")

    try:
        config = json.loads(target["template"].read_text())
    except json.JSONDecodeError as e:
        die(f"template {target['template'].name} is not valid JSON ({e})")

    # Secret token: written outside the workspace, injected by Docker --env-file.
    secret_path = write_secret_file(gen_dir, target["secret_file"], token)

    mounts: list[str] = list(config.get("mounts", []))
    container_env: dict = dict(config.get("containerEnv", {}))

    # Repo mounts: read-only in research, read-write in apply.
    repo_flags = "type=bind,readonly,consistency=cached" if target["readonly"] else "type=bind,consistency=cached"
    for slug, host_path in repos:
        mounts.append(f"source={host_path},target=/workspace/repos/{slug},{repo_flags}")

    # State-subtree mounts: each env gets only the subtrees it needs, with the
    # right RW/RO. Emitted as ABSOLUTE host paths (like the repo mounts above) —
    # they point under the repo root's state/, which is OUTSIDE the
    # ${localWorkspaceFolder}/app workspace bind, so it can't be expressed
    # relative to it. The bind SOURCE must exist before launch (Docker otherwise
    # auto-creates it root-owned), so mkdir each here, owned by whoever runs this.
    #
    # The host source is namespaced by a fingerprint of THIS target's token:
    # state/by-token/<fp>/<subtree>. Rotating the token points the SAME container
    # mount target at a fresh host tree (and rotating back at the cached one) —
    # the container-side path never changes, so nothing else in the harness cares.
    state_base = STATE_ROOT / "by-token" / token_fingerprint(token)
    if target.get("state_mounts"):
        state_base.mkdir(parents=True, exist_ok=True)
        write_state_label(state_base, token)
    for sub, container_target, readonly in target.get("state_mounts", []):
        host = state_base / sub
        host.mkdir(parents=True, exist_ok=True)
        state_flags = "type=bind,readonly,consistency=cached" if readonly else "type=bind,consistency=cached"
        mounts.append(f"source={host},target={container_target},{state_flags}")

    # repos.yaml: apply needs it read-only for load-plan.sh's --apply-repos
    # allowlist re-check. It's no longer reachable via a whole-repo /workspace
    # bind, so mount it explicitly. (Research doesn't use it.)
    if target.get("mount_repos_yaml"):
        mounts.append(f"source={REPOS_YAML},target=/workspace/repos.yaml,type=bind,readonly,consistency=cached")

    # Trusted house-style file: read-only so the apply agent can't rewrite the
    # conventions it's told to follow. Committed to the harness, so it exists.
    if target.get("mount_conventions"):
        if not APPLY_CONVENTIONS_FILE.is_file():
            die(f"missing {APPLY_CONVENTIONS_FILE.relative_to(REPO_ROOT)} — it backs the apply house-conventions injection")
        mounts.append(
            f"source={APPLY_CONVENTIONS_FILE},target=/workspace/apply-conventions.md,type=bind,readonly,consistency=cached"
        )

    # Research focus scope: newline-joined owner/repo list the agent should stay
    # within. Only set when non-empty; unset means "roam whatever the PAT allows".
    if target["name"] == "research" and research_scope:
        container_env["RESEARCH_REPO_SCOPE"] = "\n".join(research_scope)

    # Apply git identity: the agent commits, so configure who it commits as.
    # post-create.sh reads GIT_IDENTITY_* (fail-closed) to set user.name/email;
    # GIT_AUTHOR_*/GIT_COMMITTER_* are a deterministic fallback if ~/.gitconfig is
    # ever missing. Sourced from creds.env, defaulting to the corporate identity.
    if target["name"] == "apply":
        ident_name = env_clean("GIT_IDENTITY_NAME") or DEFAULT_GIT_IDENTITY_NAME
        ident_email = env_clean("GIT_IDENTITY_EMAIL") or DEFAULT_GIT_IDENTITY_EMAIL
        container_env["GIT_IDENTITY_NAME"] = ident_name
        container_env["GIT_IDENTITY_EMAIL"] = ident_email
        container_env["GIT_AUTHOR_NAME"] = ident_name
        container_env["GIT_AUTHOR_EMAIL"] = ident_email
        container_env["GIT_COMMITTER_NAME"] = ident_name
        container_env["GIT_COMMITTER_EMAIL"] = ident_email

    config["mounts"] = mounts
    config["containerEnv"] = container_env

    # Read-only platform credentials for research from research-access.yaml:
    # mounts, container env, devcontainer Features, and egress domains. No-op
    # for apply, which gets a scoped GitHub token and nothing else.
    inject_research_access(target, config, research_access)

    run_args = list(config.get("runArgs", []))
    run_args += ["--env-file", str(secret_path)]
    config["runArgs"] = run_args

    banner = (
        "// GENERATED by scripts/gen_devcontainer.py from repos.yaml + your creds file.\n"
        "// Edit repos.yaml or creds.env and re-run scripts/setup.sh, then rebuild.\n"
        "// The GitHub token is NOT here — it lives in the --env-file outside the workspace.\n"
    )
    target["output"].write_text(banner + json.dumps(config, indent=2) + "\n")


def main() -> None:
    # Resolve every token up front (each call dies on a missing/empty file), so
    # a bad pointer never leaves a half-generated set of files.
    tokens = {t["name"]: resolve_token(t["token_file_var"]) for t in TARGETS}

    repos, research_explicit = load_repos()
    research_scope = compute_research_scope(research_explicit, repos)
    # Resolve research platform access once, up front, so a bad entry dies
    # cleanly here rather than mid-render.
    try:
        research_access = load_research_access(RESEARCH_ACCESS_YAML, REPO_ROOT)
    except ResearchAccessError as e:
        die(str(e))
    gen_dir = generated_dir()
    for t in TARGETS:
        render(t, repos, gen_dir, tokens[t["name"]], research_scope, research_access)

    rel = REPOS_YAML.relative_to(REPO_ROOT)
    print(f"gen-devcontainer: rendered {len(repos)} apply repo mount(s) from {rel}:")
    for slug, host_path in repos:
        print(f"  • {slug:25s} → {host_path}")
    if research_scope:
        n_extra = len(research_scope) - len(research_explicit)
        print(
            f"  research-scope: {len(research_scope)} repo(s) "
            f"({len(research_explicit)} listed + {n_extra} from apply targets) — "
            + ", ".join(research_scope)
        )
    else:
        print("  research-scope: (none — research may read anything its PAT allows)")
    if research_access.summary:
        print(
            f"  platforms: {len(research_access.summary)} read-only — "
            + ", ".join(f"{n} → {t}" for n, t in research_access.summary)
        )
    else:
        print("  platforms: (none — research uses GitHub only)")
    print(f"  secret env-files written to {gen_dir} (chmod 600, outside the workspace)")


if __name__ == "__main__":
    main()
