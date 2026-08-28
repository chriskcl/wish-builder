import { createHash } from "node:crypto";
import { createRequire } from "node:module";
import { lstat, readFile, readdir, realpath } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { parseStrictJsonBytes } from "./strict-json.mjs";

const REQUIRE = createRequire(import.meta.url);
const PIN_MANIFEST_URL = new URL("./pins.json", import.meta.url);
const TREE_HASH_DOMAIN = Buffer.from("wish-builder-trellis-package-tree-v1\0", "utf8");
const TASK_MODULE = "dist/task/index.js";

export class CorePinError extends Error {
  constructor(message) {
    super(message);
    this.name = "CorePinError";
  }
}

export async function loadPinnedTrellisCore(options = {}) {
  const environment = options.environment ?? process.env;
  const cwd = options.cwd ?? process.cwd();
  const pins = await readPinManifest();
  assertConfiguredPin(environment, "WISH_BUILDER_TRELLIS_CORE_VERSION_PIN", pins.packageVersion);
  assertConfiguredPin(environment, "WISH_BUILDER_TRELLIS_CORE_SHA256_PIN", pins.archiveSha256);
  assertConfiguredPin(environment, "WISH_BUILDER_TRELLIS_CORE_INTEGRITY_PIN", pins.npmIntegrity);
  assertConfiguredPin(environment, "WISH_BUILDER_TRELLIS_CORE_SHASUM_PIN", pins.npmShasum);
  assertConfiguredPin(environment, "WISH_BUILDER_TRELLIS_CORE_TREE_SHA256_PIN", pins.packageTreeSha256);

  const locations = await resolveCoreLocations(environment, cwd, pins);
  const packageBytes = await readRegularFile(locations.packageJson, "Trellis Core package.json");
  const packageJson = parseStrictJsonBytes(packageBytes);
  if (!isPlainObject(packageJson)) throw new CorePinError("Trellis Core package.json must be an object");
  if (packageJson.name !== pins.packageName || packageJson.version !== pins.packageVersion) {
    throw new CorePinError("Trellis Core package name or version does not match the immutable pin");
  }

  const packageTreeSha256 = await hashPackageTree(locations.packageRoot);
  if (packageTreeSha256 !== pins.packageTreeSha256) {
    throw new CorePinError("Trellis Core package tree does not match the immutable SHA-256 pin");
  }

  let archiveVerified = false;
  const archivePath = environment.WISH_BUILDER_TRELLIS_CORE_ARCHIVE;
  if (archivePath !== undefined && archivePath !== "") {
    const archive = path.resolve(cwd, archivePath);
    const archiveBytes = await readRegularFile(archive, "Trellis Core archive");
    const archiveSha256 = `sha256:${createHash("sha256").update(archiveBytes).digest("hex")}`;
    if (archiveSha256 !== pins.archiveSha256) {
      throw new CorePinError("Trellis Core archive does not match the immutable SHA-256 pin");
    }
    if (`sha512-${createHash("sha512").update(archiveBytes).digest("base64")}` !== pins.npmIntegrity) {
      throw new CorePinError("Trellis Core archive does not match the official npm integrity pin");
    }
    if (createHash("sha1").update(archiveBytes).digest("hex") !== pins.npmShasum) {
      throw new CorePinError("Trellis Core archive does not match the official npm shasum pin");
    }
    archiveVerified = true;
  }

  let api;
  let taskApi;
  try {
    api = await import(pathToFileURL(locations.channelModule).href);
    taskApi = await import(pathToFileURL(locations.taskModule).href);
  } catch {
    throw new CorePinError("Pinned Trellis Core channel or task module could not be imported");
  }
  assertCoreContract(api);
  assertTaskContract(taskApi);
  return Object.freeze({
    api,
    taskApi,
    metadata: Object.freeze({
      packageName: pins.packageName,
      packageVersion: pins.packageVersion,
      archiveSha256: pins.archiveSha256,
      archiveVerified,
      npmIntegrity: pins.npmIntegrity,
      npmShasum: pins.npmShasum,
      packageTreeSha256,
      taskApiSurface: Object.freeze([
        "inferTaskPhase",
        "loadTaskRecord",
        "taskRecordSchema",
        "writeTaskRecord",
      ]),
    }),
  });
}

export async function hashPackageTree(packageRoot) {
  const root = await realpath(packageRoot);
  const files = await collectRegularFiles(root, root);
  files.sort((left, right) => Buffer.compare(Buffer.from(left.relative, "utf8"), Buffer.from(right.relative, "utf8")));
  const hash = createHash("sha256");
  hash.update(TREE_HASH_DOMAIN);
  for (const entry of files) {
    const relative = Buffer.from(entry.relative, "utf8");
    const contents = await readFile(entry.absolute);
    hash.update(lengthPrefix(relative.length));
    hash.update(relative);
    hash.update(lengthPrefix(contents.length));
    hash.update(contents);
  }
  return `sha256:${hash.digest("hex")}`;
}

async function readPinManifest() {
  const value = parseStrictJsonBytes(await readFile(PIN_MANIFEST_URL));
  if (!isPlainObject(value)) throw new CorePinError("Trellis Core pin manifest must be an object");
  exactFields(value, [
    "archiveSha256",
    "channelModule",
    "npmIntegrity",
    "npmShasum",
    "packageName",
    "packageTreeSha256",
    "packageVersion",
    "schemaVersion",
  ], "Trellis Core pin manifest");
  if (
    value.schemaVersion !== 1 ||
    value.packageName !== "@mindfoldhq/trellis-core" ||
    value.packageVersion !== "0.6.15" ||
    value.channelModule !== "dist/channel/index.js" ||
    !/^sha256:[0-9a-f]{64}$/.test(value.archiveSha256) ||
    !/^sha512-[A-Za-z0-9+/]+={0,2}$/.test(value.npmIntegrity) ||
    !/^[0-9a-f]{40}$/.test(value.npmShasum) ||
    !/^sha256:[0-9a-f]{64}$/.test(value.packageTreeSha256)
  ) {
    throw new CorePinError("Trellis Core pin manifest does not describe official 0.6.15");
  }
  return Object.freeze({ ...value });
}

async function resolveCoreLocations(environment, cwd, pins) {
  let configuredModule = environment.WISH_BUILDER_TRELLIS_CORE_MODULE;
  let configuredRoot = environment.WISH_BUILDER_TRELLIS_CORE_ROOT;
  let modulePath = configuredModule ? resolveModuleReference(configuredModule, cwd) : null;
  let packageRoot;
  if (configuredRoot) {
    packageRoot = path.resolve(cwd, configuredRoot);
  } else if (modulePath !== null) {
    packageRoot = path.resolve(path.dirname(modulePath), "..", "..");
  } else {
    try {
      packageRoot = path.dirname(REQUIRE.resolve(`${pins.packageName}/package.json`));
    } catch {
      throw new CorePinError(
        "Pinned Trellis Core is not installed; configure WISH_BUILDER_TRELLIS_CORE_ROOT",
      );
    }
  }
  packageRoot = await realpath(packageRoot).catch(() => {
    throw new CorePinError("Configured Trellis Core package root does not exist");
  });
  const expectedModule = await realpath(path.join(packageRoot, pins.channelModule)).catch(() => {
    throw new CorePinError("Pinned Trellis Core channel module is missing");
  });
  const taskModule = await realpath(path.join(packageRoot, TASK_MODULE)).catch(() => {
    throw new CorePinError("Pinned Trellis Core task module is missing");
  });
  if (modulePath !== null) {
    modulePath = await realpath(modulePath).catch(() => {
      throw new CorePinError("Configured Trellis Core module does not exist");
    });
    if (modulePath !== expectedModule) {
      throw new CorePinError("Configured Trellis Core module is not the pinned channel entry point");
    }
  }
  return Object.freeze({
    packageRoot,
    packageJson: path.join(packageRoot, "package.json"),
    channelModule: expectedModule,
    taskModule,
  });
}

function resolveModuleReference(reference, cwd) {
  if (reference.startsWith("file:")) return fileURLToPath(reference);
  if (path.isAbsolute(reference) || reference.startsWith(".") || reference.includes("/") || reference.includes("\\")) {
    const candidate = path.resolve(cwd, reference);
    if (path.isAbsolute(reference) || reference.startsWith(".")) return candidate;
  }
  try {
    return REQUIRE.resolve(reference);
  } catch {
    throw new CorePinError("Configured Trellis Core module could not be resolved");
  }
}

async function collectRegularFiles(root, directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const absolute = path.join(directory, entry.name);
    if (entry.isSymbolicLink()) throw new CorePinError("Trellis Core package tree must not contain symlinks");
    if (entry.isDirectory()) {
      files.push(...(await collectRegularFiles(root, absolute)));
    } else if (entry.isFile()) {
      files.push({ absolute, relative: path.relative(root, absolute).split(path.sep).join("/") });
    } else {
      throw new CorePinError("Trellis Core package tree contains an unsupported filesystem entry");
    }
  }
  return files;
}

async function readRegularFile(file, label) {
  let stat;
  try {
    stat = await lstat(file);
  } catch {
    throw new CorePinError(`${label} is missing`);
  }
  if (!stat.isFile() || stat.isSymbolicLink()) throw new CorePinError(`${label} must be a regular file`);
  return readFile(file);
}

function assertCoreContract(api) {
  const functions = [
    "createChannel",
    "listWorkers",
    "probeWorkerRuntime",
    "sendMessage",
  ];
  if (functions.some((name) => typeof api[name] !== "function")) {
    throw new CorePinError("Trellis Core channel module is missing an official 0.6.15 API");
  }
}

function assertTaskContract(api) {
  for (const name of [
    "loadTaskRecord",
    "writeTaskRecord",
    "inferTaskPhase",
  ]) {
    if (typeof api[name] !== "function") {
      throw new CorePinError(`Trellis Core task module is missing ${name}`);
    }
  }
  if (
    !isPlainObject(api.taskRecordSchema) ||
    typeof api.taskRecordSchema.parse !== "function" ||
    typeof api.taskRecordSchema.safeParse !== "function"
  ) {
    throw new CorePinError("Trellis Core task module is missing taskRecordSchema");
  }
}

function assertConfiguredPin(environment, name, expected) {
  const configured = environment[name];
  if (configured !== undefined && configured !== "" && configured !== expected) {
    throw new CorePinError(`${name} cannot relax the immutable Trellis Core pin`);
  }
}

function lengthPrefix(value) {
  const buffer = Buffer.alloc(8);
  buffer.writeBigUInt64BE(BigInt(value));
  return buffer;
}

function isPlainObject(value) {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function exactFields(value, fields, label) {
  const expected = new Set(fields);
  for (const key of Object.keys(value)) {
    if (!expected.has(key)) throw new CorePinError(`${label} contains unknown field: ${key}`);
  }
  for (const key of fields) {
    if (!(key in value)) throw new CorePinError(`${label}.${key} is required`);
  }
}
