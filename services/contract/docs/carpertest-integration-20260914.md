# Python integration baseline: carpertest

The user requested consolidating this round of Python work into `carpertest`
and retiring its completed task branches. This does not change GitHub's default
branch, deploy Docker services, or merge separate frontend/Java repositories.

## Preserved work

- Prior remote `carpertest`: `ec2236d29e87e43d36e5158b9e4bd05f963e32d4`.
- Review development checkpoint: `7d50baa42ae606b9a666a313e007ebf9d83735b7`.
- Previously uncommitted review fixes: `99a8c90` (82 files).
- Rule-authoring/lab branch: all five commits through
  `30d684a313fbf9f291e12f4ac6186d2998c5936c`.

Four textual merge conflicts were resolved by preserving semantic multi-role
selection together with task-local frozen Java rule snapshots. Rule selection,
review, cache identity, and replay date use the same task snapshot. Previously
removed rule-count schema limits were not restored; tenant, duplicate-ID and
snapshot integrity checks remain.

The lab fragment adapter now provides complete deterministic source coordinates
and hash. Its offline demo model uses the same ID-only output protocol as formal
rule review. Lab cache keys include the reviewer protocol version. Tests cover
unknown-source row isolation, preservation of valid sibling decisions, exact
fragment binding, and combined task snapshots/multi-role selection. Two stale
payload-limit test fixtures were updated to the ID-only protocol without
relaxing production evidence validation.

## Verification

- Final offline regression: **1226 passed, 44 skipped**, zero failures.
- Skipped historical/environment-dependent cases are not treated as passes.
- No paid model calls, live five-contract resubmissions, database changes, Docker
  rebuilds, or service restarts were performed for this Git consolidation.
- Original refs, working files and patch were backed up outside the repository
  before committing; a Git bundle preserves recoverability after branch removal.

## Branch lifecycle

Retire only the merged Python task branches:

- `feature/legal-knowledge-graph-e2e` (local and remote).
- `feature/rule-authoring-ai` (local; no remote branch existed at inspection).
- `feature/legal-evidence-planner` (remote; already at the old baseline).

Keep `main`, `caranteetest`, release branches and unrelated business branches.
Preserve the other worktree directory at its existing snapshot when detaching
it to release the completed branch name. Future changes start from freshly
fetched `origin/carpertest` on a new `codex/<task-name>` branch, as specified in
the repository's `AGENTS.md`.

Git publication is separate from deployment. The merged Java-snapshot path
requires its matching Java internal API and configuration when later deployed;
this merge does not claim that cross-repository deployment has been validated.
