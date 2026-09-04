# セットアップ手順

## 1. 依存パッケージのインストール

```powershell
cd C:\path\to\r2-sync
npm install
```

## 2. `.env` の作成

```powershell
copy .env.example .env
```

`.env` を開いて以下を埋める。

| 変数 | 説明 |
|---|---|
| `VAULT_PATH` | 同期するvaultの絶対パス（`<vault-path>`） |
| `R2_ENDPOINT` | `https://<account-id>.r2.cloudflarestorage.com` 形式 |
| `R2_BUCKET` | Remotely Save側の設定と同じバケット名 |
| `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` | R2のAPIトークン |
| `R2_REMOTE_PREFIX` | Remotely Save側で「Remote Prefix」を設定していればそれと同じ値。未設定なら空でよい |
| `SYNC_PASSWORD` | Obsidianの Remotely Save 設定画面にある暗号化パスワード（encryption method が `rclone-base64` であることを確認） |

Remotely Saveの設定値はObsidianの `設定 → Remotely Save` から確認できる（`.obsidian/plugins/remotely-save/data.json` は暗号化されていて直接は読めない）。

## 3. 動作確認（暗号化・同期ロジックの自己テスト、R2に接続しない）

```powershell
npm test
```

全件 `OK` になることを確認する。

## 4. 実行結果に出てくる用語

`npm run sync` を実行すると、ファイルごとに以下のいずれかの種別で計画・結果が表示される。

| 種別 | 意味 |
|---|---|
| `PUSH` | ローカル→リモートへアップロード |
| `PULL` | リモート→ローカルへダウンロード |
| `NOOP` | 前回と変化なし。何もしない |
| `SEED` | 状態ファイルが空の初回実行時、ローカル・リモート両方に既に存在し**内容が一致**したファイル。転送はせず「同期済み」として状態記録だけする（既にRemotely Saveで運用中のバケットに初めて向けた場合に大量に出るのが正常） |
| `FORGET` | 前回は存在したが、今はローカル・リモート両方から消えている。状態から記録を消すだけ |
| `DELETE_REMOTE` / `DELETE_LOCAL` | リモート/ローカルからの削除。`--allow-delete` が無いと実行されない |
| `SKIPPED_DELETE_REMOTE` / `SKIPPED_DELETE_LOCAL` | 削除が計画されたが `--allow-delete` が無いためスキップ（警告のみ、次回また同じ計画が出る） |

`PUSH`/`PULL`には`reason`が付くことがある（例:「両側変更・ローカルの方が新しいため上書き」「リモート削除だがローカルは編集済み: 編集を優先」）。両側変更時の判定方法は`DESIGN.md`の「同期アルゴリズム」参照。

`SEED`ばかりでなく大量の`PUSH`/`PULL`が「不一致による上書き」reason付きで出る場合は `SYNC_PASSWORD` や `R2_REMOTE_PREFIX` の設定が間違っている可能性が高い（暗号化キーが合わずファイル名/内容が正しく復号できていない）。**この場合は `--apply` を実行しないこと。**

## 5. 初回実行は必ずdry-runから

```powershell
npm run sync
```

- 実際には何も変更されない。上記の種別ごとに計画件数とパス一覧が表示されるだけ。
- `Obsidianを開いて手動でRemotely Save同期を1回走らせてから`実行すると、ローカルとリモートの状態が揃っているのでdry-runの結果を検証しやすい。

## 6. 問題なければ適用（削除を伴わない範囲）

```powershell
npm run sync:apply
```

DELETE系のアクションは既定では実行されず、警告ログのみ出る。

## 7. 削除も含めて完全に適用

数回 `sync:apply` を実行して挙動に問題がないことを確認してから:

```powershell
npm run sync:full
```

## 8. (任意) node_modules無しで動く単体バンドルを作る

Discord bot連携など、`npm install` 済みの `node_modules` を気にせず1ファイルだけ配置して動かしたい場合:

```powershell
npm run build
node dist/r2-sync.bundle.cjs               # dry-run
node dist/r2-sync.bundle.cjs --apply
```

`.env` はプロジェクト直下（実行時のカレントディレクトリ）から読まれるので、`dist/r2-sync.bundle.cjs` をプロジェクト外に持ち出す場合は `.env` と `.sync-state.json` も同じディレクトリに置くこと。詳細は `DESIGN.md` の「単体実行用バンドル」参照。

## iOS full sync

iOS版は `ios/sync.py` と設定JSON・状態JSONをVault内の専用フォルダへ配置できる。状態JSONは設定JSONと同じフォルダを基準に相対指定でき、設定JSON・状態JSON・状態更新用一時ファイルは同期処理が自動的に同期対象外として扱う。旧版でCWD基準の相対`statePath`に既存stateがある場合はその場所を互換利用し、新規指定は設定JSON基準になる。設定JSONには必ずプレースホルダーを置き換えた実値を利用者自身で記入する。設定JSONの例は `DESIGN.md` を参照する。

Shortcuts の「Run a-Shell script」または a-Shell In App から、次の形で実行する。

```text
python3 <ios-script-path>/sync.py --config <vault-path>/r2-sync-tools/r2-sync-config.json
python3 <ios-script-path>/sync.py --config <vault-path>/r2-sync-tools/r2-sync-config.json --apply
```

開発環境でVaultとR2を現行同等に全実行する場合は、設定JSONに`"mode": "full"`を指定する。最初の実行はdry-runでVault走査、R2一覧、PULL/PUSH/delete/merge対象の検証だけを行い、結果JSONをShortcutsの通知で確認する。問題がなければ`--apply`を付ける。PUSHとmergeは`--apply`で実行され、削除だけは`--allow-delete`を追加する。

```text
python3 <ios-script-path>/sync.py --config <vault-path>/r2-sync-tools/r2-sync-config.json
python3 <ios-script-path>/sync.py --config <vault-path>/r2-sync-tools/r2-sync-config.json --apply
python3 <ios-script-path>/sync.py --config <vault-path>/r2-sync-tools/r2-sync-config.json --allow-delete --apply
```

full syncは全走査・全列挙を行う。`--apply`でPUSH/PULL/自動mergeを実行し、`--allow-delete`を追加するとremote/local削除も実行する。設定JSONの`mode`を省略するか`probe`にすると、`files`に指定した1〜2個だけを対象にする既存PULL Probe互換モードになる。PUSHには書き込み権限、削除には削除権限を持つテスト用R2キーを使い、実R2へ向ける前にテストデータで確認する。

実行中はNode版と同じ形式で、Vault・モード、走査件数、action別計画と対象パス、取得・検証／適用進捗、最終結果がa-Shellへ表示される。人向け進捗は標準エラー、Shortcutsが受け取る整形済み最終JSONは標準出力へ分離して出力される。JSONは複数行だが、Shortcuts側では通常どおりJSONとして解析できる。

stateが添付ファイルのmerge baseで大きくなる場合は、設定へ`"textMergeBaseMaxBytes": 1048576`を追加すると、1MiB以下のUTF-8テキストだけbaseを保持する。画像・PDF・大容量テキストなどbaseを保持しないファイルが両側変更された場合は、上書きせず競合停止する。未指定では従来どおり全内容を保持する。

適用直前のR2再一覧は既定で有効である。同期中に他クライアントがR2を変更しないことを手動運用で保証できる場合に限り、`"recheckRemoteBeforeApply": false`で省略できる。省略時は競合更新を直前検出できないため、通常は`true`のまま使う。

Python版のR2取得並列数は`"fetchConcurrency": 2`（1〜16）、1リクエストのtimeoutは`"requestTimeoutSeconds": 30`（1〜300秒）で設定できる。PC上で通信が止まる場合は、まず`fetchConcurrency`を1、`requestTimeoutSeconds`を10程度に下げて切り分ける。`python`と`python3`のコマンド名の違い自体は原因ではなく、Python 3.9以上であれば同じ実装を実行する。

取得・検証中のCtrl+Cは未開始リクエストを取り消し、workerの終了待ちをせずCLIを終了する。この段階ではVault、R2、stateをまだ変更していない。これらの設定と中断処理はPython版だけに適用し、Node版は従来どおり固定8並列のままとする。

## 実行のたびに確認すること

- 両側で変更されたUTF-8テキストは、stateにmerge baseがあれば3-way mergeする。同じ行の変更、baseなし、またはバイナリでは全体を競合停止し、どちらの内容も上書きしない。
- `.git/`, `node_modules/`, `.DS_Store`, `Thumbs.db` に加え、本ツールが使用中の設定・stateは自動保護される。それ以外の秘匿フォルダやツール配置先など、除外したいパスは自分で `.env` の `IGNORE_EXTRA` に指定すること（`.env.example` に実例あり、`DESIGN.md`の「除外ルール」も参照）。指定を忘れると同期される。
- ignoreパターンは設定ファイルのあるディレクトリを基準にしたGitignore風glob。専用フォルダに実行ファイルと設定をまとめた場合は`/**`でその配下をすべて除外できる。`/sync.py`は設定ファイルと同じディレクトリ直下、`sync.py`は配下の全階層に一致する。`./sync.py`は旧設定互換で`/sync.py`と同じ。
