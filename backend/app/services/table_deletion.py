"""物理删除数据表，并同步 manifest / upload_index 元数据。"""

from __future__ import annotations

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


@dataclass(frozen=True, slots=True)
class TableDeletionResult:
    """删表结果；remaining_tables 已按目录当前顺序排列。"""

    deleted_table: str
    deleted_files: tuple[str, ...]
    remaining_tables: tuple[str, ...]


class TableDeletionService:
    """删除整张表：文件、上传索引与 manifest 声明保持同步。"""

    def __init__(self, settings: Settings, catalog: TableCatalog | None) -> None:
        self._settings = settings
        self._catalog = catalog
        self._directory = Path(settings.data_table_dir)

    @property
    def enabled(self) -> bool:
        return self._catalog is not None and self._settings.table_directory_enabled

    def delete_table(self, table_name: str) -> TableDeletionResult:
        """删除指定表及其同内容副本，返回删除后的剩余表名。"""
        if not self.enabled or self._catalog is None:
            raise TableImportUnavailableError()

        directory = self._directory.resolve()
        with self._catalog.operation_lock:
            snapshot = self._catalog.scan()
            dataset = snapshot.table_map().get(table_name)
            if dataset is None:
                raise TableNotFoundError()

            target = dataset.path.resolve()
            # 双保险：只允许删除数据表目录内的 .xlsx，防止路径穿越。
            if target.parent != directory or target.suffix.lower() != ".xlsx":
                raise TableDeletionError("数据表路径不合法，已取消删除")

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
                table_name=table_name,
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
                logger.warning("删除数据表失败 table=%s error=%s", table_name, type(exc).__name__)
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

        logger.info("已删除数据表 table=%s files=%s", table_name, len(file_paths))
        return TableDeletionResult(
            deleted_table=table_name,
            deleted_files=tuple(sorted(deleted_names)),
            remaining_tables=remaining,
        )

    def _same_content_files(self, target: Path) -> list[Path]:
        """查找与目标文件 MD5 相同的所有目录内 .xlsx。"""
        assert self._catalog is not None
        try:
            target_digest = self._catalog.file_digest(target)
        except OSError:
            return []

        matches: list[Path] = []
        for path in self._directory.glob("*.xlsx"):
            try:
                resolved = path.resolve()
                if resolved.parent != target.parent or not resolved.is_file():
                    continue
                if self._catalog.file_digest(resolved) == target_digest:
                    matches.append(resolved)
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
