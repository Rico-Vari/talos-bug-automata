#!/usr/bin/env python3
"""
Replay an archived payload against the local webhookd.

This is the harness's primary test loop: it signs the payload with the local
secret and posts it to 127.0.0.1, with no tunnel, no GitHub and no Sentry.
Every triage bug reproduces deterministically and for free.

  python scripts/replay.py ~/.orchestrator/events/raw/<id>.json
  python scripts/replay.py payload.json --source sentry
  python scripts/replay.py payload.json --bad-signature   # must return 401
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import yaml  # noqa: E402

from talos import secrets_env as secrets_mod  # noqa: E402

from talos.util import CONFIG_PATH  # noqa: E402


def detect_source(payload: dict) -> str:
    """Guess the source from the payload's shape, so you don't have to pass it.

    Sentry first: its payloads also carry `installation` (the Internal
    Integration's), so checking that first classified all of Sentry as
    GitHub. What sets Sentry apart is `data` as an object; GitHub never
    sends it at the root.
    """
    if isinstance(payload.get("data"), dict):
        return "sentry"
    return "github"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("payload", help="JSON file with the raw payload")
    ap.add_argument("--source", choices=["github", "sentry"], help="auto-detected by default")
    ap.add_argument("--event", default="", help="X-GitHub-Event (github) or Sentry-Hook-Resource")
    ap.add_argument("--action", default="", help="overrides payload['action']")
    ap.add_argument("--delivery", default="", help="delivery id; a new one by default")
    ap.add_argument("--bad-signature", action="store_true", help="deliberately invalid signature")
    ap.add_argument("--host", default="", help="defaults to webhook.bind/port from config.yaml")
    args = ap.parse_args()

    cfg = yaml.safe_load(open(CONFIG_PATH)) or {}
    secrets_mod.load_dotenv()

    body = Path(args.payload).read_bytes()
    payload = json.loads(body)
    if args.action:
        payload["action"] = args.action
        body = json.dumps(payload).encode()

    source = args.source or detect_source(payload)

    if source == "github":
        secret = secrets_mod.resolve_secret(cfg, "github_webhook_secret_env")
        sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        path = "/gh"
        headers = {
            "Content-Type": "application/json",
            "X-GitHub-Event": args.event or ("issues" if "issue" in payload else "ping"),
            "X-GitHub-Delivery": args.delivery or str(uuid.uuid4()),
            "X-Hub-Signature-256": "sha256=" + "0" * 64 if args.bad_signature else sig,
        }
    else:
        secret = secrets_mod.resolve_secret(cfg, "sentry_client_secret_env")
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        path = "/sentry"
        headers = {
            "Content-Type": "application/json",
            "Sentry-Hook-Resource": args.event or "event_alert",
            # Epoch seconds, the way Sentry sends it. Sending ISO hid the fact
            # that webhookd could not parse the real format.
            "Sentry-Hook-Timestamp": str(int(time.time())),
            "Sentry-Hook-Signature": "0" * 64 if args.bad_signature else sig,
        }

    wh = cfg.get("webhook") or {}
    base = args.host or f"http://{wh.get('bind', '127.0.0.1')}:{wh.get('port', 8787)}"
    url = base.rstrip("/") + path

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"{resp.status} {resp.reason}")
            print(resp.read().decode()[:2000])
            return 0
    except urllib.error.HTTPError as e:
        print(f"{e.code} {e.reason}")
        print(e.read().decode()[:2000])
        return 0 if args.bad_signature and e.code == 401 else 1
    except urllib.error.URLError as e:
        print(f"Could not connect to {url}: {e.reason}")
        print("Is webhookd running? systemctl --user status talos-webhookd")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
