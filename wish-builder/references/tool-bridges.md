# Tool Bridges

## Contents

1. Preflight
2. gstack Boundary
3. Trellis Boundary
4. Worker Backend
5. Compatibility Rules

## Preflight

Inspect before changing the repository:

| Capability | Evidence | If Missing |
| --- | --- | --- |
| Git | repository root, status, base branch | Ask where to initialize or select a repo |
| gstack | installed skill metadata for required skills | Offer official gstack setup, then stop |
| Trellis | `.trellis/workflow.md` and task scripts | Offer official Trellis setup, then stop |
| Worker backend | native agents or Trellis worktree pipeline | Fall back to serial execution only with approval |
| Issue/PR | authenticated provider and remote | Keep local IDs until access is approved |
| Test runner | repository commands and CI config | Make test bootstrap a Wave 0 task |

Never auto-install a global dependency. Current official sources are:

- gstack: `https://github.com/garrytan/gstack`
- Trellis: `https://github.com/mindfold-ai/Trellis`
- Trellis package: `npm install -g @mindfoldhq/trellis@latest`

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
control. Do not use `spec --execute`: Trellis owns implementation. Using `spec` only to draft an
Issue is acceptable when it does not dispatch or duplicate the execution owner.

gstack artifacts are advisory inputs. Preserve their alternatives, concerns, and scores under
the parent Trellis task's `research/` directory. Consolidate them into approved `prd.md` and
`design.md`; do not make workers read every raw review when curated context is enough.

For a spawned or headless advisory review, Wish Builder may auto-select the option explicitly
marked recommended and continue through advisory-only question or stop points. Preserve every
material alternative. Never use this rule to bypass setup, safety, credential, payment,
deployment, or other external-mutation approval.

When the approved run policy forbids network access or writes outside the repository, use the
installed skill's supported offline or headless path. Skip optional update checks, telemetry,
global state, web research, and browser or mockup artifacts, and list every skipped section in the
saved review. If the substantive review cannot run within the boundary, report the capability gap
instead of imitating its result.

## Trellis Boundary

Trellis owns:

- repository-scoped engineering specs in `.trellis/spec/`;
- parent and child task artifacts;
- phase-specific `implement.jsonl`, `check.jsonl`, and `debug.jsonl` context;
- each leaf task's Implement, Check, Finish, and Create PR lifecycle;
- journals, task history, and archive.

The wish-builder coordinator owns:

- Gate A and Gate B;
- the cross-task dependency DAG and ready queue;
- Issue/PR identity, worktree assignment, concurrency, and merge order;
- cross-task drift, integration, release, and escalation.

Create tasks using the installed Trellis scripts instead of writing `task.json` from scratch.
Common local commands are `task.py create`, `init-context`, `add-context`, `set-branch`,
`set-base-branch`, `validate`, `start`, and `archive`. Read `.trellis/workflow.md` and local
Trellis skill instructions for exact syntax.

If the installed Trellis CLI cannot write a structured `meta.wish_builder` object, create the task
with the CLI first, then patch only `task.json.meta.wish_builder`. Never hand-create or replace the
full `task.json`.

After Gate B, import the already-approved packet. Do not invoke Trellis brainstorming for each
child because that creates duplicate questions and can mutate approved scope. A material change
to a child PRD must return to the appropriate human gate.

## Worker Backend

Choose exactly one dispatch backend for a run:

1. Prefer Trellis's installed worktree/multi-agent pipeline when it can start, monitor, and
   report isolated child tasks.
2. Otherwise use the host's native child-agent or orchestration API. Start the Trellis child
   task inside the assigned worktree and require a structured result.
3. If no supervised worker mechanism exists, propose serial execution. Do not pretend that
   asynchronous work is being supervised.

Do not invoke Trellis `/parallel` while a native or external coordinator also dispatches the
same DAG. Nested Implement and Check agents inside one Trellis leaf are allowed because they do
not schedule sibling tasks.

## Compatibility Rules

- Resolve skill names from installed metadata; installations may prefix them with `gstack-`.
- Prefer local `.trellis/` scripts over globally remembered command syntax.
- Store integration fields under `task.json.meta.wish_builder`; do not invent unsupported
  top-level Trellis fields.
- Keep the manifest schema versioned. Reject a newer unsupported schema instead of guessing.
- Record tool versions and selected backend in the parent decision log so a resumed agent can
  reproduce the run.
