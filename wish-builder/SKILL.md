---
name: wish-builder
description: Turn a vague product wish into human-governed autonomous software delivery using gstack planning reviews, Trellis task context, a dependency DAG, and supervised coding agents. Use when a user gives a high-level product direction and wants the agent to discover requirements, prepare a PRD and architecture, obtain explicit human approval, split work into independently testable Issue/PR tasks, execute serial foundations followed by parallel modules, integrate, QA, document, and escalate only for material drift or high-risk decisions. Also use to resume or audit an existing wish-builder run.
---

# Wish Builder

Convert a short product direction into a controlled multi-agent delivery run. Keep one
coordinator accountable from discovery through archive. Let humans own product and
architecture decisions; let agents own execution after approval.

## Non-Negotiable Contract

1. Keep one scheduler. The coordinator owns the DAG, dispatch, merge order, and escalation.
   Never run Trellis parallel orchestration beside native or external agent scheduling for
   the same tasks.
2. Keep two mandatory human gates. Gate A approves product scope and architecture. Gate B
   approves the complete task DAG and Issue/PR plan. The original wish is not approval.
3. Do not edit product code, create remote issues, or dispatch implementation before Gate B.
4. Treat gstack review output as advice. A human must approve or change product and
   architecture decisions. Do not use gstack `autoplan` in this workflow.
5. Use Trellis as the task, context, check, journal, and archive layer. Do not ask Trellis to
   brainstorm requirements again after the approved Gate B packet has been imported.
6. Make every leaf task independently implementable, testable, regressible, reviewable,
   squash-mergeable, and rollbackable. Tests ship in every engineering PR.
7. Permit autonomous work only inside the approved policy. Never infer approval for
   credentials, payments, production deploys, deletion, permission changes, or irreversible
   data migrations.
8. Preserve user changes and dirty worktrees. Never discard or overwrite unrelated work.

## Required References

- Read [tool-bridges.md](references/tool-bridges.md) during preflight and before invoking
  gstack or Trellis.
- Read [policy.md](references/policy.md) before presenting either gate or mutating external
  state.
- Read [artifact-contracts.md](references/artifact-contracts.md) before generating the parent
  task, child tasks, or execution manifest.
- Read [execution.md](references/execution.md) after Gate B and whenever resuming, dispatching,
  integrating, or recovering a run.

## Start Or Resume

1. Announce the current phase and the next human gate.
2. Find the repository root and inspect repository instructions, status, tests, architecture,
   product docs, existing Trellis tasks, and relevant history before asking questions.
3. Search `.trellis/tasks/` for a parent task whose `task.json.meta.wish_builder` exists.
   Resume it when its goal matches the request; do not create a duplicate run.
4. If no run exists and Git and Trellis are already initialized, the explicit request to run
   Wish Builder authorizes a local parent task and planning artifacts. Require separate setup
   consent only for installation, initialization, authentication, provider access, or repository
   settings. Store all durable run artifacts under the parent task directory.
5. After Phase 2 creates the execution manifest, run
   `scripts/wishctl.py validate <manifest> --stage <stage>` at every phase transition. Before
   then, validate Trellis context and record that no execution manifest exists yet. Treat
   validator errors as blockers and warnings as review items.

## Phase 0: Preflight

Follow `references/tool-bridges.md` and establish:

- a Git repository and base branch;
- installed gstack planning/review skills;
- an initialized, version-compatible Trellis project;
- one available worker backend and its concurrency limit;
- GitHub or equivalent issue/PR access when remote delivery is requested;
- the user's one-time autonomy policy for post-Gate-B external mutations.

If a dependency is missing, present one setup gate with exact official installation guidance.
Do not install global tools, initialize Trellis, authenticate, or change repository settings
without consent. Record accepted capabilities in the parent task decision log.

## Phase 1: Discover And Review

Use repository evidence before asking the user. Run the actual installed gstack skills and
obey their instructions; do not imitate them from memory.

1. Run `office-hours` to identify the user, problem, current alternative, narrow wedge, and
   success signal.
2. Draft the PRD, explicit non-goals, assumptions, and observable acceptance criteria.
3. Run `plan-ceo-review` to challenge product promise, scope, sequencing, and ambition.
4. Run `plan-eng-review` to propose boundaries, contracts, data flow, migrations, rollback,
   observability, and test strategy.
5. For user-facing UI, run `plan-design-review`; otherwise record why it was skipped.
6. Consolidate recommendations and close contradictions. Keep unresolved human-owned
   choices visible rather than silently selecting them.

For spawned or headless advisory reviews, auto-select the option explicitly marked recommended
instead of pausing at nested advisory question gates. Preserve material alternatives and reasons.
This rule does not bypass setup, safety, or external-mutation gates, and an advisory selection is
never human approval. The coordinator must present the consolidated result at Gate A.

## Gate A: Product And Architecture

Present one decision packet containing:

- product goal, target user, value, success measures, in-scope and out-of-scope behavior;
- recommended architecture, module boundaries, data flow, public contracts, ADRs, migration
  and rollback approach;
- UI direction when applicable;
- assumptions, alternatives, deferred items, risks, and every unresolved decision.

Write the exact candidate packet to the canonical filename `gate-a.md`; never substitute a
variant such as `gate-a-packet.md`. Compute and record its pending hash with `wishctl.py hash`
before presentation. Require an explicit response to that candidate: approve, approve with
listed edits, or revise. Recompute the hash after edits. On approval, freeze those exact bytes,
promote the same hash to approval evidence, and preserve the snapshot as specified in
`references/artifact-contracts.md`. Material later changes invalidate Gate A.

## Phase 2: Decompose And Compile

Read `references/artifact-contracts.md`. Build a requirement-ID trace and dependency DAG.

1. Put shared schemas, public interfaces, common types, authorization boundaries, migration
   scaffolding, and test infrastructure in serial Wave 0.
2. Put isolated modules with disjoint ownership in parallel Wave 1.
3. Put cross-module integration, final migrations, end-to-end validation, release toggles, and
   deployment preparation in serial Wave 2.
4. Keep dependency depth at four or less unless the Gate B packet explains why.
5. Prefer module or package boundaries. Create services only for independent deployment,
   scaling, security, or ownership needs.
6. Create one leaf task per planned Issue and PR. Include owned paths, dependencies,
   acceptance criteria, regression commands, risk, rollback, and documentation impact.
7. Render parent and child Trellis artifacts plus `execution-manifest.json`. Curate real entries
   in each child's `implement.jsonl` and `check.jsonl`.
8. Run `wishctl.py validate <manifest> --stage planning` and resolve every error.

## Gate B: DAG And Delivery Policy

Write the exact candidate packet to the canonical filename `gate-b.md`; never substitute a
variant such as `gate-b-packet.md`. Present the complete DAG, waves, leaf task packets, contract
freeze, Issue/PR map, merge order, test plan, documentation plan, autonomy settings, and expected
escalation conditions. Compute and record its pending hash with `wishctl.py hash` before
presentation. Require explicit approval of that candidate, recompute after edits, and on approval
freeze and snapshot the exact approved bytes.

After approval, record Gate B evidence, create remote Issues if authorized, assign one Issue to
each leaf, write Issue IDs and branches into the manifest, and run:

```text
wishctl.py validate <manifest> --stage execution
```

Do not dispatch while this validation fails.

## Phase 3: Execute

Read `references/execution.md` and run a coordinator loop:

1. Ask `wishctl.py ready <manifest>` for the ready set.
2. Dispatch at most one Wave 0 or Wave 2 task at a time. Dispatch Wave 1 tasks up to the
   approved limit, defaulting to three.
3. Give each worker one Trellis child task, one worktree, exact ownership, frozen contracts,
   acceptance criteria, regression commands, and a structured completion contract.
4. Let the Trellis task pipeline perform Implement, Check, Finish, and Draft PR. Do not wrap a
   second scheduler around the same pipeline.
5. Require the worker to run `wishctl.py drift` on its changed files before completion.
6. Review the diff, run the task checks, run gstack `review` when available, and verify Issue,
   PR, requirement, test, and rollback links.
7. Squash-merge only after dependencies are merged, required CI is green, ownership is clean,
   and the approved policy allows it. Keep incomplete behavior behind feature flags.
8. Update the manifest atomically after every dispatch, PR, merge, verification, failure, or
   escalation. Recompute the ready set after every change.

Workers never merge, change approved architecture, expand scope, or edit another task's owned
paths. Route conflicts and failed checks back to the owning worker.

## Phase 4: Integrate, QA, And Document

After leaf tasks merge:

1. Run all repository tests plus cross-module contract, migration, and end-to-end tests.
2. Run gstack `qa` for user-visible workflows. Run `design-review` for UI. Run security or
   benchmark skills when the approved risk model requires them.
3. Open separate traced repair tasks for non-trivial findings. Do not hide fixes in unrelated
   PRs.
4. Run `document-release` or update repository documentation directly when that skill is not
   installed. Update architecture, interaction behavior, operational notes, and rollback steps.
5. Run `wishctl.py trace <manifest> --output <parent>/traceability.md`.
6. Mark a requirement implemented only after its PRs are merged and acceptance evidence passes.
7. Run `wishctl.py validate <manifest> --stage finish`, archive completed Trellis children,
   update the journal, then archive the parent.

Production deployment is a separate Gate C unless Gate B explicitly authorized a bounded
deployment target and rollback policy.

## Drift And Recovery

Apply `references/policy.md`. For ordinary failures:

1. Let the owning worker repair once with concrete failing evidence.
2. Give a fresh reviewer or fixer the raw task artifacts and failure output.
3. On a third consecutive failure, circuit-break the task, mark it failed, and let the
   coordinator re-split or re-plan it.
4. Interrupt the user only if recovery changes the approved product, architecture, contracts,
   risk, or scope baseline.

## Completion Report

Report the delivered outcome first, then Gate evidence, merged PRs, requirement coverage,
tests, deferred work, known risks, and deployment state. Never claim completion while the
manifest, traceability matrix, CI, or Trellis archive disagrees.
