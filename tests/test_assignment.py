"""Missão 4."""
from app.domain.models import OrderStatus
from app.services.assignment import apply_plan, assign_orders
from tests.factories import classic_graph, make_courier, make_graph, make_order


def by_order(plan):
    return {a.order_id: a for a in plan.assignments}


def reasons(plan):
    return {u.order_id: u.reason for u in plan.unassigned}


def test_assigns_closest_courier():
    couriers = [make_courier("D1", location="A"), make_courier("D2", location="C")]
    plan = assign_orders([make_order("P1", destination="D")], couriers, classic_graph(), 0)
    a = by_order(plan)["P1"]
    assert a.courier_id == "D2" and a.travel_time == 3 and a.eta == 3 and not a.late


def test_tie_broken_by_courier_id_regardless_of_input_order():
    for couriers in ([make_courier("D1"), make_courier("D2")], [make_courier("D2"), make_courier("D1")]):
        plan = assign_orders([make_order("P1")], couriers, classic_graph(), 0)
        assert by_order(plan)["P1"].courier_id == "D1"


def test_prefers_on_time_courier_over_closer_but_late_one():
    """Minimizar atrasos vem ANTES de minimizar a viagem."""
    g = make_graph(("A", "X", 40), ("B", "X", 50), ("X", "Y", 10), ("B", "Y", 45))
    orders = [make_order("P1", priority=5, destination="X", deadline=1000),
              make_order("P2", priority=4, destination="Y", deadline=47)]
    plan = assign_orders(orders, [make_courier("D1", location="A"), make_courier("D2", location="B")], g, 0)
    got = by_order(plan)
    assert got["P1"].courier_id == "D1"
    # D1 chegaria em 50 (> 47, atrasado) apesar de viagem de 10; D2 chega em 45 (no prazo), com viagem de 45
    assert got["P2"].courier_id == "D2" and not got["P2"].late and got["P2"].eta == 45


def test_capacity_is_cumulative_per_courier():
    orders = [make_order("P1", weight=6, priority=5), make_order("P2", weight=6, priority=4)]
    plan = assign_orders(orders, [make_courier("D1", capacity=10)], classic_graph(), 0)
    assert list(by_order(plan)) == ["P1"]
    assert "capacidade" in reasons(plan)["P2"]


def test_capacity_boundary_is_inclusive():
    """Peso exatamente igual à capacidade restante cabe (10 == 10; 6 + 4 == 10)."""
    assert "P1" in by_order(assign_orders([make_order("P1", weight=10)], [make_courier("D1", capacity=10)], classic_graph(), 0))
    orders = [make_order("P1", weight=6, priority=5), make_order("P2", weight=4, priority=4)]
    plan = assign_orders(orders, [make_courier("D1", capacity=10)], classic_graph(), 0)
    assert set(by_order(plan)) == {"P1", "P2"}
    over = [make_order("P1", weight=10.5)]
    assert assign_orders(over, [make_courier("D1", capacity=10)], classic_graph(), 0).assignments == ()


def test_courier_without_capacity_leaves_order_unassigned():
    plan = assign_orders([make_order("P1", weight=50)], [make_courier("D1", capacity=10)], classic_graph(), 0)
    assert plan.assignments == () and "capacidade" in reasons(plan)["P1"]


def test_already_committed_load_counts_against_capacity():
    committed = make_order("OLD", weight=8, status=OrderStatus.ASSIGNED, assigned_to="D1")
    plan = assign_orders([committed, make_order("P1", weight=5)], [make_courier("D1", capacity=10)], classic_graph(), 0)
    assert "P1" not in by_order(plan) and "capacidade" in reasons(plan)["P1"]


def test_inactive_or_not_yet_available_couriers_are_ignored():
    couriers = [make_courier("D1", active=False), make_courier("D2", available_at=10)]
    plan = assign_orders([make_order("P1")], couriers, classic_graph(), 5)
    assert plan.assignments == () and "disponível" in reasons(plan)["P1"]
    assert by_order(assign_orders([make_order("P1")], couriers, classic_graph(), 10))["P1"].courier_id == "D2"


def test_unreachable_destination_stays_unassigned():
    g = make_graph(("A", "B", 1), ("X", "Y", 1))
    plan = assign_orders([make_order("P1", destination="Y")], [make_courier("D1")], g, 0)
    assert plan.assignments == () and "sem rota" in reasons(plan)["P1"]


def test_unknown_destination_stays_unassigned():
    plan = assign_orders([make_order("P1", destination="NOWHERE")], [make_courier("D1")], classic_graph(), 0)
    assert "não existe no mapa" in reasons(plan)["P1"]


def test_impossible_deadline_is_flagged_not_hidden():
    plan = assign_orders([make_order("P1", destination="B", deadline=3)], [make_courier("D1")], classic_graph(), 0)
    a = by_order(plan)["P1"]
    assert a.late and a.eta == 5 and a.reason.startswith("RISCO") and plan.at_risk == (a,)


def test_already_overdue_order_is_flagged():
    plan = assign_orders([make_order("P1", deadline=10)], [make_courier("D1", available_at=0)], classic_graph(), 50)
    assert by_order(plan)["P1"].late


def test_blocked_orders_are_not_assigned_but_reported():
    orders = [make_order("P1"), make_order("P2", dependencies=("P1",), priority=5)]
    plan = assign_orders(orders, [make_courier("D1"), make_courier("D2")], classic_graph(), 0)
    assert list(by_order(plan)) == ["P1"] and plan.blocked == {"P2": ("P1",)}
    assert "P2" not in reasons(plan)


def test_terminal_and_committed_orders_are_skipped():
    orders = [make_order("D", status=OrderStatus.DELIVERED), make_order("C", status=OrderStatus.CANCELLED),
              make_order("A", status=OrderStatus.ASSIGNED, assigned_to="D1")]
    plan = assign_orders(orders, [make_courier("D1")], classic_graph(), 0)
    assert plan.assignments == () and plan.unassigned == ()


def test_sequential_chain_updates_courier_position_and_time():
    orders = [make_order("P1", priority=5, destination="B"), make_order("P2", priority=4, destination="C")]
    got = by_order(assign_orders(orders, [make_courier("D1", location="A")], classic_graph(), 0))
    assert (got["P1"].departure, got["P1"].eta) == (0, 5)
    assert (got["P2"].departure, got["P2"].travel_time, got["P2"].eta) == (5, 7, 12)  # B->C, não A->C(20)


def test_urgent_order_is_served_first_when_capacity_is_scarce():
    orders = [make_order("N", priority=5, weight=8, deadline=900), make_order("U", priority=1, weight=8, deadline=10)]
    plan = assign_orders(orders, [make_courier("D1", capacity=10)], classic_graph(), 0)
    assert list(by_order(plan)) == ["U"]


def test_empty_inputs():
    assert assign_orders([], [], classic_graph(), 0).assignments == ()
    assert "disponível" in reasons(assign_orders([make_order("P1")], [], classic_graph(), 0))["P1"]


def test_does_not_mutate_inputs_and_apply_plan_returns_new_orders():
    orders = [make_order("P1"), make_order("P2", destination="C")]
    snapshot = list(orders)
    plan = assign_orders(orders, [make_courier("D1")], classic_graph(), 0)
    assert orders == snapshot and all(o.status is OrderStatus.PENDING for o in orders)
    updated = apply_plan(orders, plan)
    assert all(o.status is OrderStatus.ASSIGNED and o.assigned_to == "D1" for o in updated)
    assert all(o.status is OrderStatus.PENDING for o in orders)


def test_apply_plan_accepts_stored_blocked_orders_and_keeps_invariants():
    blocked = make_order("PB", status=OrderStatus.BLOCKED)
    plan = assign_orders([blocked], [make_courier("D1")], classic_graph(), 0)
    updated = apply_plan([blocked], plan)[0]
    assert updated.status is OrderStatus.ASSIGNED and updated.assigned_to == "D1"


def test_deterministic():
    orders = [make_order(f"P{i}", priority=1 + i % 5, destination="BCD"[i % 3], deadline=100 + i) for i in range(30)]
    couriers = [make_courier(f"D{i}", location="ABC"[i % 3], capacity=40) for i in range(4)]
    assert assign_orders(orders, couriers, classic_graph(), 0) == assign_orders(orders, couriers, classic_graph(), 0)
