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

`PUSH`/`PULL`には`reason`が付くことがある（例:「両側変更・ローカルの方が新しいため上書き」「リモート削除だがローカルは編集済み: 編集を優先」）。両側変更時の判定方法は`DESIGN_NODE.md`の「同期判定」参照。

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

`.env` はプロジェクト直下（実行時のカレントディレクトリ）から読まれるので、`dist/r2-sync.bundle.cjs` をプロジェクト外に持ち出す場合は `.env` と `.sync-state.json` も同じディレクトリに置くこと。詳細は `DESIGN_NODE.md` の「実行と配布」参照。

## iOS full sync

Python版は `py/sync.py`、`py/r2sync/`、設定JSON・状態JSONをa-Shellから参照できる場所へ配置する。相対`statePath`は設定JSONと同じフォルダを基準とし、設定JSON・状態JSON・状態更新用一時ファイルは同期処理が自動的に同期対象外として扱う。設定JSONには必ずプレースホルダーを置き換えた実値を利用者自身で記入する。配置例は `DESIGN_PYTHON.md` を参照する。

Shortcuts の「Run a-Shell script」または a-Shell In App から、次の形で実行する。

```text
python3 <python-script-path>/sync.py --config <vault-path>/r2-sync-tools/r2-sync-config.json
python3 <python-script-path>/sync.py --config <vault-path>/r2-sync-tools/r2-sync-config.json --apply
```

開発環境でVaultとR2を現行同等に全実行する場合は、設定JSONに`"mode": "full"`を指定する。最初の実行はdry-runでVault走査、R2一覧、PULL/PUSH/delete/merge対象の検証だけを行い、結果JSONをShortcutsの通知で確認する。問題がなければ`--apply`を付ける。PUSHとmergeは`--apply`で実行され、削除だけは`--allow-delete`を追加する。

```text
python3 <python-script-path>/sync.py --config <vault-path>/r2-sync-tools/r2-sync-config.json
python3 <python-script-path>/sync.py --config <vault-path>/r2-sync-tools/r2-sync-config.json --apply
python3 <python-script-path>/sync.py --config <vault-path>/r2-sync-tools/r2-sync-config.json --allow-delete --apply
```

R2へ接続せず、ローカルの除外結果だけを確認するには次を実行する。1つ目はVault内の`IGNORE`と`INCLUDE`（同期対象候補）の両一覧、2つ目は指定したVault相対パス1件の判定を標準出力へ表示する。R2未照合のため`INCLUDE`はPUSH確定を意味しない。

```powershell
just check-ignore <config-path>
just check-ignore-verbose <config-path>
just check-ignore-path <config-path> <vault-relative-path>
```

通常の`check-ignore`はディレクトリ全体の除外をまとめ、`INCLUDE`は除外対象を含まない最上位のサブツリー単位でまとめる。除外対象と混在するフォルダだけ下位へ展開し、`.env`など個別に除外されたファイルはパスを表示する。件数と集約注記は英語で表示する。`check-ignore-verbose`は除外ディレクトリ配下を含む全パスを表示する。`check-ignore-path`は指定パスが除外対象なら`IGNORE`と終了コード0、同期対象なら`INCLUDE`と終了コード1を返す。

full syncは全走査・全列挙を行う。`--apply`でPUSH/PULL/自動mergeを実行し、`--allow-delete`を追加するとremote/local削除も実行する。設定JSONの`mode`は必須で、`probe`では`files`に指定した1〜2個だけをPULL対象にする。PUSHには書き込み権限、削除には削除権限を持つテスト用R2キーを使い、実R2へ向ける前にテストデータで確認する。

同じ端末・同じ利用者から同一Vaultへ`sync.py`を重ねて起動した場合、後から起動した処理はR2へ接続せず、`another sync is already running for this vault`エラーで即時終了する。ロック待機はせず、強制終了後も次回実行を妨げない。Node版や別端末との排他ロックではないため、複数の同期クライアントを使う場合は従来どおり実行タイミングを分ける。

実行中はNode版と同じ形式で、Vault・モード、走査件数、action別計画と対象パス、取得・検証／適用進捗、最終結果がa-Shellへ表示される。対象パスはNOOP、PUSH、PULLなどのaction種別ごとに20件まで表示し、残りは省略件数だけを表示する。人向け進捗は標準エラー、Shortcutsが受け取る整形済み最終JSONは標準出力へ分離して出力される。JSONは複数行だが、Shortcuts側では通常どおりJSONとして解析できる。

除外ルールへ一致したremote objectは、人向け結果の`ignoredRemoteObjects`件数と最終JSONの総数・省略数だけで確認する。通常の除外パスは最終JSONへ1件ずつ展開しない。ファイル名を復号できないobjectなど調査が必要な詳細だけを最大20件表示する。

日本語の濁点・半濁点などを含むファイル名は、iOSと他OSでUnicode NFC／NFD表現が異なる場合がある。Python版は論理パスをNFCへ統一し、ローカル走査から漏れたremoteパスも計画前に直接再解決する。再解決した既存ファイル数は`reconciledLocalFiles`として表示され、新規PULLではなく内容比較の対象になる。正規化後に同名となる複数ファイルが実在する場合は、既定では安全のため停止する。

Unicode正規化後に同名となる複数候補があり、手動整理せず同期を続ける必要がある場合は、設定へ`"unicodeCollisionPolicy": "prefer-nfc"`を追加する。一意なNFC表記だけを同期対象とし、NFDなどのaliasは削除せず残したまま同期対象外にする。除外数は`unicodeAliasesIgnored`へ表示される。一意なNFC表記がない衝突は停止する。既定の`"error"`は従来どおりすべての衝突で停止する。

stateが添付ファイルのmerge baseで大きくなる場合は、設定へ`"textMergeBaseMaxBytes": 1048576`を追加すると、1MiB以下のUTF-8テキストだけbaseを保持する。画像・PDF・大容量テキストなどbaseを保持しないファイルが両側変更された場合は、上書きせず競合停止する。未指定では従来どおり全内容を保持する。

適用直前のR2再一覧は既定で有効である。同期中に他クライアントがR2を変更しないことを手動運用で保証できる場合に限り、`"recheckRemoteBeforeApply": false`で省略できる。省略時は競合更新を直前検出できないため、通常は`true`のまま使う。

Python版のR2取得並列数は`"fetchConcurrency": 2`（1〜16）、1リクエストのtimeoutは`"requestTimeoutSeconds": 30`（1〜300秒）で設定できる。PC上で通信が止まる場合は、まず`fetchConcurrency`を1、`requestTimeoutSeconds`を10程度に下げて切り分ける。`python`と`python3`のコマンド名の違い自体は原因ではなく、Python 3.9以上であれば同じ実装を実行する。

取得・検証中のCtrl+Cは未開始リクエストを取り消し、workerの終了待ちをせずCLIを終了する。この段階ではVault、R2、stateをまだ変更していない。これらの設定と中断処理はPython版だけに適用し、Node版は従来どおり固定8並列のままとする。

## 実行のたびに確認すること

- 両側で変更されたUTF-8テキストは、stateにmerge baseがあれば3-way mergeする。同じ行の変更、baseなし、またはバイナリでは全体を競合停止し、どちらの内容も上書きしない。
- `.git/`, `node_modules/`, `.DS_Store`, `Thumbs.db` に加え、本ツールが使用中の設定・stateは自動保護される。それ以外の秘匿フォルダやツール配置先など、除外したいパスは設定へ明示すること（Node.js版は`.env.example`、Python版は`py/config.example.json`と各設計書を参照）。指定を忘れると同期される。
- ignoreパターンは設定ファイルのあるディレクトリを基準にしたGitignore風glob。専用フォルダに実行ファイルと設定をまとめた場合は`/**`でその配下をすべて除外できる。`/sync.py`は設定ファイルと同じディレクトリ直下、`sync.py`は配下の全階層に一致する。`./sync.py`は旧設定互換で`/sync.py`と同じ。
