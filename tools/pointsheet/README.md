# 点表工具（Excel ⇄ 部署配置）

部署时使用的离线工具：把现场点表从 Excel 转成后端的配置 JSON。后端运行时不引用它，也不需要它的依赖。

## 安装依赖

```powershell
.\.venv\Scripts\python.exe -m pip install -r tools\pointsheet\requirements.txt
```

离线环境先在有网的机器上下载到离线包目录：`pip download -r tools\pointsheet\requirements.txt -d D:\HTSG-dev\wheelhouse`，现场再用 `--no-index --find-links` 安装。

## 用法

```powershell
# 1. 由基础配置生成模板（现场地址留空待填）
.\.venv\Scripts\python.exe tools\pointsheet\pointsheet.py template --base config\research-sim.json --out 点表.xlsx

#    模拟现场演练时可保留基础配置里的地址，再把不接入的行的地址删掉：
.\.venv\Scripts\python.exe tools\pointsheet\pointsheet.py template --base config\research-sim.json --out 点表.xlsx --keep-addresses

# 2. 填好后生成配置（有错不生成，逐条指出 表名 第几行 哪一列）
.\.venv\Scripts\python.exe tools\pointsheet\pointsheet.py convert --base config\research-sim.json --sheet 点表.xlsx --out config\site.json

# 3. 用后端自己的规则再查一遍（convert 已做过同样的校验）
.\.venv\Scripts\python.exe -m htsg.cli check --config config\site.json
```

基础配置决定点表以外的一切（模式、时限、存储、模型、界面测站、模拟场景）：模拟现场演练用 `config\research-sim.json`；真实现场用填好存储路径的生产配置。

## 表格怎么填

- **数据源**：每个 Modbus 或 OPC UA 连接一行；“编号”要与点所在行的“数据源”一致。OPC UA 的用户名、口令只填环境变量名。
- **Modbus点 / OPC UA点**：每行一个输入点。
  - “内部编号”是系统内部的固定编号，派生量、界面测站、故障模板都按它引用。已有的行不要改编号。
  - **接入**：填“地址”（Modbus）或“NodeId”（OPC UA）。**不接入**：这一列留空即可，是正常情况，不报错。
  - “显示名称”可填现场位号或中文名，只用于页面显示；“含义”只是说明，不进配置。
  - 数值列留空即用默认值（比例1、偏移0、无阈值）；“无效值”可填多个，用分号分隔。
  - 可以在表尾加新行接入模板里没有的点（编号不重复）；新点没有派生量和测站。
- 规模上限仍按后端：Modbus 最多 8 点，OPC UA 最多 200 点。

## 转换时自动处理的事

- 依赖未接入点的派生量去掉（温差、热应力、应变、应变变化）；热应力峰值这类多输入派生量只用接入的输入，一个都不剩才去掉。
- 引用了未接入点的界面测站去掉；工况量、显示名称只保留接入的。
- 模拟现场配置里，模拟信号与场景只保留接入的点；表尾新加的点由模拟器给恒定值（极值中点，无极值时为0）。
- 没有接入点的数据源改为不启用。
- 去掉了什么都会逐条打印出来。

点的顺序按基础配置保持（页面列表按它排），新加的点排在最后。用 `--keep-addresses` 生成的模板不做任何修改就转换，得到的配置与基础配置完全相同（配置编号一致）。
