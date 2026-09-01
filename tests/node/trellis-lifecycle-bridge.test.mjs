import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { existsSync } from "node:fs";
import { mkdir, mkdtemp, readFile, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import test, { after } from "node:test";

import {
  applyTrellisLifecycle,
  inspectTrellisLifecycle,
} from "../../wish_builder/bridges/trellis_core/lifecycle.mjs";

const CORE_ROOT = path.resolve(
  "../work/tools/trellis-core-0.6.15/package",
);
const temporaryRoots = [];

after(async () => {
  for (const root of temporaryRoots) await rm(root, { recursive: true, force: true });
});

test("lifecycle adapter persists official task-record operations idempotently", async (t) => {
  const fixture = await lifecycleFixture(t);
  if (fixture === null) return;
  const prepare = command("prepare_attempt", {
    attempt: 1,
    dispatch_id: "DISPATCH-1",
    expected_base_commit: "0123456789012345678901234567890123456789",
    manifest_digest: digest("1"),
    operation_id: "ATTEMPT-1",
    parent_task_id: "parent-1",
    run_id: "run-1",
    task_id: "task-1",
    trellis_graph_digest: digest("2"),
    trellis_task_id: "task-1",
  });
  const prepareRequest = applyRequest(fixture.root, prepare, {
    worktreePath: path.join(fixture.root, "attempt-1"),
    worktreeId: "worktree-1",
  });
  const first = await applyTrellisLifecycle(fixture.api, prepareRequest);
  assert.equal(first.status, "applied");
  const replay = await applyTrellisLifecycle(fixture.api, prepareRequest);
  assert.deepEqual(replay, first);

  const check = command("check_attempt", {
    attempt_id: "ATTEMPT-1",
    expected_head_commit: "0123456789012345678901234567890123456789",
    operation_id: "CHECK-1",
    task_id: "task-1",
    task_packet_digest: digest("3"),
    trellis_task_id: "task-1",
  });
  const checked = await applyTrellisLifecycle(
    fixture.api,
    applyRequest(fixture.root, check),
  );
  assert.equal(checked.status, "applied");
  assert.equal(checked.passed, true);

  const finish = command("finish_attempt", {
    attempt_id: "ATTEMPT-1",
    delivered_commit: "0123456789012345678901234567890123456789",
    delivery_evidence_digest: digest("4"),
    operation_id: "FINISH-1",
    task_id: "task-1",
    trellis_task_id: "task-1",
  });
  const finished = await applyTrellisLifecycle(
    fixture.api,
    applyRequest(fixture.root, finish),
  );
  assert.equal(finished.status, "applied");
  assert.equal(finished.finished, true);
  assert.equal(
    JSON.parse(await readFile(fixture.taskFile, "utf8")).status,
    "planning",
    "lifecycle adapter must not complete the Trellis task",
  );
});

test("lifecycle adapter fails closed on collisions, missing dependencies, and uncertain writes", async (t) => {
  const fixture = await lifecycleFixture(t, "task-2");
  if (fixture === null) return;
  const prepare = command("prepare_attempt", {
    attempt: 1,
    dispatch_id: "DISPATCH-2",
    expected_base_commit: "0123456789012345678901234567890123456789",
    manifest_digest: digest("1"),
    operation_id: "ATTEMPT-2",
    parent_task_id: "parent-1",
    run_id: "run-1",
    task_id: "task-2",
    trellis_graph_digest: digest("2"),
    trellis_task_id: "task-2",
  });
  const request = applyRequest(fixture.root, prepare, {
    worktreePath: path.join(fixture.root, "attempt-2"),
    worktreeId: "worktree-2",
  });
  const crashingApi = Object.freeze({
    loadTaskRecord: fixture.api.loadTaskRecord,
    writeTaskRecord(options) {
      fixture.api.writeTaskRecord(options);
      throw new Error("simulated crash after write");
    },
  });
  await assert.rejects(
    applyTrellisLifecycle(crashingApi, request),
    (error) => error?.code === "lifecycle_write_outcome_unknown",
  );
  const reconciled = await applyTrellisLifecycle(fixture.api, request);
  assert.equal(reconciled.status, "applied");

  const collision = await applyTrellisLifecycle(fixture.api, {
    ...request,
    command: { ...request.command, dispatch_id: "DISPATCH-OTHER" },
    commandHash: commandHash({ ...request.command, dispatch_id: "DISPATCH-OTHER" }),
  });
  assert.equal(collision.status, "unknown");
  assert.match(collision.evidence[0], /^operation_id_collision:/);

  const missingCheck = command("check_attempt", {
    attempt_id: "NEVER-PREPARED",
    expected_head_commit: "0123456789012345678901234567890123456789",
    operation_id: "CHECK-MISSING",
    task_id: "task-2",
    task_packet_digest: digest("3"),
    trellis_task_id: "task-2",
  });
  const missing = await applyTrellisLifecycle(
    fixture.api,
    applyRequest(fixture.root, missingCheck),
  );
  assert.equal(missing.status, "unknown");
  assert.match(missing.evidence[0], /^attempt_not_prepared:/);

  const inspected = await inspectTrellisLifecycle(fixture.api, {
    checkoutRoot: fixture.root,
    trellisTaskId: "task-2",
    operationKind: "prepare_attempt",
    operationId: "ATTEMPT-2",
    expectedRequestPayloadHash: digest("f"),
  });
  assert.equal(inspected.status, "unknown");
  assert.match(inspected.evidence[0], /^request_hash_mismatch:/);
  assert.equal(
    existsSync(path.join(fixture.root, ".trellis", ".wish-builder-lifecycle-writer.lock")),
    false,
  );
});

async function lifecycleFixture(t, taskId = "task-1") {
  if (!existsSync(CORE_ROOT)) {
    t.skip("local pinned Trellis Core 0.6.15 fixture is unavailable");
    return null;
  }
  const api = await import(
    pathToFileURL(path.join(CORE_ROOT, "dist", "task", "index.js")).href,
  );
  const root = await mkdtemp(path.join(os.tmpdir(), "wish-builder-lifecycle-"));
  temporaryRoots.push(root);
  await mkdir(path.join(root, ".git"));
  const taskDir = path.join(root, ".trellis", "tasks", `08-31-${taskId}`);
  api.writeTaskRecord({
    taskDir,
    record: api.emptyTaskRecord({
      id: taskId,
      name: taskId,
      title: "Lifecycle fixture",
      description: "Official Trellis Core lifecycle integration",
      creator: "wish-builder-test",
      assignee: "wish-builder-test",
      createdAt: "2026-08-31",
    }),
  });
  return { api, root, taskFile: path.join(taskDir, "task.json") };
}

function command(kind, fields) {
  return {
    ...fields,
    command_type: kind,
    schema_version: 1,
  };
}

function applyRequest(root, commandValue, extra = {}) {
  const kind = commandValue.command_type;
  return {
    checkoutRoot: root,
    operationKind: kind,
    commandHash: commandHash(commandValue),
    command: commandValue,
    worktreePath: extra.worktreePath ?? null,
    worktreeId: extra.worktreeId ?? null,
  };
}

function commandHash(value) {
  const sorted = Object.fromEntries(
    Object.keys(value).sort().map((key) => [key, value[key]]),
  );
  return `sha256:${createHash("sha256").update(`${JSON.stringify(sorted)}\n`).digest("hex")}`;
}

function digest(character) {
  return `sha256:${character.repeat(64)}`;
}
