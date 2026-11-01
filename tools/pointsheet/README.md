# 点表工具（Excel → 部署配置）

把现场点表从 Excel 转成后端的配置文件。后端运行时不引用它。现场使用的是**点表工具包**（`HTSG-POINTSHEET-…`，自带运行环境，双击即用），用法见包里的《点表工具使用说明》（源文件 `tools/pointsheet/点表工具使用说明.md`）。

## 命令行

点表工具包目录里（`runtime\python.exe` 是包自带的运行环境）：

```powershell
.\runtime\python.exe pointsheet.py template --base production.base.json --out 点表.xlsx
.\runtime\python.exe pointsheet.py convert  --base production.base.json --sheet 点表.xlsx --out production.json
```

`template` 生成空白点表；`convert` 读回填好的点表、生成配置，有错不生成，逐条指出“表名 第几行 哪一列”，通过的配置已按后端自己的规则校验（与 `htsg check` 相同）。双击入口“1-生成点表模板”“2-生成配置”调用的是 `launch.py`，它在这两条之外还会保护已填好的点表、给旧配置留底。

## 规则

- 点表只填现场事实：类别、内外壁温度的温度对号、显示名称、数据源、地址或 NodeId；单位、阈值、极值可不填。
- 内部编号、派生量（温差、热应力估算、换算应变、应变变化量、热应力峰值）、界面测站、标签与过程量列表都由工具生成；规则写在 `pointsheet.py` 开头。电桥不与温度关联。
- 留空的单位、阈值、极值按类别取默认值（`DEFAULTS`），填“无”去掉某个默认值；“其他”类没有默认值。
- 基础配置（`--base`，即 `config/production.base.json`）只提供点表以外的部分：项目、模型、时限、存储、界面精度。用研究配置作基础时，模拟器给每个点一个信号（基础配置里有的照用，没有的给恒定值）。
