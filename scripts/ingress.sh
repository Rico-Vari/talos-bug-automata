#!/usr/bin/env bash
# Expose the harness to the internet and register the GitHub webhook.
#
# Run it by hand, not from systemd, because the two remaining steps are your
# call: `tailscale funnel` needs root, and enabling Funnel on the tailnet takes
# two clicks in the admin console that no script can make for you.
#
# Idempotent: rerun it as many times as you like.
set -euo pipefail

PORT="${PORT:-8787}"
REPOS="${REPOS:-your-org/sandbox}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
die() { printf '\n\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

say "1. webhookd listening on 127.0.0.1:$PORT"
code=$(curl -s -m 3 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/healthz" || true)
[ "$code" = "200" ] || die "healthz returned '$code'. Start the service:
  systemctl --user start talos-webhookd.service
  journalctl --user -u talos-webhookd -n 30"
echo "   ✔ healthz 200"

say "2. Tailscale Funnel"
command -v tailscale >/dev/null || die "tailscale is not installed."
DNS_NAME=$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')
echo "   node: $DNS_NAME"

# `funnel --bg` stores the config in tailscaled, so it survives reboots and
# needs no systemd unit of its own. It fails if the tailnet doesn't have HTTPS
# or the `funnel` attribute enabled: both live in the admin console.
if ! sudo tailscale funnel --bg "$PORT"; then
  die "Funnel did not start. The two likely causes, in order:

  a) HTTPS disabled on the tailnet:
     https://login.tailscale.com/admin/dns → 'Enable HTTPS…'

  b) The node lacks the 'funnel' attribute in the ACL:
     https://login.tailscale.com/admin/acls → add

       \"nodeAttrs\": [{\"target\": [\"$DNS_NAME\"], \"attr\": [\"funnel\"]}]

  Then rerun this script."
fi

PUBLIC="https://$DNS_NAME"
say "3. Checking from outside: $PUBLIC/healthz"
# The first request can be slow: Funnel provisions the certificate on demand.
for i in $(seq 1 12); do
  code=$(curl -s -m 10 -o /dev/null -w '%{http_code}' "$PUBLIC/healthz" || true)
  [ "$code" = "200" ] && break
  echo "   attempt $i: '$code' — waiting for the certificate…"
  sleep 5
done
[ "$code" = "200" ] || die "The public endpoint returns '$code'. Check:
  tailscale funnel status
  journalctl --user -u talos-webhookd -n 30"
echo "   ✔ $PUBLIC/healthz returns 200"

say "4. Registering the webhook on GitHub"
for repo in $REPOS; do
  python3 "$HERE/scripts/setup_webhook.py" --repo "$repo" --url "$PUBLIC"
done

say "5. Save the public host in config.yaml"
cat <<EOF
   webhook:
     public_host: $DNS_NAME

And for Sentry (Settings → Developer Settings → New Internal Integration):
   Webhook URL:  $PUBLIC/sentry
   Alert Rule Action: ON
   Permissions: Issue & Event: Read, Project: Read — 'issue' resource enabled

Copy the Client Secret and the Auth Token into ~/.orchestrator/secrets.env and:
   systemctl --user restart talos-webhookd.service
EOF
