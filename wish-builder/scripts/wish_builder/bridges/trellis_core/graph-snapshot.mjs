import { createHash } from "node:crypto";
import { lstat, opendir, readFile, realpath } from "node:fs/promises";
import path from "node:path";

import { parseStrictJsonBytes } from "./strict-json.mjs";

export const WISH_BUILDER_GRAPH_FORMAT = "wish-builder.trellis-graph.v1";

const MAX_TASK_DIRECTORIES = 4096;
const MAX_TASK_RECORD_BYTES = 2 * 1024 * 1024;
const MAX_GRAPH_SNAPSHOT_BYTES = 8 * 1024 * 1024;
const TASK_RECORD_FIELDS = Object.freeze([
  "id", "name", "title", "description", "status", "dev_type", "scope",
  "package", "priority", "creator", "assignee", "createdAt", "completedAt",
  "branch", "base_branch", "worktree_path", "commit", "pr_url", "subtasks",
  "children", "parent", "relatedFiles", "notes", "meta",
]);
const TASK_SPEC_FIELDS = Object.freeze([
  "requirement_ids",
  "depends_on",
  "owned_paths",
  "allowed_auxiliary_paths",
  "acceptance_criteria",
  "regression_commands",
  "rollback",
  "documentation",
  "wave",
  "risk",
  "may_change_contracts",
  "instruction_context_digest",
  "approved_document_digests",
  "task_packet_template_digest",
]);

export class TrellisGraphSnapshotError extends Error {
  constructor(code) {
    super(code);
    this.name = "TrellisGraphSnapshotError";
    this.code = code;
  }
}

export async function deriveTrellisGraphSnapshot(taskApi, input) {
  assertTaskApi(taskApi);
  const request = validateInput(input);
  const store = await loadTaskStore(taskApi, request.checkoutRoot);
  const parents = store.filter((item) => item.record.id === request.parentTaskId);
  if (parents.length === 0) throw new TrellisGraphSnapshotError("graph_parent_missing");
  if (parents.length !== 1) throw new TrellisGraphSnapshotError("graph_parent_ambiguous");
  const parent = parents[0];
  const parentMetadata = wishBuilderMetadata(parent.record.meta, "parent");
  exactFields(parentMetadata, ["schemaVersion", "requirements"], "parent metadata");
  if (parentMetadata.schemaVersion !== 1 || !Array.isArray(parentMetadata.requirements)) {
    throw new TrellisGraphSnapshotError("graph_parent_metadata_invalid");
  }

  const declared = uniqueStrings([...parent.record.children, ...parent.record.subtasks]);
  const children = store.filter((item) => item.record.parent === request.parentTaskId);
  const actual = children.map((item) => item.record.id).sort(compareUtf8);
  if (children.length === 0) throw new TrellisGraphSnapshotError("graph_tasks_empty");
  if (declared.length !== actual.length || declared.some((item, index) => item !== actual[index])) {
    throw new TrellisGraphSnapshotError("graph_membership_incomplete");
  }

  const tasks = children
    .sort((left, right) => compareUtf8(left.record.id, right.record.id))
    .map((child) => projectTask(child.record));
  const materialRecords = [parent, ...children]
    .sort((left, right) => compareUtf8(left.record.id, right.record.id))
    .map((item) => ({ id: item.record.id, recordSha256: item.rawSha256 }));
  const revision = canonicalDigest({
    format: WISH_BUILDER_GRAPH_FORMAT,
    parentTaskId: request.parentTaskId,
    records: materialRecords,
  });
  const payload = {
    schema_version: 1,
    parent_task_id: request.parentTaskId,
    revision,
    requirements: cloneJson(parentMetadata.requirements),
    tasks,
  };
  const snapshotBytes = Buffer.from(canonicalString(payload), "utf8");
  if (snapshotBytes.length > MAX_GRAPH_SNAPSHOT_BYTES) {
    throw new TrellisGraphSnapshotError("graph_snapshot_limit_exceeded");
  }
  return Object.freeze({
    exportVersion: WISH_BUILDER_GRAPH_FORMAT,
    trellisVersion: request.trellisVersion,
    parentTaskId: request.parentTaskId,
    revision,
    observedAt: request.observedAt,
    snapshotBytes,
    sourceSha256: `sha256:${createHash("sha256").update(snapshotBytes).digest("hex")}`,
    complete: true,
  });
}

function projectTask(record) {
  const metadata = wishBuilderMetadata(record.meta, "task");
  exactFields(metadata, ["schemaVersion", "task"], "task metadata");
  if (metadata.schemaVersion !== 1) throw new TrellisGraphSnapshotError("graph_task_metadata_invalid");
  const spec = plainObject(metadata.task, "task specification");
  exactFields(spec, TASK_SPEC_FIELDS, "task specification");
  return {
    id: record.id,
    title: record.title,
    ...cloneJson(spec),
  };
}

async function loadTaskStore(taskApi, requestedRoot) {
  const checkoutRoot = await validateCheckout(requestedRoot);
  const first = await loadTaskStorePass(taskApi, checkoutRoot);
  const second = await loadTaskStorePass(taskApi, checkoutRoot);
  if (taskStoreDigest(first) !== taskStoreDigest(second)) {
    throw new TrellisGraphSnapshotError("graph_unstable_read");
  }
  return second;
}

async function loadTaskStorePass(taskApi, checkoutRoot) {
  const tasksRoot = path.join(checkoutRoot, ".trellis", "tasks");
  const taskRootStat = await lstat(tasksRoot).catch(() => {
    throw new TrellisGraphSnapshotError("graph_task_store_missing");
  });
  if (!taskRootStat.isDirectory() || taskRootStat.isSymbolicLink()) {
    throw new TrellisGraphSnapshotError("graph_task_store_unsafe");
  }
  const canonicalRoot = await realpath(tasksRoot).catch(() => {
    throw new TrellisGraphSnapshotError("graph_task_store_unreadable");
  });
  const directory = await opendir(canonicalRoot).catch(() => {
    throw new TrellisGraphSnapshotError("graph_task_store_unreadable");
  });
  const records = [];
  let count = 0;
  for await (const entry of directory) {
    if (entry.name === "archive" || entry.name.startsWith(".")) continue;
    if (entry.isSymbolicLink()) throw new TrellisGraphSnapshotError("graph_task_store_unsafe");
    if (!entry.isDirectory()) continue;
    count += 1;
    if (count > MAX_TASK_DIRECTORIES) throw new TrellisGraphSnapshotError("graph_task_store_limit_exceeded");
    const taskDir = await realpath(path.join(canonicalRoot, entry.name)).catch(() => {
      throw new TrellisGraphSnapshotError("graph_task_store_unreadable");
    });
    if (!samePath(path.dirname(taskDir), canonicalRoot)) {
      throw new TrellisGraphSnapshotError("graph_task_store_unsafe");
    }
    records.push(await loadStableRecord(taskApi, checkoutRoot, taskDir));
  }
  const ids = records.map((item) => item.record.id);
  if (new Set(ids).size !== ids.length) throw new TrellisGraphSnapshotError("graph_task_id_ambiguous");
  return records;
}

function taskStoreDigest(records) {
  return canonicalDigest(
    records
      .map((item) => ({ directory: item.directory, rawSha256: item.rawSha256 }))
      .sort((left, right) => compareUtf8(left.directory, right.directory)),
  );
}

async function loadStableRecord(taskApi, checkoutRoot, taskDir) {
  const taskFile = path.join(taskDir, "task.json");
  const stat = await lstat(taskFile).catch(() => {
    throw new TrellisGraphSnapshotError("graph_task_record_invalid");
  });
  if (!stat.isFile() || stat.isSymbolicLink() || stat.size < 2 || stat.size > MAX_TASK_RECORD_BYTES) {
    throw new TrellisGraphSnapshotError("graph_task_record_invalid");
  }
  const before = await readFile(taskFile);
  let record;
  try {
    const strict = parseStrictJsonBytes(before);
    record = taskApi.loadTaskRecord({ taskDir, cwd: checkoutRoot });
    if (!recordMatchesRaw(record, strict)) throw new Error("decoder disagreement");
  } catch {
    throw new TrellisGraphSnapshotError("graph_task_record_invalid");
  }
  const after = await readFile(taskFile);
  if (!before.equals(after)) throw new TrellisGraphSnapshotError("graph_unstable_read");
  return Object.freeze({
    directory: path.basename(taskDir),
    record: Object.freeze(record),
    rawSha256: `sha256:${createHash("sha256").update(after).digest("hex")}`,
  });
}

function recordMatchesRaw(record, raw) {
  if (!isPlainObject(record) || !isPlainObject(raw)) return false;
  if (Object.keys(record).length !== TASK_RECORD_FIELDS.length) return false;
  return TASK_RECORD_FIELDS.every(
    (field) => Object.hasOwn(record, field)
      && Object.hasOwn(raw, field)
      && canonicalDigest(record[field]) === canonicalDigest(raw[field]),
  );
}

async function validateCheckout(requestedRoot) {
  const stat = await lstat(requestedRoot).catch(() => {
    throw new TrellisGraphSnapshotError("graph_checkout_missing");
  });
  if (!stat.isDirectory() || stat.isSymbolicLink()) throw new TrellisGraphSnapshotError("graph_checkout_unsafe");
  const root = await realpath(requestedRoot).catch(() => {
    throw new TrellisGraphSnapshotError("graph_checkout_unreadable");
  });
  const git = await lstat(path.join(root, ".git")).catch(() => {
    throw new TrellisGraphSnapshotError("graph_checkout_not_git");
  });
  if (git.isSymbolicLink() || (!git.isFile() && !git.isDirectory())) {
    throw new TrellisGraphSnapshotError("graph_checkout_not_git");
  }
  return root;
}

function validateInput(value) {
  const source = plainObject(value, "graph snapshot input");
  exactFields(source, ["checkoutRoot", "parentTaskId", "trellisVersion", "observedAt"], "graph snapshot input");
  if (typeof source.checkoutRoot !== "string" || !path.isAbsolute(source.checkoutRoot) || source.checkoutRoot.length > 1024) {
    throw new TrellisGraphSnapshotError("graph_checkout_invalid");
  }
  const observedAt = stableText(source.observedAt, "observedAt", 64);
  if (new Date(observedAt).toISOString() !== observedAt) throw new TrellisGraphSnapshotError("graph_observed_at_invalid");
  return Object.freeze({
    checkoutRoot: path.resolve(source.checkoutRoot),
    parentTaskId: stableText(source.parentTaskId, "parentTaskId", 512),
    trellisVersion: stableToken(source.trellisVersion, "trellisVersion"),
    observedAt,
  });
}

function wishBuilderMetadata(meta, kind) {
  const source = plainObject(meta, `${kind} meta`);
  if (!Object.hasOwn(source, "wish_builder")) throw new TrellisGraphSnapshotError(`graph_${kind}_metadata_missing`);
  return plainObject(source.wish_builder, `${kind} wish_builder metadata`);
}

function uniqueStrings(value) {
  if (!value.every((item) => typeof item === "string")) throw new TrellisGraphSnapshotError("graph_membership_invalid");
  const result = value.map((item) => stableText(item, "child task id", 512));
  if (new Set(result).size !== result.length) throw new TrellisGraphSnapshotError("graph_membership_duplicate");
  return result.sort(compareUtf8);
}

function assertTaskApi(taskApi) {
  if (
    !isPlainObject(taskApi) ||
    typeof taskApi.loadTaskRecord !== "function" ||
    !isPlainObject(taskApi.taskRecordSchema) ||
    typeof taskApi.taskRecordSchema.parse !== "function"
  ) throw new TrellisGraphSnapshotError("graph_task_api_missing");
}

function stableToken(value, field) {
  const result = stableText(value, field, 256);
  if (!/^[A-Za-z0-9][-A-Za-z0-9._:@/+]{0,255}$/.test(result)) throw new TrellisGraphSnapshotError("graph_token_invalid");
  return result;
}

function stableText(value, field, maximum) {
  if (typeof value !== "string" || value.length < 1 || value.length > maximum) throw new TrellisGraphSnapshotError("graph_text_invalid");
  const normalized = value.replaceAll("\r\n", "\n").replaceAll("\r", "\n").normalize("NFC");
  if (normalized !== value || value.trim().length === 0) throw new TrellisGraphSnapshotError("graph_text_invalid");
  for (const character of value) {
    const code = character.codePointAt(0);
    if (code < 0x20 || code === 0x7f || (code >= 0x80 && code <= 0x9f)) {
      throw new TrellisGraphSnapshotError(`graph_${field}_control_invalid`);
    }
  }
  return value;
}

function plainObject(value, field) {
  if (!isPlainObject(value)) throw new TrellisGraphSnapshotError(`graph_${field.replaceAll(" ", "_")}_invalid`);
  return value;
}

function isPlainObject(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value) && (Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null);
}

function exactFields(value, fields, field) {
  const expected = new Set(fields);
  if (Object.keys(value).some((key) => !expected.has(key)) || fields.some((key) => !Object.hasOwn(value, key))) {
    throw new TrellisGraphSnapshotError(`graph_${field.replaceAll(" ", "_")}_invalid`);
  }
}

function cloneJson(value) {
  if (value === null || typeof value === "string" || typeof value === "boolean") return value;
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new TrellisGraphSnapshotError("graph_json_invalid");
    return value;
  }
  if (Array.isArray(value)) return value.map(cloneJson);
  if (isPlainObject(value)) return Object.fromEntries(Object.entries(value).map(([key, child]) => [key, cloneJson(child)]));
  throw new TrellisGraphSnapshotError("graph_json_invalid");
}

function canonicalString(value) {
  return JSON.stringify(canonicalValue(value));
}

function canonicalDigest(value) {
  return `sha256:${createHash("sha256").update(canonicalString(value), "utf8").digest("hex")}`;
}

function canonicalValue(value) {
  if (Array.isArray(value)) return value.map(canonicalValue);
  if (!isPlainObject(value)) return value;
  return Object.fromEntries(Object.keys(value).sort(compareUtf8).map((key) => [key, canonicalValue(value[key])]));
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
