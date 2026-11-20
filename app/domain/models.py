"""Modelos de domínio: pedidos, entregadores, mapa e regras de transição de status.

Invariantes garantidas na construção (qualquer violação levanta ``ValidationError``):

* ``assigned``/``in_transit`` exigem ``assigned_to``; ``pending``/``blocked``/``cancelled`` não podem tê-lo;
  ``delivered`` mantém o entregador apenas como histórico (opcional).
* Um pedido não depende de si mesmo; dependências repetidas são reduzidas a uma só (ordem preservada).
* IDs, destinos e nomes de locais são textos não vazios, sem espaços nas pontas.
* Campos desconhecidos em JSON são rejeitados (um typo como ``"dependecies"`` não pode virar "sem dependências").
"""
from __future__ import annotations

import difflib
import math
from dataclasses import asdict, dataclass, replace
from enum import Enum
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Optional

from app.domain.exceptions import (InvalidTransitionError, NegativeWeightError, SelfDependencyError,
                                   UnknownLocationError, ValidationError)


class OrderStatus(str, Enum):
    """Estados possíveis de um pedido."""

    PENDING = "pending"
    ASSIGNED = "assigned"
    IN_TRANSIT = "in_transit"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


TERMINAL_STATUSES = frozenset({OrderStatus.DELIVERED, OrderStatus.CANCELLED})
COMMITTED_STATUSES = frozenset({OrderStatus.ASSIGNED, OrderStatus.IN_TRANSIT})
NO_COURIER_STATUSES = frozenset({OrderStatus.PENDING, OrderStatus.BLOCKED, OrderStatus.CANCELLED})

#: Máquina de estados. ``blocked`` é um estado derivado (dependências não entregues); quando as dependências
#: são satisfeitas ele pode ser atribuído diretamente. Cancelar um pedido em trânsito NÃO é permitido.
ALLOWED_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.PENDING: frozenset({OrderStatus.ASSIGNED, OrderStatus.CANCELLED, OrderStatus.BLOCKED}),
    OrderStatus.BLOCKED: frozenset({OrderStatus.PENDING, OrderStatus.ASSIGNED, OrderStatus.CANCELLED}),
    OrderStatus.ASSIGNED: frozenset({OrderStatus.IN_TRANSIT, OrderStatus.PENDING, OrderStatus.CANCELLED}),
    OrderStatus.IN_TRANSIT: frozenset({OrderStatus.DELIVERED}),
    OrderStatus.DELIVERED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
}

_TEXT_RULE = "texto não vazio e sem espaços nas pontas"
_ORDER_FIELDS = ("id", "title", "priority", "created_at", "deadline", "weight", "destination",
                 "dependencies", "status", "assigned_to")
_COURIER_FIELDS = ("id", "location", "capacity", "available_at", "active")
_KEEP: Any = object()


def _coerce_status(value: Any, ctx: str) -> OrderStatus:
    try:
        return OrderStatus(value)
    except ValueError:
        validos = ", ".join(s.value for s in OrderStatus)
        raise ValidationError(f"{ctx}: status inválido {value!r} (válidos: {validos})") from None


def ensure_transition(order_id: str, current: OrderStatus | str, new: OrderStatus | str) -> None:
    """Levanta InvalidTransitionError se ``current -> new`` não for permitido (aceita ``OrderStatus`` ou texto)."""
    ctx = f"pedido {order_id!r}"
    current, new = _coerce_status(current, ctx), _coerce_status(new, ctx)
    if new not in ALLOWED_TRANSITIONS[current]:
        permitidos = ", ".join(sorted(s.value for s in ALLOWED_TRANSITIONS[current])) or "nenhum (estado final)"
        raise InvalidTransitionError(
            f"{ctx}: transição {current.value} -> {new.value} não permitida (permitidos: {permitidos})"
        )


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_text(value: Any) -> bool:
    return isinstance(value, str) and value != "" and value == value.strip()


def _reject_unknown(data: Mapping[str, Any], allowed: Iterable[str], ctx: str) -> None:
    """Rejeita chaves fora de ``allowed``, sugerindo a mais parecida (ex.: 'dependecies' -> 'dependencies')."""
    allowed = sorted(allowed)
    unknown = [k for k in data if k not in allowed]
    if not unknown:
        return
    dicas = []
    for key in unknown:
        close = difflib.get_close_matches(str(key), allowed, n=1)
        dicas.append(f"{key!r}" + (f" (você quis dizer {close[0]!r}?)" if close else ""))
    raise ValidationError(f"{ctx}: campo(s) desconhecido(s): {', '.join(dicas)}; válidos: {', '.join(allowed)}")


@dataclass(frozen=True)
class Order:
    """Pedido de entrega (imutável; mudanças de estado geram novas instâncias via ``with_status``)."""

    id: str
    title: str
    priority: int
    created_at: float
    deadline: float
    weight: float
    destination: str
    dependencies: tuple[str, ...] = ()
    status: OrderStatus = OrderStatus.PENDING
    assigned_to: Optional[str] = None

    def __post_init__(self) -> None:
        if not _is_text(self.id):
            raise ValidationError(f"pedido: 'id' deve ser {_TEXT_RULE} (recebido {self.id!r})")
        ctx = f"pedido {self.id!r}"
        if not isinstance(self.title, str):
            raise ValidationError(f"{ctx}: 'title' deve ser texto")
        if isinstance(self.priority, bool) or not isinstance(self.priority, int) or not 1 <= self.priority <= 5:
            raise ValidationError(f"{ctx}: 'priority' deve ser inteiro entre 1 e 5 (recebido {self.priority!r})")
        if not _is_number(self.created_at) or self.created_at < 0:
            raise ValidationError(f"{ctx}: 'created_at' deve ser número >= 0 (recebido {self.created_at!r})")
        if not _is_number(self.deadline):
            raise ValidationError(f"{ctx}: 'deadline' deve ser número (recebido {self.deadline!r})")
        if self.deadline < self.created_at:
            raise ValidationError(f"{ctx}: 'deadline' ({self.deadline}) anterior a 'created_at' ({self.created_at})")
        if not _is_number(self.weight) or self.weight <= 0:
            raise ValidationError(f"{ctx}: 'weight' deve ser número positivo (recebido {self.weight!r})")
        if not _is_text(self.destination):
            raise ValidationError(f"{ctx}: 'destination' deve ser {_TEXT_RULE} (recebido {self.destination!r})")
        if isinstance(self.dependencies, (str, bytes)):
            raise ValidationError(f"{ctx}: 'dependencies' deve ser uma lista de IDs, não texto")
        try:
            deps = tuple(self.dependencies)
        except TypeError:
            raise ValidationError(f"{ctx}: 'dependencies' deve ser uma lista de IDs") from None
        if not all(_is_text(d) for d in deps):
            raise ValidationError(f"{ctx}: 'dependencies' deve conter apenas IDs ({_TEXT_RULE}); recebido {deps!r}")
        if self.id in deps:
            raise SelfDependencyError(self.id)
        object.__setattr__(self, "dependencies", tuple(dict.fromkeys(deps)))  # repetidas viram uma só
        status = _coerce_status(self.status, ctx)
        object.__setattr__(self, "status", status)
        if self.assigned_to is not None and not _is_text(self.assigned_to):
            raise ValidationError(f"{ctx}: 'assigned_to' deve ser {_TEXT_RULE} ou nulo")
        if status in COMMITTED_STATUSES and self.assigned_to is None:
            raise ValidationError(f"{ctx}: status {status.value!r} exige 'assigned_to' (entregador responsável)")
        if status in NO_COURIER_STATUSES and self.assigned_to is not None:
            raise ValidationError(f"{ctx}: status {status.value!r} não pode ter 'assigned_to' "
                                  f"(recebido {self.assigned_to!r}); libere a atribuição antes")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Order":
        """Constrói e valida um pedido a partir de um dicionário JSON (campos desconhecidos são rejeitados)."""
        if not isinstance(data, Mapping):
            raise ValidationError(f"pedido deve ser um objeto JSON (recebido {type(data).__name__})")
        ctx = f"pedido {data.get('id')!r}"
        _reject_unknown(data, _ORDER_FIELDS, ctx)
        obrigatorios = ("id", "title", "priority", "created_at", "deadline", "weight", "destination")
        faltando = [k for k in obrigatorios if k not in data]
        if faltando:
            raise ValidationError(f"{ctx}: campo(s) obrigatório(s) ausente(s): {', '.join(faltando)}")
        deps = data.get("dependencies", [])
        if not isinstance(deps, (list, tuple)):
            raise ValidationError(f"{ctx}: 'dependencies' deve ser uma lista")
        return cls(
            id=data["id"], title=data["title"], priority=data["priority"], created_at=data["created_at"],
            deadline=data["deadline"], weight=data["weight"], destination=data["destination"],
            dependencies=tuple(deps), status=data.get("status", "pending"), assigned_to=data.get("assigned_to"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serializa para um dicionário compatível com JSON."""
        d = asdict(self)
        d["status"] = self.status.value
        d["dependencies"] = list(self.dependencies)
        return d


def with_status(order: Order, new_status: OrderStatus | str, assigned_to: Optional[str] = _KEEP) -> Order:
    """Devolve uma NOVA ordem com o status trocado, validando a transição e as invariantes.

    ``assigned_to`` padrão: limpo para pending/blocked/cancelled; mantido nos demais. Ao atribuir
    (``ASSIGNED``) informe o entregador. É o único caminho que os serviços usam para mudar status.
    """
    new_status = _coerce_status(new_status, f"pedido {order.id!r}")
    ensure_transition(order.id, order.status, new_status)
    if assigned_to is _KEEP:
        assigned_to = None if new_status in NO_COURIER_STATUSES else order.assigned_to
    return replace(order, status=new_status, assigned_to=assigned_to)


@dataclass(frozen=True)
class Courier:
    """Entregador."""

    id: str
    location: str
    capacity: float
    available_at: float = 0
    active: bool = True

    def __post_init__(self) -> None:
        if not _is_text(self.id):
            raise ValidationError(f"entregador: 'id' deve ser {_TEXT_RULE} (recebido {self.id!r})")
        ctx = f"entregador {self.id!r}"
        if not _is_text(self.location):
            raise ValidationError(f"{ctx}: 'location' deve ser {_TEXT_RULE} (recebido {self.location!r})")
        if not _is_number(self.capacity) or self.capacity <= 0:
            raise ValidationError(f"{ctx}: 'capacity' deve ser número positivo (recebido {self.capacity!r})")
        if not _is_number(self.available_at) or self.available_at < 0:
            raise ValidationError(f"{ctx}: 'available_at' deve ser número >= 0 (recebido {self.available_at!r})")
        if not isinstance(self.active, bool):
            raise ValidationError(f"{ctx}: 'active' deve ser booleano (recebido {self.active!r})")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Courier":
        """Constrói e valida um entregador a partir de um dicionário JSON (campos desconhecidos são rejeitados)."""
        if not isinstance(data, Mapping):
            raise ValidationError(f"entregador deve ser um objeto JSON (recebido {type(data).__name__})")
        ctx = f"entregador {data.get('id')!r}"
        _reject_unknown(data, _COURIER_FIELDS, ctx)
        faltando = [k for k in ("id", "location", "capacity") if k not in data]
        if faltando:
            raise ValidationError(f"{ctx}: campo(s) ausente(s): {', '.join(faltando)}")
        return cls(id=data["id"], location=data["location"], capacity=data["capacity"],
                   available_at=data.get("available_at", 0), active=data.get("active", True))


class Graph:
    """Mapa: grafo não direcionado e ponderado, em lista de adjacência.

    Pesos negativos são rejeitados na construção e a visão devolvida por ``neighbors`` é somente leitura, o que
    garante a pré-condição de Dijkstra. Arestas repetidas mantêm o menor tempo. Uma aresta inválida é rejeitada
    por completo: nenhum nó é criado pela tentativa.
    """

    def __init__(self) -> None:
        self._adj: dict[str, dict[str, float]] = {}

    @staticmethod
    def _check_name(name: Any) -> None:
        if not _is_text(name):
            raise ValidationError(f"nome de local inválido: {name!r} (deve ser {_TEXT_RULE})")

    def add_node(self, name: str) -> None:
        """Adiciona um local (pode ficar isolado)."""
        self._check_name(name)
        self._adj.setdefault(name, {})

    def add_edge(self, a: str, b: str, time: float) -> None:
        """Adiciona estrada bidirecional entre ``a`` e ``b`` com o tempo de viagem ``time``."""
        self._check_name(a)
        self._check_name(b)
        if not _is_number(time):
            raise ValidationError(f"estrada {a!r}-{b!r}: 'time' deve ser número finito (recebido {time!r})")
        if time < 0:
            raise NegativeWeightError(f"estrada {a!r}-{b!r} com tempo negativo ({time}); pesos devem ser >= 0")
        self._adj.setdefault(a, {})  # só mexe no estado depois de validar tudo
        self._adj.setdefault(b, {})
        best = min(time, self._adj[a].get(b, math.inf))
        self._adj[a][b] = best
        self._adj[b][a] = best

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Graph":
        """Constrói o grafo a partir de ``{"edges": [{"from","to","time"}], "nodes": [...] (opcional)}``."""
        if not isinstance(data, Mapping):
            raise ValidationError(f"mapa deve ser um objeto JSON (recebido {type(data).__name__})")
        _reject_unknown(data, ("edges", "nodes"), "mapa")
        if "edges" not in data:
            raise ValidationError("mapa: campo obrigatório 'edges' ausente")
        if not isinstance(data["edges"], list):
            raise ValidationError("mapa: 'edges' deve ser uma lista")
        nodes = data.get("nodes", [])
        if not isinstance(nodes, (list, tuple)):
            raise ValidationError(f"mapa: 'nodes' deve ser uma lista de nomes de locais (recebido {nodes!r})")
        g = cls()
        for node in nodes:
            g.add_node(node)
        for i, edge in enumerate(data["edges"]):
            if not isinstance(edge, Mapping):
                raise ValidationError(f"aresta #{i}: esperado objeto com 'from', 'to' e 'time' (recebido {edge!r})")
            _reject_unknown(edge, ("from", "to", "time"), f"aresta #{i}")
            if not {"from", "to", "time"} <= set(edge):
                raise ValidationError(f"aresta #{i}: esperado objeto com 'from', 'to' e 'time' (recebido {edge!r})")
            g.add_edge(edge["from"], edge["to"], edge["time"])
        return g

    def neighbors(self, node: str) -> Mapping[str, float]:
        """Vizinhos de ``node`` com o tempo de viagem (visão SOMENTE LEITURA). Local inexistente: UnknownLocationError."""
        try:
            return MappingProxyType(self._adj[node])
        except KeyError:
            raise UnknownLocationError(f"local {node!r} não existe no mapa") from None

    def nodes(self) -> list[str]:
        """Lista de locais."""
        return list(self._adj)

    def __contains__(self, node: object) -> bool:
        return node in self._adj

    def __len__(self) -> int:
        return len(self._adj)
