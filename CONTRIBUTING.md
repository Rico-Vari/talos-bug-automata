# Contributing to Talos

Thanks for your interest! Talos is a small, single-maintainer project, so a
short conversation before a big change saves everyone time.

## Before you start

- **Bugs:** open an issue with the bug template. Include `talos-dispatch
  --dry-run` output or the relevant `~/.orchestrator/logs` excerpt, with
  secrets removed.
- **Features / refactors:** open an issue first and describe the problem you
  want to solve. PRs that arrive without prior discussion may be declined if
  they don't fit the design.
- **Security issues:** see [SECURITY.md](SECURITY.md). Never in a public issue.

## Development setup

```bash
git clone https://github.com/Rico-Vari/talos-bug-automata.git
cd talos-bug-automata
python3 -m venv venv && source venv/bin/activate
pip install -e .
cp config.example.yaml config.yaml   # edit paths; never commit config.yaml
```

Docker, `gh` and a logged-in Claude Code are only needed to run real
pipelines, not for the test suite.

## Tests

```bash
python tests/test_offline.py
```

The suite is fully offline: it fakes GitHub, Sentry, Docker and Telegram.
Every behavior change needs a check in it. A PR is ready when the suite prints
`✔ all green` (or the equivalent final line) and exits 0.

## Style

- Python 3.11+, standard library first. New dependencies need a reason.
- Match the surrounding code: comments explain *why*, not *what*.
- English for code, comments, docs, log messages and bot replies.
- Keep persisted identifiers stable (DB state values, label names, front-matter
  keys, file names under `~/.orchestrator/`). Changing one needs a migration
  note in the PR.

## Commits and PRs

- [Conventional Commits](https://www.conventionalcommits.org/):
  `feat(webhookd): …`, `fix(dispatch): …`, `docs: …`.
- One logical change per PR. Fill in the PR template, including how you tested.
- Never commit `config.yaml`, `secrets.env`, tokens, or real webhook payloads
  from private repositories. Test fixtures must be synthetic or scrubbed.

## Agent prompts and BMAD overrides

Changes to `src/talos/prompts/` or `src/talos/bmad-kit/` change what the
agents do on real repositories. Describe the behavior change in the PR and, if
you can, link a run (sandbox repository) that shows it.

## License

By contributing you agree that your contributions are licensed under the
[MIT License](LICENSE).
