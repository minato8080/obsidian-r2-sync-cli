import path from "node:path";

const IGNORE_DIR_NAMES = new Set([".git", "node_modules"]);
const IGNORE_BASENAMES = new Set([".DS_Store", "Thumbs.db", "state.json"]);

function normalize(value) {
  return String(value ?? "").replace(/\\/g, "/").replace(/^\/+|\/+$/g, "").normalize("NFC");
}

function globToRegExp(pattern) {
  let source = "";
  for (let i = 0; i < pattern.length; i += 1) {
    const char = pattern[i];
    if (char === "*" && pattern[i + 1] === "*") {
      if (pattern[i + 2] === "/") { source += "(?:.*/)?"; i += 2; }
      else { source += ".*"; i += 1; }
    } else if (char === "*") source += "[^/]*";
    else if (char === "?") source += "[^/]";
    else if (char === "[") {
      const end = pattern.indexOf("]", i + 1);
      if (end < 0 || end === i + 1) source += "\\[";
      else { let content = pattern.slice(i + 1, end); if (content.startsWith("!")) content = `^${content.slice(1)}`; source += `[${content.replaceAll("\\", "\\\\")}]`; i = end; }
    } else source += char.replace(/[\\.^$+{}()|]/g, "\\$&");
  }
  return new RegExp(`^${source}$`);
}

function pathPrefixes(relPath) {
  const parts = relPath.split("/");
  return parts.map((_, i) => parts.slice(0, i + 1).join("/"));
}

function relativeBasePath(vaultPath, basePath) {
  if (!vaultPath || !basePath) return "";
  const relative = path.relative(path.resolve(vaultPath), path.resolve(basePath));
  return !relative || relative === "." || relative === ".." || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative) ? "" : normalize(relative);
}

export function createIgnoreMatcher(extraPatterns = [], options = {}) {
  const baseRelPath = options.baseRelPath !== undefined ? normalize(options.baseRelPath) : relativeBasePath(options.vaultPath, options.basePath);
  const protectedRelPaths = new Set((options.protectedRelPaths ?? []).map(normalize));
  const parsed = extraPatterns.map((raw) => {
    let value = String(raw).replace(/\\/g, "/").trim();
    if (!value) return null;
    const anchored = value.startsWith("./") || value.startsWith("/");
    if (value.startsWith("./")) value = value.slice(2);
    value = value.replace(/^\/+/, "");
    const dirOnly = value.endsWith("/");
    if (dirOnly) value = value.slice(0, -1);
    const hasSlash = value.includes("/");
    return { pattern: anchored || hasSlash ? [baseRelPath, value].filter(Boolean).join("/") : value, anyDepth: !anchored && !hasSlash, dirOnly, regex: globToRegExp(anchored || hasSlash ? [baseRelPath, value].filter(Boolean).join("/") : value) };
  }).filter(Boolean);

  const underBase = (relPath) => !baseRelPath || relPath === baseRelPath || relPath.startsWith(`${baseRelPath}/`);
  function matchesExtra(relPath, isFile) {
    if (!underBase(relPath)) return false;
    for (const item of parsed) {
      if (item.anyDepth) {
        const parts = relPath.split("/");
        const candidates = isFile && item.dirOnly ? parts.slice(0, -1) : parts;
        if (candidates.some((part) => item.regex.test(part))) return true;
      } else {
        for (const prefix of pathPrefixes(relPath)) {
          if (item.dirOnly && isFile && prefix === relPath) continue;
          if (item.regex.test(prefix) || (item.pattern.endsWith("/**") && prefix === item.pattern.slice(0, -3))) return true;
        }
      }
    }
    return false;
  }
  function isIgnoredDir(relPath) {
    const normalized = normalize(relPath);
    return IGNORE_DIR_NAMES.has(normalized.split("/").at(-1)) || matchesExtra(normalized, false);
  }
  function isIgnoredFile(relPath) {
    const normalized = normalize(relPath);
    const parts = normalized.split("/");
    const basename = parts.at(-1);
    const parent = parts.slice(0, -1).join("/");
    const protectedTemp = [...protectedRelPaths].some((protectedPath) => {
      const slash = protectedPath.lastIndexOf("/");
      const protectedParent = slash < 0 ? "" : protectedPath.slice(0, slash);
      const name = slash < 0 ? protectedPath : protectedPath.slice(slash + 1);
      return parent === protectedParent && basename.startsWith(`.${name}.`) && basename.endsWith(".tmp");
    });
    return protectedRelPaths.has(normalized) || protectedTemp || IGNORE_BASENAMES.has(basename) || parts.slice(0, -1).some((part) => IGNORE_DIR_NAMES.has(part)) || matchesExtra(normalized, true);
  }
  return { isIgnoredDir, isIgnoredFile };
}
