import fs from "node:fs/promises";
import path from "node:path";
import crypto from "node:crypto";
import { runPool, DEFAULT_CONCURRENCY } from "./concurrency.js";

function sha256Hex(bytes) {
  return crypto.createHash("sha256").update(bytes).digest("hex");
}

function parseRemoteMtimeMs(metadata) {
  const raw = metadata.mtime ?? metadata.mMTime ?? metadata.MTime;
  if (!raw) return undefined;
  const num = Number(raw);
  if (Number.isNaN(num)) return undefined;
  // remotely-save旧版はミリ秒で保存していたための桁数判定(fsS3.tsと同じ考え方)
  const isMillis = Math.trunc(num).toString().length > 10;
  return isMillis ? num : num * 1000;
}

export async function planSync({ vaultPath, localFiles, remoteItems, prevEntries, ignoreMatcher, cipher, r2Prefix, r2 }) {
  const remoteByPath = new Map();
  for (const item of remoteItems) {
    const keyWithoutPrefix = r2Prefix && item.key.startsWith(r2Prefix) ? item.key.slice(r2Prefix.length) : item.key;
    if (!keyWithoutPrefix) continue;
    let relPath;
    try {
      relPath = await cipher.decryptPath(keyWithoutPrefix);
    } catch {
      continue; // 復号できないキー(レガシーmetadataファイル等)は無視
    }
    if (ignoreMatcher.isIgnoredFile(relPath)) continue;
    remoteByPath.set(relPath, item);
  }

  const allPaths = new Set([...localFiles.keys(), ...remoteByPath.keys(), ...Object.keys(prevEntries)]);
  const actions = [];
  const bootstrapCandidates = [];
  const conflictCandidates = [];
  const verifyCandidates = [];

  for (const relPath of allPaths) {
    const loc = localFiles.get(relPath);
    const rem = remoteByPath.get(relPath);
    const prev = prevEntries[relPath];

    if (!prev) {
      if (loc && rem) {
        // 状態ファイルが空の初回実行時、既にvaultとリモートの両方に存在するファイル。
        // 内容比較(ダウンロード+復号)が必要なので後段でまとめて並列処理する。
        bootstrapCandidates.push({ relPath, rem });
      } else if (loc) {
        actions.push({ type: "PUSH", relPath });
      } else if (rem) {
        actions.push({ type: "PULL", relPath, remoteKey: rem.key, remoteEtag: rem.etag });
      }
      continue;
    }

    const locGone = !loc && prev.localMtimeMs !== undefined;
    const remGone = !rem && prev.remoteETag !== undefined;
    const localChanged = !!loc && (loc.mtimeMs !== prev.localMtimeMs || loc.size !== prev.localSize);
    const remoteChanged = !!rem && rem.etag !== prev.remoteETag;

    if (locGone && remGone) {
      actions.push({ type: "FORGET", relPath });
    } else if (locGone && !remGone) {
      if (remoteChanged) actions.push({ type: "PULL", relPath, remoteKey: rem.key, remoteEtag: rem.etag, reason: "ローカル削除だがリモートは編集済み: 編集を優先" });
      else actions.push({ type: "DELETE_REMOTE", relPath, remoteKey: rem.key });
    } else if (!locGone && remGone) {
      if (localChanged) actions.push({ type: "PUSH", relPath, reason: "リモート削除だがローカルは編集済み: 編集を優先" });
      else actions.push({ type: "DELETE_LOCAL", relPath });
    } else if (localChanged && remoteChanged) {
      // 両側変更。ただしどちらもRemotely Save等の別クライアントが「中身は同じだが
      // touchしただけ(mtime更新/再アップロードでETagだけ変わった)」場合があるため、
      // まず内容が実際に一致するか確認してから勝敗(mtime比較)を決める。
      conflictCandidates.push({ relPath, loc, rem });
    } else if (localChanged) {
      // mtime/sizeは変わっているが、暗号化ランダムnonceのせいで別クライアントの
      // 再アップロードだけでもETagは変わりうる(ここではremote側は不変=remoteChanged false)。
      // ローカル側だけの変化なので、前回のcontent hashがあれば内容比較で誤検知を弾く。
      if (prev.localContentHash) {
        verifyCandidates.push({ relPath, loc, rem, prev, mode: "local" });
      } else {
        actions.push({ type: "PUSH", relPath });
      }
    } else if (remoteChanged) {
      // 別クライアント(本家プラグイン等)が中身を変えずに再アップロードしただけの
      // 可能性がある(暗号化が毎回ランダムnonceのためETagは必ず変わる)。
      // ダウンロード＆復号して実際に内容が一致するか確認してからPULLするか決める。
      verifyCandidates.push({ relPath, loc, rem, prev, mode: "remote" });
    } else {
      actions.push({ type: "NOOP", relPath });
    }
  }

  if (bootstrapCandidates.length > 0) {
    console.log(`初回比較: ローカル/リモート両方に存在する${bootstrapCandidates.length}件の内容を確認しています(並列${DEFAULT_CONCURRENCY}件)...`);
    let done = 0;
    await runPool(bootstrapCandidates, DEFAULT_CONCURRENCY, async ({ relPath, rem }) => {
      const localBytes = await fs.readFile(path.join(vaultPath, ...relPath.split("/")));
      const { bytes: remoteBytes, metadata } = await r2.getObject(rem.key);
      const decrypted = await cipher.decryptContent(remoteBytes);
      if (Buffer.from(decrypted).equals(Buffer.from(localBytes))) {
        actions.push({ type: "SEED", relPath, remoteEtag: rem.etag, localContentHash: sha256Hex(localBytes) });
      } else {
        const localMtimeMs = localFiles.get(relPath).mtimeMs;
        const remoteMtimeMs = parseRemoteMtimeMs(metadata) ?? 0;
        actions.push(
          localMtimeMs > remoteMtimeMs
            ? { type: "PUSH", relPath, reason: "初回比較: 内容不一致・ローカルの方が新しいため上書き" }
            : { type: "PULL", relPath, remoteKey: rem.key, remoteEtag: rem.etag, reason: "初回比較: 内容不一致・リモートの方が新しいため上書き" }
        );
      }
      done++;
      if (done % 50 === 0 || done === bootstrapCandidates.length) {
        console.log(`  ${done}/${bootstrapCandidates.length}`);
      }
    });
  }

  if (conflictCandidates.length > 0) {
    await runPool(conflictCandidates, DEFAULT_CONCURRENCY, async ({ relPath, loc, rem }) => {
      const localBytes = await fs.readFile(path.join(vaultPath, ...relPath.split("/")));
      const { bytes: remoteBytes, metadata } = await r2.getObject(rem.key);
      const decrypted = await cipher.decryptContent(remoteBytes);
      if (Buffer.from(decrypted).equals(Buffer.from(localBytes))) {
        actions.push({ type: "SEED", relPath, remoteEtag: rem.etag, localContentHash: sha256Hex(localBytes), reason: "両側でtouchされたが内容は一致・転送スキップ" });
        return;
      }
      const remoteMtimeMs = parseRemoteMtimeMs(metadata) ?? 0;
      actions.push(
        loc.mtimeMs > remoteMtimeMs
          ? { type: "PUSH", relPath, reason: "両側変更・ローカルの方が新しいため上書き" }
          : { type: "PULL", relPath, remoteKey: rem.key, remoteEtag: rem.etag, reason: "両側変更・リモートの方が新しいため上書き" }
      );
    });
  }

  if (verifyCandidates.length > 0) {
    await runPool(verifyCandidates, DEFAULT_CONCURRENCY, async ({ relPath, loc, rem, prev, mode }) => {
      const localBytes = await fs.readFile(path.join(vaultPath, ...relPath.split("/")));

      if (mode === "local") {
        const hash = sha256Hex(localBytes);
        if (hash === prev.localContentHash) {
          actions.push({ type: "SEED", relPath, remoteEtag: rem.etag, localContentHash: hash, reason: "mtime変化のみ・内容一致のため転送スキップ" });
        } else {
          actions.push({ type: "PUSH", relPath });
        }
        return;
      }

      // mode === "remote"
      const { bytes: remoteBytes } = await r2.getObject(rem.key);
      const decrypted = await cipher.decryptContent(remoteBytes);
      if (Buffer.from(decrypted).equals(Buffer.from(localBytes))) {
        actions.push({ type: "SEED", relPath, remoteEtag: rem.etag, localContentHash: sha256Hex(localBytes), reason: "リモート再アップロードのみ・内容一致のため転送スキップ" });
      } else {
        actions.push({ type: "PULL", relPath, remoteKey: rem.key, remoteEtag: rem.etag });
      }
    });
  }

  return actions;
}

export async function applyActions({ actions, vaultPath, localFiles, prevEntries, cipher, r2, r2Prefix, apply, allowDelete, onCheckpoint }) {
  const newEntries = {};
  const summary = {};
  const errors = [];
  const bump = (type) => { summary[type] = (summary[type] ?? 0) + 1; };

  let done = 0;
  let checkpointing = false;
  async function maybeCheckpoint() {
    if (!onCheckpoint) return;
    if (done % 50 !== 0) return;
    if (checkpointing) return; // 保存中に他workerが次の閾値に達しても二重書き込みしない
    checkpointing = true;
    try {
      await onCheckpoint(newEntries);
    } finally {
      checkpointing = false;
    }
  }

  async function processOne(action) {
    const { type, relPath } = action;
    const absPath = path.join(vaultPath, ...relPath.split("/"));

    try {
      switch (type) {
        case "NOOP": {
          bump("NOOP");
          const loc = localFiles.get(relPath);
          newEntries[relPath] = {
            localMtimeMs: loc.mtimeMs,
            localSize: loc.size,
            remoteETag: prevEntries[relPath].remoteETag,
            localContentHash: prevEntries[relPath].localContentHash,
          };
          break;
        }
        case "FORGET": {
          bump("FORGET");
          break;
        }
        case "SEED": {
          bump("SEED");
          if (apply) {
            const loc = localFiles.get(relPath);
            newEntries[relPath] = {
              localMtimeMs: loc.mtimeMs,
              localSize: loc.size,
              remoteETag: action.remoteEtag,
              localContentHash: action.localContentHash ?? prevEntries[relPath]?.localContentHash,
            };
          }
          break;
        }
        case "PUSH": {
          bump("PUSH");
          if (!apply) break;
          const bytes = await fs.readFile(absPath);
          const encrypted = await cipher.encryptContent(bytes);
          const encKey = r2Prefix + (await cipher.encryptPath(relPath));
          const loc = localFiles.get(relPath);
          const { etag } = await r2.putObject(encKey, encrypted, { mtime: String(loc.mtimeMs / 1000) });
          newEntries[relPath] = { localMtimeMs: loc.mtimeMs, localSize: loc.size, remoteETag: etag, localContentHash: sha256Hex(bytes) };
          break;
        }
        case "PULL": {
          bump("PULL");
          if (!apply) break;
          const { bytes, metadata } = await r2.getObject(action.remoteKey);
          const decrypted = await cipher.decryptContent(bytes);
          await fs.mkdir(path.dirname(absPath), { recursive: true });
          await fs.writeFile(absPath, decrypted);
          const mtimeMs = parseRemoteMtimeMs(metadata) ?? Date.now();
          const mtimeDate = new Date(mtimeMs);
          await fs.utimes(absPath, mtimeDate, mtimeDate);
          const stat = await fs.stat(absPath);
          newEntries[relPath] = { localMtimeMs: stat.mtimeMs, localSize: stat.size, remoteETag: action.remoteEtag, localContentHash: sha256Hex(decrypted) };
          break;
        }
        case "DELETE_REMOTE": {
          if (!apply || !allowDelete) {
            bump("SKIPPED_DELETE_REMOTE");
            if (apply) newEntries[relPath] = prevEntries[relPath];
            break;
          }
          bump("DELETE_REMOTE");
          await r2.deleteObject(action.remoteKey);
          break;
        }
        case "DELETE_LOCAL": {
          if (!apply || !allowDelete) {
            bump("SKIPPED_DELETE_LOCAL");
            if (apply) newEntries[relPath] = prevEntries[relPath];
            break;
          }
          bump("DELETE_LOCAL");
          await fs.rm(absPath);
          break;
        }
        default:
          break;
      }
    } catch (err) {
      errors.push({ relPath, message: err.message });
      if (prevEntries[relPath]) newEntries[relPath] = prevEntries[relPath];
    }

    done++;
    if (done % 50 === 0 || done === actions.length) {
      console.log(`  適用中: ${done}/${actions.length}`);
    }
    await maybeCheckpoint();
  }

  if (actions.length > 0) {
    console.log(`変更を適用しています(並列${DEFAULT_CONCURRENCY}件)...`);
  }
  await runPool(actions, DEFAULT_CONCURRENCY, processOne);

  return { newEntries, summary, errors };
}
