"""Missão 5 - Persistência em SQLite com integridade, idempotência e auditoria.

Garantias (e por que cada uma existe):
* ``BEGIN IMMEDIATE``: toda escrita adquire o lock de escrita ANTES de ler. Sem isso, duas requisições
  poderiam ler "pending" ao mesmo tempo (check-then-act) e ambas atribuir o pedido.
* ``UPDATE ... WHERE id=? AND status='pending'`` + checagem de ``rowcount``: atualização condicional;
  mesmo que o lock falhe, só uma escrita "vence".
* ``assignments.order_id PRIMARY KEY``: restrição única no banco impede dupla atribuição mesmo com bugs na aplicação.
* ``idempotency_keys``: a resposta da primeira execução é gravada na MESMA transação do efeito; repetições
  devolvem a resposta salva sem reexecutar.
* Tudo (status + atribuição + chave + auditoria) é atômico: qualquer exceção faz ROLLBACK.
* Tentativas rejeitadas são auditadas em uma transação SEPARADA (o rollback não apaga o rastro).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from app.domain.exceptions import (AlreadyAssignedError, CapacityExceededError, ConcurrentModificationError,
                                   CourierUnavailableError, DuplicateOrderError, IdempotencyConflictError,
                                   OrderBlockedError, TaskMindError, UnknownCourierError, UnknownOrderError,
                                   ValidationError)
from app.domain.models import Courier, Order, OrderStatus, ensure_transition
from app.logging_config import log_event

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    priority INTEGER NOT NULL CHECK (priority BETWEEN 1 AND 5),
    created_at NUMERIC NOT NULL,
    deadline NUMERIC NOT NULL,
    weight NUMERIC NOT NULL CHECK (weight > 0),
    destination TEXT NOT NULL,
    dependencies TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK (status IN ('pending','assigned','in_transit','delivered','cancelled','blocked')),
    assigned_to TEXT,
    CHECK ((status IN ('assigned','in_transit') AND assigned_to IS NOT NULL)
        OR (status IN ('pending','blocked','cancelled') AND assigned_to IS NULL)
        OR status = 'delivered')
);
CREATE TABLE IF NOT EXISTS couriers (
    id TEXT PRIMARY KEY,
    location TEXT NOT NULL,
    capacity NUMERIC NOT NULL CHECK (capacity > 0),
    available_at NUMERIC NOT NULL,
    active INTEGER NOT NULL CHECK (active IN (0,1))
);
CREATE TABLE IF NOT EXISTS assignments (
    order_id TEXT PRIMARY KEY REFERENCES orders(id),
    courier_id TEXT NOT NULL REFERENCES couriers(id),
    idempotency_key TEXT
);
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    courier_id TEXT NOT NULL,
    response TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    event TEXT NOT NULL,
    order_id TEXT,
    courier_id TEXT,
    idempotency_key TEXT,
    details TEXT
);
"""

#: Gancho de injeção de falhas (apenas testes): ``hook(stage, conn)`` é chamado dentro da transação.
FaultHook = Callable[[str, sqlite3.Connection], None]


@dataclass(frozen=True)
class ProcessResult:
    """Resultado de ``process_order``. ``replayed=True`` quando a resposta veio da chave de idempotência."""

    order_id: str
    courier_id: str
    status: str
    replayed: bool


class OrderRepository:
    """Repositório SQLite. Cada operação abre sua própria conexão (seguro entre threads)."""

    def __init__(self, db_path: str | Path):
        if str(db_path) == ":memory:":
            raise ValidationError("use um arquivo: bancos :memory: não são compartilháveis entre conexões")
        self._path = str(db_path)
        self._fault_hook: Optional[FaultHook] = None
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
        finally:
            conn.close()

    # ---------------------------------------------------------------- infraestrutura
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=30.0, isolation_level=None)  # autocommit; transações manuais
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def _write_txn(self) -> Iterator[sqlite3.Connection]:
        """Transação de escrita: BEGIN IMMEDIATE ... COMMIT, com ROLLBACK em qualquer exceção."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    def _hook(self, stage: str, conn: sqlite3.Connection) -> None:
        if self._fault_hook is not None:
            self._fault_hook(stage, conn)

    @staticmethod
    def _audit(conn: sqlite3.Connection, event: str, order_id: Optional[str] = None,
               courier_id: Optional[str] = None, key: Optional[str] = None,
               details: Optional[dict[str, Any]] = None) -> None:
        conn.execute(
            "INSERT INTO audit_log(event, order_id, courier_id, idempotency_key, details) VALUES (?,?,?,?,?)",
            (event, order_id, courier_id, key, json.dumps(details or {}, ensure_ascii=False)),
        )

    @staticmethod
    def _to_order(row: sqlite3.Row) -> Order:
        return Order(id=row["id"], title=row["title"], priority=row["priority"], created_at=row["created_at"],
                     deadline=row["deadline"], weight=row["weight"], destination=row["destination"],
                     dependencies=tuple(json.loads(row["dependencies"])), status=OrderStatus(row["status"]),
                     assigned_to=row["assigned_to"])

    @staticmethod
    def _to_courier(row: sqlite3.Row) -> Courier:
        return Courier(id=row["id"], location=row["location"], capacity=row["capacity"],
                       available_at=row["available_at"], active=bool(row["active"]))

    # ---------------------------------------------------------------- cadastro e consulta
    def add_order(self, order: Order) -> None:
        """Insere um pedido. ID repetido -> DuplicateOrderError."""
        try:
            with self._write_txn() as conn:
                conn.execute(
                    "INSERT INTO orders(id,title,priority,created_at,deadline,weight,destination,dependencies,"
                    "status,assigned_to) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (order.id, order.title, order.priority, order.created_at, order.deadline, order.weight,
                     order.destination, json.dumps(list(order.dependencies)), order.status.value, order.assigned_to))
                self._audit(conn, "ORDER_CREATED", order.id)
        except sqlite3.IntegrityError as exc:
            raise DuplicateOrderError(f"pedido {order.id!r} já existe") from exc

    def add_courier(self, courier: Courier) -> None:
        """Insere um entregador."""
        try:
            with self._write_txn() as conn:
                conn.execute("INSERT INTO couriers(id,location,capacity,available_at,active) VALUES (?,?,?,?,?)",
                             (courier.id, courier.location, courier.capacity, courier.available_at,
                              int(courier.active)))
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"entregador {courier.id!r} já existe") from exc

    def get_order(self, order_id: str) -> Optional[Order]:
        """Pedido por ID ou None."""
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            return self._to_order(row) if row else None
        finally:
            conn.close()

    def list_orders(self) -> list[Order]:
        """Todos os pedidos, na ordem de inserção."""
        conn = self._connect()
        try:
            return [self._to_order(r) for r in conn.execute("SELECT * FROM orders ORDER BY rowid")]
        finally:
            conn.close()

    def get_courier(self, courier_id: str) -> Optional[Courier]:
        """Entregador por ID ou None."""
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM couriers WHERE id=?", (courier_id,)).fetchone()
            return self._to_courier(row) if row else None
        finally:
            conn.close()

    def list_couriers(self) -> list[Courier]:
        """Todos os entregadores."""
        conn = self._connect()
        try:
            return [self._to_courier(r) for r in conn.execute("SELECT * FROM couriers ORDER BY rowid")]
        finally:
            conn.close()

    def assignments(self) -> dict[str, str]:
        """Mapa pedido -> entregador das atribuições vigentes."""
        conn = self._connect()
        try:
            return {r["order_id"]: r["courier_id"] for r in conn.execute("SELECT * FROM assignments")}
        finally:
            conn.close()

    def audit_events(self, order_id: Optional[str] = None) -> list[dict[str, Any]]:
        """Trilha de auditoria (opcionalmente filtrada por pedido), em ordem cronológica."""
        conn = self._connect()
        try:
            sql, args = "SELECT * FROM audit_log", ()
            if order_id is not None:
                sql, args = sql + " WHERE order_id=?", (order_id,)
            return [{"id": r["id"], "event": r["event"], "order_id": r["order_id"], "courier_id": r["courier_id"],
                     "idempotency_key": r["idempotency_key"], "details": json.loads(r["details"] or "{}")}
                    for r in conn.execute(sql + " ORDER BY id", args)]
        finally:
            conn.close()

    # ---------------------------------------------------------------- transições
    def transition(self, order_id: str, new_status: OrderStatus) -> Order:
        """Muda o status respeitando a máquina de estados, com atualização condicional (compare-and-set)."""
        new_status = OrderStatus(new_status)
        with self._write_txn() as conn:
            row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if row is None:
                raise UnknownOrderError(f"pedido {order_id!r} não existe")
            current = OrderStatus(row["status"])
            ensure_transition(order_id, current, new_status)
            if new_status is OrderStatus.ASSIGNED:
                raise ValidationError(f"pedido {order_id!r}: use process_order para atribuir "
                                      "(exige entregador, capacidade e chave de idempotência)")
            clear_courier = new_status in (OrderStatus.PENDING, OrderStatus.CANCELLED)
            cur = conn.execute(
                "UPDATE orders SET status=?, assigned_to=CASE WHEN ? THEN NULL ELSE assigned_to END "
                "WHERE id=? AND status=?", (new_status.value, int(clear_courier), order_id, current.value))
            if cur.rowcount != 1:
                raise ConcurrentModificationError(f"pedido {order_id!r} mudou de status durante a operação")
            if clear_courier:  # libera a atribuição (capacidade e unicidade)
                conn.execute("DELETE FROM assignments WHERE order_id=?", (order_id,))
            self._audit(conn, "ORDER_STATUS_CHANGED", order_id, row["assigned_to"],
                        details={"from": current.value, "to": new_status.value})
            updated = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            return self._to_order(updated)

    # ---------------------------------------------------------------- atribuição idempotente
    def process_order(self, order_id: str, courier_id: str, idempotency_key: str) -> ProcessResult:
        """Atribui ``order_id`` a ``courier_id`` de forma atômica, sem duplicidade e idempotente.

        * Mesma chave + mesmos parâmetros -> devolve o resultado salvo (``replayed=True``), sem reexecutar.
        * Mesma chave + parâmetros diferentes -> ``IdempotencyConflictError``.
        * Pedido já atribuído (mesmo por outra chave) -> ``AlreadyAssignedError``.
        * Falhas de regra NÃO consomem a chave: o cliente pode corrigir e tentar de novo.
        """
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValidationError("idempotency_key é obrigatória e deve ser texto não vazio")
        try:
            with self._write_txn() as conn:
                return self._process_locked(conn, order_id, courier_id, idempotency_key)
        except Exception as exc:
            self._audit_rejection(exc, order_id, courier_id, idempotency_key)
            raise

    def _process_locked(self, conn: sqlite3.Connection, order_id: str, courier_id: str, key: str) -> ProcessResult:
        saved = conn.execute("SELECT * FROM idempotency_keys WHERE key=?", (key,)).fetchone()
        if saved is not None:
            if (saved["order_id"], saved["courier_id"]) != (order_id, courier_id):
                raise IdempotencyConflictError(
                    f"chave {key!r} já usada para {saved['order_id']!r}/{saved['courier_id']!r}; "
                    f"não pode ser reutilizada para {order_id!r}/{courier_id!r}")
            self._audit(conn, "ORDER_ASSIGN_REPLAYED", order_id, courier_id, key)
            data = json.loads(saved["response"])
            return ProcessResult(data["order_id"], data["courier_id"], data["status"], replayed=True)

        row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if row is None:
            raise UnknownOrderError(f"pedido {order_id!r} não existe")
        crow = conn.execute("SELECT * FROM couriers WHERE id=?", (courier_id,)).fetchone()
        if crow is None:
            raise UnknownCourierError(f"entregador {courier_id!r} não existe")
        order, courier = self._to_order(row), self._to_courier(crow)

        if order.status is OrderStatus.ASSIGNED or order.status is OrderStatus.IN_TRANSIT:
            raise AlreadyAssignedError(f"pedido {order_id!r} já está {order.status.value} (entregador {order.assigned_to!r})")
        ensure_transition(order.id, order.status, OrderStatus.ASSIGNED)
        if not courier.active:
            raise CourierUnavailableError(f"entregador {courier_id!r} está inativo")

        deps = list(dict.fromkeys(order.dependencies))
        if deps:
            marks = ",".join("?" * len(deps))
            delivered = {r["id"] for r in conn.execute(
                f"SELECT id FROM orders WHERE id IN ({marks}) AND status='delivered'", deps)}
            pendentes = [d for d in deps if d not in delivered]
            if pendentes:
                raise OrderBlockedError(f"pedido {order_id!r} bloqueado: dependências não entregues {pendentes}")

        load = conn.execute(
            "SELECT COALESCE(SUM(o.weight),0) FROM orders o JOIN assignments a ON a.order_id=o.id "
            "WHERE a.courier_id=? AND o.status IN ('assigned','in_transit')", (courier_id,)).fetchone()[0]
        if load + order.weight > courier.capacity:
            raise CapacityExceededError(
                f"entregador {courier_id!r}: carga {load:g} + {order.weight:g} excede a capacidade {courier.capacity:g}")

        self._hook("before_update", conn)
        cur = conn.execute("UPDATE orders SET status='assigned', assigned_to=? WHERE id=? AND status=?",
                           (courier_id, order_id, order.status.value))
        if cur.rowcount != 1:
            raise ConcurrentModificationError(f"pedido {order_id!r} mudou de status durante a operação")
        self._hook("after_update", conn)
        try:
            conn.execute("INSERT INTO assignments(order_id, courier_id, idempotency_key) VALUES (?,?,?)",
                         (order_id, courier_id, key))
        except sqlite3.IntegrityError as exc:
            raise AlreadyAssignedError(f"pedido {order_id!r} já possui atribuição") from exc
        self._hook("after_assignment", conn)
        response = {"order_id": order_id, "courier_id": courier_id, "status": OrderStatus.ASSIGNED.value}
        conn.execute("INSERT INTO idempotency_keys(key, order_id, courier_id, response) VALUES (?,?,?,?)",
                     (key, order_id, courier_id, json.dumps(response)))
        self._audit(conn, "ORDER_ASSIGNED", order_id, courier_id, key, {"from": order.status.value, "to": "assigned"})
        log_event(log, "order_assigned", order_id=order_id, courier_id=courier_id, idempotency_key=key)
        return ProcessResult(order_id, courier_id, OrderStatus.ASSIGNED.value, replayed=False)

    def _audit_rejection(self, exc: Exception, order_id: str, courier_id: str, key: str) -> None:
        """Registra a tentativa que falhou em transação própria (o rollback anterior não a apagou)."""
        try:
            with self._write_txn() as conn:
                self._audit(conn, "ORDER_ASSIGN_REJECTED", order_id, courier_id, key,
                            {"error": type(exc).__name__, "message": str(exc)})
        except Exception:  # auditoria é "best effort" aqui; nunca mascara o erro original
            log_event(log, "audit_rejection_failed", level=logging.ERROR, order_id=order_id)
        level = logging.WARNING if isinstance(exc, TaskMindError) else logging.ERROR
        log_event(log, "order_assign_rejected", level=level, order_id=order_id, courier_id=courier_id,
                  error=type(exc).__name__)
