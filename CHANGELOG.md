# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/) once it reaches 1.0.

## [0.1.0] — 2026-09-25

First public release.

### Added

- Event harness: GitHub issues and Sentry errors arrive through `webhookd`
  (HMAC-verified) or the polling reconciler, get triaged, and become
  `issue-fix` briefs.
- `review-fix` rounds triggered by review comments on harness PRs; issues
  close when the fix is released to production.
- Hardened pipelines: every event-driven run gets its own git worktree and a
  container without host Claude/SSH credentials.
- BMAD Method integration: the harness installs BMAD skills plus headless
  overrides into target repositories.
- Manual briefs from an Obsidian vault or Telegram, with per-project agent
  memory.
- Telegram bot for status, logs, pause/resume, retries and reconciliation.
- Offline test suite (`tests/test_offline.py`).
