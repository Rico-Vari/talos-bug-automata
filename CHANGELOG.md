# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/) once it reaches 1.0.

## [Unreleased]

### Added

- `run_window.finish_open_work` (default `true`): outside the run window,
  parked usage-limit resumes and review-fix rounds still run, so a PR cycle
  cut by the limit or by the window's end completes. Only new stage A work
  waits for the window.

- A run that hits the Claude usage limit is parked instead of failed: its
  brief goes back to `pending`, no run starts for `usage_limit_retry_minutes`
  (default 60), and the retry resumes the same Claude session in the same
  worktree. `/resume` lifts the wait and `/status` shows it.

### Security

- `GH_TOKEN` goes to `docker run` by name only, and the prompt goes in on
  stdin, so neither shows up in `ps`. A prompt over 128 KiB no longer fails
  with `E2BIG`.
- Hardened runs in an umbrella project mount the sibling repos and the
  umbrella's `.git` read-only, and report any repo the run created.
- Each step of a run's cleanup runs on its own, security steps first. A
  failing step (or a Ctrl-C) no longer skips the token scrub or the removal of
  the throwaway home.

### Changed

- Dispatch looks for new briefs after every run instead of after every
  batch, and runs review-fix rounds before new stage A briefs. A round that
  stage A enqueued mid-pass used to wait behind the rest of the batch and was
  lost when the pass stopped on the usage limit or the window's end.

- The Telegram token moves to `secrets.env` (`secrets.telegram_bot_token_env`,
  default `TELEGRAM_BOT_TOKEN`). A literal `telegram_bot_token` in
  `config.yaml` still works, with a warning.

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
