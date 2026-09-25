#!/usr/bin/env python3
"""
Headless Claude Code orchestrator.

Reads Markdown briefs from Obsidian/ToDos/, runs them one by one inside
a Docker container (claude-dev), moves the successful ones to
projects/{project}/completed/ and reports results to Telegram.
"""
from __future__ import annotations

import argparse
import fcntl
import html
import json
import logging
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import frontmatter
import requests
import yaml

from talos import bmad_kit
from talos import worktree
from talos import secrets_env
from talos.routing import resolve_gh_repo, resolve_sentry_project, validate_routing
from talos.util import CONFIG_PATH, is_paused, slugify, utcnow

# ── Module constants (also imported by bot.py) ───────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
PROMPTS_DIR = SCRIPT_DIR / "prompts"
LOG_DIR = Path("~/.orchestrator/logs").expanduser()
LOCK_FILE = Path("~/.orchestrator/dispatch.lock").expanduser()
LEAD_TEMPLATE_PATH = PROMPTS_DIR / "lead-orchestrator.md"

# Each pipeline brings its own system prompt. The default is the usual
# swarm, which behaves exactly as it did before this was added.
LEAD_TEMPLATES = {
    "default": LEAD_TEMPLATE_PATH,
    "issue-fix": PROMPTS_DIR / "lead-issue-fix.md",
    "review-fix": PROMPTS_DIR / "lead-review-fix.md",
}

# Pipelines where the PR is load-bearing: without the result JSON we cannot
# tell whether to continue, so they stop tolerating a missing file.
STRICT_RESULT_PIPELINES = {"issue-fix", "review-fix"}

# Pipelines that run with a prompt from an external source. The container is
# hardened: no SSH key (push over HTTPS with GH_TOKEN), no full vault, and a
# throwaway ~/.claude instead of the host's.
HARDENED_PIPELINES = STRICT_RESULT_PIPELINES

# Sources whose body was written by someone other than the operator.
EXTERNAL_SOURCES = {"github", "sentry"}

# Git auth in the hardened containers, via environment variables rather than
# a file. The previous version ran `git remote set-url origin
# https://x-access-token:$GH_TOKEN@…` inside the container, and since the repo
# is a bind mount from the host, the token ended up in plain text in the
# host's `.git/config` and `origin` switched from SSH to HTTPS. With this, git
# rewrites SSH URLs to HTTPS on the fly and the credential helper reads the
# token from the environment: nothing touches disk. The variable's value is
# the helper script, not the token, so it can go in argv.
GIT_HTTPS_ENV = {
    "GIT_CONFIG_COUNT": "4",
    "GIT_CONFIG_KEY_0": "url.https://github.com/.insteadOf",
    "GIT_CONFIG_VALUE_0": "git@github.com:",
    "GIT_CONFIG_KEY_1": "url.https://github.com/.insteadOf",
    "GIT_CONFIG_VALUE_1": "ssh://git@github.com/",
    # Empty first: resets any helper the mounted gitconfig brings along.
    "GIT_CONFIG_KEY_2": "credential.https://github.com.helper",
    "GIT_CONFIG_VALUE_2": "",
    "GIT_CONFIG_KEY_3": "credential.https://github.com.helper",
    "GIT_CONFIG_VALUE_3": (
        '!f() { test "$1" = get || return 0; '
        'echo username=x-access-token; echo "password=$GH_TOKEN"; }; f'
    ),
}

# The only parts of ~/.claude.json the hardened container needs. The rest of
# the file holds `mcpServers` (with their env, which may be secrets) and
# `projects`; a prompt-injected agent that adds an MCP server there gets
# code execution on the host the next time you open Claude.
CLAUDE_JSON_KEEP = (
    "hasCompletedOnboarding", "lastOnboardingVersion", "oauthAccount",
    "userID", "firstStartTime", "installMethod",
)
AGENT_HOMES_DIR = Path("~/.orchestrator/agent-home").expanduser()

PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}
MAX_INLINE_FILE_BYTES = 100 * 1024  # 100 KB
# How often dispatch checks whether the run log grew. The silence threshold
# is `stall_timeout_seconds` from the config.
STALL_POLL_SECONDS = 30

# Regex to detect absolute paths referenced in the brief body.
# The `.` before the extension is escaped so it does not match any char.
FILE_REF_RE = re.compile(r'(?<![\w/])(~?/[^\s\'"`)\]]+\.[a-zA-Z0-9]+)')

DEFAULT_CONFIG = """\
vault: ~/Obsidian
briefs_dir: ToDos
memory_dir: agentes/memoria
poll_interval_seconds: 300

# Telegram bot — create it with @BotFather and get chat_id from /getUpdates
telegram_bot_token: ""   # e.g. 123456:ABC-DEF...
telegram_chat_id: ""     # e.g. 987654321

# Auth: manual briefs mount the host's ~/.claude read-write (run `claude`
# interactively once on your machine to log in; the container inherits that
# session). Hardened pipelines (issue-fix, review-fix) never see it: they get
# a token from secrets.claude_oauth_token_env instead.

# Docker
docker_image: claude-dev
claude_timeout_seconds: 3600
# No new output in the run log for this long and the run is considered
# frozen and cut like a timeout. 0 turns it off.
stall_timeout_seconds: 1200

# Multi-agent swarm settings
# How many fix-and-revalidate rounds (QA bugs + reviewer issues) the
# architect may run before aborting the brief and leaving the branch local,
# unpushed. Iteration 0 = first pass. Raise it if the devs are slow to
# converge; lower it if you want the system to escalate to a human sooner.
max_iterations: 3

projects:
  acme: ~/code/acme
  my-app: ~/code/my-app
"""

MEMORY_TEMPLATE = """\
# Learnings — {project}

> Persistent agent memory for the **{project}** project.
> Decisions, errors and useful patterns accumulate here across runs.

## Decisions

## Errors and solutions

## Useful patterns
"""

logger = logging.getLogger("talos")


# ── Dataclass ────────────────────────────────────────────────────────────────


@dataclass
class Brief:
    path: Path
    project: str
    priority: str
    body: str
    metadata: dict = field(default_factory=dict)


# ── Setup: config + logging + prereqs ────────────────────────────────────────


def load_config() -> dict:
    """Loads config.yaml from the script directory. Creates it if missing."""
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(DEFAULT_CONFIG)
        print(
            f"Config created at {CONFIG_PATH}.\n"
            "Edit these fields:\n"
            "  - telegram_bot_token + telegram_chat_id (optional)\n"
            "Make sure ~/.claude exists on your host (run `claude` interactively\n"
            "once to log in if it does not).\n"
            "Then run:\n"
            "  docker build --network=host -f docker/Dockerfile -t claude-dev docker/\n"
            "And run the orchestrator again."
        )
        sys.exit(0)

    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f) or {}

    # systemd loads secrets.env with EnvironmentFile; this covers manual
    # runs and the bot. It does not override what is already in the environment.
    secrets_env.load_dotenv()

    # Expand ~ in paths
    cfg["vault"] = str(Path(cfg.get("vault", "")).expanduser())
    projects = cfg.get("projects") or {}
    cfg["projects"] = {k: str(Path(v).expanduser()) for k, v in projects.items()}
    cfg.setdefault("briefs_dir", "ToDos")
    cfg.setdefault("memory_dir", "agentes/memoria")
    cfg.setdefault("poll_interval_seconds", 300)
    cfg.setdefault("docker_image", "claude-dev")
    cfg.setdefault("claude_timeout_seconds", 3600)
    cfg.setdefault("stall_timeout_seconds", 1200)
    cfg.setdefault("telegram_bot_token", "")
    cfg.setdefault("telegram_chat_id", "")
    cfg.setdefault("max_iterations", 3)

    # ── Event harness ───────────────────────────────────────────────────
    cfg.setdefault("repos", {})
    cfg.setdefault("sentry", {"org": "", "projects": {}})
    cfg.setdefault("webhook", {"bind": "127.0.0.1", "port": 8787})
    cfg.setdefault("secrets", {})
    cfg.setdefault("timeouts", {})
    cfg.setdefault("global_daily_cap", 10)
    cfg.setdefault("reconcile_interval_minutes", 30)

    stall = cfg["stall_timeout_seconds"]
    if isinstance(stall, bool) or not isinstance(stall, int) or stall < 0:
        # A negative value would cut every run after 30s, and a string would
        # blow up halfway through run_claude_in_docker with the credentials
        # already copied.
        logger.warning("config.yaml: stall_timeout_seconds=%r is not an integer >= 0; using 1200", stall)
        cfg["stall_timeout_seconds"] = 1200

    # A badly written routing config has to fail here, not six hours
    # later when the first webhook arrives.
    warnings: list[str] = []
    problems = validate_routing(cfg, warnings)
    for msg in warnings:
        logger.warning("config.yaml (repo disabled): %s", msg)
    if problems:
        for msg in problems:
            logger.error("config.yaml: %s", msg)
        raise SystemExit(
            f"config.yaml has {len(problems)} routing problem(s) (see above)"
        )

    return cfg


def get_gh_token() -> str:
    """Reads the gh token from the host. Returns '' on failure."""
    if shutil.which("gh") is None:
        logger.warning("gh is not installed on the host — the agent will not be able to create PRs")
        return ""
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception as e:
        logger.warning("gh auth token failed (%s) — the agent will not be able to create PRs", e)
        return ""
    logger.warning("gh auth token returned exit %d — the agent will not be able to create PRs", result.returncode)
    return ""


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    (LOG_DIR / "runs").mkdir(parents=True, exist_ok=True)
    (LOG_DIR / "plans").mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    file_handler = RotatingFileHandler(
        LOG_DIR / "dispatch.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=3,
    )
    file_handler.setFormatter(fmt)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)

    # The modules dispatch imports (bmad_kit, worktree, reconcile, feedback…)
    # log to "orchestrator", not "talos": without its own handlers
    # their INFO lines never reached dispatch.log.
    for lg in (logger, logging.getLogger("orchestrator")):
        lg.setLevel(logging.INFO)
        lg.handlers.clear()
        lg.addHandler(file_handler)
        lg.addHandler(stream_handler)
        lg.propagate = False


def check_prereqs(config: dict, dry_run: bool = False) -> None:
    """Checks that docker, the image and the Claude credentials exist."""
    if dry_run:
        return

    if shutil.which("docker") is None:
        logger.error("Docker not found in PATH. Install it before continuing.")
        sys.exit(1)

    image = config["docker_image"]
    result = subprocess.run(
        ["docker", "image", "inspect", image],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        logger.error(
            "Docker image '%s' not built. Run: docker build --network=host "
            "-f docker/Dockerfile -t %s docker/",
            image, image,
        )
        sys.exit(1)

    claude_home = Path.home() / ".claude"
    claude_json = Path.home() / ".claude.json"
    if not claude_home.exists() or not claude_json.exists():
        logger.error(
            "~/.claude or ~/.claude.json does not exist on the host. Run `claude` "
            "interactively once to log in (opens a browser) — the "
            "container mounts both and inherits your session."
        )
        sys.exit(1)

    if not (Path.home() / ".ssh").exists():
        logger.warning(
            "~/.ssh does not exist on the host — the agent will not be able to git push."
        )


# ── Finding and ordering briefs ──────────────────────────────────────────────


def find_pending_briefs(vault: str, briefs_dir: str) -> list[Brief]:
    briefs_path = Path(vault) / briefs_dir
    if not briefs_path.exists():
        logger.warning("Briefs folder does not exist: %s", briefs_path)
        return []

    briefs: list[Brief] = []
    for md_path in briefs_path.rglob("*.md"):
        try:
            post = frontmatter.load(md_path)
        except Exception as e:
            logger.warning("Could not parse %s: %s", md_path, e)
            continue

        status = post.metadata.get("status")
        if status != "pending":
            continue

        project = post.metadata.get("project")
        if not project:
            logger.warning("Brief without `project` in front matter, skip: %s", md_path)
            continue

        priority = post.metadata.get("priority", "medium")

        briefs.append(Brief(
            path=md_path,
            project=str(project),
            priority=str(priority),
            body=post.content,
            metadata=dict(post.metadata),
        ))
    return briefs


def sort_by_priority(briefs: list[Brief]) -> list[Brief]:
    """Priority first, then oldest first.

    Without the second key, briefs of the same priority ran in whatever order
    `rglob` found them. `created` is the admission time, and the reconciler
    admits oldest issue first, so this is issue age. A brief without it
    (hand-written) falls back to its name, which starts with the date.
    """
    return sorted(briefs, key=lambda b: (
        PRIORITY_ORDER.get(b.priority, 99),
        str(b.metadata.get("created") or b.path.name),
    ))


# ── Agent memory ─────────────────────────────────────────────────────────────


def ensure_memory_file(vault: str, memory_dir: str, project: str) -> Path:
    memory_path = Path(vault) / memory_dir / f"{project}-aprendizajes.md"
    memory_path.parent.mkdir(parents=True, exist_ok=True)
    if not memory_path.exists():
        memory_path.write_text(MEMORY_TEMPLATE.format(project=project))
        logger.info("Initial memory created: %s", memory_path)
    return memory_path


# ── Inlining referenced files ────────────────────────────────────────────────


def inline_file_refs(body: str) -> str:
    def _replace(match: re.Match) -> str:
        raw_path = match.group(1)
        try:
            p = Path(raw_path).expanduser()
        except Exception:
            return raw_path

        if not p.is_file():
            logger.debug("Reference to a file that was not found: %s", raw_path)
            return raw_path

        try:
            size = p.stat().st_size
        except OSError:
            return raw_path

        if size > MAX_INLINE_FILE_BYTES:
            logger.warning(
                "Referenced file too large (%d bytes), not inlined: %s",
                size, raw_path,
            )
            return raw_path

        try:
            content = p.read_text(errors="replace")
        except Exception as e:
            logger.warning("Could not read %s: %s", raw_path, e)
            return raw_path

        ext = p.suffix.lstrip(".") or "text"
        return f"\n\n## Contents of {raw_path}\n```{ext}\n{content}\n```\n"

    return FILE_REF_RE.sub(_replace, body)


# ── Building the final prompt ────────────────────────────────────────────────


def trusted_body(brief: Brief) -> bool:
    """True if the brief body was written by the operator, not an external source.

    `inline_file_refs` runs on the HOST and pastes whatever file the body
    names. With an issue body that is straight exfiltration: an issue saying
    "see `~/.orchestrator/secrets.env`" pasted the webhook secrets, the Sentry
    token or the Claude credentials into the prompt, above the untrusted-data
    fence, for an agent that can post on GitHub.
    """
    pipeline = str(brief.metadata.get("pipeline") or "default").strip().lower()
    source = str(brief.metadata.get("source") or "").strip().lower()
    return pipeline not in HARDENED_PIPELINES and source not in EXTERNAL_SOURCES


def build_prompt(memory_path: Path, brief: Brief) -> str:
    try:
        memoria = memory_path.read_text()
    except FileNotFoundError:
        memoria = ""

    body_processed = inline_file_refs(brief.body) if trusted_body(brief) else brief.body

    # Path inside the container, not on the host
    memoria_container_path = (
        f"/obsidian/agentes/memoria/{brief.project}-aprendizajes.md"
    )

    return (
        "# Project memory\n\n"
        f"{memoria}\n\n"
        "---\n\n"
        "# Current task\n\n"
        f"{body_processed}\n\n"
        "---\n\n"
        f"When you finish successfully, update your memory at "
        f"`{memoria_container_path}` with what you learned in this task "
        f"(decisions, errors, useful patterns).\n"
    )


# ── Result contract ──────────────────────────────────────────────────────────

PR_URL_RE = re.compile(r"^https://github\.com/(?P<repo>[^/]+/[^/]+)/pull/(?P<num>\d+)$")


def rescue_pr_from_gh(gh_repo: str, head_branch: str) -> dict | None:
    """Looks up the PR by branch when the agent did not report the result JSON.

    A PR that exists but was not reported is the most annoying failure mode —
    the work is done and we would lose it — and the rescue costs one call.
    """
    if not gh_repo or not head_branch or shutil.which("gh") is None:
        return None
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--repo", gh_repo, "--head", head_branch,
             "--state", "open", "--json", "url,number,baseRefName", "--limit", "1"],
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode != 0:
            return None
        prs = json.loads(result.stdout or "[]")
        return prs[0] if prs else None
    except Exception as e:
        logger.warning("PR rescue via gh failed: %s", e)
        return None


def read_stream_result(run_log: Path) -> dict | None:
    """Returns the final `result` event from the stream-json run log.

    It exists because `claude -p` exits 0 even when the run crashed: a burst
    of API 500s leaves `is_error: true` in the stream and a clean exit code.
    Without this, dispatch reads rc=0, the rescue finds the PR from an
    earlier run and a failure is reported as a success.
    """
    try:
        last = None
        with open(run_log, errors="replace") as f:
            for line in f:
                # Logs from when claude ran inside a pty carry stray \r, and
                # the order of the JSON keys is not guaranteed, so we have to
                # parse it to know the type.
                line = line.strip().strip("\r")
                if not line.startswith("{") or '"result"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("type") == "result":
                    last = obj
        return last
    except Exception as e:
        logger.warning("Could not read the stream result in %s: %s", run_log, e)
        return None


def load_strict_result(
    result_path: Path, pipeline: str, gh_repo: str, head_branch: str,
    defaults: dict, run_ok: bool = True,
) -> dict:
    """Loads the result JSON of a strict pipeline, or raises ValueError.

    Unlike the tolerant path, here the PR is load-bearing: without it we do
    not know whether to open stage B or what to comment on the issue.
    """
    data = dict(defaults)
    parsed = None
    if result_path.exists():
        try:
            parsed = json.loads(result_path.read_text())
        except Exception as e:
            logger.error("unreadable %s result JSON: %s", pipeline, e)

    if parsed is None:
        rescued = rescue_pr_from_gh(gh_repo, head_branch)
        if rescued is None:
            raise ValueError(
                f"{pipeline}: there is no .orchestrator-result.json and I did not "
                f"find an open PR with head '{head_branch}' in {gh_repo} either"
            )
        logger.warning(
            "⚠ The agent did NOT write .orchestrator-result.json, but PR %s "
            "exists. Synthesizing the result and continuing — check the run log.",
            rescued.get("url"),
        )
        data.update({
            "pipeline": pipeline,
            "pr_url": rescued.get("url", ""),
            "pr_number": rescued.get("number"),
            "base": rescued.get("baseRefName", ""),
            "status": "ok",
            "summary": "result rebuilt from gh pr list",
            "prs": [{"repo": gh_repo, "url": rescued.get("url", ""), "branch": head_branch}],
        })
        if not run_ok:
            # The PR found may come from an EARLIER run: the rescue looks up
            # by branch, not by run. If this one crashed, treating it as a
            # success hides that there was no review and sends the brief to
            # in-review as if it were ready for a human.
            data["status"] = "result_contract_violation"
            data["summary"] = (
                "the run failed and left no result JSON; PR "
                f"{rescued.get('url')} is from a previous run"
            )
        return data

    data.update(parsed)

    # review-fix does not open a new PR, so we do not require one.
    if pipeline == "review-fix":
        return data

    pr_url = str(data.get("pr_url") or "").strip()
    if not pr_url:
        rescued = rescue_pr_from_gh(gh_repo, head_branch)
        if rescued is None:
            raise ValueError(
                f"{pipeline}: the result JSON has no pr_url and I could not find the PR"
            )
        data["pr_url"] = pr_url = rescued.get("url", "")
        data.setdefault("pr_number", rescued.get("number"))

    m = PR_URL_RE.match(pr_url)
    if m is None:
        raise ValueError(f"{pipeline}: pr_url has an unexpected format: {pr_url!r}")
    if gh_repo and m.group("repo") != gh_repo:
        # A PR in another repo means the agent worked in the wrong sub-repo.
        # Fail loudly: merging it is worse than losing it.
        raise ValueError(
            f"{pipeline}: the PR points to {m.group('repo')} but the brief is for "
            f"{gh_repo} — the agent worked in the wrong sub-repo"
        )
    data.setdefault("pr_number", int(m.group("num")))
    return data


# ── Hardened container isolation ─────────────────────────────────────────────

# `https://user:secret@` in any URL of a .git/config.
TOKEN_IN_URL_RE = re.compile(r"(https?://)[^/@\s:]+:[^@\s]+@")


def _git_origin(repo: Path) -> str | None:
    try:
        r = worktree.host_git(repo, "remote", "get-url", "origin")
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def guard_git_remote(repo: Path, before: str | None) -> None:
    """Leaves the host repo's remote as it was before the run.

    Safety net on top of GIT_HTTPS_ENV: if something inside the container
    still rewrites the remote — a skill that does it, a prompt injection —
    the token does not stay written in the host's `.git/config`.
    """
    try:
        after = _git_origin(repo)
        if before is not None and after is not None and after != before:
            logger.error("The run changed the origin of %s — restoring it", repo)
            worktree.host_git(repo, "remote", "set-url", "origin", before)
        # The real git dir: with --separate-git-dir, or if the repo is itself
        # a worktree, `.git` is a file and not a directory.
        r = worktree.host_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
        cfg = Path(r.stdout.strip()) / "config" if r.returncode == 0 else repo / ".git" / "config"
        if cfg.is_file():
            text = cfg.read_text()
            clean = TOKEN_IN_URL_RE.sub(r"\1", text)
            if clean != text:
                logger.error("There were credentials written in %s — removing them", cfg)
                cfg.write_text(clean)
    except Exception as e:
        logger.warning("Could not check the remote of %s: %s", repo, e)


def prepare_agent_home(config: dict, container_home: str, run_id: str):
    """Throwaway ~/.claude for the hardened container.

    Mounting the host's ~/.claude read-write let a prompt-injected issue add
    a hook in `settings.json`, an agent or an MCP server that would later run
    on the host, with your permissions, in your next interactive session.
    Here the container sees a home built for the run and thrown away at the
    end: no settings, hooks, agents or MCP from the host. The BMAD skills
    live in the project (`/workspace/.claude/skills`), so nothing is lost.

    Auth, in order of preference:
    1. `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) if
       `secrets.claude_oauth_token_env` names it. It goes through the
       inherited environment, never in argv.
    2. Otherwise, a copy of `~/.claude/.credentials.json`. If the run
       refreshes the token, the new copy is written back to the host — only
       if the host did not refresh it in the meantime — because the refresh
       rotates the refresh token and the host's old one would stop working.

    Returns (home, mounts, env, finish). finish does the write-back and
    always runs before the home is deleted.
    """
    home = AGENT_HOMES_DIR / run_id
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    try:
        host_json = json.loads((Path.home() / ".claude.json").read_text())
    except Exception:
        host_json = {}
    slim = {k: host_json[k] for k in CLAUDE_JSON_KEEP if k in host_json}
    (home / ".claude.json").write_text(json.dumps(slim))
    mounts = [
        "-v", f"{claude_dir}:{container_home}/.claude",
        "-v", f"{home / '.claude.json'}:{container_home}/.claude.json",
    ]
    env: dict = {}
    finish = lambda: None  # noqa: E731 — no write-back except in case 2

    token = secrets_env.resolve_secret(config, "claude_oauth_token_env", required=False)
    host_creds = Path.home() / ".claude" / ".credentials.json"
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    elif host_creds.is_file():
        logger.info(
            "No CLAUDE_CODE_OAUTH_TOKEN: the container uses a copy of the "
            "host credentials (recommended: `claude setup-token`)"
        )
        snapshot = host_creds.read_bytes()
        copy = claude_dir / ".credentials.json"
        copy.write_bytes(snapshot)
        os.chmod(copy, 0o600)

        def write_back() -> None:
            try:
                new = copy.read_bytes()
                if new == snapshot or not new.strip():
                    return
                if host_creds.read_bytes() != snapshot:
                    logger.warning(
                        "The Claude token was refreshed in the container and on the "
                        "host at the same time — keeping the host's"
                    )
                    return
                tmp = host_creds.with_name(".credentials.json.orq-tmp")
                tmp.write_bytes(new)
                os.chmod(tmp, 0o600)
                os.replace(tmp, host_creds)
                logger.info("Claude token refreshed in the container → copied to the host")
            except Exception as e:
                logger.warning("Could not return the refreshed token to the host: %s", e)

        finish = write_back
    else:
        logger.error("No CLAUDE_CODE_OAUTH_TOKEN and no ~/.claude/.credentials.json: "
                     "the container will not be able to authenticate")
    return home, mounts, env, finish


def wait_for_run(proc, log_file, timeout_seconds: int, stall_seconds: int) -> str | None:
    """Wait for the docker client. None if it exited on its own; "timeout" or "stall" if it must be cut.

    Stall = the run log did not grow in `stall_seconds`. With stream-json a
    live run writes at least one event per tool call, its own or its
    subagents'; twenty minutes of nothing is a frozen run, and waiting
    for the pipeline timeout wastes up to two hours and holds up the queue.
    `stall_seconds` at 0 turns it off. It measures the open file, not the
    path: deleting or moving the log by hand must not kill a healthy run.
    """
    start = last_growth = time.monotonic()
    last_size = os.fstat(log_file.fileno()).st_size
    while True:
        try:
            proc.wait(timeout=STALL_POLL_SECONDS)
            return None
        except subprocess.TimeoutExpired:
            pass
        now = time.monotonic()
        size = os.fstat(log_file.fileno()).st_size
        if size != last_size:
            last_size, last_growth = size, now
        if now - start >= timeout_seconds:
            cut = "timeout"
        elif stall_seconds and now - last_growth >= stall_seconds:
            cut = "stall"
        else:
            continue
        # It may have exited between the wait and here: a finished run is not cut.
        if proc.poll() is not None:
            return None
        return cut


def effective_returncode(returncode: int, stream: dict | None) -> int:
    """`claude -p` exits 0 even when the run crashed halfway (a burst of
    API 500s, for example). The only place that gets recorded is the
    stream's `result` event, so that wins: 125 = failed despite exit 0.
    """
    if stream is not None and stream.get("is_error") and returncode == 0:
        return 125
    return returncode


# ── Running Claude in Docker ─────────────────────────────────────────────────


def run_claude_in_docker(
    prompt: str,
    brief: "Brief",
    project_path: str,
    vault: str,
    config: dict,
) -> tuple[int, Path, dict]:
    project_name = brief.project
    brief_slug = brief.path.stem
    # pr-strategy from the front matter — if it is "draft" the agent opens PRs
    # as drafts (gh pr create --draft). Any other value, or none, produces
    # regular PRs. We accept both key variants (`pr-strategy` with a hyphen
    # as in the Obsidian template, and `pr_strategy` with an underscore in
    # case someone writes it Python-style).
    pr_strategy = str(
        brief.metadata.get("pr-strategy")
        or brief.metadata.get("pr_strategy")
        or ""
    ).strip().lower()

    # Fields written by the event harness. A hand-written brief does not have
    # them and falls into the "default" pipeline, which behaves as always.
    meta = brief.metadata
    pipeline = str(meta.get("pipeline") or "default").strip().lower()
    if pipeline not in LEAD_TEMPLATES:
        logger.warning(
            "unknown pipeline '%s' in %s — using 'default'",
            pipeline, brief.path.name,
        )
        pipeline = "default"
    subrepo = str(meta.get("subrepo") or "").strip()
    base_branch = str(meta.get("base_branch") or "").strip()
    gh_repo = str(meta.get("gh_repo") or "").strip()
    gh_issue = str(meta.get("gh_issue") or "").strip()
    pr_number = str(meta.get("pr_number") or "").strip()
    sentry_issue_url = str(meta.get("sentry_issue_url") or "").strip()
    ai_pr_label = str(
        ((config.get("repos") or {}).get(gh_repo) or {}).get("pr_label", "ai-generated")
    )
    hardened = pipeline in HARDENED_PIPELINES
    repo_path = Path(project_path) / subrepo if subrepo else Path(project_path)
    # Hardened pipelines work in their own worktree (worktree.py), never in
    # the main checkout. /workspace is the folder with BMAD and the run's
    # dotfiles; the worktree goes in /workspace/$SUBREPO.
    if hardened:
        workspace = worktree.workspace_dir(project_name, project_path, subrepo)
        container_subrepo = worktree.container_subrepo(project_path, subrepo)
    else:
        workspace, container_subrepo = Path(project_path), subrepo

    runs_dir = LOG_DIR / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    plans_dir = LOG_DIR / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    timestamp = f"{datetime.now():%Y%m%d-%H%M%S}"
    run_log = runs_dir / f"{timestamp}-{project_name}.log"
    plan_archive = plans_dir / f"{timestamp}-{project_name}-{brief_slug}.md"

    # Render the pipeline's system prompt. Placeholders are substituted one
    # at a time so a template missing some of them does not break.
    template_path = LEAD_TEMPLATES[pipeline]
    if not template_path.exists():
        raise FileNotFoundError(
            f"Pipeline '{pipeline}' needs {template_path}, which does not exist"
        )
    lead_rendered = template_path.read_text()
    for key, value in {
        "project": project_name,
        "subrepo": container_subrepo,
        "base_branch": base_branch,
        "gh_repo": gh_repo,
        "gh_issue": gh_issue,
        "pr_number": pr_number,
        "brief_slug": brief_slug,
        "ai_pr_label": ai_pr_label,
    }.items():
        lead_rendered = lead_rendered.replace("{" + key + "}", value)
    workspace.mkdir(parents=True, exist_ok=True)
    lead_path = workspace / ".lead-orchestrator.md"
    lead_path.write_text(lead_rendered)

    # Clean up the previous result and plan (in case one was orphaned by an earlier run)
    result_path = workspace / ".orchestrator-result.json"
    result_path.unlink(missing_ok=True)
    plan_path = workspace / ".orchestrator-plan.md"
    plan_path.unlink(missing_ok=True)

    claude_home = Path.home() / ".claude"
    claude_json = Path.home() / ".claude.json"
    host_gitconfig = Path.home() / ".gitconfig"
    host_ssh = Path.home() / ".ssh"
    gh_token = get_gh_token()
    origin_before = _git_origin(repo_path) if hardened else None
    # The branch the run is going to take: it cannot be checked out in your
    # checkout (worktree.branch_holder). still_wanted left the PR's one here.
    agent_branch = brief_slug if pipeline == "issue-fix" else str(meta.get("head_ref") or "")
    run_id = worktree.run_id(timestamp, brief_slug)
    container_name = f"orq-{run_id}"

    # Running the container as the host user keeps the files the agent
    # creates/modifies from being owned by root, which breaks later manual
    # fixes (typically: `chown`s with sudo). HOME points to a world-writable
    # dir created in the image (/home/agent), onto which we bind-mount the
    # host credentials at the path that HOME expects.
    uid_gid = f"{os.getuid()}:{os.getgid()}"
    container_home = "/home/agent"

    cmd = [
        "docker", "run", "--rm",
        # Sin pty, claude es el PID 1 del container: sin un init, los nietos
        # que dejan los Bash del agente quedan zombies.
        "--init",
        "--name", container_name,
        "--network=host",
        "--user", uid_gid,
        "-v", f"{workspace}:/workspace",
    ]
    # The worktree mounts are added here, already inside the try: if
    # something blows up after creating it, the finally removes it.
    worktree_mounts_at = len(cmd)

    # The harness pipelines run with a prompt from an external source: the
    # body of an issue or a production error, not something the operator
    # wrote. Their scope is cut down on two fronts.
    if hardened:
        # 1. Only the memory folder, not the whole vault. It is the only
        #    thing the pipeline needs from Obsidian.
        memoria_dir = Path(vault) / config["memory_dir"]
        memoria_dir.mkdir(parents=True, exist_ok=True)
        cmd += ["-v", f"{memoria_dir}:/obsidian/{config['memory_dir']}"]
        # 2. No SSH key. The agent pushes over HTTPS with GH_TOKEN, which it
        #    already has and whose scope is only repos. The SSH key is worth
        #    much more and the pipeline does not need it. Auth goes through
        #    GIT_HTTPS_ENV.
        # 3. A throwaway ~/.claude instead of the host's.
        agent_home, home_mounts, extra_env, finish_home = prepare_agent_home(
            config, container_home, run_id,
        )
        cmd += home_mounts
    else:
        agent_home, extra_env, finish_home = None, {}, None
        cmd += ["-v", f"{vault}:/obsidian"]
        cmd += ["-v", f"{host_ssh}:{container_home}/.ssh:ro"]
        cmd += [
            "-v", f"{claude_home}:{container_home}/.claude",
            "-v", f"{claude_json}:{container_home}/.claude.json",
        ]
    # The host gitconfig is optional — if the user has none, the container
    # falls back to what the Dockerfile copied to /root/.gitconfig (which
    # will not be read because HOME is no longer /root, but git can use
    # /etc/gitconfig if it exists). In practice, every host has one.
    if host_gitconfig.exists():
        cmd += ["-v", f"{host_gitconfig}:{container_home}/.gitconfig:ro"]

    cmd += [
        "-e", f"HOME={container_home}",
        "-e", f"CLAUDE_PROJECT={project_name}",
        "-e", f"BRIEF_SLUG={brief_slug}",
        "-e", f"GH_TOKEN={gh_token}",
        "-e", f"MAX_ITERATIONS={config['max_iterations']}",
        "-e", f"PR_STRATEGY={pr_strategy}",
        "-e", "IS_SANDBOX=1",
        # Contract with the pipeline. SENTRY_AUTH_TOKEN is left out on purpose:
        # the Sentry context already comes inlined in the brief.
        "-e", f"PIPELINE={pipeline}",
        "-e", f"SUBREPO={container_subrepo}",
        "-e", f"BASE_BRANCH={base_branch}",
        "-e", f"GH_REPO={gh_repo}",
        "-e", f"GH_ISSUE={gh_issue}",
        "-e", f"PR_NUMBER={pr_number}",
        "-e", f"SENTRY_ISSUE_URL={sentry_issue_url}",
        "-e", f"AI_PR_LABEL={ai_pr_label}",
        # The switch the BMAD overrides hang on. Without it, the skills
        # HALT waiting for a human who is not there.
        "-e", f"HEADLESS={'1' if hardened else '0'}",
    ]
    if hardened:
        for key, value in GIT_HTTPS_ENV.items():
            cmd += ["-e", f"{key}={value}"]
    # Only the name: docker inherits it from this process's environment and
    # the value never shows up in `ps`.
    for key in extra_env:
        cmd += ["-e", key]
    cmd += ["-w", "/workspace", config["docker_image"]]

    # claude writes straight into dispatch's pipe, with no pty. It used to be
    # wrapped in `script -qfc` so Node would think it was in a terminal and
    # not buffer; with stream-json that is no longer needed, because every
    # event goes out on its own line as it happens. And the pty was costly:
    # with stdin on /dev/null, `script` sometimes spins without draining the
    # pty, claude blocks on write and the run freezes until the timeout.
    claude_args = [
        "claude", "-p", prompt,
        "--append-system-prompt-file", "/workspace/.lead-orchestrator.md",
        "--dangerously-skip-permissions",
        # `claude -p` in text mode prints NOTHING until the final message, so
        # a two-hour run that dies halfway leaves an empty log and zero
        # evidence of how far it got. With stream-json every tool call lands
        # in the log as it happens, and the stall watchdog has something to
        # measure.
        "--output-format", "stream-json", "--verbose",
    ]
    if hardened:
        # The worktree was created with --no-checkout: the checkout runs here,
        # in the container, with whatever filters and LFS the repo has, not on the host.
        cmd += ["sh", "-c", 'git -C "/workspace/$SUBREPO" reset --hard --quiet && exec '
                + " ".join(shlex.quote(a) for a in claude_args)]
    else:
        cmd += claude_args

    # issue-fix needs more than the default (plan + implementation + two
    # review passes); review-fix needs quite a bit less.
    timeout_seconds = int(
        (config.get("timeouts") or {}).get(pipeline, config["claude_timeout_seconds"])
    )
    stall_seconds = config.get("stall_timeout_seconds", 0)
    logger.info(
        "docker run %s [pipeline=%s, timeout=%ss] (log: %s)",
        project_name, pipeline, timeout_seconds, run_log,
    )
    pr_data: dict = {"prs": [], "summary": "", "status": "ok", "iterations": 0, "blockers": []}
    timed_out = False
    container_exited = False
    wt = None
    try:
        if hardened:
            wt, why = worktree.create(repo_path, base_branch, run_id, agent_branch)
            if wt is None:
                raise worktree.WorktreeError(why)
            # The worktree's .git points to the repo's git dir with an absolute
            # host path: it is mounted at the same path. hooks/ goes on top,
            # read-only: a hook planted there would run the next time someone
            # uses git by hand in the main checkout.
            (workspace / container_subrepo).mkdir(exist_ok=True)
            hooks = wt.git_dir / "hooks"
            hooks.mkdir(exist_ok=True)
            cmd[worktree_mounts_at:worktree_mounts_at] = [
                "-v", f"{wt.path}:/workspace/{container_subrepo}",
                "-v", f"{wt.git_dir}:{wt.git_dir}",
                "-v", f"{hooks}:{hooks}:ro",
            ]
        with open(run_log, "w") as f:
            f.write(f"# Run: {project_name} @ {datetime.now().isoformat()}\n\n")
            f.flush()
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=f,
                stderr=subprocess.STDOUT,
                text=True,
                env={**os.environ, **extra_env},
            )
            try:
                cut = wait_for_run(proc, f, timeout_seconds, stall_seconds)
            finally:
                # Timeout, stall, exception or Ctrl-C: the docker client dies
                # here. The finally below kills the container.
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
            if cut is None:
                returncode = proc.returncode
                container_exited = True
            else:
                if cut == "stall":
                    logger.error(
                        "No output in the run log for %ss in %s [pipeline=%s]: "
                        "run frozen, cutting it",
                        stall_seconds, project_name, pipeline,
                    )
                else:
                    logger.error(
                        "Timeout (%ss) in %s [pipeline=%s]",
                        timeout_seconds, project_name, pipeline,
                    )
                # Telegram shows the tail of the run log: make the reason visible there.
                f.write(f"\n# dispatch: run cut by {cut} "
                        f"({stall_seconds if cut == 'stall' else timeout_seconds}s)\n")
                f.flush()
                timed_out = True
                returncode = 124

        # `claude -p` exits 0 even when the run crashed halfway: the stream's
        # `result` event wins (see effective_returncode).
        stream = read_stream_result(run_log)
        if stream is not None:
            pr_data["turns"] = stream.get("num_turns")
            pr_data["cost_usd"] = stream.get("total_cost_usd")
            logger.info(
                "Run %s: %s turns=%s cost=$%.2f is_error=%s",
                project_name, stream.get("subtype"), stream.get("num_turns"),
                stream.get("total_cost_usd") or 0.0, stream.get("is_error"),
            )
        if effective_returncode(returncode, stream) != returncode:
            logger.error(
                "The stream reports is_error=true despite exit 0 in %s "
                "[pipeline=%s] — treating it as a failed run.",
                project_name, pipeline,
            )
            returncode = effective_returncode(returncode, stream)

        if pipeline in STRICT_RESULT_PIPELINES:
            # The failure propagates as pr_data["status"], not as an exception,
            # so the finally cleans up the dotfiles as always.
            try:
                pr_data = load_strict_result(
                    result_path, pipeline, gh_repo, brief_slug, pr_data,
                    run_ok=(returncode == 0),
                )
            except ValueError as e:
                logger.error("%s", e)
                pr_data["status"] = "result_contract_violation"
                pr_data["summary"] = str(e)
            if pipeline == "issue-fix" and str(pr_data.get("status") or "ok") == "ok":
                problem = verify_stage_a(gh_repo, pr_data.get("pr_number"), repo_path, brief_slug)
                if problem:
                    logger.error("Stage A incomplete in %s: %s", pr_data.get("pr_url"), problem)
                    pr_data["status"] = "result_contract_violation"
                    pr_data["summary"] = problem
        elif result_path.exists():
            try:
                pr_data.update(json.loads(result_path.read_text()))
            except Exception as e:
                logger.warning(
                    "Could not parse .orchestrator-result.json: %s", e,
                )

        # Archive the architect's plan (if any) and warn if there is none.
        # The plan is an external artifact: it never stays in the project
        # repo, only in the host's ~/.orchestrator/logs/plans/ for auditing.
        if plan_path.exists():
            try:
                plan_archive.write_text(plan_path.read_text())
                pr_data["plan_archive"] = str(plan_archive)
                logger.info(
                    "Architect plan archived: %s", plan_archive,
                )
            except Exception as e:
                logger.warning(
                    "Could not archive .orchestrator-plan.md: %s", e,
                )
        elif pipeline not in STRICT_RESULT_PIPELINES:
            logger.warning(
                "⚠ ARCHITECT DID NOT WRITE .orchestrator-plan.md — "
                "the agent skipped planning Step 0. "
                "Brief: %s, project: %s. "
                "Check the run log to understand what happened.",
                brief.path.name, project_name,
            )
            pr_data["plan_archive"] = None

        return returncode, run_log, pr_data
    finally:
        # Killing the docker client (timeout, exception, Ctrl-C) does not stop
        # the container: it would keep working, and pushing, with nobody
        # reading its output. Before removing its worktree, not after.
        if not container_exited:
            worktree.kill_container(container_name)
        if lead_path.exists():
            lead_path.unlink()
        result_path.unlink(missing_ok=True)
        plan_path.unlink(missing_ok=True)
        if hardened:
            guard_git_remote(repo_path, origin_before)
            unsafe = worktree.unsafe_git_config(repo_path)
            if unsafe:
                logger.error("After the run, the config of %s is no longer safe: %s. The "
                             "next run will reject it; check .git/config by hand",
                             repo_path, unsafe)
        if wt is not None:
            worktree.remove(repo_path, wt)
        if finish_home is not None:
            finish_home()
        if agent_home is not None:
            shutil.rmtree(agent_home, ignore_errors=True)


# ── Closing a brief: done or failed ──────────────────────────────────────────


def mark_state(
    brief: Brief,
    state: str,
    extra: dict | None = None,
    vault: str | None = None,
    move_to: str | None = None,
) -> Path:
    """Writes the state to the front matter and, optionally, moves the brief.

    The wrappers below (mark_done_and_move / mark_failed / mark_pr_open) are
    the interface the rest of the module uses; this function exists so that
    adding a state does not mean duplicating the frontmatter+shutil logic.
    """
    post = frontmatter.load(brief.path)
    post.metadata["status"] = state
    for key, value in (extra or {}).items():
        if value not in (None, "", [], {}):
            post.metadata[key] = value
    brief.path.write_text(frontmatter.dumps(post))

    if move_to is None or vault is None:
        return brief.path

    target_dir = Path(vault) / "projects" / brief.project / move_to
    target_dir.mkdir(parents=True, exist_ok=True)
    new_path = target_dir / brief.path.name
    shutil.move(str(brief.path), str(new_path))
    logger.info("Brief %s → %s", state, new_path)
    return new_path


def mark_running(brief: Brief) -> None:
    """Marks the brief as in progress before starting the container.

    find_pending_briefs() only picks up `pending` ones, so this keeps a crash
    or a reboot mid-run from making the next pass blindly re-run it against
    a branch that may already exist and a PR that may already be open.
    """
    mark_state(brief, "running", {"started_at": datetime.now().isoformat()})


def mark_pr_open(brief: Brief, vault: str, pr_data: dict) -> Path:
    """Stage A finished: there is a PR and a published review, the human is next.

    It is terminal for dispatch. The brief moves to in-review/ and is only
    closed when the merge webhook moves it to completed/.
    """
    findings = pr_data.get("findings_count") or {}
    return mark_state(
        brief,
        "pr_open",
        {
            "completed_at": datetime.now().isoformat(),
            "pr_url": pr_data.get("pr_url", ""),
            "pr_number": pr_data.get("pr_number"),
            "review_comment_url": pr_data.get("review_comment_url", ""),
            "findings_count": findings,
            "threads_opened": pr_data.get("threads_opened"),
            "folded_into_rollup": pr_data.get("folded_into_rollup"),
            "summary": pr_data.get("summary", ""),
            "prs": pr_data.get("prs") or [],
        },
        vault=vault,
        move_to="in-review",
    )


def mark_done_and_move(brief: Brief, vault: str, pr_data: dict | None = None) -> Path:
    post = frontmatter.load(brief.path)
    post.metadata["status"] = "done"
    post.metadata["completed_at"] = datetime.now().isoformat()
    if pr_data and pr_data.get("prs"):
        post.metadata["prs"] = pr_data["prs"]
    if pr_data and pr_data.get("summary"):
        post.metadata["summary"] = pr_data["summary"]
    if pr_data is not None:
        # Always write iterations (0 if the agent did not report it) so the
        # feedback loop stays traceable in the completed brief.
        post.metadata["iterations"] = pr_data.get("iterations", 0)
    brief.path.write_text(frontmatter.dumps(post))

    completed_dir = Path(vault) / "projects" / brief.project / "completed"
    completed_dir.mkdir(parents=True, exist_ok=True)
    new_path = completed_dir / brief.path.name
    shutil.move(str(brief.path), str(new_path))
    logger.info("Brief done → %s", new_path)
    return new_path


def mark_failed(brief: Brief, error_msg: str, pr_data: dict | None = None) -> None:
    post = frontmatter.load(brief.path)
    post.metadata["status"] = "failed"
    post.metadata["failed_at"] = datetime.now().isoformat()
    post.metadata["last_error"] = error_msg[:500]
    if pr_data is not None:
        post.metadata["iterations"] = pr_data.get("iterations", 0)
        if pr_data.get("blockers"):
            post.metadata["blockers"] = pr_data["blockers"]
        if pr_data.get("status"):
            post.metadata["abort_status"] = pr_data["status"]
    brief.path.write_text(frontmatter.dumps(post))
    logger.info("Brief failed → %s", brief.path)


def record_event_state(
    config: dict, brief: Brief, state: str, pr_data: dict | None = None,
    brief_path: str | None = None, error: str | None = None,
) -> None:
    """Mirrors the brief's outcome in the event ledger.

    Best-effort on purpose: the ledger is for auditing and for deduping
    future webhooks, not for deciding what to run. If it fails, the brief
    already holds the truth in its front matter and the pass does not crash.
    """
    event_id = brief.metadata.get("event_id")
    if not event_id:
        return
    try:
        from talos import store

        fields: dict = {}
        if pr_data:
            if pr_data.get("pr_url"):
                fields["pr_url"] = pr_data["pr_url"]
            if pr_data.get("pr_number"):
                fields["pr_number"] = pr_data["pr_number"]
        if brief_path:
            fields["brief_path"] = brief_path
        if error:
            fields["last_error"] = error[:500]
        with store.connect() as conn:
            store.set_state(conn, int(event_id), state, **fields)
    except Exception as e:
        logger.warning("Could not update event %s in the ledger: %s", event_id, e)


def log_pr_summary(brief: Brief, pr_data: dict) -> None:
    """Logs the created PRs so the user sees them without opening the front
    matter or the run log."""
    if str(brief.metadata.get("pipeline") or "").strip().lower() == "review-fix":
        # review-fix never opens a PR: it commits to the one under review, so
        # an empty `prs` does not mean nothing was committed.
        pr = pr_data.get("pr_url") or brief.metadata.get("pr_url") or (
            f"{brief.metadata.get('gh_repo') or '?'}#{brief.metadata.get('pr_number') or '?'}"
        )
        threads = []
        if pr_data.get("threads_resolved") is not None:
            threads.append(f"threads resolved: {pr_data['threads_resolved']}")
        if pr_data.get("threads_open") is not None:
            threads.append(f"declined or unresolved: {pr_data['threads_open']}")
        logger.info(
            "Review-fix of %s on PR %s%s",
            brief.path.name, pr, (" — " + ", ".join(threads)) if threads else "",
        )
        # Not expected from review-fix, so it is worth seeing when it happens.
        for extra in pr_data.get("prs") or []:
            if isinstance(extra, dict):
                logger.warning(
                    "  review-fix also reported a PR: %s @ %s → %s",
                    extra.get("repo") or "(mono-repo)", extra.get("branch") or "(no branch)",
                    extra.get("url") or "(no URL)",
                )
        return
    prs = pr_data.get("prs") or []
    if not prs:
        logger.info(
            "PRs created for %s: none (no committable changes)",
            brief.path.name,
        )
        return
    logger.info("PRs created for %s (%d):", brief.path.name, len(prs))
    for pr in prs:
        repo = pr.get("repo") or "(mono-repo)"
        url = pr.get("url", "(no URL)")
        branch = pr.get("branch", "(no branch)")
        logger.info("  • %s @ %s → %s", repo, branch, url)


def alert_iteration_cap(brief: Brief, pr_data: dict) -> None:
    """Prominent alert when the architect aborts a brief on max-iterations.

    For now it only goes to the terminal via logger.error. TODO: also send
    it to Telegram with richer formatting — see README "In progress".
    """
    summary = pr_data.get("summary", "(no summary)")
    iterations = pr_data.get("iterations", "?")
    blockers = pr_data.get("blockers") or []
    blockers_block = (
        "\n      ".join(f"- {b}" for b in blockers) if blockers else "(none reported)"
    )
    banner = (
        "\n"
        "╔══════════════════════════════════════════════════════════════════╗\n"
        "║  ⚠  BRIEF ABORTED — iteration cap reached                        ║\n"
        "╚══════════════════════════════════════════════════════════════════╝\n"
        f"  brief:       {brief.path.name}\n"
        f"  project:     {brief.project}\n"
        f"  iterations:  {iterations}\n"
        f"  summary:     {summary}\n"
        f"  blockers:    {blockers_block}\n"
        "  The branch stayed local with the devs' commits, it was NEVER pushed.\n"
        "  Check the run log and decide whether to pick it up by hand or rewrite the brief.\n"
    )
    logger.error(banner)


# ── Telegram notification ────────────────────────────────────────────────────


def notify_telegram(
    config: dict,
    brief_name: str,
    project: str,
    status: str,
    run_log_path: Path | None,
    pr_data: dict | None = None,
) -> None:
    token = config.get("telegram_bot_token", "")
    chat_id = config.get("telegram_chat_id", "")
    if not token or not chat_id:
        logger.debug("Telegram not configured, skip notify")
        return

    emoji = {
        "done": "✅",
        "aborted": "⚠️",
        "pr_open": "🔀",
        "review_published": "🔍",
        "threads_resolved": "🧹",
        "merged": "🎉",
        "merged_dev": "📦",
        "human_review": "👀",
    }.get(status, "❌")
    last_lines = ""
    if run_log_path is not None:
        try:
            output = run_log_path.read_text()
            last_lines = "\n".join(output.strip().splitlines()[-5:])
        except Exception:
            last_lines = ""

    # HTML and not legacy Markdown: with Markdown, a stray `_` or `*` in a
    # title (snake_case, `decision_needed`, `repos[*]`) makes Telegram answer
    # 400, and since `requests` does not raise on that, the notification was
    # silently lost.
    e = html.escape
    pr_lines = []
    if pr_data and pr_data.get("prs"):
        for pr in pr_data["prs"]:
            repo = pr.get("repo") or project
            url = pr.get("url", "")
            branch = pr.get("branch", "")
            pr_lines.append(f"PR ({e(str(repo))}): {e(str(url))}\nBranch: <code>{e(str(branch))}</code>")
    pr_block = ("\n".join(pr_lines) + "\n") if pr_lines else ""

    review_block = ""
    if status == "pr_open" and pr_data:
        fc = pr_data.get("findings_count") or {}
        findings = ", ".join(f"{k}: {v}" for k, v in fc.items()) or "no findings"
        review_block = (
            f"PR: {e(str(pr_data.get('pr_url', '?')))}\n"
            f"Review published ({e(findings)})\n"
            f"Open threads: <code>{e(str(pr_data.get('threads_opened', 0)))}</code>\n"
            "Waiting for human review — the merge is up to you.\n"
        )

    merge_block = ""
    if status in ("merged", "merged_dev") and pr_data:
        base = str(pr_data.get("base") or "")
        prod = str(pr_data.get("prod") or "") or "prod"
        if pr_data.get("pr_url"):
            merge_block += f"PR: {e(str(pr_data['pr_url']))}\n"
        if status == "merged_dev":
            merge_block += (f"Merged to <code>{e(base or '?')}</code>. The issue closes itself "
                            f"once it reaches <code>{e(prod)}</code>.\n")
        elif base:
            merge_block += f"Merged to <code>{e(base)}</code>.\n"

    abort_block = ""
    if status == "aborted" and pr_data:
        iterations = pr_data.get("iterations", "?")
        summary = pr_data.get("summary", "")
        abort_block = (
            f"Iterations: <code>{e(str(iterations))}</code> (cap reached)\n"
            f"Reason: {e(str(summary))}\n"
            "Local branch — not pushed.\n"
        )

    head = (
        f"{emoji} <b>{e(project)}</b> — <code>{e(brief_name)}</code>\n"
        f"Status: <code>{e(status)}</code>\n"
        f"{review_block}"
        f"{merge_block}"
        f"{abort_block}"
        f"{pr_block}"
    )[:3000]
    # Trim the raw text BEFORE wrapping it: cutting already-built HTML can
    # split a tag, and Telegram rejects the whole message.
    budget = 4000 - len(head) - len("\n<pre></pre>")
    tail = e(last_lines)[-budget:] if budget > 0 else ""
    text = head + (f"\n<pre>{tail}</pre>" if tail else "")

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if not resp.ok:
            logger.warning("Telegram rejected the notification (%s): %s",
                           resp.status_code, resp.text[:200])
    except Exception as e:
        logger.debug("Telegram notification failed: %s", e)


# ── Pipeline ─────────────────────────────────────────────────────────────────


def _gh_api(path: str, timeout: int = 20):
    """`gh api` → JSON, or None if the query failed."""
    if shutil.which("gh") is None:
        return None
    try:
        r = subprocess.run(
            ["gh", "api", "-H", "Accept: application/vnd.github+json", path],
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode != 0:
            logger.warning("gh api %s → exit %d: %s", path, r.returncode, r.stderr.strip()[:200])
            return None
        return json.loads(r.stdout or "null")
    except Exception as e:
        logger.warning("gh api %s failed: %s", path, e)
        return None


def verify_stage_a(gh_repo: str, pr_number, repo_path: Path, branch: str) -> str:
    """What stage A has to leave on GitHub, checked against what the agent says.

    Returns "" if complete, or what is missing. It exists because a run that
    ended its turn "waiting for the build" left the PR open with no published
    review and the last commit unpushed, and the rescue by branch took it as
    a success: the PR sat in in-review as if someone had reviewed it. If
    GitHub does not answer, the run is not failed: we do not know.
    """
    from talos.util import is_harness_authored

    if not (gh_repo and pr_number):
        return ""
    problems = []
    reviews = _gh_api(f"repos/{gh_repo}/pulls/{pr_number}/reviews?per_page=100")
    if isinstance(reviews, list) and not any(is_harness_authored(r.get("body")) for r in reviews):
        problems.append("the bmad-code-review review is not published on the PR")
    pr = _gh_api(f"repos/{gh_repo}/pulls/{pr_number}")
    try:
        local = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", f"refs/heads/{branch}"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except Exception:
        local = ""
    remote = ((pr or {}).get("head") or {}).get("sha", "") if isinstance(pr, dict) else ""
    if local and remote and local != remote:
        ahead = subprocess.run(
            ["git", "-C", str(repo_path), "rev-list", "--count", f"{remote}..{local}"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if ahead and ahead != "0":
            problems.append(f"the local branch has {ahead} unpushed commit(s)")
    return "; ".join(problems)


def still_wanted(config: dict, brief: Brief) -> tuple[bool | None, str, str]:
    """Does running this brief still make sense? → (yes/no/unknown, reason, gate).

    What changes between admission and dispatch does not go through the
    ledger: an `ai-skip` added after admission only bumped `seen_count`, and
    closing the issue is not even a subscribed event. One API call before
    spending up to two hours of container time is cheap. `None` = could not
    verify, and the brief is deferred: a gh failure does not admit work.
    """
    meta = brief.metadata
    pipeline = str(meta.get("pipeline") or "default").strip().lower()
    gh_repo = str(meta.get("gh_repo") or "").strip()
    if not meta.get("event_id") or not gh_repo or shutil.which("gh") is None:
        return True, "", ""

    if pipeline == "issue-fix" and meta.get("gh_issue"):
        issue = _gh_api(f"repos/{gh_repo}/issues/{meta['gh_issue']}")
        if issue is None:
            return None, f"could not query {gh_repo}#{meta['gh_issue']}", ""
        if issue.get("state") != "open":
            return False, "the issue was closed after it was admitted", "closed"
        skip = ((config.get("repos") or {}).get(gh_repo) or {}).get("skip_label", "ai-skip")
        labels = {
            (lbl.get("name") if isinstance(lbl, dict) else str(lbl))
            for lbl in (issue.get("labels") or [])
        }
        if skip and skip in labels:
            return False, f"the issue got '{skip}' after it was admitted", "optout"
    elif pipeline == "review-fix" and meta.get("pr_number"):
        pr = _gh_api(f"repos/{gh_repo}/pulls/{meta['pr_number']}")
        if pr is None:
            return None, f"could not query PR {gh_repo}#{meta['pr_number']}", ""
        if pr.get("state") != "open":
            return False, "the PR is no longer open", "pr_closed"
        # The branch `gh pr checkout` is going to take. run_claude_in_docker
        # refuses to run if it is checked out in your checkout.
        meta["head_ref"] = str((pr.get("head") or {}).get("ref") or "")
    return True, "", ""


def enrich_sentry_brief(config: dict, brief: Brief) -> str:
    """Fills in the Sentry context over REST when the brief is picked up.

    webhookd writes the brief with only what the payload carries: fetching
    the issue and the latest event inside the handler meant two GETs of up to
    20s, and Sentry cuts off at ~1s. Here there is no rush.

    Returns a reason to skip the brief if the issue is no longer unresolved,
    or "" to continue. If REST fails we continue with the webhook context: a
    brief with partial context is more useful than none.
    """
    meta = brief.metadata
    if meta.get("source") != "sentry" or meta.get("sentry_context") != "webhook":
        return ""
    issue_id = str(meta.get("sentry_issue_id") or "")
    token = secrets_env.resolve_secret(config, "sentry_auth_token_env", required=False)
    if not (issue_id and token):
        return ""
    try:
        from talos import brief_factory
        from talos import sentry_api

        issue, context_md = sentry_api.build_context(issue_id, token)
    except Exception as e:
        logger.warning("Could not fetch the Sentry context for %s (continuing with the webhook's): %s",
                       issue_id, e)
        return ""

    status = str(issue.get("status") or "unresolved")
    if status != "unresolved":
        return f"the Sentry issue became '{status}' after it was admitted"

    start, end = brief_factory.SENTRY_CONTEXT_START, brief_factory.SENTRY_CONTEXT_END
    body = brief.body
    if start in body and end in body:
        head, rest = body.split(start, 1)
        _, tail = rest.split(end, 1)
        body = f"{head}{start}\n{context_md}\n{end}{tail}"
    post = frontmatter.load(brief.path)
    post.content = body
    post.metadata["sentry_context"] = "rest"
    brief.path.write_text(frontmatter.dumps(post))
    brief.body = body
    brief.metadata["sentry_context"] = "rest"
    return ""


def skip_brief(config: dict, brief: Brief, reason: str, gate: str) -> dict:
    """The brief no longer applies: it leaves ToDos/ without running, with the reason."""
    new_path = mark_state(
        brief, "skipped",
        {"skipped_at": datetime.now().isoformat(), "skip_reason": reason},
        vault=config["vault"], move_to="skipped",
    )
    record_event_state(config, brief, f"rejected_{gate}", brief_path=str(new_path), error=reason)
    logger.info("Brief %s skipped: %s", brief.path.name, reason)
    return {"brief": brief, "status": "skipped", "pr_data": {"prs": []}}


def cap_verdict(config: dict, brief: Brief):
    """The load caps (D1): only for an event's stage A.

    review-fix does not go through here on purpose: it works on a PR that
    already exists, and blocking it with the open-PR cap would be a deadlock
    — you cannot merge what never gets fixed. It is bounded by
    `max_review_fix_rounds`.
    """
    from talos import triage

    meta = brief.metadata
    pipeline = str(meta.get("pipeline") or "default").strip().lower()
    gh_repo = str(meta.get("gh_repo") or "").strip()
    if pipeline != "issue-fix" or not meta.get("event_id") or not gh_repo:
        return triage.OK
    try:
        from talos import store

        with store.connect() as conn:
            return triage.check_dispatch_caps(config, gh_repo, conn)
    except Exception as e:
        logger.warning("Could not evaluate the caps for %s: %s", gh_repo, e)
        return triage.Verdict(False, "cap_check_failed", str(e))


def defer_brief(config: dict, brief: Brief, verdict) -> None:
    """A cap applies: the brief stays `pending` and runs once there is room."""
    logger.info("Brief %s deferred (deferred_%s): %s",
                brief.path.name, verdict.gate, verdict.detail)
    event_id = brief.metadata.get("event_id")
    if not event_id:
        return
    try:
        from talos import store

        with store.connect() as conn:
            store.annotate(conn, int(event_id),
                           last_error=f"deferred_{verdict.gate}: {verdict.detail}"[:500])
    except Exception as e:
        logger.warning("Could not record the deferral of %s: %s", event_id, e)


def _count(value, default: int = 0) -> int | None:
    """A whole, non-negative count from the agent's result JSON, or None when it is not one.

    `json.loads` accepts `Infinity` and `NaN`, and the agent may write
    `true`, `-1`, `2.5` or `"12"`: only the last one is a count.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None
        value = int(value)
    elif isinstance(value, str):
        if not re.fullmatch(r"[0-9]+", value.strip()):
            return None
        value = int(value.strip())
    elif not isinstance(value, int):
        return None
    return value if value >= 0 else None


def check_thread_count(brief: Brief, pr_data: dict) -> None:
    """Warn when stage A's thread count does not add up to its findings.

    Every `patch` and `decision_needed` finding gets its own thread, or is
    folded into the roll-up when GitHub rejects the inline comment. A gap
    means findings were grouped in one thread (PR #89: `patch: 12`, 11
    threads), which breaks the one-commit-per-finding mapping of stage B.
    Also warns when findings were folded while there are `patch` findings:
    review-fix only reads threads, so a folded `patch` is never fixed.
    Only warnings: the review is published and the run is still useful.
    """
    pr_url = pr_data.get("pr_url") or "?"
    fc = pr_data.get("findings_count")
    if fc is None:
        fc = {}
    if not isinstance(fc, dict):
        logger.warning(
            "findings_count of %s (%s) is not an object: %r — cannot check the threads",
            brief.path.name, pr_url, fc,
        )
        return
    raw = {
        "patch": fc.get("patch"),
        "decision_needed": fc.get("decision_needed"),
        "threads_opened": pr_data.get("threads_opened"),
        "folded_into_rollup": pr_data.get("folded_into_rollup"),
    }
    counts = {k: _count(v) for k, v in raw.items()}
    bad = {k: raw[k] for k, v in counts.items() if v is None}
    if bad:
        logger.warning(
            "Invalid counts in the result of %s (%s): %s — cannot check the threads",
            brief.path.name, pr_url, ", ".join(f"{k}={v!r}" for k, v in bad.items()),
        )
        return
    patches, decisions = counts["patch"], counts["decision_needed"]
    threads, folded = counts["threads_opened"], counts["folded_into_rollup"]
    if patches + decisions != threads + folded:
        logger.warning(
            "Thread count does not add up in %s (%s): patch %d + decision_needed %d = %d, "
            "but threads_opened %d + folded_into_rollup %d = %d — "
            "findings grouped in a single thread?",
            brief.path.name, pr_url,
            patches, decisions, patches + decisions, threads, folded, threads + folded,
        )
    if folded and patches:
        logger.warning(
            "%s (%s): %d finding(s) were folded into the roll-up. The `patch` ones among "
            "them have no thread, so review-fix does not see them: they need a human",
            brief.path.name, pr_url, folded,
        )


def enqueue_review_round(config: dict, brief: Brief, pr_data: dict) -> None:
    """Stage B, round 1, straight from here when the automated review left `patch`.

    The review stage A publishes carries the anti-loop marker, so its
    webhook is rejected — on purpose: otherwise the harness would trigger
    itself forever. Without this shortcut, the `patch` findings of the
    review itself waited for a human to comment, and the stage B the README
    promises ("the same route works for the review the bot publishes") did
    not exist. The round goes through the same door as the webhook ones
    (`open_review_round`): it counts against `max_review_fix_rounds` and
    respects the round in flight.
    """
    fc = pr_data.get("findings_count") or {}
    if not isinstance(fc, dict):
        return
    patches = _count(fc.get("patch"))
    threads = _count(pr_data.get("threads_opened"))
    number = _count(pr_data.get("pr_number"))
    if patches is None or threads is None or number is None:
        return
    gh_repo = str(brief.metadata.get("gh_repo") or "")
    if not (patches and threads and number and gh_repo):
        return
    try:
        from talos import webhookd

        target = resolve_gh_repo(config, gh_repo)
        if target is None or not target.enabled or target.dry_run:
            return
        res = webhookd.open_review_round(
            config, target,
            number=number,
            pr_url=str(pr_data.get("pr_url") or ""),
            title=brief.path.stem,
            head_ref=brief.path.stem,
            trigger=f"stage A automated review ({patches} patch)",
            trigger_at=utcnow(),
            origin="dispatch",
            kick=False,
        )
        logger.info("Stage B for PR #%s → %s", number, res.get("state"))
    except Exception as e:
        logger.warning("Could not enqueue the review-fix round for PR #%s: %s", number, e)


def _fail(config: dict, brief: Brief, reason: str, run_log: Path | None,
          pr_data: dict, status: str = "failed") -> dict:
    """Closes out a run that did not make it: brief, ledger, source and Telegram.

    Previously only `result_contract_violation` did all four. An exit
    ≠ 0 or a timeout left the row in `running` until the reaper marked it
    `orphaned` — with a misleading reason — and the issue got no notice.
    """
    mark_failed(brief, reason, pr_data)
    record_event_state(config, brief, "failed", pr_data, error=reason)
    try:
        from talos import feedback

        feedback.announce_failure(config, brief.metadata, reason, str(run_log or ""))
    except Exception as e:
        logger.warning("Feedback to the source failed: %s", e)
    notify_telegram(config, brief.path.name, brief.project, status, run_log, pr_data)
    return {"brief": brief, "status": "aborted" if status == "aborted" else "failed",
            "pr_data": pr_data}


def process_brief(brief: Brief, config: dict, dry_run: bool) -> dict | None:
    """Processes a brief and returns the result so run_once can add it to
    the final summary. Returns None if the brief was skipped (invalid
    project, dry-run, etc.) or a dict with keys: status, pr_data, brief."""
    project_path = config["projects"].get(brief.project)
    if not project_path:
        logger.warning(
            "Project '%s' is not in config.yaml — skip %s",
            brief.project, brief.path.name,
        )
        return None

    if not Path(project_path).exists():
        logger.warning(
            "Folder for project '%s' does not exist: %s — skip",
            brief.project, project_path,
        )
        return None

    pipeline = str(brief.metadata.get("pipeline") or "default").strip().lower()

    memory_path = ensure_memory_file(
        config["vault"], config["memory_dir"], brief.project,
    )

    if dry_run:
        prompt = build_prompt(memory_path, brief)
        logger.info(
            "[DRY RUN] %s | project=%s priority=%s",
            brief.path.name, brief.project, brief.priority,
        )
        logger.info("[DRY RUN] cwd=%s", project_path)
        logger.info("[DRY RUN] memory=%s", memory_path)
        logger.info(
            "[DRY RUN] prompt (first 500 chars):\n%s",
            prompt[:500],
        )
        return None

    wanted, why, gate = still_wanted(config, brief)
    if wanted is None:
        logger.warning("Brief %s deferred: %s", brief.path.name, why)
        return None
    if wanted is False:
        return skip_brief(config, brief, why, gate)
    why = enrich_sentry_brief(config, brief)
    if why:
        return skip_brief(config, brief, why, "sentry_resolved")
    if pipeline in HARDENED_PIPELINES:
        # Without the kit the agent does not fail: it HALTs until the timeout.
        why = bmad_kit.ensure(worktree.workspace_dir(
            brief.project, project_path, str(brief.metadata.get("subrepo") or "").strip()))
        if why:
            return _fail(config, brief, why, None, {"prs": []})

    prompt = build_prompt(memory_path, brief)
    mark_running(brief)
    # The ledger has to find out BEFORE the container starts: it is the only
    # moment we can leave the `claimed_at` that makes the run visible to the
    # reaper if the machine reboots halfway.
    record_event_state(config, brief, "running", brief_path=str(brief.path))
    try:
        returncode, run_log, pr_data = run_claude_in_docker(
            prompt, brief, project_path, config["vault"], config,
        )
    except worktree.WorktreeError as e:
        logger.error("No worktree for %s: %s", brief.path.name, e)
        return _fail(config, brief, str(e), None, {"prs": []})
    except Exception as e:
        logger.exception("Error running docker for %s: %s", brief.path.name, e)
        return _fail(config, brief, f"docker error: {e}", None, {"prs": []})

    status = str(pr_data.get("status") or "ok").strip().lower()
    if returncode == 0 and status == "aborted_max_iterations":
        # The architect ran fine but aborted the brief for not converging.
        alert_iteration_cap(brief, pr_data)
        return _fail(
            config, brief,
            f"aborted_max_iterations after {pr_data.get('iterations', '?')} cycles: "
            f"{pr_data.get('summary', '')}",
            run_log, pr_data, status="aborted",
        )
    if returncode == 0 and (
        status == "failed" or (pipeline in STRICT_RESULT_PIPELINES and status != "ok")
    ):
        # Clean exit but the agent reported it did not make it: `failed` with
        # blockers, or `result_contract_violation` (it left no usable
        # evidence of the PR). Previously only the latter was checked, so a
        # `failed` with a PR rescued by branch ended up `pr_open` with the
        # `ai-in-review` label, and a failed review-fix ended up `done`. The
        # brief does not move: it stays in ToDos for /retry.
        return _fail(
            config, brief,
            f"{status}: {pr_data.get('summary') or 'no summary'}",
            run_log, pr_data,
        )
    if returncode != 0:
        return _fail(config, brief, f"exit {returncode}, see {run_log}", run_log, pr_data)

    if pipeline == "issue-fix":
        # Terminal for dispatch, not for the work: there is a PR and a
        # published review, and now we wait for a human. The merge closes it.
        check_thread_count(brief, pr_data)
        new_path = mark_pr_open(brief, config["vault"], pr_data)
        log_pr_summary(brief, pr_data)
        record_event_state(config, brief, "pr_open", pr_data, str(new_path))
        try:
            from talos import feedback
            feedback.announce_pr_open(config, brief.metadata, pr_data)
        except Exception as e:
            logger.warning("Feedback to the source failed: %s", e)
        notify_telegram(
            config, brief.path.name, brief.project, "pr_open", run_log, pr_data,
        )
        enqueue_review_round(config, brief, pr_data)
        return {"brief": brief, "status": "pr_open", "pr_data": pr_data}

    new_path = mark_done_and_move(brief, config["vault"], pr_data)
    log_pr_summary(brief, pr_data)
    # The new path goes to the ledger: the merge closes the loop by reading
    # `brief_path`, and an old one points to a file that is no longer there.
    record_event_state(config, brief, "done", pr_data, str(new_path))
    notify_telegram(
        config, brief.path.name, brief.project, "done", run_log, pr_data,
    )
    return {"brief": brief, "status": "done", "pr_data": pr_data}


# ── Lock to prevent concurrent passes ────────────────────────────────────────


class _DispatchLock:
    """fcntl file lock so two passes never run at the same time."""

    def __init__(self) -> None:
        LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._fd = None

    def __enter__(self) -> bool:
        self._fd = open(LOCK_FILE, "w")
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._fd.write(str(os.getpid()))
            self._fd.flush()
            return True
        except BlockingIOError:
            self._fd.close()
            self._fd = None
            return False

    def __exit__(self, *_exc) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                self._fd.close()
                self._fd = None


def log_pass_summary(results: list[dict]) -> None:
    """Logs a consolidated summary of the pass: status per brief and every
    PR created in a single list, so the user can copy them without opening
    individual front matters."""
    if not results:
        return
    done = [r for r in results if r["status"] == "done"]
    aborted = [r for r in results if r["status"] == "aborted"]
    failed = [r for r in results if r["status"] == "failed"]

    logger.info("─" * 70)
    logger.info(
        "Pass summary: %d done, %d aborted, %d failed (total %d)",
        len(done), len(aborted), len(failed), len(results),
    )

    all_prs: list[tuple[str, dict]] = []
    for r in results:
        for pr in r["pr_data"].get("prs") or []:
            all_prs.append((r["brief"].path.name, pr))

    if all_prs:
        logger.info("PRs created in this pass (%d):", len(all_prs))
        for brief_name, pr in all_prs:
            repo = pr.get("repo") or "(mono-repo)"
            url = pr.get("url", "(no URL)")
            branch = pr.get("branch", "(no branch)")
            logger.info("  • [%s] %s @ %s → %s", brief_name, repo, branch, url)
    else:
        logger.info("PRs created in this pass: none")

    if aborted:
        logger.info("Aborted briefs (review manually):")
        for r in aborted:
            iterations = r["pr_data"].get("iterations", "?")
            summary = r["pr_data"].get("summary", "")
            logger.info(
                "  • %s — iterations=%s — %s",
                r["brief"].path.name, iterations, summary,
            )
    if failed:
        logger.info("Briefs with a technical error:")
        for r in failed:
            logger.info("  • %s", r["brief"].path.name)
    logger.info("─" * 70)


def _reap_zombie_brief(brief_path: str | None) -> None:
    """Takes out of `running` the brief whose run died with the machine.

    Without this the brief stays invisible forever: `find_pending_briefs`
    only looks at `pending`, so nobody touches it again and nobody notices.
    We leave it `failed` (not `pending`) because the half-finished run may
    have pushed a branch; reviving it is a human decision via /retry.
    """
    if not brief_path:
        return
    path = Path(brief_path)
    if not path.exists():
        return
    try:
        post = frontmatter.load(path)
        if post.metadata.get("status") != "running":
            return
        post.metadata["status"] = "failed"
        post.metadata["error"] = "orphaned: the run died halfway"
        post.metadata["failed_at"] = datetime.now().isoformat()
        path.write_text(frontmatter.dumps(post))
        logger.warning("Orphaned brief marked failed: %s", path.name)
    except Exception as e:
        logger.warning("Could not mark the orphaned brief %s: %s", path, e)


def reap_stale_events(config: dict) -> None:
    """Marks as failed the events whose run died halfway.

    It does not re-enqueue them: a half-finished run may have pushed a
    branch or opened a PR, so retrying is a human decision (/retry).
    """
    try:
        from talos import store

        if not store.DB_PATH.exists():
            return
        longest = max(
            [config["claude_timeout_seconds"]]
            + list((config.get("timeouts") or {}).values())
        )
        with store.connect() as conn:
            orphans = store.reap_orphans(conn, int(longest) + 300)
        if orphans:
            for o in orphans:
                _reap_zombie_brief(o["brief_path"])
            logger.warning(
                "Reaped orphaned events (run died halfway): %s. "
                "Retry with /retry if appropriate.",
                ", ".join(str(o["id"]) for o in orphans),
            )
    except Exception as e:
        logger.warning("The orphan reaper failed: %s", e)


def run_reconciler(config: dict) -> None:
    """Asks GitHub for open issues the webhook never delivered.

    The webhook is the fragile link: tunnel down, systemd restarting, or
    GitHub retrying a few times and giving up. Any of the three leaves an
    issue with no brief and no error signal — just silence. This closes that
    gap, and along the way makes the tunnel non-critical for the GitHub half.

    It runs here and not in webhookd because it needs the dispatch lock: two
    simultaneous passes would admit the same issue twice.
    """
    interval = int(config.get("reconcile_interval_minutes", 30))
    if interval <= 0:
        return
    try:
        from talos import reconcile
        from talos import webhookd

        if not reconcile.due(interval):
            return
        # webhookd keeps the config in a global because its HTTP handler does
        # not get it as a parameter. The reconciler goes through the same
        # door, so it has to be populated with this pass's config.
        webhookd.CFG = config
        store_mod = __import__("store")
        store_mod.init_db()
        results = reconcile.reconcile(config)
        reconcile.touch_stamp()
        admitted = [r for r in results if r.get("state") == "admitted"]
        if admitted:
            logger.info(
                "Reconciler: %d issue(s) admitted that the webhook never delivered (%s)",
                len(admitted),
                ", ".join(f"#{r.get('number')}" for r in admitted),
            )
        elif results:
            logger.info("Reconciler: %d new issue(s), none admitted", len(results))
    except Exception as e:
        # A reconciler failure must not take down the pass: the briefs that
        # are already pending have to run anyway.
        logger.warning("The reconciler failed (continuing with the pass): %s", e)


def reap_running_briefs(config: dict) -> None:
    """Takes out of `running` the briefs whose run no longer exists.

    Called with the flock held, and every pass takes the flock before
    touching a brief: if this pass holds it, a `running` brief in ToDos/ is a
    run that died with the machine. The ledger reaper only sees briefs with
    an event; without this a manual brief stayed `running` forever. It ends
    up `failed` rather than `pending` for the same reason as the other one:
    the half-finished run may have pushed a branch; reviving it is /retry.
    """
    todos = Path(config["vault"]) / config["briefs_dir"]
    if not todos.exists():
        return
    for md in todos.rglob("*.md"):
        try:
            post = frontmatter.load(md)
        except Exception:
            continue
        if post.metadata.get("status") != "running":
            continue
        post.metadata["status"] = "failed"
        post.metadata["last_error"] = "orphaned: the run died halfway"
        post.metadata["failed_at"] = datetime.now().isoformat()
        md.write_text(frontmatter.dumps(post))
        logger.warning("Orphaned brief marked failed: %s", md.name)
        event_id = post.metadata.get("event_id")
        if event_id:
            try:
                from talos import store

                with store.connect() as conn:
                    store.set_state(conn, int(event_id), "failed", last_error="orphaned")
            except Exception as e:
                logger.warning("Could not mark the orphaned event %s: %s", event_id, e)


def run_once(config: dict, dry_run: bool = False) -> int:
    """
    Full pipeline: find pending, sort, process.
    Returns the number of briefs processed (0 if locked by another pass in
    progress).
    """
    if dry_run:
        briefs = sort_by_priority(
            find_pending_briefs(config["vault"], config["briefs_dir"])
        )
        logger.info("Found %d pending brief(s)", len(briefs))
        for b in briefs:
            process_brief(b, config, dry_run=True)
        return len(briefs)

    # Kill switch reachable from your phone: the bot creates the sentinel and
    # no more runs start. Webhooks keep admitting and writing briefs, which
    # wait in `pending` until /resume.
    if is_paused():
        logger.warning(
            "PAUSED sentinel active (%s) — not starting any run.",
            Path("~/.orchestrator/PAUSED").expanduser(),
        )
        return 0

    with _DispatchLock() as acquired:
        if not acquired:
            logger.warning("Another pass in progress (lock held), skip.")
            return 0

        # With the lock held there is no run in progress: a live `orq-*` is
        # from a dispatch that died halfway.
        worktree.kill_orphan_containers()
        reap_running_briefs(config)
        reap_stale_events(config)
        run_reconciler(config)

        results: list[dict] = []
        seen: set[Path] = set()
        processed = 0
        paused = False
        # Rescan when the batch ends: stage A enqueues review-fix round 1
        # (and the webhook keeps admitting) during the pass, and webhookd
        # cannot kick a pass that already holds the lock.
        while not paused:
            briefs = [
                b for b in sort_by_priority(
                    find_pending_briefs(config["vault"], config["briefs_dir"])
                )
                if b.path not in seen
            ]
            if not briefs:
                break
            logger.info("Found %d pending brief(s)", len(briefs))
            for b in briefs:
                seen.add(b.path)
                # Before EACH brief, not only at startup: each run lasts up to
                # two hours, and a /pause mid-pass has to stop the remaining
                # ones, not wait for the queue to drain.
                if is_paused():
                    logger.warning("PAUSED appeared mid-pass — not starting "
                                   "%s or the ones after it", b.path.name)
                    paused = True
                    break
                verdict = cap_verdict(config, b)
                if not verdict.admitted:
                    defer_brief(config, b, verdict)
                    continue
                res = process_brief(b, config, dry_run=False)
                processed += 1
                if res is not None:
                    results.append(res)
        log_pass_summary(results)
        return processed


def watch_loop(config: dict) -> None:
    interval = int(config.get("poll_interval_seconds", 300))
    logger.info("Watch mode started, polling every %ds", interval)
    try:
        while True:
            run_once(config, dry_run=False)
            time.sleep(interval)
    except KeyboardInterrupt:
        logger.info("Watch mode stopped by the user.")


# ── CLI ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Headless Claude Code orchestrator with Docker."
    )
    parser.add_argument("--watch", action="store_true",
                        help="Daemon mode, polling every N seconds.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what it would do without invoking Docker or moving files.")
    parser.add_argument("--once", action="store_true",
                        help="Single pass (default).")
    args = parser.parse_args()

    setup_logging()
    config = load_config()
    check_prereqs(config, dry_run=args.dry_run)

    if args.watch:
        watch_loop(config)
    else:
        run_once(config, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
