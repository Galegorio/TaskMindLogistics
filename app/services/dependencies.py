"""Missão 2 - Dependências entre pedidos: validação, ciclos, ordem topológica e bloqueios."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Iterable, Mapping

from app.domain.exceptions import CycleError, DuplicateOrderError, UnknownDependencyError
from app.domain.models import Order, OrderStatus


@dataclass(frozen=True)
class DependencyReport:
    """Resultado da validação: ordem de execução válida e pedidos bloqueados (id -> deps não entregues)."""

    execution_order: tuple[str, ...]
    blocked: Mapping[str, tuple[str, ...]]


def index_orders(orders: Iterable[Order]) -> dict[str, Order]:
    """Indexa pedidos por ID, rejeitando IDs duplicados. O(n)."""
    by_id: dict[str, Order] = {}
    for o in orders:
        if o.id in by_id:
            raise DuplicateOrderError(f"ID de pedido duplicado: {o.id!r}")
        by_id[o.id] = o
    return by_id


def unsatisfied_dependencies(order: Order, by_id: Mapping[str, Order]) -> tuple[str, ...]:
    """Dependências ainda não entregues. ID inexistente ou dependência cancelada contam como não satisfeitas."""
    return tuple(
        d for d in order.dependencies
        if (dep := by_id.get(d)) is None or dep.status is not OrderStatus.DELIVERED
    )


def find_blocked(orders: Iterable[Order]) -> dict[str, tuple[str, ...]]:
    """Pedidos ainda executáveis (pending/blocked) que têm dependências não entregues. O(V+E)."""
    olist = list(orders)
    by_id = index_orders(olist)
    blocked: dict[str, tuple[str, ...]] = {}
    for o in olist:
        if o.status in (OrderStatus.PENDING, OrderStatus.BLOCKED):
            pending = unsatisfied_dependencies(o, by_id)
            if pending:
                blocked[o.id] = pending
    return blocked


def validate_dependencies(orders: Iterable[Order]) -> DependencyReport:
    """Valida o grafo de dependências e devolve uma ordem de execução.

    Levanta ``DuplicateOrderError``, ``UnknownDependencyError`` ou ``CycleError`` (com o caminho do ciclo).
    Autodependência e dependências repetidas já são tratadas pelo próprio ``Order`` (na construção). Algoritmo: Kahn iterativo (sem recursão, seguro para cadeias muito longas),
    O(V+E). Aresta ``dependência -> pedido``: um pedido só entra na ordem após todas as suas dependências.
    """
    olist = list(orders)
    by_id = index_orders(olist)

    missing: list[tuple[str, str]] = []
    deps_of: dict[str, tuple[str, ...]] = {}
    for o in olist:
        for d in o.dependencies:
            if d not in by_id:
                missing.append((o.id, d))
        deps_of[o.id] = o.dependencies
    if missing:
        raise UnknownDependencyError(missing)

    indegree = {oid: len(deps) for oid, deps in deps_of.items()}
    dependents: dict[str, list[str]] = {oid: [] for oid in by_id}
    for oid, deps in deps_of.items():
        for d in deps:
            dependents[d].append(oid)

    queue = deque(oid for oid in by_id if indegree[oid] == 0)
    order: list[str] = []
    while queue:
        node = queue.popleft()
        order.append(node)
        for nxt in dependents[node]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)

    if len(order) < len(by_id):
        raise CycleError(_find_cycle(by_id, deps_of, {oid for oid, d in indegree.items() if d > 0}))
    return DependencyReport(tuple(order), find_blocked(olist))


def _find_cycle(by_id: Mapping[str, Order], deps_of: Mapping[str, tuple[str, ...]], remaining: set[str]) -> list[str]:
    """Extrai um ciclo concreto dos nós que a ordenação topológica não conseguiu processar.

    Todo nó restante depende de pelo menos outro nó restante; seguir essas dependências
    sem sair do conjunto termina obrigatoriamente em um ciclo. O(V).
    """
    start = next(oid for oid in by_id if oid in remaining)
    path = [start]
    position = {start: 0}
    current = start
    while True:
        nxt = next(d for d in deps_of[current] if d in remaining)
        if nxt in position:
            return path[position[nxt]:] + [nxt]
        position[nxt] = len(path)
        path.append(nxt)
        current = nxt
