# Search Typeahead System

## Quick Start (Docker — recommended)

```bash
git clone <your-repo-url>
cd search-typeahead
docker compose up --build
```

- Frontend: http://localhost:3000
- Backend API: http://localhost:8000
- API docs: http://localhost:8000/docs

## Quick Start (local, no Docker)

```bash
cd backend
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python -m app.core.load_dataset ../data/queries.csv
python -m uvicorn app.main:app --reload
# open frontend/index.html in your browser
```

---

## Architecture

```
Browser (frontend/index.html)
        │  GET /suggest?q=<prefix>&ranking=count|recency
        │  POST /search  {"query": "..."}
        │  GET /trending
        │  GET /cache/debug?prefix=<prefix>
        │  GET /metrics
        ▼
FastAPI (backend/app/main.py)
        │
   ┌────┴────────────────────────┐
   │                             │
DistributedCache            BatchWriter
(cache.py)                  (batch_writer.py)
3 in-process shards         asyncio.Queue
routed by consistent         flushes every 3s
hashing ring                 or 50 events
   │                             │
   └────────┬────────────────────┘
            │
          Trie (trie.py)
          in-memory prefix index
          built at startup from SQLite
            │
          SQLite (query_counts table)
          source of truth for counts
```

## Dataset

- **Source:** Synthetically generated from curated topic vocabulary
  (tech/electronics, software/CS, fitness, finance, education)
- **Method:** Base terms × search modifiers (price, review, tutorial, etc.) →
  counts assigned via Zipfian distribution (count = BASE / rank^0.55 + noise)
- **Size:** 176,233 unique queries
- **Justification:** Zipfian distribution mirrors real search query frequency
  (a few queries dominate volume, long tail gets little). This matches the
  assignment's "derive counts by aggregation" allowance.

To regenerate: `python data/generate_dataset.py`

---

## API Reference

| Endpoint | Method | Description |
|---|---|---|
| `/suggest?q=<prefix>&ranking=count\|recency` | GET | Returns up to 10 prefix-matching suggestions |
| `/search` | POST | Submits a search, increments count, returns `{"message":"Searched"}` |
| `/trending?limit=10` | GET | Top trending queries (recency-boosted) |
| `/cache/debug?prefix=<prefix>` | GET | Shows which cache node owns this prefix and whether it's a hit |
| `/metrics` | GET | Cache hit/miss per node, pending writes, flush count |
| `/health` | GET | Health check |

---

## Design Decisions

### 1. Trie for prefix indexing
Each TrieNode stores `{full_query: count}` for all queries passing through it.
`suggest(prefix)` walks to the right node in O(len(prefix)) then returns
top-10 via `heapq.nlargest` — no table scan needed.

**Trade-off:** Extra memory proportional to sum of all query lengths. Acceptable
at this dataset size; at billions of queries you'd use an external prefix store
(Redis sorted sets with a ZRANGEBYLEX query).

### 2. Consistent hashing cache
**Why consistent hashing over `hash(prefix) % N`:** Adding or removing a node
with modular hashing remaps almost all keys, causing a mass cache miss
(stampede). Consistent hashing places nodes and keys on a hash ring; only the
keys between a removed node and its neighbor get remapped — the rest stay put.

**Virtual nodes:** Each real node gets 150 virtual points on the ring to
average out uneven arc sizes and balance load across shards.

**What "distributed" means here:** The 3 nodes are in-process dicts (not
separate machines/containers). The routing logic — ring, hashing, ownership —
is identical to routing to 3 real Redis instances. Swap `SimpleCacheNode`
for a `redis.Redis(host=...)` client to make it truly network-distributed.

### 3. Batch writes
**Why:** Writing to SQLite synchronously on every search request would create
write pressure at high QPS. Instead:
- POST /search updates the in-memory Trie immediately (reads see new count right away)
- The query is queued in an `asyncio.Queue`
- A background task aggregates and flushes to SQLite every 3 seconds OR every 50 events

**Failure trade-off:** If the process crashes between flushes, up to one
flush window of increments (≤3s or ≤50 events) is lost from SQLite. The
Trie is rebuilt from SQLite on restart, so those counts disappear from
suggestions too. Mitigation: persist the queue to a WAL file or use an
external message queue (Kafka/SQS) so pending writes survive a crash.

### 4. Trending / recency ranking
**Formula:** `score = all_time_count × (1 + 0.15 × recent_event_count)`

**Why multiplicative not additive:** This dataset's counts span 1 to 200k+.
An additive "+50 per recent search" is invisible on a 100k-count query but
falsely dominates a 10-count query. A percentage boost scales with each
query's own baseline — same logic as why real trending systems use relative
change rather than absolute delta.

**Spike decay:** The boost is computed from a sliding 120-second window.
Once a burst of searches stops, those timestamps age out naturally — no
manual cleanup or decay function needed.

**Cache invalidation on trending change:** Every POST /search invalidates
all cache entries for all prefixes of that query (both ranking modes), so
stale sorted results don't linger.

---

## Performance

To measure p95 latency and cache hit rate, hit `/metrics` after some usage.

Example shell benchmark:
```bash
# warm up
for i in $(seq 1 50); do curl -s "http://localhost:8000/suggest?q=python" > /dev/null; done
# measure
for q in "py" "python" "docker" "machine" "iphone"; do
  curl -s -w "q=$q time=%{time_total}s\n" -o /dev/null "http://localhost:8000/suggest?q=$q"
done
```

---

## Project Structure

```
search-typeahead/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI app, all endpoints
│   │   └── core/
│   │       ├── db.py            # SQLite schema + session
│   │       ├── trie.py          # Prefix index
│   │       ├── cache.py         # Consistent hashing + LRU cache shards
│   │       ├── batch_writer.py  # Async batch write queue
│   │       ├── trending.py      # Recency-aware scoring
│   │       └── load_dataset.py  # CSV → SQLite loader
│   ├── requirements.txt
│   └── Dockerfile
├── frontend/
│   └── index.html               # Single-file UI (no build step)
├── data/
│   ├── queries.csv              # 176k generated queries
│   └── generate_dataset.py      # Regenerate dataset
├── docker-compose.yml
└── README.md
```
