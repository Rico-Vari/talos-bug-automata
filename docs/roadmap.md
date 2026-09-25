# Roadmap

This is a living document, updated as the system evolves.

## Current status

The event harness has been in production since September 2026 on a single
host:

- On a sandbox repo in `live` mode, the first full cycle completed: issue →
  PR → review → two review-fix rounds → merge.
- A production web-app repo is `live` with `daily_cap: 2`.
- A multi-service backend repo (several services reporting to one Sentry
  project) is still off. It goes last, as described in
  [the rollout order](event-harness.md#rolling-out-to-production-in-this-order).
- The deployment runs without a public tunnel. The GitHub half works through
  the reconciler. Sentry is waiting for the ingress and the Internal
  Integration.
- Manual briefs have run against real projects, including the end-to-end
  pass (brief → Docker → done → moved → memory updated).

## In progress

- [ ] **Migrate from `bmad-quick-dev` (BMAD 6.10) to `bmad-build-auto`.** BMAD
  is pinned to 6.10.0 because from 6.11 on, `bmad-quick-dev` is a shim to
  `bmad-build` that HALTs on the headless override. The migration is a
  pipeline change.
- [ ] **Validate the Telegram alert for an iteration-cap abort end to end.**
  When the swarm architect runs out of `max_iterations` and aborts, dispatch
  prints a banner (`alert_iteration_cap`) and sends a ⚠️ notification that
  lists the `summary` and `blockers`. The same pattern could cover other
  "needs a human" events: auth down, expired `gh` token, and so on.
- [ ] Follow-ups deferred from the initial review of the event harness.

## Known limitations (event harness)

- [ ] **A revert in dev isn't detected.** If the change is reverted before the
  release, the merge commit is still in prod's history and the issue closes
  anyway. For now, reopen it by hand.
- [ ] **Renaming the default branch needs a restart.** When `prod_branch` is
  unset, the default branch is cached once per process. Restart `webhookd`
  and dispatch after renaming it.
- [ ] **Lifecycle labels overwrite manual edits.** `gh label create --force`
  resets color and description.
- [ ] **Repos whose default branch is the integration branch.** GitHub closes
  the issue through `Closes #N` on merge to dev. The PR should say `Refs #N`
  in those repos.
- [ ] **Squash or rebase releases** never carry the merge commit to prod, so
  the issue waits in `merged_dev` until the 14-day notice.

## Backlog: core system

- [x] ~~Add `dispatch --watch` to systemd~~. Replaced by
  `talos-dispatch.timer` plus the kick from `webhookd`.
- [ ] Automatically copy `.claude/agents/` from this repo into project
  workspaces before each run. Today it's a manual `cp -r`.
- [ ] Auto-sync Claude credentials for manual-brief containers, triggered on
  changes to `~/.claude/.credentials.json`. There are three possible
  triggers, and the cleanest one still has to be chosen:
  a) a cron job every N minutes,
  b) `inotifywait`, which fires only when the file changes,
  c) a shell hook in `.bashrc`/`.zshrc` when a terminal opens, plus a VS Code
     task (`tasks.json`) when the workspace opens.

  Hardened pipelines already avoid this problem with `claude setup-token`.
- [ ] MCP dev tools for the QA agent.
- [ ] QA hooks in each project's `.claude/settings.json`, so tests must pass
  before Stop.
- [ ] `/brief` in Telegram: add a "reference files" field.
- [ ] Rate limiting in the bot, to prevent accidental `/run` spam.
- [ ] Resolve Obsidian wikilinks in dispatch.

## Backlog: memory

- [ ] Evaluate [MemPalace](https://github.com/milla-jovovich/mempalace) once a
  project's memory grows past about 50 KB.
- [ ] Use the MemPalace MCP server instead of injecting Markdown files by hand
  (this would replace `build_prompt`).
- [ ] Mine Claude Code conversation exports to seed the initial memory, using
  MemPalace's `convos` mode.

## Backlog: security and stability

- [ ] Firewall rules inside the container, limiting network access to what's
  needed (GitHub, npm, pip).
- [ ] Scoped credentials: inject only the relevant project's SSH key, not all
  keys, and a `GH_TOKEN` scoped to the target repo.
- [ ] An audit log of the agent's actions (files touched, commands run),
  separate from the dispatch log.
- [ ] A per-session budget limit (`--max-budget-usd`) once Claude Code
  supports it for subscription plans.
- [ ] **Classify Claude Code failures (hybrid option).** Today every failed
  run lands in `failed`, with no distinction between a transient rate limit,
  exhausted monthly quota, auth down, a full context window, OOM, a network
  blip, an agent error or a timeout. The plan is a post-run classifier in
  dispatch that parses the last N lines of the run log and returns an enum:
  `ok | rate_limited | quota_exceeded | auth_failed | context_full | network_error | timeout | agent_error | unknown`.
  A hybrid policy would then apply:

  | Class | Policy |
  | --- | --- |
  | `rate_limited` | auto retry (max 3, exponential backoff 120 s / 240 s / 480 s) |
  | `network_error` | auto retry (max 2, backoff 60 s) |
  | `timeout` | fail + notify a human (there may be partial progress) |
  | `quota_exceeded` | fail + prominent Telegram notice + block future runs |
  | `auth_failed` | fail + prominent notice + block future runs |
  | `context_full` | fail + notify a human (the brief needs to be smaller) |
  | `agent_error` | fail + notify a human (no retry, it may be a prompt bug) |
  | `unknown` | fail + notify a human (check the log) |

  For retries, the brief would stay `pending` with a new front matter field,
  `next_retry_at: <timestamp>`, and a `retry_count` counter.
  `find_pending_briefs` would skip briefs whose `next_retry_at` is in the
  future. The error patterns have to be calibrated against the Claude plan in
  use. The hybrid option was chosen over retrying everything automatically
  because retrying quota, auth or prompt failures only burns runs.

## Ideas to evaluate (no commitment)

- A Linear integration that syncs briefs as issues.
- A local web dashboard (FastAPI + HTMX) as a visual alternative to
  Telegram.
- Multi-model support: a local model on a local GPU for low-complexity tasks,
  and Claude Code for complex ones.
