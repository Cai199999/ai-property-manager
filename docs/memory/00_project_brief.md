# 00 项目简报（Project Brief）

> 建立日期：2026-10-30
> 本文件为项目记忆库起点，后续阶段性总结以 `YYYY-MM-DD_<主题>.md` 命名追加到本目录。

## 1. 项目名称与目标

- **名称**：AI 物管助手（`ai-property-manager`）
- **目标**：用自然语言驱动物业工单数据的查询与汇总。用户输入“上个月各项目的报修工单情况”这类口语化描述，系统自动解析时间范围、取数、用 Pandas 计算指标与分组，并输出可读报表（Excel 明细 + Word 可视化报告）。
- **定位**：第一阶段以确定性模拟数据打通全链路；适配器层按生产结构设计，后续可不改 Agent 代码切换到真实 RPA 或 HTTP API 数据源。

## 2. 技术栈

| 层 | 技术 | 端口 |
| --- | --- | --- |
| 前端 | 原生 HTML / CSS / JavaScript + 本地 ECharts 5.6（离线可用） | 8081 |
| 网关 | Java 21 + Spring Boot 3.5.16（Web / Validation / Actuator） | 8080 |
| Agent | Python 3 + FastAPI + Uvicorn | 8000 |
| 数据处理 | Pandas 2.3+、openpyxl | — |
| 报表 | openpyxl（Excel）、python-docx + matplotlib（Word 图表） | — |
| LLM | 多 Provider：DeepSeek / 豆包 / 通义千问（OpenAI 兼容），支持 BYOK；系统 Key 失败时降级本地中文规则解析 | — |
| 沙箱 | bwrap（`CODE_SANDBOX_MODE`，默认 `bwrap`） | — |
| 持久化 | 当前无数据库，仅运行期文件目录 `.runtime/` | — |

固定调用链：**浏览器 8081 → Java 网关 8080 → Python Agent 8000**。浏览器只访问 8080，8000 不对外暴露；查询、动态分析、表目录、上传、下载全部由网关透传/代理。

## 3. 核心功能模块

1. **自然语言时间解析**：`LLMTimeRangeExtractor` → 起止日期；系统 Key 缺失或调用失败时降级 `rule_time_parser`（用户自带 Key 鉴权失败不回退）。
2. **数据源适配**：`DataFetcherRegistry` + `RPAFetcher`（RPA 驱动，当前 `SimulatedRpaExportDriver`）/ `APIFetcher`（第二阶段占位，返回 `501 DATA_SOURCE_NOT_CONFIGURED`）；统一列映射与清洗。
3. **汇总分析**：`summary.py` 用 Pandas 计算指标、按项目/类型/状态分组、生成明细预览。
4. **多表能力**：表目录扫描、多文件上传、同构工单表纵向汇总、多表联查、Excel 原始内容分页预览。
5. **NL → Pandas 代码查询**：`code_generator` 生成代码，`code_safety` 静态校验，`sandbox/runner` 在 bwrap 隔离中执行。
6. **导出能力**：Excel 结果导出（`report_export.py`，TTL + 文件数/大小上限）、Word 可视化报告（`word_exporter.py`，AI 结论 + 图表 + 数据表）。
7. **网关职责**：参数校验、请求 ID、超时、统一错误体、JSON 透传、流式下载代理（并发与大小上限）。
8. **多模型 BYOK**：`llm_provider.py` 工厂统一解析 provider/model/key，用户 Key 优先；时间解析、代码生成、Word 总结共用同一 Provider；日志强制脱敏，响应仅回显 `meta.llm` 元信息。

## 4. 已知边界 / 后续方向

- 当前无数据库；查询历史、用户偏好尚未持久化（如需落地，按全局规范使用 MySQL + SQLAlchemy ORM）。
- 网关不缓存、不重试；后续可按幂等键增加缓存/重试。
- 鉴权依赖本机回环地址，对外开放前必须补身份认证、限流与审计。
- 模拟数据仅用于打通链路，不代表真实业务口径。

## 5. 运行与测试

```bash
./scripts/test-all.sh          # 全链路回归
cd backend && ../.venv/bin/pytest
cd gateway && mvn test
```

Python 测试用 MockTransport 验证 DeepSeek 请求格式，无需真实 Key；Java 测试启动真实 Tomcat + 本地假 Agent 验证转发链路。
