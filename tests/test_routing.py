"""Missão 3."""
import itertools
import math
import random

import pytest

from app.domain.exceptions import NegativeWeightError, UnknownLocationError
from app.domain.models import Graph
from app.services.routing import RouteCache, shortest_path
from tests.factories import classic_graph, make_graph


def test_statement_example_prefers_indirect_route():
    r = shortest_path(classic_graph(), "A", "C")
    assert r.found and r.path == ("A", "B", "C") and r.total_time == 12


def test_roads_are_bidirectional():
    r = shortest_path(classic_graph(), "C", "A")
    assert r.path == ("C", "B", "A") and r.total_time == 12


def test_same_origin_and_destination():
    r = shortest_path(classic_graph(), "B", "B")
    assert r.found and r.path == ("B",) and r.total_time == 0


def test_disconnected_returns_clear_result():
    g = make_graph(("A", "B", 1), ("X", "Y", 1))
    r = shortest_path(g, "A", "Y")
    assert r.found is False and r.path == () and r.total_time is None


def test_isolated_node():
    g = Graph()
    g.add_node("Z")
    g.add_edge("A", "B", 1)
    assert shortest_path(g, "A", "Z").found is False


def test_unknown_location_raises():
    with pytest.raises(UnknownLocationError, match="'Q'"):
        shortest_path(classic_graph(), "A", "Q")
    with pytest.raises(UnknownLocationError):
        shortest_path(classic_graph(), "Q", "A")


def test_negative_weight_rejected_at_build_time():
    g = Graph()
    with pytest.raises(NegativeWeightError):
        g.add_edge("A", "B", -0.5)
    assert "A" not in g or "B" not in g.neighbors("A")


def test_zero_weight_edges_and_parallel_edges_keep_minimum():
    g = make_graph(("A", "B", 9), ("A", "B", 4), ("B", "C", 0))
    r = shortest_path(g, "A", "C")
    assert r.total_time == 4 and r.path == ("A", "B", "C")


def test_accepts_dict_graph():
    r = shortest_path({"edges": [{"from": "A", "to": "B", "time": 3}]}, "A", "B")
    assert r.total_time == 3


def _floyd(nodes, edges):
    d = {(a, b): (0 if a == b else math.inf) for a in nodes for b in nodes}
    for a, b, t in edges:
        d[(a, b)] = min(d[(a, b)], t)
        d[(b, a)] = min(d[(b, a)], t)
    for k, i, j in itertools.product(nodes, nodes, nodes):
        if d[(i, k)] + d[(k, j)] < d[(i, j)]:
            d[(i, j)] = d[(i, k)] + d[(k, j)]
    return d


def test_matches_floyd_warshall_on_random_graphs_including_disconnected():
    rng = random.Random(3)
    for _ in range(15):
        nodes = [f"N{i}" for i in range(14)]
        edges = [(rng.choice(nodes), rng.choice(nodes), rng.randint(0, 20)) for _ in range(rng.randint(5, 25))]
        g = Graph()
        for n in nodes:
            g.add_node(n)
        for a, b, t in edges:
            g.add_edge(a, b, t)
        ref = _floyd(nodes, edges)
        for a, b in itertools.product(nodes, nodes):
            r = shortest_path(g, a, b)
            if math.isinf(ref[(a, b)]):
                assert not r.found
            else:
                assert r.found and r.total_time == ref[(a, b)]
                assert r.path[0] == a and r.path[-1] == b
                assert sum(g.neighbors(x)[y] for x, y in zip(r.path, r.path[1:])) == r.total_time


class CountingGraph(Graph):
    def __init__(self):
        super().__init__()
        self.expansions = 0

    def neighbors(self, node):
        self.expansions += 1
        return super().neighbors(node)


def test_stops_early_instead_of_scanning_whole_map():
    g = CountingGraph()
    for i in range(2000):
        g.add_edge(f"N{i}", f"N{i+1}", 1)
    r = shortest_path(g, "N0", "N3")
    assert r.total_time == 3
    assert g.expansions <= 4  # N0..N3, nunca as outras ~2000 posições


def test_route_cache_memoizes():
    g = CountingGraph()
    for i in range(50):
        g.add_edge(f"N{i}", f"N{i+1}", 1)
    cache = RouteCache(g)
    first = cache.get("N0", "N20")
    used = g.expansions
    assert cache.get("N0", "N20") is first and g.expansions == used
