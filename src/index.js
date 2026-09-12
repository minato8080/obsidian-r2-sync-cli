import fs from "node:fs/promises";
import path from "node:path";
import { loadConfig } from "./config.js";
import { createCipher } from "./crypto.js";
import { createR2Client } from "./s3.js";
import { createIgnoreMatcher } from "./ignore.js";
import { classifyLocalPaths, listLocalFiles, summarizeIncludedPaths, vaultPath } from "./localFiles.js";
import { loadState, saveState } from "./state.js";
import { executeFullSync } from "./sync.js";
import { executeProbe } from "./probe.js";

function argValue(args, name, fallback) { const index = args.indexOf(name); return index >= 0 ? args[index + 1] : fallback; }
function formatCount(count, singular, plural = `${singular}s`) { return `${count} ${count === 1 ? singular : plural}`; }
const REMOTE_OBJECT_DETAIL_LIMIT = 20;
function resultForJson(result) {
  if (!Array.isArray(result.ignoredRemoteObjects)) return result;
  const details = result.ignoredRemoteObjects.filter((item) => !item || typeof item !== "object" || item.reason !== "ignored").slice(0, REMOTE_OBJECT_DETAIL_LIMIT);
  return { ...result, ignoredRemoteObjects: details, ignoredRemoteObjectsTotal: result.ignoredRemoteObjects.length, ignoredRemoteObjectsOmitted: result.ignoredRemoteObjects.length - details.length };
}
function report(result) {
  console.error("\n=== 実行結果 ===");
  for (const key of ["ok", "planned", "unchanged", "validated", "applied", "reconciledLocalFiles", "unicodeAliasesIgnored"]) if (key in result) console.error(`${key}: ${result[key]}`);
  for (const [type, count] of Object.entries(result.appliedByType ?? {})) console.error(`${type}: ${count}`);
  for (const [type, count] of Object.entries(result.skippedByType ?? {})) console.error(`SKIPPED_${type}: ${count}`);
  if (result.errors?.length) { console.error(`\nエラー: ${result.errors.length}件`); for (const error of result.errors.slice(0, 20)) console.error(`  ${error.relPath ?? error.path}: ${error.message ?? error.error}`); }
  if (result.conflicts?.length) { console.error(`\n競合: ${result.conflicts.length}件`); for (const conflict of result.conflicts.slice(0, 20)) console.error(`  ${conflict.path}: ${conflict.reason}`); }
}

async function main() {
  const args = process.argv.slice(2), apply = args.includes("--apply"), allowDelete = args.includes("--allow-delete");
  if (args.includes("--help")) { console.log("Usage: node src/index.js [--config config.json] [--apply] [--allow-delete]"); return; }
  const config = await loadConfig(argValue(args, "--config", path.join(process.cwd(), "config.json")));
  if (config.legacyStatePath && config.statePath !== config.legacyStatePath) {
    try {
      await fs.access(config.statePath);
    } catch (error) {
      if (error.code === "ENOENT") {
        try {
          await fs.access(config.legacyStatePath);
          throw new Error("legacy .sync-state.json detected; set statePath to it explicitly or migrate it manually before syncing");
        } catch (legacyError) {
          if (legacyError.message.includes("legacy .sync-state.json")) throw legacyError;
          if (legacyError.code !== "ENOENT") throw legacyError;
        }
      } else throw error;
    }
  }
  const ignoreMatcher = createIgnoreMatcher(config.ignoreExtra, { vaultPath: config.vaultPath, protectedRelPaths: config.protectedRelPaths });
  if (args.includes("--check-ignore")) {
    const verbose = args.includes("--verbose");
    console.log(`=== 除外判定（ローカルのみ / R2通信なし）===\nvault: ${config.vaultPath}\nignoreExtra: ${formatCount(config.ignoreExtra.length, "pattern")}\nunicodeCollisionPolicy: ${config.unicodeCollisionPolicy}`);
    const marker = args.indexOf("--check-ignore");
    const paths = args.slice(marker + 1).filter((value) => !value.startsWith("--"));
    if (paths.length) {
      let allIgnored = true;
      for (const raw of paths) { const relPath = raw.replace(/[\\/]$/, "").replace(/\\/g, "/").normalize("NFC"); const target = vaultPath(config.vaultPath, relPath); let isDir = raw.endsWith("/") || raw.endsWith("\\"); try { isDir ||= (await fs.stat(target)).isDirectory(); } catch {} const ignored = isDir ? ignoreMatcher.isIgnoredDir(relPath) : ignoreMatcher.isIgnoredFile(relPath); console.log(`[${ignored ? "IGNORE" : "INCLUDE"}] ${relPath}${isDir ? "/" : ""}`); allIgnored &&= ignored; }
      process.exitCode = allIgnored ? 0 : 1; return;
    }
    const classified = await classifyLocalPaths(config.vaultPath, ignoreMatcher, verbose);
    console.log(`\n[IGNORE] ${formatCount(classified.ignored.length, "entry", "entries")}`);
    for (const item of classified.ignored) console.log(`  ${item}${item.endsWith("/") && !verbose ? " (all files)" : ""}`);
    console.log(`\n[INCLUDE] ${formatCount(classified.included.length, "file")}`);
    if (verbose) for (const item of classified.included) console.log(`  ${item}`);
    else for (const [item, count] of summarizeIncludedPaths(classified.included, classified.ignored)) console.log(`  ${item}${count === null ? "" : ` (${formatCount(count, "file")})`}`);
    return;
  }
  const cipher = await createCipher(config.password, config.encryption), r2 = createR2Client(config.r2);
  console.error(`vault: ${config.vaultPath}`);
  console.error(`mode: ${apply ? (allowDelete ? "APPLY (削除含む)" : "APPLY (削除は警告のみ)") : "DRY-RUN"}`);
  if (config.mode === "probe") {
    const result = await executeProbe(config, r2, cipher, ignoreMatcher, apply); report(result); console.log(JSON.stringify(resultForJson(result), null, 2)); process.exitCode = result.ok ? 0 : 1; return;
  }
  await r2.checkConnection();
  const result = await executeFullSync({
    vaultPath: config.vaultPath, statePath: config.statePath, listRemote: () => r2.listAll(), getObject: (key) => r2.getObject(key), putObject: (key, bytes, metadata) => r2.putObject(key, bytes, metadata), deleteObject: (key) => r2.deleteObject(key), cipher, r2Prefix: config.r2.prefix, extraPatterns: config.ignoreExtra, protectedRelPaths: config.protectedRelPaths, unicodeCollisionPolicy: config.unicodeCollisionPolicy, textMergeBaseMaxBytes: config.textMergeBaseMaxBytes, recheckRemoteBeforeApply: config.recheckRemoteBeforeApply, apply, allowDelete, listLocalFiles, loadState, saveState, ignoreMatcher, progress: (message) => console.error(message),
  });
  if (!apply && result.ok) console.error("\ndry-runのため実際の変更は行っていません。--apply を付けて実行してください。");
  report(result); console.log(JSON.stringify(resultForJson(result), null, 2)); process.exitCode = result.ok ? 0 : 1;
}

main().catch((error) => { const result = { ok: false, mode: process.argv.includes("--apply") ? "apply" : "dry-run", planned: 0, validated: 0, applied: 0, errors: [{ error: error.message }], conflicts: [], timingsMs: {} }; report(result); console.log(JSON.stringify(result, null, 2)); process.exitCode = 1; });
