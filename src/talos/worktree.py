#!/usr/bin/env python3
"""
The agent never works in a repo's main checkout.

Every hardened pipeline run gets its own git worktree on a detached HEAD
at `origin/<base_branch>`, and the container sees it at
`/workspace/$SUBREPO`. The main checkout, where a person works or where the
harness services run from, keeps its branch, its working tree and its
untracked files. The only things it shares with the run are the object
store and the refs: the agent's branch stays in the repo when the worktree
is deleted, and a commit on a detached HEAD is saved to `refs/orq/<run>`,
so a missed push can be recovered.

The host does no fetch and no checkout: the worktree is created with
`--no-checkout` and the container does the `reset --hard` and the fetch.
That way nothing the run could have written to the repo's config (filters,
`insteadOf`, `uploadpack`, credential helpers) runs on the host. What the
host does run (`rev-parse`, `worktree add --no-checkout`, `worktree list`,
`update-ref`) goes through a config key allowlist: see `unsafe_git_config`.

A worktree's `.git` points to the repo's git dir with an absolute host
path, so the container mounts that git dir at the same path.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("orchestrator")

WORKTREES_DIR = Path("~/.orchestrator/worktrees").expanduser()
# /workspace for projects that are the repo itself (no sub-repo): BMAD lives
# there, outside the main checkout.
WORKSPACES_DIR = Path("~/.orchestrator/workspaces").expanduser()
ADD_TIMEOUT = 300
CONTAINER_PREFIX = "orq-"


class WorktreeError(RuntimeError):
    """The run has no worktree. The message is the reason, verbatim."""


def host_git(repo: Path, *args: str, timeout: int = 10) -> subprocess.CompletedProcess:
    """Run git on a host repo without running anything the agent wrote.

    The container can write the repo's git dir (bind mount), so a hook or an
    fsmonitor planted there would run on the host, with dispatch's
    environment, the next time dispatch runs git. Both are switched off on the
    command line, which outranks every config file and is passed down to
    child git processes. `protocol.allow=never` and GIT_NO_LAZY_FETCH make
    any network access fail: the host never fetches, so a planted
    `uploadpack` or `insteadOf` has nothing to run. The environment carries
    no secrets.
    """
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "LC_ALL") if k in os.environ}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_NO_LAZY_FETCH"] = "1"
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
         "-c", "protocol.allow=never", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=timeout, env=env,
    )


# Repo-level config keys the host tolerates. Anything else in the repo's own
# config refuses the run: an allowlist, because the list of keys that make git
# run a command (filters, `insteadOf` + `protocol.ext`, `uploadpack`,
# credential helpers, `core.askPass`, promisor remotes, includes…) keeps
# growing. None of these runs anything or points git outside the repo.
SAFE_GIT_CONFIG_RE = re.compile(
    r"core\.(repositoryformatversion|filemode|bare|logallrefupdates|ignorecase"
    r"|precomposeunicode|symlinks|autocrlf|eol|safecrlf)"
    r"|remote\..+\.(url|pushurl|fetch|push|tagopt|prune|gh-resolved)"
    r"|branch\..+\.[a-z0-9-]+"
    r"|user\.(name|email|signingkey)"
    r"|extensions\.(worktreeconfig|objectformat)"
    r"|lfs\.repositoryformatversion"
    r"|pull\.(rebase|ff)|push\.(default|autosetupremote)|init\.defaultbranch"
    r"|commit\.gpgsign|gpg\.format|rerere\.enabled|gc\.auto"
)
# Keys that run a command, allowed only with the exact value their tool
# writes: `git lfs install --local`. The host never checks out, but a person
# does in the main checkout, so a changed value is still refused.
PINNED_GIT_CONFIG = {
    "filter.lfs.clean": "git-lfs clean -- %f",
    "filter.lfs.smudge": "git-lfs smudge -- %f",
    "filter.lfs.process": "git-lfs filter-process",
    "filter.lfs.required": "true",
}


def unsafe_git_config(repo: Path) -> str | None:
    """Why dispatch must not touch `repo`, or None when it is safe.

    Only the repo's own config (local and worktree scope) counts: that is the
    part the container can write. The user's global config is trusted.
    """
    try:
        r = host_git(repo, "config", "--list", "--show-scope", "-z")
    except Exception as e:
        return f"could not read its config: {e}"
    if r.returncode != 0:
        return f"could not read its config: {r.stderr.strip()}"
    fields = r.stdout.split("\0")
    for scope, entry in zip(fields[::2], fields[1::2]):
        if scope not in ("local", "worktree"):
            continue
        key, _, value = entry.partition("\n")
        key = key.lower()
        if key in PINNED_GIT_CONFIG:
            if value != PINNED_GIT_CONFIG[key]:
                return f"its config sets {key} = {value!r}, which is not the git-lfs value"
        elif not SAFE_GIT_CONFIG_RE.fullmatch(key):
            return f"its config sets {key}, which is not in the worktree.py allowlist"
    return None


def valid_branch(name: str) -> bool:
    """Is `name` a branch name git accepts, without looking like an option?"""
    if not name or name.startswith("-") or "@{" in name:
        return False
    try:
        # cwd="/": outside any repo, without reading anyone's config.
        return subprocess.run(["git", "check-ref-format", "--branch", name],
                              capture_output=True, timeout=10, cwd="/").returncode == 0
    except Exception:
        return False


def workspace_dir(project: str, project_path: str | Path, subrepo: str) -> Path:
    """The folder mounted at /workspace: BMAD and the run's dotfiles.

    A project with sub-repos has it in its own folder, as always. A project
    that is the repo itself uses one owned by the harness, so BMAD never
    lands in the main checkout.
    """
    return Path(project_path) if subrepo else WORKSPACES_DIR / project


def container_subrepo(project_path: str | Path, subrepo: str) -> str:
    """The repo's name inside /workspace ($SUBREPO in the container)."""
    return subrepo or Path(project_path).resolve().name


@dataclass
class Worktree:
    path: Path     # on the host; in the container it is /workspace/$SUBREPO
    git_dir: Path  # the repo's common git dir; mounted at the same path
    start: str = ""  # commit it started from; a detached HEAD on another commit gets rescued


def run_id(timestamp: str, brief_slug: str) -> str:
    """An id fit for a worktree, container and ref name."""
    return re.sub(r"[^A-Za-z0-9_.-]", "-", f"{timestamp}-{brief_slug}").strip(".")


def _ours(path: Path) -> Path | None:
    """`path` resolved if it is a direct child folder of WORKTREES_DIR, else None.

    The paths come from files the container can write
    (`git_dir/worktrees/*/gitdir`): a `worktrees/..` or a symlink has to land
    outside, not in `~/.orchestrator`.
    """
    try:
        p = path.resolve()
    except (OSError, RuntimeError):
        return None
    return p if p.parent == WORKTREES_DIR.resolve() and p.name not in ("", ".", "..") else None


def _harness_worktrees(git_dir: Path) -> list[tuple[Path, Path]]:
    """(metadata, folder) of every worktree of this repo that lives in WORKTREES_DIR."""
    base = git_dir / "worktrees"
    found = []
    try:
        metas = sorted(base.iterdir()) if base.is_dir() else []
    except OSError:
        return found
    for meta in metas:
        try:
            gitdir = Path((meta / "gitdir").read_text().strip())
        except OSError:
            continue
        if not gitdir.is_absolute():
            gitdir = meta / gitdir
        path = _ours(gitdir.parent)
        if path is not None:
            found.append((meta, path))
    return found


def branch_holder(repo: Path, branch: str) -> str | None:
    """Path of the non-harness worktree that has `branch` checked out, or None.

    With the branch checked out in another worktree, `git checkout -B` resets
    it without an error (git 2.43) under whoever works there, and
    `gh pr checkout` fails.
    """
    r = host_git(repo, "worktree", "list", "--porcelain")
    if r.returncode != 0:
        return f"(could not list the worktrees: {r.stderr.strip()})"
    path = ""
    for line in r.stdout.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line == f"branch refs/heads/{branch}" and _ours(Path(path)) is None:
            return path
    return None


def create(repo: Path, base_branch: str, rid: str, branch: str = "") -> tuple[Worktree | None, str]:
    """A new worktree, without checkout, on a detached HEAD at `origin/<base_branch>`.

    Returns (worktree, "") or (None, reason). `branch` is the branch the run
    will use: if it is checked out somewhere else, nothing is created.
    The pipeline does its own fetch inside and creates the branch from
    `origin/$BASE_BRANCH`: the local `origin/<base_branch>` is only the
    worktree's starting point.
    """
    if not (repo / ".git").exists():
        return None, f"worktree: {repo} is not a git repo"
    if not valid_branch(base_branch):
        return None, f"worktree: invalid base_branch: {base_branch!r}"
    try:
        return _create(repo, base_branch, rid, branch)
    except (subprocess.TimeoutExpired, OSError) as e:
        return None, f"worktree: git did not respond in {repo}: {e}"


def _create(repo: Path, base_branch: str, rid: str, branch: str) -> tuple[Worktree | None, str]:
    unsafe = unsafe_git_config(repo)
    if unsafe:
        return None, f"worktree: not creating one from {repo}: {unsafe}. Check its .git/config by hand"
    r = host_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if r.returncode != 0:
        return None, f"worktree: could not read the git dir of {repo}: {r.stderr.strip()}"
    git_dir = Path(r.stdout.strip()).resolve()

    r = host_git(repo, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{base_branch}^{{commit}}")
    if r.returncode != 0:
        return None, (f"worktree: {repo} has no origin/{base_branch} (wrong base_branch in the config, "
                      f"or the repo never fetched that branch?)")
    start = r.stdout.strip()

    sweep_stale(repo, git_dir)
    if branch:
        holder = branch_holder(repo, branch)
        if holder:
            return None, (f"worktree: branch {branch} is checked out in {holder}. The run would "
                          f"reset it under you: switch branches there and retry")

    path = WORKTREES_DIR / rid
    if path.exists():
        return None, f"worktree: {path} already exists"
    WORKTREES_DIR.mkdir(parents=True, exist_ok=True)
    wt = Worktree(path, git_dir, start)
    # --no-checkout: the checkout (filters, LFS, submodule.recurse) is done by
    # the container with `git reset --hard`. useRelativePaths=false: the
    # container's git does not understand extensions.relativeWorktrees.
    try:
        add = host_git(repo, "-c", "worktree.useRelativePaths=false", "worktree", "add", "--quiet",
                       "--no-checkout", "--detach", str(path), start, timeout=ADD_TIMEOUT)
    except subprocess.TimeoutExpired:
        remove(repo, wt)
        return None, f"worktree: git worktree add took more than {ADD_TIMEOUT}s"
    if add.returncode != 0:
        remove(repo, wt)
        return None, f"worktree: git worktree add failed: {add.stderr.strip()[:300]}"
    logger.info("Worktree %s from origin/%s (%s) of %s", path, base_branch, start[:12], repo)
    return wt, ""


def sweep_stale(repo: Path, git_dir: Path) -> list[Path]:
    """Deletes the harness worktrees left over from earlier runs.

    dispatch runs one run at a time (the pass lock), so a worktree of this
    repo still in WORKTREES_DIR when another one is created belongs to a run
    that died before reaching its finally: a machine reboot or dispatch
    killed halfway. If it stayed, the brief's branch would still be held by
    it and the retry could not check it out. Its container, if still alive,
    works with nobody reading it: it gets killed too. It reads the metadata
    directly, so a `locked` worktree (a `worktree add` that died halfway) is
    swept as well.
    """
    stale = [path for _, path in _harness_worktrees(git_dir)]
    for path in stale:
        logger.warning("Orphan worktree from an earlier run: %s — deleting it", path)
        kill_container(CONTAINER_PREFIX + path.name)
        remove(repo, Worktree(path, git_dir))
    return stale


def _force(func, path, _exc) -> None:
    """rmtree over whatever the agent left read-only (module caches, etc.)."""
    try:
        os.chmod(os.path.dirname(path), 0o700)
        if os.path.isdir(path) and not os.path.islink(path):
            os.chmod(path, 0o700)
        func(path)
    except OSError:
        pass


_RMTREE_ERR = {"onexc": _force} if sys.version_info >= (3, 12) else {"onerror": _force}


def _rescue(repo: Path, meta: Path, name: str, start: str) -> None:
    """Commits on a detached HEAD (the agent failed before creating its branch) → refs/orq/<run>."""
    try:
        head = (meta / "HEAD").read_text().strip()
    except OSError:
        return
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head) or head == start:
        return
    try:
        # Already reachable from a branch or a ref: nothing to rescue.
        if host_git(repo, "for-each-ref", "--count=1", "--contains", head).stdout.strip():
            return
        r = host_git(repo, "update-ref", f"refs/orq/{name}", head)
    except Exception as e:
        logger.warning("Could not save the HEAD of %s to refs/orq/%s: %s", meta, name, e)
        return
    if r.returncode == 0:
        logger.warning("Worktree %s was left on a detached HEAD at %s: saving it to refs/orq/%s",
                       name, head[:12], name)


def remove(repo: Path, wt: Worktree) -> None:
    """Deletes the worktree and its metadata. The branch the agent left stays in the repo.

    No `worktree remove` and no `worktree prune`: both read what the run
    could have written, and `prune` is repo-wide (it also deletes the record
    of a worktree of yours on an unmounted disk). Only this worktree's folder
    and metadata are deleted, and the metadata is located through its
    `gitdir`, validated against WORKTREES_DIR.
    """
    path = _ours(wt.path)
    if path is None:
        logger.error("Not deleting %s: it is not in %s", wt.path, WORKTREES_DIR)
        return
    metas = [meta for meta, p in _harness_worktrees(wt.git_dir) if p == path]
    for meta in metas:
        _rescue(repo, meta, path.name, wt.start)
    if path.exists():
        shutil.rmtree(path, **_RMTREE_ERR)
    if path.exists():
        logger.error("Could not fully delete %s — delete it by hand", path)
    for meta in metas:
        try:
            if meta.is_symlink():
                meta.unlink()
            else:
                shutil.rmtree(meta, **_RMTREE_ERR)
        except OSError as e:
            logger.warning("Could not delete the metadata %s: %s", meta, e)


def _docker(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def kill_container(name: str) -> None:
    """`docker rm -f`, without raising: a missing container or a slow docker does not break the cleanup."""
    try:
        _docker("rm", "-f", name)
    except Exception as e:
        logger.warning("docker rm -f %s failed: %s", name, e)


def kill_orphan_containers() -> list[str]:
    """Kills the `orq-*` containers still alive. Only with no run in progress.

    dispatch calls it when a pass starts, with the lock held: any live
    `orq-*` belongs to a run whose dispatch died, and it keeps writing (and
    pushing) with nobody reading its output.
    """
    try:
        r = _docker("ps", "-q", "--filter", f"name=^{CONTAINER_PREFIX}")
    except Exception as e:
        logger.warning("docker ps failed: %s", e)
        return []
    ids = r.stdout.split() if r.returncode == 0 else []
    for cid in ids:
        logger.warning("Orphan container %s from an earlier run — killing it", cid)
        kill_container(cid)
    return ids
