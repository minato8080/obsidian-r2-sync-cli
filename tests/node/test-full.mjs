import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs/promises";
import { createServer } from "node:http";
import os from "node:os";
import path from "node:path";

import { createCipher } from "../../src/crypto.js";
import { createIgnoreMatcher } from "../../src/ignore.js";
import { executeProbe } from "../../src/probe.js";
import { loadState, saveState } from "../../src/state.js";
import { executeFullSync } from "../../src/sync.js";
import { listLocalFiles, reconcileLocalFiles } from "../../src/localFiles.js";

const cipher = await createCipher("", "plain");

function remoteApi(objects, { failPut = false } = {}) {
  return {
    async listRemote() { return [...objects.entries()].map(([key, value]) => ({ key, etag: value.etag, size: value.bytes.length })); },
    async getObject(key) { const value = objects.get(key); return { ...value, etag: value.etag }; },
    async putObject(key, bytes, metadata) { if (failPut) throw new Error("simulated PUSH failure"); const etag = `put-${key}`; objects.set(key, { bytes: Buffer.from(bytes), metadata, etag }); return { etag }; },
    async deleteObject(key) { objects.delete(key); },
  };
}

async function makeRoot(prefix) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), prefix));
  const vault = path.join(root, "vault");
  await fs.mkdir(vault);
  return { root, vault, state: path.join(root, "state.json") };
}

function runCli(args, options) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, args, { ...options, stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "", stderr = "";
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.on("error", reject);
    child.on("close", (status) => resolve({ status, stdout, stderr }));
  });
}

async function run() {
  {
    const { root, vault } = await makeRoot("r2-sync-full-path-");
    try {
      const outside = path.join(root, "outside");
      await fs.mkdir(outside);
      await fs.writeFile(path.join(outside, "secret.md"), "outside");
      await fs.symlink(outside, path.join(vault, "link"), process.platform === "win32" ? "junction" : "dir");
      await assert.rejects(() => reconcileLocalFiles(vault, new Map(), ["link/secret.md"]), /escapes vault/);
    } finally { await fs.rm(root, { recursive: true, force: true }); }
  }

  {
    const { root, vault, state } = await makeRoot("r2-sync-full-collision-");
    try {
      const target = path.join(vault, "é.md");
      await fs.writeFile(target, "keep");
      const stat = await fs.stat(target);
      await saveState(state, { "é.md": { localMtimeMs: stat.mtimeMs, localSize: stat.size, remoteETag: "old", localContentHash: "keep" } });
      const objects = new Map([
        ["é.md", { bytes: Buffer.from("one"), etag: "one" }],
        ["é.md", { bytes: Buffer.from("two"), etag: "two" }],
      ]);
      const api = remoteApi(objects);
      const result = await executeFullSync({ vaultPath: vault, statePath: state, listRemote: api.listRemote, getObject: api.getObject, putObject: api.putObject, deleteObject: api.deleteObject, cipher, apply: true, allowDelete: true, listLocalFiles, loadState, saveState, ignoreMatcher: createIgnoreMatcher([]) });
      assert.equal(result.ok, false);
      assert.ok(result.conflicts.some((item) => item.path === "é.md"));
      assert.equal(result.appliedByType.DELETE_LOCAL, undefined);
      await fs.access(target);
    } finally { await fs.rm(root, { recursive: true, force: true }); }
  }

  {
    const { root, vault, state } = await makeRoot("r2-sync-full-push-failure-");
    try {
      await fs.writeFile(path.join(vault, "local.md"), "local");
      const objects = new Map([["remote.md", { bytes: Buffer.from("remote"), etag: "remote" }]]);
      const api = remoteApi(objects, { failPut: true });
      const result = await executeFullSync({ vaultPath: vault, statePath: state, listRemote: api.listRemote, getObject: api.getObject, putObject: api.putObject, deleteObject: api.deleteObject, cipher, apply: true, allowDelete: true, listLocalFiles, loadState, saveState, ignoreMatcher: createIgnoreMatcher([]) });
      assert.equal(result.ok, false);
      assert.ok(result.errors.some((item) => item.relPath === "local.md"));
      await assert.rejects(() => fs.access(path.join(vault, "remote.md")));
    } finally { await fs.rm(root, { recursive: true, force: true }); }
  }

  {
    const { root, vault, state } = await makeRoot("r2-sync-probe-protect-");
    try {
      await fs.writeFile(path.join(vault, "existing.md"), "local");
      const config = { vaultPath: vault, statePath: state, r2: { prefix: "" }, encryption: "plain", files: [{ path: "existing.md", key: "existing.md" }] };
      const api = { getObject: async () => ({ bytes: Buffer.from("remote"), etag: "remote", metadata: {} }) };
      await assert.rejects(() => executeProbe(config, api, cipher, createIgnoreMatcher([], { protectedRelPaths: ["state.json"] }), true), /without checkpoint/);
      await assert.rejects(() => executeProbe({ ...config, files: [{ path: "state.json", key: "state.json" }] }, api, cipher, createIgnoreMatcher([], { protectedRelPaths: ["state.json"] }), true), /protected/);
    } finally { await fs.rm(root, { recursive: true, force: true }); }
  }

  {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), "r2-sync-migration-"));
    try {
      const vault = path.join(root, "vault");
      await fs.mkdir(vault);
      await fs.writeFile(path.join(root, "config.json"), JSON.stringify({ vaultPath: vault, statePath: "new-state.json", endpoint: "http://127.0.0.1:1", bucket: "bucket", accessKeyId: "key", secretAccessKey: "secret", password: "password", mode: "full" }));
      await fs.writeFile(path.join(root, ".sync-state.json"), "{}\n");
      const entry = spawnSync(process.execPath, [path.resolve("src/index.js"), "--config", path.join(root, "config.json")], { cwd: root, encoding: "utf8", timeout: 10000 });
      assert.match(entry.stdout, /legacy \.sync-state\.json detected/);
      assert.equal(entry.status, 1);
    } finally { await fs.rm(root, { recursive: true, force: true }); }
  }

  {
    const server = createServer((request, response) => {
      if (request.method === "HEAD") {
        response.writeHead(200);
        response.end();
        return;
      }
      response.writeHead(200, { "content-type": "application/xml" });
      response.end("<ListBucketResult xmlns=\"http://s3.amazonaws.com/doc/2006-03-01/\"><IsTruncated>false</IsTruncated></ListBucketResult>");
    });
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    const root = await fs.mkdtemp(path.join(os.tmpdir(), "r2-sync-cli-smoke-"));
    try {
      const vault = path.join(root, "vault");
      await fs.mkdir(vault);
      const configPath = path.join(root, "config.json");
      await fs.writeFile(configPath, JSON.stringify({
        vaultPath: vault,
        statePath: "state.json",
        endpoint: `http://127.0.0.1:${server.address().port}`,
        bucket: "bucket",
        accessKeyId: "key",
        secretAccessKey: "secret",
        password: "",
        encryption: "plain",
        mode: "full",
        remotePrefix: "",
      }));
      const result = await runCli([path.resolve("src/index.js"), "--config", configPath], { cwd: root });
      assert.equal(result.status, 0, result.stderr + result.stdout);
      assert.match(result.stdout, /"ok": true/);
      assert.match(result.stderr, /DRY-RUN/);
    } finally {
      server.close();
      await fs.rm(root, { recursive: true, force: true });
    }
  }
  console.log("full sync safety tests passed");
}

await run();
