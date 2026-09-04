python := env_var_or_default("PYTHON", "python")

# R2へ接続せず、Vaultの除外対象を一覧表示する
check-ignore config:
    {{python}} ios/sync.py --config "{{config}}" --check-ignore

# R2へ接続せず、Vault相対パス1件の除外判定を表示する
check-ignore-path config path:
    {{python}} ios/sync.py --config "{{config}}" --check-ignore "{{path}}"
