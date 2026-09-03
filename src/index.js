import path from "node:path";
import { config } from "./config.js";
import { createCipher } from "./crypto.js";
import { createR2Client } from "./s3.js";
import { createIgnoreMatcher } from "./ignore.js";
import { listLocalFiles } from "./localFiles.js";
import { loadState, saveState } from "./state.js";
import { planSync, applyActions } from "./sync.js";

const args = process.argv.slice(2);
const apply = args.includes("--apply");
const allowDelete = args.includes("--allow-delete");

function groupBy(arr, fn) {
  const m = {};
  for (const item of arr) {
    const k = fn(item);
    (m[k] ??= []).push(item);
  }
  return m;
}

async function main() {
  // .envとstateのある実行ディレクトリを、ignoreパターンの基準にする。
  // 通常はVault内のr2-sync配置ディレクトリと同じになる。
  const vaultRoot = path.resolve(config.vaultPath);
  const asVaultRelative = (absolutePath) => {
    const relative = path.relative(vaultRoot, path.resolve(absolutePath));
    return relative && relative !== "." && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative)
      ? relative.replace(/\\/g, "/")
      : null;
  };
  const protectedRelPaths = [
    asVaultRelative(config.stateFilePath),
    asVaultRelative(path.join(process.cwd(), ".env")),
  ].filter(Boolean);
  const ignoreMatcher = createIgnoreMatcher(config.ignoreExtra, {
    basePath: process.cwd(),
    vaultPath: config.vaultPath,
    protectedRelPaths,
  });
  const cipher = await createCipher(config.syncPassword);
  const r2 = createR2Client(config.r2);

  console.log(`vault: ${config.vaultPath}`);
  console.log(`mode: ${apply ? (allowDelete ? "APPLY (削除含む)" : "APPLY (削除は警告のみ)") : "DRY-RUN"}`);

  await r2.checkConnection();

  const [localFiles, remoteItems, prevEntries] = await Promise.all([
    listLocalFiles(config.vaultPath, ignoreMatcher),
    r2.listAll(),
    loadState(config.stateFilePath),
  ]);

  console.log(`local files: ${localFiles.size}件 / remote objects: ${remoteItems.length}件 / 前回状態: ${Object.keys(prevEntries).length}件`);

  const actions = await planSync({
    vaultPath: config.vaultPath,
    localFiles,
    remoteItems,
    prevEntries,
    ignoreMatcher,
    cipher,
    r2Prefix: config.r2.prefix,
    r2,
  });

  const grouped = groupBy(actions, (a) => a.type);
  for (const [type, list] of Object.entries(grouped)) {
    console.log(`\n[${type}] ${list.length}件`);
    for (const a of list.slice(0, 50)) console.log(`  ${a.relPath}${a.reason ? ` (${a.reason})` : ""}`);
    if (list.length > 50) console.log(`  ...ほか${list.length - 50}件`);
  }

  if (!apply) {
    console.log("\ndry-runのため実際の変更は行っていません。--apply を付けて実行してください。");
    return;
  }

  const { newEntries, summary, errors } = await applyActions({
    actions,
    vaultPath: config.vaultPath,
    localFiles,
    prevEntries,
    cipher,
    r2,
    r2Prefix: config.r2.prefix,
    apply,
    allowDelete,
    onCheckpoint: async (newEntriesSoFar) => {
      await saveState(config.stateFilePath, { ...prevEntries, ...newEntriesSoFar });
    },
  });

  if (actions.some(({ type }) => type !== "NOOP")) {
    await saveState(config.stateFilePath, newEntries);
  }

  console.log("\n=== 実行結果 ===");
  for (const [k, v] of Object.entries(summary)) console.log(`${k}: ${v}`);
  if (errors.length > 0) {
    console.log(`\nエラー: ${errors.length}件`);
    for (const e of errors) console.log(`  ${e.relPath}: ${e.message}`);
  }
}

main().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
