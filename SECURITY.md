# Security Policy

Talos runs AI coding agents with `--dangerously-skip-permissions` inside Docker
containers, on prompts that can originate from untrusted sources (public GitHub
issues, Sentry events). Its security model is described in
[docs/security-model.md](docs/security-model.md). Please read it before running
the event harness against a repository that accepts issues from strangers.

## Supported versions

Talos is alpha software. Only the latest commit on `main` receives fixes.

## Reporting a vulnerability

**Do not open a public issue for security problems.**

Report privately through GitHub's
[private vulnerability reporting](https://github.com/Rico-Vari/talos-bug-automata/security/advisories/new)
("Security" tab → "Report a vulnerability"). Include:

- what an attacker controls (issue body, PR comment, Sentry payload, webhook request…),
- what they can reach (host files, credentials, other repos, the Telegram bot…),
- a minimal reproduction if you have one.

You can expect an acknowledgement within 7 days. Fixes are released on `main`
and credited in the advisory unless you ask otherwise.

## In scope

- Prompt injection that escapes the hardened container or reaches host credentials
- Webhook signature bypass or replay in `webhookd`
- Admission-control bypass (an untrusted author getting a pipeline to run)
- Secrets leaking into logs, briefs, PR bodies or Telegram messages

## Out of scope

- Behavior of Claude Code, BMAD Method or other upstream tools themselves —
  report those upstream.
- Running manual briefs with host credentials mounted: that mode trusts the
  brief author by design.
