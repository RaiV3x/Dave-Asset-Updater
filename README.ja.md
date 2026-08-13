# Dave Asset Toolkit

[English](README.md) · [简体中文](README.zh-CN.md) · **日本語**

中国版「プロジェクトセカイ」向けの軽量な AssetBundle インデックス作成・ダウンロードツールです。最新インデックスの作成、変更されたバンドルのダウンロード、対応する Unity アセットの抽出、GitHub Actions による公開インデックスの配布に対応します。

## 機能

- 最新の AssetBundle インデックスを作成します。
- すべて、変更済み、または指定したバンドルをダウンロードします。
- 対応する Unity アセットを抽出します。
- 日付付き GitHub Release を自動公開します。

## クイックスタート

Python 3.11 以降が必要です。

```powershell
python -m venv .venv
copy .env.example .env
# .env を入力してから続行してください。
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe updater.py --index-only --skip-extract
```

Linux：

```bash
python3 -m venv .venv
cp .env.example .env
# .env を入力してから続行してください。
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python updater.py --index-only --skip-extract
```

## 主な操作

```bash
# インデックスのみを作成
python updater.py --index-only --skip-extract

# インデックス作成、変更分のダウンロード、抽出
python updater.py

# ローカルまたは公開インデックスから指定アセットをダウンロード
python download_from_index.py --index "INDEX_PATH_OR_URL" --filter "REGEX"

# ダウンロード済みアセットを抽出
python extractor.py
```

## ポータブル版

各 Release では次の環境を対象にします。

- Windows x64、x86（`.exe`）
- Linux x64、ARM64（`.tgz`）
- macOS Intel x64、Apple Silicon ARM64（`.tgz`）

ポータブル実行ファイルは、すべての機能を一つの入口に統合します。

```bash
Dave-Asset-Toolkit init
Dave-Asset-Toolkit index
Dave-Asset-Toolkit update
Dave-Asset-Toolkit download --index "INDEX_PATH_OR_URL" --filter "REGEX"
Dave-Asset-Toolkit extract
```

## GitHub Actions

付属ワークフローは、次のファイルを含む日付付き GitHub Release を公開します。

- `AssetBundleInfo.dave.json`
- Windows、Linux、macOS 向けポータブル版
- `SHA256SUMS.txt`

Fork 後は、必要なリポジトリ Secrets と Variables を設定してください。

## 免責事項

本プロジェクトは非公式であり、SEGA、Colorful Palette、Crypton Future Media とは関係ありません。アクセス権のあるデータのみを扱い、適用される法律と利用規約を遵守してください。

## ライセンス

[MIT License](LICENSE) の下で提供されます。

Copyright (c) 2026 RaiV3x.
