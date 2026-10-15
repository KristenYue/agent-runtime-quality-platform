# ADR-0001：舆情 V2 与 Runtime Platform 的集成边界

- 状态：Accepted
- 日期：2026-10-15
- 决策者：项目维护者（由当前工程实现记录，待人工评审后提交）

## 背景

舆情 Agent V2 已经用 PostgreSQL repository 管理 Job/Run 状态、执行 lease、heartbeat、重试和结果落库。Runtime Platform 0.2 也有 SQLite checkpoint、run lease、heartbeat、工具幂等账本和恢复状态机。

如果直接在 `JobExecutor` 内再调用完整 `AgentRuntime.run()`，会出现两套互相不知道的租约：

- 外层 PostgreSQL lease 可能有效，内层 SQLite lease 失效；
- 内层恢复可能继续执行，但外层 Job 已被另一个 worker 接管；
- SQLite 是单机文件，不能作为现有多 worker V2 的生产协调事实源；
- 两边分别重试，可能放大模型费用或外部副作用。

## 决策

1. 生产协调的唯一 owner 继续是舆情 V2 PostgreSQL `JobRepository`。
2. 当前 `OpinionAgentRuntimeAdapter` 只用于离线、开发和 shadow 验证，不直接替换 V2 `JobExecutor`。
3. 接入 V2 前，Runtime 必须拆出可注入的 `LeaseBackend`、`CheckpointBackend` 与 `IdempotencyBackend`；生产实现适配 PostgreSQL，不能依赖 worker 本地 SQLite。
4. 外部副作用工具必须接收 Runtime 生成的幂等键，并把它传给目标服务。只有真实满足幂等协议时才能声明 `replay_safe=True`。
5. fencing token 由生产协调库单调生成并随工具上下文传递；目标写入端若支持，应拒绝旧 token。
6. 灰度顺序固定为：离线双跑 -> shadow 记录但不影响正式结果 -> 小流量单 owner -> 扩大流量。每一步都必须通过现有发布门禁与成对回归门禁。

## 为什么不直接复用 SQLite lease

SQLite 实现适合验证状态机语义和单机故障注入，但不能提供当前 V2 所需的跨容器协调。把它放进生产路径还会创造新的本地持久化运维问题。

## 为什么先透传幂等键

本地 tool ledger 只能覆盖“handler 已返回、ledger 已保存”的窗口。若远端提交成功后本地进程立刻崩溃，只有远端看到相同幂等键并去重，才能避免重复副作用。因此幂等键透传是接入生产前的必要条件，不是可选优化。

## 结果与代价

正向结果：

- PostgreSQL 仍是唯一协调事实源；
- 避免双 lease、双 retry；
- Runtime 核心仍可独立测试；
- 外部幂等语义可以通过故障注入验证。

代价：

- 暂时不能声称 Runtime 已进入舆情生产路径；
- 需要新增后端抽象和 PostgreSQL adapter；
- shadow 阶段会增加计算成本，必须限制样本与预算。

## 被否决的方案

### 方案 A：V2 JobExecutor 内直接嵌套完整 SQLite Runtime

否决原因：双租约、双重试、单机状态、恢复所有权不一致。

### 方案 B：删除 V2 PostgreSQL lease，全部改用 SQLite

否决原因：能力倒退，无法支持现有容器和多 worker 目标。

### 方案 C：只靠本地 tool ledger 宣称 exactly-once

否决原因：远端提交与本地记账之间存在不可消除的崩溃窗口。

## 实施门槛

进入 V2 shadow 前必须同时满足：

- 后端接口与 PostgreSQL 实现测试；
- 幂等键进入 Tool Trace 和外部 handler；
- fencing token 设计评审；
- shadow 结果不写正式 Job result；
- 旧 V2 测试、Runtime 测试、RAG gate、安全 gate、release gate 全部通过；
- 有显式回滚开关。

## 实施进度

- 2026-10-15：已完成异步 `RuntimeBackend` Protocol 与 `AsyncSQLiteBackend`；
- 2026-10-15：已完成独立 PostgreSQL backend、显式连接池、幂等上下文、fencing token 和 stale-write 拒绝；
- 已编写真实 PostgreSQL 双连接池竞争测试，但当前主机无专用 DSN，尚未取得通过证据；
- 2026-10-15：已编写 Runtime CI，拆分普通回归和 PostgreSQL 16 service 集成 job，并上传 JUnit；因仓库尚未 commit/push，远端 run 仍待验证；
- 待完成：CI 真实通过证据、shadow runner、默认关闭/回滚开关、版本化任务集和组合门禁。
