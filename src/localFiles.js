import fs from "node:fs/promises";
import path from "node:path";

function selectNfc(entries, relDir, policy, aliasStats) {
  const groups = new Map();
  for (const entry of entries) groups.set(entry.name.normalize("NFC"), [...(groups.get(entry.name.normalize("NFC")) ?? []), entry]);
  const selected = [];
  for (const [name, candidates] of groups) {
    if (candidates.length > 1) {
      const exact = candidates.filter((entry) => entry.name === name);
      if (policy !== "prefer-nfc" || exact.length !== 1) throw new Error(`multiple local paths normalize to the same path: ${relDir ? `${relDir}/` : ""}${name}`);
      aliasStats.ignored += candidates.length - 1;
      selected.push([exact[0], name]);
    } else selected.push([candidates[0], name]);
  }
  return selected;
}

export function vaultPath(vaultPath, relPath) {
  const root = path.resolve(vaultPath);
  const parts = relPath.normalize("NFC").replace(/\\/g, "/").split("/");
  for (const part of parts) {
    if (!part || part === "." || part === "..") throw new Error(`unsafe relative path: ${relPath}`);
  }
  const resolved = path.resolve(root, ...parts);
  if (resolved !== root && !resolved.startsWith(`${root}${path.sep}`)) throw new Error(`path escapes vault: ${relPath}`);
  return resolved;
}

function isWithin(root, target) {
  const relative = path.relative(root, target);
  return relative === "" || (!relative.startsWith(`..${path.sep}`) && relative !== ".." && !path.isAbsolute(relative));
}

// Check the real path of the target or its nearest existing ancestor. This
// prevents a lexical in-vault path from crossing a symlink/junction/reparse
// point before a caller reads, replaces, or deletes it.
export async function assertVaultPath(vaultRoot, relPath) {
  const lexicalRoot = path.resolve(vaultRoot);
  const lexicalTarget = vaultPath(lexicalRoot, relPath);
  const realRoot = await fs.realpath(lexicalRoot);
  let candidate = lexicalTarget;
  while (true) {
    try {
      const realCandidate = await fs.realpath(candidate);
      if (!isWithin(realRoot, realCandidate)) throw new Error(`path escapes vault: ${relPath}`);
      return lexicalTarget;
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
      const parent = path.dirname(candidate);
      if (parent === candidate) throw new Error(`path cannot be resolved inside vault: ${relPath}`);
      candidate = parent;
    }
  }
}

export async function listLocalFiles(vaultPath, ignoreMatcher, options = {}) {
  const result = new Map();
  const aliasStats = options.aliasStats ?? { ignored: 0 };
  const policy = options.unicodeCollisionPolicy ?? "error";
  async function walk(absDir, relDir) {
    const entries = await fs.readdir(absDir, { withFileTypes: true });
    for (const [entry, name] of selectNfc(entries, relDir, policy, aliasStats)) {
      const relPath = relDir ? `${relDir}/${name}` : name;
      const absPath = path.join(absDir, entry.name);
      if (entry.isDirectory()) {
        if (!ignoreMatcher.isIgnoredDir(relPath)) await walk(absPath, relPath);
      } else if (entry.isFile() && !ignoreMatcher.isIgnoredFile(relPath)) {
        const stat = await fs.stat(absPath);
        result.set(relPath, { mtimeMs: stat.mtimeMs, size: stat.size });
      }
    }
  }
  await fs.mkdir(vaultPath, { recursive: true });
  await walk(vaultPath, "");
  return result;
}

export async function reconcileLocalFiles(vaultPath, localFiles, remotePaths) {
  let recovered = 0;
  for (const relPath of [...new Set(remotePaths)].sort()) {
    if (localFiles.has(relPath)) continue;
    const target = await assertVaultPath(vaultPath, relPath);
    try {
      const stat = await fs.stat(target);
      if (!stat.isFile()) continue;
      localFiles.set(relPath, { mtimeMs: stat.mtimeMs, size: stat.size });
      recovered += 1;
    } catch (error) { if (error.code !== "ENOENT") throw error; }
  }
  return recovered;
}

export async function classifyLocalPaths(vaultPath, ignoreMatcher, verbose = false) {
  const ignored = [], included = [];
  async function walk(directory, relDir, inherited = false) {
    const entries = await fs.readdir(directory, { withFileTypes: true });
    for (const [entry, name] of selectNfc(entries, relDir, "prefer-nfc", { ignored: 0 })) {
      const relPath = relDir ? `${relDir}/${name}` : name;
      const absPath = path.join(directory, entry.name);
      if (entry.isDirectory()) {
        const excluded = inherited || ignoreMatcher.isIgnoredDir(relPath);
        if (excluded) { ignored.push(`${relPath}/`); if (verbose) await walk(absPath, relPath, true); }
        else await walk(absPath, relPath, false);
      } else if (entry.isFile()) (inherited || ignoreMatcher.isIgnoredFile(relPath) ? ignored : included).push(relPath);
    }
  }
  await fs.mkdir(vaultPath, { recursive: true }); await walk(vaultPath, ""); return { ignored, included };
}
