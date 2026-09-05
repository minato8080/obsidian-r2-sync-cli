# Node.js版 設計書

## 役割

デスクトップでVaultとCloudflare R2を双方向同期する。R2のS3互換APIとRemotely Save互換暗号はnpm依存を利用し、配布が必要な場合だけ単一CJSバンドルを生成する。

共有する外部形式と安全原則は[DESIGN.md](DESIGN.md)を参照する。

## ファイル構成

```text
src/
├── index.js         # CLI entrypoint
├── config.js        # .env読込とVaultパス解決
├── crypto.js        # rclone-crypt互換ラッパー
├── s3.js            # R2 list/get/put/delete
├── ignore.js        # 同期対象外判定
├── localFiles.js    # Vault走査
├── state.js         # checkpoint読書き
├── concurrency.js   # 並列実行
└── sync.js          # 3-way差分計算と適用

tests/node/
├── test-ignore.mjs
└── test-sync.mjs
```

## 暗号化

`@fyears/rclone-crypt`を`base64` filename encryption、空salt指定で利用する。固定default saltによりRemotely Saveと同じ鍵導出・ファイル名暗号化になる。内容暗号化は毎回random nonceを使うため、同一平文でもETagは変化し得る。

`@fyears/rclone-crypt@0.0.7`がCJSから読み込めるよう、依存する`base32768`はCJS版`2.0.2`へ固定する。

## 同期判定

`localSet ∪ remoteSet ∪ prevState.keys`の各Vault相対パスを比較する。

| 前回 | local | remote | action |
|---|---|---|---|
| なし | あり | なし | PUSH |
| なし | なし | あり | PULL |
| なし | あり | あり | 内容一致ならSEED、不一致ならmtimeが新しい側 |
| あり | 消滅 | 消滅 | FORGET |
| あり | 消滅 | 未変更 | DELETE_REMOTE |
| あり | 消滅 | 変更 | PULL |
| あり | 未変更 | 消滅 | DELETE_LOCAL |
| あり | 変更 | 消滅 | PUSH |
| あり | 変更 | 変更 | 内容一致ならSEED、不一致ならmtimeが新しい側 |
| あり | 変更 | 未変更 | hash一致ならSEED、それ以外はPUSH |
| あり | 未変更 | 変更 | 内容一致ならSEED、それ以外はPULL |
| あり | 未変更 | 未変更 | NOOP |

local変更はmtime/size、remote変更はListObjectsV2のETagで判定する。両側変更かつ内容不一致では、local mtimeとremote metadataのmtimeを比較し、新しい側を採用する。mtimeが不足する場合はremoteを優先する。

NOOPは集計だけに含め、適用・checkpoint更新・適用進捗から外す。SEEDは外部データを変更せず、状態だけを更新する。PUSH/PULL/SEED後はlocal content hashをcheckpointへ保存する。

初回に両側へ存在するファイル、remoteだけの再upload、両側変更では、転送前に復号済み内容を比較して同一内容の誤検出をSEEDへ落とす。

## 除外規則

`src/ignore.js`は`.git/`、`node_modules/`、`.DS_Store`、`Thumbs.db`を既定除外する。環境固有の除外は`.env`の`IGNORE_EXTRA`で指定する。

`IGNORE_EXTRA`は設定ディレクトリ基準のgitignore風globである。先頭`/`は基準直下、slashを含むpatternは基準からの相対、slashを含まないpatternは全階層に一致する。`*`と`?`はslashをまたがず、`**`は複数階層をまたぐ。`./path`は`/path`と同じ扱いを維持する。

暗号化されずに置かれるlegacy metadata objectは、filename復号に失敗するためremote同期対象から除外される。

## 適用と安全性

- `--apply`なしではVault、R2、checkpointを変更しない。
- DELETE_LOCAL/DELETE_REMOTEは`--allow-delete`指定時だけ実行する。
- 計画をaction別に表示してから適用する。
- checkpointはNode.js版専用の`.sync-state.json`とする。

## テスト

`npm test`は`tests/node/`のテストを実行する。実R2へ接続せず、in-memory remoteと一時VaultでPUSH/PULL/NOOP、delete guard、初回SEED、両側変更を確認する。

## 実行と配布

```powershell
node src/index.js
node src/index.js --apply
node src/index.js --apply --allow-delete
```

`npm run build`は`src/index.js`と依存を`dist/r2-sync.bundle.cjs`へCJS形式でbundleする。`.env`と`.sync-state.json`は実行時current directory基準であり、`dist/`はversion管理しない。

## 関連

- [DESIGN.md](DESIGN.md)
- [REQUIREMENTS.md](REQUIREMENTS.md)
