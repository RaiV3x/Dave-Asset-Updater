# Dave Asset Toolkit

**English** · [简体中文](README.zh-CN.md) · [日本語](README.ja.md)

A lightweight AssetBundle index builder and downloader for the CN version of Project SEKAI. It can build a fresh index, download changed bundles, extract supported Unity assets, and publish the public index through GitHub Actions.

## Features

- Build the latest AssetBundle index.
- Download all, changed, or selected bundles.
- Extract supported Unity assets.
- Publish a dated GitHub Release automatically.

## Quick start

Requires Python 3.11 or newer.

```powershell
python -m venv .venv
copy .env.example .env
# Fill in .env before continuing.
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe updater.py --index-only --skip-extract
```

Linux:

```bash
python3 -m venv .venv
cp .env.example .env
# Fill in .env before continuing.
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python updater.py --index-only --skip-extract
```

## Common tasks

```bash
# Build the index only
python updater.py --index-only --skip-extract

# Build the index, download changes, and extract them
python updater.py

# Download selected bundles from a local or hosted index
python download_from_index.py --index "INDEX_PATH_OR_URL" --filter "REGEX"

# Extract downloaded bundles
python extractor.py
```

## Portable builds

Each Release targets:

- Windows x64 and x86 (`.exe`)
- Linux x64 and ARM64 (`.tgz`)
- macOS Intel x64 and Apple Silicon ARM64 (`.tgz`)

The portable executable combines every feature behind one command:

```bash
Dave-Asset-Toolkit init
Dave-Asset-Toolkit index
Dave-Asset-Toolkit update
Dave-Asset-Toolkit download --index "INDEX_PATH_OR_URL" --filter "REGEX"
Dave-Asset-Toolkit extract
```

## GitHub Actions

The included workflow publishes a dated GitHub Release containing:

- `AssetBundleInfo.dave.json`
- Portable Windows, Linux, and macOS builds
- `SHA256SUMS.txt`

Fork owners must configure the required repository Secrets and Variables before running the workflow.

## Disclaimer

This project is unofficial and is not affiliated with SEGA, Colorful Palette, or Crypton Future Media. Use it only with data you are authorized to access and comply with applicable laws and terms of service.

## License

Licensed under the [MIT License](LICENSE).

Copyright (c) 2026 RaiV3x.
