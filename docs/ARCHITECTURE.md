# Runtime Platform 架构与学习说明

## 1. 这层平台解决什么问题

业务 Agent 通常把“模型决定下一步、调用工具、记录结果、继续推理”写在一个函数里。这样能演示，但很难回答四个工程问题：

1. 工具调用到一半失败，系统处于什么状态？
2. 高风险操作是否真的在执行前停住？
3. 新版本是否比旧版本退化？
4. 进程重启后，人工审批中的任务是否还能继续？

本项目把这些横切能力放到 provider-neutral runtime；Planner 只负责返回结构化动作，不绑定任何模型 SDK。

## 2. 主流程

```text
request
   |
   v
Planner.decide(request, observations, step)
   |
   +-- finish --------------------------> completed
   |
   +-- pause_for_review ----------------> waiting_review
   |
   +-- tool / tool_batch
          |
          +-- 未注册 / 超预算 ----------> failed_closed
          |
          +-- dangerous ----------------> waiting_review
          |
          +-- read/write tool
                 |
                 +-- asyncio.gather 并发执行
                 +-- 生成 ToolCallTrace
                 +-- SQLite checkpoint
                 +-- 结果作为 observation 回给 Planner
```

每次 run 在 Planner 执行前先写一个 `step_no=-1` 的初始 checkpoint；每个动作处理后再以步骤号覆盖或追加状态。状态 JSON 与 SHA-256 一同存储，读取时重新计算哈希，避免损坏状态被静默使用。

## 3. 状态与 Trace

- `RunResult`：一次任务的最终或暂停状态；
- `StepTrace`：一次 Planner 决策；
- `ToolCallTrace`：工具名、参数、结果、耗时、错误类型；
- `RuntimeErrorTrace`：预算、白名单、Planner 或工具失败；
- `PlannerAction`：`tool`、`tool_batch`、`pause_for_review`、`finish` 四种动作。

`observations` 是 Planner 的内部上下文，不放进对外 `RunResult`，但会保存在 checkpoint 中，保证重启后的审批恢复仍能继续决策。

## 4. 两层预算为什么都需要

`max_tool_calls` 限制工具总调用数，但 Planner 也可能不断产生不调用工具的动作。`max_steps` 限制状态机总步数。两者任一超限均进入 `failed_closed`，而不是猜测一个结果。

批量工具在预算检查通过后才统一计数和执行。因此一个剩余预算不足的批次不会“执行一半”。

## 5. 人工审批语义

- Planner 显式返回 `pause_for_review` 时暂停；
- 任何批次只要包含 `dangerous` 工具，整批在执行前暂停；
- `approved=True` 后才执行待处理工具；
- `approved=False` 进入 `failed_closed`，留下 `human_rejected` 错误；
- 新 Runtime 实例只要连接同一个 SQLite 文件，就能继续等待审批的 run。

这里实现的是最小审批状态机，不包含用户认证、角色授权、多人会签、审批超时或 Web UI。

## 6. 并发与失败语义

同一 `tool_batch` 使用 `asyncio.gather` 并发。每个工具异常都被转成失败 Trace；只要一个失败，整个 run 失败关闭。当前设计不会自动重试有副作用的工具，因为缺少幂等键时自动重试可能重复写入。

必须理解的边界：当前版本能恢复“已经安全暂停”的 run，但还没有实现任意进程崩溃后的自动 replay。外部副作用发生后、checkpoint 写入前若进程崩溃，需要幂等工具协议和租约机制才能安全重放；本版本不声称解决了它。

## 7. 回归门禁

`paired_regression_gate` 要求 baseline 和 candidate 拥有完全相同的 `case_id` 和 category，然后比较：

- 成功率；
- 平均质量分；
- 平均错误数；
- 平均工具调用数（观察指标，当前不阻断）；
- 平均延迟绝对增量；
- 平均估算成本绝对增量。

“成对”很重要：如果两边样本不同，指标变化无法归因于代码版本，因此直接拒绝评测。成功率、质量、错误、延迟和成本可以阻断发布；总览与每个 category 都保留相同指标。业务仓库必须在自己的版本化 manifest 中锁定阈值，不能把平台默认值解释为业务 SLA。

## 8. 当前集成状态与下一阶段

幂等上下文、fencing token、PostgreSQL backend、版本化舆情 JSONL 和默认关闭的 V2 shadow Worker 已完成；舆情 PR `#4` 的组合 CI 已通过 60 对 baseline/candidate 门禁。业务任务和报告由舆情仓库维护，本仓库只维护通用 Runtime contracts 与 paired gate，避免平台反向拥有业务数据。

下一阶段不继续增加 Runtime 抽象，优先完成：

1. Trace、暂停、恢复和失败原因查看页；
2. OpenTelemetry 或兼容导出器；
3. 持久化审批主体、RBAC 与审计事件；
4. 将合成校准报告逐步补充为真实 shadow 观测，但不把它包装成生产 SLA。

## 9. 0.2 租约与保守恢复

每次 `run`、`resume`、`recover` 都必须先获得 `run_id` 对应的 SQLite 租约。租约包含 owner 和过期时间；后台 heartbeat 每隔约 `TTL / 3` 续租。另一个 owner 在租约有效时会收到 `RuntimeLeaseBusy`。

恢复采用“先记意图、再执行、最后确认”的顺序：

```text
Planner action
  -> checkpoint(inflight_action)
  -> tool handler
  -> tool_results[idempotency_key]
  -> checkpoint(step trace + observation, inflight cleared)
```

幂等键固定为 `run_id:step_no:tool_index`。如果进程在 tool ledger 已保存、step checkpoint 未保存之间崩溃，新 worker 会复用缓存 Trace，并标记 `replayed_from_cache=true`，不再次调用 handler。

如果崩溃发生在外部副作用之后、tool ledger 之前，Runtime 无法仅凭本地数据库判断副作用是否发生。因此：

- `read` 工具默认可重放；
- `compute`、`write`、`dangerous` 默认不可重放；
- 只有工具所有者显式声明 `replay_safe=True` 才可自动重试；
- 不可安全重放时进入 `failed_closed / unsafe_replay_blocked`。

这不是 exactly-once。要进一步保证外部写入，需要目标系统接受同一幂等键，或引入事务型 outbox/fencing token。当前 lease 也没有 fencing token；失租旧 worker 的外部调用无法被 SQLite 强制撤销。

## 9.1 外部幂等上下文

工具可声明 `inject_context=True`，Runtime 会额外注入不可由 Planner 参数覆盖的 `runtime_context`：

- `idempotency_key`；
- `run_id`；
- `step_no`；
- `tool_index`；
- `lease_owner`。

工具 handler 应把 `idempotency_key` 原样交给远端写入接口。`replay_safe=True` 是工具所有者对恢复协议的显式承诺；仅设置该标志、但远端不实际去重，仍可能产生重复副作用。

`runtime_context` 使用独立注入通道，Planner 不能在普通 arguments 中伪造或覆盖它。

## 10. 舆情 Agent 薄适配器

`OpinionAgentRuntimeAdapter` 复刻的是输入/输出契约，不复制业务实现：

- 输入字段与现有 `AnalyzeRequest` 对齐；
- 生成 `request_id/event_id/query/comments/tool_traces/errors` 状态；
- 在线程中调用同步 `graph.invoke(state)`，避免阻塞 async runtime；
- 要求结果包含匹配的 `event_id`、`analysis_report`、`tool_traces`、`errors`；
- 业务图分类为 `compute`，因为模型调用可能付费且非确定，崩溃不盲目重跑；
- 安全治理通过 transform 注入，平台不反向依赖舆情包。

可选 integration test 已实际加载相邻舆情仓库的 `build_opinion_graph`，跑通 sentiment classifier、aggregator 和 briefing composer 三节点链路。

## 11. 0.3 异步持久化边界

`RuntimeBackend` 是 Runtime 依赖的异步 Protocol，包含三组能力：

- checkpoint：保存、读取 run state；
- lease：获取、续租、释放执行所有权；
- idempotency ledger：保存、读取工具 Trace。

Runtime 不再直接调用 SQLite。兼容参数 `checkpoints=SQLiteCheckpointStore(...)` 会被包装为 `AsyncSQLiteBackend`，每个同步 stdlib SQLite 操作经 `asyncio.to_thread` 执行；新实现可以直接使用 `backend=` 注入。

构造函数要求 `checkpoints` 与 `backend` 必须且只能提供一个，避免两个持久化源同时存在。`get()` 因此也改成 async，所有 backend I/O 都遵循同一调用模型。

这一步只是为 PostgreSQL 打开边界，不代表 PostgreSQL adapter 已经实现。生产实现仍需事务、SQL migration、连接池、故障分类、fencing token 和跨进程集成测试。

## 12. 0.4 PostgreSQL backend 与 fencing grant

`PostgresRuntimeBackend` 使用 psycopg 异步连接，实现 `RuntimeBackend` 全部方法。migration 只创建：

- `runtime_checkpoints`；
- `runtime_run_leases`；
- `runtime_tool_results`。

0.5.0 起，常驻服务应使用 `async with PostgresRuntimeBackend(...)` 显式打开和关闭 `AsyncConnectionPool`。池以 `open=False` 构造，再执行 `open(wait=True)`，让连接错误在启动阶段而不是首个业务请求时暴露。未进入上下文时保留逐操作连接模式，只用于短脚本和兼容路径。

它不会修改舆情 V2 的 `analysis_jobs`、`agent_runs`、`run_checkpoints` 或 `tool_calls`。

Lease acquire 使用原子 upsert：首次 token 为 1；其他 owner 接管已过期 lease 时 token +1；同 owner 的有效重复 acquire 保持 token。renew/release 同时匹配 owner 与 token。`LeaseGrant` 随执行传递，工具上下文可以把 token 交给下游。

0.4.1 起，checkpoint 和 tool-ledger 写入都携带 LeaseGrant。SQLite 在同一 `BEGIN IMMEDIATE` 事务校验 owner+token；PostgreSQL 使用 `INSERT ... SELECT ... WHERE EXISTS(current owner+token)`。不匹配会成为 `RuntimeLeaseLost`。

写入不单独依赖 `expires_at > now`：没有接管时 owner+token 仍是同一代际；真实接管会递增 token。这样可避免极短 TTL 的调度抖动制造假 stale，同时拒绝新代际之后的旧 worker。
