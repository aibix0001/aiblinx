"""Which stories the reader can show, and the ranking penalty for sites it can't.

A story is readable when the reader finds its article text or a video it
plays itself (``article.get_page``: plain fetch, then the headless browser,
with the chat model judging the text); otherwise the reader sends the user to
the original page. At
feed-build time each story on the shortlist is checked once (the result is
kept in ``page_checks``), and every failure charges its site. A "site" is the
host plus the page's first directory (``tagesschau.de/tagesthemen``), so a
section of video episodes does not drag down the same host's articles:

- the first failure while the site's penalty is 0 sets it to 0.2,
- every further failure adds 0.1,
- each day without a failure from that site takes 0.2 off again, down to 0.

A page that could not be loaded at all (timeout, dropped connection, browser
down) says
nothing about the reader: it stays unchecked, charges nothing, and is checked
again at the next build.

The penalty is subtracted from the site's stories' scores. It is a ranking
penalty only: no downvote is recorded and the interest profile never sees it.

The same check reads the page's own publication date (``htmldate``: page
metadata, structured data, the URL). A story is dated by its listing (a
Hacker News post, a feed entry), so an old page posted again looks new; a
page dated before the freshness window (``CANDIDATE_MAX_AGE_DAYS``) is
stale and not served. A page without a date is kept.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, date, datetime, timedelta
from typing import NamedTuple
from urllib.parse import urlsplit

from htmldate import find_date

from ..article import Unreachable, get_page
from ..clients.llm import LLMClient
from ..config import Settings
from ..db import connection

FIRST_FAILURE = 0.2
NEXT_FAILURE = 0.1
DAILY_RECOVERY = 0.2


def site_of(url: str) -> str:
    """The penalty key: host plus first directory, or the host alone for a
    page directly under the root."""
    parts = urlsplit(url)
    host = (parts.hostname or "").removeprefix("www.")
    dirs = [part for part in parts.path.split("/") if part][:-1]
    return f"{host}/{dirs[0]}" if dirs else host


def _today() -> date:
    return datetime.now(UTC).date()


def current_penalty(penalty: float, last_failure: date, today: date) -> float:
    """The stored penalty less the recovery for each failure-free day since."""
    return max(0.0, penalty - DAILY_RECOVERY * (today - last_failure).days)


def penalty_after_failure(current: float) -> float:
    return current + NEXT_FAILURE if current > 0 else FIRST_FAILURE


def site_penalties(conn: sqlite3.Connection, today: date | None = None) -> dict[str, float]:
    """{site: penalty > 0} as of ``today``."""
    today = today or _today()
    out = {}
    for row in conn.execute("SELECT site, penalty, last_failure FROM site_penalties"):
        penalty = current_penalty(row[1], date.fromisoformat(row[2]), today)
        if penalty > 0:
            out[row[0]] = penalty
    return out


def record_failure(conn: sqlite3.Connection, site: str, today: date | None = None) -> float:
    """Charge one failure to ``site``; returns its new penalty."""
    today = today or _today()
    row = conn.execute(
        "SELECT penalty, last_failure FROM site_penalties WHERE site = ?", (site,)
    ).fetchone()
    current = current_penalty(row[0], date.fromisoformat(row[1]), today) if row else 0.0
    penalty = penalty_after_failure(current)
    conn.execute(
        "INSERT OR REPLACE INTO site_penalties(site, penalty, last_failure) VALUES(?, ?, ?)",
        (site, penalty, today.isoformat()),
    )
    return penalty


class PageCheck(NamedTuple):
    readable: bool
    published: date | None  # the page's own date, when it names one


def page_date(page: bytes, url: str) -> date | None:
    """The date the page says it was first published, or None."""
    found = find_date(page, url=url, original_date=True, extensive_search=False)
    return date.fromisoformat(found) if found else None


def is_stale(check: PageCheck, settings: Settings, today: date | None = None) -> bool:
    """Whether the page is dated before the freshness window."""
    oldest = (today or _today()) - timedelta(days=settings.candidate_max_age_days)
    return check.published is not None and check.published < oldest


async def check_page(
    settings: Settings, url: str, title: str | None, llm: LLMClient | None
) -> PageCheck:
    """Whether the reader can show the page (article text or a playable
    video), and its date. Raises ``Unreachable`` when the page could not be
    loaded."""
    page = await get_page(settings, url, title, llm)
    if page is None:
        return PageCheck(False, None)
    return PageCheck(True, await asyncio.to_thread(page_date, page.html, url))


async def check_pages(
    settings: Settings, items: list[tuple[int, str, str | None]], llm: LLMClient | None = None
) -> dict[int, PageCheck]:
    """{candidate id: check} for ``(id, url, title)`` items. Pages not
    checked before are fetched now, and each failure is charged to its site.
    A page that could not be loaded is left out of the result."""
    if not items:
        return {}
    ids = [cid for cid, _, _ in items]
    with connection(settings) as conn:
        placeholders = ",".join("?" * len(ids))
        known = {
            cid: PageCheck(bool(readable), date.fromisoformat(published) if published else None)
            for cid, readable, published in conn.execute(
                f"SELECT candidate_id, readable, published FROM page_checks "  # noqa: S608
                f"WHERE candidate_id IN ({placeholders})",
                ids,
            )
        }
    todo = [(cid, url, title) for cid, url, title in items if cid not in known]
    semaphore = asyncio.Semaphore(settings.enrich_concurrency)

    async def one(url: str, title: str | None) -> PageCheck:
        async with semaphore:
            return await check_page(settings, url, title, llm)

    results = await asyncio.gather(
        *(one(url, title) for _, url, title in todo), return_exceptions=True
    )
    with connection(settings) as conn:
        for (cid, url, _), result in zip(todo, results, strict=True):
            if isinstance(result, Unreachable):
                continue
            if isinstance(result, BaseException):
                result = PageCheck(False, None)  # broke in a way a reader page would too
            published = result.published.isoformat() if result.published else None
            conn.execute(
                "INSERT OR REPLACE INTO page_checks(candidate_id, readable, published) "
                "VALUES(?, ?, ?)",
                (cid, int(result.readable), published),
            )
            if not result.readable:
                record_failure(conn, site_of(url))
            known[cid] = result
    return {cid: known[cid] for cid in ids if cid in known}
