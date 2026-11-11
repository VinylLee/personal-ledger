# 收支账本

把支付宝、微信、工行、建行的导出账单合并为一份本地 SQLite 账本，经过自动分类和人工复核后，提供月度/分类报表、交易明细查询、退款与报销核对、图表分析和 Tkinter 桌面界面。

本地优先、单机运行，不依赖账号体系。账本数据、统计和存储都在本地完成；只有桌面界面的汇率折算会在需要时访问公开汇率接口，且请求只包含币种代码，不发送任何账本数据。

> 本仓库只包含程序代码和文档，**不包含任何真实账单或账本数据**。

## 功能

- **多来源合并**：识别支付宝 CSV、微信 XLSX、工行 CSV、建行 XLS，按订单号或流水指纹去重。
- **自动分类**：支出、收入、退款、报销、代垫、账户划转、理财、未完成、待复核、不计收支。
- **跨渠道去重**：识别银行流水与平台支付是同一笔交易，支持人工确认或保留独立流水。
- **退款与报销**：自动匹配候选，支持部分回款按金额分摊，一对一关联校验。
- **统计口径**：净收支（net）、纯收支（pure）、原始收支（gross）、现金流水（cash），退款和报销可分别设定。
- **汇率折算**：收支报表和交易明细的汇总行可把非人民币金额按当前汇率折合为人民币，并显示全部折合合计；单笔明细、图表和命令行报表不折算。
- **桌面界面**：收支报表、交易明细、退款与报销复核、图表分析四个页签；交易明细支持 Ctrl/Shift 多选后批量修改。
- **可追溯**：每笔交易保留来源文件、行号和原始字段，可随时查看原始流水。

## 快速开始

需要 Python 3.12（推荐使用 Conda）和 `code/requirements.txt` 中的依赖。

```powershell
conda create -n personal-ledger --override-channels -c conda-forge python=3.12 pip
conda activate personal-ledger
python -m pip install -r code/requirements.txt

# 可选：按本人姓名识别账户划转（文件不会入库）
copy code\local_rules.example.py code\local_rules.py

# 把账单放入 交易记录/<批次>/，然后重建账本
python code/ledger.py build --start 2026-01-01 --end 2026-11-11
python code/ledger.py audit

# 打开桌面界面
python code/ledger_gui.py
```

默认输入目录为 `交易记录/26.11.11`，默认账本为 `output/收支账本.sqlite3`。用 `--input` 指定其他批次，`--db` 指定其他账本。

## 目录结构

```
code/                 程序代码
  ledger.py           建库、查询、审计
  sources.py          账单解析与去重
  accounting.py       分类、关联与匹配规则
  reports.py          统计汇总
  exchange.py         汇率获取、缓存与折算
  terminal.py         命令行表格
  charts.py           图表
  ledger_gui.py       桌面界面
  docs/               架构、开发与变更文档
交易记录/             原始账单（本地生成，不入库）
output/               账本数据库与汇率缓存（本地生成，不入库）
```

## 文档

- 用户使用说明：[code/README.md](code/README.md)
- 开发与 Agent 文档：[code/docs/README.md](code/docs/README.md)
- 架构说明：[code/docs/ARCHITECTURE.md](code/docs/ARCHITECTURE.md)

## 数据安全

`output/`、`交易记录/` 和 `code/local_rules.py` 已在 `.gitignore` 中排除。提交前请确认 `git status` 中没有这些路径，也不要把真实账本贴进 issue 或讨论区。

汇率缓存 `output/exchange_rates.json` 只包含公开汇率，不含账本数据；它同样在 `output/` 下，不会入库。
