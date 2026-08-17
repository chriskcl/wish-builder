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

## Gate B

Gate B approves execution, not just a task list. Show:

- requirement-to-task coverage and explicit deferred items;
- the dependency graph and serial/parallel waves;
- every leaf's Issue/PR scope, ownership, acceptance, tests, risk, docs, and rollback;
- contract freeze and merge order;
- worker count and scheduler backend;
- external mutation, auto-merge, and deployment policy;
- escalation thresholds.

The initial wish, Gate A, or approval of an older packet does not count. Store approval evidence
and artifact hash. A material decomposition or policy change invalidates Gate B.

## Post-Approval Autonomy

The recommended guarded-autonomy policy permits the coordinator after Gate B to:

- create approved Issues, worktrees, branches, commits, and Draft PRs;
- dispatch and supervise bounded workers;
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

Repository docs and Trellis artifacts are the source of truth. Keep:

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
