import { createHash } from "node:crypto";
import { createRequire } from "node:module";
import { lstat, readFile, readdir, realpath } from "node:fs/promises";
import path from "node:path";

import { parseStrictJsonBytes } from "./strict-json.mjs";

const REQUIRE = createRequire(import.meta.url);
const PIN_MANIFEST_URL = new URL("./cli-pins.json", import.meta.url);
const TREE_HASH_DOMAIN = Buffer.from("wish-builder-trellis-cli-package-tree-v1\0", "utf8");

export class CliPinError extends Error {
  constructor(message) {
    super(message);
    this.name = "CliPinError";
  }
}

export async function loadPinnedTrellisCli(options = {}) {
  const environment = options.environment ?? process.env;
  const cwd = options.cwd ?? process.cwd();
  const pins = await readPinManifest();
  assertConfiguredPin(environment, "WISH_BUILDER_TRELLIS_CLI_VERSION_PIN", pins.packageVersion);
  assertConfiguredPin(environment, "WISH_BUILDER_TRELLIS_CLI_SHA256_PIN", pins.archiveSha256);
  assertConfiguredPin(environment, "WISH_BUILDER_TRELLIS_CLI_INTEGRITY_PIN", pins.npmIntegrity);
  assertConfiguredPin(environment, "WISH_BUILDER_TRELLIS_CLI_SHASUM_PIN", pins.npmShasum);
  assertConfiguredPin(
    environment,
    "WISH_BUILDER_TRELLIS_CLI_TREE_SHA256_PIN",
    pins.packageTreeSha256,
  );

  const packageRoot = await resolvePackageRoot(environment, cwd, pins);
  const packageJson = parseStrictJsonBytes(
    await readRegularFile(path.join(packageRoot, "package.json"), "Trellis CLI package.json"),
  );
  if (!isPlainObject(packageJson)) throw new CliPinError("Trellis CLI package.json must be an object");
  if (packageJson.name !== pins.packageName || packageJson.version !== pins.packageVersion) {
    throw new CliPinError("Trellis CLI package name or version does not match the immutable pin");
  }

  const packageTreeSha256 = await hashCliPackageTree(packageRoot);
  if (packageTreeSha256 !== pins.packageTreeSha256) {
    throw new CliPinError("Trellis CLI package tree does not match the immutable SHA-256 pin");
  }

  const cliEntry = await realpath(path.join(packageRoot, pins.cliEntry)).catch(() => {
    throw new CliPinError("Pinned Trellis CLI entry point is missing");
  });
  if (!isWithin(packageRoot, cliEntry)) {
    throw new CliPinError("Pinned Trellis CLI entry point escapes the verified package tree");
  }
  await readRegularFile(cliEntry, "Pinned Trellis CLI entry point");

  let archiveVerified = false;
  const archiveReference = environment.WISH_BUILDER_TRELLIS_CLI_ARCHIVE;
  if (archiveReference !== undefined && archiveReference !== "") {
    const archive = path.resolve(cwd, archiveReference);
    const archiveBytes = await readRegularFile(archive, "Trellis CLI archive");
    const archiveSha256 = `sha256:${createHash("sha256").update(archiveBytes).digest("hex")}`;
    if (archiveSha256 !== pins.archiveSha256) {
      throw new CliPinError("Trellis CLI archive does not match the immutable SHA-256 pin");
    }
    if (`sha512-${createHash("sha512").update(archiveBytes).digest("base64")}` !== pins.npmIntegrity) {
      throw new CliPinError("Trellis CLI archive does not match the official npm integrity pin");
    }
    if (createHash("sha1").update(archiveBytes).digest("hex") !== pins.npmShasum) {
      throw new CliPinError("Trellis CLI archive does not match the official npm shasum pin");
    }
    archiveVerified = true;
  }

  return Object.freeze({
    entryPath: cliEntry,
    packageRoot,
    metadata: Object.freeze({
      packageName: pins.packageName,
      packageVersion: pins.packageVersion,
      archiveSha256: pins.archiveSha256,
      archiveVerified,
      npmIntegrity: pins.npmIntegrity,
      npmShasum: pins.npmShasum,
      packageTreeSha256,
    }),
  });
}

export async function hashCliPackageTree(packageRoot) {
  const root = await realpath(packageRoot).catch(() => {
    throw new CliPinError("Configured Trellis CLI package root does not exist");
  });
  const files = await collectRegularFiles(root, root);
  files.sort((left, right) =>
    Buffer.compare(Buffer.from(left.relative, "utf8"), Buffer.from(right.relative, "utf8")),
  );
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
  if (!isPlainObject(value)) throw new CliPinError("Trellis CLI pin manifest must be an object");
  exactFields(
    value,
    [
      "archiveSha256",
      "cliEntry",
      "npmIntegrity",
      "npmShasum",
      "packageName",
      "packageTreeSha256",
      "packageVersion",
      "schemaVersion",
    ],
    "Trellis CLI pin manifest",
  );
  if (
    value.schemaVersion !== 1 ||
    value.packageName !== "@mindfoldhq/trellis" ||
    value.packageVersion !== "0.6.15" ||
    value.cliEntry !== "dist/cli/index.js" ||
    !/^sha256:[0-9a-f]{64}$/.test(value.archiveSha256) ||
    !/^sha512-[A-Za-z0-9+/]+={0,2}$/.test(value.npmIntegrity) ||
    !/^[0-9a-f]{40}$/.test(value.npmShasum) ||
    !/^sha256:[0-9a-f]{64}$/.test(value.packageTreeSha256)
  ) {
    throw new CliPinError("Trellis CLI pin manifest does not describe official 0.6.15");
  }
  return Object.freeze({ ...value });
}

async function resolvePackageRoot(environment, cwd, pins) {
  const configured = environment.WISH_BUILDER_TRELLIS_CLI_ROOT;
  let candidate;
  if (configured !== undefined && configured !== "") {
    candidate = path.resolve(cwd, configured);
  } else {
    try {
      candidate = path.dirname(REQUIRE.resolve(`${pins.packageName}/package.json`));
    } catch {
      throw new CliPinError(
        "Pinned Trellis CLI is not installed; configure WISH_BUILDER_TRELLIS_CLI_ROOT",
      );
    }
  }
  return realpath(candidate).catch(() => {
    throw new CliPinError("Configured Trellis CLI package root does not exist");
  });
}

async function collectRegularFiles(root, directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const absolute = path.join(directory, entry.name);
    // Dependencies are installed separately and are not bytes from the pinned
    // published package archive. Verify the package payload, not npm's nested
    // dependency layout.
    if (directory === root && entry.name === "node_modules") continue;
    if (entry.isSymbolicLink()) throw new CliPinError("Trellis CLI package tree must not contain symlinks");
    if (entry.isDirectory()) {
      files.push(...(await collectRegularFiles(root, absolute)));
    } else if (entry.isFile()) {
      files.push({ absolute, relative: path.relative(root, absolute).split(path.sep).join("/") });
    } else {
      throw new CliPinError("Trellis CLI package tree contains an unsupported filesystem entry");
    }
  }
  return files;
}

async function readRegularFile(file, label) {
  let stat;
  try {
    stat = await lstat(file);
  } catch {
    throw new CliPinError(`${label} is missing`);
  }
  if (!stat.isFile() || stat.isSymbolicLink()) throw new CliPinError(`${label} must be a regular file`);
  return readFile(file);
}

function isWithin(root, candidate) {
  const relative = path.relative(root, candidate);
  return relative !== "" && !relative.startsWith(`..${path.sep}`) && relative !== ".." && !path.isAbsolute(relative);
}

function assertConfiguredPin(environment, name, expected) {
  const configured = environment[name];
  if (configured !== undefined && configured !== "" && configured !== expected) {
    throw new CliPinError(`${name} cannot relax the immutable Trellis CLI pin`);
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
    if (!expected.has(key)) throw new CliPinError(`${label} contains unknown field: ${key}`);
  }
  for (const key of fields) {
    if (!(key in value)) throw new CliPinError(`${label}.${key} is required`);
  }
}
