"""Compute reports from the original events and confirmed allocations."""

from calendar import monthrange
from collections import Counter, defaultdict
from dataclasses import dataclass

PENDING_DIRECTIONS = {"in": "income", "out": "expense"}


@dataclass(frozen=True)
class Entry:
    event_id: str
    day: str
    currency: str
    side: str
    category: str
    bucket: str
    amount: int


def decimal_text(amount):
    units, cents = divmod(abs(amount), 100)
    return f"{'-' if amount < 0 else ''}{units}.{cents:02d}"


def confirmed_adjustments(db):
    return db.execute("""
        SELECT l.first_id, l.second_id, l.relation, l.amount_minor, e.category
        FROM links l JOIN events e ON e.id = l.second_id
        WHERE l.status = 'confirmed' AND l.relation IN ('refund', 'reimbursement')
    """)


def linked_receipt_categories(db):
    return {link["first_id"]: link["category"] for link in confirmed_adjustments(db)}


def entries(db, actions):
    receipt_categories = {}
    offsets = Counter()
    for link in confirmed_adjustments(db):
        receipt_categories[link["first_id"]] = link["category"]
        if actions[link["relation"]] == "pure":
            offsets[link["second_id"]] += link["amount_minor"]

    for event in db.execute("SELECT * FROM events ORDER BY day, occurred_at, id"):
        event_id, day, currency = (event[k] for k in ("id", "day", "currency"))
        kind, amount = event["kind"], event["amount_minor"]
        category = receipt_categories.get(event_id, event["category"])
        if amount == 0:
            continue
        if kind == "pending":
            kind = PENDING_DIRECTIONS.get(event["direction"])
        prefix = (event_id, day, currency)
        if kind == "income":
            yield Entry(*prefix, "income", category, "income", amount)
        elif kind in {"expense", "advance"}:
            if kind == "advance" and actions["reimbursement"] == "pure":
                continue
            effective = amount - offsets[event_id]
            if effective > 0:
                yield Entry(*prefix, "expense", category, "expense", effective)
        elif kind in actions and actions[kind] == "cash":
            yield Entry(*prefix, "income", category, kind, amount)
        elif kind in actions and actions[kind] == "net":
            yield Entry(*prefix, "expense", category, kind, amount)


def summarize(db, by, start, end, currency, side, category, actions):
    groups = defaultdict(list)
    use_category = "category" in by
    for item in entries(db, actions):
        if (not start <= item.day <= end or currency and item.currency != currency
                or side and item.side != side or category and item.category != category):
            continue
        period = item.day if by.startswith("day") else item.day[:7] if by.startswith("month") else ""
        key = (period, item.currency, item.side if use_category else "",
               item.category if use_category else "")
        groups[key].append(item)

    totals = Counter()
    result = []
    for (period, currency, side, category), group in sorted(groups.items()):
        amounts = Counter()
        counts = defaultdict(set)
        for item in group:
            amounts[item.bucket] += item.amount
            counts[item.bucket].add(item.event_id)
            if item.side == "income":
                amounts["total_income"] += item.amount
            if item.side == "expense" and item.bucket != "expense":
                amounts["offsets"] += item.amount
        net_expense = amounts["expense"] - amounts["offsets"]
        line = dict(
            income=amounts["total_income"], ordinary_income=amounts["income"],
            expense=amounts["expense"], refunds=amounts["refund"],
            reimbursements=amounts["reimbursement"], net_expense=net_expense,
            balance=amounts["total_income"] - net_expense,
        )
        key = (period, currency, side)
        subtotal = line["income"] if side == "income" else line["net_expense"]
        totals[key] += subtotal
        row = {"period": period or None, "currency": currency}
        if by.startswith("month"):
            year, month = map(int, period.split("-"))
            row.update(coverage_start=max(start, period + "-01"),
                       coverage_end=min(end, f"{period}-{monthrange(year, month)[1]:02d}"))
        if use_category:
            row.update(side=side, category=category)
        row.update({name: decimal_text(amount) for name, amount in line.items()})
        row.update({f"{kind}_count": len(counts[kind])
                    for kind in ("income", "expense", "refund", "reimbursement")})
        result.append((row, key, subtotal))
    if use_category:
        for row, key, subtotal in result:
            denominator = totals[key]
            row["share_pct"] = round(100 * subtotal / denominator, 2) if denominator > 0 else None
    return [row for row, _, _ in result]
