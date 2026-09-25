# Event harness: Sentry and GitHub issues to reviewed PRs

Besides the pull-based brief flow, the harness listens for events and turns
production errors and GitHub issues into PRs that come with a review.

- [The harness repo and a sandbox repo](#the-harness-repo-and-a-sandbox-repo)
- [The flow](#the-flow)
- [From merge to prod: when the issue closes](#from-merge-to-prod-when-the-issue-closes)
- [Services](#services)
- [The reconciler: why the tunnel is not critical](#the-reconciler-why-the-tunnel-is-not-critical)
- [Exposing the ingress (only Sentry needs it)](#exposing-the-ingress-only-sentry-needs-it)
- [Configure a repo](#configure-a-repo)
- [Every issue gets in: what keeps it under control](#every-issue-gets-in-what-keeps-it-under-control)
- [Sentry](#sentry)
- [Headless BMAD skills](#headless-bmad-skills)
- [Operations](#operations)
- [Rolling out to production, in this order](#rolling-out-to-production-in-this-order)

Container hardening for these pipelines is covered in
[security-model.md](security-model.md#hardened-containers).

## The harness repo and a sandbox repo

- **This repo** is the harness code. Never point the harness at its own repo,
  or it will open PRs against its own code.
- **A sandbox repo** (for example `your-org/sandbox`) is a test repo where
  the harness runs in `live` mode without touching a production repo. Test
  PRs go there.

The sandbox is optional. It's useful for testing harness changes end to end.
If you don't use it, turn it off with `enabled: false`.

## The flow

```text
Sentry / GitHub → tunnel ──→ webhookd :8787 ──┬─→ events.db (dedupe + triage)
GitHub (every 30 min) → reconciler ───────────┘          ↓
                                          brief in {vault}/ToDos/*.md
                                                         ↓
                                           talos-dispatch → container
                                  bmad-quick-dev → PR → bmad-code-review
                                          → review published with gh
                                                         ↓
                   (the review left `patch` findings, or you comment on the PR) → review-fix
                                                         ↓
                              (you merge to base_branch, e.g. developer)
                                                         ↓
                     merged_dev: the issue stays open, label ai-merged-dev
                                                         ↓
                          (release developer → prod_branch, e.g. main)
                                                         ↓
              reconciler sees the merge commit in prod → closes the issue → done
```

The work is split into two stages on purpose.

**Stage A (`issue-fix`)** implements the fix, opens the PR and publishes the
review. **Stage A does not fix what its own review finds.** It publishes the
review against the SHA it reviewed (`Reviewed at <sha>`), and a different
agent makes the fixes in stage B, with clean context. That way every commit
after the review answers a thread (`Fixed in <sha>`), and the reviewer's
context isn't spent on fixing. Dispatch checks this. If the PR doesn't have
the harness's review, or the local branch has unpushed commits, the run is a
`result_contract_violation` even though the PR exists.

**Stage B (`review-fix`)** resolves the threads and runs in a separate
container, because human comments can arrive days later and you can't keep a
container open waiting for them.

**One thread per finding.** Stage A opens one inline thread for each `patch`
and `decision_needed` finding, and never one that groups several ("the three
points above"). Otherwise a stage B commit no longer answers a single
finding. If GitHub rejects an inline comment (the line is outside the diff),
that finding goes into the roll-up comment, marked as such, and the result
JSON counts it in `folded_into_rollup`. The numbers have to add up:
`patch + decision_needed == threads_opened + folded_into_rollup`. If they
don't, or a count isn't a valid integer, dispatch logs a warning with the
counts. The run doesn't fail, because the review is already published.

**Nobody fixes a `patch` that was folded into the roll-up automatically.**
Stage B only reads threads, and without a thread it doesn't see the finding.
The roll-up marks it "needs a human", and dispatch logs a warning when there
are folded findings and `patch` findings together. Fix it yourself, or open a
thread by hand on a diff line so the next round picks it up.

Stage B starts from two entry points. Both end in the same place
(`webhookd.open_review_round`, with the same round cap):

- **The bot's own review.** If stage A leaves `patch` findings with open
  threads, dispatch queues round 1 as soon as it finishes. This doesn't go
  through the webhook: that review carries the anti-loop marker, and the
  webhook rejects it on purpose. The round fixes the `patch` findings and
  leaves the `decision_needed` ones for you.
- **Your comments**, through the webhook or the reconciler.

A GitHub review arrives as a burst of deliveries: one `submitted` plus one
`comment.created` for each inline comment. The burst opens **one** round, not
one per delivery. Only PRs with the `pr_label` count, so the fixer never
touches a teammate's PR.

**Merging is never automatic.**

## From merge to prod: when the issue closes

Automatic PRs merge into the integration branch (`base_branch`, e.g.
`developer` or `devel`), never straight to prod. Going to prod is a release
PR, `developer → main`. GitHub only closes an issue through the PR's
`Closes #N` when that PR merges into the default branch, so without help the
harness's issues would never close. This is the full cycle:

| Moment | Row in `events.db` | Issue label | What happens on GitHub |
| --- | --- | --- | --- |
| Stage A opens the PR | `pr_open` | `ai-in-review` | comment with a link to the PR |
| You merge the PR to `base_branch` | `merged_dev` (+ `merge_commit_sha`) | `ai-merged-dev` | comment: "this will close on its own when it reaches `main`" |
| The release reaches `prod_branch` | `done` | `ai-released` | the harness closes the issue (`completed`) and sends a Telegram notice |

- **`merged_dev` is a standby state.** The issue is fixed in dev but not in
  production. It counts as known, so the reconciler doesn't admit it again,
  but it isn't an open PR, so it doesn't take a slot in `max_open_auto_prs`.
- **How prod is detected.** On every pass, for each `merged_dev` row, the
  reconciler asks GitHub for `compare/{prod_branch}...{merge_commit_sha}`.
  With `behind` or `identical`, the commit is already in prod and the
  reconciler closes the issue. With `ahead` or `diverged`, it hasn't arrived
  yet and nothing happens. If GitHub doesn't answer, nothing happens either,
  and the next pass retries. **Nothing closes an issue without that
  evidence.** The closing comment says "already in `main` (head `abc1234`)".
  That SHA is the head of prod at the time of the pass, not the release
  commit.
- **Human decisions are respected.**
  - If someone already closed the issue, reaching prod only changes the
    labels, with no comment and no Telegram notice.
  - If someone closed it and the commit **hasn't** reached prod, the row
    moves from `merged_dev` to `done` silently, because there's nothing left
    to close.
  - If someone reopened it (most likely the fix didn't work in dev), reaching
    prod does **not** close it. The labels are set, and a Telegram notice
    asks you to decide. The `reopened` webhook doesn't admit it again because
    it already has an `ai-*` label.
- **Stuck rows.** If a row has been in `merged_dev` for 14 days with the
  issue still open, you get **one** Telegram notice and the row is marked in
  `last_error`. It's almost always a squash or rebase release (see below), or
  a force-push that orphaned the merge commit. If it no longer applies, close
  the issue and the row clears on the next pass.
- **Merging straight to prod.** If the PR's base is already `prod_branch`
  and that is also the default branch, GitHub closes the issue through
  `Closes`, and the harness only adds `ai-released`. If `prod_branch` is
  **not** the default branch (git-flow), GitHub doesn't close it. The row
  moves to `merged_dev` without a comment, and the reconciler closes the
  issue on its next pass, with the compare as evidence.
- **`prod_branch`** is an optional field on each repo in `repos:`. If you
  leave it out, it defaults to the repo's default branch, which is fetched
  from GitHub once per process. If you rename the default branch, restart
  `webhookd` and dispatch. If a repo's default branch is the integration
  branch (e.g. `developer`), set `prod_branch` by hand, or every merge to dev
  counts as a release. Even then, in those repos GitHub closes the issue
  through `Closes #N` as soon as you merge to dev. The harness doesn't post
  the "stays open" comment there, it only sets labels. The real fix is for
  the PR to say `Refs #N` in those repos (see the [roadmap](roadmap.md)).
- **The release has to be a merge commit.** Detection works by ancestry: the
  PR's merge commit has to end up in prod's history. If the
  `developer → main` release PR is squashed or rebased, that commit never
  reaches `main`, the compare stays `diverged`, and the issue stays in
  `merged_dev` (open) until you close it or the 14-day notice arrives.
- **A revert is not detected.** If you revert the change in dev before the
  release, the merge commit is still in the history, and the issue still
  closes when the release reaches prod. Reopen it by hand.
- **`enabled: false` also freezes closing.** A disabled repo doesn't admit
  issues and doesn't close the ones waiting in `merged_dev`. The log warns
  about it on every pass.
- **Accepted risk.** If the process dies right between marking the row
  `done` and closing the issue, the issue stays open and nothing retries it.
  This fails safe, since nothing closes without evidence, and the window is a
  single `gh` call. If you see it, close the issue by hand.
- **Labels create themselves** (`gh label create --force`) the first time the
  harness needs them in a repo, and `webhookd` pre-creates them at startup.
  Because of `--force`, this overwrites any color or description you changed
  by hand.
- **Retroactive migration.** Rows that were closed before `merged_dev`
  existed (in `done` or `pr_open`, with the PR already merged to a base that
  isn't prod) move to `merged_dev` on the next reconciler pass, with their
  merge commit and the `ai-merged-dev` label. The `done` rows were already
  announced, so only their label changes. The `pr_open` rows never learned
  about the merge, so they get the merge-to-dev comment and also close the
  review-fix round for the same PR. Rows merged to prod only store their
  merge commit, so GitHub isn't asked about them again. The migration is
  idempotent.

`/status` in Telegram shows how many issues are waiting for a release in each
repo, and `/events` and `scripts/events.py` mark `merged_dev` with ⏳.

## Services

| Unit | What it does |
| --- | --- |
| `talos-bot.service` | The Telegram bot |
| `talos-webhookd.service` | Webhook ingress on `127.0.0.1:8787` |
| `talos-dispatch.timer` | Starts a pass every 2 min |
| `talos-dispatch.service` | The pass itself (`--once`). Starting it by hand runs a pass right away |

The timer is a safety net. `webhookd` starts `talos-dispatch.service` as soon
as it admits an event, so normal latency is seconds. The design uses a timer
instead of `--watch` because the flock already makes concurrent passes safe,
and a crash can't wedge a timer. Installation is in
[setup.md](setup.md#run-as-systemd-user-services).

`webhookd` endpoints:

- `POST /gh` checks `X-Hub-Signature-256` against `GITHUB_WEBHOOK_SECRET`.
- `POST /sentry` checks `Sentry-Hook-Signature` against
  `SENTRY_CLIENT_SECRET`.
- `GET /healthz` returns 200 and needs no auth.

## The reconciler: why the tunnel is not critical

The webhook is the weak link. The tunnel goes down, systemd restarts, or
GitHub retries a few times and gives up. Any of these leaves an issue with no
brief and **no error anywhere**, only silence.

The reconciler (`src/talos/reconcile.py`) closes that gap by asking GitHub,
the source of truth, what is open. It runs inside every dispatch pass (every
`reconcile_interval_minutes`, default 30, where `0` turns it off) and covers
both halves of the cycle:

- **Open issues** with no row in `events.db` are admitted through the same
  gates as the webhook.
- **PRs with the automation label** get any missing `review-fix` round. Each
  human comment is compared against the row's `last_comment_at` marker, so the
  same comment never opens two rounds. The reconciler also closes the cycle
  for the PRs you merged, and for releases that reached prod.

As a result, **the GitHub half of the harness works without a tunnel**, with
the latency of the interval instead of the latency of a webhook. Only Sentry
really needs the tunnel, because it has no forwarder.

```bash
talos-reconcile --dry-run                        # what it would admit, without writing
talos-reconcile --repo owner/repo                # one pass now
talos-reconcile --repo owner/repo --backfill 2   # the 2 oldest issues in the backlog
```

The reconciler **skips everything that already has a row**, rejections
included. That is deliberate: the `activated_at` cutoff has to stick, or the
backlog gets in through the back door. The legitimate way into the backlog is
`/backfill` (or `--backfill`). It bypasses that cutoff and no other gate, and
it counts admissions, so an issue with `ai-skip` at the head of the queue
doesn't block it.

The exceptions are rows that had a brief that never ran: rows from dry run
(once the repo is `live`) and rows that failed while the brief was being
written (`brief_error`). Those are admitted again.

## Exposing the ingress (only Sentry needs it)

```bash
REPOS="owner/repo other/repo" bash scripts/ingress.sh   # Funnel + checks + GitHub webhook
```

The script checks `webhookd`, starts Tailscale Funnel, waits for the
certificate, checks the public `/healthz`, and registers the GitHub webhook
through the API. The secret goes through stdin and is never pasted into a
browser. The script is idempotent. At the end it prints the
`webhook.public_host` value for `config.yaml` and the Sentry Internal
Integration settings.

There are two things the script can't do for you, and it tells you exactly
which one is missing. You have to enable HTTPS on the tailnet
(`login.tailscale.com/admin/dns`), and you have to give the node the `funnel`
attribute in the ACL (`login.tailscale.com/admin/acls`). Funnel also needs
`sudo`. `tailscale funnel --bg` saves its config in tailscaled, so it survives
reboots and doesn't need a systemd unit of its own.

Any other HTTPS reverse proxy or tunnel works, as long as it forwards to
`127.0.0.1:8787`.

The webhook registers with only the `repo` scope, so you don't need to
refresh the token with `admin:repo_hook`:

```bash
python scripts/setup_webhook.py --repo owner/repo --list
python scripts/setup_webhook.py --repo owner/repo --url https://your-host.your-tailnet.ts.net
python scripts/setup_webhook.py --repo owner/repo --url https://your-host.your-tailnet.ts.net --delete
```

## Configure a repo

In `config.yaml`, under `repos:`, keyed as `owner/repo`:

```yaml
repos:
  "acme-org/web-app":
    project: acme                   # must exist in projects:
    subrepo: web-app                # "" if the project is the repo itself
    base_branch: developer          # where the run starts and where the PR goes
    prod_branch: main               # optional; default: the repo's default branch
    enabled: true
    mode: dry_run                   # dry_run writes draft briefs that nothing runs
    activated_at: "2026-09-21T00:00:00Z"
    skip_label: ai-skip
    authors_allow: []               # empty = any collaborator
    pr_label: ai-generated
    cooldown_minutes: 60
    max_open_auto_prs: 3
    daily_cap: 3
    max_review_fix_rounds: 3
```

**`base_branch` is the repo's development branch, and it varies from repo to
repo.** The run starts from `origin/<base_branch>` and the PR targets it. It
might be `developer` in one repo, `devel` in another, `qa` in a third, and
`main` in a small repo with no integration branch. `prod_branch` is where the
release lands, which is when the issue closes.

**`activated_at` is the handbrake.** Only issues created after that date get
in. Without it, turning on a repo with 51 open issues queues 51 runs. Add the
backlog by hand, a little at a time.

To add a repo to the harness:

1. Put the repo's folder in `projects:`. Your usual checkout is fine, because
   the agent works in a separate worktree and doesn't touch it. If the
   project groups several repos (like `Acme Main`), use the umbrella folder
   and put the repo name in `subrepo`.
2. Add its block under `repos:` with `enabled: true`, `mode: dry_run` and
   today's date in `activated_at`.
3. Check BMAD in the workspace with `python scripts/bmad_check.py --install`
   (see [Headless BMAD skills](#headless-bmad-skills)).
4. Restart `webhookd`, which reads `repos:` at startup:
   `systemctl --user restart talos-webhookd talos-bot`. Dispatch and the
   reconciler re-read the config on every pass.
5. If you have a tunnel, register the webhook with
   `scripts/setup_webhook.py`. Without a tunnel, the reconciler covers it.

To remove a repo, set `enabled: false`.

A routing mistake in `config.yaml` fails at startup, not hours later when
the first webhook arrives. You can also check routing by hand:

```bash
python -m talos.routing --validate
python -m talos.routing --resolve acme-org/web-app
python -m talos.routing --sentry acme-frontend
```

`projects:` is still the allowlist of local paths, and `repos:` maps
`owner/repo` onto that registry. This is what `/workspace` is in the
container:

- **With `subrepo`** (an umbrella folder like `Acme Main`): `/workspace` is
  the project folder, which is where `_bmad/` and the project skills live.
  The worktree is mounted over the sub-repo at `/workspace/$SUBREPO`, and the
  sibling repos stay visible as context.
- **Without `subrepo`** (the project is the repo): `/workspace` is a harness
  folder, `~/.orchestrator/workspaces/<project>`, with BMAD inside. The
  worktree goes in `/workspace/<checkout folder name>`. That is the folder
  name in `projects:`, which isn't always the repo's name on GitHub. This
  way BMAD never lands in your checkout, even if the repo already has its own
  `_bmad/`.

## Every issue gets in: what keeps it under control

There's no opt-in label: any new issue triggers the harness. This works
because the repos are private, so whoever opens an issue is on your team. If
your repo is public or has outside contributors, use `authors_allow` (see
below). These are the limits:

- **`activated_at`**: the backlog doesn't get in by itself.
- **`ai-skip`** (`skip_label`): opt-out per issue. If you add it after the
  issue got in, or you close the issue, dispatch checks GitHub before it
  starts the container, and the brief moves to `skipped/`.
- **`max_open_auto_prs: 3`**: **the real valve.** A repo never has more than
  3 open automatic PRs. The queue moves again when you merge or close one.
- **`daily_cap` / `global_daily_cap`**: stage A runs started today (UTC).
- **`cooldown_minutes`**: minimum time between two stage A runs in the same
  repo.
- **`authors_allow`**: when it isn't empty, only these GitHub logins get in.
  Bot authors are rejected unless they're listed.
- **`events.db`**: the main re-entry guard. An issue that has a row doesn't
  get in again. Back up this file.
- **Lifecycle labels**: the second lock. An issue with `ai-in-review`,
  `ai-merged-dev` or `ai-released` is rejected at admission
  (`rejected_already_handled`), even if `events.db` is lost or someone
  reopens the issue.

The three caps are evaluated **in dispatch, before each brief**, not at
admission. A brief that hits a cap isn't rejected. It stays `pending` (the
ledger writes `deferred_<cap>` to `last_error`) and runs on the first pass
where the cap frees up. The caps apply only to stage A. Holding back a
`review-fix` with the open-PR cap would deadlock, because you can't merge
what can't finish getting fixed. Stage B is bounded by
`max_review_fix_rounds` instead.

Other triage rejections you may see in `scripts/events.py --rejected`
include pull requests posing as issues, empty bodies, the harness's own
comments and reviews (anti-loop), disallowed authors, stale or missing
timestamps, and Sentry events filtered out by level, environment or title.

## Sentry

Only the `event_alert` resource is accepted. Subscribe the Internal
Integration to **Alert Rule Action**, and set the threshold in the alert rule
itself ("seen more than N times in M minutes", `environment = production`).
Sentry has the real count. `issue.created` arrives with a count of about 1,
and Sentry doesn't resend it when the issue grows, so a threshold on the
harness side rejected exactly the events that mattered. `min_level`,
`environments` and `title_denylist` still filter.

The Internal Integration needs:

- **Webhook URL:** `https://<public host>/sentry`
- **Alert Rule Action:** on
- **Permissions:** Issue & Event: Read, Project: Read

Copy its Client Secret and Auth Token into `~/.orchestrator/secrets.env`
(`SENTRY_CLIENT_SECRET`, `SENTRY_AUTH_TOKEN`) and restart `webhookd`.

Map Sentry projects to repos under `sentry.projects`:

```yaml
sentry:
  org: acme
  projects:
    acme-frontend:
      repo: "acme-org/web-app"
      project_id: ""          # numeric id; see below
      service_path: ""
      environments: [production]
      min_level: error
      enabled: false
    acme-backend:             # several services reporting to ONE Sentry project
      repo: "acme-org/api"
      project_id: ""
      service_map:            # value of the `service` tag -> path in the repo
        accounts: services/accounts
        billing: services/billing
      environments: [production]
      min_level: error
      enabled: false
```

When several services report to the same Sentry project, the slug doesn't
say which one failed. The `service` tag does, as long as each service sets it
(e.g. `sentry_sdk.set_tag("service", ...)`). An event without the tag is
rejected as `rejected_unknown_service`, so the harness never guesses a
service.

`webhookd` answers in milliseconds with what the payload carries. Dispatch
fetches the full context (the issue and its latest event, over REST) when it
picks up the brief. If the issue is already resolved at that point, the brief
moves to `skipped/`.

**Not yet verified against a real payload:** whether `data.event.project` is
numeric (that's why `sentry.projects[*].project_id` exists; you'll find it in
Settings → Projects → your project, as the number in the Client Keys URL),
and whether tags arrive as `[[k, v]]` (both shapes are accepted). Capture one
with the real integration and run it through `scripts/replay.py` before you
turn a project on.

Without `SENTRY_CLIENT_SECRET`, `/gh` still works and `/sentry` returns 503.

## Headless BMAD skills

The pipelines run [BMAD Method](https://github.com/bmad-code-org/BMAD-METHOD)
skills. `issue-fix` uses `bmad-quick-dev` to implement and `bmad-code-review`
to review, and `review-fix` follows the same headless contract. Those skills are
written for a human at the keyboard. They HALT in about 14 places, greet you,
run `code -r`, and declare `NEVER auto-push`. The harness neutralizes this in
three layers, all keyed on `HEADLESS=1`:

1. `_bmad/custom/bmad-*.toml` in the workspace: `activation_steps_prepend`
   and `persistent_facts` (appended) and `on_complete` (a scalar that
   overrides).
2. `_bmad/custom/headless-contract.md`: the table of what the agent decides at
   each checkpoint.
3. `src/talos/prompts/lead-issue-fix.md` (or `lead-review-fix.md`), passed
   with `--append-system-prompt-file`. It takes precedence over the skills'
   Markdown.

Your interactive sessions don't change at all, because `HEADLESS` is only
set inside the harness's containers.

**If the project doesn't have BMAD, dispatch installs it.** Before every
`issue-fix` or `review-fix` run, it checks the workspace:

- Without BMAD, it runs the official remote installer, pinned to v6.10.0:
  `npx bmad-method@6.10.0 install --modules bmm --tools claude-code --yes`.
  It's pinned because since v6.11 `bmad-quick-dev` is a shim to `bmad-build`,
  which HALTs when it sees the harness's `bmad-quick-dev.toml`, and the
  current version no longer ships it. A workspace with BMAD 6.11 or later is
  reported as a conflict instead of running. Moving to `bmad-build-auto` is a
  pipeline change (see the [roadmap](roadmap.md)).
- The headless overrides are the only thing that comes from this repo
  (`src/talos/bmad-kit/`). The `bmad-*.toml` files are copied only if they're
  missing. If the project has its own without the `HEADLESS` hook, the brief
  fails with that reason and you merge the two by hand.
  `headless-contract.md` and `headless-complete.md` belong to the harness and
  are re-synced on every run, so a fix to the contract reaches every workspace
  automatically.
- In a project without `subrepo`, the workspace is
  `~/.orchestrator/workspaces/<project>` and not the repo, so BMAD is never
  installed in your checkout (see [Configure a repo](#configure-a-repo)). If a
  workspace turns out to be a git repo, the installed files go into
  `.git/info/exclude`.

```bash
python scripts/bmad_check.py              # status of each repo in `repos:`
python scripts/bmad_check.py --install    # install before turning a repo on
```

## Operations

```bash
python scripts/events.py                    # what came in
python scripts/events.py --rejected         # what the filters dropped
python scripts/events.py --active           # only live rows
python scripts/events.py --repo owner/repo  # one repo
python scripts/events.py --show 42          # one full row
python scripts/events.py --deliveries       # every delivery and its verdict
python scripts/replay.py <payload.json>     # replay an archived event against webhookd
python scripts/runlog.py <run log>          # one line per agent action
talos-reconcile --dry-run                   # what the webhook missed
python scripts/bmad_check.py                # headless BMAD in each workspace
systemctl --user start talos-dispatch.service   # run a pass now
touch ~/.orchestrator/PAUSED                # kill switch (or /pause in Telegram)
```

`scripts/replay.py` also takes `--source`, `--event`, `--action`,
`--delivery`, `--bad-signature` and `--host`.

### Pause stops dispatch, not admission

With `PAUSED` set, webhooks and the reconciler keep admitting events and
writing briefs, which wait as `pending`. Dispatch checks the sentinel before
each brief, so a pass in progress finishes its current run and doesn't start
the next one. `/resume` runs whatever piled up, with nothing extra to do. If
pausing stopped admission instead, whatever arrived during the pause would be
lost, because the reconciler skips rows it already knows.

### Where to see what's happening

There is no web UI.

| What | Where |
| --- | --- |
| Briefs (draft in dry run, pending, running) | Obsidian, `{vault}/ToDos/` |
| Finished briefs | `{vault}/projects/{project}/completed/`, `skipped/`, `dry-run/` |
| State of each event | `python scripts/events.py`, or `/events` and `/status` in Telegram |
| PR opened, failures, merges | Telegram notices, and comments on the issue |
| The live run | `~/.orchestrator/logs/runs/<date>-<project>.log` |
| The agent's work | `~/.orchestrator/worktrees/<run>` while it runs, then the brief's branch in the repo |

The run log is the `claude -p` JSON stream. To see which phase a run is in,
without the noise:

```bash
tail -f ~/.orchestrator/logs/runs/<log> | grep -o '"task_description":"[^"]*"'
git -C ~/.orchestrator/worktrees/<run> log --oneline origin/<base_branch>..HEAD
```

A slow run isn't necessarily stuck. The reviewers in `bmad-quick-dev` and
`bmad-code-review` run as subagents and take up a good part of the run. In
one early live run, the fix commit showed up at about 15 minutes and the rest
was review. A run is stuck when the run log has no new events for several
minutes, or when the pipeline's `timeouts:` value has passed.

The per-pipeline timeouts are set in `config.yaml`:

```yaml
timeouts:
  issue-fix: 7200     # plan + implementation + two review passes
  review-fix: 2700
```

### Services run from the checkout

The systemd units point at the checkout, so whatever you have checked out is
what runs, uncommitted changes included. After you merge a PR to the harness:

```bash
git checkout main && git pull --ff-only
systemctl --user restart talos-webhookd talos-bot
```

Dispatch doesn't need a restart because the timer starts a new process for
every pass. If your local `main` falls behind a merge that adds files, a
service can crash-loop without warning. This has happened: 1,830 restarts
before the pull. `systemctl --user status` shows it.

### Raw payloads are the test corpus

Raw payloads are kept in `~/.orchestrator/events/raw/`. Every triage bug can
be reproduced with `scripts/replay.py` without touching GitHub or Sentry.

## Rolling out to production, in this order

The order matters because each step makes the next one observable. Don't skip
steps.

**1. One week in `dry_run`.** The repo is `enabled: true, mode: dry_run`.
Events enter the ledger in the `dry_run` state, and briefs are written with
`status: draft`, which dispatch ignores. No containers, no PRs. When you
switch to `live`, nothing that piled up is lost. The reconciler (or the
issue's next webhook) re-admits those rows if they still pass the gates, and
the old draft is archived in `projects/{project}/dry-run/`. This is what you
measure during that week:

```bash
python scripts/events.py                 # did what you expected come in?
python scripts/events.py --rejected      # did the filters drop something they shouldn't have?
ls {vault}/ToDos/*.md                    # read 3-4 full briefs
```

The question to answer is "would I hand this brief to a junior dev?" If the
answer is no, the problem is in the template or the filters, and fixing it
now costs nothing.

**2. `live` with `daily_cap: 1`.** One automatic PR a day is enough to find
everything that's missing without the queue getting away from you. Raise the
cap when merging those PRs becomes routine instead of an event.

```yaml
mode: live
daily_cap: 1
```

There is no command to run a single draft, since `/retry` only applies to
`failed`. Switching to live is per repo, and the drafts that piled up during
dry run run like this:

1. In `config.yaml`, set `mode: live` and the `daily_cap` you want. With N
   approved drafts, `daily_cap: N` runs those and nothing else that day.
2. Run `systemctl --user restart talos-webhookd talos-bot`.
3. Send `/reconcile` in Telegram. The reconciler re-admits the `dry_run`
   rows, archives the drafts in `projects/{project}/dry-run/`, writes
   `pending` briefs and kicks dispatch.

They run one at a time, because dispatch takes a flock, and the next one in
the same repo waits `cooldown_minutes` from the start of the previous one.
From then on, any new issue in the repo also runs automatically. If you only
wanted to run the drafts, switch back to `mode: dry_run` once they're
`running`.

**3. The caps set the system's pace.** With no label gate, the team opening
issues sets the incoming volume, not you labeling them. With `daily_cap: 3`
and `max_open_auto_prs: 3`, a repo never has more than 3 open automatic PRs,
and you merge or close to keep it moving. You choose that limit.

**4. `activated_at` before `enabled: true`.** Always. On a repo with 51 open
issues, no cutoff means days of serialized queue. Check it with
`talos-reconcile --dry-run`, which has to report 0 admitted, and then add the
backlog with `/backfill`, 1 or 2 a day.

**5. Multi-service repos go last.** When several services report to the same
Sentry project, routing depends on the `service` tag (see `service_map`
above). Confirm that every call site sets the tag and that you have the Sentry
project's real slug before you set `enabled: true`.
