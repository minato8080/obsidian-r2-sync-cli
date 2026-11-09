# Python版セットアップ

## 前提

- Python 3.9以上
- iOSのa-ShellまたはPythonを実行できる環境
- Cloudflare R2またはS3互換ストレージ
- Remotely Saveの`rclone-base64`暗号化パスワード

設定形式と初回実行の共通ルールは[SETUP.md](SETUP.md)を参照する。

## 1. Pythonファイルの配置

`py/sync.py`と`py/r2sync/`を同じ階層で、a-Shellから参照できる場所へ配置する。外部Python packageは必要ない。

```text
<python-script-path>/
├── sync.py
└── r2sync/
```

## 2. 設定JSONの作成

[`py/config.example.json`](py/config.example.json)をコピーし、利用環境の設定JSONを作成する。設定JSONとstate JSONはVault内または実行スクリプトの配置先に置けるが、同期対象から除外される場所を選ぶ。

```text
<vault-path>/r2-sync-tools/
├── r2-sync-config.json
└── r2-sync-state.json
```

設定JSONへ接続情報、`vaultPath`、`statePath`、`password`、`mode`を記入する。実値設定、state、ログ、認証情報は公開リポジトリへ保存しない。

相対`statePath`は設定JSONのあるディレクトリ基準で解決される。

## 3. Probeの実行

`mode`を`probe`にすると、設定の`files`に指定した1〜2個のファイルだけをPULL対象にできる。

```text
python3 <python-script-path>/sync.py --config <config-path>
python3 <python-script-path>/sync.py --config <config-path> --apply
```

## 4. full syncの実行

`mode`を`full`にするとVault全走査とR2全列挙を行う。最初はdry-runで計画を確認する。

```text
python3 <python-script-path>/sync.py --config <config-path>
```

問題がなければPUSH、PULL、mergeを適用する。

```text
python3 <python-script-path>/sync.py --config <config-path> --apply
```

削除も適用する場合は、内容と対象を確認した後に`--allow-delete`を追加する。

```text
python3 <python-script-path>/sync.py --config <config-path> --apply --allow-delete
```

Shortcutsの「Run a-Shell script」またはa-Shell In Appから上記コマンドを実行する。Shortcutsへ渡す最終結果は標準出力のJSON、人向け進捗は標準エラーに分離される。

## 5. 除外結果の確認（任意）

R2へ接続せず、Vault内の除外判定だけを確認できる。

```powershell
just check-ignore <config-path>
just check-ignore-verbose <config-path>
just check-ignore-path <config-path> <vault-relative-path>
```

`check-ignore-path`は除外対象なら`IGNORE`、同期対象なら`INCLUDE`を表示する。`INCLUDE`はPUSH確定を意味しない。

## 6. スクリプトの更新

一度`--self-update`対応版を配置した後は、GitHub Releasesから自分自身を更新できる。同期は行わず、設定JSONも不要。

```text
python3 <python-script-path>/sync.py --version
python3 <python-script-path>/sync.py --self-update --check
python3 <python-script-path>/sync.py --self-update
```

- `--check`は新版の有無だけを表示し、ファイルを変更しない。
- 更新時はzipのSHA-256と中身を検証してから置き換え、直前の版を`<python-script-path>/.r2sync-backup/`へ1世代残す。元に戻す場合は、その中の`sync.py`と`r2sync/`を配置先へ戻す。
- フォーク等から取得する場合は`--update-repo <owner>/<name>`を付ける。
- 配置先をVault内にしている場合は、`.r2sync-backup/`と更新中の一時ディレクトリ`.r2sync-update-*`も`ignoreExtra`で同期対象外にする。
- Git checkout内で実行した場合は、開発用ファイルを上書きしないよう更新を拒否する。
- Shortcutsには同期用とは別に、`--self-update`を実行するショートカットを作ると更新が1タップになる。

### リリース手順（メンテナー向け）

1. `py/r2sync/__init__.py`の`__version__`を上げてコミットする。
2. 同じ版の`vX.Y.Z`タグを作ってpushする。
3. GitHub Actionsがテスト、`r2sync-python-vX.Y.Z.zip`と`SHA256SUMS`の作成、GitHub Releaseの作成を行う。

ローカルで配布物だけを確認する場合は`python tools/build_python_release.py --tag vX.Y.Z --out dist/release`を実行する。

## 実行時の設定

- `fetchConcurrency`はR2取得・検証の並列数（1〜16）。
- `applyConcurrency`はPUSHの並列数（1〜16）。iPhoneでは1、PCではまず4を推奨する。
- `requestTimeoutSeconds`は1リクエストのtimeout（1〜300秒）。
- PUSHだけが並列化され、Vault書き込み、MERGE、削除は逐次実行される。
- Python版は同一Vaultへの重複実行をロックで拒否する。Node.js版や別端末との排他ではないため、複数クライアントの実行時刻は分ける。
- 並列PUSHの一部が失敗した場合は成功分だけcheckpointし、残りは次回実行で再評価する。
- 実行中の進捗は標準エラー、Shortcutsが受け取る最終JSONは標準出力へ出力される。

Unicode表現差、3-way merge、Ctrl+C時の動作、stateの扱いは[DESIGN_PYTHON.md](DESIGN_PYTHON.md)を参照する。
