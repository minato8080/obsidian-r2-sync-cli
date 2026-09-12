import fs from "node:fs/promises";
import path from "node:path";
import crypto from "node:crypto";

export async function loadState(stateFilePath, policy = "error", aliasStats = { ignored: 0 }) {
  let parsed;
  try { parsed = JSON.parse(await fs.readFile(stateFilePath, "utf8")); }
  catch (error) { if (error.code === "ENOENT") return {}; throw error; }
  if (!parsed || typeof parsed !== "object" || !parsed.entries || typeof parsed.entries !== "object") throw new Error("state file has an invalid format");
  const grouped = new Map();
  for (const [rawPath, entry] of Object.entries(parsed.entries)) {
    const normalized = rawPath.replace(/\\/g, "/").normalize("NFC");
    if (!normalized || normalized.startsWith("/") || normalized.split("/").some((part) => !part || part === "." || part === "..")) throw new Error(`unsafe relative path in state: ${rawPath}`);
    grouped.set(normalized, [...(grouped.get(normalized) ?? []), [rawPath, entry]]);
  }
  const result = {};
  for (const [normalized, candidates] of grouped) {
    let selected = candidates[0];
    if (candidates.length > 1) {
      const exact = candidates.filter(([raw]) => raw === normalized);
      if (policy !== "prefer-nfc" || exact.length !== 1) throw new Error(`multiple state paths normalize to the same path: ${normalized}`);
      selected = exact[0];
      aliasStats.ignored += candidates.length - 1;
    }
    result[normalized] = selected[1];
  }
  return result;
}

export async function saveState(stateFilePath, entries) {
  await fs.mkdir(path.dirname(stateFilePath), { recursive: true });
  const tempPath = path.join(path.dirname(stateFilePath), `.${path.basename(stateFilePath)}.${process.pid}.${Date.now()}.tmp`);
  const data = JSON.stringify({ version: 1, updatedAt: new Date().toISOString(), entries }, null, 2) + "\n";
  try {
    const handle = await fs.open(tempPath, "w");
    try { await handle.writeFile(data, "utf8"); await handle.sync(); } finally { await handle.close(); }
    await fs.rename(tempPath, stateFilePath);
  } finally { await fs.rm(tempPath, { force: true }); }
}

export function entryFromStat(stat, etag, bytes, maxBytes) {
  const entry = { localMtimeMs: stat.mtimeMs, localSize: stat.size, remoteETag: etag, localContentHash: bytes ? sha256Hex(bytes) : null };
  if (bytes && (maxBytes === undefined || (bytes.length <= maxBytes && isUtf8(bytes)))) entry.baseContentBase64 = Buffer.from(bytes).toString("base64");
  return entry;
}

export function baseBytes(entry) {
  if (!entry || typeof entry.baseContentBase64 !== "string") return null;
  try { return Buffer.from(entry.baseContentBase64, "base64"); } catch { return null; }
}

export function sha256Hex(bytes) { return crypto.createHash("sha256").update(bytes).digest("hex"); }
function isUtf8(bytes) { try { new TextDecoder("utf-8", { fatal: true }).decode(bytes); return true; } catch { return false; } }
