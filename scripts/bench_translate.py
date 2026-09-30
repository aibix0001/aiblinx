"""Benchmark reader translations: one fixed corpus, any chat model.

  freeze  export the prepared articles from a discover.db into a JSON corpus
  run     translate that corpus against an OpenAI-compatible endpoint

Both run with the app's environment, e.g. from the repo root:

  uv run --project app python scripts/bench_translate.py freeze \\
      --db .data/discover-app/data/discover.db --out corpus.json
  uv run --project app python scripts/bench_translate.py run --corpus corpus.json \\
      --base-url http://localhost:8080/v1 --model <model> --out result.json

``run`` uses the stage's own code path (``translate_article``, same chunking)
and never touches a database. Call time is measured inside the concurrency
slot, so waiting for a free slot is not counted against the model.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sqlite3
import statistics
import time
from pathlib import Path

from discover_app.clients.llm import LLMClient
from discover_app.config import Settings
from discover_app.pipeline.articles import translate_article

# The reader's top-level blocks; their text is escaped, so no raw tag inside
_BLOCK = re.compile(r"<(p|h2|h3|blockquote|ul|ol|pre)>.*?</\1>", re.S)


def freeze(db: Path, out: Path) -> None:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT a.candidate_id, a.lang, c.title, a.body FROM articles a "
        "JOIN candidates c ON c.id = a.candidate_id "
        "WHERE a.body IS NOT NULL AND a.lang IS NOT NULL ORDER BY a.candidate_id"
    ).fetchall()
    corpus = [
        {
            "id": cid,
            "lang": lang,
            "title": title or "",
            "blocks": [m[0] for m in _BLOCK.finditer(body)],
        }
        for cid, lang, title, body in rows
    ]
    out.write_text(json.dumps(corpus, ensure_ascii=False, indent=1))
    chars = sum(len("".join(a["blocks"])) for a in corpus)
    print(f"{len(corpus)} articles, {chars} characters -> {out}")


class _TimedLLM:
    """One article's view of the model: shares the slot limit, sums its own
    call time (inside the slot) and counts calls."""

    def __init__(self, llm: LLMClient, slots: asyncio.Semaphore) -> None:
        self.llm, self.slots, self.seconds = llm, slots, 0.0

    async def chat(self, messages, **kwargs) -> str:
        async with self.slots:
            t0 = time.monotonic()
            try:
                return await self.llm.chat(messages, **kwargs)
            finally:
                self.seconds += time.monotonic() - t0


async def run(args: argparse.Namespace) -> None:
    corpus = json.loads(Path(args.corpus).read_text())
    settings = Settings(
        _env_file=None,
        llm_base_url=args.base_url,
        llm_chat_model=args.model,
        llm_token=os.environ.get(args.token_env, "") if args.token_env else "",
        llm_embed_model="unused",
        llm_concurrency=1000,  # the benchmark's own semaphore sets the pace
    )
    llm = LLMClient(settings)
    slots = asyncio.Semaphore(args.concurrency)

    async def one(article: dict) -> dict:
        timed = _TimedLLM(llm, slots)
        dst = "en" if article["lang"] == "de" else "de"
        result = {
            "id": article["id"],
            "lang": article["lang"],
            "chars": len("".join(article["blocks"])),
        }
        try:
            title, body, calls = await translate_article(
                timed, article["title"], article["blocks"], article["lang"], dst
            )
            result |= {"ok": True, "calls": calls, "title_tr": title, "body_tr": body}
        except Exception as exc:
            result |= {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        result["llm_seconds"] = round(timed.seconds, 1)
        print(
            f"item {result['id']} {result['lang']} {result['chars']} chars "
            f"{'ok' if result['ok'] else 'FAILED'} {result['llm_seconds']}s",
            flush=True,
        )
        return result

    started = time.monotonic()
    try:
        results = await asyncio.gather(*(one(a) for a in corpus))
    finally:
        await llm.aclose()
    wall = time.monotonic() - started
    ok = [r for r in results if r["ok"]]
    secs = sorted(r["llm_seconds"] for r in ok)
    summary = {
        "model": args.model,
        "base_url": args.base_url,
        "concurrency": args.concurrency,
        "articles": len(results),
        "ok": len(ok),
        "failed": len(results) - len(ok),
        "wall_seconds": round(wall),
        "chars_per_minute": round(sum(r["chars"] for r in ok) / wall * 60)
        if wall
        else 0,
        "seconds_per_article": {
            "mean": round(statistics.mean(secs), 1) if secs else None,
            "median": round(statistics.median(secs), 1) if secs else None,
            "p90": secs[int(0.9 * (len(secs) - 1))] if secs else None,
            "max": secs[-1] if secs else None,
        },
        "seconds_per_1000_chars": (
            round(sum(secs) / sum(r["chars"] for r in ok) * 1000, 1) if ok else None
        ),
    }
    Path(args.out).write_text(
        json.dumps(
            {"summary": summary, "results": results}, ensure_ascii=False, indent=1
        )
    )
    print(json.dumps(summary, indent=1))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("freeze", help="export prepared articles into a JSON corpus")
    f.add_argument("--db", type=Path, required=True)
    f.add_argument("--out", type=Path, required=True)
    r = sub.add_parser("run", help="translate the corpus against one model")
    r.add_argument("--corpus", required=True)
    r.add_argument("--base-url", required=True)
    r.add_argument("--model", required=True)
    r.add_argument("--token-env", help="name of the env var holding the API token")
    r.add_argument("--concurrency", type=int, default=3)
    r.add_argument("--out", required=True)
    args = parser.parse_args()
    if args.cmd == "freeze":
        freeze(args.db, args.out)
    else:
        asyncio.run(run(args))


if __name__ == "__main__":
    main()
