# obsidian-r2-sync-cli 共通設計

## 目的

Obsidianが起動していない状態でも、VaultとCloudflare R2を同期する。Node.js版とPython版は独立した同期クライアントとして動作するが、Remotely Saveの`rclone-base64`形式、R2オブジェクト配置、checkpointの主要フィールドを共有する。

## 設計書の分割

- [DESIGN_NODE.md](DESIGN_NODE.md): デスクトップNode.js版の構成、同期判定、実行、配布
- [DESIGN_PYTHON.md](DESIGN_PYTHON.md): iOS/a-Shell向けPython版の構成、同期判定、安全性、実行

実装固有の設定、除外規則、並列性、テストは各設計書で管理し、本書へ混在させない。

## 共有する外部形式

- バケット内のキーは、任意のremote prefixと、Vault相対パスを`rclone-base64`方式で暗号化した名前を連結する。
- 内容は`RCLONE\0\0`ヘッダー、24 byte nonce、64 KiBブロック単位のXSalsa20-Poly1305形式とする。
- PUT時は元のmtimeをS3 custom metadataの`mtime`へepoch秒で保存し、PULL時にローカルmtimeを復元する。
- checkpoint entryは`localMtimeMs`、`localSize`、`remoteETag`、`localContentHash`を基本とする。クライアント間でcheckpointファイル自体は共有しない。
- 空フォルダは同期しない。

## 共有する安全原則

- 既定はdry-runで、変更には`--apply`を必要とする。
- local/remoteの削除には、さらに`--allow-delete`を必要とする。
- 設定、checkpoint、一時ファイル、資格情報は同期対象へ含めない。
- 実R2を使わないテストを標準の回帰テストとする。
- 公開リポジトリには実値の接続情報、個人用パス、Vault名、ログを保存しない。

## 前提・制約

- Remotely SaveのPolyForm Strict License対象コードは移植せず、暗号化形式の互換性と一般的な3-way比較に基づく独自実装とする。
- Remotely Save本体の同期履歴はローカルDBにあり外部CLIから共有できないため、Node.js版とPython版はそれぞれ専用checkpointを持つ。

## 関連

- [REQUIREMENTS.md](REQUIREMENTS.md)
- [SETUP.md](SETUP.md)
