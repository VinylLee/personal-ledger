"""Read the four statement formats without changing the source files."""

import csv
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

PLATFORMS = {"alipay", "wechat"}
BANKS = {"icbc", "ccb"}
EMPTY = {"", "/", "-", "--", "—"}
DATE_PATTERN = re.compile(r"^\d{4}[-/]?\d{2}[-/]?\d{2}(?:[ T]\d{2}:\d{2}:\d{2})?$")


def json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def identifier(*parts):
    return hashlib.sha256(json_text(parts).encode("utf-8")).hexdigest()[:24]


def clean(value):
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    return str(value).strip()


def present(value):
    return "" if value in EMPTY else value


def minor(value):
    if value in EMPTY:
        return 0
    try:
        cents = Decimal(value.replace(",", "").replace("￥", "").replace("¥", "")) * 100
    except InvalidOperation as exc:
        raise ValueError(f"无法识别金额：{value!r}") from exc
    if not cents.is_finite() or cents != cents.to_integral_value():
        raise ValueError(f"金额无法精确到分：{value!r}")
    return int(cents)


def calendar_time(value):
    day, _, clock = value.replace("T", " ").partition(" ")
    day = day.replace("/", "-")
    if len(day) == 8:
        day = f"{day[:4]}-{day[4:6]}-{day[6:]}"
    day = date.fromisoformat(day).isoformat()
    return day, datetime.fromisoformat(f"{day}T{clock}").isoformat() if clock else None


def currency(value):
    mapping = {"人民币": "CNY", "人民币元": "CNY", "美元": "USD",
               "港币": "HKD", "港元": "HKD", "欧元": "EUR", "日元": "JPY"}
    if value in mapping:
        return mapping[value]
    if re.fullmatch(r"[A-Z]{3}", value):
        return value
    raise ValueError(f"未知币种：{value!r}")


def bank_direction(income, expense):
    if income and expense:
        raise ValueError("同一银行流水同时包含收入和支出")
    return ("in" if income else "out" if expense else "neutral"), abs(income or expense)


@dataclass
class Record:
    source_id: str
    line_no: int
    raw: dict
    source: str
    day: str
    occurred_at: str | None
    direction: str
    amount_minor: int
    currency: str
    source_type: str
    description: str = ""
    counterparty: str = ""
    status: str = ""
    payment: str = ""
    order_id: str = ""
    merchant_order_id: str = ""
    posted_day: str | None = None
    original_amount_minor: int | None = None
    original_currency: str | None = None
    account_hint: str = ""
    event_id: str | None = None
    role: str = "primary"
    match_rule: str = ""
    kind: str = ""
    category: str = ""
    rule: str = ""

    @property
    def memo(self):
        return " ".join(present(self.raw.get(key, "")) for key in (
            "备注", "摘要", "交易详情", "交易场所", "交易地点/附言",
            "商品", "商品说明",
        ))


@dataclass
class Statement:
    id: str
    path: str
    source: str
    sha256: str
    account_hint: str = ""
    owner: str = ""
    records: list[Record] = field(default_factory=list)


def normalize(statement, line, raw):
    source = statement.source
    time_col = "交易时间" if source in PLATFORMS else "交易日期"
    day, occurred_at = calendar_time(raw[time_col])
    base = dict(source_id=statement.id, line_no=line, raw=raw, source=source,
                day=day, occurred_at=occurred_at, account_hint=statement.account_hint)
    if source in PLATFORMS:
        wechat = source == "wechat"
        return Record(
            **base, direction={"收入": "in", "支出": "out", "不计收支": "neutral",
                               "/": "neutral"}[raw["收/支"]],
            amount_minor=abs(minor(raw["金额(元)" if wechat else "金额"])),
            currency="CNY", source_type=raw["交易类型" if wechat else "交易分类"],
            description=present(raw["商品" if wechat else "商品说明"]),
            counterparty=present(raw["交易对方"]),
            status=raw["当前状态" if wechat else "交易状态"],
            payment=present(raw["支付方式" if wechat else "收/付款方式"]),
            order_id=present(raw["交易单号" if wechat else "交易订单号"]),
            merchant_order_id=present(raw["商户单号" if wechat else "商家订单号"]),
        )
    if source == "icbc":
        direction, amount = bank_direction(minor(raw["记账金额(收入)"]), minor(raw["记账金额(支出)"]))
        original_currency = present(raw["交易币种"])
        original_amount = None
        if original_currency:
            _, original_amount = bank_direction(minor(raw["交易金额(收入)"]), minor(raw["交易金额(支出)"]))
        return Record(
            **base, direction=direction, amount_minor=amount, currency=currency(raw["记账币种"]),
            source_type=raw["摘要"], counterparty=present(raw["对方户名"]),
            description=" ".join(filter(None, (present(raw.get("交易详情", "")), present(raw["交易场所"])))),
            posted_day=calendar_time(raw["记账日期"])[0] if present(raw.get("记账日期", "")) else None,
            original_amount_minor=original_amount,
            original_currency=currency(original_currency) if original_currency else None,
        )
    amount = minor(raw["交易金额"])
    return Record(
        **base, direction="in" if amount > 0 else "out" if amount < 0 else "neutral",
        amount_minor=abs(amount), currency=currency(raw["币别"]), source_type=raw["摘要"],
        counterparty=present(raw["对方账号与户名"]), description=present(raw["交易地点/附言"]),
    )


def read_rows(path, source):
    if source in {"alipay", "icbc"}:
        encodings = ("utf-8-sig", "gb18030") if source == "alipay" else ("utf-8-sig",)
        for index, encoding in enumerate(encodings):
            try:
                with path.open(encoding=encoding, newline="") as stream:
                    reader = csv.reader(stream)
                    rows = []
                    while True:
                        line = reader.line_num + 1
                        row = next(reader, None)
                        if row is None:
                            break
                        rows.append((line, tuple(map(clean, row))))
                return rows
            except UnicodeDecodeError:
                if index == len(encodings) - 1:
                    raise
    if source == "wechat":
        from openpyxl import load_workbook

        with path.open("rb") as stream:
            book = load_workbook(stream, read_only=True, data_only=True)
            try:
                return [(i, tuple(map(clean, row))) for i, row in enumerate(book.worksheets[0].values, 1)]
            finally:
                book.close()
    if source == "ccb":
        import xlrd

        book = xlrd.open_workbook(file_contents=path.read_bytes())
        sheet = book.sheet_by_index(0)
        rows = []
        for i in range(sheet.nrows):
            row = [xlrd.xldate_as_datetime(c.value, book.datemode).date().isoformat()
                   if c.ctype == xlrd.XL_CELL_DATE else clean(c.value) for c in sheet.row(i)]
            if len(row) > 4 and re.fullmatch(r"\d{8}\.0", row[4]):
                row[4] = row[4][:-2]
            rows.append((i + 1, tuple(row)))
        return rows
    raise ValueError(f"不支持的账单：{path.name}")


def masked_account(rows):
    for _, row in rows:
        for cell in row:
            candidate = re.search(r"(?:\d{4,}|\*{2,})[\d*]{4,}(\d{4})", cell)
            if candidate:
                return "****" + candidate[1]
    return ""


def read_statement(path, root, source):
    relative = path.relative_to(root).as_posix()
    statement = Statement(identifier("file", relative), relative, source,
                          hashlib.sha256(path.read_bytes()).hexdigest())
    rows = read_rows(path, source)
    required = {"交易时间", "收/支"} if source in PLATFORMS else {"交易日期", "摘要"}
    header = next((i for i, (_, row) in enumerate(rows) if required <= set(row)), None)
    if header is None:
        raise ValueError(f"{relative} 找不到交易表头")
    columns = rows[header][1]
    time_col = columns.index("交易时间" if source in PLATFORMS else "交易日期")
    if source in BANKS:
        statement.account_hint = masked_account(rows[:header])
    for _, row in rows[:header]:
        for cell in row:
            name = re.search(r"(?:姓名|户名|账户名称|客户名称)\s*[:：]?\s*[【\[(（]?([\u4e00-\u9fff]{2,4})", cell)
            if name:
                statement.owner = name[1]
    for line, row in rows[header + 1:]:
        cell = row[time_col] if time_col < len(row) else ""
        if not DATE_PATTERN.fullmatch(cell):
            if any(row) and time_col == 0 and not (
                cell.startswith(("---", "合计", "共", "说明", "注", "温馨提示")) or cell == ""
                or cell.endswith("合计")
            ):
                raise ValueError(f"{relative} 第 {line} 行交易日期无法识别：{cell!r}")
            if source == "ccb" and row and re.fullmatch(r"\d+(?:\.0)?", row[0]):
                raise ValueError(f"{relative} 第 {line} 行交易日期无法识别：{cell!r}")
            continue
        raw = {col: row[i] if i < len(row) else "" for i, col in enumerate(columns) if col}
        try:
            statement.records.append(normalize(statement, line, raw))
        except (KeyError, ArithmeticError, ValueError) as exc:
            raise ValueError(f"{relative} 第 {line} 行：{exc}") from exc
    return statement


def load_statements(root: Path):
    statements = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name.startswith("~$"):
            continue
        name = path.name.lower()
        if name.startswith("支付宝交易明细") and name.endswith(".csv"):
            source = "alipay"
        elif name.startswith("微信支付账单") and name.endswith(".xlsx"):
            source = "wechat"
        elif name.startswith("hisdetail") and name.endswith(".csv"):
            source = "icbc"
        elif name.startswith("hqmx") and name.endswith(".xls"):
            source = "ccb"
        else:
            if name.endswith((".csv", ".xlsx", ".xls")):
                raise ValueError(f"无法识别输入账单格式：{path.relative_to(root)}")
            continue
        statements.append(read_statement(path, root, source))
    if not statements:
        raise ValueError(f"目录没有可读取的账单：{root}")
    return statements


def bank_fingerprint(record):
    return identifier({k: v for k, v in record.raw.items() if k != "序号"})


def select_events(statements, start, end):
    events = {}
    # The ordered detail is identical in some exports of the same bank ledger.
    equivalent_exports = {}
    account_aliases = {}
    for statement in statements:
        account = statement.account_hint or statement.path
        if statement.source in BANKS and len(statement.records) > 1:
            signature = (statement.source, tuple(map(bank_fingerprint, statement.records)))
            account = equivalent_exports.setdefault(signature, account)
        account_aliases[statement.id] = account
        occurrences = Counter()
        for record in statement.records:
            if not start <= record.day <= end:
                record.role = "outside_range"
                record.match_rule = "超出统计日期范围"
                continue
            if record.source in PLATFORMS and record.order_id:
                event_id = identifier(record.source, "order", record.order_id)
            else:
                fingerprint = bank_fingerprint(record)
                occurrences[fingerprint] += 1
                event_id = identifier(record.source, account, fingerprint, occurrences[fingerprint])
            record.event_id = event_id
            previous = events.get(event_id)
            if previous is not None:
                if (record.direction, record.amount_minor, record.currency, record.day) != (
                    previous.direction, previous.amount_minor, previous.currency, previous.day
                ):
                    raise ValueError(f"同一交易的金额、方向或日期冲突：{statement.path} 第 {record.line_no} 行")
                record.role = "duplicate_export"
                record.match_rule = "相同平台订单号" if record.order_id else "相同银行账本流水"
            else:
                events[event_id] = record
    aliases = {}
    for statement in statements:
        key = (statement.source, account_aliases[statement.id])
        aliases.setdefault(key, set()).add(statement.account_hint)
    return events, {s.id: aliases[s.source, account_aliases[s.id]] - {""} for s in statements}
