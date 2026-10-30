"""Mock /ask API for the Embrace Healthcare help chatbot.

Retrieves the top few relevant chunks from the crawled site (TF-IDF, computed
in-process — no vector DB) and asks Groq to answer using only those, returning
validated source links. Never trusts model-authored URLs directly: the model
must pick from the URLs actually present in the retrieved chunks, and anything
else it returns is dropped (see embrace-ai-help-simple-handover.md, Phase 3).
"""
import json
import os
import pathlib
import time

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from groq import Groq
from pydantic import BaseModel
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

load_dotenv()

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
if not GROQ_API_KEY:
    raise SystemExit("GROQ_API_KEY not set — copy .env.example to .env and fill it in")

MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-20b")
TOP_K = 6
CACHE_TTL_SECONDS = 60  # short TTL since this is a local mock, not a scheduled recrawl

CRAWLER_DIR = pathlib.Path(__file__).parent.parent / "crawler"
CHUNKS_FILE = CRAWLER_DIR / "chunks.json"
FRONTEND_DIR = pathlib.Path(__file__).parent.parent / "frontend"

client = Groq(api_key=GROQ_API_KEY)
app = FastAPI(title="Embrace Healthcare Help Assistant (mock)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_cache = {"chunks": None, "vectorizer": None, "matrix": None, "loaded_at": 0.0}


def load_site_data():
    now = time.time()
    if _cache["chunks"] is not None and (now - _cache["loaded_at"]) < CACHE_TTL_SECONDS:
        return _cache["chunks"], _cache["vectorizer"], _cache["matrix"]

    if not CHUNKS_FILE.exists():
        raise HTTPException(
            status_code=503,
            detail="Site not crawled yet — run discover_urls.py, crawl.py, build_context.py first",
        )

    chunks = json.loads(CHUNKS_FILE.read_text(encoding="utf-8"))
    # No stop_words filtering: excluding common words (e.g. "our") drops them
    # from the vocabulary entirely, so a short query like "our" always scored
    # zero matches no matter what — even though "our" appears right in a page
    # title ("Our Story"). TF-IDF's own weighting already downweights common
    # words that appear in most chunks, so keeping them in the vocabulary is
    # safe — it just means single common-word queries can now match too.
    vectorizer = TfidfVectorizer()
    matrix = vectorizer.fit_transform(c["text"] for c in chunks)

    _cache.update(chunks=chunks, vectorizer=vectorizer, matrix=matrix, loaded_at=now)
    return chunks, vectorizer, matrix


def retrieve_top_chunks(question: str, chunks, vectorizer, matrix, top_k: int = TOP_K):
    q_vec = vectorizer.transform([question])
    scores = cosine_similarity(q_vec, matrix)[0]
    ranked = scores.argsort()[::-1][:top_k]
    return [chunks[i] for i in ranked if scores[i] > 0], q_vec.nnz


class AskRequest(BaseModel):
    question: str


class Source(BaseModel):
    title: str
    url: str


class AskResponse(BaseModel):
    answer: str
    sources: list[Source]


def build_system_prompt(context_chunks: list[dict]) -> tuple[str, dict]:
    context = "\n---\n".join(
        f"URL: {c['url']}\nTITLE: {c['title']}\n{c['text']}" for c in context_chunks
    )
    url_titles = {c["url"]: c["title"] for c in context_chunks}

    prompt = f"""You are the help assistant for Embrace Healthcare's website.
Answer using ONLY the content below. Never invent facts or URLs.

Respond as JSON only, in exactly this shape:
{{"answer": "your answer, no links inside the text", "source_urls": ["exact-url-from-the-list"]}}

source_urls must be copied exactly from this list — never write a URL that isn't here:
{chr(10).join(url_titles.keys())}

If the content doesn't answer the question, say so in "answer" and return an empty source_urls list.

SITE CONTENT (top matches for this question):
{context}
"""
    return prompt, url_titles


@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest):
    chunks, vectorizer, matrix = load_site_data()
    top_chunks, query_token_count = retrieve_top_chunks(req.question, chunks, vectorizer, matrix)

    if not top_chunks:
        if query_token_count == 0:
            # The question was made up entirely of common words ("the", "our",
            # "is", ...) that get filtered out before matching — there was
            # nothing distinctive left to search on, not "no results found".
            answer = (
                "That's a bit too general for me to search on — could you add "
                "more detail? E.g. a service name or topic, like "
                "\"How do I get transport funding?\" or \"What is community nursing?\""
            )
        else:
            answer = "I couldn't find anything relevant on the site for that — try rephrasing, or contact Embrace directly."
        return AskResponse(answer=answer, sources=[])

    system_prompt, url_titles = build_system_prompt(top_chunks)

    completion = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": req.question},
        ],
        response_format={"type": "json_object"},
        temperature=0.2,
    )

    raw = completion.choices[0].message.content
    parsed = json.loads(raw)

    # Defensive check — drop any URL the model returns that isn't actually in the retrieved chunks
    valid_urls = [u for u in parsed.get("source_urls", []) if u in url_titles]
    sources = [Source(title=url_titles[u], url=u) for u in valid_urls]

    return AskResponse(answer=parsed.get("answer", ""), sources=sources)


@app.get("/health")
def health():
    return {"status": "ok", "chunks_loaded": CHUNKS_FILE.exists()}


@app.get("/")
def serve_frontend():
    return FileResponse(FRONTEND_DIR / "index.html")
