import { CorePinError, loadPinnedTrellisCore } from "./core-loader.mjs";
import {
  TrellisGraphSnapshotError,
  deriveTrellisGraphSnapshot,
} from "./graph-snapshot.mjs";
import {
  TrellisProjectionError,
  applyTrellisTaskProjection,
  inspectTrellisTaskProjection,
} from "./projection.mjs";
import {
  TrellisLifecycleError,
  applyTrellisLifecycle,
  inspectTrellisLifecycle,
} from "./lifecycle.mjs";

export const BRIDGE_PROTOCOL_VERSION = 1;
export const MAX_STDIN_BYTES = 2 * 1024 * 1024;
const ACTIONS = Object.freeze([
  "probe",
  "execute",
  "inspect",
  "graph_snapshot",
  "projection_inspect",
  "projection_apply",
  "lifecycle_prepare",
  "lifecycle_check",
  "lifecycle_finish",
  "lifecycle_inspect",
]);
const PROJECT_KEY = /^(?!\.{1,2}$)[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;

export class BridgeRequestError extends Error {
  constructor(code, message, options = {}) {
    super(message);
    this.name = "BridgeRequestError";
    this.code = code;
    this.exitCode = options.exitCode ?? 2;
    this.details = options.details ?? null;
  }
}

export async function handleBridgeRequest(value, dependencies = {}) {
  const request = validateEnvelope(value);
  const loadCore = dependencies.loadCore ?? loadPinnedTrellisCore;
  let coreContext;
  try {
    coreContext = await loadCore(dependencies.coreOptions);
  } catch (error) {
    if (error instanceof CorePinError) throw error;
    throw new CorePinError("Pinned Trellis Core failed closed during verification");
  }
  if (["probe", "execute", "inspect"].includes(request.action)) {
    throw operationApiUnavailable(coreContext);
  }
  if (request.action === "graph_snapshot") {
    return deriveGraphSnapshot(request, coreContext);
  }
  if (request.action === "projection_inspect") {
    return inspectProjection(request, coreContext);
  }
  if (request.action === "projection_apply") {
    return applyProjection(request, coreContext);
  }
  if (request.action === "lifecycle_inspect") {
    return inspectLifecycle(request, coreContext);
  }
  return applyLifecycle(request, coreContext);
}

export function failureResponse(error, action = null) {
  let code = "BRIDGE_FAILURE";
  let exitCode = 5;
  let details = null;
  if (error instanceof BridgeRequestError) {
    code = error.code;
    exitCode = error.exitCode;
    details = error.details;
  } else if (error instanceof CorePinError) {
    code = "CORE_PIN_MISMATCH";
    exitCode = 3;
  }
  return Object.freeze({
    exitCode,
    body: Object.freeze({
      protocolVersion: BRIDGE_PROTOCOL_VERSION,
      ok: false,
      action: ACTIONS.includes(action) ? action : null,
      error: Object.freeze({
        code,
        message: safeMessage(error),
        details,
      }),
    }),
  });
}

function validateEnvelope(value) {
  if (!isPlainObject(value)) {
    throw new BridgeRequestError("INVALID_REQUEST", "Bridge input must be one JSON object");
  }
  if (value.protocolVersion !== BRIDGE_PROTOCOL_VERSION) {
    throw new BridgeRequestError(
      "UNSUPPORTED_PROTOCOL",
      `protocolVersion must be ${BRIDGE_PROTOCOL_VERSION}`,
    );
  }
  if (typeof value.action !== "string" || !ACTIONS.includes(value.action)) {
    throw new BridgeRequestError(
      "INVALID_REQUEST",
      "action is not supported by this bridge",
    );
  }
  if (value.action === "probe") {
    exactFields(value, ["protocolVersion", "action", "capabilitiesInput"], "probe request");
    requirePlainObject(value.capabilitiesInput, "capabilitiesInput");
  } else if (value.action === "execute") {
    exactFields(value, ["protocolVersion", "action", "projectKey", "requestInput"], "execute request");
    validateProjectKey(value.projectKey);
    requirePlainObject(value.requestInput, "requestInput");
  } else if (value.action === "inspect") {
    const keys = Object.keys(value);
    const byOperationId = keys.includes("operationId");
    const byRequest = keys.includes("requestInput");
    if (byOperationId === byRequest) {
      throw new BridgeRequestError(
        "INVALID_REQUEST",
        "inspect request requires exactly one of operationId or requestInput",
      );
    }
    exactFields(
      value,
      byOperationId
        ? ["protocolVersion", "action", "projectKey", "operationId"]
        : ["protocolVersion", "action", "projectKey", "requestInput"],
      "inspect request",
    );
    validateProjectKey(value.projectKey);
    if (byOperationId) {
      if (typeof value.operationId !== "string") {
        throw new BridgeRequestError("INVALID_REQUEST", "operationId must be a string");
      }
    } else {
      requirePlainObject(value.requestInput, "requestInput");
    }
  } else if (value.action === "graph_snapshot") {
    exactFields(
      value,
      ["protocolVersion", "action", "checkoutRoot", "parentTaskId", "observedAt"],
      "graph_snapshot request",
    );
  } else if (value.action === "projection_inspect") {
    exactFields(
      value,
      ["protocolVersion", "action", "checkoutRoot", "trellisTaskId"],
      "projection_inspect request",
    );
  } else if (value.action === "projection_apply") {
    exactFields(
      value,
      [
        "protocolVersion",
        "action",
        "checkoutRoot",
        "trellisTaskId",
        "expectedRevision",
        "projection",
      ],
      "projection_apply request",
    );
    requirePlainObject(value.projection, "projection");
  } else if (["lifecycle_prepare", "lifecycle_check", "lifecycle_finish"].includes(value.action)) {
    exactFields(
      value,
      ["protocolVersion", "action", "operationKind", "commandHash", "command", "checkoutRoot", "worktreePath", "worktreeId"],
      "lifecycle request",
    );
    requirePlainObject(value.command, "command");
    if (value.operationKind !== `${value.action.replace("lifecycle_", "")}_attempt`) {
      throw new BridgeRequestError("INVALID_REQUEST", "lifecycle operation kind does not match action");
    }
  } else {
    exactFields(
      value,
      ["protocolVersion", "action", "checkoutRoot", "trellisTaskId", "operationKind", "operationId", "expectedRequestPayloadHash"],
      "lifecycle inspect request",
    );
  }
  return value;
}

async function deriveGraphSnapshot(request, coreContext) {
  try {
    const snapshot = await deriveTrellisGraphSnapshot(
      coreContext.taskApi,
      {
        checkoutRoot: request.checkoutRoot,
        parentTaskId: request.parentTaskId,
        trellisVersion: coreContext.metadata.packageVersion,
        observedAt: request.observedAt,
      },
    );
    return Object.freeze({
      protocolVersion: BRIDGE_PROTOCOL_VERSION,
      ok: true,
      action: "graph_snapshot",
      snapshot: Object.freeze({
        exportVersion: snapshot.exportVersion,
        trellisVersion: snapshot.trellisVersion,
        parentTaskId: snapshot.parentTaskId,
        revision: snapshot.revision,
        observedAt: snapshot.observedAt,
        snapshotBase64: snapshot.snapshotBytes.toString("base64"),
        sourceSha256: snapshot.sourceSha256,
        byteLength: snapshot.snapshotBytes.length,
        complete: snapshot.complete,
      }),
      bridge: bridgeMetadata(coreContext.metadata),
    });
  } catch (error) {
    if (error instanceof TrellisGraphSnapshotError) {
      throw new BridgeRequestError(
        "GRAPH_SNAPSHOT_FAILURE",
        "Wish Builder graph snapshot derivation failed closed",
        {
          exitCode: 5,
          details: Object.freeze({ reason: error.code }),
        },
      );
    }
    throw new BridgeRequestError(
      "GRAPH_SNAPSHOT_FAILURE",
      "Wish Builder graph snapshot derivation failed closed",
      { exitCode: 5 },
    );
  }
}

async function inspectProjection(request, coreContext) {
  try {
    const projection = await inspectTrellisTaskProjection(
      coreContext.taskApi,
      {
        checkoutRoot: request.checkoutRoot,
        trellisTaskId: request.trellisTaskId,
      },
    );
    return Object.freeze({
      protocolVersion: BRIDGE_PROTOCOL_VERSION,
      ok: true,
      action: "projection_inspect",
      projection,
      bridge: bridgeMetadata(coreContext.metadata),
    });
  } catch (error) {
    throw projectionFailure(error);
  }
}

async function applyProjection(request, coreContext) {
  try {
    const projection = await applyTrellisTaskProjection(
      coreContext.taskApi,
      {
        checkoutRoot: request.checkoutRoot,
        trellisTaskId: request.trellisTaskId,
        expectedRevision: request.expectedRevision,
        projection: request.projection,
      },
    );
    return Object.freeze({
      protocolVersion: BRIDGE_PROTOCOL_VERSION,
      ok: true,
      action: "projection_apply",
      projection,
      bridge: bridgeMetadata(coreContext.metadata),
    });
  } catch (error) {
    throw projectionFailure(error);
  }
}

async function applyLifecycle(request, coreContext) {
  try {
    const observation = await applyTrellisLifecycle(coreContext.taskApi, {
      checkoutRoot: request.checkoutRoot,
      operationKind: request.operationKind,
      commandHash: request.commandHash,
      command: request.command,
      worktreePath: request.worktreePath,
      worktreeId: request.worktreeId,
    });
    return Object.freeze({
      protocolVersion: BRIDGE_PROTOCOL_VERSION,
      ok: true,
      action: request.action,
      observation,
      bridge: bridgeMetadata(coreContext.metadata),
    });
  } catch (error) {
    throw lifecycleFailure(error);
  }
}

async function inspectLifecycle(request, coreContext) {
  try {
    const observation = await inspectTrellisLifecycle(coreContext.taskApi, {
      checkoutRoot: request.checkoutRoot,
      trellisTaskId: request.trellisTaskId,
      operationKind: request.operationKind,
      operationId: request.operationId,
      expectedRequestPayloadHash: request.expectedRequestPayloadHash,
    });
    return Object.freeze({
      protocolVersion: BRIDGE_PROTOCOL_VERSION,
      ok: true,
      action: "lifecycle_inspect",
      observation,
      bridge: bridgeMetadata(coreContext.metadata),
    });
  } catch (error) {
    throw lifecycleFailure(error);
  }
}

function operationApiUnavailable(coreContext) {
  return new BridgeRequestError(
    "CORE_OPERATION_UNAVAILABLE",
    "Legacy Wish Builder probe, execute, and inspect actions are unavailable; official Trellis 0.6.15 has no operation runtime API",
    {
      exitCode: 5,
      details: Object.freeze({
        corePackageVersion: coreContext.metadata.packageVersion,
        reason: "official_operation_api_unavailable",
      }),
    },
  );
}

function projectionFailure(error) {
  if (error instanceof TrellisProjectionError) {
    return new BridgeRequestError(
      "PROJECTION_FAILURE",
      "Trellis projection failed closed",
      {
        exitCode: 5,
        details: Object.freeze({ reason: error.code }),
      },
    );
  }
  return new BridgeRequestError(
    "PROJECTION_FAILURE",
    "Trellis projection failed closed",
    { exitCode: 5 },
  );
}

function lifecycleFailure(error) {
  if (error instanceof TrellisLifecycleError) {
    return new BridgeRequestError(
      "LIFECYCLE_FAILURE",
      "Trellis lifecycle failed closed",
      { exitCode: 5, details: Object.freeze({ reason: error.code }) },
    );
  }
  return new BridgeRequestError(
    "LIFECYCLE_FAILURE",
    "Trellis lifecycle failed closed",
    { exitCode: 5 },
  );
}

function bridgeMetadata(metadata) {
  return Object.freeze({
    bridgeProtocolVersion: BRIDGE_PROTOCOL_VERSION,
    corePackageName: metadata.packageName,
    corePackageVersion: metadata.packageVersion,
    coreArchiveSha256: metadata.archiveSha256,
    coreArchiveVerified: metadata.archiveVerified,
    corePackageTreeSha256: metadata.packageTreeSha256,
    operationSchemaVersion: null,
    capabilitySchemaVersion: null,
    operationKinds: Object.freeze([]),
  });
}

function validateProjectKey(value) {
  if (typeof value !== "string" || !PROJECT_KEY.test(value)) {
    throw new BridgeRequestError("INVALID_REQUEST", "projectKey must be a safe project token");
  }
}

function requirePlainObject(value, field) {
  if (!isPlainObject(value)) {
    throw new BridgeRequestError("INVALID_REQUEST", `${field} must be an object`);
  }
}

function exactFields(value, fields, label) {
  const expected = new Set(fields);
  for (const key of Object.keys(value)) {
    if (!expected.has(key)) {
      throw new BridgeRequestError("INVALID_REQUEST", `${label} contains unknown field: ${key}`);
    }
  }
  for (const key of fields) {
    if (!(key in value)) {
      throw new BridgeRequestError("INVALID_REQUEST", `${label}.${key} is required`);
    }
  }
}

function isPlainObject(value) {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function safeMessage(error) {
  const source = error instanceof Error ? error.message : "Bridge failed closed";
  const normalized = String(source).replace(/[\u0000-\u001f\u007f-\u009f]+/g, " ").trim();
  return normalized.slice(0, 512) || "Bridge failed closed";
}
