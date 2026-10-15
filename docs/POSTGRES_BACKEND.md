# PostgreSQL Runtime Backend

## 当前状态

`PostgresRuntimeBackend` 已实现异步 psycopg 接口、显式连接池生命周期和独立 migration，当前证据是单元/SQL/连接池契约测试。由于当前主机没有 Docker，尚未在本轮运行真实 PostgreSQL，不能视为生产验证完成。

## 安装

```powershell
python -m pip install -e ".[dev,postgres]"
```

`postgres` extra 同时安装 `psycopg` 与 `psycopg-pool`。生产运行建议显式管理连接池：

```python
backend = PostgresRuntimeBackend(
    dsn,
    pool_min_size=2,
    pool_max_size=10,
    pool_timeout_seconds=30,
)
async with backend:
    result = await runtime.run(task_id="example", request="...")
```

进入上下文时使用 `open=False` 创建池，再显式 `open(wait=True)`，使配置错误在服务启动阶段暴露；退出时关闭池。未进入上下文时仍保留逐操作连接模式，方便脚本与向后兼容，但常驻服务不建议这样使用。

## Migration

在专用开发/测试数据库执行：

```powershell
psql "$env:RUNTIME_DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/0001_postgres_runtime.sql
```

不要让 Runtime 应用账号拥有修改舆情 V2 业务表的权限。建议只授予 `runtime_*` 表的 SELECT、INSERT、UPDATE、DELETE 和 sequence 权限。

## 可选真实集成测试

```powershell
$env:RUNTIME_DATABASE_URL = "postgresql://.../test_database"
python -m pytest tests/integration/test_postgres_runtime_backend.py -q
```

测试会创建 `runtime_*` 表、运行一个最小任务，再让两套独立连接池并发争抢已释放 lease。它验证只有一个 winner、token 递增且旧 grant 无法写 checkpoint，最后删除测试数据；表本身保留。只应对专用测试数据库执行。

仓库 `.github/workflows/ci.yml` 已配置独立的 `postgres-integration` job，使用 PostgreSQL 16 service 执行同一个测试并上传 JUnit 报告。由于当前 Runtime 仓库尚无初始 commit，本地只能验证工作流契约，不能把“已配置”写成“GitHub Actions 已通过”。

## 已知阻断项

- fenced writes 已有 SQLite 故障注入与 PostgreSQL SQL 契约测试，但尚无真实 PostgreSQL 双 worker 验证；
- 连接池生命周期已有 fake-pool 契约测试，但尚未在真实 PostgreSQL 验证资源回收和超时；
- 真实双 worker 接管测试已编写，但当前环境无 DSN，仍会跳过；
- 没有和 V2 JobExecutor 的 shadow 联调；
- 没有生产 migration/rollback 演练。
