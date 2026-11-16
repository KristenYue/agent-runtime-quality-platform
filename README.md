# 可恢复 Agent Runtime 与质量门禁平台

[![持续集成](https://github.com/KristenYue/agent-runtime-quality-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/KristenYue/agent-runtime-quality-platform/actions/workflows/ci.yml)
![Python 3.11](https://img.shields.io/badge/Python-3.11-3776AB)
![PostgreSQL 16](https://img.shields.io/badge/PostgreSQL-16-4169E1)
[![功能契约](https://img.shields.io/badge/功能契约-180%2F180-2EA44F)](data/evaluation/acceptance_v1/report.json)
[![故障处置](https://img.shields.io/badge/故障处置-70%2F70-2EA44F)](data/evaluation/acceptance_v1/report.json)
[![重复最终写入](https://img.shields.io/badge/重复最终写入-0-2EA44F)](data/evaluation/acceptance_v1/report.json)
[![开源许可](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

面向工具型 Agent 的可恢复执行内核与自动化质量门禁。

项目将 Agent 的“模型决策—工具调用—任务完成”拆分为可持久化、可恢复、可审批、可追踪和可验收的执行过程，重点解决：

> Agent 执行到一半崩溃后如何继续、多个 Worker 如何安全接管、重复任务如何避免重复提交，以及外部副作用状态不确定时为什么必须安全失败。

---

## 10 秒了解项目

```text
用户任务
   │
   ▼
Planner 结构化决策
   │
   ▼
策略、权限与预算检查
   │
   ├── 未授权操作 ───────────────→ failed_closed
   │
   ├── 高风险操作 ───────────────→ waiting_review
   │                                  │
   │                              人工批准/拒绝
   │                                  │
   └── 合法工具调用                    ▼
            │                      继续/安全失败
            ▼
      工具并发执行
            │
            ▼
      幂等结果账本
            │
            ▼
     Checkpoint + SHA-256
            │
            ├── 正常完成 ─────────→ completed
            │
            └── Worker 崩溃
                    │
                    ▼
         Lease 接管 + Fencing Token
                    │
                    ▼
         回放已提交结果，不重复调用工具
                    │
                    ▼
           Trace + 自动化质量门禁
```

项目不是一个绑定特定大模型的 Agent 应用，而是位于业务 Agent 下方的通用执行层：

```text
业务 Agent
    ↓
Planner Protocol
    ↓
Agent Runtime
    ↓
工具、数据库与外部系统
```

Planner 只需返回结构化动作，不依赖特定模型厂商 SDK。

---

# 核心验收结果

| 验收维度 | 结果 | 证明的能力 |
|---|---:|---|
| 功能契约 | **180/180 通过** | 正常执行、审批、策略拒绝和异常动作均符合预期 |
| 故障处置 | **70/70 受控处理** | 所有故障均进入恢复或明确失败状态 |
| 自动恢复 | **68 个场景恢复** | Worker 崩溃后从幂等账本恢复工具结果 |
| 安全失败 | **2 个场景 Fail-closed** | 无法确认副作用时不盲目重放 |
| 未处置故障 | **0** | 没有任务遗留在未知状态 |
| 重复任务 | **60 组** | 验证多次提交情况下的结果一致性 |
| 额外最终写入 | **0** | 验收范围内未产生重复业务结果 |
| PostgreSQL 接管 | **通过** | 双 Worker 竞争、Lease 接管与陈旧写入拒绝 |
| 机器可读报告 | **1 份逐 Case JSON** | 每个任务保留预期状态、实际状态和错误码 |

完整逐 Case 证据：

- [机器可读验收报告](data/evaluation/acceptance_v1/report.json)
- [Runtime 架构说明](docs/ARCHITECTURE.md)
- [工程日志](docs/ENGINEERING_LOG.md)
- [PostgreSQL Backend 设计](docs/POSTGRES_BACKEND.md)

---

# 为什么需要 Agent Runtime

一个能够演示的 Agent 通常只需要：

```python
while not finished:
    action = model.decide(context)
    result = call_tool(action)
    context.append(result)
```

但真实工程环境还需要回答：

- 进程在工具调用后崩溃，任务是否会重复执行？
- Worker 失去任务所有权后，是否还能继续写入旧结果？
- 危险工具是否真的在执行前暂停？
- 人工审批中的任务能否在进程重启后继续？
- Planner 一直不结束时，Runtime 如何强制终止？
- 同一个任务被重复提交时，最终结果是否会写入多次？
- 新版本成功率提高，但延迟、错误或成本退化，是否还能发布？
- 故障恢复后，能否还原每次决策和工具调用过程？

本项目将这些横切能力从业务 Agent 中抽离，形成独立的执行与验收平台。

---

# 系统架构

```mermaid
flowchart LR
    USER["用户任务"] --> PLANNER["Planner Protocol\n结构化动作"]
    PLANNER --> POLICY["策略、权限与预算"]

    POLICY -->|"允许"| EXECUTOR["工具执行器"]
    POLICY -->|"高风险"| REVIEW["人工审批"]
    POLICY -->|"拒绝/超预算"| CLOSED["failed_closed"]

    REVIEW -->|"批准"| EXECUTOR
    REVIEW -->|"拒绝"| CLOSED

    EXECUTOR --> LEDGER["幂等结果账本"]
    LEDGER --> CHECKPOINT["Checkpoint\n状态 + SHA-256"]
    CHECKPOINT --> BACKEND["SQLite / PostgreSQL Backend"]

    BACKEND --> LEASE["Lease + Heartbeat"]
    LEASE --> FENCING["Fencing Token"]
    FENCING --> RECOVERY["Worker 接管与恢复"]
    RECOVERY --> LEDGER

    CHECKPOINT --> TRACE["统一 Trace"]
    TRACE --> GATE["验收基准与回归门禁"]
```

## 核心组件

| 组件 | 职责 |
|---|---|
| `Planner Protocol` | 屏蔽不同模型 SDK，只接收结构化动作 |
| `AgentRuntime` | 驱动状态机、预算、审批、工具执行和结果提交 |
| `ToolRegistry` | 注册同步/异步工具及其风险级别 |
| `RuntimeBackend` | 统一 Checkpoint、Lease 和幂等账本接口 |
| `SQLite Backend` | 提供本地可复现的持久化执行环境 |
| `PostgreSQL Backend` | 支持多 Worker、连接池、事务和接管测试 |
| `Checkpoint` | 保存任务进度、Planner 上下文和执行状态 |
| `Lease/Heartbeat` | 管理任务当前执行所有权 |
| `Fencing Token` | 阻止旧 Worker 在接管后继续写入 |
| `Idempotency Ledger` | 保存已完成工具结果，恢复时避免重复调用 |
| `Trace` | 记录 Run、Step、ToolCall 和 Error |
| `Regression Gate` | 比较基线版与候选版的质量和工程指标 |

---

# Runtime 状态模型

一次任务具有以下状态：

```mermaid
stateDiagram-v2
    [*] --> running

    running --> completed: Planner finish
    running --> waiting_review: dangerous tool
    running --> waiting_review: pause_for_review
    running --> failed_closed: policy rejected
    running --> failed_closed: budget exceeded
    running --> failed_closed: unsafe replay
    running --> failed_closed: malformed action

    waiting_review --> running: approved
    waiting_review --> failed_closed: rejected

    running --> running: tool result checkpointed
    running --> running: lease takeover and recovery
```

| 状态 | 含义 |
|---|---|
| `running` | Runtime 正在决策或执行工具 |
| `waiting_review` | 高风险操作已暂停，等待人工审批 |
| `completed` | Planner 明确返回最终结果 |
| `failed_closed` | 为避免不确定行为而终止执行 |

`failed_closed` 不等于程序崩溃。

它表示 Runtime 已识别到无法安全继续的情况，并主动阻止潜在的重复副作用或越权操作。

---

# 核心技术设计

## 1. Checkpoint：让任务状态可恢复

### 问题

如果任务只保存在进程内存中：

```text
Planner 已完成 3 步
    ↓
进程崩溃
    ↓
内存状态丢失
    ↓
任务只能从头执行
```

从头执行可能重复调用已经成功的工具。

### 解决方案

Runtime 在以下位置持久化 Checkpoint：

- Planner 首次执行前；
- 每次结构化动作产生后；
- 工具结果写入后；
- 人工审批暂停时；
- 任务状态变化时；
- 最终完成或失败关闭时。

每条 Checkpoint 包含：

```text
run_id
step_no
state_json
state_sha256
```

读取状态时重新计算 SHA-256：

```text
保存状态
  → 生成规范化 JSON
  → 计算 SHA-256
  → 同时写入数据库

恢复状态
  → 读取 JSON 与 SHA-256
  → 重新计算哈希
  → 一致才允许继续
```

哈希校验能够发现意外损坏，避免将不完整状态静默交给 Planner。

---

## 2. 幂等结果账本：崩溃后不重复调用工具

### 高风险时间窗口

```text
工具已经执行成功
    ↓
外部系统已经产生结果
    ↓
Runtime 尚未保存新的 Step Checkpoint
    ↓
进程崩溃
```

如果新 Worker 直接重新执行该工具，可能产生重复写入。

### 恢复协议

Runtime 为每次工具调用生成确定性幂等键：

```text
run_id : step_no : tool_index
```

执行顺序为：

```text
保存 inflight_action
    ↓
执行工具
    ↓
将 ToolCallTrace 写入幂等结果账本
    ↓
写入 Step Checkpoint
    ↓
清除 inflight_action
```

恢复时：

```text
发现未完成的 inflight_action
    ↓
根据幂等键查询结果账本
    │
    ├── 找到已提交结果
    │       └── 回放 Trace，标记 replayed_from_cache
    │
    └── 没有找到结果
            ├── replay_safe=True → 允许重试
            └── 不可安全重放 → failed_closed
```

### 验收结果

- 68 个 `WORKER_CRASH_AFTER_TOOL_LEDGER` 场景成功恢复；
- 恢复过程复用已提交工具结果；
- 工具 Handler 不被再次调用；
- Trace 标记 `replayed_from_cache=true`。

---

## 3. Lease、Heartbeat 与 Fencing Token

### 问题

仅有 Checkpoint 仍无法解决多个 Worker 同时处理一个任务的问题。

可能出现：

```text
Worker A 获得任务
    ↓
Worker A 暂时失联
    ↓
Lease 过期
    ↓
Worker B 接管任务
    ↓
Worker A 恢复并继续写入
    ↓
旧状态覆盖 Worker B 的新状态
```

### Lease 与 Heartbeat

每次 `run`、`resume` 或 `recover` 前，Worker 必须获得对应 `run_id` 的 Lease。

Lease 包含：

- `owner`
- `expires_at`
- `fencing_token`

Heartbeat 按约 `TTL / 3` 的间隔续租。

其他 Worker 在 Lease 有效时不能接管；Lease 过期后可以原子接管。

### Fencing Token

每次新的 Worker 接管任务，Fencing Token 单调递增：

```text
Worker A：token = 1
Worker B：token = 2
Worker C：token = 3
```

所有 Checkpoint 和 Tool Ledger 写入都必须携带当前：

```text
owner + fencing_token
```

当 Worker A 恢复并尝试使用旧 Token 写入时：

```text
当前 token = 2
Worker A token = 1
    ↓
写入被拒绝
    ↓
StaleLeaseWriteError / RuntimeLeaseLost
```

### PostgreSQL 实现

PostgreSQL Backend 使用：

- 异步连接池；
- 原子 Lease Upsert；
- 单调 Fencing Token；
- 带所有权校验的状态写入；
- 双连接池并发竞争测试；
- PostgreSQL 16 CI 服务；
- 独立的 `runtime_*` 数据表。

Runtime 只创建：

```text
runtime_checkpoints
runtime_run_leases
runtime_tool_results
```

它不会修改业务 Agent 自己的任务表。

详细说明见：

- [PostgreSQL Backend](docs/POSTGRES_BACKEND.md)
- [ADR-0002：PostgreSQL 表所有权与 Fencing](docs/adr/0002-postgres-table-ownership-and-fencing.md)

---

## 4. 人工审批、工具权限与安全失败

每个工具在注册时显式声明类型：

```text
read
write
dangerous
```

Runtime 不根据工具名称猜测风险。

### 执行规则

| 情况 | Runtime 行为 |
|---|---|
| 工具未注册 | `failed_closed / tool_not_allowed` |
| 普通读取工具 | 允许执行 |
| 写入工具 | 按注册策略执行 |
| 危险工具 | 执行前进入 `waiting_review` |
| 人工批准 | 恢复并执行待处理工具 |
| 人工拒绝 | `failed_closed / human_rejected` |
| 批次包含危险工具 | 整批执行前暂停 |

审批状态会进入 Checkpoint，因此新的 Runtime 实例连接同一个数据库后，可以继续等待中的任务。

---

## 5. 双层预算：阻止无限执行

仅限制工具调用次数并不足以防止无限循环。

Planner 可能持续返回不调用工具、也不结束的动作。

因此 Runtime 同时限制：

| 预算 | 控制对象 |
|---|---|
| `max_tool_calls` | 工具调用总数 |
| `max_steps` | Planner 状态机总步数 |

任一预算耗尽后：

```text
记录 RuntimeErrorTrace
    ↓
写入 Checkpoint
    ↓
进入 failed_closed
```

工具批次会先进行整体预算检查。

如果剩余预算不足，整个批次不会“只执行一半”。

---

# 统一 Trace 与可观测性

平台统一记录四类结构：

| Trace 类型 | 内容 |
|---|---|
| `RunResult` | 一次任务的最终、暂停或失败状态 |
| `StepTrace` | Planner 每一步结构化决策 |
| `ToolCallTrace` | 工具名、参数、结果、耗时和错误 |
| `RuntimeErrorTrace` | 权限、预算、Planner 和工具错误 |

Trace 能够回答：

- Planner 每一步决定了什么；
- 哪个工具被调用；
- 工具参数与结果是什么；
- 工具是否来自缓存回放；
- 哪一步发生错误；
- 为什么任务进入人工审批；
- 为什么任务安全失败；
- 任务由哪个 Worker 执行；
- 是否发生过 Lease 接管。

这使 Runtime 的结果不仅是“成功/失败”，而是可用于诊断和回归的执行证据。

---

# 可执行验收基准

验收基准直接运行 Runtime、Checkpoint、审批、恢复、权限和幂等代码路径，不是静态表格。

## 功能契约：180/180

| 场景类型 | 数量 | 验证目标 |
|---|---:|---|
| 自动执行 | **114** | 正常工具调用和任务完成 |
| 人工审批 | **30** | 高风险操作暂停、批准与恢复 |
| 策略拒绝 | **30** | 未授权工具在执行前被阻止 |
| 畸形 Planner 动作 | **6** | 不完整或非法动作安全失败 |
| 合计 | **180** | 全部符合预期 |

## 故障注入：70/70

| 故障类型 | 数量 | 结果 |
|---|---:|---|
| Worker 在 Tool Ledger 写入后崩溃 | **68** | 从缓存结果恢复 |
| 工具超时 | **1** | 安全失败 |
| 缺少必要字段 | **1** | 安全失败 |
| 未处置故障 | **0** | 无未知状态遗留 |

这里的 2 个 `failed_closed` 是正确验收结果。

因为 Runtime 无法确认外部副作用时，继续自动重放反而可能造成重复写入。

## 重复提交：60 组

每组对同一逻辑任务发起重复提交，并检查业务唯一键与 Runtime 结果账本。

结果：

```text
重复任务组：60
额外最终写入：0
```

该结论只适用于验收基准覆盖的幂等协议和最终写入路径，不外推为任意外部系统的全局 Exactly-once 保证。

---

# 版本回归质量门禁

平台提供成对回归比较：

```text
Baseline 版本
      │
      ├── 相同 case_id
      ├── 相同任务分类
      └── 相同评测输入
              │
              ▼
Candidate 版本
```

如果两边 Case 不一致，评测直接拒绝，避免因样本变化产生错误结论。

门禁比较：

- 成功率；
- 平均质量分；
- 平均错误数；
- 平均工具调用数；
- 平均延迟增量；
- 平均估算成本增量；
- 每个任务分类的单独结果。

以下指标可以阻断发布：

```text
成功率下降
质量分下降
错误数增加
延迟超过阈值
成本超过阈值
```

工具调用次数目前作为观察指标保留，不单独阻断发布。

---

# 业务 Agent 集成

[通信网络故障诊断 Agent](https://github.com/KristenYue/telecom-network-incident-agent) 是当前 Runtime 的端到端业务适配项目。

两个仓库的职责边界：

| Runtime 平台 | 通信故障诊断 Agent |
|---|---|
| Checkpoint | FastAPI 接口 |
| Lease/Heartbeat | Redis Streams Worker |
| Fencing Token | 通信领域工具 |
| 幂等结果账本 | 根因分析与排序 |
| 人工审批 | 工单草稿 |
| 统一 Trace | 领域评测数据 |
| 回归质量门禁 | 业务验收规则 |

业务仓库通过固定版本依赖 Runtime，不复制执行内核。

这样能够做到：

- Runtime 升级由通用验收基准验证；
- 通信业务变化由领域评测验证；
- 平台能力与业务逻辑保持清晰边界。

集成边界见 [ADR-0001](docs/adr/0001-v2-runtime-integration-boundary.md)。

---

# 快速运行

## 环境要求

- Python 3.11 或更高版本
- Git
- 可选：PostgreSQL 16

## 1. 创建环境

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

### Linux

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

## 2. 运行单元与契约测试

### Windows

```powershell
.\.venv\Scripts\python.exe -m pytest -q `
  --basetemp .pytest-tmp
```

### Linux

```bash
.venv/bin/python -m pytest -q \
  --basetemp .pytest-tmp
```

## 3. 运行完整验收基准

### Windows

```powershell
.\.venv\Scripts\python.exe scripts\run_acceptance_benchmark.py
```

### Linux

```bash
.venv/bin/python scripts/run_acceptance_benchmark.py
```

验收完成后更新：

```text
data/evaluation/acceptance_v1/report.json
```

报告包含：

- 每个 Case 的编号；
- 预期状态；
- 实际状态；
- 错误码；
- Step 数量；
- Tool Call 数量；
- Checkpoint 数量；
- 是否通过验收。

## 4. 运行 PostgreSQL 集成测试

安装 PostgreSQL 依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev,postgres]"
```

设置测试数据库：

```powershell
$env:RUNTIME_DATABASE_URL = "postgresql://runtime_test:integration-only@localhost:5432/runtime_test"
```

运行集成测试：

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/integration/test_postgres_runtime_backend.py `
  --basetemp .pytest-tmp-postgres
```

GitHub Actions 会使用真实 PostgreSQL 16 Service 自动执行该测试。

---

# 持续集成

CI 包含三个独立任务：

| CI 任务 | 验证内容 |
|---|---|
| `unit-and-contract` | Runtime、工具、Checkpoint、契约和回归门禁 |
| `postgres-integration` | Migration、连接池、Lease 接管和 Fencing |
| `acceptance-benchmark` | 180 项功能、70 项故障和 60 组重复任务 |

每次执行会上传：

- 单元与契约测试报告；
- PostgreSQL 集成测试报告；
- 机器可读验收报告。

---

# 关键问题与工程改进

| 发现的问题 | 根因 | 工程处理 | 防回归证据 |
|---|---|---|---|
| Planner 不调用工具时仍可能无限循环 | 只有工具预算，没有步骤预算 | 增加独立 `max_steps` | `step_budget_exceeded` 测试 |
| Worker 失租后仍可能写入 | 只检测 Heartbeat，没有写入代际校验 | 写入绑定 Owner 与 Fencing Token | 陈旧 Token 写入被拒绝 |
| 合法 Worker 被过期墙钟误判 | 写入 SQL 将租约活性与执行代际混合 | Fencing 只判断单调代际 | PostgreSQL 接管边界测试 |
| 恢复后重复调用已完成工具 | 恢复流程未优先读取结果账本 | 使用确定性幂等键回放 Trace | `replayed_from_cache` 断言 |
| 外部写入状态不确定 | 副作用发生后本地尚未记账 | 非 Replay-safe 操作失败关闭 | 2 个明确安全失败场景 |
| 工具注册表发生变化 | 恢复时工具语义可能与原执行不同 | 恢复前检查工具契约 | 工具漂移回归测试 |
| PostgreSQL 连接问题延迟到首次请求 | 连接池隐式打开 | 显式打开并等待连接池可用 | CI 启动阶段失败可见 |

完整定位过程见 [工程日志](docs/ENGINEERING_LOG.md)。

---

# 工程决策记录

项目使用 ADR 记录关键架构取舍。

## ADR-0001：Runtime 与业务 Agent 的集成边界

明确：

- Runtime 不复制业务 Agent 代码；
- 业务 Agent 不自行实现第二套 Runtime；
- 平台维护通用执行契约；
- 业务仓库维护领域任务和评测数据。

查看：[ADR-0001](docs/adr/0001-v2-runtime-integration-boundary.md)

## ADR-0002：PostgreSQL 表所有权与 Fencing

明确：

- Runtime 使用独立的 `runtime_*` 表；
- 不修改业务 Agent 的任务表；
- Lease 接管必须产生单调 Token；
- 状态写入必须验证当前 Owner 与 Token。

查看：[ADR-0002](docs/adr/0002-postgres-table-ownership-and-fencing.md)

---

# 工程证据索引

| 想了解的内容 | 对应文件 |
|---|---|
| Runtime 主流程与状态语义 | [架构说明](docs/ARCHITECTURE.md) |
| 每项功能和故障的验收结果 | [机器可读报告](data/evaluation/acceptance_v1/report.json) |
| 开发过程、Bad Case 与根因分析 | [工程日志](docs/ENGINEERING_LOG.md) |
| PostgreSQL Backend | [PostgreSQL 设计](docs/POSTGRES_BACKEND.md) |
| Runtime 与业务项目的边界 | [ADR-0001](docs/adr/0001-v2-runtime-integration-boundary.md) |
| PostgreSQL 表与 Fencing 决策 | [ADR-0002](docs/adr/0002-postgres-table-ownership-and-fencing.md) |
| 数据与代码来源边界 | [来源说明](docs/SOURCE_BOUNDARY.md) |
| 数据库结构 | [Migration](migrations/0001_postgres_runtime.sql) |
| 验收基准入口 | [运行脚本](scripts/run_acceptance_benchmark.py) |

---

# 仓库结构

```text
agent-runtime-quality-platform/
├── src/agent_runtime_platform/
│   ├── runtime.py                 # Runtime 主循环
│   ├── tools.py                   # 工具注册与权限
│   ├── checkpoint.py              # SQLite Checkpoint
│   ├── backends.py                # RuntimeBackend 协议
│   ├── postgres.py                # PostgreSQL Backend
│   ├── contracts.py               # Run、Step、Tool 和 Error 契约
│   ├── evaluation.py              # 成对回归质量门禁
│   ├── acceptance_benchmark.py    # 可执行验收基准
│   └── adapters/
│       └── opinion_agent.py       # 业务 Agent 薄适配器
├── tests/
│   ├── integration/               # PostgreSQL 与业务集成测试
│   ├── test_runtime.py
│   ├── test_checkpoint.py
│   ├── test_postgres_backend.py
│   └── test_acceptance_benchmark.py
├── data/evaluation/acceptance_v1/
│   └── report.json                # 机器可读验收报告
├── docs/
│   ├── ARCHITECTURE.md
│   ├── ENGINEERING_LOG.md
│   ├── POSTGRES_BACKEND.md
│   └── adr/
├── migrations/
│   └── 0001_postgres_runtime.sql
├── scripts/
│   └── run_acceptance_benchmark.py
└── .github/workflows/ci.yml
```

---

# 项目证明的工程能力

本项目重点展示：

- 将 Agent 应用抽象为 Provider-neutral Runtime；
- 使用 Pydantic 定义稳定的执行契约；
- 使用 Checkpoint 保存可恢复任务状态；
- 使用异步接口统一 SQLite 与 PostgreSQL Backend；
- 使用 Lease 和 Heartbeat 管理任务所有权；
- 使用 Fencing Token 阻止旧 Worker 陈旧写入；
- 使用幂等结果账本避免恢复时重复调用工具；
- 区分自动恢复与安全失败；
- 实现危险工具执行前的人工审批；
- 使用双层预算阻止 Planner 无限执行；
- 使用 Trace 保留任务、步骤、工具和错误证据；
- 使用成对回归门禁比较质量、错误、延迟和成本；
- 使用真实 PostgreSQL 服务执行多 Worker 接管测试；
- 将功能、故障和重复提交转化为机器可读验收报告。

适配岗位方向：

- Agent 平台开发
- Agent Runtime 开发
- AI 基础设施工程
- AI 工程化平台
- AI 系统可靠性工程
- Python 后端开发
- 分布式任务平台开发
- AI 研发效能与质量平台

---

## 当前验证范围

验收基准执行真实的 Runtime、Checkpoint、Lease、Fencing、审批、恢复和幂等代码路径。

当前结果证明：

- 已覆盖的故障场景能够恢复或安全失败；
- 已覆盖的重复任务没有产生额外最终写入；
- PostgreSQL 双 Worker 接管与陈旧状态写入拒绝符合设计。

跨任意外部系统的 Exactly-once 仍要求目标系统接受相同幂等键，或提供事务、Outbox、去重和 Fencing 协议。项目不会把局部验收结果外推为所有外部副作用的全局保证。

## 开源许可

本项目使用 [MIT License](LICENSE)。
