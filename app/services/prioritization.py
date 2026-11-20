"""Missão 1 - Motor de prioridades."""
from __future__ import annotations

from typing import Iterable

from app.config import DEFAULT_URGENT_WINDOW
from app.domain.models import Order, OrderStatus
from app.services.dependencies import index_orders, unsatisfied_dependencies

_CANDIDATE_STATUSES = (OrderStatus.PENDING, OrderStatus.BLOCKED)


def is_urgent(order: Order, current_time: float, urgent_window: float = DEFAULT_URGENT_WINDOW) -> bool:
    """Urgente = prazo em até ``urgent_window`` minutos (inclusive). Pedidos já atrasados também são urgentes."""
    return order.deadline - current_time <= urgent_window


def rank_key(order: Order, current_time: float, urgent_window: float = DEFAULT_URGENT_WINDOW) -> tuple:
    """Chave de ordenação, na ordem dos critérios do enunciado."""
    return (
        0 if is_urgent(order, current_time, urgent_window) else 1,  # 1) urgentes primeiro
        -order.priority,                                            # 2) maior prioridade
        order.deadline - current_time,                              # 3) menor tempo restante
        order.created_at,                                           # 4) criado primeiro
        order.id,                                                   # 5) desempate final por ID
    )


def rank_orders(orders: Iterable[Order], current_time: float,
                urgent_window: float = DEFAULT_URGENT_WINDOW) -> list[Order]:
    """Fila de pedidos elegíveis, do mais para o menos prioritário.

    Elegível = status pending/blocked E todas as dependências entregues. Entregues, cancelados,
    atribuídos e em trânsito nunca entram. Não altera a lista original.
    Complexidade: O(n log n) (um filtro O(n+E) e um ``sorted`` com chave pré-calculada).
    """
    olist = list(orders)
    by_id = index_orders(olist)
    eligible = [o for o in olist if o.status in _CANDIDATE_STATUSES and not unsatisfied_dependencies(o, by_id)]
    return sorted(eligible, key=lambda o: rank_key(o, current_time, urgent_window))


def explain_rank(order: Order, current_time: float, urgent_window: float = DEFAULT_URGENT_WINDOW) -> str:
    """Explica em texto por que o pedido está na posição que está (rastreabilidade das decisões)."""
    restante = order.deadline - current_time
    grupo = "URGENTE" if is_urgent(order, current_time, urgent_window) else "normal"
    return (f"{order.id}: grupo {grupo} (faltam {restante:g} min; urgente se <= {urgent_window:g}), "
            f"prioridade {order.priority}, criado em {order.created_at:g}")
