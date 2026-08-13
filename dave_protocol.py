from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import quote

import msgpack
import requests
from Crypto.Cipher import AES
from requests.adapters import HTTPAdapter


ENCRYPTED_BUNDLE_MAGIC = b"\x10\x00\x00\x00"
SCHEMA_VERSION = 1


def load_local_environment() -> None:
    """Load an ignored local .env file without adding a third-party dependency."""
    base = Path(os.path.abspath(sys.executable)).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    path = base / ".env"
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if name and name not in os.environ:
            os.environ[name] = value


load_local_environment()


def require_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value or value.lower().startswith("replace"):
        raise ValueError(f"Missing secret environment variable: {name}")
    return value


def secret_bytes(name: str, valid_lengths: set[int]) -> bytes:
    raw = require_environment(name)
    try:
        value = bytes.fromhex(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be hexadecimal") from exc
    if len(value) not in valid_lengths:
        expected = ", ".join(str(length) for length in sorted(valid_lengths))
        raise ValueError(f"{name} decoded length must be one of: {expected}")
    return value


def require_string(source: dict[str, Any], key: str) -> str:
    value = source.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Missing non-empty configuration value: {key}")
    return value.strip()


def validate_config(config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    app = config.get("app")
    download = config.get("download", {})
    if not isinstance(app, dict) or not isinstance(download, dict):
        raise ValueError("Configuration sections 'app' and 'download' must be objects")
    app = dict(app)
    environment_overrides = {
        "DAVE_APP_VERSION": "version",
        "DAVE_APP_HASH": "app_hash",
        "DAVE_AB_VERSION": "ab_version",
    }
    for environment_name, field_name in environment_overrides.items():
        value = os.environ.get(environment_name, "").strip()
        if value and not value.lower().startswith("replace-with-"):
            app[field_name] = value
    if require_string(app, "region") != "cn":
        raise ValueError("Dave's native protocol engine currently supports the CN region")
    if require_string(app, "platform") not in {"android", "ios"}:
        raise ValueError("app.platform must be android or ios")
    require_string(app, "version")
    require_string(app, "app_hash")
    require_string(app, "ab_version")
    workers = int(download.get("workers", 2))
    if workers < 1:
        raise ValueError("download.workers must be at least 1")
    return app, download


def request_headers(app: dict[str, Any]) -> dict[str, str]:
    platform = require_string(app, "platform")
    platform_name = "Android" if platform == "android" else "iOS"
    unity_version = str(app.get("unity_version", "2022.3.21f1"))
    return {
        "Accept": "application/octet-stream",
        "Accept-Encoding": "gzip, deflate",
        "Content-Type": "application/octet-stream",
        "User-Agent": f"UnityPlayer/{unity_version}",
        "X-App-Version": require_string(app, "version"),
        "X-App-Hash": require_string(app, "app_hash"),
        "X-Platform": platform_name,
        "X-OperatingSystem": platform_name,
        "X-Unity-Version": unity_version,
        "X-DeviceModel": "DaveToolkit/1",
    }


def decrypt_index(payload: bytes, key: bytes, iv: bytes) -> dict[str, Any]:
    if not payload or len(payload) % AES.block_size:
        raise ValueError("The CN index payload is not valid AES-CBC data")
    decrypted = AES.new(key, AES.MODE_CBC, iv).decrypt(payload)
    padding = decrypted[-1]
    if padding < 1 or padding > AES.block_size or decrypted[-padding:] != bytes([padding]) * padding:
        raise ValueError("The CN index payload has invalid PKCS#7 padding")
    value = msgpack.unpackb(decrypted[:-padding], raw=False, strict_map_key=False)
    if not isinstance(value, dict):
        raise ValueError("The decoded CN index root is not an object")
    return value


def normalize_index(value: dict[str, Any]) -> dict[str, Any]:
    raw_bundles = value.get("bundles")
    if isinstance(raw_bundles, list):
        bundles = {
            item["bundleName"]: item
            for item in raw_bundles
            if isinstance(item, dict) and isinstance(item.get("bundleName"), str)
        }
    elif isinstance(raw_bundles, dict):
        bundles = {
            str(name): metadata
            for name, metadata in raw_bundles.items()
            if isinstance(metadata, dict)
        }
    else:
        raise ValueError("The decoded CN index has no bundles collection")
    if not bundles:
        raise ValueError("The decoded CN index is empty")
    normalized = dict(value)
    normalized["bundles"] = bundles
    return normalized


def signature(metadata: dict[str, Any]) -> str:
    identity = {
        "hash": metadata.get("hash"),
        "crc": metadata.get("crc"),
        "fileSize": metadata.get("fileSize"),
        "cacheFileName": metadata.get("cacheFileName"),
        "downloadPath": metadata.get("downloadPath"),
    }
    return json.dumps(identity, sort_keys=True, separators=(",", ":"))


def load_database_signatures(path: Path) -> dict[str, str]:
    if not path.is_file() or path.stat().st_size == 0:
        return {}
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(path)
        return dict(connection.execute("SELECT name, signature FROM bundles"))
    except sqlite3.DatabaseError:
        return {}
    finally:
        if connection is not None:
            connection.close()


def write_database_atomic(path: Path, index: dict[str, Any], source: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    connection = sqlite3.connect(temporary)
    try:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute(
            "CREATE TABLE bundles (name TEXT PRIMARY KEY, signature TEXT NOT NULL, raw_json TEXT NOT NULL)"
        )
        metadata = {
            "schema_version": str(SCHEMA_VERSION),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "index_version": str(index.get("version", "")),
            "version_number": str(source.get("version_number", "")),
            "index_url": str(source.get("index_url", "")),
        }
        connection.executemany("INSERT INTO metadata(key, value) VALUES (?, ?)", metadata.items())
        rows = [
            (name, signature(item), json.dumps(item, ensure_ascii=False, separators=(",", ":")))
            for name, item in index["bundles"].items()
        ]
        connection.executemany(
            "INSERT INTO bundles(name, signature, raw_json) VALUES (?, ?, ?)", rows
        )
        connection.commit()
    finally:
        connection.close()
    os.replace(temporary, path)


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


class DaveCnProtocol:
    def __init__(self, app: dict[str, Any], workers: int, logger: logging.Logger):
        self.app = app
        self.workers = workers
        self.logger = logger
        self.headers = request_headers(app)
        self.cn_cdn = require_environment("DAVE_CN_CDN").rstrip("/")
        self.cn_version_cdn = require_environment("DAVE_CN_VERSION_CDN").rstrip("/")
        self.cn_release_path = require_environment("DAVE_CN_RELEASE_PATH").strip("/")
        self.api_key = secret_bytes("DAVE_API_KEY_HEX", {16, 24, 32})
        self.api_iv = secret_bytes("DAVE_API_IV_HEX", {16})
        self._thread_local = threading.local()

    def session(self) -> requests.Session:
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = requests.Session()
            adapter = HTTPAdapter(pool_connections=1, pool_maxsize=1, max_retries=0)
            session.mount("https://", adapter)
            session.headers.update(self.headers)
            self._thread_local.session = session
        return session

    def fetch_index(self) -> tuple[dict[str, Any], dict[str, Any]]:
        ab_version = require_string(self.app, "ab_version")
        platform = require_string(self.app, "platform")
        version_url = (
            f"{self.cn_version_cdn}/Mainland/{quote(ab_version)}/Release/"
            f"{self.cn_release_path}/{quote(platform)}/version"
        )
        response = self.session().get(version_url, timeout=(15, 60))
        response.raise_for_status()
        version_number = int(response.text.strip())
        index_url = (
            f"{self.cn_cdn}/AssetBundle/{quote(ab_version)}/Release/{self.cn_release_path}/"
            f"{quote(platform)}{version_number}/AssetBundleInfoNew.json"
        )
        response = self.session().get(index_url, timeout=(15, 120))
        response.raise_for_status()
        index = normalize_index(decrypt_index(response.content, self.api_key, self.api_iv))
        source = {"version_number": version_number, "index_url": index_url}
        self.logger.info(
            "Dave decoded CN index version %s with %d Bundles",
            version_number,
            len(index["bundles"]),
        )
        return index, source

    @staticmethod
    def expand_dependencies(index: dict[str, Any], selected: set[str]) -> set[str]:
        bundles = index["bundles"]
        stack = list(selected)
        while stack:
            name = stack.pop()
            metadata = bundles.get(name)
            if not isinstance(metadata, dict):
                continue
            dependencies = metadata.get("dependencies", [])
            if not isinstance(dependencies, list):
                continue
            for dependency in dependencies:
                if isinstance(dependency, str) and dependency in bundles and dependency not in selected:
                    selected.add(dependency)
                    stack.append(dependency)
        return selected

    def bundle_url(self, metadata: dict[str, Any], name: str) -> str:
        ab_version = require_string(self.app, "ab_version")
        download_path = str(metadata.get("downloadPath") or "").strip("/")
        encoded_name = "/".join(quote(part) for part in name.split("/"))
        prefix = f"{self.cn_cdn}/AssetBundle/{quote(ab_version)}/Release/{self.cn_release_path}"
        return f"{prefix}/{download_path}/{encoded_name}" if download_path else f"{prefix}/{encoded_name}"

    @staticmethod
    def _copy_decrypted(response: requests.Response, output: Any) -> int:
        response.raw.decode_content = True
        first = response.raw.read(4)
        written = 0
        if first == ENCRYPTED_BUNDLE_MAGIC:
            header = bytearray(response.raw.read(128))
            if len(header) != 128:
                raise IOError("encrypted Bundle header is truncated")
            for offset in range(0, 128, 8):
                for index in range(5):
                    header[offset + index] = ~header[offset + index] & 0xFF
            output.write(header)
            written += len(header)
        else:
            output.write(first)
            written += len(first)
        while True:
            block = response.raw.read(1024 * 1024)
            if not block:
                break
            output.write(block)
            written += len(block)
        return written

    def download(self, index: dict[str, Any], names: set[str] | list[str], destination: Path) -> int:
        bundle_names = sorted(set(names))
        if not bundle_names:
            self.logger.info("Dave downloader found no changed Bundles")
            return 0
        destination.mkdir(parents=True, exist_ok=True)

        def download_one(name: str) -> None:
            metadata = index["bundles"][name]
            target = destination / Path(name)
            temporary = target.with_name(target.name + ".dave-part")
            target.parent.mkdir(parents=True, exist_ok=True)
            url = self.bundle_url(metadata, name)
            last_error: Exception | None = None
            for attempt in range(4):
                try:
                    with self.session().get(url, stream=True, timeout=(15, 180)) as response:
                        response.raise_for_status()
                        with temporary.open("wb") as output:
                            written = self._copy_decrypted(response, output)
                            output.flush()
                            os.fsync(output.fileno())
                    expected = metadata.get("fileSize")
                    if isinstance(expected, int) and expected >= 0 and written != expected:
                        raise IOError(f"decoded size is {written}, index says {expected}")
                    os.replace(temporary, target)
                    return
                except Exception as exc:
                    last_error = exc
                    temporary.unlink(missing_ok=True)
                    if attempt < 3:
                        time.sleep(min(2**attempt, 8))
            raise RuntimeError(f"{name}: {last_error}")

        failures: list[str] = []
        completed = 0
        self.logger.info(
            "Dave downloader selected %d Bundles with %d workers",
            len(bundle_names),
            self.workers,
        )
        with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="dave-download") as pool:
            futures = {pool.submit(download_one, name): name for name in bundle_names}
            for future in as_completed(futures):
                name = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    failures.append(name)
                    self.logger.error("Bundle download failed: %s", exc)
                completed += 1
                if completed == len(bundle_names) or completed % 100 == 0:
                    self.logger.info("Dave downloader progress: %d/%d", completed, len(bundle_names))
        if failures:
            raise RuntimeError(
                f"{len(failures)} Bundle downloads failed; first failure: {failures[0]}"
            )
        return len(bundle_names)


def refresh_and_download(
    config: dict[str, Any],
    database: Path,
    index_path: Path,
    bundles_path: Path,
    had_previous_cache: bool,
    index_only: bool,
    force_full: bool,
    logger: logging.Logger,
) -> int:
    app, download = validate_config(config)
    workers = int(download.get("workers", 2))
    protocol = DaveCnProtocol(app, workers, logger)
    previous = load_database_signatures(database) if had_previous_cache else {}
    index, source = protocol.fetch_index()
    index["_dave"] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "app": {
            "region": require_string(app, "region"),
            "platform": require_string(app, "platform"),
            "version": require_string(app, "version"),
            "ab_version": require_string(app, "ab_version"),
        },
        "version_number": source["version_number"],
        "bundle_count": len(index["bundles"]),
    }
    current = index["bundles"]
    write_database_atomic(database, index, source)
    write_json_atomic(index_path, index)
    if index_only:
        return 0
    if force_full:
        selected = set(current)
    elif previous:
        selected = {
            name for name, metadata in current.items() if previous.get(name) != signature(metadata)
        }
    elif bundles_path.exists() and any(bundles_path.iterdir()):
        selected = {
            name
            for name, metadata in current.items()
            if not (bundles_path / Path(name)).is_file()
            or (bundles_path / Path(name)).stat().st_size != metadata.get("fileSize")
        }
    else:
        selected = set(current)
    if bool(download.get("ensure_dependencies", False)):
        base_count = len(selected)
        protocol.expand_dependencies(index, selected)
        logger.info("Dependency expansion added %d Bundles", len(selected) - base_count)
    return protocol.download(index, selected, bundles_path)


def repair_bundles(
    config: dict[str, Any],
    index: dict[str, Any],
    names: list[str],
    bundles_path: Path,
    logger: logging.Logger,
) -> int:
    app, download = validate_config(config)
    protocol = DaveCnProtocol(app, int(download.get("workers", 2)), logger)
    return protocol.download(index, names, bundles_path)
