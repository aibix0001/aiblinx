# Changelog

## 0.6.0

- **Save for later:** a paper button on every card and in the reader keeps
  a story in the new Bookmarks tab for 14 days, without teaching your feed
  anything and without adding it to Linkwarden. From the tab, Save it for
  good or Remove it. Unread holds go after 14 days (`HOLD_DAYS`).
- **Icon tabs and buttons:** the tabs are a heart (For you), a globe
  (Exploring) and a sheet of paper (Bookmarks). Card actions are five equal
  icon buttons: Save, Save to Exploring or Promote, Save for later, up, down.
- **Saves weigh less:** a save now counts +1 for its interest, the same as
  ↑, instead of +2.
- **Fade time you can set:** `FEEDBACK_HALF_LIFE_DAYS` (how fast saves and
  votes fade, default 30 days) and `HOLD_DAYS` now reach the app when set in
  `.env`; before, Compose dropped them.
- **"For you" knows what you like:** the ranking model now sees your
  interests (titles of the pages you keep) and your latest ↓, and stories it
  scores below `MIN_RELEVANCE` (0.3) are left out: a thin day gives a shorter
  feed, not filler. A story closer to one you voted down than to anything you
  keep is dropped.
- **Cleaner interests:** bookmarks are embedded from their own text, so a
  site's error, login or cookie page no longer becomes an interest that
  matches everything.

## 0.5.0

- **Leave the chat and come back:** an answer keeps being written when you
  leave the reader for another story, and the discussion stays on your device
  for 30 days. Open the story again to see the answer and go on talking. The
  server holds an answer in memory only, for an hour after it is done.

## 0.4.0

- **Talk about a story:** the reader has a chat under the article. The model
  knows the whole article, as you read it and in the language on screen, plus
  the card summary and why the story was picked. With SearXNG set up, it
  searches the web when the story doesn't have the answer, and shows its
  searches with the result links. Answers stream in as they are written.
  Nothing about the conversation is stored or logged; it lives in the page.
- **Keep the discussion:** with Linkwarden connected, Save attaches the
  conversation to the link as a PDF: the story, the summary, why it was
  picked, and every question and answer with its searches. It takes the
  place of the page PDF Linkwarden makes; the page stays preserved as a
  screenshot and a single-file HTML copy, and the link keeps its preview
  picture. A story saved earlier gets "Attach this discussion".
- The chat text follows the reader's A− / A+ size, and on a phone the
  keyboard stays open after sending.
- `READER_CHAT=false` turns the chat off. `READER_TRANSLATE` now also works
  when set in `.env` with Docker Compose; before, Compose did not pass it on.

## 0.3.0

- **Read in German or English:** each morning aiblinx prepares the day's
  stories in advance and translates them between German and English with your
  chat model. The reader opens a story in its own language, and a DE / EN
  button swaps in the translation. A prepared story is not fetched from the
  publisher again when you open it. `READER_TRANSLATE=false` turns translation
  off (it costs tokens on a paid API).
- **Pictures in the reader:** the article's pictures appear where they stand in
  the text, with their captions. Logos, teasers and tracking pixels are left
  out.
- **Bigger or smaller text:** A− / A+ in the reader, remembered per device.
  Title images are clickable: on a card they open the reader, in the reader
  they open the original page.
- **Home-screen app stays current:** the app added to the home screen now
  loads the newest feed when you return to it, and pulling down at the top
  reloads it.
- The theme button shows the sun in dark mode. The log has one line per
  prepared story with its translation time, and `scripts/bench_translate.py`
  compares chat models on the same stories.

## 0.2.0

- **Read inside aiblinx:** tapping a story opens a clean reader view of the
  article, in the same tab, so swiping back returns to the feed. Video pages
  play their video right there. Pages without readable text open the original.
  Nothing about what you open is recorded, cached or logged, and publishers
  get no referrer.
- **Exploring stays separate:** saves and votes on Exploring stories no longer
  change "For you". They teach Exploring which distractions you like instead,
  and Exploring saves go to their own Linkwarden collection ("Exploring",
  created for you, or set `LINKWARDEN_EXPLORE_COLLECTION_ID`).
- **Move stories between the two:** *Promote* (⌃⌃) on an Exploring story makes
  it a main interest. The compass on a "For you" story saves it to Exploring
  and shows less like it in "For you".
- **Feeds in Setup:** see every feed aiblinx reads, unsubscribe, or add a site.
  A site you unsubscribe is never added back automatically.
- **Finer interests:** the number of interest clusters now grows with your
  profile (8 to 32; `PROFILE_CLUSTERS` still fixes it).
- **New look:** navy and teal colours, and new home-screen icons. Phones cache
  the icon: remove aiblinx from the home screen and add it again to see it.
- Reloading keeps the tab you were on. Headlines no longer show raw `&#34;`
  codes, and summaries that only repeat the headline are left out.

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
