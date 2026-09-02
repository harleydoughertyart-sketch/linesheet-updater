"""Write the corrected figures back into the line sheet.

The output is a working document, not a render: the business workflow is "edit in
Acrobat", so what comes out has to be an ordinary PDF someone opens next.

Two rules, both from decisions recorded on the map:

- **Only approved styles are touched.** A reviewer confirms each one; nothing else
  is written, including styles that merely happen to differ.
- **A style missing from the workbook is never written**, even if approved. There
  is no correct figure to write, and blanking it would destroy information.

The old figures are covered and the new ones drawn at the same position, in the
same blue, flush right — the sheet sets the two lines of a cell on a common right
edge, and anchoring a replacement at its start would break that edge whenever the
new string is shorter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping

import fitz

from .linesheet import Printed, is_unidentified
from .plan import Change

#: The blue the line sheet prints its figures in.
BLUE = (0x3E / 255, 0x6F / 255, 0xB7 / 255)
#: The sheet sets these in 12pt Arial. Helvetica is the metric-compatible base-14
#: stand-in, so nothing has to be embedded.
FONT = "helv"
FONT_SIZE = 12
#: Where the baseline sits inside a figure's box, as a fraction of its height up
#: from the bottom. Text boxes include the descender space the digits don't use.
BASELINE_LIFT = 0.22

#: How the corrected file is saved. All three are lossless: none of them touch
#: image resolution. A line sheet is a print document whose photographs are the
#: reason it exists, so downsampling them to save megabytes is deliberately not
#: on offer — only the PDF's own housekeeping is.
SAVE_PROFILES: dict[str, dict] = {
    # Nearest to the file that came in: no stream recompression, no tidying.
    "print": dict(garbage=1, deflate=False),
    # Drops objects nothing points at and compresses streams. The default.
    "balanced": dict(garbage=3, deflate=True),
    # Everything lossless the format allows, including image and font streams.
    "small": dict(garbage=4, deflate=True, deflate_images=True,
                  deflate_fonts=True, clean=True),
}
DEFAULT_PROFILE = "balanced"


@dataclass
class Report:
    """What the write actually did."""

    written: int = 0
    skipped: int = 0
    refused: list[str] = field(default_factory=list)
    touched: list[str] = field(default_factory=list)
    #: styles where a figure the reviewer typed was used instead of the
    #: workbook's, so the summary can say so rather than imply the file decided
    edited: list[str] = field(default_factory=list)
    #: bytes on disk, before and after
    size: int = 0
    original_size: int = 0
    #: what each touched figure should now say, ``{style: {"rank": "3/412"}}`` —
    #: the input to :func:`verify_output`
    expected: dict[str, dict[str, str]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.refused


@dataclass
class Check:
    """The result of reading the written file back."""

    verified: int = 0
    #: styles whose figures did not read back as intended
    wrong: list[str] = field(default_factory=list)
    #: styles that could not be found at all in the written file
    missing: list[str] = field(default_factory=list)
    pages_before: int = 0
    pages_after: int = 0

    @property
    def ok(self) -> bool:
        return (not self.wrong and not self.missing
                and self.pages_before == self.pages_after)


def verify_output(source: str | Path, out_path: str | Path,
                  expected: Mapping[str, Mapping[str, str]]) -> Check:
    """Read the written file back and check it says what it was told to say.

    Every failure this tool has had produced a *plausible wrong number* rather
    than an error — a figure claimed by the wrong cell, a digit eaten by a
    control character. The only way to be sure the file on disk is right is to
    read it with the same decoder that read the original and compare. So that is
    what happens after every write, and what the receipt reports.
    """
    from .linesheet import read_line_sheet   # imported here to avoid a cycle
    from .plan import format_rank, format_sales

    check = Check()
    with fitz.open(source) as before, fitz.open(out_path) as after:
        check.pages_before, check.pages_after = before.page_count, after.page_count

    printed = read_line_sheet(out_path, known_styles=set(expected))
    for style, figures in expected.items():
        shown = printed.get(style)
        if shown is None:
            check.missing.append(style)
            continue
        # Only compare a figure the written file actually carries. Reading a
        # missing rank as "0/0" or a missing sales line as "$0" and comparing
        # *that* would let a write that lost the figure pass as verified, which
        # is the one thing this function exists to catch.
        got: dict[str, str | None] = {
            "rank": (format_rank(shown.row_num, shown.qty)
                     if shown.rank_box is not None else None),
            "sales": format_sales(shown.sales) if shown.sales_box is not None else None,
        }
        if all(got.get(key) is not None and got[key] == want
               for key, want in figures.items()):
            check.verified += 1
        else:
            check.wrong.append(style)
    return check


def _text_lines(page: fitz.Page) -> list[fitz.Rect]:
    """Every line of text on the page, boxed in display space."""
    return [fitz.Rect(line["bbox"]) * page.rotation_matrix
            for block in page.get_text("dict")["blocks"]
            for line in block.get("lines", [])]


def _redaction(page: fitz.Page, box: tuple[float, float, float, float],
               lines: list[fitz.Rect]) -> fitz.Rect:
    """The rectangle that covers an old figure, held clear of its neighbours.

    A text box is the font's line box, not the ink: it carries the room a glyph
    may need and not the room this one takes. One subset on this sheet claims a
    third of a point more of it than the rest, and a caption set on its side
    claims two points of margin beside the money it sits next to. Redaction goes
    by box and removes a whole character for any part of it the box touches, so
    those points cost real text: the first corrected sheet printed "L2443 w/o
    Lurex" as "o Lurex", and lost "Mock Neck" off a description — a customer's
    document damaged to correct the figure beside it.

    So a rectangle retreats from any line it merely grazes, by whichever edge
    costs least, and gives up padding rather than ink. Two boxes that genuinely
    lie on top of each other — a figure and the line it is part of, above all —
    offer no cheap retreat at all, and are left alone: that is what the half-way
    limit says. Should a retreat ever cut ink, the old digits would survive under
    the new ones, which is precisely what reading the file back catches.
    """
    target = fitz.Rect(*box) * page.rotation_matrix
    for other in lines:
        if not target.intersects(other):
            continue
        across, down = (target.x0 + target.x1) / 2, (target.y0 + target.y1) / 2
        retreats = []
        if other.x1 <= across:
            retreats.append((other.x1 - target.x0, "x0", other.x1))
        if other.x0 >= across:
            retreats.append((target.x1 - other.x0, "x1", other.x0))
        if other.y1 <= down:
            retreats.append((other.y1 - target.y0, "y0", other.y1))
        if other.y0 >= down:
            retreats.append((target.y1 - other.y0, "y1", other.y0))
        if retreats:
            _, edge, to = min(retreats)
            setattr(target, edge, to)
    return target * page.derotation_matrix


@dataclass(frozen=True)
class Edit:
    """One figure to replace: where it sits, what it should say, and any nudge."""

    box: tuple[float, float, float, float]
    text: str
    nudge: tuple[float, float] = (0.0, 0.0)


def redraw(page: fitz.Page, edits: Iterable[Edit], erase_only: bool = False) -> None:
    """Replace figures on one page: cover each old one, draw each new one.

    The one place this is done, so the review screen's preview and the file that
    gets written cannot drift apart. They had. The preview kept its own copy and
    covered a figure by its raw box, where this retreats from any neighbouring
    line it merely grazes — so a card showed "WHSL $99" losing its "WHSL $" to a
    correction that leaves it untouched on disk, and a reviewer who believes the
    preview turns down a correction that was never going to do any harm.

    The page's other text is read before anything is removed from it, every
    redaction is applied at once, and only then is anything drawn: applying a
    redaction after drawing erases what was drawn.

    ``erase_only`` stops after the covering, for the preview crop the screen
    draws its own replacement on top of.
    """
    edits = list(edits)
    if not edits:
        return
    lines = _text_lines(page)
    for edit in edits:
        page.add_redact_annot(_redaction(page, edit.box, lines), fill=(1, 1, 1))
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
    if erase_only:
        return
    for edit in edits:
        _draw(page, edit.box, edit.text, edit.nudge)


def _draw(page: fitz.Page, box: tuple[float, float, float, float], text: str,
          nudge: tuple[float, float]) -> None:
    """Draw one replacement figure. The old one must already be redacted away.

    Placement is worked out in display space, where text reads left to right
    whatever the page's rotation, then mapped back for the write. Hard-coding a
    rotation works on one sheet and lays the figure on its side on another: Fall
    2026's pages are rotated 90 degrees and Spring 2026's are not.

    The sheet sets these figures flush right, so the two lines of a cell end on a
    common edge. Anchoring at the string's start would let a shorter replacement
    drift and break that edge.
    """
    placed = fitz.Rect(*box) * page.rotation_matrix
    width = fitz.get_text_length(text, fontname=FONT, fontsize=FONT_SIZE)
    dx, dy = nudge
    origin = fitz.Point(
        placed.x1 - width + dy,
        placed.y1 - placed.height * BASELINE_LIFT + dx,
    ) * page.derotation_matrix
    page.insert_text(
        origin, text,
        fontname=FONT, fontsize=FONT_SIZE, color=BLUE,
        rotate=page.rotation,
    )


def write_corrected(
    pdf_path: str | Path,
    plan: Iterable[Change],
    printed: Mapping[str, Printed],
    out_path: str | Path,
    approved: set[str] | None = None,
    offsets: Mapping[str, Mapping[str, tuple[float, float]]] | None = None,
    overrides: Mapping[str, Mapping[str, str]] | None = None,
    profile: str = DEFAULT_PROFILE,
) -> Report:
    """Write a corrected copy of the line sheet.

    Args:
        approved: styles the reviewer confirmed. ``None`` means every style that
            needs a write — convenient for tests, never for the app, where a
            human decides one at a time.
        offsets: per-style nudges, ``{style: {"rank": (dx, dy), "sales": ...}}``,
            from dragging a figure clear of the artwork. Moves where a figure sits;
            never what it says.
        overrides: per-style text the reviewer typed in place of the workbook's
            figure, ``{style: {"rank": "12/340"}}``. The workbook is the source of
            truth, so anything typed over it is recorded in
            :attr:`Report.edited` — a figure a human invented must never be
            reported the same way as one the file supplied. A blank override is
            ignored rather than treated as "erase this", because erasing is a
            decision that belongs to Deny, not to an empty text box.
        profile: a key of :data:`SAVE_PROFILES`. All of them are lossless.
    """
    changes = list(plan)
    if approved is None:
        approved = {c.style for c in changes if c.needs_write}
    offsets = offsets or {}
    overrides = overrides or {}
    options = SAVE_PROFILES.get(profile, SAVE_PROFILES[DEFAULT_PROFILE])

    report = Report()
    #: page index -> every figure to replace on it. Gathered across all styles
    #: first and carried out per page afterwards, because a redaction applied
    #: while another style's replacement is already drawn would erase it.
    pending: dict[int, list[Edit]] = {}
    document = fitz.open(pdf_path)
    try:
        for change in changes:
            if not change.needs_write:
                # Approving a style with no workbook row must not blank it.
                if change.style in approved and change.blocks_confirm:
                    report.refused.append(change.style)
                continue
            if change.style not in approved:
                report.skipped += 1
                continue
            if is_unidentified(change.style):
                report.refused.append(change.style)
                continue

            shown = printed.get(change.style)
            if shown is None or shown.page is None:
                report.refused.append(change.style)
                continue

            page = document[shown.page - 1]
            nudge = offsets.get(change.style, {})
            typed = overrides.get(change.style, {})
            wrote = edited = False
            for key, box, new, old in (
                ("rank", shown.rank_box, change.correct_rank, change.printed_rank),
                ("sales", shown.sales_box, change.correct_sales, change.printed_sales),
            ):
                by_hand = key in typed and str(typed[key]).strip()
                if by_hand:
                    new = str(typed[key]).strip()
                if box is None or new is None or new == old:
                    continue
                edited = edited or bool(by_hand)
                # Painting a white rectangle over the old figure only hides it:
                # the text stays in the content stream, so the file still *says*
                # the wrong number even though it looks right. :func:`redraw`
                # redacts it away instead.
                pending.setdefault(shown.page - 1, []).append(
                    Edit(box, new, tuple(nudge.get(key, (0.0, 0.0))))
                )
                report.expected.setdefault(change.style, {})[key] = new
                wrote = True
            if wrote:
                report.written += 1
                report.touched.append(change.style)
                if edited:
                    report.edited.append(change.style)
            else:
                report.skipped += 1

        for index, edits in pending.items():
            redraw(document[index], edits)

        document.save(out_path, **options)
    finally:
        document.close()
    report.original_size = Path(pdf_path).stat().st_size
    report.size = Path(out_path).stat().st_size
    return report
