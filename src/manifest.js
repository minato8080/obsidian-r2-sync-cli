import crypto from "node:crypto";

export const MANIFEST_VERSION = 1;
export const MANIFEST_ALGORITHM = "HMAC-SHA256";

export function contentFingerprint(bytes, manifestKey) {
  if (typeof manifestKey !== "string" || manifestKey.length === 0) throw new Error("manifestKey must be a non-empty string");
  return crypto.createHmac("sha256", Buffer.from(manifestKey, "utf8")).update(Buffer.from(bytes)).digest("hex");
}

function validHex(value) { return typeof value === "string" && /^[0-9a-f]{64}$/.test(value); }

export function parseManifest(bytes, { listedKeys, prefix, reservedKey } = {}) {
  let value;
  try { value = JSON.parse(Buffer.from(bytes).toString("utf8")); } catch { return null; }
  if (!value || typeof value !== "object" || Array.isArray(value) || value.version !== MANIFEST_VERSION || value.algorithm !== MANIFEST_ALGORITHM || value.prefix !== (prefix ?? "") || !value.entries || typeof value.entries !== "object" || Array.isArray(value.entries)) return null;
  const entries = value.entries;
  const expected = new Set((listedKeys ?? []).filter((key) => key !== reservedKey));
  const actual = new Set(Object.keys(entries));
  if (expected.size !== actual.size || [...expected].some((key) => !actual.has(key))) return null;
  for (const key of expected) {
    const entry = entries[key];
    if (!entry || typeof entry !== "object" || Array.isArray(entry) || typeof entry.etag !== "string" || !Number.isInteger(entry.size) || entry.size < 0 || !validHex(entry.fingerprint)) return null;
  }
  return { ...value, entries };
}

export function buildManifest(entries, prefix = "") {
  return JSON.stringify({ version: MANIFEST_VERSION, algorithm: MANIFEST_ALGORITHM, prefix, entries }, null, 2) + "\n";
}
