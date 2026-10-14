# HTSG V1 全量测试前问题清单复核意见书

版本：v0.1｜日期：2026-10-14｜复核对象：[全量测试前问题清单 v0.1](HTSG_V1_全量测试前问题清单_v0.1.md)（未提交文件）｜代码基线：`de5f644`，复核开始时工作区除两份未提交审核文档外无差异｜依据：[终版设计](HTSG_V1_终版设计_v1.0.md)第3–13、15.1节，`config/*.json` 现行参数，已安装的 pymodbus 3.15.0 / asyncua 2.0.1 源码。

本文只复核原清单各条是否属实并进一步定位，**不提出处理方案**，不修改代码和任何已提交文档。复核中新发现、不在原清单里的事项单列在第6节，用户确认之前不当作已纳入的条目（出处：该节每条写明由哪一步核对引发）。模拟方案部分见[模拟方案复核意见书](HTSG_V1_模拟信号适配审核复核意见书_v0.1.md)。

## 1. 结论

原清单的七项源码问题（Q01–Q07）在当前代码里全部找得到对应路径，**没有一项被推翻**；其中 Q01、Q03、Q05、Q07 用真实组件或真实 Owner 复现，Q02 复现到查询层，Q04、Q06 复现了路径但触发条件比原文窄。需要修正表述或补充限定的有：Q04（泄漏条件）、Q05（合同归属与可达性）、Q06（量级）、Q07（场景描述与新路径）、V04（保活量值）。V01 已解决，V02–V05 事实成立。

| 条目 | 原优先级 | 复核结论 | 复现方式 | 需要修正或补充的一点 |
| --- | --- | --- | --- | --- |
| Q01 | P1 | 成立·已复现 | 真实 pymodbus＋项目采集器 | 取消被吞的概率约等于“读请求在途的时间占比”：对端即答 0/12，0.6秒应答 8/12，沉默 11/12；二次取消同样可被吞 |
| Q02 | P1 | 成立·读码核对＋纯逻辑复现 | `history.assemble` 合成输入；SQL 未在真实库执行 | 违背的是“以持久化水位为准”的口径；默认部署两个源，典型偏差约一个水位周期；现有测试只覆盖单源 |
| Q03 | P1 | 成立·已复现 | 真实 Owner＋真实提交队列＋桩路由 | 触发需要某源处于“恢复单元待确认”；写序里 RUN_END 先于同源数据和 SEG_END |
| Q04 | P1 | 限定后成立·已复现 | 合成串7例 | 仅当带凭据 URL 的 `@` 落在4096之后且截断点后无空白分隔词时泄漏；URL 为末词或 key=value 形式不泄漏 |
| Q05 | P2 | 成立·已复现（Owner层） | 合成61秒部分超时轮 | 合同未明文“部分超时轮”归类；默认配置只有1个读请求，不可达；同一分类还让部分超时轮复位“连续3轮超时重连”计数 |
| Q06 | P2 | 限定后成立·路径已复现 | 合成5 MB 异常文本 | 库自己构造的异常文本是固定短模板，真实触发需要异常文本异常膨胀 |
| Q07 | P2 | 成立·三条边界均已复现 | Owner层 | 执行记录第②③条描述与实际触发条件不完全一致；另有更常见的 RESYNC 触发路径；只影响段元数据，查询仍受缺口/水位约束（已复现） |
| V01 | 已解决 | 属实 | `git` 核对 | — |
| V02 | P1 | 事实成立 | 端口检查 | 复核时 55432/55433 无监听 |
| V03 | P1 | 成立·摘要精确复现 | 复算摘要 | — |
| V04 | P2 | 成立，需修正量值 | asyncua 保活间隔探针 | 实测保活比协商晚**两个**周期，不是注释所说一个 |
| V05 | P2 | 成立；用户已知待决 | 逐处核对 | 执行记录另有一处陈旧陈述 |

## 2. 复核范围、方法与边界

**读过的：** `htsg/` 下 owner、modbus、ua、ingress、runtime、commitq、archive、history、commlog、records、config、research_config、simulation、recovery、timing、cli、web；`scripts/` 下 measure、smoke、research_lab；终版设计全文；`config/research-sim.json`、`development.json`；执行记录、交付记录、容量报告中被原清单引用的行；`tests/` 的夹具与被引用的用例；pymodbus `transaction/transaction.py`、asyncua `server/internal_subscription.py` 及监控项服务。

**方法：** 先逐条按原清单给的文件和行号读码，再在**会话临时目录**写探针（不写入项目目录、不改源码、不改配置），用项目自己的 `.venv` 驱动真实组件。探针一览见第8节。

**边界（如实）：**

- 数据库实例 55432/55433 在复核时**没有监听**（`netstat` 无结果），我没有启动它。所以凡涉及真实库的结论——Q02 的 SQL 执行、Q03 的库内实际提交顺序、Q07 的段行形态——只做到读 SQL 与队列写序，**没有在真实库里执行**。
- 没有运行整套测试、浏览器用例和 D44–D48 的任何测量。
- 原清单第1节引用的三个测试事实本次逐个重跑：`test_comm_log.py` 45通过（0.15秒）；`--collect-only` 收集632项；模拟配置/确定性/冻结/编码选择集 7通过、6未选中。三者一致，不构成全量通过的证据。
- 用语：**成立·已复现**＝用真实组件或真实 Owner 跑出了原文所述现象；**成立·读码核对**＝路径逐行核对但未执行；**限定后成立**＝现象属实，触发条件或量级比原文窄。

## 3. 源码问题逐项复核

### Q01：Modbus 取消被当作请求超时继续处理

**结论：成立·已复现。** 原文的行号与机理全部准确。

**证据链：**
1. pymodbus 3.15.0 的异步 `execute` 在等待应答时捕获 `CancelledError`，改抛 `ModbusIOException("Request cancelled outside library.")`（[transaction.py](../.venv/Lib/site-packages/pymodbus/transaction/transaction.py)第227–231行）。
2. [modbus.py](../htsg/modbus.py)第155–158行把 `ModbusIOException` 与 `TimeoutError` 放在同一分支：记一次超时、`continue`。取消请求因此消失，`poll` 返回一个 `TIMEOUT` 批次，`session` 循环（第210–223行）带着下一次节拍继续。
3. [runtime.py](../htsg/runtime.py)第149–152行 `_end` 对每个任务只调用一次 `cancel()`，随后 `gather` 无期限等待；第157行用它停采集任务。

**探针结果（真实 `AsyncModbusTcpClient`＋项目 `ModbusAcquirer.run`，本机回环）：**

| 对端行为 | 在随机时刻取消，取消被吞的次数 |
| --- | --- |
| 立即应答 | 0 / 12 |
| 0.6秒后应答 | 8 / 12 |
| 从不应答 | 11 / 12 |

沉默对端的单次探针里，取消后6秒内采集器又产出5个 `TIMEOUT` 批次；对已被吞掉一次取消的任务再取消一次，3秒后仍未结束（第二次取消又落在在途读请求里）。

**进一步定位：**
- **触发条件**是“取消落在读请求在途期间”。每轮计划节拍里，请求在途时长最长为0.8秒请求超时（`Timing.modbus_request_timeout`），所以对端慢或沉默时，一次取消被吞的概率接近请求占整秒的比例；对端即答时，请求在途只有毫秒级，探针里一次也没吞。
- 同形的“单次取消＋无期限 gather”收尾还出现在：[tests/live_support.py](../tests/live_support.py)第94–100行 `Rig.__aexit__`、[tests/test_simulation.py](../tests/test_simulation.py) 的 `Chain.__aexit__`，以及 [web.py](../htsg/web.py)第121–132行 lifespan 结束时调用的 `Runtime.stop()`。
- OPC UA 侧未见同类：[ua.py](../htsg/ua.py)第433–434行显式重抛 `CancelledError`。asyncua 库内部未审。
- “两轮之间取消”（原文要求一并覆盖）在探针里属于即答对端的常见落点，0/12被吞，可作对照，本身不是缺陷现象。

**与历史挂起的关系（原文的限定正确）：** 执行记录记载的 `test_capstone_c::test_full_scale_nominal_run_stays_quiet_when_nothing_changes` 那次挂住，对端是即答的 Modbus 夹具；按上表，本条对它的解释力很弱，因果仍无证据。

### Q02：崩溃收尾用全运行最晚证据，可能延长另一来源的历史

**结论：成立·读码核对＋纯逻辑复现。** 未在真实库里执行 SQL。

**证据链：**
1. [archive.py](../htsg/archive.py)第293–302行：证据取 `greatest(启动时刻, 全 run 的水位最大值, 全 run 的观测/结果 effective_wall 最大值, 全 run 的状态记录最大时刻（不含 CLOCK_JUMP）, 全 run 的故障确认最大时刻)`，没有按 `source_id` 或 `seg` 过滤；第307–311行把该 run 里所有 `end_wall IS NULL AND start_wall IS NOT NULL` 的段一律设为这个值、原因 `EXIT_TRUNCATED`。
2. [history.py](../htsg/history.py)第181行：段已有 `end_wall` 时，`until` 取段终点，**水位只对未结束段的尾部生效**（第182–183行）。所以被截断段的最后一条记录一直保持到该 run 的最晚证据。
3. [history.py](../htsg/history.py)第429行：`coverage_end` 对有 `end_wall` 的段直接取 `end_wall`，同步被抬高。

**复现（`history.assemble` 合成输入）：** 某源观测1001、该段水位1002、段被收尾为 `EXIT_TRUNCATED`＝1010：保持终点＝1010、原因 `SEGMENT_END`、`coverage_end`＝1010；同一输入去掉收尾、段保持未结束：保持终点＝1002、原因 `WATERMARK`。

**进一步定位：**
- 默认部署有两个源（`mb_field` 每秒有轮次记录，`ua_field` 每10秒一次水位）。崩溃时全 run 最晚证据通常来自 Modbus；OPC UA 段因此被延长到 Modbus 的最后记录。**典型偏差约为一个水位周期加提交滞后（10秒量级）**，不是小时级；违背的是终版第5节“不确定尾段以实际持久化水位为准”的**口径**。
- 现有测试把 run 级证据当作预期：[test_run_segments.py](../tests/test_run_segments.py)第60–82行（`expected` 取全部观测/水位/状态的最大值）、[test_capstone_d.py](../tests/test_capstone_d.py)第112–115行；二者都是**单源**，单源下 run 级与源级相同，所以没有覆盖本条。
- 与 Q07 的关联：Q07 产生的“从未收尾的段”（`end_wall` 为空）同样会被第307–311行用 run 级证据收尾。此时回放里最后一条仍受缺口行约束（见 Q07 复现），但段元数据显示的终点不是该段自己的证据。

### Q03：RUN_END 可能先于恢复期间待提交的数据入队

**结论：成立·已复现。** 原清单里数值“order”的说明也正确——写序以队列顺序为准。

**证据链：**
1. [owner.py](../htsg/owner.py)第221–232行 `_submit`：源处于 `RECOVERING` 时，组进入 `s.pending`（第227–228行）。第265–271行 `committed()` 确认恢复单元后才把它们依次交给 `commit`。
2. [owner.py](../htsg/owner.py)第872–880行 `_run_group` 直接调用 `self.commit(group)`，**不经 `_submit`**，不受源的归档状态约束；第892–906行 `shutdown` 先对每个开着的段 `_emit` 一个 SEG_END（走 `_submit`，同样可能被 pending 压住），随后第906行立刻提交 RUN_END。
3. [commitq.py](../htsg/commitq.py)第67–80行：单写线程、队首未决不可越过，所以**入队顺序就是库内提交顺序**。

**复现（真实 `Owner`＋真实 `CommitQueue`＋桩路由，桩路由把恢复单元的确认卡住，直到停止之后才放行）：** 写入顺序为：

`RUN_START(1) → SEG_START(2) → BATCH(3) → RECOVERY(5) → RUN_END(8) → BATCH(6) → SEG_END(7)`

（4号组被策略拒绝，未写入。）6号是恢复期间接纳的数据，7号是 `shutdown` 为本源收尾的 SEG_END；二者都写在 RUN_END 之后，停止时 `unwritten_at_stop` 为0。

**进一步定位：**
- 触发需要：某源的恢复单元已入队但未确认，此期间又有被接纳的数据，或发生正常停止。恢复单元确认越慢，窗口越宽。
- **后果（读码推断，未在库里演示）：** RUN_END 的提交会写 `run.ended_wall`（[archive.py](../htsg/archive.py)第258–262行）；若此后进程在6、7号写入前被终止，该运行以 `NORMAL` 登记，而它们缺失；`reconcile_unfinished_runs` 只处理 `ended_wall IS NULL` 的运行（第287–289行），不会再碰它。SEG_END(7) 晚于 RUN_END 也意味着该段在 RUN_END 时刻对库而言仍是开着的。
- 原文“覆盖恢复失败、停止写入等待到期”两种边界，本次**未**单独复现。

### Q04：超长带凭据 URL 可能绕过完整脱敏

**结论：限定后成立·已复现。** [commlog.py](../htsg/commlog.py)第58–63行先按 `SCRUB_INPUT_LIMIT`＝4096 预截断（并退回最后一个完整“词”），第62行才脱敏。

**探针（全部为合成串，秘密占位为 `SYNTHSECRET` 重复）：**

| 输入形态 | 输出是否含秘密前缀 |
| --- | --- |
| A：单个 URL，`@` 在 4096 之后，URL 内无空白 | **含**（512字符里是秘密重复） |
| B：前有一句话，其后同一 URL 作为最后一个词 | 不含（最后一个词被整个丢掉，输出只剩前面的话） |
| C：URL 的 `@` 在 4096 之后，URL 之后还有很多空白分隔的词 | **含** |
| D：秘密中带空格（userinfo 写成多个词） | **含** |
| E：5000字符前缀＋ `password=…` | 不含 |
| G：`password=<值>` 跨4096边界 | 不含 |
| F：短 URL（对照） | 不含，输出 `opc.tcp://***@localhost/` |

**修正与进一步定位：**
- 原文“预截断移除了辨认 userinfo 所需的后半段”成立；但“输出仍含秘密前缀”**只在截断落点不能退回到 URL 之前**时成立：URL 恰是最后一个空白分隔词时（案例 B）整个丢掉、不泄漏。`key=value` 形式的 `_SECRET` 能匹配被截断的半截值，也不泄漏。
- **可达性：** 输入来自 pymodbus 异常文本、`repr(OSError)` 和 asyncua 异常。Modbus 源没有凭据；OPC UA 源的凭据按配置走环境变量（`Source.username_env/password_env`），[ua.py](../htsg/ua.py)第383–389行 `_plain` 在入日志前先把这两个环境变量的**实际值**替换为 `***`，与长度无关。所以真正可能泄漏的只有**不在这两个环境变量里**的凭据，例如被写进 `endpoint` 的 userinfo（`Source.endpoint` 无格式校验）。再叠加“异常文本 ≥4 KB 且含该 URL”，实际概率很低，但终版第15.1节对凭据脱敏是无条件规定，条目本身成立。

### Q05：部分请求超时仍可能被计为稳定通信并清零失步累计

**结论：成立·已复现（Owner 层）。** 行号 [owner.py](../htsg/owner.py)第734–737行准确。

**复现：** 一源 `resync_count`＝3；连续74秒每轮两个请求（一个 OK、一个 TIMEOUT，整轮 outcome 为 `OK`）：`resync_count` 归零。对照：整轮 `TIMEOUT` 74秒：保持3。

**进一步定位：**
- 分类来自 [modbus.py](../htsg/modbus.py)第169–176行：`answered` 非零即整轮 `OK`。该分类同时决定：(a) [owner.py](../htsg/owner.py)第734–737行是否中断稳定计时；(b) [modbus.py](../htsg/modbus.py)第217–222行 `consecutive_timeouts` 是否复位（部分超时轮走 `else` 分支清零，**永远不计入“连续3轮超时重连”**）——原清单未提到 (b)。
- 稳定计时的清零在 [owner.py](../htsg/owner.py)第499–507行 `_settle`。
- **合同归属需说明：** 终版第9、11节对“单次超时”“该轮受影响输入”有规定，但没有明文规定“一轮里部分请求超时、部分成功”属于“超时轮”还是“通信有效轮”。原清单把它写成与“超时中断连续稳定区间”的既定语义不符，这是对合同的一种解读，本次复核只确认代码行为、不裁定归属。第15.1节要求的“按请求保留失败”已实现（[owner.py](../htsg/owner.py)第757–768行）。
- **可达性：** `build_plan` 对默认研究配置只得到1个读请求（`holding/1/0+16`），所以默认配置下不会出现部分超时轮；只有现场点表需要 ≥2 个读请求（不同设备号、不同寄存器类型、地址不连续、超过125个寄存器）时才会。

### Q06：请求失败详情可未经限长进入业务入口队列

**结论：限定后成立·路径已复现。**

**证据：** [modbus.py](../htsg/modbus.py)第157行把原始 `str(error)` 放进 `RequestResult`；[ingress.py](../htsg/ingress.py)第78–97行 `offer` 不处理批次内容；清理在 [owner.py](../htsg/owner.py)第764行消费时。探针：合成5,000,029字符的异常文本，`poll` 交出的批次与 `Ingress.queue` 里排队的批次详情长度都是5,000,029。对比 `attempt_failed` 路径在入口就清理并截断（[ingress.py](../htsg/ingress.py)第61–70行）。

**修正与进一步定位：**
- 内存上界＝`limits.ingress`（4096）× 每批请求数 × 单条详情长度。pymodbus 3.15.0 自己构造的异常文本是固定短模板（[transaction.py](../.venv/Lib/site-packages/pymodbus/transaction/transaction.py)第124–136、173–180、239–241行），asyncio `TimeoutError` 的文本为空，所以**真实触发需要异常文本异常膨胀**，原文“队列条数有界不等于详情内存有界”在理论上成立。
- 合同适用性：第15.1节“新增诊断事件的入队数量和记录长度也须有界”针对采集侧新增的诊断事实；`RequestResult.detail` 随输入批次入队，不属于 `attempt_failed` 那类诊断事件，条文是否覆盖它属于解释问题。

### Q07：lost_seg 不能覆盖所有丢失期间的段收尾

**结论：成立·三条边界均已在 Owner 层复现。** 原文对影响范围的限定（不能据此宣称查询直接跨缺口保持）正确，下面已复现。

**证据与复现（真实 `Owner`，提交接收桩可拒绝）：**

| 序号 | 触发序列 | 结果 |
| --- | --- | --- |
| ① | 段1落库→提交被拒（LOST，`lost_seg`＝1）→来源失效（SEG_END 因 LOST 未入队，`open_segment` 置假）→3–6秒内恢复 | 恢复单元只有 `GAP / SEG_START / WATERMARK`，**没有段1的 SEG_END** |
| ② | 段1落库→`begin_generation` 的组（SEG_END＋SEG_START）被拒 | 此时 `lost_seg`＝2（新段，从未落库）；恢复单元的 SEG_END 指向**段2**；已落库的段1不被收尾 |
| ②′ | 段1落库→连接断开（`source_down`→`_start_resync` 的 SEG_END 组被拒）→新代次 | `lost_seg`＝**None**（[owner.py](../htsg/owner.py)第536–538行先清 `open_segment` 才 `_emit`）；恢复单元的 SEG_END 同样指向段2；段1不被收尾 |
| ③ | 恢复单元在途（`RECOVERING`，`lost_seg`＝1）→待发队列溢出 `PENDING_OVERFLOW`（[owner.py](../htsg/owner.py)第230–232行）→`_archive_lost` 把 `lost_seg` 改成在途恢复单元的新段2→首个恢复单元又被策略拒绝 | 下一个恢复单元的 SEG_END 指向段2；段1不被收尾 |

**修正：**
- 执行记录第339行的第③条写“`RECOVERING→LOST` 后恢复单元又被策略拒绝”；实际只靠 `refused()` 路径（[owner.py](../htsg/owner.py)第285–294行）**不会**改 `lost_seg`，复现里该路径下下一个恢复单元仍正确收尾段1。触发 `lost_seg` 被改写的是 `RECOVERING` 中重新进入 `LOST`（如待发队列溢出），再叠加首个恢复单元被拒。
- ②′ 是本次新定位：任何一次“来源 RESYNC 的 SEG_END 组恰被队列/策略拒绝”都走这条路径，比第②条（恰是含 SEG_START 的组被拒）更常见。

**进一步定位：**
- 库内形态（读 [archive.py](../htsg/archive.py)第229–235行推断，未在真实库执行）：SEG_END 对从未有过 SEG_START 的段号执行 upsert，生成只有终点、`start_wall` 为空的“幻影行”；段1保持 `end_wall` 为空。
- **查询仍受约束（复现）：** `history.assemble` 合成输入——段未收尾、水位1001.5、缺口起点1002：保持终点＝1001.5（`WATERMARK`）；无水位：＝1002（`GAP`）；既无水位也无缺口：无终点（报“未知”）。所以影响限于段元数据与异常退出时的收尾终点（见 Q02），不是回放把值保持过缺口。

## 4. 测试准备缺口逐项复核

### V01（已解决）：属实

`de5f644` 含27个文件，其中新增的 `acquirer_support.py`、`test_comm_facts.py`、`test_comm_log.py`、`test_comm_protocols.py`、`test_health_communication.py`、`test_resync_health.py`、`test_web_comm.py` 共7个；`git ls-files tests` 为78项（75个 `.py`、2个 `.mjs`、1个 `.sql`），与磁盘一致；`git status --ignored` 中 `tests/`、`docs/`、`htsg/`、`scripts/` 下未跟踪的只有 `__pycache__` 和 `docs/evidence/` 下的本地证据，没有被遗漏的用例文件。附带事实见第6节 B。

### V02：事实成立

[conftest.py](../tests/conftest.py)第19–25行读 `.local/database.json`、连管理库、建临时库；第50–55行启动本机 Chrome。**复核时 55432/55433 无监听**，与执行记录第329行（重启后实例未启动导致挂起）一致。本次没有启动它，也没有读到“实例当前停机”以外的连通信息。

### V03：成立·摘要精确复现

用冒烟脚本同一方法（运行入口实际加载的 `htsg` 模块集合）复算：

| 项 | D48 证据（`smoke-20261013-104559.json`） | 当前 |
| --- | --- | --- |
| 提交／是否干净 | `beeb188`／`clean: false` | `de5f644` |
| 加载的 `htsg` 模块数 | 25 | **26**（新增 `commlog.py`） |
| 代码摘要 | `2dd796b4…` | `f978c83f…` |
| 页面摘要 | `b2f966fb…` | `cbe3013a…` |
| 配置摘要 | `6e1f673d…` | `6e1f673d…`（未变） |
| 结构摘要 | `2479b07e…` | `2479b07e…`（未变） |

与原清单一致。[交付记录](HTSG_V1_交付记录_v0.1.md)第33行、[执行记录](HTSG_V1_开发执行记录_v0.1.md)第234行（H0）仍称25个模块、摘要相同。

### V04：成立，**量值需修正**

引用的行号全部属实：[measure.py](../scripts/measure.py)第88–90行、[live_support.py](../tests/live_support.py)第42行、[交付记录](HTSG_V1_交付记录_v0.1.md)第66行。但这三处共同的解释——“asyncua 服务器的保活比协商值晚一个发布周期，请求2实际静默3秒，请求1给出设计假设的2秒”——**与实测不符**：独立探针（asyncua 2.0.1 服务器、一个静止变量、项目自己的 `open_subscription`）测得相邻消息间隔

| 请求的保活计数 | 实测相邻保活间隔 |
| --- | --- |
| 2（默认） | **4.0秒**（4.0、4.0、4.02） |
| 1（measure/smoke/Rig 使用） | **3.0秒**（3.01、3.0、2.99、3.0、3.0） |

与读码一致：[internal_subscription.py](../.venv/Lib/site-packages/asyncua/server/internal_subscription.py)第128–137行 `has_published_results` 在计数 **大于** 最大保活计数时才发，发出后第193行清零，所以静默＝（计数＋2）个周期。后果见[模拟方案复核意见书](HTSG_V1_模拟信号适配审核复核意见书_v0.1.md)第4节 N1。对本节的含义：原清单“计数1的测量不能直接证明默认计数2的静止行为”成立，但计数1的静默＝3.0秒＝终版数据有效期（3秒），**也没有余量**。

### V05：成立；用户已知并留待决定

终版第251、260行、方案第3、175行仍写“六页、未实现”；README、交付记录写七页已实现，与执行记录 D44f 的记录一致。这是用户此前明确“是否回写由用户定（会改终版哈希）”的事项，本次不动。核对中另见**执行记录第359行**仍写“Capstone G 历史记录：曾按错误页面集合通过，纠偏后的页面验收尚未完成”，而同一文件第325行记载 Capstone G 15项在纠偏后重跑通过并重拍证据；两处现并存。

## 5. 第4节“全量验收与现场投运的分界”复核

四个门槛的表述与证据一致：容量报告第120–139行的满量约10.5 TB/年、可用卷约1.33 TB、结论“不通过”属实；632项收集、45项单测、631项分段通过确为不同口径。本次没有新证据改变这一节。

## 6. 复核中补充发现（原清单没有；用户确认前不当作已纳入）

**A. `_run_group` 绕过归档状态，且被拒后不重试（读码核对）。** 出处：核对 Q03 时读到 [owner.py](../htsg/owner.py)第872–880行：`self.commit(group) is False` 时只计 `health["run_group_rejected"]`，不重试、不进入 pending、不受 `is_void`/`RECOVERING` 约束。若 RUN_END 恰在队列满时被拒，库里该运行没有结束登记，下次启动按异常退出收尾（`reconcile_unfinished_runs` 只认 `ended_wall IS NULL`）；若 RUN_START 被拒，缺的只是那条 RUN_START 状态记录（运行行由该运行第一个写入的组创建，[archive.py](../htsg/archive.py)第173–177行）。本次没有复现。

**B. `.gitignore` 的 `tests/` 与 `docs/` 规则。** 出处：核对 V01 时发现 `git check-ignore -v --no-index` 命中 `.gitignore` 第14、15行。已跟踪文件不受影响，但**新建在这两个目录下的文件不会出现在 `git status` 里**，要强制添加才会入库。`de5f644` 里这7个新增测试文件即是如此（只是事实陈述）。

**C. 保活间隔低估。** 出处：核对 V04 时的探针，详见 V04 与模拟意见书 N1。

## 7. 未能核实或受限的事项

- Q02 的 `reconcile_unfinished_runs` 与 Q07 的段行形态未在真实库中执行（库未启动）。
- Q03 的库内提交顺序以队列写序代替（单写线程、队首未决，推理成立，但未经库验证）。
- Q03 的恢复失败、停止写入等待到期两种边界，Q07 原文所说“丢失组本身含 SEG_START”以外的其他 lost_seg 边界，本次未逐项复现。
- 未重现任何一次历史挂起；Q01 与它们的因果关系仍无证据。

## 8. 探针一览（均在会话临时目录运行，未写入项目）

环境：Windows，项目 `.venv`（CPython 3.13、pymodbus 3.15.0、asyncua 2.0.1）。

| 编号 | 对应 | 做法 |
| --- | --- | --- |
| P1 | Q01 | 本机 TCP 对端接收但不应答；`ModbusAcquirer.run` 作为任务；读请求在途时 `cancel()` |
| P1b | Q01 | 同上，对端分三种行为，每种12次，在周期内随机时刻取消 |
| P2 | Q03 | 真实 `Owner`＋真实 `CommitQueue`＋桩路由；先拒绝一组触发 LOST，再让恢复单元的确认被卡住，其间接纳数据并 `shutdown` |
| P3 | Q07 | 真实 `Owner`，提交桩可拒绝；四种序列（①②②′③） |
| P4 | Q02、Q07 | `history.assemble` 合成输入 |
| P5 | Q05 | 真实 `Owner`；整轮 `OK` 但一个请求超时的轮次 74 秒，对照整轮超时 |
| P6 | Q04 | `clean_detail` 合成7例 |
| P7 | Q06 | 假客户端抛5 MB 的 `ModbusIOException`，经 `poll` 与 `Ingress.offer` |
| P8 | V04 | 真实 asyncua 服务器＋项目 `ManagedClient`/`open_subscription`，保活计数 2 与 1 各16秒 |
| P9 | V03 | 复算 `smoke.py` 的候选摘要 |
| P10 | 第1节 | 重跑原清单引用的三个快速测试事实 |
