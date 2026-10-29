# AI 物管助手

自然语言驱动的物业工单查询系统：DeepSeek 解析时间范围 → 数据源适配器获取数据 → Pandas 汇总 → Java 网关统一对外。

系统固定为三段式调用链：**前端 8081 → Java 网关 8080 → Python Agent 8000**。浏览器只访问 8080，8000 不对浏览器暴露；查询、动态分析、表目录、文件上传和 Excel 下载全部由 Java 网关透传。

第一阶段使用 RPA 模拟下载 Excel，模拟文件完全复刻真实「工单汇总查询」导出件的标题行、46 列业务表头和明细版式；适配器层已按生产环境设计，可在不改 Agent 代码的前提下切换到真实 RPA 或 HTTP API。

## 架构

```text
浏览器看板 :8081
  │  只请求 Java 网关，响应中的 download_url 也由网关代理
  ▼
Java Spring Boot 网关 :8080
  │  参数校验、请求 ID、超时、统一错误、JSON 透传、流式下载代理
  ▼
Python FastAPI Agent :8000
  │
  ├─ DeepSeekTimeRangeExtractor  自然语言 -> 起止日期
  │    └─ 无 Key / 调用失败时可降级到本地中文规则解析
  │
  ├─ DataFetcherRegistry
  │    ├─ RPAFetcher  ── RpaExportDriver ── SimulatedRpaExportDriver（第一阶段）
  │    │                 └─ 真实报表 46 列结构 -> 统一工单列映射
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

页面提供三种操作：

- **开始**：输入自然语言后点击“开始”（或按回车），统一调用 `/api/v1/assistant/query/code`，按响应中的 `result.columns/rows` 动态渲染表格，并按 `chart_meta` 渲染柱状图、折线图或饼图；
- **导入文件**：向 `/api/v1/assistant/tables/upload` 一次上传一个或多个 `.xlsx`；页面首次加载不预置任何表，导入后“当前数据表”只展示本次会话导入的文件；
- **Excel 预览**：点击“当前数据表”中的表标签，分页查看该文件的实际单元格内容。表较多时列表横向滚动，不撑破页面。

响应包含 `download_url` 时，页面会显示“下载 Excel”按钮，包含 `word_download_url` 时额外显示“下载 Word 报告”按钮；两者链接都指向 Java 网关的下载接口，前端不会直接访问 8000。

### 5. 调用

```bash
curl -s http://127.0.0.1:8080/api/v1/assistant/query \
  -H 'Content-Type: application/json' \
  -H 'X-Request-Id: demo-001' \
  -d '{
    "message": "汇总上个月的工单数据",
    "property_name": "望京国际研发园"
  }' | python3 -m json.tool
```

浏览器和前端代码不得直连 Agent；`http://127.0.0.1:8000` 只用于本机开发调试。

## API 契约

请求：

```json
{
  "message": "汇总上个月的工单数据",
  "property_name": "望京国际研发园"
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
    "by_project": [{"name": "望京国际研发园", "count": 90, "amount": 82345.67}],
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

## 生成式 Pandas 查询

新增端点 `POST /api/v1/assistant/query/code`，让 DeepSeek 根据自然语言直接生成 Pandas 代码，不再要求模型输出固定字段结构：

```text
自然语言 -> 时间解析 -> RPA/API 获取 source_df
        -> 表结构摘要（列名、dtype、5 行样例）
        -> DeepSeek 生成 result_df 代码
        -> 静态安全校验 -> bwrap 隔离子进程执行
        -> 通用结果表 {columns, rows, row_count}
```

- 只向模型发送列结构和最多 5 行脱敏样例，不发送完整明细、密钥或本机文件。
- 生成代码必须在模块顶层给 `result_df` 赋值；禁止 import、网络、文件、系统命令和双下划线对象访问。
- 代码只在 `bwrap` 子进程中执行，网络和项目目录均不可见；沙箱不可用时直接失败，不会退化为主进程 `exec()`。
- 执行失败会把安全错误和异常信息追加到对话，最多重试 2 次。
- 新端点返回通用结果表，不改变现有 `/api/v1/assistant/query` 的固定业务 Schema。

```bash
curl -s http://127.0.0.1:8080/api/v1/assistant/query/code \
  -H 'Content-Type: application/json' \
  -H 'X-Request-Id: code-demo-001' \
  -d '{"message": "按所属区域统计工单量，用饼图展示"}' \
  | python3 -m json.tool
```

### 动态结果契约

```json
{
  "result": {
    "columns": [
      {"name": "所属区域", "dtype": "object"},
      {"name": "工单量", "dtype": "int64"}
    ],
    "rows": [{"所属区域": "朝阳区", "工单量": 132}],
    "row_count": 1
  },
  "chart_meta": {
    "type": "pie",
    "title": "工单量按所属区域",
    "x": "所属区域",
    "y": ["工单量"],
    "series": [{"name": "工单量", "key": "工单量", "unit": "单"}]
  },
  "download_url": "/api/v1/assistant/download/report_<32位hex>.xlsx",
  "word_download_url": "/api/v1/assistant/download/report_<32位hex>.docx"
}
```

- `chart_meta` 由 Python 根据结果列类型和取值确定，不额外调用大模型；无法判断时回退为 `table`。
- `download_url` 是相对路径，前端只允许拼接 Java 网关地址；网关收到 `/download/{filename}` 后校验文件名，再向 Agent 取文件流。
- `word_download_url` 与 `download_url` 同级且同源；Word 关闭、结果为空或生成失败时为 `null`。
- Java 侧所有动态接口统一返回 `JsonNode`，不定义业务 DTO。Python 新增字段会原样出现在响应里，无需改 Java。
- 大结果只返回前端预览所需行数；完整结果通过 Excel 下载获取。

### 下载与内存安全

网关下载接口 `GET /api/v1/assistant/download/{filename}` 不会把文件读成 `byte[]`：

1. 使用独立 `RestClient` 和 120 秒下载读超时，禁止跟随重定向；
2. 文件名必须匹配 `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.(xlsx|docx)$`，拒绝路径穿越和其他后缀；
   Content-Type 按后缀修正（`.xlsx` / `.docx`），不信任上游返回的 MIME；
3. 用 64 KB 缓冲区把上游 `InputStream` 直接复制到 `HttpServletResponse.getOutputStream()`，堆内存占用与文件大小无关；
4. 超过 `download-max-bytes` 立即中断；`Semaphore` 限制并发下载数，超限返回 429；
5. 透传 `Content-Length`、`Content-Disposition`、`nosniff` 和私有缓存策略，浏览器能显示进度且不会缓存敏感报表。

Python 导出侧使用 openpyxl `write_only` 模式、随机 token 文件名、TTL 清理和最大文件数限制；单元格以 `= + - @` 开头时加单引号，防止 Excel 公式注入。

### Word 可视化报告

`/query/code` 在生成 Excel 之后，会用同一次查询结果再产出一份 `.docx`（`app/services/word_exporter.py`）。依赖 `python-docx` 与 `matplotlib`（Agg 后端，无需图形界面）：

```bash
cd backend && .venv/bin/pip install -r requirements.txt
```

报告结构：

1. 标题与元信息（提问原文、时间范围、数据来源表、请求 ID、生成时间、告警）；
2. **AI 结论**：独立第二次调用 DeepSeek（不占用主查询的重试预算），只发送列结构、最多 20 行 × 30 列截断样例和 `chart_meta`，要求输出 150~250 字纯文本结论；超时 10 秒；
3. 数据明细表：最多 50 行 × 8 列，超出部分提示“完整数据请下载 Excel”；
4. 静态图表：按 `chart_meta` 用 matplotlib 渲染柱状图/折线图/饼图，Top 12 类目，PNG 写入临时文件后内嵌，导出结束即删除。

降级策略（任何一步失败都不影响主查询返回 200）：

- 未配置 `DEEPSEEK_API_KEY` 或总结调用失败：使用规则生成的摘要，并在报告里标注“系统自动摘要”；只有 Key 已配置却调用失败时才在 `meta.warnings` 中追加 `AI 总结生成失败…`；
- Word 生成异常：`word_download_url` 为 `null`，`meta.warnings` 追加 `Word 报告生成失败，可先下载 Excel 结果`；
- 结果为空、`WORD_REPORT_ENABLED=false`：不生成文件，`word_download_url` 为 `null`。

图表渲染受全局锁保护（matplotlib 的 rcParams 与字体缓存不是线程安全的），字体优先使用 Noto Sans CJK SC，未登记时会扫描系统字体目录手动 `addfont()`，仍找不到则记录告警并用 DejaVu Sans 兜底。

启动前确认本机存在 `bwrap`：

```bash
command -v bwrap
```

如果看到 `CODE_SANDBOX_UNAVAILABLE`，说明沙箱条件不满足，接口会拒绝执行而不是降级。

## 多文件汇总与文件导入

### 1. 生成同构 Mock 工单表

```bash
./scripts/generate_mock_tables.py
```

默认输出到 `backend/.runtime/rpa_downloads/tables/`。三个文件模拟不同区域分次导出的工单，**表头与 `report+(5).xlsx` 完全一致（46 列、顺序相同）**，只有数据不同：

| 表 | 行数 | 单号前缀 | schema_group |
| --- | --- | --- | --- |
| 工单表_A区 | 420 | `A-` | `work_orders` |
| 工单表_B区 | 360 | `B-` | `work_orders` |
| 工单表_C区 | 139 | `C-` | `work_orders` |

固定 seed 保证生成结果可复现。旧的外键 Mock（`工单表.xlsx`、`项目表.xlsx`、`人员表.xlsx`）会在生成时删除，`manifest.json` 使用 v2 格式声明同构组：

```json
{
  "version": 2,
  "schema_groups": [
    {
      "name": "work_orders",
      "tables": ["工单表_A区", "工单表_B区", "工单表_C区"],
      "concat_recommended": true
    }
  ]
}
```

> 真实报表没有费用列。为支持“总金额”分析，`TableCatalog` 读取报表同构文件时会在**内存中**派生 `费用金额`：复用 RPA 链路的稳定哈希规则，同一单号每次计算结果一致。磁盘上的 Excel 仍严格保持 46 列，分析表显示 47 列。

### 2. 动态目录与 Prompt

`TableCatalog` 在每次 `/api/v1/assistant/query/code` 请求时扫描目录（按文件 mtime + size 缓存，上传后主动失效）：

- 按有序列名签名自动识别同构组（`homogeneous_groups`），同构表之间不生成 `same_name_hint`，避免把同名列误当连接键；
- Prompt 注入 `tables`（`dict[str, DataFrame]`）、`source_df`（仅主表）、`relationships` 和 `homogeneous_groups`；
- 同构组必须使用 `pd.concat([...], ignore_index=True)` 纵向拼接，**严禁 `pd.merge`**；只有 manifest 声明的外键才允许 join；
- 统计总工单量用 `df["单号"].nunique()`，金额用 `pd.to_numeric(df["费用金额"], errors="coerce").sum()`；
- 所有事实/同构表都按时间、项目条件过滤，`source_row_count` 为过滤后各文件行数之和；
- 用户未写时间时，多表模式默认使用目录内时间列覆盖的完整区间（如 2026-09-01 至 2026-09-30）并在 warnings 中说明；RPA 单表模式仍要求明确时间；
- 目录为空时自动回退原 RPA 单表链路。

沙箱协议仍是多表 JSON 传输：`{"frames": {"工单表_A区": {...}, "工单表_B区": {...}}, "primary_table": "工单表_A区"}`，不挂载项目目录、不联网、不执行 pickle。

### 3. 上传接口（支持多文件）

```bash
curl -s http://127.0.0.1:8080/api/v1/assistant/tables/upload \
  -F 'file=@/path/to/工单表_A区.xlsx' \
  -F 'file=@/path/to/工单表_B区.xlsx' \
  -F 'file=@/path/to/工单表_C区.xlsx' \
  | python3 -m json.tool

curl -s http://127.0.0.1:8080/api/v1/assistant/tables | python3 -m json.tool
```

- 一次请求可重复提交 `file` 字段，默认最多 10 个文件、合计 20 MB；单表仍限制 5 万行、100 列，仅导入第一个非空工作表；
- 响应同时返回 `imported`（第一个文件，兼容旧客户端）和 `imported_tables`（全部成功文件）；部分文件失败时其余文件仍会导入，失败原因放入 `warnings`；
- 校验 xlsx 压缩包条目数与解压总量，文件名只取 basename，落盘名用 UUID；
- 上传成功后写入 `upload_index.json` 并刷新目录；
- Java 网关转发 multipart 时，文件段使用 ASCII 文件名 `upload_1.xlsx`、`upload_2.xlsx`…，原始中文名按顺序通过独立表单字段 `filename` 传给 Python，避免 RFC 2047 encoded-word 导致后缀校验失败；
- 网关限制单文件、文件数量和单请求总大小，Python 侧再次校验压缩包结构，上传件始终按不可信数据处理。

前端“导入文件”按钮支持多选，同样只请求 8080；上传成功后刷新“当前数据表”清单并展示本次导入的全部文件。

### 4. Excel 内容预览

点击“当前数据表”中的任意表标签，页面会分页展示该 Excel 的实际内容：

```bash
curl -s 'http://127.0.0.1:8080/api/v1/assistant/tables/preview?name=%E5%B7%A5%E5%8D%95%E8%A1%A8_A%E5%8C%BA&offset=0&limit=50' \
  | python3 -m json.tool
```

- `limit` 默认 50，范围 1–200；`offset` 从 0 开始，避免一次返回 5 万行；
- 只展示磁盘文件中的真实列，分析阶段派生的 `费用金额` 单独放在 `derived_columns`，不会冒充 Excel 内容；
- 单元格统一转换为字符串/数字/null，时间字段格式化为 `YYYY-MM-DD HH:mm:ss`，前端不会收到 NaN/NaT；
- 数据表不存在返回 `404 TABLE_NOT_FOUND`，多表目录关闭返回 `503 TABLE_PREVIEW_UNAVAILABLE`；
- Java 网关只透传 JSON，前端仍只访问 8080。

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

第一阶段模拟器生成的文件位于 `backend/.runtime/rpa_downloads/`。导出件结构对齐真实报表：

- 工作表：`Sheet0`
- 第 1 行：合并单元格标题「工单汇总查询」
- 第 2 行：46 个业务列名
- 第 3 行起：工单明细
- 列宽、表头底纹、细边框、行高与真实导出件一致

`backend/app/adapters/report_schema.py` 负责识别该结构，并把原始列映射为平台统一的工单列。真实报表没有费用列，模拟费用会按单号稳定生成，保证同一工单多次查询金额一致。

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
| `TABLE_DIRECTORY_ENABLED` | `true` | 是否启用多表目录 |
| `DATA_TABLE_DIR` | `.runtime/rpa_downloads/tables` | 多表扫描目录 |
| `TABLE_MAX_FILES` | `8` | 单次扫描最大表数 |
| `TABLE_UPLOAD_MAX_BYTES` | `20000000` | 单个上传文件上限 |
| `TABLE_UPLOAD_MAX_FILES` | `10` | 单次请求最多上传文件数 |
| `CODE_SANDBOX_MODE` | `bwrap` | 代码执行隔离模式，`disabled` 时拒绝执行 |
| `REPORT_EXPORT_ENABLED` | `true` | 是否生成结果 Excel |
| `REPORT_EXPORT_DIR` | `.runtime/exports` | 导出文件目录 |
| `REPORT_EXPORT_TTL_SECONDS` | `3600` | 导出文件有效期 |
| `REPORT_EXPORT_MAX_FILES` | `100` | 导出目录最大文件数 |
| `REPORT_EXPORT_MAX_BYTES` | `20000000` | 单个导出文件上限 |
| `WORD_REPORT_ENABLED` | `true` | 是否生成 Word 可视化报告 |
| `REPORT_SUMMARY_TIMEOUT_SECONDS` | `10` | AI 结论单次调用超时 |
| `REPORT_WORD_MAX_ROWS` | `50` | Word 数据表最大行数 |
| `REPORT_WORD_MAX_COLUMNS` | `8` | Word 数据表最大列数 |
| `REPORT_CHART_MAX_CATEGORIES` | `12` | Word 图表最多展示类目数 |
| `CORS_ORIGINS` | `http://127.0.0.1:8081,...` | 浏览器访问网关的跨域白名单 |
| `FASTAPI_BASE_URL` | `http://127.0.0.1:8000` | 网关 -> Agent 地址 |
| `gateway.agent.download-max-concurrent` | `16` | 网关并发下载上限（`application.yml`） |
| `gateway.agent.download-max-bytes` | `20000000` | 网关允许透传的下载大小（`application.yml`） |

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
- RPA 模拟器按真实导出件结构生成确定性样例数据，仅用于打通链路，不代表真实业务口径。
- 网关不缓存、不重试；后续可在网关层按幂等键增加缓存/重试策略。
- 前端所有业务请求都走 Java 8080；8000 仅限本机调试，不配置浏览器跨域。
- 动态 JSON 不做字段映射，网关只负责校验、限制大小和原样转发。
- 下载文件由 Python 生成并短期保留，Java 流式代理，不在网关落盘。
- 当前鉴权依赖本机回环地址；对外开放前必须补充身份认证、限流和审计。
