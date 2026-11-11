"""Classify transactions and connect evidence from separate statement rows."""

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sources import BANKS, PLATFORMS, minor

try:
    import local_rules
except ModuleNotFoundError as exc:
    if exc.name != "local_rules":
        raise
    local_rules = None

SELF_NAMES = getattr(local_rules, "SELF_NAMES", ())
SPEND_WORDS = getattr(local_rules, "SPEND_WORDS", {})
KEYWORD_ADDITIONS = getattr(local_rules, "KEYWORD_ADDITIONS", {})
CONDITIONAL_KEYWORDS = getattr(local_rules, "CONDITIONAL_KEYWORDS", ())
MEITUAN_NON_FOOD = getattr(local_rules, "MEITUAN_NON_FOOD", None)
INCOME_WORDS = getattr(local_rules, "INCOME_WORDS", {})
LAB_WATER = getattr(local_rules, "LAB_WATER", None)
LANDLORD_KEYWORD = getattr(local_rules, "LANDLORD_KEYWORD", "")
FOOD_CONTEXT_KEYWORD = getattr(local_rules, "FOOD_CONTEXT_KEYWORD", "")
KEYWORD_IGNORE_TERMS = getattr(local_rules, "KEYWORD_IGNORE_TERMS", ())
COUNTERPARTY_WATER_RULE = getattr(local_rules, "COUNTERPARTY_WATER_RULE", "交易对方规则")
LANDLORD_RULE = getattr(local_rules, "LANDLORD_RULE", "交易对方规则")
CATEGORY_FOR_RECORD = getattr(local_rules, "category_for_record", None)

DEFAULT_REIMBURSEMENT_TOLERANCE = Decimal("5")

CATEGORIES = {
    "交通出行": "交通", "爱车养车": "交通", "餐饮美食": "餐饮",
    "生活服务": "生活服务", "日用百货": "购物", "公益捐赠": "捐赠",
    "充值缴费": "生活缴费", "医疗健康": "医疗", "文化休闲": "文娱",
    "商业服务": "商业服务",
}
# Combine locally configured patterns for transactions not otherwise classified.
KEYWORD_PATTERNS = {}
for category in {**SPEND_WORDS, **KEYWORD_ADDITIONS}:
    parts = [pattern for pattern in (SPEND_WORDS.get(category), KEYWORD_ADDITIONS.get(category)) if pattern]
    KEYWORD_PATTERNS[category] = re.compile("|".join(parts), re.IGNORECASE)
REIMBURSE = re.compile(r"报销|报账|报帐|垫付(?:款)?(?:归还|返还|收回)|归还垫付|代垫款")
ADVANCE_HINT = re.compile(r"垫付|代付|代垫|代购|公款|报销")
TRANSFER = re.compile(r"转账|网转|汇款|跨行转出|跨行转入|群收款|二维码收款|银联入账")
SELF_NAME_PATTERNS = tuple(
    re.compile(rf"(?:[\d*Xx\s-]+/\s*)?{re.escape(name.strip())}(?:\s*[（(][^()（）]*[）)])?")
    for name in SELF_NAMES if isinstance(name, str) and name.strip()
)
KINDS = {"expense", "income", "refund", "reimbursement", "advance",
         "internal", "investment", "failed", "pending", "excluded"}
TRANSFER_CATEGORY_ALIASES = {"本人账户划转", "账户资金划转"}
CATEGORY_ALIASES = {"车辆": "交通", "电瓶车充电": "交通"}


def normalize_category(category):
    if category in TRANSFER_CATEGORY_ALIASES:
        return "账户划转"
    return CATEGORY_ALIASES.get(category, category)


def keyword_match(category, text, context):
    """Return the first acceptable keyword, applying local context exclusions."""
    for match in KEYWORD_PATTERNS[category].finditer(text):
        word = match.group(0)
        if category == "餐饮" and word == FOOD_CONTEXT_KEYWORD and MEITUAN_NON_FOOD and MEITUAN_NON_FOOD.search(context):
            continue
        return word
    return None


def keyword_category(record):
    """Classify an unconfirmed outflow from keywords in its counterparty, memo, description, or source type.

    Returns (category, matched text) when exactly one category matches a field.
    Counterparty is checked first because descriptions often contain station or
    mall names, and an ambiguous field is left unclassified instead of guessing.
    """
    party = (record.counterparty or "")
    other = " ".join(filter(None, (record.memo, record.description, record.source_type)))
    for term in KEYWORD_IGNORE_TERMS:
        party = party.replace(term, "")
        other = other.replace(term, "")
    context = f"{party} {other}"
    for required_party, required_terms, category, label in CONDITIONAL_KEYWORDS:
        if required_party in party and any(term in other for term in required_terms):
            return category, label
    for text in (party, other):
        if not text.strip():
            continue
        matches = {}
        for category in KEYWORD_PATTERNS:
            word = keyword_match(category, text, context)
            if word:
                matches[category] = word
        if matches:
            return next(iter(matches.items())) if len(matches) == 1 else None
    return None


def classify(record):
    kind = record.source_type
    status = record.status
    text = record.memo
    if re.search(r"失败|关闭|待支付|未支付|处理中|待到账", status):
        return "failed", "未完成交易", "原状态未完成"
    if re.search(r"投资理财|理财[产到赎申]|基金[赎上下申]|零钱通|活期利息|利息存入", kind) or kind == "利息":
        return "investment", "理财及利息", "来源类型为理财或利息"
    if (kind in {"退款", "退货", "消费退货"} or kind.endswith("-退款")
            or (record.source == "alipay" and status == "退款成功")):
        return "refund", "未关联退款", "来源为退款明细"
    if kind == "信用借还" and re.search(r"利息|手续费|服务费", text) and record.direction == "out":
        return "pending", "借还费用待拆分", "流水含还款费用，需确认本金和费用"
    if re.search(r"信用借还|还款|数字人民币兑|购汇|汇兑|经营账户提现|零钱提现|支付机构提现|ATM存款|现金支取|^卡存$|^充值$", kind):
        return "internal", "账户划转", "来源类型为还款、提现、存取款或换汇"
    if record.direction == "neutral":
        if record.amount_minor and record.source == "alipay":
            return "pending", "中性交易待判断", "平台标为不计收支，未识别资金用途"
        return "internal", "中性交易", "来源未记入收支"
    if record.direction == "in":
        for category, pattern in INCOME_WORDS.items():
            if re.search(pattern, text):
                return "income", category, "交易摘要的收入用途"
        if REIMBURSE.search(text):
            return "reimbursement", "报销回款", "交易摘要明确提到报销或归还垫付"
        if kind == "经营收款":
            return "income", "经营收入", "来源类型为经营收款"
        if "红包" in kind:
            return "income", "红包", "来源类型为红包"
        if record.source == "alipay" and kind == "收入" and not TRANSFER.search(text):
            return "income", "其他收入", "支付宝标为收入"
        if re.search(r"#[^#\s]{1,12}报$", kind):
            return "pending", "疑似报销或劳务", "银行摘要以「报」结尾，用途可能被截断"
        return "pending", "流入用途待判断", "可能为真实收入、报销、代收或账户转入"
    if ADVANCE_HINT.search(text) and not kind.startswith("商户"):
        return "pending", "疑似代垫", "摘要含代垫或报销字样，需核对"
    if kind in CATEGORIES:
        return "expense", CATEGORIES[kind], "支付宝交易分类"
    if "红包" in kind:
        return "expense", "红包", "来源类型为红包"
    if re.search(r"服务费|手续费", kind):
        return "expense", "手续费", "银行摘要为手续费或服务费"
    if kind in {"转账", "转账红包", "网转", "财付通转账", "跨行汇款", "跨行转出",
                "金融付款", "其他金融付款", "无卡支付", "银联代收"}:
        return "pending", "转出用途待判断", "可能为消费、代垫或账户划转"
    for category, pattern in SPEND_WORDS.items():
        if re.search(pattern, text, re.IGNORECASE):
            return "expense", category, "交易说明中的类别关键词"
    keyword = keyword_category(record)
    if keyword:
        category, word = keyword
        return "expense", category, f"关键字：{word}"
    if re.search(r"消费|缴费|群收款|扫二维码付款", kind):
        return "expense", "待分类", "交易类型可确认支出"
    return "pending", "流出用途待判断", "交易用途未知"


def set_classification(record):
    record.kind, record.category, record.rule = classify(record)
    apply_counterparty_rules(record)


def apply_local_category_rule(record):
    """Apply a local category label after the transaction type and links are settled."""
    if (CATEGORY_FOR_RECORD is None or record.kind in {"failed", "excluded", "investment"}
            or record.category == "跨渠道重复待确认"):
        return
    category = CATEGORY_FOR_RECORD(record)
    if category and (category := normalize_category(category)) != record.category:
        record.category = category
        record.rule = f"{record.rule}；本机分类规则"


def apply_counterparty_rules(record):
    if record.kind in {"failed", "excluded"} or record.category == "跨渠道重复待确认":
        return
    party = record.counterparty.strip()
    if any(pattern.fullmatch(party) for pattern in SELF_NAME_PATTERNS):
        record.kind, record.category, record.rule = "internal", "账户划转", "交易对方为本人"
        return
    if LAB_WATER and LAB_WATER.search(party):
        category, rule = "实验室水费", COUNTERPARTY_WATER_RULE
    elif record.direction == "out" and LANDLORD_KEYWORD and LANDLORD_KEYWORD in party:
        category, rule = "房租", LANDLORD_RULE
    else:
        return
    if record.direction == "out" and record.kind == "pending":
        record.kind = "expense"
    record.category, record.rule = category, rule


def recover_closed_purchases(events):
    refunds = defaultdict(list)
    purchases = defaultdict(list)
    for record in events.values():
        if record.source != "alipay" or not record.merchant_order_id:
            continue
        key = record.merchant_order_id, record.currency
        if record.kind == "refund":
            refunds[key].append(record)
        elif record.direction == "out":
            purchases[key].append(record)
    for key, originals in purchases.items():
        if len(originals) != 1:
            continue
        original = originals[0]
        paid_back = refunds[key]
        if (original.status == "交易关闭" and original.kind == "failed" and original.amount_minor > 0
                and all(record.day >= original.day for record in paid_back)
                and sum(record.amount_minor for record in paid_back) == original.amount_minor):
            original.kind = "expense"
            original.category = CATEGORIES.get(original.source_type, "待分类")
            original.rule = "关闭订单有同商家订单号的成功全额退款，保留原支出与退款两条流水"


def flow(record):
    return "in" if record.kind in {"refund", "reimbursement"} else record.direction


def merchant(value):
    return re.sub(r"[\W_]", "", value.casefold())


def day_gap(first, second):
    return (date.fromisoformat(second.day) - date.fromisoformat(first.day)).days


def bank_channel(record):
    value = f"{record.memo} {record.counterparty}".lower()
    if re.search(r"支付宝|alipay", value):
        return "alipay"
    if re.search(r"微信|财付通|wechat|tenpay", value):
        return "wechat"
    return ""


def bank_used(record):
    if "工商银行" in record.payment:
        return "icbc"
    if "建设银行" in record.payment:
        return "ccb"
    return ""


@dataclass(frozen=True)
class Link:
    first_id: str
    second_id: str
    relation: str
    status: str
    amount_minor: int
    evidence: str


def merge_bank_records(events, statements, aliases, decisions):
    platform = defaultdict(list)
    for record in events.values():
        if record.source in PLATFORMS and record.kind not in {"failed", "investment"} and flow(record) != "neutral":
            platform[record.currency, flow(record), record.amount_minor].append(record)

    banks = [r for r in events.values() if r.source in BANKS]
    bank_ids = {r.event_id for r in banks}
    merges = {}
    suggestions = {}
    exact = {}
    for bank in banks:
        if bank.event_id in decisions:
            target = decisions[bank.event_id]
            if target is not None:
                keep = events.get(target)
                if keep is None or keep.source not in PLATFORMS or keep.currency != bank.currency or flow(keep) != flow(bank):
                    raise ValueError(f"人工重复记录的方向、币种或交易编号无效：{bank.event_id}")
                merges[bank.event_id] = (target, "人工确认同笔交易")
            continue
        hint = bank_channel(bank)
        if not hint or bank.kind in {"failed", "investment", "internal"}:
            continue
        matches = []
        for txn in platform[bank.currency, flow(bank), bank.amount_minor]:
            if txn.source != hint or bank_used(txn) not in {"", bank.source}:
                continue
            if (txn.kind == "refund") != (bank.kind == "refund"):
                continue
            gap = min(abs(day_gap(bank, txn)), abs((date.fromisoformat(bank.posted_day)
                      - date.fromisoformat(txn.day)).days) if bank.posted_day else 999)
            if gap > 3:
                continue
            tails = set(re.findall(r"(?<!\d)\d{4}(?!\d)", txn.payment))
            bank_tails = {a[-4:] for a in aliases[bank.source_id]}
            if tails and bank_tails and not tails & bank_tails:
                continue
            merchant_name = merchant(txn.counterparty)
            merchant_match = (len(merchant_name) >= 3
                              and merchant_name in merchant(bank.memo + bank.counterparty))
            if gap == 0 and (bank_used(txn) == bank.source or merchant_match):
                matches.append((txn.event_id, "银行渠道、日期、方向、币种和金额一致，付款银行或商户一致", True))
            elif bank_used(txn) or merchant_match or gap == 0:
                matches.append((txn.event_id, f"银行摘要指向支付平台，金额及方向一致，日期相差 {gap} 天", False))
        if matches:
            suggestions[bank.event_id] = matches
            if len(matches) == 1 and matches[0][2]:
                exact[bank.event_id] = matches[0]
    if decisions.keys() - bank_ids:
        raise ValueError("人工重复设置中有不存在的银行交易编号")
    # A platform payment can appear in only one bank event after export deduplication.
    target_count = Counter(target for matches in suggestions.values() for target, _, _ in matches)
    target_count.update(target for target, _ in merges.values())
    if any(n > 1 for n in Counter(target for target, _ in merges.values()).values()):
        raise ValueError("同一平台交易关联了多笔人工银行重复记录")
    for bank_id, (target, reason, _) in exact.items():
        if target_count[target] == 1:
            merges[bank_id] = (target, reason)

    links = []
    merged_targets = {target for target, _ in merges.values()}
    for bank_id, matches in suggestions.items():
        if bank_id in merges:
            continue
        remaining = [item for item in matches if item[0] not in merged_targets]
        if not remaining:
            continue
        bank = events[bank_id]
        bank.kind, bank.category, bank.rule = "pending", "跨渠道重复待确认", "疑似与平台交易重复，待核对"
        for target, reason, _ in remaining:
            links.append(Link(bank_id, target, "duplicate", "candidate", bank.amount_minor, reason))
    for statement in statements:
        for record in statement.records:
            if record.event_id not in merges:
                continue
            target, reason = merges[record.event_id]
            record.event_id = target
            if record.role == "primary":
                record.role, record.match_rule = "duplicate_channel", reason
    for duplicate in merges:
        del events[duplicate]
    return links


def identify_internal_transfers(events, statements):
    owners = {s.owner for s in statements if s.owner}
    bank_receipts = defaultdict(list)
    withdrawals = defaultdict(list)
    for record in events.values():
        if record.source in BANKS and record.kind in {"pending", "internal"}:
            own_party = re.sub(r"[\d*()（）/\s-]", "", record.counterparty)
            if (TRANSFER.search(record.source_type) and own_party in owners
                    and record.category != "跨渠道重复待确认"):
                record.kind, record.category, record.rule = (
                    "internal", "账户划转", "转账对手户名与账单持有人一致",
                )
            channel = bank_channel(record)
            if (record.direction == "in" and record.source_type in {"银联入账", "支付机构提现", "财付通转账"}
                    and channel and record.category != "跨渠道重复待确认"):
                bank_receipts[channel, record.day, record.currency, record.amount_minor].append(record)
        elif (record.source in PLATFORMS and record.kind == "internal"
              and record.source_type in {"经营账户提现", "零钱提现"}):
            withdrawals[record.source, record.day, record.currency, record.amount_minor].append(record)
    links = []
    for key, legs in bank_receipts.items():
        related = withdrawals[key]
        if len(legs) == len(related) == 1:
            bank, wallet = legs[0], related[0]
            bank.kind, bank.category, bank.rule = (
                "internal", "钱包提现", "同日、同币种金额的银行入账和钱包提现一对一对应",
            )
            links.append(Link(bank.event_id, wallet.event_id, "internal_transfer", "confirmed",
                              bank.amount_minor, bank.rule))
    return links


def apply_classifications(events, classifications):
    for event_id, rule in classifications.items():
        record = events.get(event_id)
        if record is None:
            raise ValueError(f"人工分类的交易编号不存在：{event_id}")
        kind, category = rule["kind"], rule["category"]
        if kind not in KINDS or not isinstance(category, str) or not category.strip():
            raise ValueError(f"人工分类的类型或类目无效：{event_id}")
        note = rule.get("note", "")
        if not isinstance(note, str):
            raise ValueError(f"交易备注应为文字：{event_id}")
        if kind == "excluded" and not note.strip():
            raise ValueError(f"不计收支的交易需要填写备注：{event_id}")
        if (kind in {"expense", "advance"} and record.direction != "out"
                or kind in {"income", "reimbursement", "refund"} and record.direction == "out"):
            raise ValueError(f"人工分类与交易方向冲突：{event_id}")
        record.kind, record.category, record.rule = kind, normalize_category(category.strip()), "人工分类"


def link_adjustments(events, manual, reimbursement_tolerance=DEFAULT_REIMBURSEMENT_TOLERANCE,
                     *, unassociated=()):
    links = []
    excluded = set(unassociated)
    matched_receipts, matched_expenses = excluded.copy(), excluded.copy()

    def add(receipt, spend, relation, amount, evidence):
        if receipt.event_id in matched_receipts or spend.event_id in matched_expenses:
            raise ValueError(f"退款和报销须一对一关联，回款或支出已配对：{receipt.event_id} / {spend.event_id}")
        if (amount <= 0 or receipt.currency != spend.currency or spend.direction != "out"
                or receipt.direction == "out" or receipt is spend
                or receipt.amount_minor < amount or spend.amount_minor < amount):
            raise ValueError(f"退款/报销分摊超额，或币种、方向冲突：{receipt.event_id} / {spend.event_id}")
        matched_receipts.add(receipt.event_id)
        matched_expenses.add(spend.event_id)
        links.append(Link(receipt.event_id, spend.event_id, relation, "confirmed", amount, evidence))

    # Older rules could record several allocations between the same two transactions.
    manual_amounts = Counter()
    for item in manual:
        amount = minor(str(item["amount"]))
        if amount <= 0:
            raise ValueError("退款/报销分摊金额必须大于零")
        manual_amounts[item["receipt"], item["expense"], item["relation"]] += amount
    for (receipt_id, expense_id, relation), amount in manual_amounts.items():
        receipt, spend = events.get(receipt_id), events.get(expense_id)
        if receipt is None or spend is None or relation not in {"refund", "reimbursement"}:
            raise ValueError("人工关联含无效交易编号或类型")
        if receipt_id in excluded or expense_id in excluded:
            raise ValueError("同一交易同时设置了关联和无关联，请核对复核规则。")
        if receipt.kind not in {relation, "income", "pending"} or spend.kind not in {"expense", "advance", "pending"}:
            raise ValueError("人工关联包含非支出或非回款流水")
        add(receipt, spend, relation, amount, "人工确认")
        receipt.kind = relation
        receipt.category = "报销回款" if relation == "reimbursement" else "未关联退款"
        receipt.rule = "人工确认回款性质"
        apply_counterparty_rules(receipt)
        if spend.kind == "pending":
            spend.kind = "expense"
            spend.category = "代垫支出" if relation == "reimbursement" else "待分类"
            spend.rule = "人工确认原支出"

    spend_events = [r for r in events.values() if r.kind in {"expense", "advance"}]
    expense_by_order = defaultdict(list)
    for spend in spend_events:
        if spend.merchant_order_id:
            expense_by_order[spend.source, spend.merchant_order_id].append(spend)
    refunds = sorted((r for r in events.values() if r.kind == "refund"), key=lambda r: (r.day, r.event_id))
    refunds_with_known_order = set()
    order_matches = []
    for receipt in refunds:
        if receipt.event_id in matched_receipts or receipt.amount_minor <= 0:
            continue
        by_order = expense_by_order[receipt.source, receipt.merchant_order_id]
        if len(by_order) == 1:
            refunds_with_known_order.add(receipt.event_id)
            spend = by_order[0]
            gap = day_gap(spend, receipt)
            difference = spend.amount_minor - receipt.amount_minor
            if gap >= 0 and receipt.currency == spend.currency and difference >= 0:
                order_matches.append(((difference, gap, receipt.event_id), receipt, spend))
    for _, receipt, spend in sorted(order_matches, key=lambda item: item[0]):
        if spend.event_id not in matched_expenses:
            add(receipt, spend, "refund", receipt.amount_minor, "同平台的商家订单号一致")

    refund_matches = []
    for receipt in refunds:
        available = receipt.amount_minor
        if available <= 0 or receipt.event_id in matched_receipts or receipt.event_id in refunds_with_known_order:
            continue
        matches = []
        ref_name = merchant(receipt.counterparty)
        type_name = merchant(receipt.source_type.removesuffix("-退款"))
        for spend in spend_events:
            if spend.event_id in matched_expenses or receipt.source != spend.source or receipt.currency != spend.currency:
                continue
            if receipt.merchant_order_id and spend.merchant_order_id and receipt.merchant_order_id != spend.merchant_order_id:
                continue
            if day_gap(spend, receipt) < 0 or spend.amount_minor < available:
                continue
            name = merchant(spend.counterparty)
            same_description = bool(spend.description and spend.description == receipt.description)
            bank_description_match = (
                receipt.source in BANKS and receipt.account_hint
                and receipt.account_hint == spend.account_hint and not (ref_name and name)
                and same_description and spend.amount_minor == available
            )
            if not bank_description_match and (len(name) < 2 or name not in {ref_name, type_name}):
                continue
            status = re.search(r"已退款\(?[￥¥]([\d,.]+)", spend.status)
            full_refund = spend.status == "已全额退款" and spend.amount_minor == available
            partial_refund = status and minor(status[1]) == available
            if full_refund or partial_refund or same_description or spend.amount_minor == available:
                evidence = ("同一银行账户、交易说明、币种和金额一致" if bank_description_match
                            else "同渠道、币种和商户，金额可能对应")
                matches.append((spend, bool(full_refund or partial_refund), same_description, evidence))
        marked = [item for item in matches if item[1]]
        described = [item for item in marked if item[2]]
        verified = (described or marked) if receipt.source == "wechat" else []
        refund_matches.append((receipt, available, matches, verified))
    refund_targets = Counter(item[0].event_id for _, _, _, verified in refund_matches for item in verified)
    candidates = []
    for receipt, available, matches, verified in refund_matches:
        if len(verified) == 1 and refund_targets[verified[0][0].event_id] == 1:
            add(receipt, verified[0][0], "refund", available, "商户、退款状态和金额相符且两端匹配唯一")
        else:
            for spend, has_refund_status, same_description, evidence in matches:
                score = (0, not has_refund_status, not same_description, day_gap(spend, receipt),
                         receipt.event_id, spend.event_id)
                candidates.append((score, Link(receipt.event_id, spend.event_id, "refund", "candidate",
                                               available, evidence)))

    outgoing = [r for r in events.values() if r.direction == "out"
                and r.kind in {"expense", "advance", "pending"}
                and r.category != "跨渠道重复待确认" and r.event_id not in matched_expenses]
    tolerance_numerator, tolerance_denominator = reimbursement_tolerance.as_integer_ratio()
    for receipt in events.values():
        possible = (receipt.kind == "pending" and receipt.direction == "in"
                    and receipt.category != "跨渠道重复待确认")
        if receipt.kind != "reimbursement" and not possible:
            continue
        available = receipt.amount_minor
        if available <= 0 or receipt.event_id in matched_receipts:
            continue
        for spend in outgoing:
            capacity = spend.amount_minor
            if spend.currency != receipt.currency or capacity <= 0:
                continue
            difference = abs(available - capacity)
            if difference * 100 * tolerance_denominator > capacity * tolerance_numerator:
                continue
            same_party = bool(merchant(receipt.counterparty)
                              and merchant(receipt.counterparty) == merchant(spend.counterparty))
            advance = spend.kind == "advance" or bool(ADVANCE_HINT.search(spend.memo))
            difference_pct = 100 * difference / capacity
            gap = day_gap(spend, receipt)
            evidence = [f"金额相差 {difference_pct:.2f}%" if difference else "金额相同"]
            if same_party:
                evidence.append("交易对方一致")
            if advance:
                evidence.append("支出有代垫线索")
            if receipt.kind == "reimbursement":
                evidence.append("入账有报销线索")
            evidence.append(f"回款比支出{'晚' if gap >= 0 else '早'} {abs(gap)} 天")
            score = (1, not (same_party or advance), difference_pct, abs(gap), receipt.event_id, spend.event_id)
            candidates.append((score, Link(receipt.event_id, spend.event_id, "reimbursement", "candidate",
                                           min(available, capacity), "，".join(evidence))))

    for _, link in sorted(candidates, key=lambda item: item[0]):
        if link.first_id in matched_receipts or link.second_id in matched_expenses:
            continue
        matched_receipts.add(link.first_id)
        matched_expenses.add(link.second_id)
        links.append(link)
    return links
