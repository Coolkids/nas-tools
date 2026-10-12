# 数据库与存储维护说明

本文说明数据库、缓存文件的固定位置，以及设置页面中的迁移、备份和恢复流程。数据库迁移和恢复会暂停部分业务访问；开始前应确认配置目录已持久化，并留出足够的磁盘空间。

## 存储位置

| 内容 | 固定位置 | 说明 |
| --- | --- | --- |
| SQLite 业务数据库 | `<配置目录>/db/nastools.db` | 保存用户、订阅、识别记录、媒体索引等业务数据；SQLite 默认使用此库 |
| 本地持久化缓存 | `<配置目录>/cache/system.sqlite3` | 仅在缓存保存方式为“内存 + 本地持久化”时使用；与业务数据库分开 |
| 维护任务状态 | `<配置目录>/.maintenance/` | 保存迁移、备份和恢复任务记录；不要手动删除运行中的任务文件 |

Docker 部署应将整个 `/config` 持久挂载到主机目录。只挂载 `config.yaml` 会丢失数据库、缓存以及维护任务恢复所需的文件。业务数据库完整备份不包含缓存或配置文件。

SQLite 的文件名和目录由后端固定生成，不能在设置页或 API 中修改。没有数据库配置时使用 SQLite；显式选择 PostgreSQL、MySQL 或 MariaDB 后，连接失败会显示错误，不会自动切回 SQLite。

## 数据库设置

在“设置 → 数据库”查看当前活动数据库、连接状态、结构版本、逻辑域和表统计。大库初始统计可能是估算值；需要精确结果时运行“刷新精确统计”，任务完成后查看实际记录数。

远程数据库连接参数在设置页填写。PostgreSQL 使用同步 `psycopg` 驱动；MySQL 和 MariaDB 使用 `PyMySQL`，并要求数据库字符集为 `utf8mb4`、默认表引擎支持事务。TLS 证书路径由 NAS-Tools 服务进程所在主机读取。密码由后端保存，读取设置时不会返回原密码。

连接到远程目标时，目标必须是专用空数据库：已有用户表、视图、序列或应用结构版本都会阻止迁移，即使这些表当前没有记录也一样。准备切换时按页面顺序操作：

1. 选择目标数据库并填写连接参数，然后运行“检查连接与空库”。
2. 查看产品版本、能力检查和目标冲突。检查凭证在参数或活动配置变化后失效，需要重新检查。
3. 检查通过后启动迁移。任务会固定源数据快照、分批复制数据并校验结构、记录数和内容摘要。
4. 只有校验通过后才会保存数据库配置并申请受控重启。重启后页面重新读取活动连接和任务状态，确认新库启动验证成功后才报告切换完成。

远程数据库切回 SQLite 时，若固定位置已有旧业务库，页面会先要求运行“准备 SQLite 空库”。该步骤备份并退役已识别的业务 SQLite 文件，随后检查固定空目标；新 SQLite 库的数据来自当前远程活动库。缓存数据库和无关文件不在删除范围内。

### 旧版 SQLite 升级

从旧版本启动时，程序会在业务服务初始化前检查配置目录根部和 `db/` 下的旧 `user.db`、`media.db`。识别到唯一、可验证的旧布局后，程序对源库及 WAL 中已提交数据创建快照，在暂存区升级并合并为 `db/nastools.db`，再校验和发布。源布局冲突、损坏或无法识别时会停止启动并保留源文件供排查，不会用新空库覆盖旧数据。

合库成功并通过启动检查后，旧数据库会归档在 `db/.legacy-archive/`，保留期为 30 天。不要手动把归档文件改回 `user.db` 或 `media.db`。

## 完整数据库备份与恢复

完整备份覆盖统一结构中的 main、media 两个逻辑域，采用压缩 ZIP 文件和 JSONL 表数据。备份不包含 `config.yaml`、缓存文件或连接密码；数据库记录中的业务数据仍属于备份内容。备份生成在后台运行，可在任务区域查看状态，完成后下载文件。

恢复流程为“上传 → 检查 → 确认替换 → 恢复”。上传文件先保存在维护目录，不会立即修改活动数据库。检查通过后，恢复任务先生成当前业务库的恢复前备份，再在暂存区构建、校验并提交目标数据。SQLite 发布使用固定的 `db/nastools.db` 路径；远程数据库恢复要求现有结构与备份结构兼容。配置中的数据库连接信息不会被备份数据覆盖。

若上传的是历史设置备份而不是完整数据库备份，页面会按其实际内容提示可导入范围；缺少 `media.db`、历史记录或其他应用表的文件不能按完整数据库恢复。

下载的完整备份应存放在配置目录之外，并定期确认文件可读取。维护目录中的过期临时备份和上传文件会按保留策略清理。

## SQLite 空间回收

“回收空间”仅作用于当前 SQLite 整库，不删除业务记录，也不支持选择 main/media 范围。页面提供空闲页估算、数据库文件和 WAL 占用；执行后显示实际占用变化。

回收需要独占维护窗口。程序会暂停新的数据库访问、等待在途事务结束、检查 WAL checkpoint 状态，再执行 SQLite `VACUUM` 并校验数据库。可用空间不足、数据库健康检查失败或其他维护任务运行时，页面会显示不可执行原因。远程数据库不提供此操作。

## SQLite 查询计划评估

识别记录分页用 20 万条合成记录对比了当前 ORM 查询形状的执行计划。仅按创建时间和请求 ID 排序时，旧单列索引需要额外临时 B-tree 排序；`(CREATED_AT, REQUEST_ID)` 可直接覆盖稳定排序。来源、状态和时间范围查询使用 `(SOURCE, CREATED_AT, REQUEST_ID)` 后也能按索引顺序分页。新索引替换原来的 `SOURCE` 与 `CREATED_AT` 单列索引。

同一评估中，媒体存在性查询的 `(SERVER, GENERATION_ID, TITLE, YEAR)` 候选没有改变 `MULTI-INDEX OR` 计划；转移历史 `(DATE, ID)` 候选也没有消除 NULL 日期排序表达式产生的临时排序，所以未增加这些索引。计划评估用于筛选候选方案，不代表生产环境的请求延迟或写入开销验收；实际规模与同步窗口测量仍待第 8.6 节完成。

## 测试环境记录

验证日期：2026-10-13。测试使用 Python 3.14.4、SQLAlchemy 2.1.4、临时 SQLite，以及三个专用且运行前确认为空的远程测试库。远程契约用例结束后再次确认三个测试库均无遗留表。驱动和服务版本仅代表本次验收环境，不构成对其他版本的兼容承诺。

| 后端 | 服务端版本 | 驱动版本 | 启动/重启方式 | 空库 schema 初始化耗时 | 验收日期 |
| --- | --- | --- | --- | --- | --- |
| SQLite | Python `sqlite3`（随 Python 3.14.4 提供） | Python 标准库 | 隔离临时配置目录直接运行 `run.py`；健康接口、登录、SQLite 业务库/缓存文件创建和 SIGINT 退出通过 | 应用启动 smoke test 通过；未单独计时 | 2026-10-13 |
| PostgreSQL | 18.6 (Ubuntu build) | `psycopg` 3.3.6 | 应用级 SQLAlchemy 契约及 `app_database.update_db()` schema bootstrap；应用进程重启未在该远程配置下测试 | 0.152 秒，34 张应用表 | 2026-10-13 |
| MySQL | 8.4.11 Community | `PyMySQL` 1.2.3 | 应用级 SQLAlchemy 契约及 `app_database.update_db()` schema bootstrap；应用进程重启未在该远程配置下测试 | 4.527 秒，34 张应用表 | 2026-10-13 |
| MariaDB | 11.8.9-MariaDB-ubu2404 | `PyMySQL` 1.2.3 | 应用级 SQLAlchemy 契约及 `app_database.update_db()` schema bootstrap；应用进程重启未在该远程配置下测试 | 4.831 秒，34 张应用表 | 2026-10-13 |

`tests/test_database_engine_contract.py` 的隔离远程运行结果为 9 passed。该结果验证 CRUD、事务回滚、批量插入/upsert、唯一约束、分页、自增、非连续主键、会话归还，以及四种后端的空库 metadata 初始化。它不代表执行了跨数据库业务数据迁移，也不代表备份恢复矩阵通过。计时只包括新空库的应用 schema 初始化，不包括跨库导入、备份、恢复或进程重启。

`tests/test_database_backup_contract.py` 在 SQLite、PostgreSQL、MySQL、MariaDB 上生成并校验完整归档，并通过低层导出、校验、导入接口运行全部 16 个源/目标组合；远程执行结果 `20 passed`（含四种源归档检查）。数据包含 Unicode、长路径、JSON 和空表，恢复后校验主键并执行新增写入。测试发现并修复空表预期摘要遗漏和 PostgreSQL 序列校准传参问题。该矩阵不覆盖完整后台任务生命周期、上传检查、维护锁、原子发布/重启或大数据压力。

`tests/test_database_migration_contract.py` 通过 `_copy_table`、逐表内容摘要和自增校准验证全部 16 个源/目标引擎组合，远程运行 `16 passed`。每个用例从空隔离库开始，复制含 Unicode、长路径和 JSON 的样例，校验目标摘要并验证新增主键。相同远程实例的用例使用本地 SQLite 快照作为隔离中间库，因为正式流程应拒绝源/目标为同一实例。此项不覆盖 `start_database_migration` 的检查凭证、维护锁、配置提交、重启及故障恢复编排。

`tests/test_cache_migration_matrix.py` 通过临时 Valkey 服务覆盖 memory、memory_disk、Redis 客户端和 Valkey 客户端之间的 12 个有向切换，以及两种远程客户端各自换到另一服务实例；结果为 `14 passed`。用例验证数据/TTL 保留、任务代次隔离、配置在校验后保存、受控重启调度和启动后目标核验，也确认认证令牌与配置加载缓存被列为排除项。检查时发现并修复了 memory_disk 首次遍历命名空间会更新本地 revision、导致迁移误判源变化的问题。当前环境没有原生 Redis 服务，因此 Redis 客户端路径由 Redis 协议兼容的 Valkey 服务承载；Docker 镜像构建及容器重启仍待 daemon 权限可用后验证。

`tests/test_cache_legacy_migration.py` 注入旧迁移标记删除失败，验证重启后可继续清理，且“旧 CacheLib 数据导入 → 清空 → 重启”不会复活；已知 `cache/migrations/` 删除，邻近 `db_scripts/` 文件不受影响，结果为 `1 passed`。

2026-10-13 本地回归：后端 `338 passed, 43 skipped, 83 subtests passed`；隔离缓存容量压力 `2 passed`；前端 `npm run type-check` 和 `npm run build` 均通过。跳过项包含未注入远程设置和门控压力用例；远程契约、备份归档矩阵、迁移复制矩阵和压力用例另行运行并记录在上文。

使用 Google Chrome 154 headless 加本地 mock `/do` 响应加载构建产物，缓存和数据库标签在 360、390、768、1280、1440 px 与浅/深色主题下均无页面横向溢出。此项只验证空数据状态的布局，不覆盖真实 API 联调、含数据表格、软键盘、安全区、放大、键盘/屏幕阅读器或迁移/恢复交互。

完整后台备份/恢复仍未完成端到端验收。本次已对隔离远程库实际生成归档并运行低层导入，但未通过上传、检查、维护锁、恢复任务、原子发布和受控重启全链路恢复。暂不记录端到端恢复耗时；正式启用恢复前仍需在隔离副本完成任务生命周期、损坏包和中断回滚验收。

直接运行 smoke test 使用临时配置目录和 SQLite，验证 `/index`、登录以及 SIGINT 停止。PyInstaller 6.22.2 Linux x86_64 onedir 产物在另一临时配置目录启动，数据库升级/初始化、`/index` 健康检查及 SIGTERM 退出通过；冻结入口通过 `multiprocessing.freeze_support()` 避免资源跟踪器递归启动。Windows 可执行文件未构建。`docker compose -f docker/compose.yml config --quiet` 静态检查通过；Docker CLI 可用，但当前执行用户无法访问 daemon socket，镜像构建、Supervisor 容器启动和容器内受控重启均未验证。前端 `npm run type-check` 与 `npm run build` 已通过；浏览器空状态布局已按上文验证，真实 API、可访问性和迁移/恢复交互仍待验收。

运行本地测试：

```bash
.venv/bin/python -m pytest -q
NASTOOLS_RUN_CACHE_PRESSURE=1 .venv/bin/python -m pytest tests/test_cache_unbounded_pressure.py -q
```

远程契约测试只应对独立空测试库执行。通过 `NASTOOLS_TEST_DATABASE_SETTINGS` 提供 PostgreSQL、MySQL、MariaDB 的 JSON 连接配置；完整 schema bootstrap 用例会拒绝非空数据库，并在完成后删除本次创建的应用表。不要将真实业务数据库配置用于该测试。
