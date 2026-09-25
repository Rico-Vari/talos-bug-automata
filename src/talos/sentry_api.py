#!/usr/bin/env python3
"""
Sentry REST client and error-context rendering.

It is fetched host-side, not from the container, for three reasons: the
container has neither sentry-cli nor a token; the brief stays self-contained
and auditable in Obsidian before an hour of container time is spent; and it
keeps SENTRY_AUTH_TOKEN out of a container that runs with
--dangerously-skip-permissions.
"""
from __future__ import annotations

import logging

import requests

logger = logging.getLogger("orchestrator")

API_BASE = "https://sentry.io/api/0"

# Truncation bounds. A full Next.js stack trace with node_modules can run to
# hundreds of KB; what helps the diagnosis are the in_app frames.
MAX_FRAMES = 15
MAX_BREADCRUMBS = 20
MAX_CONTEXT_BYTES = 12 * 1024


class SentryError(RuntimeError):
    pass


def _get(path: str, token: str, timeout: int = 20) -> dict:
    resp = requests.get(
        f"{API_BASE}{path}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise SentryError(f"GET {path} → {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def fetch_issue(issue_id: str, token: str) -> dict:
    return _get(f"/issues/{issue_id}/", token)


def fetch_latest_event(issue_id: str, token: str) -> dict:
    return _get(f"/issues/{issue_id}/events/latest/", token)


def post_issue_comment(issue_id: str, token: str, body: str) -> bool:
    """Comments on the Sentry issue. Returns False if the endpoint does not exist.

    It degrades silently on purpose: the PR body already carries the Sentry
    permalink, so the link goes both ways even if this comment fails.
    """
    try:
        resp = requests.post(
            f"{API_BASE}/issues/{issue_id}/comments/",
            headers={"Authorization": f"Bearer {token}"},
            json={"text": body},
            timeout=15,
        )
        if resp.status_code in (200, 201):
            return True
        logger.warning(
            "Comment on Sentry issue %s → %d (degrading to PR-body-only)",
            issue_id, resp.status_code,
        )
    except Exception as e:
        logger.warning("Comment on Sentry issue %s failed: %s", issue_id, e)
    return False


# ── Extraction and rendering ─────────────────────────────────────────────────


def _entries(event: dict, entry_type: str) -> list:
    for entry in event.get("entries") or []:
        if entry.get("type") == entry_type:
            data = entry.get("data") or {}
            return data.get("values") or ([data] if data else [])
    return []


def extract_frames(event: dict) -> list[dict]:
    """Returns the most relevant frames of the stack, in_app first.

    Sentry delivers frames from outermost to innermost; the one that broke
    is the last, so we reverse them so the first thing the agent reads is
    the point of failure.
    """
    for exc in reversed(_entries(event, "exception")):
        frames = ((exc.get("stacktrace") or {}).get("frames")) or []
        if not frames:
            continue
        in_app = [f for f in frames if f.get("inApp")]
        chosen = in_app or frames
        return list(reversed(chosen))[:MAX_FRAMES]
    return []


def extract_breadcrumbs(event: dict) -> list[dict]:
    crumbs = _entries(event, "breadcrumbs")
    return crumbs[-MAX_BREADCRUMBS:]


def event_tags(event: dict) -> dict:
    """Tags as a dict. REST sends them as `[{"key", "value"}]`; webhooks, as `[[k, v]]`."""
    out: dict = {}
    for t in event.get("tags") or []:
        if isinstance(t, dict):
            k, v = t.get("key"), t.get("value")
        elif isinstance(t, (list, tuple)) and len(t) == 2:
            k, v = t
        else:
            continue
        if k:
            out[str(k)] = v
    return out


def _clean_path(path: str) -> str:
    """Strips the leading slash from frame paths.

    inline_file_refs() in dispatch.py scans the body for absolute paths and
    tries to inline the file. A frame like /app/src/foo.ts would trigger that
    check for nothing; without the leading slash, it does not even look.
    """
    return (path or "?").lstrip("/")


def render_context(issue: dict, event: dict | None) -> str:
    """Builds the Markdown error-context block for the brief body."""
    meta = issue.get("metadata") or {}
    tags = event_tags(event or {})
    culprit = issue.get("culprit") or meta.get("filename") or "?"

    lines = [
        "## Error context",
        "",
        "| field | value |",
        "|---|---|",
        f"| culprit | `{culprit}` |",
        f"| type | `{meta.get('type', '?')}` |",
        f"| level / environment | {issue.get('level', '?')} / {tags.get('environment', '?')} |",
        f"| release | {tags.get('release', '?')} |",
        f"| times seen / users | {issue.get('count', '?')} / {issue.get('userCount', '?')} |",
        f"| first / last seen | {issue.get('firstSeen', '?')} / {issue.get('lastSeen', '?')} |",
        f"| url | {tags.get('url', '?')} |",
        "",
    ]

    frames = extract_frames(event or {})
    if frames:
        lines += ["## Stack trace (most relevant frames first)", "", "```"]
        for f in frames:
            loc = f"{_clean_path(f.get('filename') or f.get('absPath'))}"
            fn = f.get("function") or "?"
            ln = f.get("lineNo")
            marker = "*" if f.get("inApp") else " "
            lines.append(f"{marker} {loc}:{ln} in {fn}")
            ctx = f.get("context") or []
            for num, text in ctx[:5]:
                lines.append(f"      {num}| {text}")
        lines += ["```", "", "`*` = application frame (in_app).", ""]

    crumbs = extract_breadcrumbs(event or {})
    if crumbs:
        lines += [f"## Breadcrumbs (last {len(crumbs)})", "", "```"]
        for c in crumbs:
            lines.append(
                f"{c.get('timestamp', '?')} [{c.get('category', '?')}/{c.get('level', '?')}] "
                f"{str(c.get('message') or c.get('data') or '')[:200]}"
            )
        lines += ["```", ""]

    text = "\n".join(lines)
    if len(text.encode()) > MAX_CONTEXT_BYTES:
        text = text.encode()[:MAX_CONTEXT_BYTES].decode("utf-8", "ignore")
        text += "\n```\n\n> (context truncated for size)\n"
    return text


def build_context(issue_id: str, token: str) -> tuple[dict, str]:
    """Fetches the issue + latest event and returns (issue, Markdown context).

    If the event cannot be fetched, it goes on with the issue's data: a
    brief with partial context is far more useful than none.
    """
    issue = fetch_issue(issue_id, token)
    try:
        event = fetch_latest_event(issue_id, token)
    except Exception as e:
        logger.warning("Could not fetch the latest event of %s: %s", issue_id, e)
        event = None
    return issue, render_context(issue, event)
