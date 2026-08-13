from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Iterator

from dave_protocol import refresh_and_download, repair_bundles


BUNDLE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else BUNDLE_DIR
DEFAULT_CONFIG = PROJECT_DIR / "config.json"
if not DEFAULT_CONFIG.is_file():
    DEFAULT_CONFIG = BUNDLE_DIR / "config.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, path)


def resolve_project_path(raw_path: str) -> Path:
    path = Path(os.path.expandvars(os.path.expanduser(raw_path)))
    if not path.is_absolute():
        path = PROJECT_DIR / path
    return path.resolve()


def require_string(source: dict[str, Any], key: str) -> str:
    value = source.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Missing non-empty configuration value: {key}")
    return value.strip()


def configure_logging(log_path: Path, verbose: bool) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("dave_updater")
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname).1s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(formatter)
    file_handler = RotatingFileHandler(
        log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(console)
    logger.addHandler(file_handler)
    return logger


@contextmanager
def single_instance(lock_path: Path) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    handle.seek(0)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("Dave Asset Toolkit is already running") from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("Dave Asset Toolkit is already running") from exc
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def bundle_signatures(index_path: Path) -> dict[str, str]:
    if not index_path.exists():
        return {}
    bundles = read_json(index_path).get("bundles", {})
    if not isinstance(bundles, dict):
        return {}
    result: dict[str, str] = {}
    for name, metadata in bundles.items():
        if isinstance(name, str) and isinstance(metadata, dict):
            identity = {
                "hash": metadata.get("hash"),
                "crc": metadata.get("crc"),
                "fileSize": metadata.get("fileSize"),
                "cacheFileName": metadata.get("cacheFileName"),
                "downloadPath": metadata.get("downloadPath"),
            }
            result[name] = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return result


def compare_indexes(old_path: Path, new_path: Path) -> dict[str, int]:
    old = bundle_signatures(old_path)
    new = bundle_signatures(new_path)
    return {
        "bundle_count": len(new),
        "added": sum(name not in old for name in new),
        "changed": sum(name in old and old[name] != value for name, value in new.items()),
        "removed": sum(name not in new for name in old),
    }


def repair_bundle_sizes(
    config: dict[str, Any],
    index_path: Path,
    bundles_dir: Path,
    logger: logging.Logger,
) -> int:
    logger.info("Verifying local Bundle names and sizes against the refreshed index")
    index = read_json(index_path)
    entries = index.get("bundles", {})
    if not isinstance(entries, dict):
        raise RuntimeError("The refreshed index does not contain a bundles object")

    local_sizes: dict[str, int] = {}
    for directory, _, names in os.walk(bundles_dir):
        directory_path = Path(directory)
        for name in names:
            path = directory_path / name
            if not name.endswith(".dave-part"):
                local_sizes[path.relative_to(bundles_dir).as_posix()] = path.stat().st_size

    repair_names = [
        name
        for name, metadata in entries.items()
        if isinstance(name, str)
        and isinstance(metadata, dict)
        and (
            name not in local_sizes
            or (
                isinstance(metadata.get("fileSize"), int)
                and local_sizes[name] != metadata["fileSize"]
            )
        )
    ]
    if not repair_names:
        logger.info("Local Bundle size verification passed")
        return 0
    logger.warning("Repairing %d missing or size-mismatched Bundles", len(repair_names))
    repair_bundles(config, index, repair_names, bundles_dir, logger)
    remaining = [
        name
        for name in repair_names
        if not (bundles_dir / Path(name)).is_file()
        or (bundles_dir / Path(name)).stat().st_size != entries[name].get("fileSize")
    ]
    if remaining:
        raise RuntimeError(f"{len(remaining)} Bundles remain invalid after repair")
    return len(repair_names)


def run_incremental_extractor(
    config_path: Path,
    previous_index: Path,
    state_dir: Path,
    logger: logging.Logger,
) -> dict[str, Any]:
    if not previous_index.is_file():
        logger.warning(
            "No previous index is available; automatic extraction is skipping the initial "
            "full set. Run ./extract_assets.sh to perform the resumable initial extraction."
        )
        return {"skipped": True, "reason": "no_previous_index"}
    command = [
        sys.executable,
        str(PROJECT_DIR / "extractor.py"),
        "--config",
        str(config_path),
        "--changed-from",
        str(previous_index),
    ]
    logger.info("Starting Dave incremental extraction for new and changed Bundles")
    process = subprocess.Popen(
        command,
        cwd=PROJECT_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert process.stdout is not None
    for line in process.stdout:
        message = line.rstrip("\r\n")
        if message:
            logger.info("[extractor] %s", message)
    return_code = process.wait()
    summary_path = state_dir / "extraction_last_run.json"
    summary = read_json(summary_path) if summary_path.is_file() else {}
    summary["return_code"] = return_code
    if return_code:
        logger.error("Incremental extraction failed; failed Bundles remain queued for retry")
    return summary


def execute(args: argparse.Namespace) -> int:
    config_path = Path(args.config).resolve()
    config = read_json(config_path)
    paths = config.get("paths", {})
    if not isinstance(paths, dict):
        raise ValueError("Configuration section 'paths' must be an object")
    database = resolve_project_path(require_string(paths, "database"))
    index = resolve_project_path(require_string(paths, "index"))
    bundles = resolve_project_path(require_string(paths, "bundles"))
    state_dir = resolve_project_path(str(paths.get("state", "state")))
    logs_dir = resolve_project_path(str(paths.get("logs", "logs")))
    logger = configure_logging(logs_dir / "dave-toolkit.log", args.verbose)

    database.parent.mkdir(parents=True, exist_ok=True)
    index.parent.mkdir(parents=True, exist_ok=True)
    bundles.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    previous_database = state_dir / "previous_dave_index.sqlite3"
    previous_index = state_dir / "previous_index.json"
    working_database = state_dir / "working_dave_index.sqlite3"
    working_index = state_dir / "working_index.json"
    status_path = state_dir / "status.json"

    started = time.monotonic()
    status: dict[str, Any] = {
        "last_attempt": now_iso(),
        "success": False,
        "engine": "dave-native-cn-v1",
        "mode": "index-only" if args.index_only else "update-and-download",
    }
    try:
        with single_instance(state_dir / "update.lock"):
            had_previous_cache = database.is_file() and database.stat().st_size > 0
            if had_previous_cache:
                shutil.copy2(database, working_database)
            else:
                working_database.unlink(missing_ok=True)
            working_index.unlink(missing_ok=True)

            downloaded = refresh_and_download(
                config,
                working_database,
                working_index,
                bundles,
                had_previous_cache,
                args.index_only,
                args.force_full,
                logger,
            )
            if not working_database.is_file() or not working_index.is_file():
                raise RuntimeError("Dave native engine did not produce its database and index")
            repaired = 0
            if not args.index_only:
                repaired = repair_bundle_sizes(config, working_index, bundles, logger)

            if database.exists():
                shutil.copy2(database, previous_database)
            else:
                previous_database.unlink(missing_ok=True)
            if index.exists():
                shutil.copy2(index, previous_index)
            else:
                previous_index.unlink(missing_ok=True)
            os.replace(working_database, database)
            os.replace(working_index, index)
            comparison = compare_indexes(previous_index, index)

            extraction_summary: dict[str, Any] | None = None
            extract = config.get("extract", {})
            if (
                not args.index_only
                and not args.skip_extract
                and isinstance(extract, dict)
                and bool(extract.get("enabled", True))
                and bool(extract.get("auto_after_update", True))
            ):
                extraction_summary = run_incremental_extractor(
                    config_path, previous_index, state_dir, logger
                )
            status.update(comparison)
            status.update(
                {
                    "success": True,
                    "last_success": now_iso(),
                    "duration_seconds": round(time.monotonic() - started, 2),
                    "database": str(database),
                    "index": str(index),
                    "bundles": str(bundles),
                    "downloaded": downloaded,
                    "repaired": repaired,
                    "extraction": extraction_summary,
                }
            )
            write_json_atomic(status_path, status)
            logger.info(
                "Update complete: %d total, %d added, %d changed, %d removed, %d downloaded, %d repaired",
                comparison["bundle_count"],
                comparison["added"],
                comparison["changed"],
                comparison["removed"],
                downloaded,
                repaired,
            )
            return 0
    except Exception as exc:
        logger.exception("Update failed: %s", exc)
        old_status = read_json(status_path) if status_path.exists() else {}
        if isinstance(old_status.get("last_success"), str):
            status["last_success"] = old_status["last_success"]
        status["error"] = str(exc)
        status["duration_seconds"] = round(time.monotonic() - started, 2)
        write_json_atomic(status_path, status)
        return 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dave Asset Toolkit for CN indexes, hot-update downloads and extraction"
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="path to config.json")
    parser.add_argument("--index-only", action="store_true", help="update only Dave's index")
    parser.add_argument("--force-full", action="store_true", help="download the full current index")
    parser.add_argument("--skip-extract", action="store_true", help="skip extraction for this run")
    parser.add_argument("--verbose", action="store_true", help="show debug output")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(execute(parse_args()))
