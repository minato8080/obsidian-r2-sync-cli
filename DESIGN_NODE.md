# Node.js版 設計書

## 役割

デスクトップでVaultとCloudflare R2を双方向同期する。R2のS3互換APIとRemotely Save互換暗号はnpm依存を利用し、配布が必要な場合だけ単一CJSバンドルを生成する。

共有する外部形式と安全原則は[DESIGN.md](DESIGN.md)を参照する。

## ファイル構成

```text
src/
├── index.js         # CLI entrypoint
├── config.js        # config.json読込とパス解決
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

`src/ignore.js`は`.git/`、`node_modules/`、`.DS_Store`、`Thumbs.db`、`state.json`を既定除外する。環境固有の除外は`config.json`の`ignoreExtra`で指定し、Python版と同じくVaultルート基準で評価する。設定ファイル、state、state更新用一時ファイルは自動保護する。

`ignoreExtra`はVaultルート基準のgitignore風globである。先頭`/`はVault直下、slashを含むpatternはVaultからの相対、slashを含まないpatternは全階層に一致する。`*`と`?`はslashをまたがず、`**`は複数階層をまたぐ。`./path`は`/path`と同じ扱いを維持する。

暗号化されずに置かれるlegacy metadata objectは、filename復号に失敗するためremote同期対象から除外される。

## 適用と安全性

- `--apply`なしではVault、R2、checkpointを変更しない。
- DELETE_LOCAL/DELETE_REMOTEは`--allow-delete`指定時だけ実行する。
- 計画をaction別に表示してから適用する。`NOOP`は適用対象・checkpoint更新から除外する。
- checkpointは`config.json`の`statePath`へ保存し、Python版と同じentry形式を使う。merge baseが利用できる場合は`baseContentBase64`も保存する。
- full modeではPULL候補を先に取得・復号・検証し、ローカル再検査とR2 snapshot再確認を終えてから変更する。PUSHは固定8並列、Vault書込み・MERGE・削除は逐次実行する。
- `vaultPath`は実体解決後もVault配下であることを確認し、symlink、junction、reparse point経由のVault外アクセスを拒否する。再確認・適用時にも同じ検査を行う。
- remote filename衝突、無効なremote一覧、またはその他の全体競合は、競合をactionへ変換せず、対象パスまたは全体を適用対象から除外する。
- apply直前はPUSH/PULL/MERGE/DELETE/FORGET全対象の内容hashまたは不在を再確認する。PUSH batchに失敗があれば成功分だけcheckpointし、後続の非PUSH操作は中止する。
- Probeは既存targetのlocal snapshot、設定・state・state tempの保護対象判定を行い、保護対象または競合targetを変更しない。
- 新`statePath`がない状態で旧`.sync-state.json`だけを検出した場合は、旧stateを自動採用せず、移行手順を示してR2接続前に停止する。

## テスト

`npm test`は`tests/node/`のテストを実行する。実R2へ接続せず、in-memory remoteと一時VaultでPUSH/PULL/NOOP、delete guard、初回SEED、両側変更を確認する。

## 実行と配布

```powershell
node src/index.js --config config.json
node src/index.js --config config.json --apply
node src/index.js --config config.json --apply --allow-delete
```

`npm run build`は`src/index.js`と依存を`dist/r2-sync.bundle.cjs`へCJS形式でbundleする。`config.json`は`--config`で指定し、省略時は実行時current directoryの`config.json`を読む。`dist/`はversion管理しない。

## 関連

- [DESIGN.md](DESIGN.md)
- [REQUIREMENTS.md](REQUIREMENTS.md)
