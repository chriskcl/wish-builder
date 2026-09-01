import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { once } from "node:events";
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

test("lifecycle writer rejects a concurrent owner and recovers after that process is killed", async (t) => {
  const fixture = await lifecycleFixture(t, "task-lock-recovery");
  if (fixture === null) return;
  const prepare = command("prepare_attempt", {
    attempt: 1,
    dispatch_id: "DISPATCH-LOCK-RECOVERY",
    expected_base_commit: "0123456789012345678901234567890123456789",
    manifest_digest: digest("1"),
    operation_id: "ATTEMPT-LOCK-RECOVERY",
    parent_task_id: "parent-1",
    run_id: "run-1",
    task_id: "task-lock-recovery",
    trellis_graph_digest: digest("2"),
    trellis_task_id: "task-lock-recovery",
  });
  const request = applyRequest(fixture.root, prepare, {
    worktreePath: path.join(fixture.root, "attempt-lock-recovery"),
    worktreeId: "worktree-lock-recovery",
  });
  const holder = spawnLifecycleHolder(fixture, request);
  t.after(async () => {
    if (holder.exitCode === null && holder.signalCode === null) {
      const exited = once(holder, "exit");
      holder.kill("SIGKILL");
      await exited;
    }
  });
  await waitForOutput(holder, "LOCK_ACQUIRED");

  await assert.rejects(
    applyTrellisLifecycle(fixture.api, request),
    (error) => error?.code === "lifecycle_writer_busy",
  );

  const exited = once(holder, "exit");
  assert.equal(holder.kill("SIGKILL"), true);
  await exited;
  const recovered = await applyTrellisLifecycle(fixture.api, request);
  assert.equal(recovered.status, "applied");
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

function spawnLifecycleHolder(fixture, request) {
  const lifecycleUrl = pathToFileURL(path.resolve(
    "wish_builder/bridges/trellis_core/lifecycle.mjs",
  )).href;
  const coreUrl = pathToFileURL(path.join(CORE_ROOT, "dist", "task", "index.js")).href;
  const source = `
    const [lifecycleUrl, coreUrl, requestJson] = process.argv.slice(1);
    const [{ applyTrellisLifecycle }, taskApi] = await Promise.all([
      import(lifecycleUrl),
      import(coreUrl),
    ]);
    const blockingApi = Object.freeze({
      loadTaskRecord: taskApi.loadTaskRecord,
      async writeTaskRecord() {
        process.stdout.write("LOCK_ACQUIRED\\n");
        await new Promise(() => {});
      },
    });
    await applyTrellisLifecycle(blockingApi, JSON.parse(requestJson));
  `;
  return spawn(
    process.execPath,
    ["--input-type=module", "--eval", source, lifecycleUrl, coreUrl, JSON.stringify(request)],
    { stdio: ["ignore", "pipe", "pipe"], windowsHide: true },
  );
}

function waitForOutput(child, marker) {
  return new Promise((resolve, reject) => {
    let stdout = "";
    let stderr = "";
    const timeout = setTimeout(() => finish(new Error(
      `timed out waiting for ${marker}; stdout=${stdout}; stderr=${stderr}`,
    )), 10_000);
    const onStdout = (chunk) => {
      stdout += chunk.toString("utf8");
      if (stdout.split(/\r?\n/u).includes(marker)) finish();
    };
    const onStderr = (chunk) => { stderr += chunk.toString("utf8"); };
    const onError = (error) => finish(error);
    const onExit = (code, signal) => finish(new Error(
      `holder exited before ${marker}; code=${code}; signal=${signal}; stderr=${stderr}`,
    ));
    const finish = (error) => {
      clearTimeout(timeout);
      child.stdout.off("data", onStdout);
      child.stderr.off("data", onStderr);
      child.off("error", onError);
      child.off("exit", onExit);
      if (error) reject(error); else resolve();
    };
    child.stdout.on("data", onStdout);
    child.stderr.on("data", onStderr);
    child.once("error", onError);
    child.once("exit", onExit);
  });
}
