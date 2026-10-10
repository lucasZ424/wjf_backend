# HTSG V1 开发执行记录

版本：v0.1｜起始：2026-10-10｜依据：[开发实施方案](HTSG_V1_开发实施方案_v1.0.md)（设计基线哈希见方案头）、[终版设计](HTSG_V1_终版设计_v1.0.md)。本文只记录执行事实；设计和验收口径以上述两份为准。

## 0. 环境与约定

- 分支：`dev/htsg-v1-impl`（自 `main` 的 `40750c9` 切出），只做本地提交，不推送。
- 平台：Windows 11；Python 3.13.4（`.venv`）；asyncua 2.0.1、pymodbus 3.15.0、psycopg 3.3.5、pytest 9.1.1、ruff 0.16.6；Node 24.16.0。
- 数据库：项目既有两套隔离开发集群（`D:\HTSG-dev\primary`、`emergency`，127.0.0.1:55432/55433），用 `scripts/bootstrap_db.py start` 启动；测试每个用例新建 `htsg_test_<32位hex>` 临时库，结束强制删除；不触碰旧开发库/研究库。凭据只在 `.local/database.json`，本文与测试输出不含凭据。
- **新旧并存（D45 前）**：新链路写成新模块（`records`、`archive`、`archive_schema.sql`、`derive`、`owner` 等），旧帧链路和旧测试保持可跑，作为回归基线；D45 切换并删除旧链路。这是为保留回归基线的过渡，不是长期双架构。
- 命令：
  ```powershell
  .venv/Scripts/python.exe -m pytest -q
  .venv/Scripts/python.exe -m ruff check htsg tests
  node --check htsg/static/app.js
  git diff --check
  ```

## 1. 记录

### D01 基线与用例映射

- 代码版本：`40750c9`（工作区干净）。基线全套：**202 通过 / 0 失败 / 0 跳过，131.61 秒**。
- 差异与设计摘要：设计基线哈希 `8070026e…436fb` 已按 UTF-8、LF 规范化重算，与方案头一致。
- 环境问题：首次基线因开发数据库集群未启动，fixture 阶段 `ConnectionTimeout`（130 秒后中止）；按项目脚本启动集群后重跑得到上述结果。
- 测试库隔离：见第 0 节。

旧测试处置（沿用＝保留并随接口改接；替换＝由所列任务的新用例取代；删除＝随终版取消的功能移除，括号内为依据）：

| 旧文件（用例数） | 处置 |
| --- | --- |
| test_account_persistence(7)、test_dev_login(2)、test_research_api(2)、test_research_lab(6)、test_storage_binding(5)、test_extremes(5) | 沿用；存储夹具在 D45 改接 `ArchiveStore`。extremes 的派生部分在 D09/D10 补批末语义 |
| test_calculation(6) | `full_bridge…`、`thermal_peak…` 沿用为 D10 手算依据；`chained_calculation_cannot_hide_held_input_age`、`derivative_rejects…` 删除（held-input 年龄与 derivative 取消，§6、§7）；`strain_change_*`(2) 由 D11 替换 |
| test_engine(13)、test_pre_coverage(2)、test_audit_lifecycle(2) | 删除/替换：整秒窗口、Pre 环、旧库升级随帧链路取消（§1、§16.1）；有价值的业务断言由 D08–D11、D27–D31 重写 |
| test_audit_regressions(11) | `next_bucket…`、`out_of_order_frame…`、`cleanup_drains…` 删除（帧/清理取消）；其余按 D17、D19、D21、D26、D34 替换 |
| test_boundaries(4) | `ingress_is_bounded…`→D08；`actual_emergency_writes_only_source…`→D20（C3 反转，应急同样接收派生）；其余沿用 |
| test_api(5) | `slow_subscriber_merges_frames…`→D36/D37；`calculation_timeout_publishes…` 删除（异步帧计算取消，§6）；`websocket_authentication_snapshot_and_fault`→D36/D38；其余沿用 |
| test_capacity_recall(3)、test_storage(14) | 事件/Pre/seed/recomputable 相关删除（固定捕获窗口取消，§8.1）；路由、合并、应急往返用例由 D20、D26、D34 替换 |
| test_external_*(14)、test_storage_config(7) | 路由/移动库语义沿用并在 D26、D34 重写验收 |
| test_fault_points(5)、test_fault_validation(8)、test_research_flow(2)、test_replay(3)、test_replay_ui(1) | 由 D19/D25/D27–D31/D35/D39–D40 与 capstone C、E、G 替换 |
| test_config_adapters(6) | `profile_cannot_omit_derived_input`、capture 时长部分删除（profiles 取消，§18.1）；其余沿用 |
| test_frequency(4) | 沿用；D07 在其上增加节拍编号 |
| test_protocols(3)、test_simulation(9)、test_scenario_archive_guard(1)、test_thermal(9) | 协议用例由 D12–D16 重写（保留本地真实服务器思路）；模拟器自身用例沿用（场景另行规划，freeze 等暂定记录不改）；thermal 的 3 个 frame 用例在 D10 改接 |

### D02 记录与身份类型

`任务ID｜工作区｜修改文件｜命令｜结果｜证据｜未覆盖`
D02｜dev 分支工作区｜新增 `htsg/records.py`、`tests/test_records.py`｜`pytest tests/test_records.py`｜8 通过 / 0 失败 / 0 跳过｜用例见文件（同点同时间不碰撞、确定性 ID、原观测时间与重申时间分离、32 位状态码）｜无

### D03 协议时限与周期配置

D03｜同上｜`htsg/config.py`（新增 `Timing`）、`tests/test_timing_config.py`｜`pytest tests/test_timing_config.py`｜16 通过｜第 11 节表项逐项落字段；订阅约束可对服务端修订后的值重核（`check_negotiated`）｜**实现取值（非设计规定）**：`ua_silence_margin=0.5` 秒——终版要求"静默间隔加裕量小于有效期"但未给裕量数值，已作为可配置字段

### D04 普通阈值与点表契约

D04｜同上｜`htsg/config.py`（`hysteresis_*`、派生 `low/high`、`fault_rule`）、`config/research-sim.json`（+820 行）、`tests/test_fault_thresholds.py`｜`pytest tests/test_fault_thresholds.py tests/test_extremes.py tests/test_config_adapters.py tests/test_storage_config.py`｜34 通过｜期望值**从规范文档表格解析**得出（304 路阈值、417 路极值），不与配置同源｜无

补充发现（实现中暴露，已修）：① 回差使复归线落到极值带之外会导致确认后永不复归，配置校验补上拒绝；② `config_id` 原对 JSONB 键序敏感，快照回读后不能复现，改为键排序规范化哈希。②改变了 `config_id` 取值（旧库中的旧 ID 不受影响）。

### D05/D06 新库表与初始化

D05/D06｜同上｜新增 `htsg/archive_schema.sql`、`htsg/archive.py`、`tests/test_archive_schema.py`，`tests/conftest.py`（`archive_db_factory`），`pyproject.toml`（包含新 schema）｜`pytest tests/test_archive_schema.py`｜12 通过｜真实 PostgreSQL；旧库被 `LegacyDatabaseError` 拒绝且表结构逐列不变；重复初始化幂等；无 `frame` 表｜`validate_external_binding`、路由状态读写等随 D26 迁入

### Capstone A 契约可执行

A｜同上｜`tests/test_capstone_a.py`｜`pytest tests/test_capstone_a.py`＋全套｜5 通过；**全套 258 通过 / 0 失败 / 0 跳过，137.37 秒**｜满量配置装载后回读快照重算 `config_id` 一致；同点同时间两条与一条重申可区分；错误阈值/未知配置版本明确失败；旧库保持原样｜不宣称采集已运行

### D07 计划节拍编号

D07｜同上｜`htsg/timing.py`、`tests/test_tick_numbers.py`｜`pytest tests/test_tick_numbers.py tests/test_frequency.py`｜13 通过（含旧 frequency 4 项）｜跳两拍后编号 2→5；无突发补发；调度只读单调钟｜无

### D08–D11 批次接纳、有效性、批末算子、change 基线

D08–D11｜dev 分支 `d40d8dd`｜新增 `htsg/owner.py`、`htsg/derive.py`、`htsg/ingress.py`、`tests/owner_support.py`、`tests/test_owner_batches.py`｜`pytest tests/test_owner_batches.py`｜23 通过 / 0 失败 / 0 跳过｜见用例：跨整秒不拆组、旧代次不覆盖、同时间按批内序号、入口满按序上报丢失；Bad/Uncertain 载荷与完整状态码保留、无值不补零、等于极值边界不触发（800.0/-50.0）、严格越界才触发；手算 118 με 与 57.375 MPa；缺输入不借上一轮；峰值取有符号最大且要求全部有效；所有者模块经 AST 检查无 asyncio/psycopg/time 导入、无 await；失败/跳拍/重连/丢失轮后首轮只建基线｜"静止点不按末次变化过期"依赖 tick（D17），在那里补验

实现取值（非设计明文，已在代码注释/此处标明）：
- 从未产生过有效值的派生量，首次计算为 NA 时**不写记录**（"未知保持未知"）；有值之后变 NA 才写。依据：终版 §5 "没有水位时只保留实存点"、§4 "缺失测点继续未知"。若验收希望记录首次 NA 的原因，需改此策略。
- Modbus 每轮的原始观测逐轮存储（change 的"连续两轮"依据需要），派生结果仅在值/状态/原因变化时存储。

### Capstone B 无帧计算闭环

B｜`d40d8dd`｜`tests/test_capstone_b.py`｜`pytest tests/test_capstone_b.py`｜3 通过 / 0 失败 / 0 跳过｜208 路输入（200 路 UA 单批初值突发）+209 路派生；跨整秒、两批相隔 5 ms、质量单独变化后恢复、合法过零、超时轮与跳拍轮；每个批次后用独立 oracle（仅依据所有者自己的原始观测重算）逐项核对；输出类型无 `window_start/window_end/stale_after`，把全部点 `stale_after` 改成 0.001 结果不变｜不评价真实协议与落库

### D12–D13 Modbus 读取计划、轮次与故障分类

D12–D13｜dev 分支工作区（待提交，见下一条提交）｜新增 `htsg/modbus.py`、`htsg/recovery.py`、`tests/protocol_support.py`、`tests/test_modbus_acquisition.py`｜`pytest tests/test_modbus_acquisition.py`（连跑三次）｜11 通过 / 0 失败 / 0 跳过，各 3.5 秒｜真实 pymodbus 本地服务器：8 路 Bridge 合并为单次 `(0,16)` 读取；多请求同 `poll_id`、响应各有 `response_id` 与自己的收到时间；字节/字序解码；不跨设备/寄存器类型/空洞/125 上限；客户端 `timeout=0.8, retries=0, reconnect_delay=0`（库不自行重连）；慢设备一次超时约 0.8 秒且只发 1 个请求；单次超时保连接、连续 3 轮超时重连；设备异常与极值不当作断连；服务器断开→立即 RESYNC→恢复后换新代次；10 秒告警从最后一次响应算起，不被重连尝试清零｜无

实测发现：pymodbus 3.15 超时抛 `ModbusIOException` 且连接保留；设备异常是 `ExceptionResponse(isError)`；服务端关闭后抛 `ConnectionException`；库默认会自动重连，必须传 `reconnect_delay=0`。

### D14–D16 OPC UA 客户端、订阅协商、通知连续性

D14–D16｜同上｜新增 `htsg/ua.py`、`tests/test_ua_client.py`（真实 asyncua 服务器）、`tests/test_ua_channel.py`（库边界注入）｜`pytest tests/test_ua_client.py tests/test_ua_channel.py`｜7 + 15 通过 / 0 失败 / 0 跳过｜`ManagedClient` 覆盖 `_connection_supervisor`/`_stale_watchdog_loop`：对照实验证明 stock 客户端 1 秒内探测 server_state ≥2 次，而受管客户端 0 次，Publish 与通道续期照常；暂停代理 2.5 秒库不 `recreate()`；服务器断开后应用经 `check_connection()` 发现，库不再回调 `connection_lost_callback`、不推 BadShutdown；订阅用显式参数（寿命 60、保活 2，不是库默认 10000），保存服务端返回的修订值，修订后重核约束；队列修订为 1 标"覆盖不可检测"但不拒绝；创建失败（节点不存在）后服务端订阅数为 0；空保活不消耗序号、重复去重、32 位回绕、缺号/Overflow/未知句柄/回调异常/订阅状态变化/入口满均触发 C1｜库内私有方法依赖：asyncua 2.0.1 已锁定于 pyproject

### D17–D18 来源有效性、告警、重建与同代次恢复

D17–D18｜同上｜`htsg/owner.py`（来源状态机 WAITING/VALID/EXPIRED/RESYNC，`tick()`、`source_down`、`lost`、`_recover`）、`htsg/ingress.py`（有序事件入口）、`tests/test_source_validity.py`｜`pytest tests/test_source_validity.py`｜17 通过 / 0 失败 / 0 跳过（假时钟，边界 2.9/3.0/5.9/6.0 秒逐点验证）｜3 秒只失效不重建；保活维持有效；明确断开立即失效；6 秒静默同时告警与重建；首次无响应以"启用时刻"为告警基准、数据保持未知；重建尝试不重置告警基准，只有有效响应才更新通信时间；显式禁用源不告警；退避 1,2,4,8,16,30,30、稳定 60 秒才复位、5 次后标记持续失步但仍每 30 秒自动恢复；3–6 秒恢复=新段+缓存重申+恢复批次+派生更新，**一个提交组、一个发布组**，不回填失效区间；旧代次迟到响应被丢弃；新订阅等待基准（3/6 秒）与源告警基准互不混淆｜无

### Capstone C 协议到状态闭环

C｜同上｜`tests/live_support.py`、`tests/test_capstone_c.py`｜`pytest tests/test_capstone_c.py`（连跑三次）+ 模块 C 全部用例｜4 通过 / 0 失败 / 0 跳过（约 33 秒）；模块 C 合计 54 通过（连跑两次各约 49 秒）｜真实 pymodbus + asyncua 同时运行，8 路 Bridge + 200 个 monitored item（**一个订阅**）；静止 4.5 秒内 OPC UA 观测数不增（不逐秒重报）；变化只产生 1 条观测；Modbus 慢响应→TIMEOUT→恢复且不换代次；暂停代理造成静默：3 秒失效、3–6 秒内恢复同代次（恢复组含 200 条重申）、6 秒以上触发 SILENCE 重建到第 2 代；服务器断开只触发 1 次重建（`resyncs==1`、历史 1 条）、服务端订阅数为 1（旧订阅未泄漏）、`auto_reconnect=False`｜**未覆盖**：真实 Acheron、真实 UA 网关；持久化为内存接收桩

**重要发现（需现场核验）：** asyncua 2.0.1 测试服务器在"保活计数 > MaxKeepAliveCount"时才发保活，实际静默间隔比协商值多一个发布周期（请求保活 2、发布 1 秒时实测约 3–4 秒），恰好落在 3 秒有效期上，导致空闲时每隔数秒就产生一次 3–6 秒"失效—恢复"。这与终版第 11 节"保活静默加裕量须小于有效期"的约束一致：**协商值满足约束不等于网关实际满足**。测试中对该服务器使用保活计数 1（实际静默约 2 秒）。真实 UA 网关的保活节奏必须在联调时实测，列入 D48 的"需现场验证"清单。

### D19–D20 整组写入与整组路由

D19–D20｜dev 分支工作区｜`htsg/archive.py`（`write_group`、`WriteOutcome`、段/水位/缺口/运行/故障摘要落表）、新增 `htsg/archive_router.py`、`tests/test_archive_write.py`、`tests/test_archive_router.py`｜`pytest tests/test_archive_write.py tests/test_archive_router.py`｜7 + 6 通过 / 0 失败 / 0 跳过｜真实 PostgreSQL：原始/派生/状态/故障同一事务；外键违反→FAILED 且无任何残留行；重复整组→ALREADY、重交同一故障 ID 不产生第二行；提交答复丢失→UNKNOWN，按稳定 ID 在**同一库**核实（已落库→COMMITTED，未落库→保持 UNKNOWN 并同目标重试，不写别的库）；会话参数 3/3/1 秒（SHOW 值核对、新会话重连后仍生效；`pg_sleep(8)` 在约 3 秒被取消；持锁方存在时 1 秒后 `LockNotAvailable`）；超预算整组 REFUSED、400 条的组一次原子写入；应急路由保留派生结果；一组只落一个库｜无

### D21 有序组队列与提交确认

D21｜同上｜新增 `htsg/commitq.py`、`tests/test_commit_queue.py`｜`pytest tests/test_commit_queue.py`｜6 通过｜单一 FIFO，队首失败时后面的水位组不会越过；满队列 `offer` 返回 False 且原内容不被覆盖；数据库阻塞期间 59 轮接纳每轮 <50 ms、全部实时发布、`on_committed` 零次调用，放行后按序确认；REFUSED 组被丢弃并报告而不堵死后续；UNKNOWN 重试同一组；真实库中确认回调触发时该组已可查｜无

### D22 运行/连接/归档段生命周期

D22｜同上｜`htsg/owner.py`（`start`/`shutdown`、`_clock_jump`、段滚动）、`archive.py`（`reconcile_unfinished_runs`）、`tests/test_run_segments.py`｜`pytest tests/test_run_segments.py`｜10 通过｜启动登记；正常停止在已接纳记录处理完后写 `RUN_END` 并关闭所有段；异常退出按**已落库证据**（水位/观测/状态的最大时间）截断，不推测崩溃时刻，重复调用幂等，当前运行不被关闭；墙钟−单调钟增量绝对值 >2 秒才开段（边界 2.0 不开，2.01 开，回拨 20 秒开，回拨 0.5 秒不开）；回拨后旧新两段按 (seg,batch_seq) 排序而不靠墙钟；旧库被 `REFUSED` 且不被读写｜无

### D23 10 秒水位与提交屏障

D23｜同上｜`owner.py`（`_watermarks`、`confirmed_watermark`、`history_status`）、`tests/test_watermark.py`｜`pytest tests/test_watermark.py`｜5 通过｜从未响应的源不产生水位；水位 = 最后一次**被证明的通信**时刻，不是 now，且静默后停止推进；队首数据组连续失败时其后的水位组不落库、落库的水位之前的所有组都已落库；存储停写 60 秒时只声明**已确认**水位，尾段不确定区间 >50 秒（不承诺 10 秒）；无水位时只呈现实存点｜无

### D24–D25 缺口、可靠基线与原子恢复

D24–D25｜同上｜`owner.py`（归档状态机 OK/LOST/RECOVERING、`_archive_lost`、`_try_archive_recovery`、`_segment_group`、`committed`/`refused`/`is_void`）、`commitq.py`（`is_void` 过滤）、`tests/test_archive_gap.py`、`tests/test_archive_recovery.py`｜`pytest tests/test_archive_gap.py tests/test_archive_recovery.py`｜6 + 6 通过｜队列满→LOST，缺口期内该源不再入队但实时发布不受影响；恢复单元=GAP+SEG_END(缺口起点)+SEG_START(ARCHIVE_GAP)+保留基线(原观测 ID/时间不变，另记重申时间)+首个水位，一个组；缺口内算出的派生值基线标 `basis_incomplete`、缺口前算出的不标；采集入口失步(RESYNC)时不拿旧缓存当基线，等新代次有数据后再截取；重申不计算 change、change 链在缺口处被切断；恢复期间排队有界(`pending_limit`)，再次溢出重新截取基线；恢复组被拒时回到 LOST、不宣告恢复；恢复确认后按序放行等待的组；缺口内同代次恢复把 GAP+基线+恢复批次放进同一单元；队首被拒组后面已入队的同源组被 `is_void` 作废(`voided`)，水位不会跨过缺口；真实库中恢复单元的观测/状态/段/缺口/水位同一 `group_id`、同一事务｜无

### D26 路由恢复、预算与维护节拍

D26｜同上｜`archive_router.py`（`route`、`maintain`、开发预算、`validate_external_binding`）、`archive.py`（`routing_mode`、`external_selected`、`metrics`）、`tests/test_storage_routing.py`｜`pytest tests/test_storage_routing.py`｜20 通过（约 14 秒）｜95% 切到移动库且旧数据不搬迁/不复制(两库行数核对)；选定后主库回落不切回；重启后恢复选择；移动库不可读→应急，回到移动库需 ≤70%+探测，永不回主库；无移动库时应急退出需 ≤70%+探测，探测失败保持应急；绑定失败(与主库同库)不写不探测；生产模式：量不再受 10 GiB 约束，仍执行 100 GiB 应急余量(REFUSED:EMERGENCY_RESERVE)、整组预算、卷校验(实际卷≠配置卷→ExternalBindingError)，`dev_admin_login` 在生产被拒；开发：预算 1024 字节→整组 REFUSED、卷余量<1 GiB→REFUSED、占用未知→REFUSED 且已知值被缓存；重统计 60 秒/轻状态 1 秒的边界(0/0.5/1/59.9/60/60.5/120)；`maintain` 不含任何删除，旧于一年的记录在 `cleanup_enabled` 真/假下都保留；`development_budget_bytes` 拒绝 0/-1/1023/超 10 GiB/None/2^63，接受 1024/5 GiB/10 GiB｜`validate_external_binding` 的"卷"校验在开发机上只能用同盘不同库验证，真实独立卷仍待现场

### Capstone D 持久化与恢复闭环

D｜同上｜`tests/crash_worker.py`、`tests/test_capstone_d.py`｜`pytest tests/test_capstone_d.py`｜4 通过 / 0 失败 / 0 跳过｜三个隔离库：正常/移动/应急均保存完整组(每个库内每个组的成员行数=组记录数)；切换前后记录 ID 核对旧观测未搬迁或复制、跨库直接 SQL 联合可见两段历史且无重复 ID；300 条的组原子写入；**真实子进程在事务前/事务中(INSERT 已发出、COMMIT 未发)/提交后确认前被 `os._exit(9)` 强杀**：库中无半组、无孤儿行、提交后强杀的那组存在且仅一次；重启后新运行把未结束的旧运行按证据截断(`ABNORMAL_EXIT`/`EXIT_TRUNCATED`)，旧段结束时刻早于新运行起点，无重复 ID｜磁盘水位全部为注入，没有填充真实磁盘；真实移动盘/独立卷未验证

### D27–D28 故障按单调时钟推进、双侧回差

D27–D28｜dev 分支工作区｜新增 `htsg/fault_tracker.py`、`tests/test_fault_tracker.py`｜`pytest tests/test_fault_tracker.py`｜16 通过｜纯函数式计时：一个越限值只观测一次，之后仅靠每秒推进即在第 5.0 秒确认（4.9 秒不确认）；待确认期回到边界内即清零且不需越过回差；无效数据取消待确认但不复归已确认；确认后不重复计时；边界等号为正常，复归 `<=`/`>=` 含等号；上下限回差分别使用（给水 6/1、含 304 路配置逐项）；直接穿到另一侧时先复归再对新方向计时；`cancel` 不是复归｜无

### D29–D30 304 路接入、批内多版本、故障记录

D29–D30｜同上｜`htsg/owner.py`（`tracker`、`rules`、`source_deps`、`_ingest` 的批内版本检测、`_recompute` 的派生接入、`_advance_faults`、`_fault_record`）、`tests/fault_support.py`、`tests/test_fault_owner.py`｜`pytest tests/test_fault_owner.py`｜D29/D30 共 11 通过｜研究配置恰为 304 路（96 温差 + 8 应变 + 192 壁温 + 8 过程量），不含 Bridge/应变变化/热应力代理；同批同点出现不同值时派生计时在批内重置（§8.2 例：内壁 490→530 同批，批末 80℃，旧计时作废，新计时从该批起算）；单版本更新、完全相同的重复、无关点更新、两路输入各更新一次均不重置；批末结果决定复归（经过中间正常值不复归）；源点逐条按批内序处理，有效正常值取消计时；**通信失效先于计时**（同一 tick 内到期与到点重合时，到期先判，不确认）；3–6 秒恢复后计时从恢复时刻的最终有效状态重新开始；Bad/极值不开始也不确认；记录中确认时刻（tick）与依据观测时间分开保存、含阈值快照/配置版本/段与批次定位；定时产生的故障走 `GROUP_FAULT` 进入同源有序提交；同一 ID 重交只存一行｜无

### D31 故障缓冲与满额处置

D31｜同上｜`owner.py`（`fault_buffer`、`fault_loss`、`fault_status`、`committed` 才移除）、`tests/test_fault_owner.py`｜同上文件｜（含在 D29/D30 的 11 项之外另 5 项，合计该文件 18 通过）｜归档停写期间确认的故障留在缓冲、随恢复单元重交、恢复单元被拒时仍在缓冲、成功后才移除；缓冲恰好 1000 条，第 1001 条起拒绝新增且不覆盖既有，累计数/源/时间范围可查（`fault_status()["loss"]`），`archive_incomplete` 为真；摘要作为 `FAULT_LOSS` 状态随恢复单元提交；状态接口明确写出"仅内存，进程退出会丢"｜**实测**：1000 条故障 + 200 条观测基线 + 193 条派生结果组成的恢复单元为 1,674,587 字节，预算 8,388,608 字节，约占 20%

### Capstone E 故障到历史闭环

E｜同上｜`tests/test_capstone_e.py`｜`pytest tests/test_capstone_e.py`｜2 通过 / 0 失败 / 0 跳过｜满量配置（208/209）、确定性时间、真实有序写库线程（可阻塞）：恒定越限仅一个样本也在 5 秒确认；4 秒断连（3–6 秒窗口）后同代次恢复，原先 116.0 起算的计时被丢弃、到 127.0 才确认；批内两个版本使派生计时在该批重置；回差复归后向相反方向越限得到第二条记录（HIGH 后 LOW，各一条）；数据库阻塞 23 秒期间 7 路温度 + 7 路温差共 14 条故障确认——当前页状态为已确认，而历史列表（`fault_record` 表）只含真实已提交的；存储恢复后恢复单元携带缓冲中的故障，最终每条已确认的故障恰好落库一次，缓冲清空，缺口表有记录；1000 条故障 + 全源基线恢复单元在预算内｜无

### D32 有效段内历史查询

D32｜dev 分支工作区｜新增 `htsg/history.py`（`read_store`、`assemble`、`query`）、`tests/test_history_query.py`｜`pytest tests/test_history_query.py`｜9 通过｜窗口内无变化也返回窗口起点所在**同段**的依据值，保留其原时间（`held=true`），不改成窗口起点；终点取下一次变化/段结束/缺口起点/水位中最早者（四种逐一验证）；无水位的开放段尾部为未知（`until=null`）；缺口前的值不跨缺口延伸、丢失的样本不被虚构；两段之间的窗口什么也不显示、值不跨段；重申值的 `time` 为重申时刻、`observed_at` 为原观测时间、`orig_id` 指向原记录，失效区间不回填；同一批内同点同时间的多条全部保留并按批内序号排列；每通道行数上限可配置（默认 10000），达到上限在结果中标 `truncated`；6 万行单通道、窗口 100 秒的查询 <1.5 秒（用点+时间索引）｜无

修复（实现中暴露）：水位早于某条记录时间时，保持值的终点曾被算成水位（早于起点）。现规定：水位只约束开放段的**最后一条**记录的尾部，且只有 `水位 ≥ 该记录时间` 才采用；后面还有同段记录时由下一条记录终止。回归用例在 D34 的跨库合并用例里。

### D33 输入依据与重申查询

D33｜同上｜`htsg/history.py`（`input_basis`）、`tests/test_input_basis.py`｜`pytest tests/test_input_basis.py`｜7 通过｜Modbus 只返回本轮响应，某输入本轮缺失时 `missing=[该点]`、`complete=false`，**不借上一轮**；`strain_change` 同时给出当前与前一计划轮次（批次号、节拍号连续）；OPC UA 按批末状态还原：同批内全部版本按批内序号返回，`batch_end` 指向最后一版，批外输入取同段内截至该批的最新状态；数值不变而质量 Good→Bad→Good 得到 3 条可区分的派生结果（OK/NOT_AVAILABLE/OK），各自指向不同的观测记录，Bad 区间不借前一条 Good；缺口内算出的重申结果标"依据不完整"（`INPUTS_LOST_IN_ARCHIVE_GAP`/`RESTATED_RESULT`/`ORIGINAL_OBSERVATION_LOST`），原始输入真丢了就如实缺失；配置版本找不到时明确报告｜跨源输入只给"最近值，仅供参考"，不承诺跨源正确性（终版 §2.1）

### D34 跨库合并与部分可用

D34｜同上｜`htsg/history.py`（`channel_sources`、`coverage_end`、`list_faults`、游标）、`tests/test_history_merge.py`｜`pytest tests/test_history_merge.py`｜7 通过｜同一段拆在两个库时合成一个 span、同 ID 记录只出现一份（含人为重复写入的副本），保持值能跨库衔接；库不可读→`partial=true`+`unavailable`，覆盖终点全部置 None，不把孤立的较晚样本当完整水位；`external_enabled=false` 后移动库不再被写入但已有历史仍可查；故障列表三库分页共 25 条（含同一时间戳并列、跨库重复投递），每页 7 条，不重不漏、(时间 desc, id desc) 稳定；游标绑定查询条件，换条件即拒绝；库不可读时列表仍可用并标 `pagination_incomplete`｜无

### D35 故障检索与回溯 API

D35｜dev 分支工作区｜新增 `htsg/history_api.py`（`build_history_router`、`parse_instant`）、`tests/test_history_api.py`｜`pytest tests/test_history_api.py`｜19 通过 / 0 失败 / 0 跳过（约 15 秒）｜端点全是 GET：`/api/channels`、`/api/faults`（过滤 point/direction/source/start/end，游标分页）、`/api/replay`（`center` 为 UTC 秒或**带偏移**的 ISO，`seconds` ∈ (0, 366 天]，`points` 必须是配置内通道，`limit` ≤10000）、`/api/replay/basis/{result_id}`；返回含查询时刻、`coverage_end`（每源）与 `coverage_complete_until`（任一源未知则为 null）、缺口、来源、`truncated`、`partial`/`unavailable`；无凭据 401，viewer 与 admin 都可读；OpenAPI 里所有路径只有 get，POST/DELETE 通道返回 404/405；无故障记录也能指定窗口回溯；不带偏移的 ISO、`seconds<=0`、超 366 天、未知通道、`limit>10000`、非法游标均 422；同一瞬间用 UTC / +08:00 / 秒数表示查得的 UTC 窗口完全相同；一个库不可读→200 且 `partial`，全部不可读→503；库中即使有旧 `frame_value`/`source_sample` 表也不会混入结果｜`history_api` 尚未挂到 `api.create_app`（D45 与旧路由一并替换）；**D35 提交前只重跑了 D35 的 19 项和 ruff，全套未重跑**（只新增文件，未改既有代码；上次全套 473 通过）

### Capstone F 全局回放可验证

F｜dev 分支工作区｜新增 `tests/test_capstone_f.py`；改 `htsg/owner.py`（`SourceState.lost_seg`）、`htsg/history.py`（`read_segment_rows`、`query` 补查、`assemble` 过滤）；回归用例加在 `tests/test_archive_gap.py`、`tests/test_history_merge.py`｜`pytest tests/test_capstone_f.py`｜9 通过 / 0 失败 / 0 跳过；**全套 503 通过 / 0 失败 / 0 跳过，284.33 秒**，`ruff check .` 与 `git diff --check` 通过｜满量配置(417 路)、t=101..145 秒的已知值脚本经 owner→整组路由→三个真实库→HTTP 回放：417 路逐项应答、无截断无部分；输入值与时间逐项对得上（静止点由原观测或重申支撑，窗口内无变化时仍给出同段保持值并保留原时间）；Modbus 每轮观测与应变等派生值逐轮对应，无"旧输入配新结果"；t=110 同点三版本按批内序全留、批末只算一次派生；t=120/122 数值不变仅质量变化得 3 条可区分记录，Bad 区间不借 Good 值；t=126–128 归档拒绝并在 t=127 丢一次 UA 变化——缺口不被跨越、丢失样本不被虚构、模拟量不跨缺口插值；t=137 墙钟 +10 秒开新段，三个通道均恰为 3 段，任何保持值不越过段终点，段起点 1047；墙钟 ≥1024 起整组走移动库，一个库离线→`partial` 且覆盖终点全 None、行数上限在 `truncated` 中明示；同一瞬间不同时区写法得到相同 UTC 窗口｜见下

**写测试时没有迁就预期、逐项查因的结果（共 4 个失败，原因各不相同）：**
1. **脚本接线缺陷（测试侧）**：`Sink` 只接收、从不回调 `owner.committed`，来源因此永远停在 `RECOVERING`，时钟跳变不开段、水位不再产生。脚本现在在写入器接收后、owner 调用返回之后才回调（与 `CommitQueue` 的异步确认一致；在 `commit` 内同步回调会赶在 `archive="RECOVERING"` 之前）。
2. **owner 真实缺陷（已修）**：丢失期间每个 `tick` 的恢复尝试都先 `s.seg += 1` 再提交，被队列拒绝后段号白白用掉；最终成功的恢复单元的 `SEG_END` 指向最后一次尝试的段号（库里从未存在），缺口前真正落库的段永远不被收尾（`end_wall=NULL`）。修法：`SourceState.lost_seg` 记录丢失开始时档案里仍开着的段，恢复单元的 `SEG_END` 指向它，`committed` 确认恢复后清空。回归：`test_archive_gap.py::test_recovery_attempts_the_queue_turns_away_do_not_move_the_segment_the_gap_closes`（修前失败：`SEG_END.seg` 为 3 而非 1）。
3. **历史读取真实缺陷（已修）**：段的开头在一个库、结尾（无起点的占位行）在另一个库时，`read_store` 对每个库各自按窗口过滤，窗口落在段结束之后时占位行被滤掉，合并只看到另一库里仍"开着"的段，回放把已结束的段当成仍在保持。修法：`query` 在各库读完后让每个可读库补查它缺少的段行；`assemble` 丢弃合并后确认已在窗口前结束的段中早于窗口的保持值行；补查失败按该库不可读计入 `partial`。回归：`test_history_merge.py::test_a_segment_whose_end_sits_in_another_database_is_not_open_for_a_window_after_that_end`（修前失败）。窗口取宽（980–1080）时合并本来就对，所以 D34 的用例没发现。
4. **预期写错（测试侧，两处）**：(a) t=129 的那一轮在同一秒、`tick` 之前到达，来源仍是 `LOST`，设计上丢失期间不入队，它只作为恢复单元的基线重申入史（`STATE_ASSERTION`），不是 `RECEIVED`；用例现断言它以重申形式存在。(b) "安静窗口"原取 1043±3，但 t=137 的 +10 秒使 wall 1037–1046 不属于任何段，该窗口本就应什么都不声称；现改取段内的 1033±2 验证保持值，另加空洞窗口断言"无段、不声称"。

### D36 版本快照、组推送及心跳

D36｜dev 分支工作区｜新增 `htsg/publisher.py`（`Publisher`、`encode_group`）、`tests/test_publisher.py`、`tests/client_model.py`（参考客户端，D38 的 JS 照它实现）；改 `htsg/owner.py`（新增 `channel_views`、`status`、`snapshot`；`_advance_faults` 对缓冲拒绝的确认发 `FAULT_REFUSED` 状态组）｜`pytest tests/test_publisher.py`｜10 通过 / 0 失败 / 0 跳过；全套 513 通过 / 0 失败 / 0 跳过（296.21 秒），`ruff check .` 通过｜**线上消息（实现取值）**：`snapshot`{run_id,pub_seq,channels,status,wall}；`groups`{first,last,groups[{seq,order,source,wall,channels,states,faults}],status,wall}；`heartbeat`{seq,status,wall}。组内不带原始 Observation/Result 记录（那是归档用的），带"通道视图"——该组改变了的通道的完整当前状态（值、质量、时间、来源有效性与原因、故障待确认/已确认/方向）。视图由 `Owner.channel_views()` 唯一定义，快照与增量共用，前端不必从生命周期记录里推断有效性。**100 ms**：从第一个等待的组到达起算，到点把等待的组整批按序发出，多组同消息、不删不合（用例：同一通道 480→481→482 三组，消息里三组的该通道值各为 480/481/482）。**快照**：`subscribe` 先把等待中的组发给已连接客户端，再取 `owner.snapshot()`，其 `pub_seq` 必须等于发布器收到的最后组号（否则报错），此后该客户端只收更高序号的组。**心跳**：自上次任何广播起满 1 秒无发送才发，不含通道和组，`seq` 为最近组号（客户端据此发现漏组）；空的 UA 保活批不产生组，所以静默订阅只有心跳。**性质测试**：34 步故事（Modbus 轮缺失/超时、UA 良好/Bad/极值、故障待确认→确认→复归、3 秒失效、同代次恢复、6 秒重建、新代次、归档拒绝与恢复、墙钟跳变、断连），每一步前加入一个新客户端（共 34 个），每步后断言"各组累计的视图 == owner 当前视图"（证明没有未发布的状态变化），结束时每个客户端的状态 == owner；并断言故事确实走到了 PENDING/CONFIRMED/NORMAL、POLL_MISSING/POLL_TIMEOUT、QUALITY、EXTREME、SOURCE_WAITING/EXPIRED/RESYNC（防空转）。变异试验：去掉 `subscribe` 的先刷新、或丢掉打包消息的中间组，各有 5 项用例变红（试验后已还原）｜见下

**暴露的缺陷（已修）：** 缓冲满额后确认的故障只改了追踪器状态，不产生任何组，客户端永远不知道该通道已进入"已确认"（D44 要求此时显示归档不完整及计数）。现对被缓冲拒绝的确认发一个 `FAULT_REFUSED` 状态组（先例：`APP_LOSS`——损失一律成为存储状态；它也进归档，是状态行不是故障行，不冒充已找回的单条故障）。**实测：** 满量(417 路)一组 Modbus 轮的变化只带 24 个通道；发布器每组开销 0.23 ms；而 `Owner.accept` 处理一轮 Modbus 在满量配置下约 29 ms（不挂发布器也如此，非 D36 引入）——D46 的满量测量需覆盖，必要时优化。**D45 接线补充：** `Publisher(owner)`，`owner.publish = publisher.offer`，事件循环里常驻 `publisher.run()`。

**同时修正上一次提交的笔误：** Capstone F 条目之后的 `## 2. 偏离、风险与未验收` 标题被我替换时误删，现已补回。

### D37 慢客户端丢失即断开

D37｜dev 分支工作区｜新增 `htsg/stream_api.py`（`serve`、`build_stream_router`）、`tests/test_stream.py`；改 `htsg/publisher.py`（`Client`、`Publisher.connect/disconnect`，`_send` 摘除掉队的客户端）｜`pytest tests/test_stream.py tests/test_publisher.py`｜20 通过 / 0 失败 / 0 跳过（D37 的 10 项约 1 秒）；`ruff check .` 通过；**全套未重跑**（只新增文件和发布器的新增类，未改既有行为；上次全套 513 通过，Capstone G 重跑）｜每个客户端一个有界发件箱（`limits.ws_queue`，默认 64，最小 2；快照占第一个位置）；放不下新消息即清空发件箱、标记丢失、发布器不再向它投递，`next()` 返回 None，端点以 1013 关闭——此前排队的消息不会在洞之后再发出；等待快照期间再次溢出同样断开（用例：连上后不读，两轮都被断开）；重连得到的是最新快照，其后的组无洞（参考客户端核对）。**5 秒发送超时**（`timing.client_send_timeout`）→ 1013；认证 5 秒（`AUTH_WAIT`）；凭据错/来源不符 → 1008（来源不符在 accept 之前）；账号停用：每条消息发送前询问 `account_ok`（由心跳触发），之后不再发送任何消息；`max_clients` 超限 → `RuntimeError("CLIENT_LIMIT")`。**其他客户端与归档**：6 条消息间一个客户端被断开，另一个客户端的状态 == owner，归档收到 1..order 的全部组且顺序不乱。**真实 WebSocket**（Starlette TestClient 的协议栈，事件循环线程里改 owner）：快照先到、组随后、重连得到更新的快照。变异试验：溢出时静默丢最新而不断开 → 4 项变红；去掉发送超时 → 1 项变红（均已还原）｜旧 `api.stream` 仍在；D45 挂 `build_stream_router(publisher, verify, account_ok, config)` 并删旧，`account_ok` 对应旧代码的 `accounts.uncertain or not users[...]["active"]`。**未测：** 16 个客户端同时 × 每秒 10 条消息的序列化开销（每个客户端各自 `send_json`），D46 覆盖；浏览器端真实慢连接由 Capstone G 验证

### D38 前端摆脱 frame 并整组应用

D38｜dev 分支工作区｜新增 `htsg/static/stream.js`（`apply`/`newLive`/`Rejected`，与 `tests/client_model.py` 同一套规则）、`htsg/web.py`（`create_web_app`：静态页、`/api/me`、`/api/ui-config`、实时 socket、历史路由）、`tests/web_rig.py`（服务线程里跑真实 owner/publisher/socket 的测试台）、`tests/browser_support.py`（Playwright+Chrome 夹具）、`tests/test_stream_js.py`、`tests/js/apply_messages.mjs`、`tests/test_web_stream.py`；重写 `htsg/static/app.js`、`index.html`；**删除** `tests/test_replay_ui.py`｜`pytest tests/test_web_stream.py tests/test_stream_js.py`｜5 通过 / 0 失败 / 0 跳过（浏览器 4 项约 11 秒、Node 契约 1 项）；`node --check` 两个 JS 文件通过；`ruff check .` 通过；**全套未重跑**（Capstone G 重跑）｜**前端规则**：整条消息先校验（首尾序号、组内连续、组内容形状、通道必须在快照中、视图必须有 `valid`）再整组应用，不过则整条丢弃、`ws.close()`、提示并重连取快照；快照整体替换缓存；断线即清空缓存（重连前页面显示"等待实时数据"，不保留旧值或残缺派生）；`valid=false` 一律显示"—"加原因，不当 0；连续 3 秒（`ui_stall_notice`）无任何消息显示延迟提示并标"消息中断"，下一条消息到来即清除，提示明确"不代表数据源状态"。**跨语言契约测试**：Python 发布器产出的真实消息（34 步故事）由 Node 用 `stream.js` 应用，终态与 owner 的 `channel_views` 完全相等；11 种破坏（首尾错、组内缺/乱序/前缺、未知通道、无 `valid`、缺字段、心跳领先、未知类型）全部被拒且状态逐字不变；变异（去掉前缺检查 / 改成先应用后校验）各使其变红。**真浏览器**（Chrome，viewer 登录）：有效值显示、Bad 质量变"—"+"质量不良"且绝无"0"；馈送器暂停+心跳停→约 3 秒后出现延迟提示、数值仍在但被标注，恢复后提示消失；断线（服务端按溢出切断）→"重连中"、旧值不保留、断线期间变化的值在重连快照中出现；注入一条被篡改的组消息（`last` 错，含 999 的伪造值）→ 页面绝不出现 999、提示"未通过校验"、重连后恢复真实值且提示消失；页面无 JS 错误。变异：断线时不清缓存 → "断线后不留任何东西"一项变红｜`htsg/web.py` 尚未被运行入口使用，D45 构造 owner/publisher/archive/accounts 后调用 `create_web_app(config=, accounts=, publisher=, archive_router=)`，并为 `accounts` 挂 `persist/reconcile`（旧 `api.py` lifespan 的做法）。**旧资料失效：** `scripts/validate_ui.py`、`validate_recall_ui.py` 针对旧页面（旧 id 与旧接口），已不能用，D45 删除或改写；被删的 `test_replay_ui.py` 测的是旧历史页"故障定位后仍可自由换通道、改中心时间和窗口"，**D39–D40 的新用例必须承接这一意图**（终版 §14）

### D39 历史页布局与故障检索

D39｜dev 分支工作区｜改 `htsg/static/app.js`（第 5 页：`historyHtml`、`mountHistory`、故障检索 `loadFaults`、设置 `replay`）、`style.css`；`tests/web_rig.py` 增加可选一次性归档库（`archive=True`：`CommitQueue` 写入真实库、`seed_faults` 直接写故障记录）、`tests/conftest.py` 增加 `archive_rig` 夹具与固定时区 Asia/Shanghai 的 `page`；新增 `tests/test_web_history.py`｜`pytest tests/test_web_history.py`｜5 通过 / 0 失败 / 0 跳过（约 8 秒）；D38 的 5 项复跑通过；`ruff check .`、`node --check` 通过；全套未重跑｜**布局**：左侧故障列表、中上回溯设置、中部结果区（同一中间栏，坐标核对：列表右缘 ≤ 设置左缘、设置下缘 ≤ 结果上缘、二者左缘与宽度一致）；窄屏单栏。**检索**：通道检索框收窄通道下拉后选择，方向、起止时间过滤；每页 20 条、"加载更多"续页，不重不漏、最新在前（45 条分三页）；"加载更多"沿用上次检索的条件（游标属于其查询），改了输入框但未点检索也不会因游标不符报错；快速连续检索时用代次丢弃迟到的旧响应；部分库不可读时列表标明不完整。**设置**：点选故障只填入中心时间和默认通道并加锁定提示，中心时间、窗口长度、通道（可多选、可检索）始终可改，改了中心时间会标"已手动修改"，可解除锁定而保留已填值，完全手动填写不需要任何故障（承接被删旧用例的意图）。变异：去掉迟到响应守卫 → 变红（此前该用例是空转的——守卫缺失只污染内存不重绘——已加"迟到响应后再触发重绘并核对条数"才抓得到）；"加载更多"改读未应用的输入框 → 变红｜设置尚未查询和持久化（D40）；结果区只是占位

### D40 回溯设置、查询与并发响应

D40｜dev 分支工作区｜改 `htsg/static/app.js`（`loadReplay/saveReplay/clearReplay`、`runReplay`、`drawReplay`、`plotReplay`、记录表）；`tests/web_rig.py` 增加 `run_id`/`feed_archive`/`write_groups`（只带归档、实时 owner 不写库）、`tests/conftest.py` 增加 `replay_rig`（归档里放入 Capstone F 场景）、`tests/test_capstone_f.py::script(cfg=None)`；新增 `tests/test_web_replay.py`｜`pytest tests/test_web_replay.py`｜10 通过 / 0 失败 / 0 跳过（约 16 秒）；前端全部 29 项（含 D36–D39 的浏览器与 Node 用例、Capstone F）复跑通过；`ruff check .`、`node --check` 通过；全套未重跑｜**设置**：只有设置存 sessionStorage（键 `htsg.replay`：fault、center、seconds、points、chart，实测 < 600 字符，不含任何结果）；刷新后保留并在进入页面时自动重新查询；"清空设置"与登出删除。**查询**：进入页面、点"查询"、选故障、切换图表通道都重新向数据库查询；没有设置时不发任何 `/api/replay` 请求，也没有"当前时刻"默认窗口（核对请求列表）；新请求取消旧请求，迟到的旧响应被丢弃，新窗口失败时不显示旧窗口结果。**显示**：查询时刻、窗口与中心、覆盖终点（全部来源确认到何时；任一来源未知则写"未知"）；逐通道汇总（窗口起点依据——值、原观测时间、实收/状态重申；窗口内记录数；最后一条；是否含无效或极值）；归档缺口逐条列出并在图上画阴影、缺口内不画线也不造数；有效段起点竖虚线；质量不良区间标红且不借前值；同一时刻的三个版本按批内序号逐行列出；窗口起点的保持值虚线加空心点、记录表标"窗口起点的保持值"并保留原观测时间；达到上限时按通道提示并标"截断"；数据库不可读时提示结果不完整。**对着已知答案核对**（Capstone F 场景）：缺口两条（UA 1027→1029、Modbus 1026→1029，QUEUE_FULL）、重申行 `原观测 08:16:41`、丢失的 479 没有"实收"行、Bad 区间红色像素有而青色线像素为 0、缺口内青色线像素为 0 而阴影像素有（读 canvas 像素，几何由页面提供）。变异：线拉到窗口末尾 → 缺口与 Bad 两项变红；登出不清设置 → 变红；去掉上限提示 → 变红｜**如实说明：** "旧窗口的迟到响应不能覆盖新窗口"这一项在可见行为上有取消请求与代次守卫两层保护，单独去掉其中一层，用户看不到差别（污染的状态不会被重绘），所以这项用例验证的是行为而不是某一层；记录表一次渲染全部行（上限每通道 10000 行），极端窗口下可能偏慢，D46 的浏览器测量覆盖。结果区按通道只画"图表与记录通道"一个，CSV 导出范围在 D41 确定

### D41 CSV 导出当前结果

D41｜dev 分支工作区｜新增 `htsg/static/replay.js`（纯函数：记录文字、`channelRows`、`gapMark`、`isoLocal`、`buildCsv`；从 `app.js` 搬出 `REASONS/ORIGINS/ENDS/rowState`）、`tests/js/build_csv.mjs`、`tests/test_web_export.py`；改 `htsg/static/app.js`（导出按钮与 `exportCsv`，记录表加"缺口标记"列，查询请求记录当时的参考故障）｜`pytest tests/test_web_export.py`｜5 通过 / 0 失败 / 0 跳过；前端全部 25 项（D36–D41 的浏览器与 Node 用例）复跑通过；`ruff check .`、`node --check` 通过；全套未重跑｜**导出内容（实现取值）：** 导出的是屏幕上"图表与记录通道"的记录（表格与 CSV 由同一个 `channelRows` 产出同样的行）；其他已查询通道通过切换该通道查看并另行导出，文件头列出本次查询的全部通道。文件头：查询时刻（含偏移与 UTC 秒）、时区、中心时间、窗口起止与长度、参考故障、查询通道/导出通道/名称/单位、每通道行数上限、是否达到上限与哪些通道、覆盖终点、不可读数据库、逐条归档缺口、"保持值"说明；空一行后是记录：通道、种类、生效时间与原观测时间（各含带偏移的 ISO 与 UTC 秒）、批次号、批内序号、值、是否有效、质量/状态、来源（实收/状态重申）、保持值、有效至与终止原因、有效段、缺口标记。UTF-8 带 BOM，CRLF，字段按 RFC 4180 加引号；数字保持数字（负数不被当文本），文本以 `= + - @` 等开头时前置单引号防公式注入；无效值留空，绝不写成 0。**验证：** 点击导出不发任何新请求（核对请求列表）；文件行数、批内序号、来源与屏幕表格逐行相同，无效行恰是红色行，数值与屏幕显示一致到显示精度，BAD 行带 `0x80350000`；窗口起点的保持值导出为"保持值=是"，生效时间 1029 与原观测时间 1001 原样（不盖成窗口起点 1031）；上限 3 行时文件头写"是"并列出通道；查询后又改了中心时间但未重新查询，文件仍描述已显示的结果；无结果时没有导出按钮；Node 下用含逗号、引号、换行、`=1+1` 的通道名/单位验证往返与防公式｜**未做变异验证**：本项我准备对源文件做原地变异试验时被用户中止，之后没有再做；上述断言是对产出文件的直接核对（BOM 字节、空值、转义往返），但"断言能否抓住对应缺陷"这一层没有证据。含缺口的窗口里"缺口标记"来自结果里的缺口列表，窗口之外的缺口不会出现在结果中，因此不标

### D42 总览与 8 点应变分布

D42｜dev 分支工作区｜改 `htsg/static/app.js`（页 1 重写：`overviewHtml/updateOverview`；页 2 新增：`strainHtml/updateStrain`；`stationState`、`strainTable`）、`style.css`；新增 `tests/test_web_strain.py`｜`pytest tests/test_web_strain.py`｜4 通过 / 0 失败 / 0 跳过（约 16 秒）；前端全部 29 项复跑通过；`ruff check .`、`node --check` 通过；全套未重跑｜**三种量分别显示**：原始信号（mV/V）、换算应变（με，GF=6，未作温度补偿）、应变变化（με，相邻两个计划轮次的增量）各一列，列头带量名与单位，页内明写"不是已验证的机械应变"；用已知值核对：桥路 0.6→100 με，0.9→150 με，变化 150−100=50 με，三个数字各归各位（页 1 与页 2 一致）；无效时三列都是"—"不是 0。**8 个固定色块**：恰 8 个，位置按配置的示意坐标（核对中心点百分比）、互不重叠、页内没有 canvas、标注"测点分布示意"和"不在测点之间插值"。**状态与颜色**：每个测点一个状态，来自 owner 自己的有效性与故障状态——正常（绿）、越限待确认（琥珀）、故障已确认（红，注明方向）、无效（灰，注明根因，如"本轮未应答"，根因取桥路信号而不是派生值的笼统原因）；四种状态四种计算样式颜色互不相同；5 秒后由待确认自动变已确认，回到正常后恢复为正常。**页 1**：来源卡（Modbus/OPC UA 的数据状态与"通信告警/通信正常"分开显示）、越限待确认与故障已确认的通道计数、过程量表、应变三种量表；一个来源进入"重新同步中"时，该来源的值显示"—"并写明原因，同时来源卡显示"重新同步中"——有效性与通信状态是两条独立的事实。**不新增弹窗/声音/独立报警**：页面期间没有 dialog 事件、没有 audio/dialog 元素、没有 role=alert 的报警列表，页上写明"只显示当前状态，不另设报警、确认或等级"。**测试发现并已修的版面缺陷**：桌面宽度下相邻色块上下重叠（每块 5 行，地图原高 520px，行距 104px），地图高度改为 640px。已看过页 1、页 2 的真浏览器截图。**未做变异验证**（同 D41 的原因，本批前端用例以对已知答案的直接核对为主）｜窄屏（≤520px）只做了 CSS 缩小，没有用真浏览器按手机尺寸逐项核对；Capstone G 的浏览器验证应补

### D43 第四页实时三分栏

D43｜dev 分支工作区｜改 `htsg/static/app.js`（页 4：`trendHtml/mountTrend/updateTrend/plotTrend`，趋势缓冲 `trendNote/trendBreak`，`findPairs`；单位 degC 显示为 ℃）；新增 `tests/test_web_trend.py`｜`pytest tests/test_web_trend.py`｜5 通过 / 0 失败 / 0 跳过（约 29 秒）；前端全部 34 项复跑通过；`ruff check .`、`node --check` 通过；全套未重跑｜**一个图表区域三个纵向分栏**：负荷 MW、所选温度对的内外壁温差 ℃、同一对的热应力代理 MPa；共用一条时间轴（最近 300 秒，右端取服务端最后一条消息的 `wall`，不混入本机时钟）并带同步游标；各栏纵轴独立（核对三栏范围互不相同）、保留单位；温差与热应力代理带正负号（刻度、最新值、游标读数都带，实测 +50、+10、−20 原样），负荷是恒正量不加正号；热应力标"研究估算"（控制区徽标、栏名、页内文字"不是实测应力"）；页面写明"共同时间轴仅便于观察，不表示三者同步采样"，游标读数写明"各栏取游标时刻之前最近一次的值，不代表同步采样"。**同一温度对**：温差与热应力由配置里输入完全相同的一对派生（测试在配置层核对 `delta_t_NN` 与 `stress_NN` 的 `inputs` 相同），页面显示该对的内壁/外壁通道号，切换温度对时两栏同时换且不串；已用两对不同的已知值核对（+40 与 +5 不互相出现）。**断开不连线**：无效状态和连接中断都在序列里记成"无值"的点，不画线、画红色标记；用 canvas 像素核对——来源重新同步的 5 秒里三栏都是青线像素 0、红标记像素 > 0；服务端拒绝新连接 4.5 秒后重连，三栏在断开到快照之间同样无青线、有红标记（先前版本的这条断言因重连太快而没真正检查，已改为拒绝连接拉宽区间后严格检查）。**自定义历史只在第 5 页**：页 1/2/4 没有 datetime-local 输入、没有回溯设置与故障面板，也没有任何对 `/api/replay`、`/api/faults` 的请求（核对请求列表）。已看过真浏览器截图｜趋势只含页面连接后收到的组（刷新后重新累积）；一对温度通道的趋势缓冲每个序列最多保留 600 秒；窄屏未逐项核对。**未做变异验证**（同 D41/D42）

### D44 自检、权限、模型页及导航

D44｜dev 分支工作区｜改 `htsg/web.py`（新增 `/api/health`、`/api/admin/accounts`（GET/POST/DELETE）、`/api/admin/audit`；`create_web_app` 增加 `queue`、`diagnostics` 参数；数据库故障统一回 503"变更未确认"）、`htsg/static/app.js`（页 12/14/15、全局归档横幅、七页导航）、`index.html`、`style.css`；`tests/web_rig.py`（`diagnostics`、`accounts.persist` 接到归档）；新增 `tests/test_web_system.py`｜`pytest tests/test_web_system.py`｜7 通过 / 0 失败 / 0 跳过（约 18 秒）；**全套 563 通过 / 0 失败 / 0 跳过，408.75 秒**；`ruff check .`、三个 JS 文件 `node --check` 通过｜**导航**：七页顺序固定为 系统总览、水冷壁应变场、热应力分析、历史查询、系统自检、系统管理·权限、算法引擎·数字孪生（编号 01/02/04/05/12/14/15，逐页点击核对标题编号）。**系统自检（12）**：数据源与通信；OPC UA 协商参数——请求值与服务端实际授予值并列，不同处标"与请求不同"，队列被修订为 1 时标"覆盖不可检测"并写明"发布序号连续不能证明监控项无丢值"，约束违反逐条列出，未协商时写"尚未取得协商结果"；历史归档——写入目标（正常库/移动库/应急库）、写入队列与已确认/被拒/重试计数、每源归档状态、已确认水位（无水位时写"只能说明已存的点，不推断其后"）与缺口起点；故障记录归档——缓冲 N/上限、累计拒绝、按源的缺口摘要、"只在内存中，进程退出会丢失"；配置的运行时限。**全局横幅**：任何一页顶部在"故障归档不完整"（写明缓冲 N/上限、已拒绝条数、尚未写入的缺口摘要）或某源归档中断/恢复中时显示（用上限 1 的系统核对：缓冲满额后在页 1/4/12/15 都可见，累计计数在状态恢复正常后仍显示）。**权限（14）**：权限矩阵（含"在线增加、删除或修改通道、阈值与配置：只读用户与管理员都不允许"）；只读用户看到"仅管理员可…"，其访问 `/api/admin/*` 得 403；开发简化登录 `admin:admin` 仍可用；管理员创建账户（凭据仅此一次显示，离开页面后不再显示）、两次点击停用（不弹窗）、最后一个管理员不可停用并显示原因、停用后凭据立即失效、创建与停用都进管理操作记录。**模型（15）**：模型编号与版本、依据、E/α/k 与各自变化幅度、温度适用范围、使用该模型的 96 路通道、当前估算示例（温差、代理值、参数敏感性范围、符号约定），研究估算说明（研究代理量、不是统计置信区间、符号只表示温差方向）；页面不含任何重模型或占位文字（核对 RBF/PINN/iFEM/Norton/蠕变/剩余寿命/占位/即将 均不出现）。后端的英文说明（符号约定、内存说明）页面译成中文，未知的原样显示。**发现并修复**：① 前端：停用最后一个管理员被拒绝后，错误提示被随后的列表刷新清掉，用户看不到原因，现保留到下一次操作；② 后端：`web.py` 缺少旧 `api.py` 对数据库故障的统一处理，账户变更遇到数据库错误会是裸 500，现回 503"变更未确认"并有测试（同时账户状态保持"待核对"，不被默默信任）。已看过页 12、14、15 的真浏览器截图｜**一次未查明的偶发失败**：整模块运行里"管理员创建账户"那一项出现过 1 次超时（页面始终没出现新凭据），此后同一模块连续 8 次（含 6 次循环）和单项 2 次全部通过，没能复现。猜测是账户变更同步写库（`Accounts.persist`，语句超时 5 秒）偶发变慢或失败后账户进入"待核对"状态；未证实。该用例现在失败时会把页面账户区文字、`accounts.uncertain` 和账户列表写进断言消息，再出现时可直接定位。**未做变异验证**：D41–D44 的用例是对已知答案和下载文件的直接核对，没有做"改坏源码看它变红"的验证（D41 时用户中止了变异试验，之后没有再做）。`/api/health` 的协商参数由调用方的 `diagnostics()` 提供，当前只在测试台里用合成值验证过；真实采集器（`UaAcquirer.negotiated.as_dict()`）的接线在 D45。Modbus 没有"协商参数"可显示。**记录顺序修正：** D39–D43 的条目先前被我误插进 D36 "同时修正上一次提交的笔误"那段话的中间（插入点命中了该段里引用的标题字样），D37、D38 反而排在后面；本次提交已重排为 D36 → D44 的顺序

## 2. 偏离、风险与未验收

- 无偏离设计。方案"位置"列写的是改造旧文件；本次以新模块并存，D45 再删旧（见第 0 节）。
- **实现取值（非设计明文，需用户知晓）：** ① `Timing.ua_silence_margin=0.5` 秒（终版要求"静默+裕量<有效期"但未给数）；② 从未有过有效值的派生量首次算出 NA 时不写记录；③ 水位只约束开放段最后一条记录的尾部；④ Modbus 每轮原始观测逐轮存储，派生结果仅变化时存；⑤ `config_id` 改为键排序规范化哈希。
- **已知但未修、未用例验证（按代码推断）：** `lost_seg` 只修了"丢失开始时该段开着、恢复尝试被拒"这条路径。下列情形仍可能让缺口前落库的段 `end_wall` 为 NULL：① 丢失期间来源又 EXPIRED/RESYNC（`SEG_END` 因 LOST 未入队，恢复单元看到 `open_segment=False` 不再补）；② 引发丢失的正是含 `SEG_START` 的组被拒（新段从未落库，旧段的结束也在被拒的同组里）；③ `RECOVERING→LOST` 后恢复单元又被策略拒绝（`lost_seg` 已被改记成恢复单元的新段）。这几种情形下回放仍受缺口行与水位约束，但段本身显示为开着；要修需在丢失期间把"已落库段的收尾事实"记下来、恢复时补发，D45 接线前后择机做。
- **现场必须核验：** asyncua 测试服务器的保活比协商值晚一个发布周期（见 Capstone C 记录）；真实 Acheron、UA 网关、独立卷、一年容量均未验。
- 会话预算：用户"用尽 session limit"指 5 小时会话额度；CLAUDE.md 的 30,000 token 条款，用户随后明确说"别控制预算了，我自己看着来"——预算由用户掌控，执行时不自行限额、不为预算停下或提问。

## 3. 交接（新会话从这里开始）

**完成：** D01–D44（模块 A–F 全部，Capstone A–F，模块 G 的 D36–D44，即后端推送与七页前端的全部单项）。D44 提交时全套 **563 通过 / 0 失败 / 0 跳过，408.75 秒**（D36 时 513；D37–D44 新增 51 项并删除旧 `test_replay_ui` 1 项），`ruff check .` 与三个 JS 文件的 `node --check` 通过。**未做：** **Capstone G（浏览器端到端，用户指示完成 D44 后停止开发，故未开始）**、模块 H（D45–D48、H0、最终 Capstone）。下一步是 Capstone G，见下。**接手前注意：** 第 2 节"已知但未修"一条（丢失期间已落库段可能不被收尾）与 D45 接线相关；`ruff format --check` 在旧文件上本就不通过，项目只要求 `ruff check` 与 `git diff --check`。

**进入新会话的第一步：**
```powershell
.venv/Scripts/python.exe scripts/bootstrap_db.py start      # 开发库集群默认不启动；不启动则所有 DB 用例 130 秒后 ConnectionTimeout
.venv/Scripts/python.exe -m pytest -q                        # 全套约 4 分钟；输出里的连接串含凭据，落盘前用 sed 脱敏
git log --oneline -8; git status --short
```

**新链路代码地图（均已就绪、已测，但 Runtime 尚未接线，D45 才切）：**
`records.py`（记录与确定性 ID）→ `owner.py`（单所有者：批次接纳、有效性、派生、change 基线、来源状态机 WAITING/VALID/EXPIRED/RESYNC、告警、同代次恢复、段/时钟跳变/水位、归档状态 OK/LOST/RECOVERING、故障接入与缓冲）；`derive.py`（算子）；`fault_tracker.py`（5 秒计时/回差）；`ingress.py`（有序入口：批次+代次/断连/失步事件）；`modbus.py`、`ua.py`、`recovery.py`（采集与退避）；`commitq.py`（有序队列）→ `archive_router.py`（整组路由）→ `archive.py`（`ArchiveStore.write_group`）+ `archive_schema.sql`；`history.py`（读取、依据、故障列表）。测试支撑：`tests/owner_support.py`、`protocol_support.py`、`live_support.py`、`fault_support.py`、`conftest.py::archive_db_factory`。

**D45 接线清单（Runtime 现仍是旧帧链路）：** `router.initialize(run_id)`+`bind_run` → `owner.start` → 每个启用源建 `ModbusAcquirer`/`UaAcquirer`，`hub=Ingress`，`owner.on_rebuild=uaa.request_rebuild`（多源需分派）→ `CommitQueue(on_committed=owner.committed, on_refused=owner.refused, is_void=owner.is_void)`，`owner.commit=queue.offer` → 事件循环里每秒 `owner.tick(time.time(), time.monotonic())`，`ingress.drain()` 由唤醒驱动 → 停止顺序：停采集、排空 ingress、`owner.shutdown`、`queue.stop`。要删的旧模块：`engine.py`、`runtime.py` 旧部分、`calculation.py`、`faults.py`、`adapters.py`（`decode_registers` 先搬进 `modbus.py`）、`storage.py`、`schema.sql`、`capture.py`；旧测试按第 1 节 D01 映射处理；`config.py` 中 `profiles/stale_after/max_age/derivative/aggregation/Source.timeout` 等旧字段随之删除。

**Capstone G 要做什么（方案第 9 节）：** 在同一代码版本重跑模块 G 全部用例（`tests/test_stream.py`、`test_publisher.py`、`test_stream_js.py`、`test_web_*.py`），再补跨页场景：单组/多组消息首尾序号一致（已有 Python/Node 契约）；**注入首尾错误、内部缺组、乱序时前端无部分更新并重连取快照**（D38 已覆盖首尾错一种，需补内部缺组与乱序的浏览器用例）；**制造慢连接**（真实 WebSocket 不读取，触发发件箱溢出或发送超时，浏览器端的表现）、源失效（已有 RESYNC 用例）、归档缺口（D40 用 Capstone F 的缺口）下"页面不出现新输入配旧结果"；刷新保留设置、登出清除、CSV 与界面一致（D40/D41 已覆盖）；故障缓冲满额后当前页显示不完整及计数（D44 已覆盖）；viewer 不能在线改通道（目前没有任何改通道接口，且 `/api/admin/*` 对 viewer 403）；确认第 1/2/4 页不含自定义历史查询（D43 已覆盖）；三种应变量分别显示、模型页参数/版本/说明齐全、第四页不暗示同步采样（D42/D43/D44 已覆盖）。**窄屏（手机宽度）从未用真浏览器逐页核对**，应补。浏览器日志与截图是第 11 节要求的工程证据：D42/D43/D44 看过截图但没有落盘归档，Capstone G 需保存。D41–D44 的用例没有做变异验证，Capstone G 前宜补做（不要在源码上原地改完不还原）。

**模块 G 现状：** 后端 `publisher.py`（100 ms 打包、版本快照、心跳）、`stream_api.py`（有界发件箱、5 秒发送超时、重连取快照）、`web.py`（页面、身份、`/api/ui-config`、`/api/health`、账户与审计、历史路由）；前端 `htsg/static/{index.html,app.js,stream.js,replay.js,style.css}`，七页。`stream.js`/`replay.js` 是浏览器与 Node 测试共用的纯函数（`tests/js/*.mjs`）。测试台 `tests/web_rig.py`（服务线程里跑真实 owner/publisher/socket，可选一次性归档库，`feed_archive=False` 时由测试直接写归档）与 `conftest.py` 的 `browser/rig/archive_rig/replay_rig/page` 夹具；浏览器是 Playwright + 本机 Chrome（`channel="chrome"`）。新链路仍未接入运行入口（D45）；`scripts/validate_*.py` 针对旧页面，已失效。

**工具坑：** Bash 工具里超过约 8–10 KB 的 heredoc 会被截断（报 `unexpected EOF`）——大补丁用 Write 写到 scratchpad 再 `python` 执行；`Edit` 前必须先 `Read`；Write 工具写出的文件在工作区是 LF，git 提交时按仓库约定统一成 CRLF（只会有"LF will be replaced by CRLF"告警）。pytest 输出在连接失败时会带凭据。Chrome 的 `datetime-local` 在秒为 00 时把 `value` 规范成不带秒，Playwright 的 `fill("…:00")` 会报 Malformed value，测试里用非零秒。Playwright 同步 API 的路由处理函数里不能阻塞等待（会卡死分发线程），要"扣住请求、测试体里再 `route.continue_()`"。变异试验不要在源码上原地改了再靠 `cp` 还原。
