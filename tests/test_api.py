"""Testes da API REST. Pulados automaticamente se FastAPI/httpx não estiverem instalados."""
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from app.api import create_app  # noqa: E402
from app.config import Settings  # noqa: E402

DATA = Path(__file__).resolve().parent.parent / "data"
ORDER = {"id": "P1", "title": "t", "priority": 3, "created_at": 0, "deadline": 500, "weight": 2, "destination": "B"}


def client(tmp_path):
    s = Settings(db_path=tmp_path / "api.db", data_dir=DATA, log_level="WARNING", log_json=False, urgent_window_minutes=30)
    c = TestClient(create_app(s))
    assert c.post("/orders", json=ORDER).status_code == 201
    assert c.post("/couriers", json={"id": "D1", "location": "A", "capacity": 10}).status_code == 201
    assert c.post("/couriers", json={"id": "D2", "location": "A", "capacity": 10}).status_code == 201
    return c


def test_health_and_validation_errors(tmp_path):
    c = client(tmp_path)
    assert c.get("/health").json() == {"status": "ok"}
    r = c.post("/orders", json={**ORDER, "id": "P2", "priority": 9})
    assert r.status_code == 422 and "priority" in r.json()["message"]
    assert c.post("/orders", json=ORDER).status_code == 409


def test_assign_is_idempotent_and_conflicts_are_409(tmp_path):
    c = client(tmp_path)
    h = {"Idempotency-Key": "k1"}
    first = c.post("/orders/P1/assign", json={"courier_id": "D1"}, headers=h).json()
    again = c.post("/orders/P1/assign", json={"courier_id": "D1"}, headers=h).json()
    assert first["replayed"] is False and again["replayed"] is True
    assert c.post("/orders/P1/assign", json={"courier_id": "D2"}, headers={"Idempotency-Key": "k2"}).status_code == 409
    assert c.post("/orders/P1/assign", json={"courier_id": "D2"}, headers=h).status_code == 409  # conflito de chave
    assert c.post("/orders/P1/assign", json={"courier_id": "D1"}).status_code == 422             # sem chave
    # reutilizar a chave k1 (já ligada a P1/D1) em outro pedido é conflito de idempotência, não "não encontrado"
    assert c.post("/orders/NOPE/assign", json={"courier_id": "D1"}, headers=h).status_code == 409
    assert c.post("/orders/NOPE/assign", json={"courier_id": "D1"}, headers={"Idempotency-Key": "k3"}).status_code == 404


def test_route_and_rank(tmp_path):
    c = client(tmp_path)
    r = c.get("/route", params={"origin": "A", "destination": "C"}).json()
    assert r["path"] == ["A", "B", "C"] and r["total_time"] == 12
    assert c.get("/route", params={"origin": "A", "destination": "H"}).json()["found"] is False
    assert c.get("/route", params={"origin": "A", "destination": "ZZ"}).status_code == 404
    assert [x["order"]["id"] for x in c.get("/rank", params={"current_time": 0}).json()] == ["P1"]
