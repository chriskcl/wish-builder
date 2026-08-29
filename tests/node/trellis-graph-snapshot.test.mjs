import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test, { after } from "node:test";

import { loadPinnedTrellisCore } from "../../wish_builder/bridges/trellis_core/core-loader.mjs";
import {
  WISH_BUILDER_GRAPH_FORMAT,
  deriveTrellisGraphSnapshot,
} from "../../wish_builder/bridges/trellis_core/graph-snapshot.mjs";

const REPOSITORY_ROOT = path.resolve(fileURLToPath(new URL("../..", import.meta.url)));
const WORK_ROOT = path.resolve(REPOSITORY_ROOT, "..", "work");
const DEFAULT_AUDIT_ROOT = path.join(WORK_ROOT, "artifacts", "trellis-0.6.15");
const CORE_ROOT = process.env.WISH_BUILDER_TEST_TRELLIS_CORE_ROOT
  ?? path.join(WORK_ROOT, "tools", "trellis-core-0.6.15", "package");
const CORE_ARCHIVE = process.env.WISH_BUILDER_TEST_TRELLIS_CORE_ARCHIVE
  ?? path.join(DEFAULT_AUDIT_ROOT, "mindfoldhq-trellis-core-0.6.15.tgz");
const BRIDGE = path.join(
  REPOSITORY_ROOT,
  "wish_builder",
  "bridges",
  "trellis_core",
  "bridge.mjs",
);
const temporaryRoots = [];

after(async () => {
  for (const root of temporaryRoots) await rm(root, { recursive: true, force: true });
});

test("official Trellis tasks produce a deterministic Wish Builder-derived graph snapshot", async (t) => {
  if (!fixtureAvailable(t)) return;
  const core = await loadPinnedTrellisCore({ environment: coreEnvironment() });
  const fixture = await graphFixture(core.taskApi);
  const input = {
    checkoutRoot: fixture.root,
    parentTaskId: fixture.parentId,
    trellisVersion: "0.6.15",
    observedAt: "2026-08-20T01:00:00.000Z",
  };
  const first = await deriveTrellisGraphSnapshot(core.taskApi, input);
  const second = await deriveTrellisGraphSnapshot(core.taskApi, input);

  assert.equal(first.exportVersion, WISH_BUILDER_GRAPH_FORMAT);
  assert.equal(first.trellisVersion, "0.6.15");
  assert.equal(first.revision, second.revision);
  assert.deepEqual(first.snapshotBytes, second.snapshotBytes);
  assert.match(first.sourceSha256, /^sha256:[0-9a-f]{64}$/);
  const payload = JSON.parse(first.snapshotBytes.toString("utf8"));
  assert.equal(payload.parent_task_id, fixture.parentId);
  assert.equal(payload.revision, first.revision);
  assert.deepEqual(payload.tasks.map((item) => item.id), ["child-a", "child-b"]);
  assert.deepEqual(payload.tasks[1].depends_on, ["child-a"]);
});

test("derived graph refuses an incomplete Trellis parent-child membership", async (t) => {
  if (!fixtureAvailable(t)) return;
  const core = await loadPinnedTrellisCore({ environment: coreEnvironment() });
  const fixture = await graphFixture(core.taskApi, { omitDeclaredChild: true });
  await assert.rejects(
    deriveTrellisGraphSnapshot(core.taskApi, {
      checkoutRoot: fixture.root,
      parentTaskId: fixture.parentId,
      trellisVersion: "0.6.15",
      observedAt: "2026-08-20T01:00:00.000Z",
    }),
    /graph_membership_incomplete/,
  );
});

test("derived graph fails closed when task records change between store scans", async (t) => {
  if (!fixtureAvailable(t)) return;
  const core = await loadPinnedTrellisCore({ environment: coreEnvironment() });
  const fixture = await graphFixture(core.taskApi);
  let calls = 0;
  let first = null;
  const mutatingTaskApi = {
    ...core.taskApi,
    loadTaskRecord(input) {
      const record = core.taskApi.loadTaskRecord(input);
      calls += 1;
      if (calls === 1) first = { input, record };
      if (calls === 3) {
        core.taskApi.writeTaskRecord({
          taskDir: first.input.taskDir,
          record: { ...first.record, title: `${first.record.title} changed` },
        });
      }
      return record;
    },
  };

  await assert.rejects(
    deriveTrellisGraphSnapshot(mutatingTaskApi, {
      checkoutRoot: fixture.root,
      parentTaskId: fixture.parentId,
      trellisVersion: "0.6.15",
      observedAt: "2026-08-20T01:00:00.000Z",
    }),
    /graph_unstable_read/,
  );
});

test("official bridge exposes the derived graph without accepting a caller version", async (t) => {
  if (!fixtureAvailable(t)) return;
  const core = await loadPinnedTrellisCore({ environment: coreEnvironment() });
  const fixture = await graphFixture(core.taskApi);
  const request = {
    protocolVersion: 1,
    action: "graph_snapshot",
    checkoutRoot: fixture.root,
    parentTaskId: fixture.parentId,
    observedAt: "2026-08-20T01:00:00.000Z",
  };
  const result = spawnSync(process.execPath, [BRIDGE], {
    input: JSON.stringify(request),
    env: { ...process.env, ...coreEnvironment() },
    encoding: "utf8",
    maxBuffer: 16 * 1024 * 1024,
  });
  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
  assert.equal(result.stderr, "");
  const response = JSON.parse(result.stdout);
  assert.equal(response.action, "graph_snapshot");
  assert.equal(response.snapshot.trellisVersion, "0.6.15");
  assert.equal(response.snapshot.exportVersion, WISH_BUILDER_GRAPH_FORMAT);
  assert.equal(response.snapshot.parentTaskId, fixture.parentId);
  assert.equal(
    Buffer.from(response.snapshot.snapshotBase64, "base64").length,
    response.snapshot.byteLength,
  );

  const callerVersion = spawnSync(process.execPath, [BRIDGE], {
    input: JSON.stringify({ ...request, trellisVersion: "9.9.9" }),
    env: { ...process.env, ...coreEnvironment() },
    encoding: "utf8",
  });
  assert.equal(callerVersion.status, 2);
  assert.equal(JSON.parse(callerVersion.stdout).error.code, "INVALID_REQUEST");
});

test("Wish Builder-derived graph snapshot from official Trellis task records imports into manifest v2", async (t) => {
  if (!fixtureAvailable(t)) return;
  const core = await loadPinnedTrellisCore({ environment: coreEnvironment() });
  const fixture = await graphFixture(core.taskApi);
  const snapshot = await deriveTrellisGraphSnapshot(core.taskApi, {
    checkoutRoot: fixture.root,
    parentTaskId: fixture.parentId,
    trellisVersion: "0.6.15",
    observedAt: "2026-08-20T01:00:00.000Z",
  });
  const snapshotPath = path.join(fixture.root, "derived-graph.json");
  const settingsPath = path.join(fixture.root, "import-settings.json");
  await writeFile(snapshotPath, snapshot.snapshotBytes);
  await writeFile(settingsPath, JSON.stringify(importSettings(snapshot)));

  const repositoryPython = path.join(
    REPOSITORY_ROOT,
    ".venv",
    process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
  );
  const python = process.env.WISH_BUILDER_TEST_PYTHON
    ?? (existsSync(repositoryPython) ? repositoryPython : (process.platform === "win32" ? "py" : "python3"));
  const result = spawnSync(
    python,
    ["scripts/wishctl.py", "import-trellis", snapshotPath, settingsPath],
    { cwd: REPOSITORY_ROOT, encoding: "utf8", maxBuffer: 4 * 1024 * 1024 },
  );
  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
  const manifest = JSON.parse(result.stdout);
  assert.equal(manifest.schema_version, 2);
  assert.equal(manifest.trellis_parent_task_id, fixture.parentId);
  assert.equal(manifest.trellis_revision, snapshot.revision);
  assert.deepEqual(manifest.task_id_mapping, { "child-a": "TASK-001", "child-b": "TASK-002" });
  assert.match(manifest.trellis_graph_digest, /^sha256:[0-9a-f]{64}$/);
});

async function graphFixture(taskApi, options = {}) {
  const root = await mkdtemp(path.join(os.tmpdir(), "wish-builder-graph-"));
  temporaryRoots.push(root);
  await mkdir(path.join(root, ".git"));
  const taskRoot = path.join(root, ".trellis", "tasks");
  const parentId = "parent-wish";
  writeRecord(taskApi, path.join(taskRoot, "08-20-parent"), {
    id: parentId,
    name: parentId,
    title: "Parent wish",
    description: "Approved product and architecture context",
    creator: "architect",
    assignee: "architect",
    createdAt: "2026-08-20",
    children: options.omitDeclaredChild ? ["child-a"] : ["child-b", "child-a"],
    meta: {
      wish_builder: {
        schemaVersion: 1,
        requirements: [
          { id: "REQ-001", text: "Build the shared contract", status: "approved", decision_ref: null },
          { id: "REQ-002", text: "Build the independent feature", status: "approved", decision_ref: null },
        ],
      },
    },
  });
  writeRecord(taskApi, path.join(taskRoot, "08-20-child-b"), childRecord("child-b", parentId, {
    requirement_ids: ["REQ-002"],
    depends_on: ["child-a"],
    wave: 1,
  }));
  writeRecord(taskApi, path.join(taskRoot, "08-20-child-a"), childRecord("child-a", parentId, {
    requirement_ids: ["REQ-001"],
    depends_on: [],
    wave: 0,
    may_change_contracts: true,
  }));
  return { root, parentId };
}

function childRecord(id, parentId, overrides) {
  return {
    id,
    name: id,
    title: `Task ${id}`,
    description: `Execution task ${id}`,
    creator: "architect",
    assignee: "worker",
    createdAt: "2026-08-20",
    parent: parentId,
    meta: {
      wish_builder: {
        schemaVersion: 1,
        task: {
          requirement_ids: overrides.requirement_ids,
          depends_on: overrides.depends_on,
          owned_paths: [`src/${id}/**`],
          allowed_auxiliary_paths: [`.wish-builder/evidence/${id}/**`],
          acceptance_criteria: [`${id} passes acceptance`],
          regression_commands: [{
            executable_profile: "python",
            executable_identity_digest: `sha256:${"1".repeat(64)}`,
            argv: ["python", "-m", "unittest", "discover"],
            working_directory: ".",
            timeout_seconds: 300,
            stdout_limit_bytes: 1048576,
            stderr_limit_bytes: 1048576,
            result_limit_bytes: 262144,
            environment_allowlist: [],
            network_policy: "denied",
            display_text: "Run repository tests",
          }],
          rollback: "Revert the task commit.",
          documentation: [`docs/${id}.md`],
          wave: overrides.wave,
          risk: "low",
          may_change_contracts: overrides.may_change_contracts ?? false,
          instruction_context_digest: null,
          approved_document_digests: [],
          task_packet_template_digest: `sha256:${"5".repeat(64)}`,
        },
      },
    },
  };
}

function writeRecord(taskApi, taskDir, overrides) {
  taskApi.writeTaskRecord({ taskDir, record: taskApi.emptyTaskRecord(overrides) });
}

function fixtureAvailable(t) {
  const available = existsSync(CORE_ROOT) && existsSync(CORE_ARCHIVE);
  if (!available) t.skip("official Trellis 0.6.15 fixture is unavailable");
  return available;
}

function coreEnvironment() {
  return {
    WISH_BUILDER_TRELLIS_CORE_ROOT: CORE_ROOT,
    WISH_BUILDER_TRELLIS_CORE_ARCHIVE: CORE_ARCHIVE,
  };
}

function importSettings(snapshot) {
  const digest = (character) => `sha256:${character.repeat(64)}`;
  return {
    export_version: snapshot.exportVersion,
    trellis_version: snapshot.trellisVersion,
    parent_task_id: snapshot.parentTaskId,
    revision: snapshot.revision,
    observed_at: snapshot.observedAt,
    run_id: "WISH-2026-0615",
    goal: "Import official Trellis 0.6.15 tasks",
    base_branch: "main",
    imported_at: snapshot.observedAt,
    gate_a: {
      approved_by: "architect",
      approved_at: "2026-08-20T00:30:00Z",
      artifact_hash: digest("a"),
    },
    provider: "codex",
    capability_digest: digest("b"),
    launch_profile_digest: digest("c"),
    policy_digest: digest("d"),
    execution_budget: {
      max_attempts_per_task: 2,
      max_attempts_per_run: 4,
      attempt_deadline_seconds: 1800,
      total_worker_seconds: 7200,
      max_output_bytes: 8388608,
      max_retained_evidence_bytes: 16777216,
      max_concurrent_workers: 2,
      billing_posture: "preapproved",
    },
    max_concurrency: 2,
    lease_ttl_seconds: 90,
    lease_clock_skew_seconds: 2,
    path_case_mode: "insensitive",
    protected_paths: ["db/schema/**"],
  };
}
