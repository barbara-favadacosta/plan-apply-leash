# Apply env image — deliberately narrower than research.
# NO aws-cli, NO kubectl: by design the apply agent only has GitHub access
# (via the GH_TOKEN env var set in devcontainer.json). Any AWS/Kubernetes
# work in a plan is informational only — it must be executed elsewhere
# (CI/CD, manual deploy step). Removing the binaries makes that constraint
# enforceable at the image layer, not just by config.

# Microsoft's TypeScript-on-Node devcontainer base. Gives us Node 22 + npm
# (needed by the reference coding agent) on Debian Bookworm.
FROM mcr.microsoft.com/devcontainers/typescript-node:1-22-bookworm

# Non-root user the container will run as. UID 1000 is the conventional first
# non-root uid on Linux; it does NOT match the macOS host user (uid 501), so
# bind-mounted repos appear owned by a foreign uid inside the container. git's
# dubious-ownership guard is handled separately via safe.directory below.
ARG USERNAME=dev
ARG UID=1000

# The base image ships with a 'node' user at UID 1000; remove it so our own
# 'dev' user can take that UID without collision.
RUN if id -u node >/dev/null 2>&1; then userdel -r node 2>/dev/null || true; fi \
 && groupadd -g ${UID} ${USERNAME} \
 && useradd -m -u ${UID} -g ${UID} -s /bin/bash ${USERNAME}

# OS packages we need:
#   git, curl, jq, less, ca-certificates : everyday CLI plumbing
#   python3 + pip + venv                 : runs our hooks (validate-plan, pre-tool, stop)
#   iptables, ipset, dnsutils, unzip     : used by init-firewall.sh
#   squid                                : domain-allowlisting egress proxy. Apply
#                                          reaches GitHub through it; squid filters
#                                          by hostname at connect time, so it is
#                                          immune to GitHub's CDN endpoints'
#                                          rotating IPs (which an IP-pinned firewall
#                                          drops mid-session once they roll).
#   gh                                   : GitHub CLI, talks to api.github.com
RUN apt-get update && apt-get install -y --no-install-recommends \
      git curl jq python3 python3-pip python3-venv \
      iptables ipset dnsutils ca-certificates unzip less \
      squid \
      gh \
 && rm -rf /var/lib/apt/lists/* \
 && rm -f /etc/squid/squid.conf   # init-firewall.sh regenerates it from the allowlist

# The reference coding agent (swap this line for your agent's install command
# to run a different agent in this harness).
RUN npm install -g @anthropic-ai/claude-code

# Python libs used by hooks/*.
RUN python3 -m pip install --break-system-packages pyyaml jsonschema

# Bake the firewall script into a fixed system path. The apply firewall is
# tighter than research's — no AWS endpoints, no package registries.
COPY init-firewall.sh /usr/local/bin/init-firewall.sh
RUN chmod +x /usr/local/bin/init-firewall.sh

# Allow the 'dev' user to run ONLY the firewall script as root.
RUN echo "${USERNAME} ALL=(root) NOPASSWD: /usr/local/bin/init-firewall.sh" > /etc/sudoers.d/init-firewall \
 && chmod 0440 /etc/sudoers.d/init-firewall

# Bind-mounted repos come from the host (uid 501 on macOS) and so are NOT owned by the
# container's dev user (uid 1000). git's dubious-ownership guard would refuse every repo.
# Trust them at the image layer (writes /etc/gitconfig; must be root, before USER switch).
RUN git config --system --add safe.directory '*'

USER ${USERNAME}
WORKDIR /workspace

# Pre-create the agent-auth-apply named-volume mount point in the image, owned
# by dev. Docker copies the image layer's contents — including ownership — into
# a freshly-created empty named volume on first mount. Without this, the volume
# root ends up owned by root and dev can't write to it, so post-create.sh fails
# copying agent-settings.json into ~/.claude/.
RUN mkdir -p /home/${USERNAME}/.claude
