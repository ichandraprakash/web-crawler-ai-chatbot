"""Split crawl_output/*.json into small chunks for lightweight local retrieval.

Originally this built one giant site_context.txt sent whole on every question
(per embrace-ai-help-simple-handover.md). That blew Groq's free-tier
tokens-per-minute limit on a single request regardless of model (45k tokens
vs. a 6k-12k TPM cap), so instead we chunk pages and let the API retrieve only
the handful of relevant chunks per question — still no vector DB, just
TF-IDF similarity computed in-process at query time.
"""
import json
import pathlib

HERE = pathlib.Path(__file__).parent
CRAWL_DIR = HERE / "crawl_output"
CHUNKS_FILE = HERE / "chunks.json"
INDEX_FILE = HERE / "url_index.json"

CHUNK_WORDS = 250


def chunk_text(text: str, max_words: int = CHUNK_WORDS) -> list[str]:
    words = text.split()
    return [" ".join(words[i : i + max_words]) for i in range(0, len(words), max_words)]


def main():
    files = sorted(CRAWL_DIR.glob("*.json"))
    if not files:
        raise SystemExit("No crawled pages found — run crawl.py first")

    pages = [json.loads(f.read_text()) for f in files]

    chunks = []
    url_index = {}
    for page in pages:
        if not page["text"]:
            continue
        url_index[page["url"]] = page["title"]
        for i, piece in enumerate(chunk_text(page["text"])):
            chunks.append(
                {
                    "id": f"{page['url']}#{i}",
                    "url": page["url"],
                    "title": page["title"],
                    "text": piece,
                }
            )

    CHUNKS_FILE.write_text(json.dumps(chunks, indent=2), encoding="utf-8")
    INDEX_FILE.write_text(json.dumps(url_index, indent=2), encoding="utf-8")
    print(f"{len(pages)} pages -> {len(chunks)} chunks written to {CHUNKS_FILE}")


if __name__ == "__main__":
    main()
