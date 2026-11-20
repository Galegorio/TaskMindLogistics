"""Interface REST (FastAPI). Opcional: o núcleo do sistema não depende de FastAPI.

Execução:  uvicorn app.api:create_app --factory --reload
"""
from __future__ import annotations

from typing import Any, Optional

from app.config import Settings
from app.domain.exceptions import (AlreadyAssignedError, CapacityExceededError, ConcurrentModificationError,
                                   CourierUnavailableError, DuplicateOrderError, IdempotencyConflictError,
                                   InvalidTransitionError, OrderBlockedError, TaskMindError, UnknownCourierError,
                                   UnknownLocationError, UnknownOrderError, ValidationError)
from app.domain.models import Courier, Order, OrderStatus
from app.logging_config import configure_logging
from app.repositories.files import load_graph
from app.repositories.orders import OrderRepository
from app.services.prioritization import explain_rank, rank_orders
from app.services.routing import shortest_path
from app.services.simulation import simulate
from app.services.assignment import assign_orders

#: Mapeamento exceção -> HTTP. A primeira correspondência vence (subclasses antes das bases).
_HTTP_STATUS: list[tuple[type[Exception], int]] = [
    (UnknownOrderError, 404), (UnknownCourierError, 404), (UnknownLocationError, 404),
    (DuplicateOrderError, 409), (AlreadyAssignedError, 409), (ConcurrentModificationError, 409),
    (InvalidTransitionError, 409), (IdempotencyConflictError, 409), (CapacityExceededError, 409),
    (OrderBlockedError, 409), (CourierUnavailableError, 409), (ValidationError, 422), (TaskMindError, 400),
]


def create_app(settings: Optional[Settings] = None) -> Any:
    """Cria a aplicação FastAPI (importa FastAPI sob demanda)."""
    from fastapi import Body, FastAPI, Header
    from fastapi.responses import JSONResponse

    settings = settings or Settings.from_env()
    configure_logging(settings.log_level, settings.log_json)
    repo = OrderRepository(settings.db_path)
    app = FastAPI(title="TaskMind Logistics", version="1.0.0")

    def graph():
        return load_graph(settings.data_dir / "map.json")

    @app.exception_handler(TaskMindError)
    async def _handle(_request: Any, exc: TaskMindError) -> Any:
        status = next(code for cls, code in _HTTP_STATUS if isinstance(exc, cls))
        return JSONResponse(status_code=status, content={"error": type(exc).__name__, "message": str(exc)})

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/orders", status_code=201)
    def create_order(body: dict = Body(...)) -> dict:
        order = Order.from_dict(body)
        repo.add_order(order)
        return order.to_dict()

    @app.get("/orders")
    def list_orders() -> list[dict]:
        return [o.to_dict() for o in repo.list_orders()]

    @app.post("/couriers", status_code=201)
    def create_courier(body: dict = Body(...)) -> dict:
        courier = Courier.from_dict(body)
        repo.add_courier(courier)
        return body

    @app.get("/rank")
    def rank(current_time: float) -> list[dict]:
        return [{"order": o.to_dict(), "why": explain_rank(o, current_time)}
                for o in rank_orders(repo.list_orders(), current_time)]

    @app.get("/route")
    def route(origin: str, destination: str) -> dict:
        r = shortest_path(graph(), origin, destination)
        return {"found": r.found, "path": list(r.path), "total_time": r.total_time}

    @app.post("/plan")
    def plan(current_time: float) -> dict:
        p = assign_orders(repo.list_orders(), repo.list_couriers(), graph(), current_time)
        return {"assignments": [a.__dict__ for a in p.assignments], "unassigned": [u.__dict__ for u in p.unassigned],
                "blocked": {k: list(v) for k, v in p.blocked.items()}}

    @app.post("/orders/{order_id}/assign")
    def assign(order_id: str, body: dict = Body(...), idempotency_key: Optional[str] = Header(None)) -> dict:
        """Cabeçalho obrigatório ``Idempotency-Key``; corpo: ``{"courier_id": "D01"}``."""
        if not idempotency_key:
            raise ValidationError("cabeçalho Idempotency-Key é obrigatório")
        courier_id = body.get("courier_id")
        if not isinstance(courier_id, str):
            raise ValidationError("campo 'courier_id' é obrigatório")
        r = repo.process_order(order_id, courier_id, idempotency_key)
        return {"order_id": r.order_id, "courier_id": r.courier_id, "status": r.status, "replayed": r.replayed}

    @app.post("/orders/{order_id}/cancel")
    def cancel(order_id: str) -> dict:
        return repo.transition(order_id, OrderStatus.CANCELLED).to_dict()

    @app.get("/audit")
    def audit(order_id: Optional[str] = None) -> list[dict]:
        return repo.audit_events(order_id)

    @app.post("/simulate")
    def run_simulation(body: dict = Body(...)) -> dict:
        result = simulate(repo.list_orders(), repo.list_couriers(), graph(), body.get("events", []),
                          body.get("start_time", 0))
        return {"history": result.history, "at_risk": result.at_risk, "fingerprint": result.fingerprint(),
                "orders": {k: v.to_dict() for k, v in result.orders.items()}}

    return app
