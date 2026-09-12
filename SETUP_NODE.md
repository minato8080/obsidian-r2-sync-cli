# Node.js版セットアップ

## 前提

- Node.js 18以上
- Cloudflare R2またはS3互換ストレージ
- Remotely Saveの`rclone-base64`暗号化パスワード

設定形式と初回実行の共通ルールは[SETUP.md](SETUP.md)を参照する。

## 1. 依存パッケージのインストール

```powershell
cd <project-root>
npm install
```

## 2. 設定JSONの作成

```powershell
copy config.example.json config.json
```

`config.json`へ接続情報、`vaultPath`、`statePath`、`password`、`mode`を記入する。`config.json`は公開リポジトリへコミットしない。

実行時に別の設定ファイルを指定する場合は`--config`を使う。省略時は実行時カレントディレクトリの`config.json`を読む。

## 3. テスト

実R2へ接続せず、暗号化・同期ロジックを検証する。

```powershell
npm test
```

## 4. dry-run

```powershell
npm run sync
```

Vault、R2、stateは変更されない。action別の計画と対象パスを確認する。

## 5. 適用

削除を伴わない変更を適用する。

```powershell
npm run sync:apply
```

問題がないことを数回確認してから、削除も含めて適用する。

```powershell
npm run sync:full
```

`npm run sync:full`は`--apply --allow-delete`相当である。削除を実行したくない場合は`sync:apply`を使う。

## 6. 単体バンドル（任意）

`node_modules`なしで配置する場合は、依存込みのCJSバンドルを作成する。

```powershell
npm run build
node dist/r2-sync.bundle.cjs
node dist/r2-sync.bundle.cjs --apply
```

設定JSONはバンドルと同じディレクトリに置くか、`--config <config-path>`で指定する。

## 実行時の注意

- Node.js版のPUSHは固定8並列で実行する。
- Vault書き込み、MERGE、削除は逐次実行する。
- Node.js版とPython版は共通のプロセスロックを使わないため、同一Vaultを同時に実行しない。
- 両側変更時の判定や除外規則は[DESIGN_NODE.md](DESIGN_NODE.md)を参照する。
