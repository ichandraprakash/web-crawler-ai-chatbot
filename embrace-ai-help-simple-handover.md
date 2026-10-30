# Embrace Healthcare — AI Site Search (Simple Version, No Vector DB)

**Target site:** `https://sso-embracehealthcare-489151498736.australia-southeast1.run.app/`
**Audience:** dev with junior-level GCP/devops experience
**Relationship to the other handover doc:** this is the lean alternative to `embrace-ai-help-search-handover.md`. That doc uses pgvector and retrieval; this one skips both and just hands the model the whole site as context on every question. Build this one first — it's faster to ship and good enough at the site's current size.

---

## 1. What we're building

Same target as before: the "How can we help? Ask me anything about our services…" box on the homepage needs a real backend. The difference is architectural. Instead of embedding pages into a vector database and retrieving the top few matches per question, the crawler produces one text file containing the entire site, and that whole file gets sent to the LLM alongside every question. No embeddings, no Cloud SQL, no retrieval logic.

## 2. When this approach is the right call

The site is a few dozen pages plus a dozen or so blog posts — call it 20,000 to 60,000 words total. Gemini 2.0 Flash has a 1M token context window, so the whole site fits with plenty of headroom. At this scale, sending everything is simpler to build, simpler to debug, and cheap enough not to matter.

That stops being true once the site grows past a few hundred pages. Two things degrade: cost per query goes up because you're resending the whole site every time instead of a handful of relevant chunks, and very long context windows can lose precision when the answer is buried in the middle of a huge blob. If Embrace's content grows a lot — more service pages, years of blog history — move to the retrieval version in the other handover doc. Nothing here is wasted if that happens: the crawler is identical, only the storage and query logic change.

## 3. Architecture

```
Cloud Scheduler ──▶ Crawler Job (Cloud Run Job)
                          │ writes raw pages + builds
                          │ site_context.txt + url_index.json
                          ▼
                    Cloud Storage
                    gs://embrace-crawl-raw/latest/
                          │ read on startup, refreshed periodically
                          ▼
Existing site   ──▶ /ask API (Cloud Run service) ──▶ Gemini (Vertex AI)
search box                │
                          ▼
                    JSON: {"answer": "...", "source_urls": [...]}
                          │
                          ▼
                    Frontend renders answer text + real <a href> links
```

## 4. Prerequisites

- [ ] Access to the same GCP project as the live site, `australia-southeast1`.
- [ ] Roles: `roles/run.admin`, `roles/storage.admin`, `roles/aiplatform.user`. No Cloud SQL role needed — there's no database in this version.
- [ ] Enable APIs:

```bash
gcloud services enable \
  run.googleapis.com \
  cloudscheduler.googleapis.com \
  aiplatform.googleapis.com \
  storage.googleapis.com
```

- [ ] Check whether the blog content is already available through a structured backend (the `/api/images/view/blogs/...` path on the site suggests one might exist). If so, pull content from that API directly rather than scraping rendered HTML — it's cleaner and won't break if the page markup changes later. Confirm with whoever owns the site's frontend before starting the crawler.

## 5. Phase 1 — Crawler

Same crawler as the retrieval version — this part of the job doesn't change based on which architecture you pick downstream.

```bash
mkdir embrace-crawler && cd embrace-crawler
python3 -m venv venv
source venv/bin/activate
pip install httpx selectolax
```

```python
# crawl.py
import httpx, json, hashlib, pathlib
from selectolax.parser import HTMLParser

BASE = "https://sso-embracehealthcare-489151498736.australia-southeast1.run.app"
OUT_DIR = pathlib.Path("crawl_output")
OUT_DIR.mkdir(exist_ok=True)

SEED_URLS = [
    f"{BASE}/", f"{BASE}/our-story", f"{BASE}/assistance-with-daily-life",
    f"{BASE}/capacity-building", f"{BASE}/community-participation",
    f"{BASE}/opportunities", f"{BASE}/empowerment-hub", f"{BASE}/team",
    f"{BASE}/blogs", f"{BASE}/apps",
]

def clean_page(url: str) -> dict:
    resp = httpx.get(url, timeout=20, follow_redirects=True)
    resp.raise_for_status()
    tree = HTMLParser(resp.text)
    title = tree.css_first("title")
    title = title.text() if title else ""
    for tag in tree.css("nav, footer, script, style"):
        tag.decompose()
    body_node = tree.css_first("main") or tree.css_first("body")
    text = body_node.text(separator=" ", strip=True) if body_node else ""
    return {"url": url, "title": title, "text": text,
            "hash": hashlib.sha256(text.encode()).hexdigest()}

if __name__ == "__main__":
    for url in SEED_URLS:
        try:
            page = clean_page(url)
            slug = url.rstrip("/").split("/")[-1] or "home"
            (OUT_DIR / f"{slug}.json").write_text(json.dumps(page, indent=2))
            print(f"OK  {url}  ({len(page['text'])} chars)")
        except Exception as e:
            print(f"FAIL {url}: {e}")
```

Check `/blogs` for the full post list rather than relying only on the homepage teasers, and check for a sitemap first:

```bash
curl -s https://sso-embracehealthcare-489151498736.australia-southeast1.run.app/sitemap.xml
```

If that returns real XML, crawl from it instead of the hand-typed seed list — it'll catch pages the nav doesn't link to directly.

Run it and spot-check a couple of the output files by hand before moving on. This step is the one most likely to need selector tweaks once you actually look at the rendered HTML in devtools.

## 6. Phase 2 — Build the context file

This replaces the entire embedding step from the retrieval doc. One script, two outputs: a single text file with everything, and a small index mapping URL to title so the API can validate links later.

```python
# build_context.py
import json, pathlib

pages = [json.loads(f.read_text()) for f in pathlib.Path("crawl_output").glob("*.json")]

context = "\n---\n".join(
    f"URL: {p['url']}\nTITLE: {p['title']}\n{p['text']}" for p in pages
)
pathlib.Path("site_context.txt").write_text(context)

url_index = {p["url"]: p["title"] for p in pages}
pathlib.Path("url_index.json").write_text(json.dumps(url_index, indent=2))

approx_tokens = len(context) // 4
print(f"site_context.txt: ~{approx_tokens} tokens")
if approx_tokens > 500_000:
    print("WARNING: approaching context budget — consider the retrieval version instead")
```

Upload both to Cloud Storage:

```bash
gsutil mb -l australia-southeast1 gs://embrace-crawl-raw
gsutil cp site_context.txt gs://embrace-crawl-raw/latest/site_context.txt
gsutil cp url_index.json gs://embrace-crawl-raw/latest/url_index.json
```

## 7. Phase 3 — The `/ask` API

The one part that needs real care here is stopping the model from inventing URLs. Don't let it write markdown links inline in its prose — that's the exact spot where a model will confidently produce a link that doesn't exist. Instead, force a JSON response where the answer text and the source links are separate fields, and check every returned URL against the known list before showing it to anyone.

```python
# main.py
import json, time
from fastapi import FastAPI
from pydantic import BaseModel
from google.cloud import storage
from vertexai.generative_models import GenerativeModel

app = FastAPI()
llm = GenerativeModel("gemini-2.0-flash")

BUCKET = "embrace-crawl-raw"
_cache = {"context": None, "url_index": {}, "loaded_at": 0}
CACHE_TTL_SECONDS = 3600  # reload hourly so a recrawl doesn't need a redeploy

def load_site_data():
    now = time.time()
    if _cache["context"] and (now - _cache["loaded_at"]) < CACHE_TTL_SECONDS:
        return _cache["context"], _cache["url_index"]

    client = storage.Client()
    bucket = client.bucket(BUCKET)
    context = bucket.blob("latest/site_context.txt").download_as_text()
    url_index = json.loads(bucket.blob("latest/url_index.json").download_as_text())

    _cache.update(context=context, url_index=url_index, loaded_at=now)
    return context, url_index

class AskRequest(BaseModel):
    question: str

@app.post("/ask")
def ask(req: AskRequest):
    context, url_index = load_site_data()

    system_prompt = f"""You are the help assistant for Embrace Healthcare Solutions.
Answer using ONLY the content below. Never invent facts or URLs.

Respond as JSON only, in exactly this shape:
{{"answer": "your answer, no links inside the text", "source_urls": ["/exact-url"]}}

source_urls must be copied exactly from this list — never write a URL that isn't here:
{chr(10).join(url_index.keys())}

If the content doesn't answer the question, say so in "answer" and return an empty source_urls list.

SITE CONTENT:
{context}
"""

    raw = llm.generate_content(f"{system_prompt}\n\nQuestion: {req.question}").text
    parsed = json.loads(raw.strip().strip("```json").strip("```"))

    # Defensive check — drop any URL the model returns that isn't actually real
    valid_urls = [u for u in parsed.get("source_urls", []) if u in url_index]
    sources = [{"title": url_index[u], "url": u} for u in valid_urls]

    return {"answer": parsed["answer"], "sources": sources}
```

Deploy:

```bash
gcloud builds submit --tag australia-southeast1-docker.pkg.dev/YOUR_PROJECT/embrace/ask-api
gcloud run deploy embrace-ask-api \
  --image australia-southeast1-docker.pkg.dev/YOUR_PROJECT/embrace/ask-api \
  --region australia-southeast1 \
  --allow-unauthenticated
```

No `--add-cloudsql-instances` flag this time — there's no database to connect to.

Test it:

```bash
curl -X POST https://embrace-ask-api-xxxx.a.run.app/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "How do I get transport funding through the NDIS?"}'
```

Expect a JSON body with an `answer` string and a `sources` array pointing at the transport funding blog post.

## 8. Phase 4 — Wire into the existing UI

Find the component behind the "How can we help?" placeholder in the site's frontend repo (search for that string). Point its submit handler at `POST /ask`, then render the response like this:

```jsx
<p>{data.answer}</p>
{data.sources.length > 0 && (
  <ul className="sources">
    {data.sources.map(s => <li key={s.url}><a href={s.url}>{s.title}</a></li>)}
  </ul>
)}
```

The links here come from the API's own `url_index` lookup, not from anything the model typed out, so there's nothing for the model to get wrong at render time. If the site's frontend is Next.js, proxy the call through a Next.js API route rather than hitting the Cloud Run URL directly from client-side code.

## 9. Phase 5 — Recrawl schedule

```bash
gcloud scheduler jobs create http embrace-recrawl-daily \
  --schedule="0 17 * * *" \
  --uri="https://REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/YOUR_PROJECT/jobs/embrace-crawler:run" \
  --http-method=POST \
  --oauth-service-account-email=YOUR_SERVICE_ACCOUNT \
  --time-zone="Australia/Perth"
```

`17:00 UTC` lands at `01:00 AWST` — Embrace operates out of the Pilbara, so Perth time is the one that matters here, not Melbourne or Sydney time.

The crawler job needs to run `build_context.py` at the end and re-upload both files to the same Cloud Storage path. Because the API caches the context for an hour (see `CACHE_TTL_SECONDS` above), a new crawl shows up in answers within an hour without needing a redeploy. Drop the TTL if fresher content matters more than the extra Cloud Storage reads.

## 10. Testing checklist

- [ ] `site_context.txt` token estimate stays comfortably under budget — check the printed warning from `build_context.py`
- [ ] Ask a question the site doesn't cover (e.g. "do you offer NDIS plan management") and confirm the answer says it's unsure, with an empty `sources` list, rather than guessing
- [ ] Ask something that could tempt a fabricated link and confirm every URL in the response actually resolves when clicked
- [ ] Recrawl updates the Cloud Storage files, and the API picks up the change within the cache TTL without a redeploy
- [ ] Confirm the model's raw output never leaks the system prompt or the full URL list back to the user — check the parsed `answer` field specifically, not just that the request succeeded

## 11. Cost note

No Cloud SQL bill in this version, which is the main saving over the retrieval build. The trade-off runs the other way on LLM cost: every single question resends the entire site as input tokens, so per-query cost is higher than the retrieval version once query volume climbs. At the traffic a site like this gets, it's still cheap — a few cents per hundred questions on Gemini 2.0 Flash pricing — but it's the number to watch if the help box gets popular. If daily query volume starts climbing into the thousands, that's a second signal (alongside site size) that it's time to move to the retrieval version.

## 12. Suggested order of work

1. **Day 1:** crawler running locally, `site_context.txt` and `url_index.json` built and uploaded to Cloud Storage.
2. **Day 2:** `/ask` API deployed, tested via curl, JSON contract confirmed to reject invented URLs.
3. **Day 3:** frontend wired to the real endpoint, tested through the actual search box on the site.
4. **Day 4:** Cloud Scheduler job in place, one full dry-run recrawl confirmed to flow through to the API without a redeploy, testing checklist run end to end.

Four days rather than the week estimated for the retrieval build — most of the time saved comes from skipping the Cloud SQL setup and the embedding pipeline entirely.
