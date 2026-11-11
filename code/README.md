# 收支账本

账本使用 SQLite，按笔保留交易、原始字段、来源文件及行号、合并依据和退款或报销关联。可用 Python 自带的 `sqlite3` 查询。命令行的 `report`、`transactions` 和 `review` 默认显示中文表格，可用 `--format csv` 或 `--format json` 输出给其他程序。

开发与 Agent 协作文档见 [docs/](docs/README.md)：架构、数据格式、开发验证方法和变更记录。

## Conda 环境

以下命令从记账目录运行。现有环境名为 `personal-ledger`，已配置 Python 3.12。

```powershell
conda create -n personal-ledger --override-channels -c conda-forge python=3.12 pip
conda activate personal-ledger
python -m pip install -r code/requirements.txt
```

如需按本人姓名识别账户划转，再复制一份本地规则文件并填写姓名（该文件不会入库）：

```powershell
copy code\local_rules.example.py code\local_rules.py
```

## 桌面界面

Windows 下双击 `code/open_ledger.cmd` 打开现有账本，也可在 Conda 环境中运行 `python code/ledger_gui.py`。若需打开其他账本，指定 `--db 路径`。

切换标签页时会保留各页的输入、表格排序、选中项和滚动位置。账本内容更新后，返回标签页时会按上次查询条件刷新数据。

- **收支报表**可按月、日或分类分组。可按币种、方向、分类和日期筛选，并分别设定退款和报销的统计口径。
- **交易明细**支持按类型、金额、商户、说明、账本交易编号或原始订单号查找。选择交易后点击「编辑交易」，可修改类型、分类、交易对方、说明和备注。双击交易可查看源文件和行号，也可复制交易编号。
  按住 Ctrl 或 Shift 可多选交易，再点「编辑交易」进行批量修改。批量窗口里每个字段需先勾选才会统一设置，未勾选的字段保留各笔原值；勾选后留空会清空该字段（交易类型和分类不能清空）。批量修改类型时会核对所选流水的方向，方向不一致需分开处理。与已确认的退款或报销关联冲突时只需确认一次，保存后账本只重建一次。批量修改说明只会覆盖账本中的说明文字，原始流水仍可在「查看原始流水」中查看。
- **图表分析**按所选币种、日期、分类和统计口径展示分类饼图及按月或按日柱状图，可切换收入和支出。饼图展示正金额，负金额分类和合并的小分类会在图表上方注明。
- **退款与报销复核**并列展示回款和支出。选中候选可确认并输入分摊金额。查不到候选时，可复制两笔编号后用「手动关联」。退款和报销共用一对一规则，回款和支出各只能配对一次。部分回款按实际分摊金额确认，回款余额可在「未分摊」中查看。

确认关联后，整笔退款或报销在交易明细、报表和图表中都归入原支出的分类。修改原支出的分类会同步反映到回款，解除关联后回款使用自身分类。

收支报表、交易明细和图表分析的起止日期都可通过「日历」选择。选定年、月后点击所需日期，填写完范围再点击「查询」，也可以直接输入日期。
交易明细上方的合计按当前筛选结果和净收支口径计算，分别显示各币种的收入、支出、退款、报销、净支出和结余。

当结果包含非人民币币种时，收支报表的汇总和交易明细的合计会在每个外币金额后追加「（≈ 人民币 X）」，并增加一行「全部折合人民币」。汇率从公开接口获取，缓存在账本同目录的 `exchange_rates.json`（默认在 `output/`，不会入库），有效期 24 小时；缓存过期时先用旧汇率立即显示并在标签注明日期，后台再尝试更新，成功后会同时刷新已打开的收支报表和交易明细；没有缓存且无法联网时保留原金额并提示。单笔明细、图表和命令行报表不做折算。这是本项目唯一的网络访问，请求只包含币种代码，不发送账本数据。

复核列表中按住 Ctrl 点选多行，或按住 Shift 选择连续行，再点「确认所选关联」。批量窗口可逐组核对和修改分摊金额，「全部保存」会一次确认所选候选。确认后按当前筛选条件刷新候选列表，并保留排序和滚动位置。输入框右侧的 × 可清空当前文本。清空阈值输入框后，已保存的阈值继续生效，直到应用新的数值。

- **跨渠道重复**：选择「跨渠道重复 → 候选」，分别查看左右两侧流水，核对金额、商户和付款渠道。同一笔交易选择「确认同一笔交易」，银行和平台原始记录会归到同一笔账；确实是两笔独立交易则点「保留独立流水」。合并记录可在「已确认」中查看。
- **待复核交易**：选择流水后点「确认交易性质」，从交易类型和分类下拉框选择，也可输入自定义分类。选择「不计收支」时填写原因，备注会显示在交易明细和原始流水窗口。若划转两侧都在待复核列表，可一起标记。手动标记可在「账户划转 → 手动标记」查看，自动识别的划转配对在「已确认」查看。
- **确认无关联**：在退款、报销复核或交易明细中选中流水后点击按钮。成对的记录可选择回款、支出或两侧。已有确认关联会在再次确认后解除，原交易类型继续用于统计。标记结果可在「复核类型 → 已确认无关联」查看。

分类和本人账户识别规则保存在本机忽略文件 `local_rules.py`；可从 `local_rules.example.py` 创建配置。该文件不应提交到仓库。
「本人账户划转」和「账户资金划转」统一归入「账户划转」。

来源分类和说明无法确认的支出，可依据本机配置的关键词规则归类。交易对方优先于说明；同一字段命中多个分类时沿用原来的兜底结果。分类依据可在「查看原始流水」中核对。

保存关联时，程序会根据原始流水及已有复核规则重建账本，成功后应用新的分摊。若以后从命令行重建，先在复核页导出规则，再通过 `build --decisions 路径` 沿用。

## 生成和查询

```powershell
python code/ledger.py build --start 2026-01-01 --end 2026-11-11
python code/ledger.py audit
python code/ledger.py report --by month
python code/ledger.py report --by day --start 2026-11-01 --end 2026-11-11
python code/ledger.py report --by category --side expense --basis pure --currency CNY
python code/ledger.py report --by category --side income --basis pure
python code/ledger.py report --by month-category --format json
python code/ledger.py transactions --kind refund
python code/ledger.py transactions --search "关键词"
python code/ledger.py transactions --amount 250.00 --currency CNY
```

默认输入为 `交易记录/26.11.11`，输出为 `output/收支账本.sqlite3`。导入其他批次时给 `build` 指定 `--input`。需要指定账本时，将 `--db 路径` 放在子命令前。重建会重新读取源文件，核对通过后替换账本。报表按币种分组。月报会标出未覆盖整月的日期范围，CSV 和 JSON 中对应字段为 `coverage_start` 和 `coverage_end`。

### 统计口径

| 口径 | 支出 | 退款和报销回款 |
|---|---|---|
| `net`，默认 | 保留原支出 | 回款全额在到账日冲减支出。已确认关联时，整笔按原支出分类归集 |
| `gross` | 保留原支出金额 | 排除回款流水 |
| `pure` | 在原支出日扣除已确认的退款和报销金额。标记为 `advance` 的已知代垫支出整体排除 | 排除回款流水 |
| `cash` | 保留原支出 | 在到账日列作单独类型的流入，普通收入仍可在 `ordinary_income` 查看 |

可用 `--refunds` 或 `--reimbursements` 分别覆盖口径。例如 `report --by month --basis net --reimbursements pure` 会在退款到账日冲减支出，并在原支出日扣除已确认的报销金额。

报告包含收入、支出、退款、报销回款、净支出和结余，以及各自的笔数。分类占比按同币种、同期、同方向的金额计算。按类筛选时，占比在筛选结果内计算。已确认关联的部分回款会按原支出分类整笔归集，余额可在「未分摊」中查看。纯消费按账本中已确认的分摊金额回溯扣减。待复核流水的流入和流出分别暂计收入和支出，收入和支出筛选也会显示相应的待复核流水。确认后按更新的交易类型统计。

### 复核报销、退款与重复记录

```powershell
python code/ledger.py review --type pending
python code/ledger.py review --type reimbursement
python code/ledger.py review --type refund --status unlinked
python code/ledger.py review --type duplicate
python code/ledger.py trace <交易编号>
```

退款可跨月匹配到账本中较早的支出。银行流水缺少对方户名时，同一账户、同币种、同金额且交易说明相同的两笔可进入退款候选。

报销候选会在账本覆盖期内双向查找，允许先花钱、先收到款以及转账形式的代垫。默认按支出金额计算差额，差额不超过 5% 才能成为候选。建议分摊金额取回款和支出中较小的一项。已确认的关联优先，其余交易按匹配依据排序，回款和支出各最多出现在一组候选中。确认候选后才改变统计。候选里的 `first_id` 为回款，`second_id` 为支出，`amount` 为建议核对的金额。两端原金额的 `*_amount_minor` 字段以分为单位。

在桌面复核页填写「报销差额上限 (%)」并点击「应用阈值」，即可刷新并保存设置。命令行重建可用 `build --decisions 路径 --reimbursement-tolerance 3`，保留导出的复核规则并将阈值改为 3%。省略阈值参数时，沿用导出规则里的 `reimbursement_tolerance_pct`，缺省值为 5%。阈值为 0 时只查找等额候选。

将确认结果记入自建的 JSON 文件，后续重建时传入 `build --decisions 路径`。下例中的编号需替换为复核命令给出的真实编号。每笔回款和支出各只能出现在一组关联中，分摊金额以两笔交易金额的较小值为上限。

```json
{
  "classify": {
    "确认是个人收入的交易编号": {"kind": "income", "category": "劳务"},
    "确认是未来可报销的代垫交易编号": {"kind": "advance", "category": "代垫支出"}
  },
  "duplicates": {
    "确认重复的银行交易编号": "平台交易编号",
    "确认独立的银行交易编号": null
  },
  "links": [
    {"relation": "reimbursement", "receipt": "回款编号", "expense": "支出编号", "amount": "250.00"}
  ]
}
```

`classify` 可使用 `expense`、`income`、`refund`、`reimbursement`、`advance`、`internal`、`investment`、`failed`、`pending`、`excluded`。不计收支的 `excluded` 需通过 `note` 填写原因。`links` 支持 `refund` 和 `reimbursement`，用字符串提供金额。

## 数据结构

| 表 | 内容 |
|---|---|
| `events` | 去重后的交易。含原始方向、类别、交易状态、订单号、时间精度、金额和币种、分类依据 |
| `source_rows` | 所有导入的交易行、原始字段 JSON、主记录或重复标记、关联交易编号 |
| `sources` | 输入文件相对路径、文件 SHA-256、来源和脱敏账户提示 |
| `links` | 退款、报销、划转和疑似跨渠道重复关系、金额及匹配依据 |
| `metadata` | 版本、导入范围、输入目录和所用复核规则 |

库内的 `amount_minor` 为整数分，金额方向存于独立字段。银行导出中只有日期时，`occurred_at` 为空，`day` 保留该日期。报表 CSV 和 JSON 用十进制字符串显示金额。账本含原始交易字段和对手信息，适合保存在本机。
