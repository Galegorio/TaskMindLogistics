"""Exceções de domínio. Todas herdam de TaskMindError e carregam mensagens claras."""
from __future__ import annotations

from typing import Sequence


class TaskMindError(Exception):
    """Base de todos os erros previstos pelo sistema."""


class ValidationError(TaskMindError):
    """Dado de entrada inválido."""


class DuplicateOrderError(ValidationError):
    """Dois pedidos com o mesmo ID."""


class NegativeWeightError(ValidationError):
    """Estrada com tempo de viagem negativo (Dijkstra exige pesos >= 0)."""


class DependencyError(ValidationError):
    """Problema na configuração de dependências."""


class UnknownDependencyError(DependencyError):
    """Pedido depende de um ID que não existe."""

    def __init__(self, missing: Sequence[tuple[str, str]]):
        self.missing = tuple(missing)
        detalhes = "; ".join(f"{o!r} depende de {d!r}, que não existe" for o, d in self.missing)
        super().__init__(f"dependência(s) inexistente(s): {detalhes}")


class SelfDependencyError(DependencyError):
    """Pedido depende dele mesmo."""

    def __init__(self, order_id: str):
        self.order_id = order_id
        super().__init__(f"pedido {order_id!r} não pode depender de si mesmo")


class CycleError(DependencyError):
    """Dependência circular. ``cycle`` traz o caminho, ex.: ('P1','P2','P3','P1')."""

    def __init__(self, cycle: Sequence[str]):
        self.cycle = tuple(cycle)
        super().__init__("ciclo de dependências: " + " -> ".join(self.cycle) + " (cada pedido depende do próximo)")


class UnknownOrderError(TaskMindError):
    """Pedido não encontrado."""


class UnknownCourierError(TaskMindError):
    """Entregador não encontrado."""


class UnknownLocationError(TaskMindError):
    """Local inexistente no mapa."""


class InvalidTransitionError(TaskMindError):
    """Transição de status não permitida."""


class AlreadyAssignedError(InvalidTransitionError):
    """O pedido já foi atribuído (possivelmente por outra requisição concorrente)."""


class ConcurrentModificationError(InvalidTransitionError):
    """O registro mudou entre a leitura e a escrita (atualização condicional falhou)."""


class OrderBlockedError(TaskMindError):
    """Pedido com dependências não entregues."""


class CourierUnavailableError(TaskMindError):
    """Entregador inativo ou indisponível."""


class CapacityExceededError(TaskMindError):
    """Carga do entregador excederia a capacidade."""


class IdempotencyConflictError(TaskMindError):
    """Mesma chave de idempotência reutilizada com parâmetros diferentes."""
