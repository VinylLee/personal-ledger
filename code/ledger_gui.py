"""Desktop interface for the existing SQLite ledger and accounting rules."""

import argparse
import json
import queue
import sqlite3
import threading
import tkinter as tk
from argparse import Namespace
from calendar import monthcalendar, monthrange
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from tkinter import filedialog, font, messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText

import exchange
import ledger
from reports import (PENDING_DIRECTIONS, confirmed_adjustments as report_adjustments, decimal_text, entries,
                     filtered_entries, linked_receipt_categories)
from sources import minor

ALL = "全部"
GROUPS = {"按月": "month", "按日": "day", "按分类": "category",
          "月度分类": "month-category", "每日分类": "day-category"}
BASIS = {"净收支": "net", "纯收支": "pure", "原始收支": "gross", "现金流水": "cash"}
ADJUSTMENTS = {"跟随统计口径": None, "到账日冲减": "net", "回溯原支出": "pure",
               "排除回款": "gross", "列作流入": "cash"}
SIDES = {ALL: None, "仅收入": "income", "仅支出": "expense"}
REVIEW_TYPES = {"待复核交易": "pending", "报销": "reimbursement", "退款": "refund",
                "跨渠道重复": "duplicate", "账户划转": "internal_transfer", "已确认无关联": "unassociated"}
STATUSES = {"候选": "candidate", "已确认": "confirmed", "未分摊": "unlinked", "手动标记": "marked"}
KINDS = {"expense": "支出", "income": "收入", "refund": "退款", "reimbursement": "报销",
         "advance": "代垫", "internal": "划转", "investment": "理财", "failed": "未完成",
         "pending": "待复核", "excluded": "不计收支"}
EDIT_FIELDS = ("kind", "category", "counterparty", "description", "note")
OUT_ONLY_KINDS = frozenset(("expense", "advance"))
IN_OR_NEUTRAL_KINDS = frozenset(("income", "refund", "reimbursement"))
DIRECTIONS = {"in": "流入", "out": "流出", "neutral": "中性", "income": "收入", "expense": "支出"}
SOURCES = {"alipay": "支付宝", "wechat": "微信", "icbc": "工行", "ccb": "建行"}


def report_totals_text(totals, side, book):
    """Build report summary captions plus an optional conversion note."""
    captions = []
    foreign = [currency for currency in totals if currency != "CNY"]
    combined = {"income": Decimal(0), "net_expense": Decimal(0), "balance": Decimal(0)}
    missing, converted = set(), False
    fields = []
    if side != "expense":
        fields.append(("income", "收入"))
    if side != "income":
        fields.append(("net_expense", "净支出"))
    if side is None:
        fields.append(("balance", "结余"))
    for currency, (income, expense) in totals.items():
        values = {"income": income, "net_expense": expense, "balance": income - expense}
        parts = [currency]
        for key, label in fields:
            value = values[key]
            text = f"{label} {value:,.2f}"
            if currency == "CNY":
                combined[key] += value
            elif book is not None and value:
                converted_value = exchange.to_cny(value, currency, book)
                if converted_value is None:
                    missing.add(currency)
                else:
                    converted = True
                    combined[key] += converted_value
                    text += exchange.conversion_suffix(converted_value)
            parts.append(text)
        captions.append("   ".join(parts))
    note = ""
    if foreign:
        if book is None:
            note = "（汇率不可用，未折算非人民币金额）"
        elif converted:
            totals_text = []
            if side != "expense":
                totals_text.append(f"收入 {exchange.money(combined['income'])}")
            if side != "income":
                totals_text.append(f"净支出 {exchange.money(combined['net_expense'])}")
            if side is None:
                totals_text.append(f"结余 {exchange.money(combined['balance'])}")
            note = exchange.rate_note(book, foreign)
            if missing:
                note += f" · 缺 {', '.join(sorted(missing))} 汇率"
            captions.append("全部折合人民币   " + "   ".join(totals_text)
                            + (f"   · {note}" if note else ""))
            note = ""
        elif missing:
            note = f"（缺少 {', '.join(sorted(missing))} 汇率，未折算）"
    return captions, note


def transaction_totals_text(totals, book):
    """Build transaction summary captions plus an optional conversion note."""
    captions = []
    foreign = [currency for currency in totals if currency != "CNY"]
    fields = (("income", "收入"), ("expense", "支出"), ("refund", "退款"),
              ("reimbursement", "报销"), ("net_expense", "净支出"), ("balance", "结余"))
    combined = {key: Decimal(0) for key, _ in fields}
    missing, converted = set(), False
    for currency, amounts in sorted(totals.items()):
        income = amounts["income"]
        expense = amounts["expense"]
        refund = amounts["refund"]
        reimbursement = amounts["reimbursement"]
        net_expense = expense - refund - reimbursement
        values = {"income": income, "expense": expense, "refund": refund,
                  "reimbursement": reimbursement, "net_expense": net_expense,
                  "balance": income - net_expense}
        parts = []
        for key, label in fields:
            value = Decimal(decimal_text(values[key]))
            text = f"{label} {value:,.2f}"
            if currency == "CNY":
                combined[key] += value
            elif book is not None and value:
                converted_value = exchange.to_cny(value, currency, book)
                if converted_value is None:
                    missing.add(currency)
                else:
                    converted = True
                    combined[key] += converted_value
                    text += exchange.conversion_suffix(converted_value)
            parts.append(text)
        captions.append(f"{currency}  " + "   ".join(parts))
    note = ""
    if foreign:
        if book is None:
            note = "（汇率不可用，未折算非人民币金额）"
        elif converted:
            totals_text = [f"{label} {exchange.money(combined[key])}" for key, label in fields]
            note = exchange.rate_note(book, foreign)
            if missing:
                note += f" · 缺 {', '.join(sorted(missing))} 汇率"
            captions.append("全部折合人民币   " + "   ".join(totals_text)
                            + (f"   · {note}" if note else ""))
            note = ""
        elif missing:
            note = f"（缺少 {', '.join(sorted(missing))} 汇率，未折算）"
    return captions, note


def summary_label(parent, variable):
    """A wrapping summary label that follows the page width."""
    label = ttk.Label(parent, textvariable=variable, style="Total.TLabel",
                      wraplength=800, justify="left")
    label.pack(anchor="w", fill="x", pady=(2, 8))

    def resize(event):
        label.configure(wraplength=max(320, event.width - 24))

    parent.bind("<Configure>", resize, add="+")
    return label


def clearable_entry(parent, variable, width, *, on_clear=None):
    field = ttk.Frame(parent)
    entry = ttk.Entry(field, textvariable=variable, width=width)
    entry.pack(side="left")

    def clear():
        variable.set("")
        entry.focus_set()
        if on_clear:
            on_clear()

    ttk.Button(field, text="×", width=2, style="Clear.TButton", command=clear).pack(
        side="left", fill="y", padx=(3, 0))
    return field, entry


class DatePicker(simpledialog.Dialog):
    def __init__(self, parent, initial, first_year, last_year, title):
        self.initial = initial
        self.first_year = min(first_year, initial.year)
        self.last_year = max(last_year, initial.year)
        super().__init__(parent, title)

    def body(self, master):
        bar = ttk.Frame(master, padding=(4, 0, 4, 8))
        bar.pack()
        self.year = tk.StringVar(bar, str(self.initial.year))
        self.month = tk.StringVar(bar, f"{self.initial.month:02d}")
        year_choice = ttk.Combobox(bar, textvariable=self.year, state="readonly", width=6,
                                  values=tuple(range(self.first_year, self.last_year + 1)))
        month_choice = ttk.Combobox(bar, textvariable=self.month, state="readonly", width=4,
                                   values=tuple(f"{month:02d}" for month in range(1, 13)))
        for widget, label in ((year_choice, "年"), (month_choice, "月")):
            widget.pack(side="left")
            widget.bind("<<ComboboxSelected>>", lambda _: self.render_month())
            ttk.Label(bar, text=label).pack(side="left", padx=(4, 12))
        self.days = ttk.Frame(master)
        self.days.pack()
        self.render_month()
        return year_choice

    def render_month(self):
        for child in self.days.winfo_children():
            child.destroy()
        year, month = int(self.year.get()), int(self.month.get())
        for col, weekday in enumerate("一二三四五六日"):
            ttk.Label(self.days, text=weekday, anchor="center", style="Muted.TLabel").grid(
                row=0, column=col, sticky="ew", pady=(0, 4))
        for row, week in enumerate(monthcalendar(year, month), 1):
            for col, day in enumerate(week):
                if day:
                    choice = date(year, month, day)
                    style = "SelectedDate.TButton" if choice == self.initial else "Date.TButton"
                    ttk.Button(self.days, text=str(day), width=3, style=style,
                               command=lambda value=choice: self.choose(value)).grid(row=row, column=col, padx=2, pady=2)

    def choose(self, value):
        self.result = value.isoformat()
        self.cancel()

    def buttonbox(self):
        ttk.Button(self, text="取消", command=self.cancel).pack(pady=(4, 10))
        self.bind("<Escape>", self.cancel)


@dataclass(frozen=True)
class Column:
    key: str
    title: str
    width: int
    format: str = ""


@dataclass(frozen=True)
class ChartScope:
    start: str
    end: str
    currency: str
    side: str
    categories: tuple[str, ...] | None
    basis: str
    refunds: str | None
    reimbursements: str | None



class Table(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.tree = ttk.Treeview(self, show="headings", selectmode="browse")
        vertical = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        horizontal = ttk.Scrollbar(self, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.tree.tag_configure("alternate", background="#F1F5F9")
        self.records = {}
        self.columns = []
        self.sort_order = None

    def fill(self, columns, rows):
        layout_changed = self.columns != columns
        self.columns = columns
        self.sort_order = None
        self.tree.delete(*self.tree.get_children())
        if layout_changed:
            self.tree.configure(columns=[col.key for col in columns])
        for col in columns:
            anchor = "e" if col.format else "w"
            self.tree.heading(col.key, text=col.title, anchor=anchor,
                              command=lambda c=col: self.sort(c, False))
            if layout_changed:
                self.tree.column(col.key, width=col.width, minwidth=56, stretch=col.width >= 150,
                                 anchor=anchor)
        self.records = {str(index): row for index, row in enumerate(rows)}
        for index, row in self.records.items():
            values = [self.cell(row.get(col.key), col.format) for col in columns]
            self.tree.insert("", "end", iid=index, values=values,
                             tags=("alternate",) if int(index) % 2 else ())
        self.tree.yview_moveto(0)
        self.tree.xview_moveto(0)

    @staticmethod
    def cell(value, fmt):
        if value is None:
            return "—"
        if fmt == "money":
            return f"{Decimal(value):,.2f}"
        if fmt == "percent":
            return f"{value:.2f}%"
        return " ".join(str(value).split())

    def sort(self, col, reverse):
        def sort_key(item):
            value = self.records[item].get(col.key)
            return value is None, Decimal(str(value or 0)) if col.format else str(value or "")

        order = sorted(self.records, key=sort_key, reverse=reverse)
        for index, item in enumerate(order):
            self.tree.move(item, "", index)
            self.tree.item(item, tags=("alternate",) if index % 2 else ())
        for other in self.columns:
            self.tree.heading(other.key, text=other.title, command=lambda c=other: self.sort(c, False))
        self.tree.heading(col.key, text=f"{col.title} {'▼' if reverse else '▲'}",
                          command=lambda: self.sort(col, not reverse))
        self.sort_order = col, reverse

    def selected(self):
        selected = self.tree.selection()
        focused = self.tree.focus()
        return self.records.get(focused if focused in selected else selected[0]) if selected else None

    def selected_rows(self):
        return [self.records[item] for item in self.tree.selection()]

    @staticmethod
    def row_key(row):
        if "id" in row:
            return ("event", row["id"])
        if "first_id" in row:
            return ("link", *(row.get(key) for key in (
                "first_id", "second_id", "relation", "first_day", "first_source", "amount_minor", "evidence"
            )))
        return ("summary", *(row.get(key) for key in ("period", "currency", "side", "category")))

    def view_state(self):
        selected = [self.row_key(self.records[item]) for item in self.tree.selection()]
        focused = self.tree.focus()
        focus_key = self.row_key(self.records[focused]) if focused in self.records else None
        return self.sort_order, self.tree.xview()[0], self.tree.yview()[0], selected, focus_key

    def restore_view(self, state):
        sort_order, horizontal, vertical, selected, focused = state
        if sort_order:
            column = next((col for col in self.columns if col.key == sort_order[0].key), None)
            if column:
                self.sort(column, sort_order[1])
        remaining = Counter(selected)
        selection = []
        focus_item = None
        for item, row in self.records.items():
            key = self.row_key(row)
            if remaining[key]:
                selection.append(item)
                remaining[key] -= 1
            if key == focused and focus_item is None:
                focus_item = item
        if selection:
            self.tree.selection_set(*selection)
        if focus_item is not None:
            self.tree.focus(focus_item)
        self.tree.xview_moveto(horizontal)
        self.tree.yview_moveto(vertical)


def rebuild_with_decisions(database, meta, decisions):
    with TemporaryDirectory(prefix="ledger-review-") as folder:
        rule_file = Path(folder) / "decisions.json"
        rule_file.write_text(json.dumps(decisions, ensure_ascii=False), encoding="utf-8")
        return ledger.build(Namespace(db=database, input=Path(meta["input_root"]),
                                      start=meta["start"], end=meta["end"], decisions=rule_file))


def save_allocation(database, relation, receipt, expense, amount):
    return save_allocations(database, [(relation, receipt, expense, amount)])


def save_allocations(database, allocations):
    """Validate every selected pair before rebuilding the ledger once."""
    new_links = []
    selected_receipts, selected_expenses = set(), set()
    with closing(ledger.open_db(database)) as db:
        meta = dict(db.execute("SELECT key, value FROM metadata"))
        for index, (relation, receipt, expense, amount) in enumerate(allocations, 1):
            if amount <= 0:
                raise ValueError(f"第 {index} 组分摊金额必须大于零。")
            if receipt in selected_receipts or expense in selected_expenses:
                raise ValueError(f"第 {index} 组与本次选择中的其他关联重复占用回款或支出。")
            for event_id, endpoint, label in ((receipt, "first_id", "回款"), (expense, "second_id", "支出")):
                row = db.execute(f"""
                    SELECT e.amount_minor, EXISTS(
                        SELECT 1 FROM links l WHERE l.{endpoint} = e.id
                          AND l.status = 'confirmed' AND l.relation IN ('refund', 'reimbursement')
                    ) AS matched
                    FROM events e WHERE e.id = ?
                """, (event_id,)).fetchone()
                if row is None:
                    raise ValueError(f"第 {index} 组找不到交易编号 {event_id}。")
                if row["matched"]:
                    raise ValueError(f"第 {index} 组的{label}已有退款或报销关联，每笔交易只能配对一次。")
                if amount > row["amount_minor"]:
                    raise ValueError(f"第 {index} 组超过{label}金额 {decimal_text(row['amount_minor'])}。")
            selected_receipts.add(receipt)
            selected_expenses.add(expense)
            new_links.append({"relation": relation, "receipt": receipt, "expense": expense,
                              "amount": decimal_text(amount)})
    decisions = json.loads(meta["decisions"])
    decisions.setdefault("links", []).extend(new_links)
    if "unassociated" in decisions:
        paired = selected_receipts | selected_expenses
        decisions["unassociated"] = [event_id for event_id in decisions["unassociated"] if event_id not in paired]
    return rebuild_with_decisions(database, meta, decisions)


def save_duplicate_review(database, rows, *, merge):
    with closing(ledger.open_db(database)) as db:
        meta = dict(db.execute("SELECT key, value FROM metadata"))
        selected = {}
        for row in rows:
            bank, platform = row["first_id"], row["second_id"]
            candidate = db.execute("""
                SELECT 1 FROM links
                WHERE relation = 'duplicate' AND status = 'candidate' AND first_id = ? AND second_id = ?
            """, (bank, platform)).fetchone()
            if candidate is None:
                raise ValueError("所选重复候选已变化，请查询后重新选择。")
            if merge and bank in selected and selected[bank] != platform:
                raise ValueError("同一笔银行流水有多个候选，请只选择其中一条。")
            selected[bank] = platform if merge else None
        if merge and len(set(selected.values())) != len(selected):
            raise ValueError("同一笔平台流水只能与一笔银行流水合并。")
    decisions = json.loads(meta["decisions"])
    decisions.setdefault("duplicates", {}).update(selected)
    rebuild_with_decisions(database, meta, decisions)


def save_pending_classifications(database, classifications):
    with closing(ledger.open_db(database)) as db:
        meta = dict(db.execute("SELECT key, value FROM metadata"))
        for event_id in classifications:
            record = db.execute("SELECT kind, category FROM events WHERE id = ?", (event_id,)).fetchone()
            if record is None or record["kind"] != "pending":
                raise ValueError("所选交易状态已变化，请查询后重新选择。")
            if record["category"] == "跨渠道重复待确认":
                raise ValueError("所选流水有重复候选，请先在「跨渠道重复」中确认是否为同一笔交易。")
    decisions = json.loads(meta["decisions"])
    classify = decisions.setdefault("classify", {})
    for event_id, rule in classifications.items():
        # Keep text edits made before the nature was confirmed instead of replacing the entry.
        classify[event_id] = {**classify.get(event_id, {}), **rule}
    rebuild_with_decisions(database, meta, decisions)


def confirmed_adjustments(db, event_ids):
    return [row for row in db.execute("""
        SELECT first_id, second_id FROM links
        WHERE status = 'confirmed' AND relation IN ('refund', 'reimbursement')
    """) if row["first_id"] in event_ids or row["second_id"] in event_ids]


def save_unassociated(database, event_ids, *, allow_unlink):
    with closing(ledger.open_db(database)) as db:
        meta = dict(db.execute("SELECT key, value FROM metadata"))
        decisions = json.loads(meta["decisions"])
        for event_id in event_ids:
            record = db.execute("SELECT kind, category FROM events WHERE id = ?", (event_id,)).fetchone()
            if record is None or record["kind"] not in {"pending", "expense", "advance", "refund", "reimbursement"}:
                raise ValueError("请选择代付、支出、报销、退款或待复核流水。")
            if record["category"] == "跨渠道重复待确认":
                raise ValueError("请先复核这笔跨渠道重复候选。")
        if confirmed_adjustments(db, event_ids) and not allow_unlink:
            raise ValueError("所选流水已有确认关联，请重新核对后确认。")
        remaining = []
        for rule in decisions.get("links", []):
            if rule["receipt"] not in event_ids and rule["expense"] not in event_ids:
                remaining.append(rule)
                continue
            # Keep the transaction nature that was explicitly confirmed along with the old pairing.
            for event_id in (rule["receipt"], rule["expense"]):
                record = db.execute("SELECT kind, category, rule FROM events WHERE id = ?", (event_id,)).fetchone()
                if event_id == rule["receipt"] or record["rule"] == "人工确认原支出":
                    classification = decisions.setdefault("classify", {}).setdefault(event_id, {})
                    classification["kind"] = record["kind"]
                    if event_id == rule["receipt"]:
                        classification.setdefault("category", record["category"])
                    else:
                        classification["category"] = record["category"]
        if "links" in decisions:
            decisions["links"] = remaining
    decisions["unassociated"] = sorted(set(decisions.get("unassociated", [])) | event_ids)
    rebuild_with_decisions(database, meta, decisions)


def kind_conflicts_direction(kind, direction):
    return (kind in OUT_ONLY_KINDS and direction != "out"
            or kind in IN_OR_NEUTRAL_KINDS and direction == "out")


def conflicting_edit_links(db, event_id, kind):
    return [row for row in db.execute("""
        SELECT first_id, second_id, relation FROM links
        WHERE status = 'confirmed' AND relation IN ('refund', 'reimbursement')
          AND (? IN (first_id, second_id))
    """, (event_id,)) if (row["first_id"] == event_id and kind != row["relation"])
            or (row["second_id"] == event_id and kind not in {"expense", "advance"})]


def unlink_conflicting_links(db, decisions, classify, event_ids):
    """Drop refund/reimbursement links touching edited transactions and keep the other side's nature."""
    keep = []
    for link in decisions.get("links", []):
        touched = {link["receipt"], link["expense"]} & event_ids
        if not touched:
            keep.append(link)
            continue
        for event_id in touched:
            other_id = link["expense"] if event_id == link["receipt"] else link["receipt"]
            if other_id in event_ids:
                continue
            other = db.execute("SELECT kind, category, rule FROM events WHERE id = ?", (other_id,)).fetchone()
            if other and (other_id == link["receipt"] or other["rule"] == "人工确认原支出"):
                classify.setdefault(other_id, {"kind": other["kind"], "category": other["category"]})
    if "links" in decisions:
        decisions["links"] = keep
    decisions["unassociated"] = sorted(set(decisions.get("unassociated", [])) | event_ids)


def save_transaction_edits(database, edits, *, allow_unlink=False):
    """Merge partial edits for one or more transactions and rebuild the ledger once."""
    with closing(ledger.open_db(database)) as db:
        meta = dict(db.execute("SELECT key, value FROM metadata"))
        decisions = json.loads(meta["decisions"])
        classify = decisions.setdefault("classify", {})
        receipt_categories = linked_receipt_categories(db)
        current = {}
        for event_id, edit in edits.items():
            unsupported = set(edit) - set(EDIT_FIELDS)
            if unsupported:
                raise ValueError(f"不支持的修改字段：{', '.join(sorted(unsupported))}")
            row = db.execute("""
                SELECT kind, category, counterparty, description, direction FROM events WHERE id = ?
            """, (event_id,)).fetchone()
            if row is None:
                raise ValueError("交易已变化，请查询后重新选择。")
            if row["category"] == "跨渠道重复待确认":
                raise ValueError("所选流水有跨渠道重复候选，请先在「跨渠道重复」中复核。")
            if "category" in edit and not str(edit["category"]).strip():
                raise ValueError("批量设置的分类不能为空。")
            kind = edit.get("kind", row["kind"])
            if kind_conflicts_direction(kind, row["direction"]):
                raise ValueError(f"「{KINDS[kind]}」与交易 {event_id} 的流水方向不一致，请分开处理。")
            current[event_id] = row
        conflicts = {event_id for event_id, edit in edits.items()
                     if conflicting_edit_links(db, event_id, edit.get("kind", current[event_id]["kind"]))}
        if conflicts and not allow_unlink:
            raise ValueError("所选类型与已确认的退款或报销关联冲突。")
        if conflicts:
            unlink_conflicting_links(db, decisions, classify, conflicts)
    for event_id, edit in edits.items():
        row = current[event_id]
        rule = dict(classify.get(event_id) or {})
        rule.update({key: value for key, value in edit.items() if key in EDIT_FIELDS})
        # A link can determine the current type and displayed category of a receipt.
        # Keep its own category for use when the link is removed.
        rule["kind"] = edit.get("kind", row["kind"])
        own_category = rule.get("category", row["category"]) if event_id in receipt_categories else row["category"]
        rule["category"] = edit.get("category", own_category)
        rule["manual_override"] = True
        if rule["kind"] == "excluded" and not str(rule.get("note", "")).strip():
            raise ValueError(f"不计收支的交易需要填写备注：{event_id}")
        classify[event_id] = rule
    rebuild_with_decisions(database, meta, decisions)
    return len(edits)


def save_transaction_edit(database, event_id, edit, *, allow_unlink=False):
    return save_transaction_edits(database, {event_id: edit}, allow_unlink=allow_unlink)


class ConfirmationDialog(simpledialog.Dialog):
    save_label = "保存关联"

    def buttonbox(self):
        bar = ttk.Frame(self, padding=8)
        ttk.Button(bar, text=self.save_label, command=self.ok, style="Accent.TButton").pack(side="left", padx=4)
        ttk.Button(bar, text="取消", command=self.cancel).pack(side="left", padx=4)
        bar.pack()
        self.bind("<Return>", self.ok)
        self.bind("<Escape>", self.cancel)


class UnassociatedSideDialog(ConfirmationDialog):
    save_label = "选择"

    def body(self, master):
        ttk.Label(master, text="选择要标记为无关联的一侧").pack(anchor="w", pady=(4, 8))
        self.side = tk.StringVar(master, "回款")
        widget = ttk.Combobox(master, textvariable=self.side, state="readonly", width=18,
                              values=("回款", "支出", "回款和支出"))
        widget.pack(anchor="w")
        return widget

    def apply(self):
        self.result = {
            "回款": ("first_id",), "支出": ("second_id",),
            "回款和支出": ("first_id", "second_id"),
        }[self.side.get()]


class ClassificationDialog(ConfirmationDialog):
    save_label = "保存交易性质"
    flow_label = "按流水方向确认收入或支出"
    transfer_label = "本人账户划转"
    types = {
        "收入": "income", "支出": "expense", "退款": "refund", "报销": "reimbursement",
        "代垫支出": "advance", "本人账户划转": "internal", "理财": "investment",
        "不计收支": "excluded",
    }

    def __init__(self, parent, rows, categories=()):
        self.rows = rows
        self.categories = tuple(categories)
        super().__init__(parent, f"确认 {len(rows)} 笔待复核交易")

    def body(self, master):
        self.nature = tk.StringVar(master, self.flow_label)
        self.category = tk.StringVar(master)
        self.note = tk.StringVar(master)
        ttk.Label(master, text="交易类型").grid(row=0, column=0, sticky="w", padx=8, pady=5)
        choice = ttk.Combobox(master, textvariable=self.nature, state="readonly", width=28,
                              values=(self.flow_label, *self.types))
        choice.grid(row=0, column=1, sticky="w", padx=8, pady=5)
        ttk.Label(master, text="分类").grid(row=1, column=0, sticky="w", padx=8, pady=5)
        category = ttk.Combobox(master, textvariable=self.category, width=26, values=self.categories)
        category.grid(row=1, column=1, sticky="w", padx=8, pady=5)
        ttk.Label(master, text="备注（不计收支时必填）").grid(row=2, column=0, sticky="w", padx=8, pady=5)
        note_field, _ = clearable_entry(master, self.note, 28)
        note_field.grid(row=2, column=1, sticky="w", padx=8, pady=5)
        return choice

    def validate(self):
        result = {}
        for row in self.rows:
            if self.nature.get() == self.flow_label:
                kind = PENDING_DIRECTIONS.get(row["direction"])
                if kind is None:
                    messagebox.showerror("请核对交易方向", "所选交易含方向不明的流水，请分别复核。", parent=self)
                    return False
            else:
                kind = self.types[self.nature.get()]
            if kind_conflicts_direction(kind, row["direction"]):
                messagebox.showerror("请核对交易方向", "所选类型与部分流水方向不一致，请分别复核。", parent=self)
                return False
            note = self.note.get().strip()
            if kind == "excluded" and not note:
                messagebox.showerror("请填写备注", "请说明这笔交易不计入收支的原因。", parent=self)
                return False
            defaults = {"income": "其他收入", "expense": "其他支出", "refund": "未关联退款",
                        "reimbursement": "报销回款", "advance": "代垫支出",
                        "internal": "账户划转", "investment": "理财及利息", "excluded": "不计收支"}
            result[row["id"]] = {"kind": kind, "category": self.category.get().strip() or defaults[kind]}
            if note:
                result[row["id"]]["note"] = note
        self.result = result
        return True


class EditTransactionDialog(ConfirmationDialog):
    save_label = "保存修改"

    def __init__(self, parent, row, categories):
        self.row, self.categories = row, categories
        super().__init__(parent, "编辑交易")

    def body(self, master):
        self.kind = tk.StringVar(master, KINDS[self.row["kind"]])
        follows_expense = "own_category" in self.row
        self.category = tk.StringVar(master, self.row.get("own_category", self.row["category"]))
        self.party = tk.StringVar(master, self.row["counterparty"])
        self.description = tk.StringVar(master, self.row["description"])
        self.note = tk.StringVar(master, self.row.get("review_note", ""))
        fields = [
            ("交易类型", ttk.Combobox(master, textvariable=self.kind, values=tuple(KINDS.values()),
                                     state="readonly", width=27)),
            ("解除关联后的分类" if follows_expense else "分类",
             ttk.Combobox(master, textvariable=self.category, values=self.categories, width=27)),
            ("交易对方", clearable_entry(master, self.party, 29)[0]),
            ("说明", clearable_entry(master, self.description, 29)[0]),
            ("备注", clearable_entry(master, self.note, 29)[0]),
        ]
        for index, (label, widget) in enumerate(fields):
            ttk.Label(master, text=label).grid(row=index, column=0, sticky="w", padx=8, pady=5)
            widget.grid(row=index, column=1, sticky="w", padx=8, pady=5)
        if follows_expense:
            ttk.Label(master, text=f"当前分类为「{self.row['category']}」，随原支出更新。",
                      style="Muted.TLabel", wraplength=360).grid(
                row=len(fields), column=0, columnspan=2, sticky="w", padx=8, pady=(4, 0))
        return fields[0][1]

    def validate(self):
        kind = {label: key for key, label in KINDS.items()}[self.kind.get()]
        category, note = self.category.get().strip(), self.note.get().strip()
        if not category or (kind == "excluded" and not note):
            messagebox.showerror("请检查交易信息", "请填写分类。不计收支的交易还需填写备注。", parent=self)
            return False
        if kind_conflicts_direction(kind, self.row["direction"]):
            messagebox.showerror("请核对交易方向", "所选类型与流水方向不一致。", parent=self)
            return False
        self.result = {"kind": kind, "category": category, "counterparty": self.party.get().strip(),
                       "description": self.description.get().strip(), "note": note}
        return True


class BatchEditTransactionDialog(ConfirmationDialog):
    save_label = "保存批量修改"
    fields = (
        ("kind", "交易类型", tuple(KINDS.values())),
        ("category", "分类", None),
        ("counterparty", "交易对方", None),
        ("description", "说明", None),
        ("note", "备注", None),
    )

    def __init__(self, parent, rows, categories):
        self.rows, self.categories = rows, tuple(categories)
        super().__init__(parent, f"批量编辑 {len(rows)} 笔交易")

    def body(self, master):
        ttk.Label(master, text="勾选要统一修改的字段，未勾选的字段保持每笔原值。",
                  style="Muted.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", padx=8, pady=(4, 6))
        self.checked, self.variables, self.widgets = {}, {}, {}
        for index, (key, label, options) in enumerate(self.fields, 1):
            checked = self.checked[key] = tk.BooleanVar(master, False)
            variable = self.variables[key] = tk.StringVar(master)
            ttk.Checkbutton(master, text=label, variable=checked,
                            command=lambda k=key: self.toggle(k)).grid(
                row=index, column=0, sticky="w", padx=8, pady=4)
            if options:
                widget = ttk.Combobox(master, textvariable=variable, values=options,
                                      state="disabled", width=27)
            elif key == "category":
                widget = ttk.Combobox(master, textvariable=variable, values=self.categories,
                                      state="disabled", width=27)
            else:
                widget = ttk.Entry(master, textvariable=variable, width=29, state="disabled")
            self.widgets[key] = widget
            widget.grid(row=index, column=1, sticky="w", padx=8, pady=4)
        ttk.Label(master, text="勾选但留空会清空该字段；交易类型和分类不能清空。",
                  style="Muted.TLabel").grid(row=len(self.fields) + 1, column=0, columnspan=2,
                                             sticky="w", padx=8, pady=(6, 2))
        if any("own_category" in row for row in self.rows):
            ttk.Label(master, text="已关联回款的当前分类随原支出更新。修改回款自身分类会在解除关联后生效。",
                      style="Muted.TLabel", wraplength=360).grid(
                row=len(self.fields) + 2, column=0, columnspan=2, sticky="w", padx=8, pady=(2, 0))
        return self.widgets["kind"]

    def toggle(self, key):
        checked = self.checked[key].get()
        state = ("readonly" if key == "kind" else "normal") if checked else "disabled"
        self.widgets[key].configure(state=state)
        if checked:
            self.widgets[key].focus_set()

    def validate(self):
        result = {key: self.variables[key].get().strip()
                  for key in EDIT_FIELDS if self.checked[key].get()}
        if not result:
            messagebox.showerror("请检查批量修改", "请至少勾选一个要统一修改的字段。", parent=self)
            return False
        if "kind" in result:
            kinds = {label: key for key, label in KINDS.items()}
            if result["kind"] not in kinds:
                messagebox.showerror("请检查批量修改", "请选择要统一设置的交易类型。", parent=self)
                return False
            kind = kinds[result["kind"]]
            mismatched = sum(kind_conflicts_direction(kind, row["direction"]) for row in self.rows)
            if mismatched:
                messagebox.showerror(
                    "请核对交易方向",
                    f"所选中有 {mismatched} 笔流水的方向与「{KINDS[kind]}」不一致，请分开处理。",
                    parent=self)
                return False
            result["kind"] = kind
        if "category" in result and not result["category"]:
            messagebox.showerror("请检查批量修改", "批量设置的分类不能为空。", parent=self)
            return False
        duplicated = sum(row.get("category") == "跨渠道重复待确认" for row in self.rows)
        if duplicated:
            messagebox.showerror(
                "请先复核重复候选",
                f"所选中有 {duplicated} 笔流水存在跨渠道重复候选，请先在「跨渠道重复」中复核。",
                parent=self)
            return False
        missing_notes = sum(
            result.get("kind", row["kind"]) == "excluded"
            and not (result["note"] if "note" in result else row.get("review_note", "")).strip()
            for row in self.rows)
        if missing_notes:
            messagebox.showerror(
                "请填写备注",
                f"所选中有 {missing_notes} 笔交易将不计收支，请勾选备注并填写原因。",
                parent=self)
            return False
        self.result = result
        return True


class AllocationDialog(ConfirmationDialog):
    def __init__(self, parent, row=None):
        self.row = row or {}
        super().__init__(parent, "确认退款或报销关联")

    def body(self, master):
        label = "退款" if self.row.get("relation") == "refund" else "报销"
        self.relation = tk.StringVar(master, label)
        self.receipt = tk.StringVar(master, self.row.get("first_id", ""))
        self.expense = tk.StringVar(master, self.row.get("second_id", ""))
        self.amount = tk.StringVar(master, self.row.get("amount", ""))
        receipt_field, receipt_entry = clearable_entry(master, self.receipt, 34)
        expense_field, _ = clearable_entry(master, self.expense, 34)
        amount_field, amount_entry = clearable_entry(master, self.amount, 16)
        fields = [("关联类型", ttk.Combobox(master, textvariable=self.relation, values=("报销", "退款"),
                                           state="readonly", width=12)),
                  ("回款编号", receipt_field), ("支出编号", expense_field), ("本次分摊金额", amount_field)]
        for index, (title, widget) in enumerate(fields):
            ttk.Label(master, text=title).grid(row=index, column=0, sticky="w", padx=8, pady=5)
            widget.grid(row=index, column=1, sticky="w", padx=8, pady=5)
        return amount_entry if self.row else receipt_entry

    def validate(self):
        try:
            amount = minor(self.amount.get().strip())
            receipt, expense = self.receipt.get().strip(), self.expense.get().strip()
            if amount <= 0 or not receipt or not expense or receipt == expense:
                raise ValueError("请输入两笔不同的交易编号和大于零的分摊金额。")
        except (ValueError, ArithmeticError) as exc:
            messagebox.showerror("请检查分摊信息", str(exc), parent=self)
            return False
        self.result = [("refund" if self.relation.get() == "退款" else "reimbursement", receipt, expense, amount)]
        return True


class BatchAllocationDialog(ConfirmationDialog):
    save_label = "全部保存"

    def __init__(self, parent, rows):
        self.rows = rows
        kind = "退款" if rows[0]["relation"] == "refund" else "报销"
        super().__init__(parent, f"确认 {len(rows)} 组{kind}关联")

    def body(self, master):
        width = min(780, self.winfo_screenwidth() - 100)
        details_width = max(160, (width - 160) // 2)
        height = min(360, 34 + 56 * len(self.rows))
        canvas = tk.Canvas(master, width=width, height=height, background="#F8FAFC",
                           highlightthickness=0, yscrollincrement=24)
        scrollbar = ttk.Scrollbar(master, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        content = ttk.Frame(canvas)
        window = canvas.create_window(0, 0, window=content, anchor="nw")
        content.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        self.bind("<MouseWheel>", lambda event: canvas.yview_scroll(-int(event.delta / 120), "units"))
        for col, label in enumerate(("回款", "支出", "分摊金额")):
            ttk.Label(content, text=label, style="Total.TLabel").grid(row=0, column=col, sticky="w", padx=8, pady=6)
        content.columnconfigure((0, 1), weight=1, uniform="pair")
        self.amounts = []
        for index, row in enumerate(self.rows, 1):
            for col, endpoint in enumerate(("first", "second")):
                party = row[f"{endpoint}_display"] or "—"
                amount = decimal_text(row[f"{endpoint}_amount_minor"])
                text = f"{row[f'{endpoint}_day']}  {row['currency']} {amount}\n{party}"
                ttk.Label(content, text=text, wraplength=details_width).grid(
                    row=index, column=col, sticky="w", padx=8, pady=5)
            variable = tk.StringVar(content, row["amount"])
            self.amounts.append(variable)
            field, entry = clearable_entry(content, variable, 12)
            field.grid(row=index, column=2, sticky="w", padx=8, pady=5)
            if index == 1:
                first_entry = entry
        return first_entry

    def validate(self):
        result = []
        for index, (row, variable) in enumerate(zip(self.rows, self.amounts), 1):
            try:
                amount = minor(variable.get().strip())
                limit = min(row["first_amount_minor"], row["second_amount_minor"])
                if not 0 < amount <= limit:
                    raise ValueError(f"分摊金额应大于零且不超过 {decimal_text(limit)}。")
            except (ValueError, ArithmeticError) as exc:
                messagebox.showerror("请检查分摊金额", f"第 {index} 组：{exc}", parent=self)
                return False
            result.append((row["relation"], row["first_id"], row["second_id"], amount))
        self.result = result
        return True


class LedgerWindow:
    def __init__(self, root, database):
        self.root, self.database = root, database
        with closing(ledger.open_db(database)) as db:
            self.meta = dict(db.execute("SELECT key, value FROM metadata"))
            currencies = [r[0] for r in db.execute("SELECT DISTINCT currency FROM events ORDER BY currency")]
            categories = [r[0] for r in db.execute("SELECT DISTINCT category FROM events ORDER BY category")]
        self.currencies = [ALL, *currencies]
        self.categories = [ALL, *categories]
        self.forms = {}
        self.tables = {}
        self.pages = {}
        self.category_widgets = []
        self.loaded_pages = set()
        self.page_versions = {}
        self.page_status = {}
        self.applied_forms = {}
        self.active_page = None
        self.status = tk.StringVar(root)
        self.totals = tk.StringVar(root)
        self.transaction_totals = tk.StringVar(root)
        self.chart_note = tk.StringVar(root)
        self.chart_scope = None
        self.chart_detail = None
        self.chart_detail_text = tk.StringVar(root)
        self._rate_book = None
        self._rate_fetching = False
        self._rate_attempted = False
        self._rate_retry_at = None
        self._rate_queue = queue.Queue()
        root.title("收支账本")
        width = min(1140, root.winfo_screenwidth() - 48)
        height = min(760, root.winfo_screenheight() - 94)
        root.geometry(f"{width}x{height}")
        root.minsize(min(920, width), min(590, height))
        self.style()
        window = ttk.Frame(root, padding=(22, 16))
        window.pack(fill="both", expand=True)
        ttk.Label(window, text="收支账本", style="Title.TLabel").pack(anchor="w")
        ttk.Label(window, text=f"{self.meta['start']} 至 {self.meta['end']}  ·  {database.name}",
                  style="Muted.TLabel").pack(anchor="w", pady=(0, 14))
        self.tabs = ttk.Notebook(window)
        self.tabs.pack(fill="both", expand=True)
        self.create_report()
        self.create_transactions()
        self.create_review()
        self.create_charts()
        ttk.Label(window, textvariable=self.status, style="Muted.TLabel").pack(anchor="w", pady=(10, 0))
        self.tabs.bind("<<NotebookTabChanged>>", self.tab_changed)
        self.refresh("report")

    def style(self):
        families = set(font.families(self.root))
        family = "Inter" if "Inter" in families else "Microsoft YaHei UI"
        for name in ("TkDefaultFont", "TkTextFont", "TkHeadingFont"):
            font.nametofont(name, self.root).configure(family=family, size=10)
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", background="#F8FAFC", foreground="#172B42", font=(family, 10))
        style.configure("Title.TLabel", font=(family, 22, "bold"))
        style.configure("Muted.TLabel", foreground="#475569")
        style.configure("Total.TLabel", font=(family, 10, "bold"), foreground="#155E75")
        style.configure("TNotebook.Tab", padding=(18, 8))
        style.map("TNotebook.Tab", background=[("selected", "#FFFFFF")])
        style.configure("Treeview", background="#FFFFFF", fieldbackground="#FFFFFF", rowheight=30)
        style.configure("Treeview.Heading", background="#E2E8F0", padding=(6, 6),
                        font=(family, 10, "bold"))
        style.map("Treeview", background=[("selected", "#CCFBF1")], foreground=[("selected", "#134E4A")])
        style.configure("TButton", padding=(10, 5))
        style.configure("Clear.TButton", padding=(2, 0))
        style.configure("Date.TButton", padding=(4, 3))
        style.configure("SelectedDate.TButton", padding=(4, 3), background="#0E6674", foreground="#FFFFFF")
        style.configure("Accent.TButton", background="#0E6674", foreground="#FFFFFF")
        style.map("Accent.TButton", background=[("active", "#115E59"), ("disabled", "#94A3B8")])
        self.root.configure(background="#F8FAFC")

    def page(self, name, title):
        frame = ttk.Frame(self.tabs, padding=12)
        self.tabs.add(frame, text=title)
        self.forms[name] = {}
        self.pages[str(frame)] = name
        return frame

    @staticmethod
    def toolbar(parent):
        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=(0, 10))
        return bar

    def field(self, bar, page, key, label, default, options=None, width=13, changed=None, *, calendar=False):
        group = ttk.Frame(bar)
        group.pack(side="left", padx=(0, 12))
        ttk.Label(group, text=label, style="Muted.TLabel").pack(anchor="w", pady=(0, 3))
        variable = tk.StringVar(group, default)
        self.forms[page][key] = variable
        if options is None:
            refresh = lambda: self.refresh(page)
            field, widget = clearable_entry(group, variable, width, on_clear=None if changed else refresh)
            widget.bind("<Return>", lambda _: (changed or refresh)())
            if calendar:
                ttk.Button(field, text="日历", style="Clear.TButton",
                           command=lambda: self.pick_date(page, key, label)).pack(side="left", fill="y", padx=(3, 0))
            field.pack()
        else:
            widget = ttk.Combobox(group, textvariable=variable, values=tuple(options), state="readonly", width=width)
            widget.bind("<<ComboboxSelected>>", lambda _: changed() if changed else self.refresh(page))
            widget.pack()
            if key == "category":
                self.category_widgets.append(widget)
        return widget

    def get(self, page, field):
        return self.forms[page][field].get().strip()

    def date_fields(self, bar, page):
        first = date.fromisoformat(self.meta["start"])
        last = date.fromisoformat(self.meta["end"])
        months = [f"{year:04d}-{month:02d}" for year in range(first.year, last.year + 1)
                  for month in range(1, 13) if first.isoformat()[:7] <= f"{year:04d}-{month:02d}" <= last.isoformat()[:7]]
        self.field(bar, page, "preset", "快速选月", "完整账期", ["完整账期", *months], 10,
                   changed=lambda: self.preset(page))
        self.field(bar, page, "start", "开始日期", self.meta["start"], width=11, calendar=True)
        self.field(bar, page, "end", "结束日期", self.meta["end"], width=11, calendar=True)

    def pick_date(self, page, key, label):
        try:
            initial = date.fromisoformat(self.get(page, key))
        except ValueError:
            initial = date.fromisoformat(self.meta[key])
        first_year, last_year = (date.fromisoformat(self.meta[k]).year for k in ("start", "end"))
        dialog = DatePicker(self.root, initial, first_year, last_year, label)
        if dialog.result:
            self.forms[page][key].set(dialog.result)

    def preset(self, page):
        period = self.get(page, "preset")
        start, end = self.meta["start"], self.meta["end"]
        if period != "完整账期":
            year, month = map(int, period.split("-"))
            start = max(start, period + "-01")
            end = min(end, f"{period}-{monthrange(year, month)[1]:02d}")
        self.forms[page]["start"].set(start)
        self.forms[page]["end"].set(end)
        self.refresh(page)

    def scope(self, page):
        start, end = [ledger.iso_date(self.get(page, key) or self.meta[key]) for key in ("start", "end")]
        if start > end:
            raise ValueError("开始日期晚于结束日期。")
        currency, category = [self.get(page, key) for key in ("currency", "category")]
        return dict(start=start, end=end, currency=None if currency == ALL else currency,
                    category=None if category == ALL else category)

    def add_table(self, page, parent):
        table = self.tables[page] = Table(parent)
        table.pack(fill="both", expand=True)
        return table

    def create_report(self):
        page = self.page("report", "收支报表")
        bar = self.toolbar(page)
        self.field(bar, "report", "by", "分组", "按月", GROUPS, 10)
        self.field(bar, "report", "basis", "统计口径", "净收支", BASIS, 11)
        self.field(bar, "report", "refunds", "退款口径", "跟随统计口径", ADJUSTMENTS, 15)
        self.field(bar, "report", "reimbursements", "报销口径", "跟随统计口径", ADJUSTMENTS, 15)
        self.field(bar, "report", "side", "收支方向", ALL, SIDES, 9)
        self.field(bar, "report", "currency", "币种", ALL, self.currencies, 8)
        bar = self.toolbar(page)
        self.date_fields(bar, "report")
        self.field(bar, "report", "category", "分类", ALL, self.categories, 18)
        ttk.Button(bar, text="查询", style="Accent.TButton", command=lambda: self.refresh("report")).pack(
            side="left", anchor="s")
        summary_label(page, self.totals)
        self.add_table("report", page)

    def create_transactions(self):
        page = self.page("transactions", "交易明细")
        self.transaction_filters = ttk.Frame(page)
        self.transaction_filters.pack(fill="x")
        bar = self.toolbar(self.transaction_filters)
        self.date_fields(bar, "transactions")
        self.field(bar, "transactions", "kind", "交易类型", ALL, [ALL, *KINDS.values()], 8)
        self.field(bar, "transactions", "currency", "币种", ALL, self.currencies, 5)
        bar = self.toolbar(self.transaction_filters)
        self.field(bar, "transactions", "category", "分类", ALL, self.categories, 14)
        self.field(bar, "transactions", "search", "交易对方、说明、交易编号或订单号", "", width=31)
        self.field(bar, "transactions", "amount", "原金额", "", width=12)
        ttk.Button(bar, text="查询", style="Accent.TButton", command=lambda: self.refresh("transactions")).pack(
            side="left", anchor="s")
        bar = self.toolbar(self.transaction_filters)
        for key, label in (("hide_reimbursements", "隐藏报销回款及已报销支出"),
                           ("hide_refunds", "隐藏退款回款及已退款支出")):
            variable = tk.BooleanVar(bar, False)
            self.forms["transactions"][key] = variable
            ttk.Checkbutton(bar, text=label, variable=variable,
                            command=lambda: self.refresh("transactions", preserve=True)).pack(
                side="left", padx=(0, 12))
        self.chart_detail_bar = ttk.Frame(page)
        ttk.Button(self.chart_detail_bar, text="清除图表筛选", command=self.clear_chart_detail).pack(
            side="right", padx=(10, 0))
        ttk.Label(self.chart_detail_bar, textvariable=self.chart_detail_text, wraplength=660,
                  justify="left").pack(side="left", fill="x", expand=True)
        actions = self.transaction_actions = self.toolbar(page)
        for label, command in (("查看原始流水", lambda: self.show_selected("transactions")),
                               ("编辑交易", self.edit_transaction),
                               ("确认无关联", lambda: self.mark_unassociated("transactions"))):
            ttk.Button(actions, text=label, command=command).pack(side="left", padx=(0, 8))
        ttk.Label(actions, text="Ctrl / Shift 多选可批量编辑", style="Muted.TLabel").pack(
            side="left", padx=(4, 0))
        summary_label(page, self.transaction_totals)
        table = self.add_table("transactions", page)
        table.tree.configure(selectmode="extended")
        table.tree.bind("<Double-1>", lambda event: self.double_click(event, "transactions"))

    def create_review(self):
        page = self.page("review", "退款与报销复核")
        bar = self.toolbar(page)
        self.field(bar, "review", "type", "复核类型", "报销", REVIEW_TYPES, 14,
                   changed=self.change_review_type)
        self.review_status = self.field(bar, "review", "status", "关联状态", "候选", STATUSES, 10)
        self.field(bar, "review", "event", "按交易编号筛选", "", width=26)
        ttk.Button(bar, text="查询", command=lambda: self.refresh("review")).pack(side="left", anchor="s", padx=(0, 18))
        tolerance = ledger.configured_reimbursement_tolerance(json.loads(self.meta["decisions"]))
        self.field(bar, "review", "tolerance", "报销差额上限 (%)", str(tolerance), width=12,
                   changed=self.apply_tolerance)
        ttk.Button(bar, text="应用阈值", command=self.apply_tolerance).pack(side="left", anchor="s")
        bar = self.toolbar(page)
        self.review_first = ttk.Button(bar, command=lambda: self.show_selected("review", "first_id"))
        self.review_second = ttk.Button(bar, command=lambda: self.show_selected("review", "second_id"))
        self.review_confirm = ttk.Button(bar, text="确认所选关联", style="Accent.TButton",
                                         command=self.confirm_review)
        self.review_independent = ttk.Button(bar, text="保留独立流水",
                                             command=lambda: self.resolve_duplicates(merge=False))
        self.review_unassociated = ttk.Button(bar, text="确认无关联",
                                             command=lambda: self.mark_unassociated("review"))
        for button in (self.review_first, self.review_second, self.review_confirm,
                       ttk.Button(bar, text="手动关联", command=lambda: self.allocate(False)),
                       ttk.Button(bar, text="导出复核规则", command=self.export_decisions)):
            button.pack(side="left", padx=(0, 8))
        ttk.Label(bar, text="Ctrl / Shift 多选", style="Muted.TLabel").pack(side="left", padx=(4, 0))
        table = self.add_table("review", page)
        table.tree.configure(selectmode="extended")
        table.tree.bind("<Double-1>", lambda event: self.double_click(event, "review"))

    def create_charts(self):
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
        from charts import LedgerCharts

        page = self.page("charts", "图表分析")
        bar = self.toolbar(page)
        self.field(bar, "charts", "period", "时间分组", "按月", ("按月", "按日"), 8)
        self.field(bar, "charts", "side", "统计内容", "支出", ("支出", "收入"), 8)
        self.field(bar, "charts", "basis", "统计口径", "净收支", BASIS, 10)
        self.field(bar, "charts", "refunds", "退款口径", "跟随统计口径", ADJUSTMENTS, 14)
        self.field(bar, "charts", "reimbursements", "报销口径", "跟随统计口径", ADJUSTMENTS, 14)
        currencies = self.currencies[1:]
        default_currency = "CNY" if "CNY" in currencies else currencies[0]
        self.field(bar, "charts", "currency", "币种", default_currency, currencies, 5)
        bar = self.toolbar(page)
        self.date_fields(bar, "charts")
        self.field(bar, "charts", "category", "分类", ALL, self.categories, 14)
        ttk.Button(bar, text="查询", style="Accent.TButton", command=lambda: self.refresh("charts")).pack(
            side="left", anchor="s")
        ttk.Label(page, textvariable=self.chart_note, style="Muted.TLabel", wraplength=850).pack(
            anchor="w", pady=(0, 5))
        self.charts = LedgerCharts()
        self.chart_canvas = FigureCanvasTkAgg(self.charts.figure, master=page)
        self.chart_canvas.get_tk_widget().pack(fill="both", expand=True)
        self.chart_tooltip = tk.Label(page, justify="left", anchor="w", wraplength=420,
                                      background="#F8FAFC", foreground="#172B42", highlightbackground="#CBD5E1",
                                      highlightthickness=1, padx=10, pady=7, font="TkDefaultFont")
        self.chart_canvas.mpl_connect("motion_notify_event", self.hover_chart)
        self.chart_canvas.mpl_connect("figure_leave_event", self.hide_chart_tooltip)
        self.chart_canvas.mpl_connect("button_press_event", self.open_chart_detail)

    def hover_chart(self, event):
        target = self.charts.target_at(event)
        if target is None:
            self.hide_chart_tooltip()
            return
        tooltip = self.chart_tooltip
        canvas = self.chart_canvas.get_tk_widget()
        page = canvas.master
        if tooltip.cget("text") != target.tooltip:
            tooltip.configure(text=target.tooltip)
            tooltip.update_idletasks()
        canvas.configure(cursor="hand2")
        mouse = event.guiEvent
        x = canvas.winfo_x() + (mouse.x if mouse is not None else event.x)
        y = canvas.winfo_y() + (mouse.y if mouse is not None else canvas.winfo_height() - event.y)
        width, height = tooltip.winfo_reqwidth(), tooltip.winfo_reqheight()
        left = x + 14 if x + width + 14 <= page.winfo_width() else max(0, x - width - 14)
        top = y + 14 if y + height + 14 <= page.winfo_height() else max(0, y - height - 14)
        tooltip.place(x=left, y=top)
        tooltip.lift()

    def hide_chart_tooltip(self, _event=None):
        self.chart_tooltip.place_forget()
        self.chart_canvas.get_tk_widget().configure(cursor="")

    def open_chart_detail(self, event):
        if event.button != 1 or self.chart_scope is None:
            return
        target = self.charts.target_at(event)
        if target is None:
            return
        scope = self.chart_scope
        self.chart_detail = replace(scope, start=target.start or scope.start, end=target.end or scope.end,
                                    categories=target.categories if target.categories is not None else scope.categories)
        self.hide_chart_tooltip()
        self.transaction_filters.pack_forget()
        self.chart_detail_bar.pack(fill="x", pady=(0, 10), before=self.transaction_actions)
        self.refresh("transactions")
        self.tabs.select(self.tables["transactions"].master)

    def clear_chart_detail(self):
        self.chart_detail = None
        self.chart_detail_bar.pack_forget()
        self.transaction_filters.pack(fill="x", before=self.transaction_actions)
        self.refresh("transactions", use_applied=True)

    def change_review_type(self):
        kind = REVIEW_TYPES[self.get("review", "type")]
        self.forms["review"]["status"].set("已确认" if kind in {"internal_transfer", "unassociated"} else "候选")
        self.refresh("review")

    def database_version(self):
        try:
            state = self.database.stat()
        except FileNotFoundError:
            return None
        return state.st_ino, state.st_size, state.st_mtime_ns

    def tab_changed(self, _event):
        page = self.pages[self.tabs.select()]
        if self.active_page == "charts" and page != "charts":
            self.hide_chart_tooltip()
        if self.active_page and self.active_page != page:
            self.page_status[self.active_page] = self.status.get()
        self.active_page = page
        if page not in self.loaded_pages:
            self.refresh(page)
        elif self.page_versions[page] != self.database_version():
            self.refresh(page, preserve=True, use_applied=True)
        else:
            self.status.set(self.page_status.get(page, ""))

    def refresh(self, page=None, *, preserve=False, use_applied=False):
        page = page or self.pages[self.tabs.select()]
        table = self.tables.get(page)
        view = table.view_state() if preserve and table else None
        current = {name: value.get() for name, value in self.forms[page].items()}
        applied = self.applied_forms.get(page) if use_applied else None
        if applied is not None:
            for name, value in applied.items():
                self.forms[page][name].set(value)
        previous_status = self.status.get()
        try:
            with closing(ledger.open_db(self.database)) as db:
                getattr(self, f"load_{page}")(db)
            if view:
                table.restore_view(view)
            self.loaded_pages.add(page)
            self.page_versions[page] = self.database_version()
            self.page_status[page] = self.status.get()
            if page != "transactions" or self.chart_detail is None:
                self.applied_forms[page] = {name: value.get() for name, value in self.forms[page].items()}
        except (ValueError, ArithmeticError, sqlite3.Error) as exc:
            self.status.set(f"查询失败  {exc}")
            messagebox.showerror("查询失败", str(exc), parent=self.root)
        finally:
            if applied is not None:
                for name, value in current.items():
                    self.forms[page][name].set(value)
            if self.active_page and self.active_page != page:
                self.status.set(previous_status)

    def rate_book(self):
        """Return cached rates immediately; refresh stale rates in the background."""
        now = datetime.now(timezone.utc)
        if self._rate_book is not None:
            if now - self._rate_book.fetched_at <= exchange.MAX_AGE:
                return self._rate_book
            if self._rate_fetching or (self._rate_retry_at and now < self._rate_retry_at):
                return self._rate_book
        path = self.database.parent / "exchange_rates.json"
        cached = exchange.read_cache(path)
        if cached is not None:
            self._rate_book = cached
            if cached.stale and not self._rate_fetching:
                self._rate_fetching = True
                self._rate_retry_at = now + timedelta(minutes=10)
                threading.Thread(target=self._refresh_rates, args=(path,), daemon=True).start()
                self.root.after(100, self._poll_rates)
            return cached
        if self._rate_attempted:
            return self._rate_book
        self._rate_attempted = True
        self._rate_retry_at = now + timedelta(minutes=10)
        previous = self.status.get()
        self.status.set("正在获取汇率…")
        self.root.configure(cursor="watch")
        self.root.update_idletasks()
        try:
            fetched = exchange.fetch_rates(path)
        finally:
            self.root.configure(cursor="")
            self.status.set(previous)
        if fetched is not None:
            self._rate_book = fetched
            return fetched
        return self._rate_book

    def _refresh_rates(self, path):
        """Run only network/file I/O; the main thread polls the queue."""
        try:
            fetched = exchange.fetch_rates(path)
        except Exception:
            fetched = None
        self._rate_queue.put(fetched)

    def _poll_rates(self):
        try:
            fetched = self._rate_queue.get_nowait()
        except queue.Empty:
            try:
                if self.root.winfo_exists():
                    self.root.after(100, self._poll_rates)
            except tk.TclError:
                pass
            return
        self._rate_fetching = False
        if fetched is None:
            return
        try:
            if not self.root.winfo_exists():
                return
        except tk.TclError:
            return
        self._rate_book = fetched
        self._rate_retry_at = None
        for page in ("report", "transactions"):
            if page in self.loaded_pages:
                self.refresh(page, preserve=True, use_applied=True)

    def load_report(self, db):
        get = lambda key: self.get("report", key)
        args = Namespace(**self.scope("report"), by=GROUPS[get("by")], basis=BASIS[get("basis")],
                         side=SIDES[get("side")], refunds=ADJUSTMENTS[get("refunds")],
                         reimbursements=ADJUSTMENTS[get("reimbursements")])
        rows = ledger.report(db, args)
        columns = []
        if args.by != "category":
            columns.append(Column("period", "日期" if args.by.startswith("day") else "月份", 103))
        columns.append(Column("currency", "币种", 62))
        if "category" in args.by:
            columns.extend([Column("side", "方向", 64), Column("category", "分类", 130)])
        if args.side != "expense":
            columns.append(Column("income", "收入", 103, "money"))
        if args.side != "income":
            columns.append(Column("expense", "支出", 103, "money"))
        columns.extend([Column("refunds", "退款", 90, "money"),
                        Column("reimbursements", "报销", 90, "money")])
        if args.side != "income":
            columns.append(Column("net_expense", "净支出", 107, "money"))
        if args.side is None and "category" not in args.by:
            columns.append(Column("balance", "结余", 108, "money"))
        if "category" in args.by:
            columns.append(Column("share_pct", "占比", 72, "percent"))
        totals = defaultdict(lambda: [Decimal(0), Decimal(0)])
        for row in rows:
            amounts = totals[row["currency"]]
            amounts[0] += Decimal(row["income"])
            amounts[1] += Decimal(row["net_expense"])
        book = self.rate_book() if any(currency != "CNY" for currency in totals) else None
        captions, note = report_totals_text(totals, args.side, book)
        if note:
            captions.append(note)
        self.totals.set("\n".join(captions) or "当前筛选没有收支记录")
        self.tables["report"].fill(columns, [{**r, "side": DIRECTIONS.get(r.get("side"))} for r in rows])
        pending = db.execute("SELECT COUNT(*) FROM events WHERE kind = 'pending'").fetchone()[0]
        self.status.set(f"{len(rows)} 组汇总  ·  账本内待复核 {pending} 笔，按原方向暂计收支")

    def load_transactions(self, db):
        if self.chart_detail is not None:
            self.load_chart_transactions(db)
            return
        get = lambda key: self.get("transactions", key)
        kinds = {label: key for key, label in KINDS.items()}
        args = Namespace(**self.scope("transactions"), kind=kinds.get(get("kind")),
                         search=get("search") or None, amount=minor(get("amount")) if get("amount") else None)
        rows = ledger.transactions(db, args, include_own_category=True)
        hide_refunds = self.forms["transactions"]["hide_refunds"].get()
        if self.forms["transactions"]["hide_reimbursements"].get():
            reimbursed_ids = {
                event_id for link in db.execute("""
                    SELECT first_id, second_id FROM links
                    WHERE relation = 'reimbursement' AND status = 'confirmed'
                """) for event_id in link
            }
            rows = [row for row in rows if row["kind"] != "reimbursement" and row["id"] not in reimbursed_ids]
        refunded_ids = set()
        if hide_refunds:
            links = [link for link in report_adjustments(db) if link["relation"] == "refund"]
            refund_receipts = {link["first_id"] for link in links}
            refunded_ids = {link["second_id"] for link in links}
            rows = [row for row in rows if row["kind"] != "refund" and row["id"] not in refund_receipts]
        rows_by_id = {row["id"]: row for row in rows}
        totals = defaultdict(Counter)
        actions = {"refund": "pure" if hide_refunds else "net", "reimbursement": "net"}
        for item in entries(db, actions):
            row = rows_by_id.get(item.event_id)
            if row is None:
                continue
            totals[item.currency][item.bucket] += item.amount
            if item.event_id in refunded_ids:
                row["amount"] = decimal_text(item.amount)
                row["refund_remainder"] = True
        rows = [row for row in rows if row["id"] not in refunded_ids or row.get("refund_remainder")]
        for row in rows:
            totals.setdefault(row["currency"], Counter())
        book = self.rate_book() if any(currency != "CNY" for currency in totals) else None
        captions, note = transaction_totals_text(totals, book)
        if note:
            captions.append(note)
        summary_title = "当前明细 · 退款后支出" if hide_refunds else "当前明细 · 净收支口径"
        self.transaction_totals.set(summary_title + "\n" + "\n".join(captions)
                                    if captions else "当前筛选没有交易")
        self.show_records("transactions", rows)
        self.status.set(f"{len(rows)} 笔交易")

    def load_chart_transactions(self, db):
        scope = self.chart_detail
        actions = {"refund": scope.refunds or scope.basis, "reimbursement": scope.reimbursements or scope.basis}
        contributions = Counter()
        for item in filtered_entries(db, actions, start=scope.start, end=scope.end, currency=scope.currency,
                                     side=scope.side, categories=scope.categories):
            contributions[item.event_id] += item.report_amount
        args = Namespace(start=scope.start, end=scope.end, currency=scope.currency,
                         kind=None, category=None, amount=None, search=None)
        rows = [row for row in ledger.transactions(db, args, include_own_category=True)
                if row["id"] in contributions]
        for row in rows:
            row["chart_amount"] = decimal_text(contributions[row["id"]])
        side = DIRECTIONS[scope.side]
        category = "、".join(scope.categories) if scope.categories is not None else "全部分类"
        period = scope.start if scope.start == scope.end else f"{scope.start} 至 {scope.end}"
        basis = next(label for label, key in BASIS.items() if key == scope.basis)
        adjustment_labels = {key: label for label, key in ADJUSTMENTS.items()}
        modes = [basis]
        if scope.refunds:
            modes.append(f"退款{adjustment_labels[scope.refunds]}")
        if scope.reimbursements:
            modes.append(f"报销{adjustment_labels[scope.reimbursements]}")
        self.chart_detail_text.set(f"图表筛选  ·  {period}  ·  {scope.currency} {side}  ·  {' / '.join(modes)}\n{category}")
        total = Decimal(decimal_text(sum(contributions.values())))
        self.transaction_totals.set(f"计入图表合计  {total:,.2f} {scope.currency}")
        self.show_records("transactions", rows, chart_amounts=True)
        self.status.set(f"{len(rows)} 笔交易")

    def load_charts(self, db):
        get = lambda key: self.get("charts", key)
        period = "month" if get("period") == "按月" else "day"
        side = "expense" if get("side") == "支出" else "income"
        args = Namespace(**self.scope("charts"), by="category", side=side, basis=BASIS[get("basis")],
                         refunds=ADJUSTMENTS[get("refunds")], reimbursements=ADJUSTMENTS[get("reimbursements")])
        categories = ledger.report(db, args)
        args.by = period
        periods = ledger.report(db, args)
        bounds = dict(db.execute("SELECT key, value FROM metadata WHERE key IN ('start', 'end')"))
        self.hide_chart_tooltip()
        note = self.charts.draw(categories, periods, side=side, period=period, currency=args.currency)
        self.chart_scope = ChartScope(max(args.start, bounds["start"]), min(args.end, bounds["end"]), args.currency,
                                      side, (args.category,) if args.category else None,
                                      args.basis, args.refunds, args.reimbursements)
        self.chart_note.set(f"悬停查看金额，点击查看交易明细。 {note}".strip())
        self.chart_canvas.draw_idle()
        self.status.set(f"{len(categories)} 个分类  ·  {len(periods)} 个{'月' if period == 'month' else '日期'}")

    def load_review(self, db):
        get = lambda key: self.get("review", key)
        kind = REVIEW_TYPES[get("type")]
        if kind == "internal_transfer":
            allowed = ("已确认", "手动标记")
        elif kind == "unassociated":
            allowed = ("已确认",)
        elif kind in {"reimbursement", "refund"}:
            allowed = ("候选", "已确认", "未分摊")
        else:
            allowed = ("候选", "已确认")
        self.review_status.configure(state="disabled" if kind in {"pending", "unassociated"} else "readonly",
                                     values=allowed)
        if get("status") not in allowed:
            self.forms["review"]["status"].set(allowed[0])
        status = STATUSES[get("status")]
        event_id = get("event")
        if status == "marked":
            rows = ledger.transactions(db, Namespace(kind="internal", start=None, end=None, currency=None,
                                                      category="账户划转", amount=None, search=None))
            rows = [r for r in rows if r["rule"] == "人工分类" and (not event_id or r["id"] == event_id)]
        else:
            rows = ledger.review(db, Namespace(type=kind, status=status, event=event_id or None))
        paired = kind not in {"pending", "unassociated"} and status not in {"unlinked", "marked"}
        merged = kind == "duplicate" and status == "confirmed"
        self.review_first.configure(text="查看合并流水" if merged else "查看左侧流水" if paired else "查看原始流水")
        self.review_second.configure(text="查看右侧流水", state="normal" if paired and not merged else "disabled")
        confirm_labels = {"pending": "确认交易性质", "duplicate": "确认同一笔交易",
                          "refund": "确认所选关联", "reimbursement": "确认所选关联"}
        can_confirm = kind in confirm_labels and status == "candidate" and bool(rows)
        self.review_confirm.configure(text=confirm_labels.get(kind, "已确认"),
                                      state="normal" if can_confirm else "disabled")
        if kind == "duplicate" and status == "candidate":
            self.review_independent.configure(state="normal" if rows else "disabled")
            self.review_independent.pack(side="left", padx=(0, 8), after=self.review_confirm)
        else:
            self.review_independent.pack_forget()
        if kind in {"pending", "refund", "reimbursement"}:
            self.review_unassociated.configure(state="normal" if rows else "disabled")
            self.review_unassociated.pack(side="left", padx=(0, 8), after=self.review_confirm)
        else:
            self.review_unassociated.pack_forget()
        if not paired:
            self.show_records("review", rows, unlinked=status == "unlinked")
        else:
            first, second = {
                "reimbursement": ("回款", "支出"), "refund": ("退款", "原支出"),
                "duplicate": ("银行", "平台"), "internal_transfer": ("银行入账", "钱包提现"),
            }[kind]
            columns = [Column("first_day", f"{first}日期", 104), Column("first_display", f"{first}对方", 126),
                       Column("first_amount", f"{first}金额", 104, "money"),
                       Column("second_day", f"{second}日期", 104), Column("second_display", f"{second}对方", 126),
                       Column("second_amount", f"{second}金额", 104, "money"),
                       Column("currency", "币种", 54), Column("amount", "分摊金额" if kind in {"refund", "reimbursement"}
                                                            else "金额", 96, "money"),
                       Column("evidence", "匹配依据", 220)]
            for row in rows:
                for key in ("first", "second"):
                    party = row[f"{key}_party"] or row[f"{key}_description"] or SOURCES.get(row[f"{key}_source"])
                    row[f"{key}_display"] = party
                    row[f"{key}_amount"] = decimal_text(row[f"{key}_amount_minor"])
            self.tables["review"].fill(columns, rows)
        unit = "组关联" if paired else "笔交易"
        self.status.set(f"{len(rows)} {unit}")

    def show_records(self, page, rows, *, unlinked=False, chart_amounts=False):
        columns = [Column("stamp", "时间" if page == "transactions" else "日期", 160),
                   Column("source_label", "来源", 70), Column("type_label", "类型", 76),
                   Column("category", "分类", 132), Column("currency", "币种", 58),
                   Column("amount", "未分摊金额" if unlinked else "流水金额" if chart_amounts else "金额", 100, "money")]
        if chart_amounts:
            columns.append(Column("chart_amount", "计入图表金额", 118, "money"))
        columns.extend([Column("counterparty", "交易对方", 158), Column("note", "说明", 208)])
        for row in rows:
            row["stamp"] = (row.get("occurred_at") or row["day"]).replace("T", " ")
            row["source_label"] = SOURCES.get(row["source"], row["source"])
            row["type_label"] = KINDS.get(row.get("kind")) or DIRECTIONS.get(row.get("direction"))
            if unlinked:
                row["type_label"] = self.get("review", "type")
            elif row.get("kind") == "pending" or (page == "review" and "kind" not in row):
                side = PENDING_DIRECTIONS.get(row["direction"])
                row["type_label"] = "待复核" + KINDS.get(side, "")
            elif row.get("refund_remainder"):
                row["type_label"] = "差额支出"
            row["note"] = row["description"] or row.get("rule") or row.get("source_type")
            if row.get("review_note"):
                row["note"] = f'{row["note"]}  ·  复核备注：{row["review_note"]}'
        self.tables[page].fill(columns, rows)

    def double_click(self, event, page):
        if event.widget.identify_row(event.y):
            self.show_selected(page, "first_id" if page == "review" else "id")

    def show_selected(self, page, key="id"):
        row = self.tables[page].selected()
        event_id = (row.get(key) or row.get("id")) if row else None
        if not event_id:
            self.status.set("请先选择一条有对应流水的记录。")
            return
        try:
            with closing(ledger.open_db(self.database)) as db:
                details = ledger.trace(db, event_id)
        except (ValueError, sqlite3.Error) as exc:
            messagebox.showerror("读取流水失败", str(exc), parent=self.root)
            return
        event = details["event"]
        window = tk.Toplevel(self.root)
        window.title("原始流水")
        window.geometry("780x620")
        panel = ttk.Frame(window, padding=14)
        panel.pack(fill="both", expand=True)
        ttk.Label(panel, text=f"{event['day']}  ·  {event['currency']} {decimal_text(event['amount_minor'])}  ·  "
                              f"{KINDS.get(event['kind'], event['kind'])} / {event['category']}").pack(anchor="w")
        ttk.Label(panel, text=f"交易编号  {event_id}", style="Muted.TLabel").pack(anchor="w", pady=5)
        ttk.Button(panel, text="复制交易编号", command=lambda: self.copy_id(event_id)).pack(anchor="w", pady=(0, 8))
        body = ScrolledText(panel, wrap="word", relief="flat", padx=10, pady=10, font="TkTextFont")
        body.pack(fill="both", expand=True)
        if details["unassociated"]:
            body.insert("end", "关联状态  已确认无关联\n")
        if details["review_note"]:
            body.insert("end", f"复核备注  {details['review_note']}\n")
        body.insert("end", f"处理依据  {event['rule']}\n")
        for source in details["sources"]:
            body.insert("end", f"\n{source['path']}  第 {source['line_no']} 行\n")
            if source["match_rule"]:
                body.insert("end", source["match_rule"] + "\n")
            for key, value in source["raw"].items():
                if value:
                    body.insert("end", f"{key}  {value}\n")
        if details["links"]:
            body.insert("end", "\n关联记录\n")
            for link in details["links"]:
                other = link["second_id"] if event_id == link["first_id"] else link["first_id"]
                body.insert("end", f"{other}  {decimal_text(link['amount_minor'])}  {link['evidence']}\n")
        body.configure(state="disabled")

    def copy_id(self, event_id):
        self.root.clipboard_clear()
        self.root.clipboard_append(event_id)
        self.status.set("交易编号已复制")

    def edit_transaction(self):
        rows = self.tables["transactions"].selected_rows()
        if not rows:
            self.status.set("请选择要编辑的交易。")
            return
        if len(rows) == 1:
            self.edit_single_transaction(rows[0])
        else:
            self.batch_edit_transactions(rows)

    def edit_single_transaction(self, row):
        dialog = EditTransactionDialog(self.root, row, self.categories[1:])
        if not dialog.result:
            return
        try:
            with closing(ledger.open_db(self.database)) as db:
                conflicts = conflicting_edit_links(db, row["id"], dialog.result["kind"])
        except (OSError, sqlite3.Error) as exc:
            messagebox.showerror("读取关联失败", str(exc), parent=self.root)
            return
        if conflicts and not messagebox.askyesno(
                "确认修改交易", "新类型与已确认的退款或报销关联冲突。解除关联并保存修改？", parent=self.root):
            return
        if not self.save_review_change(lambda: save_transaction_edit(
                self.database, row["id"], dialog.result, allow_unlink=bool(conflicts))):
            return
        self.reload_categories()
        self.refresh("transactions", preserve=True, use_applied=True)
        self.status.set("交易信息已保存。")

    def batch_edit_transactions(self, rows):
        dialog = BatchEditTransactionDialog(self.root, rows, self.categories[1:])
        if not dialog.result:
            return
        edits = {row["id"]: dict(dialog.result) for row in rows}
        try:
            with closing(ledger.open_db(self.database)) as db:
                conflicts = sum(
                    1 for row in rows
                    if conflicting_edit_links(db, row["id"], edits[row["id"]].get("kind", row["kind"]))
                )
        except (OSError, sqlite3.Error) as exc:
            messagebox.showerror("读取关联失败", str(exc), parent=self.root)
            return
        if conflicts and not messagebox.askyesno(
                "确认批量修改", f"{conflicts} 笔交易的新类型与已确认的退款或报销关联冲突。"
                                "解除关联并保存修改？", parent=self.root):
            return
        if not self.save_review_change(lambda: save_transaction_edits(
                self.database, edits, allow_unlink=bool(conflicts))):
            return
        self.reload_categories()
        self.refresh("transactions", preserve=True, use_applied=True)
        self.status.set(f"已批量更新 {len(edits)} 笔交易。")

    def reload_categories(self):
        with closing(ledger.open_db(self.database)) as db:
            categories = [r[0] for r in db.execute("SELECT DISTINCT category FROM events ORDER BY category")]
        self.categories = [ALL, *categories]
        for widget in self.category_widgets:
            widget.configure(values=self.categories)

    def apply_tolerance(self):
        self.root.configure(cursor="watch")
        self.root.update_idletasks()
        try:
            threshold = ledger.parse_reimbursement_tolerance(self.get("review", "tolerance"))
            with closing(ledger.open_db(self.database)) as db:
                meta = dict(db.execute("SELECT key, value FROM metadata"))
            decisions = json.loads(meta["decisions"])
            if threshold != ledger.configured_reimbursement_tolerance(decisions):
                decisions[ledger.REIMBURSEMENT_TOLERANCE_KEY] = str(threshold)
                rebuild_with_decisions(self.database, meta, decisions)
        except (ValueError, KeyError, ArithmeticError, OSError, sqlite3.Error) as exc:
            messagebox.showerror("阈值设置失败", str(exc), parent=self.root)
            return
        finally:
            self.root.configure(cursor="")
        self.forms["review"]["tolerance"].set(str(threshold))
        self.refresh("review")
        self.status.set(f"报销差额上限 {threshold}%  ·  {self.status.get()}")

    def save_review_change(self, action):
        self.root.configure(cursor="watch")
        self.root.update_idletasks()
        try:
            action()
        except (ValueError, KeyError, ArithmeticError, OSError, sqlite3.Error) as exc:
            messagebox.showerror("复核结果保存失败", str(exc), parent=self.root)
            return False
        finally:
            self.root.configure(cursor="")
        return True

    def refresh_review_in_place(self, message):
        table = self.tables["review"]
        sort_order = table.sort_order
        horizontal, vertical = table.tree.xview()[0], table.tree.yview()[0]
        self.refresh("review")
        if sort_order:
            table.sort(*sort_order)
        table.tree.xview_moveto(horizontal)
        table.tree.yview_moveto(vertical)
        table.tree.focus_set()
        self.status.set(message)

    def confirm_review(self):
        kind = REVIEW_TYPES[self.get("review", "type")]
        if kind == "duplicate":
            self.resolve_duplicates(merge=True)
        elif kind == "pending":
            rows = self.tables["review"].selected_rows()
            if not rows:
                self.status.set("请选择需要确认性质的交易。")
                return
            if any(row["category"] == "跨渠道重复待确认" for row in rows):
                self.status.set("所选流水有重复候选，请先在「跨渠道重复」中复核。")
                return
            dialog = ClassificationDialog(self.root, rows, self.categories[1:])
            if dialog.result and self.save_review_change(
                    lambda: save_pending_classifications(self.database, dialog.result)):
                self.refresh_review_in_place(f"已确认 {len(dialog.result)} 笔交易的性质。")
        else:
            self.allocate(True)

    def resolve_duplicates(self, *, merge):
        rows = self.tables["review"].selected_rows()
        if not rows or any(row.get("relation") != "duplicate" or row.get("status") != "candidate" for row in rows):
            self.status.set("请选择需要复核的跨渠道重复候选。")
            return
        count = len({row["first_id"] for row in rows})
        prompt = (f"将所选 {count} 笔银行流水与对应的平台流水合并，只计一次收支？" if merge
                  else f"将所选 {count} 笔银行流水保留为独立交易？")
        if messagebox.askyesno("确认复核结果", prompt, parent=self.root) and self.save_review_change(
                lambda: save_duplicate_review(self.database, rows, merge=merge)):
            result = "已确认同一笔交易" if merge else "已保留为独立交易"
            self.refresh_review_in_place(f"{result}  {count} 笔。")

    def mark_unassociated(self, page):
        rows = self.tables[page].selected_rows()
        if not rows:
            self.status.set("请选择需要标记为无关联的交易。")
            return
        if "first_id" in rows[0]:
            dialog = UnassociatedSideDialog(self.root, "确认无关联")
            if not dialog.result:
                return
            event_ids = {row[key] for row in rows for key in dialog.result}
        else:
            event_ids = {row["id"] for row in rows}
        try:
            with closing(ledger.open_db(self.database)) as db:
                existing = confirmed_adjustments(db, event_ids)
        except (OSError, sqlite3.Error) as exc:
            messagebox.showerror("读取关联失败", str(exc), parent=self.root)
            return
        count = len(event_ids)
        prompt = (f"解除涉及所选交易的 {len(existing)} 组已确认关联，"
                  f"并将所选 {count} 笔标记为无关联？" if existing
                  else f"将所选 {count} 笔交易标记为无关联？")
        if not messagebox.askyesno("确认无关联", prompt, parent=self.root):
            return
        if self.save_review_change(lambda: save_unassociated(
                self.database, event_ids, allow_unlink=bool(existing))):
            message = f"已确认 {count} 笔无关联。"
            if page == "review":
                self.refresh_review_in_place(message)
            else:
                self.status.set(message)

    def allocate(self, from_selection):
        table = self.tables["review"]
        rows = table.selected_rows() if from_selection else []
        if from_selection and (not rows or any(
                row.get("relation") not in {"refund", "reimbursement"} or row.get("status") != "candidate"
                for row in rows)):
            self.status.set("请选择退款或报销候选，或使用手动关联。")
            return
        dialog = (BatchAllocationDialog(self.root, rows) if len(rows) > 1
                  else AllocationDialog(self.root, rows[0] if rows else None))
        if not dialog.result:
            return
        allocations = dialog.result
        if not self.save_review_change(lambda: save_allocations(self.database, allocations)):
            return
        message = f"已确认 {len(allocations)} 组关联。"
        if from_selection:
            self.refresh_review_in_place(message)
            return
        relation, receipt, _, _ = allocations[0]
        self.forms["review"]["type"].set("退款" if relation == "refund" else "报销")
        self.forms["review"]["status"].set("已确认")
        self.forms["review"]["event"].set(receipt)
        self.refresh("review")
        item = next((item for item, row in table.records.items() if row.get("first_id") == receipt), None)
        if item is not None:
            table.tree.selection_set(item)
            table.tree.focus(item)
            table.tree.see(item)
        self.status.set(message)

    def export_decisions(self):
        path = filedialog.asksaveasfilename(parent=self.root, title="导出复核规则",
                                            defaultextension=".json", initialfile="复核规则.json",
                                            filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            target = Path(path)
            if target.suffix.lower() != ".json" or target.resolve() == self.database:
                raise ValueError("请将规则保存为单独的 JSON 文件。")
            with closing(ledger.open_db(self.database)) as db:
                raw = db.execute("SELECT value FROM metadata WHERE key = 'decisions'").fetchone()[0]
            target.write_text(json.dumps(json.loads(raw), ensure_ascii=False, indent=2), encoding="utf-8")
        except (ValueError, OSError, sqlite3.Error) as exc:
            messagebox.showerror("导出失败", str(exc), parent=self.root)
            return
        self.status.set("复核规则已导出")


def main():
    parser = argparse.ArgumentParser(description="个人收支账本桌面界面")
    parser.add_argument("--db", type=Path, default=ledger.DATABASE, help="SQLite 账本路径")
    args = parser.parse_args()
    root = tk.Tk()
    try:
        LedgerWindow(root, args.db.resolve())
    except (ValueError, KeyError, OSError, sqlite3.Error) as exc:
        root.withdraw()
        messagebox.showerror("无法打开账本", str(exc), parent=root)
        root.destroy()
        return
    root.mainloop()


if __name__ == "__main__":
    main()
