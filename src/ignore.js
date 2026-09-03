import path from "node:path";

// ここに書くのは「どんなvaultでも汎用的に除外すべきもの」だけに限定する。
// vault固有・配置固有の除外(private/, 自分のツール配置先, .obsidian内の特定ファイル等)は
// ハードコードせず、利用者が .env の IGNORE_EXTRA で指定する(.env.example 参照)。
const IGNORE_DIR_NAMES = new Set([".git", "node_modules"]);
const IGNORE_BASENAMES = new Set([".DS_Store", "Thumbs.db"]);

// .gitignore風のglob。パターンの基準ディレクトリはconfig/.envのある場所。
//   "/foo"      : 基準ディレクトリ直下のfoo
//   "foo/bar"   : 基準ディレクトリからの相対パス
//   "foo"       : 基準ディレクトリ以下の全階層にあるfoo
//   "foo/"      : fooディレクトリと配下
//   "foo/**"    : foo配下を再帰的にすべて
//   "**/foo"    : 基準ディレクトリ以下の任意階層にあるfoo
//   "*", "?", "[]" はパス区切りをまたがず、"**" は複数階層をまたぐ。
// 先頭の"./"は旧設定との互換用に"/"と同じ扱いにする。
function normalizeRelPath(value) {
  return value.replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
}

function joinRelPath(base, child) {
  const parts = [base, child].filter(Boolean).map(normalizeRelPath).filter(Boolean);
  return parts.join("/");
}

function relativeBasePath(vaultPath, basePath) {
  if (!vaultPath || !basePath) return "";
  const relative = path.relative(path.resolve(vaultPath), path.resolve(basePath));
  if (!relative || relative === ".") return "";
  if (relative === ".." || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) return "";
  return normalizeRelPath(relative);
}

function globToRegExp(pattern) {
  let source = "";
  for (let index = 0; index < pattern.length; index += 1) {
    const char = pattern[index];
    if (char === "*" && pattern[index + 1] === "*") {
      if (pattern[index + 2] === "/") {
        source += "(?:.*/)?";
        index += 2;
      } else {
        source += ".*";
        index += 1;
      }
    } else if (char === "*") {
      source += "[^/]*";
    } else if (char === "?") {
      source += "[^/]";
    } else if (char === "[") {
      const end = pattern.indexOf("]", index + 1);
      if (end === -1 || end === index + 1) {
        source += "\\[";
      } else {
        let content = pattern.slice(index + 1, end);
        if (content.startsWith("!")) content = `^${content.slice(1)}`;
        source += `[${content.replaceAll("\\", "\\\\")}]`;
        index = end;
      }
    } else {
      source += char.replace(/[\\.^$+{}()|]/g, "\\$&");
    }
  }
  return new RegExp(`^${source}$`);
}

function globMatches(pattern, value) {
  return globToRegExp(pattern).test(value);
}

function pathPrefixes(relPath) {
  const parts = relPath.split("/");
  return parts.map((_, index) => parts.slice(0, index + 1).join("/"));
}

function parseExtraPattern(raw, baseRelPath) {
  let norm = raw.replace(/\\/g, "/").trim();
  if (!norm) return null;
  const legacyAnchored = norm.startsWith("./");
  const anchored = legacyAnchored || norm.startsWith("/");
  if (legacyAnchored) norm = norm.slice(2);
  norm = norm.replace(/^\/+/, "");
  const isDirOnly = norm.endsWith("/");
  if (isDirOnly) norm = norm.slice(0, -1);
  if (!norm) return null;
  const hasSlash = norm.includes("/");
  return {
    pattern: anchored || hasSlash ? joinRelPath(baseRelPath, norm) : norm,
    anyDepth: !anchored && !hasSlash,
    isDirOnly,
  };
}

export function createIgnoreMatcher(extraPatterns = [], options = {}) {
  const baseRelPath = options.baseRelPath !== undefined
    ? normalizeRelPath(options.baseRelPath)
    : relativeBasePath(options.vaultPath, options.basePath);
  const protectedRelPaths = new Set((options.protectedRelPaths ?? []).map(normalizeRelPath));
  const parsed = extraPatterns.map((pattern) => parseExtraPattern(pattern, baseRelPath)).filter(Boolean);

  function isUnderBase(relPath) {
    return !baseRelPath || relPath === baseRelPath || relPath.startsWith(`${baseRelPath}/`);
  }

  function matchesExtra(relPath, isFile) {
    if (!isUnderBase(relPath)) return false;
    for (const { pattern, anyDepth, isDirOnly } of parsed) {
      if (anyDepth) {
        const parts = relPath.split("/");
        const candidates = isFile && isDirOnly ? parts.slice(0, -1) : parts;
        if (candidates.some((part) => globMatches(pattern, part))) return true;
        continue;
      }
      for (const prefix of pathPrefixes(relPath)) {
        if (isDirOnly && isFile && prefix === relPath) continue;
        if (globMatches(pattern, prefix) || (pattern.endsWith("/**") && prefix === pattern.slice(0, -3))) return true;
      }
    }
    return false;
  }

  function isIgnoredDir(relDirPath) {
    const name = relDirPath.split("/").pop();
    if (IGNORE_DIR_NAMES.has(name)) return true;
    if (matchesExtra(relDirPath, false)) return true;
    return false;
  }

  function isIgnoredFile(relFilePath) {
    const parts = relFilePath.split("/");
    const basename = parts[parts.length - 1];
    const normalized = normalizeRelPath(relFilePath);
    if (protectedRelPaths.has(normalized)) return true;
    for (const protectedPath of protectedRelPaths) {
      const slash = protectedPath.lastIndexOf("/");
      const parent = slash === -1 ? "" : protectedPath.slice(0, slash);
      const protectedName = slash === -1 ? protectedPath : protectedPath.slice(slash + 1);
      const fileParent = parts.length === 1 ? "" : parts.slice(0, -1).join("/");
      if (fileParent === parent && basename.startsWith(`.${protectedName}.`) && basename.endsWith(".tmp")) return true;
    }
    if (IGNORE_BASENAMES.has(basename)) return true;
    if (parts.slice(0, -1).some((part) => IGNORE_DIR_NAMES.has(part))) return true;
    if (matchesExtra(relFilePath, true)) return true;
    return false;
  }

  return { isIgnoredDir, isIgnoredFile };
}
