# 2026-10-30 本地 Ollama（qwen2.5:7b）接入测试与选型结论

## 目的

验证把 LLM 后端从云端 DeepSeek 切到局域网 Ollama 后，代码查询链路（意图理解 → Pandas 代码生成 → 沙箱执行 → 报告导出）是否可用，为后续「本地小模型 / 云端大模型」选型留下数据。

## 测试环境

| 项 | 值 |
|---|---|
| Ollama 宿主 | Windows 宿主机，`http://192.168.10.197:11434` |
| 模型 | `qwen2.5:7b`（4.68 GB，`/api/tags` 确认） |
| 接口 | OpenAI 兼容 `/v1/chat/completions`，支持 `response_format=json_object` |
| 后端 | uvicorn 8000，`DATA_SOURCE=rpa`，3 张工单表共 919 行 |
| 网关 | Java 8080，读超时临时放宽为 330s（`GATEWAY_AGENT_READ_TIMEOUT`） |

> 注意：本项目按 Provider 命名配置，**不存在** `LLM_BASE_URL / LLM_MODEL_NAME / LLM_API_KEY`。
> 默认 Provider 是 `deepseek`，因此本地测试是复用 `DEEPSEEK_*` 三个字段指向 Ollama（Key 填 `ollama`）。
> 原云端配置备份在 `~/.local/share/ai-property-manager/env.deepseek.bak`。

## 测试数据

### 1. 直连连通

- `POST /v1/chat/completions`：HTTP 200。
- 冷启动首次 11.4s，热态 1.8s（6 tokens）。
- JSON 模式正常。

### 2. 经网关（8080）的查询

| 轮次 | 请求 | HTTP | 耗时 | 代码生成 | Self-Correction |
|---|---|---|---|---|---|
| #1 | 统计当前所有表的总行数 | 200 | **136.3s** | 首次即生成合法 pandas 代码（`attempts=1`） | 未触发 |
| #2 | 统计2026年9月所有表的总行数 | **502** | 133s | 代码生成 >120s（`CODE_LLM_TIMEOUT_SECONDS`）被切断 | 无代码可修 |

#1 生成的代码本身有效：

```python
group = ["工单表_A区", "工单表_C区", "工单表_B区"]
orders = pd.concat([tables[name] for name in group], ignore_index=True)
result_df = pd.DataFrame([{"总行数": int(orders.shape[0])}])
```

但**返回 0 行**：模型把“当前”理解成 `time_range = 2026-10-30 ~ 2026-10-30`（当天），919 行数据全被过滤。
同轮 Word 报告总结在 10s 内未返回（该调用有独立短超时），走了兜底摘要。

### 3. 临时放宽的超时（仅本地模式）

```env
DEEPSEEK_TIMEOUT_SECONDS=120   # 原 30
CODE_LLM_TIMEOUT_SECONDS=120   # 原 20
CODE_TOTAL_TIMEOUT_SECONDS=180 # 原 50（配置上限 180）
```

## 结论

1. **链路可用**：7B 模型能一次生成合法的 pandas 代码，沙箱执行成功，说明架构对模型是解耦的。
2. **不适合做查询主模型**：
   - 单次耗时约 136s，是 DeepSeek（约 6s）的 20 倍以上；前端 95s `AbortController` 会先超时。
   - 时间语义不可靠（“当前”被判成当天），会静默产出错误结果，比报错更危险。
   - 长代码生成会超过 120s，直接 502。
3. **适合的场景**：离线环境跑通链路、演示意图识别与二次确认流程、无外网时做冒烟测试。
4. 若坚持本地代码生成，可换 `qwen2.5-coder:7b`（代码准确率更高但更慢）；无论如何仍需放宽前端 95s 中断。

## 本地 / 云端切换清单

| 配置项 | 本地 Ollama | 云端 DeepSeek |
|---|---|---|
| `DEEPSEEK_API_KEY` | `ollama`（任意非空） | 真实 `sk-…` |
| `DEEPSEEK_BASE_URL` | `http://192.168.10.197:11434/v1` | `https://api.deepseek.com` |
| `DEEPSEEK_MODEL` | `qwen2.5:7b` | `deepseek-chat` |
| `DEEPSEEK_TIMEOUT_SECONDS` | 120 | 30 |
| `CODE_LLM_TIMEOUT_SECONDS` | 120 | 20（默认） |
| `CODE_TOTAL_TIMEOUT_SECONDS` | 180 | 50（默认） |
| 网关读超时 | 启动带 `GATEWAY_AGENT_READ_TIMEOUT=330s` | 默认 60s |
| 前端中断 | 需从 95s 放宽到 200s+ | 95s 足够 |

切回云端后回归：`统计当前所有表的总行数` → HTTP 200、约 6.2s、919 行、`attempts=1`。
