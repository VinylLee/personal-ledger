# 架构说明

## 1. 总览

单机离线应用：Python 3.12 + SQLite + Tkinter。没有服务端、没有账号体系，账本文件保存在本机 `output/收支账本.sqlite3`。

```
交易记录/26.11.11/*.csv|xlsx|xls        output/收支账本.sqlite3
        │                                      ▲
        ▼                                      │ 原子替换
sources.load_statements ──► Statement/Record ──┤
        │                                      │
        ▼                                      │
sources.select_events ──► events{event_id: Record}
        │                                      │
        ▼                                      │
accounting 规则链（分类、去重、划转、退款/报销关联）── ledger.build
        │                                      │
        └──────────────┬───────────────────────┘
                       ▼
        只读查询：ledger.report / transactions / review / trace
                       │
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
   terminal.py     reports.py     ledger_gui.py (Tkinter)
   CLI 表格/CSV/JSON  统计汇总        桌面四页签
```

两条使用路径：

- **命令行**：`ledger.py build` 重建账本；`report`、`transactions`、`review`、`trace`、`audit` 只读查询。
- **桌面界面**：`ledger_gui.py` 只读查询 + 人工复核。任何修改（分类、关联、说明、备注等）都先写入 `decisions` JSON，再调用 `ledger.build` 整库重建。

## 2. 文件与职责

| 文件 | 职责 | 关键入口 |
|---|---|---|
| `sources.py` | 识别四种账单格式、解析原始行、生成稳定交易编号、去重同一导出的重复行 | `load_statements`、`select_events`、`minor` |
| `accounting.py` | 自动分类、交易对方规则、关闭订单恢复、跨渠道重复合并、账户划转识别、退款/报销候选匹配 | `classify`、`set_classification`、`merge_bank_records`、`link_adjustments` |
| `ledger.py` | 建库、表结构、复核规则读写、只读查询、完整性审计 | `build`、`report`、`transactions`、`review`、`trace`、`audit` |
| `reports.py` | 把 `events` 和已确认关联折算成统计条目，按维度汇总 | `entries`、`summarize`、`decimal_text` |
| `terminal.py` | 命令行的 rich 表格输出 | `display` |
| `charts.py` | matplotlib 饼图 + 柱状图 | `LedgerCharts.draw` |
| `ledger_gui.py` | Tkinter 界面、复核对话框、写操作入口 | `LedgerWindow`、`save_*`、`main` |
| `open_ledger.cmd` | Windows 双击启动 | — |

依赖：`openpyxl`（微信 xlsx）、`xlrd`（建行 xls）、`rich`（终端表格）、`matplotlib`（图表）。

**本地私有规则**：`accounting.py` 会尝试导入同目录的 `local_rules.py`，读取 `SELF_NAMES`（本人户名列表）。命中这些户名的流水归为「账户划转」。该文件含个人身份信息，已加入 `.gitignore`；仓库只提供 `local_rules.example.py` 模板。文件不存在时 `SELF_NAMES` 为空，规则不生效，其余功能不受影响。

## 3. 构建流程（`ledger.build`）

顺序不能随意调整，后面的步骤依赖前面的分类结果：

1. `load_statements(input)` 递归读取输入目录。按文件名识别来源：
   - `支付宝交易明细*.csv` → `alipay`
   - `微信支付账单*.xlsx` → `wechat`
   - `hisdetail*.csv` → `icbc`
   - `hqmx*.xls` → `ccb`
   识别不了但扩展名是 `.csv/.xlsx/.xls` 会直接报错，避免静默漏账。
2. `select_events(statements, start, end)` 生成 `events` 字典：
   - 平台流水用订单号生成编号；银行流水用除「序号」外的整行内容指纹 + 账户 + 同指纹出现次数生成编号。
   - 重复导出（同一订单号或同一银行流水）标记为 `duplicate_export`，不重复计入。
   - 区间外的行标记 `outside_range`，保留在 `source_rows` 但不进账本。
3. `read_decisions` 读取人工复核 JSON，并规范化分类名（`本人账户划转`/`账户资金划转` → `账户划转`）。
4. 对每笔交易执行 `set_classification`（即 `classify` + `apply_counterparty_rules`）。
5. `recover_closed_purchases`：交易关闭但有同商家订单号的全额成功退款时，恢复原支出。
6. `merge_bank_records`：把银行流水与平台流水判为同一笔。人工确认优先，其次唯一强匹配自动合并，其余生成 `duplicate` 候选并把银行流水改为 `pending / 跨渠道重复待确认`。
7. `identify_internal_transfers`：识别同日、同币种、同金额的银行入账与钱包提现。
8. `apply_classifications`：应用所有人工分类的 `kind/category`（规则里没有 `manual_override` 的，之后还会被交易对方规则修正）。
9. `apply_counterparty_rules`：对非人工覆盖的交易重新应用交易对方规则。
10. `link_adjustments`：应用人工退款/报销关联，再自动匹配退款和报销候选。人工关联会把回款类型改写为 `refund/reimbursement`，把待复核支出改写为 `expense`。
11. 应用 `manual_override` 规则：覆盖 `kind/category`，可覆盖 `description/counterparty`，并校验类型不与已确认关联冲突。
12. 在输出目录的临时子目录建库，`audit` 通过后 `Path.replace` 原子替换正式文件。任何一步失败都不会破坏原账本。

## 4. 数据库结构

`ledger.SCHEMA` 定义五张表：

| 表 | 说明 |
|---|---|
| `metadata` | 键值：`schema_version`、`start`、`end`、`input_root`、`decisions` |
| `sources` | 输入文件相对路径、SHA-256、来源类型、脱敏账户尾号 |
| `source_rows` | 每个原始数据行：文件、行号、`event_id`、角色、原始字段 JSON |
| `events` | 去重后的账本交易，是报表的唯一数据源 |
| `links` | 退款、报销、跨渠道重复、账户划转关系及分摊金额、证据 |

关键约束与不变量：

- 金额一律是整数分 `amount_minor >= 0`，方向存在 `direction`（`in/out/neutral`）。
- 每个 event 恰好有一行 `source_rows.role = 'primary'`；`validate_db` 会强制核对。
- `source_rows.role`：`primary`、`duplicate_export`、`duplicate_channel`、`outside_range`。
- 退款/报销关联用两个部分唯一索引保证「回款、支出各只能配对一次」。
- 已确认关联的分摊总额不得超过任一端金额（`validate_db` 校验）。
- `metadata.input_root` 是绝对路径。移动项目目录后，界面保存复核结果会找不到源账单。
- `links.relation`：`refund`、`reimbursement`、`duplicate`、`internal_transfer`；`status`：`candidate`、`confirmed`。

## 5. 交易编号 `event_id`

`sources.identifier(...)` 取 SHA-256 前 24 位。它出现在 `decisions`、`source_rows`、界面和命令行中，**重建后保持稳定**，因此适合做人工规则的键。

- 平台流水：`identifier(source, "order", order_id)`。
- 银行流水：`identifier(source, 账户, 原始行指纹, 同指纹第几次出现)`。

注意：银行账单内容一旦变化（补导、重导、行内容修改），编号可能变化，旧的人工规则会失效并导致重建报错。此时需要在复核页重新确认或清理规则。

## 6. `decisions` 复核规则

账本把当前全部人工规则序列化在 `metadata.decisions`。界面上的所有保存操作都是：读出这份 JSON → 修改 → 写入临时文件 → 调 `ledger.build(decisions=临时文件)`。

```json
{
  "classify": {
    "交易编号": {
      "kind": "expense",
      "category": "餐饮",
      "note": "复核备注",
      "description": "覆盖后的说明",
      "counterparty": "覆盖后的交易对方",
      "manual_override": true
    }
  },
  "duplicates": {
    "银行交易编号": "平台交易编号",
    "确认独立的银行交易编号": null
  },
  "links": [
    {"relation": "reimbursement", "receipt": "回款编号", "expense": "支出编号", "amount": "250.00"}
  ],
  "unassociated": ["确认无关联的交易编号"],
  "reimbursement_tolerance_pct": "5"
}
```

`classify` 各字段语义：

- `kind`：必须是 `accounting.KINDS` 之一（`expense/income/refund/reimbursement/advance/internal/investment/failed/pending/excluded`）。
- `category`：非空字符串，会做别名归一化。
- `note`：即界面的「备注」，在明细里显示为「复核备注」；`kind = excluded` 时必填。
- `description` / `counterparty`：只有带 `manual_override` 的规则才会覆盖。
- `manual_override`：`true` 时在规则链最后生效，且不再被交易对方规则改写。
- 未带 `manual_override` 的规则（如「确认交易性质」写入的）只覆盖 `kind/category/note`，之后仍会应用交易对方规则。

容易踩的坑：

1. **规则里的类型可能过期。** `link_adjustments` 会在人工关联后把回款的 `kind` 改为 `refund/reimbursement`，但 `classify` 里可能还留着旧的 `income/pending`。任何「只改部分字段」的保存都要以数据库中的有效 `kind/category` 为基准，不能直接沿用旧规则（`save_transaction_edits` 已按此处理）。
2. **`unassociated` 与 `links` 互斥。** 同一笔交易不能既标记无关联又出现在关联里；`link_adjustments` 会报错。
3. **确认交易性质时应合并旧规则，而不是整体替换。** 否则此前批量设置的说明、备注会被清掉（`save_pending_classifications` 已改为合并）。
4. 新增规则键时，必须同步 `ledger.read_decisions` 的白名单、`build` 的应用逻辑、界面的保存/导出函数和用户 README。

## 7. 统计口径

`reports.entries(db, actions)` 把每笔 event 折算成统计条目；`summarize` 再按日期/币种/方向/分类聚合。`actions` 是 `{"refund": 模式, "reimbursement": 模式}`。

| 模式 | 退款/报销回款如何处理 |
|---|---|
| `net`（默认） | 在到账日按原支出分类冲减支出；未分摊部分单列为回款分类 |
| `gross` | 保留原支出，排除回款流水 |
| `pure` | 在原支出日回溯扣除已确认分摊；`advance` 整体排除 |
| `cash` | 回款作为单独类型的流入，在 `ordinary_income` 中保留普通收入 |

其他行为：

- `pending` 流水按方向暂计为收入或支出，确认性质后按新类型统计。
- 汇总输出金额为十进制字符串（`decimal_text`），内部始终用整数分。
- 分组维度：`day`、`month`、`category`、`day-category`、`month-category`。
- 月报带 `coverage_start` / `coverage_end`，标明该月是否被账期截断。

## 8. 桌面界面结构

`LedgerWindow` 按页签组织，每页由「筛选工具栏 + 汇总标签 + 表格」构成：

| 页 | 创建方法 | 加载方法 | 主要写操作 |
|---|---|---|---|
| 收支报表 | `create_report` | `load_report` | 无 |
| 交易明细 | `create_transactions` | `load_transactions` | 编辑/批量编辑、确认无关联 |
| 退款与报销复核 | `create_review` | `load_review` | 确认关联、确认性质、重复合并、手动关联、阈值 |
| 图表分析 | `create_charts` | `load_charts` | 无 |

状态约定：

- `self.forms[page][key]`：筛选条件的 `StringVar`。
- `self.applied_forms[page]`：最近一次实际查询使用的条件，用于切回页签时还原。
- `self.page_versions[page]`：上次加载时的数据库版本（inode、大小、修改时间）。切页签时数据库变了就自动刷新。
- `Table.view_state()` / `restore_view()`：保存和恢复排序、滚动位置、多选行；行标识由 `Table.row_key` 生成（交易用 `event_id`）。
- 写操作统一走 `save_review_change(action)`，负责忙指针和错误弹窗。
- 所有写函数（`save_transaction_edit(s)`、`save_allocations`、`save_duplicate_review`、`save_pending_classifications`、`save_unassociated`）最终调用 `rebuild_with_decisions`，整库只重建一次。

对话框都继承 `ConfirmationDialog`（基于 `simpledialog.Dialog`），在 `body()` 建控件、`validate()` 校验并写 `self.result`，取消时 `result` 为空。

批量编辑交易（`BatchEditTransactionDialog` + `save_transaction_edits`）的约定：

- 每个字段独立勾选，未勾选字段保留各笔交易当前值。
- 勾选但留空表示清空；`kind/category` 不允许为空。
- 类型变更校验流水方向；与已确认退款/报销关联冲突时统一确认一次并解除关联。
- 未勾选类型/分类时，以数据库中的有效值写入规则，避免旧规则类型过期。

## 9. 扩展点

- **新增交易类型**：同步 `accounting.KINDS`、`ledger_gui.KINDS`、`terminal.KINDS`，检查 `reports.entries`、`ledger.audit` 的统计分支和方向校验。
- **新增账单来源**：在 `sources.load_statements` 增加文件名识别和 `normalize` 分支，补齐必需列校验。
- **新增复核规则键**：见第 6 节第 4 条。
- **新增界面页**：实现 `create_<page>` 与 `load_<page>`，在 `LedgerWindow.__init__` 注册；`refresh` 依赖命名约定，不要写错。
