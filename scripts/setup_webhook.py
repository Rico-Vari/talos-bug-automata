#!/usr/bin/env python3
"""Register (or update) the GitHub webhook on a harness repo.

Idempotent: it looks the hook up by URL and PATCHes it instead of creating a
second one. Two hooks on the same endpoint deliver everything twice; dedupe
absorbs it, but the ledger becomes unreadable.

The secret comes from ~/.orchestrator/secrets.env, goes through stdin and is
never printed. No need to paste it by hand into the GitHub UI: this call
sends it.

Usage:
    python scripts/setup_webhook.py --repo your-org/sandbox \\
        --url https://your-host.your-tailnet.ts.net
    python scripts/setup_webhook.py --repo ... --list
    python scripts/setup_webhook.py --repo ... --url ... --delete
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import yaml  # noqa: E402

from talos import secrets_env  # noqa: E402

from talos.util import CONFIG_PATH  # noqa: E402

# The four lifecycle events: intake, fixer round (in both its forms) and
# close on merge. Fine-grained action filtering lives in triage.py, not here,
# because GitHub can't subscribe to an action, only to an event type.
EVENTS = [
    "issues",
    "pull_request",
    "pull_request_review",
    "pull_request_review_comment",
]


def gh_api(args: list[str], stdin: str | None = None) -> tuple[int, str, str]:
    r = subprocess.run(
        ["gh", "api", *args], input=stdin,
        capture_output=True, text=True, timeout=30,
    )
    return r.returncode, r.stdout, r.stderr


def find_hook(repo: str, endpoint: str) -> dict | None:
    rc, out, err = gh_api([f"repos/{repo}/hooks"])
    if rc != 0:
        raise SystemExit(f"Could not list the hooks of {repo}: {err.strip()[:300]}")
    for hook in json.loads(out or "[]"):
        if (hook.get("config") or {}).get("url") == endpoint:
            return hook
    return None


def cmd_list(repo: str) -> int:
    rc, out, err = gh_api([f"repos/{repo}/hooks"])
    if rc != 0:
        raise SystemExit(err.strip()[:300])
    hooks = json.loads(out or "[]")
    if not hooks:
        print("No hooks.")
    for h in hooks:
        conf = h.get("config") or {}
        print(f"#{h['id']}  {conf.get('url')}  active={h.get('active')}  "
              f"secret={'yes' if conf.get('secret') else 'NO'}")
        print(f"       events: {', '.join(h.get('events') or [])}")
        last = h.get("last_response") or {}
        if last.get("code"):
            print(f"       last delivery: {last.get('code')} {last.get('message') or ''}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Register the GitHub webhook.")
    ap.add_argument("--repo", required=True, help="owner/repo")
    ap.add_argument("--url", help="Public base URL of the tunnel, no path (https://…)")
    ap.add_argument("--list", action="store_true", help="Only list the hooks.")
    ap.add_argument("--delete", action="store_true", help="Delete the harness hook.")
    args = ap.parse_args()

    # systemd hands the secret to webhookd via EnvironmentFile; a manual run
    # doesn't have that, so read the file the same way it does.
    secrets_env.load_dotenv()
    cfg = yaml.safe_load(open(CONFIG_PATH)) or {}

    if args.list:
        return cmd_list(args.repo)

    if args.repo not in (cfg.get("repos") or {}):
        print(f"⚠  {args.repo} is not under repos: in config.yaml — the webhook "
              f"would deliver events the harness rejects as unmapped.\n")

    base = (args.url or "").rstrip("/")
    if not base:
        raise SystemExit("Missing --url (the public base URL of the tunnel).")
    endpoint = f"{base}/gh"

    if args.delete:
        hook = find_hook(args.repo, endpoint)
        if hook is None:
            print("No hook with that URL.")
            return 0
        rc, _, err = gh_api(["-X", "DELETE", f"repos/{args.repo}/hooks/{hook['id']}"])
        print("Hook deleted." if rc == 0 else f"Failed: {err.strip()[:200]}")
        return 0 if rc == 0 else 1

    try:
        secret = secrets_env.resolve_secret(cfg, "github_webhook_secret_env")
    except Exception as e:
        raise SystemExit(f"{e}\n\nGenerate one with:  openssl rand -hex 32")

    payload = {
        "name": "web",
        "active": True,
        "events": EVENTS,
        "config": {
            "url": endpoint,
            "content_type": "json",
            "secret": secret,
            "insecure_ssl": "0",
        },
    }

    existing = find_hook(args.repo, endpoint)
    if existing:
        # The PATCH rotates the secret too: if the one in secrets.env changed,
        # this is the command that realigns both sides.
        method, url, action = (
            "PATCH", f"repos/{args.repo}/hooks/{existing['id']}", "updated",
        )
    else:
        method, url, action = "POST", f"repos/{args.repo}/hooks", "created"

    # The payload goes through stdin, not -f: a secret on the command line
    # shows up in `ps` and in shell history.
    rc, out, err = gh_api(["-X", method, url, "--input", "-"], stdin=json.dumps(payload))
    if rc != 0:
        msg = err.strip()[:400]
        if "404" in msg or "Not Found" in msg:
            msg += ("\n\nA 404 here almost always means missing admin permission "
                    "on the repo, not a nonexistent repo.")
        raise SystemExit(f"Could not register the hook: {msg}")

    hook = json.loads(out or "{}")
    hook_id = hook.get("id") or (existing or {}).get("id")
    print(f"✔ Hook {action} on {args.repo} (#{hook_id})")
    print(f"  endpoint: {endpoint}")
    print(f"  events:   {', '.join(EVENTS)}")
    print("\nTest delivery with a ping (it triggers no pipeline):")
    print(f"  gh api -X POST repos/{args.repo}/hooks/{hook_id}/pings")
    print("  journalctl --user -u talos-webhookd -f")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
