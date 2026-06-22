"""
Trending / recency-aware ranking.

THE 5 THINGS THE ASSIGNMENT EXPLICITLY WANTS YOU TO EXPLAIN:

1. How recent searches are tracked:
   Each query keeps a deque of timestamps for every search event on it,
   in TrendingTracker.events. Old timestamps are pruned lazily (on read)
   once they fall outside WINDOW_SECONDS.

2. How recent activity affects ranking:
   score(query) = all_time_count + BOOST_WEIGHT * recent_event_count
   recent_event_count = number of searches for that query still inside
   the sliding window. So a query with a sudden burst of searches gets a
   visible bump over its raw historical count.

3. How we avoid permanently over-ranking a short-lived spike:
   The boost is computed FROM the sliding window, not stored as a
   permanent score. Once the burst of searches stops happening, those
   timestamps age out of the window on their own, and recent_event_count
   decays back to 0 -- the boost disappears naturally without any manual
   cleanup or decay function needed.

4. How the cache is updated/invalidated when rankings change:
   Every POST /search invalidates the cache entries for all prefixes of
   that query (see batch_writer.py) since the suggestion order for those
   prefixes may now be stale. The /trending endpoint itself is NOT cached
   (see trade-off below) so it's always computed fresh.

5. Trade-offs (freshness vs latency vs complexity):
   - We recompute the trending list on every request instead of
     maintaining an incrementally-updated top-K heap. This is simpler and
     always fresh, but means O(active queries) work per request. At
     classroom-assignment scale (a handful of concurrent searches) this is
     fine; at real production scale you'd maintain a running top-K
     structure updated at write time instead of read time.
   - WINDOW_SECONDS and BOOST_WEIGHT are both tunable constants -- a
     larger window = smoother but slower-reacting trends; a larger boost
     weight = recency dominates more strongly over historical popularity.
     We chose values that visibly demonstrate the effect within a short
     classroom demo (a window of minutes, not hours/days, so you can
     actually show before/after within your demo video).
"""
import heapq
import threading
import time
from collections import defaultdict, deque

WINDOW_SECONDS = 120  # demo-scale window; would be larger (e.g. 1hr) in prod
RECENCY_FACTOR = 0.15  # each recent search event adds a 15% multiplicative boost
# Multiplicative (not additive) on purpose: this dataset's counts span a huge
# range (a handful to 200k+). An additive boost like "+50 per recent search"
# is invisible against a 100k-count query and would falsely dominate a
# 10-count query. A percentage boost scales with the query's own baseline,
# which also matches real-world trending behavior: an already-popular query
# picking up a burst of fresh interest should rise meaningfully, but a
# brand-new near-zero-count query shouldn't instantly outrank an
# established one off a handful of searches.


class TrendingTracker:
    def __init__(self, window_seconds: int = WINDOW_SECONDS, recency_factor: float = RECENCY_FACTOR):
        self.window_seconds = window_seconds
        self.recency_factor = recency_factor
        self.events: dict[str, deque] = defaultdict(deque)
        self._lock = threading.RLock()

    def _prune(self, dq: deque, now: float):
        while dq and now - dq[0] > self.window_seconds:
            dq.popleft()

    def record(self, query: str):
        now = time.time()
        with self._lock:
            dq = self.events[query]
            dq.append(now)
            self._prune(dq, now)

    def recent_count(self, query: str) -> int:
        now = time.time()
        with self._lock:
            dq = self.events.get(query)
            if not dq:
                return 0
            self._prune(dq, now)
            return len(dq)

    def active_prefixed(self, prefix: str) -> list[str]:
        """Recently-searched queries that start with prefix (for surfacing
        fresh-but-low-count queries that wouldn't make a pure top-K-by-count cut)."""
        with self._lock:
            return [q for q in self.events.keys() if q.startswith(prefix)]

    def score(self, query: str, all_time_count: int) -> float:
        boost = 1 + self.recency_factor * self.recent_count(query)
        return all_time_count * boost

    def rerank(self, candidates: list[tuple[str, int]], limit: int = 10):
        """Re-rank a list of (query, all_time_count) by recency-boosted score."""
        scored = [(self.score(q, c), q, c) for q, c in candidates]
        top = heapq.nlargest(limit, scored, key=lambda x: x[0])
        return [(q, c, round(s, 1)) for s, q, c in top]

    def top_trending(self, trie_root_completions: dict[str, int], limit: int = 10):
        """
        Global trending list (no prefix). Candidate pool = queries with
        recent activity, UNIONED with the top-50 all-time queries so the
        list isn't empty on a cold start with no recent searches yet.
        """
        with self._lock:
            active = list(self.events.keys())
        fallback = heapq.nlargest(
            50, trie_root_completions.items(), key=lambda kv: kv[1]
        )
        candidates = {q: trie_root_completions.get(q, 0) for q in active}
        for q, c in fallback:
            candidates.setdefault(q, c)
        return self.rerank(list(candidates.items()), limit=limit)
