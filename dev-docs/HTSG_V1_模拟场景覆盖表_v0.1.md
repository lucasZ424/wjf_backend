# HTSG V1 模拟场景覆盖表（N2）

版本：v0.1｜日期：2026-10-20｜代码提交 `a8a6a0b` 起｜使用方式见[模拟现场使用说明](HTSG_V1_模拟现场使用说明_v0.1.md)

**性质：** 只陈述现状，不是验收，也不是对模拟器的改动要求。出处：[模拟信号适配审核复核意见书](HTSG_V1_模拟信号适配审核复核意见书_v0.1.md)第4节 N2，用户 2026-10-20 裁定“先出覆盖表（已单测／已 e2e／未覆盖），在表完成前不得声称完整模拟场景已验收”。行取自终版第11节“实施验收”清单，另加两行相邻场景。

**读法：** 四列分别回答——哪个测试在不经协议的地方验证过（“单元／组件”）；哪个测试在**真实协议服务器**上验证过（项目测试夹具 `FieldModbus`／`FieldModbusDevices`／`FieldUa`／`PausableProxy`，不是研究模拟器）；**研究模拟器**（`htsg/simulation.py`＋`config/research-sim.json`）能不能产生这个场景；完整研究模拟（后端＋模拟现场）里有没有验证过。测试名按文件核对过；标“（未核对）”的格子我没有逐条读过用例内容。

| # | 终版第11节要求验证的行为 | 单元／组件用例 | 真实协议服务器上的用例 | 研究模拟器能否产生 | 完整研究模拟里验证过吗 | 判断 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 单次超时（轮内不重试、保留连接） | `test_modbus_acquisition`：慢设备只花一次 0.8 秒、单次超时保留连接 | `test_partial_requests`（真实从站）；`test_capstone_c::modbus_timeout_static_values_and_recovery`（`FieldModbus.delay=1.5`） | 否：只在“尚无值”时回 `DEVICE_BUSY`，无延迟或超时注入 | 否 | 夹具层已覆盖；模拟层未覆盖 |
| 2 | 连续失败→第3轮重连 | `test_modbus_acquisition::..._three_in_a_row_reconnect` | `test_partial_requests`（单从站整轮超时、多从站全沉默） | 否 | 否 | 同上 |
| 3 | 协议异常、部分请求成功 | `test_partial_requests`、`test_failure_records` | `FieldModbusDevices`（真实从站；R07 端到端另见 `test_web_failure_records`，注入的是已成形的批次，不走线） | 否：只有坏质量整型点回 `DEVICE_FAILURE` | 否 | 夹具层已覆盖；模拟层未覆盖 |
| 4 | 明确断连与恢复 | `test_modbus_acquisition::dropped_connection`、`test_source_validity::explicit_disconnect` | `test_capstone_c::dropped_opcua_server`（真实 asyncua 服务器） | **是**：`disconnect_modbus`／`disconnect_opcua`，各20秒 | D46 `measure.outage` 使用（证据绑定旧候选，待 G3 刷新） | 已覆盖；证据待刷新 |
| 5 | 调度跳拍（不补发积压轮次） | `test_tick_numbers` | 无 | 否 | 否 | 仅单元层 |
| 6 | 沉默（不关连接）：3秒失效、3–6秒同代次恢复、6秒静默重建 | `test_source_validity`（失效、3–6秒恢复、6秒告警＋重建、回退） | `test_capstone_c::silence_inside_the_window...`（`PausableProxy`） | 否：断连场景是停服务器，不是沉默 | 否 | 夹具层已覆盖；模拟层未覆盖 |
| 7 | 静止源／纯保活／稀疏变化 | `test_source_validity`（保活证明活动、静止点不被时间淘汰）、`test_simulated_keepalive`（适配后的保活间隔与相位） | `test_capstone_c::nominal_run_stays_quiet` | 否：208路信号均带噪声和振幅，默认研究运行里**没有任何静止点**；`measure.make_config(quiet=True)` 可清零 | 执行记录 R06 条目：`measure.py --steady 600 --quiet`（真实200点＋8路，600.1秒，`expiries=0`）；D47 容量测量也用 quiet 配置。证据绑定旧候选，待 G3 刷新 | 研究默认运行不产生；quiet 配置有 |
| 8 | 静止（数值不变）的越限持续确认 | owner 层用例（`test_fault_owner` 等，不经模拟器） | 无 | 否 | 否 | 仅单元层 |
| 9 | 缺号、Overflow、同批短暂正常值 | `test_ua_channel`（缺号、Overflow、同批同点两版本）、`test_fault_owner`（同批第二个版本重启计时） | 无夹具能注入（`FieldUa` 只有 `set(value, good)`） | 否：无发布层注入手段 | 否 | 仅单元层 |
| 10 | 服务端修订参数（含队列被修订为1） | `test_timing_config`、`test_ua_client`（修订值替换请求值、队列1仅标注） | 部分：`test_ua_client` 对真实 asyncua 服务器（修订值取自实际协商） | 否：无参数入口，用 asyncua 默认 | 否 | 单元＋单服务器；模拟层未覆盖 |
| 11 | 连续失步退避（5次进入持续失步） | `test_source_validity`、`test_resync_health` | `test_comm_protocols`（连上不应答、拒绝连接、订阅被拒的五周期） | 部分：断连场景20秒走不完5个周期 | 否 | 夹具层已覆盖 |
| 12 | 库内后台任务已停用（无自主 recreate，正常 Publish 与通道续期继续） | `test_ua_client` | 同左（真实 asyncua 服务器） | 不适用 | 不适用 | 已覆盖 |
| 13 | 坏质量 | `test_ua_channel`（非 Good 值保留负载与完整状态码）；`test_web_replay`（BAD 区间标红） | `test_comm_protocols`（首值坏质量也是通信） | **是**：`bad_quality`（2个点） | （未核对） | 已覆盖 |
| 14 | 冻结（信号断联） | `test_simulation::freeze_preserves_source_timestamp_not_receipt_status` | 无 | 有 `freeze` 场景（保留值与 OPC UA 源时间戳），**但 freeze＝信号断联的新判据未定**（用户 2026-10-09 决定随模拟按新判据重新规划） | 否 | 不纳入本表判断 |
| 15 | 满200点＋8路联调：端到端延迟、队列峰值、维护阻塞、恢复时间 | 无 | `test_capstone_c`（满量，内存提交） | **是** | D46（`scripts/measure.py`＋`test_measure`）：稳定负载、初值突发、两种断连；“维护阻塞”是否在其中（未核对）。证据绑定旧候选，待 G3 刷新 | 部分；待 G3 |

**场景是单一互斥状态：** 研究模拟器同一时刻只有一个场景（`POST /scenario`，下一次生成才生效），组合场景（如断连叠加越限）产生不了。**网页没有场景切换入口**，只有模拟控制端口的 HTTP 接口。

**结论（只列事实）：**
- 研究模拟器能产生的终版场景只有：明确断连（两种协议）、坏质量、满量稳定负载与初值突发；quiet 配置另可产生整源静止。
- 第 1、2、3、5、6、8、9、10 行在模拟层**没有覆盖**；其中 1、2、3、6、11 在真实协议服务器夹具上有覆盖，5、8、9 只有单元层。
- 在这张表有人逐行认领之前，不得写“完整模拟场景已验收”；D46／D47／D48 的证据也只对旧候选成立，G3 刷新后才对新候选成立。
