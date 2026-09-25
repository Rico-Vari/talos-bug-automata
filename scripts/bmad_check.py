#!/usr/bin/env python3
"""
Does every harness repo have the headless BMAD kit in its workspace?

dispatch already installs it on its own before each run; this is for checking
(or installing) it before switching on a new repo, without waiting for an
issue to come in. BMAD is fetched remotely with the official installer
(bmad-method@BMAD_VERSION); only the headless overrides ship from this repo.

  python scripts/bmad_check.py              # status of each repo in `repos:`
  python scripts/bmad_check.py --install    # install whatever is missing (never overwrites)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from talos import bmad_kit  # noqa: E402
from talos import worktree  # noqa: E402
from talos.util import CONFIG_PATH  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--install", action="store_true", help="install whatever is missing")
    ap.add_argument("--config", default=str(CONFIG_PATH))
    args = ap.parse_args()

    config = yaml.safe_load(Path(args.config).read_text()) or {}
    projects = config.get("projects") or {}
    seen: set[str] = set()
    bad = 0
    for gh_repo, rcfg in (config.get("repos") or {}).items():
        project = str(rcfg.get("project") or "")
        project_path = projects.get(project)
        state = "on " if rcfg.get("enabled") else "off"
        label = f"{gh_repo} [{state} {rcfg.get('mode', 'dry_run')}] → {project}"
        if not project_path or not Path(project_path).is_dir():
            print(f"✗ {label}: project has no folder in `projects:` ({project_path or 'no path'})")
            bad += 1
            continue
        # A project that is the repo itself keeps BMAD in a harness folder,
        # not in the checkout (see worktree.py).
        workspace = str(worktree.workspace_dir(project, project_path, str(rcfg.get("subrepo") or "").strip()))
        if workspace in seen:
            print(f"  {label}: same workspace as above")
            continue
        seen.add(workspace)
        missing, conflicts = bmad_kit.check(workspace)
        if args.install and missing and not conflicts:
            added, error = bmad_kit.install(workspace)
            if error:
                print(f"✗ {label}: {error}")
            elif added:
                print(f"+ {label}: added {', '.join(added)}")
            missing, conflicts = bmad_kit.check(workspace)
        if not missing and not conflicts:
            print(f"✓ {label}: ready ({workspace})")
            continue
        bad += 1
        for rel in missing:
            what = "out of date vs bmad-kit/" if (Path(workspace) / rel).exists() else "missing"
            print(f"✗ {label}: {what} {rel}")
        for why in conflicts:
            print(f"✗ {label}: {why}")
    if bad and not args.install:
        print("\nRun with --install to add what is missing. It only resyncs the headless contract "
              "and completion files; a project's .toml files are never overwritten.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
