"""Build and query a SQLite ledger from exported payment statements."""

import argparse
import csv
import json
import sqlite3
import sys
import tempfile
from collections import Counter
from contextlib import closing
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from accounting import (
    DEFAULT_REIMBURSEMENT_TOLERANCE, KINDS, apply_classifications, identify_internal_transfers, link_adjustments,
    apply_counterparty_rules, apply_local_category_rule, merge_bank_records, normalize_category,
    recover_closed_purchases, set_classification,
)
from reports import decimal_text, linked_receipt_categories, summarize
from sources import json_text, load_statements, minor, select_events

WORKSPACE = Path(__file__).resolve().parents[1]
INPUT = WORKSPACE / "交易记录" / "26.11.11"
DATABASE = WORKSPACE / "output" / "收支账本.sqlite3"
MODES = ("net", "gross", "pure", "cash")
REIMBURSEMENT_TOLERANCE_KEY = "reimbursement_tolerance_pct"
EVENT_FIELDS = (
    "source", "day", "occurred_at", "posted_day", "direction", "currency", "amount_minor",
    "original_currency", "original_amount_minor", "kind", "category", "counterparty",
    "description", "source_type", "status", "payment", "account_hint", "order_id",
    "merchant_order_id", "rule",
)

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE sources (
    id TEXT PRIMARY KEY, path TEXT UNIQUE NOT NULL, source TEXT NOT NULL,
    sha256 TEXT NOT NULL, account_hint TEXT NOT NULL
);
CREATE TABLE events (
    id TEXT PRIMARY KEY, source TEXT NOT NULL, day TEXT NOT NULL, occurred_at TEXT, posted_day TEXT,
    direction TEXT NOT NULL CHECK(direction IN ('in', 'out', 'neutral')),
    currency TEXT NOT NULL, amount_minor INTEGER NOT NULL CHECK(amount_minor >= 0),
    original_currency TEXT, original_amount_minor INTEGER,
    kind TEXT NOT NULL, category TEXT NOT NULL, counterparty TEXT NOT NULL, description TEXT NOT NULL,
    source_type TEXT NOT NULL, status TEXT NOT NULL, payment TEXT NOT NULL, account_hint TEXT NOT NULL,
    order_id TEXT NOT NULL, merchant_order_id TEXT NOT NULL, rule TEXT NOT NULL
);
CREATE TABLE source_rows (
    source_id TEXT NOT NULL REFERENCES sources(id), line_no INTEGER NOT NULL,
    event_id TEXT REFERENCES events(id), day TEXT NOT NULL, source_direction TEXT NOT NULL,
    currency TEXT NOT NULL, amount_minor INTEGER NOT NULL, role TEXT NOT NULL,
    match_rule TEXT NOT NULL, raw_json TEXT NOT NULL,
    PRIMARY KEY (source_id, line_no)
);
CREATE TABLE links (
    id INTEGER PRIMARY KEY,
    first_id TEXT NOT NULL REFERENCES events(id), second_id TEXT NOT NULL REFERENCES events(id),
    relation TEXT NOT NULL CHECK(relation IN ('refund', 'reimbursement', 'duplicate', 'internal_transfer')),
    status TEXT NOT NULL CHECK(status IN ('confirmed', 'candidate')),
    amount_minor INTEGER NOT NULL CHECK(amount_minor > 0), evidence TEXT NOT NULL
);
CREATE INDEX events_by_day ON events(day, kind, currency);
CREATE INDEX rows_by_event ON source_rows(event_id);
CREATE INDEX links_by_status ON links(status, relation, first_id);
CREATE UNIQUE INDEX adjustment_by_receipt ON links(first_id)
    WHERE relation IN ('refund', 'reimbursement');
CREATE UNIQUE INDEX adjustment_by_expense ON links(second_id)
    WHERE relation IN ('refund', 'reimbursement');
"""


def iso_date(text):
    return date.fromisoformat(text).isoformat()


def parse_reimbursement_tolerance(value):
    try:
        percent = Decimal(str(value).strip().removesuffix("%").removesuffix("％").strip())
    except InvalidOperation as exc:
        raise ValueError("报销差额上限须为大于或等于 0 的百分数。") from exc
    if not percent.is_finite() or percent < 0:
        raise ValueError("报销差额上限须为大于或等于 0 的百分数。")
    return percent


def configured_reimbursement_tolerance(decisions):
    return parse_reimbursement_tolerance(decisions.get(
        REIMBURSEMENT_TOLERANCE_KEY, DEFAULT_REIMBURSEMENT_TOLERANCE
    ))


def validate_db(db, expected_rows=None):
    if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or db.execute("PRAGMA foreign_key_check").fetchone():
        raise ValueError("数据库完整性检查失败")
    roles = dict(db.execute("SELECT role, COUNT(*) FROM source_rows GROUP BY role").fetchall())
    event_count = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    bad_rows = db.execute("""
        SELECT COUNT(*) FROM source_rows
        WHERE (role = 'outside_range') <> (event_id IS NULL)
    """).fetchone()[0]
    bad_events = db.execute("""
        SELECT COUNT(*) FROM events e WHERE 1 <> (
          SELECT COUNT(*) FROM source_rows r WHERE r.event_id = e.id AND r.role = 'primary'
        )
    """).fetchone()[0]
    if bad_rows or bad_events or event_count != roles.get("primary", 0):
        raise ValueError("源明细与账本交易未能逐条核对")
    if expected_rows is not None and sum(roles.values()) != expected_rows:
        raise ValueError("导入的原始明细数与预期不一致")
    for field in ("first_id", "second_id"):
        invalid = db.execute(f"""
            SELECT 1 FROM links l JOIN events e ON e.id = l.{field}
            WHERE l.status = 'confirmed'
            GROUP BY l.{field} HAVING SUM(l.amount_minor) > e.amount_minor
        """).fetchone()
        if invalid:
            raise ValueError("已确认的退款或报销分摊超额")
    return roles, event_count


def audit(db, expected_rows=None):
    roles, event_count = validate_db(db, expected_rows)
    bounds = dict(db.execute("SELECT key, value FROM metadata").fetchall())
    actions = {"refund": "net", "reimbursement": "net"}
    measure = ("income", "expense", "refunds", "reimbursements", "net_expense", "balance")
    expected = {}
    for row in db.execute("""
        SELECT currency,
               SUM(CASE WHEN kind = 'income' OR (kind = 'pending' AND direction = 'in')
                        THEN amount_minor ELSE 0 END) AS income,
               SUM(CASE WHEN kind IN ('expense', 'advance') OR (kind = 'pending' AND direction = 'out')
                        THEN amount_minor ELSE 0 END) AS expense,
               SUM(CASE WHEN kind = 'refund' THEN amount_minor ELSE 0 END) AS refunds,
               SUM(CASE WHEN kind = 'reimbursement' THEN amount_minor ELSE 0 END) AS reimbursements
        FROM events WHERE amount_minor > 0 AND (
            kind IN ('income', 'expense', 'advance', 'refund', 'reimbursement')
            OR (kind = 'pending' AND direction IN ('in', 'out'))
        )
        GROUP BY currency
    """):
        out = {key: row[key] for key in measure[:4]}
        out["net_expense"] = out["expense"] - out["refunds"] - out["reimbursements"]
        out["balance"] = out["income"] - out["net_expense"]
        expected[row["currency"]] = out
    for group_by in ("day", "month", "category", "month-category"):
        seen = {}
        for row in summarize(db, group_by, bounds["start"], bounds["end"], None, None, None, actions):
            bucket = seen.setdefault(row["currency"], Counter())
            bucket.update({field: minor(row[field]) for field in measure})
        if seen != expected:
            raise ValueError(f"{group_by} 汇总未通过逐币种对账")

    return {
        "start": bounds["start"], "end": bounds["end"], "source_rows": sum(roles.values()),
        REIMBURSEMENT_TOLERANCE_KEY: str(configured_reimbursement_tolerance(json.loads(bounds["decisions"]))),
        "source_roles": roles, "events": event_count,
        "event_kinds": dict(db.execute("SELECT kind, COUNT(*) FROM events GROUP BY kind").fetchall()),
        "links": [dict(row) for row in db.execute("""
            SELECT relation, status, COUNT(*) AS links, COUNT(DISTINCT first_id) AS first_events
            FROM links GROUP BY relation, status ORDER BY relation, status
        """)],
        "pending_by_currency_and_direction": [
            {**dict(row), "amount": decimal_text(row["amount_minor"])}
            for row in db.execute("""
                SELECT currency, direction, COUNT(*) AS events, SUM(amount_minor) AS amount_minor
                FROM events WHERE kind = 'pending' GROUP BY currency, direction
            """)
        ],
    }


def read_decisions(path):
    if path is None:
        return {}
    with path.open(encoding="utf-8-sig") as stream:
        decisions = json.load(stream)
    allowed = {"classify", "duplicates", "links", "unassociated", REIMBURSEMENT_TOLERANCE_KEY}
    if not isinstance(decisions, dict) or decisions.keys() - allowed:
        raise ValueError("复核 JSON 可包含 classify、duplicates、links、unassociated 和 reimbursement_tolerance_pct")
    for key, expected_type in (("classify", dict), ("duplicates", dict), ("links", list), ("unassociated", list)):
        if key in decisions and not isinstance(decisions[key], expected_type):
            raise ValueError(f"复核 JSON 的 {key} 类型错误")
    if any(not isinstance(event_id, str) or not event_id for event_id in decisions.get("unassociated", [])):
        raise ValueError("unassociated 应填写交易编号。")
    return decisions


def build(args):
    root = args.input.resolve()
    output = args.db.resolve()
    if not root.is_dir():
        raise ValueError(f"输入目录不存在：{root}")
    statements = load_statements(root)
    dates = [record.day for statement in statements for record in statement.records]
    if not dates:
        raise ValueError("账单没有交易明细")
    if output in {(root / s.path).resolve() for s in statements} or (
        args.decisions is not None and output == args.decisions.resolve()
    ):
        raise ValueError("输出路径与源文件或复核文件相同")
    start, end = args.start or min(dates), args.end or max(dates)
    if start > end:
        raise ValueError("开始日期晚于结束日期")
    events, aliases = select_events(statements, start, end)
    if not events:
        raise ValueError("统计区间内没有交易明细")
    decisions = read_decisions(args.decisions)
    for rule in decisions.get("classify", {}).values():
        if isinstance(rule.get("category"), str):
            rule["category"] = normalize_category(rule["category"].strip())
    requested_tolerance = getattr(args, "reimbursement_tolerance", None)
    if requested_tolerance is not None:
        decisions[REIMBURSEMENT_TOLERANCE_KEY] = str(requested_tolerance)
    tolerance = configured_reimbursement_tolerance(decisions)
    decisions[REIMBURSEMENT_TOLERANCE_KEY] = str(tolerance)
    for record in events.values():
        set_classification(record)
    recover_closed_purchases(events)
    links = merge_bank_records(events, statements, aliases, decisions.get("duplicates", {}))
    links.extend(identify_internal_transfers(events, statements))
    apply_classifications(events, decisions.get("classify", {}))
    classifications = decisions.get("classify", {})
    for event_id, record in events.items():
        if not classifications.get(event_id, {}).get("manual_override"):
            apply_counterparty_rules(record)
    links.extend(link_adjustments(events, decisions.get("links", []), tolerance,
                                  unassociated=decisions.get("unassociated", [])))
    for event_id, rule in classifications.items():
        if not rule.get("manual_override"):
            continue
        record = events[event_id]
        for link in links:
            if link.status != "confirmed" or link.relation not in {"refund", "reimbursement"}:
                continue
            if (link.first_id == event_id and rule["kind"] != link.relation
                    or link.second_id == event_id and rule["kind"] not in {"expense", "advance"}):
                raise ValueError(f"编辑类型与已确认关联冲突：{event_id}")
        record.kind, record.category, record.rule = rule["kind"], normalize_category(rule["category"]), "手动编辑"
        for field in ("description", "counterparty"):
            if field in rule:
                if not isinstance(rule[field], str):
                    raise ValueError(f"编辑的交易信息必须是文字：{event_id}")
                setattr(record, field, rule[field])

    for event_id, record in events.items():
        if event_id not in classifications:
            apply_local_category_rule(record)

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".ledger-", dir=output.parent) as folder:
        draft = Path(folder) / "ledger.sqlite3"
        with closing(sqlite3.connect(draft)) as db, db:
            db.row_factory = sqlite3.Row
            db.executescript(SCHEMA)
            db.executemany("INSERT INTO metadata VALUES (?, ?)", [
                ("schema_version", "1"), ("start", start), ("end", end),
                ("input_root", str(root)), ("decisions", json_text(decisions)),
            ])
            db.executemany("INSERT INTO sources VALUES (?, ?, ?, ?, ?)",
                           ((s.id, s.path, s.source, s.sha256, s.account_hint) for s in statements))
            fields = ", ".join(("id", *EVENT_FIELDS))
            params = ", ".join("?" for _ in range(1 + len(EVENT_FIELDS)))
            db.executemany(f"INSERT INTO events ({fields}) VALUES ({params})", (
                (event_id, *(getattr(r, key) for key in EVENT_FIELDS)) for event_id, r in events.items()
            ))
            db.executemany("INSERT INTO source_rows VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
                (r.source_id, r.line_no, r.event_id, r.day, r.direction, r.currency, r.amount_minor,
                 r.role, r.match_rule, json_text(r.raw))
                for s in statements for r in s.records
            ))
            db.executemany("""
                INSERT INTO links (first_id, second_id, relation, status, amount_minor, evidence)
                VALUES (?, ?, ?, ?, ?, ?)
            """, ((l.first_id, l.second_id, l.relation, l.status, l.amount_minor, l.evidence) for l in links))
            result = audit(db, len(dates))
        draft.replace(output)
    return {"database": str(output), "sources": len(statements), **result}


def open_db(path):
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def report(db, args):
    bounds = dict(db.execute("SELECT key, value FROM metadata").fetchall())
    start, end = args.start or bounds["start"], args.end or bounds["end"]
    if start > end:
        raise ValueError("开始日期晚于结束日期")
    start, end = max(start, bounds["start"]), min(end, bounds["end"])
    return summarize(db, args.by, start, end, args.currency, args.side, args.category,
                     {"refund": args.refunds or args.basis,
                      "reimbursement": args.reimbursements or args.basis})


def transactions(db, args, *, include_own_category=False):
    rows = db.execute("""
        SELECT * FROM events
        WHERE (? IS NULL OR kind = ? OR (kind = 'pending' AND direction = ?))
          AND (? IS NULL OR day >= ?) AND (? IS NULL OR day <= ?)
          AND (? IS NULL OR currency = ?)
          AND (? IS NULL OR amount_minor = ?)
        ORDER BY day, occurred_at, id
    """, (args.kind, args.kind, {"income": "in", "expense": "out"}.get(args.kind),
          args.start, args.start, args.end, args.end,
          args.currency, args.currency, args.amount, args.amount))
    decisions = json.loads(db.execute("SELECT value FROM metadata WHERE key='decisions'").fetchone()[0])
    classifications = decisions.get("classify", {})
    receipt_categories = linked_receipt_categories(db)
    search = args.search.casefold() if args.search else ""
    result = []
    for row in rows:
        event_id = row["id"]
        category = receipt_categories.get(event_id, row["category"])
        if args.category is not None and args.category != category:
            continue
        classification = classifications.get(event_id, {})
        note = classification.get("note", "")
        if search and not any(search in value.casefold() for value in (
            *(row[field] for field in ("id", "counterparty", "description", "source_type",
                                       "order_id", "merchant_order_id")), note,
        )):
            continue
        event = {**dict(row), "category": category, "amount": decimal_text(row["amount_minor"]),
                 "review_note": note}
        if include_own_category and event_id in receipt_categories:
            event["own_category"] = classification.get("category", row["category"])
        result.append(event)
    return result


def review(db, args):
    if args.type == "pending":
        rows = db.execute("""
            SELECT id, day, source, direction, currency, amount_minor, source_type,
                   counterparty, description, category, rule
            FROM events WHERE kind = 'pending' AND (? IS NULL OR id = ?) ORDER BY day, id
        """, (args.event, args.event))
    elif args.type == "unassociated":
        rows = db.execute("""
            SELECT e.* FROM events e
            WHERE (? IS NULL OR e.id = ?) AND EXISTS (
                SELECT 1 FROM metadata m, json_each(m.value, '$.unassociated') j
                WHERE m.key = 'decisions' AND j.value = e.id
            )
            ORDER BY e.day, e.id
        """, (args.event, args.event))
    elif args.type == "duplicate" and args.status == "confirmed":
        rows = db.execute("""
            SELECT 'duplicate' AS relation, 'confirmed' AS status, e.id AS first_id,
                   r.day AS first_day, s.source AS first_source,
                   COALESCE(json_extract(r.raw_json, '$."对方户名"'),
                            json_extract(r.raw_json, '$."对方账号与户名"')) AS first_party,
                   COALESCE(json_extract(r.raw_json, '$."交易详情"'),
                            json_extract(r.raw_json, '$."交易地点/附言"')) AS first_description,
                   r.amount_minor AS first_amount_minor, e.id AS second_id, e.day AS second_day,
                   e.source AS second_source, e.counterparty AS second_party,
                   e.description AS second_description, e.amount_minor AS second_amount_minor,
                   r.currency, r.amount_minor, r.match_rule AS evidence
            FROM source_rows r JOIN sources s ON s.id = r.source_id
            JOIN events e ON e.id = r.event_id
            WHERE r.role = 'duplicate_channel' AND (? IS NULL OR e.id = ?)
            ORDER BY r.day, s.path, r.line_no
        """, (args.event, args.event))
    elif args.status == "unlinked":
        rows = db.execute("""
            SELECT e.id, e.day, e.source, e.currency, e.counterparty, e.description,
                   COALESCE(spend.category, e.category) AS category,
                   e.amount_minor AS receipt_amount_minor,
                   e.amount_minor - COALESCE(SUM(l.amount_minor), 0) AS amount_minor
            FROM events e LEFT JOIN links l
              ON l.first_id = e.id AND l.status = 'confirmed' AND l.relation = e.kind
            LEFT JOIN events spend ON spend.id = l.second_id
            WHERE e.kind = ? AND (? IS NULL OR e.id = ?)
              AND NOT EXISTS (
                  SELECT 1 FROM metadata m, json_each(m.value, '$.unassociated') j
                  WHERE m.key = 'decisions' AND j.value = e.id
              )
            GROUP BY e.id HAVING e.amount_minor - COALESCE(SUM(l.amount_minor), 0) > 0
            ORDER BY e.day, e.id
        """, (args.type, args.event, args.event))
    else:
        rows = db.execute("""
            SELECT l.relation, l.status, l.first_id, a.day AS first_day, a.source AS first_source,
                   a.counterparty AS first_party, a.description AS first_description, a.amount_minor AS first_amount_minor,
                   l.second_id, b.day AS second_day, b.source AS second_source,
                   b.counterparty AS second_party, b.description AS second_description, b.amount_minor AS second_amount_minor,
                   a.currency, l.amount_minor, l.evidence
            FROM links l JOIN events a ON a.id = l.first_id JOIN events b ON b.id = l.second_id
            WHERE l.relation = ? AND l.status = ?
                  AND (? IS NULL OR ? IN (l.first_id, l.second_id))
            ORDER BY a.day, l.first_id, l.id
        """, (args.type, args.status, args.event, args.event))
    return [{**dict(row), "amount": decimal_text(row["amount_minor"])} for row in rows]


def trace(db, event_id):
    event = db.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if event is None:
        raise ValueError(f"找不到交易：{event_id}")
    event = dict(event)
    event["category"] = linked_receipt_categories(db).get(event_id, event["category"])
    rows = db.execute("""
        SELECT s.path, r.line_no, r.role, r.match_rule, r.raw_json
        FROM source_rows r JOIN sources s ON s.id = r.source_id WHERE r.event_id = ?
        ORDER BY r.role != 'primary', s.path, r.line_no
    """, (event_id,)).fetchall()
    return {
        "event": event,
        "review_note": json.loads(db.execute(
            "SELECT value FROM metadata WHERE key='decisions'").fetchone()[0]
        ).get("classify", {}).get(event_id, {}).get("note", ""),
        "unassociated": bool(db.execute("""
            SELECT 1 FROM metadata m, json_each(m.value, '$.unassociated') j
            WHERE m.key = 'decisions' AND j.value = ?
        """, (event_id,)).fetchone()),
        "sources": [{**{k: row[k] for k in row.keys() if k != "raw_json"},
                     "raw": json.loads(row["raw_json"])} for row in rows],
        "links": [dict(row) for row in db.execute(
            "SELECT * FROM links WHERE ? IN (first_id, second_id) ORDER BY id", (event_id,)
        )],
    }


def output_result(result, fmt="json"):
    if fmt == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif result:
        writer = csv.DictWriter(sys.stdout, fieldnames=list(result[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(result)


def parser():
    cli = argparse.ArgumentParser(description="整合并查询个人账本")
    cli.add_argument("--db", type=Path, default=DATABASE, help="SQLite 账本路径")
    commands = cli.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("build", help="读取原始流水并重建账本")
    ingest.add_argument("--input", type=Path, default=INPUT, help="原始流水目录")
    ingest.add_argument("--decisions", type=Path, help="人工复核规则 JSON")
    ingest.add_argument("--reimbursement-tolerance", type=parse_reimbursement_tolerance, metavar="百分数",
                        help="报销候选的差额上限，默认 5%%，可随复核规则保存")

    stats = commands.add_parser("report", help="按日、月或类型统计")
    stats.add_argument("--by", choices=("day", "month", "category", "day-category", "month-category"),
                       default="month")
    stats.add_argument("--basis", choices=MODES, default="net")
    stats.add_argument("--refunds", choices=MODES, help="覆盖退款统计口径")
    stats.add_argument("--reimbursements", choices=MODES, help="覆盖报销统计口径")
    stats.add_argument("--side", choices=("income", "expense"), help="只看收入或支出")
    details = commands.add_parser("transactions", help="查看或导出明细")
    details.add_argument("--kind", choices=sorted(KINDS))
    details.add_argument("--amount", type=minor, help="按原金额查询，如 250.00")
    details.add_argument("--search", help="按交易编号、商户、商品说明、摘要或订单号搜索")
    for command in (ingest, stats, details):
        command.add_argument("--start", type=iso_date)
        command.add_argument("--end", type=iso_date)
    for command in (stats, details):
        command.add_argument("--currency", type=str.upper)
        command.add_argument("--category")
    inspect = commands.add_parser("review", help="查看待复核交易或交易关联")
    inspect.add_argument("--type", choices=("pending", "reimbursement", "refund", "duplicate", "internal_transfer", "unassociated"),
                         default="pending")
    inspect.add_argument("--status", choices=("candidate", "confirmed", "unlinked"), default="candidate")
    inspect.add_argument("--event", help="筛选交易编号")
    for command in (stats, details, inspect):
        command.add_argument("--format", choices=("table", "csv", "json"), default="table",
                             help="输出格式，默认显示表格")
    provenance = commands.add_parser("trace", help="查看交易原始记录和关联")
    provenance.add_argument("id")
    commands.add_parser("audit", help="核对交易去向和各统计维度的总额")
    return cli


def main():
    args = parser().parse_args()
    if sys.stdout.encoding and not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        if args.command == "build":
            output_result(build(args))
        else:
            with closing(open_db(args.db)) as db:
                if args.command == "trace":
                    output_result(trace(db, args.id))
                elif args.command == "audit":
                    output_result(audit(db))
                else:
                    handler = {"report": report, "review": review, "transactions": transactions}[args.command]
                    result = handler(db, args)
                    if args.format == "table":
                        from terminal import display

                        display(result, args)
                    else:
                        output_result(result, args.format)
    except (ValueError, KeyError, TypeError, ArithmeticError, OSError, sqlite3.Error) as exc:
        sys.exit(f"账本处理失败：{exc}")


if __name__ == "__main__":
    main()
