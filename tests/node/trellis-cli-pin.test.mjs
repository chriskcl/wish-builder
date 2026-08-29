import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import { loadPinnedTrellisCli } from "../../wish_builder/bridges/trellis_core/cli-loader.mjs";

const REPOSITORY_ROOT = path.resolve(fileURLToPath(new URL("../..", import.meta.url)));
const WORK_ROOT = path.resolve(REPOSITORY_ROOT, "..", "work");
const CLI_ROOT = path.join(WORK_ROOT, "tools", "trellis-0.6.15", "package");
const CLI_ARCHIVE = path.join(
  WORK_ROOT,
  "artifacts",
  "trellis-0.6.15",
  "mindfoldhq-trellis-0.6.15.tgz",
);
const CLI_ARCHIVE_SHA256 =
  "sha256:7b97e4247f54e71f22ff80caa328d9e68fb81908f984f15d70a4d81cc2a0306c";
const CLI_NPM_INTEGRITY =
  "sha512-grbF8PToesHojsaWkoG4+Aupih7eZHkXH5y33uzPrWQXwIRewwlM1AoeJEttcXAia9nLZzF/ezuR338PWCKv+A==";
const CLI_NPM_SHASUM = "2119748cccde49006e9897fc7a3481febdf98076";
const CLI_TREE_SHA256 =
  "sha256:f11904ad9d93e2e0dfdb7add3e4eb2caf7dd41d388a4a396aef5bf8305bdcceb";

test("official 0.6.15 CLI is pinned independently from Core", async (t) => {
  if (!fixtureAvailable(t)) return;
  const loaded = await loadPinnedTrellisCli({ environment: cliEnvironment() });
  assert.equal(loaded.metadata.packageName, "@mindfoldhq/trellis");
  assert.equal(loaded.metadata.packageVersion, "0.6.15");
  assert.equal(loaded.metadata.archiveSha256, CLI_ARCHIVE_SHA256);
  assert.equal(loaded.metadata.npmIntegrity, CLI_NPM_INTEGRITY);
  assert.equal(loaded.metadata.npmShasum, CLI_NPM_SHASUM);
  assert.equal(loaded.metadata.packageTreeSha256, CLI_TREE_SHA256);
  assert.equal(loaded.metadata.archiveVerified, true);
  assert.equal(loaded.entryPath, path.join(CLI_ROOT, "dist", "cli", "index.js"));
});

test("immutable CLI pins cannot be relaxed", async (t) => {
  if (!fixtureAvailable(t)) return;
  for (const override of [
    { WISH_BUILDER_TRELLIS_CLI_VERSION_PIN: "0.6.14" },
    { WISH_BUILDER_TRELLIS_CLI_SHA256_PIN: `sha256:${"0".repeat(64)}` },
    { WISH_BUILDER_TRELLIS_CLI_INTEGRITY_PIN: `sha512-${"A".repeat(88)}` },
    { WISH_BUILDER_TRELLIS_CLI_SHASUM_PIN: "0".repeat(40) },
    { WISH_BUILDER_TRELLIS_CLI_TREE_SHA256_PIN: `sha256:${"0".repeat(64)}` },
  ]) {
    await assert.rejects(
      loadPinnedTrellisCli({ environment: { ...cliEnvironment(), ...override } }),
      /cannot relax/,
    );
  }
});

test("CLI archive mismatch fails closed", async (t) => {
  if (!fixtureAvailable(t)) return;
  await assert.rejects(
    loadPinnedTrellisCli({
      environment: {
        ...cliEnvironment(),
        WISH_BUILDER_TRELLIS_CLI_ARCHIVE: path.join(CLI_ROOT, "package.json"),
      },
    }),
    /archive does not match/,
  );
});

function fixtureAvailable(t) {
  const available = existsSync(CLI_ROOT) && existsSync(CLI_ARCHIVE);
  if (!available) t.skip("local official Trellis CLI 0.6.15 fixture is unavailable");
  return available;
}

function cliEnvironment() {
  return {
    WISH_BUILDER_TRELLIS_CLI_ROOT: CLI_ROOT,
    WISH_BUILDER_TRELLIS_CLI_ARCHIVE: CLI_ARCHIVE,
  };
}
