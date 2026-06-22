"""
Full backend: /suggest (basic + recency ranking), /search (batched writes),
/trending, /cache/debug, /metrics.
"""
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import select

from app.core.db import engine, query_counts, init_db
from app.core.trie import Trie
from app.core.cache import DistributedCache
from app.core.batch_writer import BatchWriter
from app.core.trending import TrendingTracker

trie = Trie()
cache = DistributedCache()
trending = TrendingTracker()
batch_writer = BatchWriter(trie, cache)


def load_trie_from_db() -> int:
    init_db()
    with engine.connect() as conn:
        rows = conn.execute(select(query_counts.c.query, query_counts.c.count))
        count = 0
        for query, c in rows:
            trie.insert(query, c)
            count += 1
    return count


@asynccontextmanager
async def lifespan(app: FastAPI):
    n = load_trie_from_db()
    print(f"Trie built from {n} rows in SQLite.")
    import asyncio
    flush_task = asyncio.create_task(batch_writer.run_periodic_flush())
    yield
    flush_task.cancel()
    await batch_writer.flush()  # final flush on shutdown


app = FastAPI(title="Search Typeahead", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/suggest")
def suggest(
    q: str = Query(default=""),
    ranking: str = Query(default="count", description="'count' or 'recency'"),
):
    if not q.strip():
        return {"query": q, "ranking": ranking, "suggestions": []}

    t0 = time.perf_counter()
    cache_key = f"{ranking}:{q.strip().lower()}"
    cached = cache.get(cache_key)
    if cached is not None:
        latency_ms = (time.perf_counter() - t0) * 1000
        return {"query": q, "ranking": ranking, "suggestions": cached,
                "cache_hit": True, "latency_ms": round(latency_ms, 3)}

    if ranking == "recency":
        # over-fetch more candidates by count, then re-rank with recency boost.
        # Also fold in recently-searched queries under this prefix that
        # wouldn't make a pure top-30-by-count cut, so a fresh burst of
        # activity on a previously low-count query can still surface.
        raw = dict(trie.suggest(q, limit=30))
        for aq in trending.active_prefixed(q.strip().lower()):
            if aq not in raw:
                raw[aq] = trie.root.completions.get(aq, 0)
        reranked = trending.rerank(list(raw.items()), limit=10)
        suggestions = [
            {"text": text, "count": count, "score": score}
            for text, count, score in reranked
        ]
    else:
        raw = trie.suggest(q, limit=10)
        suggestions = [{"text": text, "count": count} for text, count in raw]

    cache.set(cache_key, suggestions)
    latency_ms = (time.perf_counter() - t0) * 1000
    return {"query": q, "ranking": ranking, "suggestions": suggestions,
            "cache_hit": False, "latency_ms": round(latency_ms, 3)}


class SearchRequest(BaseModel):
    query: str


@app.post("/search")
async def search(req: SearchRequest):
    new_count = await batch_writer.submit(req.query)
    trending.record(req.query.strip().lower())
    return {"message": "Searched", "query": req.query, "count": new_count}


@app.get("/trending")
def get_trending(limit: int = 10):
    results = trending.top_trending(trie.root.completions, limit=limit)
    return {
        "trending": [
            {"text": text, "count": count, "score": score}
            for text, count, score in results
        ]
    }


@app.get("/cache/debug")
def cache_debug(prefix: str = Query(default="")):
    if not prefix.strip():
        return {"error": "prefix required"}
    ranking = "count"
    key = f"{ranking}:{prefix.strip().lower()}"
    return cache.debug_route(key)


@app.get("/metrics")
def metrics():
    return {
        "cache_nodes": cache.stats(),
        "pending_writes_in_queue": len(batch_writer._pending),
        "total_increments_received": batch_writer.total_increments_received,
        "total_db_flushes": batch_writer.total_writes_flushed,
        "trie_total_queries": len(trie.root.completions),
    }


@app.get("/health")
def health():
    return {"status": "ok"}
