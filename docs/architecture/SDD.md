# AI 物管助手 · 技术设计文档（SDD）

> 版本：v1.0｜日期：2026-10-30｜范围：BYOK 多模型架构
> 读者：参与本项目的 AI Agent 与开发者
> 原则：只记录“从代码里看不出来的约束”。实现细节以代码为准；文档与代码冲突时，先改文档再改代码。

## 0. 一页速览（不可违反的 6 条）

1. 浏览器只访问 **8080**；Python 8000 仅监听本机回环，不对外暴露。
2. Java 网关**不做业务判断**，只做校验、请求 ID、超时、大小限制与透传。
3. API Key 只存在于请求内存中：**不落盘、不进日志、不进错误响应**。
4. 生成代码必须经过 `code_safety` 静态校验 + bwrap 沙箱，禁止在父进程执行。
5. 用户自带 Key 鉴权失败 → **直接 401**，绝不回退系统 Key 或本地规则。
6. 删表是确定性工具调用：命中意图后**不调用 LLM、不生成/执行代码**，直接操作目录并返回 `action_result`。

## 1. 架构拓扑（文字版）

```
浏览器 / 前端静态页 (8081)
   │  HTTP/JSON、multipart
   ▼
Java 网关 Spring Boot (8080)  /api/v1/assistant/*
   │  HTTP/JSON，携带 X-Request-Id
   ▼
Python Agent FastAPI (8000，仅 127.0.0.1)
   ├─▶ 数据源适配器：RPA（当前为模拟导出） / API（占位，501）
   ├─▶ 外部 LLM（OpenAI 兼容 /chat/completions，HTTPS）
   │      deepseek | doubao | qwen
   ├─▶ bwrap 沙箱子进程
   │      stdin: JSON{frames, code, limits} → stdout: JSON{columns, rows}
   └─▶ .runtime/
          rpa_downloads/tables/   多表目录
          exports/                Excel / Word 结果（TTL 清理）
```

**下载反向链路**：浏览器 ← 8080 流式代理 ← 8000 `FileResponse`。浏览器永远拿不到 Python 文件路径。

**职责边界**

| 层 | 负责 | 不负责 |
| --- | --- | --- |
| 前端 8081 | 展示、图表、上传交互、BYOK 设置（localStorage） | 不直连 8000、不持有系统 Key |
| Java 8080 | 入参校验、请求 ID、超时/大小上限、错误体统一、文件流代理 | 不解析业务字段、不填 BYOK 默认值、不调用 LLM |
| Python 8000 | 时间解析、取数、生成代码、沙箱执行、导出、Provider 解析 | 不做用户鉴权（当前单机内网） |

**`/query/code` 主流程**

```
POST /query/code
 → 规则识别删表意图（命中则走 delete_table_tool，直接返回 action_result）
 → 解析 BYOK 覆盖（provider_type / user_api_key / model_name）
 → LLM 解析时间范围（系统 Key 失败可降级规则；用户 Key 失败直接 401）
 → 扫描多表目录（MD5 去重 → 同构分组）
 → LLM 生成 Pandas 代码
 → 静态安全校验 → bwrap 执行（失败带错误反馈重试，最多 3 次）
 → 组装 result / chart_meta → 可选导出 Excel / Word
 → 返回 JSON（含 meta.llm，不含 Key）
```

## 2. 核心接口契约

### 2.1 前端 → Java 网关（唯一对外入口）

| 方法 | 路径 | 请求 | 响应 |
| --- | --- | --- | --- |
| POST | `/api/v1/assistant/query` | `message`(2–500)、`property_name?`(≤100) | 固定业务汇总 `summary` |
| POST | `/api/v1/assistant/query/code` | 见 2.2 | 通用结果表 + 图表元信息，或工具调用 `action_result` |
| GET | `/api/v1/assistant/tables` | — | 表目录、主表、警告 |
| GET | `/api/v1/assistant/tables/preview` | `name`、`offset≥0`、`limit 1–200` | 原始 Excel 分页 |
| POST | `/api/v1/assistant/tables/upload` | multipart：`file` 可重复，≤10 个 / 总大小 ≤20MB | 导入结果 + 最新目录 |
| GET | `/api/v1/assistant/download/{filename}` | 文件名白名单校验 | 文件流（Excel/Word） |

上传约定：网关把文件段统一改名为 `upload_N.xlsx`（ASCII），原始中文文件名通过重复的 `filename` 字段按顺序透传，避免 multipart 编码问题。

上传去重约定：文件内容 MD5 与目录中已有表一致时，**不落盘第二份副本**，直接复用已有表并返回 200，`warnings` 包含“上传成功，但检测到内容与已有表【XXX】完全一致，已自动复用去重”。

### 2.2 `/query/code` 字段契约（BYOK）

| 字段 | 类型 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- | --- |
| `message` | string | 是 | — | 2–500 字符 |
| `property_name` | string? | 否 | null | ≤100 |
| `provider_type` | string? | 否 | Python 侧 `DEFAULT_LLM_PROVIDER`（默认 `deepseek`） | 仅允许 `deepseek/doubao/qwen` |
| `user_api_key` | string? | 否 | 空 → 系统 `.env` 对应 Key | ≤512；**不回显、不落盘** |
| `model_name` | string? | 否 | Provider 默认模型 | ≤128 |

- Java 使用 `CodeQueryRequest` DTO，**不填默认值**，Jackson `SNAKE_CASE` 原样序列化后透传。
- Java → Python 路径相同，附带 `X-Request-Id`。
- Key 优先级：`user_api_key` 非空 > 系统 `.env` 中对应 Provider 的 Key。
- 空字符串等同于“未提供”，不会触发 BYOK 解析。
- 响应只暴露 `meta.llm = {provider_type, model, key_source}`，`key_source ∈ user|system|missing`；任何情况下不返回 Key。
- `/query`（固定业务查询）不接受 BYOK 字段，契约保持不变。

**前端 BYOK 设置**

- 右上角「⚙️ 模型设置」打开居中 Modal：Provider、Model Name、API Key（支持显隐切换）。
- 保存在 `localStorage` 的 `assistant-model-settings`，页面加载时回填。
- 查询时读取配置并附加上述 3 个字段；选择“系统默认”且未填 Key 时，3 个字段均传空字符串，走后端系统 Key。
- 收到 HTTP 401 时 Toast 提示“大模型鉴权失败，请检查您在‘模型设置’中的 API Key”。
- 已知风险：localStorage 明文存储 Key，可被同源 XSS 读取；仅适用于当前单机内网场景。

**工具调用响应（删表）**

```json
{
  "request_id": "...",
  "type": "action_result",
  "action": "delete_table",
  "success": true,
  "message": "已成功删除数据表：工单表_A区",
  "deleted_table": "工单表_A区",
  "remaining_tables": ["工单表_B区", "工单表_C区"],
  "candidates": []
}
```

- 前端看到 `type == "action_result"` 时**不渲染图表/结果表**；成功时 Toast “删除成功”，并重新请求 `/tables` 刷新“当前数据表”。
- 意图不唯一或表名不存在时仍返回 HTTP 200，`success=false`，`message` 说明原因，`candidates` 给出候选；不会执行删除。
- 文件/元数据写入失败返回 `success=false`，不会静默吞错。

### 2.3 统一错误体

```json
{ "request_id": "...", "error": { "code": "...", "message": "...", "details": {} } }
```

| 场景 | HTTP | code |
| --- | --- | --- |
| 参数校验失败 | 422 | `REQUEST_VALIDATION_FAILED` |
| 未知 Provider | 400 | `LLM_PROVIDER_UNSUPPORTED` |
| Key 缺失/无效 | 401 | `LLM_AUTH_FAILED`（网关包装为 `UPSTREAM_SERVICE_ERROR`，状态码保留） |
| 限流 | 429 | `LLM_RATE_LIMITED` |
| LLM 5xx/超时/解析失败 | 502 | `LLM_SERVICE_ERROR` |
| 模型名被拒 | 400 | `LLM_REQUEST_REJECTED` |
| 代码多次失败 | 422 | `CODE_GENERATION_FAILED` |
| 总预算耗尽 | 504 | `CODE_EXECUTION_TIMEOUT` |
| 沙箱不可用 | 503 | `CODE_SANDBOX_UNAVAILABLE` |
| 上传/预览/下载失败 | 422/404/503 | `TABLE_*` / `REPORT_*` |
| 删表：未找到/多义/写失败 | 200（`action_result.success=false`） | — |

> 网关统一把上游错误码包装成 `UPSTREAM_SERVICE_ERROR`，但**原样保留上游 HTTP 状态码**。前端应以状态码区分鉴权失败。

## 3. 数据流与去重规则

### 3.1 导入 → 扫描

1. 网关校验扩展名 `.xlsx`、单文件/总大小、文件数。
2. Python 分块读取（超限立即中断），写入 `.runtime/rpa_downloads/tables/`，同时维护 manifest 与上传索引（原始文件名、工作表、角色）。重复内容在写入前按 MD5 复用已有表，不产生新文件/索引。
3. 每次查询扫描目录：最多 50 个文件，单表 ≤50,000 行 / ≤100 列；单表解析失败只跳过并告警。

### 3.2 MD5 去重（文件级，不是行级）

- 按 1MB 分块计算文件 MD5；缓存键为 `(mtime_ns, size)`，文件未变不重复读盘。
- 相同 MD5 只保留 **1 份**，保留优先级：
  1. manifest 声明的原始文件
  2. 非上传件
  3. 字典序更小的文件名
- 被丢弃文件写入 `warnings`：`文件 X 与 Y 内容相同，已跳过重复表`。
- 哈希读取失败 → 无法判断重复，**按独立表处理**并告警。
- 去重发生在同构分组与 concat **之前**，防止金额等可加指标翻倍。

### 3.3 合并规则（结构驱动，不是 MD5 驱动）

- **同构表**：列名与顺序完全一致的 ≥2 张表归为一组，标记 `concat_recommended=true`。
- 合并由 LLM 生成的 `pd.concat([...], ignore_index=True)` 完成；提示词明确禁止对同构表使用 `pd.merge`。
- 非同构表只提供“同名列”作为 join 线索，不自动 JOIN。
- **重要边界**：MD5 只解决“整份文件重复”。两份文件内容部分重叠但 MD5 不同时，仍会原样 concat，系统不做行级去重。

### 3.4 删表工具（delete_table_tool）

- 规则识别（`table_delete_intent.py`）：必须同时出现删除动词（删除/删掉/移除/去掉等）与结构性对象（表/表格/数据表/Excel 文件等）；包含“表里的数据/记录/行”等行级操作时**不拦截**。
- 表名匹配：先按目录当前表名做归一化别名匹配（忽略空格、下划线、`工单表` 前缀），唯一命中才执行；多命中返回候选，零命中返回“未找到/请指定”。
- 删除范围：目标 `.xlsx` 及目录内所有 **MD5 相同**的历史副本；同步移除 `upload_index.json` 键与 `manifest.json` 的 `tables` 记录，并清理其他表 `concat_tables`、`schema_groups` 中的引用。
- 一致性：导入与删除共用 `TableCatalog.operation_lock`；先把文件重命名为隐藏隔离名，再原子写元数据，最后物理 unlink。元数据写失败会回滚文件与 JSON。
- 安全：只允许删除数据表目录内的 `.xlsx`，解析后的父目录必须等于目录本身，防路径穿越。

### 3.5 执行与结果

```
frames(JSON) + code → bwrap stdin
 → runner 注入 pd/np/source_df/tables → exec 生成代码
 → 结果 DataFrame 限量序列化 → stdout JSON
 → 父进程校验列/行/单元格/字节上限 → 返回前端
```

## 4. 安全与异常边界

### 4.1 API Key 安全

- 传输：仅 JSON body；生产必须 HTTPS，反向代理不得记录请求体。
- 内存：Python 使用 `SecretStr`；`ResolvedProvider.describe()` 只输出 provider/model/key_source。
- 日志：只允许 `provider / model / key_source / status`；`SecretRedactionFilter` 对 `sk-*`、`Bearer`、`api_key|secret_key|user_api_key|password=` 兜底脱敏。
- 持久化：服务端**不写 `.env`、不写数据库、不写文件、不回显**；网关同样不记录请求体。
- 前端：仅保存在 `localStorage`，不发送给任何第三方。
- 降级：用户 Key 失败绝不回退系统 Key。

### 4.2 代码沙箱限制

| 维度 | 规则 |
| --- | --- |
| 命名空间 | `bwrap --unshare-all`（网络/PID/USER/IPC/UTS 隔离）、`--die-with-parent`、`--new-session` |
| 权限 | `--cap-drop ALL`；`/usr`、venv、runner 只读绑定；`/tmp` 为 tmpfs；`--clearenv` |
| 进程 | 独立 Python `-I -B`，stdin/stdout 只传 JSON，`print` 输出被丢弃 |
| rlimit | CPU 5s、地址空间 1.5GB、单文件 1MB、句柄/进程 64、core 0 |
| 静态校验 | 禁止 `import`、`open`、`eval/exec`、`getattr/setattr`、`__builtins__`；禁止 Pandas/NumPy 的 `read_*`/`to_*`/序列化/网络入口 |
| 结果上限 | 5,000 行 × 50 列、单元格 1,000 字符、输出 5MB、payload 20MB |
| 模式 | `CODE_SANDBOX_MODE=bwrap`；`disabled` 或缺少 bwrap 时**直接 503 拒绝执行**，绝不降级为裸跑 |

### 4.3 超时与降级矩阵

| 场景 | 行为 |
| --- | --- |
| 时间解析：系统 Key 缺失/失败 | `LLM_FALLBACK_ENABLED=true` → 本地规则解析 + warning；否则报错 |
| 时间解析：用户 Key 401 | **直接 401**，不降级 |
| 时间解析：用户 Key 5xx/超时 | 502，不静默换规则 |
| 代码生成/执行失败 | 带错误反馈重试，最多 3 次；仍失败 → 422 |
| 总时间预算耗尽 | 504 `CODE_EXECUTION_TIMEOUT` |
| Word 结论失败 | 规则摘要兜底，报告照常生成 |
| Excel/Word 导出失败 | 仅 warning，查询结果照常 200，下载 URL 为空 |
| Agent 不可达 | 网关 503；其他调用异常 502 |

**时间预算**：代码查询总计 50s；单次 LLM 20s；单次沙箱 8s；提取器 30s；Word 总结 10s；网关连接 3s / JSON 读 60s / 下载读 120s。

## 5. AI 协同开发规则

新增 Provider：`llm_provider.py` 注册表 + `config.py` Key/BaseURL/Model + `.env.example` + `test_llm_provider.py`，并同步 `ParserEngine`。

新增字段：Python schema + Java DTO + 透传测试 + 本 SDD 字段表，四者缺一不可。

提交门禁：`backend: pytest`、`gateway: mvn test`、E2E 全量回归；Python 执行 `black .`。

禁止事项：Java 里写业务默认值、日志打印请求体/Key、绕过沙箱执行、用户 Key 失败后静默降级、前端生产环境直连 8000。

## 6. 已知边界与后续方向

1. 网关把上游错误码统一包装为 `UPSTREAM_SERVICE_ERROR`（HTTP 状态码保留）。
2. `user_api_key` 按产品决策放在 JSON body；生产必须 HTTPS，并确保反向代理不记录请求体。
3. 前端在 localStorage 明文保存 Key，仅适用于单机内网；对外开放前应改为服务端加密存储或短时令牌。
4. 无数据库，查询历史与用户偏好未持久化；对外开放前需补身份认证、限流、审计。
5. 豆包默认模型 `doubao-seed-1-6-250615` 为示例值，实际可能需在火山方舟创建推理接入点后填写 endpoint ID。
6. MD5 去重仅覆盖整文件重复，不做行级去重；跨文件部分重叠需在业务口径中明确。
7. 删表会连同同 MD5 的历史副本一起删除；不做回收站，删除前请自行备份。
