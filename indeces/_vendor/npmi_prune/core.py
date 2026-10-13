"""core.py — frozen graph loading (hash-pinned) and the v1.1 ranking on a draft (set of removed nodes).
The computation mirrors reference_rank_v11.py operation by operation (same edge order, same np.add.at accumulation),
so with no removals the alpha_meet top-10 is identical to the frozen reference output."""
import json, hashlib, collections, heapq, math, os
import numpy as np

PINNED_V302 = {  # graph v3.0.2 release files
    "nodes_v3.jsonl": "2a3e3f7ff2ce1f954fce53aa2b83a4f214f27e1e7d7cae32e5fdc94f4a7599c7",
    "edges_v3.jsonl": "c408b67c45c1b73d6bf04af7212169f3155b1538a20990751e27dc4375d1bf18",
    "member_to_cid.json": "290befb5d5dfc500ab0ddf45a78bc828191651f7b877fef5e4a50ea8b92f06d1",
}
PINNED_INDECES = {  # Indeces-paper-graph-minimal-20261010 package files used for evidence
    "word_records.jsonl": "da7f74a07e68445acc715762f90be038ad909e16bccda875dbe15f9f12c17cf6",
    "chunk_locations.jsonl": "c96f31c98ad0caed98aa2b760460a6617402b767d523376e20319328fa1fd238",
}

def _sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""): h.update(chunk)
    return h.hexdigest()

class Graph:
    """Read-only. Never mutated after construction."""
    def __init__(self, graph_dir, indeces_dir, verify_hashes=True):
        self.graph_dir, self.indeces_dir = graph_dir, indeces_dir
        self.hashes = {}
        for d, pins in ((graph_dir, PINNED_V302), (indeces_dir, PINNED_INDECES)):
            for fn, want in pins.items():
                got = _sha(os.path.join(d, fn)); self.hashes[fn] = got
                if verify_hashes and got != want:
                    raise RuntimeError(f"HASH MISMATCH {fn}: expected {want}, got {got}")
        self.nodes = {}
        for l in open(os.path.join(graph_dir, "nodes_v3.jsonl")):
            x = json.loads(l); self.nodes[x["cid"]] = x
        self.m2c = {int(k): v for k, v in json.load(open(os.path.join(graph_dir, "member_to_cid.json"))).items()}
        self.ids = sorted(self.nodes); self.ix = {n: i for i, n in enumerate(self.ids)}; self.V = len(self.ids)
        ea, eb, ew = [], [], []
        self.nbr = collections.defaultdict(dict); self.edge = {}
        for l in open(os.path.join(graph_dir, "edges_v3.jsonl")):
            e = json.loads(l)
            ea.append(self.ix[e["a"]]); eb.append(self.ix[e["b"]]); ew.append(e["npmi"])
            self.nbr[e["a"]][e["b"]] = e["npmi"]; self.nbr[e["b"]][e["a"]] = e["npmi"]
            self.edge[(e["a"], e["b"])] = self.edge[(e["b"], e["a"])] = (e["npmi"], e.get("co_count"))
        self.ea, self.eb, self.ew = np.array(ea), np.array(eb), np.array(ew, dtype=float)
        # concept -> records (through member node ids), record -> (source_id, pages)
        self.crec = collections.defaultdict(set)
        for l in open(os.path.join(indeces_dir, "word_records.jsonl")):
            x = json.loads(l); self.crec[self.m2c[x["node_id"]]].add(x["record_id"])
        self.rloc = {}
        for l in open(os.path.join(indeces_dir, "chunk_locations.jsonl")):
            x = json.loads(l); self.rloc[x["record_id"]] = (x["source_id"], x["pdf_page_numbers"])

    # ----- draft-aware arrays, built exactly like reference_rank_v11 (interleaved a->b, b->a in file order) -----
    def arrays(self, removed):
        if removed:
            rm = np.zeros(self.V, bool); rm[[self.ix[n] for n in removed]] = True
            keep = ~(rm[self.ea] | rm[self.eb])
        else:
            keep = np.ones(len(self.ea), bool)
        a, b, w = self.ea[keep], self.eb[keep], self.ew[keep]
        rows = np.empty(2 * len(a), dtype=int); cols = np.empty(2 * len(a), dtype=int); vals = np.empty(2 * len(a))
        rows[0::2], rows[1::2] = a, b; cols[0::2], cols[1::2] = b, a; vals[0::2], vals[1::2] = w, w
        deg = np.zeros(self.V); np.add.at(deg, rows, vals); deg[deg == 0] = 1
        return rows, cols, vals, deg

    def alpha(self, arr, seeds):
        rows, cols, vals, deg = arr
        s0 = np.zeros(self.V); s0[[self.ix[n] for n in sorted(set(seeds))]] = 1; s0 = s0 / s0.sum(); r = s0.copy()
        for _ in range(40):
            y = np.zeros(self.V); np.add.at(y, cols, r[rows] * vals / deg[rows]); r = 0.5 * s0 + 0.5 * y
        return r

    def in_target(self, cid, target, level):
        nd = self.nodes[cid]
        return (nd["fine"] == target) if level == "fine" else (nd["coarse"] == target)

    def rank(self, A, B, target, level, removed=frozenset(), k=10):
        seeds = set(A) | set(B)
        pool = [n for n in self.ids if n not in seeds and n not in removed and self.in_target(n, target, level)]
        arr = self.arrays(removed)
        sc = np.sqrt(self.alpha(arr, A) * self.alpha(arr, B)) if B else self.alpha(arr, A)
        top = sorted(pool, key=lambda n: (-sc[self.ix[n]], n))[:k]
        return top, {n: float(sc[self.ix[n]]) for n in top}, len(pool)

    def best_path(self, sources, dst, removed=frozenset(), cap=4):
        """Path maximising the product of NPMI (Dijkstra on -ln w), at most `cap` hops, avoiding removed nodes."""
        best = {s: (0.0, [s]) for s in sources if s not in removed}
        heap = [(0.0, s, [s]) for s in best]
        heapq.heapify(heap)
        while heap:
            d, u, path = heapq.heappop(heap)
            if u == dst: return path
            if d > best.get(u, (math.inf,))[0] or len(path) > cap: continue
            for v, w in self.nbr[u].items():
                if v in removed or w <= 0: continue
                nd = d - math.log(w)
                if nd < best.get(v, (math.inf,))[0]:
                    best[v] = (nd, path + [v]); heapq.heappush(heap, (nd, v, path + [v]))
        return None

    def edge_records(self, a, b, limit=2):
        return sorted(self.crec[a] & self.crec[b])[:limit]
