# Changelog

This file records user-visible changes to Wish Builder.

## Unreleased

- Locally published the independently reviewed Codex/Windows backend qualification with a
  maximum of two concurrent turns; all other backend/OS cells remain disabled.
- Made backend qualification publication rollback cleanly after partial filesystem failures and
  reject conflicting evidence without overwriting it.
- Stored bundled qualification evidence under a shorter content-derived key so Windows wheel and
  sdist builds include the complete evidence set without exceeding legacy path limits.

## 0.1.0.dev1 - 2026-08-30

- Replaced the local `0.7.0-dev.2` test fixture (later withdrawn from Wish Builder and never an
  official Trellis release) with exact official
  `@mindfoldhq/trellis@0.6.15` and `@mindfoldhq/trellis-core@0.6.15` integration pins.
- Split Trellis graph/projection compatibility from Codex, Pi, and Oh My Pi backend
  qualification; every backend/OS dispatch cell remains disabled.
- Official Trellis evidence now runs only the real 0.6.15 graph/projection adapters and
  lifecycle path (22 Node + 7 Python tests); fake service tests remain in the general suite.
- Bound that evidence digest to the complete Wish Builder runtime, official integration tests,
  bridge pins, compatibility data, and evidence-policy sources so stale local results fail closed.
- Separated backend runtime effects from Trellis projection writeback: workers use isolated
  worktrees and the Journal, while one writer projects results to Trellis. Backend admission now
  depends on its own live evidence instead of Trellis projection CAS.
- Excluded Trellis package tarballs from Wish Builder release artifacts and retained only
  verified npm integrity, archive hash, and extracted-tree metadata.
- Serialized recovery of the same external operation across independently constructed local
  coordinator components so stale provider state cannot create a duplicate channel or turn.
- Fixed Windows lease-owner probing so an exited process whose kernel object is still retained
  by an open handle is reported dead instead of being mistaken for the exact live owner.
- Changed the M1 acceptance rule so passing local tests is sufficient for the development preview.
  Hosted CI is not run when its budget is unavailable, and no CI result is claimed.
- Added an optional local evidence manifest and release verifier for teams that want a stricter,
  revision-bound release packet; backend dispatch qualification remains separate and every
  backend/OS cell stays disabled.
- Made local release promotion reject a dirty checkout or a candidate revision that is not the
  checked-out commit, so release bytes cannot be attributed to another source revision.
- Kept the existing CI-backed release verifier as an optional path with a hash-locked build
  toolchain, deterministic rebuild and byte comparison, plus protected-environment review.

## 0.1.0.dev0 - 2026-08-20

Initial development preview.

- Added the human-approved Gate A and Gate B workflow.
- Added deterministic import of official Trellis `0.6.15` task records into an immutable execution manifest.
- Added single-writer projection back to Trellis with before-and-after digest checks.
- Added TaskDag, GraphIndex, Journal, fencing, recovery, Git worktree isolation, acceptance checks, and deterministic promotion order.
- Added separate Trellis compatibility and Codex, Pi, and Oh My Pi backend qualification records.
- Added Windows and Linux CI definitions for Python 3.11, 3.12, and 3.13.

Codex, Pi, and Oh My Pi dispatch remains disabled until each backend/OS cell has the required live qualification evidence. Official Trellis `0.6.15` does not provide cross-process CAS, so M1 projection remains single-writer and fail-closed, but that projection limit no longer blocks independently qualified Agent workers. `trellis + trellis` is a future design boundary, not an active manifest v2 value; it needs a later schema plus qualified pre-launch admission, fencing, stop/reject behavior, and concurrent-write ownership.
