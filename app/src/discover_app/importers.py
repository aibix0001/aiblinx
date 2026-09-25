"""Onboarding imports: browser bookmarks, pasted URLs, OPML feed lists.

Every browser exports bookmarks in the same Netscape HTML format, so one
parser covers Chrome, Firefox, Safari and Edge. Imported pages become profile
points (``imports`` table), which gives an install without Linkwarden a
profile on day one. OPML feed lists are handed to Miniflux, which has its own
importer.
"""

from __future__ import annotations

import asyncio
import html
import re

import httpx

from .config import Settings
from .db import connection
from .html_text import strip_html
from .pipeline.enrich import PAGE_HEADERS, fetch_meta
from .urls import norm_url

_ANCHOR = re.compile(r"<a\b[^>]*?\bhref\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.I | re.S)
_URL = re.compile(r"https?://[^\s<>\"']+")
_FEED_URL = re.compile(r"\bxmlUrl\s*=\s*[\"']([^\"']+)[\"']", re.I)

# Pasted URLs have no title, so each page is read once for its description.
_MAX_PASTED_URLS = 100


def parse_bookmarks_html(text: str) -> list[tuple[str, str]]:
    """``(url, title)`` pairs from a browser bookmarks export, http(s) only
    (bookmarklets, ``place:`` queries and local files are skipped)."""
    out = []
    for href, label in _ANCHOR.findall(text):
        url = html.unescape(href).strip()
        if url.startswith(("http://", "https://")):
            out.append((url, strip_html(label)))
    return out


def parse_url_list(text: str) -> list[str]:
    """Every http(s) URL in free text, in order, trailing punctuation dropped."""
    return [url.rstrip(".,;:)]}") for url in _URL.findall(text)]


def opml_feed_urls(text: str) -> list[str]:
    """Feed URLs of an OPML document (http(s) only, in order, once each)."""
    urls = (html.unescape(u).strip() for u in _FEED_URL.findall(text))
    return list(dict.fromkeys(u for u in urls if u.startswith(("http://", "https://"))))


def opml_feed_count(text: str) -> int:
    return len(opml_feed_urls(text))


def store_imports(settings: Settings, pages: list[dict], source: str) -> tuple[int, int]:
    """Insert pages (url, title, description) as profile points, once per
    normalized URL. Returns ``(added, skipped)``."""
    added = 0
    with connection(settings) as conn:
        for page in pages:
            cur = conn.execute(
                "INSERT OR IGNORE INTO imports(url, url_key, title, description, source) "
                "VALUES(?, ?, ?, ?, ?)",
                (
                    page["url"],
                    norm_url(page["url"]),
                    page.get("title") or None,
                    page.get("description"),
                    source,
                ),
            )
            added += cur.rowcount
    return added, len(pages) - added


def import_bookmarks(settings: Settings, text: str) -> tuple[int, int]:
    pages = [{"url": url, "title": title} for url, title in parse_bookmarks_html(text)]
    return store_imports(settings, pages, "bookmarks")


async def import_urls(settings: Settings, text: str) -> tuple[int, int]:
    """Pasted URLs: read each page's description (bounded) so the profile has
    more than a bare URL to go on."""
    urls = list(dict.fromkeys(parse_url_list(text)))[:_MAX_PASTED_URLS]
    semaphore = asyncio.Semaphore(settings.enrich_concurrency)

    async def one(client: httpx.AsyncClient, url: str) -> str | None:
        async with semaphore:
            return (await fetch_meta(client, url))[1]

    async with httpx.AsyncClient(
        headers=PAGE_HEADERS, timeout=settings.enrich_timeout_s, follow_redirects=True
    ) as client:
        descriptions = await asyncio.gather(*(one(client, url) for url in urls))
    pages = [{"url": u, "description": d} for u, d in zip(urls, descriptions, strict=True)]
    return store_imports(settings, pages, "urls")
