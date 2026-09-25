# Setup

This guide covers installing the harness on one Linux host, building the
container image, authenticating Claude Code and git, writing the config, and
running the processes as systemd user services.

- [Prerequisites](#prerequisites)
- [Install](#install)
- [Build the Docker image](#build-the-docker-image)
- [Configuration](#configuration)
- [Claude Code auth inside the container](#claude-code-auth-inside-the-container)
- [Git identity](#git-identity)
- [Running it](#running-it)
- [Run as systemd user services](#run-as-systemd-user-services)
- [Tests](#tests)

## Prerequisites

- Linux with systemd user units. The deploy files assume it.
- Docker, usable by your user without `sudo`.
- Python 3.11+.
- Claude Code authenticated on the host. `~/.claude/` and `~/.claude.json`
  must exist. If they don't, run `claude` interactively once on your machine.
  `talos-dispatch` refuses to start without them.
- `~/.ssh/` with your key, so the agent can `git push` from manual-brief
  containers. This one is a warning, not an error.
- `gh` authenticated on the host (`gh auth login`). Dispatch reads
  `gh auth token` and passes it into the container as `GH_TOKEN`. Without it
  the agent can still commit, and push over SSH on manual briefs, but it can't
  open PRs.
- Node.js with `npx`, for the event harness. Dispatch runs the BMAD installer
  in workspaces that don't have BMAD yet.

## Install

```bash
git clone https://github.com/Rico-Vari/talos-bug-automata ~/talos-bug-automata
cd ~/talos-bug-automata
python3 -m venv venv
source venv/bin/activate
pip install -e .
```

This installs four entry points into the venv: `talos-dispatch`, `talos-bot`,
`talos-webhookd` and `talos-reconcile`.

The systemd units in `deploy/systemd/` assume the checkout is at
`~/talos-bug-automata` and the venv is at `~/talos-bug-automata/venv`. If yours
is somewhere else, edit the paths in the units.

## Build the Docker image

```bash
docker build --network=host -f docker/Dockerfile -t claude-dev docker/
```

The image (`claude-dev`) is `node:22-slim` plus the Claude Code CLI, git,
Python, ripgrep, the OpenSSH client and `gh`. The build takes about 30 s and
you only need it once, plus again after you change `docker/Dockerfile` or
`docker/gitconfig`. The image name is configurable (`docker_image`).

> **About `--network=host`.** Some VPNs (Surfshark, Mullvad and others) use
> per-uid policy routing. With them, Docker's default bridge gets trapped in
> the tunnel and `apt-get update` times out during the build.
> `--network=host` makes the build use the host's network namespace directly.
> Without a VPN you can omit it, and it does no harm to keep it.
>
> For the same reason, dispatch runs `docker run --network=host` at runtime.
> Without it, containers can't reach github.com, npm or PyPI over HTTPS, and
> `gh pr create`, `npm install` and similar commands fail with SSL handshake
> errors. The trade-off is that the container shares the host's network
> namespace, so there is no network isolation. See
> [security-model.md](security-model.md).

## Configuration

The config file is `config.yaml` at the repo root. Set the `TALOS_CONFIG`
environment variable to use a different path.

```bash
cp config.example.yaml config.yaml
```

If `config.yaml` is missing, the first `talos-dispatch` run writes a
minimal default and exits so you can edit it. Copying the example is better
because it documents every key.

The main keys:

| Key | Meaning |
| --- | --- |
| `vault` | Path to the Obsidian vault (any folder of Markdown files works) |
| `briefs_dir` | Brief queue folder inside the vault (default `ToDos`) |
| `memory_dir` | Agent memory folder inside the vault |
| `poll_interval_seconds` | Poll interval for `talos-dispatch --watch` |
| `telegram_bot_token`, `telegram_chat_id` | See [telegram.md](telegram.md) |
| `docker_image` | Image to run (default `claude-dev`) |
| `claude_timeout_seconds` | Timeout for a manual brief |
| `max_iterations` | Fix-and-revalidate rounds the swarm architect may run before it aborts a manual brief |
| `projects` | Allowlist of local project paths, `name: path` |
| `repos`, `sentry`, `webhook`, `secrets`, `timeouts`, `global_daily_cap`, `reconcile_interval_minutes` | Event harness. See [event-harness.md](event-harness.md) |

`config.yaml` holds names of environment variables, never secret values.
Secrets go in `~/.orchestrator/secrets.env`. See
[security-model.md](security-model.md#secrets).

### Add a project

```yaml
projects:
  my-app: ~/code/my-app
  acme: ~/code/acme/Acme Main      # an umbrella folder with several repos inside
```

The name must match the `project:` field in a brief's front matter, and the
path must exist on the host.

## Claude Code auth inside the container

Claude Code can't run `claude login` inside the container because there is no
browser for OAuth. How the container authenticates depends on the pipeline.

### Manual briefs: mount the host session

For manual briefs, the container mounts the host's `~/.claude` and
`~/.claude.json` **read-write** and inherits your session. Before the first
run, check that they exist:

```bash
ls ~/.claude/ ~/.claude.json
```

If they don't exist, run `claude` interactively once and finish the OAuth flow
in the browser.

**Why read-write instead of read-only?** Claude Code writes session state to
`~/.claude/session-env/` every time a run starts. With a `:ro` mount, the Bash
tool fails inside the container with
`ENOENT: no such file or directory, mkdir '.../.claude/session-env/...'`, and
every brief that needs shell commands breaks. Dispatch holds a lock for each
pass and runs one container at a time, so two containers never write to the
host's `~/.claude` at once.

### Event pipelines: a throwaway home

The `issue-fix` and `review-fix` pipelines run a prompt that someone outside
your machine wrote, so they **don't** mount your `~/.claude`. Each run gets a
throwaway one (see
[security-model.md](security-model.md#hardened-containers)). These are the
auth options, in order of preference:

```bash
claude setup-token          # one-year token; put it in ~/.orchestrator/secrets.env:
# CLAUDE_CODE_OAUTH_TOKEN=...
```

Without that token, the container gets a copy of
`~/.claude/.credentials.json`. If the run refreshes the token, the new copy
is written back to the host, but only if the host didn't refresh it too in
the meantime.

### Smoke test

```bash
docker run --rm \
  -e IS_SANDBOX=1 \
  -v ~/.claude:/root/.claude \
  -v ~/.claude.json:/root/.claude.json \
  claude-dev claude --version

docker run --rm \
  -e IS_SANDBOX=1 \
  -v ~/.claude:/root/.claude \
  -v ~/.claude.json:/root/.claude.json \
  claude-dev claude -p "reply only: authenticated" --dangerously-skip-permissions
```

`IS_SANDBOX=1` is required because Claude Code refuses to run
`--dangerously-skip-permissions` as root. The variable tells it that the
process is already isolated in a sandbox (the container), which unlocks the
flag. The smoke test runs as root. Real runs use your host UID, with `HOME`
set to an agent home directory that the image creates, so files the agent
creates are owned by you and not by root.

## Git identity

The agent's commits use the identity from your host `~/.gitconfig`. Dispatch
mounts that file read-only into the container when it exists. The image also
ships `docker/gitconfig` as a fallback identity. If you want the agent's
commits under a different name or email, edit `docker/gitconfig` and rebuild
the image:

```bash
docker build --network=host -f docker/Dockerfile -t claude-dev docker/
```

## Running it

```bash
talos-dispatch              # single pass (same as --once)
talos-dispatch --once       # single pass; what the systemd timer runs
talos-dispatch --watch      # loop: a pass every poll_interval_seconds
talos-dispatch --dry-run    # preview without Docker or file changes
talos-bot                   # Telegram bot
talos-webhookd              # webhook ingress on 127.0.0.1:8787
talos-reconcile --dry-run   # what the reconciler would admit
```

`deploy/run.sh` runs the bot and `talos-dispatch --watch` side by side in the
foreground until you press Ctrl+C. It's handy for trying things out. For
normal use, run the systemd units.

## Run as systemd user services

| Unit | What it does |
| --- | --- |
| `talos-bot.service` | The Telegram bot |
| `talos-webhookd.service` | Webhook ingress on `127.0.0.1:8787` |
| `talos-dispatch.timer` | Starts a dispatch pass every 2 min |
| `talos-dispatch.service` | The pass itself (`--once`). Starting it by hand runs a pass right away |

The timer is a safety net. `webhookd` starts `talos-dispatch.service` as soon
as it admits an event, so normal latency is seconds. The design uses a timer
instead of `--watch` because the dispatch flock already makes concurrent
passes safe, and a crash can't wedge a timer.

`talos-webhookd.service` and `talos-dispatch.service` load
`~/.orchestrator/secrets.env` with `EnvironmentFile`. Every entry point also
reads that file itself, so runs by hand work too. It never overrides variables
that are already set in the environment.

### Install the units

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/*.service deploy/systemd/*.timer ~/.config/systemd/user/
# edit WorkingDirectory/ExecStart if your checkout is not ~/talos-bug-automata
systemctl --user daemon-reload
systemctl --user enable --now talos-bot.service talos-webhookd.service talos-dispatch.timer
```

If you only use manual briefs, `talos-bot` and the timer are enough.

### Check that it is running

```bash
systemctl --user status talos-bot
```

You should see `Active: active (running)` and a recent log line saying that
the bot started and is waiting for commands, with the allowed `chat_id`.

### Logs

```bash
journalctl --user -u talos-bot -f           # live (Ctrl+C to exit)
journalctl --user -u talos-bot -n 50        # last 50 lines
journalctl --user -u talos-bot --since "1 hour ago"
journalctl --user -u talos-webhookd -n 30
```

Dispatch output also goes to `~/.orchestrator/logs/dispatch.log`, and each
agent run gets a full log in `~/.orchestrator/logs/runs/`. The journal only
holds what each service prints itself: commands received, and errors from
python-telegram-bot or the HTTP server.

### Stop, start, restart

```bash
systemctl --user stop talos-bot
systemctl --user start talos-bot
systemctl --user restart talos-bot
```

Python doesn't hot-reload. After you edit `config.yaml` or the code, restart
the long-running services:

```bash
systemctl --user restart talos-webhookd talos-bot
```

Dispatch doesn't need a restart because the timer starts a new process for
every pass. The reconciler runs inside the dispatch pass, and both re-read
the config each time.

### Services run from the checkout

The units point at the checkout, so whatever you have checked out is what
runs, uncommitted changes included. After you pull an update:

```bash
git checkout main && git pull --ff-only
pip install -e .            # if dependencies or entry points changed
systemctl --user restart talos-webhookd talos-bot
```

If the local checkout falls behind a change that adds files, a service can
crash-loop without warning. This has happened in production: 1,830 restarts
before anyone ran `git pull`. `systemctl --user status` shows it.

### Enable or disable autostart

```bash
systemctl --user disable talos-bot          # don't start at next login
systemctl --user enable talos-bot           # enable again
systemctl --user enable --now talos-bot     # enable and start now
```

### Debugging a service that won't start or crashes

1. Run `systemctl --user status talos-bot` and look at the last lines. If
   they say `Active: failed` or `Active: activating (auto-restart)`, the
   service is in a crash loop.
2. Run `journalctl --user -u talos-bot -n 100 --no-pager` and find the Python
   traceback. These are the usual causes:
   - `telegram_chat_id` not configured: fill in the field in `config.yaml`.
   - `ModuleNotFoundError`: the venv is missing dependencies. Run
     `./venv/bin/pip install -e .`.
   - `telegram.error.InvalidToken`: the token in `config.yaml` is wrong.
3. Run the process by hand to see the error without systemd in the way:

   ```bash
   cd ~/talos-bug-automata
   ./venv/bin/talos-bot
   ```

   Any exception you see here is the same one systemd is catching.

### Editing a unit

After you change a unit file, reload before you restart:

```bash
systemctl --user daemon-reload
systemctl --user restart talos-bot
```

### Uninstall

```bash
systemctl --user disable --now talos-bot talos-webhookd talos-dispatch.timer
rm ~/.config/systemd/user/talos-{bot,webhookd,dispatch}.service ~/.config/systemd/user/talos-dispatch.timer
systemctl --user daemon-reload
```

### A note on `linger`

By default, user services start when you log in and stop when you log out. To
keep them running after you log out, or to start them at boot without a login,
run this once:

```bash
sudo loginctl enable-linger "$USER"
```

To undo it, run `sudo loginctl disable-linger "$USER"`.

## Tests

```bash
python tests/test_offline.py
```

The tests are offline: no network, no Docker, no GitHub. They cover the paths
that have actually broken, which are the ones that normal runs exercise least
because they depend on something going wrong: a run that dies halfway, a
merge, or a gate that lifts later. Each test drives the function that runs in
production, not a smaller stand-in. `tests/fixtures/` has recorded webhook
payloads.
