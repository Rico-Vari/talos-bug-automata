#!/usr/bin/env python3
"""
Identity mapping: owner/repo and Sentry project → local target.

The case that drives the design is the umbrella directory: `projects.acme`
points to "Acme Main", which is NOT a git repo but the directory that holds
several sibling repos. The container's workspace must stay "Acme Main"
(that is what makes `_bmad/` and the project skills resolve) and the
sub-repo is passed separately so the agent can `cd` into it.

Sentry resolves *through* `repos`, not in parallel: a single mapping table,
not two that drift apart.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class RoutingError(ValueError):
    pass


@dataclass
class Target:
    """The resolved target of an event."""
    gh_repo: str              # "acme-org/web-app"
    project: str              # key in config.projects
    project_path: Path        # the container's workspace (/workspace)
    subrepo: str              # subdirectory with the .git, "" if the project is the repo
    service_path: str         # the service's subdir inside the subrepo, "" if not applicable
    base_branch: str
    cfg: dict                 # the full repos[gh_repo] block

    @property
    def repo_path(self) -> Path:
        """Absolute path to the git repo. project_path if there is no subrepo."""
        return self.project_path / self.subrepo if self.subrepo else self.project_path

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", False))

    @property
    def dry_run(self) -> bool:
        return str(self.cfg.get("mode", "dry_run")).lower() != "live"


def validate_routing(cfg: dict, warnings: list[str] | None = None) -> list[str]:
    """Checks the repos/sentry block against projects. Returns the problems.

    It runs at startup: a misspelled config has to fail there and not six
    hours later when the first webhook arrives.

    A repo with `enabled: false` does not block: its problem goes to
    `warnings`. If it blocked, a misspelled path in a disabled repo would
    bring down the whole bot, and `/pause` with it — the kill switch would
    be out of reach precisely because of something that is not running.
    """
    problems: list[str] = []
    projects = cfg.get("projects") or {}
    repos = cfg.get("repos") or {}

    for gh_repo, rc in repos.items():
        sink = problems if (rc or {}).get("enabled") else (
            warnings if warnings is not None else []
        )
        if "/" not in gh_repo:
            sink.append(f"repos: '{gh_repo}' is not in owner/repo form")
        project = (rc or {}).get("project")
        if not project:
            sink.append(f"repos['{gh_repo}'] does not define project")
            continue
        if project not in projects:
            sink.append(
                f"repos['{gh_repo}'].project = '{project}' does not exist in projects:"
            )
            continue
        base = Path(projects[project])
        subrepo = (rc or {}).get("subrepo", "")
        repo_path = base / subrepo if subrepo else base
        if not repo_path.exists():
            sink.append(f"repos['{gh_repo}']: path {repo_path} does not exist")
        elif not (repo_path / ".git").exists():
            sink.append(f"repos['{gh_repo}']: {repo_path} is not a git repo")
        if not (rc or {}).get("base_branch"):
            sink.append(f"repos['{gh_repo}'] does not define base_branch")

    for slug, sc in ((cfg.get("sentry") or {}).get("projects") or {}).items():
        repo = (sc or {}).get("repo")
        if not repo:
            problems.append(f"sentry.projects['{slug}'] does not define repo")
        elif repo not in repos:
            problems.append(
                f"sentry.projects['{slug}'].repo = '{repo}' is not in repos:"
            )

    return problems


def resolve_gh_repo(cfg: dict, gh_repo: str) -> Target | None:
    """owner/repo → Target. None if the repo is not mapped."""
    rc = (cfg.get("repos") or {}).get(gh_repo)
    if not rc:
        return None
    project = rc.get("project", "")
    project_path = (cfg.get("projects") or {}).get(project)
    if not project_path:
        # A disabled repo with a broken config must not bring down the caller
        # (the reconciler walks every repo): for it there is no target.
        if not rc.get("enabled"):
            return None
        raise RoutingError(
            f"repos['{gh_repo}'].project = '{project}' does not exist in projects:"
        )
    return Target(
        gh_repo=gh_repo,
        project=project,
        project_path=Path(project_path),
        subrepo=rc.get("subrepo", "") or "",
        service_path=rc.get("service_path", "") or "",
        base_branch=rc.get("base_branch", "main"),
        cfg=rc,
    )


def resolve_sentry_project(
    cfg: dict, slug: str, project_id: str | int | None = None
) -> tuple[Target, dict] | None:
    """Sentry project slug (or numeric id) → (Target, config block).

    The Sentry block's service_path overrides the repo's: in a services
    monorepo like acme-org/api, Sentry knows which service it is, not the
    repo.

    The id exists because the `event_alert` payload carries the project as a
    number and not as a slug. `sentry.projects[*].project_id` maps it.
    """
    projects = (cfg.get("sentry") or {}).get("projects") or {}
    sc = projects.get(slug) if slug else None
    if not sc and project_id not in (None, ""):
        sc = next(
            (c for c in projects.values()
             if str((c or {}).get("project_id", "")) == str(project_id)),
            None,
        )
    if not sc:
        return None
    target = resolve_gh_repo(cfg, sc.get("repo", ""))
    if target is None:
        return None
    if sc.get("service_path"):
        target.service_path = sc["service_path"]
    return target, sc


def resolve_service(sentry_cfg: dict, tags: dict | None) -> tuple[str, str]:
    """Which monorepo service produced the event. Returns (path, problem).

    `acme-org/api` is a monorepo of six services that all report to the SAME
    Sentry project, so the project slug does not say which one failed. The
    `service` tag does, and every service already emits it —
    `sentry_sdk.set_tag("service", "<name>")`, Celery workers included. This
    map translates that tag to the subdirectory.

    When the project declares `service_map` and the event has no tag, the
    problem is returned instead of guessing: sending the agent to patch the
    wrong service burns an hour of container time and dirties a repo that
    did not have the bug.
    """
    smap = sentry_cfg.get("service_map") or {}
    if not smap:
        return sentry_cfg.get("service_path", "") or "", ""
    key = ((tags or {}).get("service") or "").strip()
    if not key:
        return "", ("the event has no 'service' tag and the project covers "
                    f"{len(smap)} services")
    if key not in smap:
        return "", (f"the tag service='{key}' is not in service_map "
                    f"(known: {', '.join(sorted(smap))})")
    return smap[key], ""


if __name__ == "__main__":
    import argparse
    import sys

    import yaml
    from talos.util import CONFIG_PATH

    ap = argparse.ArgumentParser(description="Harness routing resolver")
    ap.add_argument("--config", default=str(CONFIG_PATH))
    ap.add_argument("--resolve", metavar="OWNER/REPO")
    ap.add_argument("--sentry", metavar="PROJECT_SLUG")
    ap.add_argument("--validate", action="store_true")
    args = ap.parse_args()

    cfg = yaml.safe_load(open(args.config)) or {}
    cfg["projects"] = {
        k: str(Path(v).expanduser()) for k, v in (cfg.get("projects") or {}).items()
    }

    if args.validate or not (args.resolve or args.sentry):
        warnings: list[str] = []
        problems = validate_routing(cfg, warnings)
        for w in warnings:
            print(f"  (warning, disabled repo) {w}")
        if problems:
            print("Routing problems:")
            for p in problems:
                print(f"  - {p}")
            sys.exit(1)
        print(f"Routing OK: {len(cfg.get('repos') or {})} repo(s) mapped")

    if args.resolve:
        t = resolve_gh_repo(cfg, args.resolve)
        if t is None:
            print(f"{args.resolve}: NOT MAPPED")
            sys.exit(1)
        print(f"gh_repo      {t.gh_repo}")
        print(f"project      {t.project}")
        print(f"project_path {t.project_path}   (→ /workspace)")
        print(f"subrepo      {t.subrepo or '(none)'}")
        print(f"repo_path    {t.repo_path}")
        print(f"base_branch  {t.base_branch}")
        print(f"enabled      {t.enabled}   dry_run: {t.dry_run}")

    if args.sentry:
        r = resolve_sentry_project(cfg, args.sentry)
        if r is None:
            print(f"{args.sentry}: NOT MAPPED")
            sys.exit(1)
        t, sc = r
        print(f"sentry slug  {args.sentry}  →  {t.gh_repo}")
        print(f"repo_path    {t.repo_path}")
        print(f"service_path {t.service_path or '(none)'}")
        print(f"filters      envs={sc.get('environments')} min_level={sc.get('min_level')} "
              "(the times-seen threshold is set by the Sentry alert rule)")
