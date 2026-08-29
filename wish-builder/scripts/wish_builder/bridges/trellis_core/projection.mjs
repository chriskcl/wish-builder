import { createHash, randomBytes } from "node:crypto";
import { lstat, open, opendir, readFile, realpath, unlink } from "node:fs/promises";
import path from "node:path";

import { parseStrictJsonBytes } from "./strict-json.mjs";

const MAX_TASK_DIRECTORIES = 4096;
const MAX_TASK_RECORD_BYTES = 2 * 1024 * 1024;
const PROJECTION_META_KEY = "wish_builder_projection";
const PROJECTION_WRITER_LOCK = ".wish-builder-projection-writer.lock";
const TASK_RECORD_FIELDS = Object.freeze([
  "id",
  "name",
  "title",
  "description",
  "status",
  "dev_type",
  "scope",
  "package",
  "priority",
  "creator",
  "assignee",
  "createdAt",
  "completedAt",
  "branch",
  "base_branch",
  "worktree_path",
  "commit",
  "pr_url",
  "subtasks",
  "children",
  "parent",
  "relatedFiles",
  "notes",
  "meta",
]);
const SHA256 = /^sha256:[0-9a-f]{64}$/;
const TOKEN = /^[A-Za-z0-9][-A-Za-z0-9._:@/+]{0,255}$/;

export class TrellisProjectionError extends Error {
  constructor(code) {
    super(code);
    this.name = "TrellisProjectionError";
    this.code = code;
  }
}

export async function inspectTrellisTaskProjection(taskApi, input) {
  assertTaskApi(taskApi);
  const request = validateInspectInput(input);
  const context = await locateTask(taskApi, request.checkoutRoot, request.trellisTaskId);
  return projectionSnapshot(context);
}

export async function applyTrellisTaskProjection(taskApi, input) {
  assertTaskApi(taskApi);
  const request = validateApplyInput(input);
  const located = await locateTask(taskApi, request.checkoutRoot, request.trellisTaskId);
  return withProjectionWriter(located.checkoutRoot, async () => {
    // Trellis 0.6.15 has no cross-process CAS.  Re-read only after acquiring
    // Wish Builder's single-writer lock and bind the write to the derived
    // pre-image digest supplied by the caller.
    const context = await locateTask(taskApi, request.checkoutRoot, request.trellisTaskId);
    const existing = projectionMetadata(context.snapshot.record.meta);
    const incoming = Object.freeze({
      ...request.projection,
      projectionDigest: projectionDigest(request.projection),
    });

    if (context.snapshot.revision !== request.expectedRevision) {
      return conflictSnapshot(context, existing, "revision_conflict");
    }
    const relation = compareProjection(existing, incoming);
    if (relation === "idempotent") {
      if (context.snapshot.record.status !== incoming.targetStatus) {
        return conflictSnapshot(context, existing, "status_mismatch");
      }
      return Object.freeze({
        ...projectionSnapshot(context),
        disposition: "idempotent",
        reason: "none",
      });
    }
    if (relation !== "behind") {
      return conflictSnapshot(context, existing, relation);
    }
    if (
      existing !== null &&
      context.snapshot.record.status !== existing.targetStatus
    ) {
      return conflictSnapshot(context, existing, "status_mismatch");
    }

    const beforeWrite = await stableTaskSnapshot(
      taskApi,
      context.taskDir,
      context.checkoutRoot,
    );
    if (beforeWrite.revision !== context.snapshot.revision) {
      const changed = Object.freeze({ ...context, snapshot: beforeWrite });
      return conflictSnapshot(
        changed,
        projectionMetadata(beforeWrite.record.meta),
        "revision_conflict",
      );
    }

    const record = Object.freeze({
      ...beforeWrite.record,
      status: incoming.targetStatus,
      meta: Object.freeze({
        ...beforeWrite.record.meta,
        [PROJECTION_META_KEY]: incoming,
      }),
    });
    try {
      await taskApi.writeTaskRecord({
        taskDir: context.taskDir,
        cwd: context.checkoutRoot,
        record,
      });
    } catch {
      throw new TrellisProjectionError("projection_write_outcome_unknown");
    }

    let verified;
    let verifiedProjection;
    try {
      verified = await stableTaskSnapshot(
        taskApi,
        context.taskDir,
        context.checkoutRoot,
      );
      verifiedProjection = projectionMetadata(verified.record.meta);
      if (
        verified.revision === beforeWrite.revision ||
        canonicalDigest(verified.record) !== canonicalDigest(record) ||
        verified.record.id !== request.trellisTaskId ||
        verified.record.status !== record.status ||
        verifiedProjection === null ||
        verifiedProjection.projectionDigest !== incoming.projectionDigest
      ) {
        throw new Error("written projection does not match the requested task state");
      }
    } catch {
      throw new TrellisProjectionError("projection_write_unverified");
    }
    return Object.freeze({
      disposition: "applied",
      reason: "none",
      recordRevision: verified.revision,
      byteLength: verified.byteLength,
      taskStatus: verified.record.status,
      projection: verifiedProjection,
    });
  });
}

function validateInspectInput(value) {
  const source = exactObject(value, ["checkoutRoot", "trellisTaskId"]);
  if (typeof source.checkoutRoot !== "string" || !path.isAbsolute(source.checkoutRoot)) {
    throw new TrellisProjectionError("projection_checkout_invalid");
  }
  if (source.checkoutRoot.length > 1024 || source.checkoutRoot.includes("\0")) {
    throw new TrellisProjectionError("projection_checkout_invalid");
  }
  return Object.freeze({
    checkoutRoot: path.resolve(source.checkoutRoot),
    trellisTaskId: stableText(source.trellisTaskId, "trellis_task_id", 512),
  });
}

function validateApplyInput(value) {
  const source = exactObject(value, [
    "checkoutRoot",
    "trellisTaskId",
    "expectedRevision",
    "projection",
  ]);
  const inspected = validateInspectInput({
    checkoutRoot: source.checkoutRoot,
    trellisTaskId: source.trellisTaskId,
  });
  return Object.freeze({
    ...inspected,
    expectedRevision: digest(source.expectedRevision, "expected_revision"),
    projection: validateProjection(source.projection, inspected.trellisTaskId),
  });
}

function validateProjection(value, trellisTaskId) {
  const source = exactObject(value, [
    "schemaVersion",
    "operationId",
    "runId",
    "taskId",
    "trellisTaskId",
    "manifestDigest",
    "trellisGraphDigest",
    "canonicalSequence",
    "canonicalEventHash",
    "canonicalState",
    "targetStatus",
    "evidenceDigests",
    "summary",
  ]);
  if (source.schemaVersion !== 1) {
    throw new TrellisProjectionError("projection_schema_invalid");
  }
  if (!Number.isSafeInteger(source.canonicalSequence) || source.canonicalSequence < 1) {
    throw new TrellisProjectionError("projection_sequence_invalid");
  }
  if (!Array.isArray(source.evidenceDigests) || source.evidenceDigests.length > 32) {
    throw new TrellisProjectionError("projection_evidence_invalid");
  }
  const evidenceDigests = source.evidenceDigests.map((item) =>
    digest(item, "evidence_digest"),
  );
  evidenceDigests.sort(compareUtf8);
  if (new Set(evidenceDigests).size !== evidenceDigests.length) {
    throw new TrellisProjectionError("projection_evidence_invalid");
  }
  const projectedTaskId = stableText(source.trellisTaskId, "trellis_task_id", 512);
  if (projectedTaskId !== trellisTaskId) {
    throw new TrellisProjectionError("projection_task_identity_mismatch");
  }
  return Object.freeze({
    schemaVersion: 1,
    operationId: token(source.operationId, "operation_id"),
    runId: token(source.runId, "run_id"),
    taskId: token(source.taskId, "task_id"),
    trellisTaskId: projectedTaskId,
    manifestDigest: digest(source.manifestDigest, "manifest_digest"),
    trellisGraphDigest: digest(source.trellisGraphDigest, "trellis_graph_digest"),
    canonicalSequence: source.canonicalSequence,
    canonicalEventHash: digest(source.canonicalEventHash, "canonical_event_hash"),
    canonicalState: token(source.canonicalState, "canonical_state"),
    targetStatus: token(source.targetStatus, "target_status"),
    evidenceDigests: Object.freeze(evidenceDigests),
    summary: stableText(source.summary, "summary", 1024),
  });
}

async function locateTask(taskApi, requestedRoot, trellisTaskId) {
  let rootStat;
  try {
    rootStat = await lstat(requestedRoot);
  } catch {
    throw new TrellisProjectionError("projection_checkout_missing");
  }
  if (!rootStat.isDirectory() || rootStat.isSymbolicLink()) {
    throw new TrellisProjectionError("projection_checkout_unsafe");
  }
  const checkoutRoot = await realpath(requestedRoot).catch(() => {
    throw new TrellisProjectionError("projection_checkout_unreadable");
  });
  const gitMarker = path.join(checkoutRoot, ".git");
  const gitStat = await lstat(gitMarker).catch(() => {
    throw new TrellisProjectionError("projection_checkout_not_git");
  });
  if (gitStat.isSymbolicLink() || (!gitStat.isFile() && !gitStat.isDirectory())) {
    throw new TrellisProjectionError("projection_checkout_not_git");
  }
  const tasksRoot = path.join(checkoutRoot, ".trellis", "tasks");
  const trellisRoot = path.join(checkoutRoot, ".trellis");
  const trellisStat = await lstat(trellisRoot).catch(() => {
    throw new TrellisProjectionError("projection_task_store_missing");
  });
  if (!trellisStat.isDirectory() || trellisStat.isSymbolicLink()) {
    throw new TrellisProjectionError("projection_task_store_unsafe");
  }
  const tasksStat = await lstat(tasksRoot).catch(() => {
    throw new TrellisProjectionError("projection_task_store_missing");
  });
  if (!tasksStat.isDirectory() || tasksStat.isSymbolicLink()) {
    throw new TrellisProjectionError("projection_task_store_unsafe");
  }
  const canonicalTasksRoot = await realpath(tasksRoot).catch(() => {
    throw new TrellisProjectionError("projection_task_store_unreadable");
  });
  const directory = await opendir(canonicalTasksRoot).catch(() => {
    throw new TrellisProjectionError("projection_task_store_unreadable");
  });
  const matches = [];
  let count = 0;
  for await (const entry of directory) {
    if (entry.name === "archive" || entry.name.startsWith(".")) continue;
    if (entry.isSymbolicLink()) {
      throw new TrellisProjectionError("projection_task_store_unsafe");
    }
    if (!entry.isDirectory()) continue;
    count += 1;
    if (count > MAX_TASK_DIRECTORIES) {
      throw new TrellisProjectionError("projection_task_store_limit_exceeded");
    }
    const taskDir = await realpath(path.join(canonicalTasksRoot, entry.name)).catch(
      () => {
        throw new TrellisProjectionError("projection_task_store_unreadable");
      },
    );
    if (!samePath(path.dirname(taskDir), canonicalTasksRoot)) {
      throw new TrellisProjectionError("projection_task_store_unsafe");
    }
    let snapshot;
    try {
      const taskFile = path.join(taskDir, "task.json");
      const taskFileStat = await lstat(taskFile);
      if (!taskFileStat.isFile() || taskFileStat.isSymbolicLink()) {
        throw new TrellisProjectionError("projection_task_store_unsafe");
      }
      snapshot = await stableTaskSnapshot(taskApi, taskDir, checkoutRoot);
    } catch (error) {
      if (error instanceof TrellisProjectionError) throw error;
      throw new TrellisProjectionError("projection_task_record_invalid");
    }
    if (snapshot.record.id === trellisTaskId) {
      matches.push({ checkoutRoot, taskDir, snapshot });
    }
  }
  if (matches.length === 0) throw new TrellisProjectionError("projection_task_missing");
  if (matches.length !== 1) throw new TrellisProjectionError("projection_task_ambiguous");
  return Object.freeze(matches[0]);
}

async function stableTaskSnapshot(taskApi, taskDir, checkoutRoot) {
  const taskFile = path.join(taskDir, "task.json");
  const before = await stableTaskFile(taskFile);
  let record;
  try {
    record = taskApi.loadTaskRecord({ taskDir, cwd: checkoutRoot });
    const rawRecord = parseStrictJsonBytes(before.bytes);
    if (!recordMatchesRaw(record, rawRecord)) {
      throw new Error("official task decoder disagrees with stable raw bytes");
    }
  } catch {
    throw new TrellisProjectionError("projection_task_record_invalid");
  }
  const after = await stableTaskFile(taskFile);
  if (
    before.revision !== after.revision ||
    !sameFileIdentity(before.identity, after.identity)
  ) {
    throw new TrellisProjectionError("projection_unstable_read");
  }
  return Object.freeze({
    record: Object.freeze(record),
    revision: after.revision,
    byteLength: after.byteLength,
  });
}

async function stableTaskFile(taskFile) {
  const first = await taskFileStat(taskFile);
  const bytes = await readFile(taskFile).catch(() => {
    throw new TrellisProjectionError("projection_task_record_invalid");
  });
  const second = await taskFileStat(taskFile);
  if (
    !sameFileVersion(first, second) ||
    bytes.byteLength !== Number(second.size) ||
    bytes.byteLength > MAX_TASK_RECORD_BYTES
  ) {
    throw new TrellisProjectionError("projection_unstable_read");
  }
  return Object.freeze({
    bytes,
    revision: `sha256:${createHash("sha256").update(bytes).digest("hex")}`,
    byteLength: bytes.byteLength,
    identity: second,
  });
}

async function taskFileStat(taskFile) {
  const stat = await lstat(taskFile, { bigint: true }).catch(() => {
    throw new TrellisProjectionError("projection_task_record_invalid");
  });
  if (
    !stat.isFile() ||
    stat.isSymbolicLink() ||
    stat.nlink !== 1n ||
    stat.size < 2n ||
    stat.size > BigInt(MAX_TASK_RECORD_BYTES)
  ) {
    throw new TrellisProjectionError("projection_task_record_invalid");
  }
  return stat;
}

function recordMatchesRaw(record, rawRecord) {
  if (!isPlainObject(record) || !isPlainObject(rawRecord)) return false;
  if (
    Object.keys(record).length !== TASK_RECORD_FIELDS.length ||
    TASK_RECORD_FIELDS.some((field) => !(field in record) || !(field in rawRecord))
  ) {
    return false;
  }
  return TASK_RECORD_FIELDS.every(
    (field) => canonicalDigest(record[field]) === canonicalDigest(rawRecord[field]),
  );
}

async function withProjectionWriter(checkoutRoot, action) {
  const lockPath = path.join(checkoutRoot, ".trellis", PROJECTION_WRITER_LOCK);
  const tokenValue = `${process.pid}:${randomBytes(32).toString("hex")}`;
  let handle;
  try {
    handle = await open(lockPath, "wx", 0o600);
    await handle.writeFile(tokenValue, "utf8");
    await handle.sync();
    await assertWriterLock(handle, lockPath, tokenValue);
  } catch (error) {
    if (handle !== undefined) {
      await handle.close().catch(() => {});
    }
    if (error?.code === "EEXIST") {
      throw new TrellisProjectionError("projection_writer_busy");
    }
    throw new TrellisProjectionError("projection_writer_lock_failed");
  }

  let result;
  let actionError;
  try {
    result = await action();
    await assertWriterLock(handle, lockPath, tokenValue);
  } catch (error) {
    actionError = error;
  }

  let releaseFailed = false;
  try {
    const lockIdentity = await assertWriterLock(handle, lockPath, tokenValue);
    await handle.close();
    const releasedStat = await lstat(lockPath, { bigint: true });
    if (
      !releasedStat.isFile() ||
      releasedStat.isSymbolicLink() ||
      !sameFileIdentity(lockIdentity, releasedStat)
    ) {
      throw new Error("writer lock identity changed");
    }
    const storedToken = await readFile(lockPath, "utf8");
    if (storedToken !== tokenValue) throw new Error("writer lock token changed");
    await unlink(lockPath);
  } catch {
    releaseFailed = true;
  }
  if (actionError !== undefined) throw actionError;
  if (releaseFailed) {
    throw new TrellisProjectionError("projection_writer_lock_release_failed");
  }
  return result;
}

async function assertWriterLock(handle, lockPath, tokenValue) {
  let handleStat;
  let pathStat;
  let storedToken;
  try {
    [handleStat, pathStat, storedToken] = await Promise.all([
      handle.stat({ bigint: true }),
      lstat(lockPath, { bigint: true }),
      readFile(lockPath, "utf8"),
    ]);
  } catch {
    throw new TrellisProjectionError("projection_writer_lock_invalid");
  }
  if (
    !handleStat.isFile() ||
    !pathStat.isFile() ||
    pathStat.isSymbolicLink() ||
    handleStat.nlink !== 1n ||
    pathStat.nlink !== 1n ||
    !sameFileIdentity(handleStat, pathStat) ||
    storedToken !== tokenValue
  ) {
    throw new TrellisProjectionError("projection_writer_lock_invalid");
  }
  return handleStat;
}

function projectionSnapshot(context) {
  return Object.freeze({
    disposition: "inspected",
    reason: "none",
    recordRevision: context.snapshot.revision,
    byteLength: context.snapshot.byteLength,
    taskStatus: context.snapshot.record.status,
    projection: projectionMetadata(context.snapshot.record.meta),
  });
}

function conflictSnapshot(context, projection, reason) {
  return Object.freeze({
    disposition: "conflict",
    reason,
    recordRevision: context.snapshot.revision,
    byteLength: context.snapshot.byteLength,
    taskStatus: context.snapshot.record.status,
    projection,
  });
}

function compareProjection(existing, incoming) {
  if (existing === null) return "behind";
  for (const field of ["runId", "taskId", "trellisTaskId", "manifestDigest", "trellisGraphDigest"]) {
    if (existing[field] !== incoming[field]) return "identity_mismatch";
  }
  if (existing.canonicalSequence > incoming.canonicalSequence) return "ahead";
  if (existing.canonicalSequence < incoming.canonicalSequence) return "behind";
  return existing.projectionDigest === incoming.projectionDigest
    ? "idempotent"
    : "digest_mismatch";
}

function projectionMetadata(meta) {
  if (!isPlainObject(meta) || !(PROJECTION_META_KEY in meta)) return null;
  const value = meta[PROJECTION_META_KEY];
  if (!isPlainObject(value)) {
    throw new TrellisProjectionError("projection_metadata_invalid");
  }
  const projection = validateProjection(
    Object.fromEntries(
      Object.entries(value).filter(([key]) => key !== "projectionDigest"),
    ),
    value.trellisTaskId,
  );
  const storedDigest = digest(value.projectionDigest, "projection_digest");
  if (storedDigest !== projectionDigest(projection)) {
    throw new TrellisProjectionError("projection_metadata_digest_mismatch");
  }
  return Object.freeze({ ...projection, projectionDigest: storedDigest });
}

function projectionDigest(projection) {
  return canonicalDigest(projection);
}

function canonicalDigest(value) {
  return `sha256:${createHash("sha256")
    .update(JSON.stringify(sortObjectKeys(value)), "utf8")
    .digest("hex")}`;
}

function sortObjectKeys(value) {
  if (Array.isArray(value)) return value.map(sortObjectKeys);
  if (!isPlainObject(value)) return value;
  return Object.fromEntries(
    Object.keys(value)
      .sort(compareUtf8)
      .map((key) => [key, sortObjectKeys(value[key])]),
  );
}

function assertTaskApi(taskApi) {
  if (
    !isPlainObject(taskApi) ||
    typeof taskApi.loadTaskRecord !== "function" ||
    typeof taskApi.writeTaskRecord !== "function"
  ) {
    throw new TrellisProjectionError("projection_task_api_missing");
  }
}

function exactObject(value, fields) {
  if (!isPlainObject(value)) throw new TrellisProjectionError("projection_input_invalid");
  const expected = new Set(fields);
  if (
    Object.keys(value).some((key) => !expected.has(key)) ||
    fields.some((key) => !(key in value))
  ) {
    throw new TrellisProjectionError("projection_input_invalid");
  }
  return value;
}

function token(value, field) {
  const result = stableText(value, field, 256);
  if (!TOKEN.test(result)) throw new TrellisProjectionError(`projection_${field}_invalid`);
  return result;
}

function digest(value, field) {
  if (typeof value !== "string" || !SHA256.test(value)) {
    throw new TrellisProjectionError(`projection_${field}_invalid`);
  }
  return value;
}

function stableText(value, field, maximum) {
  if (typeof value !== "string" || value.length < 1 || value.length > maximum) {
    throw new TrellisProjectionError(`projection_${field}_invalid`);
  }
  const normalized = value.replaceAll("\r\n", "\n").replaceAll("\r", "\n").normalize("NFC");
  if (normalized !== value || value.trim().length === 0) {
    throw new TrellisProjectionError(`projection_${field}_invalid`);
  }
  for (const character of value) {
    const code = character.codePointAt(0);
    if (code < 0x20 || code === 0x7f || (code >= 0x80 && code <= 0x9f)) {
      throw new TrellisProjectionError(`projection_${field}_invalid`);
    }
  }
  return value;
}

function isPlainObject(value) {
  return (
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value) &&
    (Object.getPrototypeOf(value) === Object.prototype ||
      Object.getPrototypeOf(value) === null)
  );
}

function compareUtf8(left, right) {
  return Buffer.compare(Buffer.from(left, "utf8"), Buffer.from(right, "utf8"));
}

function samePath(left, right) {
  const normalizedLeft = path.normalize(left);
  const normalizedRight = path.normalize(right);
  return process.platform === "win32"
    ? normalizedLeft.toLowerCase() === normalizedRight.toLowerCase()
    : normalizedLeft === normalizedRight;
}

function sameFileIdentity(left, right) {
  return left.dev === right.dev && left.ino === right.ino;
}

function sameFileVersion(left, right) {
  return (
    sameFileIdentity(left, right) &&
    left.size === right.size &&
    left.mtimeNs === right.mtimeNs &&
    left.ctimeNs === right.ctimeNs &&
    left.mode === right.mode &&
    left.nlink === right.nlink
  );
}
