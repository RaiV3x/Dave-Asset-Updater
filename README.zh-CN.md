# Dave Asset Toolkit

[English](README.md) · **简体中文** · [日本語](README.ja.md)

一个面向《世界计划》国服的轻量 AssetBundle 索引构建与下载工具。它可以创建最新索引、下载发生变化的资源包、提取支持的 Unity 资源，并通过 GitHub Actions 发布公开索引。

## 功能

- 构建最新 AssetBundle 索引。
- 下载全部、发生变化或指定的资源包。
- 解包支持的 Unity 资源。
- 自动发布带日期的 GitHub Release。

## 快速开始

需要 Python 3.11 或更高版本。

```powershell
python -m venv .venv
copy .env.example .env
# 填写 .env 后继续。
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe updater.py --index-only --skip-extract
```

Linux：

```bash
python3 -m venv .venv
cp .env.example .env
# 填写 .env 后继续。
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python updater.py --index-only --skip-extract
```

## 常用操作

```bash
# 仅构建索引
python updater.py --index-only --skip-extract

# 构建索引、下载变化并解包
python updater.py

# 根据本地或在线索引下载指定资源
python download_from_index.py --index "索引路径或地址" --filter "正则表达式"

# 解包已经下载的资源
python extractor.py
```

## 便携版本

每个 Release 计划提供：

- Windows x64 和 x86（`.exe`）
- Linux x64 和 ARM64（`.tgz`）
- macOS Intel x64 和 Apple Silicon ARM64（`.tgz`）

便携程序使用同一个入口整合全部功能：

```bash
Dave-Asset-Toolkit init
Dave-Asset-Toolkit index
Dave-Asset-Toolkit update
Dave-Asset-Toolkit download --index "索引路径或地址" --filter "正则表达式"
Dave-Asset-Toolkit extract
```

## GitHub Actions

内置工作流会发布带日期的 GitHub Release，其中包含：

- `AssetBundleInfo.dave.json`
- Windows、Linux 和 macOS 便携版本
- `SHA256SUMS.txt`

Fork 后需要先在仓库中配置所需的 Secrets 和 Variables。

## 免责声明

本项目为非官方项目，与 SEGA、Colorful Palette、Crypton Future Media 无关。请仅处理你有权访问的数据，并遵守当地法律与相关服务条款。

## 许可证

本项目采用 [MIT License](LICENSE)。

Copyright (c) 2026 RaiV3x.
