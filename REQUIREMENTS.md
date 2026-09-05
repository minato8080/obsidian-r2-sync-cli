# obsidian-r2-sync-cli 要件定義

## 目的

Obsidianが起動していない状態でも、VaultとCloudflare R2を同期できる独立CLIを提供する。デスクトップ版はNode.jsで双方向同期し、iOS版はiOSショートカットからa-ShellのPythonを起動して、Remotely Saveのファイル適用経路を避けた同期を検証する。

## プロジェクト運用

- 本書と設計書は本リポジトリのルートに置く
- ソースコード、テスト、設定テンプレートも本PJで管理する
- 利用者環境のVault内にある生成済みバンドル配置先は、要件・設計・ソースの管理場所にしない
- 本リポジトリの実装変更後、必要な場合だけ `npm run build` でバンドルを生成し、利用者が指定する配布先へ配置する
- 実値の `.env`、アクセスキー、パスワード、個人用パスはコミット・バンドル・Vaultへ含めない

## デスクトップ版の要件

- Node.jsからR2のS3互換APIへ直接アクセスする
- Remotely Saveのrclone-base64暗号化形式と互換にする
- VaultとR2を全走査し、独自の3-way比較でPUSH／PULL／NOOP／削除を判定する
- `.sync-state.json`で本PJ自身の前回状態を管理する
- デフォルトはdry-runとし、実行系・削除系には明示フラグを要求する
- `NOOP`は変更なし件数として表示するだけで、適用処理・checkpoint更新・適用進捗には含めない
- `npm test`で偽リモートと一時Vaultを使った同期判定・適用を検証できる

## iOS版の要件

### 対象

- 実行経路は iOSショートカット → a-Shell `In App` → Python
- R2とiPhone間のfull同期。PULL、明示許可されたPUSH／削除／mergeを含む
- ローカルVaultとR2の双方を全走査する
- 現行PC版とR2オブジェクト形式、暗号化、ファイル名規則、差分判定を互換にする
- Vaultは利用者が指定するローカルファイル領域を対象にする。特定のVault名や個人環境を前提にしない
- 現行の同期対象・除外設定を踏襲する

### iOSの実行オプション

- `--apply`がない実行はdry-run。付けるとPUSH／PULL／mergeを実行する
- リモート／ローカル削除だけは`--allow-delete`が必要
- iOSバックグラウンド実行の保証
- 全走査・全量取得に対する5秒保証

### 安全性

- 同期状態は設定ファイルと同じVault内フォルダへ置ける。ログは結果出力として扱い、Vault内へ自動保存しない
- 状態形式はPC版と互換にするが、端末ごとの状態ファイルは共有しない
- 取得物は一時ファイルへ保存し、検証後に原子的置換を行う
- R2のmtimeをローカルへ復元する
- 競合が1件でもあれば、ファイル適用前に同期全体を中止する
- 取得・書き込みの失敗時は既存ファイルを壊さない
- 初回または状態消失時はDry Runで差分確認してから適用する。両側に存在するファイルは内容一致ならSEED、不一致ならmtime判定または自動mergeを行う
- テストR2バケットと読み取り専用キーを使用し、本番データへ直接Probeしない

### 性能

- Probeは1〜2個の小さなMarkdown、full PULLは実Vaultのファイル数・サイズを別基準として測定する
- T1はショートカット起動からVaultへの直接ファイル置換完了までとし、中央値5秒以内を目標にする
- 同一条件を10回測定し、95パーセンタイル10秒以内を目安にする
- Obsidianが外部変更を認識するまでのT2は別計測とする
- iOS/Python版のR2取得・内容確認は`fetchConcurrency`で1〜16並列を指定でき、既定値は2とする。Vault書き込みとR2変更は1並列を維持する
- iOS/Python版のR2リクエストtimeoutは`requestTimeoutSeconds`で1〜300秒を指定でき、既定値は30秒とする
- PC版の並列数8は変更しない
- 定常時の全件NOOP判定ではファイル内容を再読込せず、走査で取得したmtime/sizeを前回stateと比較する
- `timingsMs`は全体の`scan`に加えて、`scanLocal`、`listRemote`、`decodeRemote`を個別に返す
- NOOPパスは実際にファイルへアクセスする分岐まで絶対パス解決を遅延し、定常時の差分判定でファイルシステムへ再アクセスしない
- `timingsMs`は適用前R2再確認の`snapshotCheck`、R2変更の`applyRemote`、state保存の`applyCheckpoint`も個別に返す

## 受入基準

- 1ファイルのPULL後、内容・UTF-8・パス・mtime・次回NOOP判定が一致する
- 日本語、空白、ドットファイル、深い階層を扱える
- 取得途中や置換直後の停止から安全に再開できる
- 認証失敗、通信断、429、5xxで既存ファイルを変更しない
- 現行PC版とiOS版で同じR2データを相互に読める
- 暗号化有効時、固定テストベクトルで復号結果が一致する

## 段階導入

1. R2なしで直接ファイル置換とT1／T2計測
2. テストR2から1ファイルを取得するProbe
3. 全走査・状態管理・Dry Run・チェックポイントを追加（iOS full sync）
4. 暗号化互換と失敗系を検証（継続）
5. 複数ファイル、PUSH、削除、競合処理を必要性に応じて拡張

### iOS full sync の初回実装範囲

- `ios/sync.py` は、Shortcuts から a-Shell の Python を呼び出すための依存パッケージなしの実行点とする
- `mode: "full"` ではVaultを再帰走査し、R2をListObjectsV2で全列挙して、現行PC版のstate形式を使った同期計画を作る
- R2取得・内容確認は`fetchConcurrency`の指定数、Vault書き込みとR2変更は1並列で実行する。全候補の取得・復号・競合判定を終えてから最初の変更を行う
- Python版の`ignoreExtra`はVaultルートからの相対パス（区切りは`/`）へ適用するGitignore風globの配列とする。`foo/bar/**`はVault直下の対象ディレクトリと配下、`**/.env`はVault全階層の`.env`、`.git`のようにスラッシュを含まないパターンは全階層の同名要素に一致する。ローカル走査と復号後のR2一覧へ同じ判定を適用する。Node版の`IGNORE_EXTRA`は既存のconfigディレクトリ基準を維持する
- PUSHはrclone-base64のファイル名・内容暗号化とmtime metadata付きPUTを行う。削除は`--allow-delete`指定時だけR2またはVaultへ反映する
- 初回にローカルとリモートの両方にあるファイルは内容を比較し、一致時だけ`SEED`としてstateへ記録する。不一致はmtime判定または自動mergeに従う
- stateがあるファイルはlocalMtimeMs、localSize、localContentHash、baseContentBase64とremoteETagで変更を判定する。PULL対象のlocal変更とremote変更が同時ならmergeまたはmtime判定を行う
- リモート一覧から消えたオブジェクトは、削除許可がない場合は保持し、許可時だけローカル削除またはPUSHで処理する
- PUSH・PULL・merge対象のローカル内容を変更開始前に再検査し、R2一覧も再取得して当初のremote snapshotと比較する。競合が1件でもあれば変更を開始しない
- 成功した操作ごとに、設定ファイル基準で指定した状態ファイルを一時ファイル経由で更新し、次回再開できる checkpoint とする。Vault内にある設定ファイル、状態ファイル、状態更新用一時ファイルは、Vault走査・R2一覧から強制除外して同期しない
- Python版はbasenameが`state.json`のファイルも設定に関係なくVault全階層で強制除外し、ローカル・R2・旧stateのいずれからも同期計画へ入れない
- Python版は`--check-ignore [PATH ...]`で、R2へ接続せずに設定済み除外ルールの判定結果を確認できる。PATH省略時はVaultをローカル走査し、除外対象を`IGNORE`、除外されないローカルファイルを`INCLUDE`として両方とも標準出力へ表示する。通常表示では、ディレクトリ全体の除外だけを配下全体としてまとめ、`**/.env`、個別ファイル指定、`state.json`などのファイル除外は各ファイルパスを表示する。`INCLUDE`は除外対象を含まないサブツリーを可能な限りVaultルート側のフォルダ単位にまとめ、除外対象と混在するフォルダだけ下位へ展開する。件数と集約注記は英語で表示する。`--verbose`指定時は除外ディレクトリ配下も走査して全パスを表示する。`INCLUDE`はR2未照合の同期対象候補であり、PUSH確定を意味しない。PATH指定時は各Vault相対パスを個別判定し、全件除外なら終了コード0、同期対象が1件でもあれば1を返す。この診断ではVault・R2・stateを変更しない
- `--apply` がない実行は取得・検証だけを行い、Vaultと状態を変更しない。結果はShortcutsが受け取れるJSONで標準出力へ出す
- Node版と同じく、Vault・実行モード、local/remote/state件数、action別の計画と対象パス、取得・検証／適用件数、最終集計、競合・エラーを実行中に表示する。Shortcuts向けJSONを壊さないよう、人向け進捗は標準エラーへ逐次出力し、最終JSONだけを標準出力へ整形して出す。整形JSONは複数行だが、標準のJSONパーサーでそのまま受け取れる
- `NOOP`は変更なし件数として表示するだけで、適用計画・適用前再検証・checkpoint更新・適用進捗には含めない。全件`NOOP`の実行結果は`planned=0`かつ`applied=0`とする
- `SEED`は外部データを変更しないため、複数件のstate更新を一括保存する。PUSH・PULL・削除・mergeは成功ごとのcheckpointを維持する
- `textMergeBaseMaxBytes`は任意の非負整数とし、指定時はUTF-8テキストかつ指定バイト数以下の内容だけを3-way merge用baseとしてstateへ保存する。対象外ファイルが両側変更された場合は自動上書きせず競合停止する。未指定時は互換のため従来どおり全内容を保存する
- `recheckRemoteBeforeApply`は真偽値とし、既定値は`true`とする。明示的に`false`を指定した場合だけ適用直前のR2再一覧を省略し、警告と結果JSONのフラグで安全確認を省略したことを示す
- Python版の並列取得中にCtrl+Cを受けた場合は、未開始タスクを取り消してworker待機をせず終了する。取得・検証段階ではVault、R2、stateを変更しない
- Python版の同期実行は、正規化したVaultパス単位の非待機OSロックをR2接続前に取得し、処理終了まで保持する。同一端末・同一利用者から同じVaultへの別実行が進行中なら、待機せずエラー結果と終了コード1を返し、Vault、R2、stateを変更しない。ロックは正常終了、例外、Ctrl+C、プロセス終了時に解放され、ロック用ファイルはVault外の一時領域へ置く。読み取り専用の`--check-ignore`はロック対象外とする
- Node版および別端末の同期処理はPython版のOSロックへ参加しないため、端末間の排他は保証しない。Python版は既存の適用前R2 snapshot再確認により、計画後から適用前までに別クライアントが行ったremote変更を競合として停止する
- Node版は既存の固定8並列を維持し、`fetchConcurrency`と`requestTimeoutSeconds`の対象外とする
- stateに保存した前回共通内容をbaseとしてUTF-8テキストを3-way mergeする。baseがない旧stateやバイナリの衝突は自動解決せず全体を中止する
- 既存の`files`を使う1〜2ファイル明示モードはPULL専用のProbeとして互換維持する

## 関連

- 設計: [DESIGN.md](DESIGN.md)
- 調査知識: 利用者固有の調査結果は本リポジトリ外で管理する
