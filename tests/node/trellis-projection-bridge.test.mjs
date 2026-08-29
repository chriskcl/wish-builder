import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { appendFileSync, existsSync } from "node:fs";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath, pathToFileURL } from "node:url";
import test, { after } from "node:test";

import {
  applyTrellisTaskProjection,
  inspectTrellisTaskProjection,
} from "../../wish_builder/bridges/trellis_core/projection.mjs";

const REPOSITORY_ROOT = path.resolve(fileURLToPath(new URL("../..", import.meta.url)));
const WORK_ROOT = path.resolve(REPOSITORY_ROOT, "..", "work");
const BRIDGE = path.join(
  REPOSITORY_ROOT,
  "wish_builder",
  "bridges",
  "trellis_core",
  "bridge.mjs",
);
const CORE_ROOT = path.join(WORK_ROOT, "tools", "trellis-core-0.6.15", "package");
const OFFICIAL_CORE_ROOT = CORE_ROOT;
const CORE_ARCHIVE = path.join(
  WORK_ROOT,
  "artifacts",
  "trellis-0.6.15",
  "mindfoldhq-trellis-core-0.6.15.tgz",
);
const temporaryRoots = [];

after(async () => {
  for (const root of temporaryRoots) await rm(root, { recursive: true, force: true });
});

test("projection uses the official Trellis Core 0.6.15 load/write surface", async (t) => {
  const fixture = await officialProjectionFixture(t);
  if (fixture === null) return;

  assert.deepEqual(Object.keys(fixture.taskApi).sort(), [
    "loadTaskRecord",
    "writeTaskRecord",
  ]);
  const initial = await inspectTrellisTaskProjection(
    fixture.taskApi,
    projectionDirectInspect(fixture.root, fixture.taskId),
  );
  assert.equal(initial.recordRevision, rawRevision(await readFile(fixture.taskFile)));

  const applied = await applyTrellisTaskProjection(
    fixture.taskApi,
    projectionDirectApply(
      fixture.root,
      fixture.taskId,
      initial.recordRevision,
      projectionTarget(fixture.taskId, 1),
    ),
  );
  assert.equal(applied.disposition, "applied");
  assert.equal(applied.recordRevision, rawRevision(await readFile(fixture.taskFile)));
  assert.equal(applied.taskStatus, "in_progress");
  assert.equal(
    existsSync(path.join(fixture.root, ".trellis", ".wish-builder-projection-writer.lock")),
    false,
  );
  const stored = JSON.parse(await readFile(fixture.taskFile, "utf8"));
  assert.deepEqual(stored.externalExtension, { preserved: true });
});

test("projection fails closed on an unstable official load", async (t) => {
  const fixture = await officialProjectionFixture(t, "unstable-task");
  if (fixture === null) return;
  let mutate = true;
  const unstableApi = Object.freeze({
    loadTaskRecord(options) {
      const record = fixture.taskApi.loadTaskRecord(options);
      if (mutate) {
        mutate = false;
        appendFileSync(fixture.taskFile, " ");
      }
      return record;
    },
    writeTaskRecord: fixture.taskApi.writeTaskRecord,
  });

  await assert.rejects(
    inspectTrellisTaskProjection(
      unstableApi,
      projectionDirectInspect(fixture.root, fixture.taskId),
    ),
    (error) => error?.code === "projection_unstable_read",
  );
});

test("projection writer lock and post-write verification fail closed", async (t) => {
  const fixture = await officialProjectionFixture(t, "locked-task");
  if (fixture === null) return;
  const initial = await inspectTrellisTaskProjection(
    fixture.taskApi,
    projectionDirectInspect(fixture.root, fixture.taskId),
  );
  const request = projectionDirectApply(
    fixture.root,
    fixture.taskId,
    initial.recordRevision,
    projectionTarget(fixture.taskId, 1),
  );
  const lock = path.join(
    fixture.root,
    ".trellis",
    ".wish-builder-projection-writer.lock",
  );
  const before = await readFile(fixture.taskFile);
  await writeFile(lock, "another-writer", "utf8");
  await assert.rejects(
    applyTrellisTaskProjection(fixture.taskApi, request),
    (error) => error?.code === "projection_writer_busy",
  );
  assert.deepEqual(await readFile(fixture.taskFile), before);
  await rm(lock);

  const noWriteApi = Object.freeze({
    loadTaskRecord: fixture.taskApi.loadTaskRecord,
    writeTaskRecord() {},
  });
  await assert.rejects(
    applyTrellisTaskProjection(noWriteApi, request),
    (error) => error?.code === "projection_write_unverified",
  );
  assert.deepEqual(await readFile(fixture.taskFile), before);
  assert.equal(existsSync(lock), false);

  const driftedWriteApi = Object.freeze({
    loadTaskRecord: fixture.taskApi.loadTaskRecord,
    writeTaskRecord(options) {
      fixture.taskApi.writeTaskRecord({
        ...options,
        record: { ...options.record, title: "Unexpected concurrent title" },
      });
    },
  });
  await assert.rejects(
    applyTrellisTaskProjection(driftedWriteApi, request),
    (error) => error?.code === "projection_write_unverified",
  );
  assert.equal(existsSync(lock), false);
});

test("projection writer lock rejects a concurrent writer until the first write settles", async (t) => {
  const fixture = await officialProjectionFixture(t, "contended-task");
  if (fixture === null) return;
  const initial = await inspectTrellisTaskProjection(
    fixture.taskApi,
    projectionDirectInspect(fixture.root, fixture.taskId),
  );
  const request = projectionDirectApply(
    fixture.root,
    fixture.taskId,
    initial.recordRevision,
    projectionTarget(fixture.taskId, 1),
  );
  const writeStarted = deferred();
  const releaseWrite = deferred();
  const delayedWriteApi = Object.freeze({
    loadTaskRecord: fixture.taskApi.loadTaskRecord,
    async writeTaskRecord(options) {
      writeStarted.resolve();
      await releaseWrite.promise;
      fixture.taskApi.writeTaskRecord(options);
    },
  });

  const firstWriter = applyTrellisTaskProjection(delayedWriteApi, request);
  await writeStarted.promise;

  await assert.rejects(
    applyTrellisTaskProjection(fixture.taskApi, request),
    (error) => error?.code === "projection_writer_busy",
  );

  releaseWrite.resolve();
  const applied = await firstWriter;
  assert.equal(applied.disposition, "applied");
  assert.equal(applied.taskStatus, "in_progress");
  assert.equal(
    existsSync(path.join(fixture.root, ".trellis", ".wish-builder-projection-writer.lock")),
    false,
  );
});

test("projection bridge applies, repairs, and rejects ahead or drifted state", async (t) => {
  const fixture = await projectionFixture(t);
  if (fixture === null) return;

  const initial = runBridge(projectionInspect(fixture.root, fixture.taskId));
  assert.equal(
    initial.status,
    0,
    `${initial.stderr}\n${JSON.stringify(initial.body)}`,
  );
  assert.equal(initial.body.projection.disposition, "inspected");
  assert.equal(initial.body.projection.projection, null);

  const sequenceTen = projectionTarget(fixture.taskId, 10);
  const applied = runBridge(
    projectionApply(
      fixture.root,
      fixture.taskId,
      initial.body.projection.recordRevision,
      sequenceTen,
    ),
  );
  assert.equal(applied.status, 0, applied.stderr);
  assert.equal(applied.body.projection.disposition, "applied");
  assert.equal(applied.body.projection.taskStatus, "in_progress");

  const idempotent = runBridge(
    projectionApply(
      fixture.root,
      fixture.taskId,
      applied.body.projection.recordRevision,
      sequenceTen,
    ),
  );
  assert.equal(idempotent.body.projection.disposition, "idempotent");

  const repaired = runBridge(
    projectionApply(
      fixture.root,
      fixture.taskId,
      applied.body.projection.recordRevision,
      projectionTarget(fixture.taskId, 11, {
        canonicalState: "verified",
        targetStatus: "completed",
        summary: "Canonical verification completed.",
      }),
    ),
  );
  assert.equal(repaired.body.projection.disposition, "applied");
  assert.equal(repaired.body.projection.taskStatus, "completed");

  const staleRevision = runBridge(
    projectionApply(
      fixture.root,
      fixture.taskId,
      applied.body.projection.recordRevision,
      projectionTarget(fixture.taskId, 12),
    ),
  );
  assert.equal(staleRevision.body.projection.disposition, "conflict");
  assert.equal(staleRevision.body.projection.reason, "revision_conflict");

  const ahead = runBridge(
    projectionApply(
      fixture.root,
      fixture.taskId,
      repaired.body.projection.recordRevision,
      projectionTarget(fixture.taskId, 10),
    ),
  );
  assert.equal(ahead.body.projection.disposition, "conflict");
  assert.equal(ahead.body.projection.reason, "ahead");

  const digestMismatch = runBridge(
    projectionApply(
      fixture.root,
      fixture.taskId,
      repaired.body.projection.recordRevision,
      projectionTarget(fixture.taskId, 11, {
        canonicalState: "verified",
        targetStatus: "completed",
        summary: "Different bytes at the same canonical sequence.",
      }),
    ),
  );
  assert.equal(digestMismatch.body.projection.disposition, "conflict");
  assert.equal(digestMismatch.body.projection.reason, "digest_mismatch");
});

test("projection bridge never writes outside its explicit checkout", async (t) => {
  const source = await projectionFixture(t, "source-task");
  const projection = await projectionFixture(t, "projection-task");
  if (source === null || projection === null) return;
  const before = await readFile(source.taskFile);
  const inspected = runBridge(projectionInspect(projection.root, projection.taskId));
  assert.equal(
    inspected.status,
    0,
    `${inspected.stderr}\n${JSON.stringify(inspected.body)}`,
  );
  const applied = runBridge(
    projectionApply(
      projection.root,
      projection.taskId,
      inspected.body.projection.recordRevision,
      projectionTarget(projection.taskId, 1),
    ),
  );
  assert.equal(applied.body.projection.disposition, "applied");
  assert.deepEqual(await readFile(source.taskFile), before);
});

async function projectionFixture(t, taskId = "trellis-task") {
  if (!existsSync(CORE_ROOT) || !existsSync(CORE_ARCHIVE)) {
    t.skip("local pinned Trellis Core fixture is unavailable");
    return null;
  }
  const taskApi = await import(
    pathToFileURL(path.join(CORE_ROOT, "dist", "task", "index.js")).href
  );
  const root = await mkdtemp(path.join(os.tmpdir(), "wish-builder-projection-"));
  temporaryRoots.push(root);
  await mkdir(path.join(root, ".git"));
  const taskDir = path.join(root, ".trellis", "tasks", `08-19-${taskId}`);
  taskApi.writeTaskRecord({
    taskDir,
    record: taskApi.emptyTaskRecord({
      id: taskId,
      name: taskId,
      title: "Projection fixture",
      description: "Independent Trellis projection checkout",
      status: "planning",
      creator: "wish-builder-test",
      assignee: "wish-builder-test",
      createdAt: "2026-08-19",
    }),
  });
  return { root, taskDir, taskId, taskFile: path.join(taskDir, "task.json") };
}

async function officialProjectionFixture(t, taskId = "official-task") {
  if (!existsSync(OFFICIAL_CORE_ROOT)) {
    t.skip("official Trellis Core 0.6.15 fixture is unavailable");
    return null;
  }
  const official = await import(
    pathToFileURL(path.join(OFFICIAL_CORE_ROOT, "dist", "task", "index.js")).href
  );
  const taskApi = Object.freeze({
    loadTaskRecord: official.loadTaskRecord,
    writeTaskRecord: official.writeTaskRecord,
  });
  const root = await mkdtemp(path.join(os.tmpdir(), "wish-builder-official-projection-"));
  temporaryRoots.push(root);
  await mkdir(path.join(root, ".git"));
  const taskDir = path.join(root, ".trellis", "tasks", `08-20-${taskId}`);
  taskApi.writeTaskRecord({
    taskDir,
    record: official.emptyTaskRecord({
      id: taskId,
      name: taskId,
      title: "Official projection fixture",
      description: "Trellis Core 0.6.15 load/write integration",
      status: "planning",
      creator: "wish-builder-test",
      assignee: "wish-builder-test",
      createdAt: "2026-08-20",
    }),
  });
  const taskFile = path.join(taskDir, "task.json");
  const stored = JSON.parse(await readFile(taskFile, "utf8"));
  stored.externalExtension = { preserved: true };
  await writeFile(taskFile, `${JSON.stringify(stored, null, 2)}\n`, "utf8");
  return { root, taskDir, taskId, taskFile, taskApi };
}

function projectionInspect(checkoutRoot, trellisTaskId) {
  return {
    protocolVersion: 1,
    action: "projection_inspect",
    checkoutRoot,
    trellisTaskId,
  };
}

function projectionDirectInspect(checkoutRoot, trellisTaskId) {
  return { checkoutRoot, trellisTaskId };
}

function projectionApply(checkoutRoot, trellisTaskId, expectedRevision, projection) {
  return {
    protocolVersion: 1,
    action: "projection_apply",
    checkoutRoot,
    trellisTaskId,
    expectedRevision,
    projection,
  };
}

function projectionDirectApply(
  checkoutRoot,
  trellisTaskId,
  expectedRevision,
  projection,
) {
  return { checkoutRoot, trellisTaskId, expectedRevision, projection };
}

function projectionTarget(trellisTaskId, canonicalSequence, overrides = {}) {
  return {
    schemaVersion: 1,
    operationId: `projection-${canonicalSequence}`,
    runId: "run-1",
    taskId: "task-1",
    trellisTaskId,
    manifestDigest: `sha256:${"1".repeat(64)}`,
    trellisGraphDigest: `sha256:${"2".repeat(64)}`,
    canonicalSequence,
    canonicalEventHash: `sha256:${canonicalSequence.toString(16).padStart(64, "0")}`,
    canonicalState: overrides.canonicalState ?? "dispatched",
    targetStatus: overrides.targetStatus ?? "in_progress",
    evidenceDigests: [`sha256:${"3".repeat(64)}`],
    summary: overrides.summary ?? "Canonical task transition recorded.",
  };
}

function rawRevision(bytes) {
  return `sha256:${createHash("sha256").update(bytes).digest("hex")}`;
}

function deferred() {
  let resolve;
  const promise = new Promise((fulfill) => {
    resolve = fulfill;
  });
  return Object.freeze({ promise, resolve });
}

function runBridge(request) {
  const result = spawnSync(process.execPath, [BRIDGE], {
    cwd: REPOSITORY_ROOT,
    encoding: "utf8",
    input: JSON.stringify(request),
    env: {
      ...process.env,
      WISH_BUILDER_TRELLIS_CORE_ROOT: CORE_ROOT,
      WISH_BUILDER_TRELLIS_CORE_ARCHIVE: CORE_ARCHIVE,
    },
  });
  return {
    status: result.status,
    stderr: result.stderr,
    body: JSON.parse(result.stdout),
  };
}
