# Embrace Healthcare Help Assistant — local mock

A local prototype of the chatbot described in `embrace-ai-help-simple-handover.md`
and `embrace-ai-help-search-handover.md`: crawl the live site, save the content,
and let an LLM answer visitor questions using only that content — pointing back
to the real page (e.g. a specific blog post) instead of making things up.

Everything here runs on your own machine: Groq's API instead of Gemini/Vertex AI,
local disk instead of Cloud Storage, no Cloud Run/Cloud SQL/GCP at all.

## What it does

1. **Crawl** — reads your site's WordPress sitemap to find every real page
   (main pages, service pages, blog posts), fetches each one, and strips out
   the nav bar/footer/junk so only the actual title + body text is kept.
2. **Chunk** — splits each page's text into small ~250-word pieces. This is
   the chatbot's whole "knowledge base."
3. **Ask** — when a question comes in:
   - it searches the chunks for the handful most related to the question
     (fast word-overlap matching — no AI needed for this step),
   - hands only those chunks to Groq's LLM along with the question, and tells
     it to answer using *only* that content, returning the answer text and
     the source URL as separate JSON fields (never as a link written inline —
     that's exactly where a model would confidently invent a URL that doesn't
     exist),
   - double-checks every URL the model returns against the real crawled page
     list before showing it to anyone. Anything that isn't a real page is
     silently dropped.
4. **Chat page** — a plain HTML page with an input box. Type a question, it
   calls the local API, and renders the answer plus a clickable link to the
   actual page on your site.

## Why it searches first instead of sending the whole site

The original plan (per `embrace-ai-help-simple-handover.md`) was simpler: hand
the LLM the *entire* site as context on every question, no search step at all.
That works fine on Vertex AI's Gemini, which is what the doc assumed — but on
Groq's free tier, a single request is capped at 6,000–12,000 tokens, and this
site's full content is ~45,000 tokens. Sending everything failed immediately
regardless of which Groq model was used.

Searching for the relevant chunks first and sending only those solves the
rate-limit problem, and is arguably a better design anyway — cheaper and more
precise per question. Nothing about the crawler changed; only what happens
between "site is crawled" and "question gets answered."

## Setup

```bash
python -m venv venv
venv\Scripts\activate        # Windows
pip install -r requirements.txt
copy .env.example .env       # then paste your Groq API key into .env
```

## 1. Crawl the site

```bash
cd crawler
python discover_urls.py      # finds page/service/blog URLs from the WP sitemap -> urls.json
python crawl.py               # fetches each page, saves clean JSON to crawl_output/
python build_context.py       # splits crawl_output/*.json into chunks.json + url_index.json
cd ..
```

Spot-check a file in `crawler/crawl_output/` to make sure the text looks like
real page content and not nav/footer junk.

## 2. Run the API

```bash
uvicorn api.main:app --reload --port 8000
```

If port 8000 is already in use (e.g. a previous run still holding it), find
and stop that process, or start on a different `--port`.

Test it:

```bash
curl -X POST http://localhost:8000/ask -H "Content-Type: application/json" -d "{\"question\": \"How do I get transport funding?\"}"
```

## 3. Open the chat page

Open `frontend/index.html` directly in a browser (no server needed — it just
calls `http://localhost:8000/ask`).

## Notes

- The API never lets the model write its own links: it forces JSON output and
  drops any `source_urls` entry that isn't one of the URLs actually retrieved
  for that question.
- Re-run the three crawler scripts any time to refresh content; the API
  reloads `chunks.json` from disk every 60 seconds (`CACHE_TTL_SECONDS` in
  `api/main.py`), so a fresh crawl shows up without restarting the server.
- The site currently has a few leftover placeholder/test pages (`/pm/`,
  `/pc/`, `/new-demo-test/`, etc.) with generic template text rather than real
  content — not a crawler bug, but worth cleaning up on the WordPress side
  since the chatbot will happily quote whatever's actually published there.
- If the site's content grows a lot (hundreds of pages, years of blog
  history), revisit `embrace-ai-help-search-handover.md`'s pgvector design —
  the crawler here is identical either way, only the storage/retrieval layer
  would change.
