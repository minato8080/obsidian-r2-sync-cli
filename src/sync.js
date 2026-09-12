import fs from "node:fs/promises";
import path from "node:path";
import crypto from "node:crypto";
import { runPool, DEFAULT_CONCURRENCY } from "./concurrency.js";
import { entryFromStat, baseBytes, sha256Hex, saveState } from "./state.js";
import { assertVaultPath, reconcileLocalFiles, vaultPath } from "./localFiles.js";

const equal = (a, b) => Buffer.from(a).equals(Buffer.from(b));
const hash = (data) => crypto.createHash("sha256").update(data).digest("hex");
export function remoteMtime(metadata = {}, listed = {}) {
  const raw = metadata.mtime ?? metadata.mmtime ?? metadata["mtime-ms"];
  const value = Number(raw);
  if (Number.isFinite(value)) return Math.abs(value) > 100_000_000_000 ? value : value * 1000;
  const listedValue = listed.lastModified ? Date.parse(listed.lastModified) : NaN;
  return Number.isFinite(listedValue) ? listedValue : Date.now();
}
function safeRelative(value) {
  const normalized = String(value).replace(/\\/g, "/");
  if (!normalized || normalized.startsWith("/") || normalized.split("/").some((part) => !part || part === "." || part === "..")) throw new Error(`unsafe relative path: ${value}`);
  return normalized;
}

async function localSnapshotHash(root, relPath) {
  return hash(await fs.readFile(await assertVaultPath(root, relPath)));
}
function remoteKey(prefix, cipher, relPath) { return prefix + cipher.encryptPath(relPath); }

export function threeWayMerge(base, local, remote) {
  const split = (bytes) => new TextDecoder("utf-8", { fatal: true }).decode(bytes).split(/(?<=\n)/);
  let baseLines, localLines, remoteLines;
  try { baseLines = split(base); localLines = split(local); remoteLines = split(remote); } catch { return { data: null, conflict: true }; }
  const hunks = (a, b) => {
    const rows = [];
    const matrix = Array.from({ length: a.length + 1 }, () => Array(b.length + 1).fill(0));
    for (let i = a.length - 1; i >= 0; i -= 1) for (let j = b.length - 1; j >= 0; j -= 1) matrix[i][j] = a[i] === b[j] ? matrix[i + 1][j + 1] + 1 : Math.max(matrix[i + 1][j], matrix[i][j + 1]);
    let i = 0, j = 0, start = null, replacement = [];
    const flush = (end) => { if (start !== null) { rows.push({ start, end, replacement }); start = null; replacement = []; } };
    while (i < a.length || j < b.length) {
      if (i < a.length && j < b.length && a[i] === b[j]) { flush(i); i += 1; j += 1; }
      else { if (start === null) start = i; if (j < b.length && (i === a.length || matrix[i][j + 1] >= matrix[i + 1][j])) replacement.push(b[j++]); else i += 1; }
    }
    flush(i); return rows;
  };
  const left = hunks(baseLines, localLines), right = hunks(baseLines, remoteLines);
  const overlap = (a, b) => (a.start === a.end && b.start === b.end) ? a.start === b.start : a.start === a.end ? b.start <= a.start && a.start <= b.end : b.start === b.end ? a.start <= b.start && b.start <= a.end : Math.max(a.start, b.start) < Math.min(a.end, b.end);
  const groups = [];
  for (const side of [["local", left], ["remote", right]]) for (const hunk of side[1]) {
    let group = groups.find((candidate) => candidate.some(([name, existing]) => overlap(hunk, existing)));
    if (!group) groups.push(group = []); group.push([side[0], hunk]);
  }
  const render = (start, end, changes) => { const out = [], sorted = [...changes].sort((a, b) => a.start - b.start); let cursor = start; for (const hunk of sorted) { out.push(...baseLines.slice(cursor, hunk.start), ...hunk.replacement); cursor = hunk.end; } out.push(...baseLines.slice(cursor, end)); return out; };
  const output = []; let cursor = 0;
  for (const group of [...groups].sort((a, b) => Math.min(...a.map(([, h]) => h.start)) - Math.min(...b.map(([, h]) => h.start)))) {
    const start = Math.min(...group.map(([, h]) => h.start)), end = Math.max(...group.map(([, h]) => h.end));
    output.push(...baseLines.slice(cursor, start));
    const localText = render(start, end, group.filter(([side]) => side === "local").map(([, h]) => h));
    const remoteText = render(start, end, group.filter(([side]) => side === "remote").map(([, h]) => h));
    if (JSON.stringify(localText) === JSON.stringify(remoteText)) output.push(...localText);
    else if (!group.some(([side]) => side === "local")) output.push(...remoteText);
    else if (!group.some(([side]) => side === "remote")) output.push(...localText);
    else { const withNl = (lines) => { const text = lines.join(""); return text && !text.endsWith("\n") ? `${text}\n` : text; }; output.push("<<<<<<< LOCAL\n", withNl(localText), "=======\n", withNl(remoteText), ">>>>>>> REMOTE\n"); return { data: Buffer.from(output.join("") + baseLines.slice(end).join("")), conflict: true }; }
    cursor = end;
  }
  output.push(...baseLines.slice(cursor)); return { data: Buffer.from(output.join("")), conflict: false };
}

async function collectRemote(remoteItems, cipher, prefix, ignoreMatcher, policy = "error", stats = { ignored: 0 }) {
  const grouped = new Map(), ignored = [], conflicts = [];
  for (const raw of remoteItems) {
    if (!raw || typeof raw.key !== "string") { conflicts.push({ path: "<remote-list>", reason: "R2 LIST returned an invalid object" }); continue; }
    if (prefix && !raw.key.startsWith(prefix)) continue;
    const encoded = prefix ? raw.key.slice(prefix.length) : raw.key;
    try {
      const spelling = safeRelative(await cipher.decryptPath(encoded));
      const canonical = spelling.normalize("NFC");
      if (ignoreMatcher.isIgnoredFile(canonical)) { ignored.push({ key: raw.key, path: canonical, reason: "ignored" }); continue; }
      grouped.set(canonical, [...(grouped.get(canonical) ?? []), { ...raw, _decodedPath: spelling }]);
    } catch (error) { ignored.push({ key: raw.key, reason: `cannot decode object path: ${error.message}` }); }
  }
  const remotes = new Map();
  for (const [canonical, candidates] of grouped) {
    const exact = candidates.filter((candidate) => candidate._decodedPath === canonical);
    if (candidates.length > 1 && (policy !== "prefer-nfc" || exact.length !== 1)) { conflicts.push({ path: canonical, reason: "multiple remote objects decode to the same path" }); continue; }
    const selected = exact[0] ?? candidates[0]; remotes.set(canonical, selected); stats.ignored += candidates.length - 1;
    for (const alias of candidates) if (alias !== selected) ignored.push({ key: alias.key, path: alias._decodedPath, reason: "Unicode alias ignored in favor of NFC" });
  }
  return { remotes, ignored, conflicts };
}

export async function planSync({ vaultPath: root, localFiles, remoteItems, remoteMap, prevEntries, ignoreMatcher, cipher, r2Prefix, r2, unicodeCollisionPolicy = "error", collisionStats = { ignored: 0 }, merge = false }) {
  const remoteByPath = remoteMap ?? (await collectRemote(remoteItems, cipher, r2Prefix, ignoreMatcher, unicodeCollisionPolicy, collisionStats)).remotes;
  const actions = [], candidates = [];
  const paths = [...new Set([...localFiles.keys(), ...remoteByPath.keys(), ...Object.keys(prevEntries)])].sort();
  for (const relPath of paths) {
    const loc = localFiles.get(relPath), rem = remoteByPath.get(relPath), prev = prevEntries[relPath];
    if (!prev) {
      if (loc && rem) candidates.push({ type: "BOOTSTRAP", relPath, loc, rem });
      else if (loc) actions.push({ type: "PUSH", relPath, localSnapshotHash: await localSnapshotHash(root, relPath) });
      else if (rem) actions.push({ type: "PULL", relPath, localSnapshotHash: null, remoteKey: rem.key, remoteEtag: rem.etag });
      continue;
    }
    const localGone = !loc && prev.localMtimeMs !== undefined, remoteGone = !rem && prev.remoteETag !== undefined;
    const localChanged = !!loc && (prev.localMtimeMs === undefined || Math.abs(loc.mtimeMs - Number(prev.localMtimeMs)) > 1 || loc.size !== prev.localSize);
    const remoteChanged = !!rem && rem.etag !== prev.remoteETag;
    if (localGone && remoteGone) actions.push({ type: "FORGET", relPath, localSnapshotHash: null });
    else if (localGone) actions.push(remoteChanged ? { type: "PULL", relPath, localSnapshotHash: null, remoteKey: rem.key, remoteEtag: rem.etag, reason: "ローカル削除だがリモートは編集済み: 編集を優先" } : { type: "DELETE_REMOTE", relPath, localSnapshotHash: null, remoteKey: rem.key });
    else if (remoteGone) actions.push(localChanged ? { type: "PUSH", relPath, localSnapshotHash: await localSnapshotHash(root, relPath), reason: "リモート削除だがローカルは編集済み: 編集を優先" } : { type: "DELETE_LOCAL", relPath, localSnapshotHash: await localSnapshotHash(root, relPath) });
    else if (localChanged && remoteChanged) candidates.push({ type: "BOTH_CHANGED", relPath, loc, rem, prev });
    else if (localChanged && prev.localContentHash) candidates.push({ type: "LOCAL_CHANGED", relPath, loc, rem, prev });
    else if (localChanged) actions.push({ type: "PUSH", relPath, localSnapshotHash: await localSnapshotHash(root, relPath) });
    else if (remoteChanged) candidates.push({ type: "REMOTE_CHANGED", relPath, loc, rem, prev });
    else actions.push({ type: "NOOP", relPath });
  }
  await runPool(candidates, DEFAULT_CONCURRENCY, async (candidate) => {
    const localBytes = await fs.readFile(vaultPath(root, candidate.relPath));
    if (candidate.type === "LOCAL_CHANGED") { const current = hash(localBytes); actions.push(current === candidate.prev.localContentHash ? { type: "SEED", relPath: candidate.relPath, localSnapshotHash: current, remoteEtag: candidate.rem.etag, localContentHash: current } : { type: "PUSH", relPath: candidate.relPath, localSnapshotHash: current }); return; }
    const remote = await r2.getObject(candidate.rem.key), remoteBytes = Buffer.from(await cipher.decryptContent(remote.bytes));
    if (equal(localBytes, remoteBytes)) { actions.push({ type: "SEED", relPath: candidate.relPath, localSnapshotHash: hash(localBytes), remoteEtag: candidate.rem.etag, localContentHash: hash(localBytes) }); return; }
    const remoteMs = remoteMtime(remote.metadata, candidate.rem);
    if (candidate.type === "BOTH_CHANGED" && merge) {
      const base = baseBytes(candidate.prev);
      if (!base) actions.push({ type: "CONFLICT", relPath: candidate.relPath, reason: "merge base is unavailable in checkpoint state" });
      else {
        const merged = threeWayMerge(base, localBytes, remoteBytes);
        if (merged.conflict) actions.push({ type: "CONFLICT", relPath: candidate.relPath, reason: "three-way merge conflict" });
        else actions.push({ type: "MERGE", relPath: candidate.relPath, data: merged.data, remoteKey: candidate.rem.key, remoteEtag: candidate.rem.etag, localMtimeMs: Math.max(candidate.loc.mtimeMs, remoteMs), localSnapshotHash: hash(localBytes), remoteBytes });
      }
    } else if (candidate.type === "BOTH_CHANGED") actions.push(candidate.loc.mtimeMs > remoteMs ? { type: "PUSH", relPath: candidate.relPath, localSnapshotHash: hash(localBytes), reason: "両側変更・ローカルの方が新しいため上書き" } : { type: "PULL", relPath: candidate.relPath, localSnapshotHash: hash(localBytes), remoteKey: candidate.rem.key, remoteEtag: candidate.rem.etag, data: remoteBytes, mtimeMs: remoteMs, reason: "両側変更・リモートの方が新しいため上書き" });
    else actions.push({ type: "PULL", relPath: candidate.relPath, localSnapshotHash: hash(localBytes), remoteKey: candidate.rem.key, remoteEtag: candidate.rem.etag, data: remoteBytes, mtimeMs: remoteMs });
  });
  return actions;
}

export async function atomicReplace(target, data, mtimeMs) {
  await fs.mkdir(path.dirname(target), { recursive: true });
  const temp = path.join(path.dirname(target), `.${path.basename(target)}.${process.pid}.${Date.now()}.tmp`);
  try { await fs.writeFile(temp, data); const date = new Date(mtimeMs); await fs.utimes(temp, date, date); await fs.rename(temp, target); }
  finally { await fs.rm(temp, { force: true }); }
}

async function verifyLocalSnapshots(actions, root) {
  const retained = [], conflicts = [];
  for (const action of actions) {
    try {
      const target = await assertVaultPath(root, action.relPath);
      if (action.localSnapshotHash === null) {
        try { await fs.lstat(target); conflicts.push({ path: action.relPath, reason: "local target appeared during planning" }); }
        catch (error) { if (error.code !== "ENOENT") throw error; }
      } else if (action.localSnapshotHash !== undefined) {
        const current = hash(await fs.readFile(target));
        if (current !== action.localSnapshotHash) conflicts.push({ path: action.relPath, reason: "local file changed during planning" });
      }
      if (!conflicts.some((item) => item.path === action.relPath)) retained.push(action);
    } catch (error) {
      conflicts.push({ path: action.relPath, reason: error.message });
    }
  }
  return { retained, conflicts };
}

export async function applyActions({ actions, vaultPath: root, localFiles, prevEntries, cipher, r2, r2Prefix, apply, allowDelete, onCheckpoint, textMergeBaseMaxBytes, timings = { remote: 0, checkpoint: 0 } }) {
  const newEntries = { ...prevEntries }, summary = {}, errors = [], bump = (key) => { summary[key] = (summary[key] ?? 0) + 1; };
  for (const action of actions.filter(({ type }) => type === "NOOP")) { bump("NOOP"); newEntries[action.relPath] = { ...prevEntries[action.relPath], localMtimeMs: localFiles.get(action.relPath).mtimeMs, localSize: localFiles.get(action.relPath).size }; }
  const applicable = actions.filter(({ type }) => type !== "NOOP" && type !== "CONFLICT");
  if (!apply) { for (const action of applicable) bump(action.type); return { newEntries, summary, errors }; }
  const checkpoint = async (relPath, etag, bytes, persist = true) => { const target = await assertVaultPath(root, relPath); const stat = await fs.stat(target); newEntries[relPath] = entryFromStat(stat, etag, bytes, textMergeBaseMaxBytes); if (persist && onCheckpoint) { const started = performance.now(); await onCheckpoint(newEntries); timings.checkpoint += performance.now() - started; } };
  const push = applicable.filter(({ type }) => type === "PUSH");
  let pushed = 0;
  await runPool(push, DEFAULT_CONCURRENCY, async (action) => { try { const target = await assertVaultPath(root, action.relPath); const stat = await fs.stat(target); const planned = localFiles.get(action.relPath); if (!planned || Math.abs(stat.mtimeMs - planned.mtimeMs) > 1 || stat.size !== planned.size) throw new Error("local file changed during planning"); const bytes = await fs.readFile(target); if (action.localSnapshotHash !== undefined && hash(bytes) !== action.localSnapshotHash) throw new Error("local file changed during planning"); const encrypted = await cipher.encryptContent(bytes); const key = r2Prefix + await cipher.encryptPath(action.relPath); const started = performance.now(); const result = await r2.putObject(key, encrypted, { mtime: String(stat.mtimeMs / 1000) }); timings.remote += performance.now() - started; bump("PUSH"); pushed += 1; await checkpoint(action.relPath, result.etag, bytes, false); } catch (error) { errors.push({ relPath: action.relPath, message: error.message }); } });
  if (pushed && onCheckpoint) { const started = performance.now(); await onCheckpoint(newEntries); timings.checkpoint += performance.now() - started; }
  if (errors.length > 0) return { newEntries, summary, errors };
  for (const action of applicable.filter(({ type }) => !["PUSH", "SEED"].includes(type))) {
    try {
      const currentCheck = await verifyLocalSnapshots([action], root);
      if (currentCheck.conflicts.length) throw new Error(currentCheck.conflicts[0].reason);
      const target = await assertVaultPath(root, action.relPath);
       if (action.type === "PULL") { const remote = action.data ? null : await r2.getObject(action.remoteKey); const bytes = action.data ? Buffer.from(action.data) : Buffer.from(await cipher.decryptContent(remote.bytes)); await atomicReplace(target, bytes, action.mtimeMs ?? remoteMtime(remote.metadata)); await checkpoint(action.relPath, action.remoteEtag ?? remote?.etag, bytes); bump("PULL"); }
      else if (action.type === "MERGE") { await atomicReplace(target, action.data, action.localMtimeMs); const encrypted = await cipher.encryptContent(action.data); const started = performance.now(); const result = await r2.putObject(action.remoteKey, encrypted, { mtime: String(action.localMtimeMs / 1000) }); timings.remote += performance.now() - started; await checkpoint(action.relPath, result.etag, action.data); bump("MERGE"); }
      else if (action.type === "DELETE_REMOTE") { if (allowDelete) { const started = performance.now(); await r2.deleteObject(action.remoteKey); timings.remote += performance.now() - started; delete newEntries[action.relPath]; bump("DELETE_REMOTE"); if (onCheckpoint) { const checkpointStarted = performance.now(); await onCheckpoint(newEntries); timings.checkpoint += performance.now() - checkpointStarted; } } else bump("SKIPPED_DELETE_REMOTE"); }
      else if (action.type === "DELETE_LOCAL") { if (allowDelete) { await fs.rm(target); delete newEntries[action.relPath]; bump("DELETE_LOCAL"); if (onCheckpoint) { const checkpointStarted = performance.now(); await onCheckpoint(newEntries); timings.checkpoint += performance.now() - checkpointStarted; } } else bump("SKIPPED_DELETE_LOCAL"); }
      else if (action.type === "FORGET") { delete newEntries[action.relPath]; bump("FORGET"); if (onCheckpoint) { const checkpointStarted = performance.now(); await onCheckpoint(newEntries); timings.checkpoint += performance.now() - checkpointStarted; } }
    } catch (error) { errors.push({ relPath: action.relPath, message: error.message }); }
  }
  const seeds = applicable.filter(({ type }) => type === "SEED");
  for (const action of seeds) { try { const target = await assertVaultPath(root, action.relPath); const bytes = await fs.readFile(target); if (action.localSnapshotHash !== undefined && hash(bytes) !== action.localSnapshotHash) throw new Error("local file changed during planning"); bump("SEED"); await checkpoint(action.relPath, action.remoteEtag, bytes, false); } catch (error) { errors.push({ relPath: action.relPath, message: error.message }); } }
  if (seeds.length && onCheckpoint) await onCheckpoint(newEntries);
  return { newEntries, summary, errors };
}

export async function executeFullSync(options) {
  const started = performance.now(), root = path.resolve(options.vaultPath), statePath = path.resolve(options.statePath), progress = options.progress ?? (() => {}), collisionStats = { ignored: 0 };
  const { listRemote, getObject, putObject, deleteObject, cipher, r2Prefix = "", extraPatterns = [], protectedRelPaths = [], unicodeCollisionPolicy = "error" } = options;
  const ignoreMatcher = options.ignoreMatcher;
  const localStarted = performance.now();
  const localFiles = await options.listLocalFiles(root, ignoreMatcher, { unicodeCollisionPolicy, aliasStats: collisionStats });
  const scanLocal = performance.now() - localStarted;
  const listStarted = performance.now(), listed = await listRemote(); const listRemoteMs = performance.now() - listStarted;
  const decodeStarted = performance.now(), collected = await collectRemote(listed, cipher, r2Prefix, ignoreMatcher, unicodeCollisionPolicy, collisionStats); const decodeRemote = performance.now() - decodeStarted;
  const reconcileStarted = performance.now(), reconciled = await reconcileLocalFiles(root, localFiles, collected.remotes.keys()); const reconcileLocal = performance.now() - reconcileStarted;
  const previous = await options.loadState(statePath, unicodeCollisionPolicy, collisionStats);
  progress(`local files: ${localFiles.size}件 / remote objects: ${listed.length}件 / 前回状態: ${Object.keys(previous).length}件`);
  if (reconciled) progress(`local paths reconciled: ${reconciled}件`);
  const actions = await planSync({ vaultPath: root, localFiles, remoteItems: listed, remoteMap: collected.remotes, prevEntries: previous, ignoreMatcher, cipher, r2Prefix, r2: { getObject }, unicodeCollisionPolicy, collisionStats, merge: true });
  const conflictsFromActions = actions.filter(({ type }) => type === "CONFLICT").map((action) => ({ path: action.relPath, reason: action.reason }));
  const conflicts = [...collected.conflicts, ...conflictsFromActions];
  const blockedPaths = new Set(conflicts.filter((item) => item.path && item.path !== "<remote-list>").map((item) => item.path));
  const rawApplicable = actions.filter(({ type }) => type !== "NOOP" && type !== "CONFLICT");
  const applicable = rawApplicable.filter((action) => !blockedPaths.has(action.relPath));
  const grouped = Object.groupBy ? Object.groupBy(actions, (a) => a.type) : actions.reduce((m, a) => ((m[a.type] ??= []).push(a), m), {});
  for (const [type, items] of Object.entries(grouped)) { progress(`\n[${type}] ${items.length}件`); for (const item of items.slice(0, 20)) progress(`  ${item.relPath}${item.reason ? ` (${item.reason})` : ""}`); if (items.length > 20) progress(`  ...ほか${items.length - 20}件`); }
  const result = { ok: conflicts.length === 0, mode: options.apply ? "apply" : "dry-run", unchanged: actions.filter(({ type }) => type === "NOOP").length, scannedLocal: localFiles.size, scannedRemote: listed.length, planned: applicable.length, validated: 0, applied: 0, reconciledLocalFiles: reconciled, unicodeAliasesIgnored: collisionStats.ignored, errors: [], conflicts, ignoredRemoteObjects: collected.ignored, remoteSnapshotRecheckEnabled: options.recheckRemoteBeforeApply !== false, remoteSnapshotRechecked: false, plannedByType: Object.fromEntries(Object.entries(grouped).filter(([type]) => !["NOOP", "CONFLICT"].includes(type)).map(([type, values]) => [type, values.length])), appliedByType: {}, skippedByType: {}, timingsMs: { scan: performance.now() - started, scanLocal, listRemote: listRemoteMs, decodeRemote, reconcileLocal, conflictCheck: 0, fetchValidate: 0, snapshotCheck: 0, apply: 0, applyRemote: 0, applyCheckpoint: 0 } };
  for (const action of rawApplicable.filter((item) => blockedPaths.has(item.relPath))) result.skippedByType[action.type] = (result.skippedByType[action.type] ?? 0) + 1;
  if (!options.apply) { result.timingsMs.total = performance.now() - started; return result; }
  const hasGlobalConflict = conflicts.some((item) => item.path === "<remote-list>");
  let actionsToApply = hasGlobalConflict ? [] : applicable;
  if (options.recheckRemoteBeforeApply !== false && applicable.length > 0) {
    const checkStarted = performance.now();
    try {
      const latest = await listRemote();
       const latestCollected = await collectRemote(latest, cipher, r2Prefix, ignoreMatcher, unicodeCollisionPolicy);
       result.conflicts.push(...latestCollected.conflicts);
       const changed = new Set();
      for (const relPath of new Set([...collected.remotes.keys(), ...latestCollected.remotes.keys()])) {
        const before = collected.remotes.get(relPath), after = latestCollected.remotes.get(relPath);
        const identity = (item) => item ? `${item.key}\0${item.etag}\0${item.size}\0${item.lastModified}` : "<missing>";
        if (identity(before) !== identity(after)) { changed.add(relPath); result.conflicts.push({ path: relPath, reason: "remote object changed during planning" }); }
      }
       for (const conflict of latestCollected.conflicts) if (conflict.path && conflict.path !== "<remote-list>") changed.add(conflict.path);
       if (latestCollected.conflicts.some((item) => item.path === "<remote-list>")) result.errors.push({ path: "<remote-list>", error: "R2 LIST returned an invalid object" });
       actionsToApply = applicable.filter((action) => !changed.has(action.relPath));
      for (const action of applicable.filter((item) => changed.has(item.relPath))) result.skippedByType[action.type] = (result.skippedByType[action.type] ?? 0) + 1;
      result.planned = actionsToApply.length;
      result.remoteSnapshotRechecked = true;
    } catch (error) { result.errors.push({ path: "<remote-list>", error: error.message }); }
    result.timingsMs.snapshotCheck = performance.now() - checkStarted;
  } else if (options.recheckRemoteBeforeApply === false && applicable.length > 0) progress("警告: 適用前のR2 snapshot再確認をスキップします。");
  if (result.errors.length === 0 && !hasGlobalConflict) {
    const localCheck = await verifyLocalSnapshots(actionsToApply, root);
    actionsToApply = localCheck.retained;
    for (const conflict of localCheck.conflicts) {
      result.conflicts.push(conflict);
      const action = applicable.find((item) => item.relPath === conflict.path);
      if (action) result.skippedByType[action.type] = (result.skippedByType[action.type] ?? 0) + 1;
    }
    result.planned = actionsToApply.length;
    const applyTimings = { remote: 0, checkpoint: 0 };
    const applied = await applyActions({ actions: actionsToApply, vaultPath: root, localFiles, prevEntries: previous, cipher, r2: { getObject, putObject, deleteObject }, r2Prefix, apply: true, allowDelete: options.allowDelete, textMergeBaseMaxBytes: options.textMergeBaseMaxBytes, timings: applyTimings, onCheckpoint: async (entries) => options.saveState(statePath, entries) });
    for (const [type, count] of Object.entries(applied.summary)) { if (type.startsWith("SKIPPED_")) result.skippedByType[type.slice(8)] = count; else if (type !== "NOOP") result.appliedByType[type] = count; if (!type.startsWith("SKIPPED_")) result.applied += count; }
    result.errors.push(...applied.errors);
    result.timingsMs.apply = performance.now() - started - result.timingsMs.scan;
    result.timingsMs.applyRemote = applyTimings.remote;
    result.timingsMs.applyCheckpoint = applyTimings.checkpoint;
  }
  result.ok = result.errors.length === 0 && result.conflicts.length === 0; result.timingsMs.total = performance.now() - started; return result;
}
