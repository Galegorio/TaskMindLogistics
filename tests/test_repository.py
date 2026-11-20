"""Missão 5: integridade, idempotência, concorrência e falhas no meio da transação."""
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.domain.exceptions import (AlreadyAssignedError, CapacityExceededError, ConcurrentModificationError,
                                   CourierUnavailableError, DuplicateOrderError, IdempotencyConflictError,
                                   InvalidTransitionError, OrderBlockedError, UnknownCourierError,
                                   UnknownOrderError, ValidationError)
from app.domain.models import OrderStatus
from app.repositories.orders import OrderRepository
from tests.factories import make_courier, make_order

S = OrderStatus


def seed(repo, n_couriers=2, **order_kw):
    repo.add_order(make_order("P1", **order_kw))
    for i in range(1, n_couriers + 1):
        repo.add_courier(make_courier(f"D{i}", capacity=10))


def events(repo, name, order_id="P1"):
    return [e for e in repo.audit_events(order_id) if e["event"] == name]


def raw(repo, sql, args=()):
    conn = sqlite3.connect(repo._path)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def test_roundtrip_and_duplicates(repo):
    o = make_order("P1", dependencies=("P0",), weight=2.5)
    repo.add_order(o)
    assert repo.get_order("P1") == o and repo.get_order("NOPE") is None
    with pytest.raises(DuplicateOrderError):
        repo.add_order(o)
    repo.add_courier(make_courier("D1"))
    with pytest.raises(ValidationError, match="já existe"):
        repo.add_courier(make_courier("D1"))
    assert [c.id for c in repo.list_couriers()] == ["D1"]


def test_happy_path_updates_everything_and_audits(repo):
    seed(repo)
    r = repo.process_order("P1", "D1", "k1")
    assert (r.status, r.replayed) == ("assigned", False)
    order = repo.get_order("P1")
    assert order.status is S.ASSIGNED and order.assigned_to == "D1"
    assert repo.assignments() == {"P1": "D1"}
    assert len(events(repo, "ORDER_ASSIGNED")) == 1


def test_idempotent_replay_does_not_execute_again(repo):
    seed(repo)
    first = repo.process_order("P1", "D1", "k1")
    again = repo.process_order("P1", "D1", "k1")
    assert (first.replayed, again.replayed) == (False, True)
    assert repo.assignments() == {"P1": "D1"}
    assert len(events(repo, "ORDER_ASSIGNED")) == 1 and len(events(repo, "ORDER_ASSIGN_REPLAYED")) == 1
    repo.transition("P1", S.PENDING)  # mesmo que o estado mude depois, a chave devolve a resposta original
    assert repo.process_order("P1", "D1", "k1").replayed is True
    assert repo.get_order("P1").status is S.PENDING


def test_same_key_with_different_payload_conflicts(repo):
    seed(repo)
    repo.process_order("P1", "D1", "k1")
    with pytest.raises(IdempotencyConflictError):
        repo.process_order("P1", "D2", "k1")
    assert repo.get_order("P1").assigned_to == "D1"


def test_second_assignment_with_new_key_is_rejected_and_state_unchanged(repo):
    seed(repo)
    repo.process_order("P1", "D1", "k1")
    with pytest.raises(AlreadyAssignedError, match="D1"):
        repo.process_order("P1", "D2", "k2")
    assert repo.assignments() == {"P1": "D1"} and repo.get_order("P1").assigned_to == "D1"
    assert len(events(repo, "ORDER_ASSIGN_REJECTED")) == 1
    assert raw(repo, "SELECT COUNT(*) FROM idempotency_keys WHERE key='k2'")[0][0] == 0  # falha não consome a chave


def test_business_rule_rejections(repo):
    seed(repo)
    repo.add_courier(make_courier("OFF", active=False))
    with pytest.raises(UnknownOrderError):
        repo.process_order("NOPE", "D1", "a")
    with pytest.raises(UnknownCourierError):
        repo.process_order("P1", "NOPE", "b")
    with pytest.raises(CourierUnavailableError):
        repo.process_order("P1", "OFF", "c")
    with pytest.raises(ValidationError):
        repo.process_order("P1", "D1", "  ")
    assert repo.get_order("P1").status is S.PENDING and repo.assignments() == {}


def test_capacity_counts_assigned_load_and_is_freed_by_cancellation(repo):
    seed(repo, weight=6)
    repo.add_order(make_order("P2", weight=6))
    repo.process_order("P1", "D1", "a")
    with pytest.raises(CapacityExceededError):
        repo.process_order("P2", "D1", "b")
    repo.transition("P1", S.CANCELLED)
    assert repo.process_order("P2", "D1", "b").replayed is False
    assert repo.assignments() == {"P2": "D1"}


def test_dependencies_block_assignment_until_delivered(repo):
    seed(repo)
    repo.add_order(make_order("P2", dependencies=("P1",)))
    with pytest.raises(OrderBlockedError, match="P1"):
        repo.process_order("P2", "D2", "x")
    repo.process_order("P1", "D1", "a")
    repo.transition("P1", S.IN_TRANSIT)
    repo.transition("P1", S.DELIVERED)
    assert repo.process_order("P2", "D2", "y").status == "assigned"


def test_order_cancelled_during_planning_cannot_be_assigned(repo):
    seed(repo)
    repo.transition("P1", S.CANCELLED)
    with pytest.raises(InvalidTransitionError):
        repo.process_order("P1", "D1", "late-request")
    assert repo.get_order("P1").status is S.CANCELLED and repo.assignments() == {}


def test_invalid_transitions_are_rejected(repo):
    seed(repo)
    with pytest.raises(UnknownOrderError):
        repo.transition("NOPE", S.CANCELLED)
    with pytest.raises(InvalidTransitionError):
        repo.transition("P1", S.DELIVERED)       # pending -> delivered
    repo.process_order("P1", "D1", "a")
    with pytest.raises(InvalidTransitionError):
        repo.transition("P1", S.DELIVERED)       # assigned -> delivered (falta in_transit)
    repo.transition("P1", S.IN_TRANSIT)
    with pytest.raises(InvalidTransitionError):
        repo.transition("P1", S.CANCELLED)       # em trânsito não cancela
    repo.transition("P1", S.DELIVERED)
    for target in (S.PENDING, S.CANCELLED, S.ASSIGNED):
        with pytest.raises(InvalidTransitionError):
            repo.transition("P1", target)        # estado final
    assert repo.get_order("P1").status is S.DELIVERED


def test_release_to_pending_frees_the_assignment_slot(repo):
    seed(repo)
    repo.process_order("P1", "D1", "a")
    repo.transition("P1", S.PENDING)
    assert repo.assignments() == {} and repo.get_order("P1").assigned_to is None
    assert repo.process_order("P1", "D2", "b").courier_id == "D2"
    assert [e["details"]["to"] for e in events(repo, "ORDER_STATUS_CHANGED")] == ["pending"]


# ------------------------------------------------------------------ falhas no meio da transação
@pytest.mark.parametrize("stage", ["before_update", "after_update", "after_assignment"])
def test_failure_mid_transaction_leaves_no_partial_state(repo, stage):
    seed(repo)

    def boom(s, conn):
        if s == stage:
            raise RuntimeError("falha injetada")

    repo._fault_hook = boom
    with pytest.raises(RuntimeError, match="falha injetada"):
        repo.process_order("P1", "D1", "k1")
    assert repo.get_order("P1").status is S.PENDING and repo.get_order("P1").assigned_to is None
    assert repo.assignments() == {}
    assert raw(repo, "SELECT COUNT(*) FROM idempotency_keys")[0][0] == 0
    assert events(repo, "ORDER_ASSIGNED") == [] and len(events(repo, "ORDER_ASSIGN_REJECTED")) == 1
    repo._fault_hook = None  # a mesma chave pode ser reenviada com sucesso
    assert repo.process_order("P1", "D1", "k1").replayed is False
    assert repo.get_order("P1").status is S.ASSIGNED


def test_conditional_update_catches_status_changed_between_check_and_write(repo):
    """Simula a corrida check-then-act: outro escritor muda o status depois da checagem."""
    seed(repo)

    def racer(stage, conn):
        if stage == "before_update":
            conn.execute("UPDATE orders SET status='cancelled' WHERE id='P1'")

    repo._fault_hook = racer
    with pytest.raises(ConcurrentModificationError):
        repo.process_order("P1", "D1", "k1")
    repo._fault_hook = None
    assert repo.get_order("P1").status is S.PENDING and repo.assignments() == {}  # tudo revertido


# ------------------------------------------------------------------ restrições do próprio banco
def test_database_enforces_single_assignment_even_if_application_is_bypassed(repo):
    seed(repo)
    repo.process_order("P1", "D1", "k1")
    with pytest.raises(sqlite3.IntegrityError):
        sqlite3.connect(repo._path).execute("INSERT INTO assignments(order_id, courier_id) VALUES ('P1','D2')")


def test_database_rejects_invalid_status_and_priority(repo):
    seed(repo)
    conn = sqlite3.connect(repo._path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE orders SET status='flying' WHERE id='P1'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE orders SET priority=9 WHERE id='P1'")
    conn.close()


# ------------------------------------------------------------------ concorrência
def run_parallel(repo, jobs):
    barrier = threading.Barrier(len(jobs))

    def run(job):
        barrier.wait()
        try:
            return ("ok", repo.process_order(*job))
        except Exception as exc:  # noqa: BLE001
            return ("err", exc)

    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        return list(pool.map(run, jobs))


def test_concurrent_assignments_of_same_order_yield_exactly_one_winner(repo):
    for round_ in range(5):
        oid = f"R{round_}"
        repo.add_order(make_order(oid))
    for i in range(8):
        repo.add_courier(make_courier(f"D{i}", capacity=100))
    for round_ in range(5):
        oid = f"R{round_}"
        results = run_parallel(repo, [(oid, f"D{i}", f"{oid}-key{i}") for i in range(8)])
        winners = [r for kind, r in results if kind == "ok"]
        losers = [r for kind, r in results if kind == "err"]
        assert len(winners) == 1
        assert len(losers) == 7 and all(isinstance(e, AlreadyAssignedError) for e in losers)
        assert raw(repo, "SELECT COUNT(*) FROM assignments WHERE order_id=?", (oid,))[0][0] == 1
        assert repo.get_order(oid).assigned_to == winners[0].courier_id
        assert len(events(repo, "ORDER_ASSIGNED", oid)) == 1


def test_concurrent_duplicate_requests_with_same_key_apply_once(repo):
    seed(repo)
    results = run_parallel(repo, [("P1", "D1", "same-key")] * 8)
    assert all(kind == "ok" for kind, _ in results)
    assert sorted(r.replayed for _, r in results) == [False] + [True] * 7
    assert repo.assignments() == {"P1": "D1"}
    assert len(events(repo, "ORDER_ASSIGNED")) == 1 and len(events(repo, "ORDER_ASSIGN_REPLAYED")) == 7


def test_concurrent_requests_cannot_exceed_courier_capacity(repo):
    repo.add_courier(make_courier("D1", capacity=10))
    for i in range(6):
        repo.add_order(make_order(f"W{i}", weight=4))
    results = run_parallel(repo, [(f"W{i}", "D1", f"k{i}") for i in range(6)])
    assert sum(kind == "ok" for kind, _ in results) == 2  # 2 x 4 = 8 <= 10; o terceiro estouraria
    assert all(isinstance(r, CapacityExceededError) for kind, r in results if kind == "err")


def test_memory_db_is_refused(tmp_path):
    with pytest.raises(ValidationError):
        OrderRepository(":memory:")


# ------------------------------------------------------------------ regressões da auditoria (M-01, M-04)
def test_db_check_constraint_enforces_status_courier_invariant(repo):
    seed(repo)
    conn = sqlite3.connect(repo._path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE orders SET status='assigned' WHERE id='P1'")           # assigned sem entregador
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE orders SET assigned_to='D1' WHERE id='P1'")            # pending com entregador
    conn.close()


def test_transition_cannot_assign_only_process_order_can(repo):
    seed(repo)
    with pytest.raises(ValidationError, match="process_order"):
        repo.transition("P1", S.ASSIGNED)
    assert repo.get_order("P1").status is S.PENDING and repo.assignments() == {}


def test_blocked_order_can_be_assigned_once_dependencies_are_delivered(repo):
    seed(repo)
    repo.add_order(make_order("PB", status=S.BLOCKED, dependencies=("P1",)))
    with pytest.raises(OrderBlockedError):
        repo.process_order("PB", "D2", "x")                    # dependência ainda pendente
    repo.process_order("P1", "D1", "a")
    repo.transition("P1", S.IN_TRANSIT)
    repo.transition("P1", S.DELIVERED)
    assert repo.process_order("PB", "D2", "y").status == "assigned"
    assert repo.get_order("PB").status is S.ASSIGNED and repo.get_order("PB").assigned_to == "D2"
