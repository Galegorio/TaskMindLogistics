"""Teste de estresse: mede tempo e pico de memória (tracemalloc) e aponta gargalos.

Uso:  python benchmarks/stress.py
"""
from __future__ import annotations

import cProfile
import pstats
import random
import sys
import tempfile
import threading
import time
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.domain.exceptions import CycleError  # noqa: E402
from app.domain.models import Courier, Graph, Order, OrderStatus  # noqa: E402
from app.repositories.orders import OrderRepository  # noqa: E402
from app.services.assignment import assign_orders  # noqa: E402
from app.services.dependencies import validate_dependencies  # noqa: E402
from app.services.prioritization import rank_orders  # noqa: E402
from app.services.routing import RouteCache, shortest_path  # noqa: E402
from app.services.simulation import simulate  # noqa: E402

ROWS: list[tuple[str, str, str]] = []


def measure(name: str, fn, note: str = ""):
    """Executa ``fn`` medindo tempo (sem tracemalloc) e pico de memória (em uma segunda execução)."""
    t0 = time.perf_counter()
    result = fn()
    elapsed = time.perf_counter() - t0
    tracemalloc.start()
    fn()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    ROWS.append((name, f"{elapsed * 1000:,.0f} ms", f"{peak / 1e6:,.1f} MB"))
    print(f"{name:58s} {elapsed * 1000:9,.0f} ms  pico {peak / 1e6:7,.1f} MB  {note}")
    return result


def orders(n: int, seed: int = 1, deps: bool = False) -> list[Order]:
    rng = random.Random(seed)
    out = []
    for i in range(n):
        created = rng.randint(0, 500)
        d = tuple(f"P{rng.randrange(i)}" for _ in range(3)) if deps and i else ()
        out.append(Order(f"P{i}", "x", rng.randint(1, 5), created, created + rng.randint(0, 600), rng.randint(1, 20),
                         f"L{rng.randrange(200)}", d))
    return out


def graph(n: int, seed: int = 2) -> Graph:
    rng = random.Random(seed)
    g = Graph()
    for i in range(1, n):
        g.add_edge(f"L{i}", f"L{rng.randrange(i)}", rng.randint(1, 30))
    for _ in range(3 * n):
        g.add_edge(f"L{rng.randrange(n)}", f"L{rng.randrange(n)}", rng.randint(1, 30))
    return g


class CountingGraph(Graph):
    expansions = 0

    def neighbors(self, node):
        CountingGraph.expansions += 1
        return super().neighbors(node)


def main() -> None:
    print("== Estresse TaskMind ==")
    o10k = orders(10_000)
    measure("rank_orders: 10.000 pedidos", lambda: rank_orders(o10k, 200))
    measure("validate_dependencies: 10.000 pedidos, ~30.000 arestas", lambda: validate_dependencies(orders(10_000, deps=True)),
            "(inclui criar os pedidos)")

    chain = [Order("P0", "x", 3, 0, 10, 1, "A")] + [Order(f"P{i}", "x", 3, 0, 10, 1, "A", (f"P{i-1}",)) for i in range(1, 10_000)]
    chain[0] = Order("P0", "x", 3, 0, 10, 1, "A", ("P9999",))  # fecha o ciclo
    def cyc():
        try:
            validate_dependencies(chain)
        except CycleError as e:
            return e
    e = measure("ciclo gigante: 10.000 pedidos em anel", cyc)
    print(f"    -> rejeitado; ciclo com {len(e.cycle) - 1} pedidos: {' -> '.join(e.cycle[:4])} -> ...")

    g5k = graph(5_000)
    rng = random.Random(4)
    pairs = [(f"L{rng.randrange(5000)}", f"L{rng.randrange(5000)}") for _ in range(200)]
    measure("shortest_path: 200 consultas aleatórias, mapa de 5.000 locais", lambda: [shortest_path(g5k, a, b) for a, b in pairs])

    cg = CountingGraph()
    for i in range(5_000):
        cg.add_edge(f"L{i}", f"L{i+1}", 1)
    CountingGraph.expansions = 0
    shortest_path(cg, "L0", "L10")
    near = CountingGraph.expansions
    CountingGraph.expansions = 0
    shortest_path(cg, "L0", "L4999")
    far = CountingGraph.expansions
    print(f"    -> locais expandidos: destino a 10 saltos = {near}; destino no outro extremo = {far} (de 5.001)")

    g300 = graph(300)
    o1k = orders(1_000)
    cs = [Courier(f"D{i}", f"L{i * 7}", 400, 0, True) for i in range(10)]
    plan = measure("assign_orders: 1.000 pedidos x 10 entregadores, mapa 300", lambda: assign_orders(o1k, cs, g300, 200),
                   "")
    print(f"    -> atribuídos {len(plan.assignments)}, sem atribuição {len(plan.unassigned)}, em risco {len(plan.at_risk)}")
    cache = RouteCache(g300)
    prof = cProfile.Profile()
    prof.enable()
    assign_orders(o1k, cs, g300, 200, cache)
    prof.disable()
    st = pstats.Stats(prof)
    total = st.total_tt
    dj = sum(v[3] for k, v in st.stats.items() if k[2] == "shortest_path")
    print(f"    -> Dijkstra (shortest_path) = {dj / total:.0%} do tempo; {len(cache._memo)} rotas distintas calculadas")

    ev_rng = random.Random(8)
    o200 = orders(200, seed=3)
    cs20 = [Courier(f"D{i}", f"L{i * 11}", 300, 0, True) for i in range(20)]
    events = []
    for k in range(2_000):
        kind = ev_rng.choice(["ORDER_CANCELLED", "ORDER_DELIVERED", "COURIER_UNAVAILABLE", "COURIER_AVAILABLE"])
        key = "order_id" if kind.startswith("ORDER") else "courier_id"
        val = f"P{ev_rng.randrange(200)}" if key == "order_id" else f"D{ev_rng.randrange(20)}"
        events.append({"id": f"E{k}", "time": k // 4, "type": kind, key: val})
    res = measure("simulate: 200 pedidos, 20 entregadores, 2.000 eventos", lambda: simulate(o200, cs20, g300, events))
    outcomes = {}
    for h in res.history:
        outcomes[h["outcome"]] = outcomes.get(h["outcome"], 0) + 1
    print(f"    -> desfechos: {outcomes}")
    prof = cProfile.Profile()
    prof.enable()
    simulate(o200, cs20, g300, events)
    prof.disable()
    st = pstats.Stats(prof)
    rank_t = sum(v[3] for k, v in st.stats.items() if k[2] in ("rank_orders",))
    print(f"    -> replanejar a cada evento: rank_orders = {rank_t / st.total_tt:.0%} do tempo total")

    with tempfile.TemporaryDirectory() as d:
        repo = OrderRepository(Path(d) / "s.db")
        for i in range(8):
            repo.add_courier(Courier(f"D{i}", "A", 1e9))
        for i in range(400):
            repo.add_order(Order(f"S{i}", "x", 3, 0, 100, 1, "B"))
        t0 = time.perf_counter()
        for i in range(400):
            repo.process_order(f"S{i}", "D0", f"seq-{i}")
        seq = time.perf_counter() - t0
        for i in range(400):
            repo.add_order(Order(f"C{i}", "x", 3, 0, 100, 1, "B"))
        wins = []

        def job(i: int):
            try:
                repo.process_order(f"C{i % 100}", f"D{i % 8}", f"conc-{i}")
                wins.append(i)
            except Exception:
                pass

        t0 = time.perf_counter()
        with ThreadPoolExecutor(8) as pool:
            list(pool.map(job, range(400)))
        conc = time.perf_counter() - t0
        distinct = len(repo.assignments()) - 400
        print(f"{'SQLite: 400 atribuições sequenciais':58s} {seq * 1000:9,.0f} ms  ({400 / seq:,.0f} ops/s, 1 commit cada)")
        print(f"{'SQLite: 400 requisições concorrentes (8 threads, 100 pedidos)':58s} {conc * 1000:9,.0f} ms  "
              f"({len(wins)} sucessos; {distinct} atribuições gravadas; esperado 100 = sem duplicidade)")
        ROWS.append(("SQLite 400 sequenciais", f"{seq * 1000:,.0f} ms", "-"))
        ROWS.append(("SQLite 400 concorrentes (100 pedidos)", f"{conc * 1000:,.0f} ms", "-"))


if __name__ == "__main__":
    main()
