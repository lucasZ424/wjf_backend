# HTSG V1 基础后端

<!-- current-contract: 2026-10-08 -->
现行规定以[终版设计](HTSG_V1_终版设计_v1.0.md)为准。
<!-- /current-contract -->

完整范围见[现行软件边界](HTSG_V1_软件边界与已知设计方向_v0.2.md)。

## 0.2.2 现有实现与使用信息（未按新规定整改）

以下保留原实现、模型、审计或验证事实。正文中的“当前”“已确认”“待定”及规则要求均属于原记录日期和旧基线；冲突机制已由上方规定替代，不构成新机制的开发要求或验收结果。现有启动命令仍启动旧版程序。

当前修订为 0.2.2，开发预算调整为 10 GiB，暂停自动清理，增加 95% 移动盘接续归档。当前移动盘尚未接入，真实切换保持禁用，见 [存储策略调整](docs/移动盘接续与清理暂停.md)。0.2.1 追忆停写与刷新修复记录继续保留。

本仓库实现 Windows 单机监测后端及科研验证前端。设计依据为现行四份设计，审核落地规则集中在《数据流详细设计》§23。0.2.0 新增独立的 Modbus TCP / OPC UA 模拟现场、六页前端及带假设说明的热应力估算；后端轮询 Modbus，并通过 OPC UA 订阅接收数据，浏览器不生成业务数值。现场实测仍需替换配置并重新验证。

科研演示的启动、配置替换及验证方法见 [模拟现场使用说明](docs/模拟现场使用说明.md)，模型依据见 [热应力估算模型](docs/热应力估算模型.md)。原 `config/development.json` 保持基础验证用途；本轮入口为 `config/research-sim.json`。

## 已实现

- 配置校验与固定部署参数；模拟源、Modbus 解码与 OPC UA 订阅通过统一入口。
- 逐有效源观测判异常，形成 1 Hz 单点透传帧；不再进行时间聚合、插值或旧值保持，缺秒和同秒多样本显式标记不可用/异常，全部原始输入仍归档。
- 单一状态所有者、独立有界写入与计算任务；计算超时输出不可用，不修改已发布帧。
- Profile 事件分组、Pre/连续恢复/Post、恢复证据中断、历史迟到、重启中断和时间段隔离。
- PostgreSQL 窄表历史、事件源样本及关联、重复写入幂等、已绑定实例事件版本汇合、结果清理状态与缺口查询。
- 原始事件样本至少 365 天保护；普通历史 30 天种子；停写/恢复不因时间戳跳进而清空旧种子。
- 自动清理暂时停用；95% 移动盘接续、最终应急路由与余量保护；开发预算 10 GiB。
- REST、完整快照 WebSocket、慢客户端重同步、admin/viewer 凭证权限与账户停用。
- 配置检查、容量情景估算、日志轮转、故障重连、进程监督及启动任务注册脚本。

## 开发环境与数据库

Python 3.13，项目 `.venv`。依赖直接版本见 `pyproject.toml`，完整锁定见 `requirements.lock`。PostgreSQL 18.6 Windows 二进制与数据均在 D 盘：

| 用途 | 位置 / 端口 |
| --- | --- |
| PostgreSQL 二进制 | `D:\HTSG-dev\pgsql` |
| 主开发实例 | `D:\HTSG-dev\primary`，`127.0.0.1:55432` |
| 紧急开发实例 | `D:\HTSG-dev\emergency`，`127.0.0.1:55433` |
| 离线 wheel 归档 | `D:\HTSG-dev\wheelhouse` |

C 盘没有部署数据库。两个开发实例均位于 D 盘，只验证双实例逻辑，不代表生产跨盘故障验证完成。实例使用隔离的 32 MB shared_buffers、受限连接和临时文件预算；`max_wal_size` 是检查点目标，不是 WAL 的物理硬上限。

项目 `.local/database.json` 保存开发连接信息，`.local/accounts.json` 保存认证哈希快照，初始凭证位于 `.local/initial-credentials.json`。这些均被 Git 忽略，不应分享或提交。运行时账户变更先写主库并更新本地认证快照；主库故障时已有本地快照用于验证，管理写入失败会明确返回错误。当前自助引导账号用于隔离开发；生产需配置独立数据库服务账号与目录访问权限。

首次准备及启动（项目根目录 PowerShell）：

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-build-isolation --no-deps -e .
.\.venv\Scripts\python.exe scripts\bootstrap_db.py install
.\.venv\Scripts\python.exe scripts\bootstrap_db.py start
.\.venv\Scripts\python.exe -m htsg.cli init-accounts
.\scripts\run.ps1
```

本机已经具备 `.venv` 和 D 盘开发实例；无需重复下载。`bootstrap_db.py install` 不覆盖非空未知数据目录，也不会重置已有凭证。`stop` 命令仅停止这两个开发实例。后端默认绑定 `127.0.0.1:8000`，交互式 API 文档在 `/docs`，离线接口定义见 [docs/openapi.json](docs/openapi.json)。

## 配置与接入

本期按轻量边界运行，不因输入来源组合不同拒绝注册或订阅，也不提供跨源正确性保障及扩展预留。默认运行 8 路 Bridge＋200 个 OPC UA 点，后端另有 209 路派生量，共 417 个可查询量，见[模拟通道分配](docs/模拟通道分配.md)。所有量均配置[默认极值阈值](docs/默认极值阈值.md)。极端值保留原数值并显示 EXTREME；“测点01极端温度30秒”场景用于验证，普通升温场景不会再因旧 510 °C 阈值被当作极值。

`config/development.json` 的点位和单位均以 `demo` 标识，模拟源明确启用，间隔 1 秒。默认配置不再设置捕获 Profile；故障按各源通道自身阈值持续满 5 秒确认。旧配置中的 Profile 只保留解析兼容，不参与新故障判断。

采集源和独立模拟器的 interval 固定为 1；源测点 aggregation 固定为 Direct，并移除旧的源点 max_age 保持参数。旧部署配置须同步调整后再启动，不接受其他频率及旧聚合模式。后端帧、状态维护和页面自动刷新均采用 1 秒周期；固定历史窗口由用户操作刷新。连接重试/保活及数据库写入仍按各自协议和有界队列处理。

`config/production.template.json` 保留空点表和禁用的真实源。填入现场资料后运行 `htsg check --config ...`。启用 Modbus 必须有地址、寄存器及格式；启用 OPC UA 必须有端点及 NodeId，账号从指定环境变量读取，安全模式和证书参数使用 `security` 配置。Modbus 采用有界间隔轮询；OPC UA 使用 monitored items 订阅并按 1 Hz 向统一入口提交订阅缓存的最近值。相同数值不代表厂家产生了新的独立采样；源端时间不具备共同参考条件时使用明确的接收时间域，不能把它解释成现场同时采样。

前端若在其他本机端口运行，在 `allowed_origins` 明确列出来源，例如 `http://127.0.0.1:5173`；默认不开放跨域来源。此配置重启生效。系统不提供来源映射、单位或规则热切换。

## API 使用

HTTP 传 `Authorization: Bearer <username:token>`。Viewer 可读，Admin 可管理账户；开发观测注入入口仅对开发模式的 Admin 开放。

当前开发调试可直接输入 `admin:admin` 登录，HTTP 使用 `Authorization: Bearer admin:admin`，WebSocket 使用同一凭据。`config/development.json` 和 `config/research-sim.json` 已启用 `dev_admin_login=true`，修改后需重启后端。该凭据只对应现有且有效的 admin 管理员；原令牌仍可使用。结束调试时设为 false 并重启，生产模式拒绝启用此开关。

| 接口 | 用途 |
| --- | --- |
| `GET /live` | 无敏感信息的存活检查，核心任务故障返回失败 |
| `GET /api/current` | 最新展示帧及未复归通道 active_faults，不依赖逐帧查库 |
| `GET /api/health` | 源质量、队列、归档、容量和未配置能力 |
| `GET /api/points` | 测点/派生量说明 |
| `GET /api/history?start=...&end=...` | UTC Unix 秒区间历史；可按 point/event_id 筛选 |
| `GET /api/faults?point=...` | 已归档故障参考点，按确认时间倒序，可分页 |
| `GET /api/replay?center=...&seconds=...&point=...` | UTC Unix 秒中心、窗口秒数及可选通道，全量原始观测与已存派生结果，区间为左闭右开；不按事件关联限制 |
| `GET /api/events`、`GET /api/events/{id}` | 兼容读取旧事件及阶段/覆盖信息 |
| `GET /api/events/{id}/samples` | 原始事件样本 |
| `GET /api/events/{id}/triggers` | 触发与迟到异常事实 |
| `GET /api/events/{id}/products` | 结果可用性/已清理标记 |
| `GET /api/gaps`、事件下的 `/gaps` | 实际缺口 |
| `GET/POST /api/admin/accounts`、`DELETE /api/admin/accounts/{name}` | 管理账户，不能停用最后一个管理员 |
| `POST /api/development/observations` | 受控开发注入；生产不可用 |

分页默认 100、单次最多 1000。完整遍历使用响应中的 `next_cursor`，下页保持相同筛选条件并传 `cursor`，不设累计 2000 条上限。旧 `offset` 只保留浅分页兼容，不能与游标混用。单库查询、跨库合并和游标使用相同的时间/稳定 ID 排序；事件按触发时间倒序。游标不是冻结数据库快照，遍历期间新增的历史迟到记录需重新查询；单库不可用时 `pagination_incomplete=true` 且不生成前进游标，恢复后从原位置重试。查询包含实例来源和部分可用状态。已关闭事件的 `archive_status=SUBMITTED` 表示已提交归档序列，不是无缺口认证；同时检查 `coverage`、`has_gaps` 和产品状态。本期不回填停写历史，不提供重建接口。

WebSocket `/api/stream` 连接后 5 秒内发送 `{"credential":"username:token"}`；凭证不放入 URL。先收到 `snapshot`，再收到包含运行段和序号的 `frame` / `fault` / `gap`。fault 通知只在数据库提交成功后发布。普通帧可以合并，通知队列溢出发送 `resync_required` 并断开；重新连接取得快照并按已保存记录补读。归档拒写通过健康状态和缺口报告，不声称记录已保存。

## 清理与容量

正常期、越限期和复归后均持续归档所有收到的原始观测，包含无效质量，故障确认不决定归档范围。新原始记录标识 archive_scope=continuous，不使用旧事件样本一年到期规则删除。当前暂停业务数据自动删除，长期保存期限和容量仍待确认；历史重现只返回实际保存的数据，不补造停机期间的数据。

当前 `cleanup_enabled=false`，暂停自动到期删除及 80%/70% 梯次清理，保留健康统计、表增长和 autovacuum 监测。主盘达到 95% 后优先由移动盘接续完整归档；移动盘选择跨重启保持，不自动切回主盘。最终应急实例接收连续原始观测及故障点，帧与派生结果归档暂停，缺失结果不承诺回填。移动盘尚未接入，实际切换关闭；D 盘隔离测试只验证逻辑。恢复清理需先完成实际增长测量和容量评估。

开发累计业务写入预算为 10 GiB（10,737,418,240 字节），并检查已绑定业务库的用户表与索引总量；单批增长预算 8 MiB，D 盘至少预留 1 GiB。达到预算拒绝新归档并报告。此为应用保护，不是对 PostgreSQL WAL 或其他进程占盘的物理配额。测试注入水位并使用少量记录，不填满磁盘。外部库离线时的开发预算限制见存储策略调整记录。

容量估算命令 `htsg capacity` 输出每样本不同字节开销下连续捕获一年的情景值；不是现场容量验收。主机 CPU/内存、协议实际频率、阈值及真实图谱依赖确定后还需校核。

## 运行管理

`scripts/run.ps1` 启动一个后端进程。核心任务意外退出会让 CLI 返回失败；源连接采用退避重试，数据库操作在线程内执行，不堵塞实时循环。日志最多 3 个备份、每份 2 MiB。

`scripts/register_task.ps1` 提供 Windows 原生任务计划程序托管：默认登录时启动；传入 `-StartupCredential` 则在系统启动时运行，失败间隔一分钟重启，禁止任务并行实例。凭证通过 PowerShell 的 PSCredential 传入，不写入代码。此脚本尚未在本开发机注册，避免自动后台采集。现场必须安排 PostgreSQL 自动启动、服务账号目录权限及系统启动验证；单纯登录任务不能作为无人值守开机验收通过的依据。

开发数据库按需运行，可用以下命令停止：

```powershell
.\.venv\Scripts\python.exe scripts\bootstrap_db.py stop
```

## 验证与离线构建

```powershell
.\.venv\Scripts\python.exe -m pytest -q --basetemp=.local/pytest-tmp
.\.venv\Scripts\python.exe -m ruff check htsg scripts tests
.\.venv\Scripts\python.exe scripts\smoke.py
.\.venv\Scripts\python.exe -m build --wheel --no-isolation --outdir D:\HTSG-dev\wheelhouse
```

集成测试会创建随机 `htsg_test_...` 数据库，结束后只删除这些测试库；使用 `.local/database.json` 的本机开发管理连接。协议测试使用临时本机端口，不连接实际设备。单元测试、协议集成、PostgreSQL 集成与短时 HTTP 烟雾验证结果见 [docs/验证记录.md](docs/验证记录.md)。

离线环境先安装 Python 3.13 x64 并建立 `.venv`，再运行 `pip install --no-index --find-links D:\HTSG-dev\wheelhouse -r requirements.lock`，最后安装同目录下本项目 wheel。原始 PostgreSQL 下载包及摘要也保留在 D 盘。

现场接口、最终测点与判据、正式 Profile 依赖仍待替换。科研热应力代理估算已由本项目实现，现场模型对照与参数依据可继续迭代。I10-F01 系统盘保留线及真实跨盘能力、现场启动/长稳/吞吐容量验收未完成；开发测试不替代这些结论。

## 0.1.1 审核整改实现

Pre 使用不可变样本引用、封存帧及一次冻结的事件版本形成独立作业，所有在途快照按保守估算合计最多 64 MiB；预算不足明确标记 PARTIAL 和缺口。写任务轮流处理实时事件、Pre 和普通历史，默认每批至多 128 条；每工作线程复用连接，批内事务提交，失败重连、稳定 ID 重试。它不保证内存队列在断电后无损。持续异常达到时限与源失效超时使用不同关闭原因；一般写入恢复不清除持续异常的抑制。

未完成帧和 Pre 作业全部交接后才释放关闭事件的归档责任。超前源时间不推进当前状态前沿、不证明恢复；已知源丢失关联对应事件。紧急恢复须完成原正常目标的写探测提交，探测后正式写失败仍退回紧急模式；已选移动盘后不自动回主盘。

结构版本 2 在启动事务内为已有源历史建立计数/顺序，不复制种子记录；已验证旧记录迁移、重复初始化、重复写入及事务回滚。大量历史库升级仍需部署维护窗口和实际耗时校核，默认数据库语句超时继续生效，失败不报告升级成功。整改详情见代码审核文件第 12 节及验证记录。
