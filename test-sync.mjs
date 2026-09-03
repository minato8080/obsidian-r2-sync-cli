import fs from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import { createCipher } from "./src/crypto.js";
import { createIgnoreMatcher } from "./src/ignore.js";
import { listLocalFiles } from "./src/localFiles.js";
import { planSync, applyActions } from "./src/sync.js";

const vaultPath = await fs.mkdtemp(path.join(os.tmpdir(), "r2sync-test-"));
console.log("test vault:", vaultPath);

const cipher = await createCipher("test-password");
const ignoreMatcher = createIgnoreMatcher([]);
const r2Prefix = "";

// --- 偽のR2(インメモリ) ---
const remoteStore = new Map(); // key -> { bytes, metadata, etag }
let etagCounter = 0;
const r2 = {
  async listAll() {
    return [...remoteStore.entries()].map(([key, v]) => ({ key, etag: v.etag, size: v.bytes.length }));
  },
  async getObject(key) {
    const v = remoteStore.get(key);
    return { bytes: v.bytes, metadata: v.metadata };
  },
  async headObject(key) {
    const v = remoteStore.get(key);
    return { metadata: v.metadata };
  },
  async putObject(key, bytes, metadata) {
    const etag = `"etag-${etagCounter++}"`;
    remoteStore.set(key, { bytes, metadata, etag });
    return { etag };
  },
  async deleteObject(key) {
    remoteStore.delete(key);
  },
};

async function writeLocal(rel, content, mtimeMsOverride) {
  const abs = path.join(vaultPath, rel);
  await fs.mkdir(path.dirname(abs), { recursive: true });
  await fs.writeFile(abs, content);
  if (mtimeMsOverride) {
    const d = new Date(mtimeMsOverride);
    await fs.utimes(abs, d, d);
  }
}

async function runSync({ apply = true, allowDelete = false } = {}) {
  const localFiles = await listLocalFiles(vaultPath, ignoreMatcher);
  const remoteItems = await r2.listAll();
  const prevEntries = await loadPrevState();
  const actions = await planSync({ vaultPath, localFiles, remoteItems, prevEntries, ignoreMatcher, cipher, r2Prefix, r2 });
  const result = await applyActions({ actions, vaultPath, localFiles, prevEntries, cipher, r2, r2Prefix, apply, allowDelete });
  if (apply && actions.some(({ type }) => type !== "NOOP")) await savePrevState(result.newEntries);
  return { actions, ...result };
}

let stateHolder = {};
let stateSaveCount = 0;
async function loadPrevState() { return stateHolder; }
async function savePrevState(entries) { stateHolder = entries; stateSaveCount++; }

function summarizeActions(actions) {
  const m = {};
  for (const a of actions) m[a.type] = (m[a.type] ?? 0) + 1;
  return m;
}

let pass = 0, fail = 0;
function check(label, cond) {
  if (cond) { pass++; console.log(`OK   ${label}`); }
  else { fail++; console.log(`FAIL ${label}`); }
}

// 1. 初回: ローカルに新規ファイル -> PUSH
await writeLocal("note1.md", "hello world");
await writeLocal("dir/note2.md", "nested note");
let r1 = await runSync();
check("run1: PUSHが2件", summarizeActions(r1.actions).PUSH === 2);
check("run1: remoteに2件アップロードされた", remoteStore.size === 2);

// 2. 変化なし -> NOOP
const stateSaveCountBeforeNoop = stateSaveCount;
const noopLogs = [];
const originalConsoleLog = console.log;
console.log = (...parts) => noopLogs.push(parts.join(" "));
let r2run;
try {
  r2run = await runSync();
} finally {
  console.log = originalConsoleLog;
}
check("run2: 全てNOOP", summarizeActions(r2run.actions).NOOP === 2);
check("run2: remoteは変化なし(件数)", remoteStore.size === 2);
check("run2: NOOPは適用進捗に含まれない", !noopLogs.some((line) => line.includes("適用")));
check("run2: NOOPだけならstateを書き直さない", stateSaveCount === stateSaveCountBeforeNoop);

// 3. リモート側でファイルが増える(別クライアントが追加した想定) -> PULL
const encKey = await cipher.encryptPath("remote-only.md");
const encContent = await cipher.encryptContent(new TextEncoder().encode("from another device"));
remoteStore.set(encKey, { bytes: encContent, metadata: { mtime: String(Date.now() / 1000) }, etag: `"etag-manual-1"` });
let r3 = await runSync();
check("run3: PULLが1件", summarizeActions(r3.actions).PULL === 1);
const pulledContent = await fs.readFile(path.join(vaultPath, "remote-only.md"), "utf-8");
check("run3: pull内容が正しい", pulledContent === "from another device");

// 4. ローカル編集 -> PUSH
await new Promise((r) => setTimeout(r, 20));
await writeLocal("note1.md", "hello world EDITED");
let r4 = await runSync();
check("run4: PUSHが1件(note1.md)", r4.actions.some((a) => a.type === "PUSH" && a.relPath === "note1.md"));

// 5. ローカル削除、リモート変化なし、allowDelete=false -> SKIPPED
await fs.rm(path.join(vaultPath, "dir/note2.md"));
let r5 = await runSync({ allowDelete: false });
check("run5: DELETE_REMOTEが計画される", r5.actions.some((a) => a.type === "DELETE_REMOTE" && a.relPath === "dir/note2.md"));
check("run5: allowDelete falseなのでremoteにまだ残っている", [...remoteStore.keys()].length > 0 && await remoteStillHas("dir/note2.md"));

// 6. allowDelete=true で実際に削除
let r6 = await runSync({ allowDelete: true });
check("run6: DELETE_REMOTEが実行された", !(await remoteStillHas("dir/note2.md")));

// 7. 両側変更: リモートの方が新しい mtime -> 上書き(PULL)、バックアップファイルは作らない
await new Promise((r) => setTimeout(r, 20));
await writeLocal("note1.md", "local edit version");
const currentKey = await cipher.encryptPath("note1.md");
const conflictRemoteContent = await cipher.encryptContent(new TextEncoder().encode("remote edit version"));
const futureMtimeSec = Date.now() / 1000 + 3600; // 確実にローカルより新しい
remoteStore.set(currentKey, { bytes: conflictRemoteContent, metadata: { mtime: String(futureMtimeSec) }, etag: `"etag-conflict-1"` });
let r7 = await runSync();
check("run7: 両側変更はPUSH/PULLのどちらかに解決される(CONFLICT型が残らない)", !r7.actions.some((a) => a.type.startsWith("CONFLICT")));
check("run7: リモートの方が新しいのでPULL", r7.actions.some((a) => a.type === "PULL" && a.relPath === "note1.md"));
const note1After = await fs.readFile(path.join(vaultPath, "note1.md"), "utf-8");
check("run7: note1.mdはリモート内容で上書きされている", note1After === "remote edit version");
const dirEntries = await fs.readdir(vaultPath);
check("run7: コンフリクトコピーは作られない", !dirEntries.some((f) => f.includes("conflict")));

// 8. 両側変更: 今度はローカルの方が新しい -> 上書き(PUSH)
await new Promise((r) => setTimeout(r, 20));
await writeLocal("note1.md", "local edit version 2");
const remoteOldContent = await cipher.encryptContent(new TextEncoder().encode("remote edit version 2"));
remoteStore.set(currentKey, { bytes: remoteOldContent, metadata: { mtime: "1" }, etag: `"etag-conflict-2"` });
let r8 = await runSync();
check("run8: ローカルの方が新しいのでPUSH", r8.actions.some((a) => a.type === "PUSH" && a.relPath === "note1.md"));
const remoteAfter = await cipher.decryptContent((await r2.getObject(currentKey)).bytes);
check("run8: リモートがローカル内容で上書きされている", Buffer.from(remoteAfter).toString("utf-8") === "local edit version 2");

async function remoteStillHas(relPath) {
  const key = await cipher.encryptPath(relPath);
  return remoteStore.has(key);
}

// 9. 別のvault/バケットで「状態ファイルなしの初回実行」を再現し、bootstrap比較(SEED/CONFLICT)を検証
const bootVault = await fs.mkdtemp(path.join(os.tmpdir(), "r2sync-boot-"));
const bootRemote = new Map();
let bootEtagCounter = 0;
const bootR2 = {
  async listAll() { return [...bootRemote.entries()].map(([key, v]) => ({ key, etag: v.etag })); },
  async getObject(key) { const v = bootRemote.get(key); return { bytes: v.bytes, metadata: v.metadata }; },
};

async function seedBoot(rel, localContent, remoteContent, remoteMtimeSec) {
  const abs = path.join(bootVault, rel);
  await fs.mkdir(path.dirname(abs), { recursive: true });
  await fs.writeFile(abs, localContent);
  const key = await cipher.encryptPath(rel);
  const enc = await cipher.encryptContent(new TextEncoder().encode(remoteContent));
  bootRemote.set(key, { bytes: enc, metadata: { mtime: String(remoteMtimeSec) }, etag: `"boot-etag-${bootEtagCounter++}"` });
}

await seedBoot("same.md", "identical content", "identical content", 1);
// リモートの方が新しいmtime -> PULLで上書きされるはず
await seedBoot("diff.md", "local version", "remote version", Date.now() / 1000 + 3600);

const bootLocalFiles = await listLocalFiles(bootVault, ignoreMatcher);
const bootRemoteItems = await bootR2.listAll();
const bootActions = await planSync({
  vaultPath: bootVault, localFiles: bootLocalFiles, remoteItems: bootRemoteItems,
  prevEntries: {}, ignoreMatcher, cipher, r2Prefix, r2: bootR2,
});
check("boot: same.mdはSEED", bootActions.some((a) => a.type === "SEED" && a.relPath === "same.md"));
check("boot: diff.mdはリモートの方が新しいのでPULL", bootActions.some((a) => a.type === "PULL" && a.relPath === "diff.md"));
await fs.rm(bootVault, { recursive: true, force: true });

// 10. 本家プラグイン等の別クライアントが「中身は同じだがtouchしただけ」を再現し、
//     誤検知でPUSH/PULLしないことを検証する。

// 10a. リモート側だけtouch(別クライアントが同一内容を再アップロード=ETagだけ変わる)
//      -> PULLではなくSEED(転送スキップ)になるはず
const untouchedRel = "note1.md";
const untouchedKey = await cipher.encryptPath(untouchedRel);
const sameContentReencrypted = await cipher.encryptContent(new TextEncoder().encode("local edit version 2"));
remoteStore.set(untouchedKey, { bytes: sameContentReencrypted, metadata: { mtime: String(Date.now() / 1000) }, etag: `"etag-retouch-1"` });
let r9 = await runSync();
check("run9: リモートtouchのみ(内容同一)はSEED", r9.actions.some((a) => a.type === "SEED" && a.relPath === untouchedRel));
check("run9: PULLは発生しない", !r9.actions.some((a) => a.type === "PULL" && a.relPath === untouchedRel));

// 10b. ローカル側だけtouch(内容は同じだがmtimeだけ更新=本家プラグインがPULLした想定)
//      -> PUSHではなくSEED(転送スキップ)になるはず
await new Promise((r) => setTimeout(r, 20));
const note1Path = path.join(vaultPath, untouchedRel);
const etagBeforeTouch = remoteStore.get(untouchedKey).etag;
await fs.utimes(note1Path, new Date(), new Date()); // 内容は変えずmtimeだけ更新
let r10 = await runSync();
check("run10: ローカルtouchのみ(内容同一)はSEED", r10.actions.some((a) => a.type === "SEED" && a.relPath === untouchedRel));
check("run10: PUSHは発生しない", !r10.actions.some((a) => a.type === "PUSH" && a.relPath === untouchedRel));
check("run10: リモートは再アップロードされていない(etag不変)", remoteStore.get(untouchedKey).etag === etagBeforeTouch);

console.log(`\n${pass} passed, ${fail} failed`);
await fs.rm(vaultPath, { recursive: true, force: true });
process.exitCode = fail > 0 ? 1 : 0;
