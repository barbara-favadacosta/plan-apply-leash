# Microsoft's TypeScript-on-Node devcontainer base. Gives us Node 22 + npm
# (needed by the reference coding agent) on Debian Bookworm — a familiar,
# well-supported Linux for everything else we install below.
FROM mcr.microsoft.com/devcontainers/typescript-node:1-22-bookworm

# Non-root user the container will run as. UID 1000 is the conventional first
# non-root uid on Linux; it does NOT match the macOS host user (uid 501), so
# bind-mounted repos appear owned by a foreign uid inside the container. git's
# dubious-ownership guard is handled separately via safe.directory below.
ARG USERNAME=dev
ARG UID=1000

# The base image ships with a 'node' user at UID 1000; remove it so our own
# 'dev' user can take that UID without collision, then create dev as a normal
# login user with a home directory.
RUN if id -u node >/dev/null 2>&1; then userdel -r node 2>/dev/null || true; fi \
 && groupadd -g ${UID} ${USERNAME} \
 && useradd -m -u ${UID} -g ${UID} -s /bin/bash ${USERNAME}

# OS packages we need:
#   git, curl, jq, less, ca-certificates : everyday CLI plumbing
#   python3 + pip + venv                 : runs our hooks (validate-plan etc.)
#   iptables, ipset, dnsutils, unzip     : used by init-firewall.sh
#   squid                                : domain-allowlisting egress proxy.
#                                          The agent reaches platform endpoints
#                                          (AWS, EKS, …) through it; squid filters
#                                          by hostname at connect time, so it is
#                                          immune to those endpoints' rotating IPs
#                                          (which an IP-pinned firewall cannot allow).
#   gh                                   : GitHub CLI, talks to api.github.com
RUN apt-get update && apt-get install -y --no-install-recommends \
      git curl jq python3 python3-pip python3-venv \
      iptables ipset dnsutils ca-certificates unzip less \
      squid \
      gh \
 && rm -rf /var/lib/apt/lists/* \
 && rm -f /etc/squid/squid.conf   # init-firewall.sh regenerates it from the allowlist

# Platform CLIs (aws-cli, kubectl, azure-cli, …) are NOT baked in. They are
# added only when a platform declares one in research-access.yaml, via the
# devcontainer Feature named in research_access.py's CATALOG — so the image
# carries exactly the CLIs you asked for and nothing else. Tier-A platforms
# (token + HTTP, no CLI) add nothing here at all.

# The reference coding agent, installed globally via npm — this is what runs
# inside the container. To use a different agent, swap this line for that
# agent's install command; the rest of the harness is agent-agnostic.
RUN npm install -g @anthropic-ai/claude-code

# Python libs used by hooks/validate-plan.py.
# --break-system-packages is required because Debian 12 marks the system python
# as PEP 668 'externally managed'; we accept that since this image is single-purpose.
RUN python3 -m pip install --break-system-packages pyyaml jsonschema

# Bake the firewall script into a fixed system path. post-create.sh calls it via sudo
# to install the egress allowlist before the agent is allowed to make outbound
# requests.
COPY init-firewall.sh /usr/local/bin/init-firewall.sh
RUN chmod +x /usr/local/bin/init-firewall.sh

# Bake the lifecycle script + agent settings + slash commands into the image.
# /workspace is bound to the repo's app/ subfolder (so research-state stays out
# of the bind), which means the repo's .devcontainer/ is NOT visible inside the
# container — the old `bash /workspace/.devcontainer/research/...` lifecycle
# command can no longer find these files. COPY them to fixed system paths
# instead (the build context is .devcontainer/research/), like init-firewall.sh.
COPY post-create.sh /usr/local/bin/research-post-create.sh
COPY agent-settings.json /usr/local/share/research-agent-settings.json
COPY commands /usr/local/share/research-commands
RUN chmod +x /usr/local/bin/research-post-create.sh

# Allow the 'dev' user to run ONLY the firewall script as root, without a password.
# This is the only sudo capability granted to dev — needed because iptables/ipset
# require root, but we don't want dev to have general root.
RUN echo "${USERNAME} ALL=(root) NOPASSWD: /usr/local/bin/init-firewall.sh" > /etc/sudoers.d/init-firewall \
 && chmod 0440 /etc/sudoers.d/init-firewall

# Bind-mounted repos come from the host (uid 501 on macOS) and so are NOT owned by the
# container's dev user (uid 1000). git's dubious-ownership guard would refuse every repo.
# Trust them at the image layer (writes /etc/gitconfig; must be root, before USER switch).
RUN git config --system --add safe.directory '*'

# From here on, anything that runs in the container runs as the unprivileged dev user.
# /workspace is where devcontainer.json bind-mounts the project.
USER ${USERNAME}
WORKDIR /workspace

# Pre-create the named-volume mount points (agent-auth-research, gh-auth-research)
# in the image, owned by dev. Docker copies the image layer's contents — including
# ownership — into a freshly-created empty named volume on first mount. Without
# this, the volume root ends up owned by root and dev can't write to it, so
# post-create.sh fails copying agent-settings.json into ~/.claude/.
RUN mkdir -p /home/${USERNAME}/.claude /home/${USERNAME}/.config/gh
