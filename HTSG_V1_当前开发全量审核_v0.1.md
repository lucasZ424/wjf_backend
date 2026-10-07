# HTSG V1 当前开发全量审核

**版本：v0.1｜审核日期：2026-10-06**
**审核对象：** `htsg/`（Python 全部模块、静态前端 `app.js` / `index.html`）、`tests/`（全部用例源码）、`scripts/`（除 `validate_ui.py`、`validate_recall_ui.py`、`export_openapi.py` 外）、`config/`（核对派生规则、Profile、存储与模型引用）、`pyproject.toml`
**方法：** 逐文件通读源码与测试源码；对照既有审核 `HTSG_V1_代码审核_v0.1.md` 核对整改是否落地；对"数据库故障"与"测试有效性"做定向核查。
**分类依据：** 1）严重问题（逻辑谬误、框架错误、虚假通过或无意义测试、数据库故障路径）；2）鲁棒性不足（影响系统韧性的设计失误）；3）冗余设计（增加维护成本的重复或死代码）。

## 本轮边界（请先读）

- **测试套件本轮未取得结论。** 开发库两实例（`D:\HTSG-dev\primary`、`emergency`）当前未运行：主库于 2026-09-29 崩溃（日志见下文 C3），此后未重启。运行 pytest 时 DB 相关用例报连接失败；随后读取测试输出的操作被权限策略拒绝，未重试。**因此下文关于测试的所有判断都是源码静态判断，不是运行结果。**
- 未做任何性能或压力实测。标注"静态推断·待实测"的条目需要在实机验证后才能定性。
- 未修改任何业务代码、测试或既有设计文件。
- 未逐行审阅 `docs/` 下的截图与生成 JSON，也未审阅 `config/research-sim.json` 的全部 1050 行数据。

## 0. 结论

- **语义层大体已按既有审核整改落地。** 单一状态所有者、帧语义、聚合定义、种子计数（[schema.sql:58-97](htsg/schema.sql:58)）、连接复用与批量事务（[storage.py:40-63](htsg/storage.py:40)）、键集分页（[storage.py:806-877](htsg/storage.py:806)）、被阻断原因的保留（[runtime.py:315-321](htsg/runtime.py:315)）均可在源码中找到对应实现。读码未发现回退。
- **本轮新发现 8 项严重问题**，其中最影响"留证可信度"的三项：
  - **C4**：活动事件期间数据源断联不产生缺口，事件仍可标为 `OBSERVED` / `RECOVERED`；
  - **C1**：写入循环对永久性错误无限重试，单条坏记录可永久阻塞归档；
  - **C3**：主库服务故障没有任何切换路径。
  - C3 与 C4 涉及设计取舍，见第 5 节，需要你决定方向。
- **测试层存在恒真断言与名不副实的用例**（C7、C8），并有若干依赖墙钟的精确计数（R16）。
- **冗余设计**集中在三处：类型宽度表重复四份、仅测试引用的生产方法、写入即死的字段。

计数：严重 8 项（C1–C8）｜鲁棒性 18 项（R1–R18）｜冗余 10 项（D1–D10）。

---

## 1. 严重问题（critical）

### C1　写入循环把所有 psycopg 错误当作瞬时故障无限重试（毒丸阻塞归档）

- **位置：** [runtime.py:448-462](htsg/runtime.py:448)
- **现象：** `except psycopg.Error` 分支把失败批次原样放回队首、置 `accepting=False`、退避后重试；退避上限 5 秒，没有重试次数上限，也不区分永久错误（`DataError`、`IntegrityError` 等）与连接类错误。
- **触发路径（静态推断）：** 开发注入接口的 `quality` 是无约束字符串（[api.py:195-199](htsg/api.py:195)），经 [engine.py:131-155](htsg/engine.py:131) 写入样本 payload（[storage.py:168-171](htsg/storage.py:168)）。若字符串含 `\x00`，`Jsonb` 序列化为 `\u0000`，PostgreSQL 的 `jsonb` 拒绝该转义并报错。
- **影响：** 该批次永远提交不了，其后的事件、帧、缺口全部排在它后面，归档整体停止；`accepting` 保持 False，活动追忆被关闭（见 C2）。
- **证据状态：** 重试行为已读码确认。PostgreSQL 对 `\u0000` 的拒绝是已知行为，**本轮未实测**。
- **建议：** 永久性错误隔离该记录、写入明确的缺口并继续；只有连接类错误才重试；`quality` 改为枚举类型。

### C2　数据库写入失败被归因为"资源受限"

- **位置：** [runtime.py:448-451](htsg/runtime.py:448)、[runtime.py:315-321](htsg/runtime.py:315)、[engine.py:381-431](htsg/engine.py:381)
- **现象：** 任何 `psycopg.Error` 都把 `accepting` 置 False；`synchronize_capture_availability` 据此设置 `engine.capture_available=False`；`advance_events` 随即以 `RESOURCE_LIMITED` 关闭所有活动事件（[engine.py:420-424](htsg/engine.py:420)），覆盖度降为 PARTIAL（[engine.py:385-387](htsg/engine.py:385)），并通过 `blocked` 抑制后续重开。
- **影响：** 一次瞬时数据库中断（连接被终止、服务重启）会把活动追忆记为"资源受限"。事后无法区分"磁盘满"与"数据库抖动"，关闭原因与真实原因不符。
- **证据：** 已读码确认。现有测试 [test_fault_validation.py:78-102](tests/test_fault_validation.py:78) 只断言检测不中断，未断言事件的关闭原因。
- **建议：** 数据库错误使用独立的阻断原因与关闭原因（例如 `STORAGE_UNAVAILABLE`），并为此补测试。

### C3　主库服务故障没有切换或降级路径

- **位置：** [storage.py:647-698](htsg/storage.py:647)（`route` 只依据磁盘水位）；[storage.py:778-779](htsg/storage.py:778)（主库写失败且模式为 PRIMARY 时直接重抛，交给 C1 的循环）。
- **现象：** 主库进程崩溃或连接不可用时，路由仍为 PRIMARY，写入反复失败；同机的应急库从不被使用。
- **环境证据：** `D:\HTSG-dev\primary-startup.log` 与主库服务器日志记录 2026-09-29 17:50 "background worker ... terminated by exception 0x40010004 ... all server processes terminated; reinitializing"。此后主库未再运行（2026-10-06 本轮检查时进程列表中无 postgres）。这不是假设情形。
- **影响：** 数据库进程故障期间，历史与追忆全部不可归档，并叠加 C2 的误归因与队列溢出。
- **设计状态：** 数据流与移动盘文档只规定了**磁盘水位**触发的应急路径（[移动盘接续与清理暂停.md:40](docs/移动盘接续与清理暂停.md)），**未规定数据库服务故障时的行为**。见第 5 节 D-1。

### C4　活动事件期间的数据源断联或测点失效不产生缺口，事件仍可 OBSERVED + RECOVERED

- **位置：** [engine.py:451-458](htsg/engine.py:451)（`disconnected` 只改状态，不调用 `note_loss`，不改覆盖度）；[engine.py:486-489](htsg/engine.py:486)（`tick` 的陈旧判定同样不留记录）；[engine.py:407-411](htsg/engine.py:407)（不可用只记录起始时间，不影响覆盖度）。
- **现象：** 源断联或测点陈旧期间，活动事件保持 ACTIVE；恢复后正常进入 RECOVERED，覆盖度仍为 `OBSERVED`。只有断联超过 `unavailable_timeout`（600 秒，[config.py:98](htsg/config.py:98)）才以 `SOURCE_UNAVAILABLE_TIMEOUT` 关闭并降为 PARTIAL（[engine.py:424-430](htsg/engine.py:424)）。
- **影响：** 短于 600 秒的中断在事件留证中完全不可见：没有 gap，覆盖度不降，`has_gaps` 为 false。这与冻结原则"不据此假定缺失或协议失败为正常有效数值"（[HTSG_V1_已冻结主架构设计_v1.1.md:15](HTSG_V1_已冻结主架构设计_v1.1.md)）直接相悖。
- **证据：** 已读码确认。[test_research_flow.py:100-101](tests/test_research_flow.py:100) 只在无断联的事件上断言"无缺口"，未覆盖"事件期间断联"。
- **设计状态：** 数据流文档明确写着"断联的处理边界待明确"（[数据流详细设计_v1.0.1.md:15](数据流详细设计_v1.0.1.md)）。见第 5 节 D-2。

### C5　迟到样本被挂到不相关的活动事件上

- **位置：** [engine.py:258-259](htsg/engine.py:258)：`if event and point_id in profile.source_points: self.capture(event, sample)`，未检查样本的 `effective_time` 是否落在事件的 `[start, end)` 内。
- **现象：** `in_order=False` 的迟到样本（例如源时间基准下乱序到达的旧样本）只要所属点是源测点，就被关联到当前活动事件。事件样本查询在不带时间参数时返回全部关联样本（[storage.py:245-249](htsg/storage.py:245)、[storage.py:806-877](htsg/storage.py:806)），因此事件样本列表会包含时间窗外的样本。
- **影响：** 事件留证中混入时间窗外样本，样本计数与首末时间失真，且窗外样本没有任何标记。
- **触发条件较窄：** 接收时间基准下几乎不发生；源时间基准下乱序或迟到数据时发生。
- **证据：** 已读码确认。测试未覆盖。

### C6　历史查询的相关子查询导致逐行全表扫描（静态推断）

- **位置：** [storage.py:281-283](htsg/storage.py:281)：`EXISTS(SELECT 1 FROM trigger_fact t WHERE t.payload->>'point_id'=... AND ...)` 位于 SELECT 列表；[schema.sql:103](htsg/schema.sql:103) 的 `trigger_fact` 索引只覆盖 `event_id` 与 `time`，不覆盖 `run_id`、`window_start`、`point_id`、`state` 这些 JSON 表达式。
- **现象：** 每个输出行执行两次相关子查询，各自在 `trigger_fact` 上顺序扫描。PostgreSQL 通常不会把列表中的 EXISTS 提升为半连接，并且投影可能在 `ORDER BY` 之前对所有匹配行求值，而不是只对最后 `LIMIT` 的一页求值。
- **影响：** 查询代价约为"窗口内行数 × 触发表行数"，随数据增长很快超过 `statement_timeout = 3000`（[storage.py:45](htsg/storage.py:45)）。超时被 [storage.py:822-824](htsg/storage.py:822) 当作"实例不可用"，结果变为 partial，全部实例不可用时返回 503（[api.py:99-100](htsg/api.py:99)）。
- **证据状态：** 静态推断，**待 `EXPLAIN (ANALYZE)` 在开发库实测**。
- **建议：** 把两个标志改为聚合后的 `LEFT JOIN`，或为 `(payload->>'run_id', payload->>'window_start', payload->>'point_id', payload->>'state')` 建立表达式索引；标志只对当前页的键做查询。

### C7　测试中的恒真断言或无意义断言

- [tests/test_api.py:71](tests/test_api.py:71)：`assert len(runtime.events) <= settings.limits.event_queue`。`runtime.events` 是 `deque(maxlen=event_queue)`（[runtime.py:53](htsg/runtime.py:53)），长度不可能超过上限，断言恒真，对"慢数据库不阻塞所有者"的结论没有任何贡献。
- [tests/test_fault_validation.py:136-137](tests/test_fault_validation.py:136)：`assert fresh.run_id != ready["run_id"] and not fresh.active`。`Engine` 每次构造都生成新 uuid（[engine.py:80](htsg/engine.py:80)），新引擎也没有活动事件；两个断言都是构造保证的，与"进程被杀后重启将事件标为中断"无关。
- [tests/test_engine.py:20](tests/test_engine.py:20)：`assert e.latest_frame is None` 在任何 `tick` 之前断言，构造后恒为真，不能证明"突变检测不产生帧"。

### C8　测试名称与断言不一致，或断言对被测行为不敏感

- [tests/test_storage.py:196-212](tests/test_storage.py:196) `test_reconnecting_store_does_not_interrupt_live_capture`：写入的事件 payload 本身就是 `ACTIVE`（`Event(...)` 默认阶段），再写一个样本后断言仍为 `ACTIVE`。在"完全没有关闭或中断逻辑"的实现下这个断言也会通过；`router.ready.discard("primary")` 只触发惰性初始化，没有测到任何重连路径。
- [tests/test_storage.py:77-107](tests/test_storage.py:77) `test_recomputable_result_cleanup_never_removes_source`：标题称"从不删除源"，但断言只检查样本行与 `results_removed`，不检查帧级源值。第 102-103 行用原始 `UPDATE` 把 `recomputable` 直接置真，绕过了被测的 `certify_recomputable` 正向路径（第 101 行只做了反向断言）。
- **影响：** 测试通过并不证明该语义成立；修改 `certify_recomputable` 的正向逻辑不会让任何测试失败。

---

## 2. 鲁棒性不足（robustness deficit）

### R1　异常持续期间逐样本写入触发记录与事件修订

- **位置：** [engine.py:232-236](htsg/engine.py:232)：每个 `in_order` 的越限样本都调用 `trigger()`。[engine.py:263-282](htsg/engine.py:263) 中 `trigger()` 既 `emit("trigger")`，又调用 `event_record()`（revision+1 并 emit "event"）。
- **影响：** 每个越限样本产生 1 条 trigger 与 1 条事件修订，二者都是优先级记录，进入 2048 长度的事件队列。长时间高频越限会按采样率放大写入量；队列满时 [runtime.py:158-166](htsg/runtime.py:158) 把 `accepting` 置 False，把自己制造的写入压力变成捕获丢失。
- **状态：** 静态推断，待压测。

### R2　迟到的异常样本逐条生成 HISTORICAL 事件，无去重、无限速

- **位置：** [engine.py:237-257](htsg/engine.py:237)：每个不 `in_order` 的越限样本都会新建一个 CLOSED 的历史事件并写入 event 与 trigger 记录；即使样本已超过 `historical_age`，也仍然写 event 与 trigger，只是额外多一条 gap。
- **影响：** 设备回放缓存数据时，一次回放即可向优先队列灌入大量记录，引发与 R1 相同的后果。

### R3　`submit` 不校验测点，未知测点会让整个运行时 fatal

- **位置：** [runtime.py:88-108](htsg/runtime.py:88) 对未知测点仍然入队；所有者调用 [engine.py:131-133](htsg/engine.py:131) 时抛 `ValueError`；[runtime.py:281-285](htsg/runtime.py:281) 不捕获；最终由 `task_completed` 置 fatal。
- **现状：** API 层已校验（[api.py:206-207](htsg/api.py:206)），当前可达路径安全；但任何新增的注入入口都会直接打崩服务。

### R4　主库与应急库的独立性在启动时不校验

- **位置：** [storage.py:71-100](htsg/storage.py:71) 只校验外部库与对端库的独立性；[cli.py:90-93](htsg/cli.py:90) 创建主库与应急库 Store 时不检查 DSN 是否指向同一数据库或同一卷。生产模板 [config/production.template.json](config/production.template.json) 也不强制它们位于不同卷（只有外部库有此要求，见 [移动盘接续与清理暂停.md:36](docs/移动盘接续与清理暂停.md)）。
- **影响：** 误配置时，"应急库"可能与主库同库同盘，应急路由失去意义，且没有任何启动告警。

### R5　数据库不可用时以本地缓存鉴权，已在库中停用的账户仍可登录

- **位置：** [api.py:19-25](htsg/api.py:19) 在数据库读取失败且无未决标记时进入 `LOCAL_VERIFICATION_CACHE_ONLY`；[auth.py:26-33](htsg/auth.py:26) 此时按本地缓存验证。
- **说明：** 这一取舍是有意设计的（[README.md:59](README.md)、[模拟现场使用说明.md:93](docs/模拟现场使用说明.md)）。但在数据库故障期间，缓存中已被撤销的账户会重新有效，而健康接口只显示模式名称，不显示风险。见第 5 节 D-4。

### R6　`pending_events` 没有分页，超过 256 个未收尾事件时启动后不会全部收尾

- **位置：** [storage.py:137-141](htsg/storage.py:137) `LIMIT 256`，无排序键；`reconcile_open_events` 只处理这 256 个（[storage.py:620](htsg/storage.py:620)）。超出部分在本次启动后仍为非 CLOSED 或 PENDING，且不再被报告。

### R7　维护清理整轮回滚，没有进展保证（静态推断）

- **位置：** [storage.py:447-568](htsg/storage.py:447) 的清理是单个事务；任一语句超过剩余预算（`maintenance_budget` 默认 0.2 秒，[config.py:87](htsg/config.py:87)）即 `QueryCanceled`，[storage.py:57-63](htsg/storage.py:57) 回滚并关闭连接，整轮作废。
- **影响：** 若某条候选查询本身耗时超过预算，清理将永远不前进，磁盘水位持续上升，健康接口只显示 `MAINTENANCE_TIMEOUT`。**本轮未实测。**

### R8　停机超时后未落盘记录丢失，且没有持久痕迹

- **位置：** [runtime.py:474-490](htsg/runtime.py:474)：写线程收尾最多等待 8 秒，超时即 `cancel()`。队列中尚未提交的记录（包括 `shutdown` 刚生成的 PROCESS_INTERRUPTED 事件与缺口）随进程消失，且不会留下任何缺口记录。

### R9　计算超时的线程不可取消，calculator 对其无界等待

- **位置：** [runtime.py:348-364](htsg/runtime.py:348)：超时后 `await future` 没有上限。计算挂死时 calc 队列满，之后帧退化为 `CALCULATION_QUEUE_FULL`，线程无法回收。

### R10　未校验的 `quality` 字段

- **位置：** [api.py:195-199](htsg/api.py:195) 是任意字符串；[engine.py:195-198](htsg/engine.py:195) 对非 "GOOD" 的值一律视为不可用，不报错。与 C1 直接相关。

### R11　生产配置允许 OPC UA 无安全策略

- **位置：** [config.py:29](htsg/config.py:29) 默认 `security=""`；[config.py:175-176](htsg/config.py:175) 只校验端点与节点映射；[adapters.py:86-87](htsg/adapters.py:86) 无安全字符串时直接匿名连接。生产模式下可以合法地以明文、匿名方式读取厂家 DCS。见第 5 节 D-5。

### R12　模拟控制器的边界错误处理

- **位置：** [simulation.py:323-325](htsg/simulation.py:323)：`hmac.compare_digest` 对非 ASCII 的 `Authorization` 头会抛 `TypeError`，返回 500 而不是 401。
- **位置：** [simulation.py:34-44](htsg/simulation.py:34) `encode_registers` 对超出目标类型范围的值抛 `struct.error`，而该调用发生在 pymodbus 的动作回调中。范围限于开发模拟器，优先级低。

### R13　鉴权细节

- **位置：** [auth.py:26-28](htsg/auth.py:26)：用户名不存在时直接返回，存在时才执行 scrypt，形成可观察的时序差异（用户名枚举）。
- **位置：** [auth.py:28、50](htsg/auth.py:28)：scrypt 参数 `n=2**14`，低于常见建议值。令牌为 256 位随机值，暴力破解不可行，因此风险低。

### R14　会话凭据存放在 `sessionStorage`

- **位置：** [app.js:7](htsg/static/app.js:7)、[app.js:153](htsg/static/app.js:153)：任何能在页面执行脚本的注入都能读取令牌。现有渲染普遍经过 `esc()`，本轮未发现注入点，但这是唯一防线。

### R15　测试套件依赖工作目录、本地凭据与常驻开发库

- **位置：** [conftest.py:58](tests/conftest.py:58) 用相对路径读取 `.local/database.json`；所有 DB 用例假定 `D:\HTSG-dev` 存在且两实例运行（默认路径见 [config.py:105-106](htsg/config.py:105)）。无法在干净环境或 CI 中运行，也无法区分"代码失败"与"环境失败"——本轮正是如此，全部 DB 用例报连接错误。

### R16　依赖墙钟时间的测试

- [tests/test_capacity_recall.py:54-83](tests/test_capacity_recall.py:54)：引擎窗口由构造时刻取整，封口约在 +1.2 秒。若测试运行中跨过封口，会产生额外帧，使 `archive_dropped == 2` 与 `len(calls) == 1` 失败。间歇性失败风险，本轮未复现。
- [tests/test_account_persistence.py:14-29](tests/test_account_persistence.py:14)：服务端只在收到消息时复查账户状态（[api.py:230-241](htsg/api.py:230)），该用例依赖背景帧的广播节奏才能进入关闭分支。
- [tests/test_research_flow.py:77](tests/test_research_flow.py:77) 用固定 `sleep(2.2)` 等待窗口。

### R17　`api.py` 对 `psycopg` 的时序依赖

- **位置：** [api.py:21](htsg/api.py:21) 在闭包中使用 `psycopg`，而 `import psycopg` 写在 [api.py:39](htsg/api.py:39)，之后才被求值。当前能工作，但依赖闭包的求值时机，重构时容易失效。

### R18　写入热路径上的同步磁盘统计

- **位置：** [storage.py:718-751](htsg/storage.py:718)：每个批次执行两次 `shutil.disk_usage`；开发模式下每秒对每个库执行一次 `pg_total_relation_size` 全库统计，并在写线程中同步进行。优先级低。

---

## 3. 冗余设计（redundant design）

### D1　数据类型表在四处重复

- 字节数与格式表：[adapters.py:17-19](htsg/adapters.py:17)、[adapters.py:56-63](htsg/adapters.py:56)、[simulation.py:37-39](htsg/simulation.py:37)、[simulation.py:126-130](htsg/simulation.py:126)、[research_config.py:172-174](htsg/research_config.py:172)。新增一种数据类型需要同步修改五处。

### D2　仅测试引用的生产方法

- [storage.py:117-135](htsg/storage.py:117) `interrupt_events` 在生产代码中无调用方，只有 [tests/test_storage.py:113](tests/test_storage.py:113) 使用。它与 `Router.reconcile_open_events` 功能重叠。

### D3　写入即死的字段

- `Event.previous_id`：[engine.py:62](htsg/engine.py:62)、[engine.py:293](htsg/engine.py:293)。写入数据库与 API，无读取方。
- `PointState.usable`：[engine.py:201](htsg/engine.py:201)、[engine.py:483](htsg/engine.py:483)，无读取方。
- `Router.baseline`：[storage.py:581](htsg/storage.py:581)、[storage.py:602](htsg/storage.py:602)，无读取方。

### D4　拒写与丢失记账在三处重复实现

- 路由层：[storage.py:757-764](htsg/storage.py:757)、[storage.py:785-790](htsg/storage.py:785)。
- 写线程：[runtime.py:412-416](htsg/runtime.py:412)、[runtime.py:433-444](htsg/runtime.py:433)。
- emit：[runtime.py:153-167](htsg/runtime.py:153)。
- 每处都在维护 `archive_dropped`、`loss_summary`、`record_event_loss`；修改一处很容易漏掉另一处。

### D5　类内伪方法的签名

- [storage.py:71](htsg/storage.py:71) `validate_external_binding(external, config, primary, emergency)` 写在类内但没有 `self`。调用方通过 `external.validate_external_binding(...)` 能正常工作，测试也以 `DatabaseStore.validate_external_binding(external, ...)` 直接调用（[test_storage_binding.py:12-13](tests/test_storage_binding.py:12)）。这种写法会误导后续维护者。

### D6　`config_id` 每次访问都重算整份配置的 SHA-256

- [config.py:233-235](htsg/config.py:233) 是 `@property`；每帧经 [engine.py:606](htsg/engine.py:606) 访问一次。既有审核已指出，仍未修复。

### D7　三套临时健康字典

- `Engine.health`（[engine.py:97](htsg/engine.py:97)）、`Runtime.health`（[runtime.py:64](htsg/runtime.py:64)）、`Router.health`（[storage.py:579](htsg/storage.py:579)）。键名为分散的字符串，`status()` 需要分别拼接（[runtime.py:195-245](htsg/runtime.py:195)）。

### D8　D 盘检查有三份实现

- [config.py:224-230](htsg/config.py:224)、[storage.py:87](htsg/storage.py:87)（外部库卷校验）、[research_lab.py:46](scripts/research_lab.py:46) 与 [research_lab.py:74](scripts/research_lab.py:74)、[bootstrap_db.py:30-31](scripts/bootstrap_db.py:30)。规则相同，实现各异，已经出现口径差异的风险。

### D9　验证脚本与测试重叠；生成物入库

- 与测试功能重叠的脚本：[smoke.py](scripts/smoke.py)、[validate_research_cycle.py](scripts/validate_research_cycle.py)（另有 `validate_ui.py`、`validate_recall_ui.py` 未读）。
- Git 追踪的生成物：`docs/*.json`、`docs/*-sha256.txt`、`docs/screenshots/**`。它们与 `.local` 中的原始记录重复，每次重跑都需要人工同步。

### D10　命名与文档冗余

- `recomputable` 字段名暗示"可重算"，但代码与设计文档都明确没有重算路径（[docs/验证记录.md:75](docs/验证记录.md)）。建议改为表达"证据充分"的名字，避免误读。
- [HTSG_V1_代码审核_v0.1.md](HTSG_V1_代码审核_v0.1.md) 共 588 行，第 0–11 节保留了全部历史复核原文；结论与整改状态分散在多节中，阅读与维护成本高。
- 根目录有 9 份 HTSG_V1 文档，`docs/` 另有 6 份说明，多数互相引用版本号；`pyproject.toml` 为 0.2.2，而 [代码审核 §12](HTSG_V1_代码审核_v0.1.md) 记录的是 0.1.1，版本口径不统一。

---

## 4. 既有审核整改的落地情况（读码，未运行）

| 既有编号 | 代码现状 | 判断 |
| --- | --- | --- |
| S1 Pre 同步突发 | 独立 Pre 作业与预算（[engine.py:324-339](htsg/engine.py:324)、[runtime.py:123-144](htsg/runtime.py:123)） | 已落实 |
| S2 逐条连接与提交 | 线程本地连接复用、单事务批量（[storage.py:40-63](htsg/storage.py:40)、[storage.py:155-159](htsg/storage.py:155)） | 已落实 |
| S3 误解除抑制 | 只解除 `RESOURCE_LIMITED`（[runtime.py:315-321](htsg/runtime.py:315)） | 已落实，但见 C2 |
| S4 种子计数 | 同事务序号与计数器（[schema.sql:58-97](htsg/schema.sql:58)） | 已落实 |
| N4 切回探测 | 写探测含提交（[storage.py:102-107](htsg/storage.py:102)、[storage.py:684-692](htsg/storage.py:684)） | 已落实 |
| N5 清理预算 | 每轮预算与超时回滚（[storage.py:458-467](htsg/storage.py:458)） | 已落实，但见 R7 |
| N6 键集分页 | 单库排序、双库合并、游标绑定（[pagination.py](htsg/pagination.py)、[storage.py:806-877](htsg/storage.py:806)） | 已落实 |

既有审核中"80 项测试通过"的结论**本轮未复核**，因为测试未能运行。

---

## 5. 需要你决定的事项

以下项目的正确做法取决于设计取舍，我没有擅自选择。

- **D-1（对应 C3）主库服务故障时的行为。** (a) 自动切到同机应急库写入，并在主库恢复后回切（需要补充设计，并增加切换与回切测试）；(b) 保持现状，仅停止接收并明确告警（需要把"不归档"的状态显式化）。
- **D-2（对应 C4）活动事件期间的数据源断联或测点失效。** (a) 断联开始与恢复时各写入一条缺口，并把覆盖度置为 PARTIAL（与冻结原则一致，改动较大）；(b) 保持现状，只在健康接口中显式标注。
- **D-3（对应 C2）数据库瞬时错误是否进入"资源受限"语义。** 我建议拆分为独立的阻断原因与关闭原因。这一改动范围较小，你同意后可以直接实施。
- **D-4（对应 R5）数据库不可用时以本地缓存鉴权是否接受。** 若不接受，需要改为数据库不可用即拒绝登录，并在已有连接上同步关闭。
- **D-5（对应 R11）生产模式是否强制 OPC UA 安全策略。** 若强制，需在配置校验中要求 `security` 非空，并补充相应测试。

## 6. 未覆盖与未验证

- **测试套件未运行**；上文所有测试相关判断均为静态判断。
- 以下内容**未实测**：C1 中 `\u0000` 对 `jsonb` 的拒绝行为；C6 的查询计划与耗时；R7 清理进展；R1、R2 的写入放大倍数；R16 的间歇性失败。
- 未审阅：`scripts/validate_ui.py`、`scripts/validate_recall_ui.py`、`scripts/export_openapi.py`、`htsg/static/style.css`；`README.md` 与 `docs/*.md` 仅通过关键词检索核对了与代码相关的断言。
- 前端仅审阅了 `app.js` 与 `index.html` 的数据流与转义方式，未做浏览器测试。

---

## 7. 复核修订（2026-10-06，第二轮）

本节对第 1–3 节的条目逐一复核。第 1–3 节保留原文作为历史记录；**若两者不一致，以本节为准**。复核结论分三类：**接受**（原结论成立）、**修正**（方向成立，表述或范围需调整）、**驳回**（原结论不成立，已撤销）。

复核方报告的 `\u0000` 实测结果，本轮未独立复现，此处以复核方记录为准。

### 7.1 严重问题

| 编号 | 结论 | 修订后的表述与依据 |
| --- | --- | --- |
| C1 | **修正，范围收窄** | 写线程按轮转选择队列（[runtime.py:372-398](htsg/runtime.py:372)），毒丸只阻塞其所在的队列（事件队列或普通队列）及队列中其后的记录；其他队列在轮到时仍会被处理，不是"所有队列都被阻塞"。失败时 `accepting` 被置 False，其他队列成功后又会恢复，因此表现为间歇性异常。严重度维持。 |
| C2 | **修正** | 归档记录中的关闭原因是 `RESOURCE_LIMITED`，与真实原因不符，这一点成立。运行时健康接口仍保留最近一次错误类型（[runtime.py:450](htsg/runtime.py:450)），只是不保留历史。原文"事后完全无法区分"过重，应改为"归档记录中归因错误"。 |
| C3 | **接受行为，降级为设计缺口** | 主库故障无通用切换路径成立，但设计文档未规定该场景，属于需要决策的问题（见 D-1）。原文"主库此后未运行"只描述 2026-10-06 检查时的状态，不代表当前环境。移出严重问题，转入鲁棒性决策事项。 |
| C4 | **修正，范围收窄为事件级** | 断联期间帧值为 `UNAVAILABLE`（[engine.py:547-601](htsg/engine.py:547)），持久化后的帧与历史查询中可见，因此原文"完全不可见"不成立。真正缺失的是**事件级**证据：事件仍为 `OBSERVED` / `RECOVERED`，`has_gaps` 为 false，覆盖度不因中断而降低。 |
| C5 | **接受** | 复核方复现：事件起点 991，关联到时间为 980 的样本。但默认时间基准为接收时间（[config.py:131](htsg/config.py:131)），该问题主要影响源时间基准下的乱序或迟到数据。 |
| C6 | **驳回为严重问题，降级为待实测风险** | 原文用"必然""迅速超时"等表述超出了静态判断的证据。查询结构存在相关子查询与索引缺口，但性能结论必须以执行计划和真实数据规模证明。移出严重问题，列入待实测事项。 |
| C7 | **修正** | [test_api.py:71](tests/test_api.py:71) 的队列长度断言恒真，接受。[test_fault_validation.py:136-137](tests/test_fault_validation.py:136) 只是弱断言，不能否定整个用例（其主体断言已检查事件标记、未提交样本与已提交样本）。**驳回** [test_engine.py:20](tests/test_engine.py:20) 为恒真：它与第 19 行的触发断言配合，证明通知先于封窗产生；若检测被推迟到 `tick()`，第 19 行会失败。 |
| C8 | **修正** | [test_storage.py:196-212](tests/test_storage.py:196) 是惰性初始化的回归守卫：若惰性初始化触发了 `reconcile` 并中断活动事件，断言会失败。因此"名不副实"的说法撤销，保留为弱覆盖项；它确实没有覆盖真实连接断开。[test_storage.py:77-107](tests/test_storage.py:77) 的样本保留断言有效；缺失的是正向认证路径（`certify_recomputable` 返回真的情形）的覆盖。 |

修订后的严重问题为 **C1、C2、C4、C5、C7、C8**（范围已按上表收窄）。

### 7.2 鲁棒性

| 编号 | 结论 | 修订后的表述与依据 |
| --- | --- | --- |
| R1 | 接受，待压测 | 逐样本写入放大的机制成立，但放大倍数未测量。复核意见提到"新故障点机制"会替代这条路径，本仓库当前状态中未找到该机制，需要确认指代的改动。 |
| R2 | **修正** | 迟到异常样本逐条生成历史事件成立。去重必须以源身份（如 `source_seq`）为依据，不能按数值去重；在接收时间基准下该问题优先级降低。 |
| R3 | 接受，降级为潜在防御不足 | API 层已校验（[api.py:206-207](htsg/api.py:206)），当前可达路径安全。 |
| R4 | 接受 | 开发库同盘属于授权例外，生产应校验。 |
| R5 | **修正，降级为低** | 正常撤销会同步写入本地缓存（[auth.py:71-78](htsg/auth.py:71)）；缓存未决时拒绝验证（[auth.py:21-22](htsg/auth.py:21)）。已撤销账户重新生效，需要数据库被库外修改，或旧缓存被恢复。D-4 仍需决策，但原风险描述应收窄。 |
| R6 | **修正** | `LIMIT 256` 是**每个存储实例各自**的上限（[storage.py:137-141](htsg/storage.py:137)，逐库收集见 [storage.py:618-621](htsg/storage.py:618)），不是跨库合计 256。无分页的问题成立。 |
| R7 | 接受，降级为待实测 | 当前 `cleanup_enabled` 默认为 false（[config.py:110](htsg/config.py:110)），不是当前运行故障。 |
| R8 | **修正** | 已落盘的未收尾事件由重启协调标记为 `INTERRUPTED`（[storage.py:615-645](htsg/storage.py:615)）。确实丢失的是未落盘的记录，包括停机时生成的缺口，它们不留下持久痕迹。 |
| R9 | **修正，降级** | 计算线程不可取消，且 calculator 在其完成前不处理后续帧，成立。但队列仍有界，影响限于派生量恢复，不是无界积压。 |
| R10 | 接受 | 与 C1 相关。 |
| R11 | **修正** | 原文"匿名连接"不完整。未配置用户名时匿名明文；配置用户名与密码时，凭据经无安全策略的明文通道传输（[adapters.py:83-85](htsg/adapters.py:83)）。两种情况都是明文，表述改为"明文"。D-5 仍需决策。 |
| R12 | 接受 | 复核方已复现非 ASCII 头导致 500。 |
| R13 | 接受，定性为低风险 | 令牌为 256 位随机值，不以 scrypt 参数定性。 |
| R14 | 接受，定性为条件性风险 | 仅在存在脚本注入时才成为问题。 |
| R15 | **修正** | 需要准备环境（`D:` 上的开发库与本地凭据），不是"无法在 CI 运行"。 |
| R16 | 接受，未复现 | 间歇性失败风险存在，但不能断言相关断言必然失效。 |
| R17 | **驳回，撤销** | 闭包在调用时求值，当前顺序正确，不构成缺陷。 |
| R18 | 接受，待测量 | 是否构成性能问题需测量。 |

### 7.3 冗余设计

| 编号 | 结论 | 修订后的表述与依据 |
| --- | --- | --- |
| D1 | **修正** | 类型映射重复成立，共 **五处**（标题原写"四处"有误）。 |
| D2 | 接受 | 仅测试引用。 |
| D3 | **部分驳回** | `Event.previous_id` 经 `asdict` 写入数据库并随事件输出（[engine.py:293](htsg/engine.py:293)），撤销"死字段"的说法。`PointState.usable`（[engine.py:201](htsg/engine.py:201)）与 `Router.baseline`（[storage.py:581](htsg/storage.py:581)）仍成立，无读取方。 |
| D4 | **驳回跨层"三处重复"** | 路由层返回拒写清单、运行时统计丢弃、入口统计队列溢出，职责不同。仅写线程内 `PolicyRefusal` 分支与 `refused` 循环的记账逻辑相似，影响小。 |
| D5 | **驳回"伪方法"** | 该方法就是实例方法，首参数承担 `self` 的角色，只是命名约定不同。影响低。 |
| D6 | 接受，需测量 | 配置哈希重算的优化价值待测。 |
| D7 | **驳回** | 三套健康字典分别描述引擎、运行时与存储三层，不是冗余。 |
| D8 | 修正，降级为低 | 路径规则可以统一，但检查对象不同（配置值与实际数据目录），不是完全重复。 |
| D9 | **驳回** | 单元测试、真实协议验证与真实时钟验证覆盖不同层次；生成物是交付证据。 |
| D10 | **部分驳回** | 版本号口径差异（0.2.2 与 0.1.1）不构成冲突，撤销。`recomputable` 字段名易被误读的建议保留为低优先级；文档已明确不承诺重算接口。 |

### 7.4 修订后的汇总

- **严重问题**：C1、C2、C4、C5、C7、C8（范围已收窄）。
- **移出严重问题**：C3（转为设计决策 D-1）、C6（转为待实测）。
- **鲁棒性**：R1–R16、R18 保留（部分降级，见上表）；R17 撤销。
- **冗余设计**：D1、D2、D3（仅 `usable`、`baseline`）、D6、D8（低）、D10（低）保留；D4、D5、D7、D9 撤销；D3 中的 `previous_id` 与 D10 的版本口径撤销。

### 7.5 待你决定与待补充

- D-1 至 D-5 仍需决策，见第 5 节。
- C4 的修订仍需要决定：事件级缺口是否写入，见 D-2。
- C6 与 R1、R7、R18 需要在开发库上实测，才能定性。
- R1 的"新故障点机制"指代不明，需要确认是哪一项改动。
