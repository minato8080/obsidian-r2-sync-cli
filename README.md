# obsidian-r2-sync-cli

[Obsidian](https://obsidian.md/) プラグイン [Remotely Save](https://github.com/remotely-save/remotely-save) の `rclone-base64` 暗号化方式と互換のファイル形式で、**Obsidianを起動していない状態でも** Node.js から直接 Cloudflare R2 (S3互換API) と双方向同期するCLIツールです。

## これは何のためのツールか

Remotely Save は便利ですが、同期はObsidianアプリが起動している間しか動きません。バックグラウンドで定期的に同期したい、あるいはCIやサーバーレスな環境からvaultをバックアップ/復元したい、といった用途にはObsidian自体の起動が前提になってしまいます。

obsidian-r2-sync-cli は、Remotely Save が使う暗号化フォーマット(`rclone-base64`、[`@fyears/rclone-crypt`](https://github.com/fyears/rclone-crypt) 互換)でファイル名・内容を暗号化してR2に読み書きするため、**同じバケットに対してObsidian側のRemotely SaveとNode側のr2-syncを両方使っても問題なく共有できます**。どちらか一方だけを使う必要はありません。

## 特徴

- Remotely Save (`rclone-base64`方式) と暗号化フォーマット互換。同じパスワードで同じバケットを読み書きできる
- 独自の3-way差分同期（前回状態を`config.json`の`statePath`で管理。Obsidian側の内部状態には依存しない）
- 双方向: ローカル→リモート(PUSH)、リモート→ローカル(PULL)、削除の伝播
- 既定は**dry-run**。実際の変更には `--apply` が必要、削除はさらに `--allow-delete` が必要
- 両側変更時はmtimeが新しい方でそのまま上書き（バックアップコピーは作らない。git等で別途バージョン管理している運用を想定）
- `esbuild` で依存込みの単一ファイル(`dist/*.bundle.cjs`)にビルド可能。`node_modules` なしでどこでも実行できる

## 動作要件

- Node.js 18以上
- Cloudflare R2（または他のS3互換ストレージ）のバケットとAPIトークン
- Remotely Save の暗号化パスワード（Obsidian側の設定画面で確認。encryption methodが `rclone-base64` であること）

## クイックスタート

```powershell
git clone <このリポジトリのURL>
cd r2-sync
npm install
copy config.example.json config.json
# config.json を編集: 接続情報、vaultPath、password、mode を設定

npm test          # 暗号化・同期ロジックの自己テスト(R2に接続しない)
npm run sync      # config.jsonを使ったdry-run
npm run sync:apply       # 削除以外を実際に適用
npm run sync:full        # 削除も含めて完全に適用
```

詳しい手順は [`SETUP.md`](./SETUP.md)、[`SETUP_NODE.md`](./SETUP_NODE.md)、[`SETUP_PYTHON.md`](./SETUP_PYTHON.md)、共通設計は [`DESIGN.md`](./DESIGN.md)、実装別の詳細は [`DESIGN_NODE.md`](./DESIGN_NODE.md) と [`DESIGN_PYTHON.md`](./DESIGN_PYTHON.md) を参照してください。

性能測定の条件・対象断面・結果は [`PERFORMANCE.md`](./PERFORMANCE.md) に記録しています。

## 安全設計

- 初めて実行する既存バケットに対しては、ローカル・リモート両方に既にあるファイルの内容を実際に比較してから同期計画を立てる（無条件に上書き・コンフリクト扱いにしない）
- 削除は既定で実行されない（`--allow-delete` が必要。それまでは警告ログのみ）
- 何が起きるかは常に `dry-run` で事前確認できる

## ライセンス・互換性についての注意

Remotely Save は `src/` (Apache-2.0) と `pro/`（[PolyForm Strict License](https://polyformproject.org/licenses/strict/1.0.0/)、同期アルゴリズム本体はこちら）のデュアルライセンスです。本ツールは `pro/` のコードやアルゴリズムを一切参照・移植せず、暗号化フォーマットの互換性のみをApache-2.0な範囲・公開ドキュメント・一般的な3-way同期の設計パターンをもとに独自実装しています。

このツール自体は [MIT License](./LICENSE) です。

## 免責事項

暗号化資産（vaultの中身）を扱うツールです。必ず `dry-run` で計画を確認してから `--apply` を実行し、重要なvaultは別途バックアップ（gitなど）を取った上でご利用ください。
