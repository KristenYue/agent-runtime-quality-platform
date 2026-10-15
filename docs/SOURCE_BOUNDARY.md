# 来源边界与冻结仓库保护

## 结论

本项目是独立的 clean-room 实现。创建它的原因是原有两个项目已经冻结且存在用户自己的未提交文件；直接在其中重构会同时破坏可复现基线和用户工作。

## 只读参考

### `../fso-agent-benchmark`

- 冻结状态：保留；
- 本轮观察到的能力：provider-neutral loop、工具预算、失败恢复等概念；
- 明确缺口：checkpoint、并发和人工中断；
- 本项目没有复制其源文件；接口、状态模型和实现均在新目录独立编写；
- 仓库声明 MIT，但本项目仍需单独决定自身许可证。

### `../agent-reliability-eval`

- 冻结状态：保留；
- 本轮观察到的能力：paired regression、trajectory adapter、测试结构；
- 本项目只采用“同一 case 成对比较”的一般评测思想，重新设计了数据结构和实现；
- 没有复制 AgentEvals 或该仓库的源文件；
- 原仓库是 MIT 来源，若未来复制任何代码，必须单独登记文件、版权与许可证文本。

## 本轮没有做的事

- 没有修改两个冻结仓库；
- 没有清理它们的 dirty worktree；
- 没有把旧提交历史移入新项目；
- 没有声称新项目继承任何生产验证结论；
- 没有擅自为新项目选择 LICENSE。

## 后续引入第三方代码的规则

1. 先记录来源 URL、commit、文件与许可证；
2. 判断是思想参考、重新实现还是代码复制；
3. 复制代码时保留版权声明和许可证义务；
4. 将依赖和许可证加入 NOTICE/SBOM；
5. 未完成核对前，不在 README 写“已开源”或“可商用”。

## 0.2 跨仓库 adapter 边界

新增 `adapters/opinion_agent.py` 仅依据公开的 TypedDict/Pydantic 契约构造输入并校验输出，没有复制 `opinion_agent` 节点、图、治理或模型代码。运行时通过用户传入的 `graph_provider` 获取图对象。

`tests/integration/test_opinion_agent_adapter.py` 只有在显式设置 `OPINION_AGENT_PROJECT` 后才将目标仓库 `src` 加入测试进程搜索路径。它用于本地组合验证，不把相邻仓库变成本项目安装依赖。

## PostgreSQL 表边界

Runtime migration 使用 `runtime_*` 独立命名空间，不复制或修改舆情 V2 migration。表结构是依据 Runtime 自身的 checkpoint、lease、tool ledger 契约重新设计。

`PostgresRuntimeBackend` 的 psycopg 使用方式参考了相邻项目现有异步连接风格，但 SQL、表名和状态语义在本项目独立实现。真实组合边界见 ADR-0002。
