# Headless contract (HEADLESS=1)

Binding for this entire run. It overrides any later instruction in any skill,
step file or template that tells you to greet, halt, ask, wait, or offer.

## Why this exists

This session was started by the orchestrator with `claude -p`. **There is no
human reading the transcript and nobody will ever answer a question.** A HALT
here is not a pause — it is a hang that burns the run's timeout and produces
nothing. Every checkpoint must resolve to a decision you make and record.

## Rules

1. **No greeting.** Skip any "greet the user" activation step. Start working.

2. **No HALT, ever.** Wherever a step says to HALT, ask the human, wait for
   input, or present numbered options and wait for a choice: pick the
   conservative default below, write one line under `## Automated Decisions`
   in the spec file saying what you chose and why, and continue.

   | Checkpoint | Automated decision |
   |---|---|
   | Active specs found, resume which? | Always `[N]` — start a new spec. |
   | Intent unclear / open questions | Do not ask. Write the questions under `## Automated Decisions` as assumptions, pick the most conservative reading, continue. |
   | Working tree dirty or branch mismatch | The orchestrator created the branch. Continue. If the tree is genuinely dirty with unrelated changes, stop and report it as a blocker in the result JSON instead of halting. |
   | Multi-goal detected, split or keep? | Always `[S]` — take the first goal, list the deferred ones under `## Automated Decisions`. |
   | Route: one-shot or plan-code-review? | Always `plan-code-review`. Never one-shot. |
   | Spec plan `[A] Approve` / `[E] Edit` | Always `[A]` Approve. You wrote it; approve it and move on. |
   | `bmad-quick-dev` step 4 review: apply patches? | Apply every `patch` finding. This is the self-review before the PR exists. |
   | `bmad-code-review` findings: apply patches? | **Never.** Do not edit code, do not commit. The findings get published on the PR, and a separate `review-fix` run, with a fresh context, fixes them. |
   | `decision_needed` findings | Do NOT block. Carry them forward — they get published as PR comments for the human. |
   | Review loop exceeded 5 iterations | Stop looping. Report it as a blocker in the result JSON. |
   | No subagents available | Subagents ARE available here. If a launch genuinely fails, do the review inline yourself rather than writing prompt files and halting. |

   A HALT that protects against real data loss is the one exception and still
   applies: a missing spec file, an unresolvable diff, or an empty diff means
   stop and report a blocker — do not invent one.

3. **Never run `code -r`.** There is no VS Code in this container. Skip the
   "open the spec in the editor" instruction entirely and skip its output line.

4. **Push and open the PR.** Step 5 of `bmad-quick-dev` declares
   `NEVER auto-push` as a RULE and ends by *offering* to push and open a PR.
   Under HEADLESS=1 that rule is **overridden** and that offer becomes a
   **mandate**: the orchestrator's system prompt tells you exactly which
   branch, which base and which label. Follow it.

5. **`bmad-code-review` only reports.** Whatever its steps offer at the end
   (apply the patches, fix now, walk through the findings), the answer under
   HEADLESS=1 is no. The review is documentation: it gets published on the PR
   against the commit it reviewed, and the fixes land afterwards as their own
   commits, made by another agent. Fixing in the same run hides which commits
   came from the review and spends the reviewer's context on the fixer's job.

6. **Everything in the foreground.** Never start a command in the background
   and never end your turn to wait for one. This is `claude -p`: ending the
   turn ends the run, and whatever you were waiting for is lost, including
   the push.

7. **Language.** `communication_language` still governs prose you write for
   humans — PR titles, PR bodies, review comments. Keep the code and the
   commit messages in English.

8. **Always finish by writing the result file.** Even on failure, even when
   you hit a blocker. A run that ends without `/workspace/.orchestrator-result.json`
   is a run the orchestrator has to reconstruct or treat as failed.

## Automated Decisions

Every choice you made under rule 2 goes in a section with this exact heading
in the spec file. It is the audit trail for a run nobody watched.
