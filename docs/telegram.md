# Telegram bot

The Telegram bot is the control panel. It runs passes, shows the queue and the
event ledger, pauses the harness, and creates briefs from the chat. Dispatch
and the event harness also use it to send notifications.

Telegram is optional. If `telegram_bot_token` and `telegram_chat_id` are
empty, dispatch skips notifications without a warning and everything else
works. The bot process itself (`talos-bot`) won't start without both.

## Set up the bot (5 minutes)

1. **Create the bot.** In Telegram, talk to
   [@BotFather](https://t.me/BotFather), send `/newbot`, and give it a name.
   BotFather replies with the token.
2. **Get your `chat_id`.** Send any message to the new bot, then run this in a
   terminal:

   ```bash
   curl https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
   ```

   Look for `"chat":{"id": XXXXXXX}`. That number is your `chat_id`.
3. **Put both in `config.yaml`:**

   ```yaml
   telegram_bot_token: "1234567890:ABCdef..."
   telegram_chat_id: "987654321"
   ```

4. Start the bot with `systemctl --user start talos-bot` (see
   [setup.md](setup.md#run-as-systemd-user-services)), or with
   `deploy/run.sh`, which also runs the dispatch watcher.

The bot only answers the configured `chat_id`. A message from any other chat
gets "not authorized" and a warning in the log.

Restart the bot after you change `config.yaml`, because it reads the config
once at startup.

## Commands

### Briefs and passes

| Command | What it does |
| --- | --- |
| `/run` | Runs one dispatch pass now |
| `/status` | How many briefs are pending and failed, and how many issues wait for a release in each repo |
| `/list` | Lists pending briefs with their priority |
| `/brief` | Guided conversation that creates a brief from the chat |
| `/logs` | Last 15 lines of `dispatch.log` |
| `/cancel` | Cancels a `/brief` in progress |
| `/help`, `/start` | Shows the command list |

### Event harness

| Command | What it does |
| --- | --- |
| `/pause` | Kill switch. Everything is still admitted and recorded, but no run starts |
| `/resume` | Lifts the pause |
| `/events` | What came in. `/events rejected` shows what the filters dropped |
| `/retry <id>` | Requeues a failed event |
| `/reconcile` | Asks GitHub for what the webhook missed and runs the reconciler now |
| `/backfill <repo> [n]` | Admits the `n` oldest open issues (default 1, max 5) |

`/pause` and `/resume` create and remove the `~/.orchestrator/PAUSED`
sentinel. See
[event-harness.md](event-harness.md#pause-stops-dispatch-not-admission) for
what pausing does and doesn't stop.

## `/brief`: create a brief from the chat

`/brief` walks you through the fields and writes the file to
`{vault}/{briefs_dir}/` with `status: pending`:

1. **Project**: pick one from the keyboard. The choices come from
   `projects:` in `config.yaml`.
2. **Title**.
3. **Context**: what the agent needs to know.
4. **Priority**: `high`, `medium` or `low`.
5. **Confirm**: create the brief, create it and run a pass right away, or
   cancel.

The file is named `YYYY-MM-DD-<title-slug>.md` and has `Context`,
`Definition of done` and `Constraints` sections. You can fill in the last two
in Obsidian before the next pass picks up the brief. See
[briefs.md](briefs.md) for the format.

## Notifications

Dispatch sends a message when a brief finishes. The emoji shows the outcome:
✅ done, ❌ failed, ⚠️ aborted, 🔀 PR opened, 🔍 review published, 🧹 review
threads resolved, 📦 merged to the integration branch, 🎉 merged or released,
and 👀 needs a human. The message includes one line per PR and the tail of the
run log.

The event harness also sends notices for issues that reach production and
get closed, for a reopened issue that reached prod (you decide what to do),
and for rows that are stuck in `merged_dev` (see
[event-harness.md](event-harness.md#from-merge-to-prod-when-the-issue-closes)).
