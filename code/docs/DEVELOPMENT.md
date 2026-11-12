# 开发与验证指南

## 1. 环境

项目使用 Conda 环境 `personal-ledger`（Python 3.12）。环境名和解释器路径因机器而异，先确认：

```powershell
conda env list
conda activate personal-ledger
python -m pip install -r code/requirements.txt
```

从项目根目录（`code/` 的上一级）运行以下命令。若本机没有该环境，任意 Python 3.10+ 装齐 `requirements.txt` 也可，但图表和中文界面的字体效果可能不同。

可选：按本人姓名识别账户划转。复制模板并填写 `SELF_NAMES`：

```powershell
copy code\local_rules.example.py code\local_rules.py
```

`local_rules.py` 含真实姓名，已加入 `.gitignore`，不要提交；缺失时该规则自动禁用。

## 2. 常用命令

```powershell
# 重建账本（默认输入 交易记录/26.11.11，输出 output/收支账本.sqlite3）
python code/ledger.py build --start 2026-01-01 --end 2026-11-11

# 完整性审计：源明细逐条核对、各统计维度总额对账
python code/ledger.py audit

# 查询
python code/ledger.py report --by month
python code/ledger.py transactions --search "关键词"
python code/ledger.py review --type pending
python code/ledger.py trace <交易编号>

# 桌面界面
python code/ledger_gui.py
python code/ledger_gui.py --db 路径\其他账本.sqlite3
```

- `--db` 放在子命令之前。
- `build --decisions 路径` 可沿用导出的复核规则；`--reimbursement-tolerance 3` 覆盖报销差额阈值。
- 命令行 `--format csv|json` 便于给其他程序用；默认是中文表格。
- GUI 不传 `--db` 时打开 `output/last_database.json` 记录的上次账本；显式 `--db` 优先。命令行 `ledger.py` 的默认路径仍是 `output/收支账本.sqlite3`，重命名默认账本后命令行需显式 `--db`。

## 3. 测试策略

**现状：没有自动化测试套件。** 修改后至少做下面三类验证：

### 3.1 静态检查

```powershell
python -m py_compile code/ledger.py code/ledger_gui.py code/accounting.py code/sources.py code/reports.py code/charts.py code/terminal.py code/exchange.py
```

### 3.2 无界面功能测试（必须用账本副本）

界面逻辑和写操作都可以在没有窗口的情况下验证。**永远不要拿真实的 `output/收支账本.sqlite3` 做实验**，它是用户的真实账本。

推荐的脚本骨架（放在系统临时目录，不要放进仓库）：

```python
import json, shutil, sys, tempfile
from contextlib import closing
from pathlib import Path

ROOT = Path(r"<项目根目录>")
CODE = ROOT / "code"
sys.path.insert(0, str(CODE))
import ledger, ledger_gui

real = ROOT / "output" / "收支账本.sqlite3"
folder = Path(tempfile.mkdtemp(prefix="ledger-test-"))
db = folder / "ledger.sqlite3"
shutil.copy(real, db)

with closing(ledger.open_db(db)) as conn:
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM events WHERE kind='expense' ORDER BY day LIMIT 2")]

# 写操作：内部会整库重建，源账单必须还在 metadata.input_root 指向的位置
ledger_gui.save_transaction_edits(db, {ids[0]: {"description": "测试说明"}})

with closing(ledger.open_db(db)) as conn:
    assert conn.execute("SELECT description FROM events WHERE id=?", (ids[0],)).fetchone()[0] == "测试说明"
print("PASSED", folder)
```

要点：

- `ledger.open_db` 是只读 URI，写操作请用 `ledger_gui.save_*` 或 `sqlite3.connect`。
- `save_*` 会读取 `metadata.input_root` 重新解析源账单，所以副本必须能访问原始 `交易记录` 目录。
- 写操作失败时账本不会被破坏（临时库 + 原子替换）。
- `sqlite3.Row` 在连接关闭后仍可读取字段。

### 3.3 界面冒烟测试

不需要手动点界面就能发现 Tk 控件错误：

```python
import sys, tkinter as tk
from pathlib import Path
sys.path.insert(0, str(Path(r"<项目根目录>") / "code"))
import ledger_gui

root = tk.Tk()
root.withdraw()  # 不显示窗口
app = ledger_gui.LedgerWindow(root, Path(r"<项目根目录>") / "output" / "收支账本.sqlite3")
root.update_idletasks()
assert str(app.tables["transactions"].tree.cget("selectmode")) == "extended"
root.destroy()
```

对话框是模态的（构造时进入 `wait_window`）。无界面测试时先替换：

```python
from tkinter import simpledialog
simpledialog.Dialog.wait_window = lambda self, window: None
```

注意 `messagebox.showerror` 在验证失败时会真的弹窗，测试失败路径前把它替换为空函数。

Tk 控件状态用 `widget.state()` 或 `str(widget.cget("key"))` 判断；`cget` 可能返回 Tcl 对象，不要直接和字符串比较。

### 3.4 汇率折算测试

折算只影响界面汇总标签，测试时不要联网，也不要改真实账本：

- 用临时账本副本，在副本同目录预置 `exchange_rates.json`，例如 `rates: {"USD": "0.14"}`；`fetched_at` 用当前时间，`base` 必须是 `CNY`。
- 断言 `exchange.to_cny(Decimal("100"), "USD", book)` 等于 `100 / 0.14`（方向是除法，不是乘法）。
- 分别测试：新鲜缓存；过期缓存（先用旧值并显示「汇率缓存」）；无缓存且 `exchange.fetch_rates` 被替换为返回 `None`（显示「汇率不可用」，不抛异常）；只有 CNY 的视图不读写汇率。
- 无界面检查 `app.totals.get()` 和 `app.transaction_totals.get()` 是否包含 `≈ 人民币` 与 `全部折合人民币`，并确认表格行数、行内容和列没有变化。
- 测试结束删除临时目录；不要在生产账本目录生成假汇率文件。

### 3.5 改动后的回归清单

根据改动范围选择：

- 数据层：`build` 成功 + `audit` 通过（逐币种、逐维度对账）。
- 报表：抽查 `report --by month/category`，核对收入、支出、退款、报销、净支出、结余。
- 明细/复核：查询、排序、多选、刷新后选中与滚动位置是否保留。
- 明细隐藏报销：核对未关联回款、已确认关联的两端、部分报销和合计，取消勾选后恢复显示。
- 明细隐藏退款：核对全额与部分退款、未关联回款、跨月回溯和明细合计，取消勾选后恢复原金额。
- 写操作：验证 `decisions` 内容、重建后的 `events` 字段、失败时原数据不变。
- 界面：主窗口和涉及的对话框能否构造；按钮禁用状态是否符合当前页签。
- 汇率：两个汇总标签正确、表格不变；CNY-only 视图不联网；断网/无缓存时正常降级。

### 3.6 多账本与数据库管理测试

- 所有测试使用临时目录和账本副本；把 `ledger_gui.LAST_DATABASE` 指向临时 JSON，避免写入真实 `output/last_database.json`。
- **校验**：有效账本通过；文本文件、缺表、缺 metadata 键、损坏文件都抛 `ValueError`，且不修改文件。
- **自动命名**：用固定 `today` 调 `next_database_path`；预置同名文件验证 `-2`；覆盖非法字符、空名、Windows 保留名和长路径。
- **导入**：用真实批次目录（如 `交易记录/26.11.12kyh`，只读）向临时当前账本目录导入；断言新库文件存在、`metadata.input_root` 指向该目录、`decisions.classify` 为空、事件数 > 0、窗口切换到新库。导入失败（空目录）时当前窗口保持、无新文件。
- **切换/选择**：准备两个有效副本；`switch_database` 返回新实例、旧实例 `_closed=True`、菜单重建、标题/副标题/上次数据库更新；无效文件只弹错误，当前窗口不变。
- **重命名**：替换 `simpledialog.askstring`/`messagebox.askyesno`；验证成功改名、`retarget` 更新路径/标题/配置、筛选状态保留；同名文件、路径分隔符、Windows 保留名、空名都被拒绝。
- **轮询取消**：准备过期汇率缓存触发后台轮询，切换窗口后反复 `root.update()`，确认旧实例不再更新新窗口、无 Tk 异常。
- **真实数据安全**：测试前后比较 `output/收支账本.sqlite3` 的 SHA-256，确保未被修改。

## 4. 修改检查清单

### 4.1 改一处，记得查这几处

| 改动 | 需要同步的位置 |
|---|---|
| 交易类型（kind） | `accounting.KINDS`、`ledger_gui.KINDS`、`terminal.KINDS`、`reports.entries`、`ledger.audit` 的统计 SQL、方向校验 |
| 分类关键词 | 在忽略文件 `local_rules.py` 中配置；`accounting.py` 负责加载和匹配。 |
| `decisions` 新键 | `ledger.read_decisions` 白名单、`ledger.build` 应用逻辑、界面保存/导出、用户 README、本文档 |
| 数据库字段 | `ledger.SCHEMA`、`EVENT_FIELDS`、`sources.Record`、`build` 插入、查询 SELECT、`audit` |
| 新建界面页 | `create_<page>`、`load_<page>`、`LedgerWindow.__init__` 中的创建顺序；`refresh` 依赖命名约定 |
| 菜单/数据库操作 | `ledger.validate_database`、`ledger_gui.LAST_DATABASE`、`switch_database`、`import_statements`、`choose_database`、`rename_database`；切换窗口必须取消汇率轮询 |
| 上次数据库 | `output/last_database.json`、`read_last_database`、`write_last_database`、`resolve_startup_database`、`main --db` 语义 |
| 新写操作 | 校验函数、`save_*`、`rebuild_with_decisions`、界面刷新与状态提示、失败回滚 |
| 账单格式 | `sources.load_statements` 文件名识别、`normalize`、`read_rows`、必需列校验 |
| 汇率/汇总折算 | `exchange.py`、`ledger_gui.py` 的 `report_totals_text` / `transaction_totals_text`；不要改 `reports.py`、`terminal.py`、`charts.py` 或数据库 |
| 命令行参数 | `ledger.parser`、`main` 分派、用户 README |
| 用户可见行为 | `README.md`、`docs/ARCHITECTURE.md`、`docs/DEVELOPMENT.md`、`docs/CHANGELOG.md` |

### 4.2 写操作的三条硬规则

1. **规则合并不要整体替换。** 部分字段编辑应保留未修改字段。已关联回款在明细中显示的是原支出分类，保存时要保留回款自身分类，供解除关联后使用。
2. **校验放在保存前。** 界面先拦截方向冲突、空分类、不计收支缺备注、重复候选；`save_*` 再做一次后端校验。
3. **一次操作只重建一次。** 多笔修改先汇总成 `decisions`，最后调用一次 `rebuild_with_decisions`。

## 5. 常见坑

- **控制台中文乱码**：Windows 控制台是 GBK，而源码和输出是 UTF-8，`print` 看到乱码通常不是数据问题。用 `ascii()` 或直接查数据库确认。
- **真实账本被误改**：测试一律复制 `output/收支账本.sqlite3` 到临时目录，并把副本路径传给 `--db`/函数。
- **规则丢失风险**：`decisions` 只存在数据库里。做实验性重建前，先在复核页「导出复核规则」，或复制整个账本文件。
- **`metadata.input_root` 失效**：账本记录了源账单的绝对路径。项目移动、源目录改名后，所有写操作都会失败，需要 `build --input 新路径` 重建。
- **银行交易编号会变**：同一条银行流水只要原始行内容变了，`event_id` 就会变，旧人工规则会报「交易编号不存在」。这是设计使然，不要试图让编号对内容变化保持稳定。
- **`Table.selected()` vs `selected_rows()`**：前者返回当前焦点/首个选中行，适合「查看原始流水」；后者返回全部选中行，适合批量操作。
- **切页签自动刷新**：`tab_changed` 会比较数据库版本并重载。修改刷新逻辑时注意 `applied_forms`（查询条件）和 `view_state`（选中/滚动）的配合。
- **图表跳转**：用账本副本验证普通扇区、「其他」、日柱和部分月份。核对计入金额合计与图表一致，覆盖净收支的负数退款和纯收支的部分报销，并验证清除图表筛选后恢复普通查询。
- **模态对话框**：`simpledialog.Dialog.__init__` 会阻塞到窗口关闭；无界面测试要替换 `wait_window`。
- **字符宽度**：界面是中文，表格列宽以像素计；新增列先对照现有 `Column` 宽度。
- **`local_rules.py` 不入库**：真实姓名只写在这个本地文件里。不要把 `SELF_NAMES` 的具体值写回公开代码、README 或文档；测试时可以用虚构姓名临时验证。
- **关键字兜底只影响原本待分类的流出**：修改本机规则后，用账本副本重建并比对，确认已分类交易和手工复核不受影响。交易对方优先于说明，同一字段命中多个分类时保留原来的兜底分类。
- **汇率方向不能写反**：`rates["USD"]` 是「1 CNY 换多少 USD」，折算人民币要用除法 `金额 / rate`。全部用 `Decimal`，不要用 float；合计先累加未舍入值，最后统一格式化。
- **汇率只在两个汇总标签**：不要改 `reports.py`、`terminal.py`、`charts.py`、表格行或数据库。只有 CNY 的视图不要触发网络；缓存文件与账本同目录，默认在 `output/exchange_rates.json`，不要提交。
- **后台汇率刷新**：线程只做网络/文件 I/O，把结果放入队列；主线程用 `root.after` 轮询并更新 UI。不要在后台线程直接调用 `root.after` 或任何 Tk 方法（会报 `main thread is not in main loop`）；窗口关闭时用 `winfo_exists()` 保护。
- **导入新账单总是新建账本**：不要合并到当前库；测试用临时当前账本目录，真实账单目录只读，失败时不留下目标文件。
- **上次数据库配置**：`output/last_database.json` 不入库；测试必须把 `LAST_DATABASE` 指到临时文件，避免污染真实启动状态。
- **重命名默认账本**：GUI 会记住新名字，但命令行默认仍是 `output/收支账本.sqlite3`；命令行需显式 `--db`。
- **切换数据库要重建窗口**：先 `ledger.validate_database`，再 `close()` 取消 `_poll_after`、销毁 root 子控件和菜单，最后创建新 `LedgerWindow`；否则旧汇率轮询或旧控件会操作新窗口。重命名只需 `retarget`，不必重建。

## 6. 代码约定

- 沿用现有风格：4 空格缩进、单行约 110 字符、界面文案用中文、标识符用英文。
- 规则计算层（`accounting`、`reports`）尽量写纯函数或不可变数据结构；`sources.Record` 在构建期是可变的，这是既有设计。
- 账本金额只以整数分计算，展示时才用 `decimal_text`；汇率折算用 `Decimal`；不要用浮点数算钱。
- 不要静默吞异常。界面写操作由 `save_review_change` 统一弹窗；命令行由 `main` 捕获 `ValueError` 等并退出。
- 新增依赖前先确认是否必要；账单格式解析依赖 `xlrd`/`openpyxl`，不要引入 pandas。
- 不要加 `print` 调试语句到生产代码；需要诊断时用现有状态栏或临时脚本。
- 文档里的函数名、表名、参数名必须能在代码中搜到。

## 7. 文档维护

见 [README.md](README.md) 的「维护规则」。简而言之：**改行为就同步改文档，并在 CHANGELOG 追加一条**。本项目没有 git 历史，文档缺失意味着下一位开发者（或 Agent）只能靠猜。
