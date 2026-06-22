"""
Batch writer for search-count updates.

WHY (viva answer): if every POST /search wrote directly to SQLite, you'd
pay a disk write on every single keystroke-triggered search at peak load.
Instead we buffer increments in memory and flush them to SQLite as one
batched UPSERT, either when PENDING reaches a size threshold or every
FLUSH_INTERVAL seconds -- whichever comes first.

DESIGN:
- POST /search updates the in-memory Trie immediately (so /suggest reflects
  the new count right away -- feels "live" to the user).
- The same query is ALSO queued for durable persistence to SQLite.
- A background asyncio task periodically aggregates the queue (duplicate
  queries in the same window are summed into a single increment) and does
  ONE bulk UPSERT instead of N individual writes.

FAILURE TRADE-OFF (you will be asked this): if the process crashes between
flushes, any increments enqueued-but-not-yet-flushed are lost from SQLite,
the durable source of truth -- bounded data loss equal to one flush
interval / one batch size, whichever triggers first. The in-memory Trie
itself is NOT crash-safe either; on restart it's rebuilt fresh from
whatever SQLite has, so the same unflushed increments are gone from
suggestions too after a restart. Mitigations not implemented here for time
reasons: write-ahead log on disk, or a message queue (Kafka/SQS) instead
of an in-memory Python queue, so pending writes survive a process crash.
"""
import asyncio
import time
from collections import defaultdict

from sqlalchemy import text

from app.core.db import engine
from app.core.trie import Trie
from app.core.cache import DistributedCache

FLUSH_INTERVAL_SECONDS = 3.0
FLUSH_SIZE_THRESHOLD = 50


class BatchWriter:
    def __init__(self, trie: Trie, cache: DistributedCache):
        self.trie = trie
        self.cache = cache
        self._pending: dict[str, int] = defaultdict(int)
        self._lock = asyncio.Lock()
        self.total_writes_flushed = 0  # count of DB upserts (for hit/write reporting)
        self.total_increments_received = 0
        self.last_flush_at = time.time()

    async def submit(self, query: str) -> int:
        """Called from the /search endpoint. Returns the new live count."""
        query = query.strip().lower()
        if not query:
            return 0
        new_count = self.trie.increment(query, 1)  # immediate, visible to /suggest
        # invalidate every prefix-cache-entry that could include this query,
        # for BOTH ranking modes since cache keys are "<ranking>:<prefix>"
        for i in range(1, len(query) + 1):
            prefix = query[:i]
            self.cache.invalidate(f"count:{prefix}")
            self.cache.invalidate(f"recency:{prefix}")

        async with self._lock:
            self._pending[query] += 1
            self.total_increments_received += 1
            pending_size = len(self._pending)

        if pending_size >= FLUSH_SIZE_THRESHOLD:
            await self.flush()

        return new_count

    async def flush(self):
        async with self._lock:
            if not self._pending:
                return
            batch = dict(self._pending)
            self._pending.clear()

        with engine.begin() as conn:
            for query, delta in batch.items():
                conn.execute(
                    text(
                        """
                        INSERT INTO query_counts (query, count) VALUES (:q, :d)
                        ON CONFLICT(query) DO UPDATE SET count = count + :d
                        """
                    ),
                    {"q": query, "d": delta},
                )
        self.total_writes_flushed += 1
        self.last_flush_at = time.time()

    async def run_periodic_flush(self):
        while True:
            await asyncio.sleep(FLUSH_INTERVAL_SECONDS)
            await self.flush()
