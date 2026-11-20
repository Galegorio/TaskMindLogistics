"""Fábricas de objetos para os testes."""
from __future__ import annotations

from app.domain.models import Courier, Graph, Order


def make_order(id: str = "P1", **kw) -> Order:
    base = dict(title=f"Pedido {id}", priority=3, created_at=0, deadline=1000, weight=1, destination="B")
    base.update(kw)
    return Order(id=id, **base)


def make_courier(id: str = "D1", **kw) -> Courier:
    base = dict(location="A", capacity=100, available_at=0, active=True)
    base.update(kw)
    return Courier(id=id, **base)


def make_graph(*edges: tuple) -> Graph:
    g = Graph()
    for a, b, t in edges:
        g.add_edge(a, b, t)
    return g


def classic_graph() -> Graph:
    """A-B 5, B-C 7, A-C 20 (exemplo do enunciado), C-D 3."""
    return make_graph(("A", "B", 5), ("B", "C", 7), ("A", "C", 20), ("C", "D", 3))
