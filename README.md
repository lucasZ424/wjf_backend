# HTSG V1 基础后端

<!-- current-contract: 2026-10-08 -->
现行规定以[终版设计](dev-docs/HTSG_V1_终版设计_v1.0.md)为准；开发的拆分、顺序与验收见[开发实施方案](dev-docs/HTSG_V1_开发实施方案_v1.0.md)，执行事实见[开发执行记录](dev-docs/HTSG_V1_开发执行记录_v0.1.md)。
<!-- /current-contract -->

有效设计与开发记录见[开发文档索引](dev-docs/README.md)，现行规定以[终版设计](dev-docs/HTSG_V1_终版设计_v1.0.md)为准。本期交付的验证数据见[满量测量与容量报告](dev-docs/HTSG_V1_满量测量与容量报告_v0.1.md)，交付清单、订阅参数表与尚未在现场验证的项目见[交付记录](dev-docs/HTSG_V1_交付记录_v0.1.md)。

## 是什么

**状态更新（2026-10-25）：** 初版开发完成（本机模拟范围，用户2026-10-23定）。七页已齐，通信·日志页已验收；整套连续运行已通过（候选`ed0dafd`，858/858，996.5秒，无挂住），此前“连续运行随机挂住”的取消丢失已在整改轮R01修复。最终Capstone H在执行记录里没有单独写明关闭的结论。一年容量按终版口径仍不通过；真实Acheron设备、真实OPC UA网关、独立卷、现场长稳仍未验，不宣称现场投运。2026-10-23回写各文档状态陈述时漏了本段，本次补上，见[开发执行记录](dev-docs/HTSG_V1_开发执行记录_v0.1.md)第1节末条。下一段是2026-10-13的状态说明，保留为历史。

**页面纠偏（2026-10-13，按用户最新指示）：** 目标仍为系统总览、水冷壁应变场、热应力分析、历史查询、系统自检、通信·日志、系统管理·权限七页。此前已移除数字孪生页及其专属接口；2026-10-14 起通信·日志页已实现并验收（执行记录 D44a–D44f），现为七页。本轮按用户授权完成通信页审核处置：采纳审核建议并补充语义限定，回写终版并拟定实施方案；具体选择由助手在授权范围内作出，见[审核处置](dev-docs/HTSG_V1_通信日志页设计审核_v0.1.md#review-disposition)、[终版第15节](dev-docs/HTSG_V1_终版设计_v1.0.md#15-前端页面)及[实施方案状态说明](dev-docs/HTSG_V1_开发实施方案_v1.0.md#page-contract-correction)。当日（10-13）只改文档、未实现通信页、未运行测试。既有错误页面集合的通过记录保留为历史事实；整套**连续**运行仍没有通过记录（分段运行 631 项全过，连续运行会随机挂住，见执行记录 H0、D44f 条目），最终 Capstone H 仍未关闭。

Windows 单机监测后端和科研前端（七页）。后端主动读取一路 Modbus TCP（8 路 Bridge）和一路 OPC UA 订阅（200 个监控项），在 417 个可查询量（208 路输入、209 路派生量）上运行；浏览器不产生业务数值。热应力是温差—应力的研究代理量，不是实测应力。

```
Modbus 轮询 ─┐                                        ┌─ 实时发布（100 ms 合并、整组、版本快照、1 秒心跳）→ 浏览器（七页）
             ├→ 有序入口 → 单一状态所有者 ──────────┤
OPC UA 订阅 ─┘   (批次/代次/断连事件同序)  (有效性、批末派生、       └─ 有序提交队列 → 归档路由 → PostgreSQL（正常库／移动库／应急库）
                                            故障、段、水位)                          ↑ 历史查询按稳定 ID 合并三库
```

- 所有者只处理完整批次，不读时钟、不做 I/O；输入、有效性和这一批算出的结果成组提交、成组发布，页面与数据库不会看到“新输入配旧结果”。
- 实时状态不等数据库：归档不可用时界面照常更新，缺口被记录、横幅提示，恢复后补发基线并换新有效段。
- 历史只保存实收值与变化；静止值不按秒重复写入，查询窗口起点处仍有效的值以“保持值”标出并保留原观测时间。

## 开发环境与数据库

Python 3.13，项目 `.venv`。依赖直接版本见 `pyproject.toml`，完整锁定见 `requirements.lock`。PostgreSQL 18.6 Windows 二进制与数据均在 D 盘：

| 用途 | 位置 / 端口 |
| --- | --- |
| PostgreSQL 二进制 | `D:\HTSG-dev\pgsql` |
| 主开发实例 | `D:\HTSG-dev\primary`，`127.0.0.1:55432` |
| 紧急开发实例 | `D:\HTSG-dev\emergency`，`127.0.0.1:55433` |
| 离线 wheel 归档 | `D:\HTSG-dev\wheelhouse` |

两个开发实例都在 D 盘，只验证双实例逻辑，不代表生产跨盘故障验证完成。项目 `.local/database.json` 保存开发连接信息，`.local/accounts.json` 保存认证哈希快照，初始凭证在 `.local/initial-credentials.json`；这些被 Git 忽略，不要分享或提交。测试和度量脚本只创建并删除自己的 `htsg_test_`／`htsg_perf_` 等临时库，不碰已有开发库。

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-build-isolation --no-deps -e .
.\.venv\Scripts\python.exe scripts\bootstrap_db.py install    # 首次
.\.venv\Scripts\python.exe scripts\bootstrap_db.py start      # 开发实例默认不启动；不启动则数据库用例超时；只建空库 htsg
.\.venv\Scripts\python.exe scripts\bootstrap_db.py build      # 把两实例的 htsg 建到当前归档代（建表）；旧的帧式库会被拒绝，须删库重建
.\.venv\Scripts\python.exe -m htsg.cli init-accounts
.\scripts\run.ps1                                              # 默认 config/development.json，127.0.0.1:8000
```

`scripts\run.ps1` 可带 `-Config`、`-Port`、`-Database`、`-Accounts`。核心任务意外退出时进程以失败码结束，由任务计划重启。`stop` 命令（`bootstrap_db.py stop`）只停这两个开发实例。

## 配置

| 文件 | 用途 |
| --- | --- |
| `config/research-sim.json` | 满量研究演示入口：8 路 Bridge＋200 个 OPC UA 点＋209 路派生量，配套独立的现场模拟器，见[模拟现场使用说明](dev-docs/HTSG_V1_模拟现场使用说明_v0.1.md)与[通道分配](dev-docs/模拟通道分配.md) |
| `config/development.json` | 最小开发配置：一个 Modbus 源（127.0.0.1:15020）、两个演示点；进程内模拟源已取消 |
| `config/production.template.json` | 空点表、禁用的真实源；填入现场资料后用 `htsg check --config ...` 校验 |

配置是严格校验的：旧机制的字段（`stale_after`、`aggregation`、`max_age`、`derivative`、`profiles`、捕获窗口类 `limits`、`sources[].timeout`、`simulator` 源）已删除且不再被接受。队列容量在 `limits.ingress`（入口）、`limits.commit_queue`（提交，默认 400 组）、`limits.ws_queue`（每客户端发送，默认 64）。时限与订阅参数在 `timing`，其含义与取值依据见终版设计第 11 节。极值与普通阈值见[默认极值阈值](dev-docs/默认极值阈值.md)、[默认越限阈值与回差](dev-docs/默认越限阈值与回差.md)，热应力模型见[热应力估算模型](dev-docs/热应力估算模型.md)。配置修改后重启生效，系统不提供在线改通道、阈值或配置的入口。

开发简化登录：`dev_admin_login=true` 时可直接输入 `admin:admin`（HTTP 用 `Authorization: Bearer admin:admin`，WebSocket 用同一凭据），只对应现有且有效的 admin 账户；生产模式拒绝启用。

## 接口

HTTP 传 `Authorization: Bearer <username:token>`，WebSocket 连接后 5 秒内发送 `{"credential":"username:token"}`（凭据不放 URL）。离线接口定义见 [docs/openapi.json](docs/openapi.json)（`scripts/export_openapi.py` 生成）。

| 接口 | 用途 |
| --- | --- |
| `GET /live` | 存活检查，无敏感信息 |
| `GET /`、`/static/*` | 科研前端（七页） |
| `GET /api/me`、`/api/ui-config` | 当前身份；点表、派生量、模型、界面与时限配置 |
| `GET /api/health` | 数据源与通信、每源重新同步次数与连续建连失败次数、归档路由与队列计数、实际协商的订阅参数、运行时状态；另含本次运行的通信日志与通信输入读数整表（通信·日志页使用，只读内存，不查数据库、不向设备发请求） |
| `GET /api/channels` | 可查询的 417 个量 |
| `GET /api/replay` | 窗口内的全部实收与变化记录（含批次号、批内序号、来源、缺口、窗口起点保持值），跨库合并，单库不可读则标注部分可用 |
| `GET /api/replay/basis/{result_id}` | 一个结果的输入依据 |
| `GET /api/faults` | 已归档的故障记录，按确认时间倒序分页 |
| `GET/POST /api/admin/accounts`、`DELETE /api/admin/accounts/{username}`、`GET /api/admin/audit` | 账户与管理操作记录（仅管理员；变更先写主库，结果未确认时返回 503） |
| `WS /api/stream` | 先发带发布序号的完整快照，再按序发布整组；慢客户端立即断开，重连取新快照 |

这是全部的写入口：只有账户的创建与停用。只读用户看不到管理页的数据，对 `/api/admin/*` 得 403。

## 存储与路由

三个库（正常库、可选的移动库、应急库）都接收完整提交组，不拆组、不丢派生结果。主数据盘达到 95% 后转向移动盘（已启用且绑定校验通过时），移动盘选择跨重启保持、不自动切回；移动盘未启用、不可用或达到 95% 时转应急库；正常目标降到 70% 且写探测成功后退出应急。开发各数据卷保留至少 1 GiB 余量，单批预算 8 MiB，开发写入预算 10 GiB（仅开发阶段），自动清理关闭、满一年不自动删除。

一年容量按满量实测日增长乘 365，另计索引、WAL 与维护峰值并列出磁盘余量，不足不得通过一年留存验收；实测见[满量测量与容量报告](dev-docs/HTSG_V1_满量测量与容量报告_v0.1.md)。运行时 `htsg capacity` 只给情景估算，不是验收。

## 运行管理

`scripts\register_task.ps1` 注册 Windows 任务计划（默认登录时启动；`-StartupCredential` 则开机启动，失败一分钟后重启，禁止并行实例），本开发机未注册，避免自动后台采集。现场必须安排 PostgreSQL 自动启动、服务账号目录权限与开机验证。

**停止与崩溃：** Ctrl+C／Ctrl+Break 是正常停止，进程先停采集、处理完已收批次、登记运行结束并写完提交队列（结束原因 `NORMAL`）。任务计划的“结束任务”或强行终止是硬杀，没有这一步：下次启动时，未结束的运行按各段已持久化的水位收尾（`ABNORMAL_EXIT`；没有水位的段取段起点），不猜测崩溃时刻；收尾只读小表，耗时不随崩溃运行的大小增长。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check htsg tests scripts
node --check htsg/static/app.js
git diff --check
.\.venv\Scripts\python.exe scripts\smoke.py                       # 交付启动路径：启动、停止、重启（临时库）
.\.venv\Scripts\python.exe scripts\measure.py --steady 180 --outage 25 --browser --out docs/evidence/d46
.\.venv\Scripts\python.exe scripts\verify_resilience.py all --out docs/evidence/d47
```

测试每个用例新建 `htsg_test_<hex>` 库并在结束时强制删除，浏览器用例用 Playwright 驱动本机 Chrome（`HTSG_EVIDENCE_DIR=docs/evidence/capstone-g` 时保存截图与浏览器日志）。协议用例使用本机临时端口和真实的 Modbus／OPC UA 服务器，不连接实际设备。结果与未验收项记在[开发执行记录](dev-docs/HTSG_V1_开发执行记录_v0.1.md)。

离线环境先安装 Python 3.13 x64 并建立 `.venv`，再 `pip install --no-index --find-links D:\HTSG-dev\wheelhouse -r requirements.lock`，最后安装本项目 wheel（`python -m build --wheel --no-isolation --outdir D:\HTSG-dev\wheelhouse`）。

## 尚未验证

真实的 Acheron 设备与点表、真实 OPC UA 网关实际修订的订阅参数（尤其测点队列被修订为 1、保活晚于协商值（未适配的asyncua服务器晚两个发布周期）的服务器）、独立卷与真实磁盘、一年容量与现场变化速率、现场启动与长稳，都没有在本环境验证；模拟环境的结果不能代替它们，清单见[交付记录](dev-docs/HTSG_V1_交付记录_v0.1.md)。
