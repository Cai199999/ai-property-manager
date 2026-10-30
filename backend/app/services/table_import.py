"""Excel 文件导入：校验、落盘、登记元数据并刷新表目录。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any

from app.config import Settings
from app.core.exceptions import TableImportError, TableImportUnavailableError
from app.services.table_catalog import (
    UPLOAD_INDEX_NAME,
    TableCatalog,
    TableDataset,
    TableSnapshot,
    read_table_file,
)

logger = logging.getLogger(__name__)

_SAFE_STEM_RE = re.compile(r"[^\w一-鿿.-]+", re.UNICODE)
_XLSX_SUFFIX = ".xlsx"
_MAX_ZIP_ENTRIES = 1_000


@dataclass(frozen=True, slots=True)
class TableImportResult:
    """一次导入的结果；重复内容会复用已有表并附带 warning。"""

    dataset: TableDataset
    snapshot: TableSnapshot
    warnings: tuple[str, ...] = ()


class TableImportService:
    """把上传文件变成目录中的一张可查询表。"""

    def __init__(self, settings: Settings, catalog: TableCatalog | None) -> None:
        self._settings = settings
        self._catalog = catalog
        self._directory = Path(settings.data_table_dir)

    @property
    def enabled(self) -> bool:
        return self._catalog is not None and self._settings.table_directory_enabled

    def import_bytes(
        self,
        *,
        filename: str,
        content: bytes,
    ) -> TableImportResult:
        """导入一个 xlsx 文件并返回新表与刷新后的目录快照。

        内容 MD5 与目录中已有表一致时，不落盘、不写索引，直接复用已有表，
        并在 warnings 中说明去重原因。
        """
        if not self.enabled or self._catalog is None:
            raise TableImportUnavailableError()

        if not filename or not filename.strip():
            raise TableImportError("缺少文件名")
        if len(content) > self._settings.table_upload_max_bytes:
            raise TableImportError(
                f"文件超过 {self._settings.table_upload_max_bytes / 1_000_000:g} MB 限制"
            )
        if not content:
            raise TableImportError("文件为空")

        original_name = Path(filename).name
        if not original_name.lower().endswith(_XLSX_SUFFIX):
            raise TableImportError("只支持 .xlsx 文件，不支持 .xls、.xlsm 或 CSV")
        if original_name.startswith("~$"):
            raise TableImportError("不能导入 Excel 临时锁文件")

        _validate_xlsx_archive(
            content,
            max_uncompressed=self._settings.table_upload_max_uncompressed_bytes,
        )

        stem = Path(original_name).stem
        safe_stem = _safe_stem(stem)
        stored_name = f"upload_{uuid.uuid4().hex[:12]}_{safe_stem}{_XLSX_SUFFIX}"
        self._directory.mkdir(parents=True, exist_ok=True)
        target = self._directory / stored_name
        temporary = self._directory / f".{stored_name}.part"

        try:
            temporary.write_bytes(content)
            parsed = read_table_file(
                temporary,
                max_rows=self._settings.table_max_rows,
                max_columns=self._settings.table_max_columns,
                max_uncompressed_bytes=self._settings.table_upload_max_uncompressed_bytes,
            )
            if parsed.frame.empty:
                raise TableImportError("文件中没有可导入的数据行")
        except TableImportError:
            temporary.unlink(missing_ok=True)
            raise
        except Exception as exc:  # noqa: BLE001 - 解析库异常统一转成业务错误
            temporary.unlink(missing_ok=True)
            logger.warning("上传文件解析失败: %s", type(exc).__name__)
            raise TableImportError("无法解析 Excel，请确认文件未损坏") from exc

        digest = hashlib.md5(content).hexdigest()

        with self._catalog.operation_lock:
            # 先检查内容是否已存在，避免把重复文件写入目录后再靠扫描剔除。
            snapshot = self._catalog.scan()
            duplicate = self._find_duplicate_table(snapshot, digest)
            if duplicate is not None:
                temporary.unlink(missing_ok=True)
                logger.info(
                    "上传内容与已有表重复，已复用 table=%s",
                    duplicate.name,
                )
                return TableImportResult(
                    dataset=duplicate,
                    snapshot=snapshot,
                    warnings=(self._duplicate_warning(duplicate.name),),
                )

            try:
                temporary.replace(target)
                self._update_upload_index(
                    stored_name=stored_name,
                    original_stem=safe_stem,
                    parsed=parsed,
                    size=len(content),
                )
            except OSError as exc:
                temporary.unlink(missing_ok=True)
                target.unlink(missing_ok=True)
                logger.warning("上传文件保存失败: %s", type(exc).__name__)
                raise TableImportError("文件保存失败，请重试") from exc

            self._catalog.invalidate()
            snapshot = self._catalog.scan()
            dataset = next(
                (table for table in snapshot.tables if table.path.resolve() == target.resolve()),
                None,
            )
            if dataset is None:
                # 兜底：并发或极端情况下仍被去重时，按内容找回已有表，
                # 不留下重复文件和无效索引记录。
                duplicate = self._find_duplicate_table(snapshot, digest)
                try:
                    self._remove_upload_index_entry(stored_name)
                except OSError:
                    logger.warning("清理重复上传的索引记录失败 stored_name=%s", stored_name)
                target.unlink(missing_ok=True)
                self._catalog.invalidate()
                if duplicate is not None:
                    refreshed = self._catalog.scan()
                    reused = self._table_by_name(refreshed, duplicate.name) or duplicate
                    logger.info(
                        "上传内容与已有表重复，已复用 table=%s",
                        reused.name,
                    )
                    return TableImportResult(
                        dataset=reused,
                        snapshot=refreshed,
                        warnings=(self._duplicate_warning(reused.name),),
                    )
                raise TableImportError("文件保存失败，请重试")

        return TableImportResult(dataset=dataset, snapshot=snapshot)

    def _find_duplicate_table(
        self,
        snapshot: TableSnapshot,
        digest: str,
    ) -> TableDataset | None:
        """在已扫描出的表中查找内容 MD5 相同的表。"""
        if self._catalog is None:
            return None
        for table in snapshot.tables:
            try:
                if self._catalog.file_digest(table.path) == digest:
                    return table
            except OSError:
                # 单个文件读取失败不应阻断上传流程。
                continue
        return None

    @staticmethod
    def _duplicate_warning(table_name: str) -> str:
        return f"上传成功，但检测到内容与已有表【{table_name}】完全一致，" "已自动复用去重"

    @staticmethod
    def _table_by_name(
        snapshot: TableSnapshot,
        table_name: str,
    ) -> TableDataset | None:
        return snapshot.table_map().get(table_name)

    def _update_upload_index(
        self,
        *,
        stored_name: str,
        original_stem: str,
        parsed: Any,
        size: int,
    ) -> None:
        index_path = self._directory / UPLOAD_INDEX_NAME
        payload = self._read_upload_index(index_path)
        payload[stored_name] = {
            "original_stem": original_stem,
            "uploaded_at": datetime.now(self._settings.tz).isoformat(),
            "size_bytes": size,
            "sheet_name": parsed.sheet_name,
            "row_count": int(len(parsed.frame)),
            "column_count": int(len(parsed.frame.columns)),
        }
        self._write_upload_index(index_path, payload)

    def _remove_upload_index_entry(self, stored_name: str) -> None:
        index_path = self._directory / UPLOAD_INDEX_NAME
        if not index_path.is_file():
            return
        payload = self._read_upload_index(index_path)
        if payload.pop(stored_name, None) is None:
            return
        self._write_upload_index(index_path, payload)

    @staticmethod
    def _read_upload_index(index_path: Path) -> dict[str, Any]:
        if not index_path.is_file():
            return {}
        try:
            existing = json.loads(index_path.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                return existing
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            logger.warning("upload_index.json 损坏，将重建")
        return {}

    @staticmethod
    def _write_upload_index(index_path: Path, payload: dict[str, Any]) -> None:
        temporary = index_path.parent / f"{index_path.name}.part"
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(index_path)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise


def _validate_xlsx_archive(content: bytes, *, max_uncompressed: int) -> None:
    """限制 zip 条目数与解压总量，避免 xlsx zip bomb 撑爆内存。"""
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            infos = archive.infolist()
            if len(infos) > _MAX_ZIP_ENTRIES:
                raise TableImportError("Excel 内部条目过多，已拒绝导入")
            total = sum(info.file_size for info in infos)
            if total > max_uncompressed:
                raise TableImportError("Excel 解压后体积过大，已拒绝导入")
    except zipfile.BadZipFile as exc:
        raise TableImportError("文件不是有效的 .xlsx 压缩包") from exc


def _safe_stem(stem: str) -> str:
    value = _SAFE_STEM_RE.sub("_", stem).strip("._- ")
    value = value[:40]
    return value or "uploaded"
