#!/usr/bin/env python3
"""Reconciliation pass against GitHub: open issues with no row in the ledger.

It exists because the webhook is the fragile link of the ingress. The tunnel
can go down, systemd can restart, GitHub retries a few times and gives up.
Any of those three leaves an issue without a brief and nobody finds out:
there is no error, just silence. The reconciler closes that gap by asking
GitHub — the source of truth — which issues are open, and admitting the ones
the ledger does not know.

The important side effect is that the tunnel stops being critical for the
GitHub half of the harness: without it the system keeps working, with the
latency of the interval instead of a webhook's. Only Sentry really needs the
tunnel.

It reuses the same admission ladder as `webhookd` (gates, dedupe, brief
factory) instead of reimplementing it: two admission paths are two places
where the backlog cutoff can be forgotten.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import yaml

from talos import feedback
from talos import secrets_env
from talos import store
from talos import webhookd
from talos.routing import resolve_gh_repo, validate_routing
from talos.util import CONFIG_PATH, is_harness_authored, utcnow

logger = logging.getLogger("orchestrator")

STAMP_PATH = Path("~/.orchestrator/last-reconcile").expanduser()

# How many rejections in a row /backfill tolerates before giving up on a pass.
BACKFILL_LOOKAHEAD = 25

# REST and not `gh issue list --json`: the subcommand does not expose
# `authorAssociation`, so `gate_association` would go blind and a stranger
# would get in through the reconciler even though the webhook rejects them. The
# REST API returns the issue in the SAME shape as the webhook payload
# (`created_at`, `user`, `author_association`, `html_url`), so nothing needs
# translating either — and a translation is exactly where a gate gets lost.
PER_PAGE = 100


def _concat_pages(out: str) -> list:
    """`gh api --paginate` prints one JSON list per page, back to back."""
    decoder = json.JSONDecoder()
    items: list = []
    i, n = 0, len(out)
    while i < n:
        while i < n and out[i].isspace():
            i += 1
        if i >= n:
            break
        page, i = decoder.raw_decode(out, i)
        items.extend(page if isinstance(page, list) else [page])
    return items


def gh_api(path: str, timeout: int = 30) -> dict | None:
    """One REST object (not a list), or None if GitHub did not answer."""
    try:
        out = subprocess.run(
            ["gh", "api", "-H", "Accept: application/vnd.github+json", path],
            capture_output=True, text=True, timeout=timeout, check=True,
        ).stdout
        data = json.loads(out or "null")
        return data if isinstance(data, dict) else None
    except subprocess.CalledProcessError as e:
        logger.warning("gh api %s failed: %s", path, (e.stderr or "").strip()[:200])
    except Exception as e:
        logger.warning("gh api %s failed: %s", path, e)
    return None


def gh_paginated(url: str, timeout: int = 120) -> list | None:
    """Every page of a REST list endpoint, or None if it fails.

    A single page silently left out anything past 100: the oldest issues, and
    review comments from the 101st on — which never opened a round.
    """
    try:
        out = subprocess.run(
            ["gh", "api", "--paginate", "-H", "Accept: application/vnd.github+json", url],
            capture_output=True, text=True, timeout=timeout, check=True,
        ).stdout
        return _concat_pages(out or "[]")
    except subprocess.CalledProcessError as e:
        logger.warning("gh api %s failed: %s", url, (e.stderr or "").strip()[:200])
    except Exception as e:
        logger.warning("gh api %s failed: %s", url, e)
    return None


def fetch_open_issues(gh_repo: str, direction: str = "desc") -> list[dict]:
    """The repo's open issues, shaped like a webhook payload. Every page."""
    rows = gh_paginated(
        f"repos/{gh_repo}/issues?state=open&per_page={PER_PAGE}"
        f"&sort=created&direction={direction}"
    )
    if rows is None:
        return []

    # `/issues` also returns PRs. The webhook filters them with the same
    # criterion (`gate_github_event`), so the same one goes here.
    return [r for r in rows if "pull_request" not in r]


def _admit(cfg: dict, target, row: dict, delivery_id: str,
           skip_gates: frozenset = frozenset()) -> dict:
    payload = {
        "action": "opened",
        "issue": row,
        "repository": {"full_name": target.gh_repo},
        # Provenance marker: the archived payload has to say the reconciler
        # synthesized it, not GitHub, or the audit lies.
        "_synthetic": {"by": "reconcile.py", "at": utcnow()},
    }
    raw_path = store.archive_raw(delivery_id, json.dumps(payload).encode())
    return webhookd._handle_gh_issue(
        target, delivery_id, payload, raw_path, "opened", skip_gates=skip_gates
    )


def reconcile_repo(cfg: dict, gh_repo: str) -> list[dict]:
    """Admits the repo's open issues that have no row in the ledger."""
    target = resolve_gh_repo(cfg, gh_repo)
    if target is None or not target.enabled:
        return []

    rows = fetch_open_issues(gh_repo)
    if not rows:
        return []

    with store.connect() as conn:
        known = store.known_issue_numbers(conn, gh_repo, reopen=_reopenable(target))

    out = []
    stamp = utcnow()
    for row in rows:
        number = row.get("number")
        if number in known:
            continue
        # With a stamp: a row that reopens (dry run → live, brief_error) hits
        # the same issue again, and a fixed id would make it count as a
        # duplicate delivery forever.
        res = _admit(cfg, target, row, f"reconcile:{gh_repo}#{number}:{stamp}")
        res["number"] = number
        out.append(res)
        logger.info("Reconciler %s #%s → %s", gh_repo, number, res.get("state"))
    return out


def _reopenable(target) -> frozenset:
    """States the reconciler treats as unknown for this repo.

    `brief_error` always (the admission was undone because writing the brief
    failed). Whatever came in during dry run or under the old pause, only if
    the repo is already live: in dry run it would rewrite the same draft on
    every pass.
    """
    states = {"brief_error"}
    if not target.dry_run:
        states |= store.REOPENABLE_STATES
    return frozenset(states)


def reconcile(cfg: dict) -> list[dict]:
    """One pass over every repo with listening on: issues, PRs and releases."""
    results = []
    repos = cfg.get("repos") or {}
    failed = []
    for gh_repo in repos:
        # One repo failing (DB locked, unexpected payload) must not skip the
        # admission of the repos after it.
        try:
            results.extend(reconcile_repo_all(cfg, gh_repo))
        except Exception as e:
            logger.warning("Reconciler %s failed (moving on to the rest): %s", gh_repo, e)
            failed.append(e)
    # Every enabled repo failing is an outage, not "nothing new": /reconcile
    # and dispatch have to see it.
    enabled = [r for r, rc in repos.items() if (rc or {}).get("enabled")]
    if failed and len(failed) >= len(enabled):
        raise failed[0]
    return results


def reconcile_repo_all(cfg: dict, gh_repo: str) -> list[dict]:
    # Migration before release: a row parked this pass can be closed in the
    # same pass if its commit already reached prod. One try per step: a
    # failing release must not drop the admissions this repo already made.
    # Only every step failing means the repo is down, and that is raised.
    steps = (reconcile_repo, reconcile_prs, migrate_merged_dev, release_merged)
    out, errors = [], []
    for step in steps:
        try:
            out.extend(step(cfg, gh_repo))
        except Exception as e:
            logger.warning("Reconciler %s: %s failed: %s", gh_repo, step.__name__, e)
            errors.append(e)
    if len(errors) == len(steps):
        raise errors[0]
    return out


def backfill(cfg: dict, gh_repo: str, count: int) -> list[dict]:
    """Pulls in by hand the `count` OLDEST open issues from the backlog.

    Skips the `activated_at` cutoff — that is exactly what the backlog
    violates — but no other gate: an issue with `ai-skip` stays out, and the
    dispatch caps still bound how much runs per day. A few at a time on
    purpose: each one is a container run of up to two hours.

    `count` counts ADMITTED, not attempts. Slicing the list before the gates
    meant the first issue that will never get in (`ai-skip`,
    `author_association=NONE`) was picked on every /backfill and jammed it.
    """
    target = resolve_gh_repo(cfg, gh_repo)
    if target is None:
        raise ValueError(f"{gh_repo} is not under repos: in the config")
    if not target.enabled:
        raise ValueError(f"{gh_repo} has enabled: false")

    rows = fetch_open_issues(gh_repo, direction="asc")
    with store.connect() as conn:
        known = store.known_issue_numbers(
            conn, gh_repo, worked_only=True, reopen=_reopenable(target)
        )

    # Oldest first: the backlog is eaten from the old end, which is where the
    # issues nobody looks at anymore are.
    pending = sorted(
        [r for r in rows if r.get("number") not in known],
        key=lambda r: r.get("created_at") or "",
    )

    out = []
    stamp = utcnow()
    admitted = 0
    for row in pending[:count + BACKFILL_LOOKAHEAD]:
        if admitted >= count:
            break
        number = row.get("number")
        res = _admit(
            cfg, target, row,
            f"backfill:{gh_repo}#{number}:{stamp}",
            skip_gates=frozenset({"backlog"}),
        )
        res["number"] = number
        out.append(res)
        if res.get("state") in ("admitted", "dry_run"):
            admitted += 1
        logger.info("Backfill %s #%s → %s", gh_repo, number, res.get("state"))
    return out


def due(interval_minutes: int) -> bool:
    """True if a reconcile is due. The stamp avoids hitting the API every 2 min."""
    if interval_minutes <= 0:
        return False
    try:
        import time

        if not STAMP_PATH.exists():
            return True
        age = time.time() - STAMP_PATH.stat().st_mtime
        return age >= interval_minutes * 60
    except Exception:
        return True


def touch_stamp() -> None:
    try:
        STAMP_PATH.parent.mkdir(parents=True, exist_ok=True)
        STAMP_PATH.write_text(utcnow())
    except Exception as e:
        logger.warning("Could not write the reconciler stamp: %s", e)


def load_cfg() -> dict:
    secrets_env.load_dotenv()
    cfg = yaml.safe_load(open(CONFIG_PATH)) or {}
    cfg["vault"] = str(Path(cfg.get("vault", "")).expanduser())
    cfg["projects"] = {
        k: str(Path(v).expanduser()) for k, v in (cfg.get("projects") or {}).items()
    }
    warnings: list[str] = []
    problems = validate_routing(cfg, warnings)
    for p in problems:
        logger.error("config.yaml: %s", p)
    for w in warnings:
        logger.warning("config.yaml (repo disabled): %s", w)
    webhookd.CFG = cfg
    # The bot calls this before reconcile(): without it a freshly deployed
    # column (merge_commit_sha) may not exist yet in its process's DB.
    store.init_db()
    return cfg


def main() -> int:
    ap = argparse.ArgumentParser(description="GitHub issue reconciliation.")
    ap.add_argument("--repo", help="Only this repo (default: every enabled one).")
    ap.add_argument("--backfill", type=int, metavar="N",
                    help="Admits the N oldest open issues, skipping "
                         "the activated_at cutoff. Requires --repo.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Lists what it would admit, without writing anything.")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    cfg = load_cfg()
    store.init_db()

    if args.dry_run:
        repos = [args.repo] if args.repo else list(cfg.get("repos") or {})
        for gh_repo in repos:
            target = resolve_gh_repo(cfg, gh_repo)
            if target is None:
                print(f"{gh_repo}: not mapped")
                continue
            rows = fetch_open_issues(gh_repo)
            with store.connect() as conn:
                known = store.known_issue_numbers(conn, gh_repo, reopen=_reopenable(target))
            fresh = [r for r in rows if r.get("number") not in known]
            print(f"\n{gh_repo} (enabled={target.enabled}, mode="
                  f"{'live' if not target.dry_run else 'dry_run'}): "
                  f"{len(rows)} open, {len(fresh)} with no row in the ledger")
            for r in fresh[:20]:
                from talos import triage
                issue = r
                verdicts = [
                    triage.gate_skip_label(target.cfg, issue),
                    triage.gate_handled_labels(issue),
                    triage.gate_backlog_cutoff(target.cfg, issue),
                    triage.gate_association(issue),
                    triage.gate_author(target.cfg, issue.get("user") or {}),
                ]
                bad = next((v for v in verdicts if not v.admitted), None)
                state = bad.state if bad else "admitted"
                print(f"  #{r['number']:<5} {state:<28} {r['created_at']}  "
                      f"{(r.get('title') or '')[:52]}")
            if len(fresh) > 20:
                print(f"  … and {len(fresh) - 20} more")
        return 0

    if args.backfill:
        if not args.repo:
            print("--backfill needs --repo")
            return 2
        res = backfill(cfg, args.repo, args.backfill)
        print(f"{len(res)} issue(s) processed: "
              + ", ".join(f"#{r['number']}→{r['state']}" for r in res))
        return 0

    if args.repo:
        res = reconcile_repo_all(cfg, args.repo)
    else:
        res = reconcile(cfg)
    touch_stamp()
    admitted = [r for r in res if r.get("state") == "admitted"]
    print(f"{len(res)} new event(s) seen, {len(admitted)} admitted")
    for r in res:
        ref = f"#{r['pr']}" if r.get("pr") else f"#{r.get('number')}"
        print(f"  {ref:<8} {r.get('state')}  {r.get('detail') or r.get('brief') or ''}")
    return 0




# ── PRs: review-fix rounds and merges the webhook missed ─────────────────────


def human_comments(gh_repo: str, number: int) -> list[dict]:
    """The PR's review comments NOT written by the harness, newest last.

    Same criterion as the webhook: only reviews and review comments, not the
    PR's standalone comments. The filter is by the marker in the body and not
    by author, because the harness posts with the repo owner's token.
    """
    found: list[dict] = []
    for item in (gh_paginated(f"repos/{gh_repo}/pulls/{number}/comments?per_page=100") or []):
        found.append({
            "id": item.get("id"),
            "body": item.get("body") or "",
            "at": item.get("created_at") or "",
            "login": ((item.get("user") or {}).get("login")) or "?",
            "type": ((item.get("user") or {}).get("type")) or "User",
            "kind": "pull_request_review_comment",
        })
    for item in (gh_paginated(f"repos/{gh_repo}/pulls/{number}/reviews?per_page=100") or []):
        found.append({
            "id": item.get("id"),
            "body": item.get("body") or "",
            "at": item.get("submitted_at") or "",
            "login": ((item.get("user") or {}).get("login")) or "?",
            "type": ((item.get("user") or {}).get("type")) or "User",
            "state": item.get("state") or "",
            "kind": "pull_request_review",
        })

    out = [
        c for c in found
        if c["at"] and c["body"].strip() and not is_harness_authored(c["body"])
    ]
    return sorted(out, key=lambda c: c["at"])


def reconcile_prs(cfg: dict, gh_repo: str) -> list[dict]:
    """Closes merged PRs and opens any missing review-fix round.

    The issue reconciler only covers the start of the cycle. Without this, a
    comment of yours on a PR with the tunnel down triggers nothing and the
    fixer never runs — the most useful half of the harness would depend on
    the most fragile link.
    """
    target = resolve_gh_repo(cfg, gh_repo)
    if target is None or not target.enabled:
        return []

    label = target.cfg.get("pr_label", "ai-generated")
    # Open and merged separately: with a single `--state all --limit 30` a
    # repo with 30 closed automated PRs stopped seeing the open ones.
    prs: list[dict] = []
    for state, limit in (("open", 200), ("merged", 100)):
        try:
            raw = subprocess.run(
                ["gh", "pr", "list", "--repo", gh_repo, "--label", label, "--state", state,
                 "--limit", str(limit),
                 "--json", "number,title,url,state,mergedAt,headRefName,baseRefName,mergeCommit"],
                capture_output=True, text=True, timeout=60, check=True,
            ).stdout
            prs += json.loads(raw or "[]")
        except Exception as e:
            logger.warning("Could not list %s PRs for %s: %s", state, gh_repo, e)
            return []

    out = []
    for pr in prs:
        number = pr.get("number")
        if pr.get("mergedAt"):
            res = _close_if_open(cfg, target, pr)
        elif pr.get("state") == "OPEN":
            res = _round_if_pending(cfg, target, pr)
        else:
            res = None  # closed without merge: nothing to do
        if res:
            res["pr"] = number
            out.append(res)
            logger.info("Reconciler PR %s #%s → %s", gh_repo, number, res.get("state"))
    return out


def _close_if_open(cfg: dict, target, pr: dict) -> dict | None:
    """Merged on GitHub but the ledger never heard → close the cycle."""
    number = pr.get("number")
    with store.connect() as conn:
        # Both rows, not the first one found: with a review-fix round already
        # closed, looking only at the PR key left the issue row in `pr_open`
        # forever. Without rounds the PR key does not exist and the branch is
        # the thread to the brief.
        rows = [
            store.find_by_dedupe(conn, f"pr:{target.gh_repo}#{number}"),
            store.find_issue_row_by_branch(conn, target.gh_repo, pr.get("headRefName") or ""),
        ]
        if all(r is None or r["state"] in store.TERMINAL_STATES for r in rows):
            return None

    payload = {
        "action": "closed",
        "pull_request": {
            "number": number, "html_url": pr.get("url", ""), "merged": True,
            "title": pr.get("title", ""), "head": {"ref": pr.get("headRefName") or ""},
            "base": {"ref": pr.get("baseRefName") or ""},
            "merge_commit_sha": (pr.get("mergeCommit") or {}).get("oid") or "",
            # It came from listing by label, so it has it: webhookd requires it.
            "labels": [{"name": target.cfg.get("pr_label", "ai-generated")}],
        },
        "repository": {"full_name": target.gh_repo},
        "_synthetic": {"by": "reconcile.py", "at": utcnow()},
    }
    # With a stamp: a fixed id made a merge that did not close its rows the
    # first time (the #88 case) count as a duplicate forever. Replays are safe
    # because the handler skips rows already in TERMINAL_STATES.
    delivery_id = f"reconcile:{target.gh_repo}#pr{number}:merged:{utcnow()}"
    raw_path = store.archive_raw(delivery_id, json.dumps(payload).encode())
    return webhookd._handle_gh_merged(target, delivery_id, payload, raw_path, "closed")


# ── Release: merged_dev → done once the commit reaches prod ─────────────────

# `compare/{prod}...{sha}` from GitHub's point of view: `behind` or
# `identical` mean every commit of `sha` is already in prod. `ahead` and
# `diverged` mean it has not arrived yet.
RELEASED_STATUSES = {"behind", "identical"}


def _pr_merge_info(gh_repo: str, number) -> dict | None:
    pr = gh_api(f"repos/{gh_repo}/pulls/{number}")
    if pr is None:
        return None
    return {
        "merged": bool(pr.get("merged")),
        "base": ((pr.get("base") or {}).get("ref")) or "",
        "sha": pr.get("merge_commit_sha") or "",
    }


def migrate_merged_dev(cfg: dict, gh_repo: str) -> list[dict]:
    """Parks stage A rows already merged into the integration branch.

    Rows closed before `merged_dev` existed sit in `done` (or in `pr_open`, if
    the merge never reached the ledger) with an issue that will never close.
    Idempotent: a parked row leaves `done`/`pr_open`, and a `done` row merged
    where GitHub closes the issue by itself gets only its sha, which takes it
    off the candidates. `pr_open` rows get the merged-to-dev comment, since
    their merge was never announced; `done` rows only get labels.
    """
    with store.connect() as conn:
        rows = [
            r for r in store.rows_in_states(conn, {"done", "pr_open"}, gh_repo)
            if store.is_stage_a_issue(r) and r["pr_number"] and not r["merge_commit_sha"]
        ]
    if not rows:
        return []
    target = resolve_gh_repo(cfg, gh_repo)
    if target is None or not target.enabled:
        logger.info("Migration %s: %d candidate row(s), but the repo is disabled",
                    gh_repo, len(rows))
        return []
    prod = feedback.prod_branch(gh_repo, target.cfg)
    default = feedback.default_branch(gh_repo)
    if not prod or not default:
        logger.warning("Migration %s: %d candidate row(s), but GitHub did not tell me the "
                       "prod or default branch", gh_repo, len(rows))
        return []

    out = []
    for row in rows:
        info = _pr_merge_info(gh_repo, row["pr_number"])
        if not info or not info["merged"] or not info["sha"]:
            continue
        to_prod = info["base"] == prod
        github_closes = info["base"] == default
        if to_prod and github_closes:
            # Already where it belongs. The sha marks it as classified so no
            # pass asks GitHub about it again, as the webhook does for a merge
            # into prod. `pr_open` ones are left to reconcile_prs.
            if row["state"] == "done":
                with store.connect() as conn:
                    store.annotate(conn, int(row["id"]), merge_commit_sha=info["sha"])
            continue
        with store.connect() as conn:
            moved = store.transition(conn, int(row["id"]), row["state"], "merged_dev",
                                     merge_commit_sha=info["sha"])
        if not moved:
            continue
        if row["state"] == "pr_open":
            # The merge never reached the ledger: nobody told the issue and
            # the briefs are still in in-review/. Same closing as
            # webhookd._handle_gh_merged, review-fix row included (the #88
            # bug). The CAS above makes it happen once.
            feedback.announce_merged_dev(gh_repo, row["gh_issue"], info["base"],
                                         row["pr_number"], prod,
                                         comment=not to_prod and not github_closes)
            closing = [row]
            with store.connect() as conn:
                pr_row = store.find_by_dedupe(conn, f"pr:{gh_repo}#{row['pr_number']}")
                if (pr_row is not None and pr_row["state"] not in store.TERMINAL_STATES
                        and store.transition(conn, int(pr_row["id"]), pr_row["state"], "done")):
                    closing.append(pr_row)
            for r in closing:
                new_path = feedback.close_merged(cfg, r, notify=False, base=info["base"],
                                                 prod=prod, parked=True)
                if new_path:
                    with store.connect() as conn:
                        store.annotate(conn, int(r["id"]), brief_path=new_path)
        else:
            # `done` rows were already announced as closed: labels only.
            feedback.label_issue(gh_repo, row["gh_issue"], add="ai-merged-dev",
                                 remove="ai-in-review")
        logger.info("Migration: %s #%s → merged_dev (PR #%s into %s)",
                    gh_repo, row["gh_issue"], row["pr_number"], info["base"])
        out.append({"state": "merged_dev", "number": row["gh_issue"],
                    "detail": f"PR #{row['pr_number']} into {info['base']} (migrated)"})
    return out


def release_merged(cfg: dict, gh_repo: str) -> list[dict]:
    """Closes the issues whose merge commit already reached the prod branch.

    Only GitHub's compare decides. Any call that fails leaves the row in
    `merged_dev`, and the next pass tries again.
    """
    with store.connect() as conn:
        rows = store.rows_in_states(conn, {"merged_dev"}, gh_repo)
    if not rows:
        return []
    target = resolve_gh_repo(cfg, gh_repo)
    if target is None or not target.enabled:
        # `enabled: false` freezes the closes too; say so, or they just wait.
        logger.warning("Release %s: %d issue(s) waiting in merged_dev, but the repo "
                       "is disabled", gh_repo, len(rows))
        return []
    prod = feedback.prod_branch(gh_repo, target.cfg)
    if not prod:
        logger.warning("Release %s: %d issue(s) waiting in merged_dev, but I do not know "
                       "the prod branch (GitHub did not answer)", gh_repo, len(rows))
        return []

    out = []
    for row in rows:
        res = _release_one(cfg, gh_repo, row, prod)
        if res:
            out.append(res)
            logger.info("Release %s #%s → %s", gh_repo, res["number"], res["detail"])
    return out


# A row this long in `merged_dev` gets one Telegram warning: the release was
# probably a squash or a rebase, or a force-push left the commit orphaned.
STALE_MERGED_DEV_DAYS = 14
STALE_MARK = "stale_merged_dev"


def _release_one(cfg: dict, gh_repo: str, row, prod: str) -> dict | None:
    issue = row["gh_issue"]
    sha = row["merge_commit_sha"] or ""
    if not sha and row["pr_number"]:
        sha = ((_pr_merge_info(gh_repo, row["pr_number"]) or {}).get("sha")) or ""
        if sha:
            with store.connect() as conn:
                store.annotate(conn, int(row["id"]), merge_commit_sha=sha)

    # per_page=1: only `status` and `base_commit` are read, not the diff. No
    # sha, or no answer (a 404 for an orphaned commit), is never "released";
    # the row can still leave `merged_dev` through the issue state.
    cmp = gh_api(f"repos/{gh_repo}/compare/{prod}...{sha}?per_page=1") if sha else None
    released = bool(cmp) and cmp.get("status") in RELEASED_STATUSES
    gh_issue = gh_api(f"repos/{gh_repo}/issues/{issue}")
    if gh_issue is None:
        return None
    closed = gh_issue.get("state") == "closed"
    if not released:
        return _not_released(cfg, gh_repo, row, prod, closed)

    prod_sha = ((cmp.get("base_commit") or {}).get("sha")) or ""
    # A human reopened it after the merge to dev (most likely the fix did not
    # work): reaching prod does not overrule that. Labels and a warning only.
    reopened = not closed and gh_issue.get("state_reason") == "reopened"

    # Claim first, then talk to GitHub: two concurrent passes must not both
    # comment. If closing fails, the claim is undone and the next pass retries.
    with store.connect() as conn:
        if not store.transition(conn, int(row["id"]), "merged_dev", "done"):
            return None
    # A crash right here leaves the row in `done` with the issue open, and
    # nothing retries it. Accepted: it fails on the safe side (nothing is
    # closed without evidence) and the window is one `gh` call wide.
    if not feedback.announce_released(gh_repo, issue, prod, prod_sha,
                                      row["pr_number"], closed or reopened):
        with store.connect() as conn:
            store.transition(conn, int(row["id"]), "done", "merged_dev")
        return None
    # Telegram only when the harness closed it, or a human has to decide.
    if not closed:
        feedback.notify_released(cfg, gh_repo, issue, row["title"] or "", prod, prod_sha,
                                 reopened=reopened)
    note = " (already closed)" if closed else " (reopened: leaving it open)" if reopened else ""
    return {"state": "released", "number": issue, "detail": f"{prod} @ {prod_sha[:7]}{note}"}


def _not_released(cfg: dict, gh_repo: str, row, prod: str, closed: bool) -> dict | None:
    issue = row["gh_issue"]
    if closed:
        # Closed by a human (or by GitHub's `Closes #N` when the integration
        # branch is the default) before the commit reached prod: nothing left
        # to close, and waiting would keep it counting in /status and asking
        # GitHub every pass. Silent: no comment, no Telegram.
        with store.connect() as conn:
            if not store.transition(conn, int(row["id"]), "merged_dev", "done"):
                return None
        return {"state": "done", "number": issue,
                "detail": f"closed without reaching {prod}: leaves merged_dev"}
    if str(row["last_error"] or "").startswith(STALE_MARK):
        return None
    age = _age_days(row["updated_at"])
    if age is None or age < STALE_MERGED_DEV_DAYS:
        return None
    with store.connect() as conn:
        store.annotate(conn, int(row["id"]),
                       last_error=f"{STALE_MARK}: {age} days without reaching {prod}")
    feedback.notify_stale_merged_dev(cfg, gh_repo, issue, row["title"] or "", prod, age)
    logger.warning("Release %s #%s: %d days in merged_dev without reaching %s",
                   gh_repo, issue, age, prod)
    return None


def _age_days(stamp) -> int | None:
    try:
        then = datetime.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - then).days


def _round_if_pending(cfg: dict, target, pr: dict) -> dict | None:
    """There is a human comment newer than the last round → trigger the fixer."""
    number = pr.get("number")
    comments = human_comments(target.gh_repo, number)
    if not comments:
        return None
    newest = comments[-1]

    with store.connect() as conn:
        row = store.find_by_dedupe(conn, f"pr:{target.gh_repo}#{number}")
        if row is not None:
            # A run in progress is not stepped on: the running round will read
            # this comment from GitHub when it loads the threads.
            if row["state"] in ("admitted", "dispatched", "running"):
                return None
            mark = row["last_comment_at"] or ""
            if mark and newest["at"] <= mark:
                return None

    payload = {
        "action": "created" if newest["kind"] == "pull_request_review_comment" else "submitted",
        "pull_request": {
            "number": number, "html_url": pr.get("url", ""), "title": pr.get("title", ""),
            "head": {"ref": pr.get("headRefName") or ""},
            "labels": [{"name": target.cfg.get("pr_label", "ai-generated")}],
        },
        # The author's real type, not a hardcoded "User": with it hardcoded,
        # Copilot or CodeRabbit comments slipped past the bot gate.
        "sender": {"login": newest["login"], "type": newest.get("type") or "User"},
        ("comment" if newest["kind"] == "pull_request_review_comment" else "review"): {
            "body": newest["body"],
            "created_at": newest["at"],
            "submitted_at": newest["at"],
            "state": newest.get("state") or "",
        },
        "repository": {"full_name": target.gh_repo},
        "_synthetic": {"by": "reconcile.py", "at": utcnow()},
    }
    delivery_id = f"reconcile:{target.gh_repo}#pr{number}:c{newest['id']}"
    raw_path = store.archive_raw(delivery_id, json.dumps(payload).encode())
    return webhookd._handle_gh_review(
        target, delivery_id, payload, raw_path, newest["kind"], payload["action"]
    )


if __name__ == "__main__":
    raise SystemExit(main())
