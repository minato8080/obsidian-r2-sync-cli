# Performance notes

## 比較対象

現在のNode.js版とPython版を比較した測定結果です。

| 実装種別 | 対象ソース | 測定日 |
|---|---|---|
| Node.js | `src/` | 2026-09-12 |
| Python | `py/` | 2026-09-12 |

## 測定条件

- Windows上で測定
- Node.js `v24.20.0`
- Python `3.9.13`
- 一時Vault、256ファイル、1ファイル1KiB
- 各処理を5回実行し、中央値を採用
- 実R2への通信は行わない
- 測定項目は、ファイル走査、暗号化済みパスの復号とNOOP差分計画、256ファイル分の内容暗号化・復号

「合算トータル」は、3項目の中央値を合算した値である。Node/Pythonのプロセス起動、テストデータ生成、実R2通信は含まない。

## 結果

単位はmsです。

| 実装種別 | 対象ソース | ファイル走査 | 差分計画 | 暗号化・復号 | 合算トータル |
|---|---|---:|---:|---:|---:|
| Node.js | `src/` | 62.80 | 57.31 | 30.37 | **150.48** |
| Python | `py/` | 14.25 | 4.16 | 3,406.59 | **3,425.00** |

## 比較

| 比較 | ファイル走査 | 差分計画 | 暗号化・復号 | 合算トータル |
|---|---:|---:|---:|---:|
| Python / Node.js | -77.31% | -92.74% | +11,116.33% | **+2,176.06%** |

## 解釈

- Python版はファイル走査と差分計画が高速である。
- Python版は内容暗号化・復号が大幅なボトルネックになっており、合算トータルではNode.js版より遅い。
- この比較はローカル処理の比較であり、R2のネットワーク待ち時間や実際のPUT/GET並列性は評価していない。

## 再測定

ベンチマーク実装は次のファイルにある。

- `tools/benchmark-runtime.mjs`
- `tools/benchmark_runtime.py`

共通条件での実行例は次の通り。

```powershell
node tools/benchmark-runtime.mjs --profile windows --files 256 --bytes 1024 --repeats 5
python tools/benchmark_runtime.py --profile windows --files 256 --bytes 1024 --repeats 5
```
