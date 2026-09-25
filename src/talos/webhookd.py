#!/usr/bin/env python3
"""
Webhook ingress for the harness (GitHub + Sentry).

Binds to loopback: the tunnel is the only thing that reaches it, so there is
no second auth surface to get wrong. Each handler does four things — read the
raw bytes, verify the HMAC, write to the ledger, answer 202 — and nothing
else. It never runs Docker inline: GitHub gives up after 10 seconds.

  POST /gh       X-GitHub-Event, X-GitHub-Delivery, X-Hub-Signature-256
  POST /sentry   Sentry-Hook-Resource, Sentry-Hook-Timestamp, Sentry-Hook-Signature
  GET  /healthz  200, no auth
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

from talos import brief_factory
from talos import secrets_env
from talos import feedback
from talos import store
from talos import triage
from talos.routing import (
    resolve_gh_repo,
    resolve_sentry_project,
    resolve_service,
    validate_routing,
)
from talos.util import CONFIG_PATH, is_paused, utcnow

logger = logging.getLogger("orchestrator")

MAX_BODY_BYTES = 1024 * 1024  # 1 MiB; a bigger payload is a bug or abuse

CFG: dict = {}
SELF_LOGIN = ""
_LOCK = threading.Lock()


# ── Signature verification ───────────────────────────────────────────────────


def verify_github(body: bytes, header: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header or "")


def verify_sentry(body: bytes, header: str, secret: str) -> bool:
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header or "")


# ── Kick a dispatch ──────────────────────────────────────────────────────────


def kick_dispatch() -> None:
    """Requests a dispatch pass without blocking.

    dispatch.py's flock makes a concurrent pass safe: if one is already
    running, the second returns 0 instead of competing. The systemd timer
    stays as a safety net.
    """
    try:
        subprocess.run(
            ["systemctl", "--user", "start", "--no-block", "talos-dispatch.service"],
            capture_output=True, timeout=10,
        )
    except Exception as e:
        logger.warning("Could not kick dispatch (the timer will pick it up anyway): %s", e)


# ── Event processing ─────────────────────────────────────────────────────────

# States of a PR row with a review-fix round in flight.
IN_FLIGHT_STATES = ("admitted", "dispatched", "running")


def _reject_delivery(delivery_id: str, source: str, resource: str, action: str,
                     raw_path: str, verdict, ref: str = "") -> dict:
    """A rejection before there is an event row: it is audited in deliveries.

    These are the easiest rejections to lose track of — event type, unmapped
    repo, the anti-loop marker, someone else's PR — and without this they left
    no trace anywhere. `python tools/events.py --deliveries` shows them.
    """
    with _LOCK, store.connect() as conn:
        store.record_delivery(conn, delivery_id, source, resource, action, raw_path,
                              verdict=verdict.state)
    logger.info("%s %s.%s%s → %s (%s)", source, resource, action,
                f" {ref}" if ref else "", verdict.state, verdict.detail)
    return {"state": verdict.state, "detail": verdict.detail}


def _retire_draft(row) -> None:
    """Moves out of ToDos/ the dry-run draft of a row that is now going live.

    It is not deleted: it stays in `projects/{project}/dry-run/` as a record of
    what the harness would have done, which is exactly what the dry-run week
    measures.
    """
    path = Path(str(row["brief_path"] or ""))
    if not path.name or not path.is_file():
        return
    try:
        import frontmatter

        post = frontmatter.load(path)
        post.metadata["status"] = "superseded"
        post.metadata["superseded_at"] = utcnow()
        path.write_text(frontmatter.dumps(post))
        dest = Path(CFG["vault"]) / "projects" / str(row["project"] or "_") / "dry-run"
        dest.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(dest / path.name))
    except Exception as e:
        logger.warning("Could not retire the draft %s: %s", path, e)


def _record(dedupe_key: str, fields: dict, verdict, delivery_id: str, source: str,
            resource: str, action: str, raw_path: str,
            live: bool = False) -> tuple[int | None, bool]:
    """Writes delivery + event with the verdict.

    Returns `(event_id, write_brief)`. The second value is NOT "the row is
    new": a row that exists but never got a brief — because a gate ate it —
    has to be able to produce one once the condition that stopped it goes
    away. With the previous version ("is_new") an issue rejected for backlog
    or for `ai-skip` was doomed forever, even after a /backfill.

    `live` says whether the repo is already out of dry run: a row that came in
    during dry run has a brief (a draft), but that brief never ran, so it is
    admitted again when the repo goes live.
    """
    with _LOCK, store.connect() as conn:
        fresh = store.record_delivery(conn, delivery_id, source, resource, action, raw_path,
                                      verdict=verdict.state)
        if not fresh:
            logger.info("Delivery %s already processed — ignored", delivery_id)
            return None, False
        fields = {**fields, "state": verdict.state}
        event_id, is_new = store.upsert_event(conn, dedupe_key, fields)
        conn.execute(
            "UPDATE deliveries SET event_id = ? WHERE delivery_id = ?",
            (event_id, delivery_id),
        )
        if is_new:
            return event_id, True
        existing = store.get_event(conn, event_id)
        state = existing["state"] if existing else ""
        if state == "dry_run":
            if live and verdict.admitted:
                logger.info("%s came in during dry run and the repo is now live — admitting", dedupe_key)
                _retire_draft(existing)
                store.set_state(conn, event_id, verdict.state, brief_path=None)
                return event_id, True
            return event_id, False
        if state in store.ACTIVE_STATES:
            logger.info(
                "%s is already in '%s' (seen %d times) — not generating another brief",
                dedupe_key, state, existing["seen_count"],
            )
            return event_id, False
        if existing and existing["brief_path"]:
            # There already was a brief and the run ended badly (failed/aborted).
            # Reviving it is a human decision via /retry: the half-done run
            # may have pushed a branch.
            logger.info(
                "%s already had a brief (state '%s') — use /retry, not re-queueing it",
                dedupe_key, state,
            )
            return event_id, False
        # Row without a brief: a gate stopped it. If it passes now, this is the
        # moment to give it its brief.
        if verdict.admitted:
            logger.info(
                "%s had a row in '%s' without a brief and now passes the gates — admitting",
                dedupe_key, state or "?",
            )
            store.set_state(conn, event_id, verdict.state)
            return event_id, True
        return event_id, False


def _emit_brief(cfg: dict, target, event_id: int, build, pipeline: str,
                delivery_id: str = "", rollback: dict | None = None,
                kick: bool = True) -> dict:
    """Writes the brief and only then marks the row ready for dispatch.

    If writing fails, the delivery is undone. Without that, GitHub's retry
    counted as a duplicate and the row stayed `admitted` with no brief: active
    for dedupe, invisible to dispatch, and with nothing /retry could revive.
    `brief_error` is not an active state, so the retry (or the reconciler's
    next pass) tries again.
    """
    try:
        filename, content = build()
        path = brief_factory.write_brief(cfg, filename, content)
    except Exception as e:
        with _LOCK, store.connect() as conn:
            if delivery_id:
                store.forget_delivery(conn, delivery_id)
            store.set_state(conn, event_id, "brief_error",
                            last_error=f"brief: {e}"[:500], **(rollback or {}))
        logger.exception("Could not write the brief for event %s", event_id)
        raise
    # In dry run the row gets its own state, outside ACTIVE_STATES: that way
    # going live can re-admit it instead of seeing it as a duplicate.
    state = "dry_run" if target.dry_run else "admitted"
    with _LOCK, store.connect() as conn:
        store.set_state(conn, event_id, state, brief_path=str(path),
                        pipeline=pipeline, last_error=None)
    if kick and not target.dry_run:
        kick_dispatch()
    return {"state": state, "event_id": event_id, "brief": str(path),
            "dry_run": target.dry_run}


def handle_github(event: str, delivery_id: str, payload: dict, raw_path: str) -> dict:
    action = payload.get("action", "")
    repo_full = ((payload.get("repository") or {}).get("full_name")) or ""

    verdict = triage.gate_github_event(event, payload)
    if not verdict.admitted:
        return _reject_delivery(delivery_id, "github", event, action, raw_path,
                                verdict, repo_full)

    target = resolve_gh_repo(CFG, repo_full)
    if target is None or not target.enabled:
        detail = "repo not mapped" if target is None else "repo mapped but enabled: false"
        return _reject_delivery(delivery_id, "github", event, action, raw_path,
                                triage.Verdict(False, "unmapped", detail), repo_full)

    if event == "issues":
        return _handle_gh_issue(target, delivery_id, payload, raw_path, action)
    if event in ("pull_request_review", "pull_request_review_comment"):
        return _handle_gh_review(target, delivery_id, payload, raw_path, event, action)
    if event == "pull_request":
        return _handle_gh_merged(target, delivery_id, payload, raw_path, action)
    return {"state": "rejected_event_type", "detail": event}


def _handle_gh_issue(target, delivery_id, payload, raw_path, action,
                     skip_gates: frozenset = frozenset()) -> dict:
    issue = payload.get("issue") or {}
    number = issue.get("number")
    dedupe_key = f"gh:{target.gh_repo}#{number}"

    # `skip_gates` exists for /backfill: pulling in a backlog issue by hand is
    # an explicit human decision, so it has to be able to skip the
    # `activated_at` cutoff without duplicating the gate ladder here.
    gates = [
        triage.gate_skip_label(target.cfg, issue),
        triage.gate_handled_labels(issue),
        triage.gate_backlog_cutoff(target.cfg, issue),
        triage.gate_association(issue),
        # No SELF_LOGIN on purpose: the anti-loop guard exists so the review
        # the harness posts does not trigger it again, and that happens in
        # review events. Applying it here would mean an issue opened by the
        # token owner — the normal case — never gets in.
        triage.gate_author(target.cfg, issue.get("user") or {}),
    ]
    for gate in gates:
        if not gate.admitted:
            if gate.state.replace("rejected_", "") in skip_gates:
                logger.info("Gate %s skipped on request for #%s", gate.state, number)
                continue
            _record(dedupe_key, _issue_fields(target, issue), gate,
                    delivery_id, "github", "issues", action, raw_path)
            logger.info("GH issue #%s → %s (%s)", number, gate.state, gate.detail)
            return {"state": gate.state, "detail": gate.detail}

    # Pause is NOT checked here: pausing stops dispatch, not admission. If it
    # stopped admission, whatever came in during the pause would be lost (the
    # reconciler skips known rows) and /resume would not bring it back.
    event_id, write = _record(
        dedupe_key, _issue_fields(target, issue), triage.OK,
        delivery_id, "github", "issues", action, raw_path, live=not target.dry_run,
    )
    if event_id is None or not write:
        return {"state": "duplicate", "detail": dedupe_key}

    return _emit_brief(
        CFG, target, event_id,
        lambda: brief_factory.build_github_brief(
            CFG, target, issue, event_id, dedupe_key, created_by=_origin(payload)
        ),
        pipeline="issue-fix", delivery_id=delivery_id,
    )


def _origin(payload: dict) -> str:
    """Who brought this event in: the webhook or the reconciliation pass.

    It goes into the brief's front matter. A brief that says `webhookd` when
    the reconciler actually brought it turns the audit trail into a guess,
    and the trail is the reason briefs exist.
    """
    return "reconcile" if payload.get("_synthetic") else "webhookd"


def _issue_fields(target, issue: dict) -> dict:
    return {
        "source": "github",
        "project": target.project,
        "subrepo": target.subrepo,
        "service_path": target.service_path,
        "gh_repo": target.gh_repo,
        "gh_issue": issue.get("number"),
        "title": (issue.get("title") or "")[:300],
        "pipeline": "issue-fix",
    }


def _handle_gh_review(target, delivery_id, payload, raw_path, event, action) -> dict:
    pr = payload.get("pull_request") or {}
    number = pr.get("number")
    actor = payload.get("sender") or {}
    review = payload.get("review") or {}
    comment = payload.get("comment") or {}
    body = (review if event == "pull_request_review" else comment).get("body") or ""

    # Anti-loop goes by the marker in the body, not by author: the harness
    # posts with the repo owner's token, so filtering by author would reject
    # the human's comments — exactly what stage B has to handle. `gate_author`
    # still runs, but only for bots and authors_allow (no self_login).
    for gate in (
        triage.gate_pr_label(target.cfg, pr),
        triage.gate_self_review(body),
        triage.gate_review_body(body, review.get("state", "")),
        triage.gate_author(target.cfg, actor),
    ):
        if not gate.admitted:
            return _reject_delivery(delivery_id, "github", event, action, raw_path,
                                    gate, f"PR #{number}")

    return open_review_round(
        CFG, target,
        number=number,
        pr_url=pr.get("html_url", ""),
        title=pr.get("title") or "",
        head_ref=((pr.get("head") or {}).get("ref")) or "",
        trigger=f"{event} from @{actor.get('login', '?')}",
        trigger_at=comment.get("created_at") or review.get("submitted_at") or "",
        origin=_origin(payload),
        delivery=(delivery_id, event, action, raw_path),
    )


def open_review_round(cfg: dict, target, *, number: int, pr_url: str, title: str,
                      head_ref: str, trigger: str, trigger_at: str, origin: str,
                      delivery: tuple | None = None, kick: bool = True) -> dict:
    """Opens a review-fix round on a harness PR.

    A single door for the webhook, the reconciler and dispatch (which opens
    round 1 when stage A's automatic review left `patch`): that way the round
    cap and the in-flight round guard cannot be forgotten in any of the three.

    `delivery` is `(delivery_id, event, action, raw_path)` when the round
    comes from a webhook delivery.
    """
    dedupe_key = f"pr:{target.gh_repo}#{number}"
    delivery_id = ""
    with _LOCK, store.connect() as conn:
        if delivery:
            delivery_id, event, action, raw_path = delivery
            if not store.record_delivery(conn, delivery_id, "github", event, action, raw_path):
                return {"state": "duplicate", "detail": delivery_id}
        row = store.find_by_dedupe(conn, dedupe_key)

        if row is not None and row["state"] in IN_FLIGHT_STATES:
            # A GitHub review arrives as a burst: one
            # `pull_request_review.submitted` plus one
            # `pull_request_review_comment.created` per inline comment, each
            # with its own delivery id. Counting a round per delivery used up
            # the cap with a single review and queued parallel fixers on the
            # same branch. The in-flight round will read this comment from
            # GitHub if it has not started yet, or if the comment predates its
            # start; in those cases the mark moves forward so the reconciler
            # does not open another round for it. If it comes later, the mark
            # is left alone and the reconciler picks it up when the round ends.
            absorbed = row["state"] != "running" or (
                bool(trigger_at) and bool(row["claimed_at"])
                and trigger_at <= row["claimed_at"]
            )
            if absorbed and trigger_at > (row["last_comment_at"] or ""):
                store.annotate(conn, int(row["id"]), last_comment_at=trigger_at)
            if delivery_id:
                store.set_delivery_verdict(conn, delivery_id, "in_flight", int(row["id"]))
            logger.info("PR #%s already has round %s in '%s' — not opening another",
                        number, row["rounds"], row["state"])
            return {"state": "in_flight", "event_id": int(row["id"]),
                    "detail": f"round {row['rounds']} in '{row['state']}'"}

        if row is not None and row["state"] == "dry_run" and target.dry_run:
            if delivery_id:
                store.set_delivery_verdict(conn, delivery_id, "duplicate", int(row["id"]))
            return {"state": "duplicate", "detail": "there is already a dry-run draft for this PR"}

        rounds = int(row["rounds"]) if row else 0
        cap = int(target.cfg.get("max_review_fix_rounds", 3))
        if rounds >= cap:
            if row:
                store.set_state(conn, int(row["id"]), "rejected_round_cap")
            if delivery_id:
                store.set_delivery_verdict(conn, delivery_id, "rejected_round_cap",
                                           int(row["id"]) if row else None)
            logger.warning("PR #%s hit the cap of %d rounds — needs a human", number, cap)
            return {"state": "rejected_round_cap", "detail": f"{rounds} rounds (cap {cap})"}

        fields = {
            "source": "github", "project": target.project, "subrepo": target.subrepo,
            "service_path": target.service_path, "gh_repo": target.gh_repo,
            "pr_number": number, "pr_url": pr_url,
            "pipeline": "review-fix", "state": "admitted",
            "title": (title or "")[:300],
        }
        event_id, _ = store.upsert_event(conn, dedupe_key, fields)
        if delivery_id:
            store.set_delivery_verdict(conn, delivery_id, "admitted", event_id)
        # The branch is the thread back to the stage A brief (see
        # `find_issue_row_by_branch`); it is kept as `parent_brief` so the
        # "they asked for this three times" trail actually exists.
        parent = store.find_issue_row_by_branch(conn, target.gh_repo, head_ref)
        parent_brief = str(parent["brief_path"]) if parent else ""
        round_n = rounds + 1
        # Storing the timestamp of the comment that triggered the round is what
        # keeps the PR reconciler from firing again for the same comment: both
        # paths compare against this mark. Never backwards: a mark that moves
        # back reopens comments already handled.
        mark = max(trigger_at or "", (row["last_comment_at"] if row else "") or "")
        # Dry run does not spend a round: the draft never runs.
        store.set_state(conn, event_id, "admitted",
                        rounds=rounds if target.dry_run else round_n,
                        pipeline="review-fix", pr_number=number, pr_url=pr_url,
                        last_comment_at=mark or None)

    result = _emit_brief(
        cfg, target, event_id,
        lambda: brief_factory.build_review_fix_brief(
            cfg, target, number, pr_url, event_id, dedupe_key, round_n,
            parent_brief=parent_brief, trigger=trigger, created_by=origin,
        ),
        pipeline="review-fix", delivery_id=delivery_id,
        rollback={"rounds": rounds}, kick=kick,
    )
    return {**result, "round": round_n}


def _handle_gh_merged(target, delivery_id, payload, raw_path, action) -> dict:
    """PR merged: closes the whole cycle and moves the briefs to completed/.

    Closes BOTH rows: the issue's (stage A, reached through the branch) and
    the PR's (stage B, which only exists if there were rounds). It used to
    close one or the other, and a PR with a review-fix round left the issue's
    brief in in-review/ forever.

    A merge into the integration branch does not close the issue: GitHub only
    honors `Closes #N` on the default branch. The stage A row goes to
    `merged_dev` with the merge commit, and reconcile.release_merged closes
    the issue once that commit shows up in the prod branch.

    The same happens, without the comment, when the base is a prod branch
    that is not the default (git-flow): GitHub will not close the issue, so
    the release step does it on its next pass, with the compare as evidence.
    """
    pr = payload.get("pull_request") or {}
    number = pr.get("number")
    gate = triage.gate_pr_label(target.cfg, pr)
    if not gate.admitted:
        return _reject_delivery(delivery_id, "github", "pull_request", action, raw_path,
                                gate, f"PR #{number}")
    head = ((pr.get("head") or {}).get("ref")) or ""
    base = ((pr.get("base") or {}).get("ref")) or ""
    merge_sha = pr.get("merge_commit_sha") or ""
    # Without a base (a legacy payload) the old behavior stays: `done`. The
    # retroactive migration in reconcile parks those rows if they belong in
    # `merged_dev`. An unknown prod branch counts as "not prod": waiting is
    # safe, closing without evidence is not.
    prod = feedback.prod_branch(target.gh_repo, target.cfg) if base else None
    to_prod = not base or (prod is not None and base == prod)
    # Unknown default branch counts as "GitHub will not close it": the release
    # step then checks the issue state before touching it.
    github_closes = bool(base) and base == feedback.default_branch(target.gh_repo)
    park = bool(base) and not (to_prod and github_closes)

    with _LOCK, store.connect() as conn:
        if not store.record_delivery(conn, delivery_id, "github", "pull_request", action,
                                     raw_path, verdict="done"):
            return {"state": "duplicate", "detail": delivery_id}
        found = [
            r for r in (
                store.find_issue_row_by_branch(conn, target.gh_repo, head),
                store.find_by_dedupe(conn, f"pr:{target.gh_repo}#{number}"),
            ) if r is not None
        ]
        # A row that already closed its cycle is not touched again: replaying
        # the merge would turn `merged_dev` back into `done` and comment twice.
        # Compare-and-set, because the reconciler's migration can park the
        # same row from another process (the bot's /reconcile).
        rows = []
        parked = None
        for r in found:
            if r["state"] in store.TERMINAL_STATES:
                continue
            if park and store.is_stage_a_issue(r):
                if store.transition(conn, int(r["id"]), r["state"], "merged_dev",
                                    merge_commit_sha=merge_sha or None,
                                    pr_number=number or r["pr_number"]):
                    rows.append(r)
                    parked = r
            elif store.is_stage_a_issue(r) and merge_sha:
                # The sha also tells the retroactive migration this row was
                # already classified, so it does not ask GitHub about it again.
                if store.transition(conn, int(r["id"]), r["state"], "done",
                                    merge_commit_sha=merge_sha):
                    rows.append(r)
            elif store.transition(conn, int(r["id"]), r["state"], "done"):
                rows.append(r)

    issue_row = next((r for r in rows if store.is_stage_a_issue(r)), None)
    if parked is not None:
        # No "it stays open" comment when it would be false: GitHub already
        # closed the issue (base is the default), or the base is prod itself.
        feedback.announce_merged_dev(target.gh_repo, parked["gh_issue"], base, number, prod,
                                     comment=not to_prod and not github_closes)
    elif issue_row is not None and base:
        feedback.label_issue(target.gh_repo, issue_row["gh_issue"],
                             add="ai-released", remove="ai-in-review")

    moved = []
    for i, r in enumerate(rows):
        new_path = feedback.close_merged(CFG, r, pr.get("html_url", ""), notify=(i == 0),
                                         base=base, prod=prod, parked=parked is not None)
        if new_path:
            moved.append(new_path)
            with _LOCK, store.connect() as conn:
                store.annotate(conn, int(r["id"]), brief_path=new_path)
    state = "merged_dev" if parked is not None else "done"
    logger.info("PR #%s merged into %s → %d event(s) in %s (briefs: %s)",
                number, base or "?", len(rows), state, ", ".join(moved) or "none moved")
    return {"state": state, "pr": number, "brief": moved[0] if moved else None,
            "briefs": moved}


def _sentry_tags(event: dict) -> dict:
    """The event's tags. Not verified against a real `event_alert` payload,
    so `event_tags` accepts both shapes Sentry uses."""
    from talos import sentry_api

    return sentry_api.event_tags(event)


def _sentry_project_ref(payload: dict, issue: dict, event: dict) -> tuple[str, str]:
    """(slug, numeric id) of the project, wherever it comes from.

    `event_alert` carries `event.project` as a number and the slug only inside
    `event.url` (`/api/0/projects/{org}/{slug}/events/…`). Not verified
    against a real payload: every known shape is tried.
    """
    proj = issue.get("project")
    slug = (proj.get("slug") if isinstance(proj, dict) else "") or payload.get("projectSlug") or ""
    pid = ""
    ev_proj = event.get("project")
    if isinstance(ev_proj, dict):
        slug = slug or ev_proj.get("slug") or ""
        pid = str(ev_proj.get("id") or "")
    elif ev_proj not in (None, ""):
        if str(ev_proj).isdigit():
            pid = str(ev_proj)
        else:
            slug = slug or str(ev_proj)
    if not slug:
        m = re.search(r"/projects/[^/]+/([^/]+)/", str(event.get("url") or ""))
        if m:
            slug = m.group(1)
    return str(slug), pid


def handle_sentry(resource: str, delivery_id: str, payload: dict, raw_path: str) -> dict:
    data = payload.get("data") or {}
    event = data.get("event") or {}
    issue = data.get("issue") or event.get("issue") or {}
    if not issue and event:
        # event_alert carries the event; the issue is referenced by id
        issue = {
            "id": event.get("issue_id") or event.get("groupID"),
            "title": event.get("title") or event.get("message"),
            "level": event.get("level", "error"),
            "environment": event.get("environment"),
            "permalink": event.get("web_url") or "",
        }
    action = payload.get("action", "")

    slug, project_id = _sentry_project_ref(payload, issue, event)
    resolved = resolve_sentry_project(CFG, slug, project_id)
    if resolved is None:
        return _reject_delivery(delivery_id, "sentry", resource, action, raw_path,
                                triage.Verdict(False, "unmapped",
                                               f"Sentry project '{slug or project_id}' not mapped"))
    target, sentry_cfg = resolved
    # The canonical slug is the config key: if it was resolved by id, the
    # payload did not carry it and the dedupe_key still has to be stable.
    slug = next((k for k, v in ((CFG.get("sentry") or {}).get("projects") or {}).items()
                 if v is sentry_cfg), slug)
    if not sentry_cfg.get("enabled", False) or not target.enabled:
        return _reject_delivery(delivery_id, "sentry", resource, action, raw_path,
                                triage.Verdict(False, "unmapped",
                                               "project or repo with enabled: false"))

    issue_id = str(issue.get("id") or "").strip()
    if not issue_id:
        # Without an id the dedupe_key collapses to `sentry:org/slug/` and the
        # first event blocks every other one in the project.
        return _reject_delivery(delivery_id, "sentry", resource, action, raw_path,
                                triage.Verdict(False, "no_issue_id",
                                               "the payload has no issue id"))
    dedupe_key = f"sentry:{(CFG.get('sentry') or {}).get('org', '?')}/{slug}/{issue_id}"
    tags = _sentry_tags(event)

    # In a services monorepo, the tag says which service failed, not the
    # project: all six services of acme-org/api report to the same Sentry
    # project. Without this the agent would work in the wrong subdirectory.
    service_path, service_problem = resolve_service(sentry_cfg, tags)
    if service_problem:
        gate = triage.Verdict(False, "unknown_service", service_problem)
        _record(dedupe_key, {
            "source": "sentry", "project": target.project, "subrepo": target.subrepo,
            "gh_repo": target.gh_repo, "sentry_issue_id": issue_id,
            "sentry_project": str(slug), "pipeline": "issue-fix",
            "title": (issue.get("title") or "")[:300],
        }, gate, delivery_id, "sentry", resource, action, raw_path)
        logger.warning("Sentry %s → %s (%s)", issue_id, gate.state, gate.detail)
        return {"state": gate.state, "detail": gate.detail}
    # Without a service_map, resolve_service returns "" and that must not
    # overwrite the service_path routing already set (from the Sentry block or
    # the repo).
    target.service_path = service_path or target.service_path

    fields = {
        "source": "sentry", "project": target.project, "subrepo": target.subrepo,
        "service_path": target.service_path, "gh_repo": target.gh_repo,
        "sentry_issue_id": issue_id, "sentry_project": str(slug),
        "sentry_permalink": issue.get("web_url") or issue.get("permalink", ""),
        "pipeline": "issue-fix", "title": (issue.get("title") or "")[:300],
    }

    gate = triage.gate_sentry_issue(sentry_cfg, issue, tags)
    if not gate.admitted:
        _record(dedupe_key, fields, gate, delivery_id, "sentry", resource, action, raw_path)
        logger.info("Sentry %s → %s (%s)", issue_id, gate.state, gate.detail)
        return {"state": gate.state, "detail": gate.detail}

    event_id, write = _record(dedupe_key, fields, triage.OK, delivery_id, "sentry",
                              resource, action, raw_path, live=not target.dry_run)
    if event_id is None or not write:
        return {"state": "duplicate", "detail": dedupe_key}

    # Only what the webhook brought. The full context (issue + latest event
    # via REST) is fetched by dispatch when it picks up the brief: doing it
    # here meant two GETs of up to 20s inside the handler, and Sentry gives up
    # after ~1s.
    from talos import sentry_api

    context_md = sentry_api.render_context(issue, event)
    return _emit_brief(
        CFG, target, event_id,
        lambda: brief_factory.build_sentry_brief(
            CFG, target, issue, context_md, event_id, dedupe_key,
            created_by=_origin(payload),
        ),
        pipeline="issue-fix", delivery_id=delivery_id,
    )


# ── HTTP ─────────────────────────────────────────────────────────────────────


class Handler(BaseHTTPRequestHandler):
    server_version = "talos-webhookd"

    def log_message(self, fmt, *args):  # logging goes through the logger, not stderr
        logger.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, code: int, payload: dict | None = None) -> None:
        body = json.dumps(payload or {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.rstrip("/") == "/healthz":
            self._send(200, {"status": "ok"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send(400, {"error": "invalid Content-Length"})
            return
        # Negative too: `-1 > MAX` is false and `rfile.read(-1)` reads to EOF,
        # unbounded and before the HMAC is verified.
        if length < 0 or length > MAX_BODY_BYTES:
            self._send(413, {"error": "payload too large"})
            return
        body = self.rfile.read(length)

        path = self.path.split("?")[0].rstrip("/") or "/"
        try:
            if path == "/gh":
                self._handle_gh(body)
            elif path == "/sentry":
                self._handle_sentry(body)
            else:
                self._send(404, {"error": "not found"})
        except Exception:
            logger.exception("Error processing %s", path)
            # A 500 makes GitHub retry; the deliveries PK makes the retry
            # harmless.
            self._send(500, {"error": "internal"})

    def _handle_gh(self, body: bytes) -> None:
        secret = secrets_env.resolve_secret(CFG, "github_webhook_secret_env")
        if not verify_github(body, self.headers.get("X-Hub-Signature-256", ""), secret):
            logger.warning("Invalid GitHub signature from %s", self.address_string())
            self._send(401, {})
            return

        event = self.headers.get("X-GitHub-Event", "")
        delivery = self.headers.get("X-GitHub-Delivery", "") or "sin-id"
        if event == "ping":
            self._send(200, {"pong": True})
            return

        raw_path = store.archive_raw(delivery, body)
        payload = json.loads(body)
        result = handle_github(event, delivery, payload, raw_path)
        self._send(202, result)

    def _handle_sentry(self, body: bytes) -> None:
        secret = secrets_env.resolve_secret(CFG, "sentry_client_secret_env", required=False)
        if not secret:
            self._send(503, {"error": "sentry_client_secret not configured"})
            return
        if not verify_sentry(body, self.headers.get("Sentry-Hook-Signature", ""), secret):
            logger.warning("Invalid Sentry signature from %s", self.address_string())
            self._send(401, {})
            return

        resource = self.headers.get("Sentry-Hook-Resource", "")
        timestamp = self.headers.get("Sentry-Hook-Timestamp", "")
        delivery = self.headers.get("Sentry-Hook-Id", "") or self.headers.get(
            "Request-Id", ""
        ) or f"sentry-{hashlib.sha256(body).hexdigest()[:32]}"
        raw_path = store.archive_raw(delivery, body)

        gate = triage.gate_sentry_resource(resource, timestamp)
        if not gate.admitted:
            self._send(202, _reject_delivery(delivery, "sentry", resource, "", raw_path, gate))
            return

        payload = json.loads(body)
        result = handle_sentry(resource, delivery, payload, raw_path)
        self._send(202, result)


def _warm_github_caches(cfg: dict) -> None:
    """Labels and branches per repo, before the first merge needs them.

    Cold, the first merge webhook made up to six `gh` calls in series before
    answering, close to GitHub's 10 s delivery timeout. Best-effort: whatever
    fails here is asked again on demand.
    """
    for gh_repo, rc in (cfg.get("repos") or {}).items():
        if not (rc or {}).get("enabled"):
            continue
        try:
            feedback.ensure_labels(gh_repo)
            feedback.default_branch(gh_repo)
        except Exception as e:
            logger.warning("Could not prewarm %s: %s", gh_repo, e)


def main() -> int:
    global CFG, SELF_LOGIN

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    secrets_env.load_dotenv()
    CFG = yaml.safe_load(open(CONFIG_PATH)) or {}
    CFG["vault"] = str(Path(CFG.get("vault", "")).expanduser())
    CFG["projects"] = {
        k: str(Path(v).expanduser()) for k, v in (CFG.get("projects") or {}).items()
    }

    warnings: list[str] = []
    problems = validate_routing(CFG, warnings)
    for w in warnings:
        logger.warning("config.yaml (repo disabled): %s", w)
    if problems:
        for p in problems:
            logger.error("config.yaml: %s", p)
        return 1

    # We generate the GitHub secret ourselves, so its absence is a setup error
    # and must block startup. Sentry's is issued by Sentry when the Internal
    # Integration is created: until it exists, /gh has to keep working and
    # /sentry has to reject honestly with a 503.
    try:
        secrets_env.resolve_secret(CFG, "github_webhook_secret_env")
    except secrets_env.MissingSecret as e:
        logger.error("%s", e)
        return 1
    if not secrets_env.resolve_secret(CFG, "sentry_client_secret_env", required=False):
        logger.warning(
            "SENTRY_CLIENT_SECRET not configured: /sentry will answer 503 "
            "until you create the Internal Integration and put it in secrets.env"
        )

    SELF_LOGIN = triage.self_login()
    store.init_db()
    threading.Thread(target=_warm_github_caches, args=(CFG,), daemon=True).start()

    wh = CFG.get("webhook") or {}
    bind, port = wh.get("bind", "127.0.0.1"), int(wh.get("port", 8787))
    enabled = [r for r, c in (CFG.get("repos") or {}).items() if c.get("enabled")]
    logger.info("webhookd listening on %s:%d (account: @%s)", bind, port, SELF_LOGIN or "?")
    logger.info("Repos listening: %s", ", ".join(enabled) or "none")
    if is_paused():
        logger.warning("PAUSED sentinel active: admitting events and writing briefs, "
                       "but dispatch will not start runs until /resume")

    ThreadingHTTPServer((bind, port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
