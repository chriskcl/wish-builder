# Artifact Contracts

## Contents

1. Parent Task
2. Child Task
3. Execution Manifest
4. Leaf Readiness
5. Requirement Trace
6. Worker Result

## Parent Task

Use one Trellis parent task for the entire wish. Artifact presence and approval state are
phase-dependent:

```text
prd.md                       Candidate until Gate A; then approved requirements and non-goals
design.md                    Candidate until Gate A; then approved architecture and ADR summary
implement.md                 DAG, waves, integration and rollback plan
gate-a.md                    Exact product and architecture approval packet
gate-b.md                    Exact DAG and delivery-policy approval packet
execution-manifest.json      Machine-readable source for scheduling
decisions.md                 Gates, tool versions, policy and later decisions
traceability.md              Generated requirement delivery matrix
research/                    Raw gstack review reports and relevant research
gates/                       Immutable snapshots of approved gate packets
```

During Phase 1 require `prd.md`, `design.md`, `gate-a.md`, `decisions.md`, and `research/`. After
Gate A add `implement.md`, `execution-manifest.json`, child tasks, and `gate-b.md`. Generate
`traceability.md` during integration and finish.

Put a `wish_builder` object in the parent's `task.json.meta` containing the manifest path,
schema version, current phase, Gate evidence, and selected worker backend.

Treat `gate-a.md` and `gate-b.md` as the canonical active filenames. Do not rename them or invent
alternatives such as `gate-a-packet.md`. Before presenting a candidate, hash its exact UTF-8 bytes
with `wishctl.py hash <gate-file>` and record lowercase `sha256:<hex>` evidence with status
`pending`. Recompute after every edit. Approval freezes the same candidate bytes: promote that
hash to approval evidence and copy the file to `gates/gate-<letter>-<hex>.md`, omitting the
`sha256:` prefix from the Windows-safe snapshot filename. If a frozen packet changes materially,
replace the canonical candidate, clear its approval evidence, and return to that gate; never edit
the immutable snapshot.

## Child Task

Create one child task for every Issue/PR leaf. A child must contain:

- a focused `prd.md` with requirement IDs and observable acceptance criteria;
- `design.md` for local design details without redefining approved architecture;
- `implement.md` with ordered work, tests, risky files, docs, and rollback;
- real curated entries in `implement.jsonl` and `check.jsonl`;
- a `wish_builder` metadata object with task ID, Issue ID, dependencies, owned paths, wave,
  risk, branch, and PR ID;
- an optional `result.json` written by a worker when the backend lacks structured completion.

## Execution Manifest

Use JSON so the bundled validator works without third-party packages:

```json
{
  "schema_version": 1,
  "run_id": "WISH-2026-001",
  "goal": "Observable product outcome",
  "base_branch": "main",
  "max_concurrency": 3,
  "protected_paths": ["db/schema/**", "src/contracts/**"],
  "approved": {
    "gate_a": {"approved_by": null, "approved_at": null, "artifact_hash": null},
    "gate_b": {"approved_by": null, "approved_at": null, "artifact_hash": null}
  },
  "requirements": [
    {"id": "REQ-001", "text": "User-visible outcome", "status": "approved"}
  ],
  "tasks": [
    {
      "id": "TASK-001",
      "title": "Freeze shared contract",
      "requirement_ids": ["REQ-001"],
      "depends_on": [],
      "owned_paths": ["src/contracts/**"],
      "allowed_auxiliary_paths": [".trellis/tasks/01-01-contract/**"],
      "acceptance_criteria": ["Contract test passes"],
      "regression_commands": ["npm test -- contract"],
      "rollback": "Revert the squash commit",
      "documentation": ["docs/contracts.md"],
      "wave": 0,
      "risk": "medium",
      "may_change_contracts": true,
      "issue_id": null,
      "branch": null,
      "pr_id": null,
      "squash_commit": null,
      "agent_owner": null,
      "status": "approved"
    }
  ]
}
```

Allowed requirement statuses are `approved`, `implemented`, `deferred`, and `out_of_scope`.
Allowed task statuses are `proposed`, `approved`, `ready`, `dispatched`, `pr_open`, `merged`,
`verified`, `archived`, `blocked`, and `failed`.

Gate artifact hashes use `sha256:<64 lowercase hexadecimal characters>`. After merge, preserve
the unique PR ID and squash commit in the task record.

## Leaf Readiness

A leaf is ready only when:

- Gate A and Gate B evidence is present;
- it maps to at least one approved requirement;
- all dependencies are merged, verified, or archived;
- it has one Issue ID and branch;
- owned paths, acceptance criteria, regression commands, risk, and rollback are explicit;
- it does not overlap an active sibling's ownership;
- it does not change a frozen public contract unless Wave 0 and explicitly authorized.

Wave 0 and Wave 2 must be totally ordered by dependencies. Wave 1 siblings may be parallel only
when neither depends on the other and their owned paths do not overlap.

## Requirement Trace

Use stable `REQ-NNN` and `TASK-NNN` IDs. Preserve this chain:

```text
Requirement -> Trellis child -> Issue -> PR -> tests -> squash commit -> status
```

Do not mark a requirement `implemented` merely because code exists. All mapped tasks must be
merged or later, and acceptance evidence must pass. Deferred and out-of-scope requirements must
include a decision-log entry.

## Worker Result

When the worker backend has no structured lifecycle message, require `result.json`:

```json
{
  "task_id": "TASK-001",
  "outcome": "success",
  "summary": "What changed and why",
  "files_modified": ["src/contracts/api.ts"],
  "commands_run": [{"command": "npm test -- contract", "exit_code": 0}],
  "commit": "optional-hash",
  "pr_id": "optional-id",
  "remaining": [],
  "risks": []
}
```

Accept completion only from the assigned worker and matching task/dispatch identity. A review-only
worker reports findings; it does not gain permission to edit or merge.
