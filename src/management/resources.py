"""Bounded, immutable file resources. Business persistence belongs to Core.

The service owns one ResourceManager per data directory. Its reservations protect
concurrent work in this process; the runtime's single-owner lock excludes another
service. No method garbage-collects blobs or overwrites user output. Files become
usable only after an atomic no-replace publish and an integrity check.
"""
from __future__ import annotations

import codecs
import csv
import hashlib
import io
import json
import mimetypes
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import threading
import time
from contextlib import contextmanager
from typing import Callable, Iterable
from urllib.parse import quote
import uuid


CHUNK_SIZE = 4 * 1024 * 1024
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_TEXT_BYTES = 64 * 1024 * 1024
MAX_NOTEBOOK_BYTES = 16 * 1024 * 1024
SHA_PATTERN = re.compile(r"^[0-9a-f]{64}$")
JOB_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$")
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10))}


class ResourceError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details or {}


def _plain_path(path: str | Path, *, file: bool = False) -> Path:
    """Reject links/reparse junctions before resolve can hide them."""
    path = Path(path).absolute()
    for part in [*reversed(path.parents), path]:
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise ResourceError("UNSAFE_PATH", "符号链接或目录连接不能作为资源路径。")
    if file and (not path.is_file() or not stat.S_ISREG(path.stat().st_mode)):
        raise ResourceError("NOT_A_FILE", "资源必须是存在的普通文件。")
    return path


def _relative(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024 or "\\" in value:
        raise ResourceError("UNSAFE_PATH", "资源成员必须使用有效的相对路径。")
    path = PurePosixPath(value)
    if path.is_absolute() or len(path.parts) > 16:
        raise ResourceError("UNSAFE_PATH", "资源成员不能使用绝对路径或过深路径。")
    # PurePath normalizes '.' and duplicate slashes; reject their original forms.
    parts = value.split("/")
    if any(not p or p in (".", "..") or p.endswith((".", " "))
           or any(ord(c) < 32 or c in ':<>"|?*' for c in p)
           or p.split(".")[0].upper() in RESERVED for p in parts):
        raise ResourceError("UNSAFE_PATH", "资源成员含不安全或不可移植的路径。")
    if any(p.startswith(".") for p in parts):
        raise ResourceError("UNSAFE_PATH", "资源成员不能覆盖内部管理文件。")
    return path.as_posix()


def _digest(path: Path, *, limit: int | None = None) -> tuple[str, int]:
    path = _plain_path(path, file=True)
    h, count = hashlib.sha256(), 0
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        while block := stream.read(CHUNK_SIZE):
            count += len(block)
            if limit is not None and count > limit:
                raise ResourceError("RESOURCE_LIMIT", "资源超过允许的大小。")
            h.update(block)
        after = os.fstat(stream.fileno())
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or count != before.st_size:
        raise ResourceError("SOURCE_CHANGED", "读取期间资源发生变化，请重新读取当前版本。")
    return h.hexdigest(), count


def _read_json(path: Path, maximum: int = MAX_MANIFEST_BYTES):
    path = _plain_path(path, file=True)
    if path.stat().st_size > maximum:
        raise ResourceError("RESOURCE_LIMIT", "资源清单超过允许的大小。")
    try:
        return json.loads(path.read_text("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ResourceError("INVALID_MANIFEST", "资源清单不是有效的 UTF-8 JSON。") from exc


def _write_json(path: Path, data: dict, *, replace: bool = False):
    path = _plain_path(path)
    encoded = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise ResourceError("RESOURCE_LIMIT", "资源清单超过允许的大小。")
    temp = path.parent / f".manifest-{uuid.uuid4().hex}.partial"
    try:
        with temp.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            _plain_path(path)
            os.replace(temp, path)
        else:
            _publish_new(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _publish_new(temp: Path, destination: Path):
    _plain_path(temp, file=True)
    _plain_path(destination)
    try:
        os.link(temp, destination)
    except FileExistsError as exc:
        raise ResourceError("ALREADY_EXISTS", "目标已存在，不会覆盖已有文件。") from exc
    except OSError as exc:
        raise ResourceError("ATOMIC_PUBLISH_FAILED", "无法安全固化文件；原件和暂存保持可恢复。",
                            {"errno": exc.errno}) from exc


def _connect_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect("file:" + quote(path.as_posix(), safe="/:") + "?mode=ro", uri=True, timeout=2)
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA trusted_schema=OFF")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class ResourceManager:
    def __init__(self, data_dir: str | Path, reserve_bytes: int = 1024**3,
                 staging_limit: int = 2 * 1024**3):
        if reserve_bytes < 0 or staging_limit <= 0:
            raise ValueError("invalid resource budgets")
        self.root = _plain_path(data_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.reserve_bytes, self.staging_limit = reserve_bytes, staging_limit
        self._lock, self._reserved = threading.RLock(), 0
        for name in ("blobs", "jobs", "backups", "exports", ".staging"):
            target = _plain_path(self.root / name)
            target.mkdir(exist_ok=True)

    @contextmanager
    def _reservation(self, amount: int, volume: Path | None = None):
        volume = volume or self.root
        with self._lock:
            if amount < 0 or self._reserved + amount > self.staging_limit:
                raise ResourceError("RESOURCE_LIMIT", "资源处理预算不足，请分批处理。")
            self._space(volume, amount + self._reserved)
            self._reserved += amount
        try:
            yield
        except OSError as exc:
            raise ResourceError("FILE_IO_ERROR", "文件处理未完成。", {"errno": exc.errno}) from exc
        finally:
            with self._lock:
                self._reserved -= amount

    def _space(self, path: Path, additional: int = 0):
        probe = path
        while not probe.exists():
            probe = probe.parent
        free = shutil.disk_usage(probe).free
        if free - additional < self.reserve_bytes:
            raise ResourceError("LOW_DISK_SPACE", "可用空间不足，已保留普通业务操作余量。",
                                {"free_bytes": free, "required_bytes": additional,
                                 "reserve_bytes": self.reserve_bytes})

    def _blob(self, sha256: str, *, verify: bool = True) -> Path:
        if not isinstance(sha256, str) or not SHA_PATTERN.fullmatch(sha256):
            raise ResourceError("INVALID_HASH", "资产哈希无效。")
        path = _plain_path(self.root / "blobs" / sha256, file=True)
        if verify and _digest(path)[0] != sha256:
            raise ResourceError("HASH_MISMATCH", "不可变资产完整性检查失败。", {"sha256": sha256})
        return path

    def import_file(self, source: str | Path, *, expected_sha256: str | None = None,
                    max_bytes: int | None = None, media_type: str | None = None) -> dict:
        source = _plain_path(source, file=True)
        before = source.stat()
        if max_bytes is not None and before.st_size > max_bytes:
            raise ResourceError("RESOURCE_LIMIT", "资源超过允许的大小。")
        if expected_sha256 is not None and not SHA_PATTERN.fullmatch(expected_sha256):
            raise ResourceError("INVALID_HASH", "预期哈希无效。")
        partial = _plain_path(self.root / ".staging" / f"import-{uuid.uuid4().hex}.partial")
        h, size = hashlib.sha256(), 0
        with self._reservation(before.st_size):
            try:
                with source.open("rb") as src, partial.open("xb") as dst:
                    opened = os.fstat(src.fileno())
                    while block := src.read(CHUNK_SIZE):
                        size += len(block)
                        if size > before.st_size or (max_bytes is not None and size > max_bytes):
                            raise ResourceError("SOURCE_CHANGED", "导入期间资源发生变化。")
                        self._space(self.root)
                        dst.write(block)
                        h.update(block)
                    dst.flush()
                    os.fsync(dst.fileno())
                    after = os.fstat(src.fileno())
                if (opened.st_size, opened.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or size != before.st_size:
                    raise ResourceError("SOURCE_CHANGED", "导入期间资源发生变化。")
                digest = h.hexdigest()
                if expected_sha256 and digest != expected_sha256:
                    raise ResourceError("HASH_MISMATCH", "输入文件与预期哈希不符。")
                target = self.root / "blobs" / digest
                with self._lock:
                    if target.exists() or target.is_symlink():
                        existing = self._blob(digest)
                        if existing.stat().st_size != size:
                            raise ResourceError("HASH_MISMATCH", "现存资产与内容地址不符。")
                    else:
                        _publish_new(partial, target)
                return {"sha256": digest, "size": size, "path": f"blobs/{digest}",
                        "filename": source.name, "media_type": media_type or mimetypes.guess_type(source.name)[0] or "application/octet-stream",
                        "state": "ready", "validation": {"integrity": "verified", "content": "not_checked"}}
            finally:
                # Only our never-adopted partial file; no source or published blob deletion.
                partial.unlink(missing_ok=True)

    def create_workspace(self, job_id: str | None = None) -> dict:
        job_id = job_id or uuid.uuid4().hex
        if not JOB_PATTERN.fullmatch(job_id):
            raise ResourceError("INVALID_JOB_ID", "作业编号无效。")
        path = _plain_path(self.root / "jobs" / job_id)
        with self._lock:
            self._space(self.root)
            if path.exists():
                return self._workspace(job_id)[1]
            path.mkdir()
            metadata = {"job_id": job_id, "path": str(path), "relative_path": f"jobs/{job_id}",
                        "state": "open", "created_at": time.time(), "files": []}
            _write_json(path / ".workspace.json", metadata)
            return metadata

    def _workspace(self, job_id: str) -> tuple[Path, dict]:
        if not isinstance(job_id, str) or not JOB_PATTERN.fullmatch(job_id):
            raise ResourceError("INVALID_JOB_ID", "作业编号无效。")
        path = _plain_path(self.root / "jobs" / job_id)
        meta = _read_json(path / ".workspace.json")
        if not isinstance(meta, dict) or meta.get("job_id") != job_id:
            raise ResourceError("INVALID_MANIFEST", "作业登记不匹配。")
        return path, meta

    def _update_workspace(self, job_id: str, entry: dict):
        with self._lock:
            path, meta = self._workspace(job_id)
            meta["files"] = [x for x in meta.get("files", []) if x.get("relative_path") != entry["relative_path"]]
            meta["files"].append(entry)
            _write_json(path / ".workspace.json", meta, replace=True)

    def materialize_bundle(self, entries: Iterable[dict], *, job_id: str | None = None) -> dict:
        checked, seen, total = [], set(), 0
        for entry in entries:
            if not isinstance(entry, dict) or "path" not in entry or "sha256" not in entry:
                raise ResourceError("INVALID_MANIFEST", "Bundle member requires path and sha256.")
            if len(checked) >= 10000:
                raise ResourceError("RESOURCE_LIMIT", "资料包成员过多，请分批处理。")
            relative = _relative(entry["path"])
            key = relative.casefold()
            if key in seen or any(key.startswith(p + "/") or p.startswith(key + "/") for p in seen):
                raise ResourceError("BUNDLE_CONFLICT", "资料包包含重复或相互覆盖的成员路径。")
            seen.add(key)
            blob = self._blob(entry["sha256"])
            size = blob.stat().st_size
            total += size
            checked.append((relative, entry["sha256"], blob, size))
        if not checked:
            raise ResourceError("EMPTY_BUNDLE", "资料包不能没有成员。")
        metadata = self.create_workspace(job_id)
        path, _ = self._workspace(metadata["job_id"])
        # Complete preflight before any member is published.
        for relative, _, _, _ in checked:
            target = _plain_path(path / relative)
            if target.exists():
                raise ResourceError("ALREADY_EXISTS", "资料包目标已存在。")
        files = []
        with self._reservation(total):
            try:
                for relative, digest, blob, size in checked:
                    target = _plain_path(path / relative)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    part = target.parent / f".bundle-{uuid.uuid4().hex}.partial"
                    try:
                        self._copy_verified(blob, part, digest, size)
                        _publish_new(part, target)
                    finally:
                        part.unlink(missing_ok=True)
                    entry = {"relative_path": relative, "sha256": digest, "size": size, "state": "materialized"}
                    self._update_workspace(metadata["job_id"], entry)
                    files.append(entry)
            except Exception:
                with self._lock:
                    _, meta = self._workspace(metadata["job_id"])
                    meta["state"] = "materialization_failed"
                    _write_json(path / ".workspace.json", meta, replace=True)
                raise
        return {"job_id": metadata["job_id"], "path": str(path), "state": "materialized", "files": files}

    def _copy_verified(self, source: Path, destination: Path, digest: str, size: int):
        h, count = hashlib.sha256(), 0
        source = _plain_path(source, file=True)
        destination = _plain_path(destination)
        with source.open("rb") as src, destination.open("xb") as dst:
            while block := src.read(CHUNK_SIZE):
                count += len(block)
                if count > size:
                    raise ResourceError("SOURCE_CHANGED", "复制期间资产发生变化。")
                self._space(destination.parent)
                h.update(block)
                dst.write(block)
            dst.flush()
            os.fsync(dst.fileno())
        if count != size or h.hexdigest() != digest:
            raise ResourceError("HASH_MISMATCH", "复制后的资产与来源清单不符。")

    def generate_artifact(self, job_id: str, relative_path: str, kind: str, content) -> dict:
        path, _ = self._workspace(job_id)
        relative_path = _relative(relative_path)
        kind = kind.lower()
        if kind in ("text", "markdown"):
            if not isinstance(content, str):
                raise ResourceError("INVALID_ARTIFACT", "文本成果必须提供文本。")
            payload = content.encode("utf-8")
        elif kind == "csv":
            if not isinstance(content, list) or len(content) > 100000:
                raise ResourceError("INVALID_ARTIFACT", "CSV 成果必须提供有界行列表。")
            stream = io.StringIO(newline="")
            writer = csv.writer(stream, lineterminator="\n")
            for row in content:
                if not isinstance(row, (list, tuple)) or len(row) > 500 or any(not isinstance(x, (str, int, float, bool, type(None))) for x in row):
                    raise ResourceError("INVALID_ARTIFACT", "CSV 行或字段无效。")
                writer.writerow(row)
                if stream.tell() > MAX_TEXT_BYTES:
                    raise ResourceError("RESOURCE_LIMIT", "CSV 超过生成预算。")
            payload = stream.getvalue().encode("utf-8")
        elif kind == "notebook":
            self._validate_notebook(content)
            payload = json.dumps(content, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
        else:
            raise ResourceError("UNSUPPORTED_ARTIFACT", "此成果格式尚未配置生产与验证执行器。")
        limit = MAX_NOTEBOOK_BYTES if kind == "notebook" else MAX_TEXT_BYTES
        if len(payload) > limit:
            raise ResourceError("RESOURCE_LIMIT", "成果超过生成预算。")
        target = _plain_path(path / relative_path)
        with self._reservation(len(payload)):
            if target.exists():
                raise ResourceError("ALREADY_EXISTS", "成果已存在，请使用新的版本名称。")
            target.parent.mkdir(parents=True, exist_ok=True)
            part = target.parent / f".artifact-{uuid.uuid4().hex}.partial"
            try:
                with part.open("xb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                _publish_new(part, target)
            finally:
                part.unlink(missing_ok=True)
        entry = {"relative_path": relative_path, "kind": kind, "size": len(payload), "state": "staged"}
        self._update_workspace(job_id, entry)
        return {**entry, "job_id": job_id, "path": str(target)}

    @staticmethod
    def _validate_notebook(value):
        if not isinstance(value, dict) or value.get("nbformat") != 4 or not isinstance(value.get("nbformat_minor"), int) or not isinstance(value.get("metadata"), dict):
            raise ResourceError("INVALID_ARTIFACT", "Notebook 必须符合 v4 基本结构。")
        cells = value.get("cells")
        if not isinstance(cells, list) or len(cells) > 10000:
            raise ResourceError("INVALID_ARTIFACT", "Notebook 单元格列表无效。")
        for cell in cells:
            if not isinstance(cell, dict) or cell.get("cell_type") not in ("markdown", "code", "raw") or not isinstance(cell.get("metadata"), dict):
                raise ResourceError("INVALID_ARTIFACT", "Notebook 单元格类型或元数据无效。")
            source = cell.get("source")
            if not isinstance(source, str) and not (isinstance(source, list) and all(isinstance(s, str) for s in source)):
                raise ResourceError("INVALID_ARTIFACT", "Notebook 单元格内容无效。")
            if cell["cell_type"] == "code" and (cell.get("outputs") != [] or cell.get("execution_count", "missing") is not None):
                raise ResourceError("INVALID_ARTIFACT", "生成的 Notebook 不得伪造执行结果。")

    def freeze_artifact(self, job_id: str, relative_path: str, *, kind: str = "file",
                        source_versions: list | None = None, generator_version: str = "builtin-v1") -> dict:
        path, _ = self._workspace(job_id)
        relative_path = _relative(relative_path)
        target = _plain_path(path / relative_path, file=True)
        validation = {"integrity": "verified", "content": "not_checked", "execution": "not_run"}
        if kind in ("text", "markdown", "csv"):
            if target.stat().st_size > MAX_TEXT_BYTES:
                raise ResourceError("RESOURCE_LIMIT", "文本验证超过预算。")
            decoder = codecs.getincrementaldecoder("utf-8")("strict")
            try:
                with target.open("rb") as stream:
                    while block := stream.read(CHUNK_SIZE):
                        decoder.decode(block)
                    decoder.decode(b"", final=True)
            except UnicodeError as exc:
                raise ResourceError("INVALID_ARTIFACT", "成果不是有效 UTF-8。") from exc
            validation["content"] = "utf8_verified"
        elif kind == "notebook":
            self._validate_notebook(_read_json(target, MAX_NOTEBOOK_BYTES))
            validation["content"] = "notebook_structure_verified"
        elif kind != "file":
            raise ResourceError("UNSUPPORTED_ARTIFACT", "此成果格式未配置内容验证器。")
        data = self.import_file(target)
        result = {**data, "job_id": job_id, "kind": kind, "relative_path": relative_path,
                  "state": "frozen", "source_versions": source_versions or [],
                  "generator_version": generator_version, "validation": validation,
                  "retain_policy": "user_artifact", "pinned": True}
        self._update_workspace(job_id, result)
        return result

    def create_backup(self, database_path: str | Path,
                      asset_selector: Callable[[sqlite3.Connection], Iterable[dict]], *,
                      app_lock, metadata: dict | None = None) -> dict:
        """Snapshot + select exact assets while app_lock blocks migrations/GC.

        No business transaction may be open on the supplied DB. File copies happen
        after releasing app_lock. This version never GCs blobs; future GC must pin
        manifest references until backup publication. The service checks business
        relationships/schema through its own selector/restore validator.
        """
        database_path = _plain_path(database_path, file=True)
        ident = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex}"
        partial = _plain_path(self.root / "backups" / f".{ident}.partial")
        final = _plain_path(self.root / "backups" / ident)
        partial.mkdir()
        (partial / "blobs").mkdir()
        try:
            with self._reservation(database_path.stat().st_size * 2 + CHUNK_SIZE):
                with app_lock:
                    source = _connect_readonly(database_path)
                    target = sqlite3.connect(partial / "database.sqlite3")
                    try:
                        started = time.monotonic()
                        def check_progress(*_):
                            self._space(partial)
                            if time.monotonic() - started > 30:
                                raise ResourceError("BACKUP_TIMEOUT", "Database snapshot exceeded its time budget.")
                        source.backup(target, pages=256, progress=check_progress, sleep=0.05)
                        target.commit()
                        assets = self._normalize_assets(asset_selector(target))
                    finally:
                        target.close()
                        source.close()
            # The snapshot is closed before hashing or copying attachments.
            db_hash, db_size = _digest(partial / "database.sqlite3")
            pool = _plain_path(self.root / "backups" / ".objects")
            pool.mkdir(exist_ok=True)
            total = sum(a["size"] for a in assets if not (pool / a["sha256"]).exists())
            with self._reservation(total):
                for item in assets:
                    source = self._blob(item["sha256"])
                    if source.stat().st_size != item["size"]:
                        raise ResourceError("HASH_MISMATCH", "数据库快照的资产大小与文件不符。")
                    pooled = _plain_path(pool / item["sha256"])
                    if not pooled.exists():
                        temp = pool / f".{uuid.uuid4().hex}.partial"
                        try:
                            self._copy_verified(source, temp, item["sha256"], item["size"])
                            with self._lock:
                                if not pooled.exists():
                                    _publish_new(temp, pooled)
                        finally:
                            temp.unlink(missing_ok=True)
                    if _digest(pooled) != (item["sha256"], item["size"]):
                        raise ResourceError("HASH_MISMATCH", "Backup content pool integrity failed.")
                    # A backup-private immutable content pool is separate from live
                    # blobs. Same-volume links reuse bytes across backup versions;
                    # each backup still contains directly readable regular files.
                    _publish_new(pooled, partial / "blobs" / item["sha256"])
            if metadata and self._contains_secret_key(metadata):
                raise ResourceError("UNSAFE_BACKUP_METADATA", "备份元数据不能包含运行令牌或凭据。")
            manifest = {"format": "management-backup/1", "id": ident, "created_at": time.time(),
                        "database": {"sha256": db_hash, "size": db_size}, "assets": assets,
                        "metadata": metadata or {}, "state": "complete"}
            _write_json(partial / "manifest.json", manifest)
            report = self.verify_backup(partial)
            with self._lock:
                if final.exists():
                    raise ResourceError("ALREADY_EXISTS", "备份目标已存在。")
                os.rename(partial, final)
            return {"id": ident, "path": str(final), "manifest": manifest, "report": report, "state": "verified"}
        except Exception:
            # Preserve incomplete owned work for inspection; never list it complete.
            try:
                _write_json(partial / "incomplete.json", {"state": "failed", "id": ident}, replace=True)
            except Exception:
                pass
            raise

    @staticmethod
    def _contains_secret_key(data) -> bool:
        if isinstance(data, dict):
            return any(str(k).lower() in {"token", "runtime_token", "secret", "password", "api_key", "authorization", "private_key"}
                       or ResourceManager._contains_secret_key(v) for k, v in data.items())
        return isinstance(data, list) and any(ResourceManager._contains_secret_key(v) for v in data)

    @staticmethod
    def _normalize_assets(assets: Iterable[dict]) -> list[dict]:
        indexed = {}
        for asset in assets:
            if not isinstance(asset, dict):
                raise ResourceError("INVALID_MANIFEST", "Asset entries must be objects.")
            sha, size = asset.get("sha256"), asset.get("size")
            if not isinstance(sha, str) or not SHA_PATTERN.fullmatch(sha) or not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise ResourceError("INVALID_MANIFEST", "资产清单的哈希或大小无效。")
            if sha in indexed and indexed[sha]["size"] != size:
                raise ResourceError("INVALID_MANIFEST", "同一资产哈希具有冲突大小。")
            indexed[sha] = {"sha256": sha, "size": size}
            if len(indexed) > 100000:
                raise ResourceError("RESOURCE_LIMIT", "备份资产清单过大。")
        return sorted(indexed.values(), key=lambda x: x["sha256"])

    def verify_backup(self, path: str | Path) -> dict:
        path = _plain_path(path)
        if (path / "incomplete.json").exists():
            raise ResourceError("INCOMPLETE_BACKUP", "备份未完成，不能恢复。")
        manifest = _read_json(path / "manifest.json")
        if not isinstance(manifest, dict) or manifest.get("format") != "management-backup/1" or manifest.get("state") != "complete":
            raise ResourceError("INVALID_MANIFEST", "备份格式或状态无效。")
        try:
            assets = self._normalize_assets(manifest["assets"])
            db = self._normalize_assets([manifest["database"]])[0]
        except (KeyError, TypeError, AttributeError) as exc:
            raise ResourceError("INVALID_MANIFEST", "备份清单结构不完整。") from exc
        if len(assets) != len(manifest["assets"]):
            raise ResourceError("INVALID_MANIFEST", "备份清单包含重复成员。")
        db_path = _plain_path(path / "database.sqlite3", file=True)
        if _digest(db_path) != (db["sha256"], db["size"]):
            raise ResourceError("HASH_MISMATCH", "数据库备份哈希不符。")
        conn = _connect_readonly(db_path)
        try:
            if conn.execute("PRAGMA quick_check").fetchall() != [("ok",)] or conn.execute("PRAGMA foreign_key_check").fetchone():
                raise ResourceError("INVALID_DATABASE", "数据库结构或外键验证失败。")
        except sqlite3.DatabaseError as exc:
            raise ResourceError("INVALID_DATABASE", "数据库备份无法读取。") from exc
        finally:
            conn.close()
        for item in assets:
            file = _plain_path(path / "blobs" / item["sha256"], file=True)
            if _digest(file) != (item["sha256"], item["size"]):
                raise ResourceError("HASH_MISMATCH", "备份附件完整性检查失败。")
        return {"valid": True, "asset_count": len(assets), "asset_bytes": sum(a["size"] for a in assets),
                "database_bytes": db["size"], "database_check": "quick_check_and_foreign_keys",
                "business_validation": "caller_required", "manifest": manifest}

    def restore_backup(self, path: str | Path, target_dir: str | Path, *, validator=None) -> dict:
        """Verify completely, then publish into a nonexistent new data directory.

        The caller must set a fresh epoch and reconcile jobs/notifications before
        starting a service. Runtime discovery/credentials are never copied.
        """
        path, destination = _plain_path(path), _plain_path(target_dir)
        if destination.exists():
            raise ResourceError("ALREADY_EXISTS", "恢复只能写入不存在的新目录。")
        report = self.verify_backup(path)
        if validator:
            conn = _connect_readonly(path / "database.sqlite3")
            try:
                validation = validator(conn)
                if validation is False or isinstance(validation, dict) and validation.get("valid") is False:
                    raise ResourceError("INVALID_DATABASE", "恢复前业务校验未通过。")
                report["business_validation"] = validation
            finally:
                conn.close()
        manifest = report["manifest"]
        if not destination.parent.is_dir():
            raise ResourceError("INVALID_DESTINATION", "恢复目录的父目录必须已存在。")
        stage = _plain_path(destination.parent / f".{destination.name}-restore-{uuid.uuid4().hex}.partial")
        total = report["database_bytes"] + report["asset_bytes"] + MAX_MANIFEST_BYTES
        with self._reservation(total, destination.parent):
            stage.mkdir()
            (stage / "blobs").mkdir()
            try:
                db = manifest["database"]
                self._copy_verified(path / "database.sqlite3", stage / "database.sqlite3", db["sha256"], db["size"])
                for item in manifest["assets"]:
                    self._copy_verified(path / "blobs" / item["sha256"], stage / "blobs" / item["sha256"], item["sha256"], item["size"])
                _write_json(stage / "restore_pending.json", {"backup_id": manifest["id"], "state": "awaiting_service_reconciliation",
                            "requires_new_epoch": True, "requires_job_reconciliation": True})
                if destination.exists():
                    raise ResourceError("ALREADY_EXISTS", "恢复目标已被其他操作创建。")
                os.rename(stage, destination)
            except Exception:
                # Keep a clearly named partial; do not recursively delete unique data.
                raise
        return {"path": str(destination), "state": "restored_pending_reconciliation", "report": report,
                "requires_new_epoch": True, "requires_job_reconciliation": True}
