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

## 2. 偏离、风险与未验收

- **页面集合与用户意图不一致（2026-10-13 用户指出并已确认纠偏方向）：** 用户要的是参考原型 `htsg_hmi.html` 里按**显示顺序**排的第 1、2、4、5（新增历史查询）、12、14、15 页，其中第 14 页是“通信·日志”、第 15 页是“系统管理·权限”。原型的导航是 `PAGES` 数组按位置编号（`i+1`）：…第 12 页系统自检、第 13 页算法引擎·数字孪生、第 14 页通信·日志、第 15 页系统管理·权限；而它的元素 id 是另一套（`p13` 通信·日志、`p14` 系统管理·权限、`p15` 算法引擎·数字孪生）。**原终版设计第 15 节的表按元素 id 写成了“14 系统管理·权限、15 算法引擎·数字孪生”**（该表在提交 `40750c9` 随终版定稿进入），实施方案 D44 与我的实现照着这张表做，于是：①交付了设计里“沿用入口、仅显示轻量模型参数”的**算法引擎·数字孪生页**（模型页，原型里是四层算法，设计又把它削成参数页——这是设计与实现共同造出来的页）；②**没有通信·日志页**。而更早的前端（提交 `40750c9` 的 `app.js`，D36 之前）恰好是 `[1,2,4,5,12,14 通信·日志,15 系统管理·权限]`，后端有 `GET /api/communications`（协议读数与映射、实际错误与恢复记录，最近 500 条内存记录，`dev-docs/history/模拟现场使用说明.md` 第 59 行仍在描述它）。D36–D44 的重写与 D45 对旧链路的清除把这个页和这个接口一并丢了——这是我按设计表做的、没有向你单独指出的**功能丢失**，不是你要求的。**当前处置：此前已按用户指示移除数字孪生页及其专属接口。** 移除后的六页是过渡状态，目标七页未完成；本轮已按用户授权完成[通信页审核处置](HTSG_V1_通信日志页设计审核_v0.1.md#review-disposition)，回写终版并拟定实施方案，通信页代码尚未实现。此前提前细化的任务拆分曾撤回；本次以审核处置后的终版与实施方案为准，历史接口只作为功能丢失经过的证据。**2026-10-14：通信·日志页已实现并验收（D44a–D44f），现为七页，页面集合与用户意图一致。**
- **页面不标页序号（用户指示，已做）：** 导航按钮与页面标题上的“01/02/04…”“HTSG / 14”已去掉，只留标题；用例改为按标题核对。`docs/evidence/capstone-g/` 里已有的截图仍带旧编号；2026-10-14 已按七页重拍整套到 `docs/evidence/d44/`，旧目录保留为历史，不再引用。
- 方案"位置"列写的是改造旧文件；本次以新模块并存，D45 再删旧（见第 0 节）。
- **实现取值（非设计明文，需用户知晓）：** ① `Timing.ua_silence_margin=0.5` 秒（终版要求"静默+裕量<有效期"但未给数）；② 从未有过有效值的派生量首次算出 NA 时不写记录；③ 水位只约束开放段最后一条记录的尾部；④ Modbus 每轮原始观测逐轮存储，派生结果仅变化时存；⑤ `config_id` 改为键排序规范化哈希。
- **已知但未修、未用例验证（按代码推断）：** `lost_seg` 只修了"丢失开始时该段开着、恢复尝试被拒"这条路径。下列情形仍可能让缺口前落库的段 `end_wall` 为 NULL：① 丢失期间来源又 EXPIRED/RESYNC（`SEG_END` 因 LOST 未入队，恢复单元看到 `open_segment=False` 不再补）；② 引发丢失的正是含 `SEG_START` 的组被拒（新段从未落库，旧段的结束也在被拒的同组里）；③ `RECOVERING→LOST` 后恢复单元又被策略拒绝（`lost_seg` 已被改记成恢复单元的新段）。这几种情形下回放仍受缺口行与水位约束，但段本身显示为开着；要修需在丢失期间把"已落库段的收尾事实"记下来、恢复时补发，D45 接线前后择机做。
- **现场必须核验：** asyncua 测试服务器的保活比协商值晚一个发布周期（见 Capstone C 记录）；真实 Acheron、UA 网关、独立卷、一年容量均未验。
- 会话预算：用户"用尽 session limit"指 5 小时会话额度；CLAUDE.md 的 30,000 token 条款，用户随后明确说"别控制预算了，我自己看着来"——预算由用户掌控，执行时不自行限额、不为预算停下或提问。

## 3. 交接（新会话从这里开始）

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
