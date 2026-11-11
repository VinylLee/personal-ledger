"""Charts drawn from the same report rows as the ledger's tables."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import matplotlib.dates as mdates
from matplotlib.figure import Figure
from matplotlib.font_manager import FontProperties
from matplotlib.ticker import MaxNLocator, StrMethodFormatter

COLORS = ("#0E7490", "#14B8A6", "#3B82F6", "#8B5CF6", "#F59E0B", "#F97316", "#84CC16", "#94A3B8")


class LedgerCharts:
    def __init__(self):
        self.figure = Figure(figsize=(11, 4.4), dpi=100, layout="constrained", facecolor="#FFFFFF")
        grid = self.figure.add_gridspec(1, 3, width_ratios=(1.0, 0.9, 1.6))
        self.pie = self.figure.add_subplot(grid[0, 0])
        self.legend = self.figure.add_subplot(grid[0, 1])
        self.bars = self.figure.add_subplot(grid[0, 2])
        candidates = [Path("C:/Windows/Fonts/msyh.ttc"), Path("C:/Windows/Fonts/simhei.ttf")]
        path = next((p for p in candidates if p.exists()), None)
        self.font = FontProperties(fname=str(path)) if path else FontProperties()

    def draw(self, category_rows, period_rows, *, side, period, currency):
        self.pie.clear()
        self.legend.clear()
        self.bars.clear()
        self.legend.axis("off")
        field = "net_expense" if side == "expense" else "income"
        label = "支出" if side == "expense" else "收入"
        positive = sorted(((row["category"], Decimal(row[field])) for row in category_rows
                           if Decimal(row[field]) > 0), key=lambda item: (-item[1], item[0]))
        negative = [(row["category"], Decimal(row[field])) for row in category_rows if Decimal(row[field]) < 0]
        note = ""
        if negative:
            total = sum(amount for _, amount in negative)
            note = f"分类饼图展示正金额。另有 {len(negative)} 个负金额分类，合计 {total:,.2f} {currency}。"
        if len(positive) > 7:
            rest = positive[7:]
            positive = positive[:7] + [(f"其他（{len(rest)} 类）", sum(amount for _, amount in rest))]
            note += " 饼图合并了较小分类，完整分类可在收支报表查看。"
        self.pie.set_title(f"分类构成 · {label}", fontproperties=self.font, fontsize=12, color="#17334C")
        if positive:
            wedges, _ = self.pie.pie([float(amount) for _, amount in positive], colors=COLORS,
                                      startangle=90, counterclock=False,
                                      wedgeprops={"width": 0.52, "edgecolor": "white"})
            total = sum(amount for _, amount in positive)
            labels = [f"{name}\n{amount:,.2f}  ·  {amount / total * 100:.1f}%" for name, amount in positive]
            legend_font = self.font.copy()
            legend_font.set_size(9)
            self.legend.legend(wedges, labels, loc="center left", frameon=False,
                               prop=legend_font)
        else:
            self.pie.text(0.5, 0.5, "没有正金额", transform=self.pie.transAxes,
                          ha="center", va="center", fontproperties=self.font, color="#64748B")

        self.bars.set_title(f"{'每月' if period == 'month' else '每日'}{label} · {currency}",
                            fontproperties=self.font, fontsize=12, color="#17334C")
        if period_rows:
            dates = [date.fromisoformat(row["period"] + "-01" if period == "month" else row["period"])
                     for row in period_rows]
            values = [float(Decimal(row[field])) for row in period_rows]
            self.bars.bar(dates, values, width=20 if period == "month" else 0.8,
                          color=["#0E7490" if value >= 0 else "#D97706" for value in values], zorder=3)
            locator = mdates.AutoDateLocator(minticks=3, maxticks=7)
            self.bars.xaxis.set_major_locator(locator)
            if len(dates) == 1:
                self.bars.set_xticks(dates)
            date_format = "%Y-%m" if period == "month" else "%m-%d"
            if period == "day" and dates[0].year != dates[-1].year:
                date_format = "%Y-%m-%d"
            self.bars.xaxis.set_major_formatter(mdates.DateFormatter(date_format))
            self.bars.yaxis.set_major_locator(MaxNLocator(nbins=5))
            self.bars.yaxis.set_major_formatter(StrMethodFormatter("{x:,.0f}"))
            self.bars.axhline(0, color="#94A3B8", linewidth=0.8)
            self.bars.grid(axis="y", color="#E2E8F0", linewidth=0.7, zorder=0)
            self.bars.tick_params(axis="x", labelrotation=35, labelsize=8)
            self.bars.tick_params(axis="y", labelsize=8)
        else:
            self.bars.text(0.5, 0.5, "当前筛选没有记录", transform=self.bars.transAxes,
                           ha="center", va="center", fontproperties=self.font, color="#64748B")
            self.bars.set_xticks([])
            self.bars.set_yticks([])
        for spine in self.bars.spines.values():
            spine.set_visible(False)
        return note.strip()
