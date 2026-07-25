import { config as loadEnv } from "dotenv";
import path from "node:path";

// カレントディレクトリ基準(dotenvの既定動作)。バンドル後の単体ファイルでも
// 実行ディレクトリに .env / .sync-state.json を置けばそのまま動くようにするため、
// __dirname(ソースファイルの位置)には依存しない。
loadEnv();

function required(name) {
  const v = process.env[name];
  if (!v) throw new Error(`.env に ${name} を設定してください（.env.example を参照）`);
  return v;
}

function normalizePrefix(prefix) {
  if (!prefix) return "";
  let p = prefix.replace(/\\/g, "/");
  if (p.startsWith("/")) p = p.slice(1);
  if (!p.endsWith("/")) p += "/";
  return p;
}

export const config = {
  vaultPath: required("VAULT_PATH"),
  r2: {
    endpoint: required("R2_ENDPOINT"),
    bucket: required("R2_BUCKET"),
    accessKeyId: required("R2_ACCESS_KEY_ID"),
    secretAccessKey: required("R2_SECRET_ACCESS_KEY"),
    prefix: normalizePrefix(process.env.R2_REMOTE_PREFIX || ""),
  },
  syncPassword: required("SYNC_PASSWORD"),
  ignoreExtra: (process.env.IGNORE_EXTRA || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean),
  stateFilePath: path.join(process.cwd(), ".sync-state.json"),
};
