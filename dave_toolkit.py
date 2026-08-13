from __future__ import annotations

import shutil
import sys
import multiprocessing
from pathlib import Path
from typing import Callable

import download_from_index
import extractor
import updater


APP_NAME = "Dave Asset Toolkit"


def bundle_dir() -> Path:
    return Path(__file__).resolve().parent


def runtime_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return bundle_dir()


def print_help() -> None:
    print(
        f"""{APP_NAME}

Usage:
  Dave-Asset-Toolkit init
  Dave-Asset-Toolkit index [options]
  Dave-Asset-Toolkit update [options]
  Dave-Asset-Toolkit download [options]
  Dave-Asset-Toolkit extract [options]

Commands:
  init      Create editable config.json and .env files beside the executable
  index     Build only the latest AssetBundle index
  update    Build the index, download changes, and extract supported assets
  download  Download selected bundles from a local or hosted index
  extract   Extract already downloaded Unity bundles

Run a command with --help to see its options.
"""
    )


def initialize() -> int:
    destination = runtime_dir()
    destination.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    for source_name, target_name in (("config.json", "config.json"), (".env.example", ".env")):
        source = bundle_dir() / source_name
        target = destination / target_name
        if target.exists():
            print(f"Exists: {target}")
            continue
        shutil.copyfile(source, target)
        created.append(target)
        print(f"Created: {target}")
    if created:
        print("Fill in the private values in .env before running index, update, or download.")
    return 0


def dispatch(arguments: list[str], callback: Callable[[], int]) -> int:
    original = sys.argv
    try:
        sys.argv = [original[0], *arguments]
        return callback()
    finally:
        sys.argv = original


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in {"-h", "--help", "help"}:
        print_help()
        return 0
    command = sys.argv[1].lower()
    arguments = sys.argv[2:]
    if command == "init":
        return initialize()
    if command == "index":
        return dispatch(
            ["--index-only", "--skip-extract", *arguments],
            lambda: updater.execute(updater.parse_args()),
        )
    if command == "update":
        return dispatch(arguments, lambda: updater.execute(updater.parse_args()))
    if command == "download":
        return dispatch(arguments, download_from_index.main)
    if command == "extract":
        return dispatch(arguments, lambda: extractor.run_extraction(extractor.parse_args()))
    print(f"Unknown command: {command}", file=sys.stderr)
    print_help()
    return 2


if __name__ == "__main__":
    multiprocessing.freeze_support()
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"{APP_NAME} failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
