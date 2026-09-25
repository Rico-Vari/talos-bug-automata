#!/usr/bin/env python3
"""Offline tests for the harness: no network, no Docker, no GitHub.

They cover the paths that actually broke along the way. They all have one
thing in common: they are the ones that running the system exercises least,
because they depend on something going wrong (a run that dies halfway, a
merge, a gate that lifts later). A bug there stays invisible until the day it
matters.

Each test drives the function that runs in production, not a smaller one that
looks like it: a test that calls `store.reap_orphans` directly stays green
even if `run_once` stops calling it.

  python tests/test_offline.py
"""
from __future__ import annotations

import contextlib
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

FAILED: list[str] = []
REPO = "your-org/sandbox"
LABEL = [{"name": "ai-generated"}]


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✔' if ok else '✘'} {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


@contextlib.contextmanager
def patched(obj, name: str, value):
    # From __dict__ when possible: `getattr` on a classmethod returns the
    # already bound method, and restoring that does not leave the class as it was.
    old = obj.__dict__[name] if isinstance(obj, type) and name in obj.__dict__ else getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)


class Sandbox:
    """Temporary DB, vault and project. Module globals are the injection point.

    `kick_dispatch` is disabled: without that, every admission in a test
    really started `talos-dispatch.service` on the machine.
    """

    def __enter__(self):
        from talos import store
        from talos import webhookd
        from talos import worktree

        self.root = Path(tempfile.mkdtemp(prefix="orq-test-"))
        self.store = store
        self.webhookd = webhookd
        self.worktree = worktree
        self._saved = (store.DB_PATH, store.RAW_DIR, webhookd.kick_dispatch, webhookd.CFG,
                       worktree.WORKTREES_DIR, worktree.WORKSPACES_DIR, worktree._docker)
        store.DB_PATH = self.root / "events.db"
        store.RAW_DIR = self.root / "raw"
        # Nothing a test runs may land in the real ~/.orchestrator.
        worktree.WORKTREES_DIR = self.root / "worktrees"
        worktree.WORKSPACES_DIR = self.root / "workspaces"
        # No real `docker ps` or `docker rm -f`: the sweep and the start of
        # each pass would kill the machine's harness containers.
        # Each call lands in `docker`, together with whether the run's
        # worktree still existed at that moment.
        self.docker: list[tuple[tuple, bool]] = []
        self.docker_ps = ""

        def fake_docker(*args, timeout=60):
            name = args[-1] if args and args[0] == "rm" else ""
            alive = bool(name.startswith("orq-") and (self.root / "worktrees" / name[4:]).exists())
            self.docker.append((args, alive))
            out = self.docker_ps if args and args[0] == "ps" else ""
            return subprocess.CompletedProcess(["docker", *args], 0, stdout=out, stderr="")

        worktree._docker = fake_docker
        webhookd.kick_dispatch = lambda: None
        store.init_db()
        self.vault = self.root / "vault"
        (self.vault / "ToDos").mkdir(parents=True)
        self.project = self.root / "proj"
        (self.project / "sandbox" / ".git").mkdir(parents=True)
        return self

    def cfg(self, **repo) -> dict:
        """Full config, the kind load_config() builds."""
        rc = {"project": "sandbox", "subrepo": "sandbox", "base_branch": "developer",
              "enabled": True, "mode": "live", "activated_at": "2020-01-01T00:00:00Z", **repo}
        return {
            "vault": str(self.vault), "briefs_dir": "ToDos", "memory_dir": "agentes/memoria",
            "projects": {"sandbox": str(self.project)}, "repos": {REPO: rc},
            "claude_timeout_seconds": 60, "timeouts": {}, "reconcile_interval_minutes": 0,
            "global_daily_cap": 10, "secrets": {},
        }

    def __exit__(self, *exc):
        (self.store.DB_PATH, self.store.RAW_DIR,
         self.webhookd.kick_dispatch, self.webhookd.CFG,
         self.worktree.WORKTREES_DIR, self.worktree.WORKSPACES_DIR,
         self.worktree._docker) = self._saved
        shutil.rmtree(self.root, ignore_errors=True)


class FakeTarget:
    def __init__(self, cfg: dict | None = None, dry: bool = False):
        self.gh_repo = REPO
        self.project = "sandbox"
        self.subrepo = "sandbox"
        self.project_path = Path("/tmp/proj-sandbox")
        self.service_path = ""
        self.base_branch = "developer"
        self.cfg = cfg or {}
        self.enabled = True
        self.dry_run = dry


def _issue(number: int = 7, **kw) -> dict:
    return {
        "number": number, "title": "something broken", "body": "details",
        "created_at": "2026-09-21T10:00:00Z", "html_url": f"http://x/{number}",
        "author_association": "OWNER", "user": {"login": "octo-owner", "type": "User"},
        "labels": [], **kw,
    }


def _review(number: int, body: str, kind: str = "review", at: str = "2026-09-21T10:00:00Z",
            head: str = "", labels=None, login: str = "octo-owner", user_type: str = "User",
            state: str = "commented") -> dict:
    item = {"body": body, "submitted_at": at, "created_at": at, "state": state}
    return {
        "action": "submitted" if kind == "review" else "created",
        "pull_request": {"number": number, "html_url": f"http://x/pull/{number}",
                         "title": "fix", "head": {"ref": head},
                         "labels": LABEL if labels is None else labels},
        "sender": {"login": login, "type": user_type},
        ("review" if kind == "review" else "comment"): item,
        "repository": {"full_name": REPO},
    }


def _event_kind(kind: str) -> str:
    return "pull_request_review" if kind == "review" else "pull_request_review_comment"


def _write_brief(path: Path, meta: dict, body: str = "# x\n") -> Path:
    import frontmatter

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter.dumps(frontmatter.Post(body, **meta)))
    return path


def _brief(path: Path):
    from talos import dispatch
    import frontmatter

    post = frontmatter.load(path)
    return dispatch.Brief(path=path, project=str(post.metadata.get("project")),
                          priority="medium", body=post.content, metadata=dict(post.metadata))


# ── Critical ─────────────────────────────────────────────────────────────────


def test_issue_body_cannot_inline_host_files() -> None:
    """C1: an issue body cannot paste host files into the prompt."""
    print("\nC1 — file inlining only for manual briefs")
    from talos import dispatch

    with Sandbox() as sb:
        secret = sb.root / "secrets.env"
        secret.write_text("GITHUB_WEBHOOK_SECRET=must-not-leak\n")
        body = f"The error is described in {secret}\n"
        memory = sb.root / "mem.md"
        memory.write_text("")

        issue = sb.root / "issue.md"
        _write_brief(issue, {"project": "sandbox", "pipeline": "issue-fix", "source": "github"}, body)
        prompt = dispatch.build_prompt(memory, _brief(issue))
        check("an issue brief does not inline the file", "must-not-leak" not in prompt)

        sentry = sb.root / "sentry.md"
        _write_brief(sentry, {"project": "sandbox", "source": "sentry"}, body)
        check("a Sentry one does not either",
              "must-not-leak" not in dispatch.build_prompt(memory, _brief(sentry)))

        manual = sb.root / "manual.md"
        _write_brief(manual, {"project": "sandbox"}, body)
        check("a manual one does",
              "must-not-leak" in dispatch.build_prompt(memory, _brief(manual)))


def test_git_auth_never_touches_disk() -> None:
    """C2: git auth goes through the environment, and the host remote survives the run."""
    print("\nC2 — GH_TOKEN never in .git/config")
    from talos import dispatch

    with Sandbox() as sb:
        env = {**os.environ, **dispatch.GIT_HTTPS_ENV, "GH_TOKEN": "test-token",
               "GIT_TERMINAL_PROMPT": "0", "HOME": str(sb.root)}
        url = subprocess.run(
            ["git", "ls-remote", "--get-url", "git@github.com:your-org/sandbox.git"],
            capture_output=True, text=True, env=env, cwd=sb.root,
        ).stdout.strip()
        check("the SSH URL is rewritten to HTTPS",
              url == "https://github.com/your-org/sandbox.git", url)
        fill = subprocess.run(
            ["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
            capture_output=True, text=True, env=env, cwd=sb.root,
        ).stdout
        check("the helper hands over the token from the environment",
              "password=test-token" in fill and "username=x-access-token" in fill, fill)
        check("and the token is in no GIT_CONFIG_* value",
              all("test-token" not in v for v in dispatch.GIT_HTTPS_ENV.values()))

        repo = sb.root / "repo"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        ssh = "git@github.com:your-org/sandbox.git"
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", ssh], check=True)
        before = dispatch._git_origin(repo)
        # What the old template did, inside the container:
        subprocess.run(["git", "-C", str(repo), "remote", "set-url", "origin",
                        "https://x-access-token:test-token@github.com/a/b.git"], check=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "other",
                        "https://x-access-token:test-token@github.com/c/d.git"], check=True)
        dispatch.guard_git_remote(repo, before)
        check("origin goes back to SSH", dispatch._git_origin(repo) == ssh)
        check("and no token is left in .git/config",
              "test-token" not in (repo / ".git" / "config").read_text())


def test_dispatch_caps() -> None:
    """C3/D1: the caps defer stage A in dispatch, without touching review-fix."""
    print("\nC3 — load caps in dispatch")
    from talos import dispatch
    from talos import store
    from talos import triage

    with Sandbox() as sb:
        cfg = sb.cfg(daily_cap=1, cooldown_minutes=0, max_open_auto_prs=3)
        issue = _write_brief(sb.vault / "ToDos" / "a.md", {
            "project": "sandbox", "status": "pending", "pipeline": "issue-fix",
            "event_id": 99, "gh_repo": REPO,
        })
        review = _write_brief(sb.vault / "ToDos" / "b.md", {
            "project": "sandbox", "status": "pending", "pipeline": "review-fix",
            "event_id": 98, "gh_repo": REPO,
        })
        manual = _write_brief(sb.vault / "ToDos" / "c.md", {"project": "sandbox", "status": "pending"})

        with patched(triage, "_gh_json", lambda *a, **k: []):
            check("no runs today, it passes", dispatch.cap_verdict(cfg, _brief(issue)).admitted)
            with store.connect() as c:
                eid, _ = store.upsert_event(c, "gh:x#1", {"gh_repo": REPO, "pipeline": "issue-fix",
                                                          "source": "github", "state": "admitted"})
                store.set_state(c, eid, "running")
            v = dispatch.cap_verdict(cfg, _brief(issue))
            check("with one run today and daily_cap 1, it defers", v.gate == "daily_cap", repr(v))
            check("review-fix is not held back by caps", dispatch.cap_verdict(cfg, _brief(review)).admitted)
            check("a manual brief is not either", dispatch.cap_verdict(cfg, _brief(manual)).admitted)

            cfg2 = sb.cfg(daily_cap=5, cooldown_minutes=60)
            v = dispatch.cap_verdict(cfg2, _brief(issue))
            check("cooldown: the run that just happened holds back the next one", v.gate == "cooldown", repr(v))

        cfg3 = sb.cfg(daily_cap=5, cooldown_minutes=0, max_open_auto_prs=2)
        with patched(triage, "_gh_json", lambda *a, **k: [{"number": 1}, {"number": 2}]):
            v = dispatch.cap_verdict(cfg3, _brief(issue))
            check("with 2 open PRs and cap 2, it defers", v.gate == "open_pr_cap", repr(v))
        with patched(triage, "_gh_json", lambda *a, **k: None):
            v = dispatch.cap_verdict(cfg3, _brief(issue))
            check("if gh fails, it defers (does not admit by default)", not v.admitted, repr(v))

        calls = []
        with patched(triage, "_gh_json", lambda *a, **k: [{"number": 1}, {"number": 2}]), \
             patched(dispatch, "process_brief", lambda b, c, dry_run: calls.append(b.path.name)), \
             patched(dispatch, "LOCK_FILE", sb.root / "dispatch.lock"), \
             patched(dispatch, "is_paused", lambda: False):
            dispatch.run_once(cfg3)
        check("run_once runs the ones that can and leaves the deferred one",
              sorted(calls) == ["b.md", "c.md"], str(calls))
        import frontmatter
        check("the deferred one is still pending", frontmatter.load(issue).metadata["status"] == "pending")


# ── Decisions ────────────────────────────────────────────────────────────────


def test_stage_a_enqueues_stage_b() -> None:
    """D2: if the automated review left `patch`, dispatch enqueues round 1."""
    print("\nD2 — stage A enqueues stage B")
    from talos import dispatch
    from talos import feedback
    import frontmatter
    from talos import store

    with Sandbox() as sb:
        cfg = sb.cfg()
        with store.connect() as c:
            eid, _ = store.upsert_event(c, f"gh:{REPO}#7", {
                "source": "github", "project": "sandbox", "gh_repo": REPO, "gh_issue": 7,
                "pipeline": "issue-fix", "state": "admitted"})
        path = _write_brief(sb.vault / "ToDos" / "2026-09-21-github-7-bug.md", {
            "project": "sandbox", "status": "pending", "pipeline": "issue-fix",
            "source": "github", "event_id": eid, "gh_repo": REPO, "gh_issue": 7,
        })
        pr_data = {"status": "ok", "pr_url": f"https://github.com/{REPO}/pull/12", "pr_number": 12,
                   "findings_count": {"patch": 2, "decision_needed": 1}, "threads_opened": 3,
                   "prs": []}
        with patched(dispatch, "run_claude_in_docker", lambda *a: (0, sb.root / "run.log", pr_data)), \
             patched(dispatch, "still_wanted", lambda *a: (True, "", "")), \
             patched(feedback, "announce_pr_open", lambda *a: None):
            res = dispatch.process_brief(_brief(path), cfg, dry_run=False)
        check("stage A ends in pr_open", res["status"] == "pr_open", str(res))
        briefs = list((sb.vault / "ToDos").glob("*review*.md"))
        check("there is a review-fix brief", len(briefs) == 1, str(briefs))
        if briefs:
            meta = frontmatter.load(briefs[0]).metadata
            check("it is round 1", meta.get("round") == 1)
            check("with parent_brief pointing at stage A",
                  Path(meta.get("parent_brief", "")).name == path.name, str(meta.get("parent_brief")))
            check("and pending (it runs in this same pass)", meta.get("status") == "pending")
        with store.connect() as c:
            row = store.find_by_dedupe(c, f"pr:{REPO}#12")
            check("the ledger counts one round", row is not None and row["rounds"] == 1)

        pr_data2 = {**pr_data, "pr_number": 13, "findings_count": {"patch": 0}}
        path2 = _write_brief(sb.vault / "ToDos" / "2026-09-21-github-8-other.md", {
            "project": "sandbox", "status": "pending", "pipeline": "issue-fix",
            "source": "github", "gh_repo": REPO, "gh_issue": 8,
        })
        with patched(dispatch, "run_claude_in_docker", lambda *a: (0, sb.root / "run.log", pr_data2)), \
             patched(dispatch, "still_wanted", lambda *a: (True, "", "")), \
             patched(feedback, "announce_pr_open", lambda *a: None):
            dispatch.process_brief(_brief(path2), cfg, dry_run=False)
        with store.connect() as c:
            check("without patch there is no stage B", store.find_by_dedupe(c, f"pr:{REPO}#13") is None)


def test_pause_stops_dispatch_not_admission() -> None:
    """D3: pausing stops dispatch (before each brief) but not admission."""
    print("\nD3 — PAUSED = do not dispatch")
    from talos import dispatch
    from talos import triage
    from talos import util
    from talos import webhookd

    with Sandbox() as sb:
        sentinel = sb.root / "PAUSED"
        sentinel.touch()
        webhookd.CFG = sb.cfg()
        with patched(util, "PAUSED_FILE", str(sentinel)):
            res = webhookd._handle_gh_issue(FakeTarget({"activated_at": "2020-01-01T00:00:00Z"}),
                                            "p1", {"action": "opened", "issue": _issue()}, "", "opened")
        check("with PAUSED the issue is still admitted", res["state"] == "admitted", str(res))
        check("and has a brief", bool(res.get("brief")) and Path(res["brief"]).exists())

        for name in ("a.md", "b.md", "c.md"):
            _write_brief(sb.vault / "ToDos" / name, {"project": "sandbox", "status": "pending"})
        answers = iter([False, False, True, True])
        calls = []
        with patched(dispatch, "is_paused", lambda: next(answers)), \
             patched(dispatch, "process_brief", lambda b, c, dry_run: calls.append(b.path.name)), \
             patched(dispatch, "LOCK_FILE", sb.root / "dispatch.lock"), \
             patched(triage, "_gh_json", lambda *a, **k: []):
            dispatch.run_once(sb.cfg())
        check("a /pause halfway through the pass stops the ones that follow", len(calls) == 1, str(calls))


def test_hardened_home_is_disposable() -> None:
    """D4: the hardened container does not see the host's settings, hooks or MCP."""
    print("\nD4 — disposable ~/.claude")
    from talos import dispatch

    with Sandbox() as sb:
        fake_home = sb.root / "home"
        (fake_home / ".claude").mkdir(parents=True)
        (fake_home / ".claude" / "settings.json").write_text('{"hooks": {}}')
        (fake_home / ".claude" / ".credentials.json").write_text('{"token": "old"}')
        (fake_home / ".claude.json").write_text(
            '{"oauthAccount": {"x": 1}, "mcpServers": {"evil": {}}, "hasCompletedOnboarding": true}')
        with patched(dispatch, "AGENT_HOMES_DIR", sb.root / "agent-home"), \
             patched(Path, "home", classmethod(lambda cls: fake_home)):
            home, mounts, env, finish = dispatch.prepare_agent_home({"secrets": {}}, "/home/agent", "r1")
            import json
            slim = json.loads((home / ".claude.json").read_text())
            check("~/.claude.json without mcpServers", "mcpServers" not in slim and "oauthAccount" in slim)
            check("without the host's settings.json", not (home / ".claude" / "settings.json").exists())
            check("the host's ~/.claude is not mounted",
                  not any(str(fake_home / ".claude") + ":" in m for m in mounts), str(mounts))
            (home / ".claude" / ".credentials.json").write_text('{"token": "refreshed"}')
            finish()
            check("a refreshed token goes back to the host",
                  "refreshed" in (fake_home / ".claude" / ".credentials.json").read_text())

            os.environ["ORQ_TEST_TOKEN"] = "sk-oat-test"
            try:
                _, mounts2, env2, _ = dispatch.prepare_agent_home(
                    {"secrets": {"claude_oauth_token_env": "ORQ_TEST_TOKEN"}}, "/home/agent", "r2")
            finally:
                del os.environ["ORQ_TEST_TOKEN"]
            check("with a configured token it goes through the environment",
                  env2.get("CLAUDE_CODE_OAUTH_TOKEN") == "sk-oat-test")


def test_sentry_event_alert() -> None:
    """D5/P10/P11/P12: event_alert gets in without its own threshold, with no GETs in the handler."""
    print("\nD5 — Sentry via event_alert")
    import time

    import frontmatter
    from talos import store
    from talos import triage
    from talos import webhookd

    check("issue.created is no longer accepted", not triage.gate_sentry_resource("issue", str(int(time.time()))).admitted)
    check("event_alert with a current epoch timestamp passes",
          triage.gate_sentry_resource("event_alert", str(int(time.time()))).admitted)
    v = triage.gate_sentry_resource("event_alert", str(int(time.time()) - 3600))
    check("an epoch from an hour ago is a replay (P10)", v.gate == "stale_timestamp", repr(v))

    with Sandbox() as sb:
        cfg = sb.cfg(service_path="repo-svc")
        cfg["sentry"] = {"org": "o", "projects": {"sb": {
            "repo": REPO, "project_id": "42", "enabled": True,
            "environments": ["production"], "min_level": "error"}}}
        webhookd.CFG = cfg
        payload = {"action": "triggered", "data": {"event": {
            "project": 42, "issue_id": "777", "title": "Boom", "level": "error",
            "tags": [["environment", "production"]], "web_url": "https://sentry/777"}}}
        res = webhookd.handle_sentry("event_alert", "s1", payload, "")
        check("it is admitted, resolving the project by id", res["state"] == "admitted", str(res))
        with store.connect() as c:
            row = store.find_by_dedupe(c, "sentry:o/sb/777")
            check("with the config slug in the dedupe_key", row is not None)
            check("and the repo's service_path intact (P11)",
                  row is not None and row["service_path"] == "repo-svc",
                  row["service_path"] if row else "")
        if res.get("brief"):
            post = frontmatter.load(res["brief"])
            check("the brief waits for dispatch's enrichment",
                  post.metadata.get("sentry_context") == "webhook"
                  and "sentry-context:start" in post.content)

        no_id = {"data": {"event": {"project": 42, "title": "x", "tags": []}}}
        res = webhookd.handle_sentry("event_alert", "s2", no_id, "")
        check("without an issue id it is rejected (P12)", res["state"] == "rejected_no_issue_id", str(res))


def test_dry_run_rows_come_back_live() -> None:
    """D6: what came in during dry run is re-admitted when the repo goes live."""
    print("\nD6 — dry_run → live")
    import frontmatter
    from talos import store
    from talos import webhookd

    with Sandbox() as sb:
        webhookd.CFG = sb.cfg()
        rc = {"activated_at": "2020-01-01T00:00:00Z"}
        payload = {"action": "opened", "issue": _issue()}
        first = webhookd._handle_gh_issue(FakeTarget(rc, dry=True), "d1", payload, "", "opened")
        check("in dry run it ends in state dry_run", first["state"] == "dry_run", str(first))
        check("with a draft brief", frontmatter.load(first["brief"]).metadata["status"] == "draft")
        again = webhookd._handle_gh_issue(FakeTarget(rc, dry=True), "d2", payload, "", "labeled")
        check("in dry run it does not write another draft", again["state"] == "duplicate", str(again))

        live = webhookd._handle_gh_issue(FakeTarget(rc), "d3", payload, "", "labeled")
        check("in live it is re-admitted", live["state"] == "admitted", str(live))
        check("with a pending brief", frontmatter.load(live["brief"]).metadata["status"] == "pending")
        check("and the draft leaves ToDos/", len(list((sb.vault / "ToDos").glob("*.md"))) == 1)
        check("archived in dry-run/",
              (sb.vault / "projects" / "sandbox" / "dry-run" / Path(first["brief"]).name).exists())
        with store.connect() as c:
            known = store.known_issue_numbers(c, REPO, reopen=store.REOPENABLE_STATES)
            check("the reconciler no longer sees it as reopenable", 7 in known)


# ── Patches ──────────────────────────────────────────────────────────────────


def test_review_burst_opens_one_round() -> None:
    """P1/P13/P15: a review with N inline comments opens ONE round."""
    print("\nP1 — review burst → one round")
    import frontmatter
    from talos import store
    from talos import webhookd

    with Sandbox() as sb:
        webhookd.CFG = sb.cfg()
        target = FakeTarget({"max_review_fix_rounds": 3})
        with store.connect() as c:
            store.upsert_event(c, f"gh:{REPO}#7", {
                "source": "github", "gh_repo": REPO, "gh_issue": 7, "pipeline": "issue-fix",
                "state": "pr_open", "brief_path": str(sb.vault / "in-review" / "2026-09-21-github-7-bug.md")})
        head = "2026-09-21-github-7-bug"
        deliveries = [
            ("r1", "review", "Please look at this", "2026-09-21T10:00:00Z"),
            ("r2", "comment", "not convinced by this name", "2026-09-21T10:00:01Z"),
            ("r3", "comment", "and a test is missing", "2026-09-21T10:00:02Z"),
        ]
        results = []
        for did, kind, body, at in deliveries:
            payload = _review(12, body, kind, at, head=head)
            results.append(webhookd._handle_gh_review(target, did, payload, "", _event_kind(kind),
                                                      payload["action"]))
        states = [r["state"] for r in results]
        check("the first one opens the round, the others stay in flight",
              states == ["admitted", "in_flight", "in_flight"], str(states))
        with store.connect() as c:
            row = store.find_by_dedupe(c, f"pr:{REPO}#12")
            check("a single round counted", row["rounds"] == 1, str(row["rounds"]))
            check("the mark moves to the last absorbed comment",
                  row["last_comment_at"] == "2026-09-21T10:00:02Z", row["last_comment_at"])
        briefs = list((sb.vault / "ToDos").glob("*review*.md"))
        check("a single brief", len(briefs) == 1, str(briefs))
        if briefs:
            post = frontmatter.load(briefs[0])
            check("with parent_brief (P13)",
                  Path(post.metadata.get("parent_brief", "")).stem == head)
            check("and the untrusted-data fence (P15)", "UNTRUSTED DATA" in post.content)

        with store.connect() as c:
            store.set_state(c, row["id"], "running", claimed_at="2026-09-21T11:00:00Z")
        late = webhookd._handle_gh_review(
            target, "r4", _review(12, "something else", "comment", "2026-09-21T12:00:00Z", head=head),
            "", "pull_request_review_comment", "created")
        with store.connect() as c:
            row = store.find_by_dedupe(c, f"pr:{REPO}#12")
        check("a comment after the start does not touch the mark",
              late["state"] == "in_flight" and row["last_comment_at"] == "2026-09-21T10:00:02Z")
        check("and does not overwrite running", row["state"] == "running")


def test_review_gates() -> None:
    """P2/P3/P14/bug 2: harness PRs only, no empty body, audited, owner not filtered out."""
    print("\nP2/P3 — review gates")
    from talos import store
    from talos import webhookd

    with Sandbox() as sb:
        webhookd.CFG = sb.cfg()
        target = FakeTarget()
        res = webhookd._handle_gh_review(target, "g1", _review(20, "change this", labels=[]),
                                         "", "pull_request_review", "submitted")
        check("a PR without the harness label does not trigger (P2)",
              res["state"] == "rejected_not_harness_pr", str(res))
        with store.connect() as c:
            d = c.execute("SELECT verdict FROM deliveries WHERE delivery_id='g1'").fetchone()
        check("and the rejection is recorded in deliveries (P14)", d is not None and d["verdict"] == res["state"])

        res = webhookd._handle_gh_review(target, "g2", _review(20, ""), "",
                                         "pull_request_review", "submitted")
        check("a review with an empty body does not trigger (P3)", res["state"] == "rejected_empty_body", str(res))
        res = webhookd._handle_gh_review(target, "g3", _review(20, "LGTM", state="approved"), "",
                                         "pull_request_review", "submitted")
        check("an approve does not trigger", res["state"] == "rejected_approved", str(res))

        with patched(webhookd, "SELF_LOGIN", "octo-owner"):
            res = webhookd._handle_gh_review(target, "g4", _review(20, "a test is missing"), "",
                                             "pull_request_review", "submitted")
        check("the token owner can still ask for changes (bug 2)",
              res["state"] == "admitted", str(res))


def test_merge_moves_the_brief() -> None:
    """The bug: the ledger said `done` and the brief stayed in in-review/.

    And the hard case is the PR nobody commented on: the `pr:` key never
    existed, so the brief has to be reached by the branch name.
    """
    print("\nmerge → brief to completed/")
    from talos import store
    from talos import webhookd

    with Sandbox() as sb:
        branch = "2026-09-21-github-1-bug"
        brief = sb.vault / "projects" / "sandbox" / "in-review" / f"{branch}.md"
        brief.parent.mkdir(parents=True)
        brief.write_text("---\nproject: sandbox\nstatus: pr_open\n---\n\n# x\n")
        webhookd.CFG = {"vault": str(sb.vault)}

        with store.connect() as c:
            eid, _ = store.upsert_event(c, f"gh:{REPO}#1", {
                "source": "github", "project": "sandbox", "gh_issue": 1, "pipeline": "issue-fix",
                "gh_repo": REPO, "state": "pr_open", "brief_path": str(brief),
            })

        payload = {
            "action": "closed",
            "pull_request": {"number": 2, "merged": True, "html_url": "http://x/2",
                             "head": {"ref": branch}, "labels": LABEL},
            "repository": {"full_name": REPO},
        }
        webhookd._handle_gh_merged(FakeTarget(), "t:merged", payload, "", "closed")

        moved = sb.vault / "projects" / "sandbox" / "completed" / brief.name
        check("the brief reaches completed/", moved.exists())
        check("it is not left in in-review/", not brief.exists())
        with store.connect() as c:
            row = store.get_event(c, eid)
            check("the ledger ends in done", row["state"] == "done")
            check("with the new path", row["brief_path"] == str(moved))


def test_merge_after_review_round_closes_both() -> None:
    """P4/P8: with a review-fix round, the merge also closes stage A."""
    print("\nP4 — merge with rounds closes both rows")
    from talos import store
    from talos import webhookd

    with Sandbox() as sb:
        webhookd.CFG = {"vault": str(sb.vault)}
        branch = "2026-09-21-github-5-fix-login"
        a = _write_brief(sb.vault / "projects" / "sandbox" / "in-review" / f"{branch}.md",
                         {"project": "sandbox", "status": "pr_open"})
        b = _write_brief(sb.vault / "projects" / "sandbox" / "completed" / "2026-09-21-review-pr-9.md",
                         {"project": "sandbox", "status": "done"})
        other = _write_brief(sb.vault / "projects" / "sandbox" / "in-review" / "2026-09-20-github-12-fix-login.md",
                             {"project": "sandbox", "status": "pr_open"})
        with store.connect() as c:
            ea, _ = store.upsert_event(c, f"gh:{REPO}#5", {
                "project": "sandbox", "gh_repo": REPO, "pipeline": "issue-fix", "state": "pr_open",
                "brief_path": str(a), "source": "github"})
            eb, _ = store.upsert_event(c, f"pr:{REPO}#9", {
                "project": "sandbox", "gh_repo": REPO, "pipeline": "review-fix", "state": "done",
                "brief_path": str(b), "source": "github"})
            eo, _ = store.upsert_event(c, f"gh:{REPO}#12", {
                "project": "sandbox", "gh_repo": REPO, "pipeline": "issue-fix", "state": "pr_open",
                "brief_path": str(other), "source": "github"})
        payload = {"action": "closed", "pull_request": {
            "number": 9, "merged": True, "html_url": "http://x/9", "head": {"ref": branch},
            "labels": LABEL}, "repository": {"full_name": REPO}}
        webhookd._handle_gh_merged(FakeTarget(), "m1", payload, "", "closed")
        with store.connect() as c:
            check("the issue row ends in done", store.get_event(c, ea)["state"] == "done")
            check("the PR row too", store.get_event(c, eb)["state"] == "done")
            check("another issue's row with a similar branch is not touched (P8)",
                  store.get_event(c, eo)["state"] == "pr_open")
        check("the issue's brief leaves in-review/", not a.exists())

        payload2 = {**payload, "pull_request": {**payload["pull_request"], "number": 30, "labels": []}}
        res = webhookd._handle_gh_merged(FakeTarget(), "m2", payload2, "", "closed")
        check("merging someone else's PR closes nothing", res["state"] == "rejected_not_harness_pr")


def test_failures_reach_ledger_and_source() -> None:
    """P5/P6: exit≠0 and `status: failed` close the ledger and report on the issue."""
    print("\nP5/P6 — failures to the ledger and to the source")
    from talos import dispatch
    from talos import feedback
    from talos import store

    with Sandbox() as sb:
        cfg = sb.cfg()
        announced = []
        for i, (rc, pr_data, why) in enumerate([
            (1, {"status": "ok", "prs": []}, "exit≠0"),
            (124, {"status": "ok", "prs": []}, "timeout"),
            (0, {"status": "failed", "summary": "could not reproduce", "prs": [],
                 "pr_url": f"https://github.com/{REPO}/pull/3"}, "status failed"),
        ]):
            with store.connect() as c:
                eid, _ = store.upsert_event(c, f"gh:{REPO}#{40 + i}", {
                    "gh_repo": REPO, "gh_issue": 40 + i, "pipeline": "issue-fix",
                    "state": "admitted", "source": "github"})
            path = _write_brief(sb.vault / "ToDos" / f"f{i}.md", {
                "project": "sandbox", "status": "pending", "pipeline": "issue-fix",
                "event_id": eid, "gh_repo": REPO, "gh_issue": 40 + i})
            with patched(dispatch, "run_claude_in_docker", lambda *a, _r=rc, _p=pr_data: (_r, sb.root / "l", _p)), \
                 patched(dispatch, "still_wanted", lambda *a: (True, "", "")), \
                 patched(feedback, "announce_failure", lambda *a: announced.append(a[1].get("gh_issue"))), \
                 patched(feedback, "announce_pr_open", lambda *a: None):
                res = dispatch.process_brief(_brief(path), cfg, dry_run=False)
            with store.connect() as c:
                row = store.get_event(c, eid)
            check(f"{why}: the ledger ends in failed", row["state"] == "failed", row["state"])
            check(f"{why}: it is not treated as a success", res["status"] == "failed", res["status"])
        check("and all three issues are notified", announced == [40, 41, 42], str(announced))


def test_brief_write_failure_is_retryable() -> None:
    """P7: if writing the brief fails, GitHub's retry tries again."""
    print("\nP7 — failure writing the brief")
    from talos import brief_factory
    from talos import store
    from talos import webhookd

    with Sandbox() as sb:
        webhookd.CFG = sb.cfg()
        target = FakeTarget({"activated_at": "2020-01-01T00:00:00Z"})
        payload = {"action": "opened", "issue": _issue()}

        def boom(*a, **k):
            raise OSError("disk full")

        raised = False
        with patched(brief_factory, "write_brief", boom):
            try:
                webhookd._handle_gh_issue(target, "w1", payload, "", "opened")
            except OSError:
                raised = True
        check("the exception propagates (→ 500, GitHub retries)", raised)
        with store.connect() as c:
            row = store.find_by_dedupe(c, f"gh:{REPO}#7")
            gone = c.execute("SELECT 1 FROM deliveries WHERE delivery_id='w1'").fetchone() is None
        check("the row ends in brief_error, out of the active ones", row["state"] == "brief_error")
        check("and the delivery is undone", gone)
        retry = webhookd._handle_gh_issue(target, "w1", payload, "", "opened")
        check("the retry with the same delivery id gets in", retry["state"] == "admitted", str(retry))


def test_negative_content_length() -> None:
    """P9: a negative Content-Length cannot read until EOF before the HMAC."""
    print("\nP9 — negative Content-Length")
    from http.server import ThreadingHTTPServer

    from talos import webhookd

    srv = ThreadingHTTPServer(("127.0.0.1", 0), webhookd.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with socket.create_connection(srv.server_address, timeout=5) as s:
            s.sendall(b"POST /gh HTTP/1.1\r\nHost: x\r\nContent-Length: -1\r\n\r\n")
            status = s.recv(64).split(b"\r\n")[0]
        check("answers 413 without reading the body", b" 413 " in status, status.decode(errors="replace"))
    finally:
        srv.shutdown()


def test_revalidate_before_dispatch() -> None:
    """P16: an issue closed or labeled `ai-skip` after admission does not run."""
    print("\nP16 — revalidation before the container")
    from talos import dispatch
    from talos import store

    with Sandbox() as sb:
        cfg = sb.cfg(skip_label="ai-skip")
        with store.connect() as c:
            eid, _ = store.upsert_event(c, f"gh:{REPO}#50", {
                "gh_repo": REPO, "gh_issue": 50, "pipeline": "issue-fix", "state": "admitted",
                "source": "github"})
        path = _write_brief(sb.vault / "ToDos" / "s.md", {
            "project": "sandbox", "status": "pending", "pipeline": "issue-fix",
            "event_id": eid, "gh_repo": REPO, "gh_issue": 50})
        ran = []
        with patched(dispatch, "_gh_api", lambda *a, **k: {"state": "open", "labels": [{"name": "ai-skip"}]}), \
             patched(dispatch, "run_claude_in_docker", lambda *a: ran.append(1)), \
             patched(shutil, "which", lambda *_: "/usr/bin/gh"):
            res = dispatch.process_brief(_brief(path), cfg, dry_run=False)
        check("the container does not start", not ran)
        check("the brief goes to skipped/",
              res["status"] == "skipped" and (sb.vault / "projects" / "sandbox" / "skipped" / "s.md").exists())
        with store.connect() as c:
            check("the ledger says why", store.get_event(c, eid)["state"] == "rejected_optout")

        with patched(dispatch, "_gh_api", lambda *a, **k: None), \
             patched(shutil, "which", lambda *_: "/usr/bin/gh"):
            w, _, _ = dispatch.still_wanted(cfg, _brief(_write_brief(sb.vault / "ToDos" / "t.md", {
                "project": "sandbox", "pipeline": "issue-fix", "event_id": 1,
                "gh_repo": REPO, "gh_issue": 51})))
        check("if gh fails, it defers (None) instead of running", w is None)


def test_telegram_html() -> None:
    """P18: variable text is HTML-escaped, not legacy Markdown."""
    print("\nP18 — Telegram in HTML")
    from talos import dispatch

    sent = {}

    class Resp:
        ok, status_code, text = True, 200, ""

    def fake_post(url, json=None, timeout=None):
        sent.update(json)
        return Resp()

    cfg = {"telegram_bot_token": "t", "telegram_chat_id": "1"}
    with patched(dispatch.requests, "post", fake_post):
        dispatch.notify_telegram(cfg, "brief_with_snake_case.md", "p<x>", "pr_open", None,
                                 {"findings_count": {"decision_needed": 1}, "pr_url": "u"})
    check("parse_mode HTML", sent.get("parse_mode") == "HTML")
    check("with the text escaped", "p&lt;x&gt;" in sent.get("text", "") and "<b>" in sent.get("text", ""))

    with patched(dispatch.requests, "post", fake_post):
        dispatch.notify_telegram(cfg, "b.md", "acme", "merged_dev", None,
                                 {"pr_url": "u", "base": "developer", "prod": "main"})
    text = sent.get("text", "")
    check("merge to integration: 📦, not 🎉, and says when it closes",
          text.startswith("📦") and "🎉" not in text and "<code>developer</code>" in text
          and "once it reaches <code>main</code>" in text, text)
    with patched(dispatch.requests, "post", fake_post):
        dispatch.notify_telegram(cfg, "b.md", "acme", "merged", None,
                                 {"pr_url": "u", "base": "main", "prod": "main"})
    text = sent.get("text", "")
    check("direct merge to prod: 🎉 without 'closes itself'",
          text.startswith("🎉") and "closes itself" not in text and "<code>main</code>" in text, text)


def test_oldest_brief_first() -> None:
    """Same priority runs oldest first; priority still wins."""
    print("\nDispatch order — priority, then oldest first")
    from talos import dispatch

    def b(name: str, priority: str = "medium", created: str | None = None):
        meta = {"created": created} if created else {}
        return dispatch.Brief(path=Path(f"/v/ToDos/{name}.md"), project="p",
                              priority=priority, body="", metadata=meta)

    briefs = [
        b("2026-09-24-github-90-new", created="2026-09-24T18:00:00Z"),
        b("2026-09-24-github-85-old", created="2026-09-24T09:00:00Z"),
        b("2026-09-25-review-pr-89", "high", created="2026-09-25T10:00:00Z"),
        b("2026-09-23-by-hand"),
    ]
    order = [x.path.stem for x in dispatch.sort_by_priority(briefs)]
    check("high first, then medium from oldest to newest",
          order == ["2026-09-25-review-pr-89", "2026-09-23-by-hand",
                    "2026-09-24-github-85-old", "2026-09-24-github-90-new"], str(order))


def test_backfill_counts_admitted() -> None:
    """P19/bug 10: /backfill does not get stuck on the first issue that will never get in."""
    print("\nP19 — backfill counts admitted issues")
    from talos import reconcile
    from talos import webhookd
    from talos.routing import resolve_gh_repo

    with Sandbox() as sb:
        cfg = sb.cfg(activated_at="2026-09-01T00:00:00Z", skip_label="ai-skip")
        webhookd.CFG = cfg
        rows = [
            _issue(1, created_at="2026-01-01T00:00:00Z", labels=[{"name": "ai-skip"}]),
            _issue(2, created_at="2026-01-02T00:00:00Z"),
            _issue(3, created_at="2026-01-03T00:00:00Z"),
        ]
        with patched(reconcile, "fetch_open_issues", lambda *a, **k: rows):
            out = reconcile.backfill(cfg, REPO, 1)
        states = {r["number"]: r["state"] for r in out}
        check("reports the rejected one", states.get(1) == "rejected_optout", str(states))
        check("and admits the next one", states.get(2) == "admitted", str(states))
        check("without going over the count", 3 not in states, str(states))
        check("resolve_gh_repo still resolves the repo", resolve_gh_repo(cfg, REPO) is not None)


def test_reconciler_keeps_bot_gate() -> None:
    """P20: the reconciler passes the author's real type, so the bot gate still holds."""
    print("\nP20 — bots through the reconciler")
    from talos import reconcile
    from talos import webhookd

    with Sandbox() as sb:
        webhookd.CFG = sb.cfg()
        comments = [{"id": 1, "body": "nit: rename", "at": "2026-09-21T10:00:00Z",
                     "login": "coderabbitai[bot]", "type": "Bot",
                     "kind": "pull_request_review_comment"}]
        with patched(reconcile, "human_comments", lambda *a: comments):
            res = reconcile._round_if_pending(webhookd.CFG, FakeTarget(),
                                              {"number": 12, "url": "u", "title": "t", "headRefName": "h"})
        check("a bot does not open a round", res["state"] == "rejected_bot_author", str(res))


def test_disabled_repo_does_not_block() -> None:
    """P21: a broken path in a disabled repo is a warning, not a SystemExit."""
    print("\nP21 — a disabled repo does not break startup")
    from talos.routing import validate_routing

    cfg = {"projects": {"p": "/does/not/exist"},
           "repos": {"a/b": {"project": "p", "base_branch": "main", "enabled": False}}}
    warnings: list[str] = []
    check("no blocking problems", validate_routing(cfg, warnings) == [])
    check("with the warning", len(warnings) == 1, str(warnings))
    cfg["repos"]["a/b"]["enabled"] = True
    check("enabled, it does block", len(validate_routing(cfg)) == 1)


def test_running_manual_brief_is_reaped() -> None:
    """P22/bug 3: a manual brief in `running` with no live run shows up again."""
    print("\nP22 — orphans through run_once")
    from talos import dispatch
    import frontmatter
    from talos import store

    with Sandbox() as sb:
        manual = _write_brief(sb.vault / "ToDos" / "m.md", {"project": "sandbox", "status": "running"})
        with store.connect() as c:
            eid, _ = store.upsert_event(c, "gh:x/y#1", {
                "source": "github", "gh_repo": "x/y", "gh_issue": 1, "state": "admitted"})
            store.set_state(c, eid, "running")
            c.execute("UPDATE events SET claimed_at = '2020-01-01T00:00:00Z' WHERE id = ?", (eid,))
        with patched(dispatch, "LOCK_FILE", sb.root / "dispatch.lock"), \
             patched(dispatch, "is_paused", lambda: False):
            dispatch.run_once(sb.cfg())
        check("the manual brief ends in failed", frontmatter.load(manual).metadata["status"] == "failed")
        with store.connect() as c:
            row = store.get_event(c, eid)
        check("run_once reaps the old event", row["state"] == "failed" and row["last_error"] == "orphaned")


def test_small_fixes() -> None:
    """P23/P24/bug 5: pagination, file names, source detection, exit code."""
    print("\nP23/P24 — minor fixes")
    from talos import dispatch
    from talos import reconcile
    from talos import store
    from talos import webhookd
    from scripts import replay

    check("gh --paginate pages are concatenated",
          reconcile._concat_pages('[1, 2]\n[3]\n[]') == [1, 2, 3])
    with Sandbox() as sb:
        a = store.archive_raw("reconcile:a/b#12", b"1", sb.root)
        b = store.archive_raw("reconcile:a/b1#2", b"2", sb.root)
        check("ids that sanitize the same do not overwrite each other", a != b)
    check("a Sentry payload with installation is Sentry",
          replay.detect_source({"installation": {"uuid": "x"}, "data": {"event": {}}}) == "sentry")
    check("a GitHub one is still GitHub",
          replay.detect_source({"installation": {"id": 1}, "repository": {}}) == "github")
    check("is_error with exit 0 counts as a failure (bug 5)",
          dispatch.effective_returncode(0, {"is_error": True}) == 125)
    check("and an exit≠0 is not overwritten", dispatch.effective_returncode(1, {"is_error": True}) == 1)
    check("tags [[k, v]] and [{key, value}]",
          webhookd._sentry_tags({"tags": [["a", 1], {"key": "b", "value": 2}]}) == {"a": 1, "b": 2})


# ── The earlier ones ─────────────────────────────────────────────────────────


def test_gate_that_clears_gets_a_brief() -> None:
    """The bug: a row without a brief could never get one again.

    An issue held back by `ai-skip` stayed doomed even after you removed the
    label, because admission asked "is this row new?" instead of "did it
    already have a brief?".
    """
    print("\ngate that lifts → brief")
    from talos import webhookd

    with Sandbox() as sb:
        webhookd.CFG = {"vault": str(sb.vault), "briefs_dir": "ToDos"}
        target = FakeTarget({"activated_at": "2020-01-01T00:00:00Z", "skip_label": "ai-skip"})
        issue = _issue(labels=[{"name": "ai-skip"}])
        payload = {"action": "opened", "issue": issue,
                   "repository": {"full_name": target.gh_repo}}

        first = webhookd._handle_gh_issue(target, "d1", payload, "", "opened")
        check("with ai-skip it is rejected", first["state"] == "rejected_optout", str(first))

        issue["labels"] = []
        second = webhookd._handle_gh_issue(target, "d2", payload, "", "unlabeled")
        check("without the label it gets in", second["state"] == "admitted", str(second))
        check("and writes the brief", bool(second.get("brief")) and Path(second["brief"]).exists())

        third = webhookd._handle_gh_issue(target, "d3", payload, "", "labeled")
        check("but does not duplicate it", third["state"] == "duplicate", str(third))


def test_backfill_skips_only_the_cutoff() -> None:
    """/backfill has to skip the backlog cutoff and NOTHING else."""
    print("\nbackfill → skips the cutoff, not the other gates")
    from talos import webhookd

    with Sandbox() as sb:
        webhookd.CFG = {"vault": str(sb.vault), "briefs_dir": "ToDos"}
        target = FakeTarget({"activated_at": "2026-09-21T00:00:00Z", "skip_label": "ai-skip"})
        old_issue = _issue(39, created_at="2026-07-30T04:16:25Z")
        payload = {"action": "opened", "issue": old_issue,
                   "repository": {"full_name": target.gh_repo}}

        blocked = webhookd._handle_gh_issue(target, "b1", payload, "", "opened")
        check("without backfill, the cutoff holds it back", blocked["state"] == "rejected_backlog")

        forced = webhookd._handle_gh_issue(
            target, "b2", payload, "", "opened", skip_gates=frozenset({"backlog"})
        )
        check("with backfill it gets in", forced["state"] == "admitted", str(forced))

        payload2 = {"action": "opened",
                    "issue": _issue(40, created_at="2026-07-30T04:16:25Z", labels=[{"name": "ai-skip"}]),
                    "repository": {"full_name": target.gh_repo}}
        skipped = webhookd._handle_gh_issue(
            target, "b3", payload2, "", "opened", skip_gates=frozenset({"backlog"})
        )
        check("but ai-skip still applies", skipped["state"] == "rejected_optout", str(skipped))


def test_service_map() -> None:
    """In a monorepo, the tag says which service; without a tag, no guessing."""
    print("\nrouting by service tag")
    from talos.routing import resolve_service

    smap = {"service_map": {"coach": "services/coach", "pdi": "services/pdi"}}
    check("a known tag resolves", resolve_service(smap, {"service": "coach"})[0] == "services/coach")
    check("no tag is a problem", resolve_service(smap, {})[1] != "")
    check("an unknown tag is a problem", resolve_service(smap, {"service": "nope"})[1] != "")
    check("without service_map it does not get in the way", resolve_service({"service_path": "x"}, {}) == ("x", ""))


def test_stream_result_beats_exit_code() -> None:
    """`claude -p` exits 0 even if the run crashed."""
    print("\nstream result over the exit code")
    from talos import dispatch

    with Sandbox() as sb:
        log = sb.root / "run.log"
        # Key order deliberately different: parsing cannot depend on it,
        # and the pty leaves stray \r at the end of each line.
        log.write_text(
            '{"type":"system","subtype":"init"}\r\n'
            '{"duration_api_ms":188129,"is_error":true,"type":"result",'
            '"subtype":"success","num_turns":30,"total_cost_usd":1.51}\r\n'
        )
        res = dispatch.read_stream_result(log)
        check("finds the result event", res is not None and res.get("type") == "result")
        check("and reads is_error=true", bool(res and res.get("is_error")))
        check("and that wins over exit 0", dispatch.effective_returncode(0, res) == 125)


def test_anti_loop_marker() -> None:
    """The guard goes by marker, not by author: the harness posts as the owner."""
    print("\nanti-loop by marker")
    from talos import triage
    from talos.util import AUTO_REVIEW_MARKER

    mine = f"**patch** — something\n\n{AUTO_REVIEW_MARKER}"
    check("the harness's own text is rejected", not triage.gate_self_review(mine).admitted)
    check("a human's text gets in", triage.gate_self_review("not convinced by this change").admitted)


def test_bmad_kit_is_ensured() -> None:
    """Without BMAD it is installed remotely (pinned); your own overrides are not overwritten."""
    print("\nBMAD — installed before the container if missing")
    from talos import bmad_kit
    from talos import dispatch
    from talos import feedback
    from talos import store

    installs = []

    def fake_installer(ws: Path) -> str:
        # What bmad-method@6.10.0 install leaves behind, as far as we care.
        installs.append(ws)
        for rel in bmad_kit.BMAD_REQUIRED:
            (ws / rel).parent.mkdir(parents=True, exist_ok=True)
            (ws / rel).write_text("x")
        (ws / bmad_kit.MANIFEST).parent.mkdir(parents=True, exist_ok=True)
        (ws / bmad_kit.MANIFEST).write_text("installation:\n  version: 6.10.0\n")
        return ""

    with Sandbox() as sb, patched(bmad_kit, "install_bmad", fake_installer):
        cfg = sb.cfg()
        (sb.project / ".git" / "info").mkdir(parents=True)
        missing, _ = bmad_kit.check(sb.project)
        check("an empty workspace reports missing files", bool(missing))

        ran, announced = [], []

        def run(n: int, subrepo: str = "sandbox"):
            with store.connect() as c:
                eid, _ = store.upsert_event(c, f"gh:{REPO}#{n}", {
                    "gh_repo": REPO, "gh_issue": n, "pipeline": "issue-fix",
                    "state": "admitted", "source": "github"})
            path = _write_brief(sb.vault / "ToDos" / f"b{n}.md", {
                "project": "sandbox", "subrepo": subrepo, "status": "pending",
                "pipeline": "issue-fix", "event_id": eid, "gh_repo": REPO, "gh_issue": n})
            with patched(dispatch, "run_claude_in_docker",
                         lambda *a: (ran.append(n), (0, sb.root / "l", {"status": "ok", "prs": []}))[1]), \
                 patched(dispatch, "still_wanted", lambda *a: (True, "", "")), \
                 patched(feedback, "announce_failure", lambda *a: announced.append(a[2])), \
                 patched(feedback, "announce_pr_open", lambda *a: None):
                return dispatch.process_brief(_brief(path), cfg, dry_run=False)

        run(60)
        check("installs BMAD and the overrides, and the container runs",
              installs == [sb.project] and ran == [60] and bmad_kit.check(sb.project) == ([], []),
              str(bmad_kit.check(sb.project)))
        check("the headless contract comes from the harness kit",
              (sb.project / "_bmad/custom/headless-contract.md").read_bytes()
              == (bmad_kit.KIT_DIR / "_bmad/custom/headless-contract.md").read_bytes())
        exclude = (sb.project / ".git" / "info" / "exclude").read_text()
        check("what was installed stays out of the agent's commits", "/_bmad/" in exclude)

        run(61)
        check("with BMAD present it does not reinstall", len(installs) == 1 and ran == [60, 61])

        run(63, subrepo="")
        check("without a sub-repo, process_brief installs BMAD in the harness folder",
              installs[-1] == sb.root / "workspaces" / "sandbox" and ran[-1] == 63, str(installs))
        installs.pop()
        ran.pop()

        toml = sb.project / "_bmad" / "custom" / "bmad-quick-dev.toml"
        toml.write_text("[workflow]\n# mine, no hook\n")
        res = run(62)
        check("your own override without a hook is not overwritten", "mine" in toml.read_text())
        check("and the brief fails with the reason instead of doing HALT",
              ran == [60, 61] and res["status"] == "failed" and "HEADLESS" in announced[-1],
              str(announced[-1:]))

        contract = sb.project / "_bmad/custom/headless-contract.md"
        contract.write_text("stale contract")
        check("an outdated contract counts as missing",
              "_bmad/custom/headless-contract.md" in bmad_kit.check(sb.project)[0])
        toml.write_text((bmad_kit.KIT_DIR / "_bmad/custom/bmad-quick-dev.toml").read_text())
        check("ensure resyncs it with the kit", bmad_kit.ensure(sb.project) == ""
              and contract.read_bytes() == (bmad_kit.KIT_DIR / "_bmad/custom/headless-contract.md").read_bytes())
        toml.write_text("[workflow]\n# mine, with hook: headless-contract.md\n")
        bmad_kit.ensure(sb.project)
        check("but your own .toml with a hook is not touched", "mine" in toml.read_text())
        (sb.project / bmad_kit.MANIFEST).write_text("installation:\n  version: 6.12.0\n")
        _, conflicts = bmad_kit.check(sb.project)
        check("BMAD ≥ 6.11 is reported: its quick-dev is a shim that does HALT",
              any("6.12.0" in c for c in conflicts), str(conflicts))


def test_stage_a_must_publish_its_review() -> None:
    """A PR without the published review, or with unpushed commits, is not a success."""
    print("\nStage A — the review has to be on the PR")
    from talos import dispatch

    with Sandbox() as sb:
        repo = sb.project / "sandbox"
        shutil.rmtree(repo / ".git")
        git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, check=True).stdout.strip()
        git("init", "-q", "-b", "br")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "a")
        pushed = git("rev-parse", "HEAD")

        def api(reviews, head):
            def fake(path, timeout=20):
                return reviews if path.endswith("/reviews?per_page=100") else {"head": {"sha": head}}
            return fake

        marker = "summary\n<!-- orquestrator:auto-review -->"
        with patched(dispatch, "_gh_api", api([{"body": marker}], pushed)):
            check("review published and branch pushed: complete",
                  dispatch.verify_stage_a(REPO, 88, repo, "br") == "")
        with patched(dispatch, "_gh_api", api([{"body": "LGTM from a human"}], pushed)):
            why = dispatch.verify_stage_a(REPO, 88, repo, "br")
            check("without the harness review: it fails", "review" in why, why)

        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "b")
        with patched(dispatch, "_gh_api", api([{"body": marker}], pushed)):
            why = dispatch.verify_stage_a(REPO, 88, repo, "br")
            check("unpushed local commit: it fails", "1 unpushed commit(s)" in why, why)
        with patched(dispatch, "_gh_api", lambda *a, **k: None):
            check("if GitHub does not answer, no failure is made up",
                  dispatch.verify_stage_a(REPO, 88, repo, "br") == "")


@contextlib.contextmanager
def captured_logs(logger=None):
    """Records emitted by `logger` (the dispatch logger by default) while the block runs."""
    import logging

    from talos import dispatch

    logger = logger or dispatch.logger
    records: list = []

    class Collect(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = Collect(level=logging.DEBUG)
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


def _messages(records, level: str | None = None) -> list[str]:
    return [r.getMessage() for r in records if level is None or r.levelname == level]


def test_stage_a_thread_count() -> None:
    """One thread per finding: counts that do not add up warn, and never fail the run."""
    print("\nStage A — one thread per finding")
    from talos import dispatch
    from talos import feedback
    import frontmatter

    with Sandbox() as sb:
        cfg = sb.cfg()

        def run(n: int, **counts):
            path = _write_brief(sb.vault / "ToDos" / f"2026-09-24-github-{n}-x.md", {
                "project": "sandbox", "status": "pending", "pipeline": "issue-fix",
                "source": "github", "gh_repo": REPO, "gh_issue": n,
            })
            pr_data = {"status": "ok", "pr_url": f"https://github.com/{REPO}/pull/{n}",
                       "pr_number": n, "prs": [], **counts}
            with patched(dispatch, "run_claude_in_docker", lambda *a: (0, sb.root / "run.log", pr_data)), \
                 patched(dispatch, "still_wanted", lambda *a: (True, "", "")), \
                 patched(dispatch, "enqueue_review_round", lambda *a: None), \
                 patched(feedback, "announce_pr_open", lambda *a: None), \
                 captured_logs() as logs:
                res = dispatch.process_brief(_brief(path), cfg, dry_run=False)
            return res, _messages(logs, "WARNING")

        mismatch = lambda ws: [w for w in ws if "does not add up" in w]
        folded_warn = lambda ws: [w for w in ws if "folded into the roll-up" in w]

        res, warns = run(89, findings_count={"patch": 12, "decision_needed": 0}, threads_opened=11)
        check("patch 12 with 11 threads: warning", len(mismatch(warns)) == 1, str(warns))
        check("with the numbers",
              any("patch 12 + decision_needed 0 = 12" in w
                  and "threads_opened 11 + folded_into_rollup 0 = 11" in w for w in warns), str(warns))
        check("and the run stays in pr_open", res["status"] == "pr_open", str(res))

        res, warns = run(90, findings_count={"patch": 2, "decision_needed": 1},
                         threads_opened=2, folded_into_rollup=1)
        check("threads + folded into the roll-up add up: no count warning",
              mismatch(warns) == [], str(warns))
        check("but a folded finding with patch warns that it needs a human",
              len(folded_warn(warns)) == 1 and "1 finding" in folded_warn(warns)[0]
              and "human" in folded_warn(warns)[0], str(warns))
        moved = sb.vault / "projects" / "sandbox" / "in-review" / "2026-09-24-github-90-x.md"
        check("folded_into_rollup stays in the brief",
              moved.exists() and frontmatter.load(moved).metadata.get("folded_into_rollup") == 1)

        res, warns = run(92, findings_count={"patch": 0, "decision_needed": 1},
                         threads_opened=0, folded_into_rollup=1)
        check("folded without patch: no warnings", warns == [], str(warns))

        res, warns = run(91, findings_count={"patch": 3, "decision_needed": 0, "defer": 4},
                         threads_opened=3)
        check("missing folded_into_rollup counts as 0, and defer does not count", warns == [], str(warns))

        res, warns = run(93, findings_count={"patch": "12", "decision_needed": 0}, threads_opened=12)
        check("a count given as a digit string is valid", warns == [], str(warns))

        for n, bad, shown in ((94, float("inf"), "threads_opened=inf"),
                              (95, True, "threads_opened=True"),
                              (96, -1, "threads_opened=-1"),
                              (97, 2.5, "threads_opened=2.5"),
                              (98, "twelve", "threads_opened='twelve'")):
            res, warns = run(n, findings_count={"patch": 12}, threads_opened=bad)
            check(f"threads_opened={bad!r}: invalid count warning, without crashing",
                  any("Invalid counts" in w and shown in w for w in warns)
                  and res["status"] == "pr_open", str(warns))

        res, warns = run(99, findings_count=["patch"], threads_opened=1)
        check("findings_count that is not an object: warning",
              any("is not an object" in w for w in warns) and res["status"] == "pr_open", str(warns))

        check("_count: ints, integral floats and digits",
              [dispatch._count(v) for v in (0, 3, 3.0, " 7 ", None)] == [0, 3, 3, 7, 0])
        check("_count: everything else is not a count",
              [dispatch._count(v) for v in (True, -1, 2.5, float("nan"), float("inf"), "x", "²", [1])]
              == [None] * 8)

        brief = _brief(_write_brief(sb.root / "e.md", {"project": "sandbox", "gh_repo": REPO}))
        try:
            dispatch.enqueue_review_round(cfg, brief, {"findings_count": {"patch": float("inf")},
                                                       "threads_opened": 1, "pr_number": 89})
            ok = True
        except Exception as e:
            ok = False
            print(f"    {e!r}")
        check("enqueue_review_round with Infinity does not crash", ok)


def test_review_fix_log_summary() -> None:
    """review-fix never opens PRs: the log names the PR it committed to and the threads."""
    print("\nreview-fix — summary log")
    from talos import dispatch

    with Sandbox() as sb:
        rf = _write_brief(sb.root / "rf.md", {"project": "sandbox", "pipeline": "review-fix",
                                              "gh_repo": REPO, "pr_number": 89})
        url = f"https://github.com/{REPO}/pull/89"
        with captured_logs() as logs:
            dispatch.log_pr_summary(_brief(rf), {"prs": [], "pr_url": url,
                                                 "threads_resolved": 11, "threads_open": 1})
        msgs = _messages(logs)
        check("does not say 'none (no committable changes)'",
              not any("none" in m for m in msgs), str(msgs))
        check("shows the PR and the resolved and declined threads",
              any(url in m and "threads resolved: 11" in m and "declined or unresolved: 1" in m
                  for m in msgs), str(msgs))

        with captured_logs() as logs:
            dispatch.log_pr_summary(_brief(rf), {"prs": []})
        msgs = _messages(logs)
        check("without pr_url or threads in the result, it falls back to the brief's PR",
              any(m.endswith(f"on PR {REPO}#89") for m in msgs)
              and not any("none" in m for m in msgs), str(msgs))

        empty = _write_brief(sb.root / "rf-null.md", {"project": "sandbox", "pipeline": "review-fix",
                                                      "gh_repo": None, "pr_number": None})
        with captured_logs() as logs:
            dispatch.log_pr_summary(_brief(empty), {"prs": []})
        msgs = _messages(logs)
        check("null fields in the brief: '?#?', not 'None#None'",
              any(m.endswith("on PR ?#?") for m in msgs)
              and not any("None" in m for m in msgs), str(msgs))

        other = f"https://github.com/{REPO}/pull/90"
        with captured_logs() as logs:
            dispatch.log_pr_summary(_brief(rf), {"pr_url": url, "prs": [
                {"repo": REPO, "url": other, "branch": "b"}]})
        warns = _messages(logs, "WARNING")
        check("an unexpected PR in prs is not hidden",
              len(warns) == 1 and other in warns[0], str(_messages(logs)))

        manual = _write_brief(sb.root / "m.md", {"project": "sandbox"})
        with captured_logs() as logs:
            dispatch.log_pr_summary(_brief(manual), {"prs": []})
        check("the other pipelines do not change",
              any("none (no committable changes)" in m for m in _messages(logs)))


def test_brief_names_the_repo_dir() -> None:
    """Without a sub-repo the brief names the repo folder, not "(project root)"."""
    print("\nBriefs — without a sub-repo they name the repo folder")
    from talos import brief_factory

    target = FakeTarget({"pr_label": "ai-generated"})
    target.subrepo = ""
    target.project_path = Path("/tmp/automate-me")
    block = brief_factory._requirements_block(target, "x")
    check("issue-fix: Touch ONLY the sub-repo `automate-me`",
          "`automate-me`" in block and "root" not in block.replace("root cause", ""), block)
    _, content = brief_factory.build_review_fix_brief({}, target, 5, "u", 1, "k", 1)
    check("review-fix: same", "Touch ONLY the sub-repo `automate-me`" in content)


def test_run_watchdog() -> None:
    """wait_for_run with real processes, and the threshold that comes from config.yaml."""
    print("\nFrozen-run watchdog")
    from talos import dispatch

    with tempfile.TemporaryDirectory() as tmp, patched(dispatch, "STALL_POLL_SECONDS", 0.05):
        log = Path(tmp) / "run.log"

        def watch(script: str, stall: int, timeout: int = 30):
            with open(log, "w") as f:
                f.write("# Run\n")
                f.flush()
                proc = subprocess.Popen(["sh", "-c", script], stdout=f, stdin=subprocess.DEVNULL)
                started = time.monotonic()
                try:
                    return dispatch.wait_for_run(proc, f, timeout, stall), time.monotonic() - started
                finally:
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait()

        cut, took = watch("echo x; sleep 30", stall=1)
        check("a process that goes quiet is cut for stall", cut == "stall" and took < 5, f"{cut} {took:.1f}s")

        cut, took = watch("for i in 1 2 3 4 5 6 7 8; do echo x; sleep 0.3; done", stall=1)
        check("one that writes slower than the poll but faster than the threshold keeps going",
              cut is None and took > 1.5, f"{cut} {took:.1f}s")

        cut, _ = watch("sleep 30", stall=0, timeout=1)
        check("with stall at 0, only the timeout cuts the silence", cut == "timeout", str(cut))

        cut, _ = watch("echo x", stall=1)
        check("a process that exits on its own is not cut", cut is None, str(cut))

        with open(log, "w") as f:
            proc = subprocess.Popen(["sh", "-c", "for i in 1 2 3 4 5 6; do echo x; sleep 0.3; done"],
                                    stdout=f, stdin=subprocess.DEVNULL)
            log.unlink()
            try:
                cut = dispatch.wait_for_run(proc, f, 30, 1)
            finally:
                proc.wait()
        check("deleting the run log by hand does not kill a run that keeps writing", cut is None, str(cut))

        class ExitedDuringCheck:
            """The wait times out and the process exits before the cut is decided."""

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired("docker", timeout)

            def poll(self):
                return 0

        with open(log, "w") as f:
            cut = dispatch.wait_for_run(ExitedDuringCheck(), f, 0, 0)
        check("a run that exited right at the limit is not reported as a timeout", cut is None, str(cut))

        cfg_path = Path(tmp) / "config.yaml"
        for value, expected in (("-5", 1200), ("'20m'", 1200), ("true", 1200), ("0", 0), ("900", 900)):
            cfg_path.write_text(f"vault: {tmp}\nstall_timeout_seconds: {value}\n")
            with patched(dispatch, "CONFIG_PATH", cfg_path), captured_logs() as logs:
                got = dispatch.load_config()["stall_timeout_seconds"]
            check(f"stall_timeout_seconds: {value} → {expected}",
                  got == expected and (expected != 1200 or any("stall_timeout_seconds" in m
                                                               for m in _messages(logs, "WARNING"))),
                  f"{got} {_messages(logs)}")
        cfg_path.write_text(f"vault: {tmp}\n")
        with patched(dispatch, "CONFIG_PATH", cfg_path):
            check("without the key, the default is 20 min", dispatch.load_config()["stall_timeout_seconds"] == 1200)


def test_agent_works_in_a_worktree() -> None:
    """Hardened runs get a worktree from origin/<base>; the main checkout is never touched."""
    print("\nPer-run worktree — the main checkout is not touched")
    from talos import dispatch
    from talos import worktree

    with Sandbox() as sb:
        run = lambda *a, cwd=None: subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd,
            capture_output=True, text=True, check=True).stdout.strip()
        origin = sb.root / "origin.git"
        run("init", "-q", "--bare", "-b", "main", str(origin))
        seed = sb.root / "seed"
        run("clone", "-q", str(origin), str(seed))
        (seed / "a.txt").write_text("a\n")
        run("add", "a.txt", cwd=seed)
        run("commit", "-q", "-m", "a", cwd=seed)
        run("push", "-q", "origin", "HEAD:main", "HEAD:developer", cwd=seed)

        repo = sb.project / "sandbox"
        shutil.rmtree(repo)
        run("clone", "-q", str(origin), str(repo))
        git = lambda *a: run("-C", str(repo), *a)
        git("checkout", "-q", "-b", "my-work")
        (repo / "a.txt").write_text("uncommitted change\n")
        (repo / "new.txt").write_text("untracked\n")
        known = git("rev-parse", "origin/developer")

        # origin/developer gets a merge the local clone does not have yet:
        # the host does not fetch, the agent does, inside.
        (seed / "b.txt").write_text("b\n")
        run("add", "b.txt", cwd=seed)
        run("commit", "-q", "-m", "merge on developer", cwd=seed)
        run("push", "-q", "origin", "HEAD:developer", cwd=seed)

        hook_ran = sb.root / "pwn-hook"
        hook = repo / ".git" / "hooks" / "post-checkout"
        hook.write_text(f"#!/bin/sh\ntouch {hook_ran}\n")
        hook.chmod(0o755)

        wt, why = worktree.create(repo, "developer", "r1")
        check("creates the worktree", wt is not None and wt.path.is_dir(), why)
        check("from the origin/developer the repo already had: the host does not fetch",
              run("-C", str(wt.path), "rev-parse", "HEAD") == known
              and git("rev-parse", "origin/developer") == known)
        check("no checkout on the host: the working tree starts empty",
              sorted(p.name for p in wt.path.iterdir()) == [".git"])
        check("with .git pointing at the git dir by absolute path",
              (wt.path / ".git").read_text().startswith(f"gitdir: {(repo / '.git').resolve()}"))
        run("-C", str(wt.path), "reset", "--hard", "--quiet")
        check("the container's reset --hard fills it", (wt.path / "a.txt").read_text() == "a\n")
        check("with a detached HEAD", run("-C", str(wt.path), "rev-parse", "--abbrev-ref", "HEAD") == "HEAD")
        check("the git dir is the repo's", wt.git_dir == (repo / ".git").resolve(), str(wt.git_dir))
        check("without running the planted post-checkout hook", not hook_ran.exists())
        check("the main checkout stays on its branch",
              git("symbolic-ref", "--short", "HEAD") == "my-work")
        check("with its change and its untracked file",
              (repo / "a.txt").read_text() == "uncommitted change\n" and (repo / "new.txt").exists())

        run("-C", str(wt.path), "checkout", "-q", "-b", "agent-feature")
        run("-C", str(wt.path), "commit", "-q", "--allow-empty", "-m", "fix")
        ro = wt.path / "cache" / "mod"
        ro.mkdir(parents=True)
        (ro / "f").write_text("x")
        ro.chmod(0o555)
        worktree.remove(repo, wt)
        check("remove deletes the folder, even with read-only folders inside", not wt.path.exists())
        check("and the worktree metadata", len(git("worktree", "list").splitlines()) == 1,
              git("worktree", "list"))
        check("the agent's branch stays in the repo", git("branch", "--list", "agent-feature") != "")
        hook.unlink()

        wt, _ = worktree.create(repo, "developer", "detached")
        run("-C", str(wt.path), "commit", "-q", "--allow-empty", "-m", "no branch")
        lost = run("-C", str(wt.path), "rev-parse", "HEAD")
        worktree.remove(repo, wt)
        check("a commit on a detached HEAD is rescued in refs/orq/<run>",
              git("rev-parse", "refs/orq/detached") == lost)

        orphan, _ = worktree.create(repo, "developer", "orphan")
        run("-C", str(orphan.path), "checkout", "-q", "-b", "taken-branch")
        sb.docker.clear()
        wt, why = worktree.create(repo, "developer", "retry")
        check("an orphan worktree from another run is swept when the next one is created",
              wt is not None and not orphan.path.exists(), why)
        check("and its container is killed before it is deleted",
              (("rm", "-f", "orq-orphan"), True) in sb.docker, str(sb.docker))
        run("-C", str(wt.path), "checkout", "-q", "-B", "taken-branch")
        check("and its branch is free for the retry",
              run("-C", str(wt.path), "symbolic-ref", "--short", "HEAD") == "taken-branch")
        worktree.remove(repo, wt)

        locked, _ = worktree.create(repo, "developer", "half-done")
        meta = next((repo / ".git" / "worktrees").iterdir())
        (meta / "locked").write_text("initializing")
        wt, why = worktree.create(repo, "developer", "after-lock")
        check("a `locked` worktree (an add that died halfway) is swept too",
              wt is not None and not locked.path.exists() and not meta.exists(), why)
        worktree.remove(repo, wt)

        # One of your worktrees on an unmounted disk: `worktree prune` would delete it.
        mine = sb.root / "disk" / "mine"
        git("worktree", "add", "-q", "--detach", str(mine))
        shutil.rmtree(mine.parent)
        wt, _ = worktree.create(repo, "developer", "r-prune")
        worktree.remove(repo, wt)
        check("remove only deletes its own worktree's metadata, not yours",
              str(mine) in git("worktree", "list"), git("worktree", "list"))
        git("worktree", "prune")

        # B2: planted metadata pointing at `worktrees/..` with `locked`.
        # Before, the next create did rmtree of ~/.orchestrator.
        (sb.root / "worktrees").mkdir(exist_ok=True)
        (sb.root / "events.db").touch()
        evil = repo / ".git" / "worktrees" / "evil"
        evil.mkdir(parents=True)
        (evil / "gitdir").write_text(f"{sb.root / 'worktrees' / '..' / '.git'}\n")
        (evil / "locked").write_text("x")
        link = sb.root / "worktrees" / "trap"
        link.symlink_to(sb.root)
        evil2 = repo / ".git" / "worktrees" / "evil2"
        evil2.mkdir()
        (evil2 / "gitdir").write_text(f"{link / '.git'}\n")
        wt, why = worktree.create(repo, "developer", "r-b2")
        check("metadata with `worktrees/..` or a symlink does not delete ~/.orchestrator",
              wt is not None and (sb.root / "events.db").exists() and (sb.root / "vault").is_dir(), why)
        worktree.remove(repo, worktree.Worktree(sb.root / "worktrees" / "..", wt.git_dir))
        check("nor does remove with a path like that", (sb.root / "events.db").exists())
        worktree.remove(repo, wt)
        link.unlink()
        shutil.rmtree(evil)
        shutil.rmtree(evil2)

        wt, why = worktree.create(repo, "missing-branch", "r2")
        check("base_branch without origin: creates nothing and says why",
              wt is None and "origin/missing-branch" in why and not (sb.root / "worktrees" / "r2").exists(), why)
        for bad in ("--upload-pack=touch x", "-x", "a..b", ""):
            wt, why = worktree.create(repo, bad, "r2")
            check(f"base_branch {bad!r}: does not reach git", wt is None and "invalid" in why, why)

        # D3: the run's branch is checked out in your checkout.
        wt, why = worktree.create(repo, "developer", "r-d3", branch="my-work")
        check("if the run's branch is checked out in your checkout, it creates nothing",
              wt is None and "my-work" in why and str(repo.resolve()) in why
              and not (sb.root / "worktrees" / "r-d3").exists(), why)

        # B1: everything the repo config could make the host execute.
        pwn = sb.root / "pwn"
        for key, value in (("filter.x.smudge", f"touch {pwn}"),
                           ("include.path", str(sb.root / "other.gitconfig")),
                           ("includeIf.gitdir:/.path", str(sb.root / "other.gitconfig")),
                           ("core.worktree", str(sb.root)),
                           ("core.fsmonitor", f"touch {pwn}"),
                           ("core.sshCommand", f"touch {pwn}"),
                           ("core.askPass", f"touch {pwn}"),
                           ("credential.helper", f"!touch {pwn}"),
                           ("remote.origin.uploadpack", f"touch {pwn}; git-upload-pack"),
                           ("remote.origin.promisor", "true"),
                           ("extensions.partialClone", "origin"),
                           ("url.ext::sh -c touch% /tmp/x.insteadOf", str(origin)),
                           ("protocol.ext.allow", "always"),
                           ("filter.lfs.smudge", f"touch {pwn}")):
            git("config", key, value)
            wt, why = worktree.create(repo, "developer", "r3")
            git("config", "--unset", key)
            check(f"with {key} in the repo config: the worktree is not created",
                  wt is None and key.lower().split(".")[0] in why.lower()
                  and not (sb.root / "worktrees" / "r3").exists() and not pwn.exists(), why)
        for key, value in worktree.PINNED_GIT_CONFIG.items():
            git("config", key, value)
        git("config", "branch.release.1.2.vscode-merge-base", "origin/main")
        git("config", "remote.origin.gh-resolved", "base")
        wt, why = worktree.create(repo, "developer", "r-lfs")
        check("but `git lfs install --local`, gh and VS Code do pass", wt is not None, why)
        worktree.remove(repo, wt)
        git("config", "--remove-section", "filter.lfs")
        check("and the host's git cannot fetch even if it wants to",
              worktree.host_git(repo, "fetch", "-q", "origin").returncode != 0)

        outside = worktree.Worktree(sb.root / "outside", repo / ".git")
        outside.path.mkdir()
        with captured_logs(worktree.logger) as logs:
            worktree.remove(repo, outside)
        check("remove deletes nothing outside WORKTREES_DIR",
              outside.path.exists() and len(_messages(logs, "ERROR")) == 1)

        seen: dict = {}
        real_run = subprocess.run

        def spy_run(cmd, *a, **k):
            seen["env"] = k.get("env")
            return real_run(cmd, *a, **k)

        spy = type("Spy", (), {"run": staticmethod(spy_run)})
        os.environ["GH_TOKEN_TEST_LEAK"] = "s3cret"
        try:
            with patched(worktree, "subprocess", spy):
                worktree.host_git(repo, "status")
        finally:
            del os.environ["GH_TOKEN_TEST_LEAK"]
        check("the host's git runs without dispatch's environment",
              seen.get("env") is not None and "GH_TOKEN_TEST_LEAK" not in seen["env"]
              and "PATH" in seen["env"], str(sorted(seen.get("env") or {})))

        sb.docker_ps = "abc123\ndef456\n"
        sb.docker.clear()
        check("at the start of the pass it kills the live orq-* containers",
              worktree.kill_orphan_containers() == ["abc123", "def456"]
              and [a for a, _ in sb.docker] == [("ps", "-q", "--filter", "name=^orq-"),
                                                ("rm", "-f", "abc123"), ("rm", "-f", "def456")],
              str(sb.docker))
        sb.docker_ps = ""

        check("workspace: with a sub-repo it is the project folder",
              worktree.workspace_dir("acme", sb.project, "x") == sb.project)
        check("workspace: without a sub-repo it is a harness folder",
              worktree.workspace_dir("orq", repo, "") == sb.root / "workspaces" / "orq")
        check("without a sub-repo, $SUBREPO is the repo name",
              worktree.container_subrepo(repo, "") == "sandbox")

        # guard_git_remote with a repo whose `.git` is a file.
        sep = sb.root / "sep"
        run("init", "-q", "--separate-git-dir", str(sb.root / "sep.git"), str(sep))
        run("-C", str(sep), "remote", "add", "origin", "https://github.com/o/r.git")
        run("-C", str(sep), "config", "remote.fork.url", "https://x-access-token:ghs_s3cret@github.com/o/r.git")
        dispatch.guard_git_remote(sep, "https://github.com/o/r.git")
        check("guard_git_remote cleans the config even when `.git` is a file",
              "ghs_s3cret" not in (sb.root / "sep.git" / "config").read_text())

        # Wiring: run_claude_in_docker mounts the worktree, and cleans it up
        # whatever happens. A fake `docker run` stands in for the agent and
        # commits in the worktree through its host path.
        cfg = {**sb.cfg(), "max_iterations": 1, "docker_image": "x"}
        docker_calls: list[list[str]] = []
        proc_seen: dict = {}

        def fake_subprocess(behaviour: str):
            def fake_run(cmd, *a, **k):
                return real_run(cmd, *a, **k)

            class FakeProc:
                """`docker run` without docker. behaviour: ok, plant, crash, timeout
                (writes and never exits), stall (neither writes nor exits), alive
                (writes for a few rounds and exits 0)."""

                def __init__(self, cmd, *a, stdout=None, **k):
                    self.cmd, self.out, self.returncode, self.waits = cmd, stdout, None, 0
                    docker_calls.append(list(cmd))
                    proc_seen["stdin"] = k.get("stdin")
                    mounts = [cmd[i + 1] for i, c in enumerate(cmd) if c == "-v"]
                    wt_mount = [m for m in mounts if m.endswith(":/workspace/sandbox")]
                    if wt_mount:
                        path = wt_mount[0].rsplit(":", 1)[0]
                        real_run(["git", "-C", path, "checkout", "-q", "-B", "agent-branch"], check=True)
                        if behaviour == "plant":
                            real_run(["git", "-C", path, "config", "core.sshCommand", "touch /tmp/x"], check=True)

                def poll(self):
                    return self.returncode

                def kill(self):
                    proc_seen["killed"] = True
                    self.returncode = -9

                def wait(self, timeout=None):
                    if self.returncode is not None:
                        return self.returncode
                    if behaviour == "crash":
                        raise KeyboardInterrupt
                    if behaviour not in ("timeout", "stall", "alive"):
                        self.returncode = 0
                        return 0
                    self.waits += 1
                    if behaviour != "stall":
                        self.out.write("{}\n")
                        self.out.flush()
                    if behaviour == "alive" and self.waits >= 150:
                        self.returncode = 0
                        return 0
                    time.sleep(timeout)
                    raise subprocess.TimeoutExpired(self.cmd, timeout)

            return type("FakeSubprocess", (), {
                "run": staticmethod(fake_run), "Popen": FakeProc,
                "TimeoutExpired": subprocess.TimeoutExpired, "DEVNULL": subprocess.DEVNULL,
                "STDOUT": subprocess.STDOUT, "CompletedProcess": subprocess.CompletedProcess,
            })

        def run_brief(pipeline: str, behaviour: str = "ok", project_path=None,
                      subrepo="sandbox", cfg_over=None, **extra):
            docker_calls.clear()
            proc_seen.clear()
            run_brief.result = run_brief.error = None
            sb.docker.clear()
            meta = {"project": "sandbox", "subrepo": subrepo, "gh_repo": REPO,
                    "pr_number": 89, "base_branch": "developer", **extra}
            if pipeline:
                meta["pipeline"] = pipeline
            path = _write_brief(sb.root / f"{pipeline or 'manual'}.md", meta)

            def strict(*a, **k):
                if behaviour == "raise":
                    raise RuntimeError("boom")
                return {"status": "ok", "prs": []}

            with patched(dispatch, "subprocess", fake_subprocess(behaviour)), \
                 patched(dispatch, "STALL_POLL_SECONDS", 0.01), \
                 patched(dispatch, "LOG_DIR", sb.root / "logs"), \
                 patched(dispatch, "get_gh_token", lambda: "x"), \
                 patched(dispatch, "prepare_agent_home",
                         lambda *a: (sb.root / "agent-home", [], {}, lambda: None)), \
                 patched(dispatch, "load_strict_result", strict):
                try:
                    run_brief.result = dispatch.run_claude_in_docker(
                        "p", _brief(path), str(project_path or sb.project), str(sb.vault),
                        {**cfg, **(cfg_over or {})})
                except (RuntimeError, KeyboardInterrupt) as e:
                    run_brief.error = e
            runs = [c for c in docker_calls if c[1] == "run"]
            return runs[0] if runs else None

        def mounts(cmd) -> list[str]:
            return [cmd[i + 1] for i, c in enumerate(cmd) if c == "-v"]

        killed = lambda: [alive for args, alive in sb.docker if args[:2] == ("rm", "-f")]
        git_dir = (repo / ".git").resolve()

        cmd = run_brief("review-fix")
        check("review-fix: mounts the worktree at /workspace/$SUBREPO and the git dir at its path",
              cmd is not None
              and any(m.startswith(str(sb.root / "worktrees")) and m.endswith(":/workspace/sandbox")
                      for m in mounts(cmd))
              and f"{git_dir}:{git_dir}" in mounts(cmd),
              str(cmd and mounts(cmd)))
        check("with hooks/ read-only on top",
              mounts(cmd).index(f"{git_dir / 'hooks'}:{git_dir / 'hooks'}:ro")
              > mounts(cmd).index(f"{git_dir}:{git_dir}"))
        check("and the container does the checkout before starting claude",
              cmd[-3:-1] == ["sh", "-c"] and cmd[-1].startswith('git -C "/workspace/$SUBREPO" reset --hard'))
        check("claude runs with exec, without script(1)'s pty",
              "&& exec claude -p " in cmd[-1] and "script -qfc" not in cmd[-1], cmd[-1][:120])
        check("with --init: claude is PID 1 and something has to reap zombies", "--init" in cmd)
        check("and the docker client does not inherit dispatch's stdin",
              proc_seen.get("stdin") == subprocess.DEVNULL)
        check("and /workspace is the project folder", f"{sb.project}:/workspace" in mounts(cmd))
        check("when it ends the worktree is gone", list((sb.root / "worktrees").iterdir()) == [])
        check("the agent's branch stays in the repo", git("branch", "--list", "agent-branch") != "")
        check("and the main checkout stays on its branch",
              git("symbolic-ref", "--short", "HEAD") == "my-work")
        check("a container that exited on its own is not killed", killed() == [], str(sb.docker))

        run_brief("review-fix", "raise")
        check("even if the run crashes after the container, the worktree is deleted",
              list((sb.root / "worktrees").iterdir()) == [])

        with captured_logs(dispatch.logger) as logs:
            run_brief("review-fix", "timeout", cfg_over={"timeouts": {"review-fix": 1},
                                                         "stall_timeout_seconds": 1})
        check("on a timeout it kills the container by name before deleting the worktree",
              killed() == [True] and list((sb.root / "worktrees").iterdir()) == [], str(sb.docker))
        check("a timeout is rc 124, kills the docker client and the log says timeout",
              run_brief.result[0] == 124 and proc_seen.get("killed")
              and any(m.startswith("Timeout (1s)") for m in _messages(logs, "ERROR")), str(_messages(logs)))

        with captured_logs(dispatch.logger) as logs:
            run_brief("review-fix", "stall", cfg_over={"stall_timeout_seconds": 1})
        check("no new output within stall_timeout_seconds: cut like a timeout",
              run_brief.result[0] == 124 and proc_seen.get("killed") and killed() == [True]
              and list((sb.root / "worktrees").iterdir()) == [], str(sb.docker))
        check("and the dispatch log says frozen, not timeout",
              any("frozen" in m for m in _messages(logs, "ERROR"))
              and not any(m.startswith("Timeout") for m in _messages(logs, "ERROR")), str(_messages(logs)))
        check("and the run log ends with the reason for the cut, which is what Telegram shows",
              run_brief.result[1].read_text().rstrip().endswith("# dispatch: run cut by stall (1s)"),
              run_brief.result[1].read_text()[-200:])

        run_brief("review-fix", "alive", cfg_over={"stall_timeout_seconds": 1})
        check("a run that keeps writing stays alive past stall_timeout_seconds",
              run_brief.result[0] == 0 and not proc_seen.get("killed") and killed() == [],
              str((run_brief.result[0], sb.docker)))

        with captured_logs(dispatch.logger) as logs:
            run_brief("review-fix", "stall", cfg_over={"stall_timeout_seconds": 0,
                                                       "timeouts": {"review-fix": 1}})
        check("with stall_timeout_seconds at 0 only the pipeline timeout applies",
              run_brief.result[0] == 124 and proc_seen.get("killed") and killed() == [True]
              and any(m.startswith("Timeout (1s)") for m in _messages(logs, "ERROR"))
              and not any("frozen" in m for m in _messages(logs)), str(_messages(logs)))

        run_brief("review-fix", "crash")
        check("with Ctrl-C halfway through the container, same: it kills it, then deletes the worktree",
              isinstance(getattr(run_brief, "error", None), KeyboardInterrupt)
              and proc_seen.get("killed") and killed() == [True] and list((sb.root / "worktrees").iterdir()) == [], str(sb.docker))

        with captured_logs(dispatch.logger) as logs:
            run_brief("review-fix", "plant")
        git("config", "--unset", "core.sshCommand")
        check("if the run leaves an unsafe config, dispatch reports it at the end",
              any("is no longer safe" in m for m in _messages(logs, "ERROR")), str(_messages(logs)))

        run_brief.error = None
        cmd = run_brief("review-fix", head_ref="my-work")
        check("review-fix with the PR head checked out in your checkout: does not run",
              cmd is None and isinstance(run_brief.error, worktree.WorktreeError)
              and "my-work" in str(run_brief.error), str(run_brief.error))

        cmd = run_brief("", project_path=sb.project)
        check("a manual brief does not use a worktree (hardened pipelines only)",
              cmd is not None and not any("/worktrees/" in m for m in mounts(cmd))
              and cmd[-9:-7] == ["claude", "-p"] and "script" not in cmd, str(cmd and cmd[-9:]))

        cmd = run_brief("issue-fix", project_path=repo, subrepo="")
        env = [cmd[i + 1] for i, c in enumerate(cmd) if c == "-e"] if cmd else []
        check("project without a sub-repo: /workspace is the harness folder",
              cmd is not None and f"{sb.root / 'workspaces' / 'sandbox'}:/workspace" in mounts(cmd)
              and "SUBREPO=sandbox" in env, str(cmd and mounts(cmd)))
        check("and nothing from the harness lands in the main checkout",
              not (repo / ".lead-orchestrator.md").exists() and git("status", "--porcelain").count("\n") == 1,
              git("status", "--porcelain"))

        git("checkout", "-q", "-b", "issue-fix")
        cmd = run_brief("issue-fix")
        check("issue-fix with the brief's branch checked out in your checkout: does not run",
              cmd is None and isinstance(run_brief.error, worktree.WorktreeError), str(run_brief.error))
        git("checkout", "-q", "my-work")

        # still_wanted leaves the PR head for the check above.
        brief = _brief(_write_brief(sb.root / "rf.md", {
            "project": "sandbox", "pipeline": "review-fix", "event_id": 1, "gh_repo": REPO,
            "pr_number": 89}))
        with patched(dispatch, "_gh_api", lambda *a, **k: {"state": "open", "head": {"ref": "feat-x"}}), \
             patched(shutil, "which", lambda *_: "/usr/bin/gh"):
            dispatch.still_wanted(cfg, brief)
        check("still_wanted stores the PR head", brief.metadata.get("head_ref") == "feat-x")


# ── Dev → prod cycle: merged_dev, release and labels ─────────────────────────


class FakeGh:
    """GitHub without network, for feedback and reconcile.

    Every `gh` call lands in `calls`. `api` maps path → `gh api` answer
    (None = GitHub does not answer). `fail` is a predicate over the args of a
    feedback call that makes it fail.
    """

    def __init__(self, api: dict | None = None, default_branch: str = "main", fail=None):
        self.calls: list[list[str]] = []
        self.api = api or {}
        self.default_branch = default_branch
        self.fail = fail or (lambda args: False)

    def _output(self, args, timeout=20):
        self.calls.append(list(args))
        if self.fail(args):
            return None
        if args[:2] == ["repo", "view"]:
            return self.default_branch + "\n" if self.default_branch else None
        return ""

    def _api(self, path, timeout=30):
        self.calls.append(["api", path])
        return self.api.get(path)

    def __enter__(self):
        from talos import feedback
        from talos import reconcile

        self._stack = contextlib.ExitStack()
        self._stack.enter_context(patched(feedback, "_gh_output", self._output))
        self._stack.enter_context(patched(reconcile, "gh_api", self._api))
        self._stack.enter_context(patched(feedback, "_LABELS_READY", set()))
        self._stack.enter_context(patched(feedback, "_DEFAULT_BRANCH", {}))
        return self

    def __exit__(self, *exc):
        self._stack.close()

    def of(self, *prefix: str) -> list[list[str]]:
        return [c for c in self.calls if c[:len(prefix)] == list(prefix)]


def _merged_payload(number, head: str, base: str | None, sha: str) -> dict:
    pr = {"number": number, "merged": True, "html_url": f"http://x/pull/{number}",
          "head": {"ref": head}, "merge_commit_sha": sha, "labels": LABEL}
    if base is not None:
        pr["base"] = {"ref": base}
    return {"action": "closed", "pull_request": pr, "repository": {"full_name": REPO}}


def _stage_a_row(c, issue: int, state: str, **fields) -> int:
    from talos import store

    sha = fields.pop("merge_commit_sha", None)
    eid, _ = store.upsert_event(c, f"gh:{REPO}#{issue}", {
        "source": "github", "project": "sandbox", "gh_repo": REPO, "gh_issue": issue,
        "pipeline": "issue-fix", "state": state, **fields})
    if sha:
        store.annotate(c, eid, merge_commit_sha=sha)
    return eid


def _cmp(prod: str, sha: str) -> str:
    return f"repos/{REPO}/compare/{prod}...{sha}?per_page=1"


def _arg(call: list[str], flag: str) -> str:
    return call[call.index(flag) + 1] if call and flag in call else ""


class Notified:
    """Stands in for feedback.notify_released / notify_stale_merged_dev."""

    def __init__(self):
        self.calls: list[tuple] = []

    def __call__(self, *a, **kw):
        self.calls.append((a[2], kw))

    @property
    def issues(self) -> list:
        return sorted(n for n, _ in self.calls)


def test_merge_to_integration_parks_issue() -> None:
    """The PR goes to `developer`: the issue is NOT closed, it waits in merged_dev."""
    print("\nmerge to the integration branch → merged_dev")
    from talos import store
    from talos import webhookd

    with Sandbox() as sb, FakeGh() as gh:
        webhookd.CFG = {"vault": str(sb.vault)}
        branch = "2026-09-24-github-85-fix"
        brief = _write_brief(sb.vault / "projects" / "sandbox" / "in-review" / f"{branch}.md",
                             {"project": "sandbox", "status": "pr_open"})
        with store.connect() as c:
            ea = _stage_a_row(c, 85, "pr_open", brief_path=str(brief), pr_number=88)
            eb, _ = store.upsert_event(c, f"pr:{REPO}#88", {
                "project": "sandbox", "gh_repo": REPO, "pipeline": "review-fix",
                "state": "failed", "source": "github"})

        res = webhookd._handle_gh_merged(
            FakeTarget(), "d1", _merged_payload(88, branch, "developer", "abc123"), "", "closed")
        check("the handler reports merged_dev", res["state"] == "merged_dev", str(res))
        with store.connect() as c:
            a, b = store.get_event(c, ea), store.get_event(c, eb)
        check("the issue row ends in merged_dev", a["state"] == "merged_dev", a["state"])
        check("with the merge commit", a["merge_commit_sha"] == "abc123")
        check("the review-fix round does go to done", b["state"] == "done", b["state"])
        check("the brief still goes to completed/", not brief.exists())
        check("labels: −ai-in-review +ai-merged-dev",
              gh.of("issue", "edit", "85") and "ai-merged-dev" in gh.of("issue", "edit", "85")[0]
              and "ai-in-review" in gh.of("issue", "edit", "85")[0], str(gh.of("issue", "edit")))
        comments = gh.of("issue", "comment", "85")
        body = _arg(comments[0] if comments else [], "--body")
        check("a comment that says where and until when",
              len(comments) == 1 and "`developer`" in body and "#88" in body and "`main`" in body,
              body)
        check("no gh issue close", not gh.of("issue", "close"))
        check("the labels are created before they are used", len(gh.of("label", "create")) == 3)

        webhookd._handle_gh_merged(
            FakeTarget(), "d2", _merged_payload(88, branch, "developer", "abc123"), "", "closed")
        with store.connect() as c:
            again = store.get_event(c, ea)
            known = store.known_issue_numbers(c, REPO)
        check("a repeated merge does not send it back to done", again["state"] == "merged_dev")
        check("nor comments twice", len(gh.of("issue", "comment")) == 1)
        check("the reconciler counts it as known", 85 in known)

    # The integration branch is the repo default: GitHub already closed the
    # issue by the `Closes #N`, so "it stays open" would be a lie.
    with Sandbox() as sb, FakeGh(default_branch="developer") as gh:
        webhookd.CFG = {"vault": str(sb.vault)}
        with store.connect() as c:
            ea = _stage_a_row(c, 85, "pr_open", pr_number=88, brief_path=str(sb.root / "b-85.md"))
        webhookd._handle_gh_merged(FakeTarget({"prod_branch": "main"}), "d3",
                                   _merged_payload(88, "b-85", "developer", "abc123"), "", "closed")
        with store.connect() as c:
            row = store.get_event(c, ea)
        check("default = integration: still merged_dev", row["state"] == "merged_dev")
        check("default = integration: labels only, no comment",
              gh.of("issue", "edit", "85") and not gh.of("issue", "comment"))

    # Payload without a number: the row keeps the pr_number it had.
    with Sandbox() as sb, FakeGh():
        webhookd.CFG = {"vault": str(sb.vault)}
        with store.connect() as c:
            ea = _stage_a_row(c, 85, "pr_open", pr_number=88, brief_path=str(sb.root / "b-85.md"))
        webhookd._handle_gh_merged(FakeTarget(), "d4",
                                   _merged_payload(None, "b-85", "developer", "abc123"), "",
                                   "closed")
        with store.connect() as c:
            row = store.get_event(c, ea)
        check("no number in the payload: keeps pr_number",
              row["state"] == "merged_dev" and row["pr_number"] == 88, str(dict(row)))


def test_merge_with_unknown_or_missing_branches() -> None:
    """Unknown prod parks (safe side); a legacy payload without base stays `done`."""
    print("\nmerge — unknown prod or payload without base")
    from talos import store
    from talos import webhookd

    with Sandbox() as sb, FakeGh(default_branch="") as gh:
        webhookd.CFG = {"vault": str(sb.vault)}
        with store.connect() as c:
            ea = _stage_a_row(c, 5, "pr_open", pr_number=50, brief_path=str(sb.root / "b-5.md"))
        res = webhookd._handle_gh_merged(FakeTarget(), "u1",
                                         _merged_payload(50, "b-5", "main", "aaa"), "", "closed")
        with store.connect() as c:
            row = store.get_event(c, ea)
        check("GitHub does not say which branch is prod → merged_dev, not done",
              res["state"] == "merged_dev" and row["state"] == "merged_dev", row["state"])
        check("and closes nothing", not gh.of("issue", "close"))

    with Sandbox() as sb, FakeGh() as gh:
        webhookd.CFG = {"vault": str(sb.vault)}
        with store.connect() as c:
            ea = _stage_a_row(c, 6, "pr_open", pr_number=60, brief_path=str(sb.root / "b-6.md"))
        res = webhookd._handle_gh_merged(FakeTarget(), "u2",
                                         _merged_payload(60, "b-6", None, "bbb"), "", "closed")
        with store.connect() as c:
            row = store.get_event(c, ea)
        check("legacy payload without base → done", res["state"] == "done" and row["state"] == "done")
        check("no labels or comments", not gh.of("issue"))
        check("nor does it ask GitHub about branches", not gh.of("repo", "view"))


def test_reconciler_closes_issue_row_after_round() -> None:
    """The real #89 case: the `pr:` row already in done hid the issue row."""
    print("\nreconciler — merged PR with its round already closed")
    from talos import reconcile
    from talos import store
    from talos import webhookd
    from talos.routing import resolve_gh_repo

    with Sandbox() as sb, FakeGh():
        cfg = sb.cfg()
        webhookd.CFG = cfg
        branch = "2026-09-24-github-86-fix"
        with store.connect() as c:
            ea = _stage_a_row(c, 86, "pr_open", brief_path=str(sb.root / f"{branch}.md"),
                              pr_number=89)
            store.upsert_event(c, f"pr:{REPO}#89", {
                "project": "sandbox", "gh_repo": REPO, "pipeline": "review-fix",
                "state": "done", "source": "github"})
            # #88: an earlier pass recorded the old fixed id without closing the row.
            store.record_delivery(c, f"reconcile:{REPO}#pr89:merged", "github",
                                  "pull_request", "closed", "", verdict="done")
        pr = {"number": 89, "url": "http://x/pull/89", "title": "fix", "mergedAt": "x",
              "headRefName": branch, "baseRefName": "developer",
              "mergeCommit": {"oid": "6a368d5"}}
        res = reconcile._close_if_open(cfg, resolve_gh_repo(cfg, REPO), pr)
        with store.connect() as c:
            row = store.get_event(c, ea)
        check("it does not stay in pr_open", res is not None and row["state"] == "merged_dev",
              row["state"])
        check("with the sha that gh pr list returns", row["merge_commit_sha"] == "6a368d5")
        check("a second pass does nothing",
              reconcile._close_if_open(cfg, resolve_gh_repo(cfg, REPO), pr) is None)


def test_merge_to_prod_is_released() -> None:
    """Base = prod = default: `done` as before, GitHub closes by the Closes.

    Base = prod but not the default (git-flow): GitHub will not close it, so
    it is parked silently and the release step closes it with evidence.
    """
    print("\ndirect merge to prod")
    from talos import feedback
    from talos import reconcile
    from talos import store
    from talos import webhookd

    with Sandbox() as sb, FakeGh() as gh:
        webhookd.CFG = {"vault": str(sb.vault)}
        with store.connect() as c:
            ea = _stage_a_row(c, 3, "pr_open", brief_path=str(sb.root / "b-3.md"))
        res = webhookd._handle_gh_merged(
            FakeTarget({"prod_branch": "main"}), "p1",
            _merged_payload(4, "b-3", "main", "fff111"), "", "closed")
        with store.connect() as c:
            row = store.get_event(c, ea)
        check("prod = default: the row ends in done",
              res["state"] == "done" and row["state"] == "done", row["state"])
        edits = gh.of("issue", "edit", "3")
        check("prod = default: only the ai-released label",
              len(edits) == 1 and "ai-released" in edits[0], str(edits))
        check("prod = default: no comment or close",
              not gh.of("issue", "comment") and not gh.of("issue", "close"))

    api = {_cmp("main", "fff111"): {"status": "identical", "base_commit": {"sha": "fff1110"}},
           f"repos/{REPO}/issues/3": {"state": "open"}}
    notified = Notified()
    with Sandbox() as sb, FakeGh(api, default_branch="developer") as gh, \
            patched(feedback, "notify_released", notified):
        cfg = sb.cfg(prod_branch="main")
        webhookd.CFG = cfg
        with store.connect() as c:
            ea = _stage_a_row(c, 3, "pr_open", pr_number=4, brief_path=str(sb.root / "b-3.md"))
        res = webhookd._handle_gh_merged(
            FakeTarget({"prod_branch": "main"}), "p2",
            _merged_payload(4, "b-3", "main", "fff111"), "", "closed")
        with store.connect() as c:
            row = store.get_event(c, ea)
        check("git-flow: to a prod that is not the default → merged_dev with sha",
              row["state"] == "merged_dev" and row["merge_commit_sha"] == "fff111", row["state"])
        check("git-flow: without the 'it stays open' comment", not gh.of("issue", "comment"))
        reconcile.release_merged(cfg, REPO)
        with store.connect() as c:
            row = store.get_event(c, ea)
        check("git-flow: the release closes it on the next pass",
              row["state"] == "done" and [c[2] for c in gh.of("issue", "close")] == ["3"])
        check("git-flow: with a Telegram notice", notified.issues == [3])


def test_release_waits_for_prod() -> None:
    """The reconciler closes the issue only with evidence from GitHub."""
    print("\nrelease — compare against the prod branch")
    from talos import feedback
    from talos import reconcile
    from talos import store

    base = f"repos/{REPO}"
    api = {
        _cmp("main", "d1"): {"status": "diverged"},
        f"{base}/issues/1": {"state": "open"},
        _cmp("main", "a1"): {"status": "ahead"},
        f"{base}/issues/6": {"state": "open"},
        _cmp("main", "b2"): {"status": "behind", "base_commit": {"sha": "1234567890"}},
        f"{base}/issues/2": {"state": "open"},
        _cmp("main", "c3"): {"status": "identical", "base_commit": {"sha": "abcdef0"}},
        f"{base}/issues/3": {"state": "closed"},
        # e4: compare down, issue open. f5: compare ok, issue down.
        f"{base}/issues/4": {"state": "open"},
        _cmp("main", "f5"): {"status": "behind", "base_commit": {"sha": "99"}},
    }
    notified = Notified()
    with Sandbox() as sb, FakeGh(api) as gh, patched(feedback, "notify_released", notified):
        cfg = sb.cfg()
        ids = {}
        with store.connect() as c:
            for n, sha in ((1, "d1"), (2, "b2"), (3, "c3"), (4, "e4"), (5, "f5"), (6, "a1")):
                ids[n] = _stage_a_row(c, n, "merged_dev", merge_commit_sha=sha, pr_number=100 + n)
        res = reconcile.release_merged(cfg, REPO)
        with store.connect() as c:
            state = {n: store.get_event(c, i)["state"] for n, i in ids.items()}

        check("diverged: no changes", state[1] == "merged_dev"
              and ["api", _cmp("main", "d1")] in gh.calls)
        check("ahead: no changes", state[6] == "merged_dev")
        closes = gh.of("issue", "close")
        check("behind: the issue is closed once", [c[2] for c in closes] == ["2"], str(closes))
        body = _arg(closes[0] if closes else [], "--comment")
        check("with reason completed and the prod head",
              closes and "completed" in closes[0]
              and "is now on `main` (head `1234567`)" in body, body)
        edit2 = gh.of("issue", "edit", "2")
        check("behind: −ai-merged-dev −ai-in-review +ai-released",
              edit2 and "ai-released" in edit2[0]
              and _arg(edit2[0], "--remove-label") == "ai-merged-dev,ai-in-review", str(edit2))
        check("behind: the row ends in done", state[2] == "done")
        check("already closed: labels only, no close or comment",
              state[3] == "done" and gh.of("issue", "edit", "3")
              and not gh.of("issue", "close", "3") and not gh.of("issue", "comment"))
        check("GitHub down (compare): no changes", state[4] == "merged_dev")
        check("GitHub down (issue): no changes", state[5] == "merged_dev")
        check("Telegram only for the one the harness closed", notified.issues == [2],
              str(notified.calls))
        check("the result lists the released ones", sorted(r["number"] for r in res) == [2, 3])

        reconcile.release_merged(cfg, REPO)
        check("a second pass does not close again", len(gh.of("issue", "close")) == 1)

    with Sandbox() as sb, FakeGh(api, fail=lambda a: a[:2] == ["issue", "close"]):
        with store.connect() as c:
            eid = _stage_a_row(c, 2, "merged_dev", merge_commit_sha="b2")
        reconcile.release_merged(sb.cfg(), REPO)
        with store.connect() as c:
            row = store.get_event(c, eid)
        check("if closing fails, the row goes back to merged_dev", row["state"] == "merged_dev",
              row["state"])

    with Sandbox() as sb, FakeGh(api, fail=lambda a: a[:2] == ["issue", "edit"]):
        with store.connect() as c:
            eid = _stage_a_row(c, 3, "merged_dev", merge_commit_sha="c3")
        reconcile.release_merged(sb.cfg(), REPO)
        with store.connect() as c:
            row = store.get_event(c, eid)
        check("already closed and the labels fail: back to merged_dev",
              row["state"] == "merged_dev", row["state"])

    with Sandbox() as sb, FakeGh(api, default_branch=""):
        with store.connect() as c:
            eid = _stage_a_row(c, 2, "merged_dev", merge_commit_sha="b2")
        reconcile.release_merged(sb.cfg(), REPO)
        with store.connect() as c:
            row = store.get_event(c, eid)
        check("without knowing which branch is prod nothing is closed", row["state"] == "merged_dev")

    # prod_branch from config: the compare goes against it, GitHub is not asked.
    api2 = {_cmp("release", "b2"): {"status": "behind", "base_commit": {"sha": "7777777"}},
            f"{base}/issues/2": {"state": "open"}}
    with Sandbox() as sb, FakeGh(api2) as gh, patched(feedback, "notify_released", Notified()):
        with store.connect() as c:
            eid = _stage_a_row(c, 2, "merged_dev", merge_commit_sha="b2")
        reconcile.release_merged(sb.cfg(prod_branch="release"), REPO)
        with store.connect() as c:
            row = store.get_event(c, eid)
        check("prod_branch from config: compares against that branch",
              row["state"] == "done" and ["api", _cmp("release", "b2")] in gh.calls)
        check("prod_branch from config: does not ask for the default", not gh.of("repo", "view"))

    # A row without a sha: it is taken from the PR and stored.
    api3 = {f"{base}/pulls/102": {"merged": True, "base": {"ref": "developer"},
                                  "merge_commit_sha": "b2"},
            **{k: v for k, v in api.items() if k.endswith("b2?per_page=1") or k.endswith("/2")}}
    with Sandbox() as sb, FakeGh(api3), patched(feedback, "notify_released", Notified()):
        with store.connect() as c:
            eid = _stage_a_row(c, 2, "merged_dev", pr_number=102)
        reconcile.release_merged(sb.cfg(), REPO)
        with store.connect() as c:
            row = store.get_event(c, eid)
        check("no sha: takes it from the PR, stores it and releases",
              row["merge_commit_sha"] == "b2" and row["state"] == "done", str(dict(row)))


def test_release_respects_humans() -> None:
    """Reopened issues stay open; closed-by-hand rows leave; stuck rows warn once."""
    print("\nrelease — reopened, closed by hand and stuck")
    from talos import feedback
    from talos import reconcile
    from talos import store

    base = f"repos/{REPO}"
    api = {
        _cmp("main", "r1"): {"status": "behind", "base_commit": {"sha": "5555555"}},
        f"{base}/issues/1": {"state": "open", "state_reason": "reopened"},
        _cmp("main", "m2"): {"status": "diverged"},
        f"{base}/issues/2": {"state": "closed", "state_reason": "completed"},
        # s3: compare 404 (orphaned sha), open, old. n4: same, but recent.
        f"{base}/issues/3": {"state": "open"},
        f"{base}/issues/4": {"state": "open"},
    }
    released, stale = Notified(), Notified()
    with Sandbox() as sb, FakeGh(api) as gh, \
            patched(feedback, "notify_released", released), \
            patched(feedback, "notify_stale_merged_dev", stale):
        cfg = sb.cfg()
        with store.connect() as c:
            e1 = _stage_a_row(c, 1, "merged_dev", merge_commit_sha="r1", pr_number=11)
            e2 = _stage_a_row(c, 2, "merged_dev", merge_commit_sha="m2", pr_number=12)
            e3 = _stage_a_row(c, 3, "merged_dev", merge_commit_sha="s3", pr_number=13)
            e4 = _stage_a_row(c, 4, "merged_dev", merge_commit_sha="n4", pr_number=14)
            c.execute("UPDATE events SET updated_at = '2020-01-01T00:00:00Z' WHERE id = ?", (e3,))
        res = reconcile.release_merged(cfg, REPO)
        with store.connect() as c:
            row = {n: store.get_event(c, i) for n, i in ((1, e1), (2, e2), (3, e3), (4, e4))}

        check("reopened: it is not closed", not gh.of("issue", "close"))
        check("reopened: the row leaves merged_dev with ai-released",
              row[1]["state"] == "done" and gh.of("issue", "edit", "1")
              and "ai-released" in gh.of("issue", "edit", "1")[0])
        check("reopened: Telegram notice flagged as reopened",
              released.calls == [(1, {"reopened": True})], str(released.calls))
        check("closed by hand without a release: goes to done silently",
              row[2]["state"] == "done" and not gh.of("issue", "edit", "2")
              and not gh.of("issue", "comment"))
        check("stuck 14+ days: one notice and the mark in last_error",
              stale.issues == [3] and str(row[3]["last_error"]).startswith("stale_merged_dev")
              and row[3]["state"] == "merged_dev", str(stale.calls))
        check("recent: no notice", row[4]["state"] == "merged_dev" and not row[4]["last_error"])
        check("result: reopened released, closed by hand done",
              sorted((r["number"], r["state"]) for r in res) == [(1, "released"), (2, "done")],
              str(res))

        reconcile.release_merged(cfg, REPO)
        check("the stuck notice is not repeated", stale.issues == [3])


def test_handled_labels_gate() -> None:
    """Second lock: an issue with an ai-* label does not come back in."""
    print("\nai-* label gate")
    from talos import store
    from talos import triage
    from talos import webhookd

    for label in ("ai-in-review", "ai-merged-dev", "ai-released"):
        v = triage.gate_handled_labels(_issue(labels=[{"name": label}]))
        check(f"{label} → rejected_already_handled", v.state == "rejected_already_handled",
              v.state)
    check("without ai-* labels it passes", triage.gate_handled_labels(_issue(labels=["bug"])).admitted)

    with Sandbox() as sb:
        webhookd.CFG = sb.cfg()
        target = FakeTarget({"activated_at": "2020-01-01T00:00:00Z"})
        payload = {"action": "reopened", "issue": _issue(9, labels=[{"name": "ai-released"}])}
        res = webhookd._handle_gh_issue(target, "g1", payload, "", "reopened")
        check("admission rejects it even without a row (events.db lost)",
              res["state"] == "rejected_already_handled", str(res))
        check("and does not write a brief", not list((sb.vault / "ToDos").rglob("*.md")))


def test_migration_parks_legacy_rows() -> None:
    """#85/#86: old rows closed before merged_dev existed go to standby."""
    print("\nretroactive migration → merged_dev")
    from talos import reconcile
    from talos import store

    base = f"repos/{REPO}"
    api = {
        f"{base}/pulls/88": {"merged": True, "base": {"ref": "developer"}, "merge_commit_sha": "s88"},
        f"{base}/pulls/89": {"merged": True, "base": {"ref": "developer"}, "merge_commit_sha": "s89"},
        f"{base}/pulls/90": {"merged": True, "base": {"ref": "main"}, "merge_commit_sha": "s90"},
        f"{base}/pulls/91": {"merged": False, "base": {"ref": "developer"}},
    }
    with Sandbox() as sb, FakeGh(api) as gh:
        cfg = sb.cfg()
        in_review = sb.vault / "projects" / "sandbox" / "in-review"
        brief = _write_brief(in_review / "b-86.md", {"project": "sandbox", "status": "pr_open"})
        round_brief = _write_brief(in_review / "b-86-r1.md",
                                   {"project": "sandbox", "status": "pr_open"})
        with store.connect() as c:
            e85 = _stage_a_row(c, 85, "done", pr_number=88)
            e86 = _stage_a_row(c, 86, "pr_open", pr_number=89, brief_path=str(brief))
            e87 = _stage_a_row(c, 87, "done", pr_number=90)
            e92 = _stage_a_row(c, 92, "pr_open", pr_number=91)
            er, _ = store.upsert_event(c, f"pr:{REPO}#89", {
                "project": "sandbox", "gh_repo": REPO, "pipeline": "review-fix",
                "state": "pr_open", "source": "github", "brief_path": str(round_brief)})
        first = reconcile.migrate_merged_dev(cfg, REPO)
        pulls_before = len(gh.of("api"))
        second = reconcile.migrate_merged_dev(cfg, REPO)
        with store.connect() as c:
            row = {n: store.get_event(c, i)
                   for n, i in ((85, e85), (86, e86), (87, e87), (92, e92), ("r", er))}
        check("done merged to dev → merged_dev with its sha",
              row[85]["state"] == "merged_dev" and row[85]["merge_commit_sha"] == "s88")
        check("pr_open merged to dev → merged_dev with its sha",
              row[86]["state"] == "merged_dev" and row[86]["merge_commit_sha"] == "s89")
        check("and its brief leaves in-review/", not brief.exists())
        check("the review-fix round of the same PR is closed too",
              row["r"]["state"] == "done" and not round_brief.exists(), row["r"]["state"])
        check("merged to prod: stays done, only stores the sha",
              row[87]["state"] == "done" and row[87]["merge_commit_sha"] == "s90")
        check("unmerged PR: not touched", row[92]["state"] == "pr_open")
        check("the first run migrates two", sorted(r["number"] for r in first) == [85, 86])
        check("the second migrates nothing", second == [], str(second))
        check("the second only asks GitHub about the unmerged PR",
              [c[1] for c in gh.of("api")[pulls_before:]] == [f"{base}/pulls/91"],
              str(gh.of("api")[pulls_before:]))
        check("ai-merged-dev label once per issue",
              len(gh.of("issue", "edit", "85")) == 1 and len(gh.of("issue", "edit", "86")) == 1)
        check("only the pr_open one gets the merge notice, once",
              [c[2] for c in gh.of("issue", "comment")] == ["86"], str(gh.of("issue", "comment")))


def test_migration_and_release_in_one_pass() -> None:
    """reconcile_repo_all: a legacy row whose commit is already in prod closes the same pass."""
    print("\nmigration + release in the same pass")
    from talos import feedback
    from talos import reconcile
    from talos import store

    base = f"repos/{REPO}"
    api = {f"{base}/pulls/88": {"merged": True, "base": {"ref": "developer"},
                                "merge_commit_sha": "s88"},
           _cmp("main", "s88"): {"status": "behind", "base_commit": {"sha": "8888888"}},
           f"{base}/issues/85": {"state": "open"}}
    with Sandbox() as sb, FakeGh(api) as gh, \
            patched(reconcile, "reconcile_repo", lambda cfg, r: []), \
            patched(reconcile, "reconcile_prs", lambda cfg, r: []), \
            patched(feedback, "notify_released", Notified()):
        with store.connect() as c:
            eid = _stage_a_row(c, 85, "done", pr_number=88)
        res = reconcile.reconcile_repo_all(sb.cfg(), REPO)
        with store.connect() as c:
            row = store.get_event(c, eid)
        check("migrates and releases in one pass",
              [r["state"] for r in res] == ["merged_dev", "released"] and row["state"] == "done",
              str(res))
        check("the issue is closed", [c[2] for c in gh.of("issue", "close")] == ["85"])


def test_reconcile_step_failures() -> None:
    """One failing step keeps the rest; everything failing is raised, not 'nothing new'."""
    print("\nreconciler — failures per step and per repo")
    from talos import reconcile

    def boom(cfg, r):
        raise RuntimeError("GitHub down")

    with Sandbox() as sb, \
            patched(reconcile, "reconcile_repo", lambda cfg, r: [{"state": "admitted"}]), \
            patched(reconcile, "reconcile_prs", boom), \
            patched(reconcile, "migrate_merged_dev", boom), \
            patched(reconcile, "release_merged", boom):
        res = reconcile.reconcile(sb.cfg())
        check("a failing step does not drop another step's results", res == [{"state": "admitted"}])

    with Sandbox() as sb, patched(reconcile, "reconcile_repo", boom), \
            patched(reconcile, "reconcile_prs", boom), \
            patched(reconcile, "migrate_merged_dev", boom), \
            patched(reconcile, "release_merged", boom):
        try:
            reconcile.reconcile(sb.cfg())
            raised = False
        except RuntimeError:
            raised = True
        check("all repos down → exception, not 'Nothing new'", raised)


def test_small_release_patches() -> None:
    """Telegram does not log the token; labels; /reconcile icons; cache warm-up."""
    print("\nrelease — small patches")
    import logging

    from talos import feedback
    import requests
    from talos import store
    from talos import webhookd

    class Capture(logging.Handler):
        def __init__(self):
            super().__init__()
            self.lines: list[str] = []

        def emit(self, record):
            self.lines.append(record.getMessage())

    def post(url, **kw):
        raise ConnectionError(f"Max retries exceeded with url: {url}")

    cap = Capture()
    log = logging.getLogger("orchestrator")
    log.addHandler(cap)
    try:
        with patched(requests, "post", post):
            feedback.notify_released({"telegram_bot_token": "123:S3CR3T", "telegram_chat_id": "1"},
                                     REPO, 2, "t", "main", "abc")
    finally:
        log.removeHandler(cap)
    check("a network error does not leave the token in the log",
          cap.lines and not any("S3CR3T" in line for line in cap.lines), str(cap.lines))

    with FakeGh(fail=lambda a: a[:2] == ["issue", "edit"]):
        ok = feedback.announce_released(REPO, 3, "main", "abc", 9, already_closed=True)
    check("already closed + labels fail → False", ok is False)

    check("/reconcile: done and duplicate do not show 🚫",
          store.state_icon("done") == "✅" and store.state_icon("duplicate") == "•")
    check("/reconcile: released 🚀, rejected_* 🚫",
          store.state_icon("released") == "🚀" and store.state_icon("rejected_author") == "🚫")

    with FakeGh() as gh:
        webhookd._warm_github_caches({"repos": {REPO: {"enabled": True},
                                                "disabled/repo": {"enabled": False}}})
        check("warms up labels and default branch only for enabled repos",
              len(gh.of("label", "create")) == 3 and len(gh.of("repo", "view")) == 1
              and all("disabled/repo" not in c for c in gh.calls), str(gh.calls))


def test_ensure_labels_once_per_repo() -> None:
    print("\nensure_labels — once per repo and process")
    from talos import feedback

    with FakeGh() as gh:
        for _ in range(3):
            feedback.label_issue(REPO, 1, add="ai-in-review")
        feedback.label_issue("other/repo", 1, add="ai-merged-dev")
        feedback.label_issue("third/repo", 1, remove="ai-merged-dev,ai-in-review")
        created = gh.of("label", "create")
        check("three labels per repo, three repos (also with a comma-separated list)",
              len(created) == 9, str(len(created)))
        check("with --force (idempotent)", all("--force" in c for c in created))
        check("the three cycle labels",
              {c[2] for c in created} == {"ai-in-review", "ai-merged-dev", "ai-released"})

    with FakeGh(fail=lambda a: a[:2] == ["label", "create"]):
        failed = feedback.ensure_labels(REPO)
        check("a failure is not cached", not failed and REPO not in feedback._LABELS_READY)


def main() -> int:
    print("Offline harness tests")
    for fn in (
        test_issue_body_cannot_inline_host_files,
        test_git_auth_never_touches_disk,
        test_dispatch_caps,
        test_stage_a_enqueues_stage_b,
        test_pause_stops_dispatch_not_admission,
        test_hardened_home_is_disposable,
        test_sentry_event_alert,
        test_dry_run_rows_come_back_live,
        test_review_burst_opens_one_round,
        test_review_gates,
        test_merge_moves_the_brief,
        test_merge_after_review_round_closes_both,
        test_failures_reach_ledger_and_source,
        test_brief_write_failure_is_retryable,
        test_negative_content_length,
        test_revalidate_before_dispatch,
        test_telegram_html,
        test_oldest_brief_first,
        test_backfill_counts_admitted,
        test_reconciler_keeps_bot_gate,
        test_disabled_repo_does_not_block,
        test_running_manual_brief_is_reaped,
        test_small_fixes,
        test_gate_that_clears_gets_a_brief,
        test_backfill_skips_only_the_cutoff,
        test_service_map,
        test_stream_result_beats_exit_code,
        test_anti_loop_marker,
        test_bmad_kit_is_ensured,
        test_stage_a_must_publish_its_review,
        test_stage_a_thread_count,
        test_review_fix_log_summary,
        test_brief_names_the_repo_dir,
        test_agent_works_in_a_worktree,
        test_run_watchdog,
        test_merge_to_integration_parks_issue,
        test_merge_with_unknown_or_missing_branches,
        test_reconciler_closes_issue_row_after_round,
        test_merge_to_prod_is_released,
        test_release_waits_for_prod,
        test_release_respects_humans,
        test_handled_labels_gate,
        test_migration_parks_legacy_rows,
        test_migration_and_release_in_one_pass,
        test_reconcile_step_failures,
        test_small_release_patches,
        test_ensure_labels_once_per_repo,
    ):
        try:
            fn()
        except Exception as e:
            import traceback

            traceback.print_exc()
            FAILED.append(f"{fn.__name__} (exception: {e})")

    print()
    if FAILED:
        print(f"✘ {len(FAILED)} check(s) failed:")
        for f in FAILED:
            print(f"   - {f}")
        return 1
    print("✔ all green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
