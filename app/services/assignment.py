"""Missão 4 - Distribuição de pedidos entre entregadores (heurística gulosa).

Decisões e limitações (também no README):
* Os pedidos são processados na ordem de ``rank_orders`` (urgentes primeiro). Para cada pedido escolhemos o
  entregador que minimiza, nesta ordem: (1) perder o prazo (0/1), (2) tempo de viagem, (3) ID do entregador.
* É GULOSA: decide pedido a pedido e nunca volta atrás. Pode, por exemplo, usar o entregador mais próximo
  em um pedido folgado e deixar um pedido seguinte sem entregador que chegue no prazo. O ótimo global é um
  problema NP-difícil (variação de VRP / atribuição com janelas de tempo); não é resolvido aqui.
* Cada entregador leva uma carga única por rodada: a soma dos pesos atribuídos (mais a carga já
  comprometida) não pode passar da capacidade. Os pedidos de um entregador são entregues em sequência:
  após cada atribuição ele "se move" para o destino e fica pronto no horário estimado de chegada.
* Prazo impossível NÃO bloqueia a atribuição: o pedido é atribuído ao entregador mais rápido e marcado
  ``late=True`` (risco sinalizado). Só fica sem atribuição se nenhum entregador for elegível.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Optional

from app.domain.exceptions import ValidationError
from app.domain.models import COMMITTED_STATUSES, Courier, Order, OrderStatus, with_status
from app.logging_config import log_event
from app.services.dependencies import find_blocked, index_orders
from app.services.prioritization import rank_orders
from app.services.routing import GraphLike, RouteCache, coerce_graph

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Assignment:
    """Pedido atribuído a um entregador. ``late=True`` sinaliza risco de perder o prazo."""

    order_id: str
    courier_id: str
    departure: float
    travel_time: float
    eta: float
    late: bool
    reason: str


@dataclass(frozen=True)
class Unassigned:
    """Pedido que permaneceu sem atribuição e o motivo."""

    order_id: str
    reason: str


@dataclass(frozen=True)
class AssignmentPlan:
    """Resultado de ``assign_orders`` (não altera as entradas)."""

    assignments: tuple[Assignment, ...] = ()
    unassigned: tuple[Unassigned, ...] = ()
    blocked: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def at_risk(self) -> tuple[Assignment, ...]:
        """Atribuições cujo ETA passa do prazo."""
        return tuple(a for a in self.assignments if a.late)


@dataclass
class _CourierState:
    location: str
    ready_at: float
    remaining: float


def assign_orders(orders: Iterable[Order], couriers: Iterable[Courier], graph: GraphLike,
                  current_time: float, route_cache: Optional[RouteCache] = None) -> AssignmentPlan:
    """Atribui pedidos elegíveis a entregadores ativos e disponíveis (``available_at <= current_time``).

    Complexidade: O(n log n) para ranquear + O(n * m) avaliações de rota (n pedidos, m entregadores),
    cada uma com Dijkstra memoizado por (origem, destino).
    """
    g = coerce_graph(graph)
    cache = route_cache or RouteCache(g)
    olist = list(orders)
    index_orders(olist)
    ranked = rank_orders(olist, current_time)
    blocked = find_blocked(olist)

    committed: dict[str, float] = defaultdict(float)
    for o in olist:
        if o.status in COMMITTED_STATUSES and o.assigned_to:
            committed[o.assigned_to] += o.weight

    states: dict[str, _CourierState] = {}
    seen: set[str] = set()
    for c in couriers:
        if c.id in seen:
            raise ValidationError(f"ID de entregador duplicado: {c.id!r}")
        seen.add(c.id)
        if c.active and c.available_at <= current_time:
            states[c.id] = _CourierState(c.location, current_time, c.capacity - committed[c.id])
    courier_ids = sorted(states)

    assignments: list[Assignment] = []
    unassigned: list[Unassigned] = []
    for order in ranked:
        if order.destination not in g:
            unassigned.append(Unassigned(order.id, f"destino {order.destination!r} não existe no mapa"))
            continue
        if not courier_ids:
            unassigned.append(Unassigned(order.id, "nenhum entregador ativo e disponível"))
            continue

        no_capacity = no_route = 0
        best: Optional[tuple[tuple, str, float, float, bool]] = None
        candidates = 0
        for cid in courier_ids:
            st = states[cid]
            if st.remaining < order.weight:
                no_capacity += 1
                continue
            if st.location not in g:
                no_route += 1
                continue
            route = cache.get(st.location, order.destination)
            if not route.found:
                no_route += 1
                continue
            candidates += 1
            eta = st.ready_at + route.total_time
            late = eta > order.deadline
            key = (1 if late else 0, route.total_time, cid)
            if best is None or key < best[0]:
                best = (key, cid, route.total_time, eta, late)

        if best is None:
            partes = []
            if no_capacity:
                partes.append(f"{no_capacity} sem capacidade para peso {order.weight:g}")
            if no_route:
                partes.append(f"{no_route} sem rota até {order.destination!r}")
            unassigned.append(Unassigned(order.id, "nenhum entregador elegível: " + "; ".join(partes)))
            continue

        _, cid, travel, eta, late = best
        st = states[cid]
        if late:
            motivo = (f"RISCO: ETA {eta:g} > prazo {order.deadline:g} (+{eta - order.deadline:g} min); "
                      f"era o entregador mais rápido entre {candidates} candidato(s)")
        else:
            motivo = (f"chega no prazo (ETA {eta:g} <= {order.deadline:g}); menor viagem ({travel:g} min) "
                      f"entre {candidates} candidato(s) no prazo, desempate por ID")
        assignments.append(Assignment(order.id, cid, st.ready_at, travel, eta, late, motivo))
        st.location, st.ready_at, st.remaining = order.destination, eta, st.remaining - order.weight
        if late:
            log_event(log, "order_at_risk", level=logging.WARNING, order_id=order.id, courier_id=cid,
                      eta=eta, deadline=order.deadline)

    log_event(log, "assignment_plan", assigned=len(assignments), unassigned=len(unassigned), blocked=len(blocked))
    return AssignmentPlan(tuple(assignments), tuple(unassigned), blocked)


def apply_plan(orders: Iterable[Order], plan: AssignmentPlan) -> list[Order]:
    """Devolve NOVOS pedidos com status ASSIGNED conforme o plano (as entradas não são alteradas)."""
    by_courier = {a.order_id: a.courier_id for a in plan.assignments}
    return [with_status(o, OrderStatus.ASSIGNED, by_courier[o.id]) if o.id in by_courier else o for o in orders]
