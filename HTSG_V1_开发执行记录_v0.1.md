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

## 2. 偏离、风险与未验收

- 无偏离设计。方案"位置"列写的是改造旧文件；本次以新模块并存，D45 再删旧（见第 0 节）。
- 现场项（真实 Acheron、UA 网关、实际卷、一年容量实测）均未验收，随 D46–D48 单列。

## 3. 下一步

D08（`owner.py` 接纳完整批次）→ D09 → D10（`derive.py` 已就绪）→ D11 → Capstone B。
