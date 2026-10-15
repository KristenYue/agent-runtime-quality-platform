# Agent Runtime Quality Platform 工程日志

日期：2026-10-15
阶段：Week 5 / Runtime Platform consolidation
版本：`0.1.0.dev0`

这份日志不仅记录“做了什么”，还记录为什么这样做、遇到了什么问题、怎样判断根因，以及哪些能力尚未完成。

## 1. 开始前的仓库审计

计划原本涉及 `fso-agent-benchmark` 与 `agent-reliability-eval`。审计发现：

- 两个仓库都属于已经冻结的成果；
- 两边都有用户自己的 README 或文档改动；
- FSO 基线缺少 checkpoint、并发和人工中断；
- reliability eval 更适合作为评测思想参考，而不是继续堆运行时代码。

决策：新建 `agent-runtime-quality-platform`，旧仓库全程只读。这样既保留旧结果，也能清楚展示“运行时 + 可靠性评测”如何组成一个平台。

## 2. 问题：递归枚举碰到 pytest cache 权限

### 现象

仓库盘点时递归目录命令进入 `.pytest_cache`，出现访问拒绝。

### 判断

这是环境缓存目录权限，不代表项目文件损坏。继续强行递归既无必要，也可能遗漏真正重要的信息。

### 解决

改用 `rg --files` 做有目标的文件清单，并只读取 README、配置和关键源码。经验是：代码盘点先使用版本控制/文本索引工具，避免把缓存、虚拟环境和构建产物当作源码遍历。

## 3. 先定义契约，再写循环

建立以下 Pydantic 契约：

- `PlannerAction`：限制为四种动作并校验形状；
- `RunStatus`：明确 `running / waiting_review / completed / failed_closed`；
- `RunResult / StepTrace / ToolCallTrace / RuntimeErrorTrace`：统一观测结构；
- `Planner Protocol`：屏蔽 OpenAI、Anthropic 或本地模型 SDK 差异。

这样做的原因是运行时最难的不是 `while`，而是状态语义。契约先稳定，工具、checkpoint 和评测才能共享同一种事实。

## 4. 工具注册与权限

`ToolRegistry` 同时支持同步和异步 handler。每个工具显式标记 `read / write / dangerous`；未注册工具属于 allowlist 之外，危险工具缺少人工确认时拒绝。

这里没有根据工具名字猜危险性，因为命名不是安全边界。权限必须在注册时由代码所有者声明。

## 5. SQLite checkpoint 与完整性验证

每条 checkpoint 保存：`run_id + step_no + state_json + state_sha256`。读取最新状态时重新计算哈希；不一致直接抛错，不把损坏状态交给 Planner。

初始化时自动创建数据库父目录。新增 `count()` 只用于测试和可观测验证，不参与运行时决策。

注意：SHA-256 能发现意外损坏，但没有密钥，不能抵御能够同时改 JSON 和哈希的攻击者。若要防恶意篡改，应使用 HMAC、签名或数据库访问控制。

## 6. 问题：第一批 apply_patch 基线不匹配

### 现象

大补丁预期 `__init__.py` 只有另一段 docstring，但实际文件已经包含 `AgentRuntime` import。`apply_patch` 报 `verification failed`。

### 结果判断

整批补丁原子失败；重新读取后确认 `checkpoint.py`、`pyproject.toml` 和 `runtime.py` 都没有留下半截修改。

### 解决

按实际文件内容拆成两个较小补丁：先改现有文件，再单独新增 `runtime.py`。经验是：补丁失败后不要假设“前半段已经成功”，必须重新读取目标文件确认原子性和当前基线。

## 7. Runtime loop 的关键语义

- run 创建后，Planner 调用前先保存初始状态；
- 普通工具成功后，Trace 和 observation 一同 checkpoint；
- 批量工具先做整体预算检查，再并发执行；
- 一个工具失败，run 失败关闭，异常保留在 Trace；
- 未注册工具在执行前失败；
- 高风险批次在执行前整体暂停；
- 人工拒绝后不再请求 Planner；
- 同一个 SQLite 文件可供新 Runtime 实例恢复审批中的任务。

## 8. 问题：只有工具预算仍可能无限循环

### 风险

`max_tool_calls` 能限制工具，但不能覆盖异常 Planner 产生大量非工具步骤的情况。只依赖它不是完整的终止条件。

### 解决

增加 `max_steps`。每轮调用 Planner 前检查步数，耗尽后写入 `step_budget_exceeded` 并失败关闭。测试用一个永不返回 finish 的脚本 Planner 验证恰好执行两步后停止。

## 9. 并发测试如何证明是真的并发

仅比较耗时容易受 CI 负载影响。测试工具维护 `active` 和 `maximum_active`：两个 coroutine 都先递增 active，再 `await`；若并发成立，`maximum_active == 2`。这比断言“耗时小于某阈值”更稳定。

`asyncio.gather` 保持结果顺序与请求顺序一致，因此 Trace 可以稳定对应 Planner 的 tool list。

## 10. 人工暂停与重启恢复测试

先用 Runtime A 运行到 `waiting_review`，随后完全创建 Runtime B（只共享 SQLite 路径）调用 `resume`。测试证明恢复依赖持久化状态，而不是 Runtime A 的内存。

危险工具测试还验证：暂停前执行次数为 0，批准后恰好为 1。拒绝测试验证 Planner 不会被再次调用。

## 11. 问题：pytest 在第 5 个用例后无输出卡住

### 初始现象

checkpoint 与 evaluation 的 5 个同步测试通过，进入第一个 runtime 测试后长时间无输出。

### 定位过程

1. 中止整套测试，单独运行第一个 runtime 用例，仍卡住；
2. 增加 pytest 内置 `faulthandler_timeout=8`；
3. 堆栈显示还未进入 `AgentRuntime.run`，而是卡在：
   `asyncio.run -> Windows ProactorEventLoop -> socket._fallback_socketpair -> accept`；
4. 因此排除状态机死循环，根因是 Windows 沙箱限制事件循环内部 socketpair。

### 解决

经用户授权，在沙箱外使用同一个 Python、同一代码、同一测试命令复验。第一轮结果 `16 passed in 0.31s`；加入步骤预算测试后最终复验为 `17 passed in 0.30s`。

### 可复用知识

“测试卡住”不能直接归因为业务死循环。先用 faulthandler 看线程停在哪里；若栈还在事件循环初始化，就应区分运行环境限制与代码缺陷。

## 12. 成对回归门禁

实现 `RunRecord`、`GateThresholds`、`GateReport` 和 `paired_regression_gate`。门禁要求：

- baseline/candidate case 集合完全一致；
- 同 case 的 category 一致；
- case_id 不重复；
- 至少有一条样本。

当前阻断指标为成功率下降、平均质量下降和平均错误数上升；工具调用数先作为观察指标。分类 delta 用于定位退化集中在哪一类任务。

## 13. 当前验证清单

- checkpoint round-trip 与哈希篡改检测；
- 同步/异步工具与危险工具权限；
- 单工具闭环与结构化 Trace；
- 批量工具真实并发；
- 工具预算失败关闭且零执行；
- 步骤预算终止异常 Planner；
- 显式暂停、新实例恢复、危险工具审批、人工拒绝；
- 工具异常与未注册工具失败关闭；
- paired gate 通过、三类退化失败、样本不配对拒绝。

## 14. 仍未完成，不能夸大

- 没有真实模型 provider adapter；
- 没有通用 crash replay、lease 或幂等工具协议；
- 没有认证、RBAC、多人审批或 UI；
- 没有 PostgreSQL/Redis 横向扩展；
- 没有 OpenTelemetry exporter；
- 没有选择本项目许可证；
- 当前测试是确定性离线证据，不等于生产 SLA。

## 15. 下一步计划

进入 `0.2`：增加可恢复执行租约、版本化评测数据集和一个真实业务 Agent adapter，使平台从“独立内核”连接到现有舆情 Agent，同时保持原业务仓库的发布门禁不退化。

## 16. 问题：pytest 能 import，但普通 Python 不能

### 现象

`compileall` 和 `pip check` 成功，随后普通 `python -c "import agent_runtime_platform"` 报 `ModuleNotFoundError`。

### 根因

这是标准 `src/` layout：包不在仓库根目录。pytest 会读取 `pyproject.toml` 的 `pythonpath = ["src"]`，普通 Python 不会；当前复验借用了另一个项目的虚拟环境，也没有把本项目 editable install 进去。

### 解决

- 正常开发使用 README 的 `pip install -e ".[dev]"`；
- 本轮为了不修改共享虚拟环境，验证命令显式把本仓库 `src` 加入 `sys.path`；
- TOML 解析和顶层包 import 随后通过，输出 `imports-and-toml-ok`。

这说明“pytest 通过”不能替代包安装检查；后续 CI 应增加创建干净 venv、安装 wheel/editable 包后再 import 的 job。

## 17. 本阶段最终复验与清理

- 完整测试：`17 passed in 0.30s`；
- `compileall`：通过；
- `pip check`：`No broken requirements found`；
- `pyproject.toml` 解析与源码包 import：通过；
- 4 个测试临时目录在解析绝对路径并确认位于项目目录内后删除；
- `fso-agent-benchmark` 与 `agent-reliability-eval` 的原有 dirty 状态仍在，未被本轮修改；
- 只读状态检查再次遇到 FSO 的 `.pytest_cache` 权限 warning，和第 2 节一致，没有尝试改权限或删除用户缓存。
- 新项目已初始化为独立 Git 仓库，当前分支 `main`；没有自动创建提交，方便用户先审查完整首版。

## 18. 0.2 恢复语义：为什么本地缓存不等于 exactly-once

目标是支持普通 running checkpoint 的崩溃恢复。关键故障窗口有两个：

1. handler 已完成且 tool ledger 已保存，但 step checkpoint 尚未保存；
2. 外部副作用可能已发生，但进程在保存 tool ledger 前崩溃。

第一种可以用确定性幂等键 `run_id:step_no:tool_index` 找回 Trace，避免再次调用。第二种仅靠本地 SQLite 无法判定，因此不能承诺 exactly-once。

实现策略：新增 `inflight_action` 意图 checkpoint、工具结果账本和 `replayed_from_cache` 标记；自动 replay 只允许 `read` 或显式 `replay_safe=True` 的工具。`compute/write/dangerous` 遇到不确定窗口时以 `unsafe_replay_blocked` 失败关闭。

## 19. 执行租约与 heartbeat

新增 `run_leases` 表，`run/resume/recover` 先原子获取 owner 租约，后台 heartbeat 周期续租，结束时只释放自己的租约。有效租约被其他 owner 持有时抛 `RuntimeLeaseBusy`。

测试覆盖：有效租约排他、过期后接管、非 owner 不能释放、旧 owner 在接管后不能续租、长工具调用期间 heartbeat 保持所有权。

## 20. 问题：23 项首测中的超短租约失败

### 现象

首轮共 23 项，22 项通过；`lease_ttl_seconds=0.03` 的长工具 heartbeat 测试报 `RuntimeLeaseLost`。

### 根因

续租 SQL 同时要求 owner 相同且 `expires_at > now`。Windows 定时器/测试调度发生轻微抖动时，同一 owner 的 heartbeat 可能刚过 30ms 才执行，即使没有其他 worker 接管，也被误判为失租。

### 修复

续租原子条件改为 owner 相同。若无人接管，同一 owner 可在调度迟到后续租；若其他 worker 已接管，owner 已改变，旧 worker 仍无法续租。新增“同 owner 迟到续租”和“接管后旧 owner 续租失败”回归。

修复后完整测试：`24 passed in 0.82s`。

## 21. 舆情 adapter 的接入方式

读取真实项目的 `AnalyzeRequest`、`AgentState`、`build_opinion_graph` 和 V2 worker 契约后，选择薄 adapter，而不是复制业务节点：

- Pydantic 输入限制与现有 API 对齐；
- Runtime 第 0 步调用 `analyze_opinion`，第 1 步 finish；
- 图的同步 `invoke` 放到 worker thread；
- 输出必须包含匹配的 event_id、结构化 analysis_report、tool_traces 和 errors；
- 输入/输出安全变换用依赖注入，避免平台依赖业务治理包；
- 工具权限使用新增的 `compute`，表达“无外部写入但可能付费、非确定且不应盲目重放”。

3 条 adapter 单测加入后为 `27 passed in 0.88s`。

## 22. 不只测 Fake：真实 LangGraph 跨仓库验证

新增可选 integration test，通过环境变量定位相邻舆情项目，再实际构建 `build_opinion_graph(StubClassifier)`。它跑通：

1. sentiment_classifier；
2. sentiment_aggregator；
3. briefing_composer。

最终结果 `28 passed, 2 warnings in 6.97s`。两条 warning 分别来自 LangGraph 待变更的 serializer 默认值，以及 jieba 间接使用已弃用 `pkg_resources`；都不是本轮 adapter 断言失败，但已保留为后续依赖升级事项。

## 23. 0.2 仍然不能宣称的能力

- SQLite lease 没有 fencing token，失租旧 worker 的外部请求不能被强制撤销；
- 工具 handler 尚未接收/透传幂等键到外部服务；
- 单机 SQLite 不等于 PostgreSQL 多 worker 生产协调；
- adapter 还没有替换舆情项目现有 V2 JobExecutor，只完成可验证连接；
- 真实模型、Redis/PostgreSQL、Docker 和生产并发尚未做组合测试；
- 两条第三方依赖 warning 尚未处理。

## 24. 二次审查：恢复时工具注册表可能已经变化

旧 checkpoint 中的 inflight tool 可能在新版本被删除或改名。第一版恢复代码调用 `is_replay_safe()` 时会直接抛 `ValueError`，既没有结构化错误，也没有把决定写回 checkpoint。

修复为和正常执行路径一致：记录 `tool_not_allowed`、清空 inflight、持久化 `failed_closed`。新增工具漂移回归，确保升级后的恢复不会在运行时边界外泄漏注册表异常。

## 25. 问题：工具漂移回归首次使用了非法 run_id

加入工具漂移测试后的全量结果是 28 通过、1 失败。失败前，恢复逻辑已经正确生成 `tool_not_allowed`；真正失败位置是 `RunResult` 校验，手工夹具使用了便于阅读的 `tool-drift-run`，但生产契约要求 UUID。

处理方式不是放宽 `RunResult.run_id`，而是把测试夹具换成合法固定 UUID。测试数据也必须遵守真实契约，否则会把夹具错误误判成业务错误。

## 26. 0.2 最终复验与清理

- 含真实舆情 LangGraph 的完整测试：`29 passed, 2 warnings in 5.75s`；
- `compileall`：通过；
- `pip check`：`No broken requirements found`；
- `pyproject.toml`、Runtime 与 Opinion adapter import：通过；
- 本轮 6 个 `.pytest-tmp-*` 目录经绝对路径边界验证后删除；
- 版本更新为 `0.2.0.dev0`；
- 没有修改两个冻结来源仓库；舆情主仓库只追加总工程日志，不改变业务代码。

保留的两条 warning：LangGraph serializer 的未来默认值变化，以及 jieba 间接依赖已弃用的 `pkg_resources`。它们不阻断当前确定性集成测试，但升级依赖前必须单独验证。

## 27. 先写 ADR：为什么不能直接接入 V2 JobExecutor

舆情 V2 已经有 PostgreSQL Job lease、heartbeat、重试与结果状态；Runtime 0.2 又有 SQLite lease。若直接嵌套，会形成双 owner、双 heartbeat、双 retry，并且两个状态源可能得出不同结论。

新增 `docs/adr/0001-v2-runtime-integration-boundary.md`，接受的决策是：

- 生产协调唯一事实源继续是 V2 PostgreSQL repository；
- SQLite Runtime 目前只用于离线、开发与 shadow 验证；
- 正式接入前必须拆分可注入 backend，并实现 PostgreSQL adapter；
- 先完成外部幂等键，再设计 fencing token；
- 不在本轮修改 V2 JobExecutor。

这避免了为了“看起来已经集成”而降低现有生产架构的一致性。

## 28. 外部幂等键透传

新增 `ToolExecutionContext`：

- `idempotency_key`；
- `run_id`；
- `step_no`；
- `tool_index`；
- `lease_owner`。

工具通过 `inject_context=True` 显式选择接收。Runtime 使用独立关键字 `runtime_context` 注入，Planner arguments 若包含同名字段会被拒绝，不能伪造或覆盖运行时元数据。Trace 同时记录幂等键，便于审计。

字段使用可选注入而不是强行传给所有 handler，是为了保持现有工具签名兼容。`ToolCallTrace.idempotency_key` 暂时可空，以便读取 0.2 已写入的旧账本。

## 29. 故障注入：远端已提交、本地尚未记账

新增测试模拟最危险窗口：

1. 第一次 handler 用幂等键写入“远端”字典；
2. 远端写成功后抛 `SimulatedCrash`，本地 tool ledger 尚未保存；
3. 新 Runtime 从 inflight checkpoint 恢复；
4. handler 第二次收到完全相同的幂等键；
5. 假远端返回已存在结果，不重复产生副作用；
6. Runtime 完成任务并在 Trace 中保留该 key。

断言证明 handler 被调用两次，但外部 effect 只有一条。这是“外部协议 + 稳定幂等键”带来的去重，不是本地 SQLite 凭空实现 exactly-once。

## 30. 0.2.1 最终复验（2026-10-15）

- 含真实舆情图和外部幂等故障注入：`32 passed, 2 warnings in 13.25s`；
- `compileall`：通过；
- `pip check`：`No broken requirements found`；
- TOML 与 `ToolExecutionContext` import：通过；
- 1 个测试临时目录经绝对路径边界检查后删除；
- 版本更新为 `0.2.1.dev0`；
- 两条既有第三方 warning 未新增、未隐藏。

仍未完成：fencing token、可注入持久化 backend、PostgreSQL Runtime adapter、V2 shadow runner。ADR 将这些列为进入生产灰度前的硬门槛。

## 31. 为什么 backend 必须从一开始就是 async

仅把 SQLite 类换成同步 Protocol 虽然改动小，但未来 PostgreSQL 实现会在 async worker 事件循环中执行同步数据库 I/O。那会阻塞 heartbeat、工具并发和其他任务，形成隐藏的吞吐与失租风险。

因此 `RuntimeBackend` 从第一版就定义为 async，覆盖 checkpoint、lease 和 tool ledger。现有 stdlib SQLite 保持同步实现，通过 `AsyncSQLiteBackend + asyncio.to_thread` 适配。

## 32. 向后兼容与唯一持久化入口

为了不一次性破坏现有调用方：

- 旧写法 `checkpoints=SQLiteCheckpointStore(...)` 继续可用；
- 新写法使用 `backend=RuntimeBackend`；
- 两者必须且只能提供一个；
- Runtime 内部只调用 `self.backend`；
- `get()` 改成 async，避免为读路径重新引入同步 I/O。

新增测试证明 backend-only 能完成 run 和 get，也验证同时提供/都不提供会立即失败。

## 33. 二次审查：不能用真假值选择 backend

初版赋值使用 `backend or AsyncSQLiteBackend(...)`。自定义 backend 可能实现 `__bool__` 并返回 False，这时明明显式注入的对象会被错误替换。

修复为 `backend is not None` 的身份判断，并新增 `FalsySQLiteBackend` 回归。这个问题在常规类上不出现，但 backend 是扩展点，不能假定第三方对象永远 truthy。

## 34. 0.3 最终复验（2026-10-15）

- 完整测试（含真实舆情图、外部幂等故障注入、backend-only/falsy backend）：`35 passed, 2 warnings in 5.39s`；
- 版本更新为 `0.3.0.dev0`；
- 异步 backend 已成为 Runtime 内部唯一持久化路径；
- `compileall`、`pip check`、TOML 与 backend import 通过；
- 2 个测试临时目录经绝对路径边界检查后删除；
- PostgreSQL 实现仍未交付，不能把 Protocol 通过等同于生产数据库通过。

## 35. PostgreSQL 接入前先审计现有 V2 schema

读取了舆情项目 `0001_auditable_jobs.sql`、`0002_leases_and_checkpoints.sql`、`PsycopgJobRepository` 和真实 Postgres/Redis integration test。确认：

- `analysis_jobs` 的 lease 属于业务 Job owner；
- `agent_runs` 属于一次业务 attempt；
- `run_checkpoints` 只保存 input replay/completed/failure 边界，不是 Planner 每步状态；
- 现有 repository 使用 psycopg async connection 和显式事务。

因此 Runtime 不能复用同名/近似表，也不能向现有表偷偷增加自己的状态列。

## 36. ADR-0002：独立表和事务边界

新增 ADR 规定 Runtime 只拥有 `runtime_checkpoints`、`runtime_run_leases`、`runtime_tool_results`。migration 不 ALTER 舆情表，生产也不在应用启动时自动迁移。

长时间模型/工具调用不放进数据库事务。Lease 接管和 token 递增由单条 upsert 完成；heartbeat/release 匹配 owner + token；tool result 以幂等键首次写为准。

## 37. SQLite 与 PostgreSQL fencing token

SQLite schema 新增 `fencing_token`，初始化会检测旧数据库列并执行兼容 ALTER。`LeaseGrant` 取代简单 bool：

- 首次获取为 1；
- 释放或过期后再次接管递增；
- 相同 owner 在有效租约内重复 acquire 不递增；
- stale owner/token 无法续租或释放；
- token 注入 `ToolExecutionContext`。

新增回归证明 token 递增、旧 token 被拒绝以及工具看到正整数 token。

## 38. PostgreSQL backend 开发实现

新增异步 `PostgresRuntimeBackend`：checkpoint 哈希、原子 lease upsert、带 token 的 renew/release、tool trace 首次写入和读取完整性验证。psycopg 放在可选 `postgres` extra，普通安装不被强制增加数据库依赖。

新增 migration contract 与 fake async connection 测试，检查：

- 只创建 `runtime_*` 表；
- 不 ALTER/CREATE `analysis_jobs`；
- checkpoint SQL 和哈希往返；
- lease 返回 fencing grant；
- renew 参数实际包含 token；
- backend 满足 RuntimeBackend 结构协议。

首轮完整结果 `42 passed, 2 warnings in 5.42s`。

## 39. 真实 PostgreSQL 测试为什么是 skipped

新增 `tests/integration/test_postgres_runtime_backend.py`，在提供 `RUNTIME_DATABASE_URL` 时会：执行 migration、运行最小 Runtime、从 PostgreSQL 读回状态并清理该 run 数据。

当前主机没有 Docker，也没有提供专用 PostgreSQL DSN。最终测试明确显示 `1 skipped`，而不是把 fake connection 测试称为真实数据库验证。文档同时警告只能对专用测试数据库执行。

## 40. 完整 fencing 仍未完成

当前 token 已保护 renew/release，并能传给下游；但 checkpoint/tool ledger 写入还没有 owner + token 条件。heartbeat 检测存在时间窗口，失租旧 worker 可能在发现之前写平台状态。

ADR-0002 状态因此是 `Accepted for development; Production blocked`。下一步必须把 LeaseGrant 传入 backend 写方法，并用真实双 worker PostgreSQL 测试 stale write 拒绝。

## 41. 0.4 最终复验（2026-10-15）

- `42 passed, 1 skipped, 2 warnings in 5.59s`；
- skipped：未配置真实 PostgreSQL；
- `compileall`、`pip check`、TOML、LeaseGrant/Postgres backend import 通过；
- 2 个测试临时目录经绝对路径边界检查后清理；
- 版本更新为 `0.4.0.dev0`；
- 两条既有 LangGraph/jieba warning 未新增。

## 42. 完整 fencing 首测再次暴露墙钟问题

加入 fenced checkpoint/tool-result 后首轮为 `44 passed, 1 failed, 1 skipped`。失败仍是 30ms TTL 长工具测试：没有其他 worker 接管，但写入 SQL 同时检查 `expires_at > now`，Windows 调度抖动导致假 stale。

修复依据：fencing 识别的是 lease 代际，不是某一瞬间的本地墙钟。真正接管会原子递增 token；若 owner+token 仍匹配，就没有新代际。因此写入条件改为当前 owner+token，heartbeat 仍负责活性，接管测试仍必须拒绝旧 token。

## 43. Fenced state writes 的实现

`RuntimeBackend.save_checkpoint/save_tool_trace` 现在强制接收 LeaseGrant。Runtime 用 ContextVar 保存当前异步任务的 grant，避免在每层业务函数重复传参，同时保证并发 run 各自隔离。

- SQLite：`BEGIN IMMEDIATE` 后校验当前 owner+token，再写 checkpoint/tool result；
- PostgreSQL：`INSERT ... SELECT ... WHERE EXISTS` 在同一语句校验；
- backend 拒绝时抛 `StaleLeaseWriteError`；
- Runtime 对外转换为 `RuntimeLeaseLost`；
- tool result 写入也受到保护，旧 worker 不能留下“成功”Trace。

## 44. 接管故障注入

测试工具在执行中主动释放旧 lease，并让 `new-worker` 接管，随后旧 handler 返回成功。断言：

- 新 token 大于旧 token；
- 旧 Runtime 收到 `RuntimeLeaseLost`；
- `run_id:0:0` tool trace 不存在；
- stale checkpoint/tool trace 的底层直接写也被拒绝。

这证明旧 worker 即使业务调用已经返回，也不能覆盖新代际状态。

## 45. 0.4.1 最终复验（2026-10-15）

- `45 passed, 1 skipped, 2 warnings in 5.33s`；
- skipped 仍是没有真实 PostgreSQL DSN；
- 版本更新为 `0.4.1.dev0`；
- SQLite stale-write 竞争已验证；
- PostgreSQL fenced SQL 通过 fake connection/契约测试，真实双 worker 仍未验证。
- `compileall` 通过，说明 `src/` 与 `tests/` 没有语法或字节码编译错误；
- `pip check` 返回 `No broken requirements found`；
- `pyproject.toml` 可由标准库 `tomllib` 解析，`StaleLeaseWriteError` 与 `PostgresRuntimeBackend` 可从包公共接口导入；
- 2 个 `.pytest-tmp*` 测试临时目录先解析绝对路径并验证位于项目根目录内，再执行删除；清理后复查数量为 0。

## 46. 审计发现 PostgreSQL tool trace 残留墙钟条件

继续检查生产阻断项时发现：checkpoint fenced SQL 已只校验 `owner_id + fencing_token`，但 tool trace SQL 仍残留 `expires_at > CURRENT_TIMESTAMP`。这与 0.4.1 的设计结论冲突，可能让没有发生接管、只是事件循环调度稍晚的合法 worker 被误判为 stale。

修复：删除 tool trace 的过期时间条件，新增两个契约测试，分别验证 fence 子句只依赖代际、以及旧 token 返回空行时抛出 `StaleLeaseWriteError`。定向结果为 `8 passed`，后续连接池测试加入后该文件为 `12 passed`。

## 47. 显式 PostgreSQL 连接池生命周期

`PostgresRuntimeBackend` 新增 `open/close` 和异步上下文管理器。设计依据是 psycopg 官方建议：异步池用 `open=False` 构造，再显式 `await pool.open()`；本项目使用 `wait=True`，使 DSN、权限或网络错误在启动阶段失败，而不是延迟到首个请求。官方参考：<https://www.psycopg.org/psycopg3/docs/api/pool.html>。

- 默认池大小为 1 到 10，可配置启动超时；
- 参数在构造时校验；
- 池打开失败会执行 close，避免后台资源泄漏；
- `async with backend` 退出时关闭池；
- 未显式打开时保留原逐操作连接模式，兼容短脚本；
- `postgres` extra 新增 `psycopg-pool>=3.2,<4`。

当前环境没有安装 `psycopg-pool`，所以用 fake pool 验证构造参数、`open(wait=True)`、连接借还和 close；不能把它表述成真实连接池验证。

## 48. 真实 PostgreSQL 双连接池竞争测试入口

可选集成测试不再只跑顺序生命周期。提供 `RUNTIME_DATABASE_URL` 后，它会打开两套独立连接池，释放旧 lease 后让两个不同 owner 并发 acquire，并断言：

- 恰好一个 contender 获胜；
- winner token 大于旧 token；
- 旧 grant 的 checkpoint 写入被拒绝；
- winner 可以写入并读回状态；
- 测试结束清理两类 run 数据，但保留 runtime 表。

这段测试已经可收集、可跳过，但本机没有专用 PostgreSQL，因此尚未取得真实数据库通过证据。

## 49. 0.5.0 完整测试中的 Windows 临时目录问题

第一次完整测试得到 `20 passed, 1 skipped, 31 errors`。31 个错误都发生在 fixture setup，根因是 pytest 无权扫描 `C:\Users\29007\AppData\Local\Temp\pytest-of-29007`，不是业务断言失败。

解决：使用项目内唯一目录 `--basetemp .pytest-tmp-v050` 重跑。最终结果为 `51 passed, 1 skipped, 2 warnings in 5.61s`。skipped 仍是真实 PostgreSQL；两条 warning 仍来自既有 LangGraph serializer 默认值和 jieba/pkg_resources。

## 50. 阶段结束后的路线校准

按维护者要求，阶段完成后重新读取最初六周计划、两个 ADR、舆情 V2 代码/CI/Compose 与当前 Git 状态。结论：0.5.0 仍属于原计划第 5 周 Runtime 整合，没有变成第四个普通 Demo；舆情 V2 的第 1–4 周主体能力也确实先于 Runtime 建设。

同时确认了拐点：Runtime 核心已经足够支撑集成，继续增加抽象会开始偏离目标。下一顺序校准为：Runtime CI 真实 PostgreSQL证据 -> 默认关闭的 V2 shadow -> 版本化任务集与组合门禁 -> 展示包装。FSO、通信场景和 Multi-Agent 延后。

风险记录：Runtime 新仓库当前 `main` 没有初始 commit，文件全部 untracked；本轮没有擅自提交。完整对照与验收线记录在舆情项目 `docs/V2_ROADMAP_ALIGNMENT_2026-10-15.md`。

收尾验证第一次在 Runtime 工作目录中用相对路径读取上述舆情文档，得到 `FileNotFoundError`。原因是验证脚本工作目录选择错误，不是文档缺失；切换到舆情仓库后内容断言返回 `roadmap-alignment-doc-ok`。

## 51. P0：Runtime CI 真实 PostgreSQL 门禁配置

新增 `.github/workflows/ci.yml`，将证据拆成两个独立 job：

- `unit-and-contract`：只安装 `.[dev]`，运行 `-m "not integration"`，避免独立仓库因缺少舆情兄弟目录而误跑跨仓库测试；
- `postgres-integration`：启动 PostgreSQL 16 service，设置非空 `RUNTIME_DATABASE_URL`，安装 `.[dev,postgres]`，只运行真实 PostgreSQL lifecycle/takeover 测试；
- 两个 job 都执行 `pip check`，并用 `actions/upload-artifact@v4` 保存 JUnit 报告；
- 工作流权限保持 `contents: read`，没有加入部署或写仓库权限。

新增 CI 契约测试，防止后续删除 DSN、PostgreSQL service、pool extra、指定集成测试或报告上传。定向结果：`14 passed in 0.17s`。

## 52. 0.5.1 本地回归与证据边界

版本更新为 `0.5.1.dev0`。完整本地结果为 `53 passed, 1 skipped, 2 warnings in 5.56s`；skip 仍是未设置真实 PostgreSQL DSN，两条 warning 未变化。

关键边界：本地测试证明工作流文本契约和所有可运行代码通过，但 Runtime 仓库尚无初始 commit，工作流还没有进入 GitHub，也没有 Actions run。因此当前状态只能写“CI 已配置”，不能写“CI 已通过”。JUnit 输出目录 `reports/` 已加入 `.gitignore`。

## 53. 初始提交的作者与内容审计

维护者要求仓库只保留本人一个作者。提交前核对结果：全局 Git 身份为 `KristenYue <yueyt1112@163.com>`，与舆情仓库既有提交身份一致；Runtime 仓库没有局部 user.name/user.email 覆盖。初始提交不加入 `Co-authored-by` 或 AI 作者标记。

提交内容审计未发现 API key、云访问密钥或密码；`.pytest_cache`、`__pycache__` 和本地测试目录均被忽略。第一次 `git diff --cached --check` 捕获本文件开头两处 Markdown 行尾空格，已删除后重新检查。Git 同时给出 LF/CRLF 转换提示，这是当前 Windows Git 换行配置提示，不是敏感信息或语法错误。

## 54. 本地仓库可评审基线

初始提交成功创建，主题为 `Build agent runtime quality platform v0.5.1`。提交后用 Git metadata 和 `git shortlog -sne --all` 双重核对：Author、Committer 和唯一作者均为 `KristenYue <yueyt1112@163.com>`，没有 `Co-authored-by`。

远端仍未建立：`git remote -v` 为空；GitHub CLI 检测到 `KristenYue` 账号配置，但 token 已失效。为避免替维护者创建公开/私有仓库或变更账号认证，本阶段不自动处理远端。维护者重新登录并决定仓库可见性后，再添加 `origin` 和 push，届时才能取得真实 Actions 结果。
