# 收支账本 · 项目须知

把支付宝、微信、工行、建行导出账单合并为 SQLite 账本（`output/收支账本.sqlite3`），提供命令行查询和 Tkinter 桌面界面。离线单机，Python 3.12，无自动化测试套件。

## 动手前先读

1. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)：数据流、模块职责、数据库结构、`decisions` 规则格式、统计口径。
2. [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)：环境、命令、无界面测试方法、修改检查清单、常见坑。
3. [docs/CHANGELOG.md](docs/CHANGELOG.md)：最近改了什么。

用户使用说明见 [README.md](README.md)。

## 文档维护（必须）

本项目不是 git 仓库，文档是唯一历史记录。**任何影响行为、数据结构、命令行参数、`decisions` 格式或界面操作的修改，都必须在同一次改动中更新 `docs/`**：

- 架构/数据流变化 → `docs/ARCHITECTURE.md`
- 命令、测试方法、易错点变化 → `docs/DEVELOPMENT.md`
- 每次改动 → 在 `docs/CHANGELOG.md` 顶部追加一条

## 环境与常用命令

```powershell
conda activate personal-ledger
python code/ledger.py build --start 2026-01-01 --end 2026-11-11
python code/ledger.py audit
python code/ledger_gui.py
```

## 硬性约束

- 账本包含真实个人财务数据。**测试一律复制账本到临时目录**，不要直接修改或重建 `output/收支账本.sqlite3`。
- 所有写操作都是「改 `decisions` → 整库重建」。多笔修改只重建一次。
- 金额只用整数分（`amount_minor`）计算，展示时才转十进制字符串。
- 部分字段编辑要合并已有规则，并以数据库中的有效 `kind/category` 为基准；不要整体替换 `classify` 条目。
- 修改后至少运行 `python -m py_compile`、`ledger.py audit`，并用账本副本做无界面回归。
