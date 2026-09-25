#!/usr/bin/env python3
"""
Admission and dispatch gates.

Two families, split by *where the truth lives*:

- Admission gates: decided with the payload in hand, in webhookd, before
  writing a brief. Pure and testable with fixtures.
- Dispatch gates: depend on the world and on time (how many PRs are
  open, how long since the last run), so they cannot be decided at
  webhook time.

A rejected event is still written to the ledger as `rejected_<gate>`: a
filter you cannot audit is one you switch off within a week.
"""
from __future__ import annotations

import logging
import re
import subprocess
from datetime import datetime, timedelta, timezone

from talos import util
from talos.util import parse_iso

logger = logging.getLogger("orchestrator")

# GitHub events we care about, and with which actions.
GH_EVENT_ACTIONS = {
    # `unlabeled` is here so that removing `ai-skip` actually has an
    # effect. Without it, an issue held back by the opt-out stayed held back
    # forever: the reconciler skips everything that already has a row, and GitHub
    # never sends `opened` again. `labeled` is in for symmetry — putting
    # `priority:high` on an issue a gate held back and that now passes must
    # also be able to admit it. What keeps this from generating work
    # twice is `_record`: an event that already had a brief does not get another.
    "issues": {"opened", "reopened", "labeled", "unlabeled"},
    "pull_request_review": {"submitted"},
    "pull_request_review_comment": {"created"},
    "pull_request": {"closed"},
}

# Sentry resources we accept. Only `event_alert`: the threshold ("seen
# more than N times", environment, level) is decided by the Sentry alert rule, which
# is what has the real count. `issue.created` arrives with count≈1 and Sentry does not
# resend it when the issue grows, so a threshold of our own on that resource
# rejected everything that mattered.
SENTRY_RESOURCES = {"event_alert"}

# Tolerance window for the Sentry timestamp, against payload replay.
SENTRY_MAX_SKEW_SECONDS = 300


class Verdict:
    """Result of the gate ladder."""

    def __init__(self, admitted: bool, gate: str = "", detail: str = ""):
        self.admitted = admitted
        self.gate = gate
        self.detail = detail

    @property
    def state(self) -> str:
        return "admitted" if self.admitted else f"rejected_{self.gate}"

    def __repr__(self) -> str:
        return f"<Verdict {self.state}{': ' + self.detail if self.detail else ''}>"


OK = Verdict(True)


def _reject(gate: str, detail: str = "") -> Verdict:
    return Verdict(False, gate, detail)


# ── Admission gates ──────────────────────────────────────────────────────────


def gate_github_event(event: str, payload: dict) -> Verdict:
    """Filter by type and action, and drop issues that are PRs.

    GitHub sends PR comments through the `issues` event with a
    `pull_request` key inside; treating them as issues would open a PR to fix
    a PR.
    """
    action = payload.get("action", "")
    wanted = GH_EVENT_ACTIONS.get(event)
    if wanted is None:
        return _reject("event_type", f"event '{event}' not of interest")
    if action not in wanted:
        return _reject("event_action", f"{event}.{action} not of interest")
    if event == "issues" and "pull_request" in (payload.get("issue") or {}):
        return _reject("is_pull_request", "the 'issue' is actually a PR")
    if event == "pull_request" and not (payload.get("pull_request") or {}).get("merged"):
        return _reject("pr_not_merged", "PR closed without merge")
    return OK


def gate_skip_label(repo_cfg: dict, issue: dict) -> Verdict:
    """Opt-out: the skip label takes the issue out of the harness.

    It is opt-out and not opt-in on purpose — every issue gets in by default.
    """
    skip = repo_cfg.get("skip_label", "ai-skip")
    if not skip:
        return OK
    labels = {
        (lbl.get("name") if isinstance(lbl, dict) else str(lbl)) or ""
        for lbl in (issue.get("labels") or [])
    }
    if skip in labels:
        return _reject("optout", f"has the label '{skip}'")
    return OK


# Labels the harness puts on an issue once it worked on it (see
# feedback.HARNESS_LABELS). Kept literal here so triage does not import egress.
HANDLED_LABELS = ("ai-in-review", "ai-merged-dev", "ai-released")


def gate_handled_labels(issue: dict) -> Verdict:
    """Second re-entry lock: the harness already worked on this issue.

    The first one is the row in events.db. This one survives losing the DB
    and somebody reopening an issue that was already released.
    """
    labels = {
        (lbl.get("name") if isinstance(lbl, dict) else str(lbl)) or ""
        for lbl in (issue.get("labels") or [])
    }
    found = [lbl for lbl in HANDLED_LABELS if lbl in labels]
    if found:
        return _reject("already_handled", f"has the label '{found[0]}'")
    return OK


def gate_backlog_cutoff(repo_cfg: dict, issue: dict) -> Verdict:
    """Only issues created after activated_at get in.

    Without this cutoff, turning the harness on in a repo with a backlog admits every
    open issue at once and the serialized queue grows to days.
    """
    cutoff = parse_iso(repo_cfg.get("activated_at"))
    if cutoff is None:
        return _reject("no_cutoff", "repos[*].activated_at is not set or does not parse")
    created = parse_iso(issue.get("created_at"))
    if created is None:
        return _reject("no_created_at", "the issue has no parseable created_at")
    if created <= cutoff:
        return _reject("backlog", f"created {created.isoformat()} <= cutoff {cutoff.isoformat()}")
    return OK


def gate_author(repo_cfg: dict, actor: dict, self_login: str = "") -> Verdict:
    """Guard against the self-trigger loop and against untrusted authors.

    The review the bot publishes comes back as pull_request_review; without
    this filter, the harness triggers itself indefinitely.
    """
    login = (actor.get("login") or "").strip()
    if self_login and login.lower() == self_login.lower():
        return _reject("self_authored", f"@{login} is the harness's own account")
    if (actor.get("type") or "").lower() == "bot":
        allow = repo_cfg.get("authors_allow") or []
        if login not in allow:
            return _reject("bot_author", f"@{login} is a bot and is not in authors_allow")
    allow = repo_cfg.get("authors_allow") or []
    if allow and login not in allow:
        return _reject("author_not_allowed", f"@{login} is not in authors_allow")
    return OK


def gate_pr_label(repo_cfg: dict, pr: dict) -> Verdict:
    """Only PRs the harness opened. Same criterion as the reconciler.

    Without this, a review on a teammate's PR triggers the fixer: it does
    `gh pr checkout` of their branch, pushes commits to it and resolves their threads.
    """
    label = repo_cfg.get("pr_label", "ai-generated")
    names = {
        (lbl.get("name") if isinstance(lbl, dict) else str(lbl)) or ""
        for lbl in (pr.get("labels") or [])
    }
    if label not in names:
        return _reject("not_harness_pr", f"the PR does not have the label '{label}'")
    return OK


def gate_review_body(body: str | None, review_state: str = "") -> Verdict:
    """A review without text asks for nothing.

    It is also the other half of the anti-loop: every inline thread the
    harness opens generates a `pull_request_review.submitted` with an empty body, which
    does not carry the marker. The thread's text arrives through its own
    `pull_request_review_comment` event, and that one does carry it.
    """
    if not (body or "").strip():
        return _reject("empty_body", "review without text")
    if (review_state or "").lower() == "approved":
        return _reject("approved", "an approve does not ask for changes")
    return OK


def gate_self_review(body: str | None) -> Verdict:
    """Anti-loop guard for review events, by marker and not by author.

    The review the harness publishes comes back as `pull_request_review`; without
    this filter it would trigger itself indefinitely. The author cannot be
    used: the harness publishes with the repo owner's token, so in a
    private repo of your own the human and the harness are the same account.
    """
    if util.is_harness_authored(body):
        return _reject("self_review", "carries the automated review marker")
    return OK


def gate_association(issue: dict) -> Verdict:
    """Reject authors with no relationship to the repo.

    On private repos this never fires, but if one goes
    public it is the only thing standing between a stranger and a container with
    broad permissions.
    """
    assoc = (issue.get("author_association") or "").upper()
    if assoc in {"NONE", "MANNEQUIN"}:
        return _reject("author_association", f"author_association={assoc}")
    return OK


def parse_sentry_timestamp(value: str | None) -> datetime | None:
    """`Sentry-Hook-Timestamp` is Unix epoch seconds, not ISO-8601.

    With plain `parse_iso` it returned None and the skew check never
    ran: the replay protection was off without anyone noticing.
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromtimestamp(float(text), tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return parse_iso(text)


def gate_sentry_resource(resource: str, timestamp: str) -> Verdict:
    if resource not in SENTRY_RESOURCES:
        return _reject("resource", f"resource '{resource}' not of interest")
    ts = parse_sentry_timestamp(timestamp)
    if ts is None:
        return _reject("no_timestamp", "Sentry-Hook-Timestamp is missing or does not parse")
    skew = abs((datetime.now(timezone.utc) - ts).total_seconds())
    if skew > SENTRY_MAX_SKEW_SECONDS:
        return _reject("stale_timestamp", f"{int(skew)}s of skew")
    return OK


def gate_sentry_issue(sentry_cfg: dict, issue: dict, tags: dict | None = None) -> Verdict:
    """Severity and noise filters on a Sentry issue.

    There is no times-seen threshold: the alert rule decides that (see
    SENTRY_RESOURCES). The `event_alert` payload does not even carry `count`.
    """
    tags = tags or {}

    if (issue.get("status") or "unresolved") != "unresolved":
        return _reject("not_unresolved", f"status={issue.get('status')}")

    levels = {"debug": 0, "info": 1, "warning": 2, "error": 3, "fatal": 4}
    min_level = str(sentry_cfg.get("min_level", "error")).lower()
    level = str(issue.get("level", "error")).lower()
    if levels.get(level, 3) < levels.get(min_level, 3):
        return _reject("level", f"level={level} < {min_level}")

    envs = sentry_cfg.get("environments") or []
    env = tags.get("environment") or issue.get("environment") or ""
    if envs and env and env not in envs:
        return _reject("environment", f"environment={env} is not in {envs}")

    denylist = sentry_cfg.get("title_denylist") or []
    title = issue.get("title") or ""
    for pattern in denylist:
        if re.search(pattern, title, re.I):
            return _reject("denylist", f"the title matches /{pattern}/")

    return OK


# ── Dispatch gates ───────────────────────────────────────────────────────────


def _gh_json(args: list[str], timeout: int = 20):
    """Run gh and return the JSON, or None if it fails.

    A gh failure must not admit work by default: callers treat
    None as 'cannot verify' and defer.
    """
    try:
        result = subprocess.run(
            ["gh", *args], capture_output=True, text=True, timeout=timeout
        )
        if result.returncode != 0:
            logger.warning("gh %s → exit %d: %s", " ".join(args), result.returncode, result.stderr[:200])
            return None
        import json as _json
        return _json.loads(result.stdout or "null")
    except Exception as e:
        logger.warning("gh %s failed: %s", " ".join(args), e)
        return None


def gate_open_auto_prs(gh_repo: str, repo_cfg: dict) -> Verdict:
    """The real anti-flood valve: it clears itself when you merge.

    With no label gate on issues, this cap is the primary load-control
    mechanism, not a safety net.
    """
    cap = int(repo_cfg.get("max_open_auto_prs", 3))
    label = repo_cfg.get("pr_label", "ai-generated")
    prs = _gh_json([
        "pr", "list", "--repo", gh_repo, "--label", label,
        "--state", "open", "--json", "number", "--limit", "100",
    ])
    if prs is None:
        return _reject("gh_unavailable", "could not list the open PRs")
    if len(prs) >= cap:
        return _reject("open_pr_cap", f"{len(prs)} automated PRs open (cap {cap})")
    return OK


def gate_cooldown(repo_cfg: dict, last_dispatch_at: str | None) -> Verdict:
    minutes = int(repo_cfg.get("cooldown_minutes", 0))
    if not minutes or not last_dispatch_at:
        return OK
    last = parse_iso(last_dispatch_at)
    if last is None:
        return OK
    elapsed = datetime.now(timezone.utc) - last
    if elapsed < timedelta(minutes=minutes):
        remaining = int((timedelta(minutes=minutes) - elapsed).total_seconds() / 60)
        return _reject("cooldown", f"~{remaining} min left")
    return OK


def gate_daily_cap(repo_cfg: dict, cfg: dict, repo_today: int, global_today: int) -> Verdict:
    repo_cap = int(repo_cfg.get("daily_cap", 3))
    if repo_today >= repo_cap:
        return _reject("daily_cap", f"{repo_today} today in this repo (cap {repo_cap})")
    global_cap = int(cfg.get("global_daily_cap", 10))
    if global_today >= global_cap:
        return _reject("global_daily_cap", f"{global_today} today in total (cap {global_cap})")
    return OK


def check_dispatch_caps(cfg: dict, gh_repo: str, conn) -> Verdict:
    """The load caps, evaluated at dispatch time.

    They live here and not in admission because they depend on the world at
    run time: a brief that hits the cap today is not rejected, it stays
    `pending` and runs when the cap frees up — when a PR is merged or closed, or
    the next day. That way the backlog drains on its own and nothing is lost.

    From cheapest to most expensive: the open-PR one is the only one that hits
    GitHub.
    """
    from talos import store

    repo_cfg = (cfg.get("repos") or {}).get(gh_repo) or {}
    for verdict in (
        gate_daily_cap(
            repo_cfg, cfg,
            store.count_dispatched_today(conn, gh_repo),
            store.count_dispatched_today(conn),
        ),
        gate_cooldown(repo_cfg, store.last_dispatch_at(conn, gh_repo)),
    ):
        if not verdict.admitted:
            return verdict
    return gate_open_auto_prs(gh_repo, repo_cfg)


def self_login() -> str:
    """Login of the token's account. Cached: it does not change during the process."""
    global _SELF_LOGIN
    if _SELF_LOGIN is None:
        try:
            result = subprocess.run(
                ["gh", "api", "user", "--jq", ".login"],
                capture_output=True, text=True, timeout=10,
            )
            _SELF_LOGIN = result.stdout.strip() if result.returncode == 0 else ""
        except Exception:
            _SELF_LOGIN = ""
    return _SELF_LOGIN


_SELF_LOGIN: str | None = None
