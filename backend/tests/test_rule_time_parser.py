from datetime import date

import pytest

from app.core.exceptions import TimeRangeParseError
from app.services.rule_time_parser import RuleBasedTimeParser

TODAY = date(2026, 10, 29)


@pytest.fixture()
def parser() -> RuleBasedTimeParser:
    return RuleBasedTimeParser(today=TODAY)


@pytest.mark.parametrize(
    ("text", "expected_start", "expected_end"),
    [
        ("帮我查上个月的工单", date(2026, 9, 1), date(2026, 9, 30)),
        ("最近7天的数据", date(2026, 10, 23), date(2026, 10, 29)),
        ("本周汇总", date(2026, 10, 26), date(2026, 10, 29)),
        ("近三个月", date(2026, 7, 30), date(2026, 10, 29)),
        ("2026年9月1日至9月30日", date(2026, 9, 1), date(2026, 9, 30)),
        ("2026-09-01 到 2026-09-15", date(2026, 9, 1), date(2026, 9, 15)),
        ("2026年9月", date(2026, 9, 1), date(2026, 9, 30)),
        ("2025年", date(2025, 1, 1), date(2025, 12, 31)),
    ],
)
def test_parse_common_chinese_expressions(
    parser: RuleBasedTimeParser,
    text: str,
    expected_start: date,
    expected_end: date,
) -> None:
    result = parser.parse(text, today=TODAY)

    assert result.time_range.start_date == expected_start
    assert result.time_range.end_date == expected_end
    assert result.engine == "rule_based"


def test_parse_rejects_text_without_time_range(parser: RuleBasedTimeParser) -> None:
    with pytest.raises(TimeRangeParseError):
        parser.parse("帮我看一下数据", today=TODAY)


def test_parse_rolling_window_rejects_zero(parser: RuleBasedTimeParser) -> None:
    with pytest.raises(TimeRangeParseError):
        parser.parse("近0天", today=TODAY)
