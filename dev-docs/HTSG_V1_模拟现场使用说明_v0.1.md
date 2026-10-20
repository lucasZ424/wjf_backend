# HTSG V1 模拟现场使用说明

版本：v0.1｜日期：2026-10-20｜依据：[终版设计](HTSG_V1_终版设计_v1.0.md)、[交付记录](HTSG_V1_交付记录_v0.1.md)｜适用代码：分支 `dev/htsg-v1-impl`，代码提交 `a8a6a0b` 起｜取代：[旧使用说明](history/模拟现场使用说明.md)（历史文件，自标“未按新规定整改”，不再维护）

**出处：** 用户 2026-10-20 裁定 SIM-03 在首次正式完整模拟前处理——新写现行说明、README 改指向它、历史文件不动（[复核意见书 SIM-03](HTSG_V1_模拟信号适配审核复核意见书_v0.1.md)）。

## 1. 它是什么，不是什么

独立的研究模拟现场：真实的 Modbus TCP 服务器（8 路 Bridge mV/V）和 OPC UA 服务器（200 个工程量点），后端像连真实设备一样轮询、订阅、计算、归档，浏览器读取后端接口。**只在开发模式、只用本机回环地址。** 它的数据是模拟的：默认 660 MW、测点位置、内外温度配对、材料参数与阈值都是科研假设，不代表任何现场机组。模拟器只改变模拟服务器，不向真实设备写任何值。

模拟结果不能代替真实 Acheron 设备、真实 OPC UA 网关、独立卷、一年容量和现场长稳（见[交付记录](HTSG_V1_交付记录_v0.1.md)第5节）。**目前研究模拟器能产生的终版场景有限**，哪些有、哪些没有见[模拟场景覆盖表](HTSG_V1_模拟场景覆盖表_v0.1.md)；在它被逐行认领之前，本说明不声称完整模拟场景已验收。

## 2. 启动与停止

在项目根目录 PowerShell 中执行：

```powershell
.\.venv\Scripts\python.exe scripts\bootstrap_db.py start
.\.venv\Scripts\python.exe scripts\research_lab.py run
```

第一条启动两个开发 PostgreSQL 实例（开机后默认不启动；不启动时所有数据库用例会在约130秒后连接超时）；第二条准备实验库、启动独立模拟现场和后端。浏览器打开 `http://127.0.0.1:8000`。`Ctrl+C` 正常停止（运行以 `NORMAL` 结束；见[交付记录](HTSG_V1_交付记录_v0.1.md)第3节停止语义）。

| 服务 | 默认端口 |
| --- | --- |
| Modbus TCP（模拟） | 15020 |
| OPC UA（模拟） | 14840 |
| 模拟控制接口 | 14841 |
| 网页与后端 | 8000 |

- **登录：** `config/research-sim.json` 里 `dev_admin_login` 为 `true` 时，可直接输入 `admin:admin` 以管理员登录（仅开发模式允许，设为 `false` 并重启即关闭）。原管理员与 viewer 凭据在本机 `.local/research/initial-credentials.json`，凭据不放入网址，不提交该文件。
- **数据库：** 启动脚本在 D 盘已有的两个开发实例里创建随机名 `htsg_research_*` 实验库，连接信息存 `.local/research/database.json`；再次启动复用同一库。**不要手工改 `.local/research` 里的库信息去连现有业务库。** C 盘不部署数据库。
- **重复启动：** 后台仍在运行时，再次启动会因端口或部署锁（`.local/backend.lock`）被拒绝。

## 3. 开发预算与启动估算

默认开发预算 10 GiB（`storage.development_budget_bytes`），同时约束本进程累计写入量和已绑定库的用户表与索引总量；总量包含以前的试验，不是每次启动归零的增量；不含 WAL、系统目录等全部磁盘开销，另有 D 盘至少 1 GiB 剩余空间保护。

启动时脚本打印**估算值**与剩余预算：剩余不足一次完整追忆周期的估算量时给出醒目警告，已耗尽时仍可启动查看已有历史、但新的追忆不能归档。**估算不再是配置门禁**（2026-10-20，SIM-01）：它不会因为估算超过预算而拒绝加载配置，也不是容量保证；真正的拒写由运行时的预算与余量保护决定，并形成归档缺口。

需要开始新一轮实验时：先 `Ctrl+C` 停止，再执行 `scripts\research_lab.py new-lab`，然后 `run`。`new-lab` 在既有两实例里创建新的实验库，旧连接信息存到 `.local/research/archives/<编号>/database.json`，旧库与历史保留（不合并进新实验），不提高预算、不删除旧库；账号继续复用。存在账号未决标记（`.local/research/accounts.pending`）时必须先在原库完成协调，不能用换库跳过。

## 4. 修改模拟设定

统一入口 `config/research-sim.json`。修改后先校验，再停止并重启：

```powershell
.\.venv\Scripts\python.exe -m htsg.cli check --config config\research-sim.json
```

| 配置位置 | 内容 |
| --- | --- |
| `mode`、`dev_admin_login`、`time_basis` | 开发模式、开发登录开关、时间基准（接收时间） |
| `project` | 标题、额定容量、科研说明 |
| `sources`、`points` | 本机端口、寄存器、数据类型／字节序、NodeId、单位、缩放、采样间隔、阈值 |
| `derived`、`models` | 派生通道及其输入映射、热应力模型参数 |
| `simulation` | 随机种子、生成间隔、控制端口与令牌变量名、`ledger_limit`、`validation_duration_seconds`、各点信号（基值、波幅、周期、噪声、相位）与场景 |
| `storage` | 开发预算、路径、批次预算 |
| `ui` | 测点示意坐标、中文标签、展示精度、过程量、历史窗口 |

旧版配置键（`profiles`、捕获类 `limits`、`stale_after` 等）**已不被接受**，加入会被校验拒绝。默认规模：8 路 Bridge 原始输入、200 个 OPC UA 点（96 对内外壁温度＋8 个过程量）、209 路派生量；原始输入与派生量分别计数，历史可选全部 417 个量。通道分配见[模拟通道分配](模拟通道分配.md)；模拟的温度配对不代表现场拓扑。

## 5. 场景

模拟器有 8 个场景，同一时刻只有一个生效：`baseline`、`thermal_step`、`load_cycle`、`extreme_temperature`、`bad_quality`、`freeze`、`disconnect_modbus`、`disconnect_opcua`。**网页没有场景切换入口**；切换用模拟控制接口（仅本机回环，令牌认证）：

```powershell
$token = (Get-Content .local\research\simulation-secret.txt).Trim()
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:14841/scenario `
  -Headers @{Authorization = "Bearer $token"} -ContentType "application/json" -Body '{"scenario":"bad_quality"}'
Invoke-RestMethod -Uri http://127.0.0.1:14841/status -Headers @{Authorization = "Bearer $token"}
```

- 切换在模拟器的**下一次生成**才生效，`POST /scenario` 立即返回的状态不证明协议端已应用（断连场景还要等服务器重配）。
- 非基线场景各持续配置里的 `duration` 秒，之后自动回到基线：`thermal_step`、`extreme_temperature` 30 秒，`load_cycle` 120 秒，`bad_quality`、`freeze`、两个断连各 20 秒。断连场景是停掉相应服务器（连接被拒），不是“沉默”；`bad_quality` 与 `freeze` 各作用于2个点；`freeze` 保留值与 OPC UA 源时间戳，在接收时间基准下，读回的恒定值**不能单独证明设备故障**（freeze 作为“信号断联”的新判据尚未确定）。
- 默认研究配置里 208 路信号都带噪声和振幅，**没有静止点**；要整源静止须用测量脚本的 quiet 配置，而不是场景。组合场景、请求超时、协议异常、缺号／Overflow、服务端修订参数等模拟器都产生不了（见[模拟场景覆盖表](HTSG_V1_模拟场景覆盖表_v0.1.md)）。

## 6. 页面

七页：系统总览、水冷壁应变场、热应力分析、历史查询、系统自检、通信·日志、系统管理·权限；只标标题，不标页序号。页面职责与限定以[终版设计第15节](HTSG_V1_终版设计_v1.0.md#15-前端页面)为准，本说明不重复。通信·日志页的日志是最近500条运行内存记录，重启后重新开始。

## 7. 验证命令

```powershell
.\.venv\Scripts\python.exe -m pytest -q                                  # 全部测试；需要开发库已启动
.\.venv\Scripts\python.exe scripts\measure.py --steady 120 --outage 25 --browser --out docs\evidence\d46
.\.venv\Scripts\python.exe scripts\smoke.py --out docs\evidence\d48       # 交付启动路径：启动、正常停止、同库重启
.\.venv\Scripts\python.exe scripts\verify_resilience.py all --out docs\evidence\d47
```

- 测量、烟测、抗故障脚本各自在**临时库**上运行并在结束时删除，不触碰现有的开发库和研究库。浏览器类用例和 `--browser` 选项需要 `.venv` 里的 Playwright 与本机 Chrome（`pip install -r requirements-validation.lock`）；运行网页本身不需要，也不访问 CDN。
- **命令行输出报告不等于通过：** `measure.py` 报告的 `noticed_after_seconds` 只表示“至少一个输入无效”，不是告警时延；`smoke.py` 的通过以 `tests/test_smoke.py` 的断言为准。
- 旧版的 `validate_ui.py`、`validate_research_cycle.py` 已不存在，原“Pre／恢复／Post 周期”流程不再适用。

## 8. 已知限制

- 保活：项目自建模拟服务器已局部适配，保活间隔等于协商计数×发布周期；未适配的 asyncua 服务器比协商值晚**两个**发布周期，**真实网关的保活行为必须现场核验**。
- 模拟器源账本只保留最近120次更新，用于核对协议解码与源时间戳，业务接口不直接注入这批数据。
- 热应力用独立算式核对，不能据此宣称真实机组模型精度已验证。
