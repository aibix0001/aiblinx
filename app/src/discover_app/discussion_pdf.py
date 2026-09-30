"""A reader discussion as a PDF, for the saved story's Linkwarden link.

One document: the story (title, address, source and date), our card summary
and the line on why it was picked, then the conversation turn by turn with
the searches the model ran and their result links.

DejaVu Sans (Debian's fonts-dejavu-core, installed in the image) covers
umlauts, typographic quotes and most symbols; without it the PDF falls back
to Helvetica, which only knows Latin-1.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from fpdf import FPDF

_FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")
# fpdf2's markdown: **bold**, __italic__, --underline--. The model writes
# Markdown links and headings, which it does not know.
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_HEADING = re.compile(r"^#{1,6}\s+(.*)$", re.M)
_UNDERLINE = re.compile(r"--")


def _markdown(text: str) -> str:
    """The model's Markdown as fpdf2 markdown: links as "text (url)",
    headings as bold lines, and "--" kept as dashes."""
    text = _LINK.sub(r"\1 (\2)", text)
    text = _HEADING.sub(r"**\1**", text)
    return _UNDERLINE.sub("–", text)


class _Doc(FPDF):
    def __init__(self) -> None:
        super().__init__(format="A4")
        self.set_margins(20, 20, 20)
        self.set_auto_page_break(True, margin=18)
        regular, bold = _FONT_DIR / "DejaVuSans.ttf", _FONT_DIR / "DejaVuSans-Bold.ttf"
        if regular.exists() and bold.exists():
            # no oblique in fonts-dejavu-core: italic is set upright
            for style, path in (("", regular), ("B", bold), ("I", regular), ("BI", bold)):
                self.add_font("DejaVu", style, str(path))
            self._family = "DejaVu"
        else:
            self._family = "Helvetica"

    def text_of(self, text: str) -> str:
        if self._family == "Helvetica":
            return text.encode("latin-1", "replace").decode("latin-1")
        return text

    def para(
        self, text: str, size: float = 11, style: str = "", color: int = 0, markdown: bool = False
    ) -> None:
        self.set_font(self._family, style, size)
        self.set_text_color(color)
        self.multi_cell(0, size * 0.5, self.text_of(text), markdown=markdown, new_x="LMARGIN")

    def heading(self, text: str) -> None:
        self.ln(4)
        self.para(text, size=13, style="B")
        self.ln(1)

    def link_line(self, text: str, url: str, size: float = 9) -> None:
        self.set_font(self._family, "", size)
        self.set_text_color(26, 122, 130)
        self.multi_cell(0, size * 0.5, self.text_of(text), link=url, new_x="LMARGIN")
        self.set_text_color(0)


def render_discussion(
    item: dict, summary: str, why: str, turns: list[dict], saved_at: datetime | None = None
) -> bytes:
    """The PDF for one story. ``item``: title, url, source, published_at.
    ``turns``: ``{"role": "user" | "assistant", "content", "searches"}``,
    searches as ``[{"query", "results": [{"title", "url"}]}]``."""
    saved_at = saved_at or datetime.now(UTC)
    doc = _Doc()
    doc.set_title(item.get("title") or item.get("url") or "Discussion")
    doc.set_creator("aiblinx")
    doc.add_page()
    doc.para(item.get("title") or item.get("url") or "", size=18, style="B")
    doc.ln(2)
    if item.get("url"):
        doc.link_line(item["url"], item["url"])
    info = " · ".join(
        part for part in (item.get("source"), (item.get("published_at") or "")[:10]) if part
    )
    if info:
        doc.para(info, size=9, color=100)
    if summary:
        doc.heading("Summary")
        doc.para(summary)
    if why:
        doc.heading("Why aiblinx picked it")
        doc.para(why)
    doc.heading(f"Discussion, saved {saved_at:%Y-%m-%d %H:%M} UTC")
    for turn in turns:
        doc.ln(2)
        mine = turn["role"] == "user"
        doc.para("You" if mine else "aiblinx", size=10, style="B", color=100)
        for search in turn.get("searches") or []:
            doc.para(f"Searched: {search.get('query', '')}", size=9, color=100)
            for result in search.get("results") or []:
                doc.link_line(f"  {result.get('title') or result['url']}", result["url"], size=8)
        doc.para(_markdown(turn["content"]) if not mine else turn["content"], markdown=not mine)
    return bytes(doc.output())
