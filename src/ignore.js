// ここに書くのは「どんなvaultでも汎用的に除外すべきもの」だけに限定する。
// vault固有・配置固有の除外(private/, 自分のツール配置先, .obsidian内の特定ファイル等)は
// ハードコードせず、利用者が .env の IGNORE_EXTRA で指定する(.env.example 参照)。
const IGNORE_DIR_NAMES = new Set([".git", "node_modules"]);
const IGNORE_BASENAMES = new Set([".DS_Store", "Thumbs.db"]);

// gitignore風の簡易パターン(globの完全実装ではない):
//   "foo/"     : ルートからの相対パスがfoo/配下すべて
//   "foo/bar"  : ルートからのfoo/bar完全一致
//   "**/foo"   : 深さ問わずfooという名前(ファイル/ディレクトリどちらでも)
//   "foo"      : スラッシュを含まない場合は "**/foo" と同じ(名前だけでどの階層にも一致)
function parseExtraPattern(raw) {
  let norm = raw.replace(/\\/g, "/").replace(/^\/+/, "").trim();
  if (!norm) return null;
  const isDirOnly = norm.endsWith("/");
  if (isDirOnly) norm = norm.slice(0, -1);
  if (norm.startsWith("**/")) norm = norm.slice(3);
  const anyDepth = !norm.includes("/");
  return { norm, anyDepth };
}

export function createIgnoreMatcher(extraPatterns = []) {
  const parsed = extraPatterns.map(parseExtraPattern).filter(Boolean);

  function matchesExtra(relPath) {
    const parts = relPath.split("/");
    for (const { norm, anyDepth } of parsed) {
      if (anyDepth) {
        if (parts.includes(norm)) return true;
      } else if (relPath === norm || relPath.startsWith(`${norm}/`)) {
        return true;
      }
    }
    return false;
  }

  function isIgnoredDir(relDirPath) {
    const name = relDirPath.split("/").pop();
    if (IGNORE_DIR_NAMES.has(name)) return true;
    if (matchesExtra(relDirPath)) return true;
    return false;
  }

  function isIgnoredFile(relFilePath) {
    const parts = relFilePath.split("/");
    const basename = parts[parts.length - 1];
    if (IGNORE_BASENAMES.has(basename)) return true;
    if (parts.slice(0, -1).some((part) => IGNORE_DIR_NAMES.has(part))) return true;
    if (matchesExtra(relFilePath)) return true;
    return false;
  }

  return { isIgnoredDir, isIgnoredFile };
}
