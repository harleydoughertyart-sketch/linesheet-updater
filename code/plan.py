"""Compare what the line sheet prints against what the workbook says.

This is what the review screen renders and what the rewrite executes. Two rules
shape it, both from decisions on the map:

- **Nothing is dropped silently.** Every style on either side appears with a
  status. A stale figure left on a sales document is worse than an obvious gap.
- **Matching is exact, never fuzzy.** A fuzzy match of ``LX2041`` to ``LX2042``
  yields a plausible wrong number, which is the worst outcome this tool can have.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum

from .linesheet import Printed
from .sales import SalesRow


class Status(Enum):
    """Why a style needs the reviewer's attention, or doesn't."""

    NOT_IN_SPREADSHEET = "not-in-spreadsheet"
    CHANGED = "changed"
    NOT_IN_LINE_SHEET = "not-in-line-sheet"
    UNCHANGED = "unchanged"


#: Reviewers should not have to hunt. Blocking items first, then edits, then the
#: untouched majority, then the merely informational.
_ORDER = {
    Status.NOT_IN_SPREADSHEET: 0,
    Status.CHANGED: 1,
    Status.UNCHANGED: 2,
    Status.NOT_IN_LINE_SHEET: 3,
}


def format_rank(row_num: int, qty: int | None) -> str | None:
    """The house format for the upper blue value.

    ``None`` only when there is no quantity at all to format — the line sheet
    side can be absent, the workbook side never is (an empty cell there reads as
    zero; see :func:`linesheet.sales._whole`).
    """
    if qty is None:
        return None
    return f"{row_num}/{qty}"


def format_sales(value: float | None) -> str | None:
    """The house format for the lower blue value: no cents, comma thousands.

    Rounds half *up*, the way money is rounded on an invoice. Python's built-in
    round() is half-to-even, which sends $1,234.50 to $1,234 and $12,578.50 to
    $12,578 — a pound short, on a figure a buyer reads.
    """
    if value is None:
        return None
    whole = Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return f"${whole:,}"


@dataclass(frozen=True)
class Change:
    """One style's row on the review screen."""

    style: str
    status: Status
    printed_rank: str | None
    correct_rank: str | None
    printed_sales: str | None
    correct_sales: str | None
    page: int | None

    @property
    def needs_write(self) -> bool:
        """Whether the rewrite should touch this style's cell."""
        return self.status is Status.CHANGED

    @property
    def blocks_confirm(self) -> bool:
        """Whether the reviewer must acknowledge this before confirming.

        Only a style printed on the sheet but absent from the workbook blocks:
        its stale figure stays on the page unless a human rules on it. A style
        that sold but was never pictured has nothing on the page to be wrong.
        """
        return self.status is Status.NOT_IN_SPREADSHEET


def plan_updates(
    sales: dict[str, SalesRow], printed: dict[str, Printed]
) -> list[Change]:
    """Every style on either side, with what should happen to it."""
    changes: list[Change] = []

    for style in sorted(set(printed) | set(sales)):
        on_sheet = printed.get(style)
        in_book = sales.get(style)

        shown_rank = format_rank(on_sheet.row_num, on_sheet.qty) if on_sheet else None
        shown_sales = format_sales(on_sheet.sales) if on_sheet else None
        right_rank = format_rank(in_book.row_num, in_book.qty) if in_book else None
        right_sales = format_sales(in_book.sales) if in_book else None

        # A figure the sheet never printed has no "before". Showing "$0" would
        # imply the sheet claims zero sales; it claims nothing. The Spring 2026
        # sheet omits sales entirely — an oversight, so the figure gets added.
        # A figure the sheet never printed has no "before"; the correction is an
        # addition. Where the sheet has no box at all, both sides are suppressed:
        # there is nowhere on the page to put a figure, so reporting a change
        # would offer a correction the writer cannot carry out.
        #
        # A sales line the sheet prints but that is not a figure — "$3.445",
        # "$9,0721" — is shown exactly as printed. It is somebody's typing, not a
        # gap, and settling it as "nothing to do" leaves a wrong number on a sales
        # document and says nothing about it. The box is known, so the difference
        # from the workbook goes on the screen and the rewrite replaces it.
        if on_sheet is not None and on_sheet.sales_misprint is not None:
            shown_sales = on_sheet.sales_misprint
        elif on_sheet is not None and on_sheet.sales_added:
            shown_sales = None
        elif on_sheet is not None and on_sheet.sales_box is None:
            shown_sales = right_sales = None
        if on_sheet is not None and on_sheet.rank_box is None:
            shown_rank = right_rank = None

        if on_sheet is None:
            status = Status.NOT_IN_LINE_SHEET
        elif in_book is None:
            status = Status.NOT_IN_SPREADSHEET
        elif (shown_rank, shown_sales) == (right_rank, right_sales):
            status = Status.UNCHANGED
        else:
            status = Status.CHANGED

        changes.append(
            Change(
                style=style,
                status=status,
                printed_rank=shown_rank,
                correct_rank=right_rank,
                printed_sales=shown_sales,
                correct_sales=right_sales,
                page=on_sheet.page if on_sheet else None,
            )
        )

    changes.sort(key=lambda c: (_ORDER[c.status], c.style))
    return changes
