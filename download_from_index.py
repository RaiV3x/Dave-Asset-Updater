from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from dave_protocol import DaveCnProtocol, normalize_index, validate_config


BUNDLE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else BUNDLE_DIR
DEFAULT_CONFIG = PROJECT_DIR / "config.json"
if not DEFAULT_CONFIG.is_file():
    DEFAULT_CONFIG = BUNDLE_DIR / "config.json"
MAX_INDEX_BYTES = 256 * 1024 * 1024


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def resolve_project_path(raw: str) -> Path:
    path = Path(os.path.expandvars(os.path.expanduser(raw)))
    return (path if path.is_absolute() else PROJECT_DIR / path).resolve()


def load_index(source: str, token: str | None) -> dict[str, Any]:
    parsed = urlparse(source)
    if parsed.scheme in {"http", "https"}:
        headers = {"Accept": "application/octet-stream"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        with requests.get(source, headers=headers, stream=True, timeout=(15, 180)) as response:
            response.raise_for_status()
            declared = int(response.headers.get("Content-Length", "0") or 0)
            if declared > MAX_INDEX_BYTES:
                raise ValueError(f"Remote index is too large: {declared} bytes")
            payload = bytearray()
            for block in response.iter_content(1024 * 1024):
                payload.extend(block)
                if len(payload) > MAX_INDEX_BYTES:
                    raise ValueError("Remote index exceeded the 256 MiB safety limit")
        value = json.loads(payload.decode("utf-8"))
    else:
        value = read_json(resolve_project_path(source))
    if not isinstance(value, dict):
        raise ValueError("Index root must be an object")
    return normalize_index(value)


def configure_logger(verbose: bool) -> logging.Logger:
    logger = logging.getLogger("dave_index_downloader")
    logger.handlers.clear()
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname).1s | %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    return logger


def index_app(index: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    metadata = index.get("_dave")
    embedded = metadata.get("app") if isinstance(metadata, dict) else None
    if isinstance(embedded, dict):
        candidate = dict(fallback)
        candidate.update(embedded)
        validate_config({"app": candidate, "download": {"workers": 1}})
        return candidate
    return fallback


def select_names(
    index: dict[str, Any],
    output: Path,
    exact: list[str] | None,
    pattern: str | None,
    force: bool,
    limit: int | None,
) -> set[str]:
    bundles = index["bundles"]
    if exact:
        missing = [name for name in exact if name not in bundles]
        if missing:
            raise ValueError(f"Bundle not found in index: {missing[0]}")
        candidates = list(dict.fromkeys(exact))
    elif pattern:
        compiled = re.compile(pattern)
        candidates = [name for name in bundles if compiled.search(name)]
    else:
        candidates = list(bundles)

    selected: list[str] = []
    for name in candidates:
        target = output / Path(name)
        expected = bundles[name].get("fileSize")
        valid = target.is_file() and (
            not isinstance(expected, int) or target.stat().st_size == expected
        )
        if force or not valid:
            selected.append(name)
            if limit is not None and len(selected) >= limit:
                break
    return set(selected)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download Project SEKAI CN Bundles from a local or GitHub-hosted Dave index"
    )
    parser.add_argument("--index", required=True, help="index JSON path or HTTPS URL")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="path to config.json")
    parser.add_argument("--output", help="Bundle output directory; defaults to config paths.bundles")
    parser.add_argument("--bundle", action="append", help="exact Bundle name; repeatable")
    parser.add_argument("--filter", help="regular expression applied to Bundle names")
    parser.add_argument("--limit", type=int, help="maximum number of Bundles to download")
    parser.add_argument("--workers", type=int, help="override download worker count")
    parser.add_argument("--force", action="store_true", help="overwrite valid existing files")
    parser.add_argument(
        "--token",
        default=os.environ.get("GITHUB_TOKEN"),
        help="GitHub token for a private index URL; defaults to GITHUB_TOKEN",
    )
    parser.add_argument("--verbose", action="store_true", help="show debug output")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.limit is not None and args.limit < 1:
        raise ValueError("--limit must be at least 1")
    logger = configure_logger(args.verbose)
    config = read_json(Path(args.config).resolve())
    fallback_app, download = validate_config(config)
    index = load_index(args.index, args.token)
    app = index_app(index, fallback_app)
    paths = config.get("paths", {})
    if not isinstance(paths, dict):
        raise ValueError("Configuration section 'paths' must be an object")
    output = resolve_project_path(args.output or str(paths.get("bundles", "data/bundles")))
    workers = args.workers if args.workers is not None else int(download.get("workers", 2))
    if workers < 1:
        raise ValueError("--workers must be at least 1")
    names = select_names(index, output, args.bundle, args.filter, args.force, args.limit)
    logger.info(
        "Index contains %d Bundles; %d require downloading",
        len(index["bundles"]),
        len(names),
    )
    protocol = DaveCnProtocol(app, workers, logger)
    protocol.download(index, names, output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"Dave index download failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
