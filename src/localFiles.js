import fs from "node:fs/promises";
import path from "node:path";

export async function listLocalFiles(vaultPath, ignoreMatcher) {
  const result = new Map();

  async function walk(absDir, relDir) {
    const entries = await fs.readdir(absDir, { withFileTypes: true });
    for (const entry of entries) {
      const relPath = relDir ? `${relDir}/${entry.name}` : entry.name;
      const absPath = path.join(absDir, entry.name);
      if (entry.isDirectory()) {
        if (ignoreMatcher.isIgnoredDir(relPath)) continue;
        await walk(absPath, relPath);
      } else if (entry.isFile()) {
        if (ignoreMatcher.isIgnoredFile(relPath)) continue;
        const stat = await fs.stat(absPath);
        result.set(relPath, { mtimeMs: stat.mtimeMs, size: stat.size });
      }
    }
  }

  await walk(vaultPath, "");
  return result;
}
