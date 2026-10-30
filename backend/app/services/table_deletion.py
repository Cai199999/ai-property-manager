"""物理删除数据表，并同步 manifest / upload_index 元数据。"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import Settings
from app.core.exceptions import (
    TableDeletionError,
    TableImportUnavailableError,
    TableNotFoundError,
)
from app.services.table_catalog import (
    TABLE_MANIFEST_NAME,
    UPLOAD_INDEX_NAME,
    TableCatalog,
)

logger = logging.getLogger(__name__)

# 对前端与单元测试暴露统一的安全错误文案，避免不同入口返回不同提示。
DELETE_SECURITY_MESSAGE = "安全限制：只能删除工作区内的数据表"


@dataclass(frozen=True, slots=True)
class TableDeletionResult:
    """删表结果；remaining_tables 已按目录当前顺序排列。"""

    deleted_table: str
    deleted_files: tuple[str, ...]
    remaining_tables: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TableBulkDeletionResult:
    """删除全部表的结果；文件名不暴露绝对路径。"""

    deleted_tables: tuple[str, ...]
    deleted_files: tuple[str, ...]
    remaining_tables: tuple[str, ...]
    fingerprint: str


class TableDeletionService:
    """删除整张表：文件、上传索引与 manifest 声明保持同步。"""

    def __init__(self, settings: Settings, catalog: TableCatalog | None) -> None:
        self._settings = settings
        self._catalog = catalog
        self._directory = Path(settings.data_table_dir)

    @property
    def enabled(self) -> bool:
        return self._catalog is not None and self._settings.table_directory_enabled

    def delete_table_tool(self, table_name: str) -> TableDeletionResult:
        """删除指定表及其同内容副本，返回删除后的剩余表名。

        安全边界（硬约束）：
        1. 表名只能是名称，不允许出现 ``..``、``/``、``\\``、``~`` 或绝对路径；
        2. 每个待删文件必须解析为绝对路径，且必须位于 ``data_table_dir`` 内；
        3. 只删除工作区内由系统托管的 ``.xlsx`` 副本，不做目录级删除。
        """
        if not self.enabled or self._catalog is None:
            raise TableImportUnavailableError()

        safe_name = self._validate_table_name(table_name)
        directory = self._workspace_root()
        with self._catalog.operation_lock:
            snapshot = self._catalog.scan()
            dataset = snapshot.table_map().get(safe_name)
            if dataset is None:
                raise TableNotFoundError()

            # 扫描结果来自目录，但仍重新解析并做前缀校验，防止符号链接或竞态。
            target = self._resolve_workspace_file(dataset.path)

            # 同一份内容可能被历史重复上传过，全部一起删除，
            # 否则删掉一份后，去重逻辑会把另一份重新暴露出来。
            file_paths = self._same_content_files(target)
            if target not in file_paths:
                file_paths.insert(0, target)
            deleted_names = {path.name for path in file_paths}

            index_path = directory / UPLOAD_INDEX_NAME
            manifest_path = directory / TABLE_MANIFEST_NAME
            index_payload = self._load_json_object(index_path)
            manifest_payload = self._load_json_object(manifest_path)
            original_index = (
                None
                if index_payload is None
                else json.dumps(
                    index_payload,
                    ensure_ascii=False,
                    indent=2,
                )
            )
            original_manifest = (
                None
                if manifest_payload is None
                else json.dumps(
                    manifest_payload,
                    ensure_ascii=False,
                    indent=2,
                )
            )

            index_changed = self._remove_upload_entries(
                index_payload,
                deleted_names=deleted_names,
            )
            manifest_changed = self._remove_manifest_entries(
                manifest_payload,
                deleted_names=deleted_names,
                table_name=safe_name,
            )

            quarantined: list[tuple[Path, Path]] = []
            try:
                for path in file_paths:
                    quarantine = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.deleting")
                    path.replace(quarantine)
                    quarantined.append((path, quarantine))

                if index_changed:
                    self._write_json_object(index_path, index_payload or {})
                if manifest_changed:
                    self._write_json_object(manifest_path, manifest_payload or {})
            except (OSError, TableDeletionError) as exc:
                # 文件尚未真正 unlink，可安全回滚，避免索引与文件不一致。
                for path, quarantine in reversed(quarantined):
                    try:
                        quarantine.replace(path)
                    except OSError:
                        logger.warning("回滚删除文件失败 file=%s", path.name)
                self._restore_json(index_path, original_index)
                self._restore_json(manifest_path, original_manifest)
                if isinstance(exc, TableDeletionError):
                    raise
                logger.warning("删除数据表失败 table=%s error=%s", safe_name, type(exc).__name__)
                raise TableDeletionError() from exc

            # 元数据已落盘，再物理删除隔离文件；单个文件 unlink 失败只记录日志，
            # 因为它已不在 .xlsx 命名空间内，不会再被扫描到。
            for _, quarantine in quarantined:
                try:
                    quarantine.unlink()
                except OSError:
                    logger.warning("清理隔离文件失败 file=%s", quarantine.name)

            self._catalog.invalidate()
            try:
                remaining = tuple(table.name for table in self._catalog.scan().tables)
            except Exception as exc:  # noqa: BLE001 - 删除已完成，目录重扫失败不应反报失败
                logger.warning("删除后扫描目录失败: %s", type(exc).__name__)
                remaining = tuple(
                    table.name for table in snapshot.tables if table.name != table_name
                )

        logger.info("已删除数据表 table=%s files=%s", safe_name, len(file_paths))
        return TableDeletionResult(
            deleted_table=safe_name,
            deleted_files=tuple(sorted(deleted_names)),
            remaining_tables=remaining,
        )

    def delete_table(self, table_name: str) -> TableDeletionResult:
        """兼容旧调用名；实际执行 ``delete_table_tool``。"""
        return self.delete_table_tool(table_name)

    def table_set_fingerprint(self) -> tuple[tuple[str, ...], str]:
        """返回当前可删除文件与指纹，供确认令牌绑定。

        指纹只使用文件名、mtime 与大小，不读取文件内容；目录在确认期间
        发生任何新增/删除/替换都会变化，从而拒绝过期确认。
        """
        if not self.enabled or self._catalog is None:
            raise TableImportUnavailableError()
        with self._catalog.operation_lock:
            files = self._workspace_excel_files()
            digest = hashlib.sha256()
            for path in files:
                stat = path.stat()
                digest.update(f"{path.name}\0{stat.st_mtime_ns}\0{stat.st_size}\n".encode("utf-8"))
            return tuple(path.name for path in files), digest.hexdigest()

    def delete_all_tables(
        self,
        *,
        expected_fingerprint: str | None = None,
    ) -> TableBulkDeletionResult:
        """删除工作区内全部受管 Excel，并清空表目录元数据。

        安全边界与单表删除一致：每个文件都先 ``resolve()`` 并做工作区路径
        前缀校验；工作区外软链接跳过；不删除目录本身，不使用 ``rmtree``。
        """
        if not self.enabled or self._catalog is None:
            raise TableImportUnavailableError()

        directory = self._workspace_root()
        with self._catalog.operation_lock:
            files = self._workspace_excel_files()
            fingerprint = self._fingerprint_files(files)
            if expected_fingerprint is not None and fingerprint != expected_fingerprint:
                raise TableDeletionError("数据表在确认前已发生变化，请重新发起删除全部表")

            snapshot = self._catalog.scan()
            names_by_file = {table.file_name: table.name for table in snapshot.tables}

            index_path = directory / UPLOAD_INDEX_NAME
            manifest_path = directory / TABLE_MANIFEST_NAME
            index_payload = self._load_json_object(index_path)
            manifest_payload = self._load_json_object(manifest_path)

            original_index = self._serialize_json(index_payload)
            original_manifest = self._serialize_json(manifest_payload)

            index_changed = self._clear_upload_index(index_payload)
            manifest_changed = self._clear_manifest(manifest_payload)
            deleted_names = {path.name for path in files}

            quarantined: list[tuple[Path, Path]] = []
            try:
                for path in files:
                    quarantine = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.deleting")
                    path.replace(quarantine)
                    quarantined.append((path, quarantine))

                if index_changed:
                    self._write_json_object(index_path, index_payload or {})
                if manifest_changed:
                    self._write_json_object(manifest_path, manifest_payload or {})
            except (OSError, TableDeletionError) as exc:
                for path, quarantine in reversed(quarantined):
                    try:
                        quarantine.replace(path)
                    except OSError:
                        logger.warning("回滚删除全部表文件失败 file=%s", path.name)
                self._restore_json(index_path, original_index)
                self._restore_json(manifest_path, original_manifest)
                if isinstance(exc, TableDeletionError):
                    raise
                logger.warning("删除全部数据表失败 error=%s", type(exc).__name__)
                raise TableDeletionError() from exc

            for _, quarantine in quarantined:
                try:
                    quarantine.unlink()
                except OSError:
                    logger.warning("清理隔离文件失败 file=%s", quarantine.name)

            self._catalog.invalidate()
            try:
                remaining = tuple(table.name for table in self._catalog.scan().tables)
            except Exception as exc:  # noqa: BLE001 - 删除已完成，重扫失败不应反报失败
                logger.warning("删除全部表后扫描目录失败: %s", type(exc).__name__)
                remaining = ()

        deleted_tables = tuple(
            sorted(names_by_file.get(name, Path(name).stem) for name in deleted_names)
        )
        logger.info("已删除全部数据表 files=%s", len(files))
        return TableBulkDeletionResult(
            deleted_tables=deleted_tables,
            deleted_files=tuple(sorted(deleted_names)),
            remaining_tables=remaining,
            fingerprint=fingerprint,
        )

    @classmethod
    def validate_table_name(cls, table_name: str) -> str:
        """公开的表名白名单校验，供自然语言入口在查表前先行拦截。"""
        return cls._validate_table_name(table_name)

    def _workspace_root(self) -> Path:
        """工作区绝对路径；后续所有待删文件都必须以它为前缀。"""
        return self._directory.resolve()

    @staticmethod
    def _validate_table_name(table_name: str) -> str:
        """拒绝任何路径形态的表名，表目录内只接受纯名称查找。"""
        raw = (table_name or "").strip()
        if not raw or len(raw) > 255:
            raise TableDeletionError(DELETE_SECURITY_MESSAGE)
        if "\x00" in raw or any(ord(char) < 32 for char in raw):
            raise TableDeletionError(DELETE_SECURITY_MESSAGE)
        # 明确拦截路径穿越与路径分隔符；正常中文表名不会包含这些字符。
        if (
            raw in {".", ".."}
            or ".." in raw
            or "/" in raw
            or "\\" in raw
            or raw.startswith("~")
            or Path(raw).is_absolute()
        ):
            raise TableDeletionError(DELETE_SECURITY_MESSAGE)
        return raw

    def _resolve_workspace_file(self, path: Path) -> Path:
        """把待删文件解析为绝对路径，并强制校验位于工作区内。

        ``Path.resolve`` 会展开符号链接，因此可拦截“工作区内软链接指向外部”
        的情况；``relative_to`` 做的是路径分量级前缀匹配，能避免
        ``/tables-evil`` 伪装成 ``/tables`` 的前缀。
        """
        workspace = self._workspace_root()
        if ".." in path.parts:
            raise TableDeletionError(DELETE_SECURITY_MESSAGE)
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(workspace)
        except ValueError as exc:
            raise TableDeletionError(DELETE_SECURITY_MESSAGE) from exc
        if not relative.parts or resolved.suffix.lower() != ".xlsx":
            raise TableDeletionError(DELETE_SECURITY_MESSAGE)
        if not resolved.is_file():
            raise TableDeletionError("数据表文件不存在，已取消删除")
        return resolved

    def _workspace_excel_files(self) -> list[Path]:
        """枚举工作区内可安全删除的 .xlsx，跳过越界软链接与临时文件。"""
        directory = self._workspace_root()
        if not directory.is_dir():
            return []

        files: list[Path] = []
        seen: set[Path] = set()
        for path in sorted(directory.iterdir(), key=lambda item: item.name.lower()):
            if path.name.startswith("~$") or path.suffix.lower() != ".xlsx":
                continue
            try:
                resolved = self._resolve_workspace_file(path)
            except TableDeletionError:
                logger.warning("跳过工作区外或异常路径 file=%s", path.name)
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            files.append(resolved)
        return files

    @staticmethod
    def _fingerprint_files(files: list[Path]) -> str:
        digest = hashlib.sha256()
        for path in files:
            stat = path.stat()
            digest.update(f"{path.name}\0{stat.st_mtime_ns}\0{stat.st_size}\n".encode("utf-8"))
        return digest.hexdigest()

    @staticmethod
    def _serialize_json(payload: dict[str, Any] | None) -> str | None:
        if payload is None:
            return None
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @staticmethod
    def _clear_upload_index(payload: dict[str, Any] | None) -> bool:
        if payload is None:
            return False
        changed = bool(payload)
        payload.clear()
        return changed

    @staticmethod
    def _clear_manifest(payload: dict[str, Any] | None) -> bool:
        if payload is None:
            return False
        changed = payload.get("tables") not in (None, []) or payload.get("schema_groups") not in (
            None,
            [],
        )
        payload["tables"] = []
        payload["schema_groups"] = []
        return changed

    def _same_content_files(self, target: Path) -> list[Path]:
        """查找与目标文件 MD5 相同的所有工作区内 .xlsx。"""
        assert self._catalog is not None
        try:
            target_digest = self._catalog.file_digest(target)
        except OSError:
            return []

        matches: list[Path] = []
        for path in self._directory.glob("*.xlsx"):
            try:
                resolved = self._resolve_workspace_file(path)
                if resolved == target:
                    continue
                if self._catalog.file_digest(resolved) == target_digest:
                    matches.append(resolved)
            except TableDeletionError:
                # 工作区外软链接或异常路径一律跳过，绝不删除。
                logger.warning("跳过工作区外或异常路径 file=%s", path.name)
            except OSError:
                continue
        return matches

    @staticmethod
    def _load_json_object(path: Path) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.warning("%s 无法解析，已取消删除", path.name)
            raise TableDeletionError("数据表元数据损坏，已取消删除") from exc
        if not isinstance(payload, dict):
            raise TableDeletionError("数据表元数据格式错误，已取消删除")
        return payload

    @staticmethod
    def _remove_upload_entries(
        payload: dict[str, Any] | None,
        *,
        deleted_names: set[str],
    ) -> bool:
        if payload is None:
            return False
        changed = False
        for key in list(payload):
            if key in deleted_names:
                payload.pop(key, None)
                changed = True
        return changed

    @staticmethod
    def _remove_manifest_entries(
        payload: dict[str, Any] | None,
        *,
        deleted_names: set[str],
        table_name: str,
    ) -> bool:
        if payload is None:
            return False
        changed = False

        tables = payload.get("tables")
        if isinstance(tables, list):
            filtered: list[Any] = []
            for entry in tables:
                if not isinstance(entry, dict):
                    filtered.append(entry)
                    continue
                entry_file = str(entry.get("file", "")).strip()
                entry_name = str(entry.get("name", "")).strip()
                # 以 file 为准定位被删文件；只有未声明 file 时才按 name 兜底。
                if entry_file in deleted_names or (not entry_file and entry_name == table_name):
                    changed = True
                    continue
                concat_tables = entry.get("concat_tables")
                if isinstance(concat_tables, list) and table_name in concat_tables:
                    entry["concat_tables"] = [name for name in concat_tables if name != table_name]
                    changed = True
                filtered.append(entry)
            payload["tables"] = filtered

        # schema_groups 只是辅助元数据，也要移除已删除表的引用。
        groups = payload.get("schema_groups")
        if isinstance(groups, list):
            for group in groups:
                if not isinstance(group, dict):
                    continue
                group_tables = group.get("tables")
                if isinstance(group_tables, list) and table_name in group_tables:
                    group["tables"] = [name for name in group_tables if name != table_name]
                    changed = True
        return changed

    @staticmethod
    def _write_json_object(path: Path, payload: dict[str, Any]) -> None:
        temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.part")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(path)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise

    @staticmethod
    def _restore_json(path: Path, original: str | None) -> None:
        """写失败时尽力还原元数据；原文件不存在则删除残留副本。"""
        try:
            if original is None:
                path.unlink(missing_ok=True)
                return
            temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.restore")
            temporary.write_text(original, encoding="utf-8")
            temporary.replace(path)
        except OSError:
            logger.warning("还原元数据失败 file=%s", path.name)
