# TODOS

## Provider Support

### Add a Claude Code backend adapter

**What:** Qualify Claude Code as an additional Wish Builder backend after Pi, Oh My Pi, and Codex are stable.

**Why:** Claude Code is useful to teams that already run it, but adding a fourth provider now would enlarge the compatibility matrix before the shared Channel contract is proven.

**Context:** Do not modify Trellis or add provider behavior to its task store. Add a Wish Builder backend adapter that follows the existing caller-supplied operation identities, fresh attempt sessions, capability digests, recovery rules, and Windows/Linux conformance suite. Keep the Trellis compatibility record separate from Claude backend qualification.

**Effort:** M
**Priority:** P3
**Depends on:** Pi, Oh My Pi, and Codex provider/OS matrix complete; stable Wish Builder backend contract

## Platform Support

### Qualify macOS support

**What:** Add macOS as a supported Wish Builder backend host after the Windows/Linux v1 matrix is complete.

**Why:** macOS is common for local development, but it needs its own path, filesystem, process, cancellation, credential, and live-provider evidence rather than inheriting a Linux claim.

**Context:** Reuse the same graph import, manifest, Journal, fencing, and backend contracts. Add a macOS host profile, CI coverage, local filesystem durability checks, process-tree/cancellation evidence, and live Pi/Oh My Pi/Codex conformance. Do not mark macOS supported from unit tests alone.

**Effort:** L
**Priority:** P3
**Depends on:** Windows/Linux v1 release; stable Wish Builder backend contract

## Trellis Upstream

### Propose a minimal cross-process CAS API

**What:** Submit the smallest compare-and-swap operation needed to guard Trellis task-record
projection across processes, then qualify its atomicity and concurrent-writer behavior.

**Why:** Official Trellis `0.6.15` exposes task-record reads and writes but no reliable
cross-process CAS. Wish Builder therefore keeps projection single-writer. Backend dispatch is
qualified independently, but multiple projection writers and any future Trellis-owned scheduler
need a trustworthy concurrent-write ownership boundary. Digest checks detect drift but cannot act
as a distributed lock.

**Context:** Keep the API in Trellis. After an upstream release proves the operation and the
projection adapter's concurrency tests pass, update the separate Trellis compatibility record and
qualify an optional concurrent-projection mode. Do not enable multiple projection writers from
local fixtures alone, and do not use this TODO to block independently qualified Agent workers.

**Effort:** M
**Priority:** P2
**Depends on:** Official Trellis maintainer review and a versioned CAS implementation

## Completed
