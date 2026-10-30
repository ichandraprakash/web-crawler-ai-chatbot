# Embrace Healthcare — AI Site Search / Help Assistant

**Handover doc for dev team**
**Target site:** `https://sso-embracehealthcare-489151498736.australia-southeast1.run.app/`
**Author:** Niluksha (NNLP Consulting) — prepared for dev/devops handover
**Audience:** GCP/devops experience — commands are spelled out, no steps skipped

---

## 1. What we're building

The homepage already has a UI stub: *"How can we help? Ask me anything about our services…"*. Right now it's (presumably) not wired to anything real. The job is to make that box actually work:

1. Crawl the site and turn it into a searchable index (pages, blog posts, services).
2. Embed that content into a vector store.
3. Build a small API that takes a question, retrieves relevant content, and asks an LLM to answer with a link to the right page.
4. Wire the existing search box to call that API.
5. Recrawl on a schedule so new blog posts get picked up automatically.

No new UI needs to be designed — the input box exists. This is a backend + integration job.

---

## 2. Architecture (target state)

```
                    ┌─────────────────────┐
   Cloud Scheduler ─▶│  Crawler Job         │
   (daily, 3am AEST) │  (Cloud Run Job)     │
                    └──────────┬───────────┘
                               │ writes raw pages
                               ▼
                    ┌─────────────────────┐
                    │  Cloud Storage       │
                    │  gs://embrace-crawl  │
                    └──────────┬───────────┘
                               │ triggers
                               ▼
                    ┌─────────────────────┐
                    │  Embedding Job       │
                    │  (Cloud Run Job)     │
                    └──────────┬───────────┘
                               ▼
                    ┌─────────────────────┐
                    │  Cloud SQL (pgvector)│◀──── nav graph JSON
                    │  or Vertex Vector    │      (Cloud Storage)
                    │  Search              │
                    └──────────┬───────────┘
                               │ queried by
                               ▼
                    ┌─────────────────────┐
   Existing site  ──▶│  /ask API            │──▶ Gemini / Claude
   search box        │  (Cloud Run service) │    (via Vertex AI or
                    └─────────────────────┘     Anthropic API)
```

Everything runs in the same GCP project and region (`australia-southeast1`) as the existing site, so latency and data residency stay consistent.

---

## 3. Prerequisites (do this first)

- [ ] Confirm which GCP project the site lives in: `gcloud config get-value project` after being added as a collaborator, or ask whoever owns the Cloud Run service.
- [ ] Get `roles/editor` or scoped roles (`roles/run.admin`, `roles/cloudsql.admin`, `roles/storage.admin`, `roles/aiplatform.user`) on that project.
- [ ] Enable required APIs:

```bash
gcloud services enable \
  run.googleapis.com \
  cloudscheduler.googleapis.com \
  sqladmin.googleapis.com \
  aiplatform.googleapis.com \
  storage.googleapis.com \
  secretmanager.googleapis.com
```

- [ ] Decide on LLM provider: Gemini via Vertex AI (simplest IAM story, stays fully inside GCP) vs Claude via the Anthropic API (needs an API key in Secret Manager). Either works — this doc uses Vertex AI Gemini in examples since it avoids a second vendor's credentials, but swap the SDK call if Claude is preferred.
- [ ] Check if there's already a CMS/API behind `/api/images/view/blogs/...` — if the blog content is available as structured JSON from an existing backend, use that instead of scraping rendered HTML. Ask whoever built the site's frontend before starting the crawler.

---

## 4. Phase 1 — Crawler

**Goal:** turn the live site into a folder of clean, structured JSON files (one per page).

### 4.1 Set up the project

```bash
mkdir embrace-crawler && cd embrace-crawler
python3 -m venv venv
source venv/bin/activate
pip install httpx selectolax beautifulsoup4
```

### 4.2 Discover all URLs

Start from the nav links already known:

```python
# seed_urls.py
BASE = "https://sso-embracehealthcare-489151498736.australia-southeast1.run.app"

SEED_URLS = [
    f"{BASE}/",
    f"{BASE}/our-story",
    f"{BASE}/assistance-with-daily-life",
    f"{BASE}/capacity-building",
    f"{BASE}/community-participation",
    f"{BASE}/opportunities",
    f"{BASE}/empowerment-hub",
    f"{BASE}/team",
    f"{BASE}/blogs",
    f"{BASE}/apps",
]
```

Then crawl `/blogs` for the full list of blog post links (there are more than the ones shown on the homepage — the homepage only teases the latest ones). Also check for a sitemap:

```bash
curl -s https://sso-embracehealthcare-489151498736.australia-southeast1.run.app/sitemap.xml
```

If that returns XML, use it as the URL list instead of hand-crawling nav — it's more reliable and will catch pages not linked from the main nav.

### 4.3 Crawl and extract

```python
# crawl.py
import httpx, json, hashlib, pathlib
from selectolax.parser import HTMLParser

OUT_DIR = pathlib.Path("crawl_output")
OUT_DIR.mkdir(exist_ok=True)

def clean_page(url: str) -> dict:
    resp = httpx.get(url, timeout=20, follow_redirects=True)
    resp.raise_for_status()
    tree = HTMLParser(resp.text)

    title = tree.css_first("title")
    title = title.text() if title else ""

    # Strip nav, footer, script, style before extracting body text
    for tag in tree.css("nav, footer, script, style"):
        tag.decompose()

    body_node = tree.css_first("main") or tree.css_first("body")
    text = body_node.text(separator=" ", strip=True) if body_node else ""

    return {
        "url": url,
        "title": title,
        "text": text,
        "hash": hashlib.sha256(text.encode()).hexdigest(),
    }

def save(page: dict):
    slug = page["url"].rstrip("/").split("/")[-1] or "home"
    (OUT_DIR / f"{slug}.json").write_text(json.dumps(page, indent=2))

if __name__ == "__main__":
    from seed_urls import SEED_URLS
    for url in SEED_URLS:
        try:
            page = clean_page(url)
            save(page)
            print(f"OK  {url}  ({len(page['text'])} chars)")
        except Exception as e:
            print(f"FAIL {url}: {e}")
```

Run it:

```bash
python crawl.py
```

You should end up with a `crawl_output/` folder full of JSON files like `capacity-building.json`, `our-story.json`, etc. **Check a couple by hand** — open `home.json` and confirm the text isn't full of nav junk or empty. This is the step most likely to need tweaking (CSS selectors will differ once you actually inspect the rendered HTML in devtools).

### 4.4 Upload to Cloud Storage

```bash
gsutil mb -l australia-southeast1 gs://embrace-crawl-raw
gsutil -m cp -r crawl_output/* gs://embrace-crawl-raw/latest/
```

### 4.5 Containerize as a Cloud Run Job (so it can run on a schedule later)

```dockerfile
# Dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "crawl.py"]
```

```bash
gcloud builds submit --tag australia-southeast1-docker.pkg.dev/YOUR_PROJECT/embrace/crawler
gcloud run jobs create embrace-crawler \
  --image australia-southeast1-docker.pkg.dev/YOUR_PROJECT/embrace/crawler \
  --region australia-southeast1 \
  --max-retries 1
```

Test it manually before scheduling:

```bash
gcloud run jobs execute embrace-crawler --region australia-southeast1
```

---

## 5. Phase 2 — Embeddings + vector store

**Goal:** every crawled page/section becomes a vector you can search semantically.

### 5.1 Choose the store

For a site this size (a few dozen pages, dozens of blog posts) **use pgvector on Cloud SQL** — it's cheaper and simpler to reason about than Vertex AI Vector Search, which is built for millions of vectors. Only switch to Vector Search if the content grows to thousands of pages.

```bash
gcloud sql instances create embrace-vector-db \
  --database-version=POSTGRES_15 \
  --tier=db-f1-micro \
  --region=australia-southeast1

gcloud sql databases create embrace --instance=embrace-vector-db
```

Connect and enable the extension:

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE page_chunks (
    id SERIAL PRIMARY KEY,
    url TEXT NOT NULL,
    title TEXT,
    section TEXT,          -- e.g. "Services > Capacity Building"
    chunk_text TEXT NOT NULL,
    embedding VECTOR(768),  -- 768 for Gemini text-embedding, adjust if using a different model
    content_hash TEXT,
    updated_at TIMESTAMP DEFAULT now()
);
```

### 5.2 Chunk + embed

Long blog posts (the transport funding one, the cyclone prep one) need chunking — don't embed a 2000-word post as one vector, split into ~300-word chunks so retrieval is precise.

```python
# embed.py
import json, pathlib
from google.cloud import aiplatform
from vertexai.language_models import TextEmbeddingModel
import psycopg2

model = TextEmbeddingModel.from_pretrained("text-embedding-004")

def chunk_text(text: str, max_words=300):
    words = text.split()
    return [" ".join(words[i:i+max_words]) for i in range(0, len(words), max_words)]

def embed_and_store(conn, page: dict):
    chunks = chunk_text(page["text"])
    embeddings = model.get_embeddings(chunks)
    cur = conn.cursor()
    for chunk, emb in zip(chunks, embeddings):
        cur.execute(
            """INSERT INTO page_chunks (url, title, chunk_text, embedding, content_hash)
               VALUES (%s, %s, %s, %s, %s)""",
            (page["url"], page["title"], chunk, emb.values, page["hash"])
        )
    conn.commit()

if __name__ == "__main__":
    conn = psycopg2.connect(...)  # use Cloud SQL connector, not raw psycopg2 in prod
    for f in pathlib.Path("crawl_output").glob("*.json"):
        page = json.loads(f.read_text())
        embed_and_store(conn, page)
```

**Important for recrawls:** before inserting, check `content_hash` against what's already in the table for that URL. If unchanged, skip re-embedding (saves API cost and avoids duplicate rows). Delete old chunks for a URL before inserting new ones if the hash changed.

### 5.3 Nav graph

Separately, hand-build (or script from the seed URL list) a small JSON describing the site structure, so the LLM can point people to sections, not just raw text:

```json
{
  "Home": "/",
  "Our Story": "/our-story",
  "Services": {
    "Assistance with daily life": "/assistance-with-daily-life",
    "Capacity Building": "/capacity-building",
    "Community participation": "/community-participation"
  },
  "Opportunities": "/opportunities",
  "Empowerment Hub": "/empowerment-hub",
  "Team": "/team",
  "Blogs": "/blogs",
  "Embrace Apps": "/apps"
}
```

Store this in `gs://embrace-crawl-raw/nav-graph.json`.

---

## 6. Phase 3 — The `/ask` API

**Goal:** one endpoint the frontend can call.

```python
# main.py — FastAPI service
from fastapi import FastAPI
from pydantic import BaseModel
import psycopg2
from vertexai.language_models import TextEmbeddingModel
from vertexai.generative_models import GenerativeModel

app = FastAPI()
embed_model = TextEmbeddingModel.from_pretrained("text-embedding-004")
llm = GenerativeModel("gemini-2.0-flash")

class AskRequest(BaseModel):
    question: str

class Source(BaseModel):
    title: str
    url: str

class AskResponse(BaseModel):
    answer: str
    sources: list[Source]

@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest):
    q_embedding = embed_model.get_embeddings([req.question])[0].values

    conn = psycopg2.connect(...)  # Cloud SQL connector in prod
    cur = conn.cursor()
    cur.execute(
        """SELECT title, url, chunk_text
           FROM page_chunks
           ORDER BY embedding <-> %s::vector
           LIMIT 5""",
        (q_embedding,)
    )
    rows = cur.fetchall()

    context = "\n\n".join(f"[{title}]({url}): {text}" for title, url, text in rows)

    prompt = f"""You are the help assistant for Embrace Healthcare Solutions' website.
Answer the user's question using ONLY the context below. If the answer isn't in the
context, say you're not sure and suggest they contact Embrace directly.
Keep answers short and point to the relevant page.

Context:
{context}

Question: {req.question}
"""

    response = llm.generate_content(prompt)

    sources = [Source(title=t, url=u) for t, u, _ in rows]
    return AskResponse(answer=response.text, sources=sources)
```

### 6.1 Deploy

```bash
gcloud builds submit --tag australia-southeast1-docker.pkg.dev/YOUR_PROJECT/embrace/ask-api
gcloud run deploy embrace-ask-api \
  --image australia-southeast1-docker.pkg.dev/YOUR_PROJECT/embrace/ask-api \
  --region australia-southeast1 \
  --allow-unauthenticated \
  --add-cloudsql-instances YOUR_PROJECT:australia-southeast1:embrace-vector-db
```

`--allow-unauthenticated` is fine here since it's just answering questions about public content — no user data involved. If that changes later (e.g. it starts touching participant data), lock it down and add auth.

### 6.2 Test it

```bash
curl -X POST https://embrace-ask-api-xxxx.a.run.app/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "How do I get transport funding through the NDIS?"}'
```

Expected: an answer referencing the transport funding blog post, with a link back to it.

---

## 7. Phase 4 — Wire into the existing UI

The homepage already has:

```
How can we help?
Ask me anything about our services…
```

Find this component in the site's frontend repo (search for that placeholder text). It's likely already a form/input with an `onSubmit` that either does nothing or points somewhere temporary. Wire it to call `POST https://embrace-ask-api-xxxx.a.run.app/ask` and render `answer` + a list of linked `sources`.

If this is a Next.js app, the fetch can go through a Next.js API route (`/api/ask`) that proxies to the Cloud Run service — keeps the Cloud Run URL out of client-side code and gives you a place to add rate limiting later.

---

## 8. Phase 5 — Scheduling recrawl + re-embedding

```bash
gcloud scheduler jobs create http embrace-recrawl-daily \
  --schedule="0 17 * * *" \
  --uri="https://REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/YOUR_PROJECT/jobs/embrace-crawler:run" \
  --http-method=POST \
  --oauth-service-account-email=YOUR_SERVICE_ACCOUNT \
  --time-zone="Australia/Perth"
```

(`17:00 UTC` = `01:00 AWST` — adjust to whenever off-peak makes sense; Embrace operates out of the Pilbara/WA so AWST is the relevant timezone, not AEST.)

Chain the embedding job to run right after — either a second Scheduler job offset by 15–20 minutes, or (cleaner) have the crawler job trigger a Cloud Run job execution via a Pub/Sub message on completion. Start with the simple time-offset version; only build the event-driven version if the crawl time becomes unpredictable.

---

## 9. Testing checklist before calling this done

- [ ] Crawler picks up all blog posts, not just the ones shown on the homepage (check `/blogs` pagination)
- [ ] Re-running the crawler + embed job twice in a row doesn't create duplicate rows in `page_chunks`
- [ ] `/ask` returns a sensible answer for at least these test questions:
  - "How do I get transport funding?"
  - "What is capacity building?"
  - "Do you help with aged care?"
  - "How do I get cyclone ready?"
  - Something **not** on the site (e.g. "do you offer NDIS plan management") — confirm it says it's unsure rather than hallucinating
- [ ] Answers include correct source links (click through and confirm they load)
- [ ] Recrawl schedule actually fires (check Cloud Scheduler logs after first scheduled run)
- [ ] `/ask` API has basic rate limiting or is at least behind the Next.js proxy so it's not trivially hammerable from outside

---

## 10. Rough cost note

At this site's scale (dozens of pages, low query volume), expect this to run near-free:
- Cloud SQL `db-f1-micro`: ~$10–15/month
- Cloud Run (crawler job + API service): likely within free tier or a few dollars/month at low traffic
- Embedding + Gemini calls: fractions of a cent per question at this volume

No need to over-provision anything here — this isn't a high-traffic consumer app.

---

## 11. Suggested order of work / milestones

1. **Day 1–2:** Crawler working locally, JSON output looks clean, uploaded to Cloud Storage manually.
2. **Day 3:** pgvector schema live, embedding script running, spot-check a few semantic searches directly against the DB.
3. **Day 4:** `/ask` API deployed, tested via curl.
4. **Day 5:** Frontend wired up, end-to-end test through the actual search box on the site.
5. **Day 6:** Scheduler jobs in place, do a full dry-run recrawl to confirm no duplication.
6. **Day 7:** Testing checklist above, handover/demo.

This assumes one dev working on it with some GCP familiarity — a junior devops person should be able to follow this end to end, but flag Phase 1 (crawler selector tuning) and Phase 3 (Cloud SQL connector auth in prod, not raw `psycopg2.connect`) as the two spots most likely to need a hand from someone more senior.
