import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import { buildManifest, contentFingerprint } from "../../src/manifest.js";
import { createIgnoreMatcher } from "../../src/ignore.js";
import { executeFullSync } from "../../src/sync.js";
import { listLocalFiles } from "../../src/localFiles.js";
import { loadState, saveState } from "../../src/state.js";

const cipher = { encryptPath: async (value) => value, decryptPath: async (value) => value, decryptContent: async (value) => value, encryptContent: async (value) => value };

async function runCase({ local = null, objects, manifestObjectKey = ".r2-sync-manifest.json", manifestKey = "separate-key", apply = false, seedState = false }) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "r2-sync-manifest-"));
  const vault = path.join(root, "vault");
  const state = path.join(root, "state.json");
  await fs.mkdir(vault);
  if (local !== null) await fs.writeFile(path.join(vault, "note.md"), local);
  if (seedState) {
    const stat = await fs.stat(path.join(vault, "note.md"));
    await saveState(state, { "note.md": { localMtimeMs: stat.mtimeMs, localSize: stat.size, remoteETag: "e1", localContentHash: contentFingerprint(Buffer.from(local), "raw-state-key") } });
  }
  const calls = [];
  const api = {
    async listRemote() { return [...objects.entries()].map(([key, value]) => ({ key, etag: value.etag, size: value.bytes.length })); },
    async getObject(key) { calls.push(key); const value = objects.get(key); if (!value) throw new Error("not found"); return { ...value, etag: value.etag }; },
    async putObject(key, bytes, metadata) { calls.push(`PUT:${key}`); objects.set(key, { bytes: Buffer.from(bytes), etag: `put-${key}`, metadata }); return { etag: `put-${key}` }; },
    async deleteObject(key) { objects.delete(key); },
  };
  try {
    const result = await executeFullSync({ vaultPath: vault, statePath: state, listRemote: api.listRemote, getObject: api.getObject, putObject: api.putObject, deleteObject: api.deleteObject, cipher, apply, allowDelete: true, listLocalFiles, loadState, saveState, ignoreMatcher: createIgnoreMatcher([]), manifestObjectKey, manifestKey });
    return { result, calls, objects, state, vault };
  } finally {
    if (!apply) await fs.rm(root, { recursive: true, force: true });
  }
}

{
  const content = Buffer.from("same");
  const objects = new Map([
    ["note.md", { bytes: content, etag: "e1" }],
    [".r2-sync-manifest.json", { bytes: Buffer.from(buildManifest({ "note.md": { etag: "e1", size: content.length, fingerprint: contentFingerprint(content, "separate-key") } })), etag: "manifest-1" }],
  ]);
  const { result, calls, state } = await runCase({ local: content, objects, apply: true, seedState: true });
  assert.equal(result.ok, true);
  assert.equal(result.planned, 0);
  assert.equal(result.manifestUpdated, false);
  assert.deepEqual(calls, [".r2-sync-manifest.json"]);
  await fs.rm(path.dirname(state), { recursive: true, force: true });
}

{
  const content = Buffer.from("same");
  const manifestKey = "separate-key";
  const objects = new Map([
    ["note.md", { bytes: content, etag: "e1" }],
    [".r2-sync-manifest.json", { bytes: Buffer.from(buildManifest({ "note.md": { etag: "e1", size: content.length, fingerprint: contentFingerprint(content, manifestKey) } })), etag: "manifest-1" }],
  ]);
  const { result, calls } = await runCase({ local: content, objects });
  assert.equal(result.ok, true);
  assert.equal(result.plannedByType.SEED, 1);
  assert.deepEqual(calls, [".r2-sync-manifest.json"]);
  assert.equal(result.manifestUpdated, false);
}

{
  const content = Buffer.from("same");
  const objects = new Map([
    ["note.md", { bytes: content, etag: "new-etag" }],
    [".r2-sync-manifest.json", { bytes: Buffer.from(buildManifest({ "note.md": { etag: "old-etag", size: content.length, fingerprint: contentFingerprint(content, "separate-key") } })), etag: "manifest-1" }],
  ]);
  const { result, calls } = await runCase({ local: content, objects });
  assert.equal(result.ok, true);
  assert.deepEqual(calls, [".r2-sync-manifest.json", "note.md"]);
}

{
  const objects = new Map([["note.md", { bytes: Buffer.from("same"), etag: "e1" }]]);
  const { result, calls, state } = await runCase({ local: Buffer.from("same"), objects, apply: true });
  assert.equal(result.ok, true);
  assert.equal(result.manifestUpdated, true);
  assert.ok(calls.includes("PUT:.r2-sync-manifest.json"));
  assert.match((await fs.readFile(state, "utf8")), /"note\.md"/);
  await fs.rm(path.dirname(state), { recursive: true, force: true });
}

console.log("manifest tests passed");
