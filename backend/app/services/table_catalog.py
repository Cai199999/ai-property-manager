"""Excel 多表目录扫描与元数据组织。

设计目标：
1. 目录是唯一事实来源，新增/替换 Excel 后无需改代码；
2. ``manifest.json`` 只声明业务语义（主键、外键、时间列），行数、
   dtype、空值数、样例等运行时信息由扫描器实时计算；
3. 上传件没有 manifest 时也能被扫描，但外键只做“同名列”提示，不臆造关系。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from app.adapters.report_schema import add_derived_amount
from app.config import Settings

logger = logging.getLogger(__name__)

TABLE_MANIFEST_NAME = "manifest.json"
UPLOAD_INDEX_NAME = "upload_index.json"

_REPORT_MARKERS = frozenset({"单号", "登记时间", "项目名称", "处理状态"})
_DATE_NAME_RE = re.compile(r"时间|日期|date|time|_at$", re.IGNORECASE)
_DATE_VALUE_RE = re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}")
_ID_NAME_RE = re.compile(r"编号|单号|代码|电话|手机|(^|[^a-z])id($|[^a-z])", re.IGNORECASE)
_KEY_NAME_RE = re.compile(r"编号|单号|代码|(^|[^a-z])id($|[^a-z])", re.IGNORECASE)
_VALID_ROLES = frozenset({"fact", "dimension", "table"})


@dataclass(frozen=True, slots=True)
class ColumnInfo:
    """扫描阶段得到的列摘要，不包含完整数据。"""

    name: str
    dtype: str
    null_count: int


@dataclass(frozen=True, slots=True)
class ForeignKeyInfo:
    """manifest 声明的一条外键。"""

    columns: tuple[str, ...]
    referenced_table: str
    referenced_columns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ParsedTable:
    """一个 Excel 工作表解析后的结果。"""

    frame: pd.DataFrame
    sheet_name: str
    header_row: int
    warnings: tuple[str, ...] = ()
    derived_columns: tuple[str, ...] = ()


@dataclass(slots=True)
class TableDataset:
    """扫描得到的单张表：数据 + 业务元数据。"""

    name: str
    path: Path
    file_name: str
    sheet_name: str
    frame: pd.DataFrame
    columns: tuple[ColumnInfo, ...]
    role: str = "table"
    description: str = ""
    schema_group: str = ""
    derived_columns: tuple[str, ...] = ()
    primary_key: tuple[str, ...] = ()
    time_column: str | None = None
    foreign_keys: tuple[ForeignKeyInfo, ...] = ()
    key_candidates: tuple[str, ...] = ()
    possible_join_keys: tuple[dict[str, Any], ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def row_count(self) -> int:
        return int(len(self.frame))

    @property
    def column_count(self) -> int:
        return int(len(self.columns))


@dataclass(frozen=True, slots=True)
class TableSnapshot:
    """一次目录扫描的完整结果。"""

    tables: tuple[TableDataset, ...]
    primary_table: str | None
    relationships: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...] = ()
    homogeneous_groups: tuple[dict[str, Any], ...] = ()

    @property
    def has_tables(self) -> bool:
        return bool(self.tables)

    def table_map(self) -> dict[str, TableDataset]:
        return {table.name: table for table in self.tables}


@dataclass(slots=True)
class _CachedTable:
    signature: tuple[int, int]
    parsed: ParsedTable


class TableCatalog:
    """扫描 ``data_table_dir``，提供带缓存的表结构快照。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._directory = Path(settings.data_table_dir)
        self._cache: dict[Path, _CachedTable] = {}
        # 文件内容哈希缓存：key 为绝对路径，value 为 (mtime_ns, size) -> md5。
        self._hash_cache: dict[Path, tuple[tuple[int, int], str]] = {}

    @property
    def directory(self) -> Path:
        return self._directory

    def invalidate(self) -> None:
        """上传或删除文件后立即失效缓存。"""
        self._cache.clear()
        self._hash_cache.clear()

    def scan(self) -> TableSnapshot:
        """扫描目录并返回当前所有表的元数据与 DataFrame。"""
        directory = self._directory
        if not directory.exists():
            return TableSnapshot(
                tables=(),
                primary_table=None,
                relationships=(),
                warnings=(),
                homogeneous_groups=(),
            )

        warnings: list[str] = []
        manifest = _load_manifest(directory, warnings)
        upload_index = _load_upload_index(directory, warnings)
        manifest_by_file = {
            str(entry.get("file", "")).strip(): entry
            for entry in manifest
            if str(entry.get("file", "")).strip()
        }
        files = _list_excel_files(
            directory,
            max_files=self._settings.table_max_files,
            warnings=warnings,
        )
        if not files:
            return TableSnapshot(
                tables=(),
                primary_table=None,
                relationships=(),
                warnings=tuple(warnings),
                homogeneous_groups=(),
            )

        parsed_by_file: dict[str, ParsedTable] = {}
        for path in files:
            try:
                parsed = self._read_cached(path)
            except Exception as exc:  # noqa: BLE001 - 单表损坏不应拖垮整个目录
                logger.warning("跳过无法解析的数据表 %s: %s", path.name, type(exc).__name__)
                warnings.append(f"文件 {path.name} 无法解析，已跳过")
                continue
            parsed_by_file[path.name] = parsed

        # 同一份 Excel 被重复上传/复制时，只保留一份参与后续分组与汇总，
        # 否则同构表 concat 会把金额等可加指标成倍放大。
        files = self._dedupe_duplicate_files(
            files,
            manifest_by_file=manifest_by_file,
            upload_index=upload_index,
            warnings=warnings,
        )

        used_names: set[str] = set()
        datasets: list[TableDataset] = []
        for path in files:
            parsed = parsed_by_file.get(path.name)
            if parsed is None:
                continue

            entry = manifest_by_file.get(path.name, {})
            upload_meta = upload_index.get(path.name, {})
            base_name = (
                _text(entry.get("name")) or _text(upload_meta.get("original_stem")) or path.stem
            )
            name = _unique_name(base_name or "未命名表", used_names)
            used_names.add(name)

            role = _text(entry.get("role")).lower() or "table"
            if role not in _VALID_ROLES:
                warnings.append(f"表 {name} 的 role={role!r} 不受支持，已按 table 处理")
                role = "table"

            warnings_for_table = list(parsed.warnings)
            frame = parsed.frame
            columns = tuple(
                ColumnInfo(
                    name=str(column),
                    dtype=str(frame[column].dtype),
                    null_count=int(frame[column].isna().sum()),
                )
                for column in frame.columns
            )
            time_column = _resolve_time_column(
                frame,
                _text(entry.get("time_column")),
                warnings_for_table,
                name,
            )
            primary_key = _resolve_columns(
                frame,
                entry.get("primary_key"),
                f"表 {name} 的主键",
                warnings_for_table,
            )
            foreign_keys = _parse_foreign_keys(
                frame,
                entry.get("foreign_keys"),
                name,
                warnings_for_table,
            )
            key_candidates = _infer_key_candidates(frame, primary_key)

            datasets.append(
                TableDataset(
                    name=name,
                    path=path,
                    file_name=path.name,
                    sheet_name=parsed.sheet_name,
                    frame=frame,
                    columns=columns,
                    role=role,
                    description=_text(entry.get("description")),
                    schema_group=_text(entry.get("schema_group")),
                    derived_columns=parsed.derived_columns,
                    primary_key=primary_key,
                    time_column=time_column,
                    foreign_keys=foreign_keys,
                    key_candidates=key_candidates,
                    warnings=tuple(warnings_for_table),
                )
            )

        homogeneous_groups = _build_homogeneous_groups(datasets)
        _attach_possible_join_keys(datasets, homogeneous_groups)
        _validate_foreign_keys(datasets, warnings)
        primary_table = _choose_primary_table(datasets, manifest_by_file)
        relationships = _build_relationships(datasets)

        return TableSnapshot(
            tables=tuple(datasets),
            primary_table=primary_table,
            relationships=relationships,
            warnings=tuple(_dedupe(warnings)),
            homogeneous_groups=homogeneous_groups,
        )

    def _read_cached(self, path: Path) -> ParsedTable:
        """按 mtime + size 缓存解析结果，文件变化后自动重读。"""
        resolved = path.resolve()
        stat = resolved.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
        cached = self._cache.get(resolved)
        if cached is not None and cached.signature == signature:
            return cached.parsed

        parsed = read_table_file(
            resolved,
            max_rows=self._settings.table_max_rows,
            max_columns=self._settings.table_max_columns,
            max_uncompressed_bytes=self._settings.table_upload_max_uncompressed_bytes,
        )
        self._cache[resolved] = _CachedTable(signature=signature, parsed=parsed)
        return parsed

    def _md5_cached(self, path: Path) -> str:
        """按 mtime + size 缓存文件 MD5，避免每次扫描都读整份 Excel。"""
        resolved = path.resolve()
        stat = resolved.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
        cached = self._hash_cache.get(resolved)
        if cached is not None and cached[0] == signature:
            return cached[1]

        digest = hashlib.md5()
        with resolved.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        value = digest.hexdigest()
        self._hash_cache[resolved] = (signature, value)
        return value

    def _dedupe_duplicate_files(
        self,
        files: list[Path],
        *,
        manifest_by_file: dict[str, dict[str, Any]],
        upload_index: dict[str, dict[str, Any]],
        warnings: list[str],
    ) -> list[Path]:
        """按文件 MD5 去除重复表，只保留一份参与分析。

        保留优先级：manifest 声明的原始文件 > 非上传件 > 字典序更小的文件名。
        这样重复上传件不会进入同构表组，避免 concat 后金额翻倍。
        """
        by_digest: dict[str, list[Path]] = {}
        kept_names: set[str] = set()

        for path in files:
            try:
                digest = self._md5_cached(path)
            except OSError as exc:
                # 哈希失败无法判断重复，保守起见按独立表处理。
                warnings.append(f"文件 {path.name} 无法计算内容哈希，按独立表处理: {exc}")
                kept_names.add(path.name)
                continue
            by_digest.setdefault(digest, []).append(path)

        for candidates in by_digest.values():
            winner = min(
                candidates,
                key=lambda path: _duplicate_priority(
                    path,
                    manifest_by_file=manifest_by_file,
                    upload_index=upload_index,
                ),
            )
            kept_names.add(winner.name)
            losers = [path.name for path in candidates if path.name != winner.name]
            if losers:
                warnings.append(f"文件 {'、'.join(losers)} 与 {winner.name} 内容相同，已跳过重复表")

        return [path for path in files if path.name in kept_names]


def _duplicate_priority(
    path: Path,
    *,
    manifest_by_file: dict[str, dict[str, Any]],
    upload_index: dict[str, dict[str, Any]],
) -> tuple[int, int, str]:
    """内容相同的候选文件中，越小越优先保留。

    manifest 声明的原始文件优先于上传副本，其次是非上传件，最后按文件名
    保证结果稳定可复现。
    """
    return (
        0 if path.name in manifest_by_file else 1,
        1 if path.name in upload_index else 0,
        path.name.lower(),
    )


def read_table_file(
    path: Path,
    *,
    max_rows: int,
    max_columns: int,
    max_uncompressed_bytes: int = 200_000_000,
) -> ParsedTable:
    """读取第一个工作表，自动识别“标题行 + 表头行”或普通单行表头。"""
    _check_xlsx_archive(path, max_uncompressed_bytes)
    workbook = pd.ExcelFile(path, engine="openpyxl")
    try:
        if not workbook.sheet_names:
            raise ValueError("工作簿没有工作表")
        sheet_name = workbook.sheet_names[0]
        preview = pd.read_excel(
            workbook,
            sheet_name=sheet_name,
            header=None,
            nrows=min(15, max_rows + 1),
            dtype=object,
        )
        if preview.empty:
            raise ValueError("工作表为空")

        header_row = _detect_header_row(preview)
        if header_row is None:
            header_row = 0

        header_values = preview.iloc[header_row].tolist()
        data = pd.read_excel(
            workbook,
            sheet_name=sheet_name,
            header=None,
            skiprows=header_row + 1,
            nrows=max_rows + 1,
            dtype=object,
        )
    finally:
        workbook.close()

    warnings: list[str] = []
    data = data.dropna(how="all").reset_index(drop=True)
    if len(data) > max_rows:
        data = data.iloc[:max_rows].copy()
        warnings.append(f"数据超过 {max_rows} 行，已截断")

    width = max(len(header_values), int(data.shape[1]))
    headers = _normalize_headers(header_values, width)
    if len(headers) > max_columns:
        headers = headers[:max_columns]
        warnings.append(f"列数超过 {max_columns}，已截断")

    if data.shape[1] < len(headers):
        data = data.reindex(columns=range(len(headers)))
    else:
        data = data.iloc[:, : len(headers)].copy()
    data.columns = headers

    # 丢弃完全空白的列，但保留有表头的空列，避免误删业务字段。
    keep = [
        column
        for column in data.columns
        if data[column].notna().any() or not str(column).startswith("未命名列")
    ]
    if not keep:
        raise ValueError("工作表没有有效列")
    data = data.loc[:, keep].copy()

    for column in list(data.columns):
        data[column] = _normalize_series(data[column], str(column))

    # 报表同构文件仍保持 46 列；费用金额只在内存分析表中派生，
    # 与 RPA 单表链路使用同一套稳定哈希规则。
    source_columns = {str(column) for column in data.columns}
    data = add_derived_amount(data)
    derived_columns = tuple(
        str(column) for column in data.columns if str(column) not in source_columns
    )

    return ParsedTable(
        frame=data.reset_index(drop=True),
        sheet_name=sheet_name,
        header_row=header_row,
        warnings=tuple(warnings),
        derived_columns=derived_columns,
    )


def _check_xlsx_archive(path: Path, max_uncompressed_bytes: int) -> None:
    """扫描目录中的文件同样做 zip bomb 防护。"""
    try:
        with zipfile.ZipFile(path) as archive:
            total = sum(info.file_size for info in archive.infolist())
    except zipfile.BadZipFile as exc:
        raise ValueError("文件不是有效的 .xlsx 压缩包") from exc
    if total > max_uncompressed_bytes:
        raise ValueError("Excel 解压后体积过大，已拒绝读取")


def table_fingerprint(paths: Iterable[Path]) -> str:
    """生成一组文件的稳定指纹，便于测试或缓存失效判断。"""
    digest = hashlib.blake2s(digest_size=8)
    for path in sorted(paths):
        stat = path.stat()
        digest.update(f"{path.name}:{stat.st_mtime_ns}:{stat.st_size}".encode("utf-8"))
    return digest.hexdigest()


def _list_excel_files(
    directory: Path,
    *,
    max_files: int,
    warnings: list[str],
) -> list[Path]:
    files: list[Path] = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name.lower()):
        if not path.is_file() or path.name.startswith("~$"):
            continue
        if path.suffix.lower() != ".xlsx":
            continue
        resolved = path.resolve()
        if not _within_directory(resolved, directory):
            warnings.append(f"文件 {path.name} 指向目录外，已跳过")
            continue
        files.append(resolved)
    if len(files) > max_files:
        message = f"目录中 Excel 超过 {max_files} 个，仅扫描前 {max_files} 个"
        logger.warning("%s（可通过 TABLE_MAX_FILES 调整上限）", message)
        warnings.append(message)
        files = files[:max_files]
    return files


def _load_manifest(directory: Path, warnings: list[str]) -> list[dict[str, Any]]:
    path = directory / TABLE_MANIFEST_NAME
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        warnings.append("manifest.json 无法解析，已忽略表关系声明")
        return []
    tables = payload.get("tables") if isinstance(payload, dict) else None
    if not isinstance(tables, list):
        warnings.append("manifest.json 缺少 tables 数组，已忽略表关系声明")
        return []
    return [entry for entry in tables if isinstance(entry, dict)]


def _load_upload_index(directory: Path, warnings: list[str]) -> dict[str, dict[str, Any]]:
    path = directory / UPLOAD_INDEX_NAME
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        warnings.append("upload_index.json 无法解析，上传表将使用文件名")
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key): value for key, value in payload.items() if isinstance(value, dict)}


def _detect_header_row(preview: pd.DataFrame) -> int | None:
    """在前 10 行中寻找最像表头的一行。"""
    best_index: int | None = None
    best_score = -1
    limit = min(10, len(preview))
    for index in range(limit):
        values = {value for value in _non_empty_texts(preview.iloc[index].tolist())}
        if len(values) < 2:
            continue

        marker_hits = len(values & _REPORT_MARKERS)
        if marker_hits >= 2:
            return index

        score = len(values) * 2 + marker_hits * 10
        if index + 1 < len(preview):
            if _non_empty_texts(preview.iloc[index + 1].tolist()):
                score += 2
        if score > best_score:
            best_score = score
            best_index = index
    return best_index


def _normalize_headers(values: list[Any], width: int) -> list[str]:
    """清洗表头：空表头补名、重复表头加后缀。"""
    headers: list[str] = []
    counts: dict[str, int] = {}
    for index in range(width):
        raw = values[index] if index < len(values) else None
        name = _text(raw) or f"未命名列{index + 1}"
        name = re.sub(r"\s+", " ", name)
        count = counts.get(name, 0) + 1
        counts[name] = count
        headers.append(name if count == 1 else f"{name}_{count}")
    return headers


def _normalize_series(series: pd.Series, column: str) -> pd.Series:
    """做轻量类型收敛，便于模型写出正确的 merge/agg 代码。"""
    if series.dtype == object:
        series = series.map(lambda value: value.strip() if isinstance(value, str) else value)

    non_null = int(series.notna().sum())
    if non_null == 0:
        return series

    looks_like_date = bool(_DATE_NAME_RE.search(column)) or _date_value_ratio(series) >= 0.9
    if looks_like_date and not _is_identifier_column(column):
        parsed = pd.to_datetime(series, errors="coerce")
        if int(parsed.notna().sum()) >= max(1, int(non_null * 0.8)):
            return parsed

    if not _is_identifier_column(column):
        numeric = pd.to_numeric(series, errors="coerce")
        if int(numeric.notna().sum()) >= max(1, int(non_null * 0.9)):
            return numeric
    return series


def _date_value_ratio(series: pd.Series) -> float:
    values = [str(value).strip() for value in series.dropna().head(50)]
    if not values:
        return 0.0
    matches = sum(1 for value in values if _DATE_VALUE_RE.match(value))
    return matches / len(values)


def _is_identifier_column(column: str) -> bool:
    return bool(_ID_NAME_RE.search(column))


def _non_empty_texts(values: list[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value is None:
            continue
        try:
            if pd.isna(value):
                continue
        except (TypeError, ValueError):
            pass
        text = str(value).strip()
        if text:
            result.append(text)
    return result


def _resolve_columns(
    frame: pd.DataFrame,
    raw: object,
    label: str,
    warnings: list[str],
) -> tuple[str, ...]:
    if raw is None:
        return ()
    values = [raw] if isinstance(raw, str) else list(raw) if isinstance(raw, list) else []
    columns = tuple(str(value).strip() for value in values if str(value).strip())
    missing = [column for column in columns if column not in frame.columns]
    if missing:
        warnings.append(f"{label}中的列 {missing} 不存在，已忽略")
        columns = tuple(column for column in columns if column in frame.columns)
    return columns


def _resolve_time_column(
    frame: pd.DataFrame,
    configured: str,
    warnings: list[str],
    table_name: str,
) -> str | None:
    if configured:
        if configured in frame.columns:
            return configured
        warnings.append(f"表 {table_name} 的时间列 {configured!r} 不存在，已忽略")
    for column in frame.columns:
        if pd.api.types.is_datetime64_any_dtype(frame[column]):
            return str(column)
    for column in frame.columns:
        if _DATE_NAME_RE.search(str(column)):
            return str(column)
    return None


def _parse_foreign_keys(
    frame: pd.DataFrame,
    raw: object,
    table_name: str,
    warnings: list[str],
) -> tuple[ForeignKeyInfo, ...]:
    if not isinstance(raw, list):
        return ()
    result: list[ForeignKeyInfo] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        columns = _resolve_columns(
            frame,
            entry.get("columns"),
            f"表 {table_name} 的外键列",
            warnings,
        )
        reference = entry.get("references")
        if not isinstance(reference, dict):
            warnings.append(f"表 {table_name} 有一条外键缺少 references，已忽略")
            continue
        referenced_table = _text(reference.get("table"))
        referenced_columns = tuple(
            str(value).strip() for value in _as_list(reference.get("columns")) if str(value).strip()
        )
        if not columns or not referenced_table or len(columns) != len(referenced_columns):
            warnings.append(f"表 {table_name} 有一条外键定义不完整，已忽略")
            continue
        result.append(
            ForeignKeyInfo(
                columns=columns,
                referenced_table=referenced_table,
                referenced_columns=referenced_columns,
            )
        )
    return tuple(result)


def _infer_key_candidates(
    frame: pd.DataFrame,
    declared_primary_key: tuple[str, ...],
) -> tuple[str, ...]:
    candidates: list[str] = []
    for column in frame.columns:
        name = str(column)
        if name in declared_primary_key or not _KEY_NAME_RE.search(name):
            continue
        series = frame[column]
        if series.notna().all() and series.is_unique:
            candidates.append(name)
    return tuple(candidates[:5])


def _build_homogeneous_groups(
    datasets: list[TableDataset],
) -> tuple[dict[str, Any], ...]:
    """把列名与顺序完全一致的表归为同构组。

    同构表示“同一业务事实的不同文件”，正确动作是纵向 concat；
    若把它们当成可 join 的表会按同名业务列产生笛卡尔积。
    """
    grouped: dict[tuple[str, ...], list[TableDataset]] = {}
    for table in datasets:
        signature = tuple(str(column.name) for column in table.columns)
        grouped.setdefault(signature, []).append(table)

    groups: list[dict[str, Any]] = []
    for index, (columns, tables) in enumerate(grouped.items(), start=1):
        if len(tables) < 2:
            continue
        declared = {table.schema_group for table in tables if table.schema_group}
        name = declared.pop() if len(declared) == 1 else f"同构表组{index}"
        groups.append(
            {
                "name": name,
                "tables": [table.name for table in tables],
                "columns": list(columns),
                "concat_recommended": True,
            }
        )
    return tuple(groups)


def _attach_possible_join_keys(
    datasets: list[TableDataset],
    homogeneous_groups: tuple[dict[str, Any], ...],
) -> None:
    """给非同构表附上“同名列”提示。

    同构表之间不产生 join 提示，避免模型把相同业务列误当外键。
    """
    homogeneous_pairs: set[frozenset[str]] = set()
    for group in homogeneous_groups:
        names = list(group["tables"])
        for index, left in enumerate(names):
            for right in names[index + 1 :]:
                homogeneous_pairs.add(frozenset((left, right)))

    for index, left in enumerate(datasets):
        hints: list[dict[str, Any]] = []
        left_columns = {column.name for column in left.columns}
        for right in datasets[index + 1 :]:
            if frozenset((left.name, right.name)) in homogeneous_pairs:
                continue
            common = sorted(left_columns & {column.name for column in right.columns})
            if common:
                hints.append(
                    {
                        "left_table": left.name,
                        "right_table": right.name,
                        "columns": common,
                        "source": "same_name_hint",
                    }
                )
        left.possible_join_keys = tuple(hints[:20])


def _validate_foreign_keys(datasets: list[TableDataset], warnings: list[str]) -> None:
    """校验 manifest 外键是否存在，并统计未匹配值。"""
    by_name = {table.name: table for table in datasets}
    for table in datasets:
        valid: list[ForeignKeyInfo] = []
        for foreign_key in table.foreign_keys:
            referenced = by_name.get(foreign_key.referenced_table)
            if referenced is None:
                warnings.append(
                    f"表 {table.name} 的外键引用了不存在的表 {foreign_key.referenced_table}"
                )
                continue
            missing = [
                column
                for column in foreign_key.referenced_columns
                if column not in referenced.frame.columns
            ]
            if missing:
                warnings.append(
                    f"表 {table.name} 的外键引用了 {referenced.name} 中不存在的列 {missing}"
                )
                continue
            valid.append(foreign_key)

            try:
                left_values = {
                    str(value) for value in table.frame[foreign_key.columns[0]].dropna().unique()
                }
                right_values = {
                    str(value)
                    for value in referenced.frame[foreign_key.referenced_columns[0]]
                    .dropna()
                    .unique()
                }
                unmatched = left_values - right_values
                if unmatched:
                    warnings.append(
                        f"表 {table.name}.{foreign_key.columns[0]} 有 "
                        f"{len(unmatched)} 个值在 {referenced.name} 中不存在"
                    )
            except (KeyError, TypeError):
                continue
        table.foreign_keys = tuple(valid)


def _choose_primary_table(
    datasets: list[TableDataset],
    manifest_by_file: dict[str, dict[str, Any]],
) -> str | None:
    if not datasets:
        return None
    for table in datasets:
        if table.role == "fact":
            return table.name
    for table in datasets:
        entry = manifest_by_file.get(table.file_name, {})
        if entry.get("primary") is True:
            return table.name
    return max(datasets, key=lambda table: (table.row_count, table.column_count)).name


def _build_relationships(datasets: list[TableDataset]) -> tuple[dict[str, Any], ...]:
    relationships: list[dict[str, Any]] = []
    for table in datasets:
        for foreign_key in table.foreign_keys:
            relationships.append(
                {
                    "from_table": table.name,
                    "columns": list(foreign_key.columns),
                    "to_table": foreign_key.referenced_table,
                    "to_columns": list(foreign_key.referenced_columns),
                    "source": "manifest",
                }
            )
    return tuple(relationships)


def _within_directory(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory.resolve())
        return True
    except ValueError:
        return False


def _unique_name(name: str, used: set[str]) -> str:
    base = name.strip() or "未命名表"
    if base not in used:
        return base
    index = 2
    while f"{base}_{index}" in used:
        index += 1
    return f"{base}_{index}"


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    return str(value).strip()


def _as_list(value: object) -> list[object]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result
