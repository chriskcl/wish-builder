# Gates, Autonomy, And Delivery Policy

## Contents

1. Gate Setup
2. Gate A
3. Gate B
4. Post-Approval Autonomy
5. Escalation
6. Issue And PR Rules
7. Documentation

## Gate Setup

Ask once before installing global dependencies, initializing Trellis, authenticating a provider,
creating a remote project, or changing repository settings. Show exact targets and commands.
Installation consent does not approve product work or external delivery.

An explicit Wish Builder request authorizes local planning artifacts when Git and Trellis are
already initialized. A `local-only` policy permits reading installed skills and tools outside the
repository, but forbids network or provider access and all other writes outside the repository
unless explicitly authorized.

## Gate A

Gate A is a human architecture decision, not an informational status update. The packet must
contain the final product promise, target user, outcomes, scope and non-goals, architecture,
public contracts, data and migration design, security boundaries, alternatives, risks, UI
direction when relevant, and unresolved decisions.

Accept only an explicit approval response to the latest packet. Store approver, timestamp, and a
stable artifact hash. Approval with edits requires applying the edits, regenerating the packet,
and confirming that no new unresolved choice appeared.

Invalidate Gate A when the product promise, target user, architecture boundary, public contract,
data ownership, security model, or irreversible migration strategy changes.

Only after Gate A may Trellis turn the approved product and architecture documents into candidate
tasks and dependencies. Candidate task preparation is not permission to implement or dispatch.

## Gate B

Gate B approves execution, not just a task list. Show:

- requirement-to-task coverage and explicit deferred items;
- the imported Trellis parent task ID and the stable task-record input used to project the material
  graph, plus the Wish Builder-derived revision provenance and canonical graph digest;
- the dependency graph, serial/parallel waves, and exact execution-manifest digest;
- every leaf's Issue/PR scope, ownership, acceptance, tests, risk, docs, and rollback;
- contract freeze and merge order;
- worker count, the mutually exclusive `scheduler_mode`, its allowed `worker_backend`,
  `lease_ttl_seconds`, and `lease_clock_skew_seconds`;
- external mutation, auto-merge, and deployment policy;
- escalation thresholds.

The initial wish, Gate A, or approval of an older packet does not count. Store approval evidence
and artifact hash. A material Trellis task-graph or policy change invalidates Gate B. Stop new
admissions, import and validate the newly projected material graph, compile a new manifest, and
obtain fresh approval. Trellis lifecycle progress that leaves the canonical graph digest unchanged
does not invalidate Gate B.

## Post-Approval Autonomy

The recommended guarded-autonomy policy permits the coordinator after Gate B to:

- create approved Issues, worktrees, branches, commits, and Draft PRs;
- admit and supervise bounded workers through the one scheduler mode and worker backend selected
  at Gate B;
- run tests, reviews, QA, documentation, and bounded repairs;
- squash-merge low- and medium-risk PRs when required evidence is green;
- create repair Issues that stay inside approved scope and architecture.

It does not permit the coordinator to:

- request, expose, rotate, or store credentials without approval;
- charge money, change billing, or contact external people;
- deploy to production unless Gate B or Gate C names the target and rollback policy;
- delete production data, weaken permissions, or perform irreversible migrations;
- change product promise, target user, frozen architecture, or public contracts;
- expand effort beyond the approved drift threshold.

Record repository-specific changes to this policy in `decisions.md`.

Active M1 requires `scheduler_mode=wish_builder` with `worker_backend=pi`, `oh_my_pi`, or `codex`:
Wish Builder dispatches that backend from the frozen graph while Trellis stores task context and
progress. Reject every other pair. A future schema may add `trellis + trellis`, where Trellis
dispatches siblings and Wish Builder validates admission, fencing, results, and merge policy, but
that path is not representable or executable in M1. Changing either field is a material Gate B
policy change.

## Escalation

Interrupt the human only when at least one condition holds:

- target user, product promise, or observable acceptance behavior must change;
- estimated scope grows to roughly twice the Gate B baseline;
- approved architecture, public contract, data ownership, or security boundary must change;
- credentials, payments, production deployment, deletion, or permission changes are required;
- an irreversible migration or equally valid brand-defining design choice appears;
- three consecutive task attempts fail, or two repair loops show no improvement;
- a legal, compliance, or security-sensitive choice cannot be verified.

Do not escalate ordinary implementation choices, discoverable repository facts, formatting,
routine test failures, or choices already covered by the policy. Give an escalation packet with
status, evidence, attempts, recommendation, alternatives, and downstream impact.

## Issue And PR Rules

- Map exactly one leaf task to one Issue and one PR.
- Include task and requirement IDs in both artifacts.
- Open Draft PRs early enough for CI and visibility, but merge only after completion.
- Squash merge so each leaf has one mainline rollback unit.
- Merge only in dependency order and keep the base branch green.
- Make migrations serial, backward compatible where possible, and separately rollbackable.
- Route conflicts and regressions to the owning task; do not hide unrelated fixes.
- Create a parent integration/release Issue only when it represents real, independently testable
  work such as a release toggle or cross-module E2E harness.

## Documentation

Repository docs and Trellis artifacts are the planning source of truth. Before Gate B, the
editable Trellis task graph is the task source of truth. After Gate B, the approved immutable
execution manifest is the admission baseline and the Journal plus Trellis lifecycle state are the
runtime source of truth. Keep:

- implementation and interaction behavior in repository documentation;
- requirements and acceptance in `prd.md`;
- architecture and ADRs in `design.md` and repository architecture docs;
- task execution and test evidence in Trellis child artifacts;
- decisions and gate evidence in `decisions.md`;
- delivery status in the generated traceability matrix;
- session learnings in Trellis journals and stable standards in `.trellis/spec/`.

An LLM wiki may index these artifacts, but it must not become the only source. Mark a requirement
implemented only after merge and acceptance evidence, then archive Trellis tasks without deleting
their trace.
