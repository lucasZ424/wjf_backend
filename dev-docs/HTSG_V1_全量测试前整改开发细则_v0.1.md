# HTSG V1 全量测试前整改开发细则（临时，已归档为历史）

版本：v0.1｜日期：2026-10-15｜代码基线：`946cb5d`（`htsg/`、`scripts/`、`config/` 自 `de5f644` 起未变）｜终版规范化 SHA：`109ace007f2ec96d67e4744ebb77d3fba37226e3d2eea446e82db4b3220b7501`｜分支：`dev/htsg-v1-impl`

**用途：** 供新开的开发会话执行[整改方案 v0.2](HTSG_V1_全量测试前整改解决方案_v0.2.md)。本文把 v0.2 已定稿的内容展开为可执行细则：每批改什么、不许改什么、做到什么程度算完成。


**效力顺序（发生冲突时）：**

1. [终版设计](HTSG_V1_终版设计_v1.0.md)；
2. [整改方案 v0.2](HTSG_V1_全量测试前整改解决方案_v0.2.md)及其中的用户确认；
3. 本细则；
4. [开发实施方案](HTSG_V1_开发实施方案_v1.0.md)的通用执行规则。

后者不得被解读为放宽前者。本细则与上述文件冲突、或遇到本细则没有覆盖的情况时，**停下并向用户报告**，不得自行选择口径。

本细则中标注“**【细则约定】**”的条目，是为消除歧义从 v0.2 推导出的实现约定，不是 v0.2 原文，已在第11节集中列出，并已于2026-10-15由用户全部确认。

**版本状态（修订2026-10-20）：** 本文件由用户2026-10-20裁定入库归档，**历史身份**：整改轮（R01–R07）的开发细则，适用于基线`946cb5d`至候选`8dea1c5`／`082dd19`；自入库起不再修改。其后的改动（2026-10-20的历史截断修复、第10项边界、SIM-01、R07端到端）及之后的任何工作，以执行记录为准，不受本文约束。现行规定以终版设计为准。
**有效期：** 整改轮的G2已于2026-10-20对`082dd19`取得（第二次整套827/827，见执行记录“候选检查”条目）；本文自入库起转为历史。

**修订（2026-10-19）：** 按用户裁定修订第8.1、8.2、11节：停机开始时在途的恢复单元被明确拒绝后，补发终结组，被收下即可登记 NORMAL（第11节 J）。同时写入终结组遇队列满的重交（K）、writer 退出的实现方式（L）、终结组不带故障缓冲（M）。其余各节不变。

---

## 1. 总则（对所有批次都成立）

### 1.1 不允许的事

1. **不改合同。** 不修改终版、开发实施方案、问题清单、复核意见书和整改方案 v0.1/v0.2。唯一例外：R06 第4步（第10.2节）要求修正的交付记录第66行量值，以及第12节规定的执行记录更新。
2. **不改运行参数。** `config/*.json` 和 `htsg/config.py` 的 `Timing`、`Limits`、`Storage` 默认值一律不变；尤其不改 `ua_data_validity`（3秒）、`ua_keepalive_count`（2）、`modbus_request_timeout`（0.8秒）、`modbus_reconnect_timeouts`（3）、`backoff_*`、`STOP_WRITE_WAIT`（8秒）。
3. **不扩范围。** 不新增跨源同步、Republish、补传缓存、通用 outbox、重模型、死点/跳变/冻结诊断、空间插值、按点告警阈值、旧历史迁移；不新增表、列或归档记录种类（R07 的失效记录复用 `observation` 表，见第7节）；不新增 API 路径、页面或现场控制入口；不做移动端布局。
4. **不动 Modbus 轮次语义。** 不改 `ModbusAcquirer.poll` 的整轮定性（[modbus.py](../htsg/modbus.py)第169–176行），也不改 `session` 的连续超时计数（第217–222行）；终版第9、11节已写明“超时轮指整轮无有效协议响应”。
5. **不改第三方库。** 不改 `.venv/Lib/site-packages` 下任何文件，不对 asyncua、pymodbus 的类做全局猴子补丁；asyncua 锁定 2.0.1、pymodbus 锁定 3.15.0，不升级。
6. **不原地变异源码。** 不在工作区改源码做变异试验；需要变异时在临时副本里做，工作区不留痕迹。
7. **不碰保留的数据库。** 不迁移、清空或删除 `htsg` 与 `htsg_research_*` 库，不改 `.local/research/` 下的绑定；测试只用 `conftest.py` 的 `archive_db_factory` 建的 `htsg_test_<32位hex>` 临时库（或 measure/smoke 自建的同类前缀库），用完即删。
8. **不打印凭据。** 不打印、不复制 `.local/database.json`（其中有一个顶层明文 `password` 字段）、`.local/research/*.json`、`initial-credentials.json`。需要端口时用 `netstat` 或按字段解析，只输出主机和端口。
9. **不推送。** 只做本地提交；是否提交、多久提交一次，以用户在开发会话开头的答复为准，未得到答复前不提交。

### 1.2 必须做的事

1. **先写复现用例。** 每批先写能失败的用例，在当前代码上确认它失败，再改最小路径让它通过；改前改后各记一次结果。
2. **用例要说明理由。** 每个新用例的 docstring 写明它保护的是哪条终版条款或哪个 Q 号问题，不能只断言硬编码输出。
3. **同步调用方。** 改函数签名或记录类型时，直接调用方、序列化（`as_dict`、OpenAPI `docs/openapi.json`、`HEALTH_SCHEMA`）和读取方（`history.py`、`app.js`）一并更新。
4. **每批完成立即写执行记录。** 格式见第12节；未通过或未覆盖的如实写。
5. **发现新问题就停。** 发现本细则和 v0.2 都没列出的缺陷，记为“待用户裁定”并停止扩展，不得顺手修。

### 1.3 环境

- **启动数据库：** 机器重启后开发库不会自动启动，先执行 `.venv/Scripts/python.exe scripts/bootstrap_db.py start`，再确认 55432/55433 在监听。库没起来会让数据库用例卡住，不要误判为代码问题。
- **测试命令：** `PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest <文件> -q -p no:cacheprovider --basetemp=<已存在的临时目录下的新子目录> -o faulthandler_timeout=120`。`--basetemp` 的父目录必须事先存在；后台运行时直接把输出重定向到文件，不要经管道缓冲。
- **其他检查：** `ruff check htsg tests scripts`、`node --check htsg/static/app.js`（改了前端时）、`git diff --check`。
- **Q01 修好之前**，任何真实 Modbus 夹具的收尾都可能因取消被吞而挂住；挂住时记录堆栈，不能强杀了事，也不能把挂住记为通过。

---

## 2. 批次总览与顺序

| 批次 | 问题 | 优先级 | 依赖 | 可否并行 |
| --- | --- | --- | --- | --- |
| R01 | Q01 取消被吞 | P1 | 无 | 可 |
| R02 | Q04 脱敏、Q06 详情入队 | P1/P2 | 无 | 可 |
| R03a | Q02 崩溃与跨库收尾 | P1 | 无 | 可 |
| R03b | Q07 持久段身份 | P2 | 无 | 改 `owner.py`，与 R02、R04、R07 串行整合 |
| R04 | Q03 停机顺序及补充 A | P1 | R01、R03b；跨库验收另依赖 R03a | 否 |
| R05 | Q05 回归用例 | P2 | 无（终版已回写） | 可 |
| R06 | V04 保活适配、V03 证据刷新 | — | 无；须在 G3 前完成 | 可 |
| R07 | Q08 失败轮失效记录 | P1 | 无；验收③用 R05 的服务端 | 改 `owner.py`，串行整合 |

**建议顺序：** R01 → R02 → R03a → R05 → R07 → R03b → R04 → R06；然后 G1 → G2 → G3。四批都改 `owner.py`（R02、R03b、R04、R07），同一时间只开一批，一批合入后再开下一批。

---

## 3. R01：恢复可靠取消（Q01）

### 3.1 现状

pymodbus 3.15.0 在请求等应答时，把外部取消转换为 `ModbusIOException(...) from CancelledError`（[transaction.py](../.venv/Lib/site-packages/pymodbus/transaction/transaction.py)第227–231行）。[modbus.py](../htsg/modbus.py)第155–158行把它当作超时继续执行，`Runtime._end`（[runtime.py](../htsg/runtime.py)第149–152行）只发一次 cancel，随后 `gather` 无期限等待。复核实测：对端沉默时 12 次取消里有 11 次被吞。

### 3.2 改动

1. **[modbus.py](../htsg/modbus.py) `poll` 的异常分支（第155行）：**
   - 捕获 `ModbusIOException` 或 `TimeoutError` 后，若 `asyncio.current_task().cancelling() > 0`，立即 `raise asyncio.CancelledError`，可用 `from` 保留原异常；
   - 否则维持现行“记 TIMEOUT、继续下一请求”的行为，不变。
2. **判据只有一条：** 本任务是否有未处理的取消请求。异常因果链（`__cause__` 是否为 `CancelledError`）只作日志佐证，**不得**单独作为重新抛出取消的条件；不得匹配异常文本。
3. **[runtime.py](../htsg/runtime.py) `_done`（第109–114行）：** `self.running` 为真时，任务以 cancelled 结束也记 fatal（`health["fatal"]` 写 `"<任务名>: CANCELLED"`），并置 `failed`；`self.running` 为假（正常停机）时行为不变。
4. **不做：**
   - 不给 `_end` 加超时；
   - 不循环重复 cancel，不加固定 sleep；
   - 不改 `ModbusAcquirer.run` 的重连与退避；
   - 不改 UA 侧（[ua.py](../htsg/ua.py)第433–434行已正确重抛）。

### 3.3 验收（新用例，放在 `tests/test_modbus_acquisition.py` 或新建 `tests/test_cancellation.py`）

1. 真实 `AsyncModbusTcpClient` 加本机回环对端：对端收到请求后，用事件确认请求已到达，然后暂扣应答；此时单次 `task.cancel()`，任务须在 2 秒内结束，客户端已关闭，取消后不再产生 `TIMEOUT` 批次，`attempt_failed`、`source_down` 也不再被调用。
2. **对照：**
   - 对端即答时取消；
   - 两轮之间（节拍睡眠中）取消；
   - 未取消时真实请求超时：仍产出 `TIMEOUT` 批次、任务继续运行。
3. **反例：** 用假客户端抛出 `ModbusIOException(...) from CancelledError()`，但本任务没有被取消：采集任务不得结束，该轮记 `TIMEOUT`。
4. **运行时：**
   - 运行期间让一个采集任务以 cancelled 结束，`runtime.health["fatal"]` 非空且 `failed` 被置位；
   - 正常 `stop()` 期间的取消不报 fatal。
5. **收尾回归：** `tests/live_support.py` 的 `Rig.__aexit__` 与 `tests/test_simulation.py` 的 `Chain.__aexit__` 在对端慢响应（如 `FieldModbus.delay=0.6`）时，10次收尾全部在有限时间内结束。
6. **完成标准：** 第1条、第4条中“运行期间以 cancelled 结束报 fatal”、第5条在修改前失败；第2、3条对照和反例在修改前后都成立；修改后全部通过；`test_modbus_acquisition.py`、`test_runtime.py`、`test_capstone_c.py` 原有用例全过。

---

## 4. R02：详情脱敏与入队边界（Q04、Q06）

### 4.1 规则（已确认，第2.3节决策6）

| 输入 | 输出 |
| --- | --- |
| 原文长度 > 4096 字符 | `detail = None`，`truncated = True`；不做任何部分保留 |
| 原文长度 ≤ 4096 字符 | 先完整脱敏；脱敏后长度 > 512 则截到 512 并 `truncated = True` |
| 脱敏中的 URL 规则 | 从 `scheme://` 之后到其后**最近的一个 `@`**，中间内容（可含空白）一律替换为 `***`；`@` 之后照常保留 |
| `_BEARER`、`_SECRET` | 保持现有规则不变 |

**说明：**

- “最近的一个 `@`”指 `scheme://` 之后出现的第一个 `@`。没有 `@` 时不遮挡，URL 原样保留。
- 不解析 URL 结构，不判断 `host:port` 与 `user:pass` 的区别，不新增通用 URL 解析器。
- 4096、512 这两个数沿用现行常量（[commlog.py](../htsg/commlog.py)第21–22行），不改成配置项，也不写进终版。

### 4.2 改动

1. **[commlog.py](../htsg/commlog.py)：** `_USERINFO`（第31行）改为上述“遮到最近 `@`、可跨空白”的语义；`clean_detail`（第49–63行）改为上表规则，删除“预截断后退回最后一个词”的逻辑（第58–61行）。
2. **[records.py](../htsg/records.py) `RequestResult`（第70–77行）：** 新增字段 `truncated: bool = False`，位置放在 `detail` 之后。
3. **[modbus.py](../htsg/modbus.py) `poll`（第157行）：** 构造 `RequestResult` 前调用 `clean_detail(str(error))`，把清理后的文本和截断标志存入。
4. **[owner.py](../htsg/owner.py) `_log_requests`（第757–768行）：** 二次清理保留，截断标志取 `q.truncated or 本次清理的截断标志`，不得把 True 覆盖为 False。
5. **[ingress.py](../htsg/ingress.py)：** `attempt_failed`（第61–70行）已在入口清理，规则随 `clean_detail` 自动更新，不需另改；`offer` 不处理批次内容。
6. **页面与 API：** 详情为空且 `detail_truncated` 为真时，通信页显示“详情已省略（过长）”，不得显示成空白或“无详情”。核对 `htsg/static/app.js` 的渲染；`docs/openapi.json` 若因字段变化需重新生成，用 `scripts/export_openapi.py`。

### 4.3 验收

1. 复核意见书 Q04 的合成串 A–G 全部不含合成秘密片段；A、C、D 的输出为 `None` 或已遮挡。
2. **边界：**
   - 4096 字符的原文正常处理，4097 字符整段省略；
   - 短文本 `opc.tcp://user:SYNTH SECRET@localhost/`（userinfo 带空格）不泄漏；
   - `connect opc.tcp://host:4840 failed; mail admin@example.com` 允许被多遮。这条用例写明“多遮是已确认的取舍”。
3. **必须原样保留：** `opc.tcp://127.0.0.1:4840/`、`127.0.0.1:502`、pymodbus 自带的常规超时消息、`OSError` 的 `repr` 短文本。
4. 假客户端抛出 5,000,000 字符的 `ModbusIOException`：在 `poll` 返回的批次里、`Ingress.queue` 未排空时、`owner.commlog.snapshot()` 及 `/api/health` 响应里，详情均为 `None` 且截断标志为真。
5. UA 侧：`_plain` 替换环境变量密码的行为不变（`test_comm_facts.py` 相关用例全过）。
6. `test_comm_log.py`、`test_comm_facts.py`、`test_comm_protocols.py`、`test_web_comm.py`、`test_health_communication.py` 原有用例全过。其中依赖旧“退回最后一个词”行为的断言如需修改，在执行记录中逐条列出并写明理由。

---

## 5. R03a：崩溃收尾与跨库收尾（Q02）

### 5.1 规则（已确认，第2.3节决策2、7）

1. **本段证据：** 某段（run_id, source_id, seg）的证据时刻 = max(该段**有效观测**的 `effective_wall`, 该段**水位** `covered_wall`)。
   - 有效观测：`quality = 'GOOD'`、`value IS NOT NULL`、`extreme = false`，含状态重申。
   - 不计：`state_event` 中的任何记录（POLL、COMM_ALARM、RESYNC、CLOCK_JUMP 等）、`result`、`fault_record`、R07 的失效记录（`quality` 以 `MISSING_` 开头）。
   - 该段既无有效观测也无水位时，证据时刻取该段的 `start_wall`。【细则约定】
2. **崩溃收尾（[archive.py](../htsg/archive.py) `reconcile_unfinished_runs`，第279–313行）：**
   - 运行的 `ended_wall` 与 `last_evidence_wall` 仍可取全 run 最大证据（现行 SQL 不变），这只是运行元数据；
   - 每个 `end_wall IS NULL AND start_wall IS NOT NULL` 的段，`end_wall` 改为**该段自己的证据时刻**，`end_reason='EXIT_TRUNCATED'` 不变。
3. **跨库收尾（[archive_router.py](../htsg/archive_router.py) `initialize`，第121–140行）：**
   - 先从所有已绑定且可读的库收集“已结束的 run”和“已有终点的段（run_id, source_id, seg）”；
   - 对每个库，只收尾在所有可读库里都没有结束登记的 run；对这些 run，只截断在所有可读库里都没有终点的段。
   - **不向其他库复制 RUN_END 组，也不改其他库的 run 行。**
   - 任一已绑定库不可读时：不收尾任何运行，在 `router.health` 记 `"reconcile": "PENDING_UNREADABLE:<库名>"`，留待下次启动重试；不在运行期间循环重试。【细则约定】
4. **查询（[history.py](../htsg/history.py)）：**
   - `_merge_segments`（第120–131行）：`end_wall` 取各库最大值，`end_reason` 必须取自 `end_wall` 最大的那一条记录；若某库有终点、另一库只有 `EXIT_TRUNCATED`，以有终点且非 `EXIT_TRUNCATED` 的那条为准。
   - `assemble` 对合并后 `end_reason='EXIT_TRUNCATED'` 的段：
     - 最后一条记录的保持终点 = 有该段水位时取 min(段终点, max(该行时刻, 水位))；无水位时取该行自身时刻，即只保留实存点、不推断保持；
     - 非最后一条记录照旧保持到下一条；
     - 缺口规则照旧。
   - `coverage_end`（第418–435行）：`EXIT_TRUNCATED` 段的确定终点取该段水位，无水位时不贡献。
   - 正常结束的段（其他 `end_reason`）行为全部不变。

### 5.2 验收（真实数据库，用 `archive_db_factory`）

1. **两源不同水位：** 源A观测1001、水位1002；源B观测与水位都到1010；不调用 shutdown 即模拟崩溃，然后收尾。
   - 源A段 `end_wall=1002`、源B段 `end_wall=1010`；
   - 查询源A最后一条保持到1002，源A的 `coverage_end` 为1002，不被源B抬高。
2. **无水位：** 源只有观测1001、没有水位：收尾后 `end_wall=1001`，查询最后一条保持终点等于其自身时刻，`coverage_end` 无值。
3. **水位后的实存点：** 水位1005，其后还有观测1007、1008：两行都可见；1007保持到1008，1008不向后保持。
4. **既有错误数据：** 直接写入一条按旧规则截断的段（`end_wall` 为全 run 最大证据、`end_reason='EXIT_TRUNCATED'`）：查询保持终点仍按该段水位限制，库里的数据不被修改。
5. **跨库：**
   - 运行在主库开始、中途路由切到应急库、再正常停止（RUN_END 只在应急库），然后重启：主库该运行不被记为 `ABNORMAL_EXIT`；合并后的段终点与结束原因来自同一条记录；正常结束的段不被截短。
   - 对照：同样的跨库运行但崩溃，重启后按本段证据截断。
   - 库不可读：RUN_END 所在库在重启时不可读，则不收尾、`health` 显示 PENDING，查询标 partial；该库恢复后再次启动，收尾正确。
6. 缺口记录、partial 标记不被删除或改写。`test_run_segments.py`、`test_capstone_d.py`、`test_history_query.py`、`test_history_merge.py` 原有用例全过；其中以全 run 最大证据作预期的断言（`test_run_segments.py` 第60–82行、`test_capstone_d.py` 第112–115行）改为本段证据，并在执行记录中写明理由。

---

## 6. R03b：持久段身份与恢复单元（Q07）

### 6.1 规则（已确认，第2.3节决策1，以及 v0.2 R03b）

1. **持久段事实。** owner 为每个源维护“已落库且未收尾的段集合”：
   - 只在 writer 回调 `committed()` 确认含 `SEG_START` 的组后加入；
   - 确认含该段 `SEG_END` 的组后移出；
   - `refused()` 和作废（void）不改变它。
   - 不再用 `open_segment` 或 `lost_seg` 推断要收尾哪个已落库段；`lost_seg` 字段改为由持久段事实取代，或删除。
2. **收尾意图。** 源失效、重同步、换代、停机时，若归档状态不是 OK，就把“段号、收尾原因、终点”登记为收尾意图，不再静默丢弃。
3. **延后补写的 SEG_END。** 由恢复单元或停机终结组携带：
   - 段号取持久段事实中仍开着的已落库段；
   - 终点取 min(收尾意图的终点, 该段所在缺口的起点)；
   - 每个已落库段最终恰好有一条 SEG_END。
4. **每源最多一个在途恢复单元。**
   - 在途单元未结清（`committed` 或 `refused`）前，`tick` 不建第二个恢复单元（`_try_archive_recovery`，第296–302行）；
   - 在途期间 pending 溢出时，进入 LOST 但不清除 `recovery_order`，`is_void`（第312–322行）不得作废该在途单元。
5. **在途单元结清后：**
   - 确认成功且其后发生过溢出：登记该单元的持久事实（新段已落库、旧段已收尾、缺口已记），但归档状态保持 LOST；下一个恢复单元的缺口从**溢出时刻**起算。
   - 被拒：持久事实不推进；下一个恢复单元的缺口从**最早未恢复的丢失起点**起算。
   - UNKNOWN：由提交队列按现行逻辑原样重试，原组号和原目标不变；结清前不建下一个单元。

### 6.2 验收（先内存、后真实数据库）

1. 复核意见书 Q07 的四条序列 ①、②、②′、③：每个已落库段都恰有一条 SEG_END，不出现只有终点的“幻影段”。
2. **竞态：**
   - 恢复单元在途时 pending 溢出，之后分别出现三种结局：首个单元先被确认再到下一次 tick；首个单元在下一次 tick 之后才被确认；首个单元被拒；
   - 三种情况都只有一个在途单元，首个单元从不被作废。
3. **一致性断言（所有序列）：**
   - 每段 `end_wall ≥ start_wall`；
   - 同源缺口互不重叠，也不盖住已确认恢复单元的基线；
   - 被拒后最早的丢失区间仍被记录；
   - `coverage_end` 不越过缺口起点；
   - 复核探针 X2 的“段终点早于段起点”不再出现。
4. 覆盖：提交后确认丢失（UNKNOWN 后再确认）、重复确认（ALREADY）、三库间的段片段合并。
5. `test_archive_gap.py`、`test_archive_recovery.py`、`test_capstone_f.py`、`test_capstone_e.py` 原有用例全过。其中 `test_recovery_attempts_the_queue_turns_away_do_not_move_the_segment_the_gap_closes` 的意图须保留。

---

## 7. R07：Modbus 失败轮的按点失效记录（Q08）

### 7.1 规则（v0.2 R07，已确认方向）

1. **何时写。** 某个 Modbus 点在本轮由“有效”变为“无效”时，写一条失效记录。“有效”指本轮处理前该点 `PointState.good` 为真。触发情形：
   - 整轮 `TIMEOUT`、`EXCEPTION`、`CONNECTION`（[owner.py](../htsg/owner.py) `_poll_failed`，第786–797行）；
   - 整轮 `OK` 但该点不在本轮应答中（`POLL_MISSING`，第744–748行）。
2. **不写的情形：**
   - 该点本轮前已无效（含从未有过观测、上一条已是 BAD/极值/失效记录）；
   - 持续失败的后续轮次；
   - OPC UA 源；
   - Modbus 解码为 BAD 的点（已有 BAD 观测）。
3. **记录形态（复用 `observation` 表，不加列）。**【细则约定】
   - `value = NULL`，`status_code = NULL`，`extreme = false`，`origin = 'RECEIVED'`；
   - `quality` 取值：整轮 TIMEOUT 为 `MISSING_TIMEOUT`，整轮 EXCEPTION 为 `MISSING_EXCEPTION`，整轮 CONNECTION 为 `MISSING_CONNECTION`，OK 轮中未应答为 `MISSING_UNANSWERED`；
   - `received_wall/mono` 取本批次的 `wall/mono`；`tick`、`poll_id` 取本轮值，`response_id = NULL`；
   - `batch_seq` 取本轮值，`item_seq` 接在本轮已收观测之后连续编号，ID 按 `observation_id` 规则生成，不与本轮观测冲突；
   - 与本轮 POLL 状态记录、派生量结果放在**同一个提交组**。
4. **owner 内部状态。**
   - 写失效记录后，该点 `PointState.obs` 指向这条记录；
   - `PointState.reason` 保持现行的 `POLL_<outcome>` 或 `POLL_MISSING`，实时视图和通信页显示的原因不变，不得变成 `QUALITY`；
   - 故障计时照现行逻辑取消。
5. **读取侧。**
   - `history.assemble` 不需要特判：`quality ≠ GOOD` 即 `valid = False`，自然终止此前的保持；
   - `input_basis`（[history.py](../htsg/history.py)第315–403行）：某输入在 THIS_ROUND 里只有 `MISSING_*` 记录时，必须计入 `missing`，`complete = False`；
   - CSV 导出的 `quality` 列如实写出 `MISSING_*`。
6. **连带：**
   - C2 状态重申会重申失效记录，这是正确的当前状态，不得改为重申更早的有效值；
   - 通信页输入表该行显示值为空、质量为 `MISSING_*`；
   - R03a 的“本段证据”不计失效记录。

### 7.2 验收（内存一遍，真实数据库一遍）

| 场景 | 断言 |
| --- | --- |
| ① 第10秒成功、第11秒整轮超时、第12秒成功 | 历史中 `b1` 在1011有一条 `MISSING_TIMEOUT`、`valid=False`；1010的值只保持到1011；1012重新有效；同一时刻 `s1` 为不可用，原始量与派生量一致 |
| ①′ 连续2轮整轮超时 | 只在第一轮写一条失效记录 |
| ② 协议异常持续60秒以上 | 只写一条 `MISSING_EXCEPTION`；历史在异常开始时终止保持，不再一直保持到水位 |
| ③ 多请求轮中一个请求持续超时（用 R05 的多请求服务端） | 未应答的点写一条 `MISSING_UNANSWERED`；应答的点照常有效 |
| ④ 以上各情形之后恢复 | 首条有效观测重新开始保持 |
| 一致性 | 实时视图、历史页、CSV、`input_basis`、`coverage_end` 对同一时刻的有效性一致；OPC UA 与解码为 BAD 的路径产生的记录与修改前逐条相同 |
| 容量 | 满量（8路 Modbus）下，一次失败转移最多增加8条记录；把这一增量写进执行记录 |

`test_owner_batches.py`、`test_modbus_acquisition.py`、`test_input_basis.py`、`test_history_query.py`、`test_capstone_b.py`、`test_capstone_f.py` 原有用例全过；因新增失效记录而必须调整的断言，逐条列出理由。

---

## 8. R04：以提交结果完成正常收尾（Q03 及补充 A）

### 8.1 规则（已确认，第2.3节决策3、4）

**停机流程（`Runtime.stop`，[runtime.py](../htsg/runtime.py)第154–167行）：**

1. 取消采集任务（R01 之后保证能结束），取消 owner 循环和维护任务，然后排空入口（与现行相同）。
2. 调用 `owner.shutdown`：
   - 为每个源登记收尾意图（R03b）；
   - 归档状态为 OK 的源，照常提交 SEG_END；
   - 归档状态为 RECOVERING 的源，SEG_END 进入 pending，等恢复单元结清；
   - 归档状态为 LOST 的源（不论 RESYNC、EXPIRED 还是 VALID），由 `shutdown` 显式构造一个**终结组**：GAP（起点为丢失起点，终点为停机时刻）加上每个已落库未收尾段的 SEG_END（终点按 R03b 规则），不带 C2 基线，也不带内存中已确认、尚未落库的故障记录（第11节 M）。终结组直接交提交队列，不受 LOST 抑制；它须像恢复单元一样登记为该源的 `recovery_order`，否则会被 `is_void`（[owner.py](../htsg/owner.py)第312–322行）当作“丢失之后的组”作废。终结组被**明确拒绝**（路由返回 REFUSED）时不再重试，该源记为“收尾失败”；终结组因提交队列已满而无法入队时，在期限内用**同一个组对象**重交，直到入队或期限到（第11节 K）。
   - **此时不提交 RUN_END。**
2a. **停机开始时恢复单元仍在途，之后被明确拒绝（放宽，第11节 J）。** 该源回到 LOST，按上一项由停机流程补发终结组。终结组被收下，该源结清，之后可登记 RUN_END（运行记 `NORMAL`）；终结组也被明确拒绝，该源收尾失败，不登记 RUN_END。排在该恢复单元之后等待的 pending 组随单元一起丢弃，它们覆盖的时间由终结组的 GAP（最初丢失起点至停机时刻）承担。
   **前提，缺一即不适用，不得登记 NORMAL：**
   - (a) 拒绝必须是确定未提交的拒绝（路由返回 REFUSED，发生在写库之前）；FAILED、UNKNOWN 不适用，仍由队列原样重试，期限到即放弃；
   - (b) 缺口自该源最早未恢复的丢失起点起，到停机时刻止，覆盖所有未落库的数据（含被丢弃的 pending，和被拒恢复单元所带的基线）；
   - (c) 终结组为每一个已落库、尚未收尾的段写 SEG_END，终点按 R03b 规则取较早者；
   - (d) 终结组确认提交之后，才提交 RUN_END；
   - (e) 以上全部在同一个 `STOP_WRITE_WAIT` 内完成，不另设期限。
3. 提交队列继续工作：恢复单元确认后释放 pending（现行 `committed()` 机制），终结组、pending 都按顺序写入。
4. **结清条件：** 每个源都满足以下之一——
   - 归档 OK、pending 为空、无在途恢复单元，且 SEG_END 已确认；
   - 终结组已确认（含恢复单元被拒之后按 2a 补发的终结组）；
   - 终结组被明确拒绝：该源记为“收尾失败”，视为“不能登记正常结束”；
   - 恢复单元被拒本身不使该源收尾失败，按 2a 处理。
5. 全部源已结清、且没有收尾失败的源时，构造 RUN_END 组并提交，等待其确认。
6. **期限：** 从第2步开始到 RUN_END 确认，总计不超过 `STOP_WRITE_WAIT`（8秒）。任一情况不提交 RUN_END，运行行保持 `ended_wall IS NULL`，下次启动由收尾流程记为 `ABNORMAL_EXIT`：
   - 期限到；
   - 有源收尾失败（其终结组被明确拒绝）；
   - RUN_END 被拒；
   - RUN_END 处于 UNKNOWN 未决。
7. **writer 退出：** `CommitQueue` 在停机期间，必须等到“RUN_END 已确认”或“期限到”才退出，不得因队列暂时为空而退出。实现方式：`Runtime.stop` 在 RUN_END 确认或期限到之前不调用 `queue.stop()`，writer 因此不会提前退出，[commitq.py](../htsg/commitq.py) 第59行不改（第11节 L）。
8. **RUN_END 重交：** 提交时队列满（`offer` 返回 False），在期限内重交**同一个 CommitGroup 对象**（组号、order、状态记录号不变）；不得再次调用 `_run_group` 重建。writer 返回 FAILED/UNKNOWN 时由队列按现行逻辑重试同一组。
9. **RUN_START：** 不加重试状态，不阻塞采集，只补用例。
10. **跨库：** RUN_END 写入当前路由目标库即为登记结束；其他库的处理由 R03a 负责。

**不做：** 不新增停机参数；不分阶段设期限；不无限等待；不新增通用投递系统；owner 仍不等待数据库（等待只发生在 `Runtime.stop` 的异步流程里）。

### 8.2 验收（真实数据库）

1. **复现 Q03：** 恢复单元确认被延迟时接纳数据并停止，库内实际提交顺序中 RUN_END 是本运行的最后一个组；在 RUN_END 提交前终止进程（`crash_worker.py` 方式），重启后该运行为 `ABNORMAL_EXIT`，不是 NORMAL。
2. 分别覆盖以下场景，每种都核对运行状态、段和缺口：
   - 停机时提交队列满；
   - 恢复单元在停机期间被拒（停机开始时它仍在途）：
     - 终结组被收下：运行 `NORMAL`；段在丢失起点收尾；库内有一条缺口记录，自丢失起点至停机时刻；恢复单元里的新段不入库；RUN_END 按提交时间排最后；
     - 终结组也被拒：不登记 RUN_END，运行行 `ended_wall IS NULL`，`Runtime.stop` 在期限前结束；下次启动记 `ABNORMAL_EXIT`，段按 `EXIT_TRUNCATED` 收尾；
   - RUN_END 处于 UNKNOWN；
   - 8秒期限到；
   - RUN_END 被重交后库内只有一条 RUN_END，且“已存组完整”（`commit_group.records` 与成员数相等）；
   - RUN_START 被拒时运行行仍由其后第一个组创建。
3. **停机时三种 LOST 源：** LOST＋RESYNC、LOST＋EXPIRED、LOST＋VALID，缺口与段收尾都落库，没有 C2 基线记录。
4. **跨库运行正常停止后重启：** 与 R03a 第5条同一场景，联合验收。
5. **停机耗时：** 正常情况下停机耗时不超过9秒（8秒期限加1秒裕量），记录实测值。
6. 只断言队列桩里的 order 不足以关闭本条。`test_runtime.py`、`test_run_segments.py`、`test_capstone_d.py`、`test_smoke.py` 原有用例全过。

---

## 9. R05：部分请求语义的回归用例（Q05）

### 9.1 规则

- 只写测试，不改 `htsg/` 下任何文件。
- **多请求服务端：** 在 `tests/protocol_support.py` 新增一个多设备 Modbus 服务端夹具：至少两个从站，可分别设置每个从站“正常应答 / 沉默 / 回协议异常”。复用 pymodbus 真实服务器或真实 TCP 帧，不 mock 客户端路径。【细则约定】

### 9.2 验收（用例固定现行行为）

1. 两个从站，一个持续沉默、一个正常，运行超过60秒（受控时钟版本可缩短真实时间）：
   - 连接不断开，`consecutive_timeouts` 始终为0，不触发重连；
   - 健康从站的点持续有效；
   - 稳定区间照常累计，60秒后失步次数清零；
   - 通信日志对沉默请求只有一条持续中的 `POLL_TIMEOUT` 聚合。
2. 两个从站都沉默连续3轮：触发一次重连；中间任一轮有一个从站应答，连续计数清零。
3. 一个从站回协议异常、另一个正常：不计超时，不告警。
4. 单请求（现行研究配置）行为不变。
5. 先用受控时钟跑一遍，再用真实服务端跑一遍。

---

## 10. R06：模拟保活适配与候选证据刷新（V04、V03）

### 10.1 规则（已确认，v0.2 第2.2节）

1. **一个共用适配。**
   - 在 `htsg/` 内提供一个函数，例如在 `htsg/simulation.py` 中新增 `install_keepalive_adapter(server)`，作用于已 `init()` 的 asyncua `Server` **实例**；【细则约定】
   - 实现方式只限两种：包装该实例 `iserver.subscription_service` 创建的每个 `InternalSubscription` 实例的方法，或使用实例级子类；
   - 不改 asyncua 的类，不改 site-packages，不影响同进程中未安装适配的服务器实例；
   - 后端客户端路径（`htsg/ua.py`）不得调用或依赖它。
2. **目标行为（OPC UA Part 4 订阅规定）：** 无通知时，保活间隔 = 协商的 `MaxKeepAliveCount` × 发布周期。即请求计数2、周期1秒时间隔2秒，计数1时间隔1秒。有数据通知时行为不变。
3. **覆盖范围：**
   - `FieldSimulator.start_opcua`（[simulation.py](../htsg/simulation.py)第156–179行），覆盖研究模拟器、`measure.Lab`、`smoke`；
   - `tests/protocol_support.py` 的 `FieldUa.start`，覆盖协议测试和 `test_runtime`。`Rig`（`tests/live_support.py`）复用 `FieldUa`，不另做修正。
4. **保留原始对照：** 适配函数有开关，或允许不调用；至少一个用例显式使用未适配的服务器，断言原始偏差仍存在（计数2时约4秒、计数1时约3秒）。
5. **不改的东西：**
   - 后端 `ua_data_validity`（3秒）、`ua_keepalive_count` 默认值（2）、`Timing.problems()` 的约束公式；
   - 不用逐秒重报、噪声等手段遮蔽静止输入。

### 10.2 步骤与验收

1. **复现原始偏差：** 未适配服务器，计数2与1，各观察至少16秒，记录请求值、修订值、实测间隔。
2. **适配后实测：** 计数2时相邻保活间隔为 2.0±0.2 秒，计数1时为 1.0±0.2 秒；有数据变化时通知不延迟。
3. **Owner 层相位扫描（受控时间）：** 保活间隔2秒、有效期3秒，tick 相位从0到1秒按0.1秒步进扫描：任何相位下都没有 EXPIRED。对照：间隔3秒时存在会触发 EXPIRED 的相位。
4. **撤销绕行：** 以上通过后，撤回 `scripts/measure.py` 第90行、`tests/live_support.py` 第42行、`tests/test_runtime.py` 第30行的 `ua_keepalive_count=1`，改用默认值；同步修正三处注释，以及[交付记录](HTSG_V1_交付记录_v0.1.md)第66行的量值说明——原写“晚一个周期、计数2实为3秒”，实测为“晚两个周期、计数2实为4秒、计数1实为3秒”，并写明适配后的行为。旧测量结论保留为历史，不改写其数字。
5. **满量静止联调（真实数据库）：** `measure.make_config(quiet=True)`（默认计数2，适配后）跑完整200点 OPC UA 加8路 Modbus：
   - 至少10分钟，覆盖多个保活周期、有效期和水位周期；
   - 无 EXPIRED、无重建、静止值不重复归档；
   - 之后执行 `disconnect_opcua` 场景，仍按3秒失效、6秒告警并重建的规则失效与恢复。
6. **稀疏通知、纯保活、真实断开分别验证，** 不得用持续噪声替代。
7. **候选证据刷新（V03）：**
   - 候选冻结后，按 `scripts/smoke.py` 的 `candidate()` 重算代码、页面、配置、schema 摘要；
   - 重跑受影响的 D46 测量、D47 抗故障、D48 烟测，证据存 `docs/evidence/` 下新的带日期目录；
   - 旧证据不删不改；
   - 报告中 `measure.outage` 的 `noticed_after_seconds` 只表示“至少一个输入无效”，不得写成告警时延；`smoke.py` 命令行产出报告不等于通过，以 `test_smoke.py` 的断言为准。

---

## 11. 细则约定（推导项，已确认）

以下约定由本细则为消除歧义而补入，不是 v0.2 原文。**用户已于2026-10-15确认 A–I 全部生效，J–M 于2026-10-19确认生效，N–T 于2026-10-20确认生效**，与正文其他条款同等约束，开发中不得自行改动。

| # | 约定 | 所在章节 | 推导依据 |
| --- | --- | --- | --- |
| A | 段既无有效观测也无水位时，收尾证据取段起点 | 5.1-1 | 决策7“不推断持续有效”；需要一个不越过实存事实的终点 |
| B | 有库不可读时不收尾，记 `PENDING_UNREADABLE`，下次启动重试，不在运行期循环重试 | 5.1-3 | v0.2 R03a“保留待核查状态”；不新增运行期任务 |
| C | 失效记录的 `quality` 取 `MISSING_TIMEOUT / MISSING_EXCEPTION / MISSING_CONNECTION / MISSING_UNANSWERED` | 7.1-3 | v0.2 R07“质量标明缺失类别”；owner 只知道这四类 |
| D | 失效记录不改变实时原因码（仍为 `POLL_*`） | 7.1-4 | 不改变现有页面与 health 语义 |
| E | 多设备 Modbus 测试夹具放在 `tests/protocol_support.py` | 9.1 | R05 只改测试 |
| F | 保活适配函数放在 `htsg/` 内（建议 `htsg/simulation.py`），作用于服务器实例 | 10.1-1 | 研究模拟器与测试夹具都要能导入；不改全局类 |
| G | 适配后保活间隔容差 ±0.2 秒，相位扫描步长 0.1 秒，满量静止联调至少10分钟，停机实测上限9秒 | 8.2-5、10.2 | 验收需要可判定的数值 |
| H | 页面在详情省略时显示“详情已省略（过长）” | 4.2-6 | 终版第15.1节“详情截断须标明” |
| I | 停机终结组登记为该源的 `recovery_order`，免于 `is_void` 作废 | 8.1-2 | 现行 `is_void` 会作废丢失之后的组；终结组必须能落库 |
| J | 停机开始时在途的恢复单元被明确拒绝后补发终结组，被收下即可登记 NORMAL，也被拒才不登记；须满足 2a 的五项前提 | 8.1-2、2a、4、6，8.2-2 | v0.2 R04 第5步已允许“其拒写已形成持久缺口、源收尾结清后”再登记；原 8.1-4 比方案更严；用户 2026-10-16 选择、2026-10-19 确认并要求写入 |
| K | 终结组因队列满无法入队时，在期限内重交同一组对象；仅明确 REFUSED 不再重试 | 8.1-2 | 用户 2026-10-16 裁定保留 |
| L | writer 不提前退出靠 `Runtime.stop` 先等 RUN_END／期限、之后才调 `queue.stop()` 保证，`commitq.py` 不改 | 8.1-7 | 用户 2026-10-16 裁定不加 |
| M | 终结组只含 GAP 与段收尾，不带内存中的故障缓冲，也不写 FAULT_LOSS 摘要，这些记录随进程结束丢失 | 8.1-2 | 用户 2026-10-16 裁定不带 |
| N | R02取舍(b)：未转义的`@`按最后一个`@`遮蔽，`u:p@ss@host`→`***@host`，host保持可读；去掉严格xfail，不得删用例来认可泄漏 | 4.1 | 用户2026-10-19裁定 |
| O | 停机三小项：终结段结束原因与恢复路径一致（LOST且来源VALID记`ARCHIVE_GAP`）；不因第一个收尾失败就退出停机循环（其余来源继续到结清或8秒期限，有失败仍不登记NORMAL）；以`shutdown_incomplete`／`run_end_status`／`failed_sources`／未结清队列项取代`unwritten_at_stop`，不得解释为“零数据丢失” | 8.1 | 用户2026-10-19裁定 |
| P | R06验收口径：量连续保活间隔、容差±0.2秒；未适配计数1／2对照各观察≥16秒；3秒保活间隔至少有一个会失效的相位；补稀疏变化、纯保活、变化及时性；超差先查原因，不放宽容差；生产有效期仍3秒 | 10.2 | 用户2026-10-19裁定 |
| Q | 审核必修2–5纳入本轮：UNKNOWN重试固定原库、首次Modbus失败不显示VALID、归档非OK时的墙钟跳变、跨库历史按通道全局限流 | 5、6、7 | 用户2026-10-19裁定 |
| R | 历史每通道行数上限＝窗口内最多N行＋起点处生效的保持值；上限之后的第一行只用于结束最后显示行的保持，不显示、不计数；页面与CSV同源 | 历史查询 | 用户2026-10-20裁定（评审发现的截断错误） |
| S | UNKNOWN钉住表满时不淘汰既有绑定，新的UNKNOWN组以`PIN_TABLE_FULL`明确拒绝并计数；被队列作废的组释放自己的钉子 | 路由 | 用户2026-10-20裁定10(a)及void组选(a) |
| T | 丢失期间时钟跳变标记超过16条时，记录被淘汰条数并在被存储的第一条标记上注明；缺口与段身份不依赖被淘汰的标记 | 6 | 用户2026-10-20裁定10(b) |

---

## 12. 记录、提交与门槛

### 12.1 每批完成后

在[执行记录](HTSG_V1_开发执行记录_v0.1.md)第1节末尾追加一条，格式沿用实施方案第11节：

`R0x｜代码版本/工作区摘要｜修改文件｜验收用例及命令｜通过/失败/跳过｜证据位置｜未覆盖项`

同时更新第3节交接的首段：当前做到哪一批、下一批是什么、有哪些已知卡点。批次未通过时写清停在哪一步，不得用后续批次的结果补偿。

### 12.2 门槛（来自 v0.2 第4节，不放宽）

| 门槛 | 条件 |
| --- | --- |
| G1 | R01、R02、R03a、R03b、R04、R05、R07 的上述用例全部通过；Q02、Q03、Q07、Q08（含跨库运行）取得真实数据库证据；取消与生命周期检查都在有限等待内结束 |
| G2 | 同一候选上连续（不分段）运行全部适用测试，记录实际收集数、通过、失败、跳过、中止、耗时；有挂住的，记录 faulthandler 堆栈。632、631、45 这些历史数字都不是本门槛 |
| G3 | R06 通过后，完成受影响 capstone、满量性能、崩溃/路由/容量测量和部署烟测，证据绑定同一候选 |

G4（一年容量、现场设备、独立卷、现场长稳）不在本细则范围内；一年容量“不通过”的结论保持不变。

### 12.3 明确不在本轮的事项

- SIM-01（旧预算门禁）已于2026-10-20处理（见执行记录）；SIM-03（过时使用说明）新写现行版，N2（完整场景规划）先出覆盖表，均在首次正式完整模拟之前，不属本细则批次。
- V05：终版与实施方案中“六页/通信页未实现”的状态陈述，用户已决定**不回写**。
- R02 的规则不写入终版。
- 任何改变默认发布周期、保活计数或有效期的提议，须作为独立架构决定另行评审。

### 12.4 开工检查清单（新会话第一件事）

1. 读本细则、整改方案 v0.2、执行记录第3节；对照 `git log`，确认基线为 `946cb5d` 或其后代；工作区只有本细则一个未跟踪文件。
2. 第11节细则约定已确认，不再重复确认；只向用户确认本轮代码的提交方式。
3. 启动并确认开发库；跑一遍 `test_spec_links.py` 与 `test_fault_thresholds.py` 作冒烟。
4. 从 R01 开始。
