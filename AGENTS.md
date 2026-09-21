# r2-sync プロジェクト運用

## Global Agent Harness

このProjectは`development` Harness Profileを使用する。普遍的な安全、secret保護、Main Agent ownership、context boundary、subagent利用、model escalation、approval、auditabilityは、user-levelのGlobal Invariantsに従い、このProjectからoverrideできない。

通常loopはMain Agentが`understand → investigate → plan → implement → deterministic verification → diagnose/fix → self-review → risk assessment`を継続して担当する。subagentは大規模探索・独立仕様調査・非依存並列・独立reviewに限り、task ownershipをhandoffしない。

このProject固有のverificationは`npm test`、必要時の`npm run build`、公開情報チェック、dry-run結果確認である。`npm run sync:apply`、`npm run sync:full`、R2変更、Vault書き込み、削除、公開はexternal side effectとし、対象・scope・影響を確認して明示承認された場合だけ実行する。`--allow-delete`は特に不可逆操作として扱う。

common safety policyやHarness Coreをこのrepositoryへコピーしない。以下のProject Policyは、公開リポジトリ制約、R2/Vault同期仕様、dry-run、個人情報・secret禁止という差分だけを保持する。

このリポジトリは公開プロジェクトである。公開利用者に一般化できない個人環境の情報や運用は、リポジトリへ持ち込まない。

## 配置の境界

- 要件定義は `REQUIREMENTS.md`、共通設計は `DESIGN.md`、実装別設計は `DESIGN_NODE.md` と `DESIGN_PYTHON.md` に保存する
- Node.js版ソースは `src/`、iOS/a-Shell向けPython版ソースは `py/` に置く
- 利用者環境のVault内にある生成済みバンドル配置先は、正本ではない
- 利用者固有のVaultパス、ショートカット、ログ、設定は利用者側で管理する

## 公開リポジトリの禁止事項

- 個人名、個人用絶対パス、Vault名、端末固有ID、アカウントID、実バケット名、実エンドポイントを記載・コミットしない
- APIキー、パスワード、Cookie、トークン、認証済みレスポンス、生ログを記載・コミットしない
- 個人Vaultのフォルダ構成や、特定ユーザーだけの連携手順を要件・設計・READMEへ記載しない
- 例示には `<project-root>`、`<vault-path>`、`<account-id>`、`<bucket-name>` などのプレースホルダーを使う
- 利用者固有の調査結果・環境制約は、公開リポジトリ外のナレッジへ保存する

## 公開前ハーネスチェック

公開情報チェックは、まずこのハーネスの作業ルールとして実施する。GitHub Actionsによる自動チェックは必須要件ではなく、別途導入が決まった場合だけ追加する。

通常のコミットでは、バージョン管理された `.githooks/pre-commit` も実行する。clone後は `npm run setup-hooks` で有効化する。hookはstaged差分だけをローカル検査し、外部通信を行わない。

コミット前に次を確認する。

1. 変更対象が意図したファイルだけである
2. 個人パス、Vault名、端末固有情報、実バケット情報がない
3. APIキー、パスワード、Cookie、トークン、認証済みレスポンス、生ログがない
4. 実値の`config.json`、同期状態ファイル、個人用設定がステージされていない
5. ドキュメントの例がプレースホルダーになっている
6. 仕様・設計・コードの変更内容が公開利用者にも一般化できる

pre-commit hookは上記のうち機械的に判定できる項目と `npm test` を検査する。ハーネス側ではhookの結果だけに依存せず、公開内容全体を確認する。`--no-verify` による回避は行わない。

1つでも判断できない項目がある場合は、コミットを止めて確認する。

## 変更手順

1. 仕様変更は `REQUIREMENTS.md` を先に更新する
2. 構成変更は `DESIGN.md` を更新する
3. 実装は `src/` または `py/` で行う
4. Node.js版は `npm test` を実行する
5. 配布が必要な場合だけ `npm run build` でバンドルを生成する
6. 公開前に個人情報・秘密情報・環境固有パス・未整理のログが混入していないか確認する

## セキュリティ

- 実値の`config.json`、アクセスキー、パスワード、個人用絶対パスを読んだりコミットしたりしない
- `config.example.json`にはプレースホルダーだけを書く
- iOS版の状態ファイル・ログ・認証情報をVaultへ保存しない
- 本番R2へ向けたProbeは禁止し、テストバケットと専用キーを使う

## iOS版の運用

- 初期版はiOSショートカット → a-Shell `In App` → PythonのPULL専用
- iOS用の `py/sync.py` と `py/r2sync/` は本PJからiPhone側の実行領域へ一緒にデプロイし、Shortcutsのブックマークで参照する
- iOSはVault全走査・R2全列挙を行う現行互換モードから開始する
- 5秒目標は直接ファイル置換完了までで、Obsidianの反映時間とは分けて計測する
