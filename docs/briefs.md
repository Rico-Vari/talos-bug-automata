# Manual briefs

A brief is a Markdown file with YAML front matter. It is the unit of work for
the whole harness. The event harness writes briefs too, but this page covers
the ones you write by hand. The dispatcher picks up briefs from
`{vault}/{briefs_dir}/` (by default `ToDos/`), runs them one at a time in the
`claude-dev` container, and moves the ones that succeed to
`{vault}/projects/{project}/completed/`.

- [Writing a brief](#writing-a-brief)
- [Front matter](#front-matter)
- [Automatic PRs (mono-repo and multi-repo)](#automatic-prs-mono-repo-and-multi-repo)
- [The flow of a brief](#the-flow-of-a-brief)
- [The multi-agent swarm](#the-multi-agent-swarm)
- [Running your first real brief](#running-your-first-real-brief)
- [Logs](#logs)
- [Agent memory](#agent-memory)
- [Running bmalph Ralph loops](#running-bmalph-ralph-loops)

## Writing a brief

Create a `.md` file in `{vault}/ToDos/`, or use `/brief` in
[Telegram](telegram.md#brief-create-a-brief-from-the-chat):

```markdown
---
project: my-app          # must match a key in config.yaml:projects
status: pending
priority: high           # high | medium | low (high runs first)
created: 2026-04-10
---

# Short task title

## Context
Why this matters, what you already know, what references to look at.
If you want the agent to see files from the host, mention the absolute path
(e.g. `~/code/my-app/src/main.py`) and dispatch inlines them into the
prompt automatically if they are under 100 KB.

## What must exist when it's done
- A concrete, verifiable list of deliverables.
- The more specific, the better.

## Constraints
- Things it must NOT do (e.g. don't touch X, don't commit to main).
```

Dispatch scans the briefs folder recursively and picks up every file with
`status: pending`. Briefs run by priority (`high` first) and, within the same
priority, oldest first by `created`. A brief without `created` falls back to
its filename, which normally starts with the date.

**File inlining.** On manual briefs, dispatch looks for absolute paths
(`/…` or `~/…`) with a file extension in the body. For each one that exists on
the host and is under 100 KB, it pastes the file's content into the prompt.
This runs on the host, so it can read anything your user can read. That is why
it never runs for briefs from GitHub or Sentry (see
[security-model.md](security-model.md#hardened-containers)).

## Front matter

| Field | Required | Meaning |
| --- | --- | --- |
| `project` | yes | Key in `config.yaml:projects`. Without it, the brief is skipped |
| `status` | yes | `pending` to run. Dispatch sets `running`, `done`, `failed`, and so on |
| `priority` | no | `high`, `medium` (default) or `low` |
| `created` | no | Date or timestamp, used for ordering |
| `pr-strategy` | no | `draft` makes the agent open draft PRs (`gh pr create --draft`). `pr_strategy` also works |

Dispatch adds more fields as the brief moves through its states:
`completed_at`, `last_error`, `prs`, `summary`. Briefs from the event harness
also carry `pipeline`, `source`, `gh_repo`, `gh_issue`, `subrepo`,
`base_branch` and similar fields. A hand-written brief doesn't need them and
runs in the `default` pipeline.

## Automatic PRs (mono-repo and multi-repo)

Every brief that produces committable changes opens its own PR. The agent
handles two layouts, depending on what the project folder looks like:

**Mono-repo** (the `config.yaml` entry points straight at a git repo). The
agent creates the branch `<slug>` in that repo, commits, pushes and opens the
PR.

**Multi-repo** (the entry points at a parent folder with several git repos
inside, e.g. `acme: ~/code/acme/Acme Main`). The agent decides which sub-repos
to work in based on the brief, and creates **the same branch** `<slug>` in
each of them. That lets you track cross-repo features (front end and back end)
under one name. Each sub-repo gets its own PR.

In both cases:

- `BRIEF_SLUG` is the brief's filename without the extension.
- The agent respects each repo's base branch. It doesn't assume `main`, and
  detects `master`, `dev`, `devel`, `developer` and so on.
- If the brief produces no changes, it ends OK without a PR or a branch.
- When the brief lands in `completed/`, its front matter includes:

  ```yaml
  prs:
    - repo: api
      url: https://github.com/acme-org/api/pull/42
      branch: <slug>
      commits: [abc1234, def5678]
    - repo: web-app
      url: https://github.com/acme-org/web-app/pull/17
      branch: <slug>
      commits: [9876fed]
  summary: one line on what the agent did
  ```

- The Telegram notification, if enabled, includes one line per PR above the
  log tail.

`gh pr create` only works if the host has run `gh auth login`. Dispatch uses
`gh auth token` to pass the token into the container. If `gh` isn't
authenticated, the agent can still commit and push with the SSH key, but it
can't open the PR automatically.

## The flow of a brief

1. The brief sits in `{vault}/ToDos/` with `status: pending`.
2. Dispatch picks it up and builds the prompt: the project memory, then the
   brief body.
3. It runs `claude -p` inside a `claude-dev` container, with the project
   folder mounted as `/workspace`. The system prompt is
   `src/talos/prompts/lead-orchestrator.md`.
4. On success (exit 0), the brief gets `status: done` and `completed_at`, and
   moves to `{vault}/projects/{project}/completed/`.
5. On failure (exit other than 0), it gets `status: failed` and `last_error`,
   and stays in `ToDos/` for you to review.
6. Telegram gets a notification with the tail of the log.

Only one brief runs at a time, because dispatch takes a file lock for the
whole pass.

## The multi-agent swarm

`.claude/agents/` in this repo defines a team of Claude Code subagents:
`architect`, `frontend_dev`, `backend_dev`, `qa_testing`, `code_reviewer` and
`github_ops`. When a workspace has
`/workspace/.claude/agents/01_ARCHITECT.md`, the lead prompt switches to
"team mode". The lead hands the brief to the `architect`, the architect
decomposes it and delegates, and `github_ops` handles branches, commits and
PRs. The lead only updates memory and writes the result file. Without the
agents, the lead does the work itself.

The architect gets up to `max_iterations` fix-and-revalidate rounds (QA bugs
plus reviewer findings; the default is 3). If it runs out, it aborts the
brief. The branch stays local with the devs' commits and is never pushed.
Dispatch prints a prominent `BRIEF ABORTED — iteration cap reached` banner
with the summary and blockers, and Telegram gets a ⚠️ notice.

The agent definitions live in **this repo** as the source of truth. Copy them
into the project's workspace so Claude Code finds them at startup. For
multi-repo projects, copy them into the parent folder, not into each
sub-repo:

```bash
cp -r ~/talos-bug-automata/.claude/agents ~/code/my-app/.claude/
```

Copy them again whenever you update the prompts here. Automating this copy is
on the [roadmap](roadmap.md).

## Running your first real brief

1. **Check that the project is in `config.yaml`.**

   ```bash
   grep -A20 '^projects:' config.yaml
   ```

   The name in the brief's front matter (`project: foo`) has to match a key
   here, and the path has to exist on the host.

2. **Write the brief** in `{vault}/ToDos/<anything>.md`, using the template
   [above](#writing-a-brief).

3. **(Optional) Copy the agents** into the workspace, as described in
   [the multi-agent swarm](#the-multi-agent-swarm).

4. **Start dispatch.** In one terminal:

   ```bash
   cd ~/talos-bug-automata
   ./venv/bin/talos-dispatch
   ```

   In another, watch what the agent does while it runs:

   ```bash
   tail -f ~/.orchestrator/logs/runs/*.log
   ```

5. **Check the result.**
   - **Success** (`exit 0`):
     - The brief moves to `{vault}/projects/{project}/completed/<file>.md`.
     - Its front matter changes to `status: done` with `completed_at`.
     - The project's memory file in `{vault}/{memory_dir}/` has new entries
       written by the agent.
   - **Failure** (`exit != 0`):
     - The brief stays in `ToDos/` with `status: failed` and `last_error`.
     - Check the log in `~/.orchestrator/logs/runs/` to find out why.

6. **(Optional) Turn on Telegram** for notifications. See
   [telegram.md](telegram.md).

## Logs

- `~/.orchestrator/logs/dispatch.log` is the general dispatch log.
- `~/.orchestrator/logs/runs/YYYYMMDD-HHMMSS-{project}.log` has the full output
  of each agent run.
- `~/.orchestrator/logs/plans/` archives the swarm architect's plan
  (`.orchestrator-plan.md`) for each run.

The run log is the `claude -p` JSON stream (`--output-format stream-json`), so
each tool call shows up the moment it happens. To follow a run while it's
going:

```bash
tail -f ~/.orchestrator/logs/runs/*.log
python scripts/runlog.py ~/.orchestrator/logs/runs/<log>         # one line per action, latest stretch
python scripts/runlog.py ~/.orchestrator/logs/runs/<log> --all
```

A raw run log can be tens of MB. `scripts/runlog.py` condenses it to one line
per action.

## Agent memory

Each project has a persistent memory file in `{vault}/{memory_dir}/`, one per
project and named after it. If the file doesn't exist, dispatch creates it
from a template with `Decisions`, `Errors and fixes` and `Useful patterns`
sections. Dispatch puts the file at the top of every prompt, and the prompt
tells the agent to update it after a successful run with the decisions,
errors and patterns worth keeping. Because it lives in the vault, you can
read and edit it in Obsidian like any other note.

Manual briefs mount the whole vault at `/obsidian`. The event pipelines only
mount the memory folder (see
[security-model.md](security-model.md#hardened-containers)).

## Running bmalph Ralph loops

[bmalph](https://github.com/LarsCowe/bmalph) combines BMAD planning with a
Ralph autonomous loop. The harness has no bmalph-specific code, and doesn't
need any. A manual brief is a prompt that Claude Code runs with full
permissions in the project's workspace. If a project already has bmalph set
up, you can write a brief that tells the agent to run its Ralph loop there,
and dispatch runs it like any other brief, with the same container, logs,
memory and PR flow.

Keep these points in mind:

- Size the timeout. `claude_timeout_seconds` applies to manual briefs, and a
  loop that runs past it is killed.
- Follow bmalph's own documentation for setup and commands. This repo doesn't
  wrap them.
