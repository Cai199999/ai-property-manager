from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import Settings
from app.main import create_app


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "test",
        "data_source": "rpa",
        "rpa_download_dir": tmp_path / "downloads",
        "rpa_simulate_delay_seconds": 0.0,
        "deepseek_api_key": SecretStr(""),
        "llm_fallback_enabled": True,
        "max_query_days": 366,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_query_uses_fallback_parser_and_returns_pandas_summary(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/assistant/query",
            headers={"X-Request-Id": "test-request-001"},
            json={"message": "查询2026年9月1日至2026年9月5日的工单", "property_name": "樱花苑"},
        )

    assert response.status_code == 200
    assert response.headers["X-Request-Id"] == "test-request-001"
    body = response.json()
    assert body["request_id"] == "test-request-001"
    assert body["data_source"] == "rpa"
    assert body["parser"]["engine"] == "rule_based"
    assert body["time_range"] == {
        "start_date": "2026-09-01",
        "end_date": "2026-09-05",
    }
    assert body["summary"]["total_records"] > 0
    assert body["records_preview"]
    assert body["meta"]["warnings"]
    assert list((tmp_path / "downloads").glob("*.xlsx"))


def test_query_without_time_range_returns_422(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/assistant/query",
            json={"message": "帮我看一下数据"},
        )

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "TIME_RANGE_INVALID"


def test_invalid_request_is_wrapped_in_unified_error(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))

    with TestClient(app) as client:
        response = client.post("/api/v1/assistant/query", json={"message": ""})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REQUEST_VALIDATION_FAILED"


def test_api_source_is_explicitly_not_configured(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path, data_source="api"))

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/assistant/query",
            json={"message": "最近7天"},
        )

    assert response.status_code == 501
    assert response.json()["error"]["code"] == "DATA_SOURCE_NOT_CONFIGURED"


def test_health_and_sources(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))

    with TestClient(app) as client:
        health = client.get("/health")
        sources = client.get("/api/v1/assistant/sources")

    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert sources.json()["available"] == ["api", "rpa"]
