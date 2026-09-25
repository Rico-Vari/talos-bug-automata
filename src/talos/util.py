#!/usr/bin/env python3
"""
Utilities shared by bot.py, dispatch.py and the event harness.

It lives on its own so webhookd.py does not have to import bot.py (which
pulls in python-telegram-bot) or dispatch.py (which pulls in docker and the
global lock).
"""
from __future__ import annotations

import os
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

# Repo checkout root (src/talos/util.py → two levels up). config.yaml lives
# here unless TALOS_CONFIG points somewhere else.
REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = Path(os.environ.get("TALOS_CONFIG") or REPO_ROOT / "config.yaml").expanduser()

# Global pause sentinel. While it exists, dispatch.py starts no run (it
# checks before every brief). Admission goes on: webhookd and the reconciler
# write the briefs, which wait in `pending` until /resume.
PAUSED_FILE = "~/.orchestrator/PAUSED"


def slugify(title: str) -> str:
    """Turns a title into a slug fit for a branch name and a file name.

    The result is used as `$BRIEF_SLUG` inside the container, so it cannot
    carry anything git rejects in a branch name.
    """
    slug = unicodedata.normalize("NFKD", title)
    slug = slug.encode("ascii", "ignore").decode("ascii")
    slug = slug.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug)
    return slug[:40].strip("-") or "brief"


def utcnow() -> str:
    """ISO-8601 UTC timestamp with a Z suffix. The format the DB uses."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str | None) -> datetime | None:
    """Parses an ISO-8601 timestamp (with Z or an offset) into an aware datetime.

    Returns None if the value is empty or does not parse — callers treat
    None as 'no cutoff', so a misspelled config field never silently blocks
    every event.
    """
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def is_paused() -> bool:
    from pathlib import Path
    return Path(PAUSED_FILE).expanduser().exists()


# The harness posts its reviews with the repo owner's own GH_TOKEN, so in a
# private repo of your own the author of the automated review and of the
# human review are the same account. Filtering by author would keep the
# human's comments (the case stage B exists to handle) from ever getting
# in. That is why the anti-loop guard relies on a marker in the body: the
# agent adds it to everything it posts, and GitHub does not render it.
AUTO_REVIEW_MARKER = "<!-- orquestrator:auto-review -->"


def is_harness_authored(body: str | None) -> bool:
    """True if this text was posted by the harness itself."""
    return AUTO_REVIEW_MARKER in (body or "")
