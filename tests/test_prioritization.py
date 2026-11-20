"""Missão 1."""
import random
from functools import cmp_to_key

from app.domain.exceptions import DuplicateOrderError
from app.domain.models import OrderStatus
from app.services.prioritization import explain_rank, is_urgent, rank_orders
from tests.factories import make_order

import pytest


def ids(orders):
    return [o.id for o in orders]


def test_empty_input():
    assert rank_orders([], 0) == []


def test_urgent_beats_higher_priority():
    urgent_low = make_order("U", priority=1, deadline=20)
    normal_high = make_order("N", priority=5, deadline=500)
    assert ids(rank_orders([normal_high, urgent_low], 0)) == ["U", "N"]


def test_urgent_boundary_is_inclusive_at_30():
    assert is_urgent(make_order(deadline=30), 0) is True
    assert is_urgent(make_order(deadline=31), 0) is False
    assert ids(rank_orders([make_order("N", priority=5, deadline=31), make_order("U", priority=1, deadline=30)], 0)) == ["U", "N"]


def test_overdue_orders_are_urgent_and_most_overdue_first():
    a = make_order("A", priority=3, deadline=10)   # atrasado 40 min em t=50
    b = make_order("B", priority=3, deadline=40)   # atrasado 10 min
    assert ids(rank_orders([b, a], 50)) == ["A", "B"]


def test_priority_then_remaining_time_then_created_then_id():
    p5 = make_order("P5", priority=5, deadline=900)
    p4 = make_order("P4", priority=4, deadline=100)
    assert ids(rank_orders([p4, p5], 0)) == ["P5", "P4"]
    soon = make_order("S", priority=3, deadline=300)
    late = make_order("L", priority=3, deadline=400)
    assert ids(rank_orders([late, soon], 0)) == ["S", "L"]
    old = make_order("O", priority=3, deadline=300, created_at=1)
    new = make_order("W", priority=3, deadline=300, created_at=9)
    assert ids(rank_orders([new, old], 0)) == ["O", "W"]
    b = make_order("B", priority=3, deadline=300, created_at=1)
    a = make_order("A", priority=3, deadline=300, created_at=1)
    assert ids(rank_orders([b, a], 0)) == ["A", "B"]


@pytest.mark.parametrize("status", [OrderStatus.DELIVERED, OrderStatus.CANCELLED, OrderStatus.ASSIGNED, OrderStatus.IN_TRANSIT])
def test_non_candidates_never_appear(status):
    courier = "D1" if status in (OrderStatus.ASSIGNED, OrderStatus.IN_TRANSIT) else None
    assert rank_orders([make_order("X", status=status, assigned_to=courier)], 0) == []


def test_dependencies_block_until_delivered():
    p1 = make_order("P1")
    p2 = make_order("P2", dependencies=("P1",), priority=5)
    assert ids(rank_orders([p1, p2], 0)) == ["P1"]
    delivered = make_order("P1", status=OrderStatus.DELIVERED)
    assert ids(rank_orders([delivered, p2], 0)) == ["P2"]


def test_cancelled_or_missing_dependency_keeps_order_blocked():
    cancelled = make_order("P1", status=OrderStatus.CANCELLED)
    assert rank_orders([cancelled, make_order("P2", dependencies=("P1",))], 0) == []
    assert rank_orders([make_order("P2", dependencies=("NOPE",))], 0) == []


def test_stored_blocked_status_is_reevaluated():
    done = make_order("P1", status=OrderStatus.DELIVERED)
    blocked_but_ready = make_order("P2", dependencies=("P1",), status=OrderStatus.BLOCKED)
    assert ids(rank_orders([done, blocked_but_ready], 0)) == ["P2"]


def test_input_list_is_not_modified():
    orders = [make_order("B", priority=1), make_order("A", priority=5), make_order("C", status=OrderStatus.CANCELLED)]
    snapshot = list(orders)
    result = rank_orders(orders, 0)
    assert orders == snapshot and result is not orders


def test_duplicate_ids_rejected():
    with pytest.raises(DuplicateOrderError):
        rank_orders([make_order("P1"), make_order("P1")], 0)


def test_explanation_mentions_group_and_priority():
    text = explain_rank(make_order("P1", priority=4, deadline=20), 0)
    assert "URGENTE" in text and "prioridade 4" in text
    assert "normal" in explain_rank(make_order("P2", deadline=500), 0)


def _cmp(a, b, now):
    """Comparador independente (par a par) dos 5 critérios, usado como oráculo."""
    ua, ub = a.deadline - now <= 30, b.deadline - now <= 30
    if ua != ub:
        return -1 if ua else 1
    for x, y in ((-a.priority, -b.priority), (a.deadline - now, b.deadline - now),
                 (a.created_at, b.created_at), (a.id, b.id)):
        if x != y:
            return -1 if x < y else 1
    return 0


def test_matches_independent_oracle_on_random_data():
    rng = random.Random(7)
    for _ in range(20):
        now = rng.randint(0, 100)
        orders = []
        for i in range(150):
            created = rng.randint(0, 60)
            orders.append(make_order(f"P{i:04d}", priority=rng.randint(1, 5), created_at=created,
                                     deadline=created + rng.randint(0, 150)))
        expected = sorted(orders, key=cmp_to_key(lambda a, b: _cmp(a, b, now)))
        assert ids(rank_orders(orders, now)) == ids(expected)
