from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import time
import traceback
import uuid
import warnings
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from contextlib import contextmanager
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator


BUNDLE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else BUNDLE_DIR
DEFAULT_CONFIG = PROJECT_DIR / "config.json"
if not DEFAULT_CONFIG.is_file():
    DEFAULT_CONFIG = BUNDLE_DIR / "config.json"
LEDGER_VERSION = 1
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


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
        json.dump(value, handle, ensure_ascii=False, indent=2, default=json_default)
        handle.write("\n")
    os.replace(temporary, path)


def json_default(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"$bytes_base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, bytearray):
        return {"$bytes_base64": base64.b64encode(bytes(value)).decode("ascii")}
    if hasattr(value, "path_id"):
        return {"path_id": getattr(value, "path_id", 0)}
    if hasattr(value, "value"):
        return value.value
    return str(value)


def resolve_project_path(raw_path: str) -> Path:
    path = Path(os.path.expandvars(os.path.expanduser(raw_path)))
    if not path.is_absolute():
        path = PROJECT_DIR / path
    return path.resolve()


def bundle_signature(metadata: dict[str, Any]) -> str:
    identity = {
        "hash": metadata.get("hash"),
        "crc": metadata.get("crc"),
        "fileSize": metadata.get("fileSize"),
        "cacheFileName": metadata.get("cacheFileName"),
        "downloadPath": metadata.get("downloadPath"),
    }
    return json.dumps(identity, sort_keys=True, separators=(",", ":"))


def index_signatures(index_path: Path) -> dict[str, str]:
    if not index_path.is_file():
        return {}
    bundles = read_json(index_path).get("bundles", {})
    if not isinstance(bundles, dict):
        raise ValueError(f"Index has no bundles object: {index_path}")
    return {
        name: bundle_signature(metadata)
        for name, metadata in bundles.items()
        if isinstance(name, str) and isinstance(metadata, dict)
    }


def safe_component(value: str, fallback: str, max_length: int = 120) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    if not cleaned:
        cleaned = fallback
    if cleaned.upper() in WINDOWS_RESERVED_NAMES:
        cleaned = "_" + cleaned
    if len(cleaned) > max_length:
        digest = hashlib.sha1(cleaned.encode("utf-8", errors="replace")).hexdigest()[:10]
        cleaned = cleaned[: max_length - 12] + "__" + digest
    return cleaned


def checked_bundle_parts(bundle_name: str) -> tuple[str, ...]:
    path = PurePosixPath(bundle_name)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"Unsafe Bundle name: {bundle_name!r}")
    return tuple(safe_component(part, "bundle") for part in path.parts)


def bundle_output_path(output_root: Path, bundle_name: str) -> Path:
    parts = checked_bundle_parts(bundle_name)
    return output_root.joinpath(*parts[:-1], parts[-1] + ".bundle")


def object_filename(name: str, path_id: int, extension: str) -> str:
    extension = extension if extension.startswith(".") else "." + extension
    return f"{safe_component(name, 'unnamed')}__{path_id}{extension}"


def text_asset_filename(name: str, path_id: int) -> str:
    suffix = Path(name).suffix
    if suffix and len(suffix) <= 16 and re.fullmatch(r"\.[A-Za-z0-9._-]+", suffix):
        stem = name[: -len(suffix)]
        return f"{safe_component(stem, 'text')}__{path_id}{suffix}"
    return object_filename(name, path_id, ".bin")


def unique_output_path(path: Path, path_id: int, used_paths: set[str]) -> Path:
    key = path.as_posix().lower()
    if key not in used_paths and not path.exists():
        used_paths.add(key)
        return path
    candidate = path.with_name(f"{path.stem}__{path_id}{path.suffix}")
    counter = 2
    while candidate.as_posix().lower() in used_paths or candidate.exists():
        candidate = path.with_name(f"{path.stem}__{path_id}_{counter}{path.suffix}")
        counter += 1
    used_paths.add(candidate.as_posix().lower())
    return candidate


def container_relative_path(container: str, bundle_name: str) -> Path | None:
    normalized = container.replace("\\", "/").strip("/")
    parts = PurePosixPath(normalized).parts
    bundle_parts = PurePosixPath(bundle_name).parts
    for start in range(0, len(parts) - len(bundle_parts) + 1):
        if tuple(parts[start : start + len(bundle_parts)]) == bundle_parts:
            remainder = parts[start + len(bundle_parts) :]
            if remainder:
                return Path(*[safe_component(part, "asset") for part in remainder])
    return None


def semantic_object_path(
    stage_dir: Path,
    bundle_name: str,
    container: str | None,
    type_name: str,
    name: str,
    path_id: int,
    extension: str,
    used_paths: set[str],
) -> Path:
    relative = container_relative_path(container, bundle_name) if container else None
    if relative is not None:
        destination = stage_dir / relative
        lower_name = destination.name.lower()
        if type_name == "TextAsset":
            if lower_name.endswith(".acb.bytes") or lower_name.endswith(".usm.bytes"):
                destination = destination.with_suffix("")
            elif lower_name.endswith(".bytes"):
                destination = destination.with_suffix("")
            elif not destination.suffix:
                destination = destination.with_suffix(extension)
        else:
            destination = destination.with_suffix(extension)
    else:
        type_folder = {
            "MonoBehaviour": "monobehaviour",
            "Texture2D": "texture2d",
            "Sprite": "sprite",
            "AudioClip": "audio",
            "TextAsset": "textasset",
            "Font": "font",
            "Shader": "shader",
        }.get(type_name, safe_component(type_name.lower(), "object"))
        if type_name == "TextAsset":
            file_name = text_asset_filename(name, path_id)
        else:
            file_name = f"{safe_component(name, type_name)}{extension}"
        destination = stage_dir / "_objects" / type_folder / file_name
    return unique_output_path(destination, path_id, used_paths)


def write_bytes(path: Path, payload: bytes | bytearray | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        # UnityPy decodes TextAsset bytes with surrogate escapes. Encoding the
        # same way round-trips arbitrary ACB/USM/binary payloads losslessly.
        path.write_bytes(payload.encode("utf-8", errors="surrogateescape"))
    else:
        path.write_bytes(bytes(payload))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, default=json_default)
        handle.write("\n")


def promote_directory(stage_dir: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        shutil.rmtree(destination)
    last_error: OSError | None = None
    for attempt in range(5):
        try:
            os.replace(stage_dir, destination)
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(0.1 * (2**attempt))

    # Some Windows filesystem/filter-driver combinations intermittently deny
    # a directory rename after many files were created by a worker. Copy into
    # a sibling, then perform the short final rename; the old result was not
    # removed until the new stage had completed successfully.
    incoming = destination.with_name(destination.name + f".incoming.{uuid.uuid4().hex[:8]}")
    try:
        shutil.copytree(stage_dir, incoming)
        os.replace(incoming, destination)
        shutil.rmtree(stage_dir)
    except Exception:
        shutil.rmtree(incoming, ignore_errors=True)
        if last_error is not None:
            raise last_error
        raise


def export_object(
    obj: Any,
    stage_dir: Path,
    bundle_name: str,
    container: str | None,
    enabled_types: set[str],
    image_format: str,
    used_paths: set[str],
) -> list[str]:
    type_name = obj.type.name
    if type_name not in enabled_types:
        return []

    data = obj.read()
    path_id = int(getattr(obj, "path_id", 0))
    name = str(getattr(data, "m_Name", "") or type_name)
    exported: list[Path] = []

    if type_name in {"Texture2D", "Sprite"}:
        extension = "." + image_format.lower()
        destination = semantic_object_path(
            stage_dir, bundle_name, container, type_name, name, path_id, extension, used_paths
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        image = data.image
        save_format = "JPEG" if image_format.lower() in {"jpg", "jpeg"} else image_format.upper()
        if save_format == "JPEG" and image.mode not in {"RGB", "L"}:
            image = image.convert("RGB")
        image.save(destination, format=save_format)
        exported.append(destination)
    elif type_name == "TextAsset":
        destination = semantic_object_path(
            stage_dir, bundle_name, container, type_name, name, path_id, ".bytes", used_paths
        )
        write_bytes(destination, getattr(data, "m_Script", b""))
        exported.append(destination)
    elif type_name == "MonoBehaviour":
        destination = semantic_object_path(
            stage_dir, bundle_name, container, type_name, name, path_id, ".json", used_paths
        )
        write_json(destination, obj.read_typetree())
        exported.append(destination)
    elif type_name == "AudioClip":
        samples = getattr(data, "samples", {})
        if not isinstance(samples, dict):
            raise TypeError("AudioClip.samples did not return a mapping")
        for sample_name, payload in samples.items():
            destination = stage_dir / "_objects" / "audio" / safe_component(
                str(sample_name), f"audio_{path_id}"
            )
            if not Path(destination.name).suffix:
                destination = destination.with_suffix(".wav")
            destination = unique_output_path(destination, path_id, used_paths)
            write_bytes(destination, payload)
            exported.append(destination)
    elif type_name == "Font":
        payload = getattr(data, "m_FontData", b"")
        destination = semantic_object_path(
            stage_dir, bundle_name, container, type_name, name, path_id, ".ttf", used_paths
        )
        write_bytes(destination, payload)
        exported.append(destination)
    elif type_name == "Shader":
        payload = getattr(data, "m_Script", "")
        destination = semantic_object_path(
            stage_dir, bundle_name, container, type_name, name, path_id, ".shader", used_paths
        )
        write_bytes(destination, payload)
        exported.append(destination)

    return [path.relative_to(stage_dir).as_posix() for path in exported]


def extract_bundle_worker(task: dict[str, Any]) -> dict[str, Any]:
    bundle_name = str(task["bundle_name"])
    bundle_path = Path(task["bundle_path"])
    output_root = Path(task["output_root"])
    staging_root = Path(task["staging_root"])
    signature = str(task["signature"])
    unity_version = str(task["unity_version"])
    enabled_types = set(task["types"])
    image_format = str(task["image_format"])
    started = time.monotonic()
    stage_dir = staging_root / (
        hashlib.sha1(bundle_name.encode("utf-8")).hexdigest()
        + f".{os.getpid()}.{uuid.uuid4().hex[:8]}"
    )
    try:
        warnings.filterwarnings(
            "ignore", message=r"No valid Unity version found.*", category=Warning
        )
        import UnityPy

        UnityPy.config.FALLBACK_UNITY_VERSION = unity_version
        stage_dir.mkdir(parents=True, exist_ok=False)
        environment = UnityPy.load(str(bundle_path))
        containers = {
            int(getattr(pointer, "path_id", 0)): path
            for path, pointer in environment.container.items()
        }
        type_counts: dict[str, int] = {}
        exported_files: list[str] = []
        object_errors: list[dict[str, Any]] = []
        skipped_types: set[str] = set()
        object_count = 0
        used_paths: set[str] = set()

        for obj in environment.objects:
            object_count += 1
            type_name = obj.type.name
            type_counts[type_name] = type_counts.get(type_name, 0) + 1
            if type_name not in enabled_types:
                skipped_types.add(type_name)
                continue
            try:
                exported_files.extend(
                    export_object(
                        obj,
                        stage_dir,
                        bundle_name,
                        containers.get(int(getattr(obj, "path_id", 0))),
                        enabled_types,
                        image_format,
                        used_paths,
                    )
                )
            except Exception as exc:
                object_errors.append(
                    {
                        "type": type_name,
                        "path_id": int(getattr(obj, "path_id", 0)),
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )

        bundle_manifest = {
            "bundle": bundle_name,
            "signature": signature,
            "unity_version": unity_version,
            "extracted_at": now_iso(),
            "object_count": object_count,
            "exported_file_count": len(exported_files),
            "type_counts": type_counts,
            "skipped_types": sorted(skipped_types),
            "object_errors": object_errors,
            "files": exported_files,
        }
        write_json(stage_dir / "_bundle.json", bundle_manifest)

        destination = bundle_output_path(output_root, bundle_name)
        promote_directory(stage_dir, destination)
        return {
            "bundle": bundle_name,
            "success": True,
            "partial": bool(object_errors),
            "signature": signature,
            "objects": object_count,
            "files": len(exported_files),
            "object_errors": len(object_errors),
            "duration_seconds": round(time.monotonic() - started, 3),
        }
    except Exception as exc:
        if stage_dir.exists():
            shutil.rmtree(stage_dir, ignore_errors=True)
        return {
            "bundle": bundle_name,
            "success": False,
            "signature": signature,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(limit=8),
            "duration_seconds": round(time.monotonic() - started, 3),
        }


def bounded_process_map(
    tasks: Iterable[dict[str, Any]], workers: int
) -> Iterator[dict[str, Any]]:
    iterator = iter(tasks)
    with ProcessPoolExecutor(max_workers=workers) as executor:
        pending: dict[Future[dict[str, Any]], str] = {}

        def submit_one() -> bool:
            try:
                task = next(iterator)
            except StopIteration:
                return False
            future = executor.submit(extract_bundle_worker, task)
            pending[future] = str(task["bundle_name"])
            return True

        for _ in range(max(workers * 2, 1)):
            if not submit_one():
                break
        while pending:
            finished, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in finished:
                bundle_name = pending.pop(future)
                try:
                    yield future.result()
                except Exception as exc:
                    yield {
                        "bundle": bundle_name,
                        "success": False,
                        "error": f"WorkerError: {type(exc).__name__}: {exc}",
                    }
                submit_one()


def configure_logging(log_path: Path, verbose: bool) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("dave_extractor")
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
def extraction_lock(lock_path: Path) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
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
                raise RuntimeError("Dave Extractor is already running") from exc
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
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


def load_ledger(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"version": LEDGER_VERSION, "bundles": {}, "failures": {}}
    ledger = read_json(path)
    if ledger.get("version") != LEDGER_VERSION:
        raise ValueError(f"Unsupported extraction ledger version in {path}")
    if not isinstance(ledger.get("bundles"), dict):
        ledger["bundles"] = {}
    if not isinstance(ledger.get("failures"), dict):
        ledger["failures"] = {}
    return ledger


def select_candidates(
    current: dict[str, str],
    ledger: dict[str, Any],
    changed_from: Path | None,
    exact_names: list[str],
    filter_pattern: str | None,
    force: bool,
) -> list[str]:
    if exact_names:
        missing = [name for name in exact_names if name not in current]
        if missing:
            raise ValueError(f"Unknown Bundle name(s): {', '.join(missing[:5])}")
        selected = set(exact_names)
    elif force:
        selected = set(current)
    elif changed_from is not None:
        previous = index_signatures(changed_from)
        selected = {
            name
            for name, signature in current.items()
            if name not in previous or previous[name] != signature
        }
        selected.update(name for name in ledger["failures"] if name in current)
    else:
        completed = ledger["bundles"]
        selected = {
            name
            for name, signature in current.items()
            if not isinstance(completed.get(name), dict)
            or completed[name].get("signature") != signature
        }

    if filter_pattern:
        pattern = re.compile(filter_pattern)
        selected = {name for name in selected if pattern.search(name)}
    return sorted(selected)


def run_extraction(args: argparse.Namespace) -> int:
    config_path = Path(args.config).resolve()
    config = read_json(config_path)
    paths = config.get("paths", {})
    extract_config = config.get("extract", {})
    if not isinstance(paths, dict) or not isinstance(extract_config, dict):
        raise ValueError("Configuration sections paths and extract must be objects")

    bundles_dir = resolve_project_path(str(paths.get("bundles", "data/bundles")))
    index_path = resolve_project_path(str(paths.get("index", "data/AssetBundleInfo.dave.json")))
    output_root = resolve_project_path(str(paths.get("extracted", "data/extracted")))
    state_dir = resolve_project_path(str(paths.get("state", "state")))
    logs_dir = resolve_project_path(str(paths.get("logs", "logs")))
    staging_root = state_dir / "extract-staging"
    ledger_path = state_dir / "extraction_manifest.json"
    summary_path = state_dir / "extraction_last_run.json"
    logger = configure_logging(logs_dir / "dave-extractor.log", args.verbose)

    unity_version = str(extract_config.get("unity_version", "2022.3.21f1"))
    image_format = str(extract_config.get("image_format", "png")).lower()
    if image_format not in {"png", "jpg", "jpeg", "webp"}:
        raise ValueError("extract.image_format must be png, jpg, jpeg, or webp")
    enabled_types = extract_config.get(
        "types", ["Texture2D", "Sprite", "TextAsset", "MonoBehaviour", "AudioClip", "Font", "Shader"]
    )
    if not isinstance(enabled_types, list) or not all(isinstance(item, str) for item in enabled_types):
        raise ValueError("extract.types must be a list of Unity type names")
    workers = int(args.workers or extract_config.get("workers", 2))
    if workers < 1:
        raise ValueError("extract.workers must be at least 1")
    save_every = max(int(extract_config.get("ledger_save_every", 25)), 1)
    changed_from = Path(args.changed_from).resolve() if args.changed_from else None

    output_root.mkdir(parents=True, exist_ok=True)
    staging_root.mkdir(parents=True, exist_ok=True)
    current = index_signatures(index_path)
    ledger = load_ledger(ledger_path)
    candidates = select_candidates(
        current,
        ledger,
        changed_from,
        args.bundle or [],
        args.filter,
        args.force,
    )
    if args.limit is not None:
        if args.limit < 0:
            raise ValueError("--limit cannot be negative")
        candidates = candidates[: args.limit]

    started_at = now_iso()
    started = time.monotonic()
    summary: dict[str, Any] = {
        "started_at": started_at,
        "success": False,
        "selected": len(candidates),
        "completed": 0,
        "partial": 0,
        "failed": 0,
        "exported_files": 0,
        "objects": 0,
    }

    logger.info("Selected %d Bundles for extraction with %d workers", len(candidates), workers)
    try:
        with extraction_lock(state_dir / "extract.lock"):
            tasks = (
                {
                    "bundle_name": name,
                    "bundle_path": str(bundles_dir.joinpath(*PurePosixPath(name).parts)),
                    "output_root": str(output_root),
                    "staging_root": str(staging_root),
                    "signature": current[name],
                    "unity_version": unity_version,
                    "types": enabled_types,
                    "image_format": image_format,
                }
                for name in candidates
            )

            for position, result in enumerate(bounded_process_map(tasks, workers), start=1):
                name = str(result["bundle"])
                if result.get("success"):
                    summary["completed"] += 1
                    summary["partial"] += int(bool(result.get("partial")))
                    summary["exported_files"] += int(result.get("files", 0))
                    summary["objects"] += int(result.get("objects", 0))
                    ledger["bundles"][name] = {
                        "signature": current[name],
                        "extracted_at": now_iso(),
                        "files": int(result.get("files", 0)),
                        "objects": int(result.get("objects", 0)),
                        "object_errors": int(result.get("object_errors", 0)),
                    }
                    ledger["failures"].pop(name, None)
                else:
                    summary["failed"] += 1
                    ledger["failures"][name] = {
                        "signature": current.get(name),
                        "attempted_at": now_iso(),
                        "error": str(result.get("error", "unknown extraction failure")),
                    }
                    logger.error("Extraction failed for %s: %s", name, result.get("error"))
                    logger.debug("%s", result.get("traceback", ""))

                if position % save_every == 0:
                    ledger["updated_at"] = now_iso()
                    write_json_atomic(ledger_path, ledger)
                if position % 25 == 0 or position == len(candidates):
                    logger.info(
                        "Extraction progress: %d/%d, failed=%d, files=%d",
                        position,
                        len(candidates),
                        summary["failed"],
                        summary["exported_files"],
                    )

            ledger["updated_at"] = now_iso()
            write_json_atomic(ledger_path, ledger)
            summary["success"] = summary["failed"] == 0
            summary["finished_at"] = now_iso()
            summary["duration_seconds"] = round(time.monotonic() - started, 2)
            summary["output"] = str(output_root)
            write_json_atomic(summary_path, summary)
            logger.info(
                "Extraction complete: %d completed, %d partial, %d failed, %d files",
                summary["completed"],
                summary["partial"],
                summary["failed"],
                summary["exported_files"],
            )
            return 0 if summary["failed"] == 0 else 1
    except Exception as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
        summary["finished_at"] = now_iso()
        summary["duration_seconds"] = round(time.monotonic() - started, 2)
        write_json_atomic(summary_path, summary)
        logger.exception("Extraction stopped: %s", exc)
        return 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dave Extractor - incrementally export Project SEKAI Unity assets"
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="path to config.json")
    parser.add_argument("--bundle", action="append", help="extract one exact Bundle name; repeatable")
    parser.add_argument("--filter", help="regular expression applied to Bundle names")
    parser.add_argument("--changed-from", help="only extract Bundles changed from this older index")
    parser.add_argument("--force", action="store_true", help="re-extract selected Bundles")
    parser.add_argument("--limit", type=int, help="maximum number of selected Bundles")
    parser.add_argument("--workers", type=int, help="override extraction worker count")
    parser.add_argument("--verbose", action="store_true", help="show debug output")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(run_extraction(parse_args()))
