#!/usr/bin/env python3
"""
Event ledger of the harness (SQLite).

The split is deliberate: this DB is the *ledger* — what fired, what we
decided and why. The Markdown brief in the vault is still the *work item*.
dispatch.py never queries this DB to decide what to run; that is what
`status: pending` in the front matter is for. That is what keeps the addition small.

This DB is also the main re-entry guard. The second one is the lifecycle
labels (`triage.gate_handled_labels`), which only cover issues the harness
already opened a PR for: if the DB is lost, the reconciler re-admits
everything else after the cutoff. Backing it up is part of operations.
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from talos.util import utcnow

logger = logging.getLogger("orchestrator")

DB_PATH = Path("~/.orchestrator/events.db").expanduser()
RAW_DIR = Path("~/.orchestrator/events/raw").expanduser()

# ThreadingHTTPServer serves each request on its own thread. SQLite in WAL
# mode tolerates concurrent readers, but we serialize writes so two
# simultaneous deliveries of the same issue do not trample each other in the ON CONFLICT.
_WRITE_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS deliveries (
  delivery_id TEXT PRIMARY KEY,
  source      TEXT NOT NULL,
  resource    TEXT,
  action      TEXT,
  received_at TEXT NOT NULL,
  raw_path    TEXT,
  event_id    INTEGER,
  verdict     TEXT
);

CREATE TABLE IF NOT EXISTS events (
  id               INTEGER PRIMARY KEY,
  dedupe_key       TEXT UNIQUE NOT NULL,
  source           TEXT NOT NULL,
  project          TEXT,
  subrepo          TEXT,
  service_path     TEXT,
  gh_repo          TEXT,
  gh_issue         INTEGER,
  pr_number        INTEGER,
  pr_url           TEXT,
  sentry_issue_id  TEXT,
  sentry_project   TEXT,
  sentry_permalink TEXT,
  pipeline         TEXT,
  state            TEXT NOT NULL,
  brief_path       TEXT,
  title            TEXT,
  seen_count       INTEGER NOT NULL DEFAULT 1,
  attempts         INTEGER NOT NULL DEFAULT 0,
  rounds           INTEGER NOT NULL DEFAULT 0,
  claimed_at       TEXT,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  last_error       TEXT,
  last_comment_at  TEXT,
  merge_commit_sha TEXT
);

CREATE INDEX IF NOT EXISTS ix_events_repo_state ON events(gh_repo, state, updated_at);
CREATE INDEX IF NOT EXISTS ix_events_state      ON events(state);
"""

# States in which the event is already being handled or already finished: a
# Sentry re-alert on one of these bumps seen_count and stops there.
ACTIVE_STATES = {"admitted", "dispatched", "running", "pr_open", "merged_dev", "done"}

# `merged_dev`: the stage A PR was merged into the integration branch
# (`developer`/`devel`) and the issue waits for the release to prod. It counts
# as known so the reconciler never re-admits the issue, but it is NOT an open
# PR: the open-PR cap asks GitHub, not this table.
TERMINAL_STATES = {"merged_dev", "done"}

# One icon per state, shared by /events, /reconcile and tools/events.py.
# `released` is not a row state but a reconciler result.
STATE_ICONS = {
    "admitted": "📥", "running": "⚙️", "pr_open": "🔀", "merged_dev": "⏳",
    "done": "✅", "failed": "❌", "dry_run": "🧪", "brief_error": "⚠️",
    "released": "🚀",
}


def state_icon(state: str) -> str:
    # 🚫 only for actual rejections: `done` or `duplicate` are not failures.
    return STATE_ICONS.get(state, "🚫" if state.startswith("rejected_") else "•")

# States of a row that had a brief but can be admitted again.
# `dry_run` stays OUT of ACTIVE_STATES on purpose: if it counted as
# handled, switching a repo to `live` would leave everything that came in during
# the dry-run week marked as a duplicate forever. `rejected_paused` comes
# from the version where the pause blocked admission; today the pause only
# blocks dispatch, but those old rows must still be able to get in.
REOPENABLE_STATES = {"dry_run", "rejected_paused"}


@contextmanager
def connect(db_path: Path | None = None):
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# Columns added after the first version of the schema. `CREATE TABLE
# IF NOT EXISTS` does not add them to a table that already exists, so without this a
# live DB stays in the old shape and the new code fails with "no such
# column" — right on the least exercised path.
# (table, column) → (declaration, optional backfill). The backfill matters:
# a new column starts as NULL, and the code that reads it usually takes
# NULL to mean "nothing ever happened" — which is false for old data and makes the
# system redo work already done the first time the new version runs.
MIGRATIONS = [
    (
        "events", "last_comment_at", "TEXT",
        # Without this, the first PR reconciler would see every human comment
        # older than this version as new and open an extra round on
        # every open PR. `updated_at` is the conservative approximation: whatever
        # was already recorded counts as handled.
        "UPDATE events SET last_comment_at = updated_at "
        "WHERE last_comment_at IS NULL AND pipeline = 'review-fix'",
    ),
    (
        # What was decided about each delivery. Rejections that happen before
        # an event row exists (event type, unmapped repo, the
        # anti-loop marker) are only audited here.
        "deliveries", "verdict", "TEXT", None,
    ),
    (
        # Merge commit of the stage A PR. The release step compares it against
        # the prod branch to decide whether the issue can be closed.
        "events", "merge_commit_sha", "TEXT", None,
    ),
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, decl, backfill in MIGRATIONS:
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not have or column in have:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        if backfill:
            conn.execute(backfill)
        logger.info("Migration: added %s.%s", table, column)


def init_db(db_path: Path | None = None) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def archive_raw(delivery_id: str, body: bytes, raw_dir: Path | None = None) -> str:
    """Store the raw payload before parsing anything.

    It is the replay and test corpus: every triage bug can be reproduced without
    touching GitHub or Sentry. It is written first, on purpose.
    """
    d = Path(raw_dir) if raw_dir else RAW_DIR
    d.mkdir(parents=True, exist_ok=True)
    # The hash is there because sanitizing the id is not injective: without it,
    # `reconcile:a/b#12` and `reconcile:a/b1#2` overwrite the same file.
    safe = "".join(c for c in delivery_id if c.isalnum() or c in "-_")[:64] or "unknown"
    digest = hashlib.sha1(delivery_id.encode()).hexdigest()[:12]
    target = d / f"{safe}-{digest}.json"
    target.write_bytes(body)
    return str(target)


def record_delivery(
    conn: sqlite3.Connection,
    delivery_id: str,
    source: str,
    resource: str = "",
    action: str = "",
    raw_path: str = "",
    verdict: str = "",
) -> bool:
    """Record a delivery. Returns False if we had already seen it.

    Webhook retries come for free here: GitHub resends with the
    same X-GitHub-Delivery, the INSERT hits the PK and we return 202
    without doing anything.
    """
    cur = conn.execute(
        "INSERT OR IGNORE INTO deliveries "
        "(delivery_id, source, resource, action, received_at, raw_path, verdict) "
        "VALUES (?,?,?,?,?,?,?)",
        (delivery_id, source, resource, action, utcnow(), raw_path, verdict or None),
    )
    return cur.rowcount > 0


def set_delivery_verdict(
    conn: sqlite3.Connection, delivery_id: str, verdict: str, event_id: int | None = None
) -> None:
    conn.execute(
        "UPDATE deliveries SET verdict = ?, event_id = COALESCE(?, event_id) "
        "WHERE delivery_id = ?",
        (verdict, event_id, delivery_id),
    )


def forget_delivery(conn: sqlite3.Connection, delivery_id: str) -> None:
    """Delete a delivery so its retry gets processed again.

    It is the rollback of an admission that never got to write the brief: if the
    delivery stayed recorded, GitHub's retry would count as a
    duplicate and the event would be lost without anyone noticing.
    """
    conn.execute("DELETE FROM deliveries WHERE delivery_id = ?", (delivery_id,))


def upsert_event(conn: sqlite3.Connection, dedupe_key: str, fields: dict) -> tuple[int, bool]:
    """Insert the event, or bump seen_count if the dedupe_key already exists.

    Returns (event_id, is_new). An `is_new == False` on a row in
    ACTIVE_STATES means "we are already handling it" and the caller stops.
    """
    now = utcnow()
    cols = {
        "dedupe_key": dedupe_key,
        "state": fields.get("state", "received"),
        "created_at": now,
        "updated_at": now,
    }
    allowed = {
        "source", "project", "subrepo", "service_path", "gh_repo", "gh_issue",
        "pr_number", "pr_url", "sentry_issue_id", "sentry_project",
        "sentry_permalink", "pipeline", "brief_path", "title", "state",
    }
    cols.update({k: v for k, v in fields.items() if k in allowed})
    names = ", ".join(cols)
    holes = ", ".join("?" for _ in cols)
    # INSERT OR IGNORE and not SELECT-then-INSERT: the thread lock does not cover
    # another process (the bot runs /reconcile and /backfill on its own), and
    # the other's INSERT can land between one's SELECT and INSERT.
    cur = conn.execute(
        f"INSERT OR IGNORE INTO events ({names}) VALUES ({holes})", tuple(cols.values())
    )
    if cur.rowcount > 0:
        return int(cur.lastrowid), True
    row = conn.execute(
        "SELECT id FROM events WHERE dedupe_key = ?", (dedupe_key,)
    ).fetchone()
    conn.execute(
        "UPDATE events SET seen_count = seen_count + 1, updated_at = ? WHERE id = ?",
        (now, row["id"]),
    )
    return int(row["id"]), False


def set_state(
    conn: sqlite3.Connection, event_id: int, state: str, **fields
) -> None:
    # `claimed_at` is stamped here and not in the caller because it is the only thing that
    # makes a run visible to the orphan reaper. If the caller
    # forgets, a run that died halfway stays in `running` forever.
    if state == "running":
        fields.setdefault("claimed_at", utcnow())
    sets = ["state = ?", "updated_at = ?"]
    vals: list = [state, utcnow()]
    for k, v in fields.items():
        sets.append(f"{k} = ?")
        vals.append(v)
    vals.append(event_id)
    conn.execute(f"UPDATE events SET {', '.join(sets)} WHERE id = ?", tuple(vals))


def transition(
    conn: sqlite3.Connection, event_id: int, from_state: str, to_state: str, **fields
) -> bool:
    """Compare-and-set of the state. False if the row was no longer in `from_state`.

    The bot's /reconcile and dispatch's pass can run at the same time; the
    one that loses the race must not comment on the issue a second time.
    """
    sets = ["state = ?", "updated_at = ?"] + [f"{k} = ?" for k in fields]
    vals = [to_state, utcnow(), *fields.values(), event_id, from_state]
    cur = conn.execute(
        f"UPDATE events SET {', '.join(sets)} WHERE id = ? AND state = ?", tuple(vals)
    )
    return cur.rowcount > 0


def annotate(conn: sqlite3.Connection, event_id: int, **fields) -> None:
    """Update a row's fields without touching its state."""
    if not fields:
        return
    sets = [f"{k} = ?" for k in fields] + ["updated_at = ?"]
    vals = [*fields.values(), utcnow(), event_id]
    conn.execute(f"UPDATE events SET {', '.join(sets)} WHERE id = ?", tuple(vals))


def get_event(conn: sqlite3.Connection, event_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()


def find_by_dedupe(conn: sqlite3.Connection, dedupe_key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM events WHERE dedupe_key = ?", (dedupe_key,)
    ).fetchone()


def find_issue_row_by_branch(
    conn: sqlite3.Connection, gh_repo: str, branch: str
) -> sqlite3.Row | None:
    """The stage A row whose brief has the same name as the branch.

    Exact file-name comparison, not `LIKE`: with `LIKE
    '%fix-login.md'` a `fix-login` branch grabbed the brief
    `…-github-12-fix-login.md` of another issue, and `_` acted as a wildcard.
    """
    if not branch:
        return None
    rows = conn.execute(
        "SELECT * FROM events WHERE gh_repo = ? AND pipeline = 'issue-fix' "
        "AND brief_path IS NOT NULL ORDER BY id DESC",
        (gh_repo,),
    ).fetchall()
    for r in rows:
        if Path(r["brief_path"]).stem == branch:
            return r
    return None


def is_stage_a_issue(row) -> bool:
    """True for the stage A row of a GitHub issue (the one `Closes #N` closes).

    Sentry-born stage A rows have no issue to close, and review-fix rows
    (`pr:`) belong to the PR, not to the issue.
    """
    return (
        str(row["dedupe_key"] or "").startswith("gh:")
        and row["pipeline"] == "issue-fix"
        and row["gh_issue"] is not None
    )


def rows_in_states(
    conn: sqlite3.Connection, states: set[str] | tuple, gh_repo: str | None = None
) -> list[sqlite3.Row]:
    holes = ", ".join("?" for _ in states)
    sql = f"SELECT * FROM events WHERE state IN ({holes})"
    params: list = sorted(states)
    if gh_repo:
        sql += " AND gh_repo = ?"
        params.append(gh_repo)
    return conn.execute(sql + " ORDER BY id", tuple(params)).fetchall()


def known_issue_numbers(
    conn: sqlite3.Connection, gh_repo: str, worked_only: bool = False,
    reopen: set[str] | frozenset = frozenset(),
) -> set[int]:
    """Issues of the repo that already have a row. What the reconciler must NOT admit.

    With `worked_only=True` it returns only those that actually generated work
    (they have a brief or are active). That is what /backfill needs: after
    a reconciler pass, the WHOLE backlog has a row —in
    `rejected_backlog`— so a "has a row" filter would leave
    backfill with nothing to add, which is exactly the opposite of why it exists.

    Rows whose state is in `reopen` do not count as known: that is
    how a repo that switched to `live` recovers what came in during dry run.
    """
    sql = "SELECT gh_issue FROM events WHERE gh_repo = ? AND gh_issue IS NOT NULL"
    params: list = [gh_repo]
    if worked_only:
        holes = ", ".join("?" for _ in ACTIVE_STATES)
        sql += f" AND (brief_path IS NOT NULL OR state IN ({holes}))"
        params += sorted(ACTIVE_STATES)
    if reopen:
        holes = ", ".join("?" for _ in reopen)
        sql += f" AND state NOT IN ({holes})"
        params += sorted(reopen)
    rows = conn.execute(sql, tuple(params)).fetchall()
    return {int(r["gh_issue"]) for r in rows}


def count_dispatched_today(
    conn: sqlite3.Connection, gh_repo: str | None = None, pipeline: str = "issue-fix"
) -> int:
    """Runs started today (UTC). Feeds daily_cap and global_daily_cap.

    Counts by `claimed_at` —when the container started— and not by
    `created_at`: the cap bounds work done, not events received, and a
    brief deferred yesterday that runs today is today's work.
    """
    sql = (
        "SELECT COUNT(*) AS n FROM events WHERE pipeline = ? "
        "AND claimed_at >= strftime('%Y-%m-%dT00:00:00Z','now')"
    )
    params: list = [pipeline]
    if gh_repo:
        sql += " AND gh_repo = ?"
        params.append(gh_repo)
    return int(conn.execute(sql, tuple(params)).fetchone()["n"])


def last_dispatch_at(
    conn: sqlite3.Connection, gh_repo: str, pipeline: str = "issue-fix"
) -> str | None:
    """When the repo's last run started. Feeds cooldown_minutes."""
    row = conn.execute(
        "SELECT MAX(claimed_at) AS t FROM events WHERE gh_repo = ? AND pipeline = ?",
        (gh_repo, pipeline),
    ).fetchone()
    return row["t"] if row else None


def reap_orphans(conn: sqlite3.Connection, stale_seconds: int) -> list[dict]:
    """Mark runs that died halfway as failed.

    It does not requeue them on its own, on purpose: a half-done run may have
    pushed a branch or opened a PR, so the decision to retry is
    a human one (/retry).
    """
    rows = conn.execute(
        "SELECT id, brief_path FROM events "
        "WHERE state = 'running' AND claimed_at IS NOT NULL "
        "AND (julianday('now') - julianday(claimed_at)) * 86400 > ?",
        (stale_seconds,),
    ).fetchall()
    out = [{"id": int(r["id"]), "brief_path": r["brief_path"]} for r in rows]
    for o in out:
        set_state(conn, o["id"], "failed", last_error="orphaned")
    return out
