#!/usr/bin/env python3
"""
research_access.py — resolve research-access.yaml into the devcontainer pieces
the RESEARCH env needs: read-only credential mounts, container env, the
devcontainer Features that install each platform's CLI, and the egress domains
those CLIs need at runtime.

This is a research-only concern. The apply env never calls this — apply gets a
scoped GitHub token and nothing else. gen_devcontainer.py imports `load()`
for the research target only.

Two tiers per platform:
  • Tier A (no `install`): mount a credential + set env + list allow_domains.
    The agent reaches the platform over HTTP; nothing is added to the image.
  • Tier B (`install: <catalog-name>`): the catalog supplies a devcontainer
    Feature that installs the CLI plus default mount_at / allow_domains. An
    `install` value not in the catalog is a hard error — there is no escape
    hatch to an arbitrary installer.

Public API:
    CATALOG                      — the curated known-CLI map
    Resolved                     — dataclass of resolved devcontainer pieces
    ResearchAccessError          — raised on any invalid config
    parse_platforms(data, root)  — validate already-parsed YAML → Resolved
    load(path, root)             — read+parse a research-access.yaml → Resolved

Run standalone to inspect a file:
    python3 scripts/research_access.py [--file research-access.yaml] [--workspace .]
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

# Where credentials are allowed to land in the container. The research image
# runs as the unprivileged `dev` user; keep mounts inside its home.
HOME = "/home/dev"
# Reserved by the research devcontainer (auth volumes); a platform mount here
# would collide with the harness's own state.
RESERVED_TARGETS = (f"{HOME}/.claude", f"{HOME}/.config/gh")

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
# An egress-allowlist host: a bare hostname, or a leading-dot suffix wildcard
# (".amazonaws.com" matches the apex and every subdomain). No scheme/path/port.
HOSTNAME_RE = re.compile(r"^\.?[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*$")
# {ENV_VAR} placeholders in catalog allow_domains, filled from the entry's env.
TEMPLATE_RE = re.compile(r"\{([A-Z0-9_]+)\}")


# ── The catalog: the only CLIs Tier B can install ──────────────────────────────
# Each entry maps an install-name to a devcontainer Feature (the Docker-native
# installer) plus defaults. Curated on purpose: prefer official
# `devcontainers/features`, and a name absent here cannot be installed.
#
# NOTE: Feature refs use floating `:1` tags below. Pin each to an immutable
#       @sha256 digest before release — a floating tag is a supply-chain hole
#       for a security-posture tool. (Resolve digests with the dev container
#       CLI or `oras`/`crane`; out of scope for an offline run.)
#
# Fields:
#   feature       — the Feature reference injected into devcontainer.json
#   options       — Feature options (devcontainer.json passes these through)
#   mount_at      — default container path for `credential` (overridable)
#   container_env — extra env the CLI expects (merged into containerEnv)
#   allow_domains — default runtime egress; {ENV_VAR} expands from the entry env
CATALOG: dict[str, dict] = {
    "aws-cli": {
        "feature": "ghcr.io/devcontainers/features/aws-cli:1",
        "options": {},
        "mount_at": f"{HOME}/.aws",
        "container_env": {},
        # One suffix covers every AWS service in every region. The egress proxy
        # matches on hostname at connect time (see init-firewall.sh), so a
        # leading-dot wildcard is honored directly — no per-service / per-region
        # enumeration, and immune to AWS's rotating endpoint IPs. `.api.aws`
        # covers the newer dualstack/IPv6 endpoints some SDKs prefer.
        "allow_domains": [
            ".amazonaws.com",
            ".api.aws",
        ],
    },
    "kubectl": {
        "feature": "ghcr.io/devcontainers/features/kubectl-helm-minikube:1",
        "options": {"helm": "none", "minikube": "none"},
        "mount_at": f"{HOME}/.kube/config",
        "container_env": {"KUBECONFIG": f"{HOME}/.kube/config"},
        # The cluster API server is site-specific — the user supplies it via
        # the entry's allow_domains.
        "allow_domains": [],
    },
    "azure-cli": {
        "feature": "ghcr.io/devcontainers/features/azure-cli:1",
        "options": {},
        "mount_at": f"{HOME}/.azure",
        "container_env": {},
        "allow_domains": [
            "management.azure.com",
            "login.microsoftonline.com",
            "graph.microsoft.com",
        ],
    },
}


class ResearchAccessError(Exception):
    """Any invalid research-access.yaml. Carries a human-readable message."""


@dataclass
class Resolved:
    mounts: list[str] = field(default_factory=list)            # docker bind strings
    container_env: dict[str, str] = field(default_factory=dict)
    features: dict[str, dict] = field(default_factory=dict)    # feature ref → options
    allow_domains: list[str] = field(default_factory=list)     # deduped, ordered
    summary: list[tuple[str, str]] = field(default_factory=list)  # (name, mount_at)


def _err(name: str | None, msg: str) -> ResearchAccessError:
    where = f"platform {name!r}: " if name else ""
    return ResearchAccessError(f"research-access.yaml: {where}{msg}")


def _expand(value: str, env: dict[str, str], name: str) -> str:
    """Fill {ENV_VAR} placeholders from the platform's env; missing → error."""
    def sub(m: re.Match) -> str:
        var = m.group(1)
        if var not in env:
            raise _err(
                name,
                f"egress domain {value!r} needs env.{var}, which isn't set "
                f"(catalog CLIs like aws-cli require it to build their endpoint list)",
            )
        return env[var]

    return TEMPLATE_RE.sub(sub, value)


def _norm_target(name: str, mount_at: str) -> str:
    """Validate a container mount path: absolute, under /home/dev, no traversal,
    not a reserved harness path."""
    if not isinstance(mount_at, str) or not mount_at.strip():
        raise _err(name, "mount_at must be a non-empty string")
    p = PurePosixPath(mount_at)
    if not p.is_absolute():
        raise _err(name, f"mount_at must be absolute, got {mount_at!r}")
    norm = os.path.normpath(mount_at)  # collapses any '..'
    if norm != HOME and not norm.startswith(HOME + "/"):
        raise _err(name, f"mount_at must be under {HOME}/, got {mount_at!r}")
    for reserved in RESERVED_TARGETS:
        if norm == reserved or norm.startswith(reserved + "/"):
            raise _err(name, f"mount_at {mount_at!r} collides with reserved harness path {reserved}")
    return norm


def _resolve_credential(name: str, credential: str, workspace_root: Path) -> Path:
    """Expand and validate a host credential path: must exist and resolve
    OUTSIDE the workspace (a secret in the tree is readable by research)."""
    if not isinstance(credential, str) or not credential.strip():
        raise _err(name, "credential must be a non-empty host path")
    host = Path(credential).expanduser()
    real = Path(os.path.realpath(host))
    if not real.exists():
        raise _err(name, f"credential path does not exist on host: {host}")
    ws = Path(os.path.realpath(workspace_root))
    try:
        common = os.path.commonpath([str(real), str(ws)])
    except ValueError:  # different drives (Windows) — definitely outside
        common = ""
    if common == str(ws):
        raise _err(
            name,
            f"credential {host} resolves inside the workspace ({ws}). Keep "
            "credentials outside the repo — research mounts the tree read-only "
            "and could read it.",
        )
    return real


def _coerce_env(name: str, env: object) -> dict[str, str]:
    if env is None:
        return {}
    if not isinstance(env, dict):
        raise _err(name, "env must be a mapping of NAME: value")
    out: dict[str, str] = {}
    for k, v in env.items():
        if not isinstance(k, str) or not k:
            raise _err(name, f"env key must be a non-empty string, got {k!r}")
        if isinstance(v, (dict, list)):
            raise _err(name, f"env[{k}] must be a scalar, got {type(v).__name__}")
        out[k] = "" if v is None else str(v)
    return out


def _coerce_domains(name: str, domains: object) -> list[str]:
    if domains is None:
        return []
    if not isinstance(domains, list):
        raise _err(name, "allow_domains must be a list of hostnames")
    out: list[str] = []
    for d in domains:
        if not isinstance(d, str) or not HOSTNAME_RE.match(d):
            raise _err(name, f"allow_domains entry {d!r} is not a bare hostname (no scheme/path/port)")
        out.append(d)
    return out


def parse_platforms(data: object, workspace_root: Path) -> Resolved:
    """Validate already-parsed research-access.yaml content and resolve it.

    `data` is the object yaml.safe_load returns (or None for an empty file).
    Raises ResearchAccessError on the first problem.
    """
    res = Resolved()
    if data is None:
        return res
    if not isinstance(data, dict):
        raise ResearchAccessError(
            "research-access.yaml: top level must be a mapping with a 'platforms:' list"
        )

    platforms = data.get("platforms")
    if platforms is None:
        return res
    if not isinstance(platforms, list):
        raise ResearchAccessError("research-access.yaml: 'platforms' must be a list")

    seen_names: set[str] = set()
    seen_targets: dict[str, str] = {}

    for entry in platforms:
        if not isinstance(entry, dict):
            raise ResearchAccessError(f"research-access.yaml: each platform must be a mapping, got {entry!r}")

        name = entry.get("name")
        if not isinstance(name, str) or not NAME_RE.match(name or ""):
            raise _err(None, f"each platform needs a 'name' matching {NAME_RE.pattern}, got {name!r}")
        if name in seen_names:
            raise _err(name, "duplicate platform name")
        seen_names.add(name)

        install = entry.get("install")
        cat: dict = {}
        if install is not None:
            if not isinstance(install, str) or install not in CATALOG:
                raise _err(
                    name,
                    f"install: {install!r} is not in the catalog "
                    f"(known: {', '.join(sorted(CATALOG))}). Drop `install` to mount "
                    "it config-only (Tier A), or add a catalog entry.",
                )
            cat = CATALOG[install]

        env = _coerce_env(name, entry.get("env"))

        # mount_at: entry override, else catalog default; Tier A must specify it.
        mount_at = entry.get("mount_at") or cat.get("mount_at")
        if not mount_at:
            raise _err(name, "mount_at is required (no `install` to default it from)")
        target = _norm_target(name, mount_at)
        if target in seen_targets:
            raise _err(name, f"mount_at {target} already used by platform {seen_targets[target]!r}")
        seen_targets[target] = name

        host = _resolve_credential(name, entry.get("credential"), workspace_root)

        # Build this platform's egress domains: catalog defaults (template-
        # expanded from env) then the entry's own, deduped in order.
        domains: list[str] = []
        for d in cat.get("allow_domains", []):
            domains.append(_expand(d, env, name))
        domains.extend(_coerce_domains(name, entry.get("allow_domains")))

        # Emit the resolved pieces.
        res.mounts.append(f"source={host},target={target},type=bind,readonly")
        res.container_env.update(cat.get("container_env", {}))
        res.container_env.update(env)  # entry env wins over catalog container_env
        if install is not None:
            res.features[cat["feature"]] = dict(cat.get("options", {}))
        for d in domains:
            if d not in res.allow_domains:
                res.allow_domains.append(d)
        res.summary.append((name, target))

    return res


def load(path: Path, workspace_root: Path) -> Resolved:
    """Read a research-access.yaml from disk and resolve it. A missing file is
    not an error — it means 'no extra platforms', returning an empty Resolved."""
    if not path.exists():
        return Resolved()
    try:
        import yaml  # lazy: only the file-reading path needs pyyaml
    except ImportError as e:
        raise ResearchAccessError(
            "pyyaml is required to read research-access.yaml — "
            "install it: pip3 install --user pyyaml"
        ) from e

    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise ResearchAccessError(f"{path.name} is not valid YAML: {e}") from e
    return parse_platforms(data, workspace_root)


def _main(argv: list[str]) -> int:
    file = Path("research-access.yaml")
    workspace = Path(".")
    it = iter(argv)
    for arg in it:
        if arg == "--file":
            file = Path(next(it))
        elif arg == "--workspace":
            workspace = Path(next(it))
        else:
            print(f"research_access: unknown arg {arg!r}", file=sys.stderr)
            return 2
    try:
        res = load(file, workspace)
    except ResearchAccessError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(json.dumps({
        "mounts": res.mounts,
        "containerEnv": res.container_env,
        "features": res.features,
        "allow_domains": res.allow_domains,
        "summary": res.summary,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
