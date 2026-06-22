"""
Consistent hashing cache layer.

WHY consistent hashing (viva answer): with N cache nodes and a naive
hash(prefix) % N routing scheme, adding/removing a single node remaps
almost ALL keys to different nodes -> instant mass cache miss ("cache
stampede"). Consistent hashing places both nodes and keys on a hash ring;
losing/adding one node only remaps the keys between it and its neighbor,
leaving the rest of the ring untouched.

WHY virtual nodes: with only N real nodes on the ring, key distribution
across them is lumpy (some nodes get a much bigger arc of the ring than
others). Giving each real node V virtual points scattered around the ring
averages this out -> much more even load distribution.

WHAT is actually "distributed" here: for a local/academic deployment, the
N nodes are simulated as separate in-process cache dicts rather than
separate machines/containers. The routing logic (ring, hashing, ownership)
is identical to what you'd use with N real Redis instances -- only the
transport changes. This is a documented trade-off: real network-isolated
nodes would demonstrate actual fault tolerance, but would burn most of an
already-tight time budget for a local class assignment. Swapping each
SimpleCacheNode for a redis.Redis(host=node_host) client is a small,
well-contained change if you want to extend this later.
"""
import bisect
import hashlib
import threading
import time
from collections import OrderedDict


def _hash(key: str) -> int:
    return int(hashlib.md5(key.encode()).hexdigest(), 16)


class SimpleCacheNode:
    """One logical cache shard: a bounded LRU dict with TTL per entry."""

    def __init__(self, node_id: str, capacity: int = 5000, ttl_seconds: int = 60):
        self.node_id = node_id
        self.capacity = capacity
        self.ttl_seconds = ttl_seconds
        self._store: OrderedDict[str, tuple[float, object]] = OrderedDict()
        self._lock = threading.RLock()
        self.hits = 0
        self.misses = 0

    def get(self, key: str):
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self.misses += 1
                return None
            ts, value = entry
            if time.time() - ts > self.ttl_seconds:
                del self._store[key]
                self.misses += 1
                return None
            self._store.move_to_end(key)
            self.hits += 1
            return value

    def set(self, key: str, value):
        with self._lock:
            self._store[key] = (time.time(), value)
            self._store.move_to_end(key)
            if len(self._store) > self.capacity:
                self._store.popitem(last=False)  # evict least recently used

    def invalidate(self, key: str):
        with self._lock:
            self._store.pop(key, None)

    def stats(self):
        total = self.hits + self.misses
        hit_rate = self.hits / total if total else 0.0
        return {
            "node_id": self.node_id,
            "size": len(self._store),
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(hit_rate, 4),
        }


class ConsistentHashRing:
    def __init__(self, nodes: list[str], virtual_nodes: int = 150):
        self.virtual_nodes = virtual_nodes
        self.ring: dict[int, str] = {}
        self.sorted_keys: list[int] = []
        for node in nodes:
            self.add_node(node)

    def add_node(self, node_id: str):
        for i in range(self.virtual_nodes):
            h = _hash(f"{node_id}#{i}")
            self.ring[h] = node_id
            bisect.insort(self.sorted_keys, h)

    def remove_node(self, node_id: str):
        for i in range(self.virtual_nodes):
            h = _hash(f"{node_id}#{i}")
            self.ring.pop(h, None)
            idx = bisect.bisect_left(self.sorted_keys, h)
            if idx < len(self.sorted_keys) and self.sorted_keys[idx] == h:
                self.sorted_keys.pop(idx)

    def get_node(self, key: str) -> str:
        if not self.sorted_keys:
            raise RuntimeError("No cache nodes available")
        h = _hash(key)
        idx = bisect.bisect(self.sorted_keys, h) % len(self.sorted_keys)
        return self.ring[self.sorted_keys[idx]]


class DistributedCache:
    """Routes prefix lookups to a shard via consistent hashing."""

    def __init__(self, node_ids: list[str] | None = None):
        node_ids = node_ids or ["cache-node-1", "cache-node-2", "cache-node-3"]
        self.nodes: dict[str, SimpleCacheNode] = {
            nid: SimpleCacheNode(nid) for nid in node_ids
        }
        self.ring = ConsistentHashRing(list(self.nodes.keys()))

    def route(self, key: str) -> SimpleCacheNode:
        node_id = self.ring.get_node(key)
        return self.nodes[node_id]

    def get(self, key: str):
        return self.route(key).get(key)

    def set(self, key: str, value):
        self.route(key).set(key, value)

    def invalidate(self, key: str):
        self.route(key).invalidate(key)

    def debug_route(self, key: str):
        node = self.route(key)
        cached = node.get(key)  # NOTE: this also counts as a hit/miss touch
        return {
            "key": key,
            "routed_to": node.node_id,
            "currently_cached": cached is not None,
        }

    def stats(self):
        return [node.stats() for node in self.nodes.values()]
