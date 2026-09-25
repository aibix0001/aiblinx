# Changelog

## 0.1.2

- **New wordmark:** *aib* | *linx* in the app header, the setup and login
  pages, and a new home-screen icon. (Phones cache the icon: remove aiblinx
  from the home screen and add it again to see it.)

## 0.1.1

- **Switch AI presets without losing anything:** when the embedding model
  changes, aiblinx backs up its database and re-computes every vector from the
  text it keeps, instead of refusing to start. Saves, imports, votes and topics
  are kept.
- Quieter logs: one summary line instead of a warning per long article.

## 0.1.0

First release: a private, self-hosted Discover feed.

- **Learns from what you keep:** browser bookmarks (import on the setup page),
  [Linkwarden](https://linkwarden.app) bookmarks, and stories you save or
  upvote. Save, more like this and less like this are the only buttons.
- **Finds its own sources:** subscribes to the feeds of the sites you keep,
  plus Hacker News; optional [Miniflux](https://miniflux.app) and
  [SearXNG](https://docs.searxng.org) for an *Exploring* section on topics you
  pick.
- **Phone-first feed:** title images from the articles, a two-to-three
  sentence summary, a line on why each story was picked, light and dark mode,
  and *Add to Home Screen*. Also an Atom feed and a daily digest.
- **One key to start:** an OpenRouter API key configures everything (free
  embeddings, about 5 cents a month for the ranking model, or fully free with
  `LLM_RERANK=false`). Ollama, NVIDIA NIM and any OpenAI-compatible server work
  too.
- **One container:** Miniflux, SearXNG and Ollama are optional Compose
  profiles. Optional password (`APP_PASSWORD`).

Known limitation: the embedding model is fixed once the first feed is built;
switching it keeps your local saves only after a future release adds
re-embedding.
