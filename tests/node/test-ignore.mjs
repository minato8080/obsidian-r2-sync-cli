import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
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
  const configPath = path.join(smokeRoot, "config.json");
  writeFileSync(configPath, JSON.stringify({
    vaultPath: path.join(smokeRoot, "vault"), statePath: "state.json",
    endpoint: "http://127.0.0.1:1", bucket: "placeholder",
    accessKeyId: "placeholder", secretAccessKey: "placeholder",
    password: "placeholder", encryption: "plain", mode: "full",
  }));
  const smoke = spawnSync(process.execPath, [indexPath], {
    cwd: smokeRoot,
    encoding: "utf8",
    timeout: 10_000,
  });
  assert.notEqual(smoke.error?.code, "ETIMEDOUT", "Node CLI smoke test timed out");
  assert.equal(`${smoke.stdout}${smoke.stderr}`.includes("path is not defined"), false, "Node CLI referenced an unimported path binding");
} finally {
  rmSync(smokeRoot, { recursive: true, force: true });
}

const classifyRoot = mkdtempSync(path.join(os.tmpdir(), "r2-sync-check-ignore-"));
try {
  const indexPath = fileURLToPath(new URL("../../src/index.js", import.meta.url));
  const vault = path.join(classifyRoot, "vault");
  mkdirSync(path.join(vault, "ignored"), { recursive: true });
  mkdirSync(path.join(vault, ".agents"), { recursive: true });
  mkdirSync(path.join(vault, "mixed", "pure", "deep"), { recursive: true });
  mkdirSync(path.join(vault, "mixed"), { recursive: true });
  for (const relative of [
    "ignored/file.txt", ".agents/one.md", ".agents/two.md", "keep.txt",
    "mixed/keep.txt", "mixed/.env", "mixed/pure/a.txt", "mixed/pure/deep/b.txt",
    ".env", "state.json",
  ]) writeFileSync(path.join(vault, ...relative.split("/")), relative);
  const configPath = path.join(classifyRoot, "config.json");
  writeFileSync(configPath, JSON.stringify({
    vaultPath: vault, statePath: "state.json", endpoint: "http://127.0.0.1:1",
    bucket: "placeholder", accessKeyId: "placeholder", secretAccessKey: "placeholder",
    password: "placeholder", encryption: "plain", mode: "full",
    ignoreExtra: ["ignored/**", "**/.env"],
  }));
  const listed = spawnSync(process.execPath, [indexPath, "--config", configPath, "--check-ignore"], { cwd: classifyRoot, encoding: "utf8", timeout: 10_000 });
  const verbose = spawnSync(process.execPath, [indexPath, "--config", configPath, "--verbose", "--check-ignore"], { cwd: classifyRoot, encoding: "utf8", timeout: 10_000 });
  assert.equal(listed.status, 0, listed.stderr);
  assert.equal(verbose.status, 0, verbose.stderr);
  assert.match(listed.stdout, /\[IGNORE\] 4 entries/);
  assert.match(listed.stdout, /  ignored\/ \(all files\)/);
  assert.match(listed.stdout, /\[INCLUDE\] 6 files/);
  assert.match(listed.stdout, /  \.agents\/ \(2 files\)/);
  assert.match(listed.stdout, /  mixed\/pure\/ \(2 files\)/);
  assert.doesNotMatch(listed.stdout, /  \.agents\/one\.md/);
  assert.match(verbose.stdout, /\[IGNORE\] 5 entries/);
  assert.match(verbose.stdout, /  ignored\/file\.txt/);
  assert.match(verbose.stdout, /\[INCLUDE\] 6 files/);
  assert.match(verbose.stdout, /  \.agents\/one\.md/);
  assert.match(verbose.stdout, /  mixed\/pure\/deep\/b\.txt/);
} finally {
  rmSync(classifyRoot, { recursive: true, force: true });
}

console.log("ignore tests passed");
