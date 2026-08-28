import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import test from "node:test";

import { loadPinnedTrellisCore } from "../../wish_builder/bridges/trellis_core/core-loader.mjs";
import { parseStrictJsonBytes } from "../../wish_builder/bridges/trellis_core/strict-json.mjs";

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
const CORE_ARCHIVE = path.join(
  WORK_ROOT,
  "artifacts",
  "trellis-0.6.15",
  "mindfoldhq-trellis-core-0.6.15.tgz",
);
const ARCHIVE_SHA256 =
  "sha256:3af3e71fbaba3b4e7f081ca7df39dc9d00f9c527d855dc159263f4a34cf8587a";
const NPM_INTEGRITY =
  "sha512-UYMVMM47Zyr/ns39U/f620cs7XaFKX2yez91QMV40Eah+uxxEdGwYHgNjDPZxwMhlr/0TIsZuMM+KF6lcbxg9w==";
const NPM_SHASUM = "65b82a3a03af7400de943fe52564dfd8a95a4703";
const TREE_SHA256 =
  "sha256:49602e2bbd8a9f172c63e0bcd341810b3a70fe592adf1860c15a213898c790af";

test("strict JSON rejects duplicate keys at every depth", () => {
  assert.throws(
    () => parseStrictJsonBytes(Buffer.from('{"a":{"x":1,"x":2}}')),
    /duplicate key/,
  );
});

test("strict JSON rejects multiple values, invalid UTF-8, and non-finite numbers", () => {
  assert.throws(() => parseStrictJsonBytes(Buffer.from("{} {}")), /exactly one value/);
  assert.throws(() => parseStrictJsonBytes(Buffer.from([0xff])), /valid UTF-8/);
  assert.throws(() => parseStrictJsonBytes(Buffer.from("1e400")), /finite/);
});

test("CLI always returns one strict JSON object for hostile input", () => {
  for (const input of [
    '{"protocolVersion":1,"protocolVersion":1,"action":"probe","capabilitiesInput":{}}',
    "{} {}",
    "[]",
    '{"protocolVersion":1,"action":"probe","capabilitiesInput":{},"extra":true}',
  ]) {
    const result = runBridgeRaw(input);
    assert.equal(result.status, 2);
    assert.equal(result.stderr, "");
    const response = parseStrictJsonBytes(Buffer.from(result.stdout));
    assert.equal(response.ok, false);
    assert.deepEqual(Object.keys(response).sort(), [
      "action",
      "error",
      "ok",
      "protocolVersion",
    ]);
  }
});

test("CLI rejects extra argv without loading Core", () => {
  const result = spawnSync(process.execPath, [BRIDGE, "unexpected"], {
    input: "{}",
    encoding: "utf8",
  });
  assert.equal(result.status, 2);
  assert.equal(JSON.parse(result.stdout).error.code, "INVALID_INVOCATION");
});

test("official 0.6.15 Core is pinned by archive, npm metadata, and extracted tree", async (t) => {
  if (!fixtureAvailable(t)) return;
  const loaded = await loadPinnedTrellisCore({
    environment: coreEnvironment(),
  });
  assert.equal(loaded.metadata.packageName, "@mindfoldhq/trellis-core");
  assert.equal(loaded.metadata.packageVersion, "0.6.15");
  assert.equal(loaded.metadata.archiveSha256, ARCHIVE_SHA256);
  assert.equal(loaded.metadata.npmIntegrity, NPM_INTEGRITY);
  assert.equal(loaded.metadata.npmShasum, NPM_SHASUM);
  assert.equal(loaded.metadata.packageTreeSha256, TREE_SHA256);
  assert.equal(loaded.metadata.archiveVerified, true);
  for (const name of ["loadTaskRecord", "writeTaskRecord", "inferTaskPhase"]) {
    assert.equal(typeof loaded.taskApi[name], "function", name);
  }
  assert.equal(typeof loaded.taskApi.taskRecordSchema.parse, "function");
});

test("immutable Core pins cannot be relaxed", async (t) => {
  if (!fixtureAvailable(t)) return;
  for (const override of [
    { WISH_BUILDER_TRELLIS_CORE_VERSION_PIN: "0.6.14" },
    { WISH_BUILDER_TRELLIS_CORE_SHA256_PIN: `sha256:${"0".repeat(64)}` },
    { WISH_BUILDER_TRELLIS_CORE_INTEGRITY_PIN: `sha512-${"A".repeat(88)}` },
    { WISH_BUILDER_TRELLIS_CORE_SHASUM_PIN: "0".repeat(40) },
    { WISH_BUILDER_TRELLIS_CORE_TREE_SHA256_PIN: `sha256:${"0".repeat(64)}` },
  ]) {
    await assert.rejects(
      loadPinnedTrellisCore({ environment: { ...coreEnvironment(), ...override } }),
      /cannot relax/,
    );
  }
});

test("legacy operation actions fail closed on official 0.6.15", (t) => {
  if (!fixtureAvailable(t)) return;
  const requests = [
    { protocolVersion: 1, action: "probe", capabilitiesInput: {} },
    {
      protocolVersion: 1,
      action: "execute",
      projectKey: "official-0.6.15",
      requestInput: {},
    },
    {
      protocolVersion: 1,
      action: "inspect",
      projectKey: "official-0.6.15",
      operationId: "operation-1",
    },
  ];
  for (const request of requests) {
    const result = runBridge(request, coreEnvironment());
    assert.equal(result.status, 5, JSON.stringify(result.body));
    assert.equal(result.stderr, "");
    assert.equal(result.body.ok, false);
    assert.equal(result.body.action, request.action);
    assert.equal(result.body.error.code, "CORE_OPERATION_UNAVAILABLE");
    assert.equal(
      result.body.error.message,
      "Legacy Wish Builder probe, execute, and inspect actions are unavailable; official Trellis 0.6.15 has no operation runtime API",
    );
    assert.deepEqual({ ...result.body.error.details }, {
      corePackageVersion: "0.6.15",
      reason: "official_operation_api_unavailable",
    });
  }
});

test("a mismatched archive fails before any operation response", (t) => {
  if (!fixtureAvailable(t)) return;
  const result = runBridge(
    { protocolVersion: 1, action: "probe", capabilitiesInput: {} },
    {
      ...coreEnvironment(),
      WISH_BUILDER_TRELLIS_CORE_ARCHIVE: path.join(CORE_ROOT, "package.json"),
    },
  );
  assert.equal(result.status, 3);
  assert.equal(result.body.error.code, "CORE_PIN_MISMATCH");
});

function fixtureAvailable(t) {
  const available = existsSync(CORE_ROOT) && existsSync(CORE_ARCHIVE);
  if (!available) t.skip("local official Trellis 0.6.15 fixture is unavailable");
  return available;
}

function coreEnvironment() {
  return {
    WISH_BUILDER_TRELLIS_CORE_ROOT: CORE_ROOT,
    WISH_BUILDER_TRELLIS_CORE_ARCHIVE: CORE_ARCHIVE,
  };
}

function runBridge(request, environment = {}) {
  const result = runBridgeRaw(JSON.stringify(request), environment);
  return { ...result, body: parseStrictJsonBytes(Buffer.from(result.stdout)) };
}

function runBridgeRaw(input, environment = {}) {
  return spawnSync(process.execPath, [BRIDGE], {
    input,
    env: { ...process.env, ...environment },
    encoding: "utf8",
    maxBuffer: 4 * 1024 * 1024,
  });
}
