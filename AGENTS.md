# Branch workflow

The user designated `origin/carpertest` as this Python repository's integration
baseline on 2026-09-14. GitHub's default branch may still be `main`; do not confuse
that setting with the development baseline.

- For future code changes, first inspect and preserve existing work, fetch
  `origin/carpertest`, and start a new `codex/<task-name>` branch from its latest
  commit. Do not resume the retired legal-knowledge-graph or rule-authoring branches.
- Merge and push changes only when the user requests it. Never force-push or
  delete unrelated branches as part of routine development.
- Before deleting a completed task branch, verify its commits are reachable from
  the remotely confirmed integration commit. Preserve other worktree directories
  and uncommitted changes.
- Git synchronization does not deploy Docker services or run paid model tests.
  Report these operations separately and follow the user's testing budget.
