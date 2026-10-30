# 2026-10-30 BYOK 多模型改造（第一轮：后端）

## 范围

用户自带大模型 API Key（BYOK）的后端架构抽象与网关透传。本轮不动前端，
前端仍按原请求体调用 `/api/v1/assistant/query/code`，行为保持不变。

## 设计决策

1. **统一 Provider 抽象**：新增 `backend/app/services/llm_provider.py`。
   DeepSeek、豆包（火山方舟）、通义千问（DashScope）均走 OpenAI 兼容
   `/chat/completions`，工厂只负责解析 endpoint/model/key 与发起请求。
2. **BYOK 覆盖全部 LLM 调用**：同一请求内的时间解析、Pandas 代码生成、
   Word 报告结论复用同一个 `ResolvedProvider`，避免混用两套 Key。
3. **Key 优先级**：`user_api_key` 非空 > 系统 `.env` 对应 Provider 的 Key。
4. **降级语义**：系统 Key 故障仍按 `LLM_FALLBACK_ENABLED` 降级本地规则；
   用户自带 Key 鉴权失败直接 401，绝不回退系统 Key 或本地规则。
5. **错误矩阵**：未知 provider 400；Key 缺失/无效 401；限流 429；
   上游 5xx/超时/格式异常 502；Provider 拒绝参数（如模型名错误）400。
6. **安全**：请求字段使用 `SecretStr`；Provider 客户端只记录
   provider/model/key_source/status；`core/logging.py` 新增
   `SecretRedactionFilter` 兜底脱敏；响应只回显 `meta.llm` 元信息。
7. **网关**：新增专用 `CodeQueryRequest` DTO，三个 BYOK 字段可选、原样透传；
   `/query` 接口不使用该 DTO，行为不变。

## 主要文件

| 文件 | 变更 |
| --- | --- |
| `backend/app/services/llm_provider.py` | 新增：Provider 注册表、`ResolvedProvider`、`LLMProviderFactory` |
| `backend/app/core/exceptions.py` | 新增 `LLMAuthError`(401)、`LLMProviderUnsupportedError`(400)、`LLMRateLimitError`(429)、`LLMRequestRejectedError`(400) |
| `backend/app/core/logging.py` | 新增密钥脱敏函数与 handler 过滤器 |
| `backend/app/config.py` | 新增 `DEFAULT_LLM_PROVIDER`、豆包/千问的 Key/BaseURL/Model |
| `backend/app/services/deepseek_extractor.py` | 类改名 `LLMTimeRangeExtractor`，支持传入 provider |
| `backend/app/services/code_generator.py` | 类改名 `LLMCodeGenerator`，HTTP 调用下沉到 Provider 工厂 |
| `backend/app/services/word_exporter.py` | 类改名 `LLMReportSummarizer`，复用 Provider |
| `backend/app/services/code_query_agent.py` | 解析 BYOK 覆盖、贯穿三类 LLM 调用、响应增加 `meta.llm` |
| `backend/app/schemas/code_query.py` | 请求新增 3 个可选字段，`CodeQueryMeta.llm` |
| `gateway/.../dto/CodeQueryRequest.java` | 新增 BYOK 请求 DTO |
| `gateway/.../controller|client` | `/query/code` 改用新 DTO，仍为原样透传 |

## 验证结果

- 后端 pytest：**118 passed**（新增 `tests/test_llm_provider.py` 16 例）。
- 网关 `mvn test`：**12 tests, 0 failures**（新增 BYOK 字段透传用例）。
- E2E 全量回归：**20/20 通过**，平均 RT 4.84s，重试 0/20。
- curl（经 8080 网关）：
  - `provider_type=doubao` + 伪造 Key → **HTTP 401**，
    Python 日志 `code=LLM_AUTH_FAILED`，上游状态 401；
  - `provider_type=fake-llm` → **HTTP 400**，`不支持的大模型服务商`；
  - 不传 BYOK 字段 → 正常返回，`meta.llm.key_source=system`；
  - 传真实系统 Key 作为 `user_api_key` → 正常返回，
    `meta.llm.key_source=user`，证明优先级生效。
- 日志检查：网关日志与 Python 后端日志中伪造 Key 命中数均为 **0**。

## 已知边界

- 网关统一异常体仍把上游错误码包装为 `UPSTREAM_SERVICE_ERROR`，
  但 HTTP 状态码原样透传（401/400）。前端可依赖状态码区分鉴权失败。
- `user_api_key` 按需求放在 JSON body；生产环境应强制 HTTPS，
  并确保反向代理不记录请求体。
- 前端尚未提供 Provider / Key / 模型选择 UI，本轮默认走系统配置。
- 豆包默认模型 `doubao-seed-1-6-250615` 为示例值，实际使用可能需要在
  火山方舟控制台创建推理接入点后填写 endpoint ID。
