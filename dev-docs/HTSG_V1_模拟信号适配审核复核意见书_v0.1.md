# HTSG V1 模拟信号与当前架构适配审核复核意见书

版本：v0.1｜日期：2026-10-14｜复核对象：[模拟信号与当前架构适配审核 v0.1](HTSG_V1_模拟信号与当前架构适配审核_v0.1.md)（未提交文件）｜代码基线：`de5f644`｜依据：[终版设计](HTSG_V1_终版设计_v1.0.md)第3.3、3.4、4、8、11、16节，`config/research-sim.json`，已安装的 asyncua 2.0.1 / pymodbus 3.15.0 源码。

本文只复核、定位，**不提出方案**，不新增场景或参数，不修改代码、配置和已提交文档。另有[问题清单复核意见书](HTSG_V1_全量测试前问题清单复核意见书_v0.1.md)，两份共用同一轮探针。

## 1. 结论

**不能直接回复“方案合理”，现出本意见书。** 我对“模拟方案”的理解是：审核报告所评价的整套对象——`htsg/simulation.py` 模拟器、`config/research-sim.json`、`scripts/research_lab.py` / `measure.py` / `smoke.py`，以及 README 与使用说明里对它们的描述——是否匹配终版设计。分两层看：

- **作为接入当前真实协议链路与批次架构的信号源：合理。** 审核第2节的四条相符陈述逐条核实成立（第2节）。
- **作为验证终版第11节时限与恢复行为的信号源：存在已确认的偏差。** SIM-01、SIM-03 成立；SIM-02 应由“待验证项”上调为“已由探针确认的服务器行为偏差”，且仓库里现有的量值低估了一个周期；SIM-04 部分成立（对 `smoke.py` 退出码的陈述需限定为命令行）。另有一项覆盖限制清单（N2）供后续规划使用。

| 条目 | 审核定级 | 复核结论 | 要点 |
| --- | --- | --- | --- |
| 第2节四条相符陈述 | — | 成立 | 全部有源码与实测支持 |
| SIM-01 | P2 | 成立·已复现 | 数值全部吻合；补充：仅 `simulation.enabled` 时执行；1 GiB 预算即拒绝默认配置 |
| SIM-02 | 重点验证项 | 成立，**应上调**·已复现 | 实测保活间隔＝（计数＋2）个周期：默认 4.0秒，计数1为 3.0秒（＝数据有效期） |
| SIM-03 | P2 | 成立（行号逐条核对无误） | 该文自标“未按新规定整改”；第11行“启动命令仍启动旧版程序”已不属实；按第47行配置会被校验拒绝 |
| SIM-04 | 覆盖限制 | 部分成立 | “noticed”、恢复条件、场景覆盖三点成立；`smoke.py` 不转失败退出只对命令行成立，`test_smoke.py` 对同一报告有断言 |
| N1 | — | 复核补充 | 保活间隔实测，见第4节 |
| N2 | — | 复核补充 | 模拟器与终版验收项的覆盖对照，见第4节 |

## 2. 审核第2节“与当前架构相符的部分”逐条核实

1. **协议链路是当前采集链路——成立。** [simulation.py](../htsg/simulation.py)第115–154行启动真实 `ModbusTcpServer`，第156–179行启动真实 asyncua `Server`；该模块只导入 `config`、`research_config`、`timing`，不碰 owner 或旧引擎。第85–86行：写请求与非3/4功能码一律回 `ILLEGAL_FUNCTION`，`test_real_protocols_fixed_values_bad_quality_and_write_rejection`（[test_simulation.py](../tests/test_simulation.py)第142行起）核对了写被拒。验证链路 `Chain`（同文件）用的是真实 `ModbusAcquirer`/`UaAcquirer`→`Ingress`→`Owner`。
2. **当前配置未重新启用旧捕获合同——成立。** `Settings` 的各模型 `extra="forbid"`。本次逐一验证：`profiles`、`limits.capture_seconds`、`points[].stale_after` 加进研究配置都被拒绝。研究配置本身通过：208路输入、209路派生，开发配置2路输入。
3. **值、质量、源时间戳可分开观察——成立。** 第199–224行 `generate` 生成值/质量/源时间戳，`freeze` 场景保留指定点的三者；[monitored_item_service.py](../.venv/Lib/site-packages/asyncua/server/monitored_item_service.py)第252行：没有过滤器时用 `DataChangeTrigger.StatusValue`，值与状态不变则不通知——所以“每秒向服务器写值”不等于“后端每秒收到通知”，审核的限定成立。
4. **断连场景是真实服务停止/重启——成立。** 第234–245行 `reconcile_servers` 对 `disconnect_*` 场景停止/重启对应服务器；不模拟半开连接或逐请求超时。

附带核实：审核第1节的补跑结果可复现——选择集 7通过、6未选中；旧预算函数返回 `1,352,388,608` 字节。

## 3. 偏差逐项复核

### SIM-01：旧 frame 预算估算仍参与配置拒绝

**结论：成立·已复现，数值与行号全部吻合。**

- [research_config.py](../htsg/research_config.py)第84–96行：`秒数 ×（输入数＋派生数）× 1024 ＋ 秒数 × 输入数 × 1024 ＋ 8 MiB`；第171–173行与 `development_budget_bytes` 比较，超限抛 `ValueError`；[config.py](../htsg/config.py)第339行在模型校验里调用；[simulation.py](../htsg/simulation.py)第50行构造时再校验，第296行 `status()` 回显。[research_lab.py](../scripts/research_lab.py)第94–104行只打印剩余容量警告，但它之前的加载已经过了这道门，审核的判断（不只是提示文字）成立。
- **复现（改内存副本，不写文件）：**

| `development_budget_bytes` | `validation_duration_seconds` | 结果 |
| --- | --- | --- |
| 10 GiB | 2100（默认） | 接受（估算 1,352,388,608） |
| 10 GiB | 16,764 | 接受 |
| 10 GiB | 16,765 | **拒绝** |
| 10 GiB | 20,000 | **拒绝** |
| 2 GiB | 2100 | 接受 |
| 2 GiB | 16,764 | 拒绝 |
| 1 GiB | 2100 | **拒绝**（默认配置本身被拒） |

每秒按 640,000 字节计，10 GiB 下限约 16,764 秒。字段自身允许的范围是 1–86,400 秒（[research_config.py](../htsg/research_config.py)第60行）、预算字段允许 ≥1024 字节，所以**配置模型的字段范围与这道门禁互相冲突**。

- **补充定位：** 门禁只在 `simulation.enabled` 为真时执行（第121–122行提前返回），开发配置与生产模板不经过；对现行 2100 秒默认值不起作用。审核“不能据此报告默认满量启动已坏”成立。

### SIM-02：measure/smoke 改变保活参数，不能替默认模拟配置证明静止源稳定

**结论：成立，且应由“重点验证项”上调为“已确认的服务器行为偏差”。** 审核引用的行号全部准确：[measure.py](../scripts/measure.py)第84–90行、[smoke.py](../scripts/smoke.py)第117行、[internal_subscription.py](../.venv/Lib/site-packages/asyncua/server/internal_subscription.py) 保活逻辑；`freeze` 只冻结两个点（`bridge_mvv_01`、`temp_inner_01`）；`research-sim.json` 没有 `timing` 键，默认保活计数2、数据有效期3秒。

审核写“本次没有启动服务复现”。本次复核已用探针确认，结果见第4节 N1：**实际保活间隔比审核（以及仓库里的注释）所设想的更长**。

**另一条补充：** 研究配置里208路信号**全部**带非零噪声和振幅（`noise==0` 与 `amplitude==0` 的信号各为0个），所以默认研究运行里没有任何一个 OPC UA 点会静止；审核说的“模拟器每秒都在变化，所以演示中不会触发”成立，同时意味着稀疏通知、纯保活发布、3–6秒失效窗口在默认研究运行里**从不出现**。`freeze` 在原设计里模拟的是信号断联（用户 2026-10-09 的决定），本文不把它当作“数值不变”的覆盖。

### SIM-03：使用说明仍要求旧捕获流程和不存在的脚本

**结论：成立。** 行号逐条核对：[模拟现场使用说明](history/模拟现场使用说明.md)第47行（`profiles`）、第49行（捕获预算）、第60、62行（网页管理/总览切换场景）、第62–64行（Pre/恢复/Post、600/60/1200秒、`stale_after`）、第70–74行（`validate_ui.py`、`validate_research_cycle.py`）；`scripts/` 下现有 `bootstrap_db.py`、`export_openapi.py`、`measure.py`、`register_task.ps1`、`research_lab.py`、`run.ps1`、`smoke.py`、`verify_resilience.py`，两个脚本确不存在；[web.py](../htsg/web.py) 与 [app.js](../htsg/static/app.js) 没有场景切换入口，模拟器的 `POST /scenario` 仍在（[simulation.py](../htsg/simulation.py)第337–344行）。

**补充：**
- 该文第9–11行自标“0.2.2 现有实现与使用信息（未按新规定整改）”，并写明“现有启动命令仍启动旧版程序”。后一句**现已不属实**：[research_lab.py](../scripts/research_lab.py)第158–172行经 `htsg.cli` 启动的是新链路。README 第55行仍把这份说明作为模拟现场的使用入口链接。
- 按第47行去配置 `profiles`、按第49行去配置捕获类 `limits`，都会被配置校验拒绝（第2节已验证）。
- 同类遗留词（`stale_after`、`profiles`、`validate_ui`、Pre/Post）还出现在 `dev-docs/history/模拟现场验证记录.md`、`dev-docs/history/验证记录.md`、`dev-docs/history/历史追忆容量与刷新修复.md` 和 [模拟现场前后端开发与验证方案](history/HTSG_V1_模拟现场前后端开发与验证方案_v0.1.md)；它们文首都带“现行规定以终版设计为准”的标记，属于已披露的历史内容，我只确认词的存在，未逐条核对是否指导当前操作。

### SIM-04：测量报告不等于验收通过，场景与停止证据需按含义读取

**结论：部分成立。**

- **成立的三点：** (a) [measure.py](../scripts/measure.py)第368行 `noticed` 的条件是该源“至少一个输入无效”（`not everything_valid(inputs)`），不是6/10秒告警确认；(b) 第372行恢复条件是417量全部有效；(c) 第404–408行只有初值突发、稳定负载和两种断连——坏质量、冻结、热阶跃等场景不在其中。另外场景是单一互斥状态：[simulation.py](../htsg/simulation.py)第73–78行切换、第199–224行下一次生成才生效，`POST /scenario` 立即返回的 `status()` 不证明协议端已应用（断连场景还要等 `reconcile_servers`，同循环内随后执行）。
- **需限定的一点：** 审核说 `smoke.py` 第167–181行“只输出报告，没有将业务检查统一转换为失败退出”。**对命令行成立**；但同一报告在 [tests/test_smoke.py](../tests/test_smoke.py)第18–31行被逐项断言（两次运行的归档源均 `OK`、`queue_failures==0`、协商无违反、`stop=="STOPPED"`、运行记录为两次 `NORMAL`、历史无重复/有序/非部分可用），[tests/test_measure.py](../tests/test_measure.py)第24–49行对测量报告同样有断言（无未解释丢失、无积压、断连被察觉且恢复、归档状态全 `OK`）。所以“报告生成不等于通过”应写成“**命令行**运行不等于通过，经 pytest 运行时由用例判定”。
- 第91–104行以 HTTP 端口关闭报告 `STOPPED`、第150行再等2秒读运行记录——属实，且 `test_smoke.py` 的 `runs == [(True,"NORMAL"),(True,"NORMAL")]` 正是对运行结束证据的核对。

## 4. 复核中补充发现（审核没有；用户确认前不当作已纳入）

### N1：asyncua 服务器的保活间隔是（计数＋2）个周期

出处：核对 SIM-02 时读 [internal_subscription.py](../.venv/Lib/site-packages/asyncua/server/internal_subscription.py)，发现与仓库内的量值说法不一致，随后做了实测。

**探针：** asyncua 2.0.1 服务器、一个静止变量、项目自己的 `ManagedClient`＋`open_subscription`（发布周期1秒），每个设置观察16秒，记录每条发布消息（数据或保活）的到达时刻。

| 请求的保活计数 | 服务端修订值 | 相邻消息间隔（秒） |
| --- | --- | --- |
| 2（默认） | 2 | 4.0、4.0、4.02 |
| 1（measure、smoke、Rig 使用） | 1 | 3.01、3.0、2.99、3.0、3.0 |

读码一致：保活在计数**大于**最大保活计数时才发，发出后清零，静默＝（计数＋2）个周期。

**与仓库现有说法的差异：** [live_support.py](../tests/live_support.py)第39–41行、[measure.py](../scripts/measure.py)第88–89行、[交付记录](HTSG_V1_交付记录_v0.1.md)第66行都写作“比协商值晚一个发布周期”（计数2＝3秒、计数1＝2秒，并称计数1“给出设计假设的2秒”）。**实测晚两个周期。**

**后果与位置（只列事实）：**
- 默认计数2时静默4.0秒大于数据有效期3秒：静止源会反复失效—恢复。这与交付记录第66行的**观测**（“每约4秒来回切换”）相符，但它给出的原因（“实际静默3秒”）不对。
- 计数1时静默3.0秒**等于**数据有效期（[config.py](../htsg/config.py)第152行默认3秒；[owner.py](../htsg/owner.py)第493行判据 `quiet >= ua_data_validity`），没有余量；是否实际抖动取决于1秒 tick 与保活到达的相位，本次**未验证**。所以 measure/smoke/Rig 的“静止”结果是否无抖动，现有证据不足以断言——容量报告“保持静止”一栏的“段重述”成分构成未区分，无法判断其中是否含此类重述（[容量报告](HTSG_V1_满量测量与容量报告_v0.1.md)第133行）。
- [config.py](../htsg/config.py)第174–186行 `Timing.problems()` 的静默模型是“发布周期×保活计数”，对 asyncua 服务器不成立。真实 UA 网关是否遵循规范（按 MaxKeepAliveCount 个周期发保活）**未知**，交付记录第5节已列为现场核验项；本次不对它下结论。
- 模拟器自身用 asyncua 的 `Server`（[simulation.py](../htsg/simulation.py)第156–179行），所以由它提供的“静止源”必然带这个非典型保活行为（读码判断保活逻辑按订阅计数，与节点数无关；未用200节点的 `FieldSimulator` 服务器实测）。

### N2：模拟器与终版验收项的覆盖对照（只列事实）

出处：核对 SIM-02、SIM-04 时对照终版第11节“实施验收”清单与用户 2026-10-09 的决定（freeze 与断连场景随模拟按新判据重新规划）。下表仅陈述现状，**不是对模拟器的改动要求**。

| 终版要求的行为 | 研究模拟器（`htsg/simulation.py`＋`research-sim.json`）能否产生 | 现有其他来源 |
| --- | --- | --- |
| 整源数值不变（静止） | 否：208路信号均带噪声；无场景使其归零 | `measure.make_config(quiet=True)` 清零幅值与噪声（[measure.py](../scripts/measure.py)第94–96行），用于容量测量（[verify_resilience.py](../scripts/verify_resilience.py)第267行）；没有在该模式下叠加越限偏移的脚本或用例 |
| 数值不变的越限持续确认 | 否 | Owner 层用例（[test_fault_owner.py](../tests/test_fault_owner.py)等），不经模拟器 |
| 缺号、Overflow | 否：模拟器没有任何发布层注入手段 | 无夹具注入（`FieldUa` 只有 `set(value, good)`）；用例直接向 `UaChannel` 送构造的发布结果 |
| Modbus 请求级超时、协议异常、部分请求成功 | 否：只在“尚无值”时回 `DEVICE_BUSY`、坏质量整型点回 `DEVICE_FAILURE`（[simulation.py](../htsg/simulation.py)第97、105行），无延迟或超时注入；`disconnect_modbus` 是停服务器（连接被拒） | [tests/protocol_support.py](../tests/protocol_support.py) 的 `FieldModbus.delay` / `.exception` |
| 沉默（不关闭连接）：3秒失效、3–6秒恢复、6秒静默重建 | 否：断连场景是停服务器 | `PausableProxy`（同上），Capstone C 使用 |
| 服务端把队列或保活修订为别的值 | 否：无参数入口，用 asyncua 默认 | — |
| 明确断连与恢复 | 是：`disconnect_modbus`/`disconnect_opcua`，各20秒 | `measure.outage` 使用 |
| 坏质量 | 是：`bad_quality`（2个点） | — |

## 5. 未能核实或受限的事项

- 没有启动完整研究模拟、后端和数据库（库未启动）；N1 用单变量 asyncua 服务器探针，Owner 层是否在计数1下抖动未验证。
- 真实 UA 网关的保活行为未知。
- 第3节 SIM-03 同类遗留词的其他文档只确认词存在。
- 本次未运行 `measure.py`、`smoke.py` 或任何依赖数据库的用例。

## 6. 探针一览

沿用[问题清单复核意见书](HTSG_V1_全量测试前问题清单复核意见书_v0.1.md)第8节环境；与本文有关的有：预算门禁在内存副本上对 `Settings.model_validate` 扫描不同预算/时长；保活探针（该文 P8）；配置里旧键（`profiles`、`limits.capture_seconds`、`stale_after`）加入后的校验结果；`research-sim.json` 信号噪声统计；`build_plan` 对 Modbus 点的读请求数。
