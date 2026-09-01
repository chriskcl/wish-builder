import { createHash, randomBytes } from "node:crypto";
import { lstat, open, opendir, readFile, realpath, unlink } from "node:fs/promises";
import path from "node:path";

const MAX_TASK_DIRECTORIES = 4096;
const MAX_TASK_RECORD_BYTES = 2 * 1024 * 1024;
const LIFECYCLE_META_KEY = "wish_builder_lifecycle";
const LIFECYCLE_WRITER_LOCK = ".wish-builder-lifecycle-writer.lock";
const TASK_RECORD_FIELDS = Object.freeze([
  "id", "name", "title", "description", "status", "dev_type", "scope",
  "package", "priority", "creator", "assignee", "createdAt", "completedAt",
  "branch", "base_branch", "worktree_path", "commit", "pr_url", "subtasks",
  "children", "parent", "relatedFiles", "notes", "meta",
]);
const SHA256 = /^sha256:[0-9a-f]{64}$/;
const TOKEN = /^[A-Za-z0-9][-A-Za-z0-9._:@/+]{0,255}$/;
const OPERATION_KINDS = new Set(["prepare_attempt", "check_attempt", "finish_attempt"]);

export class TrellisLifecycleError extends Error {
  constructor(code) {
    super(code);
    this.name = "TrellisLifecycleError";
    this.code = code;
  }
}

export async function applyTrellisLifecycle(taskApi, input) {
  assertTaskApi(taskApi);
  const request = validateApplyInput(input);
  const located = await locateTask(taskApi, request.checkoutRoot, request.command.trellis_task_id);
  return withLifecycleWriter(located.checkoutRoot, async () => {
    const context = await locateTask(taskApi, request.checkoutRoot, request.command.trellis_task_id);
    const lifecycle = lifecycleMetadata(context.snapshot.record.meta);
    const existing = lifecycle.operations[request.command.operation_id];
    if (existing !== undefined) {
      return replayOrCollision(existing, request);
    }

    let result;
    let inspection = null;
    if (request.operationKind === "prepare_attempt") {
      result = prepareObservation(request);
      inspection = result;
    } else if (request.operationKind === "check_attempt") {
      if (!hasPrepared(lifecycle, request.command.attempt_id)) {
        result = unknownObservation(request.operationKind, request.command.operation_id,
          `attempt_not_prepared:${request.command.attempt_id}`);
      } else {
        result = checkObservation(request);
        inspection = attemptStateObservation(request, "checked", result.effectDigest);
      }
    } else {
      if (!hasChecked(lifecycle, request.command.attempt_id)) {
        result = unknownObservation(request.operationKind, request.command.operation_id,
          `attempt_not_checked:${request.command.attempt_id}`);
      } else {
        result = finishObservation(request);
        inspection = attemptStateObservation(request, "finished", result.effectDigest);
      }
    }

    const next = {
      ...context.snapshot.record,
      meta: {
        ...context.snapshot.record.meta,
        [LIFECYCLE_META_KEY]: {
          schemaVersion: 1,
          operations: {
            ...lifecycle.operations,
            [request.command.operation_id]: {
              operationId: request.command.operation_id,
              operationKind: request.operationKind,
              requestPayloadHash: request.commandHash,
              observation: result,
              inspection,
            },
          },
        },
      },
    };
    await writeAndVerify(taskApi, context, next, request.command.operation_id);
    return result;
  });
}

export async function inspectTrellisLifecycle(taskApi, input) {
  assertTaskApi(taskApi);
  const request = validateInspectInput(input);
  const context = await locateTask(taskApi, request.checkoutRoot, request.trellisTaskId);
  const lifecycle = lifecycleMetadata(context.snapshot.record.meta);
  const existing = lifecycle.operations[request.operationId];
  if (existing === undefined) return absentObservation(request.operationKind, request.operationId);
  if (existing.operationKind !== request.operationKind) {
    return unknownObservation(request.operationKind, request.operationId,
      `operation_kind_mismatch:${request.operationId}`);
  }
  if (request.expectedRequestPayloadHash !== null &&
      existing.requestPayloadHash !== request.expectedRequestPayloadHash) {
    return unknownObservation(request.operationKind, request.operationId,
      `request_hash_mismatch:${request.operationId}`);
  }
  if (request.operationKind === "prepare_attempt") return existing.observation;
  return existing.observation;
}

function replayOrCollision(existing, request) {
  if (existing.operationKind === request.operationKind &&
      existing.requestPayloadHash === request.commandHash) {
    return existing.observation;
  }
  return unknownObservation(request.operationKind, request.command.operation_id,
    `operation_id_collision:${request.command.operation_id}`);
}

function prepareObservation(request) {
  const command = request.command;
  const effectDigest = effectDigestFor("prepare_attempt", request.commandHash);
  return {
    operationId: command.operation_id,
    status: "applied",
    observedAt: now(),
    lifecycleState: "prepared",
    effectDigest,
    attemptId: command.operation_id,
    trellisTaskId: command.trellis_task_id,
    worktreeId: request.worktreeId,
    worktreePath: request.worktreePath,
    baseCommit: command.expected_base_commit,
    evidence: [],
  };
}

function checkObservation(request) {
  const command = request.command;
  const effectDigest = effectDigestFor("check_attempt", request.commandHash);
  return {
    operationId: command.operation_id,
    status: "applied",
    observedAt: now(),
    effectDigest,
    attemptId: command.attempt_id,
    passed: true,
    headCommit: command.expected_head_commit,
    checkDigest: effectDigestFor("check_result", request.commandHash),
    evidence: [],
  };
}

function finishObservation(request) {
  const command = request.command;
  const effectDigest = effectDigestFor("finish_attempt", request.commandHash);
  return {
    operationId: command.operation_id,
    status: "applied",
    observedAt: now(),
    effectDigest,
    attemptId: command.attempt_id,
    finished: true,
    deliveredCommit: command.delivered_commit,
    finishDigest: effectDigestFor("finish_result", request.commandHash),
    evidence: [],
  };
}

function attemptStateObservation(request, state, effectDigest) {
  const command = request.command;
  return {
    operationId: command.operation_id,
    status: "applied",
    observedAt: now(),
    lifecycleState: state,
    effectDigest,
    attemptId: command.attempt_id,
    trellisTaskId: command.trellis_task_id,
    evidence: [],
  };
}

function unknownObservation(kind, operationId, reason) {
  if (kind === "prepare_attempt") {
    return {
      operationId, status: "unknown", observedAt: now(), lifecycleState: "unknown",
      effectDigest: null, attemptId: null, trellisTaskId: null, worktreeId: null,
      worktreePath: null, baseCommit: null, evidence: [reason],
    };
  }
  if (kind === "check_attempt") {
    return {
      operationId, status: "unknown", observedAt: now(), effectDigest: null,
      attemptId: null, passed: null, headCommit: null, checkDigest: null,
      evidence: [reason],
    };
  }
  return {
    operationId, status: "unknown", observedAt: now(), effectDigest: null,
    attemptId: null, finished: null, deliveredCommit: null, finishDigest: null,
    evidence: [reason],
  };
}

function absentObservation(kind, operationId) {
  if (kind === "prepare_attempt") {
    return {
      operationId, status: "absent", observedAt: now(), lifecycleState: "absent",
      effectDigest: null, attemptId: null, trellisTaskId: null, worktreeId: null,
      worktreePath: null, baseCommit: null, evidence: [],
    };
  }
  if (kind === "check_attempt") {
    return {
      operationId, status: "absent", observedAt: now(), effectDigest: null,
      attemptId: null, passed: null, headCommit: null, checkDigest: null,
      evidence: [],
    };
  }
  return {
    operationId, status: "absent", observedAt: now(), effectDigest: null,
    attemptId: null, finished: null, deliveredCommit: null, finishDigest: null,
    evidence: [],
  };
}

function lifecycleMetadata(meta) {
  if (!isPlainObject(meta)) throw new TrellisLifecycleError("lifecycle_metadata_invalid");
  const value = meta[LIFECYCLE_META_KEY];
  if (value === undefined) return { schemaVersion: 1, operations: {} };
  if (!isPlainObject(value) || value.schemaVersion !== 1 || !isPlainObject(value.operations)) {
    throw new TrellisLifecycleError("lifecycle_metadata_invalid");
  }
  for (const [operationId, entry] of Object.entries(value.operations)) {
    if (!TOKEN.test(operationId) || !isPlainObject(entry) ||
        Object.keys(entry).length !== 5 ||
        !["operationId", "operationKind", "requestPayloadHash", "observation", "inspection"].every((field) => Object.hasOwn(entry, field)) ||
        entry.operationId !== operationId || !OPERATION_KINDS.has(entry.operationKind) ||
        !SHA256.test(entry.requestPayloadHash) || !isPlainObject(entry.observation) ||
        (entry.inspection !== null && !isPlainObject(entry.inspection))) {
      throw new TrellisLifecycleError("lifecycle_metadata_invalid");
    }
  }
  return value;
}

function hasPrepared(lifecycle, attemptId) {
  return Object.values(lifecycle.operations).some((entry) =>
    entry.operationKind === "prepare_attempt" &&
    entry.observation?.status === "applied" &&
    entry.observation?.attemptId === attemptId);
}

function hasChecked(lifecycle, attemptId) {
  return Object.values(lifecycle.operations).some((entry) =>
    entry.operationKind === "check_attempt" &&
    entry.observation?.status === "applied" &&
    entry.observation?.attemptId === attemptId);
}

async function writeAndVerify(taskApi, context, record, operationId) {
  const before = context.snapshot;
  try {
    await taskApi.writeTaskRecord({ taskDir: context.taskDir, cwd: context.checkoutRoot, record });
  } catch {
    throw new TrellisLifecycleError("lifecycle_write_outcome_unknown");
  }
  try {
    const verified = await stableTaskSnapshot(taskApi, context.taskDir, context.checkoutRoot);
    if (verified.revision === before.revision ||
        canonicalDigest(verified.record) !== canonicalDigest(record) ||
        verified.record.id !== context.record.id ||
        !lifecycleMetadata(verified.record.meta).operations[operationId]) {
      throw new Error("lifecycle write verification mismatch");
    }
  } catch {
    throw new TrellisLifecycleError("lifecycle_write_unverified");
  }
}

async function locateTask(taskApi, requestedRoot, trellisTaskId) {
  const checkoutRoot = await validateCheckout(requestedRoot);
  const tasksRoot = path.join(checkoutRoot, ".trellis", "tasks");
  const canonicalRoot = await realpath(tasksRoot).catch(() => {
    throw new TrellisLifecycleError("lifecycle_task_store_missing");
  });
  const directory = await opendir(canonicalRoot).catch(() => {
    throw new TrellisLifecycleError("lifecycle_task_store_unreadable");
  });
  const matches = [];
  let count = 0;
  for await (const entry of directory) {
    if (entry.name === "archive" || entry.name.startsWith(".")) continue;
    if (entry.isSymbolicLink()) throw new TrellisLifecycleError("lifecycle_task_store_unsafe");
    if (!entry.isDirectory()) continue;
    if (++count > MAX_TASK_DIRECTORIES) throw new TrellisLifecycleError("lifecycle_task_store_limit_exceeded");
    const taskDir = await realpath(path.join(canonicalRoot, entry.name)).catch(() => {
      throw new TrellisLifecycleError("lifecycle_task_store_unreadable");
    });
    if (!samePath(path.dirname(taskDir), canonicalRoot)) throw new TrellisLifecycleError("lifecycle_task_store_unsafe");
    const snapshot = await stableTaskSnapshot(taskApi, taskDir, checkoutRoot);
    if (snapshot.record.id === trellisTaskId) matches.push({ checkoutRoot, taskDir, snapshot, record: snapshot.record });
  }
  if (matches.length === 0) throw new TrellisLifecycleError("lifecycle_task_missing");
  if (matches.length !== 1) throw new TrellisLifecycleError("lifecycle_task_ambiguous");
  return matches[0];
}

async function stableTaskSnapshot(taskApi, taskDir, checkoutRoot) {
  const taskFile = path.join(taskDir, "task.json");
  const before = await stableTaskFile(taskFile);
  let record;
  try {
    record = taskApi.loadTaskRecord({ taskDir, cwd: checkoutRoot });
    const rawRecord = JSON.parse(before.bytes.toString("utf8"));
    if (!recordMatchesRaw(record, rawRecord)) throw new Error("decoder disagreement");
  } catch {
    throw new TrellisLifecycleError("lifecycle_task_record_invalid");
  }
  const after = await stableTaskFile(taskFile);
  if (before.revision !== after.revision || before.identity !== after.identity) {
    throw new TrellisLifecycleError("lifecycle_unstable_read");
  }
  return { record, revision: after.revision, byteLength: after.byteLength, identity: after.identity };
}

async function stableTaskFile(taskFile) {
  const first = await lstat(taskFile).catch(() => {
    throw new TrellisLifecycleError("lifecycle_task_record_invalid");
  });
  if (!first.isFile() || first.isSymbolicLink() || first.nlink !== 1 || first.size < 2 || first.size > MAX_TASK_RECORD_BYTES) {
    throw new TrellisLifecycleError("lifecycle_task_record_invalid");
  }
  const bytes = await readFile(taskFile).catch(() => {
    throw new TrellisLifecycleError("lifecycle_task_record_invalid");
  });
  const second = await lstat(taskFile).catch(() => {
    throw new TrellisLifecycleError("lifecycle_task_record_invalid");
  });
  if (!sameStat(first, second) || bytes.byteLength !== second.size) throw new TrellisLifecycleError("lifecycle_unstable_read");
  return {
    bytes,
    revision: `sha256:${createHash("sha256").update(bytes).digest("hex")}`,
    byteLength: bytes.byteLength,
    identity: `${second.dev}:${second.ino}:${second.mtimeMs}:${second.ctimeMs}:${second.size}`,
  };
}

async function withLifecycleWriter(checkoutRoot, action) {
  const lockPath = path.join(checkoutRoot, ".trellis", LIFECYCLE_WRITER_LOCK);
  const tokenValue = `${process.pid}:${randomBytes(32).toString("hex")}`;
  let handle;
  try {
    handle = await open(lockPath, "wx", 0o600);
    await handle.writeFile(tokenValue, "utf8");
    await handle.sync();
  } catch (error) {
    if (handle) await handle.close().catch(() => {});
    if (error?.code === "EEXIST") throw new TrellisLifecycleError("lifecycle_writer_busy");
    throw new TrellisLifecycleError("lifecycle_writer_lock_failed");
  }
  let result;
  let actionError;
  try {
    result = await action();
  } catch (error) {
    actionError = error;
  }
  try {
    await handle.close();
    const stored = await readFile(lockPath, "utf8");
    if (stored !== tokenValue) throw new Error("lock token changed");
    await unlink(lockPath);
  } catch {
    if (!actionError) actionError = new TrellisLifecycleError("lifecycle_writer_lock_release_failed");
  }
  if (actionError) throw actionError;
  return result;
}

function validateApplyInput(value) {
  if (!isPlainObject(value)) throw new TrellisLifecycleError("lifecycle_input_invalid");
  exactFields(value, ["checkoutRoot", "operationKind", "commandHash", "command", "worktreePath", "worktreeId"], "lifecycle input");
  if (typeof value.checkoutRoot !== "string" || !path.isAbsolute(value.checkoutRoot) || value.checkoutRoot.length > 1024 || value.checkoutRoot.includes("\0")) throw new TrellisLifecycleError("lifecycle_checkout_invalid");
  if (!OPERATION_KINDS.has(value.operationKind)) throw new TrellisLifecycleError("lifecycle_operation_kind_invalid");
  if (!SHA256.test(value.commandHash)) throw new TrellisLifecycleError("lifecycle_command_hash_invalid");
  const command = validateCommand(value.operationKind, value.command);
  if (value.operationKind === "prepare_attempt") {
    if (typeof value.worktreePath !== "string" || !path.isAbsolute(value.worktreePath) || value.worktreePath.length > 1024 || value.worktreePath.includes("\0")) throw new TrellisLifecycleError("lifecycle_worktree_path_invalid");
    if (typeof value.worktreeId !== "string" || !TOKEN.test(value.worktreeId)) throw new TrellisLifecycleError("lifecycle_worktree_id_invalid");
  } else if (value.worktreePath !== null || value.worktreeId !== null) throw new TrellisLifecycleError("lifecycle_input_invalid");
  if (canonicalCommandHash(command) !== value.commandHash) throw new TrellisLifecycleError("lifecycle_command_hash_mismatch");
  return { checkoutRoot: path.resolve(value.checkoutRoot), operationKind: value.operationKind, commandHash: value.commandHash, command, worktreePath: value.worktreePath, worktreeId: value.worktreeId };
}

function validateInspectInput(value) {
  if (!isPlainObject(value)) throw new TrellisLifecycleError("lifecycle_input_invalid");
  exactFields(value, ["checkoutRoot", "trellisTaskId", "operationKind", "operationId", "expectedRequestPayloadHash"], "lifecycle inspect input");
  if (typeof value.checkoutRoot !== "string" || !path.isAbsolute(value.checkoutRoot) || value.checkoutRoot.length > 1024 || value.checkoutRoot.includes("\0")) throw new TrellisLifecycleError("lifecycle_checkout_invalid");
  if (!OPERATION_KINDS.has(value.operationKind) || !TOKEN.test(value.operationId)) throw new TrellisLifecycleError("lifecycle_inspect_invalid");
  if (typeof value.trellisTaskId !== "string" || !value.trellisTaskId || value.trellisTaskId.length > 512) throw new TrellisLifecycleError("lifecycle_task_id_invalid");
  if (value.expectedRequestPayloadHash !== null && !SHA256.test(value.expectedRequestPayloadHash)) throw new TrellisLifecycleError("lifecycle_expected_hash_invalid");
  return { checkoutRoot: path.resolve(value.checkoutRoot), trellisTaskId: value.trellisTaskId, operationKind: value.operationKind, operationId: value.operationId, expectedRequestPayloadHash: value.expectedRequestPayloadHash };
}

function validateCommand(kind, value) {
  if (!isPlainObject(value)) throw new TrellisLifecycleError("lifecycle_command_invalid");
  const fields = {
    prepare_attempt: ["attempt", "command_type", "dispatch_id", "expected_base_commit", "manifest_digest", "operation_id", "parent_task_id", "run_id", "schema_version", "task_id", "trellis_graph_digest", "trellis_task_id"],
    check_attempt: ["attempt_id", "command_type", "expected_head_commit", "operation_id", "schema_version", "task_id", "task_packet_digest", "trellis_task_id"],
    finish_attempt: ["attempt_id", "command_type", "delivered_commit", "delivery_evidence_digest", "operation_id", "schema_version", "task_id", "trellis_task_id"],
  }[kind];
  exactFields(value, fields, "lifecycle command");
  if (value.command_type !== kind || value.schema_version !== 1 || typeof value.operation_id !== "string" || !TOKEN.test(value.operation_id) || typeof value.trellis_task_id !== "string" || !value.trellis_task_id) throw new TrellisLifecycleError("lifecycle_command_invalid");
  for (const field of fields) {
    if (field === "command_type" || field === "schema_version") continue;
    if (typeof value[field] !== "string" && typeof value[field] !== "number") throw new TrellisLifecycleError("lifecycle_command_invalid");
  }
  return value;
}

function canonicalCommandHash(command) {
  return `sha256:${createHash("sha256").update(`${JSON.stringify(sortObjectKeys(command))}\n`, "utf8").digest("hex")}`;
}

function effectDigestFor(kind, commandHash) {
  return `sha256:${createHash("sha256").update(`${JSON.stringify(sortObjectKeys({ command_hash: commandHash, lifecycle_effect: kind }))}\n`, "utf8").digest("hex")}`;
}

function canonicalDigest(value) {
  return `sha256:${createHash("sha256").update(JSON.stringify(sortObjectKeys(value)), "utf8").digest("hex")}`;
}

function sortObjectKeys(value) {
  if (Array.isArray(value)) return value.map(sortObjectKeys);
  if (!isPlainObject(value)) return value;
  return Object.fromEntries(Object.keys(value).sort(compareUtf8).map((key) => [key, sortObjectKeys(value[key])]));
}

function recordMatchesRaw(record, raw) {
  return isPlainObject(record) && isPlainObject(raw) && TASK_RECORD_FIELDS.every((field) => Object.hasOwn(record, field) && Object.hasOwn(raw, field) && canonicalDigest(record[field]) === canonicalDigest(raw[field]));
}

function assertTaskApi(taskApi) {
  if (!isPlainObject(taskApi) || typeof taskApi.loadTaskRecord !== "function" || typeof taskApi.writeTaskRecord !== "function") throw new TrellisLifecycleError("lifecycle_task_api_missing");
}

async function validateCheckout(requestedRoot) {
  const stat = await lstat(requestedRoot).catch(() => { throw new TrellisLifecycleError("lifecycle_checkout_missing"); });
  if (!stat.isDirectory() || stat.isSymbolicLink()) throw new TrellisLifecycleError("lifecycle_checkout_unsafe");
  const root = await realpath(requestedRoot).catch(() => { throw new TrellisLifecycleError("lifecycle_checkout_unreadable"); });
  const git = await lstat(path.join(root, ".git")).catch(() => { throw new TrellisLifecycleError("lifecycle_checkout_not_git"); });
  if (git.isSymbolicLink() || (!git.isFile() && !git.isDirectory())) throw new TrellisLifecycleError("lifecycle_checkout_not_git");
  return root;
}

function exactFields(value, fields, label) {
  const expected = new Set(fields);
  if (Object.keys(value).some((key) => !expected.has(key)) || fields.some((key) => !Object.hasOwn(value, key))) throw new TrellisLifecycleError(`${label.replaceAll(" ", "_")}_invalid`);
}

function sameStat(left, right) {
  return left.dev === right.dev && left.ino === right.ino && left.size === right.size && left.mtimeMs === right.mtimeMs && left.ctimeMs === right.ctimeMs && left.mode === right.mode && left.nlink === right.nlink;
}

function samePath(left, right) {
  const a = path.normalize(left); const b = path.normalize(right);
  return process.platform === "win32" ? a.toLowerCase() === b.toLowerCase() : a === b;
}

function compareUtf8(left, right) { return Buffer.compare(Buffer.from(left, "utf8"), Buffer.from(right, "utf8")); }
function now() { return new Date().toISOString(); }
function isPlainObject(value) { return typeof value === "object" && value !== null && !Array.isArray(value) && (Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null); }
