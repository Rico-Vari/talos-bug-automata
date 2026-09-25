#!/usr/bin/env python3
"""
Headless BMAD in the workspace, before a harness pipeline runs.

`lead-issue-fix.md` and `lead-review-fix.md` invoke `bmad-quick-dev` and
`bmad-code-review`, which live in the project (`/workspace/.claude/skills`),
and the overrides that make them headless live in `/workspace/_bmad/custom/`.
A project without them does not fail visibly: the agent does not find the
skill, or finds it without the overrides and HALTs waiting for a human until
the timeout. That is why dispatch checks it before starting the container.

Two layers, with different owners:

1. **BMAD itself** — it does not live in this repo. If it is missing, it is
   installed remotely with the official installer, pinned to
   `BMAD_VERSION`. Pinned because since v6.11 `bmad-quick-dev` is a shim to
   `bmad-build` that HALTs when it finds `_bmad/custom/bmad-quick-dev.toml`
   (it asks for permission to rename it), and HEAD no longer ships it.
   Migrating to `bmad-build-auto` is a pipeline change, not an install one.
   A project that is the repo itself (no sub-repo) has it in a harness
   folder and not in its checkout: see `worktree.workspace_dir`.
2. **The headless overrides** — they live in `bmad-kit/`. The `.toml` files
   are copied only when missing: a project can have its own. The headless
   contract and closing step (`OWNED`) belong to the harness and nobody else
   edits them, so they are synced whenever they change here; otherwise a fix
   to the contract would never reach the workspaces that already had it.
"""
from __future__ import annotations

import glob
import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger("orchestrator")

BMAD_VERSION = "6.10.0"
INSTALL_TIMEOUT = 300
KIT_DIR = Path(__file__).resolve().parent / "bmad-kit"

# What the installer puts in place and both skills use.
BMAD_REQUIRED = [
    ".claude/skills/bmad-quick-dev/SKILL.md",
    ".claude/skills/bmad-code-review/SKILL.md",
    "_bmad/scripts/resolve_customization.py",
    "_bmad/bmm/config.yaml",
]
MANIFEST = "_bmad/_config/manifest.yaml"

# The overrides must carry the hook; if the project already had its own
# without it, the skills never learn about HEADLESS=1.
HEADLESS_HOOK = "headless-contract.md"
HOOKED = ("_bmad/custom/bmad-quick-dev.toml", "_bmad/custom/bmad-code-review.toml")

# Owned by the harness: synced with `bmad-kit/` on every ensure.
OWNED = ("_bmad/custom/headless-contract.md", "_bmad/custom/headless-complete.md")

# What gets installed must not end up in an agent commit when the
# workspace is also the repo.
EXCLUDE_LINES = ("/_bmad/", "/_bmad-output/", "/.claude/skills/bmad-*/")


def _overrides() -> list[str]:
    return sorted(p.relative_to(KIT_DIR).as_posix() for p in KIT_DIR.rglob("*") if p.is_file())


def installed_version(ws: Path) -> str | None:
    """Version from the installer's manifest; None if BMAD was copied by hand."""
    try:
        for line in (ws / MANIFEST).read_text().splitlines():
            key, _, value = line.strip().partition(":")
            if key == "version":
                return value.strip().strip("'\"")
    except OSError:
        pass
    return None


def check(workspace: str | Path) -> tuple[list[str], list[str]]:
    """Returns (missing or outdated, conflicts). Both empty = ready."""
    ws = Path(workspace)
    missing = [rel for rel in BMAD_REQUIRED + _overrides() if not (ws / rel).is_file()]
    stale = [rel for rel in OWNED if (ws / rel).is_file()
             and (ws / rel).read_bytes() != (KIT_DIR / rel).read_bytes()]
    missing += stale
    conflicts = []
    for rel in HOOKED:
        path = ws / rel
        if path.is_file() and HEADLESS_HOOK not in path.read_text(errors="replace"):
            conflicts.append(f"{rel} exists without the HEADLESS hook — merge it by hand "
                             f"from {KIT_DIR / rel} (yours is not overwritten)")
    version = installed_version(ws)
    if version and tuple(map(int, version.split(".")[:2])) >= (6, 11):
        conflicts.append(
            f"BMAD {version} installed: since 6.11 bmad-quick-dev is a shim that "
            f"HALTs with our overrides (the harness uses {BMAD_VERSION})")
    return missing, conflicts


def _npx() -> tuple[str, dict] | None:
    """npx and an environment where `node` resolves.

    systemd does not load nvm's PATH, so `which` alone is not enough.
    """
    found = shutil.which("npx")
    if not found:
        candidates = sorted(glob.glob(str(Path.home() / ".nvm/versions/node/*/bin/npx")))
        found = candidates[-1] if candidates else None
    if not found:
        return None
    env = dict(os.environ)
    env["PATH"] = str(Path(found).parent) + os.pathsep + env.get("PATH", "")
    return found, env


def install_bmad(ws: Path) -> str:
    """Runs the official installer. Returns "" or the reason it failed."""
    npx = _npx()
    if not npx:
        return "npx is not available (install Node.js)"
    cmd = [npx[0], "-y", f"bmad-method@{BMAD_VERSION}", "install",
           "--directory", str(ws), "--modules", "bmm", "--tools", "claude-code", "--yes"]
    logger.warning("BMAD is not in %s — installing bmad-method@%s", ws, BMAD_VERSION)
    try:
        result = subprocess.run(cmd, cwd=ws, env=npx[1], capture_output=True,
                                text=True, timeout=INSTALL_TIMEOUT)
    except subprocess.TimeoutExpired:
        return f"the BMAD installer took more than {INSTALL_TIMEOUT}s"
    except OSError as e:
        return f"could not run the BMAD installer: {e}"
    if result.returncode != 0:
        tail = (result.stderr or result.stdout).strip().splitlines()[-3:]
        return f"the BMAD installer exited with {result.returncode}: {' | '.join(tail)}"
    return ""


def install_overrides(ws: Path) -> list[str]:
    """Copies the missing overrides and resyncs the OWNED ones. Returns what was copied."""
    copied = []
    for rel in _overrides():
        dest = ws / rel
        if dest.exists() and (rel not in OWNED or dest.read_bytes() == (KIT_DIR / rel).read_bytes()):
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(KIT_DIR / rel, dest)
        copied.append(rel)
    return copied


def _exclude_from_git(ws: Path) -> None:
    info = ws / ".git" / "info"
    if not info.is_dir():
        return
    exclude = info / "exclude"
    current = exclude.read_text() if exclude.exists() else ""
    lines = [line for line in EXCLUDE_LINES if line not in current.splitlines()]
    if lines:
        prefix = "" if not current or current.endswith("\n") else "\n"
        exclude.write_text(current + prefix + "# orquestrator: BMAD headless\n" + "\n".join(lines) + "\n")


def install(workspace: str | Path) -> tuple[list[str], str]:
    """Installs whatever is missing. Returns (what was added, error or "")."""
    ws = Path(workspace)
    ws.mkdir(parents=True, exist_ok=True)
    added: list[str] = []
    if any(not (ws / rel).is_file() for rel in BMAD_REQUIRED):
        error = install_bmad(ws)
        if error:
            return added, error
        added.append(f"bmad-method@{BMAD_VERSION}")
    added += install_overrides(ws)
    if added:
        _exclude_from_git(ws)
    return added, ""


def ensure(workspace: str | Path) -> str:
    """Installs whatever is missing. Returns "" if it is ready, or the reason if not."""
    missing, conflicts = check(workspace)
    if missing and not conflicts:
        added, error = install(workspace)
        if error:
            return f"bmad: {error}"
        logger.warning("Headless BMAD in %s: added %s", workspace, ", ".join(added))
        missing, conflicts = check(workspace)
    if conflicts:
        return f"bmad: {'; '.join(conflicts)}"
    if missing:
        return f"bmad: missing from the workspace: {', '.join(missing)}"
    return ""
