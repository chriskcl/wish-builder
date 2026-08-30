# Artifact Contracts

## Contents

1. Parent Task
2. Child Task
3. Compatibility And Qualification
4. Execution Manifest
5. Leaf Readiness
6. Requirement Trace
7. Worker Result

## Parent Task

Use one Trellis parent task for the entire wish. Artifact presence and approval state are
phase-dependent:

```text
prd.md                       Candidate until Gate A; then approved requirements and non-goals
design.md                    Candidate until Gate A; then approved architecture and ADR summary
implement.md                 Trellis-authored candidate DAG, waves, integration and rollback plan
gate-a.md                    Exact product and architecture approval packet
gate-b.md                    Material-graph approval, provenance, manifest digest and delivery-policy packet
execution-manifest.json      Immutable Wish Builder snapshot derived from the approved Trellis task graph
decisions.md                 Gates, tool versions, policy and later decisions
traceability.md              Generated requirement delivery matrix
research/                    Raw gstack review reports and relevant research
gates/                       Immutable snapshots of approved gate packets
```

During Phase 1 require `prd.md`, `design.md`, `gate-a.md`, `decisions.md`, and `research/`. After
Gate A, Trellis creates the candidate child tasks, dependencies, and `implement.md`. Wish Builder
then imports and validates that graph, compiles `execution-manifest.json`, and prepares
`gate-b.md`. Gate B freezes the approved snapshot. Generate `traceability.md` during integration
and finish.

Put a `wish_builder` object in the parent's `task.json.meta` containing the manifest path,
schema version, current phase, Gate evidence, `scheduler_mode`, and `worker_backend`.

Treat `gate-a.md` and `gate-b.md` as the canonical active filenames. Do not rename them or invent
alternatives such as `gate-a-packet.md`. Before presenting a candidate, hash its exact UTF-8 bytes
with `wishctl.py hash <gate-file>` and record lowercase `sha256:<hex>` evidence with status
`pending`. Recompute after every edit. Approval freezes the same candidate bytes: promote that
hash to approval evidence and copy the file to `gates/gate-<letter>-<hex>.md`, omitting the
`sha256:` prefix from the Windows-safe snapshot filename. If a frozen packet changes materially,
replace the canonical candidate, clear its approval evidence, and return to that gate; never edit
the immutable snapshot.

## Child Task

Trellis creates one child task for every Issue/PR leaf. Wish Builder validates these children but
does not maintain a second task set. A child must contain:

- a focused `prd.md` with requirement IDs and observable acceptance criteria;
- `design.md` for local design details without redefining approved architecture;
- `implement.md` with ordered work, tests, risky files, docs, and rollback;
- real curated entries in `implement.jsonl` and `check.jsonl`;
- a `wish_builder` metadata object with task ID, Issue ID, dependencies, owned paths, wave,
  risk, branch, and PR ID;
- an optional `result.json` written by a worker when the backend lacks structured completion.

## Compatibility And Qualification

Trellis compatibility and worker-backend qualification are separate contracts. Passing the first
permits the supported import and projection operations. It never enables an Agent dispatch cell.

### Trellis Compatibility

`wish_builder/compatibility/trellis-0.6.15.json` is the generated, immutable compatibility record
for exactly these official packages:

- `@mindfoldhq/trellis@0.6.15`;
- `@mindfoldhq/trellis-core@0.6.15`.

Never install or resolve either package through `@latest`. Trellis `0.7.0-dev.2` was a local test
fixture later withdrawn from Wish Builder; it was never an official Trellis release and is not
supported. The compatibility record is not a task database and is never edited during a run. It
binds the CLI and core tarball names, exact versions, sizes, SHA-256
values, supported task-record operations, adapter contract, and digest of the complete record.

Wish Builder distributes a compiled digest pin alongside this JSON and requires canonical byte
parity before trusting it. Duplicate keys, unknown fields, wrong primitive types, unsupported enum
values, package drift, and digest drift are compatibility failures. A coherently rehashed
replacement still fails when it no longer matches the compiled pin.

Official Trellis `0.6.15` exposes task-record operations, including the Core
`loadTaskRecord`/`writeTaskRecord` boundary. It does not expose Wish Builder's graph snapshot
format, graph-projection schema, revision digest, graph digest, task-ID mapping, or immutable
execution manifest. Those are versioned Wish Builder-derived contracts. Trellis `0.6.15` also has
no cross-process compare-and-swap (CAS) operation. The M1 projection protocol therefore permits
one projection writer, requires stable reads plus SHA-256 verification before and after each
write, and fails closed on conflicts or unknown outcomes. This permits import/projection only; the
single writer is not a dispatcher and does not qualify an Agent. Backend workers still write only
their Git worktrees and the Journal. M1 worker dispatch admission is independent of projection CAS
and uses the separately published backend/OS qualification record. The future `trellis + trellis`
scheduler path is not represented by active manifest v2 and remains deferred until a later schema
plus its pre-launch admission, fencing, stop/reject, and concurrent-write ownership integration are
qualified.

### Backend Qualification

`wish_builder/compatibility/backend-qualification-0.6.15.json` is the separate immutable record
for the closed Codex, Pi, and Oh My Pi provider set, Windows and Linux launch profiles, SDK pins,
capabilities, qualification evidence, the single-scheduler policy, and their digests. Trellis
compatibility and backend-cell admission remain independent decisions.

`enabledForDispatch=true` is valid only with `status=passed`, `live=true`, and
`evidenceScope=full_turn_and_cancellation`. Startup/handshake, deterministic fixture,
credential-blocked, and CI-pending evidence may document capability progress but cannot authorize
worker dispatch. The bundled M1 record enables only locally published `Codex / Windows`, with a
maximum concurrency of two. That publication preserves human-accepted detached provider
provenance and explicitly does not claim an OpenAI-signed attestation. Import and projection
success must not be treated as dispatch admission. Every additional backend cell requires the
full dispatch evidence above. For `scheduler_mode=wish_builder`, final admission binds that exact cell
to the approved manifest, frozen Trellis compatibility digest, Journal lease, and fencing identity.
It does not inspect projection CAS or concurrent-writer capability. A future
`scheduler_mode=trellis` does not use an Agent backend/OS cell; before a later schema can admit it,
require a separately qualified pre-launch proposal, admission, identity, fencing, stop/reject
contract, and concurrent-write ownership. Neither scheduler path may treat projection digest
checks as a dispatch lock.

An enabled cell must also be built with a content-addressed evidence root. Every harness,
full-turn, active-cancellation, crash-reconcile, cleanup, and optional sibling-overlap digest must
resolve to canonical JSON bytes whose SHA-256 matches the declared digest. The builder checks that
the records agree on provider, platform, qualification run, harness, package pins, SDK pin, policy,
launch profile, and capability. For parallel qualification, it derives simultaneous turn count and
owned-path disjointness from the recorded sibling intervals and concrete repository-relative paths;
caller-supplied booleans or dangling digest strings are not evidence. Keep the raw evidence records
with the qualification release inputs even though the distributed qualification record contains
only their content digests.

## Execution Manifest

`execution-manifest.json` is a deterministic Wish Builder projection derived from stable Trellis
task-record reads. Trellis remains the editable source; the manifest is never a task-authoring
surface and is never projected back into Trellis. The graph snapshot format, revision digest,
graph-projection schema, graph digest, and task-ID mapping are Wish Builder contracts, not official
Trellis `0.6.15` APIs. Use JSON so the bundled validator works without third-party packages:

```json
{
  "schema_version": 2,
  "graph_projection_version": 1,
  "run_id": "WISH-2026-001",
  "goal": "Observable product outcome",
  "base_branch": "main",
  "trellis_parent_task_id": "2026-08-18-wish-001",
  "trellis_revision": "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "trellis_graph_digest": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "task_id_mapping": {
    "2026-08-18-contract": "TASK-001"
  },
  "imported_at": "2026-08-18T04:00:00Z",
  "provider": "codex",
  "capability_digest": "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "launch_profile_digest": "sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
  "policy_digest": "sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
  "scheduler_mode": "wish_builder",
  "execution_budget": {
    "max_attempts_per_task": 2,
    "max_attempts_per_run": 4,
    "attempt_deadline_seconds": 1800,
    "total_worker_seconds": 7200,
    "max_output_bytes": 8388608,
    "max_retained_evidence_bytes": 16777216,
    "max_concurrent_workers": 1,
    "billing_posture": "preapproved"
  },
  "max_concurrency": 1,
  "lease_ttl_seconds": 90,
  "lease_clock_skew_seconds": 2,
  "path_case_mode": "sensitive",
  "protected_paths": ["db/schema/**", "src/contracts/**"],
  "approved": {
    "gate_a": {
      "approved_by": "approver-id",
      "approved_at": "2026-08-18T03:00:00Z",
      "artifact_hash": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    },
    "gate_b": {"approved_by": null, "approved_at": null, "artifact_hash": null}
  },
  "requirements": [
    {
      "id": "REQ-001",
      "text": "User-visible outcome",
      "status": "approved",
      "decision_ref": null
    }
  ],
  "tasks": [
    {
      "id": "TASK-001",
      "title": "Freeze shared contract",
      "requirement_ids": ["REQ-001"],
      "depends_on": [],
      "owned_paths": ["src/contracts/**"],
      "allowed_auxiliary_paths": [
        ".trellis/tasks/01-01-contract/**",
        "docs/contracts.md"
      ],
      "acceptance_criteria": ["Contract test passes"],
      "regression_commands": [
        {
          "executable_profile": "python",
          "executable_identity_digest": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
          "argv": ["python", "-m", "unittest", "tests.contracts"],
          "working_directory": ".",
          "timeout_seconds": 120,
          "stdout_limit_bytes": 1048576,
          "stderr_limit_bytes": 1048576,
          "result_limit_bytes": 262144,
          "environment_allowlist": ["PATH", "PYTHONUTF8"],
          "network_policy": "denied",
          "display_text": "Run contract tests"
        }
      ],
      "rollback": "Revert the squash commit",
      "documentation": ["docs/contracts.md"],
      "wave": 0,
      "risk": "medium",
      "may_change_contracts": true,
      "instruction_context_digest": "sha256:2222222222222222222222222222222222222222222222222222222222222222",
      "approved_document_digests": [
        "sha256:3333333333333333333333333333333333333333333333333333333333333333"
      ],
      "task_packet_template_digest": null
    }
  ]
}
```

### Versioned Projection Schema

Schema v2 uses the following complete root contract. `Required` means there is no implicit
default. `Graph` marks fields serialized into the graph projection before calculating
`trellis_graph_digest`. `Manifest` marks fields present in the complete canonical manifest and
therefore covered by its digest.

| Root field | Type / allowed value | Default | Canonical order | Graph | Manifest |
| --- | --- | --- | --- | --- | --- |
| `schema_version` | integer `2` | Required | N/A | No | Yes |
| `graph_projection_version` | integer `1` | Required | N/A | Yes | Yes |
| `run_id` | non-empty NFC string | Required | N/A | No | Yes |
| `goal` | non-empty NFC string | Required | N/A | No | Yes |
| `base_branch` | non-empty NFC string | Required | N/A | No | Yes |
| `trellis_parent_task_id` | non-empty NFC string | Required | N/A | Yes | Yes |
| `trellis_revision` | Wish Builder-derived `sha256:<64 lowercase hex>` for Trellis `0.6.15`, or `null` | `null` only when unavailable | N/A | No | Yes |
| `trellis_graph_digest` | `sha256:<64 lowercase hex>` | Derived, required | N/A | No; it is the result | Yes |
| `task_id_mapping` | object: normalized Trellis ID -> `TASK-NNN` | Required | Keys by normalized UTF-8 bytes | Yes | Yes |
| `imported_at` | UTC RFC 3339 string | Caller-supplied, required | N/A | No | Yes |
| `provider` | selected worker backend: `pi`, `oh_my_pi`, or `codex` | Required | N/A | No | Yes |
| `capability_digest` | `sha256:<64 lowercase hex>` | Required | N/A | No | Yes |
| `launch_profile_digest` | `sha256:<64 lowercase hex>` | Required | N/A | No | Yes |
| `policy_digest` | `sha256:<64 lowercase hex>` | Required | N/A | No | Yes |
| `scheduler_mode` | active M1 fixed value `wish_builder` | Required | N/A | No | Yes |
| `execution_budget` | closed execution-budget object | Required | Object keys sorted | No | Yes |
| `max_concurrency` | positive integer | Required | N/A | No | Yes |
| `lease_ttl_seconds` | integer from `30` through `3600` | Required | N/A | No | Yes |
| `lease_clock_skew_seconds` | non-negative integer less than one-quarter TTL | Required | N/A | No | Yes |
| `path_case_mode` | `sensitive` or `insensitive` | Required from target worktree policy | N/A | No | Yes |
| `protected_paths` | list of canonical path patterns | `[]` | Normalized UTF-8 byte order | No | Yes |
| `approved` | Gate evidence object | Required | Object keys sorted | No | Yes |
| `requirements` | non-empty list of requirement objects | Required | Numeric `REQ-NNN` order | Yes | Yes |
| `tasks` | non-empty list of task objects | Required | Numeric `TASK-NNN` order | Yes | Yes |

`max_concurrency` must equal `execution_budget.max_concurrent_workers`. The execution-budget
object has exactly these required fields: positive integers `max_attempts_per_task`,
`max_attempts_per_run`, `attempt_deadline_seconds`, `total_worker_seconds`, `max_output_bytes`,
`max_retained_evidence_bytes`, and `max_concurrent_workers`, plus `billing_posture` set to
`preapproved`, `unmetered`, or `operator_required`. `max_attempts_per_run` must be at least
`max_attempts_per_task`.

The `approved` object has exactly these digest-covered fields:

| Approval field | Type / fixed value | Default | Manifest digest |
| --- | --- | --- | --- |
| `gate_a.approved_by` | non-empty NFC string | Required | Yes |
| `gate_a.approved_at` | UTC RFC 3339 string | Required | Yes |
| `gate_a.artifact_hash` | `sha256:<64 lowercase hex>` | Required | Yes |
| `gate_b.approved_by` | `null` | Fixed `null` | Yes |
| `gate_b.approved_at` | `null` | Fixed `null` | Yes |
| `gate_b.artifact_hash` | `null` | Fixed `null` | Yes |

Gate B fields stay `null` in the immutable manifest because Gate B approves the manifest itself.
Store Gate B approval evidence in `gate-b.md` plus the Journal, both referring to the manifest
digest; never inject that evidence back into the approved manifest.

Each requirement object has exactly these fields:

| Requirement field | Type / allowed value | Default | Canonical order | Graph | Manifest |
| --- | --- | --- | --- | --- | --- |
| `id` | unique `REQ-NNN` from approved Gate A documents | Required | Numeric suffix | Yes | Yes |
| `text` | non-empty NFC string | Required | N/A | Yes | Yes |
| `status` | `approved`, `deferred`, or `out_of_scope` | `approved` | N/A | Yes | Yes |
| `decision_ref` | non-empty decision-log reference or `null` | `null`; required for deferred/out-of-scope | N/A | Yes | Yes |

Each task object has exactly these fields:

| Task field | Type / allowed value | Default | Canonical order | Graph | Manifest |
| --- | --- | --- | --- | --- | --- |
| `id` | unique derived `TASK-NNN` | Required | Numeric suffix | Yes | Yes |
| `title` | non-empty NFC string | Required | N/A | Yes | Yes |
| `requirement_ids` | non-empty list of `REQ-NNN` | Required | Numeric suffix | Yes | Yes |
| `depends_on` | list of `TASK-NNN` | `[]` | Numeric suffix | Yes | Yes |
| `owned_paths` | non-empty list of canonical path patterns | Required | Normalized UTF-8 bytes | Yes | Yes |
| `allowed_auxiliary_paths` | list of canonical path patterns | `[]` | Normalized UTF-8 bytes | Yes | Yes |
| `acceptance_criteria` | non-empty list of NFC strings | Required | Preserve declared order | Yes | Yes |
| `regression_commands` | non-empty list of closed command objects | Required | Preserve declared order | Yes | Yes |
| `rollback` | non-empty NFC string | Required | N/A | Yes | Yes |
| `documentation` | list of canonical repository paths | `[]` | Normalized UTF-8 bytes | Yes | Yes |
| `wave` | Trellis-authored integer `0`, `1`, or `2` | Required | N/A | Yes | Yes |
| `risk` | `low`, `medium`, or `high` | Required | N/A | Yes | Yes |
| `may_change_contracts` | boolean | `false` | N/A | Yes | Yes |
| `instruction_context_digest` | `sha256:<64 lowercase hex>` or `null` | See worker-input rule below | N/A | Yes | Yes |
| `approved_document_digests` | list of `sha256:<64 lowercase hex>` | See worker-input rule below | Digest order | Yes | Yes |
| `task_packet_template_digest` | `sha256:<64 lowercase hex>` or `null` | See worker-input rule below | N/A | Yes | Yes |

Each task freezes worker input in exactly one form: either a non-null
`instruction_context_digest` plus a non-empty `approved_document_digests` list and a null
`task_packet_template_digest`, or one non-null `task_packet_template_digest` with the other two
forms empty. Issue, branch, PR, commit, worker-owner, and lifecycle status fields are runtime state;
the closed manifest v2 decoder rejects them.

Each command object has exactly these required fields: `executable_profile`,
`executable_identity_digest`, non-empty `argv`, repository-relative `working_directory`, positive
`timeout_seconds`, positive `stdout_limit_bytes`, `stderr_limit_bytes`, and `result_limit_bytes`,
`environment_allowlist`, `network_policy` (`denied`, `loopback_only`, or `allowed`), and
`display_text`. Commands are executed without a shell through the approved executable profile.

For the Wish Builder-admitted official Trellis `0.6.15` baseline, `trellis_revision` records the
revision digest derived by Wish Builder from the accepted stable task-record snapshot. It is
provenance, not an official Trellis revision token, cross-process CAS value, or material graph
identity. A future adapter may store an official versioned token only when that exact Trellis
version defines one. Use `trellis_graph_digest` as the sole material graph identity and never
substitute it for a missing revision value.

### Deterministic Snapshot IDs

Keep requirement IDs from the approved Gate A documents; reject missing, malformed, or duplicate
IDs rather than inventing requirements. Assign task IDs deterministically:

1. Decode each Trellis task ID as strict UTF-8, normalize it to NFC, and reject empty IDs,
   disallowed controls, or duplicates after normalization.
2. Sort the normalized IDs by unsigned lexicographic UTF-8 byte order, with no locale or
   case-folding.
3. Assign one-based ordinals in that order as `TASK-001`, `TASK-002`, and so on. Use at least
   three digits; `TASK-1000` follows `TASK-999` without renumbering earlier widths.
4. Replace dependency references with mapped snapshot IDs. Serialize mapping keys in the same
   UTF-8 byte order and verify the mapping is bijective.
5. Import Trellis's semantic `wave`: `0` is shared foundations/contracts, `1` is independently
   parallelizable modules, and `2` is integration/release work. Require every dependency to be in
   the same or an earlier wave, every Wave 1/2 task to transitively depend on every task in its
   immediately preceding wave when that wave is non-empty, every pair in Wave 0 and Wave 2 to be dependency-ordered,
   and every parallel Wave 1 pair to have disjoint writable sets. `TaskDag.compile` consumes this
   validated field; Wish Builder does not re-author it.

Adding, removing, or renaming a Trellis task can renumber later snapshot IDs. That is a material
graph change and correctly requires a new Gate B.

### Repository Path Rules

Normalize every path before overlap or ownership validation:

1. Decode strict UTF-8, normalize to NFC, and use `/` as the only separator. Reject `\`, NUL,
   controls, empty segments, `.`, `..`, a leading `/`, drive prefixes such as `C:`, UNC prefixes,
   URI schemes, and any colon-bearing segment. A directory pattern may end in exactly `/**`;
   otherwise reject glob metacharacters and trailing `/`.
2. Interpret the result relative to the recorded Git worktree root. Resolve each existing parent
   without following a repository symlink or gitlink. Reject a symlink/gitlink prefix, a resolved
   parent outside the worktree, or a target that cannot be proven contained. Recheck containment at
   dispatch and result validation to catch replacement races.
3. Set `path_case_mode` from the target integration worktree during preflight. In `sensitive` mode,
   compare NFC path segments exactly. In `insensitive` mode, compare with the adapter version's
   pinned Unicode default case-fold table. Reject two spellings that collide under the selected
   mode.
4. Define a task's complete writable set as `owned_paths + allowed_auxiliary_paths`.
   `documentation` entries must already be covered by that set and do not grant extra write
   permission. Use the complete writable set for concurrent-overlap checks. Exclude only the
   task's unique Trellis artifact directory after verifying no sibling can reference it.

### Canonical Projection And Hashes

Normalize the imported graph before hashing or compiling:

1. Decode text as strict UTF-8, normalize strings to NFC, normalize line endings to LF, reject
   disallowed control characters, and reject floats.
2. Apply the snapshot-ID algorithm above. Sort requirements and tasks by the numeric suffix of
   their snapshot ID. Sort set-like IDs and paths by the rules in the schema tables. Preserve the
   declared order of acceptance criteria and regression commands.
3. Emit every schema field. Normalize a missing optional value to its documented `null`, empty
   list, or scalar default before serialization; never let input object ordering affect bytes.
4. Serialize JSON as UTF-8 without BOM. Sort every object member by unsigned lexicographic UTF-8
   bytes of its NFC key. Emit no spaces; use exactly `,` and `:` separators. Emit non-ASCII
   characters as raw UTF-8. Escape only `"`, `\`, and U+0000 through U+001F, using the short JSON
   escape where one exists and lowercase `\u00xx` otherwise. Reject lone surrogates. Emit integers
   in shortest decimal form with no leading zero, reject floats, and append exactly one LF after
   the root value. Include the graph-projection schema marker so a rule change changes the digest.
5. Compute SHA-256 over those exact bytes and render lowercase `sha256:<hex>`.

The graph projection contains only the root, requirement, and task fields marked `Graph` in the
tables above. Its version marker makes any future projection-rule change alter the digest. It
excludes `trellis_revision`, `imported_at`, approval evidence, scheduler/worker policy, lifecycle
progress, journals, history, Issue and PR state, worker identity, commits, and archive timestamps.

The exact manifest digest is a separate SHA-256 over the complete canonical manifest bytes,
including `trellis_revision`, `imported_at`, scheduler/worker policy, and the stored graph digest. The
projector receives `imported_at` as an explicit input; for the same normalized graph, ID mapping,
policy, revision, and explicit import time, it must produce byte-identical manifest bytes.

### Import Validation

Fail closed before Gate B when the adapter sees a missing or duplicate task ID, non-bijective ID
mapping, missing dependency, self-dependency, cycle, missing owned path, missing acceptance or
regression command, invalid wave/risk/rollback data, orphan coverage, overlapping parallel
ownership, or an unsupported task-shaping field. Use these exact definitions:

- **Orphan task:** a task with no `approved` requirement ID. Infrastructure and integration work
  still needs an approved non-functional requirement.
- **Orphan requirement:** an `approved` requirement with no task. A `deferred` or `out_of_scope`
  requirement is not orphaned only when `decision_ref` is present.
- **Ownership overlap:** two tasks that may run concurrently have intersecting complete writable
  sets under the repository path rules above. Equal paths, a literal under a directory prefix, or
  ancestor prefixes intersect. Reject unsupported syntax or any intersection the adapter cannot
  decide.
- **Task-shaping field:** any Trellis field that can alter membership, requirement mapping,
  dependencies, ownership, acceptance, regression commands, rollback, documentation, wave, risk,
  or contract permission. Map every recognized field explicitly. Ignore only a versioned allowlist
  of lifecycle, history, and presentation fields; reject every unknown task-shaping field.

Reject an unsupported or incomplete Wish Builder snapshot version or Trellis task-record shape
instead of guessing. Active manifest v2 rejects every scheduler/worker pair except
`wish_builder + pi|oh_my_pi|codex`, as defined in `tool-bridges.md`. Future `trellis + trellis`
requires a later schema and is not executable in M1. Schema validation is not dispatch
authorization: backend qualification is checked separately. The bundled M1 record enables only
locally published `Codex / Windows`, at concurrency one or two.

Gate B approves the material graph projected from a stable Trellis task-record read,
`trellis_parent_task_id`, the Wish Builder-derived `trellis_revision` provenance,
`trellis_graph_digest`, the exact manifest digest, `scheduler_mode`, and `worker_backend` as one
packet. After approval, the manifest is
immutable. Runtime Issue, branch, worker, PR, merge, and task-state changes are appended to the
Journal and reflected in Trellis; they do not rewrite the manifest. A derived task-record revision
change must be recorded as new provenance and trigger a complete stable read. Drift checks compare
the recomputed material graph digest, not the revision. If the graph digest changes, the change is
material: stop new admissions, invalidate Gate B automatically, re-import, recompile, and obtain a
new Gate B approval. A progress-only revision with the same graph digest neither invalidates Gate B
nor regenerates the approved manifest.

Allowed frozen-manifest requirement statuses are `approved`, `deferred`, and `out_of_scope`.
`implemented` is a traceability/runtime status and is never written back into the immutable
manifest. The candidate manifest may contain `proposed` tasks; every task is `approved` in the
frozen manifest. Later lifecycle states belong to the Journal/runtime projection and Trellis.

Gate artifact hashes use `sha256:<64 lowercase hexadecimal characters>`. After merge, preserve
the unique PR ID and squash commit in Trellis plus the Journal/runtime projection, linked through
`task_id_mapping`.

## Leaf Readiness

A leaf is ready only when:

- Gate A and Gate B evidence is present;
- it maps to at least one approved requirement;
- all dependencies are merged, verified, or archived;
- it has one Issue ID and branch in the admitted Trellis/runtime mapping;
- owned paths, acceptance criteria, regression commands, risk, and rollback are explicit;
- it does not overlap an active sibling's ownership;
- it does not change a frozen public contract unless Wave 0 and explicitly authorized.

Wave 0 and Wave 2 must be totally ordered by dependencies. Wave 1 siblings may be parallel only
when neither depends on the other and their owned paths do not overlap. With active
`scheduler_mode=wish_builder`, this is the dispatch ready set. In the future Trellis-owned mode,
the same result may validate and fence Trellis dispatches, but active manifest v2 cannot represent
or dispatch that mode.

## Requirement Trace

Use stable `REQ-NNN` and `TASK-NNN` IDs. Preserve this chain:

```text
Requirement -> Trellis child -> snapshot task ID -> Issue -> PR -> tests -> squash commit -> status
```

Do not mark a requirement `implemented` merely because code exists. All mapped tasks must be
merged or later, and acceptance evidence must pass. Deferred and out-of-scope requirements must
include a decision-log entry.

## Worker Result

When the worker backend has no structured lifecycle message, require `result.json`:

```json
{
  "run_id": "WISH-2026-001",
  "task_id": "TASK-001",
  "trellis_graph_digest": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "scheduler_mode": "wish_builder",
  "fencing_token": 7,
  "dispatch_id": "dispatch-0007-task-001",
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

Accept completion only from the assigned worker and an exact match on `(run_id, task_id,
trellis_graph_digest, scheduler_mode, fencing_token, dispatch_id)`. Resolve the admission event to
verify its manifest digest and worker backend before accepting evidence. A review-only worker
reports findings; it does not gain permission to edit or merge.
