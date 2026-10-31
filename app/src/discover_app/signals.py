"""Reading signals per Linkwarden link, for transcribix (``GET /api/signals``).

Contract v1, the "Ask" half.
A link is ``aiblinx`` when one of our saves made it, else ``hand`` (a link the
mirror knows that no save of ours made). Unknown ids are left out.
"""

from __future__ import annotations

from .config import Settings
from .db import connection
from .pipeline.feedback import known_archive_collection, known_explore_collection, latest_facets
from .urls import norm_url

MAX_IDS = 200


def save_collection(conn, settings: Settings, link_id: int, section: str) -> int | None:
    """The collection of the link a save made: the mirror's when it knows it,
    else the one the save's section files into."""
    row = conn.execute("SELECT collection_id FROM links WHERE id = ?", (link_id,)).fetchone()
    if row is not None and row[0] is not None:
        return row[0]
    if section == "broad":
        return known_explore_collection(conn, settings)
    if section == "archive":
        return known_archive_collection(conn, settings)
    return settings.linkwarden_collection_id


def link_signals(settings: Settings, link_ids: list[int]) -> list[dict]:
    wanted = list(dict.fromkeys(link_ids))
    if not wanted:
        return []
    marks = ",".join("?" * len(wanted))  # placeholders only: the ids are bound
    save_cols = "linkwarden_id, url_key, section, pdf_turns"
    save_sql = f"SELECT {save_cols} FROM saves WHERE linkwarden_id IN ({marks})"  # noqa: S608
    link_sql = f"SELECT id, url, collection_id FROM links WHERE id IN ({marks})"  # noqa: S608
    with connection(settings) as conn:
        saves = {row["linkwarden_id"]: row for row in conn.execute(save_sql, wanted)}
        links = {row["id"]: row for row in conn.execute(link_sql, wanted)}
        collections = {
            link_id: save_collection(conn, settings, link_id, save["section"])
            for link_id, save in saves.items()
        }
        opened = {row[0] for row in conn.execute("SELECT url FROM opens")}
        highlights: dict[str, int] = {}
        for url, count in conn.execute(
            "SELECT c.url, COUNT(*) FROM highlights h JOIN candidates c ON c.id = h.candidate_id "
            "GROUP BY h.candidate_id"
        ):
            key = norm_url(url)
            highlights[key] = highlights.get(key, 0) + count

    result = []
    for link_id in wanted:
        save, link = saves.get(link_id), links.get(link_id)
        if save is None and link is None:
            continue
        url_key = save["url_key"] if save is not None else norm_url(link["url"])
        if save is not None:
            collection_id = collections[link_id]
        else:
            collection_id = link["collection_id"]
        result.append(
            {
                "linkwarden_id": link_id,
                "via": "aiblinx" if save is not None else "hand",
                "collection_id": collection_id,
                "section": save["section"] if save is not None else None,
                "interest": latest_facets(settings, url_key).get("interest"),
                "opened": url_key in opened,
                "highlights": highlights.get(url_key, 0),
                "discussion_turns": (save["pdf_turns"] or 0) if save is not None else 0,
            }
        )
    return result
