# Build:  docker build --network=host -f docker/Dockerfile -t claude-dev docker/

FROM node:22-slim

# Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# Tools the projects will need
RUN apt-get update && apt-get install -y --no-install-recommends \
      git python3 python3-pip curl ca-certificates ripgrep openssh-client \
    && rm -rf /var/lib/apt/lists/*

# GitHub CLI (so the agent can open PRs via `gh pr create`)
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
       > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Git identity so `git commit` works without prompts.
# This gitconfig is only a fallback. At runtime, dispatch mounts the host's
# real ~/.gitconfig over $HOME/.gitconfig to keep the user's identity and
# credential helper.
COPY gitconfig /root/.gitconfig

# Agent HOME when the container runs as a non-root user
# (see `--user` in src/talos/dispatch.py). It is world-writable so any UID
# inherited from the host can use it as home; the auth files
# (~/.claude, ~/.ssh, ~/.gitconfig) are bind-mounted here at runtime.
RUN mkdir -p /home/agent && chmod 1777 /home/agent

WORKDIR /workspace
