# Python版 設計書

## 役割と実行環境

iOS Shortcutsからa-Shell `In App`のPythonを起動し、VaultとR2を同期する。CPython標準ライブラリだけで動作し、外部packageを必要としない。Probeは明示した1〜2ファイルのPULL、full modeはVault/R2全走査による双方向同期を行う。

共有する外部形式と安全原則は[DESIGN.md](DESIGN.md)を参照する。

## ソース構成

```text
py/
├── sync.py                 # 薄いCLI entrypoint
├── config.example.json     # placeholderだけの設定例
└── r2sync/
    ├── __init__.py         # package共有例外
    ├── cli.py              # CLI解析、表示、依存の組立て
    ├── config.py           # JSON設定の読込・検証・パス解決
    ├── crypto.py           # rclone-base64名前/内容暗号
    ├── local.py            # path安全性、ignore、Vault走査、atomic replace、run lock
    ├── checkpoint.py       # state読書き、merge base管理
    ├── remote.py           # SigV4 R2 client、remote一覧正規化、並列read
    ├── planner.py          # pureなaction分類と3-way text merge
    └── executor.py         # Probe/full orchestration、再検証、適用

tests/python/
├── support.py
├── test_cli.py
├── test_crypto.py
├── test_local.py
└── test_sync.py
```

`python py/sync.py ...`をrepository上の実行形式とする。配布時も`sync.py`と`r2sync/`を同じ階層へ配置する。

## 依存方向

```text
sync.py → cli
cli → config, crypto, local, remote, executor
executor → local, checkpoint, remote, planner
remote → local（path正規化とignore判定）
checkpoint → local（path正規化）
planner → 標準ライブラリのみ
```

`planner`はfilesystem、checkpoint、HTTPへ依存させない。`local`と`checkpoint`もR2へ依存させない。共有例外はpackage rootで定義し、下位module間の循環を避ける。

## 設定とCLI

設定JSONのkey、default、validationは既存形式を維持する。主要項目は`vaultPath`、`statePath`、R2接続情報、`password`、`mode`、`remotePrefix`、`files`、`ignoreExtra`、`fetchConcurrency`、`requestTimeoutSeconds`、`unicodeCollisionPolicy`、`textMergeBaseMaxBytes`、`recheckRemoteBeforeApply`である。

`mode`は必須とし、`full`または`probe`を指定する。相対`statePath`は設定JSONのdirectoryだけを基準とする。

CLI optionは`--apply`、`--allow-delete`、`--check-ignore`、`--verbose`とする。stdoutは最終JSON専用、進捗はstderrへ逐次flushする。

## 暗号化とR2

標準ライブラリでscrypt、AES-EME filename encryption、XSalsa20-Poly1305 content encryptionを実装する。AES key schedule、S-box、GF table、filename変換はbounded cacheで再利用する。

R2 accessはAWS SigV4で署名し、GET、PUT、DELETE、ListObjectsV2 paginationを提供する。request timeoutは`requestTimeoutSeconds`、read-only取得並列数は`fetchConcurrency`を使う。Vault書込みとR2 mutationは逐次実行する。

## パス、Unicode、除外

論理パスはUnicode NFCのVault相対POSIX pathへ統一し、absolute path、空要素、`.`、`..`を拒否する。実ファイル参照は完全一致を優先し、存在しない要素だけNFC同値名を探索する。Vault外へ解決されるpathは拒否する。

`unicodeCollisionPolicy=error`では、NFC正規化後にlocal、remote、stateの複数候補が同名になる場合に停止する。`prefer-nfc`では正規化前からNFCそのものの候補が一意な場合だけ採用し、aliasは変更・削除しない。除外数は`unicodeAliasesIgnored`へ記録する。

既定除外は`.git/`、`node_modules/`、`.DS_Store`、`Thumbs.db`と、basenameが`state.json`のファイルである。設定JSON、checkpoint、checkpoint更新用temp fileも強制保護する。`ignoreExtra`はVault root基準のgitignore風globで、local scan、復号後remote一覧、旧checkpointのfilterへ同じmatcherを使う。

`--check-ignore [PATH ...]`はR2 client生成前に終了し、Vault/R2/checkpointを変更しない。PATH省略ではIGNORE/INCLUDEを列挙し、通常表示は安全に集約、`--verbose`は全pathを表示する。

remote一覧の復号後、scan結果にないremote pathをVault上で直接再解決する。Files Providerの遅延列挙やUnicode表現差で見つかったregular fileはlocal mapへ補完し、`reconciledLocalFiles`へ記録する。

## checkpoint

JSON形式は`{"version": 1, "updatedAt": ..., "entries": ...}`を維持する。保存はstateと同じdirectoryのtemp fileへwrite/fsyncし、`os.replace`後に可能ならdirectoryもfsyncする。

entryは共有fieldに加え、3-way merge用の`baseContentBase64`を持てる。`textMergeBaseMaxBytes`指定時は上限以下のUTF-8だけを保存し、既存の対象外baseは成功したapply時に除去する。未指定時は従来互換のため制限しない。

## 同期計画

基本action tableはNode.js版と同じだが、Python full modeは両側変更でmergeが有効なとき、checkpoint baseを使ったUTF-8 line単位3-way mergeを先に試す。非重複変更はMERGE、重複変更、binary、baseなしはconflictとして全体を適用前に停止する。

計画処理は次の段階に分ける。

1. local scan、remote list、checkpoint load
2. pureな存在/mtime/size/ETag比較でactionまたは内容取得candidateを分類
3. candidateだけを並列GET・復号し、内容一致、mtime、3-way mergeで最終actionへ解決
4. local全対象を内容hashで再検証
5. remote snapshotを再listして計画時identityと比較
6. mutationを1件ずつ適用し、再開境界ごとにcheckpoint

NOOPは`unchanged`だけへ集計し、適用対象、事前再検証、checkpoint、適用進捗から除外する。SEEDは外部mutationを伴わないためmemory上でまとめ、次のcheckpointまたはloop終了時に保存する。

## mutationと失敗時の境界

- dry-runは取得・検証まで行えるが、Vault、R2、checkpointを変更しない。
- 全candidateの取得・復号・競合判定を完了してから最初のmutationを行う。
- PULLは同一directoryのtemp fileをfsyncし、mtimeを設定してから`os.replace`する。
- PUSH/DELETE_REMOTEはR2成功後、PULL/DELETE_LOCALはlocal成功後にcheckpointする。
- MERGEはlocalをatomic replaceしてからR2へPUTする。PUT失敗時はcheckpointせず、次回に変更済みlocalを再計画する。
- delete actionは計画されても`--allow-delete`なしではskipし、対象を変更しない。
- 適用途中の失敗では、それ以前に成功した操作のcheckpointを保持して再開可能にする。
- `recheckRemoteBeforeApply=false`の明示時だけremote snapshot再確認を省略し、stderr警告と結果flagへ記録する。

## 同時実行と中断

正規化したVault絶対pathのSHA-256から一時領域のlock file名を作り、非待機OS lockを取得する。Windowsは`msvcrt.locking`、Unix/iOSは`fcntl.flock`を使う。lock競合時はR2 client生成前に終了し、`--check-ignore`はlock対象外とする。

並列取得中のCtrl+Cでは未開始futureをcancelし、worker終了待ちをせずCLIを終了する。この段階はmutation前なのでVault、R2、checkpointは変更済みにならない。

## 進捗と計測

Vault/mode、local/remote/state件数、action別計画、取得・適用進捗、最終集計をstderrへ出す。action pathはtypeごとに20件まで表示する。最終JSONだけをstdoutへ出す。

`timingsMs`は`scanLocal`、`listRemote`、`decodeRemote`、`reconcileLocal`、`conflictCheck`、`fetchValidate`、`snapshotCheck`、`applyRemote`、`applyCheckpoint`、`total`を必要に応じて返す。

内部結果の`ignoredRemoteObjects`は診断用に保持するが、stdout用copyではroutine ignore detailを省略し、総数・省略数と、復号不能など先頭20件の詳細だけを返す。

## テスト

Pythonテストは実R2へ接続せず、fake remoteと一時Vaultを使う。最低限、pure plannerのPUSH/PULL/MERGE/DELETE分類、dry-runの非mutation、delete guard、checkpoint保護、Unicode衝突、暗号vector、SigV4/List XML、atomic replace、fetch失敗時の非mutation、CLI stdout/stderr分離を確認する。

```powershell
python -m unittest discover -s tests/python -t .
```

## 配置例

公開リポジトリにはplaceholderだけの`py/config.example.json`を置く。利用者は`py/sync.py`と`py/r2sync/`をa-Shellの実行領域へ配置し、Shortcuts bookmarkから参照する。実値設定とcheckpointは利用者側で管理し、同期対象から保護する。

```text
<python-script-path>/
├── sync.py
└── r2sync/

<vault-path>/r2-sync-tools/
├── r2-sync-config.json
└── r2-sync-state.json
```

## 関連

- [DESIGN.md](DESIGN.md)
- [REQUIREMENTS.md](REQUIREMENTS.md)
