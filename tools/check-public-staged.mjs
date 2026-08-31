import { execFileSync } from "node:child_process";

function git(args) {
  return execFileSync("git", args, { encoding: "utf8" });
}

const stagedFiles = git(["diff", "--cached", "--name-only", "--diff-filter=ACMR"])
  .split(/\r?\n/)
  .map((file) => file.trim())
  .filter(Boolean);

const forbiddenFilePatterns = [
  { pattern: /(^|\/)\.env$/i, label: "実値の.env" },
  { pattern: /(^|\/)\.sync-state(?:\.json)?$/i, label: "同期状態ファイル" },
  { pattern: /\.(?:log|trace)$/i, label: "ログファイル" },
];

const forbiddenContentPatterns = [
  { pattern: /[A-Za-z]:\\Users\\[^\\\s]+\\/i, label: "個人用Windowsパス" },
  { pattern: /\/(?:Users|home)\/[^/\s]+\//i, label: "個人用POSIXパス" },
  { pattern: /-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----/i, label: "秘密鍵" },
  { pattern: /\b(?:AKIA|ASIA)[0-9A-Z]{16}\b/, label: "AWSアクセスキー形式" },
  { pattern: /\bgithub_pat_[A-Za-z0-9_]{20,}\b/, label: "GitHubトークン形式" },
  { pattern: /\bgh[pousr]_[A-Za-z0-9]{20,}\b/, label: "GitHubトークン形式" },
];

const failures = [];

for (const file of stagedFiles) {
  for (const rule of forbiddenFilePatterns) {
    if (rule.pattern.test(file)) {
      failures.push(`${file}: ${rule.label}`);
    }
  }
}

const diff = git(["diff", "--cached", "--unified=0", "--no-ext-diff", "--text", "--"]);
let currentFile = "";
for (const line of diff.split(/\r?\n/)) {
  if (line.startsWith("+++ b/")) {
    currentFile = line.slice("+++ b/".length);
    continue;
  }
  if (!line.startsWith("+") || line.startsWith("+++")) continue;
  if (currentFile === "tools/check-public-staged.mjs") continue;

  const addedContent = line.slice(1);
  for (const rule of forbiddenContentPatterns) {
    if (rule.pattern.test(addedContent)) {
      failures.push(`${currentFile || "staged diff"}: ${rule.label}`);
    }
  }
}

if (failures.length > 0) {
  console.error("公開前チェックに失敗しました。コミットを中止します。");
  for (const failure of [...new Set(failures)]) console.error(`- ${failure}`);
  process.exitCode = 1;
} else {
  console.log("公開前チェック: OK（staged差分のみ、外部通信なし）");
}
