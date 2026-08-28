# Tool Bridges

## Contents

1. Preflight
2. gstack Boundary
3. Trellis Boundary
4. Scheduler And Worker Selection
5. Compatibility Rules

## Preflight

Inspect before changing the repository:

| Capability | Evidence | If Missing |
| --- | --- | --- |
| Git | repository root, status, base branch | Ask where to initialize or select a repo |
| gstack | installed skill metadata for required skills | Offer official gstack setup, then stop |
| Trellis | `.trellis/workflow.md` and task scripts | Offer official Trellis setup, then stop |
| Scheduler mode | Active M1: `wish_builder`; future design: `trellis` | Reject the future mode until a later schema and qualification exist |
| Worker backend | Active M1: Pi, Oh My Pi, or Codex; future design: Trellis | Stop until one active scheduler/worker pair is qualified |
| Issue/PR | authenticated provider and remote | Keep local IDs until access is approved |
| Test runner | repository commands and CI config | Make test bootstrap a Wave 0 task |

Never auto-install a global dependency. Current official sources are:

- gstack: `https://github.com/garrytan/gstack`
- Trellis: `https://github.com/mindfold-ai/Trellis`
- Trellis CLI: `npm install -g @mindfoldhq/trellis@0.6.15`
- Trellis Core bridge: a verified local package root or official tarball for
  `@mindfoldhq/trellis-core@0.6.15`

The implemented bridge supports exactly those two `0.6.15` packages. Never install or resolve
`@latest` for this integration. `0.7.0-dev.2` was a local test fixture later withdrawn from Wish
Builder; it was never an official Trellis release and is not supported.

Trellis currently requires Node.js 18 or newer and Python 3.9 or newer. Follow the installed
version's documentation instead of assuming commands from another version. Typical Codex
initialization is `trellis init --codex -u <name>`.

On Windows, Trellis probes `python`, `python3`, and `py -3`. If the host provides Python at a
different path, pass that executable through `TRELLIS_PYTHON_CMD` for the Trellis process. Use an
absolute path with forward slashes (`C:/.../python.exe`): some Trellis versions render the value
inside Python docstrings, where a path containing `\U` is a syntax error. Do not use the skip-check
escape hatch unless the executable has separately been verified as Python 3.9 or newer.

After initialization or update, run a read-only smoke test such as the installed task script's
`list` or help command. File existence alone is not evidence that generated hooks and scripts can
execute. If the smoke test fails, stop at setup rather than patching generated Trellis templates
silently.

## gstack Boundary

Run the actual installed skills in this order:

1. `office-hours`
2. `plan-ceo-review`
3. `plan-eng-review`
4. `plan-design-review` for UI work
5. `review` for each landing candidate
6. `qa` after integration
7. `design-review` for integrated UI
8. `document-release` after code is stable

Do not use `autoplan`: it makes early decisions automatically and weakens human architecture
control. Do not use `spec --execute`: Trellis owns the editable task graph and task lifecycle, and
that command would introduce a second execution owner or dispatcher. Using `spec` only to draft an
Issue is acceptable when it does not dispatch or duplicate the execution owner.

gstack artifacts are advisory inputs. Preserve their alternatives, concerns, and scores under
the parent Trellis task's `research/` directory. Consolidate them into approved `prd.md` and
`design.md`; do not make workers read every raw review when curated context is enough.

### gstack Question Handoff

Use this protocol for every gstack review, in Phase 1 or later:

1. **GQ-1 - Isolate every review.** Run every gstack review in its own non-interactive child
   session. The child must return results to the Wish Builder coordinator and must not own the
   user conversation.
2. **GQ-2 - Hide raw questions.** Never display, quote, or relay a raw gstack question to the
   user. A later plain-language decision prompt must be written by Wish Builder from the decision
   transfer, not copied from the child prompt.
3. **GQ-3 - Transfer complete decisions.** At each advisory question or stop point, temporarily
   choose only the answer explicitly marked recommended so the child can finish the review. Return
   one decision transfer per choice with all of these fields:

   - `practical_outcome`: what changes in the product or implementation;
   - `alternatives`: the other viable choices and their consequences;
   - `recommendation_and_reason`: the provisional choice and why gstack recommends it;
   - `changeability`: how easy or difficult the choice is to change later;
   - `decision_class`: a yes/no flag for each of `product`, `architecture`, `cost`, `security`,
     and `external_action`; all five may be false for a purely engineering choice;
   - `original_technical_explanation`: gstack's technical rationale, without its raw question.

4. **GQ-4 - Reclassify centrally.** Wish Builder must classify each transfer again. Auto-adopt a
   choice only when it is an easy, reversible engineering choice with no material, disputed,
   product, architecture, cost, security, or external-action consequence; record the adoption and
   rationale in the decision log. If a choice is material, difficult to reverse, disputed, or
   concerns product, architecture, cost, or security, rewrite it in plain language and queue it for
   Gate A. Keep `external_action` under the separate approval rules in `policy.md` and include it in
   Gate A when it affects the product or architecture baseline.
5. **GQ-5 - Preserve approval authority.** Automatic gstack choices are advice only. They are
   never human approval and cannot approve product scope, architecture, cost, security, setup,
   credentials, payment, deployment, or external mutation.
6. **GQ-6 - Batch human decisions.** Do not interrupt the user decision by decision. Consolidate
   all human-owned review choices into one Gate A decision packet. A gstack prompt never creates a
   per-question user interruption; independent setup or safety gates still apply.
7. **GQ-7 - Fail closed.** If the child directly asks the user, exposes an interactive question,
   lacks an explicitly recommended answer, or omits any required GQ-3 field, stop that child review
   immediately. Record an `integration_capability_failure`, preserve diagnostic evidence, and do
   not treat the review as complete. Never relay the raw question or invent the missing decision
   data; resolve the integration failure and rerun the child review.

The provisional choice exists only to let the non-interactive child review continue. Preserve the
complete transfer as advisory evidence, even when Wish Builder auto-adopts the choice.

When the approved run policy forbids network access or writes outside the repository, use the
installed skill's supported offline or headless path. Skip optional update checks, telemetry,
global state, web research, and browser or mockup artifacts, and list every skipped section in the
saved review. If the substantive review cannot run within the boundary, report the capability gap
instead of imitating its result.

## Trellis Boundary

Trellis owns:

- task creation and decomposition from the approved Gate A documents;
- task dependency editing;
- repository-scoped engineering specs in `.trellis/spec/`;
- parent and child task context and lifecycle;
- phase-specific `implement.jsonl`, `check.jsonl`, and `debug.jsonl` context;
- each leaf task's Implement, Check, Finish, and Create PR lifecycle;
- task history and archive.

The wish-builder coordinator owns:

- validation of the imported Trellis task graph;
- Gate A orchestration plus Gate B approval of the material graph projected from a stable Trellis
  task-record read and the Wish Builder-derived revision, graph, and manifest digests;
- the immutable `execution-manifest.json` derived from the approved Trellis graph;
- execution admission, fencing, Journal, and recovery;
- result validation, merge admission, cross-task drift, integration, release, and escalation.

Official Trellis `0.6.15` exposes task records, not an atomic graph export or official graph
revision API. Wish Builder therefore derives `wish-builder.trellis-graph.v1`, the stable graph
snapshot, revision digest, graph digest, and Trellis-to-manifest task ID mapping. These artifacts
are Wish Builder contracts and must not be presented as Trellis fields or APIs.

Projection back to Trellis is a separate, single-writer boundary. Accept only stable task-record
reads, compare the expected SHA-256 immediately before writing, verify SHA-256 and content after
writing, and fail closed on conflicts or unknown outcomes. Trellis `0.6.15` provides no
cross-process compare-and-swap (CAS). This procedure protects projection integrity; it is not a
dispatcher lock and does not qualify a backend. Backend workers never write Trellis, so backend
qualification is evaluated from backend evidence independently of projection CAS. Final dispatch
admission for active `wish_builder` dispatch requires an enabled backend/OS cell bound to the
pinned Trellis compatibility digest; it does not inspect projection CAS. Keep projection to one
writer unless a future Trellis release provides a separately qualified concurrent-write boundary.
The future `trellis` scheduler does not use those Agent backend/OS cells; it remains outside
manifest v2 until its own pre-launch admission, fencing, stop/reject, and concurrent-write
ownership integration are qualified.

The required order is:

```text
Gate A
  -> Trellis creates candidate tasks and dependencies
  -> Wish Builder imports and validates the Trellis graph
  -> Gate B approves the material graph projected from Trellis records plus derived revision, graph, and manifest digests
  -> the snapshot is locked and backend/OS qualification is checked; projection remains a separate single-writer path
```

Wish Builder may report missing dependencies, overlapping ownership, weak acceptance commands, or
tasks that are too broad. Trellis must make the corresponding task-graph edits. Wish Builder must
not create a parallel task set, task CRUD surface, dependency editor, task database, or board.

Create tasks using the installed Trellis scripts instead of writing `task.json` from scratch.
Common local commands are `task.py create`, `init-context`, `add-context`, `set-branch`,
`set-base-branch`, `validate`, `start`, and `archive`. Read `.trellis/workflow.md` and local
Trellis skill instructions for exact syntax.

If the installed Trellis CLI cannot write a structured `meta.wish_builder` object, create the task
with the CLI first, then patch only `task.json.meta.wish_builder`. Never hand-create or replace the
full `task.json`.

After Gate A, give Trellis the approved product and architecture documents and let it create the
candidate task graph. Import and validate that graph before Gate B. If validation fails, return
findings to Trellis, wait for updated task records, and re-import. Any material Trellis task or
dependency change after Gate B invalidates that gate and the execution snapshot; lifecycle-only
status or progress changes with the same canonical graph digest do not. Stop admission until the
revised graph is imported, compiled, and approved again.

## Scheduler And Worker Selection

Record two separate axes in Gate B. The active pairing rules below describe schema validity only;
backend/OS dispatch qualification is a separate contract. The Trellis-owned scheduler row records
a future boundary, not a value accepted by manifest v2.

- `scheduler_mode` selects and launches ready sibling tasks. Active manifest v2 accepts exactly
  `wish_builder` for the whole run.
- `worker_backend` implements one already-admitted leaf. Active manifest v2 accepts exactly `pi`,
  `oh_my_pi`, or `codex`.
- `trellis + trellis` is a future design boundary. Active manifest v2 cannot represent or execute
  it.

The active combination and deferred design boundary are:

| `scheduler_mode` | `worker_backend` | Schema-valid | Currently enabled | Dispatcher when qualified |
| --- | --- | --- | --- | --- |
| `trellis` | `trellis` | No; future schema only | No; pre-launch admission, identity, fencing, stop/reject behavior, and CAS are unqualified | Trellis |
| `trellis` | `pi`, `oh_my_pi`, or `codex` | No | No | None; reject Gate B |
| `wish_builder` | `trellis` | No | No | None; reject Gate B |
| `wish_builder` | `pi`, `oh_my_pi`, or `codex` | Yes | No; every bundled backend/OS cell lacks complete live evidence, so `enabledForDispatch=false` | Wish Builder |

The current Python control plane implements only `scheduler_mode=wish_builder`, but no backend is
yet qualified for live dispatch. It must return `dispatch_not_qualified` before launching a worker.
The `trellis + trellis` row is a future design boundary and is not schema-valid in active M1.

In a future `scheduler_mode=trellis`, Trellis schedules sibling tasks through its installed worktree or
multi-agent pipeline. Before launch, Trellis sends a task proposal and fresh dispatch ID. Wish
Builder validates Gate, graph digest, lease, readiness, ownership, and concurrency, durably records
admission, then returns the fencing identity that Trellis must attach to the launch and result. A
rejection or timeout means no launch; observation or cancellation after launch is insufficient.
Wish Builder never dispatches siblings from its own ready queue. `GraphIndex` is a
safety-validation and recovery index, not a dispatcher.

In `scheduler_mode=wish_builder`, Wish Builder schedules the selected Pi, Oh My Pi, or Codex
worker from the frozen graph and approved ready set. Trellis stores task context, lifecycle, and
progress but does not schedule sibling tasks. If no supervised parallel worker mechanism exists,
use this mode with concurrency one only after approval.

Admission is Wish Builder's decision that a proposed start matches the frozen graph and current
policy. Fencing rejects starts or results from stale leases, digests, or dispatch identities.
Locking means freezing the Gate B manifest and holding the durable scheduler lease; it does not
mean editing or locking Trellis's live database.

| Responsibility | Future `scheduler_mode=trellis` | Active `scheduler_mode=wish_builder` |
| --- | --- | --- |
| Create tasks and edit dependencies | Trellis | Trellis |
| Select ready sibling work | Trellis, checked by Wish Builder | Wish Builder from frozen `TaskDag` / `GraphIndex` |
| Launch sibling workers | Trellis | Wish Builder through Pi, Oh My Pi, or Codex |
| Hold task context and lifecycle state | Trellis | Trellis |
| Perform implementation | Trellis worker through the Trellis task lifecycle | Selected Pi / Oh My Pi / Codex worker through the Trellis task lifecycle |
| Write Gate/admission/fencing/recovery Journal | Wish Builder | Wish Builder |
| Schedule a retry | Trellis after Wish Builder admission | Wish Builder from the recomputed ready set |
| Independently validate acceptance and ownership | Wish Builder | Wish Builder |
| Admit squash merge | Wish Builder | Wish Builder |

Trellis Check/Finish is the task-local lifecycle record and evidence collection. Wish Builder's
result validation is an independent cross-task safety decision against the frozen acceptance,
ownership, dependency, Gate, and merge policy. One does not replace the other.

Do not invoke Trellis `/parallel` while Wish Builder or another coordinator dispatches the same
graph. Nested Implement and Check agents inside one Trellis leaf are allowed because they do not
schedule sibling tasks. A run cannot change either selection without stopping admissions,
recording a policy change, and repeating Gate B.

## Compatibility Rules

- Treat Trellis package compatibility and backend dispatch qualification as independent records.
  `wish_builder/compatibility/trellis-0.6.15.json` qualifies import and single-writer projection;
  `wish_builder/compatibility/backend-qualification-0.6.15.json` controls the exact backend/OS
  dispatch cells and references the exact Trellis compatibility digest used to create the frozen
  graph. Final dispatch admission requires both records to pass their own checks.
- For active `wish_builder` dispatch, keep each `enabledForDispatch` cell false until that backend/OS cell has complete live evidence
  and the evidence has been independently verified and published. Projection CAS is not a backend
  admission condition. Future `trellis + trellis` does not use an Agent backend/OS cell; it
  separately requires a later manifest schema, qualified pre-launch admission, fencing,
  stop/reject behavior, and concurrent-write ownership.
- Require exactly `@mindfoldhq/trellis@0.6.15` and
  `@mindfoldhq/trellis-core@0.6.15`; never resolve `@latest`.
- Resolve skill names from installed metadata; installations may prefix them with `gstack-`.
- Prefer local `.trellis/` scripts over globally remembered command syntax.
- Store integration fields under `task.json.meta.wish_builder`; do not invent unsupported
  top-level Trellis fields.
- Keep the manifest schema versioned. Reject a newer unsupported schema instead of guessing.
- Record tool versions, `scheduler_mode`, and `worker_backend` in the parent decision log so a
  resumed agent can reproduce the run.
