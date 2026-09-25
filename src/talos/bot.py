#!/usr/bin/env python3
"""
Telegram bot to control the orchestrator from your phone.

Commands:
    /run     — run one orchestrator pass now
    /status  — how many briefs are pending / failed
    /list    — list pending briefs with their priority
    /brief   — conversational flow to create a new brief
    /logs    — last 15 lines of the main log
    /cancel  — cancel an in-progress /brief
    /help    — command list (alias: /start)

Event harness control:
    /pause   — kill switch: webhooks keep recording, nothing runs
    /resume  — lift the pause
    /events  — what came in and what the filters ate
    /retry   — requeue a failed event (/retry <id>)

Restricted to the chat_id configured in config.yaml.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path

import frontmatter
from telegram import (
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.helpers import escape_markdown
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from talos.dispatch import (
    LOG_DIR,
    find_pending_briefs,
    load_config,
    run_once,
    setup_logging,
)
from talos.util import PAUSED_FILE, slugify

# The only thing /retry can revive: a run that ended badly and whose brief
# is still in ToDos/ (mark_failed does not move it).
RETRYABLE_STATES = {"failed"}

# States of the /brief ConversationHandler
PROJECT, TITLE, CONTEXT, PRIORITY, CONFIRM = range(5)

logger = logging.getLogger("talos.bot")

CONFIG = load_config()

if not CONFIG.get("telegram_chat_id"):
    raise SystemExit(
        "telegram_chat_id is not set in config.yaml. "
        "Send your bot a message and get the chat_id from "
        "https://api.telegram.org/bot<TOKEN>/getUpdates"
    )

ALLOWED_CHAT_ID = int(CONFIG["telegram_chat_id"])


def md(text) -> str:
    """Escape variable text for Telegram's legacy Markdown.

    A stray `_` or `*` —a snake_case title, `repos[*].activated_at`
    in a rejection's detail— makes Telegram reject the whole message
    with a 400, and the command looks unresponsive.
    """
    return escape_markdown(str(text), version=1)


# ── Access restriction ───────────────────────────────────────────────────────


def restricted(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.id != ALLOWED_CHAT_ID:
            logger.warning(
                "Access denied: chat_id=%s",
                update.effective_chat.id,
            )
            await update.message.reply_text("⛔ Not authorized.")
            return
        return await func(update, ctx)
    return wrapper


# ── /run ─────────────────────────────────────────────────────────────────────


async def run_once_and_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        count = run_once(CONFIG, dry_run=False)
        msg = f"✅ Pass complete. {count} brief(s) processed."
    except Exception as e:
        logger.exception("Error in run_once")
        msg = f"❌ Orchestrator error: {e}"
    await update.message.reply_text(msg)


@restricted
async def cmd_run(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("▶️ Running orchestrator...")
    ctx.application.create_task(run_once_and_report(update, ctx))


# ── /status ──────────────────────────────────────────────────────────────────


@restricted
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    vault = Path(CONFIG["vault"])
    todos = vault / CONFIG["briefs_dir"]
    all_briefs = list(todos.rglob("*.md")) if todos.exists() else []

    pending: list[Path] = []
    failed: list[Path] = []
    for f in all_briefs:
        try:
            meta = frontmatter.load(f).metadata
        except Exception:
            continue
        status = meta.get("status")
        if status == "pending":
            pending.append(f)
        elif status == "failed":
            failed.append(f)

    text = (
        "📊 *Orchestrator status*\n\n"
        f"⏳ Pending: {len(pending)}\n"
        f"❌ Failed:  {len(failed)}\n"
    )
    if failed:
        names = "\n".join(f"  • `{f.stem}`" for f in failed[:5])
        text += f"\n*Failed (first 5):*\n{names}"

    waiting = _waiting_release()
    if waiting:
        lines = "\n".join(f"  • {md(repo)}: {n}" for repo, n in waiting)
        text += f"\n\n⏳ *Waiting for prod release:*\n{lines}"

    await update.message.reply_text(text, parse_mode="Markdown")


def _waiting_release() -> list[tuple[str, int]]:
    """`merged_dev` issues per repo: merged into dev, waiting for the release."""
    try:
        from talos import store

        if not store.DB_PATH.exists():
            return []
        with store.connect() as conn:
            rows = conn.execute(
                "SELECT gh_repo, COUNT(*) AS n FROM events WHERE state = 'merged_dev' "
                "GROUP BY gh_repo ORDER BY gh_repo"
            ).fetchall()
        return [(r["gh_repo"] or "?", int(r["n"])) for r in rows]
    except Exception as e:
        logger.warning("Could not count the merged_dev issues: %s", e)
        return []


# ── /list ────────────────────────────────────────────────────────────────────


@restricted
async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    briefs = find_pending_briefs(CONFIG["vault"], CONFIG["briefs_dir"])

    if not briefs:
        await update.message.reply_text("📭 No pending briefs.")
        return

    emoji_map = {"high": "🔴", "medium": "🟡", "low": "🟢"}
    lines = [f"📋 *Pending briefs ({len(briefs)}):*\n"]
    for b in briefs:
        emoji = emoji_map.get(b.priority, "⚪")
        lines.append(f"{emoji} `{b.project}` — {b.path.stem}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── /logs ────────────────────────────────────────────────────────────────────


@restricted
async def cmd_logs(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log_file = LOG_DIR / "dispatch.log"
    if not log_file.exists():
        await update.message.reply_text("📭 No logs yet.")
        return

    lines = log_file.read_text().splitlines()[-15:]
    text = "```\n" + "\n".join(lines) + "\n```"
    text = text[:4000]
    await update.message.reply_text(text, parse_mode="Markdown")


# ── /help ────────────────────────────────────────────────────────────────────


HELP_TEXT = (
    "*Available commands:*\n\n"
    "/run — run one orchestrator pass now\n"
    "/status — how many briefs are pending / failed\n"
    "/list — list pending briefs with their priority\n"
    "/brief — guided flow to create a new brief\n"
    "/logs — last 15 lines of the main log\n"
    "/cancel — cancel an in-progress /brief\n"
    "/help — show this message\n\n"
    "*Event harness:*\n\n"
    "/pause — kill switch: everything is admitted, no run starts\n"
    "/resume — lift the pause\n"
    "/events — what came in and what the filters ate\n"
    "/retry `<id>` — requeue a failed event\n"
    "/reconcile — ask GitHub for issues the webhook missed\n"
    "/backfill `<repo> [n]` — admit the `n` oldest open issues"
)


@restricted
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


# ── /brief — conversational flow ─────────────────────────────────────────────


@restricted
async def brief_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    projects = list(CONFIG.get("projects", {}).keys())
    if not projects:
        await update.message.reply_text(
            "❌ No projects in config.yaml. Edit `projects:` and restart the bot."
        )
        return ConversationHandler.END

    keyboard = [projects[i:i + 3] for i in range(0, len(projects), 3)]
    await update.message.reply_text(
        "📝 *New brief*\n\nWhich project?",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True),
    )
    return PROJECT


async def brief_project(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    project = update.message.text.strip()
    if project not in CONFIG.get("projects", {}):
        await update.message.reply_text(
            f"❓ Project `{project}` not found. Type one of the keyboard options.",
            parse_mode="Markdown",
        )
        return PROJECT

    ctx.user_data["project"] = project
    await update.message.reply_text(
        "✏️ Brief title?",
        reply_markup=ReplyKeyboardRemove(),
    )
    return TITLE


async def brief_title(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data["title"] = update.message.text.strip()
    await update.message.reply_text(
        "💬 Context? (paste the message, a description, whatever you have)"
    )
    return CONTEXT


async def brief_context(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data["context"] = update.message.text.strip()
    keyboard = [["🔴 high", "🟡 medium", "🟢 low"]]
    await update.message.reply_text(
        "🎯 Priority?",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True),
    )
    return PRIORITY


async def brief_priority(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    priority = raw.split()[-1]
    if priority not in ("high", "medium", "low"):
        await update.message.reply_text("Pick high, medium or low.")
        return PRIORITY
    ctx.user_data["priority"] = priority

    d = ctx.user_data
    preview_ctx = d["context"]
    if len(preview_ctx) > 200:
        preview_ctx = preview_ctx[:200] + "..."

    preview = (
        "📋 *Summary*\n\n"
        f"*Project:* {md(d['project'])}\n"
        f"*Title:* {md(d['title'])}\n"
        f"*Priority:* {priority}\n\n"
        f"*Context:*\n{md(preview_ctx)}"
    )
    keyboard = [["✅ Create brief", "✅ Create and run now"], ["❌ Cancel"]]
    await update.message.reply_text(
        preview,
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True),
    )
    return CONFIRM


async def brief_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    choice = update.message.text.strip()

    if "Cancel" in choice:
        await update.message.reply_text(
            "❌ Cancelado.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ConversationHandler.END

    d = ctx.user_data
    slug = slugify(d["title"])
    filename = f"{datetime.now():%Y-%m-%d}-{slug}.md"

    brief_md = (
        f"---\n"
        f"project: {d['project']}\n"
        f"status: pending\n"
        f"priority: {d['priority']}\n"
        f"created: {datetime.now().isoformat()}\n"
        f"---\n\n"
        f"# {d['title']}\n\n"
        f"## Context\n\n"
        f"{d['context']}\n\n"
        f"## Definition of done\n\n"
        f"## Constraints\n"
    )

    todos = Path(CONFIG["vault"]) / CONFIG["briefs_dir"]
    todos.mkdir(parents=True, exist_ok=True)
    target = todos / filename
    target.write_text(brief_md)
    logger.info("Brief created via bot: %s", target)

    await update.message.reply_text(
        f"✅ Brief created: `{filename}`",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )

    if "run now" in choice.lower():
        await update.message.reply_text("▶️ Running orchestrator...")
        ctx.application.create_task(run_once_and_report(update, ctx))

    return ConversationHandler.END


@restricted
async def brief_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "❌ Cancelado.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


# ── Main ─────────────────────────────────────────────────────────────────────


# ── Event harness control ────────────────────────────────────────────────────


@restricted
async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Kill switch. A cutoff you can reach from your phone is worth more than one
    hidden in a YAML file.

    It pauses dispatch, not admission: whatever arrives meanwhile is admitted
    and stays `pending`, and /resume runs it with nothing extra. A pass in
    progress finishes the run it has in hand and does not start the next one.
    """
    sentinel = Path(PAUSED_FILE).expanduser()
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.touch()
    await update.message.reply_text(
        "⏸ *Harness paused*\n\n"
        "Webhooks keep admitting events and writing briefs, "
        "but no new run starts. The one in progress finishes.\n\n"
        "`/resume` to resume: whatever is queued runs on its own.",
        parse_mode="Markdown",
    )


@restricted
async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    sentinel = Path(PAUSED_FILE).expanduser()
    if sentinel.exists():
        sentinel.unlink()
        await update.message.reply_text("▶️ Harness resumed.")
    else:
        await update.message.reply_text("The harness was not paused.")


@restricted
async def cmd_events(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the ledger. A filter you cannot audit is one you switch off within a
    week, so rejections are listed too."""
    try:
        from talos import store
    except Exception as e:
        await update.message.reply_text(f"Could not read the ledger: {e}")
        return

    if not store.DB_PATH.exists():
        await update.message.reply_text("No events recorded yet.")
        return

    args = [a.lower() for a in (ctx.args or [])]
    only_rejected = "rejected" in args or "rechazados" in args

    with store.connect() as conn:
        sql = "SELECT * FROM events"
        if only_rejected:
            sql += " WHERE state LIKE 'rejected_%'"
        sql += " ORDER BY id DESC LIMIT 15"
        rows = list(conn.execute(sql))

    if not rows:
        await update.message.reply_text("No matching events.")
        return

    lines = []
    for r in rows:
        mark = store.state_icon(r["state"])
        title = (r["title"] or r["dedupe_key"])[:42]
        lines.append(f"{mark} `{r['id']}` `{r['state']}`\n   {md(title)}")

    header = "*Recent rejections*" if only_rejected else "*Recent events*"
    await update.message.reply_text(
        f"{header}\n\n" + "\n".join(lines) +
        "\n\n`/events rejected` to see only the filtered ones.",
        parse_mode="Markdown",
    )


@restricted
async def cmd_retry(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Requeue a failed event by setting its brief back to pending.

    It is manual on purpose: a half-done run may have pushed a branch
    or opened a PR, so retrying is a decision that needs context.

    Only on `failed`. On a `pr_open` or `done` row it left a `pending`
    brief outside ToDos/ (nobody picks it up) and the row `admitted`,
    blocking the dedupe forever; on a `running` one it duplicated the
    run.
    """
    if not ctx.args:
        await update.message.reply_text("Usage: `/retry <id>` (see `/events`)", parse_mode="Markdown")
        return
    try:
        event_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("The id must be a number.")
        return

    try:
        from talos import store
        with store.connect() as conn:
            row = store.get_event(conn, event_id)
            if row is None:
                await update.message.reply_text(f"Event {event_id} does not exist.")
                return
            if row["state"] not in RETRYABLE_STATES:
                await update.message.reply_text(
                    f"Event {event_id} is in `{row['state']}`: only events in "
                    f"{', '.join(f'`{s}`' for s in sorted(RETRYABLE_STATES))} can be retried.",
                    parse_mode="Markdown",
                )
                return
            # `Path("")` is `.`, which exists: without this check a row with no
            # brief got through and the error surfaced further down, confusingly.
            brief_path = Path(row["brief_path"]) if row["brief_path"] else None
            if brief_path is None or not brief_path.is_file():
                await update.message.reply_text(
                    f"Event {event_id} has no brief on disk "
                    f"(`{row['brief_path'] or 'none'}`)."
                )
                return
            post = frontmatter.load(brief_path)
            post.metadata["status"] = "pending"
            post.metadata.pop("last_error", None)
            post.metadata.pop("started_at", None)
            brief_path.write_text(frontmatter.dumps(post))
            store.set_state(conn, event_id, "admitted", last_error=None, claimed_at=None)
    except Exception as e:
        await update.message.reply_text(f"Could not requeue: {e}")
        return

    await update.message.reply_text(
        f"🔁 Event `{event_id}` requeued: `{brief_path.name}` is back to *pending*.\n"
        "The next pass picks it up (`/run` to force it now).",
        parse_mode="Markdown",
    )


@restricted
async def cmd_backfill(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually admit a repo's N oldest open issues.

    The `activated_at` cutoff leaves the whole backlog out on purpose: fifty-odd
    open issues on a busy repo would be days of serialized queue. This is
    the door to eat through it bit by bit, on your call and not the harness's.
    """
    if len(ctx.args or []) < 1:
        await update.message.reply_text(
            "Usage: `/backfill <repo> [n]`\n\n"
            "Example: `/backfill api 2`\n"
            "Admits the `n` *oldest* open issues (default 1), "
            "skipping only the backlog cutoff. `ai-skip` still applies, and "
            "the caps decide when they run.",
            parse_mode="Markdown",
        )
        return

    needle = ctx.args[0]
    count = 1
    if len(ctx.args) > 1:
        try:
            count = max(1, min(5, int(ctx.args[1])))
        except ValueError:
            await update.message.reply_text("The second argument must be a number.")
            return

    # Accepts the short name: typing `acme-org/api` on a
    # phone is exactly the friction that makes you not use the command.
    repos = list((CONFIG.get("repos") or {}).keys())
    matches = [r for r in repos if needle.lower() in r.lower()]
    if not matches:
        await update.message.reply_text(
            "Could not find that repo in `repos:`. Known ones:\n"
            + "\n".join(f"• `{r}`" for r in repos),
            parse_mode="Markdown",
        )
        return
    if len(matches) > 1:
        await update.message.reply_text(
            "Ambiguous, be more specific:\n" + "\n".join(f"• `{r}`" for r in matches),
            parse_mode="Markdown",
        )
        return

    gh_repo = matches[0]
    await update.message.reply_text(
        f"⏳ Looking for the {count} oldest in `{gh_repo}`…", parse_mode="Markdown"
    )
    try:
        from talos import reconcile

        cfg = await asyncio.to_thread(reconcile.load_cfg)
        results = await asyncio.to_thread(reconcile.backfill, cfg, gh_repo, count)
    except Exception as e:
        await update.message.reply_text(f"Backfill failed: {e}")
        return

    if not results:
        await update.message.reply_text(
            f"No unhandled open issues left in `{gh_repo}`.",
            parse_mode="Markdown",
        )
        return

    lines = []
    for r in results:
        mark = "📥" if r.get("state") in ("admitted", "dry_run") else "🚫"
        detail = f" ({md(r['detail'])})" if r.get("detail") else ""
        lines.append(f"{mark} #{r.get('number')} → `{r.get('state')}`{detail}")
    admitted = sum(1 for r in results if r.get("state") in ("admitted", "dry_run"))
    tail = f"\n\n{admitted} brief(s) generated. `/run` to start now." if admitted else ""
    await update.message.reply_text(
        f"*Backfill of* `{gh_repo}`\n\n" + "\n".join(lines) + tail,
        parse_mode="Markdown",
    )


@restricted
async def cmd_reconcile(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Force the reconciliation pass against GitHub.

    It runs on its own every `reconcile_interval_minutes` inside dispatch; this is
    for when you know you opened an issue while the tunnel was down and do not want
    to wait for the interval.
    """
    await update.message.reply_text("⏳ Asking GitHub…")
    try:
        from talos import reconcile

        cfg = await asyncio.to_thread(reconcile.load_cfg)
        results = await asyncio.to_thread(reconcile.reconcile, cfg)
        await asyncio.to_thread(reconcile.touch_stamp)
    except Exception as e:
        await update.message.reply_text(f"The reconciler failed: {e}")
        return

    if not results:
        await update.message.reply_text(
            "✅ Nothing new: the ledger already knows every open issue."
        )
        return

    from talos import store

    admitted = [r for r in results if r.get("state") == "admitted"]
    lines = [
        f"{store.state_icon(str(r.get('state') or ''))} "
        f"#{r.get('pr') or r.get('number')} → `{r.get('state')}`"
        for r in results[:15]
    ]
    await update.message.reply_text(
        f"*Reconciler*: {len(results)} event(s), {len(admitted)} admitted\n\n"
        + "\n".join(lines),
        parse_mode="Markdown",
    )


def main() -> None:
    setup_logging()
    token = CONFIG.get("telegram_bot_token", "")
    if not token:
        raise SystemExit("telegram_bot_token is not set in config.yaml")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("run", cmd_run))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler(["help", "start"], cmd_help))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("events", cmd_events))
    app.add_handler(CommandHandler("retry", cmd_retry))
    app.add_handler(CommandHandler("backfill", cmd_backfill))
    app.add_handler(CommandHandler("reconcile", cmd_reconcile))

    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("brief", brief_start)],
        states={
            PROJECT:  [MessageHandler(filters.TEXT & ~filters.COMMAND, brief_project)],
            TITLE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, brief_title)],
            CONTEXT:  [MessageHandler(filters.TEXT & ~filters.COMMAND, brief_context)],
            PRIORITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, brief_priority)],
            CONFIRM:  [MessageHandler(filters.TEXT & ~filters.COMMAND, brief_confirm)],
        },
        fallbacks=[CommandHandler("cancel", brief_cancel)],
    ))

    logger.info("Bot started, waiting for commands. Allowed chat_id=%s", ALLOWED_CHAT_ID)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
