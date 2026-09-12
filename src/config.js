import fs from "node:fs/promises";
import path from "node:path";

export function normalizePrefix(value = "") {
  const prefix = String(value ?? "").replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
  return prefix ? `${prefix}/` : "";
}

function required(config, key) {
  if (config[key] === undefined || config[key] === null || config[key] === "") {
    throw new Error(`config is missing: ${key}`);
  }
  return config[key];
}

function validate(config) {
  if (!config || typeof config !== "object" || Array.isArray(config)) throw new Error("config root must be an object");
  for (const key of ["vaultPath", "statePath", "endpoint", "bucket", "accessKeyId", "secretAccessKey", "mode"]) {
    if (typeof required(config, key) !== "string") throw new Error(`${key} must be a string`);
  }
  if (!["probe", "full"].includes(config.mode)) throw new Error("mode must be probe or full");
  if (config.mode === "probe" && !Array.isArray(config.files)) throw new Error("files must be an array of objects in probe mode");
  if (config.ignoreExtra !== undefined && (!Array.isArray(config.ignoreExtra) || !config.ignoreExtra.every((v) => typeof v === "string"))) throw new Error("ignoreExtra must be an array of strings");
  if (config.encryption !== undefined && !["rclone-base64", "plain"].includes(config.encryption)) throw new Error("encryption must be rclone-base64 or plain");
  if (config.textMergeBaseMaxBytes !== undefined && (!Number.isInteger(config.textMergeBaseMaxBytes) || config.textMergeBaseMaxBytes < 0)) throw new Error("textMergeBaseMaxBytes must be a non-negative integer");
  if (config.recheckRemoteBeforeApply !== undefined && typeof config.recheckRemoteBeforeApply !== "boolean") throw new Error("recheckRemoteBeforeApply must be true or false");
  if (config.unicodeCollisionPolicy !== undefined && !["error", "prefer-nfc"].includes(config.unicodeCollisionPolicy)) throw new Error("unicodeCollisionPolicy must be error or prefer-nfc");
  if (config.fetchConcurrency !== undefined && (!Number.isInteger(config.fetchConcurrency) || config.fetchConcurrency < 1 || config.fetchConcurrency > 16)) throw new Error("fetchConcurrency must be an integer from 1 to 16");
  if (config.applyConcurrency !== undefined && (!Number.isInteger(config.applyConcurrency) || config.applyConcurrency < 1 || config.applyConcurrency > 16)) throw new Error("applyConcurrency must be an integer from 1 to 16");
  if (config.requestTimeoutSeconds !== undefined && (typeof config.requestTimeoutSeconds !== "number" || !Number.isFinite(config.requestTimeoutSeconds) || config.requestTimeoutSeconds < 1 || config.requestTimeoutSeconds > 300)) throw new Error("requestTimeoutSeconds must be a number from 1 to 300");
  return config;
}

export async function loadConfig(configPath = path.join(process.cwd(), "config.json")) {
  const resolved = path.resolve(configPath);
  let parsed;
  try { parsed = JSON.parse(await fs.readFile(resolved, "utf8")); }
  catch (error) { throw new Error(`cannot read config: ${resolved}`, { cause: error }); }
  validate(parsed);
  const vaultPath = path.resolve(parsed.vaultPath);
  const statePath = path.resolve(path.dirname(resolved), parsed.statePath);
  const legacyStatePath = path.resolve(process.cwd(), ".sync-state.json");
  const rel = (target) => {
    const relative = path.relative(vaultPath, target);
    return relative && relative !== "." && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative) ? relative.replace(/\\/g, "/") : null;
  };
  return {
    ...parsed,
    configPath: resolved,
    vaultPath,
    statePath,
    legacyStatePath,
    r2: { endpoint: parsed.endpoint, bucket: parsed.bucket, accessKeyId: parsed.accessKeyId, secretAccessKey: parsed.secretAccessKey, prefix: normalizePrefix(parsed.remotePrefix) },
    password: parsed.password ?? "",
    encryption: parsed.encryption ?? "rclone-base64",
    ignoreExtra: parsed.ignoreExtra ?? [],
    unicodeCollisionPolicy: parsed.unicodeCollisionPolicy ?? "error",
    recheckRemoteBeforeApply: parsed.recheckRemoteBeforeApply ?? true,
    protectedRelPaths: [rel(resolved), rel(statePath)].filter(Boolean),
  };
}
