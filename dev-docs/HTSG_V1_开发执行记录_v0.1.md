# HTSG V1 开发执行记录

版本：v0.1｜起始：2026-10-10｜依据：[开发实施方案](HTSG_V1_开发实施方案_v1.0.md)（设计基线哈希见方案头）、[终版设计](HTSG_V1_终版设计_v1.0.md)。本文只记录执行事实；设计和验收口径以上述两份为准。

## 0. 环境与约定

**状态更新（2026-10-14）：** 通信·日志页已实现并验收，现为七页（见 D44a–D44f 条目与第 3 节首段）；下面“本次移除验证”“页面纠偏”两段是 2026-10-13 当日的状态说明，其中“六页过渡、通信页尚未实现”已不是现状，保留为历史。

**本次移除验证（2026-10-13）：** 已删除数字孪生导航、模型卡片与专属渲染辅助函数，并移除`/api/ui-config`中仅供该页使用的`models`响应字段。该页没有独立监听端口、API路由或WebSocket；共享实时流、计算模型及结果依据保留。当前为六页，通信页未实现，模型卡片未迁到其他页。

定向命令：`.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider --basetemp=<本次唯一临时目录> tests/test_web_system.py::test_removing_the_model_page_and_its_config_payload_keeps_live_thermal_results tests/test_stream_js.py tests/test_spec_links.py`，结果**16通过 / 0失败 / 0跳过，2.57秒**，两条既有依赖弃用提示。此前沙箱临时目录权限受限，本次经工具授权在沙箱外运行临时服务和无头Chrome，未连接数据库。验证真实推送驱动热应力正负变化、移除页无残留DOM错误、推送拒绝损坏消息及文档链接。`node --check htsg/static/app.js`、修改文件ruff及`git diff --check`通过。依赖数据库的六页逐页切换用例与完整Capstone G本次未运行，既有全量测试问题未处理；本结果不表示目标七页验收完成。

**页面纠偏（2026-10-13，按用户最新指示）：** 目标仍为系统总览、水冷壁应变场、热应力分析、历史查询、系统自检、通信·日志、系统管理·权限七页。此前已移除数字孪生页及其专属接口；现有六页是过渡状态，通信页尚未实现，七页目标未完成。本轮按用户授权完成通信页审核处置：采纳审核建议并补充语义限定，回写终版并拟定实施方案；具体选择由助手在授权范围内作出，见[审核处置](HTSG_V1_通信日志页设计审核_v0.1.md#review-disposition)、[终版第15节](HTSG_V1_终版设计_v1.0.md#15-前端页面)及[实施方案状态说明](HTSG_V1_开发实施方案_v1.0.md#page-contract-correction)。本轮只改文档，未实现通信页、未运行测试。既有错误页面集合的通过记录保留为历史事实；完整套件尚无修复后全部通过记录，最终 Capstone H 仍未关闭。 下文执行经过保留历史事实，页面当前状态以本说明为准。

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

### Capstone G 浏览器端到端

Capstone G｜dev 分支工作区（D44 之后）｜新增 `tests/test_capstone_g.py`（15 项）、`docs/evidence/capstone-g/`（20 张截图（13 张场景、7 张页面）与对应浏览器日志）；改 `htsg/stream_api.py`（修缺陷，见下）、`tests/test_stream.py`（+1 项）、`tests/test_web_trend.py`（加强一条断言）、`tests/browser_support.py`（抓 WebSocket 帧、`keep` 存证）、`tests/conftest.py`（记录全部浏览器日志）｜`pytest tests/test_stream.py tests/test_publisher.py tests/test_stream_js.py tests/test_web_stream.py tests/test_web_history.py tests/test_web_replay.py tests/test_web_export.py tests/test_web_strain.py tests/test_web_trend.py tests/test_web_system.py tests/test_capstone_g.py`（同一代码版本整组重跑）｜**77 通过 / 0 失败 / 0 跳过，194.78 秒**（D36–D44 原有 62 项含新增的 1 项 + 本次 15 项）；`ruff check htsg tests`、三个 JS 文件 `node --check`、`git diff --check`（只有 CRLF 提示）通过；全套未重跑｜证据：`docs/evidence/capstone-g/`（`HTSG_EVIDENCE_DIR=docs/evidence/capstone-g` 运行用例时由 `keep` 写出；`.log` 是该页面的浏览器控制台全部输出，15 项浏览器用例均为空＝没有任何 console 输出，`page.errors == []` 也在各用例里断言）｜**真实 WebSocket 上的消息契约**（浏览器侧用 Playwright 抓每一帧）：快照在先，其后每条 `groups` 消息的 `first/last` 等于首末组序号、内部连续、与上一条首尾相接无洞，心跳不带样本；同一次抓取里确有 1 组的消息和 ≥3 组的消息，三次连续变化被打成一条三组消息，趋势里三个值依序都在、没有被合并成末态。**损坏消息**（服务端把伪造消息塞进真实套接字，6 种：首尾错、内部缺组、乱序、消息前缺组、旧组重发、心跳超前）：整条拒绝，提示里是对应原因；页面里在每次 DOM 变化时检查，伪造的负荷 987,654 从未出现，趋势序列里也没有；页面放弃连接重连，新套接字首帧是快照且序号不早于注入时序号，其后链条连续，页面显示真实负荷。**新输入不配旧结果**：页内记录器把每一个出现过的屏幕状态记下（`MutationObserver`，浏览器两次绘制之间不会被看到的中间态不会被记）。第 2 页：S01 桥路 0.60+0.01k mV/V，40 轮（第 25 轮不应答，第 11–13 轮同一时刻发出，第 20 轮切断连接），每个屏幕状态里 换算应变＝原始信号×1000/6（±0.006），应变变化＝1.67，无应答时三者同为"—"，不应答后的第一轮没有被桥接的变化，其余 7 个测点始终是 0.6/100/0；第 4 页：来源失效再重建加切断连接，温差与热应力代理在每个屏幕状态里要么同为"—"，要么代理＝1.1475×温差，两条序列的时间戳与有效性一一对应。**慢连接**：（a）原始 socket 客户端握手后不读取（小接收窗口），同时一个真浏览器在线、归档在写：该客户端被切断，它收到的是"快照＋无洞的组"的干净前缀，重连拿到更新的快照；浏览器全程只见"实时连接"，停止流量后页面序号等于所有者序号；归档队列清空、被拒/失败/重试/作废全为 0、各源归档状态为正常；uvicorn 没有记 ERROR。（b）真 Chrome 页面脚本占用 20 秒不读消息，另一浏览器正常：忙碌的那个被服务端切断（探测时约 8–9 秒、约 80 个大消息后），醒来后显示"重连中"再恢复，新套接字首帧是快照、此前的消息链连续，两个页面序号都等于所有者序号，健康的那个浏览器全程只见"实时连接"，忙碌页在前后各个屏幕状态里温差与代理的关系都成立。**归档缺口**：写入队列拒绝期间（直播）顶部横幅"历史归档中断…实时值仍在更新"，第 2 页各屏幕状态依然成套，恢复后横幅消失；历史页对 Capstone F 的缺口，`bridge_mvv_01` 与 `strain_01` 的记录逐行比对：原观测时间、批次号、来源、说明、缺口标记完全相同，缺口前最后一条止于缺口起点，缺口后第一条是状态重申，两者之间没有行。**权限**：OpenAPI 里只有账户的创建与停用两个写路由，viewer 与 admin 对 `/api/ui-config`、`/api/channels`、`/api/points` 等常见路径的 POST/PUT/PATCH/DELETE 一律 404/405，配置前后一致；七页桌面宽度逐页点击核对标题编号并存截图。刷新保留设置、登出清除、CSV 与界面一致、第 1/2/4 页没有自定义历史、三种应变量分开、模型页参数齐全、第四页不暗示同步采样、故障缓冲满额的横幅，仍由 D40–D44 的用例覆盖，本次整组重跑通过，未新增。**发现并修复**：`stream_api.serve` 在发送超时后调用 `ws.close()`，对端已消失时 `close` 抛 `WebSocketDisconnect`，没有被 `except RuntimeError` 接住，从 ASGI 应用漏出（uvicorn 记 ERROR；订阅仍被清掉）。D37 的单测用的假套接字看不到，真实套接字才暴露；现在接住它，并加单测 `test_a_timed_out_client_whose_connection_is_already_gone_ends_quietly`（先在临时副本里改回旧代码，确认该用例变红）。**变异验证**（D41–D44 一直欠的，按用户"不要原地改源码"的约束在 `tests/`、`htsg/` 的临时副本里做，工作区没有被改动）：对 D41–D44 与 Capstone G 的 28 个变异——消息校验的 4 个检查各去掉一个、拒绝后不断开、结果不应用、无效值显示成 0、CSV 去掉 BOM/无效值导出为数/弱化公式防护、断开不打断点、无效区间被连线、故障归档横幅去掉、末位管理员可停用、审计记录对 viewer 开放、新增改通道的写路由、发布只发末态组、颜色相同、协商差异不标记、viewer 看到账户区、模型页加占位、热应力与温差不同对、应变列去掉 GF 说明、发送不设超时、重连时保留旧状态等——首轮 26 个变红、2 个存活（M10、M25）：①"断开连接不打断点"：D43 用例的"断开前后"断言被更早那次来源失效的断点满足，已加一条要求断点是这次断开自己的（时间晚于恢复时刻）；改后该变异变红。②"去掉 viewport 声明"：只有手机宽度用例会管，见下。28 个里有 3 个（该条、手机块宽、宽表格内部滚动）只对应手机宽度用例，用例按用户指示撤掉后不再计入，其余 25 个变异在用例的当前版本上全部变红（D43 那条在加强后重跑过，其余是加强前版本得到的结果，用例本身未改）｜**不做的事（用户指示）**：用户 2026-10-12 明确"前端不需要做移动端 layout"，交接里"窄屏应补"一条作废。我曾写过手机宽度用例并发现小表格在手机上被横向裁掉（运行时限表的"值"列、权限矩阵的"管理员"列），已撤回用例与改动，该观察不修。**局限**：慢连接要多久才切断取决于本机 TCP 缓冲（Chrome 约 8–9 秒），用例最长等 18 秒，超时报"从未被切断"；真实 Acheron、UA 网关、独立卷与一年容量仍未验。

### D45–D48 候选版本接线、度量、崩溃与容量、交付（部分完成，被删除权限阻塞）

D45–D48｜dev 分支工作区（Capstone G 之后）｜**新** `htsg/runtime.py`（新链路装配：入口排空与 1 秒 tick、提交队列、维护、按序停止；账户对账）、`cli._run`、`web.create_web_app(lifecycle=)` 与 `/api/health.runtime`；**改** `config.py`（删 `simulator` 源、`stale_after`/`aggregation`/`max_age`/`derivative`/`profiles`/捕获类 limits/`sources[].timeout`；新增 `limits.commit_queue`）、三个 `config/*.json`、`modbus.py`（收入 `decode_registers`）、`ua.py`（`Negotiated.as_dict` 增采样周期与队列范围）、`owner.py`（配置摘要只算一次）、`stream_api.py`（上一批）、`scripts/run.ps1`（可带 `-Database -Accounts`）、`pyproject.toml`、README、`docs/openapi.json`；**新** `scripts/measure.py`、`verify_resilience.py`、`smoke.py`，`tests/test_runtime.py`(4)、`test_measure.py`(4)、`test_resilience.py`(4)、`test_smoke.py`(1)、`test_spec_links.py`(14)、`legacy_support.py`+`legacy_schema.sql`；**移植/改写**：test_dev_login、test_research_api、test_account_persistence、test_boundaries、test_config_adapters、test_thermal、test_extremes、test_simulation、test_frequency、test_storage_binding、test_capstone_b/c 各一条、三个“旧库”用例改用 `legacy_support`；两份报告 [满量测量与容量报告](HTSG_V1_满量测量与容量报告_v0.1.md)、[交付记录](HTSG_V1_交付记录_v0.1.md)；证据 `docs/evidence/d46|d47|d48`｜**结果**：排除 17 个待删旧测试文件后，最后一次完整运行 **488 通过 / 1 失败 / 0 跳过，578.56 秒**；那 1 个失败是 D42 的 `test_there_are_eight_fixed_blocks…`（页面每条消息重绘色块，测量时元素恰被替换，`bounding_box()` 返回 None；单跑 3 次通过），已改成短时重取，该文件 4 项通过，整套尚未在修复后重跑；`ruff check htsg tests scripts`、JS 语法通过｜**发现并修复的真缺陷**：① `Owner` 每存一个结果就重算整份配置的哈希，一条 200 项 OPC UA 通知耗 334 毫秒，现为 1.9 毫秒（有回归用例，变异验证确认能抓）；② Playwright 同步接口让会话级 `browser` 夹具一直占着事件循环，其后所有异步用例报 “Runner.run() cannot be called from a running event loop”，现改为模块级；③ Capstone C 的 Modbus 超时用例有竞态，改为等所有者收到失败轮次。**测量**见报告：稳定满量 418 行/秒、总延迟 p99 129 毫秒（最大 334）、无未解释丢失；断连恢复 10–15 秒（退避）；崩溃硬杀 6 次无半写组、下次启动收尾为 ABNORMAL_EXIT；注入 95%/70% 路由全部符合；**一年容量按满量口径不通过**（≈10.5 TB/年，可用卷 1.33 TB）。烟测（run.ps1 启停重启）通过。｜**删除旧链路**：我的 `git rm`/`rm` 被权限分类器拒绝，没有绕过；用户选“自己删、我给命令”，30 个文件（9 个旧模块、17 个旧测试加 1 个辅助、3 个旧页面脚本）由用户用一条 `git rm` 删除，之后我重跑全套与烟测（见下面的 H0 与最终 Capstone H 条目）。**用户指示**：前端不做移动端 layout。

### H0 切换前检查点与最终 Capstone H

H0／最终 Capstone H｜dev 分支（用户删除旧链路之后）｜代码摘要（运行入口加载的 25 个 `htsg` 模块、结构代次 5、页面、配置）与 D46、D47、烟测时完全相同，见报告第 0 节；删除前后重新计算结果一致｜`pytest -q` 全套（不再排除任何文件）、`ruff check htsg tests scripts`、`node --check`（app.js、stream.js、replay.js）、`git diff --check`｜首次不带排除清单的完整运行 **488 通过 / 2 失败 / 0 跳过，515.22 秒**：①`test_spec_links` 实施方案——它第 2 节的表链接到已删除的旧文件，已改成文字并加说明；②`test_measure` 的推送延迟用例——它把“合并等待至多 100 毫秒（加 40）”当硬上界，而实测最大值会到 220–330 毫秒（事件循环偶发迟到，见测量报告第 1.4 节），改为中位数不超过 140 毫秒、最大值小于 1 秒。两处修后各自单跑通过。**之前**还有一次整套在数据库实例没启动的情况下卡住（开发实例每次开机后需先 `bootstrap_db.py start`）。烟测（经 `run.ps1`）在删除后再次通过；烟测曾两次在“第二轮后端 90 秒不出日志、PowerShell 停在调试提示符”时失败，原因是 Ctrl+Break 也让 PowerShell 外壳进入调试提示符并一直活着，现在每轮停止后收掉外壳，之后连续 3 次通过，更早的那次失败未查明根因｜**实现取值与偏离，需用户知晓**（出处：本期要求 D45–D48 及用户指示）：① `limits.commit_queue`（默认 400 组）取代 `ordinary_queue/event_queue/write_batch_records`；② 取消 `simulator` 源与开发观测注入入口，旧 `/api/current`、`/api/history`、事件类接口不再存在；③ `Negotiated.as_dict()` 增采样周期与队列范围；④ 测量与烟测请求 OPC UA 保活计数 1（asyncua 测试服务器的保活晚一个发布周期），`config/research-sim.json` 仍是默认 2；⑤ Playwright 同步接口会占住事件循环，浏览器夹具改为模块级，需要浏览器的用例各自放在独立模块里；⑥ 在 Windows 上，任务计划的“结束任务”等于硬杀，运行以 `ABNORMAL_EXIT` 收尾；Ctrl+Break 是正常停止，进程退出码为 3｜**判定**：~~应用验收通过~~ **最终 Capstone H 不能关闭**：①页面集合与用户意图不一致，见第 2 节第一条，六页仅为移除数字孪生页后的过渡，通信页审核处置及实施方案已形成、代码尚未实现；②删除旧链路后的最后一次整套重跑**停住了**（见下）。除此之外，**一年容量按终版口径不通过**（满量 ≈10.5 TB/年，可用卷 1.33 TB；静止 ≈0.90 TB/年），所以**不能宣称一年留存通过**；真实 Acheron、UA 网关、独立卷、现场长稳与开机均未验证，清单在交付记录第 5 节。当前运行的服务没有被切换或停止，也没有注册任务计划；切换由部署人员按 README 执行。

**当前未解决的错误（按用户 2026-10-13 的指示，不再尝试修复，只记录）：** 为补“修复后整套无失败”这一条，我在两处修复提交后又跑了一次完整套件（`pytest tests -q`，开发库实例在监听、`55432/55433` 正常）：约 30 分钟里只走完 60 个用例（全是通过的点）就不再前进，没有失败输出，进程几乎不占 CPU，最后被我终止。**没有查明原因**；更早有一次同样的“走几十个用例后停住”是因为开发库实例没启动，这次不是。因此目前**没有一次修复之后的完整套件通过记录**；最接近的证据是：删除旧链路后的第一次完整运行 488 通过 / 2 失败，那 2 处修复后各自单跑通过，之后又改了前端页码标注并单跑 `test_web_system`、`test_capstone_g` 的七页用例通过。下一步先单独定位停住的那个用例（用 `pytest -v` 看停在哪一个），再重跑整套。

**停住原因已定位（2026-10-13，未修）：** 工作区（含未提交的数字孪生页移除）整套 491 项，`pytest -v` 连续运行在第 291 项 `test_runtime.py::test_realtime_state_does_not_wait_for_a_database_that_cannot_be_reached` 处停住；用 `-o faulthandler_timeout=120` 重跑前段，又停在 `test_modbus_acquisition.py::test_one_timeout_keeps_the_connection_three_in_a_row_reconnect`。两处都是在取消 Modbus 采集任务后等它结束。原因：pymodbus 3.15.0 在请求等待应答期间把 `CancelledError` 改抛为 `ModbusIOException`，`ModbusAcquirer.poll` 又把它当超时计数后继续（`modbus.py:150-152`），取消因此丢失，采集任务继续轮询，等它结束的一方永远等下去。只读探针确认：两轮之间取消立即结束，读取等待应答期间取消则 4 秒后仍在运行。分段运行时 491 项全部通过（290 + 1 + 3 + 197）。这一缺陷同样会让现场在 Modbus 设备响应慢时停止服务卡住。修正方向及证据见[通信日志页设计审核第8节](HTSG_V1_通信日志页设计审核_v0.1.md#final-review)；用户在该文第8.1节定为独立问题，本轮不修，整套仍无连续通过记录。

### D44a 既有健康语义补正（失步计数、订阅等待起点、告警基准）

D44a｜基线提交 `dc84fb2` 之上的工作区（按用户 2026-10-13 的答复：基线已提交，之后每项只更新记录、不提交）｜**改** `htsg/owner.py`（`SourceState` 增 `resync_count`、`stable_since`；`_start_resync` 在实际进入 RESYNC 时计 1；`_comm`/`_expire`/`_accept_poll` 失败轮/告警/`begin_generation` 维护稳定计时；新 `_settle`、`begin_waiting`；`_check_source` 在等待起点未到时不判静默；`start()` 把各源告警基准重设为此刻；`status()` 增 `resync_count`、`persistent_resync`）、`htsg/ingress.py`（有序事件 `begin_waiting`）、`htsg/ua.py`（`open_subscription(on_created=)` 在 CreateSubscription 返回之后、建监控项之前回调；`_generation` 经入口发出 `begin_waiting`）；**测试** 新 `tests/test_resync_health.py`（28 项）、`tests/test_runtime.py`（+1 项，慢归档打开复现）、`tests/test_source_validity.py`（改 1 处：新订阅的等待起点改由 `begin_waiting` 给出，原写法把它隐含在 `begin_generation` 里）。方案列了 `runtime.py`、`recovery.py` 但**未改**：E8 的修复在 `Owner.start()`，`Runtime.start` 本就在归档打开之后才调用它；`Backoff` 按指示不动。｜**先红后绿**：新用例先写，对原代码首次运行因缺 `resync_count` 而失败；E5、E8 另用只读探针在原代码上复现——OPC UA 源启用、尚无代次，第一个节拍即 `state=RESYNC, rebuilds=[('ua','SILENCE')]`；`Owner` 构造于 100、`start()` 于 130，启动后 1 秒（131）即已 `alarm=True`，`silent_seconds=31.0`（E8 由此从“待验证疑点”变为已复现）。｜**命令**：`PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest <文件> -v -p no:cacheprovider --basetemp=<会话临时目录> -o faulthandler_timeout=120`，分两段：①`test_source_validity、test_capstone_c/e/f、test_publisher、test_stream、test_modbus_acquisition、test_runtime、test_resilience、test_resync_health` **102 通过，184.28 秒**；②其余 49 个测试文件（含 `test_capstone_g`、`test_web_*` 浏览器用例、`test_smoke`、`test_measure`）**418 通过，343.63 秒**。合计 **520 通过 / 0 失败 / 0 跳过**（= 基线 491 + 新增 29），两段均未挂住。`ruff check htsg tests scripts` 通过；`git diff --check` 只有 CRLF 提示；未改 JS，未跑 `node --check`。**这是分段运行，不是整套连续运行，不据此宣称整套通过。**

**S1/P2 取值（出处：终版第11节约束第2条、审核 §8.1 S1、§8.3 P2）：** ① `resync_count` 在 `_start_resync` 里“已在 RESYNC 则返回”之后加 1，所以已在 RESYNC 中的建连失败重试、重复断开报告、旧代次消息都不加；启动尚无代次为 0；一次断连后一直连不上为 1；“连上即失败”的循环每轮加 1（用例：Modbus 假客户端连接成功、读取抛 `ModbusIOException`，3 轮超时→重连，5 个周期 `resync_count=5`、`conn_gen=5`；OPC UA 真实本地服务器、点表含一个服务器上不存在的节点，连上后建监控项被拒，5 个周期同样 5/5）。新代次不清零。`persistent_resync = resync_count >= timing.resync_persistent_after`（默认 5）。② 稳定计时从**当前代次的首个有效响应**（`_comm`）起算：Modbus 的 OK 与 EXCEPTION 响应、OPC UA 的保活、载有 Bad 值的正常发布都算；超时轮、`EXPIRED`、`RESYNC`、告警、换代都把它清掉，之后从下一次有效响应重新起算；`tick` 中 `now - stable_since >= 60` 则 `resync_count` 清零（边界：59.9/59 秒不清，60 秒清）。③ 60 秒用的是 `timing.backoff_stable_seconds`（同为 60），没有新增配置项。④ **与 Backoff 复位语义的差异（按指示未统一，仅记录）：** Backoff（`recovery.py`）对 Modbus 从**建连成功**起算（`modbus.py` 建连即 `healthy()`，会话结束时 `settle()`），会话内的单次超时不打断；对 OPC UA 则在 `state == VALID` 的节拍上累计。S1 的稳定计时从**有效响应**起算且任何超时都打断，所以两者可以不一致——用例 `test_a_timed_out_round_restarts_the_stable_period_although_the_state_stays_valid` 证明只看 `state == VALID` 会误判（Modbus 超时轮后状态仍是 VALID）；`test_a_modbus_gateway_that_never_connects_has_a_failing_backoff_but_zero_resyncs` 证明 `Backoff.persistent` 为真时 `resync_count` 仍为 0。

**E5：** `begin_generation` 不再设订阅等待起点（`wait_mono=None`），`_check_source` 在 `wait_mono is None` 时既不判 3 秒失效也不判 6 秒 `SILENCE`，且不退回用上一代次的最后通信时刻；首次启动尚无代次时同样不判。`begin_waiting(source, gen, mono)` 经有序入口到达 owner，只设 `wait_mono`，不碰 `last_comm_*`（用例：事件迟于首个响应到达时，通信时刻仍是响应时刻，3 秒失效按响应时刻算）；旧代次的等待事件被忽略。`UaAcquirer` 在 `open_subscription` 里于 `CreateSubscription` 返回后、`create_items` 之前发出该事件（用例用真实本地服务器，把 `create_items` 拖慢 0.4 秒，断言事件顺序为 `begin_generation → begin_waiting → items_start`，即不等监控项创建完成）；`begin_generation` 仍在初值之前进入 owner，未挪动。**新增的一条规则（超出条文，请知悉）：** 若某代次的首个有效响应先于 `begin_waiting` 被处理，`_comm` 也把 `wait_mono` 置为该响应时刻——依据：该订阅的响应只可能出现在 CreateSubscription 返回之后，所以等待必已开始，不让这样的代次失去 3/6 秒监控；生产路径上 `begin_waiting` 总在首批数据之前，此条只是兜底，也让既有以“生成后立刻送数据”写的夹具无须逐个改动。

**E8：** `Owner.start(wall, mono)` 把各源 `enabled_mono` 重设为该 `mono`；`Runtime.start` 在 `_open_archive` 之后才调它，随后立刻创建采集任务，所以“从实际启动采集起算”。仅调过 `tick` 而没调 `start` 的单元测试仍以构造时刻为基准（与此前相同）。用例：①`Owner` 构造于 100、`start` 于 130：131 无告警，Modbus 在 140、OPC UA 在 136 告警，`silent_seconds` 恰为 10/6；②`Runtime` 用慢 `router.initialize`（0.7 秒）：`enabled_mono` 不早于构造后 0.7 秒。禁用源不告警、重连不重置告警基准，由既有用例继续覆盖，本次未改。

**未覆盖/留给后续：** D44f 要求的“真实本地 Modbus 服务器”版本的 0/1/5 边界与 R1 未在本项做（本项 Modbus 用假客户端，避开已知的 pymodbus 取消丢失问题）；`resync_count`/`persistent_resync` 现经 `owner.status()` 进入 `/api/health` 的 `status` 与实时快照，D44d 再明确字段、加源状态的 `connect_failures` 并更新 OpenAPI；日志、`connect_failures` 属 D44b/c，本项没有；`Backoff.persistent` 仍只被单元测试使用。

### D44b 有界诊断入口与 owner 日志

D44b｜基线 `dc84fb2` 之上的工作区（D44a 之后，未提交）｜**新** `htsg/commlog.py`（日志本体：条目、聚合、结束、500 条先进先出、淘汰清索引、详情脱敏与截断；纯内存、无时钟、不引用归档）；**改** `htsg/ingress.py`（诊断事实单独计数 `diag_pending`/`diag_dropped`，上限 `DIAG_LIMIT=500`；`attempt_failed`/`attempt_ok` 入口；`drain` 开头把累计的“诊断丢失”数一次性告知 owner）、`htsg/owner.py`（`self.commlog`；`attempt_failed`/`attempt_ok`/`report_diag_lost` 三个处理函数；`begin_generation` 末尾结束该源的同代次轮次聚合）；**测试** 新 `tests/test_comm_log.py`（44 项）。本项只做机制：尚无任何采集器调用 `attempt_failed`，owner 自身的事件（新代次、RESYNC、失效、告警、恢复……）也还没写入日志，二者都在 D44c。｜**命令**：`PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest tests/test_comm_log.py -q`：**44 通过 / 0 失败 / 0 跳过，0.19 秒**；受 owner/ingress 影响的回归集（`test_comm_log、test_resync_health、test_source_validity、test_capstone_b/c/e/f、test_publisher、test_stream、test_modbus_acquisition、test_ua_channel、test_ua_client、test_owner_batches、test_runtime、test_resilience、test_run_segments、test_archive_gap、test_archive_recovery、test_input_basis、test_fault_owner`，真实 Modbus/OPC UA 服务与运行时在内）**243 通过 / 0 失败 / 0 跳过，203.45 秒**，未挂住。`ruff check htsg tests scripts` 通过。其余测试文件本项未重跑（D44b 未触及其依赖）；这是分段运行，不是整套连续运行。

**发现并修复的缺陷（自己的第一版）：** 脱敏正则对超长文本二次方回溯——首版 44 项用例中，一条 10 万字符的异常文本使整个文件跑了 **194 秒**；诊断详情是在事件循环上入队时清洗的，这等于能让协议任务停住。现改为：①原文超过 `SCRUB_INPUT_LIMIT=4096` 字符先截（并退回到最后一个完整词，避免尾部留下半截口令），再脱敏；②URL 的 scheme 加了回顾断言、密钥名的前缀部分移出匹配并用占有式量词，使匹配线性。加了 4 条耗时上界用例（100 万字符单词、重复关键字、重复 `a://`、重复 `password=`，各须 2 秒内完成，实测约 0.01 秒）。顺序仍是“先脱敏、后截断到 512 字符”：原因是先截会把 `scheme://user:pass@` 的 `@` 截掉，口令留在文本里（用例 `text_is_cleaned_before_it_is_cut` 覆盖）。

**实现取值（出处：终版 §15.1 重复失败/保留与淘汰/原因证据/记录路径与容量；审核 §8.1 S2、§8.3 P1）：**
- 500 条先进先出，按**首次插入**顺序；聚合更新不移动条目（用例：满 500 条、最老的是仍在不断更新的聚合，再来一条新事件，被挤出去的恰是它，`evicted` 加 1）；淘汰一条时同时清掉它的活跃聚合索引（按对象身份比对，不会误删后来的同键条目），之后同类失败新建条目（新序号、`count=1`、`first_failure` 只覆盖保留下来的跨度）。条目序号由日志分配、不复用；展示字段 `evicted`、`oldest_time`、`diag_lost`、`incomplete`、`scope="THIS_RUN_IN_MEMORY"`。
- **CONNECT/SUBSCRIBE 尝试失败**（`kind=ATTEMPT`，`conn_gen=None`，`phase` 标明阶段）：键为（源，阶段），原因码相同则合并（次数、`first_failure`、`last_failure`、最近一次详情），**不被**换代、RESYNC、另一阶段的事件、`begin_generation` 截断；同阶段原因码变了则结束旧条（`end_reason=REASON_CHANGED`）并新开；`attempt_ok(阶段)` 只结束该阶段的聚合（`CONNECT_OK` / `SUBSCRIBE_OK`），成功本身不写行；其后同样的失败是新条目。
- **轮次/请求失败**（`kind=ROUND`）：键为（源，范围 `scope`＝一条请求），同代次、同事件类型、同原因码才合并；`round_ok(scope)` 只结束该范围的聚合（`SUCCESS`）——多请求轮里一部分请求成功不会结束仍在失败的另一条请求；类型或原因码变化结束旧条；`begin_generation` 结束该源全部轮次聚合（`NEW_GENERATION`）；`EXCEPTION→OK→EXCEPTION` 是两条（用例）。
- 结束时刻 `ended_time` 与 `last_failure` 分开存，结束原因 `end_reason` 单列，不称“恢复”。条目另存“发现时刻”`found_time`、“发现时已知的最后有效通信”`last_comm_time` 和单调钟算的 `silent_seconds`（从未通信为空而不是 0）。
- 任何单条事件（`event()`）会结束同源的轮次聚合（`EVENT:<类型>`），但**不**结束尝试失败聚合——这是我对条文“该源其他非失败事件及新代次同样结束”的读法：把 RESYNC、新代次、恢复等都当作“其他事件”；条文未逐一列举，D44c 接入这些事件时按此执行，有异议请指出。
- **诊断入口：** 诊断事实走既有 `Ingress` 队列（与批次、生命周期事件保持同一顺序），`diag_pending` 与批次的 `pending` 分开计数，上限 500；满了只把 `diag_dropped` 加 1，**不**产生 `APP_LOSS`、不触发 C1、不占批次容量；批次满了也不挡诊断（两个配额互不借用，用例覆盖）；满了也不拖延生命周期事件（`source_down` 照常处理）。`attempt_ok` 是日志聚合的边界，和 `begin_generation` 一样不受诊断上限限制（极少、有界）。入队前就脱敏并截断，所以队列里至多 500 条短文本。owner 对“诊断丢失”只记总数（`commlog.diag_lost`），不记来源。
- **只进内存：** 日志操作不产生提交组、实时组、状态记录（用例：做完一轮日志操作后 `commits==[]`、`pubs==[]`、`pub_seq==0`、`order==0`）；新 `Owner`（重启）日志为空；被配置禁用的源不记尝试失败。500/501 条、聚合不移位、索引清除、乱序详情不拆分同类失败、脱敏 12 种凭据写法（URL 用户信息含 `@`、`password=`/`password:`/JSON、`token=`、`Bearer`/`Basic`、`OPCUA_PASSWORD=`）、5 条普通错误文本（含 `Authentication failed: …`）原样保留，均有用例。

**未覆盖/留给后续：** 没有采集器来源的真实事实（D44c）；owner 自己的通信事件还没写入日志（D44c）；日志通过 `/api/health` 读取与前端（D44d/e）；`CommLog.snapshot()` 的字段形状在 D44d 与 OpenAPI 一并固定。

### D44c 两协议真实事实接入与恢复记录

D44c｜基线 `dc84fb2` 之上的工作区（D44a、D44b 之后，未提交）｜**改** `htsg/modbus.py`（`_connect` 拆成 `(是否成功, 原因码, 详情)`；`run` 在每次建连失败/成功时经入口报 `attempt_failed`/`attempt_ok`；`connect_failures`；`poll` 收集每个请求的结果）、`htsg/ua.py`（`phase`、`connect_failures`、`_plain`；`_generation` 在 `connect()` 返回、`open_subscription` 返回时各报一次成功；`run` 在异常时按阶段报失败；**删除** `UaAcquirer.history`）、`htsg/owner.py`（日志写入点，见下）、`htsg/records.py`（新 `RequestResult`；`InputBatch.requests`）、`htsg/commlog.py`（`event(..., failure=)`）；**测试** 新 `tests/test_comm_facts.py`（25 项）、`tests/acquirer_support.py`（逐步驱动采集器的脚本化客户端与“闸门”）；`tests/test_comm_log.py`（+1 项；1 项随 `begin_generation` 现在写日志而更新期望）；`tests/test_capstone_c.py`（3 处原用 `uaa.history` 的断言换成同义的 owner 侧事实：`health["resyncs"]==0`、`resync_count==0/1`）。`runtime.py` 方案列了但**未改**：`connect_failures` 进入 health 属 D44d。｜**命令**：`PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest <文件> -v -p no:cacheprovider --basetemp=<会话临时目录> -o faulthandler_timeout=120`。D44b/D44c 的 70 项 **70 通过，19.76 秒**；全部测试文件分两段：段 A（日志/失步/源有效性/Capstone B、C、E、F/发布/实时流/Modbus 采集/UA 通道与客户端/批次/运行时/抗故障/测量/烟测等 24 个文件）**282 通过，301.20 秒**，段 B（其余文件，含全部浏览器用例）**308 通过，270.19 秒**；合计 **590 通过 / 0 失败 / 0 跳过**（= 基线 491 + D44a 29 + D44b 45 + D44c 25）。`ruff check htsg tests scripts` 通过。**一次挂住，原因未确定：** 段 A 第一次运行停在 `test_capstone_c.py::test_full_scale_nominal_run_stays_quiet_when_nothing_changes`，`faulthandler`（120 秒）只显示主线程在事件循环里等待、没有协程栈；该用例体内的等待都带 30 秒超时，所以更像是收尾时 `Rig.__aexit__` 里 `gather` 等某个采集任务结束——与第 2 节所记 pymodbus 取消丢失问题的症状相同，但**我没有证据证实**；我终止了进程，用只读探针（真实 `Rig`，12 次不同时刻退出）未能复现，段 A 整段重跑 282 项全部通过。按指示不当新缺陷、不修，也**不据此宣称整套连续通过**——这仍然是分段运行。

**CONNECT 失败及原因（出处：终版 §15.1 原因证据、审核 §8.1 S4、§8.5）：** ① Modbus：公开 `connect()` 返回 `False` 记原因码 `LIBRARY_NO_REASON`、详情“连接失败，库未提供原因”（不猜拒绝/超时）；应用自己的 `asyncio.wait_for` 超时记 `APP_CONNECT_TIMEOUT`、详情“建连调用0.8秒内未返回”（秒数取自配置，不说“TCP 没连上”）；抛出 `OSError` 记 `ERROR:<类型名>` 和 `repr`。`_connect` 的捕获范围保持原样（`TimeoutError`、`OSError`），没有放宽；先捕获 `TimeoutError`（它是 `OSError` 的子类）。`poll` 里 `except (ModbusIOException, TimeoutError)` 那一支行为不变（只多绑了异常变量取文本），**没有碰**取消丢失问题。② OPC UA：`set_security_string`（在 `connect()` 之前）抛异常与 `connect()` 抛异常同属 CONNECT 失败；`connect()` 正常返回即成功（不看返回值）；SUBSCRIBE 阶段从 `connect()` 返回起，到 `open_subscription` 正常返回止（订阅与全部监控项创建完成、无拒绝项），失败原因码 `ERROR:<类型名>`（如 `ERROR:SubscriptionError`）。原因码稳定、动态文本只进详情，详情在入口先脱敏再截断。③ **除通用脱敏外，`UaAcquirer._plain` 另按配置的用户名/口令环境变量的实际值替换为 `***`**（超出条文；依据“异常详情不得泄露凭据”——库的报错可能以任何形态带出口令，如 `password 'x'` 没有分隔符，通用规则抓不到）。④ 被取消的尝试不计失败（Modbus、OPC UA 各有用例：取消发生在建连途中，`connect_failures` 不变）。

**`connect_failures`（出处：终版 §11 约束第3条、审核 §8.4 R1）：** 在两个采集器上各为一个整数属性：每次已完成的 CONNECT 失败（`False`、异常、应用超时）加 1，公开 `connect()` 成功即清零（OPC UA 不看返回值，Modbus 看布尔）；SUBSCRIBE 失败、轮次/读请求超时、主动取消、RESYNC 都不计。验证：Modbus 假客户端 `[False, 异常, 超时, 成功]` 逐次读数 0→1→2→3→0；首次启动 5 次连不上：`connect_failures=5`、`resync_count=0`、`conn_gen=0`；一次断连后 5 次连不上：`connect_failures=5`、`resync_count=1`；连上后三轮超时再重连 3 个周期：`connect_failures=0`、`resync_count=3`；OPC UA 真实本地服务器：连上后订阅被拒 5 轮：`connect_failures=0`、`resync_count=5`，日志里 SUBSCRIBE 失败是**一条**（次数 5、`conn_gen` 空）、新代次与 RESYNC 各 5 条；服务器未启动时 3 次建连被拒：`connect_failures=3`，一条 CONNECT 聚合（次数 3），服务器起来后连上即清零并以 `CONNECT_OK` 结束该聚合；停掉服务器后重连失败被记录（E1）、恢复后 `COMM_RECOVERED` 带单调钟时长。属性经 `/api/health` 呈现是 D44d。

**每个请求单独报告（出处：审核 §8.2 “多请求轮”）：** Modbus 一轮里的每个请求把自己的结果（`OK`/`TIMEOUT`/`EXCEPTION`＋协议异常码＋库文本）放在批次的 `requests` 字段里，经原有有序入口随批次交给 owner，由 owner 写入日志（范围＝`寄存器类型/设备/起址+个数`）。**取舍与理由：** 没有为每个失败请求另发诊断事件——这些信息每秒一次，用批次携带则与批次同序、不占 500 条诊断名额、不会因诊断上限而丢；代价是批次因入口满被丢弃时这一轮的请求结果同样没有（那时有 `APP_LOSS`）。`CONNECTION` 类轮次（连接错误）不写轮次条目，由随后的 `RESYNC` 一条表达，避免重复。多请求轮一部分成功不会结束仍失败的那条请求（用例）；协议异常码进原因码 `EXCEPTION_CODE_<n>`，换码即结束旧条、新开一条；`EXCEPTION→OK→EXCEPTION` 两条；异常应答属于有效通信，不称断连。

**owner 写入日志的事实（每处只写一次）：** `NEW_GENERATION`（`begin_generation`，原因取其参数；这也是“重连/重建的结果”之一）、`FIRST_COMM`（本次运行首个有效响应，保活或带 Bad 值的正常发布、Modbus 异常应答均可）、`COMM_RECOVERED`（中断后的首个有效响应，带“距最后有效通信”的单调钟时长；**未告警也记**）、`COMM_ALARM_CLEARED`（告警解除；与恢复是两条）、`RESYNC`（实际进入时一次，原因即 `DOWN:…`/`SILENCE`/`SEQUENCE_GAP`/`OVERFLOW`/`APP_LOSS:…`，已在 RESYNC 中的重复报告不再记）、`DATA_EXPIRED`、`COMM_ALARM`（原因 `NEVER_RESPONDED`/`NO_VALID_RESPONSE`）、`APP_LOSS`（条目 `count`＝丢失的批次数）。“中断”＝进入 RESYNC、数据失效或告警（`interrupted` 标志），Modbus 单次超时轮不算中断，所以其后的应答不是“恢复”。连接/订阅建立（`begin_generation`、`begin_waiting`）本身不记为恢复。每个事件带“发现时已知的最后有效通信”，页面可表达“最后有效通信 T0，T1 发现”。读取状态、快照、重复报告都不新增条目（用例）。
**修正 D44b 记录里的读法：** D44b 写“任何单条事件结束同源的轮次聚合”。接入真实事件时发现这会让一次 10 秒的超时段被它自己引起的 `RESYNC`/`COMM_ALARM` 切成两条，与条文“该源其他**非失败**事件及新代次结束”不符。现分两类：失败信号（`RESYNC`、`DATA_EXPIRED`、`COMM_ALARM`、`APP_LOSS`）传 `failure=True`，**不**结束轮次聚合；其余（`NEW_GENERATION`、`FIRST_COMM`、`COMM_RECOVERED`、`COMM_ALARM_CLEARED`）结束，`end_reason` 为 `NEW_GENERATION` 或 `EVENT:<类型>`。仍属对条文的读法，请知悉。

**条目占用额度：** 按条文，新代次、RESYNC 各占 500 条额度，尝试失败合成一条；例如 OPC UA“连上即失败”的循环每轮约占 2 条（新代次＋RESYNC）外加 1 条持续更新的 SUBSCRIBE 聚合。

**未覆盖/留给后续：** health 与页面呈现（D44d/e）；`Backoff` 复位语义的差异不变（见 D44a）；`Runtime` 不改，`connect_failures` 的汇总在 D44d；诊断入口满/日志淘汰对 `connect_failures` 无影响的专项验证、真实本地 Modbus 服务器上的版本、墙钟跳变与源时间戳口径在 D44f。

### D44d `GET /api/health` 扩展（通信状态）

D44d｜基线 `dc84fb2` 之上的工作区（D44c 之后，未提交）｜**改** `htsg/web.py`（`/api/health` 增 `communication` 段、每个源状态增 `connect_failures`；新 `HEALTH_SCHEMA` 作为该路径 200 响应的文档化模式）、`htsg/owner.py`（新 `communication_inputs()`；把“该输入此刻是否可用、为何不可用”抽成 `_input_validity()`，实时视图 `channel_views()` 与通信读数表共用同一处定义，行为不变）、`htsg/runtime.py`（新 `connect_failures()`：逐源原样读取采集器自己的属性）、`docs/openapi.json`（`scripts/export_openapi.py` 重新生成；相对原文件只有 `/api/health` 一项变化，路径集合不变）；**测试** 新 `tests/test_health_communication.py`（14 项），`tests/test_runtime.py` 的真实链路用例（真实 Modbus/OPC UA、真实 PostgreSQL）里加了 health 通信部分的断言。｜**命令**：`PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest tests/test_health_communication.py -q`：**14 通过 / 0 失败 / 0 跳过，2.23 秒**；`test_runtime、test_health_communication、test_history_api、test_web_system、test_spec_links`：**61 通过，69.39 秒**；实时视图相关（`test_publisher、test_stream、test_owner_batches、test_source_validity、test_fault_owner、test_input_basis、test_capstone_b、test_comm_log、test_comm_facts、test_resync_health`，用来核对 `_input_validity` 抽取未改变行为）：**188 通过，32.09 秒**。`ruff check htsg tests scripts` 通过。全部测试文件的重跑留到 D44e 之后（页面脚本也会变），本项不据此宣称整套通过。

**响应形状（出处：方案 D44d、终版 §15.1、§11 约束第3条）：** 原有四段 `status`、`archive`、`negotiated`、`runtime` 保持原形状（`negotiated` 仍是 `Runtime.diagnostics()` 原样返回的各 OPC UA 源协商参数，用例断言它与 `diagnostics()` 返回值相等且不含 `connect_failures`/`resync_count`）；新增 ① `status.sources[源].resync_count`、`persistent_resync`（D44a 起由 owner 提供）和 `connect_failures`（`Runtime.connect_failures()` 逐源复制采集器属性；没有采集器的源——配置禁用或测试台——为 0，不为“未知”；不从日志条数、合并次数、淘汰数、退避或失步次数推算，用例里把日志挤出 200 条、诊断丢失记 9，计数仍不变）；② `communication.taken_time`（取响应的此刻，**不是**任何观测时刻）；③ `communication.log`＝`CommLog.snapshot()`：`scope=THIS_RUN_IN_MEMORY`、`limit=500`、`evicted`（淘汰数）、`oldest_time`（现存最早条目的发现时刻）、`diag_lost`（诊断丢失，与数据丢失不同）、`incomplete`、`entries`；④ `communication.inputs`：208 行读数，每个输入一行，按配置顺序。
**读数表一行＝一条观测（S3）：** `observation_id`、`value`、`quality`、`status_code`、`received_time`、`source_time` 全部取自 owner 当前持有的那一条 `Observation`，在同一个同步调用里整行读出，不可能由两条观测拼成；`valid`/`reason` 取本次截取时 owner 的状态（数据源 RESYNC 后缓存值仍显示、但 `valid=false, reason=SOURCE_RESYNC`）。从未观测过的输入各字段为 `null`（不是 0、不是“现在”）；OPC UA 源时间戳按服务器给的原样；服务器没给、以及 Modbus，为 `null`，不据以判断任何事。用例：25 次连续推送间隔着读取 5 次，每次读到的行都满足“值与它自己的源时间戳、质量、接收时刻同属一条观测”，且每次是最新一整条；两次读取之间没有新观测则两次结果完全相同（轮询不是采样）；读取不产生日志条目、提交组、实时组或状态变化。**取舍：** 起初想在行里同时给 `origin`/`asserted_time`，写用例时发现同代次恢复的“状态重申”只进归档的恢复提交组，owner 持有的当前观测始终是原观测，这两个字段恒为常量，属多余，已删除（用例改为：失效后保活恢复，该行仍是原观测、原接收时刻，`valid` 重新为真）。

**不新增路径（出处：终版 §15.1、方案 D44d）：** 路径集合与改前完全相同，用例断言其中没有任何含 `communication`/`cursor` 的路径、没有恢复 `/api/communications`。`docs/openapi.json` 与应用逐字段一致（用例用与导出脚本相同的方式构造应用并比对，改接口忘记重新导出会变红）。`/api/health` 的响应模式过去是空的 `{}`，现在文档化了新增部分（OpenAPI 3.1 的可空写法 `type: [x, "null"]`）。未登录 401；admin 与 viewer 均 200；处理函数只复制内存状态，不碰数据库、不向设备发请求。

**体积（实测）：** 读数表 208 行约 54.6 KB；日志 500 条（每条详情 120 字符的最坏情形）约 296.6 KB；构造两者共约 0.5 毫秒。自检页每 5 秒也读同一个 `/api/health`，最坏每次多读约 350 KB。条文未要求按页面区分，我按字面**无条件**返回；若要省，可加一个只在通信页请求时带上日志与读数表的查询参数（同一路径，不违反“不新增 API 路径”），这需要你定。

**未覆盖/留给后续：** 页面（D44e）；“浏览器里连续推送时只整表替换”“轮询乱序旧响应不覆盖”“其他页面实时更新不受影响”（D44e/D44f）。

### D44e 第七页“通信·日志”与活动页刷新

D44e｜基线 `dc84fb2` 之上的工作区（D44d 之后，未提交）｜**改** `htsg/static/app.js`（页面登记在“系统自检”之后、“系统管理·权限”之前；`/api/health` 的读取改为两页共用的 `loadHealth`，带序号与“页面已离开”判断；自检页“数据源与通信”表增两列；新页面的 HTML、挂载与绘制）；**测试** 新 `tests/test_web_comm.py`（11 项真实 Chrome 用例）、`tests/web_rig.py`（给测试台一个提供 `connect_failures` 的采集侧桩 `Acquisition`，经 `lifecycle=` 交给应用）、`tests/test_web_system.py`、`tests/test_capstone_g.py`（页面清单与用例名由“过渡六页”改为七页）。无新增样式文件改动，页面沿用既有 `table`/`card`/`badge`/`note` 样式；标题不标页序号，不做移动端布局。｜**命令**：`PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest tests/test_web_comm.py tests/test_web_system.py tests/test_capstone_g.py tests/test_health_communication.py -v -o faulthandler_timeout=120`：**47 通过 / 0 失败 / 0 跳过，169.26 秒**（`test_web_comm` 11、`test_web_system` 与 `test_capstone_g` 里原有用例含七页逐页切换、`test_health_communication` 14）；`node --check htsg/static/app.js` 通过，`ruff check htsg tests scripts` 通过。用的是真实 Chrome 与真实运行中的应用线程（拥有者、发布器、WebSocket）；`test_web_comm` 不连数据库，`test_web_system`、`test_capstone_g` 里带归档的用例连开发库。

**页面（出处：终版 §15、§15.1；方案 D44e）：** 三块——① 数据源状态（数据状态、通信告警、最后有效通信、代次、`重新同步次数`＋“持续失步”标记、`连续建连失败次数`，两个数分两列，下方一段说明各自的含义以及“与日志里的合并次数含义不同，互不替代”）；② 点位映射与实际读数（通道、数据源、映射——OPC UA 显示 NodeId，Modbus 显示设备/寄存器类型/地址/数据类型——、值、质量及完整状态码、可用性、后端接收时刻、源时间戳；服务器没给源时间戳、以及 Modbus，显示“未知”，从未观测过的行显示“尚无观测”；整表 208 行）；③ 通信日志（本次运行）——最新在前，列：序号、发现时刻、数据源、事件、原因/详情（详情被截断时标“（已截断）”）、代次（尝试失败显示“跨尝试，不绑定单一代次”）、次数、首次/最近失败、状态（进行中 / 已结束＋结束时刻＋结束原因，不称“恢复”）、最后有效通信及“发现时已隔多少秒”；顶部一行写明“当前保留 N/500 条、已淘汰 N 条（现存最早记录时刻）、诊断丢失 N 次（不是数据丢失，也不触发重新同步）”，淘汰或诊断丢失时出“日志不完整”标记；无记录时写“暂无记录……不表示从未发生通信故障”。页首一段说明：只读诊断、只显示本次运行内存、不读取归档、读数是“状态获取时刻”的一次快照（与每行的接收时刻分开）、源时间戳只原样显示。**不设日志等级**（用例断言页面没有“严重/警告/致命”等字样），**没有弹窗和声响**（用例监听对话框、查 `<audio>`）。事件名用中文（新代次建立、首次有效通信、通信恢复、通信告警解除、进入重新同步、数据失效、通信告警、应用丢失、建连/建订阅尝试失败、Modbus 请求无应答、Modbus 协议异常应答），原因码原样给出。

**刷新（出处：终版 §15.1 “本页活动时每5秒读取一次health”）：** 进入页面立即读取一次；只有“系统自检”和“通信·日志”两页在前台时，每 5 秒读一次 `/api/health`；离开即停（用例：离开 7 秒期间请求数不变）。`loadHealth` 每次自增序号，应答回来时若已有更新的请求、或页面已离开、或已登出，直接丢弃——旧应答不会盖住新应答，离开期间到达的应答也不会画到别的页面或在重新进入时闪现（进入通信页时 `health.data` 清空，显示“正在读取…”，因此不会出现上次访问留下的旧行）。读取失败时：新访问里显示“读取失败：…”，已有内容时保留上次读到的内容并标“最近一次刷新失败”。**整表替换：** 页面没有 `update` 钩子，实时（WebSocket）消息不会触及本页任何单元格；读数表与日志、状态都来自同一次 health 应答。用例：以每秒 4 次的速度连续推送同一点的新值 12 秒，页面上该单元格只在轮询边界变化（≤4 次，相邻变化相隔 ≥4 秒），同期实时流序号前进 ≥40，而总览等页的实时更新不受影响。
**两个计数在两页分列显示（出处：终版 §11 约束第2、3条；方案 D44e）：** 通信页与自检页的数据源表都有“重新同步次数”（达到 5 次出“持续失步”）和“连续建连失败次数”两列，数值来自同一次 health 应答，所以两页一致（自检页的这两列因此每 5 秒更新，其余列仍随实时流）。用例：把 OPC UA 源推到 5 次重新同步、给测试台设连接失败 2/0，两页读到同样的数字；页面停留时修改计数，下一次轮询后更新。

**变异验证（在 scratchpad 的临时副本里做，工作区没有被改）：** M1 把实时值补丁进读数表 → “整表替换”用例变红；M2 去掉旧应答判断 → “旧应答不盖新应答”用例变红；M3 任何页面都轮询 → “只在前台读取”用例变红；M5 自检/通信页只留一个计数列 → “两个计数分列”用例变红；**M4（去掉进入页面时的立即读取）首轮没有被抓到**——用例只断言“稳定后请求数＝1”，下一个 5 秒定时器迟早补上；已改为限时断言（点进页面后 1 秒内必须已有请求，且反复进出 3 次以避开定时器相位），之后真实代码连跑 3 次通过、M4 连跑 2 次变红。其间发现这条限时断言本身写成“恰好 +1”会因 5 秒定时器恰在此 1 秒内触发而偶发失败，已改为“至少 +1”（离开后“请求数不变”仍是精确断言）。

**未覆盖/留给后续：** 逐行核对读数的浏览器版（值与它自己的源时间戳在页面上成对）、截图与证据（D44f）；真实协议、真实服务器上的页面验收（D44f）；窄屏不做（用户 2026-10-12 指示）。

### D44f 协议、浏览器与交付证据

D44f｜基线 `dc84fb2` 之上的工作区（D44a–D44e 之后，未提交）｜**新** `tests/test_comm_protocols.py`（16 项：真实本地 Modbus/OPC UA 服务器与受控时钟）；**改** `tests/test_web_comm.py`（+1 项逐行一致性；若设了 `HTSG_EVIDENCE_DIR` 则存通信页截图）、`htsg/static/app.js`（一处措辞：首次有效通信等行的“最后有效通信”写“此前从未有过有效通信”，原“从未通信”易被读成当前状态）；**证据** 新 `docs/evidence/d44/`（24 组、48 个文件：Capstone G 的 13 张场景截图、七页逐页截图 `desktop-1…7`、通信页 `comm-counters`/`comm-log`/`comm-log-empty`/`selfcheck-counters`，每张配浏览器控制台日志，**全部为空＝没有任何 console 输出**）；OpenAPI 已在 D44d 重新生成并有用例核对。旧的 `docs/evidence/capstone-g/` 保留为历史，其页面截图仍带原型序号，不再引用。

**验收覆盖（方案 D44f 逐条）：**
- **S1/P2 的 0/1/5：** 启动从未有代次计 0；一次断连后持续重试计 1；每个“连上即失败”的新代次再进入重新同步加 1；换代不清零；60 秒清零与各种打断——`test_resync_health.py`（两协议、owner 层）；真实栈上：Modbus 真实服务器 `delay` 超过请求超时、连上后三轮超时、5 个周期 → `resync_count=5`、`conn_gen=5`、`connect_failures=0`、日志里没有 CONNECT 尝试条目、5 条各 3 次的 `POLL_TIMEOUT`（不把轮次失败记成 CONNECT 失败）；OPC UA 真实服务器、点表含服务器没有的节点 → 连上后订阅被拒 5 轮：**一条** SUBSCRIBE 聚合次数 5、`conn_gen` 空、`resync_count=5`、`conn_gen=5`、`connect_failures=0`（`test_comm_facts.py`）。
- **E5：** 无代次/重建后等待起点未到不判 `SILENCE`、初值先于等待事件到达且通信时刻不被覆盖（`test_resync_health.py`）；**两个时点分开**（真实 OPC UA 服务器，拖住 `create_items`）：CreateSubscription 已返回时 owner 已记下等待起点（`wait_mono` 非空、状态 WAITING），而上一轮的 SUBSCRIBE 失败聚合仍是“进行中”，`open_subscription` 正常返回后才以 `SUBSCRIBE_OK` 结束（`test_comm_protocols.py`）。**E8：** 慢归档初始化复现（`test_runtime.py`）。
- **R1：** 首次从未连接 5 次失败 → `connect_failures=5`、`resync_count=0`；一次断连后 5 次已完成的建连失败 → `5` 与 `1`；公开 `connect()` 成功即清零、其后订阅失败仍为 0；无尝试/重启为 0；`False`/异常/应用超时各计 1；SUBSCRIBE、POLL、取消不计——Modbus（真实服务器，**5/0 用真实关闭端口、5/1 用真实断开**）与 OPC UA（真实服务器）两协议各有用例；**诊断队列满、日志被挤出，计数不变**（`Ingress(diag_capacity=0)`：5 次失败全被诊断入口拒收，`connect_failures` 仍是 5、日志为空、`diag_lost=5`、无 APP_LOSS、无重新同步）；`/api/health` 的计数是采集器属性的原样复制；两页数字一致（`test_web_comm.py`）。
- **S2/P1：** CONNECT/SUBSCRIBE 分别聚合、换代与 RESYNC 与另一阶段事件不截断、原因改变结束旧条、对应阶段成功才结束、CONNECT 成功不结束 SUBSCRIBE（`test_comm_log.py`、`test_comm_facts.py`）；`EXCEPTION→OK→EXCEPTION` 两条（**真实 Modbus 设备**以 `DEVICE_FAILURE` 应答，协议码进原因码 `EXCEPTION_CODE_4`；中间一轮应答正常后第一条以 `SUCCESS` 结束），进行中/已结束，最近失败与结束时刻分开，原因变化不称恢复；未告警的恢复（真实 Modbus：服务器停约 5 秒再回来，在 10 秒告警之内，记 `COMM_RECOVERED` 带单调钟时长、无 `COMM_ALARM`）与首个保活（库边界注入：首个发布只有保活 → `FIRST_COMM`）；Bad（真实 OPC UA 服务器首值即 Bad）、Uncertain（库边界注入）读数都算通信、值不被置为 Good。
- **500 条先进先出：** 用持续更新的旧聚合条与其他事件填满 500 条，再来一条，被挤出的恰是那条旧聚合（更新不移位）；淘汰后活跃索引清除，同键下一次失败新建条、首次时刻只覆盖保留的跨度（`test_comm_log.py`）；500/501 条、长异常、合并、重启清空、诊断入口满（不触发 APP_LOSS/C1、不占批次容量）同在该文件。
- **S3：** API 层 25 次连续推送间隔读取、每行取自同一条观测（`test_health_communication.py`）；**浏览器层**：以每秒约 3 次的速度推送新观测（值 500+k、质量交替 GOOD/UNCERTAIN、源时间戳 1.7e9+k），页面记录该行每一个出现过的状态（`MutationObserver`），**每个状态的值、质量、接收时刻、源时间戳都属于同一个 k**；变异 M1（把实时值补丁进读数表）使它变红（补丁后的行显示新值配旧源时间戳“未知”）。轮询之间表不被实时流改动、其他页面实时更新照常（`test_web_comm.py`）。
- **S4：** 真实 pymodbus：**端口无人监听时**，库自己的超时先到则 `connect()` 返回 `False`，记“连接失败，库未提供原因”（原因码 `LIBRARY_NO_REASON`）；我们的 0.8 秒先到（默认两个超时相等）则记“建连调用0.8秒内未返回”（`APP_CONNECT_TIMEOUT`）——两种都用真实关闭端口验证，没有一处猜成“拒绝”或“设备断电”；含凭据 URL/口令/令牌的异常详情脱敏、配置的用户名与口令按实际值替换（`test_comm_log.py`、`test_comm_facts.py`）。
- **受控时钟：** 源时间戳恒定一小时（含 1970 年与远未来的时间戳）→ 状态不变、无失效/告警/重新同步，行里原样显示；墙钟被回拨一小时 → 日志仍按 owner 序、跨度用单调钟；归档缺口、时钟跳变、水位**不是**日志条目（它们确实发生了，见用例前半）。
- **七页、权限、活动页轮询、既有自检/历史回归：** 七页逐页切换与标题（`test_web_system.py`、`test_capstone_g.py`）；viewer、admin 都能读、未登录 401（`test_health_communication.py`、`test_web_comm.py`）；全部既有测试文件整体重跑（见下）。**复验：** Capstone C（真实 Modbus＋200 项 OPC UA）、Capstone G（15 项）、D45 接线（`test_runtime.py`，真实数据库）、D48 烟测（`test_smoke.py`）均在下列分段运行里通过。

**命令与结果：** `PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest <文件> -v -p no:cacheprovider --basetemp=<会话临时目录> -o faulthandler_timeout=120`。全部 `tests/test_*.py` 分两段（A：失步/源有效性/日志/Capstone B、C、E、F/发布/实时流/协议/运行时/抗故障/测量/烟测等 25 个文件；B：其余 39 个）：**A 297 通过 / 329.77 秒，B 334 通过 / 352.88 秒，合计 631 通过 / 0 失败 / 0 跳过**（= 基线 491 + 新增 140）。其后只有两处小改，各自复跑：D44f 的“诊断队列满不丢计数”1 项通过；措辞改动后 `test_web_comm`＋`test_web_system` 19 项通过（99.92 秒）；`test_capstone_g` 15 项通过（证据重拍，78.96 秒）。所以现状共 **632 项**，**但从未作为一次连续运行跑过——这仍是分段运行，不据此宣称整套连续通过。** `test_comm_protocols.py` 另连跑三遍均 15/15 通过（当时 15 项；那一遍里的两处失败见下，已改正）。`ruff check htsg tests scripts` 通过；`node --check` 通过；`git diff --check` 只有 CRLF 提示。

**过程中发现并处理的事：** ① 首次运行 2 项失败都是**我的用例问题**：停车后采集器刚报的诊断事实还在入口队列里，断言前要先排空；真实 asyncua 服务器发布 Bad 状态时**不带数值**，用例不该要求值还是原来的 12.5（行里值为空，保持“缺失不补零”）。② **真实 pymodbus 在这台 Windows 上**，端口无人监听时 `connect()` 在 0.8 秒内不返回（我推测是 Windows 对被拒绝的连接会重试约 1 秒，未验证），所以默认配置下 Modbus 建连失败记的是“应用超时”而不是“库未提供原因”；库的超时（取请求超时）先到时才返回 `False`。两种在日志里都如实区分。这不影响计数。③ **全量回归时一次挂住有明确原因：机器在夜里重启，开发库实例没有起来**（执行记录第 3 节首句早就写了开机后默认不启动），`test_capstone_e` 的数据库用例因此卡住；用 `scripts/bootstrap_db.py start` 起库后整段重跑通过。这与 pymodbus 取消丢失问题无关。④ 前一天 D44c 回归里 `test_capstone_c` 首个用例的那次挂住（库当时是起着的）**仍未查明、未复现**，同第 2 节所记。

**未覆盖/未验收（如实）：** 没有真实 Acheron、真实 OPC UA 网关、独立卷、现场长稳的证据；一年容量按终版口径仍不通过；整套连续运行仍没有通过记录。**设计侧文档里的状态陈述我没有动**：终版第 15 节表格与 15.1 开头仍写“设计已采纳、未实现”，方案头部与第 12 节仍写“尚未实施代码”，核验文件同——它们是用户维护的设计文件，终版的哈希写在方案头，回写会使其改变，请你决定是否回写、怎么写。

### R01 恢复可靠取消（Q01）

R01｜基线 `946cb5d` 之上的工作区（本轮第一批，未提交）｜**改** `htsg/modbus.py`、`htsg/runtime.py`；**新** `tests/test_cancellation.py`（位于 `.gitignore` 的 `tests/` 下，提交时须 `git add -f`，用户已授权）｜**验收** 新用例 10 项＋原有 `test_modbus_acquisition.py`、`test_runtime.py`、`test_capstone_c.py`，命令见下｜**结果** 新用例改前 4 失败 / 6 通过，改后 10 通过；原有三套件连同新文件在开发库重启后 **31 通过 / 0 失败 / 0 跳过**；隔夜前一次运行 20 通过 / 1 失败，失败的是一条既有用例的间歇失败，**待用户裁定**（见下）｜**证据位置** 新用例本身（可重跑）；运行输出和探针脚本在会话临时目录，未入库｜**未覆盖** 见末段。

**改动（出处：细则 3.2、整改方案 R01、第2.3节决策5）：**
- `htsg/modbus.py` `poll`：`except (ModbusIOException, TimeoutError)` 一支开头加一条判据——`asyncio.current_task().cancelling() > 0` 时 `raise asyncio.CancelledError from error`；否则照旧记 TIMEOUT 并继续。判据只有“本任务是否有未处理的取消请求”这一条，异常的 `__cause__` 不作条件，不匹配文本。净增 2 行。
- `htsg/runtime.py` `_done`：`self.running` 为真时，任务以 cancelled 结束也记 `health["fatal"] = "<任务名>: CANCELLED"` 并置 `failed`；`running` 为假（正常停机）时行为不变。
- **没有做**（细则 3.2 第4条）：没给 `_end` 加超时，没有循环重复 cancel 或固定 sleep，没动 `ModbusAcquirer.run` 的重连与退避，没动 `poll` 的整轮定性和 `session` 的连续超时计数，没动 UA 侧；`config/*.json` 与 `Timing`/`Limits`/`Storage` 默认值不变；没改 `.venv` 里的任何文件。

**新用例（`tests/test_cancellation.py`，对应细则 3.3；每个用例的 docstring 写明保护的是 Q01 或终版 §9、§11 哪条）：**

| 细则 | 用例 | 改前 | 改后 |
| --- | --- | --- | --- |
| 3.3.1 | `test_one_cancel_while_the_peer_withholds_its_answer_ends_the_acquirer_and_closes_its_client`：真实 `AsyncModbusTcpClient` 加真实 pymodbus 服务端（收到请求后用事件通知、暂扣应答）；请求已到达后单次 `task.cancel()`，2 秒内结束、客户端已关闭、取消后入口上没有新调用（无 TIMEOUT 批次、`attempt_failed`、`source_down`） | **失败**：任务未结束 | 通过 |
| 3.3.2 对照 | `test_a_cancel_when_the_peer_answers_at_once_…`、`test_a_cancel_between_two_rounds_…`（节拍睡眠中）、`test_a_real_request_timeout_without_a_cancel_is_still_a_timeout_round_…`（真实请求超时：仍产出 TIMEOUT 批次、任务继续） | 通过 | 通过 |
| 3.3.3 反例 | `test_poll_records_a_timeout_round_for_an_error_that_only_looks_like_a_cancel`（`poll` 层）、`test_an_error_that_only_looks_like_a_cancel_does_not_end_the_acquirer`（任务层：`ModbusIOException(...) from CancelledError()` 但本任务没被取消，连续出 TIMEOUT 批次并触发 TIMEOUTS 重连，任务不结束） | 通过 | 通过 |
| 3.3.4 | `test_a_core_task_that_ends_cancelled_while_the_runtime_runs_is_a_failure`（`fatal == "acquire mb: CANCELLED"` 且 `failed` 置位） | **失败**：3 秒内 `failed` 未置位 | 通过 |
| 3.3.4 | `test_the_cancels_of_a_normal_stop_are_no_failure`（正常 `stop()` 期间的取消不报 fatal） | 通过 | 通过 |
| 3.3.5 | `test_the_full_scale_rig_ends_ten_times_…`、`test_the_simulation_chain_ends_ten_times_…`（`FieldModbus.delay=0.6`，请求到达后立即收尾，各 10 次；收尾设 10 秒上限，超限即判失败并强制收掉任务，不让失败变成整个运行挂住） | **失败**：`Rig.__aexit__`、`Chain.__aexit__` 10 秒内未结束 | 通过 |

**命令与结果：** 命令形式均为 `PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest <文件> -v --tb=short -p no:cacheprovider --basetemp=<会话临时目录> -o faulthandler_timeout=120`，输出重定向到文件。
- 改前（`htsg/` 未动，只有新文件）：`tests/test_cancellation.py` **4 失败 / 6 通过 / 0 跳过，49.57 秒**。4 项失败正是细则 3.3.6 要求“修改前失败”的那几项（3.3.1、3.3.4 运行期取消报 fatal、3.3.5 两项），失败原因与预期一致；6 项对照、反例和正常停机在改前改后都成立。
- 改后：同一命令 **10 通过 / 0 失败 / 0 跳过，42.69 秒**。其后把“真实请求超时”对照的对端从“晚0.6秒应答”换成“永不应答”（原因见下一条），连续再跑 3 遍均 10/10（42.28、42.36、42.46 秒）。
- 原有套件（细则 3.3.6）：`tests/test_modbus_acquisition.py tests/test_runtime.py tests/test_capstone_c.py`，隔夜前一次 **20 通过 / 1 失败 / 0 跳过，80.84 秒**（失败项即下一条）；开发库重启后与新文件一并重跑 **31 通过 / 0 失败 / 0 跳过，113.21 秒**（`test_modbus_acquisition` 11、`test_runtime` 6、`test_capstone_c` 4、`test_cancellation` 10）。
- `ruff check htsg tests scripts` 通过；`git diff --check` 通过（只有 LF→CRLF 提示）。

**待用户裁定（细则和整改方案都没覆盖，我没有改动）：既有用例 `test_modbus_acquisition.py::test_one_timeout_keeps_the_connection_three_in_a_row_reconnect` 间歇失败。** 它在第二阶段（`server.delay = 0.6` 后等 `gen == 2`，即连续3轮超时触发重连）会在第217行 `await bench.until(lambda: bench.acquirer.gen == 2)` 超过10秒而断言失败。
- **频次（依赖机器时序状态，两个时段差别很大）：** 隔夜前、改后的工作区单独重跑该用例 9 次，4 次失败；同序列加日志的探针 6 次，4 次失败。同一时段基线副本（`git archive 946cb5d` 解到临时目录，已核对 `htsg.__file__` 指向副本）同一用例 6 次：3 次 1.3 秒通过，3 次输出 60 秒 faulthandler 堆栈后 120 秒内未结束——堆栈只显示主线程在 `run_until_complete`，任务级位置没取到，所以这 3 次是“函数体失败后收尾挂住”还是“函数体通过后收尾挂住”**没有逐次证实**。开发库重启后，函数体结果与“收尾是否在15秒内结束”分开记录的探针，基线副本与改后工作区交错各 30 次：基线函数体失败 1/30、收尾未结束 2/30（另一批 6 次里收尾未结束 1 次，函数体 6/6 通过）；改后函数体失败 3/30、收尾未结束 0/30。函数体失败率两边差异不显著，且都比隔夜前低得多。
- **原因（已定位）：** pymodbus 3.15.0 服务端 `server/requesthandler.py` 第117行在延时应答之后用 `self.last_pdu.transaction_id` 给响应打事务号，而 `last_pdu` 已被后到的请求覆盖。`FieldModbus.delay=0.6` 大于轮间隔（约0.17秒）时，旧请求的迟到应答带着最新请求的事务号被客户端当成当前请求的正确应答（探针时间线：服务端按0.6秒延时发出的应答，在10毫秒内被客户端记为一轮 OK），于是“连续3轮超时”凑不齐。这是测试服务端的行为，不是被测代码；R01 的新分支在无取消时不会走到（失败探针的45轮里每轮结束时 `cancelling()` 实测均为0，任务始终存活），改动前后该用例的函数体行为一致。
- **验证：** 在临时副本（不在工作区）里把“晚0.6秒应答”改成“永不应答”（`delay=30`），同一序列连跑10次全部通过（隔夜前那个时段，原写法约45%失败）。
- **未采取：** 按细则1.2第5条没有改这条既有用例。建议的最小改动：只改这一条用例的对端行为（永不应答，不再晚应答），不碰 `htsg/`；请用户裁定是否在本轮做。裁定前，“原有用例全过”只能写作：重启后一次完整运行全过，但该用例按上述机制间歇失败。
- **对 R05 的提示：** 多从站夹具里“持续沉默”不要用比轮间隔长的 `delay`，用暂扣（永不应答）；否则迟到应答会被当成当前请求的应答，使“持续沉默”变成时有时无。

**过程中的观察（不处理，仅记录）：** ① `ModbusAcquirer.poll` 里第二个 `except (ModbusIOException, TimeoutError)` 的 `TimeoutError` 被前一个 `except (ConnectionException, OSError)` 先接住（`TimeoutError` 是 `OSError` 的子类，已用 `issubclass` 核实），所以这一支实际只会接到 `ModbusIOException`；pymodbus 3.15.0 的请求超时本身是 `ModbusIOException`，不受影响。未改。② 基线上 `Bench.__aexit__` 收尾挂住是真实存在的：基线副本探针里函数体通过后 `__aexit__` 15 秒内未结束（该次之后重复取消才结束），改后 30 次未再出现。

**未覆盖/留给后续：** 没有做变异验证（用来证明对照和反例确实能抓住“只看因果链”“遇到 `ModbusIOException` 就一律抛取消”这两种错误改法的是用例的设计，不是变异实验）；对端沉默时取消被吞的比例（复核实测12次里11次）未按12次复测，用例是确定性复现；G1 的“取消与生命周期检查在有限等待内结束”只覆盖了采集任务取消和 `Rig`/`Chain` 收尾，`Runtime.stop` 的整体期限属 R04；整套连续运行（G2）未做。

### R02 详情脱敏与入队边界（Q04、Q06）

R02｜基线 `946cb5d` 之上的工作区（R01 之后，未提交）｜**改** `htsg/commlog.py`、`htsg/records.py`、`htsg/modbus.py`、`htsg/owner.py`、`htsg/static/app.js`；测试 **新** `tests/test_detail_bounds.py`（26 项，`git add -f`），**改** `tests/test_web_comm.py`（+1 项）、`tests/test_comm_log.py`（见下，逐条）｜**验收** 命令见下｜**结果** 新用例改前 17 失败 / 10 通过，改后全部通过；细则 4.3 第6条列出的六个文件合计 **142 通过 / 1 xfail / 0 失败 / 0 跳过**（140.00 秒）；其中 1 项 xfail 是**待用户裁定**的一条旧参数（见下）｜**证据位置** 新用例本身（可重跑）；运行输出与探针在会话临时目录，未入库｜**未覆盖** 见末段。

**改动（出处：细则 4.1、4.2、整改方案 R02 / 第2.3节决策6）：**
- `commlog.py`：`_USERINFO` 改为从 `scheme://` 到其后**最近一个 `@`**，中间内容（空白也算）一律遮为 `***`，`@` 之后照常保留，没有 `@` 时不遮；`clean_detail` 改为——原文超过 4096 字符整段省略，返回 `(None, True)`，不做任何部分保留；4096 以内先完整脱敏，脱敏后超过 512 再截到 512 并标截断。删除“预截断后退回最后一个词”的逻辑。4096、512 沿用现行常量（`SCRUB_INPUT_LIMIT`、`DETAIL_LIMIT`），没有改成配置项，`_BEARER`、`_SECRET` 未动。
- `records.py`：`RequestResult` 在 `detail` 之后加 `truncated: bool = False`。
- `modbus.py` `poll`：构造 `RequestResult` 前调用 `clean_detail(str(error))`，把清理后的文本和截断标志存入；原始长文本不再离开这一行。
- `owner.py` `_log_requests`：二次清理保留，截断标志取 `q.truncated or 本次清理的标志`，不会把 True 覆盖成 False。
- `static/app.js` `commEntry`：详情为空且 `detail_truncated` 为真时，显示“详情已省略（过长）”（细则约定 H）；请求仍保留自己的“请求 <范围>”一行。
- `ingress.py` 没改：`attempt_failed` 本来就在入口清理，规则随 `clean_detail` 自动更新。`ua.py` 没改：`_plain` 替换环境变量口令的行为不变（`test_comm_facts.py` 相关用例全过）。`docs/openapi.json` 没改：没有新增 API 字段，`test_health_communication.py::test_the_checked_in_openapi_description_matches_the_application` 通过。

**新用例（`tests/test_detail_bounds.py` 26 项，`tests/test_web_comm.py` 1 项；每个用例的 docstring 写明保护的是 Q04、Q06 或终版 §15.1 的哪一条）：**
- **Q04 合成串 A–G**（按复核意见书描述构造，秘密占位 `SYNTHSECRET` 重复）：改前 A、C、D 的输出含秘密前缀，B、E、G、F 不含——与复核意见书的探针表完全一致；改后七个输出都不含任何 `SYNTH` 片段，A–E、G 为 `(None, True)`，F（短 URL）为 `opc.tcp://***@localhost/`。
- **边界：** 4096 字符正常处理（`("y"*512, True)`），4097 字符整段省略；短文本 `opc.tcp://user:SYNTH SECRET@localhost/`（userinfo 带空格）改前泄漏、改后遮挡；`connect opc.tcp://host:4840 failed; mail admin@example.com` → `connect opc.tcp://***@example.com`，用例 docstring 写明“多遮是已确认的取舍”；两个 URL 各遮到各自最近的 `@`。
- **必须原样保留：** `opc.tcp://127.0.0.1:4840/`、`127.0.0.1:502`、pymodbus 的常规超时消息、`OSError` 的 `repr` 短文本、一条 0.8 秒连接消息；另有**真实 pymodbus** 对一台不应答的真实服务端发请求，`poll` 结果里的详情就是库的原文 `Modbus Error: [Input/Output] No response received after 0 retries, continue with next request`，`truncated=False`。
- **Q06：** 假客户端抛 5,000,000 字符的 `ModbusIOException`：`poll` 返回的批次里、`Ingress.queue` 未排空时、`owner.commlog.snapshot()`、`/api/health` 响应里，详情均为 `None`、截断标志为真；另一条用 3000 字符且带凭据的文本，`poll` 结果里已是脱敏后截到 512、标志为真；owner 层的标志不被覆盖。
- **页面：** 真实 Chrome，给日志注入一条详情被省略的 Modbus 请求失败和一条被省略的订阅尝试失败，页面都显示“详情已省略（过长）”，请求那条同时保留“请求 holding/1/0+16”，不出现“无详情”，无控制台错误。改前该用例失败。

**命令与结果：** 命令形式同 R01。
- 改前（`htsg/` 未改）：`tests/test_detail_bounds.py` 加上面那条页面用例：**17 失败 / 10 通过**（5.13 秒）。失败原因：A/C/D 泄漏、4097 未省略、`RequestResult` 没有 `truncated`、页面无说明等；通过的是 B/E/G/F、各 URL 遮到各自 `@`、必须原样保留的 5 条。
- 改后：`tests/test_comm_log.py tests/test_comm_facts.py tests/test_comm_protocols.py tests/test_web_comm.py tests/test_health_communication.py tests/test_detail_bounds.py`：**142 通过 / 1 xfail / 0 失败 / 0 跳过，140.00 秒**（`test_comm_log` 48＋1 xfail、`test_comm_facts` 25、`test_comm_protocols` 16、`test_web_comm` 13、`test_health_communication` 14、`test_detail_bounds` 26）。
- `ruff check htsg tests scripts` 通过；`node --check htsg/static/app.js` 通过；`git diff --check` 通过。

**对原有用例的修改（细则 4.3 第6条；`tests/test_comm_log.py`，逐条）：**
1. `test_the_ingress_cleans_and_cuts_the_text_before_it_queues_it`：输入 100,000 字符，原断言“长度等于 512 且已截断”→ 改为“`None` 且已截断”；另加一次 3000 字符输入，保留原来的“切到 512”预期。理由：新规则——超过 4096 字符整段省略，不再截到 512。
2. `test_cleaning_a_huge_or_hostile_message_costs_milliseconds_not_minutes`（4 个参数）：断言由“长度不超过 512 且已截断”改为“`None` 且已截断”。理由：这 4 个输入都超过 4096，现在不再进入正则。**原意（对恶意长文本的清理耗时有上限）**移到新增的 `test_cleaning_a_hostile_message_of_the_largest_size_that_is_still_cleaned_costs_milliseconds`（4 个参数，输入都在 4096 以内，含无 `@` 的 1024 个 `a://`，这是新 URL 规则最坏的情形），实测每例约10毫秒以内。
3. `test_long_text_is_cut_to_the_limit_and_says_so_and_short_text_is_not_marked`：5000 字符改为 4000 字符。理由：5000 超过 4096，现在整段省略；5000 的情形由新用例 `test_the_limit_is_4096_characters_of_raw_text_to_the_character` 覆盖。
4. `test_credentials_never_survive_the_cleaning` 的参数 `cannot open opc.tcp://u:p@ss@host/ca`（秘密 `ss@`）：标成 `xfail(strict=True)`，**不是**“依赖旧‘退回最后一个词’行为”的那类断言，见下一条。

**待用户裁定（细则没覆盖，我没有替你选口径）：口令里含未转义 `@` 时，“最近一个 `@`”规则只遮到第一个 `@`。** `opc.tcp://u:p@ss@host/ca` 现在输出 `opc.tcp://***@ss@host/ca`：口令在第一个 `@` 之后的部分（`ss`）没被遮；旧规则（贪婪到下一个 `/` 或空白之前的最后一个 `@`）会整段遮成 `opc.tcp://***@host/ca`，原来的参数用例就是为此写的。按 RFC 3986，userinfo 里的 `@` 应当百分号编码，这样的 URL 本身不规范，但按你确认的第2.3节决策6字面实现就是这个结果。可选：(a) 接受字面规则，删掉该参数、另写一条用例写明这一点；(b) 在最近的 `@` 之后，若紧跟的非空白、非 `/`、非 `@` 的一段又以 `@` 结尾，则连这一段也遮（正则 `[^@]*@(?:[^\s/@]*@)*`，仍不解析 URL；只对 `x@y@z` 这类文本比字面规则多遮 `y@`），更符合决策6说的“诊断损失小于泄漏风险”。我倾向 (b)，但这是你的口径。该参数现在是严格 xfail：选 (b) 并实现后它会变成 XPASS(strict) 而失败，提示去掉标记。

**过程中的观察（不处理，仅记录）：** `CommLog.attempt_failed`、`round_failed` 在聚合已存在时只在新详情**不为 `None`** 时才更新文本和标志（`if detail is not None`），所以一条以“被截断/被省略”开头的聚合，之后再遇到一次被省略的失败，不会把标志改回来，也不会因省略而清空文本；这是既有行为，R02 没有改它。

**未覆盖/留给后续：** 没有做变异验证；5,000,000 字符的路径用假客户端覆盖（真实 pymodbus 自己的异常文本很短，也有真实对照）；`ModbusAcquirer` 以外的来源（UA 侧 `_plain`）按细则只核对未变，没有新增用例；页面只核对了“省略”这一种新文案，其余渲染沿用既有用例。

### R03a 崩溃收尾与跨库收尾只用本段证据（Q02）

R03a｜基线 `946cb5d` 之上的工作区（R02 之后，未提交）｜**改** `htsg/archive.py`、`htsg/archive_router.py`、`htsg/history.py`；测试 **新** `tests/test_exit_evidence.py`（18 项，`git add -f`），**改** `tests/test_run_segments.py`、`tests/test_capstone_d.py`（各一条断言，见下）｜**验收** 命令见下｜**结果** 新用例改前 13 失败 / 4 通过（当时 17 项）＋第18项在基线副本上失败，改后 18/18；新用例连同细则 5.2 第6条列出的原有套件及相关套件共 **107 通过 / 0 失败 / 0 跳过，172.35 秒**｜**证据位置** 新用例本身（可重跑，真实数据库）；运行输出在会话临时目录，未入库｜**未覆盖** 见末段。

**改动（出处：细则 5.1、整改方案 R03a / 第2.3节决策2、7）：**
- `archive.py`：新增 `_segment_evidence`（某段的证据＝该段最后一条**有效观测**〔`quality='GOOD'`、有值、非极值，含状态重申，取重申时刻〕与该段**水位**中较晚者）和只读的 `unfinished_facts`（本库里哪些 run、哪些段已有终点，以及本库未结束的 run 的各段证据）。`reconcile_unfinished_runs`：run 的 `ended_wall`、`last_evidence_wall` 仍取全 run 最大证据（SQL 不变，只是运行元数据）；每个 `end_wall IS NULL AND start_wall IS NOT NULL` 的段，`end_wall` 改为**该段自己的证据**，既无有效观测也无水位时取该段 `start_wall`（细则约定 A），`end_reason='EXIT_TRUNCATED'` 不变；新增可选参数 `ended_runs`、`ended_segments`、`evidence`（路由器收集的各库事实）：任一库已结束的 run 不收尾，任一库已有终点的段不截断，段证据取各库中该段的最大值。不计入证据（用例逐类覆盖）：`state_event` 的任何记录、`result`、`fault_record`、BAD／极值／缺失（`quality` 不是 GOOD）的观测。
- `archive_router.py`：`initialize` 先初始化全部库，再 `_close_unfinished_runs`：从所有可读库收集事实；**任一已绑定库不可读时不收尾任何运行**，`health["reconcile"] = "PENDING_UNREADABLE:<库名>"`，留待下次启动（细则约定 B；运行期间不重试）；全部可读时对每个库收尾，不向其他库复制 RUN_END 组、不改其他库的 run 行。成功时清掉旧的 `reconcile` 键。`/api/health` 的 `archive.health` 本来是开放对象，不需要改 OpenAPI。
- `history.py`：`_merge_segments`——同一段在各库的记录里，真实终点优于 `EXIT_TRUNCATED`，同类里取 `end_wall` 最大的那一条，`end_reason` 取自同一条；`assemble`——`end_reason='EXIT_TRUNCATED'` 的段，最后一条记录的保持终点＝`max(该行时刻, min(段终点, 水位))`，无水位时＝该行自身时刻（只保留实存点），非最后一条照旧保持到下一条，缺口规则照旧；`coverage_end`——`EXIT_TRUNCATED` 段只取该段水位，无水位不贡献。其他 `end_reason` 的段全部不变；库里已有的数据不迁移、不改写。

**新用例（`tests/test_exit_evidence.py` 18 项，真实 PostgreSQL，用 `archive_db_factory`；docstring 写明保护的是 Q02 / 终版 §5，对应细则 5.2）：**

| 细则 5.2 | 用例要点 |
| --- | --- |
| 1 两源不同水位 | 源A观测1001、水位1002；源B观测与水位到1010，另有1015的状态记录；不 shutdown，重启收尾：A段 `end_wall=1002`、B段 1010，run 的 `ended_wall` 仍是全 run 最大证据 1015；查询A最后一条保持到1002，`coverage_end`＝{A:1002, B:1010}，不被B抬高 |
| 2 无水位 | 只有观测1001：段终点1001；查询最后一条保持终点＝自身时刻，`coverage_end` 为 `None` |
| 3 水位后的实存点 | 水位1005，其后观测1007、1008：两行都可见；1007保持到1008，1008不向后保持；`coverage_end`＝1005 |
| 证据种类 | 状态重申按重申时刻计；BAD、极值、`MISSING_*` 观测和状态事件都不计，这样的段终点＝起点（细则约定 A）；坏值落在终点之后时照样显示、保持终点不早于自身时刻（第18项） |
| 4 既有错误数据 | 直接写入按旧规则截断的段（`end_wall=1020`、`EXIT_TRUNCATED`）：有水位时保持终点按水位限制，无水位时＝自身时刻；再次启动后库里的 run 行和段行原样不变（参数化两例） |
| 5 跨库 | ①运行在主库开始、续在应急库、正常停止（SEG_END 与 RUN_END 只在应急库）后重启：主库 run 行不被改（不是 `ABNORMAL_EXIT`）、主库那段不被截断，`*_closed_runs` 为空，合并后段终点与结束原因同出一条记录（1011、`SHUTDOWN`）；②同样的运行但崩溃：两库各自按本库证据记 `ABNORMAL_EXIT`，该段按**两库合并**的段证据截在1011，不被主库较早的1002截短；③RUN_END 所在库在重启时不可读：不收尾，`health` 为 `PENDING_UNREADABLE:emergency`，查询 `partial`、各源 `coverage_end` 为 `None`，该库恢复后再次启动，仍不误判、合并结果正确 |
| 段合并 | `_merge_segments` 7 个参数：真实终点胜过截断（两种读取顺序）、真实终点之间取最晚且带它自己的原因（两种顺序）、两条截断取最晚、只有起点行与终点行合并（两种顺序） |

**命令与结果：** 命令形式同 R01。
- 改前（`htsg/` 未动）：`tests/test_exit_evidence.py` 当时 17 项 **13 失败 / 4 通过**（6.52 秒），失败原因与预期一致（段终点取到全 run 最大证据、跨库主库被判异常退出、没有 `reconcile` 键、合并取了错误的原因）；通过的 4 项是与旧规则碰巧一致的合并参数。第18项（坏值尾部）是改后补的，用基线副本（`git archive 946cb5d`，已核对导入的是副本）补测：失败（段终点 1020，应为 1002）。
- 改后：新文件 **18/18 通过**；`tests/test_exit_evidence.py tests/test_run_segments.py tests/test_capstone_d.py tests/test_history_query.py tests/test_history_merge.py tests/test_capstone_f.py tests/test_history_api.py tests/test_resilience.py tests/test_storage_routing.py tests/test_archive_router.py`：**107 通过 / 0 失败 / 0 跳过，172.35 秒**（`test_exit_evidence` 18、`test_run_segments` 10、`test_capstone_d` 4、`test_history_query` 9、`test_history_merge` 8、`test_capstone_f` 9、`test_history_api` 19、`test_resilience` 4、`test_storage_routing` 20、`test_archive_router` 6）。
- `ruff check htsg tests scripts` 通过；`git diff --check` 通过。

**对原有用例的修改（细则 5.2 第6条）：** 原有套件没有一条因本批失败。两处“以全 run 最大证据作预期”的断言按细则改成本段证据，**改动前后数值相同**（两个场景都只有一个源、一个段，本段证据恰等于全 run 最大证据；实现改完、这两处断言还没改时，89 项原有用例已全过），所以改的是用例表达的意图，这两处自己区分不出新旧规则，区分靠新文件：
1. `test_run_segments.py::test_an_exit_without_a_stop_is_closed_at_the_last_persisted_evidence_and_nothing_later`：段的预期由 run 级 `expected`（含状态记录）改为 `segment_expected`＝本段有效观测时刻与水位的较晚者；run 级的断言不变。
2. `test_capstone_d.py::test_killing_the_process_before_inside_or_after_a_commit_…`（3个参数）：`segment["end_wall"] == old["ended_wall"]` 改为 `== segment_evidence`（崩溃前在库里查出的第1段的有效观测与水位较晚者）；run 级的断言不变。

**实现取值与观察（需知悉，均未超出细则；若口径不同请裁定）：**
1. **跨库的段证据取各库中该段的最大值。** 细则只写“本段证据”；一段的数据可能跨库，若只看本库证据，续在应急库里的观测会落在主库那一份段终点之后。run 级的 `ended_wall` 仍是每个库自己的全 run 最大证据，没变。
2. **旧结构被拒（`REFUSED`）的库也按“不可读”处理**，会让整个收尾推迟；一个长期配置错误的绑定库因此会使崩溃遗留的运行一直不被收尾，直到它修好。这是细则“任一已绑定库不可读则不收尾”的保守读法。多个不可读库名用逗号连接。
3. **晚于段终点的最后一行**（坏值不算证据，所以可能出现）：细则字面公式 `min(段终点, max(该行时刻, 水位))` 会得到早于该行自身时刻的保持终点，我夹到不早于自身时刻（零长度保持，行仍可见）。只在这个角落与字面公式不同。
4. **耗时没有在大库上测。** 本段证据的查询与原有的 run 级 SQL 同量级（扫崩溃运行的观测），受 `db_statement_timeout`（默认3秒）约束；超时按“不可读”处理，即推迟收尾而不是误收尾。

**过程中发生的事：** 一轮后台验收我用 `TaskStop` 中止后，外层 shell 被杀而 pytest 子进程仍继续跑完（105 通过 / 1 失败），那条失败是 `test_resilience.py::test_a_hard_kill_at_any_moment…`——需要新起后端子进程的用例，子进程以 `3221225794`（`0xC0000142`，进程初始化失败）退出，时间点正是我杀外层 shell 的时刻；中止前后我改了 `history.py` 一处（观察3）并补了第18项，所以那一轮的数字作废，上面的 107 项是改完后重新完整跑的结果。中止时遗留的 3 个 `htsg_test_*` 临时库已由那个继续跑完的进程自己清掉，之后核对没有遗留。

**未覆盖/留给后续：** 没有做变异验证；没有在大库上测收尾耗时；运行期间 owner 侧的 `lost_seg`、段/缺口一致性（Q07）属 R03b，本批没动 `owner.py`；停机时的收尾属 R04。

### R05 部分请求语义的回归用例（Q05）

R05｜基线 `946cb5d` 之上的工作区（R03a 之后，未提交）｜**改** `tests/protocol_support.py`（新增 `device_points`、`FieldModbusDevices`）；**新** `tests/test_partial_requests.py`（15 项，`git add -f`）；**未改 `htsg/` 任何文件**（细则 9.1）｜**验收** 命令见下｜**结果** 15 项全部通过；连同依赖 `protocol_support` 的 `test_modbus_acquisition`、`test_resync_health`、`test_comm_facts`、`test_cancellation` 共 **89 通过 / 0 失败 / 0 跳过，139.79 秒**｜**证据位置** 新用例本身（可重跑）；运行输出在会话临时目录，未入库｜**未覆盖** 见末段。

**夹具（细则 9.1、细则约定 E）：** `FieldModbusDevices` 是**一台真实 pymodbus TCP 服务端上的多个从站**（默认设备号1、2，各8个 float32，寄存器0–15），每个从站的 `mode[设备号]` 可随时改成 `OK`（正常应答）、`SILENT`（不应答）、`EXCEPTION`（回协议异常，设备故障码4）；`device_points` 为每个设备造两个点，所以一轮就是“每个设备一个读请求”。**“沉默”是暂扣到服务端停止，永远不回**，不用“延时应答”：见 R01 条目，pymodbus 服务端给迟到的应答打的是最新请求的事务号，迟到应答会被当成当前请求的应答，沉默就会时有时无。客户端路径没有 mock：受控时钟的用例也走真实的 `ModbusAcquirer.poll`／`run`，只是客户端是脚本化的、时间戳由用例给。

**用例（`tests/test_partial_requests.py`，对应细则 9.2；docstring 写明保护的是 Q05、终版 §9、§11）：**

| 细则 9.2 | 受控时钟（脚本化客户端，合成时钟或快速节拍） | 真实服务端 |
| --- | --- | --- |
| 1 一个沉默、一个正常，超过60秒 | `test_partly_answered_rounds_keep_the_stable_spell_so_sixty_seconds_clear_the_resync_count`：起点三次重同步（计数3），其后75个整秒的轮次，每轮 `[OK, TIMEOUT]`、整轮 `OK`：计数在第261秒（稳定区间从第201秒的首个应答起算）才清零，之前一直是3；每秒都是 VALID、无告警；健康设备的点有效，沉默设备的点不可用、原因 `POLL_MISSING`；日志里只有一条持续中的 `POLL_TIMEOUT`（范围 `holding/2/0+4`，次数75），没有另一个请求的失败。对照 `test_rounds_without_any_valid_response_never_clear_the_resync_count`：首次应答之后整轮都无应答的74秒，计数始终是3 | `test_a_real_slave_that_never_answers_leaves_the_connection_alone_and_a_minute_of_it_clears_the_count`（**62.08秒**，默认时限：请求超时0.8秒、每秒一轮）：先断开一次链路得到 `resync_count=1`、第2代，再让第二个从站永不应答；每次采样 `consecutive_timeouts==0`、代次始终是2、健康点持续有效；计数在稳定区间满60秒后清零（断言区间：自首个应答起 60 秒到 64 秒内）；日志只有第2代里一条 ACTIVE 的 `POLL_TIMEOUT`（`holding/2/0+4`，次数≥55） |
| 2 两个都沉默连续3轮重连；中间有应答则清零 | `test_three_rounds_in_a_row_without_any_response_reconnect_once`（TIMEOUT×3 后重连一次，`RESYNC` 原因 `DOWN:TIMEOUTS`）；`test_an_answer_from_any_request_in_between_clears_the_count_…`（两轮全沉默＋一轮半应答，循环4次，从不重连，连续计数最高到2） | `test_real_slaves_an_answer_in_between_resets_the_count_…`（两轮全沉默，再一轮“一个应答”或“全应答”，循环3次，12轮不重连）；`test_real_slaves_that_all_stay_silent_cost_the_connection_in_the_third_round`（见下，如实固定） |
| 3 协议异常 | `test_a_device_exception_is_a_valid_response_no_timeout_and_no_alarm`（半数异常／全部异常各75个整秒：不是超时、无告警、同样在第261秒清零，日志只有 `POLL_EXCEPTION`，原因码 `EXCEPTION_CODE_4`）；`test_exception_answers_are_not_timeouts_so_they_never_reconnect` | `test_real_slaves_that_answer_with_an_exception_are_no_timeout_and_no_alarm`（两种，各8轮：不计超时、不重连、无告警，日志一条 ACTIVE 的 `POLL_EXCEPTION`） |
| 4 单请求现行配置不变 | `test_the_current_research_configuration_has_one_request_per_round_so_every_timeout_is_a_whole_round`：研究配置的 Modbus 点只有1个读请求；单设备脚本客户端，TIMEOUT×3 重连 | `test_a_real_single_slave_that_never_answers_is_a_whole_round_timeout_three_of_them_reconnect`（真实单从站，TIMEOUT×3 后重连，其后 OK） |
| 5 先受控、再真实 | 受控版本（上表左列）先写先跑，全部通过 | 真实版本（右列）随后写；开发库不涉及 |

**命令与结果：** 命令形式同 R01。`tests/test_partial_requests.py` 在没有 `-k` 时共 15 项：除一项外分两次先后跑（14 项 6.08 秒，那一项 62.39 秒），最后连同依赖它的套件一并重跑：`tests/test_partial_requests.py tests/test_modbus_acquisition.py tests/test_resync_health.py tests/test_comm_facts.py tests/test_cancellation.py` **89 通过 / 0 失败 / 0 跳过，139.79 秒**（15＋11＋28＋25＋10）。`ruff check htsg tests scripts` 通过。R05 只固定现行行为，没有“改前失败”这一步；这些用例没有做变异验证，不能说明它们能抓住行为被改回去的情形。

**过程中发现并如实固定的既有行为（需知悉；细则和方案都没有列，我没有处理）：** 第一次写“两个从站都沉默、连续3轮后重连”的真实版本时，我预期的轮次序列 `TIMEOUT, TIMEOUT, TIMEOUT` 实际是 **`TIMEOUT, TIMEOUT, CONNECTION`**。原因在库里：pymodbus 3.15.0 `transaction/transaction.py` 第232–237行，连续 `retries+3` 个请求无应答之后会自己关闭连接（`count_until_disconnect`，任何一个应答都会把它重置）；一轮有两个请求且都沉默时，第5个无应答请求落在第3轮，库先于采集器自己的“连续3轮超时”把连接关了，该轮其余请求因连接已关而报连接异常（这一步是我对库行为的推断，没有逐行追踪库代码；观察到的事实是该轮的分类为 `CONNECTION`）。结果仍是约3轮后重连一次（终版 §11 的实际效果成立），但**日志里的原因是 `DOWN:CONNECTION`，不是 `DOWN:TIMEOUTS`**；脚本化客户端没有库的计数，所以走到 `DOWN:TIMEOUTS`。现行研究配置每轮只有1个请求，库要连续5轮无应答才关，采集器的3轮规则先到，不受影响。一轮里有任何一个请求应答，库的计数就被重置，所以“部分请求超时”的情形不会触发它（上面62秒的用例就是证据）。`test_real_slaves_that_all_stay_silent_cost_the_connection_in_the_third_round` 把这一点固定下来。**是否需要把这种“全沉默”也统一成 `TIMEOUTS` 原因，是你的口径，细则 1.1 第4条不许改轮次语义，所以我没碰。**

**未覆盖/留给后续：** 真实版本的60秒用例让整套多出约一分钟；“受控时钟”指合成的 owner 时钟和快速节拍，真实版本用真实时钟；多请求形态只覆盖“每设备一个请求”（设备号不同），没有覆盖同一设备内寄存器类型不同、地址不连续、超过125个寄存器等其他分块；R07 验收第③项要用到的“某个请求持续超时”场景，夹具已具备。

### R07 Modbus 失败轮的按点失效记录（Q08）

R07｜基线 `946cb5d` 之上的工作区（R05 之后，未提交）｜**改** `htsg/records.py`（常量 `MISSING="MISSING_"`）、`htsg/owner.py`（`_accept_poll`、`_poll_failed`，新 `_missing`）、`htsg/history.py`（`input_basis` 一处）；测试 **新** `tests/test_failure_records.py`（14 项，`git add -f`），**改** `tests/test_partial_requests.py`（`Live` 夹具留下 owner 提交的组，供 R07 的真实线路用例读取）、`tests/test_input_basis.py`（一条断言，见下）｜**验收** 命令见下｜**结果** 新用例改前 13 项失败（首批10项 9 失败/1 通过，其余4项在基线副本上 4/4 失败），改后 14/14；新用例连同细则 7.2 列出的原有套件及 `test_partial_requests` 共 **92 通过 / 0 失败 / 0 跳过，102.71 秒**；另有一轮覆盖 web／capstone／runtime／smoke 的回归 164 项全过｜**证据位置** 新用例本身（可重跑，含真实线路用例）；运行输出在会话临时目录，未入库｜**未覆盖** 见末段。

**改动（出处：细则 7.1、整改方案 R07 / 第1.1节）：**
- **何时写**（`owner.py`）：`_accept_poll` 在本轮动手前记下 `PointState.good` 为真的点。整轮 `TIMEOUT`／`EXCEPTION`／`CONNECTION`（`_poll_failed`）：这些点各写一条，质量 `MISSING_TIMEOUT`／`MISSING_EXCEPTION`／`MISSING_CONNECTION`；整轮 `OK` 但某个本轮前有效的点不在本轮应答里（`POLL_MISSING`）：写一条 `MISSING_UNANSWERED`。**不写**：本轮前已无效的点（从未有过观测、上一条是 BAD／极值／失效记录）；失败持续时的后续轮次（点已经无效）；OPC UA 源（代码只在 Modbus 的 `_accept_poll` 里）；本轮自己有观测的点，含解码为 BAD 的点。
- **形态**（复用 `observation` 表，没加表、没加列；细则约定 C）：`value=NULL`、`status_code=NULL`、`extreme=false`、`origin=RECEIVED`；`received_wall/mono` 取本批的 `wall/mono`；`tick`、`poll_id` 取本轮值，`response_id=NULL`；`batch_seq` 取本轮值，`item_seq` 接在本轮已收观测之后连续编号，ID 走 `observation_id` 规则；与本轮 `POLL` 状态记录、派生量结果**同一个提交组**（也是同一个实时发布组）。
- **owner 内部**（细则约定 D）：写出后该点 `PointState.obs` 指向这条记录；`PointState.reason` 仍是 `POLL_<outcome>`／`POLL_MISSING`，实时视图和通信页的原因不变；故障计时照旧取消。
- **读取侧**：`history.assemble` 没有改（`quality ≠ GOOD` 即 `valid=False`，自然结束此前的保持）；`input_basis` 改一处——THIS_ROUND 里这个输入**只有失效记录**时计入 `missing`、`complete=False`（原来只有“一行都没有”才算）；`strain_change` 的“上一轮”行不改变原有判断。CSV 导出前端逐行写 `row.quality`，行里就是 `MISSING_*`，没改。
- **连带**：C2 状态重申会重申失效记录本身（`orig_id` 指向它），不会改为重申更早的有效值（用例）；通信页输入表该行 `value` 为空、`quality` 为 `MISSING_*`、`reason` 仍为 `POLL_*`（用例，经 `communication_inputs()`）；R03a 的“本段证据”不计失效记录（`quality='GOOD'` 的条件已把它排除）。
- **没有做**：没改 `modbus.py` 的采集、重连和告警规则，没改轮次分类；不回填、不插值；没改 `ingress.py`、`publisher.py`。

**新用例（`tests/test_failure_records.py` 14 项；每个先看 owner 提交的组，再用同一批组做历史：内存里 `history.assemble`、真实数据库里 `history.query`，对应细则 7.2）：**

| 细则 7.2 | 用例要点 |
| --- | --- |
| ① 第10秒成功、第11秒整轮超时、第12秒成功 | `b1`、`b2` 各一条 `MISSING_TIMEOUT`（`value` 空、`status_code` 空、第11秒、`tick=11`、`poll_id=poll11`、`response_id` 空，`item_seq` 0、1，与 `POLL` 状态和派生量同组）；历史里 `b1` 三行 1010 GOOD／1011 `MISSING_TIMEOUT`／1012 GOOD，1010 的值只保持到 1011（`NEXT_CHANGE`），1011 `valid=False`，`s1` 同一时刻不可用，原始量与派生量一致；实时视图 `b1` 无效、原因 `POLL_TIMEOUT`、`value=None`；`input_basis`（真实库）把 `b1` 计入 `missing`、`complete=False` |
| ①′ 连续2轮整轮超时 | 只在第一轮写；中间有一轮有效观测则下一次失败又是一次新的转移（共4条） |
| ② 协议异常持续60秒以上 | 62轮 `EXCEPTION`，只有 `MISSING_EXCEPTION` 各一条（第1002秒）；源保持 VALID、无告警；历史里 1001 的值在 1002 结束，不再一直保持到水位（方案 1.1 节记录的改前行为；该用例改前先败在“没有失效记录”这一步，没有单独观察到这一点）；水位和 `coverage_end` 不受影响 |
| ③ 多请求轮中一个请求持续超时 | 两台从站、每轮两个请求，第二台持续不应答：它的两个点各一条 `MISSING_UNANSWERED`（后几轮不再写），`item_seq` 接在本轮观测之后（0–3）；第一台的点每轮都有观测、始终有效；实时视图 `POLL_MISSING`。**真实线路版**：真实采集器＋R05 的多从站真实服务端，第二台沉默6轮后恢复：整轮分类仍是 `OK`，各点恰好一条失效记录，历史序列收成 GOOD→`MISSING_UNANSWERED`→GOOD，失效前一行在失效时刻结束；连跑8次均通过 |
| ④ 恢复 | ①与③里各有恢复轮：首条有效观测重新开始保持（`until` 链回正常） |
| 原因 | 三种整轮失败（`TIMEOUT`／`EXCEPTION`／`CONNECTION`）各自的质量名；实时原因仍是 `POLL_<outcome>`，不是 `QUALITY` |
| 不受影响的路径 | 解码为 BAD 的点（`nan`）仍是 BAD 观测，随后整轮超时不再为它写记录；从未有过有效值的点不写；OPC UA 的好值和 Bad 通知逐条不变（质量、值），不产生 `MISSING_*`（该条改前改后都通过） |
| 容量 | 满量 8 路 Modbus：一次失败转移**增加 8 条**记录（每点一条），继续失败的轮次增加 0 条；正常轮次的观测条数不变 |

**命令与结果：** 命令形式同 R01。
- 改前（`htsg/` 未动）：首批 10 项 **9 失败 / 1 通过**（0.51 秒）；通过的是 OPC UA 路径的对照。改后补的 4 项（三种整轮失败的参数＋真实线路）用基线副本补测（`git archive 946cb5d`，已核对导入的是副本）：**4/4 失败**，1.92 秒。
- 改后：新文件 **14/14**；`tests/test_failure_records.py tests/test_owner_batches.py tests/test_modbus_acquisition.py tests/test_input_basis.py tests/test_history_query.py tests/test_capstone_b.py tests/test_capstone_f.py tests/test_partial_requests.py`：**92 通过 / 0 失败 / 0 跳过，102.71 秒**（14＋24＋11＋7＋9＋3＋9＋15）。
- 回归（改后、`test_input_basis` 修正后）：`test_web_*`（8个文件）、`test_stream`、`test_capstone_c/d/g`、`test_runtime`、`test_smoke`、`test_history_api`、`test_history_merge`、`test_exit_evidence`、`test_failure_records`（当时10项）、`test_partial_requests`：**164 通过 / 0 失败 / 0 跳过，460.60 秒**。更早的一轮 22 个 owner／历史相关文件：232 通过 / 1 失败（76.06 秒），那 1 项就是下面改的断言。
- `ruff check htsg tests scripts` 通过；`git diff --check` 通过。

**对原有用例的修改（细则 7.2：因新增失效记录而必须调整的断言，逐条）：** 只有一条。`test_input_basis.py::test_modbus_basis_is_this_round_only_and_a_missing_input_is_reported_not_borrowed`：原断言 `na["inputs"]["b2"]["rows"] == []`（未应答的输入在依据里一行都没有）改为 `[(2, "MISSING_UNANSWERED")]`（第2轮有它自己的失效记录）；`missing == ["b2"]`、`complete is False` 不变，“第1轮的值不被借用”的原意保留。理由：R07 之后这个输入在第2轮有一条失效记录，依据里本来就会列出它。其他六个细则点名的文件及全部回归都没有因本批失败。

**可见的变化与观察（需知悉）：** ① **实时视图里失效点的 `value` 现在是 `null`、`quality` 是 `MISSING_*`**（以前是旧值和旧质量，只是 `valid=false`）：因为 `PointState.obs` 指向失效记录（细则 7.1 第4条）；总览、通信页、回放表对 `null` 的显示沿用原有 BAD 观测的路径（通信页和回放表原样写出 `MISSING_*`），我没有逐页在浏览器里核对每个页面对这个新值的显示，回归套件里的页面用例都通过。② 派生量取输入时 `_input` 读的是 `obs.value`，现在是 `None`；这些输入本来就 `valid=false`，派生量照旧不可用，无行为变化。③ `CONNECTION` 轮的失效记录落在即将被 `source_down` 切断的那一段里，段终点随后由 `RESYNC` 写出，没有新增段处理。④ 满量下容量增量：每次失败转移最多 **+8 条** `observation` 记录（每个 Modbus 输入一条），之后同一次失败不再增加；请把它计入容量报告（交付件中的容量报告我没有改）。

**未覆盖/留给后续：** 没有做变异验证；CSV 导出和各页面对 `MISSING_*` 行的显示没有单独的浏览器用例（前端逐行写 `quality`）；`input_basis` 对 `strain_change` 的“上一轮”行不改变原有判断（本轮没有任何行而上一轮有时，仍不算 missing，这是原有行为）；归档容量报告和 G3 的满量测量尚未按本批重做。

### R03b 持久段身份与恢复单元（Q07）

R03b｜基线 `946cb5d` 之上的工作区（R07 之后，未提交）｜**改** `htsg/owner.py`（本批只动这一个源文件）；测试 **新** `tests/test_segment_identity.py`（15 项，`git add -f`），**改** `tests/test_archive_gap.py`（共用的 `Sink`）、`tests/test_archive_recovery.py`、`tests/fault_support.py`、`tests/test_fault_owner.py`、`tests/test_capstone_e.py`、`tests/test_history_query.py`、`tests/test_input_basis.py`（见下，逐条）｜**验收** 命令见下｜**结果** 新用例改前 13 失败 / 1 通过（当时14项），改后 15/15；随机序列另用临时脚本跑了 200 个种子全过；关联套件（含新文件）**179 passed**；整套连续运行（旁证）744 passed / 1 failed（已知不稳用例，非本批引入）/ 1 xfailed / 0 skipped｜**证据位置** 新用例本身（可重跑，含真实数据库）；运行输出与探针在会话临时目录，未入库｜**未覆盖** 见末段。

**改动（出处：细则 6.1、整改方案 R03b / 第2.3节决策1、M1）：**
- **持久段事实**（`SourceState.durable`）：`committed()` 确认含 `SEG_START` 的组后加入该段号，确认含 `SEG_END` 的组后移出；`refused()` 和作废不改变它。**去掉了 `lost_seg`**，不再用 `open_segment` 或 `lost_seg` 推断要收尾哪个已落库段。
- **收尾意图**（`SourceState.closing`：段号→原因、终点）：每个 `SEG_END` 状态记录在 `_build` 时登记，不论它所在的组后来是被丢弃（归档 LOST）、被拒、被作废、被 pending 清空，还是还在队列里；该段的 `SEG_END` 被确认时移除；恢复单元确认后，清掉从未落库的段的意图。源失效、重同步、换代、停机产生的 `SEG_END` 因此一律“登记，不再静默丢弃”。
- **延后补写**（`_segment_group(unit=True)`）：恢复单元对 `durable` 里每个段写一条 `SEG_END`，终点＝收尾意图的终点与缺口起点（`lost_since_wall`）中**较早者**；意图终点不晚于缺口起点时用意图的原因，否则用单元自己的原因（`ARCHIVE_GAP`／`RECOVERY`）。没有落库的段不写，所以不会出现只有终点的“幻影段”。
- **每源最多一个在途恢复单元**：`recovery_order` 一直保留到单元结清（确认或被拒）；`_try_archive_recovery` 和 `_recover` 在有在途单元时不建第二个；在途期间待发队列溢出，状态进 LOST 但**不清 `recovery_order`**，溢出的时刻、批次、原因和组号记在 `overflow`，`is_void` 因此不会作废在途单元（原有的 `order != recovery_order` 判断现在才可靠）。
- **在途单元结清后**：确认且其后发生过溢出——登记该单元的持久事实（新段已落库、旧段已收尾、缺口已记），归档状态保持 LOST，下一个单元的缺口从溢出时刻起算（`lost_since_wall`、`lost_reason`、`lost_from_batch_seq`、`void_after_order` 取溢出时的值）；被拒——持久事实不推进，缺口起点保持最早的丢失起点；UNKNOWN——提交队列原样重试，结清前不建下一个单元。
- **本批追加的两处（细则没写，随机序列逼出来的，需知悉）：** ① 本源还有**已被队列接纳、尚未结清**的组（`SourceState.inflight`，在 `_submit`／`_submit_recovery` 接纳时登记，`committed`／`refused`／`is_void` 判作废时移除）时，**不建恢复单元**（`_try_archive_recovery`、`_recover` 两处）。原因：丢失若由“队列满”发现，队列里还有本源更早的组；单元建好之后这些组被拒，丢失起点要前移到单元已经记录的缺口之前，单元里关闭的段就出现“终点早于起点”（随机种子28：段5 起于 1012，却在 1008 结束）。单元本来就排在这些组后面，提交时间不变，只是基线晚一点取。② 已处于 LOST 且没有在途单元时，更早的组被拒，缺口起点取较早者（原来只在进入 LOST 的那一刻取一次）。
- **没有做**：没改 `commitq.py`（作废仍然无回调，所以 `is_void` 在判作废的同时把该组从 `inflight` 里移除）；没改 `archive.py`、`history.py`；没新增表、列或归档记录种类。

**新用例（`tests/test_segment_identity.py` 15 项；同步写入桩复刻提交队列的契约：接纳或拒绝；队首的组被确认、被拒、被暂扣——UNKNOWN 重试；作废的组静默丢弃；对应细则 6.2）：**

| 细则 6.2 | 用例要点 |
| --- | --- |
| 1 Q07 ① | 段1落库→丢失→来源超时失效（SEG_END 因 LOST 无法入队）→3–6秒内恢复：恢复单元带段1的 `SEG_END`，终点＝缺口起点（早于失效时刻105）；确认后每个已落库段恰一条 `SEG_END`，无幻影段；真实库里段1已收尾、段2开着 |
| 1 Q07 ② | 段1落库→换代的组（`SEG_END` 1＋`SEG_START` 2）被拒：单元的 `SEG_END` 指向段1，不是从未落库的段2；段2不出现 |
| 1 Q07 ②′ | 段1落库→`source_down` 的 `SEG_END` 组被拒→新代次：单元收尾段1，终点＝断开时刻 |
| 1 Q07 ③ | 单元在途→待发溢出→首个单元被拒：下一个单元仍收尾段1，缺口起点仍是最早的丢失起点；被拒单元开的段2从未落库，不出现 |
| 2 竞态 | 单元在途时待发溢出，之后：首个单元在下一次 tick 之前确认／在它之后才确认／被拒（即③）三种时序都只有一个在途单元，首个单元从不被作废（`is_void` 为假）；确认后归档保持 LOST，下一个缺口从溢出时刻（1006）起算，不盖住已确认的基线，旧段2 终点＝溢出时刻；两个缺口 [102,103]、[106,108] 不重叠 |
| 3 一致性断言（所有序列） | 每段 `end_wall ≥ start_wall`；同源缺口互不重叠；被拒后最早的丢失区间仍被记录；无幻影段；结清后只有最新段开着；真实库里复核同样的几条。**另有随机序列**：6个固定种子在用例里，200个种子用临时脚本额外跑过，每步检查“至多一个在途单元、在途单元不被作废、已落库历史满足上述断言” |
| 4 覆盖 | UNKNOWN：单元被暂扣10个节拍，期间不建第二个单元，之后确认；重复确认（`ALREADY`）：同一单元再确认一次，状态和计数不变；三库间段片段合并：把一个跨多段的场景的各组轮流写进三个数据库，合并查询与单库查询的段（起止、原因、各行时刻）完全一致，`partial` 为假 |

**命令与结果：** 命令形式同 R01。
- 改前（`owner.py` 未动，只有新文件）：当时 14 项 **13 失败 / 1 通过**（0.41 秒）；失败对应 Q07 的各条（段2 被当成要收尾的段、没有 SEG_END、随机序列里直接出现“有终点无起点的幻影段”、第二个单元重叠等），通过的是“重复确认”那一条。第15项（三库合并）是改后补的，没有改前运行。
- 改后：新文件 **15/15**；临时脚本（同一个随机序列函数体，种子 6–205，每个种子收尾时在真实库里复核）**200/200**。中间三次失败的归因：① 随机序列的收尾阶段没有在来源进入重同步后开新代次，是用例问题，已改；② 随机序列里把“带 GAP 的恢复组”之外的普通恢复组也当成单元，是用例问题，已改；③ 单元建得太早（上面追加的第①处），是实现问题，已改，之后 200 个种子才全过。
- 细则 6.2 第5条列出的 `test_archive_gap.py`、`test_archive_recovery.py`、`test_capstone_f.py`、`test_capstone_e.py` 及相关套件：`test_archive_gap`、`test_archive_recovery`、`test_capstone_d`／`e`／`f`、`test_fault_owner`、`test_history_query`、`test_history_merge`、`test_input_basis`、`test_run_segments`、`test_watermark`、`test_owner_batches`、`test_source_validity`、`test_commit_queue`、`test_exit_evidence`、`test_failure_records`、`test_segment_identity` 一次 pytest 进程 **179 passed**（68.5 秒）。
- `ruff check htsg tests scripts` 通过；`git diff --check` 通过。
- **整套连续运行（旁证，不是 G2，候选版本未定）：** R03b 完成后的工作区，一次 pytest 进程、不分段：收集 746，**744 passed、1 failed、1 xfailed、0 skipped**，813.66 秒（0:13:33）。失败的是已知不稳用例 `test_modbus_acquisition.py::test_one_timeout_keeps_the_connection_three_in_a_row_reconnect`（见 R01 条目，未改动，待用户裁定）；xfail 的是 R02 的 `@` 口令参数。同一工作区的**第一次**连续运行在第 385 个用例 `test_measure.py::test_the_push_latency_is_split_into_its_parts_and_the_packing_wait_is_reported_apart`（有浏览器参与）处停住：faulthandler 150 秒时的堆栈里主线程空闲在事件循环的 IOCP 等待中，另有 2 个 anyio 工作线程、2 个线程池工作线程都在空等，没有线程在忙；那段时间我这边没有同时运行别的测试进程。**第二次没有复现**（该用例通过，12.8 秒）；该用例单独运行 6/6 通过，`test_measure.py` 单文件通过。原因未查明，也没有抓到停住时的 asyncio 任务栈（第二次带了观察插件，但没有用例超过 100 秒）。这属于用户已知的“整套连续运行挂住”，G2 前仍是开放问题。

**对原有用例的修改（细则 6.2 第5条；意图都保留，`test_recovery_attempts_the_queue_turns_away_do_not_move_the_segment_the_gap_closes` 的意图——被拒绝的恢复尝试只消耗编号、不移动缺口要收尾的那个段——原样保留并通过）：** 全部是同一个原因：这些用例的“提交桩”接纳一个组就当它已落库，而 R03b 之后 owner 只信 `committed()` 的确认，并且只在本源没有更早的在途组时才建恢复单元。改法都是让桩去扮演一个跟得上的写入线程。
1. `tests/test_archive_gap.py` 的共用 `Sink`：接纳非恢复单元的组时立刻调 `owner.committed(group)`（`build()` 和另外一个自己建 `Sink` 的用例把 `sink.owner = owner` 接上）；恢复单元仍由用例自己决定命运。
2. `tests/test_archive_recovery.py::test_a_second_overflow_while_waiting_retakes_the_baseline_and_memory_stays_bounded`：在期望第二个单元之前，先确认首个单元（并断言它确认后归档仍是 LOST）。理由：现在每源只有一个在途单元，第二个要等第一个结清；“溢出后重新取基线、内存有界”的意图不变。`test_against_postgres_the_recovery_unit_lands_in_one_transaction_with_every_part` 同样只是接上 `sink.owner`。
3. `tests/fault_support.py` 的 `Accepting` 与 `fault_owner`、`tests/test_fault_owner.py` 的 1000 故障用例：同第 1 条。
4. `tests/test_capstone_e.py::test_a_recovery_unit_with_1000_faults_and_the_full_baseline_stays_inside_the_group_budget`：手工把状态置为 LOST 之前，先确认桩已接纳的组。
5. `tests/test_history_query.py::test_an_archive_gap_stops_the_hold_at_the_start_of_the_gap`：前一个桩接纳的组（运行开始、第1段开始）先确认，并连同它们一起写进数据库（原先第1段的起点组没写进库，靠“只有终点的幻影行”撑住断言）。
6. `tests/test_input_basis.py::test_values_derived_inside_an_archive_gap_say_that_their_basis_is_incomplete`：接上 `sink.owner`。

**需知悉：** ① **同一段的 `SEG_END` 在极少数交错下会写两次，终点相同。** 那条 `SEG_END` 所在的组还在队列里、没确认时，恢复单元已经按“持久段＝起点已确认、终点未确认”的口径带上了它；两条随后都落库，`segment` 表是幂等 upsert，一行一个终点（随机序列的断言放宽为“各条终点一致”，四条命名序列仍是严格一条）。要做到任何交错都恰好一条，得让 owner 跟踪队列里每个未结清组并处理作废无回调的情形，我没做。② `closing` 里从未落库的段的意图要等恢复单元确认才清理，归档长期 LOST 而来源一直抖动时会慢慢增多（每次失效一条，量很小，但理论上没有上界）。③ 恢复单元现在要等本源在途的组结清后才建，期间进来的数据落在 LOST 里（记为缺口的一部分），而不是排进 pending；归档写入很慢时缺口会比原来长，但单元本来也排在这些组之后，提交时刻不变。④ `inflight` 只有真实的提交队列（它保证 `committed`／`refused`／`is_void` 三条出路）才会清空；用纯接收器（`commit` 返回 `None` 且从不确认）的用例里，丢失之后恢复单元不会建，这也是上面逐条改桩的原因。

**未覆盖/留给后续：** 没有做变异验证；停机时的终结组（R04）要用的“登记为恢复单元”目前没有，`shutdown()` 产生的 `SEG_END` 只登记意图；`_clock_jump` 在归档非 OK 时仍只写标记不切段，没动；真实数据库里没有跑“写入线程在途时 `is_void` 判作废”的并发时序，只有同步桩加三个真实库的落库复核。

### 整改续记：R02、R03a、R03b复核补正与R04

**接手基线与方法（2026-10-16）：** 从未提交的R03b工作区继续，先按[整改开发细则](HTSG_V1_全量测试前整改开发细则_v0.1.md)重读直接调用方和用例；GitNexus没有本仓库索引，本次以源码、diff和定向测试核对。开发库55432/55433可达。没有重做已完成批次。

**R02补正：** `commlog.py`同原因聚合原先只有新详情非空才覆盖旧详情；“短详情→超长详情被省略(None,True)”会继续展示旧短详情且不标省略。已改为`detail is not None or truncated`时更新，ATTEMPT和ROUND均补“短→省略→短”用例。聚合省略标志定向7项通过。口令自身含未转义`@`的严格xfail仍保留待裁定，本次未擅自改变已确认的“第一个@”规则。

**R03a补正：** 跨库崩溃收尾逐库执行时，第一库写入`ABNORMAL_EXIT/EXIT_TRUNCATED`、第二库失败，会让下次启动把清理结果误当真实RUN_END/SEG_END而永久跳过第二库。`unfinished_facts()`现只把非`ABNORMAL_EXIT`运行及非`EXIT_TRUNCATED`段作为跨库真实终结证据；新增真实双库“第一次中途失败、第二次继续”回归。`test_exit_evidence.py` **19通过**；R03a相关扩展集曾为49通过，旧记录保留。

**R03b补正：** 新增`opening`记录尚可能落库的SEG_START，并在拒绝、作废、未入队、pending清空和确认时结清；`closing`只保留已持久或仍可能持久的段。长期LOST反复1000次换代后，关闭意图保持为已持久段1条，不再线性增长。`test_segment_identity`＋archive gap/recovery **29通过**。极少数相同终点的重复SEG_END仍由既有幂等upsert处理，本次未擅自放宽或重写该历史说明。

**R04实现：** `owner.py`把源收尾和RUN_END拆开：恢复单元、pending、普通SEG_END或LOST终结组先结清；LOST终结组只含GAP和已持久段SEG_END，不带C2基线。每源记录稳定的终结组、确认/拒绝结果；`runtime.py`保持writer运行，在统一`STOP_WRITE_WAIT`内推进源收尾，全部确认后才提交一个稳定RUN_END并等待确认；队列满重交同一对象，REFUSED、UNKNOWN或期限未决不登记NORMAL。新增`test_shutdown_commit.py`覆盖队列满同组重交、REFUSED、UNKNOWN真实库不写正常结束、真实恢复pending顺序；R03b直接用例覆盖恢复中停机和LOST终结组。

**验证：** `test_runtime.py` **6通过**；`test_run_segments.py` **10通过**；`test_shutdown_commit.py` **4通过**；R01–R04扩大定向集 **161通过 / 1 xfail / 0失败**，94.00秒；R03b/R04及Capstone D/E/F、历史/依据/故障关联集 **90通过 / 0失败**，80.80秒。ruff、JavaScript语法和`git diff --check`通过。两次沙箱内pytest的业务断言均跑完，但最终因临时目录访问权限报错，以上正式数字来自授权后的隔离目录运行或不依赖该目录的重跑。

**该次续记尚未关闭（后续状态见下面R06条目）：** ① `test_modbus_acquisition.py::test_one_timeout_keeps_the_connection_three_in_a_row_reconnect`仍有慢应答夹具间歇失败，不能归因于R01；② R02未转义`@`严格xfail；③ R04还未执行G2连续全套与G3证据刷新；④ 当时R06尚未开始。当前没有提交这些整改改动。

### R06 模拟服务器保活适配（主体实现，G3未完成）

R06｜R04后的同一未提交工作区｜**新**`htsg/sim_ua.py`（锁定asyncua 2.0.1的实例级SubscriptionService包装，只修项目自建Server实例）；**改**`htsg/simulation.py`和`tests/protocol_support.py`（FieldSimulator/FieldUa共用适配，FieldUa可显式关闭作raw对照）、`tests/live_support.py`、`tests/test_runtime.py`、`scripts/measure.py`（撤销计数1绕行，使用默认2）、交付记录保活说明；**新**`tests/test_simulated_keepalive.py`（适配/raw计数2/1真实间隔＋Owner十种tick相位）｜未修改生产UA客户端、第三方Server、3秒有效期和默认保活计数。

**结果：** 真实回环asyncua测试：适配实例计数2／1约2秒／1秒，随后启动的未适配实例仍约4秒／3秒，证明没有全局类污染；四种间隔＋十种Owner tick相位 **14通过**。`test_ua_client.py`、`test_capstone_c.py`、`test_runtime.py`、`test_simulation.py`合计 **30通过**，96.74秒；ruff、JavaScript语法和差异检查通过。

**长稳与候选证据（随后完成）：**

- 静止满量：`measure.py --steady 600 --outage 7 --quiet --stats-interval 60`，真实200点OPC UA＋8路Modbus＋Owner＋双PostgreSQL运行600.1秒；`expiries=0`、入口/队列/写失败均0、队列无遗留、停机未写0，UA/Modbus各一次真实停服后均重建并恢复归档OK。稳态约1.19组/秒、9.2行/秒；报告`docs/evidence/r06/measure-20261016-140349.json`（本地忽略证据）。
- 标准D46＋Chrome：120.1秒满量变化、双协议各25秒断连；417量稳定写入约417.8行/秒，无拒写/失败；接收到绘制延迟p50 112.0ms、p95 124.7ms、p99 137.1ms、最大393.3ms，停机未写0；报告`measure-20261016-141740.json`。
- D47实际脚本：6次硬杀无半组/孤儿，三库路由与滞回按预期；满量变化一年仍需约10.51TB并判`INSUFFICIENT`，静止工况约0.23TB判当前卷足够但不替代满量结论；报告`resilience-all-20261016-141257.json`。
- D48实际烟测：启动、正常停止、同库重启均通过，两个run均`NORMAL`、历史无重复/缺口。候选摘要已扩展到后端＋独立模拟进程加载的28个模块：代码`830e096e...cdb0`、页面`23e75d26...6ac`、配置`6e1f673d...eac5`、schema`2479b07e...ee2`；报告`smoke-20261016-141901.json`。工作区仍未提交，所以`clean=false`属实。
- 受影响回归：`test_measure.py`、`test_smoke.py`、`test_resilience.py` **9通过**；静态检查通过。

**R06证据刷新当时仍未完成（后续G2结果见下）：** 当时G2当前候选连续全套尚未运行；真实网关行为、现场设备/卷和一年容量通过结论仍未取得。历史满量容量“不通过”被新D47再次确认，没有被静止工况推翻。

**G2连续全套（随后完成）：** 当前工作区收集771项。第一次连续运行 **766通过 / 4失败 / 1 xfail / 0跳过**，852.94秒；没有挂住。四个失败分别是：共享`owner_and_groups`测试助手被R04改成自动确认后破坏了两条“未确认水位”用例；Capstone E和新增恢复停机用例以“队列已弹出”误当数据库回调已完成。均属于测试支撑/竞态等待问题，不是业务断言被实现推翻；改为只有需要的生命周期用例启用即时确认，并等待持久结果而非只看队列深度，四项定向重跑全部通过。

第二次同一工作区、单一pytest进程连续运行：**770通过 / 0失败 / 1 xfail / 0跳过**，815.96秒（13分35秒），退出码0，无停滞；两条既有依赖弃用警告。唯一strict xfail仍是R02中URL口令自身含未转义`@`时“第一个@”遮蔽规则的待裁定边界。故G2运行证据已取得，但R02该安全取舍及G1最终无遗留状态仍未关闭。

第二次全量后，最终聚焦回归暴露`test_shutdown_commit`自身仍有一个前置竞态：只等待SEG_START确认，没有等待紧随其后的首批数据组结清，偶尔因此在恢复单元尚不应建立时开始3秒等待。用例改为同时等待`durable={1}`、本源`inflight`为空及队列深度0；业务代码未再改变。该真实数据库用例随后连续5次通过（0.91–1.01秒/次）。没有为这条纯测试等待修正运行第三次全套，上述第二次G2对应的业务代码与当前相同。

### R04 以提交结果完成正常收尾（Q03及补充A）——正式条目与细则修订

R04｜基线`946cb5d`之上的未提交工作区（R03b之后；2026-10-16实现并补正，2026-10-19细则修订）｜**改**`htsg/owner.py`（`shutdown`、`shutdown_progress`、`submit_run_end`、终结组、`opening`结清）、`htsg/runtime.py`（`Runtime.stop`停机循环），`htsg/commitq.py`不改；**新**`tests/test_shutdown_commit.py`（8项）、`tests/test_runtime_stop.py`（16项）；**改**`tests/crash_worker.py`（新增`before_end`／`after_end`两种停机模式）、`tests/test_run_segments.py`（`owner_and_groups`的`confirm`开关），另有`tests/test_segment_identity.py`中的停机相关用例｜**验收**`pytest tests/test_runtime_stop.py tests/test_shutdown_commit.py`（真实数据库）；整套连续运行｜**结果**两个新文件 **24通过 / 0失败 / 0跳过**，连跑4次稳定（42.5–43.7秒），其中`test_shutdown_commit.py`由上文“验证”所记的4项增至8项；`opening`补正后的整套连续运行（收集775）**774通过 / 1 xfail / 0失败 / 0跳过**，836.74秒（首次运行的1项失败见下面“整套连续运行”一段）；当前收集795项，尚无整套记录｜**证据位置**用例本身（可重跑），停机实测值由用例打印｜**未覆盖**见本条末段。

**停机流程：** 取消采集、owner循环和维护任务并排空入口后，`Runtime.stop`开始计`STOP_WRITE_WAIT`（8秒）并调用`owner.shutdown`：它登记各源收尾意图、关段（归档非LOST的源照常发出SEG_END，LOST的源只记收尾意图），不再无条件提交RUN_END。期限内每50毫秒推进一次`shutdown_progress()`（LOST且无在途单元的源在此构造终结组，并登记为该源的`recovery_order`），`submit_run_end()`只有在全部来源结清后才构造并提交唯一的RUN_END组，队列满则在期限内重交同一对象。循环在RUN_END被确认或被拒、有源收尾失败（其终结组被明确拒绝）或期限到时结束，其后才调`queue.stop()`，writer因此不会在RUN_END确认前因队列暂空而退出（`commitq.py`不改）。只有RUN_END被确认才登记正常结束；被拒、UNKNOWN未决、期限到、有源收尾失败都不登记，下次启动记`ABNORMAL_EXIT`。上文“R04实现”一段里的REFUSED指终结组或RUN_END被明确拒绝；恢复单元被拒见下面“细则修订”。

**细则修订（2026-10-19，用户确认）：**

- **内容：** 细则8.1新增2a项：停机开始时在途的恢复单元之后被明确拒绝，该源回到LOST，由停机流程补发终结组（GAP＋已落库段的SEG_END，无基线）。终结组被收下则运行登记`NORMAL`，也被拒才不登记。须同时满足五项前提：拒绝是确定未提交的拒绝；缺口覆盖全部未落库数据；每个已落库段都收尾；终结组确认提交后才提交RUN_END；全部在同一个8秒内。同时改了第4、5、6步（引入“收尾失败”）、第7步实现方式、8.2-2验收，并在第11节新增约定J–M。
- **为什么：** 原8.1-4(c)、8.1-6把“恢复单元被拒”直接算作该源不能正常结束，比整改方案v0.2 R04第5步更严。方案写的是“已接纳记录确认提交或其拒写已形成持久缺口、源收尾结清后，才提交RUN_END”。用户2026-10-16在审核中选“保留现状”，2026-10-19确认并要求写入细则。方案不改，其“明确失败”在此处指终结组被明确拒绝或期限到。
- **后果：** 满量恢复单元约0.6MB（积压1000条故障约1.7MB），远低于单次写入上限8MiB。生产模式下的拒收原因多与包大小无关（磁盘余量），终结组也会被拒，结果与按原细则相同；差别只出现在与包大小有关的拒收，如单次上限`WRITE_BATCH_BUDGET`、开发模式累计写入预算`DEVELOPMENT_WRITE_BUDGET_EXHAUSTED`。`NORMAL`的含义因此变弱：不再等于“停机时一次拒收都没发生”，库内也不记录“恢复单元被拒过”，缺口原因记的是最初的丢失原因。
- **随带约定K–M（用户2026-10-16裁定，2026-10-19一并写入）：** 终结组遇队列满时在期限内重交同一对象，仅明确REFUSED不再重试（K）；`commitq.py`第59行不改，writer不提前退出靠`Runtime.stop`的调用顺序（L）；终结组不带内存中的故障缓冲，这些记录随进程结束丢失（M）。
- **用例钉住：** `test_shutdown_commit.py::test_a_unit_turned_away_after_the_stop_began_is_followed_by_the_final_group_of_its_source`（终结组收下／也被拒两个变体）和`test_runtime_stop.py::test_a_unit_and_the_final_group_both_turned_away_end_the_stop_at_once_without_the_end`。在临时副本里把行为改成“恢复单元被拒即该源失败”，收下变体失败、被拒变体通过。

**同期补正（用户2026-10-16批准，审核发现）：** 恢复单元被拒时，`pending`里等着的新段起点组被丢弃，却没有结清`opening`，之后正常停机永远等不到结清（随机历史里约15%的收尾后停机、约2.5%的随机时刻停机会卡住）。在`owner.py`三处`pending.clear()`前补`_discard_groups`（`_archive_lost`、恢复单元被拒的分支、`_submit_recovery`）；`_submit`的溢出分支原本就有。`test_segment_identity.py`新增4项（1个确定性回归、3个随机停机性质），改前全失败，改后全过；临时脚本跑了30,000个收尾后停机和3,000个随机时刻停机（写入端全收：3,000个完成；写入端拒收30%：2,311个完成、689个以明确拒绝结束），0个卡住。

**整套连续运行（`opening`补正后、新增20项之前，收集775项）：** 第一次 **773通过 / 1失败 / 1 xfail**，失败的是`test_comm_protocols.py`里“五次拒绝重连”一条（pymodbus请求超时），当时我在并行跑别的进程；同一文件单独连跑12次、16项全过；其后不并行的干净重跑 **774通过 / 1 xfail / 0失败 / 0跳过**，836.74秒，无停滞。把那次失败归因于并行负载是推断，不是证实。

**停机实测（细则8.2-5，上限9秒）：** 4次运行，数字由用例打印：单来源正常停机0.113–0.123秒；满量（8路Modbus＋200点UA，真实双库）0.112–0.175秒；归档不应答、走到真实8秒期限8.037–8.078秒。

**未覆盖／待裁定：**

- 这两个测试文件的变异检查只做过一次（上面“恢复单元被拒即该源失败”那一种），其余未做；
- 当前795项尚无整套连续运行；
- `owner.py`补正后代码指纹由`830e096e…cdb0`变为`600b9bc5…`（页面、配置、schema摘要未变），`docs/evidence/r06/`的D46／D47／D48证据须在最终候选上重做（G3）；
- 审核时列出的三个小项待裁定：LOST＋VALID的终结段`end_reason`写`SHUTDOWN`（恢复单元路径写`ARCHIVE_GAP`）；停机循环遇第一个收尾失败即退出，其余来源可能没来得及补终结组；`unwritten_at_stop`是混合计数，不是未写组数；
- 停机时LOST源已确认、未落库的故障记录随进程丢失（探针2条），属约定M已裁定的后果，此处只记数量。

### 整改补充：2026-10-19 用户裁定与审核必修2–5

**出处：** 用户2026-10-19转来的审核建议（逐项裁定），并要求把上一份审核的必修2–5纳入本轮；各项只跑了直接相关的测试，没有做变异检查、整套连续运行和G3刷新。

- **R02 未转义`@`（选方案(b)）：** `commlog.py`的`_USERINFO`尾部加`(?:[^\s/@]*@)*`，`u:p@ss@host`输出`***@host`；`test_comm_log.py`去掉严格xfail并加一条参数，`test_detail_bounds.py`新增4项（三个以上`@`、百分号编码口令、含空格凭据、多个URL各遮各的、遇空白或斜杠即停、二次清理不再变化）。`test_comm_log`＋`test_detail_bounds`＋`test_comm_facts`共107通过，已无xfail。取舍：不带空白、相连的`b.com,c@`这类片段也会被多遮。
- **R04停机三项：** ① 终结段结束原因与恢复路径共用`_durable_ends`：离段时刻不晚于缺口起点保留原因，否则记`ARCHIVE_GAP`（LOST＋VALID原记`SHUTDOWN`，现记`ARCHIVE_GAP`）；由CLOCK_JUMP切出的段一律视为在缺口之前。② `Runtime.stop`不再遇到第一个收尾失败就退出：全部来源成功或明确失败、或同一个8秒期限到才结束；有失败来源仍不提交RUN_END。③ 取消`unwritten_at_stop`，改为`shutdown_incomplete`、`run_end_status`（`NOT_QUEUED`／`UNCONFIRMED`／`COMMITTED`／`REFUSED`）、`failed_sources`、`unsettled_queue_items`；`Owner.shutdown_unsettled()`随之删除，`scripts/measure.py`报告新增`stop`一栏（旧证据JSON保留旧字段）。这些字段不能读作“零数据丢失”（约定M仍成立）。用例：`test_runtime_stop.py`+3（含双源A/B两种顺序），并把原有的14处停机结果断言改为新字段；`test_runtime_stop`＋`test_shutdown_commit`共27通过，owner层相关46通过，`test_runtime`＋`test_measure`10通过。双源用例在旧的“首败即退”下也会通过（队列排空本来就会把另一源的终结组写进去），区别新旧行为的是结束原因断言。
- **必修2 UNKNOWN重试固定原库：** `ArchiveRouter`新增有界`pinned`表（256条），首次UNKNOWN后只在该库核验或重试，不因磁盘水位改道，确认后释放；固定的重试不再经过磁盘与预算策略。`test_archive_router.py`+4项，该组共23通过。
- **必修3 首次Modbus失败不显示VALID：** 只有`OK`或`EXCEPTION`能使WAITING变VALID，首次`TIMEOUT`／`CONNECTION`保持WAITING（页面显示“等待首次数据”），曾经VALID后单次超时仍VALID。`test_comm_facts.py`+4项；相关owner层文件111通过（加上4项新用例单跑通过），真实Modbus三个文件54通过。
- **必修4 归档非OK时的墙钟跳变：** RECOVERING时也切段（切段组排在恢复单元之后的`pending`里，单元打开的段不再包住时钟的两半）；LOST时的CLOCK_JUMP标记记入`owed_jumps`（至多16个），由下一个恢复单元或终结组带上，确认后释放；缺口终点不早于起点（回拨时为零长度）。`test_segment_identity.py`+6项（前跳、回拨各3种：RECOVERING、LOST、切段被拒），6通过。
- **必修5 跨库10000行上限：** `history.assemble`合并去重后按通道统一限流，设`truncated`；窗口前的“保持值”基准行不计入也不被截。`test_history_query.py`+4项，历史相关共66通过。
- **R01老用例：** `FieldModbus`新增`withhold`（接下来N个请求永不应答，含迟到应答，直到服务器停止）；`test_one_timeout_keeps_the_connection_three_in_a_row_reconnect`改用`withhold=1`和`withhold=3`，超时数恰为4。单个用例连续12次通过；旧写法隔夜曾约45%失败。
- **R06测试收紧：** `test_simulated_keepalive.py`改为量连续保活间隔，容差±0.2秒（未适配计数1、2对照各观察16秒，适配的各6秒）；新增变化按发布周期送达（六种相位，≤1.2秒）、稀疏变化与保活节奏（该用例实测间隔1.98–2.01秒）、3秒保活在某个tick相位会失效的反例。17项，连续两次全过（各77秒），未放宽容差。

**状态：** 当前收集824项（原795）；ruff、`git diff --check`通过。**未覆盖：** 变异检查、整套连续运行、G3证据（owner／runtime／commlog／archive_router／history均已变，须在冻结后重做）；R03a大库收尾耗时仍待验；SIM-01／SIM-03／N2另行安排；审核提到的R07端到端`MISSING_*`用例未排。上文R04条目“未覆盖／待裁定”里的三个小项（终结段标签、首败即退、`unwritten_at_stop`混合计数）已按本条处理。

### 整改补充：候选检查——变异检查与两次整套连续运行（2026-10-19／20）

**出处：** 用户2026-10-19要求“提交→变异检查→清理缓存→整套连续运行尝试捕捉问题”，2026-10-20要求按失败加固R05那组用例后重跑；测试改动与结果如下，源码没有再改。

- **提交：** `8dea1c5`（整改R01–R07与审核补充，本地，未推送）；`082dd19`（只改测试：R05时序加固、停机新用例）。细则文件与`docs/evidence/r06/`未入库。
- **变异检查（临时副本，工作区未动）：** 28个变异体，副本基线（218项，179秒）通过；26个被杀死，2个存活。存活的是`_archive_lost`与`_submit_recovery`里的`_discard_groups`：把该行换成“断言`pending`为空”的探针，在八个测试文件（含300种子随机用例、capstone D/E）里都没有触发，即这两处`pending`本来就是空的，属冗余防御；真正起作用的只有`refused()`里恢复单元被拒的那一处。原先也存活的“`Runtime.stop`遇第一个收尾失败就退出”（落库结果等价，队列排空会写出另一源的终结组）用新增用例杀死：恢复中的来源在另一来源被拒之后才完成恢复，停机返回时不得有来源停在“半结清”（`test_runtime_stop.py`+2项，单独连跑5次通过）。
- **数据库：** 两个集群里没有残留的临时测试库，保留库`htsg`、`htsg_research_*`没动（“清空缓存数据”按只清临时测试库理解，无物可清）。
- **整套连续运行（单进程、faulthandler 300秒）：**
  - 第一次（2026-10-19 20:59–21:15，`8dea1c5`＋停机新用例，826项）：825通过、**1失败**、0跳过、965秒、无挂住。失败：`test_partial_requests.py::test_a_real_single_slave_that_never_answers_is_a_whole_round_timeout_three_of_them_reconnect`，重连后第4轮TIMEOUT而非OK。
  - 原因：事件循环停住的时间超过请求超时（该组用例原为0.15秒）时，客户端一圈循环的计时先于服务端几圈循环的应答触发，晚到的答复被丢弃（日志：`received pdu without a corresponding request`）。在第二条连接收到首批字节处人为停住循环0.3秒，原超时下得到与失败相同的`['TIMEOUT','TIMEOUT','TIMEOUT','TIMEOUT','OK']`；0.8秒的超时扛得住0.3秒和0.6秒的停顿，1.0秒不行。停顿本身在全量运行里的来源（疑为垃圾回收）没有观察到；单独、满核负载（16个空转进程）、紧接前一条、整文件重复共63次运行都没复现。
  - 加固（只改测试）：`QUICK`的请求与连接超时改为0.8秒（生产值），三处精确序列断言带上`live.rounds`，新增守卫用例（停顿0.5秒仍须是3个TIMEOUT加2个OK；把超时改回0.15秒它即失败）。整个文件由68秒增到87秒，连跑3次16项全过，出错那条单跑10次全过。
  - 第二次（2026-10-20 10:16–10:33，即`082dd19`的状态，827项）：**827通过、0失败、0跳过、0 xfail**、1002秒、无挂住，只有两条依赖弃用警告。运行后两集群无残留测试库。
- **G2：** 第二次运行在同一候选（`8dea1c5`的源码）上连续运行全部适用测试并全部通过，G2的运行证据取得；第一次的失败如实保留，不以重跑抹去。

**状态与未覆盖：** 当前收集827项（上条所记824项为当时数字：停机新用例+2、R05守卫+1）；ruff、`git diff --check`通过。仍未做：G3证据（D46／D47／D48，owner／runtime／commlog／archive_router／history均已变，须在冻结后重做）；R03a大库收尾耗时待验；SIM-01／SIM-03／N2另行安排；审核提到的R07端到端`MISSING_*`用例未排；执行记录第3节交接三段、第2节过时条目和细则的同步修订仍待另议。

**开放问题（2026-10-20评审发现，已复现，未修复）：** 历史查询的行数上限（每通道10000，`history.py`）先删掉超限记录、再计算值的有效区间，被截掉的下一条不再参与，显示出来的最后一行因此被错误保持到段终点（或水位）。最小复现：值1、2、3依次在10、20、30秒，段到100秒；完整查询里值2保持到30秒（`NEXT_CHANGE`），上限为2时值2被保持到100秒（`SEGMENT_END`）；`truncated=true`只是提示，页面曲线和CSV仍使用这个`until`。该缺陷在基线`946cb5d`的单库查询里已存在，本轮的跨库合并（`_limit_per_channel`）沿用了同一做法。上面的G2只说明现有用例在当前候选上连续通过，不代表没有未覆盖的缺陷；本条修复并补齐回归用例（单库、跨库、被截掉的下一条为坏质量或`MISSING_*`记录）之前，不冻结候选，不刷新G3。

### 整改补充：历史截断修复、第10项边界、SIM-01与R07端到端（2026-10-20）

**出处：** 2026-10-20评审发现“历史行数上限先删记录再算区间”并已复现（见上条“开放问题”）；用户当日对12项清单的裁定：先修该错误并落实第10项边界，再做SIM-01、SIM-03、R07端到端，最后冻结候选、统一刷新G3。

- **历史截断（已修）：** `history.py`——`read_store`不再丢弃上限之后的那一行；`_limit_per_channel`把合并后第N+1行标为`boundary`并删去其后各行；`assemble`用它结束上一行的保持（`NEXT_CHANGE`），但不显示它，也不列出只剩它的段。**定义：每通道上限＝窗口内最多N行＋窗口起点处生效的保持值（后者不计入N）；页面与CSV由同一份结果生成，所以一致。** 基线`946cb5d`的单库查询已有此缺陷，上条所记的跨库合并沿用了同一做法。新用例4项（`test_history_query.py`）：单库逐个上限（1–4）；跨库两种摆放；被截掉的下一条为坏质量或`MISSING_*`记录（单库与跨库）；后一段只剩边界行时不列空段。在旧`history.py`上其中3项失败，第4项是新代码的守卫。
- **第10项(a)钉住表（已改）：** `archive_router.py`——UNKNOWN钉住表（256）满时不再淘汰最旧一条；新的UNKNOWN组返回`REFUSED／PIN_TABLE_FULL`并计入`health["pin_table_full"]`，已有的钉住照常工作。原“淘汰最旧”用例改写为“从不为新的腾位”。被队列作废（`is_void`）的组释放自己的钉子（`commitq.py`的作废分支调用路由器新增的`release`；用户2026-10-20选(a)；用例各1项：`test_archive_router`、`test_commit_queue`）。**取舍：** 被拒的那一组实际上可能已落库（回复丢失），它按“归档缺口”报告，不是“不存在”的声明。
- **第10项(b)时钟标记（已改）：** `owner.py`——归档丢失期间超过16条的时钟跳变标记：计入`SourceState.owed_dropped`与`health["clock_markers_dropped"]`；恢复单元或终结组携带的第一条标记的`detail.earlier_markers_dropped`写明被淘汰的条数（不新增记录种类），入库确认后清零，单元被拒则下次再写。缺口与段身份不依赖这些标记（用例：同一次丢失在上限16与100下，单元里除标记外的记录完全相同）。新用例5项（`test_segment_identity.py`）。
- **SIM-01（已改）：** `research_config.py`去掉“估算超过预算即拒绝加载”两行；`scripts/research_lab.py`的启动警告不变。新用例`test_the_estimate_of_a_validation_run_never_decides_whether_a_configuration_loads`（预算1 GiB或1 KiB、时长20000或86400秒均可加载，估算仍如实给出“放不下”）；旧用例`…_and_overbudget`其实没有触发预算（它设的2 GiB配额下默认配置本就通过），改名并去掉该配额行。新用例在旧校验器上失败。
- **R07端到端（新）：** `tests/test_web_failure_records.py`（被`.gitignore`隐藏，提交须`git add -f`）：真实Chrome、真实应用与归档库；四轮协议异常——实时页显示“—”与“本轮协议异常”；库里每个桥只有1条`MISSING_EXCEPTION`；历史页表格含该记录（无数值、标红）、其前一值“至下一次变化”；CSV该行值为空、是否有效“否”、质量`MISSING_EXCEPTION`，前一行“有效至”＝失败时刻，该行“有效至”＝恢复时刻；再把行数上限定在恰好排除失败记录时，最后显示的一行仍止于失败时刻（旧`history.py`上失败）。重复运行10次全过；其间发现的偶发失败是Chrome的`datetime-local`在整分钟时去掉秒、Playwright`fill`报Malformed value（已在测试里避开；交接“工具坑”早有此记载）。该测试注入的是已成形的批次，走线的覆盖见`test_failure_records.py`的真实从站用例。
- **定向复验：** 历史相关12个文件139通过（早于(b)用例加入）；owner／归档相关11个文件127通过；模拟与存储配置23通过；新端到端10/10；ruff通过。代码提交`a8a6a0b`、文档提交`c761572`；当前收集842项（原827：历史+4、段身份+5、模拟+1、端到端+1、钉子释放+2、文档链接核对+2）。
- **整套连续运行（G2）：** 冻结候选`c761572`（代码提交`a8a6a0b`＋文档提交，工作区干净），单进程不分段、faulthandler 300秒（2026-10-20 14:07–14:23）：**842收集、842通过、0失败、0跳过、0 xfail**，983.2秒，无挂住；只有两条依赖弃用警告；运行后两个集群没有残留的测试库，保留库`htsg`、两个`htsg_research_*`没动。
- **G3刷新（候选`c761572`，`clean=true`；代码摘要`6c423179…`〔28个模块〕，页面`23e75d26…`、配置`6e1f673d…`、结构`2479b07e…`均与上一候选相同；报告在本地`docs/evidence/g3-20261020/`，不入库；旧证据`docs/evidence/r06/`保留不动）：**
  - D48烟测（`smoke-20261020-142516.json`）：启动、正常停止、同库重启通过；两个run均`NORMAL`，历史无重复、有序、无缺口；协商无违反（队列10、保活2、寿命60、会话超时60秒），归档源均OK，队列失败0。
  - D47抗故障（`resilience-all-20261020-143109.json`）：6次硬杀（启动中1次、满载5次）无半写组、无孤儿行，被杀的5个run按库内证据收尾为`ABNORMAL_EXIT`，最后一次正常停止的run为`NORMAL`（退出码3）；三库路由与滞回按预期（主库96%→外部，回落40%不回；移动盘96%→应急，80%仍应急，69%→外部），组标识唯一、历史完整。容量：满量变化417.8行/秒、一年约10.51 TB，判`INSUFFICIENT`——**一年容量仍不通过，结论不变**；静止工况9.2行/秒、一年约0.230 TB，判当前卷足够（不替代满量结论）。
  - D46标准＋Chrome（`measure-20261020-143437.json`）：120.1秒满量变化、双协议各断连25秒；417.8行/秒，入口丢弃／队列拒绝／拒写／失败均0；两次断连均在约0.5秒内察觉，UA约15.5秒、Modbus约10.7秒恢复有效，积压0.06秒内清空，归档状态OK；接收到绘制延迟p50 115.6／p95 123.8／p99 134.3／最大346.8毫秒（其中100毫秒合并等待中位105.4、最大328.5毫秒）；事件循环迟到最大144.3毫秒；内存154.8→166.0 MB；停机`run_end_status=COMMITTED`、未结清0。
  - D46静止600秒（`measure-20261020-144511.json`）：600.1秒静止满量（200点＋8路）：`expiries=0`，`resyncs=2`（仅两次受控停服），入口／队列／写失败均0，9.2行/秒；两次停服（约7.3、7.4秒）均在约0.5–0.7秒内察觉并重建、5.3／3.1秒恢复有效，归档OK；事件循环迟到最大173.2毫秒，提交等待最大402.3毫秒；停机`COMMITTED`。
  - 注意：`measure`的`noticed_after_seconds`只表示“至少一个输入无效”，不是告警时延；延迟最大值的偶发（事件循环迟到）原因仍未查明。
- **G3之外仍不变：** 真实Acheron设备、真实UA网关、独立卷、现场长稳均未验；一年容量按终版口径不通过；R03a大库收尾耗时待现场部署前实测。
- **变异检查证据留存：** 本地`docs/evidence/mutation-20261019/`（不入库）：脚本、三份报告、日志，以及逐个变异点的判定和理由（基线提交`8dea1c5`，28个变异点，26杀死、2存活；存活的两处`_discard_groups`冗余防御按“现有被测路径无差异”记录并保留）。
- **未覆盖：** N2覆盖表的逐行认领（SIM-03现行说明[HTSG_V1_模拟现场使用说明_v0.1.md](HTSG_V1_模拟现场使用说明_v0.1.md)与[场景覆盖表](HTSG_V1_模拟场景覆盖表_v0.1.md)已写入，历史说明不动）；R03a大库收尾耗时（现场部署前实测：长跑、大量观测、跨库、3秒查询超时之后的行为）；首次正式完整模拟；真实网关、现场设备、独立卷、一年容量与现场长稳不变。

### 整改补充：数据库构建步骤与实验库重建（2026-10-20）

**出处：** 用户2026-10-20起实验前端后发现：现有`htsg_research_*`和`htsg`都是帧式旧库，新链路拒绝它们，后端照常运行但没有归档、历史与故障列表503；用户要求全部清掉，按新合同重建，建库部分若还引用旧脚本就重写。

- **检查：** 建库脚本没有引用旧脚本或旧表结构，但也没有建表：`bootstrap_db.py`只建空库`htsg`，`research_lab.py`只建空的实验库，表结构要等后端首次启动才由`ArchiveStore.initialize`创建，旧库因此只在运行时显示为`refused`。
- **改（`c4e7125`）：** `scripts/bootstrap_db.py`新增`build`（把两实例的`htsg`建到当前归档代，已是当前代的照收，帧式或其他代的库直接报错）；`scripts/research_lab.py`的`prepare`（`run`、`new-lab`都经过）建好实验库后同样建表，库不可用就停下并提示`new-lab`；`tests/test_research_lab.py`新增2项（新实验库被建到当前代；帧式旧库使启动器停下），共9项通过；README与现行使用说明补上建库步骤。
- **清理（本地，未入库）：** 两个集群里的`htsg`（帧式，846帧、2个账户）和全部`htsg_research_*`已删除，实验库的本地绑定与归档记录已删除，6个`ui_test_*`测试账户已从实验库账户文件删除（`admin`、`viewer`保留）；随后用`bootstrap_db.py start`＋`build`＋`research_lab.py prepare`重建：四个库均为第5代、16张表、无旧表、行数为0。起实验前端实测归档`OK`、故障列表可用（该次运行的数据随后又清空重建）。
- **未改：** 启动估算`estimate_budget`仍是旧的“每帧值1024字节”模型（SIM-01只拿掉了它的拒绝作用，数值约为实测的2倍，仅用于提示）；`.local`下其余的测试与审计遗留文件未动；G3证据仍绑定`c761572`，本次只改`scripts/`、测试和文档，不在候选的代码摘要内。

### 整改补充：手动场景触发脚本与N2认领结论（2026-10-20）

**出处：** 用户2026-10-20问故障能否由前端按钮形成；回答是现在只能经模拟控制端口由脚本触发。用户要求评估“加完再取消”的成本：低就加按钮，有隐患就写一个可手动配置触发（不按顺序）的脚本；认领结论写在场景覆盖表后段。

- **评估：不做前端按钮。** 加按钮要改冻结的终版第15节（第7页职责与权限表“不允许在线修改配置”）；后端要多一条转发控制请求、持有模拟令牌的写路径，被测系统与独立模拟现场耦合；候选摘要里的`web.py`与`app.js`会变，绑定`c761572`的G2与G3证据作废、约50分钟重跑；取消时同样要回写设计并重跑。脚本在`scripts/`里，不在候选摘要内。
- **做：** `scripts/trigger_scenario.py`——读`.local/research/trigger.json`（仅放一个`scenario`，可选`research_config`），触发一个场景；`--list`列出场景与持续时间；多余的键、多个场景、未知场景名在发请求前就拒绝；模拟器未运行、令牌不对有明确报错；令牌不打印。新用例`tests/test_trigger_scenario.py`11项（含对模拟器真实控制应用的一次调用，被`.gitignore`隐藏，已`git add -f`）；对运行中的实验前端实测：`--list`、触发`thermal_step`（12秒后故障列表出现`temp_inner_01`与`delta_t_01`两条高限记录）、未知场景名被拒。
- **文档：** 现行使用说明第5节改为脚本用法并写明不做按钮的原因；[场景覆盖表](HTSG_V1_模拟场景覆盖表_v0.1.md)后段新增“认领结论”：8个可触发场景及预期现象（哪些在实验前端上手动看过、哪些没有）和15行逐行结论（模拟层认领4、13、15；夹具层认领1、2、3、6、11、12；7仅测量层；单元层接受5、8、9、10，其中8、9、10已由用户2026-10-20确认；14不认领）。结论里没有任何一行写成“模拟层已验收”。
- **未做：** 把1、2、3、6、8、9、10提到模拟层须给模拟器新增注入手段，另议；组合场景模拟器产生不了。实验库在这次实测后又清空重建（第5代、0行）。

### 整改补充：R03a收尾证据只用水位（2026-10-23）

**出处：** 项目硬规则（用户2026-10-20：一年运行不崩溃，断电等情况后以最简单稳定的方式重新投入运行）要求先量R03a大库收尾耗时；用户2026-10-23看过实测后选B（收尾证据只用水位）并要求同时做D（健康输出修正），草稿五点（`docs/drafts/04_R03a收尾证据改为水位_草稿.md`）全部确认，整改方案v0.2决策7不加注。

- **实测（本地证据`docs/evidence/r03a-20261023/`）：** 在一次性库里造一次崩溃的运行，观测N行＋结果N行，生产超时3秒：1M行0.22秒、4M行0.99秒，8M行`unfinished_facts`2.39秒而收尾里的运行证据语句3.18秒被取消，16M行起读取部分也被取消（不限时：16M行6.49秒、32M行15.85秒）。约6–8M行即超限，换算为满量变化约10小时、静止约7–9天的运行。**超时之后（真实`router.initialize`）：** 全有或全无，一个大运行使所有运行、所有库都不收尾；之后的小崩溃同样收不了尾；健康输出对可用的库报`UNAVAILABLE`／`PENDING_UNREADABLE`；每次重启重复同样的失败扫描；后端不崩溃、不卡住。
- **复现“不收尾的后果”：** 崩溃的段没有结束时刻，历史把它当作延续到无穷远，之后每个查询窗口都会把它的旧值作为“窗口起点的保持值”显示出来；收尾后消失。已保留为回归用例`test_a_crashed_segment_that_nobody_closed_is_held_into_every_later_window_until_the_start_up_close_out_ends_it`（`tests/test_history_query.py`，无水位与有水位两种）。
- **规则（已写入）：** 终版第5节追加一句；细则第5.1节末尾追加修订块、第11节约定U、V（细则仍为历史文件，原文未动）；实施方案头的现行设计SHA刷新为`a6bb00362e48ca249cd74029a7d43957d64d4b2ebdfe3cab61a52061560f99d2`，上一基线`109ace00…`保留。
- **代码（已改）：** `htsg/archive.py`——`_segment_evidence`只读`watermark`；`reconcile_unfinished_runs`以运行为单位各自成事务（新增`_close_run`、`CloseOutDeferred`），运行结束取起点与各水位最大值，段终点`max(段起点, 水位)`；`htsg/archive_router.py`——收尾失败写`DEFERRED:<库>:<原因类>`，不再标库`UNAVAILABLE`（库真不可读仍是`PENDING_UNREADABLE`），语句超时也归`DEFERRED`；`htsg/history.py`——`EXIT_TRUNCATED`段中时刻不早于终点的记录有效至取自身时刻。
- **用例：** `tests/test_exit_evidence.py`改写4项（运行结束时刻、无水位、水位之后的实存点、重申值不再算证据）、跨库中断用例改查`DEFERRED`，新增2项（收尾发出的SQL不涉及`observation`／`result`／`state_event`／`fault_record`；一个运行收尾失败时另一个仍被收尾、库不报不可用、下次启动补收）；`tests/test_capstone_d.py`杀进程用例的预期改为起点与水位；回归用例（崩溃段不被保持进之后的窗口）一起提交。相关9个文件定向复验通过；改前的代码上更新与新增的10项失败，回归用例两边都通过。
- **改后复测（本地`docs/evidence/r03a-20261023/`）：** 16M行崩溃运行（改前两条语句都被3秒超时取消），生产超时下`router.initialize`0.08秒，运行与6个段被收尾，健康输出`OK`。
- **代价（已知并接受）：** 崩溃段的终点最多比最后一条有效观测早约一个水位间隔（10秒）；不足10秒、没写过水位的短段终点取段起点；这些观测仍在库里、照常显示，只是不再被保持到更后面。
- **整套连续运行与G3（2026-10-23，冻结候选`ed0dafd`，`clean=true`，代码摘要`3f5e7630…`，页面`23e75d26…`、配置`6e1f673d…`、结构`2479b07e…`与上一候选相同；报告在本地`docs/evidence/g3-20261023/`，不入库）：**
  - **G2：** 单进程不分段、faulthandler 300秒（15:19–15:36）：**858收集、858通过、0失败、0跳过**，996.5秒，无挂住（较上次842：收尾证据2项、回归用例1项、触发脚本11项、建库2项）。
  - **D48烟测：** 启动、正常停止、同库重启通过，两个run均`NORMAL`，历史无重复、有序、无缺口。**D47：** 6次硬杀无半写组与孤儿行，被杀的5个run收尾为`ABNORMAL_EXIT`，最后一次正常停止为`NORMAL`；路由与滞回按预期；满量变化402.1行/秒、一年约10.14 TB仍判`INSUFFICIENT`（**一年容量不通过，结论不变**），静止约0.23 TB。**D46静止600秒：** 600.4秒，`expiries=0`，`resyncs=2`（仅受控停服），入口／队列／写失败均0，停机`COMMITTED`。
  - **D46标准＋Chrome的延迟比2026-10-20偏高**（两次重测：事件循环迟到p99 43–50毫秒〔上次17〕、最大269–333毫秒〔144〕，提交等待中位68–103毫秒〔27〕、p99 636–697毫秒〔172〕，接收到绘制p99 232–242毫秒〔134〕）；吞吐、无丢失、断连恢复（UA约16秒、Modbus约11秒）不变。**A/B排除了本次改动：** 同一台机器、同一时段，换回`c761572`的三个收尾相关文件（`archive.py`、`archive_router.py`、`history.py`）交替测三次——旧文件两次得到同样偏高的数字（迟到p99 43和45毫秒、提交等待p99 695和711毫秒），当前代码一次为36和544毫秒。所以偏高来自2026-10-23当天的机器状态（日期变过、像是重启过；原因未查明），不是这次改动；本次改动也没有碰运行期路径。
- **未覆盖：** 现场项（真实设备、网关、独立卷、现场长稳）；一年容量按终版口径不通过；D46延迟偏高的机器侧原因未查明；N2覆盖表的逐行认领与首次正式完整模拟。

## 2. 偏离、风险与未验收

- **页面集合与用户意图不一致（2026-10-13 用户指出并已确认纠偏方向）：** 用户要的是参考原型 `htsg_hmi.html` 里按**显示顺序**排的第 1、2、4、5（新增历史查询）、12、14、15 页，其中第 14 页是“通信·日志”、第 15 页是“系统管理·权限”。原型的导航是 `PAGES` 数组按位置编号（`i+1`）：…第 12 页系统自检、第 13 页算法引擎·数字孪生、第 14 页通信·日志、第 15 页系统管理·权限；而它的元素 id 是另一套（`p13` 通信·日志、`p14` 系统管理·权限、`p15` 算法引擎·数字孪生）。**原终版设计第 15 节的表按元素 id 写成了“14 系统管理·权限、15 算法引擎·数字孪生”**（该表在提交 `40750c9` 随终版定稿进入），实施方案 D44 与我的实现照着这张表做，于是：①交付了设计里“沿用入口、仅显示轻量模型参数”的**算法引擎·数字孪生页**（模型页，原型里是四层算法，设计又把它削成参数页——这是设计与实现共同造出来的页）；②**没有通信·日志页**。而更早的前端（提交 `40750c9` 的 `app.js`，D36 之前）恰好是 `[1,2,4,5,12,14 通信·日志,15 系统管理·权限]`，后端有 `GET /api/communications`（协议读数与映射、实际错误与恢复记录，最近 500 条内存记录，`dev-docs/history/模拟现场使用说明.md` 第 59 行仍在描述它）。D36–D44 的重写与 D45 对旧链路的清除把这个页和这个接口一并丢了——这是我按设计表做的、没有向你单独指出的**功能丢失**，不是你要求的。**当前处置：此前已按用户指示移除数字孪生页及其专属接口。** 移除后的六页是过渡状态，目标七页未完成；本轮已按用户授权完成[通信页审核处置](HTSG_V1_通信日志页设计审核_v0.1.md#review-disposition)，回写终版并拟定实施方案，通信页代码尚未实现。此前提前细化的任务拆分曾撤回；本次以审核处置后的终版与实施方案为准，历史接口只作为功能丢失经过的证据。**2026-10-14：通信·日志页已实现并验收（D44a–D44f），现为七页，页面集合与用户意图一致。**
- **页面不标页序号（用户指示，已做）：** 导航按钮与页面标题上的“01/02/04…”“HTSG / 14”已去掉，只留标题；用例改为按标题核对。`docs/evidence/capstone-g/` 里已有的截图仍带旧编号；2026-10-14 已按七页重拍整套到 `docs/evidence/d44/`，旧目录保留为历史，不再引用。
- 方案"位置"列写的是改造旧文件；本次以新模块并存，D45 再删旧（见第 0 节）。
- **实现取值（非设计明文，需用户知晓）：** ① `Timing.ua_silence_margin=0.5` 秒（终版要求"静默+裕量<有效期"但未给数）；② 从未有过有效值的派生量首次算出 NA 时不写记录；③ 水位只约束开放段最后一条记录的尾部；④ Modbus 每轮原始观测逐轮存储，派生结果仅变化时存；⑤ `config_id` 改为键排序规范化哈希。
- **【已由R03b关闭（2026-10-16），原文保留作历史】已知但未修、未用例验证（按代码推断）：** `lost_seg` 只修了"丢失开始时该段开着、恢复尝试被拒"这条路径。下列情形仍可能让缺口前落库的段 `end_wall` 为 NULL：① 丢失期间来源又 EXPIRED/RESYNC（`SEG_END` 因 LOST 未入队，恢复单元看到 `open_segment=False` 不再补）；② 引发丢失的正是含 `SEG_START` 的组被拒（新段从未落库，旧段的结束也在被拒的同组里）；③ `RECOVERING→LOST` 后恢复单元又被策略拒绝（`lost_seg` 已被改记成恢复单元的新段）。这几种情形下回放仍受缺口行与水位约束，但段本身显示为开着；要修需在丢失期间把"已落库段的收尾事实"记下来、恢复时补发，D45 接线前后择机做。该条所述路径已被R03b的持久段身份（owner记录归档自己持有的开放段与待确认的段尾）取代，对应用例在 tests/test_segment_identity.py。
- **现场必须核验：** 未适配的asyncua测试服务器的保活比协商值晚**两个**发布周期（间隔＝计数＋2个周期，N1探针与R06实测：计数2约4秒、计数1约3秒；本条原写“一个周期”不正确）；项目自建的模拟服务器实例已局部适配（R06）。真实Acheron、UA网关、独立卷、一年容量均未验。
- 会话预算：用户"用尽 session limit"指 5 小时会话额度；CLAUDE.md 的 30,000 token 条款，用户随后明确说"别控制预算了，我自己看着来"——预算由用户掌控，执行时不自行限额、不为预算停下或提问。

## 3. 交接（新会话从这里开始）

**整改轮交接（更新于2026-10-23，R03a收尾改动及其G2／G3之后）：**
- **提交状态：** R01–R07与审核补充已提交为`8dea1c5`，测试加固`082dd19`，记录`443190a`；均在本地分支`dev/htsg-v1-impl`，**未推送**（领先`origin/dev/htsg-v1-impl`数个提交，以`git status`为准；不开PR）。2026-10-20的代码改动（历史截断修复、第10项边界与void组钉子释放、SIM-01、R07端到端）已提交为`a8a6a0b`；其后只有文档提交，冻结候选＝文档提交。整改开发细则已入库并转为历史：[HTSG_V1_全量测试前整改开发细则_v0.1.md](HTSG_V1_全量测试前整改开发细则_v0.1.md)（适用于`946cb5d`至`082dd19`）。新测试`tests/test_web_failure_records.py`被`.gitignore`隐藏，已`git add -f`入库。
- **三档状态，互不替代：**
  - **G2（单进程整套连续运行）：** 对`8dea1c5`／`082dd19`候选，第二次整套827/827干净通过（1002秒，无挂住）；第一次825/826、1失败（R05时序，已查明并加固）如实保留。对冻结候选`c761572`：842/842通过、983秒、无挂住（2026-10-20）。**对R03a收尾改动后的冻结候选`ed0dafd`：858/858通过、996.5秒、无挂住（2026-10-23）。**
  - **G3（D46／D47／D48证据绑定候选摘要）：** 已于2026-10-20对冻结候选`c761572`刷新（`clean=true`，代码摘要`6c423179…`，28个模块；报告在本地`docs/evidence/g3-20261020/`，不入库，要点见上条）；旧证据（绑定`946cb5d`之后的旧摘要`830e096e…`）保留不动。**2026-10-23对`ed0dafd`再刷新一轮**（`clean=true`，代码摘要`3f5e7630…`，其余摘要不变；`docs/evidence/g3-20261023/`）：D48、D47、D46静止600秒通过，D46标准＋Chrome延迟偏高但A/B排除了本次改动（见R03a条目）。**一年容量仍不通过。**
  - **现场（G4）：** 真实Acheron设备、真实UA网关（尤其保活与队列修订）、独立卷、现场长稳均未验；一年容量按终版口径**不通过**，结论不变；R03a大库收尾耗时已于2026-10-23在开发实例上实测并解决（收尾只用水位，16M行崩溃运行0.08秒收尾）。
- **开放问题：** 首次正式完整模拟前须完成SIM-03说明并认领N2覆盖表；模拟器能产生的终版场景有限（N2表）。
- **已完成与剩余（用户2026-10-20定的顺序）：** 文档草稿已批准写入、代码与文档已提交冻结（`a8a6a0b`、`c761572`），G2与G3已完成。剩余：推送与否由用户定（目前不推送）；N2覆盖表的逐行认领与首次正式完整模拟。保留库`htsg`、`htsg_research_*`不属缓存清理范围，只清测试自建的临时库。
- **不要做：** 不推送；不用“G2通过”表示所有正确性问题已关闭（本轮历史截断就是G2通过之后才发现的）。

以下三段（2026-10-16）及其后“状态（2026-10-14）”是历史交接：其中的测试数字（771、770、631、632等）和状态（“G2连续全套尚未通过”“工作区累计、不提交”等）已被上面一段取代，保留作记录，不改动。

**整改轮交接（更新于2026-10-16，R04之后）：** 已完成并定向复验R01、R02、R03a、R05、R07、R03b、R04；本次接手另补R02聚合省略标志、R03a跨库收尾中断续跑及R03b长期LOST关闭意图上界。下一批按细则为R06（模拟服务器保活适配与候选证据刷新）。当前工作区仍按用户此前决定累计、不提交；新增且被`.gitignore`隐藏的测试包括`test_cancellation.py`、`test_detail_bounds.py`、`test_exit_evidence.py`、`test_partial_requests.py`、`test_failure_records.py`、`test_segment_identity.py`、`test_shutdown_commit.py`，提交时须显式加入。R06前仍需注意上述间歇失败与严格xfail；G2连续全套尚未通过，不得以定向测试代替。

**整改轮交接（更新于2026-10-16，G2之后）：** R01、R02、R03a、R05、R07、R03b、R04、R06的实现、定向验证、D46/D47/D48刷新及771项连续全套均已完成；第二次G2为770通过、1 strict xfail、0失败。新增被忽略测试包括`test_simulated_keepalive.py`和`test_shutdown_commit.py`。工作区仍按用户决定累计、未提交。下一项需要用户口径的是R02未转义`@`边界；完成后应重跑对应安全用例并决定是否再做最终提交/推送。容量满量不通过、真实设备/网关/卷与现场长稳仍是独立开放项。

**整改轮交接（更新于 2026-10-16，R03b 之后）：** 本轮按[整改开发细则](HTSG_V1_全量测试前整改开发细则_v0.1.md)（临时文件，按用户决定不提交）和[整改方案 v0.2](HTSG_V1_全量测试前整改解决方案_v0.2.md)执行，顺序 R01 → R02 → R03a → R05 → R07 → R03b → R04 → R06；基线 `946cb5d`，分支 `dev/htsg-v1-impl`。**提交方式（用户 2026-10-15 答复）：** 各批攒在工作区，G1 达成或用户指定节点前不提交；新建的测试文件允许 `git add -f`（`.gitignore` 有 `tests/`、`docs/`）；目前新增 `tests/test_cancellation.py`、`tests/test_detail_bounds.py`、`tests/test_exit_evidence.py`、`tests/test_partial_requests.py`、`tests/test_failure_records.py`、`tests/test_segment_identity.py`。**已做：** R01（`modbus.py`、`runtime.py`）、R02（`commlog.py`、`records.py`、`modbus.py`、`owner.py`、`app.js`）、R03a（`archive.py`、`archive_router.py`、`history.py`）、R05（只改测试：`protocol_support.py` 的多从站夹具＋`test_partial_requests.py`）、R07（`records.py`、`owner.py`、`history.py`）、R03b（`owner.py`；另按“提交桩要确认接纳的组”改了六个既有测试文件的桩，逐条在 R03b 条目），见各自条目。**下一批：** R04（停机按提交结果收尾；改 `runtime.py`、`commitq.py`、`owner.py`）。终结组要登记成该源的 `recovery_order`，并沿用 R03b 的四件事实：`durable`（档案里开着的段）、`closing`（已离开未确认的段终点）、`inflight`（已被队列接纳未结清的组）、`recovery_order`（每源至多一个在途单元）；先读 R03b 条目。之后是 R06。**已知卡点／待用户裁定：** ① 既有用例 `test_modbus_acquisition.py::test_one_timeout_keeps_the_connection_three_in_a_row_reconnect` 间歇失败（测试服务端给延时应答打最新请求的事务号），证据与建议改法在 R01 条目，未改动；R03b 之后的整套连续运行里它又失败了一次；② R02：口令含未转义 `@` 时“最近一个 `@`”规则只遮到第一个 `@`，`test_comm_log.py` 一条参数暂标严格 xfail，选项在 R02 条目；③ R03a 的四处实现取值（跨库段证据取各库最大值、旧结构库也按不可读推迟收尾、晚于段终点的行夹到自身时刻、大库耗时未测）在 R03a 条目；④ R05 发现的既有行为：两个从站都沉默时，pymodbus 在第3轮自己关连接，该轮记 `CONNECTION`，重连原因是 `DOWN:CONNECTION` 而不是 `DOWN:TIMEOUTS`（未改，见 R05 条目）；⑤ R07 的可见变化：实时视图里失效点的 `value` 现在是 `null`、`quality` 是 `MISSING_*`，满量下每次失败转移最多 +8 条记录（容量报告未改），见 R07 条目；⑥ R03b 有两处超出细则文字：本源有已接纳未结清的组时不建恢复单元（`inflight` 门），以及已 LOST 且无在途单元时更早的组被拒，缺口起点取较早者；另有三点需知悉：同一段的 `SEG_END` 极少数交错下会写两次、终点相同（`segment` 是幂等 upsert），`closing` 理论上无上界，恢复单元要等在途组结清、缺口可能略长，见 R03b 条目；⑦ 整套连续运行做过两次（R03b 之后的工作区，不是 G2）：第一次在 `test_measure.py` 的 push-latency 用例（有浏览器参与）处停住，原因未明；第二次收集 746，744 通过、1 失败（上面①）、1 xfail，813.66 秒，没有挂住，也没有用例超过 100 秒，数字与堆栈见 R03b 条目；⑧ 开发库开机后默认不启动，2026-10-16 已执行 `bootstrap_db.py start`（55432/55433 在听）。

**状态（2026-10-14）：** D01–D48 的编码、测量与交付件都做了，旧链路已由用户删除；**通信·日志页（D44a–D44f）已实现并验收，现为七页**（总览、应变场、热应力、历史查询、自检、通信·日志、权限；只标标题、不标页序号、不做移动端布局）。全部测试文件分两段运行 **631 通过 / 0 失败 / 0 跳过**，其后三处小改各自复跑，现共 632 项（D44f 条目）。**但仍不能说收口**：① 整套**连续**运行没有通过记录——pymodbus 3.15.0 把取消改抛为 `ModbusIOException`、`ModbusAcquirer.poll` 当超时继续（`modbus.py` 的 `except (ModbusIOException, TimeoutError)` 一支）导致取消丢失，连续运行会随机挂住，用户定为独立问题本轮不修；另有一次 `test_capstone_c` 首个用例挂住未查明、未复现；开发库实例没起来也会让 DB 用例卡住（机器重启后实际发生过）；② **一年容量按终版口径不通过**（交付记录第 5 节）；③ 真实 Acheron、UA 网关、独立卷、现场长稳均未验证；④ **设计侧文档仍写着“通信页尚未实现”**（终版第 15 节表格与 15.1 开头、方案头部与第 12 节、核验文件），我没有动——终版哈希写在方案头，回写由用户决定。开发库实例开机后默认不启动，先 `bootstrap_db.py start`。

**进入新会话的第一步：**
```powershell
.venv/Scripts/python.exe scripts/bootstrap_db.py start      # 开发库集群默认不启动；不启动则所有 DB 用例 130 秒后 ConnectionTimeout
.venv/Scripts/python.exe -m pytest -q                        # 分段运行约 11 分钟（连续运行会随机挂住，见 H0 条目与 D44f 条目）；输出里的连接串含凭据，落盘前用 sed 脱敏
git log --oneline -8; git status --short
```

**新链路代码地图（均已就绪、已测，但 Runtime 尚未接线，D45 才切）：**
`records.py`（记录与确定性 ID）→ `owner.py`（单所有者：批次接纳、有效性、派生、change 基线、来源状态机 WAITING/VALID/EXPIRED/RESYNC、告警、同代次恢复、段/时钟跳变/水位、归档状态 OK/LOST/RECOVERING、故障接入与缓冲）；`derive.py`（算子）；`fault_tracker.py`（5 秒计时/回差）；`ingress.py`（有序入口：批次+代次/断连/失步事件）；`modbus.py`、`ua.py`、`recovery.py`（采集与退避）；`commitq.py`（有序队列）→ `archive_router.py`（整组路由）→ `archive.py`（`ArchiveStore.write_group`）+ `archive_schema.sql`；`history.py`（读取、依据、故障列表）。通信·日志（D44a–D44f）：`commlog.py`（本次运行内存日志：聚合、500 条先进先出、详情脱敏截断）、`ingress.py` 的诊断入口（单独计数、上限 500）、`owner.py` 写日志与 `communication_inputs()`、`web.py` 的 `/api/health`、`static/app.js` 第七页；其测试 `test_resync_health.py`、`test_comm_log.py`、`test_comm_facts.py`、`test_comm_protocols.py`、`test_health_communication.py`、`test_web_comm.py`。测试支撑：`tests/owner_support.py`、`protocol_support.py`、`live_support.py`、`fault_support.py`、`conftest.py::archive_db_factory`。

**D45 接线清单（Runtime 现仍是旧帧链路）：** `router.initialize(run_id)`+`bind_run` → `owner.start` → 每个启用源建 `ModbusAcquirer`/`UaAcquirer`，`hub=Ingress`，`owner.on_rebuild=uaa.request_rebuild`（多源需分派）→ `CommitQueue(on_committed=owner.committed, on_refused=owner.refused, is_void=owner.is_void)`，`owner.commit=queue.offer` → 事件循环里每秒 `owner.tick(time.time(), time.monotonic())`，`ingress.drain()` 由唤醒驱动 → 停止顺序：停采集、排空 ingress、`owner.shutdown`、`queue.stop`。要删的旧模块：`engine.py`、`runtime.py` 旧部分、`calculation.py`、`faults.py`、`adapters.py`（`decode_registers` 先搬进 `modbus.py`）、`storage.py`、`schema.sql`、`capture.py`；旧测试按第 1 节 D01 映射处理；`config.py` 中 `profiles/stale_after/max_age/derivative/aggregation/Source.timeout` 等旧字段随之删除。

**Capstone G 历史记录：曾按错误页面集合通过，纠偏后的页面验收尚未完成。**（用例 `tests/test_capstone_g.py`，存证 `docs/evidence/capstone-g/`，重跑存证用 `HTSG_EVIDENCE_DIR=docs/evidence/capstone-g`）。

**模块 G 历史实现记录（页面当前状态见本节首段）：** 后端 `publisher.py`（100 ms 打包、版本快照、心跳）、`stream_api.py`（有界发件箱、5 秒发送超时、重连取快照）、`web.py`（页面、身份、`/api/ui-config`、`/api/health`、账户与审计、历史路由）；前端 `htsg/static/{index.html,app.js,stream.js,replay.js,style.css}`，七页。`stream.js`/`replay.js` 是浏览器与 Node 测试共用的纯函数（`tests/js/*.mjs`）。测试台 `tests/web_rig.py`（服务线程里跑真实 owner/publisher/socket，可选一次性归档库，`feed_archive=False` 时由测试直接写归档）与 `conftest.py` 的 `browser/rig/archive_rig/replay_rig/page` 夹具；浏览器是 Playwright + 本机 Chrome（`channel="chrome"`）。新链路仍未接入运行入口（D45）；`scripts/validate_*.py` 针对旧页面，已失效。

**工具坑：** Bash 工具里超过约 8–10 KB 的 heredoc 会被截断（报 `unexpected EOF`）——大补丁用 Write 写到 scratchpad 再 `python` 执行；`Edit` 前必须先 `Read`；Write 工具写出的文件在工作区是 LF，git 提交时按仓库约定统一成 CRLF（只会有"LF will be replaced by CRLF"告警）。pytest 输出在连接失败时会带凭据。Chrome 的 `datetime-local` 在秒为 00 时把 `value` 规范成不带秒，Playwright 的 `fill("…:00")` 会报 Malformed value，测试里用非零秒。Playwright 同步 API 的路由处理函数里不能阻塞等待（会卡死分发线程），要"扣住请求、测试体里再 `route.continue_()`"。变异试验不要在源码上原地改了再靠 `cp` 还原。 变异验证用 `scratchpad` 里的临时副本（复制 `htsg/ tests/ config/ .local/` 到别处，用 `.venv` 的 python 在副本目录跑，先核对 `htsg.__file__` 指向副本）。Windows 控制台输出中文会乱码，pytest 前设 `PYTHONUTF8=1`。Bash 工具会吃掉 heredoc 里一层反斜杠（写含 `\\` 的 JSON/正则时用 `chr(92)`）；`sleep` 被禁，等条件用 `until` 循环或 Monitor。工作区里 `.css`、记录文档是 CRLF，用脚本改要按二进制或指定换行写。
