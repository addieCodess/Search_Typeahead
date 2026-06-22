"""
Trie-based prefix index.

Design: each TrieNode keeps a dict {full_query: count} for every query that
passes through that node's prefix. On suggest(prefix), we walk to the node
for that prefix and return the top 10 entries by count (heapq.nlargest).

Why this over a SQL "LIKE 'prefix%'" query: O(len(prefix)) to locate the
node, then O(k log 10) to get the top suggestions, vs. a table scan or
index range scan on every keystroke. Trade-off: extra memory (~proportional
to sum of query lengths), which is acceptable at this dataset size
(100k-1M queries).

update_count() is called by the batch writer after it flushes to SQLite,
keeping the Trie in sync with the source of truth.
"""
import heapq
import threading


class TrieNode:
    __slots__ = ("children", "completions")

    def __init__(self):
        self.children: dict[str, "TrieNode"] = {}
        self.completions: dict[str, int] = {}  # full_query -> count


class Trie:
    def __init__(self):
        self.root = TrieNode()
        self._lock = threading.RLock()  # batch flush + reads can overlap

    def insert(self, query: str, count: int):
        """Insert or overwrite a query's count. Idempotent."""
        query = query.strip().lower()
        if not query:
            return
        with self._lock:
            node = self.root
            node.completions[query] = count
            for ch in query:
                node = node.children.setdefault(ch, TrieNode())
                node.completions[query] = count

    def increment(self, query: str, delta: int = 1) -> int:
        """Increment a query's count, return new count."""
        query = query.strip().lower()
        if not query:
            return 0
        with self._lock:
            node = self.root
            new_count = node.completions.get(query, 0) + delta
            self.insert(query, new_count)
            return new_count

    def suggest(self, prefix: str, limit: int = 10) -> list[tuple[str, int]]:
        prefix = prefix.strip().lower()
        if not prefix:
            return []
        node = self.root
        for ch in prefix:
            node = node.children.get(ch)
            if node is None:
                return []
        with self._lock:
            return heapq.nlargest(
                limit, node.completions.items(), key=lambda kv: kv[1]
            )

    @classmethod
    def build_from_pairs(cls, pairs: list[tuple[str, int]]) -> "Trie":
        trie = cls()
        for query, count in pairs:
            trie.insert(query, count)
        return trie
