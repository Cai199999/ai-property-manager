from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest

from app.services.confirmation_store import DeleteAllConfirmationStore


class _Clock:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _store(clock: _Clock, *, ttl: int = 120, max_entries: int = 200):
    return DeleteAllConfirmationStore(
        ttl,
        tz=ZoneInfo("Asia/Shanghai"),
        max_entries=max_entries,
        clock=clock,
    )


def test_issue_and_consume_binds_fingerprint() -> None:
    clock = _Clock()
    store = _store(clock)

    record = store.issue(fingerprint="fp-1", table_count=3)
    result = store.consume(record.token, fingerprint="fp-1")

    assert result.ok
    assert result.record is not None
    assert result.record.table_count == 3
    assert record.expires_at > record.expires_at - timedelta(seconds=1)


def test_token_is_single_use() -> None:
    clock = _Clock()
    store = _store(clock)
    token = store.issue(fingerprint="fp-1", table_count=3).token

    assert store.consume(token, fingerprint="fp-1").ok
    replay = store.consume(token, fingerprint="fp-1")

    assert not replay.ok
    assert "已使用" in (replay.error or "")


def test_token_expires() -> None:
    clock = _Clock()
    store = _store(clock, ttl=30)
    token = store.issue(fingerprint="fp-1", table_count=3).token

    clock.advance(30)
    result = store.consume(token, fingerprint="fp-1")

    assert not result.ok
    assert "过期" in (result.error or "")


def test_fingerprint_change_rejects_confirmation() -> None:
    clock = _Clock()
    store = _store(clock)
    token = store.issue(fingerprint="fp-1", table_count=3).token

    result = store.consume(token, fingerprint="fp-2")

    assert not result.ok
    assert "已发生变化" in (result.error or "")


def test_unknown_and_empty_tokens_are_rejected() -> None:
    store = _store(_Clock())

    assert not store.consume("not-a-token", fingerprint="fp-1").ok
    assert not store.consume("   ", fingerprint="fp-1").ok


def test_issue_requires_at_least_one_table() -> None:
    store = _store(_Clock())

    with pytest.raises(ValueError):
        store.issue(fingerprint="fp-1", table_count=0)


def test_store_evicts_oldest_when_capacity_reached() -> None:
    clock = _Clock()
    store = _store(clock, max_entries=2)
    first = store.issue(fingerprint="fp-1", table_count=1).token
    second = store.issue(fingerprint="fp-2", table_count=1).token
    third = store.issue(fingerprint="fp-3", table_count=1).token

    assert not store.consume(first, fingerprint="fp-1").ok
    assert store.consume(second, fingerprint="fp-2").ok
    assert store.consume(third, fingerprint="fp-3").ok
