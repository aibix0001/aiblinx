"""Highlighter marks in the reader.

The page owns the marks: it finds them in the text, splits them where a new
mark or the eraser overlaps, and sends the whole set of one text back
(``PUT /read/{id}/highlights``). The server only keeps that set. A saved
story whose marks change gets its PDF flagged as stale, so the reader offers
"Update PDF".
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .config import Settings
from .db import connection
from .linkviews import HIGHLIGHT_DELAY_S, queue_put
from .urls import norm_url

Variant = Literal["orig", "tr"]
MAX_MARKS = 500


class Mark(BaseModel):
    color: Literal["yellow", "green", "red"]
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    quote: str = Field(min_length=1, max_length=20000)
    prefix: str = Field(default="", max_length=64)
    suffix: str = Field(default="", max_length=64)

    @model_validator(mode="after")
    def _ordered(self) -> Mark:
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


class MarksRequest(BaseModel):
    variant: Variant = "orig"
    marks: list[Mark] = Field(default=[], max_length=MAX_MARKS)


_FIELDS = ("color", "start", "end", "quote", "prefix", "suffix")


def load_marks(settings: Settings, candidate_id: int) -> dict[str, list[dict]]:
    """The story's marks by text: ``{"orig": [...], "tr": [...]}``."""
    marks: dict[str, list[dict]] = {"orig": [], "tr": []}
    with connection(settings) as conn:
        for row in conn.execute(
            "SELECT variant, color, start, end, quote, prefix, suffix FROM highlights "
            "WHERE candidate_id = ? ORDER BY start, id",
            (candidate_id,),
        ):
            marks[row["variant"]].append({f: row[f] for f in _FIELDS})
    return marks


def store_marks(
    settings: Settings, candidate_id: int, variant: Variant, marks: list[Mark], url: str
) -> None:
    """Replace the marks of one text; a saved story's PDF is stale then."""
    with connection(settings) as conn:
        conn.execute(
            "DELETE FROM highlights WHERE candidate_id = ? AND variant = ?",
            (candidate_id, variant),
        )
        conn.executemany(
            "INSERT INTO highlights(candidate_id, variant, color, start, end, quote, prefix, "
            "suffix) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (candidate_id, variant, m.color, m.start, m.end, m.quote, m.prefix, m.suffix)
                for m in marks
            ],
        )
        conn.execute("UPDATE saves SET pdf_stale = 1 WHERE url_key = ?", (norm_url(url),))
        queue_put(conn, settings, norm_url(url), delay_s=HIGHLIGHT_DELAY_S)
