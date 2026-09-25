# On complete (HEADLESS=1)

The skill's own workflow is finished. Do **not** halt, do not offer to push,
and do not ask what to do next.

Return control to the orchestrator system prompt
(`/workspace/.lead-orchestrator.md`) and continue from the phase that follows
the one you just completed. The orchestrator owns the PR, the review
publication and `/workspace/.orchestrator-result.json`.

Before returning, state in one line: which skill finished, the path of the
spec file it produced (relative to `/workspace`), and the count of entries
under `## Automated Decisions`.
