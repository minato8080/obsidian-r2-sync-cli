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
- 処理後、実際に存在する状態を `.sync-state.json` に書き戻す

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

それ以外（秘匿フォルダ、このツール自身の配置先、Obsidianの端末固有UI状態ファイルなど）は全て `.env` の `IGNORE_EXTRA` で利用者が指定する（`.env.example` に記法と実例あり）。

- **`_remotely-save-metadata-on-remote.json` / `.bin`（レガシーファイル）を個別にハードコード除外する必要はない**: このファイル名はリモート上では暗号化されない生のファイル名で保存されるため、`planSync`が`cipher.decryptPath()`を試みた時点で復号エラーになり、既存の`try/catch`で自動的にスキップされる（`src/sync.js`）。ローカルに同名ファイルが実在する可能性はほぼ無視できるため、特別扱いは不要。
- **接続情報が絡むファイル(`.obsidian/plugins/remotely-save/data.json`等)や秘匿フォルダ(`private/`等)を同期対象から外したい場合は、`IGNORE_EXTRA`に明示的に追加すること**。ツール側は安全側のデフォルトを持たないので、これを怠ると同期される（暗号化はされるが、資格情報が絡むファイルを同期に含めたくない場合は必ず設定する）。

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

PULL実行ではVault内に設定・状態・ログを置かない。設定JSONと状態JSONはShortcutsまたはa-Shellが利用できるアプリ領域など、Vault外の利用者管理領域に置く。

```text
ios/
├── sync.py                 # 標準ライブラリだけで動くiOS同期実行点
└── test_sync.py            # R2を使わない安全性・暗号のテスト

<ios-app-data>/
├── r2-sync-config.json     # 実値は利用者が作成。リポジトリには含めない
└── r2-sync-state.json      # 端末専用状態。Vault外に自動生成
```

設定JSONの例は次のようにプレースホルダーだけで記載する。アクセスキー、パスワード、実際のVaultパスはリポジトリへ保存しない。

```json
{
  "vaultPath": "<vault-path>",
  "statePath": "<ios-app-data>/r2-sync-state.json",
  "endpoint": "https://<account-id>.r2.cloudflarestorage.com",
  "bucket": "<bucket-name>",
  "accessKeyId": "<access-key-id>",
  "secretAccessKey": "<secret-access-key>",
  "password": "<sync-password>",
  "mode": "full",
  "remotePrefix": "",
  "ignoreExtra": []
}
```

`mode`は`full`または`probe`を指定する。fullではVaultを再帰走査し、`remotePrefix`配下をR2 ListObjectsV2で全列挙する。fullの`--apply`はPUSH／PULL／自動mergeを実行し、削除だけは`--allow-delete`を追加指定した場合に実行する。`ignoreExtra`はデスクトップ版の追加除外パターンと同じ簡易形式である。`mode`を省略した既存設定はProbe互換のため`files`に明示した1〜2ファイルを対象にする。Probeでは`files[].key`を指定でき、省略時は`remotePrefix + rclone-base64で暗号化した相対パス`を使用する。

`sync.py`はGETとListObjectsV2の署名にAWS SigV4を使い、`rclone-base64`のscrypt／AES-EMEによるファイル名復号と、RCLONEヘッダー・24バイトnonce・64KiB単位のXSalsa20-Poly1305による内容復号を標準ライブラリで行う。ProbeのMarkdownはUTF-8として検証し、full PULLでは復号済みバイト列をそのまま検証済みデータとして扱う。検証済みバイト列だけを同一ディレクトリの一時ファイルへ書き、`os.replace`で置換する。mtimeは一時ファイルへ設定してから置換するため、既存ファイルは取得・復号・検証・一時書き込みの失敗で変更されない。

適用前に全対象のローカル競合を検査する。競合または取得・復号エラーがある場合、VaultまたはR2への変更は開始しない。適用中に書き込みが失敗した場合は既に成功した対象を状態へcheckpointし、次回は状態とローカル内容を再検査して再開する。PUSHとmerge後のR2 PUT成功後にローカル置換が失敗するような異種システム間の中断は、次回のstate／ETag再検査で収束させる。結果JSONには`ok`、`mode`、`planned`、`applied`、`errors`、`conflicts`、各処理段階の経過時間を含め、Shortcutsの通知へ渡せるようにする。

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
- iOSの状態ファイルはVault外に置く。PC版のプロジェクトルートにある状態ファイルとは端末別に分離する
- 状態形式は `{ localMtimeMs, localSize, remoteETag, localContentHash }` を基本にPC版と互換にする
- 状態がない場合は、両側に存在するファイルの内容一致を`SEED`、不一致を競合として扱い、Dry Runを先に実行する
- リモート一覧から消えたオブジェクトは`--allow-delete`がなければ保持し、指定時はlocal変更の有無に応じてPUSHまたはDELETE_LOCALを計画する
- stateへ`baseContentBase64`を保存し、3-way mergeの共通祖先として利用する。旧stateにbaseがなければ自動mergeせず競合とする
- 1ファイルの置換成功ごとに状態を一時ファイル経由で更新する
- 状態更新に失敗した場合は同期を失敗扱いにし、次回に再確認する

### iOS版の並列性

- R2確認・取得は最大2並列
- 復号は取得結果ごとに行う
- Vaultへの書き込みは初期値1並列
- 1並列／2並列でVault書き込み時間を比較し、書き込みが支配的かを計測で判断する
- デスクトップ版の並列数8は変更しない

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
