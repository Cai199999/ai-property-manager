"""本地中文日期规则解析器。

用途：
- DeepSeek 未配置或临时不可用时的降级方案；
- 单元测试中的确定性基线；
- 避免「模型不可用 -> 整个助手不可用」的单点故障。
"""

from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

from app.core.exceptions import TimeRangeParseError
from app.schemas.assistant import TimeRange
from app.services.time_range import TimeExtraction

_CN_NUMBER_CHARS = "零一二三四五六七八九十百两"
_NUMBER_PATTERN = rf"(?:\d{{1,4}}|[{_CN_NUMBER_CHARS}]{{1,5}})"


def _chinese_number_to_int(value: str) -> int:
    """转换 1~999 的中文数字，兼容「十二」「三十」「一百二十」。"""
    if value.isdigit():
        return int(value)

    digits = {
        "零": 0,
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    total = 0
    current = 0
    for char in value:
        if char in digits:
            current = digits[char]
        elif char == "十":
            total += (current or 1) * 10
            current = 0
        elif char == "百":
            total += (current or 1) * 100
            current = 0
    return total + current


def _subtract_months(value: date, months: int) -> date:
    """按自然月回退；对 31 号做月末收敛。"""
    total_month = value.year * 12 + (value.month - 1) - months
    year, month_index = divmod(total_month, 12)
    month = month_index + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


class RuleBasedTimeParser:
    """基于关键词和正则的解析器。日期可注入，方便测试。"""

    def __init__(self, today: date | None = None) -> None:
        self._fixed_today = today

    def parse(self, text: str, *, today: date | None = None) -> TimeExtraction:
        current_day = today or self._fixed_today or date.today()
        normalized = re.sub(r"\s+", "", text)

        exact = self._parse_exact_relative(normalized, current_day)
        if exact:
            return exact

        rolling = self._parse_rolling_window(normalized, current_day)
        if rolling:
            return rolling

        explicit = self._parse_explicit_range(normalized, current_day)
        if explicit:
            return explicit

        calendar_expression = self._parse_calendar_expression(normalized, current_day)
        if calendar_expression:
            return calendar_expression

        raise TimeRangeParseError(
            "未识别到时间范围，请补充如「上个月」「近 7 天」「2026-09-01 至 2026-09-30」",
        )

    def _parse_exact_relative(self, text: str, today: date) -> TimeExtraction | None:
        # 顺序必须在「上月」之前判断「上上个月」等更具体表达。
        exact_rules: tuple[
            tuple[tuple[str, ...], callable],
            ...,
        ] = (
            (("今天", "今日", "当天"), lambda: (today, today, "今天")),
            (
                ("昨天", "昨日"),
                lambda: (today - timedelta(days=1), today - timedelta(days=1), "昨天"),
            ),
            (("前天",), lambda: (today - timedelta(days=2), today - timedelta(days=2), "前天")),
            (
                ("本周", "这周", "本星期", "这个星期"),
                lambda: (today - timedelta(days=today.weekday()), today, "本周至今"),
            ),
            (
                ("上周", "上个星期", "上星期"),
                lambda: (
                    today - timedelta(days=today.weekday() + 7),
                    today - timedelta(days=today.weekday() + 1),
                    "上周",
                ),
            ),
            (
                ("本月", "这个月", "当月"),
                lambda: (today.replace(day=1), today, "本月至今"),
            ),
            (
                ("上个月", "上月"),
                lambda: (
                    _subtract_months(today.replace(day=1), 1),
                    today.replace(day=1) - timedelta(days=1),
                    "上个月",
                ),
            ),
            (
                ("本季度", "这个季度"),
                lambda: (
                    date(today.year, ((today.month - 1) // 3) * 3 + 1, 1),
                    today,
                    "本季度至今",
                ),
            ),
            (
                ("上个季度", "上季度"),
                lambda: self._previous_quarter(today),
            ),
            (("今年", "本年"), lambda: (date(today.year, 1, 1), today, "今年至今")),
            (
                ("去年", "上一年"),
                lambda: (date(today.year - 1, 1, 1), date(today.year - 1, 12, 31), "去年"),
            ),
        )

        for keywords, builder in exact_rules:
            if any(keyword in text for keyword in keywords):
                start, end, label = builder()
                return self._result(start, end, f"命中相对时间表达「{label}」", 0.92)
        return None

    @staticmethod
    def _previous_quarter(today: date) -> tuple[date, date, str]:
        current_quarter_start_month = ((today.month - 1) // 3) * 3 + 1
        previous_quarter_end = date(today.year, current_quarter_start_month, 1) - timedelta(days=1)
        previous_quarter_start = date(
            previous_quarter_end.year,
            ((previous_quarter_end.month - 1) // 3) * 3 + 1,
            1,
        )
        return previous_quarter_start, previous_quarter_end, "上季度"

    def _parse_rolling_window(self, text: str, today: date) -> TimeExtraction | None:
        pattern = re.compile(
            rf"(?:最近|近|过去|前)\s*(?P<count>{_NUMBER_PATTERN})\s*"
            r"(?P<unit>天|日|周|星期|礼拜|个月|月|年)"
        )
        match = pattern.search(text)
        if not match:
            return None

        count = _chinese_number_to_int(match.group("count"))
        if count <= 0:
            raise TimeRangeParseError("时间跨度必须大于 0")

        unit = match.group("unit")
        if unit in {"天", "日"}:
            start = today - timedelta(days=count - 1)
            label = f"近{count}天"
        elif unit in {"周", "星期", "礼拜"}:
            start = today - timedelta(days=count * 7 - 1)
            label = f"近{count}周"
        elif unit in {"个月", "月"}:
            start = _subtract_months(today, count) + timedelta(days=1)
            label = f"近{count}个月"
        else:
            try:
                start = today.replace(year=today.year - count) + timedelta(days=1)
            except ValueError:
                # 2 月 29 日回退到平年时取 2 月 28 日。
                start = today.replace(year=today.year - count, day=28) + timedelta(days=1)
            label = f"近{count}年"

        return self._result(start, today, f"命中滚动时间窗口「{label}」", 0.9)

    def _parse_explicit_range(self, text: str, today: date) -> TimeExtraction | None:
        # 支持 2026-09-01 至 2026-09-30 / 2026/9/1 ~ 9/30。
        numeric_pattern = re.compile(
            r"(?:(?P<sy>\d{4})[-/.])?(?P<sm>\d{1,2})[-/.](?P<sd>\d{1,2})"
            r"\s*(?:至|到|~|～|—|－)\s*"
            r"(?:(?P<ey>\d{4})[-/.])?(?P<em>\d{1,2})[-/.](?P<ed>\d{1,2})"
        )
        # 支持 2026年9月1日到9月30日 / 9月1日到9月30日。
        chinese_pattern = re.compile(
            r"(?:(?P<sy>\d{4})年)?(?P<sm>\d{1,2})月(?P<sd>\d{1,2})[日号]?"
            r"\s*(?:至|到|~|～|—|－)\s*"
            r"(?:(?P<ey>\d{4})年)?(?P<em>\d{1,2})月(?P<ed>\d{1,2})[日号]?"
        )

        match = numeric_pattern.search(text) or chinese_pattern.search(text)
        if not match:
            return None

        groups = match.groupdict()
        start_year = int(groups["sy"]) if groups.get("sy") else today.year
        end_year = int(groups["ey"]) if groups.get("ey") else start_year
        start_month = int(groups["sm"])
        end_month = int(groups["em"])

        # 跨年表达，如「12月20日到1月5日」。
        if not groups.get("ey") and end_month < start_month:
            end_year += 1

        try:
            start = date(start_year, start_month, int(groups["sd"]))
            end = date(end_year, end_month, int(groups["ed"]))
        except ValueError as exc:
            raise TimeRangeParseError("日期表达式非法，请检查月份和日期") from exc

        if not groups.get("sy"):
            start = self._infer_year_for_date(start, today)

        return self._result(start, end, "识别到显式日期区间", 0.98)

    def _parse_calendar_expression(self, text: str, today: date) -> TimeExtraction | None:
        # 单日：2026年9月1日 / 9月1日 / 9月1号。
        day_match = re.search(
            r"(?:(?P<year>\d{4})年)?(?P<month>\d{1,2})月(?P<day>\d{1,2})[日号]",
            text,
        )
        if day_match:
            year = int(day_match.group("year") or today.year)
            try:
                target = date(year, int(day_match.group("month")), int(day_match.group("day")))
            except ValueError as exc:
                raise TimeRangeParseError("日期表达式非法，请检查月份和日期") from exc
            if not day_match.group("year"):
                target = self._infer_year_for_date(target, today)
            return self._result(target, target, "识别到单个自然日", 0.96)

        # 整月：2026年9月 / 9月份。
        month_match = re.search(r"(?:(?P<year>\d{4})年)?(?P<month>\d{1,2})月份?", text)
        if month_match:
            year = int(month_match.group("year") or today.year)
            month = int(month_match.group("month"))
            if not 1 <= month <= 12:
                raise TimeRangeParseError("月份必须在 1~12 之间")
            if not month_match.group("year") and month > today.month:
                year -= 1
            start = date(year, month, 1)
            end = date(year, month, calendar.monthrange(year, month)[1])
            if year == today.year and month == today.month:
                end = today
            return self._result(start, end, "识别到自然月", 0.95)

        # 整年：2026年 / 今年已在相对表达中处理。
        year_match = re.search(r"(?<!\d)(?P<year>20\d{2})年", text)
        if year_match:
            year = int(year_match.group("year"))
            start = date(year, 1, 1)
            end = date(year, 12, 31)
            if year == today.year:
                end = today
            return self._result(start, end, "识别到自然年", 0.94)

        return None

    @staticmethod
    def _infer_year_for_date(candidate: date, today: date) -> date:
        """未写年份时不查询未来：大于今天则视为上一年。"""
        return (
            date(candidate.year - 1, candidate.month, candidate.day)
            if candidate > today
            else candidate
        )

    @staticmethod
    def _result(start: date, end: date, reason: str, confidence: float) -> TimeExtraction:
        if start > end:
            raise TimeRangeParseError("开始日期不能晚于结束日期")
        return TimeExtraction(
            time_range=TimeRange(start_date=start, end_date=end),
            engine="rule_based",
            model=None,
            confidence=confidence,
            reason=reason,
        )
