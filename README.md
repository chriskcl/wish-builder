# Wish Builder

English | [繁體中文](README.zh-TW.md)

Turn a product idea into a reviewed, traceable software project that agents can carry forward.

Wish Builder is a Codex Skill for work that needs more than a one-shot code prompt. You describe the direction. [gstack](https://github.com/garrytan/gstack) helps shape the product and engineering plan. A person approves the scope and architecture. [Trellis](https://github.com/mindfold-ai/Trellis) then creates the editable tasks and dependencies. Wish Builder checks that graph, freezes the approved version, and supervises execution.

It does not contain a second task planner or task database. Trellis owns the working task graph. Wish Builder owns the approved execution snapshot and the rules that keep agents inside it.

> **Status:** development preview (`0.1.0.dev1`). The local control plane, immutable execution snapshot checks, fail-closed admission, Journal and recovery boundaries, Git adapter, and Wish Builder import/projection bridge for official Trellis `0.6.15` are implemented. The assembled lifecycle, including crashes around Git changes, is tested end to end with controlled subprocess workers.
>
> `Codex / Windows` is now locally qualified and published for real dispatch with a maximum
> concurrency of two. The record covers a full turn, active cancellation, crash/restart
> reconciliation without redelivery, cleanup, and two overlapping siblings with disjoint paths.
> The evidence and package integrity were independently reviewed before the fail-closed publisher
> added the exact version record and compiled registry trust pin. This is a local formal publication based on
> human-accepted detached provider provenance, not an OpenAI-signed attestation or official OpenAI
> certification. Pi, Oh My Pi, and Codex/Linux remain candidates and cannot dispatch. Unknown,
> candidate, quarantined, or mismatched backend versions fail closed. Official Trellis `0.6.15`
> also lacks cross-process compare-and-swap
> (CAS), so projection stays single-writer and fail-closed. Worker dispatch and Trellis projection
> are separate: workers write only isolated Git worktrees and the Journal, while one writer later
> projects results to Trellis. The repository is public, and
> [`v0.1.0.dev1`](https://github.com/chriskcl/wish-builder/releases/tag/v0.1.0.dev1) is available as
> a prerelease under GPL-3.0-only.

## Why it exists

A short product idea is a good starting point, but it is not enough to safely coordinate several coding agents. Work can start before the requirements settle, agents can edit the same files, and architecture problems can surface after implementation is already underway.

Wish Builder keeps the work in a predictable order:

1. Clarify the product before touching product code.
2. Let a person approve the scope and architecture.
3. Let Trellis turn the approved documents into small tasks with explicit dependencies.
4. Validate and freeze one exact task graph before dispatch.
5. Run independent work in parallel, then test, review, and merge it in a stable order.

The point is not to remove people from the project. It is to spend human attention on product and architecture decisions, while agents handle well-bounded engineering work.

## How it works

```text
┌────────────────────────────────────────┐
│ You describe the product direction     │
└───────────────────┬────────────────────┘
                    │
                    v
┌────────────────────────────────────────┐
│ gstack reviews run in child sessions   │
└───────────────────┬────────────────────┘
                    │
                    v
┌────────────────────────────────────────┐
│ Wish Builder batches human decisions   │
└───────────────────┬────────────────────┘
                    │
                    v
┌────────────────────────────────────────┐
│ Gate A: approve product + architecture │
└───────────────┬────────────────────────┘
         PASS   │       NEEDS CHANGES → return to gstack
                v
┌────────────────────────────────────────┐
│ Trellis creates tasks + dependencies   │<────┐
└───────────────────┬────────────────────┘     │
                    │                          │
                    v                          │
┌────────────────────────────────────────┐     │
│ Wish Builder imports + validates       │─────┘
└───────────────────┬────────────────────┘  INVALID → return to Trellis
                    │
                    v
┌────────────────────────────────────────┐
│ Gate B: approve graph + digests        │
└───────────────┬────────────────────────┘
         PASS   │       NEEDS CHANGES → return to Trellis
                v
┌────────────────────────────────────────┐
│ Freeze, dispatch, verify, and recover  │
└───────────────────┬────────────────────┘
                    │
                    v
┌────────────────────────────────────────┐
│ Test, review, merge, and archive       │
└────────────────────────────────────────┘
```

This is the intended end-to-end workflow. The current preview includes the assembled local lifecycle and crash recovery path, verified with controlled subprocess workers. Trellis compatibility passes for import and single-writer projection. The separate backend version registry admits only `Codex 0.149.0 / Windows`, at concurrency one or two; every other bundled backend version remains a non-dispatching candidate.

The planning stage normally uses `office-hours`, `plan-ceo-review`, and `plan-eng-review`, plus `plan-design-review` when the product has a user interface. Each review runs in its own non-interactive child session. The child temporarily follows the review's explicit recommendation so it can finish, then returns the practical result, alternatives, ease of changing later, and technical reasoning. Wish Builder records only easy, reversible engineering choices automatically. Product, architecture, cost, security, and other material choices are rewritten in plain language and collected for Gate A. A gstack recommendation is advice, not human approval. A child that tries to question the user directly or returns incomplete decision data is stopped.

Gate A is where a person approves what will be built and how the main parts fit together.

Trellis then prepares the candidate task graph. Wish Builder checks dependencies, owned paths, acceptance commands, and task size. Problems go back to Trellis; Wish Builder does not invent a separate task list. The graph snapshot, revision digest, and manifest used for this check are Wish Builder-derived contracts, not official Trellis APIs.

Gate B approves the material graph projected from one stable Trellis task-record read, the Wish Builder-derived graph and execution-manifest digests, the scheduler, worker backend, and permission set. The Wish Builder-derived task-record revision digest is retained as provenance; status, progress, and other lifecycle-only changes do not invalidate Gate B when the canonical graph digest is unchanged. A meaningful change to the Trellis graph invalidates that approval. Product code is not changed and implementation agents are not dispatched before Gate B passes.

## Who owns what

| Trellis owns | Wish Builder owns |
| --- | --- |
| Creating and splitting tasks | Validating the imported task graph |
| Editing task dependencies | Gate B and content digests |
| Task context and lifecycle | The immutable execution snapshot |
| Implement, Check, and Finish steps | Admission, fencing, Journal, and recovery |
| Task history and archive | Result validation and merge admission |

There is no PRD-to-task generator, task CRUD system, second board, or second task database in this project.

## Scheduling and agent backends

The design defines two mutually exclusive scheduler modes:

| `scheduler_mode` | `worker_backend` | Intended responsibility | Current M1 status |
| --- | --- | --- | --- |
| `trellis` | `trellis` | Trellis schedules sibling tasks; Wish Builder validates and supervises | Disabled: the Trellis scheduler path has no qualified pre-launch admission and fencing integration; `0.6.15` also has no cross-process CAS |
| `wish_builder` | `pi`, `oh_my_pi`, or `codex` | Wish Builder schedules from the frozen graph into isolated worktrees; one separate writer later projects Journal results to Trellis | Exact `Codex 0.149.0 / Windows` locally qualified at concurrency 1-2; all other bundled versions are candidates |

For M1, only `scheduler_mode=wish_builder` is accepted by the current Python control plane. Each run chooses one backend. Before launch, Wish Builder probes the installed package and requires an exact version, npm integrity, protocol profile, launch profile, OS, and concurrency match in the pinned backend version registry. Unknown, candidate, quarantined, or drifted versions stop instead of being guessed, downgraded, or silently replaced. `Codex 0.149.0 / Windows` is admitted at concurrency one or two. Concurrency three returns `concurrency_not_qualified`; every other bundled version returns `dispatch_not_qualified` before an agent is launched.

When the Trellis scheduler is implemented later, `GraphIndex` will remain a validation and recovery index, not a second dispatcher.

Backend qualification is intentionally conservative:

| Backend | Windows evidence | Linux evidence | Production dispatch |
| --- | --- | --- | --- |
| Codex | `0.149.0` qualified; maximum two concurrent turns | `0.149.0` candidate; full live qualification required | Windows `0.149.0` only |
| Pi | `0.84.2` candidate; startup and handshake only | `0.84.2` candidate; full live qualification required | Disabled |
| Oh My Pi | `17.4.0` candidate; a configured model and credential are required | `17.4.0` candidate; full live qualification required | Disabled |

The locally published `Codex 0.149.0 / Windows` record completed the required full-turn,
active-cancellation, crash/reconcile, cleanup, parallel-overlap, and platform evidence and is
`status=qualified` with `maxConcurrency=2`. Its source revision is
`fd3296ed1f8d85e9a1347eb1e2dcdf611ec62720`. Independent review also matched the official
`@openai/codex@0.149.0` package and Windows native package against their npm integrity and
installed files. The preserved provenance is a human-accepted local detached provider reference,
not an OpenAI-signed attestation. The other five bundled version records remain `status=candidate`.

Trellis compatibility and backend qualification remain separate records. The Trellis record binds
the frozen graph and projection adapter. The stable backend baseline records policy, capabilities,
launch profiles, and historical evidence. The backend version registry decides whether one exact
backend/OS/version may dispatch. Active `wish_builder` dispatch requires a qualified version record
whose profile and launch digest match the approved baseline; it does not require Trellis projection CAS because workers never write
Trellis. The future Trellis-owned scheduler does not use an Agent backend/OS cell, but it needs a
later manifest schema plus qualified pre-launch admission, fencing, stop/reject behavior, and
concurrent-write ownership. Claude Code and macOS are deferred until the first three backends and
the Windows/Linux matrix are stable.

Trellis compatibility and backend qualification are separate contracts:

- [`wish_builder/compatibility/trellis-0.6.15.json`](wish_builder/compatibility/trellis-0.6.15.json) qualifies the official `@mindfoldhq/trellis@0.6.15` and `@mindfoldhq/trellis-core@0.6.15` packages for the documented import and single-writer projection boundary.
- [`wish_builder/compatibility/backend-qualification-0.6.15.json`](wish_builder/compatibility/backend-qualification-0.6.15.json) is the stable adapter policy, capability, launch-profile, and historical-evidence baseline.
- [`wish_builder/compatibility/backend-version-registry.json`](wish_builder/compatibility/backend-version-registry.json) is the exact backend/OS/version dispatch authority. It currently qualifies only `Codex 0.149.0 / Windows`, with a concurrency limit of two.

Official Trellis `0.6.15` has no reliable cross-process CAS. M1 therefore permits one projection writer at a time, accepts only stable task-record reads, checks the expected SHA-256 before writing, verifies SHA-256 and content after writing, and fails closed on conflicts or unknown outcomes. These digest checks protect projection integrity; they are not CAS and do not act as a worker-dispatch lock. Backend workers write only isolated Git worktrees and the Journal. The separate Trellis scheduler mode needs its own qualified pre-launch admission, fencing, and concurrent-write ownership.

## What is implemented

The repository currently includes:

- strict input contracts and deterministic error messages;
- deterministic Wish Builder graph snapshots derived from official Trellis `0.6.15` task records, followed by manifest v2 generation;
- single-writer lifecycle projection into the authoritative Trellis repository through official Core `loadTaskRecord` and `writeTaskRecord`, with stable reads, pre/post-write digest checks, and no automatic retry after a pre-write digest conflict;
- dependency, owned-path, ready-set, and requirement-trace checks;
- Gate decisions tied to content hashes, so later edits invalidate old approval;
- durable Gate B admission that can be rerun safely even after later runtime events were written;
- an append-only Journal, leases, epochs, fencing, checkpoints, replay, and `GraphIndex` rebuilds;
- a foreground coordinator with renewable scheduler leases, isolated attempt worktrees, stable
  promotion order, and fixture subprocess E2E coverage;
- restart recovery that recognizes already finished Codex turns, preserves worktree identity, and
  resumes only when the previous worker is proven gone;
- ordinary project acceptance commands run inside the materialized promotion candidate before the target branch advances;
- subprocess containment, output limits, timeout handling, and fail-closed recovery;
- Git staging, promotion, cleanup, quarantine, and trace/export services;
- protocol-profile adapters plus fail-closed exact-version probing and admission;
- fail-closed local publication of candidate, qualified, or quarantined backend version records, including evidence digests and a compiled registry trust pin;
- package-to-Skill runtime synchronization and a reproducible development ZIP;
- local tests for contracts, scheduling, recovery, Git effects, packaging, and controlled performance.

These parts are real and tested. The assembled local lifecycle, including recovery from crashes around Git changes, is covered end to end with controlled subprocess workers. The guarded `wishctl run` entry probes the installed SDK and admits only the exact locally published `Codex 0.149.0 / Windows` record at concurrency one or two. The remaining backend work is to produce and independently review the same complete, content-addressed evidence before promoting any candidate version to qualified.

Real Issue, Pull Request, hosting, credential, background supervisor, and production deployment adapters are outside the current implementation.

## Install the development preview

### Requirements

- Python 3.11 or newer
- Git
- Node.js 18.17 or newer
- Codex with Skill support
- gstack
- `@mindfoldhq/trellis@0.6.15`
- a verified local SDK copy of `@mindfoldhq/trellis-core@0.6.15` for the bridge

The Python runtime itself has no third-party package dependency. Wish Builder reports missing tools before it starts and does not install global tools, sign in to accounts, or change repository settings without approval.

Install the Trellis CLI by exact version:

```bash
npm install -g @mindfoldhq/trellis@0.6.15
```

The Core bridge accepts either an extracted `@mindfoldhq/trellis-core@0.6.15` package root or its verified official npm tarball. The tarball is an input for local verification and is never bundled into a Wish Builder release. Do not substitute `@latest` or another prerelease.

### Install the Skill ZIP

Download [`wish-builder-skill-0.1.0.dev1.zip`](https://github.com/chriskcl/wish-builder/releases/download/v0.1.0.dev1/wish-builder-skill-0.1.0.dev1.zip) and [`SHA256SUMS`](https://github.com/chriskcl/wish-builder/releases/download/v0.1.0.dev1/SHA256SUMS) from the published prerelease. The repository also contains a synchronized [`wish-builder-skill.zip`](wish-builder-skill.zip) for testing directly from a source checkout.

The tagged `v0.1.0.dev1` asset predates the `Unreleased` backend-version registry work described here. To test the current `main` behavior before the next prerelease, use the repository ZIP from the same source revision.

Windows PowerShell:

```powershell
Expand-Archive .\wish-builder-skill-0.1.0.dev1.zip -DestinationPath "$env:USERPROFILE\.codex\skills"
```

macOS or Linux:

```bash
mkdir -p ~/.codex/skills
unzip wish-builder-skill-0.1.0.dev1.zip -d ~/.codex/skills
```

The installed file should appear at:

```text
~/.codex/skills/wish-builder/SKILL.md
```

Repository ZIP SHA-256 (the prerelease asset has its own value in `SHA256SUMS`):

```text
adcda3a2a2aaa26785e3def244a45a37d5df2e9d506c72b374ea86d7fd6bd58f
```

The repository is public. Codex's Skill installer can also install the repository's `wish-builder/` directory directly from GitHub.

## Start a project

Open the target Git repository in Codex and give Wish Builder a short direction:

```text
Use $wish-builder in this repository.

Build a shared expense tracker for two roommates.
Keep the first version local-only, with no payments or bank connections.
After I approve the product, architecture, and task graph, continue on your own.
Stop only if the work leaves the approved plan, needs a high-risk action,
or is ready for production deployment.
```

You do not need a full specification at the start. The early reviews identify the user, problem,
success criteria, boundaries, and architecture. The current preview can admit an approved Gate B
snapshot and run a qualified manifest in the foreground. It is not an unattended background
service: keep the command attached, and rerun the same manifest to invoke the guarded recovery path
after an interruption.

## Decisions that still need a person

| Decision | When it appears | What you approve |
| --- | --- | --- |
| Setup gate | A tool, login, or repository change is required | Installation, initialization, authentication, or configuration |
| Gate A | Before Trellis prepares the task graph | Product goal, scope, architecture, data, and security choices |
| Gate B | Before implementation agents are dispatched | The material graph projected from Trellis task records, Wish Builder-derived graph and manifest digests, scheduler, backend, tests, merge policy, and permissions |
| Gate C | Before production deployment | Destination, risks, checks, and rollback plan |
| Drift decision | Work leaves the approved boundary | Whether to revise the scope, architecture, public interface, or security boundary |

A wish is not an approval. Gate A and Gate B require an explicit pass or a list of changes.

## Safety boundaries

Without separate approval, Wish Builder will not:

- request, store, or rotate credentials;
- spend money or change billing;
- deploy to production;
- delete production data or weaken access controls;
- perform an irreversible data migration;
- change an approved product direction, architecture, or public interface;
- run Trellis and Wish Builder as competing dispatchers for the same graph.

Normal implementation choices and ordinary test failures stay with the coordinator. Scope changes, high-risk actions, and unresolved repeated failures return to a person.

## Command-line checks

`wishctl` turns important workflow rules into repeatable checks. It uses only the Python standard library at runtime.

| Command | Purpose |
| --- | --- |
| `validate` | Validate approvals, graph structure, paths, tests, and recovery data |
| `ready` | List ready tasks that do not conflict in the frozen graph |
| `drift` | Check changed files against a task's owned paths |
| `trace` | Render requirement-to-task and delivery traceability |
| `hash` | Calculate a Gate artifact SHA-256 |
| `snapshot-trellis` | Derive a Wish Builder graph snapshot from official Trellis `0.6.15` task records |
| `import-trellis` | Convert a Wish Builder-derived Trellis graph snapshot into manifest v2 |
| `admit-gate-b` | Verify and durably record the approved Gate B snapshot before execution |
| `run` | Execute one admitted, qualified manifest in the foreground and recover it safely after restart |
| `backend-probe` | Inspect an installed backend's exact version, integrity, profile, OS status, and concurrency limit without launching it |
| `decide` | Record a direct CLI Gate decision in the Journal |
| `resume` | Resume one unknown dispatch from a verified recovery proof |

Examples:

```bash
python scripts/wishctl.py --help
python scripts/wishctl.py backend-probe --provider codex --provider-sdk-root C:/path/to/pinned-sdk-root
python scripts/wishctl.py validate path/to/execution-manifest.json --stage planning
python scripts/wishctl.py snapshot-trellis <parent-task-id> --core-archive path/to/mindfoldhq-trellis-core-0.6.15.tgz --output trellis-graph.json
python scripts/wishctl.py import-trellis path/to/trellis-graph.json path/to/import-settings.json --output execution-manifest.json
python scripts/wishctl.py admit-gate-b execution-manifest.json gate-b-<sha256>.md import-settings.json --approved-artifact-hash <sha256> --runtime-root path/to/run --workspace-root . --actor-id <actor>
python scripts/wishctl.py run execution-manifest.json --runtime-root path/to/run --workspace-root . --provider-sdk-root C:/path/to/pinned-sdk-root --core-root C:/path/to/trellis-core
```

The installed Skill exposes the same runtime at `wish-builder/scripts/wishctl.py`. `backend-probe`
returns exit code `0` only for a qualified exact version, `1` for unknown, candidate, quarantined,
or drifted versions, and `2` for invalid input.

Maintainers update the registry without changing the execution kernel:

```powershell
python scripts\manage_backend_versions.py candidate --help
python scripts\manage_backend_versions.py qualify --help
python scripts\manage_backend_versions.py quarantine --help
```

Every update requires the current registry digest. A newly detected version starts as `candidate`.
It becomes `qualified` only after the fixed local harness has exercised dispatch, structured
results, cancellation, crash reconciliation, cleanup, sibling overlap, approved concurrency, and
hostile input handling, followed by an independent evidence review. A bad version can be moved to
`quarantined` without changing `TaskDag`, `GraphIndex`, Gate, Journal, or recovery code.

## Repository layout

```text
.
|-- README.md                     English overview and usage
|-- README.zh-TW.md               Traditional Chinese overview and usage
|-- pyproject.toml                Python package and wishctl entry point
|-- wish_builder/                 Authoritative Python implementation
|   |-- adapters/                 Trellis, process, storage, and Git boundaries
|   |-- compatibility/            Trellis compatibility, stable backend baseline, and exact-version registry
|   |-- contracts/                Input and artifact contracts
|   |-- kernel/                   DAG, gates, state, and GraphIndex
|   |-- presentation/             Trace and export output
|   |-- processes/                Coordinator and worker execution
|   `-- services/                 Journal, recovery, cleanup, and Git services
|-- wish-builder/                 Standalone installable Codex Skill
|-- scripts/                      Build, sync, and CI checks
|-- tests/                        Unit, integration, fault, and performance tests
`-- wish-builder-skill.zip        Reproducible development archive
```

The full operating rules live in [`wish-builder/SKILL.md`](wish-builder/SKILL.md). Artifact formats and tool boundaries are under [`wish-builder/references/`](wish-builder/references/).

## Verification status

**Local tests passed.** Under the current M1 policy, that is enough to accept this development preview.

| Check | Result |
| --- | --- |
| Earlier local non-performance matrix | Windows and Linux on Python 3.11/3.12/3.13; 1,498 run per cell, 0 failures or errors; 9 allowed skips on Windows and 13 on Linux |
| Fresh full local suites | Windows on Python 3.13.14; 1,601 non-performance tests plus 16 performance tests, 0 failures or errors, 3 platform-specific skips |
| Independent Codex/Windows evidence audit | 52 passed, 1 Windows symlink-permission skip; verdict `PASS` |
| Post-publication qualification and admission tests | 68 passed, 1 Windows symlink-permission skip, 59 subtests passed |
| Official Trellis `0.6.15` integration | Current Windows run passed 24 Node bridge tests and 14 Python integration tests; the pinned cross-platform evidence retains the same set per platform |
| Skill packaging and installed runtime | 245 packaging and release-policy tests inside the full suite plus 13 standalone runtime smoke tests passed |
| Python compilation and whitespace checks | Passed |

These are local results. GitHub Actions was not run because its budget is exhausted, so this project does not claim a CI pass or failure for the candidate. The `Codex / Windows` qualification is also a local publication, not an official provider certification.

Run the main local suites with:

```powershell
uv run --python 3.13.14 --no-project python scripts\ci_test_suite.py --exclude-package performance
uv run --python 3.13.14 --no-project python scripts\ci_test_suite.py --only-package performance
uv run --python 3.13.14 --no-project python wish-builder\scripts\test_wishctl.py
```

For a stricter, reproducible release packet, the optional local evidence tools can still bind raw results and release files to one committed revision:

```powershell
uv run --locked --python 3.13 python scripts\local_evidence_packet.py `
  --evidence-root <evidence-root> --candidate-revision <commit-sha> `
  --safety-base-ref <base-ref> --output <manifest.json> `
  --digest-output <manifest.sha256>

uv run --locked --python 3.13 python scripts\ci_local_release.py `
  --repository-root . --evidence-root <evidence-root> `
  --safety-base-ref <base-ref> --distribution-root <distribution-root> `
  --manifest <manifest.json> --manifest-digest <manifest.sha256> `
  --output-dir <release-assets> --revision <commit-sha> `
  --version 0.1.0.dev1 --tag v0.1.0.dev1
```

## Remaining dispatch work

- Repeat the full live qualification and independent review before promoting Pi, Oh My Pi, Codex/Linux, or any later version record from candidate to qualified.
- Keep `Codex 0.149.0 / Windows` at a maximum concurrency of two until stronger evidence is formally published.
- Add the real Issue, Pull Request, and hosting adapters needed by the chosen workflow.
- Complete one public example from a short product wish through reviewed, merged changes.

## Contributing

Start with the concrete problem a change solves. Workflow changes should update the Skill, its reference documents, and the related tests together. Changes to `wishctl` should include a small test that reproduces the problem.

Do not remove an approval gate, owned-path check, fencing rule, or fail-closed recovery behavior without a replacement that provides the same protection.

## License

Wish Builder is licensed under the [GNU General Public License v3.0 only](LICENSE). See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for bundled and development-tool notices.
