"""Missão 6 (e cenários de regressão do replanejamento)."""
import random

import pytest

from app.domain.exceptions import CycleError, ValidationError
from app.domain.models import OrderStatus
from app.services.simulation import simulate
from tests.factories import classic_graph, make_courier, make_order

S = OrderStatus


def ev(id, time, type, **kw):
    return {"id": id, "time": time, "type": type, **kw}


def chain_setup():
    """D1 sozinho: P1 (A->B) sai agora; P2 (B->C) fica na fila (assigned). D2 começa inativo."""
    orders = [make_order("P1", priority=5, destination="B"), make_order("P2", priority=4, destination="C")]
    couriers = [make_courier("D1"), make_courier("D2", active=False)]
    return orders, couriers


def check_invariants(res):
    for o in res.orders.values():
        if o.status in (S.ASSIGNED, S.IN_TRANSIT):
            assert o.assigned_to in res.couriers
            assert res.plan[o.id].courier_id == o.assigned_to
        else:
            assert o.id not in res.plan  # cancelado/entregue/pendente nunca fica no plano
            if o.status in (S.PENDING, S.CANCELLED):
                assert o.assigned_to is None
        if o.status is S.ASSIGNED:
            assert res.couriers[o.assigned_to].active  # fila de entregador inativo deve ter sido liberada
    load = {}
    for o in res.orders.values():
        if o.status in (S.ASSIGNED, S.IN_TRANSIT):
            load[o.assigned_to] = load.get(o.assigned_to, 0) + o.weight
    for cid, total in load.items():
        assert total <= res.couriers[cid].capacity


def test_initial_plan_and_dispatch():
    orders, couriers = chain_setup()
    res = simulate(orders, couriers, classic_graph(), [])
    assert res.history[0]["event_id"] == "START"
    assert res.orders["P1"].status is S.IN_TRANSIT and res.orders["P2"].status is S.ASSIGNED
    assert res.plan["P2"].departure == 5
    check_invariants(res)


def test_courier_unavailable_releases_queued_orders_and_replans():
    orders, couriers = chain_setup()
    res = simulate(orders, couriers, classic_graph(), [ev("E1", 1, "COURIER_AVAILABLE", courier_id="D2"),
                                                       ev("E2", 2, "COURIER_UNAVAILABLE", courier_id="D1")])
    assert res.history[-1]["released"] == ["P2"]
    assert res.orders["P2"].assigned_to == "D2" and res.orders["P2"].status is S.IN_TRANSIT
    assert res.orders["P1"].assigned_to == "D1" and res.orders["P1"].status is S.IN_TRANSIT  # já saiu: continua
    assert res.plan["P2"].eta == 2 + 12  # replanejado a partir de t=2, A->B->C
    check_invariants(res)


def test_cancelled_order_is_never_assigned_later():
    orders = [make_order("P1", destination="B")]
    res = simulate(orders, [make_courier("D1", active=False)], classic_graph(), [
        ev("E1", 1, "ORDER_CANCELLED", order_id="P1"),
        ev("E2", 2, "COURIER_AVAILABLE", courier_id="D1"),
        ev("E3", 3, "ORDER_CREATED", order={"id": "P2", "title": "x", "priority": 1, "created_at": 3, "deadline": 99,
                                           "weight": 1, "destination": "C"}),
    ])
    assert res.orders["P1"].status is S.CANCELLED and res.orders["P1"].assigned_to is None
    assert res.orders["P2"].assigned_to == "D1"
    assert all(a["order_id"] != "P1" for h in res.history for a in h["assigned"])
    check_invariants(res)


def test_cancelling_a_queued_order_frees_it_from_the_plan():
    orders, couriers = chain_setup()
    res = simulate(orders, couriers, classic_graph(), [ev("E1", 1, "ORDER_CANCELLED", order_id="P2")])
    assert res.orders["P2"].status is S.CANCELLED and "P2" not in res.plan
    check_invariants(res)


def test_delivery_unblocks_dependents_and_replans_from_new_position():
    orders = [make_order("P1", destination="B"), make_order("P3", destination="C", dependencies=("P1",))]
    res0 = simulate(orders, [make_courier("D1")], classic_graph(), [])
    assert res0.orders["P3"].status is S.PENDING  # bloqueado
    res = simulate(orders, [make_courier("D1")], classic_graph(), [ev("E1", 5, "ORDER_DELIVERED", order_id="P1")])
    assert res.orders["P1"].status is S.DELIVERED
    assert res.orders["P3"].assigned_to == "D1" and res.plan["P3"].travel_time == 7  # B->C, saindo de B
    assert res.couriers["D1"].location == "B"
    check_invariants(res)


def test_busy_courier_does_not_take_new_orders_until_it_is_free():
    new = {"id": "P9", "title": "x", "priority": 5, "created_at": 1, "deadline": 99, "weight": 1, "destination": "C"}
    orders = [make_order("P1", destination="B")]
    mid = simulate(orders, [make_courier("D1")], classic_graph(), [ev("E1", 1, "ORDER_CREATED", order=new)])
    assert mid.orders["P9"].status is S.PENDING and "P9" in mid.unassigned  # D1 está em trânsito com P1
    res = simulate(orders, [make_courier("D1")], classic_graph(),
                   [ev("E1", 1, "ORDER_CREATED", order=new), ev("E2", 5, "ORDER_DELIVERED", order_id="P1")])
    assert res.orders["P9"].assigned_to == "D1" and res.plan["P9"].travel_time == 7  # saindo de B, não de A
    check_invariants(res)


def test_stored_blocked_order_with_satisfied_dependencies_is_planned_through_valid_transitions():
    orders = [make_order("PB", status=S.BLOCKED, destination="B")]
    res = simulate(orders, [make_courier("D1")], classic_graph(), [])
    assert res.orders["PB"].status is S.IN_TRANSIT and res.orders["PB"].assigned_to == "D1"
    check_invariants(res)


def test_unavailable_courier_releases_orders_with_courier_cleared():
    orders, couriers = chain_setup()
    res = simulate(orders, couriers, classic_graph(), [ev("E1", 1, "COURIER_UNAVAILABLE", courier_id="D1")])
    assert res.orders["P2"].status is S.PENDING and res.orders["P2"].assigned_to is None
    check_invariants(res)


def test_same_event_twice_does_not_duplicate_effects():
    orders, couriers = chain_setup()
    new = {"id": "P9", "title": "x", "priority": 1, "created_at": 0, "deadline": 99, "weight": 1, "destination": "D"}
    once = simulate(orders, couriers, classic_graph(), [ev("E1", 1, "ORDER_CREATED", order=new)])
    twice = simulate(orders, couriers, classic_graph(), [ev("E1", 1, "ORDER_CREATED", order=new),
                                                         ev("E1", 1, "ORDER_CREATED", order=new)])
    assert twice.history[-1]["outcome"] == "duplicate_ignored"
    assert twice.orders == once.orders and twice.plan == once.plan
    # entrega duplicada também não reaplica efeitos
    d1 = [ev("E1", 5, "ORDER_DELIVERED", order_id="P1")]
    assert simulate(orders, couriers, classic_graph(), d1 + d1).couriers == simulate(orders, couriers, classic_graph(), d1).couriers


def test_same_order_with_different_event_id_is_rejected_not_duplicated():
    orders, couriers = chain_setup()
    new = {"id": "P9", "title": "x", "priority": 1, "created_at": 0, "deadline": 99, "weight": 1, "destination": "D"}
    res = simulate(orders, couriers, classic_graph(), [ev("E1", 1, "ORDER_CREATED", order=new),
                                                       ev("E2", 2, "ORDER_CREATED", order=new)])
    assert res.history[-1]["outcome"] == "rejected" and "duplicado" in res.history[-1]["detail"]


def test_invalid_events_are_rejected_with_reason_and_do_not_break_the_run():
    orders, couriers = chain_setup()
    res = simulate(orders, couriers, classic_graph(), [
        ev("E1", 1, "ORDER_CANCELLED", order_id="NOPE"),            # inexistente
        ev("E2", 2, "ORDER_CANCELLED", order_id="P1"),               # já em trânsito
        ev("E3", 3, "ORDER_DELIVERED", order_id="P2"),               # ainda não saiu
        ev("E4", 4, "COURIER_UNAVAILABLE", courier_id="D2"),         # já inativo
        ev("E5", 5, "COURIER_AVAILABLE", courier_id="GHOST"),        # inexistente
        ev("E6", 6, "ORDER_CREATED", order={"id": "P8", "title": "x", "priority": 1, "created_at": 0, "deadline": 9,
                                           "weight": 1, "destination": "B", "dependencies": ["???"]}),
    ])
    outcomes = [h["outcome"] for h in res.history[1:]]
    assert outcomes == ["rejected"] * 6
    assert "in_transit" in res.history[2]["detail"] and "GHOST" in res.history[5]["detail"]
    assert res.orders["P1"].status is S.IN_TRANSIT and "P8" not in res.orders
    check_invariants(res)


def test_malformed_events_fail_fast():
    orders, couriers = chain_setup()
    for bad in ({"id": "E", "time": 1, "type": "EXPLODE"}, {"time": 1, "type": "ORDER_CANCELLED", "order_id": "P1"},
                {"id": "E", "time": "x", "type": "ORDER_CANCELLED", "order_id": "P1"},
                {"id": "E", "time": 1, "type": "ORDER_CANCELLED"}, {"id": "E", "time": 1, "type": "ORDER_CREATED"}):
        with pytest.raises(ValidationError):
            simulate(orders, couriers, classic_graph(), [bad])


def test_circular_initial_configuration_is_rejected():
    orders = [make_order("P1", dependencies=("P2",)), make_order("P2", dependencies=("P1",))]
    with pytest.raises(CycleError):
        simulate(orders, [make_courier("D1")], classic_graph(), [])


def test_replay_is_reproducible_and_events_are_ordered_by_time():
    orders, couriers = chain_setup()
    events = [ev("E1", 1, "COURIER_AVAILABLE", courier_id="D2"), ev("E2", 2, "COURIER_UNAVAILABLE", courier_id="D1")]
    a = simulate(orders, couriers, classic_graph(), events)
    b = simulate(orders, couriers, classic_graph(), list(events))
    c = simulate(orders, couriers, classic_graph(), list(reversed(events)))  # fora de ordem na entrada
    assert a.history == b.history and a.fingerprint() == b.fingerprint() == c.fingerprint()


def test_inputs_are_not_mutated():
    orders, couriers = chain_setup()
    o_snap, c_snap = list(orders), list(couriers)
    simulate(orders, couriers, classic_graph(), [ev("E1", 1, "COURIER_AVAILABLE", courier_id="D2")])
    assert orders == o_snap and couriers == c_snap


def test_at_risk_is_exposed():
    res = simulate([make_order("P1", destination="B", deadline=2)], [make_courier("D1")], classic_graph(), [])
    assert res.at_risk == ["P1"]


def test_randomized_event_streams_keep_invariants_and_are_deterministic():
    for seed in range(25):
        rng = random.Random(seed)
        orders = [make_order(f"P{i}", priority=rng.randint(1, 5), destination=rng.choice("BCD"), weight=rng.randint(1, 20),
                             deadline=rng.randint(10, 80), dependencies=(f"P{i-1}",) if i and rng.random() < .3 else ())
                  for i in range(8)]
        couriers = [make_courier(f"D{i}", location=rng.choice("ABC"), capacity=rng.randint(15, 40)) for i in range(3)]
        events = []
        for k in range(40):
            t = rng.randint(0, 60)
            kind = rng.choice(["ORDER_CANCELLED", "ORDER_DELIVERED", "COURIER_UNAVAILABLE", "COURIER_AVAILABLE", "ORDER_CREATED"])
            if kind == "ORDER_CREATED":
                events.append(ev(f"E{k}", t, kind, order={"id": f"N{k}", "title": "n", "priority": rng.randint(1, 5),
                                                          "created_at": t, "deadline": t + rng.randint(5, 60),
                                                          "weight": rng.randint(1, 20), "destination": rng.choice("BCD")}))
            elif kind.startswith("ORDER"):
                events.append(ev(f"E{k}", t, kind, order_id=f"P{rng.randint(0, 9)}"))
            else:
                events.append(ev(f"E{k}", t, kind, courier_id=f"D{rng.randint(0, 3)}"))
        r1 = simulate(orders, couriers, classic_graph(), events)
        r2 = simulate(orders, couriers, classic_graph(), events)
        assert r1.fingerprint() == r2.fingerprint()
        check_invariants(r1)
