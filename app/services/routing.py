"""Missão 3 - Roteamento: Dijkstra com heapq."""
from __future__ import annotations

import heapq
import math
from dataclasses import dataclass
from typing import Mapping, Union

from app.domain.exceptions import UnknownLocationError, ValidationError
from app.domain.models import Graph

GraphLike = Union[Graph, Mapping]


@dataclass(frozen=True)
class Route:
    """Resultado de roteamento. Sem rota: ``found=False``, ``path=()`` e ``total_time=None``."""

    origin: str
    destination: str
    path: tuple[str, ...]
    total_time: float | None
    found: bool


def coerce_graph(graph: GraphLike) -> Graph:
    """Aceita um ``Graph`` ou o dicionário JSON do mapa."""
    if isinstance(graph, Graph):
        return graph
    if isinstance(graph, Mapping):
        return Graph.from_dict(graph)
    raise ValidationError(f"mapa inválido: esperado Graph ou dict, recebido {type(graph).__name__}")


def shortest_path(graph: GraphLike, origin: str, destination: str) -> Route:
    """Rota de menor tempo entre dois locais (Dijkstra com fila de prioridade).

    - Locais desconectados -> ``Route(found=False, ...)`` (não levanta exceção).
    - Local inexistente no mapa -> ``UnknownLocationError``.
    - Pesos negativos já são rejeitados na construção do ``Graph``.
    - Para assim que o destino é retirado do heap, sem varrer o mapa inteiro.
    Complexidade: O((V+E) log V) no pior caso, usando "lazy deletion" no heap.
    """
    g = coerce_graph(graph)
    for name in (origin, destination):
        if name not in g:
            raise UnknownLocationError(f"local {name!r} não existe no mapa")
    if origin == destination:
        return Route(origin, destination, (origin,), 0, True)

    dist: dict[str, float] = {origin: 0}
    previous: dict[str, str] = {}
    settled: set[str] = set()
    heap: list[tuple[float, str]] = [(0, origin)]
    while heap:
        d, u = heapq.heappop(heap)
        if u in settled:  # entrada obsoleta (já fechada com custo menor)
            continue
        if u == destination:
            return Route(origin, destination, _rebuild(previous, origin, destination), d, True)
        settled.add(u)
        for v, w in g.neighbors(u).items():
            if v in settled:
                continue
            nd = d + w
            if nd < dist.get(v, math.inf):
                dist[v] = nd
                previous[v] = u
                heapq.heappush(heap, (nd, v))
    return Route(origin, destination, (), None, False)


def _rebuild(previous: Mapping[str, str], origin: str, destination: str) -> tuple[str, ...]:
    path = [destination]
    while path[-1] != origin:
        path.append(previous[path[-1]])
    path.reverse()
    return tuple(path)


class RouteCache:
    """Memoiza rotas (origem, destino) durante um ciclo de planejamento: evita recalcular Dijkstra."""

    def __init__(self, graph: GraphLike):
        self._graph = coerce_graph(graph)
        self._memo: dict[tuple[str, str], Route] = {}

    def get(self, origin: str, destination: str) -> Route:
        """Rota memoizada."""
        key = (origin, destination)
        if key not in self._memo:
            self._memo[key] = shortest_path(self._graph, origin, destination)
        return self._memo[key]
