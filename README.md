# AI 物管助手（第一阶段）

自然语言驱动的物业工单查询系统：DeepSeek 解析时间范围 → 数据源适配器获取数据 → Pandas 汇总 → Java 网关统一对外。

第一阶段使用 RPA 模拟下载 Excel，但适配器层已按生产环境设计，可在不改 Agent 代码的前提下切换到真实 RPA 或 HTTP API。

## 架构

```text
调用方
  │  POST /api/v1/assistant/query
  ▼
Java Spring Boot 网关 :8080
  │  校验参数、请求 ID 透传、超时控制、统一错误
  ▼
Python FastAPI Agent :8000
  │
  ├─ DeepSeekTimeRangeExtractor  自然语言 -> 起止日期
  │    └─ 无 Key / 调用失败时可降级到本地中文规则解析
  │
  ├─ DataFetcherRegistry
  │    ├─ RPAFetcher  ── RpaExportDriver ── SimulatedRpaExportDriver（第一阶段）
  │    └─ APIFetcher  ── 第二阶段占位实现
  │
  └─ Pandas 汇总（指标 + 项目/类型/状态分组 + 明细预览）
```

### 目录

```text
ai-property-manager/
├── backend/
│   ├── app/
│   │   ├── adapters/        # DataFetcher 接口、RPA/API 实现、统一数据清洗
│   │   ├── api/             # FastAPI 路由与统一异常
│   │   ├── domain/          # 工单标准列契约
│   │   ├── schemas/         # Pydantic 请求/响应
│   │   ├── services/        # DeepSeek、规则解析、Pandas 汇总、Agent 编排
│   │   ├── config.py
│   │   └── main.py
│   ├── tests/
│   ├── .env                 # 本机配置（不提交）
│   └── .env.example
├── gateway/                 # Spring Boot 3.5 / Java 21
│   └── src/
├── frontend/                # 原生 HTML/CSS/JS + 本地 ECharts
│   ├── data/                # 由 API 响应生成的数据快照
│   ├── vendor/              # 本地 ECharts（离线可用）
│   └── index.html           # 工单月报看板
├── scripts/
└── .venv/                   # 使用 --system-site-packages 创建
```

## 快速启动

### 1. 配置 DeepSeek

编辑 `backend/.env`：

```dotenv
DEEPSEEK_API_KEY=sk-xxxxxxxx
```

未配置 Key 时，默认使用本地规则解析，并在响应 `meta.warnings` 中明确标注。
生产环境建议设置 `LLM_FALLBACK_ENABLED=false`，让模型故障显式失败。

### 2. 启动 Python Agent

```bash
cd /home/admin1/Workspace/ai-property-manager
./scripts/start-backend.sh
```

- Swagger：http://127.0.0.1:8000/docs
- 健康检查：http://127.0.0.1:8000/health

### 3. 启动 Java 网关

另开终端：

```bash
cd /home/admin1/Workspace/ai-property-manager
./scripts/start-gateway.sh
```

网关健康检查：http://127.0.0.1:8080/actuator/health

### 4. 打开可视化看板

```bash
cd /home/admin1/Workspace/ai-property-manager
./scripts/start-frontend.sh
```

访问 http://127.0.0.1:8081/

看板读取 `frontend/data/assistant-response.js` 中的 API 响应快照，用 ECharts 绘制报修类型饼图，并提供核心指标、图例数值、可展开数据表、明暗主题与纹理辅助。更新数据时，将新的 `/api/v1/assistant/query` 响应写入该文件即可。

### 5. 调用

```bash
curl -s http://127.0.0.1:8080/api/v1/assistant/query \
  -H 'Content-Type: application/json' \
  -H 'X-Request-Id: demo-001' \
  -d '{
    "message": "汇总上个月的工单数据",
    "property_name": "樱花苑"
  }' | python3 -m json.tool
```

也可直连 Agent：`http://127.0.0.1:8000/api/v1/assistant/query`。

## API 契约

请求：

```json
{
  "message": "汇总上个月的工单数据",
  "property_name": "樱花苑"
}
```

`property_name` 可选；`message` 必填，2~500 字符。

响应（节选）：

```json
{
  "request_id": "demo-001",
  "query": "汇总上个月的工单数据",
  "data_source": "rpa",
  "time_range": {
    "start_date": "2026-09-01",
    "end_date": "2026-09-30"
  },
  "parser": {
    "engine": "deepseek",
    "model": "deepseek-chat",
    "confidence": 0.98,
    "reason": "用户指定上个月"
  },
  "summary": {
    "total_records": 90,
    "total_amount": 82345.67,
    "completed_count": 70,
    "completion_rate": 0.7778,
    "average_satisfaction": 4.21,
    "by_project": [{"name": "樱花苑", "count": 90, "amount": 82345.67}],
    "by_repair_type": [],
    "by_status": []
  },
  "records_preview": [],
  "meta": {
    "fetched_at": "2026-10-29T12:00:00+08:00",
    "elapsed_ms": 450,
    "row_count": 90,
    "source_file": "property_work_orders_....xlsx",
    "warnings": []
  }
}
```

错误响应统一为：

```json
{
  "request_id": "demo-001",
  "error": {
    "code": "TIME_RANGE_INVALID",
    "message": "无法从请求中识别有效的数据时间范围",
    "details": {}
  }
}
```

## 扩展数据源

1. 在 `backend/app/adapters/` 新建类，实现 `DataFetcher.fetch()`。
2. 输出必须包含 `app/domain/work_orders.py` 定义的标准列。
3. 调用 `normalize_work_order_frame()` 统一时间过滤与类型。
4. 在 `create_default_registry()` 中注册，例如：

```python
registry.register("mcp", lambda settings: McpFetcher(settings))
```

5. 设置 `DATA_SOURCE=mcp`。Agent、API、Java 网关完全无需改动。

### 替换模拟 RPA

实现 `RpaExportDriver` 协议（`download_work_orders`），用 Playwright 完成登录、筛选、导出，返回下载后的 Excel 路径，然后注入 `RPAFetcher(download_dir=..., driver=PlaywrightRpaExportDriver(...))`。

第一阶段模拟器生成的文件位于 `backend/.runtime/rpa_downloads/`。

### 接入真实 API

补全 `backend/app/adapters/api_fetcher.py` 中的 TODO：请求 API、JSON 转标准 DataFrame、调用统一清洗，然后设置：

```dotenv
DATA_SOURCE=api
PROPERTY_API_BASE_URL=https://your-property-system/api
PROPERTY_API_KEY=...
```

当前 APIFetcher 会明确返回 `501 DATA_SOURCE_NOT_CONFIGURED`，不会静默返回错误数据。

## 配置项

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATA_SOURCE` | `rpa` | `rpa` / `api` |
| `DEEPSEEK_API_KEY` | 空 | DeepSeek 密钥 |
| `DEEPSEEK_MODEL` | `deepseek-chat` | 模型名 |
| `LLM_FALLBACK_ENABLED` | `true` | 模型失败时是否本地降级 |
| `MAX_QUERY_DAYS` | `366` | 单次查询最大天数 |
| `PREVIEW_ROWS` | `20` | 响应中明细预览行数 |
| `TIMEZONE` | `Asia/Shanghai` | 相对日期计算时区 |
| `RPA_DOWNLOAD_DIR` | `.runtime/rpa_downloads` | 模拟导出目录 |
| `FASTAPI_BASE_URL` | `http://127.0.0.1:8000` | 网关 -> Agent 地址 |

## 测试

```bash
cd /home/admin1/Workspace/ai-property-manager
./scripts/test-all.sh
```

Python 测试使用 MockTransport 验证 DeepSeek 请求格式，无需真实 Key；Java 测试启动真实 Tomcat 和本地假 Agent 服务，验证完整转发链路。

单独执行：

```bash
cd backend && ../.venv/bin/pytest
cd gateway && mvn test
```

## 设计边界

- 当前没有数据库；第一阶段只读 Excel 并即时汇总。
- RPA 模拟器生成确定性样例数据，仅用于打通链路，不代表真实业务口径。
- 网关不缓存、不重试；后续可在网关层按幂等键增加缓存/重试策略。
- 第一阶段不包含前端；FastAPI 与网关均已提供稳定 JSON 契约。
