# 2026-10-30 导入去重修复与自然语言删表工具

## 背景

上传重复 Excel 时，扫描阶段按 MD5 去重会排除新文件，导入服务随后误判为“文件已保存但未能加入数据表目录”，并删除文件。历史目录中同时存在 6 份 `upload_*` 重复副本与 11 条失效上传索引。

## 本次改动

1. **导入去重**
   - `TableImportService.import_bytes` 返回 `TableImportResult(dataset, snapshot, warnings)`。
   - 落盘前先按内容 MD5 查重；命中则删除临时文件、复用已有表，HTTP 200 + warning：
     “上传成功，但检测到内容与已有表【XXX】完全一致，已自动复用去重”。
   - 不再产生重复文件和索引；极端并发下被扫描去重时也会兜底复用并清理。
2. **目录写锁**
   - `TableCatalog.operation_lock`（RLock）由导入与删除共用，避免文件/索引交错。
3. **删表工具**
   - 新增 `table_delete_intent.py`：规则识别删除动词 + 结构性对象；排除“删除表里的数据/记录/行”。
   - 新增 `table_deletion.py`：删除目标 `.xlsx` 及同 MD5 副本；同步移除 `upload_index.json`、`manifest.json` 记录与 `concat_tables` / `schema_groups` 引用；先隔离重命名、再原子写元数据、最后物理删除，失败可回滚。
   - `CodeQueryAgent` 在 LLM/沙箱之前处理删表，返回 `ActionResultResponse`（`type=action_result`），成功消息“已成功删除数据表：XXX”。
   - 前端识别 `action_result`：不渲染图表，Toast “删除成功”，重新请求 `/tables` 刷新当前数据表。
4. **脏数据清理**
   - 备份：`/tmp/ai-property-manager-tables-backup-20261030-102621.tar.gz`
   - 删除 6 份 `upload_*.xlsx` 重复副本，`upload_index.json` 重置为 `{}`；目录仅保留 `工单表_A区/B区/C区.xlsx`。
   - 清理后扫描：3 张表、无重复 warning，行数 420/360/139。

## 验证

- `pytest`：131 passed（新增重复上传、意图识别、删表 API、索引/副本清理等用例）。
- `black` 已执行。
- 前端为原生 HTML/JS，无独立测试框架；已通过真实 API/浏览器手动验证路径待用户执行“删除工单表_A区”。

## 注意

- 删表无回收站，会连同同 MD5 副本一起物理删除。
- Java 网关仍以 `JsonNode` 原样透传，无需感知 `action_result` 字段。
- 剩余风险：`/query/code` 的 `ActionResultResponse` 为 200 + `success=false` 表达业务失败，前端必须以该字段判断，不能只看 HTTP 状态码。
