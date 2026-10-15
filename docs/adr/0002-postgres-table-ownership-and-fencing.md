# ADR-0002：PostgreSQL 表所有权、事务边界与 fencing

- 状态：Accepted；Production blocked by live verification
- 日期：2026-10-15

## 背景

舆情 V2 已拥有 `analysis_jobs`、`agent_runs`、`run_checkpoints`、`tool_calls` 等表。它们描述业务 Job 生命周期和审计结果。Runtime Platform 的 checkpoint 是 Planner/Tool 状态机内部状态，语义和粒度不同。

## 决策

### 表所有权

Runtime 只拥有以下独立表：

- `runtime_checkpoints`；
- `runtime_run_leases`；
- `runtime_tool_results`。

Runtime migration 不得 ALTER、DELETE 或复用舆情 V2 表。未来若两套表建立关联，只通过显式外键/映射 migration，并由两个项目共同评审。

### 事务边界

- checkpoint upsert 是单事务；
- lease acquire/takeover 与 fencing token 递增是单条原子 upsert；
- heartbeat 和 release 必须同时匹配 run、owner、fencing token；
- tool result 以 idempotency key 首次写入为准；
- 不把长时间模型/工具调用放进数据库事务。

### Fencing token

每个 run 首次 lease token 为 1；释放、过期或其他 owner 接管后递增。相同 owner 在有效 lease 内重复 acquire 不递增。token 进入 `ToolExecutionContext`，下游支持时应拒绝旧 token。

## 当前实施状态与阻断项

0.5.0 已完成：backend 写方法接收 LeaseGrant；checkpoint/tool-result 在同一事务或 SQL 中校验当前 owner+token；stale write 映射为 `RuntimeLeaseLost`；SQLite 接管故障注入证明旧 worker 无法写 Trace；PostgreSQL backend 提供显式异步连接池生命周期。

仍处于 `Production blocked`，直到：

1. 真实 PostgreSQL 双 owner 接管测试已在 CI run `38742299975` 通过；
2. 下游副作用接口验证 token 或至少验证幂等键；
3. PostgreSQL 连接池打开/借还/关闭和 migration 已在真实 CI 通过；仍需超时与 rollback 演练；
4. V2 shadow 模式有显式开关和回滚路径。

写入 fencing 不单独比较本地墙钟过期时间：若没有接管，owner+token 仍代表同一代际；真实接管会原子递增 token。这样避免极短 TTL 下调度抖动造成假 stale write，同时仍拒绝被新代际替换的旧 worker。

## Migration 策略

`migrations/0001_postgres_runtime.sql` 只使用 `CREATE ... IF NOT EXISTS` 建立独立表。开发/CI 可在专用数据库执行；生产由部署 migration job 管理，不由应用启动时自动执行。

## 被否决的方案

- 复用舆情 `run_checkpoints`：语义和 sequence 规则不同；
- 在应用启动时自动 ALTER V2 表：扩大权限和故障面；
- 只有 owner 没有 token：owner 名称可能复用，无法识别旧时代执行者；
- 把工具调用包在数据库长事务：会造成锁、连接占用和不可控回滚语义。
