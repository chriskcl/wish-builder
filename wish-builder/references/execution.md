# Execution And Recovery

## Contents

1. State Machine
2. Coordinator Loop
3. Worker Contract
4. Integration
5. Quality Layers
6. Failure Recovery
7. Resume

## State Machine

```text
preflight
  -> discovery
  -> gate_a_pending
  -> decomposition
  -> gate_b_pending
  -> foundations
  -> parallel_modules
  -> integration
  -> quality
  -> documentation
  -> gate_c_pending (only when needed)
  -> archived
```

`paused`, `blocked`, `failed`, and `escalated` are side states. Persist every transition in the
parent decision log and manifest before beginning the next action.

## Coordinator Loop

Repeat until no unfinished task remains:

1. Validate the manifest for the execution stage.
2. Read current task and PR state from the source systems; do not trust stale chat memory.
3. Reconcile manifest status with Trellis, worker, Issue, PR, and CI evidence.
4. Run `wishctl.py ready` and select only the returned tasks.
5. Create or reuse the correct isolated worktree and dispatch one task per worker.
6. Wait on structured worker completion or escalation, not arbitrary sleep loops.
7. Validate changed-path ownership, test evidence, Trellis check, and PR metadata.
8. Route repairs to the owner or dispatch an independent review task.
9. Merge in topological order when policy permits, then update the manifest atomically.
10. Recompute readiness after every state change.

Keep Wave 0 and Wave 2 serial. During Wave 1, use the lower of the approved limit and the host's
available worker capacity. Default to three workers. Do not reserve all capacity for workers if
the coordinator needs one slot.

## Worker Contract

Every dispatch must state:

- task ID, dispatch identity, Trellis task directory, worktree, base branch, and Issue;
- requirement IDs, acceptance criteria, regression commands, and documentation impact;
- owned and protected paths, frozen contracts, dependencies, and allowed auxiliary paths;
- instruction to inspect repository evidence and use the active Trellis task context;
- instruction to implement, test, self-review, run drift detection, and open a Draft PR;
- prohibition on scope expansion, architecture changes, merges, deploys, secrets, and edits to
  another task's ownership;
- exact structured completion and escalation mechanism for the selected backend.

Workers must not ask the user routine engineering questions. They should inspect evidence, pick
the smallest compliant implementation, and ask the coordinator only when a decision changes the
approved baseline.

## Integration

Prefer small squash merges directly to the approved base branch while incomplete behavior stays
behind feature flags. This preserves one Issue, one PR, one rollback commit, and a continuously
green mainline.

Before merge, require:

- all dependency PRs merged;
- CI and declared regression commands green;
- Trellis Check and Finish evidence;
- changed paths inside ownership;
- acceptance criteria demonstrated;
- gstack review findings resolved or explicitly accepted;
- docs and migration/rollback notes included;
- `Closes #<issue>` plus requirement and task IDs in the PR.

Workers do not merge. The coordinator merges only when Gate B policy authorizes it. Use a final
traced release-toggle Issue/PR when activating a feature is separate from implementation.

## Quality Layers

| Layer | Owner | Required Evidence |
| --- | --- | --- |
| Leaf | worker | unit/module/contract tests and regression commands |
| PR | coordinator/reviewer | diff review, CI, ownership and acceptance |
| Wave | coordinator | dependency integration and migration checks |
| Product | QA worker | end-to-end user journeys and failure paths |
| UI | design reviewer | responsive, visual, accessibility and interaction checks |
| Release | coordinator | docs, rollback, observability and trace completeness |

Final QA does not replace leaf testing. Significant QA fixes become new traced leaf tasks rather
than unreviewed edits in the coordinator's worktree.

## Failure Recovery

Use evidence-driven repair:

1. First failure: return logs and reproduction steps to the owning worker for one bounded repair.
2. Second failure: dispatch a fresh reviewer/fixer with raw artifacts and logs, not the previous
   diagnosis.
3. Third consecutive failure: circuit-break, mark failed, stop dependents, and re-split or
   re-plan the task.
4. Escalate to a human only when recovery changes product intent, architecture, contracts, risk,
   external authorization, or roughly doubles approved scope.

A timed-out wait is not a worker failure when the worker is alive. Check task/terminal state and
continue bounded waits. Never kill or redispatch active work solely because it is slow.

## Resume

On resume:

1. Locate the parent by `task.json.meta.wish_builder`.
2. Read its PRD, design, implementation plan, decision log, manifest, and traceability matrix.
3. Validate the manifest and compare it with Trellis tasks, Git branches, Issues, PRs, CI, and
   recent commits.
4. Repair stale manifest state only from objective evidence and log the reconciliation.
5. Re-enter the persisted phase. Do not repeat approved reviews or gates unless their artifact
   hashes changed materially.
