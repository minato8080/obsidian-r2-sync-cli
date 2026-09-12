import fs from "node:fs/promises";
import crypto from "node:crypto";

import { assertVaultPath } from "./localFiles.js";
import { remoteMtime } from "./sync.js";
import { loadState, saveState } from "./state.js";

const hash = (bytes) => crypto.createHash("sha256").update(bytes).digest("hex");

async function localConflict(target, previous) {
  try {
    const stat = await fs.stat(target);
    if (!previous) return "target exists without checkpoint state";
    if (previous.localSize !== undefined && stat.size !== previous.localSize) return "target changed since checkpoint";
    if (previous.localMtimeMs !== undefined && Math.abs(stat.mtimeMs - Number(previous.localMtimeMs)) > 1) return "target changed since checkpoint";
    if (previous.localContentHash && hash(await fs.readFile(target)) !== previous.localContentHash) return "target content changed since checkpoint";
    return null;
  } catch (error) {
    if (error.code === "ENOENT") return null;
    throw error;
  }
}

export async function executeProbe(config, r2, cipher, ignoreMatcher, apply) {
  const files = config.files;
  if (files.length < 1 || files.length > 2) throw new Error("Probe accepts one or two files");
  await fs.mkdir(config.vaultPath, { recursive: true });
  const previous = await loadState(config.statePath, config.unicodeCollisionPolicy);
  const prepared = [];
  const seen = new Set();
  for (const spec of files) {
    if (!spec || typeof spec.path !== "string") throw new Error("each files entry needs a path");
    const relPath = spec.path.replace(/\\/g, "/").normalize("NFC");
    if (!relPath || seen.has(relPath) || ignoreMatcher.isIgnoredFile(relPath)) throw new Error(`protected or duplicate Probe path: ${relPath}`);
    seen.add(relPath);
    const target = await assertVaultPath(config.vaultPath, relPath);
    const conflict = await localConflict(target, previous[relPath]);
    if (conflict) throw new Error(`Probe refused for ${relPath}: ${conflict}`);
    const key = spec.key ?? config.r2.prefix + (config.encryption === "plain" ? relPath : await cipher.encryptPath(relPath));
    const remote = await r2.getObject(key), data = Buffer.from(await cipher.decryptContent(remote.bytes));
    new TextDecoder("utf-8", { fatal: true }).decode(data);
    prepared.push({ relPath, target, data, etag: remote.etag, mtimeMs: remoteMtime(remote.metadata) });
  }
  const result = { ok: true, mode: apply ? "apply" : "dry-run", planned: prepared.length, validated: prepared.length, applied: 0, errors: [], conflicts: [], timingsMs: {} };
  if (apply) for (const item of prepared) {
    const target = await assertVaultPath(config.vaultPath, item.relPath);
    const current = await localConflict(target, previous[item.relPath]);
    if (current) throw new Error(`Probe refused for ${item.relPath}: ${current}`);
    const { atomicReplace } = await import("./sync.js");
    await atomicReplace(target, item.data, item.mtimeMs);
    const stat = await fs.stat(target);
    previous[item.relPath] = { localMtimeMs: stat.mtimeMs, localSize: stat.size, remoteETag: item.etag, localContentHash: hash(item.data), baseContentBase64: item.data.toString("base64") };
    await saveState(config.statePath, previous);
    result.applied += 1;
  }
  return result;
}
