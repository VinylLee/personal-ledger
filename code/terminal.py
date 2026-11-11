"""Readable terminal views for the ledger's existing query results."""

from calendar import monthrange
from collections import defaultdict
from decimal import Decimal

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from reports import PENDING_DIRECTIONS, decimal_text

CURRENCIES = {"CNY": "人民币", "USD": "美元", "HKD": "港元", "EUR": "欧元", "JPY": "日元"}
SOURCES = {"alipay": "支付宝", "wechat": "微信", "icbc": "工行", "ccb": "建行"}
DIRECTIONS = {"in": "流入", "out": "流出", "neutral": "中性"}
KINDS = {
    "expense": "支出", "income": "收入", "refund": "退款", "reimbursement": "报销",
    "advance": "代垫", "internal": "划转", "investment": "理财", "failed": "未完成",
    "pending": "待复核", "excluded": "不计收支",
}
MODES = {"net": "到账日冲减", "pure": "按原支出扣除", "gross": "排除回款", "cash": "列作流入"}
REPORT_TITLES = {
    "day": "每日收支", "month": "月度收支", "category": "收支分类",
    "day-category": "每日收支分类", "month-category": "月度收支分类",
}
LINK_TITLES = {
    "reimbursement": ("报销回款", "支出或代垫"),
    "refund": ("退款", "原支出"),
    "duplicate": ("银行记录", "平台记录"),
    "internal_transfer": ("银行入账", "钱包提现"),
}


def money(value):
    return f"{Decimal(value):,.2f}"


def source_label(source):
    return SOURCES.get(source, source)


def grid(console, headers, rows, *, numeric=(), footer=None, lines=False, id_columns=()):
    pad = console.width >= (100 if lines else 80)
    table = Table(box=box.ROUNDED, padding=(0, int(pad)),
                  header_style="bold cyan", border_style="cyan",
                  show_footer=footer is not None, footer_style="bold", show_lines=lines)
    for index, label in enumerate(headers):
        table.add_column(label, justify="right" if index in numeric else "left",
                         min_width=24 if index in id_columns and console.width >= 76 else None,
                         no_wrap=index in numeric,
                         overflow="fold", footer=footer[index] if footer else "")
    for row in rows:
        table.add_row(*(Text(str(value)) for value in row))
    console.print(table)


def section(console, title):
    console.print(Text(title, style="bold"))


def amount_columns(rows):
    return [(field, label) for field, label in (("refunds", "退款"), ("reimbursements", "报销"))
            if any(Decimal(row[field]) for row in rows)]


def coverage_notes(console, rows):
    for period, start, end in dict.fromkeys(
        (row["period"], row["coverage_start"], row["coverage_end"]) for row in rows
    ):
        year, month = map(int, period.split("-"))
        if start[-2:] != "01" or int(end[-2:]) != monthrange(year, month)[1]:
            console.print(Text(f"{period} 统计范围 {start} 至 {end}", style="dim"))


def totals_table(console, rows, args, period_header):
    columns = []
    if args.side != "expense":
        columns.append(("income", "收入"))
        if args.side == "income" and any(row["income"] != row["ordinary_income"] for row in rows):
            columns.append(("ordinary_income", "普通收入"))
    if args.side != "income":
        columns.append(("expense", "支出"))
    columns.extend(amount_columns(rows))
    if args.side != "income" and any(row["net_expense"] != row["expense"] for row in rows):
        columns.append(("net_expense", "净支出"))
    if args.side is None:
        columns.append(("balance", "结余"))
    headers = [period_header, *(label for _, label in columns)]
    body = [[row["period"], *(money(row[field]) for field, _ in columns)] for row in rows]
    footer = ["合计", *(money(sum(Decimal(row[field]) for row in rows)) for field, _ in columns)]
    grid(console, headers, body, numeric=range(1, len(headers)), footer=footer)


def category_table(console, rows, side, period_header):
    income = side == "income"
    adjusted = any(row["net_expense"] != row["expense"] for row in rows)
    columns = [("income", "收入")] if income else [("net_expense", "净支出" if adjusted else "支出")]
    columns.extend(amount_columns(rows))
    headers = ([period_header] if period_header else []) + ["分类", *(label for _, label in columns), "笔数", "占比"]
    body = []
    amount_field = columns[0][0]
    for row in sorted(rows, key=lambda item: (item["period"] or "", -Decimal(item[amount_field]), item["category"])):
        count = (row["income_count"] + row["refund_count"] + row["reimbursement_count"]
                 if income else row["expense_count"])
        share = f'{row["share_pct"]:.2f}%' if row["share_pct"] is not None else "—"
        prefix = [row["period"]] if period_header else []
        body.append([*prefix, row["category"], *(money(row[field]) for field, _ in columns), count, share])
    grid(console, headers, body, numeric=range(2 if period_header else 1, len(headers)))


def display_report(console, rows, args):
    section(console, REPORT_TITLES[args.by])
    refund = MODES[args.refunds or args.basis]
    reimbursement = MODES[args.reimbursements or args.basis]
    console.print(Text(f"退款{refund} · 报销{reimbursement}", style="dim"))
    period_header = "月份" if args.by.startswith("month") else "日期" if args.by.startswith("day") else ""
    currencies = defaultdict(list)
    for row in rows:
        currencies[row["currency"]].append(row)
    for currency, group in currencies.items():
        console.print()
        name = CURRENCIES.get(currency)
        section(console, f"{name} {currency}" if name else currency)
        if "category" in args.by:
            for side, label in (("income", "收入"), ("expense", "支出")):
                same_side = [row for row in group if row["side"] == side]
                if same_side:
                    section(console, label)
                    category_table(console, same_side, side, period_header)
        else:
            totals_table(console, group, args, period_header)
        if args.by.startswith("month"):
            coverage_notes(console, group)


def details(party, description):
    return "\n".join(dict.fromkeys(value for value in (party, description) if value)) or "—"


def display_records(console, rows, args):
    pending = args.command == "review" and args.type == "pending"
    unassociated = args.command == "review" and args.type == "unassociated"
    unlinked = args.command == "review" and args.status == "unlinked" and not pending and not unassociated
    section(console, "待复核交易" if pending else "已确认无关联" if unassociated
            else "含未分摊金额的回款" if unlinked else "交易明细")
    headers = ["日期与编号", "来源", "币种", "未分摊金额" if unlinked else "金额", "分类", "对方与说明"]
    body = []
    for row in rows:
        stamp = (row.get("occurred_at") or row["day"]).replace("T", " ")
        direction = DIRECTIONS.get(row.get("direction"), "")
        kind = KINDS.get(row.get("kind"), direction)
        if pending or row.get("kind") == "pending":
            kind = "待复核" + KINDS.get(PENDING_DIRECTIONS.get(row.get("direction")), "")
        label = "\n".join(dict.fromkeys(value for value in (kind, row["category"]) if value))
        note = details(row["counterparty"], row["description"] or row.get("source_type", ""))
        if pending:
            note += "\n" + row["rule"]
        body.append((f'{stamp}\n{row["id"]}', source_label(row["source"]), row["currency"],
                     money(row["amount"]), label, note))
    grid(console, headers, body, numeric=(3,), lines=True, id_columns=(0,))


def display_links(console, rows, args):
    status = "候选" if args.status == "candidate" else "已确认"
    title = {"reimbursement": "报销", "refund": "退款", "duplicate": "跨渠道重复",
             "internal_transfer": "账户划转"}[args.type]
    section(console, f"{title}关联 · {status}")
    body = []
    for row in rows:
        legs = []
        for key in ("first", "second"):
            amount = money(decimal_text(row[f"{key}_amount_minor"]))
            legs.append(f'{row[f"{key}_day"]}  {amount}\n'
                        f'{source_label(row[f"{key}_source"])} · {details(row[f"{key}_party"], row[f"{key}_description"])}\n'
                        f'{row[f"{key}_id"]}')
        body.append((*legs, f'{money(row["amount"])}\n{row["currency"]}', row["evidence"]))
    grid(console, [*LINK_TITLES[args.type], "关联金额", "匹配依据"], body, numeric=(2,),
         lines=True, id_columns=(0, 1))


def display(rows, args):
    console = Console()
    if not rows:
        console.print("没有符合条件的记录。")
    elif args.command == "report":
        display_report(console, rows, args)
    elif args.command == "review" and args.type not in {"pending", "unassociated"} and args.status != "unlinked":
        display_links(console, rows, args)
    else:
        display_records(console, rows, args)
