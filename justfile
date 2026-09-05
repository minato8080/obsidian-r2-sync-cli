python := env_var_or_default("PYTHON", "python")

# R2へ接続せず、VaultのIGNORE/INCLUDEを一覧表示する
check-ignore config:
    {{python}} py/sync.py --config "{{config}}" --check-ignore

# R2へ接続せず、VaultのIGNORE/INCLUDEを全パス表示する
check-ignore-verbose config:
    {{python}} py/sync.py --config "{{config}}" --verbose --check-ignore

# R2へ接続せず、Vault相対パス1件の除外判定を表示する
check-ignore-path config path:
    {{python}} py/sync.py --config "{{config}}" --check-ignore "{{path}}"
