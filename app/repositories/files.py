"""Carregamento e validação dos arquivos JSON de entrada (pedidos, entregadores, mapa)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.domain.exceptions import ValidationError
from app.domain.models import Courier, Graph, Order


def _load(path: str | Path) -> Any:
    p = Path(path)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValidationError(f"arquivo não encontrado: {p}") from None
    except UnicodeDecodeError as exc:
        raise ValidationError(f"{p.name} não está codificado em UTF-8 ({exc.reason} no byte {exc.start})") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"JSON inválido em {p.name}: {exc}") from exc


def load_orders(path: str | Path) -> list[Order]:
    """Lê e valida uma lista de pedidos."""
    data = _load(path)
    if not isinstance(data, list):
        raise ValidationError(f"{Path(path).name}: esperado uma lista de pedidos")
    return [Order.from_dict(item) for item in data]


def load_couriers(path: str | Path) -> list[Courier]:
    """Lê e valida uma lista de entregadores."""
    data = _load(path)
    if not isinstance(data, list):
        raise ValidationError(f"{Path(path).name}: esperado uma lista de entregadores")
    return [Courier.from_dict(item) for item in data]


def load_graph(path: str | Path) -> Graph:
    """Lê e valida o mapa."""
    return Graph.from_dict(_load(path))
