# Agent Runtime & Evaluation Platform

这是从两个冻结项目之后继续建设的 V2 基线：把 provider-neutral Planning Loop、异步工具批次、预算、权限、SQLite checkpoint、人工暂停/恢复、统一 Trace 和成对回归门禁放进同一套小型平台。

当前状态是 `0.5.2.dev0`，已加入 PostgreSQL backend、显式异步连接池生命周期、fenced state writes、保守崩溃恢复和舆情 Agent 薄适配器。首次远端 PostgreSQL service job 已真实通过；完整 CI 仍需在可选依赖隔离修复后复验，不声称已经达到生产 SLA。

## 当前能力

- 同步或异步工具统一注册；
- 独立工具批次使用 `asyncio.gather` 并发执行；
- 工具白名单、步骤/调用次数双预算和危险工具审批；
- 每一步保存 SQLite checkpoint 与 SHA-256；
- `pause_for_review` / `resume`；
- Run、Step、ToolCall、Error 统一数据结构；
- baseline/candidate 成对回归门禁；
- 不依赖具体模型供应商的 Planner Protocol。
- 带心跳的单 run 执行租约与确定性幂等键；
- 已完成结果从本地账本恢复，不重复调用工具；
- 不确定的非 replay-safe 写入/计算拒绝自动重放；
- 舆情 Agent LangGraph 输入/输出契约适配器。
- 可选 `ToolExecutionContext` 将幂等键、run/step/index 和 lease owner 交给外部工具；
- ADR 明确禁止把 SQLite lease 直接嵌套进现有 PostgreSQL V2 执行路径。
- `RuntimeBackend` 是异步协议；现有 SQLite 通过 `AsyncSQLiteBackend` 在线程中执行，不阻塞事件循环。
- `PostgresRuntimeBackend` 使用独立 `runtime_*` 表，并支持 `async with backend` 管理连接池；当前真实数据库测试仍需 `RUNTIME_DATABASE_URL`。
- LeaseGrant 同时保护 checkpoint/tool-result 写入并进入工具上下文；真实 PostgreSQL 双 worker 验证仍是生产阻断项。

## 快速验证

```powershell
python -m pip install -e ".[dev]"
python -m pytest -q --basetemp .pytest-tmp
```

GitHub Actions 将普通单元/契约测试与真实 PostgreSQL 集成测试拆成两个 job。后者安装 `.[dev,postgres]`，启动 PostgreSQL 16，执行 migration、连接池生命周期和双 worker lease 接管测试，并上传 JUnit 报告。工作流文件存在不等于远端已经通过；必须以实际 Actions run 为准。

运行真实舆情 LangGraph 的可选跨仓库测试：

```powershell
$env:OPINION_AGENT_PROJECT = "C:\path\to\舆情研判Agent_高质量重建版"
python -m pytest tests/integration/test_opinion_agent_adapter.py -q --basetemp .pytest-tmp-integration
```

adapter 不复制舆情项目代码，通过 `graph_provider().invoke(state)` 契约连接。真实项目的 `redact_sensitive` 和安全校验应以 `input_transform`、`output_transform` 注入。

生产 V2 集成边界见 `docs/adr/0001-v2-runtime-integration-boundary.md`。

设计、来源边界和完整开发过程见 `docs/ARCHITECTURE.md`、`docs/SOURCE_BOUNDARY.md` 与 `docs/ENGINEERING_LOG.md`。
