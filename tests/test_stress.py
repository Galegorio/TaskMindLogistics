"""Cenários de estresse (corretude em escala; sem metas arbitrárias de velocidade)."""
import random
import time

from app.domain.models import Graph, OrderStatus
from app.services.dependencies import validate_dependencies
from app.services.prioritization import rank_key, rank_orders
from app.services.routing import shortest_path
from tests.factories import make_order


def make_orders(n, seed=1):
    rng = random.Random(seed)
    out = []
    for i in range(n):
        created = rng.randint(0, 500)
        out.append(make_order(f"P{i:06d}", priority=rng.randint(1, 5), created_at=created,
                              deadline=created + rng.randint(0, 600),
                              status=rng.choice([OrderStatus.PENDING] * 8 + [OrderStatus.CANCELLED, OrderStatus.DELIVERED])))
    return out


def best_of(fn, repeats=3):
    best = float("inf")
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best


def test_10k_orders_are_ranked_correctly():
    orders = make_orders(10_000)
    ranked = rank_orders(orders, 200)
    keys = [rank_key(o, 200) for o in ranked]
    assert keys == sorted(keys)
    assert len(ranked) == sum(o.status is OrderStatus.PENDING for o in orders)  # só pendentes


def test_ranking_scales_like_n_log_n_not_quadratic():
    small, large = make_orders(5_000), make_orders(20_000)
    ratio = best_of(lambda: rank_orders(large, 200)) / best_of(lambda: rank_orders(small, 200))
    assert ratio < 9  # 4x mais dados: ~4-5x em O(n log n); um O(n^2) daria ~16x


def test_dependency_validation_scales_linearly_on_wide_dag():
    def build(n):
        rng = random.Random(5)
        return [make_order(f"P{i}", dependencies=tuple(f"P{rng.randrange(i)}" for _ in range(3)) if i else ()) for i in range(n)]
    small, large = build(5_000), build(20_000)
    ratio = best_of(lambda: validate_dependencies(large)) / best_of(lambda: validate_dependencies(small))
    assert ratio < 9
    assert len(validate_dependencies(large).execution_order) == 20_000


def build_big_graph(n=5_000, seed=2):
    rng = random.Random(seed)
    g = Graph()
    for i in range(1, n):
        g.add_edge(f"L{i}", f"L{rng.randrange(i)}", rng.randint(1, 30))  # árvore geradora (conectado)
    for _ in range(3 * n):
        g.add_edge(f"L{rng.randrange(n)}", f"L{rng.randrange(n)}", rng.randint(1, 30))
    return g


def test_5000_node_map_routes_are_valid_and_obey_triangle_inequality():
    g = build_big_graph()
    rng = random.Random(9)
    for _ in range(40):
        a, b, c = (f"L{rng.randrange(5000)}" for _ in range(3))
        ab, bc, ac = shortest_path(g, a, b), shortest_path(g, b, c), shortest_path(g, a, c)
        assert ab.found and bc.found and ac.found
        assert ac.total_time <= ab.total_time + bc.total_time
        assert sum(g.neighbors(x)[y] for x, y in zip(ac.path, ac.path[1:])) == ac.total_time


def test_5000_node_map_with_unreachable_target_terminates():
    g = build_big_graph()
    g.add_edge("ISLAND1", "ISLAND2", 1)
    assert shortest_path(g, "L0", "ISLAND2").found is False
