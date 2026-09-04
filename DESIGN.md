# obsidian-r2-sync-cli 設計書

## 目的

Obsidianが起動していない状態でも、vault（別リポジトリで管理）の内容をCloudflare R2へ双方向同期する。ObsidianのUIにキー送信するだけのスクリプトはObsidianプロセスが無いと機能しない。本スクリプトはNode.jsから直接R2 (S3互換API) を叩き、Remotely Save プラグイン (`.obsidian/plugins/remotely-save`) と暗号化フォーマット互換のファイルを読み書きすることで、Obsidian側からもNode側からも同じリモートを問題なく共有できるようにする。

このツール自体はvaultのリポジトリとは別（例: `C:\path\to\r2-sync`）でソース管理する想定。vault側からは絶対パス（`.env`の`VAULT_PATH`）で参照するだけで、vaultリポジトリには一切ファイルを置かない。

## 前提・制約

- Remotely Save のコア同期ロジック (`pro/src/sync.ts`) は **PolyForm Strict License**（ソース公開のみ、改変・再配布不可）。閲覧して概念を理解する目的のみに使い、アルゴリズムはそのまま移植していない。同期判定は本書の独自設計（3-way比較、Unison/Syncthing等でも使われる一般的な手法）。
- Remotely Save の `_remotely-save-metadata-on-remote.json` / `.bin` は **現行バージョン(v0.5.25)では実質デッドコード**（`main.ts` からも `pro/src/sync.ts` からも読み書きされていない。リモートから除外対象としてのファイル名比較にのみ使われる）。本スクリプトもこのファイル名を見かけたら読み書きせず無視する。
- 「前回の同期状態」はObsidian本体はローカルDB（IndexedDB、V3同期アルゴリズム）で管理しており、Node側から読めない。そのため **本スクリプトは自分専用の同期状態ファイル (`.sync-state.json`) を別途持つ**。Obsidian本体の同期履歴とは独立した、もう1台の同期クライアントとして振る舞う。

## ファイル構成

```
r2-sync/                    ← 例: C:\path\to\r2-sync （vaultとは別リポジトリ）
├── DESIGN.md          ← 本書
├── SETUP.md            ← セットアップ手順
├── package.json
├── .env.example         ← 接続情報のテンプレート（実値は .env に自分で入力、gitignore対象）
├── .sync-state.json     ← 前回同期状態（自動生成、gitignore対象）
└── src/
    ├── index.js         ← CLIエントリーポイント
    ├── config.js         ← .env読み込み・vaultパス解決（VAULT_PATHで指定した絶対パスを指す）
    ├── crypto.js         ← rclone-crypt互換の暗号化/復号ラッパー
    ├── s3.js              ← R2クライアント（list/get/put/delete）
    ├── ignore.js          ← 同期対象外パスの判定
    ├── localFiles.js      ← vault内ファイル一覧の取得
    ├── state.js           ← 前回同期状態の読み書き
    └── sync.js            ← 3-way差分計算と実行
```

## 暗号化仕様（Remotely Save の `rclone-base64` 方式と互換）

`@fyears/rclone-crypt` (MIT, npm公開パッケージ) を使用。Remotely Save 本体の `src/encryptRClone.ts` と同じ呼び方をする。

```js
const cipher = new Cipher("base64"); // fileNameEnc = "base64" 固定（READMEの"base32"例ではない）
await cipher.key(password, "");      // salt引数は常に空文字 → rclone-crypt標準の固定defaultSaltが使われる
```

- **ファイル名**: `cipher.encryptFileName(relativePath)` — パスを `/` で分割し、各セグメントを決定的暗号化（EME/AES）。同じ平文は常に同じ暗号文になるため、Obsidian側が書き込んだファイルもNode側から同じキーで参照できる。
- **ファイル内容**: `cipher.encryptData(bytes)` — `RCLONE\0\0` マジック + 24byteランダムnonce + 64KiBブロック単位の xsalsa20poly1305 (secretbox)。ランダムnonce前提なので同じ内容でも暗号文は毎回変わる（差分検知にはS3のETagやローカルmtime/sizeを使い、暗号文そのものは比較しない）。
- 暗号化パスワードは `.env` の `SYNC_PASSWORD`。Remotely Save側の設定と同じ値を入れる必要がある（Obsidianの設定画面で確認）。
- `@fyears/rclone-crypt@0.0.7` は依存の `base32768@3.0.1` がESM専用で、CJSビルドの `dist/index.js` から `require()` すると `ERR_REQUIRE_ESM` で落ちる（base32768エンコードを使わなくても、モジュール先頭で無条件requireされるため回避不可）。`package.json` の `overrides` で `base32768` をCJS版の `2.0.2` に固定して回避している。

## リモートレイアウト

- バケット内のキー = `R2_REMOTE_PREFIX`（任意、末尾`/`）+ `cipher.encryptFileName(relativePath)`
- アップロード時にカスタムメタデータ `mtime`（ローカルの元mtime、epoch秒・小数可）を付与する。ダウンロード時はこの値でローカルファイルのmtimeを復元し（`fs.utimes`）、次回実行時に「ダウンロードしただけなのに変更扱いされる」誤検知を防ぐ。
- 空フォルダは同期しない（ファイルが無いフォルダはR2上にオブジェクトとして存在しないため、そもそも表現できない。Remotely Save本体も `generateFolderObject:false` がデフォルトで同じ考え方）。

## 同期アルゴリズム（独自3-way比較）

前回同期状態 `.sync-state.json` の各パスごとに `{ localMtimeMs, localSize, remoteETag, localContentHash }` を保持する。毎回の実行で以下を計算する。

1. `localSet` = vault内の現在のファイル一覧（ignoreルール適用後）
2. `remoteSet` = R2の現在のオブジェクト一覧をfilename復号したもの（`ignore.js`のパターンに一致するキー・legacyメタデータファイル名は無視）
3. `prevState` = `.sync-state.json`

`localSet ∪ remoteSet ∪ prevState.keys` の全パスについて判定：

| 前回情報 | ローカル | リモート | 判定 |
|---|---|---|---|
| なし | あり | なし | PUSH（新規アップロード） |
| なし | なし | あり | PULL（新規ダウンロード） |
| なし | あり | あり | 内容(復号後バイト列)が**一致するならSEED**（転送せず状態記録のみ＝既存の同期済みバケットに対する初回実行を想定）。**不一致ならmtimeが新しい方で上書き**（PUSHまたはPULL、下記参照） |
| あり | 消えた | 消えた | 状態から削除（何もしない） |
| あり | 消えた | 変化なし | DELETE_REMOTE |
| あり | 消えた | 変化あり | **編集が削除に勝つ** → PULL（リモートの新しい内容でローカルを復元） |
| あり | 変化なし | 消えた | DELETE_LOCAL |
| あり | 変化あり | 消えた | **編集が削除に勝つ** → PUSH（ローカルの内容でリモートを復元） |
| あり | 変化あり | 変化あり | 内容が**一致するならSEED**（転送スキップ）。**不一致ならmtimeが新しい方で上書き**（PUSHまたはPULL） |
| あり | 変化あり | 変化なし | 前回の`localContentHash`と**一致するならSEED**（touchのみ）。**不一致ならPUSH** |
| あり | 変化なし | 変化あり | 内容が**一致するならSEED**（別クライアントの再アップロードのみ）。**不一致ならPULL** |
| あり | 変化なし | 変化なし | 何もしない |

- ローカルの「変化」判定: mtime(ms)またはsizeが前回と異なるか
- リモートの「変化」判定: S3の ETag が前回と異なるか（`ListObjectsV2` の結果をそのまま使う。多くのノートは単純PUTなのでETag=MD5として信頼できる）
- **両側変更時の方針**: PC側はgitで管理しながらの運用なので、コンフリクトコピー(`<name> (conflict ...)`)は作らない。本家Remotely Save同様、単純にmtimeが新しい方をそのまま勝たせて上書きする（ローカルmtime vs リモートの`Metadata.mtime`、`GetObjectCommand`のレスポンスから軽量に取得。どちらのmtimeも無ければリモート優先）。上書きされた側の変更を復元したい場合はgit履歴から戻す想定。
- `NOOP`は変更なしとして集計するだけで、Node版・iOS版とも適用処理、checkpoint更新、適用進捗の対象から除外する
- 処理後、実際に存在する状態を `.sync-state.json` に書き戻す

iOS版の定常実行では、Vault走査で得たmtime/sizeだけでローカル変更の有無を判定し、mtime/sizeが変わったファイルに限って内容ハッシュを確認する。rclone暗号化ファイル名のAES-EME処理は、AES鍵スケジュールとMixColumns用GF乗算表を再利用し、オブジェクトごとの同一計算を避ける。全体の`scan`時間は`scanLocal`、`listRemote`、`decodeRemote`にも分けて記録する。

NOOPのパスは同期計画中に絶対パスへ解決せず、ファイル内容の読取りまたは適用が必要な分岐で初めて`_vault_path`を呼ぶ。適用時間はR2 PUT/DELETEの`applyRemote`とstate永続化の`applyCheckpoint`へ分け、適用前R2再一覧・snapshot比較は`snapshotCheck`として記録する。

SEEDはVault/R2を変更しないため、entryをメモリ上で更新し、次の変更操作のcheckpointまたはループ終了時にまとめてstateへ保存する。PUSH・PULL・DELETE・mergeは従来どおり成功直後にcheckpointし、中断復帰の境界を変えない。

`textMergeBaseMaxBytes`を指定した場合、`baseContentBase64`はUTF-8として復号可能で上限以下の内容にだけ保存する。既存stateの対象外baseも、成功したapply実行時に一度だけ除去する。baseがないファイルの両側変更は安全側に倒して競合とし、自動mergeまたはmtime上書きを行わない。未指定時はstate互換のため制限しない。推奨初期値は1MiB (`1048576`) とする。

`recheckRemoteBeforeApply`は既定`true`で、最初の変更直前にR2を再一覧して計画時snapshotと比較する。手動実行で外部更新が発生しないことを利用者が保証できる場合のみ`false`を指定できる。この場合は再一覧を省略するが、stderrへ警告し、結果JSONの`remoteSnapshotRechecked`を`false`にする。

### 本家Remotely Save（Obsidianプラグイン）との共存と誤検知防止

本スクリプトの`.sync-state.json`は自分専用の前回状態であり、Obsidian本体のRemotely SaveがPUSH/PULLしても更新されない。そのため本家プラグインとこのCLIを交互に使うと、以下の理由で「実際には中身が変わっていないのに変化ありと誤検知」しやすい:

- Remotely SaveがPULLでローカルファイルを書き換えると、内容が同じでもmtimeは変わる → `localChanged`が誤って立つ。
- 暗号化は`encryptData`が毎回ランダムnonceを使う仕様（前述）のため、**同じ平文を再アップロードしてもS3のETagは毎回変わる**。Remotely SaveがPUSHすると、内容が同じでも`remoteChanged`が誤って立つ。
- 上記2つが同時に起きると「両側変更」判定（コンフリクト扱い）にもなり得るが、実体は同一内容の単なる二重反映であることが多い。

これを防ぐため、`localChanged`/`remoteChanged`/両側変更のいずれの分岐でも、**転送を実行する前に実際の内容一致を確認する**（初回実行時の`bootstrapCandidates`と同じ考え方を、状態ファイルがある通常フローにも適用）:

- `localChanged`のみ: 前回記録した`localContentHash`（sha256、`fs.readFile`だけで済みネットワーク不要）と現在のローカル内容のハッシュを比較。一致すれば「touchされただけ」と判断しPUSHせずSEED相当（状態だけ更新）。前回ハッシュが無い（旧状態ファイルからの移行直後など）場合のみ、安全側に倒して従来通りPUSHする。
- `remoteChanged`のみ: リモートをダウンロード＆復号し、ローカル内容と比較。一致すればPULLせずSEED相当。不一致なら通常通りPULL。
- 両側変更（コンフリクト）: 同様にリモートをダウンロード＆復号して内容比較を先に行う。一致すればSEED相当（転送なし）。不一致の場合のみ、従来通りmtime比較でPUSH/PULLの勝者を決める。

いずれの場合も、PUSH/PULL/SEEDの結果としてローカル内容のsha256を`.sync-state.json`に書き戻す（PUSH/PULLは転送のためどのみち読んでいるバイト列からハッシュを計算するだけなので追加コストはほぼゼロ、SEEDは判定時に読んだバイト列のハッシュをそのまま流用する）。
- **初回実行の注意**: `.sync-state.json` が空の状態で、既にRemotely Saveで運用中のバケットに向けて実行すると、ローカル・リモート両方に存在する全ファイルについて内容比較（ダウンロード＆復号）が走る。並列8件・進捗ログ付きで処理する（`planSync`内、`bootstrapCandidates`）。数百〜千件規模だと初回のみ数分かかることがあるが、2回目以降は状態ファイルがあるので通常の軽量な差分比較（mtime/size/ETag比較のみ）に戻る。

## 除外ルール（`src/ignore.js`）

`ignore.js` にハードコードするのは「どんなvaultでも汎用的に除外すべきもの」だけに限定し、vault固有・配置固有の判断は一切持たせない（特定ユーザーの環境を前提にした"マジックワード"をツール本体に埋め込まないため）。

ハードコードされた既定除外は以下のみ:

```
.git/, node_modules/   ← VCS/依存パッケージの内部ファイル。業界標準的に除外されるもので、vault固有の話ではない
.DS_Store, Thumbs.db   ← OSが生成するゴミファイル
```

Python版は上記に加えて、basenameが`state.json`のファイルをVault全階層で常時除外する。設定した`statePath`そのものとcheckpoint一時ファイルを保護する既存処理も維持する。ローカル走査、復号後のR2一覧、旧stateのフィルタで同じ判定を使うため、`ignoreExtra`に記載がなくても同期計画へ入らない。Node版の除外ルールは変更しない。

`sync.py --config <config> --check-ignore`は同じmatcherでVaultを読み取り専用走査し、除外するファイルとディレクトリを表示してR2クライアント生成前に終了する。`--check-ignore <path>...`では指定したVault相対パスだけを判定する。リポジトリの`justfile`は一覧用`check-ignore`と個別判定用`check-ignore-path`を提供する。

Node版の`.env`と同期state、iOS版の設定JSON・同期state・state更新用一時ファイルは、Vault内にある場合も実装が自動的に除外する。それ以外（秘匿フォルダ、このツール自身の配置先、Obsidianの端末固有UI状態ファイルなど）は `.env` の `IGNORE_EXTRA` で利用者が指定する（`.env.example` に記法と実例あり）。

Node版の`IGNORE_EXTRA`は、設定ファイルのあるディレクトリを基準にしたGitignore風globとして解釈する。先頭`/`を付けると基準ディレクトリ直下に固定し、`foo/bar`のようにスラッシュを含むパターンも基準ディレクトリからの相対パスになる。`sync.py`のようにスラッシュを含まないパターンは基準ディレクトリ以下の全階層に一致する。末尾`/`はディレクトリと配下、`*`と`?`は`/`をまたがず、`**`は複数階層をまたぐ。`./path`は旧設定互換で`/path`と同じ扱いにする。

Python版の`ignoreExtra`はVaultルートからの相対パスへ適用するGitignore風globの配列として解釈する。対象パスの区切りはOSにかかわらず`/`とする。`*`と`?`は`/`をまたがず、`**`は複数階層をまたぐ。スラッシュを含むパターンはVaultルート基準、スラッシュを含まないパターンは全階層の同名要素に一致する。末尾`/`はディレクトリと配下を表し、`foo/bar/**`は`foo/bar`ディレクトリ自体も含めて除外する。ディレクトリへの一致は走査を打ち切る。ローカル走査、復号後のR2一覧、旧stateのフィルタへ同じmatcherを適用する。

- **`_remotely-save-metadata-on-remote.json` / `.bin`（レガシーファイル）を個別にハードコード除外する必要はない**: このファイル名はリモート上では暗号化されない生のファイル名で保存されるため、`planSync`が`cipher.decryptPath()`を試みた時点で復号エラーになり、既存の`try/catch`で自動的にスキップされる（`src/sync.js`）。ローカルに同名ファイルが実在する可能性はほぼ無視できるため、特別扱いは不要。
- **上記の自動保護対象以外で接続情報が絡むファイル(`.obsidian/plugins/remotely-save/data.json`等)や秘匿フォルダ(`private/`等)を同期対象から外したい場合は、`IGNORE_EXTRA`に明示的に追加すること**。指定しないファイルは同期されるため、資格情報が絡むファイルを同期に含めたくない場合は必ず設定する。

## 安全機構

- デフォルトは **dry-run**。実際にR2へ書き込む/ローカルへ書き込むには `--apply` が必要。
- 削除操作（DELETE_REMOTE/DELETE_LOCAL）はさらに `--allow-delete` が無いと実行されず、代わりに警告ログのみ出す。
- 実行のたびに計画（PUSH/PULL/DELETE/CONFLICT件数と対象パス一覧）を標準出力に表示する。

## テスト

`test-sync.mjs` は実際のR2を使わず、インメモリの偽リモートと一時ディレクトリ上の偽vaultに対して `planSync`/`applyActions` を通しで検証する（PUSH/PULL/NOOP/削除のスキップと実行/両側変更コンフリクトのリネーム保護と後続PUSHまで）。本番のR2バケットに向ける前の変更検証に使う。

```powershell
npm test
```

## 実行方法

`SETUP.md` 参照。基本形:

```powershell
cd C:\path\to\r2-sync
node src/index.js               # dry-run（何が起きるか確認のみ）
node src/index.js --apply        # 削除以外を実行
node src/index.js --apply --allow-delete   # 削除も含めて完全実行
```

## 単体実行用バンドル

`npm run build` で `esbuild` を使い `src/` 一式と依存パッケージ（`@aws-sdk/*`, `@fyears/rclone-crypt`, `dotenv`）を `dist/r2-sync.bundle.cjs` 1ファイル（`--minify`で約630KB）に固める。`node_modules` なしで動く。

- 出力は **CJS形式**（`--format=cjs`）。ESM形式だと `dotenv` など内部で `require()` を使うCJS依存が `Dynamic require of "fs" is not supported` で落ちるため（esbuildの既知の制約）。
- `.env` / `.sync-state.json` はカレントディレクトリ基準で解決される（`src/config.js`参照）。バンドルファイルをどこに配置しても、実行時にそのディレクトリへ `cd` して `.env` を隣に置けば動く。
- `dist/` はビルド成果物なのでgit管理対象外。使う端末で都度 `npm run build` するか、生成物だけ配布する。

```powershell
npm run build
# プロジェクト直下(.envのある場所)から実行するのが簡単
node dist/r2-sync.bundle.cjs --apply

# バンドルファイル単体を別の場所に持ち出す場合は .env / .sync-state.json をその隣に置く
```

## iOS版の実証設計

### 配置と運用境界

iOS用Pythonは本リポジトリからa-ShellのDocuments等へデプロイし、Shortcutsは利用者が設定したブックマークでその実行先を参照する。

```text
r2-sync/
├── src/                    # デスクトップNode.js版
├── ios/                    # iOS用Python版（a-Shell向け、full sync／Probe）
├── REQUIREMENTS.md         # プロジェクト要件
└── DESIGN.md               # デスクトップ＋iOS設計

<consumer-vault>/generated/
└── r2-sync.bundle.cjs      # 利用者が必要に応じて配置する生成物
```

#### iOS PULL のファイル構成

PULL実行では、設定JSONと状態JSONをVault内の専用ディレクトリへ置いて管理できる。設定JSON、状態JSON、状態更新用一時ファイルは同期処理が自動的に保護し、Vault走査・R2一覧のどちらからも同期対象外にする。公開リポジトリには実値を置かず、設定例はプレースホルダーだけにする。

```text
ios/
├── sync.py                 # 標準ライブラリだけで動くiOS同期実行点
└── test_sync.py            # R2を使わない安全性・暗号のテスト

<vault-path>/r2-sync-tools/
├── r2-sync-config.json     # 実値は利用者が作成。リポジトリには含めない
└── r2-sync-state.json      # 同期処理が自動的に同期対象外として扱う
```

設定JSONの例は次のようにプレースホルダーだけで記載する。アクセスキー、パスワード、実際のVaultパスはリポジトリへ保存しない。

```json
{
  "vaultPath": "<vault-path>",
  "statePath": "<vault-path>/r2-sync-tools/r2-sync-state.json",
  "endpoint": "https://<account-id>.r2.cloudflarestorage.com",
  "bucket": "<bucket-name>",
  "accessKeyId": "<access-key-id>",
  "secretAccessKey": "<secret-access-key>",
  "password": "<sync-password>",
  "mode": "full",
  "remotePrefix": "",
  "ignoreExtra": [
    "r2-sync-tools/**",
    "**/.env"
  ]
}
```

`mode`は`full`または`probe`を指定する。fullではVaultを再帰走査し、`remotePrefix`配下をR2 ListObjectsV2で全列挙する。fullの`--apply`はPUSH／PULL／自動mergeを実行し、削除だけは`--allow-delete`を追加指定した場合に実行する。`ignoreExtra`はVaultルート基準のGitignore風glob配列である。`mode`を省略した既存設定はProbe互換のため`files`に明示した1〜2ファイルを対象にする。Probeでは`files[].key`を指定でき、省略時は`remotePrefix + rclone-base64で暗号化した相対パス`を使用する。

`sync.py`はGETとListObjectsV2の署名にAWS SigV4を使い、`rclone-base64`のscrypt／AES-EMEによるファイル名復号と、RCLONEヘッダー・24バイトnonce・64KiB単位のXSalsa20-Poly1305による内容復号を標準ライブラリで行う。ProbeのMarkdownはUTF-8として検証し、full PULLでは復号済みバイト列をそのまま検証済みデータとして扱う。検証済みバイト列だけを同一ディレクトリの一時ファイルへ書き、`os.replace`で置換する。mtimeは一時ファイルへ設定してから置換するため、既存ファイルは取得・復号・検証・一時書き込みの失敗で変更されない。

適用前に全対象のローカル競合を検査し、R2一覧も再取得して最初の一覧snapshotと比較する。競合または取得・復号エラーがある場合、VaultまたはR2への変更は開始しない。適用中に書き込みが失敗した場合は既に成功した対象を状態へcheckpointし、次回は状態とローカル内容を再検査して再開する。mergeはローカルを原子的に置換してからR2へPUTするため、ローカル置換失敗時にremoteだけが更新されることはない。後続のR2 PUTが失敗した場合、stateは更新せず、次回に変更済みローカル内容を再計画する。

Node版と同じく、開始時にVaultとDRY-RUN/APPLYモード、走査後にlocal/remote/state件数、計画確定後にaction種別・件数・対象パス（最大50件）、処理中に取得・検証件数と適用件数、終了時にaction別集計・競合・エラーを表示する。人向け表示は標準エラーへ逐次flushし、機械処理用の最終JSONだけを標準出力へ整形して出す。Python版の整形JSONは複数行だが、Shortcutsは標準JSONとして受け取れる。Node版の処理・出力は変更しない。結果JSONには`ok`、`mode`、`planned`、`applied`、`errors`、`conflicts`、各処理段階の経過時間を含める。

差分判定で得た`NOOP`は`unchanged`として集計・表示するが、実行対象のactionリストから分離する。したがって`NOOP`はローカル再検証、remote snapshot再取得、state checkpoint、適用進捗の分母には入らず、全件`NOOP`なら走査と判定後に`planned=0`、`applied=0`で終了する。

### 初期実行経路

full syncの実行経路は次の通りである。既存のProbeモードは走査・一覧取得を明示ファイルへ置き換えるが、取得・検証・原子的適用・checkpointの境界は共通である。

```text
iOS Shortcuts
  ↓
a-Shell In App
  ↓
ios/sync.py
  ↓
Vault全走査 + R2全列挙
  ↓
既存PC版と互換の差分判定
  ↓
競合があれば適用前に中止
  ↓
R2取得・復号 → 一時ファイル → 検証 → 原子的置換
  ↓
mtime復元 → 状態チェックポイント更新 → 通知・ログ
```

ProbeはPULL専用で、full同期では明示許可されたPUSH、remote/local削除、mergeを実行する。Obsidianは閉じるか編集停止状態で実行する。Shortcutsの初回だけVaultフォルダを選択し、以後はブックマークを使う。

### iOS版の差分・状態

- ローカルVaultとR2の双方を全走査し、PULLに必要な範囲で現行PC版の3-way比較を再現する
- iOSの状態ファイルは設定ファイル基準で配置でき、Vault内に置く場合も同期対象から強制除外する。PC版のプロジェクトルートにある状態ファイルとは端末別に分離する
- 状態形式は `{ localMtimeMs, localSize, remoteETag, localContentHash }` を基本にPC版と互換にする
- 状態がない場合は、両側に存在するファイルの内容一致を`SEED`、不一致を競合として扱い、Dry Runを先に実行する
- リモート一覧から消えたオブジェクトは`--allow-delete`がなければ保持し、指定時はlocal変更の有無に応じてPUSHまたはDELETE_LOCALを計画する
- stateへ`baseContentBase64`を保存し、3-way mergeの共通祖先として利用する。旧stateにbaseがなければ自動mergeせず競合とする
- 1ファイルの置換成功ごとに状態を一時ファイル経由で更新する
- 状態更新に失敗した場合は同期を失敗扱いにし、次回に再確認する

### iOS版の並列性

- R2確認・取得はconfigの`fetchConcurrency`（1〜16、既定2）を使う
- R2通信は`requestTimeoutSeconds`（1〜300秒、既定30）でtimeoutを設定する
- 復号は取得結果ごとに行う
- Vaultへの書き込みとR2変更は1並列を維持する
- 完了したfutureから処理して進捗を更新する。Ctrl+Cでは未開始futureを取り消し、executorのworker完了を待たずにCLIを終了する。取得・検証は変更前フェーズなので強制終了しても同期対象とstateは変更されない
- デスクトップNode版の固定8並列は変更しない。`fetchConcurrency`と`requestTimeoutSeconds`はPython版だけのconfigであり、Node版の`.env`や処理には影響しない

### iOS版の計測

```text
t0  Shortcuts起動
t1  Python開始
t2  Vault走査完了
t3  R2一覧完了
t4  差分判定完了
t5  R2取得・復号完了
t6  Vault直接置換完了
t7  状態更新完了
t8  Obsidian外部変更認識
```

- T1 = `t0 → t6`
- T2 = `t6 → t8`
- コールドスタートとウォーム実行を分ける
- 1ファイル、2ファイル、10ファイルで比較する
- 小さなMarkdownと大きな添付ファイルを分ける

### 将来拡張

R2一覧取得が支配的だった場合のみ、マニフェストまたは一覧キャッシュを検討する。ローカル適用が支配的だった場合は、書き込み並列度、Obsidianの外部変更検知、差分ZIP方式を個別に検証する。PUSH、削除、競合マージは、full syncの相互運用と中断復帰をテストR2で継続検証する。

## 関連

- 要件定義: [REQUIREMENTS.md](REQUIREMENTS.md)
