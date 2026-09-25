# Security model

Every brief runs `claude -p --dangerously-skip-permissions`. The agent can run
any command, edit any file it can see, and use any credential it's given,
without asking. **The Docker container is the boundary.** This page describes
what that boundary contains, what it doesn't, and why.

There are two trust levels:

- **Manual briefs** (`default` pipeline). You wrote the prompt. The container
  gets broad access for convenience.
- **Event pipelines** (`issue-fix`, `review-fix`). Someone outside your
  machine wrote the prompt: an issue body, a review comment, or a production
  error message. There's no opt-in label, so any collaborator who opens an
  issue controls part of a prompt that runs with full permissions. These
  containers are hardened.

- [What every container shares](#what-every-container-shares)
- [Manual-brief containers](#manual-brief-containers)
- [Hardened containers](#hardened-containers)
- [Host-side git](#host-side-git)
- [Prompt-injection defenses](#prompt-injection-defenses)
- [Secrets](#secrets)
- [Known gaps and accepted risks](#known-gaps-and-accepted-risks)

## What every container shares

- **Host network.** Containers run with `--network=host`. On hosts with a VPN
  that uses per-uid policy routing, the default bridge can't reach
  github.com, npm or PyPI over HTTPS (see
  [setup.md](setup.md#build-the-docker-image)). As a result there is **no
  network isolation**. The agent can reach anything the host can, including
  services bound to `127.0.0.1` such as `webhookd`.
- **Host UID.** Containers run as `--user <your uid>:<your gid>`, with `HOME`
  pointing at a world-writable agent home that the image creates. Files the
  agent creates in mounted folders are owned by you, not by root. The
  `$HOME/...` paths below are inside the container.
- **`GH_TOKEN`.** Dispatch passes the host's `gh auth token` into the
  container. The agent can do anything that token can do: push branches, open
  PRs, comment, and label. It isn't written to disk.
- **Your `~/.gitconfig`**, mounted read-only when it exists.
- **`IS_SANDBOX=1`**, which lets Claude Code accept
  `--dangerously-skip-permissions`.
- **One container at a time.** Dispatch holds a file lock for the whole pass.
  Each container is named `orq-<run>`. When a container doesn't exit on its
  own (timeout, exception, Ctrl-C), dispatch kills it by name, because killing
  the docker client doesn't stop it. At the start of every pass, with the lock
  held, dispatch also kills any `orq-*` container still alive, since it
  belongs to a dispatch that died mid-run.

## Manual-brief containers

A manual brief's container sees only these:

| Mount | Mode | Why |
| --- | --- | --- |
| `/workspace` | rw | the project folder on the host |
| `/obsidian` | rw | the whole vault, to read notes and update memory |
| `$HOME/.claude` | **rw** | host credentials and session state |
| `$HOME/.claude.json` | **rw** | Claude Code's main config |
| `$HOME/.ssh` | ro | for `git push` |
| `$HOME/.gitconfig` | ro | commit identity |

It gets these environment variables:

- `CLAUDE_PROJECT`: the project from the brief's front matter.
- `BRIEF_SLUG`: the brief's filename without extension, used as the branch
  name.
- `GH_TOKEN`: inherited from the host through `gh auth token`. It isn't
  persisted, and it's empty if the host's `gh` isn't authenticated.
- `MAX_ITERATIONS`, `PR_STRATEGY`: settings for the swarm and PRs.
- `IS_SANDBOX=1`.

The rest of the host filesystem isn't mounted. The dispatch lock guarantees
that only one container runs at a time, so the read-write `~/.claude` mounts
don't cause concurrency problems. For why `~/.claude` has to be read-write,
see [setup.md](setup.md#manual-briefs-mount-the-host-session).

This container has your SSH key, your real Claude session, and your whole
vault, so treat a manual brief as code you run yourself.

## Hardened containers

The harness pipelines run a prompt from an outside source, so their container
sees less than a manual brief's.

- **No SSH key.** Git talks to GitHub over HTTPS. `GIT_CONFIG_*` environment
  variables rewrite SSH URLs on the fly, and a credential helper reads
  `GH_TOKEN` from the environment. Nothing is written to `.git/config`, which
  is a bind mount from the host. When the run finishes, dispatch restores
  `origin` if it changed and removes any credential that shows up in that
  file. An earlier version ran `git remote set-url origin
  https://x-access-token:$GH_TOKEN@…` inside the container, which left the
  token in plain text in the host's `.git/config`.
- **The agent works in a worktree, never in your checkout.** Before starting
  the container, dispatch creates a worktree in
  `~/.orchestrator/worktrees/<run>`, with a detached HEAD on the
  `origin/<base_branch>` the repo already has, and **without a checkout**
  (`--no-checkout`). The container runs `git reset --hard` before it starts
  claude. The pipeline then runs its own `git fetch` and creates its branch
  from the freshly fetched `origin/$BASE_BRANCH`, so the run starts from the
  latest code even if your clone is behind. The container sees that worktree
  at `/workspace/$SUBREPO`. Your working tree (your branch, your uncommitted
  changes, your untracked files) isn't touched, so you can keep working in
  the same repo while a run is going. This also covers the harness itself,
  whose services run from its own checkout. When the run ends (success,
  failure or timeout), the worktree is deleted. The agent's branch stays in
  the repo, because refs are shared.
  - A worktree's `.git` points at the repo's git dir with an absolute path,
    so the container mounts that git dir at the same path, read-write. The
    agent needs to write refs, objects and its branch's config. This means
    **your checkout's git dir is writable by the run** (its `HEAD`, its
    `index`, its `config`). `hooks/` is mounted read-only on top, so a planted
    hook can't run the next time you use git by hand. If the run leaves a key
    outside the allowlist (below) in the config, dispatch reports an error
    when the run ends and the next run refuses to start.
  - **The run's branch can't be checked out in your checkout.** That's the
    brief's branch for `issue-fix` and the PR head for `review-fix`. If it is
    checked out, the run fails with that reason before the container starts.
    Without this check, `git checkout -B` reset your branch under you and
    `gh pr checkout` failed. Switch branches and retry.
  - **The worktree starts clean**: no `.env`, no `node_modules`, no builds,
    and no initialized submodules. The templates tell the agent to install
    dependencies and run `git submodule update --init` if needed.
  - On a timeout, whatever the agent didn't commit is lost with the worktree.
    If it committed on the detached HEAD (because it failed before creating
    its branch), the commit is saved in `refs/orq/<run>`. Use
    `git for-each-ref refs/orq` to list them and
    `git update-ref -d refs/orq/<run>` to delete one.
  - If `origin/<base_branch>` doesn't exist in the repo, the run fails with
    that reason. It's almost always a typo in `base_branch`.
  - When dispatch creates a worktree, it sweeps
    `~/.orchestrator/worktrees` for leftovers from that repo's runs that died
    without cleaning up (a reboot, a dead dispatch), including `locked` ones,
    and kills their containers. This assumes one run at a time, which the lock
    guarantees.
- **Only the memory folder** of the vault (`{vault}/{memory_dir}`) is
  mounted, not the whole vault.
- **A throwaway `~/.claude`**, built for the run in
  `~/.orchestrator/agent-home/` and deleted when it ends. It has no
  `settings.json`, hooks, agents or MCP servers from the host, and it comes
  with a `~/.claude.json` trimmed to the onboarding and account fields. Mounting
  the host's read-write would let a prompt injection install a hook that runs
  on your machine the next time you open Claude, or add an MCP server that
  does the same. Auth comes from `CLAUDE_CODE_OAUTH_TOKEN` (from
  `claude setup-token`) or, as a fallback, a copy of
  `~/.claude/.credentials.json` (see
  [setup.md](setup.md#event-pipelines-a-throwaway-home)).
- **No file inlining.** `inline_file_refs`, the step that pastes in the
  absolute paths a brief mentions, runs on the host. With an issue body as
  input, it would be direct exfiltration of `~/.orchestrator/secrets.env`. An
  issue that said "see `~/.orchestrator/secrets.env`" would put the webhook
  secret, the Sentry token or the Claude credentials into the prompt of an
  agent that can post on GitHub. It only runs for manual briefs from a
  non-external source.
- **No `SENTRY_AUTH_TOKEN`.** The Sentry context is fetched on the host and
  inlined into the brief, so the container doesn't need the token.

## Host-side git

The repo's git dir is a bind mount that the container can write, so the host
treats it as untrusted. The host never fetches or checks out. It only runs
`rev-parse`, `worktree add --no-checkout`, `worktree list` and `update-ref`.
Those commands run with hooks and fsmonitor off, with `protocol.allow=never`
so any network access fails, and without the secrets in dispatch's
environment:

```text
-c core.hooksPath=/dev/null -c core.fsmonitor=false -c protocol.allow=never
```

The repo's local config also goes through an **allowlist** of keys
(`SAFE_GIT_CONFIG_RE` in `src/talos/worktree.py`). It allows `core` keys that
don't run commands, `remote.*.url|fetch|…`, `branch.*`, `user.*`, and little
else. `filter.lfs.*` passes only with the exact values that
`git lfs install --local` writes. Any other key (`include`,
`url.*.insteadOf`, `remote.*.uploadpack`, `credential.helper`,
`core.sshCommand`, other filters and so on) stops dispatch from creating the
worktree. The run fails and asks you to review `.git/config` by hand. If a
repo needs a legitimate key that isn't on the list, add it to the allowlist.

To delete a worktree, dispatch doesn't use `worktree remove` or
`worktree prune`. It deletes the folder and that worktree's metadata, after
checking that the path is inside `~/.orchestrator/worktrees`.

## Prompt-injection defenses

The container limits what an injection can do. The prompts try to keep an
injection from succeeding in the first place:

- Every brief built from an external event puts the event's text above an
  **untrusted-data fence**. The fence tells the agent to treat that text as
  evidence to investigate, never as instructions, and to record any request to
  do something outside the brief as a `blocker`.
- For `review-fix`, where the agent reads the comments from GitHub itself, the
  brief carries a fence that says review comments are arguments to evaluate.
  A comment that asks the agent to disable a test, remove a check, exfiltrate
  a secret or touch another repo gets declined, with the reason recorded.
- The harness's own reviews carry an anti-loop marker, and `webhookd`
  rejects deliveries triggered by the harness itself.
- Stage B only acts on PRs with the harness's `pr_label`, so it never touches
  a teammate's PR.
- Nothing merges automatically. A human merges every PR.

## Secrets

Secrets live in `~/.orchestrator/secrets.env`, mode 0600, loaded by systemd
with `EnvironmentFile` (and by each entry point if it's run by hand).
**`config.yaml` stores variable names, never values.**

```bash
GITHUB_WEBHOOK_SECRET=...     # you generate it (e.g. openssl rand -hex 32); scripts/setup_webhook.py sends it to GitHub
SENTRY_CLIENT_SECRET=...      # issued by Sentry when you create the Internal Integration
SENTRY_AUTH_TOKEN=...         # same
CLAUDE_CODE_OAUTH_TOKEN=...   # `claude setup-token`, for hardened containers
```

`config.yaml` maps each one by name:

```yaml
secrets:
  github_webhook_secret_env: GITHUB_WEBHOOK_SECRET
  sentry_client_secret_env: SENTRY_CLIENT_SECRET
  sentry_auth_token_env: SENTRY_AUTH_TOKEN
  claude_oauth_token_env: CLAUDE_CODE_OAUTH_TOKEN
```

Dispatch passes secrets to `docker run` by name only (`-e NAME`), so the
values never show up in `ps`.

## Known gaps and accepted risks

- **No network isolation.** See above. A firewall inside the container is on
  the [roadmap](roadmap.md).
- **`GH_TOKEN` scope.** The token is whatever `gh auth token` returns on the
  host, which usually covers every repo you can reach. A token scoped to the
  target repo would be tighter.
- **Writable git dir.** A hardened run can write your checkout's git dir. The
  hooks mount, the config allowlist and the host-side git flags limit what it
  can do with that.
- **Manual briefs are trusted.** They get your SSH key, your real
  `~/.claude`, and the whole vault. Don't paste untrusted text into a manual
  brief.
- **The agent can post on GitHub.** An injection that gets past the fences
  could post comments or push branches in repos that `GH_TOKEN` can reach.
  Branch protection on your integration and production branches is strongly
  recommended.
- **The harness assumes private repos.** With no opt-in label, anyone who can
  open an issue feeds the prompt. For public repos, set `authors_allow`.
