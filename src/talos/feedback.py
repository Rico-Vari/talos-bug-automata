#!/usr/bin/env python3
"""
Egress back to the event's source: comments on the GitHub issue and on the
Sentry issue.

It lives on the host and not in the agent for a concrete reason: the agent
has a documented history of skipping its bookkeeping (dispatch.py's
warning about `.orchestrator-plan.md` exists because of that), and the
failure cases are, by definition, the ones where the agent did not finish.
Asking it to report its own failure is structurally impossible.

Everything is best-effort: a comment that does not go out must not bring
down the pass. The truth of the run is already in the brief's front matter
and in the ledger.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("orchestrator")

FOOTER = "\n\n— talos (automated harness)"


def _gh_output(args: list[str], timeout: int = 20) -> str | None:
    """stdout of a gh call, or None if it failed."""
    if shutil.which("gh") is None:
        logger.warning("gh is not installed — no feedback to GitHub")
        return None
    try:
        result = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            logger.warning("gh %s → exit %d: %s", args[0], result.returncode, result.stderr[:200])
            return None
        return result.stdout
    except Exception as e:
        logger.warning("gh %s failed: %s", args[0], e)
        return None


def _gh(args: list[str], timeout: int = 20) -> bool:
    return _gh_output(args, timeout) is not None


# Lifecycle labels the harness puts on issues: name → (color, description).
# `gh issue edit --add-label` fails when the label does not exist in the repo,
# which is why no harness issue had a label until they were created on demand.
HARNESS_LABELS = {
    "ai-in-review": ("fbca04", "The harness opened a PR for this issue"),
    "ai-merged-dev": ("1d76db", "Merged to the integration branch; waiting for the prod release"),
    "ai-released": ("0e8a16", "The fix has reached the production branch"),
}

# Per process: repos whose labels are known to exist, and their default
# branches. A renamed default branch needs a webhookd/dispatch restart.
_LABELS_READY: set[str] = set()
_DEFAULT_BRANCH: dict[str, str] = {}


def ensure_labels(gh_repo: str) -> bool:
    """Creates the harness labels in the repo if missing. Once per repo and process.

    `--force` makes it idempotent (it updates an existing label instead of
    failing). A failure is not cached, so the next call retries.
    """
    if not gh_repo:
        return False
    if gh_repo in _LABELS_READY:
        return True
    ok = True
    for name, (color, description) in HARNESS_LABELS.items():
        ok = _gh(["label", "create", name, "--repo", gh_repo, "--color", color,
                  "--description", description, "--force"]) and ok
    if ok:
        _LABELS_READY.add(gh_repo)
    return ok


def default_branch(gh_repo: str) -> str | None:
    """The repo's default branch, asked to GitHub once per repo and process.

    It matters on its own because GitHub only honors a PR's `Closes #N` when
    the PR is merged into the default branch. None if GitHub does not answer.
    """
    if not gh_repo:
        return None
    if gh_repo in _DEFAULT_BRANCH:
        return _DEFAULT_BRANCH[gh_repo]
    out = _gh_output(["repo", "view", gh_repo, "--json", "defaultBranchRef",
                      "--jq", ".defaultBranchRef.name"])
    name = (out or "").strip()
    if not name:
        return None
    _DEFAULT_BRANCH[gh_repo] = name
    return name


def prod_branch(gh_repo: str, repo_cfg: dict | None) -> str | None:
    """The branch whose arrival closes an issue: `prod_branch`, else the repo default.

    None when it is not configured and GitHub does not answer: callers must
    then act as if the merge had not reached prod, never the other way around.
    """
    configured = str((repo_cfg or {}).get("prod_branch") or "").strip()
    return configured or default_branch(gh_repo)


def comment_on_issue(gh_repo: str, issue: int | str, body: str) -> bool:
    if not gh_repo or not issue:
        return False
    return _gh(["issue", "comment", str(issue), "--repo", gh_repo, "--body", body + FOOTER])


def comment_on_pr(gh_repo: str, pr: int | str, body: str) -> bool:
    """A comment on the PR conversation, with the anti-loop marker.

    `issue_comment` is not subscribed today, so the marker is not
    load-bearing; it goes anyway so subscribing to it tomorrow does not open
    a loop.
    """
    if not gh_repo or not pr:
        return False
    from talos.util import AUTO_REVIEW_MARKER

    return _gh(["pr", "comment", str(pr), "--repo", gh_repo,
                "--body", body + FOOTER + "\n\n" + AUTO_REVIEW_MARKER])


def label_issue(gh_repo: str, issue: int | str, add: str = "", remove: str = "") -> bool:
    if not gh_repo or not issue:
        return False
    names = {n.strip() for n in f"{add},{remove}".split(",") if n.strip()}
    if names & set(HARNESS_LABELS):
        ensure_labels(gh_repo)
    args = ["issue", "edit", str(issue), "--repo", gh_repo]
    if add:
        args += ["--add-label", add]
    if remove:
        args += ["--remove-label", remove]
    return _gh(args)


def announce_pr_open(config: dict, brief_meta: dict, pr_data: dict) -> None:
    """Stage A success: tells the issue and Sentry that there is a PR."""
    gh_repo = str(brief_meta.get("gh_repo") or "")
    issue = brief_meta.get("gh_issue")
    pr_url = pr_data.get("pr_url", "")
    fc = pr_data.get("findings_count") or {}
    findings = ", ".join(f"{k}: {v}" for k, v in fc.items()) or "no findings"

    if issue and pr_url:
        comment_on_issue(
            gh_repo, issue,
            f"🔀 PR opened: {pr_url}\n\n"
            f"Automated review posted on the PR ({findings}). "
            f"Open threads: {pr_data.get('threads_opened', 0)}.\n\n"
            f"Waiting for human review — the merge is not automatic.",
        )
        # Visual signal for humans and the second admission lock
        # (triage.gate_handled_labels); the first one is its events.db row.
        label_issue(gh_repo, issue, add="ai-in-review")

    sentry_id = str(brief_meta.get("sentry_issue_id") or "")
    if sentry_id and pr_url:
        try:
            from talos import sentry_api
            from talos import secrets_env

            token = secrets_env.resolve_secret(config, "sentry_auth_token_env", required=False)
            if token:
                sentry_api.post_issue_comment(
                    sentry_id, token,
                    f"Automated PR opened for this error: {pr_url}",
                )
        except Exception as e:
            logger.warning("Could not comment on Sentry: %s", e)


def announce_failure(config: dict, brief_meta: dict, reason: str, run_log: str = "") -> None:
    """Failure or abort: leaves a record on the issue, or on the PR for review-fix.

    It sends the log's *path*, never its content: run logs carry the repo's
    source code, and an issue can be read by more people than the host.
    """
    gh_repo = str(brief_meta.get("gh_repo") or "")
    issue = brief_meta.get("gh_issue")
    pr = brief_meta.get("pr_number")
    log_line = f"\n\nRun log (on the host): `{run_log}`" if run_log else ""
    if gh_repo and issue:
        comment_on_issue(
            gh_repo, issue,
            f"❌ The harness could not resolve this issue automatically.\n\n"
            f"Reason: {reason[:800]}{log_line}\n\n"
            f"Left for a human.",
        )
    elif gh_repo and pr:
        comment_on_pr(
            gh_repo, pr,
            f"❌ The automated review-fix round did not finish.\n\n"
            f"Reason: {reason[:800]}{log_line}\n\n"
            f"The threads still open are left for a human.",
        )


def announce_merged_dev(gh_repo: str, issue: int | str, base: str, pr: int | str,
                        prod: str | None, comment: bool = True) -> None:
    """Stage A PR merged into the integration branch: the issue waits for prod.

    `comment=False` when the comment would lie: GitHub already closed the
    issue by the `Closes #N` (the integration branch is the repo default), or
    the merge went straight to a prod branch that is not the default.
    """
    if not gh_repo or not issue:
        return
    target = f"`{prod}`" if prod else "the production branch"
    label_issue(gh_repo, issue, add="ai-merged-dev", remove="ai-in-review")
    if not comment:
        return
    comment_on_issue(
        gh_repo, issue,
        f"📦 PR #{pr} was merged to `{base}`.\n\n"
        f"This issue stays open on purpose: it will close on its own when "
        f"the change reaches {target}.",
    )


def announce_released(gh_repo: str, issue: int | str, prod: str, prod_sha: str,
                      pr: int | str | None, already_closed: bool) -> bool:
    """The merge commit reached prod: closes the issue and swaps the labels.

    Returns False when the step that matters failed, so the row goes back to
    `merged_dev` and the next pass retries: closing the issue or, for an issue
    that is already closed, fixing its labels (the only thing left to do).
    `already_closed` also covers an issue a human reopened: it is left as is.
    """
    if not gh_repo or not issue:
        return False
    if not already_closed:
        via = f" with PR #{pr}" if pr else ""
        head = f" (head `{prod_sha[:7]}`)" if prod_sha else ""
        body = (
            f"🚀 The change that went in{via} is now on `{prod}`{head}, "
            f"so I am closing this issue."
        )
        if not _gh(["issue", "close", str(issue), "--repo", gh_repo,
                    "--reason", "completed", "--comment", body + FOOTER]):
            return False
    # ai-in-review too: announce_merged_dev may have failed to remove it.
    labeled = label_issue(gh_repo, issue, add="ai-released",
                          remove="ai-merged-dev,ai-in-review")
    return labeled if already_closed else True


def notify_released(config: dict, gh_repo: str, issue: int | str, title: str,
                    prod: str, prod_sha: str, reopened: bool = False) -> None:
    """One Telegram message per issue the harness closed. Best-effort, like the rest.

    `reopened`: the fix reached prod but a human had reopened the issue, so
    it stays open and a human has to decide.
    """
    import html

    e = html.escape
    tail = ("But a human reopened it: leaving it open for you to decide."
            if reopened else "Issue closed.")
    _telegram(config, (
        f"🚀 <b>{e(gh_repo)}</b> #{e(str(issue))} reached <code>{e(prod)}</code> "
        f"(head <code>{e(prod_sha[:7])}</code>)\n{e(title or '')}\n{tail}"
    ))


def notify_stale_merged_dev(config: dict, gh_repo: str, issue: int | str, title: str,
                            prod: str, days: int) -> None:
    """One-off warning: an issue has waited in `merged_dev` for too long."""
    import html

    e = html.escape
    _telegram(config, (
        f"⏳ <b>{e(gh_repo)}</b> #{e(str(issue))} has been merged to dev for {days} days "
        f"without reaching <code>{e(prod)}</code>\n{e(title or '')}\n"
        f"Check whether the release was a squash or a rebase, or whether the commit got lost. "
        f"If it no longer applies, close the issue and the row clears itself."
    ))


def _telegram(config: dict, text: str) -> None:
    token = config.get("telegram_bot_token", "")
    chat_id = config.get("telegram_chat_id", "")
    if not token or not chat_id:
        return
    import requests

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=10,
        )
        if not resp.ok:
            logger.warning("Telegram rejected the notice (%s): %s",
                           resp.status_code, resp.text[:200])
    except Exception as e:
        # Only the type: requests' errors carry the URL, and the URL carries
        # the bot token.
        logger.warning("Could not send the Telegram notice: %s", type(e).__name__)


def close_merged(config: dict, row, pr_url: str = "", notify: bool = True,
                 base: str = "", prod: str | None = None, parked: bool = False) -> str | None:
    """Closes the loop when an automated PR is merged.

    Moves the brief to `projects/{project}/completed/` and notifies over
    Telegram. It exists because the merge event marked the ledger `done` and
    left the brief in `in-review/` forever: the ledger said closed, the vault
    said awaiting review, and the vault is what one looks at.

    Returns the brief's new path, or None if there was nothing to move.
    `notify=False` when the same merge closes more than one brief (stage A
    and stage B rounds): one merge, one notification.

    `parked` = the merge went to the integration branch and the issue stays
    in `merged_dev`: the notice says so (📦) and not "merged" (🎉), which read
    as if it were already in prod.
    """
    import frontmatter

    brief_path = Path(str(row["brief_path"] or ""))
    project = str(row["project"] or "")
    vault = str(config.get("vault") or "")
    if not (brief_path.name and vault and project):
        return None
    if not brief_path.exists():
        logger.info("The brief for event %s is no longer at %s", row["id"], brief_path)
        return None

    try:
        post = frontmatter.load(brief_path)
        post.metadata["status"] = "merged_dev" if parked else "merged"
        post.metadata["merged_at"] = datetime.now().isoformat()
        if base:
            post.metadata["merged_to"] = base
        if pr_url:
            post.metadata["pr_url"] = pr_url
        brief_path.write_text(frontmatter.dumps(post))

        completed = Path(vault) / "projects" / project / "completed"
        completed.mkdir(parents=True, exist_ok=True)
        new_path = completed / brief_path.name
        shutil.move(str(brief_path), str(new_path))
        logger.info("PR merged → brief to completed/: %s", new_path)
    except Exception as e:
        logger.warning("Could not move the brief for event %s: %s", row["id"], e)
        return None

    if not notify:
        return str(new_path)
    try:
        from talos.dispatch import notify_telegram

        notify_telegram(
            config, new_path.name, project, "merged_dev" if parked else "merged", None,
            {"pr_url": pr_url or str(row["pr_url"] or ""), "summary": "PR merged by a human",
             "base": base, "prod": prod or ""},
        )
    except Exception as e:
        logger.warning("Could not notify the merge over Telegram: %s", e)
    return str(new_path)
