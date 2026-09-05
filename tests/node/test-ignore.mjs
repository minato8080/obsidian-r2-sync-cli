import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createIgnoreMatcher } from "../../src/ignore.js";

const anchored = createIgnoreMatcher(["/sync.py", "/r2-sync-config.json", "/generated/"] , { baseRelPath: "tools" });
assert.equal(anchored.isIgnoredFile("tools/sync.py"), true);
assert.equal(anchored.isIgnoredFile("tools/r2-sync-config.json"), true);
assert.equal(anchored.isIgnoredDir("tools/generated"), true);
assert.equal(anchored.isIgnoredFile("tools/nested/sync.py"), false);
assert.equal(anchored.isIgnoredFile("tools/nested/r2-sync-config.json"), false);
assert.equal(anchored.isIgnoredFile("tools/nested/generated/note.md"), false);
assert.equal(anchored.isIgnoredFile("other/sync.py"), false);

const recursive = createIgnoreMatcher(["/**"], { baseRelPath: "tools" });
assert.equal(recursive.isIgnoredFile("tools/sync.py"), true);
assert.equal(recursive.isIgnoredFile("tools/nested/generated/note.md"), true);
assert.equal(recursive.isIgnoredDir("tools/nested"), true);
assert.equal(recursive.isIgnoredFile("notes/keep.md"), false);

const protectedState = createIgnoreMatcher([], { baseRelPath: "tools", protectedRelPaths: ["tools/r2-sync-state.json", "tools/.env"] });
assert.equal(protectedState.isIgnoredFile("tools/r2-sync-state.json"), true);
assert.equal(protectedState.isIgnoredFile("tools/.r2-sync-state.json.interrupted.tmp"), true);
assert.equal(protectedState.isIgnoredFile("tools/.env"), true);
assert.equal(protectedState.isIgnoredFile("notes/r2-sync-state.json"), false);

const glob = createIgnoreMatcher(["/generated/*.json", "**/secret?.md"], { baseRelPath: "tools" });
assert.equal(glob.isIgnoredFile("tools/generated/config.json"), true);
assert.equal(glob.isIgnoredFile("tools/generated/deep/config.json"), false);
assert.equal(glob.isIgnoredFile("tools/nested/secret1.md"), true);
assert.equal(glob.isIgnoredFile("notes/secret1.md"), false);

const legacyAnyDepth = createIgnoreMatcher(["sync.py"]);
assert.equal(legacyAnyDepth.isIgnoredFile("sync.py"), true);
assert.equal(legacyAnyDepth.isIgnoredFile("nested/sync.py"), true);

const negatedClass = createIgnoreMatcher(["**/secret[!0].md"], { baseRelPath: "tools" });
assert.equal(negatedClass.isIgnoredFile("tools/secret0.md"), false);
assert.equal(negatedClass.isIgnoredFile("tools/secret1.md"), true);
assert.equal(negatedClass.isIgnoredFile("tools/nested/secret2.md"), true);

const rootBased = createIgnoreMatcher(["/**"], { vaultPath: "/vault", basePath: "/vault" });
assert.equal(rootBased.isIgnoredFile("note.md"), true);

const smokeRoot = mkdtempSync(path.join(os.tmpdir(), "r2-sync-index-smoke-"));
try {
  const indexPath = fileURLToPath(new URL("../../src/index.js", import.meta.url));
  const smoke = spawnSync(process.execPath, [indexPath], {
    cwd: smokeRoot,
    env: {
      ...process.env,
      VAULT_PATH: path.join(smokeRoot, "vault"),
      R2_ENDPOINT: "http://127.0.0.1:1",
      R2_BUCKET: "placeholder",
      R2_ACCESS_KEY_ID: "placeholder",
      R2_SECRET_ACCESS_KEY: "placeholder",
      SYNC_PASSWORD: "placeholder",
    },
    encoding: "utf8",
    timeout: 10_000,
  });
  assert.notEqual(smoke.error?.code, "ETIMEDOUT", "Node CLI smoke test timed out");
  assert.equal(`${smoke.stdout}${smoke.stderr}`.includes("path is not defined"), false, "Node CLI referenced an unimported path binding");
} finally {
  rmSync(smokeRoot, { recursive: true, force: true });
}

console.log("ignore tests passed");
