---
name: wish-builder
description: Turn a vague product wish into human-governed autonomous software delivery using gstack planning reviews, a Trellis-authored task graph, an approved immutable execution snapshot, and supervised coding agents. Use when a user gives a high-level product direction and wants the agent to discover requirements, prepare a PRD and architecture, obtain explicit human approval, have Trellis create independently testable Issue/PR tasks, import and validate that graph, execute with exactly one scheduler, integrate, QA, document, and escalate only for material drift or high-risk decisions. Also use to resume or audit an existing wish-builder run.
---

# Wish Builder

Convert a short product direction into a controlled multi-agent delivery run. Keep one
coordinator accountable from discovery through archive. Let humans own product and
architecture decisions; let agents own execution after approval.

## Non-Negotiable Contract

1. Trellis owns the editable task graph; the coordinator owns the approved immutable execution
   snapshot, admission, fencing, Journal, and recovery.
2. Keep scheduler ownership and worker implementation as separate axes. Active M1 manifest v2
   accepts only `wish_builder + pi|oh_my_pi|codex` and rejects every other
   `scheduler_mode + worker_backend` pair. A separate backend/OS qualification record must enable
   the exact cell before launch; the bundled record currently disables every dispatch cell. A
   future `trellis + trellis` mode may let Trellis dispatch sibling tasks while the coordinator
   validates and supervises, but active M1 cannot represent or execute that mode. Never run both
   dispatchers for the same tasks. In that future mode, `GraphIndex` remains a safety-validation
   and recovery index, not a second dispatcher.
3. Keep two mandatory human gates. Gate A approves product scope and architecture. Gate B
   approves the material graph projected from a stable Trellis task-record read, the Wish Builder-
   derived graph and manifest digests, and the Issue/PR plan. The Wish Builder-derived task-record
   revision digest is provenance;
   lifecycle-only status or progress changes do not invalidate Gate B when the canonical graph digest
   is unchanged. The original wish is not approval.
4. Do not edit product code, create remote issues, or dispatch implementation before Gate B.
5. Treat gstack review output as advice. A human must approve or change product and
   architecture decisions. Do not use gstack `autoplan` in this workflow.
6. Use Trellis to create and decompose tasks, edit dependencies, hold task context, run the
   Implement/Check/Finish lifecycle, and preserve history and archives. Wish Builder may report
   decomposition defects, but it must not create or maintain a competing task graph. A material
   Trellis graph change after Gate B invalidates that approval.
7. Make every leaf task independently implementable, testable, regressible, reviewable,
   squash-mergeable, and rollbackable. Tests ship in every engineering PR.
8. Permit autonomous work only inside the approved policy. Never infer approval for
   credentials, payments, production deploys, deletion, permission changes, or irreversible
   data migrations.
9. Preserve user changes and dirty worktrees. Never discard or overwrite unrelated work.

## Required References

- Read [tool-bridges.md](references/tool-bridges.md) during preflight and before invoking
  gstack or Trellis.
- Read [policy.md](references/policy.md) before presenting either gate or mutating external
  state.
- Read [artifact-contracts.md](references/artifact-contracts.md) before asking Trellis to prepare
  candidate tasks or importing and compiling an execution manifest.
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
5. After Phase 2 imports the Trellis graph and compiles the execution manifest, run
   `scripts/wishctl.py validate <manifest> --stage <stage>` at every phase transition. Before
   then, validate Trellis context and record that no execution manifest exists yet. Treat
   validator errors as blockers and warnings as review items.
6. For an unknown-status dispatch that needs a direct CLI recovery, use
   `scripts/wishctl.py resume <approved-manifest-v2> <dispatch-recovery-proof.json> --journal-root <journal-root>`.
   Do not hand-edit the Journal, rewrite the manifest, or relaunch the worker outside that command.
   The proof must be canonical `DispatchRecoveryPayload` evidence for exactly one prior
   `DISPATCH_REQUESTED` worker-dispatch event, with an absent dispatch receipt, proven process-tree
   termination, the current Journal head as its recovery anchor, and a human direct-CLI reconcile
   command.

## Phase 0: Preflight

Follow `references/tool-bridges.md` and establish:

- a Git repository and base branch;
- installed gstack planning/review skills;
- an initialized, version-compatible Trellis project;
- one allowed `scheduler_mode + worker_backend` pair, concurrency limit, and lease policy; Trellis
  scheduling is available only when it can propose before launch, wait for admission, carry the
  full run/fencing identity, and honor stop/reject decisions;
- GitHub or equivalent issue/PR access when remote delivery is requested;
- the user's one-time autonomy policy for post-Gate-B external mutations.

The implemented Trellis bridge supports exactly `@mindfoldhq/trellis@0.6.15` and
`@mindfoldhq/trellis-core@0.6.15`. Never install or resolve `@latest`. `0.7.0-dev.2` was a
local test fixture later withdrawn from Wish Builder; it was never an official Trellis release and
is not supported. Check Trellis graph/import compatibility,
projection writeback capability, backend/OS dispatch qualification, and scheduler-specific
admission as separate concerns. Trellis `0.6.15` lacks reliable cross-process CAS, so projection
remains single-writer with stable reads, pre/post-write digest checks, and fail-closed conflict
handling. Backend workers operate only in isolated Git worktrees and never write Trellis. Their
dispatch admission is independent of projection CAS: keep a backend/OS cell
`enabledForDispatch=false` until that exact cell has complete, independently verified live
evidence and a human publishes it. Keep projection single-writer while official Trellis lacks CAS.
The future `trellis + trellis` mode remains outside manifest v2 until its pre-launch admission,
fencing, stop/reject contract, and concurrent-write ownership are qualified.

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

Apply the gstack question handoff contract in
`references/tool-bridges.md#gstack-question-handoff` to every gstack review:

1. **GQ-1 - Isolate every review.** Run each review in its own non-interactive child session. The
   coordinator is the only user-facing session.
2. **GQ-2 - Hide raw questions.** Never display, quote, or relay a raw gstack question to the user.
3. **GQ-3 - Transfer complete decisions.** Temporarily select only gstack's explicitly recommended
   answer so the child can continue. For every decision, require the child to return the practical
   outcome, alternatives, recommendation and reason, ease of changing later, whether it concerns
   `product`, `architecture`, `cost`, `security`, or `external_action`, and the original technical
   explanation.
4. **GQ-4 - Reclassify centrally.** Auto-adopt and record only an easy, reversible engineering
   choice. Rewrite any material, difficult-to-reverse, disputed, product, architecture, cost, or
   security choice in plain language and queue it for Gate A. Keep external actions under the
   explicit approval rules in `references/policy.md`.
5. **GQ-5 - Preserve approval authority.** Treat every automatic gstack choice as advice only,
   never as human approval.
6. **GQ-6 - Batch human decisions.** Do not interrupt the user decision by decision. Consolidate
   the human-owned choices into the Gate A packet.
7. **GQ-7 - Fail closed.** If a child review tries to ask the user directly or returns incomplete
   decision data, stop that child review, record an `integration_capability_failure`, and never
   relay its raw question. Do not treat that review as complete.

An automatic advisory choice never bypasses setup, safety, credential, payment, deployment, or
other external-mutation approval.

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

## Phase 2: Prepare In Trellis, Import And Compile

Read `references/artifact-contracts.md`. Trellis is the only editable task-graph authority.

1. Let Trellis create candidate tasks and dependencies from the Gate A product and architecture
   documents. Trellis owns task creation, decomposition, dependency editing, task context, and
   lifecycle artifacts.
2. Read the parent and candidate Trellis task records through the `0.6.15` bridge. Require stable
   reads and derive the Wish Builder graph snapshot, revision digest, graph digest, and task ID
   mapping. These are Wish Builder contracts, not Trellis APIs or fields.
3. Validate dependency integrity, owned paths, acceptance and regression commands, requirement
   coverage, rollback data, and task granularity. Shared contracts and foundations must precede
   parallel siblings; parallel tasks must have disjoint ownership; integration work must follow
   its dependencies; and each leaf must fit one independently testable Issue/PR. Return defects
   to Trellis for revision and re-import. Wish Builder may explain decomposition problems, but it
   must not generate a different task set or edit Trellis dependencies itself.
4. Deterministically compile `execution-manifest.json` from that derived snapshot. Run
   `TaskDag.compile` and build `GraphIndex` from the imported snapshot, then run
   `wishctl.py validate <manifest> --stage planning` and resolve every error through Trellis.
5. Gate B approves the material graph projected from the stable Trellis task-record read, the
   Wish Builder-derived graph and revision provenance, execution-manifest digest, selected
   `scheduler_mode`, selected `worker_backend`, lease policy, and delivery policy. Freeze the
   manifest after approval. Lifecycle-only task status or progress changes do not invalidate Gate B
   while the canonical graph digest remains unchanged.

## Gate B: DAG And Delivery Policy

Write the exact candidate packet to the canonical filename `gate-b.md`; never substitute a
variant such as `gate-b-packet.md`. Present the stable task-record input used to project the material
graph, the derived revision provenance and graph digests, the
complete DAG, waves, leaf task packets, execution-manifest digest, scheduler mode, worker backend,
maximum concurrency, lease TTL/skew, contract freeze, Issue/PR map, merge order, test plan,
documentation plan, autonomy settings, and expected
escalation conditions. Compute and record its pending hash with `wishctl.py hash` before
presentation. Require explicit approval of that candidate, recompute after edits, and on approval
freeze and snapshot the exact approved bytes. A material Trellis task or dependency change changes
the derived canonical graph digest and automatically invalidates Gate B. Lifecycle-only progress
is excluded from that graph projection; it remains provenance and does not invalidate the gate.

After approval, record Gate B evidence, create remote Issues if authorized, and assign one Issue to
each leaf. Record runtime Issue, branch, PR, and worker identities in Trellis metadata and the
Journal/runtime projection; do not mutate the approved execution snapshot. Then run:

```text
wishctl.py validate <manifest> --stage execution
```

Do not dispatch while this validation fails.

## Phase 3: Execute

Read `references/execution.md`. First validate the official Trellis graph/import compatibility and
the frozen graph digest. Active M1 then validates the exact `wish_builder` backend/OS qualification
cell.
For `scheduler_mode=wish_builder`, stop with `dispatch_not_qualified` unless that backend/OS cell
has `enabledForDispatch=true`. Bind the selected cell to the pinned Trellis compatibility digest,
but do not inspect projection CAS as a worker-dispatch condition. Backend qualification and
Trellis compatibility remain separate evidence records. Workers run in isolated Git worktrees,
canonical progress is committed to the Journal, and one coordinator-owned projection writer
catches Trellis up afterward. A projection delay or conflict is repairable from the Journal and
must not roll back or redeliver admitted worker work. Reject `scheduler_mode=trellis` as unsupported
in active manifest v2. Its future path requires a separately qualified pre-launch proposal,
admission, identity, fencing, stop/reject contract, and concurrent-write ownership; it does not use
a Pi, Oh My Pi, or Codex backend/OS cell. The bundled backend record currently disables every
active M1 cell because the required live evidence is incomplete, so the current M1 stops here.
Only after a future qualification record enables one cell may the coordinator enforce the Gate B
`scheduler_mode + worker_backend` pair and run exactly one sibling-task scheduler:

1. Derive a fresh stable snapshot from the current Trellis task records and revalidate its canonical
   graph digest against the frozen manifest before every admission decision. Stop and invalidate
   Gate B on material graph drift; lifecycle-only progress is outside the graph projection.
2. Future design only: in `scheduler_mode=trellis`, require `worker_backend=trellis`. Active M1
   manifest v2 rejects this pair. Once implemented, Trellis must propose each sibling
   before launch. Validate the proposal against the Gate, lease, frozen `TaskDag`, `GraphIndex`,
   ownership, and concurrency; durably admit it with the full fencing identity, then let Trellis
   launch. Rejection or timeout means no launch. Do not dispatch siblings from Wish Builder's
   ready queue.
3. In `scheduler_mode=wish_builder`, require `worker_backend=pi`, `oh_my_pi`, or `codex`; ask
   `wishctl.py ready <manifest>` for the ready set and dispatch only that backend. Dispatch at most
   one serial foundation or integration task at a time and parallel siblings only up to the
   approved limit.
4. Give each worker one Trellis child task, one worktree, exact ownership, frozen contracts,
   acceptance criteria, regression commands, and a structured completion contract.
5. Use Trellis task context and its Implement, Check, Finish, and Draft PR lifecycle in either
   mode. Scheduler ownership does not change Trellis's task-lifecycle ownership.
6. Require the worker to run `wishctl.py drift` on its changed files before completion.
7. Review the diff, run the task checks, run gstack `review` when available, and verify Issue,
   PR, requirement, test, and rollback links.
8. Squash-merge only after dependencies are merged, the revision-bound verification required by
   Gate B is green, ownership is clean, and the approved policy allows it. Verification may be
   local or CI-backed as the project policy states; never mislabel local evidence as CI. Keep
   incomplete behavior behind feature flags.
9. Append every dispatch, PR, merge, verification, failure, and escalation to the Journal and
   rebuild the runtime projection. Never use runtime progress to rewrite the immutable manifest.

Workers never merge, change approved architecture, expand scope, or edit another task's owned
paths. Route conflicts and failed checks back to the owning worker.

Scheduler boundaries are exact:

- Future `scheduler_mode=trellis` means Trellis is the only sibling dispatcher. Active M1 rejects
  this mode. Once implemented, Wish Builder may validate
  proposals, admit or reject them, supervise progress, reconcile evidence, and recover, but it must
  not select from its ready queue or launch sibling workers itself. `wishctl.py ready` and
  `GraphIndex` are comparison and recovery tools in this mode.
- `scheduler_mode=wish_builder` means Wish Builder is the only sibling dispatcher. Trellis still
  owns task context and Implement/Check/Finish/Draft PR lifecycle records, but it must not schedule
  sibling tasks or launch workers.
- Serial execution is not a separate mode. It is `scheduler_mode=wish_builder` with an allowed
  worker backend and concurrency one.

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
3. On a third consecutive failure, circuit-break the task and mark it failed. If decomposition
   must change, return the finding to Trellis, re-import the revised graph, and repeat Gate B.
4. Interrupt the user only if recovery changes the approved product, architecture, contracts,
   risk, or scope baseline.

For crash or lease recovery, rely on the Journal and recovery commands rather than memory. Before
accepting recovered output or resuming an unknown dispatch, prove the active lease, manifest digest,
dispatch request identity, worker absence, and process termination. The direct CLI path is
`wishctl.py resume`; it may append recovery events, but it must not execute worker effects or repair
the immutable execution manifest.

## Implementation Boundary

Keep the existing `Task`, `ExecutionManifest`, `TaskDag.compile`, `GraphIndex`, Journal, Gate, and
recovery components. They validate, freeze, admit, fence, replay, and recover an imported Trellis
graph; they do not author the editable graph.

The implemented task-graph boundary is the Wish Builder import/projection bridge for official
Trellis `0.6.15`, a
deterministic `Trellis task records -> wish-builder.trellis-graph.v1 -> execution manifest`
conversion, and validation tests for projection, drift, and Gate B invalidation. The graph snapshot,
revision digest, graph digest, and task ID mapping are Wish Builder-derived contracts. Projection
uses one writer, stable reads, pre- and post-write SHA-256 checks, and fail-closed conflict or
unknown-outcome handling; it is not cross-process CAS. The projection record does not qualify or
block a backend worker. Worker admission uses the separately published backend/OS qualification
cell and binds it to the approved Trellis compatibility digest. Do not
implement `decomposer.py`, an AI PRD-to-task decomposer, task CRUD, a dependency editor, or another
task database or board.

## Completion Report

Report the delivered outcome first, then Gate evidence, merged PRs, requirement coverage,
tests, deferred work, known risks, and deployment state. Never claim completion while the
manifest, traceability matrix, required verification evidence, or Trellis archive disagrees.
