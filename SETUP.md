# セットアップ案内

実装ごとのセットアップ手順は次のドキュメントに分けている。

- [Node.js版セットアップ](SETUP_NODE.md)
- [Python版セットアップ](SETUP_PYTHON.md)

## 共通の前提

Node.js版とPython版は同じJSON設定形式、暗号化形式、state entry形式を使用する。設定例は[`config.example.json`](config.example.json)を使い、実際の接続情報を記入した`config.json`は公開リポジトリへ保存しない。

主要な設定項目は次の通り。

| key | 説明 |
|---|---|
| `vaultPath` | 同期するVaultの絶対パス（`<vault-path>`） |
| `statePath` | 同期状態JSONのパス。相対パスは設定JSONのあるディレクトリ基準 |
| `endpoint` | `https://<account-id>.r2.cloudflarestorage.com` 形式 |
| `bucket` | Remotely Save側の設定と同じバケット名 |
| `accessKeyId` / `secretAccessKey` | R2 APIトークン |
| `remotePrefix` | Remotely Save側で設定したRemote Prefix。未設定なら空 |
| `password` | Remotely Saveの暗号化パスワード |
| `mode` | `probe` または `full` |
| `ignoreExtra` | Vaultルート基準の追加除外glob |

Remotely Saveの設定値はObsidianの`設定 → Remotely Save`から確認する。既存バケットへ初めて接続するときは、テスト用R2バケットと専用キーで確認してから本番へ向ける。

## 初回実行の原則

最初は必ずdry-runで実行し、`PUSH`、`PULL`、削除、mergeの計画を確認する。問題がなければ`--apply`を付け、削除はさらに`--allow-delete`を付ける。重要なVaultは別途バックアップを取得する。

実行結果の主なactionは次の通り。

| 種別 | 意味 |
|---|---|
| `PUSH` | ローカルからリモートへアップロード |
| `PULL` | リモートからローカルへダウンロード |
| `NOOP` | 前回と変化なし |
| `SEED` | 両側に存在し内容一致。転送せずstateだけ記録 |
| `FORGET` | 両側から消えたパスをstateから削除 |
| `DELETE_REMOTE` / `DELETE_LOCAL` | リモートまたはローカルから削除 |
| `SKIPPED_DELETE_REMOTE` / `SKIPPED_DELETE_LOCAL` | 削除が計画されたが`--allow-delete`がないためスキップ |

`SEED`ではなく大量の`PUSH`/`PULL`が不一致理由付きで出る場合は、`password`または`remotePrefix`が誤っている可能性が高い。その場合は`--apply`を実行しない。
