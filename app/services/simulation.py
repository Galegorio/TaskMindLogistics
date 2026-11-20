"""Missão 6 - Simulação orientada a eventos com replanejamento.

Modelo (simplificações documentadas no README):
* Eventos são ordenados por (time, posição original): a simulação é determinística e reproduzível.
* Cada evento tem um ``id``. Um ``id`` já processado é ignorado (idempotência: efeitos não se duplicam).
* Após cada evento APLICADO, os pedidos pendentes são replanejados com ``assign_orders`` usando somente
  entregadores ativos e ociosos (sem pedidos atribuídos/em trânsito). O primeiro pedido da fila de um entregador
  sai imediatamente (``in_transit``); os seguintes ficam ``assigned`` até seu horário de saída.
* ``COURIER_UNAVAILABLE`` libera os pedidos ``assigned`` (que ainda não saíram) para replanejamento;
  pedidos já ``in_transit`` continuam com o entregador.
* Eventos inválidos (pedido inexistente, transição proibida...) são REGISTRADOS como ``rejected`` e não
  interrompem a simulação; eventos malformados (campos faltando) falham logo no início.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Iterable, Mapping, Optional

from app.domain.exceptions import (CourierUnavailableError, TaskMindError, UnknownCourierError,
                                   UnknownOrderError, ValidationError)
from app.domain.models import Courier, Order, OrderStatus, with_status
from app.logging_config import log_event
from app.services.assignment import Assignment, AssignmentPlan, assign_orders
from app.services.dependencies import validate_dependencies
from app.services.routing import GraphLike, RouteCache, coerce_graph

log = logging.getLogger(__name__)


class EventType(str, Enum):
    """Tipos de evento aceitos."""

    ORDER_CREATED = "ORDER_CREATED"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    ORDER_DELIVERED = "ORDER_DELIVERED"
    COURIER_UNAVAILABLE = "COURIER_UNAVAILABLE"
    COURIER_AVAILABLE = "COURIER_AVAILABLE"


@dataclass(frozen=True)
class Event:
    """Evento da simulação. ``order`` só em ORDER_CREATED; ``order_id``/``courier_id`` nos demais."""

    id: str
    time: float
    type: EventType
    order: Optional[Order] = None
    order_id: Optional[str] = None
    courier_id: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Event":
        """Valida e constrói um evento a partir de JSON."""
        if not isinstance(data, Mapping):
            raise ValidationError(f"evento deve ser um objeto JSON (recebido {type(data).__name__})")
        eid = data.get("id")
        if not isinstance(eid, str) or not eid.strip():
            raise ValidationError(f"evento sem 'id' válido: {dict(data)!r}")
        t = data.get("time")
        if isinstance(t, bool) or not isinstance(t, (int, float)):
            raise ValidationError(f"evento {eid!r}: 'time' deve ser número")
        try:
            etype = EventType(data.get("type"))
        except ValueError:
            validos = ", ".join(e.value for e in EventType)
            raise ValidationError(f"evento {eid!r}: tipo inválido {data.get('type')!r} (válidos: {validos})") from None
        if etype is EventType.ORDER_CREATED:
            if "order" not in data:
                raise ValidationError(f"evento {eid!r}: ORDER_CREATED exige o campo 'order'")
            return cls(eid, t, etype, order=Order.from_dict(data["order"]))
        key = "order_id" if etype in (EventType.ORDER_CANCELLED, EventType.ORDER_DELIVERED) else "courier_id"
        if not isinstance(data.get(key), str):
            raise ValidationError(f"evento {eid!r}: {etype.value} exige o campo '{key}'")
        return cls(eid, t, etype, **{key: data[key]})


@dataclass
class SimulationResult:
    """Estado final, histórico completo e plano vigente."""

    orders: dict[str, Order]
    couriers: dict[str, Courier]
    history: list[dict[str, Any]]
    plan: dict[str, Assignment]
    unassigned: dict[str, str] = field(default_factory=dict)

    @property
    def at_risk(self) -> list[str]:
        """IDs de pedidos planejados com ETA depois do prazo."""
        return sorted(oid for oid, a in self.plan.items() if a.late)

    def fingerprint(self) -> str:
        """Hash do histórico: duas execuções idênticas produzem o mesmo valor (reprodutibilidade)."""
        return hashlib.sha256(json.dumps(self.history, sort_keys=True).encode()).hexdigest()


class _Simulator:
    def __init__(self, orders: list[Order], couriers: list[Courier], graph: GraphLike, start_time: float):
        self.orders = {o.id: o for o in orders}
        self.couriers = {c.id: c for c in couriers}
        self.graph = coerce_graph(graph)
        self.cache = RouteCache(self.graph)
        self.plan: dict[str, Assignment] = {}
        self.unassigned: dict[str, str] = {}
        self.processed: set[str] = set()
        self.history: list[dict[str, Any]] = []
        self.time = start_time

    # ------------------------------------------------------------ utilidades
    def _record(self, event_id: str, time: float, etype: str, outcome: str, detail: str,
                released: Iterable[str] = (), assigned: Iterable[Assignment] = ()) -> None:
        self.history.append({
            "seq": len(self.history) + 1, "event_id": event_id, "time": time, "type": etype,
            "outcome": outcome, "detail": detail, "released": list(released),
            "assigned": [{"order_id": a.order_id, "courier_id": a.courier_id, "eta": a.eta, "late": a.late}
                         for a in assigned],
            "unassigned": sorted(self.unassigned),
        })

    def _order(self, oid: Optional[str]) -> Order:
        if oid not in self.orders:
            raise UnknownOrderError(f"pedido {oid!r} não existe")
        return self.orders[oid]

    def _courier(self, cid: Optional[str]) -> Courier:
        if cid not in self.couriers:
            raise UnknownCourierError(f"entregador {cid!r} não existe")
        return self.couriers[cid]

    def _dispatch(self, t: float) -> None:
        """Pedidos ``assigned`` cujo horário de saída já chegou passam a ``in_transit``."""
        for oid in sorted(self.plan):
            o = self.orders[oid]
            if o.status is OrderStatus.ASSIGNED and self.plan[oid].departure <= t:
                self.orders[oid] = with_status(o, OrderStatus.IN_TRANSIT)

    def replan(self, t: float) -> AssignmentPlan:
        """Reatribui pedidos pendentes a entregadores ativos e ociosos."""
        busy = {o.assigned_to for o in self.orders.values()
                if o.status in (OrderStatus.ASSIGNED, OrderStatus.IN_TRANSIT) and o.assigned_to}
        idle = [c for c in self.couriers.values() if c.active and c.id not in busy]
        plan = assign_orders(list(self.orders.values()), idle, self.graph, t, self.cache)
        for a in plan.assignments:
            self.orders[a.order_id] = with_status(self.orders[a.order_id], OrderStatus.ASSIGNED, a.courier_id)
            self.plan[a.order_id] = a
        self.unassigned = {u.order_id: u.reason for u in plan.unassigned}
        self._dispatch(t)
        return plan

    # ------------------------------------------------------------ eventos
    def handle(self, ev: Event) -> None:
        if ev.id in self.processed:
            self._record(ev.id, ev.time, ev.type.value, "duplicate_ignored",
                         "evento já processado; nenhum efeito aplicado")
            return
        self.processed.add(ev.id)
        self.time = max(self.time, ev.time)
        self._dispatch(self.time)
        try:
            detail, released = self._apply(ev)
        except TaskMindError as exc:
            log_event(log, "event_rejected", level=logging.WARNING, event_id=ev.id, error=type(exc).__name__)
            self._record(ev.id, ev.time, ev.type.value, "rejected", str(exc))
            return
        plan = self.replan(self.time)
        self._record(ev.id, ev.time, ev.type.value, "applied", detail, released, plan.assignments)

    def _apply(self, ev: Event) -> tuple[str, list[str]]:
        t = self.time
        if ev.type is EventType.ORDER_CREATED:
            new = ev.order
            assert new is not None
            if new.status is not OrderStatus.PENDING:
                raise ValidationError(f"pedido novo {new.id!r} deve nascer 'pending' (recebido {new.status.value})")
            validate_dependencies([*self.orders.values(), new])  # duplicado, dependência inexistente, ciclo
            self.orders[new.id] = new
            return f"pedido {new.id} criado", []

        if ev.type is EventType.ORDER_CANCELLED:
            o = self._order(ev.order_id)
            self.orders[o.id] = with_status(o, OrderStatus.CANCELLED)
            self.plan.pop(o.id, None)
            return f"pedido {o.id} cancelado", []

        if ev.type is EventType.ORDER_DELIVERED:
            o = self._order(ev.order_id)
            delivered = with_status(o, OrderStatus.DELIVERED)  # valida antes de mexer em qualquer estado
            courier = self.couriers.get(o.assigned_to or "")
            if courier is not None:  # o entregador agora está no destino e livre
                self.couriers[courier.id] = replace(courier, location=o.destination, available_at=t)
            self.orders[o.id] = delivered
            self.plan.pop(o.id, None)
            return f"pedido {o.id} entregue", []

        c = self._courier(ev.courier_id)
        if ev.type is EventType.COURIER_UNAVAILABLE:
            if not c.active:
                raise CourierUnavailableError(f"entregador {c.id!r} já está indisponível")
            self.couriers[c.id] = replace(c, active=False)
            released: list[str] = []
            for oid in sorted(self.orders):
                o = self.orders[oid]
                if o.status is OrderStatus.ASSIGNED and o.assigned_to == c.id:
                    self.orders[oid] = with_status(o, OrderStatus.PENDING)
                    self.plan.pop(oid, None)
                    released.append(oid)
            return f"entregador {c.id} indisponível; {len(released)} pedido(s) liberado(s)", released

        if c.active:  # COURIER_AVAILABLE
            raise ValidationError(f"entregador {c.id!r} já está ativo")
        self.couriers[c.id] = replace(c, active=True, available_at=t)
        return f"entregador {c.id} disponível", []

    def result(self) -> SimulationResult:
        return SimulationResult(dict(self.orders), dict(self.couriers), self.history, dict(self.plan),
                                dict(self.unassigned))


def simulate(orders: Iterable[Order], couriers: Iterable[Courier], graph: GraphLike,
             events: Iterable[Mapping[str, Any] | Event], start_time: float = 0) -> SimulationResult:
    """Executa a simulação e devolve estado final + histórico.

    A configuração inicial é validada (ciclos/IDs inexistentes levantam erro). Mesmos dados + mesma ordem de
    eventos => mesmo histórico (veja ``SimulationResult.fingerprint``). As entradas não são alteradas.
    """
    olist, clist = list(orders), list(couriers)
    validate_dependencies(olist)
    ids = [c.id for c in clist]
    if len(ids) != len(set(ids)):
        raise ValidationError("IDs de entregador duplicados")
    parsed = [e if isinstance(e, Event) else Event.from_dict(e) for e in events]
    ordered = [ev for _, ev in sorted(enumerate(parsed), key=lambda p: (p[1].time, p[0]))]

    sim = _Simulator(olist, clist, graph, start_time)
    plan = sim.replan(start_time)
    sim._record("START", start_time, "START", "applied", "planejamento inicial", (), plan.assignments)
    for ev in ordered:
        sim.handle(ev)
    return sim.result()
