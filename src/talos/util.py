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
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


# ── Claude usage limit ───────────────────────────────────────────────────────
# A run that hits the Claude usage limit parks its brief and writes this
# marker with the time of the next attempt. Until then dispatch starts no
# run; reaping, the reconciler and admission go on. Unlike PAUSED it lifts
# itself: the first pass after that time tries the parked brief again.
USAGE_LIMIT_FILE = "~/.orchestrator/USAGE_LIMIT"


def usage_limit_until() -> datetime | None:
    """When the next run may start, or None if no usage-limit wait is active.

    An expired or unreadable marker counts as no wait: a corrupt file must
    not block every run forever.
    """
    path = Path(USAGE_LIMIT_FILE).expanduser()
    try:
        until = parse_iso(path.read_text().strip())
    except OSError:
        return None
    if until is None or until <= datetime.now(timezone.utc):
        return None
    return until


def set_usage_limit(until: datetime) -> None:
    path = Path(USAGE_LIMIT_FILE).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(until.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") + "\n")


def clear_usage_limit() -> bool:
    """Removes the marker. True if there was one."""
    path = Path(USAGE_LIMIT_FILE).expanduser()
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


# ── Run window ───────────────────────────────────────────────────────────────
# Optional `run_window:` block in config.yaml. Outside it, dispatch starts no
# new work (same per-brief check as PAUSED); reaping, the reconciler and
# admission go on, and the briefs wait in `pending` until the window opens.
# With `finish_open_work` (the default), work that finishes a run already
# started still goes: a parked run's resume and a PR's review-fix round.


@dataclass(frozen=True)
class RunWindow:
    start: dt_time          # inclusive
    end: dt_time            # exclusive; start > end crosses midnight
    tz: ZoneInfo | None     # None = the system's local time
    finish_open_work: bool = True  # resumes and review-fix rounds ignore the window

    def describe(self) -> str:
        zone = self.tz.key if self.tz else "system local time"
        return f"{self.start:%H:%M}–{self.end:%H:%M} {zone}"


def _parse_hhmm(field: str, value) -> dt_time:
    # PyYAML (YAML 1.1) reads an unquoted 19:00 as the sexagesimal int 1140,
    # i.e. minutes since midnight. Accept it instead of failing on a config
    # that looks right.
    if isinstance(value, bool):
        raise ValueError(f"run_window.{field}={value!r} is not a HH:MM time")
    if isinstance(value, int):
        # A real sexagesimal time is >= 60 (0:xx stays a string). A smaller
        # int is someone writing `start: 19`, which would mean 00:19.
        if value < 60:
            raise ValueError(f"run_window.{field}={value!r} is ambiguous; write it as \"HH:MM\"")
        hours, minutes = divmod(value, 60)
    elif isinstance(value, str) and re.fullmatch(r"\d{1,2}:\d{2}", value.strip()):
        hours, minutes = (int(p) for p in value.strip().split(":"))
    else:
        raise ValueError(f"run_window.{field}={value!r} is not a HH:MM time")
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        raise ValueError(f"run_window.{field}={value!r} is out of range (00:00–23:59)")
    return dt_time(hours, minutes)


def parse_run_window(raw) -> RunWindow | None:
    """Parses the `run_window:` config block. None = no window (always on).

    Raises ValueError on a malformed block: load_config turns it into a
    SystemExit, because a typo here would otherwise either block every run
    or silently let runs through at any hour.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"run_window must be a mapping, got {type(raw).__name__}")
    # A misspelled `timezone` would otherwise fall back to the system's
    # local time and shift the window without a word.
    unknown = set(raw) - {"start", "end", "timezone", "finish_open_work"}
    if unknown:
        raise ValueError(f"run_window has unknown key(s): {', '.join(sorted(map(str, unknown)))}")
    for field in ("start", "end"):
        if raw.get(field) is None:
            raise ValueError(f"run_window.{field} is missing")
    start = _parse_hhmm("start", raw["start"])
    end = _parse_hhmm("end", raw["end"])
    if start == end:
        raise ValueError("run_window.start and run_window.end are equal; "
                         "remove the block to run at any hour")
    tz_raw = raw.get("timezone")
    if tz_raw is not None and not isinstance(tz_raw, str):
        raise ValueError(f"run_window.timezone={tz_raw!r} is not an IANA timezone name")
    tz_name = (tz_raw or "").strip()
    tz = None
    if tz_name:
        try:
            tz = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            raise ValueError(f"run_window.timezone={tz_name!r} is not a known IANA timezone") from None
    finish = raw.get("finish_open_work", True)
    if not isinstance(finish, bool):
        raise ValueError(f"run_window.finish_open_work={finish!r} is not true or false")
    return RunWindow(start=start, end=end, tz=tz, finish_open_work=finish)


def local_now(window: RunWindow, now: datetime | None = None) -> datetime:
    """`now` (aware; defaults to the current time) in the window's timezone."""
    now = now or datetime.now(timezone.utc)
    return now.astimezone(window.tz) if window.tz else now.astimezone()


def in_run_window(window: RunWindow | None, now: datetime | None = None) -> bool:
    """True if a run may start at `now`. No window = always True."""
    if window is None:
        return True
    current = local_now(window, now).time()
    if window.start < window.end:
        return window.start <= current < window.end
    return current >= window.start or current < window.end


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
