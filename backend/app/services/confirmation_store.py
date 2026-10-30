"""删除全部表的一次性确认令牌。

令牌只保存在当前 Python 进程内存中：
- 高熵随机值，前端只在确认弹窗存活期间持有；
- 绑定当前表集合指纹，确认前表发生变化则拒绝执行；
- 短 TTL + 单次使用，避免重放和长期悬挂的破坏性操作。

当前服务为单进程部署；若未来改为多 worker，需要把存储替换为 Redis 等
共享实现，不能仅依赖进程内字典。
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from time import monotonic
from typing import Callable


@dataclass(frozen=True, slots=True)
class DeleteAllConfirmation:
    """一次待确认的删除全部表操作。"""

    token: str
    fingerprint: str
    table_count: int
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ConfirmationConsumeResult:
    """令牌消费结果；error 为空表示校验通过。"""

    record: DeleteAllConfirmation | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.record is not None and self.error is None


class DeleteAllConfirmationStore:
    """TTL + 单次使用 + 表集合指纹绑定的内存令牌存储。"""

    def __init__(
        self,
        ttl_seconds: int,
        *,
        tz: tzinfo,
        max_entries: int = 200,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds 必须为正数")
        if max_entries < 1:
            raise ValueError("max_entries 必须为正数")
        self._ttl_seconds = ttl_seconds
        self._tz = tz
        self._max_entries = max_entries
        self._clock = clock
        self._lock = threading.Lock()
        # 字典保持插入顺序，超过上限时从最旧令牌开始淘汰。
        self._records: dict[str, tuple[DeleteAllConfirmation, float]] = {}

    def issue(self, *, fingerprint: str, table_count: int) -> DeleteAllConfirmation:
        """签发新令牌；同一进程内不会出现重复 token。"""
        if table_count < 1:
            raise ValueError("没有可删除的数据表时不应签发确认令牌")

        now_monotonic = self._clock()
        expires_at = datetime.now(self._tz) + timedelta(seconds=self._ttl_seconds)
        with self._lock:
            self._purge_expired(now_monotonic)
            while len(self._records) >= self._max_entries:
                oldest = next(iter(self._records), None)
                if oldest is None:
                    break
                self._records.pop(oldest, None)

            token = secrets.token_urlsafe(32)
            while token in self._records:
                token = secrets.token_urlsafe(32)

            record = DeleteAllConfirmation(
                token=token,
                fingerprint=fingerprint,
                table_count=table_count,
                expires_at=expires_at,
            )
            self._records[token] = (
                record,
                now_monotonic + self._ttl_seconds,
            )
            return record

    def consume(self, token: str, *, fingerprint: str) -> ConfirmationConsumeResult:
        """校验并消费令牌；无论成功与否，该令牌都不会再次可用。"""
        normalized = (token or "").strip()
        if not normalized:
            return ConfirmationConsumeResult(error="确认信息无效，请重新发起删除全部表")

        with self._lock:
            entry = self._records.pop(normalized, None)

        if entry is None:
            return ConfirmationConsumeResult(error="确认信息无效或已使用，请重新发起删除全部表")

        record, expires_monotonic = entry
        if self._clock() >= expires_monotonic:
            return ConfirmationConsumeResult(error="确认已过期，请重新发起删除全部表")
        if not secrets.compare_digest(record.fingerprint, fingerprint):
            return ConfirmationConsumeResult(error="数据表在确认前已发生变化，请重新发起删除全部表")
        return ConfirmationConsumeResult(record=record)

    def _purge_expired(self, now_monotonic: float) -> None:
        expired = [
            token for token, (_, expires_at) in self._records.items() if now_monotonic >= expires_at
        ]
        for token in expired:
            self._records.pop(token, None)
