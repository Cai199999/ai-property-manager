# 2026-10-30 LLM 删除意图理解与全表删除二次确认

## 背景

旧版 `table_delete_intent.py` 用正则匹配删除动词 + 结构性对象，无法区分“删除工单表_A区”与“删除所有表”，也无法可靠排除“删除表里的数据/记录”。现改为大模型语义理解，并为高风险的全表删除增加服务端一次性令牌确认。

## 本次改动（后端）

1. **意图理解** `backend/app/services/query_understanding.py`
   - 单次 LLM 调用（`temperature=0`、`response_format=json_object`、`max_tokens=400`）同时输出 `intent` 与时间范围。
   - 四类意图：`query` / `delete_specific_table` / `delete_all_tables` / `ambiguous_delete`。
   - 表名必须与当前目录表名**精确匹配**；多表、未知表、路径形态、置信度 < 0.7 一律降级为 `ambiguous_delete`。
   - 用户 Key 401 直接上抛；系统 Key 失败时仅 `query` 可回退规则时间解析。
   - `ExtractorQueryUnderstanding` 兼容旧测试注入的 extractor，固定返回 `query`。
2. **确认令牌** `backend/app/services/confirmation_store.py`
   - `secrets.token_urlsafe(32)`，内存字典，`consume` 原子 pop（单次有效）。
   - TTL `DELETE_CONFIRMATION_TTL_SECONDS`（默认 120s，30–600），容量 200，超限淘汰最旧。
   - 绑定表集合指纹 `SHA-256(filename\0mtime_ns\0size)`，确认时重算比对；令牌内容不写日志。
3. **批量删除** `backend/app/services/table_deletion.py`
   - 新增 `table_set_fingerprint()` 与 `delete_all_tables(expected_fingerprint=...)`。
   - 流程：校验指纹 → 全部重命名隔离 `.{name}.{uuid}.deleting` → 原子写 `upload_index.json={}`、`manifest.tables=[]`、`schema_groups=[]` → unlink → 失效并重扫目录；失败回滚。
   - 不使用 `shutil.rmtree`；跳过 `~$` 与指向工作区外的符号链接。
4. **Agent 与路由**
   - `CodeQueryAgent.handle()` 先解析 BYOK，再调用一次 understanding；删表意图在代码生成/沙箱之前返回。
   - `delete_all_tables` 首次请求只返回 `confirmation_required`（含 token、expires_at、table_count）。
   - 新增 `POST /api/v1/assistant/tables/delete-all/confirm`，body `{"confirmation_token": "..."}`。
   - 删除 `table_delete_intent.py`，避免两套入口并存。
5. **Java 网关**
   - 新增 `DeleteAllConfirmationRequest`（20–256 字符）、`GatewayProperties.deleteAllConfirmPath`、`application.yml` 配置与控制器端点，纯 JSON 透传，不记录令牌。

## 验证

- `pytest`：165 passed（新增 `test_query_understanding.py`、`test_confirmation_store.py` 及 8 个批量删除/令牌用例）。
- `black --check`：68 files unchanged。
- `mvn test`（gateway）：15 passed（新增确认令牌透传与非法令牌本地拦截）。
- 隔离目录 E2E（端口 8020 + 临时 `DATA_TABLE_DIR`）：首次请求返回 `confirmation_required`（table_count=3）；取消后表仍在；确认后 `action_result.success=true`，3 个 `.xlsx` 删除、`manifest.tables=[]`、`upload_index={}`；令牌复用返回 `success=false`。
- 生产工作区验证：`删除所有数据表` 返回 `confirmation_required`；伪造令牌返回 `success=false` 且 3 张表保持原样；短令牌被网关 400 拦截。

## 前端二次确认（同日完成）

- `frontend/index.html` 的 `/query/code` 响应处理新增 `type === "confirmation_required"` 分支，优先于图表/结果解析，命中后只弹窗。
- 新增 `#deleteAllModal`（`role="alertdialog"`）：⚠️ 图标 + 后端 `message` + “将删除 N 张数据表，删除后无法恢复。”；按钮为灰色【取消】与红色【确认清空（不可逆）】，窄屏自动换行。
- 令牌只存 `pendingDeleteAllToken` 内存变量；取消/关闭/遮罩点击/Esc 均丢弃令牌且不发请求。
- 确认时禁用两个按钮并显示“正在删除…”，POST `/api/v1/assistant/tables/delete-all/confirm`；成功则 Toast、清空图表/表格/下载链接、本地清空目录并刷新 `/tables`；业务失败或 HTTP 失败均 Toast 后关闭弹窗。
- `action_result.action === "none"`（指代不明）固定 Toast “请明确指定数据表的名称（例如：工单表_A区）”，后端原始原因显示在错误区。
- 弹窗未关闭时禁止发起新查询，避免覆盖待确认令牌。

## 验证（前端）

- 内联脚本 `node --check` 通过；DOM id 引用全部唯一且存在。
- 网关 `OPTIONS` 预检确认 `/tables/delete-all/confirm` 在 CORS 白名单内。
- 浏览器点击流程由用户在 8081 实测（当前环境无 headless 浏览器）。

## 注意

- “把全部工单表都删掉”这类隐含多表的说法会被判为 `ambiguous_delete` 并列出候选——这是有意的保守行为。
- 令牌只在单进程内存中；服务重启或多实例部署需重新发起。
