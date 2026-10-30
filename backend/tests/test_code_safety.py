import pytest

from app.services.code_safety import (
    CodeExtractionError,
    UnsafeCodeError,
    extract_python_code,
    sanitize_error_text,
    validate_generated_code,
)


def test_extract_fenced_python_code() -> None:
    result = extract_python_code(
        "```python\nresult_df = source_df.copy()\n```",
        max_chars=1_000,
    )

    assert result.code == "result_df = source_df.copy()"
    assert result.warnings


def test_extract_plain_code_without_fence() -> None:
    result = extract_python_code("result_df = source_df.head(10)", max_chars=1_000)

    assert result.code == "result_df = source_df.head(10)"
    assert result.warnings == ()


def test_extract_prefers_unique_result_block() -> None:
    content = (
        "说明如下：\n\n```text\n不要执行这一段\n```\n\n"
        "```python\nresult_df = source_df.copy()\n```\n"
    )

    result = extract_python_code(content, max_chars=1_000)

    assert "result_df = source_df.copy()" in result.code


def test_extract_rejects_ambiguous_result_blocks() -> None:
    content = (
        "```python\nresult_df = source_df.copy()\n```\n"
        "```python\nresult_df = source_df.head(1)\n```"
    )

    with pytest.raises(CodeExtractionError):
        extract_python_code(content, max_chars=1_000)


@pytest.mark.parametrize(
    "code",
    [
        "import os\nresult_df = source_df.copy()",
        "result_df = source_df.to_csv('/tmp/result.csv')",
        "result_df = pd.read_csv('/etc/passwd')",
        "result_df = open('/etc/passwd').read()",
        "result_df = (1).__class__",
        "result_df = getattr(source_df, 'to_csv')",
        "def build():\n    result_df = source_df.copy()",
        "result_df = source_df.copy()\nresult_df = result_df.eval('1+1')",
    ],
)
def test_validate_rejects_unsafe_or_missing_result(code: str) -> None:
    with pytest.raises(UnsafeCodeError):
        validate_generated_code(code, max_chars=2_000)


def test_validate_accepts_result_in_module_level_branches() -> None:
    """空数据保护常用 if/else、try/except，在模块级赋值应放行。"""
    conditional = """
if source_df.empty:
    result_df = pd.DataFrame(columns=["项目名称", "工单量"])
else:
    result_df = source_df.groupby("项目名称", as_index=False).size()
"""
    try_block = """
try:
    result_df = source_df.head(10)
except Exception:
    result_df = pd.DataFrame()
"""
    with_block = """
with pd.option_context("mode.copy_on_write", True):
    result_df = source_df.copy()
"""

    for code in (conditional, try_block, with_block):
        assert validate_generated_code(code, max_chars=2_000) is not None


def test_validate_accepts_aggregation() -> None:
    tree = validate_generated_code(
        """
result_df = (
    source_df.groupby("项目名称", as_index=False)
    .agg(工单量=("工单编号", "size"))
    .sort_values("工单量", ascending=False)
)
""",
        max_chars=2_000,
    )

    assert tree is not None


def test_sanitize_error_text_hides_absolute_paths() -> None:
    result = sanitize_error_text("FileNotFoundError: /home/admin1/private/data.csv\n密钥 sk-test")

    assert "/home/admin1/private/data.csv" not in result
    assert "<path>" in result
