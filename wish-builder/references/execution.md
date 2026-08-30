# Execution And Recovery

## Contents

1. State Machine
2. Compatibility And Qualification Gates
3. Import Consistency
4. Durable Lease Protocol
5. Scheduler And Admission Loop
6. Worker Contract
7. Integration
8. Quality Layers
9. Failure Recovery
10. Direct CLI Resume
11. Resume

## State Machine

```text
preflight
  -> discovery
  -> gate_a_pending
  -> trellis_preparation
  -> import_validation
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
parent decision log and Journal/runtime projection before beginning the next action. The approved
execution manifest is immutable.

## Compatibility And Qualification Gates

Run separately recorded checks. Trellis compatibility pins the official packages and qualifies
graph import plus single-writer projection. The stable backend baseline records provider policy,
capabilities, and launch profiles. The exact backend version registry controls
`wish_builder + pi|oh_my_pi|codex` dispatch. The Trellis scheduler path has
its own future pre-launch admission and fencing qualification. Backend workers never write
Trellis, so the two active evidence records remain independent. Active `wish_builder` dispatch
requires a qualified backend cell bound to the pinned Trellis compatibility digest, but it does
not depend on projection CAS. Future Trellis-owned dispatch does not use an Agent backend/OS cell
and requires its own qualified ownership and concurrent-write contract.

### Trellis Compatibility Gate

Before importing or projecting task records, load
`wish_builder/compatibility/trellis-0.6.15.json` through the strict compatibility decoder and
require all of the following:

1. The installed official packages are exactly `@mindfoldhq/trellis@0.6.15` and
   `@mindfoldhq/trellis-core@0.6.15`. Never install or resolve `@latest`.
2. The canonical record bytes and digest match Wish Builder's compiled trust pin, including the
   exact package names, versions, tarball SHA-256 values, adapter contract, and supported
   task-record operations.
3. The adapter uses only the qualified `loadTaskRecord`/`writeTaskRecord` boundary and rejects
   unknown fields, task-record shapes, package drift, and digest drift.

Trellis `0.7.0-dev.2` was a local test fixture later withdrawn from Wish Builder; it was never an
official Trellis release and is not supported. Passing this gate permits only the qualified Trellis
`0.6.15` import/projection path. It does not authorize a worker, scheduler, provider, or Agent
dispatch.

### Backend Qualification Gate

Before any launch, load the stable baseline
`wish_builder/compatibility/backend-qualification-0.6.15.json` and the exact version authority
`wish_builder/compatibility/backend-version-registry.json`. Require all of the following:

1. Each file's canonical bytes and digest match its own compiled trust pin.
2. The baseline's `policyDigest`, `launchProfileDigest`, and `capabilityDigest` values recompute,
   and the selected provider/platform cell matches the approved manifest and Trellis compatibility
   digest. Baseline `enabledForDispatch` values are historical evidence, not version admission.
3. Probe the installed package without launching it. Require an exact semantic version, an exact
   dependency pin, a matching package-lock version and npm SHA-512 integrity, the expected package
   name and executable entrypoint, and no symlink escape.
4. Resolve one exact `(provider, platform, backendVersion)` registry record. Its protocol profile,
   stable launch-profile digest, npm integrity, OS, and requested concurrency must match the probe
   and manifest.
5. Require `status=qualified`. Unknown, candidate, quarantined, malformed, or drifted records fail
   closed. Never guess a nearby version, use a floating tag, or silently downgrade.
6. Require the qualified record's evidence digest, publication receipt digest, and independent
   review reference. Derive parallel overlap and path-disjointness from the content-addressed raw
   evidence rather than trusting claim booleans.
7. Do not use projection CAS or concurrent-projection capability as worker-dispatch conditions.
   The worker writes only its isolated Git worktree and Wish Builder records canonical progress in
   the Journal; one separate projection writer catches Trellis up afterward.

The bundled registry admits only `Codex 0.149.0 / Windows` at concurrency one or two. Stop with
`concurrency_not_qualified` above two and `dispatch_not_qualified` for any other bundled version.
The published provenance is a human-accepted local detached provider reference, not an
OpenAI-signed attestation. Any additional version must pass the fixed local qualification harness,
independent evidence review, and fail-closed publication before it can become `qualified`. Active
manifest v2 rejects `trellis + trellis`. A later schema may add that path only after its pre-launch
proposal, admission, identity, fencing, stop/reject, and concurrent-write ownership contracts are
qualified; it does not depend on these Agent backend/OS records. Treat credentials as
provider-owned; a `blocked_credentials` result is not permission to request, copy, or inspect
credentials.

Use `wishctl.py backend-probe` for read-only detection. Use
`scripts/manage_backend_versions.py candidate|qualify|quarantine` with the current registry digest
for updates. The registry enforces no more than two qualified versions per backend/OS cell. A
package, policy, protocol profile, capability, compatibility record, or qualification record
change requires regenerated canonical data and an intentional trust-pin update. Never patch a
bundled JSON file by hand.

## Import Consistency

Official Trellis `0.6.15` exposes task records through its Core API. It does not expose an atomic
whole-graph snapshot, Wish Builder's snapshot format or revision digest, or a cross-process
compare-and-swap operation. Wish Builder derives its versioned graph snapshot, revision digest,
graph digest, and task-ID mapping from accepted task-record bytes. Record the exact Trellis and
adapter versions plus the derived snapshot and raw-byte digests in the parent decision log.

M1 permits exactly one projection writer. Import uses this bounded stable-read protocol:

1. Enumerate the complete candidate task-record set in deterministic order and read it through
   the qualified `loadTaskRecord` boundary while retaining the raw bytes used for hashing.
2. Immediately repeat the complete read and calculate a SHA-256 digest over each deterministic raw
   task-record set.
3. Accept only when both complete reads contain the same task identities and dependencies and the
   raw-byte digests match. Retry a mismatched pair at most three times, then fail closed with a
   concurrent-edit conflict.
4. Validate every dependency reference, derive the Wish Builder snapshot and revision digest, and
   calculate `trellis_graph_digest` from the canonical graph projection. None of these values is an
   official Trellis revision or CAS token.
5. Immediately before the first qualified dispatch and every later admission, repeat the stable
   read and compare the canonical material graph digest. A mismatch stops admission and invalidates
   Gate B; lifecycle-only record changes with the same graph digest do not.

Projection writes use the same single-writer boundary:

1. Stable-read the target record and calculate its pre-write SHA-256.
2. Confirm that the pre-write hash matches the expected observation. A mismatch is a conflict.
3. Write only through the qualified `writeTaskRecord` operation.
4. Read the record again and verify its post-write SHA-256 and expected semantic state.
5. Treat a conflict, interrupted write, unreadable result, mismatched post-write hash, or unknown
   outcome as blocked. Never guess, blindly retry, or advance canonical state from the projection.

The single-writer rule prevents Wish Builder from starting overlapping projection writers; it is
not cross-process CAS and cannot exclude an unrelated Trellis writer. Stable reads and pre/post
hash checks detect such races and fail closed. Never combine records from different accepted read
sets or accept a task whose referenced dependency is absent.

## Durable Lease Protocol

The append-only Wish Builder Journal is the lease authority. Treat its current head as
`(sequence, head_hash)`. The Journal store must expose one atomic compare-and-append operation that
appends only when both expected values still match; a process-local lock is not sufficient. Prove
atomicity across every coordinator process that can access the run. A local-file Journal may
arbitrate only coordinators on the same host with a verified OS-level atomic lock and durable
append. Distributed coordinators require a shared atomic Journal authority; otherwise reject that
deployment mode.

This compare-and-append contract belongs only to the Wish Builder Journal. It does not add CAS to
Trellis, turn the projection writer into a distributed lock, or qualify a Trellis dispatch path.
Official Trellis `0.6.15` still has no cross-process CAS.

1. To acquire, read the latest valid lease and the highest fencing token for the run. If no
   unexpired conflicting lease exists, compare-and-append `LEASE_ACQUIRED` with the expected head.
   Set the new token to the previous highest token plus one. Never reuse a token after expiry or
   crash.
2. Record run ID, lease ID, coordinator identity, approved `scheduler_mode`, fencing token,
   issued time, expiry, and the Gate B manifest digest. Use `lease_ttl_seconds` and
   `lease_clock_skew_seconds` from the frozen manifest. The Journal authority stamps
   `committed_at` and computes expiry from its own UTC clock; callers do not supply either value.
3. Renew at intervals no greater than one-third of `lease_ttl_seconds`, and commit renewal before
   `expires_at - lease_clock_skew_seconds`. Renewal uses compare-and-append with the current
   expected head, retains the same token, and extends expiry only after `LEASE_RENEWED` is durably
   appended. A contender may acquire only after `expires_at + lease_clock_skew_seconds`.
4. A CAS conflict requires a fresh Journal read and reconciliation. A failed renewal, expiry, a
   different live holder, or a mismatched scheduler mode means the lease is lost.
5. On lease loss, immediately stop sibling dispatch, admission, and merge. Best-effort stop or
   fence the active scheduler; completed worker output may be retained but cannot be admitted under
   the old token.
6. Only the current lease holder may reconcile and admit effects. After expiry or crash, a new
   holder first obtains the next token, then reconciles Trellis, workers, worktrees, Issues, PRs,
   CI, and prior effects before admitting new work.

This is a verification contract for the retained Journal, fencing, Gate, and recovery components,
not permission to build another lease subsystem inside the Trellis adapter. If the retained code
cannot pass it for a proposed coordinator boundary, block that boundary and require a separately
reviewed kernel-hardening change.

## Scheduler And Admission Loop

Gate B records one schema-valid `scheduler_mode + worker_backend` pair for the whole run. A run may
enter the loop below only after the separate backend qualification gate passes. The bundled
registry admits only `Codex 0.149.0 / Windows` at concurrency one or two; every other bundled
version stops with `dispatch_not_qualified`, while concurrency above two stops with
`concurrency_not_qualified`, before this loop and may still
perform qualified import/projection work. For an admitted cell, repeat until no unfinished task
remains:

1. Acquire or renew the durable scheduler lease using the protocol above. A live conflicting lease
   or different scheduler mode blocks execution.
2. Validate the manifest for the execution stage.
3. Obtain a complete stable task-record snapshot and recompute its canonical graph digest. Record
   the Wish Builder-derived revision digest as runtime provenance, but compare only the graph digest
   for material drift. A graph mismatch invalidates Gate B and stops new admissions; a derived
   revision change caused only by lifecycle progress never regenerates the manifest.
4. Read current task and PR state from Trellis, the Journal, workers, Issues, PRs, and CI; do not
   trust stale chat memory or rewrite the immutable manifest with runtime progress.
5. Compile or load `TaskDag` and `GraphIndex` from the frozen manifest and compute the allowed
   ready set.
6. Require active `scheduler_mode=wish_builder` with `worker_backend=pi`, `oh_my_pi`, or `codex`. Wish
   Builder selects only from the allowed set, creates or reuses the correct isolated worktree, and
   dispatches one task to the approved backend. Trellis stores task context and lifecycle but does
   not schedule sibling tasks.
7. Wait on structured worker completion or escalation, not arbitrary sleep loops.
8. Validate changed-path ownership, test evidence, Trellis Check/Finish, and PR metadata.
9. Route repairs to the owner or dispatch an independent review task through the selected mode.
10. Merge in topological order when policy permits, append the result to the Journal, rebuild the
     runtime projection, and recompute readiness.

A future `scheduler_mode=trellis` would instead require `worker_backend=trellis` and a pre-launch
handshake: Trellis proposes without launching, Wish Builder durably admits or rejects with the full
fencing identity, and a rejection or timeout means no launch. Active manifest v2 rejects this mode.

Every dispatch and result carries the exact fencing identity `(run_id, task_id,
trellis_graph_digest, scheduler_mode, fencing_token, dispatch_id)`. The admission event also binds
the manifest digest and worker backend. On crash or lease expiry, reconcile Trellis, worker,
worktree, Issue, PR, and effect evidence before acquiring a higher token. Results from an older
token may be inspected but are never admitted or merged. A future `scheduler_mode=trellis` may be
added only when the installed integration can propose before launch, wait for admission, attach the
complete identity, and stop or reject an unadmitted task. Active manifest v2 rejects it even if
local hooks appear to exist.
Official Trellis `0.6.15` does not provide cross-process CAS, so projection remains single-writer
and the `trellis + trellis` scheduler path remains unavailable until a qualified pre-launch
admission, fencing, and concurrent-write ownership integration exists. The `wish_builder` path
does not wait for upstream CAS: worker admission and projection admission are separate. Never
treat digest checks as a dispatcher lock or as a CAS substitute.

Trellis authors semantic waves as part of its task graph: Wave 0 contains shared foundations and
contracts, Wave 1 contains independently parallelizable modules, and Wave 2 contains integration
or release work. Wish Builder validates monotonic dependency waves, requires each Wave 1/2 task to
transitively depend on every task in its immediately preceding wave when that wave is non-empty,
requires total dependency order within Waves 0 and 2, and requires disjoint writable sets for
parallel Wave 1 tasks. `TaskDag.compile` consumes the validated values. Use the lower of the
approved concurrency limit and the selected worker backend's available capacity. Active manifest
v2 requires that limit explicitly; with the current bundled registry, `Codex 0.149.0 / Windows`
may use one or two workers, never three. Serial fallback is `scheduler_mode=wish_builder` with an allowed
worker backend and concurrency one, not a third mode.

The scheduler boundary is part of Gate B evidence and is not inferred from the worker backend:

- Future design only: when a later schema and integration are separately qualified,
  `scheduler_mode=trellis` admits only `worker_backend=trellis`.
  Trellis proposes and launches sibling tasks after durable admission; Wish Builder validates,
  admits, rejects, supervises,
  reconciles, and recovers. Wish Builder must not select tasks from `wishctl.py ready`, create
  sibling worker dispatches, or treat `GraphIndex` as a dispatcher in this mode.
- Once the exact backend/OS/version record and requested concurrency are qualified, `scheduler_mode=wish_builder`
  admits only `worker_backend=pi`, `oh_my_pi`, or `codex`. Wish Builder selects ready tasks from the
  frozen manifest and launches the approved backend; Trellis records task context, lifecycle,
  checks, and history, but must not schedule sibling tasks.
- Active M1 cannot change scheduler mode. A future coordinator may do so only by invalidating Gate B, returning to Trellis if the
  graph changed, and obtaining a new approval packet. Runtime progress, recovery, or lease
  takeover never changes the scheduler boundary.

## Worker Contract

Every dispatch must state:

- task ID, dispatch identity, Trellis task directory, worktree, base branch, and Issue;
- requirement IDs, acceptance criteria, regression commands, and documentation impact;
- owned and protected paths, frozen contracts, dependencies, and allowed auxiliary paths;
- instruction to inspect repository evidence and use the active Trellis task context;
- instruction to implement, test, self-review, run drift detection, and open a Draft PR;
- prohibition on scope expansion, architecture changes, merges, deploys, secrets, and edits to
  another task's ownership;
- exact structured completion and escalation mechanism for the selected worker backend.

Workers must not ask the user routine engineering questions. They should inspect evidence, pick
the smallest compliant implementation, and ask the coordinator only when a decision changes the
approved baseline.

## Integration

Prefer small squash merges directly to the approved base branch while incomplete behavior stays
behind feature flags. This preserves one Issue, one PR, one rollback commit, and a continuously
green mainline.

Before merge, require:

- all dependency PRs merged;
- revision-bound verification and declared regression commands green, using local or CI evidence
  exactly as Gate B requires and never relabeling one as the other;
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
| PR | coordinator/reviewer | diff review, required verification, ownership and acceptance |
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
3. Third consecutive failure: circuit-break, mark failed, and stop dependents. If the task graph
   must change, return the failure evidence to Trellis, import its revised graph, and repeat Gate B.
4. Escalate to a human only when recovery changes product intent, architecture, contracts, risk,
   external authorization, or roughly doubles approved scope.

A timed-out wait is not a worker failure when the worker is alive. Check task/terminal state and
continue bounded waits. Never kill or redispatch active work solely because it is slow.

## Direct CLI Resume

Use direct resume only for one unknown-status worker dispatch after crash, lease loss, or recovery
takeover has left the coordinator unable to prove whether the original dispatch effect happened.
The command is:

```bash
python scripts/wishctl.py resume <approved-execution-manifest-v2> <dispatch-recovery-proof.json> --journal-root <journal-root>
```

The proof file must be canonical `DispatchRecoveryPayload` JSON decoded by the strict runtime
decoder. It must establish all of the following before any recovery event is appended:

1. The manifest is execution manifest v2 and its canonical digest matches the active recovered
   lease.
2. The Journal hash chain replays cleanly, has not changed during recovery, and contains exactly
   one matching `DISPATCH_REQUESTED` event for `EffectOperation.WORKER_DISPATCH`.
3. The proof's `subject_identity`, `request_event_id`, `request_sequence`, and
   `request_event_hash` match that dispatch request exactly.
4. The proof carries a human `DIRECT_CLI` reconcile command whose expected sequence is the current
   Journal recovery anchor.
5. The effect receipt is for the same attempt identity, has `EffectStatus.ABSENT`, and is included
   as required evidence.
6. Process-tree termination is proven and required process evidence is bound to the same attempt
   identity.
7. The recovered coordinator has one active lease with a higher fencing token than the old attempt
   epoch.

`wishctl.py resume` constructs a recovery-only coordinator port. It can append the admitted
recovery events and print their JSON summary, but it cannot execute worker effects, reconcile
external worker effects, rewrite the approved manifest, or bypass the scheduler boundary selected
at Gate B. If any proof field is stale, mismatched, oversized, non-canonical, or insufficient,
resume must fail without appending.

## Resume

On resume:

1. Locate the parent by `task.json.meta.wish_builder`.
2. Read its PRD, design, Trellis task graph, implementation plan, decision log, immutable
   manifest, Journal, and traceability matrix.
3. Validate the manifest and compare its Trellis parent ID, Wish Builder-derived task-record
   revision digest, and graph digest with a current stable task-record snapshot, then compare
   runtime state with Git branches, Issues, PRs, CI, and recent commits.
4. Rebuild stale runtime projections only from objective evidence and log the reconciliation.
   Never repair runtime drift by rewriting the approved manifest.
5. Re-enter the persisted phase. Do not repeat approved reviews or gates unless their artifact
   hashes or the canonical Trellis graph digest changed materially.
